from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from dashboards.gantt import export_gantt
from experiments.mfg_flow_shop_paper.audit_inventory_results import gantt_state_checks


def _state_event(
    t: float,
    worker_id: str,
    availability: str,
    *,
    task_code: str = "",
    primitive: str = "",
    power: str = "POWER_NORMAL",
) -> dict:
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
                "power": power,
                "manipulation": "FREE",
                "task_context": task_context,
                "reason": None,
                "metadata": {"source": "test"},
            }
        },
    }


class GanttExportTests(unittest.TestCase):
    def test_charging_overlay_preserves_blocked_availability(self) -> None:
        events = [
            _state_event(0, "A1", "EXECUTING", power="CHARGING"),
            _state_event(2, "A1", "BLOCKED", power="CHARGING"),
            _state_event(3, "A1", "EXECUTING", power="CHARGING"),
            _state_event(5, "A1", "AVAILABLE"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            export_gantt(events, path)
            with (path / "gantt_segments.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            content = (path / "gantt.html").read_text(encoding="utf-8")
        self.assertEqual(["CHARGING"] * 3, [row["status"] for row in rows])
        self.assertEqual(["EXECUTING", "BLOCKED", "EXECUTING"], [row["availability"] for row in rows])
        self.assertTrue(all(row["charging"] == "True" for row in rows))
        self.assertIn("Availability=BLOCKED", content)
        axes = {"availability": {"EXECUTING": 4, "BLOCKED": 1}, "power": {"CHARGING": 5}}
        for code, actual, expected in gantt_state_checks(rows, axes):
            self.assertAlmostEqual(actual, expected, msg=code)
        legacy = [{k: v for k, v in row.items() if k not in ("availability", "charging")} for row in rows]
        for code, actual, expected in gantt_state_checks(legacy, axes):
            self.assertAlmostEqual(actual, expected, msg=code)
        bad_axes = {"availability": {"EXECUTING": 5}, "power": {"CHARGING": 5}}
        self.assertTrue(any(abs(a-b) > 0.01 for _, a, b in gantt_state_checks(rows, bad_axes)))

    def test_open_machine_downtime_is_retained_at_horizon(self) -> None:
        events = [
            {"t": 2, "type": "MACHINE_PM_START", "entity_id": "S1M1", "details": {}},
            {"t": 3, "type": "MACHINE_BROKEN", "entity_id": "S2M1", "details": {}},
            {"t": 5, "type": "RUN_END", "entity_id": "system", "details": {}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            export_gantt(events, Path(directory))
            with (Path(directory) / "gantt_segments.csv").open() as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual({("S1M1", 2.0, 5.0), ("S2M1", 3.0, 5.0)},
                         {(r["lane"], float(r["start"]), float(r["end"])) for r in rows})

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

    def test_charging_power_state_gets_a_distinct_worker_segment(self) -> None:
        events = [
            _state_event(0.0, "A1", "EXECUTING", task_code="MANAGE_ROBOT_POWER"),
            _state_event(
                2.0,
                "A1",
                "EXECUTING",
                task_code="MANAGE_ROBOT_POWER",
                primitive="EXECUTE_SYSTEM_ACTION",
                power="CHARGING",
            ),
            _state_event(7.0, "A1", "EXECUTING", task_code="MANAGE_ROBOT_POWER"),
            _state_event(8.0, "A1", "AVAILABLE"),
        ]
        with tempfile.TemporaryDirectory() as raw_dir:
            output_dir = Path(raw_dir)
            export_gantt(events=events, output_dir=output_dir)
            with (output_dir / "gantt_segments.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        worker_rows = [row for row in rows if row["entity_group"] == "Worker" and row["lane"] == "A1"]
        charging_rows = [row for row in worker_rows if row["status"] == "CHARGING"]
        self.assertEqual(1, len(charging_rows))
        self.assertAlmostEqual(2.0, float(charging_rows[0]["start"]))
        self.assertAlmostEqual(7.0, float(charging_rows[0]["end"]))

    def test_machine_processing_includes_resume_and_open_horizon_segments(self) -> None:
        def machine_event(t: float, event_type: str, cycle_id: str) -> dict:
            return {
                "t": t,
                "day": 1,
                "type": event_type,
                "entity_id": "S1M1",
                "location": "Station1",
                "details": {"cycle_id": cycle_id},
            }

        events = [
            machine_event(1.0, "MACHINE_START", "C1"),
            machine_event(3.0, "MACHINE_ABORTED", "C1"),
            machine_event(5.0, "MACHINE_RESUME", "C1"),
            machine_event(8.0, "MACHINE_END", "C1"),
            machine_event(9.0, "MACHINE_START", "C2"),
            {"t": 12.0, "day": 1, "type": "RUN_END", "entity_id": "system", "details": {}},
        ]
        with tempfile.TemporaryDirectory() as raw_dir:
            output_dir = Path(raw_dir)
            export_gantt(events=events, output_dir=output_dir)
            with (output_dir / "gantt_segments.csv").open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        machine_rows = [row for row in rows if row["interval_type"] == "MACHINE_PROCESSING"]
        intervals = [(float(row["start"]), float(row["end"])) for row in machine_rows]
        self.assertEqual([(1.0, 3.0), (5.0, 8.0), (9.0, 12.0)], intervals)


if __name__ == "__main__":
    unittest.main()
