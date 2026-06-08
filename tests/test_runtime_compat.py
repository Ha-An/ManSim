from __future__ import annotations

import unittest

from omegaconf import OmegaConf

from runtime.compat import build_legacy_experiment_cfg


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


if __name__ == "__main__":
    unittest.main()
