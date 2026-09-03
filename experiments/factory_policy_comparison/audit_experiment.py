from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.factory_policy_comparison.common import (
    AUDIT_CSV,
    DEFAULT_CONFIG_PATH,
    FAIRNESS_CSV,
    REPO_ROOT,
    ExperimentConfig,
    SCENARIO_DEFAULT_OBJECTIVE,
    discover_run_dirs,
    load_experiment_config,
    load_run_identity,
    read_json,
    run_subprocess,
    stable_hash,
    write_csv,
    write_json,
)


def _expected_lookup(cfg: ExperimentConfig) -> dict[tuple[str, str, int, int], dict[str, object]]:
    return {
        (objective_mode, mode, worker_count, seed): {
            "scenario": cfg.scenario,
            "objective_mode": objective_mode,
            "worker_count": worker_count,
            "seed": seed,
            "total_days": (
                cfg.makespan_max_sim_days
                if objective_mode == "minimize_makespan"
                else cfg.horizon_days
            ),
            "configured_throughput_days": (
                0 if objective_mode == SCENARIO_DEFAULT_OBJECTIVE else cfg.horizon_days
            ),
            "configured_max_sim_days": (
                0 if objective_mode == SCENARIO_DEFAULT_OBJECTIVE else cfg.makespan_max_sim_days
            ),
            "minutes_per_day": cfg.minutes_per_day,
        }
        for objective_mode in cfg.objective_modes
        for mode in cfg.modes
        for worker_count in cfg.worker_counts
        for seed in cfg.seeds
    }


def _objective_contract(
    run_dir: Path,
    identity: dict[str, object],
    cfg: ExperimentConfig,
    event_signature: dict[str, object],
) -> tuple[bool, str]:
    objective_mode = str(identity.get("objective_mode", SCENARIO_DEFAULT_OBJECTIVE))
    if objective_mode == SCENARIO_DEFAULT_OBJECTIVE:
        return True, "scenario default objective"
    kpi = read_json(run_dir / "kpi.json")
    if objective_mode == "maximize_throughput":
        expected_elapsed = float(cfg.horizon_days) * float(cfg.minutes_per_day)
        expected_restock_times = [
            round(day * float(cfg.minutes_per_day), 6)
            for day in range(cfg.horizon_days)
        ]
        checks = {
            "objective_status": str(kpi.get("objective_status", "")) == "complete",
            "termination_reason": str(kpi.get("termination_reason", "")) == "completed_horizon",
            "sim_elapsed_min": abs(float(kpi.get("sim_elapsed_min", -1.0)) - expected_elapsed) <= 1e-6,
            "restock_schedule": list(event_signature.get("restock_times", [])) == expected_restock_times,
            "makespan_not_applicable": str(kpi.get("makespan_status", "")) == "not_applicable",
        }
    elif objective_mode == "minimize_makespan":
        initial_count = int(kpi.get("initial_batch_material_count", 0) or 0)
        terminal_count = int(kpi.get("initial_batch_terminal_material_count", 0) or 0)
        checks = {
            "objective_status": str(kpi.get("objective_status", "")) == "complete",
            "termination_reason": str(kpi.get("termination_reason", ""))
            == "initial_material_batch_terminal_complete",
            "makespan_status": str(kpi.get("makespan_status", "")) == "complete",
            "makespan_present": kpi.get("makespan_min") not in {None, ""},
            "initial_batch_30": initial_count == 30,
            "terminal_batch_complete": terminal_count == initial_count,
            "batch_progress": abs(float(kpi.get("initial_batch_progress_ratio", -1.0)) - 1.0) <= 1e-9,
            "initial_fill_only": list(event_signature.get("restock_times", [])) == [0.0],
        }
    else:
        return False, f"unsupported objective_mode={objective_mode}"
    failures = [name for name, passed in checks.items() if not passed]
    return not failures, "pass" if not failures else ", ".join(failures)


def _stochastic_signature(run_dir: Path) -> dict[str, object]:
    run_meta = read_json(run_dir / "run_meta.json")
    streams = run_meta.get("stochastic_streams", {})
    streams = streams if isinstance(streams, dict) else {}
    compact = run_meta.get("event_audit_signature", {})
    compact = compact if isinstance(compact, dict) else {}
    quality = [str(value) for value in compact.get("quality", [])]
    repair_samples: dict[str, list[float]] = defaultdict(list)
    for machine_id, values in (
        compact.get("repair_samples", {})
        if isinstance(compact.get("repair_samples", {}), dict)
        else {}
    ).items():
        repair_samples[str(machine_id)].extend(float(value) for value in values)
    restock_times = [float(value) for value in compact.get("restock_times", [])]
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        quality.clear()
        repair_samples.clear()
        restock_times.clear()
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type", ""))
                if event_type in {"INSPECT_PASS", "INSPECT_FAIL"}:
                    quality.append("P" if event_type == "INSPECT_PASS" else "F")
                elif event_type == "MACHINE_REPAIR_START":
                    details = event.get("details", {})
                    details = details if isinstance(details, dict) else {}
                    machine_id = str(event.get("entity_id", ""))
                    if machine_id and details.get("repair_total_min") is not None:
                        repair_samples[machine_id].append(round(float(details["repair_total_min"]), 6))
                elif event_type == "WAREHOUSE_MATERIAL_RESTOCK":
                    restock_times.append(round(float(event.get("t", 0.0)), 6))
    return {
        "isolated_streams": streams.get("scheme") == "isolated_v1",
        "quality": quality,
        "repair_samples": dict(repair_samples),
        "restock_times": restock_times,
    }


def _common_prefix_equal(sequences: list[list[object]]) -> bool:
    if not sequences or any(not sequence for sequence in sequences):
        return False
    prefix_length = min(len(sequence) for sequence in sequences)
    return len({tuple(sequence[:prefix_length]) for sequence in sequences}) == 1


def _observed_prefixes_consistent(sequences: list[list[object]]) -> bool:
    """Compare stochastic prefixes without penalizing runs with no observations yet."""
    nonempty = [sequence for sequence in sequences if sequence]
    return len(nonempty) < 2 or _common_prefix_equal(nonempty)


def _pre_run_fingerprints(run_dir: Path) -> tuple[str, str]:
    diagnostics = read_json(run_dir / "pre_run_diagnostics.json")
    inputs = diagnostics.get("inputs", {}) if isinstance(diagnostics.get("inputs", {}), dict) else {}
    policy_runtime = (
        diagnostics.get("policy_runtime", {})
        if isinstance(diagnostics.get("policy_runtime", {}), dict)
        else {}
    )
    battery_inputs = inputs.get("battery", {}) if isinstance(inputs.get("battery", {}), dict) else {}
    environment_battery = {
        key: value
        for key, value in battery_inputs.items()
        if key not in {"delivery_provider_agent_ids", "delivery_receiver_agent_ids"}
    }
    policy_battery = {
        key: value
        for key, value in battery_inputs.items()
        if key in {"delivery_provider_agent_ids", "delivery_receiver_agent_ids"}
    }
    environment_inputs = {
        key: value
        for key, value in inputs.items()
        if key
        not in {
            "worker_task_allowlists",
            "mfg_flow_shop_task_policy",
            "rolling_horizon_scheduler",
            "battery",
        }
    }
    if environment_battery:
        environment_inputs["battery"] = environment_battery
    payload = {
        "scenario_type": diagnostics.get("scenario_type"),
        "supported": diagnostics.get("supported"),
        "metric_order": diagnostics.get("metric_order"),
        "inputs": environment_inputs,
    }
    policy_payload = {
        "battery_delivery_policy": policy_battery,
        "worker_task_allowlists": inputs.get("worker_task_allowlists", {}),
        "mfg_flow_shop_task_policy": inputs.get("mfg_flow_shop_task_policy", {}),
        "rolling_horizon_scheduler": inputs.get(
            "rolling_horizon_scheduler",
            policy_runtime.get("rolling_horizon_scheduler", {}),
        ),
    }
    return stable_hash(payload), stable_hash(policy_payload)


def _run_audit_script(script_name: str, run_dir: Path) -> tuple[str, int, str]:
    command = [str(Path.cwd() / ".venv" / "Scripts" / "python.exe")]
    if not Path(command[0]).exists():
        import sys

        command = [sys.executable]
    command.extend([str((REPO_ROOT / "scripts" / script_name).resolve()), str(run_dir.resolve())])
    if script_name == "audit_run_artifacts.py":
        command.append("--skip-replay-log")
    return_code, output = run_subprocess(command, cwd=REPO_ROOT)
    status = "pass" if return_code == 0 else "fail"
    return status, return_code, output.strip()


def audit_experiment(output_root: Path, cfg: ExperimentConfig) -> dict[str, object]:
    output_root = output_root.resolve()
    expected = _expected_lookup(cfg)
    run_dirs = discover_run_dirs(output_root)
    fairness_rows: list[dict[str, object]] = []
    audit_rows: list[dict[str, object]] = []
    stochastic_by_run: dict[str, dict[str, object]] = {}

    for run_dir in run_dirs:
        identity = load_run_identity(run_dir)
        mode = str(identity["mode"])
        objective_mode = str(identity["objective_mode"])
        worker_count = int(identity["worker_count"])
        seed = int(identity["seed"])
        expected_row = expected.get((objective_mode, mode, worker_count, seed), {})
        pre_hash, policy_hash = _pre_run_fingerprints(run_dir) if (run_dir / "pre_run_diagnostics.json").exists() else ("", "")
        artifact_status = read_json(run_dir / "artifact_status.json")
        artifact_errors = artifact_status.get("errors", {})
        stochastic_by_run[str(run_dir.resolve())] = _stochastic_signature(run_dir)
        objective_contract_ok, objective_contract_details = _objective_contract(
            run_dir,
            identity,
            cfg,
            stochastic_by_run[str(run_dir.resolve())],
        )
        fairness_rows.append(
            {
                "run_dir": str(run_dir.resolve()),
                "objective_mode": objective_mode,
                "mode": mode,
                "worker_count": worker_count,
                "seed": seed,
                "scenario_ok": identity["scenario"] == expected_row.get("scenario"),
                "objective_mode_ok": objective_mode == expected_row.get("objective_mode"),
                "worker_count_ok": worker_count == int(expected_row.get("worker_count", -1)),
                "horizon_ok": int(identity["total_days"]) == int(expected_row.get("total_days", -1)),
                "configured_throughput_days_ok": int(identity["configured_throughput_days"])
                == int(expected_row.get("configured_throughput_days", 0)),
                "configured_max_sim_days_ok": int(identity["configured_max_sim_days"])
                == int(expected_row.get("configured_max_sim_days", 0)),
                "minutes_per_day_ok": float(identity["minutes_per_day"]) == float(expected_row.get("minutes_per_day", -1)),
                "seed_ok": seed == int(expected_row.get("seed", -1)),
                "timing_profile_fingerprint": str(identity.get("timing_profile_fingerprint", "")),
                "pre_run_environment_fingerprint": pre_hash,
                "pre_run_policy_fingerprint": policy_hash,
                "pre_run_policy_independent_ok": bool(pre_hash),
                "objective_contract_ok": objective_contract_ok,
                "objective_contract_details": objective_contract_details,
                "stochastic_stream_scheme_ok": bool(stochastic_by_run[str(run_dir.resolve())]["isolated_streams"]),
                "artifact_errors_empty": not bool(artifact_errors),
                "artifact_errors": artifact_errors,
            }
        )

        artifact_audit_status, artifact_return_code, artifact_output = _run_audit_script("audit_run_artifacts.py", run_dir)
        kpi_audit_status, kpi_return_code, kpi_output = _run_audit_script("audit_kpi.py", run_dir)
        audit_rows.append(
            {
                "run_dir": str(run_dir.resolve()),
                "objective_mode": objective_mode,
                "mode": mode,
                "worker_count": worker_count,
                "seed": seed,
                "artifact_audit_status": artifact_audit_status,
                "artifact_audit_return_code": artifact_return_code,
                "kpi_audit_status": kpi_audit_status,
                "kpi_audit_return_code": kpi_return_code,
                "artifact_audit_tail": artifact_output[-1000:],
                "kpi_audit_tail": kpi_output[-1000:],
            }
        )

    reference_hashes_by_group: dict[tuple[str, int, int], set[str]] = {}
    timing_fingerprints = {
        str(row.get("timing_profile_fingerprint", ""))
        for row in fairness_rows
        if str(row.get("timing_profile_fingerprint", ""))
    }
    for row in fairness_rows:
        worker_count = int(row.get("worker_count", 0) or 0)
        objective_mode = str(row.get("objective_mode", ""))
        seed = int(row.get("seed", 0) or 0)
        fingerprint = str(row.get("pre_run_environment_fingerprint", "") or "")
        if fingerprint:
            reference_hashes_by_group.setdefault((objective_mode, worker_count, seed), set()).add(fingerprint)

    rows_by_group: dict[tuple[str, int, int], list[dict[str, object]]] = defaultdict(list)
    for row in fairness_rows:
        rows_by_group[
            (
                str(row.get("objective_mode", "")),
                int(row.get("worker_count", 0) or 0),
                int(row.get("seed", 0) or 0),
            )
        ].append(row)
    for group_rows in rows_by_group.values():
        signatures = [stochastic_by_run[str(row["run_dir"])] for row in group_rows]
        quality_matches = _observed_prefixes_consistent(
            [list(signature.get("quality", [])) for signature in signatures]
        )
        machine_ids = sorted(
            {
                machine_id
                for signature in signatures
                for machine_id in dict(signature.get("repair_samples", {})).keys()
            }
        )
        repair_matches = True
        for machine_id in machine_ids:
            sequences = [
                list(dict(signature.get("repair_samples", {})).get(machine_id, []))
                for signature in signatures
            ]
            nonempty = [sequence for sequence in sequences if sequence]
            if len(nonempty) >= 2 and not _common_prefix_equal(nonempty):
                repair_matches = False
                break
        for row in group_rows:
            row["quality_stream_prefix_matches"] = quality_matches
            row["repair_stream_prefix_matches"] = repair_matches

    for row in fairness_rows:
        worker_count = int(row.get("worker_count", 0) or 0)
        group = (
            str(row.get("objective_mode", "")),
            worker_count,
            int(row.get("seed", 0) or 0),
        )
        reference_hashes = reference_hashes_by_group.get(group, set())
        row["pre_run_matches_reference"] = len(reference_hashes) <= 1 and bool(row.get("pre_run_environment_fingerprint"))
        row["timing_profile_matches_reference"] = len(timing_fingerprints) == 1 and bool(
            row.get("timing_profile_fingerprint")
        )
        row["fairness_pass"] = all(
            bool(row.get(key))
            for key in [
                "scenario_ok",
                "objective_mode_ok",
                "worker_count_ok",
                "horizon_ok",
                "configured_throughput_days_ok",
                "configured_max_sim_days_ok",
                "minutes_per_day_ok",
                "seed_ok",
                "pre_run_policy_independent_ok",
                "pre_run_matches_reference",
                "timing_profile_matches_reference",
                "objective_contract_ok",
                "stochastic_stream_scheme_ok",
                "quality_stream_prefix_matches",
                "repair_stream_prefix_matches",
                "artifact_errors_empty",
            ]
        )

    write_csv(output_root / FAIRNESS_CSV, fairness_rows)
    write_csv(output_root / AUDIT_CSV, audit_rows)
    summary = {
        "expected_run_count": len(expected),
        "run_count": len(run_dirs),
        "missing_run_count": max(0, len(expected) - len(run_dirs)),
        "unexpected_run_count": sum(
            1
            for row in fairness_rows
            if (
                str(row.get("objective_mode", "")),
                str(row.get("mode", "")),
                int(row.get("worker_count", 0) or 0),
                int(row.get("seed", 0) or 0),
            )
            not in expected
        ),
        "fairness_pass_count": sum(1 for row in fairness_rows if row.get("fairness_pass")),
        "artifact_audit_fail_count": sum(1 for row in audit_rows if row.get("artifact_audit_status") != "pass"),
        "kpi_audit_fail_count": sum(1 for row in audit_rows if row.get("kpi_audit_status") != "pass"),
        "pre_run_fingerprint_count_by_objective_worker_seed": {
            f"{objective_mode}|{worker_count}|{seed}": len(fingerprints)
            for (objective_mode, worker_count, seed), fingerprints in sorted(reference_hashes_by_group.items())
        },
        "timing_profile_fingerprint_count": len(timing_fingerprints),
    }
    write_json(output_root / "audit_experiment_summary.json", summary)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit a factory policy comparison result root.")
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_experiment_config(args.config)
    summary = audit_experiment(args.output_root, cfg)
    print(summary)
    return (
        0
        if summary.get("run_count", 0) > 0
        and summary.get("run_count", 0) == summary.get("expected_run_count", 0)
        and summary.get("fairness_pass_count", 0) == summary.get("run_count", 0)
        and summary.get("unexpected_run_count", 0) == 0
        and summary.get("artifact_audit_fail_count", 0) == 0
        and summary.get("kpi_audit_fail_count", 0) == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
