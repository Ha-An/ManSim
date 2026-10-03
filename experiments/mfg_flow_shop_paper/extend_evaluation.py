"""Append the three deferred rule policies without changing archived runs."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from experiments.mfg_flow_shop_paper.prepare_experiment import _powershell_command

ADDITIONAL_MODES = (
    "immediate_dedicated_roles",
    "rolling_horizon_shared",
    "rolling_horizon_dedicated_roles",
)


def build_extension(rows: list[dict[str, str]], window: float) -> list[dict[str, str]]:
    existing = {row["run_id"] for row in rows}
    result = []
    for mode in ADDITIONAL_MODES:
        for source in rows:
            if source["mode"] != "immediate_shared":
                continue
            run_id = f"{mode}__workers_{source['worker_count']}__seed_{source['seed']}"
            if run_id in existing:
                continue
            old_dir = Path(source["run_dir"])
            run_dir = old_dir.parents[2] / mode / old_dir.parent.name / old_dir.name
            command = [
                f"decision={mode}" if value.startswith("decision=") else
                f"hydra.run.dir={run_dir.as_posix()}" if value.startswith("hydra.run.dir=") else value
                for value in json.loads(source["command_json"])
            ]
            if mode.startswith("rolling_horizon_"):
                command.append(f"decision.rolling_horizon.window_min={window:g}")
            result.append({
                **source, "run_id": run_id, "mode": mode, "run_dir": str(run_dir),
                "command_json": json.dumps(command), "command": _powershell_command(command),
            })
            existing.add(run_id)
    return result


def check_configs(rows: list[dict[str, str]], added: list[dict[str, str]]) -> int:
    references = {row["worker_count"]: row for row in rows if row["mode"] == "immediate_shared"}
    checked = set()
    for row in added:
        key = (row["mode"], row["worker_count"])
        if key in checked:
            continue
        reference = references[row["worker_count"]]
        archived = yaml.safe_load((Path(reference["run_dir"]) / ".hydra/config.yaml").read_text(encoding="utf-8"))
        completed = subprocess.run(
            [*json.loads(row["command_json"]), "--cfg", "job"], cwd=ROOT,
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        current = yaml.safe_load(completed.stdout)
        # Only the decision policy and run seed may differ from this fleet's reference.
        ignore = {"decision", "seed", "hydra"}
        differences = [name for name in set(archived) | set(current)
                       if name not in ignore and archived.get(name) != current.get(name)]
        if differences:
            raise RuntimeError(f"Archived configuration mismatch for {key}: {differences}")
        checked.add(key)
    return len(checked)


def extend(prepared: Path) -> dict:
    plan_path = prepared / "experiment_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    with (prepared / "evaluation_plan.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    added = build_extension(rows, float(plan["rolling_horizon_window_min"]))
    if not added:
        return {"status": "already_extended", "run_count": len(rows)}
    if len(added) != 3 * len(plan["worker_counts"]) * len(plan["test_seeds"]):
        raise RuntimeError("Expected one complete additional three-policy factorial.")
    checks = check_configs(rows, added)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = prepared / f"before_six_policy_extension_{stamp}"
    backup.mkdir()
    for path in prepared.iterdir():
        if path.is_file():
            shutil.copy2(path, backup / path.name)
    dashboard = prepared / "policy_comparison_dashboard"
    if dashboard.exists():
        shutil.copytree(dashboard, backup / dashboard.name)
    old_start = (prepared / "evaluation_preflight.json").stat().st_mtime
    old_end = (prepared / "evaluation_status.csv").stat().st_mtime
    report = {
        "status": "pass", "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "added_modes": list(ADDITIONAL_MODES), "reused_run_count": len(rows),
        "added_run_count": len(added), "config_checks": checks,
        "original_evaluation_wall_sec": max(0.0, old_end - old_start),
        "backup_path": str(backup),
    }
    rows.extend(added)
    with (prepared / "evaluation_plan.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    plan["policies"] = list(dict.fromkeys([*plan["policies"], *ADDITIONAL_MODES]))
    plan["counts"]["baseline_evaluation_runs"] += len(added)
    plan["counts"]["evaluation_runs"] = len(rows)
    plan["extension"] = report
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (prepared / "evaluation_extension.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (prepared / "evaluation_commands.ps1").write_text(
        "$ErrorActionPreference = 'Stop'\n" + "\n".join(row["command"] for row in rows) + "\n", encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(extend(args.prepared.resolve()), indent=2))
