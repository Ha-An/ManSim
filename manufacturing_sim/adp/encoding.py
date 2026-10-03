from __future__ import annotations

from typing import Any

import numpy as np

from manufacturing_sim.simulation.scenarios.manufacturing.entities import MachineState

from .schema import (
    GLOBAL_FEATURE_DIM,
    PAIR_FEATURE_DIM,
    TASK_FEATURE_DIM,
    WORKER_FEATURE_DIM,
    EncodedDecisionState,
)


def _norm(value: float, scale: float) -> float:
    return float(max(-2.0, min(2.0, float(value) / max(1e-9, float(scale)))))


_DOWNSTREAM_PROGRESS_BY_ROLE = {
    1: 0.05,
    2: 0.30,
    3: 0.35,
    4: 0.72,
    5: 1.00,
    6: 0.95,
    7: 0.10,
    8: 0.40,
    9: 0.45,
    10: 0.15,
    11: 0.50,
    12: 0.25,
    13: 0.65,
    14: 0.80,
    15: 0.88,
    16: 0.94,
    17: 0.00,
}

# Roles that still have to be completed after the candidate action. These are
# expected work-content paths, not precedence enforcement; feasibility remains
# owned by the simulator. The feature derived from this map is deliberately
# state-dependent through machine and queue delays below.
_TERMINAL_REMAINING_ROLES = {
    1: (7, 10, 12, 3, 2, 8, 9, 11, 13, 4, 14, 15, 16, 5),
    2: (8, 9, 11, 13, 4, 14, 15, 16, 5),
    3: (8, 9, 11, 13, 4, 14, 15, 16, 5),
    4: (14, 15, 16, 5),
    5: (),
    6: (),
    7: (10, 12, 3, 2, 8, 9, 11, 13, 4, 14, 15, 16, 5),
    8: (9, 11, 13, 4, 14, 15, 16, 5),
    9: (8, 11, 13, 4, 14, 15, 16, 5),
    10: (12, 3, 2, 8, 9, 11, 13, 4, 14, 15, 16, 5),
    11: (13, 4, 14, 15, 16, 5),
    12: (3, 2, 8, 9, 11, 13, 4, 14, 15, 16, 5),
    13: (4, 14, 15, 16, 5),
    14: (15, 16, 5),
    15: (16, 5),
    16: (5,),
}

_STATION_1_PROCESS_PENDING_ROLES = {1, 7, 10}
_STATION_2_PROCESS_PENDING_ROLES = {1, 2, 3, 7, 8, 9, 10, 11, 12}
_STATION_1_QUEUE_WAIT_ROLES = {1, 7}
_STATION_2_QUEUE_WAIT_ROLES = {1, 2, 3, 7, 8, 9, 10, 12}
_INSPECTION_WAIT_ROLES = {1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13}


def _role_expected_duration(world: Any, role_number: int) -> float:
    policy = getattr(world, "mfg_flow_task_policy", None)
    metrics = getattr(policy, "rule_metrics", {}) if policy is not None else {}
    for metric in metrics.values() if isinstance(metrics, dict) else ():
        if int(metric.get("role_number", 0) or 0) == int(role_number):
            return max(0.0, float(metric.get("expected_duration_min", 0.0) or 0.0))
    return 0.0


def _machine_release_delay(world: Any, station: int) -> float:
    machines = [machine for machine in world.machines.values() if int(machine.station) == int(station)]
    if not machines:
        return 0.0
    repair_min = float(world.timing.expected_task_duration("REPAIR_MACHINE"))
    pm_min = float(world.timing.expected_task_duration("PREVENTIVE_MAINTENANCE"))
    unload_role = 12 if int(station) == 1 else 13
    unload_min = _role_expected_duration(world, unload_role)
    release_delays: list[float] = []
    for machine in machines:
        if machine.broken or machine.state in {MachineState.BROKEN, MachineState.UNDER_REPAIR}:
            release_delays.append(max(0.0, float(machine.repair_work_remaining_min or repair_min)))
        elif machine.state == MachineState.UNDER_PM:
            release_delays.append(pm_min)
        elif machine.state == MachineState.PROCESSING:
            release_delays.append(world.machine_remaining_processing_min(machine))
        elif machine.state == MachineState.DONE_WAIT_UNLOAD or machine.output_intermediate is not None:
            release_delays.append(unload_min)
        elif machine.state == MachineState.SETUP:
            release_delays.append(max(0.0, float(world.processing_time_min.get(station, 0.0))))
        else:
            release_delays.append(0.0)
    return min(release_delays, default=0.0)


def _station_queue_delay(world: Any, station: int) -> float:
    machines = [machine for machine in world.machines.values() if int(machine.station) == int(station)]
    machine_count = max(1, len(machines))
    if int(station) == 1:
        ready_count = len(world.material_queues.get(1, ()))
    else:
        ready_count = min(
            len(world.material_queues.get(2, ())),
            len(world.intermediate_queues.get(2, ())),
        )
    ahead = max(0, int(ready_count) - 1)
    process_min = max(0.0, float(world.processing_time_min.get(station, 0.0)))
    return _machine_release_delay(world, station) + ahead * process_min / machine_count


def _inspection_queue_delay(world: Any) -> float:
    station = int(getattr(world, "inspection_queue_station", 4) or 4)
    waiting = len(world.intermediate_queues.get(station, ()))
    desk_busy = int(getattr(world, "inspection_desk_item_id", None) is not None)
    pass_waiting = len(world.output_buffers.get(station, ()))
    inspection_cycle = (
        _role_expected_duration(world, 14)
        + _role_expected_duration(world, 15)
        + _role_expected_duration(world, 16)
    )
    terminal_transfer = _role_expected_duration(world, 5)
    return (waiting + desk_busy) * inspection_cycle + pass_waiting * terminal_transfer


def _expected_terminal_closure_minutes(world: Any, task: Any) -> float | None:
    """Expected post-action minutes until the affected item reaches a terminal.

    The estimate uses only information observable at the decision epoch. It
    combines remaining expected work content with current machine-release,
    queue, and inspection congestion. Non-item support actions return ``None``
    so the encoder can represent the feature as not applicable.
    """

    rule = world._mfg_flow_task_rule(task)
    role_number = int(getattr(rule, "role_number", 0) or 0)
    if role_number not in _TERMINAL_REMAINING_ROLES:
        return None

    remaining = sum(
        _role_expected_duration(world, downstream_role)
        for downstream_role in _TERMINAL_REMAINING_ROLES[role_number]
    )
    if role_number in _STATION_1_PROCESS_PENDING_ROLES:
        remaining += max(0.0, float(world.processing_time_min.get(1, 0.0)))
    if role_number in _STATION_2_PROCESS_PENDING_ROLES:
        remaining += max(0.0, float(world.processing_time_min.get(2, 0.0)))
    if role_number in _STATION_1_QUEUE_WAIT_ROLES:
        remaining += _station_queue_delay(world, 1)
    if role_number in _STATION_2_QUEUE_WAIT_ROLES:
        remaining += _station_queue_delay(world, 2)
    if role_number in _INSPECTION_WAIT_ROLES:
        remaining += _inspection_queue_delay(world)
    return max(0.0, remaining)


def _buffer_group_features(world: Any, buffer_ids: list[str]) -> tuple[float, float, float]:
    capacity = sum(int(world._buffer_capacity(buffer_id) or 0) for buffer_id in buffer_ids)
    if capacity <= 0:
        return 0.0, 0.0, 0.0
    occupancy = sum(len(world._buffer_queue(buffer_id) or ()) for buffer_id in buffer_ids)
    reserved = sum(int(world._buffer_reserved_count(buffer_id)) for buffer_id in buffer_ids)
    free = max(0, capacity - occupancy - reserved)
    return (
        float(occupancy) / capacity,
        float(reserved) / capacity,
        float(free) / capacity,
    )


def _task_downstream_progress(world: Any, task: Any) -> float:
    rule = world._mfg_flow_task_rule(task)
    role_number = int(getattr(rule, "role_number", 0) or 0)
    if role_number == 18:
        machine = world.machines.get(str(task.payload.get("machine_id", "") or ""))
        return 0.25 if machine is not None and int(machine.station) == 1 else 0.65
    return float(_DOWNSTREAM_PROGRESS_BY_ROLE.get(role_number, 0.0))


def _task_releases_blockage(world: Any, task: Any) -> float:
    payload = task.payload if isinstance(task.payload, dict) else {}
    task_type = str(task.task_type or "").strip().upper()
    machine = world.machines.get(str(payload.get("machine_id", "") or ""))
    if task_type == "UNLOAD_MACHINE" and machine is not None:
        return float(
            machine.output_intermediate is not None
            or machine.state == MachineState.DONE_WAIT_UNLOAD
        )
    if task_type == "REPAIR_MACHINE" and machine is not None:
        return float(
            bool(machine.broken)
            or machine.state in {MachineState.BROKEN, MachineState.UNDER_REPAIR}
        )
    if task_type == "TRANSFER":
        try:
            from_station = int(payload.get("from_station", 0) or 0)
        except (TypeError, ValueError):
            from_station = 0
        source_buffer = f"output_buffer_station_{from_station}"
        capacity = world._buffer_capacity(source_buffer)
        source_full = bool(
            capacity is not None
            and len(world._buffer_queue(source_buffer) or ()) >= int(capacity)
        )
        blocked_machine = any(
            machine.station == from_station
            and (
                machine.output_intermediate is not None
                or machine.state == MachineState.DONE_WAIT_UNLOAD
            )
            for machine in world.machines.values()
        )
        return float(source_full and blocked_machine)
    if task_type == "LOAD_UNLOAD_TRANSFER_INTERFACE":
        action = str(payload.get("interface_action") or payload.get("action") or "").lower()
        return float(action == "unload" and world.inspection_desk_item_id is not None)
    return 0.0


def _destination_capacity_features(world: Any, task: Any) -> tuple[float, float]:
    buffer_id = str(world._task_destination_buffer_id(task) or "")
    capacity = world._buffer_capacity(buffer_id)
    if not buffer_id or capacity is None or int(capacity) <= 0:
        return 0.0, 0.0
    available = world._buffer_available_slots(
        buffer_id,
        exclude_task_id=str(task.task_id or ""),
    )
    return 1.0, float(available) / float(capacity)


class ADPStateEncoder:
    def __init__(
        self,
        *,
        review_interval_min: float = 1.0,
        allow_wait_action: bool = False,
    ) -> None:
        self.review_interval_min = max(0.0, float(review_interval_min))
        self.allow_wait_action = bool(allow_wait_action)

    def encode(
        self,
        world: Any,
        workers: list[Any],
        tasks_by_worker: dict[str, dict[str, Any]],
    ) -> EncodedDecisionState:
        opportunity_ids = sorted({opportunity for rows in tasks_by_worker.values() for opportunity in rows})
        canonical_tasks = {
            opportunity: next(rows[opportunity] for rows in tasks_by_worker.values() if opportunity in rows)
            for opportunity in opportunity_ids
        }
        horizon = max(1.0, float(world.num_days * world.minutes_per_day))
        machine_values = sorted(world.machines.values(), key=lambda machine: machine.machine_id)
        global_features = np.zeros(GLOBAL_FEATURE_DIM, dtype=np.float32)
        global_features[:12] = np.asarray(
            [
                _norm(horizon - world.env.now, horizon),
                _norm(world.product_count, 30.0),
                _norm(world.scrap_count, 30.0),
                _norm(world._material_shelf_count(), 30.0),
                _norm(sum(len(q) for q in world.material_queues.values()), 20.0),
                _norm(sum(len(q) for q in world.intermediate_queues.values()), 20.0),
                _norm(sum(len(q) for q in world.output_buffers.values()), 20.0),
                _norm(sum(1 for m in machine_values if m.broken), max(1, len(machine_values))),
                _norm(sum(1 for m in machine_values if m.state == MachineState.PROCESSING), max(1, len(machine_values))),
                _norm(len(workers), max(1, world.num_workers)),
                _norm(len(opportunity_ids), 20.0),
                _norm(world.num_workers, 6.0),
            ],
            dtype=np.float32,
        )
        # Each processing station contributes finite-buffer and machine-state
        # aggregates. The representation stays fixed while retaining the
        # bottleneck context of parallel machines.
        for index, station in enumerate(sorted(world.stations)[:2]):
            base = 12 + index * 9
            station_machines = [machine for machine in machine_values if machine.station == station]
            input_buffer_ids = [f"material_queue_{station}"]
            if world._station_requires_intermediate(station):
                input_buffer_ids.append(f"intermediate_queue_{station}")
            input_occupancy, input_reserved, _ = _buffer_group_features(
                world,
                input_buffer_ids,
            )
            output_occupancy, output_reserved, _ = _buffer_group_features(
                world,
                [f"output_buffer_station_{station}"],
            )
            global_features[base : base + 4] = np.asarray(
                [
                    input_occupancy,
                    input_reserved,
                    output_occupancy,
                    output_reserved,
                ],
                dtype=np.float32,
            )
            machine_count = max(1, len(station_machines))
            idle_count = sum(
                1
                for machine in station_machines
                if not machine.broken
                and machine.state in {MachineState.IDLE, MachineState.WAIT_INPUT}
            )
            processing_count = sum(
                1 for machine in station_machines if machine.state == MachineState.PROCESSING
            )
            blocked_count = sum(
                1
                for machine in station_machines
                if machine.output_intermediate is not None
                or machine.state == MachineState.DONE_WAIT_UNLOAD
            )
            broken_count = sum(
                1
                for machine in station_machines
                if machine.broken
                or machine.state in {MachineState.BROKEN, MachineState.UNDER_REPAIR}
            )
            remaining_values = [
                world.machine_remaining_processing_min(machine)
                for machine in station_machines
                if machine.state == MachineState.PROCESSING
            ]
            global_features[base + 4 : base + 8] = np.asarray(
                [
                    idle_count / machine_count,
                    processing_count / machine_count,
                    blocked_count / machine_count,
                    broken_count / machine_count,
                ],
                dtype=np.float32,
            )
            global_features[base + 8] = np.float32(
                _norm(
                    float(np.mean(remaining_values)) if remaining_values else 0.0,
                    max(
                        1.0,
                        max(
                            (float(machine.process_time_min) for machine in station_machines),
                            default=1.0,
                        ),
                    ),
                )
            )

        all_workers = sorted(world.workers.values(), key=lambda worker: worker.agent_id)
        decision_worker_ids = {worker.agent_id for worker in workers}
        worker_features = np.zeros((len(all_workers), WORKER_FEATURE_DIM), dtype=np.float32)
        for row, worker in enumerate(all_workers):
            tile = worker.tile or (0, 0)
            state = worker.humanoid_state if isinstance(worker.humanoid_state, dict) else {}
            worker_features[row, :12] = [
                _norm(tile[0], 100.0),
                _norm(tile[1], 70.0),
                _norm(world.battery_remaining(worker), world.battery_swap_period_min),
                float(worker.discharged),
                float(worker.current_task_id is not None),
                float(worker.suspended_task is not None),
                float(worker.carrying_item_id is not None),
                _norm(worker.carrying_item_count, 3.0),
                float(str(state.get("availability", "")).upper() == "AVAILABLE"),
                float(str(state.get("mobility", "")).upper() in {"NAVIGATING", "MOBILE"}),
                float(worker.charging_started_at is not None),
                _norm(len(tasks_by_worker.get(worker.agent_id, {})), 20.0),
            ]
            started_at = worker.current_task_started_at
            elapsed = max(0.0, float(world.env.now) - float(started_at if started_at is not None else world.env.now))
            expected = (
                float(world.timing.expected_task_duration(str(worker.current_task_code).upper()))
                if str(worker.current_task_code or "").upper() in world.timing.task_steps
                else 0.0
            )
            worker_features[row, 14] = _norm(max(0.0, expected - elapsed), 60.0)
            worker_features[row, 15] = float(worker.agent_id in decision_worker_ids)

        task_features = np.zeros((len(opportunity_ids), TASK_FEATURE_DIM), dtype=np.float32)
        role_count = 19.0
        for col, opportunity in enumerate(opportunity_ids):
            task = canonical_tasks[opportunity]
            rule = world._mfg_flow_task_rule(task)
            station = world._task_target_station(task) or 0
            task_features[col, :17] = [
                _norm(getattr(rule, "role_number", 0), role_count),
                _norm(station, 4.0),
                float(str(task.task_type).upper() == "REPAIR_MACHINE"),
                float(str(task.task_type).upper() in {"BATTERY_CHARGE", "BATTERY_SWAP"}),
                float(str(task.task_type).upper() == "TRANSFER"),
                float(str(task.task_type).upper() == "LOAD_MACHINE"),
                float(str(task.task_type).upper() == "UNLOAD_MACHINE"),
                float(str(task.task_type).upper() == "INSPECT_PRODUCT"),
                float(str(task.task_type).upper() == "LOAD_UNLOAD_TRANSFER_INTERFACE"),
                _norm(task.priority, 200.0),
                _norm(world.env.now - float(task.payload.get("first_seen_min", world.env.now)), horizon),
                float(task.payload.get("load_slot") == "material"),
                float(task.payload.get("load_slot") == "intermediate"),
                float(task.payload.get("interface_action") == "load"),
                float(task.payload.get("interface_action") == "unload"),
                float(task.payload.get("transfer_kind") == "material_supply"),
                float(task.payload.get("transfer_kind") == "inter_station"),
            ]
            task_features[col, 18] = np.float32(
                max(0.0, min(1.0, float(task.payload.get("repair_urgency_score", 0.0) or 0.0)))
            )
            task_features[col, 19] = float(
                str(task.payload.get("repair_urgency_tier", "")).strip().lower() == "critical"
            )
            terminal_closure_min = _expected_terminal_closure_minutes(world, task)
            task_features[col, 20] = (
                -1.0
                if terminal_closure_min is None
                else _norm(terminal_closure_min, 120.0)
            )

        feasibility = np.zeros((len(all_workers), len(opportunity_ids)), dtype=bool)
        pair_features = np.zeros((len(all_workers), len(opportunity_ids), PAIR_FEATURE_DIM), dtype=np.float32)
        for row, worker in enumerate(all_workers):
            rows = tasks_by_worker.get(worker.agent_id, {})
            for col, opportunity in enumerate(opportunity_ids):
                task = rows.get(opportunity)
                if task is None:
                    continue
                feasibility[row, col] = True
                travel = float(world.travel_time(world.agent_display_location(worker), task.location))
                duration = float(world._task_estimated_duration(worker, task))
                finite_destination, destination_free = _destination_capacity_features(
                    world,
                    task,
                )
                battery_risk = world._task_battery_risk_metadata(worker, task, estimated_duration=duration)
                pair_features[row, col] = [
                    _norm(travel, 30.0),
                    _norm(duration, 60.0),
                    _norm(
                        float(battery_risk["expected_battery_margin_min"]),
                        world.battery_swap_period_min,
                    ),
                    float(str(task.task_type).upper() == "REPAIR_MACHINE"),
                    _task_downstream_progress(world, task),
                    _task_releases_blockage(world, task),
                    finite_destination,
                    destination_free,
                ]
        return EncodedDecisionState(
            global_features=global_features,
            worker_features=worker_features,
            task_features=task_features,
            pair_features=pair_features,
            feasibility=feasibility,
            selected_assignment_mask=np.zeros_like(feasibility, dtype=bool),
            worker_ids=[worker.agent_id for worker in all_workers],
            opportunity_ids=opportunity_ids,
            decision_worker_ids=[worker.agent_id for worker in workers],
            tasks_by_worker=tasks_by_worker,
            time_min=float(world.env.now),
            horizon_min=horizon,
            review_interval_min=self.review_interval_min,
            wait_action_enabled=self.allow_wait_action,
        )
