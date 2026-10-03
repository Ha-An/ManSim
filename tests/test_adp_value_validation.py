from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from manufacturing_sim.adp.value_validation import (
    DEFAULTS, _resolve_iterations, dashboard_panels, paired_action_loss,
    rank_correlation, regression_stats, reseed_future_draws, time_only_predictions, validate_config,
    score_episode,
)


class IndependentValueValidationTests(unittest.TestCase):
    def test_mse_and_signed_bias_units(self):
        stats = regression_stats([1, 4], [2, 2])
        self.assertEqual(stats["mse"], 2.5)
        self.assertEqual(stats["bias"], .5)
        self.assertAlmostEqual(stats["rmse"], 2.5 ** .5)
        for p, y in [([], []), ([1], [1, 2]), ([float("nan")], [0])]:
            with self.assertRaises(ValueError):
                regression_stats(p, y)

    def test_episode_scoring_uses_complete_raw_mc_not_clipped_counts(self):
        from tests.test_simulation_based_adp import _state

        state = _state()
        state.global_features[2] = 2.0
        transitions = [{"post_state": state, "raw_reward": r} for r in [1., 2., 0.]]
        with patch("manufacturing_sim.adp.model.predict_values", return_value=np.asarray([3., 2., 0.])):
            rows = score_episode(None, transitions, "cpu")
        self.assertEqual([row["target"] for row in rows], [3., 2., 0.])
        self.assertEqual(len(transitions), 3)
        self.assertEqual(state.global_features[2], 2.0)

    def test_time_only_baseline_excludes_evaluated_seed(self):
        rows = [{"seed": s, "target": float(s * 10 * r), "remaining_fraction": r}
                for s in [1, 2, 3] for r in [.1, .5, .9]]
        before = time_only_predictions(rows)
        for row in rows[:3]:
            row["target"] = 9999.
        after = time_only_predictions(rows)
        np.testing.assert_allclose(before[:3], after[:3])
        with self.assertRaises(ValueError):
            time_only_predictions(rows[:3])

    def test_paired_action_difference_cancels_common_noise(self):
        values = [[2, 10, 5, 0, 8], [5, 13, 8, 3, 11], [1, 9, 4, -1, 7]]
        result = paired_action_loss(values)
        self.assertEqual(result["observed_selection_loss"], 3.)
        self.assertEqual(result["empirical_action_spread"], 4.)
        self.assertEqual(result["action_spread_ci95_low"], 4.)
        self.assertEqual(result["empirical_best_candidate_id"], 1)
        self.assertEqual(result["empirical_worst_candidate_id"], 2)
        self.assertEqual(result["ci95_low"], 3.)
        self.assertEqual(result["ci95_high"], 3.)
        self.assertEqual(paired_action_loss(values), result)

    def test_ties_and_noise_do_not_prove_positive_loss(self):
        result = paired_action_loss([[5, 5, 5, 5, 5], [4, 6, 4, 6, 6], [6, 4, 6, 4, 4]])
        self.assertEqual(result["ci95_low"], 0.)
        self.assertGreater(result["ci95_high"], result["observed_selection_loss"])
        self.assertEqual(paired_action_loss([[1, 2, 3], [1, 2, 3]])["ci95_high"], 0.)
        with self.assertRaises(ValueError):
            paired_action_loss([[1, 2], [3, 4]])

    def test_rank_correlation_tracks_candidate_order(self):
        self.assertAlmostEqual(rank_correlation([1, 2, 3], [10, 20, 30]), 1.0)
        self.assertAlmostEqual(rank_correlation([1, 2, 3], [30, 20, 10]), -1.0)
        self.assertEqual(rank_correlation([1, 1, 1], [1, 2, 3]), 0.0)

    def test_seed_partition_rejection(self):
        validate_config({}, set(range(2026, 2600)))
        for cfg, occupied in [({}, {942001}), ({"future_seeds": [943001, 1, 2]}, set()),
                              ({"mc_seeds": [1, 1]}, set()), ({"process_count": 20}, set()),
                              ({"decision_thresholds": [20, 20]}, set())]:
            with self.assertRaises(ValueError):
                validate_config(cfg, occupied)

    def test_checkpoint_dedup_and_missing(self):
        self.assertEqual(_resolve_iterations(["initial", "best", "last"], {"best_iteration": 0}, [0, 1, 2]), [0, 2])
        with self.assertRaises(ValueError):
            _resolve_iterations([3], {"best_iteration": 0}, [0, 1, 2])

    def test_reseed_preserves_observed_and_active_state(self):
        world = SimpleNamespace(seed=2026, scenario_key="mfg_flow_shop", rng=random.Random(1),
                                quality_rng=random.Random(2), machine_failure_rngs={"S1M1": random.Random(3)},
                                humanoid_incident_rngs={"A1": random.Random(4)},
                                timing=SimpleNamespace(seed=2026, _movement_sample_cache={"move1": .1}),
                                adp_coordinator=SimpleNamespace(rng=random.Random(1)),
                                cycle_remaining=4., failure_threshold=123., position=(1, 2))
        reseed_future_draws(world, 900)
        draw = world.quality_rng.random()
        reseed_future_draws(world, 900)
        self.assertEqual(draw, world.quality_rng.random())
        reseed_future_draws(world, 901)
        self.assertNotEqual(draw, world.quality_rng.random())
        self.assertEqual(world.seed, 2026)
        self.assertEqual(world.timing._movement_sample_cache, {"move1": .1})
        self.assertEqual((world.cycle_remaining, world.failure_threshold, world.position), (4., 123., (1, 2)))

    def test_dashboard_missing_is_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("미실행", dashboard_panels(Path(tmp)))

    def test_dashboard_units_intervals_and_legacy_probe_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {"status": "completed", "episode_count": 9, "wave_count": 3, "wall_sec": 123.,
                    "process_count": 6, "config": {**DEFAULTS, "worker_counts": [3]},
                    "value_rows": [{"iteration": 1, "worker_count": 3, "rmse": 2., "bias": -1., "time_only_rmse": 3.}],
                    "action_rows": [{"iteration": 1, "worker_count": 3, "observed_selection_loss": 1., "ci95_low": 0., "ci95_high": 2.}]}
            (root / "summary.json").write_text(json.dumps(data), encoding="utf-8")
            (root / "value_validation_latest.json").write_text(json.dumps({"summary": "summary.json"}), encoding="utf-8")
            page = dashboard_panels(root)
            for expected in ("독립 Greedy MC", "동일 상태 반복 MC", "<svg", "iteration", "123.0초", "기존 학습 시간", "모집단 일반화 CI가 아닙니다"):
                self.assertIn(expected, page)
            self.assertNotIn("Top-1 일치율", page)


if __name__ == "__main__":
    unittest.main()
