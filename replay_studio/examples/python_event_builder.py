from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _clean_ref_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_entity_refs(entity_refs: Dict[str, Any]) -> Dict[str, Any]:
    """Drop empty refs and coerce scalar ids to strings for schema-safe replay logs."""

    normalized: Dict[str, Any] = {}
    for key in ("primary", "source", "target"):
        value = _clean_ref_value(entity_refs.get(key))
        if value:
            normalized[key] = value

    related: List[str] = []
    raw_related = entity_refs.get("related", [])
    if isinstance(raw_related, list):
        for item in raw_related:
            value = _clean_ref_value(item)
            if value:
                related.append(value)
    if related:
        normalized["related"] = related
    return normalized


@dataclass
class SequenceAllocator:
    """Stable sequence index allocator.

    Simulators should allocate one deterministic integer per emitted event.
    This index is the secondary ordering key after timestamp.
    """

    next_value: int = 1

    def take(self) -> int:
        value = self.next_value
        self.next_value += 1
        return value


@dataclass
class ReplayEventBuilder:
    allocator: SequenceAllocator = field(default_factory=SequenceAllocator)

    def build(
        self,
        *,
        event_id: str,
        timestamp: float,
        event_type: str,
        entity_refs: Dict[str, Any],
        payload: Optional[Dict[str, Any]] = None,
        durative: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {
            "event_id": event_id,
            "sequence_index": self.allocator.take(),
            "timestamp": timestamp,
            "event_type": event_type,
            "entity_refs": normalize_entity_refs(entity_refs),
            "payload": payload or {},
            **({"durative": durative} if durative else {}),
        }


def build_message(
    builder: ReplayEventBuilder,
    *,
    event_id: str,
    timestamp: float,
    source: str,
    target: str,
    message: str,
    related: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return builder.build(
        event_id=event_id,
        timestamp=timestamp,
        event_type="message_sent",
        entity_refs={"source": source, "target": target, "related": related or []},
        payload={"message": message},
    )
