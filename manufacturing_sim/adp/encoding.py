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
                _norm(world.env.now, horizon),
                _norm(horizon - world.env.now, horizon),
                _norm(world.product_count, 30.0),
                _norm(world.scrap_count, 30.0),
                _norm(len(world.warehouse_material_shelf_slots), 30.0),
                _norm(sum(len(q) for q in world.material_queues.values()), 20.0),
                _norm(sum(len(q) for q in world.intermediate_queues.values()), 20.0),
                _norm(sum(len(q) for q in world.output_buffers.values()), 20.0),
                _norm(sum(1 for m in machine_values if m.broken), max(1, len(machine_values))),
                _norm(sum(1 for m in machine_values if m.state == MachineState.PROCESSING), max(1, len(machine_values))),
                _norm(len(workers), max(1, world.num_workers)),
                _norm(len(opportunity_ids), 20.0),
            ],
            dtype=np.float32,
        )
        # The four fixed machine slots represent station aggregates, not the
        # first two machine IDs. This keeps the tensor shape stable while
        # representing both stations when each station has parallel machines.
        for index, station in enumerate(sorted(world.stations)[:2]):
            base = 12 + index * 2
            station_machines = [machine for machine in machine_values if machine.station == station]
            processing_count = sum(
                1 for machine in station_machines if machine.state == MachineState.PROCESSING
            )
            remaining_values = [
                max(0.0, float(machine.cycle_remaining_process_min))
                for machine in station_machines
                if machine.state == MachineState.PROCESSING
            ]
            global_features[base] = np.float32(
                processing_count / max(1, len(station_machines))
            )
            global_features[base + 1] = np.float32(
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
            elapsed = max(0.0, float(world.env.now) - float(worker.current_task_started_at or world.env.now))
            expected = (
                float(world.timing.expected_task_duration(str(worker.current_task_code).upper()))
                if str(worker.current_task_code or "").upper() in world.timing.task_steps
                else 0.0
            )
            worker_features[row, 14] = _norm(max(0.0, expected - elapsed), 60.0)
            worker_features[row, 15] = float(worker.agent_id in {candidate.agent_id for candidate in workers})

        task_features = np.zeros((len(opportunity_ids), TASK_FEATURE_DIM), dtype=np.float32)
        role_count = 18.0
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
                pair_features[row, col] = [
                    _norm(travel, 30.0),
                    _norm(duration, 60.0),
                    _norm(world.battery_remaining(worker) - duration, world.battery_swap_period_min),
                    float(str(task.task_type).upper() == "REPAIR_MACHINE"),
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
