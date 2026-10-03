from __future__ import annotations

import random
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np
import simpy
from omegaconf import OmegaConf

from agents.factory import build_decision_module
from agents.modes import format_decision_mode_label, normalize_decision_mode
from manufacturing_sim.adp.checkpoint import checkpoint_fingerprint, load_checkpoint, save_checkpoint
from manufacturing_sim.adp.compact import (
    CompactPairwiseMCBatch,
    compact_episode_transitions,
    merge_compact_batches,
    select_compact_samples,
    stratified_episode_split,
)
from manufacturing_sim.adp.encoding import (
    ADPStateEncoder,
    _destination_capacity_features,
    _expected_terminal_closure_minutes,
    _task_releases_blockage,
)
from manufacturing_sim.adp.finalize_interrupted_training import (
    _aggregate_wave_metrics,
    _reconstruct_iteration_rows,
)
from manufacturing_sim.adp.model import build_value_network, collate_states, predict_values, require_torch
from manufacturing_sim.adp.ood_diagnostics import (
    build_ood_support_bank,
    evaluate_ood_selected_actions,
)
from manufacturing_sim.adp.policy import (
    greedy_beam_matching,
    normalized_beam_value_entropy,
    probe_feasible_matchings,
    random_feasible_matching,
)
from manufacturing_sim.adp.schema import (
    GLOBAL_FEATURE_DIM,
    PAIR_FEATURE_DIM,
    TASK_FEATURE_DIM,
    WORKER_FEATURE_DIM,
    EncodedDecisionState,
)
from manufacturing_sim.adp.train import (
    EpisodeResult,
    InMemoryEventLogger,
    RolloutJob,
    _balanced_worker_schedule,
    _weighted_mc_regression_loss,
    build_short_replay_batch,
    build_parser,
    _compose_episode_cfg,
    _configure_training_determinism,
    _mc_regression_loss,
    _pairwise_mc_advantage_loss,
    _payload_hash,
    _resolve_screening_iterations,
    _should_validate,
    _should_accept_candidate,
    _snapshot_state_dict,
    _resolve_training_device,
    _svg_chart,
    _validate_seed_partitions,
    _wave_chunks,
    _wave_performance_row,
    monte_carlo_samples,
    render_training_dashboard,
)
from manufacturing_sim.simulation.scenarios.manufacturing.entities import MachineState, Task
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


def _state() -> EncodedDecisionState:
    task_a = Task("T1", "TRANSFER", "inter_station_transfer", 1.0, "Station2", payload={"_adp_resource_keys": ["item:X"]})
    task_b = Task("T2", "LOAD_MACHINE", "load_machine", 1.0, "S2M1", payload={"_adp_resource_keys": ["item:X", "machine:S2M1"]})
    return EncodedDecisionState(
        global_features=np.zeros(GLOBAL_FEATURE_DIM, dtype=np.float32),
        worker_features=np.zeros((2, WORKER_FEATURE_DIM), dtype=np.float32),
        task_features=np.zeros((2, TASK_FEATURE_DIM), dtype=np.float32),
        pair_features=np.zeros((2, 2, PAIR_FEATURE_DIM), dtype=np.float32),
        feasibility=np.ones((2, 2), dtype=bool),
        worker_ids=["A1", "A2"],
        opportunity_ids=["O1", "O2"],
        tasks_by_worker={"A1": {"O1": task_a, "O2": task_b}, "A2": {"O1": task_a, "O2": task_b}},
        time_min=120.0,
        horizon_min=1200.0,
        review_interval_min=1.0,
    )


class SimulationBasedADPTests(unittest.TestCase):
    def test_pairwise_mc_advantage_loss_uses_same_state_action_difference(self) -> None:
        torch = require_torch()
        state = _state()
        selected_state = state.post_decision({"A1": "O1", "A2": "O2"})
        alternative_state = state.post_decision({"A1": "O2", "A2": "O1"})
        selected = compact_episode_transitions(
            [{"post_state": selected_state, "raw_reward": 5.0}],
            episode_id=7,
            worker_count=2,
        )
        alternative = compact_episode_transitions(
            [{"post_state": alternative_state, "raw_reward": 2.0}],
            episode_id=7,
            worker_count=2,
        )
        pairs = CompactPairwiseMCBatch(selected=selected, alternative=alternative)

        class AssignmentValue(torch.nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.weight = torch.nn.Parameter(torch.tensor(1.0))

            def forward(self, **inputs):
                mask = inputs["selected_assignment_mask"].to(torch.float32)
                return self.weight * (mask[:, 0, 0] - mask[:, 0, 1])

        loss = _pairwise_mc_advantage_loss(
            AssignmentValue(), pairs, [0], "cpu"
        )
        # Predicted advantage is 2, while the paired MC advantage is 3.
        self.assertAlmostEqual(float(loss.item()), 1.0)
        self.assertEqual(int(pairs.selected.episode_ids[0]), 7)
        self.assertEqual(int(pairs.alternative.episode_ids[0]), 7)

    def test_select_compact_samples_preserves_pair_alignment(self) -> None:
        first = compact_episode_transitions(
            [{"post_state": _state(), "raw_reward": 1.0}],
            episode_id=1,
            worker_count=2,
        )
        second = compact_episode_transitions(
            [{"post_state": _state(), "raw_reward": 2.0}],
            episode_id=2,
            worker_count=2,
        )
        selected = select_compact_samples(
            merge_compact_batches([first, second]), [1]
        )
        self.assertEqual(selected.episode_ids.tolist(), [2])
        self.assertEqual(selected.targets.tolist(), [2.0])

    def test_training_determinism_reseeds_torch(self) -> None:
        torch = require_torch()
        first_meta = _configure_training_determinism(torch, 2026)
        first = torch.rand(8)
        second_meta = _configure_training_determinism(torch, 2026)
        second = torch.rand(8)

        self.assertTrue(torch.equal(first, second))
        self.assertEqual(first_meta, second_meta)
        self.assertTrue(first_meta["torch_deterministic_algorithms"])

    def test_encoder_represents_parallel_machines_in_both_station_aggregates(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True},
        )
        world = ManufacturingWorld(
            simpy.Environment(),
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        world.machines["S1M1"].state = MachineState.PROCESSING
        world.machines["S1M1"].cycle_remaining_process_min = 4.0
        world.machines["S2M2"].state = MachineState.PROCESSING
        world.machines["S2M2"].cycle_remaining_process_min = 6.0

        encoded = ADPStateEncoder().encode(world, [], {})

        self.assertAlmostEqual(0.5, float(encoded.global_features[11]))
        self.assertAlmostEqual(0.5, float(encoded.global_features[17]))
        self.assertGreater(float(encoded.global_features[20]), 0.0)
        self.assertAlmostEqual(0.5, float(encoded.global_features[26]))
        self.assertGreater(float(encoded.global_features[29]), 0.0)

    def test_encoder_represents_buffer_occupancy_reservations_and_free_capacity(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True},
        )
        world = ManufacturingWorld(
            simpy.Environment(),
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        world.material_queues[1].append("material-fixture")
        world.buffer_slot_reservations["material_queue_1"]["task-fixture"] = {
            "task_id": "task-fixture",
            "slot_token": 1,
        }

        encoded = ADPStateEncoder().encode(world, [], {})

        self.assertAlmostEqual(0.25, float(encoded.global_features[12]))
        self.assertAlmostEqual(0.25, float(encoded.global_features[13]))
        self.assertAlmostEqual(0.50, 1.0 - float(encoded.global_features[12:14].sum()))

    def test_action_effect_features_capture_blockage_and_destination_capacity(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True},
        )
        world = ManufacturingWorld(
            simpy.Environment(),
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        machine = world.machines["S1M1"]
        machine.state = MachineState.DONE_WAIT_UNLOAD
        machine.output_intermediate = "intermediate-fixture"
        unload = Task(
            "U1",
            "UNLOAD_MACHINE",
            "unload_machine",
            1.0,
            "S1M1",
            payload={"machine_id": "S1M1"},
        )
        replenish = Task(
            "R1",
            "TRANSFER",
            "material_supply",
            1.0,
            "Station1",
            payload={"transfer_kind": "material_supply", "station": 1},
        )

        self.assertEqual(_task_releases_blockage(world, unload), 1.0)
        self.assertEqual(_destination_capacity_features(world, replenish), (1.0, 1.0))

    def test_terminal_closure_feature_orders_stage_and_responds_to_congestion(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True},
        )
        world = ManufacturingWorld(
            simpy.Environment(),
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        upstream = Task(
            "R1",
            "REPLENISH_MATERIAL",
            "material_supply",
            1.0,
            "Station1",
            payload={"transfer_kind": "material_supply", "station": 1},
            task_code="REPLENISH_MATERIAL",
        )
        terminal = Task(
            "T1",
            "TRANSFER",
            "accepted_product_transfer",
            1.0,
            "CompletedProducts",
            payload={"transfer_kind": "inter_station", "from_station": 4},
            task_code="TRANSFER",
        )

        uncongested = _expected_terminal_closure_minutes(world, upstream)
        self.assertIsNotNone(uncongested)
        self.assertGreater(float(uncongested), float(_expected_terminal_closure_minutes(world, terminal)))

        world.intermediate_queues[world.inspection_queue_station].extend(["P1", "P2"])
        congested = _expected_terminal_closure_minutes(world, upstream)
        self.assertGreater(float(congested), float(uncongested))

        worker = world.workers["A1"]
        encoded = ADPStateEncoder().encode(
            world,
            [worker],
            {worker.agent_id: {"upstream": upstream, "terminal": terminal}},
        )
        self.assertEqual(21, encoded.task_features.shape[1])
        upstream_index = encoded.opportunity_ids.index("upstream")
        terminal_index = encoded.opportunity_ids.index("terminal")
        self.assertGreater(
            float(encoded.task_features[upstream_index, 20]),
            float(encoded.task_features[terminal_index, 20]),
        )

    def test_environment_fingerprint_changes_with_buffer_capacity(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True},
        )
        first = ManufacturingWorld(
            simpy.Environment(),
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        changed_cfg = OmegaConf.to_container(OmegaConf.create(cfg), resolve=True)
        changed_cfg["factory"]["buffers"]["station1"]["output_capacity"] = 3
        second = ManufacturingWorld(
            simpy.Environment(),
            changed_cfg,
            InMemoryEventLogger(),
            build_decision_module(
                experiment_cfg=changed_cfg,
                decision_mode="simulation_based_adp",
            ),
        )

        self.assertNotEqual(
            checkpoint_fingerprint(first)["environment_fingerprint"],
            checkpoint_fingerprint(second)["environment_fingerprint"],
        )
        current = checkpoint_fingerprint(first)["environment_fingerprint"]
        with patch("manufacturing_sim.adp.checkpoint.MACHINE_LIFECYCLE_CONTRACT", "legacy_pm"):
            self.assertNotEqual(current, checkpoint_fingerprint(first)["environment_fingerprint"])

    def test_environment_fingerprint_includes_reliability_and_recovery_contract(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True},
        )
        first = ManufacturingWorld(
            simpy.Environment(),
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        changed_cfg = OmegaConf.to_container(OmegaConf.create(cfg), resolve=True)
        changed_cfg["machine_failure"]["preventive_maintenance"]["hazard_multiplier"] = 0.75
        second = ManufacturingWorld(
            simpy.Environment(),
            changed_cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=changed_cfg, decision_mode="simulation_based_adp"),
        )

        first_fingerprint = checkpoint_fingerprint(first)
        second_fingerprint = checkpoint_fingerprint(second)
        self.assertEqual("mfg_flow_shop_adp_v10", first_fingerprint["feature_schema_version"])
        self.assertNotEqual(
            first_fingerprint["environment_fingerprint"],
            second_fingerprint["environment_fingerprint"],
        )

    def test_checkpoint_fingerprint_accepts_wait_contract_before_coordinator_assignment(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True},
        )
        world = ManufacturingWorld(
            simpy.Environment(),
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        world.adp_coordinator = None

        fingerprint = checkpoint_fingerprint(world, wait_action_enabled=True)

        self.assertTrue(fingerprint["wait_action_enabled"])
        self.assertEqual(fingerprint["random_policy"], "uniform_random_feasible_with_wait")

    def test_single_point_chart_renders_visible_marker_and_value(self) -> None:
        chart = _svg_chart(
            [("Validation mean", [30.45], "#d78cff")],
            x_values=[15.0],
            x_label="Checkpoint policy update",
            y_label="Completed products",
            include_zero=True,
        )
        self.assertIn("<circle", chart)
        self.assertIn(">30.4</text>", chart)

    def test_mode_is_registered(self) -> None:
        self.assertEqual(normalize_decision_mode("simulation_based_adp"), "simulation_based_adp")
        self.assertEqual(format_decision_mode_label("simulation_based_adp"), "Simulation-Based ADP")
        self.assertEqual(normalize_decision_mode("random_feasible_dispatch"), "random_feasible_dispatch")
        self.assertEqual(format_decision_mode_label("random_feasible_dispatch"), "Random Feasible Dispatch")

    def test_random_feasible_mode_does_not_require_checkpoint(self) -> None:
        cfg = _compose_episode_cfg(worker_count=3, seed=2026, days=1, adp_cfg={"force_random_policy": True})
        cfg["decision"]["mode"] = "random_feasible_dispatch"
        cfg["decision"]["adp"]["checkpoint_path"] = ""
        cfg["decision"]["adp"]["training"] = False
        cfg["decision"]["adp"]["force_random_policy"] = True
        env = simpy.Environment()
        world = ManufacturingWorld(
            env,
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="random_feasible_dispatch"),
        )
        self.assertTrue(world._adp_active())
        self.assertTrue(world.adp_coordinator.force_random_policy)
        self.assertFalse(world.adp_coordinator.allow_wait_action)
        self.assertIsNone(world.adp_coordinator.model)
        fingerprint = checkpoint_fingerprint(world)
        self.assertFalse(fingerprint["wait_action_enabled"])
        self.assertEqual(fingerprint["random_policy"], "uniform_random_feasible_no_wait")
        self.assertEqual(fingerprint["worker_order_strategy"], "cyclic")

    def test_mfg_flow_shop_artifact_audit_supports_random_baseline_mode(self) -> None:
        audit_source = Path("scripts/audit_run_artifacts.py").read_text(encoding="utf-8")
        self.assertIn('"random_feasible_dispatch",', audit_source)

    def test_random_matching_is_reproducible_and_respects_resource_conflicts(self) -> None:
        first = random_feasible_matching(_state(), rng=random.Random(2026))
        second = random_feasible_matching(_state(), rng=random.Random(2026))
        self.assertEqual(first.assignment, second.assignment)
        assigned = [value for value in first.assignment.values() if value]
        self.assertEqual(len(assigned), 1, "no-WAIT random matching should make the maximal feasible assignment")
        self.assertEqual(first.policy, "uniform_random_feasible_no_wait")

    def test_normalized_beam_value_entropy_is_bounded_and_scale_invariant(self) -> None:
        uniform = normalized_beam_value_entropy([2.0, 2.0, 2.0])
        concentrated = normalized_beam_value_entropy([0.0, 0.0, 20.0])
        shifted = normalized_beam_value_entropy([100.0, 100.0, 120.0])

        self.assertEqual(normalized_beam_value_entropy([1.0]), None)
        self.assertAlmostEqual(float(uniform), 1.0)
        self.assertGreaterEqual(float(concentrated), 0.0)
        self.assertLess(float(concentrated), 0.8)
        self.assertAlmostEqual(float(concentrated), float(shifted), places=7)

    def test_random_feasible_excludes_wait_by_default(self) -> None:
        state = _state()
        state.decision_worker_ids = ["A1"]
        state.tasks_by_worker = {"A1": {"O1": state.tasks_by_worker["A1"]["O1"]}}
        choices = {
            random_feasible_matching(state, rng=random.Random(seed)).assignment["A1"]
            for seed in range(50)
        }
        self.assertEqual(choices, {"O1"})

    def test_random_feasible_can_reenable_wait_for_ablation(self) -> None:
        state = _state()
        state.decision_worker_ids = ["A1"]
        state.tasks_by_worker = {"A1": {"O1": state.tasks_by_worker["A1"]["O1"]}}
        choices = {
            random_feasible_matching(
                state,
                rng=random.Random(seed),
                allow_wait_action=True,
            ).assignment["A1"]
            for seed in range(50)
        }
        self.assertEqual(choices, {None, "O1"})

    def test_fixed_probe_candidates_include_wait_and_respect_conflicts(self) -> None:
        state = _state()
        candidates = probe_feasible_matchings(
            state,
            rng=random.Random(2026),
            candidate_limit=6,
            allow_wait_action=True,
        )
        self.assertGreaterEqual(len(candidates), 2)
        self.assertIn({"A1": None, "A2": None}, candidates)
        for assignment in candidates:
            assigned = [value for value in assignment.values() if value]
            self.assertLessEqual(len(assigned), 1)

    def test_greedy_adp_excludes_wait_when_a_task_is_feasible(self) -> None:
        import torch

        state = _state()
        state.decision_worker_ids = ["A1"]
        state.tasks_by_worker = {"A1": {"O1": state.tasks_by_worker["A1"]["O1"]}}
        state.opportunity_ids = ["O1"]
        state.task_features = state.task_features[:1]
        state.pair_features = state.pair_features[:, :1]
        state.feasibility = state.feasibility[:, :1]
        state.selected_assignment_mask = state.selected_assignment_mask[:, :1]
        model = build_value_network(embedding_dim=32, heads=4, layers=1)
        selection = greedy_beam_matching(
            state,
            model=model,
            device=torch.device("cpu"),
            beam_width=8,
            allow_wait_action=False,
        )
        self.assertEqual(selection.assignment["A1"], "O1")

    def test_beam_worker_order_rotates_across_decisions_and_idle_subsets(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True, "worker_order_strategy": "cyclic"},
        )
        world = ManufacturingWorld(
            simpy.Environment(),
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        state = _state()
        state.worker_ids = ["A1", "A2", "A3"]
        state.decision_worker_ids = ["A1", "A2", "A3"]
        expected = [
            ["A1", "A2", "A3"],
            ["A2", "A3", "A1"],
            ["A3", "A1", "A2"],
        ]
        for decision_count, order in enumerate(expected):
            world.adp_coordinator.metrics["decision_count"] = decision_count
            self.assertEqual(world.adp_coordinator._beam_worker_order(state), order)

        state.decision_worker_ids = ["A1", "A3"]
        world.adp_coordinator.metrics["decision_count"] = 1
        self.assertEqual(world.adp_coordinator._beam_worker_order(state), ["A3", "A1"])

    def test_mc_return_uses_complete_episode_reverse_sum(self) -> None:
        state = _state()
        transitions = [
            {"post_state": state, "raw_reward": 0.0},
            {"post_state": state, "raw_reward": 1.0},
            {"post_state": state, "raw_reward": 2.0},
        ]
        samples = monte_carlo_samples(transitions, gamma=1.0)
        self.assertEqual([round(target, 6) for _, target in samples], [3.0, 3.0, 2.0])

    def test_mc_loss_is_mse(self) -> None:
        import torch

        predictions = torch.tensor([0.0, 0.0])
        targets = torch.tensor([1.0, 3.0])
        self.assertAlmostEqual(float(_mc_regression_loss(predictions, targets)), 5.0)

    def test_weighted_mc_loss_upweights_latest_wave(self) -> None:
        import torch

        predictions = torch.tensor([0.0, 0.0])
        targets = torch.tensor([1.0, 3.0])
        weights = torch.tensor([1.0, 2.0])
        self.assertAlmostEqual(
            float(_weighted_mc_regression_loss(predictions, targets, weights)),
            19.0 / 3.0,
            places=6,
        )

    def test_short_replay_is_bounded_and_weights_only_latest_batch(self) -> None:
        batches = []
        for iteration in range(6):
            batch = compact_episode_transitions(
                [{"post_state": _state(), "raw_reward": float(iteration)}],
                episode_id=iteration,
                worker_count=3,
            )
            batches.append((iteration, batch))

        replay, weights, metrics = build_short_replay_batch(
            batches,
            window_iterations=5,
            latest_iteration_weight=2.0,
        )
        self.assertEqual(replay.episode_ids.tolist(), [1, 2, 3, 4, 5])
        self.assertEqual(weights.tolist(), [1.0, 1.0, 1.0, 1.0, 2.0])
        self.assertEqual(metrics["replay_iteration_count"], 5)
        self.assertEqual(metrics["replay_oldest_iteration"], 1)
        self.assertEqual(metrics["replay_newest_iteration"], 5)
        self.assertEqual(metrics["replay_episode_count"], 5)
        self.assertEqual(metrics["replay_sample_count"], 5)
        self.assertEqual(metrics["replay_effective_sample_count"], 6.0)

    def test_recovered_iteration_rows_reconstruct_short_replay_window(self) -> None:
        cfg = {
            "worker_counts": [3],
            "training": {
                "policy_iterations": 2,
                "replay_scope": "recent_window",
                "replay_window_iterations": 2,
                "latest_iteration_weight": 2.0,
                "epsilon_start": 0.5,
                "epsilon_end": 0.1,
            },
        }
        episodes = [
            {
                "iteration": iteration,
                "phase": "initial_random" if iteration == 0 else f"policy_iteration_{iteration}",
                "compact_sample_count": 10 * (iteration + 1),
                "compact_memory_mib": 0.1 * (iteration + 1),
            }
            for iteration in range(3)
        ]
        rows = _reconstruct_iteration_rows(cfg=cfg, episode_rows=episodes, wave_rows=[])
        self.assertEqual(rows[2]["replay_oldest_iteration"], 1)
        self.assertEqual(rows[2]["replay_newest_iteration"], 2)
        self.assertEqual(rows[2]["replay_episode_count"], 2)
        self.assertEqual(rows[2]["replay_sample_count"], 50)
        self.assertEqual(rows[2]["replay_effective_sample_count"], 80.0)
        self.assertAlmostEqual(rows[2]["replay_buffer_mib"], 0.5)

    def test_post_decision_marks_selected_edge_without_changing_pair_features(self) -> None:
        state = _state()
        state.pair_features[0, 0] = np.asarray(
            [0.1, 0.2, 0.3, 0.0, 0.8, 1.0, 1.0, 0.5],
            dtype=np.float32,
        )
        post = state.post_decision({"A1": "O1", "A2": None})
        self.assertTrue(post.selected_assignment_mask[0, 0])
        self.assertEqual(int(post.selected_assignment_mask.sum()), 1)
        self.assertTrue(np.array_equal(post.pair_features, state.pair_features))
        self.assertEqual(post.time_min, state.time_min)

    def test_all_wait_afterstate_advances_only_the_encoded_review_clock(self) -> None:
        state = _state()
        state.wait_action_enabled = True
        state.global_features[0] = 0.9
        post = state.post_decision({"A1": None, "A2": None})
        self.assertAlmostEqual(post.time_min, 121.0)
        self.assertAlmostEqual(float(post.global_features[0]), 1079.0 / 1200.0, places=6)
        self.assertEqual(int(post.selected_assignment_mask.sum()), 0)

        partial = state.post_decision({"A1": None})
        mixed = state.post_decision({"A1": "O1", "A2": None})
        self.assertEqual(partial.time_min, state.time_min)
        self.assertEqual(mixed.time_min, state.time_min)

        state.wait_action_enabled = False
        disabled = state.post_decision({"A1": None, "A2": None})
        self.assertEqual(disabled.time_min, state.time_min)

    def test_removed_potential_and_random_wait_settings_are_rejected(self) -> None:
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True, "potential_shaping": {"enabled": True}},
        )
        with self.assertRaisesRegex(ValueError, "potential_shaping was removed"):
            ManufacturingWorld(
                simpy.Environment(),
                cfg,
                InMemoryEventLogger(),
                build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
            )
        cfg = _compose_episode_cfg(
            worker_count=3,
            seed=2026,
            days=1,
            adp_cfg={"force_random_policy": True, "random_wait_probability": 0.1},
        )
        with self.assertRaisesRegex(ValueError, "random_wait_probability was removed"):
            ManufacturingWorld(
                simpy.Environment(),
                cfg,
                InMemoryEventLogger(),
                build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
            )

    def test_attention_value_network_accepts_variable_worker_task_counts(self) -> None:
        import torch

        model = build_value_network(embedding_dim=32, heads=4, layers=1)
        state = _state()
        shorter = EncodedDecisionState(
            global_features=state.global_features,
            worker_features=state.worker_features[:1],
            task_features=state.task_features[:1],
            pair_features=state.pair_features[:1, :1],
            feasibility=state.feasibility[:1, :1],
            worker_ids=["A1"],
            opportunity_ids=["O1"],
        )
        batch = collate_states([state, shorter], torch.device("cpu"))
        values = model(**batch)
        self.assertEqual(tuple(values.shape), (2,))
        self.assertTrue(torch.isfinite(values).all())

    def test_compact_episode_matches_regular_collation_without_runtime_references(self) -> None:
        import torch

        first = _state()
        second = EncodedDecisionState(
            global_features=np.ones(GLOBAL_FEATURE_DIM, dtype=np.float32),
            worker_features=first.worker_features[:1].copy(),
            task_features=np.zeros((0, TASK_FEATURE_DIM), dtype=np.float32),
            pair_features=np.zeros((1, 0, PAIR_FEATURE_DIM), dtype=np.float32),
            feasibility=np.zeros((1, 0), dtype=bool),
            worker_ids=["A1"],
            opportunity_ids=[],
        )
        transitions = [
            {"post_state": first, "raw_reward": 0.0, "value_policy_selected": True},
            {"post_state": second, "raw_reward": 1.0, "value_policy_selected": False},
        ]
        compact = compact_episode_transitions(transitions, episode_id=7, worker_count=3)
        regular = collate_states([first, second], torch.device("cpu"))
        compact_inputs, targets = compact.model_batch([0, 1], torch.device("cpu"))
        for key in regular:
            self.assertTrue(torch.equal(regular[key], compact_inputs[key]), key)
        self.assertTrue(torch.allclose(targets, torch.tensor([1.0, 1.0])))
        self.assertTrue(torch.equal(compact.value_policy_selected, torch.tensor([True, False])))
        self.assertTrue(torch.equal(regular["selected_assignment_mask"], compact_inputs["selected_assignment_mask"]))
        self.assertFalse(hasattr(compact, "tasks_by_worker"))
        self.assertNotIn(Task, {type(value) for value in vars(compact).values()})

    def test_ood_diagnostics_detect_shifted_greedy_actions_and_excess(self) -> None:
        import torch

        def compact_sample(value: float, target: float, episode_id: int):
            state = _state()
            state.global_features[0] = value
            post_state = state.post_decision({"A1": "O1", "A2": None})
            return compact_episode_transitions(
                [
                    {
                        "post_state": post_state,
                        "raw_reward": target,
                        "value_policy_selected": True,
                    }
                ],
                episode_id=episode_id,
                worker_count=3,
            )

        support_batch = merge_compact_batches(
            [compact_sample(index / 100.0, 0.0, index) for index in range(8)]
        )
        support = build_ood_support_bank(
            support_batch,
            device=torch.device("cpu"),
            seed=2026,
            reference_samples=4,
            calibration_samples=4,
            quantile=0.95,
        )
        self.assertIsNotNone(support)

        query_batch = merge_compact_batches(
            [
                compact_sample(0.02, 0.2, 20),
                compact_sample(0.03, 0.3, 21),
                compact_sample(1.00, 0.0, 22),
                compact_sample(1.10, 0.0, 23),
            ]
        )

        class GlobalFeatureValue(torch.nn.Module):
            def forward(self, global_features, **_kwargs):
                return global_features[:, 0] * 10.0

        metrics = evaluate_ood_selected_actions(
            GlobalFeatureValue(),
            query_batch,
            support,
            device=torch.device("cpu"),
            batch_size=4,
            seed=2027,
            evaluation_samples=4,
        )
        self.assertTrue(metrics["available"])
        self.assertEqual(metrics["evaluated_selection_count"], 4)
        self.assertEqual(metrics["ood_selection_count"], 2)
        self.assertAlmostEqual(metrics["ood_selection_rate"], 0.5)
        self.assertGreater(metrics["ood_overestimation_excess"], 5.0)

    def test_compact_batches_split_by_episode_and_not_individual_transition(self) -> None:
        first = compact_episode_transitions(
            [{"post_state": _state(), "raw_reward": 1.0}] * 2,
            episode_id=1,
            worker_count=3,
        )
        second = compact_episode_transitions(
            [{"post_state": _state(), "raw_reward": 1.0}] * 3,
            episode_id=2,
            worker_count=3,
        )
        merged = merge_compact_batches([first, second])
        train_indices, validation_indices = stratified_episode_split(
            merged, validation_fraction=0.5, rng=random.Random(2026)
        )
        train_episodes = {int(merged.episode_ids[index]) for index in train_indices}
        validation_episodes = {int(merged.episode_ids[index]) for index in validation_indices}
        self.assertFalse(train_episodes & validation_episodes)
        self.assertEqual(len(train_episodes | validation_episodes), 2)

    def test_value_prediction_does_not_update_frozen_policy_parameters(self) -> None:
        import torch

        model = build_value_network(embedding_dim=32, heads=4, layers=1)
        before = {name: value.detach().clone() for name, value in model.state_dict().items()}
        predict_values(model, [_state()], torch.device("cpu"))
        for name, value in model.state_dict().items():
            self.assertTrue(torch.equal(before[name], value), name)

    def test_same_timestamp_idle_workers_are_jointly_observed(self) -> None:
        cfg = _compose_episode_cfg(worker_count=3, seed=2026, days=1, adp_cfg={"force_random_policy": True})
        env = simpy.Environment()
        logger = InMemoryEventLogger()
        module = build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp")
        world = ManufacturingWorld(env, cfg, logger, module)
        world.bootstrap()
        env.run(until=0.001)
        decisions = [event for event in logger.events if event["type"] == "ADP_JOINT_DECISION"]
        self.assertTrue(decisions)
        self.assertEqual(decisions[0]["details"]["idle_workers"], ["A1", "A2", "A3"])
        self.assertFalse(decisions[0]["details"]["wait_action_enabled"])
        self.assertEqual(decisions[0]["details"]["worker_order_strategy"], "cyclic")
        metrics = world.adp_coordinator.metrics
        summary = world.adp_coordinator.summary()
        self.assertEqual(
            metrics["wait_count"],
            metrics["candidate_available_wait_count"]
            + metrics["no_candidate_unassigned_count"],
        )
        self.assertAlmostEqual(
            summary["worker_wait_ratio"],
            summary["candidate_available_wait_ratio"]
            + summary["no_candidate_unassigned_ratio"],
        )
        self.assertEqual(
            metrics["joint_all_wait_count"],
            metrics["joint_all_wait_with_candidate_count"]
            + metrics["joint_no_candidate_count"],
        )

    def test_standard_training_config_uses_worker3_n_step_td(self) -> None:
        cfg = OmegaConf.to_container(
            OmegaConf.load(Path("configs/adp/mfg_flow_shop_throughput.yaml")), resolve=True
        )
        cli_default_cfg = OmegaConf.to_container(
            OmegaConf.load(build_parser().parse_args([]).config), resolve=True
        )
        for section in ("worker_counts", "training", "validation", "model", "rollout"):
            self.assertEqual(cfg[section], cli_default_cfg[section])
        training = cfg["training"]
        total = int(training["initial_random_episodes"]) + (
            int(training["policy_iterations"]) * int(training["episodes_per_iteration"])
        )
        self.assertEqual(total, 800)
        self.assertEqual(cfg["worker_counts"], [3])
        self.assertEqual(training["initial_random_episodes"], 50)
        self.assertEqual(training["policy_iterations"], 75)
        self.assertEqual(training["episodes_per_iteration"], 10)
        self.assertEqual(training["policy_iterations"] + 1, 76)
        self.assertEqual(training["replay_scope"], "recent_episodes")
        self.assertEqual(training["replay_capacity_episodes"], 100)
        self.assertEqual(training["replay_sample_episodes_per_update"], 30)
        self.assertEqual(training["initial_update_epochs"], 1)
        self.assertEqual(training["max_epochs_per_iteration"], 2)
        self.assertEqual(training["target_tau"], 0.03)
        self.assertTrue(training["compact_episode_tensors"])
        self.assertTrue(training["release_samples_after_update"])
        schedule = _balanced_worker_schedule([3], 50)
        self.assertEqual(schedule, [3] * 50)
        validation = cfg["validation"]
        screening_iterations = _resolve_screening_iterations(
            policy_iterations=int(training["policy_iterations"]),
            interval=int(validation["screening_interval_iterations"]),
            include_initial=bool(validation["screening_include_initial"]),
            configured=list(validation["screening_iterations"]),
        )
        self.assertEqual(
            screening_iterations,
            [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75],
        )
        screening_episodes = len(screening_iterations) * int(
            validation["screening_seed_count"]
        )
        selection_episodes = int(validation["final_candidate_count"]) * int(
            validation["final_selection_seed_count"]
        )
        self.assertEqual(screening_episodes, 160)
        self.assertEqual(selection_episodes, 40)
        self.assertEqual(screening_episodes + selection_episodes, 200)
        self.assertTrue(training["early_stopping"]["enabled"])
        self.assertEqual(training["early_stopping"]["min_iterations"], 45)
        self.assertEqual(training["early_stopping"]["patience_iterations"], 20)
        self.assertEqual(training["early_stopping"]["consecutive_checks"], 2)
        self.assertEqual(training["early_stopping"]["paired_ci_upper_threshold"], .5)
        self.assertEqual(training["learning_rate"], 0.00005)
        self.assertEqual(training["loss_type"], "mse")
        self.assertNotIn("huber_delta", training)
        self.assertNotIn("random_wait_probability", training)
        self.assertEqual(training["max_epochs_per_iteration"], 2)
        self.assertEqual(training["epsilon_start"], 0.50)
        self.assertEqual(training["epsilon_end"], 0.05)
        self.assertEqual(training["epsilon_schedule"]["points"][25], 0.15)
        self.assertEqual(training["learning_rate_schedule"]["points"][75], 0.00001)
        self.assertEqual(training["max_review_interval_min"], 1.0)
        self.assertFalse(training["pairwise_mc_advantage"]["enabled"])
        self.assertEqual(training["pairwise_mc_advantage"]["loss_weight"], 1.0)
        self.assertEqual(cfg["algorithm"]["return_estimator"], "n_step_td")
        self.assertTrue(cfg["algorithm"]["n_step_enabled"])
        self.assertTrue(cfg["algorithm"]["td_bootstrap_enabled"])
        self.assertEqual(cfg["algorithm"]["n_step"], 30)
        self.assertFalse(cfg["algorithm"]["allow_wait_action"])
        self.assertFalse(cfg["diagnostics"]["value_validation"]["enabled"])
        self.assertFalse(
            cli_default_cfg["diagnostics"]["value_validation"]["enabled"]
        )
        self.assertEqual(
            cfg["algorithm"]["initial_policy"],
            "uniform_random_feasible_no_wait",
        )

    def test_explicit_short_replay_profile_matches_default_replay_contract(self) -> None:
        cfg = OmegaConf.to_container(
            OmegaConf.load(
                Path("configs/adp/mfg_flow_shop_throughput_worker3_short_replay.yaml")
            ),
            resolve=True,
        )
        training = cfg["training"]
        self.assertEqual(training["replay_scope"], "recent_window")
        self.assertEqual(training["replay_window_iterations"], 5)
        self.assertEqual(training["latest_iteration_weight"], 2.0)
        self.assertEqual(training["policy_iterations"], 50)
        self.assertEqual(training["episodes_per_iteration"], 10)
        rollout = cfg["rollout"]
        self.assertTrue(rollout["parallel"])
        self.assertEqual(rollout["process_count"], 10)
        self.assertEqual(rollout["wave_size"], 10)

    def test_training_cli_defaults_to_worker3_td_profile(self) -> None:
        args = build_parser().parse_args([])
        self.assertEqual(
            args.config.name,
            "mfg_flow_shop_throughput.yaml",
        )

    def test_10x100_training_profile_has_requested_wave_schedule(self) -> None:
        cfg = OmegaConf.to_container(
            OmegaConf.load(Path("configs/adp/mfg_flow_shop_throughput_10x100.yaml")),
            resolve=True,
        )
        training = cfg["training"]
        validation = cfg["validation"]
        rollout = cfg["rollout"]
        wave_size = int(rollout["wave_size"])
        screening_iterations = [
            iteration
            for iteration in range(int(training["policy_iterations"]) + 1)
            if _should_validate(
                iteration,
                int(training["policy_iterations"]),
                int(validation["screening_interval_iterations"]),
                include_initial=bool(validation["screening_include_initial"]),
            )
        ]
        self.assertEqual(screening_iterations, list(range(11)))
        self.assertTrue(validation["combined_checkpoint_evaluation"])
        self.assertEqual(validation["train_eval_seed_count"], 10)
        self.assertEqual(validation["screening_seed_count"], 10)
        self.assertEqual(len(cfg["seed_partitions"]["training_evaluation"]), 10)
        self.assertEqual(int(training["initial_random_episodes"]) // wave_size, 2)
        self.assertEqual(
            int(training["policy_iterations"])
            * (int(training["episodes_per_iteration"]) // wave_size),
            100,
        )
        self.assertEqual(
            len(screening_iterations)
            * (
                (
                    int(validation["train_eval_seed_count"])
                    + int(validation["screening_seed_count"])
                )
                // wave_size
            ),
            22,
        )
        self.assertEqual(
            int(validation["final_candidate_count"])
            * (int(validation["final_selection_seed_count"]) // wave_size),
            6,
        )
        self.assertEqual(rollout["process_count"], 10)
        self.assertEqual(rollout["wave_size"], 10)
        self.assertEqual(rollout["device"], "cpu")
        self.assertEqual(rollout["start_method"], "spawn")
        self.assertEqual(rollout["torch_threads_per_process"], 1)
        self.assertEqual(cfg["runtime"]["device"], "cuda:0")
        self.assertTrue(cfg["runtime"]["require_cuda_training"])
        self.assertFalse(cfg["algorithm"]["allow_wait_action"])
        self.assertEqual(
            cfg["algorithm"]["initial_policy"],
            "uniform_random_feasible_no_wait",
        )
        self.assertTrue(cfg["diagnostics"]["fixed_action_probe"]["enabled"])
        self.assertEqual(len(cfg["diagnostics"]["fixed_action_probe"]["seeds"]), 2)

    def test_worker3_wave_update_profile_has_requested_schedule(self) -> None:
        cfg = OmegaConf.to_container(
            OmegaConf.load(
                Path(
                    "configs/adp/"
                    "mfg_flow_shop_throughput_worker3_wave_updates.yaml"
                )
            ),
            resolve=True,
        )
        training = cfg["training"]
        validation = cfg["validation"]
        rollout = cfg["rollout"]
        screening_iterations = _resolve_screening_iterations(
            policy_iterations=int(training["policy_iterations"]),
            interval=int(validation["screening_interval_iterations"]),
            include_initial=bool(validation["screening_include_initial"]),
            configured=list(validation["screening_iterations"]),
        )

        self.assertEqual(cfg["worker_counts"], [3])
        self.assertEqual(int(rollout["process_count"]), 10)
        self.assertEqual(int(rollout["wave_size"]), 10)
        self.assertEqual(int(training["initial_random_episodes"]), 10)
        self.assertEqual(int(training["policy_iterations"]), 50)
        self.assertEqual(int(training["episodes_per_iteration"]), 10)
        self.assertEqual(int(training["policy_iterations"]) + 1, 51)
        self.assertEqual(float(training["epsilon_start"]), 0.50)
        self.assertEqual(float(training["epsilon_end"]), 0.10)
        pairwise = training["pairwise_mc_advantage"]
        self.assertFalse(pairwise["enabled"])
        self.assertEqual(float(pairwise["loss_weight"]), 1.0)
        self.assertEqual(float(pairwise["capture_episode_fraction"]), 0.20)
        self.assertEqual(int(pairwise["minimum_pairs_per_batch"]), 2)
        self.assertEqual(list(pairwise["decision_thresholds"]), [20, 100, 300])
        self.assertFalse(validation["combined_checkpoint_evaluation"])
        self.assertEqual(
            screening_iterations,
            [0, 1, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50],
        )
        self.assertEqual(
            (
                int(training["initial_random_episodes"])
                // int(rollout["wave_size"])
            ),
            1,
        )
        self.assertEqual(
            int(training["policy_iterations"])
            * (
                int(training["episodes_per_iteration"])
                // int(rollout["wave_size"])
            ),
            50,
        )
        self.assertEqual(len(screening_iterations), 12)
        self.assertEqual(
            len(screening_iterations)
            * int(validation["screening_seed_count"])
            // int(rollout["wave_size"]),
            12,
        )
        self.assertEqual(
            int(validation["final_candidate_count"])
            * int(validation["final_selection_seed_count"])
            // int(rollout["wave_size"]),
            1,
        )
        self.assertNotIn("potential_shaping", cfg)

    def test_quick_multifleet_profile_balances_worker3_and_worker5(self) -> None:
        cfg = OmegaConf.to_container(
            OmegaConf.load(
                Path("configs/adp/mfg_flow_shop_throughput_workers_3_5_quick.yaml")
            ),
            resolve=True,
        )
        training = cfg["training"]
        validation = cfg["validation"]
        self.assertEqual(cfg["worker_counts"], [3, 5])
        self.assertEqual(
            int(training["initial_random_episodes"])
            + int(training["policy_iterations"])
            * int(training["episodes_per_iteration"]),
            660,
        )
        self.assertEqual(_balanced_worker_schedule([3, 5], 80).count(3), 40)
        self.assertEqual(_balanced_worker_schedule([3, 5], 80).count(5), 40)
        self.assertEqual(
            [
                iteration
                for iteration in range(9)
                if _should_validate(
                    iteration,
                    8,
                    int(validation["screening_interval_iterations"]),
                    include_initial=True,
                )
            ],
            [0, 2, 4, 6, 8],
        )
        self.assertFalse(cfg["algorithm"]["allow_wait_action"])
        self.assertEqual(cfg["algorithm"]["worker_order_strategy"], "cyclic")
        self.assertFalse(cfg["diagnostics"]["fixed_action_probe"]["enabled"])

    def test_training_device_rejects_cpu_when_cuda_is_required(self) -> None:
        import torch

        with self.assertRaisesRegex(RuntimeError, "require CUDA"):
            _resolve_training_device(torch, "cpu", require_cuda=True)
        self.assertEqual(str(_resolve_training_device(torch, "cpu", require_cuda=False)), "cpu")

    def test_standard_parallel_schedule_uses_twenty_episode_waves(self) -> None:
        def jobs(count: int, phase: str = "policy_iteration_1") -> list[RolloutJob]:
            return [
                RolloutJob(
                    episode=index + 1,
                    phase=phase,
                    iteration=1,
                    worker_count=3 + index % 4,
                    seed=2026 + index,
                    days=5,
                    epsilon=0.2,
                    force_random=False,
                    adp_cfg={},
                    collect_compact_samples=True,
                    gamma=1.0,
                    wave_id=f"{phase}-W{index // 20 + 1:02d}",
                    snapshot_hash="snapshot",
                )
                for index in range(count)
            ]

        self.assertEqual([len(wave) for wave in _wave_chunks(jobs(50), 20)], [20, 20, 10])
        self.assertEqual([len(wave) for wave in _wave_chunks(jobs(100), 20)], [20] * 5)
        initial_waves = len(_wave_chunks(jobs(100, "initial_random"), 20))
        policy_waves = 30 * len(_wave_chunks(jobs(100), 20))
        screening_waves = 16 * len(_wave_chunks(jobs(10, "screening_validation"), 20))
        selection_waves = 5 * len(_wave_chunks(jobs(30, "final_selection_validation"), 20))
        self.assertEqual(initial_waves + policy_waves + screening_waves + selection_waves, 181)

    def test_seed_partitions_reject_overlap(self) -> None:
        with self.assertRaisesRegex(ValueError, "seed partitions"):
            _validate_seed_partitions(
                training_seeds=[1, 2],
                screening_seeds=[3],
                final_selection_seeds=[4],
                held_out_seeds=[2, 5],
                enforce_disjoint=True,
            )
        metadata = _validate_seed_partitions(
            training_seeds=[1, 2],
            screening_seeds=[3],
            final_selection_seeds=[4],
            held_out_seeds=[5],
            enforce_disjoint=True,
        )
        self.assertTrue(metadata["disjoint"])

    def test_parallel_wave_metrics_use_actual_active_slots(self) -> None:
        jobs = [
            RolloutJob(
                episode=index + 1,
                phase="smoke",
                iteration=0,
                worker_count=3,
                seed=2026 + index,
                days=1,
                epsilon=1.0,
                force_random=True,
                adp_cfg={},
                collect_compact_samples=True,
                gamma=1.0,
                wave_id="smoke-W01",
                snapshot_hash="RANDOM",
            )
            for index in range(2)
        ]
        results = [
            EpisodeResult(
                episode=index + 1,
                phase="smoke",
                worker_count=3,
                seed=2026 + index,
                products=1,
                scrap=0,
                raw_return=1.0,
                decisions=4,
                fingerprint={},
                elapsed_sec=5.0,
                child_peak_rss_mib=512.0,
                process_slot=f"SpawnProcess-{index + 1}",
                snapshot_hash="RANDOM",
            )
            for index in range(2)
        ]
        row = _wave_performance_row(
            wave_jobs=jobs,
            results=results,
            wave_wall_sec=6.0,
            configured_process_count=10,
        )
        self.assertEqual(row["active_process_count"], 2)
        self.assertAlmostEqual(row["effective_speedup"], 10.0 / 6.0, places=5)
        self.assertAlmostEqual(row["parallel_efficiency"], (10.0 / 6.0) / 2.0, places=5)
        self.assertEqual(row["snapshot_hash_match"], True)

    def test_combined_checkpoint_wave_reports_both_evaluation_partitions(self) -> None:
        jobs = [
            RolloutJob(
                episode=index + 1,
                phase="checkpoint_train_eval" if index < 10 else "checkpoint_validation",
                iteration=4,
                worker_count=3,
                seed=2026 + index,
                days=5,
                epsilon=0.0,
                force_random=False,
                adp_cfg={},
                collect_compact_samples=False,
                gamma=1.0,
                wave_id="checkpoint_diagnostic-I04-W01",
                snapshot_hash="frozen",
            )
            for index in range(20)
        ]
        results = [
            EpisodeResult(
                episode=job.episode,
                phase=job.phase,
                worker_count=3,
                seed=job.seed,
                products=10,
                scrap=0,
                raw_return=10.0,
                decisions=20,
                fingerprint={},
                elapsed_sec=1.0,
                process_slot=f"SpawnProcess-{index + 1}",
                snapshot_hash="frozen",
            )
            for index, job in enumerate(jobs)
        ]
        row = _wave_performance_row(
            wave_jobs=jobs,
            results=results,
            wave_wall_sec=2.0,
            configured_process_count=20,
        )
        self.assertEqual(row["phase"], "checkpoint_diagnostic")
        self.assertEqual(
            row["phase_components"],
            "checkpoint_train_eval,checkpoint_validation",
        )
        self.assertTrue(row["snapshot_hash_match"])

    def test_recovered_wave_aggregation_weights_partial_wave_slots(self) -> None:
        metrics = _aggregate_wave_metrics(
            [
                {
                    "wall_sec": 10.0,
                    "episode_elapsed_sum_sec": 90.0,
                    "episode_count": 10,
                    "active_process_count": 10,
                },
                {
                    "wall_sec": 5.0,
                    "episode_elapsed_sum_sec": 9.0,
                    "episode_count": 2,
                    "active_process_count": 2,
                },
            ]
        )
        self.assertAlmostEqual(metrics["speedup"], 99.0 / 15.0)
        self.assertAlmostEqual(metrics["efficiency"], 99.0 / 110.0)
        self.assertAlmostEqual(metrics["utilization"], 99.0 / 110.0)

    def test_policy_snapshot_hash_is_stable_and_parameter_sensitive(self) -> None:
        import torch

        model = build_value_network(embedding_dim=32, heads=4, layers=1)
        first_state, first_hash = _snapshot_state_dict(model)
        second_state, second_hash = _snapshot_state_dict(model)
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(set(first_state), set(second_state))
        with torch.no_grad():
            next(model.parameters()).add_(1.0)
        _, changed_hash = _snapshot_state_dict(model)
        self.assertNotEqual(first_hash, changed_hash)

    def test_conservative_gate_requires_strict_validation_improvement(self) -> None:
        self.assertTrue(
            _should_accept_candidate(
                enabled=True,
                iteration=0,
                candidate_mean=10.0,
                incumbent_mean=None,
                min_improvement=0.0,
            )
        )
        self.assertFalse(
            _should_accept_candidate(
                enabled=True,
                iteration=1,
                candidate_mean=10.0,
                incumbent_mean=10.0,
                min_improvement=0.0,
            )
        )
        self.assertTrue(
            _should_accept_candidate(
                enabled=True,
                iteration=1,
                candidate_mean=10.5,
                incumbent_mean=10.0,
                min_improvement=0.0,
            )
        )
        self.assertTrue(
            _should_accept_candidate(
                enabled=False,
                iteration=1,
                candidate_mean=1.0,
                incumbent_mean=10.0,
                min_improvement=0.0,
            )
        )

    def test_optimizer_hash_returns_to_incumbent_after_restore(self) -> None:
        import copy
        import torch

        model = build_value_network(embedding_dim=32, heads=4, layers=1)
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
        incumbent_state = copy.deepcopy(optimizer.state_dict())
        incumbent_hash = _payload_hash(incumbent_state)
        optimizer.param_groups[0]["lr"] = 5e-2
        self.assertNotEqual(_payload_hash(optimizer.state_dict()), incumbent_hash)
        optimizer.load_state_dict(copy.deepcopy(incumbent_state))
        self.assertEqual(_payload_hash(optimizer.state_dict()), incumbent_hash)

    def test_training_dashboard_contains_parallel_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            episode_rows = [
                {
                    "episode": 1,
                    "phase": "initial_random",
                    "worker_count": 3,
                    "seed": 2026,
                    "products": 2,
                    "worker_wait_ratio": 0.25,
                    "joint_all_wait_ratio": 0.1,
                    "max_consecutive_all_wait_decisions": 2,
                    "elapsed_sec": 5.0,
                    "child_peak_rss_mib": 500.0,
                },
                {
                    "episode": 1000001,
                    "iteration": 0,
                    "phase": "screening_validation",
                    "worker_count": 3,
                    "seed": 102626,
                    "products": 999,
                    "worker_wait_ratio": 0.0,
                    "joint_all_wait_ratio": 0.0,
                    "max_consecutive_all_wait_decisions": 0,
                    "elapsed_sec": 5.0,
                    "child_peak_rss_mib": 500.0,
                },
            ]
            iteration_rows = [
                {
                    "iteration": 0,
                    "mc_loss": 1.0,
                    "batch_episode_count": 1,
                    "sample_count": 2,
                    "compact_batch_mib": 0.1,
                    "process_rss_mib": 600.0,
                    "rollout_wall_sec": 6.0,
                    "mc_update_sec": 1.0,
                    "epsilon": 1.0,
                    "validation_performed": True,
                    "train_eval_performed": True,
                    "train_eval_products_avg": 2.2,
                    "train_eval_products_ci95_low": 1.7,
                    "train_eval_products_ci95_high": 2.7,
                    "validation_products_avg": 2.0,
                    "validation_products_ci95_low": 1.5,
                    "validation_products_ci95_high": 2.5,
                    "ood_diagnostics_available": True,
                    "ood_selection_rate": 0.25,
                    "ood_overestimation_excess": 0.75,
                    "action_rank_correlation": 0.4,
                    "candidate_top1_agreement": 0.5,
                    "selected_action_regret": 1.0,
                },
                {
                    "iteration": "1",
                    "mc_loss": "0.5",
                    "batch_episode_count": "1",
                    "sample_count": "2",
                    "compact_batch_mib": "0.1",
                    "process_rss_mib": "600.0",
                    "rollout_wall_sec": "6.0",
                    "mc_update_sec": "1.0",
                    "epsilon": "0.2",
                    "validation_performed": "False",
                    "train_eval_performed": "False",
                    "train_eval_products_avg": "",
                    "validation_products_avg": "",
                },
            ]
            wave_rows = [
                {
                    "wave_id": "initial_random-I00-W01",
                    "phase": "initial_random",
                    "status": "completed",
                    "episode_count": 1,
                    "wall_sec": 6.0,
                    "episodes_per_hour": 600.0,
                    "effective_speedup": 0.8,
                    "parallel_efficiency": 0.08,
                },
                {
                    "wave_id": "policy_iteration_1-I01-W01",
                    "phase": "policy_iteration_1",
                    "status": "failed",
                    "completed_episode_count": 2,
                    "cancelled_episode_count": 8,
                    "error": "RuntimeError: fixture failure",
                },
            ]
            summary = {
                "training_episode_count": 1,
                "validation_episode_count": 0,
                "train_evaluation_episode_count": 1,
                "configured_process_count": 10,
                "actual_process_count_max": 1,
                "wave_size": 10,
                "multiprocessing_start_method": "spawn",
                "rollout_device": "cpu",
                "training_device": "cuda",
                "require_cuda_training": True,
                "training_device_metadata": {
                    "resolved_device": "cuda:0",
                    "torch_version": "2.test",
                    "cuda_runtime_version": "12.8",
                    "cudnn_version": 9000,
                    "gpu_count": 2,
                    "gpu_index": 0,
                    "gpu_name": "Fixture GPU",
                    "gpu_total_memory_mib": 24564.0,
                    "gpu_compute_capability": "8.9",
                },
                "runtime_environment": {
                    "host_name": "fixture-host",
                    "operating_system": "Fixture OS",
                    "python_version": "3.12.test",
                    "cpu_model": "Fixture CPU",
                    "logical_cpu_count": 32,
                },
                "torch_threads_per_process": 1,
                "phase_wave_counts": {
                    "initial_random": 1,
                    "policy_iteration": 0,
                    "checkpoint_diagnostic": 0,
                    "screening_validation": 1,
                    "final_selection_validation": 0,
                },
                "total_wave_count": 1,
                "best_checkpoint": "best.pt",
                "loss_type": "mse",
                "wait_action_enabled": True,
                "replay_scope": "recent_window",
                "replay_window_iterations": 5,
                "latest_iteration_weight": 2.0,
            }
            dashboard = render_training_dashboard(output, episode_rows, iteration_rows, wave_rows, summary)
            content = dashboard.read_text(encoding="utf-8")
            self.assertIn("병렬 실행 설정", content)
            self.assertIn("Effective Speedup", content)
            self.assertIn("가장 느린 Wave", content)
            self.assertIn("실패한 Wave", content)
            self.assertIn("fixture failure", content)
            self.assertIn("Fixture GPU", content)
            self.assertIn("Fixture CPU", content)
            self.assertIn("PyTorch / CUDA / cuDNN", content)
            self.assertIn("Value Updates (incl. initial)", content)
            self.assertIn("Policy Iterations", content)
            self.assertIn("Validation mean [95% CI]", content)
            self.assertIn("2.000 [1.500, 2.500]", content)
            self.assertIn("Random Baseline</span><strong>2.000 +/- 0.000", content)
            self.assertNotIn("Random Baseline</span><strong>500.500", content)
            self.assertIn("Iteration별 탐색 Rollout 및 Validation 생산량", content)
            self.assertIn("Worker 수별 미사용 Validation 생산량", content)
            self.assertIn("Initial / policy / checkpoint diagnostic / final waves", content)
            self.assertIn("1 / 0 / 1 / 0", content)
            self.assertNotIn("Train-eval (epsilon=0)", content)
            self.assertIn("Validation (epsilon=0)", content)
            self.assertIn("Beam 후보 가치 엔트로피", content)
            self.assertIn("Iteration별 탐색 Rollout 및 Validation 생산량", content)
            self.assertIn("Iteration", content)
            self.assertIn("5일 episode 완료 제품 수", content)
            self.assertIn("미관측 행동 선택률", content)
            self.assertIn("미관측 행동 추가 과대평가량", content)
            self.assertIn("25.00%", content)
            self.assertIn("0.750 products", content)
            self.assertIn("업데이트 입력 Rollout의 WAIT/미배정 행동", content)
            self.assertIn("Rollout 생성 checkpoint (-1=초기 무작위)", content)
            self.assertIn("업데이트 직후 Checkpoint Validation의 WAIT/미배정 행동", content)
            self.assertIn("업데이트 직후 checkpoint", content)
            self.assertIn("명시적 WAIT와 후보 없음에 따른 미배정이 합산", content)
            self.assertIn("Post-Decision 가치함수 Holdout MSE", content)
            self.assertIn("최근 replay window를 episode 단위로 나눈 holdout", content)
            self.assertIn("MC 업데이트 Batch와 Short Replay", content)
            self.assertIn("Replay samples (weighted)", content)
            self.assertIn("Short Replay 보관 규모", content)
            self.assertNotIn("고정 Probe 가치 예측 MSE", content)
            self.assertNotIn("후보 행동 가치 순위 상관계수", content)
            self.assertNotIn("후보집합 최선 행동 Top-1 일치율", content)
            self.assertNotIn("선택 행동 Regret", content)
            self.assertIn("독립 MC 및 반복 행동 검증 미실행", content)
            self.assertIn("독립 greedy validation 오차가 아닙니다", content)
            self.assertNotIn("미선택 행동 가치 예측 MSE", content)
            self.assertNotIn("Potential-Shaped", content)
            self.assertNotIn("Huber", content)
            self.assertIn("Prediction std", content)
            self.assertIn("MC target std", content)
            self.assertIn("<strong>해석:</strong>", content)

    def test_recovered_dashboard_marks_unavailable_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            dashboard = render_training_dashboard(
                output,
                [
                    {
                        "episode": 1,
                        "iteration": 0,
                        "phase": "initial_random",
                        "worker_count": 3,
                        "products": 2,
                        "elapsed_sec": 5.0,
                        "child_peak_rss_mib": 500.0,
                    }
                ],
                [
                    {
                        "iteration": 0,
                        "mc_loss": 0.0,
                        "mc_mae": 0.0,
                        "mc_rmse": 0.0,
                        "prediction_std": 0.0,
                        "target_std": 0.0,
                        "mc_update_sec": 0.0,
                        "process_rss_mib": 0.0,
                    }
                ],
                [],
                {
                    "fit_diagnostics_available": False,
                    "update_timing_available": False,
                    "gpu_memory_diagnostics_available": False,
                    "process_memory_diagnostics_available": False,
                    "wall_clock_complete": False,
                    "recovered_after_interruption": True,
                    "recovery_note": "fixture recovery",
                    "total_wall_clock_sec": 5.0,
                },
            )
            content = dashboard.read_text(encoding="utf-8")
            self.assertIn("Post-Decision Value Holdout MSE (Unavailable After Recovery)", content)
            self.assertIn("Monte Carlo Value Prediction Error (Unavailable After Recovery)", content)
            self.assertIn("GPU Value-Network Update Time (Unavailable After Recovery)", content)
            self.assertIn("GPU Peak Allocated Memory by Policy Update (Unavailable After Recovery)", content)
            self.assertIn("Parent Process Memory by Policy Update (Unavailable After Recovery)", content)
            self.assertIn("Observed Simulation Wall", content)
            self.assertIn("N/A (recovered run)", content)
            self.assertNotIn("GPU update 0.0s", content)

    def test_checkpoint_fingerprint_mismatch_fails_without_fallback(self) -> None:
        import torch

        cfg = _compose_episode_cfg(worker_count=3, seed=2026, days=1, adp_cfg={"force_random_policy": True})
        env = simpy.Environment()
        world = ManufacturingWorld(
            env,
            cfg,
            InMemoryEventLogger(),
            build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
        )
        model = build_value_network(embedding_dim=32, heads=4, layers=1)
        manifest = {
            "scenario_type": "mfg_flow_shop",
            "objective_mode": "maximize_throughput",
            "feature_schema_version": "mfg_flow_shop_adp_v2",
            "timing_fingerprint": world.timing.profile_fingerprint,
            "worker_count_range": [3, 6],
            "model": {"embedding_dim": 32, "heads": 4, "layers": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.pt"
            save_checkpoint(path, model=model, optimizer=None, manifest=manifest)
            with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
                load_checkpoint(path, world=world, device=torch.device("cpu"))

    def test_multifleet_checkpoint_accepts_exact_supported_worker_counts(self) -> None:
        import torch

        def make_world(worker_count: int) -> ManufacturingWorld:
            cfg = _compose_episode_cfg(
                worker_count=worker_count,
                seed=2026,
                days=1,
                adp_cfg={
                    "force_random_policy": True,
                    "allow_wait_action": False,
                    "worker_order_strategy": "cyclic",
                },
            )
            return ManufacturingWorld(
                simpy.Environment(),
                cfg,
                InMemoryEventLogger(),
                build_decision_module(
                    experiment_cfg=cfg,
                    decision_mode="simulation_based_adp",
                ),
            )

        worlds = {count: make_world(count) for count in (3, 4, 5)}
        manifest = {
            **checkpoint_fingerprint(worlds[3]),
            "supported_worker_counts": [3, 5],
            "worker_count_range": [3, 5],
            "environment_fingerprints_by_worker_count": {
                str(count): checkpoint_fingerprint(worlds[count])["environment_fingerprint"]
                for count in (3, 5)
            },
            "model": {"embedding_dim": 32, "heads": 4, "layers": 1},
        }
        model = build_value_network(embedding_dim=32, heads=4, layers=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "multifleet.pt"
            save_checkpoint(path, model=model, optimizer=None, manifest=manifest)
            for count in (3, 5):
                loaded, _ = load_checkpoint(
                    path,
                    world=worlds[count],
                    device=torch.device("cpu"),
                    wait_action_enabled=False,
                    worker_order_strategy="cyclic",
                )
                self.assertIsNotNone(loaded)
            with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
                load_checkpoint(
                    path,
                    world=worlds[4],
                    device=torch.device("cpu"),
                    wait_action_enabled=False,
                    worker_order_strategy="cyclic",
                )


if __name__ == "__main__":
    unittest.main()
