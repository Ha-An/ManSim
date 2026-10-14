"""Train inventory policies, then evaluate fresh or qualified baseline runs."""
from __future__ import annotations

import argparse
import csv
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from experiments.mfg_flow_shop_paper.inventory_study import SEEDS, inventory_overrides
from experiments.mfg_flow_shop_paper.prepare_experiment import (
    _powershell_command, evaluation_plan_rows, load_config, prepare as prepare_plan,
)
from experiments.mfg_flow_shop_paper.run_evaluation import (
    _pending_rows, _read_rows, _write_status,
)
from experiments.mfg_flow_shop_paper.summarize_results import METRICS, collect_runs
from manufacturing_sim.adp.live import atomic_json

ALL_POLICIES = [
    "immediate_shared", "immediate_dedicated_roles",
    "rolling_horizon_shared", "rolling_horizon_dedicated_roles",
    "random_feasible_dispatch", "simulation_based_adp",
]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_rows(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def source_digest(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda value: value["run_id"]):
        digest.update(row["run_id"].encode())
        for name in ("kpi.json", "run_meta.json", "pre_run_diagnostics.json", ".hydra/config.yaml"):
            digest.update((Path(row["run_dir"]) / name).read_bytes())
    return digest.hexdigest()


def qualify_source(source: Path, fill: int) -> tuple[dict, list[dict], list[dict]]:
    from omegaconf import OmegaConf
    from runtime.compat import build_legacy_experiment_cfg
    from manufacturing_sim.adp.train import _compose_episode_cfg

    plan = read_json(source / "experiment_plan.json")
    overrides = inventory_overrides(fill)
    if plan.get("scenario_overrides") != {key.lstrip("+"): value for key, value in overrides.items()}:
        raise ValueError("Source inventory/map configuration does not match the requested condition.")
    if (plan["worker_counts"] != [2, 3, 4, 5, 6] or plan["horizon_days"] != 5
            or plan["minutes_per_day"] != 480 or set(plan["policies"]) != {
                "immediate_shared", "random_feasible_dispatch"}):
        raise ValueError("Unexpected source fleet, horizon or policies.")
    rows, errors = collect_runs(source)
    if errors or len(rows) != 2 * 5 * len(plan["test_seeds"]):
        raise RuntimeError(f"Source result audit failed: {errors[:10]}")
    plans = _read_rows(source / "evaluation_plan.csv")
    statuses = _read_rows(source / "evaluation_status.csv")
    if _pending_rows(plans, {row["run_id"]: row for row in statuses}):
        raise RuntimeError("Source includes unaudited, incomplete or incompatible runs.")
    current = {}
    for worker in plan["worker_counts"]:
        current[worker] = _compose_episode_cfg(
            worker_count=worker, seed=plan["test_seeds"][0], days=5,
            adp_cfg={"_scenario_overrides": overrides},
        )
    for row in plans:
        archived = yaml.load((Path(row["run_dir"]) / ".hydra/config.yaml").read_text(encoding="utf-8"),
                             Loader=yaml.CSafeLoader)
        legacy = build_legacy_experiment_cfg(OmegaConf.create(archived))
        expected = current[int(row["worker_count"])]
        mismatched = [key for key in expected if key not in {"decision", "runtime", "seed"}
                      and expected[key] != legacy.get(key)]
        if mismatched:
            raise RuntimeError(f"Archived environment differs from current code: {row['run_id']}: {mismatched}")
    return plan, plans, statuses


def prepare(source: Path | None, output: Path, fill: int, *, all_policies: bool = False) -> dict:
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    overrides = inventory_overrides(fill)
    source_plan, baselines, statuses = (
        qualify_source(source, fill) if source is not None else
        ({"test_seeds": SEEDS,
          "condition_label": f"Material initial/top-up target {fill}; shelf capacity 40; Warehouse 26 x 15"}, [], [])
    )
    cfg = load_config()
    template = yaml.safe_load(cfg.training_template.read_text(encoding="utf-8"))
    validation = template["validation"]
    cfg = replace(cfg, test_seeds=source_plan["test_seeds"],
                  screening_iterations=validation["screening_iterations"],
                  screening_seed_count=validation["screening_seed_count"],
                  final_candidate_count=validation["final_candidate_count"],
                  final_selection_seed_count=validation["final_selection_seed_count"],
                  evaluation_jobs=10,
                  policies=ALL_POLICIES if all_policies else cfg.policies)
    plan = prepare_plan(cfg, output)
    for row in plan["training_plan"]:
        path = Path(row["config_path"])
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        payload["scenario_overrides"] = overrides
        payload["validation"]["screening_interval_iterations"] = validation["screening_interval_iterations"]
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    reused_ids = {row["run_id"] for row in baselines}
    new_rows = [row for row in evaluation_plan_rows(cfg, output) if row["run_id"] not in reused_ids]
    for row in new_rows:
        command = json.loads(row["command_json"]) + [f"{key}={value}" for key, value in overrides.items()]
        row.update(command_json=json.dumps(command), command=_powershell_command(command))
    rows = baselines + new_rows
    write_rows(output / "evaluation_plan.csv", rows)
    (output / "evaluation_commands.ps1").write_text(
        "$ErrorActionPreference = 'Stop'\n" + "\n".join(row["command"] for row in rows) + "\n", encoding="utf-8")
    reused = [{**row, "status": "skipped_existing", "reason": f"Reused from {source}"} for row in statuses]
    if reused:
        _write_status(output / "evaluation_status.csv", reused)
    atomic_json(output / "evaluation_timing.json", {
        "run_phase_wall_sec": 0.0, "experiment_wall_sec_through_summary": 0.0,
        "measurement": "New evaluation wall time only; any reused baseline durations remain in statuses.",
    })
    plan.update(
        study_name=f"mfg_flow_shop_materials_{fill}_adp",
        condition_label=source_plan["condition_label"],
        scenario_overrides={key.lstrip("+"): value for key, value in overrides.items()},
        parallel_jobs=10, baseline_source=str(source) if source is not None else None,
        baseline_source_digest=source_digest(baselines) if baselines else None,
        reused_run_ids=sorted(reused_ids),
        retain_episode_events=False, export_replay_artifacts=False,
        created_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    plan["counts"].update(reused_baseline_runs=len(baselines), new_evaluation_runs=len(new_rows))
    # Existing compressed comparison artifacts use roughly 10--15 MiB per run.
    # Reserve headroom before starting a new large evaluation block.
    plan["evaluation_min_free_gib"] = 45.0 if len(new_rows) >= 3000 else 0.0
    atomic_json(output / "experiment_plan.json", plan)
    atomic_json(output / "baseline_reuse_preflight.json", {
        "status": "pass" if baselines else "not_applicable",
        "source": str(source) if source is not None else None, "source_runs": len(baselines),
        "effective_environment_and_timing_match": True if baselines else None,
        "source_digest": plan["baseline_source_digest"],
    })
    return plan


def wait_for_evaluation_space(prepared: Path, required_gib: float) -> None:
    while required_gib > 0:
        free_gib = shutil.disk_usage(prepared).free / 2**30
        if free_gib >= required_gib:
            return
        atomic_json(prepared / "pipeline_status.json", {
            "phase": "waiting_for_disk_space", "free_gib": round(free_gib, 2),
            "required_free_gib": required_gib,
            "message": "Training is preserved. Evaluation starts automatically when sufficient disk space is available.",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        print(f"Evaluation waiting for disk space: {free_gib:.1f} / {required_gib:.1f} GiB free", flush=True)
        time.sleep(60)


def verify_reproduction(prepared: Path) -> None:
    from experiments.mfg_flow_shop_paper.run_evaluation import _run
    from experiments.factory_policy_comparison.audit_experiment import _pre_run_fingerprints

    plan = read_json(prepared / "experiment_plan.json")
    reused_ids = plan.get("reused_run_ids")
    baselines = [row for row in _read_rows(prepared / "evaluation_plan.csv")
                 if (row["run_id"] in reused_ids if reused_ids is not None
                     else row["mode"] != "simulation_based_adp")]
    evidence = []
    for source in baselines:
        if int(source["worker_count"]) not in (2, 6) or int(source["seed"]) != min(plan["test_seeds"]):
            continue
        destination = prepared / "baseline_reproduction" / source["run_id"]
        command = [f"hydra.run.dir={destination.as_posix()}" if arg.startswith("hydra.run.dir=") else
                   "+" + arg if arg.startswith("+scenario.map.warehouse_height_tiles=") else arg
                   for arg in json.loads(source["command_json"])]
        row = {**source, "run_dir": str(destination), "command_json": json.dumps(command)}
        result = _run(row, False, True, False)
        if result["status"] not in {"completed", "skipped_existing"}:
            raise RuntimeError(f"Baseline reproduction failed: {result}")
        original = read_json(Path(source["run_dir"]) / "kpi.json")
        actual = read_json(destination / "kpi.json")
        differences = {key: [original.get(key), actual.get(key)] for key in METRICS if original.get(key) != actual.get(key)}
        old_meta = read_json(Path(source["run_dir"]) / "run_meta.json")
        new_meta = read_json(destination / "run_meta.json")
        for key in ("event_audit_signature", "stochastic_streams"):
            if old_meta.get(key) != new_meta.get(key):
                differences[key] = "mismatch"
        if _pre_run_fingerprints(Path(source["run_dir"])) != _pre_run_fingerprints(destination):
            differences["pre_run_fingerprints"] = "mismatch"
        evidence.append({"run_id": source["run_id"], "differences": differences})
        atomic_json(prepared / "baseline_reproduction.json", {"checks": evidence, "status": "running"})
        if differences:
            raise RuntimeError(f"Baseline is no longer reproducible: {source['run_id']}: {differences}")
    if len(evidence) != 4:
        raise RuntimeError("Expected four baseline reproduction cases.")
    atomic_json(prepared / "baseline_reproduction.json", {"checks": evidence, "status": "pass"})


def run(prepared: Path) -> int:
    plan = read_json(prepared / "experiment_plan.json")
    def phase(name: str) -> None:
        atomic_json(prepared / "pipeline_status.json", {"phase": name, "updated_at_utc": datetime.now(timezone.utc).isoformat()})
        print(name, flush=True)
    try:
        if plan.get("baseline_source"):
            phase("baseline_reproduction")
            verify_reproduction(prepared)
        phase("training")
        subprocess.run([sys.executable, str(HERE / "run_training.py"), "--prepared", str(prepared)], cwd=ROOT, check=True)
        phase("evaluation_preflight")
        if plan.get("baseline_source"):
            reused_ids = plan.get("reused_run_ids")
            baselines = [row for row in _read_rows(prepared / "evaluation_plan.csv")
                         if row["run_id"] in reused_ids] if reused_ids is not None else [
                             row for row in _read_rows(prepared / "evaluation_plan.csv")
                             if row["mode"] != "simulation_based_adp"]
            if source_digest(baselines) != plan["baseline_source_digest"]:
                raise RuntimeError("Referenced baseline artifacts changed during training; requalification required.")
        from manufacturing_sim.adp.checkpoint import load_checkpoint
        from manufacturing_sim.adp.train import _compose_episode_cfg, InMemoryEventLogger, ManufacturingWorld, build_decision_module
        import simpy
        for row in plan["training_plan"]:
            cfg = _compose_episode_cfg(worker_count=int(row["worker_count"]), seed=plan["test_seeds"][0], days=5,
                adp_cfg={"_scenario_overrides": inventory_overrides(int(plan["scenario_overrides"]["scenario.warehouse.material_shelf.initial_fill"]))})
            world = ManufacturingWorld(simpy.Environment(), cfg, InMemoryEventLogger(),
                build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"))
            load_checkpoint(Path(row["checkpoint_path"]), world=world, device="cpu")
        wait_for_evaluation_space(prepared, float(plan.get("evaluation_min_free_gib", 0.0)))
        phase("policy_comparison")
        subprocess.run([sys.executable, str(HERE / "run_evaluation.py"), "--prepared", str(prepared),
                        "--jobs", "10", "--pending-only"], cwd=ROOT, check=True)
        phase("completed")
        return 0
    except Exception as exc:
        atomic_json(prepared / "pipeline_status.json", {
            "phase": "failed", "error": f"{type(exc).__name__}: {exc}",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        })
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fill", type=int, choices=(20, 30, 40), default=20)
    parser.add_argument("--fresh", action="store_true", help="Run every evaluation anew without archived baseline reuse.")
    parser.add_argument("--all-policies", action="store_true", help="Compare ADP, Random Feasible and all four rule policies.")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--run-prepared", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if args.run_prepared:
        return run(output)
    if (args.source is None) == (not args.fresh):
        parser.error("Choose exactly one of --source or --fresh")
    plan = prepare(args.source.resolve() if args.source is not None else None,
                   output, args.fill, all_policies=args.all_policies)
    print(json.dumps(plan["counts"], indent=2), flush=True)
    if args.background:
        from manufacturing_sim.adp.background import launch
        monitor = launch(command=[sys.executable, "-u", str(Path(__file__).resolve()),
                         "--output", str(output), "--run-prepared"], job_dir=output / "background",
                         runs=[{"label": f"Materials {args.fill} / Humanoids {row['worker_count']}",
                                "output": row["training_output"]} for row in plan["training_plan"]])
        print(f"Monitor: {monitor}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
