from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class EventLogger:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.output_dir / "events.jsonl"
        self._events_fp = self.events_path.open("w", encoding="utf-8")
        self.events: list[dict[str, Any]] = []
        self.closed = False

    def log(
        self,
        *,
        t: float,
        day: int,
        event_type: str,
        entity_id: str = "",
        location: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        if self.closed:
            return
        event = {
            "t": round(float(t), 3),
            "day": int(day),
            "type": event_type,
            "entity_id": entity_id,
            "location": location,
            "details": details or {},
        }
        self.events.append(event)
        self._events_fp.write(json.dumps(event, ensure_ascii=True) + "\n")

    def write_json(self, filename: str, payload: dict[str, Any]) -> None:
        path = self.output_dir / filename
        with path.open("w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=2, ensure_ascii=True)

    def close(self) -> None:
        if self.closed:
            return
        self._events_fp.flush()
        self._events_fp.close()
        self.closed = True
