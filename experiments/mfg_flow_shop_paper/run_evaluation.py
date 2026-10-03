from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import csv
import io
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import yaml

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(REPO_ROOT))

from experiments.factory_policy_comparison.run_experiment import validate_adp_held_out_seeds
from experiments.mfg_flow_shop_paper.render_standard_dashboard import render as render_dashboard, _experiment_timing
from experiments.mfg_flow_shop_paper.summarize_results import summarize
from manufacturing_sim.simulation.scenarios.manufacturing.entities import MACHINE_LIFECYCLE_CONTRACT
from manufacturing_sim.adp.live import atomic_json, atomic_text
from experiments.mfg_flow_shop_paper.evaluation_live import EvaluationMonitor


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_status(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "run_id", "mode", "worker_count", "seed", "training_replicate", "status",
        "return_code", "elapsed_sec", "run_dir", "checkpoint_path", "reason",
        "artifact_audit", "kpi_audit", "ntfs_compressed",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(path, handle.getvalue())


def _compact(run_dir: Path) -> bool:
    executable = shutil.which("compact.exe")
    if executable is None:
        return False
    completed = subprocess.run(
        [executable, "/c", "/i", "/q", f"/s:{run_dir}"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def _record_evaluation_timing(prepared: Path, prior: dict, *, jobs: int, started_at: str,
                              elapsed: float, status: str) -> None:
    prior_sec = float(prior.get("run_phase_wall_sec", 0.0))
    payload = {
        "started_at_utc": prior.get("started_at_utc", started_at),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "parallel_jobs": jobs, "session_status": status,
        "run_phase_wall_sec": round(prior_sec + elapsed, 3),
        "experiment_wall_sec_through_summary": round(prior_sec + elapsed, 3),
        "prior_evaluation_wall_sec": prior_sec,
        "last_session_wall_sec": round(elapsed, 3),
        "measurement": "sum of recorded evaluation session wall clocks including per-run audits",
    }
    atomic_json(prepared / "evaluation_timing.json", payload)


def _archive_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(f"{path.name}_incomplete_{stamp}")
    suffix = 1
    while target.exists():
        target = path.with_name(f"{path.name}_incomplete_{stamp}_{suffix}")
        suffix += 1
    shutil.move(str(path), str(target))
    return target


def _expected_sim_minutes(command: list[str]) -> float | None:
    values: dict[str, float] = {}
    for value in command:
        for key in ("scenario.horizon.num_days", "scenario.horizon.minutes_per_day"):
            prefix = key + "="
            if str(value).startswith(prefix):
                values[key] = float(str(value)[len(prefix):])
    if len(values) != 2:
        return None
    return values["scenario.horizon.num_days"] * values["scenario.horizon.minutes_per_day"]


def _command_overrides(command: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in command:
        text = str(item)
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        values[key.lstrip("+")] = value
    return values


def _scenario_override_errors(run_dir: Path, overrides: dict) -> list[str]:
    if not overrides:
        return []
    try:
        config = yaml.safe_load((run_dir / ".hydra/config.yaml").read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return [f"Cannot verify archived scenario overrides: {exc}"]
    errors = []
    for key, expected in overrides.items():
        actual = config
        for part in key.lstrip("+").split("."):
            actual = actual.get(part) if isinstance(actual, dict) else None
        if actual != expected:
            errors.append(f"{key}: {actual!r} != planned {expected!r}")
    return errors


def _completed_kpi_is_valid(path: Path, command: list[str]) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if payload.get("objective_status") != "complete":
        return False
    overrides = _command_overrides(command)
    inventory_keys = (
        "scenario.warehouse.material_shelf.capacity",
        "scenario.warehouse.material_shelf.initial_fill",
        "scenario.objective.throughput.restock_target_fill",
        "scenario.map.warehouse_height_tiles",
    )
    if _scenario_override_errors(path.parent, {
        key: int(overrides[key]) for key in inventory_keys if key in overrides
    }):
        return False
    run_meta = payload.get("run_meta", {})
    if not isinstance(run_meta, dict):
        return False
    if (run_meta.get("scenario_type") == "mfg_flow_shop"
            and run_meta.get("machine_lifecycle_contract") != MACHINE_LIFECYCLE_CONTRACT):
        return False
    identity_checks = {
        "scenario": str(run_meta.get("scenario_type", payload.get("scenario_type", ""))),
        "decision": str(run_meta.get("decision_mode", "")),
        "seed": str(run_meta.get("seed", "")),
        "scenario.objective.mode": str(run_meta.get("objective_mode", payload.get("objective_mode", ""))),
    }
    for key, actual in identity_checks.items():
        if key in overrides and actual != overrides[key]:
            return False
    if "scenario.factory.num_workers" in overrides:
        diagnostic_path = path.with_name("pre_run_diagnostics.json")
        try:
            diagnostics = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            inputs = diagnostics.get("inputs", {})
            worker_count = len(inputs.get("worker_ids", [])) if isinstance(inputs, dict) else -1
        except (OSError, ValueError, TypeError):
            return False
        if worker_count != int(overrides["scenario.factory.num_workers"]):
            return False
    expected = _expected_sim_minutes(command)
    if expected is None:
        return True
    return abs(float(payload.get("sim_elapsed_min", -1.0)) - expected) <= 1e-6


def _audit_run(run_dir: Path) -> tuple[str, str]:
    results: dict[str, str] = {}
    commands = [
        (
            "artifact_audit",
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "audit_run_artifacts.py"),
                str(run_dir),
                "--skip-replay-log",
            ],
        ),
        ("kpi_audit", [sys.executable, str(REPO_ROOT / "scripts" / "audit_kpi.py"), str(run_dir)]),
    ]
    with (run_dir / "paper_experiment_audit.log").open("w", encoding="utf-8") as log:
        for name, command in commands:
            completed = subprocess.run(command, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
            results[name] = "pass" if completed.returncode == 0 else "fail"
    return results["artifact_audit"], results["kpi_audit"]


def _preflight_adp_checkpoints(
    rows: list[dict[str, str]],
    *,
    allow_undeclared: bool = False,
    validator=validate_adp_held_out_seeds,
) -> list[dict[str, object]]:
    groups: dict[tuple[Path, int], set[int]] = {}
    for row in rows:
        if str(row.get("mode", "")) != "simulation_based_adp":
            continue
        checkpoint_text = str(row.get("checkpoint_path", "")).strip()
        if not checkpoint_text:
            raise RuntimeError(f"ADP evaluation row has no checkpoint: {row.get('run_id', '')}")
        key = (Path(checkpoint_text).expanduser().resolve(), int(row["worker_count"]))
        groups.setdefault(key, set()).add(int(row["seed"]))

    reports: list[dict[str, object]] = []
    for (checkpoint, worker_count), seeds in sorted(
        groups.items(), key=lambda item: (item[0][1], str(item[0][0]))
    ):
        report = validator(
            checkpoint,
            sorted(seeds),
            [worker_count],
            allow_undeclared=allow_undeclared,
        )
        reports.append({
            **report,
            "worker_count": worker_count,
            "requested_seeds": sorted(seeds),
        })
    for report in reports:
        if report.get("return_estimator") != "n_step_td":
            raise RuntimeError(f"Paper ADP checkpoint is not n-step TD: {report.get('checkpoint')}")
        if int(report.get("horizon_days", 0)) != 5:
            raise RuntimeError(f"Paper ADP checkpoint was not trained on the five-day horizon: {report.get('checkpoint')}")
        if int(report.get("n_step", 0)) != 30:
            raise RuntimeError(f"Paper ADP checkpoint does not use n=30: {report.get('checkpoint')}")
        if abs(float(report.get("target_tau", 0.0)) - 0.03) > 1e-12:
            raise RuntimeError(f"Paper ADP checkpoint does not use target_tau=0.03: {report.get('checkpoint')}")
    fingerprints_by_worker: dict[int, set[str]] = {}
    for report in reports:
        worker_count = int(report["worker_count"])
        mapping = report.get("environment_fingerprints_by_worker_count", {})
        fingerprint = str(mapping.get(str(worker_count), "")) if isinstance(mapping, dict) else ""
        if not fingerprint:
            raise RuntimeError(f"Paper ADP checkpoint has no environment fingerprint: {report.get('checkpoint')}")
        fingerprints_by_worker.setdefault(worker_count, set()).add(fingerprint)
    mismatched = {
        worker_count: sorted(values)
        for worker_count, values in fingerprints_by_worker.items()
        if len(values) != 1
    }
    if mismatched:
        raise RuntimeError(f"ADP training replicates used different environments: {mismatched}")
    return reports


def _run(row: dict[str, str], force: bool, audit: bool, compress: bool) -> dict[str, object]:
    run_dir = Path(row["run_dir"])
    kpi = run_dir / "kpi.json"
    command = json.loads(row["command_json"])
    result: dict[str, object] = {key: row.get(key, "") for key in (
        "run_id", "mode", "worker_count", "seed", "training_replicate", "run_dir", "checkpoint_path"
    )}
    if kpi.is_file() and not force and _completed_kpi_is_valid(kpi, command):
        artifact_audit, kpi_audit = _audit_run(run_dir) if audit else ("not_run", "not_run")
        status = "skipped_existing" if artifact_audit != "fail" and kpi_audit != "fail" else "audit_failed"
        compressed = _compact(run_dir) if status == "skipped_existing" and compress else False
        return {
            **result, "status": status, "return_code": 0, "elapsed_sec": 0.0,
            "reason": "" if status == "skipped_existing" else f"artifact_audit={artifact_audit}; kpi_audit={kpi_audit}",
            "artifact_audit": artifact_audit, "kpi_audit": kpi_audit, "ntfs_compressed": compressed,
        }
    checkpoint = row.get("checkpoint_path", "").strip()
    if checkpoint and not Path(checkpoint).is_file():
        return {
            **result, "status": "failed_dependency", "return_code": 2, "elapsed_sec": 0.0,
            "reason": f"missing checkpoint: {checkpoint}", "artifact_audit": "not_run",
            "kpi_audit": "not_run", "ntfs_compressed": False,
        }
    archived = _archive_existing(run_dir) if run_dir.exists() else None
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    with (run_dir / "experiment_console.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
    elapsed = round(time.perf_counter() - started, 3)
    status = "completed" if completed.returncode == 0 and kpi.is_file() else "failed"
    reason = "" if status == "completed" else f"return_code={completed.returncode}; kpi_exists={kpi.is_file()}"
    artifact_audit = "not_run"
    kpi_audit = "not_run"
    if status == "completed" and audit:
        artifact_audit, kpi_audit = _audit_run(run_dir)
        if artifact_audit != "pass" or kpi_audit != "pass":
            status = "audit_failed"
            reason = f"artifact_audit={artifact_audit}; kpi_audit={kpi_audit}"
    compressed = _compact(run_dir) if status == "completed" and compress else False
    return {
        **result, "status": status, "return_code": completed.returncode, "elapsed_sec": elapsed,
        "reason": (
            reason if reason else f"restarted_from={archived}" if archived is not None else ""
        ), "artifact_audit": artifact_audit, "kpi_audit": kpi_audit,
        "ntfs_compressed": compressed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the prepared confirmatory policy evaluations.")
    parser.add_argument("--prepared", type=Path, default=HERE / "prepared")
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--modes", nargs="+", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-undeclared-held-out-seeds", action="store_true",
        help="Allow an explicitly recorded test-seed extension; training/validation overlap is still rejected.",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--pending-only", action="store_true",
                        help="Retain validated successful statuses and execute only missing/failed runs.")
    parser.add_argument("--skip-audit", action="store_true")
    parser.add_argument("--no-compress", action="store_true")
    parser.add_argument("--no-open-dashboard", action="store_true")
    parser.add_argument("--no-live", action="store_true")
    return parser.parse_args()


def _pending_rows(rows: list[dict], previous: dict) -> list[dict]:
    pending = []
    for row in rows:
        status = previous.get(row["run_id"], {})
        if (status.get("status") in {"completed", "skipped_existing"}
                and status.get("artifact_audit") == "pass" and status.get("kpi_audit") == "pass"
                and _completed_kpi_is_valid(Path(row["run_dir"]) / "kpi.json", json.loads(row["command_json"]))):
            continue
        pending.append(row)
    return pending


@contextmanager
def _evaluation_lock(prepared: Path):
    # OS locks are released automatically after a crash or Windows restart.
    with (prepared / ".evaluation.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"An evaluator already owns this experiment: {prepared}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def main() -> int:
    args = parse_args()
    if args.dry_run:
        return _main(args)
    with _evaluation_lock(args.prepared.resolve()):
        return _main(args)


def _main(args) -> int:
    if args.jobs < 1:
        raise ValueError("jobs must be at least 1")
    if args.force and args.pending_only:
        raise ValueError("--force and --pending-only are mutually exclusive")
    prepared = args.prepared.resolve()
    all_rows = _read_rows(prepared / "evaluation_plan.csv")
    rows = all_rows
    if args.modes:
        unknown = set(args.modes) - {row["mode"] for row in all_rows}
        if unknown:
            raise ValueError(f"Modes absent from the evaluation plan: {sorted(unknown)}")
        rows = [row for row in rows if row["mode"] in args.modes]
        rows.sort(key=lambda row: (int(row["worker_count"]), int(row["seed"]), row["mode"]))
    if args.limit is not None:
        rows = rows[: max(0, args.limit)]
    if args.dry_run:
        for row in rows:
            print(row["command"])
        return 0
    status_path = prepared / "evaluation_status.csv"
    previous = {row["run_id"]: row for row in _read_rows(status_path)} if status_path.exists() else {}
    if args.pending_only:
        rows = _pending_rows(rows, previous)
    rows.sort(key=lambda row: (int(row["seed"]), int(row["worker_count"]), row["mode"]))
    selected_ids = {row["run_id"] for row in rows}
    planned_ids = {row["run_id"] for row in all_rows}
    statuses = [row for key, row in previous.items() if key in planned_ids and key not in selected_ids]
    live = None if args.no_live else EvaluationMonitor(prepared, all_rows, statuses, args.jobs)
    try:
        if live:
            live.start()
            if not args.no_open_dashboard:
                from manufacturing_sim.adp.background import open_monitor

                open_monitor(prepared / "live_evaluation.html")
        return _evaluate(args, prepared, all_rows, rows, previous, statuses, live)
    except Exception as exc:
        if live:
            live.set_phase("failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if live:
            live.close()


def _evaluate(args, prepared, all_rows, rows, previous, statuses, live) -> int:
    # Check all checkpoints even when some results are reused or only rule modes run.
    preflight = _preflight_adp_checkpoints(
        all_rows, allow_undeclared=args.allow_undeclared_held_out_seeds,
    )
    prior_timing = _experiment_timing(prepared, parallel_jobs=args.jobs) if (prepared / "evaluation_status.csv").is_file() else {}
    started_at = datetime.now(timezone.utc).isoformat()
    session_started = time.perf_counter()
    (prepared / "evaluation_preflight.json").write_text(
        json.dumps({"status": "pass", "checkpoints": preflight}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    status_path = prepared / "evaluation_status.csv"
    if live:
        live.set_phase("running")
    executor = ThreadPoolExecutor(max_workers=args.jobs)
    interrupted = False
    futures = {}
    try:
        futures = {
            (executor.submit(live.execute, _run, row, args.force, not args.skip_audit, not args.no_compress)
             if live else executor.submit(_run, row, args.force, not args.skip_audit, not args.no_compress)): row
            for row in rows
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    **{key: source.get(key, "") for key in (
                        "run_id", "mode", "worker_count", "seed", "training_replicate", "run_dir", "checkpoint_path"
                    )},
                    "status": "internal_error", "return_code": 1, "elapsed_sec": 0.0,
                    "reason": f"{type(exc).__name__}: {exc}", "artifact_audit": "not_run",
                    "kpi_audit": "not_run", "ntfs_compressed": False,
                }
            statuses.append(result)
            prior = previous.get(str(result["run_id"]), {})
            if result["status"] == "skipped_existing" and prior.get("elapsed_sec"):
                result["elapsed_sec"] = prior["elapsed_sec"]
            if live:
                live.finish_run(result)
            statuses.sort(key=lambda item: str(item["run_id"]))
            _write_status(status_path, statuses)
            _record_evaluation_timing(prepared, prior_timing, jobs=args.jobs, started_at=started_at,
                                      elapsed=time.perf_counter() - session_started, status="running")
            print(f"[{len(statuses)}/{len(all_rows)}] {result['run_id']}: {result['status']}", flush=True)
    except KeyboardInterrupt:
        interrupted = True
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        if statuses:
            _write_status(status_path, statuses)
        _record_evaluation_timing(prepared, prior_timing, jobs=args.jobs, started_at=started_at,
                                  elapsed=time.perf_counter() - session_started,
                                  status="interrupted" if interrupted else "finished")
    if interrupted:
        if live:
            live.set_phase("interrupted", "Execution interrupted; final comparison has not been verified.")
        return 130
    failures = [row for row in statuses if row["status"] not in {"completed", "skipped_existing"}]
    if failures:
        if live:
            live.set_phase("failed", f"{len(failures)} runs failed. Final comparison is unavailable.")
        return 1
    if len(statuses) != len(all_rows) or args.skip_audit:
        if live:
            live.set_phase("partial", "Full audited coverage is required before generating final statistics.")
        return 0
    if not args.skip_audit:
        if live:
            live.set_phase("postprocessing", "Verifying common seeds, computing paired statistics and building the dashboard.")
        analysis = summarize(prepared)
        if analysis["status"] != "pass":
            if live:
                live.set_phase("failed", "Final fairness/statistical audit failed; see analysis_summary.json.")
            return 1
        dashboard = render_dashboard(prepared)
        if live:
            live.set_phase("completed")
        if not args.no_open_dashboard:
            from manufacturing_sim.adp.background import open_monitor

            open_monitor(dashboard)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
