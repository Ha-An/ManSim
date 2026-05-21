from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


def default_humanoid_state_payload(humanoid_id: str = "") -> dict[str, Any]:
    return {
        "humanoid_id": humanoid_id or "",
        "availability": "AVAILABLE",
        "mobility": "STATIONARY",
        "power": "POWER_NORMAL",
        "manipulation": "FREE",
        "task_context": None,
        "reason": None,
        "timestamp_s": None,
        "metadata": {},
    }


class MachineState(str, Enum):
    IDLE = "IDLE"
    WAIT_INPUT = "WAIT_INPUT"
    SETUP = "SETUP"
    PROCESSING = "PROCESSING"
    DONE_WAIT_UNLOAD = "DONE_WAIT_UNLOAD"
    BROKEN = "BROKEN"
    UNDER_REPAIR = "UNDER_REPAIR"
    UNDER_PM = "UNDER_PM"


class ItemState(str, Enum):
    CREATED = "CREATED"
    IN_STORAGE = "IN_STORAGE"
    IN_QUEUE = "IN_QUEUE"
    CARRIED_BY_WORKER = "CARRIED_BY_WORKER"
    LOADED_ON_MACHINE = "LOADED_ON_MACHINE"
    PROCESSING = "PROCESSING"
    WAITING_MACHINE_UNLOAD = "WAITING_MACHINE_UNLOAD"
    WAITING_INSPECTION = "WAITING_INSPECTION"
    INSPECTING = "INSPECTING"
    WAITING_INSPECTION_OUTPUT = "WAITING_INSPECTION_OUTPUT"
    WAITING_SCRAP_DISPOSAL = "WAITING_SCRAP_DISPOSAL"
    DROPPED = "DROPPED"
    COMPLETED = "COMPLETED"
    SCRAPPED = "SCRAPPED"


@dataclass
class Machine:
    machine_id: str
    station: int
    process_time_min: float
    state: MachineState = MachineState.WAIT_INPUT
    input_material: Optional[str] = None
    input_intermediate: Optional[str] = None
    output_intermediate: Optional[str] = None
    broken: bool = False
    failed_since: Optional[float] = None
    pm_until: float = 0.0
    last_pm_at: float = 0.0
    repair_owner: Optional[str] = None
    repair_team: list[str] = field(default_factory=list)
    repair_work_remaining_min: float = 0.0
    repair_last_progress_at: Optional[float] = None
    repair_done_event: Any = None
    repair_monitor_process: Any = None
    repair_monitor_token: int = 0
    setup_owner: Optional[str] = None
    unload_owner: Optional[str] = None
    pm_owner: Optional[str] = None
    active_process: Any = None
    total_processing_min: float = 0.0
    total_broken_min: float = 0.0
    total_pm_min: float = 0.0
    failures: int = 0
    pm_count: int = 0


@dataclass
class Worker:
    worker_id: str
    location: str = "Home"
    tile: tuple[int, int] | None = None
    reserved_tile: tuple[int, int] | None = None
    movement_path: list[tuple[int, int]] = field(default_factory=list)
    movement_target_tile: tuple[int, int] | None = None
    current_move_id: Optional[str] = None
    current_move_segment_index: int = 0
    current_move_segment_from_tile: tuple[int, int] | None = None
    current_move_segment_to_tile: tuple[int, int] | None = None
    current_move_logical_destination: Optional[str] = None
    current_move_started_at: Optional[float] = None
    discharged: bool = False
    discharged_since: Optional[float] = None
    current_task_id: Optional[str] = None
    current_task_type: Optional[str] = None
    current_task_code: Optional[str] = None
    current_task_instance_id: Optional[str] = None
    current_child_task_code: Optional[str] = None
    current_child_task_name: Optional[str] = None
    current_child_task_instance_id: Optional[str] = None
    current_task_path: Optional[str] = None
    current_task_depth: int = 0
    current_step_id: Optional[str] = None
    current_primitive_call_code: Optional[str] = None
    current_task_started_at: Optional[float] = None
    humanoid_state: dict[str, Any] = field(default_factory=default_humanoid_state_payload)
    process_ref: Any = None
    last_battery_swap: float = 0.0
    suspended_task: Any = None
    battery_service_owner: Optional[str] = None
    awaiting_battery_from: Optional[str] = None
    in_transit_from: Optional[str] = None
    in_transit_to: Optional[str] = None
    in_transit_progress: float = 0.0
    in_transit_total_min: float = 0.0
    carrying_item_id: Optional[str] = None
    carrying_item_type: Optional[str] = None
    carrying_item_ids: list[str] = field(default_factory=list)
    carrying_item_count: int = 0
    carrying_item_max_count: int = 1
    transport_session_id: Optional[str] = None
    shared_carry_role: Optional[str] = None
    battery_swap_critical: bool = False
    low_battery_alerted: bool = False
    total_task_time_min: dict[str, float] = field(default_factory=dict)
    current_commitment_id: Optional[str] = None
    claimed_commitments: list[str] = field(default_factory=list)
    incident_backlog: list[dict[str, Any]] = field(default_factory=list)
    local_response_attempts: dict[str, int] = field(default_factory=dict)
    pending_recovery_incident: Optional[dict[str, Any]] = None
    last_recovery_completed_task_id: Optional[str] = None
    last_recovery_completed_at: Optional[float] = None

    @property
    def agent_id(self) -> str:
        # Backward compatibility for legacy dashboards/decision code.
        return self.worker_id

    @agent_id.setter
    def agent_id(self, value: str) -> None:
        self.worker_id = value


# Deprecated compatibility alias. Manufacturing-domain code should use Worker.
Agent = Worker


@dataclass
class Task:
    task_id: str
    task_type: str
    priority_key: str
    priority: float
    location: str
    payload: dict[str, Any] = field(default_factory=dict)
    selection_meta: dict[str, Any] = field(default_factory=dict)
    task_code: str = ""
    instance_id: str = ""
    assigned_robot_id: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    task_spec_name: str = ""
    step_plan: list[dict[str, Any]] = field(default_factory=list)
    humanoid: dict[str, Any] = field(default_factory=dict)


@dataclass
class Item:
    item_id: str
    item_type: str
    created_at: float
    state: ItemState = ItemState.CREATED
    current_station: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
