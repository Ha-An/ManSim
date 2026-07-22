from __future__ import annotations

import argparse
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
    discover_run_dirs,
    load_experiment_config,
    load_run_identity,
    read_json,
    run_subprocess,
    stable_hash,
    write_csv,
    write_json,
)


def _expected_lookup(cfg: ExperimentConfig) -> dict[tuple[str, int, int], dict[str, object]]:
    return {
        (mode, worker_count, seed): {
            "scenario": cfg.scenario,
            "worker_count": worker_count,
            "seed": seed,
            "total_days": cfg.horizon_days,
            "minutes_per_day": cfg.minutes_per_day,
        }
        for mode in cfg.modes
        for worker_count in cfg.worker_counts
        for seed in cfg.seeds
    }


def _pre_run_fingerprints(run_dir: Path) -> tuple[str, str]:
    diagnostics = read_json(run_dir / "pre_run_diagnostics.json")
    inputs = diagnostics.get("inputs", {}) if isinstance(diagnostics.get("inputs", {}), dict) else {}
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
        if key not in {"worker_task_allowlists", "battery"}
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
    }
    return stable_hash(payload), stable_hash(policy_payload)


def _run_audit_script(script_name: str, run_dir: Path) -> tuple[str, int, str]:
    command = [str(Path.cwd() / ".venv" / "Scripts" / "python.exe")]
    if not Path(command[0]).exists():
        import sys

        command = [sys.executable]
    command.extend([str((REPO_ROOT / "scripts" / script_name).resolve()), str(run_dir.resolve())])
    return_code, output = run_subprocess(command, cwd=REPO_ROOT)
    status = "pass" if return_code == 0 else "fail"
    return status, return_code, output.strip()


def audit_experiment(output_root: Path, cfg: ExperimentConfig) -> dict[str, object]:
    output_root = output_root.resolve()
    expected = _expected_lookup(cfg)
    run_dirs = discover_run_dirs(output_root)
    fairness_rows: list[dict[str, object]] = []
    audit_rows: list[dict[str, object]] = []

    for run_dir in run_dirs:
        identity = load_run_identity(run_dir)
        mode = str(identity["mode"])
        worker_count = int(identity["worker_count"])
        seed = int(identity["seed"])
        expected_row = expected.get((mode, worker_count, seed), {})
        pre_hash, policy_hash = _pre_run_fingerprints(run_dir) if (run_dir / "pre_run_diagnostics.json").exists() else ("", "")
        artifact_status = read_json(run_dir / "artifact_status.json")
        artifact_errors = artifact_status.get("errors", {}) if isinstance(artifact_status.get("errors", {}), dict) else {}
        fairness_rows.append(
            {
                "run_dir": str(run_dir.resolve()),
                "mode": mode,
                "worker_count": worker_count,
                "seed": seed,
                "scenario_ok": identity["scenario"] == expected_row.get("scenario"),
                "worker_count_ok": worker_count == int(expected_row.get("worker_count", -1)),
                "horizon_ok": int(identity["total_days"]) == int(expected_row.get("total_days", -1)),
                "minutes_per_day_ok": float(identity["minutes_per_day"]) == float(expected_row.get("minutes_per_day", -1)),
                "seed_ok": seed == int(expected_row.get("seed", -1)),
                "pre_run_environment_fingerprint": pre_hash,
                "pre_run_policy_fingerprint": policy_hash,
                "pre_run_policy_independent_ok": bool(pre_hash),
                "artifact_errors_empty": artifact_errors == {},
                "artifact_errors": artifact_errors,
            }
        )

        artifact_audit_status, artifact_return_code, artifact_output = _run_audit_script("audit_run_artifacts.py", run_dir)
        kpi_audit_status, kpi_return_code, kpi_output = _run_audit_script("audit_kpi.py", run_dir)
        audit_rows.append(
            {
                "run_dir": str(run_dir.resolve()),
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

    reference_hashes_by_worker_count: dict[int, set[str]] = {}
    for row in fairness_rows:
        worker_count = int(row.get("worker_count", 0) or 0)
        fingerprint = str(row.get("pre_run_environment_fingerprint", "") or "")
        if fingerprint:
            reference_hashes_by_worker_count.setdefault(worker_count, set()).add(fingerprint)
    for row in fairness_rows:
        worker_count = int(row.get("worker_count", 0) or 0)
        reference_hashes = reference_hashes_by_worker_count.get(worker_count, set())
        row["pre_run_matches_reference"] = len(reference_hashes) <= 1 and bool(row.get("pre_run_environment_fingerprint"))
        row["fairness_pass"] = all(
            bool(row.get(key))
            for key in [
                "scenario_ok",
                "worker_count_ok",
                "horizon_ok",
                "minutes_per_day_ok",
                "seed_ok",
                "pre_run_policy_independent_ok",
                "pre_run_matches_reference",
                "artifact_errors_empty",
            ]
        )

    write_csv(output_root / FAIRNESS_CSV, fairness_rows)
    write_csv(output_root / AUDIT_CSV, audit_rows)
    summary = {
        "run_count": len(run_dirs),
        "fairness_pass_count": sum(1 for row in fairness_rows if row.get("fairness_pass")),
        "artifact_audit_fail_count": sum(1 for row in audit_rows if row.get("artifact_audit_status") != "pass"),
        "kpi_audit_fail_count": sum(1 for row in audit_rows if row.get("kpi_audit_status") != "pass"),
        "pre_run_fingerprint_count_by_worker_count": {
            str(worker_count): len(fingerprints)
            for worker_count, fingerprints in sorted(reference_hashes_by_worker_count.items())
        },
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
        and summary.get("fairness_pass_count", 0) == summary.get("run_count", 0)
        and summary.get("artifact_audit_fail_count", 0) == 0
        and summary.get("kpi_audit_fail_count", 0) == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
