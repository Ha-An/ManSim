from __future__ import annotations

import unittest

from manufacturing_sim.simulation.pre_run_diagnostics import (
    FACTORY_PRE_RUN_METRIC_CODES,
    build_factory_pre_run_diagnostics,
)


class _FakeGridMap:
    width_tiles = 8
    height_tiles = 6
    tile_time_min = 0.1

    def __init__(self) -> None:
        self.blocked = {(3, 2), (3, 3)}

    def is_passable_static(self, tile: tuple[int, int]) -> bool:
        x, y = tile
        return 0 <= x < self.width_tiles and 0 <= y < self.height_tiles and tile not in self.blocked

    def neighbors(self, tile: tuple[int, int]) -> list[tuple[int, int]]:
        x, y = tile
        candidates = [(x, y - 1), (x - 1, y), (x + 1, y), (x, y + 1)]
        return [candidate for candidate in candidates if self.is_passable_static(candidate)]

    def destination_tiles(
        self,
        location: str,
        *,
        worker_id: str,
        from_tile: tuple[int, int] | None = None,
        ignore_dynamic: bool = False,
    ) -> list[tuple[int, int]]:
        del worker_id, from_tile, ignore_dynamic
        if str(location).startswith("S"):
            return [(1, 1), (1, 2)]
        return [(0, 0)]


class _FakeWorld:
    def __init__(self) -> None:
        self.workers = {"A1": object(), "A2": object(), "A3": object()}
        self.machines = {"S1M1": object(), "S1M2": object(), "S2M1": object(), "S2M2": object()}
        self.machines_by_station = {1: ["S1M1", "S1M2"], 2: ["S2M1", "S2M2"]}
        self.grid_map = _FakeGridMap()
        self.num_days = 5
        self.minutes_per_day = 240
        self.battery_swap_period_min = 200.0
        self.battery_available_rate_multiplier = 0.5
        self.battery_non_available_rate_multiplier = 1.0


class PreRunDiagnosticsTests(unittest.TestCase):
    def test_factory_pre_run_diagnostics_returns_six_metrics(self) -> None:
        cfg = {
            "type": "factory_mfg_basic",
            "horizon": {"num_days": 5, "minutes_per_day": 240},
            "factory": {
                "processing_time_min": {"station1": 20, "station2": 30},
            },
            "movement": {"setup_min": 3, "unload_min": 2},
            "worker": {
                "battery_swap_period_min": 200,
                "battery_drain": {"available_rate_multiplier": 0.5, "non_available_rate_multiplier": 1.0},
            },
            "decision": {
                "rolling_horizon": {
                    "scenario_worker_task_priority": {
                        "factory_mfg_basic": {
                            "A1": ["REPLENISH_MATERIAL"],
                            "A2": ["LOAD_MACHINE", "SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE"],
                            "A3": ["TRANSFER", "INSPECT_PRODUCT", "MANAGE_ROBOT_POWER"],
                        }
                    }
                },
                "battery": {
                    "low_threshold_ratio": 0.3,
                    "delivery_provider_agent_ids": ["A3"],
                    "delivery_receiver_agent_ids": ["A1", "A2"],
                },
            },
        }
        diagnostics = build_factory_pre_run_diagnostics(world=_FakeWorld(), cfg=cfg)

        self.assertTrue(diagnostics["supported"])
        self.assertEqual(FACTORY_PRE_RUN_METRIC_CODES, diagnostics["metric_order"])
        self.assertEqual(set(FACTORY_PRE_RUN_METRIC_CODES), set(diagnostics["metrics"].keys()))
        for metric_code in FACTORY_PRE_RUN_METRIC_CODES:
            metric = diagnostics["metrics"][metric_code]
            self.assertIn("value", metric)
            self.assertIn("inputs", metric)
            self.assertIn("calculation", metric)

    def test_non_factory_scenario_is_explicitly_unsupported(self) -> None:
        diagnostics = build_factory_pre_run_diagnostics(world=_FakeWorld(), cfg={"type": "shipyard_basic"})
        self.assertFalse(diagnostics["supported"])
        self.assertEqual({}, diagnostics["metrics"])


if __name__ == "__main__":
    unittest.main()
