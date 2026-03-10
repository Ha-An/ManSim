from __future__ import annotations

import itertools
import math
import random
from collections import defaultdict, deque
from statistics import mean
from typing import Any

import simpy

from manufacturing_sim.simulation.scenarios.manufacturing.decision.base import JobPlan, StrategyState
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
        self.current_job_plan = JobPlan(task_weights={}, quotas={}, rationale="default")
        self.norms: dict[str, Any] = {
            "min_pm_per_machine_per_day": int(
                self._rule("world.initial_norms.min_pm_per_machine_per_day", 1)
            ),
            "quality_weight": float(self._rule("world.initial_norms.quality_weight", 1.0)),
        }

        self.material_queues: dict[int, deque[str]] = {station: deque() for station in self.stations}
        # Station1 does not consume component; component queues start at Station2.
        self.component_queues: dict[int, deque[str]] = {
            station: deque() for station in self.stations if self._station_requires_component(station)
        }
        self.component_queues[self.inspection_queue_station] = deque()
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

    def _station_requires_component(self, station: int) -> bool:
        # First stage is material-only; later stages require material + component.
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
            details={"bottleneck_station": strategy.bottleneck_station, "notes": strategy.notes},
        )
        self.logger.log(
            t=self.env.now,
            day=day,
            event_type="PHASE_JOB_ASSIGNMENT",
            entity_id="system",
            location="TownHall",
            details={"task_weights": job_plan.task_weights, "quotas": job_plan.quotas},
        )

    def build_observation(self, last_day_summary: dict[str, Any] | None) -> dict[str, Any]:
        return {
            "t": self.env.now,
            "day": self.current_day,
            "component_queue_lengths": {k: len(v) for k, v in self.component_queues.items()},
            "material_queue_lengths": {k: len(v) for k, v in self.material_queues.items()},
            "inspection_backlog": len(self.component_queues[self.inspection_queue_station]),
            "machine_states": {mid: m.state.value for mid, m in self.machines.items()},
            "last_day_machine_breaks": 0 if not last_day_summary else last_day_summary.get("machine_breakdowns", 0),
            "last_day_scrap_rate": 0.0 if not last_day_summary else last_day_summary.get("scrap_rate", 0.0),
        }

    def local_state_for_urgent(self) -> dict[str, Any]:
        return {
            "inspection_backlog": len(self.component_queues[self.inspection_queue_station]),
            "broken_machines": sum(1 for m in self.machines.values() if m.broken),
            "discharged_agents": sum(1 for a in self.agents.values() if a.discharged),
        }

    def capture_snapshot(self) -> None:
        t = self.env.now
        self.minute_snapshots.append(
            {
                "t": round(t, 3),
                "day": self.day_for_time(t),
                "material_queue_lengths": {k: len(v) for k, v in self.material_queues.items()},
                "component_queue_lengths": {k: len(v) for k, v in self.component_queues.items()},
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

    def _push_component_queue(self, station: int, item_id: str) -> None:
        if station not in self.component_queues:
            raise ValueError(f"component queue for station {station} is not defined")
        self.component_queues[station].append(item_id)
        location = "Inspection" if station == self.inspection_queue_station else f"Station{station}"
        queue_name = "product" if station == self.inspection_queue_station else "component"
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_PUSH",
            entity_id=f"component_queue_{station}",
            location=location,
            details={"item_id": item_id, "queue": queue_name},
        )

    def _pop_component_queue(self, station: int) -> str | None:
        if station not in self.component_queues:
            return None
        if not self.component_queues[station]:
            return None
        item_id = self.component_queues[station].popleft()
        location = "Inspection" if station == self.inspection_queue_station else f"Station{station}"
        queue_name = "product" if station == self.inspection_queue_station else "component"
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="QUEUE_POP",
            entity_id=f"component_queue_{station}",
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
        if self.env.now - self.last_urgent_chat_t < self.urgent_chat_cooldown:
            return
        event = {"event_type": event_type, "entity_id": entity_id, "time": self.env.now, "details": details}
        updates = self.decision_module.urgent_discuss(event, self.local_state_for_urgent())
        weight_updates = updates.get("weight_updates", {})
        self.current_job_plan.task_weights.update(weight_updates)
        self.last_urgent_chat_t = self.env.now
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="CHAT_URGENT",
            entity_id="system",
            location="urgent",
            details={"event": event, "weight_updates": weight_updates},
        )

    def start_agent_task(self, agent: Agent, task: Task, start_t: float) -> None:
        agent.current_task_id = task.task_id
        agent.current_task_type = task.task_type
        agent.current_task_started_at = start_t
        self.logger.log(
            t=start_t,
            day=self.day_for_time(start_t),
            event_type="AGENT_TASK_START",
            entity_id=agent.agent_id,
            location=self.agent_display_location(agent),
            details={"task_id": task.task_id, "task_type": task.task_type, "payload": task.payload, "category": task.category},
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
        self.task_records.append(
            {
                "day": self.day_for_time(end_t),
                "agent_id": agent.agent_id,
                "task_id": task.task_id,
                "task_type": task.task_type,
                "status": status,
                "start_t": start_t,
                "end_t": end_t,
                "duration": duration,
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
                    moved_id = task.payload.pop("transfer_component_id", None)
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
            component_id = task.payload.pop("component_id", None)
            if material_id is not None:
                self.material_queues[station].appendleft(material_id)
            if component_id is not None and station in self.component_queues:
                self.component_queues[station].appendleft(component_id)
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
                self.component_queues[self.inspection_queue_station].appendleft(product_id)

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
                elif machine.output_component is not None:
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
                category="safety",
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
            return agent.suspended_task
        mandatory = self.mandatory_task_for_agent(agent)
        if mandatory is not None:
            return mandatory
        candidates = self._candidate_tasks(agent)
        if not candidates:
            return None
        return sorted(candidates, key=self._task_score, reverse=True)[0]

    def _task_score(self, task: Task) -> float:
        category_weight = float((self.current_job_plan.task_weights or {}).get(task.category, 1.0))
        score = task.priority * category_weight
        if task.category == "quality":
            score *= float(self.norms.get("quality_weight", 1.0))
        bottleneck_task_types = set(
            str(v) for v in self._rule("world.task_scoring.bottleneck_task_types", ["SETUP_MACHINE", "TRANSFER"])
        )
        if task.task_type in bottleneck_task_types:
            station = int(task.payload.get("station", 0) or task.payload.get("from_station", 0) or 0)
            if station == self.current_strategy.bottleneck_station:
                score *= float(self._rule("world.task_scoring.bottleneck_multiplier", 1.2))
        return score

    def _candidate_tasks(self, agent: Agent) -> list[Task]:
        tasks: list[Task] = []
        deliver_threshold = float(self._rule("world.battery.deliver_to_others_threshold_min", 15.0))
        deliver_priority_discharged = float(self._rule("world.battery.deliver_priority_discharged", 149.0))
        deliver_priority_low_battery = float(self._rule("world.battery.deliver_priority_low_battery", 140.0))
        priority_repair_machine = float(self._rule("world.task_priority.repair_machine", 115.0))
        priority_unload_machine = float(self._rule("world.task_priority.unload_machine", 110.0))
        priority_setup_machine = float(self._rule("world.task_priority.setup_machine", 90.0))
        priority_pm = float(self._rule("world.task_priority.preventive_maintenance", 65.0))
        priority_transfer = float(
            self._rule(
                "world.task_priority.transfer",
                self._rule("world.task_priority.transfer_component", 85.0),
            )
        )
        priority_inspect_product = float(
            self._rule(
                "world.task_priority.inspect_product",
                self._rule("world.task_priority.inspect_component", 72.0),
            )
        )

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
                        category="safety",
                        priority=deliver_priority,
                        location=self.agent_display_location(other),
                        payload={"transfer_kind": "battery_delivery", "target_agent_id": other.agent_id},
                    )
                )

        for machine in self.machines.values():
            if machine.broken and machine.repair_owner is None:
                tasks.append(
                    Task(
                        task_id=self._next_task_id("RM"),
                        task_type="REPAIR_MACHINE",
                        category="blocking",
                        priority=priority_repair_machine,
                        location=f"Station{machine.station}",
                        payload={"machine_id": machine.machine_id},
                    )
                )
            elif machine.output_component is not None and machine.unload_owner is None:
                tasks.append(
                    Task(
                        task_id=self._next_task_id("UL"),
                        task_type="UNLOAD_MACHINE",
                        category="blocking",
                        priority=priority_unload_machine,
                        location=f"Station{machine.station}",
                        payload={"machine_id": machine.machine_id, "station": machine.station},
                    )
                )
            elif (
                not machine.broken
                and machine.state == MachineState.WAIT_INPUT
                and machine.setup_owner is None
                and machine.output_component is None
                and (
                    machine.input_material is None
                    or (self._station_requires_component(machine.station) and machine.input_component is None)
                )
                and (
                    machine.input_material is not None
                    or len(self.material_queues[machine.station]) > 0
                )
                and (
                    not self._station_requires_component(machine.station)
                    or machine.input_component is not None
                    or len(self.component_queues[machine.station]) > 0
                )
            ):
                tasks.append(
                    Task(
                        task_id=self._next_task_id("SET"),
                        task_type="SETUP_MACHINE",
                        category="flow",
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
                and machine.output_component is None
                and machine.pm_owner is None
            ):
                tasks.append(
                    Task(
                        task_id=self._next_task_id("PM"),
                        task_type="PREVENTIVE_MAINTENANCE",
                        category="maintenance",
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
                        category="flow",
                        priority=priority_transfer,
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
                        category="supply",
                        priority=priority_transfer,
                        location="Warehouse",
                        payload={"transfer_kind": "material_supply", "station": station},
                    )
                )

        if self.component_queues[self.inspection_queue_station]:
            tasks.append(
                Task(
                    task_id=self._next_task_id("INS"),
                    task_type="INSPECT_PRODUCT",
                    category="quality",
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
                machine.state = MachineState.DONE_WAIT_UNLOAD if machine.output_component is not None else MachineState.WAIT_INPUT
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
                if machine.broken or machine.output_component is None:
                    return False
                yield from self.move_agent(agent, f"Station{machine.station}", emit_move_events=True)
                if machine.broken:
                    return False
                yield self.env.timeout(float(self.movement_cfg["unload_min"]))
                if machine.broken:
                    return False
                output_id = machine.output_component
                if output_id is not None:
                    carried_kind = "product" if machine.station == self.last_processing_station else "component"
                    if not self._set_agent_carrying(agent, carried_kind, output_id):
                        return False
                machine.output_component = None
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
                    moved_item_kind = "product" if from_station >= self.last_processing_station else "component"
                    if not self._set_agent_carrying(agent, moved_item_kind, moved_item_id):
                        self.output_buffers[from_station].appendleft(moved_item_id)
                        task.payload.pop("transfer_item_id", None)
                        return False
                elif agent.carrying_item_id != moved_item_id:
                    moved_item_kind = "product" if from_station >= self.last_processing_station else "component"
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
                    self._push_component_queue(target_queue_station, moved_item_id)
                task.payload.pop("transfer_item_id", None)
                moved_item_kind = "product" if from_station >= self.last_processing_station else "component"
                self._clear_agent_carrying(agent, destination=to_location)
                if from_station == self.inspection_queue_station:
                    move_to = "Warehouse"
                else:
                    move_to = (
                        f"product_queue_{self.inspection_queue_station}"
                        if (from_station + 1) == self.inspection_queue_station
                        else f"component_queue_{from_station + 1}"
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
                if machine.broken or machine.output_component is not None:
                    return False
                if machine.state not in {MachineState.WAIT_INPUT, MachineState.SETUP}:
                    return False
                requires_component = self._station_requires_component(station)
                needs_material = machine.input_material is None
                needs_component = requires_component and machine.input_component is None
                if not needs_material and not needs_component:
                    machine.state = MachineState.IDLE
                    return False

                has_reserved_material = bool(task.payload.get("material_id"))
                has_reserved_component = bool(task.payload.get("component_id")) if requires_component else False
                if needs_material and not has_reserved_material and not self.material_queues[station]:
                    return False
                if needs_component and not has_reserved_component and not self.component_queues[station]:
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

                if needs_component:
                    component_id = str(task.payload.get("component_id", ""))
                    if not component_id:
                        popped_component = self._pop_component_queue(station)
                        if popped_component is None:
                            machine.state = MachineState.WAIT_INPUT
                            return False
                        component_id = popped_component
                        task.payload["component_id"] = component_id
                    # Then load component as a separate one-item carry.
                    if not self._set_agent_carrying(agent, "component", component_id):
                        self.component_queues[station].appendleft(component_id)
                        task.payload.pop("component_id", None)
                        machine.state = MachineState.WAIT_INPUT
                        return False
                    yield self.env.timeout(setup_step)
                    machine.input_component = component_id
                    task.payload.pop("component_id", None)
                    self._clear_agent_carrying(agent, destination=machine.machine_id)

                if machine.input_material is None or (requires_component and machine.input_component is None):
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
                            self._station_requires_component(station)
                            and machine.input_component is None
                        )
                    ):
                        machine.state = MachineState.WAIT_INPUT
        if task_type == "INSPECT_PRODUCT":
            product_id = str(task.payload.get("inspection_product_id", ""))
            if not product_id and not self.component_queues[self.inspection_queue_station]:
                return False
            yield from self.move_agent(agent, "Inspection", emit_move_events=True)
            if not product_id:
                popped = self._pop_component_queue(4)
                if popped is None:
                    return False
                product_id = popped
                task.payload["inspection_product_id"] = product_id
            if not self._set_agent_carrying(agent, "product", product_id):
                self.component_queues[self.inspection_queue_station].appendleft(product_id)
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
            details={"cycle_id": cycle_id, "input_material": machine.input_material, "input_component": machine.input_component},
        )
        return cycle_id

    def complete_machine_cycle(self, machine: Machine, cycle_id: str) -> None:
        if machine.station == self.last_processing_station:
            output_id = self._next_item_id("PRODUCT")
            output_type = "product"
        else:
            output_id = self._next_item_id(f"COMP-S{machine.station}")
            output_type = "component"
        self.items[output_id] = Item(item_id=output_id, item_type=output_type, created_at=self.env.now, current_station=machine.station)
        machine.input_material = None
        machine.input_component = None
        machine.output_component = output_id
        machine.state = MachineState.DONE_WAIT_UNLOAD
        self.station_throughput[machine.station] += 1
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_END",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={"cycle_id": cycle_id, "output_component": output_id},
        )

    def abort_machine_cycle(self, machine: Machine, cycle_id: str, reason: str) -> None:
        machine.input_material = None
        machine.input_component = None
        machine.state = MachineState.BROKEN if machine.broken else MachineState.WAIT_INPUT
        self.logger.log(
            t=self.env.now,
            day=self.day_for_time(self.env.now),
            event_type="MACHINE_ABORTED",
            entity_id=machine.machine_id,
            location=f"Station{machine.station}",
            details={"cycle_id": cycle_id, "reason": reason},
        )

    def finalize_day(self, day: int) -> dict[str, Any]:
        products_today = self.product_count - int(self.day_baseline["products"])
        scrap_today = self.scrap_count - int(self.day_baseline["scrap"])
        total_checked = products_today + scrap_today
        scrap_rate = (scrap_today / total_checked) if total_checked > 0 else 0.0

        machine_breakdowns = sum(1 for e in self.logger.events if e["day"] == day and e["type"] == "MACHINE_BROKEN")
        snapshots = [s for s in self.minute_snapshots if s["day"] == day]
        if snapshots:
            avg_wip_material = mean(sum(s["material_queue_lengths"].values()) for s in snapshots)
            avg_wip_component = mean(sum(s["component_queue_lengths"].values()) for s in snapshots)
        else:
            avg_wip_material = 0.0
            avg_wip_component = 0.0

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

        summary = {
            "day": day,
            "products": products_today,
            "scrap": scrap_today,
            "scrap_rate": round(scrap_rate, 5),
            "machine_breakdowns": machine_breakdowns,
            "avg_wip_material": round(avg_wip_material, 3),
            "avg_wip_component": round(avg_wip_component, 3),
            "task_minutes": dict(task_breakdown),
            "machine_processing_min": processing_delta,
            "machine_broken_min": broken_delta,
            "machine_pm_min": pm_delta,
            "inspection_backlog_end": len(self.component_queues[self.inspection_queue_station]),
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
            "avg_wip_component": round(mean(sum(s["component_queue_lengths"].values()) for s in self.minute_snapshots), 4)
            if self.minute_snapshots
            else 0.0,
            "machine_utilization": round(total_machine_processing / max(1.0, total_time * n_machines), 6),
            "machine_broken_ratio": round(total_machine_broken / max(1.0, total_time * n_machines), 6),
            "machine_pm_ratio": round(total_machine_pm / max(1.0, total_time * n_machines), 6),
            "agent_task_minutes": dict(task_totals),
            "terminated": self.terminated,
            "termination_reason": self.termination_reason,
        }


