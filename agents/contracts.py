from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class OpportunityTarget:
    target_type: str = "none"
    target_id: str = ""
    target_station: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Opportunity:
    opportunity_id: str
    task_family: str
    worker_id: str = ""
    priority_key: str = ""
    location: str = ""
    target: OpportunityTarget = field(default_factory=OpportunityTarget)
    payload: dict[str, Any] = field(default_factory=dict)
    preconditions: list[str] = field(default_factory=list)
    expected_output_impact: float = 0.0
    blocking_effect: str = ""
    shareable: bool = False
    capacity: int = 1
    owners: list[str] = field(default_factory=list)
    why_available: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target"] = self.target.to_dict()
        return data


@dataclass
class Commitment:
    commitment_id: str
    opportunity_id: str
    task_family: str
    assigned_worker: str
    target: OpportunityTarget = field(default_factory=OpportunityTarget)
    alternate_workers: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    expiry_min: float | None = None
    handoff_policy: str = "allowed"
    success_criteria: list[str] = field(default_factory=list)
    rationale: str = ""
    source: str = "manager"
    status: str = "planned"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["target"] = self.target.to_dict()
        return data


@dataclass
class Claim:
    commitment_id: str
    worker_id: str
    status: str = "claimed"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Handoff:
    commitment_id: str
    from_worker: str
    to_worker: str
    reason: str = ""
    status: str = "requested"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncidentEvent:
    incident_id: str
    incident_class: str
    time_min: float
    day: int
    affected_entities: list[str] = field(default_factory=list)
    blocked_commitments: list[str] = field(default_factory=list)
    escalation_level: str = "worker_local"
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IncidentBlocker:
    blocker_id: str
    agent_id: str
    blocker_type: str
    candidate_signature_hash: str
    active_plan_revision: int
    created_at_min: float
    incident_id: str = ""
    source_incident_id: str = ""
    escalation_emitted: bool = False
    last_seen_min: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
