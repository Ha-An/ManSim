from __future__ import annotations

from typing import Any

from manufacturing_sim.simulation.scenarios.manufacturing.entities import MachineState, Task, Worker


class ThroughputOptimizerUnavailable(RuntimeError):
    """Raised when OR-Tools is required but not installed."""


class ThroughputOptimizerFailed(RuntimeError):
    """Raised when the CP-SAT optimizer cannot return an accepted solution."""


def require_cp_sat() -> Any:
    try:
        from ortools.sat.python import cp_model
    except ModuleNotFoundError as exc:
        raise ThroughputOptimizerUnavailable(
            "decision=rolling_horizon_throughput_optimizer requires OR-Tools. "
            "Install it with: python -m pip install -r requirements.txt"
        ) from exc
    return cp_model


def cp_sat_status_name(cp_model: Any, status: int) -> str:
    names = {
        getattr(cp_model, "OPTIMAL", 4): "OPTIMAL",
        getattr(cp_model, "FEASIBLE", 2): "FEASIBLE",
        getattr(cp_model, "INFEASIBLE", 3): "INFEASIBLE",
        getattr(cp_model, "MODEL_INVALID", 1): "MODEL_INVALID",
        getattr(cp_model, "UNKNOWN", 0): "UNKNOWN",
    }
    return names.get(status, str(status))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _queue_length(value: Any) -> int:
    try:
        return len(value)
    except TypeError:
        return 0


def _station_from_task(task: Task) -> int | None:
    payload = task.payload if isinstance(task.payload, dict) else {}
    for key in ("station", "target_station", "from_station"):
        raw = payload.get(key)
        try:
            if raw is not None:
                return int(raw)
        except (TypeError, ValueError):
            continue
    return None


def _machine_from_task(world: Any, task: Task) -> Any | None:
    payload = task.payload if isinstance(task.payload, dict) else {}
    machine_id = str(payload.get("machine_id", "") or "").strip()
    if not machine_id:
        return None
    machines = getattr(world, "machines", {})
    return machines.get(machine_id) if isinstance(machines, dict) else None


def score_task_for_throughput(world: Any, agent: Worker, task: Task, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a deterministic throughput score and an explainable component payload."""

    cfg = cfg if isinstance(cfg, dict) else {}
    score_cfg = cfg.get("score", {}) if isinstance(cfg.get("score", {}), dict) else {}
    travel_penalty = _safe_float(score_cfg.get("travel_time_penalty", 0.8), 0.8)
    resource_penalty = _safe_float(score_cfg.get("resource_risk_penalty", 8.0), 8.0)
    battery_penalty = _safe_float(score_cfg.get("battery_risk_penalty", 25.0), 25.0)
    bottleneck_weight = _safe_float(score_cfg.get("bottleneck_relief_weight", 1.0), 1.0)
    downstream_weight = _safe_float(score_cfg.get("downstream_progress_weight", 1.0), 1.0)
    continuity_weight = _safe_float(score_cfg.get("machine_continuity_weight", 1.0), 1.0)

    task_type = str(task.task_type or task.task_code or "").strip().upper()
    task_code = str(task.task_code or task.task_type or "").strip().upper()
    priority_key = str(getattr(world, "_task_priority_key", lambda item: item.priority_key)(task) or task.priority_key)
    station = _station_from_task(task)
    machine = _machine_from_task(world, task)

    bottleneck_relief = 0.0
    downstream_progress = 0.0
    machine_continuity = 0.0

    if task_type == "REPAIR_MACHINE" and machine is not None:
        if bool(getattr(machine, "broken", False)) or getattr(machine, "state", None) in {MachineState.BROKEN, MachineState.UNDER_REPAIR}:
            bottleneck_relief += 70.0
            machine_continuity += 35.0
    elif task_type == "UNLOAD_MACHINE" and machine is not None:
        if getattr(machine, "output_intermediate", None) is not None or getattr(machine, "state", None) == MachineState.DONE_WAIT_UNLOAD:
            bottleneck_relief += 45.0
            downstream_progress += 30.0
            machine_continuity += 20.0
    elif task_type == "LOAD_MACHINE" and machine is not None:
        if getattr(machine, "state", None) in {MachineState.WAIT_INPUT, MachineState.IDLE, MachineState.SETUP}:
            bottleneck_relief += 35.0
            machine_continuity += 25.0
        load_slot = str((task.payload or {}).get("load_slot", "")).strip().lower()
        if load_slot == "intermediate":
            downstream_progress += 10.0
    elif task_type == "SETUP_MACHINE" and machine is not None:
        if getattr(machine, "state", None) in {MachineState.WAIT_INPUT, MachineState.SETUP, MachineState.IDLE}:
            bottleneck_relief += 25.0
            machine_continuity += 30.0
    elif task_code == "REPLENISH_MATERIAL" or priority_key == "material_supply":
        station_key = station if station is not None else 1
        if bool(getattr(world, "is_mfg_flow_shop", False)):
            shortage = 1.0
        else:
            material_queues = getattr(world, "material_queues", {})
            inventory_targets = getattr(world, "inventory_targets", {})
            target = 0
            try:
                target = int((inventory_targets.get("material", {}) or {}).get(f"station{station_key}", 0))
            except (AttributeError, TypeError, ValueError):
                target = 0
            queue_size = _queue_length(material_queues.get(station_key)) if isinstance(material_queues, dict) else 0
            shortage = max(0, target - queue_size)
        bottleneck_relief += 20.0 + 3.0 * shortage
    elif task_type == "TRANSFER":
        transfer_kind = str((task.payload or {}).get("transfer_kind", "")).strip().lower()
        if transfer_kind == "inter_station":
            downstream_progress += 30.0
            bottleneck_relief += 15.0
        elif transfer_kind == "battery_delivery":
            bottleneck_relief += 40.0
            machine_continuity += 10.0
        else:
            downstream_progress += 15.0
    elif task_type == "INSPECT_PRODUCT":
        downstream_progress += 30.0
        bottleneck_relief += 20.0
    elif task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
        action = str((task.payload or {}).get("interface_action") or (task.payload or {}).get("action") or "").strip().lower()
        if action == "unload":
            downstream_progress += 35.0
            bottleneck_relief += 25.0
        else:
            downstream_progress += 20.0
            bottleneck_relief += 15.0
    elif task_type == "PREVENTIVE_MAINTENANCE":
        bottleneck_relief += 8.0
        machine_continuity += 20.0
    elif task_type == "BATTERY_SWAP":
        bottleneck_relief += 30.0

    try:
        estimated_duration = float(world._task_estimated_duration(agent, task))
    except Exception:
        estimated_duration = 0.0
    try:
        travel_time = float(world.travel_time(agent.location, task.location))
    except Exception:
        travel_time = 0.0

    resource_risk = 0.0
    try:
        keys = list(world._rolling_horizon_exclusive_resource_keys(task))
        queued = world._rolling_horizon_queued_resource_index() if hasattr(world, "_rolling_horizon_queued_resource_index") else {}
        active = world._rolling_horizon_active_resource_index() if hasattr(world, "_rolling_horizon_active_resource_index") else {}
        resource_risk = float(sum(1 for key in keys if key in queued or key in active))
    except Exception:
        resource_risk = 0.0

    battery_risk = 0.0
    try:
        remaining = float(world.battery_remaining(agent))
        required = estimated_duration + 5.0
        if required > 0:
            battery_risk = max(0.0, min(1.0, (required - remaining) / required))
    except Exception:
        battery_risk = 0.0

    weighted_bottleneck = bottleneck_weight * bottleneck_relief
    weighted_downstream = downstream_weight * downstream_progress
    weighted_continuity = continuity_weight * machine_continuity
    travel_cost = travel_penalty * max(travel_time, estimated_duration)
    resource_cost = resource_penalty * resource_risk
    battery_cost = battery_penalty * battery_risk
    total = weighted_bottleneck + weighted_downstream + weighted_continuity - travel_cost - resource_cost - battery_cost

    return {
        "total": round(float(total), 3),
        "components": {
            "bottleneck_relief": round(float(bottleneck_relief), 3),
            "downstream_progress": round(float(downstream_progress), 3),
            "machine_continuity": round(float(machine_continuity), 3),
            "estimated_duration_min": round(float(estimated_duration), 3),
            "travel_time_min": round(float(travel_time), 3),
            "resource_risk": round(float(resource_risk), 3),
            "battery_risk": round(float(battery_risk), 3),
            "weighted_bottleneck": round(float(weighted_bottleneck), 3),
            "weighted_downstream": round(float(weighted_downstream), 3),
            "weighted_continuity": round(float(weighted_continuity), 3),
            "travel_cost": round(float(travel_cost), 3),
            "resource_cost": round(float(resource_cost), 3),
            "battery_cost": round(float(battery_cost), 3),
        },
    }
