from __future__ import annotations

from collections import Counter, defaultdict
from math import ceil
from statistics import mean
from typing import Any


FACTORY_PRE_RUN_METRIC_CODES = [
    "worker_otc_imbalance",
    "resource_conflict_potential",
    "traffic_contention_index",
    "service_tile_scarcity",
    "robot_interaction_load",
    "power_coordination_risk",
]

FACTORY_TASK_CODES = [
    "REPLENISH_MATERIAL",
    "LOAD_MACHINE",
    "SETUP_MACHINE",
    "UNLOAD_MACHINE",
    "TRANSFER",
    "INSPECT_PRODUCT",
    "REPAIR_MACHINE",
    "PREVENTIVE_MAINTENANCE",
    "COLLECT_WASTE_OR_SCRAP",
    "MANAGE_ROBOT_POWER",
    "HANDOVER_ITEM",
]


def build_factory_pre_run_diagnostics(*, world: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """Build static, pre-run diagnostics for the factory multi-robot scenario.

    The metrics are intentionally computed from configuration, map topology,
    worker role policy, and HumanoidSim task complexity rather than from run
    outcomes. They are scenario-level indicators used before comparing modes.
    """
    scenario_type = _scenario_type(cfg)
    if scenario_type != "factory_mfg_basic":
        return {
            "schema_version": "1.0",
            "scenario_type": scenario_type,
            "supported": False,
            "reason": "Pre-run diagnostics are currently defined for factory_mfg_basic only.",
            "metrics": {},
        }

    worker_ids = _worker_ids(world, cfg)
    machine_ids = _machine_ids(world)
    station_machine_ids = _station_machine_ids(world, machine_ids)
    horizon = _horizon_inputs(cfg, world)
    task_complexity = _task_complexity_by_code()
    estimated_counts = _estimate_factory_task_counts(
        cfg=cfg,
        horizon_days=horizon["num_days"],
        minutes_per_day=horizon["minutes_per_day"],
        machine_count=len(machine_ids),
        worker_count=len(worker_ids),
    )
    role_allowlists = _worker_task_allowlists(cfg, worker_ids)
    task_loads_by_worker = _estimate_worker_complexity_loads(
        worker_ids=worker_ids,
        role_allowlists=role_allowlists,
        task_counts=estimated_counts,
        task_complexity=task_complexity,
    )
    resource_templates = _factory_resource_templates(
        task_counts=estimated_counts,
        machine_ids=machine_ids,
        station_machine_ids=station_machine_ids,
    )
    topology = _map_topology_inputs(getattr(world, "grid_map", None))
    service_targets = _service_tile_inputs(getattr(world, "grid_map", None), machine_ids=machine_ids)
    battery_inputs = _battery_inputs(cfg, world)

    metrics = {
        "worker_otc_imbalance": _metric_worker_otc_imbalance(task_loads_by_worker),
        "resource_conflict_potential": _metric_resource_conflict_potential(resource_templates),
        "traffic_contention_index": _metric_traffic_contention_index(topology, worker_count=len(worker_ids)),
        "service_tile_scarcity": _metric_service_tile_scarcity(service_targets),
        "robot_interaction_load": _metric_robot_interaction_load(
            task_counts=estimated_counts,
            task_complexity=task_complexity,
            total_complexity=sum(float(estimated_counts.get(code, 0)) * _complexity_value(task_complexity, code) for code in estimated_counts),
            decision_cfg=cfg.get("decision", {}) if isinstance(cfg.get("decision", {}), dict) else {},
        ),
        "power_coordination_risk": _metric_power_coordination_risk(
            task_loads_by_worker=task_loads_by_worker,
            horizon=horizon,
            battery_inputs=battery_inputs,
        ),
    }
    metrics = {code: _attach_metric_assessment(code, metric) for code, metric in metrics.items()}

    return {
        "schema_version": "1.0",
        "scenario_type": scenario_type,
        "supported": True,
        "metric_order": FACTORY_PRE_RUN_METRIC_CODES,
        "inputs": {
            "horizon": horizon,
            "worker_ids": worker_ids,
            "machine_ids": machine_ids,
            "station_machine_ids": station_machine_ids,
            "worker_task_allowlists": role_allowlists,
            "estimated_task_counts": estimated_counts,
            "task_complexity_by_code": {
                code: {
                    "complexity": _complexity_value(task_complexity, code),
                    "primitive_count": int((task_complexity.get(code, {}) if isinstance(task_complexity.get(code, {}), dict) else {}).get("primitive_count", 0) or 0),
                }
                for code in sorted(estimated_counts)
            },
            "resource_templates": resource_templates,
            "map_topology": topology,
            "service_tile_targets": service_targets,
            "battery": battery_inputs,
        },
        "metrics": metrics,
    }


def _scenario_type(cfg: dict[str, Any]) -> str:
    return str(cfg.get("scenario_type") or cfg.get("type") or cfg.get("name") or "factory_mfg_basic").strip().lower() or "factory_mfg_basic"


def _worker_ids(world: Any, cfg: dict[str, Any]) -> list[str]:
    workers = getattr(world, "workers", None)
    if isinstance(workers, dict) and workers:
        return sorted(str(key) for key in workers.keys())
    count = int(((cfg.get("factory", {}) if isinstance(cfg.get("factory", {}), dict) else {}).get("num_workers", 3)) or 3)
    return [f"A{idx}" for idx in range(1, count + 1)]


def _machine_ids(world: Any) -> list[str]:
    machines = getattr(world, "machines", None)
    if isinstance(machines, dict) and machines:
        return sorted(str(key) for key in machines.keys())
    return []


def _station_machine_ids(world: Any, machine_ids: list[str]) -> dict[str, list[str]]:
    by_station = getattr(world, "machines_by_station", None)
    if isinstance(by_station, dict) and by_station:
        return {
            f"station{station}": [str(machine_id) for machine_id in values]
            for station, values in sorted(by_station.items(), key=lambda item: str(item[0]))
            if isinstance(values, list)
        }
    inferred: dict[str, list[str]] = defaultdict(list)
    for machine_id in machine_ids:
        station = "station1" if "S1" in machine_id.upper() else "station2" if "S2" in machine_id.upper() else "unknown"
        inferred[station].append(machine_id)
    return dict(sorted(inferred.items()))


def _horizon_inputs(cfg: dict[str, Any], world: Any) -> dict[str, float]:
    horizon = cfg.get("horizon", {}) if isinstance(cfg.get("horizon", {}), dict) else {}
    num_days = float(horizon.get("num_days", getattr(world, "num_days", 1)) or 1)
    minutes_per_day = float(horizon.get("minutes_per_day", getattr(world, "minutes_per_day", 240)) or 240)
    return {
        "num_days": round(num_days, 3),
        "minutes_per_day": round(minutes_per_day, 3),
        "total_minutes": round(num_days * minutes_per_day, 3),
    }


def _task_complexity_by_code() -> dict[str, dict[str, Any]]:
    try:
        from humanoidsim import task_complexity_index

        return task_complexity_index()
    except Exception:
        return {code: {"complexity": 1.0, "primitive_count": 0} for code in FACTORY_TASK_CODES}


def _complexity_value(task_complexity: dict[str, dict[str, Any]], task_code: str) -> float:
    payload = task_complexity.get(str(task_code).strip().upper(), {})
    if not isinstance(payload, dict):
        return 0.0
    try:
        return float(payload.get("complexity", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _estimate_factory_task_counts(
    *,
    cfg: dict[str, Any],
    horizon_days: float,
    minutes_per_day: float,
    machine_count: int,
    worker_count: int,
) -> dict[str, int]:
    factory = cfg.get("factory", {}) if isinstance(cfg.get("factory", {}), dict) else {}
    processing = factory.get("processing_time_min", {}) if isinstance(factory.get("processing_time_min", {}), dict) else {}
    movement = cfg.get("movement", {}) if isinstance(cfg.get("movement", {}), dict) else {}
    setup_min = max(0.1, float(movement.get("setup_min", 3.0) or 3.0))
    unload_min = max(0.1, float(movement.get("unload_min", 2.0) or 2.0))
    process_times = [float(value or 0.0) for key, value in processing.items() if str(key).startswith("station")]
    avg_process_min = mean(process_times) if process_times else 25.0
    avg_cycle_min = max(1.0, avg_process_min + setup_min + unload_min)
    total_minutes = float(horizon_days) * float(minutes_per_day)
    estimated_machine_cycles = max(1, int((total_minutes / avg_cycle_min) * max(1, machine_count) * 0.75))
    inspection_cycles = max(1, int(estimated_machine_cycles * 0.45))
    repair_count = max(1, int(ceil(max(1, machine_count) * float(horizon_days) * 0.6)))
    pm_count = max(0, int(ceil(max(1, machine_count) * float(horizon_days) * 0.2)))
    battery_count = max(1, int(ceil(max(1, worker_count) * float(horizon_days) * 0.6)))
    return {
        "REPLENISH_MATERIAL": max(1, int(ceil(estimated_machine_cycles * 1.15))),
        "LOAD_MACHINE": estimated_machine_cycles,
        "SETUP_MACHINE": estimated_machine_cycles,
        "UNLOAD_MACHINE": estimated_machine_cycles,
        "TRANSFER": max(1, int(ceil(inspection_cycles * 1.25))),
        "INSPECT_PRODUCT": inspection_cycles,
        "REPAIR_MACHINE": repair_count,
        "PREVENTIVE_MAINTENANCE": pm_count,
        "COLLECT_WASTE_OR_SCRAP": max(1, int(ceil(inspection_cycles * 0.12))),
        "MANAGE_ROBOT_POWER": battery_count,
        "HANDOVER_ITEM": 0,
    }


def _worker_task_allowlists(cfg: dict[str, Any], worker_ids: list[str]) -> dict[str, list[str]]:
    decision = cfg.get("decision", {}) if isinstance(cfg.get("decision", {}), dict) else {}
    rolling = decision.get("rolling_horizon", {}) if isinstance(decision.get("rolling_horizon", {}), dict) else {}
    scenario_map = rolling.get("scenario_worker_task_priority", {}) if isinstance(rolling.get("scenario_worker_task_priority", {}), dict) else {}
    raw = scenario_map.get("factory_mfg_basic")
    if not isinstance(raw, dict) or not raw:
        raw = rolling.get("worker_task_priority", {}) if isinstance(rolling.get("worker_task_priority", {}), dict) else {}
    if not isinstance(raw, dict) or not raw:
        return {worker_id: list(FACTORY_TASK_CODES) for worker_id in worker_ids}
    allowlists: dict[str, list[str]] = {}
    for worker_id in worker_ids:
        values = raw.get(worker_id, [])
        allowlists[worker_id] = [str(code).strip().upper() for code in values if str(code).strip()] if isinstance(values, list) else []
    return allowlists


def _estimate_worker_complexity_loads(
    *,
    worker_ids: list[str],
    role_allowlists: dict[str, list[str]],
    task_counts: dict[str, int],
    task_complexity: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    loads: dict[str, dict[str, Any]] = {}
    for worker_id in worker_ids:
        allowed = set(role_allowlists.get(worker_id, FACTORY_TASK_CODES))
        by_task: dict[str, float] = {}
        total = 0.0
        for task_code, count in sorted(task_counts.items()):
            if task_code not in allowed:
                continue
            contribution = float(count) * _complexity_value(task_complexity, task_code)
            by_task[task_code] = round(contribution, 3)
            total += contribution
        loads[worker_id] = {
            "allowed_task_codes": sorted(allowed),
            "complexity_load": round(total, 3),
            "by_task": by_task,
        }
    return loads


def _factory_resource_templates(
    *,
    task_counts: dict[str, int],
    machine_ids: list[str],
    station_machine_ids: dict[str, list[str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(task_code: str, count: int, resources: list[str], target: str) -> None:
        if count <= 0:
            return
        rows.append(
            {
                "task_code": task_code,
                "estimated_count": int(count),
                "target": target,
                "resource_keys": sorted(set(resources)),
            }
        )

    station_count = max(1, len(station_machine_ids) or 2)
    per_station_replenish = max(1, int(ceil(task_counts.get("REPLENISH_MATERIAL", 0) / station_count)))
    for station in sorted(station_machine_ids or {"station1": [], "station2": []}):
        add("REPLENISH_MATERIAL", per_station_replenish, [f"station_material_queue:{station}", "warehouse_material_shelf"], station)

    machine_task_codes = ["LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"]
    for task_code in machine_task_codes:
        total = int(task_counts.get(task_code, 0) or 0)
        if total <= 0:
            continue
        divisor = max(1, len(machine_ids))
        per_machine = max(1, int(ceil(total / divisor)))
        for machine_id in machine_ids or ["machine_pool"]:
            add(task_code, per_machine, [f"machine:{machine_id}"], machine_id)

    add("TRANSFER", int(task_counts.get("TRANSFER", 0) or 0), ["station_output_queue", "inspection_input_queue", "wip_item"], "station output -> inspection")
    add("INSPECT_PRODUCT", int(task_counts.get("INSPECT_PRODUCT", 0) or 0), ["inspection_table", "inspection_input_queue"], "inspection")
    add("COLLECT_WASTE_OR_SCRAP", int(task_counts.get("COLLECT_WASTE_OR_SCRAP", 0) or 0), ["inspection_scrap_queue", "scrap_disposal"], "scrap disposal")
    add("MANAGE_ROBOT_POWER", int(task_counts.get("MANAGE_ROBOT_POWER", 0) or 0), ["battery_station", "fresh_battery_rack"], "battery service")
    return rows


def _map_topology_inputs(grid_map: Any) -> dict[str, Any]:
    if grid_map is None:
        return {"passable_tile_count": 0, "bottleneck_tile_count": 0, "bottleneck_ratio": 0.0, "tile_time_min": 0.0}
    width = int(getattr(grid_map, "width_tiles", 0) or 0)
    height = int(getattr(grid_map, "height_tiles", 0) or 0)
    passable: list[tuple[int, int]] = []
    for x in range(width):
        for y in range(height):
            tile = (x, y)
            try:
                if bool(grid_map.is_passable_static(tile)):
                    passable.append(tile)
            except Exception:
                continue
    bottleneck_count = 0
    for tile in passable:
        try:
            degree = len(grid_map.neighbors(tile))
        except Exception:
            degree = 0
        if degree <= 2:
            bottleneck_count += 1
    ratio = float(bottleneck_count) / float(len(passable)) if passable else 0.0
    return {
        "passable_tile_count": len(passable),
        "bottleneck_tile_count": bottleneck_count,
        "bottleneck_ratio": round(ratio, 4),
        "tile_time_min": round(float(getattr(grid_map, "tile_time_min", 0.0) or 0.0), 4),
    }


def _service_tile_inputs(grid_map: Any, *, machine_ids: list[str]) -> dict[str, Any]:
    if grid_map is None:
        return {"targets": {}, "average_service_tiles": 0.0}
    targets = set(machine_ids)
    for name in (
        "Warehouse",
        "Station1",
        "Station2",
        "Inspection",
        "BatteryStation",
        "warehouse_material_shelf",
        "station_1_output_queue",
        "station_2_output_queue",
        "inspection_table",
        "inspection_scrap_queue",
        "scrap_disposal_bin",
    ):
        targets.add(name)
    counts: dict[str, int] = {}
    for target in sorted(targets):
        try:
            tiles = grid_map.destination_tiles(target, worker_id="__diagnostics__", from_tile=None, ignore_dynamic=True)
        except Exception:
            tiles = []
        if tiles:
            counts[target] = len(tiles)
    avg = mean(counts.values()) if counts else 0.0
    return {
        "targets": counts,
        "average_service_tiles": round(float(avg), 3),
    }


def _battery_inputs(cfg: dict[str, Any], world: Any) -> dict[str, Any]:
    worker = cfg.get("worker", {}) if isinstance(cfg.get("worker", {}), dict) else {}
    agent_cfg = cfg.get("agent", {}) if isinstance(cfg.get("agent", {}), dict) else {}
    combined = {**agent_cfg, **worker}
    drain = combined.get("battery_drain", {}) if isinstance(combined.get("battery_drain", {}), dict) else {}
    decision = cfg.get("decision", {}) if isinstance(cfg.get("decision", {}), dict) else {}
    battery_decision = decision.get("battery", {}) if isinstance(decision.get("battery", {}), dict) else {}
    return {
        "battery_swap_period_min": round(float(combined.get("battery_swap_period_min", getattr(world, "battery_swap_period_min", 200.0)) or 200.0), 3),
        "available_rate_multiplier": round(float(drain.get("available_rate_multiplier", getattr(world, "battery_available_rate_multiplier", 1.0)) or 1.0), 3),
        "non_available_rate_multiplier": round(float(drain.get("non_available_rate_multiplier", getattr(world, "battery_non_available_rate_multiplier", 2.0)) or 2.0), 3),
        "low_threshold_ratio": round(float(battery_decision.get("low_threshold_ratio", 0.3) or 0.3), 3),
        "delivery_provider_agent_ids": list(battery_decision.get("delivery_provider_agent_ids", [])) if isinstance(battery_decision.get("delivery_provider_agent_ids", []), list) else [],
        "delivery_receiver_agent_ids": list(battery_decision.get("delivery_receiver_agent_ids", [])) if isinstance(battery_decision.get("delivery_receiver_agent_ids", []), list) else [],
    }


def _metric_worker_otc_imbalance(task_loads_by_worker: dict[str, dict[str, Any]]) -> dict[str, Any]:
    loads = [float(payload.get("complexity_load", 0.0) or 0.0) for payload in task_loads_by_worker.values()]
    avg = mean(loads) if loads else 0.0
    value = (max(loads) / avg) if avg > 0 else 0.0
    return {
        "label": "Worker OTC Imbalance",
        "value": round(value, 3),
        "unit": "max worker load / average worker load",
        "interpretation": "1.0에 가까울수록 worker별 예상 task complexity 부담이 고르게 나뉩니다.",
        "inputs": {"task_complexity_load_by_worker": task_loads_by_worker},
        "calculation": {
            "formula": "max_w L_w / mean_w L_w, L_w=sum_t 1[t allowed for w]*N_t*C_task(t)",
            "worker_loads": {worker_id: payload.get("complexity_load", 0.0) for worker_id, payload in task_loads_by_worker.items()},
            "average_load": round(avg, 3),
            "max_load": round(max(loads), 3) if loads else 0.0,
        },
    }


def _attach_metric_assessment(code: str, metric: dict[str, Any]) -> dict[str, Any]:
    payload = dict(metric)
    payload["assessment"] = _metric_assessment(code, float(payload.get("value", 0.0) or 0.0))
    return payload


def _metric_assessment(code: str, value: float) -> dict[str, Any]:
    """Return rule-of-thumb bands for pre-run indicators.

    These bands are not pass/fail criteria. They provide a quick reading of
    whether a scenario is light, attention-worthy, or coordination-heavy before
    comparing decision modes.
    """
    bands = {
        "worker_otc_imbalance": (
            (1.2, "low", "Good", "worker별 예상 복잡도 부담이 비교적 균등합니다."),
            (1.6, "moderate", "Watch", "특정 worker에 일이 몰릴 수 있어 role split을 확인하는 편이 좋습니다."),
            (float("inf"), "high", "High", "role imbalance가 큽니다. Dedicated role 또는 priority 재조정이 필요할 수 있습니다."),
        ),
        "resource_conflict_potential": (
            (0.2, "low", "Low", "공유자원 경쟁 가능성이 낮습니다."),
            (0.45, "moderate", "Watch", "machine, queue, shelf 같은 공유자원에서 대기/skip이 생길 수 있습니다."),
            (float("inf"), "high", "High", "공유자원 경쟁이 큰 편입니다. reservation, batching, role split을 점검하세요."),
        ),
        "traffic_contention_index": (
            (0.15, "low", "Low", "동선 병목 위험이 낮습니다."),
            (0.35, "moderate", "Watch", "좁은 통로에서 traffic wait가 생길 수 있습니다."),
            (float("inf"), "high", "High", "동선 병목이 큽니다. strict reservation, start 위치, queue 접근 tile을 확인하세요."),
        ),
        "service_tile_scarcity": (
            (0.2, "low", "Good", "target 주변 service tile 여유가 충분한 편입니다."),
            (0.5, "moderate", "Watch", "일부 target 주변 접근 tile이 부족할 수 있습니다."),
            (float("inf"), "high", "High", "worker들이 같은 접근 tile을 기다릴 가능성이 큽니다."),
        ),
        "robot_interaction_load": (
            (0.1, "low", "Low", "로봇 간 동기화 부담이 낮습니다."),
            (0.3, "moderate", "Watch", "배터리 전달/협업/공동 운반이 의사결정에 영향을 줄 수 있습니다."),
            (float("inf"), "high", "High", "로봇 간 상호작용이 운영 부담의 큰 비중을 차지합니다."),
        ),
        "power_coordination_risk": (
            (0.8, "low", "Low", "배터리 교체 없이도 버틸 가능성이 높은 편입니다."),
            (1.2, "moderate", "Watch", "일부 worker가 기간 중 battery service를 요구할 가능성이 있습니다."),
            (float("inf"), "high", "High", "battery delivery/self-swap 정책이 운영 성능을 크게 좌우할 수 있습니다."),
        ),
    }
    for upper, severity, label, message in bands.get(code, ()):
        if value <= upper:
            return {
                "severity": severity,
                "label": label,
                "message": message,
                "bands": _assessment_band_text(code),
            }
    return {
        "severity": "unknown",
        "label": "Unknown",
        "message": "이 지표의 해석 기준이 아직 정의되지 않았습니다.",
        "bands": "",
    }


def _assessment_band_text(code: str) -> str:
    return {
        "worker_otc_imbalance": "Good <= 1.2, Watch <= 1.6, High > 1.6",
        "resource_conflict_potential": "Low <= 0.20, Watch <= 0.45, High > 0.45",
        "traffic_contention_index": "Low <= 0.15, Watch <= 0.35, High > 0.35",
        "service_tile_scarcity": "Good <= 0.20, Watch <= 0.50, High > 0.50",
        "robot_interaction_load": "Low <= 0.10, Watch <= 0.30, High > 0.30",
        "power_coordination_risk": "Low <= 0.80, Watch <= 1.20, High > 1.20",
    }.get(code, "")


def _metric_resource_conflict_potential(resource_templates: list[dict[str, Any]]) -> dict[str, Any]:
    weighted_pairs = 0.0
    conflict_pairs = 0.0
    conflict_resource_counter: Counter[str] = Counter()
    for idx, left in enumerate(resource_templates):
        left_count = int(left.get("estimated_count", 0) or 0)
        left_resources = set(left.get("resource_keys", []))
        if left_count > 1 and left_resources:
            self_pairs = left_count * (left_count - 1) / 2.0
            weighted_pairs += self_pairs
            conflict_pairs += self_pairs
            for resource in left_resources:
                conflict_resource_counter[str(resource)] += int(self_pairs)
        for right in resource_templates[idx + 1 :]:
            right_count = int(right.get("estimated_count", 0) or 0)
            pair_weight = float(left_count * right_count)
            if pair_weight <= 0:
                continue
            weighted_pairs += pair_weight
            shared = left_resources & set(right.get("resource_keys", []))
            if shared:
                conflict_pairs += pair_weight
                for resource in shared:
                    conflict_resource_counter[str(resource)] += int(pair_weight)
    value = conflict_pairs / weighted_pairs if weighted_pairs > 0 else 0.0
    return {
        "label": "Resource Conflict Potential",
        "value": round(value, 3),
        "unit": "shared-resource weighted pair ratio",
        "interpretation": "값이 클수록 machine, queue, shelf, battery rack 같은 공유자원을 두고 task 간 경쟁 가능성이 큽니다.",
        "inputs": {"resource_templates": resource_templates},
        "calculation": {
            "formula": "sum conflicting weighted task pairs / sum all weighted task pairs",
            "weighted_task_pairs": round(weighted_pairs, 3),
            "conflicting_weighted_task_pairs": round(conflict_pairs, 3),
            "top_conflict_resources": dict(conflict_resource_counter.most_common(10)),
        },
    }


def _metric_traffic_contention_index(topology: dict[str, Any], *, worker_count: int) -> dict[str, Any]:
    bottleneck_ratio = float(topology.get("bottleneck_ratio", 0.0) or 0.0)
    value = min(1.0, bottleneck_ratio * max(1.0, float(worker_count) / 3.0))
    return {
        "label": "Traffic Contention Index",
        "value": round(value, 3),
        "unit": "topology bottleneck ratio adjusted by worker count",
        "interpretation": "좁은 통로와 낮은 연결도 tile이 많을수록 이동 예약 실패나 near miss 가능성이 커집니다.",
        "inputs": {"map_topology": topology, "worker_count": worker_count},
        "calculation": {
            "formula": "min(1, bottleneck_tile_ratio * max(1, worker_count/3))",
            "bottleneck_tile_ratio": bottleneck_ratio,
            "worker_factor": round(max(1.0, float(worker_count) / 3.0), 3),
        },
    }


def _metric_service_tile_scarcity(service_targets: dict[str, Any]) -> dict[str, Any]:
    targets = service_targets.get("targets", {}) if isinstance(service_targets.get("targets", {}), dict) else {}
    penalties = {target: round(1.0 / max(1, int(count or 0)), 3) for target, count in targets.items()}
    value = mean(penalties.values()) if penalties else 0.0
    tightest = dict(sorted(penalties.items(), key=lambda item: (-item[1], item[0]))[:10])
    return {
        "label": "Service Tile Scarcity",
        "value": round(value, 3),
        "unit": "mean 1/service_tiles(target)",
        "interpretation": "값이 클수록 machine, queue, zone 주변에서 여러 worker가 같은 접근 tile을 기다릴 가능성이 큽니다.",
        "inputs": {"service_tile_counts": targets},
        "calculation": {
            "formula": "mean_r 1/max(1, service_tile_count_r)",
            "per_target_penalty": penalties,
            "tightest_targets": tightest,
        },
    }


def _metric_robot_interaction_load(
    *,
    task_counts: dict[str, int],
    task_complexity: dict[str, dict[str, Any]],
    total_complexity: float,
    decision_cfg: dict[str, Any],
) -> dict[str, Any]:
    battery_cfg = decision_cfg.get("battery", {}) if isinstance(decision_cfg.get("battery", {}), dict) else {}
    providers = battery_cfg.get("delivery_provider_agent_ids", []) if isinstance(battery_cfg.get("delivery_provider_agent_ids", []), list) else []
    receivers = battery_cfg.get("delivery_receiver_agent_ids", []) if isinstance(battery_cfg.get("delivery_receiver_agent_ids", []), list) else []
    interaction_task_codes = ["HANDOVER_ITEM"]
    if providers and receivers:
        interaction_task_codes.append("MANAGE_ROBOT_POWER")
    interaction_complexity = 0.0
    by_task: dict[str, float] = {}
    for task_code in interaction_task_codes:
        contribution = float(task_counts.get(task_code, 0) or 0) * _complexity_value(task_complexity, task_code)
        if contribution > 0:
            by_task[task_code] = round(contribution, 3)
            interaction_complexity += contribution
    value = interaction_complexity / total_complexity if total_complexity > 0 else 0.0
    return {
        "label": "Robot Interaction Load",
        "value": round(value, 3),
        "unit": "interaction complexity / total complexity",
        "interpretation": "값이 클수록 배터리 전달, handover, 공동 운반처럼 로봇 간 동기화가 운영 부담에서 차지하는 비중이 큽니다.",
        "inputs": {
            "interaction_task_codes": interaction_task_codes,
            "battery_delivery_provider_agent_ids": providers,
            "battery_delivery_receiver_agent_ids": receivers,
        },
        "calculation": {
            "formula": "sum interaction task complexity / sum all task complexity",
            "interaction_complexity_by_task": by_task,
            "interaction_complexity": round(interaction_complexity, 3),
            "total_complexity": round(total_complexity, 3),
        },
    }


def _metric_power_coordination_risk(
    *,
    task_loads_by_worker: dict[str, dict[str, Any]],
    horizon: dict[str, float],
    battery_inputs: dict[str, Any],
) -> dict[str, Any]:
    loads = {worker_id: float(payload.get("complexity_load", 0.0) or 0.0) for worker_id, payload in task_loads_by_worker.items()}
    total_load = sum(loads.values())
    total_minutes = float(horizon.get("total_minutes", 0.0) or 0.0)
    battery_period = max(1e-9, float(battery_inputs.get("battery_swap_period_min", 200.0) or 200.0))
    available_rate = float(battery_inputs.get("available_rate_multiplier", 1.0) or 1.0)
    active_rate = float(battery_inputs.get("non_available_rate_multiplier", 2.0) or 2.0)
    per_worker: dict[str, dict[str, float]] = {}
    for worker_id, load in loads.items():
        active_share = load / total_load if total_load > 0 else 0.0
        active_minutes_proxy = active_share * total_minutes
        idle_minutes_proxy = max(0.0, total_minutes - active_minutes_proxy)
        normalized_energy_cycles = (active_minutes_proxy * active_rate + idle_minutes_proxy * available_rate) / battery_period
        per_worker[worker_id] = {
            "complexity_load": round(load, 3),
            "active_minutes_proxy": round(active_minutes_proxy, 3),
            "idle_minutes_proxy": round(idle_minutes_proxy, 3),
            "estimated_battery_cycles_needed": round(normalized_energy_cycles, 3),
        }
    value = max((payload["estimated_battery_cycles_needed"] for payload in per_worker.values()), default=0.0)
    return {
        "label": "Power Coordination Risk",
        "value": round(value, 3),
        "unit": "max estimated battery cycles needed per worker",
        "interpretation": "값이 1보다 크면 주어진 기간 중 적어도 한 worker가 battery service를 필요로 할 가능성이 높습니다.",
        "inputs": {"battery": battery_inputs, "horizon": horizon, "task_complexity_load_by_worker": loads},
        "calculation": {
            "formula": "max_w ((active_minutes_w*r_active + idle_minutes_w*r_idle) / battery_swap_period)",
            "per_worker": per_worker,
        },
    }
