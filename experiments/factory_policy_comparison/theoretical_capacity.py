from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Iterable

import yaml

from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import TileGridMap

from experiments.factory_policy_comparison.common import REPO_ROOT


CAPACITY_REPORT_JSON = "theoretical_capacity.json"
SUPPORTED_SCENARIO = "mfg_flow_shop"


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a mapping in {path}.")
    return payload


def _distribution_value(distribution: dict[str, Any], basis: str) -> float:
    minimum = float(distribution["min"])
    if basis == "minimum":
        return minimum
    if basis == "expected":
        return (
            minimum
            + float(distribution.get("mode", minimum))
            + float(distribution.get("max", minimum))
        ) / 3.0
    raise ValueError(f"Unsupported timing basis: {basis}")


def _task_service(timing_cfg: dict[str, Any], task_code: str, *, basis: str) -> float:
    task_cfg = timing_cfg.get("tasks", {}).get(task_code, {})
    steps = task_cfg.get("steps", {}) if isinstance(task_cfg, dict) else {}
    if not isinstance(steps, dict) or not steps:
        raise ValueError(f"Missing timing steps for {task_code}.")
    total = 0.0
    for step in steps.values():
        if not isinstance(step, dict) or str(step.get("timing_model", "duration")).lower() == "movement":
            continue
        distribution = step.get("distribution", {})
        if not isinstance(distribution, dict) or "min" not in distribution:
            raise ValueError(f"Missing duration distribution in {task_code} timing.")
        total += _distribution_value(distribution, basis)
    return total


def _call_duration(
    timing_cfg: dict[str, Any],
    task_code: str,
    call_code: str,
    *,
    basis: str,
) -> float:
    task_cfg = timing_cfg.get("tasks", {}).get(task_code, {})
    steps = task_cfg.get("steps", {}) if isinstance(task_cfg, dict) else {}
    values = []
    for step in steps.values() if isinstance(steps, dict) else []:
        if not isinstance(step, dict) or str(step.get("call_code", "")).upper() != call_code.upper():
            continue
        distribution = step.get("distribution", {})
        if isinstance(distribution, dict) and "min" in distribution:
            values.append(_distribution_value(distribution, basis))
    if not values:
        raise ValueError(f"Missing {task_code}:{call_code} duration.")
    return sum(values)


def _maximum_product_count(horizon_min: float, first_product_min: float, cycle_min: float) -> int:
    if horizon_min + 1e-9 < first_product_min:
        return 0
    return max(0, 1 + int(math.floor((horizon_min - first_product_min + 1e-9) / cycle_min)))


def _maximum_active_minutes(
    horizon_min: float,
    *,
    usable_active_min: float,
    charge_cycle_min: float,
) -> float:
    """Optimistic productive time while retaining mandatory direct-charge downtime."""
    if usable_active_min <= 0.0 or charge_cycle_min <= 0.0:
        return max(0.0, horizon_min)
    best = min(horizon_min, usable_active_min)
    max_charges = int(math.ceil(horizon_min / charge_cycle_min)) + 1
    for charge_count in range(1, max_charges + 1):
        remaining = horizon_min - charge_count * charge_cycle_min
        if remaining <= 0.0:
            break
        best = max(best, min((charge_count + 1) * usable_active_min, remaining))
    return max(0.0, best)


def calculate_theoretical_capacity(
    *,
    scenario: str,
    worker_counts: Iterable[int],
    horizon_days: int,
    minutes_per_day: float,
    resolved_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build ideal and realistic scenario-level production references.

    The ideal capacity reference uses triangular minima and relaxes disruption.
    Its steady-state cycle and charging assumptions do not certify a bound.
    The realistic reference uses triangular expectations and configured
    reliability, charging, quality, and first-order incident burden. It remains
    policy-independent; queueing, dispatch delay, and dynamic traffic are left
    to the observed experiment results.
    """
    scenario_key = str(scenario).strip().lower()
    if scenario_key != SUPPORTED_SCENARIO:
        return {
            "schema_version": 2,
            "scenario": scenario_key,
            "available": False,
            "reason": f"theoretical capacity is not defined for scenario={scenario_key}",
        }

    scenario_path = REPO_ROOT / "configs" / "scenario" / f"{scenario_key}.yaml"
    timing_path = REPO_ROOT / "configs" / "task_primitive_timing" / f"{scenario_key}.yaml"
    scenario_cfg = copy.deepcopy(resolved_config["scenario"]) if resolved_config is not None else _load_yaml(scenario_path)
    timing_cfg = copy.deepcopy(resolved_config["task_primitive_timing"]) if resolved_config is not None else _load_yaml(timing_path)
    movement_cfg = timing_cfg.get("movement", {})
    per_tile_cfg = movement_cfg.get("per_tile_min", {}) if isinstance(movement_cfg, dict) else {}
    multipliers = {
        str(key).lower(): float(value)
        for key, value in (movement_cfg.get("multipliers", {}) or {}).items()
    }
    factory_cfg = scenario_cfg.get("factory", {})
    process_cfg = factory_cfg.get("processing_time", {})
    machines_per_station = max(1, int(factory_cfg.get("machines_per_station", 1) or 1))
    machine_ids_by_station = {
        station: [f"S{station}M{index}" for index in range(1, machines_per_station + 1)]
        for station in (1, 2)
    }
    buffers_cfg = factory_cfg.get("buffers", {}) if isinstance(factory_cfg, dict) else {}
    station1_buffers = buffers_cfg.get("station1", {}) if isinstance(buffers_cfg, dict) else {}
    station2_buffers = buffers_cfg.get("station2", {}) if isinstance(buffers_cfg, dict) else {}
    inspection_buffers = buffers_cfg.get("inspection", {}) if isinstance(buffers_cfg, dict) else {}
    finite_buffer_capacities = {
        "s1_material_input": int(station1_buffers.get("material_input_capacity", 0) or 0),
        "s1_output": int(station1_buffers.get("output_capacity", 0) or 0),
        "s2_material_input": int(station2_buffers.get("material_input_capacity", 0) or 0),
        "s2_intermediate_input": int(station2_buffers.get("intermediate_input_capacity", 0) or 0),
        "s2_output": int(station2_buffers.get("output_capacity", 0) or 0),
        "inspection_input": int(inspection_buffers.get("input_capacity", 0) or 0),
        "inspection_pass": int(inspection_buffers.get("pass_output_capacity", 0) or 0),
        "inspection_scrap": int(inspection_buffers.get("scrap_output_capacity", 0) or 0),
    }
    if any(capacity <= 0 for capacity in finite_buffer_capacities.values()):
        raise ValueError(
            "mfg_flow_shop theoretical capacity requires positive finite-buffer capacities: "
            f"{finite_buffer_capacities}"
        )
    task_codes = (
        "REPLENISH_MATERIAL",
        "TRANSFER",
        "LOAD_MACHINE",
        "SETUP_MACHINE",
        "UNLOAD_MACHINE",
        "LOAD_UNLOAD_TRANSFER_INTERFACE",
        "INSPECT_PRODUCT",
        "MANAGE_ROBOT_POWER",
        "COLLECT_WASTE_OR_SCRAP",
    )

    objective_cfg = scenario_cfg.get("objective", {})
    throughput_cfg = objective_cfg.get("throughput", {}) if isinstance(objective_cfg, dict) else {}
    interval_days = max(1, int(throughput_cfg.get("restock_interval_days", 1) or 1))
    initial_materials = int(
        scenario_cfg.get("warehouse", {}).get("material_shelf", {}).get("initial_fill", 0) or 0
    )
    restock_target = int(throughput_cfg.get("restock_target_fill", 0) or 0)
    restock_events = max(0, (int(horizon_days) - 1) // interval_days)
    material_bound = (initial_materials + restock_events * restock_target) // 2
    quality_yield = max(
        0.0,
        min(1.0, 1.0 - float(scenario_cfg.get("quality", {}).get("defect_prob", 0.0) or 0.0)),
    )
    failure_cfg = scenario_cfg.get("machine_failure", {})
    mean_ttf = max(
        1e-9,
        float(
            failure_cfg.get(
                "mean_processing_time_to_failure_min",
                failure_cfg.get("mean_time_to_fail_min", 300.0),
            )
            or 300.0
        ),
    )
    failure_time_basis = str(
        failure_cfg.get("time_basis", "calendar_time") or "calendar_time"
    ).strip().lower()
    pm_cfg = (
        failure_cfg.get("preventive_maintenance", {})
        if isinstance(failure_cfg.get("preventive_maintenance", {}), dict)
        else {}
    )
    pm_enabled = bool(pm_cfg.get("enabled", False))
    pm_due_processing_min = max(
        1e-9, float(pm_cfg.get("due_processing_min", math.inf) or math.inf)
    )
    pm_protected_processing_min = max(
        0.0, float(pm_cfg.get("protected_processing_min", 0.0) or 0.0)
    )
    pm_hazard_multiplier = min(
        1.0, max(0.0, float(pm_cfg.get("hazard_multiplier", 1.0) or 1.0))
    )
    humanoid_cfg_path = REPO_ROOT / "configs" / "humanoidsim" / "default.yaml"
    humanoid_cfg = copy.deepcopy(resolved_config["humanoidsim"]) if resolved_config is not None else _load_yaml(humanoid_cfg_path)
    recovery_cfg = humanoid_cfg.get("recovery_protocol", {})
    recovery_step_min = float(
        recovery_cfg.get("default_step_min", 0.0) if isinstance(recovery_cfg, dict) else 0.0
    )

    def timing_profile(worker_count: int, basis: str) -> dict[str, Any]:
        tile_time = _distribution_value(per_tile_cfg, basis)
        process_time = {
            1: _distribution_value(process_cfg["station1"], basis),
            2: _distribution_value(process_cfg["station2"], basis),
        }
        services = {
            code: _task_service(timing_cfg, code, basis=basis)
            for code in task_codes
        }
        map_cfg = copy.deepcopy(scenario_cfg)
        map_cfg.setdefault("factory", {})["num_workers"] = worker_count
        map_cfg.setdefault("map", {})["tile_time_min"] = tile_time
        grid = TileGridMap.from_world_config(
            map_cfg,
            stations=(1, 2),
            machines_per_station=machines_per_station,
        )

        def route(source: str, destination: str, item_type: str | None = None) -> tuple[float, float]:
            base_time = float(grid.travel_time(source, destination))
            multiplier = multipliers.get(str(item_type).lower(), 1.0) if item_type else 1.0
            edges = base_time / max(1e-9, tile_time)
            return base_time * multiplier, edges

        def machine_route(
            source: str,
            station: int,
            item_type: str,
            *,
            direction: str,
            aggregate: str,
        ) -> tuple[float, float]:
            values = [
                route(source, machine_id, item_type)
                if direction == "to_machine"
                else route(machine_id, source, item_type)
                for machine_id in machine_ids_by_station[station]
            ]
            if aggregate == "minimum":
                return min(values, key=lambda value: value[0])
            if aggregate == "mean":
                return (
                    sum(value[0] for value in values) / len(values),
                    sum(value[1] for value in values) / len(values),
                )
            raise ValueError(f"Unsupported machine route aggregate: {aggregate}")

        route_values = {
            "warehouse_s1": route("Warehouse", "material_queue_1", "material"),
            "warehouse_s2": route("Warehouse", "material_queue_2", "material"),
            "s1_load": machine_route("material_queue_1", 1, "material", direction="to_machine", aggregate="mean"),
            "s2_material_load": machine_route("material_queue_2", 2, "material", direction="to_machine", aggregate="mean"),
            "s2_intermediate_load": machine_route("intermediate_queue_2", 2, "intermediate", direction="to_machine", aggregate="mean"),
            "s1_unload": machine_route("output_buffer_station_1", 1, "intermediate", direction="from_machine", aggregate="mean"),
            "s1_s2": route("output_buffer_station_1", "intermediate_queue_2", "intermediate"),
            "s2_unload": machine_route("output_buffer_station_2", 2, "product", direction="from_machine", aggregate="mean"),
            "s2_inspection": route("output_buffer_station_2", "intermediate_queue_4", "product"),
            "inspection_load": route("intermediate_queue_4", "inspection_desk", "product"),
            "inspection_unload": route("inspection_desk", "inspection_output_queue", "product"),
            "completed": route("output_buffer_station_4", "warehouse_buffer", "product"),
            "scrap": route("inspection_scrap_queue", "ScrapDisposal", "product"),
        }
        first_machine_routes = {
            "s1_load": machine_route("material_queue_1", 1, "material", direction="to_machine", aggregate="minimum"),
            "s1_unload": machine_route("output_buffer_station_1", 1, "intermediate", direction="from_machine", aggregate="minimum"),
            "s2_material_load": machine_route("material_queue_2", 2, "material", direction="to_machine", aggregate="minimum"),
            "s2_intermediate_load": machine_route("intermediate_queue_2", 2, "intermediate", direction="to_machine", aggregate="minimum"),
            "s2_unload": machine_route("output_buffer_station_2", 2, "product", direction="from_machine", aggregate="minimum"),
        }

        d = {
            "replenish_s1": services["REPLENISH_MATERIAL"] + route_values["warehouse_s1"][0],
            "replenish_s2": services["REPLENISH_MATERIAL"] + route_values["warehouse_s2"][0],
            "load_s1": services["LOAD_MACHINE"] + route_values["s1_load"][0],
            "load_s2_material": services["LOAD_MACHINE"] + route_values["s2_material_load"][0],
            "load_s2_intermediate": services["LOAD_MACHINE"] + route_values["s2_intermediate_load"][0],
            "setup_s1": services["SETUP_MACHINE"],
            "setup_s2": services["SETUP_MACHINE"],
            "unload_s1_service": services["UNLOAD_MACHINE"],
            "unload_s1_move": route_values["s1_unload"][0],
            "unload_s2_service": services["UNLOAD_MACHINE"],
            "unload_s2_move": route_values["s2_unload"][0],
            "transfer_s1_s2": services["TRANSFER"] + route_values["s1_s2"][0],
            "transfer_s2_inspection": services["TRANSFER"] + route_values["s2_inspection"][0],
            "inspection_load": services["LOAD_UNLOAD_TRANSFER_INTERFACE"] + route_values["inspection_load"][0],
            "inspection": services["INSPECT_PRODUCT"],
            "inspection_unload": services["LOAD_UNLOAD_TRANSFER_INTERFACE"] + route_values["inspection_unload"][0],
            "transfer_completed": services["TRANSFER"] + route_values["completed"][0],
            "scrap_disposal": services["COLLECT_WASTE_OR_SCRAP"] + route_values["scrap"][0],
        }

        dock_access = min(
            route(f"charging_dock_A{index}", "Warehouse")[0]
            for index in range(1, worker_count + 1)
        )
        first_s1_to_s2_input = (
            dock_access
            + d["replenish_s1"]
            + services["LOAD_MACHINE"]
            + first_machine_routes["s1_load"][0]
            + d["setup_s1"]
            + process_time[1]
            + d["unload_s1_service"]
            + first_machine_routes["s1_unload"][0]
            + d["transfer_s1_s2"]
        )
        first_s2_material_ready = (
            dock_access
            + d["replenish_s2"]
            + services["LOAD_MACHINE"]
            + first_machine_routes["s2_material_load"][0]
        )
        first_product = (
            max(first_s1_to_s2_input, first_s2_material_ready)
            + services["LOAD_MACHINE"]
            + first_machine_routes["s2_intermediate_load"][0]
            + d["setup_s2"]
            + process_time[2]
            + d["unload_s2_service"]
            + first_machine_routes["s2_unload"][0]
            + d["transfer_s2_inspection"]
            + d["inspection_load"]
            + d["inspection"]
            + d["inspection_unload"]
            + d["transfer_completed"]
        )
        station1_cycle = d["load_s1"] + d["setup_s1"] + process_time[1] + d["unload_s1_service"]
        station2_cycle = (
            d["load_s2_material"]
            + d["load_s2_intermediate"]
            + d["setup_s2"]
            + process_time[2]
            + d["unload_s2_service"]
        )
        inspection_cycle = d["inspection_load"] + d["inspection"] + d["inspection_unload"]

        worker_busy_pass = sum(value for key, value in d.items() if key != "scrap_disposal")
        worker_busy_attempt = (
            worker_busy_pass
            - d["transfer_completed"]
            + quality_yield * d["transfer_completed"]
            + (1.0 - quality_yield) * d["scrap_disposal"]
        )

        battery_cfg = scenario_cfg.get("worker", {})
        battery_capacity = float(battery_cfg.get("battery_capacity_min", 0.0) or 0.0)
        service_cfg = battery_cfg.get("battery_service", {}) if isinstance(battery_cfg, dict) else {}
        low_ratio = float(service_cfg.get("low_threshold_ratio", 0.0) or 0.0)
        usable_active_min = battery_capacity * max(0.0, 1.0 - low_ratio)
        charge_action = _call_duration(
            timing_cfg,
            "MANAGE_ROBOT_POWER",
            "EXECUTE_SYSTEM_ACTION",
            basis=basis,
        )
        charge_action_min = charge_action * max(0.0, 1.0 - low_ratio)
        charge_fixed_min = services["MANAGE_ROBOT_POWER"] - charge_action
        production_locations = (
            "Warehouse",
            "material_queue_1",
            "material_queue_2",
            "S1M1",
            "S2M1",
            "inspection_desk",
            "inspection_output_queue",
        )
        charge_round_trip_min = min(
            route(location, f"charging_dock_A{index}")[0]
            + route(f"charging_dock_A{index}", location)[0]
            for index in range(1, worker_count + 1)
            for location in production_locations
        )
        charge_cycle = charge_action_min + charge_fixed_min + charge_round_trip_min
        duty_fraction = (
            usable_active_min / (usable_active_min + charge_cycle)
            if usable_active_min > 0.0
            else 1.0
        )

        task_occurrences = {
            "REPLENISH_MATERIAL": 2.0,
            "TRANSFER": 2.0 + quality_yield,
            "LOAD_MACHINE": 3.0,
            "SETUP_MACHINE": 2.0,
            "UNLOAD_MACHINE": 2.0,
            "LOAD_UNLOAD_TRANSFER_INTERFACE": 2.0,
            "INSPECT_PRODUCT": 1.0,
            "COLLECT_WASTE_OR_SCRAP": 1.0 - quality_yield,
        }
        random_incidents = scenario_cfg.get("humanoid_incidents", {}).get("random", {})
        expected_incident_count = 0.0
        for incident_code, raw_cfg in random_incidents.items() if isinstance(random_incidents, dict) else []:
            incident_cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
            if not bool(incident_cfg.get("enabled", False)):
                continue
            if str(incident_code).upper() == "ITEM_DROPPED":
                probability = float(incident_cfg.get("probability_per_tile", 0.0) or 0.0)
                common_edges = sum(
                    value[1]
                    for name, value in route_values.items()
                    if name not in {"completed", "scrap"}
                )
                terminal_edges = (
                    quality_yield * route_values["completed"][1]
                    + (1.0 - quality_yield) * route_values["scrap"][1]
                )
                expected_incident_count += probability * (common_edges + terminal_edges)
                continue
            probability = float(incident_cfg.get("probability", 0.0) or 0.0)
            triggers = {
                str(value).strip().upper()
                for value in incident_cfg.get("trigger_primitives", [])
            }
            for task_code, occurrence_count in task_occurrences.items():
                steps = timing_cfg.get("tasks", {}).get(task_code, {}).get("steps", {})
                matching = sum(
                    1
                    for step in steps.values() if isinstance(steps, dict) and isinstance(step, dict)
                    if "*" in triggers or str(step.get("call_code", "")).upper() in triggers
                )
                expected_incident_count += probability * occurrence_count * matching
        incident_overhead = expected_incident_count * max(0.0, recovery_step_min)
        downstream_closure = (
            d["transfer_s2_inspection"]
            + d["inspection_load"]
            + d["inspection"]
            + d["inspection_unload"]
            + quality_yield * d["transfer_completed"]
            + (1.0 - quality_yield) * d["scrap_disposal"]
        )

        return {
            "process_time": process_time,
            "first_product_min": first_product,
            "station1_cycle_min": station1_cycle,
            "station2_cycle_min": station2_cycle,
            "station1_parallel_cycle_min": station1_cycle / machines_per_station,
            "station2_parallel_cycle_min": station2_cycle / machines_per_station,
            "inspection_cycle_min": inspection_cycle,
            "worker_busy_pass_min": worker_busy_pass,
            "worker_busy_attempt_min": worker_busy_attempt,
            "usable_active_min": usable_active_min,
            "charge_cycle_min": charge_cycle,
            "battery_duty_fraction": duty_fraction,
            "expected_incident_count_per_attempt": expected_incident_count,
            "expected_incident_overhead_min_per_attempt": incident_overhead,
            "downstream_closure_min": downstream_closure,
        }

    worker_values = sorted({max(1, int(value)) for value in worker_counts})
    rows: list[dict[str, Any]] = []
    component_template: dict[str, float] | None = None
    expected_component_template: dict[str, float] | None = None
    horizon_min = float(horizon_days) * float(minutes_per_day)
    for worker_count in worker_values:
        minimum = timing_profile(worker_count, "minimum")
        expected = timing_profile(worker_count, "expected")

        worker_cycle = minimum["worker_busy_pass_min"] / max(
            1e-9, worker_count * minimum["battery_duty_fraction"]
        )
        bottleneck_cycle = max(
            minimum["station1_parallel_cycle_min"],
            minimum["station2_parallel_cycle_min"],
            minimum["inspection_cycle_min"],
            worker_cycle,
        )
        time_bound = _maximum_product_count(
            horizon_min,
            minimum["first_product_min"],
            bottleneck_cycle,
        )

        productive_minutes = _maximum_active_minutes(
            horizon_min,
            usable_active_min=minimum["usable_active_min"],
            charge_cycle_min=minimum["charge_cycle_min"],
        )
        worker_capacity_bound = int(
            math.floor(
                (productive_minutes * worker_count + 1e-9)
                / minimum["worker_busy_pass_min"]
            )
        )
        theoretical_max = min(time_bound, material_bound, worker_capacity_bound)
        initial_batch_products = initial_materials // 2
        theoretical_makespan = (
            minimum["first_product_min"]
            + max(0, initial_batch_products - 1) * bottleneck_cycle
            if initial_batch_products > 0
            else 0.0
        )

        expected_repair_work = _call_duration(
            timing_cfg,
            "REPAIR_MACHINE",
            "EXECUTE_MAINTENANCE_ACTION",
            basis="expected",
        )
        expected_pm_work = (
            _task_service(timing_cfg, "PREVENTIVE_MAINTENANCE", basis="expected")
            if pm_enabled
            else 0.0
        )
        protected_fraction = (
            min(1.0, pm_protected_processing_min / pm_due_processing_min)
            if pm_enabled and math.isfinite(pm_due_processing_min)
            else 0.0
        )
        effective_hazard_multiplier = max(
            1e-9,
            1.0 - protected_fraction * (1.0 - pm_hazard_multiplier),
        )
        effective_processing_mttf = (
            mean_ttf / effective_hazard_multiplier
            if failure_time_basis == "active_processing"
            else mean_ttf
        )
        repair_downtime_per_processing_min = expected_repair_work / effective_processing_mttf
        pm_downtime_per_processing_min = (
            expected_pm_work / pm_due_processing_min
            if pm_enabled and math.isfinite(pm_due_processing_min)
            else 0.0
        )
        machine_capacity_factor = 1.0 / max(
            1e-9,
            1.0
            + repair_downtime_per_processing_min
            + pm_downtime_per_processing_min,
        )
        expected_station1_cycle = expected["station1_parallel_cycle_min"] / machine_capacity_factor
        expected_station2_cycle = expected["station2_parallel_cycle_min"] / machine_capacity_factor
        total_machine_count = 2 * machines_per_station
        expected_repair_worker_rate = total_machine_count * expected_repair_work / effective_processing_mttf
        expected_pm_worker_rate = (
            total_machine_count
            * expected_pm_work
            / pm_due_processing_min
            if pm_enabled and math.isfinite(pm_due_processing_min)
            else 0.0
        )
        expected_worker_capacity_rate = max(
            1e-9,
            worker_count * expected["battery_duty_fraction"]
            - expected_repair_worker_rate
            - expected_pm_worker_rate,
        )
        expected_worker_cycle = (
            expected["worker_busy_attempt_min"]
            + expected["expected_incident_overhead_min_per_attempt"]
        ) / expected_worker_capacity_rate
        expected_pipeline_cycle = max(
            expected_station1_cycle,
            expected_station2_cycle,
            expected["inspection_cycle_min"],
            expected_worker_cycle,
        )
        # The planning reference intentionally does not assume perfect overlap
        # between the bottleneck machine release and downstream transport,
        # inspection, and terminal delivery. This produces a conservative,
        # policy-independent operational cadence rather than another upper
        # bound. Executed policies may outperform it by coordinating overlap.
        expected_operational_cycle = (
            max(expected_station1_cycle, expected_station2_cycle, expected_worker_cycle)
            + expected["downstream_closure_min"]
        )
        expected_first_product = expected["first_product_min"] + sum(
            process_time * (1.0 / max(1e-9, machine_capacity_factor) - 1.0)
            for process_time in expected["process_time"].values()
        )
        expected_pipeline_attempts = (
            0.0
            if horizon_min < expected_first_product
            else 1.0 + max(0.0, horizon_min - expected_first_product) / expected_pipeline_cycle
        )
        expected_flow_attempts = (
            0.0
            if horizon_min < expected_first_product
            else 1.0 + max(0.0, horizon_min - expected_first_product) / expected_operational_cycle
        )
        expected_attempts = min(float(material_bound), expected_flow_attempts)
        realistic_expected_products = expected_attempts * quality_yield

        components = {
            "first_product_min": round(minimum["first_product_min"], 6),
            "station1_cycle_min": round(minimum["station1_cycle_min"], 6),
            "station2_cycle_min": round(minimum["station2_cycle_min"], 6),
            "station1_parallel_cycle_min": round(minimum["station1_parallel_cycle_min"], 6),
            "station2_parallel_cycle_min": round(minimum["station2_parallel_cycle_min"], 6),
            "inspection_cycle_min": round(minimum["inspection_cycle_min"], 6),
            "worker_busy_min_per_product": round(minimum["worker_busy_pass_min"], 6),
            "worker_cycle_min": round(worker_cycle, 6),
            "battery_charge_cycle_min": round(minimum["charge_cycle_min"], 6),
            "bottleneck_cycle_min": round(bottleneck_cycle, 6),
        }
        component_template = component_template or components
        expected_components = {
            "expected_first_product_min": round(expected_first_product, 6),
            "expected_station1_cycle_min": round(expected_station1_cycle, 6),
            "expected_station2_cycle_min": round(expected_station2_cycle, 6),
            "expected_inspection_cycle_min": round(expected["inspection_cycle_min"], 6),
            "expected_worker_cycle_min": round(expected_worker_cycle, 6),
            "expected_pipeline_cycle_min": round(expected_pipeline_cycle, 6),
            "expected_downstream_closure_min": round(expected["downstream_closure_min"], 6),
            "expected_operational_cycle_min": round(expected_operational_cycle, 6),
            "machine_availability": round(machine_capacity_factor, 6),
            "failure_time_basis": failure_time_basis,
            "configured_processing_mttf_min": round(mean_ttf, 6),
            "effective_processing_mttf_min": round(effective_processing_mttf, 6),
            "pm_enabled": pm_enabled,
            "pm_due_processing_min": round(pm_due_processing_min, 6),
            "pm_protected_processing_min": round(pm_protected_processing_min, 6),
            "pm_hazard_multiplier": round(pm_hazard_multiplier, 6),
            "pm_downtime_per_processing_min": round(pm_downtime_per_processing_min, 6),
            "battery_duty_fraction": round(expected["battery_duty_fraction"], 6),
            "quality_yield": round(quality_yield, 6),
            "expected_incident_count_per_attempt": round(
                expected["expected_incident_count_per_attempt"], 6
            ),
            "expected_incident_overhead_min_per_attempt": round(
                expected["expected_incident_overhead_min_per_attempt"], 6
            ),
        }
        expected_component_template = expected_component_template or expected_components
        rows.append(
            {
                "worker_count": worker_count,
                "machines_per_station": machines_per_station,
                "finite_buffer_capacities": dict(finite_buffer_capacities),
                "horizon_min": round(horizon_min, 6),
                "time_capacity_bound": time_bound,
                "material_capacity_bound": material_bound,
                "worker_capacity_bound": worker_capacity_bound,
                "theoretical_max_products": theoretical_max,
                "theoretical_max_throughput_per_sim_hour": round(
                    theoretical_max / max(1e-9, horizon_min / 60.0), 6
                ),
                "realistic_expected_products": round(realistic_expected_products, 6),
                "realistic_expected_throughput_per_sim_hour": round(
                    realistic_expected_products / max(1e-9, horizon_min / 60.0), 6
                ),
                "expected_flow_attempts_before_quality": round(expected_attempts, 6),
                "expected_pipeline_products_before_quality": round(
                    min(float(material_bound), expected_pipeline_attempts), 6
                ),
                "expected_pipeline_good_products": round(
                    min(float(material_bound), expected_pipeline_attempts) * quality_yield, 6
                ),
                "initial_batch_product_count": initial_batch_products,
                "theoretical_min_makespan_min": round(theoretical_makespan, 6),
                **components,
                **expected_components,
            }
        )

    return {
        "schema_version": 4,
        "scenario": scenario_key,
        "available": True,
        "certified_bound": False,
        "interpretation": (
            "공정·작업·이동시간을 반영한 이상적 생산능력 근사치입니다. "
            "평균 설비 경로, 정상상태 cycle과 충전 주기를 가정하므로 "
            "수학적으로 보장된 상한이나 최적해가 아니며 최적성 격차 산정에 사용할 수 없습니다. "
            "기존 theoretical_* 필드명은 결과 파일 호환성을 위해 유지합니다."
        ),
        "method": "parallel_resource_finite_buffer_shortest_path_bound_v3",
        "timing_basis": "triangular_minimum",
        "movement_basis": "static shortest-path tiles with item-load multipliers",
        "product_contract": (
            "15 mandatory accepted-product-path roles; scrap disposal is the alternative fail branch; "
            "two raw materials per accepted product"
        ),
        "formula": "UB=min(material bound, worker bound, 1+floor((H-L_first)/C*)); C*=max(C_S1,C_S2,C_inspection,C_workers)",
        "components": component_template or {},
        "assumptions": [
            "모든 제품이 검사를 통과한다고 가정하고 설비 고장, 예방정비, incident, 동적 교통 대기와 확률적 queue 대기는 제외합니다.",
            "모든 필수 출발지-도착지 item 이동과 최초 charging dock 접근 이동을 포함합니다.",
            "Station 1·2의 병렬 설비 대수, 단일 inspection desk, 전체 worker 작업량과 표준 직접 충전 duty cycle을 참고 모델에 반영합니다.",
            "유한 버퍼의 실제 설정 용량을 기록하되, 근사 계산에서는 버퍼 대기를 제외한 인계가 가능하다고 가정합니다.",
            "연속 작업 사이의 빈 이동과 버퍼 대기를 생략하고 설비 경로를 평균하므로 엄밀한 유한기간 스케줄링 상한은 아닙니다.",
            "충전 duty cycle은 저전력 임계치에서 충전하는 표준 운영 가정입니다. 위험 행동 허용 정책에서 강제되는 제약은 아닙니다.",
        ],
        "expected_reference": {
            "method": "configuration_derived_stochastic_mean_reference_v3",
            "timing_basis": "triangular_expected_value",
            "formula": "E[N_good]=min(material bound, 1+(H-E[L_first])/E[C_operational])*(1-defect probability)",
            "included_losses": [
                "서비스, 이동, 병렬 설비 가공, 검사와 충전시간에는 삼각분포 기댓값을 사용합니다.",
                "실제 가공시간 기준 고장 노출, 평균 수리시간, 예방정비 downtime과 PM hazard 감소를 반영합니다.",
                "worker의 직접 충전 duty cycle과 설정된 검사 불량률을 반영합니다.",
                "설정된 humanoid incident의 1차 기대 복구 부하를 반영합니다.",
                "Station 2 배출부터 운반, 검사와 최종 적치까지를 보수적인 표준 작업 cycle로 연결합니다.",
            ],
            "excluded_losses": [
                "정책에 따라 달라지는 유한 버퍼 대기, task starvation, dispatch 지연과 역할 불균형은 제외합니다.",
                "실제 실행 trace가 필요한 동적 교통 교착과 경로 reservation 대기는 제외합니다.",
                "설비 cycle과 downstream 작업을 정책이 얼마나 잘 중첩하는지는 관측 결과에서 평가합니다.",
            ],
            "interpretation": (
                "설정값으로 계산한 보수적인 운영 계획 기준입니다. "
                "실제 생산량의 통계적 기댓값, 보장 구간 또는 최적해가 아니며 실제 관측치가 이를 초과할 수 있습니다."
            ),
            "components": expected_component_template or {},
        },
        "rows": rows,
    }
