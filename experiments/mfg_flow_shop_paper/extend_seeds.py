"""Extend a completed comparison with new common seeds, preserving old runs."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from experiments.mfg_flow_shop_paper.prepare_experiment import _powershell_command
from experiments.mfg_flow_shop_paper.run_evaluation import _completed_kpi_is_valid, _preflight_adp_checkpoints
from experiments.mfg_flow_shop_paper.summarize_results import _plan_errors
from manufacturing_sim.adp.live import atomic_json, atomic_text


def build_seed_extension(rows: list[dict[str, str]], seeds: list[int]) -> list[dict[str, str]]:
    if len(seeds) != len(set(seeds)):
        raise ValueError("Duplicate requested seeds")
    existing = {(r["mode"], r["worker_count"], r["training_replicate"], int(r["seed"])) for r in rows}
    templates = {}
    for row in rows:
        templates.setdefault((row["mode"], row["worker_count"], row["training_replicate"]), row)
    result = []
    for key, source in sorted(templates.items()):
        for seed in sorted(seeds):
            if (*key, seed) in existing:
                continue
            run_dir = Path(source["run_dir"]).with_name(f"seed_{seed}")
            command = [f"seed={seed}" if arg.startswith("seed=") else
                       f"hydra.run.dir={run_dir.as_posix()}" if arg.startswith("hydra.run.dir=") else arg
                       for arg in json.loads(source["command_json"])]
            result.append({**source, "seed": str(seed),
                           "run_id": source["run_id"].rsplit("__seed_", 1)[0] + f"__seed_{seed}",
                           "run_dir": str(run_dir), "command_json": json.dumps(command),
                           "command": _powershell_command(command)})
    return result


def _check_current_configs(rows: list[dict[str, str]]) -> int:
    checked = set()
    for row in rows:
        key = (row["mode"], row["worker_count"], row["training_replicate"])
        if key in checked:
            continue
        archived = yaml.safe_load((Path(row["run_dir"]) / ".hydra/config.yaml").read_text(encoding="utf-8"))
        completed = subprocess.run([*json.loads(row["command_json"]), "--cfg", "job"], cwd=ROOT,
                                   capture_output=True, text=True, encoding="utf-8", check=True)
        current = yaml.safe_load(completed.stdout)
        differences = [k for k in set(archived) | set(current)
                       if k not in {"seed", "hydra"} and archived.get(k) != current.get(k)]
        if differences:
            raise RuntimeError(f"Configuration changed for {key}: {differences}; cannot reuse old runs")
        checked.add(key)
    return len(checked)


def mark_pending(prepared: Path, report: dict) -> None:
    atomic_json(prepared / "analysis_summary.json", {
        "status": "pending", "expected_run_count": report["reused_run_count"] + report["added_run_count"],
        "reused_run_count": report["reused_run_count"],
        "previous_analysis": str(Path(report["backup_path"]) / "analysis_summary.json"),
        "message": "The expanded comparison is not yet aggregated; see evaluation_progress.json.",
    })
    atomic_text(prepared / "policy_comparison_dashboard/comparison_dashboard.html", '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="0;url=../live_evaluation.html"><title>Policy comparison in progress</title></head>
<body><h1>Policy comparison in progress</h1><p><a href="../live_evaluation.html">Open live progress</a></p>
<p>Final statistics will replace this page after every planned run passes verification.</p></body></html>''')


def extend(prepared: Path, seeds: list[int], *, allow_undeclared: bool = False) -> dict:
    plan = json.loads((prepared / "experiment_plan.json").read_text(encoding="utf-8"))
    if not set(plan["test_seeds"]).issubset(seeds):
        raise ValueError("Existing seeds must be retained")
    with (prepared / "evaluation_plan.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    added = build_seed_extension(rows, seeds)
    if not added:
        return {"status": "already_extended", "run_count": len(rows)}
    summary = json.loads((prepared / "analysis_summary.json").read_text(encoding="utf-8"))
    with (prepared / "evaluation_status.csv").open(encoding="utf-8", newline="") as handle:
        statuses = list(csv.DictReader(handle))
    if (summary.get("status") != "pass" or summary.get("eligible_run_count") != len(rows)
            or _plan_errors(rows, statuses, plan)):
        raise RuntimeError("The source comparison is incomplete or invalid")
    by_id = {r["run_id"]: r for r in statuses}
    for row in rows:
        status = by_id.get(row["run_id"], {})
        if (status.get("status") not in {"completed", "skipped_existing"}
                or status.get("artifact_audit") != "pass" or status.get("kpi_audit") != "pass"
                or not _completed_kpi_is_valid(Path(row["run_dir"]) / "kpi.json", json.loads(row["command_json"]))):
            raise RuntimeError(f"Cannot reuse {row['run_id']}")
    for entry in plan["training_plan"]:
        digest = hashlib.sha256(Path(entry["checkpoint_path"]).read_bytes()).hexdigest()
        if digest != entry.get("checkpoint_sha256"):
            raise RuntimeError(f"Checkpoint changed: {entry['checkpoint_path']}")
    checks = _check_current_configs(rows)
    reports = _preflight_adp_checkpoints(rows + added, allow_undeclared=allow_undeclared)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    backup = prepared / f"before_seed_extension_{stamp}"
    backup.mkdir()
    for path in prepared.iterdir():
        if path.is_file():
            shutil.copy2(path, backup / path.name)
    dashboard = prepared / "policy_comparison_dashboard"
    if dashboard.exists():
        shutil.copytree(dashboard, backup / dashboard.name)
    report = {"status": "pass", "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "old_seeds": plan["test_seeds"], "test_seeds": sorted(seeds),
              "reused_run_count": len(rows), "added_run_count": len(added),
              "config_checks": checks, "backup_path": str(backup),
              "checkpoint_preflight": reports,
              "selection_rule": "Fixed expanded seed set; no significance-based stopping; checkpoints unchanged."}
    rows.extend(added)
    plan.update(test_seeds=sorted(seeds), seed_extension=report)
    plan["counts"].update(evaluation_runs=len(rows),
                          baseline_evaluation_runs=sum(r["mode"] != "simulation_based_adp" for r in rows),
                          adp_evaluation_runs=sum(r["mode"] == "simulation_based_adp" for r in rows))
    import io
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    atomic_text(prepared / "evaluation_plan.csv", stream.getvalue())
    atomic_json(prepared / "experiment_plan.json", plan)
    atomic_json(prepared / "seed_extension.json", report)
    atomic_text(prepared / "evaluation_commands.ps1",
                "$ErrorActionPreference = 'Stop'\n" + "\n".join(r["command"] for r in rows) + "\n")
    mark_pending(prepared, report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--first-seed", type=int, required=True)
    parser.add_argument("--seed-count", type=int, required=True)
    parser.add_argument("--allow-undeclared-held-out-seeds", action="store_true")
    args = parser.parse_args()
    if args.seed_count < 1:
        parser.error("seed-count must be positive")
    result = extend(args.prepared.resolve(), list(range(args.first_seed, args.first_seed + args.seed_count)),
                    allow_undeclared=args.allow_undeclared_held_out_seeds)
    print(json.dumps({k: v for k, v in result.items() if k != "checkpoint_preflight"}, indent=2))
