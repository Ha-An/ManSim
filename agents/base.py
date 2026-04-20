from __future__ import annotations

"""???? ?? ?? ?????? ?? ??? ??."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ?? ?? ??? LLM ??? ???? ???? ?? ???? ?.
TASK_PRIORITY_KEYS: tuple[str, ...] = (
    "battery_swap",
    "battery_delivery_low_battery",
    "battery_delivery_discharged",
    "repair_machine",
    "unload_machine",
    "setup_machine",
    "inter_station_transfer",
    "material_supply",
    "inspect_product",
    "preventive_maintenance",
)

FIXED_TASK_ASSIGNABLE_FAMILIES: tuple[str, ...] = (
    "repair_machine",
    "unload_machine",
    "setup_machine",
    "preventive_maintenance",
    "inspect_product",
    "inter_station_transfer",
    "material_supply",
)

FIXED_TASK_BATTERY_EXCEPTION_FAMILIES: tuple[str, ...] = (
    "battery_swap",
    "battery_delivery_low_battery",
    "battery_delivery_discharged",
)


def default_task_priority_weights() -> dict[str, float]:
    """??? ?? ???? ?? ??? 1.0? ???? ???? ????."""
    return {key: 1.0 for key in TASK_PRIORITY_KEYS}


def default_agent_priority_multipliers(agent_ids: list[str] | tuple[str, ...]) -> dict[str, dict[str, float]]:
    """?? baseline ?? ?? ????? ?? multiplier ?? ????."""
    return {str(agent_id): default_task_priority_weights() for agent_id in agent_ids}


@dataclass
class StrategyState:
    """?? ?? ? ?? ?? ??? ???? ?? ?? ??."""

    notes: list[str] = field(default_factory=list)
    summary: str = ""
    diagnosis: dict[str, list[str]] = field(default_factory=dict)
    orchestration_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkOrder:
    """???? ??? ?????? ??? ?? ?? ??."""

    order_id: str
    task_family: str
    priority: float = 1.0
    target_type: str = "none"
    target_id: str = ""
    target_station: int | None = None
    dependency_ids: list[str] = field(default_factory=list)
    parallel_group: str = ""
    handover_to: str = ""
    expires_at_day: int | None = None
    reason: str = ""


@dataclass
class HandoverMessage:
    """??? ???? ??? ?? ???? ???."""

    message_id: str
    from_agent: str
    to_agent: str
    message_type: str
    task_family: str = ""
    target_type: str = "none"
    target_id: str = ""
    target_station: int | None = None
    priority: int = 1
    body: str = ""


@dataclass
class PersonalQueue:
    """??? ? ??? ??? ?? ?? ?."""

    agent_id: str
    role: str = ""
    local_bias: dict[str, float] = field(default_factory=dict)
    work_orders: list[WorkOrder] = field(default_factory=list)


@dataclass
class AgentExperienceState:
    """LLM ?? ????? ???? ????? ?? ?? ??."""

    completed_counts: dict[str, int] = field(default_factory=dict)
    completed_minutes: dict[str, float] = field(default_factory=dict)
    interrupted_counts: dict[str, int] = field(default_factory=dict)
    skipped_counts: dict[str, int] = field(default_factory=dict)
    decision_source_counts: dict[str, int] = field(default_factory=dict)
    contribution_signals: dict[str, int] = field(default_factory=dict)
    recent_task_events: list[dict[str, Any]] = field(default_factory=list)
    current_priority_profile: dict[str, float] = field(default_factory=dict)


@dataclass
class JobPlan:
    """?? ??? ??? ?? ????? ??? ???."""

    task_priority_weights: dict[str, float]
    quotas: dict[str, int]
    rationale: str = ""
    agent_priority_multipliers: dict[str, dict[str, float]] = field(default_factory=dict)
    agent_roles: dict[str, str] = field(default_factory=dict)
    agent_task_allowlists: dict[str, list[str]] = field(default_factory=dict)
    personal_queues: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    commitments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    incident_work_orders: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    mailbox: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    parallel_groups: list[dict[str, Any]] = field(default_factory=list)
    reason_trace: list[dict[str, Any]] = field(default_factory=list)
    manager_summary: str = ""
    detector_alignment: str = "follow"
    plan_revision: int = 0
    incident_strategy: dict[str, Any] = field(default_factory=dict)
    incident_guidance: dict[str, Any] = field(default_factory=dict)

    def ensure_agent_priority_multipliers(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """누락된 에이전트 multiplier 행을 중립값 1.0으로 채운다."""
        for agent_id in agent_ids:
            agent_key = str(agent_id)
            if agent_key not in self.agent_priority_multipliers or not isinstance(self.agent_priority_multipliers.get(agent_key), dict):
                self.agent_priority_multipliers[agent_key] = default_task_priority_weights()
                continue
            row = self.agent_priority_multipliers[agent_key]
            for key in TASK_PRIORITY_KEYS:
                row.setdefault(key, 1.0)

    def effective_task_priority_weights(self, agent_id: str) -> dict[str, float]:
        """공용 baseline과 에이전트별 overlay를 곱해 실제 적용 우선순위를 계산한다."""
        effective: dict[str, float] = {}
        row = self.agent_priority_multipliers.get(str(agent_id), {})
        for key in TASK_PRIORITY_KEYS:
            effective[key] = round(float(self.task_priority_weights.get(key, 1.0)) * float(row.get(key, 1.0)), 6)
        return effective

    def ensure_personal_queues(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """?? ???? ?? ?? ? ??? ????? ????."""
        for agent_id in agent_ids:
            self.personal_queues.setdefault(str(agent_id), [])

    def ensure_commitments(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """?? ???? ?? commitment ?? ????? ????."""
        for agent_id in agent_ids:
            self.commitments.setdefault(str(agent_id), [])

    def ensure_incident_work_orders(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """누락된 incident work order 행을 빈 리스트로 채운다."""
        for agent_id in agent_ids:
            self.incident_work_orders.setdefault(str(agent_id), [])

    def ensure_agent_roles(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """?? ???? ?? ?? ??? ??? ????? ????."""
        for agent_id in agent_ids:
            self.agent_roles.setdefault(str(agent_id), "")

    def ensure_agent_task_allowlists(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """누락된 에이전트 task allowlist 행을 빈 리스트로 채운다."""
        for agent_id in agent_ids:
            self.agent_task_allowlists.setdefault(str(agent_id), [])

    def ensure_mailbox(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """?? ???? ?? ???? ??? ????? ????."""
        for agent_id in agent_ids:
            self.mailbox.setdefault(str(agent_id), [])

    def ensure_runtime_context(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """??? ?? ??? ??? ?? ??? ? ?? ????."""
        self.ensure_agent_priority_multipliers(agent_ids)
        self.ensure_agent_roles(agent_ids)
        self.ensure_agent_task_allowlists(agent_ids)
        self.ensure_personal_queues(agent_ids)
        self.ensure_commitments(agent_ids)
        self.ensure_incident_work_orders(agent_ids)
        self.ensure_mailbox(agent_ids)


class DecisionModule(ABC):
    """규칙 기반 모드와 LLM 모드가 공통으로 구현하는 인터페이스."""

    @abstractmethod
    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        """?? ??? ?? ?? ?? ?? ??? ???."""
        raise NotImplementedError

    @abstractmethod
    def propose_jobs(
        self,
        observation: dict[str, Any],
        strategy: StrategyState,
        norms: dict[str, Any],
    ) -> JobPlan:
        """?? ??? ?? ??? ???? ?? ???? ???."""
        raise NotImplementedError

    @abstractmethod
    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        """?? ?? ? ??? ??? ?? ?? ??? ??? ????."""
        raise NotImplementedError

    @abstractmethod
    def urgent_discuss(self, event: dict[str, Any], local_state: dict[str, Any]) -> dict[str, Any]:
        """?? ??? ?? ? ?? ??? ??? ?? ??? ????."""
        raise NotImplementedError
