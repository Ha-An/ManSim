"""Run the 20/40 material study using the existing evaluator and dashboard."""
from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from experiments.mfg_flow_shop_paper.prepare_experiment import (
    _powershell_command, evaluation_plan_rows, load_config,
)
from experiments.mfg_flow_shop_paper.run_evaluation import _evaluation_lock
from manufacturing_sim.adp.live import atomic_json

POLICIES = ["immediate_shared", "random_feasible_dispatch"]
SEEDS = list(range(910001, 910101))


def inventory_overrides(fill: int) -> dict[str, int]:
    if fill not in (20, 40):
        raise ValueError("Inventory study supports initial/top-up targets 20 and 40")
    return {
        "scenario.warehouse.material_shelf.capacity": 40,
        "scenario.warehouse.material_shelf.initial_fill": fill,
        "scenario.objective.throughput.restock_target_fill": fill,
        "+scenario.map.warehouse_height_tiles": 15,
    }


def prepare(output: Path, *, smoke: bool = False) -> dict:
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite study: {output}")
    cfg = replace(load_config(), policies=POLICIES, training_replicates=0,
                  worker_counts=[2, 6] if smoke else [2, 3, 4, 5, 6],
                  test_seeds=[SEEDS[0]] if smoke else SEEDS,
                  evaluation_jobs=10)
    conditions = []
    output.mkdir(parents=True)
    for fill in (20, 40):
        prepared = output / f"materials_{fill}"
        prepared.mkdir()
        rows = evaluation_plan_rows(cfg, prepared)
        overrides = inventory_overrides(fill)
        for row in rows:
            command = json.loads(row["command_json"])
            command.extend(f"{key}={value}" for key, value in overrides.items())
            if smoke:
                # Only qualification runs retain events for tile/item continuity audits.
                command = ["runtime.artifacts.export_events=true" if arg ==
                           "runtime.artifacts.export_events=false" else arg for arg in command]
            row.update(command_json=json.dumps(command), command=_powershell_command(command))
        plan = {
            "study_name": "mfg_flow_shop_inventory_sensitivity",
            "condition_label": f"Material initial/top-up target {fill}; shelf capacity 40; Warehouse 26 x 15",
            "scenario": cfg.scenario, "objective_mode": cfg.objective_mode,
            "horizon_days": cfg.horizon_days, "minutes_per_day": cfg.minutes_per_day,
            "worker_counts": cfg.worker_counts, "test_seeds": cfg.test_seeds,
            "policies": POLICIES, "training_replicates": 0, "training_plan": [],
            "parallel_jobs": 10, "common_random_numbers": True,
            "explicit_wait_action_enabled": False,
            "scenario_overrides": {k.lstrip("+"): v for k, v in overrides.items()},
            "counts": {"evaluation_runs": len(rows), "adp_evaluation_runs": 0,
                       "baseline_evaluation_runs": len(rows), "new_adp_training_runs": 0},
            "retain_episode_events": smoke, "export_replay_artifacts": False,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        atomic_json(prepared / "experiment_plan.json", plan)
        with (prepared / "evaluation_plan.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        (prepared / "evaluation_commands.ps1").write_text(
            "$ErrorActionPreference = 'Stop'\n" + "\n".join(row["command"] for row in rows) + "\n",
            encoding="utf-8",
        )
        conditions.append({"target_fill": fill, "prepared": str(prepared), "runs": len(rows)})
    study = {"conditions": conditions, "total_runs": sum(c["runs"] for c in conditions),
             "smoke": smoke, "parallel_jobs": 10}
    atomic_json(output / "inventory_study.json", study)
    return study


def run(output: Path, *, jobs: int = 10, open_dashboard: bool = True) -> int:
    if jobs < 1:
        raise ValueError("jobs must be positive")
    study = json.loads((output / "inventory_study.json").read_text(encoding="utf-8"))
    with _evaluation_lock(output):
        for condition in study["conditions"]:
            atomic_json(output / "inventory_progress.json", {
                "phase": "running", "active_target_fill": condition["target_fill"],
                "parallel_jobs": jobs, "total_runs": study["total_runs"],
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            })
            command = [sys.executable, str(HERE / "run_evaluation.py"),
                       "--prepared", condition["prepared"], "--jobs", str(jobs), "--pending-only"]
            if not open_dashboard:
                command.append("--no-open-dashboard")
            code = subprocess.run(command, cwd=ROOT, check=False).returncode
            if code:
                atomic_json(output / "inventory_progress.json", {
                    "phase": "failed", "target_fill": condition["target_fill"], "return_code": code,
                })
                return code
        atomic_json(output / "inventory_progress.json", {
            "phase": "completed", "total_runs": study["total_runs"],
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--no-open-dashboard", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not args.resume:
        print(json.dumps(prepare(output, smoke=args.smoke), indent=2), flush=True)
    return run(output, jobs=args.jobs, open_dashboard=not args.no_open_dashboard) if args.run else 0


if __name__ == "__main__":
    raise SystemExit(main())
