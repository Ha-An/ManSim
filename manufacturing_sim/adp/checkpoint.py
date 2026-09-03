from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .model import build_value_network, require_torch
from .schema import FEATURE_SCHEMA_VERSION


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
        "machine_ids": sorted(str(machine_id) for machine_id in world.machines),
        "machines_per_station": int(world.machines_per_station),
        "processing_time": {
            str(station): distribution.to_dict()
            for station, distribution in sorted(world.processing_time_distribution.items())
        },
        "buffer_capacities": dict(sorted(getattr(world, "buffer_capacities", {}).items())),
        "inspection_capacity": int(getattr(world, "inspection_capacity", 1)),
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
) -> dict[str, Any]:
    if wait_action_enabled is None:
        wait_action_enabled = bool(
            getattr(getattr(world, "adp_coordinator", None), "allow_wait_action", False)
        )
    wait_action_enabled = bool(wait_action_enabled)
    payload = {
        "scenario_type": str(world.scenario_key),
        "objective_mode": str(world.objective_mode),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "timing_fingerprint": str(world.timing.profile_fingerprint),
        "environment_fingerprint": _environment_fingerprint(world),
        "reward_mode": "completed_product_mc",
        "loss_type": "mse",
        "random_policy": (
            "uniform_random_feasible_with_wait"
            if wait_action_enabled
            else "uniform_random_feasible_no_wait"
        ),
        "wait_action_enabled": wait_action_enabled,
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
) -> None:
    torch = require_torch()
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "optimizer_state_dict": optimizer.state_dict() if optimizer is not None else None,
            "manifest": manifest,
        },
        path,
    )


def load_checkpoint(
    path: str | Path,
    *,
    world: Any,
    device: Any,
    wait_action_enabled: bool | None = None,
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
    )
    mismatches = {
        key: {"expected": expected[key], "actual": manifest.get(key)}
        for key in (
            "scenario_type",
            "objective_mode",
            "feature_schema_version",
            "timing_fingerprint",
            "environment_fingerprint",
            "reward_mode",
            "loss_type",
            "random_policy",
            "wait_action_enabled",
            "potential_shaping",
        )
        if str(manifest.get(key, "")) != str(expected[key])
    }
    worker_range = manifest.get("worker_count_range", [3, 6])
    if not isinstance(worker_range, list) or len(worker_range) != 2:
        mismatches["worker_count_range"] = {"expected": "[min,max]", "actual": worker_range}
    elif not int(worker_range[0]) <= int(world.num_workers) <= int(worker_range[1]):
        mismatches["worker_count"] = {"expected": worker_range, "actual": world.num_workers}
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
