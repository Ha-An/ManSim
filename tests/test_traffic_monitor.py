from __future__ import annotations

import unittest

from manufacturing_sim.simulation.scenarios.manufacturing.traffic import (
    TrafficMonitor,
    TrafficPlan,
    TrafficSegment,
)


class TrafficMonitorTests(unittest.TestCase):
    def test_tile_conflict_is_collision(self) -> None:
        monitor = TrafficMonitor(near_miss_headway_min=0.05)
        monitor.begin_segment(
            TrafficSegment(
                move_id="A1-move-1",
                worker_id="A1",
                segment_index=1,
                from_tile=(1, 1),
                to_tile=(2, 1),
                started_at=0.0,
                ended_at=0.1,
            )
        )

        conflicts = monitor.begin_segment(
            TrafficSegment(
                move_id="A2-move-1",
                worker_id="A2",
                segment_index=1,
                from_tile=(2, 2),
                to_tile=(2, 1),
                started_at=0.02,
                ended_at=0.12,
            )
        )

        self.assertEqual(["TILE_CONFLICT"], [conflict.conflict_type for conflict in conflicts])
        self.assertTrue(conflicts[0].collision)
        self.assertEqual("error", conflicts[0].severity)

    def test_reverse_edge_conflict_is_collision(self) -> None:
        monitor = TrafficMonitor(near_miss_headway_min=0.05)
        monitor.begin_segment(
            TrafficSegment(
                move_id="A1-move-1",
                worker_id="A1",
                segment_index=1,
                from_tile=(1, 1),
                to_tile=(2, 1),
                started_at=0.0,
                ended_at=0.1,
            )
        )

        conflicts = monitor.begin_segment(
            TrafficSegment(
                move_id="A2-move-1",
                worker_id="A2",
                segment_index=1,
                from_tile=(2, 1),
                to_tile=(1, 1),
                started_at=0.02,
                ended_at=0.12,
            )
        )

        self.assertEqual(["EDGE_CONFLICT"], [conflict.conflict_type for conflict in conflicts])
        self.assertTrue(conflicts[0].collision)

    def test_time_shifted_same_path_is_path_overlap_not_collision(self) -> None:
        monitor = TrafficMonitor(near_miss_headway_min=0.05)
        first = TrafficPlan(
            move_id="A1-move-1",
            worker_id="A1",
            path_tiles=((1, 1), (2, 1), (3, 1)),
            started_at=0.0,
            ended_at=0.2,
        )
        second = TrafficPlan(
            move_id="A2-move-1",
            worker_id="A2",
            path_tiles=((3, 1), (2, 1), (1, 1)),
            started_at=1.0,
            ended_at=1.2,
        )

        self.assertEqual([], monitor.register_plan(first))
        conflicts = monitor.register_plan(second)

        self.assertEqual(["PATH_OVERLAP"], [conflict.conflict_type for conflict in conflicts])
        self.assertFalse(conflicts[0].collision)

    def test_near_miss_uses_headway_after_segment_end(self) -> None:
        monitor = TrafficMonitor(near_miss_headway_min=0.05)
        first = TrafficSegment(
            move_id="A1-move-1",
            worker_id="A1",
            segment_index=1,
            from_tile=(1, 1),
            to_tile=(2, 1),
            started_at=0.0,
            ended_at=0.1,
        )
        monitor.begin_segment(first)
        monitor.end_segment("A1", "A1-move-1", 1, ended_at=0.1)

        conflicts = monitor.begin_segment(
            TrafficSegment(
                move_id="A2-move-1",
                worker_id="A2",
                segment_index=1,
                from_tile=(1, 1),
                to_tile=(2, 1),
                started_at=0.13,
                ended_at=0.23,
            )
        )

        self.assertEqual(["NEAR_MISS"], [conflict.conflict_type for conflict in conflicts])
        self.assertFalse(conflicts[0].collision)


if __name__ == "__main__":
    unittest.main()
