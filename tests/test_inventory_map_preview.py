from copy import deepcopy
from pathlib import Path

import pytest

from scripts.render_inventory_maps import align_condition


def condition(fill, capacity, height):
    return {
        "fill": fill,
        "restock_target": fill,
        "run": Path(f"archived/materials_{fill}"),
        "layout": {"grid": {"walls": [{"x": 37, "y": height + 3}]}},
        "warehouse": {"width": 26, "height": height},
        "doors": [{"x": 49, "y": height + 3}],
        "slots": [{"object_id": f"material_slot_{i:02d}"} for i in range(1, capacity + 1)],
        "workers": {"A1": {"x": 10, "y": 20}},
    }


def test_aligned_preview_preserves_stock_and_copies_complete_geometry():
    original = condition(30, 30, 12)
    reference = condition(40, 40, 15)
    old_original, old_reference = deepcopy(original), deepcopy(reference)
    aligned = align_condition(original, reference)
    assert aligned["fill"] == aligned["restock_target"] == 30
    assert len(aligned["slots"]) == 40
    for key in ("layout", "warehouse", "doors", "slots", "workers"):
        assert aligned[key] == reference[key]
        assert aligned[key] is not reference[key]
    assert aligned["run"] == reference["run"]
    assert aligned["historical_run"] == original["run"]
    assert aligned["source_kind"] == "proposed_aligned_layout"
    assert aligned["asset_suffix"] == "_aligned"
    assert original == old_original
    assert reference == old_reference


@pytest.mark.parametrize("key", ["fill", "restock_target"])
def test_aligned_preview_rejects_capacity_overflow(key):
    original = condition(30, 30, 12)
    original[key] = 41
    with pytest.raises(ValueError, match="capacity"):
        align_condition(original, condition(40, 40, 15))
