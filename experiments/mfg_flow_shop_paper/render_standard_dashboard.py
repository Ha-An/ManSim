from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(REPO_ROOT))

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
    load_experiment_config,
    metric_paths,
    write_csv,
    write_json,
)
from experiments.factory_policy_comparison.render_dashboard import render_dashboard
from experiments.factory_policy_comparison.summarize_results import (
    BASE_RUN_FIELDS,
    DASHBOARD_REQUIRED_METRICS,
    _mode_summary,
    _mode_worker_summary,
    _paired_adp_comparisons,
    _run_row,
)


STANDARD_DASHBOARD_DIR = "policy_comparison_dashboard"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _dashboard_config(plan: dict[str, object]) -> ExperimentConfig:
    base = load_experiment_config(DEFAULT_CONFIG_PATH)
    objective = str(plan.get("objective_mode", "maximize_throughput"))
    return replace(
        base,
        scenario=str(plan.get("scenario", "mfg_flow_shop")),
        horizon_days=int(plan.get("horizon_days", 5)),
        minutes_per_day=float(plan.get("minutes_per_day", 480.0)),
        objective_modes=[objective],
        seeds=[int(value) for value in plan.get("test_seeds", [])],
        worker_counts=[int(value) for value in plan.get("worker_counts", [])],
        modes=[str(value) for value in plan.get("policies", [])],
        adp_checkpoint_path="",
        benchmark_seeds_locked=True,
    )


def _compatibility_status(
    source: dict[str, str],
    *,
    objective_mode: str,
    scenario: str,
    horizon_days: int,
) -> dict[str, object]:
    return {
        "run_id": source.get("run_id", ""),
        "objective_mode": objective_mode,
        "mode": source.get("mode", ""),
        "worker_count": source.get("worker_count", ""),
        "seed": source.get("seed", ""),
        "scenario": scenario,
        "horizon_days": horizon_days,
        "makespan_max_sim_days": 30,
        "rolling_window_min": "",
        "run_dir": source.get("run_dir", ""),
        "status": source.get("status", ""),
        "return_code": source.get("return_code", ""),
        "elapsed_sec": source.get("elapsed_sec", ""),
        "reason": source.get("reason", ""),
        "command": "",
    }


def _compatibility_fairness(
    source: dict[str, str],
    *,
    objective_mode: str,
    group: dict[str, str],
    raw: dict[str, str],
) -> dict[str, object]:
    passed = str(group.get("fairness_pass", "")).lower() == "true"
    return {
        "run_dir": source.get("run_dir", ""),
        "objective_mode": objective_mode,
        "mode": source.get("mode", ""),
        "worker_count": source.get("worker_count", ""),
        "seed": source.get("seed", ""),
        "scenario_ok": True,
        "objective_mode_ok": True,
        "worker_count_ok": True,
        "horizon_ok": True,
        "configured_throughput_days_ok": True,
        "configured_max_sim_days_ok": True,
        "minutes_per_day_ok": True,
        "seed_ok": True,
        "timing_profile_fingerprint": raw.get("timing_profile_fingerprint", ""),
        "pre_run_environment_fingerprint": raw.get("environment_fingerprint", ""),
        "pre_run_policy_fingerprint": raw.get("policy_fingerprint", ""),
        "pre_run_policy_independent_ok": passed,
        "objective_contract_ok": True,
        "objective_contract_details": "pass",
        "stochastic_stream_scheme_ok": True,
        "artifact_errors_empty": source.get("artifact_audit") == "pass",
        "artifact_errors": "{}",
        "quality_stream_prefix_matches": group.get("quality_rng_prefix_consistent", ""),
        "repair_stream_prefix_matches": group.get("repair_rng_prefix_consistent", ""),
        "pre_run_matches_reference": passed,
        "timing_profile_matches_reference": passed,
        "fairness_pass": passed,
    }


def _experiment_timing(prepared: Path, *, parallel_jobs: int) -> dict[str, object]:
    recorded = prepared / "evaluation_timing.json"
    if recorded.is_file():
        return _read_json(recorded)
    start_path = prepared / "evaluation_preflight.json"
    end_path = prepared / "evaluation_status.csv"
    start = start_path.stat().st_mtime if start_path.is_file() else prepared.stat().st_mtime
    end = end_path.stat().st_mtime if end_path.is_file() else start
    wall_sec = max(0.0, end - start)
    extension_path = prepared / "evaluation_extension.json"
    original_wall_sec = 0.0
    if extension_path.is_file():
        original_wall_sec = float(_read_json(extension_path).get("original_evaluation_wall_sec", 0.0))
        wall_sec += original_wall_sec
    return {
        "started_at_utc": datetime.fromtimestamp(start, timezone.utc).isoformat(),
        "completed_at_utc": datetime.fromtimestamp(end, timezone.utc).isoformat(),
        "parallel_jobs": parallel_jobs,
        "run_phase_wall_sec": round(wall_sec, 3),
        "experiment_wall_sec_through_summary": round(wall_sec, 3),
        "prior_evaluation_wall_sec": original_wall_sec,
        "measurement": "sum of evaluation session wall clocks" if original_wall_sec else "evaluation preflight-to-final-status wall clock",
    }


def _add_confirmatory_overall_contrasts(
    rows: list[dict[str, object]],
    source_rows: list[dict[str, str]],
    *,
    objective_mode: str,
) -> list[dict[str, object]]:
    for row in rows:
        row["primary_comparison"] = False
    for source in source_rows:
        if source.get("scope") == "worker":
            for row in rows:
                if (str(row.get("worker_count")) == str(source.get("worker_count"))
                        and row.get("baseline_mode") == source.get("baseline_mode")
                        and row.get("metric") == "total_products"):
                    for target, field in (("mean_difference", "adp_minus_baseline_mean"),
                                          ("ci95_low", "ci95_low"), ("ci95_high", "ci95_high"),
                                          ("win_count", "win_count"), ("tie_count", "tie_count"),
                                          ("loss_count", "loss_count"),
                                          ("relative_improvement_pct", "relative_improvement_pct")):
                        row[target] = source.get(field, "")
                    row["superiority_demonstrated"] = float(source["ci95_low"]) > 0 and float(source["adp_minus_baseline_mean"]) > 0
                    row["bootstrap_source"] = "registered paper experiment"
            continue
        if source.get("scope") != "overall_equal_worker_weight":
            continue
        baseline = source.get("baseline_mode", "")
        mean_difference = float(source.get("adp_minus_baseline_mean", 0.0) or 0.0)
        ci_low = float(source.get("ci95_low", 0.0) or 0.0)
        rows.append(
            {
                "objective_mode": objective_mode,
                "worker_count": source.get("worker_count", "2-6"),
                "adp_mode": "simulation_based_adp",
                "baseline_mode": baseline,
                "metric": "total_products",
                "paired_seed_count": source.get("seed_count", ""),
                "mean_difference": source.get("adp_minus_baseline_mean", ""),
                "ci95_low": source.get("ci95_low", ""),
                "ci95_high": source.get("ci95_high", ""),
                "relative_improvement_pct": source.get("relative_improvement_pct", ""),
                "win_count": source.get("win_count", ""),
                "tie_count": source.get("tie_count", ""),
                "loss_count": source.get("loss_count", ""),
                "superiority_demonstrated": mean_difference > 0.0 and ci_low > 0.0,
                "primary_comparison": baseline == "immediate_shared",
                "bootstrap_source": "registered paper experiment; seed-block resampling across fleets",
            }
        )
    return rows


def render(prepared: Path) -> Path:
    prepared = prepared.resolve()
    plan = _read_json(prepared / "experiment_plan.json")
    analysis = _read_json(prepared / "analysis_summary.json")
    if analysis.get("status") != "pass":
        raise RuntimeError("Confirmatory analysis must pass before dashboard rendering.")

    cfg = _dashboard_config(plan)
    timing = _experiment_timing(prepared, parallel_jobs=5)
    objective_mode = cfg.objective_modes[0]
    status_sources = _read_csv(prepared / "evaluation_status.csv")
    fairness_groups = {
        (int(row["worker_count"]), int(row["seed"])): row
        for row in _read_csv(prepared / "fairness_report.csv")
    }
    raw_by_run = {
        row["run_id"]: row for row in _read_csv(prepared / "evaluation_raw.csv")
    }
    if len(status_sources) != int(analysis.get("eligible_run_count", -1)):
        raise RuntimeError("Evaluation status count does not match eligible confirmatory runs.")

    output = prepared / STANDARD_DASHBOARD_DIR
    output.mkdir(parents=True, exist_ok=True)
    if (prepared / "result_validity.json").is_file():
        write_json(output / "result_validity.json", _read_json(prepared / "result_validity.json"))
    capacity = _read_json(prepared / "theoretical_capacity.json")
    capacity["source"] = "archived_run_config"
    write_json(output / "theoretical_capacity.json", capacity)
    status_rows: list[dict[str, object]] = []
    audit_rows: list[dict[str, object]] = []
    fairness_rows: list[dict[str, object]] = []
    run_rows: list[dict[str, object]] = []
    metrics = list(dict.fromkeys([*metric_paths(cfg), *DASHBOARD_REQUIRED_METRICS]))

    for source in status_sources:
        worker_count = int(source["worker_count"])
        seed = int(source["seed"])
        group = fairness_groups.get((worker_count, seed))
        if group is None:
            raise RuntimeError(f"Missing fairness group for workers={worker_count}, seed={seed}.")
        raw = raw_by_run.get(source["run_id"], {})
        status_row = _compatibility_status(
            source,
            objective_mode=objective_mode,
            scenario=cfg.scenario,
            horizon_days=cfg.horizon_days,
        )
        audit_row = {
            "run_dir": source["run_dir"],
            "objective_mode": objective_mode,
            "mode": source["mode"],
            "worker_count": worker_count,
            "seed": seed,
            "artifact_audit_status": source.get("artifact_audit", ""),
            "artifact_audit_return_code": 0 if source.get("artifact_audit") == "pass" else 1,
            "kpi_audit_status": source.get("kpi_audit", ""),
            "kpi_audit_return_code": 0 if source.get("kpi_audit") == "pass" else 1,
            "artifact_audit_tail": "paper experiment per-run audit",
            "kpi_audit_tail": "paper experiment per-run audit",
        }
        fairness_row = _compatibility_fairness(
            source,
            objective_mode=objective_mode,
            group=group,
            raw=raw,
        )
        status_rows.append(status_row)
        audit_rows.append(audit_row)
        fairness_rows.append(fairness_row)
        run_rows.append(
            _run_row(
                Path(source["run_dir"]),
                metrics,
                status_row={key: str(value) for key, value in status_row.items()},
                audit_row={key: str(value) for key, value in audit_row.items()},
                fairness_row={key: str(value) for key, value in fairness_row.items()},
                require_audit=True,
                require_fairness=True,
            )
        )

    mode_rows = _mode_summary(run_rows, metrics)
    mode_worker_rows = _mode_worker_summary(run_rows, metrics)
    paired_rows = _add_confirmatory_overall_contrasts(
        _paired_adp_comparisons(run_rows),
        _read_csv(prepared / "paired_contrasts.csv"),
        objective_mode=objective_mode,
    )
    write_csv(output / STATUS_CSV, status_rows)
    write_csv(output / AUDIT_CSV, audit_rows)
    write_csv(output / FAIRNESS_CSV, fairness_rows)
    write_csv(output / RUN_SUMMARY_CSV, run_rows, BASE_RUN_FIELDS + metrics)
    write_csv(output / MODE_SUMMARY_CSV, mode_rows)
    write_csv(output / MODE_WORKER_SUMMARY_CSV, mode_worker_rows)
    write_csv(output / PAIRED_COMPARISON_CSV, paired_rows)
    write_json(
        output / SUMMARY_JSON,
        {
            "run_count": len(run_rows),
            "completed_run_count": sum(
                row.get("status") in {"completed", "skipped_existing"} for row in run_rows
            ),
            "comparison_run_count": sum(bool(row.get("comparison_eligible")) for row in run_rows),
            "metrics": metrics,
            "objectives": cfg.objective_modes,
            "runs": run_rows,
            "modes": mode_rows,
            "mode_worker": mode_worker_rows,
            "paired_comparisons": paired_rows,
            "source_analysis": str((prepared / "analysis_summary.json").resolve()),
        },
    )
    write_json(
        output / "experiment_plan.json",
        {
            "scenario": cfg.scenario,
            "condition_label": plan.get("condition_label", ""),
            "horizon_days": cfg.horizon_days,
            "makespan_max_sim_days": cfg.makespan_max_sim_days,
            "minutes_per_day": cfg.minutes_per_day,
            "objective_modes": cfg.objective_modes,
            "modes": cfg.modes,
            "worker_counts": cfg.worker_counts,
            "seeds": cfg.seeds,
            "run_count": len(run_rows),
            "parallel_jobs": timing["parallel_jobs"],
            "policy_contract": {
                "explicit_wait_action_enabled": False,
                "forced_idle_without_feasible_task_allowed": True,
                "beam_worker_order_strategy": "cyclic" if "simulation_based_adp" in cfg.modes else "not applicable",
            },
            "confirmatory_analysis": str((prepared / "analysis_summary.json").resolve()),
        },
    )
    write_json(output / "experiment_timing.json", timing)
    return render_dashboard(output, cfg)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render paper results with the standard policy dashboard.")
    parser.add_argument("--prepared", type=Path, required=True)
    args = parser.parse_args()
    print(render(args.prepared))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
