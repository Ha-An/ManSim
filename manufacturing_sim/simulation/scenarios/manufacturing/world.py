from __future__ import annotations

import hashlib
import itertools
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
from manufacturing_sim.simulation.scenarios.manufacturing.entities import Agent, Item, Machine, MachineState, Task
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger


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
        decision_cfg = cfg.get("decision", {}) if isinstance(cfg.get("decision", {}), dict) else {}
        self.decision_mode = normalize_decision_mode(str(decision_cfg.get("mode", "adaptive_priority")))

        seed = int(cfg.get("seed", 7))
        self.rng = random.Random(seed)

        horizon_cfg = cfg["horizon"]
        self.num_days = int(horizon_cfg["num_days"])
        self.minutes_per_day = int(horizon_cfg["minutes_per_day"])

        factory_cfg = cfg["factory"]
        self.num_agents = int(factory_cfg["num_agents"])
        self.machines_per_station = int(factory_cfg["machines_per_station"])

        process_cfg = factory_cfg["processing_time_min"]
        station_time_pairs: list[tuple[int, float]] = []
        for key, value in process_cfg.items():
            key_str = str(key)
            if not key_str.startswith("station"):
                continue
            suffix = key_str.replace("station", "", 1)
            if not suffix.isdigit():
                continue
            station_time_pairs.append((int(suffix), float(value)))
        if not station_time_pairs:
            raise ValueError("factory.processing_time_min must define at least one stationN entry.")
        station_time_pairs.sort(key=lambda x: x[0])
        self.stations = [station for station, _ in station_time_pairs]
        self.last_processing_station = max(self.stations)
        self.inspection_queue_station = 4
        self.processing_time_min = {station: proc_time for station, proc_time in station_time_pairs}
        self.inspection_base_time_min = float(factory_cfg["inspection_base_time_min"])
        self.inspection_min_time_min = float(factory_cfg["inspection_min_time_min"])

        self.movement_cfg = cfg["movement"]
        self.quality_cfg = cfg["quality"]
        self.machine_failure_cfg = cfg["machine_failure"]
        self.agent_cfg = cfg["agent"]
        self.inventory_targets = cfg["inventory_targets"]
        self.dispatcher_cfg = cfg["dispatcher"]
        self.heuristic_rules = cfg.get("heuristic_rules", {}) if isinstance(cfg.get("heuristic_rules", {}), dict) else {}
        llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
        orchestration_cfg = llm_cfg.get("orchestration", {}) if isinstance(llm_cfg.get("orchestration", {}), dict) else {}
        # The manager may queue more work than the runtime should examine; limit the local queue window here.
        self.worker_queue_limit = max(
            1,
            int(getattr(decision_module, "worker_queue_limit", orchestration_cfg.get("worker_queue_limit", 4)) or 4),
        )

        mean_ttf = float(self.machine_failure_cfg["mean_time_to_fail_min"])
        self.machine_failure_base_lambda = 1.0 / max(1.0, mean_ttf)
        self.pm_lambda_multiplier = float(self.machine_failure_cfg["pm_lambda_multiplier"])
        self.pm_effect_duration_min = float(self.machine_failure_cfg["pm_effect_duration_min"])
        self.pm_interval_target_min = float(self.machine_failure_cfg["pm_interval_target_min"])

        self.battery_swap_period_min = float(self.agent_cfg["battery_swap_period_min"])

        self.current_day = 1
        self.current_strategy = StrategyState()
        self.current_job_plan = JobPlan(
            task_priority_weights=default_task_priority_weights(),
            quotas={},
            rationale="default",
            agent_priority_multipliers=default_agent_priority_multipliers([f"A{i}" for i in range(1, self.num_agents + 1)]),
        )
        worker_cfg = cfg.get("worker", {}) if isinstance(cfg.get("worker", {}), dict) else {}
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
        self.commitment_claims: dict[str, dict[str, Any]] = {}
        self.selection_blocker_counter = itertools.count(1)
        self.selection_blockers: dict[str, dict[str, Any]] = {}
        self.active_selection_blocker_by_agent: dict[str, str] = {}
        self.incident_escalations: set[str] = set()
        self.day_unique_replan_blockers: set[str] = set()
        self.day_planner_escalations: set[str] = set()
        self.manager_queue_skipped_counts: dict[str, int] = defaultdict(int)
        decision_cfg = self.cfg.get("decision", {}) if isinstance(self.cfg.get("decision", {}), dict) else {}
        norms_cfg = decision_cfg.get("norms", {}) if isinstance(decision_cfg.get("norms", {}), dict) else {}
        self.norms_enabled = bool(norms_cfg.get("enabled", True))
        self.norms: dict[str, Any] = {
            "min_pm_per_machine_per_day": int(
                self._rule("world.initial_norms.min_pm_per_machine_per_day", 1)
            ),
            "inspect_product_priority_weight": float(
                self._rule("world.initial_norms.inspect_product_priority_weight", 1.0)
            ),
            "inspection_backlog_target": int(
                self._rule("world.initial_norms.inspection_backlog_target", 8)
            ),
            "max_output_buffer_target": int(
                self._rule("world.initial_norms.max_output_buffer_target", 4)
            ),
            "battery_reserve_min": float(
                self._rule("world.initial_norms.battery_reserve_min", 50.0)
            ),
        } if self.norms_enabled else {}

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
        self.material_supply_owner: dict[int, str | None] = {station: None for station in self.stations}

        self.items: dict[str, Item] = {}
        self.item_counter = itertools.count(1)
        self.task_counter = itertools.count(1)
        self.machine_cycle_counter = itertools.count(1)

        self.machines: dict[str, Machine] = {}
        self.machines_by_station: dict[int, list[str]] = {station: [] for station in self.stations}
        self._build_machines()

        self.agents: dict[str, Agent] = {}
        self._build_agents()

        self.product_count = 0
        self.scrap_count = 0
        self.station_throughput = defaultdict(int)
        self.inspection_active_agents = 0

        self.minute_snapshots: list[dict[str, Any]] = []
        self.task_records: list[dict[str, Any]] = []
        self.daily_summaries: list[dict[str, Any]] = []
        self.day_baseline: dict[str, Any] = {}

        urgent_cfg = decision_cfg.get("urgent_discuss", {}) if isinstance(decision_cfg.get("urgent_discuss", {}), dict) else {}
        self.urgent_discuss_enabled = bool(urgent_cfg.get("enabled", True))
        self.last_urgent_chat_t = -10_000.0
        self.urgent_chat_cooldown = float(self.dispatcher_cfg["urgent_chat_cooldown_min"])
        self.snapshot_interval = float(self.dispatcher_cfg["snapshot_interval_min"])
        self.terminated = False
        self.termination_reason = ""
        self.termination_event = self.env.event()
        self.active_battery_delivery_owner: str | None = None

    def _rule(self, dotted_path: str, default: Any) -> Any:
        node: Any = self.heuristic_rules
        for key in dotted_path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

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

    def _build_agents(self) -> None:
        for idx in range(1, self.num_agents + 1):
            agent_id = f"A{idx}"
            self.agents[agent_id] = Agent(agent_id=agent_id, location="Home")

    def bootstrap(self) -> None:
        from manufacturing_sim.simulation.scenarios.manufacturing import processes

        initial_inventory_cfg = self.cfg.get("initial_inventory", {})
        initial_material_cfg = initial_inventory_cfg.get("material", {}) if isinstance(initial_inventory_cfg, dict) else {}
        for station in self.stations:
            initial_material = int(initial_material_cfg.get(f"station{station}", 0))
            for _ in range(max(0, initial_material)):
                self._warehouse_push_material(station)

        for machine_id in self.machines:
            self.env.process(processes.machine_lifecycle(self.env, self, machine_id))
            self.env.process(processes.machine_failure_monitor(self.env, self, machine_id))

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
                "task_priority_weights": job_plan.task_priority_weights,
                "shared_task_priority_weights": job_plan.task_priority_weights,
                "agent_priority_multipliers": job_plan.agent_priority_multipliers,
                "agent_effective_task_priority_weights": {
                    agent_id: job_plan.effective_task_priority_weights(agent_id) for agent_id in sorted(self.agents.keys())
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
        return dict(self.current_job_plan.agent_priority_multipliers.get(str(agent_id), {}))

    def current_effective_task_priority_weights(self, agent_id: str) -> dict[str, float]:
        self.current_job_plan.ensure_agent_priority_multipliers(tuple(sorted(self.agents.keys())))
        return self.current_job_plan.effective_task_priority_weights(str(agent_id))

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

    def _machine_state_bucket(self, machine: Machine) -> str:
        if machine.state == MachineState.PROCESSING:
            return "processing"
        if machine.state == MachineState.DONE_WAIT_UNLOAD:
            return "finished_wait_unload"
        if machine.state == MachineState.BROKEN:
            return "broken"
        if machine.state == MachineState.UNDER_REPAIR:
            return "under_repair"
        if machine.state == MachineState.UNDER_PM:
            return "under_pm"
        return "wait_input"

    def _machine_wait_reasons(self, machine: Machine) -> list[str]:
        reasons: list[str] = []
        if machine.state == MachineState.BROKEN:
            reasons.append("broken")
        if machine.state == MachineState.UNDER_REPAIR:
            reasons.append("under_repair")
        if machine.state == MachineState.UNDER_PM:
            reasons.append("under_pm")
        if machine.output_intermediate is not None or machine.state == MachineState.DONE_WAIT_UNLOAD:
            reasons.append("waiting_unload")
        if machine.state == MachineState.WAIT_INPUT:
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
                "state": machine.state.value,
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
            "machine_states": {mid: machine.state.value for mid, machine in self.machines.items()},
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
        if task.task_type in {"UNLOAD_MACHINE", "TRANSFER"}:
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
                current["capacity"] = max(int(current.get("capacity", 1) or 1), len(owners) if bool(current.get("shareable", False)) else 1)
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

    def local_state_for_urgent(self) -> dict[str, Any]:
        observation = self.observe()
        return {
            "inspection_backlog": len(self.intermediate_queues[self.inspection_queue_station]),
            "broken_machines": sum(1 for m in self.machines.values() if m.broken),
            "discharged_agents": sum(1 for a in self.agents.values() if a.discharged),
            "recent_incidents": list(self.incident_events[-5:]),
            "commitments": self.current_commitments(),
            "incident_work_orders": self.current_incident_work_orders(),
            "norms": dict(self.norms),
            "observation": observation,
        }

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
        if task.task_type == "REPAIR_MACHINE":
            return "repair_machine"
        if task.task_type == "UNLOAD_MACHINE":
            return "unload_machine"
        if task.task_type == "SETUP_MACHINE":
            return "setup_machine"
        if task.task_type == "PREVENTIVE_MAINTENANCE":
            return "preventive_maintenance"
        if task.task_type == "INSPECT_PRODUCT":
            return "inspect_product"
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "battery_delivery":
                return "battery_delivery_discharged" if bool(task.payload.get("target_agent_discharged", False)) else "battery_delivery_low_battery"
            if transfer_kind == "material_supply":
                return "material_supply"
            if transfer_kind == "inter_station":
                return "inter_station_transfer"
        return str(task.priority_key).strip() or str(task.task_type).strip().lower()

    def capture_snapshot(self) -> None:
        t = self.env.now
        self.minute_snapshots.append(
            {
                "t": round(t, 3),
                "day": self.day_for_time(t),
                "material_queue_lengths": {k: len(v) for k, v in self.material_queues.items()},
                "intermediate_queue_lengths": {k: len(v) for k, v in self.intermediate_queues.items()},
                "output_buffer_lengths": {k: len(v) for k, v in self.output_buffers.items()},
                "machine_states": {mid: m.state.value for mid, m in self.machines.items()},
                "inspection_active_agents": self.inspection_active_agents,
                "incident_count": len(self.incident_events),
                "commitment_count": sum(len(rows) for rows in self.current_commitments().values()),
            }
        )

    def _next_item_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self.item_counter)}"

    def _next_task_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self.task_counter)}"

    def _next_cycle_id(self) -> str:
        return f"CYCLE-{next(self.machine_cycle_counter)}"

    def _set_agent_carrying(self, agent: Agent, item_type: str, item_id: str) -> bool:
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
        agent.carrying_item_id = normalized_item_id
        agent.carrying_item_type = normalized_type
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="AGENT_PICK_ITEM",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={"item_id": agent.carrying_item_id, "item_type": agent.carrying_item_type},
        )
        return True

    def _clear_agent_carrying(self, agent: Agent, destination: str = "", emit_event: bool = True) -> None:
        item_id = agent.carrying_item_id
        item_type = agent.carrying_item_type
        if item_id is None and item_type is None:
            return
        if emit_event:
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="AGENT_DROP_ITEM",
                entity_id=agent.agent_id,
                location=self.agent_display_location(agent),
                details={"item_id": item_id or "", "item_type": (item_type or ""), "to": destination},
            )
        agent.carrying_item_id = None
        agent.carrying_item_type = None

    def _push_material_queue(self, station: int, item_id: str) -> None:
        self.material_queues[station].append(item_id)
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_PUSH",
            entity_id=f"material_queue_{station}",
            location=f"Station{station}",
            details={"item_id": item_id, "queue": "material"},
        )

    def _pop_material_queue(self, station: int) -> str | None:
        if not self.material_queues[station]:
            return None
        item_id = self.material_queues[station].popleft()
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_POP",
            entity_id=f"material_queue_{station}",
            location=f"Station{station}",
            details={"item_id": item_id, "queue": "material"},
        )
        return item_id

    def _push_intermediate_queue(self, station: int, item_id: str) -> None:
        if station not in self.intermediate_queues:
            raise ValueError(f"intermediate queue for station {station} is not defined")
        self.intermediate_queues[station].append(item_id)
        location = "Inspection" if station == self.inspection_queue_station else f"Station{station}"
        queue_name = "product" if station == self.inspection_queue_station else "intermediate"
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_PUSH",
            entity_id=f"intermediate_queue_{station}",
            location=location,
            details={"item_id": item_id, "queue": queue_name},
        )

    def _pop_intermediate_queue(self, station: int) -> str | None:
        if station not in self.intermediate_queues:
            return None
        if not self.intermediate_queues[station]:
            return None
        item_id = self.intermediate_queues[station].popleft()
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
        return item_id

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

    def _worker_state_time_metrics(self) -> dict[str, Any]:
        total_time = max(1.0, float(self.env.now))
        task_intervals = self._agent_event_intervals(start_events={"AGENT_TASK_START"}, end_events={"AGENT_TASK_END"})
        move_intervals = self._agent_event_intervals(
            start_events={"AGENT_MOVE_START"},
            end_events={"AGENT_MOVE_END", "AGENT_MOVE_INTERRUPTED"},
        )
        discharged_intervals_by_agent: dict[str, list[tuple[float, float]]] = {agent_id: [] for agent_id in sorted(self.agents.keys())}
        for agent_id, start, end in self._agent_discharged_intervals():
            discharged_intervals_by_agent.setdefault(agent_id, []).append((float(start), float(end)))
        discharged_intervals_by_agent = {
            agent_id: self._merge_intervals(rows) for agent_id, rows in discharged_intervals_by_agent.items()
        }

        by_worker: dict[str, dict[str, float]] = {}
        util_by_worker: dict[str, dict[str, float]] = {}
        for agent_id in sorted(self.agents.keys()):
            agent_task_intervals = task_intervals.get(agent_id, [])
            agent_move_intervals = move_intervals.get(agent_id, [])
            agent_discharged_intervals = discharged_intervals_by_agent.get(agent_id, [])
            task_total = self._interval_total(agent_task_intervals)
            move_total = self._interval_total(agent_move_intervals)
            move_overlap = self._interval_overlap_total(agent_task_intervals, agent_move_intervals)
            working_total = max(0.0, task_total - move_overlap)
            discharged_total = self._interval_total(agent_discharged_intervals)
            idle_total = max(0.0, total_time - working_total - move_total - discharged_total)
            active_total = working_total + move_total
            available_total = max(0.0, total_time - discharged_total)
            no_idle_discharged_total = max(0.0, total_time - idle_total - discharged_total)
            by_worker[agent_id] = {
                "working_min": round(working_total, 3),
                "moving_min": round(move_total, 3),
                "discharged_min": round(discharged_total, 3),
                "idle_min": round(idle_total, 3),
            }
            util_by_worker[agent_id] = {
                "util_total": round(active_total / total_time, 6),
                "util_available": round((active_total / available_total) if available_total > 0.0 else 0.0, 6),
                "util_no_idle_discharged": round(
                    (active_total / no_idle_discharged_total) if no_idle_discharged_total > 0.0 else 0.0,
                    6,
                ),
            }
        return {
            "state_time_by_worker": by_worker,
            "utilization_by_worker": util_by_worker,
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
            if event_type == "MACHINE_START":
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
        totals_by_machine: dict[str, float] = {machine_id: 0.0 for machine_id in self.machines.keys()}
        active_setup: dict[str, tuple[str, float]] = {}
        sim_end = float(self.env.now)

        for event in self.logger.events:
            event_type = str(event.get("type", "")).strip()
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            machine_id = str(event.get("entity_id", "")).strip()
            t = float(event.get("t", 0.0) or 0.0)
            if machine_id not in totals_by_machine:
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
                    totals_by_machine[active_machine_id] += t - start_t

        for active_machine_id, start_t in active_setup.values():
            if sim_end >= start_t:
                totals_by_machine[active_machine_id] += sim_end - start_t

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
                    "machine_states": {machine_id: machine.state.value for machine_id, machine in self.machines.items()},
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

        exact_machine_time = self._machine_time_metrics().get("time_by_machine", {})
        exact_setup_time = self._machine_setup_time_metrics()
        exact_broken_repair = self._machine_broken_repair_state_metrics()
        exact_broken_time = exact_broken_repair.get("broken_by_machine", {})
        exact_repair_time = exact_broken_repair.get("repair_by_machine", {})
        approximate_only_states = ("idle", "wait_input", "done_wait_unload")
        for machine_id in sorted(by_machine.keys()):
            exact_metrics = exact_machine_time.get(machine_id, {}) if isinstance(exact_machine_time.get(machine_id, {}), dict) else {}
            exact_processing = float(exact_metrics.get("processing_min", 0.0) or 0.0)
            exact_broken = float(exact_broken_time.get(machine_id, 0.0) or 0.0)
            exact_pm = float(exact_metrics.get("pm_min", 0.0) or 0.0)
            exact_setup = float(exact_setup_time.get(machine_id, 0.0) or 0.0)
            exact_under_repair = float(exact_repair_time.get(machine_id, 0.0) or 0.0)
            by_machine[machine_id]["processing"] = exact_processing
            by_machine[machine_id]["broken"] = exact_broken
            by_machine[machine_id]["pm"] = exact_pm
            by_machine[machine_id]["setup"] = exact_setup
            by_machine[machine_id]["under_repair"] = exact_under_repair

            snapshot_remaining = sum(
                float(by_machine[machine_id].get(state_name, 0.0))
                for state_name in approximate_only_states
            )
            exact_remaining = max(0.0, total_time - exact_processing - exact_broken - exact_pm - exact_setup - exact_under_repair)
            if snapshot_remaining > 0.0:
                scale = exact_remaining / snapshot_remaining
                for state_name in approximate_only_states:
                    by_machine[machine_id][state_name] = float(by_machine[machine_id].get(state_name, 0.0)) * scale
            else:
                by_machine[machine_id]["wait_input"] = exact_remaining

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
        self._push_material_queue(station, item_id)
        return item_id

    def machine_failure_lambda(self, machine: Machine) -> float:
        multiplier = self.pm_lambda_multiplier if self.env.now < machine.pm_until else 1.0
        return self.machine_failure_base_lambda * multiplier

    def break_machine(self, machine: Machine, reason: str) -> None:
        if machine.broken or machine.state in (MachineState.UNDER_REPAIR, MachineState.UNDER_PM):
            return
        was_processing = machine.state == MachineState.PROCESSING
        machine.broken = True
        machine.failures += 1
        machine.failed_since = self.env.now
        machine.state = MachineState.BROKEN
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_BROKEN",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={"reason": reason},
        )
        self.emit_incident(
            "machine_broken",
            affected_entities=[machine.machine_id, f"Station{machine.station}"],
            escalation_level="worker_local" if self.incident_policy.get("prefer_worker_local_response", True) else "planner",
            details={"reason": reason, "station": machine.station},
            notify_workers=[agent_id for agent_id, agent in self.agents.items() if self.agent_display_location(agent) == f"Station{machine.station}"],
        )
        self.trigger_urgent_chat("machine_breakdown", machine.machine_id, {"station": machine.station})
        if was_processing and machine.active_process is not None and machine.active_process.is_alive:
            machine.active_process.interrupt("machine_breakdown")

    def battery_remaining(self, agent: Agent, at_t: float | None = None) -> float:
        t = self.env.now if at_t is None else float(at_t)
        return max(0.0, self.battery_swap_period_min - max(0.0, t - float(agent.last_battery_swap)))

    def _battery_service_margin_min(self) -> float:
        return max(3.0, float(self.movement_cfg.get("setup_min", 3.0)) + 1.0)

    def _battery_swap_service_min(self, agent: Agent) -> float:
        origin = self.agent_display_location(agent)
        return float(self.travel_time(origin, "BatteryStation")) + float(self.agent_cfg["battery_pickup_time_min"])

    def _battery_mandatory_threshold(self, agent: Agent) -> float:
        configured = float(self._rule("world.battery.mandatory_swap_threshold_min", 15.0))
        physical = self._battery_swap_service_min(agent) + self._battery_service_margin_min()
        return max(configured, physical)

    def _battery_proactive_swap_threshold(self, agent: Agent) -> float:
        return self._battery_mandatory_threshold(agent) + max(6.0, float(self.movement_cfg.get("unload_min", 2.0)) + 4.0)

    def _battery_low_alert_threshold(self, agent: Agent) -> float:
        configured = float(self._rule("world.battery.deliver_to_others_threshold_min", 15.0))
        return max(self._battery_proactive_swap_threshold(agent), min(configured, 24.0))

    def _battery_delivery_trigger_threshold(self, agent: Agent) -> float:
        return self._battery_mandatory_threshold(agent) + 2.0

    def _task_estimated_duration(self, agent: Agent, task: Task) -> float:
        task_type = str(task.task_type).strip().upper()
        if task_type == "BATTERY_SWAP":
            return self._battery_swap_service_min(agent)
        if task_type == "REPAIR_MACHINE":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            return float(self.travel_time(self.agent_display_location(agent), f"Station{machine.station}")) + float(self.machine_failure_cfg["repair_time_min"])
        if task_type == "PREVENTIVE_MAINTENANCE":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            return float(self.travel_time(self.agent_display_location(agent), f"Station{machine.station}")) + float(self.machine_failure_cfg["pm_time_min"])
        if task_type == "UNLOAD_MACHINE":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            return float(self.travel_time(self.agent_display_location(agent), f"Station{machine.station}")) + float(self.movement_cfg["unload_min"])
        if task_type == "SETUP_MACHINE":
            machine = self.machines.get(str(task.payload.get("machine_id", "")))
            if machine is None:
                return 0.0
            needs_intermediate = self._station_requires_intermediate(machine.station)
            setup_steps = 2 if needs_intermediate else 1
            return float(self.travel_time(self.agent_display_location(agent), f"Station{machine.station}")) + float(self.movement_cfg["setup_min"]) * setup_steps
        if task_type == "INSPECT_PRODUCT":
            return float(self.travel_time(self.agent_display_location(agent), "Inspection")) + float(self.inspection_base_time_min)
        if task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "material_supply":
                station = int(task.payload.get("station", 1) or 1)
                return float(self.travel_time(self.agent_display_location(agent), "Warehouse")) + float(self.travel_time("Warehouse", f"Station{station}"))
            if transfer_kind == "inter_station":
                from_station = int(task.payload.get("from_station", 1) or 1)
                from_location = "Inspection" if from_station == self.inspection_queue_station else f"Station{from_station}"
                if from_station == self.inspection_queue_station:
                    to_location = "Warehouse"
                else:
                    next_station = from_station + 1
                    to_location = f"Station{next_station}" if next_station <= self.last_processing_station else "Inspection"
                return float(self.travel_time(self.agent_display_location(agent), from_location)) + float(self.travel_time(from_location, to_location))
            if transfer_kind == "battery_delivery":
                target_agent = self.agents.get(str(task.payload.get("target_agent_id", "")))
                if target_agent is None:
                    return self._battery_swap_service_min(agent)
                return (
                    float(self.travel_time(self.agent_display_location(agent), "BatteryStation"))
                    + float(self.agent_cfg["battery_pickup_time_min"])
                    + float(self.travel_time("BatteryStation", self.agent_display_location(target_agent)))
                    + float(self.agent_cfg["battery_delivery_extra_min"])
                )
        return 0.0

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
        self.trigger_urgent_chat("battery_risk", agent.agent_id, {**details, "escalate_now": True})

    def _battery_interrupt_exempt(self, agent: Agent) -> bool:
        return bool(getattr(agent, "battery_swap_critical", False))

    def _should_interrupt_for_battery(self, agent: Agent, eps: float = 1e-6) -> bool:
        if self._battery_interrupt_exempt(agent):
            return False
        return agent.discharged or self.battery_remaining(agent) <= eps

    def check_all_agents_discharged(self) -> None:
        if self.terminated:
            return
        if self.agents and all(a.discharged for a in self.agents.values()):
            self.terminated = True
            self.termination_reason = "all_agents_discharged"
            self.logger.log(
                t=self.env.now,
                day=self.day_for_time(self.env.now),
                event_type="SIM_TERMINATED",
                entity_id="system",
                location="Factory",
                details={"reason": self.termination_reason},
            )
            if not self.termination_event.triggered:
                self.termination_event.succeed(self.termination_reason)

    def _clear_in_transit(self, agent: Agent) -> None:
        agent.in_transit_from = None
        agent.in_transit_to = None
        agent.in_transit_progress = 0.0
        agent.in_transit_total_min = 0.0

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

    def agent_display_location(self, agent: Agent) -> str:
        if self._has_in_transit_position(agent):
            return self._edge_location_label(str(agent.in_transit_from), str(agent.in_transit_to), float(agent.in_transit_progress))
        return str(agent.location)

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
    ) -> str | None:
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
        details: dict[str, Any] = {"reason": reason}
        if self._has_in_transit_position(agent):
            details.update(
                {
                    "in_transit_from": str(agent.in_transit_from),
                    "in_transit_to": str(agent.in_transit_to),
                    "in_transit_progress": round(float(agent.in_transit_progress), 4),
                }
            )
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
        self.trigger_urgent_chat("agent_discharged", agent.agent_id, {"reason": reason})
        if interrupt_process and agent.process_ref is not None and agent.process_ref.is_alive:
            agent.process_ref.interrupt("battery_depleted")
        self.check_all_agents_discharged()

    def trigger_urgent_chat(self, event_type: str, entity_id: str, details: dict[str, Any]) -> bool:
        if self.incident_policy.get("prefer_worker_local_response", True) and not bool(details.get("escalate_now", False)):
            return False
        if not self.urgent_discuss_enabled:
            return False
        if self.env.now - self.last_urgent_chat_t < self.urgent_chat_cooldown:
            return False
        event = {"event_type": event_type, "entity_id": entity_id, "time": self.env.now, "details": details}
        updates = self.decision_module.urgent_discuss(event, self.local_state_for_urgent())
        priority_updates = updates.get("priority_updates", {}) if isinstance(updates, dict) else {}
        agent_priority_updates = updates.get("agent_priority_updates", {}) if isinstance(updates, dict) else {}
        agent_role_updates = updates.get("agent_roles", {}) if isinstance(updates, dict) else {}
        mailbox_updates = updates.get("mailbox_updates", updates.get("mailbox", {})) if isinstance(updates, dict) else {}
        commitment_updates = updates.get("commitments", {}) if isinstance(updates, dict) else {}
        incident_work_order_updates = updates.get("incident_work_orders", updates.get("emergency_work_orders", {})) if isinstance(updates, dict) else {}
        incident_strategy = updates.get("incident_strategy", {}) if isinstance(updates, dict) else {}
        plan_revision = int(updates.get("plan_revision", getattr(self.current_job_plan, "plan_revision", 0)) or 0) if isinstance(updates, dict) else int(getattr(self.current_job_plan, "plan_revision", 0) or 0)
        reason_trace = updates.get("reason_trace", []) if isinstance(updates, dict) else []
        applied_plan_update = False
        if isinstance(priority_updates, dict):
            self.current_job_plan.task_priority_weights.update(priority_updates)
        if isinstance(agent_priority_updates, dict):
            for agent_id, row in agent_priority_updates.items():
                current_row = self.current_job_plan.agent_priority_multipliers.setdefault(str(agent_id), default_task_priority_weights())
                if isinstance(row, dict):
                    current_row.update({str(key): float(value) for key, value in row.items() if str(key) in current_row})
        if isinstance(agent_role_updates, dict):
            for agent_id, role in agent_role_updates.items():
                agent_key = str(agent_id).strip()
                if agent_key:
                    self.current_job_plan.agent_roles[agent_key] = str(role or "").strip()
            self.current_job_plan.ensure_agent_roles(list(self.agents.keys()))
            applied_plan_update = True
        if isinstance(commitment_updates, dict):
            self.current_job_plan.commitments = {
                str(agent_id): [dict(item) for item in rows if isinstance(item, dict)]
                for agent_id, rows in commitment_updates.items()
                if isinstance(rows, list)
            }
            self.current_job_plan.ensure_commitments(list(self.agents.keys()))
            applied_plan_update = True
        if isinstance(incident_work_order_updates, dict):
            self.current_job_plan.incident_work_orders = {
                str(agent_id): [dict(item) for item in rows if isinstance(item, dict)]
                for agent_id, rows in incident_work_order_updates.items()
                if isinstance(rows, list)
            }
            self.current_job_plan.ensure_incident_work_orders(list(self.agents.keys()))
            applied_plan_update = True
        if isinstance(mailbox_updates, dict):
            self.current_job_plan.mailbox = {
                str(agent_id): [dict(item) for item in items if isinstance(item, dict)]
                for agent_id, items in mailbox_updates.items()
                if isinstance(items, list)
            }
            self.current_job_plan.ensure_mailbox(list(self.agents.keys()))
            applied_plan_update = True
        if isinstance(incident_strategy, dict) and incident_strategy:
            self.current_job_plan.incident_strategy = dict(incident_strategy)
            applied_plan_update = True
        if plan_revision > int(getattr(self.current_job_plan, "plan_revision", 0) or 0):
            self.current_job_plan.plan_revision = plan_revision
            self._resolve_all_selection_blockers(reason="plan_revision_updated")
            applied_plan_update = True
        if isinstance(reason_trace, list):
            self.current_job_plan.reason_trace.extend(reason_trace)
        self.last_urgent_chat_t = self.env.now
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="CHAT_URGENT",
            entity_id="system",
            location="urgent",
            details={
                "event": event,
                "priority_updates": priority_updates,
                "agent_priority_updates": agent_priority_updates,
                "agent_role_updates": agent_role_updates,
                "mailbox_updates": mailbox_updates,
                "commitment_updates": commitment_updates,
                "incident_work_order_updates": incident_work_order_updates,
                "incident_strategy": incident_strategy,
                "plan_revision": int(getattr(self.current_job_plan, "plan_revision", 0) or 0),
                "reason_trace": reason_trace,
                "summary": updates.get("summary", "") if isinstance(updates, dict) else "",
                "applied_plan_update": applied_plan_update,
            },
        )
        return True

    def start_agent_task(self, agent: Agent, task: Task, start_t: float) -> None:
        agent.current_task_id = task.task_id
        agent.current_task_type = task.task_type
        agent.current_task_started_at = start_t
        selection = dict(task.selection_meta) if isinstance(task.selection_meta, dict) else {}
        details: dict[str, Any] = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "priority_key": self._task_priority_key(task),
            "payload": task.payload,
            "selection": selection,
            "agent_role": self.current_agent_role(agent.agent_id),
            "commitment_id": agent.current_commitment_id,
        }
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

    def finish_agent_task(self, agent: Agent, task: Task, start_t: float, status: str, reason: str = "") -> None:
        end_t = self.env.now
        duration = max(0.0, end_t - start_t)
        preserve_carrying = status == "interrupted" and reason in {"battery_depleted", "battery_swap_wait"}
        if (not preserve_carrying) and (agent.carrying_item_id is not None or agent.carrying_item_type is not None):
            self._clear_agent_carrying(agent, destination=agent.location, emit_event=True)
        self.logger.log(
            t=end_t,
            day=self.day_for_time(end_t),
            event_type="AGENT_TASK_END",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={
                "task_id": task.task_id,
                "task_type": task.task_type,
                "status": status,
                "duration": round(duration, 3),
                "reason": reason,
                "payload": task.payload,
            },
        )
        selection = dict(task.selection_meta) if isinstance(task.selection_meta, dict) else {}
        self.task_records.append(
            {
                "day": self.day_for_time(end_t),
                "agent_id": agent.agent_id,
                "task_id": task.task_id,
                "task_type": task.task_type,
                "priority_key": self._task_priority_key(task),
                "status": status,
                "start_t": start_t,
                "end_t": end_t,
                "duration": duration,
                "decision_source": str(selection.get("decision_source", "")),
                "decision_rule": str(selection.get("decision_rule", "")),
                "decision_trace_id": str(selection.get("decision_trace_id", "")),
                "expected_task_signature": selection.get("expected_task_signature", {}),
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
        agent.current_task_started_at = None
        agent.current_commitment_id = None

    def handle_task_interruption(self, agent: Agent, task: Task, reason: str) -> None:
        if reason in {"battery_depleted", "battery_swap_wait"}:
            if task.task_type == "BATTERY_SWAP" and agent.battery_service_owner == agent.agent_id:
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

        if task.task_type == "BATTERY_SWAP":
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
                if moved_id is not None:
                    self.output_buffers[from_station].appendleft(moved_id)
            elif transfer_kind == "material_supply":
                station = int(task.payload.get("station", 1))
                if self.material_supply_owner.get(station) == agent.agent_id:
                    self.material_supply_owner[station] = None

        elif task.task_type == "SETUP_MACHINE":
            machine = self.machines.get(task.payload.get("machine_id"))
            station = machine.station if machine is not None else int(task.payload.get("station", 1))
            material_id = task.payload.pop("material_id", None)
            intermediate_id = task.payload.pop("intermediate_id", None)
            if material_id is not None:
                self.material_queues[station].appendleft(material_id)
            if intermediate_id is not None and station in self.intermediate_queues:
                self.intermediate_queues[station].appendleft(intermediate_id)
            if machine is not None:
                if machine.setup_owner == agent.agent_id:
                    machine.setup_owner = None
                if machine.state == MachineState.SETUP:
                    machine.state = MachineState.WAIT_INPUT

        elif task.task_type == "UNLOAD_MACHINE":
            machine = self.machines.get(task.payload.get("machine_id"))
            if machine is not None and machine.unload_owner == agent.agent_id:
                machine.unload_owner = None

        elif task.task_type == "INSPECT_PRODUCT":
            product_id = task.payload.pop("inspection_product_id", None)
            if product_id is not None:
                self.intermediate_queues[self.inspection_queue_station].appendleft(product_id)

        elif task.task_type == "REPAIR_MACHINE":
            machine = self.machines.get(task.payload.get("machine_id"))
            if machine is not None:
                if machine.repair_owner == agent.agent_id:
                    machine.repair_owner = None
                if machine.broken:
                    machine.state = MachineState.BROKEN

        elif task.task_type == "PREVENTIVE_MAINTENANCE":
            machine = self.machines.get(task.payload.get("machine_id"))
            if machine is not None:
                if machine.pm_owner == agent.agent_id:
                    machine.pm_owner = None
                if machine.broken:
                    machine.state = MachineState.BROKEN
                elif machine.output_intermediate is not None:
                    machine.state = MachineState.DONE_WAIT_UNLOAD
                else:
                    machine.state = MachineState.WAIT_INPUT

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
        if agent.discharged:
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
                task_type="BATTERY_SWAP",
                priority_key="battery_swap",
                priority=mandatory_priority,
                location="BatteryStation",
                payload={"target_agent_id": agent.agent_id, "battery_remaining_min": round(battery_remaining, 3)},
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
            task_type="BATTERY_SWAP",
            priority_key="battery_swap",
            priority=proactive_priority,
            location="BatteryStation",
            payload={
                "target_agent_id": agent.agent_id,
                "battery_remaining_min": round(float(battery_remaining), 3),
                "battery_safety_guard": True,
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

    def _filter_candidates_for_agent(self, agent: Agent, candidates: list[Task]) -> list[Task]:
        filtered = list(candidates)
        battery_reserve = self._battery_swap_service_min(agent) + self._battery_service_margin_min()
        battery_safe: list[Task] = []
        for task in filtered:
            family = self._task_priority_key(task)
            if family in {"battery_swap", "battery_delivery_low_battery", "battery_delivery_discharged"}:
                battery_safe.append(task)
                continue
            if self.battery_remaining(agent) >= self._task_estimated_duration(agent, task) + battery_reserve:
                battery_safe.append(task)
        filtered = battery_safe
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

    def _select_battery_safety_task(self, candidates: list[Task], agent: Agent) -> Task | None:
        if self.battery_remaining(agent) > self._battery_proactive_swap_threshold(agent):
            return None
        battery_candidates = [
            task for task in candidates
            if self._task_priority_key(task) in {"battery_swap", "battery_delivery_discharged", "battery_delivery_low_battery"}
        ]
        if not battery_candidates:
            return None

        def _battery_rank(task: Task) -> tuple[int, tuple[float, float, float, str, str]]:
            family = self._task_priority_key(task)
            order = {"battery_swap": 0, "battery_delivery_discharged": 1, "battery_delivery_low_battery": 2}
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
        if task.task_type in {"SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            try:
                return int(task.payload.get("station")) if task.payload.get("station") not in {None, ""} else None
            except (TypeError, ValueError):
                machine_id = str(task.payload.get("machine_id", ""))
                machine = self.machines.get(machine_id)
                return int(machine.station) if machine is not None else None
        if task.task_type == "INSPECT_PRODUCT":
            return self.inspection_queue_station
        return None

    def _task_target_id(self, task: Task) -> str:
        if task.task_type == "BATTERY_SWAP":
            return str(task.payload.get("target_agent_id", ""))
        if task.task_type in {"SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            return str(task.payload.get("machine_id", ""))
        if task.task_type == "INSPECT_PRODUCT":
            return "inspection"
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "battery_delivery":
                return str(task.payload.get("target_agent_id", ""))
            if transfer_kind == "material_supply":
                return f"station{task.payload.get('station', '')}"
            if transfer_kind == "inter_station":
                return f"station{task.payload.get('from_station', '')}"
        return ""

    def _task_target_type(self, task: Task) -> str:
        if task.task_type == "BATTERY_SWAP":
            return "agent"
        if task.task_type in {"SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            return "machine"
        if task.task_type == "INSPECT_PRODUCT":
            return "station"
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "battery_delivery":
                return "agent"
            if transfer_kind in {"material_supply", "inter_station"}:
                return "station"
        return "none"

    def _task_shareable(self, task: Task) -> bool:
        return False

    def _task_capacity(self, task: Task) -> int:
        return 1

    def _task_why_available(self, task: Task) -> str:
        if task.task_type == "REPAIR_MACHINE":
            return "A broken machine is idle and can be repaired immediately."
        if task.task_type == "UNLOAD_MACHINE":
            return "A machine has finished output waiting for unload."
        if task.task_type == "SETUP_MACHINE":
            return "A machine is ready for setup because required inputs are present."
        if task.task_type == "PREVENTIVE_MAINTENANCE":
            return "A machine is idle and due for preventive maintenance."
        if task.task_type == "INSPECT_PRODUCT":
            return "Inspection input is available for acceptance processing."
        if task.task_type == "BATTERY_SWAP":
            return "The worker is below the mandatory battery threshold and can self-swap now."
        if task.task_type == "TRANSFER":
            transfer_kind = str(task.payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "material_supply":
                return "A station is below its material target and the warehouse can replenish it."
            if transfer_kind == "inter_station":
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
        elif task.task_type == "BATTERY_SWAP":
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
        if incident_class in {"worker_discharged", "worker_low_battery"} and task.task_type in {"BATTERY_SWAP", "TRANSFER"}:
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
            escalated = self.trigger_urgent_chat(
                "replan_required",
                agent.agent_id,
                {
                    "incident_id": incident_id,
                    "reason": "worker_local_response_exhausted",
                    "blocker_id": source_blocker_id,
                    "escalate_now": True,
                },
            )
            if escalated:
                self.incident_escalations.add(escalation_key)
            if source_blocker_id and escalated:
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
            return self._annotate_task_selection(
                agent.suspended_task,
                decision_source="hard_constraint",
                decision_rule="resume_suspended_task",
                rationale="Resume the interrupted task before taking a new one.",
            )

        mandatory = self.mandatory_task_for_agent(agent)
        if mandatory is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="mandatory_task_selected")
            return self._annotate_task_selection(
                mandatory,
                decision_source="hard_constraint",
                decision_rule="mandatory_battery_swap",
                rationale="Battery remaining reached the mandatory swap threshold.",
                score_hint=self._task_score(mandatory, agent),
            )

        candidates = self._filter_candidates_for_agent(agent, self._candidate_tasks(agent))
        if not candidates:
            self._resolve_selection_blocker(agent.agent_id, reason="no_candidates")
            self._escalate_incident_if_needed(agent, self._recent_incidents_for_agent(agent))
            return None

        battery_safety_task = self._select_battery_safety_task(candidates, agent)
        if battery_safety_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="battery_safety_selected")
            return battery_safety_task

        local_response_task = self._select_local_response_task(candidates, agent)
        if local_response_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="local_response_selected")
            return local_response_task

        incident_work_order_task = self._select_incident_work_order_task(candidates, agent)
        if incident_work_order_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="incident_work_order_selected")
            bias = self._selection_bias_snapshot(incident_work_order_task, agent)
            focus = [str(bias.get("priority_key", ""))]
            work_orders = bias.get("incident_work_orders", []) if isinstance(bias.get("incident_work_orders", []), list) else []
            for item in work_orders[:2]:
                if isinstance(item, dict) and str(item.get("task_family", "")).strip():
                    focus.append(str(item.get("task_family", "")).strip())
            return self._annotate_task_selection(
                incident_work_order_task,
                decision_source="manager_incident_work_order",
                decision_rule="incident_work_order_dispatch",
                rationale="Engine executed a short-lived emergency work order before planner queue, mailbox, or simulator fallback.",
                candidate_count=1,
                score_hint=self._task_score(incident_work_order_task, agent),
                decision_focus=[item for item in focus if item],
                fallback_reason="incident_work_order",
            )

        commitment_task = self._select_commitment_task(candidates, agent)
        if commitment_task is not None:
            self._resolve_selection_blocker(agent.agent_id, reason="commitment_selected")
            bias = self._selection_bias_snapshot(commitment_task, agent)
            focus = [str(bias.get("priority_key", ""))]
            commitments = bias.get("commitments", []) if isinstance(bias.get("commitments", []), list) else []
            for item in commitments[:2]:
                if isinstance(item, dict) and str(item.get("task_family", "")).strip():
                    focus.append(str(item.get("task_family", "")).strip())
            return self._annotate_task_selection(
                commitment_task,
                decision_source="manager_commitment",
                decision_rule="commitment_dispatch",
                rationale="Engine executed the first feasible commitment before planner queue, mailbox, or simulator fallback.",
                candidate_count=1,
                score_hint=self._task_score(commitment_task, agent),
                decision_focus=[item for item in focus if item],
                fallback_reason="commitment",
            )

        if self._llm_commitment_path_active():
            blocker, created = self._activate_selection_blocker(
                agent,
                blocker_type="no_commitment_match",
                candidates=candidates,
                details={"reason": "no_commitment_or_mailbox_task", "candidate_count": len(candidates)},
            )
            if created:
                escalated = self.trigger_urgent_chat(
                    "replan_required",
                    agent.agent_id,
                    {
                        "reason": "no_commitment_or_mailbox_task",
                        "candidate_count": len(candidates),
                        "blocker_id": str(blocker.get("blocker_id", "")).strip(),
                        "escalate_now": True,
                    },
                )
                if escalated:
                    self.incident_escalations.add(str(blocker.get("blocker_id", "")).strip())
                    self._mark_blocker_escalated(str(blocker.get("blocker_id", "")).strip())
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
            return self._annotate_task_selection(
                queue_task,
                decision_source="manager_queue",
                decision_rule="personal_queue_dispatch",
                rationale="Engine executed the first feasible planner queue order before considering mailbox or generic priority scoring.",
                candidate_count=1,
                score_hint=self._task_score(queue_task, agent),
                decision_focus=[item for item in focus if item],
                fallback_reason="personal_queue",
            )

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
            return self._annotate_task_selection(
                task,
                decision_source="manager_queue",
                decision_rule="mailbox_dispatch",
                rationale="Engine selected the highest priority mailbox-matched feasible task after no feasible planner queue order was available.",
                candidate_count=len(mailbox_candidates),
                score_hint=self._task_score(task, agent),
                decision_focus=[item for item in focus if item],
                fallback_reason="mailbox",
            )

        scored_candidates = sorted(candidates, key=lambda task: self._task_sort_key(task, agent))
        task = scored_candidates[0]
        self._resolve_selection_blocker(agent.agent_id, reason="legacy_fallback_selected")
        bias = self._selection_bias_snapshot(task, agent)
        focus = [str(bias.get("priority_key", ""))]
        queue = bias.get("personal_queue", []) if isinstance(bias.get("personal_queue", []), list) else []
        for item in queue[:2]:
            if isinstance(item, dict) and str(item.get("task_family", "")).strip():
                focus.append(str(item.get("task_family", "")).strip())
        return self._annotate_task_selection(
            task,
            decision_source="simulator_fallback",
            decision_rule="legacy_priority_score_fallback",
            rationale="Legacy fallback chose the highest priority feasible task because no commitment, planner queue, or mailbox task was available.",
            candidate_count=len(candidates),
            score_hint=self._task_score(task, agent),
            decision_focus=[item for item in focus if item],
            fallback_reason="legacy_priority_score",
        )

    # shared weight는 하루 단위 의도를 나타내고, agent multiplier는 그 의도를 개인별로 미세 조정한다.
    # queue와 mailbox는 이미 상위 선택 tier에서 처리하므로 최종 점수식은 단순하게 유지한다.
    def _task_score(self, task: Task, agent: Agent | str | None = None) -> float:
        priority_key = self._task_priority_key(task)
        if isinstance(agent, Agent):
            effective = self.current_effective_task_priority_weights(agent.agent_id)
            weight = float(effective.get(priority_key, 1.0))
            return float(task.priority) * weight
        if isinstance(agent, str) and agent.strip():
            effective = self.current_effective_task_priority_weights(agent.strip())
            weight = float(effective.get(priority_key, 1.0))
            return float(task.priority) * weight
        weight = float((self.current_job_plan.task_priority_weights or {}).get(priority_key, 1.0))
        return float(task.priority) * weight

    def _candidate_tasks(self, agent: Agent) -> list[Task]:
        tasks: list[Task] = []
        deliver_priority_discharged = float(self._rule("world.task_priority.battery_delivery_discharged", 149.0))
        deliver_priority_low_battery = float(self._rule("world.task_priority.battery_delivery_low_battery", 140.0))
        priority_repair_machine = float(self._rule("world.task_priority.repair_machine", 115.0))
        priority_unload_machine = float(self._rule("world.task_priority.unload_machine", 110.0))
        priority_setup_machine = float(self._rule("world.task_priority.setup_machine", 90.0))
        priority_pm = float(self._rule("world.task_priority.preventive_maintenance", 65.0))
        priority_inter_station_transfer = float(self._rule("world.task_priority.inter_station_transfer", 85.0))
        priority_material_supply = float(self._rule("world.task_priority.material_supply", 85.0))
        priority_inspect_product = float(self._rule("world.task_priority.inspect_product", 72.0))

        proactive_swap = self._proactive_battery_swap_task(agent)
        if proactive_swap is not None:
            tasks.append(proactive_swap)

        for other in self.agents.values():
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
            if machine.broken and machine.repair_owner is None:
                tasks.append(
                    Task(
                        task_id=self._next_task_id("RM"),
                        task_type="REPAIR_MACHINE",
                        priority_key="repair_machine",
                        priority=priority_repair_machine,
                        location=f"Station{machine.station}",
                        payload={"machine_id": machine.machine_id},
                    )
                )
            elif machine.output_intermediate is not None and machine.unload_owner is None:
                tasks.append(
                    Task(
                        task_id=self._next_task_id("UL"),
                        task_type="UNLOAD_MACHINE",
                        priority_key="unload_machine",
                        priority=priority_unload_machine,
                        location=f"Station{machine.station}",
                        payload={"machine_id": machine.machine_id, "station": machine.station},
                    )
                )
            elif (
                not machine.broken
                and machine.state == MachineState.WAIT_INPUT
                and machine.setup_owner is None
                and machine.output_intermediate is None
                and (
                    machine.input_material is None
                    or (self._station_requires_intermediate(machine.station) and machine.input_intermediate is None)
                )
                and (
                    machine.input_material is not None
                    or len(self.material_queues[machine.station]) > 0
                )
                and (
                    not self._station_requires_intermediate(machine.station)
                    or machine.input_intermediate is not None
                    or len(self.intermediate_queues[machine.station]) > 0
                )
            ):
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

            pm_due = self.env.now - machine.last_pm_at >= self.pm_interval_target_min
            if (
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
                        payload={"machine_id": machine.machine_id, "station": machine.station},
                    )
                )

        for station, buffer in self.output_buffers.items():
            if buffer:
                task_location = "Inspection" if station == self.inspection_queue_station else f"Station{station}"
                tasks.append(
                    Task(
                        task_id=self._next_task_id("TR"),
                        task_type="TRANSFER",
                        priority_key="inter_station_transfer",
                        priority=priority_inter_station_transfer,
                        location=task_location,
                        payload={"transfer_kind": "inter_station", "from_station": station},
                    )
                )

        for station in self.stations:
            material_target = int(self.inventory_targets["material"][f"station{station}"])
            if len(self.material_queues[station]) < material_target and self.material_supply_owner.get(station) is None:
                tasks.append(
                    Task(
                        task_id=self._next_task_id("MAT"),
                        task_type="TRANSFER",
                        priority_key="material_supply",
                        priority=priority_material_supply,
                        location="Warehouse",
                        payload={"transfer_kind": "material_supply", "station": station},
                    )
                )

        if self.intermediate_queues[self.inspection_queue_station]:
            tasks.append(
                Task(
                    task_id=self._next_task_id("INS"),
                    task_type="INSPECT_PRODUCT",
                    priority_key="inspect_product",
                    priority=priority_inspect_product,
                    location="Inspection",
                    payload={},
                )
            )
        return tasks

    def travel_time(self, src: str, dst: str) -> float:
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

    def move_agent(self, agent: Agent, dst: str, emit_move_events: bool = True):
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
        move_t = self.travel_time(src, dst)

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
                    details={"from": src, "to": dst, "duration": round(move_t, 3)},
                )
            move_start_t = self.env.now
            self._set_in_transit(agent, src, dst, 0.0, move_t)
            try:
                yield self.env.timeout(move_t)
            except simpy.Interrupt as intr:
                elapsed = max(0.0, self.env.now - move_start_t)
                progress = min(1.0, max(0.0, elapsed / max(1e-6, move_t)))
                self._set_in_transit(agent, src, dst, progress, move_t)
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
                    details={"from": src, "to": dst},
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

    def execute_task(self, agent: Agent, task: Task):
        task_type = task.task_type

        if task_type in {"UNLOAD_MACHINE", "SETUP_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            machine = self.machines[task.payload["machine_id"]]
            # Broken machines are strictly limited to REPAIR_MACHINE only.
            if machine.broken:
                return False

        if task_type == "BATTERY_SWAP":
            if agent.battery_service_owner is not None and agent.battery_service_owner != agent.agent_id:
                return False
            agent.battery_service_owner = agent.agent_id
            try:
                if agent.discharged:
                    return False
                yield from self.move_agent(agent, "BatteryStation", emit_move_events=True)
                yield self.env.timeout(float(self.agent_cfg["battery_pickup_time_min"]))
                battery_item_id = str(task.payload.get("battery_item_id", ""))
                if not battery_item_id:
                    battery_item_id = self._next_item_id("BAT")
                    task.payload["battery_item_id"] = battery_item_id
                if not self._set_agent_carrying(agent, "battery", battery_item_id):
                    return False
                agent.last_battery_swap = self.env.now
                agent.discharged = False
                agent.discharged_since = None
                agent.low_battery_alerted = False
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
                return True
            finally:
                if agent.battery_service_owner == agent.agent_id:
                    agent.battery_service_owner = None

        if task_type == "REPAIR_MACHINE":
            machine = self.machines[task.payload["machine_id"]]
            if not machine.broken:
                return False
            if machine.repair_owner is not None and machine.repair_owner != agent.agent_id:
                return False
            machine.repair_owner = agent.agent_id
            yield from self.move_agent(agent, f"Station{machine.station}", emit_move_events=True)
            try:
                if not machine.broken:
                    return False
                machine.state = MachineState.UNDER_REPAIR
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_REPAIR_START",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={"by": agent.agent_id},
                )
                yield self.env.timeout(float(self.machine_failure_cfg["repair_time_min"]))
                if machine.failed_since is not None:
                    machine.total_broken_min += self.env.now - machine.failed_since
                machine.broken = False
                machine.failed_since = None
                machine.state = MachineState.DONE_WAIT_UNLOAD if machine.output_intermediate is not None else MachineState.WAIT_INPUT
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_REPAIRED",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={"by": agent.agent_id},
                )
                return True
            finally:
                if machine.repair_owner == agent.agent_id:
                    machine.repair_owner = None

        if task_type == "UNLOAD_MACHINE":
            machine = self.machines[task.payload["machine_id"]]
            if machine.unload_owner is not None and machine.unload_owner != agent.agent_id:
                return False
            machine.unload_owner = agent.agent_id
            try:
                if machine.broken or machine.output_intermediate is None:
                    return False
                yield from self.move_agent(agent, f"Station{machine.station}", emit_move_events=True)
                if machine.broken:
                    return False
                yield self.env.timeout(float(self.movement_cfg["unload_min"]))
                if machine.broken:
                    return False
                output_id = machine.output_intermediate
                if output_id is not None:
                    carried_kind = "product" if machine.station == self.last_processing_station else "intermediate"
                    if not self._set_agent_carrying(agent, carried_kind, output_id):
                        return False
                machine.output_intermediate = None
                machine.state = MachineState.WAIT_INPUT if not machine.broken else MachineState.BROKEN
                if output_id is not None:
                    self.output_buffers[machine.station].append(output_id)
                    self._clear_agent_carrying(agent, destination=f"output_buffer_station_{machine.station}")
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="ITEM_MOVED",
                        entity_id=output_id,
                        location=f"Station{machine.station}",
                        details={"from": machine.machine_id, "to": f"output_buffer_station_{machine.station}"},
                    )
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
                        yield from self.move_agent(agent, "BatteryStation", emit_move_events=True)
                        yield self.env.timeout(float(self.agent_cfg["battery_pickup_time_min"]))
                        if not battery_item_id:
                            battery_item_id = self._next_item_id("BAT")
                            task.payload["transfer_item_id"] = battery_item_id
                        if not self._set_agent_carrying(agent, "battery_fresh", battery_item_id):
                            return False
                        task.payload["battery_loaded"] = True
                    elif agent.carrying_item_type != "battery_fresh":
                        if not battery_item_id:
                            battery_item_id = self._next_item_id("BAT")
                            task.payload["transfer_item_id"] = battery_item_id
                        if not self._set_agent_carrying(agent, "battery_fresh", battery_item_id):
                            return False

                    agent.battery_swap_critical = True
                    target_agent.battery_swap_critical = True
                    handover_location = yield from self._move_agent_to_in_transit_position(
                        agent,
                        target_agent,
                        emit_move_events=True,
                    )
                    if handover_location is None:
                        return False

                    if not target_agent.discharged and target_agent.awaiting_battery_from is None:
                        target_agent.awaiting_battery_from = agent.agent_id
                        self.logger.log(
                            t=self.env.now,
                            day=self.day_for_time(self.env.now),
                            event_type="BATTERY_SWAP_WAIT_START",
                            entity_id=target_agent.agent_id,
                            location=self.agent_display_location(target_agent),
                            details={"from_agent_id": agent.agent_id},
                        )
                        if target_agent.process_ref is not None and target_agent.process_ref.is_alive:
                            target_agent.process_ref.interrupt("battery_swap_wait")

                    yield self.env.timeout(float(self.agent_cfg["battery_delivery_extra_min"]))

                    if not self._has_in_transit_position(target_agent):
                        if agent.location != target_agent.location:
                            yield from self.move_agent(agent, target_agent.location, emit_move_events=True)
                        if agent.location != target_agent.location:
                            return False
                        handover_location = str(target_agent.location)
                    else:
                        handover_location = yield from self._move_agent_to_in_transit_position(
                            agent,
                            target_agent,
                            emit_move_events=True,
                        )
                        if handover_location is None:
                            return False

                    became_discharged_during_delivery = target_agent.discharged
                    target_agent.last_battery_swap = self.env.now
                    target_agent.discharged = False
                    target_agent.discharged_since = None
                    target_agent.low_battery_alerted = False
                    self._clear_agent_carrying(agent, destination=handover_location)
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
                    if target_agent.suspended_task is not None and target_agent.suspended_task.task_type == "BATTERY_SWAP":
                        target_agent.suspended_task = None
                    yield from self.move_agent(agent, "BatteryStation", emit_move_events=True)
                    self._clear_agent_carrying(agent, destination="BatteryStation")
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
                        target_agent.awaiting_battery_from = None
                        self.logger.log(
                            t=self.env.now,
                            day=self.day_for_time(self.env.now),
                            event_type="BATTERY_SWAP_WAIT_END",
                            entity_id=target_agent.agent_id,
                            location=self.agent_display_location(target_agent),
                            details={"from_agent_id": agent.agent_id},
                        )
                    if target_agent.battery_service_owner == agent.agent_id:
                        target_agent.battery_service_owner = None

            if transfer_kind == "inter_station":
                from_station = int(task.payload["from_station"])
                moved_item_id = str(task.payload.get("transfer_item_id", ""))
                if not moved_item_id:
                    from_location = "Inspection" if from_station == self.inspection_queue_station else f"Station{from_station}"
                    yield from self.move_agent(agent, from_location, emit_move_events=True)
                    if not self.output_buffers[from_station]:
                        return False
                    moved_item_id = self.output_buffers[from_station].popleft()
                    task.payload["transfer_item_id"] = moved_item_id
                    moved_item_kind = "product" if from_station >= self.last_processing_station else "intermediate"
                    if not self._set_agent_carrying(agent, moved_item_kind, moved_item_id):
                        self.output_buffers[from_station].appendleft(moved_item_id)
                        task.payload.pop("transfer_item_id", None)
                        return False
                elif agent.carrying_item_id != moved_item_id:
                    moved_item_kind = "product" if from_station >= self.last_processing_station else "intermediate"
                    if not self._set_agent_carrying(agent, moved_item_kind, moved_item_id):
                        return False
                if from_station == self.inspection_queue_station:
                    # Final logistics leg: inspected product -> Warehouse.
                    to_location = "Warehouse"
                    yield from self.move_agent(agent, to_location, emit_move_events=True)
                    self.product_count += 1
                    if moved_item_id in self.items:
                        self.items[moved_item_id].current_station = None
                else:
                    to_station = from_station + 1
                    to_location = f"Station{to_station}" if to_station <= self.last_processing_station else "Inspection"
                    yield from self.move_agent(agent, to_location, emit_move_events=True)
                    target_queue_station = to_station if to_station <= self.last_processing_station else self.inspection_queue_station
                    self._push_intermediate_queue(target_queue_station, moved_item_id)
                task.payload.pop("transfer_item_id", None)
                moved_item_kind = "product" if from_station >= self.last_processing_station else "intermediate"
                self._clear_agent_carrying(agent, destination=to_location)
                if from_station == self.inspection_queue_station:
                    move_to = "Warehouse"
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
                        location="Warehouse",
                        details={},
                    )
                return True

            if transfer_kind == "material_supply":
                station = int(task.payload["station"])
                owner = self.material_supply_owner.get(station)
                if owner is not None and owner != agent.agent_id:
                    return False
                self.material_supply_owner[station] = agent.agent_id
                try:
                    item_id = str(task.payload.get("transfer_item_id", ""))
                    if not item_id:
                        yield from self.move_agent(agent, "Warehouse", emit_move_events=True)
                        item_id = self._next_item_id(f"MAT-S{station}")
                        task.payload["transfer_item_id"] = item_id
                        self.items[item_id] = Item(
                            item_id=item_id,
                            item_type="material",
                            created_at=self.env.now,
                            current_station=station,
                        )
                        if not self._set_agent_carrying(agent, "material", item_id):
                            self.items.pop(item_id, None)
                            task.payload.pop("transfer_item_id", None)
                            return False
                    elif agent.carrying_item_id != item_id:
                        if agent.location != "Warehouse":
                            yield from self.move_agent(agent, "Warehouse", emit_move_events=True)
                        if not self._set_agent_carrying(agent, "material", item_id):
                            return False
                    yield from self.move_agent(agent, f"Station{station}", emit_move_events=True)
                    self._push_material_queue(station, item_id)
                    self._clear_agent_carrying(agent, destination=f"Station{station}")
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_for_time(self.env.now),
                        event_type="ITEM_MOVED",
                        entity_id=item_id,
                        location=f"Station{station}",
                        details={"from": "Warehouse", "to": f"material_queue_{station}"},
                    )
                    task.payload.pop("transfer_item_id", None)
                    task.payload.pop("material_item_id", None)
                    return True
                finally:
                    if self.material_supply_owner.get(station) == agent.agent_id:
                        self.material_supply_owner[station] = None

            return False

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
                if machine.state not in {MachineState.WAIT_INPUT, MachineState.SETUP}:
                    return False
                requires_intermediate = self._station_requires_intermediate(station)
                needs_material = machine.input_material is None
                needs_intermediate = requires_intermediate and machine.input_intermediate is None
                if not needs_material and not needs_intermediate:
                    machine.state = MachineState.IDLE
                    return False

                has_reserved_material = bool(task.payload.get("material_id"))
                has_reserved_intermediate = bool(task.payload.get("intermediate_id")) if requires_intermediate else False
                if needs_material and not has_reserved_material and not self.material_queues[station]:
                    return False
                if needs_intermediate and not has_reserved_intermediate and not self.intermediate_queues[station]:
                    return False

                yield from self.move_agent(agent, f"Station{station}", emit_move_events=True)

                setup_step = float(self.movement_cfg["setup_min"])
                machine.state = MachineState.SETUP
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

                if needs_material:
                    material_id = str(task.payload.get("material_id", ""))
                    if not material_id:
                        popped_material = self._pop_material_queue(station)
                        if popped_material is None:
                            machine.state = MachineState.WAIT_INPUT
                            _close_setup_event("missing_material")
                            return False
                        material_id = popped_material
                        task.payload["material_id"] = material_id
                    # One carry slot: load material first.
                    if not self._set_agent_carrying(agent, "material", material_id):
                        self.material_queues[station].appendleft(material_id)
                        task.payload.pop("material_id", None)
                        machine.state = MachineState.WAIT_INPUT
                        _close_setup_event("carry_failed_material")
                        return False
                    yield self.env.timeout(setup_step)
                    machine.input_material = material_id
                    task.payload.pop("material_id", None)
                    self._clear_agent_carrying(agent, destination=machine.machine_id)

                if needs_intermediate:
                    intermediate_id = str(task.payload.get("intermediate_id", ""))
                    if not intermediate_id:
                        popped_intermediate = self._pop_intermediate_queue(station)
                        if popped_intermediate is None:
                            machine.state = MachineState.WAIT_INPUT
                            _close_setup_event("missing_intermediate")
                            return False
                        intermediate_id = popped_intermediate
                        task.payload["intermediate_id"] = intermediate_id
                    # Then load intermediate as a separate one-item carry.
                    if not self._set_agent_carrying(agent, "intermediate", intermediate_id):
                        self.intermediate_queues[station].appendleft(intermediate_id)
                        task.payload.pop("intermediate_id", None)
                        machine.state = MachineState.WAIT_INPUT
                        _close_setup_event("carry_failed_intermediate")
                        return False
                    yield self.env.timeout(setup_step)
                    machine.input_intermediate = intermediate_id
                    task.payload.pop("intermediate_id", None)
                    self._clear_agent_carrying(agent, destination=machine.machine_id)

                if machine.input_material is None or (requires_intermediate and machine.input_intermediate is None):
                    machine.state = MachineState.WAIT_INPUT
                    _close_setup_event("incomplete_inputs")
                    return False

                machine.state = MachineState.IDLE
                _close_setup_event("completed")
                return True

            finally:
                if machine.setup_owner == agent.agent_id:
                    machine.setup_owner = None
                    if machine.state == MachineState.SETUP and (
                        machine.input_material is None
                        or (
                            self._station_requires_intermediate(station)
                            and machine.input_intermediate is None
                        )
                    ):
                        _close_setup_event("aborted")
                        machine.state = MachineState.WAIT_INPUT
        if task_type == "INSPECT_PRODUCT":
            product_id = str(task.payload.get("inspection_product_id", ""))
            if not product_id and not self.intermediate_queues[self.inspection_queue_station]:
                return False
            yield from self.move_agent(agent, "Inspection", emit_move_events=True)
            if not product_id:
                popped = self._pop_intermediate_queue(4)
                if popped is None:
                    return False
                product_id = popped
                task.payload["inspection_product_id"] = product_id
            if not self._set_agent_carrying(agent, "product", product_id):
                self.intermediate_queues[self.inspection_queue_station].appendleft(product_id)
                task.payload.pop("inspection_product_id", None)
                return False
            self.inspection_active_agents += 1
            k = max(1, self.inspection_active_agents)
            inspect_t = max(self.inspection_min_time_min, self.inspection_base_time_min / math.sqrt(k))
            try:
                yield self.env.timeout(inspect_t)
            finally:
                self.inspection_active_agents = max(0, self.inspection_active_agents - 1)
            defect_prob = float(self.quality_cfg["defect_prob"])
            if self.rng.random() < defect_prob:
                self.scrap_count += 1
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="INSPECT_FAIL",
                    entity_id=product_id,
                    location="Inspection",
                    details={"inspector": agent.agent_id},
                )
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="SCRAP",
                    entity_id=product_id,
                    location="Inspection",
                    details={},
                )
                self._clear_agent_carrying(agent, destination="Inspection")
            else:
                # Inspection pass: move product to inspection output buffer.
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="ITEM_MOVED",
                    entity_id=product_id,
                    location="Inspection",
                    details={
                        "from": "Inspection",
                        "to": f"output_buffer_station_{self.inspection_queue_station}",
                        "item_type": "product",
                    },
                )
                self.output_buffers[self.inspection_queue_station].append(product_id)
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="INSPECT_PASS",
                    entity_id=product_id,
                    location="Inspection",
                    details={"inspector": agent.agent_id},
                )
                self._clear_agent_carrying(
                    agent,
                    destination=f"output_buffer_station_{self.inspection_queue_station}",
                )
            task.payload.pop("inspection_product_id", None)
            return True

        if task_type == "PREVENTIVE_MAINTENANCE":
            machine = self.machines[task.payload["machine_id"]]
            if machine.pm_owner is not None and machine.pm_owner != agent.agent_id:
                return False
            machine.pm_owner = agent.agent_id
            try:
                if machine.broken or machine.state == MachineState.PROCESSING:
                    return False
                yield from self.move_agent(agent, f"Station{machine.station}", emit_move_events=True)
                machine.state = MachineState.UNDER_PM
                pm_start = self.env.now
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_PM_START",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={"by": agent.agent_id},
                )
                yield self.env.timeout(float(self.machine_failure_cfg["pm_time_min"]))
                pm_duration = self.env.now - pm_start
                machine.total_pm_min += pm_duration
                machine.pm_count += 1
                machine.last_pm_at = self.env.now
                machine.pm_until = self.env.now + self.pm_effect_duration_min
                machine.state = MachineState.WAIT_INPUT
                self.logger.log(
                    t=self.env.now,
                    day=self.day_for_time(self.env.now),
                    event_type="MACHINE_PM_END",
                    entity_id=machine.machine_id,
                    location=f"Station{machine.station}",
                    details={"by": agent.agent_id, "duration": round(pm_duration, 3)},
                )
                return True
            finally:
                if machine.pm_owner == agent.agent_id:
                    machine.pm_owner = None
        return False

    def start_machine_cycle(self, machine: Machine) -> str:
        cycle_id = self._next_cycle_id()
        machine.state = MachineState.PROCESSING
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_START",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={"cycle_id": cycle_id, "input_material": machine.input_material, "input_intermediate": machine.input_intermediate},
        )
        return cycle_id

    def complete_machine_cycle(self, machine: Machine, cycle_id: str) -> None:
        if machine.station == self.last_processing_station:
            output_id = self._next_item_id("PRODUCT")
            output_type = "product"
        else:
            output_id = self._next_item_id(f"INT-S{machine.station}")
            output_type = "intermediate"
        source_created_at: list[float] = []
        if machine.input_material and machine.input_material in self.items:
            source_created_at.append(float(self.items[machine.input_material].created_at))
        if machine.input_intermediate and machine.input_intermediate in self.items:
            source_created_at.append(float(self.items[machine.input_intermediate].created_at))
        output_created_at = min(source_created_at) if source_created_at else float(self.env.now)
        self.items[output_id] = Item(
            item_id=output_id,
            item_type=output_type,
            created_at=output_created_at,
            current_station=machine.station,
        )
        machine.input_material = None
        machine.input_intermediate = None
        machine.output_intermediate = output_id
        machine.state = MachineState.DONE_WAIT_UNLOAD
        self.station_throughput[machine.station] += 1
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_END",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={"cycle_id": cycle_id, "output_intermediate": output_id},
        )

    def abort_machine_cycle(self, machine: Machine, cycle_id: str, reason: str) -> None:
        machine.input_material = None
        machine.input_intermediate = None
        machine.state = MachineState.BROKEN if machine.broken else MachineState.WAIT_INPUT
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_ABORTED",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={"cycle_id": cycle_id, "reason": reason},
        )

    def _empty_agent_priority_counter(self, *, float_values: bool = False) -> dict[str, float] | dict[str, int]:
        default_value: float | int = 0.0 if float_values else 0
        return {key: default_value for key in default_task_priority_weights().keys()}

    def _agent_day_experience(self, task_slice: list[dict[str, Any]]) -> dict[str, Any]:
        # Build the per-agent behavioral summary that feeds next-day overlay updates and
        # becomes the compact personal memory shown to daily review artifacts and workspace memory updates.
        experience: dict[str, Any] = {}
        downstream_keys = {"unload_machine", "inter_station_transfer", "inspect_product"}
        reliability_keys = {"repair_machine", "preventive_maintenance"}
        battery_keys = {"battery_swap", "battery_delivery_low_battery", "battery_delivery_discharged"}
        supply_keys = {"material_supply", "setup_machine"}

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
                "downstream_flow_completed": sum(completed_counts[key] for key in downstream_keys),
                "reliability_completed": sum(completed_counts[key] for key in reliability_keys),
                "battery_support_completed": sum(completed_counts[key] for key in battery_keys),
                "supply_support_completed": sum(completed_counts[key] for key in supply_keys),
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
        local_response_task_count = 0
        commitment_dispatch_task_count = 0
        for rec in task_slice:
            if rec["status"] == "completed":
                task_breakdown[rec["task_type"]] += float(rec["duration"])
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
            "scrap_rate": round(scrap_rate, 5),
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
            "incident_event_count": int(incident_event_count),
            "physical_incident_count": int(physical_incident_count),
            "coordination_incident_count": int(coordination_incident_count),
            "unique_replan_blocker_count": int(len(self.day_unique_replan_blockers)),
            "planner_escalation_count": int(max(planner_escalation_count, len(self.day_planner_escalations))),
            "local_response_task_count": int(local_response_task_count),
            "commitment_dispatch_task_count": int(commitment_dispatch_task_count),
            "days_since_last_product": int(days_since_last_product),
            "task_minutes": dict(task_breakdown),
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
            "shared_task_priority_weights": dict(self.current_job_plan.task_priority_weights or {}),
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

        task_totals: dict[str, float] = defaultdict(float)
        for rec in self.task_records:
            if rec["status"] == "completed":
                task_totals[rec["task_type"]] += rec["duration"]

        discharged_metrics = self._agent_discharged_metrics()
        buffer_wait_metrics = self._buffer_wait_metrics()
        lead_time_metrics = self._completed_product_lead_time_metrics()
        machine_time_metrics = self._machine_time_metrics()
        machine_state_metrics = self._machine_state_time_metrics()
        worker_state_metrics = self._worker_state_time_metrics()
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

        return {
            "total_products": self.product_count,
            "scrap_count": self.scrap_count,
            "scrap_rate": round((self.scrap_count / total_checked) if total_checked > 0 else 0.0, 6),
            "station_throughput": dict(self.station_throughput),
            "stage_throughput": stage_throughput,
            "avg_daily_products": round(self.product_count / self.num_days, 4),
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
            "agent_task_minutes": dict(task_totals),
            "agent_discharged_time_min_total": discharged_metrics["total_min"],
            "agent_discharged_time_min_avg": discharged_metrics["avg_min_per_agent"],
            "agent_discharged_time_min_by_agent": discharged_metrics["by_agent"],
            "agent_discharged_ratio": discharged_metrics["discharged_ratio"],
            "agent_discharged_ratio_by_agent": discharged_metrics["ratio_by_agent"],
            "agent_availability_ratio": discharged_metrics["availability_ratio"],
            "worker_state_time_by_worker": worker_state_metrics["state_time_by_worker"],
            "worker_utilization_by_worker": worker_state_metrics["utilization_by_worker"],
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





