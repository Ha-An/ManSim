from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = EXPERIMENT_DIR / "config.yaml"
DEFAULT_RESULTS_DIR = EXPERIMENT_DIR / "results"
STATUS_CSV = "run_status.csv"
FAIRNESS_CSV = "fairness_report.csv"
AUDIT_CSV = "audit_summary.csv"
RUN_SUMMARY_CSV = "comparison_summary.csv"
MODE_SUMMARY_CSV = "mode_summary.csv"
MODE_WORKER_SUMMARY_CSV = "mode_worker_summary.csv"
SUMMARY_JSON = "comparison_summary.json"
DASHBOARD_HTML = "comparison_dashboard.html"

DEFAULT_MODES = [
    "fixed_priority",
    "adaptive_priority",
    "rolling_horizon_aging_priority",
    "rolling_horizon_dedicated_roles",
    "bottleneck_aware_dispatch",
    "rolling_horizon_throughput_optimizer",
]


@dataclass(frozen=True)
class ExperimentConfig:
    scenario: str
    horizon_days: int
    minutes_per_day: float
    seeds: list[int]
    worker_counts: list[int]
    modes: list[str]
    common_overrides: list[str]
    metrics: dict[str, list[str]]


@dataclass(frozen=True)
class RunSpec:
    mode: str
    seed: int
    worker_count: int
    run_dir: Path
    horizon_days: int
    scenario: str
    minutes_per_day: float

    @property
    def run_id(self) -> str:
        return f"{self.mode}__workers_{self.worker_count}__seed_{self.seed}"


def load_experiment_config(path: Path = DEFAULT_CONFIG_PATH) -> ExperimentConfig:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    scenario = str(data.get("scenario", "factory_mfg_basic")).strip() or "factory_mfg_basic"
    horizon_days = int(data.get("horizon_days", 5) or 5)
    minutes_per_day = float(data.get("minutes_per_day", 240) or 240)
    seeds = [int(seed) for seed in data.get("seeds", [2026, 2027, 2028, 2029, 2030])]
    worker_counts = [int(count) for count in data.get("worker_counts", [3, 4, 5, 6, 7, 8])]
    modes = [str(mode).strip() for mode in data.get("modes", DEFAULT_MODES) if str(mode).strip()]
    common_overrides = [str(item).strip() for item in data.get("common_overrides", []) if str(item).strip()]
    metrics = data.get("metrics", {}) if isinstance(data.get("metrics", {}), dict) else {}
    normalized_metrics = {
        str(group): [str(metric) for metric in values]
        for group, values in metrics.items()
        if isinstance(values, list)
    }
    return ExperimentConfig(
        scenario=scenario,
        horizon_days=horizon_days,
        minutes_per_day=minutes_per_day,
        seeds=seeds,
        worker_counts=worker_counts,
        modes=modes,
        common_overrides=common_overrides,
        metrics=normalized_metrics,
    )


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def resolve_results_root(output_root: str | Path | None = None, *, stamp: str | None = None) -> Path:
    if output_root:
        return Path(output_root).resolve()
    return (DEFAULT_RESULTS_DIR / (stamp or timestamp())).resolve()


def build_run_specs(
    cfg: ExperimentConfig,
    output_root: Path,
    *,
    modes: Iterable[str] | None = None,
    seeds: Iterable[int] | None = None,
    worker_counts: Iterable[int] | None = None,
    days: int | None = None,
    limit: int | None = None,
) -> list[RunSpec]:
    selected_modes = [str(mode).strip() for mode in (modes if modes is not None else cfg.modes) if str(mode).strip()]
    selected_seeds = [int(seed) for seed in (seeds if seeds is not None else cfg.seeds)]
    selected_worker_counts = [int(count) for count in (worker_counts if worker_counts is not None else cfg.worker_counts)]
    horizon_days = int(days if days is not None else cfg.horizon_days)
    specs: list[RunSpec] = []
    for mode in selected_modes:
        for worker_count in selected_worker_counts:
            for seed in selected_seeds:
                run_dir = output_root / "runs" / mode / f"workers_{worker_count}" / f"seed_{seed}"
                specs.append(
                    RunSpec(
                        mode=mode,
                        seed=seed,
                        worker_count=worker_count,
                        run_dir=run_dir,
                        horizon_days=horizon_days,
                        scenario=cfg.scenario,
                        minutes_per_day=cfg.minutes_per_day,
                    )
                )
    if limit is not None and limit >= 0:
        specs = specs[:limit]
    return specs


DEDICATED_ROLE_GROUPS = {
    "G1": ["REPLENISH_MATERIAL"],
    "G2": ["REPAIR_MACHINE", "LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE"],
    "G3": ["MANAGE_ROBOT_POWER", "TRANSFER", "INSPECT_PRODUCT", "COLLECT_WASTE_OR_SCRAP", "PREVENTIVE_MAINTENANCE"],
}


def dedicated_role_template(worker_count: int) -> dict[str, list[str]]:
    worker_count = max(1, int(worker_count))
    if worker_count == 1:
        merged: list[str] = []
        for group in ("G1", "G2", "G3"):
            merged.extend(DEDICATED_ROLE_GROUPS[group])
        return {"A1": merged}
    if worker_count == 2:
        return {
            "A1": list(DEDICATED_ROLE_GROUPS["G1"]),
            "A2": list(DEDICATED_ROLE_GROUPS["G2"] + DEDICATED_ROLE_GROUPS["G3"]),
        }
    assignments: dict[str, list[str]] = {}
    cycle = ("G1", "G2", "G3")
    for index in range(1, worker_count + 1):
        group = cycle[(index - 1) % len(cycle)]
        assignments[f"A{index}"] = list(DEDICATED_ROLE_GROUPS[group])
    return assignments


def dedicated_role_overrides(worker_count: int) -> list[str]:
    role_map = dedicated_role_template(worker_count)
    overrides: list[str] = []
    for worker_id, task_codes in role_map.items():
        value = "[" + ",".join(task_codes) + "]"
        try:
            worker_index = int(str(worker_id).removeprefix("A"))
        except ValueError:
            worker_index = 0
        prefix = "+" if worker_index > 3 else ""
        overrides.append(f"{prefix}decision.rolling_horizon.scenario_worker_task_priority.factory_mfg_basic.{worker_id}={value}")
    providers = [worker_id for worker_id, task_codes in role_map.items() if "MANAGE_ROBOT_POWER" in task_codes]
    receivers = [worker_id for worker_id in role_map if worker_id not in set(providers)]
    overrides.append("decision.battery.delivery_provider_agent_ids=[" + ",".join(providers) + "]")
    overrides.append("decision.battery.delivery_receiver_agent_ids=[" + ",".join(receivers) + "]")
    return overrides


def build_run_command(spec: RunSpec, common_overrides: Iterable[str] = ()) -> list[str]:
    run_dir = spec.run_dir.resolve().as_posix()
    command = [
        sys.executable,
        str((REPO_ROOT / "main.py").resolve()),
        f"scenario={spec.scenario}",
        f"decision={spec.mode}",
        f"seed={spec.seed}",
        f"scenario.factory.num_workers={spec.worker_count}",
        f"scenario.horizon.num_days={spec.horizon_days}",
        f"scenario.horizon.minutes_per_day={spec.minutes_per_day:g}",
        "runtime.ui.auto_open_results=false",
        f"hydra.run.dir={run_dir}",
    ]
    if spec.mode == "rolling_horizon_dedicated_roles":
        command.extend(dedicated_role_overrides(spec.worker_count))
    for override in common_overrides:
        if override and override not in command:
            command.append(str(override))
    return command


def check_optimizer_dependency(mode: str) -> tuple[bool, str]:
    if mode != "rolling_horizon_throughput_optimizer":
        return True, ""
    try:
        from ortools.sat.python import cp_model  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on local env
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        seen: list[str] = []
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.append(key)
        fieldnames = seen
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(row.get(key, "")) for key in fieldnames})


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def append_csv(path: Path, row: dict[str, Any], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow({key: _csv_value(row.get(key, "")) for key in fieldnames})


def upsert_csv(path: Path, row: dict[str, Any], fieldnames: list[str], *, key: str) -> None:
    rows = read_csv(path)
    key_value = str(row.get(key, ""))
    replaced = False
    normalized = {field: _csv_value(row.get(field, "")) for field in fieldnames}
    for index, existing in enumerate(rows):
        if str(existing.get(key, "")) == key_value:
            rows[index] = normalized
            replaced = True
            break
    if not replaced:
        rows.append(normalized)
    write_csv(path, rows, fieldnames)


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def nested_get(data: dict[str, Any], path: str, default: Any = "") -> Any:
    current: Any = data
    for part in str(path).split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def stable_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def relative_link(from_path: Path, target: Path) -> str:
    try:
        return os.path.relpath(target.resolve(), start=from_path.resolve().parent).replace("\\", "/")
    except Exception:
        return target.as_posix()


def run_subprocess(command: list[str], *, cwd: Path = REPO_ROOT, log_path: Path | None = None) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = completed.stdout or ""
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(output, encoding="utf-8")
    return int(completed.returncode), output


def metric_paths(cfg: ExperimentConfig) -> list[str]:
    paths: list[str] = []
    for values in cfg.metrics.values():
        for path in values:
            if path not in paths:
                paths.append(path)
    return paths


def as_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def sample_std(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    avg = mean(values)
    return math.sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))


def discover_run_dirs(results_root: Path) -> list[Path]:
    runs_root = results_root / "runs"
    if not runs_root.exists():
        return []
    nested = sorted(path for path in runs_root.glob("*/workers_*/*") if path.is_dir())
    legacy = sorted(path for path in runs_root.glob("*/*") if path.is_dir() and not path.name.startswith("workers_"))
    return nested + legacy


def worker_count_from_run_dir(run_dir: Path) -> int:
    for part in run_dir.parts:
        if part.startswith("workers_"):
            try:
                return int(part.removeprefix("workers_"))
            except ValueError:
                return 0
    diagnostics = read_json(run_dir / "pre_run_diagnostics.json")
    inputs = diagnostics.get("inputs", {}) if isinstance(diagnostics.get("inputs", {}), dict) else {}
    worker_ids = inputs.get("worker_ids", [])
    if isinstance(worker_ids, list) and worker_ids:
        return len(worker_ids)
    return 3


def load_run_identity(run_dir: Path) -> dict[str, Any]:
    run_meta = read_json(run_dir / "run_meta.json")
    kpi = read_json(run_dir / "kpi.json")
    return {
        "run_dir": str(run_dir.resolve()),
        "mode": str(run_meta.get("decision_mode") or kpi.get("run_meta", {}).get("decision_mode") or "").strip(),
        "seed": int(run_meta.get("seed") or kpi.get("run_meta", {}).get("seed") or 0),
        "worker_count": worker_count_from_run_dir(run_dir),
        "scenario": str(run_meta.get("scenario_type") or kpi.get("scenario_type") or "").strip(),
        "total_days": int(run_meta.get("total_days") or kpi.get("run_meta", {}).get("total_days") or 0),
        "minutes_per_day": float(run_meta.get("minutes_per_day") or kpi.get("run_meta", {}).get("minutes_per_day") or 0.0),
    }
