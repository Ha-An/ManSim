from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "replay_studio" / "examples"))
from replay_studio.examples.export_mansim_run import (
    build_initial_state,
    build_humanoid_task_window_index,
    convert_events,
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

    def test_dedicated_charging_docks_are_exported_and_track_charge_state(self) -> None:
        layout = {
            "scenario_type": "mfg_flow_shop",
            "regions": [],
            "nodes": [
                {
                    "entity_id": "charging_dock_A1",
                    "entity_type": "charger",
                    "position": {"x": 10.0, "y": 20.0},
                    "tile": {"x": 4, "y": 7},
                }
            ],
        }
        state = build_initial_state(["A1"], [], layout, battery_period_min=200.0, repair_total_min=30.0)
        entities = state["entities"]

        self.assertNotIn("battery_rack", entities)
        self.assertEqual("A1 Charging Dock", entities["charging_dock_A1"]["label"])
        self.assertEqual("A1", entities["charging_dock_A1"]["attributes"]["assigned_worker_id"])
        self.assertFalse(entities["charging_dock_A1"]["attributes"]["charging"])

        raw_events = [
            {
                "t": 15.0,
                "type": "BATTERY_CHARGE_STARTED",
                "entity_id": "A1",
                "location": "charging_dock_A1",
                "details": {
                    "task_id": "BAT-000001",
                    "charging_dock_id": "charging_dock_A1",
                    "start_soc": 0.25,
                    "target_soc": 1.0,
                    "charge_duration_min": 7.5,
                },
            },
            {
                "t": 22.5,
                "type": "BATTERY_CHARGE_COMPLETED",
                "entity_id": "A1",
                "location": "charging_dock_A1",
                "details": {
                    "task_id": "BAT-000001",
                    "charging_dock_id": "charging_dock_A1",
                    "target_soc": 1.0,
                    "charge_duration_min": 7.5,
                },
            },
        ]
        events = convert_events(raw_events, layout, battery_period_min=200.0, repair_total_min=30.0)
        dock_events = [
            event
            for event in events
            if event["event_type"] == "state_changed" and event["entity_refs"].get("primary") == "charging_dock_A1"
        ]

        self.assertEqual(["charging", "available"], [event["payload"]["state"] for event in dock_events])
        self.assertEqual("A1", dock_events[0]["payload"]["attributes"]["occupied_by"])
        self.assertEqual("", dock_events[1]["payload"]["attributes"]["occupied_by"])

    def test_output_buffer_queue_pop_uses_visible_queue_alias(self) -> None:
        raw_events = [
            {
                "t": 10.0,
                "type": "ITEM_MOVED",
                "entity_id": "INT-S1-1",
                "location": "Station1",
                "details": {"from": "S1M1", "to": "output_buffer_station_1", "item_type": "intermediate"},
            },
            {
                "t": 11.0,
                "type": "QUEUE_POP",
                "entity_id": "output_buffer_station_1",
                "location": "Station1",
                "details": {"item_id": "INT-S1-1", "queue": "output"},
            },
            {
                "t": 12.0,
                "type": "ITEM_MOVED",
                "entity_id": "INT-S1-1",
                "location": "Station2",
                "details": {"from": "output_buffer_station_1", "to": "intermediate_queue_2", "item_type": "intermediate"},
            },
        ]
        events = convert_events(raw_events, {"regions": [], "nodes": []}, battery_period_min=200.0, repair_total_min=30.0)
        output_queue_sizes = [
            event["payload"]["attributes"]["queue_size"]
            for event in events
            if event["event_type"] == "state_changed"
            and event["entity_refs"].get("primary") == "station_1_output_queue"
            and "queue_size" in event["payload"].get("attributes", {})
        ]

        self.assertEqual([1, 0], output_queue_sizes)


if __name__ == "__main__":
    unittest.main()
