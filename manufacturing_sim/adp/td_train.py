"""n-step training driver; simulation, beam policy and rollout workers stay shared."""
from __future__ import annotations

import copy
import gc
import json
import math
import random
import statistics
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from .checkpoint import save_checkpoint
from .compact import merge_compact_batches
from .model import build_value_network, require_torch
from .ood_diagnostics import build_ood_support_bank, evaluate_ood_selected_actions
from .schema import FEATURE_SCHEMA_VERSION
from .td import build_target_plan, fit_td_value, retain_recent_episodes, sample_replay_episodes


def _piecewise_schedule_points(
    training: dict[str, Any],
    *,
    schedule_key: str,
    total_iterations: int,
    fallback_start_key: str,
    fallback_end_key: str | None = None,
) -> list[tuple[int, float]]:
    schedule = training.get(schedule_key)
    if schedule is None:
        start = float(training[fallback_start_key])
        end = float(training[fallback_end_key]) if fallback_end_key else start
        return [(0, start), (max(0, int(total_iterations)), end)]
    if schedule.get("type") != "piecewise_linear":
        raise ValueError(f"training.{schedule_key}.type must be piecewise_linear.")
    raw_points = schedule.get("points")
    if not isinstance(raw_points, dict) or not raw_points:
        raise ValueError(f"training.{schedule_key}.points must be a non-empty mapping.")
    try:
        points = sorted((int(index), float(value)) for index, value in raw_points.items())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"training.{schedule_key}.points must map integer iterations to numbers.") from exc
    if len({index for index, _ in points}) != len(points):
        raise ValueError(f"training.{schedule_key}.points contains duplicate iterations.")
    if points[0][0] != 0 or points[-1][0] != int(total_iterations):
        raise ValueError(
            f"training.{schedule_key}.points must start at 0 and end at policy_iterations "
            f"({int(total_iterations)})."
        )
    if any(index < 0 or index > int(total_iterations) for index, _ in points):
        raise ValueError(f"training.{schedule_key}.points is outside the training iteration range.")
    if any(not math.isfinite(value) for _, value in points):
        raise ValueError(f"training.{schedule_key}.points must be finite.")
    return points


def _piecewise_schedule_value(points: list[tuple[int, float]], iteration: int) -> float:
    index = int(iteration)
    if index <= points[0][0]:
        return float(points[0][1])
    for (left_index, left_value), (right_index, right_value) in zip(points, points[1:]):
        if index <= right_index:
            if right_index == left_index:
                return float(right_value)
            fraction = (index - left_index) / (right_index - left_index)
            return float(left_value + (right_value - left_value) * fraction)
    return float(points[-1][1])


def validate_td_config(cfg: dict[str, Any]) -> None:
    algorithm, training = cfg["algorithm"], cfg["training"]
    warm_start = cfg.get("warm_start", {})
    warm_start_enabled = bool(warm_start.get("enabled", False))
    configured_workers = [int(value) for value in cfg.get("worker_counts", [])]
    if len(configured_workers) != 1 or configured_workers[0] not in range(2, 7):
        raise ValueError("Fleet-specific n-step TD requires exactly one worker count in the range 2..6.")
    if cfg.get("scenario") != "mfg_flow_shop" or cfg.get("objective_mode") != "maximize_throughput":
        raise ValueError("TD supports mfg_flow_shop / maximize_throughput only.")
    if algorithm.get("return_estimator") != "n_step_td" or not algorithm.get("td_bootstrap_enabled"):
        raise ValueError("n-step TD requires return_estimator=n_step_td and td_bootstrap_enabled=true.")
    if not algorithm.get("n_step_enabled") or int(algorithm.get("n_step", 30)) < 1:
        raise ValueError("n_step_enabled=true and n_step >= 1 are required.")
    if algorithm.get("off_policy_correction") != "truncate_on_greedy_mismatch":
        raise ValueError("TD replay requires truncate_on_greedy_mismatch.")
    if float(training.get("gamma", 1.0)) != 1.0:
        raise ValueError("Finite-horizon total-products TD requires gamma=1.")
    if training.get("loss_type") != "mse" or "huber_delta" in training or "potential_shaping" in cfg:
        raise ValueError("TD uses MSE and raw product rewards only.")
    if cfg.get("policy_update", {}).get("enabled") or training.get("pairwise_mc_advantage", {}).get("enabled"):
        raise ValueError("Disable conservative validation gate and pairwise MC for n-step TD.")
    if cfg.get("policy_update", {}).get("mode", "always_accept") != "always_accept":
        raise ValueError("TD policy_update.mode must be always_accept.")
    if not training.get("compact_episode_tensors", True) or not training.get("release_samples_after_update", True):
        raise ValueError("TD requires compact tensors and expiration of old replay episodes.")
    if training.get("replay_scope") != "recent_episodes" or int(training.get("replay_capacity_episodes", 0)) < 1:
        raise ValueError("TD requires bounded recent_episodes replay.")
    if float(training.get("validation_episode_fraction", 0.1)) != 0.1:
        raise ValueError("TD uses a stable 10% episode holdout (episode ID divisible by 10).")
    if not 0 < float(training.get("target_tau", 0)) <= 1:
        raise ValueError("target_tau must be in (0,1].")
    for key in ("initial_random_episodes", "episodes_per_iteration", "batch_size", "max_epochs_per_iteration"):
        if int(training[key]) < 1:
            raise ValueError(f"training.{key} must be positive.")
    initial_epochs = int(training.get("initial_update_epochs", 0))
    update_episode_count = int(training.get("replay_sample_episodes_per_update", 0))
    if warm_start_enabled:
        if initial_epochs != 0:
            raise ValueError(
                "Warm-start replay fill must use training.initial_update_epochs=0 so iteration 0 "
                "remains the unchanged source checkpoint."
            )
        if not str(warm_start.get("checkpoint_path", "")).strip():
            raise ValueError("warm_start.checkpoint_path is required when warm start is enabled.")
        epsilon = float(warm_start.get("replay_fill_epsilon", training["epsilon_start"]))
        if not 0.0 <= epsilon <= 1.0:
            raise ValueError("warm_start.replay_fill_epsilon must be in [0,1].")
    elif initial_epochs < 1:
        raise ValueError("training.initial_update_epochs must be positive.")
    if update_episode_count < int(training["episodes_per_iteration"]):
        raise ValueError(
            "training.replay_sample_episodes_per_update must include the complete current wave."
        )
    if update_episode_count > int(training["replay_capacity_episodes"]):
        raise ValueError(
            "training.replay_sample_episodes_per_update cannot exceed replay capacity."
        )
    if int(training["initial_random_episodes"]) > int(training["replay_capacity_episodes"]):
        raise ValueError("Initial random episodes must fit in replay for the initial full-data update.")
    required_history = update_episode_count - int(training["episodes_per_iteration"])
    if required_history > int(training["initial_random_episodes"]):
        raise ValueError("Initial replay does not contain enough history for the first policy update.")
    if int(training["policy_iterations"]) < 0:
        raise ValueError("policy_iterations must be nonnegative.")
    if not 0 <= float(training["epsilon_end"]) <= float(training["epsilon_start"]) <= 1:
        raise ValueError("epsilon must satisfy 0 <= end <= start <= 1.")
    total_iterations = int(training["policy_iterations"])
    epsilon_points = _piecewise_schedule_points(
        training,
        schedule_key="epsilon_schedule",
        total_iterations=total_iterations,
        fallback_start_key="epsilon_start",
        fallback_end_key="epsilon_end",
    )
    if any(not 0.0 <= value <= 1.0 for _, value in epsilon_points):
        raise ValueError("training.epsilon_schedule values must be in [0,1].")
    if any(right > left for (_, left), (_, right) in zip(epsilon_points, epsilon_points[1:])):
        raise ValueError("training.epsilon_schedule must be non-increasing.")
    if algorithm.get("worker_order_strategy") not in {"cyclic", "fixed"}:
        raise ValueError("Unknown worker_order_strategy.")
    expected = "uniform_random_feasible_with_wait" if algorithm.get("allow_wait_action") else "uniform_random_feasible_no_wait"
    if algorithm.get("initial_policy") != expected:
        raise ValueError("Initial random policy and WAIT configuration disagree.")
    if int(cfg.get("validation", {}).get("final_candidate_count", 1)) < 1:
        raise ValueError("validation.final_candidate_count must be positive.")
    if int(cfg["validation"].get("screening_interval_iterations", 0)) < 1:
        raise ValueError("screening_interval_iterations must be positive.")
    for key in ("learning_rate", "gradient_clip"):
        if not math.isfinite(float(training[key])) or float(training[key]) <= 0:
            raise ValueError(f"{key} must be finite and positive.")
    learning_rate_points = _piecewise_schedule_points(
        training,
        schedule_key="learning_rate_schedule",
        total_iterations=total_iterations,
        fallback_start_key="learning_rate",
    )
    if any(value <= 0.0 for _, value in learning_rate_points):
        raise ValueError("training.learning_rate_schedule values must be positive.")
    if any(right > left for (_, left), (_, right) in zip(learning_rate_points, learning_rate_points[1:])):
        raise ValueError("training.learning_rate_schedule must be non-increasing.")
    if cfg.get("diagnostics", {}).get("fixed_action_probe", {}).get("enabled"):
        raise ValueError("Fixed action probes are not part of n-step TD training.")
    if cfg.get("diagnostics", {}).get("value_validation", {}).get("enabled"):
        raise ValueError("Run separate value-validation diagnostics on the saved TD checkpoint.")
    rollout = cfg["rollout"]
    if rollout.get("device") != "cpu" or rollout.get("start_method") != "spawn":
        raise ValueError("TD rollout requires CPU spawn workers.")
    if not rollout.get("parallel", True):
        raise ValueError("Use process_count=1 for single-process TD rollout.")


def train_td(cfg: dict[str, Any], args: Any) -> Path:
    from .train import (
        RolloutJob, _configure_training_determinism, _process_rss_mib,
        _resolve_training_device, _resolve_screening_iterations, _run_rollout_jobs_parallel, _snapshot_state_dict,
        _training_device_metadata, _write_csv,
    )
    from .td_dashboard import render_td_dashboard
    from .live import TrainingProgress, atomic_json

    cfg = copy.deepcopy(cfg)
    t, v, a = cfg["training"], cfg["validation"], cfg["algorithm"]
    warm_start = cfg.setdefault("warm_start", {"enabled": False})
    if getattr(args, "warm_start_checkpoint", None) is not None:
        warm_start["enabled"] = True
        warm_start["checkpoint_path"] = str(args.warm_start_checkpoint)
    for name in ("initial_random_episodes", "policy_iterations", "episodes_per_iteration"):
        if getattr(args, name, None) is not None:
            t[name] = int(getattr(args, name))
    if args.days is not None:
        cfg["horizon_days"] = int(args.days)
    if args.worker_counts:
        cfg["worker_counts"] = list(args.worker_counts)
    training_worker_count = int(cfg["worker_counts"][0])
    if args.validation_episodes_per_worker is not None:
        count = int(args.validation_episodes_per_worker)
        if count < 1:
            raise ValueError("Validation seed count must be positive.")
        for key in ("screening_seed_count", "final_selection_seed_count"):
            v[key] = count
    if args.rollout_processes is not None:
        cfg["rollout"]["process_count"] = int(args.rollout_processes)
    validate_td_config(cfg)
    total_iterations = int(t["policy_iterations"])
    epsilon_points = _piecewise_schedule_points(
        t,
        schedule_key="epsilon_schedule",
        total_iterations=total_iterations,
        fallback_start_key="epsilon_start",
        fallback_end_key="epsilon_end",
    )
    learning_rate_points = _piecewise_schedule_points(
        t,
        schedule_key="learning_rate_schedule",
        total_iterations=total_iterations,
        fallback_start_key="learning_rate",
    )
    torch = require_torch()
    seed = int(cfg["base_seed"])
    determinism = _configure_training_determinism(torch, seed)
    device = _resolve_training_device(torch, args.device or cfg["runtime"]["device"],
                                      require_cuda=bool(cfg["runtime"].get("require_cuda_training", True)))
    processes = int(cfg["rollout"]["process_count"])
    wave_size = min(processes, int(cfg["rollout"]["wave_size"]))
    if processes < 1 or wave_size < 1 or int(cfg["horizon_days"]) < 1:
        raise ValueError("Positive process count, wave size and horizon are required.")
    total_episodes = int(t["initial_random_episodes"]) + int(t["policy_iterations"]) * int(t["episodes_per_iteration"])
    partitions = {"training": list(range(seed, seed + total_episodes))}
    for name, count_key in (("screening_validation", "screening_seed_count"),
                            ("final_selection_validation", "final_selection_seed_count")):
        pool = cfg["seed_partitions"][name]
        if len(pool) < int(v[count_key]) or int(v[count_key]) < 1:
            raise ValueError(f"Not enough seeds for {name}.")
        partitions[name] = list(pool[:int(v[count_key])])
    partitions["held_out_test"] = list(cfg["seed_partitions"]["held_out_test"])
    used: set[int] = set()
    for name, values in partitions.items():
        if len(set(values)) != len(values) or used.intersection(values):
            raise ValueError(f"Duplicate or overlapping {name} seeds.")
        used.update(values)
    screening = _resolve_screening_iterations(
        policy_iterations=int(t["policy_iterations"]), interval=int(v["screening_interval_iterations"]),
        include_initial=True, configured=v.get("screening_iterations") if args.policy_iterations is None else None,
    )
    output = (args.output or Path(cfg["runtime"]["output_root"]) / datetime.now().strftime("%Y%m%d_%H%M%S")).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "training_summary.json").exists() or (output / "episode_metrics.csv").exists():
        raise ValueError("Refusing to overwrite an existing TD training run.")
    (output / "checkpoints").mkdir(exist_ok=True)
    OmegaConf.save(OmegaConf.create(cfg), output / "resolved_config.yaml")
    model_cfg = cfg["model"]
    model = build_value_network(**{k: int(model_cfg[k]) for k in ("embedding_dim", "heads", "layers")}).to(device)
    target = copy.deepcopy(model).to(device).eval().requires_grad_(False)
    optimizer = torch.optim.Adam(model.parameters(), lr=_piecewise_schedule_value(learning_rate_points, 0))
    warm_start_enabled = bool(warm_start.get("enabled", False))
    warm_manifest: dict[str, Any] = {}
    warm_checkpoint_path: Path | None = None
    if warm_start_enabled:
        warm_checkpoint_path = Path(str(warm_start["checkpoint_path"])).expanduser().resolve()
        if not warm_checkpoint_path.is_file():
            raise RuntimeError(f"Warm-start checkpoint does not exist: {warm_checkpoint_path}")
        payload = torch.load(warm_checkpoint_path, map_location=device, weights_only=False)
        if not isinstance(payload, dict) or not isinstance(payload.get("manifest"), dict):
            raise RuntimeError("Warm-start checkpoint has no valid manifest.")
        warm_manifest = dict(payload["manifest"])
        expected_contract = {
            "scenario_type": str(cfg["scenario"]),
            "objective_mode": str(cfg["objective_mode"]),
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "return_estimator": "n_step_td",
            "reward_mode": "completed_product_td",
            "loss_type": "mse",
            "wait_action_enabled": bool(a["allow_wait_action"]),
            "worker_order_strategy": str(a["worker_order_strategy"]),
            "horizon_days": int(cfg["horizon_days"]),
        }
        mismatches = {
            key: {"expected": value, "actual": warm_manifest.get(key)}
            for key, value in expected_contract.items()
            if str(warm_manifest.get(key)) != str(value)
        }
        supported = sorted(int(value) for value in warm_manifest.get("supported_worker_counts", []))
        if supported != [training_worker_count]:
            mismatches["supported_worker_counts"] = {
                "expected": [training_worker_count],
                "actual": supported,
            }
        source_model_cfg = warm_manifest.get("model", {})
        for key in ("embedding_dim", "heads", "layers", "beam_width"):
            if int(source_model_cfg.get(key, -1)) != int(model_cfg[key]):
                mismatches[f"model.{key}"] = {
                    "expected": int(model_cfg[key]),
                    "actual": source_model_cfg.get(key),
                }
        if mismatches:
            raise RuntimeError("Warm-start checkpoint contract mismatch: " + json.dumps(mismatches, sort_keys=True))
        model.load_state_dict(payload["model_state_dict"])
        target_state = payload.get("target_model_state_dict")
        if target_state is None:
            raise RuntimeError("Warm-start checkpoint has no target-network state.")
        target.load_state_dict(target_state)
        optimizer_state = payload.get("optimizer_state_dict")
        if optimizer_state is None:
            raise RuntimeError("Warm-start checkpoint has no optimizer state.")
        optimizer.load_state_dict(optimizer_state)
        for group in optimizer.param_groups:
            group["lr"] = _piecewise_schedule_value(learning_rate_points, 0)
        del payload
    rng = random.Random(seed)
    episode_rows: list[dict[str, Any]] = []
    wave_rows: list[dict[str, Any]] = []
    iterations: list[dict[str, Any]] = []
    history = []
    fingerprint: dict[str, Any] | None = None
    started = time.perf_counter()
    summary: dict[str, Any] = {
        "status": "running", "return_estimator": "n_step_td", "n_step": int(a.get("n_step", 30)),
        "initial_return_estimator": "n_step_td", "target_network": True, "target_tau": float(t["target_tau"]),
        "target_policy_contract": "greedy_snapshot_per_update_truncate_on_mismatch",
        "reward_mode": "completed_product_td", "loss_type": "mse", "gamma": 1.0,
        "potential_shaping": False, "mc_targets_used_for_training": False,
        "conservative_validation_gate": False, "wait_action_enabled": bool(a["allow_wait_action"]),
        "worker_order_strategy": a["worker_order_strategy"], "horizon_days": cfg["horizon_days"],
        "worker_counts": cfg["worker_counts"], "configured_process_count": processes, "wave_size": wave_size,
        "training_device": str(device), "training_device_metadata": _training_device_metadata(torch, device),
        "determinism": determinism, "seed_partitions": partitions, "seed_overlap": False,
        "replay_capacity_episodes": int(t["replay_capacity_episodes"]),
        "replay_sample_episodes_per_update": int(t["replay_sample_episodes_per_update"]),
        "epsilon_schedule": [{"iteration": index, "value": value} for index, value in epsilon_points],
        "learning_rate_schedule": [
            {"iteration": index, "value": value} for index, value in learning_rate_points
        ],
        "initial_update_epochs": int(t["initial_update_epochs"]),
        "policy_update_epochs": int(t["max_epochs_per_iteration"]),
        "training_episode_count": 0, "validation_episode_count": 0,
        "expected_training_episode_count": total_episodes, "value_update_count": 0,
        "policy_iterations": int(t["policy_iterations"]), "screening_iterations": screening,
        "warm_start_enabled": warm_start_enabled,
        "warm_start_checkpoint": str(warm_checkpoint_path) if warm_checkpoint_path else None,
        "warm_start_checkpoint_id": warm_manifest.get("checkpoint_id") if warm_start_enabled else None,
        "warm_start_source_iteration": warm_manifest.get("iteration") if warm_start_enabled else None,
        "warm_start_replay_epsilon": (
            float(warm_start.get("replay_fill_epsilon", t["epsilon_start"]))
            if warm_start_enabled else None
        ),
        "training_rollout_sec": 0.0, "validation_sec": 0.0, "target_build_sec": 0.0,
        "update_sec": 0.0, "write_sec": 0.0, "peak_replay_mib": 0.0,
    }
    adp_cfg = {"_collect_td_replay": True, "beam_width": int(model_cfg["beam_width"]),
               "max_review_interval_min": float(t["max_review_interval_min"]),
               "allow_wait_action": bool(a["allow_wait_action"]), "worker_order_strategy": a["worker_order_strategy"]}
    episode_id, evaluation_id = 0, 1_000_000
    support_bank = None
    ood_cfg = cfg.get("diagnostics", {}).get("ood_support", {})
    best_mean, best_std, best_iteration = -math.inf, math.inf, -1
    screening_candidates: list[dict[str, Any]] = []
    progress = TrainingProgress(output)
    expected_validation = (len(screening) * len(partitions["screening_validation"])
                           + min(len(screening), int(v["final_candidate_count"]))
                           * len(partitions["final_selection_validation"]))
    progress.update(status="running", phase="starting", policy_iterations=total_iterations,
                    expected_episode_count=total_episodes + expected_validation,
                    completed_episode_count=0, completed_update_count=0,
                    training_device=str(device), process_count=processes)

    def write() -> Path:
        before = time.perf_counter()
        summary["wall_sec"] = time.perf_counter() - started
        summary["parent_peak_rss_mib"] = _process_rss_mib(peak=True)
        summary["phase_wave_counts"] = {
            phase: sum(r["phase"] == phase for r in wave_rows)
            for phase in (
                "initial_random", "warm_start_replay", "policy_iteration",
                "screening_validation", "final_selection_validation",
            )
        }
        _write_csv(output / "iteration_metrics.csv", iterations)
        _write_csv(output / "checkpoint_selection.csv", [
            {"iteration": r["iteration"], "screening_mean": r["validation_products_mean"],
             "screening_std": r["validation_products_std"],
             "final_candidate_rank": r.get("final_candidate_rank"),
             "final_mean": r.get("final_validation_products_mean"),
             "final_std": r.get("final_validation_products_std"),
             "selected": r["iteration"] == best_iteration}
            for r in iterations if "validation_products_mean" in r
        ])
        atomic_json(output / "training_summary.json", summary)
        path = render_td_dashboard(output, episode_rows, iterations, wave_rows, summary)
        summary["write_sec"] += time.perf_counter() - before
        return path

    def report_rollout(event: dict[str, Any]) -> None:
        progress.rollout(event)
        if event["event"] == "wave_completed":
            summary["training_episode_count"] = sum(
                row["phase"] in {"initial_random", "warm_start_replay", "policy_iteration"}
                for row in episode_rows
            )
            summary["validation_episode_count"] = len(episode_rows) - summary["training_episode_count"]
            write()

    def rollout(iteration: int, phase: str, seeds: list[int], epsilon: float, force_random: bool = False):
        nonlocal episode_id, evaluation_id, fingerprint
        training_phase = phase in {"initial_random", "warm_start_replay", "policy_iteration"}
        count_key = "training_episode_count" if training_phase else "validation_episode_count"
        previous_count = summary[count_key]
        previous_write_sec = summary["write_sec"]
        state, snapshot_hash = _snapshot_state_dict(None if force_random else model)
        jobs = []
        for i, episode_seed in enumerate(seeds):
            if training_phase:
                episode_id += 1
                current_id = episode_id
            else:
                evaluation_id += 1
                current_id = evaluation_id
            jobs.append(RolloutJob(
                episode=current_id, phase=phase, iteration=iteration,
                worker_count=training_worker_count,
                seed=episode_seed, days=int(cfg["horizon_days"]), epsilon=epsilon, force_random=force_random,
                adp_cfg=adp_cfg, collect_compact_samples=training_phase, gamma=1.0,
                wave_id=f"{phase}-I{iteration:02d}-W{i // wave_size + 1:02d}", snapshot_hash=snapshot_hash,
            ))
        results, batch, metrics = _run_rollout_jobs_parallel(
            jobs=jobs, model_state=state, model_cfg=model_cfg, process_count=processes, wave_size=wave_size,
            start_method="spawn", torch_threads=int(cfg["rollout"]["torch_threads_per_process"]),
            output_dir=output, episode_rows=episode_rows, wave_rows=wave_rows,
            progress_callback=report_rollout,
        )
        for result in results:
            if result.termination_reason != "completed_horizon":
                raise RuntimeError(f"TD episode {result.episode} ended early: {result.termination_reason}")
            if fingerprint is not None and fingerprint != result.fingerprint:
                raise RuntimeError("TD rollout environment fingerprint changed.")
            fingerprint = result.fingerprint
            if warm_start_enabled:
                source_environment = str(
                    warm_manifest.get("environment_fingerprints_by_worker_count", {}).get(
                        str(training_worker_count), warm_manifest.get("environment_fingerprint", "")
                    )
                )
                actual_environment = str(result.fingerprint.get("environment_fingerprint", ""))
                if source_environment != actual_environment:
                    raise RuntimeError(
                        "Warm-start environment fingerprint mismatch: "
                        f"source={source_environment}, rollout={actual_environment}."
                    )
        # Live rendering is already counted as write time, not simulation time.
        summary["training_rollout_sec" if training_phase else "validation_sec"] += max(
            0.0, metrics.wall_sec - (summary["write_sec"] - previous_write_sec)
        )
        summary[count_key] = previous_count + len(results)
        return results, batch

    write()
    try:
        for iteration in range(int(t["policy_iterations"]) + 1):
            initial = iteration == 0
            count = int(t["initial_random_episodes"] if initial else t["episodes_per_iteration"])
            epsilon = (
                float(warm_start.get("replay_fill_epsilon", t["epsilon_start"]))
                if initial and warm_start_enabled
                else 1.0 if initial
                else _piecewise_schedule_value(epsilon_points, iteration)
            )
            learning_rate = _piecewise_schedule_value(learning_rate_points, iteration)
            for group in optimizer.param_groups:
                group["lr"] = learning_rate
            initial_phase = "warm_start_replay" if warm_start_enabled else "initial_random"
            results, current = rollout(
                iteration,
                initial_phase if initial else "policy_iteration",
                list(range(seed + episode_id, seed + episode_id + count)),
                epsilon,
                force_random=initial and not warm_start_enabled,
            )
            history = retain_recent_episodes([*history, current], int(t["replay_capacity_episodes"]))
            replay = merge_compact_batches(history)
            current_episode_ids = {int(value) for value in current.episode_ids.tolist()}
            if initial:
                update_replay = replay
                update_epochs = int(t["initial_update_epochs"])
            else:
                update_replay = sample_replay_episodes(
                    replay,
                    required_episode_ids=current_episode_ids,
                    episode_count=int(t["replay_sample_episodes_per_update"]),
                    rng=rng,
                )
                update_epochs = int(t["max_epochs_per_iteration"])
            update_episode_ids = {int(value) for value in update_replay.episode_ids.tolist()}
            row: dict[str, Any] = {
                "iteration": iteration, "epsilon": epsilon, "learning_rate": learning_rate,
                "batch_episode_count": count,
                "rollout_products_mean": statistics.fmean(r.products for r in results),
                "rollout_products_std": statistics.stdev(r.products for r in results) if len(results) > 1 else 0.0,
                "replay_episode_count": replay.episode_count, "replay_sample_count": len(replay),
                "replay_mib": replay.memory_bytes / 1024 ** 2,
                "update_episode_count": update_replay.episode_count,
                "update_current_episode_count": len(update_episode_ids & current_episode_ids),
                "update_history_episode_count": len(update_episode_ids - current_episode_ids),
                "update_sample_count": len(update_replay),
                "update_mib": update_replay.memory_bytes / 1024 ** 2,
                "update_epochs_requested": update_epochs,
                "decision_count_mean": statistics.fmean(r.decisions for r in results),
                "voluntary_wait_count": sum(r.candidate_available_wait_count for r in results),
                "beam_entropy": statistics.fmean(r.beam_value_entropy_avg for r in results),
            }
            summary["peak_replay_mib"] = max(summary["peak_replay_mib"], row["replay_mib"])
            progress.update(phase="diagnostics", iteration=iteration)
            if ood_cfg.get("enabled", False):
                ood = evaluate_ood_selected_actions(model, current, support_bank, device=device,
                     batch_size=int(t["batch_size"]), seed=seed + iteration,
                     evaluation_samples=int(ood_cfg.get("evaluation_samples", 4096)))
                row.update({"ood_selection_rate": ood["ood_selection_rate"],
                            "ood_overestimation_excess": ood["ood_overestimation_excess"]})
                support_bank = build_ood_support_bank(current, device=device, seed=seed + iteration,
                    reference_samples=int(ood_cfg.get("reference_samples", 1024)),
                    calibration_samples=int(ood_cfg.get("calibration_samples", 1024)),
                    quantile=float(ood_cfg.get("quantile", .95)))
            if update_epochs > 0:
                progress.update(phase="target_build", iteration=iteration)
                before = time.perf_counter()
                plan = build_target_plan(update_replay, model, device, n=int(a.get("n_step", 30)),
                                          beam_width=int(model_cfg["beam_width"]), worker_order_strategy=a["worker_order_strategy"])
                row["target_build_sec"] = time.perf_counter() - before
                summary["target_build_sec"] += row["target_build_sec"]
                if device.type == "cuda":
                    torch.cuda.synchronize(device)
                    torch.cuda.reset_peak_memory_stats(device)
                before = time.perf_counter()
                progress.update(phase="value_update", iteration=iteration)
                row.update(fit_td_value(model=model, target_model=target, optimizer=optimizer, samples=update_replay,
                                        plan=plan, device=device, batch_size=int(t["batch_size"]),
                                        epochs=update_epochs, gradient_clip=float(t["gradient_clip"]),
                                        target_tau=float(t["target_tau"]), rng=rng))
                if device.type == "cuda":
                    torch.cuda.synchronize(device)
                row["update_sec"] = time.perf_counter() - before
                row["gpu_peak_allocated_mib"] = torch.cuda.max_memory_allocated(device) / 1024 ** 2 if device.type == "cuda" else 0.0
                summary["update_sec"] += row["update_sec"]
                summary["value_update_count"] += 1
                del plan
            else:
                row.update({"target_build_sec": 0.0, "update_sec": 0.0, "gpu_peak_allocated_mib": 0.0,
                            "epochs": 0, "sgd_steps": 0})
            del update_replay, replay, current
            gc.collect()
            if iteration in screening:
                validation, _ = rollout(iteration, "screening_validation", partitions["screening_validation"], 0.0)
                products = [r.products for r in validation]
                mean, std = statistics.fmean(products), statistics.stdev(products) if len(products) > 1 else 0.0
                num = sum(r.greedy_mc_sample_count for r in validation)
                row.update({"validation_products_mean": mean, "validation_products_std": std,
                            "validation_episode_count": len(products), "greedy_mc_sample_count": num,
                            "greedy_mc_rmse": math.sqrt(sum(r.greedy_mc_squared_error_sum for r in validation) / num) if num else None,
                            "greedy_mc_bias": sum(r.greedy_mc_error_sum for r in validation) / num if num else None,
                            "greedy_mc_prediction_mean": sum(r.greedy_mc_prediction_sum for r in validation) / num if num else None,
                            "greedy_mc_target_mean": sum(r.greedy_mc_target_sum for r in validation) / num if num else None})
            progress.update(phase="checkpoint", iteration=iteration)
            manifest = {**(fingerprint or {}), "return_estimator": "n_step_td",
                "checkpoint_id": f"ADP-TD-{output.name}-I{iteration:02d}", "iteration": iteration,
                "supported_worker_counts": [training_worker_count],
                "worker_count_range": [training_worker_count, training_worker_count],
                "environment_fingerprints_by_worker_count": {
                    str(training_worker_count): fingerprint["environment_fingerprint"]
                },
                "model": model_cfg, "horizon_days": cfg["horizon_days"],
                "seed_partitions": partitions, "seed_overlap": False,
                "training": {**t, **a, "mc_return_only": False, "target_network": True,
                              "initial_return_estimator": "n_step_td", "conservative_validation_gate": False},
                "warm_start": dict(warm_start) if warm_start_enabled else {"enabled": False}}
            save_checkpoint(output / "checkpoints" / f"iteration_{iteration:03d}.pt", model=model,
                            optimizer=None, manifest=manifest)
            save_checkpoint(output / "last.pt", model=model, optimizer=optimizer, target_model=target, manifest=manifest)
            if iteration in screening:
                screening_candidates.append({
                    "iteration": iteration,
                    "screening_mean": float(row["validation_products_mean"]),
                    "screening_std": float(row["validation_products_std"]),
                    "model": copy.deepcopy(model.state_dict()),
                    "target": copy.deepcopy(target.state_dict()),
                    "optimizer": copy.deepcopy(optimizer.state_dict()),
                    "manifest": manifest,
                })
                screening_candidates.sort(
                    key=lambda candidate: (
                        -candidate["screening_mean"],
                        candidate["screening_std"],
                        candidate["iteration"],
                    )
                )
                del screening_candidates[int(v["final_candidate_count"]):]
                best_mean = screening_candidates[0]["screening_mean"]
                best_std = screening_candidates[0]["screening_std"]
                best_iteration = screening_candidates[0]["iteration"]
            summary.update({"best_iteration": best_iteration, "best_screening_mean": best_mean})
            iterations.append(row)
            progress.update(completed_update_count=summary["value_update_count"])
            write()
            if update_epochs > 0:
                print(f"TD I{iteration:02d}: rollout={row['rollout_products_mean']:.2f}, "
                      f"TD MSE={row['td_train_mse']:.3f}, effective n={row['effective_n_mean']:.2f}", flush=True)
            else:
                print(f"TD I{iteration:02d}: warm-start replay={row['rollout_products_mean']:.2f}, "
                      "source checkpoint unchanged", flush=True)
        if not screening_candidates:
            raise RuntimeError("No validated TD checkpoint was produced.")
        final_candidates: list[dict[str, Any]] = []
        for rank, candidate in enumerate(screening_candidates, start=1):
            model.load_state_dict(candidate["model"])
            target.load_state_dict(candidate["target"])
            optimizer.load_state_dict(candidate["optimizer"])
            final, _ = rollout(
                candidate["iteration"],
                "final_selection_validation",
                partitions["final_selection_validation"],
                0.0,
            )
            final_mean = statistics.fmean(result.products for result in final)
            final_std = statistics.stdev(result.products for result in final) if len(final) > 1 else 0.0
            candidate.update({
                "screening_rank": rank,
                "final_mean": final_mean,
                "final_std": final_std,
            })
            final_candidates.append(candidate)
            for row in iterations:
                if int(row["iteration"]) == int(candidate["iteration"]):
                    row.update({
                        "final_candidate_rank": rank,
                        "final_validation_products_mean": final_mean,
                        "final_validation_products_std": final_std,
                    })
                    break
        final_candidates.sort(
            key=lambda candidate: (
                -candidate["final_mean"],
                candidate["final_std"],
                candidate["iteration"],
            )
        )
        best_payload = final_candidates[0]
        best_iteration = int(best_payload["iteration"])
        final_mean = float(best_payload["final_mean"])
        final_std = float(best_payload["final_std"])
        model.load_state_dict(best_payload["model"])
        target.load_state_dict(best_payload["target"])
        optimizer.load_state_dict(best_payload["optimizer"])
        manifest = {**best_payload["manifest"], "final_validation_completed_products_avg": final_mean,
                    "validation_completed_products_avg": final_mean,
                    "validation_completed_products_std": final_std}
        save_checkpoint(output / "best.pt", model=model, optimizer=optimizer, target_model=target, manifest=manifest)
        (output / "checkpoint_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        summary.update({"status": "completed", "best_iteration": best_iteration,
                        "best_validation_completed_products_avg": final_mean,
                        "best_validation_completed_products_std": final_std,
                        "final_candidate_results": [
                            {
                                "iteration": int(candidate["iteration"]),
                                "screening_rank": int(candidate["screening_rank"]),
                                "screening_mean": float(candidate["screening_mean"]),
                                "screening_std": float(candidate["screening_std"]),
                                "final_mean": float(candidate["final_mean"]),
                                "final_std": float(candidate["final_std"]),
                                "selected": candidate is best_payload,
                            }
                            for candidate in final_candidates
                        ],
                        "best_checkpoint_path": str(output / "best.pt")})
    except BaseException as exc:
        summary.update({"status": "failed", "failure": f"{type(exc).__name__}: {exc}"})
        progress.update(status="failed", error=summary["failure"])
        write()
        raise
    path = write()
    progress.update(status="completed", phase="completed", iteration=total_iterations)
    if not args.no_open_dashboard and cfg["runtime"].get("auto_open_dashboard", True):
        webbrowser.open(path.as_uri())
    return path
