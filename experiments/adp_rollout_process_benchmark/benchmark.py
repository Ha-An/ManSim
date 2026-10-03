from __future__ import annotations

import argparse
import csv
import gc
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from manufacturing_sim.adp.model import build_value_network, require_torch
from manufacturing_sim.adp.train import (
    RolloutJob,
    _run_rollout_jobs_parallel,
    _snapshot_state_dict,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark ADP rollout throughput with a fixed episode workload."
    )
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument(
        "--process-counts",
        nargs="+",
        type=int,
        default=[1, 2, 5, 10, 15, 20],
    )
    parser.add_argument("--episodes", type=int, default=20)
    parser.add_argument(
        "--single-wave",
        action="store_true",
        help="Run exactly one episode per process, so every setting consists of one wave.",
    )
    parser.add_argument("--worker-count", type=int, default=3)
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--seed-start", type=int, default=70001)
    parser.add_argument("--epsilon", type=float, default=0.20)
    parser.add_argument("--torch-threads", type=int, default=1)
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "outputs" / "adp_rollout_process_benchmark",
    )
    return parser.parse_args()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    process_counts = [int(value) for value in args.process_counts]
    if any(value < 1 for value in process_counts):
        raise ValueError("All process counts must be positive.")
    if args.episodes < 1:
        raise ValueError("episodes must be positive.")

    torch = require_torch()
    checkpoint_path = args.checkpoint.expanduser().resolve()
    payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    manifest = dict(payload.get("manifest", {}))
    model_cfg = dict(manifest.get("model", {}))
    model = build_value_network(
        embedding_dim=int(model_cfg["embedding_dim"]),
        heads=int(model_cfg["heads"]),
        layers=int(model_cfg["layers"]),
    ).cpu()
    model.load_state_dict(payload["model_state_dict"])
    model.eval()
    model_state, snapshot_hash = _snapshot_state_dict(model)
    del model
    del payload
    gc.collect()

    training_contract = dict(manifest.get("training", {}))
    adp_cfg = {
        "beam_width": int(model_cfg["beam_width"]),
        "max_review_interval_min": float(
            training_contract.get("max_review_interval_min", 1.0)
        ),
        "allow_wait_action": bool(manifest.get("wait_action_enabled", False)),
        "worker_order_strategy": str(
            manifest.get("worker_order_strategy", "cyclic")
        ),
    }
    supported = [int(value) for value in manifest.get("supported_worker_counts", [])]
    if supported and int(args.worker_count) not in supported:
        raise ValueError(
            f"Checkpoint does not support worker_count={args.worker_count}; supported={supported}."
        )

    output_dir = args.output.resolve() / datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=False)
    summary_rows: list[dict[str, Any]] = []
    reference_results: dict[int, tuple[int, int]] = {}

    for process_count in process_counts:
        episode_count = process_count if args.single_wave else int(args.episodes)
        run_dir = output_dir / f"processes_{process_count}"
        run_dir.mkdir(parents=True, exist_ok=False)
        episode_rows: list[dict[str, Any]] = []
        wave_rows: list[dict[str, Any]] = []
        jobs = [
            RolloutJob(
                episode=index + 1,
                phase="process_benchmark",
                iteration=0,
                worker_count=int(args.worker_count),
                seed=int(args.seed_start) + index,
                days=int(args.days),
                epsilon=float(args.epsilon),
                force_random=False,
                adp_cfg=adp_cfg,
                collect_compact_samples=True,
                gamma=1.0,
                wave_id=(
                    f"process_benchmark-P{process_count:02d}-"
                    f"W{index // process_count + 1:02d}"
                ),
                snapshot_hash=snapshot_hash,
            )
            for index in range(episode_count)
        ]

        started = time.perf_counter()
        results, compact_batch, metrics = _run_rollout_jobs_parallel(
            jobs=jobs,
            model_state=model_state,
            model_cfg=model_cfg,
            process_count=process_count,
            wave_size=process_count,
            start_method="spawn",
            torch_threads=int(args.torch_threads),
            output_dir=run_dir,
            episode_rows=episode_rows,
            wave_rows=wave_rows,
        )
        measured_wall_sec = time.perf_counter() - started
        products = [int(result.products) for result in results]
        decisions = [int(result.decisions) for result in results]
        deterministic_match = all(
            result.seed not in reference_results
            or reference_results[result.seed] == (int(result.products), int(result.decisions))
            for result in results
        )
        reference_results.update(
            {
                int(result.seed): (int(result.products), int(result.decisions))
                for result in results
            }
        )
        summary_rows.append(
            {
                "process_count": process_count,
                "episode_count": len(results),
                "wave_count": metrics.wave_count,
                "wall_sec": round(measured_wall_sec, 6),
                "wall_min": round(measured_wall_sec / 60.0, 6),
                "episodes_per_hour": round(
                    len(results) * 3600.0 / max(measured_wall_sec, 1e-9), 6
                ),
                "episode_elapsed_avg_sec": round(metrics.episode_elapsed_avg_sec, 6),
                "episode_elapsed_sum_sec": round(metrics.episode_elapsed_sum_sec, 6),
                "actual_process_count": metrics.actual_process_count,
                "active_slot_utilization": round(metrics.active_slot_utilization, 6),
                "coordination_ipc_overhead_sec": round(
                    metrics.coordination_ipc_overhead_sec, 6
                ),
                "ipc_payload_mib": round(metrics.ipc_payload_mib, 6),
                "child_peak_rss_avg_mib": round(metrics.child_peak_rss_avg_mib, 6),
                "child_peak_rss_max_mib": round(metrics.child_peak_rss_max_mib, 6),
                "compact_batch_mib": round(compact_batch.memory_bytes / (1024.0**2), 6),
                "deterministic_result_match": deterministic_match,
                "product_sum": sum(products),
                "decision_sum": sum(decisions),
            }
        )
        write_csv(output_dir / "benchmark_summary.csv", summary_rows)
        print(
            f"processes={process_count:>2} waves={metrics.wave_count:>2} "
            f"wall={measured_wall_sec:8.2f}s "
            f"episodes/hour={summary_rows[-1]['episodes_per_hour']:7.2f} "
            f"deterministic={deterministic_match}",
            flush=True,
        )
        del compact_batch
        del results
        gc.collect()

    fastest = max(summary_rows, key=lambda row: float(row["episodes_per_hour"]))
    report = {
        "checkpoint": str(checkpoint_path),
        "checkpoint_id": str(manifest.get("checkpoint_id", "")),
        "snapshot_hash": snapshot_hash,
        "worker_count": int(args.worker_count),
        "days": int(args.days),
        "episode_count_per_setting": (
            "one_per_process" if args.single_wave else int(args.episodes)
        ),
        "seed_start": int(args.seed_start),
        "seed_assignment": "shared_prefix_by_process_count",
        "epsilon": float(args.epsilon),
        "compact_mc_samples": True,
        "torch_threads_per_process": int(args.torch_threads),
        "process_counts": process_counts,
        "fastest_process_count": int(fastest["process_count"]),
        "fastest_episodes_per_hour": float(fastest["episodes_per_hour"]),
        "all_results_deterministic": all(
            bool(row["deterministic_result_match"]) for row in summary_rows
        ),
        "results": summary_rows,
    }
    (output_dir / "benchmark_summary.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(f"summary={output_dir / 'benchmark_summary.csv'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
