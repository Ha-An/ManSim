from __future__ import annotations

import copy
import csv
import dataclasses
import json
from pathlib import Path
import pickle
import random
import tempfile
import unittest

import numpy as np
from omegaconf import OmegaConf

from manufacturing_sim.adp.compact import compact_episode_transitions, merge_compact_batches, select_compact_samples
from manufacturing_sim.adp.model import build_value_network, predict_values, require_torch
from manufacturing_sim.adp.schema import EncodedDecisionState, GLOBAL_FEATURE_DIM, WORKER_FEATURE_DIM, TASK_FEATURE_DIM, PAIR_FEATURE_DIM
from manufacturing_sim.adp.td import (CompactTDData, ReplayTask, build_target_plan, fit_td_value,
    retain_recent_episodes, sample_replay_episodes, soft_update, truncated_return_plan, greedy_mc_errors)
from manufacturing_sim.adp.td_dashboard import render_td_dashboard
from manufacturing_sim.adp.td_train import (
    _piecewise_schedule_points,
    _piecewise_schedule_value,
    validate_td_config,
)


def state(time: float = 0.0) -> EncodedDecisionState:
    return EncodedDecisionState(
        global_features=np.zeros(GLOBAL_FEATURE_DIM, np.float32),
        worker_features=np.zeros((2, WORKER_FEATURE_DIM), np.float32),
        task_features=np.zeros((2, TASK_FEATURE_DIM), np.float32),
        pair_features=np.zeros((2, 2, PAIR_FEATURE_DIM), np.float32),
        feasibility=np.ones((2, 2), bool), worker_ids=["A1", "A2"], opportunity_ids=["O1", "O2"],
        time_min=time, horizon_min=100.0, tasks_by_worker={w: {
            "O1": ReplayTask("LOAD_MACHINE", {"_adp_resource_keys": ["item:X"]}),
            "O2": ReplayTask("TRANSFER", {"_adp_resource_keys": ["item:X"]})
        } for w in ("A1", "A2")})


def episode(eid: int = 1):
    s1, s2 = state(), state(1.5)
    rows = [{"state": s1, "post_state": s1.post_decision({"A1": "O1", "A2": None}),
             "raw_reward": 1.0, "done": False, "next_state": s2},
            {"state": s2, "post_state": s2.post_decision({"A1": None, "A2": "O1"}),
             "raw_reward": 2.0, "done": True, "next_state": None}]
    batch = compact_episode_transitions(rows, episode_id=eid, worker_count=2)
    batch.td_data = CompactTDData.from_transitions(rows, eid, 2)
    return batch, rows


class TDTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.torch = require_torch()
        cls.torch.set_num_threads(1)

    def test_n_step_terminal_and_episode_boundaries(self):
        rewards, endpoints, steps, reasons = truncated_return_plan([1, 2, 3, 100], [False, False, True, True],
            [1, 1, 1, 2], [True] * 4, 10)
        self.assertEqual(rewards, [6, 5, 3, 100])
        self.assertEqual(endpoints, [-1] * 4)
        self.assertEqual(steps, [3, 2, 1, 1])
        self.assertEqual(reasons, ["terminal"] * 4)

    def test_one_step(self):
        sums, ends, steps, _ = truncated_return_plan([1, 2, 3], [False, False, True], [1] * 3, [False] * 3, 1)
        self.assertEqual(sums, [1, 2, 3])
        self.assertEqual(ends, [1, 2, -1])
        self.assertEqual(steps, [1] * 3)

    def test_cut_before_off_policy_reward(self):
        sums, ends, steps, reasons = truncated_return_plan([1, 99, 3], [False, False, True], [1] * 3, [False, False, True], 10)
        self.assertEqual((sums[0], ends[0], steps[0], reasons[0]), (1, 1, 1, "off_policy"))
        self.assertEqual(sums[1], 102)

    def test_missing_terminal_rejected(self):
        with self.assertRaises(ValueError):
            truncated_return_plan([1], [False], [1], [True], 10)
        with self.assertRaises(ValueError):
            truncated_return_plan([1], [True], [1], [True], 0)

    def test_compact_roundtrip_preserves_resource_conflicts(self):
        from manufacturing_sim.adp.policy import random_feasible_matching
        batch, rows = episode()
        restored = pickle.loads(pickle.dumps(batch))
        encoded = restored.td_data.state(0)
        self.assertEqual(encoded.worker_ids, ["A1", "A2"])
        self.assertEqual(encoded.tasks_by_worker["A1"]["O1"].payload["_adp_resource_keys"], ("item:X",))
        for seed in range(20):
            assignment = random_feasible_matching(encoded, rng=random.Random(seed)).assignment
            self.assertEqual(sum(v is not None for v in assignment.values()), 1)
        json.dumps(restored.td_data.descriptors)
        for field in dataclasses.fields(restored.td_data.pre):
            value = getattr(restored.td_data.pre, field.name)
            self.assertTrue(value is None or self.torch.is_tensor(value))
        self.assertGreater(restored.memory_bytes, restored.td_data.pre.memory_bytes)

    def test_episode_capacity_not_iteration_capacity(self):
        batches = [merge_compact_batches([episode(1)[0], episode(2)[0]]), episode(3)[0]]
        kept = retain_recent_episodes(batches, 2)
        merged = merge_compact_batches(kept)
        self.assertEqual(merged.episode_ids.tolist(), [2, 2, 3, 3])
        self.assertEqual([d["decision_index"] for d in merged.td_data.descriptors], [0, 1, 0, 1])
        self.assertEqual(merged.td_data.rewards.tolist(), [1, 2, 1, 2])

    def test_episode_sampling_keeps_current_wave_and_complete_sequences(self):
        replay = merge_compact_batches([episode(value)[0] for value in range(1, 7)])
        sampled = sample_replay_episodes(
            replay,
            required_episode_ids={5, 6},
            episode_count=4,
            rng=random.Random(7),
        )
        selected = set(sampled.episode_ids.tolist())
        self.assertEqual(4, sampled.episode_count)
        self.assertTrue({5, 6}.issubset(selected))
        for episode_id in selected:
            indices = [
                index
                for index, value in enumerate(sampled.episode_ids.tolist())
                if value == episode_id
            ]
            self.assertEqual([0, 1], [sampled.td_data.descriptors[index]["decision_index"] for index in indices])

    def test_episode_sampling_rejects_missing_or_oversized_requests(self):
        replay = merge_compact_batches([episode(1)[0], episode(2)[0]])
        with self.assertRaisesRegex(ValueError, "unavailable"):
            sample_replay_episodes(
                replay,
                required_episode_ids={3},
                episode_count=1,
                rng=random.Random(1),
            )
        with self.assertRaisesRegex(ValueError, "requests"):
            sample_replay_episodes(
                replay,
                required_episode_ids={1},
                episode_count=3,
                rng=random.Random(1),
            )

    def test_mixed_replay_rejected(self):
        batch, _ = episode()
        plain = copy.deepcopy(batch)
        plain.td_data = None
        with self.assertRaises(ValueError):
            merge_compact_batches([batch, plain])

    def test_bootstrap_terminal_zero_and_gradient_detached(self):
        batch, _ = episode()
        model = build_value_network(embedding_dim=8, heads=2, layers=1)
        plan = build_target_plan(batch, model, "cpu", n=1, beam_width=8, worker_order_strategy="cyclic")
        target = copy.deepcopy(model)
        for p in target.parameters():
            p.data.zero_()
        target.value_head[-1].bias.data.fill_(7)
        values = plan.targets([0, 1], target, "cpu")
        self.assertEqual(values.tolist(), [8, 2])
        self.assertFalse(values.requires_grad)
        self.assertTrue(all(p.grad is None for p in target.parameters()))
        self.assertEqual(plan.elapsed_min[0], 1.5)

    def test_truncated_compact_sequence_rejected(self):
        batch, _ = episode()
        partial = select_compact_samples(batch, [1])
        model = build_value_network(embedding_dim=8, heads=2, layers=1)
        with self.assertRaisesRegex(ValueError, "ordered episode"):
            build_target_plan(partial, model, "cpu", n=10, beam_width=8, worker_order_strategy="cyclic")

    def test_td_fit_ignores_mc_targets_and_freezes_target_gradients(self):
        batch = merge_compact_batches([episode(1)[0], episode(10)[0]])
        original = build_value_network(embedding_dim=8, heads=2, layers=1)
        outcomes = []
        for mc_value in (3.0, 1e9):
            samples = copy.deepcopy(batch)
            samples.targets.fill_(mc_value)
            model, target = copy.deepcopy(original), copy.deepcopy(original)
            plan = build_target_plan(samples, model, "cpu", n=10, beam_width=8, worker_order_strategy="cyclic")
            metrics = fit_td_value(model=model, target_model=target,
                optimizer=self.torch.optim.Adam(model.parameters(), lr=.001), samples=samples,
                plan=plan, device="cpu", batch_size=2, epochs=2, gradient_clip=.5, target_tau=.01, rng=random.Random(5))
            self.assertEqual(metrics["td_train_sample_count"], 2)
            self.assertEqual(metrics["td_holdout_sample_count"], 2)
            self.assertEqual(metrics["sgd_steps"], 2)
            self.assertTrue(all(p.grad is None for p in target.parameters()))
            outcomes.append(model.state_dict())
        for name in outcomes[0]:
            self.assertTrue(self.torch.equal(outcomes[0][name], outcomes[1][name]))

    def test_soft_update(self):
        online, target = self.torch.nn.Linear(1, 1), self.torch.nn.Linear(1, 1)
        for p in online.parameters():
            p.data.fill_(10)
        for p in target.parameters():
            p.data.zero_()
        soft_update(target, online, .1)
        self.assertTrue(all(float(p.item()) == 1.0 for p in target.parameters()))

    def test_mc_validation_is_actual_remaining_return(self):
        _, rows = episode()
        model = build_value_network(embedding_dim=8, heads=2, layers=1)
        for p in model.parameters():
            p.data.zero_()
        model.value_head[-1].bias.data.fill_(4)
        self.assertEqual(greedy_mc_errors(model, rows, "cpu"), (2, 5.0, 3.0, 8.0, 5.0))

    def test_default_configuration(self):
        cfg = OmegaConf.to_container(OmegaConf.load("configs/adp/mfg_flow_shop_throughput.yaml"), resolve=True)
        validate_td_config(cfg)
        self.assertEqual(cfg["algorithm"]["n_step"], 30)
        self.assertEqual(cfg["training"]["target_tau"], 0.03)
        self.assertEqual(cfg["training"]["initial_random_episodes"], 50)
        self.assertEqual(cfg["training"]["policy_iterations"], 75)
        self.assertEqual(cfg["training"]["episodes_per_iteration"], 10)
        self.assertEqual(cfg["training"]["replay_capacity_episodes"], 100)
        self.assertEqual(cfg["training"]["replay_sample_episodes_per_update"], 30)
        self.assertEqual(cfg["training"]["initial_update_epochs"], 1)
        self.assertEqual(cfg["training"]["max_epochs_per_iteration"], 2)
        self.assertEqual(cfg["validation"]["final_candidate_count"], 2)
        self.assertTrue(cfg["training"]["early_stopping"]["enabled"])
        self.assertEqual(cfg["validation"]["final_selection_seed_count"], 20)
        for path, value in (("gamma", .99), ("loss_type", "huber"), ("target_tau", 0)):
            changed = copy.deepcopy(cfg)
            changed["training"][path] = value
            with self.assertRaises(ValueError):
                validate_td_config(changed)

    def test_piecewise_training_schedules(self):
        cfg = OmegaConf.to_container(OmegaConf.load("configs/adp/mfg_flow_shop_throughput.yaml"), resolve=True)
        training = cfg["training"]
        epsilon = _piecewise_schedule_points(
            training,
            schedule_key="epsilon_schedule",
            total_iterations=75,
            fallback_start_key="epsilon_start",
            fallback_end_key="epsilon_end",
        )
        learning_rate = _piecewise_schedule_points(
            training,
            schedule_key="learning_rate_schedule",
            total_iterations=75,
            fallback_start_key="learning_rate",
        )
        self.assertAlmostEqual(_piecewise_schedule_value(epsilon, 25), 0.15)
        self.assertAlmostEqual(_piecewise_schedule_value(epsilon, 75), 0.05)
        self.assertAlmostEqual(_piecewise_schedule_value(learning_rate, 20), 0.00005)
        self.assertAlmostEqual(_piecewise_schedule_value(learning_rate, 25), 0.000035)
        self.assertAlmostEqual(_piecewise_schedule_value(learning_rate, 75), 0.00001)

        invalid = copy.deepcopy(cfg)
        invalid["training"]["epsilon_schedule"]["points"] = {0: 0.5, 74: 0.05}
        with self.assertRaises(ValueError):
            validate_td_config(invalid)

    def test_fleet_specific_td_accepts_each_paper_worker_count(self):
        cfg = OmegaConf.to_container(OmegaConf.load("configs/adp/mfg_flow_shop_throughput.yaml"), resolve=True)
        for worker_count in range(2, 7):
            candidate = copy.deepcopy(cfg)
            candidate["worker_counts"] = [worker_count]
            validate_td_config(candidate)

        invalid = copy.deepcopy(cfg)
        invalid["worker_counts"] = [2, 3]
        with self.assertRaisesRegex(ValueError, "exactly one worker count"):
            validate_td_config(invalid)

    def test_finetune_configuration_uses_unchanged_warm_start_iteration_zero(self):
        cfg = OmegaConf.to_container(
            OmegaConf.load("configs/adp/mfg_flow_shop_throughput_finetune.yaml"),
            resolve=True,
        )
        validate_td_config(cfg)
        self.assertTrue(cfg["warm_start"]["enabled"])
        self.assertEqual(cfg["algorithm"]["n_step"], 30)
        self.assertEqual(cfg["training"]["initial_random_episodes"], 50)
        self.assertEqual(cfg["training"]["initial_update_epochs"], 0)
        self.assertEqual(cfg["training"]["policy_iterations"], 20)
        self.assertEqual(cfg["training"]["episodes_per_iteration"], 10)
        self.assertEqual(cfg["training"]["replay_capacity_episodes"], 100)
        self.assertEqual(cfg["training"]["replay_sample_episodes_per_update"], 30)
        self.assertEqual(cfg["training"]["target_tau"], 0.03)
        self.assertEqual(cfg["training"]["max_epochs_per_iteration"], 2)
        self.assertEqual(cfg["training"]["epsilon_start"], 0.15)
        self.assertEqual(cfg["training"]["epsilon_end"], 0.05)
        self.assertEqual(cfg["validation"]["screening_iterations"], [0, 5, 10, 15, 20])

        invalid = copy.deepcopy(cfg)
        invalid["warm_start"]["checkpoint_path"] = ""
        with self.assertRaises(ValueError):
            validate_td_config(invalid)

        cold = copy.deepcopy(cfg)
        cold["warm_start"]["enabled"] = False
        with self.assertRaises(ValueError):
            validate_td_config(cold)

    def test_td_checkpoint_loads_through_existing_inference_api(self):
        import simpy
        from agents.factory import build_decision_module
        from manufacturing_sim.adp.checkpoint import checkpoint_fingerprint, load_checkpoint, save_checkpoint
        from manufacturing_sim.adp.train import _compose_episode_cfg, InMemoryEventLogger
        from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld
        cfg = _compose_episode_cfg(worker_count=3, seed=2026, days=1, adp_cfg={"force_random_policy": True})
        world = ManufacturingWorld(simpy.Environment(), cfg, InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"))
        model = build_value_network(embedding_dim=8, heads=2, layers=1)
        manifest = {**checkpoint_fingerprint(world, return_estimator="n_step_td"),
                    "return_estimator": "n_step_td", "supported_worker_counts": [3],
                    "model": {"embedding_dim": 8, "heads": 2, "layers": 1}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "best.pt"
            save_checkpoint(path, model=model, optimizer=None, target_model=model, manifest=manifest)
            restored, loaded_manifest = load_checkpoint(path, world=world, device="cpu")
            payload = self.torch.load(path, weights_only=False)
        self.assertIsNotNone(payload["target_model_state_dict"])
        self.assertEqual(loaded_manifest["reward_mode"], "completed_product_td")
        world.adp_coordinator.checkpoint_manifest = loaded_manifest
        self.assertEqual(world.adp_coordinator.summary()["reward_mode"], "completed_product_td")
        self.assertEqual(world.adp_coordinator.summary()["return_estimator"], "n_step_td")
        np.testing.assert_array_equal(predict_values(model, [state()], "cpu"),
                                      predict_values(restored, [state()], "cpu"))

    def test_td_driver_writes_current_contract_and_default_schedule(self):
        from unittest.mock import patch
        from manufacturing_sim.adp.train import (build_parser, EpisodeResult, RolloutBatchMetrics,
                                                _episode_metric_row, _wave_performance_row, _write_csv)
        from manufacturing_sim.adp.td_train import train_td
        cfg = OmegaConf.to_container(OmegaConf.load("configs/adp/mfg_flow_shop_n_step_td_smoke.yaml"), resolve=True)
        cfg["diagnostics"]["ood_support"]["enabled"] = False
        cfg["runtime"]["require_cuda_training"] = False
        cfg["runtime"]["device"] = "cpu"
        cfg["training"]["initial_random_episodes"] = 10
        cfg["training"]["replay_capacity_episodes"] = 10
        cfg["training"]["replay_sample_episodes_per_update"] = 4
        cfg["training"]["policy_iterations"] = 4
        cfg["training"]["early_stopping"] = {"enabled": True, "min_iterations": 1,
            "patience_iterations": 2, "consecutive_checks": 2, "paired_ci_upper_threshold": .5}
        cfg["validation"]["screening_seed_count"] = 2
        cfg["seed_partitions"]["screening_validation"] = [172026, 172028]
        cfg["validation"]["final_candidate_count"] = 2
        fingerprint = {"environment_fingerprint": "fixture", "reward_mode": "completed_product_td"}
        live_snapshots = []
        validation_updates = []

        def fake_rollout(**kw):
            if kw["jobs"][0].phase == "screening_validation":
                progress = json.loads((kw["output_dir"] / "training_progress.json").read_text())
                validation_updates.append(progress["completed_update_count"])
            results, batches = [], []
            for job in kw["jobs"]:
                compact = episode(job.episode)[0] if job.collect_compact_samples else None
                if compact is not None:
                    batches.append(compact)
                results.append(EpisodeResult(job.episode, job.phase, 3, job.seed, 5, 0, 5., 2,
                    fingerprint, iteration=job.iteration, simulation_end_min=480., termination_reason="completed_horizon",
                    wave_id=job.wave_id,
                    snapshot_hash=job.snapshot_hash,
                    greedy_mc_sample_count=2 if not job.collect_compact_samples else 0,
                    greedy_mc_squared_error_sum=8., greedy_mc_error_sum=2.))
            for start in range(0, len(results), kw["wave_size"]):
                chunk = results[start:start + kw["wave_size"]]
                kw["episode_rows"].extend(_episode_metric_row(result) for result in chunk)
                kw["wave_rows"].append(_wave_performance_row(
                    wave_jobs=kw["jobs"][start:start + kw["wave_size"]], results=chunk,
                    wave_wall_sec=1.0, configured_process_count=kw["process_count"]))
                _write_csv(kw["output_dir"] / "episode_metrics.csv", kw["episode_rows"])
                _write_csv(kw["output_dir"] / "wave_metrics.csv", kw["wave_rows"])
                kw["progress_callback"]({"event": "wave_completed", "phase": job.phase,
                    "iteration": job.iteration, "wave_completed": len(chunk),
                    "wave_episode_count": len(chunk), "completed_episode_count": len(kw["episode_rows"])})
                live_snapshots.append(json.loads((kw["output_dir"] / "training_summary.json").read_text()))
            return results, merge_compact_batches(batches), RolloutBatchMetrics(wall_sec=.01)

        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(["--output", directory, "--no-open-dashboard"])
            with patch("manufacturing_sim.adp.train._run_rollout_jobs_parallel", side_effect=fake_rollout):
                train_td(cfg, args)
            summary = json.loads((Path(directory) / "training_summary.json").read_text())
            manifest = json.loads((Path(directory) / "checkpoint_manifest.json").read_text())
            progress = json.loads((Path(directory) / "training_progress.json").read_text())
            import csv
            with (Path(directory) / "iteration_metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
                iteration_rows = list(csv.DictReader(stream))
            self.assertTrue((Path(directory) / "checkpoint_selection.csv").is_file())
            self.assertTrue((Path(directory) / "last.pt").is_file())
            from scripts.audit_adp_training import audit_training
            report = audit_training(Path(directory))
            self.assertEqual(report["errors"], [])
            # A truncated run cannot claim success just by setting early_stopped.
            summary["early_stopped"] = False
            (Path(directory) / "training_summary.json").write_text(json.dumps(summary), encoding="utf-8")
            self.assertTrue(any("no declared" in error for error in audit_training(Path(directory))["errors"]))
            summary["early_stopped"] = True
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(validation_updates, [1, 2, 3])
        self.assertGreater(live_snapshots[0]["training_rollout_sec"], 0)
        self.assertGreater(live_snapshots[-1]["validation_sec"], 0)
        self.assertEqual(summary["training_episode_count"], 14)
        self.assertEqual(summary["value_update_count"], 3)
        self.assertEqual(summary["validation_episode_count"], 8)
        self.assertEqual(progress["completed_episode_count"], 22)
        self.assertEqual(progress["expected_episode_count"], 22)
        self.assertTrue(summary["early_stopped"])
        self.assertEqual(summary["stop_reason"], "production_plateau")
        self.assertEqual(summary["completed_policy_iterations"], 2)
        self.assertEqual(summary["policy_iterations"], 4)
        self.assertEqual(summary["planned_training_episode_count"], 18)
        self.assertEqual(summary["expected_training_episode_count"], 14)
        self.assertEqual(len(manifest["seed_partitions"]["training"]), 14)
        self.assertEqual(manifest["training_run"]["completed_policy_iterations"], 2)
        self.assertEqual(progress["completed_update_count"], 3)
        self.assertEqual(progress["iteration"], 2)
        self.assertEqual(progress["status"], "completed")
        self.assertEqual(summary["best_iteration"], 0)
        self.assertEqual(len(summary["final_candidate_results"]), 2)
        self.assertEqual(sum(bool(row["selected"]) for row in summary["final_candidate_results"]), 1)
        self.assertFalse(summary["mc_targets_used_for_training"])
        self.assertEqual(manifest["training"]["n_step"], 30)
        self.assertEqual(iteration_rows[0]["update_episode_count"], "10")
        self.assertEqual(iteration_rows[0]["update_current_episode_count"], "10")
        self.assertEqual(iteration_rows[0]["update_history_episode_count"], "0")
        self.assertEqual(iteration_rows[0]["epochs"], "1")
        self.assertEqual(iteration_rows[1]["update_episode_count"], "4")
        self.assertEqual(iteration_rows[1]["update_current_episode_count"], "2")
        self.assertEqual(iteration_rows[1]["update_history_episode_count"], "2")

    def test_td_warm_start_keeps_source_checkpoint_at_iteration_zero(self):
        from unittest.mock import patch
        from manufacturing_sim.adp.checkpoint import save_checkpoint
        from manufacturing_sim.adp.schema import FEATURE_SCHEMA_VERSION
        from manufacturing_sim.adp.train import build_parser, EpisodeResult, RolloutBatchMetrics
        from manufacturing_sim.adp.td_train import train_td

        cfg = OmegaConf.to_container(
            OmegaConf.load("configs/adp/mfg_flow_shop_n_step_td_smoke.yaml"),
            resolve=True,
        )
        cfg["diagnostics"]["ood_support"]["enabled"] = False
        cfg["runtime"]["require_cuda_training"] = False
        cfg["runtime"]["device"] = "cpu"
        cfg["training"].update(
            {
                "initial_random_episodes": 2,
                "initial_update_epochs": 0,
                "policy_iterations": 1,
                "episodes_per_iteration": 2,
                "replay_capacity_episodes": 4,
                "replay_sample_episodes_per_update": 4,
                "max_epochs_per_iteration": 1,
            }
        )
        cfg["validation"]["screening_iterations"] = [0, 1]
        fingerprint = {"environment_fingerprint": "fixture", "reward_mode": "completed_product_td"}
        observed_phases: set[str] = set()

        def fake_rollout(**kw):
            results, batches = [], []
            for job in kw["jobs"]:
                observed_phases.add(job.phase)
                compact = episode(job.episode)[0] if job.collect_compact_samples else None
                if compact is not None:
                    batches.append(compact)
                results.append(EpisodeResult(job.episode, job.phase, 3, job.seed, 5, 0, 5., 2,
                    fingerprint, iteration=job.iteration, simulation_end_min=480., termination_reason="completed_horizon",
                    greedy_mc_sample_count=2 if not job.collect_compact_samples else 0,
                    greedy_mc_squared_error_sum=8., greedy_mc_error_sum=2.))
            return results, merge_compact_batches(batches), RolloutBatchMetrics(wall_sec=.01)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pt"
            output = root / "output"
            model_cfg = cfg["model"]
            source_model = build_value_network(
                embedding_dim=int(model_cfg["embedding_dim"]),
                heads=int(model_cfg["heads"]),
                layers=int(model_cfg["layers"]),
            )
            source_optimizer = self.torch.optim.Adam(source_model.parameters(), lr=5e-5)
            source_manifest = {
                "scenario_type": "mfg_flow_shop",
                "objective_mode": "maximize_throughput",
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "return_estimator": "n_step_td",
                "reward_mode": "completed_product_td",
                "loss_type": "mse",
                "wait_action_enabled": False,
                "worker_order_strategy": "cyclic",
                "horizon_days": int(cfg["horizon_days"]),
                "supported_worker_counts": [3],
                "environment_fingerprints_by_worker_count": {"3": "fixture"},
                "model": dict(model_cfg),
                "checkpoint_id": "SOURCE-I50",
                "iteration": 50,
            }
            save_checkpoint(
                source,
                model=source_model,
                optimizer=source_optimizer,
                target_model=source_model,
                manifest=source_manifest,
            )
            cfg["warm_start"] = {
                "enabled": True,
                "checkpoint_path": str(source),
                "replay_fill_epsilon": 0.1,
            }
            args = build_parser().parse_args(["--output", str(output), "--no-open-dashboard"])
            with patch("manufacturing_sim.adp.train._run_rollout_jobs_parallel", side_effect=fake_rollout):
                train_td(cfg, args)
            summary = json.loads((output / "training_summary.json").read_text())
            with (output / "iteration_metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
                iteration_rows = list(csv.DictReader(stream))

        self.assertTrue(summary["warm_start_enabled"])
        self.assertEqual(summary["warm_start_checkpoint_id"], "SOURCE-I50")
        self.assertEqual(summary["value_update_count"], 1)
        self.assertEqual(iteration_rows[0]["epochs"], "0")
        self.assertEqual(iteration_rows[0]["sgd_steps"], "0")
        self.assertEqual(iteration_rows[1]["update_episode_count"], "4")
        self.assertIn("warm_start_replay", observed_phases)

    def test_dashboard_missing_validation_is_not_zero(self):
        rows = [{"iteration": 0, "rollout_products_mean": 10, "validation_products_mean": 12,
                 "validation_products_std": 2, "validation_episode_count": 10, "td_train_mse": 4,
                 "td_holdout_mse": None, "greedy_mc_rmse": 3, "greedy_mc_bias": -1,
                 "greedy_mc_prediction_mean": 8, "greedy_mc_target_mean": 9},
                {"iteration": 1, "rollout_products_mean": 11, "td_train_mse": 2}]
        with tempfile.TemporaryDirectory() as directory:
            path = render_td_dashboard(Path(directory), [], rows, [], {"n_step": 10})
            text = path.read_text(encoding="utf-8")
        self.assertIn("n-step TD 적합 오차", text)
        self.assertIn("실제 미래 생산량 예측 오차", text)
        self.assertIn("미래 생산량 가치 보정", text)
        self.assertIn("N/A", text)
        self.assertNotIn("nan", text.lower())
        self.assertNotIn("infinity", text.lower())
        self.assertIn("Iteration (0 = 초기 학습)", text)
        self.assertIn("Replay 보유량과 업데이트 사용량", text)

    def test_warm_start_dashboard_reports_actual_fill_not_replay_capacity(self):
        rows = [{"iteration": 0, "replay_episode_count": 50}]
        summary = {
            "n_step": 30,
            "warm_start_enabled": True,
            "replay_capacity_episodes": 100,
            "replay_sample_episodes_per_update": 30,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = render_td_dashboard(Path(directory), [], rows, [], summary)
            text = path.read_text(encoding="utf-8")
        self.assertIn("Warm-start fill / policy update episode", text)
        self.assertIn("<strong>50 / 30</strong>", text)
        self.assertNotIn("<strong>100 / 30</strong>", text)


if __name__ == "__main__":
    unittest.main()
