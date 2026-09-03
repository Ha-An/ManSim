from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.audit_run_artifacts import (
    Audit,
    check_inspection_task_lifecycle,
    check_item_transport_continuity,
    check_machine_breakdown_contract,
    check_spatial_continuity,
)


class SpatialAuditTests(unittest.TestCase):
    def _run_dir(self, root: Path) -> Path:
        layout = {
            "viewport": {"width": 100, "height": 100},
            "grid": {
                "width_tiles": 10,
                "height_tiles": 10,
                "tile_time_min": 0.1,
                "walls": [{"x": 5, "y": 5}],
                "doors": [],
                "object_footprints": [
                    {"x": 7, "y": 7, "width": 1, "height": 1, "blocking": True}
                ],
            },
        }
        (root / "replay_studio_layout.json").write_text(json.dumps(layout), encoding="utf-8")
        return root

    @staticmethod
    def _valid_events() -> list[dict[str, object]]:
        return [
            {
                "t": 0.0,
                "type": "AGENT_MOVE_START",
                "entity_id": "A1",
                "details": {
                    "move_id": "A1-move-1",
                    "from_tile": {"x": 1, "y": 1},
                    "to_tile": {"x": 2, "y": 2},
                    "path_tiles": [
                        {"x": 1, "y": 1},
                        {"x": 2, "y": 1},
                        {"x": 2, "y": 2},
                    ],
                    "duration": 0.2,
                    "effective_time_multiplier": 1.0,
                },
            },
            {
                "t": 0.0,
                "type": "AGENT_MOVE_TILE_START",
                "entity_id": "A1",
                "details": {
                    "move_id": "A1-move-1",
                    "segment_index": 1,
                    "from_tile": {"x": 1, "y": 1},
                    "to_tile": {"x": 2, "y": 1},
                },
            },
            {
                "t": 0.1,
                "type": "AGENT_MOVE_TILE_END",
                "entity_id": "A1",
                "details": {
                    "move_id": "A1-move-1",
                    "segment_index": 1,
                    "from_tile": {"x": 1, "y": 1},
                    "to_tile": {"x": 2, "y": 1},
                },
            },
            {
                "t": 0.1,
                "type": "AGENT_MOVE_TILE_START",
                "entity_id": "A1",
                "details": {
                    "move_id": "A1-move-1",
                    "segment_index": 2,
                    "from_tile": {"x": 2, "y": 1},
                    "to_tile": {"x": 2, "y": 2},
                },
            },
            {
                "t": 0.2,
                "type": "AGENT_MOVE_TILE_END",
                "entity_id": "A1",
                "details": {
                    "move_id": "A1-move-1",
                    "segment_index": 2,
                    "from_tile": {"x": 2, "y": 1},
                    "to_tile": {"x": 2, "y": 2},
                },
            },
            {
                "t": 0.2,
                "type": "AGENT_MOVE_END",
                "entity_id": "A1",
                "details": {"move_id": "A1-move-1", "to_tile": {"x": 2, "y": 2}},
            },
        ]

    def test_breakdown_near_horizon_does_not_require_unobservable_candidate(self) -> None:
        events = [
            {"t": 0.0, "type": "ROLLING_HORIZON_WINDOW_START"},
            {"t": 238.7, "type": "MACHINE_BROKEN", "entity_id": "S2M1"},
            {"t": 240.0, "type": "SIMULATION_END"},
        ]
        audit = Audit()

        check_machine_breakdown_contract(
            events,
            audit,
            require_immediate_repair_candidate=False,
            repair_candidate_deadline_min=5.1,
        )

        self.assertEqual(audit.errors, [])
        self.assertTrue(any("beyond the run horizon" in note for note in audit.notes))

    def test_breakdown_with_full_observation_window_requires_candidate(self) -> None:
        events = [
            {"t": 0.0, "type": "ROLLING_HORIZON_WINDOW_START"},
            {"t": 10.0, "type": "MACHINE_BROKEN", "entity_id": "S1M1"},
            {"t": 20.0, "type": "SIMULATION_END"},
        ]
        audit = Audit()

        check_machine_breakdown_contract(
            events,
            audit,
            require_immediate_repair_candidate=False,
            repair_candidate_deadline_min=5.1,
        )

        self.assertTrue(any("missing repair candidate" in error for error in audit.errors))

    def test_accepts_contiguous_passable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(Path(tmp))
            audit = Audit()
            check_spatial_continuity(run_dir, self._valid_events(), audit)
            self.assertEqual([], audit.errors)

    def test_accepts_sampled_per_tile_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(Path(tmp))
            events = self._valid_events()
            details = events[0]["details"]
            assert isinstance(details, dict)
            details["sampled_tile_time_min"] = 0.117
            details["effective_time_multiplier"] = 2.0
            details["duration"] = 0.234
            audit = Audit()
            check_spatial_continuity(run_dir, events, audit)
            self.assertEqual([], audit.errors)

    def test_accepts_cancelled_in_flight_tile_without_claiming_arrival(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(Path(tmp))
            events = self._valid_events()[:2]
            events.extend(
                [
                    {
                        "t": 0.05,
                        "type": "AGENT_MOVE_TILE_CANCELLED",
                        "entity_id": "A1",
                        "details": {
                            "move_id": "A1-move-1",
                            "segment_index": 1,
                            "from_tile": {"x": 1, "y": 1},
                            "to_tile": {"x": 2, "y": 1},
                            "committed_tile": {"x": 1, "y": 1},
                        },
                    },
                    {
                        "t": 0.05,
                        "type": "AGENT_MOVE_END",
                        "entity_id": "A1",
                        "details": {"move_id": "A1-move-1", "to_tile": {"x": 1, "y": 1}},
                    },
                ]
            )
            audit = Audit()
            check_spatial_continuity(run_dir, events, audit)
            self.assertEqual([], audit.errors)

    def test_rejects_teleport_and_blocking_tile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(Path(tmp))
            events = self._valid_events()[:1]
            details = events[0]["details"]
            assert isinstance(details, dict)
            details["to_tile"] = {"x": 5, "y": 5}
            details["path_tiles"] = [{"x": 1, "y": 1}, {"x": 5, "y": 5}]
            details["duration"] = 0.1
            audit = Audit()
            check_spatial_continuity(run_dir, events, audit)
            self.assertTrue(any("spatial continuity violations" in error for error in audit.errors))
            joined = " ".join(audit.errors)
            self.assertIn("non-adjacent", joined)
            self.assertIn("blocking tile", joined)

    def test_item_transport_requires_carry_drop_and_destination_state(self) -> None:
        valid = [
            {
                "t": 1.0,
                "type": "WORKER_CARGO_CHANGED",
                "entity_id": "A1",
                "details": {"cargo": {"item_ids": ["ITEM-1"]}},
            },
            {
                "t": 1.0,
                "type": "ITEM_STATE_CHANGED",
                "entity_id": "ITEM-1",
                "details": {"item_state": "CARRIED_BY_WORKER", "ref": "A1"},
            },
            {"t": 1.0, "type": "AGENT_PICK_ITEM", "entity_id": "A1", "details": {"item_id": "ITEM-1"}},
            {
                "t": 2.0,
                "type": "ITEM_STATE_CHANGED",
                "entity_id": "ITEM-1",
                "details": {"item_state": "IN_OUTPUT_BUFFER", "ref": "output_buffer_station_1"},
            },
            {"t": 2.1, "type": "AGENT_DROP_ITEM", "entity_id": "A1", "details": {"item_id": "ITEM-1"}},
            {"t": 2.1, "type": "ITEM_MOVED", "entity_id": "ITEM-1", "details": {"to": "output_buffer_station_1"}},
        ]
        audit = Audit()
        check_item_transport_continuity(valid, audit)
        self.assertEqual([], audit.errors)

        invalid = [event for event in valid if event.get("type") != "ITEM_STATE_CHANGED" or event.get("t") != 2.0]
        audit = Audit()
        check_item_transport_continuity(invalid, audit)
        self.assertTrue(any("item transport continuity violations" in error for error in audit.errors))

    def test_inspection_lifecycle_allows_prefixes_and_rejects_invalid_unload(self) -> None:
        valid = [
            {
                "t": 1.0,
                "type": "INSPECTION_DESK_ITEM_LOADED",
                "entity_id": "PRODUCT-1",
                "details": {"product_id": "PRODUCT-1"},
            },
            {
                "t": 2.0,
                "type": "INSPECTION_STARTED",
                "entity_id": "PRODUCT-1",
                "details": {"product_id": "PRODUCT-1"},
            },
            {
                "t": 3.0,
                "type": "INSPECTION_RESULT_RECORDED",
                "entity_id": "PRODUCT-1",
                "details": {"product_id": "PRODUCT-1", "inspection_result": "PASS"},
            },
            {
                "t": 4.0,
                "type": "INSPECTION_DESK_ITEM_UNLOADED",
                "entity_id": "PRODUCT-1",
                "details": {
                    "product_id": "PRODUCT-1",
                    "inspection_result": "PASS",
                    "destination": "inspection_output_queue",
                },
            },
            {
                "t": 5.0,
                "type": "INSPECTION_DESK_ITEM_LOADED",
                "entity_id": "PRODUCT-2",
                "details": {"product_id": "PRODUCT-2"},
            },
        ]
        audit = Audit()
        check_inspection_task_lifecycle(valid, audit)
        self.assertEqual([], audit.errors)

        invalid = valid + [
            {
                "t": 6.0,
                "type": "INSPECTION_DESK_ITEM_UNLOADED",
                "entity_id": "PRODUCT-2",
                "details": {
                    "product_id": "PRODUCT-2",
                    "inspection_result": "FAIL",
                    "destination": "inspection_output_queue",
                },
            }
        ]
        audit = Audit()
        check_inspection_task_lifecycle(invalid, audit)
        self.assertTrue(any("inspection lifecycle violations" in error for error in audit.errors))


if __name__ == "__main__":
    unittest.main()
