from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import sys
import yaml
from typing import Callable


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.factory_policy_comparison.audit_experiment import _pre_run_fingerprints
from experiments.factory_policy_comparison.theoretical_capacity import calculate_theoretical_capacity


METRICS = (
    "total_products",
    "throughput_per_sim_hour",
    "completed_product_lead_time_avg_min",
    "humanoid_execution_ratio_avg",
    "humanoid_blocked_ratio_avg",
    "humanoid_unavailable_ratio_avg",
    "machine_failure_count",
    "repair_response_time_avg_min",
    "preventive_maintenance_count",
    "worker_depleted_during_task_count",
    "worker_depleted_during_move_count",
    "worker_returned_next_day_count",
    "buffer_overflow_attempt_count",
    "buffer_reservation_leak_count",
)
PRIMARY_METRIC = "total_products"
ADP_MODE = "simulation_based_adp"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _as_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _sample_std(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = probability * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _rng(label: str) -> random.Random:
    digest = hashlib.sha256(label.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _observed_prefixes_consistent(sequences: list[list[object]]) -> bool:
    observed = [sequence for sequence in sequences if sequence]
    if len(observed) < 2:
        return True
    longest = max(observed, key=len)
    return all(sequence == longest[:len(sequence)] for sequence in observed)


def _plan_errors(plan: list[dict[str, str]], statuses: list[dict[str, str]], experiment: dict) -> list[str]:
    errors = []
    for label, values in (("run ID", [row["run_id"] for row in plan]),
                          ("run directory", [str(Path(row["run_dir"]).resolve()) for row in plan]),
                          ("status ID", [row["run_id"] for row in statuses])):
        errors.extend(f"duplicate {label}: {value}" for value, count in Counter(values).items() if count > 1)
    expected = {
        (mode, int(worker), int(seed), str(rep) if mode == ADP_MODE else "")
        for mode in experiment["policies"] for worker in experiment["worker_counts"]
        for seed in experiment["test_seeds"]
        for rep in (range(1, int(experiment["training_replicates"]) + 1) if mode == ADP_MODE else [0])
    }
    actual = [(row["mode"], int(row["worker_count"]), int(row["seed"]), row.get("training_replicate", "")) for row in plan]
    errors.extend(f"missing planned combination: {key}" for key in sorted(expected - set(actual)))
    errors.extend(f"unexpected planned combination: {key}" for key in sorted(set(actual) - expected))
    errors.extend(f"duplicate planned combination: {key}" for key, count in Counter(actual).items() if count > 1)
    return errors


def _identity_errors(source: dict, meta: dict, kpi: dict, diagnostics: dict, experiment: dict) -> list[str]:
    expected = {"scenario_type": experiment["scenario"], "decision_mode": source["mode"],
                "seed": int(source["seed"]), "objective_mode": experiment["objective_mode"],
                "minutes_per_day": float(experiment["minutes_per_day"])}
    errors = []
    for label, recorded in (("run_meta", meta), ("kpi.run_meta", kpi.get("run_meta", {}))):
        for key, value in expected.items():
            if recorded.get(key) != value:
                errors.append(f"{label}.{key}: {recorded.get(key)!r} != {value!r}")
    if len(diagnostics.get("inputs", {}).get("worker_ids", [])) != int(source["worker_count"]):
        errors.append("worker count differs from plan")
    if kpi.get("termination_reason") != "completed_horizon":
        errors.append("termination reason is not completed_horizon")
    return errors


def _centered_bootstrap_p(samples: list[float], estimate: float) -> float:
    # Recenter bootstrap errors under H0, not the distribution around the observed effect.
    extreme = sum(abs(value - estimate) >= abs(estimate) - 1e-12 for value in samples)
    return (extreme + 1) / (len(samples) + 1)


def _normalize_legacy_repair_samples(samples: object) -> dict[str, list[object]]:
    """Collapse duplicate repair-start observations emitted for one failure."""
    if not isinstance(samples, dict):
        return {}
    normalized: dict[str, list[object]] = {}
    for machine_id, raw_values in samples.items():
        values = raw_values if isinstance(raw_values, list) else []
        unique_failures: list[object] = []
        for value in values:
            if not unique_failures or value != unique_failures[-1]:
                unique_failures.append(value)
        # Historical signatures stored milliseconds; newer signatures store microseconds.
        normalized[str(machine_id)] = [
            round(value, 3) if isinstance(value, (int, float)) else value
            for value in unique_failures
        ]
    return normalized


def _bootstrap_interval(
    sampler: Callable[[random.Random], float],
    *,
    label: str,
    repetitions: int,
) -> tuple[float, float, list[float]]:
    rng = _rng(label)
    samples = [float(sampler(rng)) for _ in range(repetitions)]
    return _percentile(samples, 0.025), _percentile(samples, 0.975), samples


def collect_runs(prepared: Path) -> tuple[list[dict[str, object]], list[str]]:
    plan_path = prepared / "evaluation_plan.csv"
    status_path = prepared / "evaluation_status.csv"
    if not plan_path.is_file():
        raise FileNotFoundError(plan_path)
    plan = _read_csv(plan_path)
    experiment = _read_json(prepared / "experiment_plan.json")
    expected_minutes = float(experiment["horizon_days"]) * float(experiment["minutes_per_day"])
    status_rows = _read_csv(status_path) if status_path.is_file() else []
    statuses = {row["run_id"]: row for row in status_rows}
    errors = _plan_errors(plan, status_rows, experiment)
    rows: list[dict[str, object]] = []
    for source in plan:
        run_id = source["run_id"]
        run_dir = Path(source["run_dir"])
        status = statuses.get(run_id, {})
        status_name = str(status.get("status", "missing_status"))
        artifact_audit = str(status.get("artifact_audit", "missing"))
        kpi_audit = str(status.get("kpi_audit", "missing"))
        eligible = status_name in {"completed", "skipped_existing"}
        eligible = eligible and artifact_audit == "pass" and kpi_audit == "pass"
        kpi_path = run_dir / "kpi.json"
        meta_path = run_dir / "run_meta.json"
        diagnostic_path = run_dir / "pre_run_diagnostics.json"
        if not (kpi_path.is_file() and meta_path.is_file() and diagnostic_path.is_file()):
            eligible = False
        if not eligible:
            errors.append(
                f"{run_id}: status={status_name}, artifact={artifact_audit}, kpi={kpi_audit}, "
                f"artifacts_present={kpi_path.is_file() and meta_path.is_file() and diagnostic_path.is_file()}"
            )
            continue
        kpi = _read_json(kpi_path)
        meta = _read_json(meta_path)
        identity_errors = _identity_errors(source, meta, kpi, _read_json(diagnostic_path), experiment)
        if identity_errors:
            errors.extend(f"{run_id}: {error}" for error in identity_errors)
            continue
        if kpi.get("objective_status") != "complete" or abs(float(kpi.get("sim_elapsed_min", -1)) - expected_minutes) > 1e-6:
            errors.append(f"{run_id}: incomplete objective or wrong horizon")
            continue
        environment_fingerprint, policy_fingerprint = _pre_run_fingerprints(run_dir)
        timing = meta.get("task_primitive_timing", {})
        timing_fingerprint = str(timing.get("profile_fingerprint", "")) if isinstance(timing, dict) else ""
        if not timing_fingerprint or not environment_fingerprint or not meta.get("stochastic_streams"):
            errors.append(f"{run_id}: missing timing/environment/RNG fingerprint")
            continue
        signature = meta.get("event_audit_signature", {})
        if not isinstance(signature, dict):
            signature = {}
        row: dict[str, object] = {
            "run_id": run_id,
            "mode": source["mode"],
            "worker_count": int(source["worker_count"]),
            "seed": int(source["seed"]),
            "training_replicate": int(source["training_replicate"]) if source["training_replicate"] else "",
            "run_dir": str(run_dir.resolve()),
            "checkpoint_path": source["checkpoint_path"],
            "status": status_name,
            "artifact_audit": artifact_audit,
            "kpi_audit": kpi_audit,
            "environment_fingerprint": environment_fingerprint,
            "policy_fingerprint": policy_fingerprint,
            "timing_profile_fingerprint": timing_fingerprint,
            "stochastic_streams_fingerprint": _stable_hash(meta.get("stochastic_streams", {})),
            "quality_rng_prefix_json": json.dumps(signature.get("quality", []), separators=(",", ":")),
            "repair_rng_prefix_json": json.dumps(
                _normalize_legacy_repair_samples(signature.get("repair_samples", {})),
                sort_keys=True,
                separators=(",", ":"),
            ),
            "restock_times_json": json.dumps(signature.get("restock_times", []), separators=(",", ":")),
        }
        for metric in METRICS:
            value = kpi.get(metric, "")
            if metric == "repair_response_time_avg_min" and int(kpi.get("machine_failure_count", 0) or 0) == 0:
                value = ""
            row[metric] = value
        rows.append(row)

    expected_ids = {row["run_id"] for row in plan}
    unexpected = sorted(set(statuses) - expected_ids)
    errors.extend(f"unexpected status row: {run_id}" for run_id in unexpected)
    return rows, errors


def fairness_report(
    rows: list[dict[str, object]],
    *,
    policies: list[str],
    training_replicates: int,
) -> tuple[list[dict[str, object]], list[str]]:
    groups: dict[tuple[int, int], list[dict[str, object]]] = {}
    for row in rows:
        groups.setdefault((int(row["worker_count"]), int(row["seed"])), []).append(row)
    report: list[dict[str, object]] = []
    errors: list[str] = []
    for (worker_count, seed), group in sorted(groups.items()):
        environment = {str(row["environment_fingerprint"]) for row in group}
        timing = {str(row["timing_profile_fingerprint"]) for row in group}
        stochastic = {str(row["stochastic_streams_fingerprint"]) for row in group}
        quality_sequences = [json.loads(str(row["quality_rng_prefix_json"])) for row in group]
        repair_maps = [json.loads(str(row["repair_rng_prefix_json"])) for row in group]
        repair_machines = sorted({machine for mapping in repair_maps for machine in mapping})
        quality_consistent = _observed_prefixes_consistent(quality_sequences)
        repair_consistent = all(
            _observed_prefixes_consistent([mapping.get(machine, []) for mapping in repair_maps])
            for machine in repair_machines
        )
        restock_schedules = {str(row["restock_times_json"]) for row in group}
        mode_replicates = {
            (str(row["mode"]), str(row["training_replicate"])) for row in group
        }
        expected_units = {
            (mode, str(replicate) if mode == ADP_MODE else "")
            for mode in policies
            for replicate in (range(1, training_replicates + 1) if mode == ADP_MODE else [""])
        }
        expected = len(expected_units)
        passed = (
            len(group) == expected
            and mode_replicates == expected_units
            and len(environment) == 1
            and len(timing) == 1
            and len(stochastic) == 1
            and quality_consistent
            and repair_consistent
            and len(restock_schedules) == 1
        )
        report.append({
            "worker_count": worker_count,
            "seed": seed,
            "run_count": len(group),
            "expected_run_count": expected,
            "mode_replicate_count": len(mode_replicates),
            "environment_fingerprint_count": len(environment),
            "timing_profile_fingerprint_count": len(timing),
            "stochastic_streams_fingerprint_count": len(stochastic),
            "quality_rng_prefix_consistent": quality_consistent,
            "repair_rng_prefix_consistent": repair_consistent,
            "restock_schedule_consistent": len(restock_schedules) == 1,
            "fairness_pass": passed,
        })
        if not passed:
            errors.append(f"fairness failed for workers={worker_count}, seed={seed}")
    return report, errors


def _metric_data(
    rows: list[dict[str, object]], metric: str
) -> tuple[dict[tuple[str, int], dict[int, float]], dict[int, dict[int, dict[int, float]]]]:
    baselines: dict[tuple[str, int], dict[int, float]] = {}
    adp: dict[int, dict[int, dict[int, float]]] = {}
    for row in rows:
        value = _as_float(row.get(metric))
        if value is None:
            continue
        worker = int(row["worker_count"])
        seed = int(row["seed"])
        mode = str(row["mode"])
        if mode == ADP_MODE:
            replicate = int(row["training_replicate"])
            adp.setdefault(worker, {}).setdefault(replicate, {})[seed] = value
        else:
            baselines.setdefault((mode, worker), {})[seed] = value
    return baselines, adp


def policy_worker_summary(
    rows: list[dict[str, object]], *, repetitions: int
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    modes = sorted({str(row["mode"]) for row in rows})
    workers = sorted({int(row["worker_count"]) for row in rows})
    for metric in METRICS:
        baselines, adp = _metric_data(rows, metric)
        for worker in workers:
            for mode in modes:
                between_training_sd = None
                if mode == ADP_MODE:
                    replicate_map = adp.get(worker, {})
                    replicates = sorted(replicate_map)
                    seeds = sorted(set.intersection(*(set(replicate_map[r]) for r in replicates))) if replicates else []
                    values = [statistics.fmean(replicate_map[r][seed] for r in replicates) for seed in seeds]
                    replicate_means = [statistics.fmean(replicate_map[r][seed] for seed in seeds) for r in replicates] if seeds else []
                    between_training_sd = statistics.stdev(replicate_means) if len(replicate_means) > 1 else None

                    def sample(rng: random.Random) -> float:
                        sampled_reps = [replicates[rng.randrange(len(replicates))] for _ in replicates]
                        sampled_seeds = [seeds[rng.randrange(len(seeds))] for _ in seeds]
                        return statistics.fmean(
                            statistics.fmean(replicate_map[rep][seed] for rep in sampled_reps)
                            for seed in sampled_seeds
                        )
                else:
                    seed_map = baselines.get((mode, worker), {})
                    seeds = sorted(seed_map)
                    values = [seed_map[seed] for seed in seeds]

                    def sample(rng: random.Random, seed_map=seed_map, seeds=seeds) -> float:
                        return statistics.fmean(seed_map[seeds[rng.randrange(len(seeds))]] for _ in seeds)

                if not values:
                    continue
                low, high, _ = _bootstrap_interval(
                    sample, label=f"summary|{metric}|{mode}|{worker}", repetitions=repetitions
                )
                output.append({
                    "metric": metric,
                    "mode": mode,
                    "worker_count": worker,
                    "environment_seed_count": len(values),
                    "training_replicate_count": len(adp.get(worker, {})) if mode == ADP_MODE else 0,
                    "mean": round(statistics.fmean(values), 8),
                    "std_across_seed_units": round(_sample_std(values), 8),
                    "between_training_replicate_sd": round(between_training_sd, 8) if between_training_sd is not None else "",
                    "min": round(min(values), 8),
                    "max": round(max(values), 8),
                    "ci95_low": round(low, 8),
                    "ci95_high": round(high, 8),
                })
    return output


def paired_contrasts(
    rows: list[dict[str, object]], *, repetitions: int
) -> list[dict[str, object]]:
    baselines, adp = _metric_data(rows, PRIMARY_METRIC)
    workers = sorted(adp)
    baseline_modes = sorted({mode for mode, _worker in baselines})
    output: list[dict[str, object]] = []
    for worker in workers:
        replicate_map = adp[worker]
        replicates = sorted(replicate_map)
        for baseline in baseline_modes:
            baseline_seed = baselines.get((baseline, worker), {})
            seeds = sorted(set(baseline_seed).intersection(*(set(replicate_map[r]) for r in replicates)))
            if not seeds:
                continue
            differences = [
                statistics.fmean(replicate_map[r][seed] for r in replicates) - baseline_seed[seed]
                for seed in seeds
            ]

            def sample(rng: random.Random) -> float:
                sampled_reps = [replicates[rng.randrange(len(replicates))] for _ in replicates]
                sampled_seeds = [seeds[rng.randrange(len(seeds))] for _ in seeds]
                return statistics.fmean(
                    statistics.fmean(replicate_map[r][seed] for r in sampled_reps) - baseline_seed[seed]
                    for seed in sampled_seeds
                )

            low, high, bootstrap = _bootstrap_interval(
                sample, label=f"contrast|{worker}|{baseline}", repetitions=repetitions
            )
            raw_p = _centered_bootstrap_p(bootstrap, statistics.fmean(differences))
            baseline_mean = statistics.fmean(baseline_seed[seed] for seed in seeds)
            output.append({
                "scope": "worker",
                "worker_count": worker,
                "baseline_mode": baseline,
                "seed_count": len(seeds),
                "training_replicate_count": len(replicates),
                "adp_minus_baseline_mean": round(statistics.fmean(differences), 8),
                "ci95_low": round(low, 8),
                "ci95_high": round(high, 8),
                "relative_improvement_pct": round(100.0 * statistics.fmean(differences) / baseline_mean, 8) if baseline_mean else "",
                "win_count": sum(value > 0 for value in differences),
                "tie_count": sum(value == 0 for value in differences),
                "loss_count": sum(value < 0 for value in differences),
                "bootstrap_two_sided_p": round(raw_p, 8),
                "holm_adjusted_p": "",
            })

    worker_rows = [row for row in output if row["scope"] == "worker"]
    ranked = sorted(enumerate(worker_rows), key=lambda item: float(item[1]["bootstrap_two_sided_p"]))
    running = 0.0
    total = len(ranked)
    for rank, (_index, row) in enumerate(ranked):
        adjusted = min(1.0, float(row["bootstrap_two_sided_p"]) * (total - rank))
        running = max(running, adjusted)
        row["holm_adjusted_p"] = round(running, 8)

    for baseline in baseline_modes:
        usable_workers = [
            worker for worker in workers
            if (baseline, worker) in baselines and worker in adp
        ]
        if not usable_workers:
            continue
        # One seed block spans every fleet size in a common-random-number design.
        common_seeds = sorted(set.intersection(*[
            set(baselines[(baseline, worker)]).intersection(*(set(values) for values in adp[worker].values()))
            for worker in usable_workers
        ]))
        if not common_seeds:
            continue

        def overall_sample(rng: random.Random) -> float:
            worker_effects: list[float] = []
            sampled_seeds = [common_seeds[rng.randrange(len(common_seeds))] for _ in common_seeds]
            for worker in usable_workers:
                replicate_map = adp[worker]
                replicates = sorted(replicate_map)
                baseline_seed = baselines[(baseline, worker)]
                sampled_reps = [replicates[rng.randrange(len(replicates))] for _ in replicates]
                worker_effects.append(statistics.fmean(
                    statistics.fmean(replicate_map[r][seed] for r in sampled_reps) - baseline_seed[seed]
                    for seed in sampled_seeds
                ))
            return statistics.fmean(worker_effects)

        point_effects: list[float] = []
        for worker in usable_workers:
            replicate_map = adp[worker]
            replicates = sorted(replicate_map)
            baseline_seed = baselines[(baseline, worker)]
            point_effects.append(statistics.fmean(
                statistics.fmean(replicate_map[r][seed] for r in replicates) - baseline_seed[seed]
                for seed in common_seeds
            ))
        low, high, bootstrap = _bootstrap_interval(
            overall_sample, label=f"overall|{baseline}", repetitions=repetitions
        )
        output.append({
            "scope": "overall_equal_worker_weight",
            "worker_count": ",".join(map(str, usable_workers)),
            "baseline_mode": baseline,
            "seed_count": len(common_seeds),
            "training_replicate_count": len(next(iter(adp[worker] for worker in usable_workers))),
            "adp_minus_baseline_mean": round(statistics.fmean(point_effects), 8),
            "ci95_low": round(low, 8),
            "ci95_high": round(high, 8),
            "relative_improvement_pct": "",
            "win_count": "",
            "tie_count": "",
            "loss_count": "",
            "bootstrap_two_sided_p": round(_centered_bootstrap_p(bootstrap, statistics.fmean(point_effects)), 8),
            "holm_adjusted_p": "",
        })
    return output


def summarize(prepared: Path, *, repetitions: int = 5000) -> dict[str, object]:
    rows, errors = collect_runs(prepared)
    experiment = _read_json(prepared / "experiment_plan.json")
    fairness, fairness_errors = fairness_report(
        rows,
        policies=[str(value) for value in experiment["policies"]],
        training_replicates=int(experiment["training_replicates"]),
    )
    errors.extend(fairness_errors)
    summary_rows = policy_worker_summary(rows, repetitions=repetitions) if rows else []
    contrasts = paired_contrasts(rows, repetitions=repetitions) if rows else []
    capacity = calculate_theoretical_capacity(
        scenario=str(experiment["scenario"]),
        worker_counts=[int(value) for value in experiment["worker_counts"]],
        horizon_days=int(experiment["horizon_days"]),
        minutes_per_day=float(experiment["minutes_per_day"]),
        resolved_config=yaml.safe_load((Path(rows[0]["run_dir"]) / ".hydra/config.yaml").read_text(encoding="utf-8")) if rows else None,
    )
    _write_csv(prepared / "evaluation_raw.csv", rows)
    _write_csv(prepared / "fairness_report.csv", fairness)
    _write_csv(prepared / "policy_worker_summary.csv", summary_rows)
    _write_csv(prepared / "paired_contrasts.csv", contrasts)
    (prepared / "theoretical_capacity.json").write_text(
        json.dumps(capacity, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    plan_count = len(_read_csv(prepared / "evaluation_plan.csv"))
    training_replicates = int(experiment["training_replicates"])
    payload: dict[str, object] = {
        "status": "pass" if not errors and len(rows) == plan_count else "fail",
        "expected_run_count": plan_count,
        "eligible_run_count": len(rows),
        "fairness_group_count": len(fairness),
        "fairness_pass_count": sum(bool(row["fairness_pass"]) for row in fairness),
        "bootstrap_repetitions": repetitions,
        "overall_resampling_unit": "common environment seed block across all worker counts",
        "p_value_method": "two-sided null-centered bootstrap with plus-one Monte Carlo correction; approximate",
        "adp_unit_of_analysis": (
            "environment seed after averaging independent training replicates"
            if training_replicates > 1
            else "environment seed for one fixed worker-specific ADP checkpoint"
        ),
        "uncertainty_method": (
            "hierarchical bootstrap over ADP training replicates and common environment seeds"
            if training_replicates > 1
            else "paired bootstrap over common environment seeds; training-seed uncertainty is not estimated"
        ),
        "theoretical_capacity_available": bool(capacity.get("available", False)),
        "simulation_validity": _read_json(prepared / "result_validity.json") if (prepared / "result_validity.json").is_file() else {"status": "not_independently_qualified"},
        "errors": errors,
    }
    (prepared / "analysis_summary.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize the confirmatory mfg_flow_shop experiment.")
    parser.add_argument("--prepared", type=Path, default=HERE / "prepared")
    parser.add_argument("--bootstrap-repetitions", type=int, default=5000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.bootstrap_repetitions < 100:
        raise ValueError("bootstrap repetitions must be at least 100")
    summary = summarize(args.prepared.resolve(), repetitions=args.bootstrap_repetitions)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
