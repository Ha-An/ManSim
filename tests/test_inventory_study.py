from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from experiments.mfg_flow_shop_paper.inventory_study import prepare, inventory_overrides
from experiments.mfg_flow_shop_paper.run_evaluation import _scenario_override_errors
from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import TileGridMap

ROOT = Path(__file__).resolve().parents[1]


def config(fill=30, capacity=30, height=None):
    cfg = yaml.safe_load((ROOT / "configs/scenario/mfg_flow_shop.yaml").read_text(encoding="utf-8"))
    cfg["warehouse"]["material_shelf"].update(capacity=capacity, initial_fill=fill)
    cfg["objective"]["throughput"]["restock_target_fill"] = fill
    if height is not None:
        cfg["map"]["warehouse_height_tiles"] = height
    return cfg


def grid(cfg):
    return TileGridMap.from_world_config(cfg, stations=[1, 2], machines_per_station=2)


class InventoryStudyTests(unittest.TestCase):
    def test_existing_layout_unchanged(self):
        g = grid(config())
        self.assertEqual((37, 4, 26, 12), tuple(vars(g.zones["Warehouse"])[k] for k in ("x", "y", "width", "height")))
        self.assertEqual({(49, 15), (50, 15)}, {p for p in g.doors if g.zones["Warehouse"].contains(p)})

    def test_four_rows_identical_and_all_pickups_reachable(self):
        a, b = grid(config(20, 40, 15)), grid(config(40, 40, 15))
        self.assertEqual(a.objects, b.objects)
        self.assertEqual(a.walls, b.walls)
        self.assertEqual(a.service_tiles, b.service_tiles)
        zone = a.zones["Warehouse"]
        self.assertEqual((18, 15), (zone.y1, zone.height))
        self.assertEqual(5, a.zones["Station2"].y - zone.y1 - 1)
        self.assertEqual({(49, 18), (50, 18)}, {p for p in a.doors if zone.contains(p)})
        for i in range(1, 41):
            name = f"warehouse_material_slot_{i:02d}"
            slot = a.objects[name]
            goals = a.service_tiles[name]
            self.assertEqual(6 + 3 * ((i - 1) // 10), slot.y)
            self.assertEqual([(slot.x, slot.y + 1)], goals)
            self.assertIsNotNone(a.find_path((49, 18), goals, worker_id="A1", ignore_dynamic=True))
            self.assertIsNotNone(a.find_path(goals[0], [(49, 18)], worker_id="A1", ignore_dynamic=True))

    def test_invalid_layout_fails_early(self):
        with self.assertRaisesRegex(ValueError, "does not fit"):
            grid(config(40, 40))
        with self.assertRaisesRegex(ValueError, "overlaps Station2"):
            grid(config(40, 40, 21))

    def test_two_thousand_unique_runs_with_matching_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "study"
            study = prepare(root)
            self.assertEqual(2000, study["total_runs"])
            paths = set()
            for condition in study["conditions"]:
                prepared = Path(condition["prepared"])
                plan = json.loads((prepared / "experiment_plan.json").read_text())
                self.assertEqual(list(range(910001, 910101)), plan["test_seeds"])
                with (prepared / "evaluation_plan.csv").open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual(1000, len(rows))
                self.assertEqual(1000, len({r["run_id"] for r in rows}))
                for row in rows:
                    self.assertNotIn(row["run_dir"], paths)
                    paths.add(row["run_dir"])
                    self.assertEqual("", row["checkpoint_path"])
                    command = json.loads(row["command_json"])
                    for key, value in inventory_overrides(condition["target_fill"]).items():
                        self.assertIn(f"{key}={value}", command)
                    self.assertIn("runtime.artifacts.export_events=false", command)
                    self.assertIn("runtime.ui.export_replay_artifacts=false", command)
            with self.assertRaises(FileExistsError):
                prepare(root)

    def test_smoke_is_separate_eight_runs_with_spatial_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            study = prepare(Path(tmp) / "smoke", smoke=True)
            self.assertEqual(8, study["total_runs"])
            for condition in study["conditions"]:
                with (Path(condition["prepared"]) / "evaluation_plan.csv").open(newline="", encoding="utf-8") as handle:
                    rows = list(csv.DictReader(handle))
                self.assertEqual({"2", "6"}, {r["worker_count"] for r in rows})
                self.assertTrue(all("runtime.artifacts.export_events=true" in json.loads(r["command_json"]) for r in rows))

    def test_archived_condition_must_match_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / ".hydra").mkdir()
            (path / ".hydra/config.yaml").write_text(yaml.safe_dump({"scenario": config(20, 40, 15)}))
            self.assertEqual([], _scenario_override_errors(path, inventory_overrides(20)))
            self.assertEqual(2, len(_scenario_override_errors(path, inventory_overrides(40))))


if __name__ == "__main__":
    unittest.main()
