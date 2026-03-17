from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict, deque
from statistics import mean
from typing import Any

import simpy

from manufacturing_sim.simulation.scenarios.manufacturing.decision.base import (
    JobPlan,
    StrategyState,
    default_agent_priority_multipliers,
    default_task_priority_weights,
)
from manufacturing_sim.simulation.scenarios.manufacturing.decision.modes import normalize_decision_mode
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
        return int(t // self.minutes_per_day) + 1

    def start_day(self, day: int, strategy: StrategyState, job_plan: JobPlan) -> None:
        self.current_day = day
        self.current_strategy = strategy
        job_plan.ensure_agent_priority_multipliers(tuple(sorted(self.agents.keys())))
        self.current_job_plan = job_plan
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
            location="TownHall",
            details={
                "task_priority_weights": job_plan.task_priority_weights,
                "shared_task_priority_weights": job_plan.task_priority_weights,
                "agent_priority_multipliers": job_plan.agent_priority_multipliers,
                "agent_effective_task_priority_weights": {
                    agent_id: job_plan.effective_task_priority_weights(agent_id) for agent_id in sorted(self.agents.keys())
                },
                "quotas": job_plan.quotas,
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
        low_battery_threshold = float(self._rule("world.battery.deliver_to_others_threshold_min", 15.0))
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
            low_battery = bool((not agent.discharged) and battery_remaining <= low_battery_threshold)
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
                "carrying_item_type": agent.carrying_item_type,
                "suspended_task_type": agent.suspended_task.task_type if isinstance(agent.suspended_task, Task) else None,
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
        }

    def local_state_for_urgent(self) -> dict[str, Any]:
        return {
            "inspection_backlog": len(self.intermediate_queues[self.inspection_queue_station]),
            "broken_machines": sum(1 for m in self.machines.values() if m.broken),
            "discharged_agents": sum(1 for a in self.agents.values() if a.discharged),
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

    def _candidate_summary_for_selector(self, agent: Agent, task: Task, *, include_score_hint: bool) -> dict[str, Any]:
        summary = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "priority_key": self._task_priority_key(task),
            "location": task.location,
            "priority": round(float(task.priority), 3),
            "payload": self._serialize_task_payload(task.payload),
        }
        if include_score_hint:
            summary["score_hint"] = round(float(self._task_score(task, agent)), 3)
        return summary

    def _selector_related_entities(self, candidates: list[Task]) -> tuple[set[str], set[str], set[int]]:
        machine_ids: set[str] = set()
        agent_ids: set[str] = set()
        stations: set[int] = set()
        station_order = list(self.stations)
        station_index = {station: idx for idx, station in enumerate(station_order)}

        for task in candidates:
            payload = task.payload if isinstance(task.payload, dict) else {}
            machine_id = str(payload.get("machine_id", "")).strip()
            if machine_id:
                machine_ids.add(machine_id)
            target_agent_id = str(payload.get("target_agent_id", "")).strip()
            if target_agent_id:
                agent_ids.add(target_agent_id)
            raw_station = payload.get("station")
            if isinstance(raw_station, (int, float)):
                stations.add(int(raw_station))
            transfer_kind = str(payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "inter_station":
                from_station = payload.get("from_station")
                if isinstance(from_station, (int, float)):
                    from_station_int = int(from_station)
                    stations.add(from_station_int)
                    next_idx = station_index.get(from_station_int)
                    if next_idx is not None and next_idx + 1 < len(station_order):
                        stations.add(int(station_order[next_idx + 1]))
            elif transfer_kind == "material_supply":
                station = payload.get("station")
                if isinstance(station, (int, float)):
                    stations.add(int(station))
        return machine_ids, agent_ids, stations

    def _selector_machine_needs_detail(self, machine_id: str, machine_obs: dict[str, Any], related_machine_ids: set[str]) -> bool:
        if machine_id in related_machine_ids:
            return True
        if bool(machine_obs.get("broken", False)):
            return True
        if bool(machine_obs.get("has_output_waiting_unload", False)):
            return True
        if machine_obs.get("minutes_since_failure_started") is not None:
            return True
        owners = machine_obs.get("owners", {}) if isinstance(machine_obs.get("owners", {}), dict) else {}
        if any(owner for owner in owners.values()):
            return True
        state = str(machine_obs.get("state", "")).strip().upper()
        if state in {"BROKEN", "DONE_WAIT_UNLOAD", "SETUP", "REPAIR", "PM"}:
            return True
        return False

    def _selector_agent_needs_detail(self, agent_id: str, agent_obs: dict[str, Any], related_agent_ids: set[str]) -> bool:
        if agent_id in related_agent_ids:
            return True
        if bool(agent_obs.get("discharged", False)) or bool(agent_obs.get("low_battery", False)):
            return True
        if agent_obs.get("awaiting_battery_from") is not None:
            return True
        if agent_obs.get("battery_service_owner") is not None:
            return True
        if agent_obs.get("in_transit") is not None:
            return True
        if agent_obs.get("current_task_type"):
            return True
        status = str(agent_obs.get("status", "")).strip().upper()
        if status not in {"", "IDLE"}:
            return True
        return False


    def _compact_strategy_diagnosis_for_selector(self) -> dict[str, Any]:
        diagnosis = dict(getattr(self.current_strategy, "diagnosis", {}) or {})
        compact: dict[str, Any] = {}
        summary = str(getattr(self.current_strategy, "summary", "") or "").strip()
        if summary:
            compact["summary"] = summary
        for key in ("flow_risks", "maintenance_risks", "inspection_risks", "battery_risks"):
            values = diagnosis.get(key, []) if isinstance(diagnosis.get(key, []), list) else []
            cleaned = [str(item).strip() for item in values if str(item).strip()]
            if cleaned:
                compact[key] = cleaned[:2]
        return compact

    def _build_task_selection_context(
        self,
        agent: Agent,
        candidates: list[Task],
        *,
        include_score_hints: bool,
    ) -> dict[str, Any]:
        observation = self.build_observation(getattr(self, "last_day_summary", None))
        agents_obs = observation.get("agents", {}) if isinstance(observation.get("agents", {}), dict) else {}
        agents_by_id = agents_obs.get("by_id", {}) if isinstance(agents_obs.get("by_id", {}), dict) else {}
        machines_obs = observation.get("machines", {}) if isinstance(observation.get("machines", {}), dict) else {}
        machines_by_id = machines_obs.get("by_id", {}) if isinstance(machines_obs.get("by_id", {}), dict) else {}
        candidate_tasks = [
            self._candidate_summary_for_selector(agent, task, include_score_hint=include_score_hints) for task in candidates
        ]
        related_machine_ids, related_agent_ids, related_station_ids = self._selector_related_entities(candidates)
        focused_machines_by_id = {
            machine_id: data
            for machine_id, data in machines_by_id.items()
            if self._selector_machine_needs_detail(machine_id, data, related_machine_ids)
        }
        peer_agents_by_id = {
            other_id: data
            for other_id, data in agents_by_id.items()
            if other_id != agent.agent_id and self._selector_agent_needs_detail(other_id, data, related_agent_ids)
        }
        agent_snapshot = dict(agents_by_id.get(agent.agent_id, {}))
        agent_snapshot["agent_id"] = agent.agent_id
        agent_experience_summary: dict[str, Any] = {}
        selector_experience_fn = getattr(self.decision_module, "selector_agent_experience_view", None)
        if callable(selector_experience_fn):
            try:
                candidate = selector_experience_fn(agent.agent_id)
                if isinstance(candidate, dict):
                    agent_experience_summary = candidate
            except Exception:
                agent_experience_summary = {}
        return {
            "agent_id": agent.agent_id,
            "mode": self.decision_mode,
            "selection_rules": {
                "hard_constraints_already_applied": True,
                "must_choose_one_candidate_task_id": True,
                "do_not_invent_new_tasks": True,
                "candidate_task_ids": [str(task.task_id) for task in candidates],
                "engine_resolved_before_selector": [
                    "discharged_agent",
                    "awaiting_battery_delivery",
                    "suspended_task_resume",
                    "mandatory_battery_swap",
                    "single_candidate_shortcut",
                ],
            },
            "agent": agent_snapshot,
            "agent_experience_summary": agent_experience_summary,
            "plant_state": {
                "time": {
                    key: value
                    for key, value in (observation.get("time", {}) if isinstance(observation.get("time", {}), dict) else {}).items()
                    if key in {"day", "minutes_per_day", "day_elapsed_min", "day_progress"}
                },
                "queues": observation.get("queues", {}),
                "machines": {
                    "summary": machines_obs.get("summary", {}),
                    "focus_machine_ids": sorted(focused_machines_by_id.keys()),
                    "related_candidate_machine_ids": sorted(related_machine_ids),
                    "by_id": focused_machines_by_id,
                },
                "agents": {
                    "summary": agents_obs.get("summary", {}),
                    "focus_peer_agent_ids": sorted(peer_agents_by_id.keys()),
                    "related_candidate_agent_ids": sorted(related_agent_ids),
                    "peer_agents_by_id": peer_agents_by_id,
                },
                "flow": observation.get("flow", {}),
                "recent_history": observation.get("recent_history", {}),
                "trends": observation.get("trends", {}),
                "focus": {
                    "candidate_station_ids": sorted(related_station_ids),
                },
                "local_state": self.local_state_for_urgent(),
            },
            "current_policy": {
                # Selector gets the current agent's effective priorities plus compact peer/profile context.
                "strategy_diagnosis": self._compact_strategy_diagnosis_for_selector(),
                "shared_task_priority_weights": dict(self.current_job_plan.task_priority_weights or {}),
                "agent_priority_profile": self._agent_priority_profile_summary(agent_ids=[agent.agent_id]).get(agent.agent_id, {}),
                "effective_task_priority_weights": self.current_effective_task_priority_weights(agent.agent_id),
                "peer_priority_profiles": self._agent_priority_profile_summary(agent_ids=[aid for aid in sorted(self.agents.keys()) if aid != agent.agent_id]),
                "quotas": dict(self.current_job_plan.quotas or {}),
            },
            "candidate_tasks": candidate_tasks,
        }

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
        self.trigger_urgent_chat("machine_breakdown", machine.machine_id, {"station": machine.station})
        if was_processing and machine.active_process is not None and machine.active_process.is_alive:
            machine.active_process.interrupt("machine_breakdown")

    def battery_remaining(self, agent: Agent, at_t: float | None = None) -> float:
        t = self.env.now if at_t is None else float(at_t)
        return max(0.0, self.battery_swap_period_min - max(0.0, t - float(agent.last_battery_swap)))

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
        self.trigger_urgent_chat("agent_discharged", agent.agent_id, {"reason": reason})
        if interrupt_process and agent.process_ref is not None and agent.process_ref.is_alive:
            agent.process_ref.interrupt("battery_depleted")
        self.check_all_agents_discharged()

    def trigger_urgent_chat(self, event_type: str, entity_id: str, details: dict[str, Any]) -> None:
        if not self.urgent_discuss_enabled:
            return
        if self.env.now - self.last_urgent_chat_t < self.urgent_chat_cooldown:
            return
        event = {"event_type": event_type, "entity_id": entity_id, "time": self.env.now, "details": details}
        updates = self.decision_module.urgent_discuss(event, self.local_state_for_urgent())
        priority_updates = updates.get("priority_updates", {})
        self.current_job_plan.task_priority_weights.update(priority_updates)
        self.last_urgent_chat_t = self.env.now
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="CHAT_URGENT",
            entity_id="system",
            location="urgent",
            details={"event": event, "priority_updates": priority_updates},
        )

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
        }
        if selection:
            if "decision_source" in selection:
                details["decision_source"] = selection.get("decision_source")
            if "decision_rule" in selection:
                details["decision_rule"] = selection.get("decision_rule")
            if "decision_rationale" in selection:
                details["decision_rationale"] = selection.get("decision_rationale")
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
            }
        )
        if status == "completed":
            agent.total_task_time_min[task.task_type] = agent.total_task_time_min.get(task.task_type, 0.0) + duration
        agent.current_task_id = None
        agent.current_task_type = None
        agent.current_task_started_at = None

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
        self._clear_agent_carrying(agent, emit_event=False)

    def mandatory_task_for_agent(self, agent: Agent) -> Task | None:
        if agent.discharged:
            return None
        battery_remaining = self.battery_remaining(agent)
        threshold = float(self._rule("world.battery.mandatory_swap_threshold_min", 15.0))
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

    def select_task_for_agent(self, agent: Agent) -> Task | None:
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
            return self._annotate_task_selection(
                mandatory,
                decision_source="hard_constraint",
                decision_rule="mandatory_battery_swap",
                rationale="Battery remaining reached the mandatory swap threshold.",
                score_hint=self._task_score(mandatory, agent),
            )

        candidates = self._candidate_tasks(agent)
        if not candidates:
            return None

        scored_candidates = sorted(candidates, key=lambda task: self._task_score(task, agent), reverse=True)
        if len(scored_candidates) == 1:
            task = scored_candidates[0]
            return self._annotate_task_selection(
                task,
                decision_source="single_candidate",
                decision_rule="single_feasible_candidate",
                rationale="Only one feasible task is available.",
                candidate_count=1,
                score_hint=self._task_score(task, agent),
            )

        select_fn = getattr(self.decision_module, "select_next_task", None)
        if callable(select_fn):
            max_candidates = max(0, int(getattr(self.decision_module, "task_selector_max_candidates", 0)))
            include_score_hints = bool(getattr(self.decision_module, "task_selector_include_score_hints", False))
            selector_candidates = scored_candidates[:max_candidates] if max_candidates > 0 else list(scored_candidates)
            selection_context = self._build_task_selection_context(
                agent,
                selector_candidates,
                include_score_hints=include_score_hints,
            )
            try:
                selection = select_fn(selection_context)
            except Exception as exc:
                fallback = scored_candidates[0]
                return self._annotate_task_selection(
                    fallback,
                    decision_source="priority_score_fallback",
                    decision_rule="llm_task_selector_error",
                    rationale="LLM task selection failed; fallback to engine score dispatch.",
                    candidate_count=len(selector_candidates),
                    score_hint=self._task_score(fallback, agent),
                    fallback_reason=str(exc),
                )

            selected_task_id = str(selection.get("selected_task_id", "")).strip() if isinstance(selection, dict) else ""
            chosen_task = next((task for task in selector_candidates if task.task_id == selected_task_id), None)
            if chosen_task is not None:
                rationale = str(selection.get("rationale", "")).strip() if isinstance(selection, dict) else ""
                focus_raw = selection.get("decision_focus", []) if isinstance(selection, dict) else []
                decision_focus = [
                    str(item).strip() for item in focus_raw if isinstance(item, str) and str(item).strip()
                ]
                return self._annotate_task_selection(
                    chosen_task,
                    decision_source="llm_task_selector",
                    decision_rule="llm_candidate_selection",
                    rationale=rationale or "LLM selected one feasible candidate task.",
                    candidate_count=len(selector_candidates),
                    score_hint=self._task_score(chosen_task, agent),
                    decision_focus=decision_focus,
                )

            fallback = scored_candidates[0]
            return self._annotate_task_selection(
                fallback,
                decision_source="priority_score_fallback",
                decision_rule="invalid_llm_task_choice",
                rationale="LLM returned an invalid task_id; fallback to engine score dispatch.",
                candidate_count=len(selector_candidates),
                score_hint=self._task_score(fallback, agent),
                fallback_reason=selected_task_id,
            )

        task = scored_candidates[0]
        return self._annotate_task_selection(
            task,
            decision_source="priority_score",
            decision_rule="priority_score_dispatch",
            rationale="Engine selected the highest scored feasible task.",
            candidate_count=len(scored_candidates),
            score_hint=self._task_score(task, agent),
        )

    def _task_score(self, task: Task, agent: Agent | str | None = None) -> float:
        # LLM modes can personalize the score per agent by multiplying the shared baseline
        # with that agent's learned overlay. Heuristic modes effectively stay on the baseline.
        priority_key = self._task_priority_key(task)
        if isinstance(agent, Agent):
            effective = self.current_effective_task_priority_weights(agent.agent_id)
            weight = float(effective.get(priority_key, 1.0))
        elif isinstance(agent, str) and agent.strip():
            effective = self.current_effective_task_priority_weights(agent.strip())
            weight = float(effective.get(priority_key, 1.0))
        else:
            weight = float((self.current_job_plan.task_priority_weights or {}).get(priority_key, 1.0))
        return float(task.priority) * weight

    def _candidate_tasks(self, agent: Agent) -> list[Task]:
        tasks: list[Task] = []
        deliver_threshold = float(self._rule("world.battery.deliver_to_others_threshold_min", 15.0))
        deliver_priority_discharged = float(self._rule("world.task_priority.battery_delivery_discharged", 149.0))
        deliver_priority_low_battery = float(self._rule("world.task_priority.battery_delivery_low_battery", 140.0))
        priority_repair_machine = float(self._rule("world.task_priority.repair_machine", 115.0))
        priority_unload_machine = float(self._rule("world.task_priority.unload_machine", 110.0))
        priority_setup_machine = float(self._rule("world.task_priority.setup_machine", 90.0))
        priority_pm = float(self._rule("world.task_priority.preventive_maintenance", 65.0))
        priority_inter_station_transfer = float(self._rule("world.task_priority.inter_station_transfer", 85.0))
        priority_material_supply = float(self._rule("world.task_priority.material_supply", 85.0))
        priority_inspect_product = float(self._rule("world.task_priority.inspect_product", 72.0))

        for other in self.agents.values():
            if other.agent_id == agent.agent_id:
                continue
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
        if src == "TownHall" or dst == "TownHall":
            return float(self.movement_cfg["to_townhall_min"])
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

                if needs_material:
                    material_id = str(task.payload.get("material_id", ""))
                    if not material_id:
                        popped_material = self._pop_material_queue(station)
                        if popped_material is None:
                            machine.state = MachineState.WAIT_INPUT
                            return False
                        material_id = popped_material
                        task.payload["material_id"] = material_id
                    # One carry slot: load material first.
                    if not self._set_agent_carrying(agent, "material", material_id):
                        self.material_queues[station].appendleft(material_id)
                        task.payload.pop("material_id", None)
                        machine.state = MachineState.WAIT_INPUT
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
                            return False
                        intermediate_id = popped_intermediate
                        task.payload["intermediate_id"] = intermediate_id
                    # Then load intermediate as a separate one-item carry.
                    if not self._set_agent_carrying(agent, "intermediate", intermediate_id):
                        self.intermediate_queues[station].appendleft(intermediate_id)
                        task.payload.pop("intermediate_id", None)
                        machine.state = MachineState.WAIT_INPUT
                        return False
                    yield self.env.timeout(setup_step)
                    machine.input_intermediate = intermediate_id
                    task.payload.pop("intermediate_id", None)
                    self._clear_agent_carrying(agent, destination=machine.machine_id)

                if machine.input_material is None or (requires_intermediate and machine.input_intermediate is None):
                    machine.state = MachineState.WAIT_INPUT
                    return False

                machine.state = MachineState.IDLE
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
        self.items[output_id] = Item(item_id=output_id, item_type=output_type, created_at=self.env.now, current_station=machine.station)
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
        # becomes the compact personal memory shown to townhall and selector prompts.
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
        snapshots = [s for s in self.minute_snapshots if s["day"] == day]
        if snapshots:
            avg_wip_material = mean(sum(s["material_queue_lengths"].values()) for s in snapshots)
            avg_wip_intermediate = mean(sum(s["intermediate_queue_lengths"].values()) for s in snapshots)
        else:
            avg_wip_material = 0.0
            avg_wip_intermediate = 0.0

        task_slice = self.task_records[int(self.day_baseline["task_count"]) :]
        task_breakdown: dict[str, float] = defaultdict(float)
        for rec in task_slice:
            if rec["status"] == "completed":
                task_breakdown[rec["task_type"]] += float(rec["duration"])

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
            "battery_delivery_count": int(battery_delivery_count),
            "days_since_last_product": int(days_since_last_product),
            "task_minutes": dict(task_breakdown),
            "machine_processing_min": processing_delta,
            "machine_broken_min": broken_delta,
            "machine_pm_min": pm_delta,
            "inspection_backlog_end": len(self.intermediate_queues[self.inspection_queue_station]),
            "agent_experience": agent_experience,
            "shared_task_priority_weights": dict(self.current_job_plan.task_priority_weights or {}),
            "agent_priority_multipliers": {agent_id: self.current_agent_priority_multipliers(agent_id) for agent_id in sorted(self.agents.keys())},
            "agent_effective_task_priority_weights": {agent_id: self.current_effective_task_priority_weights(agent_id) for agent_id in sorted(self.agents.keys())},
        }
        self.daily_summaries.append(summary)
        return summary

    def finalize_kpis(self) -> dict[str, Any]:
        total_checked = self.product_count + self.scrap_count
        total_time = max(1.0, float(self.env.now))
        n_machines = len(self.machines)
        total_machine_processing = sum(m.total_processing_min for m in self.machines.values())
        total_machine_broken = sum(m.total_broken_min for m in self.machines.values())
        total_machine_pm = sum(m.total_pm_min for m in self.machines.values())

        task_totals: dict[str, float] = defaultdict(float)
        for rec in self.task_records:
            if rec["status"] == "completed":
                task_totals[rec["task_type"]] += rec["duration"]

        return {
            "total_products": self.product_count,
            "scrap_count": self.scrap_count,
            "scrap_rate": round((self.scrap_count / total_checked) if total_checked > 0 else 0.0, 6),
            "station_throughput": dict(self.station_throughput),
            "avg_daily_products": round(self.product_count / self.num_days, 4),
            "avg_wip_material": round(
                mean(sum(s["material_queue_lengths"].values()) for s in self.minute_snapshots),
                4,
            )
            if self.minute_snapshots
            else 0.0,
            "avg_wip_intermediate": round(mean(sum(s["intermediate_queue_lengths"].values()) for s in self.minute_snapshots), 4)
            if self.minute_snapshots
            else 0.0,
            "machine_utilization": round(total_machine_processing / max(1.0, total_time * n_machines), 6),
            "machine_broken_ratio": round(total_machine_broken / max(1.0, total_time * n_machines), 6),
            "machine_pm_ratio": round(total_machine_pm / max(1.0, total_time * n_machines), 6),
            "agent_task_minutes": dict(task_totals),
            "terminated": self.terminated,
            "termination_reason": self.termination_reason,
        }
