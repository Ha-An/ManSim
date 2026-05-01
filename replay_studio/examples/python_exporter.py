from __future__ import annotations

import json
from pathlib import Path

from python_event_builder import ReplayEventBuilder, build_message


def build_demo_log() -> dict:
    builder = ReplayEventBuilder()

    return {
        "schema_version": "1.0",
        "metadata": {
            "run_id": "python-export-demo",
            "domain": "manufacturing",
            "total_duration": 12,
            "time_unit": "minutes",
            "title": "Python Export Example",
        },
        "initial_state": {
            "timestamp": 0,
            "entities": {
                "robot_1": {
                    "entity_id": "robot_1",
                    "entity_type": "robot",
                    "state": "idle",
                    "label": "Robot 1",
                    "position": {"x": 300, "y": 520},
                    "attributes": {"battery_pct": 100},
                    "relations": {},
                    "updated_at": 0,
                },
                "machine_1": {
                    "entity_id": "machine_1",
                    "entity_type": "machine",
                    "state": "idle",
                    "label": "Machine 1",
                    "position": {"x": 520, "y": 220},
                    "attributes": {},
                    "relations": {},
                    "updated_at": 0,
                },
                "charger_1": {
                    "entity_id": "charger_1",
                    "entity_type": "charger",
                    "state": "idle",
                    "label": "Charger 1",
                    "position": {"x": 960, "y": 520},
                    "attributes": {},
                    "relations": {},
                    "updated_at": 0,
                },
            },
            "resources": {},
            "queues": {},
        },
        "events": [
            builder.build(
                event_id="py-001",
                timestamp=1.0,
                event_type="task_assigned",
                entity_refs={"source": "scheduler", "target": "robot_1", "related": ["machine_1"]},
                payload={"task_id": "load-machine", "task_label": "Load Machine 1"},
            ),
            builder.build(
                event_id="py-002",
                timestamp=1.1,
                event_type="entity_moved",
                entity_refs={"primary": "robot_1", "source": "storage", "target": "machine_1"},
                durative={"started_at": 1.1, "ended_at": 4.0, "expected_duration": 2.9},
                payload={
                    "from": {"x": 300, "y": 520},
                    "to": {"x": 520, "y": 220},
                },
            ),
            build_message(
                builder,
                event_id="py-003",
                timestamp=4.1,
                source="robot_1",
                target="machine_1",
                message="Material delivered",
            ),
            builder.build(
                event_id="py-004",
                timestamp=8.0,
                event_type="battery_low",
                entity_refs={"primary": "robot_1"},
                payload={"battery_pct": 16, "label": "Battery below safe threshold"},
            ),
            builder.build(
                event_id="py-005",
                timestamp=8.5,
                event_type="charging_started",
                entity_refs={"primary": "robot_1", "target": "charger_1"},
                durative={"started_at": 8.5, "ended_at": 11.0, "expected_duration": 2.5},
                payload={"battery_pct": 16},
            ),
        ],
    }


if __name__ == "__main__":
    output_path = Path(__file__).with_name("python_export_demo.json")
    output_path.write_text(json.dumps(build_demo_log(), indent=2), encoding="utf-8")
    print(f"Wrote {output_path}")
