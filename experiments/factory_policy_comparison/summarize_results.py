from __future__ import annotations

import argparse
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.factory_policy_comparison.common import (
    DEFAULT_CONFIG_PATH,
    MODE_SUMMARY_CSV,
    MODE_WORKER_SUMMARY_CSV,
    RUN_SUMMARY_CSV,
    SUMMARY_JSON,
    ExperimentConfig,
    as_float,
    discover_run_dirs,
    load_experiment_config,
    mean,
    metric_paths,
    nested_get,
    read_json,
    sample_std,
    worker_count_from_run_dir,
    write_csv,
    write_json,
)


BASE_RUN_FIELDS = [
    "run_dir",
    "mode",
    "worker_count",
    "seed",
    "scenario",
    "total_days",
    "minutes_per_day",
    "status",
]


def _run_row(run_dir: Path, metrics: list[str]) -> dict[str, object]:
    run_meta = read_json(run_dir / "run_meta.json")
    kpi = read_json(run_dir / "kpi.json")
    row: dict[str, object] = {
        "run_dir": str(run_dir.resolve()),
        "mode": str(run_meta.get("decision_mode") or kpi.get("run_meta", {}).get("decision_mode") or ""),
        "worker_count": worker_count_from_run_dir(run_dir),
        "seed": int(run_meta.get("seed") or kpi.get("run_meta", {}).get("seed") or 0),
        "scenario": str(run_meta.get("scenario_type") or kpi.get("scenario_type") or ""),
        "total_days": int(run_meta.get("total_days") or kpi.get("run_meta", {}).get("total_days") or 0),
        "minutes_per_day": float(run_meta.get("minutes_per_day") or kpi.get("run_meta", {}).get("minutes_per_day") or 0.0),
        "status": "completed" if kpi else "missing_kpi",
    }
    for metric in metrics:
        row[metric] = nested_get(kpi, metric, "")
    return row


def _mode_summary(run_rows: list[dict[str, object]], metrics: list[str]) -> list[dict[str, object]]:
    modes = sorted({str(row.get("mode", "")) for row in run_rows if row.get("mode")})
    rows: list[dict[str, object]] = []
    for mode in modes:
        mode_rows = [row for row in run_rows if row.get("mode") == mode and row.get("status") == "completed"]
        summary: dict[str, object] = {
            "mode": mode,
            "completed_run_count": len(mode_rows),
        }
        for metric in metrics:
            values = [value for value in (as_float(row.get(metric)) for row in mode_rows) if value is not None]
            summary[f"{metric}.mean"] = round(mean(values), 6) if values else ""
            summary[f"{metric}.std"] = round(sample_std(values), 6) if values else ""
            summary[f"{metric}.min"] = round(min(values), 6) if values else ""
            summary[f"{metric}.max"] = round(max(values), 6) if values else ""
        rows.append(summary)
    return rows


def _mode_worker_summary(run_rows: list[dict[str, object]], metrics: list[str]) -> list[dict[str, object]]:
    keys = sorted(
        {
            (str(row.get("mode", "")), int(row.get("worker_count", 0) or 0))
            for row in run_rows
            if row.get("mode") and row.get("worker_count")
        },
        key=lambda item: (item[0], item[1]),
    )
    rows: list[dict[str, object]] = []
    for mode, worker_count in keys:
        group_rows = [
            row
            for row in run_rows
            if row.get("mode") == mode
            and int(row.get("worker_count", 0) or 0) == worker_count
            and row.get("status") == "completed"
        ]
        summary: dict[str, object] = {
            "mode": mode,
            "worker_count": worker_count,
            "completed_run_count": len(group_rows),
        }
        for metric in metrics:
            values = [value for value in (as_float(row.get(metric)) for row in group_rows) if value is not None]
            summary[f"{metric}.mean"] = round(mean(values), 6) if values else ""
            summary[f"{metric}.std"] = round(sample_std(values), 6) if values else ""
            summary[f"{metric}.min"] = round(min(values), 6) if values else ""
            summary[f"{metric}.max"] = round(max(values), 6) if values else ""
        rows.append(summary)

    previous_by_mode: dict[str, float] = {}
    for row in rows:
        mode = str(row.get("mode", ""))
        throughput = as_float(row.get("throughput_per_sim_hour.mean"))
        if throughput is None:
            row["throughput_per_sim_hour.marginal_gain"] = ""
            continue
        previous = previous_by_mode.get(mode)
        row["throughput_per_sim_hour.marginal_gain"] = "" if previous is None else round(throughput - previous, 6)
        previous_by_mode[mode] = throughput
    return rows


def summarize_results(output_root: Path, cfg: ExperimentConfig) -> dict[str, object]:
    output_root = output_root.resolve()
    metrics = metric_paths(cfg)
    run_rows = [_run_row(run_dir, metrics) for run_dir in discover_run_dirs(output_root)]
    mode_rows = _mode_summary(run_rows, metrics)
    mode_worker_rows = _mode_worker_summary(run_rows, metrics)
    write_csv(output_root / RUN_SUMMARY_CSV, run_rows, BASE_RUN_FIELDS + metrics)
    write_csv(output_root / MODE_SUMMARY_CSV, mode_rows)
    write_csv(output_root / MODE_WORKER_SUMMARY_CSV, mode_worker_rows)
    payload = {
        "run_count": len(run_rows),
        "completed_run_count": sum(1 for row in run_rows if row.get("status") == "completed"),
        "metrics": metrics,
        "runs": run_rows,
        "modes": mode_rows,
        "mode_worker": mode_worker_rows,
    }
    write_json(output_root / SUMMARY_JSON, payload)
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize factory policy comparison results.")
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_experiment_config(args.config)
    payload = summarize_results(args.output_root, cfg)
    print(f"completed={payload['completed_run_count']} run_count={payload['run_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
