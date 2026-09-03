from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from omegaconf import OmegaConf

from runtime.compat import build_legacy_experiment_cfg
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger


class RuntimeCompatTests(unittest.TestCase):
    def test_global_worker_config_merges_without_dropping_scenario_worker_settings(self) -> None:
        cfg = OmegaConf.create(
            {
                "scenario": {
                    "worker": {
                        "battery_swap_period_min": 200,
                        "battery_drain": {
                            "available_rate_multiplier": 0.5,
                            "non_available_rate_multiplier": 1.0,
                        },
                    }
                },
                "worker": {
                    "execution_mode": "commitment",
                    "local_response": {"enabled": True},
                },
                "decision": {},
                "heuristic_rules": {},
                "humanoidsim": {},
                "runtime": {"artifacts": {"export_events": False}},
                "seed": 2026,
            }
        )

        experiment_cfg = build_legacy_experiment_cfg(cfg)

        self.assertEqual(200, experiment_cfg["worker"]["battery_swap_period_min"])
        self.assertEqual(
            {"available_rate_multiplier": 0.5, "non_available_rate_multiplier": 1.0},
            experiment_cfg["worker"]["battery_drain"],
        )
        self.assertEqual("commitment", experiment_cfg["worker"]["execution_mode"])
        self.assertEqual({"enabled": True}, experiment_cfg["worker"]["local_response"])
        self.assertFalse(experiment_cfg["runtime"]["artifacts"]["export_events"])

    def test_event_logger_can_keep_events_in_memory_without_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp), persist_events=False)
            logger.log(t=1.0, day=1, event_type="TEST_EVENT")
            logger.close()
            self.assertEqual(1, len(logger.events))
            self.assertFalse((Path(tmp) / "events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
