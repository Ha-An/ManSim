from __future__ import annotations

import argparse
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
    "mode",
    "worker_count",
    "seed",
    "scenario",
    "horizon_days",
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
    parser.add_argument("--seeds", nargs="*", type=int, default=None, help="Optional subset of seeds.")
    parser.add_argument("--worker-counts", nargs="*", type=int, default=None, help="Optional subset of worker counts.")
    parser.add_argument("--days", type=int, default=None, help="Override horizon days.")
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N mode/seed combinations.")
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
    fairness_pass_count = int(summary.get("fairness_pass_count", 0) or 0)
    return (
        run_count == 0
        or fairness_pass_count != run_count
        or int(summary.get("artifact_audit_fail_count", 0) or 0) > 0
        or int(summary.get("kpi_audit_fail_count", 0) or 0) > 0
    )


def main() -> int:
    args = parse_args()
    cfg = load_experiment_config(args.config)
    effective_cfg = replace(
        cfg,
        horizon_days=int(args.days if args.days is not None else cfg.horizon_days),
        modes=[str(mode) for mode in (args.modes if args.modes is not None else cfg.modes)],
        seeds=[int(seed) for seed in (args.seeds if args.seeds is not None else cfg.seeds)],
        worker_counts=[int(count) for count in (args.worker_counts if args.worker_counts is not None else cfg.worker_counts)],
    )
    output_root = resolve_results_root(args.output_root, stamp=args.stamp or timestamp())
    specs = build_run_specs(
        cfg,
        output_root,
        modes=args.modes,
        seeds=args.seeds,
        worker_counts=args.worker_counts,
        days=args.days,
        limit=args.limit,
    )

    if args.dry_run:
        for spec in specs:
            command = build_run_command(spec, cfg.common_overrides)
            print(" ".join(command))
        return 0

    output_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.config, output_root / "experiment_config.yaml")
    write_json(
        output_root / "experiment_plan.json",
        {
            "scenario": cfg.scenario,
            "horizon_days": effective_cfg.horizon_days,
            "minutes_per_day": cfg.minutes_per_day,
            "modes": list(dict.fromkeys(spec.mode for spec in specs)),
            "worker_counts": sorted({spec.worker_count for spec in specs}),
            "seeds": sorted({spec.seed for spec in specs}),
            "run_count": len(specs),
        },
    )

    status_path = output_root / STATUS_CSV
    for spec in specs:
        command = build_run_command(spec, cfg.common_overrides)
        command_text = " ".join(command)
        dependency_ok, dependency_reason = check_optimizer_dependency(spec.mode)
        if not dependency_ok:
            upsert_csv(
                status_path,
                {
                    "run_id": spec.run_id,
                    "mode": spec.mode,
                    "worker_count": spec.worker_count,
                    "seed": spec.seed,
                    "scenario": spec.scenario,
                    "horizon_days": spec.horizon_days,
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
                        "mode": spec.mode,
                        "worker_count": spec.worker_count,
                        "seed": spec.seed,
                        "scenario": spec.scenario,
                        "horizon_days": spec.horizon_days,
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

        started = time.perf_counter()
        return_code, output = run_subprocess(command, cwd=REPO_ROOT, log_path=spec.run_dir / "experiment_stdout.log")
        elapsed = round(time.perf_counter() - started, 3)
        status = "completed" if return_code == 0 and (spec.run_dir / "kpi.json").exists() else "failed"
        reason = "" if status == "completed" else (output.strip().splitlines()[-1] if output.strip() else "missing kpi.json")
        upsert_csv(
            status_path,
            {
                "run_id": spec.run_id,
                "mode": spec.mode,
                "worker_count": spec.worker_count,
                "seed": spec.seed,
                "scenario": spec.scenario,
                "horizon_days": spec.horizon_days,
                "run_dir": str(spec.run_dir.resolve()),
                "status": status,
                "return_code": return_code,
                "elapsed_sec": elapsed,
                "reason": reason,
                "command": command_text,
            },
            STATUS_FIELDS,
            key="run_id",
        )
        if args.compact_after_run:
            compact_run_dir(spec.run_dir)

    postprocess_failed = False
    if not args.no_postprocess:
        audit_summary = audit_experiment(output_root, effective_cfg)
        postprocess_failed = experiment_audit_failed(audit_summary)
        summarize_results(output_root, effective_cfg)
        dashboard_path = render_dashboard(output_root, effective_cfg)
        print(f"dashboard: {dashboard_path.resolve()}")
        if not args.no_open_dashboard:
            opened = open_experiment_dashboard(dashboard_path)
            print(f"dashboard_opened: {str(opened).lower()}")
        print(f"experiment_audit_passed: {str(not postprocess_failed).lower()}")

    return 1 if postprocess_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
