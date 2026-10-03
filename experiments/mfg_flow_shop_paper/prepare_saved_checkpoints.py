"""Prepare a fresh evaluation block using completed, unmodified training outputs."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from experiments.mfg_flow_shop_paper.extend_evaluation import ADDITIONAL_MODES, build_extension
from experiments.mfg_flow_shop_paper.prepare_experiment import (
    DEFAULT_CONFIG, _powershell_command, evaluation_plan_rows, load_config,
)
from experiments.mfg_flow_shop_paper.run_evaluation import _preflight_adp_checkpoints
from scripts.audit_adp_training import audit_training


def bind_checkpoints(rows, checkpoints: dict[int, Path]):
    result = []
    for source in rows:
        row = dict(source)
        if row["mode"] == "simulation_based_adp":
            checkpoint = checkpoints[int(row["worker_count"])].resolve()
            command = [
                f"decision.adp.checkpoint_path={checkpoint.as_posix()}"
                if arg.startswith("decision.adp.checkpoint_path=") else arg
                for arg in json.loads(row["command_json"])
            ]
            row.update(checkpoint_path=str(checkpoint), command_json=json.dumps(command),
                       command=_powershell_command(command))
        result.append(row)
    return result


def prepare_saved(training_roots: list[Path], output: Path, *, config: Path = DEFAULT_CONFIG,
                  six_policies: bool = False, allow_undeclared: bool = False,
                  previous_result: Path | None = None) -> dict:
    cfg = load_config(config)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite an existing study: {output}")
    checkpoints, training_rows, audits = {}, [], []
    for root in training_roots:
        root = root.resolve()
        audit = audit_training(root)
        if audit["status"] != "pass":
            raise RuntimeError(f"Training audit failed: {root}: {audit['errors']}")
        summary = json.loads((root / "training_summary.json").read_text(encoding="utf-8"))
        resolved = yaml.safe_load((root / "resolved_config.yaml").read_text(encoding="utf-8"))
        workers = resolved["worker_counts"]
        if len(workers) != 1 or int(workers[0]) in checkpoints:
            raise ValueError(f"Expected one unique worker count per checkpoint: {root}")
        worker = int(workers[0])
        if int(resolved["horizon_days"]) != cfg.horizon_days:
            raise ValueError(f"Training horizon differs from evaluation: {root}")
        checkpoint = root / "best.pt"
        checkpoints[worker] = checkpoint
        training_rows.append({
            "worker_count": worker, "training_replicate": 1,
            "base_seed": resolved["base_seed"], "config_path": str(root / "resolved_config.yaml"),
            "training_output": str(root), "checkpoint_path": str(checkpoint),
            "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
            "best_iteration": summary["best_iteration"],
            "training_episode_count": summary["training_episode_count"],
            "validation_episode_count": summary["validation_episode_count"],
            "validation_design": resolved["validation"],
            "source": "completed_external_training", "command": "",
        })
        audits.append(audit)
    if sorted(checkpoints) != cfg.worker_counts:
        raise ValueError(f"Expected checkpoints for {cfg.worker_counts}; got {sorted(checkpoints)}")

    rows = bind_checkpoints(evaluation_plan_rows(cfg, output), checkpoints)
    if six_policies:
        rows.extend(build_extension(rows, cfg.rolling_window_min))
    preflight = _preflight_adp_checkpoints(rows, allow_undeclared=allow_undeclared)

    # Verify runtime schema, map, timing and lifecycle without advancing a simulation.
    import simpy
    from manufacturing_sim.adp.checkpoint import load_checkpoint
    from manufacturing_sim.adp.train import (
        _compose_episode_cfg, InMemoryEventLogger, ManufacturingWorld, build_decision_module,
    )
    for worker, checkpoint in sorted(checkpoints.items()):
        episode_cfg = _compose_episode_cfg(
            worker_count=worker, seed=cfg.test_seeds[0], days=cfg.horizon_days,
            adp_cfg={"force_random_policy": True, "allow_wait_action": False},
        )
        world = ManufacturingWorld(
            simpy.Environment(), episode_cfg, InMemoryEventLogger(),
            build_decision_module(experiment_cfg=episode_cfg, decision_mode="simulation_based_adp"),
        )
        load_checkpoint(checkpoint, world=world, device="cpu",
                        wait_action_enabled=False, worker_order_strategy="cyclic")

    prior = None
    if previous_result is not None:
        path = previous_result.resolve() / "result_validity.json"
        prior = {
            "path": str(previous_result.resolve()), "reused_evaluation_runs": 0,
            "validity": json.loads(path.read_text(encoding="utf-8")) if path.exists() else None,
        }
    policies = list(cfg.policies) + (list(ADDITIONAL_MODES) if six_policies else [])
    plan = {
        "study_name": "mfg_flow_shop_saved_checkpoint_evaluation",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scenario": cfg.scenario, "objective_mode": cfg.objective_mode,
        "horizon_days": cfg.horizon_days, "minutes_per_day": cfg.minutes_per_day,
        "rolling_horizon_window_min": cfg.rolling_window_min,
        "worker_counts": cfg.worker_counts, "policies": policies,
        "training_replicates": 1, "test_seeds": cfg.test_seeds,
        "common_random_numbers": True, "explicit_wait_action_enabled": False,
        "seed_contract": "Recorded test extension; disjoint from source training and validation; source checkpoints unchanged.",
        "held_out_declaration_enforced": not allow_undeclared,
        "counts": {
            "adp_training_runs": len(checkpoints), "new_adp_training_runs": 0,
            "baseline_evaluation_runs": sum(r["mode"] != "simulation_based_adp" for r in rows),
            "adp_evaluation_runs": sum(r["mode"] == "simulation_based_adp" for r in rows),
            "evaluation_runs": len(rows),
        },
        "training_plan": sorted(training_rows, key=lambda r: r["worker_count"]),
        "evaluation_plan_csv": str(output / "evaluation_plan.csv"),
        "previous_result_review": prior,
    }
    output.mkdir(parents=True)
    for name, payload in (
        ("experiment_plan.json", plan),
        ("source_training_audits.json", {"status": "pass", "audits": audits}),
        ("checkpoint_preflight.json", {"status": "pass", "runtime_fingerprints_match": True, "checkpoints": preflight}),
    ):
        (output / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    for name, records in (("evaluation_plan.csv", rows), ("training_plan.csv", plan["training_plan"])):
        with (output / name).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
    (output / "evaluation_commands.ps1").write_text(
        "$ErrorActionPreference = 'Stop'\n" + "\n".join(row["command"] for row in rows) + "\n",
        encoding="utf-8",
    )
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--training-roots", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--six-policies", action="store_true")
    parser.add_argument("--allow-undeclared-held-out-seeds", action="store_true")
    parser.add_argument("--previous-result", type=Path)
    args = parser.parse_args()
    plan = prepare_saved(
        args.training_roots, args.output, config=args.config, six_policies=args.six_policies,
        allow_undeclared=args.allow_undeclared_held_out_seeds, previous_result=args.previous_result,
    )
    print(json.dumps({"output": str(args.output.resolve()), "counts": plan["counts"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
