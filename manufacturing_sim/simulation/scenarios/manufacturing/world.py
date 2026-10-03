from __future__ import annotations

import hashlib
import itertools
import copy
import json
import math
import random
from collections import defaultdict, deque
from statistics import mean
from typing import Any

import simpy

from agents.contracts import Commitment, IncidentBlocker, IncidentEvent, Opportunity, OpportunityTarget
from agents.base import (
    FIXED_TASK_BATTERY_EXCEPTION_FAMILIES,
    JobPlan,
    StrategyState,
    default_agent_priority_multipliers,
    default_task_priority_weights,
)
from agents.modes import normalize_decision_mode
from manufacturing_sim.simulation.scenarios.manufacturing.entities import (
    Item,
    ItemState,
    Machine,
    MachineState,
    Task,
    Worker,
    default_humanoid_state_payload,
)
from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import Tile, TileGridMap
from manufacturing_sim.simulation.scenarios.manufacturing.humanoid_runtime import (
    TASK_CODE_BY_PRIORITY_KEY,
    HumanoidTaskRuntime,
)
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.task_rules import (
    MFG_FLOW_SHOP_POLICY_MODES,
    MfgFlowShopTaskPolicy,
)
from manufacturing_sim.simulation.scenarios.manufacturing.traffic import (
    TrafficConflict,
    TrafficMonitor,
    TrafficPlan,
    TrafficSegment,
)
from manufacturing_sim.simulation.scenarios.manufacturing.throughput_policy import (
    ThroughputOptimizerFailed,
    cp_sat_status_name,
    require_cp_sat,
    score_task_for_throughput,
)
from manufacturing_sim.simulation.operational_complexity import build_operational_task_complexity_metrics
from manufacturing_sim.simulation.rolling_horizon import strict_periodic_rolling_horizon_loop
from manufacturing_sim.simulation.timing import PrimitiveTimingResolver, TriangularDistribution, sample_triangular


TASK_ID_PREFIX_BY_TASK_CODE: dict[str, str] = {
    "REPLENISH_MATERIAL": "MAT",
    "TRANSFER": "TR",
    "MANAGE_ROBOT_POWER": "BAT",
    "LOAD_MACHINE": "LOAD",
    "SETUP_MACHINE": "SET",
    "UNLOAD_MACHINE": "UL",
    "INSPECT_PRODUCT": "INS",
    "LOAD_UNLOAD_TRANSFER_INTERFACE": "IFT",
    "REPAIR_MACHINE": "RM",
    "PREVENTIVE_MAINTENANCE": "PM",
    "HANDOVER_ITEM": "HND",
    "COLLECT_WASTE_OR_SCRAP": "SCRAP",
}

_SUPPORTED_SCENARIO_KEYS = {
    "factory_mfg_basic": "factory_mfg_basic",
    "mfg_flow_shop": "mfg_flow_shop",
    "shipyard_basic": "shipyard_basic",
}

MFG_FLOW_SHOP_TASK_CODES = {
    "REPLENISH_MATERIAL",
    "TRANSFER",
    "LOAD_MACHINE",
    "SETUP_MACHINE",
    "UNLOAD_MACHINE",
    "INSPECT_PRODUCT",
    "LOAD_UNLOAD_TRANSFER_INTERFACE",
    "REPAIR_MACHINE",
    "PREVENTIVE_MAINTENANCE",
    "MANAGE_ROBOT_POWER",
    "COLLECT_WASTE_OR_SCRAP",
}

MFG_FLOW_SHOP_OBJECTIVE_MODES = {"minimize_makespan", "maximize_throughput"}


def _stable_random_stream(seed: int, namespace: str) -> random.Random:
    digest = hashlib.sha256(f"{int(seed)}|{str(namespace)}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:16], byteorder="big", signed=False))


def _normalized_scenario_key(cfg: dict[str, Any]) -> str:
    raw = str(cfg.get("scenario_type") or cfg.get("type") or cfg.get("name") or "factory_mfg_basic").strip().lower()
    return _SUPPORTED_SCENARIO_KEYS.get(raw, raw)


def resolve_manufacturing_objective(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve run-length and replenishment semantics without changing legacy scenarios."""

    scenario_key = _normalized_scenario_key(cfg)
    horizon_cfg = cfg.get("horizon", {}) if isinstance(cfg.get("horizon", {}), dict) else {}
    configured_days = max(1, int(horizon_cfg.get("num_days", 1) or 1))
    resolved: dict[str, Any] = {
        "mode": "maximize_throughput",
        "configured_throughput_days": configured_days,
        "run_day_limit": configured_days,
        "max_sim_days": configured_days,
        "terminal_policy": "",
        "restock_interval_days": 1,
        "restock_target_fill": None,
    }
    if scenario_key != "mfg_flow_shop":
        return resolved

    objective_cfg = cfg.get("objective", {}) if isinstance(cfg.get("objective", {}), dict) else {}
    mode = str(objective_cfg.get("mode", "maximize_throughput") or "maximize_throughput").strip().lower()
    if mode not in MFG_FLOW_SHOP_OBJECTIVE_MODES:
        raise ValueError(
            "mfg_flow_shop objective.mode must be one of: "
            + ", ".join(sorted(MFG_FLOW_SHOP_OBJECTIVE_MODES))
        )

    makespan_cfg = objective_cfg.get("makespan", {}) if isinstance(objective_cfg.get("makespan", {}), dict) else {}
    throughput_cfg = objective_cfg.get("throughput", {}) if isinstance(objective_cfg.get("throughput", {}), dict) else {}
    max_sim_days = int(makespan_cfg.get("max_sim_days", 30) or 30)
    if max_sim_days <= 0:
        raise ValueError("objective.makespan.max_sim_days must be greater than zero.")
    terminal_policy = str(
        makespan_cfg.get("terminal_policy", "product_or_disposed_scrap") or "product_or_disposed_scrap"
    ).strip().lower()
    if terminal_policy != "product_or_disposed_scrap":
        raise ValueError(
            "mfg_flow_shop objective.makespan.terminal_policy must be product_or_disposed_scrap."
        )
    restock_interval_days = int(throughput_cfg.get("restock_interval_days", 1) or 1)
    if restock_interval_days <= 0:
        raise ValueError("objective.throughput.restock_interval_days must be greater than zero.")
    raw_target_fill = throughput_cfg.get("restock_target_fill")
    restock_target_fill = int(raw_target_fill) if raw_target_fill is not None else None
    if restock_target_fill is not None and restock_target_fill < 0:
        raise ValueError("objective.throughput.restock_target_fill must be zero or greater.")

    resolved.update(
        {
            "mode": mode,
            "run_day_limit": max_sim_days if mode == "minimize_makespan" else configured_days,
            "max_sim_days": max_sim_days,
            "terminal_policy": terminal_policy,
            "restock_interval_days": restock_interval_days,
            "restock_target_fill": restock_target_fill,
        }
    )
    return resolved


def _scenario_entry(mapping: Any, scenario_key: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    candidates = [scenario_key]
    for key in candidates:
        if key and key in mapping:
            return mapping[key]
    return None


class ManufacturingWorld:
    def __init__(
        self,
        env: simpy.Environment,
        cfg: dict[str, Any],
        logger: EventLogger,
        decision_module: Any,
    ) -> None:
        self.env = env
        self.cfg = cfg
        self.logger = logger
        self.decision_module = decision_module
        self.scenario_key = _normalized_scenario_key(cfg)
        self.is_mfg_flow_shop = self.scenario_key == "mfg_flow_shop"
        self.objective = resolve_manufacturing_objective(cfg)
        self.objective_mode = str(self.objective["mode"])
        decision_cfg = cfg.get("decision", {}) if isinstance(cfg.get("decision", {}), dict) else {}
        self.decision_mode = normalize_decision_mode(str(decision_cfg.get("mode", "adaptive_priority")))
        self.mfg_flow_task_policy: MfgFlowShopTaskPolicy | None = None

        seed = int(cfg.get("seed", 7))
        self.seed = seed
        # Stochastic mechanisms use independent streams. Dispatch order may
        # change exposure, but it must not alter quality or failure samples.
        self.rng = random.Random(seed)
        self.quality_rng = _stable_random_stream(seed, f"{self.scenario_key}:quality")
        self.machine_failure_rngs: dict[str, random.Random] = {}
        self.humanoid_incident_rngs: dict[tuple[str, str], random.Random] = {}
        self.stochastic_streams = {
            "scheme": "isolated_v1",
            "base_seed": seed,
            "quality": f"{self.scenario_key}:quality",
            "machine_failure": f"{self.scenario_key}:machine_failure:<machine_id>",
            "humanoid_incident": f"{self.scenario_key}:humanoid_incident:<worker_id>:<incident_code>",
        }
        configured_task_codes = cfg.get("factory", {}).get("enabled_task_codes", [])
        self.enabled_task_codes = {
            str(code).strip().upper() for code in configured_task_codes if str(code).strip()
        }
        if not self.enabled_task_codes:
            self.enabled_task_codes = (
                set(MFG_FLOW_SHOP_TASK_CODES)
                if self.is_mfg_flow_shop
                else set(TASK_CODE_BY_PRIORITY_KEY.values())
            )
        self.timing = PrimitiveTimingResolver(
            cfg.get("task_primitive_timing", {}),
            scenario_type=self.scenario_key,
            seed=seed,
            expected_task_codes=self.enabled_task_codes,
        )
        self.primitive_duration_cache: dict[tuple[str, str], float] = {}

        horizon_cfg = cfg["horizon"]
        self.configured_throughput_days = int(self.objective["configured_throughput_days"])
        self.num_days = int(self.objective["run_day_limit"])
        self.minutes_per_day = int(horizon_cfg["minutes_per_day"])

        factory_cfg = cfg["factory"]
        self.num_workers = int(factory_cfg.get("num_workers", factory_cfg.get("num_agents", 3)))
        # Deprecated alias retained for existing decision modules and dashboards.
        self.num_agents = self.num_workers
        self.machines_per_station = int(factory_cfg["machines_per_station"])

        process_cfg = factory_cfg["processing_time"]
        station_time_pairs: list[tuple[int, TriangularDistribution]] = []
        for key, value in process_cfg.items():
            key_str = str(key)
            if not key_str.startswith("station"):
                continue
            suffix = key_str.replace("station", "", 1)
            if not suffix.isdigit():
                continue
            station_time_pairs.append(
                (
                    int(suffix),
                    TriangularDistribution.from_config(value, label=f"factory.processing_time.{key_str}"),
                )
            )
        if not station_time_pairs:
            raise ValueError("factory.processing_time must define at least one stationN entry.")
        station_time_pairs.sort(key=lambda x: x[0])
        self.stations = [station for station, _ in station_time_pairs]
        self.last_processing_station = max(self.stations)
        self.inspection_queue_station = 4
        self.processing_time_distribution = {station: distribution for station, distribution in station_time_pairs}
        self.processing_time_min = {station: distribution.expected for station, distribution in station_time_pairs}
        self.inspection_base_time_min = self.timing.expected_call_duration("INSPECT_PRODUCT", "EXECUTE_QUALITY_ACTION")
        self.inspection_min_time_min = self.inspection_base_time_min

        self.movement_cfg = cfg["movement"]
        self.traffic_cfg = self.movement_cfg.get("traffic", {}) if isinstance(self.movement_cfg.get("traffic", {}), dict) else {}
        self.traffic_enabled = bool(self.traffic_cfg.get("enabled", True))
        self.traffic_mode = str(self.traffic_cfg.get("mode", "observe_conflicts")).strip().lower() or "observe_conflicts"
        self.traffic_fidelity = str(self.traffic_cfg.get("fidelity", "tile_edge")).strip().lower() or "tile_edge"
        self.traffic_collision_effect = str(self.traffic_cfg.get("collision_effect", "log_only")).strip().lower() or "log_only"
        self.traffic_emit_tile_step_events = bool(self.traffic_cfg.get("emit_tile_step_events", True))
        self.traffic_monitor = (
            TrafficMonitor(near_miss_headway_min=float(self.traffic_cfg.get("near_miss_headway_min", 0.05) or 0.05))
            if self.traffic_enabled
            else None
        )
        self.traffic_conflicts: list[dict[str, Any]] = []
        self.traffic_move_counter = itertools.count(1)
        self.timing_move_counter = itertools.count(1)
        self.active_movement_timing_sessions: dict[str, dict[str, str]] = {}
        transport_cfg = self.movement_cfg.get("item_transport", {}) if isinstance(self.movement_cfg.get("item_transport", {}), dict) else {}
        self.item_transport_weight_multiplier = {
            "material": self.timing.multiplier("material"),
            "intermediate": self.timing.multiplier("intermediate"),
            "product": self.timing.multiplier("product"),
            "battery": self.timing.multiplier("battery"),
            "battery_fresh": self.timing.multiplier("battery_fresh", self.timing.multiplier("battery")),
            "battery_spent": self.timing.multiplier("battery_spent", self.timing.multiplier("battery")),
        }
        collaboration_cfg = (
            transport_cfg.get("product_collaboration", {})
            if isinstance(transport_cfg.get("product_collaboration", {}), dict)
            else {}
        )
        self.product_collaboration_enabled = bool(collaboration_cfg.get("enabled", True))
        self.product_collaboration_max_carriers = max(1, int(collaboration_cfg.get("max_carriers", 2) or 2))
        self.product_collaboration_divide_time = bool(collaboration_cfg.get("divide_time_by_carrier_count", True))
        self.transport_session_counter = itertools.count(1)
        self.product_transport_sessions: dict[str, dict[str, Any]] = {}
        self.product_transport_session_by_item: dict[str, str] = {}
        self.product_transport_session_by_worker: dict[str, str] = {}
        self.quality_cfg = cfg["quality"]
        scrap_transport_cfg = self.quality_cfg.get("scrap_transport", {}) if isinstance(self.quality_cfg.get("scrap_transport", {}), dict) else {}
        self.scrap_transport_max_carry_count = max(1, int(scrap_transport_cfg.get("max_carry_count", 3) or 3))
        warehouse_cfg = cfg.get("warehouse", {}) if isinstance(cfg.get("warehouse", {}), dict) else {}
        material_shelf_cfg = (
            warehouse_cfg.get("material_shelf", {})
            if isinstance(warehouse_cfg.get("material_shelf", {}), dict)
            else {}
        )
        self.material_shelf_capacity = max(0, int(material_shelf_cfg.get("capacity", 10) or 10))
        self.material_shelf_initial_fill = max(
            0,
            min(
                self.material_shelf_capacity,
                int(material_shelf_cfg.get("initial_fill", self.material_shelf_capacity) or self.material_shelf_capacity),
            ),
        )
        self.material_shelf_restock_policy = (
            "objective"
            if self.is_mfg_flow_shop
            else str(material_shelf_cfg.get("restock_policy", "day_boundary") or "day_boundary").strip().lower()
        )
        configured_restock_target = self.objective.get("restock_target_fill")
        self.throughput_restock_target_fill = (
            self.material_shelf_capacity
            if configured_restock_target is None
            else int(configured_restock_target)
        )
        if self.throughput_restock_target_fill > self.material_shelf_capacity:
            raise ValueError(
                "objective.throughput.restock_target_fill cannot exceed warehouse.material_shelf.capacity."
            )
        self.throughput_restock_interval_days = int(self.objective.get("restock_interval_days", 1) or 1)
        self.machine_failure_cfg = cfg["machine_failure"]
        self.machine_failure_cfg["repair_time_min"] = self.timing.expected_call_duration(
            "REPAIR_MACHINE", "EXECUTE_MAINTENANCE_ACTION"
        )
        preventive_cfg = (
            self.machine_failure_cfg.get("preventive_maintenance", {})
            if isinstance(self.machine_failure_cfg.get("preventive_maintenance", {}), dict)
            else {}
        )
        self.preventive_maintenance_enabled = bool(
            preventive_cfg.get("enabled", "PREVENTIVE_MAINTENANCE" in self.enabled_task_codes)
        )
        if self.preventive_maintenance_enabled:
            self.machine_failure_cfg["pm_time_min"] = self.timing.expected_call_duration(
                "PREVENTIVE_MAINTENANCE", "INSPECT_OR_DIAGNOSE"
            )
        legacy_agent_cfg = cfg.get("agent", {}) if isinstance(cfg.get("agent", {}), dict) else {}
        worker_cfg = cfg.get("worker", {}) if isinstance(cfg.get("worker", {}), dict) else {}
        # Keep legacy scenario keys readable while making `worker` the canonical config surface.
        self.agent_cfg = {
            "battery_swap_period_min": 200,
            "battery_pickup_time_min": self.timing.expected_task_duration("MANAGE_ROBOT_POWER"),
            "battery_delivery_extra_min": self.timing.expected_task_duration("TRANSFER"),
            **legacy_agent_cfg,
            **worker_cfg,
        }
        battery_service_cfg = (
            self.agent_cfg.get("battery_service", {})
            if isinstance(self.agent_cfg.get("battery_service", {}), dict)
            else {}
        )
        self.battery_service_mode = str(battery_service_cfg.get("mode", "battery_swap")).strip().lower()
        self.battery_direct_charge_enabled = self.battery_service_mode == "dock_charge"
        self.battery_delivery_enabled = bool(
            battery_service_cfg.get(
                "delivery_enabled",
                battery_service_cfg.get("allow_battery_delivery", not self.battery_direct_charge_enabled),
            )
        )
        self.battery_swap_enabled = bool(
            battery_service_cfg.get(
                "swap_enabled",
                battery_service_cfg.get("allow_battery_swap", not self.battery_direct_charge_enabled),
            )
        )
        self.battery_charge_low_threshold_ratio = max(
            0.0, min(1.0, float(battery_service_cfg.get("low_threshold_ratio", 0.30) or 0.30))
        )
        self.battery_charge_target_soc = max(
            0.0, min(1.0, float(battery_service_cfg.get("target_soc", 1.0) or 1.0))
        )
        self.inspection_workstation_id = str(
            factory_cfg.get("inspection", {}).get("workstation_id", "inspection_table")
        ).strip() or "inspection_table"
        self.inspection_capacity = max(
            1,
            int(factory_cfg.get("inspection", {}).get("capacity", 1) or 1),
        )
        self.buffer_capacities: dict[str, int] = {}
        if self.is_mfg_flow_shop:
            buffer_cfg = factory_cfg.get("buffers", {})
            if not isinstance(buffer_cfg, dict):
                raise ValueError("factory.buffers must be a mapping for mfg_flow_shop.")
            for station in self.stations:
                station_cfg = buffer_cfg.get(f"station{station}", {})
                if not isinstance(station_cfg, dict):
                    raise ValueError(f"factory.buffers.station{station} must be a mapping.")
                capacity_fields = {
                    f"material_queue_{station}": "material_input_capacity",
                    f"output_buffer_station_{station}": "output_capacity",
                }
                if self._station_requires_intermediate(station):
                    capacity_fields[f"intermediate_queue_{station}"] = "intermediate_input_capacity"
                for buffer_id, field_name in capacity_fields.items():
                    value = int(station_cfg.get(field_name, 0) or 0)
                    if value <= 0:
                        raise ValueError(
                            f"factory.buffers.station{station}.{field_name} must be greater than zero."
                        )
                    self.buffer_capacities[buffer_id] = value
            inspection_buffer_cfg = buffer_cfg.get("inspection", {})
            if not isinstance(inspection_buffer_cfg, dict):
                raise ValueError("factory.buffers.inspection must be a mapping.")
            inspection_fields = {
                f"intermediate_queue_{self.inspection_queue_station}": "input_capacity",
                f"output_buffer_station_{self.inspection_queue_station}": "pass_output_capacity",
                "inspection_scrap_queue": "scrap_output_capacity",
            }
            for buffer_id, field_name in inspection_fields.items():
                value = int(inspection_buffer_cfg.get(field_name, 0) or 0)
                if value <= 0:
                    raise ValueError(
                        f"factory.buffers.inspection.{field_name} must be greater than zero."
                    )
                self.buffer_capacities[buffer_id] = value
        # ``factory_mfg_basic`` keeps its legacy target-level replenishment.
        # ``mfg_flow_shop`` is demand-driven and intentionally has no station
        # inventory target configuration.
        self.inventory_targets = (
            cfg.get("inventory_targets", {})
            if isinstance(cfg.get("inventory_targets", {}), dict)
            else {}
        )
        self.dispatcher_cfg = cfg["dispatcher"]
        self.heuristic_rules = cfg.get("heuristic_rules", {}) if isinstance(cfg.get("heuristic_rules", {}), dict) else {}
        self.throughput_policy_cfg = (
            decision_cfg.get("throughput_policy", {})
            if isinstance(decision_cfg.get("throughput_policy", {}), dict)
            else {}
        )
        self._init_rolling_horizon(decision_cfg)
        llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
        orchestration_cfg = llm_cfg.get("orchestration", {}) if isinstance(llm_cfg.get("orchestration", {}), dict) else {}
        # The manager may queue more work than the runtime should examine; limit the local queue window here.
        self.worker_queue_limit = max(
            1,
            int(getattr(decision_module, "worker_queue_limit", orchestration_cfg.get("worker_queue_limit", 4)) or 4),
        )

        self.machine_failure_distribution = str(
            self.machine_failure_cfg.get("distribution", "exponential") or "exponential"
        ).strip().lower()
        self.machine_failure_time_basis = str(
            self.machine_failure_cfg.get("time_basis", "calendar_time") or "calendar_time"
        ).strip().lower()
        if self.machine_failure_distribution != "exponential":
            raise ValueError("machine_failure.distribution currently supports only 'exponential'.")
        if self.machine_failure_time_basis not in {"calendar_time", "active_processing"}:
            raise ValueError(
                "machine_failure.time_basis must be 'calendar_time' or 'active_processing'."
            )
        mean_ttf = float(
            self.machine_failure_cfg.get(
                "mean_processing_time_to_failure_min",
                self.machine_failure_cfg.get("mean_time_to_fail_min", 300.0),
            )
        )
        self.machine_failure_base_lambda = 1.0 / max(1.0, mean_ttf)
        self.machine_failure_mean_exposure_min = max(1.0, mean_ttf)
        self.max_repair_agents = max(1, int(self.machine_failure_cfg.get("max_repair_agents", 3) or 3))
        self.pm_lambda_multiplier = float(
            preventive_cfg.get(
                "hazard_multiplier",
                self.machine_failure_cfg.get("pm_lambda_multiplier", 1.0),
            )
        )
        self.pm_effect_duration_min = float(
            preventive_cfg.get(
                "protected_processing_min",
                self.machine_failure_cfg.get("pm_effect_duration_min", 0.0),
            )
        )
        self.pm_interval_target_min = float(
            preventive_cfg.get(
                "due_processing_min",
                self.machine_failure_cfg.get("pm_interval_target_min", math.inf),
            )
        )
        if not 0.0 <= self.pm_lambda_multiplier <= 1.0:
            raise ValueError("preventive_maintenance.hazard_multiplier must be between 0 and 1.")

        self.battery_swap_period_min = float(
            self.agent_cfg.get("battery_capacity_min", self.agent_cfg["battery_swap_period_min"])
        )
        battery_drain_cfg = self.agent_cfg.get("battery_drain", {}) if isinstance(self.agent_cfg.get("battery_drain", {}), dict) else {}
        self.battery_available_rate_multiplier = max(
            0.0,
            float(battery_drain_cfg.get("available_rate_multiplier", 1.0) or 1.0),
        )
        self.battery_non_available_rate_multiplier = max(
            0.0,
            float(battery_drain_cfg.get("non_available_rate_multiplier", 2.0) or 2.0),
        )
        battery_safety_cfg = (
            worker_cfg.get("battery_safety", {})
            if isinstance(worker_cfg.get("battery_safety", {}), dict)
            else {}
        )
        self.battery_assignment_mode = str(
            battery_safety_cfg.get("assignment_mode", "hard_reserve") or "hard_reserve"
        ).strip().lower()
        if self.battery_assignment_mode not in {"hard_reserve", "policy_decides"}:
            raise ValueError(
                "worker.battery_safety.assignment_mode must be 'hard_reserve' or 'policy_decides'."
            )
        self.expose_battery_risk_metadata = bool(
            battery_safety_cfg.get("expose_risk_metadata", True)
        )
        depleted_recovery_cfg = (
            worker_cfg.get("depleted_recovery", {})
            if isinstance(worker_cfg.get("depleted_recovery", {}), dict)
            else {}
        )
        self.depleted_recovery_enabled = bool(depleted_recovery_cfg.get("enabled", False))
        self.depleted_recovery_schedule = str(
            depleted_recovery_cfg.get("schedule", "next_day_start") or "next_day_start"
        ).strip().lower()
        self.depleted_recovery_restart_location = str(
            depleted_recovery_cfg.get("restart_location", "assigned_charging_dock")
            or "assigned_charging_dock"
        ).strip().lower()
        self.depleted_recovery_restart_soc = min(
            1.0,
            max(0.0, float(depleted_recovery_cfg.get("restart_soc", 1.0) or 1.0)),
        )
        if self.depleted_recovery_enabled and self.depleted_recovery_schedule != "next_day_start":
            raise ValueError("worker.depleted_recovery.schedule currently supports only 'next_day_start'.")

        self.current_day = 1
        self.current_strategy = StrategyState()
        self.current_job_plan = JobPlan(
            task_priority_weights=default_task_priority_weights(),
            quotas={},
            rationale="default",
            agent_priority_multipliers=default_agent_priority_multipliers([f"A{i}" for i in range(1, self.num_workers + 1)]),
        )
        local_response_cfg = worker_cfg.get("local_response", {}) if isinstance(worker_cfg.get("local_response", {}), dict) else {}
        self.worker_execution_mode = str(worker_cfg.get("execution_mode", "commitment")).strip().lower() or "commitment"
        self.worker_local_response_cfg = {
            "enabled": bool(local_response_cfg.get("enabled", True)),
            "scope": str(local_response_cfg.get("scope", "standard")).strip().lower() or "standard",
            "max_local_attempts_per_incident": max(0, int(local_response_cfg.get("max_local_attempts_per_incident", 2) or 2)),
            "allow_handoff": bool(local_response_cfg.get("allow_handoff", True)),
            "allow_self_reorder": bool(local_response_cfg.get("allow_self_reorder", True)),
            "allow_self_recovery": bool(local_response_cfg.get("allow_self_recovery", True)),
            "blocked_duration_escalation_min": float(local_response_cfg.get("blocked_duration_escalation_min", 5.0) or 5.0),
            "expiry_margin_escalation_min": float(local_response_cfg.get("expiry_margin_escalation_min", 4.0) or 4.0),
        }
        incident_cfg = orchestration_cfg.get("incident", {}) if isinstance(orchestration_cfg.get("incident", {}), dict) else {}
        detector_recheck_cfg = incident_cfg.get("detector_recheck", {}) if isinstance(incident_cfg.get("detector_recheck", {}), dict) else {}
        self.incident_policy = {
            "enabled": bool(incident_cfg.get("enabled", True)),
            "prefer_worker_local_response": bool(incident_cfg.get("prefer_worker_local_response", True)),
            "capacity_loss_ratio": float(detector_recheck_cfg.get("capacity_loss_ratio", 0.5) or 0.5),
            "recurring_incident_count": max(1, int(detector_recheck_cfg.get("recurring_incident_count", 2) or 2)),
            "backlog_delta": max(1, int(detector_recheck_cfg.get("backlog_delta", 3) or 3)),
        }
        self.incident_counter = itertools.count(1)
        self.incident_events: list[dict[str, Any]] = []
        humanoid_incident_cfg = cfg.get("humanoid_incidents", {}) if isinstance(cfg.get("humanoid_incidents", {}), dict) else {}
        self.humanoid_incident_cfg = humanoid_incident_cfg
        self.humanoid_incidents_enabled = bool(humanoid_incident_cfg.get("enabled", True))
        self.humanoid_incident_random_cfg = self._normalize_humanoid_incident_cfg_keys(
            humanoid_incident_cfg.get("random", {}) if isinstance(humanoid_incident_cfg.get("random", {}), dict) else {}
        )
        self.humanoid_incident_natural_cfg = self._normalize_humanoid_incident_cfg_keys(
            humanoid_incident_cfg.get("natural", {}) if isinstance(humanoid_incident_cfg.get("natural", {}), dict) else {}
        )
        self.humanoid_incident_schema: Any | None = None
        self.humanoid_incident_events: list[dict[str, Any]] = []
        self.humanoid_incident_retry_counts: dict[tuple[str, str], int] = defaultdict(int)
        self.commitment_claims: dict[str, dict[str, Any]] = {}
        self.selection_blocker_counter = itertools.count(1)
        self.selection_blockers: dict[str, dict[str, Any]] = {}
        self.active_selection_blocker_by_agent: dict[str, str] = {}
        self.incident_escalations: set[str] = set()
        self.day_unique_replan_blockers: set[str] = set()
        self.day_planner_escalations: set[str] = set()
        self.manager_queue_skipped_counts: dict[str, int] = defaultdict(int)
        decision_cfg = self.cfg.get("decision", {}) if isinstance(self.cfg.get("decision", {}), dict) else {}
        self.material_queues: dict[int, deque[str]] = {station: deque() for station in self.stations}
        # Station1 does not consume intermediate; intermediate queues start at Station2.
        self.intermediate_queues: dict[int, deque[str]] = {
            station: deque() for station in self.stations if self._station_requires_intermediate(station)
        }
        self.intermediate_queues[self.inspection_queue_station] = deque()
        # Output buffers for each stage.
        # processing stations: machine output before next transfer
        # inspection queue station: inspection-pass output waiting transfer to Warehouse
        self.output_buffers: dict[int, deque[str]] = {station: deque() for station in self.stations}
        self.output_buffers[self.inspection_queue_station] = deque()
        self.inspection_scrap_queue: deque[str] = deque()
        self.material_supply_owner: dict[int, str | None] = {station: None for station in self.stations}
        self.scrap_disposal_owner: str | None = None
        self.item_reservations: dict[str, dict[str, Any]] = {}
        self.buffer_slot_reservations: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        self.buffer_metrics: dict[str, Any] = {
            "overflow_attempt_count": 0,
            "reservation_failure_count": 0,
            "reservation_release_count": 0,
            "reservation_commit_count": 0,
            "blocked_after_service_count": 0,
            "blocked_after_service_started_at": {},
            "blocked_after_service_min_by_machine": defaultdict(float),
            "max_occupancy_by_buffer": defaultdict(int),
            "max_reserved_by_buffer": defaultdict(int),
            "max_committed_plus_reserved_by_buffer": defaultdict(int),
            "candidate_scan_count": 0,
            "candidate_count_total": 0,
            "candidate_count_max": 0,
        }

        self.items: dict[str, Item] = {}
        self.dropped_items: dict[str, dict[str, Any]] = {}
        self.item_counter = itertools.count(1)
        self.task_counter = itertools.count(1)
        self.machine_cycle_counter = itertools.count(1)
        self.warehouse_material_shelf_slots: dict[str, dict[str, Any]] = {}
        self.material_shelf_empty_alerted = False
        self.warehouse_material_restock_count = 0
        self.material_shelf_pick_count = 0
        self.disposed_scrap_count = 0
        self.scrap_transport_batches = 0
        self.scrap_transport_items = 0

        map_cfg = cfg.get("map", {}) if isinstance(cfg.get("map", {}), dict) else {}
        self.map_enabled = bool(map_cfg.get("enabled", True))
        self.grid_map: TileGridMap | None = (
            TileGridMap.from_world_config(
                cfg,
                stations=self.stations,
                machines_per_station=self.machines_per_station,
            )
            if self.map_enabled
            else None
        )
        if self.grid_map is not None:
            self.grid_map.tile_time_min = self.timing.expected_tile_time
        self.movement_cfg["setup_min"] = self.timing.expected_call_duration("SETUP_MACHINE", "EXECUTE_MACHINE_ACTION")
        self.movement_cfg["unload_min"] = self.timing.expected_call_duration("UNLOAD_MACHINE", "EXECUTE_MACHINE_ACTION")

        self.machines: dict[str, Machine] = {}
        self.machines_by_station: dict[int, list[str]] = {station: [] for station in self.stations}
        self._build_machines()

        self.workers: dict[str, Worker] = {}
        self._build_workers()
        # Deprecated alias retained for modules that still use "agent" as orchestration vocabulary.
        self.agents = self.workers
        self._rolling_horizon_initialize_worker_events()
        self._init_mfg_flow_task_policy(decision_cfg)
        self.humanoid_runtime = HumanoidTaskRuntime(self, cfg)

        self.product_count = 0
        self.scrap_count = 0
        self.initial_batch_material_ids: set[str] = set()
        self.initial_batch_material_station: dict[str, int] = {}
        self.initial_batch_terminal_material_ids: set[str] = set()
        self.initial_batch_accepted_output_ids: set[str] = set()
        self.initial_batch_disposed_scrap_ids: set[str] = set()
        self.initial_batch_terminal_outcomes: dict[str, dict[str, str]] = {}
        self.makespan_min: float | None = None
        self.objective_inventory_prepared_days: set[int] = set()
        self.station_throughput = defaultdict(int)
        self.inspection_active_agents = 0
        self.inspection_owner: str | None = None
        self.inspection_desk_item_id: str | None = None
        self.inspection_desk_result: str | None = None
        self.inspection_desk_state = "EMPTY"

        self.minute_snapshots: list[dict[str, Any]] = []
        self.task_records: list[dict[str, Any]] = []
        self.daily_summaries: list[dict[str, Any]] = []
        self.day_baseline: dict[str, Any] = {}

        self.snapshot_interval = float(self.dispatcher_cfg["snapshot_interval_min"])
        self.terminated = False
        self.termination_reason = ""
        self.termination_event = self.env.event()
        self.pending_termination_reason = ""
        self.pending_termination_agent_id = ""
        self.active_battery_delivery_owner: str | None = None
        self.adp_coordinator: Any | None = None
        if self.decision_mode in {"simulation_based_adp", "random_feasible_dispatch"}:
            if not self.is_mfg_flow_shop or self.objective_mode != "maximize_throughput":
                raise ValueError(
                    f"{self.decision_mode} supports only scenario=mfg_flow_shop with "
                    "scenario.objective.mode=maximize_throughput."
                )
            from manufacturing_sim.adp import ADPDecisionCoordinator

            adp_cfg = decision_cfg.get("adp", {}) if isinstance(decision_cfg.get("adp", {}), dict) else {}
            self.adp_coordinator = ADPDecisionCoordinator(self, adp_cfg)

    def _init_rolling_horizon(self, decision_cfg: dict[str, Any]) -> None:
        rolling_cfg = decision_cfg.get("rolling_horizon", {}) if isinstance(decision_cfg.get("rolling_horizon", {}), dict) else {}
        battery_cfg = decision_cfg.get("battery", {}) if isinstance(decision_cfg.get("battery", {}), dict) else {}
        scenario_key = _normalized_scenario_key(self.cfg)
        self.rolling_horizon_enabled = self.decision_mode in {
            "rolling_horizon_aging_priority",
            "rolling_horizon_shared",
            "rolling_horizon_dedicated_roles",
            "rolling_horizon_throughput_optimizer",
        }
        self.rolling_horizon_dedicated_roles_enabled = self.decision_mode == "rolling_horizon_dedicated_roles"
        self.rolling_horizon_throughput_optimizer_enabled = self.decision_mode == "rolling_horizon_throughput_optimizer"
        self.rolling_horizon_window_min = max(0.1, float(rolling_cfg.get("window_min", 5.0) or 5.0))
        self.rolling_horizon_scheduler_mode = "strict_periodic"
        self.rolling_horizon_candidate_collection_mode = "event_with_boundary_reconciliation"
        configured_scheduler_mode = str(
            rolling_cfg.get("scheduler_mode", self.rolling_horizon_scheduler_mode) or self.rolling_horizon_scheduler_mode
        ).strip().lower()
        configured_collection_mode = str(
            rolling_cfg.get(
                "candidate_collection_mode",
                self.rolling_horizon_candidate_collection_mode,
            )
            or self.rolling_horizon_candidate_collection_mode
        ).strip().lower()
        # Legacy values remain parseable, but rolling execution is always the
        # strict-periodic runtime above. Keep both for auditability.
        self.rolling_horizon_configured_scheduler_mode = configured_scheduler_mode
        self.rolling_horizon_configured_candidate_collection_mode = configured_collection_mode
        scenario_dispatch_policy = _scenario_entry(
            rolling_cfg.get("scenario_dispatch_policy", {}), scenario_key
        )
        self.rolling_horizon_dispatch_policy = (
            str(
                scenario_dispatch_policy
                if scenario_dispatch_policy is not None
                else rolling_cfg.get("dispatch_policy", "aging_priority")
            ).strip().lower()
            or "aging_priority"
        )
        optimizer_cfg = rolling_cfg.get("optimizer", {}) if isinstance(rolling_cfg.get("optimizer", {}), dict) else {}
        self.rolling_horizon_optimizer_cfg = optimizer_cfg
        self.rolling_horizon_optimizer_cp_model = None
        if self.rolling_horizon_throughput_optimizer_enabled:
            self.rolling_horizon_optimizer_cp_model = require_cp_sat()
            self.rolling_horizon_dispatch_policy = "throughput_optimizer"
        self.rolling_horizon_battery_low_ratio = max(
            0.0,
            min(1.0, float(battery_cfg.get("low_threshold_ratio", 0.20) or 0.20)),
        )
        self.rolling_horizon_battery_delivery_provider_agent_ids = [
            str(value).strip()
            for value in battery_cfg.get("delivery_provider_agent_ids", ["A1"])
            if str(value).strip()
        ]
        self.rolling_horizon_battery_delivery_receiver_agent_ids = [
            str(value).strip()
            for value in battery_cfg.get("delivery_receiver_agent_ids", ["A2", "A3"])
            if str(value).strip()
        ]
        if not self.battery_delivery_enabled:
            self.rolling_horizon_battery_delivery_provider_agent_ids = []
            self.rolling_horizon_battery_delivery_receiver_agent_ids = []

        default_priority_order = [
            "MANAGE_ROBOT_POWER",
            "REPAIR_MACHINE",
            "COLLECT_WASTE_OR_SCRAP",
            "UNLOAD_MACHINE",
            "HANDOVER_ITEM",
            "LOAD_MACHINE",
            "SETUP_MACHINE",
            "TRANSFER",
            "REPLENISH_MATERIAL",
            "LOAD_UNLOAD_TRANSFER_INTERFACE",
            "INSPECT_PRODUCT",
            "PREVENTIVE_MAINTENANCE",
        ]
        default_worker_task_priority = {
            "A1": ["MANAGE_ROBOT_POWER", "REPLENISH_MATERIAL"],
            "A2": ["REPAIR_MACHINE", "LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE"],
            "A3": ["TRANSFER", "LOAD_UNLOAD_TRANSFER_INTERFACE", "INSPECT_PRODUCT", "COLLECT_WASTE_OR_SCRAP", "PREVENTIVE_MAINTENANCE"],
        }
        scenario_worker_priority = _scenario_entry(rolling_cfg.get("scenario_worker_task_priority", {}), scenario_key)
        raw_worker_priority = (
            scenario_worker_priority
            if isinstance(scenario_worker_priority, dict)
            else rolling_cfg.get("worker_task_priority", {})
            if isinstance(rolling_cfg.get("worker_task_priority", {}), dict)
            else {}
        )
        if self.rolling_horizon_dedicated_roles_enabled and not raw_worker_priority:
            raw_worker_priority = default_worker_task_priority
        self.rolling_horizon_worker_task_priority: dict[str, list[str]] = {}
        for worker_id, values in raw_worker_priority.items():
            if not isinstance(values, list):
                continue
            normalized_codes: list[str] = []
            seen_worker_codes: set[str] = set()
            for value in values:
                code = str(value or "").strip().upper()
                if code and code not in seen_worker_codes:
                    normalized_codes.append(code)
                    seen_worker_codes.add(code)
            if normalized_codes:
                self.rolling_horizon_worker_task_priority[str(worker_id).strip()] = normalized_codes
        has_granular_policy = isinstance(decision_cfg.get("mfg_flow_shop_policy", {}), dict) and bool(
            decision_cfg.get("mfg_flow_shop_policy", {})
        )
        if self.is_mfg_flow_shop and self.rolling_horizon_dedicated_roles_enabled and not has_granular_policy:
            role_templates = [
                ["MANAGE_ROBOT_POWER", "REPAIR_MACHINE", "REPLENISH_MATERIAL"],
                ["MANAGE_ROBOT_POWER", "REPAIR_MACHINE", "LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE"],
                [
                    "MANAGE_ROBOT_POWER",
                    "REPAIR_MACHINE",
                    "TRANSFER",
                    "LOAD_UNLOAD_TRANSFER_INTERFACE",
                    "INSPECT_PRODUCT",
                    "COLLECT_WASTE_OR_SCRAP",
                ],
            ]
            for index in range(1, self.num_workers + 1):
                worker_id = f"A{index}"
                configured = self.rolling_horizon_worker_task_priority.get(worker_id)
                template = configured or role_templates[(index - 1) % len(role_templates)]
                ordered = ["MANAGE_ROBOT_POWER", "REPAIR_MACHINE", *template]
                self.rolling_horizon_worker_task_priority[worker_id] = list(
                    dict.fromkeys(code for code in ordered if code in self.enabled_task_codes)
                )
        self.rolling_horizon_worker_task_rank: dict[str, dict[str, int]] = {
            worker_id: {code: index + 1 for index, code in enumerate(codes)}
            for worker_id, codes in self.rolling_horizon_worker_task_priority.items()
        }
        scenario_order = _scenario_entry(rolling_cfg.get("scenario_task_code_priority_order", {}), scenario_key)
        configured_order = (
            scenario_order
            if isinstance(scenario_order, list)
            else rolling_cfg.get("task_code_priority_order", [])
        )
        dedicated_order: list[str] = []
        for codes in self.rolling_horizon_worker_task_priority.values():
            dedicated_order.extend(codes)
        raw_order = (
            configured_order
            if isinstance(configured_order, list) and configured_order
            else dedicated_order
            if self.rolling_horizon_dedicated_roles_enabled and dedicated_order
            else default_priority_order
        )
        priority_order: list[str] = []
        seen_codes: set[str] = set()
        for value in raw_order:
            code = str(value or "").strip().upper()
            if (
                code
                and code not in seen_codes
                and (not self.is_mfg_flow_shop or code in self.enabled_task_codes)
            ):
                priority_order.append(code)
                seen_codes.add(code)
        fallback_priority_order = [] if self.rolling_horizon_dedicated_roles_enabled and dedicated_order else default_priority_order
        for code in fallback_priority_order:
            if code not in seen_codes and (not self.is_mfg_flow_shop or code in self.enabled_task_codes):
                priority_order.append(code)
                seen_codes.add(code)
        self.rolling_horizon_task_code_priority_order = priority_order
        self.rolling_horizon_task_code_rank: dict[str, int] = {
            code: index + 1 for index, code in enumerate(priority_order)
        }
        scenario_aging_cfg = _scenario_entry(rolling_cfg.get("scenario_aging", {}), scenario_key)
        aging_cfg = (
            scenario_aging_cfg
            if isinstance(scenario_aging_cfg, dict)
            else rolling_cfg.get("aging", {})
            if isinstance(rolling_cfg.get("aging", {}), dict)
            else {}
        )
        configured_rank_boost = aging_cfg.get("rank_boost_per_window", 1)
        self.rolling_horizon_rank_boost_per_window = max(
            0,
            int(1 if configured_rank_boost is None else configured_rank_boost),
        )
        scenario_immediate_cfg = _scenario_entry(
            rolling_cfg.get("scenario_immediate_task_triggers", {}), scenario_key
        )
        immediate_cfg = (
            scenario_immediate_cfg
            if isinstance(scenario_immediate_cfg, dict)
            else rolling_cfg.get("immediate_task_triggers", {})
            if isinstance(rolling_cfg.get("immediate_task_triggers", {}), dict)
            else {}
        )
        self.rolling_horizon_immediate_triggers_enabled = bool(immediate_cfg.get("enabled", False))
        self.rolling_horizon_immediate_queue_policy = (
            str(immediate_cfg.get("queue_policy", "pool_only")).strip().lower() or "pool_only"
        )
        self.rolling_horizon_immediate_protect_from_window_requeue = bool(
            immediate_cfg.get("protect_from_window_requeue", self.rolling_horizon_immediate_queue_policy == "next_after_current")
        )
        default_immediate_task_codes = {"worker_low_battery": ["MANAGE_ROBOT_POWER"]}
        raw_immediate_task_codes = immediate_cfg.get("event_task_codes", default_immediate_task_codes)
        if not isinstance(raw_immediate_task_codes, dict):
            raw_immediate_task_codes = default_immediate_task_codes
        self.rolling_horizon_immediate_event_task_codes: dict[str, set[str]] = {}
        for event_name, values in raw_immediate_task_codes.items():
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                continue
            codes = {str(value or "").strip().upper() for value in values if str(value or "").strip()}
            if codes:
                self.rolling_horizon_immediate_event_task_codes[str(event_name or "").strip().lower()] = codes

        self.rolling_horizon_window_index = 0
        self.rolling_horizon_window_start_min = 0.0
        self.rolling_horizon_window_end_min = self.rolling_horizon_window_min
        self.rolling_horizon_logged_window_index = -1
        self.rolling_horizon_pending: dict[str, dict[str, Any]] = {}
        self.rolling_horizon_pending_resource_index: dict[str, str] = {}
        self.rolling_horizon_dispatch_queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        # Workers are constructed after decision configuration is parsed. The
        # per-worker wake-up events are created once worker IDs are available.
        self.rolling_horizon_dispatch_events: dict[str, simpy.Event] = {}
        self.rolling_horizon_candidate_refresh_scheduled = False
        self.rolling_horizon_candidate_refresh_triggers: set[str] = set()
        self.rolling_horizon_metrics: dict[str, int] = {
            "started_window_count": 0,
            "window_count": 0,
            "candidate_collected_count": 0,
            "dispatched_task_count": 0,
            "stale_skipped_task_count": 0,
            "empty_window_count": 0,
            "requeued_task_count": 0,
            "max_worker_queue_length": 0,
            "immediate_dispatched_task_count": 0,
            "throughput_optimizer_window_count": 0,
            "throughput_optimizer_solved_count": 0,
            "throughput_optimizer_failed_count": 0,
            "strict_boundary_count": 0,
            "late_boundary_count": 0,
        }
        self.rolling_horizon_max_boundary_lag_min = 0.0
        self.throughput_optimizer_objective_values: list[float] = []
        self.throughput_score_values: list[float] = []
        self.rolling_horizon_max_queue_length_by_worker: dict[str, int] = defaultdict(int)
        self.rolling_horizon_dedicated_role_metrics: dict[str, Any] = {
            "role_violation_count": 0,
            "handover_dispatch_count": 0,
            "battery_delivery_from_provider_count": 0,
            "collected_by_worker": defaultdict(int),
            "dispatched_by_worker": defaultdict(int),
            "skipped_by_worker": defaultdict(int),
        }

    def _init_mfg_flow_task_policy(self, decision_cfg: dict[str, Any]) -> None:
        if not self.is_mfg_flow_shop:
            return
        if self.decision_mode not in MFG_FLOW_SHOP_POLICY_MODES:
            supported = ", ".join(sorted(MFG_FLOW_SHOP_POLICY_MODES))
            raise ValueError(
                f"mfg_flow_shop supports only its four representative decision modes: {supported}. "
                f"Received: {self.decision_mode}."
            )
        policy_cfg = (
            decision_cfg.get("mfg_flow_shop_policy", {})
            if isinstance(decision_cfg.get("mfg_flow_shop_policy", {}), dict)
            else {}
        )
        self.mfg_flow_task_policy = MfgFlowShopTaskPolicy(
            world=self,
            decision_mode=self.decision_mode,
            cfg=policy_cfg,
            worker_ids=self.workers,
        )
        summary = self.mfg_flow_task_policy.summary()
        if self.mfg_flow_task_policy.dedicated:
            self.rolling_horizon_worker_task_priority = {
                worker_id: list(payload.get("task_codes", []))
                for worker_id, payload in summary.get("workers", {}).items()
                if isinstance(payload, dict)
            }
            self.rolling_horizon_worker_task_rank = {
                worker_id: {code: index + 1 for index, code in enumerate(codes)}
                for worker_id, codes in self.rolling_horizon_worker_task_priority.items()
            }

    def _rule(self, dotted_path: str, default: Any) -> Any:
        node: Any = self.heuristic_rules
        for key in dotted_path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def resolve_primitive_duration(self, task: Task, step: dict[str, Any]) -> float:
        task_code = str(task.task_code or task.task_type).strip().upper()
        step_path = str(step.get("path", "")).strip()
        cache_key = (str(task.task_id), step_path)
        if cache_key in self.primitive_duration_cache:
            return self.primitive_duration_cache[cache_key]
        if task_code == "REPAIR_MACHINE" and str(step.get("call_code", "")).strip().upper() == "EXECUTE_MAINTENANCE_ACTION":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is not None and machine.repair_sampled_work_min > 0.0:
                duration = float(machine.repair_sampled_work_min)
            else:
                duration = self.timing.sample_step_duration(
                    task_code,
                    step_path,
                    sample_key=f"{task.task_id}:{step_path}",
                )
        else:
            duration = self.timing.sample_step_duration(
                task_code,
                step_path,
                sample_key=f"{task.task_id}:{step_path}",
            )
        self.primitive_duration_cache[cache_key] = duration
        return duration

    @staticmethod
    def active_primitive_duration(task: Task, fallback: float = 0.0) -> float:
        timing = task.payload.get("_active_primitive_timing", {}) if isinstance(task.payload, dict) else {}
        if not isinstance(timing, dict):
            return max(0.0, float(fallback))
        return max(0.0, float(timing.get("sampled_duration_min", fallback) or fallback))

    def _station_requires_intermediate(self, station: int) -> bool:
        # First stage is material-only; later stages require material + intermediate.
        return station >= 2

    def _build_machines(self) -> None:
        for station in self.stations:
            for idx in range(1, self.machines_per_station + 1):
                machine_id = f"S{station}M{idx}"
                machine = Machine(
                    machine_id=machine_id,
                    station=station,
                    process_time_min=float(self.processing_time_min[station]),
                    last_pm_at=0.0,
                )
                self.machines[machine_id] = machine
                self.machines_by_station[station].append(machine_id)

    def _build_workers(self) -> None:
        for idx in range(1, self.num_workers + 1):
            worker_id = f"A{idx}"
            worker = Worker(worker_id=worker_id, location="Home")
            worker.humanoid_state = default_humanoid_state_payload(worker_id)
            if self.grid_map is not None:
                worker.tile = self.grid_map.register_worker(worker_id, self.grid_map.initial_worker_tile(worker_id))
                initial_zone = self.grid_map.zone_for_tile(worker.tile)
                if initial_zone:
                    worker.location = initial_zone
            self.workers[worker_id] = worker

    @property
    def minimize_makespan_enabled(self) -> bool:
        return self.is_mfg_flow_shop and self.objective_mode == "minimize_makespan"

    @property
    def maximize_throughput_enabled(self) -> bool:
        return self.is_mfg_flow_shop and self.objective_mode == "maximize_throughput"

    def objective_metadata(self) -> dict[str, Any]:
        return {
            "objective_mode": self.objective_mode,
            "configured_max_sim_days": int(self.objective.get("max_sim_days", self.num_days) or self.num_days),
            "configured_throughput_days": int(self.configured_throughput_days),
            "objective_run_day_limit": int(self.num_days),
            "objective_terminal_policy": str(self.objective.get("terminal_policy", "")),
            "throughput_restock_interval_days": int(self.throughput_restock_interval_days),
            "throughput_restock_target_fill": int(self.throughput_restock_target_fill),
        }

    def _objective_reporting_days(self) -> float:
        if self.minimize_makespan_enabled:
            return max(1e-9, float(self.env.now) / max(1.0, float(self.minutes_per_day)))
        return float(max(1, self.configured_throughput_days if self.is_mfg_flow_shop else self.num_days))

    def _validate_initial_makespan_batch(self) -> None:
        if not self.minimize_makespan_enabled:
            return
        initial_inventory_cfg = self.cfg.get("initial_inventory", {})
        initial_material_cfg = initial_inventory_cfg.get("material", {}) if isinstance(initial_inventory_cfg, dict) else {}
        nonzero_station_inventory = {
            f"station{station}": int(initial_material_cfg.get(f"station{station}", 0) or 0)
            for station in self.stations
            if int(initial_material_cfg.get(f"station{station}", 0) or 0) != 0
        }
        if nonzero_station_inventory:
            raise ValueError(
                "minimize_makespan requires initial_inventory.material at every station to be zero; "
                f"found {nonzero_station_inventory}."
            )
        if self.material_shelf_initial_fill <= 0:
            raise ValueError("minimize_makespan requires warehouse.material_shelf.initial_fill to be greater than zero.")
        material_units_per_output = max(1, len(self.stations))
        if self.material_shelf_initial_fill % material_units_per_output != 0:
            raise ValueError(
                "warehouse.material_shelf.initial_fill must be divisible by the number of material-consuming "
                f"stages ({material_units_per_output}) for minimize_makespan."
            )

    def _register_initial_material_batch(self) -> None:
        if not self.minimize_makespan_enabled:
            return
        self.initial_batch_material_ids = {
            str(slot.get("material_item_id"))
            for slot in self.warehouse_material_shelf_slots.values()
            if str(slot.get("material_item_id") or "").strip()
        }
        sorted_material_ids = sorted(self.initial_batch_material_ids)
        sorted_stations = sorted(self.stations)
        self.initial_batch_material_station = {
            item_id: sorted_stations[index % len(sorted_stations)]
            for index, item_id in enumerate(sorted_material_ids)
        }
        station_material_ids = {
            station: [
                item_id
                for item_id in sorted_material_ids
                if self.initial_batch_material_station.get(item_id) == station
            ]
            for station in sorted_stations
        }
        for item_id in self.initial_batch_material_ids:
            item = self.items.get(item_id)
            if item is not None:
                item.metadata["initial_makespan_batch"] = True
                item.metadata["initial_batch_target_station"] = self.initial_batch_material_station[item_id]
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="INITIAL_MATERIAL_BATCH_REGISTERED",
            entity_id="warehouse_material_shelf",
            location="Warehouse",
            details={
                "material_ids": sorted(self.initial_batch_material_ids),
                "material_count": len(self.initial_batch_material_ids),
                "expected_output_count": self.initial_batch_expected_output_count(),
                "terminal_policy": str(self.objective.get("terminal_policy", "")),
                "station_material_ids": station_material_ids,
                "station_material_quota": {
                    str(station): len(material_ids)
                    for station, material_ids in station_material_ids.items()
                },
            },
        )

    def initial_batch_expected_output_count(self) -> int:
        return len(self.initial_batch_material_ids) // max(1, len(self.stations))

    def prepare_objective_day(self, day: int) -> None:
        normalized_day = max(1, int(day))
        if not self.is_mfg_flow_shop or normalized_day in self.objective_inventory_prepared_days:
            return
        self.objective_inventory_prepared_days.add(normalized_day)
        if (
            self.maximize_throughput_enabled
            and normalized_day > 1
            and (normalized_day - 1) % self.throughput_restock_interval_days == 0
        ):
            self._restock_material_shelf(
                reason="throughput_day_boundary",
                target_fill=self.throughput_restock_target_fill,
            )

    def bootstrap(self) -> None:
        from manufacturing_sim.simulation.scenarios.manufacturing import processes

        self._validate_initial_makespan_batch()
        self._ensure_material_shelf_slots()
        self._restock_material_shelf(reason="initial_fill", target_fill=self.material_shelf_initial_fill)
        self._register_initial_material_batch()

        initial_inventory_cfg = self.cfg.get("initial_inventory", {})
        initial_material_cfg = initial_inventory_cfg.get("material", {}) if isinstance(initial_inventory_cfg, dict) else {}
        for station in self.stations:
            initial_material = int(initial_material_cfg.get(f"station{station}", 0))
            for _ in range(max(0, initial_material)):
                self._warehouse_push_material(station)

        for machine_id in self.machines:
            self.initialize_machine_failure_exposure(self.machines[machine_id])
            self.env.process(processes.machine_lifecycle(self.env, self, machine_id))
            if self.machine_failure_time_basis != "active_processing":
                self.env.process(processes.machine_failure_monitor(self.env, self, machine_id))

        if self._rolling_horizon_active():
            self.env.process(strict_periodic_rolling_horizon_loop(self.env, self))
        if self._adp_active():
            self.adp_coordinator.start()

        for agent_id in self.agents:
            self.env.process(processes.agent_work_loop(self.env, self, agent_id))
            self.env.process(processes.agent_battery_monitor(self.env, self, agent_id))

        self.env.process(processes.snapshot_loop(self.env, self))

    def day_for_time(self, t: float) -> int:
        computed = int(float(t) // self.minutes_per_day) + 1
        return max(1, min(int(self.num_days), computed))

    def start_day(self, day: int, strategy: StrategyState, job_plan: JobPlan) -> None:
        self.current_day = day
        self.current_strategy = strategy
        self._recover_depleted_workers_at_day_start(day)
        self.prepare_objective_day(day)
        if not self.is_mfg_flow_shop and self.material_shelf_restock_policy == "day_boundary":
            self._restock_material_shelf(reason="day_boundary")
        job_plan.ensure_runtime_context(tuple(sorted(self.agents.keys())))
        self.current_job_plan = job_plan
        self._materialize_commitments()
        self.selection_blockers = {}
        self.active_selection_blocker_by_agent = {}
        self.incident_escalations = set()
        self.day_unique_replan_blockers = set()
        self.day_planner_escalations = set()
        self.day_baseline = {
            "products": self.product_count,
            "scrap": self.scrap_count,
            "machine_processing": {mid: m.total_processing_min for mid, m in self.machines.items()},
            "machine_broken": {mid: m.total_broken_min for mid, m in self.machines.items()},
            "machine_pm": {mid: m.total_pm_min for mid, m in self.machines.items()},
            "task_count": len(self.task_records),
            "disposed_scrap": int(self.disposed_scrap_count),
            "warehouse_material_restock": int(self.warehouse_material_restock_count),
            "scrap_transport_batches": int(self.scrap_transport_batches),
            "scrap_transport_items": int(self.scrap_transport_items),
        }
        self.logger.log(
            t=self.env.now,
            day=day,
            event_type="PHASE_STRATEGY",
            entity_id="system",
            location="Home",
            details={"notes": strategy.notes},
        )
        self.logger.log(
            t=self.env.now,
            day=day,
            event_type="PHASE_JOB_ASSIGNMENT",
            entity_id="system",
            location="CoordinationReview",
            details={
                "task_priority_weights": self._current_shared_task_priority_weights(),
                "shared_task_priority_weights": self._current_shared_task_priority_weights(),
                "agent_priority_multipliers": {
                    agent_id: self.current_agent_priority_multipliers(agent_id)
                    for agent_id in sorted(self.agents.keys())
                },
                "agent_effective_task_priority_weights": {
                    agent_id: self.current_effective_task_priority_weights(agent_id)
                    for agent_id in sorted(self.agents.keys())
                },
                "agent_task_allowlists": dict(job_plan.agent_task_allowlists),
                "quotas": job_plan.quotas,
                "agent_roles": dict(job_plan.agent_roles),
                "personal_queues": dict(job_plan.personal_queues),
                "incident_work_orders": dict(job_plan.incident_work_orders),
                "mailbox": dict(job_plan.mailbox),
                "commitments": dict(self.current_job_plan.commitments),
                "parallel_groups": list(job_plan.parallel_groups),
                "reason_trace": list(job_plan.reason_trace),
                "manager_summary": str(job_plan.manager_summary or ""),
            },
        )

    def current_agent_priority_multipliers(self, agent_id: str) -> dict[str, float]:
        self.current_job_plan.ensure_agent_priority_multipliers(tuple(sorted(self.agents.keys())))
        values = dict(self.current_job_plan.agent_priority_multipliers.get(str(agent_id), {}))
        return {key: float(values.get(key, 1.0)) for key in self._scenario_priority_keys()}

    def current_effective_task_priority_weights(self, agent_id: str) -> dict[str, float]:
        self.current_job_plan.ensure_agent_priority_multipliers(tuple(sorted(self.agents.keys())))
        values = self.current_job_plan.effective_task_priority_weights(str(agent_id))
        multipliers = self.current_job_plan.agent_priority_multipliers.get(str(agent_id), {})
        return {
            key: float(
                values.get(
                    key,
                    float(self.current_job_plan.task_priority_weights.get(key, 1.0))
                    * float(multipliers.get(key, 1.0)),
                )
            )
            for key in self._scenario_priority_keys()
        }

    def _scenario_priority_keys(self) -> list[str]:
        if not self.is_mfg_flow_shop:
            return sorted(set(default_task_priority_weights().keys()) | {"battery_charge"})
        return [
            "battery_charge",
            "inspect_product",
            "inter_station_transfer",
            "load_machine",
            "material_supply",
            "preventive_maintenance",
            "repair_machine",
            "scrap_disposal",
            "setup_machine",
            "unload_machine",
        ]

    def _current_shared_task_priority_weights(self) -> dict[str, float]:
        values = self.current_job_plan.task_priority_weights or {}
        return {key: float(values.get(key, 1.0)) for key in self._scenario_priority_keys()}

    def _agent_priority_profile_summary(
        self,
        *,
        include_effective: bool = False,
        top_n: int = 2,
        include_full: bool = False,
        agent_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        scope = [str(agent_id) for agent_id in (agent_ids or sorted(self.agents.keys())) if str(agent_id) in self.agents]
        for agent_id in scope:
            values = (
                self.current_effective_task_priority_weights(agent_id)
                if include_effective
                else self.current_agent_priority_multipliers(agent_id)
            )
            ranked = sorted(
                values.items(),
                key=lambda item: abs(float(item[1]) - (1.0 if not include_effective else float(self.current_job_plan.task_priority_weights.get(item[0], 1.0)))),
                reverse=True,
            )
            entry = {
                "top_biases": [
                    {"priority_key": key, "value": round(float(value), 3)}
                    for key, value in ranked[:top_n]
                ],
            }
            if include_full:
                entry["full"] = {key: round(float(value), 3) for key, value in values.items()}
            summary[agent_id] = entry
        return summary

    def _observation_day(self) -> int:
        return max(1, min(self.num_days, int(self.day_for_time(self.env.now))))

    def _observation_time_block(self, observation_day: int) -> dict[str, Any]:
        day_start = (observation_day - 1) * self.minutes_per_day
        day_end = observation_day * self.minutes_per_day
        day_elapsed = min(float(self.minutes_per_day), max(0.0, float(self.env.now) - float(day_start)))
        days_remaining = max(0, int(self.num_days) - int(observation_day))
        horizon_total_min = int(self.num_days * self.minutes_per_day)
        horizon_elapsed_min = min(float(horizon_total_min), max(0.0, float(self.env.now)))
        horizon_remaining_min = max(0.0, float(horizon_total_min) - horizon_elapsed_min)
        return {
            "sim_min": round(float(self.env.now), 3),
            "day": observation_day,
            "total_days": int(self.num_days),
            "days_remaining": days_remaining,
            "minutes_per_day": int(self.minutes_per_day),
            "horizon_total_min": horizon_total_min,
            "horizon_remaining_min": round(horizon_remaining_min, 3),
            "day_start_min": round(float(day_start), 3),
            "day_end_min": round(float(day_end), 3),
            "day_elapsed_min": round(day_elapsed, 3),
            "day_progress": round(day_elapsed / max(1.0, float(self.minutes_per_day)), 4),
        }

    def _observation_queues(self) -> dict[str, Any]:
        material = {f"station{station}_input": len(self.material_queues[station]) for station in self.stations}
        intermediate = {
            f"station{station}_input": len(self.intermediate_queues[station])
            for station in self.stations
            if station in self.intermediate_queues
        }
        output_buffers = {f"station{station}_output_buffer": len(self.output_buffers[station]) for station in self.stations}
        inspection = {
            "inspection_input": len(self.intermediate_queues[self.inspection_queue_station]),
            "inspection_pass_output": len(self.output_buffers[self.inspection_queue_station]),
        }
        return {
            "material": material,
            "intermediate": intermediate,
            "output_buffers": output_buffers,
            "inspection": inspection,
        }


    @staticmethod
    def _empty_machine_state_counts() -> dict[str, int]:
        return {
            "total": 0,
            "wait_input": 0,
            "processing": 0,
            "finished_wait_unload": 0,
            "broken": 0,
            "under_repair": 0,
            "under_pm": 0,
        }

    @staticmethod
    def _empty_wait_reason_counts() -> dict[str, int]:
        return {
            "missing_material": 0,
            "missing_intermediate_input": 0,
            "waiting_unload": 0,
            "ready_for_setup": 0,
            "broken": 0,
            "under_repair": 0,
            "under_pm": 0,
        }

    def _observed_machine_state(self, machine: Machine) -> MachineState:
        """Return the state dashboards/KPIs should observe for transient repair.

        The domain keeps ``machine.broken`` true while repair work is active so
        downstream logic still knows the machine cannot process. For monitoring,
        KPI, and replay snapshots, a machine with an active repair team should
        be counted as UNDER_REPAIR instead of BROKEN.
        """
        if machine.state == MachineState.BROKEN and self._repair_team_size(machine) > 0:
            return MachineState.UNDER_REPAIR
        return machine.state

    def _machine_state_bucket(self, machine: Machine) -> str:
        observed_state = self._observed_machine_state(machine)
        if observed_state == MachineState.PROCESSING:
            return "processing"
        if observed_state == MachineState.DONE_WAIT_UNLOAD:
            return "finished_wait_unload"
        if observed_state == MachineState.BROKEN:
            return "broken"
        if observed_state == MachineState.UNDER_REPAIR:
            return "under_repair"
        if observed_state == MachineState.UNDER_PM:
            return "under_pm"
        return "wait_input"

    def _machine_wait_reasons(self, machine: Machine) -> list[str]:
        reasons: list[str] = []
        observed_state = self._observed_machine_state(machine)
        if observed_state == MachineState.BROKEN:
            reasons.append("broken")
        if observed_state == MachineState.UNDER_REPAIR:
            reasons.append("under_repair")
        if observed_state == MachineState.UNDER_PM:
            reasons.append("under_pm")
        if machine.output_intermediate is not None or observed_state == MachineState.DONE_WAIT_UNLOAD:
            reasons.append("waiting_unload")
        if observed_state == MachineState.WAIT_INPUT:
            has_material = machine.input_material is not None or len(self.material_queues[machine.station]) > 0
            if not has_material:
                reasons.append("missing_material")
            if self._station_requires_intermediate(machine.station):
                has_intermediate = machine.input_intermediate is not None or len(self.intermediate_queues[machine.station]) > 0
                if not has_intermediate:
                    reasons.append("missing_intermediate_input")
            if not reasons:
                reasons.append("ready_for_setup")
        return reasons

    def _machine_observation(self) -> dict[str, Any]:
        summary: dict[str, dict[str, int]] = {
            f"station{station}": self._empty_machine_state_counts() for station in self.stations
        }
        wait_reason_summary: dict[str, dict[str, int]] = {
            f"station{station}": self._empty_wait_reason_counts() for station in self.stations
        }
        overall = self._empty_machine_state_counts()
        overall_wait_reasons = self._empty_wait_reason_counts()
        by_id: dict[str, Any] = {}
        for machine_id in sorted(self.machines.keys()):
            machine = self.machines[machine_id]
            bucket = self._machine_state_bucket(machine)
            station_key = f"station{machine.station}"
            summary[station_key]["total"] += 1
            summary[station_key][bucket] += 1
            overall["total"] += 1
            overall[bucket] += 1
            intermediate_available: bool | None = None
            if self._station_requires_intermediate(machine.station):
                intermediate_available = bool(machine.input_intermediate is not None or len(self.intermediate_queues[machine.station]) > 0)
            wait_reasons = self._machine_wait_reasons(machine)
            for reason in wait_reasons:
                if reason in wait_reason_summary[station_key]:
                    wait_reason_summary[station_key][reason] += 1
                    overall_wait_reasons[reason] += 1
            by_id[machine_id] = {
                "station": f"Station{machine.station}",
                "station_index": int(machine.station),
                "state": self._observed_machine_state(machine).value,
                "broken": bool(machine.broken),
                "has_output_waiting_unload": bool(machine.output_intermediate is not None),
                "material_available_now": bool(machine.input_material is not None or len(self.material_queues[machine.station]) > 0),
                "intermediate_available_now": intermediate_available,
                "minutes_since_last_pm": round(max(0.0, float(self.env.now) - float(machine.last_pm_at)), 3),
                "minutes_since_failure_started": None if machine.failed_since is None else round(max(0.0, float(self.env.now) - float(machine.failed_since)), 3),
                "owners": {
                    "repair": machine.repair_owner,
                    "setup": machine.setup_owner,
                    "unload": machine.unload_owner,
                    "preventive_maintenance": machine.pm_owner,
                },
                "repair_team": list(machine.repair_team),
                "repair_team_size": self._repair_team_size(machine),
                "repair_slots_remaining": self._repair_slots_remaining(machine),
                "repair_remaining_min": round(float(machine.repair_work_remaining_min), 3) if machine.broken else 0.0,
                "wait_reasons": wait_reasons,
            }
        summary["all"] = overall
        wait_reason_summary["all"] = overall_wait_reasons
        return {"summary": summary, "wait_reason_summary": wait_reason_summary, "by_id": by_id}

    def _agent_status_label(self, agent: Agent) -> str:
        if agent.discharged:
            return "DISCHARGED"
        if self._has_in_transit_position(agent):
            return "MOVING"
        if agent.current_task_type:
            return "WORKING"
        return "IDLE"

    def _agent_observation(self) -> dict[str, Any]:
        summary = {
            "total": 0,
            "idle": 0,
            "working": 0,
            "moving": 0,
            "discharged": 0,
            "awaiting_battery": 0,
            "low_battery": 0,
        }
        by_id: dict[str, Any] = {}
        for agent_id in sorted(self.agents.keys()):
            agent = self.agents[agent_id]
            battery_remaining = round(float(self.battery_remaining(agent)), 3)
            status = self._agent_status_label(agent)
            low_battery = bool((not agent.discharged) and battery_remaining <= self._battery_low_alert_threshold(agent))
            summary["total"] += 1
            if status == "IDLE":
                summary["idle"] += 1
            elif status == "WORKING":
                summary["working"] += 1
            elif status == "MOVING":
                summary["moving"] += 1
            elif status == "DISCHARGED":
                summary["discharged"] += 1
            if agent.awaiting_battery_from is not None:
                summary["awaiting_battery"] += 1
            if low_battery:
                summary["low_battery"] += 1
            in_transit = None
            if agent.in_transit_from and agent.in_transit_to and float(agent.in_transit_total_min) > 0.0:
                in_transit = {
                    "from": str(agent.in_transit_from),
                    "to": str(agent.in_transit_to),
                    "progress": round(float(agent.in_transit_progress), 4),
                    "total_travel_min": round(float(agent.in_transit_total_min), 3),
                }
            by_id[agent_id] = {
                "location": self.agent_display_location(agent),
                "status": status,
                "battery_remaining_min": battery_remaining,
                "low_battery": low_battery,
                "discharged": bool(agent.discharged),
                "awaiting_battery_from": agent.awaiting_battery_from,
                "battery_service_owner": agent.battery_service_owner,
                "current_task_type": agent.current_task_type,
                "current_commitment_id": agent.current_commitment_id,
                "carrying_item_type": agent.carrying_item_type,
                "suspended_task_type": agent.suspended_task.task_type if isinstance(agent.suspended_task, Task) else None,
                "incident_backlog_count": len(agent.incident_backlog) if isinstance(agent.incident_backlog, list) else 0,
                "in_transit": in_transit,
            }
        return {"summary": summary, "by_id": by_id}


    def _flow_observation(self) -> dict[str, Any]:
        machines_waiting_unload = {
            f"station{station}": sum(
                1
                for machine in self.machines.values()
                if machine.station == station and (machine.output_intermediate is not None or machine.state == MachineState.DONE_WAIT_UNLOAD)
            )
            for station in self.stations
        }
        output_waiting_transfer = {
            f"station{station}_output_buffer": len(self.output_buffers[station]) for station in self.stations
        }
        return {
            "output_waiting_transfer": output_waiting_transfer,
            "machines_waiting_unload": machines_waiting_unload,
            "broken_machine_count": sum(1 for machine in self.machines.values() if machine.broken),
            "active_inspection_agents": int(self.inspection_active_agents),
            "inspection_owner": self.inspection_owner,
            "products_completed_total": int(self.product_count),
            "scrap_total": int(self.scrap_count),
        }

    def _recent_history_observation(self, last_day_summary: dict[str, Any] | None) -> dict[str, Any]:
        summary = last_day_summary if isinstance(last_day_summary, dict) else {}
        return {
            "last_day_products": int(summary.get("products", 0)),
            "last_day_scrap": int(summary.get("scrap", 0)),
            "last_day_scrap_rate": float(summary.get("scrap_rate", 0.0)),
            "last_day_machine_breakdowns": int(summary.get("machine_breakdowns", 0)),
            "last_day_avg_wip_material": float(summary.get("avg_wip_material", 0.0)),
            "last_day_avg_wip_intermediate": float(summary.get("avg_wip_intermediate", 0.0)),
        }


    def _observation_trends(self, window_min: float = 60.0) -> dict[str, Any]:
        end_t = float(self.env.now)
        start_t = max(0.0, end_t - float(window_min))
        snapshots = [s for s in self.minute_snapshots if float(s.get("t", 0.0)) >= start_t]
        queue_delta: dict[str, int] = {}
        if snapshots:
            first = snapshots[0]
            last = snapshots[-1]
            for station in self.stations:
                queue_delta[f"material_station{station}_input"] = int(last["material_queue_lengths"].get(station, 0)) - int(first["material_queue_lengths"].get(station, 0))
                queue_delta[f"station{station}_output_buffer"] = int(last["output_buffer_lengths"].get(station, 0)) - int(first["output_buffer_lengths"].get(station, 0))
                if station in self.intermediate_queues:
                    queue_delta[f"intermediate_station{station}_input"] = int(last["intermediate_queue_lengths"].get(station, 0)) - int(first["intermediate_queue_lengths"].get(station, 0))
            queue_delta["inspection_input"] = int(last["intermediate_queue_lengths"].get(self.inspection_queue_station, 0)) - int(first["intermediate_queue_lengths"].get(self.inspection_queue_station, 0))
            queue_delta["inspection_pass_output"] = int(last["output_buffer_lengths"].get(self.inspection_queue_station, 0)) - int(first["output_buffer_lengths"].get(self.inspection_queue_station, 0))
        recent_events = [event for event in self.logger.events if float(event.get("t", 0.0)) >= start_t]
        stage_completions_last_window = {f"station{station}": 0 for station in self.stations}
        stage_completions_last_window["inspection_pass"] = 0
        stage_completions_last_window["inspection_fail"] = 0
        for event in recent_events:
            event_type = str(event.get("type", "")).strip()
            if event_type == "MACHINE_END":
                location = str(event.get("location", "")).strip()
                if location.startswith("Station"):
                    suffix = location.removeprefix("Station")
                    if suffix.isdigit():
                        station_key = f"station{int(suffix)}"
                        if station_key in stage_completions_last_window:
                            stage_completions_last_window[station_key] += 1
            elif event_type == "INSPECT_PASS":
                stage_completions_last_window["inspection_pass"] += 1
            elif event_type == "INSPECT_FAIL":
                stage_completions_last_window["inspection_fail"] += 1
        return {
            "window_min": int(window_min),
            "queue_delta": queue_delta,
            "stage_completions_last_window": stage_completions_last_window,
            "completed_products_last_window": sum(1 for event in recent_events if event.get("type") == "COMPLETED_PRODUCT"),
            "scrap_last_window": sum(1 for event in recent_events if event.get("type") == "SCRAP"),
            "machine_breakdowns_last_window": sum(1 for event in recent_events if event.get("type") == "MACHINE_BROKEN"),
        }

    def build_observation(self, last_day_summary: dict[str, Any] | None) -> dict[str, Any]:
        observation_day = self._observation_day()
        queues = self._observation_queues()
        recent_history = self._recent_history_observation(last_day_summary)
        commitments = self.current_commitments()
        incident_work_orders = self.current_incident_work_orders()
        opportunities = self.enumerate_opportunities()
        incident_context = {
            "recent_incidents": list(self.incident_events[-10:]),
            "active_blockers": self.active_selection_blockers(),
        }
        return {
            "t": round(float(self.env.now), 3),
            "day": observation_day,
            "time": self._observation_time_block(observation_day),
            "queues": queues,
            "machines": self._machine_observation(),
            "agents": self._agent_observation(),
            "flow": self._flow_observation(),
            "recent_history": recent_history,
            "trends": self._observation_trends(),
            # 규칙 기반 planner와 일부 로깅 경로가 아직 참조하는 호환 필드다. LLM 프롬프트에는 별도 compact view만 전달된다.
            "inspection_backlog": queues["inspection"]["inspection_input"],
            "machine_states": {mid: self._observed_machine_state(machine).value for mid, machine in self.machines.items()},
            "last_day_machine_breaks": int(recent_history["last_day_machine_breakdowns"]),
            "last_day_scrap_rate": float(recent_history["last_day_scrap_rate"]),
            "commitments": commitments,
            "active_commitments": commitments,
            "incident_work_orders": incident_work_orders,
            "active_incident_work_orders": incident_work_orders,
            "opportunities": opportunities,
            "incidents": list(self.incident_events[-10:]),
            "incident_context": incident_context,
        }

    def observe(self) -> dict[str, Any]:
        last_day_summary = self.daily_summaries[-1] if self.daily_summaries else None
        return self.build_observation(last_day_summary)

    def snapshot(self) -> dict[str, Any]:
        self.capture_snapshot()
        return dict(self.minute_snapshots[-1]) if self.minute_snapshots else {}

    def _next_incident_id(self) -> str:
        return f"INC-{next(self.incident_counter):05d}"

    def current_commitments(self) -> dict[str, list[dict[str, Any]]]:
        commitments = self.current_job_plan.commitments if isinstance(self.current_job_plan.commitments, dict) else {}
        return {
            str(agent_id): [dict(item) for item in rows if isinstance(item, dict)]
            for agent_id, rows in commitments.items()
            if isinstance(rows, list)
        }

    def current_incident_work_orders(self) -> dict[str, list[dict[str, Any]]]:
        work_orders = self.current_job_plan.incident_work_orders if isinstance(self.current_job_plan.incident_work_orders, dict) else {}
        return {
            str(agent_id): [dict(item) for item in rows if isinstance(item, dict)]
            for agent_id, rows in work_orders.items()
            if isinstance(rows, list)
        }

    def active_selection_blockers(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for blocker in self.selection_blockers.values():
            if not isinstance(blocker, dict):
                continue
            if str(blocker.get("status", "active")).strip().lower() != "active":
                continue
            rows.append(dict(blocker))
        rows.sort(key=lambda item: (float(item.get("created_at_min", 0.0) or 0.0), str(item.get("blocker_id", ""))))
        return rows

    def _active_plan_revision(self) -> int:
        return max(0, int(getattr(self.current_job_plan, "plan_revision", 0) or 0))

    def _current_opportunity_ids(self) -> set[str]:
        rows = self.enumerate_opportunities()
        return {
            str(item.get("opportunity_id", "")).strip()
            for item in rows
            if isinstance(item, dict) and str(item.get("opportunity_id", "")).strip()
        }

    def _build_commitment_target(self, *, target_type: str, target_id: str, target_station: int | None) -> dict[str, Any]:
        return OpportunityTarget(
            target_type=str(target_type or "none"),
            target_id=str(target_id or ""),
            target_station=int(target_station) if target_station is not None else None,
        ).to_dict()

    def _synthesize_commitments_from_queue(self) -> dict[str, list[dict[str, Any]]]:
        synthesized: dict[str, list[dict[str, Any]]] = {}
        queues = self.current_job_plan.personal_queues if isinstance(self.current_job_plan.personal_queues, dict) else {}
        for agent_id, rows in queues.items():
            if not isinstance(rows, list):
                continue
            commitments: list[dict[str, Any]] = []
            for idx, order in enumerate(rows[: self.worker_queue_limit]):
                if not isinstance(order, dict):
                    continue
                target_type = str(order.get("target_type", "none")).strip().lower() or "none"
                target_station = None
                if order.get("target_station") not in {None, ""}:
                    try:
                        target_station = int(order.get("target_station"))
                    except (TypeError, ValueError):
                        target_station = None
                commitment = Commitment(
                    commitment_id=f"{agent_id}-DAY{self.current_day:02d}-{idx + 1:02d}",
                    opportunity_id=f"LEGACY-{agent_id}-{idx + 1:02d}",
                    task_family=str(order.get("task_family", "")).strip(),
                    assigned_worker=str(agent_id),
                    target=OpportunityTarget(
                        target_type=target_type,
                        target_id=str(order.get("target_id", "")).strip(),
                        target_station=target_station,
                    ),
                    alternate_workers=[str(value).strip() for value in order.get("alternate_workers", []) if str(value).strip()] if isinstance(order.get("alternate_workers", []), list) else [],
                    dependencies=[str(value).strip() for value in order.get("dependency_ids", []) if str(value).strip()] if isinstance(order.get("dependency_ids", []), list) else [],
                    handoff_policy="allowed" if self.worker_local_response_cfg.get("allow_handoff", True) else "manager_only",
                    success_criteria=[str(order.get("reason", "")).strip()] if str(order.get("reason", "")).strip() else [],
                    rationale=str(order.get("reason", "")).strip(),
                    source="planner_queue",
                    metadata={"origin_order": dict(order)},
                )
                commitments.append(commitment.to_dict())
            synthesized[str(agent_id)] = commitments
        return synthesized

    def _materialize_commitments(self) -> None:
        self.current_job_plan.ensure_commitments(tuple(sorted(self.agents.keys())))

    def _push_agent_incident(self, agent_id: str, incident: dict[str, Any]) -> None:
        agent = self.agents.get(str(agent_id))
        if agent is None:
            return
        backlog = list(agent.incident_backlog)
        backlog.append(dict(incident))
        agent.incident_backlog = backlog[-8:]

    def emit_incident(
        self,
        incident_class: str,
        *,
        affected_entities: list[str] | None = None,
        blocked_commitments: list[str] | None = None,
        escalation_level: str = "worker_local",
        details: dict[str, Any] | None = None,
        notify_workers: list[str] | None = None,
    ) -> dict[str, Any]:
        event = IncidentEvent(
            incident_id=self._next_incident_id(),
            incident_class=str(incident_class).strip() or "incident",
            time_min=float(self.env.now),
            day=self.day_for_time(self.env.now),
            affected_entities=[str(value) for value in (affected_entities or []) if str(value).strip()],
            blocked_commitments=[str(value) for value in (blocked_commitments or []) if str(value).strip()],
            escalation_level=str(escalation_level).strip() or "worker_local",
            details=details or {},
        ).to_dict()
        self.incident_events.append(event)
        self.incident_events = self.incident_events[-50:]
        workers = notify_workers or []
        for agent_id in workers:
            self._push_agent_incident(str(agent_id), event)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="INCIDENT_EVENT",
            entity_id=event["incident_id"],
            location="Factory",
            details=event,
        )
        return event

    def _load_humanoid_incident_schema(self) -> Any | None:
        if not bool(getattr(self, "humanoid_incidents_enabled", True)):
            return None
        if getattr(self, "humanoid_incident_schema", None) is not None:
            return self.humanoid_incident_schema
        try:
            from humanoidsim import load_incident_schema
        except ModuleNotFoundError:
            return None
        self.humanoid_incident_schema = load_incident_schema()
        return self.humanoid_incident_schema

    def _build_humanoid_incident_transition_event(self, code: str, **kwargs: Any) -> Any | None:
        schema = self._load_humanoid_incident_schema()
        if schema is None:
            return None
        try:
            from humanoidsim import build_incident_transition_event
        except ModuleNotFoundError:
            return None
        try:
            return build_incident_transition_event(code, schema=schema, **kwargs)
        except KeyError:
            return None

    def _humanoid_incident_profile(self, code: str) -> Any | None:
        schema = self._load_humanoid_incident_schema()
        if schema is None:
            return None
        try:
            return schema.get(str(code))
        except KeyError:
            return None

    @staticmethod
    def _canonical_humanoid_incident_code(code: str) -> str:
        return str(code or "").strip().upper()

    @staticmethod
    def _normalize_humanoid_incident_cfg_keys(raw: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            canonical_key = ManufacturingWorld._canonical_humanoid_incident_code(str(key))
            if canonical_key:
                normalized[canonical_key] = value
        return normalized

    def _incident_recovery_payload(self, profile: Any | None) -> list[dict[str, Any]]:
        if profile is None:
            return []
        rows = []
        for step in getattr(profile, "recovery_protocol", []) or []:
            if hasattr(step, "to_dict"):
                rows.append(step.to_dict())
            elif isinstance(step, dict):
                rows.append(dict(step))
        return rows

    def _humanoid_incident_code_for_failure_reason(self, reason: str) -> str:
        raw_reason = str(reason or "").strip()
        profile = self._humanoid_incident_profile(raw_reason)
        if profile is not None:
            return str(getattr(profile, "code", "") or raw_reason).strip().upper()
        return self._canonical_humanoid_incident_code(raw_reason)

    def _humanoid_incident_metadata_for_reason(self, reason: str) -> tuple[str, dict[str, Any]]:
        incident_code = self._humanoid_incident_code_for_failure_reason(reason)
        profile = self._humanoid_incident_profile(incident_code)
        if profile is None:
            return str(reason or "").strip(), {}
        recovery_protocol = self._incident_recovery_payload(profile)
        retry_policy = getattr(getattr(profile, "retry_policy", None), "to_dict", lambda: {})()
        canonical_code = str(getattr(profile, "code", "") or incident_code).strip().upper()
        return canonical_code, {
            "incident_code": canonical_code,
            "incident_category": str(getattr(profile, "category", "") or "unknown"),
            "incident_severity": str(getattr(profile, "severity", "warning") or "warning"),
            "recovery_protocol": recovery_protocol,
            "retry_policy": retry_policy,
            "original_reason_code": str(reason or "").strip(),
        }

    def _humanoid_incident_random_options(self, code: str) -> dict[str, Any]:
        options = self.humanoid_incident_random_cfg.get(self._canonical_humanoid_incident_code(code), {})
        return options if isinstance(options, dict) else {}

    def _humanoid_incident_enabled(self, code: str) -> bool:
        if not self.humanoid_incidents_enabled:
            return False
        options = self._humanoid_incident_random_options(code)
        return bool(options.get("enabled", False))

    def _random_incident_probability(self, code: str, *, per_tile: bool = False) -> float:
        options = self._humanoid_incident_random_options(code)
        key = "probability_per_tile" if per_tile else "probability"
        try:
            probability = float(options.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, probability))

    def _random_incident_triggers(self, code: str) -> set[str]:
        options = self._humanoid_incident_random_options(code)
        raw = options.get("trigger_primitives", [])
        if isinstance(raw, list) and raw:
            return {str(value).strip().upper() for value in raw if str(value).strip()}
        profile = self._humanoid_incident_profile(code)
        if profile is None:
            return set()
        return {str(value).strip().upper() for value in getattr(profile, "trigger_primitives", []) if str(value).strip()}

    def _maybe_random_humanoid_step_incident(
        self,
        agent: Worker,
        task: Task,
        step: dict[str, Any],
        primitive_call_code: str,
    ) -> bool:
        primitive = str(primitive_call_code or "").strip().upper()
        if not primitive:
            return False
        for code in sorted(self.humanoid_incident_random_cfg.keys()):
            if code == "ITEM_DROPPED":
                continue
            if not self._humanoid_incident_enabled(code):
                continue
            triggers = self._random_incident_triggers(code)
            if "*" not in triggers and primitive not in triggers:
                continue
            probability = self._random_incident_probability(code)
            if probability <= 0.0 or self._humanoid_incident_random(agent.agent_id, code) >= probability:
                continue
            self._emit_humanoid_incident(
                agent,
                code,
                task=task,
                step=step,
                primitive_call_code=primitive,
                source="mansim.random_incident",
                context={"random_probability": probability, "trigger_primitive": primitive},
            )
            task.payload["failure_reason"] = code
            return True
        return False

    def _maybe_item_drop_incident(self, agent: Worker, *, logical_destination: str, destination: str, move_id: str) -> bool:
        if not agent.carrying_item_id or not self._humanoid_incident_enabled("ITEM_DROPPED"):
            return False
        metadata = (agent.humanoid_state or {}).get("metadata") if isinstance(agent.humanoid_state, dict) else None
        recovery_context = metadata.get("recovery_context") if isinstance(metadata, dict) else None
        active_recovery_context = getattr(agent, "active_recovery_context", None)
        if isinstance(active_recovery_context, dict) and bool(active_recovery_context.get("active", False)):
            return False
        if isinstance(recovery_context, dict) and bool(recovery_context.get("active", False)):
            return False
        probability = self._random_incident_probability("ITEM_DROPPED", per_tile=True)
        if probability <= 0.0 or self._humanoid_incident_random(agent.agent_id, "ITEM_DROPPED") >= probability:
            return False
        self._drop_agent_cargo_due_to_incident(agent, logical_destination=logical_destination, destination=destination, move_id=move_id)
        return True

    def _drop_agent_cargo_due_to_incident(self, agent: Worker, *, logical_destination: str, destination: str, move_id: str) -> None:
        item_id = str(agent.carrying_item_id or "")
        item_type = str(agent.carrying_item_type or "")
        tile_payload = self._tile_payload(agent.tile)
        self._emit_humanoid_incident(
            agent,
            "ITEM_DROPPED",
            primitive_call_code=str(agent.current_primitive_call_code or "NAVIGATE_TO"),
            source="mansim.random_incident",
            context={
                "item_id": item_id,
                "item_type": item_type,
                "move_id": move_id,
                "logical_destination": logical_destination,
                "destination": str(destination or logical_destination),
                "tile": tile_payload,
            },
        )
        if item_id:
            self._register_dropped_item(
                item_id,
                item_type=item_type,
                tile=agent.tile,
                dropped_by=agent.agent_id,
                destination=str(destination or logical_destination),
                logical_destination=logical_destination,
                move_id=move_id,
            )
            self._set_item_state(
                item_id,
                ItemState.DROPPED,
                location=self.agent_display_location(agent),
                ref=f"dropped_by:{agent.agent_id}",
                item_type=item_type or None,
                tile=agent.tile,
            )
            self._abort_product_transport_session_for_item(item_id, reason="ITEM_DROPPED", destination="dropped")
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="AGENT_DROP_ITEM",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={
                "item_id": item_id,
                "item_type": item_type,
                "reason": "ITEM_DROPPED",
                "move_id": move_id,
                "tile": tile_payload,
                "destination": str(destination or logical_destination),
                "humanoid_state": self._humanoid_state_payload(agent),
            },
        )
        self._set_worker_cargo(agent, None, None, destination="dropped")

    def _drop_agent_cargo_due_to_depletion(self, agent: Worker, task: Task) -> list[str]:
        item_ids = [
            str(item_id)
            for item_id in getattr(agent, "carrying_item_ids", [])
            if str(item_id).strip()
        ]
        if not item_ids and agent.carrying_item_id:
            item_ids = [str(agent.carrying_item_id)]
        if not item_ids:
            return []
        item_type = str(agent.carrying_item_type or "unknown")
        payload = task.payload if isinstance(task.payload, dict) else {}
        destination = str(
            payload.get("destination")
            or payload.get("destination_buffer_id")
            or task.location
            or ""
        )
        logical_destination = str(agent.current_move_logical_destination or destination)
        move_id = str(agent.current_move_id or "")
        for item_id in item_ids:
            self._register_dropped_item(
                item_id,
                item_type=item_type,
                tile=agent.tile,
                dropped_by=agent.agent_id,
                destination=destination,
                logical_destination=logical_destination,
                move_id=move_id,
            )
            self._set_item_state(
                item_id,
                ItemState.DROPPED,
                location=self.agent_display_location(agent),
                ref=f"battery_depleted:{agent.agent_id}",
                item_type=item_type,
                tile=agent.tile,
            )
            self._abort_product_transport_session_for_item(
                item_id,
                reason="battery_depleted",
                destination="dropped",
            )
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="WORKER_DEPLETION_CARGO_DROPPED",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={
                "task_id": str(task.task_id or ""),
                "item_ids": item_ids,
                "item_count": len(item_ids),
                "item_type": item_type,
                "tile": self._tile_payload(agent.tile),
                "destination": destination,
            },
        )
        self._set_worker_cargo(agent, None, None, destination="dropped")
        return item_ids

    def _emit_humanoid_incident(
        self,
        agent: Worker,
        code: str,
        *,
        task: Task | None = None,
        step: dict[str, Any] | None = None,
        primitive_call_code: str = "",
        source: str = "mansim.humanoid_incident",
        context: dict[str, Any] | None = None,
        notify_worker: bool = True,
        apply_state: bool = True,
    ) -> dict[str, Any]:
        profile = self._humanoid_incident_profile(code)
        if profile is None:
            raise RuntimeError(
                f"ManSim attempted to emit undefined HumanoidSim incident code or alias: {code!r}. "
                "Add the incident or alias to HumanoidSim data/incident_schema_core.json."
            )
        canonical_code = str(getattr(profile, "code", "") or self._canonical_humanoid_incident_code(code))
        recovery_protocol = self._incident_recovery_payload(profile)
        retry_policy = getattr(getattr(profile, "retry_policy", None), "to_dict", lambda: {})()
        category = str(getattr(profile, "category", "") or "unknown")
        severity = str(getattr(profile, "severity", "warning") or "warning")
        description = str(getattr(profile, "description", "") or canonical_code)
        primitive = primitive_call_code or str((step or {}).get("call_code", "") or agent.current_primitive_call_code or "")
        task_code = str(getattr(task, "task_code", "") or agent.current_task_code or "")
        task_instance_id = str(getattr(task, "instance_id", "") or agent.current_task_instance_id or "")
        step_id = str((step or {}).get("step_id", "") or agent.current_step_id or "")
        metadata = {
            "primitive_call_code": primitive,
            "context": dict(context or {}),
        }
        transition_event = self._build_humanoid_incident_transition_event(
            canonical_code,
            task_code=task_code,
            task_instance_id=task_instance_id,
            step_id=step_id,
            primitive_call_code=primitive,
            timestamp_s=round(float(self.env.now), 3),
            message=description,
            source=source,
            metadata=metadata,
        )
        if transition_event is None:
            raise RuntimeError(f"HumanoidSim could not build a transition event for incident {canonical_code!r}.")
        transition_metadata = dict(getattr(transition_event, "metadata", {}) or {})
        reason_obj = transition_event.reason_obj() if hasattr(transition_event, "reason_obj") else getattr(transition_event, "reason", None)
        reason_metadata = dict(getattr(reason_obj, "metadata", {}) or {}) if reason_obj is not None else {}
        if apply_state:
            self._apply_humanoid_transition_event(agent, transition_event)
            state_payload = self._humanoid_state_payload(agent)
        else:
            state_payload = self._humanoid_state_payload(agent)
            if reason_obj is not None and hasattr(reason_obj, "to_dict"):
                state_payload["reason"] = reason_obj.to_dict()
        details = {
            **transition_metadata,
            **reason_metadata,
            "primitive_call_code": primitive,
            "context": dict(context or {}),
            **metadata,
            "description": description,
            "default_availability": str(getattr(getattr(profile, "default_availability", None), "value", getattr(profile, "default_availability", "")) or ""),
            "incident_code": canonical_code,
            "incident_category": category,
            "incident_severity": severity,
            "recovery_protocol": recovery_protocol,
            "retry_policy": retry_policy,
            "task_id": str(getattr(task, "task_id", "") or agent.current_task_id or ""),
            "task_code": task_code,
            "instance_id": task_instance_id,
            "step_id": step_id,
            "humanoid_state": state_payload,
            "cargo": self._worker_cargo_payload(agent),
        }
        self.humanoid_incident_events.append(copy.deepcopy(details))
        self.humanoid_incident_events = self.humanoid_incident_events[-200:]
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="HUMANOID_INCIDENT",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details=details,
        )
        if notify_worker and recovery_protocol:
            agent.pending_recovery_incident = {
                "incident_code": canonical_code,
                "incident_category": category,
                "incident_severity": severity,
                "description": description,
                "source": source,
                "occurred_at": round(float(self.env.now), 3),
                "task_code": task_code,
                "instance_id": task_instance_id,
                "step_id": step_id,
                "primitive_call_code": primitive,
                "recovery_protocol": copy.deepcopy(recovery_protocol),
                "context": dict(context or {}),
            }
        self.emit_incident(
            canonical_code,
            affected_entities=[agent.agent_id],
            escalation_level="worker_local",
            details=details,
            notify_workers=[agent.agent_id] if notify_worker else [],
        )
        return details

    def _execute_active_humanoid_incident_recovery(
        self,
        agent: Worker,
        code: str,
        *,
        primitive_call_code: str = "NAVIGATE_TO",
        source: str,
        context: dict[str, Any] | None = None,
    ):
        """Run the HumanoidSim recovery protocol without adding local policy.

        ManSim decides only that an environment incident occurred. The incident
        profile, state reason, and recovery steps come from HumanoidSim.
        Movement can then retry with the same normal path/reservation logic.
        """
        runtime = getattr(self, "humanoid_runtime", None)
        task = self._current_parent_task_stub(agent)
        step = {
            "step_id": str(agent.current_step_id or "motion"),
            "call_code": str(primitive_call_code or agent.current_primitive_call_code or "NAVIGATE_TO"),
        }
        self._emit_humanoid_incident(
            agent,
            code,
            task=task,
            step=step,
            primitive_call_code=str(step["call_code"]),
            source=source,
            context=dict(context or {}),
            notify_worker=True,
        )
        if runtime is not None and getattr(runtime, "enabled", False):
            yield from runtime._execute_pending_recovery_protocol(
                agent,
                task,
                complete_parent_on_success=False,
            )

    def _start_battery_swap_wait(self, target_agent: Agent, from_agent_id: str) -> None:
        """Mark the battery receiver as waiting for an already-dispatched helper."""
        target_agent.awaiting_battery_from = str(from_agent_id)
        current_availability = str(self._humanoid_state_payload(target_agent).get("availability", "")).upper()
        # WAITING is for expected short waits. BLOCKED/DISABLED/OFFLINE carry stronger
        # operational meaning, so battery helper dispatch must not downgrade them.
        if current_availability not in {"BLOCKED", "DISABLED", "OFFLINE"}:
            self._transition_humanoid_state(
                target_agent,
                "waiting",
                reason="battery_swap_wait",
                reason_message=f"Waiting for battery delivery from {from_agent_id}.",
                source="mansim.power",
                metadata={"from_agent_id": str(from_agent_id)},
            )
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="BATTERY_SWAP_WAIT_START",
            entity_id=target_agent.agent_id,
            location=self.agent_display_location(target_agent),
            details={
                "from_agent_id": str(from_agent_id),
                "humanoid_state": self._humanoid_state_payload(target_agent),
            },
        )

    def _end_battery_swap_wait(self, target_agent: Agent, from_agent_id: str) -> None:
        """Clear the receiver-side wait marker after delivery, cancel, or retry."""
        was_waiting_for_sender = target_agent.awaiting_battery_from == str(from_agent_id)
        current_state = self._humanoid_state_payload(target_agent)
        current_reason = current_state.get("reason") if isinstance(current_state.get("reason"), dict) else {}
        was_wait_state = (
            str(current_state.get("availability", "")).upper() == "WAITING"
            and str(current_reason.get("code", "")).lower() == "battery_swap_wait"
        )
        if was_waiting_for_sender:
            target_agent.awaiting_battery_from = None
        if (
            was_wait_state
            and not target_agent.discharged
        ):
            self._transition_humanoid_state(
                target_agent,
                "task_completed",
                reason="battery_swap_wait_end",
                source="mansim.power",
                metadata={"cargo_present": False, "from_agent_id": str(from_agent_id)},
            )
        if not was_waiting_for_sender and not was_wait_state:
            return
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="BATTERY_SWAP_WAIT_END",
            entity_id=target_agent.agent_id,
            location=self.agent_display_location(target_agent),
            details={
                "from_agent_id": str(from_agent_id),
                "humanoid_state": self._humanoid_state_payload(target_agent),
            },
        )

    def _recent_incidents_for_agent(self, agent: Agent) -> list[dict[str, Any]]:
        backlog = agent.incident_backlog if isinstance(agent.incident_backlog, list) else []
        if backlog:
            return [dict(item) for item in backlog if isinstance(item, dict)]
        return [
            dict(item)
            for item in self.incident_events[-8:]
            if isinstance(item, dict)
            and (
                str(agent.agent_id) in [str(value) for value in item.get("affected_entities", [])]
                or item.get("escalation_level") == "worker_local"
            )
        ]

    def _opportunity_from_task(self, agent: Agent, task: Task, owners: list[str] | None = None) -> Opportunity:
        tags = []
        if task.task_type in {"REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            tags.append("reliability")
        if task.task_type == "INSPECT_PRODUCT":
            tags.append("inspection")
        if task.task_type in {"LOAD_MACHINE", "UNLOAD_MACHINE", "TRANSFER"}:
            tags.append("flow")
        return Opportunity(
            opportunity_id=self._task_opportunity_id(task),
            task_family=str(self._task_priority_key(task)),
            worker_id=str(agent.agent_id),
            priority_key=str(self._task_priority_key(task)),
            location=str(task.location),
            target=OpportunityTarget(
                target_type=self._task_target_type(task),
                target_id=self._task_target_id(task),
                target_station=self._task_target_station(task),
            ),
            payload=dict(task.payload),
            preconditions=["feasible_now"],
            expected_output_impact=float(task.priority),
            blocking_effect=str(task.task_type),
            shareable=self._task_shareable(task),
            capacity=self._task_capacity(task),
            owners=[str(value).strip() for value in (owners or [agent.agent_id]) if str(value).strip()],
            why_available=self._task_why_available(task),
            tags=tags,
        )

    def enumerate_opportunities(self) -> list[dict[str, Any]]:
        aggregated: dict[str, dict[str, Any]] = {}
        for agent_id in sorted(self.agents.keys()):
            agent = self.agents[agent_id]
            if agent.discharged:
                continue
            for task in self._candidate_tasks(agent):
                opportunity = self._opportunity_from_task(agent, task).to_dict()
                opportunity_id = str(opportunity.get("opportunity_id", "")).strip()
                if not opportunity_id:
                    continue
                current = aggregated.get(opportunity_id)
                if current is None:
                    current = opportunity
                    current["owners"] = [agent.agent_id]
                    aggregated[opportunity_id] = current
                    continue
                owners = current.get("owners", []) if isinstance(current.get("owners", []), list) else []
                if agent.agent_id not in owners:
                    owners.append(agent.agent_id)
                current["owners"] = owners
                current["capacity"] = max(
                    int(current.get("capacity", 1) or 1),
                    int(opportunity.get("capacity", 1) or 1),
                )
                current["expected_output_impact"] = max(
                    float(current.get("expected_output_impact", 0.0) or 0.0),
                    float(opportunity.get("expected_output_impact", 0.0) or 0.0),
                )
        rows = list(aggregated.values())
        rows.sort(
            key=lambda item: (
                -float(item.get("expected_output_impact", 0.0) or 0.0),
                str(item.get("task_family", "")),
                str(item.get("opportunity_id", "")),
            )
        )
        return rows

    def _annotate_task_selection(
        self,
        task: Task,
        *,
        decision_source: str,
        decision_rule: str,
        rationale: str = "",
        candidate_count: int | None = None,
        score_hint: float | None = None,
        decision_focus: list[str] | None = None,
        fallback_reason: str = "",
    ) -> Task:
        meta = dict(task.selection_meta) if isinstance(task.selection_meta, dict) else {}
        previous_source = str(meta.get("decision_source", "")).strip()
        previous_rule = str(meta.get("decision_rule", "")).strip()
        previous_rationale = str(meta.get("decision_rationale", "")).strip()
        if previous_source and previous_source != decision_source and "origin_decision_source" not in meta:
            meta["origin_decision_source"] = previous_source
        if previous_rule and previous_rule != decision_rule and "origin_decision_rule" not in meta:
            meta["origin_decision_rule"] = previous_rule
        if previous_rationale and previous_rationale != rationale and "origin_decision_rationale" not in meta:
            meta["origin_decision_rationale"] = previous_rationale
        meta["decision_mode"] = self.decision_mode
        meta["decision_source"] = decision_source
        meta["decision_rule"] = decision_rule
        if rationale:
            meta["decision_rationale"] = rationale
        else:
            meta.pop("decision_rationale", None)
        if candidate_count is not None:
            meta["candidate_count"] = int(candidate_count)
        else:
            meta.pop("candidate_count", None)
        if score_hint is not None:
            meta["score_hint"] = round(float(score_hint), 3)
        else:
            meta.pop("score_hint", None)
        if decision_focus:
            meta["decision_focus"] = [str(item) for item in decision_focus if str(item).strip()]
        else:
            meta.pop("decision_focus", None)
        if fallback_reason:
            meta["fallback_reason"] = fallback_reason
        else:
            meta.pop("fallback_reason", None)
        meta["decision_trace_id"] = str(task.task_id)
        meta["expected_task_signature"] = self._task_signature(task)
        task.selection_meta = meta
        return task

    def _serialize_task_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[str(key)] = value
        return out

    def _task_priority_key(self, task: Task) -> str:
        if task.task_type == "BATTERY_SWAP":
            return "battery_swap"
        if task.task_type == "BATTERY_CHARGE":
            return "battery_charge"
        if task.task_type == "REPAIR_MACHINE":
            return "repair_machine"
        if task.task_type == "UNLOAD_MACHINE":
            return "unload_machine"
        if task.task_type == "LOAD_MACHINE":
            return "load_machine"
        if task.task_type == "SETUP_MACHINE":
            return "setup_machine"
        if task.task_type == "PREVENTIVE_MAINTENANCE":
            return "preventive_maintenance"
        if task.task_type == "INSPECT_PRODUCT":
            return "inspect_product"
        if task.task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            action = str(task.payload.get("interface_action") or task.payload.get("action") or "").strip().lower()
            return "load_inspection_desk" if action == "load" else "unload_inspection_desk"
        if task.task_type == "HANDOVER_ITEM":
            return "handover_item"
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "battery_delivery":
                return "battery_delivery_discharged" if bool(task.payload.get("target_agent_discharged", False)) else "battery_delivery_low_battery"
            if transfer_kind == "material_supply":
                return "material_supply"
            if transfer_kind == "inter_station":
                return "inter_station_transfer"
        return str(task.priority_key).strip() or str(task.task_type).strip().lower()

    def _tile_payload(self, tile: Tile | None) -> dict[str, int] | None:
        if tile is None:
            return None
        return {"x": int(tile[0]), "y": int(tile[1])}

    def _tile_from_payload(self, payload: Any) -> Tile | None:
        if isinstance(payload, tuple) and len(payload) == 2:
            return (int(payload[0]), int(payload[1]))
        if isinstance(payload, dict) and "x" in payload and "y" in payload:
            return (int(payload["x"]), int(payload["y"]))
        return None

    def _traffic_observe_conflicts(self) -> bool:
        return bool(self.traffic_enabled and self.traffic_mode == "observe_conflicts" and self.traffic_fidelity in {"tile_edge", "tile"})

    def _traffic_strict_reservation(self) -> bool:
        return not self._traffic_observe_conflicts()

    def _traffic_reason_code(self, conflict_type: str, *, collision: bool = False) -> str:
        if collision:
            return "COLLISION"
        normalized = str(conflict_type or "").strip().lower()
        if normalized == "near_miss":
            return "NEAR_MISS"
        return normalized or "traffic_conflict"

    def _traffic_humanoid_state_payload(self, worker: Worker, conflict: TrafficConflict) -> dict[str, Any]:
        payload = self._humanoid_state_payload(worker)
        payload["reason"] = {
            "code": self._traffic_reason_code(conflict.conflict_type, collision=conflict.collision),
            "message": f"{conflict.conflict_type} with {conflict.other_worker_id}",
            "source": "mansim.traffic",
            "metadata": {
                "conflict_id": conflict.conflict_id,
                "other_worker_id": conflict.other_worker_id,
                "collision": bool(conflict.collision),
            },
        }
        return payload

    def _log_traffic_conflicts(self, agent: Worker, conflicts: list[TrafficConflict]) -> bool:
        recovery_requested = False
        for conflict in conflicts:
            details = conflict.to_dict()
            details["humanoid_state"] = self._traffic_humanoid_state_payload(agent, conflict)
            details["traffic_mode"] = self.traffic_mode
            details["collision_effect"] = self.traffic_collision_effect
            details["location"] = self.agent_display_location(agent)
            self.traffic_conflicts.append(copy.deepcopy(details))
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="AGENT_TRAFFIC_CONFLICT",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details=details,
            )
            incident_code = ""
            conflict_type = str(conflict.conflict_type or "").strip().upper()
            if bool(conflict.collision):
                incident_code = "COLLISION"
            elif conflict_type == "NEAR_MISS":
                incident_code = "NEAR_MISS"
            if incident_code and bool(self.humanoid_incident_natural_cfg.get(incident_code, True)):
                self._emit_humanoid_incident(
                    agent,
                    incident_code,
                    primitive_call_code="NAVIGATE_TO",
                    source="mansim.traffic",
                    context={key: value for key, value in details.items() if key != "humanoid_state"},
                    notify_worker=True,
                    apply_state=True,
                )
                recovery_requested = recovery_requested or isinstance(getattr(agent, "pending_recovery_incident", None), dict)
        return recovery_requested

    def _traffic_register_plan(self, agent: Worker, move_id: str, path: list[Tile], *, started_at: float, ended_at: float) -> None:
        if self.traffic_monitor is None or not self.traffic_enabled or len(path) < 2:
            return
        conflicts = self.traffic_monitor.register_plan(
            TrafficPlan(
                move_id=move_id,
                worker_id=agent.agent_id,
                path_tiles=tuple(path),
                started_at=float(started_at),
                ended_at=float(ended_at),
            )
        )
        self._log_traffic_conflicts(agent, conflicts)

    def _traffic_complete_plan(self, move_id: str) -> None:
        if self.traffic_monitor is not None:
            self.traffic_monitor.complete_plan(move_id)

    def _traffic_begin_segment(
        self,
        agent: Worker,
        *,
        move_id: str,
        segment_index: int,
        from_tile: Tile,
        to_tile: Tile,
        started_at: float,
        ended_at: float,
        logical_destination: str,
    ) -> bool:
        agent.current_move_segment_event_started = False
        recovery_requested = False
        if self.traffic_monitor is not None and self.traffic_enabled:
            segment = TrafficSegment(
                move_id=move_id,
                worker_id=agent.agent_id,
                segment_index=segment_index,
                from_tile=from_tile,
                to_tile=to_tile,
                started_at=float(started_at),
                ended_at=float(ended_at),
            )
            conflicts = self.traffic_monitor.begin_segment(segment)
            recovery_requested = self._log_traffic_conflicts(agent, conflicts)
        if self.traffic_emit_tile_step_events and not recovery_requested:
            self._update_battery_accounting(agent, started_at)
            self.logger.log(
                t=started_at,
                day=self.day_for_time(started_at),
                event_type="AGENT_MOVE_TILE_START",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={
                    "move_id": move_id,
                    "segment_index": segment_index,
                    "from_tile": self._tile_payload(from_tile),
                    "to_tile": self._tile_payload(to_tile),
                    "logical_destination": logical_destination,
                    "started_at": round(float(started_at), 3),
                    "ended_at": round(float(ended_at), 3),
                    "humanoid_state": self._humanoid_state_payload(agent),
                    "battery_remaining_min": round(float(self.battery_remaining(agent)), 3),
                },
            )
            agent.current_move_segment_event_started = True
        return recovery_requested

    def _traffic_end_segment(
        self,
        agent: Worker,
        *,
        move_id: str,
        segment_index: int,
        from_tile: Tile,
        to_tile: Tile,
        ended_at: float,
        logical_destination: str,
    ) -> None:
        if self.traffic_monitor is not None:
            self.traffic_monitor.end_segment(agent.agent_id, move_id, segment_index, ended_at=float(ended_at))
        if self.traffic_emit_tile_step_events and bool(getattr(agent, "current_move_segment_event_started", False)):
            self._update_battery_accounting(agent, ended_at)
            self.logger.log(
                t=ended_at,
                day=self.day_for_time(ended_at),
                event_type="AGENT_MOVE_TILE_END",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={
                    "move_id": move_id,
                    "segment_index": segment_index,
                    "from_tile": self._tile_payload(from_tile),
                    "to_tile": self._tile_payload(to_tile),
                    "logical_destination": logical_destination,
                    "ended_at": round(float(ended_at), 3),
                    "humanoid_state": self._humanoid_state_payload(agent),
                    "battery_remaining_min": round(float(self.battery_remaining(agent)), 3),
                },
            )
        agent.current_move_segment_event_started = False

    def _object_service_tile_payload(self, agent: Worker, object_id: str) -> dict[str, Any] | None:
        grid = self.grid_map
        if grid is None:
            return None
        normalized = grid.normalize_location(str(object_id))
        obj = grid.objects.get(normalized)
        if obj is None:
            return None
        tile = agent.tile
        service_tiles = set(grid.service_tiles.get(normalized, []))
        side = ""
        if tile is not None:
            if tile[1] == obj.y - 1 and obj.x <= tile[0] < obj.x + obj.width:
                side = "north"
            elif tile[1] == obj.y + obj.height and obj.x <= tile[0] < obj.x + obj.width:
                side = "south"
            elif tile[0] == obj.x - 1 and obj.y <= tile[1] < obj.y + obj.height:
                side = "west"
            elif tile[0] == obj.x + obj.width and obj.y <= tile[1] < obj.y + obj.height:
                side = "east"
        return {
            "service_object_id": normalized,
            "service_object_type": obj.object_type,
            "service_tile": self._tile_payload(tile),
            "service_tile_valid": tile in service_tiles if tile is not None else False,
            "interaction_side": side,
            "object_footprint": {
                "x": int(obj.x),
                "y": int(obj.y),
                "width": int(obj.width),
                "height": int(obj.height),
            },
        }

    def _confirm_object_service_tile(self, agent: Worker, object_id: str, task: Task, purpose: str) -> bool:
        payload = self._object_service_tile_payload(agent, object_id)
        if payload is None:
            return True
        payload.update(
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "purpose": purpose,
            }
        )
        valid = bool(payload.get("service_tile_valid"))
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="AGENT_SERVICE_TILE" if valid else "AGENT_SERVICE_TILE_VIOLATION",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details=payload,
        )
        return valid

    def _refresh_blocked_after_service_metrics(self) -> None:
        starts = self.buffer_metrics["blocked_after_service_started_at"]
        durations = self.buffer_metrics["blocked_after_service_min_by_machine"]
        for machine in self.machines.values():
            buffer_id = f"output_buffer_station_{machine.station}"
            blocked = bool(
                machine.output_intermediate is not None
                and self._buffer_capacity(buffer_id) is not None
                and self._buffer_available_slots(buffer_id) <= 0
            )
            started_at = starts.get(machine.machine_id)
            if blocked and started_at is None:
                starts[machine.machine_id] = float(self.env.now)
                self.buffer_metrics["blocked_after_service_count"] += 1
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_BLOCKED_AFTER_SERVICE",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={
                        "buffer_id": buffer_id,
                        "capacity": self._buffer_capacity(buffer_id),
                        "occupancy": len(self.output_buffers[machine.station]),
                        "reserved": self._buffer_reserved_count(buffer_id),
                    },
                )
            elif not blocked and started_at is not None:
                duration = max(0.0, float(self.env.now) - float(started_at))
                durations[machine.machine_id] += duration
                starts.pop(machine.machine_id, None)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_BLOCKED_AFTER_SERVICE_CLEARED",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={"buffer_id": buffer_id, "duration_min": round(duration, 6)},
                )

    def capture_snapshot(self) -> None:
        t = self.env.now
        self._refresh_blocked_after_service_metrics()
        self.minute_snapshots.append(
            {
                "t": round(t, 3),
                "day": self.day_for_time(t),
                "material_queue_lengths": {k: len(v) for k, v in self.material_queues.items()},
                "intermediate_queue_lengths": {k: len(v) for k, v in self.intermediate_queues.items()},
                "output_buffer_lengths": {k: len(v) for k, v in self.output_buffers.items()},
                "warehouse_material_shelf_count": self._material_shelf_count(),
                "warehouse_material_shelf_capacity": self.material_shelf_capacity,
                "inspection_scrap_queue_length": len(self.inspection_scrap_queue),
                "disposed_scrap_count": int(self.disposed_scrap_count),
                "machine_states": {mid: self._observed_machine_state(m).value for mid, m in self.machines.items()},
                "worker_tiles": {
                    worker_id: self._tile_payload(worker.tile)
                    for worker_id, worker in self.workers.items()
                    if worker.tile is not None
                },
                "humanoid_states": {
                    worker_id: self._humanoid_state_payload(worker)
                    for worker_id, worker in self.workers.items()
                },
                "inspection_active_agents": self.inspection_active_agents,
                "inspection_desk_state": self.inspection_desk_state,
                "inspection_desk_item_id": self.inspection_desk_item_id,
                "inspection_desk_result": self.inspection_desk_result,
                "inspection_owner": self.inspection_owner,
                "buffer_capacities": dict(self.buffer_capacities),
                "buffer_reserved_counts": {
                    buffer_id: self._buffer_reserved_count(buffer_id)
                    for buffer_id in sorted(self.buffer_capacities)
                },
                "incident_count": len(self.incident_events),
                "commitment_count": sum(len(rows) for rows in self.current_commitments().values()),
            }
        )

    def _transition_humanoid_state(
        self,
        worker: Worker,
        event_type: str,
        *,
        reason: str = "",
        reason_message: str = "",
        source: str = "mansim.world",
        task: Task | None = None,
        step: dict[str, Any] | None = None,
        status: str = "",
        metadata: dict[str, Any] | None = None,
        reconcile_power: bool = True,
    ) -> None:
        self._update_battery_accounting(worker)
        runtime = getattr(self, "humanoid_runtime", None)
        if runtime is not None and getattr(runtime, "enabled", False) and hasattr(runtime, "transition_state"):
            runtime.transition_state(
                worker,
                event_type,
                task=task,
                step=step,
                status=status,
                reason_code=reason,
                reason_message=reason_message,
                source=source,
                metadata=metadata,
            )
        if (
            reconcile_power
            and
            event_type not in {"power_normal", "power_low", "power_critical", "disabled"}
            and hasattr(self, "heuristic_rules")
            and hasattr(self, "battery_swap_period_min")
        ):
            # HumanoidSim owns the state transition graph, while ManSim owns the
            # scenario fact of current battery level. Reconcile the power axis
            # after lifecycle/task transitions so task completion cannot leave a
            # low-battery robot displayed as POWER_NORMAL until the next monitor tick.
            self._sync_humanoid_power_state(worker)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="WORKER_STATE_CHANGED",
            entity_id=worker.worker_id,
            location=self.worker_display_location(worker),
            details={
                "humanoid_state": self._humanoid_state_payload(worker),
                "cargo": self._worker_cargo_payload(worker),
                "motion": self._worker_motion_payload(worker),
                "tile": self._tile_payload(worker.tile),
                "battery_remaining_min": round(float(self.battery_remaining(worker)), 3),
            },
        )

    def _apply_humanoid_transition_event(self, worker: Worker, transition_event: Any) -> None:
        self._update_battery_accounting(worker)
        runtime = getattr(self, "humanoid_runtime", None)
        if runtime is not None and getattr(runtime, "enabled", False) and hasattr(runtime, "apply_transition_event"):
            runtime.apply_transition_event(worker, transition_event)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="WORKER_STATE_CHANGED",
            entity_id=worker.worker_id,
            location=self.worker_display_location(worker),
            details={
                "humanoid_state": self._humanoid_state_payload(worker),
                "cargo": self._worker_cargo_payload(worker),
                "motion": self._worker_motion_payload(worker),
                "tile": self._tile_payload(worker.tile),
                "battery_remaining_min": round(float(self.battery_remaining(worker)), 3),
            },
        )

    def _log_worker_state_observation(self, worker: Worker, *, reason: str = "") -> None:
        """Log the current HumanoidSim snapshot without applying a state transition.

        This is used for passive observations such as a blocked movement wait. It
        keeps Replay motion windows fresh without inventing a local ManSim state.
        """
        details = {
            "humanoid_state": self._humanoid_state_payload(worker),
            "cargo": self._worker_cargo_payload(worker),
            "motion": self._worker_motion_payload(worker),
            "tile": self._tile_payload(worker.tile),
            "battery_remaining_min": round(float(self.battery_remaining(worker)), 3),
        }
        if reason:
            details["observation_reason"] = str(reason)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="WORKER_STATE_CHANGED",
            entity_id=worker.worker_id,
            location=self.worker_display_location(worker),
            details=details,
        )

    def log_periodic_worker_state_observations(self, *, reason: str = "snapshot_tick") -> None:
        """Emit passive worker snapshots so Replay panels stay current while idle.

        Battery accounting is intentionally lazy: the budget is reduced whenever a
        worker snapshot is requested. AVAILABLE workers can otherwise sit without
        task or motion events for long spans, leaving the Replay side panel on an
        old battery value even though the simulated battery has continued to drain.
        """
        if self.terminated or self.logger.closed:
            return
        for worker in self.workers.values():
            self._log_worker_state_observation(worker, reason=reason)

    def _set_humanoid_for_task(self, worker: Worker, task: Task | None, *, reason: str, task_id: str | None = None) -> None:
        if worker.discharged:
            self._transition_humanoid_state(
                worker,
                "disabled",
                reason=reason or "battery_depleted",
                source="mansim.discharge",
            )
            return
        if worker.current_primitive_call_code == "NAVIGATE_TO":
            self._transition_humanoid_state(
                worker,
                "primitive_finished",
                step={"step_id": worker.current_step_id or "motion", "call_code": "NAVIGATE_TO"},
                status="completed",
                reason=reason,
                source="mansim.motion",
            )
        if task is None:
            self._transition_humanoid_state(
                worker,
                "task_completed",
                status="completed",
                reason=reason,
                source="mansim.task",
                metadata={"cargo_present": bool(worker.carrying_item_id or worker.carrying_item_ids)},
            )
            return
        self._transition_humanoid_state(
            worker,
            "task_started",
            task=task,
            status="running",
            reason=reason,
            source="mansim.task",
            metadata={"cargo_present": bool(worker.carrying_item_id or worker.carrying_item_ids)},
        )

    def _set_humanoid_primitive_hint(self, agent: Agent, primitive_call_code: str, *, reason: str = "primitive_hint") -> None:
        """Update the HumanoidSim state snapshot for domain-internal primitives."""
        primitive_call_code = self._catalog_primitive_for_active_task(agent, primitive_call_code)
        agent.current_primitive_call_code = str(primitive_call_code or "")
        runtime = getattr(self, "humanoid_runtime", None)
        if runtime is not None and getattr(runtime, "enabled", False) and hasattr(runtime, "set_step_state"):
            task = self._current_task_stub(agent)
            step = {"step_id": agent.current_step_id or "", "call_code": primitive_call_code}
            runtime.set_step_state(agent, task, step, event_type="HUMANOID_STEP_START", status="running")
        logger = getattr(self, "logger", None)
        if logger is not None and hasattr(logger, "log"):
            logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="WORKER_STATE_CHANGED",
                entity_id=agent.worker_id,
                location=self.worker_display_location(agent),
                details={
                    "humanoid_state": self._humanoid_state_payload(agent),
                    "cargo": self._worker_cargo_payload(agent),
                    "motion": self._worker_motion_payload(agent),
                    "tile": self._tile_payload(agent.tile),
                    "battery_remaining_min": round(float(self.battery_remaining(agent)), 3),
                },
        )
        return

    def _catalog_primitive_for_active_task(self, agent: Agent, primitive_call_code: str) -> str:
        """Keep Replay state aligned with the active HumanoidSim task spec.

        Domain helpers may perform fine-grained side effects such as GRASP or
        PLACE while an atomic catalog task represents those effects as one
        semantic primitive. The worker panel should show the catalog primitive,
        not an implementation detail that does not belong to the active task.
        """
        primitive = str(primitive_call_code or "").strip().upper()
        if not primitive:
            return primitive
        task_code = str(agent.current_child_task_code or agent.current_task_code or "").strip().upper()
        if not task_code:
            return primitive
        runtime = getattr(self, "humanoid_runtime", None)
        catalog = getattr(runtime, "catalog", None)
        task_spec = getattr(catalog, "tasks", {}).get(task_code) if catalog is not None else None
        steps = getattr(task_spec, "steps", None)
        if not steps:
            return primitive
        declared_primitives: set[str] = set()
        for step in steps:
            level = getattr(getattr(step, "expected_level", None), "value", getattr(step, "expected_level", ""))
            if str(level or "").strip().upper() == "PRIMITIVE_SKILL":
                declared_primitives.add(str(getattr(step, "call_code", "") or "").strip().upper())
        if not declared_primitives or primitive in declared_primitives:
            return primitive
        if primitive == "ALIGN" and "NAVIGATE_TO" in declared_primitives:
            return "NAVIGATE_TO"
        internal_manipulation = {"REACH_TO", "GRASP", "LIFT", "PLACE", "RELEASE", "VERIFY_PLACEMENT"}
        semantic_fallbacks = (
            "EXECUTE_MACHINE_ACTION",
            "EXECUTE_QUALITY_ACTION",
            "EXECUTE_MAINTENANCE_ACTION",
            "EXECUTE_SYSTEM_ACTION",
            "EXECUTE_HUMAN_COLLABORATION_ACTION",
            "EXECUTE_ROBOT_COLLABORATION_ACTION",
        )
        if primitive in internal_manipulation:
            for fallback in semantic_fallbacks:
                if fallback in declared_primitives:
                    return fallback
        return primitive

    def _resume_humanoid_task_after_recovery(self, agent: Agent, *, reason: str, source: str) -> None:
        """Return a worker from incident recovery back to its active task context."""
        current_availability = str(self._humanoid_state_payload(agent).get("availability", "")).upper()
        if current_availability not in {"BLOCKED", "WAITING"}:
            return
        self._transition_humanoid_state(
            agent,
            "task_started",
            task=self._current_parent_task_stub(agent),
            status="running",
            reason=reason,
            source=source,
            metadata={"resume_after_recovery": True},
        )

    def _dock_agent_at_target(self, agent: Agent, task: Task | None = None, *, reason: str = "align_target"):
        """Represent precise local alignment after path travel and before work."""
        self._set_humanoid_primitive_hint(agent, "ALIGN", reason=reason)
        yield self.env.timeout(0.0)
        finished_call_code = self._catalog_primitive_for_active_task(agent, "ALIGN")
        self._transition_humanoid_state(
            agent,
            "primitive_finished",
            step={"step_id": agent.current_step_id or "align_target", "call_code": finished_call_code},
            status="completed",
            reason=reason,
            source="mansim.alignment",
        )

    def _current_task_stub(self, worker: Worker) -> Task:
        task_code = str(worker.current_child_task_code or worker.current_task_code or "")
        task_instance_id = str(worker.current_child_task_instance_id or worker.current_task_instance_id or "")
        task_name = str(worker.current_child_task_name or "")
        return Task(
            task_id=str(worker.current_child_task_instance_id or worker.current_task_id or ""),
            task_type=task_code or str(worker.current_task_type or ""),
            priority_key="",
            priority=0.0,
            location=str(worker.location),
            payload=copy.deepcopy(getattr(worker, "current_task_payload", {}) or {}),
            task_code=task_code,
            instance_id=task_instance_id,
            assigned_robot_id=worker.worker_id,
            task_spec_name=task_name,
        )

    def _current_parent_task_stub(self, worker: Worker) -> Task:
        task_code = str(worker.current_task_code or "")
        task_instance_id = str(worker.current_task_instance_id or "")
        return Task(
            task_id=str(worker.current_task_id or ""),
            task_type=task_code or str(worker.current_task_type or ""),
            priority_key="",
            priority=0.0,
            location=str(worker.location),
            payload=copy.deepcopy(getattr(worker, "current_task_payload", {}) or {}),
            task_code=task_code,
            instance_id=task_instance_id,
            assigned_robot_id=worker.worker_id,
            task_spec_name=task_code.replace("_", " ").title() if task_code else "",
            selection_meta=copy.deepcopy(getattr(worker, "current_task_selection_meta", {}) or {}),
        )

    def _current_child_task_stub(self, worker: Worker) -> Task | None:
        if not worker.current_child_task_code and not worker.current_child_task_instance_id:
            return None
        task_code = str(worker.current_child_task_code or "")
        return Task(
            task_id=str(worker.current_child_task_instance_id or ""),
            task_type=task_code,
            priority_key="",
            priority=0.0,
            location=str(worker.location),
            payload={},
            task_code=task_code,
            instance_id=str(worker.current_child_task_instance_id or ""),
            assigned_robot_id=worker.worker_id,
            task_spec_name=str(worker.current_child_task_name or task_code.replace("_", " ").title()),
        )

    def _sync_humanoid_cargo_state(self, worker: Worker, *, destination: str = "") -> None:
        runtime = getattr(self, "humanoid_runtime", None)
        if runtime is not None and getattr(runtime, "enabled", False) and hasattr(runtime, "sync_worker_cargo_state"):
            runtime.sync_worker_cargo_state(worker, destination=destination)

    def _set_humanoid_disabled_state(self, worker: Worker, *, reason: str) -> None:
        runtime = getattr(self, "humanoid_runtime", None)
        if runtime is not None and getattr(runtime, "enabled", False) and hasattr(runtime, "set_disabled_state"):
            runtime.set_disabled_state(worker, reason=reason)

    def _humanoid_state_payload(self, worker: Worker) -> dict[str, Any]:
        runtime = getattr(self, "humanoid_runtime", None)
        if runtime is not None and getattr(runtime, "enabled", False) and hasattr(runtime, "state_payload"):
            payload = runtime.state_payload(worker)
        else:
            if not isinstance(worker.humanoid_state, dict) or not worker.humanoid_state:
                worker.humanoid_state = default_humanoid_state_payload(worker.worker_id)
            worker.humanoid_state["humanoid_id"] = worker.worker_id
            payload = copy.deepcopy(worker.humanoid_state)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        metadata["battery_remaining_min"] = round(float(self.battery_remaining(worker)), 3)
        metadata["battery_period_min"] = round(float(getattr(self, "battery_swap_period_min", 0.0) or 0.0), 3)
        metadata["low_threshold_ratio"] = round(float(getattr(self, "rolling_horizon_battery_low_ratio", 0.0) or 0.0), 3)
        payload["metadata"] = metadata
        payload["humanoid_id"] = worker.worker_id
        payload["timestamp_s"] = round(float(self.env.now), 3)
        return payload

    def _worker_cargo_payload(self, worker: Worker) -> dict[str, Any]:
        session = self._transport_session_for_worker(worker)
        item_ids = [str(item_id) for item_id in getattr(worker, "carrying_item_ids", []) if str(item_id)]
        if not item_ids and worker.carrying_item_id:
            item_ids = [str(worker.carrying_item_id)]
        payload: dict[str, Any] = {
            "item_id": worker.carrying_item_id,
            "item_type": worker.carrying_item_type,
            "item_ids": item_ids,
            "item_count": int(worker.carrying_item_count or len(item_ids)),
            "max_item_count": int(worker.carrying_item_max_count or 1),
        }
        if session is not None:
            carrier_ids = [str(worker_id) for worker_id in session.get("carrier_ids", [])]
            payload.update(
                {
                    "transport_session_id": str(session.get("session_id", "")),
                    "shared_carry": len(carrier_ids) > 1,
                    "carrier_ids": carrier_ids,
                    "carrier_count": len(carrier_ids),
                    "shared_carry_role": worker.shared_carry_role or "",
                    "item_time_multiplier": round(self._item_transport_multiplier(worker.carrying_item_type), 3),
                    "effective_time_multiplier": round(self._current_transport_time_multiplier(worker), 3),
                }
            )
        return payload

    def _item_transport_multiplier(self, item_type: str | None) -> float:
        normalized = str(item_type or "").strip().lower()
        if not normalized:
            return 1.0
        return max(0.01, float(self.item_transport_weight_multiplier.get(normalized, 1.0) or 1.0))

    def _transport_session_for_worker(self, worker: Worker) -> dict[str, Any] | None:
        session_id = str(worker.transport_session_id or self.product_transport_session_by_worker.get(worker.worker_id, "") or "")
        session = self.product_transport_sessions.get(session_id)
        if isinstance(session, dict) and str(session.get("status", "active")) == "active":
            return session
        return None

    def _transport_session_for_item(self, item_id: str | None) -> dict[str, Any] | None:
        session_id = self.product_transport_session_by_item.get(str(item_id or ""))
        session = self.product_transport_sessions.get(str(session_id or ""))
        if isinstance(session, dict) and str(session.get("status", "active")) == "active":
            return session
        return None

    def _transport_carrier_count(self, worker: Worker) -> int:
        session = self._transport_session_for_worker(worker)
        if session is None:
            return 1
        return max(1, len([worker_id for worker_id in session.get("carrier_ids", []) if str(worker_id) in self.workers]))

    def _current_transport_time_multiplier(self, worker: Worker) -> float:
        base = self._item_transport_multiplier(worker.carrying_item_type)
        session = self._transport_session_for_worker(worker)
        if (
            session is not None
            and str(worker.carrying_item_type or "").strip().lower() == "product"
            and self.product_collaboration_divide_time
        ):
            return max(0.01, base / float(self._transport_carrier_count(worker)))
        return base

    def _ensure_product_transport_session(self, worker: Worker, *, destination: str) -> dict[str, Any] | None:
        if not self.product_collaboration_enabled:
            return None
        if str(worker.carrying_item_type or "").strip().lower() != "product" or not worker.carrying_item_id:
            return None
        existing = self._transport_session_for_item(worker.carrying_item_id)
        if existing is not None:
            existing["destination"] = str(destination)
            if worker.worker_id not in existing.get("carrier_ids", []):
                existing.setdefault("carrier_ids", []).append(worker.worker_id)
            worker.transport_session_id = str(existing.get("session_id", ""))
            worker.shared_carry_role = worker.shared_carry_role or "primary"
            self.product_transport_session_by_worker[worker.worker_id] = worker.transport_session_id
            return existing
        session_id = f"PTX-{next(self.transport_session_counter):06d}"
        done_event = self.env.event()
        session: dict[str, Any] = {
            "session_id": session_id,
            "status": "active",
            "item_id": worker.carrying_item_id,
            "item_type": "product",
            "primary_worker_id": worker.worker_id,
            "carrier_ids": [worker.worker_id],
            "destination": str(destination),
            "started_at": float(self.env.now),
            "joined_at": {},
            "handover_task_id": "",
            "max_carriers": int(self.product_collaboration_max_carriers),
            "done_event": done_event,
        }
        self.product_transport_sessions[session_id] = session
        self.product_transport_session_by_item[worker.carrying_item_id] = session_id
        self.product_transport_session_by_worker[worker.worker_id] = session_id
        worker.transport_session_id = session_id
        worker.shared_carry_role = "primary"
        self._set_worker_cargo(worker, worker.carrying_item_id, "product", destination=str(destination))
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="PRODUCT_CARRY_STARTED",
            entity_id=worker.carrying_item_id,
            location=self.agent_display_location(worker),
            details=self._transport_session_event_details(session, primary_worker=worker.worker_id),
        )
        return session

    def _transport_session_event_details(self, session: dict[str, Any], **extra: Any) -> dict[str, Any]:
        carrier_ids = [str(worker_id) for worker_id in session.get("carrier_ids", []) if str(worker_id)]
        item_type = str(session.get("item_type", "product") or "product")
        base_multiplier = self._item_transport_multiplier(item_type)
        effective_multiplier = base_multiplier / max(1, len(carrier_ids)) if self.product_collaboration_divide_time else base_multiplier
        details = {
            "transport_session_id": str(session.get("session_id", "")),
            "item_id": str(session.get("item_id", "")),
            "item_type": item_type,
            "primary_worker_id": str(session.get("primary_worker_id", "")),
            "carrier_ids": carrier_ids,
            "carrier_count": len(carrier_ids),
            "max_carriers": int(session.get("max_carriers", self.product_collaboration_max_carriers) or self.product_collaboration_max_carriers),
            "destination": str(session.get("destination", "")),
            "started_at": round(float(session.get("started_at", self.env.now) or self.env.now), 3),
            "item_time_multiplier": round(base_multiplier, 3),
            "effective_time_multiplier": round(effective_multiplier, 3),
        }
        details.update({key: value for key, value in extra.items() if value not in {None, ""}})
        return details

    def _join_product_transport_session(self, helper: Worker, session_id: str, *, task: Task) -> bool:
        session = self.product_transport_sessions.get(str(session_id))
        if not isinstance(session, dict) or str(session.get("status", "active")) != "active":
            return False
        if helper.worker_id in session.get("carrier_ids", []):
            return True
        if len(session.get("carrier_ids", [])) >= int(session.get("max_carriers", self.product_collaboration_max_carriers) or self.product_collaboration_max_carriers):
            return False
        item_id = str(session.get("item_id", ""))
        if not item_id:
            return False
        if helper.carrying_item_id not in {None, item_id}:
            return False
        if helper.carrying_item_id is None and not self._set_agent_carrying(helper, "product", item_id):
            return False
        session.setdefault("carrier_ids", []).append(helper.worker_id)
        session.setdefault("joined_at", {})[helper.worker_id] = float(self.env.now)
        session["handover_task_id"] = task.task_id
        helper.transport_session_id = str(session.get("session_id", ""))
        helper.shared_carry_role = "helper"
        self.product_transport_session_by_worker[helper.worker_id] = helper.transport_session_id
        for carrier_id in session.get("carrier_ids", []):
            carrier = self.workers.get(str(carrier_id))
            if carrier is not None and carrier.carrying_item_id == item_id:
                self._set_worker_cargo(carrier, item_id, "product", destination=str(session.get("destination", "")))
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="PRODUCT_CARRY_JOINED",
            entity_id=item_id,
            location=self.agent_display_location(helper),
            details={
                **self._transport_session_event_details(session, helper_worker_id=helper.worker_id, handover_task_id=task.task_id),
                "humanoid_state": self._humanoid_state_payload(helper),
            },
        )
        self._transition_humanoid_state(
            helper,
            "cargo_changed",
            reason="product_carry_joined",
            source="mansim.handover",
            task=task,
            metadata={"cargo_present": True},
        )
        return True

    def _leave_product_transport_session(self, worker: Worker, *, reason: str = "left_session") -> None:
        session = self._transport_session_for_worker(worker)
        if session is not None:
            carrier_ids = [str(worker_id) for worker_id in session.get("carrier_ids", []) if str(worker_id) != worker.worker_id]
            session["carrier_ids"] = carrier_ids
            if str(session.get("status", "active")) == "active":
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="PRODUCT_CARRY_LEFT",
                    entity_id=str(session.get("item_id", "")),
                    location=self.agent_display_location(worker),
                    details=self._transport_session_event_details(session, worker_id=worker.worker_id, reason=reason),
                )
        self.product_transport_session_by_worker.pop(worker.worker_id, None)
        worker.transport_session_id = None
        worker.shared_carry_role = None
        if not worker.current_task_id and not worker.discharged:
            self._set_humanoid_for_task(worker, None, reason=reason)

    def _abort_product_transport_session_for_item(self, item_id: str, *, reason: str, destination: str = "dropped") -> None:
        session_id = str(self.product_transport_session_by_item.get(str(item_id), "") or "")
        session = self.product_transport_sessions.get(session_id)
        if not isinstance(session, dict) or str(session.get("status", "active")) != "active":
            return
        session["status"] = "aborted"
        session["completed_at"] = float(self.env.now)
        carrier_ids = [str(worker_id) for worker_id in session.get("carrier_ids", []) if str(worker_id)]
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="PRODUCT_CARRY_ABORTED",
            entity_id=str(item_id),
            location=str(destination),
            details=self._transport_session_event_details(session, destination=destination, outcome="aborted", reason=reason),
        )
        done_event = session.get("done_event")
        if done_event is not None and hasattr(done_event, "triggered") and not done_event.triggered:
            done_event.succeed(str(reason or "aborted"))
        for worker_id in carrier_ids:
            worker = self.workers.get(worker_id)
            if worker is None:
                continue
            self.product_transport_session_by_worker.pop(worker_id, None)
            worker.transport_session_id = None
            worker.shared_carry_role = None
            if worker.carrying_item_id == item_id:
                self._set_worker_cargo(worker, None, None, destination=destination)
            if not worker.current_task_id and not worker.discharged:
                self._set_humanoid_for_task(worker, None, reason=reason)
        self.product_transport_session_by_item.pop(str(item_id), None)

    def _complete_product_transport_session(self, session: dict[str, Any], *, destination: str, outcome: str = "completed") -> None:
        if not isinstance(session, dict) or str(session.get("status", "active")) != "active":
            return
        session["status"] = str(outcome or "completed")
        session["completed_at"] = float(self.env.now)
        item_id = str(session.get("item_id", ""))
        carrier_ids = [str(worker_id) for worker_id in session.get("carrier_ids", []) if str(worker_id)]
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="PRODUCT_CARRY_COMPLETED",
            entity_id=item_id,
            location=str(destination),
            details={
                **self._transport_session_event_details(session, destination=destination, outcome=outcome),
                "duration": round(max(0.0, float(self.env.now) - float(session.get("started_at", self.env.now) or self.env.now)), 3),
                "shared_duration": round(
                    max(
                        0.0,
                        float(self.env.now)
                        - min(
                            [float(value) for value in (session.get("joined_at", {}) if isinstance(session.get("joined_at", {}), dict) else {}).values()]
                            or [float(self.env.now)]
                        ),
                    ),
                    3,
                ),
            },
        )
        done_event = session.get("done_event")
        if done_event is not None and hasattr(done_event, "triggered") and not done_event.triggered:
            done_event.succeed(str(outcome or "completed"))
        for worker_id in carrier_ids:
            worker = self.workers.get(worker_id)
            if worker is None:
                continue
            self.product_transport_session_by_worker.pop(worker_id, None)
            worker.transport_session_id = None
            worker.shared_carry_role = None
            if worker.carrying_item_id == item_id:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="AGENT_DROP_ITEM",
                    entity_id=worker.worker_id,
                    location=self.agent_display_location(worker),
                    details={
                        "item_id": item_id,
                        "item_type": "product",
                        "to": destination,
                        "transport_session_id": str(session.get("session_id", "")),
                        "shared_carry": len(carrier_ids) > 1,
                        "humanoid_state": self._humanoid_state_payload(worker),
                    },
                )
                self._set_worker_cargo(worker, None, None, destination=destination)
            if not worker.current_task_id and not worker.discharged:
                self._set_humanoid_for_task(worker, None, reason="shared_product_carry_completed")
        if item_id:
            self.product_transport_session_by_item.pop(item_id, None)

    def _complete_product_transport_session_keep_primary_cargo(
        self,
        primary: Worker,
        *,
        destination: str,
        outcome: str = "arrived_at_handling_point",
    ) -> None:
        session = self._transport_session_for_worker(primary)
        if not isinstance(session, dict) or str(session.get("status", "active")) != "active":
            return
        item_id = str(session.get("item_id", ""))
        if not item_id or str(primary.carrying_item_id or "") != item_id:
            return
        session["status"] = str(outcome or "completed")
        session["completed_at"] = float(self.env.now)
        carrier_ids = [str(worker_id) for worker_id in session.get("carrier_ids", []) if str(worker_id)]
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="PRODUCT_CARRY_COMPLETED",
            entity_id=item_id,
            location=str(destination),
            details={
                **self._transport_session_event_details(session, destination=destination, outcome=outcome),
                "duration": round(max(0.0, float(self.env.now) - float(session.get("started_at", self.env.now) or self.env.now)), 3),
                "primary_kept_cargo": True,
            },
        )
        done_event = session.get("done_event")
        if done_event is not None and hasattr(done_event, "triggered") and not done_event.triggered:
            done_event.succeed(str(outcome or "completed"))
        for worker_id in carrier_ids:
            worker = self.workers.get(worker_id)
            if worker is None:
                continue
            self.product_transport_session_by_worker.pop(worker_id, None)
            worker.transport_session_id = None
            worker.shared_carry_role = None
            if worker.worker_id == primary.worker_id:
                self._set_worker_cargo(worker, item_id, "product", destination=destination)
                if not worker.current_task_id and not worker.discharged:
                    self._set_humanoid_for_task(worker, None, reason="shared_product_carry_completed")
                continue
            if worker.carrying_item_id == item_id:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="AGENT_DROP_ITEM",
                    entity_id=worker.worker_id,
                    location=self.agent_display_location(worker),
                    details={
                        "item_id": item_id,
                        "item_type": "product",
                        "to": destination,
                        "transport_session_id": str(session.get("session_id", "")),
                        "shared_carry": True,
                        "reason": "shared_carry_completed",
                        "humanoid_state": self._humanoid_state_payload(worker),
                    },
                )
                self._set_worker_cargo(worker, None, None, destination=destination)
            if not worker.current_task_id and not worker.discharged:
                self._set_humanoid_for_task(worker, None, reason="shared_product_carry_completed")
        self.product_transport_session_by_item.pop(item_id, None)

    def _product_session_has_remaining_path(self, session: dict[str, Any]) -> bool:
        primary = self.workers.get(str(session.get("primary_worker_id", "")))
        destination = str(session.get("destination", ""))
        if primary is None or not destination:
            return False
        if self.grid_map is None:
            return str(primary.location) != destination or self._has_in_transit_position(primary)
        if primary.tile is None:
            return False
        destinations = self.grid_map.destination_tiles(destination, worker_id=primary.worker_id, from_tile=primary.tile, ignore_dynamic=True)
        if primary.tile in destinations:
            return False
        path = self.grid_map.find_path(primary.tile, destinations, worker_id=primary.worker_id, ignore_dynamic=True)
        return bool(path and len(path) > 1)

    def _product_session_remaining_travel_min(self, session: dict[str, Any], *, future_extra_carriers: int = 0) -> float:
        primary = self.workers.get(str(session.get("primary_worker_id", "")))
        destination = str(session.get("destination", ""))
        if primary is None or not destination:
            return 0.0
        carrier_count = len([str(worker_id) for worker_id in session.get("carrier_ids", []) if str(worker_id)])
        future_carriers = max(1, min(self.product_collaboration_max_carriers, carrier_count + max(0, int(future_extra_carriers))))
        multiplier = self._item_transport_multiplier("product")
        if self.product_collaboration_divide_time:
            multiplier = multiplier / future_carriers
        if self.grid_map is None:
            return float(self.travel_time(self.agent_display_location(primary), destination)) * multiplier
        if primary.tile is None:
            return 0.0
        destinations = self.grid_map.destination_tiles(destination, worker_id=primary.worker_id, from_tile=primary.tile, ignore_dynamic=True)
        if primary.tile in destinations:
            return 0.0
        path = self.grid_map.find_path(primary.tile, destinations, worker_id=primary.worker_id, ignore_dynamic=True)
        if not path:
            return 0.0
        return max(0, len(path) - 1) * float(self.grid_map.tile_time_min) * multiplier

    def _helper_join_travel_min(self, helper: Agent, source_agent: Agent) -> float:
        if self.grid_map is None:
            return float(self.travel_time(self.agent_display_location(helper), self.agent_display_location(source_agent)))
        if helper.tile is None or source_agent.tile is None:
            return 0.0
        destinations = self.grid_map.destination_tiles(source_agent.agent_id, worker_id=helper.agent_id, from_tile=helper.tile, ignore_dynamic=True)
        if helper.tile in destinations:
            return 0.0
        path = self.grid_map.find_path(helper.tile, destinations, worker_id=helper.agent_id, ignore_dynamic=True)
        if not path:
            return float("inf")
        return max(0, len(path) - 1) * float(self.grid_map.tile_time_min) * self._current_transport_time_multiplier(helper)

    def _helper_join_catch_min(self, helper: Agent, source_agent: Agent) -> float:
        travel_to_current_source = self._helper_join_travel_min(helper, source_agent)
        if not math.isfinite(travel_to_current_source) or travel_to_current_source <= 0.0:
            return travel_to_current_source
        if not self._has_in_transit_position(source_agent):
            return travel_to_current_source
        helper_multiplier = self._current_transport_time_multiplier(helper)
        source_multiplier = self._current_transport_time_multiplier(source_agent)
        if source_multiplier <= helper_multiplier:
            return float("inf")
        speed_advantage = 1.0 - (helper_multiplier / max(1e-9, source_multiplier))
        return travel_to_current_source / max(0.05, speed_advantage)

    def _product_session_join_feasible(self, helper: Agent, session: dict[str, Any]) -> bool:
        if not isinstance(session, dict) or str(session.get("status", "active")) != "active":
            return False
        source_agent = self.agents.get(str(session.get("primary_worker_id", "")))
        if source_agent is None or source_agent.worker_id == helper.worker_id:
            return False
        if source_agent.carrying_item_id != str(session.get("item_id", "")):
            return False
        remaining_current = self._product_session_remaining_travel_min(session, future_extra_carriers=0)
        if remaining_current <= 0.0:
            return False
        helper_travel = self._helper_join_catch_min(helper, source_agent)
        if not math.isfinite(helper_travel):
            return False
        tile_time = float(getattr(self.grid_map, "tile_time_min", 0.0) or 0.0)
        primitive_time = float(getattr(self.humanoid_runtime, "default_primitive_min_duration", 0.0) or 0.0)
        min_join_margin = max(0.5, 5.0 * tile_time, 3.0 * primitive_time)
        return (helper_travel * 1.25) + min_join_margin < remaining_current

    def _shared_transport_followers(self, primary: Worker) -> list[Worker]:
        session = self._transport_session_for_worker(primary)
        if session is None:
            return []
        return [
            worker
            for worker_id in session.get("carrier_ids", [])
            if (worker := self.workers.get(str(worker_id))) is not None and worker.worker_id != primary.worker_id
        ]

    def _start_shared_transport_segment(
        self,
        primary: Worker,
        *,
        from_tile: Tile,
        to_tile: Tile,
        logical_destination: str,
        segment_duration: float,
        segment_index: int,
    ) -> None:
        for helper in self._shared_transport_followers(primary):
            helper_from_tile = helper.tile or from_tile
            helper_to_tile = from_tile if helper_from_tile != from_tile else helper_from_tile
            helper.current_move_segment_index = segment_index
            helper.current_move_segment_from_tile = helper_from_tile
            helper.current_move_segment_to_tile = helper_to_tile
            helper.current_move_logical_destination = logical_destination
            self._set_worker_motion(
                helper,
                str(helper.location),
                logical_destination,
                0.0,
                segment_duration,
                path_tiles=[helper_from_tile, helper_to_tile] if helper_from_tile != helper_to_tile else [helper_from_tile],
                target_tile=helper_to_tile,
            )

    def _finish_shared_transport_segment(
        self,
        primary: Worker,
        *,
        from_tile: Tile,
        to_tile: Tile,
        logical_destination: str,
        segment_duration: float,
    ) -> None:
        grid = self.grid_map
        for helper in self._shared_transport_followers(primary):
            helper_to_tile = from_tile
            if grid is not None:
                grid.release_reservation(helper.worker_id)
                grid.move_worker(helper.worker_id, helper_to_tile)
            helper.tile = helper_to_tile
            helper.current_move_segment_index = 0
            helper.current_move_segment_from_tile = None
            helper.current_move_segment_to_tile = None
            self._set_in_transit(helper, str(helper.location), logical_destination, 1.0, segment_duration)
            self._clear_in_transit(helper)
            self._transition_humanoid_state(
                helper,
                "primitive_finished",
                step={"step_id": helper.current_step_id or "motion", "call_code": "NAVIGATE_TO"},
                status="completed",
                reason="shared_product_carry",
                source="mansim.transport",
            )
            self._transition_humanoid_state(
                helper,
                "cargo_changed",
                reason="shared_product_carry",
                source="mansim.transport",
                metadata={"cargo_present": True},
            )

    def _set_worker_motion(
        self,
        worker: Worker,
        from_zone: str,
        to_zone: str,
        progress: float,
        total_min: float,
        *,
        path_tiles: list[Tile] | None = None,
        target_tile: Tile | None = None,
    ) -> None:
        worker.in_transit_from = str(from_zone)
        worker.in_transit_to = str(to_zone)
        worker.in_transit_progress = min(1.0, max(0.0, float(progress)))
        worker.in_transit_total_min = max(0.0, float(total_min))
        worker.movement_path = list(path_tiles or [])
        worker.movement_target_tile = target_tile
        self._transition_humanoid_state(
            worker,
            "primitive_started",
            step={"step_id": worker.current_step_id or "motion", "call_code": "NAVIGATE_TO"},
            status="running",
            reason="motion",
            source="mansim.motion",
        )

    def _worker_motion_payload(self, worker: Worker) -> dict[str, Any] | None:
        if not worker.in_transit_from or not worker.in_transit_to:
            return None
        if worker.movement_path and not worker.current_move_id and int(worker.current_move_segment_index or 0) <= 0:
            # Tile-path motion without either an active move id or an active shared-carry
            # segment is stale state left after an interrupted move; do not let Replay
            # interpolate from it and visually teleport the worker.
            return None
        total_min = max(0.0, float(worker.in_transit_total_min))
        if total_min <= 1e-9:
            return None
        progress = min(1.0, max(0.0, float(worker.in_transit_progress)))
        started_at = float(self.env.now) - (progress * total_min)
        return {
            "from": str(worker.in_transit_from),
            "to": str(worker.in_transit_to),
            "progress": progress,
            "total_min": total_min,
            "started_at": round(started_at, 3),
            "ended_at": round(started_at + total_min, 3),
            "from_tile": self._tile_payload(worker.movement_path[0]) if worker.movement_path else self._tile_payload(worker.tile),
            "to_tile": self._tile_payload(worker.movement_target_tile),
            "path_tiles": [self._tile_payload(tile) for tile in worker.movement_path],
            "move_id": worker.current_move_id,
            "segment_index": int(worker.current_move_segment_index or 0),
        }

    def _set_worker_cargo(
        self,
        worker: Worker,
        item_id: str | None,
        item_type: str | None,
        *,
        destination: str = "",
    ) -> None:
        normalized_id = str(item_id).strip() if item_id is not None else None
        normalized_type = str(item_type).strip().lower() if item_type is not None else None
        worker.carrying_item_id = normalized_id or None
        worker.carrying_item_type = normalized_type or None
        worker.carrying_item_ids = [normalized_id] if normalized_id else []
        worker.carrying_item_count = 1 if normalized_id else 0
        worker.carrying_item_max_count = 1
        self._sync_humanoid_cargo_state(worker, destination=destination)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="WORKER_CARGO_CHANGED",
            entity_id=worker.worker_id,
            location=self.worker_display_location(worker),
            details={
                "cargo": self._worker_cargo_payload(worker),
                "humanoid_state": self._humanoid_state_payload(worker),
            },
        )

    def _set_worker_cargo_batch(
        self,
        worker: Worker,
        item_ids: list[str],
        item_type: str,
        *,
        max_item_count: int,
        destination: str = "",
    ) -> None:
        normalized_ids = [str(item_id).strip() for item_id in item_ids if str(item_id).strip()]
        normalized_type = str(item_type).strip().lower()
        worker.carrying_item_id = normalized_ids[0] if normalized_ids else None
        worker.carrying_item_type = normalized_type or None
        worker.carrying_item_ids = normalized_ids
        worker.carrying_item_count = len(normalized_ids)
        worker.carrying_item_max_count = max(1, int(max_item_count or 1))
        self._sync_humanoid_cargo_state(worker, destination=destination)
        for item_id in normalized_ids:
            self._set_item_state(
                item_id,
                ItemState.CARRIED_BY_WORKER,
                location=self.worker_display_location(worker),
                ref=worker.worker_id,
                item_type=normalized_type,
            )
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="WORKER_CARGO_CHANGED",
            entity_id=worker.worker_id,
            location=self.worker_display_location(worker),
            details={
                "cargo": self._worker_cargo_payload(worker),
                "humanoid_state": self._humanoid_state_payload(worker),
            },
        )

    def _set_machine_state(self, machine: Machine, state: MachineState | str, reason: str = "") -> None:
        next_state = state if isinstance(state, MachineState) else MachineState(str(state))
        state_changed = machine.state != next_state
        if machine.state == next_state and not reason:
            return
        machine.state = next_state
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_STATE_CHANGED",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={
                "machine_state": machine.state.value,
                "reason": reason,
                "input_item_id": machine.input_intermediate or machine.input_material,
                "input_material_id": machine.input_material,
                "input_intermediate_id": machine.input_intermediate,
                "setup_ready": bool(getattr(machine, "setup_ready", False)),
                "output_item_id": machine.output_intermediate,
                "active_worker_ids": [candidate for candidate in (machine.setup_owner, machine.unload_owner, machine.pm_owner) if candidate]
                + list(machine.repair_team),
                "repair_team_size": self._repair_team_size(machine),
                "repair_remaining_min": round(float(machine.repair_work_remaining_min), 3),
            },
        )
        if state_changed:
            self._rolling_horizon_request_candidate_refresh("machine_state_changed")

    def _set_item_state(
        self,
        item_id: str,
        state: ItemState | str,
        *,
        location: str = "",
        ref: str = "",
        item_type: str | None = None,
        tile: Tile | None = None,
    ) -> None:
        if not item_id:
            return
        next_state = state if isinstance(state, ItemState) else ItemState(str(state))
        item = self.items.get(item_id)
        if item is None:
            self.items[item_id] = Item(
                item_id=item_id,
                item_type=str(item_type or "unknown"),
                created_at=float(self.env.now),
                state=next_state,
            )
            item = self.items[item_id]
        else:
            item.state = next_state
            if item_type:
                item.item_type = str(item_type)
        if location.startswith("Station"):
            suffix = location.removeprefix("Station")
            if suffix.isdigit():
                item.current_station = int(suffix)
        elif location in {"Warehouse", "BatteryStation", "CompletedProducts", "ScrapDisposal"}:
            item.current_station = None
        if ref:
            item.metadata["state_ref"] = ref
        if tile is not None:
            item.metadata["tile"] = self._tile_payload(tile)
        details = {
            "item_id": item_id,
            "item_type": item.item_type,
            "item_state": item.state.value,
            "ref": ref,
        }
        for key in (
            "source_item_ids",
            "source_material_ids",
            "source_intermediate_ids",
            "transformed_from_item_ids",
            "transformed_to_item_id",
        ):
            value = item.metadata.get(key)
            if isinstance(value, list):
                details[key] = [str(candidate) for candidate in value if str(candidate).strip()]
            elif isinstance(value, str) and value.strip():
                details[key] = value.strip() if key == "transformed_to_item_id" else [value.strip()]
        if str(item.metadata.get("inspection_result") or "").strip():
            details["inspection_result"] = str(item.metadata.get("inspection_result")).strip().upper()
        if tile is not None:
            details["tile"] = self._tile_payload(tile)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="ITEM_STATE_CHANGED",
            entity_id=item_id,
            location=location,
            details=details,
        )

    def _record_initial_batch_terminal_output(
        self,
        item_id: str,
        *,
        outcome: str,
        completed_by_agent_id: str = "",
    ) -> None:
        if not self.minimize_makespan_enabled or not self.initial_batch_material_ids:
            return
        item = self.items.get(str(item_id))
        if item is None:
            return
        source_material_ids = {
            str(source_id)
            for source_id in item.metadata.get("source_material_ids", [])
            if str(source_id).strip()
        }
        covered_material_ids = source_material_ids & self.initial_batch_material_ids
        if not covered_material_ids:
            return

        normalized_outcome = str(outcome).strip().lower()
        if normalized_outcome == "accepted_product":
            self.initial_batch_accepted_output_ids.add(str(item_id))
        elif normalized_outcome == "disposed_scrap":
            self.initial_batch_disposed_scrap_ids.add(str(item_id))
        else:
            raise ValueError(f"Unsupported initial batch terminal outcome: {outcome}")

        newly_terminal = covered_material_ids - self.initial_batch_terminal_material_ids
        self.initial_batch_terminal_material_ids.update(covered_material_ids)
        for material_id in newly_terminal:
            self.initial_batch_terminal_outcomes[material_id] = {
                "outcome": normalized_outcome,
                "output_item_id": str(item_id),
            }
        terminal_count = len(self.initial_batch_terminal_material_ids)
        initial_count = len(self.initial_batch_material_ids)
        progress_ratio = terminal_count / max(1, initial_count)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="INITIAL_MATERIAL_BATCH_PROGRESS",
            entity_id=str(item_id),
            location="CompletedProducts" if normalized_outcome == "accepted_product" else "ScrapDisposal",
            details={
                "outcome": normalized_outcome,
                "covered_material_ids": sorted(covered_material_ids),
                "newly_terminal_material_ids": sorted(newly_terminal),
                "terminal_material_count": terminal_count,
                "initial_material_count": initial_count,
                "progress_ratio": round(progress_ratio, 6),
            },
        )
        if terminal_count == initial_count and self.makespan_min is None:
            self.makespan_min = float(self.env.now)
            self._request_simulation_termination(
                "initial_material_batch_terminal_complete",
                after_agent_id=completed_by_agent_id,
            )

    def _register_dropped_item(
        self,
        item_id: str,
        *,
        item_type: str,
        tile: Tile | None,
        dropped_by: str,
        destination: str,
        logical_destination: str,
        move_id: str,
    ) -> None:
        """Expose a dropped item as a recoverable floor pickup target.

        HumanoidSim defines the incident and recovery protocol. ManSim owns the
        environment fact that the item is physically available at a tile.
        """
        if not item_id or tile is None:
            return
        payload = {
            "item_id": str(item_id),
            "item_type": str(item_type or "unknown"),
            "tile": tile,
            "dropped_by": str(dropped_by),
            "destination": str(destination or logical_destination),
            "logical_destination": str(logical_destination or destination),
            "move_id": str(move_id or ""),
            "dropped_at": round(float(self.env.now), 3),
        }
        self.dropped_items[str(item_id)] = payload
        item = self.items.get(str(item_id))
        if item is not None:
            item.metadata.update(
                {
                    "dropped_tile": self._tile_payload(tile),
                    "dropped_by": str(dropped_by),
                    "dropped_at": payload["dropped_at"],
                    "recovery_destination": payload["destination"],
                }
            )
        if self.grid_map is not None:
            self.grid_map.service_tiles[str(item_id)] = [tile]

    def _clear_dropped_item(self, item_id: str) -> None:
        if not item_id:
            return
        self.dropped_items.pop(str(item_id), None)
        if self.grid_map is not None:
            self.grid_map.service_tiles.pop(str(item_id), None)
        item = self.items.get(str(item_id))
        if item is not None:
            for key in ("dropped_tile", "dropped_by", "dropped_at", "recovery_destination"):
                item.metadata.pop(key, None)

    def _next_item_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self.item_counter)}"

    def _next_task_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self.task_counter):06d}"

    def _next_task_id_for_task_code(self, task_code: str) -> str:
        prefix = TASK_ID_PREFIX_BY_TASK_CODE.get(str(task_code or "").strip().upper(), "TASK")
        return self._next_task_id(prefix)

    @staticmethod
    def _sync_task_instance_id(task: Task) -> None:
        task_code = str(task.task_code or "").strip().upper()
        if not task_code:
            return
        task.instance_id = f"{task.task_id}:{task_code}"
        task.assigned_robot_id = str(task.assigned_robot_id or "")
        if isinstance(task.humanoid, dict):
            task.humanoid["instance_id"] = task.instance_id
            task.humanoid["task_code"] = task_code

    def _next_cycle_id(self) -> str:
        return f"CYCLE-{next(self.machine_cycle_counter)}"

    def _set_agent_carrying(self, agent: Worker, item_type: str, item_id: str) -> bool:
        normalized_type = str(item_type).strip().lower()
        if not normalized_type:
            return False
        normalized_item_id = str(item_id)
        if agent.carrying_item_id == normalized_item_id and agent.carrying_item_type == normalized_type:
            return True
        if agent.carrying_item_id is not None or agent.carrying_item_type is not None:
            # One-slot carry rule: must drop current item before picking another.
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="AGENT_PICK_REJECTED",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={
                    "reason": "already_carrying",
                    "current_item_id": agent.carrying_item_id or "",
                    "current_item_type": agent.carrying_item_type or "",
                    "attempt_item_id": normalized_item_id,
                    "attempt_item_type": normalized_type,
                },
            )
            return False
        if self._humanoid_incident_enabled("GRIP_FAILED"):
            probability = self._random_incident_probability("GRIP_FAILED")
            if probability > 0.0 and self._humanoid_incident_random(agent.agent_id, "GRIP_FAILED") < probability:
                self._emit_humanoid_incident(
                    agent,
                    "GRIP_FAILED",
                    task=self._current_task_stub(agent),
                    primitive_call_code="GRASP",
                    source="mansim.random_incident",
                    context={
                        "item_id": normalized_item_id,
                        "item_type": normalized_type,
                        "random_probability": probability,
                    },
                )
                return False
        self._set_worker_cargo(agent, normalized_item_id, normalized_type)
        self._set_item_state(
            normalized_item_id,
            ItemState.CARRIED_BY_WORKER,
            location=self.agent_display_location(agent),
            ref=agent.agent_id,
            item_type=normalized_type,
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="AGENT_PICK_ITEM",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={
                "item_id": agent.carrying_item_id,
                "item_type": agent.carrying_item_type,
                "humanoid_state": self._humanoid_state_payload(agent),
            },
        )
        return True

    def _clear_agent_carrying(self, agent: Worker, destination: str = "", emit_event: bool = True) -> None:
        item_id = agent.carrying_item_id
        item_type = agent.carrying_item_type
        item_ids = [str(candidate) for candidate in getattr(agent, "carrying_item_ids", []) if str(candidate)]
        if item_id is None and item_type is None and not item_ids:
            return
        session = self._transport_session_for_worker(agent)
        if session is not None and str(item_id or "") == str(session.get("item_id", "")):
            self._complete_product_transport_session(session, destination=destination, outcome="completed")
            return
        if emit_event:
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="AGENT_DROP_ITEM",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={
                    "item_id": item_id or "",
                    "item_ids": item_ids,
                    "item_count": len(item_ids),
                    "item_type": (item_type or ""),
                    "to": destination,
                    "humanoid_state": self._humanoid_state_payload(agent),
                },
        )
        self._set_worker_cargo(agent, None, None, destination=destination)

    @staticmethod
    def _reserved_item_ids_from_payload(payload: dict[str, Any] | None) -> set[str]:
        if not isinstance(payload, dict):
            return set()
        ids: set[str] = set()
        scalar_keys = (
            "item_id",
            "material_id",
            "intermediate_id",
            "transfer_item_id",
            "material_item_id",
            "transfer_intermediate_id",
            "inspection_product_id",
            "scrap_item_id",
        )
        list_keys = ("item_ids", "_reserved_item_ids")
        for key in scalar_keys:
            value = str(payload.get(key, "") or "").strip()
            if value:
                ids.add(value)
        for key in list_keys:
            values = payload.get(key)
            if not isinstance(values, list):
                continue
            for value in values:
                item_id = str(value or "").strip()
                if item_id:
                    ids.add(item_id)
        return ids

    def _active_task_reserved_item_ids(self, *, exclude_task_id: str = "") -> set[str]:
        """Items already committed to a running worker task.

        Rolling-horizon queues can be re-scanned while another worker is still
        executing a task whose concrete item has not yet left its source queue.
        Treat that active task payload as a reservation so another opportunity
        cannot briefly claim the same item before the stale check catches up.
        """
        excluded_task = str(exclude_task_id or "").strip()
        ids: set[str] = set()
        for agent in self.agents.values():
            current_task_id = str(getattr(agent, "current_task_id", "") or "").strip()
            if not current_task_id or (excluded_task and current_task_id == excluded_task):
                continue
            payload = getattr(agent, "current_task_payload", {}) or {}
            ids.update(self._reserved_item_ids_from_payload(payload))
        return ids

    def _item_reserved_by_other(self, item_id: str, agent_id: str = "", task_id: str = "") -> bool:
        item_id = str(item_id or "").strip()
        if not item_id:
            return False
        reservations = getattr(self, "item_reservations", {})
        reservation = reservations.get(item_id) if isinstance(reservations, dict) else None
        if isinstance(reservation, dict):
            owner_task = str(reservation.get("task_id", "") or "").strip()
            if task_id and owner_task == str(task_id).strip():
                return False
            return True
        if item_id in self._active_task_reserved_item_ids(exclude_task_id=task_id):
            return True
        return False

    def _reserve_item_for_task(
        self,
        agent: Agent,
        task: Task,
        item_id: str,
        *,
        source: str,
        ref: str = "",
        item_type: str = "",
    ) -> bool:
        item_id = str(item_id or "").strip()
        if not item_id:
            return True
        task_id = str(getattr(task, "task_id", "") or "").strip()
        if not hasattr(self, "item_reservations") or not isinstance(self.item_reservations, dict):
            self.item_reservations = {}
        if self._item_reserved_by_other(item_id, agent.agent_id, task_id):
            return False
        self.item_reservations[item_id] = {
            "item_id": item_id,
            "agent_id": agent.agent_id,
            "task_id": task_id,
            "task_type": str(getattr(task, "task_type", "") or ""),
            "task_code": str(getattr(task, "task_code", "") or ""),
            "source": source,
            "ref": ref,
            "item_type": item_type,
            "reserved_at": float(self.env.now),
        }
        reserved = task.payload.get("_reserved_item_ids")
        if not isinstance(reserved, list):
            reserved = []
        if item_id not in reserved:
            reserved.append(item_id)
        task.payload["_reserved_item_ids"] = reserved
        task.payload["_reservation_owner_id"] = agent.agent_id
        return True

    def _task_item_reservation_refs(self, task: Task) -> list[dict[str, str]]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        refs: list[dict[str, str]] = []

        def _add(item_id: Any, *, source: str, ref: Any = "", item_type: str = "") -> None:
            value = str(item_id or "").strip()
            if not value:
                return
            refs.append(
                {
                    "item_id": value,
                    "source": source,
                    "ref": str(ref or "").strip(),
                    "item_type": item_type,
                }
            )

        task_type = str(task.task_type or "").strip().upper()
        if task_type == "TRANSFER":
            transfer_kind = str(payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "material_supply":
                _add(
                    payload.get("transfer_item_id") or payload.get("material_item_id"),
                    source="warehouse_material_shelf",
                    ref=payload.get("source_slot_id"),
                    item_type="material",
                )
            elif transfer_kind == "inter_station":
                try:
                    from_station_value = int(payload.get("from_station", 0) or 0)
                except (TypeError, ValueError):
                    from_station_value = 0
                _add(
                    payload.get("transfer_item_id") or payload.get("transfer_intermediate_id"),
                    source=f"output_buffer_station_{payload.get('from_station', '')}",
                    ref=payload.get("from_station"),
                    item_type="product" if from_station_value >= self.last_processing_station else "intermediate",
                )
        elif task_type == "LOAD_MACHINE":
            station = str(payload.get("station", "") or "")
            load_slot = str(payload.get("load_slot", "") or "").strip().lower()
            item_type = "intermediate" if load_slot == "intermediate" else "material"
            source = f"{item_type}_queue_{station}"
            _add(
                payload.get("item_id") or payload.get("material_id") or payload.get("intermediate_id"),
                source=source,
                ref=station,
                item_type=item_type,
            )
        elif task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            action = str(payload.get("interface_action") or payload.get("action") or "").strip().lower()
            _add(
                payload.get("inspection_product_id") or payload.get("item_id"),
                source=(
                    f"intermediate_queue_{self.inspection_queue_station}"
                    if action == "load"
                    else "inspection_desk"
                ),
                ref=(self.inspection_queue_station if action == "load" else self.inspection_workstation_id),
                item_type="product",
            )
        elif task_type == "INSPECT_PRODUCT":
            _add(
                payload.get("inspection_product_id"),
                source="inspection_desk",
                ref=self.inspection_workstation_id,
                item_type="product",
            )
        elif task_type == "COLLECT_WASTE_OR_SCRAP":
            item_ids = payload.get("item_ids")
            if isinstance(item_ids, list):
                for item_id in item_ids:
                    _add(item_id, source="inspection_scrap_queue", ref="inspection_scrap_queue", item_type="product")
        return refs

    def _task_item_dependencies_available(self, task: Task, agent: Agent) -> bool:
        payload = task.payload if isinstance(task.payload, dict) else {}
        task_type = str(task.task_type or "").strip().upper()
        machine_task_types = {
            "LOAD_MACHINE",
            "SETUP_MACHINE",
            "UNLOAD_MACHINE",
            "REPAIR_MACHINE",
            "PREVENTIVE_MAINTENANCE",
        }
        if task_type in machine_task_types:
            machine = self.machines.get(str(payload.get("machine_id", "") or ""))
            if machine is None:
                return False
            if task_type == "REPAIR_MACHINE":
                if not machine.broken:
                    return False
            elif machine.broken:
                return False
        destination_buffer_id = self._task_destination_buffer_id(task)
        if (
            self._buffer_capacity(destination_buffer_id) is not None
            and str(task.task_id or "") not in self.buffer_slot_reservations.get(destination_buffer_id, {})
            and self._buffer_available_slots(destination_buffer_id) <= 0
        ):
            return False
        for ref in self._task_item_reservation_refs(task):
            item_id = str(ref.get("item_id", "")).strip()
            if not item_id or self._item_reserved_by_other(item_id, agent.agent_id, task.task_id):
                return False
            source = str(ref.get("source", "")).strip()
            if source == "warehouse_material_shelf":
                slot_id = str(payload.get("source_slot_id") or ref.get("ref", "")).strip()
                slot = self.warehouse_material_shelf_slots.get(slot_id) if slot_id else None
                if not isinstance(slot, dict) or str(slot.get("material_item_id", "") or "") != item_id:
                    return False
            elif source.startswith("material_queue_"):
                try:
                    station = int(ref.get("ref") or payload.get("station"))
                except (TypeError, ValueError):
                    return False
                if item_id not in self.material_queues.get(station, deque()):
                    return False
            elif source.startswith("intermediate_queue_"):
                try:
                    station = int(ref.get("ref") or payload.get("station") or self.inspection_queue_station)
                except (TypeError, ValueError):
                    return False
                if item_id not in self.intermediate_queues.get(station, deque()):
                    return False
            elif source.startswith("output_buffer_station_"):
                try:
                    station = int(ref.get("ref") or payload.get("from_station"))
                except (TypeError, ValueError):
                    return False
                if item_id not in self.output_buffers.get(station, deque()):
                    return False
            elif source == "inspection_desk":
                if item_id != self.inspection_desk_item_id:
                    return False
            elif source == "inspection_scrap_queue":
                if item_id not in self.inspection_scrap_queue:
                    return False
        return True

    def _reserve_task_items(self, agent: Agent, task: Task) -> bool:
        if not self._task_item_dependencies_available(task, agent):
            return False
        reserved_now: list[str] = []
        for ref in self._task_item_reservation_refs(task):
            if self._reserve_item_for_task(
                agent,
                task,
                ref["item_id"],
                source=ref.get("source", ""),
                ref=ref.get("ref", ""),
                item_type=ref.get("item_type", ""),
            ):
                reserved_now.append(ref["item_id"])
                continue
            for item_id in reserved_now:
                reservation = self.item_reservations.get(item_id)
                if isinstance(reservation, dict) and reservation.get("task_id") == task.task_id:
                    self.item_reservations.pop(item_id, None)
            return False
        return True

    def _release_task_item_reservations(self, task: Task, *, reason: str = "") -> None:
        payload = task.payload if isinstance(task.payload, dict) else {}
        task_id = str(getattr(task, "task_id", "") or "").strip()
        reserved = payload.get("_reserved_item_ids")
        item_ids = [str(item_id) for item_id in reserved if str(item_id)] if isinstance(reserved, list) else []
        if not item_ids and task_id:
            item_ids = [
                item_id
                for item_id, row in list(getattr(self, "item_reservations", {}).items())
                if isinstance(row, dict) and str(row.get("task_id", "") or "") == task_id
            ]
        for item_id in item_ids:
            reservation = getattr(self, "item_reservations", {}).get(item_id)
            if isinstance(reservation, dict) and (not task_id or str(reservation.get("task_id", "") or "") == task_id):
                self.item_reservations.pop(item_id, None)
        payload.pop("_reserved_item_ids", None)
        payload.pop("_reservation_owner_id", None)

    def _reserve_task_domain_owner(self, agent: Agent, task: Task) -> bool:
        payload = task.payload if isinstance(task.payload, dict) else {}
        task_type = str(task.task_type or "").strip().upper()
        owner_kind = ""
        owner_ref = ""
        if task_type == "TRANSFER":
            transfer_kind = str(payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "material_supply":
                try:
                    station = int(payload.get("station", 0) or 0)
                except (TypeError, ValueError):
                    return False
                if not self.is_mfg_flow_shop:
                    owner = self.material_supply_owner.get(station)
                    if owner is not None and owner != agent.agent_id:
                        return False
                    self.material_supply_owner[station] = agent.agent_id
                    owner_kind = "material_supply"
                    owner_ref = str(station)
            elif transfer_kind == "battery_delivery":
                target_id = str(payload.get("target_agent_id", ""))
                target = self.agents.get(target_id)
                if target is None:
                    return False
                if target.battery_service_owner is not None and target.battery_service_owner != agent.agent_id:
                    return False
                if self.active_battery_delivery_owner is not None and self.active_battery_delivery_owner != agent.agent_id:
                    return False
                target.battery_service_owner = agent.agent_id
                self.active_battery_delivery_owner = agent.agent_id
                owner_kind = "battery_delivery"
                owner_ref = target_id
        elif task_type in {"LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            machine = self.machines.get(str(payload.get("machine_id", "")))
            if machine is None:
                return False
            if task_type in {"LOAD_MACHINE", "SETUP_MACHINE"}:
                if machine.setup_owner is not None and machine.setup_owner != agent.agent_id:
                    return False
                machine.setup_owner = agent.agent_id
                owner_kind = "machine_load" if task_type == "LOAD_MACHINE" else "machine_setup"
            elif task_type == "UNLOAD_MACHINE":
                if machine.unload_owner is not None and machine.unload_owner != agent.agent_id:
                    return False
                machine.unload_owner = agent.agent_id
                owner_kind = "machine_unload"
            else:
                if machine.pm_owner is not None and machine.pm_owner != agent.agent_id:
                    return False
                machine.pm_owner = agent.agent_id
                owner_kind = "machine_pm"
            owner_ref = machine.machine_id
        elif task_type in {"INSPECT_PRODUCT", "LOAD_UNLOAD_TRANSFER_INTERFACE"}:
            if self.inspection_owner is not None and self.inspection_owner != agent.agent_id:
                return False
            self.inspection_owner = agent.agent_id
            owner_kind = "inspection_workstation"
            owner_ref = "inspection"
        elif task_type == "COLLECT_WASTE_OR_SCRAP":
            if self.scrap_disposal_owner is not None and self.scrap_disposal_owner != agent.agent_id:
                return False
            self.scrap_disposal_owner = agent.agent_id
            owner_kind = "scrap_disposal"
            owner_ref = "inspection_scrap_queue"
        if owner_kind:
            payload["_reserved_owner"] = {"kind": owner_kind, "ref": owner_ref, "agent_id": agent.agent_id}
        return True

    def _release_task_domain_owner(self, agent: Agent, task: Task, *, reason: str = "") -> None:
        payload = task.payload if isinstance(task.payload, dict) else {}
        owner = payload.pop("_reserved_owner", None)
        if not isinstance(owner, dict):
            return
        kind = str(owner.get("kind", "") or "").strip()
        ref = str(owner.get("ref", "") or "").strip()
        owner_agent = str(owner.get("agent_id", "") or "").strip() or agent.agent_id
        if kind == "material_supply":
            try:
                station = int(ref)
            except (TypeError, ValueError):
                return
            if self.material_supply_owner.get(station) == owner_agent:
                self.material_supply_owner[station] = None
        elif kind == "battery_delivery":
            target = self.agents.get(ref)
            if target is not None and target.battery_service_owner == owner_agent:
                if target.awaiting_battery_from == owner_agent:
                    self._end_battery_swap_wait(target, owner_agent)
                target.battery_service_owner = None
            if self.active_battery_delivery_owner == owner_agent:
                self.active_battery_delivery_owner = None
        elif kind in {"machine_load", "machine_setup"}:
            machine = self.machines.get(ref)
            if machine is not None and machine.setup_owner == owner_agent:
                machine.setup_owner = None
        elif kind == "machine_unload":
            machine = self.machines.get(ref)
            if machine is not None and machine.unload_owner == owner_agent:
                machine.unload_owner = None
        elif kind == "machine_pm":
            machine = self.machines.get(ref)
            if machine is not None and machine.pm_owner == owner_agent:
                machine.pm_owner = None
        elif kind in {"inspection", "inspection_workstation"}:
            self._release_inspection_desk(
                owner_agent,
                task_id=str(getattr(task, "task_id", "") or ""),
                product_id=str(payload.get("inspection_product_id") or payload.get("item_id") or ""),
                reason=reason,
            )
        elif kind == "scrap_disposal":
            if self.scrap_disposal_owner == owner_agent:
                self.scrap_disposal_owner = None

    def _release_inspection_desk(
        self,
        agent_id: str,
        *,
        task_id: str = "",
        product_id: str = "",
        reason: str = "",
    ) -> bool:
        if self.inspection_owner != agent_id:
            return False
        self.inspection_owner = None
        if self.inspection_desk_state == "INSPECTING" and self.inspection_desk_item_id:
            item = self.items.get(self.inspection_desk_item_id)
            result_recorded = bool(
                item is not None and item.metadata.get("inspection_result_recorded", False)
            )
            next_state = "INSPECTED_WAITING_UNLOAD" if result_recorded else "STAGED_FOR_INSPECTION"
            self._set_inspection_desk_state(
                next_state,
                result=(str(item.metadata.get("inspection_result") or "") if result_recorded else None)
                if item is not None
                else None,
                reason=reason or "inspection_released",
            )
        if self.inspection_active_agents <= 0:
            self._rolling_horizon_request_candidate_refresh("inspection_workstation_released")
            return True
        self.inspection_active_agents = max(0, self.inspection_active_agents - 1)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="INSPECTION_DESK_RELEASED",
            entity_id=self.inspection_workstation_id,
            location="Inspection",
            details={
                "worker_id": agent_id,
                "task_id": task_id,
                "product_id": product_id,
                "active_worker_ids": [],
                "reason": str(reason or "inspection_complete"),
                "desk_state": self.inspection_desk_state,
                "inspection_result": self.inspection_desk_result or "",
            },
        )
        self._rolling_horizon_request_candidate_refresh("inspection_workstation_released")
        return True

    def _set_inspection_desk_state(
        self,
        state: str,
        *,
        item_id: str | None = None,
        result: str | None = None,
        reason: str = "",
    ) -> None:
        normalized_state = str(state or "EMPTY").strip().upper()
        if normalized_state not in {
            "EMPTY",
            "STAGED_FOR_INSPECTION",
            "INSPECTING",
            "INSPECTED_WAITING_UNLOAD",
        }:
            raise ValueError(f"Unsupported inspection desk state: {state}")
        normalized_item_id = str(item_id).strip() if item_id else None
        if normalized_state != "EMPTY" and normalized_item_id is None and self.inspection_desk_item_id is None:
            raise ValueError(f"Inspection desk state {normalized_state} requires an item id.")
        normalized_result = str(result or "").strip().upper()
        if normalized_state == "INSPECTED_WAITING_UNLOAD":
            effective_result = normalized_result or str(self.inspection_desk_result or "").strip().upper()
            if effective_result not in {"PASS", "FAIL"}:
                raise ValueError("INSPECTED_WAITING_UNLOAD requires a PASS or FAIL result.")
        previous_state = self.inspection_desk_state
        previous_item_id = self.inspection_desk_item_id
        if item_id is not None or normalized_state == "EMPTY":
            self.inspection_desk_item_id = normalized_item_id
        if result is not None or normalized_state in {"EMPTY", "STAGED_FOR_INSPECTION"}:
            self.inspection_desk_result = normalized_result if normalized_result in {"PASS", "FAIL"} else None
        self.inspection_desk_state = normalized_state
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="INSPECTION_DESK_STATE_CHANGED",
            entity_id=self.inspection_workstation_id,
            location="Inspection",
            details={
                "previous_state": previous_state,
                "desk_state": self.inspection_desk_state,
                "previous_item_id": previous_item_id or "",
                "product_id": self.inspection_desk_item_id or "",
                "inspection_result": self.inspection_desk_result or "",
                "owner_worker_id": self.inspection_owner or "",
                "reason": str(reason or ""),
            },
        )
        self._rolling_horizon_request_candidate_refresh("inspection_desk_state_changed")

    def _first_unreserved_queue_item(
        self,
        queue: deque[str],
        agent_id: str = "",
        task_id: str = "",
        exclude_item_ids: set[str] | None = None,
    ) -> str | None:
        excluded = exclude_item_ids or set()
        for item_id in list(queue):
            value = str(item_id or "").strip()
            if value in excluded:
                continue
            if value and not self._item_reserved_by_other(value, agent_id, task_id):
                return value
        return None

    def _unreserved_queue_items(
        self,
        queue: deque[str],
        count: int,
        agent_id: str = "",
        task_id: str = "",
        exclude_item_ids: set[str] | None = None,
    ) -> list[str]:
        limit = max(1, int(count or 1))
        items: list[str] = []
        excluded = exclude_item_ids or set()
        for item_id in list(queue):
            value = str(item_id or "").strip()
            if value in excluded:
                continue
            if value and not self._item_reserved_by_other(value, agent_id, task_id):
                items.append(value)
                if len(items) >= limit:
                    break
        return items

    @staticmethod
    def _remove_item_from_deque(queue: deque[str], item_id: str) -> bool:
        item_id = str(item_id or "").strip()
        if not item_id:
            return False
        try:
            queue.remove(item_id)
            return True
        except ValueError:
            return False

    def _buffer_queue(self, buffer_id: str) -> deque[str] | None:
        normalized = str(buffer_id or "").strip()
        if normalized.startswith("material_queue_"):
            try:
                return self.material_queues.get(int(normalized.rsplit("_", 1)[1]))
            except (TypeError, ValueError):
                return None
        if normalized.startswith("intermediate_queue_"):
            try:
                return self.intermediate_queues.get(int(normalized.rsplit("_", 1)[1]))
            except (TypeError, ValueError):
                return None
        if normalized.startswith("output_buffer_station_"):
            try:
                return self.output_buffers.get(int(normalized.rsplit("_", 1)[1]))
            except (TypeError, ValueError):
                return None
        if normalized == "inspection_output_queue":
            return self.output_buffers.get(self.inspection_queue_station)
        if normalized == "inspection_scrap_queue":
            return self.inspection_scrap_queue
        return None

    def _buffer_id_for_queue(self, queue: deque[str]) -> str:
        for station, candidate in self.material_queues.items():
            if candidate is queue:
                return f"material_queue_{station}"
        for station, candidate in self.intermediate_queues.items():
            if candidate is queue:
                return f"intermediate_queue_{station}"
        for station, candidate in self.output_buffers.items():
            if candidate is queue:
                return f"output_buffer_station_{station}"
        if self.inspection_scrap_queue is queue:
            return "inspection_scrap_queue"
        return ""

    def _buffer_capacity(self, buffer_id: str) -> int | None:
        value = self.buffer_capacities.get(str(buffer_id or "").strip())
        return int(value) if value is not None else None

    def _rolling_horizon_queued_resource_keys(self) -> set[str]:
        excluded_keys = set(
            getattr(self, "_rolling_horizon_candidate_slot_exclusions", set()) or set()
        )
        keys: set[str] = set()
        for queue in getattr(self, "rolling_horizon_dispatch_queues", {}).values():
            for entry in queue:
                if not isinstance(entry, dict):
                    continue
                for key in entry.get("exclusive_resource_keys", []):
                    value = str(key or "")
                    if value in excluded_keys:
                        continue
                    if value:
                        keys.add(value)
        return keys

    def _queued_buffer_slot_tokens(self, buffer_id: str) -> set[int]:
        prefix = f"buffer_slot:{str(buffer_id or '').strip()}:"
        tokens: set[int] = set()
        for value in self._rolling_horizon_queued_resource_keys():
            if not value.startswith(prefix):
                continue
            try:
                tokens.add(int(value.rsplit(":", 1)[1]))
            except (TypeError, ValueError):
                continue
        return tokens

    def _buffer_reserved_count(self, buffer_id: str, *, exclude_task_id: str = "") -> int:
        rows = self.buffer_slot_reservations.get(str(buffer_id or "").strip(), {})
        excluded = str(exclude_task_id or "").strip()
        active = sum(1 for task_id in rows if not excluded or str(task_id) != excluded)
        return active + len(self._queued_buffer_slot_tokens(buffer_id))

    def _buffer_available_slots(self, buffer_id: str, *, exclude_task_id: str = "") -> int:
        capacity = self._buffer_capacity(buffer_id)
        if capacity is None:
            return 1_000_000_000
        queue = self._buffer_queue(buffer_id)
        occupancy = len(queue) if queue is not None else 0
        reserved = self._buffer_reserved_count(buffer_id, exclude_task_id=exclude_task_id)
        return max(0, int(capacity) - int(occupancy) - int(reserved))

    def _buffer_available_slot_tokens(self, buffer_id: str, *, limit: int | None = None) -> list[int]:
        capacity = self._buffer_capacity(buffer_id)
        if capacity is None:
            count = max(1, int(limit or 1))
            return list(range(count))
        available = self._buffer_available_slots(buffer_id)
        if limit is not None:
            available = min(available, max(0, int(limit)))
        used_tokens = {
            int(row.get("slot_token", -1))
            for row in self.buffer_slot_reservations.get(buffer_id, {}).values()
            if int(row.get("slot_token", -1)) >= 0
        }
        used_tokens.update(self._queued_buffer_slot_tokens(buffer_id))
        tokens = [token for token in range(capacity) if token not in used_tokens]
        return tokens[:available]

    def _task_destination_buffer_id(self, task: Task) -> str:
        payload = task.payload if isinstance(task.payload, dict) else {}
        explicit = str(payload.get("destination_buffer_id", "") or "").strip()
        if explicit:
            return explicit
        task_type = str(task.task_type or "").strip().upper()
        if task_type == "UNLOAD_MACHINE":
            machine = self.machines.get(str(payload.get("machine_id", "") or ""))
            return f"output_buffer_station_{machine.station}" if machine is not None else ""
        if task_type == "TRANSFER":
            transfer_kind = str(payload.get("transfer_kind", "") or "").strip().lower()
            if transfer_kind == "material_supply":
                return f"material_queue_{int(payload.get('station', 0) or 0)}"
            if transfer_kind == "inter_station":
                from_station = int(payload.get("from_station", 0) or 0)
                if from_station == self.inspection_queue_station:
                    return ""
                target_station = from_station + 1
                if target_station > self.last_processing_station:
                    target_station = self.inspection_queue_station
                return f"intermediate_queue_{target_station}"
        if task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            action = str(payload.get("interface_action") or payload.get("action") or "").strip().lower()
            if action == "unload":
                result = str(payload.get("inspection_result", "") or "").strip().upper()
                return (
                    f"output_buffer_station_{self.inspection_queue_station}"
                    if result == "PASS"
                    else "inspection_scrap_queue"
                )
        return ""

    def _reserve_task_buffer_slot(self, agent: Agent, task: Task) -> bool:
        buffer_id = self._task_destination_buffer_id(task)
        capacity = self._buffer_capacity(buffer_id)
        if not buffer_id or capacity is None:
            return True
        task_id = str(task.task_id or "").strip()
        existing = self.buffer_slot_reservations[buffer_id].get(task_id)
        if isinstance(existing, dict):
            return True
        preferred = task.payload.get("destination_slot_token")
        available_tokens = self._buffer_available_slot_tokens(buffer_id)
        try:
            preferred_token = int(preferred) if preferred is not None else None
        except (TypeError, ValueError):
            preferred_token = None
        if preferred_token in available_tokens:
            slot_token = int(preferred_token)
        elif available_tokens:
            slot_token = int(available_tokens[0])
        else:
            self.buffer_metrics["reservation_failure_count"] += 1
            task.payload["failure_reason"] = "destination_buffer_full"
            return False
        row = {
            "task_id": task_id,
            "agent_id": agent.agent_id,
            "buffer_id": buffer_id,
            "slot_token": slot_token,
            "reserved_at": float(self.env.now),
        }
        self.buffer_slot_reservations[buffer_id][task_id] = row
        task.payload["destination_buffer_id"] = buffer_id
        task.payload["destination_slot_token"] = slot_token
        task.payload["_reserved_buffer_slot"] = dict(row)
        self.buffer_metrics["max_reserved_by_buffer"][buffer_id] = max(
            int(self.buffer_metrics["max_reserved_by_buffer"].get(buffer_id, 0)),
            len(self.buffer_slot_reservations[buffer_id]),
        )
        self._record_buffer_occupancy(buffer_id)
        return True

    def _release_task_buffer_slot(self, task: Task, *, reason: str = "") -> None:
        payload = task.payload if isinstance(task.payload, dict) else {}
        row = payload.pop("_reserved_buffer_slot", None)
        buffer_id = str(
            (row.get("buffer_id") if isinstance(row, dict) else None)
            or payload.get("destination_buffer_id")
            or ""
        ).strip()
        task_id = str(task.task_id or "").strip()
        if buffer_id and task_id and self.buffer_slot_reservations.get(buffer_id, {}).pop(task_id, None) is not None:
            self.buffer_metrics["reservation_release_count"] += 1
        if buffer_id and not self.buffer_slot_reservations.get(buffer_id):
            self.buffer_slot_reservations.pop(buffer_id, None)

    def _commit_task_buffer_slot(self, task: Task, buffer_id: str) -> bool:
        normalized = str(buffer_id or "").strip()
        task_id = str(task.task_id or "").strip()
        row = self.buffer_slot_reservations.get(normalized, {}).pop(task_id, None)
        if row is None and self._buffer_capacity(normalized) is not None:
            return False
        if normalized and not self.buffer_slot_reservations.get(normalized):
            self.buffer_slot_reservations.pop(normalized, None)
        if isinstance(task.payload, dict):
            task.payload.pop("_reserved_buffer_slot", None)
        if row is not None:
            self.buffer_metrics["reservation_commit_count"] += 1
        return True

    def _task_has_buffer_reservation(self, task: Task | None, buffer_id: str) -> bool:
        return bool(
            task is not None
            and str(task.task_id or "") in self.buffer_slot_reservations.get(buffer_id, {})
        )

    def _record_buffer_occupancy(self, buffer_id: str) -> None:
        queue = self._buffer_queue(buffer_id)
        if queue is None:
            return
        self.buffer_metrics["max_occupancy_by_buffer"][buffer_id] = max(
            int(self.buffer_metrics["max_occupancy_by_buffer"].get(buffer_id, 0)),
            len(queue),
        )
        committed_plus_reserved = len(queue) + self._buffer_reserved_count(buffer_id)
        self.buffer_metrics["max_committed_plus_reserved_by_buffer"][buffer_id] = max(
            int(self.buffer_metrics["max_committed_plus_reserved_by_buffer"].get(buffer_id, 0)),
            committed_plus_reserved,
        )
        capacity = self._buffer_capacity(buffer_id)
        if capacity is not None and committed_plus_reserved > capacity:
            raise RuntimeError(
                f"Finite buffer invariant violated for {buffer_id}: "
                f"occupancy+reservations={committed_plus_reserved} capacity={capacity}."
            )

    def _buffer_push_allowed(self, buffer_id: str, *, task: Task | None = None) -> bool:
        capacity = self._buffer_capacity(buffer_id)
        if capacity is None:
            return True
        queue = self._buffer_queue(buffer_id)
        occupancy = len(queue) if queue is not None else 0
        own_reservation = bool(
            task is not None
            and str(task.task_id or "") in self.buffer_slot_reservations.get(buffer_id, {})
        )
        if own_reservation:
            return occupancy < capacity
        return occupancy + self._buffer_reserved_count(buffer_id) < capacity

    def _log_buffer_overflow_attempt(self, buffer_id: str, item_id: str, task: Task | None = None) -> None:
        self.buffer_metrics["overflow_attempt_count"] += 1
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="BUFFER_OVERFLOW_PREVENTED",
            entity_id=buffer_id,
            location=self._grid_logical_destination(buffer_id),
            details={
                "item_id": str(item_id or ""),
                "task_id": str(getattr(task, "task_id", "") or ""),
                "capacity": self._buffer_capacity(buffer_id),
                "occupancy": len(self._buffer_queue(buffer_id) or ()),
                "reserved": self._buffer_reserved_count(buffer_id),
            },
        )

    def _appendleft_if_absent(self, queue: deque[str], item_id: str | None) -> None:
        value = str(item_id or "").strip()
        if value and value not in queue:
            buffer_id = self._buffer_id_for_queue(queue)
            if buffer_id and not self._buffer_push_allowed(buffer_id):
                self._log_buffer_overflow_attempt(buffer_id, value)
                raise RuntimeError(f"Cannot roll back {value}: finite buffer {buffer_id} is full.")
            queue.appendleft(value)
            if buffer_id:
                self._record_buffer_occupancy(buffer_id)

    def _finalize_selected_task(self, agent: Agent, task: Task | None) -> Task | None:
        if task is None:
            return None
        if self.expose_battery_risk_metadata:
            risk_metadata = self._task_battery_risk_metadata(agent, task)
            task.payload.update(risk_metadata)
            task.selection_meta["battery_risk"] = copy.deepcopy(risk_metadata)
        if self._mfg_flow_policy_active():
            rule = self._mfg_flow_task_rule(task)
            allowed_worker_ids = self._mfg_flow_allowed_worker_ids_for_task(task)
            if self._mfg_flow_dedicated_policy_active() and agent.agent_id not in allowed_worker_ids:
                self.rolling_horizon_dedicated_role_metrics["role_violation_count"] += 1
                task.payload["failure_reason"] = "ROLE_VIOLATION"
                return None
            task.selection_meta["task_rule_id"] = str(rule.rule_id)
            task.selection_meta["role_number"] = int(rule.role_number)
            task.selection_meta["role_task_code"] = str(rule.task_code)
            task.selection_meta["role_display_name"] = str(rule.display_name)
            task.selection_meta["role_policy"] = (
                "workload_balanced_dedicated" if self._mfg_flow_dedicated_policy_active() else "shared"
            )
            task.selection_meta["allowed_worker_ids"] = list(allowed_worker_ids)
            task.selection_meta["role_owner_agent_id"] = (
                agent.agent_id
                if rule.kind == "self_service"
                else self.mfg_flow_task_policy.exclusive_owner_by_rule.get(rule.rule_id, "")
            )
        if agent.suspended_task is task and isinstance(task.payload.get("_reserved_item_ids"), list):
            return task
        if not self._reserve_task_domain_owner(agent, task):
            task.payload["failure_reason"] = "RESOURCE_PREEMPTED"
            return None
        if not self._reserve_task_items(agent, task):
            self._release_task_domain_owner(agent, task, reason="item_reservation_failed")
            task.payload["failure_reason"] = "RESOURCE_PREEMPTED"
            return None
        if not self._reserve_task_buffer_slot(agent, task):
            self._release_task_item_reservations(task, reason="buffer_reservation_failed")
            self._release_task_domain_owner(agent, task, reason="buffer_reservation_failed")
            return None
        return task

    def _push_material_queue(self, station: int, item_id: str, *, task: Task | None = None) -> bool:
        buffer_id = f"material_queue_{station}"
        if not self._buffer_push_allowed(buffer_id, task=task):
            self._log_buffer_overflow_attempt(buffer_id, item_id, task)
            return False
        if self._task_has_buffer_reservation(task, buffer_id) and not self._commit_task_buffer_slot(task, buffer_id):
            self._log_buffer_overflow_attempt(buffer_id, item_id, task)
            return False
        self.material_queues[station].append(item_id)
        self._record_buffer_occupancy(buffer_id)
        self._set_item_state(item_id, ItemState.IN_QUEUE, location=f"Station{station}", ref=f"material_queue_{station}", item_type="material")
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_PUSH",
            entity_id=f"material_queue_{station}",
            location=f"Station{station}",
            details={"item_id": item_id, "queue": "material"},
        )

        self._rolling_horizon_request_candidate_refresh("material_queue_push")
        return True

    def _pop_material_queue(self, station: int, item_id: str | None = None) -> str | None:
        if not self.material_queues[station]:
            return None
        if item_id is None or not str(item_id).strip():
            item_id = self._first_unreserved_queue_item(self.material_queues[station])
        if item_id is None or not str(item_id).strip():
            return None
        item_id = str(item_id).strip()
        if not self._remove_item_from_deque(self.material_queues[station], item_id):
            return None
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_POP",
            entity_id=f"material_queue_{station}",
            location=f"Station{station}",
            details={"item_id": item_id, "queue": "material"},
        )
        self._rolling_horizon_request_candidate_refresh("material_queue_pop")
        return item_id

    def _push_inspection_scrap_queue(self, item_id: str, *, task: Task | None = None) -> bool:
        if not item_id:
            return False
        if item_id in self.inspection_scrap_queue:
            return True
        buffer_id = "inspection_scrap_queue"
        if not self._buffer_push_allowed(buffer_id, task=task):
            self._log_buffer_overflow_attempt(buffer_id, item_id, task)
            return False
        if self._task_has_buffer_reservation(task, buffer_id) and not self._commit_task_buffer_slot(task, buffer_id):
            self._log_buffer_overflow_attempt(buffer_id, item_id, task)
            return False
        self.inspection_scrap_queue.append(item_id)
        self._record_buffer_occupancy(buffer_id)
        self._set_item_state(
            item_id,
            ItemState.WAITING_SCRAP_DISPOSAL,
            location="Inspection",
            ref="inspection_scrap_queue",
            item_type="product",
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="INSPECTION_SCRAP_QUEUED",
            entity_id=item_id,
            location="Inspection",
            details={
                "queue_id": "inspection_scrap_queue",
                "queue_length": len(self.inspection_scrap_queue),
            },
        )
        self._rolling_horizon_request_candidate_refresh("inspection_scrap_queue_push")
        return True

    def _pop_inspection_scrap_batch(self, max_count: int, item_ids: list[str] | None = None) -> list[str]:
        count = max(1, int(max_count or 1))
        if item_ids is None:
            item_ids = self._unreserved_queue_items(self.inspection_scrap_queue, count)
        else:
            item_ids = [str(item_id).strip() for item_id in item_ids if str(item_id).strip()][:count]
            if any(item_id not in self.inspection_scrap_queue for item_id in item_ids):
                return []
        popped: list[str] = []
        for item_id in item_ids:
            if not self._remove_item_from_deque(self.inspection_scrap_queue, item_id):
                continue
            popped.append(item_id)
        if popped:
            self._rolling_horizon_request_candidate_refresh("inspection_scrap_queue_pop")
        return popped

    def _push_intermediate_queue(self, station: int, item_id: str, *, task: Task | None = None) -> bool:
        if station not in self.intermediate_queues:
            raise ValueError(f"intermediate queue for station {station} is not defined")
        buffer_id = f"intermediate_queue_{station}"
        if not self._buffer_push_allowed(buffer_id, task=task):
            self._log_buffer_overflow_attempt(buffer_id, item_id, task)
            return False
        if self._task_has_buffer_reservation(task, buffer_id) and not self._commit_task_buffer_slot(task, buffer_id):
            self._log_buffer_overflow_attempt(buffer_id, item_id, task)
            return False
        self.intermediate_queues[station].append(item_id)
        self._record_buffer_occupancy(buffer_id)
        location = "Inspection" if station == self.inspection_queue_station else f"Station{station}"
        queue_name = "product" if station == self.inspection_queue_station else "intermediate"
        item_state = ItemState.WAITING_INSPECTION if station == self.inspection_queue_station else ItemState.IN_QUEUE
        self._set_item_state(item_id, item_state, location=location, ref=f"intermediate_queue_{station}", item_type=queue_name)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_PUSH",
            entity_id=f"intermediate_queue_{station}",
            location=location,
            details={"item_id": item_id, "queue": queue_name},
        )
        self._rolling_horizon_request_candidate_refresh("intermediate_queue_push")
        return True

    def _pop_intermediate_queue(self, station: int, item_id: str | None = None) -> str | None:
        if station not in self.intermediate_queues:
            return None
        if not self.intermediate_queues[station]:
            return None
        if item_id is None or not str(item_id).strip():
            item_id = self._first_unreserved_queue_item(self.intermediate_queues[station])
        if item_id is None or not str(item_id).strip():
            return None
        item_id = str(item_id).strip()
        if not self._remove_item_from_deque(self.intermediate_queues[station], item_id):
            return None
        location = "Inspection" if station == self.inspection_queue_station else f"Station{station}"
        queue_name = "product" if station == self.inspection_queue_station else "intermediate"
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_POP",
            entity_id=f"intermediate_queue_{station}",
            location=location,
            details={"item_id": item_id, "queue": queue_name},
        )
        self._rolling_horizon_request_candidate_refresh("intermediate_queue_pop")
        return item_id

    def _pop_output_buffer_item(self, station: int, item_id: str | None = None) -> str | None:
        buffer = self.output_buffers.get(station)
        if buffer is None or not buffer:
            return None
        if item_id is None or not str(item_id).strip():
            item_id = self._first_unreserved_queue_item(buffer)
        if item_id is None or not str(item_id).strip():
            return None
        item_id = str(item_id).strip()
        if not self._remove_item_from_deque(buffer, item_id):
            return None
        location = "Inspection" if station == self.inspection_queue_station else f"Station{station}"
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_POP",
            entity_id=f"output_buffer_station_{station}",
            location=location,
            details={"item_id": item_id, "queue": "output"},
        )
        self._rolling_horizon_request_candidate_refresh("output_buffer_pop")
        return item_id

    def _push_output_buffer_item(self, station: int, item_id: str, *, task: Task | None = None) -> bool:
        buffer = self.output_buffers.get(station)
        if buffer is None:
            raise ValueError(f"output buffer for station {station} is not defined")
        buffer_id = f"output_buffer_station_{station}"
        if not self._buffer_push_allowed(buffer_id, task=task):
            self._log_buffer_overflow_attempt(buffer_id, item_id, task)
            return False
        if self._task_has_buffer_reservation(task, buffer_id) and not self._commit_task_buffer_slot(task, buffer_id):
            self._log_buffer_overflow_attempt(buffer_id, item_id, task)
            return False
        if item_id not in buffer:
            buffer.append(item_id)
        self._record_buffer_occupancy(buffer_id)
        self._rolling_horizon_request_candidate_refresh("output_buffer_push")
        return True

    def _agent_discharged_intervals(self) -> list[tuple[str, float, float]]:
        active: dict[str, float] = {}
        intervals: list[tuple[str, float, float]] = []
        sim_end = float(self.env.now)
        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            agent_id = str(event.get("entity_id", "")).strip()
            t = float(event.get("t", 0.0) or 0.0)
            if not agent_id:
                continue
            if event_type == "AGENT_DISCHARGED":
                active[agent_id] = t
            elif event_type == "AGENT_RECHARGED":
                start = active.pop(agent_id, None)
                if start is not None and t > start:
                    intervals.append((agent_id, start, t))
        for agent_id, start in active.items():
            if sim_end > start:
                intervals.append((agent_id, start, sim_end))
        return intervals

    def _agent_discharged_metrics(self) -> dict[str, Any]:
        by_agent: dict[str, float] = {agent_id: 0.0 for agent_id in sorted(self.agents.keys())}
        for agent_id, start, end in self._agent_discharged_intervals():
            by_agent[agent_id] = by_agent.get(agent_id, 0.0) + max(0.0, float(end) - float(start))
        total = sum(by_agent.values())
        total_agent_time = max(1.0, float(self.env.now) * max(1, len(self.agents)))
        discharged_ratio = total / total_agent_time
        return {
            "total_min": round(total, 3),
            "avg_min_per_agent": round(total / max(1, len(self.agents)), 3),
            "by_agent": {agent_id: round(float(minutes), 3) for agent_id, minutes in sorted(by_agent.items())},
            "availability_ratio": round(max(0.0, 1.0 - discharged_ratio), 6),
            "discharged_ratio": round(min(1.0, max(0.0, discharged_ratio)), 6),
            "ratio_by_agent": {
                agent_id: round(float(minutes) / max(1.0, float(self.env.now)), 6)
                for agent_id, minutes in sorted(by_agent.items())
            },
        }

    @staticmethod
    def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if not intervals:
            return []
        merged: list[list[float]] = []
        for start, end in sorted((float(start), float(end)) for start, end in intervals if float(end) > float(start)):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        return [(start, end) for start, end in merged]

    @staticmethod
    def _interval_total(intervals: list[tuple[float, float]]) -> float:
        return sum(max(0.0, float(end) - float(start)) for start, end in intervals)

    @staticmethod
    def _interval_overlap_total(
        left: list[tuple[float, float]],
        right: list[tuple[float, float]],
    ) -> float:
        left_merged = ManufacturingWorld._merge_intervals(left)
        right_merged = ManufacturingWorld._merge_intervals(right)
        total = 0.0
        i = 0
        j = 0
        while i < len(left_merged) and j < len(right_merged):
            left_start, left_end = left_merged[i]
            right_start, right_end = right_merged[j]
            overlap_start = max(left_start, right_start)
            overlap_end = min(left_end, right_end)
            if overlap_end > overlap_start:
                total += overlap_end - overlap_start
            if left_end <= right_end:
                i += 1
            else:
                j += 1
        return total

    def _agent_event_intervals(
        self,
        *,
        start_events: set[str],
        end_events: set[str],
    ) -> dict[str, list[tuple[float, float]]]:
        intervals: dict[str, list[tuple[float, float]]] = {agent_id: [] for agent_id in sorted(self.agents.keys())}
        active: dict[str, float] = {}
        sim_end = float(self.env.now)
        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            if event_type not in start_events and event_type not in end_events:
                continue
            agent_id = str(event.get("entity_id", "")).strip()
            if agent_id not in self.agents:
                continue
            t = float(event.get("t", 0.0) or 0.0)
            if event_type in start_events:
                active[agent_id] = t
            elif event_type in end_events:
                start = active.pop(agent_id, None)
                if start is not None and t > start:
                    intervals.setdefault(agent_id, []).append((start, t))
        for agent_id, start in active.items():
            if sim_end > start:
                intervals.setdefault(agent_id, []).append((start, sim_end))
        return {agent_id: self._merge_intervals(rows) for agent_id, rows in intervals.items()}

    def _humanoid_state_time_metrics(self) -> dict[str, Any]:
        axis_values = self._humanoid_state_axis_values()
        axes = tuple(axis_values.keys())
        sim_end = max(0.0, float(self.env.now))
        current: dict[str, dict[str, Any]] = {
            agent_id: default_humanoid_state_payload(agent_id)
            for agent_id in sorted(self.agents.keys())
        }
        last_t: dict[str, float] = {agent_id: 0.0 for agent_id in current}
        totals: dict[str, dict[str, dict[str, float]]] = {
            agent_id: {axis: defaultdict(float) for axis in axes}
            for agent_id in current
        }

        def add_duration(agent_id: str, end_t: float) -> None:
            start_t = last_t.get(agent_id, 0.0)
            duration = max(0.0, float(end_t) - float(start_t))
            if duration <= 0.0:
                return
            state = current.get(agent_id, {})
            for axis in axes:
                value = str(state.get(axis, "") or "").strip() or "UNKNOWN"
                totals[agent_id][axis][value] += duration

        for event in self.logger.events:
            agent_id = str(event.get("entity_id", "")).strip()
            if agent_id not in current:
                continue
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            humanoid_state = details.get("humanoid_state")
            if not isinstance(humanoid_state, dict):
                continue
            event_t = float(event.get("t", 0.0) or 0.0)
            add_duration(agent_id, event_t)
            current[agent_id] = dict(humanoid_state)
            last_t[agent_id] = event_t

        for agent_id in current:
            add_duration(agent_id, sim_end)

        return {
            agent_id: {
                axis: {
                    state: round(float(axis_totals.get(state, 0.0) or 0.0), 3)
                    for state in axis_values.get(axis, sorted(axis_totals.keys()))
                }
                for axis, axis_totals in axis_map.items()
            }
            for agent_id, axis_map in totals.items()
        }

    def _humanoid_state_axis_values(self) -> dict[str, list[str]]:
        """Return HumanoidSim state-axis order so KPI artifacts include zero states."""
        cached = getattr(self, "_humanoid_state_axis_values_cache", None)
        if isinstance(cached, dict) and cached:
            return cached
        fallback = {
            "availability": ["AVAILABLE", "ASSIGNED", "EXECUTING", "WAITING", "BLOCKED", "OFFLINE", "DISABLED"],
            "mobility": ["STATIONARY", "NAVIGATING", "DOCKING"],
            "power": ["POWER_NORMAL", "POWER_LOW", "POWER_CRITICAL", "DEPLETED", "CHARGING"],
            "manipulation": ["FREE", "REACHING", "HOLDING", "PLACING"],
        }
        try:
            from humanoidsim import load_state_schema

            schema = load_state_schema()
            axis_values = {
                str(axis): [str(value) for value in getattr(definition, "states", {}).keys()]
                for axis, definition in getattr(schema, "axes", {}).items()
            }
            if axis_values:
                self._humanoid_state_axis_values_cache = axis_values
                return axis_values
        except Exception:
            pass
        self._humanoid_state_axis_values_cache = fallback
        return fallback

    def _humanoid_state_axis_totals(self, by_worker: dict[str, Any]) -> dict[str, dict[str, float]]:
        axis_values = self._humanoid_state_axis_values()
        axis_totals: dict[str, dict[str, float]] = {
            axis: defaultdict(float)
            for axis in axis_values
        }
        for worker_rows in by_worker.values():
            if not isinstance(worker_rows, dict):
                continue
            for axis, state_rows in worker_rows.items():
                if axis not in axis_totals or not isinstance(state_rows, dict):
                    continue
                for state, minutes in state_rows.items():
                    axis_totals[axis][str(state)] += float(minutes or 0.0)
        return {
            axis: {
                state: round(float(rows.get(state, 0.0) or 0.0), 3)
                for state in axis_values.get(axis, sorted(rows.keys()))
            }
            for axis, rows in axis_totals.items()
        }

    def _humanoid_state_ratios(self, by_worker: dict[str, Any]) -> dict[str, dict[str, dict[str, float]]]:
        axis_values = self._humanoid_state_axis_values()
        ratios: dict[str, dict[str, dict[str, float]]] = {}
        for worker_id, worker_rows in by_worker.items():
            if not isinstance(worker_rows, dict):
                continue
            ratios[str(worker_id)] = {}
            for axis, state_rows in worker_rows.items():
                if not isinstance(state_rows, dict):
                    continue
                total = sum(float(value or 0.0) for value in state_rows.values())
                ratios[str(worker_id)][str(axis)] = {
                    str(state): round((float(state_rows.get(state, 0.0) or 0.0) / total) if total > 0 else 0.0, 6)
                    for state in axis_values.get(str(axis), sorted(state_rows.keys()))
                }
        return ratios

    def _humanoid_execution_ratios(self, by_worker: dict[str, Any]) -> dict[str, float]:
        ratios: dict[str, float] = {}
        for worker_id, worker_rows in by_worker.items():
            availability = worker_rows.get("availability", {}) if isinstance(worker_rows, dict) else {}
            if not isinstance(availability, dict):
                ratios[str(worker_id)] = 0.0
                continue
            total = sum(float(value or 0.0) for value in availability.values())
            ratios[str(worker_id)] = round((float(availability.get("EXECUTING", 0.0) or 0.0) / total) if total > 0 else 0.0, 6)
        return ratios

    def _humanoid_blocked_ratios(self, by_worker: dict[str, Any]) -> dict[str, float]:
        ratios: dict[str, float] = {}
        for worker_id, worker_rows in by_worker.items():
            availability = worker_rows.get("availability", {}) if isinstance(worker_rows, dict) else {}
            if not isinstance(availability, dict):
                ratios[str(worker_id)] = 0.0
                continue
            total = sum(float(value or 0.0) for value in availability.values())
            ratios[str(worker_id)] = round((float(availability.get("BLOCKED", 0.0) or 0.0) / total) if total > 0 else 0.0, 6)
        return ratios

    def _humanoid_unavailable_ratios(self, by_worker: dict[str, Any]) -> dict[str, float]:
        ratios: dict[str, float] = {}
        for worker_id, worker_rows in by_worker.items():
            availability = worker_rows.get("availability", {}) if isinstance(worker_rows, dict) else {}
            if not isinstance(availability, dict):
                ratios[str(worker_id)] = 0.0
                continue
            total = sum(float(value or 0.0) for value in availability.values())
            unavailable = float(availability.get("DISABLED", 0.0) or 0.0) + float(availability.get("OFFLINE", 0.0) or 0.0)
            ratios[str(worker_id)] = round((unavailable / total) if total > 0 else 0.0, 6)
        return ratios

    def _humanoid_primitive_minutes(self) -> dict[str, float]:
        active: dict[tuple[str, str, str], tuple[float, str]] = {}
        totals: dict[str, float] = defaultdict(float)
        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            if event_type not in {"HUMANOID_STEP_START", "HUMANOID_STEP_END"}:
                continue
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            agent_id = str(event.get("entity_id", "") or "")
            instance_id = str(details.get("instance_id", "") or "")
            step_id = str(details.get("step_id", "") or "")
            call_code = str(details.get("primitive_call_code", "") or "")
            if not agent_id or not instance_id or not step_id or not call_code:
                continue
            key = (agent_id, instance_id, step_id)
            event_t = float(event.get("t", 0.0) or 0.0)
            if event_type == "HUMANOID_STEP_START":
                active[key] = (event_t, call_code)
            else:
                start = active.pop(key, None)
                if start is not None and event_t > start[0]:
                    totals[start[1]] += event_t - start[0]
        sim_end = float(self.env.now)
        for start_t, call_code in active.values():
            if sim_end > start_t:
                totals[call_code] += sim_end - start_t
        return {call_code: round(minutes, 3) for call_code, minutes in sorted(totals.items())}

    def _humanoid_task_taxonomy_metrics(self, humanoid_task_minutes: dict[str, float]) -> dict[str, Any]:
        by_level: dict[str, float] = defaultdict(float)
        by_category: dict[str, float] = defaultdict(float)
        by_category_id: dict[str, float] = defaultdict(float)
        task_details: dict[str, dict[str, Any]] = {}
        catalog = getattr(getattr(self, "humanoid_runtime", None), "catalog", None)
        for task_code, minutes in sorted(humanoid_task_minutes.items()):
            spec = catalog.get(task_code) if catalog is not None else None
            level = str(getattr(spec, "level", "") or "UNKNOWN")
            if "." in level:
                level = level.rsplit(".", 1)[-1]
            metadata = getattr(spec, "metadata", {}) if spec is not None else {}
            catalog_meta = metadata.get("catalog", {}) if isinstance(metadata, dict) and isinstance(metadata.get("catalog", {}), dict) else {}
            category_id = str(catalog_meta.get("category_id", "UNKNOWN") or "UNKNOWN")
            category = str(catalog_meta.get("category", "UNKNOWN") or "UNKNOWN").strip()
            value = float(minutes or 0.0)
            by_level[level] += value
            by_category_id[category_id] += value
            by_category[f"{category_id} - {category}" if category_id != "UNKNOWN" else category] += value
            task_details[str(task_code)] = {
                "minutes": round(value, 3),
                "level": level,
                "category_id": category_id,
                "category": category,
            }
        return {
            "by_level": {key: round(value, 3) for key, value in sorted(by_level.items())},
            "by_category_id": {key: round(value, 3) for key, value in sorted(by_category_id.items())},
            "by_category": {key: round(value, 3) for key, value in sorted(by_category.items())},
            "by_task": task_details,
        }

    def _operational_task_complexity_metrics(self) -> dict[str, Any]:
        completed_task_counts: dict[str, int] = defaultdict(int)
        for rec in self.task_records:
            if rec.get("status") != "completed":
                continue
            task_code = str(rec.get("humanoid_task_code", "") or "").strip().upper()
            if task_code:
                completed_task_counts[task_code] += 1
        catalog = getattr(getattr(self, "humanoid_runtime", None), "catalog", None)
        return build_operational_task_complexity_metrics(
            dict(completed_task_counts),
            num_days=self._objective_reporting_days(),
            catalog=catalog,
        )

    def _traffic_metrics(self) -> dict[str, Any]:
        by_type: dict[str, int] = defaultdict(int)
        by_pair: dict[str, int] = defaultdict(int)
        by_severity: dict[str, int] = defaultdict(int)
        for row in self.traffic_conflicts:
            if not isinstance(row, dict):
                continue
            conflict_type = str(row.get("conflict_type", "UNKNOWN") or "UNKNOWN")
            severity = str(row.get("severity", "info") or "info")
            by_type[conflict_type] += 1
            by_severity[severity] += 1
            worker_ids = row.get("worker_ids", [])
            if isinstance(worker_ids, list) and len(worker_ids) >= 2:
                pair = " / ".join(sorted(str(worker_id) for worker_id in worker_ids[:2]))
                by_pair[pair] += 1
        return {
            "traffic_conflicts_by_type": dict(sorted(by_type.items())),
            "traffic_conflicts_by_worker_pair": dict(sorted(by_pair.items())),
            "traffic_conflicts_by_severity": dict(sorted(by_severity.items())),
            "collision_count": sum(1 for row in self.traffic_conflicts if isinstance(row, dict) and bool(row.get("collision", False))),
            "near_miss_count": int(by_type.get("NEAR_MISS", 0)),
            "edge_conflict_count": int(by_type.get("EDGE_CONFLICT", 0)),
            "tile_conflict_count": int(by_type.get("TILE_CONFLICT", 0)),
            "path_overlap_count": int(by_type.get("PATH_OVERLAP", 0)),
        }

    def _humanoid_incident_metrics(self) -> dict[str, Any]:
        by_code: dict[str, int] = defaultdict(int)
        by_category: dict[str, int] = defaultdict(int)
        by_worker: dict[str, int] = defaultdict(int)
        by_severity: dict[str, int] = defaultdict(int)
        recovery_protocol_by_code: dict[str, list[dict[str, Any]]] = {}
        for event in self.logger.events:
            if str(event.get("type", "")).strip() != "HUMANOID_INCIDENT":
                continue
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            code = str(details.get("incident_code", "") or details.get("reason_code", "") or "UNKNOWN")
            category = str(details.get("incident_category", "") or "unknown")
            severity = str(details.get("incident_severity", "") or "warning")
            worker_id = str(event.get("entity_id", "") or "")
            by_code[code] += 1
            by_category[category] += 1
            by_severity[severity] += 1
            if worker_id:
                by_worker[worker_id] += 1
            recovery_protocol = details.get("recovery_protocol", [])
            if isinstance(recovery_protocol, list) and code not in recovery_protocol_by_code:
                recovery_protocol_by_code[code] = [dict(row) for row in recovery_protocol if isinstance(row, dict)]
        return {
            "humanoid_incident_total": int(sum(by_code.values())),
            "humanoid_incidents_by_code": dict(sorted(by_code.items())),
            "humanoid_incidents_by_category": dict(sorted(by_category.items())),
            "humanoid_incidents_by_worker": dict(sorted(by_worker.items())),
            "humanoid_incidents_by_severity": dict(sorted(by_severity.items())),
            "humanoid_incident_recovery_protocol_by_code": recovery_protocol_by_code,
        }

    def _transport_metrics(self) -> dict[str, Any]:
        handover_count = 0
        shared_product_carry_completed = 0
        product_carry_time = 0.0
        shared_product_carry_time = 0.0
        shared_product_carry_time_by_worker: dict[str, float] = defaultdict(float)
        shared_product_carry_time_by_pair: dict[str, float] = defaultdict(float)
        item_transport_time_by_type: dict[str, float] = defaultdict(float)
        active_moves: dict[tuple[str, str], tuple[float, str]] = {}
        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            event_t = float(event.get("t", 0.0) or 0.0)
            if event_type == "PRODUCT_CARRY_JOINED":
                handover_count += 1
            if event_type == "PRODUCT_CARRY_COMPLETED":
                shared_duration = float(details.get("shared_duration", 0.0) or 0.0)
                if int(details.get("carrier_count", 0) or 0) > 1 or shared_duration > 0.0:
                    shared_product_carry_completed += 1
                product_carry_time += float(details.get("duration", 0.0) or 0.0)
                shared_product_carry_time += shared_duration
                carrier_ids = [str(worker_id) for worker_id in details.get("carrier_ids", []) if str(worker_id)] if isinstance(details.get("carrier_ids", []), list) else []
                if shared_duration > 0.0:
                    for worker_id in carrier_ids:
                        shared_product_carry_time_by_worker[worker_id] += shared_duration
                    if len(carrier_ids) >= 2:
                        pair = " / ".join(sorted(carrier_ids[:2]))
                        shared_product_carry_time_by_pair[pair] += shared_duration
            if event_type == "AGENT_MOVE_START":
                item_type = str(details.get("carrying_item_type", "") or "").strip().lower()
                if item_type:
                    active_moves[(str(event.get("entity_id", "")), str(details.get("move_id", "")))] = (event_t, item_type)
            elif event_type == "AGENT_MOVE_END":
                key = (str(event.get("entity_id", "")), str(details.get("move_id", "")))
                start = active_moves.pop(key, None)
                if start is not None and event_t > start[0]:
                    item_transport_time_by_type[start[1]] += event_t - start[0]
        shared_product_carry_ratio = (shared_product_carry_time / product_carry_time) if product_carry_time > 0.0 else 0.0
        return {
            "handover_item_count": int(handover_count),
            "shared_product_carry_completed_count": int(shared_product_carry_completed),
            "product_carry_time_min": round(product_carry_time, 3),
            "shared_product_carry_time_min": round(shared_product_carry_time, 3),
            "solo_product_carry_time_min": round(max(0.0, product_carry_time - shared_product_carry_time), 3),
            "shared_product_carry_ratio": round(shared_product_carry_ratio, 6),
            "shared_product_carry_time_by_worker": {
                key: round(value, 3) for key, value in sorted(shared_product_carry_time_by_worker.items())
            },
            "shared_product_carry_time_by_pair": {
                key: round(value, 3) for key, value in sorted(shared_product_carry_time_by_pair.items())
            },
            "item_transport_time_by_type": {
                key: round(value, 3) for key, value in sorted(item_transport_time_by_type.items())
            },
        }

    @staticmethod
    def _battery_charge_sessions_from_events(
        events: list[dict[str, Any]],
        sim_end: float,
    ) -> list[dict[str, Any]]:
        active: dict[tuple[str, str], dict[str, Any]] = {}
        sessions: list[dict[str, Any]] = []

        def _close(
            key: tuple[str, str],
            end_t: float,
            *,
            completed: bool,
            status: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            row = active.pop(key, None)
            event_details = details if isinstance(details, dict) else {}
            if row is None:
                if not completed:
                    return
                recorded_duration = max(
                    0.0,
                    float(
                        event_details.get("actual_charge_duration_min")
                        or event_details.get("charge_duration_min")
                        or 0.0
                    ),
                )
                row = {
                    "worker_id": key[0],
                    "task_id": key[1],
                    "started_at": max(0.0, float(end_t) - recorded_duration),
                }
            raw_started_at = row.get("started_at")
            started_at = float(end_t) if raw_started_at is None else float(raw_started_at)
            sessions.append(
                {
                    **row,
                    "ended_at": float(end_t),
                    "duration_min": max(0.0, float(end_t) - started_at),
                    "completed": bool(completed),
                    "status": str(status),
                }
            )

        ordered_events = sorted(
            enumerate(events),
            key=lambda pair: (float(pair[1].get("t", 0.0) or 0.0), pair[0]),
        )
        for _index, event in ordered_events:
            event_type = str(event.get("type", "")).strip().upper()
            if event_type not in {
                "BATTERY_CHARGE_STARTED",
                "BATTERY_CHARGE_COMPLETED",
                "BATTERY_CHARGE_INTERRUPTED",
                "AGENT_TASK_END",
            }:
                continue
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            payload = details.get("payload", {}) if isinstance(details.get("payload", {}), dict) else {}
            worker_id = str(event.get("entity_id", "")).strip()
            task_id = str(details.get("task_id", "")).strip()
            key = (worker_id, task_id)
            event_t = float(event.get("t", 0.0) or 0.0)

            if event_type == "BATTERY_CHARGE_STARTED":
                active[key] = {
                    "worker_id": worker_id,
                    "task_id": task_id,
                    "started_at": event_t,
                    "charging_dock_id": str(details.get("charging_dock_id", "")),
                }
            elif event_type == "BATTERY_CHARGE_COMPLETED":
                _close(key, event_t, completed=True, status="completed", details=details)
            elif event_type == "BATTERY_CHARGE_INTERRUPTED":
                _close(key, event_t, completed=False, status="interrupted", details=details)
            elif str(payload.get("action", "")).strip().lower() == "dock_charge":
                _close(
                    key,
                    event_t,
                    completed=False,
                    status=str(details.get("status", "interrupted") or "interrupted"),
                    details=details,
                )

        effective_end = max(0.0, float(sim_end))
        for key in list(active):
            _close(key, effective_end, completed=False, status="open_at_simulation_end")
        return sessions

    def _battery_charge_sessions(self) -> list[dict[str, Any]]:
        return self._battery_charge_sessions_from_events(
            self.logger.events,
            float(getattr(getattr(self, "env", None), "now", 0.0) or 0.0),
        )

    @staticmethod
    def _battery_charge_time_in_interval(
        sessions: list[dict[str, Any]],
        start_t: float,
        end_t: float,
    ) -> float:
        interval_start = float(start_t)
        interval_end = max(interval_start, float(end_t))
        total = 0.0
        for session in sessions:
            raw_started_at = session.get("started_at")
            raw_ended_at = session.get("ended_at")
            session_start = interval_start if raw_started_at is None else float(raw_started_at)
            session_end = interval_end if raw_ended_at is None else float(raw_ended_at)
            total += max(
                0.0,
                min(interval_end, session_end) - max(interval_start, session_start),
            )
        return total

    def _battery_service_metrics(self) -> dict[str, Any]:
        charge_started = 0
        swap_count = 0
        delivery_count = 0
        preventive_maintenance_task_count = 0

        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip().upper()
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            if event_type == "BATTERY_CHARGE_STARTED":
                charge_started += 1
            elif event_type == "BATTERY_SWAP":
                swap_count += 1
            elif event_type == "BATTERY_DELIVERED":
                delivery_count += 1
            elif event_type == "AGENT_TASK_START":
                if str(details.get("task_type", "")).strip().upper() == "PREVENTIVE_MAINTENANCE":
                    preventive_maintenance_task_count += 1

        sessions = self._battery_charge_sessions()
        completed_sessions = [session for session in sessions if bool(session.get("completed"))]
        charge_time_min = sum(float(session.get("duration_min", 0.0) or 0.0) for session in sessions)
        charge_count_by_worker: dict[str, int] = defaultdict(int)
        charge_time_by_worker: dict[str, float] = defaultdict(float)
        for session in sessions:
            worker_id = str(session.get("worker_id", "")).strip()
            if not worker_id:
                continue
            if bool(session.get("completed")):
                charge_count_by_worker[worker_id] += 1
            charge_time_by_worker[worker_id] += float(session.get("duration_min", 0.0) or 0.0)

        return {
            "battery_service_mode": self.battery_service_mode,
            "battery_charge_started_count": int(charge_started),
            "battery_charge_count": int(len(completed_sessions)),
            "battery_charge_time_min": round(charge_time_min, 3),
            "battery_charge_count_by_worker": dict(sorted(charge_count_by_worker.items())),
            "battery_charge_time_min_by_worker": {
                key: round(value, 3) for key, value in sorted(charge_time_by_worker.items())
            },
            "battery_swap_count": int(swap_count),
            "battery_delivery_count": int(delivery_count),
            "preventive_maintenance_task_count": int(preventive_maintenance_task_count),
        }

    def _reliability_and_risk_metrics(self) -> dict[str, Any]:
        failure_processing_samples: list[float] = []
        failure_count_by_machine: dict[str, int] = defaultdict(int)
        repair_count_by_tier: dict[str, int] = defaultdict(int)
        repair_response_by_tier: dict[str, list[float]] = defaultdict(list)
        active_failures: dict[str, dict[str, Any]] = {}
        battery_risk_margins: list[float] = []
        battery_risk_assignment_count = 0
        depleted_during_task_count = 0
        depleted_during_move_count = 0
        returned_count = 0
        recovery_scheduled_count = 0
        interrupted_by_depletion_count = 0
        dropped_by_depletion_count = 0

        ordered_events = sorted(
            enumerate(self.logger.events),
            key=lambda pair: (float(pair[1].get("t", 0.0) or 0.0), pair[0]),
        )
        for _index, event in ordered_events:
            event_type = str(event.get("type", "")).strip().upper()
            entity_id = str(event.get("entity_id", "")).strip()
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            event_t = float(event.get("t", 0.0) or 0.0)
            if event_type == "MACHINE_FAILURE_PROCESSING_THRESHOLD_REACHED":
                failure_processing_samples.append(
                    float(details.get("actual_processing_since_repair_min", 0.0) or 0.0)
                )
            elif event_type == "MACHINE_BROKEN":
                tier = str(details.get("repair_urgency_tier", "normal") or "normal").lower()
                failure_count_by_machine[entity_id] += 1
                active_failures[entity_id] = {"started_at": event_t, "tier": tier, "responded": False}
            elif event_type in {"MACHINE_REPAIR_START", "MACHINE_REPAIR_HELPER_JOIN"}:
                row = active_failures.get(entity_id)
                if isinstance(row, dict) and not bool(row.get("responded", False)):
                    tier = str(
                        details.get("repair_urgency_tier", row.get("tier", "normal"))
                        or "normal"
                    ).lower()
                    repair_count_by_tier[tier] += 1
                    repair_response_by_tier[tier].append(max(0.0, event_t - float(row["started_at"])))
                    row["tier"] = tier
                    row["responded"] = True
            elif event_type == "MACHINE_REPAIRED":
                active_failures.pop(entity_id, None)
            elif event_type == "AGENT_TASK_START":
                selection = details.get("selection", {}) if isinstance(details.get("selection", {}), dict) else {}
                payload = details.get("payload", {}) if isinstance(details.get("payload", {}), dict) else {}
                risk = selection.get("battery_risk", {}) if isinstance(selection.get("battery_risk", {}), dict) else {}
                if not risk:
                    risk = payload
                if bool(risk.get("battery_depletion_risk", False)):
                    battery_risk_assignment_count += 1
                    battery_risk_margins.append(
                        float(risk.get("expected_battery_margin_min", 0.0) or 0.0)
                    )
            elif event_type == "AGENT_DISCHARGED":
                if str(details.get("current_task_id", "")).strip():
                    depleted_during_task_count += 1
                if bool(details.get("during_move", False)):
                    depleted_during_move_count += 1
            elif event_type == "WORKER_RECOVERY_SCHEDULED":
                recovery_scheduled_count += 1
            elif event_type == "WORKER_RETURNED_NEXT_DAY":
                returned_count += 1
            elif event_type == "WORKER_DEPLETION_CARGO_DROPPED":
                dropped_by_depletion_count += int(details.get("item_count", 0) or 0)
            elif event_type == "AGENT_TASK_END":
                if (
                    str(details.get("status", "")).strip().lower() == "interrupted"
                    and str(details.get("reason", "")).strip().lower() == "battery_depleted"
                ):
                    interrupted_by_depletion_count += 1

        response_all = [value for values in repair_response_by_tier.values() for value in values]
        return {
            "machine_failure_time_basis": self.machine_failure_time_basis,
            "machine_failure_configured_mean_processing_min": round(
                float(self.machine_failure_mean_exposure_min), 6
            ),
            "machine_failure_count": int(sum(failure_count_by_machine.values())),
            "machine_failure_count_by_machine": dict(sorted(failure_count_by_machine.items())),
            "machine_failure_observed_processing_mttf_min": (
                round(float(mean(failure_processing_samples)), 6)
                if failure_processing_samples
                else None
            ),
            "machine_failure_processing_samples_min": [round(value, 6) for value in failure_processing_samples],
            "machine_processing_exposure_by_machine": {
                machine_id: round(float(machine.failure_cycle_processing_min), 6)
                for machine_id, machine in sorted(self.machines.items())
            },
            "machine_failure_threshold_by_machine": {
                machine_id: round(float(machine.failure_exposure_budget_min), 6)
                for machine_id, machine in sorted(self.machines.items())
            },
            "repair_count_by_urgency_tier": {
                tier: int(repair_count_by_tier.get(tier, 0))
                for tier in ("critical", "high", "normal")
            },
            "repair_response_time_avg_min": (
                round(float(mean(response_all)), 6) if response_all else None
            ),
            "repair_response_time_avg_min_by_urgency_tier": {
                tier: round(float(mean(repair_response_by_tier[tier])), 6)
                if repair_response_by_tier.get(tier)
                else None
                for tier in ("critical", "high", "normal")
            },
            "preventive_maintenance_count": int(sum(machine.pm_count for machine in self.machines.values())),
            "preventive_maintenance_protected_processing_min": round(
                sum(float(machine.pm_protected_processing_total_min) for machine in self.machines.values()),
                6,
            ),
            "preventive_maintenance_failure_count": int(
                sum(machine.failures_while_pm_protected for machine in self.machines.values())
            ),
            "battery_assignment_mode": self.battery_assignment_mode,
            "battery_risk_assignment_count": int(battery_risk_assignment_count),
            "battery_risk_expected_margin_avg_min": (
                round(float(mean(battery_risk_margins)), 6)
                if battery_risk_margins
                else None
            ),
            "worker_depleted_during_task_count": int(depleted_during_task_count),
            "worker_depleted_during_move_count": int(depleted_during_move_count),
            "worker_recovery_scheduled_count": int(recovery_scheduled_count),
            "worker_returned_next_day_count": int(returned_count),
            "worker_task_interrupted_by_depletion_count": int(interrupted_by_depletion_count),
            "worker_item_dropped_by_depletion_count": int(dropped_by_depletion_count),
        }

    def _repair_collaboration_metrics(self) -> dict[str, Any]:
        active: dict[str, dict[str, Any]] = {}
        team_time_by_size: dict[str, float] = defaultdict(float)
        collaboration_time_by_machine: dict[str, float] = defaultdict(float)
        collaboration_time_by_worker: dict[str, float] = defaultdict(float)
        helper_join_count_by_machine: dict[str, int] = defaultdict(int)
        helper_join_count_by_worker: dict[str, int] = defaultdict(int)
        episodes: list[dict[str, Any]] = []
        helper_join_count = 0
        repair_window_time = 0.0
        repair_team_size_minutes = 0.0
        collaboration_time = 0.0

        def _team_from_details(details: dict[str, Any], fallback: list[str] | None = None) -> list[str]:
            raw_team = details.get("repair_team", [])
            if isinstance(raw_team, list):
                team = [str(worker_id) for worker_id in raw_team if str(worker_id)]
                if team:
                    return team
            by_worker = str(details.get("by", "") or "").strip()
            if by_worker:
                merged = list(fallback or [])
                if by_worker not in merged:
                    merged.append(by_worker)
                return merged
            return list(fallback or [])

        def _size_from_details(details: dict[str, Any], fallback: int = 0) -> int:
            try:
                return max(0, int(details.get("repair_team_size", fallback) or fallback))
            except (TypeError, ValueError):
                return max(0, int(fallback))

        def _close_interval(machine_id: str, end_t: float) -> None:
            nonlocal repair_window_time, repair_team_size_minutes, collaboration_time
            row = active.get(machine_id)
            if not isinstance(row, dict):
                return
            raw_start_t = row.get("t", end_t)
            start_t = float(end_t if raw_start_t is None else raw_start_t)
            duration = max(0.0, float(end_t) - start_t)
            size = max(0, int(row.get("size", 0) or 0))
            if duration <= 0.0 or size <= 0:
                row["t"] = float(end_t)
                return
            team_time_by_size[str(size)] += duration
            row_team_time = row.setdefault("team_time_by_size", {})
            row_team_time[str(size)] = float(row_team_time.get(str(size), 0.0) or 0.0) + duration
            repair_window_time += duration
            repair_team_size_minutes += duration * size
            row["team_size_minutes"] = float(row.get("team_size_minutes", 0.0) or 0.0) + (duration * size)
            row["active_time"] = float(row.get("active_time", 0.0) or 0.0) + duration
            row["max_team_size"] = max(int(row.get("max_team_size", size) or size), size)
            if size > 1:
                collaboration_time += duration
                row["collaboration_time"] = float(row.get("collaboration_time", 0.0) or 0.0) + duration
                collaboration_time_by_machine[machine_id] += duration
                for worker_id in row.get("team", []):
                    collaboration_time_by_worker[str(worker_id)] += duration
            row["t"] = float(end_t)

        def _episode_row(machine_id: str, row: dict[str, Any], ended_at: float, *, status: str) -> dict[str, Any]:
            raw_started_at = row.get("started_at", ended_at)
            started_at = float(ended_at if raw_started_at is None else raw_started_at)
            active_time = float(row.get("active_time", 0.0) or 0.0)
            collaboration_episode_time = float(row.get("collaboration_time", 0.0) or 0.0)
            team_time = row.get("team_time_by_size", {}) if isinstance(row.get("team_time_by_size", {}), dict) else {}
            return {
                "machine_id": machine_id,
                "started_at": round(started_at, 3),
                "ended_at": round(float(ended_at), 3),
                "duration": round(max(0.0, float(ended_at) - started_at), 3),
                "active_repair_time_min": round(active_time, 3),
                "solo_time_min": round(max(0.0, active_time - collaboration_episode_time), 3),
                "collaboration_time_min": round(collaboration_episode_time, 3),
                "max_team_size": int(row.get("max_team_size", 0) or 0),
                "helper_join_count": int(row.get("helper_join_count", 0) or 0),
                "team_time_by_size": {key: round(float(value), 3) for key, value in sorted(team_time.items(), key=lambda item: int(item[0]))},
                "final_team": [str(worker_id) for worker_id in row.get("team", [])],
                "status": status,
            }

        ordered_events = sorted(
            enumerate(self.logger.events),
            key=lambda pair: (float(pair[1].get("t", 0.0) or 0.0), pair[0]),
        )
        for _index, event in ordered_events:
            event_type = str(event.get("type", "")).strip()
            if event_type not in {
                "MACHINE_REPAIR_START",
                "MACHINE_REPAIR_HELPER_JOIN",
                "MACHINE_REPAIR_HELPER_LEAVE",
                "MACHINE_REPAIRED",
            }:
                continue
            machine_id = str(event.get("entity_id", "")).strip()
            if not machine_id:
                continue
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            event_t = float(event.get("t", 0.0) or 0.0)
            previous = active.get(machine_id)
            previous_team = previous.get("team", []) if isinstance(previous, dict) else []
            previous_size = int(previous.get("size", 0) or 0) if isinstance(previous, dict) else 0

            if event_type == "MACHINE_REPAIR_START":
                active[machine_id] = {
                    "started_at": event_t,
                    "t": event_t,
                    "size": _size_from_details(details, 1),
                    "team": _team_from_details(details),
                    "max_team_size": _size_from_details(details, 1),
                    "helper_join_count": 0,
                    "team_time_by_size": {},
                    "active_time": 0.0,
                    "team_size_minutes": 0.0,
                    "collaboration_time": 0.0,
                }
                continue

            if event_type == "MACHINE_REPAIR_HELPER_JOIN":
                _close_interval(machine_id, event_t)
                helper_join_count += 1
                helper_join_count_by_machine[machine_id] += 1
                by_worker = str(details.get("by", "") or "").strip()
                if by_worker:
                    helper_join_count_by_worker[by_worker] += 1
                row = active.setdefault(
                    machine_id,
                    {
                        "started_at": event_t,
                        "team_time_by_size": {},
                        "active_time": 0.0,
                        "team_size_minutes": 0.0,
                        "collaboration_time": 0.0,
                    },
                )
                next_size = _size_from_details(details, max(1, previous_size + 1))
                row["t"] = event_t
                row["size"] = next_size
                row["team"] = _team_from_details(details, previous_team)
                row["max_team_size"] = max(int(row.get("max_team_size", next_size) or next_size), next_size)
                row["helper_join_count"] = int(row.get("helper_join_count", 0) or 0) + 1
                continue

            if event_type == "MACHINE_REPAIR_HELPER_LEAVE":
                _close_interval(machine_id, event_t)
                next_size = _size_from_details(details, max(0, previous_size - 1))
                if next_size <= 0:
                    active.pop(machine_id, None)
                else:
                    row = active[machine_id]
                    row["t"] = event_t
                    row["size"] = next_size
                    row["team"] = _team_from_details(details, previous_team)
                continue

            if event_type == "MACHINE_REPAIRED":
                _close_interval(machine_id, event_t)
                row = active.pop(machine_id, None)
                if isinstance(row, dict):
                    episodes.append(_episode_row(machine_id, row, event_t, status="completed"))

        sim_end = float(getattr(getattr(self, "env", None), "now", 0.0) or 0.0)
        for machine_id in list(active.keys()):
            _close_interval(machine_id, sim_end)
            row = active.pop(machine_id, None)
            if isinstance(row, dict):
                episodes.append(_episode_row(machine_id, row, sim_end, status="open_at_horizon"))

        repair_collaboration_ratio = (collaboration_time / repair_window_time) if repair_window_time > 0.0 else 0.0
        repair_team_size_avg = (repair_team_size_minutes / repair_window_time) if repair_window_time > 0.0 else 0.0
        return {
            "repair_helper_join_count": int(helper_join_count),
            "repair_helper_join_count_by_machine": dict(sorted(helper_join_count_by_machine.items())),
            "repair_helper_join_count_by_worker": dict(sorted(helper_join_count_by_worker.items())),
            "repair_team_time_by_size": {
                key: round(value, 3) for key, value in sorted(team_time_by_size.items(), key=lambda item: int(item[0]))
            },
            "repair_collaboration_time_min": round(collaboration_time, 3),
            "repair_solo_time_min": round(max(0.0, repair_window_time - collaboration_time), 3),
            "repair_collaboration_ratio": round(repair_collaboration_ratio, 6),
            "repair_team_size_avg": round(repair_team_size_avg, 6),
            "repair_collaboration_time_by_machine": {
                key: round(value, 3) for key, value in sorted(collaboration_time_by_machine.items())
            },
            "repair_collaboration_time_by_worker": {
                key: round(value, 3) for key, value in sorted(collaboration_time_by_worker.items())
            },
            "repair_collaboration_episodes": sorted(
                episodes,
                key=lambda row: (float(row.get("started_at", 0.0) or 0.0), str(row.get("machine_id", ""))),
            ),
        }

    def _buffer_wait_metrics(self) -> dict[str, Any]:
        wait_totals: dict[str, float] = defaultdict(float)
        wait_counts: dict[str, int] = defaultdict(int)
        queue_entries: dict[tuple[str, str, str], float] = {}
        output_entries: dict[tuple[str, str], float] = {}
        metric_keys = ("material_input", "intermediate_input", "product_input", "intermediate_output", "product_output")
        queue_metric_keys = ("s1_input", "s1_output", "s2_input", "s2_output", "inspection_input", "inspection_output")

        def _output_category(buffer_name: str) -> str:
            try:
                station = int(str(buffer_name).rsplit("_", 1)[-1])
            except ValueError:
                station = 0
            return "product_output" if station >= int(self.last_processing_station) else "intermediate_output"

        def _output_queue_bucket(buffer_name: str) -> str | None:
            try:
                station = int(str(buffer_name).rsplit("_", 1)[-1])
            except ValueError:
                return None
            if station == 1:
                return "s1_output"
            if station == 2:
                return "s2_output"
            if station == int(self.inspection_queue_station):
                return "inspection_output"
            return None

        def _queue_category(queue_name: str) -> str:
            return {
                "material": "material_input",
                "intermediate": "intermediate_input",
                "product": "product_input",
            }[queue_name]

        def _input_queue_bucket(queue_entity: str, queue_name: str) -> str | None:
            entity = str(queue_entity).strip().lower()
            if entity == "material_queue_1":
                return "s1_input"
            if entity in {"material_queue_2", "intermediate_queue_2"}:
                return "s2_input"
            if entity == f"intermediate_queue_{int(self.inspection_queue_station)}":
                return "inspection_input"
            return None

        queue_wait_totals: dict[str, float] = defaultdict(float)
        queue_wait_counts: dict[str, int] = defaultdict(int)

        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            item_id = str(details.get("item_id", "")).strip()
            if event_type == "ITEM_MOVED" and not item_id:
                item_id = str(event.get("entity_id", "")).strip()
            t = float(event.get("t", 0.0) or 0.0)

            if event_type == "QUEUE_PUSH":
                queue_name = str(details.get("queue", "")).strip().lower()
                queue_entity = str(event.get("entity_id", "")).strip()
                if item_id and queue_name in {"material", "intermediate", "product"} and queue_entity:
                    queue_entries[(queue_entity, queue_name, item_id)] = t
                continue

            if event_type == "QUEUE_POP":
                queue_name = str(details.get("queue", "")).strip().lower()
                queue_entity = str(event.get("entity_id", "")).strip()
                if item_id and queue_name in {"material", "intermediate", "product"} and queue_entity:
                    start = queue_entries.pop((queue_entity, queue_name, item_id), None)
                    if start is not None and t >= start:
                        category = _queue_category(queue_name)
                        wait_totals[category] += t - start
                        wait_counts[category] += 1
                        bucket = _input_queue_bucket(queue_entity, queue_name)
                        if bucket:
                            queue_wait_totals[bucket] += t - start
                            queue_wait_counts[bucket] += 1
                continue

            if event_type != "ITEM_MOVED" or not item_id:
                continue

            source_name = str(details.get("from", "")).strip()
            dest_name = str(details.get("to", "")).strip()
            if dest_name.startswith("output_buffer_station_"):
                output_entries[(dest_name, item_id)] = t
            if source_name.startswith("output_buffer_station_"):
                start = output_entries.pop((source_name, item_id), None)
                if start is not None and t >= start:
                    category = _output_category(source_name)
                    wait_totals[category] += t - start
                    wait_counts[category] += 1
                    bucket = _output_queue_bucket(source_name)
                    if bucket:
                        queue_wait_totals[bucket] += t - start
                        queue_wait_counts[bucket] += 1

        sim_end = float(self.env.now)
        active_wait_totals: dict[str, float] = defaultdict(float)
        active_wait_counts: dict[str, int] = defaultdict(int)
        active_queue_wait_totals: dict[str, float] = defaultdict(float)
        active_queue_wait_counts: dict[str, int] = defaultdict(int)
        for (queue_entity, queue_name, _item_id), start in queue_entries.items():
            category = _queue_category(queue_name)
            if sim_end >= start:
                active_wait_totals[category] += sim_end - start
                active_wait_counts[category] += 1
            bucket = _input_queue_bucket(queue_entity, queue_name)
            if bucket and sim_end >= start:
                active_queue_wait_totals[bucket] += sim_end - start
                active_queue_wait_counts[bucket] += 1
        for (buffer_name, _item_id), start in output_entries.items():
            category = _output_category(buffer_name)
            if sim_end >= start:
                active_wait_totals[category] += sim_end - start
                active_wait_counts[category] += 1
                bucket = _output_queue_bucket(buffer_name)
                if bucket:
                    active_queue_wait_totals[bucket] += sim_end - start
                    active_queue_wait_counts[bucket] += 1

        averages = {
            key: round(wait_totals[key] / wait_counts[key], 3) if wait_counts[key] > 0 else 0.0
            for key in metric_keys
        }
        counts = {key: int(wait_counts.get(key, 0)) for key in metric_keys}
        inclusive_averages = {
            key: round((wait_totals[key] + active_wait_totals[key]) / (wait_counts[key] + active_wait_counts[key]), 3)
            if (wait_counts[key] + active_wait_counts[key]) > 0
            else 0.0
            for key in metric_keys
        }
        active_counts = {key: int(active_wait_counts.get(key, 0)) for key in metric_keys}
        queue_averages = {
            key: round(queue_wait_totals[key] / queue_wait_counts[key], 3) if queue_wait_counts[key] > 0 else 0.0
            for key in queue_metric_keys
        }
        queue_counts = {key: int(queue_wait_counts.get(key, 0)) for key in queue_metric_keys}
        queue_inclusive_averages = {
            key: round((queue_wait_totals[key] + active_queue_wait_totals[key]) / (queue_wait_counts[key] + active_queue_wait_counts[key]), 3)
            if (queue_wait_counts[key] + active_queue_wait_counts[key]) > 0
            else 0.0
            for key in queue_metric_keys
        }
        queue_active_counts = {key: int(active_queue_wait_counts.get(key, 0)) for key in queue_metric_keys}
        return {
            "avg_wait_min": averages,
            "completed_wait_count": counts,
            "avg_wait_min_including_open": inclusive_averages,
            "open_wait_count": active_counts,
            "avg_wait_min_by_queue": queue_averages,
            "completed_wait_count_by_queue": queue_counts,
            "avg_wait_min_including_open_by_queue": queue_inclusive_averages,
            "open_wait_count_by_queue": queue_active_counts,
        }

    def _completed_product_lead_time_metrics(self) -> dict[str, float]:
        lead_times: list[float] = []
        for event in self.logger.events:
            if str(event.get("type", "")).strip() != "COMPLETED_PRODUCT":
                continue
            item_id = str(event.get("entity_id", "")).strip()
            item = self.items.get(item_id)
            if item is None:
                continue
            lead_time = float(event.get("t", 0.0) or 0.0) - float(item.created_at)
            if lead_time >= 0.0:
                lead_times.append(lead_time)
        if not lead_times:
            return {"avg_min": 0.0, "p95_min": 0.0}
        ordered = sorted(lead_times)
        p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
        return {
            "avg_min": round(sum(ordered) / len(ordered), 3),
            "p95_min": round(float(ordered[p95_index]), 3),
        }

    def _machine_time_metrics(self) -> dict[str, Any]:
        totals_by_machine: dict[str, dict[str, float]] = {
            machine_id: {"processing": 0.0, "broken": 0.0, "repair": 0.0, "pm": 0.0}
            for machine_id in self.machines.keys()
        }
        active_processing: dict[str, float] = {}
        active_pm: dict[str, float] = {}
        sim_end = float(self.env.now)
        broken_repair_metrics = self._machine_broken_repair_state_metrics()
        exact_broken_by_machine = broken_repair_metrics.get("broken_by_machine", {})
        exact_repair_by_machine = broken_repair_metrics.get("repair_by_machine", {})

        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            machine_id = str(event.get("entity_id", "")).strip()
            if machine_id not in self.machines:
                continue
            t = float(event.get("t", 0.0) or 0.0)
            if event_type in {"MACHINE_START", "MACHINE_RESUME"}:
                active_processing[machine_id] = t
            elif event_type in {"MACHINE_END", "MACHINE_ABORTED"}:
                start = active_processing.pop(machine_id, None)
                if start is not None and t >= start:
                    totals_by_machine[machine_id]["processing"] += t - start
            elif event_type == "MACHINE_PM_START":
                active_pm[machine_id] = t
            elif event_type == "MACHINE_PM_END":
                start = active_pm.pop(machine_id, None)
                if start is not None and t >= start:
                    totals_by_machine[machine_id]["pm"] += t - start

        for machine_id, start in active_processing.items():
            if sim_end >= start:
                totals_by_machine[machine_id]["processing"] += sim_end - start
        for machine_id, start in active_pm.items():
            if sim_end >= start:
                totals_by_machine[machine_id]["pm"] += sim_end - start
        for machine_id in totals_by_machine.keys():
            totals_by_machine[machine_id]["broken"] = float(exact_broken_by_machine.get(machine_id, 0.0) or 0.0)
            totals_by_machine[machine_id]["repair"] = float(exact_repair_by_machine.get(machine_id, 0.0) or 0.0)

        total_time = max(1.0, sim_end)
        n_machines = len(self.machines)
        machine_capacity_min = max(1.0, total_time * max(1, n_machines))
        total_processing = sum(metrics["processing"] for metrics in totals_by_machine.values())
        total_broken = sum(metrics["broken"] for metrics in totals_by_machine.values())
        total_repair = sum(metrics["repair"] for metrics in totals_by_machine.values())
        total_pm = sum(metrics["pm"] for metrics in totals_by_machine.values())
        processing_ratio = total_processing / machine_capacity_min
        broken_ratio = total_broken / machine_capacity_min
        repair_ratio = total_repair / machine_capacity_min
        pm_ratio = total_pm / machine_capacity_min

        by_station: dict[str, dict[str, float]] = {}
        by_machine: dict[str, dict[str, Any]] = {}
        for station in self.stations:
            machine_ids = self.machines_by_station.get(station, [])
            station_capacity_min = max(1.0, total_time * max(1, len(machine_ids)))
            station_processing = sum(totals_by_machine[machine_id]["processing"] for machine_id in machine_ids)
            station_broken = sum(totals_by_machine[machine_id]["broken"] for machine_id in machine_ids)
            station_repair = sum(totals_by_machine[machine_id]["repair"] for machine_id in machine_ids)
            station_pm = sum(totals_by_machine[machine_id]["pm"] for machine_id in machine_ids)
            station_processing_ratio = station_processing / station_capacity_min
            station_broken_ratio = station_broken / station_capacity_min
            station_repair_ratio = station_repair / station_capacity_min
            station_pm_ratio = station_pm / station_capacity_min
            by_station[f"station{station}"] = {
                "processing": round(station_processing_ratio, 6),
                "broken": round(station_broken_ratio, 6),
                "repair": round(station_repair_ratio, 6),
                "pm": round(station_pm_ratio, 6),
                "other": round(max(0.0, 1.0 - station_processing_ratio - station_broken_ratio - station_repair_ratio - station_pm_ratio), 6),
            }
        for machine_id in sorted(self.machines.keys()):
            machine = self.machines[machine_id]
            processing_min = float(totals_by_machine[machine_id]["processing"])
            broken_min = float(totals_by_machine[machine_id]["broken"])
            repair_min = float(totals_by_machine[machine_id]["repair"])
            pm_min = float(totals_by_machine[machine_id]["pm"])
            other_min = max(0.0, total_time - processing_min - broken_min - repair_min - pm_min)
            by_machine[machine_id] = {
                "station": int(machine.station),
                "processing_min": round(processing_min, 3),
                "broken_min": round(broken_min, 3),
                "repair_min": round(repair_min, 3),
                "pm_min": round(pm_min, 3),
                "other_min": round(other_min, 3),
                "processing": round(processing_min / total_time, 6),
                "broken": round(broken_min / total_time, 6),
                "repair": round(repair_min / total_time, 6),
                "pm": round(pm_min / total_time, 6),
                "other": round(max(0.0, 1.0 - (processing_min / total_time) - (broken_min / total_time) - (repair_min / total_time) - (pm_min / total_time)), 6),
            }

        return {
            "total_processing_min": round(total_processing, 3),
            "total_broken_min": round(total_broken, 3),
            "total_repair_min": round(total_repair, 3),
            "total_pm_min": round(total_pm, 3),
            "utilization_ratio": round(processing_ratio, 6),
            "broken_ratio": round(broken_ratio, 6),
            "repair_ratio": round(repair_ratio, 6),
            "pm_ratio": round(pm_ratio, 6),
            "other_ratio": round(max(0.0, 1.0 - processing_ratio - broken_ratio - repair_ratio - pm_ratio), 6),
            "ratio_by_station": by_station,
            "time_by_machine": by_machine,
        }

    def _machine_setup_time_metrics(self) -> dict[str, float]:
        intervals_by_machine: dict[str, list[tuple[float, float]]] = {machine_id: [] for machine_id in self.machines.keys()}
        active_setup: dict[str, tuple[str, float]] = {}
        sim_end = float(self.env.now)

        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            machine_id = str(event.get("entity_id", "")).strip()
            t = float(event.get("t", 0.0) or 0.0)
            if machine_id not in intervals_by_machine:
                continue
            if event_type == "MACHINE_SETUP_START":
                setup_id = str(details.get("setup_id", "")).strip() or f"{machine_id}@{t}"
                active_setup[setup_id] = (machine_id, t)
            elif event_type == "MACHINE_SETUP_END":
                setup_id = str(details.get("setup_id", "")).strip() or f"{machine_id}@{t}"
                active = active_setup.pop(setup_id, None)
                if active is None:
                    continue
                active_machine_id, start_t = active
                if t >= start_t:
                    intervals_by_machine[active_machine_id].append((start_t, t))

        for active_machine_id, start_t in active_setup.values():
            if sim_end >= start_t:
                intervals_by_machine[active_machine_id].append((start_t, sim_end))

        totals_by_machine: dict[str, float] = {}
        for machine_id, intervals in intervals_by_machine.items():
            merged: list[list[float]] = []
            for start_t, end_t in sorted((float(start), float(end)) for start, end in intervals if end > start):
                if not merged or start_t > merged[-1][1]:
                    merged.append([start_t, end_t])
                else:
                    merged[-1][1] = max(merged[-1][1], end_t)
            totals_by_machine[machine_id] = sum(max(0.0, end_t - start_t) for start_t, end_t in merged)

        return {machine_id: round(float(total), 3) for machine_id, total in totals_by_machine.items()}

    def _machine_broken_repair_state_metrics(self) -> dict[str, dict[str, float]]:
        broken_by_machine: dict[str, float] = {machine_id: 0.0 for machine_id in self.machines.keys()}
        repair_by_machine: dict[str, float] = {machine_id: 0.0 for machine_id in self.machines.keys()}
        active_broken: dict[str, float] = {}
        active_repair: dict[str, float] = {}
        sim_end = float(self.env.now)

        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            machine_id = str(event.get("entity_id", "")).strip()
            t = float(event.get("t", 0.0) or 0.0)
            if machine_id not in broken_by_machine:
                continue
            if event_type == "MACHINE_BROKEN":
                active_broken[machine_id] = t
            elif event_type == "MACHINE_REPAIR_START":
                start_t = active_broken.pop(machine_id, None)
                if start_t is not None and t >= start_t:
                    broken_by_machine[machine_id] += t - start_t
                active_repair[machine_id] = t
            elif event_type == "MACHINE_REPAIR_HELPER_JOIN":
                if machine_id not in active_repair:
                    start_t = active_broken.pop(machine_id, None)
                    if start_t is not None and t >= start_t:
                        broken_by_machine[machine_id] += t - start_t
                    active_repair[machine_id] = t
            elif event_type == "MACHINE_REPAIR_HELPER_LEAVE":
                details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
                team_size = int(details.get("repair_team_size", 0) or 0)
                if team_size <= 0:
                    repair_start_t = active_repair.pop(machine_id, None)
                    if repair_start_t is not None and t >= repair_start_t:
                        repair_by_machine[machine_id] += t - repair_start_t
                    active_broken[machine_id] = t
            elif event_type == "MACHINE_REPAIRED":
                repair_start_t = active_repair.pop(machine_id, None)
                if repair_start_t is not None and t >= repair_start_t:
                    repair_by_machine[machine_id] += t - repair_start_t
                    continue
                broken_start_t = active_broken.pop(machine_id, None)
                if broken_start_t is not None and t >= broken_start_t:
                    broken_by_machine[machine_id] += t - broken_start_t

        for machine_id, start_t in active_broken.items():
            if sim_end >= start_t:
                broken_by_machine[machine_id] += sim_end - start_t
        for machine_id, start_t in active_repair.items():
            if sim_end >= start_t:
                repair_by_machine[machine_id] += sim_end - start_t

        return {
            "broken_by_machine": {machine_id: round(float(total), 3) for machine_id, total in broken_by_machine.items()},
            "repair_by_machine": {machine_id: round(float(total), 3) for machine_id, total in repair_by_machine.items()},
        }

    def _machine_state_time_metrics(self) -> dict[str, Any]:
        state_name_map = {
            MachineState.PROCESSING.value: "processing",
            MachineState.BROKEN.value: "broken",
            MachineState.UNDER_PM.value: "pm",
            MachineState.SETUP.value: "setup",
            MachineState.UNDER_REPAIR.value: "under_repair",
            MachineState.IDLE.value: "idle",
            MachineState.WAIT_INPUT.value: "wait_input",
            MachineState.DONE_WAIT_UNLOAD.value: "done_wait_unload",
        }
        tracked_state_names = tuple(state_name_map.values())
        by_machine: dict[str, dict[str, float]] = {
            machine_id: {state_name: 0.0 for state_name in tracked_state_names}
            for machine_id in sorted(self.machines.keys())
        }
        total_time = max(1.0, float(self.env.now))
        snapshots = sorted(
            (
                {
                    "t": float(snapshot.get("t", 0.0) or 0.0),
                    "machine_states": snapshot.get("machine_states", {}) if isinstance(snapshot.get("machine_states", {}), dict) else {},
                }
                for snapshot in self.minute_snapshots
                if isinstance(snapshot, dict)
            ),
            key=lambda row: row["t"],
        )
        if not snapshots:
            snapshots = [
                {
                    "t": 0.0,
                    "machine_states": {machine_id: self._observed_machine_state(machine).value for machine_id, machine in self.machines.items()},
                }
            ]

        previous_t = 0.0
        previous_states = snapshots[0]["machine_states"]
        first_t = float(snapshots[0]["t"])
        if first_t > 0.0:
            duration = min(total_time, first_t) - previous_t
            if duration > 0.0:
                for machine_id, raw_state in previous_states.items():
                    state_name = state_name_map.get(str(raw_state).strip(), "")
                    if machine_id in by_machine and state_name:
                        by_machine[machine_id][state_name] += duration
                previous_t = min(total_time, first_t)

        for snapshot in snapshots[1:]:
            current_t = min(total_time, float(snapshot["t"]))
            duration = max(0.0, current_t - previous_t)
            if duration > 0.0:
                for machine_id, raw_state in previous_states.items():
                    state_name = state_name_map.get(str(raw_state).strip(), "")
                    if machine_id in by_machine and state_name:
                        by_machine[machine_id][state_name] += duration
            previous_t = current_t
            previous_states = snapshot["machine_states"]

        final_duration = max(0.0, total_time - previous_t)
        if final_duration > 0.0:
            for machine_id, raw_state in previous_states.items():
                state_name = state_name_map.get(str(raw_state).strip(), "")
                if machine_id in by_machine and state_name:
                    by_machine[machine_id][state_name] += final_duration

        util_by_machine: dict[str, dict[str, float]] = {}
        for machine_id in sorted(by_machine.keys()):
            state_minutes = by_machine[machine_id]
            idle_min = float(state_minutes.get("idle", 0.0))
            wait_min = float(state_minutes.get("wait_input", 0.0)) + float(state_minutes.get("done_wait_unload", 0.0))
            processing_min = float(state_minutes.get("processing", 0.0))
            broken_min = float(state_minutes.get("broken", 0.0))
            denom_no_idle = max(0.0, total_time - idle_min)
            denom_no_idle_broken = max(0.0, total_time - idle_min - broken_min)
            denom_no_idle_wait = max(0.0, total_time - idle_min - wait_min)
            util_by_machine[machine_id] = {
                "util_total": round(processing_min / total_time, 6),
                "util_no_idle": round((processing_min / denom_no_idle) if denom_no_idle > 0.0 else 0.0, 6),
                "util_no_idle_broken": round(
                    (processing_min / denom_no_idle_broken) if denom_no_idle_broken > 0.0 else 0.0,
                    6,
                ),
                "util_no_idle_wait": round(
                    (processing_min / denom_no_idle_wait) if denom_no_idle_wait > 0.0 else 0.0,
                    6,
                ),
            }

        rounded_state_times = {
            machine_id: {state_name: round(float(minutes), 3) for state_name, minutes in state_minutes.items()}
            for machine_id, state_minutes in by_machine.items()
        }
        return {
            "state_time_by_machine": rounded_state_times,
            "utilization_by_machine": util_by_machine,
        }

    def _warehouse_push_material(self, station: int) -> str:
        item_id = self._next_item_id(f"MAT-S{station}")
        self.items[item_id] = Item(item_id=item_id, item_type="material", created_at=self.env.now, current_station=station)
        if not self._push_material_queue(station, item_id):
            self.items.pop(item_id, None)
            raise ValueError(f"Initial inventory exceeds material_queue_{station} capacity.")
        return item_id

    def _ensure_material_shelf_slots(self) -> None:
        if self.warehouse_material_shelf_slots:
            return
        for index in range(1, self.material_shelf_capacity + 1):
            slot_id = f"warehouse_material_slot_{index:02d}"
            shelf_tile = None
            service_tile = None
            if self.grid_map is not None:
                obj = self.grid_map.objects.get(slot_id)
                if obj is not None:
                    shelf_tile = obj.center()
                service_tiles = self.grid_map.service_tiles.get(slot_id, [])
                if service_tiles:
                    service_tile = service_tiles[0]
            self.warehouse_material_shelf_slots[slot_id] = {
                "slot_id": slot_id,
                "material_item_id": None,
                "shelf_tile": shelf_tile,
                "service_tile": service_tile,
                "occupied": False,
            }

    def _material_shelf_count(self) -> int:
        self._ensure_material_shelf_slots()
        return sum(1 for slot in self.warehouse_material_shelf_slots.values() if slot.get("material_item_id"))

    def _material_matches_supply_station(self, item_id: str, station: int | None) -> bool:
        if station is None or not self.minimize_makespan_enabled:
            return True
        return self.initial_batch_material_station.get(str(item_id)) == int(station)

    def _material_shelf_count_for_station(self, station: int) -> int:
        self._ensure_material_shelf_slots()
        return sum(
            1
            for slot in self.warehouse_material_shelf_slots.values()
            if slot.get("material_item_id")
            and self._material_matches_supply_station(str(slot["material_item_id"]), station)
        )

    def _restock_material_shelf(self, *, reason: str, target_fill: int | None = None) -> int:
        self._ensure_material_shelf_slots()
        target = self.material_shelf_capacity if target_fill is None else max(0, min(self.material_shelf_capacity, int(target_fill)))
        current = self._material_shelf_count()
        needed = max(0, target - current)
        if needed <= 0:
            self.material_shelf_empty_alerted = False
            return 0
        restocked_slots: list[dict[str, Any]] = []
        for slot_id, slot in sorted(self.warehouse_material_shelf_slots.items()):
            if needed <= 0:
                break
            if slot.get("material_item_id"):
                continue
            item_id = self._next_item_id("MAT-WH")
            self.items[item_id] = Item(item_id=item_id, item_type="material", created_at=float(self.env.now))
            slot["material_item_id"] = item_id
            slot["occupied"] = True
            self._set_item_state(item_id, ItemState.IN_STORAGE, location="Warehouse", ref=slot_id, item_type="material")
            restocked_slots.append(
                {
                    "slot_id": slot_id,
                    "item_id": item_id,
                    "shelf_tile": self._tile_payload(slot.get("shelf_tile")),
                    "service_tile": self._tile_payload(slot.get("service_tile")),
                }
            )
            needed -= 1
        if restocked_slots:
            self.warehouse_material_restock_count += len(restocked_slots)
            self.material_shelf_empty_alerted = False
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="WAREHOUSE_MATERIAL_RESTOCK",
                entity_id="warehouse_material_shelf",
                location="Warehouse",
                details={
                    "reason": reason,
                    "restocked_count": len(restocked_slots),
                    "shelf_count": self._material_shelf_count(),
                    "shelf_capacity": self.material_shelf_capacity,
                    "slots": restocked_slots,
                },
            )
            self._rolling_horizon_request_candidate_refresh("warehouse_material_restock")
        return len(restocked_slots)

    def _first_available_material_shelf_slot(
        self,
        agent_id: str = "",
        task_id: str = "",
        exclude_item_ids: set[str] | None = None,
        exclude_slot_ids: set[str] | None = None,
        station: int | None = None,
    ) -> dict[str, Any] | None:
        self._ensure_material_shelf_slots()
        excluded_items = exclude_item_ids or set()
        excluded_slots = exclude_slot_ids or set()
        for slot in self.warehouse_material_shelf_slots.values():
            slot_id = str(slot.get("slot_id") or "").strip()
            if slot_id in excluded_slots:
                continue
            item_id = str(slot.get("material_item_id") or "").strip()
            if item_id in excluded_items:
                continue
            if item_id and not self._material_matches_supply_station(item_id, station):
                continue
            if item_id and not self._item_reserved_by_other(item_id, agent_id, task_id):
                return slot
        return None

    def _bind_available_material_shelf_slot(
        self,
        agent: Agent,
        task: Task,
        *,
        preferred_slot_id: str = "",
        exclude_slot_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Resolve a generic material request to one reserved shelf item."""
        self._release_task_item_reservations(task, reason="material_rebind")
        excluded_slots = exclude_slot_ids or set()
        preferred_slot_id = str(preferred_slot_id or "").strip()
        supply_station = int(task.payload["station"]) if task.payload.get("station") is not None else None
        candidates: list[dict[str, Any]] = []
        if preferred_slot_id and preferred_slot_id not in excluded_slots:
            preferred = self.warehouse_material_shelf_slots.get(preferred_slot_id)
            if isinstance(preferred, dict):
                candidates.append(preferred)

        first_slot = self._first_available_material_shelf_slot(
            agent.agent_id,
            task.task_id,
            exclude_slot_ids=excluded_slots | ({preferred_slot_id} if preferred_slot_id else set()),
            station=supply_station,
        )
        if first_slot is not None:
            candidates.append(first_slot)

        for slot in candidates:
            slot_id = str(slot.get("slot_id") or "").strip()
            item_id = str(slot.get("material_item_id") or "").strip()
            if not slot_id or not item_id or slot_id in excluded_slots:
                continue
            if not self._material_matches_supply_station(item_id, supply_station):
                continue
            if self._reserve_item_for_task(
                agent,
                task,
                item_id,
                source="warehouse_material_shelf",
                ref=slot_id,
                item_type="material",
            ):
                task.payload["source_slot_id"] = slot_id
                task.payload["transfer_item_id"] = item_id
                task.payload["material_item_id"] = item_id
                return slot
        return None

    def _log_material_shelf_empty_once(self) -> None:
        if self.material_shelf_empty_alerted:
            return
        self.material_shelf_empty_alerted = True
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MATERIAL_SHELF_EMPTY",
            entity_id="warehouse_material_shelf",
            location="Warehouse",
            details={
                "shelf_count": self._material_shelf_count(),
                "shelf_capacity": self.material_shelf_capacity,
            },
        )

    def _pop_material_shelf_item(
        self,
        slot_id: str | None = None,
        item_id: str | None = None,
        *,
        agent_id: str = "",
        task_id: str = "",
    ) -> tuple[str, str] | None:
        slot = None
        if slot_id:
            candidate = self.warehouse_material_shelf_slots.get(str(slot_id))
            if isinstance(candidate, dict) and candidate.get("material_item_id"):
                slot = candidate
            else:
                return None
        if slot is None:
            slot = self._first_available_material_shelf_slot(agent_id=agent_id, task_id=task_id)
        if slot is None:
            return None
        stored_item_id = str(slot.get("material_item_id") or "")
        expected_item_id = str(item_id or "").strip()
        if expected_item_id and stored_item_id != expected_item_id:
            return None
        if not stored_item_id or self._item_reserved_by_other(stored_item_id, agent_id, task_id):
            return None
        slot["material_item_id"] = None
        slot["occupied"] = False
        self.material_shelf_pick_count += 1
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="WAREHOUSE_MATERIAL_PICKED",
            entity_id=stored_item_id,
            location="Warehouse",
            details={
                "slot_id": str(slot.get("slot_id", "")),
                "shelf_tile": self._tile_payload(slot.get("shelf_tile")),
                "service_tile": self._tile_payload(slot.get("service_tile")),
                "shelf_count": self._material_shelf_count(),
                "shelf_capacity": self.material_shelf_capacity,
            },
        )
        self._rolling_horizon_request_candidate_refresh("warehouse_material_picked")
        return str(slot.get("slot_id", "")), stored_item_id

    def machine_failure_lambda(self, machine: Machine) -> float:
        if self.machine_failure_time_basis == "active_processing":
            multiplier = (
                self.pm_lambda_multiplier
                if machine.pm_protected_processing_remaining_min > 1e-9
                else 1.0
            )
        else:
            multiplier = self.pm_lambda_multiplier if self.env.now < machine.pm_until else 1.0
        return self.machine_failure_base_lambda * multiplier

    def sample_machine_failure_delay(self, machine: Machine, failure_rate: float) -> float:
        machine_id = str(machine.machine_id)
        rng = self.machine_failure_rngs.get(machine_id)
        if rng is None:
            rng = _stable_random_stream(
                getattr(self, "_diagnostic_sampling_seed", self.seed),
                f"{self.scenario_key}:machine_failure:{machine_id}",
            )
            self.machine_failure_rngs[machine_id] = rng
        return max(1.0, float(rng.expovariate(float(failure_rate))))

    def _sample_machine_failure_exposure(self, machine: Machine, *, reason: str) -> float:
        machine_id = str(machine.machine_id)
        rng = self.machine_failure_rngs.get(machine_id)
        if rng is None:
            rng = _stable_random_stream(
                getattr(self, "_diagnostic_sampling_seed", self.seed),
                f"{self.scenario_key}:machine_failure:{machine_id}",
            )
            self.machine_failure_rngs[machine_id] = rng
        sampled = max(1e-9, float(rng.expovariate(self.machine_failure_base_lambda)))
        machine.failure_exposure_budget_min = sampled
        machine.failure_exposure_used_min = 0.0
        machine.failure_cycle_processing_min = 0.0
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_FAILURE_EXPOSURE_SAMPLED",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={
                "reason": reason,
                "time_basis": self.machine_failure_time_basis,
                "distribution": self.machine_failure_distribution,
                "mean_processing_time_to_failure_min": round(
                    float(self.machine_failure_mean_exposure_min), 6
                ),
                "sampled_failure_exposure_min": round(sampled, 6),
            },
        )
        return sampled

    def initialize_machine_failure_exposure(self, machine: Machine) -> None:
        if self.machine_failure_time_basis != "active_processing":
            return
        if machine.failure_exposure_budget_min <= 0.0:
            self._sample_machine_failure_exposure(machine, reason="initialization")

    def machine_processing_segment_limit(self, machine: Machine, remaining_process_min: float) -> float:
        """Return the next exact processing segment ending at failure or PM expiry."""
        self.initialize_machine_failure_exposure(machine)
        remaining = max(0.0, float(remaining_process_min))
        if remaining <= 0.0:
            return 0.0
        multiplier = (
            self.pm_lambda_multiplier
            if machine.pm_protected_processing_remaining_min > 1e-9
            else 1.0
        )
        exposure_left = max(
            0.0,
            float(machine.failure_exposure_budget_min)
            - float(machine.failure_exposure_used_min),
        )
        until_failure = exposure_left / max(1e-12, multiplier)
        segment = min(remaining, until_failure)
        if machine.pm_protected_processing_remaining_min > 1e-9:
            segment = min(segment, float(machine.pm_protected_processing_remaining_min))
        return max(1e-9, segment)

    def record_machine_processing_exposure(self, machine: Machine, elapsed_min: float) -> None:
        elapsed = max(0.0, float(elapsed_min))
        if elapsed <= 0.0:
            return
        protected_before = max(0.0, float(machine.pm_protected_processing_remaining_min))
        protected_elapsed = min(elapsed, protected_before)
        unprotected_elapsed = elapsed - protected_elapsed
        machine.total_processing_min += elapsed
        machine.processing_since_maintenance_min += elapsed
        machine.failure_cycle_processing_min += elapsed
        machine.failure_exposure_used_min += (
            protected_elapsed * self.pm_lambda_multiplier + unprotected_elapsed
        )
        if protected_elapsed > 0.0:
            machine.pm_protected_processing_total_min += protected_elapsed
            machine.pm_protected_processing_remaining_min = max(
                0.0,
                protected_before - protected_elapsed,
            )
            if protected_before > 1e-9 and machine.pm_protected_processing_remaining_min <= 1e-9:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_PM_EFFECT_EXPIRED",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={
                        "protected_processing_min": round(
                            float(self.pm_effect_duration_min), 6
                        ),
                        "hazard_multiplier": round(float(self.pm_lambda_multiplier), 6),
                    },
                )

    def machine_failure_threshold_reached(self, machine: Machine) -> bool:
        return bool(
            machine.failure_exposure_budget_min > 0.0
            and machine.failure_exposure_used_min
            >= machine.failure_exposure_budget_min - 1e-8
        )

    def log_machine_failure_processing_threshold(self, machine: Machine) -> None:
        machine.last_failure_processing_min = float(machine.failure_cycle_processing_min)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_FAILURE_PROCESSING_THRESHOLD_REACHED",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={
                "actual_processing_since_repair_min": round(
                    float(machine.failure_cycle_processing_min), 6
                ),
                "effective_failure_exposure_min": round(
                    float(machine.failure_exposure_used_min), 6
                ),
                "sampled_failure_exposure_min": round(
                    float(machine.failure_exposure_budget_min), 6
                ),
                "pm_protected_at_failure": bool(
                    machine.pm_protected_processing_remaining_min > 1e-9
                ),
            },
        )

    def reset_machine_failure_exposure(self, machine: Machine, *, reason: str) -> None:
        if self.machine_failure_time_basis != "active_processing":
            return
        self._sample_machine_failure_exposure(machine, reason=reason)

    def machine_preventive_maintenance_due(self, machine: Machine) -> bool:
        if self.machine_failure_time_basis == "active_processing":
            return bool(
                machine.processing_since_maintenance_min
                >= self.pm_interval_target_min - 1e-9
            )
        return bool(self.env.now - machine.last_pm_at >= self.pm_interval_target_min)

    def repair_urgency(self, machine: Machine, *, emit_event: bool = True) -> dict[str, Any]:
        station_machines = [
            candidate
            for candidate in self.machines.values()
            if int(candidate.station) == int(machine.station)
        ]
        machine_count = max(1, len(station_machines))
        unavailable = [
            candidate
            for candidate in station_machines
            if candidate.broken
            or candidate.state
            in {MachineState.BROKEN, MachineState.UNDER_REPAIR, MachineState.UNDER_PM}
        ]
        available_count = max(0, machine_count - len(unavailable))
        station_outage = 1.0 if available_count == 0 else 0.0
        capacity_loss_ratio = min(1.0, len(unavailable) / machine_count)
        material_ready = len(self.material_queues.get(machine.station, ()))
        if self._station_requires_intermediate(machine.station):
            intermediate_ready = len(self.intermediate_queues.get(machine.station, ()))
            ready_sets = min(material_ready, intermediate_ready)
        else:
            ready_sets = material_ready
        demand_ratio = min(1.0, float(ready_sets) / float(machine_count))
        holds_wip = float(
            bool(
                machine.active_cycle_id
                or machine.input_material
                or machine.input_intermediate
                or machine.output_intermediate
            )
        )
        score = min(
            1.0,
            max(
                0.0,
                0.50 * station_outage
                + 0.25 * capacity_loss_ratio
                + 0.15 * demand_ratio
                + 0.10 * holds_wip,
            ),
        )
        tier = "critical" if score >= 0.75 else "high" if score >= 0.35 else "normal"
        components = {
            "station_outage": station_outage,
            "capacity_loss_ratio": round(capacity_loss_ratio, 6),
            "ready_demand_ratio": round(demand_ratio, 6),
            "machine_holds_wip": holds_wip,
        }
        changed = bool(
            tier != machine.last_repair_urgency_tier
            or abs(score - float(machine.last_repair_urgency_score)) >= 0.05
        )
        if emit_event and machine.broken and changed:
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="REPAIR_URGENCY_UPDATED",
                entity_id=machine.machine_id,
                location=f"Station{machine.station}",
                details={
                    "repair_urgency_score": round(score, 6),
                    "repair_urgency_tier": tier,
                    "components": components,
                },
            )
        machine.last_repair_urgency_score = score
        machine.last_repair_urgency_tier = tier
        return {
            "repair_urgency_score": round(score, 6),
            "repair_urgency_tier": tier,
            "repair_urgency_components": components,
        }

    @staticmethod
    def _repair_urgency_sort_key(task: Task) -> tuple[int, float, float, str]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        tier = str(payload.get("repair_urgency_tier", "normal")).strip().lower()
        tier_rank = {"critical": 0, "high": 1, "normal": 2}.get(tier, 3)
        return (
            tier_rank,
            -float(payload.get("repair_urgency_score", 0.0) or 0.0),
            float(payload.get("failed_since", math.inf) or math.inf),
            str(payload.get("machine_id", "")),
        )

    def _humanoid_incident_random(self, worker_id: str, incident_code: str) -> float:
        key = (str(worker_id).strip(), str(incident_code).strip().upper())
        rng = self.humanoid_incident_rngs.get(key)
        if rng is None:
            rng = _stable_random_stream(
                getattr(self, "_diagnostic_sampling_seed", self.seed),
                f"{self.scenario_key}:humanoid_incident:{key[0]}:{key[1]}",
            )
            self.humanoid_incident_rngs[key] = rng
        return float(rng.random())

    def _repair_total_work_min(self, machine: Machine | None = None) -> float:
        if machine is not None and float(machine.repair_sampled_work_min or 0.0) > 0.0:
            return float(machine.repair_sampled_work_min)
        return float(self.machine_failure_cfg["repair_time_min"])

    def _repair_team_size(self, machine: Machine) -> int:
        return len(machine.repair_team)

    def _repair_slots_remaining(self, machine: Machine) -> int:
        return max(0, self.max_repair_agents - self._repair_team_size(machine))

    def _refresh_repair_progress(self, machine: Machine, *, at_t: float | None = None) -> None:
        t = float(self.env.now if at_t is None else at_t)
        if machine.repair_last_progress_at is None:
            machine.repair_last_progress_at = t
            return
        if not machine.broken or machine.repair_work_remaining_min <= 0.0:
            machine.repair_last_progress_at = t
            return
        team_size = self._repair_team_size(machine)
        elapsed = max(0.0, t - float(machine.repair_last_progress_at))
        if elapsed > 0.0 and team_size > 0:
            machine.repair_work_remaining_min = max(0.0, float(machine.repair_work_remaining_min) - elapsed * team_size)
        machine.repair_last_progress_at = t

    def _interrupt_repair_monitor(self, machine: Machine) -> None:
        process = machine.repair_monitor_process
        if process is not None and getattr(process, "is_alive", False):
            try:
                process.interrupt("repair_team_changed")
            except RuntimeError:
                pass
        machine.repair_monitor_process = None

    def _sync_repair_owner(self, machine: Machine) -> None:
        machine.repair_owner = machine.repair_team[0] if machine.repair_team else None

    def _ensure_repair_done_event(self, machine: Machine) -> simpy.Event:
        event = machine.repair_done_event
        if event is None or getattr(event, "triggered", False):
            machine.repair_done_event = self.env.event()
        return machine.repair_done_event

    def _repair_worker_anchor(self, machine: Machine, agent_id: str) -> dict[str, Any]:
        team = list(machine.repair_team)
        try:
            idx = team.index(agent_id)
        except ValueError:
            idx = 0
        offsets = (
            (-0.26, -0.16),
            (-0.26, 0.18),
            (0.22, 0.02),
        )
        ox, oy = offsets[idx % len(offsets)]
        return {
            "target_machine_id": machine.machine_id,
            "anchor_index": idx,
            "team_size": len(team),
            "offset_x": ox,
            "offset_y": oy,
        }

    def _log_repair_team_event(self, machine: Machine, event_type: str, *, by: str, reason: str = "") -> None:
        urgency = self.repair_urgency(machine, emit_event=False)
        details = {
            "by": by,
            "repair_team": list(machine.repair_team),
            "repair_team_size": self._repair_team_size(machine),
            "repair_remaining_min": round(float(machine.repair_work_remaining_min), 3),
            "repair_total_min": round(self._repair_total_work_min(machine), 3),
            **urgency,
        }
        if reason:
            details["reason"] = reason
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type=event_type,
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details=details,
        )

    def _start_or_resume_repair_monitor(self, machine: Machine) -> None:
        self._interrupt_repair_monitor(machine)
        if not machine.broken or self._repair_team_size(machine) <= 0:
            machine.repair_monitor_process = None
            if machine.broken:
                self._set_machine_state(machine, MachineState.BROKEN, reason="repair_paused")
            return
        self._ensure_repair_done_event(machine)
        self._set_machine_state(machine, MachineState.UNDER_REPAIR, reason="repair_active")
        machine.repair_last_progress_at = float(self.env.now)
        machine.repair_monitor_token += 1
        token = int(machine.repair_monitor_token)
        process = self.env.process(self._repair_monitor(machine.machine_id, token))
        machine.repair_monitor_process = process

    def _join_repair_team(self, machine: Machine, agent_id: str) -> bool:
        if not machine.broken:
            return False
        if agent_id in machine.repair_team:
            return True
        if self._repair_team_size(machine) >= self.max_repair_agents:
            return False
        if machine.repair_work_remaining_min <= 0.0:
            machine.repair_work_remaining_min = self._repair_total_work_min(machine)
        self._refresh_repair_progress(machine)
        machine.repair_team.append(agent_id)
        self._sync_repair_owner(machine)
        if self._repair_team_size(machine) == 1:
            self._log_repair_team_event(machine, "MACHINE_REPAIR_START", by=agent_id)
        else:
            self._log_repair_team_event(machine, "MACHINE_REPAIR_HELPER_JOIN", by=agent_id)
        self._start_or_resume_repair_monitor(machine)
        return True

    def _leave_repair_team(self, machine: Machine, agent_id: str, *, reason: str = "") -> bool:
        if agent_id not in machine.repair_team:
            return False
        self._refresh_repair_progress(machine)
        machine.repair_team = [member for member in machine.repair_team if member != agent_id]
        self._sync_repair_owner(machine)
        self._log_repair_team_event(machine, "MACHINE_REPAIR_HELPER_LEAVE", by=agent_id, reason=reason)
        if self._repair_team_size(machine) <= 0:
            machine.repair_last_progress_at = None
            machine.repair_monitor_process = None
            if machine.broken:
                self._set_machine_state(machine, MachineState.BROKEN, reason="repair_team_empty")
        self._start_or_resume_repair_monitor(machine)
        return True

    def _complete_repair(self, machine: Machine) -> None:
        if not machine.broken:
            return
        self._refresh_repair_progress(machine)
        machine.repair_work_remaining_min = 0.0
        machine.repair_last_progress_at = None
        machine.repair_monitor_process = None
        machine.repair_monitor_token += 1
        team_snapshot = list(machine.repair_team)
        self._sync_repair_owner(machine)
        if machine.failed_since is not None:
            machine.total_broken_min += self.env.now - machine.failed_since
        machine.broken = False
        machine.failed_since = None
        self.reset_machine_failure_exposure(machine, reason="repair_completed")
        self._set_machine_state(
            machine,
            MachineState.DONE_WAIT_UNLOAD if machine.output_intermediate is not None else MachineState.WAIT_INPUT,
            reason="repair_completed",
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_REPAIRED",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={
                "by": team_snapshot[0] if team_snapshot else "",
                "repair_team": team_snapshot,
                "repair_team_size": len(team_snapshot),
                "repair_total_min": round(self._repair_total_work_min(machine), 3),
            },
        )
        done_event = self._ensure_repair_done_event(machine)
        if not done_event.triggered:
            done_event.succeed({"machine_id": machine.machine_id, "repair_team": team_snapshot})
        machine.repair_team = []
        machine.repair_owner = None

    def _repair_monitor(self, machine_id: str, token: int):
        machine = self.machines[machine_id]
        while machine.broken and machine.repair_work_remaining_min > 0.0:
            if int(machine.repair_monitor_token) != int(token):
                return
            team_size = self._repair_team_size(machine)
            if team_size <= 0:
                machine.repair_monitor_process = None
                self._set_machine_state(machine, MachineState.BROKEN, reason="repair_paused")
                return
            machine.repair_last_progress_at = float(self.env.now)
            remaining = max(0.0, float(machine.repair_work_remaining_min))
            try:
                yield self.env.timeout(remaining / team_size)
                if int(machine.repair_monitor_token) != int(token):
                    return
                if machine.repair_monitor_process is not self.env.active_process:
                    return
                self._refresh_repair_progress(machine)
                if not machine.broken or machine.repair_work_remaining_min > 1e-6:
                    continue
                self._complete_repair(machine)
                return
            except simpy.Interrupt:
                self._refresh_repair_progress(machine)
                if not machine.broken:
                    machine.repair_monitor_process = None
                    return
                continue

    def break_machine(
        self,
        machine: Machine,
        reason: str,
        *,
        interrupt_active_process: bool = True,
    ) -> None:
        if machine.broken or machine.state in (MachineState.UNDER_REPAIR, MachineState.UNDER_PM):
            return
        was_processing = machine.state == MachineState.PROCESSING
        machine.broken = True
        machine.failures += 1
        machine.failed_since = self.env.now
        if machine.pm_protected_processing_remaining_min > 1e-9:
            machine.failures_while_pm_protected += 1
        machine.repair_team = []
        machine.repair_owner = None
        machine.repair_sampled_work_min = self.timing.sample_step_duration(
            "REPAIR_MACHINE",
            "REPAIR_MACHINE/s03_execute_maintenance_action",
            sample_key=f"{machine.machine_id}:failure:{machine.failures}",
        )
        machine.repair_work_remaining_min = machine.repair_sampled_work_min
        machine.setup_ready = bool(was_processing and machine.active_cycle_id)
        machine.repair_last_progress_at = None
        machine.repair_done_event = None
        machine.repair_monitor_process = None
        machine.repair_monitor_token += 1
        self._set_machine_state(machine, MachineState.BROKEN, reason=reason)
        urgency = self.repair_urgency(machine, emit_event=False)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_BROKEN",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={
                "reason": reason,
                "failure_time_basis": self.machine_failure_time_basis,
                "actual_processing_since_repair_min": round(
                    float(machine.failure_cycle_processing_min), 6
                ),
                "effective_failure_exposure_min": round(
                    float(machine.failure_exposure_used_min), 6
                ),
                "sampled_failure_exposure_min": round(
                    float(machine.failure_exposure_budget_min), 6
                ),
                "pm_protected_at_failure": bool(
                    machine.pm_protected_processing_remaining_min > 1e-9
                ),
                **urgency,
                "sampled_repair_time_min": round(float(machine.repair_sampled_work_min), 6),
                "repair_time_distribution": self.timing.distribution_for_step(
                    "REPAIR_MACHINE", "REPAIR_MACHINE/s03_execute_maintenance_action"
                ).to_dict(),
            },
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="REPAIR_URGENCY_UPDATED",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details=urgency,
        )
        self._rolling_horizon_invalidate_machine_work(machine.machine_id)
        self.emit_incident(
            "machine_broken",
            affected_entities=[machine.machine_id, f"Station{machine.station}"],
            escalation_level="worker_local" if self.incident_policy.get("prefer_worker_local_response", True) else "planner",
            details={"reason": reason, "station": machine.station},
            notify_workers=[agent_id for agent_id, agent in self.agents.items() if self.agent_display_location(agent) == f"Station{machine.station}"],
        )
        self._rolling_horizon_request_candidate_refresh("machine_broken")
        if urgency.get("repair_urgency_tier") == "critical":
            self._rolling_horizon_collect_immediate_candidates("machine_broken_critical")
        if (
            interrupt_active_process
            and was_processing
            and machine.active_process is not None
            and machine.active_process.is_alive
        ):
            machine.active_process.interrupt("machine_breakdown")

    def battery_remaining(self, agent: Agent, at_t: float | None = None) -> float:
        period = float(getattr(self, "battery_swap_period_min", 0.0) or 0.0)
        if period <= 0:
            return 0.0
        now = float(self.env.now)
        if at_t is None or float(at_t) <= now:
            return max(0.0, self._update_battery_accounting(agent, now))
        current = self._update_battery_accounting(agent, now)
        if agent.charging_started_at is not None:
            return current
        future_delta = max(0.0, float(at_t) - now)
        return max(0.0, current - future_delta * self._battery_drain_rate_multiplier(agent))

    def _battery_drain_rate_multiplier(self, agent: Agent) -> float:
        availability = str((agent.humanoid_state or {}).get("availability", "AVAILABLE")).strip().upper()
        if availability == "AVAILABLE":
            return float(getattr(self, "battery_available_rate_multiplier", 1.0) or 1.0)
        return float(getattr(self, "battery_non_available_rate_multiplier", 2.0) or 2.0)

    def _ensure_battery_accounting(self, agent: Agent, now: float) -> None:
        swap_at = float(getattr(agent, "last_battery_swap", 0.0) or 0.0)
        budget = getattr(agent, "battery_remaining_budget_min", None)
        accounted_swap_at = float(getattr(agent, "battery_accounting_swap_at", swap_at) or 0.0)
        if budget is None or abs(accounted_swap_at - swap_at) > 1e-9:
            # Backward compatibility: tests and legacy code may still set
            # last_battery_swap directly. Initialize the budget from that value.
            elapsed = max(0.0, now - swap_at)
            period = float(getattr(self, "battery_swap_period_min", 0.0) or 0.0)
            initial_budget = period - elapsed * float(getattr(self, "battery_available_rate_multiplier", 1.0) or 1.0)
            agent.battery_remaining_budget_min = max(0.0, initial_budget)
            agent.battery_last_accounted_at = now
            agent.battery_accounting_swap_at = swap_at

    def _update_battery_accounting(self, agent: Agent, at_t: float | None = None) -> float:
        now = float(self.env.now if at_t is None else at_t)
        period = float(getattr(self, "battery_swap_period_min", 0.0) or 0.0)
        if period <= 0:
            return 0.0
        self._ensure_battery_accounting(agent, now)
        last_raw = getattr(agent, "battery_last_accounted_at", None)
        last = now if last_raw is None else float(last_raw)
        if now > last:
            elapsed = max(0.0, now - last)
            remaining_raw = getattr(agent, "battery_remaining_budget_min", period)
            remaining = float(remaining_raw if remaining_raw is not None else period)
            if agent.charging_started_at is not None:
                target = float(agent.charging_target_budget_min or period)
                duration = max(1e-9, float(agent.charging_duration_min or 0.0))
                start_budget = float(agent.charging_start_budget_min or 0.0)
                progress = min(1.0, max(0.0, (now - float(agent.charging_started_at)) / duration))
                agent.battery_remaining_budget_min = min(target, start_budget + (target - start_budget) * progress)
            else:
                agent.battery_remaining_budget_min = max(0.0, remaining - elapsed * self._battery_drain_rate_multiplier(agent))
            agent.battery_last_accounted_at = now
        budget_raw = getattr(agent, "battery_remaining_budget_min", period)
        return float(budget_raw if budget_raw is not None else period)

    def _reset_worker_battery(self, agent: Agent) -> None:
        now = float(self.env.now)
        agent.last_battery_swap = now
        agent.battery_remaining_budget_min = float(self.battery_swap_period_min)
        agent.battery_last_accounted_at = now
        agent.battery_accounting_swap_at = now
        agent.charging_started_at = None
        agent.charging_start_budget_min = None
        agent.charging_target_budget_min = None
        agent.charging_duration_min = 0.0
        agent.charging_dock_id = None

    def _assigned_charging_dock(self, agent: Agent) -> str:
        return f"charging_dock_{agent.agent_id}"

    def _worker_at_assigned_charging_dock(self, agent: Agent) -> bool:
        if self.grid_map is None or agent.tile is None:
            return self.agent_display_location(agent) == "BatteryStation"
        return agent.tile in self.grid_map.service_tiles.get(self._assigned_charging_dock(agent), [])

    def _battery_service_target(self, agent: Agent) -> str:
        return self._assigned_charging_dock(agent) if self.battery_direct_charge_enabled else "battery_rack"

    def _battery_service_margin_min(self) -> float:
        return max(3.0, float(self.movement_cfg.get("setup_min", 3.0)) + 1.0)

    def _battery_swap_service_min(self, agent: Agent) -> float:
        origin = self.agent_display_location(agent)
        target = self._battery_service_target(agent)
        return float(self.travel_time(origin, target)) + float(self.agent_cfg["battery_pickup_time_min"])

    def _battery_mandatory_threshold(self, agent: Agent) -> float:
        configured = float(self._rule("world.battery.mandatory_swap_threshold_min", 15.0))
        if self.battery_direct_charge_enabled:
            configured = max(
                configured,
                float(self.battery_swap_period_min) * float(self.battery_charge_low_threshold_ratio),
            )
        if getattr(self, "rolling_horizon_enabled", False):
            configured = max(configured, float(self.battery_swap_period_min) * float(self.rolling_horizon_battery_low_ratio))
        physical = self._battery_swap_service_min(agent) + self._battery_service_margin_min()
        return max(configured, physical)

    def _battery_proactive_swap_threshold(self, agent: Agent) -> float:
        if getattr(self, "rolling_horizon_enabled", False):
            return self._battery_mandatory_threshold(agent)
        return self._battery_mandatory_threshold(agent) + max(6.0, float(self.movement_cfg.get("unload_min", 2.0)) + 4.0)

    def _battery_low_alert_threshold(self, agent: Agent) -> float:
        if getattr(self, "rolling_horizon_enabled", False):
            return self._battery_mandatory_threshold(agent)
        configured = float(self._rule("world.battery.deliver_to_others_threshold_min", 15.0))
        return max(self._battery_proactive_swap_threshold(agent), min(configured, 24.0))

    def _battery_delivery_trigger_threshold(self, agent: Agent) -> float:
        if getattr(self, "rolling_horizon_enabled", False):
            return self._battery_mandatory_threshold(agent)
        return self._battery_mandatory_threshold(agent) + 2.0

    def _humanoid_power_event_for_battery(self, agent: Agent) -> str:
        remaining = float(self.battery_remaining(agent))
        if agent.discharged:
            return "disabled"
        if remaining <= self._battery_mandatory_threshold(agent):
            return "power_critical"
        if remaining <= self._battery_low_alert_threshold(agent):
            return "power_low"
        return "power_normal"

    def _close_interrupted_direct_charge(
        self,
        agent: Agent,
        *,
        task_id: str,
        reason: str,
    ) -> float:
        if agent.charging_started_at is None:
            return 0.0
        charge_started_at = float(agent.charging_started_at)
        dock_id = str(agent.charging_dock_id or self._assigned_charging_dock(agent))
        planned_duration = max(0.0, float(agent.charging_duration_min or 0.0))
        self._update_battery_accounting(agent)
        actual_duration = max(0.0, float(self.env.now) - charge_started_at)
        current_budget = max(0.0, float(agent.battery_remaining_budget_min or 0.0))
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="BATTERY_CHARGE_INTERRUPTED",
            entity_id=agent.agent_id,
            location=dock_id,
            details={
                "task_id": str(task_id),
                "charging_dock_id": dock_id,
                "actual_charge_duration_min": round(actual_duration, 6),
                "planned_charge_duration_min": round(planned_duration, 6),
                "final_soc": round(
                    current_budget / max(1e-9, float(self.battery_swap_period_min)),
                    6,
                ),
                "reason": str(reason or "task_interrupted"),
            },
        )
        agent.charging_started_at = None
        agent.charging_start_budget_min = None
        agent.charging_target_budget_min = None
        agent.charging_duration_min = 0.0
        agent.charging_dock_id = None
        self._sync_humanoid_power_state(agent)
        return actual_duration

    def _sync_humanoid_power_state(self, agent: Agent) -> None:
        if agent.charging_started_at is not None:
            return
        event_type = self._humanoid_power_event_for_battery(agent)
        target_power = {
            "power_normal": "POWER_NORMAL",
            "power_low": "POWER_LOW",
            "power_critical": "POWER_CRITICAL",
            "disabled": "DEPLETED",
        }[event_type]
        current_power = str((agent.humanoid_state or {}).get("power", "")).strip().upper()
        if current_power == target_power:
            return
        if event_type == "disabled":
            self._set_humanoid_disabled_state(agent, reason="battery_depleted")
            return
        self._transition_humanoid_state(
            agent,
            event_type,
            source="mansim.power",
            metadata={
                "battery_remaining_min": round(float(self.battery_remaining(agent)), 3),
                "battery_period_min": round(float(getattr(self, "battery_swap_period_min", 0.0) or 0.0), 3),
                "low_threshold_ratio": round(float(getattr(self, "rolling_horizon_battery_low_ratio", 0.2) or 0.2), 6),
                "low_threshold_min": round(float(self._battery_low_alert_threshold(agent)), 3),
                "critical_threshold_min": round(float(self._battery_mandatory_threshold(agent)), 3),
            },
        )

    def _battery_monitor_sleep_min(self, agent: Agent, eps: float = 1e-6) -> float:
        if agent.charging_started_at is not None:
            elapsed = max(0.0, float(self.env.now) - float(agent.charging_started_at))
            return max(eps, min(1.0, max(0.0, float(agent.charging_duration_min) - elapsed)))
        remaining = max(0.0, float(self.battery_remaining(agent)))
        critical = max(0.0, float(self._battery_mandatory_threshold(agent)))
        low = max(critical, float(self._battery_low_alert_threshold(agent)))
        if remaining > low + eps:
            wait = max(eps, remaining - low)
            return min(1.0, wait) if self.battery_direct_charge_enabled else wait
        if remaining > critical + eps:
            wait = max(eps, remaining - critical)
            return min(1.0, wait) if self.battery_direct_charge_enabled else wait
        wait = max(eps, remaining)
        return min(1.0, wait) if self.battery_direct_charge_enabled else wait

    def _task_estimated_duration(self, agent: Agent, task: Task) -> float:
        priority_key = str(task.priority_key or task.task_type).strip().lower()
        task_code = str(task.task_code or TASK_CODE_BY_PRIORITY_KEY.get(priority_key, "")).strip().upper()
        service_expected = self.timing.expected_task_duration(task_code) if task_code in self.timing.task_steps else 0.0

        if priority_key in {"battery_swap", "battery_charge"}:
            return float(self.travel_time(self.agent_display_location(agent), self._battery_service_target(agent))) + service_expected
        if priority_key == "repair_machine":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            active_helpers = self._repair_team_size(machine)
            future_team_size = min(self.max_repair_agents, active_helpers + (0 if agent.agent_id in machine.repair_team else 1))
            future_team_size = max(1, future_team_size)
            action_expected = self.timing.expected_call_duration("REPAIR_MACHINE", "EXECUTE_MAINTENANCE_ACTION")
            return (
                float(self.travel_time(self.agent_display_location(agent), machine.machine_id))
                + max(0.0, service_expected - action_expected)
                + (action_expected / future_team_size)
            )
        if priority_key == "preventive_maintenance":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            return float(self.travel_time(self.agent_display_location(agent), machine.machine_id)) + service_expected
        if priority_key == "unload_machine":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            output_buffer_id = f"output_buffer_station_{machine.station}"
            return (
                float(self.travel_time(self.agent_display_location(agent), machine.machine_id))
                + service_expected
                + float(self.travel_time(machine.machine_id, output_buffer_id))
            )
        if priority_key == "load_machine":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            source = str(task.payload.get("source") or "")
            if not source:
                slot = str(task.payload.get("load_slot") or "material").strip().lower()
                source = f"{'intermediate' if slot == 'intermediate' else 'material'}_queue_{machine.station}"
            return (
                float(self.travel_time(self.agent_display_location(agent), source))
                + float(self.travel_time(source, machine.machine_id))
                + service_expected
            )
        if priority_key == "setup_machine":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            return float(self.travel_time(self.agent_display_location(agent), machine.machine_id)) + service_expected
        if priority_key == "inspect_product":
            return (
                float(self.travel_time(self.agent_display_location(agent), self.inspection_workstation_id))
                + service_expected
            )
        if priority_key in {"load_inspection_desk", "unload_inspection_desk"}:
            source = str(task.payload.get("source") or "")
            destination = str(task.payload.get("destination") or "")
            return (
                float(self.travel_time(self.agent_display_location(agent), source))
                + float(self.travel_time(source, destination))
                + service_expected
            )
        if priority_key == "handover_item":
            source_agent = self.agents.get(str(task.payload.get("source_agent_id", "")))
            session = self.product_transport_sessions.get(str(task.payload.get("transport_session_id", "")))
            if source_agent is None or not isinstance(session, dict):
                return 0.0
            travel = self._helper_join_catch_min(agent, source_agent)
            if not math.isfinite(travel):
                return 0.0
            remaining = self._product_session_remaining_travel_min(session, future_extra_carriers=1)
            return travel + remaining + service_expected
        if priority_key == "material_supply":
            station = int(task.payload.get("station", 1) or 1)
            return (
                float(self.travel_time(self.agent_display_location(agent), "Warehouse"))
                + float(self.travel_time("Warehouse", f"material_queue_{station}"))
                + service_expected
            )
        if priority_key in {"inter_station_transfer", "battery_delivery_low_battery", "battery_delivery_discharged"}:
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "inter_station":
                from_station = int(task.payload.get("from_station", 1) or 1)
                from_location = "Inspection" if from_station == self.inspection_queue_station else f"Station{from_station}"
                if from_station == self.inspection_queue_station:
                    to_location = "Warehouse"
                else:
                    next_station = from_station + 1
                    to_location = f"Station{next_station}" if next_station <= self.last_processing_station else "Inspection"
                from_target = f"output_buffer_station_{from_station}"
                to_target = "warehouse_buffer" if from_station == self.inspection_queue_station else (
                    f"intermediate_queue_{from_station + 1}" if (from_station + 1) <= self.last_processing_station else "intermediate_queue_4"
                )
                return (
                    float(self.travel_time(self.agent_display_location(agent), from_target))
                    + float(self.travel_time(from_location, to_target))
                    + service_expected
                )
            if transfer_kind == "battery_delivery":
                target_agent = self.agents.get(str(task.payload.get("target_agent_id", "")))
                if target_agent is None:
                    return service_expected
                return (
                    float(self.travel_time(self.agent_display_location(agent), "battery_rack"))
                    + float(self.travel_time("battery_rack", self.agent_display_location(target_agent)))
                    + service_expected
                )
        target = str(task.location or task.payload.get("target") or "")
        return (float(self.travel_time(self.agent_display_location(agent), target)) if target else 0.0) + service_expected

    def _emit_low_battery_alert_if_needed(self, agent: Agent) -> None:
        if agent.discharged:
            agent.low_battery_alerted = False
            return
        battery_remaining = self.battery_remaining(agent)
        threshold = self._battery_low_alert_threshold(agent)
        if battery_remaining > threshold:
            agent.low_battery_alerted = False
            return
        if agent.low_battery_alerted:
            return
        agent.low_battery_alerted = True
        details = {
            "battery_remaining_min": round(float(battery_remaining), 3),
            "threshold_min": round(float(threshold), 3),
        }
        self.emit_incident(
            "worker_low_battery",
            affected_entities=[agent.agent_id],
            escalation_level="worker_local",
            details=details,
            notify_workers=[agent.agent_id],
        )
        self._rolling_horizon_collect_immediate_candidates("worker_low_battery")

    def _battery_interrupt_exempt(self, agent: Agent) -> bool:
        return bool(getattr(agent, "battery_swap_critical", False))

    def _should_interrupt_for_battery(self, agent: Agent, eps: float = 1e-6) -> bool:
        if self._battery_interrupt_exempt(agent):
            return False
        return agent.discharged or self.battery_remaining(agent) <= eps

    def _terminate_simulation(self, reason: str) -> None:
        if self.terminated:
            return
        self.pending_termination_reason = ""
        self.pending_termination_agent_id = ""
        self.terminated = True
        self.termination_reason = str(reason)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="SIM_TERMINATED",
            entity_id="system",
            location="Factory",
            details={"reason": self.termination_reason, "objective_mode": self.objective_mode},
        )
        if not self.termination_event.triggered:
            self.termination_event.succeed(self.termination_reason)

    def _request_simulation_termination(self, reason: str, *, after_agent_id: str = "") -> None:
        agent_id = str(after_agent_id or "").strip()
        agent = self.agents.get(agent_id) if agent_id else None
        if agent is not None and agent.current_task_id:
            self.pending_termination_reason = str(reason)
            self.pending_termination_agent_id = agent_id
            return
        self._terminate_simulation(reason)

    def terminate_at_objective_limit(self) -> None:
        if self.minimize_makespan_enabled and not self.terminated:
            self._terminate_simulation("makespan_max_days_reached")

    def check_all_agents_discharged(self) -> None:
        if self.terminated:
            return
        if self.depleted_recovery_enabled:
            return
        if self.agents and all(a.discharged for a in self.agents.values()):
            self._terminate_simulation("all_agents_discharged")

    def _schedule_depleted_worker_recovery(self, agent: Agent) -> None:
        if not self.depleted_recovery_enabled:
            return
        current_day = int(float(self.env.now) // max(1.0, float(self.minutes_per_day))) + 1
        due_day = current_day + 1
        due_min = float(current_day * self.minutes_per_day)
        agent.depleted_recovery_due_day = due_day
        agent.depleted_recovery_due_min = due_min
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="WORKER_RECOVERY_SCHEDULED",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={
                "recovery_day": due_day,
                "recovery_at_min": round(due_min, 6),
                "restart_location": self._assigned_charging_dock(agent),
                "restart_soc": round(self.depleted_recovery_restart_soc, 6),
            },
        )

    def _recover_depleted_workers_at_day_start(self, day: int) -> None:
        if not self.depleted_recovery_enabled:
            return
        for agent in self.agents.values():
            due_day = getattr(agent, "depleted_recovery_due_day", None)
            if not agent.discharged or due_day is None or int(due_day) > int(day):
                continue
            old_location = self.agent_display_location(agent)
            old_tile = agent.tile
            dock_id = self._assigned_charging_dock(agent)
            if self.grid_map is not None:
                self.grid_map.release_reservation(agent.agent_id)
                target_tile = self.grid_map.initial_worker_tile(agent.agent_id)
                self.grid_map.move_worker(agent.agent_id, target_tile)
                agent.tile = target_tile
                agent.location = self.grid_map.zone_for_tile(target_tile) or "BatteryStation"
            else:
                agent.location = "BatteryStation"
            self._clear_in_transit(agent)
            self._clear_current_move(agent)
            self._reset_worker_battery(agent)
            agent.battery_remaining_budget_min = (
                float(self.battery_swap_period_min) * self.depleted_recovery_restart_soc
            )
            agent.battery_last_accounted_at = float(self.env.now)
            agent.discharged = False
            agent.discharged_since = None
            agent.low_battery_alerted = False
            agent.awaiting_battery_from = None
            agent.battery_service_owner = None
            agent.suspended_task = None
            agent.depleted_recovery_due_day = None
            agent.depleted_recovery_due_min = None
            details = {
                "from_location": old_location,
                "to_location": agent.location,
                "from_tile": self._tile_payload(old_tile),
                "to_tile": self._tile_payload(agent.tile),
                "charging_dock_id": dock_id,
                "restart_soc": round(self.depleted_recovery_restart_soc, 6),
                "relocation_kind": "scheduled_external_recovery",
            }
            self.logger.log(
                t=self.env.now,
                day=int(day),
                event_type="WORKER_RETURNED_NEXT_DAY",
                entity_id=agent.agent_id,
                location=agent.location,
                details=details,
            )
            # Record the authorized external relocation before publishing a
            # state snapshot at the new tile. Replay/audit consumers can then
            # reset the worker's spatial anchor without treating it as travel.
            self._transition_humanoid_state(
                agent,
                "task_completed",
                reason="next_day_depleted_recovery",
                source="mansim.depleted_recovery",
                metadata={"cargo_present": False, "power_normal": True},
            )
            self.logger.log(
                t=self.env.now,
                day=int(day),
                event_type="AGENT_RECHARGED",
                entity_id=agent.agent_id,
                location=agent.location,
                details={**details, "recovery_source": "next_day_external_recovery"},
            )
            self._rolling_horizon_notify_worker(agent.agent_id)
            self._rolling_horizon_request_candidate_refresh("worker_returned_next_day")

    def _clear_in_transit(self, agent: Agent) -> None:
        agent.in_transit_from = None
        agent.in_transit_to = None
        agent.in_transit_progress = 0.0
        agent.in_transit_total_min = 0.0
        agent.reserved_tile = None
        agent.movement_path = []
        agent.movement_target_tile = None

    def _clear_current_move(self, agent: Agent) -> None:
        agent.current_move_id = None
        agent.current_move_segment_index = 0
        agent.current_move_segment_from_tile = None
        agent.current_move_segment_to_tile = None
        agent.current_move_segment_event_started = False
        agent.current_move_logical_destination = None
        agent.current_move_started_at = None

    def _close_current_move_segment(
        self,
        agent: Agent,
        *,
        logical_destination: str | None = None,
        reason: str = "segment_cancelled",
    ) -> None:
        """Cancel an in-flight tile segment without claiming arrival.

        This path is used before the segment timeout completes (incident,
        battery depletion, or process interruption). A tile-end event would
        make Replay move the worker to a tile that the grid never committed.
        """
        move_id = str(agent.current_move_id or "")
        segment_index = int(agent.current_move_segment_index or 0)
        from_tile = agent.current_move_segment_from_tile
        to_tile = agent.current_move_segment_to_tile
        if not move_id or segment_index <= 0 or from_tile is None or to_tile is None:
            return
        if self.traffic_monitor is not None:
            self.traffic_monitor.cancel_segment(agent.agent_id, move_id, segment_index)
        if bool(getattr(agent, "current_move_segment_event_started", False)):
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="AGENT_MOVE_TILE_CANCELLED",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={
                    "move_id": move_id,
                    "segment_index": segment_index,
                    "from_tile": self._tile_payload(from_tile),
                    "to_tile": self._tile_payload(to_tile),
                    "committed_tile": self._tile_payload(agent.tile),
                    "logical_destination": str(logical_destination or agent.current_move_logical_destination or ""),
                    "status": "interrupted",
                    "reason": str(reason or "segment_cancelled"),
                },
            )
        agent.current_move_segment_index = 0
        agent.current_move_segment_from_tile = None
        agent.current_move_segment_to_tile = None
        agent.current_move_segment_event_started = False

    def _log_interrupted_move(self, agent: Agent, *, reason: str, logical_destination: str | None = None) -> None:
        move_id = str(agent.current_move_id or "")
        if not move_id:
            return
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="AGENT_MOVE_END",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={
                "from": str(agent.in_transit_from or agent.location),
                "to": str(logical_destination or agent.current_move_logical_destination or agent.in_transit_to or ""),
                "from_tile": self._tile_payload(agent.movement_path[0]) if agent.movement_path else self._tile_payload(agent.tile),
                "to_tile": self._tile_payload(agent.tile),
                "move_id": move_id,
                "status": "interrupted",
                "reason": reason,
                "humanoid_state": self._humanoid_state_payload(agent),
            },
        )

    def _set_in_transit(self, agent: Agent, from_zone: str, to_zone: str, progress: float, total_min: float) -> None:
        agent.in_transit_from = str(from_zone)
        agent.in_transit_to = str(to_zone)
        agent.in_transit_progress = min(1.0, max(0.0, float(progress)))
        agent.in_transit_total_min = max(0.0, float(total_min))

    def _has_in_transit_position(self, agent: Agent) -> bool:
        if not agent.in_transit_from or not agent.in_transit_to:
            return False
        if agent.in_transit_total_min <= 1e-9:
            return False
        p = float(agent.in_transit_progress)
        return 1e-6 < p < (1.0 - 1e-6)

    def _edge_location_label(self, from_zone: str, to_zone: str, progress: float) -> str:
        pct = int(round(min(1.0, max(0.0, float(progress))) * 100.0))
        return f"{from_zone}->{to_zone}({pct}%)"

    def worker_display_location(self, worker: Worker) -> str:
        if self._has_in_transit_position(worker):
            return self._edge_location_label(str(worker.in_transit_from), str(worker.in_transit_to), float(worker.in_transit_progress))
        return str(worker.location)

    def agent_display_location(self, agent: Worker) -> str:
        # Deprecated alias for orchestration/dashboard compatibility.
        return self.worker_display_location(agent)

    def _move_on_edge(
        self,
        agent: Agent,
        edge_from: str,
        edge_to: str,
        start_progress: float,
        end_progress: float,
        *,
        emit_move_events: bool = True,
    ):
        total = max(1e-6, self.travel_time(edge_from, edge_to))
        start_p = min(1.0, max(0.0, float(start_progress)))
        end_p = min(1.0, max(0.0, float(end_progress)))
        duration = abs(end_p - start_p) * total
        self._set_in_transit(agent, edge_from, edge_to, start_p, total)

        if duration <= 1e-9:
            self._set_in_transit(agent, edge_from, edge_to, end_p, total)
            return

        eps = 1e-6
        if self._should_interrupt_for_battery(agent, eps):
            if not agent.discharged:
                self.discharge_agent(agent, reason="battery_depleted", interrupt_process=False)
            raise simpy.Interrupt("battery_depleted")

        if emit_move_events:
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="AGENT_EDGE_MOVE_START",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={
                    "from": edge_from,
                    "to": edge_to,
                    "duration": round(duration, 3),
                    "start_progress": round(start_p, 4),
                    "end_progress": round(end_p, 4),
                },
            )

        move_start_t = self.env.now
        try:
            yield self.env.timeout(duration)
        except simpy.Interrupt as intr:
            elapsed = max(0.0, self.env.now - move_start_t)
            frac = min(1.0, max(0.0, elapsed / max(1e-6, duration)))
            current_p = start_p + (end_p - start_p) * frac
            self._set_in_transit(agent, edge_from, edge_to, current_p, total)
            if emit_move_events:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="AGENT_EDGE_MOVE_INTERRUPTED",
                    entity_id=agent.agent_id,
                    location=self.agent_display_location(agent),
                    details={
                        "from": edge_from,
                        "to": edge_to,
                        "duration": round(duration, 3),
                        "elapsed": round(elapsed, 3),
                        "progress": round(current_p, 4),
                        "reason": str(intr.cause),
                    },
                )
            raise

        if self._should_interrupt_for_battery(agent, eps):
            self._set_in_transit(agent, edge_from, edge_to, end_p, total)
            if not agent.discharged:
                self.discharge_agent(agent, reason="battery_depleted", interrupt_process=False)
            raise simpy.Interrupt("battery_depleted")

        self._set_in_transit(agent, edge_from, edge_to, end_p, total)
        if emit_move_events:
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="AGENT_EDGE_MOVE_END",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={
                    "from": edge_from,
                    "to": edge_to,
                    "duration": round(duration, 3),
                    "start_progress": round(start_p, 4),
                    "end_progress": round(end_p, 4),
                },
            )

    def _move_agent_to_in_transit_position(
        self,
        mover: Agent,
        target: Agent,
        *,
        emit_move_events: bool = True,
        meet_destination: bool = False,
    ) -> str | None:
        if meet_destination:
            destination = str(target.current_move_logical_destination or target.in_transit_to or target.location)
            if not destination:
                return None
            yield from self.move_agent(mover, destination, emit_move_events=emit_move_events)
            return destination

        if self.grid_map is not None:
            yield from self.move_agent(mover, target.agent_id, emit_move_events=emit_move_events)
            return self.agent_display_location(target)

        if not self._has_in_transit_position(target):
            yield from self.move_agent(mover, target.location, emit_move_events=emit_move_events)
            for _ in range(2):
                if mover.location == target.location:
                    break
                yield from self.move_agent(mover, target.location, emit_move_events=emit_move_events)
            if mover.location != target.location:
                return None
            return str(target.location)

        edge_from = str(target.in_transit_from)
        edge_to = str(target.in_transit_to)
        progress = float(target.in_transit_progress)
        total = max(1e-6, float(target.in_transit_total_min))
        dist_from = progress * total
        dist_to = (1.0 - progress) * total

        via_from = self.travel_time(mover.location, edge_from) + dist_from
        via_to = self.travel_time(mover.location, edge_to) + dist_to

        if via_from <= via_to:
            entry_zone = edge_from
            start_p = 0.0
            end_p = progress
        else:
            entry_zone = edge_to
            start_p = 1.0
            end_p = progress

        yield from self.move_agent(mover, entry_zone, emit_move_events=emit_move_events)
        if abs(end_p - start_p) > 1e-9:
            yield from self._move_on_edge(
                mover,
                edge_from,
                edge_to,
                start_p,
                end_p,
                emit_move_events=emit_move_events,
            )
        return self._edge_location_label(edge_from, edge_to, progress)

    def _agents_in_handover_contact(self, provider: Agent, receiver: Agent) -> bool:
        """Return true only when a battery handoff is physically plausible."""
        if self.grid_map is None:
            return provider.location == receiver.location
        if provider.tile is None or receiver.tile is None:
            return False
        return abs(int(provider.tile[0]) - int(receiver.tile[0])) + abs(int(provider.tile[1]) - int(receiver.tile[1])) <= 1

    def _ensure_battery_handover_contact(
        self,
        provider: Agent,
        receiver: Agent,
        *,
        emit_move_events: bool = True,
    ):
        """Move the provider into actual tile contact before completing delivery.

        Zone-level location equality is not enough for battery delivery in the
        tile-map factory. The provider must reach the receiver's current worker
        service tile so the replay and the state transition agree about where
        the handoff happened.
        """
        if self._agents_in_handover_contact(provider, receiver):
            return self.agent_display_location(receiver)

        if self.grid_map is not None:
            self._set_humanoid_primitive_hint(provider, "NAVIGATE_TO")
            yield from self.move_agent(provider, receiver.agent_id, emit_move_events=emit_move_events)
            if self._agents_in_handover_contact(provider, receiver):
                return self.agent_display_location(receiver)
            return None

        if provider.location != receiver.location:
            self._set_humanoid_primitive_hint(provider, "NAVIGATE_TO")
            yield from self.move_agent(provider, receiver.location, emit_move_events=emit_move_events)
        if provider.location != receiver.location:
            return None
        return str(receiver.location)

    def _wait_for_agent_at_battery_handover_destination(
        self,
        provider: Agent,
        receiver: Agent,
        destination: str,
    ):
        """Wait for a moving battery receiver at its planned destination.

        Battery delivery should not chase a worker tile-by-tile. If the receiver
        is already moving, the provider goes to the receiver's current logical
        destination and pauses there until the receiver arrives.
        """
        while self._has_in_transit_position(receiver):
            yield self.env.timeout(0.1)

        return (yield from self._ensure_battery_handover_contact(provider, receiver, emit_move_events=True))

    def discharge_agent(
        self,
        agent: Agent,
        reason: str = "battery_depleted",
        interrupt_process: bool = True,
    ) -> None:
        if agent.discharged:
            return
        agent.discharged = True
        agent.discharged_since = self.env.now
        agent.low_battery_alerted = False
        details: dict[str, Any] = {
            "reason": reason,
            "current_task_id": str(agent.current_task_id or ""),
            "current_task_type": str(agent.current_task_type or ""),
            "during_move": bool(agent.current_move_id or self._has_in_transit_position(agent)),
            "battery_risk": copy.deepcopy(
                (agent.current_task_selection_meta or {}).get("battery_risk", {})
            ),
        }
        if self._has_in_transit_position(agent):
            details.update(
                {
                    "in_transit_from": str(agent.in_transit_from),
                    "in_transit_to": str(agent.in_transit_to),
                    "in_transit_progress": round(float(agent.in_transit_progress), 4),
                }
            )
        self._set_humanoid_disabled_state(agent, reason=reason)
        details["humanoid_state"] = self._humanoid_state_payload(agent)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="AGENT_DISCHARGED",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details=details,
        )
        self.emit_incident(
            "worker_discharged",
            affected_entities=[agent.agent_id],
            blocked_commitments=[agent.current_commitment_id] if agent.current_commitment_id else [],
            escalation_level="worker_local",
            details=details,
            notify_workers=[agent.agent_id],
        )
        self._schedule_depleted_worker_recovery(agent)
        if interrupt_process and agent.process_ref is not None and agent.process_ref.is_alive:
            agent.process_ref.interrupt("battery_depleted")
        self.check_all_agents_discharged()

    def start_agent_task(self, agent: Agent, task: Task, start_t: float) -> None:
        depleted_dock_charge = bool(
            agent.discharged
            and task.task_type == "BATTERY_CHARGE"
            and self.battery_direct_charge_enabled
            and self._worker_at_assigned_charging_dock(agent)
        )
        if agent.discharged and not depleted_dock_charge:
            raise RuntimeError(
                f"Discharged worker {agent.agent_id} cannot start task {task.task_type}; "
                "only charging at its assigned dock is allowed."
            )
        agent.current_task_id = task.task_id
        agent.current_task_type = task.task_type
        agent.current_task_code = task.task_code or ""
        agent.current_task_instance_id = task.instance_id or ""
        agent.current_task_payload = copy.deepcopy(task.payload) if isinstance(task.payload, dict) else {}
        agent.current_task_selection_meta = (
            copy.deepcopy(task.selection_meta) if isinstance(task.selection_meta, dict) else {}
        )
        agent.current_child_task_code = None
        agent.current_child_task_name = None
        agent.current_child_task_instance_id = None
        agent.current_task_path = None
        agent.current_task_depth = 0
        agent.current_task_started_at = start_t
        self._transition_humanoid_state(
            agent,
            # HumanoidSim deliberately disallows DISABLED -> ASSIGNED. A fully
            # depleted worker already at its own dock starts the recovery action
            # directly; the regular assignment lifecycle remains unchanged.
            "task_started" if depleted_dock_charge else "task_assigned",
            reason="depleted_dock_charge_started" if depleted_dock_charge else "task_selected",
            source="mansim.task_selection",
            task=task,
            status="running" if depleted_dock_charge else "pending",
            reconcile_power=not depleted_dock_charge,
        )
        selection = dict(task.selection_meta) if isinstance(task.selection_meta, dict) else {}
        details: dict[str, Any] = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "priority_key": self._task_priority_key(task),
            "task_code": task.task_code,
            "task_name": task.task_spec_name,
            "instance_id": task.instance_id,
            "assigned_robot_id": task.assigned_robot_id,
            "current_step_id": agent.current_step_id or "",
            "primitive_call_code": agent.current_primitive_call_code or "",
            "payload": task.payload,
            "args": task.args,
            "humanoid": task.humanoid,
            "humanoid_state": self._humanoid_state_payload(agent),
            "selection": selection,
            "agent_role": self.current_agent_role(agent.agent_id),
            "commitment_id": agent.current_commitment_id,
        }
        for key in (
            "task_rule_id",
            "role_number",
            "role_task_code",
            "role_owner_agent_id",
            "role_display_name",
        ):
            if key in selection:
                details[key] = copy.deepcopy(selection.get(key))
        if selection:
            if "decision_source" in selection:
                details["decision_source"] = selection.get("decision_source")
            if "decision_rule" in selection:
                details["decision_rule"] = selection.get("decision_rule")
            if "decision_rationale" in selection:
                details["decision_rationale"] = selection.get("decision_rationale")
            if "decision_trace_id" in selection:
                details["decision_trace_id"] = selection.get("decision_trace_id")
            if "expected_task_signature" in selection:
                details["expected_task_signature"] = selection.get("expected_task_signature")
        self.logger.log(
            t=start_t,
            day=self.day_for_time(start_t),
            event_type="AGENT_TASK_START",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details=details,
        )

    def _availability_after_incomplete_task(self, status: str, reason: str) -> str:
        normalized_status = str(status or "").strip().lower()
        normalized_reason = str(reason or "").strip().lower()
        temporary_wait_reasons = {
            "battery_swap_wait",
            "horizon_reached",
            "traffic_wait",
            "resource_wait",
            "task_suspended",
        }
        profile = self._humanoid_incident_profile(reason)
        if profile is not None:
            availability = getattr(profile, "default_availability", "BLOCKED")
            availability_value = str(getattr(availability, "value", availability) or "BLOCKED").strip().upper()
            return availability_value or "BLOCKED"
        if normalized_status in {"failed", "skipped"}:
            return "BLOCKED"
        if normalized_status == "interrupted" and normalized_reason not in temporary_wait_reasons:
            return "BLOCKED"
        return "WAITING"

    @staticmethod
    def _task_recovered_before_incomplete_end(agent: Agent, task: Task) -> bool:
        recovered_attr = str(getattr(agent, "last_recovery_completed_task_id", "") or "").strip()
        if recovered_attr and recovered_attr == str(getattr(task, "task_id", "") or "").strip():
            return True
        state = getattr(agent, "humanoid_state", None)
        if not isinstance(state, dict):
            return False
        metadata = state.get("metadata")
        if not isinstance(metadata, dict) or str(metadata.get("source", "")).strip() != "mansim.recovery_end":
            return False
        task_id = str(getattr(task, "task_id", "") or "").strip()
        instance_id = str(getattr(task, "instance_id", "") or "").strip()
        recovered_task_id = str(metadata.get("task_id", "") or "").strip()
        recovered_instance_id = str(metadata.get("task_instance_id", "") or "").strip()
        return bool(
            (task_id and task_id == recovered_task_id)
            or (instance_id and instance_id == recovered_instance_id)
        )

    def finish_agent_task(self, agent: Agent, task: Task, start_t: float, status: str, reason: str = "") -> None:
        end_t = self.env.now
        duration = max(0.0, end_t - start_t)
        selection = dict(task.selection_meta) if isinstance(task.selection_meta, dict) else {}
        role_event_details = {
            key: copy.deepcopy(selection.get(key))
            for key in (
                "task_rule_id",
                "role_number",
                "role_task_code",
                "role_owner_agent_id",
                "role_display_name",
            )
            if key in selection
        }
        preserve_carrying = status == "interrupted" and reason in {"battery_depleted", "battery_swap_wait", "horizon_reached"}
        preserve_reservations = preserve_carrying and agent.suspended_task is task
        if not preserve_reservations:
            self._release_task_domain_owner(agent, task, reason=f"{status}:{reason}")
            self._release_task_item_reservations(task, reason=f"{status}:{reason}")
            self._release_task_buffer_slot(task, reason=f"{status}:{reason}")
        if (not preserve_carrying) and (agent.carrying_item_id is not None or agent.carrying_item_type is not None):
            self._clear_agent_carrying(agent, destination=agent.location, emit_event=True)
        recovered_before_incomplete_end = status != "completed" and self._task_recovered_before_incomplete_end(agent, task)
        if recovered_before_incomplete_end:
            self._transition_humanoid_state(
                agent,
                "task_completed",
                task=task,
                status="completed",
                reason="recovery_completed",
                source="mansim.recovery_end",
                metadata={"cargo_present": bool(agent.carrying_item_id or getattr(agent, "carrying_item_ids", []))},
            )
        self.logger.log(
            t=end_t,
            day=self.day_for_time(end_t),
            event_type="AGENT_TASK_END",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={
                "task_id": task.task_id,
                "task_type": task.task_type,
                "task_code": task.task_code,
                "task_name": task.task_spec_name,
                "instance_id": task.instance_id,
                "status": status,
                "duration": round(duration, 3),
                "reason": reason,
                "payload": task.payload,
                "args": task.args,
                "humanoid": task.humanoid,
                "humanoid_state": self._humanoid_state_payload(agent),
                "selection": selection,
                **role_event_details,
            },
        )
        if agent.discharged:
            self._transition_humanoid_state(
                agent,
                "disabled",
                task=task,
                status=status,
                reason=reason or status,
                source="mansim.task_end",
            )
        elif status == "completed":
            self._transition_humanoid_state(
                agent,
                "task_completed",
                task=task,
                status=status,
                reason="task_completed",
                source="mansim.task_end",
                metadata={"cargo_present": bool(agent.carrying_item_id or getattr(agent, "carrying_item_ids", []))},
            )
        else:
            if not recovered_before_incomplete_end:
                availability = self._availability_after_incomplete_task(status, reason)
                current_availability = str((agent.humanoid_state or {}).get("availability", "")).strip().upper()
                if current_availability == "BLOCKED" and availability == "WAITING":
                    # Do not erase an unresolved incident with a temporary wait
                    # reason. HumanoidSim intentionally forbids BLOCKED->WAITING;
                    # the recovery/replan path must resolve BLOCKED first.
                    availability = "BLOCKED"
                state_reason, state_metadata = self._humanoid_incident_metadata_for_reason(reason)
                if not state_reason:
                    state_reason = reason or status
                if state_metadata:
                    state_metadata.update({"cargo_present": bool(agent.carrying_item_id or getattr(agent, "carrying_item_ids", []))})
                else:
                    state_metadata = {"cargo_present": bool(agent.carrying_item_id or getattr(agent, "carrying_item_ids", []))}
                self._transition_humanoid_state(
                    agent,
                    "blocked" if availability == "BLOCKED" else "waiting",
                    task=task,
                    status=status,
                    reason=state_reason,
                    source="mansim.task_end",
                    metadata=state_metadata,
                )
            else:
                agent.last_recovery_completed_task_id = None
                agent.last_recovery_completed_at = None
        self.task_records.append(
            {
                "day": self.day_for_time(end_t),
                "agent_id": agent.agent_id,
                "task_id": task.task_id,
                "task_type": task.task_type,
                "humanoid_task_code": task.task_code,
                "humanoid_task_name": task.task_spec_name,
                "humanoid_instance_id": task.instance_id,
                "priority_key": self._task_priority_key(task),
                "status": status,
                "start_t": start_t,
                "end_t": end_t,
                "duration": duration,
                "decision_source": str(selection.get("decision_source", "")),
                "decision_rule": str(selection.get("decision_rule", "")),
                "decision_trace_id": str(selection.get("decision_trace_id", "")),
                "expected_task_signature": selection.get("expected_task_signature", {}),
                **role_event_details,
            }
        )
        if status == "completed":
            agent.total_task_time_min[task.task_type] = agent.total_task_time_min.get(task.task_type, 0.0) + duration
            if str(selection.get("decision_source", "")).strip() == "worker_local_response" and isinstance(agent.incident_backlog, list) and agent.incident_backlog:
                agent.incident_backlog = list(agent.incident_backlog[1:])
            consumed_incident_work_orders = self._consume_incident_work_order_matches(agent.agent_id, task)
            consumed_commitments = self._consume_commitment_matches(agent.agent_id, task)
            consumed_orders = self._consume_personal_queue_matches(agent.agent_id, task)
            consumed_messages = self._consume_mailbox_matches(agent.agent_id, task)
            if consumed_incident_work_orders or consumed_commitments or consumed_orders or consumed_messages:
                self.logger.log(
                    t=end_t,
                    day=self.day_for_time(end_t),
                    event_type="ORCHESTRATION_ACK",
                    entity_id=agent.agent_id,
                    location=self.agent_display_location(agent),
                    details={
                        "task_id": task.task_id,
                        "task_type": task.task_type,
                        "consumed_incident_work_orders": consumed_incident_work_orders,
                        "consumed_commitments": consumed_commitments,
                        "consumed_orders": consumed_orders,
                        "consumed_messages": consumed_messages,
                    },
                )
        agent.current_task_id = None
        agent.current_task_type = None
        agent.current_task_code = None
        agent.current_task_instance_id = None
        agent.current_task_payload = {}
        agent.current_task_selection_meta = {}
        agent.current_child_task_code = None
        agent.current_child_task_name = None
        agent.current_child_task_instance_id = None
        agent.current_task_path = None
        agent.current_task_depth = 0
        agent.current_step_id = None
        agent.current_step_call_code = None
        agent.current_step_path = None
        agent.current_step_depth = 0
        agent.current_primitive_call_code = None
        agent.current_task_started_at = None
        agent.current_commitment_id = None
        self._rolling_horizon_request_candidate_refresh("task_finished")
        pending_reason = str(getattr(self, "pending_termination_reason", "") or "")
        pending_agent_id = str(getattr(self, "pending_termination_agent_id", "") or "")
        if pending_reason and pending_agent_id == agent.agent_id:
            reason_to_emit = pending_reason
            self.pending_termination_reason = ""
            self.pending_termination_agent_id = ""
            self._terminate_simulation(reason_to_emit)

    def handle_task_interruption(self, agent: Agent, task: Task, reason: str) -> None:
        recovery_depletion = bool(reason == "battery_depleted" and self.depleted_recovery_enabled)
        if reason in {"battery_depleted", "battery_swap_wait"} and not recovery_depletion:
            if (
                reason == "battery_depleted"
                and task.task_type == "BATTERY_CHARGE"
                and self.battery_direct_charge_enabled
                and not self._worker_at_assigned_charging_dock(agent)
            ):
                if agent.battery_service_owner == agent.agent_id:
                    agent.battery_service_owner = None
                agent.suspended_task = None
                task.payload["failure_reason"] = "battery_depleted_before_charging_dock"
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="BATTERY_CHARGE_ABORTED",
                    entity_id=agent.agent_id,
                    location=self.agent_display_location(agent),
                    details={
                        "task_type": task.task_type,
                        "task_id": task.task_id,
                        "charging_dock_id": self._assigned_charging_dock(agent),
                        "reason": "battery_depleted_before_charging_dock",
                    },
                )
                return
            if task.task_type == "REPAIR_MACHINE":
                machine = self.machines.get(task.payload.get("machine_id"))
                if machine is not None:
                    self._leave_repair_team(machine, agent.agent_id, reason=reason)
                agent.suspended_task = task
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="TASK_SUSPENDED",
                    entity_id=agent.agent_id,
                    location=self.agent_display_location(agent),
                    details={"task_type": task.task_type, "task_id": task.task_id, "reason": reason},
                )
                self.emit_incident(
                    "commitment_blocked",
                    affected_entities=[agent.agent_id],
                    blocked_commitments=[agent.current_commitment_id] if agent.current_commitment_id else [],
                    escalation_level="worker_local",
                    details={"reason": reason, "task_type": task.task_type},
                    notify_workers=[agent.agent_id],
                )
                return
            if task.task_type in {"BATTERY_SWAP", "BATTERY_CHARGE"} and agent.battery_service_owner == agent.agent_id:
                # If an agent gets discharged while trying to self-swap,
                # allow others to deliver a battery for rescue.
                agent.battery_service_owner = None
            # Keep the task and ownership locks as-is so no one else can take over.
            # The same agent will resume it after recharge.
            agent.suspended_task = task
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="TASK_SUSPENDED",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={"task_type": task.task_type, "task_id": task.task_id, "reason": reason},
            )
            self.emit_incident(
                "commitment_blocked",
                affected_entities=[agent.agent_id],
                blocked_commitments=[agent.current_commitment_id] if agent.current_commitment_id else [],
                escalation_level="worker_local",
                details={"reason": reason, "task_type": task.task_type},
                notify_workers=[agent.agent_id],
            )
            return

        if recovery_depletion:
            agent.suspended_task = None
            task.payload["_depletion_recovery_abandoned"] = True
            self._drop_agent_cargo_due_to_depletion(agent, task)

        if task.task_type in {"BATTERY_SWAP", "BATTERY_CHARGE"}:
            if agent.battery_service_owner == agent.agent_id:
                agent.battery_service_owner = None

        elif task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).lower()

            if transfer_kind == "battery_delivery":
                target_id = str(task.payload.get("target_agent_id", ""))
                target = self.agents.get(target_id)
                if target is not None and target.battery_service_owner == agent.agent_id:
                    target.battery_service_owner = None
            elif transfer_kind == "inter_station":
                from_station = int(task.payload.get("from_station", 1))
                moved_id = task.payload.pop("transfer_item_id", None)
                if moved_id is None:
                    moved_id = task.payload.pop("transfer_intermediate_id", None)
                if moved_id is not None and str(moved_id) not in self.dropped_items:
                    self._appendleft_if_absent(self.output_buffers[from_station], str(moved_id))
            elif transfer_kind == "material_supply":
                station = int(task.payload.get("station", 1))
                if self.material_supply_owner.get(station) == agent.agent_id:
                    self.material_supply_owner[station] = None

        elif task.task_type == "LOAD_MACHINE":
            machine = self.machines.get(task.payload.get("machine_id"))
            station = machine.station if machine is not None else int(task.payload.get("station", 1))
            load_slot = str(task.payload.get("load_slot") or "").strip().lower()
            item_id = task.payload.get("item_id")
            if item_id is None:
                item_id = task.payload.get("material_id")
            if item_id is None:
                item_id = task.payload.get("intermediate_id")
            item_id = str(item_id or "").strip()
            loaded_on_machine = False
            if machine is not None and item_id:
                if load_slot == "intermediate":
                    loaded_on_machine = str(machine.input_intermediate or "") == item_id
                else:
                    loaded_on_machine = str(machine.input_material or "") == item_id
            task.payload.pop("item_id", None)
            task.payload.pop("material_id", None)
            task.payload.pop("intermediate_id", None)
            if item_id and item_id in self.dropped_items:
                pass
            elif item_id is not None:
                if loaded_on_machine:
                    pass
                elif load_slot == "intermediate" and station in self.intermediate_queues:
                    self._appendleft_if_absent(self.intermediate_queues[station], str(item_id))
                else:
                    self._appendleft_if_absent(self.material_queues[station], str(item_id))
            if machine is not None:
                if machine.setup_owner == agent.agent_id:
                    machine.setup_owner = None
                if machine.state == MachineState.SETUP:
                    self._set_machine_state(machine, MachineState.WAIT_INPUT, reason=reason)

        elif task.task_type == "SETUP_MACHINE":
            machine = self.machines.get(task.payload.get("machine_id"))
            if machine is not None:
                if machine.setup_owner == agent.agent_id:
                    machine.setup_owner = None
                if machine.state == MachineState.SETUP:
                    self._set_machine_state(machine, MachineState.WAIT_INPUT, reason=reason)

        elif task.task_type == "UNLOAD_MACHINE":
            machine = self.machines.get(task.payload.get("machine_id"))
            if machine is not None and machine.unload_owner == agent.agent_id:
                machine.unload_owner = None

        elif task.task_type == "INSPECT_PRODUCT":
            # Keep the workstation owner until worker_work_loop has moved the
            # interrupted worker off the desk service tile. finish_agent_task()
            # releases the owner after physical egress.
            pass

        elif task.task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            product_id = str(task.payload.get("inspection_product_id") or task.payload.get("item_id") or "").strip()
            action = str(task.payload.get("interface_action") or task.payload.get("action") or "").strip().lower()
            placed = bool(task.payload.get("_inspection_interface_placed"))
            if product_id and not placed and product_id not in self.dropped_items:
                if action == "load" and self.inspection_desk_item_id != product_id:
                    self._appendleft_if_absent(self.intermediate_queues[self.inspection_queue_station], product_id)
                    self._set_item_state(
                        product_id,
                        ItemState.WAITING_INSPECTION,
                        location="Inspection",
                        ref=f"intermediate_queue_{self.inspection_queue_station}",
                        item_type="product",
                    )
                elif action == "unload" and self.inspection_desk_item_id in {None, product_id}:
                    result = str(task.payload.get("inspection_result") or "").strip().upper()
                    self._set_inspection_desk_state(
                        "INSPECTED_WAITING_UNLOAD",
                        item_id=product_id,
                        result=result,
                        reason=reason,
                    )
                    self._set_item_state(
                        product_id,
                        ItemState.INSPECTED_WAITING_UNLOAD,
                        location="Inspection",
                        ref=self.inspection_workstation_id,
                        item_type="product",
                    )
            # Owner release is deliberately deferred to finish_agent_task().
            # Releasing it here exposes the next inspection stage while this
            # interrupted worker still physically blocks the sole desk tile.

        elif task.task_type == "REPAIR_MACHINE":
            machine = self.machines.get(task.payload.get("machine_id"))
            if machine is not None:
                self._leave_repair_team(machine, agent.agent_id, reason=reason)
                if machine.broken and self._repair_team_size(machine) <= 0:
                    self._set_machine_state(machine, MachineState.BROKEN, reason=reason)

        elif task.task_type == "PREVENTIVE_MAINTENANCE":
            machine = self.machines.get(task.payload.get("machine_id"))
            if machine is not None:
                if machine.pm_owner == agent.agent_id:
                    machine.pm_owner = None
                if machine.broken:
                    self._set_machine_state(machine, MachineState.BROKEN, reason=reason)
                elif machine.output_intermediate is not None:
                    self._set_machine_state(machine, MachineState.DONE_WAIT_UNLOAD, reason=reason)
                else:
                    self._set_machine_state(machine, MachineState.WAIT_INPUT, reason=reason)

        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="TASK_INTERRUPTED",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={"task_type": task.task_type, "task_id": task.task_id, "reason": reason},
        )
        if agent.current_commitment_id:
            self.commitment_claims[agent.current_commitment_id] = {
                "agent_id": agent.agent_id,
                "status": "blocked",
                "time_min": round(float(self.env.now), 3),
                "reason": reason,
            }
        self.emit_incident(
            "commitment_blocked",
            affected_entities=[agent.agent_id, self._task_target_id(task)],
            blocked_commitments=[agent.current_commitment_id] if agent.current_commitment_id else [],
            escalation_level="worker_local",
            details={"reason": reason, "task_type": task.task_type},
            notify_workers=[agent.agent_id],
        )
        self._clear_agent_carrying(agent, emit_event=False)

    def mandatory_task_for_agent(self, agent: Agent) -> Task | None:
        if self.battery_assignment_mode == "policy_decides":
            return None
        if agent.discharged and not (
            self.battery_direct_charge_enabled
            and self.grid_map is not None
            and agent.tile in self.grid_map.service_tiles.get(self._assigned_charging_dock(agent), [])
        ):
            return None
        battery_remaining = self.battery_remaining(agent)
        threshold = self._battery_mandatory_threshold(agent)
        mandatory_priority = float(
            self._rule(
                "world.task_priority.battery_swap",
                self._rule("world.battery.mandatory_swap_priority", 150.0),
            )
        )
        if (
            battery_remaining <= threshold
            and (agent.battery_service_owner is None or agent.battery_service_owner == agent.agent_id)
        ):
            return Task(
                task_id=self._next_task_id("BAT"),
                task_type="BATTERY_CHARGE" if self.battery_direct_charge_enabled else "BATTERY_SWAP",
                priority_key="battery_charge" if self.battery_direct_charge_enabled else "battery_swap",
                priority=mandatory_priority,
                location="BatteryStation",
                payload={
                    "target_agent_id": agent.agent_id,
                    "battery_remaining_min": round(battery_remaining, 3),
                    "action": "dock_charge" if self.battery_direct_charge_enabled else "swap_battery",
                    "charging_dock_id": self._assigned_charging_dock(agent) if self.battery_direct_charge_enabled else "",
                    "target_soc": self.battery_charge_target_soc,
                },
            )
        return None

    def _proactive_battery_swap_task(self, agent: Agent) -> Task | None:
        if agent.discharged:
            return None
        battery_remaining = self.battery_remaining(agent)
        threshold = self._battery_proactive_swap_threshold(agent)
        if battery_remaining > threshold:
            return None
        if agent.battery_service_owner is not None and agent.battery_service_owner != agent.agent_id:
            return None
        proactive_priority = float(self._rule("world.task_priority.battery_swap", 150.0))
        return Task(
            task_id=self._next_task_id("BAT"),
            task_type="BATTERY_CHARGE" if self.battery_direct_charge_enabled else "BATTERY_SWAP",
            priority_key="battery_charge" if self.battery_direct_charge_enabled else "battery_swap",
            priority=proactive_priority,
            location="BatteryStation",
            payload={
                "target_agent_id": agent.agent_id,
                "battery_remaining_min": round(float(battery_remaining), 3),
                "battery_safety_guard": True,
                "action": "dock_charge" if self.battery_direct_charge_enabled else "swap_battery",
                "charging_dock_id": self._assigned_charging_dock(agent) if self.battery_direct_charge_enabled else "",
                "target_soc": self.battery_charge_target_soc,
            },
        )

    def current_personal_queue(self, agent_id: str) -> list[dict[str, Any]]:
        queue = self.current_job_plan.personal_queues.get(str(agent_id), []) if isinstance(self.current_job_plan.personal_queues, dict) else []
        return list(queue) if isinstance(queue, list) else []

    def current_incident_work_order_queue(self, agent_id: str) -> list[dict[str, Any]]:
        work_orders = self.current_job_plan.incident_work_orders.get(str(agent_id), []) if isinstance(self.current_job_plan.incident_work_orders, dict) else []
        return list(work_orders) if isinstance(work_orders, list) else []

    def current_commitment_queue(self, agent_id: str) -> list[dict[str, Any]]:
        commitments = self.current_job_plan.commitments.get(str(agent_id), []) if isinstance(self.current_job_plan.commitments, dict) else []
        return list(commitments) if isinstance(commitments, list) else []

    def current_mailbox(self, agent_id: str) -> list[dict[str, Any]]:
        mailbox = self.current_job_plan.mailbox.get(str(agent_id), []) if isinstance(self.current_job_plan.mailbox, dict) else []
        return list(mailbox) if isinstance(mailbox, list) else []

    def _llm_commitment_path_active(self) -> bool:
        return self.decision_mode == "llm_planner" and self.worker_execution_mode == "commitment"

    def _candidate_signature_hash(self, candidates: list[Task]) -> str:
        opportunity_ids = sorted({self._task_opportunity_id(task) for task in candidates})
        raw = "|".join(opportunity_ids) if opportunity_ids else "no_candidates"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16].upper()

    def _resolve_selection_blocker(self, agent_id: str, *, reason: str) -> None:
        active_id = str(self.active_selection_blocker_by_agent.get(str(agent_id), "")).strip()
        if not active_id:
            return
        blocker = self.selection_blockers.get(active_id)
        if isinstance(blocker, dict):
            blocker["status"] = "resolved"
            blocker["resolved_at_min"] = round(float(self.env.now), 3)
            blocker["resolution_reason"] = str(reason).strip() or "resolved"
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="SELECTION_BLOCKER_RESOLVED",
                entity_id=active_id,
                location=self.agent_display_location(self.agents[str(agent_id)]),
                details=dict(blocker),
            )
        self.active_selection_blocker_by_agent.pop(str(agent_id), None)

    def _resolve_all_selection_blockers(self, *, reason: str) -> None:
        for agent_id in list(self.active_selection_blocker_by_agent.keys()):
            self._resolve_selection_blocker(str(agent_id), reason=reason)

    def _activate_selection_blocker(
        self,
        agent: Agent,
        *,
        blocker_type: str,
        candidates: list[Task],
        details: dict[str, Any] | None = None,
        escalation_level: str = "planner",
        source_incident_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        candidate_signature_hash = self._candidate_signature_hash(candidates)
        plan_revision = self._active_plan_revision()
        key = f"{agent.agent_id}|{str(blocker_type).strip()}|{candidate_signature_hash}|{plan_revision}"
        active_id = str(self.active_selection_blocker_by_agent.get(agent.agent_id, "")).strip()
        active = self.selection_blockers.get(active_id) if active_id else None
        if isinstance(active, dict) and str(active.get("blocker_key", "")).strip() == key:
            active["last_seen_min"] = round(float(self.env.now), 3)
            return active, False
        if active_id:
            self._resolve_selection_blocker(agent.agent_id, reason="candidate_signature_changed")

        blocker_id = f"BLK-{next(self.selection_blocker_counter):05d}"
        incident = self.emit_incident(
            "replan_required",
            affected_entities=[agent.agent_id],
            blocked_commitments=[str(agent.current_commitment_id or "").strip()] if agent.current_commitment_id else [],
            escalation_level=escalation_level,
            details={
                "reason": str(blocker_type).strip(),
                "candidate_count": len(candidates),
                "candidate_signature_hash": candidate_signature_hash,
                "active_plan_revision": plan_revision,
                "blocker_id": blocker_id,
                **(details or {}),
            },
        )
        blocker = IncidentBlocker(
            blocker_id=blocker_id,
            agent_id=str(agent.agent_id),
            blocker_type=str(blocker_type).strip() or "selection_blocked",
            candidate_signature_hash=candidate_signature_hash,
            active_plan_revision=plan_revision,
            created_at_min=float(self.env.now),
            incident_id=str(incident.get("incident_id", "")).strip(),
            source_incident_id=str(source_incident_id).strip(),
            escalation_emitted=False,
            last_seen_min=float(self.env.now),
        ).to_dict()
        blocker["blocker_key"] = key
        blocker["status"] = "active"
        self.selection_blockers[blocker_id] = blocker
        self.active_selection_blocker_by_agent[agent.agent_id] = blocker_id
        self.day_unique_replan_blockers.add(blocker_id)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="SELECTION_BLOCKER",
            entity_id=blocker_id,
            location=self.agent_display_location(agent),
            details=dict(blocker),
        )
        return blocker, True

    def _mark_blocker_escalated(self, blocker_id: str) -> None:
        blocker = self.selection_blockers.get(str(blocker_id))
        if not isinstance(blocker, dict):
            return
        blocker["escalation_emitted"] = True
        self.day_planner_escalations.add(str(blocker_id))

    def current_agent_role(self, agent_id: str) -> str:
        roles = self.current_job_plan.agent_roles if isinstance(self.current_job_plan.agent_roles, dict) else {}
        return str(roles.get(str(agent_id), "")).strip()

    def current_agent_task_allowlist(self, agent_id: str) -> list[str]:
        allowlists = self.current_job_plan.agent_task_allowlists if isinstance(self.current_job_plan.agent_task_allowlists, dict) else {}
        rows = allowlists.get(str(agent_id), [])
        return [str(value).strip() for value in rows if str(value).strip()] if isinstance(rows, list) else []

    def _fixed_task_assignment_active(self) -> bool:
        return self.decision_mode == "fixed_task_assignment"

    def _mfg_flow_policy_active(self) -> bool:
        return bool(self.is_mfg_flow_shop and self.mfg_flow_task_policy is not None)

    def _adp_active(self) -> bool:
        return bool(
            getattr(self, "decision_mode", None) in {"simulation_based_adp", "random_feasible_dispatch"}
            and getattr(self, "adp_coordinator", None) is not None
        )

    def adp_dispatch_event(self, worker_id: str) -> simpy.Event:
        if not self._adp_active():
            return self.env.event()
        return self.adp_coordinator.dispatch_event(str(worker_id))

    def _select_adp_task(self, agent: Agent) -> Task | None:
        if not self._adp_active():
            return None
        task = self.adp_coordinator.pop_task(agent.agent_id)
        if task is None:
            self.adp_coordinator.request("worker_idle")
        return task

    def _mfg_flow_dedicated_policy_active(self) -> bool:
        return bool(self._mfg_flow_policy_active() and self.mfg_flow_task_policy.dedicated)

    def _mfg_flow_task_rule(self, task: Task) -> Any:
        if not self._mfg_flow_policy_active():
            return None
        task_code = str(
            task.task_code
            or TASK_CODE_BY_PRIORITY_KEY.get(self._task_priority_key(task), "")
            or task.task_type
        ).strip().upper()
        return self.mfg_flow_task_policy.rule_for_task(task, task_code)

    def _mfg_flow_allowed_worker_ids_for_task(self, task: Task) -> list[str]:
        if not self._mfg_flow_policy_active():
            return sorted(self.agents)
        task_code = str(
            task.task_code
            or TASK_CODE_BY_PRIORITY_KEY.get(self._task_priority_key(task), "")
            or task.task_type
        ).strip().upper()
        return self.mfg_flow_task_policy.allowed_worker_ids(task, task_code)

    def _mfg_flow_task_rank(self, task: Task) -> int:
        if not self._mfg_flow_policy_active():
            return 9999
        task_code = str(
            task.task_code
            or TASK_CODE_BY_PRIORITY_KEY.get(self._task_priority_key(task), "")
            or task.task_type
        ).strip().upper()
        return int(self.mfg_flow_task_policy.rank_for_task(task, task_code))

    def _mfg_flow_task_order_key(self, task: Task, agent: Agent) -> tuple[Any, ...]:
        repair_key: tuple[Any, ...] = (9, 0.0, math.inf, "")
        if str(task.task_type).strip().upper() == "REPAIR_MACHINE":
            repair_key = self._repair_urgency_sort_key(task)
        return (
            self._mfg_flow_task_rank(task),
            *repair_key,
            float(self.travel_time(agent.location, task.location)),
            float(self._task_estimated_duration(agent, task)),
            str(task.task_id),
        )

    def _task_battery_risk_metadata(
        self, agent: Agent, task: Task, *, estimated_duration: float | None = None,
    ) -> dict[str, Any]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        family = self._task_priority_key(task)
        estimated_duration = max(0.0, float(
            self._task_estimated_duration(agent, task) if estimated_duration is None else estimated_duration
        ))
        if family in {"battery_swap", "battery_charge"}:
            return_to_dock = 0.0
        else:
            task_end = str(
                payload.get("destination")
                or payload.get("destination_buffer_id")
                or payload.get("machine_id")
                or task.location
                or self.agent_display_location(agent)
            )
            return_to_dock = max(
                0.0,
                float(self.travel_time(task_end, self._battery_service_target(agent))),
            )
        reserve_margin = 0.0 if family in {"battery_swap", "battery_charge"} else self._battery_service_margin_min()
        required = estimated_duration + return_to_dock + reserve_margin
        remaining = max(0.0, float(self.battery_remaining(agent)))
        margin = remaining - required
        return {
            "battery_remaining_min": round(remaining, 6),
            "estimated_task_duration_min": round(estimated_duration, 6),
            "estimated_return_to_dock_min": round(return_to_dock, 6),
            "estimated_battery_required_min": round(required, 6),
            "expected_battery_margin_min": round(margin, 6),
            "battery_depletion_risk": bool(margin < 0.0),
        }

    def _filter_candidates_for_agent(self, agent: Agent, candidates: list[Task]) -> list[Task]:
        filtered = list(candidates)
        battery_reserve = self._battery_swap_service_min(agent) + self._battery_service_margin_min()
        if getattr(self, "rolling_horizon_enabled", False):
            # A rolling-window dispatch can leave a worker waiting until the next
            # boundary before a battery task is selected. Keep that scheduling
            # latency in the reserve calculation so a feasible production task
            # cannot strand the worker just short of the charger/helper.
            battery_reserve += max(0.0, float(getattr(self, "rolling_horizon_window_min", 0.0) or 0.0))
        elif self._adp_active():
            battery_reserve += max(
                0.0,
                float(getattr(self.adp_coordinator, "max_review_interval_min", 0.0) or 0.0),
            )
        delivery_receiver = (
            self._rolling_horizon_dedicated_roles_active()
            and agent.agent_id in set(getattr(self, "rolling_horizon_battery_delivery_receiver_agent_ids", []))
        )
        if self.battery_assignment_mode != "policy_decides" and self._rolling_horizon_self_battery_swap_due(agent):
            self_swaps = [task for task in filtered if self._rolling_horizon_is_self_battery_swap(task, agent)]
            if self_swaps:
                # A worker that owns MANAGE_ROBOT_POWER must service itself
                # before taking delivery or production work. Otherwise an aged
                # delivery candidate can strand the battery-service provider.
                filtered = self_swaps
        battery_safe: list[Task] = []
        for task in filtered:
            risk_metadata = self._task_battery_risk_metadata(agent, task)
            if self.expose_battery_risk_metadata:
                task.payload.update(risk_metadata)
                task.selection_meta["battery_risk"] = copy.deepcopy(risk_metadata)
            family = self._task_priority_key(task)
            if family in {"battery_swap", "battery_charge", "battery_delivery_low_battery", "battery_delivery_discharged"}:
                battery_safe.append(task)
                continue
            if self.battery_assignment_mode == "policy_decides":
                battery_safe.append(task)
                continue
            if delivery_receiver:
                # Dedicated battery receivers are serviced by a provider. They
                # should keep executing normal production work while delivery is
                # in flight; only actual depletion or the final handoff pause
                # should stop their work loop.
                battery_safe.append(task)
                continue
            if self.battery_remaining(agent) >= self._task_estimated_duration(agent, task) + battery_reserve:
                battery_safe.append(task)
        filtered = battery_safe
        if self._mfg_flow_dedicated_policy_active():
            filtered = [
                task
                for task in filtered
                if agent.agent_id in set(self._mfg_flow_allowed_worker_ids_for_task(task))
            ]
        if not self._fixed_task_assignment_active():
            return filtered
        allowlist = set(self.current_agent_task_allowlist(agent.agent_id))
        if not allowlist and not FIXED_TASK_BATTERY_EXCEPTION_FAMILIES:
            return []
        scoped: list[Task] = []
        for task in filtered:
            family = self._task_priority_key(task)
            if family in allowlist or family in FIXED_TASK_BATTERY_EXCEPTION_FAMILIES:
                scoped.append(task)
        return scoped

    def _bind_humanoid_candidate_for_agent(self, agent: Agent, task: Task | None) -> Task | None:
        if task is None:
            return None
        runtime = getattr(self, "humanoid_runtime", None)
        if runtime is None or not getattr(runtime, "enabled", False):
            return task
        return runtime.bind_candidate(agent, task)

    def _bind_humanoid_candidates_for_agent(self, agent: Agent, candidates: list[Task]) -> list[Task]:
        bound: list[Task] = []
        for task in candidates:
            candidate = self._bind_humanoid_candidate_for_agent(agent, task)
            if candidate is not None:
                bound.append(candidate)
        return bound

    def _select_battery_safety_task(self, candidates: list[Task], agent: Agent) -> Task | None:
        if self.battery_assignment_mode == "policy_decides":
            return None
        if self.battery_remaining(agent) > self._battery_proactive_swap_threshold(agent):
            return None
        battery_candidates = [
            task for task in candidates
            if self._task_priority_key(task) in {"battery_swap", "battery_charge", "battery_delivery_discharged", "battery_delivery_low_battery"}
        ]
        if not battery_candidates:
            return None

        def _battery_rank(task: Task) -> tuple[int, tuple[float, float, float, str, str]]:
            family = self._task_priority_key(task)
            order = {"battery_swap": 0, "battery_charge": 0, "battery_delivery_discharged": 1, "battery_delivery_low_battery": 2}
            return order.get(family, 9), self._task_sort_key(task, agent)

        chosen = sorted(battery_candidates, key=_battery_rank)[0]
        return self._annotate_task_selection(
            chosen,
            decision_source="hard_constraint",
            decision_rule="battery_safety_guard",
            rationale="Worker preserved enough remaining battery to complete swap or critical battery assistance before taking more production work.",
            candidate_count=len(battery_candidates),
            score_hint=self._task_score(chosen, agent),
            decision_focus=[self._task_priority_key(chosen)],
        )

    def _task_target_station(self, task: Task) -> int | None:
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "material_supply":
                try:
                    return int(task.payload.get("station"))
                except (TypeError, ValueError):
                    return None
            if transfer_kind == "inter_station":
                try:
                    return int(task.payload.get("from_station"))
                except (TypeError, ValueError):
                    return None
        if task.task_type in {"LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            try:
                return int(task.payload.get("station")) if task.payload.get("station") not in {None, ""} else None
            except (TypeError, ValueError):
                machine_id = str(task.payload.get("machine_id", ""))
                machine = self.machines.get(machine_id)
                return int(machine.station) if machine is not None else None
        if task.task_type in {"INSPECT_PRODUCT", "LOAD_UNLOAD_TRANSFER_INTERFACE"}:
            return self.inspection_queue_station
        if task.task_type == "HANDOVER_ITEM":
            return None
        return None

    @staticmethod
    def _task_is_material_supply_for_station(task: Task | None, station: int) -> bool:
        if task is None:
            return False
        payload = task.payload if isinstance(task.payload, dict) else {}
        if str(getattr(task, "task_type", "") or "").strip().upper() != "TRANSFER":
            return False
        if str(payload.get("transfer_kind", "") or "").strip().lower() != "material_supply":
            return False
        try:
            return int(payload.get("station", 0) or 0) == int(station)
        except (TypeError, ValueError):
            return False

    def _active_material_supply_task_ids_for_station(self, station: int) -> list[str]:
        active: list[str] = []
        for agent in self.agents.values():
            payload = copy.deepcopy(getattr(agent, "current_task_payload", {}) or {})
            current_task = Task(
                task_id=str(agent.current_task_id or ""),
                task_type=str(agent.current_task_type or ""),
                priority_key="",
                priority=0.0,
                location=str(agent.location),
                payload=payload,
                task_code=str(agent.current_task_code or ""),
                instance_id=str(agent.current_task_instance_id or ""),
                assigned_robot_id=agent.agent_id,
            )
            if current_task.task_id and self._task_is_material_supply_for_station(current_task, station):
                active.append(current_task.task_id)
            suspended = agent.suspended_task if isinstance(agent.suspended_task, Task) else None
            if suspended is not None and self._task_is_material_supply_for_station(suspended, station):
                active.append(str(suspended.task_id or ""))
        return sorted({task_id for task_id in active if task_id})

    @staticmethod
    def _task_is_material_load_for_machine(task: Task | None, machine_id: str) -> bool:
        if task is None or str(task.task_type).strip().upper() != "LOAD_MACHINE":
            return False
        payload = task.payload if isinstance(task.payload, dict) else {}
        return (
            str(payload.get("machine_id", "")).strip() == str(machine_id).strip()
            and str(payload.get("load_slot", "")).strip().lower() == "material"
        )

    def _machine_material_load_in_progress(self, machine_id: str) -> bool:
        for agent in self.agents.values():
            current = Task(
                task_id=str(agent.current_task_id or ""),
                task_type=str(agent.current_task_type or ""),
                priority_key="",
                priority=0.0,
                location=str(agent.location),
                payload=copy.deepcopy(getattr(agent, "current_task_payload", {}) or {}),
            )
            if current.task_id and self._task_is_material_load_for_machine(current, machine_id):
                return True
            suspended = agent.suspended_task if isinstance(agent.suspended_task, Task) else None
            if self._task_is_material_load_for_machine(suspended, machine_id):
                return True
        return False

    def _station_material_replenishment_required(self, station: int) -> bool:
        if not self.is_mfg_flow_shop:
            material_cfg = self.inventory_targets.get("material", {})
            target = int(material_cfg[f"station{station}"])
            return len(self.material_queues[station]) < target

        return self._station_material_replenishment_count(station) > 0

    def _station_material_replenishment_count(self, station: int) -> int:
        if not self.is_mfg_flow_shop:
            return 1 if self._station_material_replenishment_required(station) else 0

        # Flow-shop replenishment is driven only by currently unmet machine
        # demand. Queue contents and already-reserved inbound slots are supply;
        # there is deliberately no line-side target stock.
        demand = 0
        for machine_id in self.machines_by_station.get(station, []):
            machine = self.machines[machine_id]
            if (
                not machine.broken
                and machine.state == MachineState.WAIT_INPUT
                and machine.output_intermediate is None
                and machine.input_material is None
                and not self._machine_material_load_in_progress(machine_id)
            ):
                demand += 1
        buffer_id = f"material_queue_{station}"
        available_supply = len(self.material_queues[station]) + self._buffer_reserved_count(buffer_id)
        unmet = max(0, demand - available_supply)
        return min(unmet, self._buffer_available_slots(buffer_id))

    def _task_target_id(self, task: Task) -> str:
        if task.task_type in {"BATTERY_SWAP", "BATTERY_CHARGE"}:
            return str(task.payload.get("target_agent_id", ""))
        if task.task_type in {"LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            return str(task.payload.get("machine_id", ""))
        if task.task_type in {"INSPECT_PRODUCT", "LOAD_UNLOAD_TRANSFER_INTERFACE"}:
            return "inspection"
        if task.task_type == "HANDOVER_ITEM":
            return str(task.payload.get("source_agent_id", ""))
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "battery_delivery":
                return str(task.payload.get("target_agent_id", ""))
            if transfer_kind == "material_supply":
                return f"station{task.payload.get('station', '')}"
            if transfer_kind == "inter_station":
                try:
                    if int(task.payload.get("from_station", 0) or 0) == self.inspection_queue_station:
                        return "warehouse_buffer"
                except (TypeError, ValueError):
                    pass
                return f"station{task.payload.get('from_station', '')}"
        return ""

    def _task_target_type(self, task: Task) -> str:
        if task.task_type in {"BATTERY_SWAP", "BATTERY_CHARGE"}:
            return "agent"
        if task.task_type in {"LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            return "machine"
        if task.task_type in {"INSPECT_PRODUCT", "LOAD_UNLOAD_TRANSFER_INTERFACE"}:
            return "inspection_workstation"
        if task.task_type == "HANDOVER_ITEM":
            return "agent"
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "battery_delivery":
                return "agent"
            if transfer_kind in {"material_supply", "inter_station"}:
                return "station"
        return "none"

    def _task_shareable(self, task: Task) -> bool:
        if (
            self._rolling_horizon_dedicated_roles_active()
            and not self.is_mfg_flow_shop
            and str(task.task_type).strip().upper() == "REPAIR_MACHINE"
        ):
            return False
        return str(task.task_type).strip().upper() == "REPAIR_MACHINE"

    def _task_capacity(self, task: Task) -> int:
        if (
            self._rolling_horizon_dedicated_roles_active()
            and not self.is_mfg_flow_shop
            and str(task.task_type).strip().upper() == "REPAIR_MACHINE"
        ):
            return 1
        if str(task.task_type).strip().upper() == "REPAIR_MACHINE":
            machine = self.machines.get(str(task.payload.get("machine_id", "") or ""))
            if machine is not None:
                return max(1, self._repair_slots_remaining(machine))
            return self.max_repair_agents
        return 1

    def _task_why_available(self, task: Task) -> str:
        if task.task_type == "HANDOVER_ITEM":
            return "A product is already in transit and another humanoid can join the shared carry."
        if task.task_type == "REPAIR_MACHINE":
            return "A broken machine is idle and can be repaired immediately."
        if task.task_type == "UNLOAD_MACHINE":
            return "A machine has finished output waiting for unload."
        if task.task_type == "LOAD_MACHINE":
            return "A required machine input slot can be filled from a station queue."
        if task.task_type == "SETUP_MACHINE":
            return "A machine has all required inputs loaded and needs recipe, fixture, or program setup before processing."
        if task.task_type == "PREVENTIVE_MAINTENANCE":
            return "A machine is idle and due for preventive maintenance."
        if task.task_type == "INSPECT_PRODUCT":
            return "A staged product is waiting on the inspection workstation for quality evaluation."
        if task.task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            action = str(task.payload.get("interface_action") or task.payload.get("action") or "").strip().lower()
            if action == "load":
                return "A product in the inspection input queue can be staged on the empty inspection workstation."
            return "A classified product can be unloaded from the inspection workstation to its result queue."
        if task.task_type in {"BATTERY_SWAP", "BATTERY_CHARGE"}:
            if task.task_type == "BATTERY_CHARGE":
                return "The worker is below the battery threshold and can charge at its assigned dock after the current task."
            return "The worker is below the mandatory battery threshold and can self-swap now."
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "material_supply":
                return "A station is below its material target and the warehouse can replenish it."
            if transfer_kind == "inter_station":
                try:
                    if int(task.payload.get("from_station", 0) or 0) == self.inspection_queue_station:
                        return "Inspection pass output can be delivered to the warehouse to increase completed products."
                except (TypeError, ValueError):
                    pass
                return "A downstream transfer can move staged output to the next queue."
            if transfer_kind == "battery_delivery":
                return "Another worker needs a battery delivery and can be assisted now."
        return "The task is currently feasible."

    def _task_opportunity_id(self, task: Task) -> str:
        signature = self._task_signature(task)
        payload = task.payload if isinstance(task.payload, dict) else {}
        extra = ""
        if task.task_type == "TRANSFER":
            extra = str(payload.get("transfer_kind", "")).strip().lower()
        elif task.task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            extra = ":".join(
                [
                    str(payload.get("interface_action") or payload.get("action") or "").strip().lower(),
                    str(payload.get("inspection_product_id") or payload.get("item_id") or "").strip(),
                    str(payload.get("interface") or self.inspection_workstation_id).strip(),
                ]
            )
        elif task.task_type in {"BATTERY_SWAP", "BATTERY_CHARGE"}:
            extra = str(payload.get("target_agent_id", "")).strip().upper()
        raw = "|".join(
            [
                str(signature.get("priority_key", "")),
                str(signature.get("task_type", "")),
                str(signature.get("target_type", "")),
                str(signature.get("target_id", "")),
                str(signature.get("target_station", "")),
                extra,
            ]
        )
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12].upper()
        return f"OPP-{digest}"

    def _task_signature(self, task: Task) -> dict[str, Any]:
        return {
            "task_type": str(task.task_type),
            "priority_key": self._task_priority_key(task),
            "target_type": self._task_target_type(task),
            "target_id": self._task_target_id(task),
            "target_station": self._task_target_station(task),
        }

    def _work_order_matches_task(self, order: dict[str, Any], task: Task) -> bool:
        if not isinstance(order, dict):
            return False
        if str(order.get("task_family", "")).strip() != self._task_priority_key(task):
            return False
        target_type = str(order.get("target_type", "none")).strip().lower() or "none"
        if target_type == "none":
            return True
        if target_type == "station":
            try:
                target_station = int(order.get("target_station"))
            except (TypeError, ValueError):
                return False
            return self._task_target_station(task) == target_station
        if target_type == "machine":
            return str(order.get("target_id", "")).strip() == self._task_target_id(task)
        if target_type == "agent":
            return str(order.get("target_id", "")).strip() == self._task_target_id(task)
        if target_type == "location":
            return str(order.get("target_id", "")).strip() == str(task.location)
        return True


    def _mailbox_message_matches_task(self, message: dict[str, Any], task: Task) -> bool:
        if not isinstance(message, dict):
            return False
        if str(message.get("message_type", "")).strip().lower() not in {"assist_request", "focus_window"}:
            return False
        priority_key = self._task_priority_key(task)
        target_station = self._task_target_station(task)
        target_id = self._task_target_id(task)
        message_task = str(message.get("task_family", "")).strip()
        if message_task and message_task != priority_key:
            return False
        message_target_type = str(message.get("target_type", "none")).strip().lower() or "none"
        if message_target_type == "none":
            return True
        if message_target_type == "station":
            try:
                return int(message.get("target_station")) == target_station
            except (TypeError, ValueError):
                return False
        if message_target_type in {"machine", "agent"}:
            return str(message.get("target_id", "")).strip() == target_id
        if message_target_type == "location":
            return str(message.get("target_id", "")).strip() == str(task.location)
        return False

    def _commitment_matches_task(self, commitment: dict[str, Any], task: Task) -> bool:
        if not isinstance(commitment, dict):
            return False
        opportunity_id = str(commitment.get("opportunity_id", "")).strip()
        if opportunity_id:
            return opportunity_id == self._task_opportunity_id(task)
        if str(commitment.get("task_family", "")).strip() != self._task_priority_key(task):
            return False
        target = commitment.get("target", {}) if isinstance(commitment.get("target", {}), dict) else {}
        target_type = str(target.get("target_type", "none")).strip().lower() or "none"
        if target_type == "none":
            return True
        if target_type == "station":
            try:
                return int(target.get("target_station")) == self._task_target_station(task)
            except (TypeError, ValueError):
                return False
        if target_type in {"machine", "agent"}:
            return str(target.get("target_id", "")).strip() == self._task_target_id(task)
        if target_type == "location":
            return str(target.get("target_id", "")).strip() == str(task.location)
        return False

    def _matching_personal_queue_candidates(self, candidates: list[Task], agent: Agent) -> list[Task]:
        queue = self.current_personal_queue(agent.agent_id)
        if not queue:
            return []
        return [task for task in candidates if any(self._work_order_matches_task(order, task) for order in queue[: self.worker_queue_limit])]

    def _matching_incident_work_order_candidates(self, candidates: list[Task], agent: Agent) -> list[Task]:
        work_orders = self.current_incident_work_order_queue(agent.agent_id)
        if not work_orders:
            return []
        return [task for task in candidates if any(self._work_order_matches_task(order, task) for order in work_orders[: self.worker_queue_limit])]

    def _matching_commitment_candidates(self, candidates: list[Task], agent: Agent) -> list[Task]:
        commitments = self.current_commitment_queue(agent.agent_id)
        if not commitments:
            return []
        return [task for task in candidates if any(self._commitment_matches_task(commitment, task) for commitment in commitments[: self.worker_queue_limit])]

    def _record_manager_queue_skip(self, agent_id: str, count: int) -> None:
        if int(count or 0) <= 0:
            return
        self.manager_queue_skipped_counts[str(agent_id)] += int(count)

    def _select_incident_work_order_task(self, candidates: list[Task], agent: Agent) -> Task | None:
        work_orders = self.current_incident_work_order_queue(agent.agent_id)
        if not work_orders:
            return None
        for order in work_orders[: self.worker_queue_limit]:
            for task in candidates:
                if self._work_order_matches_task(order, task):
                    task.selection_meta = {
                        **(dict(task.selection_meta) if isinstance(task.selection_meta, dict) else {}),
                        "incident_work_order": dict(order) if isinstance(order, dict) else {"value": order},
                    }
                    return task
        return None

    def _select_planner_queue_task(self, candidates: list[Task], agent: Agent) -> Task | None:
        queue = self.current_personal_queue(agent.agent_id)
        if not queue:
            return None
        window = list(queue[: self.worker_queue_limit])
        tail = list(queue[self.worker_queue_limit :])
        skipped = 0
        for idx, order in enumerate(window):
            matching = [task for task in candidates if self._work_order_matches_task(order, task)]
            if not matching:
                skipped += 1
                continue
            self.current_job_plan.personal_queues[str(agent.agent_id)] = list(window[idx:]) + tail
            self._record_manager_queue_skip(agent.agent_id, skipped)
            return sorted(matching, key=lambda task: self._task_sort_key(task, agent))[0]
        self.current_job_plan.personal_queues[str(agent.agent_id)] = tail
        self._record_manager_queue_skip(agent.agent_id, skipped)
        return None

    def _select_commitment_task(self, candidates: list[Task], agent: Agent) -> Task | None:
        commitments = self.current_commitment_queue(agent.agent_id)
        if not commitments:
            return None
        for commitment in commitments[: self.worker_queue_limit]:
            matching = [task for task in candidates if self._commitment_matches_task(commitment, task)]
            if not matching:
                continue
            chosen = sorted(matching, key=lambda task: self._task_sort_key(task, agent))[0]
            agent.current_commitment_id = str(commitment.get("commitment_id", "")).strip() or None
            if agent.current_commitment_id and agent.current_commitment_id not in agent.claimed_commitments:
                agent.claimed_commitments.append(agent.current_commitment_id)
            if agent.current_commitment_id:
                self.commitment_claims[agent.current_commitment_id] = {
                    "agent_id": agent.agent_id,
                    "status": "claimed",
                    "time_min": round(float(self.env.now), 3),
                    "opportunity_id": str(commitment.get("opportunity_id", "")).strip(),
                }
            return chosen
        return None

    def _matching_mailbox_candidates(self, candidates: list[Task], agent: Agent) -> list[Task]:
        mailbox = self.current_mailbox(agent.agent_id)
        if not mailbox:
            return []
        return [task for task in candidates if any(self._mailbox_message_matches_task(message, task) for message in mailbox[: self.worker_queue_limit])]

    def _task_sort_key(self, task: Task, agent: Agent) -> tuple[float, float, float, str, str]:
        return (
            -self._task_score(task, agent),
            float(self.travel_time(agent.location, task.location)),
            -float(task.priority),
            self._task_priority_key(task),
            str(task.location),
        )

    def _select_mfg_flow_immediate_task(self, candidates: list[Task], agent: Agent) -> Task | None:
        if not candidates:
            return None
        task = min(
            candidates,
            key=lambda candidate: self._mfg_flow_task_order_key(candidate, agent),
        )
        rule = self._mfg_flow_task_rule(task)
        selected = self._annotate_task_selection(
            task,
            decision_source=self.decision_mode,
            decision_rule="fixed_granular_priority",
            rationale="Selected the first feasible mfg_flow_shop task by configured granular priority.",
            candidate_count=len(candidates),
            score_hint=-float(self._mfg_flow_task_rank(task)),
            decision_focus=[str(rule.rule_id)],
            fallback_reason="mfg_flow_shop_policy",
        )
        selected.selection_meta["task_rule_id"] = str(rule.rule_id)
        selected.selection_meta["role_number"] = int(rule.role_number)
        selected.selection_meta["role_task_code"] = str(rule.task_code)
        selected.selection_meta["role_display_name"] = str(rule.display_name)
        selected.selection_meta["role_owner_agent_id"] = (
            agent.agent_id if rule.kind == "self_service" else self.mfg_flow_task_policy.exclusive_owner_by_rule.get(rule.rule_id, "")
        )
        selected.selection_meta["role_policy"] = (
            "workload_balanced_dedicated" if self._mfg_flow_dedicated_policy_active() else "shared"
        )
        return selected

    def _rolling_horizon_active(self) -> bool:
        return bool(getattr(self, "rolling_horizon_enabled", False))

    def _rolling_horizon_dedicated_roles_active(self) -> bool:
        return bool(self._rolling_horizon_active() and getattr(self, "rolling_horizon_dedicated_roles_enabled", False))

    def _rolling_horizon_throughput_optimizer_active(self) -> bool:
        return bool(self._rolling_horizon_active() and getattr(self, "rolling_horizon_throughput_optimizer_enabled", False))

    def _throughput_score_payload(self, agent: Agent, task: Task) -> dict[str, Any]:
        return score_task_for_throughput(self, agent, task, self.throughput_policy_cfg)

    def _throughput_score(self, agent: Agent, task: Task) -> float:
        payload = self._throughput_score_payload(agent, task)
        return float(payload.get("total", 0.0) or 0.0)

    def _select_bottleneck_aware_task(self, candidates: list[Task], agent: Agent) -> Task | None:
        scored: list[tuple[float, float, float, str, Task, dict[str, Any]]] = []
        for task in candidates:
            payload = self._throughput_score_payload(agent, task)
            score = float(payload.get("total", 0.0) or 0.0)
            scored.append(
                (
                    -score,
                    float(self.travel_time(agent.location, task.location)),
                    float(self._task_estimated_duration(agent, task)),
                    str(task.task_id),
                    task,
                    payload,
                )
            )
        if not scored:
            return None
        scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
        _neg_score, _travel, _duration, _task_id, task, payload = scored[0]
        components = payload.get("components", {}) if isinstance(payload.get("components", {}), dict) else {}
        task = self._annotate_task_selection(
            task,
            decision_source="bottleneck_aware_dispatch",
            decision_rule="bottleneck_aware_score",
            rationale="Selected the feasible task with the highest bottleneck/throughput relief score.",
            candidate_count=len(candidates),
            score_hint=float(payload.get("total", 0.0) or 0.0),
            decision_focus=[self._task_priority_key(task)],
            fallback_reason="bottleneck_aware_score",
        )
        task.selection_meta["score_components"] = dict(components)
        self.throughput_score_values.append(float(payload.get("total", 0.0) or 0.0))
        return task

    def _rolling_horizon_task_code(self, task: Task) -> str:
        return str(task.task_code or task.task_type or "").strip().upper()

    def _rolling_horizon_transfer_kind(self, task: Task) -> str:
        payload = task.payload if isinstance(task.payload, dict) else {}
        return str(payload.get("transfer_kind", "")).strip().lower()

    def _rolling_horizon_is_battery_delivery(self, task: Task) -> bool:
        return self._rolling_horizon_transfer_kind(task) == "battery_delivery"

    def _rolling_horizon_is_self_battery_swap(self, task: Task, agent: Agent) -> bool:
        if self._task_priority_key(task) not in {"battery_swap", "battery_charge"}:
            return False
        payload = task.payload if isinstance(task.payload, dict) else {}
        return str(payload.get("target_agent_id", "")).strip() == str(agent.agent_id)

    def _rolling_horizon_self_battery_swap_due(self, agent: Agent) -> bool:
        if self.battery_assignment_mode == "policy_decides":
            return False
        if not self._rolling_horizon_active() or agent.discharged:
            return False
        if agent.battery_service_owner is not None and agent.battery_service_owner != agent.agent_id:
            return False
        if self.battery_remaining(agent) > self._battery_proactive_swap_threshold(agent):
            return False
        if self._rolling_horizon_dedicated_roles_active():
            worker_ranks = getattr(self, "rolling_horizon_worker_task_rank", {}).get(str(agent.agent_id), {})
            return "MANAGE_ROBOT_POWER" in worker_ranks
        return True

    def _rolling_horizon_role_rank_code(self, task: Task) -> str:
        # Battery delivery is a TRANSFER leaf in HumanoidSim, but for dedicated
        # roles it belongs to the configured battery-service provider role.
        if self._rolling_horizon_is_battery_delivery(task):
            return "MANAGE_ROBOT_POWER"
        return self._rolling_horizon_task_code(task)

    def _rolling_horizon_allowed_worker_ids_for_task(self, task: Task) -> list[str]:
        if self._mfg_flow_policy_active():
            return self._mfg_flow_allowed_worker_ids_for_task(task)
        if not self._rolling_horizon_dedicated_roles_active():
            return []
        task_code = self._rolling_horizon_task_code(task)
        if task_code == "HANDOVER_ITEM":
            return []
        if self._rolling_horizon_is_battery_delivery(task):
            payload = task.payload if isinstance(task.payload, dict) else {}
            target_agent_id = str(payload.get("target_agent_id", "")).strip()
            receivers = set(getattr(self, "rolling_horizon_battery_delivery_receiver_agent_ids", []))
            if receivers and target_agent_id not in receivers:
                return []
            return list(getattr(self, "rolling_horizon_battery_delivery_provider_agent_ids", []))
        rank_code = self._rolling_horizon_role_rank_code(task)
        allowed: list[str] = []
        for worker_id, ranks in getattr(self, "rolling_horizon_worker_task_rank", {}).items():
            if rank_code in ranks:
                allowed.append(str(worker_id))
        return sorted(allowed)

    def _rolling_horizon_task_allowed_for_worker(self, worker_id: str, task: Task) -> bool:
        if self._mfg_flow_policy_active():
            return str(worker_id) in set(self._mfg_flow_allowed_worker_ids_for_task(task))
        if not self._rolling_horizon_dedicated_roles_active():
            return True
        return str(worker_id) in set(self._rolling_horizon_allowed_worker_ids_for_task(task))

    def _rolling_horizon_role_owner_for_task(self, task: Task) -> str:
        allowed = self._rolling_horizon_allowed_worker_ids_for_task(task)
        return allowed[0] if len(allowed) == 1 else ""

    def _rolling_horizon_base_rank_for_worker_task(self, worker_id: str, task: Task) -> int:
        if self._mfg_flow_policy_active():
            return self._mfg_flow_task_rank(task)
        if self._rolling_horizon_dedicated_roles_active():
            rank_code = self._rolling_horizon_role_rank_code(task)
            worker_ranks = getattr(self, "rolling_horizon_worker_task_rank", {}).get(str(worker_id), {})
            if rank_code in worker_ranks:
                return int(worker_ranks[rank_code])
        return self._rolling_horizon_base_rank_for_code(self._rolling_horizon_task_code(task))

    def _rolling_horizon_base_rank_for_code(self, task_code: str) -> int:
        code = str(task_code or "").strip().upper()
        fallback_rank = len(getattr(self, "rolling_horizon_task_code_priority_order", [])) + 100
        return int(getattr(self, "rolling_horizon_task_code_rank", {}).get(code, fallback_rank))

    def _rolling_horizon_priority(self, task: Task) -> float:
        if self._mfg_flow_policy_active():
            return float(self._mfg_flow_task_rank(task))
        task_code = self._rolling_horizon_task_code(task)
        return float(self._rolling_horizon_base_rank_for_code(task_code))

    def _rolling_horizon_waited_window_count(self, entry: dict[str, Any]) -> int:
        first_window = int(entry.get("first_window_index", self.rolling_horizon_window_index) or self.rolling_horizon_window_index)
        return max(0, int(self.rolling_horizon_window_index) - first_window)

    def _rolling_horizon_effective_rank(self, entry: dict[str, Any]) -> int:
        base_rank = int(entry.get("base_priority_rank", 9999) or 9999)
        waited_windows = self._rolling_horizon_waited_window_count(entry)
        boost = int(getattr(self, "rolling_horizon_rank_boost_per_window", 1) or 0)
        return max(1, base_rank - waited_windows * boost)

    @staticmethod
    def _rolling_horizon_repair_urgency_key(entry: dict[str, Any]) -> tuple[int, float]:
        if str(entry.get("task_code", "")).strip().upper() != "REPAIR_MACHINE":
            return 9, 0.0
        tier = str(entry.get("repair_urgency_tier", "normal")).strip().lower()
        return (
            {"critical": 0, "high": 1, "normal": 2}.get(tier, 3),
            -float(entry.get("repair_urgency_score", 0.0) or 0.0),
        )

    def _rolling_horizon_task_signature(self, task: Task) -> dict[str, Any]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        item_ids = payload.get("item_ids")
        normalized_item_ids = sorted(str(item_id) for item_id in item_ids if str(item_id)) if isinstance(item_ids, list) else []
        transfer_kind = str(payload.get("transfer_kind", "")).strip().lower()
        signature_location = str(task.location)
        if str(task.task_type).strip().upper() == "TRANSFER" and transfer_kind == "battery_delivery":
            # Battery delivery is keyed by the receiver, not by the receiver's
            # transient in-transit label. Otherwise a moving low-battery worker
            # creates a fresh rolling opportunity every few tiles.
            signature_location = ""
        return {
            "task_code": self._rolling_horizon_task_code(task),
            "task_type": str(task.task_type),
            "target_type": self._task_target_type(task),
            "target_id": self._task_target_id(task),
            "target_station": self._task_target_station(task),
            "location": signature_location,
            "transfer_kind": transfer_kind,
            "interface_action": str(payload.get("interface_action") or payload.get("action") or "").strip().lower(),
            "interface": str(payload.get("interface") or ""),
            "transfer_item_id": str(
                payload.get("transfer_item_id")
                or payload.get("material_item_id")
                or payload.get("transfer_intermediate_id")
                or payload.get("inspection_product_id")
                or ""
            ),
            "source_slot_id": str(payload.get("source_slot_id") or ""),
            "destination_buffer_id": str(payload.get("destination_buffer_id") or ""),
            "destination_slot_token": str(payload.get("destination_slot_token") if payload.get("destination_slot_token") is not None else ""),
            "load_slot": str(payload.get("load_slot") or ""),
            "machine_id": str(payload.get("machine_id") or ""),
            "target_agent_id": str(payload.get("target_agent_id") or ""),
            "source_agent_id": str(payload.get("source_agent_id") or ""),
            "recipient_agent_id": str(payload.get("recipient_agent_id") or ""),
            "transport_session_id": str(payload.get("transport_session_id") or ""),
            "item_id": str(payload.get("item_id") or ""),
            "item_ids": normalized_item_ids,
            "source": str(payload.get("source") or ""),
            "destination": str(payload.get("destination") or ""),
        }

    def _rolling_horizon_opportunity_id(self, task: Task) -> str:
        # Rolling-horizon dedupe must be stricter than the legacy manager opportunity id:
        # it is keyed by HumanoidSim task code and the concrete item/resource target.
        signature = self._rolling_horizon_task_signature(task)
        raw = json.dumps(signature, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12].upper()
        return f"RHOPP-{digest}"

    def _rolling_horizon_exclusive_resource_keys(self, task: Task) -> list[str]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        keys: set[str] = set()
        transfer_kind = str(payload.get("transfer_kind", "")).strip().lower()
        if str(task.task_type).strip().upper() in {"INSPECT_PRODUCT", "LOAD_UNLOAD_TRANSFER_INTERFACE"}:
            keys.add(f"inspection_workstation:{self.inspection_workstation_id}")
        if str(task.task_type).strip().upper() == "TRANSFER" and transfer_kind == "material_supply":
            station = self._task_target_station(task)
            if station is not None and not self.is_mfg_flow_shop:
                # Material replenishment is station-scoped: while one station
                # supply opportunity is unresolved, a later window must not
                # create another station supply opportunity with a different
                # shelf item. The active task path also uses material_supply_owner
                # as a station-level lock.
                keys.add(f"material_supply_station:{station}")
        destination_buffer_id = str(payload.get("destination_buffer_id") or "").strip()
        destination_slot_token = payload.get("destination_slot_token")
        if destination_buffer_id and destination_slot_token is not None:
            keys.add(f"buffer_slot:{destination_buffer_id}:{int(destination_slot_token)}")
        if str(task.task_type).strip().upper() == "TRANSFER" and transfer_kind == "battery_delivery":
            target_agent_id = str(payload.get("target_agent_id") or "").strip()
            if target_agent_id:
                keys.add(f"battery_delivery_target:{target_agent_id}")
        if self._task_priority_key(task) in {"battery_swap", "battery_charge"}:
            target_agent_id = str(payload.get("target_agent_id") or task.assigned_robot_id or "").strip()
            if target_agent_id:
                keys.add(f"battery_service_agent:{target_agent_id}")
        source_slot_id = str(payload.get("source_slot_id") or "").strip()
        if source_slot_id:
            keys.add(f"material_slot:{source_slot_id}")
        if str(task.task_type).strip().upper() == "LOAD_MACHINE":
            machine_for_slot = str(payload.get("machine_id") or self._task_target_id(task) or "").strip()
            load_slot = str(payload.get("load_slot") or "").strip().lower()
            if machine_for_slot and load_slot:
                keys.add(f"machine_slot:{machine_for_slot}:{load_slot}")
        machine_id = str(payload.get("machine_id") or "").strip()
        if not machine_id and self._task_target_type(task) == "machine":
            machine_id = self._task_target_id(task)
        if machine_id:
            keys.add(f"machine:{machine_id}")
        item_values = [
            payload.get("transfer_item_id"),
            payload.get("material_item_id"),
            payload.get("transfer_intermediate_id"),
            payload.get("inspection_product_id"),
            payload.get("item_id"),
            payload.get("material_id"),
            payload.get("intermediate_id"),
        ]
        for item_value in item_values:
            item_id = str(item_value or "").strip()
            if item_id:
                keys.add(f"item:{item_id}")
        item_ids = payload.get("item_ids")
        if isinstance(item_ids, list):
            for item_value in item_ids:
                item_id = str(item_value or "").strip()
                if item_id:
                    keys.add(f"item:{item_id}")
        reserved_item_ids = payload.get("_reserved_item_ids")
        if isinstance(reserved_item_ids, list):
            for item_value in reserved_item_ids:
                item_id = str(item_value or "").strip()
                if item_id:
                    keys.add(f"item:{item_id}")
        return sorted(keys)

    def _rolling_horizon_queued_resource_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for queue in self.rolling_horizon_dispatch_queues.values():
            for entry in queue:
                if not isinstance(entry, dict):
                    continue
                opportunity_id = str(entry.get("opportunity_id", "") or "").strip()
                resource_keys = entry.get("exclusive_resource_keys")
                if not isinstance(resource_keys, list):
                    continue
                for key in resource_keys:
                    value = str(key or "").strip()
                    if value:
                        index[value] = opportunity_id
        return index

    def _rolling_horizon_active_resource_index(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for agent in self.agents.values():
            task_id = str(getattr(agent, "current_task_id", "") or "").strip()
            if not task_id:
                continue
            task = self._current_parent_task_stub(agent)
            for key in self._rolling_horizon_exclusive_resource_keys(task):
                value = str(key or "").strip()
                if value:
                    index[value] = task_id
        return index

    def _rolling_horizon_rebuild_pending_resource_index(self) -> None:
        self.rolling_horizon_pending_resource_index = {}
        for opportunity_id, entry in self.rolling_horizon_pending.items():
            if not isinstance(entry, dict):
                continue
            resource_keys = entry.get("exclusive_resource_keys")
            if not isinstance(resource_keys, list):
                continue
            for key in resource_keys:
                value = str(key or "").strip()
                if value:
                    self.rolling_horizon_pending_resource_index.setdefault(value, opportunity_id)

    @staticmethod
    def _rolling_horizon_entry_targets_machine(entry: dict[str, Any], machine_id: str) -> bool:
        signature = entry.get("rolling_task_signature", {})
        signature = signature if isinstance(signature, dict) else {}
        target_type = str(entry.get("target_type") or signature.get("target_type") or "").strip().lower()
        target_id = str(
            entry.get("target_id")
            or signature.get("target_id")
            or signature.get("machine_id")
            or ""
        ).strip()
        return target_type == "machine" and target_id == str(machine_id)

    def _rolling_horizon_invalidate_machine_work(self, machine_id: str) -> None:
        """Drop unstarted non-repair work that became infeasible after a breakdown."""
        if not self._rolling_horizon_active():
            return
        machine_id = str(machine_id or "").strip()
        if not machine_id:
            return

        window_index = int(self.rolling_horizon_window_index)
        pending_to_remove: list[str] = []
        for opportunity_id, entry in list(self.rolling_horizon_pending.items()):
            if not isinstance(entry, dict):
                continue
            task_code = str(entry.get("task_code", "") or "").strip().upper()
            if task_code == "REPAIR_MACHINE" or not self._rolling_horizon_entry_targets_machine(entry, machine_id):
                continue
            resource_keys = [
                str(key or "").strip()
                for key in entry.get("exclusive_resource_keys", [])
                if str(key or "").strip()
            ]
            self._rolling_horizon_log_pending_skip(
                entry,
                window_index=window_index,
                resource_keys=resource_keys,
                reason="machine_broken_invalidated",
            )
            pending_to_remove.append(str(opportunity_id))

        for opportunity_id in pending_to_remove:
            self.rolling_horizon_pending.pop(opportunity_id, None)

        queue_changed = False
        for worker_id, queue in self.rolling_horizon_dispatch_queues.items():
            if not queue:
                continue
            retained: deque[dict[str, Any]] = deque()
            agent = self.agents.get(str(worker_id))
            while queue:
                entry = queue.popleft()
                if not isinstance(entry, dict):
                    continue
                task_code = str(entry.get("task_code", "") or "").strip().upper()
                if task_code == "REPAIR_MACHINE" or not self._rolling_horizon_entry_targets_machine(entry, machine_id):
                    retained.append(entry)
                    continue
                queue_changed = True
                if agent is not None:
                    self._rolling_horizon_log_task_skip(agent, entry, "machine_broken_invalidated")
            queue.extend(retained)

        if pending_to_remove or queue_changed:
            self._rolling_horizon_rebuild_pending_resource_index()
            self._rolling_horizon_refresh_queue_metrics()

    @staticmethod
    def _rolling_horizon_immediate_repair_ignores_resource(
        *,
        collection_trigger: str,
        task_code: str,
        target_id: str,
        resource_key: str,
    ) -> bool:
        return (
            str(task_code or "").strip().upper() == "REPAIR_MACHINE"
            and str(resource_key or "").strip() == f"machine:{str(target_id or '').strip()}"
        )

    def _rolling_horizon_worker_available(self, agent: Agent) -> bool:
        if agent.discharged or agent.awaiting_battery_from is not None:
            return False
        if agent.suspended_task is not None:
            return False
        transport_session_for_worker = getattr(self, "_transport_session_for_worker", None)
        if callable(transport_session_for_worker) and transport_session_for_worker(agent) is not None:
                return False
        return True

    def _rolling_horizon_refresh_queue_metrics(self) -> None:
        total = 0
        max_length = 0
        for worker_id, queue in self.rolling_horizon_dispatch_queues.items():
            length = len(queue)
            total += length
            max_length = max(max_length, length)
            self.rolling_horizon_max_queue_length_by_worker[str(worker_id)] = max(
                int(self.rolling_horizon_max_queue_length_by_worker.get(str(worker_id), 0) or 0),
                length,
            )
        self.rolling_horizon_metrics["max_worker_queue_length"] = max(
            int(self.rolling_horizon_metrics.get("max_worker_queue_length", 0) or 0),
            max_length,
        )
        self.rolling_horizon_metrics["queued_dispatch_count"] = total

    def _rolling_horizon_urgent_queue_entry_still_feasible(
        self,
        worker_id: str,
        queue_entry: dict[str, Any],
    ) -> bool:
        agent = self.agents.get(str(worker_id))
        if agent is None:
            return False
        opportunity_id = str(queue_entry.get("opportunity_id", "") or "").strip()
        candidates = self._bind_humanoid_candidates_for_agent(
            agent,
            self._filter_candidates_for_agent(agent, self._candidate_tasks(agent)),
        )
        return any(
            self._rolling_horizon_opportunity_id(task) == opportunity_id
            and self._rolling_horizon_task_allowed_for_worker(agent.agent_id, task)
            and self._task_item_dependencies_available(task, agent)
            for task in candidates
        )

    def _rolling_horizon_requeue_unstarted_dispatches(self, window_index: int) -> int:
        """Return queued-but-not-started rolling tasks to the pending pool.

        The worker loop removes a queue entry before AGENT_TASK_START. Any entry
        still in these queues at a new window boundary has not started yet, so it
        can be re-ranked with newly collected opportunities.
        """
        now = float(self.env.now)
        requeued_count = 0
        for worker_id in sorted(list(self.rolling_horizon_dispatch_queues.keys())):
            queue = self.rolling_horizon_dispatch_queues.get(worker_id)
            if not queue:
                continue
            retained_urgent_entries: list[dict[str, Any]] = []
            while queue:
                queue_entry = queue.popleft()
                if not isinstance(queue_entry, dict):
                    continue
                if (
                    bool(queue_entry.get("urgent_dispatch", False))
                    and bool(getattr(self, "rolling_horizon_immediate_protect_from_window_requeue", False))
                ):
                    if self._rolling_horizon_urgent_queue_entry_still_feasible(worker_id, queue_entry):
                        retained_urgent_entries.append(queue_entry)
                    else:
                        agent = self.agents.get(str(worker_id))
                        if agent is not None:
                            self._rolling_horizon_log_task_skip(
                                agent,
                                queue_entry,
                                "urgent_condition_no_longer_active",
                            )
                    continue
                opportunity_id = str(queue_entry.get("opportunity_id", "") or "").strip()
                if not opportunity_id:
                    continue
                assigned_worker = str(queue_entry.get("assigned_worker_id", "") or worker_id).strip()
                entry = self.rolling_horizon_pending.get(opportunity_id)
                if entry is None:
                    entry = {
                        "opportunity_id": opportunity_id,
                        "first_window_index": int(
                            window_index
                            if queue_entry.get("first_window_index") is None
                            else queue_entry.get("first_window_index")
                        ),
                        "first_seen_min": float(
                            now if queue_entry.get("first_seen_min") is None else queue_entry.get("first_seen_min")
                        ),
                        "last_seen_min": now,
                        "task_id": str(queue_entry.get("task_id", "") or ""),
                        "task_code": str(queue_entry.get("task_code", "")),
                        "priority_key": str(queue_entry.get("priority_key", "")),
                        "task_type": str(queue_entry.get("task_type", "")),
                        "location": str(queue_entry.get("location", "CoordinationReview")),
                        "base_priority_rank": int(queue_entry.get("base_priority_rank", 9999) or 9999),
                        "effective_priority_rank": int(queue_entry.get("effective_priority_rank", 9999) or 9999),
                        "task_signature": dict(queue_entry.get("task_signature", {})),
                        "rolling_task_signature": dict(queue_entry.get("rolling_task_signature", {})),
                        "target_type": str(queue_entry.get("target_type", "")),
                        "target_id": str(queue_entry.get("target_id", "")),
                        "target_station": queue_entry.get("target_station"),
                        "shareable": bool(queue_entry.get("shareable", False)),
                        "capacity": int(queue_entry.get("capacity", 1) or 1),
                        "exclusive_resource_keys": list(queue_entry.get("exclusive_resource_keys", [])),
                        "role_policy": str(queue_entry.get("role_policy", "")),
                        "task_rule_id": str(queue_entry.get("task_rule_id", "")),
                        "role_number": int(queue_entry.get("role_number", 0) or 0),
                        "role_task_code": str(queue_entry.get("role_task_code", "")),
                        "role_display_name": str(queue_entry.get("role_display_name", "")),
                        "role_owner_agent_id": str(queue_entry.get("role_owner_agent_id", "")),
                        "allowed_worker_ids": list(queue_entry.get("allowed_worker_ids", [])),
                        "workers": set(),
                        "tasks_by_worker": {},
                        "last_logged_window_index": None,
                    }
                    self.rolling_horizon_pending[opportunity_id] = entry
                entry["last_seen_min"] = now
                entry["effective_priority_rank"] = self._rolling_horizon_effective_rank(entry)
                entry.setdefault("workers", set())
                entry.setdefault("tasks_by_worker", {})
                if assigned_worker:
                    workers = entry.get("workers")
                    if isinstance(workers, set):
                        workers.add(assigned_worker)
                requeued_count += 1
                self.rolling_horizon_metrics["requeued_task_count"] += 1
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ROLLING_HORIZON_TASK_REQUEUED",
                    entity_id=opportunity_id,
                    location=str(entry.get("location", "CoordinationReview")),
                    details={
                        **dict(queue_entry),
                        "window_index": int(window_index),
                        "requeued_window_index": int(window_index),
                        "assigned_worker_id": assigned_worker,
                        "reason": "window_boundary_replan",
                    },
                )
            if retained_urgent_entries:
                for entry in reversed(retained_urgent_entries):
                    queue.appendleft(entry)
        if requeued_count:
            self._rolling_horizon_rebuild_pending_resource_index()
            self._rolling_horizon_refresh_queue_metrics()
        elif any(self.rolling_horizon_dispatch_queues.values()):
            self._rolling_horizon_refresh_queue_metrics()
        return requeued_count

    def _rolling_horizon_initialize_worker_events(self) -> None:
        if not hasattr(self, "rolling_horizon_dispatch_events"):
            return
        self.rolling_horizon_dispatch_events = {
            agent_id: self.env.event() for agent_id in sorted(self.agents.keys())
        }

    def rolling_horizon_dispatch_event(self, agent_id: str) -> simpy.Event:
        agent_id = str(agent_id)
        event = self.rolling_horizon_dispatch_events.get(agent_id)
        if event is None or event.triggered:
            event = self.env.event()
            self.rolling_horizon_dispatch_events[agent_id] = event
        return event

    def _rolling_horizon_notify_worker(self, agent_id: str) -> None:
        agent_id = str(agent_id)
        event = self.rolling_horizon_dispatch_events.get(agent_id)
        if event is not None and not event.triggered:
            event.succeed({"worker_id": agent_id, "time_min": float(self.env.now)})
        self.rolling_horizon_dispatch_events[agent_id] = self.env.event()

    def _rolling_horizon_notify_queued_workers(self) -> None:
        for agent_id, queue in self.rolling_horizon_dispatch_queues.items():
            if queue:
                self._rolling_horizon_notify_worker(agent_id)

    def _rolling_horizon_simulation_limit_min(self) -> float:
        return float(self.num_days * self.minutes_per_day)

    def _rolling_horizon_boundary_metadata(self) -> dict[str, Any]:
        scheduled = float(
            getattr(self, "rolling_horizon_current_scheduled_boundary_min", self.env.now)
        )
        actual = float(getattr(self, "rolling_horizon_current_actual_dispatch_min", self.env.now))
        lag = max(0.0, actual - scheduled)
        return {
            "scheduled_boundary_min": round(scheduled, 6),
            "actual_dispatch_min": round(actual, 6),
            "boundary_lag_min": round(lag, 9),
            "scheduler_mode": self.rolling_horizon_scheduler_mode,
            "candidate_collection_mode": self.rolling_horizon_candidate_collection_mode,
        }

    def _rolling_horizon_initialize_strict_periodic(self) -> None:
        self.rolling_horizon_window_index = 0
        self.rolling_horizon_window_start_min = 0.0
        self.rolling_horizon_window_end_min = float(self.rolling_horizon_window_min)
        self.rolling_horizon_logged_window_index = -1
        self.rolling_horizon_current_scheduled_boundary_min = 0.0
        self.rolling_horizon_current_actual_dispatch_min = 0.0
        self._rolling_horizon_log_window_start(collection_trigger="initial_scan")
        self._rolling_horizon_collect_candidates(collection_trigger="initial_scan")

    def _rolling_horizon_reconcile_candidates_at_boundary(self) -> None:
        boundary_index = int(self.rolling_horizon_window_index)
        for entry in self.rolling_horizon_pending.values():
            entry["workers"] = set()
            entry["tasks_by_worker"] = {}
            entry["boundary_seen_index"] = -1
        # Rebuild after clearing the resource index so an obsolete pending row
        # cannot hide the currently feasible opportunity for the same resource.
        self.rolling_horizon_pending_resource_index = {}
        self._rolling_horizon_collect_candidates(collection_trigger="boundary_reconciliation")

        stale_ids: list[str] = []
        for opportunity_id, entry in self.rolling_horizon_pending.items():
            tasks_by_worker = entry.get("tasks_by_worker", {})
            seen_index = entry.get("boundary_seen_index", -1)
            if (
                int(-1 if seen_index is None else seen_index) != boundary_index
                or not isinstance(tasks_by_worker, dict)
                or not tasks_by_worker
            ):
                stale_ids.append(str(opportunity_id))
        for opportunity_id in stale_ids:
            entry = self.rolling_horizon_pending.pop(opportunity_id, None)
            if isinstance(entry, dict):
                self._rolling_horizon_log_pending_skip(
                    entry,
                    window_index=boundary_index,
                    resource_keys=list(entry.get("exclusive_resource_keys", [])),
                    reason="not_feasible_at_boundary",
                )
        self._rolling_horizon_rebuild_pending_resource_index()

    def _rolling_horizon_process_strict_boundary(self, scheduled_boundary_min: float) -> None:
        actual_dispatch_min = float(self.env.now)
        scheduled_boundary_min = float(scheduled_boundary_min)
        lag = max(0.0, actual_dispatch_min - scheduled_boundary_min)
        self.rolling_horizon_current_scheduled_boundary_min = scheduled_boundary_min
        self.rolling_horizon_current_actual_dispatch_min = actual_dispatch_min
        self.rolling_horizon_metrics["strict_boundary_count"] += 1
        self.rolling_horizon_max_boundary_lag_min = max(
            float(self.rolling_horizon_max_boundary_lag_min), lag
        )
        if lag > 1e-9:
            self.rolling_horizon_metrics["late_boundary_count"] += 1

        self._rolling_horizon_requeue_unstarted_dispatches(int(self.rolling_horizon_window_index))
        self._rolling_horizon_reconcile_candidates_at_boundary()
        self._rolling_horizon_dispatch_window()
        self._rolling_horizon_notify_queued_workers()

        self.rolling_horizon_window_index += 1
        self.rolling_horizon_window_start_min = scheduled_boundary_min
        self.rolling_horizon_window_end_min = scheduled_boundary_min + self.rolling_horizon_window_min
        self._rolling_horizon_log_window_start(collection_trigger="boundary_reconciliation")

    def _rolling_horizon_log_window_start(self, *, collection_trigger: str = "event_refresh") -> None:
        if self.rolling_horizon_logged_window_index == self.rolling_horizon_window_index:
            return
        self.rolling_horizon_logged_window_index = self.rolling_horizon_window_index
        self.rolling_horizon_metrics["started_window_count"] += 1
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="ROLLING_HORIZON_WINDOW_START",
            entity_id=f"RH-{self.rolling_horizon_window_index:05d}",
            location="CoordinationReview",
            details={
                "window_index": int(self.rolling_horizon_window_index),
                "window_start_min": round(float(self.rolling_horizon_window_start_min), 3),
                "window_end_min": round(float(self.rolling_horizon_window_end_min), 3),
                "window_min": round(float(self.rolling_horizon_window_min), 3),
                "dispatch_policy": self.rolling_horizon_dispatch_policy,
                "collection_trigger": str(collection_trigger),
                **self._rolling_horizon_boundary_metadata(),
            },
        )

    def _rolling_horizon_update(self) -> None:
        """Compatibility refresh; periodic dispatch is coordinator-owned."""
        if not self._rolling_horizon_active():
            return
        self._rolling_horizon_collect_candidates(collection_trigger="event_refresh")

    def _rolling_horizon_refresh_candidates_process(self):
        yield self.env.timeout(0)
        triggers = sorted(self.rolling_horizon_candidate_refresh_triggers)
        self.rolling_horizon_candidate_refresh_triggers.clear()
        self.rolling_horizon_candidate_refresh_scheduled = False
        if not self._rolling_horizon_active() or self.terminated:
            return
        self._rolling_horizon_collect_candidates(collection_trigger="event_refresh")
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="ROLLING_HORIZON_CANDIDATE_REFRESH",
            entity_id=f"RH-{self.rolling_horizon_window_index:05d}",
            location="CoordinationReview",
            details={
                "window_index": int(self.rolling_horizon_window_index),
                "collection_trigger": "event_refresh",
                "source_triggers": triggers,
                "scheduler_mode": self.rolling_horizon_scheduler_mode,
            },
        )

    def _rolling_horizon_request_candidate_refresh(self, trigger: str) -> None:
        if self._adp_active():
            self.adp_coordinator.request(trigger)
        if not self._rolling_horizon_active() or self.terminated:
            return
        self.rolling_horizon_candidate_refresh_triggers.add(str(trigger or "state_change"))
        if self.rolling_horizon_candidate_refresh_scheduled:
            return
        self.rolling_horizon_candidate_refresh_scheduled = True
        self.env.process(self._rolling_horizon_refresh_candidates_process())

    def _rolling_horizon_immediate_task_codes_for_event(self, event_name: str) -> set[str]:
        if not self._rolling_horizon_active():
            return set()
        if not bool(getattr(self, "rolling_horizon_immediate_triggers_enabled", False)):
            return set()
        return set(
            getattr(self, "rolling_horizon_immediate_event_task_codes", {}).get(
                str(event_name or "").strip().lower(),
                set(),
            )
        )

    def _rolling_horizon_collect_immediate_candidates(self, event_name: str) -> None:
        task_code_filter = self._rolling_horizon_immediate_task_codes_for_event(event_name)
        if not task_code_filter:
            return
        event_name = str(event_name or "").strip().lower() or "immediate"
        if event_name == "worker_low_battery" and self.battery_assignment_mode == "policy_decides":
            self._rolling_horizon_collect_candidates(
                task_code_filter=task_code_filter,
                collection_trigger="worker_low_battery_observed",
            )
            return
        if event_name == "worker_low_battery" and self.battery_delivery_enabled:
            task_code_filter = set(task_code_filter) | {"TRANSFER"}
        self._rolling_horizon_log_window_start()
        self._rolling_horizon_collect_candidates(
            task_code_filter=task_code_filter,
            collection_trigger=event_name,
        )
        if str(getattr(self, "rolling_horizon_immediate_queue_policy", "")).strip().lower() == "next_after_current":
            self._rolling_horizon_dispatch_immediate_candidates(
                str(event_name or "").strip().lower() or "immediate",
                task_code_filter,
            )

    def _rolling_horizon_collect_candidates(
        self,
        *,
        task_code_filter: set[str] | None = None,
        collection_trigger: str = "scheduled_scan",
    ) -> None:
        now = float(self.env.now)
        normalized_task_code_filter = {
            str(code or "").strip().upper()
            for code in (task_code_filter or set())
            if str(code or "").strip()
        }
        collection_trigger = str(collection_trigger or "scheduled_scan").strip().lower() or "scheduled_scan"
        immediate_trigger = (
            collection_trigger in self.rolling_horizon_immediate_event_task_codes
            and bool(normalized_task_code_filter)
        )
        queued_resource_index = self._rolling_horizon_queued_resource_index()
        queued_resource_index.update(self._rolling_horizon_active_resource_index())
        for agent_id in sorted(self.agents.keys()):
            agent = self.agents[agent_id]
            candidates = self._bind_humanoid_candidates_for_agent(
                agent,
                self._filter_candidates_for_agent(agent, self._candidate_tasks(agent)),
            )
            candidates = [task for task in candidates if self._task_item_dependencies_available(task, agent)]
            candidates = [
                task for task in candidates
                if self._rolling_horizon_task_allowed_for_worker(agent.agent_id, task)
            ]
            for task in candidates:
                priority_key = self._task_priority_key(task)
                task_code = self._rolling_horizon_task_code(task)
                if normalized_task_code_filter and task_code not in normalized_task_code_filter:
                    continue
                if (
                    priority_key in {"battery_swap", "battery_charge"}
                    and self._rolling_horizon_is_self_battery_swap(task, agent)
                    and (
                        str(agent.current_task_code or "").strip().upper() == "MANAGE_ROBOT_POWER"
                        or str(agent.current_task_type or "").strip().upper() in {"BATTERY_SWAP", "BATTERY_CHARGE"}
                    )
                ):
                    # The worker is already executing its own battery service.
                    # Do not create another rolling task instance with the same
                    # opportunity while the first one is still visible/running.
                    continue
                opportunity_id = self._rolling_horizon_opportunity_id(task)
                rolling_signature = self._rolling_horizon_task_signature(task)
                exclusive_resource_keys = self._rolling_horizon_exclusive_resource_keys(task)
                blocking_resource_keys = [
                    key
                    for key in exclusive_resource_keys
                    if not self._rolling_horizon_immediate_repair_ignores_resource(
                        collection_trigger=collection_trigger,
                        task_code=task_code,
                        target_id=self._task_target_id(task),
                        resource_key=key,
                    )
                ]
                allowed_worker_ids = self._rolling_horizon_allowed_worker_ids_for_task(task)
                role_owner_agent_id = self._rolling_horizon_role_owner_for_task(task)
                task_rule = self._mfg_flow_task_rule(task) if self._mfg_flow_policy_active() else None
                if any(key in queued_resource_index for key in blocking_resource_keys):
                    # A previous window already committed this concrete item,
                    # slot, or machine to a worker dispatch queue. Treat it as
                    # unavailable until that worker accepts or skips the task.
                    continue
                conflicting_opportunity_id = next(
                    (
                        self.rolling_horizon_pending_resource_index[key]
                        for key in blocking_resource_keys
                        if key in self.rolling_horizon_pending_resource_index
                        and self.rolling_horizon_pending_resource_index[key] != opportunity_id
                    ),
                    None,
                )
                if conflicting_opportunity_id:
                    # The same concrete item/slot cannot satisfy two different
                    # opportunities in one rolling window. Keep the first
                    # opportunity and let the next window re-evaluate reality.
                    continue
                entry = self.rolling_horizon_pending.get(opportunity_id)
                if entry is None:
                    base_rank = self._rolling_horizon_base_rank_for_worker_task(agent_id, task)
                    stable_task_id = self._next_task_id_for_task_code(task_code)
                    entry = {
                        "opportunity_id": opportunity_id,
                        "first_window_index": int(self.rolling_horizon_window_index),
                        "first_seen_min": now,
                        "last_seen_min": now,
                        "task_id": stable_task_id,
                        "task_code": task_code,
                        "priority_key": priority_key,
                        "task_type": str(task.task_type),
                        "location": str(task.location),
                        "base_priority_rank": base_rank,
                        "effective_priority_rank": base_rank,
                        "task_signature": self._task_signature(task),
                        "rolling_task_signature": rolling_signature,
                        "target_type": self._task_target_type(task),
                        "target_id": self._task_target_id(task),
                        "target_station": self._task_target_station(task),
                        "shareable": self._task_shareable(task),
                        "capacity": self._task_capacity(task),
                        "exclusive_resource_keys": list(exclusive_resource_keys),
                        "role_policy": (
                            "workload_balanced_dedicated"
                            if self._mfg_flow_dedicated_policy_active()
                            else "dedicated_roles"
                            if self._rolling_horizon_dedicated_roles_active()
                            else "shared_pool"
                        ),
                        "task_rule_id": str(task_rule.rule_id) if task_rule is not None else "",
                        "role_number": int(task_rule.role_number) if task_rule is not None else 0,
                        "role_task_code": str(task_rule.task_code) if task_rule is not None else "",
                        "role_display_name": str(task_rule.display_name) if task_rule is not None else "",
                        "role_owner_agent_id": role_owner_agent_id,
                        "allowed_worker_ids": list(allowed_worker_ids),
                        "repair_urgency_score": float(
                            task.payload.get("repair_urgency_score", 0.0) or 0.0
                        ),
                        "repair_urgency_tier": str(
                            task.payload.get("repair_urgency_tier", "") or ""
                        ),
                        "repair_urgency_components": dict(
                            task.payload.get("repair_urgency_components", {}) or {}
                        ),
                        "workers": set(),
                        "tasks_by_worker": {},
                        "last_logged_window_index": None,
                    }
                    self.rolling_horizon_pending[opportunity_id] = entry
                    for key in exclusive_resource_keys:
                        self.rolling_horizon_pending_resource_index.setdefault(key, opportunity_id)
                for key in exclusive_resource_keys:
                    self.rolling_horizon_pending_resource_index.setdefault(key, opportunity_id)
                if not str(entry.get("task_id", "") or "").strip():
                    entry["task_id"] = self._next_task_id_for_task_code(task_code)
                entry["last_seen_min"] = now
                entry["repair_urgency_score"] = float(
                    task.payload.get("repair_urgency_score", 0.0) or 0.0
                )
                entry["repair_urgency_tier"] = str(
                    task.payload.get("repair_urgency_tier", "") or ""
                )
                entry["repair_urgency_components"] = dict(
                    task.payload.get("repair_urgency_components", {}) or {}
                )
                entry["effective_priority_rank"] = self._rolling_horizon_effective_rank(entry)
                if collection_trigger == "boundary_reconciliation":
                    entry["boundary_seen_index"] = int(self.rolling_horizon_window_index)
                task_immediate_trigger = immediate_trigger and (
                    collection_trigger != "worker_low_battery"
                    or priority_key
                    in {
                        "battery_swap",
                        "battery_charge",
                        "battery_delivery_low_battery",
                        "battery_delivery_discharged",
                    }
                )
                if task_immediate_trigger:
                    entry["immediate_trigger"] = True
                    entry["collection_trigger"] = collection_trigger
                    entry["last_immediate_seen_min"] = now
                task_for_worker = copy.deepcopy(task)
                task_for_worker.task_id = str(entry.get("task_id", "") or task_for_worker.task_id)
                self._sync_task_instance_id(task_for_worker)
                entry["tasks_by_worker"][agent_id] = task_for_worker
                workers = entry["workers"]
                is_new_worker = agent_id not in workers
                if is_new_worker:
                    workers.add(agent_id)
                    self.rolling_horizon_metrics["candidate_collected_count"] += 1
                    if self._rolling_horizon_dedicated_roles_active():
                        self.rolling_horizon_dedicated_role_metrics["collected_by_worker"][agent_id] += 1
                should_log = is_new_worker or entry.get("last_logged_window_index") != self.rolling_horizon_window_index
                if not should_log:
                    continue
                entry["last_logged_window_index"] = self.rolling_horizon_window_index
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ROLLING_HORIZON_CANDIDATE_COLLECTED",
                    entity_id=opportunity_id,
                    location=str(task.location),
                    details={
                        "window_index": int(self.rolling_horizon_window_index),
                        "opportunity_id": opportunity_id,
                        "task_id": str(entry.get("task_id", "")),
                        "worker_id": agent_id,
                        "task_code": task_code,
                        "task_type": str(task.task_type),
                        "priority_key": priority_key,
                        "base_priority_rank": int(entry["base_priority_rank"]),
                        "effective_priority_rank": int(entry["effective_priority_rank"]),
                        "waited_window_count": self._rolling_horizon_waited_window_count(entry),
                        "task_signature": dict(entry["task_signature"]),
                        "rolling_task_signature": dict(entry["rolling_task_signature"]),
                        "collection_trigger": collection_trigger,
                        "immediate_trigger": task_immediate_trigger,
                        "role_policy": str(entry.get("role_policy", "")),
                        "task_rule_id": str(entry.get("task_rule_id", "")),
                        "role_number": int(entry.get("role_number", 0) or 0),
                        "role_task_code": str(entry.get("role_task_code", "")),
                        "role_display_name": str(entry.get("role_display_name", "")),
                        "role_owner_agent_id": str(entry.get("role_owner_agent_id", "")),
                        "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
                    },
                )

    def _rolling_horizon_insert_dispatch_queue_entry(
        self,
        worker_id: str,
        queue_entry: dict[str, Any],
        *,
        urgent: bool = False,
    ) -> None:
        queue = self.rolling_horizon_dispatch_queues[worker_id]
        if not urgent:
            queue.append(queue_entry)
            self._rolling_horizon_notify_worker(worker_id)
            return
        insert_at = 0
        for existing in queue:
            if not isinstance(existing, dict) or not bool(existing.get("urgent_dispatch", False)):
                break
            insert_at += 1
        queue.insert(insert_at, queue_entry)
        self._rolling_horizon_notify_worker(worker_id)

    def _rolling_horizon_dispatch_immediate_candidates(
        self,
        event_name: str,
        task_code_filter: set[str],
    ) -> None:
        event_name = str(event_name or "immediate").strip().lower() or "immediate"
        normalized_task_code_filter = {
            str(code or "").strip().upper()
            for code in (task_code_filter or set())
            if str(code or "").strip()
        }
        if not normalized_task_code_filter or not self.rolling_horizon_pending:
            return
        window_index = int(self.rolling_horizon_window_index)
        opportunities = sorted(
            [
                entry
                for entry in self.rolling_horizon_pending.values()
                if bool(entry.get("immediate_trigger", False))
                and str(entry.get("collection_trigger", "")).strip().lower() == event_name
                and str(entry.get("task_code", "")).strip().upper() in normalized_task_code_filter
                and (
                    event_name != "machine_broken_critical"
                    or str(entry.get("repair_urgency_tier", "")).strip().lower()
                    == "critical"
                )
            ],
            key=lambda entry: (
                self._rolling_horizon_effective_rank(entry),
                *self._rolling_horizon_repair_urgency_key(entry),
                float(entry.get("first_seen_min", 0.0) or 0.0),
                str(entry.get("task_code", "")),
                str(entry.get("opportunity_id", "")),
            ),
        )
        if not opportunities:
            return

        dispatched_opportunity_ids: set[str] = set()
        stale_opportunity_ids: set[str] = set()
        committed_resource_keys: set[str] = set(self._rolling_horizon_queued_resource_index().keys())
        committed_resource_keys.update(self._rolling_horizon_active_resource_index().keys())
        for entry in opportunities:
            opportunity_id = str(entry.get("opportunity_id", "")).strip()
            resource_keys = [str(key or "").strip() for key in entry.get("exclusive_resource_keys", []) if str(key or "").strip()]
            blocking_resource_keys = [
                key
                for key in resource_keys
                if not self._rolling_horizon_immediate_repair_ignores_resource(
                    collection_trigger=event_name,
                    task_code=str(entry.get("task_code", "")),
                    target_id=str(entry.get("target_id", "")),
                    resource_key=key,
                )
            ]
            if any(key in committed_resource_keys for key in blocking_resource_keys):
                # Another queued/running task already owns this urgent resource.
                continue
            tasks_by_worker = entry.get("tasks_by_worker", {})
            if not isinstance(tasks_by_worker, dict):
                continue
            rows: list[tuple[int, float, float, str]] = []
            saw_available_resource = False
            for worker_id, task in tasks_by_worker.items():
                worker_id = str(worker_id)
                agent = self.agents.get(worker_id)
                if agent is None or not isinstance(task, Task):
                    continue
                if self._rolling_horizon_self_battery_swap_due(agent) and not self._rolling_horizon_is_self_battery_swap(task, agent):
                    continue
                if not self._task_item_dependencies_available(task, agent):
                    continue
                saw_available_resource = True
                if not self._rolling_horizon_worker_available(agent):
                    continue
                if not self._rolling_horizon_task_allowed_for_worker(worker_id, task):
                    self.rolling_horizon_dedicated_role_metrics["role_violation_count"] += 1
                    continue
                if self._rolling_horizon_opportunity_id(task) != opportunity_id:
                    continue
                queue_length = len(self.rolling_horizon_dispatch_queues.get(worker_id, ()))
                rows.append(
                    (
                        int(queue_length),
                        float(self._task_estimated_duration(agent, task)),
                        float(self.travel_time(agent.location, task.location)),
                        worker_id,
                    )
                )
            if not saw_available_resource:
                stale_opportunity_ids.add(opportunity_id)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ROLLING_HORIZON_TASK_SKIPPED",
                    entity_id=opportunity_id,
                    location=str(entry.get("location", "CoordinationReview")),
                    details={
                        "window_index": window_index,
                        "opportunity_id": opportunity_id,
                        "task_id": str(entry.get("task_id", "")),
                        "task_code": str(entry.get("task_code", "")),
                        "priority_key": str(entry.get("priority_key", "")),
                        "task_type": str(entry.get("task_type", "")),
                        "base_priority_rank": int(entry.get("base_priority_rank", 9999) or 9999),
                        "effective_priority_rank": self._rolling_horizon_effective_rank(entry),
                        "waited_window_count": self._rolling_horizon_waited_window_count(entry),
                        "task_signature": dict(entry.get("task_signature", {})),
                        "rolling_task_signature": dict(entry.get("rolling_task_signature", {})),
                        "exclusive_resource_keys": list(resource_keys),
                        "role_policy": str(entry.get("role_policy", "")),
                        "task_rule_id": str(entry.get("task_rule_id", "")),
                        "role_number": int(entry.get("role_number", 0) or 0),
                        "role_task_code": str(entry.get("role_task_code", "")),
                        "role_display_name": str(entry.get("role_display_name", "")),
                        "role_owner_agent_id": str(entry.get("role_owner_agent_id", "")),
                        "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
                        "collection_trigger": event_name,
                        "immediate_trigger": True,
                        "reason": "stale_or_unavailable_resource",
                    },
                )
                self.rolling_horizon_metrics["stale_skipped_task_count"] += 1
                continue
            if not rows:
                continue
            rows.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
            capacity = max(1, int(entry.get("capacity", 1) or 1))
            if not bool(entry.get("shareable", False)):
                capacity = 1
            for _queue_length, _duration, _travel, worker_id in rows[:capacity]:
                effective_rank = self._rolling_horizon_effective_rank(entry)
                queue_entry = {
                    "window_index": window_index,
                    "first_window_index": int(
                        window_index if entry.get("first_window_index") is None else entry.get("first_window_index")
                    ),
                    "first_seen_min": float(
                        self.env.now if entry.get("first_seen_min") is None else entry.get("first_seen_min")
                    ),
                    "opportunity_id": str(entry.get("opportunity_id", "")),
                    "task_id": str(entry.get("task_id", "")),
                    "task_code": str(entry.get("task_code", "")),
                    "priority_key": str(entry.get("priority_key", "")),
                    "task_type": str(entry.get("task_type", "")),
                    "location": str(entry.get("location", "CoordinationReview")),
                    "base_priority_rank": int(entry.get("base_priority_rank", 9999) or 9999),
                    "effective_priority_rank": effective_rank,
                    "waited_window_count": self._rolling_horizon_waited_window_count(entry),
                    "task_signature": dict(entry.get("task_signature", {})),
                    "rolling_task_signature": dict(entry.get("rolling_task_signature", {})),
                    "target_type": str(entry.get("target_type", "")),
                    "target_id": str(entry.get("target_id", "")),
                    "target_station": entry.get("target_station"),
                    "shareable": bool(entry.get("shareable", False)),
                    "capacity": int(entry.get("capacity", 1) or 1),
                    "exclusive_resource_keys": list(entry.get("exclusive_resource_keys", [])),
                    "role_policy": str(entry.get("role_policy", "")),
                    "task_rule_id": str(entry.get("task_rule_id", "")),
                    "role_number": int(entry.get("role_number", 0) or 0),
                    "role_task_code": str(entry.get("role_task_code", "")),
                    "role_display_name": str(entry.get("role_display_name", "")),
                    "role_owner_agent_id": str(entry.get("role_owner_agent_id", "")),
                    "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
                    "repair_urgency_score": float(
                        entry.get("repair_urgency_score", 0.0) or 0.0
                    ),
                    "repair_urgency_tier": str(entry.get("repair_urgency_tier", "") or ""),
                    "repair_urgency_components": dict(
                        entry.get("repair_urgency_components", {}) or {}
                    ),
                    "assigned_worker_id": worker_id,
                    "queue_length_before": int(_queue_length),
                    "assigned_at_min": round(float(self.env.now), 3),
                    "urgent_dispatch": True,
                    "collection_trigger": event_name,
                    "queue_policy": str(getattr(self, "rolling_horizon_immediate_queue_policy", "")),
                    "scheduler_mode": self.rolling_horizon_scheduler_mode,
                    "candidate_collection_mode": self.rolling_horizon_candidate_collection_mode,
                    "scheduled_boundary_min": None,
                    "actual_dispatch_min": round(float(self.env.now), 6),
                    "boundary_lag_min": 0.0,
                    "dispatch_kind": "immediate_exception",
                }
                self._rolling_horizon_insert_dispatch_queue_entry(worker_id, queue_entry, urgent=True)
                dispatched_opportunity_ids.add(str(entry.get("opportunity_id", "")))
                committed_resource_keys.update(resource_keys)
                self.rolling_horizon_metrics["dispatched_task_count"] += 1
                self.rolling_horizon_metrics["immediate_dispatched_task_count"] += 1
                self._rolling_horizon_refresh_queue_metrics()
                if self._rolling_horizon_dedicated_roles_active():
                    self.rolling_horizon_dedicated_role_metrics["dispatched_by_worker"][worker_id] += 1
                    if str(entry.get("task_code", "")) == "HANDOVER_ITEM":
                        self.rolling_horizon_dedicated_role_metrics["handover_dispatch_count"] += 1
                    priority_key = str(entry.get("priority_key", ""))
                    if priority_key in {"battery_delivery_low_battery", "battery_delivery_discharged"} and worker_id in set(getattr(self, "rolling_horizon_battery_delivery_provider_agent_ids", [])):
                        self.rolling_horizon_dedicated_role_metrics["battery_delivery_from_provider_count"] += 1
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ROLLING_HORIZON_DISPATCH",
                    entity_id=str(entry.get("opportunity_id", "")),
                    location=str(entry.get("location", "CoordinationReview")),
                    details={
                        **queue_entry,
                        "assigned_worker_id": worker_id,
                        "dispatch_policy": self.rolling_horizon_dispatch_policy,
                        "candidate_worker_count": len(rows),
                        "immediate_trigger": True,
                    },
                )

        for opportunity_id in dispatched_opportunity_ids | stale_opportunity_ids:
            self.rolling_horizon_pending.pop(opportunity_id, None)
        if dispatched_opportunity_ids or stale_opportunity_ids:
            self._rolling_horizon_rebuild_pending_resource_index()

    def _rolling_horizon_log_pending_skip(
        self,
        entry: dict[str, Any],
        *,
        window_index: int,
        resource_keys: list[str],
        reason: str,
    ) -> None:
        opportunity_id = str(entry.get("opportunity_id", "")).strip()
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="ROLLING_HORIZON_TASK_SKIPPED",
            entity_id=opportunity_id,
            location=str(entry.get("location", "CoordinationReview")),
            details={
                "window_index": window_index,
                "opportunity_id": opportunity_id,
                "task_id": str(entry.get("task_id", "")),
                "task_code": str(entry.get("task_code", "")),
                "priority_key": str(entry.get("priority_key", "")),
                "task_type": str(entry.get("task_type", "")),
                "base_priority_rank": int(entry.get("base_priority_rank", 9999) or 9999),
                "effective_priority_rank": self._rolling_horizon_effective_rank(entry),
                "waited_window_count": self._rolling_horizon_waited_window_count(entry),
                "task_signature": dict(entry.get("task_signature", {})),
                "rolling_task_signature": dict(entry.get("rolling_task_signature", {})),
                "exclusive_resource_keys": list(resource_keys),
                "role_policy": str(entry.get("role_policy", "")),
                "task_rule_id": str(entry.get("task_rule_id", "")),
                "role_number": int(entry.get("role_number", 0) or 0),
                "role_task_code": str(entry.get("role_task_code", "")),
                "role_display_name": str(entry.get("role_display_name", "")),
                "role_owner_agent_id": str(entry.get("role_owner_agent_id", "")),
                "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
                "reason": reason,
            },
        )
        self.rolling_horizon_metrics["stale_skipped_task_count"] += 1
        if self._rolling_horizon_dedicated_roles_active():
            for worker_id in entry.get("workers", set()):
                self.rolling_horizon_dedicated_role_metrics["skipped_by_worker"][str(worker_id)] += 1

    def _rolling_horizon_optimizer_accept_statuses(self, cp_model: Any) -> set[int]:
        configured = self.rolling_horizon_optimizer_cfg.get("accept_statuses", ["OPTIMAL", "FEASIBLE"])
        if isinstance(configured, str):
            configured = [configured]
        if not isinstance(configured, list):
            configured = ["OPTIMAL", "FEASIBLE"]
        out: set[int] = set()
        for value in configured:
            name = str(value or "").strip().upper()
            if hasattr(cp_model, name):
                out.add(int(getattr(cp_model, name)))
        return out or {int(getattr(cp_model, "OPTIMAL")), int(getattr(cp_model, "FEASIBLE"))}

    def _rolling_horizon_dispatch_window_with_optimizer(self, window_index: int) -> None:
        cp_model = self.rolling_horizon_optimizer_cp_model or require_cp_sat()
        self.rolling_horizon_metrics["throughput_optimizer_window_count"] += 1
        optimizer_cfg = self.rolling_horizon_optimizer_cfg
        max_tasks_per_worker = max(1, int(optimizer_cfg.get("max_tasks_per_worker_per_window", 8) or 8))
        time_limit_s = max(0.01, float(optimizer_cfg.get("time_limit_s", 5.0) or 5.0))
        num_search_workers = max(1, int(optimizer_cfg.get("num_search_workers", 1) or 1))
        configured_solver_seed = optimizer_cfg.get("random_seed")
        solver_seed = int(self.seed if configured_solver_seed is None else configured_solver_seed)
        objective_scale = max(1, int(optimizer_cfg.get("objective_scale", 100) or 100))
        assignment_reward = float(optimizer_cfg.get("assignment_reward", 1000.0) or 1000.0)
        urgent_bonus = float(optimizer_cfg.get("urgent_bonus", 250.0) or 250.0)
        queue_load_penalty = float(optimizer_cfg.get("queue_load_penalty", 12.0) or 12.0)
        load_imbalance_penalty = float(optimizer_cfg.get("load_imbalance_penalty", 10.0) or 10.0)

        opportunities = sorted(
            self.rolling_horizon_pending.values(),
            key=lambda entry: (
                self._rolling_horizon_effective_rank(entry),
                *self._rolling_horizon_repair_urgency_key(entry),
                float(entry.get("first_seen_min", 0.0) or 0.0),
                str(entry.get("task_code", "")),
                str(entry.get("opportunity_id", "")),
            ),
        )
        stale_opportunity_ids: set[str] = set()
        committed_resource_keys: set[str] = set(self._rolling_horizon_queued_resource_index().keys())
        committed_resource_keys.update(self._rolling_horizon_active_resource_index().keys())
        assignment_rows: list[dict[str, Any]] = []
        rows_by_opportunity: dict[str, list[int]] = defaultdict(list)
        rows_by_worker: dict[str, list[int]] = defaultdict(list)
        rows_by_resource_key: dict[str, list[int]] = defaultdict(list)

        for entry in opportunities:
            opportunity_id = str(entry.get("opportunity_id", "")).strip()
            resource_keys = [str(key or "").strip() for key in entry.get("exclusive_resource_keys", []) if str(key or "").strip()]
            blocking_resource_keys = [
                key
                for key in resource_keys
                if not self._rolling_horizon_immediate_repair_ignores_resource(
                    collection_trigger="boundary_reconciliation",
                    task_code=str(entry.get("task_code", "")),
                    target_id=str(entry.get("target_id", "")),
                    resource_key=key,
                )
            ]
            if any(key in committed_resource_keys for key in blocking_resource_keys):
                stale_opportunity_ids.add(opportunity_id)
                self._rolling_horizon_log_pending_skip(
                    entry,
                    window_index=window_index,
                    resource_keys=resource_keys,
                    reason="resource_already_committed",
                )
                continue
            tasks_by_worker = entry.get("tasks_by_worker", {})
            if not isinstance(tasks_by_worker, dict):
                continue
            saw_available_resource = False
            for worker_id, task in tasks_by_worker.items():
                worker_id = str(worker_id)
                agent = self.agents.get(worker_id)
                if agent is None or not isinstance(task, Task):
                    continue
                if self._rolling_horizon_self_battery_swap_due(agent) and not self._rolling_horizon_is_self_battery_swap(task, agent):
                    continue
                if not self._task_item_dependencies_available(task, agent):
                    continue
                saw_available_resource = True
                if not self._rolling_horizon_worker_available(agent):
                    continue
                if not self._rolling_horizon_task_allowed_for_worker(worker_id, task):
                    self.rolling_horizon_dedicated_role_metrics["role_violation_count"] += 1
                    continue
                if self._rolling_horizon_opportunity_id(task) != opportunity_id:
                    continue
                score_payload = self._throughput_score_payload(agent, task)
                row_index = len(assignment_rows)
                queue_length = len(self.rolling_horizon_dispatch_queues.get(worker_id, ()))
                assignment_rows.append(
                    {
                        "entry": entry,
                        "opportunity_id": opportunity_id,
                        "worker_id": worker_id,
                        "task": task,
                        "resource_keys": resource_keys,
                        "queue_length": int(queue_length),
                        "score": float(score_payload.get("total", 0.0) or 0.0),
                        "score_components": dict(score_payload.get("components", {})) if isinstance(score_payload.get("components", {}), dict) else {},
                        "effective_rank": self._rolling_horizon_effective_rank(entry),
                    }
                )
                rows_by_opportunity[opportunity_id].append(row_index)
                rows_by_worker[worker_id].append(row_index)
                for key in resource_keys:
                    rows_by_resource_key[key].append(row_index)
            if not saw_available_resource:
                stale_opportunity_ids.add(opportunity_id)
                self._rolling_horizon_log_pending_skip(
                    entry,
                    window_index=window_index,
                    resource_keys=resource_keys,
                    reason="stale_or_unavailable_resource",
                )

        if not assignment_rows:
            for opportunity_id in stale_opportunity_ids:
                self.rolling_horizon_pending.pop(opportunity_id, None)
            if stale_opportunity_ids:
                self._rolling_horizon_rebuild_pending_resource_index()
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="ROLLING_HORIZON_DISPATCH",
                entity_id=f"RH-{window_index:05d}",
                location="CoordinationReview",
                details={
                    "window_index": window_index,
                    "dispatch_policy": self.rolling_horizon_dispatch_policy,
                    "candidate_count": len(self.rolling_horizon_pending),
                    "dispatch_count": 0,
                    "optimizer_status": "NOT_SOLVED",
                    "reason": "no_feasible_optimizer_rows",
                    **self._rolling_horizon_boundary_metadata(),
                },
            )
            return

        model = cp_model.CpModel()
        variables = [model.NewBoolVar(f"x_{idx}") for idx in range(len(assignment_rows))]
        for opportunity_id, row_indexes in rows_by_opportunity.items():
            entry = self.rolling_horizon_pending.get(opportunity_id, {})
            capacity = max(1, int(entry.get("capacity", 1) or 1)) if isinstance(entry, dict) else 1
            shareable = isinstance(entry, dict) and bool(entry.get("shareable", False))
            if not shareable:
                capacity = 1
            model.Add(sum(variables[idx] for idx in row_indexes) <= capacity)
        for resource_key, row_indexes in rows_by_resource_key.items():
            repair_rows = [
                idx
                for idx in row_indexes
                if str(assignment_rows[idx]["entry"].get("task_code", "")).strip().upper() == "REPAIR_MACHINE"
                and resource_key
                == f"machine:{str(assignment_rows[idx]['entry'].get('target_id', '')).strip()}"
            ]
            if len(repair_rows) == len(row_indexes) and repair_rows:
                capacity = max(
                    max(1, int(assignment_rows[idx]["entry"].get("capacity", 1) or 1))
                    for idx in repair_rows
                )
                model.Add(sum(variables[idx] for idx in row_indexes) <= capacity)
            else:
                model.Add(sum(variables[idx] for idx in row_indexes) <= 1)

        worker_counts: dict[str, Any] = {}
        for worker_id in sorted(self.agents.keys()):
            row_indexes = rows_by_worker.get(worker_id, [])
            count_var = model.NewIntVar(0, max_tasks_per_worker, f"count_{worker_id}")
            model.Add(count_var == sum(variables[idx] for idx in row_indexes))
            model.Add(count_var <= max_tasks_per_worker)
            worker_counts[worker_id] = count_var
        max_load = model.NewIntVar(0, max_tasks_per_worker, "max_load")
        min_load = model.NewIntVar(0, max_tasks_per_worker, "min_load")
        if worker_counts:
            model.AddMaxEquality(max_load, list(worker_counts.values()))
            model.AddMinEquality(min_load, list(worker_counts.values()))
        else:
            model.Add(max_load == 0)
            model.Add(min_load == 0)

        objective_terms: list[Any] = []
        for idx, row in enumerate(assignment_rows):
            entry = row["entry"]
            urgent = bool(entry.get("immediate_trigger", False) or entry.get("urgent_dispatch", False))
            coefficient = (
                assignment_reward
                + float(row["score"])
                + (urgent_bonus if urgent else 0.0)
                - queue_load_penalty * float(row["queue_length"])
            )
            objective_terms.append(int(round(coefficient * objective_scale)) * variables[idx])
        objective_terms.append(-int(round(load_imbalance_penalty * objective_scale)) * (max_load - min_load))
        model.Maximize(sum(objective_terms))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = time_limit_s
        solver.parameters.num_search_workers = num_search_workers
        solver.parameters.random_seed = solver_seed
        status = solver.Solve(model)
        status_name = cp_sat_status_name(cp_model, status)
        accepted = self._rolling_horizon_optimizer_accept_statuses(cp_model)
        if int(status) not in accepted:
            self.rolling_horizon_metrics["throughput_optimizer_failed_count"] += 1
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="ROLLING_HORIZON_OPTIMIZER_FAILED",
                entity_id=f"RH-{window_index:05d}",
                location="CoordinationReview",
                details={
                    "window_index": window_index,
                    "dispatch_policy": self.rolling_horizon_dispatch_policy,
                    "optimizer_status": status_name,
                    "candidate_count": len(self.rolling_horizon_pending),
                    "assignment_row_count": len(assignment_rows),
                    "time_limit_s": time_limit_s,
                    "num_search_workers": num_search_workers,
                    "random_seed": solver_seed,
                    **self._rolling_horizon_boundary_metadata(),
                },
            )
            raise ThroughputOptimizerFailed(
                f"rolling_horizon_throughput_optimizer failed at window {window_index}: {status_name}"
            )

        self.rolling_horizon_metrics["throughput_optimizer_solved_count"] += 1
        objective_value = float(solver.ObjectiveValue()) / float(objective_scale)
        self.throughput_optimizer_objective_values.append(objective_value)
        selected_rows = [
            {**row, "row_index": idx}
            for idx, row in enumerate(assignment_rows)
            if int(solver.Value(variables[idx])) == 1
        ]
        selected_rows.sort(
            key=lambda row: (
                str(row["worker_id"]),
                -float(row["score"]),
                int(row["effective_rank"]),
                float(row["entry"].get("first_seen_min", 0.0) or 0.0),
                str(row["entry"].get("task_id", "")),
            )
        )
        sequence_by_worker: dict[str, int] = defaultdict(int)
        dispatch_count = 0
        dispatched_opportunity_ids: set[str] = set()
        for row in selected_rows:
            entry = row["entry"]
            worker_id = str(row["worker_id"])
            resource_keys = list(row.get("resource_keys", []))
            blocking_resource_keys = [
                key
                for key in resource_keys
                if not self._rolling_horizon_immediate_repair_ignores_resource(
                    collection_trigger="boundary_reconciliation",
                    task_code=str(entry.get("task_code", "")),
                    target_id=str(entry.get("target_id", "")),
                    resource_key=key,
                )
            ]
            if any(key in committed_resource_keys for key in blocking_resource_keys):
                continue
            sequence_by_worker[worker_id] += 1
            effective_rank = self._rolling_horizon_effective_rank(entry)
            queue_entry = {
                "window_index": window_index,
                "first_window_index": int(
                    window_index if entry.get("first_window_index") is None else entry.get("first_window_index")
                ),
                "first_seen_min": float(
                    self.env.now if entry.get("first_seen_min") is None else entry.get("first_seen_min")
                ),
                "opportunity_id": str(entry.get("opportunity_id", "")),
                "task_id": str(entry.get("task_id", "")),
                "task_code": str(entry.get("task_code", "")),
                "priority_key": str(entry.get("priority_key", "")),
                "task_type": str(entry.get("task_type", "")),
                "location": str(entry.get("location", "CoordinationReview")),
                "base_priority_rank": int(entry.get("base_priority_rank", 9999) or 9999),
                "effective_priority_rank": effective_rank,
                "waited_window_count": self._rolling_horizon_waited_window_count(entry),
                "task_signature": dict(entry.get("task_signature", {})),
                "rolling_task_signature": dict(entry.get("rolling_task_signature", {})),
                "target_type": str(entry.get("target_type", "")),
                "target_id": str(entry.get("target_id", "")),
                "target_station": entry.get("target_station"),
                "shareable": bool(entry.get("shareable", False)),
                "capacity": int(entry.get("capacity", 1) or 1),
                "exclusive_resource_keys": list(entry.get("exclusive_resource_keys", [])),
                "role_policy": str(entry.get("role_policy", "")),
                "task_rule_id": str(entry.get("task_rule_id", "")),
                "role_number": int(entry.get("role_number", 0) or 0),
                "role_task_code": str(entry.get("role_task_code", "")),
                "role_display_name": str(entry.get("role_display_name", "")),
                "role_owner_agent_id": str(entry.get("role_owner_agent_id", "")),
                "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
                "repair_urgency_score": float(
                    entry.get("repair_urgency_score", 0.0) or 0.0
                ),
                "repair_urgency_tier": str(entry.get("repair_urgency_tier", "") or ""),
                "repair_urgency_components": dict(
                    entry.get("repair_urgency_components", {}) or {}
                ),
                "assigned_worker_id": worker_id,
                "queue_length_before": int(row.get("queue_length", 0) or 0),
                "assigned_at_min": round(float(self.env.now), 3),
                "optimizer_status": status_name,
                "optimizer_objective": round(objective_value, 3),
                "optimizer_score": round(float(row.get("score", 0.0) or 0.0), 3),
                "score_components": dict(row.get("score_components", {})),
                "sequence_position": int(row.get("queue_length", 0) or 0) + sequence_by_worker[worker_id],
                **self._rolling_horizon_boundary_metadata(),
            }
            self.rolling_horizon_dispatch_queues[worker_id].append(queue_entry)
            dispatched_opportunity_ids.add(str(entry.get("opportunity_id", "")))
            committed_resource_keys.update(resource_keys)
            dispatch_count += 1
            self.rolling_horizon_metrics["dispatched_task_count"] += 1
            self.throughput_score_values.append(float(row.get("score", 0.0) or 0.0))
            self._rolling_horizon_refresh_queue_metrics()
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="ROLLING_HORIZON_DISPATCH",
                entity_id=str(entry.get("opportunity_id", "")),
                location=str(entry.get("location", "CoordinationReview")),
                details={
                    **queue_entry,
                    "assigned_worker_id": worker_id,
                    "dispatch_policy": self.rolling_horizon_dispatch_policy,
                    "candidate_worker_count": len(rows_by_opportunity.get(str(entry.get("opportunity_id", "")), [])),
                },
            )

        for opportunity_id in dispatched_opportunity_ids | stale_opportunity_ids:
            self.rolling_horizon_pending.pop(opportunity_id, None)
        if dispatched_opportunity_ids or stale_opportunity_ids:
            self._rolling_horizon_rebuild_pending_resource_index()

        if dispatch_count == 0:
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="ROLLING_HORIZON_DISPATCH",
                entity_id=f"RH-{window_index:05d}",
                location="CoordinationReview",
                details={
                    "window_index": window_index,
                    "dispatch_policy": self.rolling_horizon_dispatch_policy,
                    "candidate_count": len(self.rolling_horizon_pending),
                    "dispatch_count": 0,
                    "optimizer_status": status_name,
                    "optimizer_objective": round(objective_value, 3),
                    "reason": "optimizer_selected_no_assignments",
                    **self._rolling_horizon_boundary_metadata(),
                },
            )

    def _rolling_horizon_dispatch_window(self) -> None:
        window_index = int(self.rolling_horizon_window_index)
        self.rolling_horizon_metrics["window_count"] += 1
        if not self.rolling_horizon_pending:
            self.rolling_horizon_metrics["empty_window_count"] += 1
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="ROLLING_HORIZON_DISPATCH",
                entity_id=f"RH-{window_index:05d}",
                location="CoordinationReview",
                details={
                    "window_index": window_index,
                    "dispatch_policy": self.rolling_horizon_dispatch_policy,
                    "candidate_count": 0,
                    "dispatch_count": 0,
                    **self._rolling_horizon_boundary_metadata(),
                },
            )
            return

        if self._rolling_horizon_throughput_optimizer_active():
            self._rolling_horizon_dispatch_window_with_optimizer(window_index)
            return

        opportunities = sorted(
            self.rolling_horizon_pending.values(),
            key=lambda entry: (
                self._rolling_horizon_effective_rank(entry),
                *self._rolling_horizon_repair_urgency_key(entry),
                float(entry.get("first_seen_min", 0.0) or 0.0),
                str(entry.get("task_code", "")),
                str(entry.get("opportunity_id", "")),
            ),
        )
        dispatch_count = 0
        dispatched_opportunity_ids: set[str] = set()
        stale_opportunity_ids: set[str] = set()
        committed_resource_keys: set[str] = set(self._rolling_horizon_queued_resource_index().keys())
        committed_resource_keys.update(self._rolling_horizon_active_resource_index().keys())
        for entry in opportunities:
            opportunity_id = str(entry.get("opportunity_id", "")).strip()
            resource_keys = [str(key or "").strip() for key in entry.get("exclusive_resource_keys", []) if str(key or "").strip()]
            blocking_resource_keys = [
                key
                for key in resource_keys
                if not self._rolling_horizon_immediate_repair_ignores_resource(
                    collection_trigger="boundary_reconciliation",
                    task_code=str(entry.get("task_code", "")),
                    target_id=str(entry.get("target_id", "")),
                    resource_key=key,
                )
            ]
            if any(key in committed_resource_keys for key in blocking_resource_keys):
                stale_opportunity_ids.add(opportunity_id)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ROLLING_HORIZON_TASK_SKIPPED",
                    entity_id=opportunity_id,
                    location=str(entry.get("location", "CoordinationReview")),
                    details={
                        "window_index": window_index,
                        "opportunity_id": opportunity_id,
                        "task_id": str(entry.get("task_id", "")),
                        "task_code": str(entry.get("task_code", "")),
                        "priority_key": str(entry.get("priority_key", "")),
                        "task_type": str(entry.get("task_type", "")),
                        "base_priority_rank": int(entry.get("base_priority_rank", 9999) or 9999),
                        "effective_priority_rank": self._rolling_horizon_effective_rank(entry),
                        "waited_window_count": self._rolling_horizon_waited_window_count(entry),
                        "task_signature": dict(entry.get("task_signature", {})),
                        "rolling_task_signature": dict(entry.get("rolling_task_signature", {})),
                        "exclusive_resource_keys": list(resource_keys),
                        "role_policy": str(entry.get("role_policy", "")),
                        "task_rule_id": str(entry.get("task_rule_id", "")),
                        "role_number": int(entry.get("role_number", 0) or 0),
                        "role_task_code": str(entry.get("role_task_code", "")),
                        "role_display_name": str(entry.get("role_display_name", "")),
                        "role_owner_agent_id": str(entry.get("role_owner_agent_id", "")),
                        "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
                        "reason": "resource_already_committed",
                    },
                )
                self.rolling_horizon_metrics["stale_skipped_task_count"] += 1
                if self._rolling_horizon_dedicated_roles_active():
                    for worker_id in entry.get("workers", set()):
                        self.rolling_horizon_dedicated_role_metrics["skipped_by_worker"][str(worker_id)] += 1
                continue
            tasks_by_worker = entry.get("tasks_by_worker", {})
            if not isinstance(tasks_by_worker, dict):
                continue
            rows: list[tuple[int, float, float, str]] = []
            saw_available_resource = False
            for worker_id, task in tasks_by_worker.items():
                worker_id = str(worker_id)
                agent = self.agents.get(worker_id)
                if agent is None or not isinstance(task, Task):
                    continue
                if self._rolling_horizon_self_battery_swap_due(agent) and not self._rolling_horizon_is_self_battery_swap(task, agent):
                    continue
                if not self._task_item_dependencies_available(task, agent):
                    continue
                saw_available_resource = True
                if not self._rolling_horizon_worker_available(agent):
                    continue
                if not self._rolling_horizon_task_allowed_for_worker(worker_id, task):
                    self.rolling_horizon_dedicated_role_metrics["role_violation_count"] += 1
                    continue
                if self._rolling_horizon_opportunity_id(task) != opportunity_id:
                    continue
                queue_length = len(self.rolling_horizon_dispatch_queues.get(worker_id, ()))
                rows.append(
                    (
                        int(queue_length),
                        float(self._task_estimated_duration(agent, task)),
                        float(self.travel_time(agent.location, task.location)),
                        worker_id,
                    )
                )
            if not saw_available_resource:
                stale_opportunity_ids.add(opportunity_id)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ROLLING_HORIZON_TASK_SKIPPED",
                    entity_id=opportunity_id,
                    location=str(entry.get("location", "CoordinationReview")),
                    details={
                        "window_index": window_index,
                        "opportunity_id": opportunity_id,
                        "task_id": str(entry.get("task_id", "")),
                        "task_code": str(entry.get("task_code", "")),
                        "priority_key": str(entry.get("priority_key", "")),
                        "task_type": str(entry.get("task_type", "")),
                        "base_priority_rank": int(entry.get("base_priority_rank", 9999) or 9999),
                        "effective_priority_rank": self._rolling_horizon_effective_rank(entry),
                        "waited_window_count": self._rolling_horizon_waited_window_count(entry),
                        "task_signature": dict(entry.get("task_signature", {})),
                        "rolling_task_signature": dict(entry.get("rolling_task_signature", {})),
                        "exclusive_resource_keys": list(resource_keys),
                        "role_policy": str(entry.get("role_policy", "")),
                        "task_rule_id": str(entry.get("task_rule_id", "")),
                        "role_number": int(entry.get("role_number", 0) or 0),
                        "role_task_code": str(entry.get("role_task_code", "")),
                        "role_display_name": str(entry.get("role_display_name", "")),
                        "role_owner_agent_id": str(entry.get("role_owner_agent_id", "")),
                        "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
                        "reason": "stale_or_unavailable_resource",
                    },
                )
                self.rolling_horizon_metrics["stale_skipped_task_count"] += 1
                if self._rolling_horizon_dedicated_roles_active():
                    for worker_id in entry.get("workers", set()):
                        self.rolling_horizon_dedicated_role_metrics["skipped_by_worker"][str(worker_id)] += 1
                continue
            if not rows:
                continue
            rows.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
            capacity = max(1, int(entry.get("capacity", 1) or 1))
            if not bool(entry.get("shareable", False)):
                capacity = 1
            for _queue_length, _duration, _travel, worker_id in rows[:capacity]:
                effective_rank = self._rolling_horizon_effective_rank(entry)
                queue_entry = {
                    "window_index": window_index,
                    "first_window_index": int(
                        window_index if entry.get("first_window_index") is None else entry.get("first_window_index")
                    ),
                    "first_seen_min": float(
                        self.env.now if entry.get("first_seen_min") is None else entry.get("first_seen_min")
                    ),
                    "opportunity_id": str(entry.get("opportunity_id", "")),
                    "task_id": str(entry.get("task_id", "")),
                    "task_code": str(entry.get("task_code", "")),
                    "priority_key": str(entry.get("priority_key", "")),
                    "task_type": str(entry.get("task_type", "")),
                    "location": str(entry.get("location", "CoordinationReview")),
                    "base_priority_rank": int(entry.get("base_priority_rank", 9999) or 9999),
                    "effective_priority_rank": effective_rank,
                    "waited_window_count": self._rolling_horizon_waited_window_count(entry),
                    "task_signature": dict(entry.get("task_signature", {})),
                    "rolling_task_signature": dict(entry.get("rolling_task_signature", {})),
                    "target_type": str(entry.get("target_type", "")),
                    "target_id": str(entry.get("target_id", "")),
                    "target_station": entry.get("target_station"),
                    "shareable": bool(entry.get("shareable", False)),
                    "capacity": int(entry.get("capacity", 1) or 1),
                    "exclusive_resource_keys": list(entry.get("exclusive_resource_keys", [])),
                    "role_policy": str(entry.get("role_policy", "")),
                    "task_rule_id": str(entry.get("task_rule_id", "")),
                    "role_number": int(entry.get("role_number", 0) or 0),
                    "role_task_code": str(entry.get("role_task_code", "")),
                    "role_display_name": str(entry.get("role_display_name", "")),
                    "role_owner_agent_id": str(entry.get("role_owner_agent_id", "")),
                    "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
                    "repair_urgency_score": float(
                        entry.get("repair_urgency_score", 0.0) or 0.0
                    ),
                    "repair_urgency_tier": str(entry.get("repair_urgency_tier", "") or ""),
                    "repair_urgency_components": dict(
                        entry.get("repair_urgency_components", {}) or {}
                    ),
                    "assigned_worker_id": worker_id,
                    "queue_length_before": int(_queue_length),
                    "assigned_at_min": round(float(self.env.now), 3),
                    **self._rolling_horizon_boundary_metadata(),
                }
                self.rolling_horizon_dispatch_queues[worker_id].append(queue_entry)
                dispatched_opportunity_ids.add(str(entry.get("opportunity_id", "")))
                committed_resource_keys.update(resource_keys)
                dispatch_count += 1
                self.rolling_horizon_metrics["dispatched_task_count"] += 1
                self._rolling_horizon_refresh_queue_metrics()
                if self._rolling_horizon_dedicated_roles_active():
                    self.rolling_horizon_dedicated_role_metrics["dispatched_by_worker"][worker_id] += 1
                    if str(entry.get("task_code", "")) == "HANDOVER_ITEM":
                        self.rolling_horizon_dedicated_role_metrics["handover_dispatch_count"] += 1
                    priority_key = str(entry.get("priority_key", ""))
                    if priority_key in {"battery_delivery_low_battery", "battery_delivery_discharged"} and worker_id in set(getattr(self, "rolling_horizon_battery_delivery_provider_agent_ids", [])):
                        self.rolling_horizon_dedicated_role_metrics["battery_delivery_from_provider_count"] += 1
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ROLLING_HORIZON_DISPATCH",
                    entity_id=str(entry.get("opportunity_id", "")),
                    location=str(entry.get("location", "CoordinationReview")),
                    details={
                        **queue_entry,
                        "assigned_worker_id": worker_id,
                        "dispatch_policy": self.rolling_horizon_dispatch_policy,
                        "candidate_worker_count": len(rows),
                    },
                )

        for opportunity_id in dispatched_opportunity_ids | stale_opportunity_ids:
            self.rolling_horizon_pending.pop(opportunity_id, None)
        if dispatched_opportunity_ids or stale_opportunity_ids:
            self._rolling_horizon_rebuild_pending_resource_index()

        if dispatch_count == 0:
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="ROLLING_HORIZON_DISPATCH",
                entity_id=f"RH-{window_index:05d}",
                location="CoordinationReview",
                details={
                    "window_index": window_index,
                    "dispatch_policy": self.rolling_horizon_dispatch_policy,
                    "candidate_count": len(self.rolling_horizon_pending),
                    "dispatch_count": 0,
                    "reason": "no_available_worker",
                    **self._rolling_horizon_boundary_metadata(),
                },
            )

    def _rolling_horizon_log_task_skip(self, agent: Agent, queue_entry: dict[str, Any], reason: str) -> None:
        self.rolling_horizon_metrics["stale_skipped_task_count"] += 1
        if self._rolling_horizon_dedicated_roles_active():
            self.rolling_horizon_dedicated_role_metrics["skipped_by_worker"][agent.agent_id] += 1
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="ROLLING_HORIZON_TASK_SKIPPED",
            entity_id=str(queue_entry.get("opportunity_id", "")),
            location=self.agent_display_location(agent),
            details={
                **dict(queue_entry),
                "worker_id": agent.agent_id,
                "reason": str(reason),
                "humanoid_state": self._humanoid_state_payload(agent),
            },
        )

    def _load_machine_task_from_rolling_horizon_queue_entry(self, agent: Agent, queue_entry: dict[str, Any]) -> Task | None:
        signature = queue_entry.get("rolling_task_signature")
        if not isinstance(signature, dict):
            return None
        task_type = str(queue_entry.get("task_type") or signature.get("task_type") or "").strip().upper()
        task_code = str(queue_entry.get("task_code") or signature.get("task_code") or task_type).strip().upper()
        if task_type != "LOAD_MACHINE" and task_code != "LOAD_MACHINE":
            return None

        machine_id = str(signature.get("machine_id") or signature.get("target_id") or "").strip()
        machine = self.machines.get(machine_id)
        if machine is None:
            return None
        task_id = str(queue_entry.get("task_id") or "").strip() or self._next_task_id_for_task_code("LOAD_MACHINE")
        try:
            station = int(signature.get("target_station") or machine.station)
        except (TypeError, ValueError):
            station = int(machine.station)
        load_slot = str(signature.get("load_slot") or "").strip().lower()
        item_id = str(signature.get("item_id") or signature.get("transfer_item_id") or "").strip()
        if load_slot not in {"material", "intermediate"}:
            item = self.items.get(item_id)
            load_slot = "intermediate" if item is not None and str(item.item_type).lower() == "intermediate" else "material"
        item_type = "intermediate" if load_slot == "intermediate" else "material"
        source = str(signature.get("source") or f"{item_type}_queue_{station}").strip()

        if not self._load_machine_queue_entry_feasible(agent, machine, station, load_slot, item_id, task_id):
            return None

        payload = {
            "machine_id": machine.machine_id,
            "station": station,
            "load_slot": load_slot,
            "item_type": item_type,
            "item_id": item_id,
            "source": source,
        }
        if item_type == "material":
            payload["material_id"] = item_id
        else:
            payload["intermediate_id"] = item_id
        task = Task(
            task_id=task_id,
            task_type="LOAD_MACHINE",
            priority_key=str(queue_entry.get("priority_key") or "load_machine"),
            priority=float(queue_entry.get("priority", self._rule("world.task_priority.load_machine", 105.0)) or 0.0),
            location=str(queue_entry.get("location") or f"Station{station}"),
            payload=payload,
            task_code="LOAD_MACHINE",
            assigned_robot_id=agent.agent_id,
        )
        self._sync_task_instance_id(task)
        return self._bind_humanoid_candidate_for_agent(agent, task)

    def _load_machine_queue_entry_feasible(
        self,
        agent: Agent,
        machine: Machine,
        station: int,
        load_slot: str,
        item_id: str,
        task_id: str,
    ) -> bool:
        if machine.broken or machine.output_intermediate is not None:
            return False
        if machine.setup_owner is not None and machine.setup_owner != agent.agent_id:
            return False
        if machine.state not in {MachineState.WAIT_INPUT, MachineState.SETUP, MachineState.IDLE}:
            return False
        if load_slot == "material":
            if machine.input_material is not None:
                return False
            queue = self.material_queues.get(station)
        elif load_slot == "intermediate":
            if not self._station_requires_intermediate(station) or machine.input_intermediate is not None:
                return False
            queue = self.intermediate_queues.get(station)
        else:
            return False
        if queue is None or not item_id:
            return False
        if item_id not in queue:
            return False
        if self._item_reserved_by_other(item_id, agent.agent_id, task_id):
            return False
        return True

    def _task_from_rolling_horizon_queue_entry(self, agent: Agent, queue_entry: dict[str, Any]) -> Task | None:
        # Rolling dispatch queues carry a stable task id and enough resource
        # signature to execute a still-valid queued task even when the current
        # candidate scan would bind the same item to another open resource.
        reconstructed = self._load_machine_task_from_rolling_horizon_queue_entry(agent, queue_entry)
        if reconstructed is None:
            return None
        if not self._rolling_horizon_task_allowed_for_worker(agent.agent_id, reconstructed):
            return None
        if not self._task_item_dependencies_available(reconstructed, agent):
            return None
        return reconstructed

    def _select_rolling_horizon_task(self, agent: Agent, candidates: list[Task]) -> Task | None:
        queue = self.rolling_horizon_dispatch_queues.get(agent.agent_id)
        if not queue:
            return None
        while queue:
            queue_entry = queue.popleft()
            opportunity_id = str(queue_entry.get("opportunity_id", "")).strip()
            matching = [
                task
                for task in candidates
                if self._rolling_horizon_opportunity_id(task) == opportunity_id
                and self._task_item_dependencies_available(task, agent)
            ]
            if not matching:
                reconstructed = self._task_from_rolling_horizon_queue_entry(agent, queue_entry)
                if reconstructed is None:
                    self._rolling_horizon_log_task_skip(agent, queue_entry, "stale_or_infeasible_candidate")
                    continue
                task = reconstructed
                fallback_reason = "rolling_horizon_dispatch_reconstructed"
            else:
                task = sorted(matching, key=lambda item: self._task_sort_key(item, agent))[0]
                fallback_reason = "rolling_horizon_dispatch"
            stable_task_id = str(queue_entry.get("task_id", "") or "").strip()
            if stable_task_id:
                task.task_id = stable_task_id
                self._sync_task_instance_id(task)
            score_hint = (
                float(queue_entry.get("optimizer_score"))
                if queue_entry.get("optimizer_score") is not None
                else -float(queue_entry.get("effective_priority_rank", self._rolling_horizon_priority(task)) or 0.0)
            )
            if self._rolling_horizon_throughput_optimizer_active():
                rationale = "Rolling horizon throughput optimizer selected this queued task with OR-Tools CP-SAT."
                focus = ["throughput_optimizer", self._task_priority_key(task)]
            elif self._mfg_flow_policy_active():
                rationale = "Rolling horizon dispatch selected the task by configured fixed granular priority."
                focus = [str(queue_entry.get("task_rule_id", "")) or self._task_priority_key(task)]
            elif not self._rolling_horizon_dedicated_roles_active():
                rationale = "Rolling horizon dispatch selected the task by aged HumanoidSim task-code rank."
                focus = [self._task_priority_key(task)]
            else:
                rationale = "Dedicated-role rolling horizon dispatch selected the task from the worker's configured HumanoidSim task-code list."
                focus = [self._task_priority_key(task)]
            selected = self._annotate_task_selection(
                task,
                decision_source=self.decision_mode,
                decision_rule=self.rolling_horizon_dispatch_policy,
                rationale=rationale,
                candidate_count=len(candidates),
                score_hint=score_hint,
                decision_focus=focus,
                fallback_reason=fallback_reason,
            )
            if isinstance(queue_entry.get("score_components", {}), dict):
                selected.selection_meta["score_components"] = dict(queue_entry.get("score_components", {}))
            for key in (
                "task_rule_id",
                "role_number",
                "role_task_code",
                "role_display_name",
                "role_policy",
                "role_owner_agent_id",
                "allowed_worker_ids",
                "urgent_dispatch",
                "collection_trigger",
                "queue_policy",
                "scheduler_mode",
                "candidate_collection_mode",
                "scheduled_boundary_min",
                "actual_dispatch_min",
                "boundary_lag_min",
                "dispatch_kind",
            ):
                if queue_entry.get(key) is not None:
                    selected.selection_meta[key] = copy.deepcopy(queue_entry.get(key))
            if queue_entry.get("optimizer_status") is not None:
                selected.selection_meta["optimizer_status"] = str(queue_entry.get("optimizer_status"))
            if queue_entry.get("sequence_position") is not None:
                selected.selection_meta["sequence_position"] = int(queue_entry.get("sequence_position") or 0)
            return selected
        return None


    def _selection_bias_snapshot(self, task: Task, agent: Agent) -> dict[str, Any]:
        priority_key = self._task_priority_key(task)
        effective_weights = self.current_effective_task_priority_weights(agent.agent_id)
        shared_weight = float((self.current_job_plan.task_priority_weights or {}).get(priority_key, 1.0))
        effective_weight = float(effective_weights.get(priority_key, shared_weight))
        commitments = self.current_commitment_queue(agent.agent_id)
        incident_work_orders = self.current_incident_work_order_queue(agent.agent_id)
        queue = self.current_personal_queue(agent.agent_id)
        mailbox = self.current_mailbox(agent.agent_id)
        return {
            "priority_key": priority_key,
            "shared_weight": round(shared_weight, 3),
            "effective_weight": round(effective_weight, 3),
            "incident_work_order_match": any(self._work_order_matches_task(order, task) for order in incident_work_orders[: self.worker_queue_limit]),
            "commitment_match": any(self._commitment_matches_task(commitment, task) for commitment in commitments[: self.worker_queue_limit]),
            "queue_match": any(self._work_order_matches_task(order, task) for order in queue[: self.worker_queue_limit]),
            "mailbox_match": any(self._mailbox_message_matches_task(message, task) for message in mailbox[: self.worker_queue_limit]),
            "travel_time_min": round(float(self.travel_time(agent.location, task.location)), 3),
            "agent_role": self.current_agent_role(agent.agent_id),
            "incident_work_orders": incident_work_orders[:2],
            "commitments": commitments[:2],
            "personal_queue": queue[:2],
            "mailbox": mailbox[:2],
        }

    def _incident_match_score(self, task: Task, incident: dict[str, Any]) -> float:
        affected = [str(value) for value in incident.get("affected_entities", [])] if isinstance(incident.get("affected_entities", []), list) else []
        target_id = self._task_target_id(task)
        task_location = str(task.location)
        target_station = self._task_target_station(task)
        score = 0.0
        if target_id and target_id in affected:
            score += 5.0
        if task_location in affected:
            score += 3.0
        if target_station is not None and any(str(value).lower() == f"station{target_station}".lower() for value in affected):
            score += 2.5
        incident_class = str(incident.get("incident_class", "")).strip().lower()
        if incident_class == "machine_broken" and task.task_type == "REPAIR_MACHINE":
            score += 4.0
        if incident_class in {"worker_discharged", "worker_low_battery"} and task.task_type in {"BATTERY_SWAP", "BATTERY_CHARGE", "TRANSFER"}:
            score += 4.0
        if incident_class in {"buffer_blocked", "commitment_blocked"} and task.task_type in {"UNLOAD_MACHINE", "TRANSFER", "INSPECT_PRODUCT"}:
            score += 2.0
        return score

    def _escalate_incident_if_needed(self, agent: Agent, incidents: list[dict[str, Any]]) -> None:
        if not self.incident_policy.get("enabled", True):
            return
        for incident in incidents:
            incident_id = str(incident.get("incident_id", "")).strip()
            if not incident_id:
                continue
            attempts = int(agent.local_response_attempts.get(incident_id, 0) or 0)
            if attempts < int(self.worker_local_response_cfg.get("max_local_attempts_per_incident", 2) or 2):
                continue
            source_blocker_id = str((incident.get("details", {}) if isinstance(incident.get("details", {}), dict) else {}).get("blocker_id", "")).strip()
            escalation_key = source_blocker_id or incident_id
            if escalation_key in self.incident_escalations:
                continue
            self.emit_incident(
                "replan_required",
                affected_entities=[agent.agent_id] + [str(value) for value in incident.get("affected_entities", [])],
                blocked_commitments=[str(value) for value in incident.get("blocked_commitments", []) if str(value).strip()],
                escalation_level="planner",
                details={
                    "source_incident_id": incident_id,
                    "reason": "worker_local_response_exhausted",
                    "blocker_id": source_blocker_id,
                },
            )
            self.incident_escalations.add(escalation_key)
            if source_blocker_id:
                self._mark_blocker_escalated(source_blocker_id)
            break

    def _select_local_response_task(self, candidates: list[Task], agent: Agent) -> Task | None:
        if not self.worker_local_response_cfg.get("enabled", True):
            return None
        scope = str(self.worker_local_response_cfg.get("scope", "standard")).strip().lower() or "standard"
        if scope == "minimal":
            return None
        incidents = self._recent_incidents_for_agent(agent)
        if not incidents:
            return None
        ranked: list[tuple[float, Task, str]] = []
        for incident in incidents:
            incident_id = str(incident.get("incident_id", "")).strip()
            attempts = int(agent.local_response_attempts.get(incident_id, 0) or 0)
            if attempts >= int(self.worker_local_response_cfg.get("max_local_attempts_per_incident", 2) or 2):
                continue
            for task in candidates:
                score = self._incident_match_score(task, incident)
                if score <= 0.0:
                    continue
                ranked.append((score, task, incident_id))
        if not ranked:
            for incident in incidents:
                incident_id = str(incident.get("incident_id", "")).strip()
                if not incident_id:
                    continue
                agent.local_response_attempts[incident_id] = int(agent.local_response_attempts.get(incident_id, 0) or 0) + 1
            self._escalate_incident_if_needed(agent, incidents)
            if scope != "extended":
                return None
        else:
            ranked.sort(key=lambda item: (-item[0],) + self._task_sort_key(item[1], agent))
            score, task, incident_id = ranked[0]
            if incident_id:
                agent.local_response_attempts[incident_id] = int(agent.local_response_attempts.get(incident_id, 0) or 0) + 1
            return self._annotate_task_selection(
                task,
                decision_source="worker_local_response",
                decision_rule="incident_local_response",
                rationale="Worker selected a feasible local recovery action before escalating to planner incident replanning.",
                candidate_count=len(ranked),
                score_hint=max(float(score), self._task_score(task, agent)),
                decision_focus=[str(incident.get("incident_class", "")) for incident in incidents[:2]],
                fallback_reason="incident_local_response",
            )

        if scope == "extended":
            task = sorted(candidates, key=lambda item: self._task_sort_key(item, agent))[0]
            return self._annotate_task_selection(
                task,
                decision_source="worker_local_response",
                decision_rule="extended_local_recovery",
                rationale="Worker used extended local recovery scope to keep flow moving while waiting for planner replanning.",
                candidate_count=len(candidates),
                score_hint=self._task_score(task, agent),
                decision_focus=[str(incident.get("incident_class", "")) for incident in incidents[:2]],
                fallback_reason="extended_local_recovery",
            )
        return None

    def _consume_personal_queue_matches(self, agent_id: str, task: Task) -> list[dict[str, Any]]:
        if not isinstance(self.current_job_plan.personal_queues, dict):
            return []
        queue = self.current_job_plan.personal_queues.get(str(agent_id), [])
        if not isinstance(queue, list) or not queue:
            return []
        kept: list[dict[str, Any]] = []
        consumed: list[dict[str, Any]] = []
        for item in queue:
            if not consumed and self._work_order_matches_task(item, task):
                consumed.append(dict(item) if isinstance(item, dict) else {"value": item})
                continue
            kept.append(item)
        self.current_job_plan.personal_queues[str(agent_id)] = kept
        return consumed

    def _consume_incident_work_order_matches(self, agent_id: str, task: Task) -> list[dict[str, Any]]:
        if not isinstance(self.current_job_plan.incident_work_orders, dict):
            return []
        work_orders = self.current_job_plan.incident_work_orders.get(str(agent_id), [])
        if not isinstance(work_orders, list) or not work_orders:
            return []
        kept: list[dict[str, Any]] = []
        consumed: list[dict[str, Any]] = []
        for item in work_orders:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            expires_at_day = item.get("expires_at_day")
            if expires_at_day not in {None, ""}:
                try:
                    if int(expires_at_day) < int(self.current_day):
                        continue
                except (TypeError, ValueError):
                    pass
            if self._work_order_matches_task(item, task):
                row = dict(item)
                consumed.append(row)
                remaining_uses = max(1, int(row.get("remaining_uses", 1) or 1))
                if remaining_uses > 1:
                    row["remaining_uses"] = remaining_uses - 1
                    kept.append(row)
                continue
            kept.append(item)
        self.current_job_plan.incident_work_orders[str(agent_id)] = kept
        return consumed

    def _consume_commitment_matches(self, agent_id: str, task: Task) -> list[dict[str, Any]]:
        if not isinstance(self.current_job_plan.commitments, dict):
            return []
        commitments = self.current_job_plan.commitments.get(str(agent_id), [])
        if not isinstance(commitments, list) or not commitments:
            return []
        kept: list[dict[str, Any]] = []
        consumed: list[dict[str, Any]] = []
        for item in commitments:
            if not consumed and self._commitment_matches_task(item, task):
                row = dict(item) if isinstance(item, dict) else {"value": item}
                row["status"] = "completed"
                consumed.append(row)
                commitment_id = str(row.get("commitment_id", "")).strip()
                if commitment_id:
                    self.commitment_claims[commitment_id] = {
                        "agent_id": str(agent_id),
                        "status": "completed",
                        "time_min": round(float(self.env.now), 3),
                        "opportunity_id": str(row.get("opportunity_id", "")).strip(),
                    }
                continue
            kept.append(item)
        self.current_job_plan.commitments[str(agent_id)] = kept
        return consumed

    def _consume_mailbox_matches(self, agent_id: str, task: Task) -> list[dict[str, Any]]:
        if not isinstance(self.current_job_plan.mailbox, dict):
            return []
        mailbox = self.current_job_plan.mailbox.get(str(agent_id), [])
        if not isinstance(mailbox, list) or not mailbox:
            return []
        priority_key = self._task_priority_key(task)
        target_station = self._task_target_station(task)
        target_id = self._task_target_id(task)
        kept: list[dict[str, Any]] = []
        consumed: list[dict[str, Any]] = []
        for message in mailbox:
            if not isinstance(message, dict):
                kept.append(message)
                continue
            if self._mailbox_message_matches_task(message, task):
                row = dict(message)
                consumed.append(row)
                remaining_uses = max(1, int(row.get("remaining_uses", 1) or 1))
                if str(row.get("message_type", "")).strip().lower() == "focus_window" and remaining_uses > 1:
                    row["remaining_uses"] = remaining_uses - 1
                    kept.append(row)
            else:
                kept.append(message)
        self.current_job_plan.mailbox[str(agent_id)] = kept
        return consumed


    # Runtime execution stays deterministic. The manager supplies queue/mailbox/focus
    # context, and the world converts that context into a local task choice for each worker.
    def select_task_for_agent(self, agent: Agent) -> Task | None:
        # Final task choice remains deterministic. MANAGER queues are authoritative before
        # mailbox or generic priority scoring, so the planner can act as a real operating planner.
        if agent.discharged:
            return None
        if agent.awaiting_battery_from is not None:
            return None
        if agent.suspended_task is not None:
            return self._finalize_selected_task(agent, self._bind_humanoid_candidate_for_agent(agent, self._annotate_task_selection(
                agent.suspended_task,
                decision_source="hard_constraint",
                decision_rule="resume_suspended_task",
                rationale="Resume the interrupted task before taking a new one.",
            )))

        if self._adp_active():
            return self._select_adp_task(agent)

        mandatory = None if self._rolling_horizon_active() else self.mandatory_task_for_agent(agent)
        if mandatory is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="mandatory_task_selected")
            return self._finalize_selected_task(agent, self._bind_humanoid_candidate_for_agent(agent, self._annotate_task_selection(
                mandatory,
                decision_source="hard_constraint",
                decision_rule="mandatory_battery_swap",
                rationale="Battery remaining reached the mandatory swap threshold.",
                score_hint=self._task_score(mandatory, agent),
            )))

        queue = self.rolling_horizon_dispatch_queues.get(agent.agent_id)
        head_entry = queue[0] if self._rolling_horizon_active() and queue else {}
        head_resource_keys = (
            head_entry.get("exclusive_resource_keys", [])
            if isinstance(head_entry, dict)
            else []
        )
        self._rolling_horizon_candidate_slot_exclusions = {
            str(key) for key in head_resource_keys if str(key)
        }
        try:
            candidates = self._bind_humanoid_candidates_for_agent(
                agent,
                self._filter_candidates_for_agent(agent, self._candidate_tasks(agent)),
            )
            candidates = [task for task in candidates if self._task_item_dependencies_available(task, agent)]
        finally:
            self._rolling_horizon_candidate_slot_exclusions = set()
        if not candidates:
            if self._rolling_horizon_active():
                rolling_task = self._select_rolling_horizon_task(agent, [])
                if rolling_task is not None:
                    self._resolve_selection_blocker(agent.agent_id, reason="rolling_horizon_selected")
                    return self._finalize_selected_task(agent, rolling_task)
            self._resolve_selection_blocker(agent.agent_id, reason="no_candidates")
            self._escalate_incident_if_needed(agent, self._recent_incidents_for_agent(agent))
            return None

        if self._rolling_horizon_active():
            rolling_task = self._select_rolling_horizon_task(agent, candidates)
            if rolling_task is not None:
                self._resolve_selection_blocker(agent.agent_id, reason="rolling_horizon_selected")
                return self._finalize_selected_task(agent, rolling_task)
            return None

        battery_safety_task = None if self._rolling_horizon_active() else self._select_battery_safety_task(candidates, agent)
        if battery_safety_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="battery_safety_selected")
            return self._finalize_selected_task(agent, battery_safety_task)

        if self._mfg_flow_policy_active():
            task = self._select_mfg_flow_immediate_task(candidates, agent)
            if task is not None:
                self._resolve_selection_blocker(agent.agent_id, reason="mfg_flow_shop_policy_selected")
                return self._finalize_selected_task(agent, task)

        local_response_task = self._select_local_response_task(candidates, agent)
        if local_response_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="local_response_selected")
            return self._finalize_selected_task(agent, local_response_task)

        incident_work_order_task = self._select_incident_work_order_task(candidates, agent)
        if incident_work_order_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="incident_work_order_selected")
            bias = self._selection_bias_snapshot(incident_work_order_task, agent)
            focus = [str(bias.get("priority_key", ""))]
            work_orders = bias.get("incident_work_orders", []) if isinstance(bias.get("incident_work_orders", []), list) else []
            for item in work_orders[:2]:
                if isinstance(item, dict) and str(item.get("task_family", "")).strip():
                    focus.append(str(item.get("task_family", "")).strip())
            return self._finalize_selected_task(agent, self._annotate_task_selection(
                incident_work_order_task,
                decision_source="manager_incident_work_order",
                decision_rule="incident_work_order_dispatch",
                rationale="Engine executed a short-lived emergency work order before planner queue, mailbox, or simulator fallback.",
                candidate_count=1,
                score_hint=self._task_score(incident_work_order_task, agent),
                decision_focus=[item for item in focus if item],
                fallback_reason="incident_work_order",
            ))

        commitment_task = self._select_commitment_task(candidates, agent)
        if commitment_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="commitment_selected")
            bias = self._selection_bias_snapshot(commitment_task, agent)
            focus = [str(bias.get("priority_key", ""))]
            commitments = bias.get("commitments", []) if isinstance(bias.get("commitments", []), list) else []
            for item in commitments[:2]:
                if isinstance(item, dict) and str(item.get("task_family", "")).strip():
                    focus.append(str(item.get("task_family", "")).strip())
            return self._finalize_selected_task(agent, self._annotate_task_selection(
                commitment_task,
                decision_source="manager_commitment",
                decision_rule="commitment_dispatch",
                rationale="Engine executed the first feasible commitment before planner queue, mailbox, or simulator fallback.",
                candidate_count=1,
                score_hint=self._task_score(commitment_task, agent),
                decision_focus=[item for item in focus if item],
                fallback_reason="commitment",
            ))

        if self._llm_commitment_path_active():
            blocker, created = self._activate_selection_blocker(
                agent,
                blocker_type="no_commitment_match",
                candidates=candidates,
                details={"reason": "no_commitment_or_mailbox_task", "candidate_count": len(candidates)},
            )
            if created:
                blocker_id = str(blocker.get("blocker_id", "")).strip()
                if blocker_id:
                    self.incident_escalations.add(blocker_id)
                    self._mark_blocker_escalated(blocker_id)
            return None

        queue_task = self._select_planner_queue_task(candidates, agent)
        if queue_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="planner_queue_selected")
            bias = self._selection_bias_snapshot(queue_task, agent)
            focus = [str(bias.get("priority_key", ""))]
            queue = bias.get("personal_queue", []) if isinstance(bias.get("personal_queue", []), list) else []
            for item in queue[:2]:
                if isinstance(item, dict) and str(item.get("task_family", "")).strip():
                    focus.append(str(item.get("task_family", "")).strip())
            return self._finalize_selected_task(agent, self._annotate_task_selection(
                queue_task,
                decision_source="manager_queue",
                decision_rule="personal_queue_dispatch",
                rationale="Engine executed the first feasible planner queue order before considering mailbox or generic priority scoring.",
                candidate_count=1,
                score_hint=self._task_score(queue_task, agent),
                decision_focus=[item for item in focus if item],
                fallback_reason="personal_queue",
            ))

        mailbox_candidates = self._matching_mailbox_candidates(candidates, agent)
        if mailbox_candidates:
            self._resolve_selection_blocker(agent.agent_id, reason="mailbox_selected")
            scored_candidates = sorted(mailbox_candidates, key=lambda task: self._task_sort_key(task, agent))
            task = scored_candidates[0]
            bias = self._selection_bias_snapshot(task, agent)
            focus = [str(bias.get("priority_key", ""))]
            queue = bias.get("personal_queue", []) if isinstance(bias.get("personal_queue", []), list) else []
            for item in queue[:2]:
                if isinstance(item, dict) and str(item.get("task_family", "")).strip():
                    focus.append(str(item.get("task_family", "")).strip())
            return self._finalize_selected_task(agent, self._annotate_task_selection(
                task,
                decision_source="manager_queue",
                decision_rule="mailbox_dispatch",
                rationale="Engine selected the highest priority mailbox-matched feasible task after no feasible planner queue order was available.",
                candidate_count=len(mailbox_candidates),
                score_hint=self._task_score(task, agent),
                decision_focus=[item for item in focus if item],
                fallback_reason="mailbox",
            ))

        if self.decision_mode == "bottleneck_aware_dispatch":
            task = self._select_bottleneck_aware_task(candidates, agent)
            if task is not None:
                self._resolve_selection_blocker(agent.agent_id, reason="bottleneck_aware_selected")
                return self._finalize_selected_task(agent, task)

        scored_candidates = sorted(candidates, key=lambda task: self._task_sort_key(task, agent))
        task = scored_candidates[0]
        self._resolve_selection_blocker(agent.agent_id, reason="legacy_fallback_selected")
        bias = self._selection_bias_snapshot(task, agent)
        focus = [str(bias.get("priority_key", ""))]
        queue = bias.get("personal_queue", []) if isinstance(bias.get("personal_queue", []), list) else []
        for item in queue[:2]:
            if isinstance(item, dict) and str(item.get("task_family", "")).strip():
                focus.append(str(item.get("task_family", "")).strip())
        return self._finalize_selected_task(agent, self._annotate_task_selection(
            task,
            decision_source="simulator_fallback",
            decision_rule="legacy_priority_score_fallback",
            rationale="Legacy fallback chose the highest priority feasible task because no commitment, planner queue, or mailbox task was available.",
            candidate_count=len(candidates),
            score_hint=self._task_score(task, agent),
            decision_focus=[item for item in focus if item],
            fallback_reason="legacy_priority_score",
        ))

    # shared weight는 하루 단위 의도를 나타내고, agent multiplier는 그 의도를 개인별로 미세 조정한다.
    # queue와 mailbox는 이미 상위 선택 tier에서 처리하므로 최종 점수식은 단순하게 유지한다.
    def _task_score(self, task: Task, agent: Worker | str | None = None) -> float:
        priority_key = self._task_priority_key(task)
        if isinstance(agent, Worker):
            effective = self.current_effective_task_priority_weights(agent.agent_id)
            weight = float(effective.get(priority_key, 1.0))
            return float(task.priority) * weight
        if isinstance(agent, str) and agent.strip():
            effective = self.current_effective_task_priority_weights(agent.strip())
            weight = float(effective.get(priority_key, 1.0))
            return float(task.priority) * weight
        weight = float((self.current_job_plan.task_priority_weights or {}).get(priority_key, 1.0))
        return float(task.priority) * weight

    def _handover_item_candidates(self, agent: Agent, priority: float) -> list[Task]:
        if not self.product_collaboration_enabled:
            return []
        if agent.discharged or agent.carrying_item_id is not None or agent.awaiting_battery_from is not None:
            return []
        candidates: list[Task] = []
        for session in list(self.product_transport_sessions.values()):
            if not isinstance(session, dict) or str(session.get("status", "active")) != "active":
                continue
            carrier_ids = [str(worker_id) for worker_id in session.get("carrier_ids", []) if str(worker_id)]
            if agent.agent_id in carrier_ids:
                continue
            if len(carrier_ids) >= int(session.get("max_carriers", self.product_collaboration_max_carriers) or self.product_collaboration_max_carriers):
                continue
            source_agent_id = str(session.get("primary_worker_id", ""))
            source_agent = self.agents.get(source_agent_id)
            if source_agent is None or source_agent.carrying_item_id != str(session.get("item_id", "")):
                continue
            if not self._product_session_has_remaining_path(session):
                continue
            if not self._product_session_join_feasible(agent, session):
                continue
            candidates.append(
                Task(
                    task_id=self._next_task_id("HND"),
                    task_type="HANDOVER_ITEM",
                    priority_key="handover_item",
                    priority=priority,
                    location=self.agent_display_location(source_agent),
                    payload={
                        "handover_kind": "product_collaboration_join",
                        "transport_session_id": str(session.get("session_id", "")),
                        "item_id": str(session.get("item_id", "")),
                        "item_type": "product",
                        "source_agent_id": source_agent_id,
                        "recipient_agent_id": agent.agent_id,
                        "destination": str(session.get("destination", "")),
                        "max_carriers": int(session.get("max_carriers", self.product_collaboration_max_carriers) or self.product_collaboration_max_carriers),
                    },
                )
            )
        return candidates

    def _candidate_tasks(self, agent: Agent) -> list[Task]:
        tasks: list[Task] = []
        queued_resource_keys = (
            self._rolling_horizon_queued_resource_keys()
            if self._rolling_horizon_active()
            else set()
        )
        local_candidate_item_ids: set[str] = {
            key.split(":", 1)[1]
            for key in queued_resource_keys
            if key.startswith("item:") and ":" in key
        }
        queued_material_slot_ids: set[str] = {
            key.split(":", 1)[1]
            for key in queued_resource_keys
            if key.startswith("material_slot:") and ":" in key
        }
        candidate_buffer_slots: dict[str, deque[int]] = {}

        def _next_buffer_slot(buffer_id: str, *, limit: int | None = None) -> int | None:
            if buffer_id not in candidate_buffer_slots:
                candidate_buffer_slots[buffer_id] = deque(
                    self._buffer_available_slot_tokens(buffer_id, limit=limit)
                )
            slots = candidate_buffer_slots[buffer_id]
            return int(slots.popleft()) if slots else None
        deliver_priority_discharged = float(self._rule("world.task_priority.battery_delivery_discharged", 149.0))
        deliver_priority_low_battery = float(self._rule("world.task_priority.battery_delivery_low_battery", 140.0))
        priority_repair_machine = float(self._rule("world.task_priority.repair_machine", 115.0))
        priority_unload_machine = float(self._rule("world.task_priority.unload_machine", 110.0))
        priority_load_machine = float(self._rule("world.task_priority.load_machine", 105.0))
        priority_setup_machine = float(self._rule("world.task_priority.setup_machine", 90.0))
        priority_pm = float(self._rule("world.task_priority.preventive_maintenance", 65.0))
        priority_inter_station_transfer = float(self._rule("world.task_priority.inter_station_transfer", 85.0))
        priority_material_supply = float(self._rule("world.task_priority.material_supply", 85.0))
        priority_inspect_product = float(self._rule("world.task_priority.inspect_product", 72.0))
        priority_load_inspection_desk = float(self._rule("world.task_priority.load_inspection_desk", 74.0))
        priority_unload_inspection_desk = float(self._rule("world.task_priority.unload_inspection_desk", 112.0))
        priority_handover_item = float(self._rule("world.task_priority.handover_item", 100.0))

        proactive_swap = self._proactive_battery_swap_task(agent)
        if proactive_swap is not None:
            tasks.append(proactive_swap)

        if "HANDOVER_ITEM_TO_ROBOT" in self.enabled_task_codes:
            tasks.extend(self._handover_item_candidates(agent, priority_handover_item))

        for other in (self.agents.values() if self.battery_delivery_enabled else []):
            if other.agent_id == agent.agent_id:
                continue
            deliver_threshold = self._battery_delivery_trigger_threshold(other)
            if (
                (other.discharged or self.battery_remaining(other) <= deliver_threshold)
                and other.battery_service_owner is None
            ):
                deliver_priority = deliver_priority_discharged if other.discharged else deliver_priority_low_battery
                tasks.append(
                    Task(
                        task_id=self._next_task_id("DBAT"),
                        task_type="TRANSFER",
                        priority_key="battery_delivery_discharged" if other.discharged else "battery_delivery_low_battery",
                        priority=deliver_priority,
                        location=self.agent_display_location(other),
                        payload={"transfer_kind": "battery_delivery", "target_agent_id": other.agent_id, "target_agent_discharged": bool(other.discharged)},
                    )
                )

        for machine in self.machines.values():
            if machine.broken and agent.agent_id not in machine.repair_team and self._repair_team_size(machine) < self.max_repair_agents:
                urgency = self.repair_urgency(machine)
                tasks.append(
                    Task(
                        task_id=self._next_task_id("RM"),
                        task_type="REPAIR_MACHINE",
                        priority_key="repair_machine",
                        priority=priority_repair_machine,
                        location=f"Station{machine.station}",
                        payload={
                            "machine_id": machine.machine_id,
                            "station": machine.station,
                            "repair_team_size": self._repair_team_size(machine),
                            "repair_slots_remaining": self._repair_slots_remaining(machine),
                            "repair_remaining_min": round(float(machine.repair_work_remaining_min), 3),
                            "failed_since": float(machine.failed_since or self.env.now),
                            **urgency,
                        },
                    )
                )
            elif machine.output_intermediate is not None and machine.unload_owner is None:
                destination_buffer_id = f"output_buffer_station_{machine.station}"
                destination_slot_token = _next_buffer_slot(destination_buffer_id)
                if destination_slot_token is not None:
                    tasks.append(
                        Task(
                            task_id=self._next_task_id("UL"),
                            task_type="UNLOAD_MACHINE",
                            priority_key="unload_machine",
                            priority=priority_unload_machine,
                            location=f"Station{machine.station}",
                            payload={
                                "machine_id": machine.machine_id,
                                "station": machine.station,
                                "destination_buffer_id": destination_buffer_id,
                                "destination_slot_token": destination_slot_token,
                            },
                        )
                    )
            elif (
                not machine.broken
                and machine.state == MachineState.WAIT_INPUT
                and machine.setup_owner is None
                and machine.output_intermediate is None
            ):
                load_created = False
                if machine.input_material is None:
                    material_id = self._first_unreserved_queue_item(
                        self.material_queues[machine.station],
                        agent.agent_id,
                        exclude_item_ids=local_candidate_item_ids,
                    )
                    if material_id:
                        local_candidate_item_ids.add(material_id)
                        load_created = True
                        tasks.append(
                            Task(
                                task_id=self._next_task_id("LOAD"),
                                task_type="LOAD_MACHINE",
                                priority_key="load_machine",
                                priority=priority_load_machine,
                                location=f"Station{machine.station}",
                                payload={
                                    "machine_id": machine.machine_id,
                                    "station": machine.station,
                                    "load_slot": "material",
                                    "item_type": "material",
                                    "item_id": material_id,
                                    "material_id": material_id,
                                    "source": f"material_queue_{machine.station}",
                                },
                            )
                        )
                if self._station_requires_intermediate(machine.station) and machine.input_intermediate is None:
                    intermediate_id = self._first_unreserved_queue_item(
                        self.intermediate_queues[machine.station],
                        agent.agent_id,
                        exclude_item_ids=local_candidate_item_ids,
                    )
                    if intermediate_id:
                        local_candidate_item_ids.add(intermediate_id)
                        load_created = True
                        tasks.append(
                            Task(
                                task_id=self._next_task_id("LOAD"),
                                task_type="LOAD_MACHINE",
                                priority_key="load_machine",
                                priority=priority_load_machine,
                                location=f"Station{machine.station}",
                                payload={
                                    "machine_id": machine.machine_id,
                                    "station": machine.station,
                                    "load_slot": "intermediate",
                                    "item_type": "intermediate",
                                    "item_id": intermediate_id,
                                    "intermediate_id": intermediate_id,
                                    "source": f"intermediate_queue_{machine.station}",
                                },
                            )
                        )
                inputs_ready = machine.input_material is not None and (
                    not self._station_requires_intermediate(machine.station)
                    or machine.input_intermediate is not None
                )
                if inputs_ready and not machine.setup_ready and not load_created:
                    tasks.append(
                        Task(
                            task_id=self._next_task_id("SET"),
                            task_type="SETUP_MACHINE",
                            priority_key="setup_machine",
                            priority=priority_setup_machine,
                            location=f"Station{machine.station}",
                            payload={"machine_id": machine.machine_id, "station": machine.station},
                        )
                    )

            pm_due = self.machine_preventive_maintenance_due(machine)
            if (
                self.preventive_maintenance_enabled
                and
                pm_due
                and not machine.broken
                and machine.state != MachineState.PROCESSING
                and machine.output_intermediate is None
                and machine.pm_owner is None
            ):
                tasks.append(
                    Task(
                        task_id=self._next_task_id("PM"),
                        task_type="PREVENTIVE_MAINTENANCE",
                        priority_key="preventive_maintenance",
                        priority=priority_pm,
                        location=f"Station{machine.station}",
                        payload={
                            "machine_id": machine.machine_id,
                            "station": machine.station,
                            "processing_since_maintenance_min": round(
                                float(machine.processing_since_maintenance_min), 6
                            ),
                            "pm_due_processing_min": round(
                                float(self.pm_interval_target_min), 6
                            ),
                        },
                    )
                )

        for station, buffer in self.output_buffers.items():
            if buffer:
                transfer_item_ids = self._unreserved_queue_items(
                    buffer,
                    len(buffer),
                    agent.agent_id,
                    exclude_item_ids=local_candidate_item_ids,
                )
                task_location = "Inspection" if station == self.inspection_queue_station else f"Station{station}"
                transfer_priority = priority_inter_station_transfer
                if station == self.inspection_queue_station:
                    transfer_priority = max(
                        transfer_priority,
                        float(self._rule("world.task_priority.completed_product_delivery", 125.0)),
                    )
                for transfer_item_id in transfer_item_ids:
                    payload = {
                        "transfer_kind": "inter_station",
                        "from_station": station,
                        "transfer_item_id": transfer_item_id,
                    }
                    destination_buffer_id = ""
                    destination_slot_token: int | None = None
                    if station != self.inspection_queue_station:
                        target_station = station + 1
                        if target_station > self.last_processing_station:
                            target_station = self.inspection_queue_station
                        destination_buffer_id = f"intermediate_queue_{target_station}"
                        destination_slot_token = _next_buffer_slot(destination_buffer_id)
                        if destination_slot_token is None:
                            break
                        payload["destination_buffer_id"] = destination_buffer_id
                        payload["destination_slot_token"] = destination_slot_token
                    local_candidate_item_ids.add(transfer_item_id)
                    tasks.append(
                        Task(
                            task_id=self._next_task_id("TR"),
                            task_type="TRANSFER",
                            priority_key="inter_station_transfer",
                            priority=transfer_priority,
                            location=task_location,
                            payload=payload,
                        )
                    )

        for station in self.stations:
            active_supply_task_ids = self._active_material_supply_task_ids_for_station(station)
            requested_count = self._station_material_replenishment_count(station)
            if not self.is_mfg_flow_shop and (self.material_supply_owner.get(station) is not None or active_supply_task_ids):
                requested_count = 0
            if requested_count > 0 and self._material_shelf_count_for_station(station) > 0:
                if not self.is_mfg_flow_shop:
                    tasks.append(
                        Task(
                            task_id=self._next_task_id("MAT"),
                            task_type="TRANSFER",
                            priority_key="material_supply",
                            priority=priority_material_supply,
                            location="Warehouse",
                            payload={
                                "transfer_kind": "material_supply",
                                "station": station,
                                "source": "Warehouse",
                                "destination": f"material_queue_{station}",
                                "item_request": {
                                    "entity_type": "material",
                                    "selection_policy": "available_material_from_source",
                                    "quantity": 1,
                                },
                                "target_level": int(
                                    self.inventory_targets["material"][f"station{station}"]
                                ),
                            },
                        )
                    )
                    continue
                excluded_shelf_items = set(local_candidate_item_ids)
                for _ in range(requested_count):
                    destination_buffer_id = f"material_queue_{station}"
                    destination_slot_token = _next_buffer_slot(destination_buffer_id)
                    source_slot = self._first_available_material_shelf_slot(
                        agent.agent_id,
                        exclude_item_ids=excluded_shelf_items,
                        exclude_slot_ids=queued_material_slot_ids,
                        station=station,
                    )
                    if destination_slot_token is None or source_slot is None:
                        break
                    source_slot_id = str(source_slot.get("slot_id") or "")
                    material_item_id = str(source_slot.get("material_item_id") or "")
                    if not source_slot_id or not material_item_id:
                        break
                    excluded_shelf_items.add(material_item_id)
                    local_candidate_item_ids.add(material_item_id)
                    payload = {
                        "transfer_kind": "material_supply",
                        "station": station,
                        "source": "Warehouse",
                        "destination": destination_buffer_id,
                        "destination_buffer_id": destination_buffer_id,
                        "destination_slot_token": destination_slot_token,
                        "source_slot_id": source_slot_id,
                        "transfer_item_id": material_item_id,
                        "material_item_id": material_item_id,
                        "item_request": {
                            "entity_type": "material",
                            "selection_policy": "available_material_from_source",
                            "quantity": 1,
                        },
                    }
                    payload["supply_policy"] = "machine_demand"
                    tasks.append(
                        Task(
                            task_id=self._next_task_id("MAT"),
                            task_type="TRANSFER",
                            priority_key="material_supply",
                            priority=priority_material_supply,
                            location="Warehouse",
                            payload=payload,
                        )
                    )
            elif requested_count > 0 and self._material_shelf_count() <= 0:
                self._log_material_shelf_empty_once()

        if (
            "LOAD_UNLOAD_TRANSFER_INTERFACE" in self.enabled_task_codes
            and self.inspection_desk_state == "EMPTY"
            and self.inspection_desk_item_id is None
            and self.intermediate_queues[self.inspection_queue_station]
            and self.inspection_owner is None
        ):
            product_id = self._first_unreserved_queue_item(
                self.intermediate_queues[self.inspection_queue_station],
                agent.agent_id,
                exclude_item_ids=local_candidate_item_ids,
            )
            if product_id:
                local_candidate_item_ids.add(product_id)
                tasks.append(
                    Task(
                        task_id=self._next_task_id("IFT"),
                        task_type="LOAD_UNLOAD_TRANSFER_INTERFACE",
                        priority_key="load_inspection_desk",
                        priority=priority_load_inspection_desk,
                        location="Inspection",
                        payload={
                            "interface_action": "load",
                            "action": "load",
                            "inspection_product_id": product_id,
                            "item_id": product_id,
                            "item_type": "product",
                            "interface": self.inspection_workstation_id,
                            "source": f"intermediate_queue_{self.inspection_queue_station}",
                            "destination": self.inspection_workstation_id,
                        },
                    )
                )
        elif (
            self.inspection_desk_state == "STAGED_FOR_INSPECTION"
            and self.inspection_desk_item_id
            and self.inspection_desk_result is None
            and self.inspection_owner is None
        ):
            product_id = str(self.inspection_desk_item_id)
            if not self._item_reserved_by_other(product_id, agent.agent_id):
                local_candidate_item_ids.add(product_id)
                tasks.append(
                    Task(
                        task_id=self._next_task_id("INS"),
                        task_type="INSPECT_PRODUCT",
                        priority_key="inspect_product",
                        priority=priority_inspect_product,
                        location="Inspection",
                        payload={
                            "inspection_product_id": product_id,
                            "workstation": self.inspection_workstation_id,
                        },
                    )
                )
        elif (
            "LOAD_UNLOAD_TRANSFER_INTERFACE" in self.enabled_task_codes
            and self.inspection_desk_state == "INSPECTED_WAITING_UNLOAD"
            and self.inspection_desk_item_id
            and self.inspection_desk_result in {"PASS", "FAIL"}
            and self.inspection_owner is None
        ):
            product_id = str(self.inspection_desk_item_id)
            result = str(self.inspection_desk_result)
            destination_buffer_id = (
                f"output_buffer_station_{self.inspection_queue_station}"
                if result == "PASS"
                else "inspection_scrap_queue"
            )
            destination_slot_token = _next_buffer_slot(destination_buffer_id)
            if (
                destination_slot_token is not None
                and not self._item_reserved_by_other(product_id, agent.agent_id)
            ):
                destination = "inspection_output_queue" if result == "PASS" else "inspection_scrap_queue"
                local_candidate_item_ids.add(product_id)
                tasks.append(
                    Task(
                        task_id=self._next_task_id("IFT"),
                        task_type="LOAD_UNLOAD_TRANSFER_INTERFACE",
                        priority_key="unload_inspection_desk",
                        priority=priority_unload_inspection_desk,
                        location="Inspection",
                        payload={
                            "interface_action": "unload",
                            "action": "unload",
                            "inspection_product_id": product_id,
                            "item_id": product_id,
                            "item_type": "product",
                            "inspection_result": result,
                            "interface": self.inspection_workstation_id,
                            "source": self.inspection_workstation_id,
                            "destination": destination,
                            "destination_buffer_id": destination_buffer_id,
                            "destination_slot_token": destination_slot_token,
                        },
                    )
                )
        if self.inspection_scrap_queue and self.scrap_disposal_owner is None:
            scrap_item_ids = self._unreserved_queue_items(
                self.inspection_scrap_queue,
                self.scrap_transport_max_carry_count,
                agent.agent_id,
                exclude_item_ids=local_candidate_item_ids,
            )
            if scrap_item_ids:
                local_candidate_item_ids.update(scrap_item_ids)
                tasks.append(
                    Task(
                        task_id=self._next_task_id("SCRAP"),
                        task_type="COLLECT_WASTE_OR_SCRAP",
                        priority_key="scrap_disposal",
                        priority=max(priority_inspect_product, float(self._rule("world.task_priority.scrap_disposal", 118.0))),
                        location="Inspection",
                        payload={
                            "source": "inspection_scrap_queue",
                            "destination": "scrap_disposal_bin",
                            "max_carry_count": self.scrap_transport_max_carry_count,
                            "item_ids": scrap_item_ids,
                        },
                    )
                )
        filtered_tasks = [
            task
            for task in tasks
            if TASK_CODE_BY_PRIORITY_KEY.get(self._task_priority_key(task), str(task.task_code or "").upper())
            in self.enabled_task_codes
        ]
        self.buffer_metrics["candidate_scan_count"] += 1
        self.buffer_metrics["candidate_count_total"] += len(filtered_tasks)
        self.buffer_metrics["candidate_count_max"] = max(
            int(self.buffer_metrics.get("candidate_count_max", 0)),
            len(filtered_tasks),
        )
        return filtered_tasks

    def travel_time(self, src: str, dst: str) -> float:
        if self.grid_map is not None:
            return float(self.grid_map.travel_time(src, dst))

        def _is_station_zone(zone: str) -> bool:
            return zone.startswith("Station") or zone == "Inspection"

        if src == dst:
            return 0.0
        if _is_station_zone(src) and _is_station_zone(dst):
            return float(self.movement_cfg["station_to_station_min"])
        if (src == "Warehouse" and _is_station_zone(dst)) or (dst == "Warehouse" and _is_station_zone(src)):
            return float(self.movement_cfg["warehouse_to_station_min"])
        if src == "BatteryStation" or dst == "BatteryStation":
            return float(self.movement_cfg["to_battery_station_min"])
        return float(self.movement_cfg["default_min"])

    def _grid_logical_destination(self, dst: str) -> str:
        if self.grid_map is None:
            return str(dst)
        normalized = self.grid_map.normalize_location(dst)
        target_agent = self.agents.get(normalized)
        if target_agent is not None:
            return str(target_agent.location)
        return self.grid_map.logical_location(normalized)

    def _move_agent_grid(self, agent: Agent, dst: str, emit_move_events: bool = True):
        grid = self.grid_map
        if grid is None:
            return

        eps = 1e-6
        if agent.tile is None:
            agent.tile = grid.register_worker(agent.agent_id, grid.initial_worker_tile(agent.agent_id))
        elif grid.worker_tiles.get(agent.agent_id) != agent.tile:
            agent.tile = grid.register_worker(agent.agent_id, agent.tile)

        logical_dst = self._grid_logical_destination(dst)
        start_tile = agent.tile
        blocked_started_at: float | None = None
        blocked_event_emitted = False
        movement_started = False
        planned_path: list[Tile] = [start_tile]
        planned_duration = 0.0
        move_id = ""
        sampled_tile_time_min = float(grid.tile_time_min)
        move_time_multiplier = 1.0
        segment_index = 0
        path_wait_incident_emitted = False
        last_path_wait_motion_refresh_at: float | None = None
        path_wait_motion_refresh_interval = max(float(grid.tile_time_min), 0.5)
        ignore_dynamic = self._traffic_observe_conflicts()
        self._ensure_product_transport_session(agent, destination=dst)

        while True:
            current_tile = agent.tile
            if current_tile is None:
                return
            if self._should_interrupt_for_battery(agent, eps):
                if agent.reserved_tile is not None:
                    grid.release_reservation(agent.agent_id, agent.reserved_tile)
                    agent.reserved_tile = None
                self._close_current_move_segment(agent, logical_destination=logical_dst)
                if move_id:
                    self._traffic_complete_plan(move_id)
                    self._log_interrupted_move(agent, reason="battery_depleted", logical_destination=logical_dst)
                self._clear_current_move(agent)
                self._clear_in_transit(agent)
                if not agent.discharged:
                    self.discharge_agent(agent, reason="battery_depleted", interrupt_process=False)
                raise simpy.Interrupt("battery_depleted")
            destination_tiles = grid.destination_tiles(dst, worker_id=agent.agent_id, from_tile=current_tile, ignore_dynamic=ignore_dynamic)
            if current_tile in destination_tiles:
                break

            path = grid.find_path(current_tile, destination_tiles, worker_id=agent.agent_id, ignore_dynamic=ignore_dynamic)
            if not path or len(path) < 2:
                if blocked_started_at is None:
                    blocked_started_at = float(self.env.now)
                blocked_for = float(self.env.now) - blocked_started_at
                details = {
                    "conflict_type": "TRAFFIC_WAIT",
                    "severity": "warning",
                    "collision": False,
                    "primary_worker_id": agent.agent_id,
                    "worker_ids": [agent.agent_id],
                    "move_id": move_id,
                    "wait_kind": "path_unavailable",
                    "destination": str(dst),
                    "logical_destination": logical_dst,
                    "from_tile": self._tile_payload(current_tile),
                    "destination_tiles": [self._tile_payload(tile) for tile in sorted(destination_tiles)],
                    "time_window": {
                        "started_at": round(float(self.env.now), 3),
                        "ended_at": round(float(self.env.now + grid.tile_time_min), 3),
                    },
                    "blocked_for_min": round(blocked_for, 3),
                    "humanoid_state": self._humanoid_state_payload(agent),
                    "traffic_mode": self.traffic_mode,
                    "collision_effect": self.traffic_collision_effect,
                }
                if not path_wait_incident_emitted and self.traffic_enabled:
                    path_wait_incident_emitted = True
                    self.traffic_conflicts.append(copy.deepcopy(details))
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="AGENT_TRAFFIC_CONFLICT",
                        entity_id=agent.agent_id,
                        location=self.agent_display_location(agent),
                        details=details,
                    )
                    current_availability = str(self._humanoid_state_payload(agent).get("availability", "")).upper()
                    wait_can_change_state = current_availability not in {"BLOCKED", "DISABLED", "OFFLINE"}
                    if wait_can_change_state and bool(self.humanoid_incident_natural_cfg.get("TRAFFIC_WAIT", True)):
                        yield from self._execute_active_humanoid_incident_recovery(
                            agent,
                            "TRAFFIC_WAIT",
                            primitive_call_code="NAVIGATE_TO",
                            source="mansim.traffic",
                            context={key: value for key, value in details.items() if key != "humanoid_state"},
                        )
                if movement_started and (
                    last_path_wait_motion_refresh_at is None
                    or float(self.env.now) - last_path_wait_motion_refresh_at >= path_wait_motion_refresh_interval
                ):
                    last_path_wait_motion_refresh_at = float(self.env.now)
                    current_availability = str(self._humanoid_state_payload(agent).get("availability", "")).upper()
                    if current_availability in {"BLOCKED", "DISABLED", "OFFLINE"}:
                        self._log_worker_state_observation(agent, reason="path_wait")
                    else:
                        self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO", reason="path_wait")
                if blocked_for >= grid.blocked_replan_threshold_min and not blocked_event_emitted:
                    blocked_event_emitted = True
                    blocked_details = {
                        "destination": str(dst),
                        "logical_destination": logical_dst,
                        "from_tile": self._tile_payload(current_tile),
                        "blocked_for_min": round(blocked_for, 3),
                    }
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="AGENT_TILE_BLOCKED",
                        entity_id=agent.agent_id,
                        location=self.agent_display_location(agent),
                        details=blocked_details,
                    )
                    if bool(self.humanoid_incident_natural_cfg.get("PATH_BLOCKED", True)):
                        yield from self._execute_active_humanoid_incident_recovery(
                            agent,
                            "PATH_BLOCKED",
                            primitive_call_code="NAVIGATE_TO",
                            source="mansim.traffic",
                            context=blocked_details,
                        )
                yield self.env.timeout(grid.tile_time_min)
                continue

            if not movement_started:
                movement_started = True
                planned_path = path
                move_id = f"{agent.agent_id}-move-{next(self.traffic_move_counter):06d}"
                move_time_multiplier = self._current_transport_time_multiplier(agent)
                current_task_id = str(agent.current_task_id or "")
                timing_session = self.active_movement_timing_sessions.get(agent.agent_id, {})
                if (
                    str(timing_session.get("task_id", "")) == current_task_id
                    and str(timing_session.get("destination", "")) == logical_dst
                ):
                    timing_sample_key = str(timing_session["sample_key"])
                else:
                    timing_sample_key = (
                        f"{current_task_id or agent.agent_id}:{logical_dst}:"
                        f"move-{next(self.timing_move_counter):06d}"
                    )
                    self.active_movement_timing_sessions[agent.agent_id] = {
                        "task_id": current_task_id,
                        "destination": logical_dst,
                        "sample_key": timing_sample_key,
                    }
                sampled_tile_time_min = self.timing.sample_tile_time(
                    sample_key=timing_sample_key,
                    multiplier=move_time_multiplier,
                )
                planned_duration = max(0.0, float(len(path) - 1) * sampled_tile_time_min)
                agent.current_move_id = move_id
                agent.current_move_started_at = float(self.env.now)
                agent.current_move_logical_destination = logical_dst
                self._set_in_transit(agent, str(agent.location), logical_dst, 0.0, planned_duration)
                self._set_worker_motion(
                    agent,
                    str(agent.location),
                    logical_dst,
                    0.0,
                    planned_duration,
                    path_tiles=planned_path,
                    target_tile=path[-1],
                )
                if emit_move_events:
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="AGENT_MOVE_START",
                        entity_id=agent.agent_id,
                        location=str(agent.location),
                        details={
                            "from": str(agent.location),
                            "to": logical_dst,
                            "duration": round(planned_duration, 3),
                            "from_tile": self._tile_payload(current_tile),
                            "to_tile": self._tile_payload(path[-1]),
                            "path_tiles": [self._tile_payload(tile) for tile in path],
                            "sampled_tile_time_min": round(sampled_tile_time_min, 6),
                            "base_tile_time_distribution": self.timing.movement_distribution.to_dict(),
                            "timing_sample_key": timing_sample_key,
                            "carrying_item_type": agent.carrying_item_type,
                            "carrying_item_id": agent.carrying_item_id,
                            "item_time_multiplier": round(self._item_transport_multiplier(agent.carrying_item_type), 3),
                            "effective_time_multiplier": round(move_time_multiplier, 3),
                            "transport_session": self._worker_cargo_payload(agent).get("transport_session_id"),
                            "carrier_ids": self._worker_cargo_payload(agent).get("carrier_ids", []),
                            "move_id": move_id,
                        },
                    )
                self._traffic_register_plan(
                    agent,
                    move_id,
                    planned_path,
                    started_at=float(self.env.now),
                    ended_at=float(self.env.now) + planned_duration,
                )

            next_tile = path[1]
            if self._traffic_strict_reservation() and not grid.try_reserve(agent.agent_id, next_tile):
                first_traffic_wait = blocked_started_at is None
                if blocked_started_at is None:
                    blocked_started_at = float(self.env.now)
                if self.traffic_enabled:
                    details = {
                        "conflict_type": "TRAFFIC_WAIT",
                        "severity": "warning",
                        "collision": False,
                        "primary_worker_id": agent.agent_id,
                        "worker_ids": [agent.agent_id],
                        "move_id": move_id,
                        "tile": self._tile_payload(next_tile),
                        "time_window": {
                            "started_at": round(float(self.env.now), 3),
                            "ended_at": round(float(self.env.now + grid.tile_time_min), 3),
                        },
                        "humanoid_state": self._humanoid_state_payload(agent),
                        "traffic_mode": self.traffic_mode,
                        "collision_effect": self.traffic_collision_effect,
                    }
                    self.traffic_conflicts.append(copy.deepcopy(details))
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="AGENT_TRAFFIC_CONFLICT",
                        entity_id=agent.agent_id,
                        location=self.agent_display_location(agent),
                        details=details,
                    )
                    current_availability = str(self._humanoid_state_payload(agent).get("availability", "")).upper()
                    wait_can_change_state = current_availability not in {"BLOCKED", "DISABLED", "OFFLINE"}
                    if first_traffic_wait and wait_can_change_state and bool(self.humanoid_incident_natural_cfg.get("TRAFFIC_WAIT", True)):
                        yield from self._execute_active_humanoid_incident_recovery(
                            agent,
                            "TRAFFIC_WAIT",
                            primitive_call_code="NAVIGATE_TO",
                            source="mansim.traffic",
                            context={key: value for key, value in details.items() if key != "humanoid_state"},
                        )
                yield self.env.timeout(grid.tile_time_min)
                continue

            if blocked_started_at is not None:
                self._resume_humanoid_task_after_recovery(
                    agent,
                    reason="traffic_recovered",
                    source="mansim.traffic",
                )
                self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO", reason="traffic_recovered")
            blocked_started_at = None
            path_wait_incident_emitted = False
            last_path_wait_motion_refresh_at = None
            agent.reserved_tile = next_tile
            segment_index += 1
            segment_started_at = float(self.env.now)
            segment_duration = sampled_tile_time_min
            segment_ended_at = segment_started_at + segment_duration
            agent.current_move_segment_index = segment_index
            agent.current_move_segment_from_tile = current_tile
            agent.current_move_segment_to_tile = next_tile
            agent.current_move_logical_destination = logical_dst
            traffic_recovery_requested = self._traffic_begin_segment(
                agent,
                move_id=move_id,
                segment_index=segment_index,
                from_tile=current_tile,
                to_tile=next_tile,
                started_at=segment_started_at,
                ended_at=segment_ended_at,
                logical_destination=logical_dst,
            )
            if traffic_recovery_requested and isinstance(getattr(agent, "pending_recovery_incident", None), dict):
                grid.release_reservation(agent.agent_id, next_tile)
                agent.reserved_tile = None
                self._close_current_move_segment(agent, logical_destination=logical_dst)
                runtime = getattr(self, "humanoid_runtime", None)
                if runtime is not None and getattr(runtime, "enabled", False):
                    yield from runtime._execute_pending_recovery_protocol(
                        agent,
                        self._current_parent_task_stub(agent),
                        complete_parent_on_success=False,
                    )
                blocked_started_at = float(self.env.now)
                continue
            self._start_shared_transport_segment(
                agent,
                from_tile=current_tile,
                to_tile=next_tile,
                logical_destination=logical_dst,
                segment_duration=segment_duration,
                segment_index=segment_index,
            )

            if self._should_interrupt_for_battery(agent, eps):
                grid.release_reservation(agent.agent_id, next_tile)
                agent.reserved_tile = None
                self._close_current_move_segment(agent, logical_destination=logical_dst)
                if move_id:
                    self._traffic_complete_plan(move_id)
                self._log_interrupted_move(agent, reason="battery_depleted", logical_destination=logical_dst)
                self._clear_current_move(agent)
                self._clear_in_transit(agent)
                if not agent.discharged:
                    self.discharge_agent(agent, reason="battery_depleted", interrupt_process=False)
                raise simpy.Interrupt("battery_depleted")

            remaining = self.battery_remaining(agent)
            if not self._battery_interrupt_exempt(agent) and remaining + eps < segment_duration:
                try:
                    yield self.env.timeout(max(eps, remaining))
                finally:
                    grid.release_reservation(agent.agent_id, next_tile)
                    agent.reserved_tile = None
                    self._close_current_move_segment(agent, logical_destination=logical_dst)
                    if move_id:
                        self._traffic_complete_plan(move_id)
                    self._log_interrupted_move(agent, reason="battery_depleted", logical_destination=logical_dst)
                    self._clear_current_move(agent)
                    self._clear_in_transit(agent)
                if not agent.discharged:
                    self.discharge_agent(agent, reason="battery_depleted", interrupt_process=False)
                raise simpy.Interrupt("battery_depleted")

            try:
                yield self.env.timeout(segment_duration)
            except simpy.Interrupt as intr:
                grid.release_reservation(agent.agent_id, next_tile)
                agent.reserved_tile = None
                self._close_current_move_segment(agent, logical_destination=logical_dst)
                if move_id:
                    self._traffic_complete_plan(move_id)
                self._log_interrupted_move(agent, reason=str(intr.cause or "interrupted"), logical_destination=logical_dst)
                self._clear_current_move(agent)
                self._clear_in_transit(agent)
                raise

            if self._traffic_strict_reservation():
                grid.move_worker_to_reserved(agent.agent_id, next_tile)
            else:
                grid.move_worker(agent.agent_id, next_tile)
            agent.tile = next_tile
            agent.reserved_tile = None
            self._traffic_end_segment(
                agent,
                move_id=move_id,
                segment_index=segment_index,
                from_tile=current_tile,
                to_tile=next_tile,
                ended_at=float(self.env.now),
                logical_destination=logical_dst,
            )
            self._finish_shared_transport_segment(
                agent,
                from_tile=current_tile,
                to_tile=next_tile,
                logical_destination=logical_dst,
                segment_duration=segment_duration,
            )
            if self._maybe_item_drop_incident(agent, logical_destination=logical_dst, destination=dst, move_id=move_id):
                agent.current_move_segment_index = 0
                agent.current_move_segment_from_tile = None
                agent.current_move_segment_to_tile = None
                if move_id:
                    self._traffic_complete_plan(move_id)
                self._log_interrupted_move(agent, reason="ITEM_DROPPED", logical_destination=logical_dst)
                self._clear_current_move(agent)
                self._clear_in_transit(agent)
                raise simpy.Interrupt("ITEM_DROPPED")
            agent.current_move_segment_index = 0
            agent.current_move_segment_from_tile = None
            agent.current_move_segment_to_tile = None
            if planned_duration > 1e-9 and planned_path:
                try:
                    current_index = max(0, planned_path.index(next_tile))
                    progress = min(1.0, max(0.0, current_index / max(1, len(planned_path) - 1)))
                except ValueError:
                    progress = min(1.0, float(agent.in_transit_progress) + (segment_duration / max(1e-9, planned_duration)))
                remaining_edges = max(0, (len(planned_path) - 1) - max(0, int(round(progress * max(1, len(planned_path) - 1)))))
                dynamic_total = max(
                    segment_duration,
                    (float(self.env.now) - float(agent.current_move_started_at or self.env.now))
                    + (remaining_edges * sampled_tile_time_min),
                )
                self._set_in_transit(agent, str(agent.location), logical_dst, progress, dynamic_total)

        old_location = str(agent.location)
        agent.location = logical_dst
        for helper in self._shared_transport_followers(agent):
            helper.location = logical_dst
        self._clear_in_transit(agent)
        if movement_started:
            if emit_move_events:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="AGENT_MOVE_END",
                    entity_id=agent.agent_id,
                    location=logical_dst,
                    details={
                            "from": old_location,
                            "to": logical_dst,
                            "from_tile": self._tile_payload(start_tile),
                            "to_tile": self._tile_payload(agent.tile),
                            "carrying_item_type": agent.carrying_item_type,
                            "carrying_item_id": agent.carrying_item_id,
                            "transport_session": self._worker_cargo_payload(agent).get("transport_session_id"),
                            "carrier_ids": self._worker_cargo_payload(agent).get("carrier_ids", []),
                            "move_id": move_id,
                        },
                    )
            else:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="AGENT_RELOCATED",
                    entity_id=agent.agent_id,
                    location=logical_dst,
                    details={
                            "from": old_location,
                            "to": logical_dst,
                            "duration": round(planned_duration, 3),
                            "from_tile": self._tile_payload(start_tile),
                            "to_tile": self._tile_payload(agent.tile),
                            "carrying_item_type": agent.carrying_item_type,
                            "carrying_item_id": agent.carrying_item_id,
                            "transport_session": self._worker_cargo_payload(agent).get("transport_session_id"),
                            "carrier_ids": self._worker_cargo_payload(agent).get("carrier_ids", []),
                            "move_id": move_id,
                        },
                    )
            if move_id:
                self._traffic_complete_plan(move_id)
            self._clear_current_move(agent)
            self.active_movement_timing_sessions.pop(agent.agent_id, None)
        if agent.current_task_type:
            task = Task(
                task_id=str(agent.current_task_id or ""),
                task_type=str(agent.current_task_type),
                priority_key="",
                priority=0.0,
                location=logical_dst,
                payload={},
                task_code=str(agent.current_task_code or ""),
                instance_id=str(agent.current_task_instance_id or ""),
                assigned_robot_id=agent.agent_id,
            )
            self._set_humanoid_for_task(agent, task, reason="arrived_for_task", task_id=agent.current_task_id)
        elif not agent.discharged:
            self._set_humanoid_for_task(agent, None, reason="move_completed")

    def move_agent(self, agent: Agent, dst: str, emit_move_events: bool = True):
        if self.grid_map is not None:
            yield from self._move_agent_grid(agent, dst, emit_move_events=emit_move_events)
            return

        eps = 1e-6

        # If the agent is currently on an edge, first walk to the best endpoint.
        if self._has_in_transit_position(agent):
            edge_from = str(agent.in_transit_from)
            edge_to = str(agent.in_transit_to)
            progress = float(agent.in_transit_progress)
            total = max(1e-6, float(agent.in_transit_total_min))
            via_from = progress * total + self.travel_time(edge_from, dst)
            via_to = (1.0 - progress) * total + self.travel_time(edge_to, dst)
            if via_from <= via_to:
                yield from self._move_on_edge(agent, edge_from, edge_to, progress, 0.0, emit_move_events=emit_move_events)
                agent.location = edge_from
            else:
                yield from self._move_on_edge(agent, edge_from, edge_to, progress, 1.0, emit_move_events=emit_move_events)
                agent.location = edge_to
            self._clear_in_transit(agent)

        src = agent.location
        self._ensure_product_transport_session(agent, destination=dst)
        base_move_t = self.travel_time(src, dst)
        move_t = base_move_t * self._current_transport_time_multiplier(agent)

        # A discharged agent cannot initiate movement.
        if self._should_interrupt_for_battery(agent, eps):
            if not agent.discharged:
                self.discharge_agent(agent, reason="battery_depleted", interrupt_process=False)
            raise simpy.Interrupt("battery_depleted")

        if move_t > 0:
            if emit_move_events:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="AGENT_MOVE_START",
                    entity_id=agent.agent_id,
                    location=src,
                    details={
                        "from": src,
                        "to": dst,
                        "duration": round(move_t, 3),
                        "base_duration": round(base_move_t, 3),
                        "carrying_item_type": agent.carrying_item_type,
                        "carrying_item_id": agent.carrying_item_id,
                        "item_time_multiplier": round(self._item_transport_multiplier(agent.carrying_item_type), 3),
                        "effective_time_multiplier": round(self._current_transport_time_multiplier(agent), 3),
                        "transport_session": self._worker_cargo_payload(agent).get("transport_session_id"),
                        "carrier_ids": self._worker_cargo_payload(agent).get("carrier_ids", []),
                    },
                )
            move_start_t = self.env.now
            self._set_in_transit(agent, src, dst, 0.0, move_t)
            self._set_worker_motion(agent, src, dst, 0.0, move_t)
            try:
                yield self.env.timeout(move_t)
            except simpy.Interrupt as intr:
                elapsed = max(0.0, self.env.now - move_start_t)
                progress = min(1.0, max(0.0, elapsed / max(1e-6, move_t)))
                self._set_in_transit(agent, src, dst, progress, move_t)
                self._set_worker_motion(agent, src, dst, progress, move_t)
                if emit_move_events:
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="AGENT_MOVE_INTERRUPTED",
                        entity_id=agent.agent_id,
                        location=self.agent_display_location(agent),
                        details={
                            "from": src,
                            "to": dst,
                            "duration": round(move_t, 3),
                            "elapsed": round(elapsed, 3),
                            "progress": round(progress, 4),
                            "reason": str(intr.cause),
                        },
                    )
                raise
            # If battery expires exactly at arrival boundary, keep edge location for handover logic.
            if self._should_interrupt_for_battery(agent, eps):
                self._set_in_transit(agent, src, dst, 1.0, move_t)
                if not agent.discharged:
                    self.discharge_agent(agent, reason="battery_depleted", interrupt_process=False)
                raise simpy.Interrupt("battery_depleted")

        agent.location = dst
        self._clear_in_transit(agent)
        if move_t > 0:
            if emit_move_events:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="AGENT_MOVE_END",
                    entity_id=agent.agent_id,
                    location=dst,
                    details={
                        "from": src,
                        "to": dst,
                        "carrying_item_type": agent.carrying_item_type,
                        "carrying_item_id": agent.carrying_item_id,
                        "transport_session": self._worker_cargo_payload(agent).get("transport_session_id"),
                        "carrier_ids": self._worker_cargo_payload(agent).get("carrier_ids", []),
                    },
                )
            else:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="AGENT_RELOCATED",
                    entity_id=agent.agent_id,
                    location=dst,
                    details={"from": src, "to": dst, "duration": round(move_t, 3)},
                )
        if agent.current_task_type:
            task = Task(
                task_id=str(agent.current_task_id or ""),
                task_type=str(agent.current_task_type),
                priority_key="",
                priority=0.0,
                location=dst,
                payload={},
                task_code=str(agent.current_task_code or ""),
                instance_id=str(agent.current_task_instance_id or ""),
                assigned_robot_id=agent.agent_id,
            )
            self._set_humanoid_for_task(agent, task, reason="arrived_for_task", task_id=agent.current_task_id)
        elif not agent.discharged:
            self._set_humanoid_for_task(agent, None, reason="move_completed")

    def execute_task(self, agent: Agent, task: Task):
        runtime = getattr(self, "humanoid_runtime", None)
        if runtime is not None and getattr(runtime, "enabled", False):
            result = yield from runtime.execute(agent, task)
            return result
        result = yield from self._execute_task_domain_action(agent, task)
        return result

    def vacate_completed_task_service_tile(self, agent: Agent, task: Task):
        """Move a task-ending worker off the single-capacity desk approach."""

        if self.grid_map is None or agent.discharged:
            return
        if str(task.task_type or "").strip().upper() not in {
            "LOAD_UNLOAD_TRANSFER_INTERFACE",
            "INSPECT_PRODUCT",
        }:
            return
        desk_tiles = set(self.grid_map.service_tiles.get(self.inspection_workstation_id, []))
        if agent.tile not in desk_tiles:
            return
        staging_tiles = self.grid_map.service_tiles.get("inspection_staging", [])
        if not staging_tiles:
            raise RuntimeError("Inspection workstation has no passable staging tile for worker egress.")
        from_tile = agent.tile
        yield from self.move_agent(agent, "inspection_staging", emit_move_events=True)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="INSPECTION_WORKSTATION_VACATED",
            entity_id=self.inspection_workstation_id,
            location="Inspection",
            details={
                "worker_id": agent.agent_id,
                "task_id": task.task_id,
                "task_code": str(task.task_code or task.task_type or ""),
                "from_tile": self._tile_payload(from_tile),
                "to_tile": self._tile_payload(agent.tile),
            },
        )

    def _dropped_item_recovery_destination(
        self,
        parent_task: Task,
        item_type: str,
        incident_context: dict[str, Any],
        dropped_info: dict[str, Any],
    ) -> str:
        payload = parent_task.payload if isinstance(parent_task.payload, dict) else {}
        destination = str(
            incident_context.get("destination")
            or dropped_info.get("destination")
            or incident_context.get("logical_destination")
            or dropped_info.get("logical_destination")
            or ""
        ).strip()
        if parent_task.task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            action = str(payload.get("interface_action") or payload.get("action") or "").strip().lower()
            if action == "load":
                return str(payload.get("destination") or self.inspection_workstation_id)
            if action == "unload":
                result = str(payload.get("inspection_result") or self.inspection_desk_result or "").strip().upper()
                return str(
                    payload.get("destination")
                    or ("inspection_output_queue" if result == "PASS" else "inspection_scrap_queue")
                )
        if parent_task.task_type == "INSPECT_PRODUCT":
            normalized = self.grid_map.normalize_location(destination) if self.grid_map is not None else destination
            if not normalized or normalized == self.inspection_workstation_id:
                # INSPECT_PRODUCT no longer transports the item. This fallback
                # only protects legacy traces that still report a dropped item.
                return self.inspection_workstation_id
        if destination:
            return destination
        if parent_task.task_type == "TRANSFER":
            transfer_kind = str(payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "material_supply":
                return f"material_queue_{int(payload.get('station', 1) or 1)}"
            if transfer_kind == "inter_station":
                from_station = int(payload.get("from_station", 1) or 1)
                if from_station == self.inspection_queue_station:
                    return "completed_product_buffer"
                to_station = from_station + 1
                return f"intermediate_queue_{to_station if to_station <= self.last_processing_station else self.inspection_queue_station}"
            if transfer_kind == "battery_delivery":
                return str(payload.get("target_agent_id", "") or "")
        if parent_task.task_type in {"LOAD_MACHINE", "SETUP_MACHINE"}:
            return str(payload.get("machine_id", "") or "")
        return str(dropped_info.get("logical_destination") or "")

    def _log_recovered_inspection_unload(
        self,
        agent: Agent,
        task: Task,
        item_id: str,
        result: str,
        destination: str,
    ) -> None:
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="INSPECTION_DESK_ITEM_UNLOADED",
            entity_id=item_id,
            location="Inspection",
            details={
                "worker_id": agent.agent_id,
                "task_id": task.task_id,
                "product_id": item_id,
                "action": "unload",
                "source": self.inspection_workstation_id,
                "destination": destination,
                "workstation": self.inspection_workstation_id,
                "inspection_result": result,
                "source_event": "dropped_item_recovery",
            },
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="ITEM_MOVED",
            entity_id=item_id,
            location="Inspection",
            details={
                "from": self.inspection_workstation_id,
                "to": destination,
                "item_type": "product",
                "source_event": "dropped_item_recovery",
            },
        )

    def _place_recovered_dropped_item(
        self,
        agent: Agent,
        item_id: str,
        item_type: str,
        destination: str,
        parent_task: Task,
    ) -> str:
        normalized = self.grid_map.normalize_location(destination) if self.grid_map is not None else str(destination)
        item_type_norm = str(item_type or "unknown").strip().lower()
        payload = parent_task.payload if isinstance(parent_task.payload, dict) else {}
        interface_action = str(payload.get("interface_action") or payload.get("action") or "").strip().lower()

        if normalized == self.inspection_workstation_id:
            if (
                bool(payload.get("_inspection_interface_placed"))
                and self.inspection_desk_item_id == item_id
                and self.inspection_desk_state == "STAGED_FOR_INSPECTION"
            ):
                return "Inspection"
            if self.inspection_desk_item_id not in {None, item_id}:
                raise RuntimeError(
                    f"Cannot recover {item_id} to occupied inspection desk {self.inspection_desk_item_id}."
                )
            self._set_inspection_desk_state(
                "STAGED_FOR_INSPECTION",
                item_id=item_id,
                reason="dropped_interface_load_recovered",
            )
            self._set_item_state(
                item_id,
                ItemState.STAGED_FOR_INSPECTION,
                location="Inspection",
                ref=self.inspection_workstation_id,
                item_type="product",
            )
            payload["_inspection_interface_placed"] = True
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="INSPECTION_DESK_ITEM_LOADED",
                entity_id=item_id,
                location="Inspection",
                details={
                    "worker_id": agent.agent_id,
                    "task_id": parent_task.task_id,
                    "product_id": item_id,
                    "action": "load",
                    "source": str(payload.get("source") or ""),
                    "destination": self.inspection_workstation_id,
                    "workstation": self.inspection_workstation_id,
                    "source_event": "dropped_item_recovery",
                },
            )
            return "Inspection"

        if normalized.startswith("material_queue_"):
            station = int(normalized.rsplit("_", 1)[-1])
            if not self._push_material_queue(station, item_id, task=parent_task):
                raise RuntimeError(f"Cannot recover {item_id}: material_queue_{station} is full.")
            return f"Station{station}"

        if normalized.startswith("intermediate_queue_"):
            station = int(normalized.rsplit("_", 1)[-1])
            if not self._push_intermediate_queue(station, item_id, task=parent_task):
                raise RuntimeError(f"Cannot recover {item_id}: intermediate_queue_{station} is full.")
            return "Inspection" if station == self.inspection_queue_station else f"Station{station}"

        if normalized.startswith("station_") and normalized.endswith("_output_queue"):
            station_text = normalized.removeprefix("station_").removesuffix("_output_queue")
            try:
                station = int(station_text)
            except ValueError:
                station = 0
            if station in self.output_buffers:
                if not self._push_output_buffer_item(station, item_id, task=parent_task):
                    raise RuntimeError(f"Cannot recover {item_id}: output_buffer_station_{station} is full.")
                output_item_type = "product" if station >= self.last_processing_station else "intermediate"
                self._set_item_state(
                    item_id,
                    ItemState.IN_OUTPUT_BUFFER,
                    location=f"Station{station}",
                    ref=f"output_buffer_station_{station}",
                    item_type=output_item_type,
                )
                return f"Station{station}"

        if normalized == "inspection_output_queue":
            if not self._push_output_buffer_item(
                self.inspection_queue_station,
                item_id,
                task=parent_task,
            ):
                raise RuntimeError(f"Cannot recover {item_id}: inspection output buffer is full.")
            self._set_item_state(
                item_id,
                ItemState.WAITING_INSPECTION_OUTPUT,
                location="Inspection",
                ref="inspection_output_queue",
                item_type="product",
            )
            if interface_action == "unload" and not bool(payload.get("_inspection_interface_placed")):
                payload["_inspection_interface_placed"] = True
                self._set_inspection_desk_state("EMPTY", reason="dropped_interface_unload_recovered")
                self._log_recovered_inspection_unload(agent, parent_task, item_id, "PASS", normalized)
            return "Inspection"

        if normalized == "inspection_scrap_queue":
            if not self._push_inspection_scrap_queue(item_id, task=parent_task):
                raise RuntimeError(f"Cannot recover {item_id}: inspection scrap queue is full.")
            if interface_action == "unload" and not bool(payload.get("_inspection_interface_placed")):
                payload["_inspection_interface_placed"] = True
                self._set_inspection_desk_state("EMPTY", reason="dropped_interface_unload_recovered")
                self._log_recovered_inspection_unload(agent, parent_task, item_id, "FAIL", normalized)
            return "Inspection"

        if normalized == "completed_product_buffer":
            self.product_count += 1
            self._set_item_state(
                item_id,
                ItemState.COMPLETED,
                location="CompletedProducts",
                ref="completed_product_buffer",
                item_type="product",
            )
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="COMPLETED_PRODUCT",
                entity_id=item_id,
                location="CompletedProducts",
                details={"target": "completed_product_buffer", "source": "dropped_item_recovery"},
            )
            self._record_initial_batch_terminal_output(
                item_id,
                outcome="accepted_product",
                completed_by_agent_id=agent.agent_id,
            )
            return "CompletedProducts"

        if normalized == "scrap_disposal_bin":
            self.disposed_scrap_count += 1
            self._set_item_state(item_id, ItemState.SCRAPPED, location="ScrapDisposal", ref="scrap_disposal_bin", item_type=item_type_norm)
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="SCRAP_DISPOSED",
                entity_id="scrap_disposal_bin",
                location="ScrapDisposal",
                details={
                    "item_ids": [item_id],
                    "item_count": 1,
                    "disposed_scrap_count": int(self.disposed_scrap_count),
                    "scrap_transport_batches": int(self.scrap_transport_batches),
                    "source": "dropped_item_recovery",
                    "humanoid_state": self._humanoid_state_payload(agent),
                },
            )
            self._record_initial_batch_terminal_output(
                item_id,
                outcome="disposed_scrap",
                completed_by_agent_id=agent.agent_id,
            )
            return "ScrapDisposal"

        machine = self.machines.get(normalized)
        if machine is not None:
            if item_type_norm == "material" and machine.input_material is None:
                machine.input_material = item_id
            elif item_type_norm in {"intermediate", "product"} and machine.input_intermediate is None:
                machine.input_intermediate = item_id
            self._set_item_state(item_id, ItemState.LOADED_ON_MACHINE, location=f"Station{machine.station}", ref=machine.machine_id, item_type=item_type_norm)
            return f"Station{machine.station}"

        location = self.grid_map.logical_location(normalized) if self.grid_map is not None else str(parent_task.location or "")
        self._set_item_state(item_id, ItemState.IN_QUEUE, location=location, ref=normalized, item_type=item_type_norm)
        return location

    def _execute_dropped_item_recovery_transfer(
        self,
        agent: Agent,
        parent_task: Task,
        recovery_task: Task,
        recovery_context: dict[str, Any],
    ):
        incident_context = recovery_context.get("incident_context") if isinstance(recovery_context.get("incident_context"), dict) else {}
        item_id = str(incident_context.get("item_id") or "")
        if not item_id:
            return False
        dropped_info = self.dropped_items.get(item_id)
        if not isinstance(dropped_info, dict):
            return False
        item_type = str(dropped_info.get("item_type") or incident_context.get("item_type") or "unknown")
        dropped_tile = dropped_info.get("tile")
        if not isinstance(dropped_tile, tuple):
            dropped_tile = self._tile_from_payload(incident_context.get("tile"))
        if dropped_tile is None:
            return False

        destination = self._dropped_item_recovery_destination(parent_task, item_type, incident_context, dropped_info)
        if not destination:
            return False

        if agent.carrying_item_id != item_id:
            self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO", reason="dropped_item_recovery_pickup")
            yield from self.move_agent(agent, item_id, emit_move_events=True)
            if agent.tile != dropped_tile:
                recovery_task.payload["failure_reason"] = "dropped_item_unreachable"
                return False
            self._set_humanoid_primitive_hint(agent, "LOCALIZE_OBJECT", reason="dropped_item_recovery_localize")
            self._set_humanoid_primitive_hint(agent, "PRIMITIVE_IDENTIFY_ITEM", reason="dropped_item_recovery_identify")
            self._set_humanoid_primitive_hint(agent, "GRASP", reason="dropped_item_recovery_pickup")
            if not self._set_agent_carrying(agent, item_type, item_id):
                recovery_task.payload["failure_reason"] = "dropped_item_pickup_failed"
                return False
            self._clear_dropped_item(item_id)
            self._set_humanoid_primitive_hint(agent, "LIFT", reason="dropped_item_recovery_pickup")

        self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO", reason="dropped_item_recovery_dropoff")
        yield from self.move_agent(agent, destination, emit_move_events=True)
        if not self._confirm_object_service_tile(agent, destination, recovery_task, "dropped_item_recovery_dropoff"):
            recovery_task.payload["failure_reason"] = "dropped_item_destination_unreachable"
            return False
        yield from self._dock_agent_at_target(agent, recovery_task, reason="dropped_item_recovery_dropoff_alignment")
        self._set_humanoid_primitive_hint(agent, "PLACE", reason="dropped_item_recovery_dropoff")
        placed_location = self._place_recovered_dropped_item(agent, item_id, item_type, destination, parent_task)
        self._clear_agent_carrying(agent, destination=placed_location)
        self._set_humanoid_primitive_hint(agent, "RELEASE", reason="dropped_item_recovery_dropoff")
        for key in (
            "transfer_item_id",
            "transfer_intermediate_id",
            "material_id",
            "intermediate_id",
            "scrap_item_id",
        ):
            if str(parent_task.payload.get(key, "")) == item_id:
                parent_task.payload.pop(key, None)
        parent_task.payload["dropped_item_recovered"] = True
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="DROPPED_ITEM_RECOVERED",
            entity_id=item_id,
            location=placed_location,
            details={
                "by": agent.agent_id,
                "item_id": item_id,
                "item_type": item_type,
                "from_tile": self._tile_payload(dropped_tile),
                "to": destination,
                "recovery_id": recovery_context.get("recovery_id", ""),
                "parent_task_id": parent_task.task_id,
            },
        )
        return True

    def _execute_inspection_task_primitive(self, agent: Agent, task: Task, step: dict[str, Any]):
        """Apply inspection interface and quality state changes at primitive boundaries."""

        task_code = str(task.task_code or task.task_type or "").strip().upper()
        call_code = str(step.get("call_code", "") or "").strip().upper()
        step_id = str(step.get("step_id", "") or "").strip().lower()
        payload = task.payload if isinstance(task.payload, dict) else {}
        product_id = str(payload.get("inspection_product_id") or payload.get("item_id") or "").strip()

        if task_code == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            action = str(payload.get("interface_action") or payload.get("action") or "").strip().lower()
            if action not in {"load", "unload"} or not product_id:
                payload["failure_reason"] = "invalid_inspection_interface_args"
                return False

            if call_code == "NAVIGATE_TO":
                target = str(payload.get("source") if step_id == "s01_navigate_to" else payload.get("destination") or "")
                if not target:
                    payload["failure_reason"] = "missing_interface_location"
                    return False
                yield from self.move_agent(agent, target, emit_move_events=True)
                if not self._confirm_object_service_tile(agent, target, task, f"inspection_interface_{action}"):
                    return False
                yield from self._dock_agent_at_target(agent, task, reason=f"inspection_interface_{action}_alignment")
                return True

            if call_code in {"LOCALIZE_OBJECT", "PRIMITIVE_IDENTIFY_ITEM"}:
                if self._maybe_random_humanoid_step_incident(agent, task, step, call_code):
                    return False
                if action == "load":
                    return product_id in self.intermediate_queues[self.inspection_queue_station]
                return (
                    self.inspection_desk_item_id == product_id
                    and self.inspection_desk_state == "INSPECTED_WAITING_UNLOAD"
                    and self.inspection_desk_result in {"PASS", "FAIL"}
                )

            if call_code == "GRASP":
                if action == "load":
                    if self.inspection_desk_item_id is not None or self.inspection_desk_state != "EMPTY":
                        payload["failure_reason"] = "inspection_desk_not_empty"
                        return False
                    popped = self._pop_intermediate_queue(self.inspection_queue_station, product_id)
                    if popped != product_id:
                        payload["failure_reason"] = "RESOURCE_PREEMPTED"
                        return False
                else:
                    if (
                        self.inspection_desk_item_id != product_id
                        or self.inspection_desk_state != "INSPECTED_WAITING_UNLOAD"
                        or self.inspection_desk_result not in {"PASS", "FAIL"}
                    ):
                        payload["failure_reason"] = "inspection_result_not_ready"
                        return False
                    payload["inspection_result"] = str(self.inspection_desk_result)
                if not self._set_agent_carrying(agent, "product", product_id):
                    if action == "load":
                        self._appendleft_if_absent(self.intermediate_queues[self.inspection_queue_station], product_id)
                        self._set_item_state(
                            product_id,
                            ItemState.WAITING_INSPECTION,
                            location="Inspection",
                            ref=f"intermediate_queue_{self.inspection_queue_station}",
                            item_type="product",
                        )
                    return False
                if action == "unload":
                    self._set_item_state(
                        product_id,
                        ItemState.CARRIED_BY_WORKER,
                        location="Inspection",
                        ref=agent.agent_id,
                        item_type="product",
                    )
                return True

            if call_code == "PLACE":
                if agent.carrying_item_id != product_id:
                    payload["failure_reason"] = "inspection_interface_item_not_carried"
                    return False
                if action == "load":
                    self._set_inspection_desk_state(
                        "STAGED_FOR_INSPECTION",
                        item_id=product_id,
                        reason="inspection_item_loaded",
                    )
                    self._set_item_state(
                        product_id,
                        ItemState.STAGED_FOR_INSPECTION,
                        location="Inspection",
                        ref=self.inspection_workstation_id,
                        item_type="product",
                    )
                    event_type = "INSPECTION_DESK_ITEM_LOADED"
                else:
                    result = str(payload.get("inspection_result") or "").strip().upper()
                    destination = "inspection_output_queue" if result == "PASS" else "inspection_scrap_queue"
                    if result == "PASS":
                        if not self._push_output_buffer_item(
                            self.inspection_queue_station,
                            product_id,
                            task=task,
                        ):
                            payload["failure_reason"] = "inspection_output_buffer_full"
                            return False
                        self._set_item_state(
                            product_id,
                            ItemState.WAITING_INSPECTION_OUTPUT,
                            location="Inspection",
                            ref="inspection_output_queue",
                            item_type="product",
                        )
                    elif result == "FAIL":
                        if not self._push_inspection_scrap_queue(product_id, task=task):
                            payload["failure_reason"] = "inspection_scrap_buffer_full"
                            return False
                    else:
                        payload["failure_reason"] = "missing_inspection_result"
                        return False
                    payload["destination"] = destination
                    self._set_inspection_desk_state("EMPTY", reason="inspection_item_unloaded")
                    event_type = "INSPECTION_DESK_ITEM_UNLOADED"
                payload["_inspection_interface_placed"] = True
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type=event_type,
                    entity_id=product_id,
                    location="Inspection",
                    details={
                        "worker_id": agent.agent_id,
                        "task_id": task.task_id,
                        "product_id": product_id,
                        "action": action,
                        "source": str(payload.get("source") or ""),
                        "destination": str(payload.get("destination") or ""),
                        "workstation": self.inspection_workstation_id,
                        "inspection_result": str(payload.get("inspection_result") or ""),
                    },
                )
                return True

            if call_code == "RELEASE":
                if not payload.get("_inspection_interface_placed"):
                    return False
                destination = str(payload.get("destination") or "")
                self._clear_agent_carrying(agent, destination=destination)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ITEM_MOVED",
                    entity_id=product_id,
                    location="Inspection",
                    details={
                        "from": str(payload.get("source") or ""),
                        "to": destination,
                        "item_type": "product",
                        "worker_id": agent.agent_id,
                        "task_id": task.task_id,
                        "interface_action": action,
                    },
                )
                return True

            if call_code == "VERIFY_PLACEMENT":
                if action == "load":
                    return (
                        self.inspection_desk_item_id == product_id
                        and self.inspection_desk_state == "STAGED_FOR_INSPECTION"
                    )
                destination = str(payload.get("destination") or "")
                if destination == "inspection_output_queue":
                    return product_id in self.output_buffers[self.inspection_queue_station]
                return product_id in self.inspection_scrap_queue
            return True

        if task_code != "INSPECT_PRODUCT" or not product_id:
            return False

        if call_code == "NAVIGATE_TO":
            yield from self.move_agent(agent, self.inspection_workstation_id, emit_move_events=True)
            if not self._confirm_object_service_tile(agent, self.inspection_workstation_id, task, "inspect_product_desk"):
                return False
            yield from self._dock_agent_at_target(agent, task, reason="inspection_desk_alignment")
            return True

        if call_code in {"PRIMITIVE_IDENTIFY_ITEM", "LOCALIZE_OBJECT"}:
            if self._maybe_random_humanoid_step_incident(agent, task, step, call_code):
                return False
            return (
                self.inspection_desk_item_id == product_id
                and self.inspection_desk_state in {"STAGED_FOR_INSPECTION", "INSPECTING"}
            )

        if call_code == "EXECUTE_QUALITY_ACTION":
            if self.inspection_desk_item_id != product_id or self.inspection_desk_state not in {
                "STAGED_FOR_INSPECTION",
                "INSPECTING",
            }:
                payload["failure_reason"] = "inspection_item_not_staged"
                return False
            if self.inspection_desk_state == "STAGED_FOR_INSPECTION":
                self.inspection_active_agents += 1
                self._set_inspection_desk_state("INSPECTING", item_id=product_id, reason="inspection_started")
                self._set_item_state(
                    product_id,
                    ItemState.INSPECTING,
                    location="Inspection",
                    ref=self.inspection_workstation_id,
                    item_type="product",
                )
                for event_type in ("INSPECTION_DESK_OCCUPIED", "INSPECTION_STARTED"):
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type=event_type,
                        entity_id=self.inspection_workstation_id if event_type == "INSPECTION_DESK_OCCUPIED" else product_id,
                        location="Inspection",
                        details={
                            "worker_id": agent.agent_id,
                            "task_id": task.task_id,
                            "product_id": product_id,
                            "active_worker_ids": [agent.agent_id],
                            "workstation": self.inspection_workstation_id,
                        },
                    )
            return True

        if call_code == "CLASSIFY_RESULT":
            item = self.items.get(product_id)
            if item is None or self.inspection_desk_state != "INSPECTING":
                return False
            stored_result = str(item.metadata.get("inspection_result") or "").strip().upper()
            if stored_result not in {"PASS", "FAIL"}:
                stored_result = "FAIL" if self.quality_rng.random() < float(self.quality_cfg["defect_prob"]) else "PASS"
                item.metadata["inspection_result"] = stored_result
                item.metadata["inspection_classified_at"] = round(float(self.env.now), 6)
            self.inspection_desk_result = stored_result
            payload["inspection_result"] = stored_result
            return True

        if call_code == "RECORD_RESULT":
            item = self.items.get(product_id)
            result = str(
                self.inspection_desk_result
                or (item.metadata.get("inspection_result") if item is not None else "")
                or ""
            ).strip().upper()
            if item is None or result not in {"PASS", "FAIL"}:
                return False
            self._set_inspection_desk_state(
                "INSPECTED_WAITING_UNLOAD",
                item_id=product_id,
                result=result,
                reason="inspection_result_recorded",
            )
            self._set_item_state(
                product_id,
                ItemState.INSPECTED_WAITING_UNLOAD,
                location="Inspection",
                ref=self.inspection_workstation_id,
                item_type="product",
            )
            if not bool(item.metadata.get("inspection_result_recorded", False)):
                item.metadata["inspection_result_recorded"] = True
                item.metadata["inspection_recorded_at"] = round(float(self.env.now), 6)
                if result == "FAIL":
                    self.scrap_count += 1
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="INSPECTION_RESULT_RECORDED",
                    entity_id=product_id,
                    location="Inspection",
                    details={
                        "worker_id": agent.agent_id,
                        "task_id": task.task_id,
                        "product_id": product_id,
                        "inspection_result": result,
                        "workstation": self.inspection_workstation_id,
                    },
                )
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="INSPECT_PASS" if result == "PASS" else "INSPECT_FAIL",
                    entity_id=product_id,
                    location=self.inspection_workstation_id,
                    details={"inspector": agent.agent_id},
                )
                if result == "FAIL":
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="SCRAP",
                        entity_id=product_id,
                        location=self.inspection_workstation_id,
                        details={"queue_id": "inspection_scrap_queue", "status": "awaiting_unload"},
                    )
            return True
        return True

    def _execute_inspection_task_without_runtime(self, agent: Agent, task: Task):
        """Execute the same primitive-boundary contract when HumanoidSim logging is disabled."""

        task_code = str(task.task_code or task.task_type or "").strip().upper()
        task.task_code = task_code
        if task_code == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            primitive_steps = (
                ("s01_navigate_to", "NAVIGATE_TO"),
                ("s02_localize_object", "LOCALIZE_OBJECT"),
                ("s03_reach_to", "REACH_TO"),
                ("s04_grasp", "GRASP"),
                ("s05_lift", "LIFT"),
                ("s06_navigate_to_2", "NAVIGATE_TO"),
                ("s07_place", "PLACE"),
                ("s08_release", "RELEASE"),
                ("s09_verify_placement", "VERIFY_PLACEMENT"),
            )
        elif task_code == "INSPECT_PRODUCT":
            primitive_steps = (
                ("s01_navigate_to_workstation", "NAVIGATE_TO"),
                ("s02_primitive_identify_item", "PRIMITIVE_IDENTIFY_ITEM"),
                ("s03_localize_object", "LOCALIZE_OBJECT"),
                ("s04_execute_quality_action", "EXECUTE_QUALITY_ACTION"),
                ("s05_classify_result", "CLASSIFY_RESULT"),
                ("s06_record_result", "RECORD_RESULT"),
            )
        else:
            return False

        for step_id, call_code in primitive_steps:
            step = {
                "path": f"{task_code}/{step_id}",
                "step_id": step_id,
                "call_code": call_code,
                "call_level": "PRIMITIVE_SKILL",
            }
            completed = bool((yield from self._execute_inspection_task_primitive(agent, task, step)))
            if not completed:
                return False
            if call_code != "NAVIGATE_TO":
                duration = float(self.resolve_primitive_duration(task, step) or 0.0)
                if duration > 1e-9:
                    yield self.env.timeout(duration)
        return True

    def _execute_task_domain_action(self, agent: Agent, task: Task):
        task_type = task.task_type

        if task_type in {"LOAD_MACHINE", "UNLOAD_MACHINE", "SETUP_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            machine = self.machines[task.payload["machine_id"]]
            # Broken machines are strictly limited to REPAIR_MACHINE only.
            if machine.broken:
                task.payload["failure_reason"] = "machine_broken"
                return False

        if task_type in {"LOAD_UNLOAD_TRANSFER_INTERFACE", "INSPECT_PRODUCT"}:
            return (yield from self._execute_inspection_task_without_runtime(agent, task))

        if task_type == "HANDOVER_ITEM":
            if str(task.payload.get("handover_kind", "")) != "product_collaboration_join":
                return False
            session_id = str(task.payload.get("transport_session_id", ""))
            session = self.product_transport_sessions.get(session_id)
            if not isinstance(session, dict) or str(session.get("status", "active")) != "active":
                return False
            source_agent = self.agents.get(str(task.payload.get("source_agent_id", "")))
            if source_agent is None or source_agent.worker_id == agent.worker_id:
                return False
            if agent.carrying_item_id is not None and agent.carrying_item_id != str(session.get("item_id", "")):
                return False
            if agent.worker_id in session.get("carrier_ids", []):
                return True
            if len(session.get("carrier_ids", [])) >= int(session.get("max_carriers", self.product_collaboration_max_carriers) or self.product_collaboration_max_carriers):
                return False
            if not self._product_session_join_feasible(agent, session):
                return False
            self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
            yield from self._move_agent_to_in_transit_position(agent, source_agent, emit_move_events=True)
            session = self.product_transport_sessions.get(session_id)
            if not isinstance(session, dict) or str(session.get("status", "active")) != "active":
                return False
            source_agent = self.agents.get(str(task.payload.get("source_agent_id", "")))
            if source_agent is None or source_agent.carrying_item_id != str(session.get("item_id", "")):
                return False
            if not self._product_session_has_remaining_path(session):
                return False
            self._set_humanoid_primitive_hint(agent, "ANNOUNCE_INTENT")
            self._set_humanoid_primitive_hint(agent, "SYNC_WITH_ROBOT")
            self._set_humanoid_primitive_hint(agent, "EXECUTE_ROBOT_COLLABORATION_ACTION")
            if not self._join_product_transport_session(agent, session_id, task=task):
                return False
            done_event = session.get("done_event")
            if done_event is not None and hasattr(done_event, "triggered") and not done_event.triggered:
                try:
                    yield done_event
                finally:
                    if str(session.get("status", "active")) == "active":
                        self._leave_product_transport_session(agent, reason="handover_task_interrupted")
            self._set_humanoid_primitive_hint(agent, "CONFIRM_OPERATOR_STATE")
            self._set_humanoid_primitive_hint(agent, "LOG_RESULT")
            return True

        if task_type == "BATTERY_CHARGE":
            dock_id = str(task.payload.get("charging_dock_id") or self._assigned_charging_dock(agent))
            if dock_id != self._assigned_charging_dock(agent):
                task.payload["failure_reason"] = "wrong_charging_dock"
                return False
            if agent.battery_service_owner is not None and agent.battery_service_owner != agent.agent_id:
                return False
            agent.battery_service_owner = agent.agent_id
            completed = False
            charge_started_at: float | None = None
            try:
                self._set_humanoid_primitive_hint(agent, "CHECK_CONTEXT")
                self._set_humanoid_primitive_hint(agent, "EXECUTE_SYSTEM_ACTION")
                yield from self.move_agent(agent, dock_id, emit_move_events=True)
                if not self._confirm_object_service_tile(agent, dock_id, task, "battery_charge"):
                    task.payload["failure_reason"] = "charging_dock_not_reached"
                    return False
                yield from self._dock_agent_at_target(agent, task, reason="charging_dock_alignment")

                current_budget = max(0.0, float(self.battery_remaining(agent)))
                target_budget = min(
                    float(self.battery_swap_period_min),
                    float(self.battery_swap_period_min) * float(task.payload.get("target_soc", self.battery_charge_target_soc)),
                )
                missing_ratio = max(
                    0.0,
                    (target_budget - current_budget) / max(1e-9, float(self.battery_swap_period_min)),
                )
                sampled_full_charge_min = max(
                    0.0,
                    self.active_primitive_duration(task, self.timing.expected_call_duration("MANAGE_ROBOT_POWER", "EXECUTE_SYSTEM_ACTION")),
                )
                charge_duration = sampled_full_charge_min * missing_ratio
                task.payload.update(
                    {
                        "sampled_full_charge_min": sampled_full_charge_min,
                        "missing_soc_ratio": missing_ratio,
                        "charge_duration_min": charge_duration,
                    }
                )
                agent.charging_started_at = float(self.env.now)
                charge_started_at = float(self.env.now)
                agent.charging_start_budget_min = current_budget
                agent.charging_target_budget_min = target_budget
                agent.charging_duration_min = charge_duration
                agent.charging_dock_id = dock_id
                agent.discharged = False
                agent.discharged_since = None
                self._transition_humanoid_state(
                    agent,
                    "power_charging",
                    task=task,
                    reason="dock_charge",
                    source="mansim.power",
                )
                distribution = self.timing.distribution_for_step(
                    "MANAGE_ROBOT_POWER", "MANAGE_ROBOT_POWER/s02_execute_system_action"
                ).to_dict()
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="BATTERY_CHARGE_STARTED",
                    entity_id=agent.agent_id,
                    location=dock_id,
                    details={
                        "task_id": task.task_id,
                        "charging_dock_id": dock_id,
                        "start_soc": round(current_budget / self.battery_swap_period_min, 6),
                        "target_soc": round(target_budget / self.battery_swap_period_min, 6),
                        "sampled_full_charge_min": round(sampled_full_charge_min, 6),
                        "charge_duration_min": round(charge_duration, 6),
                        "distribution": distribution,
                    },
                )
                if charge_duration > 0.0:
                    yield self.env.timeout(charge_duration)
                self._update_battery_accounting(agent)
                agent.battery_remaining_budget_min = target_budget
                agent.battery_last_accounted_at = float(self.env.now)
                agent.battery_accounting_swap_at = float(self.env.now)
                agent.last_battery_swap = float(self.env.now)
                agent.low_battery_alerted = False
                completed = True
                details = {
                    "task_id": task.task_id,
                    "charging_dock_id": dock_id,
                    "target_soc": round(target_budget / self.battery_swap_period_min, 6),
                    "charge_duration_min": round(charge_duration, 6),
                }
                for event_type in ("BATTERY_CHARGE_COMPLETED", "AGENT_RECHARGED"):
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type=event_type,
                        entity_id=agent.agent_id,
                        location=dock_id,
                        details=details,
                    )
                task.payload["_battery_charge_completed"] = True
                task.payload["_domain_action_duration_complete"] = True
                self._set_humanoid_primitive_hint(agent, "VERIFY_ROBOT_STATE")
                return True
            finally:
                if charge_started_at is not None:
                    if not completed:
                        self._close_interrupted_direct_charge(
                            agent,
                            task_id=task.task_id,
                            reason="task_interrupted",
                        )
                        task.payload["_battery_charge_interrupted"] = True
                    else:
                        agent.charging_started_at = None
                        agent.charging_start_budget_min = None
                        agent.charging_target_budget_min = None
                        agent.charging_duration_min = 0.0
                        agent.charging_dock_id = None
                        self._sync_humanoid_power_state(agent)
                if agent.battery_service_owner == agent.agent_id:
                    agent.battery_service_owner = None

        if task_type == "BATTERY_SWAP":
            if agent.battery_service_owner is not None and agent.battery_service_owner != agent.agent_id:
                return False
            agent.battery_service_owner = agent.agent_id
            try:
                if agent.discharged:
                    return False
                self._set_humanoid_primitive_hint(agent, "CHECK_CONTEXT")
                self._set_humanoid_primitive_hint(agent, "EXECUTE_SYSTEM_ACTION")
                yield from self.move_agent(agent, "battery_rack", emit_move_events=True)
                if not self._confirm_object_service_tile(agent, "battery_rack", task, "battery_swap"):
                    return False
                yield from self._dock_agent_at_target(agent, task, reason="battery_rack_alignment")
                self._transition_humanoid_state(
                    agent,
                    "power_charging",
                    task=task,
                    reason="battery_swap",
                    source="mansim.power",
                )
                yield self.env.timeout(self.active_primitive_duration(task, self.agent_cfg["battery_pickup_time_min"]))
                battery_item_id = str(task.payload.get("battery_item_id", ""))
                if not battery_item_id:
                    battery_item_id = self._next_item_id("BAT")
                    task.payload["battery_item_id"] = battery_item_id
                if not self._set_agent_carrying(agent, "battery", battery_item_id):
                    return False
                self._reset_worker_battery(agent)
                agent.discharged = False
                agent.discharged_since = None
                agent.low_battery_alerted = False
                self._transition_humanoid_state(
                    agent,
                    "power_charging",
                    task=task,
                    reason="battery_recharged",
                    source="mansim.power",
                )
                self._clear_agent_carrying(agent, destination="BatteryStation")
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="BATTERY_SWAP",
                    entity_id=agent.agent_id,
                    location="BatteryStation",
                    details={"target_agent_id": agent.agent_id},
                )
                task.payload.pop("battery_item_id", None)
                self._set_humanoid_primitive_hint(agent, "VERIFY_ROBOT_STATE")
                return True
            finally:
                if agent.battery_service_owner == agent.agent_id:
                    agent.battery_service_owner = None

        if task_type == "REPAIR_MACHINE":
            machine = self.machines[task.payload["machine_id"]]
            if not machine.broken:
                task.payload["failure_reason"] = "machine_already_repaired"
                return False
            self._set_humanoid_primitive_hint(agent, "CHECK_SAFETY_ZONE")
            yield from self.move_agent(agent, machine.machine_id, emit_move_events=True)
            if not self._confirm_object_service_tile(agent, machine.machine_id, task, "repair_machine"):
                return False
            yield from self._dock_agent_at_target(agent, task, reason="repair_machine_alignment")
            self._set_humanoid_primitive_hint(agent, "INSPECT_OR_DIAGNOSE")
            self._transition_humanoid_state(
                agent,
                "task_started",
                task=task,
                status="running",
                reason="repair_machine",
                source="mansim.maintenance",
            )
            try:
                if not machine.broken:
                    task.payload["failure_reason"] = "machine_already_repaired"
                    return False
                if not self._join_repair_team(machine, agent.agent_id):
                    return False
                self._set_humanoid_primitive_hint(agent, "EXECUTE_MAINTENANCE_ACTION")
                task.payload["repair_team_size"] = self._repair_team_size(machine)
                task.payload["repair_slots_remaining"] = self._repair_slots_remaining(machine)
                task.payload["repair_remaining_min"] = round(float(machine.repair_work_remaining_min), 3)
                task.payload["repair_anchor"] = self._repair_worker_anchor(machine, agent.agent_id)
                done_event = self._ensure_repair_done_event(machine)
                yield done_event
                self._set_humanoid_primitive_hint(agent, "VERIFY_MACHINE_STATE")
                return True
            finally:
                if machine.broken:
                    self._leave_repair_team(machine, agent.agent_id, reason="task_cancelled")

        if task_type == "UNLOAD_MACHINE":
            machine = self.machines[task.payload["machine_id"]]
            if machine.unload_owner is not None and machine.unload_owner != agent.agent_id:
                task.payload["failure_reason"] = "machine_unload_reserved"
                return False
            machine.unload_owner = agent.agent_id
            try:
                output_id = str(task.payload.get("unloaded_output_id", "") or "")
                carrying_unloaded_output = bool(output_id and agent.carrying_item_id == output_id)
                if machine.broken:
                    task.payload["failure_reason"] = "machine_broken"
                    return False
                if machine.output_intermediate is None and not carrying_unloaded_output:
                    task.payload["failure_reason"] = "missing_machine_output"
                    return False

                if not carrying_unloaded_output:
                    self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                    yield from self.move_agent(agent, machine.machine_id, emit_move_events=True)
                    if not self._confirm_object_service_tile(agent, machine.machine_id, task, "unload_machine"):
                        return False
                    yield from self._dock_agent_at_target(agent, task, reason="unload_machine_alignment")
                    self._set_humanoid_primitive_hint(agent, "READ_MACHINE_STATE")
                    self._transition_humanoid_state(
                        agent,
                        "task_started",
                        task=task,
                        status="running",
                        reason="unload_machine",
                        source="mansim.machine",
                    )
                    if machine.broken:
                        task.payload["failure_reason"] = "machine_broken"
                        return False
                    self._set_humanoid_primitive_hint(agent, "EXECUTE_MACHINE_ACTION")
                    yield self.env.timeout(self.active_primitive_duration(task, self.movement_cfg["unload_min"]))
                    if machine.broken:
                        task.payload["failure_reason"] = "machine_broken"
                        return False
                    output_id = str(machine.output_intermediate or "")
                    carried_kind = "product" if machine.station == self.last_processing_station else "intermediate"
                    if not self._set_agent_carrying(agent, carried_kind, output_id):
                        return False
                    task.payload["unloaded_output_id"] = output_id
                    machine.output_intermediate = None
                    self._set_machine_state(machine, MachineState.WAIT_INPUT if not machine.broken else MachineState.BROKEN, reason="unloaded")
                    self._set_humanoid_primitive_hint(agent, "VERIFY_MACHINE_STATE")

                if output_id:
                    output_buffer_id = f"output_buffer_station_{machine.station}"
                    self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                    yield from self.move_agent(agent, output_buffer_id, emit_move_events=True)
                    if not self._confirm_object_service_tile(agent, output_buffer_id, task, "unload_output_dropoff"):
                        return False
                    yield from self._dock_agent_at_target(agent, task, reason="unload_output_alignment")
                    self._set_humanoid_primitive_hint(agent, "PLACE")
                    if not self._push_output_buffer_item(machine.station, output_id, task=task):
                        machine.output_intermediate = output_id
                        self._set_machine_state(machine, MachineState.DONE_WAIT_UNLOAD, reason="output_buffer_full")
                        self._clear_agent_carrying(agent, destination=machine.machine_id)
                        task.payload["failure_reason"] = "destination_buffer_full"
                        return False
                    output_item_type = "product" if machine.station == self.last_processing_station else "intermediate"
                    self._set_item_state(
                        output_id,
                        ItemState.IN_OUTPUT_BUFFER,
                        location=f"Station{machine.station}",
                        ref=f"output_buffer_station_{machine.station}",
                        item_type=output_item_type,
                    )
                    self._clear_agent_carrying(agent, destination=f"output_buffer_station_{machine.station}")
                    self._set_humanoid_primitive_hint(agent, "RELEASE")
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="ITEM_MOVED",
                        entity_id=output_id,
                        location=f"Station{machine.station}",
                        details={"from": machine.machine_id, "to": f"output_buffer_station_{machine.station}"},
                    )
                    task.payload.pop("unloaded_output_id", None)
                return True
            finally:
                if machine.unload_owner == agent.agent_id:
                    machine.unload_owner = None

        if task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).lower()

            if transfer_kind == "battery_delivery":
                target_id = task.payload["target_agent_id"]
                target_agent = self.agents[target_id]
                if target_agent.battery_service_owner is not None and target_agent.battery_service_owner != agent.agent_id:
                    return False
                if agent.discharged:
                    return False
                if self.active_battery_delivery_owner is not None and self.active_battery_delivery_owner != agent.agent_id:
                    return False
                self.active_battery_delivery_owner = agent.agent_id
                target_agent.battery_service_owner = agent.agent_id
                try:
                    was_discharged = target_agent.discharged
                    battery_item_id = str(task.payload.get("transfer_item_id", ""))
                    battery_loaded = bool(task.payload.get("battery_loaded", False))
                    if not battery_loaded:
                        self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                        yield from self.move_agent(agent, "battery_rack", emit_move_events=True)
                        if not self._confirm_object_service_tile(agent, "battery_rack", task, "battery_delivery_pickup"):
                            return False
                        self._set_humanoid_primitive_hint(agent, "GRASP")
                        self._transition_humanoid_state(
                            agent,
                            "task_started",
                            task=task,
                            status="running",
                            reason="battery_delivery_pickup",
                            source="mansim.power",
                        )
                        yield self.env.timeout(self.active_primitive_duration(task) * 0.5)
                        if not battery_item_id:
                            battery_item_id = self._next_item_id("BAT")
                            task.payload["transfer_item_id"] = battery_item_id
                        if not self._set_agent_carrying(agent, "battery_fresh", battery_item_id):
                            return False
                        self._set_humanoid_primitive_hint(agent, "LIFT")
                        task.payload["battery_loaded"] = True
                    elif agent.carrying_item_type != "battery_fresh":
                        if not battery_item_id:
                            battery_item_id = self._next_item_id("BAT")
                            task.payload["transfer_item_id"] = battery_item_id
                        if not self._set_agent_carrying(agent, "battery_fresh", battery_item_id):
                            return False
                        self._set_humanoid_primitive_hint(agent, "LIFT")

                    self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                    handover_location = yield from self._move_agent_to_in_transit_position(
                        agent,
                        target_agent,
                        emit_move_events=True,
                        meet_destination=self._has_in_transit_position(target_agent),
                    )
                    if handover_location is None:
                        return False

                    if self._has_in_transit_position(target_agent):
                        handover_location = yield from self._wait_for_agent_at_battery_handover_destination(
                            agent,
                            target_agent,
                            str(handover_location),
                        )
                        if handover_location is None:
                            return False
                    elif agent.location != target_agent.location:
                        self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                        yield from self.move_agent(agent, target_agent.location, emit_move_events=True)
                        if agent.location != target_agent.location:
                            return False
                        handover_location = str(target_agent.location)

                    handover_location = yield from self._ensure_battery_handover_contact(
                        agent,
                        target_agent,
                        emit_move_events=True,
                    )
                    if handover_location is None:
                        return False

                    if not target_agent.discharged and target_agent.awaiting_battery_from is None:
                        self._start_battery_swap_wait(target_agent, agent.agent_id)
                        if target_agent.process_ref is not None and target_agent.process_ref.is_alive:
                            target_agent.process_ref.interrupt("battery_swap_wait")

                    # The battery exception is intentionally scoped to the
                    # physical handoff only. Navigation to the receiver and the
                    # spent-battery return trip must still obey normal depletion.
                    agent.battery_swap_critical = True
                    target_agent.battery_swap_critical = True
                    try:
                        yield self.env.timeout(self.active_primitive_duration(task) * 0.5)
                    finally:
                        agent.battery_swap_critical = False
                        target_agent.battery_swap_critical = False

                    handover_location = yield from self._ensure_battery_handover_contact(
                        agent,
                        target_agent,
                        emit_move_events=True,
                    )
                    if handover_location is None:
                        return False

                    became_discharged_during_delivery = target_agent.discharged
                    self._set_humanoid_primitive_hint(agent, "PLACE")
                    self._reset_worker_battery(target_agent)
                    target_agent.discharged = False
                    target_agent.discharged_since = None
                    target_agent.low_battery_alerted = False
                    self._transition_humanoid_state(
                        target_agent,
                        "task_completed",
                        reason="battery_delivered",
                        source="mansim.power",
                        metadata={"cargo_present": False},
                    )
                    self._clear_agent_carrying(agent, destination=handover_location)
                    self._set_humanoid_primitive_hint(agent, "RELEASE")
                    spent_battery_id = str(task.payload.get("spent_battery_item_id", ""))
                    if not spent_battery_id:
                        spent_battery_id = self._next_item_id("BAT-USED")
                        task.payload["spent_battery_item_id"] = spent_battery_id
                    if not self._set_agent_carrying(agent, "battery_spent", spent_battery_id):
                        return False
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="BATTERY_SWAP",
                        entity_id=target_agent.agent_id,
                        location=handover_location,
                        details={"target_agent_id": target_agent.agent_id, "by": agent.agent_id},
                    )
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="BATTERY_DELIVERED",
                        entity_id=agent.agent_id,
                        location=handover_location,
                        details={"target_agent_id": target_id},
                    )
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="AGENT_RECHARGED",
                        entity_id=target_agent.agent_id,
                        location=handover_location,
                        details={"by": agent.agent_id, "was_discharged": bool(was_discharged or became_discharged_during_delivery)},
                    )
                    # The receiver only pauses for the physical handoff. Returning
                    # the spent battery is the provider's follow-up work and must
                    # not keep the receiver blocked from resuming its suspended task.
                    if target_agent.awaiting_battery_from == agent.agent_id:
                        self._end_battery_swap_wait(target_agent, agent.agent_id)
                    if target_agent.suspended_task is not None and target_agent.suspended_task.task_type == "BATTERY_SWAP":
                        target_agent.suspended_task = None
                    # The atomic battery-swap exception only covers the physical
                    # handoff. The provider's spent-battery return trip should obey
                    # normal depletion rules; otherwise replay can show a worker
                    # moving with zero battery.
                    agent.battery_swap_critical = False
                    target_agent.battery_swap_critical = False
                    self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                    yield from self.move_agent(agent, "battery_rack", emit_move_events=True)
                    if not self._confirm_object_service_tile(agent, "battery_rack", task, "battery_delivery_return"):
                        return False
                    self._clear_agent_carrying(agent, destination="BatteryStation")
                    self._set_humanoid_primitive_hint(agent, "VERIFY_PLACEMENT")
                    task.payload.pop("battery_loaded", None)
                    task.payload.pop("transfer_item_id", None)
                    task.payload.pop("spent_battery_item_id", None)
                    return True
                finally:
                    agent.battery_swap_critical = False
                    target_agent.battery_swap_critical = False
                    if self.active_battery_delivery_owner == agent.agent_id:
                        self.active_battery_delivery_owner = None
                    if target_agent.awaiting_battery_from == agent.agent_id:
                        self._end_battery_swap_wait(target_agent, agent.agent_id)
                    if target_agent.battery_service_owner == agent.agent_id:
                        target_agent.battery_service_owner = None

            if transfer_kind == "inter_station":
                from_station = int(task.payload["from_station"])
                moved_item_id = str(task.payload.get("transfer_item_id", ""))
                if agent.carrying_item_id != moved_item_id:
                    output_buffer_id = f"output_buffer_station_{from_station}"
                    self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                    yield from self.move_agent(agent, output_buffer_id, emit_move_events=True)
                    if not self._confirm_object_service_tile(agent, output_buffer_id, task, "inter_station_pickup"):
                        return False
                    yield from self._dock_agent_at_target(agent, task, reason="inter_station_pickup_alignment")
                    self._set_humanoid_primitive_hint(agent, "LOCALIZE_OBJECT")
                    self._transition_humanoid_state(
                        agent,
                        "task_started",
                        task=task,
                        status="running",
                        reason="inter_station_pickup",
                        source="mansim.transfer",
                    )
                    popped_item_id = self._pop_output_buffer_item(from_station, moved_item_id or None)
                    if popped_item_id is None:
                        task.payload["failure_reason"] = "RESOURCE_PREEMPTED" if moved_item_id else "missing_output_item"
                        if moved_item_id and bool(self.humanoid_incident_natural_cfg.get("RESOURCE_PREEMPTED", True)):
                            self._emit_humanoid_incident(
                                agent,
                                "RESOURCE_PREEMPTED",
                                task=task,
                                primitive_call_code="PRIMITIVE_IDENTIFY_ITEM",
                                source="mansim.resource_race",
                                context={
                                    "from_station": from_station,
                                    "item_id": moved_item_id,
                                    "resource": "output_buffer",
                                },
                            )
                        return False
                    moved_item_id = popped_item_id
                    task.payload["transfer_item_id"] = moved_item_id
                    moved_item_kind = "product" if from_station >= self.last_processing_station else "intermediate"
                    self._set_humanoid_primitive_hint(agent, "GRASP")
                    if not self._set_agent_carrying(agent, moved_item_kind, moved_item_id):
                        self._appendleft_if_absent(self.output_buffers[from_station], moved_item_id)
                        task.payload.pop("transfer_item_id", None)
                        return False
                    self._set_humanoid_primitive_hint(agent, "LIFT")
                if from_station == self.inspection_queue_station:
                    # Final logistics leg: inspected product -> completed product zone.
                    to_location = "CompletedProducts"
                    self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                    yield from self.move_agent(agent, "completed_product_buffer", emit_move_events=True)
                    if not self._confirm_object_service_tile(agent, "completed_product_buffer", task, "completed_product_dropoff"):
                        return False
                    yield from self._dock_agent_at_target(agent, task, reason="completed_product_alignment")
                    self._set_humanoid_primitive_hint(agent, "PLACE")
                    self.product_count += 1
                    if moved_item_id in self.items:
                        self._set_item_state(moved_item_id, ItemState.COMPLETED, location="CompletedProducts", ref="completed_product_buffer", item_type="product")
                else:
                    to_station = from_station + 1
                    to_location = f"Station{to_station}" if to_station <= self.last_processing_station else "Inspection"
                    target_queue_station = to_station if to_station <= self.last_processing_station else self.inspection_queue_station
                    target_queue_id = f"intermediate_queue_{target_queue_station}"
                    self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                    yield from self.move_agent(agent, target_queue_id, emit_move_events=True)
                    if not self._confirm_object_service_tile(agent, target_queue_id, task, "inter_station_dropoff"):
                        return False
                    yield from self._dock_agent_at_target(agent, task, reason="inter_station_dropoff_alignment")
                    self._set_humanoid_primitive_hint(agent, "PLACE")
                    if not self._push_intermediate_queue(target_queue_station, moved_item_id, task=task):
                        self._appendleft_if_absent(self.output_buffers[from_station], moved_item_id)
                        self._clear_agent_carrying(agent, destination=f"output_buffer_station_{from_station}")
                        task.payload["failure_reason"] = "destination_buffer_full"
                        return False
                task.payload.pop("transfer_item_id", None)
                moved_item_kind = "product" if from_station >= self.last_processing_station else "intermediate"
                self._set_humanoid_primitive_hint(agent, "RELEASE")
                self._clear_agent_carrying(agent, destination=to_location)
                self._set_humanoid_primitive_hint(agent, "VERIFY_PLACEMENT")
                if from_station == self.inspection_queue_station:
                    move_to = "completed_product_buffer"
                else:
                    move_to = (
                        f"product_queue_{self.inspection_queue_station}"
                        if (from_station + 1) == self.inspection_queue_station
                        else f"intermediate_queue_{from_station + 1}"
                    )
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ITEM_MOVED",
                    entity_id=moved_item_id,
                    location=to_location,
                    details={
                        "from": f"output_buffer_station_{from_station}",
                        "to": move_to,
                        "item_type": moved_item_kind,
                    },
                )
                if from_station == self.inspection_queue_station:
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="COMPLETED_PRODUCT",
                        entity_id=moved_item_id,
                        location="CompletedProducts",
                        details={"target": "completed_product_buffer"},
                    )
                    self._record_initial_batch_terminal_output(
                        moved_item_id,
                        outcome="accepted_product",
                        completed_by_agent_id=agent.agent_id,
                    )
                return True

            if transfer_kind == "material_supply":
                station = int(task.payload["station"])
                owner = self.material_supply_owner.get(station)
                if not self.is_mfg_flow_shop and owner is not None and owner != agent.agent_id:
                    task.payload["failure_reason"] = "material_supply_owner_changed"
                    if bool(self.humanoid_incident_natural_cfg.get("RESOURCE_PREEMPTED", True)):
                        self._emit_humanoid_incident(
                            agent,
                            "RESOURCE_PREEMPTED",
                            task=task,
                            primitive_call_code="CHECK_REQUEST",
                            source="mansim.resource_race",
                            context={"station": station, "owner": owner, "resource": "material_supply"},
                        )
                    return False
                if not self.is_mfg_flow_shop:
                    self.material_supply_owner[station] = agent.agent_id
                try:
                    item_id = str(task.payload.get("transfer_item_id", ""))
                    if agent.carrying_item_id != item_id:
                        excluded_slot_ids: set[str] = set()
                        while True:
                            source_slot_id = str(task.payload.get("source_slot_id") or "").strip()
                            slot = self.warehouse_material_shelf_slots.get(source_slot_id) if source_slot_id else None
                            if not item_id or not isinstance(slot, dict) or not slot.get("material_item_id"):
                                self._set_humanoid_primitive_hint(agent, "PRIMITIVE_IDENTIFY_ITEM", reason="material_shelf_scan")
                            slot = self._bind_available_material_shelf_slot(
                                agent,
                                task,
                                preferred_slot_id=source_slot_id,
                                exclude_slot_ids=excluded_slot_ids,
                            )
                            if slot is None:
                                if self._material_shelf_count() <= 0:
                                    self._log_material_shelf_empty_once()
                                task.payload["failure_reason"] = "material_shelf_empty"
                                self._emit_humanoid_incident(
                                    agent,
                                    "RESOURCE_MISSING",
                                    task=task,
                                    primitive_call_code="CHECK_REQUEST",
                                    source="mansim.resource",
                                    context={"station": station, "resource": "warehouse_material_shelf"},
                                )
                                return False
                            slot_id = str(slot.get("slot_id", ""))
                            item_id = str(slot.get("material_item_id", ""))
                            self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                            yield from self.move_agent(agent, slot_id, emit_move_events=True)
                            if not self._confirm_object_service_tile(agent, slot_id, task, "material_shelf_pickup"):
                                task.payload["failure_reason"] = "material_shelf_pickup_unreachable"
                                return False
                            yield from self._dock_agent_at_target(agent, task, reason="material_shelf_alignment")
                            self._set_humanoid_primitive_hint(agent, "PRIMITIVE_IDENTIFY_ITEM")
                            self._transition_humanoid_state(
                                agent,
                                "task_started",
                                task=task,
                                status="running",
                                reason="material_pickup",
                                source="mansim.replenishment",
                            )
                            picked = self._pop_material_shelf_item(
                                slot_id,
                                item_id,
                                agent_id=agent.agent_id,
                                task_id=task.task_id,
                            )
                            if picked is None:
                                self._release_task_item_reservations(task, reason="material_pickup_preempted")
                                task.payload.pop("transfer_item_id", None)
                                task.payload.pop("material_item_id", None)
                                task.payload.pop("source_slot_id", None)
                                excluded_slot_ids.add(slot_id)
                                if self._first_available_material_shelf_slot(
                                    agent.agent_id,
                                    task.task_id,
                                    exclude_slot_ids=excluded_slot_ids,
                                    station=station,
                                ) is not None:
                                    continue
                                if self._material_shelf_count() <= 0:
                                    self._log_material_shelf_empty_once()
                                task.payload["failure_reason"] = "RESOURCE_PREEMPTED"
                                if bool(self.humanoid_incident_natural_cfg.get("RESOURCE_PREEMPTED", True)):
                                    self._emit_humanoid_incident(
                                        agent,
                                        "RESOURCE_PREEMPTED",
                                        task=task,
                                        primitive_call_code="PRIMITIVE_IDENTIFY_ITEM",
                                        source="mansim.resource_race",
                                        context={"station": station, "slot_id": slot_id, "resource": "material_shelf_slot"},
                                    )
                                return False
                            picked_slot_id, item_id = picked
                            task.payload["source_slot_id"] = picked_slot_id
                            task.payload["transfer_item_id"] = item_id
                            task.payload["material_item_id"] = item_id
                            if not self._set_agent_carrying(agent, "material", item_id):
                                # Put the item back into the same slot if the worker could not pick it.
                                slot_back = self.warehouse_material_shelf_slots.get(picked_slot_id)
                                if slot_back is not None:
                                    slot_back["material_item_id"] = item_id
                                    slot_back["occupied"] = True
                                    self._set_item_state(item_id, ItemState.IN_STORAGE, location="Warehouse", ref=picked_slot_id, item_type="material")
                                self._release_task_item_reservations(task, reason="material_carry_failed")
                                task.payload.pop("transfer_item_id", None)
                                task.payload.pop("material_item_id", None)
                                task.payload.pop("source_slot_id", None)
                                task.payload["failure_reason"] = "material_carry_failed"
                                return False
                            break
                    material_queue_id = f"material_queue_{station}"
                    self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                    yield from self.move_agent(agent, material_queue_id, emit_move_events=True)
                    if not self._confirm_object_service_tile(agent, material_queue_id, task, "material_supply_dropoff"):
                        task.payload["failure_reason"] = "material_supply_dropoff_unreachable"
                        return False
                    yield from self._dock_agent_at_target(agent, task, reason="material_supply_dropoff_alignment")
                    if not self._push_material_queue(station, item_id, task=task):
                        task.payload["failure_reason"] = "destination_buffer_full"
                        return False
                    self._clear_agent_carrying(agent, destination=f"Station{station}")
                    self._set_humanoid_primitive_hint(agent, "VERIFY_LEVEL_OR_QUANTITY")
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="ITEM_MOVED",
                        entity_id=item_id,
                        location=f"Station{station}",
                        details={"from": "Warehouse", "to": f"material_queue_{station}"},
                    )
                    self._set_humanoid_primitive_hint(agent, "UPDATE_RECORD")
                    return True
                finally:
                    if not self.is_mfg_flow_shop and self.material_supply_owner.get(station) == agent.agent_id:
                        self.material_supply_owner[station] = None

            return False

        if task_type == "LOAD_MACHINE":
            machine = self.machines[task.payload["machine_id"]]
            station = machine.station
            if machine.setup_owner is not None and machine.setup_owner != agent.agent_id:
                return False
            machine.setup_owner = agent.agent_id

            try:
                if machine.broken or machine.output_intermediate is not None:
                    return False
                if machine.state not in {MachineState.WAIT_INPUT, MachineState.SETUP, MachineState.IDLE}:
                    return False
                load_slot = str(task.payload.get("load_slot") or task.payload.get("target_slot") or "").strip().lower()
                if load_slot not in {"material", "intermediate"}:
                    load_slot = "intermediate" if task.payload.get("intermediate_id") else "material"
                item_type = "intermediate" if load_slot == "intermediate" else "material"
                if load_slot == "material" and machine.input_material is not None:
                    return False
                if load_slot == "intermediate":
                    if not self._station_requires_intermediate(station) or machine.input_intermediate is not None:
                        return False

                item_id = str(
                    task.payload.get("item_id")
                    or task.payload.get("material_id")
                    or task.payload.get("intermediate_id")
                    or ""
                )
                source_queue_id = f"{item_type}_queue_{station}"
                self._set_humanoid_primitive_hint(agent, "CHECK_SAFETY_ZONE")
                self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                yield from self.move_agent(agent, source_queue_id, emit_move_events=True)
                if not self._confirm_object_service_tile(agent, source_queue_id, task, f"load_machine_{load_slot}_pickup"):
                    return False
                yield from self._dock_agent_at_target(agent, task, reason=f"load_machine_{load_slot}_pickup_alignment")
                self._set_humanoid_primitive_hint(agent, "READ_MACHINE_STATE")
                if load_slot == "material":
                    popped_item = self._pop_material_queue(station, item_id or None)
                    resource_name = "material_queue"
                else:
                    popped_item = self._pop_intermediate_queue(station, item_id or None)
                    resource_name = "intermediate_queue"
                if popped_item is None:
                    task.payload["failure_reason"] = "RESOURCE_PREEMPTED" if item_id else "missing_input"
                    if item_id and bool(self.humanoid_incident_natural_cfg.get("RESOURCE_PREEMPTED", True)):
                        self._emit_humanoid_incident(
                            agent,
                            "RESOURCE_PREEMPTED",
                            task=task,
                            primitive_call_code="PRIMITIVE_IDENTIFY_ITEM",
                            source="mansim.resource_race",
                            context={"station": station, "item_id": item_id, "resource": resource_name},
                        )
                    return False
                item_id = popped_item
                task.payload["item_id"] = item_id
                task.payload[f"{item_type}_id"] = item_id
                self._set_humanoid_primitive_hint(agent, "EXECUTE_MACHINE_ACTION")
                self._set_humanoid_primitive_hint(agent, "GRASP")
                if not self._set_agent_carrying(agent, item_type, item_id):
                    if load_slot == "material":
                        self._appendleft_if_absent(self.material_queues[station], item_id)
                    else:
                        self._appendleft_if_absent(self.intermediate_queues[station], item_id)
                    task.payload.pop("item_id", None)
                    task.payload.pop(f"{item_type}_id", None)
                    return False
                self._set_humanoid_primitive_hint(agent, "LIFT")
                self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                yield from self.move_agent(agent, machine.machine_id, emit_move_events=True)
                if not self._confirm_object_service_tile(agent, machine.machine_id, task, f"load_machine_{load_slot}_dropoff"):
                    if load_slot == "material":
                        self._appendleft_if_absent(self.material_queues[station], item_id)
                    else:
                        self._appendleft_if_absent(self.intermediate_queues[station], item_id)
                    self._clear_agent_carrying(agent, destination=source_queue_id)
                    return False
                yield from self._dock_agent_at_target(agent, task, reason=f"load_machine_{load_slot}_dropoff_alignment")
                self._set_humanoid_primitive_hint(agent, "PLACE")
                if load_slot == "material":
                    if machine.input_material is not None:
                        self._appendleft_if_absent(self.material_queues[station], item_id)
                        self._clear_agent_carrying(agent, destination=source_queue_id)
                        return False
                    machine.input_material = item_id
                else:
                    if machine.input_intermediate is not None:
                        self._appendleft_if_absent(self.intermediate_queues[station], item_id)
                        self._clear_agent_carrying(agent, destination=source_queue_id)
                        return False
                    machine.input_intermediate = item_id
                self._set_item_state(item_id, ItemState.LOADED_ON_MACHINE, location=f"Station{station}", ref=machine.machine_id, item_type=item_type)
                machine.setup_ready = False
                self._clear_agent_carrying(agent, destination=machine.machine_id)
                self._set_humanoid_primitive_hint(agent, "RELEASE")
                self._set_humanoid_primitive_hint(agent, "VERIFY_MACHINE_STATE")
                self._set_machine_state(machine, MachineState.WAIT_INPUT, reason="input_loaded_waiting_setup")
                return True

            finally:
                if machine.setup_owner == agent.agent_id:
                    machine.setup_owner = None

        if task_type == "SETUP_MACHINE":
            machine = self.machines[task.payload["machine_id"]]
            station = machine.station
            if machine.setup_owner is not None and machine.setup_owner != agent.agent_id:
                return False
            machine.setup_owner = agent.agent_id
            setup_started = False
            setup_start_t = 0.0
            setup_event_id = f"{machine.machine_id}:{agent.agent_id}:{task.task_id}"

            def _close_setup_event(outcome: str) -> None:
                nonlocal setup_started
                if not setup_started:
                    return
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_SETUP_END",
                    entity_id=machine.machine_id,
                    location=f"Station{station}",
                    details={
                        "by": agent.agent_id,
                        "task_id": task.task_id,
                        "setup_id": setup_event_id,
                        "duration": round(max(0.0, self.env.now - setup_start_t), 3),
                        "outcome": outcome,
                    },
                )
                setup_started = False

            try:
                if machine.broken or machine.output_intermediate is not None:
                    return False
                requires_intermediate = self._station_requires_intermediate(station)
                if machine.input_material is None or (requires_intermediate and machine.input_intermediate is None):
                    return False
                if machine.setup_ready:
                    return False

                self._set_humanoid_primitive_hint(agent, "CHECK_SAFETY_ZONE")
                self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                yield from self.move_agent(agent, machine.machine_id, emit_move_events=True)
                if not self._confirm_object_service_tile(agent, machine.machine_id, task, "setup_machine"):
                    return False
                yield from self._dock_agent_at_target(agent, task, reason="setup_machine_alignment")
                if machine.broken:
                    task.payload["failure_reason"] = "machine_broken"
                    return False
                self._set_humanoid_primitive_hint(agent, "READ_MACHINE_STATE")
                self._transition_humanoid_state(
                    agent,
                    "task_started",
                    task=task,
                    status="running",
                    reason="setup_machine",
                    source="mansim.machine",
                )

                setup_step = self.active_primitive_duration(task, self.movement_cfg["setup_min"])
                self._set_machine_state(machine, MachineState.SETUP, reason="setup_started")
                self._set_humanoid_primitive_hint(agent, "EXECUTE_MACHINE_ACTION")
                setup_started = True
                setup_start_t = float(self.env.now)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_SETUP_START",
                    entity_id=machine.machine_id,
                    location=f"Station{station}",
                    details={"by": agent.agent_id, "task_id": task.task_id, "setup_id": setup_event_id},
                )
                yield self.env.timeout(setup_step)
                if machine.broken:
                    task.payload["failure_reason"] = "machine_broken"
                    _close_setup_event("aborted_machine_broken")
                    return False
                machine.setup_ready = True
                self._set_machine_state(machine, MachineState.IDLE, reason="setup_completed")
                self._set_humanoid_primitive_hint(agent, "VERIFY_MACHINE_STATE")
                _close_setup_event("completed")
                return True

            finally:
                if machine.setup_owner == agent.agent_id:
                    machine.setup_owner = None
                    if setup_started:
                        _close_setup_event("aborted_machine_broken" if machine.broken else "aborted")
                    if machine.state == MachineState.SETUP and not machine.setup_ready and not machine.broken:
                        self._set_machine_state(machine, MachineState.WAIT_INPUT, reason="setup_aborted")
        if task_type == "COLLECT_WASTE_OR_SCRAP":
            if self.scrap_disposal_owner is not None and self.scrap_disposal_owner != agent.agent_id:
                return False
            self.scrap_disposal_owner = agent.agent_id
            try:
                max_count = max(1, int(task.payload.get("max_carry_count", self.scrap_transport_max_carry_count) or self.scrap_transport_max_carry_count))
                if not self.inspection_scrap_queue and not task.payload.get("item_ids"):
                    return False
                self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                yield from self.move_agent(agent, "inspection_scrap_queue", emit_move_events=True)
                if not self._confirm_object_service_tile(agent, "inspection_scrap_queue", task, "scrap_batch_pickup"):
                    return False
                yield from self._dock_agent_at_target(agent, task, reason="scrap_pickup_alignment")
                self._set_humanoid_primitive_hint(agent, "PRIMITIVE_IDENTIFY_ITEM")
                self._transition_humanoid_state(
                    agent,
                    "task_started",
                    task=task,
                    status="running",
                    reason="scrap_pickup",
                    source="mansim.scrap",
                )
                item_ids = [str(item_id) for item_id in task.payload.get("item_ids", []) if str(item_id)] if isinstance(task.payload.get("item_ids"), list) else []
                if agent.carrying_item_id is None and not getattr(agent, "carrying_item_ids", []):
                    requested_item_ids = list(item_ids) if item_ids else None
                    item_ids = self._pop_inspection_scrap_batch(max_count, requested_item_ids)
                    task.payload["item_ids"] = list(item_ids)
                if not item_ids:
                    task.payload["failure_reason"] = "RESOURCE_PREEMPTED"
                    if bool(self.humanoid_incident_natural_cfg.get("RESOURCE_PREEMPTED", True)):
                        self._emit_humanoid_incident(
                            agent,
                            "RESOURCE_PREEMPTED",
                            task=task,
                            primitive_call_code="PRIMITIVE_IDENTIFY_ITEM",
                            source="mansim.resource_race",
                            context={"resource": "inspection_scrap_queue"},
                        )
                    return False
                self._set_humanoid_primitive_hint(agent, "GRASP")
                if agent.carrying_item_id is None and not getattr(agent, "carrying_item_ids", []):
                    self._set_worker_cargo_batch(
                        agent,
                        item_ids,
                        "product",
                        max_item_count=max_count,
                        destination="scrap_disposal_bin",
                    )
                self._set_humanoid_primitive_hint(agent, "LIFT")
                self.scrap_transport_batches += 1
                self.scrap_transport_items += len(item_ids)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="SCRAP_BATCH_PICKED",
                    entity_id=agent.agent_id,
                    location="Inspection",
                    details={
                        "item_ids": item_ids,
                        "item_count": len(item_ids),
                        "max_item_count": max_count,
                        "queue_id": "inspection_scrap_queue",
                        "queue_length": len(self.inspection_scrap_queue),
                        "cargo": self._worker_cargo_payload(agent),
                        "humanoid_state": self._humanoid_state_payload(agent),
                    },
                )
                self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                yield from self.move_agent(agent, "scrap_disposal_bin", emit_move_events=True)
                if not self._confirm_object_service_tile(agent, "scrap_disposal_bin", task, "scrap_disposal_dropoff"):
                    return False
                yield from self._dock_agent_at_target(agent, task, reason="scrap_disposal_alignment")
                self._set_humanoid_primitive_hint(agent, "PLACE")
                for item_id in item_ids:
                    self._set_item_state(
                        item_id,
                        ItemState.SCRAPPED,
                        location="ScrapDisposal",
                        ref="scrap_disposal_bin",
                        item_type="product",
                    )
                self.disposed_scrap_count += len(item_ids)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="SCRAP_DISPOSED",
                    entity_id="scrap_disposal_bin",
                    location="ScrapDisposal",
                    details={
                        "item_ids": item_ids,
                        "item_count": len(item_ids),
                        "disposed_scrap_count": int(self.disposed_scrap_count),
                        "scrap_transport_batches": int(self.scrap_transport_batches),
                        "humanoid_state": self._humanoid_state_payload(agent),
                    },
                )
                for item_id in item_ids:
                    self._record_initial_batch_terminal_output(
                        item_id,
                        outcome="disposed_scrap",
                        completed_by_agent_id=agent.agent_id,
                    )
                self._clear_agent_carrying(agent, destination="scrap_disposal_bin")
                self._set_humanoid_primitive_hint(agent, "RELEASE")
                task.payload.pop("item_ids", None)
                return True
            finally:
                if self.scrap_disposal_owner == agent.agent_id:
                    self.scrap_disposal_owner = None

        if task_type == "PREVENTIVE_MAINTENANCE":
            machine = self.machines[task.payload["machine_id"]]
            if machine.pm_owner is not None and machine.pm_owner != agent.agent_id:
                return False
            machine.pm_owner = agent.agent_id
            restore_state = machine.state
            pm_start = None
            pm_completed = False
            try:
                if (
                    machine.broken
                    or machine.state in {MachineState.PROCESSING, MachineState.UNDER_REPAIR}
                    or machine.output_intermediate is not None
                ):
                    return False
                self._set_humanoid_primitive_hint(agent, "CHECK_SAFETY_ZONE")
                self._set_humanoid_primitive_hint(agent, "NAVIGATE_TO")
                yield from self.move_agent(agent, machine.machine_id, emit_move_events=True)
                if not self._confirm_object_service_tile(agent, machine.machine_id, task, "preventive_maintenance"):
                    return False
                yield from self._dock_agent_at_target(agent, task, reason="preventive_maintenance_alignment")
                self._set_humanoid_primitive_hint(agent, "INSPECT_OR_DIAGNOSE")
                self._transition_humanoid_state(
                    agent,
                    "task_started",
                    task=task,
                    status="running",
                    reason="preventive_maintenance",
                    source="mansim.maintenance",
                )
                self._set_machine_state(machine, MachineState.UNDER_PM, reason="pm_started")
                self._set_humanoid_primitive_hint(agent, "EXECUTE_MAINTENANCE_ACTION")
                pm_start = self.env.now
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_PM_START",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={"by": agent.agent_id},
                )
                yield self.env.timeout(self.active_primitive_duration(task, self.machine_failure_cfg["pm_time_min"]))
                pm_duration = self.env.now - pm_start
                machine.total_pm_min += pm_duration
                machine.pm_count += 1
                machine.last_pm_at = self.env.now
                machine.processing_since_maintenance_min = 0.0
                if self.machine_failure_time_basis == "active_processing":
                    machine.pm_until = self.env.now
                    machine.pm_protected_processing_remaining_min = max(
                        0.0, float(self.pm_effect_duration_min)
                    )
                else:
                    machine.pm_until = self.env.now + self.pm_effect_duration_min
                next_state = (
                    MachineState.DONE_WAIT_UNLOAD
                    if machine.output_intermediate is not None
                    else MachineState.WAIT_INPUT
                    if restore_state == MachineState.WAIT_INPUT
                    else MachineState.IDLE
                )
                self._set_machine_state(machine, next_state, reason="pm_completed")
                self._set_humanoid_primitive_hint(agent, "VERIFY_MACHINE_STATE")
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_PM_EFFECT_STARTED",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={
                        "time_basis": self.machine_failure_time_basis,
                        "protected_processing_min": round(
                            float(self.pm_effect_duration_min), 6
                        ),
                        "hazard_multiplier": round(float(self.pm_lambda_multiplier), 6),
                        "failure_exposure_remaining_min": round(
                            max(
                                0.0,
                                float(machine.failure_exposure_budget_min)
                                - float(machine.failure_exposure_used_min),
                            ),
                            6,
                        ),
                    },
                )
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_PM_END",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={"by": agent.agent_id, "duration": round(pm_duration, 3), "outcome": "completed"},
                )
                pm_completed = True
                return True
            finally:
                if pm_start is not None and not pm_completed:
                    elapsed = max(0.0, self.env.now - pm_start)
                    machine.total_pm_min += elapsed
                    if machine.state == MachineState.UNDER_PM:
                        self._set_machine_state(machine, restore_state, reason="pm_interrupted")
                    if not self.logger.closed:
                        self.logger.log(
                            t=self.env.now,
                            day=self.day_for_time(self.env.now),
                            event_type="MACHINE_PM_END",
                            entity_id=machine.machine_id,
                            location=f"Station{machine.station}",
                            details={"by": agent.agent_id, "duration": round(elapsed, 3), "outcome": "interrupted"},
                        )
                if machine.pm_owner == agent.agent_id:
                    machine.pm_owner = None
        return False

    def machine_remaining_processing_min(self, machine: Machine) -> float:
        remaining = float(machine.cycle_remaining_process_min)
        if machine.state == MachineState.PROCESSING and machine.cycle_processing_started_at is not None:
            remaining -= max(0.0, float(self.env.now) - machine.cycle_processing_started_at)
        return max(0.0, remaining)

    def start_machine_cycle(self, machine: Machine) -> str:
        resumed = bool(machine.active_cycle_id and machine.cycle_remaining_process_min > 0.0)
        if resumed:
            cycle_id = str(machine.active_cycle_id)
        else:
            cycle_id = self._next_cycle_id()
            distribution = self.processing_time_distribution[machine.station]
            sampled = sample_triangular(
                distribution,
                seed=getattr(self, "_diagnostic_sampling_seed", self.seed),
                namespace=f"{self.scenario_key}:machine:station{machine.station}",
                sample_key=cycle_id,
            )
            machine.active_cycle_id = cycle_id
            machine.cycle_sampled_process_min = sampled
            machine.cycle_remaining_process_min = sampled
            self.timing.record_sample(
                "machine_processing",
                cycle_id,
                sampled,
                distribution,
                machine_id=machine.machine_id,
                station=machine.station,
            )
        machine.cycle_processing_started_at = float(self.env.now)
        self._set_machine_state(machine, MachineState.PROCESSING, reason="cycle_resumed" if resumed else "cycle_started")
        if machine.input_material:
            self._set_item_state(machine.input_material, ItemState.PROCESSING, location=f"Station{machine.station}", ref=machine.machine_id, item_type="material")
        if machine.input_intermediate:
            self._set_item_state(machine.input_intermediate, ItemState.PROCESSING, location=f"Station{machine.station}", ref=machine.machine_id, item_type="intermediate")
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_RESUME" if resumed else "MACHINE_START",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={
                "cycle_id": cycle_id,
                "input_material": machine.input_material,
                "input_intermediate": machine.input_intermediate,
                "sampled_process_time_min": round(machine.cycle_sampled_process_min, 6),
                "remaining_process_time_min": round(machine.cycle_remaining_process_min, 6),
                "processing_time_distribution": self.processing_time_distribution[machine.station].to_dict(),
            },
        )
        return cycle_id

    def complete_machine_cycle(self, machine: Machine, cycle_id: str) -> None:
        input_material_id = machine.input_material
        input_intermediate_id = machine.input_intermediate
        sampled_process_time_min = float(machine.cycle_sampled_process_min)
        if machine.station == self.last_processing_station:
            output_id = self._next_item_id("PRODUCT")
            output_type = "product"
        else:
            output_id = self._next_item_id(f"INT-S{machine.station}")
            output_type = "intermediate"
        source_created_at: list[float] = []
        if input_material_id and input_material_id in self.items:
            source_created_at.append(float(self.items[input_material_id].created_at))
        if input_intermediate_id and input_intermediate_id in self.items:
            source_created_at.append(float(self.items[input_intermediate_id].created_at))
        output_created_at = min(source_created_at) if source_created_at else float(self.env.now)
        self.items[output_id] = Item(
            item_id=output_id,
            item_type=output_type,
            created_at=output_created_at,
            current_station=machine.station,
        )
        source_item_ids = [item_id for item_id in (input_material_id, input_intermediate_id) if item_id]
        source_material_ids: list[str] = []
        source_intermediate_ids: list[str] = []
        if input_material_id:
            source_material_ids.append(input_material_id)
        if input_intermediate_id:
            source_intermediate_ids.append(input_intermediate_id)
            input_intermediate = self.items.get(input_intermediate_id)
            if input_intermediate is not None:
                source_material_ids.extend(str(item_id) for item_id in input_intermediate.metadata.get("source_material_ids", []) if str(item_id).strip())
                source_intermediate_ids.extend(
                    str(item_id) for item_id in input_intermediate.metadata.get("source_intermediate_ids", []) if str(item_id).strip()
                )
        self.items[output_id].metadata.update(
            {
                "source_item_ids": list(dict.fromkeys(source_item_ids)),
                "source_material_ids": list(dict.fromkeys(source_material_ids)),
                "source_intermediate_ids": list(dict.fromkeys(source_intermediate_ids)),
                "transformed_from_item_ids": list(dict.fromkeys(source_item_ids)),
            }
        )
        for source_item_id in source_item_ids:
            source_item = self.items.get(source_item_id)
            if source_item is None:
                continue
            source_item.metadata["transformed_to_item_id"] = output_id
            self._set_item_state(
                source_item_id,
                ItemState.TRANSFORMED,
                location=f"Station{machine.station}",
                ref=output_id,
                item_type=source_item.item_type,
            )
        machine.active_process = None
        machine.active_cycle_id = None
        machine.cycle_sampled_process_min = 0.0
        machine.cycle_remaining_process_min = 0.0
        machine.cycle_processing_started_at = None
        machine.input_material = None
        machine.input_intermediate = None
        machine.setup_ready = False
        machine.output_intermediate = output_id
        self._set_item_state(output_id, ItemState.WAITING_MACHINE_UNLOAD, location=f"Station{machine.station}", ref=machine.machine_id, item_type=output_type)
        self._set_machine_state(machine, MachineState.DONE_WAIT_UNLOAD, reason="cycle_completed")
        self.station_throughput[machine.station] += 1
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_END",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={
                "cycle_id": cycle_id,
                "output_intermediate": output_id,
                "output_item_id": output_id,
                "output_item_type": output_type,
                "source_item_ids": list(dict.fromkeys(source_item_ids)),
                "source_material_ids": list(dict.fromkeys(source_material_ids)),
                "source_intermediate_ids": list(dict.fromkeys(source_intermediate_ids)),
                "sampled_process_time_min": round(sampled_process_time_min, 6),
                "processing_time_distribution": self.processing_time_distribution[machine.station].to_dict(),
            },
        )

    def abort_machine_cycle(self, machine: Machine, cycle_id: str, reason: str, *, elapsed_min: float = 0.0) -> None:
        input_material_id = machine.input_material
        input_intermediate_id = machine.input_intermediate
        machine.active_process = None
        machine.cycle_remaining_process_min = max(
            0.0,
            float(machine.cycle_remaining_process_min) - max(0.0, float(elapsed_min)),
        )
        machine.cycle_processing_started_at = None
        machine.setup_ready = True
        if input_material_id:
            self._set_item_state(
                input_material_id,
                ItemState.LOADED_ON_MACHINE,
                location=f"Station{machine.station}",
                ref=machine.machine_id,
                item_type="material",
            )
        if input_intermediate_id:
            self._set_item_state(
                input_intermediate_id,
                ItemState.LOADED_ON_MACHINE,
                location=f"Station{machine.station}",
                ref=machine.machine_id,
                item_type="intermediate",
            )
        self._set_machine_state(machine, MachineState.BROKEN if machine.broken else MachineState.WAIT_INPUT, reason=reason)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_ABORTED",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={
                "cycle_id": cycle_id,
                "reason": reason,
                "elapsed_min": round(float(elapsed_min), 3),
                "retained_input_material": input_material_id,
                "retained_input_intermediate": input_intermediate_id,
                "sampled_process_time_min": round(float(machine.cycle_sampled_process_min), 6),
                "remaining_process_time_min": round(float(machine.cycle_remaining_process_min), 6),
                "progress_policy": "resume_after_repair",
            },
        )

    def _empty_agent_priority_counter(self, *, float_values: bool = False) -> dict[str, float] | dict[str, int]:
        default_value: float | int = 0.0 if float_values else 0
        return {key: default_value for key in self._scenario_priority_keys()}

    def _agent_day_experience(self, task_slice: list[dict[str, Any]]) -> dict[str, Any]:
        # Build the per-agent behavioral summary that feeds next-day overlay updates and
        # becomes the compact personal memory shown to daily review artifacts and workspace memory updates.
        experience: dict[str, Any] = {}
        downstream_keys = {"unload_machine", "inter_station_transfer", "inspect_product"}
        reliability_keys = {"repair_machine", "preventive_maintenance"}
        battery_keys = {"battery_swap", "battery_charge", "battery_delivery_low_battery", "battery_delivery_discharged"}
        supply_keys = {"material_supply", "load_machine", "setup_machine"}

        for agent_id in sorted(self.agents.keys()):
            completed_counts = self._empty_agent_priority_counter()
            completed_minutes = self._empty_agent_priority_counter(float_values=True)
            interrupted_counts = self._empty_agent_priority_counter()
            skipped_counts = self._empty_agent_priority_counter()
            decision_source_counts: dict[str, int] = defaultdict(int)
            recent_task_events: list[dict[str, Any]] = []

            for rec in task_slice:
                if str(rec.get("agent_id", "")) != agent_id:
                    continue
                priority_key = str(rec.get("priority_key", "")).strip()
                if priority_key not in completed_counts:
                    continue
                status = str(rec.get("status", "")).strip().lower()
                duration = float(rec.get("duration", 0.0) or 0.0)
                if status == "completed":
                    completed_counts[priority_key] += 1
                    completed_minutes[priority_key] += duration
                elif status == "interrupted":
                    interrupted_counts[priority_key] += 1
                elif status == "skipped":
                    skipped_counts[priority_key] += 1
                decision_source = str(rec.get("decision_source", "")).strip()
                if decision_source:
                    decision_source_counts[decision_source] += 1
                recent_task_events.append(
                    {
                        "task_id": rec.get("task_id", ""),
                        "priority_key": priority_key,
                        "status": status,
                        "duration": round(duration, 3),
                        "decision_source": decision_source,
                    }
                )

            contribution_signals = {
                "downstream_flow_completed": sum(completed_counts.get(key, 0) for key in downstream_keys),
                "reliability_completed": sum(completed_counts.get(key, 0) for key in reliability_keys),
                "battery_support_completed": sum(completed_counts.get(key, 0) for key in battery_keys),
                "supply_support_completed": sum(completed_counts.get(key, 0) for key in supply_keys),
            }
            ranked = sorted(
                completed_minutes.items(),
                key=lambda item: (float(item[1]), completed_counts.get(item[0], 0)),
                reverse=True,
            )
            experience[agent_id] = {
                "completed_counts": {key: int(value) for key, value in completed_counts.items()},
                "completed_minutes": {key: round(float(value), 3) for key, value in completed_minutes.items()},
                "interrupted_counts": {key: int(value) for key, value in interrupted_counts.items()},
                "skipped_counts": {key: int(value) for key, value in skipped_counts.items()},
                "decision_source_counts": dict(decision_source_counts),
                "manager_queue_skipped_count": int(self.manager_queue_skipped_counts.get(agent_id, 0)),
                "contribution_signals": contribution_signals,
                "top_completed_task_families": [
                    {"priority_key": key, "completed_minutes": round(float(value), 3), "completed_count": int(completed_counts.get(key, 0))}
                    for key, value in ranked[:3]
                    if float(value) > 0.0 or int(completed_counts.get(key, 0)) > 0
                ],
                "recent_task_events": recent_task_events[-5:],
                "current_priority_profile": self.current_agent_priority_multipliers(agent_id),
                "current_effective_task_priority_weights": self.current_effective_task_priority_weights(agent_id),
            }
        return experience

    @staticmethod
    def _incident_bucket(incident_class: str) -> str:
        name = str(incident_class or "").strip().lower()
        if name in {"machine_broken", "machine_recovered", "worker_discharged", "worker_low_battery", "buffer_blocked", "material_starvation", "inspection_congestion"}:
            return "physical"
        return "coordination"

    def close_open_activity_at_horizon(self, *, reason: str = "horizon_reached") -> None:
        """Close active observations and release unstarted dispatch reservations."""
        for agent in self.agents.values():
            if agent.current_move_id:
                self._close_current_move_segment(
                    agent,
                    logical_destination=agent.current_move_logical_destination,
                    reason=reason,
                )
                self._log_interrupted_move(agent, reason=reason, logical_destination=agent.current_move_logical_destination)
                self._traffic_complete_plan(str(agent.current_move_id))
                self._clear_current_move(agent)
                self._clear_in_transit(agent)

            if not agent.current_task_id:
                if not agent.discharged:
                    self._set_humanoid_for_task(agent, None, reason=reason)
                continue

            task = self._current_parent_task_stub(agent)
            runtime = getattr(self, "humanoid_runtime", None)
            task_context = (agent.humanoid_state or {}).get("task_context") if isinstance(agent.humanoid_state, dict) else None
            execution_status = (
                str(task_context.get("execution_status", "")).strip().upper()
                if isinstance(task_context, dict)
                else ""
            )
            humanoid_task_started = bool(
                execution_status and execution_status != "PENDING"
                or agent.current_step_id
                or agent.current_step_call_code
                or agent.current_child_task_code
                or agent.current_task_path
            )
            if runtime is not None and getattr(runtime, "enabled", False) and humanoid_task_started:
                step_path = str(agent.current_step_path or "")
                # Only close catalog steps that were actually started by
                # HumanoidTaskRuntime. Domain-internal hints like NAVIGATE_TO may
                # exist during a nested child task, but they do not have matching
                # HUMANOID_STEP_START rows and should not be emitted as step ends.
                if step_path and (agent.current_step_id or agent.current_step_call_code):
                    step_task = self._current_child_task_stub(agent) or task
                    step = {
                        "step_id": str(agent.current_step_id or ""),
                        "call_code": str(agent.current_step_call_code or agent.current_primitive_call_code or ""),
                        "path": step_path,
                        "depth": int(agent.current_step_depth or 0),
                        "depends_on": [],
                    }
                    runtime._log_step_event(
                        "HUMANOID_STEP_END",
                        agent,
                        step_task,
                        step,
                        status="interrupted",
                        error=reason,
                        parent_task=task,
                    )
                child_task = self._current_child_task_stub(agent)
                if child_task is not None:
                    runtime._log_child_task_event(
                        "HUMANOID_TASK_END",
                        agent,
                        task,
                        child_task,
                        {
                            "path": str(agent.current_task_path or ""),
                            "depth": int(agent.current_task_depth or 0),
                        },
                        status="interrupted",
                    )
                runtime._log_task_event("HUMANOID_TASK_END", agent, task, status="interrupted")

            if agent.charging_started_at is not None:
                self._close_interrupted_direct_charge(
                    agent,
                    task_id=str(agent.current_task_id or task.task_id),
                    reason=reason,
                )
                if agent.battery_service_owner == agent.agent_id:
                    agent.battery_service_owner = None

            self.finish_agent_task(
                agent,
                task,
                float(agent.current_task_started_at if agent.current_task_started_at is not None else self.env.now),
                status="interrupted",
                reason=reason,
            )

        # A joint assignment may still be queued while its worker finishes recovery.
        if self.adp_coordinator is not None:
            self.adp_coordinator.cancel_pending_assignments(reason=reason)

    def finalize_day(self, day: int) -> dict[str, Any]:
        products_today = self.product_count - int(self.day_baseline["products"])
        scrap_today = self.scrap_count - int(self.day_baseline["scrap"])
        total_checked = products_today + scrap_today
        scrap_rate = (scrap_today / total_checked) if total_checked > 0 else 0.0

        day_events = [e for e in self.logger.events if e["day"] == day]
        machine_breakdowns = sum(1 for e in day_events if e["type"] == "MACHINE_BROKEN")
        station_completions = {station: 0 for station in self.stations}
        inspection_passes = 0
        agent_discharged_count = 0
        battery_delivery_count = 0
        battery_charge_count = 0
        battery_charge_time_min = 0.0
        inspect_product_task_count = 0
        incident_event_count = 0
        physical_incident_count = 0
        coordination_incident_count = 0
        planner_escalation_count = 0
        for event in day_events:
            event_type = str(event.get("type", "")).strip()
            if event_type == "MACHINE_END":
                location = str(event.get("location", "")).strip()
                if location.startswith("Station"):
                    try:
                        station = int(location.replace("Station", ""))
                    except ValueError:
                        station = 0
                    if station in station_completions:
                        station_completions[station] += 1
            elif event_type == "INSPECT_PASS":
                inspection_passes += 1
            elif event_type == "AGENT_DISCHARGED":
                agent_discharged_count += 1
            elif event_type == "BATTERY_DELIVERED":
                battery_delivery_count += 1
            elif event_type == "BATTERY_CHARGE_COMPLETED":
                battery_charge_count += 1
            elif event_type == "AGENT_TASK_START":
                details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
                if str(details.get("task_type", "")).strip() == "INSPECT_PRODUCT":
                    inspect_product_task_count += 1
            elif event_type == "INCIDENT_EVENT":
                incident_event_count += 1
                details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
                incident_class = str(details.get("incident_class", "")).strip()
                if self._incident_bucket(incident_class) == "physical":
                    physical_incident_count += 1
                else:
                    coordination_incident_count += 1
                    if str(details.get("escalation_level", "")).strip().lower() == "planner":
                        planner_escalation_count += 1
        snapshots = [s for s in self.minute_snapshots if s["day"] == day]
        if snapshots:
            avg_wip_material = mean(sum(s["material_queue_lengths"].values()) for s in snapshots)
            avg_wip_intermediate = mean(sum(s["intermediate_queue_lengths"].values()) for s in snapshots)
        else:
            avg_wip_material = 0.0
            avg_wip_intermediate = 0.0

        task_slice = self.task_records[int(self.day_baseline["task_count"]) :]
        task_breakdown: dict[str, float] = defaultdict(float)
        humanoid_task_breakdown: dict[str, float] = defaultdict(float)
        local_response_task_count = 0
        commitment_dispatch_task_count = 0
        for rec in task_slice:
            if rec["status"] == "completed":
                task_breakdown[rec["task_type"]] += float(rec["duration"])
                humanoid_code = str(rec.get("humanoid_task_code", "")).strip()
                if humanoid_code:
                    humanoid_task_breakdown[humanoid_code] += float(rec["duration"])
            decision_source = str(rec.get("decision_source", "")).strip().lower()
            if decision_source == "worker_local_response":
                local_response_task_count += 1
            if decision_source == "manager_commitment":
                commitment_dispatch_task_count += 1

        processing_delta = {
            mid: self.machines[mid].total_processing_min - self.day_baseline["machine_processing"][mid]
            for mid in self.machines
        }
        broken_delta = {
            mid: self.machines[mid].total_broken_min - self.day_baseline["machine_broken"][mid]
            for mid in self.machines
        }
        pm_delta = {
            mid: self.machines[mid].total_pm_min - self.day_baseline["machine_pm"][mid]
            for mid in self.machines
        }
        agent_experience = self._agent_day_experience(task_slice)
        days_since_last_product = 0
        if products_today <= 0:
            days_since_last_product = 1
            for prior in reversed(self.daily_summaries):
                if int(prior.get("products", 0) or 0) > 0:
                    break
                days_since_last_product += 1

        total_machine_time_day = max(1.0, float(self.minutes_per_day) * max(1, len(self.machines)))
        discharged_intervals = self._agent_discharged_intervals()
        day_start_t = float((day - 1) * self.minutes_per_day)
        day_end_t = float(day * self.minutes_per_day)
        battery_charge_time_min = self._battery_charge_time_in_interval(
            self._battery_charge_sessions(),
            day_start_t,
            min(day_end_t, float(self.env.now)),
        )
        agent_discharged_min = 0.0
        for _agent_id, start_t, end_t in discharged_intervals:
            overlap_start = max(day_start_t, float(start_t))
            overlap_end = min(day_end_t, float(end_t))
            if overlap_end > overlap_start:
                agent_discharged_min += overlap_end - overlap_start

        summary = {
            "day": day,
            "products": products_today,
            "scrap": scrap_today,
            "disposed_scrap": int(self.disposed_scrap_count - int(self.day_baseline.get("disposed_scrap", 0) or 0)),
            "scrap_rate": round(scrap_rate, 5),
            "warehouse_material_shelf_count": self._material_shelf_count(),
            "warehouse_material_shelf_capacity": self.material_shelf_capacity,
            "warehouse_material_restock_count": int(
                self.warehouse_material_restock_count - int(self.day_baseline.get("warehouse_material_restock", 0) or 0)
            ),
            "inspection_scrap_queue_length": len(self.inspection_scrap_queue),
            "scrap_transport_batches": int(
                self.scrap_transport_batches - int(self.day_baseline.get("scrap_transport_batches", 0) or 0)
            ),
            "scrap_transport_items": int(
                self.scrap_transport_items - int(self.day_baseline.get("scrap_transport_items", 0) or 0)
            ),
            "machine_breakdowns": machine_breakdowns,
            "avg_wip_material": round(avg_wip_material, 3),
            "avg_wip_intermediate": round(avg_wip_intermediate, 3),
            "station1_completions": int(station_completions.get(1, 0)),
            "station2_completions": int(station_completions.get(2, 0)),
            "inspection_passes": int(inspection_passes),
            "inspect_product_task_count": int(inspect_product_task_count),
            "station1_output_buffer_end": len(self.output_buffers[1]),
            "station2_output_buffer_end": len(self.output_buffers[2]),
            "agent_discharged_count": int(agent_discharged_count),
            "agent_discharged_min": round(agent_discharged_min, 3),
            "battery_delivery_count": int(battery_delivery_count),
            "battery_charge_count": int(battery_charge_count),
            "battery_charge_time_min": round(battery_charge_time_min, 3),
            "incident_event_count": int(incident_event_count),
            "physical_incident_count": int(physical_incident_count),
            "coordination_incident_count": int(coordination_incident_count),
            "unique_replan_blocker_count": int(len(self.day_unique_replan_blockers)),
            "planner_escalation_count": int(max(planner_escalation_count, len(self.day_planner_escalations))),
            "local_response_task_count": int(local_response_task_count),
            "commitment_dispatch_task_count": int(commitment_dispatch_task_count),
            "days_since_last_product": int(days_since_last_product),
            "task_minutes": dict(task_breakdown),
            "humanoid_task_minutes": dict(humanoid_task_breakdown),
            "machine_processing_min": processing_delta,
            "machine_broken_min": broken_delta,
            "machine_pm_min": pm_delta,
            "machine_utilization": round(sum(processing_delta.values()) / total_machine_time_day, 6),
            "machine_broken_ratio": round(sum(broken_delta.values()) / total_machine_time_day, 6),
            "machine_pm_ratio": round(sum(pm_delta.values()) / total_machine_time_day, 6),
            "inspection_backlog_end": len(self.intermediate_queues[self.inspection_queue_station]),
            "manager_queue_skipped_total": int(sum(self.manager_queue_skipped_counts.values())),
            "manager_queue_skipped_by_agent": {agent_id: int(self.manager_queue_skipped_counts.get(agent_id, 0)) for agent_id in sorted(self.agents.keys())},
            "agent_experience": agent_experience,
            "shared_task_priority_weights": self._current_shared_task_priority_weights(),
            "agent_priority_multipliers": {agent_id: self.current_agent_priority_multipliers(agent_id) for agent_id in sorted(self.agents.keys())},
            "agent_effective_task_priority_weights": {agent_id: self.current_effective_task_priority_weights(agent_id) for agent_id in sorted(self.agents.keys())},
            "plan_revision": int(self._active_plan_revision()),
        }
        self.daily_summaries.append(summary)
        self.manager_queue_skipped_counts = defaultdict(int)
        self.day_unique_replan_blockers = set()
        self.day_planner_escalations = set()
        return summary

    def finalize_kpis(self) -> dict[str, Any]:
        total_checked = self.product_count + self.scrap_count
        total_time = max(1.0, float(self.env.now))
        initial_batch_material_count = len(self.initial_batch_material_ids)
        initial_batch_terminal_material_count = len(self.initial_batch_terminal_material_ids)
        initial_batch_progress_ratio = (
            initial_batch_terminal_material_count / max(1, initial_batch_material_count)
            if initial_batch_material_count
            else 0.0
        )
        initial_batch_terminal_output_count = (
            len(self.initial_batch_accepted_output_ids) + len(self.initial_batch_disposed_scrap_ids)
        )
        initial_batch_yield_ratio = (
            len(self.initial_batch_accepted_output_ids) / initial_batch_terminal_output_count
            if initial_batch_terminal_output_count
            else 0.0
        )
        if not self.is_mfg_flow_shop:
            objective_status = "not_applicable"
            makespan_status = "not_applicable"
        elif self.minimize_makespan_enabled:
            if self.termination_reason == "initial_material_batch_terminal_complete":
                objective_status = "complete"
                makespan_status = "complete"
            elif self.termination_reason == "makespan_max_days_reached":
                objective_status = "incomplete"
                makespan_status = "incomplete"
            elif self.termination_reason:
                objective_status = "failed"
                makespan_status = "incomplete"
            else:
                objective_status = "running"
                makespan_status = "pending"
        else:
            objective_status = "complete" if self.termination_reason == "completed_horizon" else "failed"
            makespan_status = "not_applicable"

        humanoid_task_totals: dict[str, float] = defaultdict(float)
        for rec in self.task_records:
            if rec["status"] == "completed":
                humanoid_code = str(rec.get("humanoid_task_code", "")).strip()
                if humanoid_code:
                    humanoid_task_totals[humanoid_code] += rec["duration"]

        discharged_metrics = self._agent_discharged_metrics()
        buffer_wait_metrics = self._buffer_wait_metrics()
        lead_time_metrics = self._completed_product_lead_time_metrics()
        machine_time_metrics = self._machine_time_metrics()
        machine_state_metrics = self._machine_state_time_metrics()
        humanoid_state_time_by_worker = self._humanoid_state_time_metrics()
        humanoid_state_time_by_axis = self._humanoid_state_axis_totals(humanoid_state_time_by_worker)
        humanoid_state_ratio_by_worker = self._humanoid_state_ratios(humanoid_state_time_by_worker)
        humanoid_execution_ratio_by_worker = self._humanoid_execution_ratios(humanoid_state_time_by_worker)
        humanoid_blocked_ratio_by_worker = self._humanoid_blocked_ratios(humanoid_state_time_by_worker)
        humanoid_unavailable_ratio_by_worker = self._humanoid_unavailable_ratios(humanoid_state_time_by_worker)
        humanoid_execution_ratio_avg = mean(humanoid_execution_ratio_by_worker.values()) if humanoid_execution_ratio_by_worker else 0.0
        humanoid_blocked_ratio_avg = mean(humanoid_blocked_ratio_by_worker.values()) if humanoid_blocked_ratio_by_worker else 0.0
        humanoid_unavailable_ratio_avg = mean(humanoid_unavailable_ratio_by_worker.values()) if humanoid_unavailable_ratio_by_worker else 0.0
        humanoid_primitive_minutes = self._humanoid_primitive_minutes()
        humanoid_task_taxonomy = self._humanoid_task_taxonomy_metrics(dict(humanoid_task_totals))
        operational_complexity_metrics = self._operational_task_complexity_metrics()
        traffic_metrics = self._traffic_metrics()
        humanoid_incident_metrics = self._humanoid_incident_metrics()
        transport_metrics = self._transport_metrics()
        battery_service_metrics = self._battery_service_metrics()
        reliability_and_risk_metrics = self._reliability_and_risk_metrics()
        repair_collaboration_metrics = self._repair_collaboration_metrics()
        incident_event_total = sum(int(summary.get("incident_event_count", 0) or 0) for summary in self.daily_summaries)
        physical_incident_total = sum(int(summary.get("physical_incident_count", 0) or 0) for summary in self.daily_summaries)
        coordination_incident_total = sum(int(summary.get("coordination_incident_count", 0) or 0) for summary in self.daily_summaries)
        unique_replan_blocker_total = sum(int(summary.get("unique_replan_blocker_count", 0) or 0) for summary in self.daily_summaries)
        planner_escalation_total = sum(int(summary.get("planner_escalation_count", 0) or 0) for summary in self.daily_summaries)
        local_response_task_total = sum(int(summary.get("local_response_task_count", 0) or 0) for summary in self.daily_summaries)
        commitment_dispatch_task_total = sum(int(summary.get("commitment_dispatch_task_count", 0) or 0) for summary in self.daily_summaries)
        inspection_pass_total = sum(int(summary.get("inspection_passes", 0) or 0) for summary in self.daily_summaries)
        downstream_closure_ratio = round(
            (self.product_count / max(1.0, float(self.station_throughput.get(self.last_processing_station, 0) or 0.0)))
            if float(self.station_throughput.get(self.last_processing_station, 0) or 0.0) > 0.0
            else 0.0,
            6,
        )
        stage_throughput: dict[str, int] = {
            f"S{int(station)}": int(count) for station, count in sorted(self.station_throughput.items(), key=lambda item: int(item[0]))
        }
        stage_throughput["Inspection"] = int(inspection_pass_total)

        adp_summary: dict[str, Any] = {"enabled": False}
        if self._adp_active():
            self.adp_coordinator.finalize_episode()
            adp_summary = self.adp_coordinator.summary()

        self._refresh_blocked_after_service_metrics()
        blocked_after_service_by_machine = {
            machine_id: float(minutes)
            for machine_id, minutes in self.buffer_metrics["blocked_after_service_min_by_machine"].items()
        }
        for machine_id, started_at in self.buffer_metrics["blocked_after_service_started_at"].items():
            blocked_after_service_by_machine[machine_id] = blocked_after_service_by_machine.get(machine_id, 0.0) + max(
                0.0,
                float(self.env.now) - float(started_at),
            )
        buffer_avg_occupancy: dict[str, float] = {}
        for buffer_id in sorted(self.buffer_capacities):
            values: list[int] = []
            for snapshot in self.minute_snapshots:
                if buffer_id.startswith("material_queue_"):
                    station = int(buffer_id.rsplit("_", 1)[1])
                    values.append(int(snapshot.get("material_queue_lengths", {}).get(station, 0)))
                elif buffer_id.startswith("intermediate_queue_"):
                    station = int(buffer_id.rsplit("_", 1)[1])
                    values.append(int(snapshot.get("intermediate_queue_lengths", {}).get(station, 0)))
                elif buffer_id.startswith("output_buffer_station_"):
                    station = int(buffer_id.rsplit("_", 1)[1])
                    values.append(int(snapshot.get("output_buffer_lengths", {}).get(station, 0)))
                elif buffer_id == "inspection_scrap_queue":
                    values.append(int(snapshot.get("inspection_scrap_queue_length", 0)))
            buffer_avg_occupancy[buffer_id] = round(float(mean(values)) if values else 0.0, 6)
        candidate_scans = int(self.buffer_metrics.get("candidate_scan_count", 0) or 0)
        buffer_reservation_leak_count = sum(
            len(rows) for rows in self.buffer_slot_reservations.values()
        )

        return {
            "task_primitive_timing": self.timing.summary(),
            "sim_elapsed_min": round(float(self.env.now), 6),
            "objective_mode": self.objective_mode if self.is_mfg_flow_shop else "",
            "objective_status": objective_status,
            "configured_max_sim_days": int(self.objective.get("max_sim_days", self.num_days) or self.num_days),
            "configured_throughput_days": int(self.configured_throughput_days),
            "initial_batch_material_count": initial_batch_material_count,
            "initial_batch_terminal_material_count": initial_batch_terminal_material_count,
            "initial_batch_progress_ratio": round(initial_batch_progress_ratio, 6),
            "initial_batch_expected_output_count": self.initial_batch_expected_output_count(),
            "initial_batch_accepted_product_count": len(self.initial_batch_accepted_output_ids),
            "initial_batch_disposed_scrap_count": len(self.initial_batch_disposed_scrap_ids),
            "initial_batch_yield_ratio": round(initial_batch_yield_ratio, 6),
            "makespan_min": round(float(self.makespan_min), 6) if self.makespan_min is not None else None,
            "makespan_status": makespan_status,
            "total_products": self.product_count,
            "scrap_count": self.scrap_count,
            "disposed_scrap_count": int(self.disposed_scrap_count),
            "scrap_rate": round((self.scrap_count / total_checked) if total_checked > 0 else 0.0, 6),
            "warehouse_material_shelf_count": self._material_shelf_count(),
            "warehouse_material_shelf_capacity": int(self.material_shelf_capacity),
            "warehouse_material_restock_count": int(self.warehouse_material_restock_count),
            "inspection_scrap_queue_length": len(self.inspection_scrap_queue),
            "scrap_transport_batches": int(self.scrap_transport_batches),
            "scrap_transport_items": int(self.scrap_transport_items),
            "buffer_capacities": dict(self.buffer_capacities),
            "buffer_avg_occupancy": buffer_avg_occupancy,
            "buffer_max_occupancy": {
                key: int(value)
                for key, value in self.buffer_metrics["max_occupancy_by_buffer"].items()
            },
            "buffer_max_reserved_slots": {
                key: int(value)
                for key, value in self.buffer_metrics["max_reserved_by_buffer"].items()
            },
            "buffer_max_committed_plus_reserved": {
                key: int(value)
                for key, value in self.buffer_metrics["max_committed_plus_reserved_by_buffer"].items()
            },
            "buffer_overflow_attempt_count": int(self.buffer_metrics["overflow_attempt_count"]),
            "buffer_reservation_failure_count": int(self.buffer_metrics["reservation_failure_count"]),
            "buffer_reservation_leak_count": int(buffer_reservation_leak_count),
            "machine_blocked_after_service_count": int(self.buffer_metrics["blocked_after_service_count"]),
            "machine_blocked_after_service_min": round(sum(blocked_after_service_by_machine.values()), 6),
            "machine_blocked_after_service_min_by_machine": {
                key: round(value, 6) for key, value in sorted(blocked_after_service_by_machine.items())
            },
            "candidate_scan_count": candidate_scans,
            "candidate_count_avg": round(
                float(self.buffer_metrics.get("candidate_count_total", 0)) / max(1, candidate_scans),
                6,
            ),
            "candidate_count_max": int(self.buffer_metrics.get("candidate_count_max", 0)),
            "station_throughput": dict(self.station_throughput),
            "stage_throughput": stage_throughput,
            "avg_daily_products": round(self.product_count / self._objective_reporting_days(), 4),
            "throughput_per_sim_hour": round(self.product_count / max(1e-6, total_time / 60.0), 4),
            "avg_wip_material": round(
                mean(sum(s["material_queue_lengths"].values()) for s in self.minute_snapshots),
                4,
            )
            if self.minute_snapshots
            else 0.0,
            "avg_wip_intermediate": round(mean(sum(s["intermediate_queue_lengths"].values()) for s in self.minute_snapshots), 4)
            if self.minute_snapshots
            else 0.0,
            "avg_wip_output": round(mean(sum(s["output_buffer_lengths"].values()) for s in self.minute_snapshots), 4)
            if self.minute_snapshots
            else 0.0,
            "machine_processing_min": machine_time_metrics["total_processing_min"],
            "machine_broken_min": machine_time_metrics["total_broken_min"],
            "machine_repair_min": machine_time_metrics["total_repair_min"],
            "machine_pm_min": machine_time_metrics["total_pm_min"],
            "machine_utilization": machine_time_metrics["utilization_ratio"],
            "machine_broken_ratio": machine_time_metrics["broken_ratio"],
            "machine_repair_ratio": machine_time_metrics["repair_ratio"],
            "machine_pm_ratio": machine_time_metrics["pm_ratio"],
            "machine_other_ratio": machine_time_metrics["other_ratio"],
            "machine_ratio_by_station": machine_time_metrics["ratio_by_station"],
            "machine_time_by_machine": machine_time_metrics["time_by_machine"],
            "machine_state_time_by_machine": machine_state_metrics["state_time_by_machine"],
            "machine_utilization_by_machine": machine_state_metrics["utilization_by_machine"],
            "incident_event_total": int(incident_event_total),
            "physical_incident_total": int(physical_incident_total),
            "coordination_incident_total": int(coordination_incident_total),
            "unique_replan_blocker_total": int(unique_replan_blocker_total),
            "planner_escalation_total": int(planner_escalation_total),
            "worker_local_response_total": int(local_response_task_total),
            "commitment_dispatch_total": int(commitment_dispatch_task_total),
            "humanoid_task_minutes": dict(humanoid_task_totals),
            "agent_discharged_time_min_total": discharged_metrics["total_min"],
            "agent_discharged_time_min_avg": discharged_metrics["avg_min_per_agent"],
            "agent_discharged_time_min_by_agent": discharged_metrics["by_agent"],
            "agent_discharged_ratio": discharged_metrics["discharged_ratio"],
            "agent_discharged_ratio_by_agent": discharged_metrics["ratio_by_agent"],
            "humanoid_state_time_by_worker": humanoid_state_time_by_worker,
            "humanoid_state_time_by_axis": humanoid_state_time_by_axis,
            "humanoid_state_ratio_by_worker": humanoid_state_ratio_by_worker,
            "humanoid_execution_ratio_by_worker": humanoid_execution_ratio_by_worker,
            "humanoid_execution_ratio_avg": round(float(humanoid_execution_ratio_avg), 6),
            "humanoid_blocked_ratio_by_worker": humanoid_blocked_ratio_by_worker,
            "humanoid_blocked_ratio_avg": round(float(humanoid_blocked_ratio_avg), 6),
            "humanoid_unavailable_ratio_by_worker": humanoid_unavailable_ratio_by_worker,
            "humanoid_unavailable_ratio_avg": round(float(humanoid_unavailable_ratio_avg), 6),
            "humanoid_primitive_minutes": humanoid_primitive_minutes,
            "humanoid_task_taxonomy": humanoid_task_taxonomy,
            "adp_checkpoint_id": str(adp_summary.get("checkpoint_id", "")),
            "adp_idle_metrics_version": int(adp_summary.get("idle_metrics_version", 1)),
            "adp_unassigned_count": int(adp_summary.get("unassigned_count", 0)),
            "adp_decision_count": int(adp_summary.get("decision_count", 0) or 0),
            "adp_wait_count": int(adp_summary.get("wait_count", 0) or 0),
            "adp_candidate_available_wait_count": int(
                adp_summary.get("candidate_available_wait_count", 0) or 0
            ),
            "adp_no_candidate_unassigned_count": int(
                adp_summary.get("no_candidate_unassigned_count", 0) or 0
            ),
            "adp_candidate_available_wait_ratio": round(
                float(adp_summary.get("candidate_available_wait_ratio", 0.0) or 0.0),
                6,
            ),
            "adp_no_candidate_unassigned_ratio": round(
                float(adp_summary.get("no_candidate_unassigned_ratio", 0.0) or 0.0),
                6,
            ),
            "adp_joint_all_wait_with_candidate_ratio": round(
                float(
                    adp_summary.get("joint_all_wait_with_candidate_ratio", 0.0)
                    or 0.0
                ),
                6,
            ),
            "adp_avg_candidate_count": round(float(adp_summary.get("avg_candidate_count", 0.0) or 0.0), 6),
            "adp_beam_value_entropy_avg": round(
                float(adp_summary.get("beam_value_entropy_avg", 0.0) or 0.0),
                6,
            ),
            "adp_beam_value_entropy_decision_count": int(
                adp_summary.get("beam_value_entropy_decision_count", 0) or 0
            ),
            "adp_inference_latency_ms_avg": round(float(adp_summary.get("inference_latency_ms_avg", 0.0) or 0.0), 6),
            "adp_validation_completed_products_avg": round(float(adp_summary.get("validation_completed_products_avg", 0.0) or 0.0), 6),
            "simulation_based_adp": adp_summary,
            **operational_complexity_metrics,
            **humanoid_incident_metrics,
            **traffic_metrics,
            **transport_metrics,
            **battery_service_metrics,
            **reliability_and_risk_metrics,
            **repair_collaboration_metrics,
            "rolling_horizon_window_count": int(self.rolling_horizon_metrics.get("started_window_count", 0)),
            "rolling_horizon_candidate_collected_count": int(self.rolling_horizon_metrics.get("candidate_collected_count", 0)),
            "rolling_horizon_dispatched_task_count": int(self.rolling_horizon_metrics.get("dispatched_task_count", 0)),
            "rolling_horizon_immediate_dispatched_task_count": int(self.rolling_horizon_metrics.get("immediate_dispatched_task_count", 0)),
            "rolling_horizon_stale_skipped_task_count": int(self.rolling_horizon_metrics.get("stale_skipped_task_count", 0)),
            "rolling_horizon_requeued_task_count": int(self.rolling_horizon_metrics.get("requeued_task_count", 0)),
            "rolling_horizon_max_worker_queue_length": int(self.rolling_horizon_metrics.get("max_worker_queue_length", 0)),
            "rolling_horizon_scheduler_mode": self.rolling_horizon_scheduler_mode,
            "rolling_horizon_strict_boundary_count": int(
                self.rolling_horizon_metrics.get("strict_boundary_count", 0)
            ),
            "rolling_horizon_late_boundary_count": int(
                self.rolling_horizon_metrics.get("late_boundary_count", 0)
            ),
            "rolling_horizon_max_boundary_lag_min": round(
                float(self.rolling_horizon_max_boundary_lag_min), 9
            ),
            "throughput_optimizer_window_count": int(self.rolling_horizon_metrics.get("throughput_optimizer_window_count", 0)),
            "throughput_optimizer_solved_count": int(self.rolling_horizon_metrics.get("throughput_optimizer_solved_count", 0)),
            "throughput_optimizer_failed_count": int(self.rolling_horizon_metrics.get("throughput_optimizer_failed_count", 0)),
            "throughput_optimizer_objective_avg": round(float(mean(self.throughput_optimizer_objective_values)) if self.throughput_optimizer_objective_values else 0.0, 3),
            "bottleneck_score_avg": round(float(mean(self.throughput_score_values)) if self.throughput_score_values else 0.0, 3),
            "rolling_horizon": {
                "enabled": bool(self._rolling_horizon_active()),
                "dedicated_roles": bool(self._rolling_horizon_dedicated_roles_active()),
                "throughput_optimizer": bool(self._rolling_horizon_throughput_optimizer_active()),
                "window_min": round(float(self.rolling_horizon_window_min), 3),
                "scheduler_mode": self.rolling_horizon_scheduler_mode,
                "candidate_collection_mode": self.rolling_horizon_candidate_collection_mode,
                "dispatch_policy": self.rolling_horizon_dispatch_policy,
                "window_count": int(self.rolling_horizon_metrics.get("started_window_count", 0)),
                "dispatched_window_count": int(self.rolling_horizon_metrics.get("window_count", 0)),
                "candidate_collected_count": int(self.rolling_horizon_metrics.get("candidate_collected_count", 0)),
                "dispatched_task_count": int(self.rolling_horizon_metrics.get("dispatched_task_count", 0)),
                "immediate_dispatched_task_count": int(self.rolling_horizon_metrics.get("immediate_dispatched_task_count", 0)),
                "stale_skipped_task_count": int(self.rolling_horizon_metrics.get("stale_skipped_task_count", 0)),
                "requeued_task_count": int(self.rolling_horizon_metrics.get("requeued_task_count", 0)),
                "empty_window_count": int(self.rolling_horizon_metrics.get("empty_window_count", 0)),
                "strict_boundary_count": int(
                    self.rolling_horizon_metrics.get("strict_boundary_count", 0)
                ),
                "late_boundary_count": int(
                    self.rolling_horizon_metrics.get("late_boundary_count", 0)
                ),
                "max_boundary_lag_min": round(float(self.rolling_horizon_max_boundary_lag_min), 9),
                "throughput_optimizer_window_count": int(self.rolling_horizon_metrics.get("throughput_optimizer_window_count", 0)),
                "throughput_optimizer_solved_count": int(self.rolling_horizon_metrics.get("throughput_optimizer_solved_count", 0)),
                "throughput_optimizer_failed_count": int(self.rolling_horizon_metrics.get("throughput_optimizer_failed_count", 0)),
                "throughput_optimizer_objective_avg": round(float(mean(self.throughput_optimizer_objective_values)) if self.throughput_optimizer_objective_values else 0.0, 3),
                "bottleneck_score_avg": round(float(mean(self.throughput_score_values)) if self.throughput_score_values else 0.0, 3),
                "pending_candidate_count": int(len(self.rolling_horizon_pending)),
                "queued_dispatch_count": int(sum(len(queue) for queue in self.rolling_horizon_dispatch_queues.values())),
                "max_worker_queue_length": int(self.rolling_horizon_metrics.get("max_worker_queue_length", 0)),
                "max_queue_length_by_worker": dict(self.rolling_horizon_max_queue_length_by_worker),
                "task_code_priority_order": list(getattr(self, "rolling_horizon_task_code_priority_order", [])),
                "task_rule_priority_order": list(
                    getattr(getattr(self, "mfg_flow_task_policy", None), "priority_order", [])
                ),
                "rank_boost_per_window": int(getattr(self, "rolling_horizon_rank_boost_per_window", 1) or 0),
                "immediate_queue_policy": str(getattr(self, "rolling_horizon_immediate_queue_policy", "")),
                "immediate_protect_from_window_requeue": bool(
                    getattr(self, "rolling_horizon_immediate_protect_from_window_requeue", False)
                ),
                "worker_task_priority": {
                    str(worker_id): list(codes)
                    for worker_id, codes in getattr(self, "rolling_horizon_worker_task_priority", {}).items()
                },
                "battery_delivery_provider_agent_ids": list(getattr(self, "rolling_horizon_battery_delivery_provider_agent_ids", [])),
                "battery_delivery_receiver_agent_ids": list(getattr(self, "rolling_horizon_battery_delivery_receiver_agent_ids", [])),
                "dedicated_role_summary": {
                    "role_violation_count": int(self.rolling_horizon_dedicated_role_metrics.get("role_violation_count", 0) or 0),
                    "handover_dispatch_count": int(self.rolling_horizon_dedicated_role_metrics.get("handover_dispatch_count", 0) or 0),
                    "battery_delivery_from_provider_count": int(self.rolling_horizon_dedicated_role_metrics.get("battery_delivery_from_provider_count", 0) or 0),
                    "collected_by_worker": dict(self.rolling_horizon_dedicated_role_metrics.get("collected_by_worker", {})),
                    "dispatched_by_worker": dict(self.rolling_horizon_dedicated_role_metrics.get("dispatched_by_worker", {})),
                    "skipped_by_worker": dict(self.rolling_horizon_dedicated_role_metrics.get("skipped_by_worker", {})),
                },
            },
            "buffer_wait_avg_min": buffer_wait_metrics["avg_wait_min"],
            "buffer_wait_avg_min_including_open": buffer_wait_metrics["avg_wait_min_including_open"],
            "buffer_wait_completed_count": buffer_wait_metrics["completed_wait_count"],
            "buffer_wait_open_count": buffer_wait_metrics["open_wait_count"],
            "buffer_wait_avg_min_by_queue": buffer_wait_metrics["avg_wait_min_by_queue"],
            "buffer_wait_avg_min_including_open_by_queue": buffer_wait_metrics["avg_wait_min_including_open_by_queue"],
            "buffer_wait_completed_count_by_queue": buffer_wait_metrics["completed_wait_count_by_queue"],
            "buffer_wait_open_count_by_queue": buffer_wait_metrics["open_wait_count_by_queue"],
            "completed_product_lead_time_avg_min": lead_time_metrics["avg_min"],
            "completed_product_lead_time_p95_min": lead_time_metrics["p95_min"],
            "downstream_closure_ratio": downstream_closure_ratio,
            "terminated": self.terminated,
            "termination_reason": self.termination_reason,
        }





