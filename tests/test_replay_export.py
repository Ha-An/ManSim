from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "replay_studio" / "examples"))
from replay_studio.examples.export_mansim_run import (
    build_initial_state,
    build_humanoid_task_window_index,
    humanoid_task_window,
)


class ReplayExportTests(unittest.TestCase):
    def test_queue_entities_keep_item_type_for_replay_rendering(self) -> None:
        state = build_initial_state([], [], {"regions": [], "nodes": []}, battery_period_min=200.0, repair_total_min=30.0)
        entities = state["entities"]

        self.assertEqual("S2 Intermediate Queue", entities["intermediate_queue_2"]["label"])
        self.assertEqual("material", entities["material_queue_1"]["attributes"]["item_type"])
        self.assertEqual("intermediate", entities["intermediate_queue_2"]["attributes"]["item_type"])
        self.assertEqual("product", entities["intermediate_queue_4"]["attributes"]["item_type"])
        self.assertEqual("intermediate", entities["station_1_output_queue"]["attributes"]["item_type"])
        self.assertEqual("product", entities["station_2_output_queue"]["attributes"]["item_type"])
        self.assertEqual("scrap", entities["inspection_scrap_queue"]["attributes"]["item_type"])

    def test_child_task_end_restores_parent_task_window(self) -> None:
        raw_events = [
            {
                "t": 10.0,
                "type": "HUMANOID_TASK_START",
                "entity_id": "A1",
                "details": {"task_id": "PARENT-1", "instance_id": "PARENT-1:REPAIR_MACHINE", "task_code": "REPAIR_MACHINE"},
            },
            {
                "t": 11.0,
                "type": "HUMANOID_TASK_START",
                "entity_id": "A1",
                "details": {"task_id": "PARENT-1:s02", "instance_id": "PARENT-1:s02", "task_code": "INSPECT_MACHINE"},
            },
            {
                "t": 12.0,
                "type": "HUMANOID_TASK_END",
                "entity_id": "A1",
                "details": {"task_id": "PARENT-1:s02", "instance_id": "PARENT-1:s02", "task_code": "INSPECT_MACHINE"},
            },
            {
                "t": 20.0,
                "type": "HUMANOID_TASK_END",
                "entity_id": "A1",
                "details": {"task_id": "PARENT-1", "instance_id": "PARENT-1:REPAIR_MACHINE", "task_code": "REPAIR_MACHINE"},
            },
        ]
        index = build_humanoid_task_window_index(raw_events)

        window = humanoid_task_window(
            index,
            "A1",
            12.0,
            {"task_id": "PARENT-1", "instance_id": "PARENT-1:REPAIR_MACHINE", "task_code": "REPAIR_MACHINE"},
        )

        self.assertEqual(10.0, window["started_at"])
        self.assertEqual(20.0, window["ended_at"])
        self.assertEqual("REPAIR_MACHINE", window["task_code"])


if __name__ == "__main__":
    unittest.main()
