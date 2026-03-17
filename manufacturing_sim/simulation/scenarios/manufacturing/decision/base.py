from __future__ import annotations

"""Core decision-mode interfaces and shared planner data structures."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# Direct task-priority keys used across rule-based and LLM modes.
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


def default_task_priority_weights() -> dict[str, float]:
    """Return neutral task-priority weights for all known task families."""
    return {key: 1.0 for key in TASK_PRIORITY_KEYS}


def default_agent_priority_multipliers(agent_ids: list[str] | tuple[str, ...]) -> dict[str, dict[str, float]]:
    """Return neutral per-agent overlays over the shared task-priority baseline."""
    return {str(agent_id): default_task_priority_weights() for agent_id in agent_ids}


@dataclass
class StrategyState:
    """Day-level diagnosis returned by reflect()."""

    notes: list[str] = field(default_factory=list)
    summary: str = ""
    diagnosis: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class JobPlan:
    """Task-priority weights and quotas proposed for the current day."""

    task_priority_weights: dict[str, float]
    quotas: dict[str, int]
    rationale: str = ""
    agent_priority_multipliers: dict[str, dict[str, float]] = field(default_factory=dict)

    def ensure_agent_priority_multipliers(self, agent_ids: list[str] | tuple[str, ...]) -> None:
        """Populate missing agent multiplier rows with neutral overlays."""
        for agent_id in agent_ids:
            agent_key = str(agent_id)
            if agent_key not in self.agent_priority_multipliers or not isinstance(self.agent_priority_multipliers.get(agent_key), dict):
                self.agent_priority_multipliers[agent_key] = default_task_priority_weights()
                continue
            row = self.agent_priority_multipliers[agent_key]
            for key in TASK_PRIORITY_KEYS:
                row.setdefault(key, 1.0)

    def effective_task_priority_weights(self, agent_id: str) -> dict[str, float]:
        """Return shared baseline multiplied by the agent-specific overlay."""
        effective: dict[str, float] = {}
        row = self.agent_priority_multipliers.get(str(agent_id), {})
        for key in TASK_PRIORITY_KEYS:
            effective[key] = round(float(self.task_priority_weights.get(key, 1.0)) * float(row.get(key, 1.0)), 6)
        return effective


@dataclass
class AgentExperienceState:
    """Rolling per-agent experience summary used only by LLM decision modes."""

    completed_counts: dict[str, int] = field(default_factory=dict)
    completed_minutes: dict[str, float] = field(default_factory=dict)
    interrupted_counts: dict[str, int] = field(default_factory=dict)
    skipped_counts: dict[str, int] = field(default_factory=dict)
    decision_source_counts: dict[str, int] = field(default_factory=dict)
    contribution_signals: dict[str, int] = field(default_factory=dict)
    recent_task_events: list[dict[str, Any]] = field(default_factory=list)
    current_priority_profile: dict[str, float] = field(default_factory=dict)


class DecisionModule(ABC):
    """Common interface implemented by rule-based and LLM decision modes."""

    @abstractmethod
    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        raise NotImplementedError

    @abstractmethod
    def propose_jobs(
        self,
        observation: dict[str, Any],
        strategy: StrategyState,
        norms: dict[str, Any],
    ) -> JobPlan:
        raise NotImplementedError

    @abstractmethod
    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def urgent_discuss(self, event: dict[str, Any], local_state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
