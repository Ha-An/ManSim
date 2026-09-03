from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import webbrowser
from collections import defaultdict
from pathlib import Path
from typing import Any

from omegaconf import OmegaConf

from .model import build_value_network, require_torch
from .train import (
    RolloutJob,
    _linear,
    _mean_and_std,
    _run_rollout_jobs_parallel,
    _snapshot_state_dict,
    _validate_seed_partitions,
    _write_csv,
    render_training_dashboard,
)


def _read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _screening_rows(output_dir: Path, torch: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted((output_dir / "checkpoints").glob("selection_*.pt")):
        payload = torch.load(path, map_location="cpu", weights_only=False)
        manifest = payload.get("manifest", {})
        iteration = int(path.stem.rsplit("_", 1)[1])
        rows.append(
            {
                "iteration": iteration,
                "screening_mean_products": float(
                    manifest.get("validation_completed_products_avg", 0.0)
                ),
                "screening_std_products": float(
                    manifest.get("validation_completed_products_std", 0.0)
                ),
                "screening_seed_count": int(
                    manifest.get("training", {}).get("screening_seed_count", 0)
                ),
                "iteration_checkpoint": str(
                    (output_dir / "checkpoints" / f"iteration_{iteration:03d}.pt").resolve()
                ),
                "selection_checkpoint": str(path.resolve()),
            }
        )
    if not rows:
        for persisted in _read_csv(output_dir / "checkpoint_selection.csv"):
            iteration = int(float(persisted["iteration"]))
            rows.append(
                {
                    "iteration": iteration,
                    "screening_mean_products": float(persisted["screening_mean_products"]),
                    "screening_std_products": float(persisted["screening_std_products"]),
                    "screening_seed_count": int(float(persisted["screening_seed_count"])),
                    "iteration_checkpoint": str(
                        (output_dir / "checkpoints" / f"iteration_{iteration:03d}.pt").resolve()
                    ),
                    "selection_checkpoint": str(
                        (output_dir / "checkpoints" / f"iteration_{iteration:03d}.pt").resolve()
                    ),
                }
            )
    return sorted(
        rows,
        key=lambda row: (
            -float(row["screening_mean_products"]),
            float(row["screening_std_products"]),
            int(row["iteration"]),
        ),
    )


def _aggregate_wave_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    wall = sum(float(row.get("wall_sec", 0.0)) for row in rows)
    elapsed = sum(float(row.get("episode_elapsed_sum_sec", 0.0)) for row in rows)
    slot_capacity = sum(
        float(row.get("wall_sec", 0.0))
        * max(1, int(float(row.get("active_process_count", 1))))
        for row in rows
    )
    return {
        "wall_sec": wall,
        "wave_count": float(len(rows)),
        "episodes_per_hour": (
            sum(int(float(row.get("episode_count", 0))) for row in rows) * 3600.0 / wall
            if wall > 0
            else 0.0
        ),
        "speedup": elapsed / wall if wall > 0 else 0.0,
        "efficiency": elapsed / slot_capacity if slot_capacity > 0 else 0.0,
        "utilization": elapsed / slot_capacity if slot_capacity > 0 else 0.0,
    }


def _reconstruct_iteration_rows(
    *,
    cfg: dict[str, Any],
    episode_rows: list[dict[str, Any]],
    wave_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    training_cfg = cfg["training"]
    policy_iterations = int(training_cfg["policy_iterations"])
    screening_by_iteration: dict[int, list[float]] = defaultdict(list)
    for row in episode_rows:
        if str(row.get("phase", "")) == "screening_validation":
            screening_by_iteration[int(float(row["iteration"]))].append(float(row["products"]))
    rows: list[dict[str, Any]] = []
    for iteration in range(policy_iterations + 1):
        phase = "initial_random" if iteration == 0 else f"policy_iteration_{iteration}"
        episode_group = [
            row
            for row in episode_rows
            if str(row.get("phase", "")) == phase
            and int(float(row.get("iteration", 0))) == iteration
        ]
        wave_group = [row for row in wave_rows if str(row.get("phase", "")) == phase]
        wave_metrics = _aggregate_wave_metrics(wave_group)
        validation_values = screening_by_iteration.get(iteration, [])
        validation_mean, validation_std = _mean_and_std(validation_values)
        validation_performed = bool(validation_values)
        compact_mib = sum(float(row.get("compact_memory_mib", 0.0)) for row in episode_group)
        row = {
            "iteration": iteration,
            "phase": phase,
            "mc_loss": 0.0,
            "mc_mae": 0.0,
            "mc_rmse": 0.0,
            "prediction_std": 0.0,
            "target_std": 0.0,
            "fit_diagnostics_available": False,
            "epochs": "",
            "batch_episode_count": len(episode_group),
            "sample_count": sum(
                int(float(item.get("compact_sample_count", 0))) for item in episode_group
            ),
            "compact_batch_mib": compact_mib,
            "process_rss_mib": 0.0,
            "epsilon": 1.0
            if iteration == 0
            else _linear(
                float(training_cfg["epsilon_start"]),
                float(training_cfg["epsilon_end"]),
                iteration - 1,
                policy_iterations,
            ),
            "rollout_wall_sec": wave_metrics["wall_sec"],
            "rollout_wave_count": int(wave_metrics["wave_count"]),
            "rollout_episodes_per_hour": wave_metrics["episodes_per_hour"],
            "rollout_effective_speedup": wave_metrics["speedup"],
            "rollout_parallel_efficiency": wave_metrics["efficiency"],
            "rollout_active_slot_utilization": wave_metrics["utilization"],
            "rollout_merge_sec": 0.0,
            "mc_update_sec": 0.0,
            "gpu_memory_allocated_mib": 0.0,
            "gpu_memory_reserved_mib": 0.0,
            "gpu_peak_allocated_mib": 0.0,
            "gpu_peak_reserved_mib": 0.0,
            "validation_wall_sec": sum(
                float(item.get("wall_sec", 0.0))
                for item in wave_rows
                if str(item.get("phase", "")) == "screening_validation"
                and int(float(item.get("iteration", 0))) == iteration
            ),
            "validation_wave_count": sum(
                1
                for item in wave_rows
                if str(item.get("phase", "")) == "screening_validation"
                and int(float(item.get("iteration", 0))) == iteration
            ),
            "validation_performed": validation_performed,
            "validation_products_avg": validation_mean if validation_performed else "",
            "validation_products_std": validation_std if validation_performed else "",
            "validation_products_ci95_low": (
                validation_mean - 1.96 * validation_std / math.sqrt(len(validation_values))
                if validation_performed
                else ""
            ),
            "validation_products_ci95_high": (
                validation_mean + 1.96 * validation_std / math.sqrt(len(validation_values))
                if validation_performed
                else ""
            ),
            "validation_stage": "screening" if validation_performed else "",
            "validation_products_avg_workers_3": validation_mean if validation_performed else "",
        }
        rows.append(row)
    return rows


def finalize(config_path: Path, output_dir: Path, *, open_dashboard: bool) -> Path:
    torch = require_torch()
    cfg = OmegaConf.to_container(OmegaConf.load(config_path), resolve=True)
    if not isinstance(cfg, dict):
        raise ValueError("ADP config must be a YAML mapping.")
    episode_rows = _read_csv(output_dir / "episode_metrics.csv")
    wave_rows = _read_csv(output_dir / "wave_metrics.csv")
    screening_rows = _screening_rows(output_dir, torch)
    final_candidate_count = int(cfg["validation"]["final_candidate_count"])
    selected_screening = screening_rows[:final_candidate_count]
    final_seeds = [int(value) for value in cfg["seed_partitions"]["final_selection_validation"]]
    training_episode_target = int(cfg["training"]["initial_random_episodes"]) + int(
        cfg["training"]["policy_iterations"]
    ) * int(cfg["training"]["episodes_per_iteration"])
    training_seeds = [int(cfg["base_seed"]) + index for index in range(training_episode_target)]
    screening_seeds = [int(value) for value in cfg["seed_partitions"]["screening_validation"]]
    held_out_seeds = [int(value) for value in cfg["seed_partitions"].get("held_out_test", [])]
    seed_partition_meta = _validate_seed_partitions(
        training_seeds=training_seeds,
        screening_seeds=screening_seeds,
        final_selection_seeds=final_seeds,
        held_out_seeds=held_out_seeds,
        enforce_disjoint=bool(cfg["seed_partitions"].get("enforce_disjoint", False)),
    )
    model_cfg = cfg["model"]
    rollout_cfg = cfg["rollout"]
    allow_wait_action = bool(cfg.get("algorithm", {}).get("allow_wait_action", False))
    random_policy_name = (
        "uniform_random_feasible_with_wait"
        if allow_wait_action
        else "uniform_random_feasible_no_wait"
    )
    adp_runtime_cfg = {
        "beam_width": int(model_cfg["beam_width"]),
        "max_review_interval_min": float(cfg["training"].get("max_review_interval_min", 1.0)),
        "allow_wait_action": allow_wait_action,
    }
    next_episode = max((int(float(row.get("episode", 0))) for row in episode_rows), default=1_000_000) + 1
    selection_payloads: dict[int, dict[str, Any]] = {}
    selection_rows: list[dict[str, Any]] = []
    for rank, screening in enumerate(selected_screening, start=1):
        iteration = int(screening["iteration"])
        checkpoint = Path(str(screening["selection_checkpoint"]))
        payload = torch.load(checkpoint, map_location="cpu", weights_only=False)
        selection_payloads[iteration] = payload
        existing = {
            int(float(row["seed"]))
            for row in episode_rows
            if str(row.get("phase", "")) == "final_selection_validation"
            and int(float(row.get("iteration", -1))) == iteration
        }
        missing = [seed for seed in final_seeds if seed not in existing]
        if missing:
            model = build_value_network(
                embedding_dim=int(model_cfg["embedding_dim"]),
                heads=int(model_cfg["heads"]),
                layers=int(model_cfg["layers"]),
            )
            model.load_state_dict(payload["model_state_dict"])
            model.eval()
            model_state, snapshot_hash = _snapshot_state_dict(model)
            jobs: list[RolloutJob] = []
            for index, seed in enumerate(missing):
                jobs.append(
                    RolloutJob(
                        episode=next_episode + index,
                        phase="final_selection_validation",
                        iteration=iteration,
                        worker_count=3,
                        seed=seed,
                        days=int(cfg["horizon_days"]),
                        epsilon=0.0,
                        force_random=False,
                        adp_cfg=adp_runtime_cfg,
                        collect_compact_samples=False,
                        gamma=float(cfg["training"].get("gamma", 1.0)),
                        wave_id=(
                            f"final_selection_validation-I{iteration:02d}-"
                            f"W{index // int(rollout_cfg['wave_size']) + 1:02d}"
                        ),
                        snapshot_hash=snapshot_hash,
                    )
                )
            results, compact, _metrics = _run_rollout_jobs_parallel(
                jobs=jobs,
                model_state=model_state,
                model_cfg=model_cfg,
                process_count=int(rollout_cfg["process_count"]),
                wave_size=int(rollout_cfg["wave_size"]),
                start_method=str(rollout_cfg["start_method"]),
                torch_threads=int(rollout_cfg["torch_threads_per_process"]),
                output_dir=output_dir,
                episode_rows=episode_rows,
                wave_rows=wave_rows,
            )
            if len(compact):
                raise RuntimeError("Final-selection recovery unexpectedly retained compact samples.")
            next_episode += len(results)
        final_values = [
            float(row["products"])
            for row in episode_rows
            if str(row.get("phase", "")) == "final_selection_validation"
            and int(float(row.get("iteration", -1))) == iteration
            and int(float(row["seed"])) in set(final_seeds)
        ]
        if len(final_values) != len(final_seeds):
            raise RuntimeError(
                f"Iteration {iteration} final selection is incomplete: "
                f"{len(final_values)}/{len(final_seeds)} seeds."
            )
        final_mean, final_std = _mean_and_std(final_values)
        selection_rows.append(
            {
                **screening,
                "screening_rank": rank,
                "final_mean_products": round(final_mean, 6),
                "final_std_products": round(final_std, 6),
                "final_seed_count": len(final_values),
                "final_ci95_low": round(
                    final_mean - 1.96 * final_std / math.sqrt(len(final_values)), 6
                ),
                "final_ci95_high": round(
                    final_mean + 1.96 * final_std / math.sqrt(len(final_values)), 6
                ),
                "final_validation_wall_sec": sum(
                    float(row.get("wall_sec", 0.0))
                    for row in wave_rows
                    if str(row.get("phase", "")) == "final_selection_validation"
                    and int(float(row.get("iteration", -1))) == iteration
                ),
            }
        )
    winner = sorted(
        selection_rows,
        key=lambda row: (
            -float(row["final_mean_products"]),
            float(row["final_std_products"]),
            int(row["iteration"]),
        ),
    )[0]
    best_iteration = int(winner["iteration"])
    best_payload = selection_payloads[best_iteration]
    best_manifest = dict(best_payload["manifest"])
    best_manifest["validation_completed_products_avg"] = float(winner["final_mean_products"])
    best_manifest["validation_completed_products_std"] = float(winner["final_std_products"])
    best_manifest["checkpoint_selection"] = {
        "stage": "final_selection",
        "screening_rank": int(winner["screening_rank"]),
        "best_iteration": best_iteration,
        "candidate_count": len(selection_rows),
        "selection_rule": "max_mean_then_min_std_then_earliest_iteration",
        "recovered_after_interruption": True,
    }
    training_meta = dict(best_manifest.get("training", {}))
    completed_waves = [row for row in wave_rows if str(row.get("status", "")) == "completed"]
    phase_wave_counts = {
        phase: sum(1 for row in completed_waves if str(row.get("phase", "")).startswith(phase))
        for phase in (
            "initial_random",
            "policy_iteration",
            "screening_validation",
            "final_selection_validation",
        )
    }
    training_meta.update(
        {
            "initial_random_episodes": int(cfg["training"]["initial_random_episodes"]),
            "policy_iterations": int(cfg["training"]["policy_iterations"]),
            "episodes_per_iteration": int(cfg["training"]["episodes_per_iteration"]),
            "training_episode_count": int(cfg["training"]["initial_random_episodes"])
            + int(cfg["training"]["policy_iterations"])
            * int(cfg["training"]["episodes_per_iteration"]),
            "best_iteration": best_iteration,
            "phase_wave_counts": phase_wave_counts,
            "recovered_after_interruption": True,
        }
    )
    best_manifest["training"] = training_meta
    best_manifest["seed_partitions"] = seed_partition_meta
    torch.save(
        {
            "model_state_dict": best_payload["model_state_dict"],
            "optimizer_state_dict": best_payload.get("optimizer_state_dict"),
            "manifest": best_manifest,
        },
        output_dir / "best.pt",
    )
    for row in selection_rows:
        row["selected_best"] = int(row["iteration"]) == best_iteration
        row.pop("selection_checkpoint", None)
        row["temporary_optimizer_checkpoint_removed"] = True
    _write_csv(output_dir / "checkpoint_selection.csv", selection_rows)
    iteration_rows = _reconstruct_iteration_rows(
        cfg=cfg, episode_rows=episode_rows, wave_rows=wave_rows
    )
    _write_csv(output_dir / "iteration_metrics.csv", iteration_rows)
    _write_csv(output_dir / "episode_metrics.csv", episode_rows)
    _write_csv(output_dir / "wave_metrics.csv", wave_rows)
    total_wave_wall = sum(float(row.get("wall_sec", 0.0)) for row in completed_waves)
    total_episode_elapsed = sum(float(row.get("elapsed_sec", 0.0)) for row in episode_rows)
    process_count = int(rollout_cfg["process_count"])
    total_slot_capacity = sum(
        float(row.get("wall_sec", 0.0))
        * max(1, int(float(row.get("active_process_count", 1))))
        for row in completed_waves
    )
    screening_iterations = sorted(
        {
            int(float(row.get("iteration", 0)))
            for row in episode_rows
            if str(row.get("phase", "")) == "screening_validation"
        }
    )
    peak_compact_batch_mib = max(
        (float(row.get("compact_batch_mib", 0.0)) for row in iteration_rows),
        default=0.0,
    )
    peak_child_rss_mib = max(
        (float(row.get("child_peak_rss_mib", 0.0)) for row in episode_rows),
        default=0.0,
    )
    resolved_training_device = str(
        training_meta.get("training_device", {}).get(
            "resolved_device", cfg["runtime"].get("device", "unknown")
        )
    )
    summary = {
        "training_episode_count": training_meta["training_episode_count"],
        "validation_episode_count": sum(
            1 for row in episode_rows if "validation" in str(row.get("phase", ""))
        ),
        "screening_validation_episode_count": sum(
            1 for row in episode_rows if str(row.get("phase", "")) == "screening_validation"
        ),
        "final_selection_validation_episode_count": sum(
            1
            for row in episode_rows
            if str(row.get("phase", "")) == "final_selection_validation"
        ),
        "worker_counts": [3],
        "horizon_days": int(cfg["horizon_days"]),
        "policy_iterations": int(cfg["training"]["policy_iterations"]),
        "episodes_per_iteration": int(cfg["training"]["episodes_per_iteration"]),
        "loss_type": "mse",
        "reward_mode": "completed_product_mc",
        "potential_shaping": False,
        "random_policy": random_policy_name,
        "wait_action_enabled": allow_wait_action,
        "learning_rate": float(cfg["training"]["learning_rate"]),
        "max_epochs_per_iteration": int(cfg["training"]["max_epochs_per_iteration"]),
        "epsilon_start": float(cfg["training"]["epsilon_start"]),
        "epsilon_end": float(cfg["training"]["epsilon_end"]),
        "screening_interval_iterations": int(
            cfg["validation"]["screening_interval_iterations"]
        ),
        "screening_iterations": screening_iterations,
        "final_candidate_count": len(selection_rows),
        "best_iteration": best_iteration,
        "best_validation_completed_products_avg": float(winner["final_mean_products"]),
        "best_validation_completed_products_std": float(winner["final_std_products"]),
        "best_checkpoint": str((output_dir / "best.pt").resolve()),
        "last_checkpoint": str((output_dir / "last.pt").resolve()),
        "checkpoint_selection_file": str((output_dir / "checkpoint_selection.csv").resolve()),
        "configured_process_count": process_count,
        "actual_process_count_max": max(
            (int(float(row.get("actual_process_count", 0))) for row in completed_waves), default=0
        ),
        "wave_size": int(rollout_cfg["wave_size"]),
        "multiprocessing_start_method": str(rollout_cfg["start_method"]),
        "rollout_device": str(rollout_cfg["device"]),
        "training_device": resolved_training_device,
        "training_device_requested": str(cfg["runtime"]["device"]),
        "require_cuda_training": bool(cfg["runtime"]["require_cuda_training"]),
        "training_device_metadata": training_meta.get("training_device", {}),
        "runtime_environment": training_meta.get("runtime_environment", {}),
        "torch_threads_per_process": int(rollout_cfg["torch_threads_per_process"]),
        "total_wave_count": len(completed_waves),
        "phase_wave_counts": phase_wave_counts,
        "seed_partitions": seed_partition_meta,
        "peak_compact_batch_mib": peak_compact_batch_mib,
        "observed_peak_process_rss_mib": 0.0,
        "peak_child_rss_mib": peak_child_rss_mib,
        "training_rollout_sec": sum(
            float(row.get("wall_sec", 0.0))
            for row in completed_waves
            if str(row.get("phase", "")).startswith(("initial_random", "policy_iteration"))
        ),
        "validation_wall_sec": sum(
            float(row.get("wall_sec", 0.0))
            for row in completed_waves
            if "validation" in str(row.get("phase", ""))
        ),
        "compact_merge_sec": 0.0,
        "mc_update_sec": 0.0,
        "total_episode_elapsed_sec": total_episode_elapsed,
        "total_wave_wall_sec": total_wave_wall,
        "effective_speedup": total_episode_elapsed / total_wave_wall,
        "parallel_efficiency": total_episode_elapsed / max(1e-9, total_slot_capacity),
        "active_slot_utilization": total_episode_elapsed / max(1e-9, total_slot_capacity),
        "episodes_per_hour": len(episode_rows) * 3600.0 / total_wave_wall,
        "failed_wave_count": sum(
            1 for row in wave_rows if str(row.get("status", "")) != "completed"
        ),
        "cancelled_episode_count": 0,
        "retried_episode_count": 0,
        "ipc_payload_mib": sum(
            float(row.get("ipc_payload_mib", 0.0)) for row in completed_waves
        ),
        "coordination_ipc_overhead_sec": sum(
            float(row.get("coordination_ipc_overhead_sec", 0.0))
            for row in completed_waves
        ),
        "mc_return_only": True,
        "n_step_used": False,
        "td_bootstrap_used": False,
        "recovered_after_interruption": True,
        "fit_diagnostics_available": False,
        "update_timing_available": False,
        "gpu_memory_diagnostics_available": False,
        "process_memory_diagnostics_available": False,
        "wall_clock_complete": False,
        "recovery_note": (
            "Final selection resumed from saved iteration checkpoints. Per-update MSE/MAE/RMSE "
            "and GPU update timing/memory were process-local at interruption time and are "
            "unavailable in the recovered dashboard. GPU training still occurred on cuda:0."
        ),
        "total_wall_clock_sec": total_wave_wall,
    }
    (output_dir / "checkpoint_manifest.json").write_text(
        json.dumps(best_manifest, indent=2), encoding="utf-8"
    )
    (output_dir / "training_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    dashboard = render_training_dashboard(
        output_dir, episode_rows, iteration_rows, wave_rows, summary
    )
    if open_dashboard:
        webbrowser.open(dashboard.resolve().as_uri())
    return dashboard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resume final checkpoint selection after an interrupted ADP training run."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--no-open-dashboard", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    dashboard = finalize(
        args.config.resolve(),
        args.output.resolve(),
        open_dashboard=not args.no_open_dashboard,
    )
    print(dashboard.resolve())


if __name__ == "__main__":
    main()
