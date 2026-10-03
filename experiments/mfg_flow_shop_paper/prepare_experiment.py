from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = Path(__file__).resolve().with_name("config.yaml")


@dataclass(frozen=True)
class PaperConfig:
    study_name: str
    scenario: str
    objective_mode: str
    horizon_days: int
    minutes_per_day: int
    rolling_window_min: float
    worker_counts: list[int]
    training_replicates: int
    training_template: Path
    policies: list[str]
    test_seeds: list[int]
    screening_iterations: list[int]
    screening_seed_count: int
    final_candidate_count: int
    final_selection_seed_count: int
    seed_layout: dict[str, int]
    training_jobs: int
    evaluation_jobs: int
    rollout_processes: int


def load_config(path: Path = DEFAULT_CONFIG) -> PaperConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    execution = raw.get("execution", {})
    validation = raw.get("validation", {})
    cfg = PaperConfig(
        study_name=str(raw["study_name"]),
        scenario=str(raw["scenario"]),
        objective_mode=str(raw["objective_mode"]),
        horizon_days=int(raw["horizon_days"]),
        minutes_per_day=int(raw["minutes_per_day"]),
        rolling_window_min=float(raw["rolling_horizon_window_min"]),
        worker_counts=[int(value) for value in raw["worker_counts"]],
        training_replicates=int(raw["adp_training_replicates"]),
        training_template=(REPO_ROOT / str(raw["adp_training_template"])).resolve(),
        policies=[str(value) for value in raw["policies"]],
        test_seeds=[int(value) for value in raw["test_seeds"]],
        screening_iterations=[int(value) for value in validation["screening_iterations"]],
        screening_seed_count=int(validation["screening_seed_count"]),
        final_candidate_count=int(validation["final_candidate_count"]),
        final_selection_seed_count=int(validation["final_selection_seed_count"]),
        seed_layout={str(key): int(value) for key, value in raw["seed_layout"].items()},
        training_jobs=int(execution.get("training_jobs", 1)),
        evaluation_jobs=int(execution.get("evaluation_jobs", 5)),
        rollout_processes=int(execution.get("rollout_processes_per_training", 10)),
    )
    validate_config(cfg)
    return cfg


def training_seed(cfg: PaperConfig, worker_count: int, replicate: int) -> int:
    return (
        cfg.seed_layout["training_base"]
        + int(worker_count) * cfg.seed_layout["worker_stride"]
        + int(replicate) * cfg.seed_layout["replicate_stride"]
    )


def screening_seeds(cfg: PaperConfig, worker_count: int) -> list[int]:
    start = cfg.seed_layout["screening_base"] + int(worker_count) * 1000
    return list(range(start, start + cfg.screening_seed_count))


def final_selection_seeds(cfg: PaperConfig, worker_count: int) -> list[int]:
    start = cfg.seed_layout["final_selection_base"] + int(worker_count) * 1000
    return list(range(start, start + cfg.final_selection_seed_count))


def validate_config(cfg: PaperConfig) -> None:
    if cfg.scenario != "mfg_flow_shop" or cfg.objective_mode != "maximize_throughput":
        raise ValueError("The confirmatory suite supports mfg_flow_shop throughput only.")
    if cfg.rolling_window_min <= 0:
        raise ValueError("rolling_horizon_window_min must be positive.")
    if cfg.worker_counts != [2, 3, 4, 5, 6]:
        raise ValueError("Paper worker_counts must be exactly [2,3,4,5,6].")
    if cfg.training_replicates != 1:
        raise ValueError("The compact experiment requires one ADP training run per worker count.")
    if len(cfg.test_seeds) != 20 or len(set(cfg.test_seeds)) != 20:
        raise ValueError("Exactly 20 unique final test seeds are required.")
    expected_policies = {
        "immediate_shared",
        "random_feasible_dispatch",
        "simulation_based_adp",
    }
    if set(cfg.policies) != expected_policies or len(cfg.policies) != len(expected_policies):
        raise ValueError("The compact comparison must contain ADP, Immediate Shared and Random Feasible exactly once.")
    if not cfg.training_template.is_file():
        raise ValueError(f"Missing ADP training template: {cfg.training_template}")

    used_training: set[int] = set()
    validation: set[int] = set()
    template = yaml.safe_load(cfg.training_template.read_text(encoding="utf-8")) or {}
    policy_iterations = int(template["training"]["policy_iterations"])
    if cfg.screening_iterations != sorted(set(cfg.screening_iterations)):
        raise ValueError("screening_iterations must be unique and sorted.")
    if not cfg.screening_iterations or cfg.screening_iterations[0] != 0:
        raise ValueError("screening_iterations must include iteration 0.")
    if cfg.screening_iterations[-1] != policy_iterations:
        raise ValueError("screening_iterations must include the final policy iteration.")
    if cfg.screening_seed_count < 1 or cfg.final_selection_seed_count < 1:
        raise ValueError("Validation seed counts must be positive.")
    if cfg.final_candidate_count < 1 or cfg.final_candidate_count > len(cfg.screening_iterations):
        raise ValueError("final_candidate_count is outside the screening checkpoint range.")
    training_episode_count = int(template["training"]["initial_random_episodes"]) + int(
        template["training"]["policy_iterations"]
    ) * int(template["training"]["episodes_per_iteration"])
    for worker_count in cfg.worker_counts:
        validation.update(screening_seeds(cfg, worker_count))
        validation.update(final_selection_seeds(cfg, worker_count))
        for replicate in range(1, cfg.training_replicates + 1):
            seeds = set(range(training_seed(cfg, worker_count, replicate),
                              training_seed(cfg, worker_count, replicate) + training_episode_count))
            if used_training & seeds:
                raise ValueError("Training seed ranges overlap.")
            used_training.update(seeds)
    test = set(cfg.test_seeds)
    if used_training & validation or used_training & test or validation & test:
        raise ValueError("Training, validation and final-test seed partitions must be disjoint.")


def training_config(
    cfg: PaperConfig,
    *,
    worker_count: int,
    replicate: int,
    output_root: Path,
) -> dict[str, Any]:
    payload = yaml.safe_load(cfg.training_template.read_text(encoding="utf-8")) or {}
    payload["worker_counts"] = [int(worker_count)]
    payload["base_seed"] = training_seed(cfg, worker_count, replicate)
    payload["seed_partitions"] = {
        "enforce_disjoint": True,
        "screening_validation": screening_seeds(cfg, worker_count),
        "final_selection_validation": final_selection_seeds(cfg, worker_count),
        "held_out_test": list(cfg.test_seeds),
    }
    payload["rollout"]["process_count"] = cfg.rollout_processes
    payload["rollout"]["wave_size"] = cfg.rollout_processes
    payload["validation"]["screening_interval_iterations"] = 15
    payload["validation"]["screening_iterations"] = list(cfg.screening_iterations)
    payload["validation"]["screening_include_initial"] = True
    payload["validation"]["screening_seed_count"] = cfg.screening_seed_count
    payload["validation"]["final_candidate_count"] = cfg.final_candidate_count
    payload["validation"]["final_selection_seed_count"] = cfg.final_selection_seed_count
    payload["runtime"]["output_root"] = str(output_root.resolve())
    payload["runtime"]["auto_open_dashboard"] = False
    return payload


def expected_counts(cfg: PaperConfig) -> dict[str, int]:
    worker_count = len(cfg.worker_counts)
    seed_count = len(cfg.test_seeds)
    baseline_policies = len(cfg.policies) - 1
    baseline_runs = baseline_policies * worker_count * seed_count
    adp_runs = cfg.training_replicates * worker_count * seed_count
    return {
        "adp_training_runs": cfg.training_replicates * worker_count,
        "baseline_evaluation_runs": baseline_runs,
        "adp_evaluation_runs": adp_runs,
        "evaluation_runs": baseline_runs + adp_runs,
    }


def _main_command(
    cfg: PaperConfig,
    *,
    mode: str,
    worker_count: int,
    seed: int,
    run_dir: Path,
    checkpoint_path: Path | None = None,
) -> list[str]:
    python = (REPO_ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    command = [
        str(python),
        str((REPO_ROOT / "main.py").resolve()),
        f"scenario={cfg.scenario}",
        f"decision={mode}",
        f"seed={seed}",
        f"scenario.factory.num_workers={worker_count}",
        f"scenario.objective.mode={cfg.objective_mode}",
        f"scenario.horizon.num_days={cfg.horizon_days}",
        f"scenario.horizon.minutes_per_day={cfg.minutes_per_day}",
        "runtime.ui.auto_open_results=false",
        "runtime.ui.export_replay_artifacts=false",
        "runtime.artifacts.export_events=false",
        f"hydra.run.dir={run_dir.resolve().as_posix()}",
    ]
    if checkpoint_path is not None:
        command.append(f"decision.adp.checkpoint_path={checkpoint_path.resolve().as_posix()}")
    if mode.startswith("rolling_horizon_"):
        command.append(f"decision.rolling_horizon.window_min={cfg.rolling_window_min:g}")
    return command


def _powershell_command(command: list[str]) -> str:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    return "& " + " ".join(quote(value) for value in command)


def evaluation_plan_rows(cfg: PaperConfig, output_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    baseline_modes = [mode for mode in cfg.policies if mode != "simulation_based_adp"]
    evaluation_root = output_root.resolve() / "evaluation" / "runs"
    for mode in baseline_modes:
        for worker_count in cfg.worker_counts:
            for seed in cfg.test_seeds:
                run_dir = evaluation_root / mode / f"workers_{worker_count}" / f"seed_{seed}"
                command = _main_command(
                    cfg,
                    mode=mode,
                    worker_count=worker_count,
                    seed=seed,
                    run_dir=run_dir,
                )
                rows.append({
                    "run_id": f"{mode}__workers_{worker_count}__seed_{seed}",
                    "mode": mode,
                    "worker_count": worker_count,
                    "seed": seed,
                    "training_replicate": "",
                    "checkpoint_path": "",
                    "run_dir": str(run_dir),
                    "command_json": json.dumps(command),
                    "command": _powershell_command(command),
                })
    for replicate in range(1, cfg.training_replicates + 1):
        for worker_count in cfg.worker_counts:
            checkpoint = (
                output_root.resolve()
                / "training"
                / f"workers_{worker_count}"
                / f"replicate_{replicate}"
                / "best.pt"
            )
            for seed in cfg.test_seeds:
                run_dir = (
                    evaluation_root
                    / "simulation_based_adp"
                    / f"replicate_{replicate}"
                    / f"workers_{worker_count}"
                    / f"seed_{seed}"
                )
                command = _main_command(
                    cfg,
                    mode="simulation_based_adp",
                    worker_count=worker_count,
                    seed=seed,
                    run_dir=run_dir,
                    checkpoint_path=checkpoint,
                )
                rows.append({
                    "run_id": f"simulation_based_adp__replicate_{replicate}__workers_{worker_count}__seed_{seed}",
                    "mode": "simulation_based_adp",
                    "worker_count": worker_count,
                    "seed": seed,
                    "training_replicate": replicate,
                    "checkpoint_path": str(checkpoint),
                    "run_dir": str(run_dir),
                    "command_json": json.dumps(command),
                    "command": _powershell_command(command),
                })
    return rows


def prepare(cfg: PaperConfig, output_root: Path) -> dict[str, Any]:
    output_root = output_root.resolve()
    config_root = output_root / "training_configs"
    rows: list[dict[str, Any]] = []
    commands: list[str] = []
    python = (REPO_ROOT / ".venv" / "Scripts" / "python.exe").resolve()
    for worker_count in cfg.worker_counts:
        for replicate in range(1, cfg.training_replicates + 1):
            training_output = output_root / "training" / f"workers_{worker_count}" / f"replicate_{replicate}"
            config_path = config_root / f"workers_{worker_count}" / f"replicate_{replicate}.yaml"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = training_config(
                cfg,
                worker_count=worker_count,
                replicate=replicate,
                output_root=training_output,
            )
            config_path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
            command = (
                f'& "{python}" -m manufacturing_sim.adp.train '
                f'--config "{config_path}" --output "{training_output}" --no-open-dashboard'
            )
            commands.append(command)
            rows.append({
                "worker_count": worker_count,
                "training_replicate": replicate,
                "base_seed": payload["base_seed"],
                "config_path": str(config_path),
                "training_output": str(training_output),
                "checkpoint_path": str(training_output / "best.pt"),
                "command": command,
            })

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "training_commands.ps1").write_text(
        "$ErrorActionPreference = 'Stop'\n" + "\n".join(commands) + "\n",
        encoding="utf-8",
    )
    with (output_root / "training_plan.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    evaluation_rows = evaluation_plan_rows(cfg, output_root)
    with (output_root / "evaluation_plan.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(evaluation_rows[0]))
        writer.writeheader()
        writer.writerows(evaluation_rows)
    (output_root / "evaluation_commands.ps1").write_text(
        "$ErrorActionPreference = 'Stop'\n" + "\n".join(row["command"] for row in evaluation_rows) + "\n",
        encoding="utf-8",
    )
    plan = {
        "study_name": cfg.study_name,
        "scenario": cfg.scenario,
        "objective_mode": cfg.objective_mode,
        "horizon_days": cfg.horizon_days,
        "minutes_per_day": cfg.minutes_per_day,
        "rolling_horizon_window_min": cfg.rolling_window_min,
        "worker_counts": cfg.worker_counts,
        "policies": cfg.policies,
        "training_replicates": cfg.training_replicates,
        "test_seeds": cfg.test_seeds,
        "validation_design": {
            "screening_iterations": cfg.screening_iterations,
            "screening_seed_count": cfg.screening_seed_count,
            "final_candidate_count": cfg.final_candidate_count,
            "final_selection_seed_count": cfg.final_selection_seed_count,
        },
        "seed_contract": "training, screening, final-selection and final-test partitions are disjoint",
        "common_random_numbers": True,
        "explicit_wait_action_enabled": False,
        "counts": expected_counts(cfg),
        "training_plan": rows,
        "evaluation_plan_csv": str(output_root / "evaluation_plan.csv"),
    }
    (output_root / "experiment_plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return plan


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare the confirmatory mfg_flow_shop paper experiment.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "prepared")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan = prepare(load_config(args.config), args.output)
    print(json.dumps(plan["counts"], indent=2))
    print(str(args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
