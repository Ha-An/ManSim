import pytest

from scripts.analyze_inventory_saturation import paired_interval, practical_knee, predict


def capacity_rows():
    return [
        {
            "worker_count": n,
            "horizon_min": 100,
            "expected_first_product_min": 10,
            "material_capacity_bound": 11,
            "expected_station1_cycle_min": 1,
            "expected_station2_cycle_min": 1,
            "expected_inspection_cycle_min": 1,
            "battery_duty_fraction": 1,
            "quality_yield": 0.9,
            "expected_pipeline_good_products": 0,
            "realistic_expected_products": 0,
        }
        for n in range(1, 4)
    ]


def test_knee_is_relative_to_observed_max_not_last_point():
    assert practical_knee({2: 36.25, 3: 43.48, 4: 42.2, 5: 42.57, 6: 42.41}) == 3
    assert practical_knee({2: 36.25, 3: 52.51, 4: 67.66, 5: 80.68, 6: 84.03}) == 5


def test_paired_interval_uses_matching_seed_not_input_order():
    result = paired_interval({2: 5, 1: 9}, {1: 12, 2: 8})
    assert result == {
        "mean_gain": 3.0,
        "paired_bootstrap_ci95_low": 3.0,
        "paired_bootstrap_ci95_high": 3.0,
    }


def test_paired_interval_rejects_different_seeds():
    with pytest.raises(ValueError, match="identical seeds"):
        paired_interval({1: 3}, {2: 5})


def test_predict_conserves_material_and_charges_empty_travel():
    rows = predict(capacity_rows(), {
        "total_worker_min_per_attempt": 20,
        "direct_worker_min_per_attempt": 10,
    })
    assert [r["revised_predicted_products"] for r in rows] == pytest.approx([4.95, 9, 9.9])
    assert rows[0]["fluid_fleet_crossover"] == pytest.approx(20 / 9)
    assert rows[0]["rounded_fleet_crossover"] == 3
    assert all(r["revised_without_empty_travel_products"] >= r["revised_predicted_products"] for r in rows)
    assert all(r["revised_predicted_products"] <= r["material_nominal_good_ceiling"] for r in rows)


def test_non_humanoid_capacity_can_limit_large_fleet():
    source = capacity_rows()
    for row in source:
        row["expected_inspection_cycle_min"] = 50
    rows = predict(source, {
        "total_worker_min_per_attempt": 20,
        "direct_worker_min_per_attempt": 10,
    })
    assert [r["revised_predicted_products"] for r in rows] == pytest.approx([2.52] * 3)
    assert all(r["rounded_fleet_crossover"] == 1 for r in rows)
