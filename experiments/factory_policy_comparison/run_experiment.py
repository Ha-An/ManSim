from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import os
import shutil
import subprocess
import time
import webbrowser
from dataclasses import replace
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.factory_policy_comparison.audit_experiment import audit_experiment
from experiments.factory_policy_comparison.common import (
    DEFAULT_CONFIG_PATH,
    REPO_ROOT,
    STATUS_CSV,
    build_run_command,
    build_run_specs,
    check_optimizer_dependency,
    load_experiment_config,
    read_csv,
    resolve_results_root,
    run_subprocess,
    timestamp,
    upsert_csv,
    write_json,
)
from experiments.factory_policy_comparison.render_dashboard import render_dashboard
from experiments.factory_policy_comparison.summarize_results import summarize_results


STATUS_FIELDS = [
    "run_id",
    "objective_mode",
    "mode",
    "worker_count",
    "seed",
    "scenario",
    "horizon_days",
    "makespan_max_sim_days",
    "rolling_window_min",
    "run_dir",
    "status",
    "return_code",
    "elapsed_sec",
    "reason",
    "command",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the factory policy comparison experiment suite.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--stamp", default=None, help="Result folder name. Defaults to current timestamp.")
    parser.add_argument("--modes", nargs="*", default=None, help="Optional subset of decision modes.")
    parser.add_argument("--objectives", nargs="*", default=None, help="Optional subset of objective modes.")
    parser.add_argument("--seeds", nargs="*", type=int, default=None, help="Optional subset of seeds.")
    parser.add_argument("--worker-counts", nargs="*", type=int, default=None, help="Optional subset of worker counts.")
    parser.add_argument("--adp-checkpoint", type=Path, default=None, help="Checkpoint for simulation_based_adp runs.")
    parser.add_argument(
        "--allow-undeclared-held-out-seeds",
        action="store_true",
        help=(
            "Allow comparison seeds not listed in the checkpoint held-out partition. "
            "Training/validation overlap is still rejected and undeclared seeds are recorded in experiment_plan.json."
        ),
    )
    parser.add_argument("--days", type=int, default=None, help="Override horizon days.")
    parser.add_argument("--makespan-max-days", type=int, default=None, help="Override the makespan safety limit.")
    parser.add_argument(
        "--rolling-window-min",
        type=float,
        default=None,
        help="Override window_min for rolling-horizon modes only.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N mode/seed combinations.")
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of independent simulation runs to execute concurrently.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without creating result files.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip runs whose kpi.json already exists.")
    parser.add_argument("--force-rerun", action="store_true", help="Run selected specs even when output artifacts already exist.")
    parser.add_argument(
        "--compact-after-run",
        action="store_true",
        help="On Windows, compress each run directory after it finishes to reduce disk pressure.",
    )
    parser.add_argument("--no-postprocess", action="store_true", help="Do not audit, summarize, or render dashboard after runs.")
    parser.add_argument(
        "--no-open-dashboard",
        action="store_true",
        help="Generate the comparison dashboard without opening it in the default browser.",
    )
    return parser.parse_args()


def open_experiment_dashboard(path: Path) -> bool:
    dashboard = path.resolve()
    if not dashboard.exists():
        return False
    try:
        if webbrowser.open(dashboard.as_uri(), new=2):
            return True
    except Exception:
        pass
    if os.name == "nt":
        try:
            os.startfile(str(dashboard))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    return False


def compact_run_dir(run_dir: Path) -> None:
    if not run_dir.exists() or shutil.which("compact.exe") is None:
        return
    subprocess.run(
        ["compact.exe", "/c", f"/s:{run_dir}"],
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def experiment_audit_failed(summary: dict[str, object]) -> bool:
    run_count = int(summary.get("run_count", 0) or 0)
    expected_run_count = int(summary.get("expected_run_count", run_count) or 0)
    fairness_pass_count = int(summary.get("fairness_pass_count", 0) or 0)
    return (
        run_count == 0
        or run_count != expected_run_count
        or int(summary.get("unexpected_run_count", 0) or 0) > 0
        or fairness_pass_count != run_count
        or int(summary.get("artifact_audit_fail_count", 0) or 0) > 0
        or int(summary.get("kpi_audit_fail_count", 0) or 0) > 0
    )


def validate_adp_held_out_seeds(
    checkpoint_path: Path,
    held_out_seeds: list[int],
    worker_counts: list[int] | None = None,
    *,
    allow_undeclared: bool = False,
) -> dict[str, object]:
    def partition_values(value: object) -> list[int]:
        if isinstance(value, dict):
            raw_values = value.get("values", [])
        elif isinstance(value, list):
            raw_values = value
        else:
            raw_values = []
        return [int(item) for item in raw_values if str(item).strip()]

    try:
        import torch
    except ModuleNotFoundError as exc:
        raise RuntimeError("ADP comparison requires PyTorch from requirements-adp.txt") from exc
    path = checkpoint_path.expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"ADP checkpoint does not exist: {path}")
    payload = torch.load(path, map_location="cpu", weights_only=False)
    manifest = payload.get("manifest", {}) if isinstance(payload, dict) else {}
    partitions = manifest.get("seed_partitions", {}) if isinstance(manifest.get("seed_partitions", {}), dict) else {}
    if not partitions:
        raise RuntimeError("ADP checkpoint is missing required seed partition metadata")
    if partitions.get("disjoint") is False:
        raise RuntimeError("ADP checkpoint reports overlapping seed partitions")
    used: set[int] = set()
    for name in ("training", "screening_validation", "final_selection_validation"):
        used.update(partition_values(partitions.get(name)))
    overlap = sorted(used & {int(seed) for seed in held_out_seeds})
    if overlap:
        raise RuntimeError(f"held-out experiment seeds overlap ADP training/validation seeds: {overlap}")
    declared_held_out = set(partition_values(partitions.get("held_out_test")))
    requested_held_out = {int(seed) for seed in held_out_seeds}
    undeclared = sorted(requested_held_out - declared_held_out)
    if undeclared and not allow_undeclared:
        raise RuntimeError(
            f"experiment seeds are not declared in the checkpoint held-out partition: {undeclared}"
        )
    requested_worker_counts = sorted({int(value) for value in (worker_counts or [3])})
    supported_worker_counts = manifest.get("supported_worker_counts")
    if isinstance(supported_worker_counts, list) and supported_worker_counts:
        supported = sorted({int(value) for value in supported_worker_counts})
    else:
        worker_range = manifest.get("worker_count_range", [])
        if not isinstance(worker_range, list) or len(worker_range) != 2 or worker_range[0] != worker_range[1]:
            raise RuntimeError(
                "ADP checkpoint must declare exact supported_worker_counts for a multi-fleet comparison; "
                f"got worker_count_range={worker_range}"
            )
        supported = [int(worker_range[0])]
    unsupported = sorted(set(requested_worker_counts) - set(supported))
    if unsupported:
        raise RuntimeError(
            "ADP checkpoint does not support requested worker counts: "
            f"unsupported={unsupported}, supported={supported}"
        )
    environment_by_worker = manifest.get("environment_fingerprints_by_worker_count", {})
    if len(requested_worker_counts) > 1:
        missing_environment = [
            worker_count
            for worker_count in requested_worker_counts
            if not isinstance(environment_by_worker, dict)
            or not str(environment_by_worker.get(str(worker_count), ""))
        ]
        if missing_environment:
            raise RuntimeError(
                "ADP checkpoint is missing worker-specific environment fingerprints: "
                f"{missing_environment}"
            )
    if bool(manifest.get("wait_action_enabled", False)):
        raise RuntimeError("Fair policy comparison requires an ADP checkpoint with WAIT disabled")
    if str(manifest.get("worker_order_strategy", "")) != "cyclic":
        raise RuntimeError("Fair policy comparison requires worker_order_strategy=cyclic")
    return {
        "checkpoint": str(path),
        "checkpoint_id": str(manifest.get("checkpoint_id", "")),
        "held_out_seed_count": len(held_out_seeds),
        "overlap": overlap,
        "seed_partitions_present": True,
        "declared_held_out_seed_count": len(declared_held_out),
        "undeclared_held_out_seeds": undeclared,
        "held_out_declaration_enforced": not allow_undeclared,
        "supported_worker_counts": supported,
        "requested_worker_counts": requested_worker_counts,
        "wait_action_enabled": False,
        "worker_order_strategy": "cyclic",
        "return_estimator": str(manifest.get("return_estimator", "")),
        "horizon_days": int(manifest.get("horizon_days", 0) or 0),
        "feature_schema_version": str(manifest.get("feature_schema_version", "")),
        "environment_fingerprints_by_worker_count": {
            str(worker_count): str(environment_by_worker.get(str(worker_count), ""))
            for worker_count in requested_worker_counts
        },
        "n_step": int((manifest.get("training", {}) or {}).get("n_step", 0) or 0),
        "target_tau": float((manifest.get("training", {}) or {}).get("target_tau", 0.0) or 0.0),
    }


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise ValueError("jobs must be at least 1")
    cfg = load_experiment_config(args.config)
    if cfg.benchmark_seeds_locked and args.seeds is not None:
        requested_seeds = [int(seed) for seed in args.seeds]
        if requested_seeds != cfg.seeds:
            raise ValueError(
                "benchmark seeds are locked by the experiment config: "
                f"expected={cfg.seeds}, requested={requested_seeds}"
            )
    effective_cfg = replace(
        cfg,
        horizon_days=int(args.days if args.days is not None else cfg.horizon_days),
        makespan_max_sim_days=int(
            args.makespan_max_days if args.makespan_max_days is not None else cfg.makespan_max_sim_days
        ),
        modes=[str(mode) for mode in (args.modes if args.modes is not None else cfg.modes)],
        objective_modes=[
            str(mode) for mode in (args.objectives if args.objectives is not None else cfg.objective_modes)
        ],
        seeds=[int(seed) for seed in (args.seeds if args.seeds is not None else cfg.seeds)],
        worker_counts=[int(count) for count in (args.worker_counts if args.worker_counts is not None else cfg.worker_counts)],
    )
    output_root = resolve_results_root(args.output_root, stamp=args.stamp or timestamp())
    specs = build_run_specs(
        cfg,
        output_root,
        modes=args.modes,
        objective_modes=args.objectives,
        seeds=args.seeds,
        worker_counts=args.worker_counts,
        days=args.days,
        makespan_max_days=args.makespan_max_days,
        limit=args.limit,
    )
    if args.adp_checkpoint is not None:
        specs = [replace(spec, adp_checkpoint_path=str(args.adp_checkpoint.resolve())) for spec in specs]
    if specs:
        effective_cfg = replace(
            effective_cfg,
            objective_modes=list(dict.fromkeys(spec.objective_mode for spec in specs)),
            modes=list(dict.fromkeys(spec.mode for spec in specs)),
            worker_counts=sorted({spec.worker_count for spec in specs}),
            seeds=sorted({spec.seed for spec in specs}),
        )

    adp_preflight: dict[str, object] = {}
    adp_specs = [spec for spec in specs if spec.mode == "simulation_based_adp"]
    if adp_specs and not args.dry_run:
        checkpoint_values = {str(spec.adp_checkpoint_path).strip() for spec in adp_specs}
        if len(checkpoint_values) != 1 or not next(iter(checkpoint_values), ""):
            raise RuntimeError("all simulation_based_adp runs must use one explicit checkpoint")
        adp_preflight = validate_adp_held_out_seeds(
            Path(next(iter(checkpoint_values))),
            sorted({spec.seed for spec in specs}),
            sorted({spec.worker_count for spec in adp_specs}),
            allow_undeclared=(
                bool(args.allow_undeclared_held_out_seeds)
                or bool(cfg.benchmark_seeds_locked)
            ),
        )

    if args.dry_run:
        for spec in specs:
            command = build_run_command(
                spec,
                cfg.common_overrides,
                rolling_window_min=args.rolling_window_min,
            )
            print(" ".join(command))
        return 0

    experiment_started_perf = time.perf_counter()
    experiment_started_at = datetime.now(timezone.utc).isoformat()
    output_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.config, output_root / "experiment_config.yaml")
    write_json(
        output_root / "experiment_plan.json",
        {
            "scenario": cfg.scenario,
            "horizon_days": effective_cfg.horizon_days,
            "makespan_max_sim_days": effective_cfg.makespan_max_sim_days,
            "minutes_per_day": cfg.minutes_per_day,
            "objective_modes": list(dict.fromkeys(spec.objective_mode for spec in specs)),
            "modes": list(dict.fromkeys(spec.mode for spec in specs)),
            "worker_counts": sorted({spec.worker_count for spec in specs}),
            "seeds": sorted({spec.seed for spec in specs}),
            "benchmark_seeds_locked": bool(cfg.benchmark_seeds_locked),
            "run_count": len(specs),
            "parallel_jobs": int(args.jobs),
            "rolling_window_min": args.rolling_window_min,
            "adp_preflight": adp_preflight,
            "policy_contract": {
                "explicit_wait_action_enabled": False,
                "forced_idle_without_feasible_task_allowed": True,
                "beam_worker_order_strategy": "cyclic",
            },
        },
    )

    status_path = output_root / STATUS_CSV
    pending_runs: list[tuple[object, list[str], str]] = []
    for spec in specs:
        command = build_run_command(
            spec,
            cfg.common_overrides,
            rolling_window_min=args.rolling_window_min,
        )
        command_text = " ".join(command)
        dependency_ok, dependency_reason = check_optimizer_dependency(spec.mode)
        if not dependency_ok:
            upsert_csv(
                status_path,
                {
                    "run_id": spec.run_id,
                    "objective_mode": spec.objective_mode,
                    "mode": spec.mode,
                    "worker_count": spec.worker_count,
                    "seed": spec.seed,
                    "scenario": spec.scenario,
                    "horizon_days": spec.horizon_days,
                    "makespan_max_sim_days": spec.makespan_max_sim_days,
                    "rolling_window_min": args.rolling_window_min if spec.mode.startswith("rolling_horizon_") else "",
                    "run_dir": str(spec.run_dir.resolve()),
                    "status": "failed_dependency",
                    "return_code": "",
                    "elapsed_sec": 0,
                    "reason": dependency_reason,
                    "command": command_text,
                },
                STATUS_FIELDS,
                key="run_id",
            )
            continue
        if args.skip_existing and not args.force_rerun and (spec.run_dir / "kpi.json").exists():
            existing_status_rows = read_csv(status_path)
            existing_row = next((row for row in existing_status_rows if row.get("run_id") == spec.run_id), None)
            if existing_row and existing_row.get("status") == "completed":
                continue
            if existing_row is None:
                upsert_csv(
                    status_path,
                    {
                        "run_id": spec.run_id,
                        "objective_mode": spec.objective_mode,
                        "mode": spec.mode,
                        "worker_count": spec.worker_count,
                        "seed": spec.seed,
                        "scenario": spec.scenario,
                        "horizon_days": spec.horizon_days,
                        "makespan_max_sim_days": spec.makespan_max_sim_days,
                        "rolling_window_min": args.rolling_window_min if spec.mode.startswith("rolling_horizon_") else "",
                        "run_dir": str(spec.run_dir.resolve()),
                        "status": "skipped_existing",
                        "return_code": "",
                        "elapsed_sec": 0,
                        "reason": "kpi.json already exists",
                        "command": command_text,
                    },
                    STATUS_FIELDS,
                    key="run_id",
                )
                continue

        pending_runs.append((spec, command, command_text))

    def execute_run(item: tuple[object, list[str], str]) -> dict[str, object]:
        spec, command, command_text = item
        started = time.perf_counter()
        return_code, output = run_subprocess(
            command,
            cwd=REPO_ROOT,
            log_path=spec.run_dir / "experiment_stdout.log",
        )
        elapsed = round(time.perf_counter() - started, 3)
        status = "completed" if return_code == 0 and (spec.run_dir / "kpi.json").exists() else "failed"
        reason = "" if status == "completed" else (
            output.strip().splitlines()[-1] if output.strip() else "missing kpi.json"
        )
        if args.compact_after_run:
            compact_run_dir(spec.run_dir)
        return {
                "run_id": spec.run_id,
                "objective_mode": spec.objective_mode,
                "mode": spec.mode,
                "worker_count": spec.worker_count,
                "seed": spec.seed,
                "scenario": spec.scenario,
                "horizon_days": spec.horizon_days,
                "makespan_max_sim_days": spec.makespan_max_sim_days,
                "rolling_window_min": args.rolling_window_min if spec.mode.startswith("rolling_horizon_") else "",
                "run_dir": str(spec.run_dir.resolve()),
                "status": status,
                "return_code": return_code,
                "elapsed_sec": elapsed,
                "reason": reason,
                "command": command_text,
        }

    if args.jobs == 1:
        completed_rows = (execute_run(item) for item in pending_runs)
        for row in completed_rows:
            upsert_csv(status_path, row, STATUS_FIELDS, key="run_id")
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as executor:
            futures = {executor.submit(execute_run, item): item[0] for item in pending_runs}
            for future in as_completed(futures):
                row = future.result()
                upsert_csv(status_path, row, STATUS_FIELDS, key="run_id")
                print(
                    f"completed: {row['run_id']} status={row['status']} "
                    f"elapsed_sec={row['elapsed_sec']}"
                )

    run_phase_wall_sec = time.perf_counter() - experiment_started_perf

    postprocess_failed = False
    if not args.no_postprocess:
        audit_summary = audit_experiment(output_root, effective_cfg)
        postprocess_failed = experiment_audit_failed(audit_summary)
        summarize_results(output_root, effective_cfg)
        write_json(
            output_root / "experiment_timing.json",
            {
                "started_at_utc": experiment_started_at,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "parallel_jobs": int(args.jobs),
                "run_phase_wall_sec": round(run_phase_wall_sec, 3),
                "experiment_wall_sec_through_summary": round(
                    time.perf_counter() - experiment_started_perf, 3
                ),
                "measurement": "monotonic_parent_process_wall_clock",
            },
        )
        dashboard_path = render_dashboard(output_root, effective_cfg)
        print(f"dashboard: {dashboard_path.resolve()}")
        if not args.no_open_dashboard:
            opened = open_experiment_dashboard(dashboard_path)
            print(f"dashboard_opened: {str(opened).lower()}")
        print(f"experiment_audit_passed: {str(not postprocess_failed).lower()}")
    else:
        write_json(
            output_root / "experiment_timing.json",
            {
                "started_at_utc": experiment_started_at,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "parallel_jobs": int(args.jobs),
                "run_phase_wall_sec": round(run_phase_wall_sec, 3),
                "experiment_wall_sec_through_summary": round(run_phase_wall_sec, 3),
                "measurement": "monotonic_parent_process_wall_clock",
            },
        )

    return 1 if postprocess_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
