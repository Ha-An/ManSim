from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .model import build_value_network, require_torch
from .schema import FEATURE_SCHEMA_VERSION
from manufacturing_sim.simulation.scenarios.manufacturing.entities import MACHINE_LIFECYCLE_CONTRACT


def _environment_fingerprint(world: Any) -> str:
    grid = getattr(world, "grid_map", None)
    objects = []
    if grid is not None:
        for object_id, obj in sorted(getattr(grid, "objects", {}).items()):
            objects.append(
                {
                    "id": str(object_id),
                    "type": str(getattr(obj, "object_type", "")),
                    "x": int(getattr(obj, "x", 0)),
                    "y": int(getattr(obj, "y", 0)),
                    "w": int(getattr(obj, "width", 0)),
                    "h": int(getattr(obj, "height", 0)),
                }
            )
    contract = {
        "machine_lifecycle_contract": MACHINE_LIFECYCLE_CONTRACT,
        "machine_ids": sorted(str(machine_id) for machine_id in world.machines),
        "machines_per_station": int(world.machines_per_station),
        "processing_time": {
            str(station): distribution.to_dict()
            for station, distribution in sorted(world.processing_time_distribution.items())
        },
        "buffer_capacities": dict(sorted(getattr(world, "buffer_capacities", {}).items())),
        "inspection_capacity": int(getattr(world, "inspection_capacity", 1)),
        "enabled_task_codes": sorted(
            str(code) for code in getattr(world, "enabled_task_codes", set())
        ),
        "role_count": int(
            len(getattr(getattr(world, "mfg_flow_task_policy", None), "rules", []))
        ),
        "machine_failure": {
            "distribution": str(getattr(world, "machine_failure_distribution", "")),
            "time_basis": str(getattr(world, "machine_failure_time_basis", "")),
            "mean_processing_time_to_failure_min": float(
                getattr(world, "machine_failure_mean_exposure_min", 0.0)
            ),
            "preventive_maintenance_enabled": bool(
                getattr(world, "preventive_maintenance_enabled", False)
            ),
            "preventive_maintenance_due_processing_min": float(
                getattr(world, "pm_interval_target_min", 0.0)
            ),
            "protected_processing_min": float(
                getattr(world, "pm_effect_duration_min", 0.0)
            ),
            "hazard_multiplier": float(getattr(world, "pm_lambda_multiplier", 1.0)),
        },
        "battery_policy": {
            "assignment_mode": str(getattr(world, "battery_assignment_mode", "hard_reserve")),
            "expose_risk_metadata": bool(getattr(world, "expose_battery_risk_metadata", False)),
            "depleted_recovery_enabled": bool(getattr(world, "depleted_recovery_enabled", False)),
            "depleted_recovery_schedule": str(getattr(world, "depleted_recovery_schedule", "")),
            "depleted_recovery_restart_location": str(
                getattr(world, "depleted_recovery_restart_location", "")
            ),
            "depleted_recovery_restart_soc": float(
                getattr(world, "depleted_recovery_restart_soc", 0.0)
            ),
        },
        "map": {
            "width": int(getattr(grid, "width_tiles", 0)) if grid is not None else 0,
            "height": int(getattr(grid, "height_tiles", 0)) if grid is not None else 0,
            "objects": objects,
        },
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def checkpoint_fingerprint(
    world: Any,
    *,
    wait_action_enabled: bool | None = None,
    worker_order_strategy: str | None = None,
    return_estimator: str = "monte_carlo",
) -> dict[str, Any]:
    if return_estimator not in {"monte_carlo", "n_step_td"}:
        raise ValueError("Unsupported ADP return estimator.")
    if wait_action_enabled is None:
        wait_action_enabled = bool(
            getattr(getattr(world, "adp_coordinator", None), "allow_wait_action", False)
        )
    wait_action_enabled = bool(wait_action_enabled)
    if worker_order_strategy is None:
        worker_order_strategy = str(
            getattr(
                getattr(world, "adp_coordinator", None),
                "worker_order_strategy",
                "cyclic",
            )
            or "cyclic"
        )
    worker_order_strategy = str(worker_order_strategy).strip().lower()
    payload = {
        "scenario_type": str(world.scenario_key),
        "objective_mode": str(world.objective_mode),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "timing_fingerprint": str(world.timing.profile_fingerprint),
        "environment_fingerprint": _environment_fingerprint(world),
        "reward_mode": "completed_product_td" if return_estimator == "n_step_td" else "completed_product_mc",
        "loss_type": "mse",
        "random_policy": (
            "uniform_random_feasible_with_wait"
            if wait_action_enabled
            else "uniform_random_feasible_no_wait"
        ),
        "wait_action_enabled": wait_action_enabled,
        "worker_order_strategy": worker_order_strategy,
        "potential_shaping": False,
    }
    payload["fingerprint"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def save_checkpoint(
    path: Path,
    *,
    model: Any,
    optimizer: Any | None,
    manifest: dict[str, Any],
    target_model: Any | None = None,
) -> None:
    torch = require_torch()
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict() if optimizer is not None else None,
            "manifest": manifest,
            "target_model_state_dict": target_model.state_dict() if target_model is not None else None,
        },
        path,
    )


def load_checkpoint(
    path: str | Path,
    *,
    world: Any,
    device: Any,
    wait_action_enabled: bool | None = None,
    worker_order_strategy: str | None = None,
) -> tuple[Any, dict[str, Any]]:
    torch = require_torch()
    checkpoint_path = Path(path).expanduser().resolve()
    if not checkpoint_path.is_file():
        raise RuntimeError(f"ADP checkpoint does not exist: {checkpoint_path}")
    payload = torch.load(checkpoint_path, map_location=device, weights_only=False)
    manifest = payload.get("manifest", {}) if isinstance(payload, dict) else {}
    expected = checkpoint_fingerprint(
        world,
        wait_action_enabled=wait_action_enabled,
        worker_order_strategy=worker_order_strategy,
        return_estimator=str(manifest.get("return_estimator", "monte_carlo")),
    )
    mismatches = {
        key: {"expected": expected[key], "actual": manifest.get(key)}
        for key in (
            "scenario_type",
            "objective_mode",
            "feature_schema_version",
            "timing_fingerprint",
            "reward_mode",
            "loss_type",
            "random_policy",
            "wait_action_enabled",
            "worker_order_strategy",
            "potential_shaping",
        )
        if str(manifest.get(key, "")) != str(expected[key])
    }
    worker_count = int(world.num_workers)
    supported_counts = manifest.get("supported_worker_counts")
    if isinstance(supported_counts, list) and supported_counts:
        normalized_counts = sorted({int(value) for value in supported_counts})
        if worker_count not in normalized_counts:
            mismatches["worker_count"] = {
                "expected": normalized_counts,
                "actual": worker_count,
            }
    else:
        worker_range = manifest.get("worker_count_range", [3, 6])
        if not isinstance(worker_range, list) or len(worker_range) != 2:
            mismatches["worker_count_range"] = {
                "expected": "[min,max]",
                "actual": worker_range,
            }
        elif not int(worker_range[0]) <= worker_count <= int(worker_range[1]):
            mismatches["worker_count"] = {
                "expected": worker_range,
                "actual": worker_count,
            }

    environment_by_worker = manifest.get("environment_fingerprints_by_worker_count")
    if isinstance(environment_by_worker, dict) and environment_by_worker:
        trained_environment = str(environment_by_worker.get(str(worker_count), ""))
        if trained_environment != str(expected["environment_fingerprint"]):
            mismatches["environment_fingerprint"] = {
                "expected": expected["environment_fingerprint"],
                "actual": trained_environment,
                "worker_count": worker_count,
            }
    elif str(manifest.get("environment_fingerprint", "")) != str(
        expected["environment_fingerprint"]
    ):
        mismatches["environment_fingerprint"] = {
            "expected": expected["environment_fingerprint"],
            "actual": manifest.get("environment_fingerprint"),
        }
    if mismatches:
        raise RuntimeError("ADP checkpoint fingerprint mismatch: " + json.dumps(mismatches, sort_keys=True))
    model_cfg = manifest.get("model", {}) if isinstance(manifest.get("model", {}), dict) else {}
    model = build_value_network(
        embedding_dim=int(model_cfg.get("embedding_dim", 128)),
        heads=int(model_cfg.get("heads", 4)),
        layers=int(model_cfg.get("layers", 3)),
    ).to(device)
    model.load_state_dict(payload["model_state_dict"])
    model.eval()
    manifest = dict(manifest)
    manifest["checkpoint_path"] = str(checkpoint_path)
    return model, manifest
