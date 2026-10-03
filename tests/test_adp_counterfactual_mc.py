from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from manufacturing_sim.adp.compact import CompactMCBatch, compact_episode_transitions, merge_compact_batches
from manufacturing_sim.adp.model import require_torch
from manufacturing_sim.adp.schema import (
    EncodedDecisionState, GLOBAL_FEATURE_DIM, WORKER_FEATURE_DIM, TASK_FEATURE_DIM, PAIR_FEATURE_DIM,
)
from manufacturing_sim.adp.train import fit_mc_value
from experiments.adp_counterfactual_mc.run_experiment import experiment_arms, sampling_counts, write_report


def samples(*, extra: bool) -> CompactMCBatch:
    episodes = []
    for episode in range(4):
        state = EncodedDecisionState(
            global_features=np.ones(GLOBAL_FEATURE_DIM, dtype=np.float32),
            worker_features=np.zeros((1, WORKER_FEATURE_DIM), dtype=np.float32),
            task_features=np.zeros((1, TASK_FEATURE_DIM), dtype=np.float32),
            pair_features=np.zeros((1, 1, PAIR_FEATURE_DIM), dtype=np.float32),
            feasibility=np.ones((1, 1), dtype=bool), worker_ids=["A1"], opportunity_ids=["O1"],
        )
        transitions = [{"post_state": state, "raw_reward": 100 + episode if extra else 1}
                       for _ in range(1 if extra else 6)]
        episodes.append(compact_episode_transitions(transitions, episode_id=episode, worker_count=1))
    return merge_compact_batches(episodes)


class CounterfactualMCTests(unittest.TestCase):
    def test_counterfactual_parent_holdout_never_enters_training_and_steps_are_equal(self):
        torch = require_torch()

        class LinearValue(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.weight = torch.nn.Parameter(torch.tensor(0.1))

            def forward(self, **inputs):
                return self.weight * inputs["global_features"][:, 0]

        histories = []
        for fraction, expected_extra_draws in ((0.0, 0), (0.01, 22), (0.1, 216)):
            model = LinearValue()
            optimizer = torch.optim.SGD(model.parameters(), lr=0.001)
            original = CompactMCBatch.model_batch
            training, holdout = [], []

            def observe(batch, indices, device):
                (training if model.training else holdout).append(
                    [(int(batch.episode_ids[i]), float(batch.targets[i])) for i in indices]
                )
                return original(batch, indices, device)

            with patch.object(CompactMCBatch, "model_batch", observe):
                fit_mc_value(model=model, optimizer=optimizer,
                    samples=merge_compact_batches([samples(extra=False)] * 60),
                    counterfactual_samples=samples(extra=True) if fraction else None,
                    counterfactual_fraction=fraction, device="cpu", batch_size=512,
                    max_epochs=2, gradient_clip=1, patience=3,
                    validation_episode_fraction=0.25, rng=random.Random(2026))
            heldout_ids = {episode for batch in holdout for episode, _ in batch}
            self.assertFalse(heldout_ids & {episode for batch in training for episode, _ in batch})
            self.assertEqual(sum(target >= 100 for batch in training for _, target in batch), expected_extra_draws)
            if fraction:
                self.assertTrue(any(target >= 100 for batch in training for _, target in batch))
                self.assertFalse(any(target >= 100 for batch in holdout for _, target in batch))
            histories.append(training)
        for history in histories[1:]:
            self.assertEqual(len(histories[0]), len(history))
            self.assertEqual([len(batch) for batch in histories[0]], [len(batch) for batch in history])

    def test_fraction_configuration_and_legacy(self):
        self.assertEqual(experiment_arms({"counterfactual_fractions": [0, 0.01, 0.1]}),
                         {"control": 0, "cf_1pct": 0.01, "cf_10pct": 0.1})
        self.assertEqual(experiment_arms({"counterfactual_fraction": 0.1}),
                         {"control": 0, "treatment": 0.1})
        for values in ([0], [0.1, 0], [0, 1], [0, float("nan")], [0, 0.1, 0.1]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                experiment_arms({"counterfactual_fractions": values})

    def test_actual_fraction_accounts_for_integer_minibatches(self):
        for fraction, count in ((0, 0), (0.01, 77), (0.1, 782)):
            result = sampling_counts(7852, 512, fraction)
            self.assertEqual(result["sgd_steps_per_epoch"], 16)
            self.assertEqual(result["counterfactual_draws_per_epoch"], count)
            self.assertAlmostEqual(result["actual_counterfactual_fraction"], count / 7852)

    def test_three_arm_report_without_action_diagnostics(self):
        arms = {"control": 0.0, "cf_1pct": 0.01, "cf_10pct": 0.1}
        summary = {"source_checkpoint": "test.pt", "worker_count": 3, "horizon_days": 5,
                   "training_episodes": 10, "epsilon": 0.5, "process_count": 10, "gpu": "test",
                   "alternative_training_samples": 10, "counterfactual_fractions": arms,
                   "evaluation_seeds": [1, 2], "identity_replay_passed": True,
                   "source_unchanged": True, "wall_sec": 1, "conclusion": "test",
                   "evaluation": {arm: {"mean": 2, "std": 0, "products": [2, 2]} for arm in arms},
                   "updates": [{"arm": arm, "holdout_mse": 1 + index, "common_batch_mse": 1,
                                "training_pair_mse": 1, "gpu_update_sec": 1}
                               for index, arm in enumerate(arms)],
                   "comparisons_vs_control": {arm: {"mean_difference": 0, "paired_bootstrap_ci95": [0, 0],
                                                      "wins": 0, "ties": 2, "losses": 0}
                                              for arm in arms if arm != "control"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            write_report(path, summary, [])
            page = (path / "diagnosis_dashboard.html").read_text(encoding="utf-8")
            self.assertEqual(page.count("<svg"), 2)
            for fraction in ("0%", "1%", "10%"):
                self.assertIn(fraction, page)
            self.assertNotIn("diagnostic_alternative", page)

    def test_counterfactual_without_training_parent_fails(self):
        torch = require_torch()
        model = torch.nn.Linear(1, 1)
        extra = samples(extra=True)
        extra.episode_ids.fill_(999)
        with self.assertRaisesRegex(ValueError, "belong to training"):
            fit_mc_value(model=model, optimizer=torch.optim.SGD(model.parameters(), lr=0.1),
                samples=samples(extra=False), counterfactual_samples=extra,
                device="cpu", batch_size=4, max_epochs=1, gradient_clip=1,
                patience=1, validation_episode_fraction=0.25, rng=random.Random(2026))


if __name__ == "__main__":
    unittest.main()
