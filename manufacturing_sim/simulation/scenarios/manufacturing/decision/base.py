from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StrategyState:
    bottleneck_station: int = 2
    notes: list[str] = field(default_factory=list)
    priority_bias: dict[str, float] = field(default_factory=dict)


@dataclass
class JobPlan:
    task_weights: dict[str, float]
    quotas: dict[str, int]
    rationale: str = ""


class DecisionModule(ABC):
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
