from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from dashboards.gantt import export_gantt


def _state_event(t: float, worker_id: str, availability: str, *, task_code: str = "", primitive: str = "") -> dict:
    task_context = None
    if task_code or primitive:
        task_context = {
            "task_code": task_code or None,
            "task_instance_id": f"{worker_id}:{task_code}" if task_code else None,
            "step_id": "s01" if primitive else None,
            "primitive_call_code": primitive or None,
            "execution_status": "RUNNING" if primitive else "PENDING",
        }
    return {
        "t": t,
        "day": 1,
        "type": "WORKER_STATE_CHANGED",
        "entity_id": worker_id,
        "location": "Warehouse",
        "details": {
            "humanoid_state": {
                "humanoid_id": worker_id,
                "availability": availability,
                "mobility": "NAVIGATING" if primitive == "NAVIGATE_TO" else "STATIONARY",
                "power": "POWER_NORMAL",
                "manipulation": "FREE",
                "task_context": task_context,
                "reason": None,
                "metadata": {"source": "test"},
            }
        },
    }


class GanttExportTests(unittest.TestCase):
    def test_worker_rows_use_humanoidsim_availability_axis(self) -> None:
        events = [
            _state_event(0.0, "A1", "ASSIGNED", task_code="TRANSFER"),
            _state_event(1.0, "A1", "EXECUTING", task_code="TRANSFER", primitive="NAVIGATE_TO"),
            _state_event(2.5, "A1", "WAITING", task_code="TRANSFER"),
            _state_event(3.0, "A1", "AVAILABLE"),
            _state_event(1.0, "PRODUCT-1", "EXECUTING", task_code="TRANSFER"),
            {
                "t": 4.0,
                "day": 1,
                "type": "MACHINE_END",
                "entity_id": "S1M1",
                "location": "Station 1",
                "details": {"cycle_id": "C1"},
            },
        ]
        with tempfile.TemporaryDirectory() as raw_dir:
            output_dir = Path(raw_dir)
            export_gantt(events=events, output_dir=output_dir)
            with (output_dir / "gantt_segments.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        worker_rows = [row for row in rows if row["entity_group"] == "Worker" and row["lane"] == "A1"]
        self.assertEqual(["ASSIGNED", "EXECUTING", "WAITING", "AVAILABLE"], [row["status"] for row in worker_rows])
        self.assertFalse(any(row["lane"] == "PRODUCT-1" for row in rows))
        self.assertNotIn("WORKING", {row["status"] for row in worker_rows})
        self.assertNotIn("MOVING", {row["status"] for row in worker_rows})


if __name__ == "__main__":
    unittest.main()
