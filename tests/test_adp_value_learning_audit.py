from __future__ import annotations

import unittest

from manufacturing_sim.adp.compact import _mc_targets
from manufacturing_sim.adp.encoding import _norm
from experiments.adp_counterfactual_mc.audit_value_learning import (
    cross_episode_baselines,
    regression_stats,
)


class ValueLearningAuditTests(unittest.TestCase):
    def test_raw_mc_target_is_independent_of_product_feature_clipping(self):
        targets = _mc_targets([{"raw_reward": r} for r in [2., 1., 0.]], 1.0)
        counts = [59., 61., 62.]
        self.assertEqual(targets, [62. - count for count in counts])
        self.assertEqual(_norm(61., 30.) * 30., 60.)
        self.assertNotEqual(targets[1], 62. - _norm(counts[1], 30.) * 30.)

    def test_regression_units_and_bias(self):
        result = regression_stats([2., 4.], [1., 2.])
        self.assertEqual(result["mse"], 2.5)
        self.assertEqual(result["mae"], 1.5)
        self.assertEqual(result["bias"], 1.5)
        self.assertAlmostEqual(result["rmse"] ** 2, result["mse"])

    def test_baselines_never_fit_evaluated_episode(self):
        rows = [
            {"seed": 1, "products": 10., "products_before": 2., "target": 8., "remaining_fraction": .8},
            {"seed": 2, "products": 12., "products_before": 2., "target": 10., "remaining_fraction": .8},
            {"seed": 3, "products": 14., "products_before": 2., "target": 12., "remaining_fraction": .8},
        ]
        original = cross_episode_baselines(rows)
        changed = [{**row} for row in rows]
        changed[0].update(products=1000., target=998.)
        modified = cross_episode_baselines(changed)
        self.assertEqual(original[0][0], 11.)
        self.assertEqual(original[0][0], modified[0][0])
        self.assertAlmostEqual(original[1][0], modified[1][0])

    def test_invalid_inputs_fail(self):
        with self.assertRaises(ValueError):
            regression_stats([float("nan")], [1.])
        with self.assertRaises(ValueError):
            regression_stats([], [])


if __name__ == "__main__":
    unittest.main()
