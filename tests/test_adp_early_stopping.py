import math

import pytest

from manufacturing_sim.adp.early_stopping import ProductionEarlyStopping, validate_early_stopping


def config(**changes):
    return {"enabled": True, "min_iterations": 45, "patience_iterations": 20,
            "consecutive_checks": 2, "paired_ci_upper_threshold": .5, **changes}


def products(value):
    return {seed: float(value) for seed in range(10)}


def test_minimum_patience_and_two_paired_checks_are_all_required():
    stopper = ProductionEarlyStopping(config())
    for i, value in [(0, 10), (15, 11), (25, 12), (30, 11), (35, 11), (40, 11)]:
        assert not stopper.observe(i, products(value))["early_stop_triggered"]
    row = stopper.observe(45, products(11))
    assert row["early_stop_triggered"]
    assert row["early_stop_paired_mean"] == -1
    assert row["early_stop_paired_ci_upper"] == -1
    assert row["early_stop_best_iteration"] == 25


def test_any_positive_improvement_resets_patience_not_half_product():
    stopper = ProductionEarlyStopping(config())
    stopper.observe(0, products(10))
    stopper.observe(20, products(10))
    tiny = products(10)
    tiny[0] = 11
    row = stopper.observe(40, tiny)
    assert row["early_stop_best_iteration"] == 40
    assert row["early_stop_stagnant_iterations"] == 0
    assert not stopper.observe(45, products(10))["early_stop_triggered"]


def test_wide_ci_does_not_stop_even_when_mean_is_unchanged():
    stopper = ProductionEarlyStopping(config())
    stopper.observe(0, products(10))
    variable = {seed: 0. if seed % 2 else 20. for seed in range(10)}
    stopper.observe(40, variable)
    row = stopper.observe(45, variable)
    assert row["early_stop_paired_mean"] == 0
    assert row["early_stop_paired_ci_upper"] > .5
    assert not row["early_stop_triggered"]


def test_disabled_and_mismatched_or_nonfinite_observations():
    assert ProductionEarlyStopping({}).observe(0, {}) == {}
    stopper = ProductionEarlyStopping(config())
    stopper.observe(0, products(10))
    with pytest.raises(ValueError, match="same screening seeds"):
        stopper.observe(5, {100: 1., 101: 2.})
    with pytest.raises(ValueError, match="finite"):
        stopper.observe(5, {1: math.nan})
    with pytest.raises(ValueError, match="increasing"):
        stopper.observe(0, products(10))


@pytest.mark.parametrize("changes", [{"min_iterations": 76}, {"min_iterations": 1.5},
    {"patience_iterations": 0}, {"consecutive_checks": 0}, {"paired_ci_upper_threshold": float("nan")}])
def test_invalid_early_stop_config(changes):
    with pytest.raises(ValueError):
        validate_early_stopping(config(**changes), max_iterations=75, seed_count=10)


def test_single_screening_seed_is_rejected():
    with pytest.raises(ValueError, match="two paired"):
        validate_early_stopping(config(), max_iterations=75, seed_count=1)
