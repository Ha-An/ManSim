from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class MachineState(str, Enum):
    IDLE = "IDLE"
    WAIT_INPUT = "WAIT_INPUT"
    SETUP = "SETUP"
    PROCESSING = "PROCESSING"
    DONE_WAIT_UNLOAD = "DONE_WAIT_UNLOAD"
    BROKEN = "BROKEN"
    UNDER_REPAIR = "UNDER_REPAIR"
    UNDER_PM = "UNDER_PM"


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
class Agent:
    agent_id: str
    location: str = "Home"
    discharged: bool = False
    discharged_since: Optional[float] = None
    current_task_id: Optional[str] = None
    current_task_type: Optional[str] = None
    current_task_started_at: Optional[float] = None
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
    battery_swap_critical: bool = False
    total_task_time_min: dict[str, float] = field(default_factory=dict)


@dataclass
class Task:
    task_id: str
    task_type: str
    priority_key: str
    priority: float
    location: str
    payload: dict[str, Any] = field(default_factory=dict)
    selection_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Item:
    item_id: str
    item_type: str
    created_at: float
    current_station: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
