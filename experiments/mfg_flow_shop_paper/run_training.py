from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import webbrowser


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "worker_count", "training_replicate", "status", "elapsed_sec",
        "checkpoint_path", "training_audit", "dashboard_opened", "reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fields} for row in rows)


def _archive_existing(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target = path.with_name(f"{path.name}_interrupted_{stamp}")
    suffix = 1
    while target.exists():
        target = path.with_name(f"{path.name}_interrupted_{stamp}_{suffix}")
        suffix += 1
    shutil.move(str(path), str(target))
    return target


def _audit_training(output: Path) -> tuple[str, str]:
    command = [
        sys.executable,
        str((REPO_ROOT / "scripts" / "audit_adp_training.py").resolve()),
        str(output.resolve()),
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    audit_log = output / "paper_training_audit.log"
    audit_log.write_text(completed.stdout, encoding="utf-8")
    return ("pass", "") if completed.returncode == 0 else ("fail", completed.stdout.strip())


def _recorded_wall_sec(output: Path) -> float:
    try:
        payload = json.loads((output / "training_summary.json").read_text(encoding="utf-8"))
        return round(float(payload.get("wall_sec", 0.0) or 0.0), 3)
    except (OSError, TypeError, ValueError):
        return 0.0


def _open_dashboard(output: Path) -> bool:
    dashboard = (output / "training_dashboard.html").resolve()
    if not dashboard.is_file():
        return False
    candidates = [
        shutil.which("chrome.exe"),
        shutil.which("chrome"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        executable = Path(candidate)
        if executable.is_file():
            subprocess.Popen(
                [str(executable), dashboard.as_uri()],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    return bool(webbrowser.open(dashboard.as_uri()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the prepared fleet-specific ADP training plan.")
    parser.add_argument("--prepared", type=Path, default=HERE / "prepared")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-open-dashboard", action="store_true")
    parser.add_argument("--background", action="store_true",
                        help="Run the complete training plan in a detached process with a live monitor.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    prepared = args.prepared.resolve()
    rows = _read_rows(prepared / "training_plan.csv")
    if args.background and not args.dry_run:
        # Script invocation puts experiments/ on sys.path, not the repository root.
        sys.path.insert(0, str(REPO_ROOT))
        from manufacturing_sim.adp.background import launch
        command = [sys.executable, "-u", str(Path(__file__).resolve()),
                   "--prepared", str(prepared)]
        if args.force:
            command.append("--force")
        if args.no_open_dashboard:
            command.append("--no-open-dashboard")
        monitor = launch(
            command=command,
            job_dir=prepared / "background_training" / datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
            runs=[{"label": f"Workers {row['worker_count']} / Replicate {row['training_replicate']}",
                   "output": str(Path(row["training_output"]).resolve())} for row in rows],
            open_browser=not args.no_open_dashboard,
        )
        print(f"Background training launched.\nMonitor: {monitor}\nJob: {monitor.parent}", flush=True)
        return 0
    status_path = prepared / "training_status.csv"
    previous_statuses = {
        (row.get("worker_count", ""), row.get("training_replicate", "")): row
        for row in (_read_rows(status_path) if status_path.is_file() else [])
    }
    statuses: list[dict[str, object]] = []
    for row in rows:
        command = [
            sys.executable,
            "-m",
            "manufacturing_sim.adp.train",
            "--config",
            row["config_path"],
            "--output",
            row["training_output"],
            "--no-open-dashboard",
        ]
        if args.dry_run:
            print(json.dumps(command))
            continue
        checkpoint = Path(row["checkpoint_path"])
        if checkpoint.is_file() and not args.force:
            audit_status, audit_reason = _audit_training(Path(row["training_output"]))
            if audit_status == "pass":
                previous = previous_statuses.get((row["worker_count"], row["training_replicate"]), {})
                was_opened = str(previous.get("dashboard_opened", "")).lower() == "true"
                dashboard_opened = was_opened or (
                    False if args.no_open_dashboard else _open_dashboard(Path(row["training_output"]))
                )
                statuses.append({
                    **row, "status": "skipped_existing", "elapsed_sec": _recorded_wall_sec(Path(row["training_output"])),
                    "training_audit": audit_status, "dashboard_opened": dashboard_opened, "reason": "",
                })
                _write_rows(status_path, statuses)
                continue
            row = {**row, "invalid_existing_reason": audit_reason}
        archived = _archive_existing(Path(row["training_output"]))
        log_path = Path(row["training_output"]) / "training_console.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(command, cwd=REPO_ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
        status = "completed" if completed.returncode == 0 and checkpoint.is_file() else "failed"
        audit_status = "not_run"
        audit_reason = ""
        if status == "completed":
            audit_status, audit_reason = _audit_training(Path(row["training_output"]))
            if audit_status != "pass":
                status = "audit_failed"
        dashboard_opened = (
            _open_dashboard(Path(row["training_output"]))
            if status == "completed" and not args.no_open_dashboard
            else False
        )
        statuses.append({
            **row,
            "status": status,
            "elapsed_sec": round(time.perf_counter() - started, 3),
            "training_audit": audit_status,
            "dashboard_opened": dashboard_opened,
            "reason": (
                f"restarted_from={archived}" if status == "completed" and archived is not None
                else "" if status == "completed"
                else audit_reason if status == "audit_failed"
                else f"return_code={completed.returncode}"
            ),
        })
        _write_rows(status_path, statuses)
        if status != "completed":
            return completed.returncode or 1
    if not args.dry_run:
        _write_rows(status_path, statuses)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
