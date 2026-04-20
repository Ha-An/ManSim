from __future__ import annotations

ENTITY_TYPES: tuple[str, ...] = (
    "Run",
    "Day",
    "Issue",
    "Incident",
    "Machine",
    "Station",
    "Worker",
    "Opportunity",
    "Commitment",
    "Intervention",
    "Outcome",
    "Lesson",
    "StrategyPattern",
)

RELATION_TYPES: tuple[str, ...] = (
    "observed_in",
    "affects",
    "blocks",
    "caused_by",
    "mitigated_by",
    "assigned_to",
    "handoff_to",
    "improved",
    "worsened",
    "recurred_after",
    "alias_of",
    "recommended_for",
    "supersedes",
)


def empty_graph_payload() -> dict[str, object]:
    return {
        "meta": {
            "version": "vNext",
            "entity_types": list(ENTITY_TYPES),
            "relation_types": list(RELATION_TYPES),
        },
        "nodes": {},
        "edges": [],
    }
