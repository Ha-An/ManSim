"""Small, atomic progress records, independent of the learning algorithm."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        # A browser/virus scanner can briefly hold a Windows file handle.
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(.02)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


class TrainingProgress:
    def __init__(self, output: Path):
        self.path = output / "training_progress.json"

    def update(self, **values: Any) -> None:
        previous = read_json(self.path)
        now = datetime.now(timezone.utc).isoformat()
        atomic_json(self.path, {"started_at": now, **previous, **values,
                               "updated_at": now, "pid": os.getpid()})

    def rollout(self, event: dict[str, Any]) -> None:
        self.update(status="running", **event)
