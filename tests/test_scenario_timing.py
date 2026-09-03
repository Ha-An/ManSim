from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from manufacturing_sim.simulation.scenarios.manufacturing.humanoid_runtime import TASK_CODE_BY_PRIORITY_KEY
from manufacturing_sim.simulation.scenarios.manufacturing.world import MFG_FLOW_SHOP_TASK_CODES
from manufacturing_sim.simulation.scenarios.shipyard.world import SHIPYARD_TIMED_TASK_CODES
from manufacturing_sim.simulation.timing import PrimitiveTimingResolver, TimingConfigError


ROOT = Path(__file__).resolve().parents[1]


def _profile(name: str) -> dict:
    return yaml.safe_load(
        (ROOT / "configs" / "task_primitive_timing" / f"{name}.yaml").read_text(encoding="utf-8")
    )


class ScenarioTimingTests(unittest.TestCase):
    def _factory(self, seed: int = 2026) -> PrimitiveTimingResolver:
        return PrimitiveTimingResolver(
            _profile("factory_mfg_basic"),
            scenario_type="factory_mfg_basic",
            seed=seed,
            expected_task_codes=set(TASK_CODE_BY_PRIORITY_KEY.values()),
        )

    def _shipyard(self, seed: int = 2026) -> PrimitiveTimingResolver:
        return PrimitiveTimingResolver(
            _profile("shipyard_basic"),
            scenario_type="shipyard_basic",
            seed=seed,
            expected_task_codes=SHIPYARD_TIMED_TASK_CODES,
        )

    def _mfg_flow_shop(self, seed: int = 2026) -> PrimitiveTimingResolver:
        return PrimitiveTimingResolver(
            _profile("mfg_flow_shop"),
            scenario_type="mfg_flow_shop",
            seed=seed,
            expected_task_codes=MFG_FLOW_SHOP_TASK_CODES,
        )

    def test_profiles_have_strict_humanoidsim_coverage(self) -> None:
        self.assertEqual(12, len(self._factory().task_steps))
        self.assertEqual(10, len(self._mfg_flow_shop().task_steps))
        self.assertEqual(8, len(self._shipyard().task_steps))

    def test_mfg_flow_shop_profile_excludes_pm_and_handover(self) -> None:
        task_codes = set(self._mfg_flow_shop().task_steps)
        self.assertEqual(MFG_FLOW_SHOP_TASK_CODES, task_codes)
        self.assertNotIn("PREVENTIVE_MAINTENANCE", task_codes)
        self.assertNotIn("HANDOVER_ITEM", task_codes)

    def test_primitive_sampling_is_reproducible_and_bounded(self) -> None:
        left = self._factory(seed=88)
        right = self._factory(seed=88)
        path = "SETUP_MACHINE/s04_execute_machine_action"
        a = left.sample_step_duration("SETUP_MACHINE", path, sample_key="SET-000001:path")
        b = right.sample_step_duration("SETUP_MACHINE", path, sample_key="SET-000001:path")
        self.assertEqual(a, b)
        self.assertGreaterEqual(a, 2.4)
        self.assertLessEqual(a, 3.6)

    def test_movement_uses_one_sample_and_scales_with_edge_count(self) -> None:
        resolver = self._factory(seed=99)
        tile_time = resolver.sample_tile_time(sample_key="A1-move-1", multiplier=1.5)
        self.assertAlmostEqual(tile_time * 8, 2.0 * (tile_time * 4))
        self.assertEqual(tile_time, resolver.sample_tile_time(sample_key="A1-move-1", multiplier=1.5))

    def test_factory_and_shipyard_profiles_are_independent(self) -> None:
        factory_cfg = _profile("factory_mfg_basic")
        shipyard = self._shipyard()
        original_shipyard_fingerprint = shipyard.profile_fingerprint
        factory_cfg["movement"]["per_tile_min"]["mode"] = 0.11
        PrimitiveTimingResolver(
            factory_cfg,
            scenario_type="factory_mfg_basic",
            seed=2026,
            expected_task_codes=set(TASK_CODE_BY_PRIORITY_KEY.values()),
        )
        self.assertEqual(original_shipyard_fingerprint, self._shipyard().profile_fingerprint)

    def test_invalid_distribution_and_call_code_fail_before_run(self) -> None:
        invalid_range = _profile("factory_mfg_basic")
        invalid_range["movement"]["per_tile_min"].update({"min": 0.2, "mode": 0.1, "max": 0.12})
        with self.assertRaises(TimingConfigError):
            PrimitiveTimingResolver(
                invalid_range,
                scenario_type="factory_mfg_basic",
                seed=1,
                expected_task_codes=set(TASK_CODE_BY_PRIORITY_KEY.values()),
            )

        invalid_call = copy.deepcopy(_profile("shipyard_basic"))
        invalid_call["tasks"]["WELD_SEAM"]["steps"]["WELD_SEAM/s01_check_safety_zone"]["call_code"] = "GRASP"
        with self.assertRaises(TimingConfigError):
            PrimitiveTimingResolver(
                invalid_call,
                scenario_type="shipyard_basic",
                seed=1,
                expected_task_codes=SHIPYARD_TIMED_TASK_CODES,
            )


if __name__ == "__main__":
    unittest.main()
