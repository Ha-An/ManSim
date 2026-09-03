from __future__ import annotations

import argparse
import hashlib
import math
import random
import statistics
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.factory_policy_comparison.common import (
    AUDIT_CSV,
    DEFAULT_CONFIG_PATH,
    FAIRNESS_CSV,
    MODE_SUMMARY_CSV,
    MODE_WORKER_SUMMARY_CSV,
    PAIRED_COMPARISON_CSV,
    RUN_SUMMARY_CSV,
    STATUS_CSV,
    SUMMARY_JSON,
    ExperimentConfig,
    as_float,
    discover_run_dirs,
    load_experiment_config,
    load_run_identity,
    mean,
    metric_paths,
    nested_get,
    read_json,
    read_csv,
    sample_std,
    worker_count_from_run_dir,
    write_csv,
    write_json,
)


BASE_RUN_FIELDS = [
    "run_dir",
    "objective_mode",
    "mode",
    "worker_count",
    "seed",
    "scenario",
    "total_days",
    "minutes_per_day",
    "timing_profile_fingerprint",
    "status",
    "artifact_audit_status",
    "kpi_audit_status",
    "fairness_pass",
    "comparison_eligible",
]


# The comparison dashboard has a stable set of core tables and charts. Keep
# their source metrics available even when a specialized experiment config
# requests only a narrower set of additional metrics.
DASHBOARD_REQUIRED_METRICS = [
    "total_products",
    "throughput_per_sim_hour",
    "avg_daily_products",
    "completed_product_lead_time_avg_min",
    "humanoid_incident_total",
    "humanoid_execution_ratio_avg",
    "humanoid_blocked_ratio_avg",
    "otc",
    "makespan_min",
    "initial_batch_progress_ratio",
    "initial_batch_yield_ratio",
]


def _rows_by_run_dir(rows: list[dict[str, str]]) -> dict[Path, dict[str, str]]:
    indexed: dict[Path, dict[str, str]] = {}
    for row in rows:
        raw_path = str(row.get("run_dir", "")).strip()
        if raw_path:
            indexed[Path(raw_path).resolve()] = row
    return indexed


def _run_row(
    run_dir: Path,
    metrics: list[str],
    *,
    status_row: dict[str, str] | None = None,
    audit_row: dict[str, str] | None = None,
    fairness_row: dict[str, str] | None = None,
    require_audit: bool = False,
    require_fairness: bool = False,
) -> dict[str, object]:
    run_meta = read_json(run_dir / "run_meta.json")
    kpi = read_json(run_dir / "kpi.json")
    identity = load_run_identity(run_dir)
    has_kpi = bool(kpi)
    execution_status = str((status_row or {}).get("status", "")).strip()
    if not execution_status:
        execution_status = "completed" if has_kpi else "missing_kpi"
    artifact_audit_status = str((audit_row or {}).get("artifact_audit_status", "")).strip()
    kpi_audit_status = str((audit_row or {}).get("kpi_audit_status", "")).strip()
    fairness_pass = str((fairness_row or {}).get("fairness_pass", "")).strip()
    execution_ok = execution_status in {"completed", "skipped_existing"}
    audit_ok = (
        artifact_audit_status == "pass" and kpi_audit_status == "pass"
        if require_audit
        else True
    )
    fairness_ok = fairness_pass.lower() == "true" if require_fairness else True
    comparison_eligible = bool(has_kpi and execution_ok and audit_ok and fairness_ok)
    row: dict[str, object] = {
        "run_dir": str(run_dir.resolve()),
        "objective_mode": str(identity.get("objective_mode", "scenario_default")),
        "mode": str(run_meta.get("decision_mode") or kpi.get("run_meta", {}).get("decision_mode") or ""),
        "worker_count": worker_count_from_run_dir(run_dir),
        "seed": int(run_meta.get("seed") or kpi.get("run_meta", {}).get("seed") or 0),
        "scenario": str(run_meta.get("scenario_type") or kpi.get("scenario_type") or ""),
        "total_days": int(run_meta.get("total_days") or kpi.get("run_meta", {}).get("total_days") or 0),
        "minutes_per_day": float(run_meta.get("minutes_per_day") or kpi.get("run_meta", {}).get("minutes_per_day") or 0.0),
        "status": execution_status,
        "artifact_audit_status": artifact_audit_status,
        "kpi_audit_status": kpi_audit_status,
        "fairness_pass": fairness_pass,
        "comparison_eligible": comparison_eligible,
        "timing_profile_fingerprint": str(
            (run_meta.get("task_primitive_timing", {}) if isinstance(run_meta.get("task_primitive_timing", {}), dict) else {}).get(
                "profile_fingerprint", ""
            )
        ),
    }
    for metric in metrics:
        row[metric] = nested_get(kpi, metric, "")
    return row


def _mode_summary(run_rows: list[dict[str, object]], metrics: list[str]) -> list[dict[str, object]]:
    keys = sorted(
        {
            (str(row.get("objective_mode", "scenario_default")), str(row.get("mode", "")))
            for row in run_rows
            if row.get("mode")
        }
    )
    rows: list[dict[str, object]] = []
    for objective_mode, mode in keys:
        completed_rows = [
            row
            for row in run_rows
            if row.get("objective_mode") == objective_mode
            and row.get("mode") == mode
            and row.get("status") in {"completed", "skipped_existing"}
        ]
        mode_rows = [row for row in completed_rows if bool(row.get("comparison_eligible"))]
        summary: dict[str, object] = {
            "objective_mode": objective_mode,
            "mode": mode,
            "completed_run_count": len(completed_rows),
            "comparison_run_count": len(mode_rows),
        }
        for metric in metrics:
            values = [value for value in (as_float(row.get(metric)) for row in mode_rows) if value is not None]
            summary[f"{metric}.mean"] = round(mean(values), 6) if values else ""
            # A sample standard deviation is undefined for a single run. Keep
            # the cell empty instead of presenting false zero uncertainty.
            summary[f"{metric}.std"] = round(sample_std(values), 6) if len(values) > 1 else ""
            summary[f"{metric}.min"] = round(min(values), 6) if values else ""
            summary[f"{metric}.max"] = round(max(values), 6) if values else ""
        rows.append(summary)
    return rows


def _mode_worker_summary(run_rows: list[dict[str, object]], metrics: list[str]) -> list[dict[str, object]]:
    keys = sorted(
        {
            (
                str(row.get("objective_mode", "")),
                str(row.get("mode", "")),
                int(row.get("worker_count", 0) or 0),
            )
            for row in run_rows
            if row.get("mode") and row.get("worker_count")
        },
        key=lambda item: (item[0], item[1], item[2]),
    )
    rows: list[dict[str, object]] = []
    for objective_mode, mode, worker_count in keys:
        completed_rows = [
            row
            for row in run_rows
            if row.get("objective_mode") == objective_mode
            and row.get("mode") == mode
            and int(row.get("worker_count", 0) or 0) == worker_count
            and row.get("status") in {"completed", "skipped_existing"}
        ]
        group_rows = [row for row in completed_rows if bool(row.get("comparison_eligible"))]
        summary: dict[str, object] = {
            "objective_mode": objective_mode,
            "mode": mode,
            "worker_count": worker_count,
            "completed_run_count": len(completed_rows),
            "comparison_run_count": len(group_rows),
        }
        for metric in metrics:
            values = [value for value in (as_float(row.get(metric)) for row in group_rows) if value is not None]
            summary[f"{metric}.mean"] = round(mean(values), 6) if values else ""
            summary[f"{metric}.std"] = round(sample_std(values), 6) if len(values) > 1 else ""
            summary[f"{metric}.min"] = round(min(values), 6) if values else ""
            summary[f"{metric}.max"] = round(max(values), 6) if values else ""
        rows.append(summary)

    previous_throughput: dict[tuple[str, str], tuple[int, float]] = {}
    previous_makespan: dict[tuple[str, str], tuple[int, float]] = {}
    for row in rows:
        objective_mode = str(row.get("objective_mode", "scenario_default"))
        mode = str(row.get("mode", ""))
        current_worker_count = int(row.get("worker_count", 0) or 0)
        group_key = (objective_mode, mode)
        throughput = as_float(row.get("throughput_per_sim_hour.mean"))
        if objective_mode != "maximize_throughput" or throughput is None:
            row["throughput_per_sim_hour.marginal_gain"] = ""
        else:
            previous = previous_throughput.get(group_key)
            row["throughput_per_sim_hour.marginal_gain"] = (
                ""
                if previous is None
                else round((throughput - previous[1]) / max(1, current_worker_count - previous[0]), 6)
            )
            previous_throughput[group_key] = (current_worker_count, throughput)

        makespan = as_float(row.get("makespan_min.mean"))
        if objective_mode != "minimize_makespan" or makespan is None:
            row["makespan_min.marginal_reduction"] = ""
        else:
            previous = previous_makespan.get(group_key)
            row["makespan_min.marginal_reduction"] = (
                ""
                if previous is None
                else round((previous[1] - makespan) / max(1, current_worker_count - previous[0]), 6)
            )
            previous_makespan[group_key] = (current_worker_count, makespan)
    return rows


def _paired_bootstrap_interval(values: list[float], *, repetitions: int = 10000) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return values[0], values[0]
    digest = hashlib.sha256(",".join(f"{value:.12g}" for value in values).encode("ascii")).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    means = sorted(
        statistics.fmean(values[rng.randrange(len(values))] for _ in values)
        for _ in range(max(1, int(repetitions)))
    )
    low_index = max(0, min(len(means) - 1, int(math.floor(0.025 * (len(means) - 1)))))
    high_index = max(0, min(len(means) - 1, int(math.ceil(0.975 * (len(means) - 1)))))
    return float(means[low_index]), float(means[high_index])


def _paired_adp_comparisons(run_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    eligible = [row for row in run_rows if bool(row.get("comparison_eligible"))]
    groups = sorted(
        {
            (str(row.get("objective_mode", "")), int(row.get("worker_count", 0) or 0))
            for row in eligible
            if row.get("mode") == "simulation_based_adp"
        }
    )
    output: list[dict[str, object]] = []
    for objective_mode, worker_count in groups:
        group_rows = [
            row for row in eligible
            if str(row.get("objective_mode", "")) == objective_mode
            and int(row.get("worker_count", 0) or 0) == worker_count
        ]
        by_mode_seed = {
            (str(row.get("mode", "")), int(row.get("seed", 0) or 0)): row
            for row in group_rows
        }
        baselines = sorted({str(row.get("mode", "")) for row in group_rows} - {"simulation_based_adp"})
        for baseline in baselines:
            adp_seeds = {seed for mode, seed in by_mode_seed if mode == "simulation_based_adp"}
            baseline_seeds = {seed for mode, seed in by_mode_seed if mode == baseline}
            paired_seeds = sorted(adp_seeds & baseline_seeds)
            for metric in ("total_products", "throughput_per_sim_hour"):
                pairs: list[tuple[float, float]] = []
                for seed in paired_seeds:
                    adp_value = as_float(by_mode_seed[("simulation_based_adp", seed)].get(metric))
                    baseline_value = as_float(by_mode_seed[(baseline, seed)].get(metric))
                    if adp_value is not None and baseline_value is not None:
                        pairs.append((adp_value, baseline_value))
                differences = [adp - base for adp, base in pairs]
                low, high = _paired_bootstrap_interval(differences)
                baseline_mean = statistics.fmean(base for _adp, base in pairs) if pairs else 0.0
                mean_difference = statistics.fmean(differences) if differences else 0.0
                output.append(
                    {
                        "objective_mode": objective_mode,
                        "worker_count": worker_count,
                        "adp_mode": "simulation_based_adp",
                        "baseline_mode": baseline,
                        "metric": metric,
                        "paired_seed_count": len(pairs),
                        "mean_difference": round(mean_difference, 6),
                        "ci95_low": round(low, 6),
                        "ci95_high": round(high, 6),
                        "relative_improvement_pct": round(
                            100.0 * mean_difference / baseline_mean, 6
                        ) if baseline_mean else "",
                        "win_count": sum(1 for value in differences if value > 0),
                        "tie_count": sum(1 for value in differences if value == 0),
                        "loss_count": sum(1 for value in differences if value < 0),
                        # A one-run smoke test can have a positive degenerate
                        # interval, but it is not inferential evidence.
                        "superiority_demonstrated": bool(
                            len(pairs) >= 2 and mean_difference > 0 and low > 0
                        ),
                        "primary_comparison": baseline == "immediate_shared" and metric == "total_products",
                    }
                )
    return output


def summarize_results(output_root: Path, cfg: ExperimentConfig) -> dict[str, object]:
    output_root = output_root.resolve()
    metrics = list(dict.fromkeys([*metric_paths(cfg), *DASHBOARD_REQUIRED_METRICS]))
    status_rows = read_csv(output_root / STATUS_CSV)
    audit_rows = read_csv(output_root / AUDIT_CSV)
    fairness_rows = read_csv(output_root / FAIRNESS_CSV)
    status_by_dir = _rows_by_run_dir(status_rows)
    audit_by_dir = _rows_by_run_dir(audit_rows)
    fairness_by_dir = _rows_by_run_dir(fairness_rows)
    require_audit = bool(audit_rows)
    require_fairness = bool(fairness_rows)
    run_rows = [
        _run_row(
            run_dir,
            metrics,
            status_row=status_by_dir.get(run_dir.resolve()),
            audit_row=audit_by_dir.get(run_dir.resolve()),
            fairness_row=fairness_by_dir.get(run_dir.resolve()),
            require_audit=require_audit,
            require_fairness=require_fairness,
        )
        for run_dir in discover_run_dirs(output_root)
    ]
    mode_rows = _mode_summary(run_rows, metrics)
    mode_worker_rows = _mode_worker_summary(run_rows, metrics)
    paired_rows = _paired_adp_comparisons(run_rows)
    write_csv(output_root / RUN_SUMMARY_CSV, run_rows, BASE_RUN_FIELDS + metrics)
    write_csv(output_root / MODE_SUMMARY_CSV, mode_rows)
    write_csv(output_root / MODE_WORKER_SUMMARY_CSV, mode_worker_rows)
    write_csv(output_root / PAIRED_COMPARISON_CSV, paired_rows)
    payload = {
        "run_count": len(run_rows),
        "completed_run_count": sum(
            1 for row in run_rows if row.get("status") in {"completed", "skipped_existing"}
        ),
        "comparison_run_count": sum(1 for row in run_rows if bool(row.get("comparison_eligible"))),
        "metrics": metrics,
        "objectives": sorted({str(row.get("objective_mode", "")) for row in run_rows}),
        "runs": run_rows,
        "modes": mode_rows,
        "mode_worker": mode_worker_rows,
        "paired_comparisons": paired_rows,
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
