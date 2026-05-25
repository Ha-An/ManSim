from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import simpy
import yaml

from manufacturing_sim.simulation.scenarios.manufacturing.entities import ItemState
from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import TileGridMap
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parents[1] / "configs" / "scenario" / "mfg_basic.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["horizon"]["num_days"] = 1
    cfg["humanoidsim"] = {"enabled": True, "validation_mode": "warn"}
    return cfg


class ZoneInventoryScrapTests(unittest.TestCase):
    def test_grid_map_contains_new_zones_and_shelf_slots(self) -> None:
        cfg = _load_cfg()
        grid = TileGridMap.from_world_config(cfg, stations=[1, 2], machines_per_station=2)
        self.assertIn("CompletedProducts", grid.zones)
        self.assertIn("ScrapDisposal", grid.zones)
        self.assertIn("completed_product_buffer", grid.objects)
        self.assertIn("inspection_scrap_queue", grid.objects)
        self.assertIn("scrap_disposal_bin", grid.objects)
        self.assertIn("warehouse_material_shelf", grid.objects)
        slot_ids = [object_id for object_id in grid.objects if object_id.startswith("warehouse_material_slot_")]
        self.assertEqual(cfg["warehouse"]["material_shelf"]["capacity"], len(slot_ids))
        row_ids = sorted(object_id for object_id in grid.objects if object_id.startswith("warehouse_material_shelf_row_"))
        wall_ids = sorted(object_id for object_id in grid.objects if object_id.startswith("warehouse_material_shelf_wall_"))
        self.assertEqual(3, len(row_ids))
        self.assertEqual(3, len(wall_ids))
        for row_id, wall_id in zip(row_ids, wall_ids):
            row = grid.objects[row_id]
            wall = grid.objects[wall_id]
            self.assertEqual(1, row.height)
            self.assertEqual(1, wall.height)
            self.assertEqual(row.y - 1, wall.y)
            self.assertEqual(row.x, wall.x)
            self.assertEqual(row.width, wall.width)
            self.assertEqual(grid.zones["Warehouse"].x1 - 1, row.x + row.width - 1)
            for x in range(row.x, row.x + row.width):
                self.assertFalse(grid.is_passable_static((x, wall.y)))
                self.assertFalse(grid.is_passable_static((x, row.y)))
                self.assertTrue(grid.is_passable_static((x, row.y + 1)))
        self.assertEqual(grid.zones["Warehouse"].y + 2, grid.objects["warehouse_material_shelf_row_01"].y)
        self.assertEqual(grid.zones["Warehouse"].x1 - 1, grid.objects["warehouse_material_slot_01"].x)
        self.assertEqual(grid.objects["warehouse_material_shelf_row_01"].x, grid.objects["warehouse_material_slot_10"].x)
        for slot_id in slot_ids:
            self.assertTrue(grid.service_tiles.get(slot_id), slot_id)
            self.assertTrue(
                grid.destination_tiles(slot_id, worker_id="A1", from_tile=grid.initial_worker_tile("A1")),
                slot_id,
            )
            slot = grid.objects[slot_id]
            self.assertEqual([(slot.x, slot.y + 1)], grid.service_tiles[slot_id])
            self.assertTrue(
                all(abs(tile[0] - slot.x) + abs(tile[1] - slot.y) == 1 for tile in grid.service_tiles[slot_id]),
                slot_id,
            )

        station2_material = grid.objects["material_queue_2"]
        station2_intermediate = grid.objects["intermediate_queue_2"]
        station2_output = grid.objects["station_2_output_queue"]
        inspection_input = grid.objects["intermediate_queue_4"]
        inspection_output = grid.objects["inspection_output_queue"]
        inspection_scrap = grid.objects["inspection_scrap_queue"]
        self.assertEqual(station2_output.y, inspection_input.y)
        self.assertEqual(station2_material.y, inspection_output.y)
        self.assertEqual(station2_intermediate.y, inspection_scrap.y)
        self.assertGreater(inspection_scrap.y, inspection_output.y)

        station2_machine = grid.objects["S2M1"]
        inspection_table = grid.objects["inspection_table"]
        self.assertEqual(station2_machine.y, inspection_table.y)
        inspection = grid.zones["Inspection"]
        south_door_tiles = {(inspection.x + inspection.width // 2, inspection.y1), (inspection.x + inspection.width // 2 - 1, inspection.y1)}
        self.assertTrue(south_door_tiles.issubset(grid.doors))
        self.assertTrue(south_door_tiles.isdisjoint(grid.walls))

    def test_material_shelf_pick_and_restock(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                self.assertEqual(world.material_shelf_initial_fill, world._material_shelf_count())
                picked = world._pop_material_shelf_item("warehouse_material_slot_01")
                self.assertIsNotNone(picked)
                self.assertEqual(world.material_shelf_initial_fill - 1, world._material_shelf_count())
                world._restock_material_shelf(reason="day_boundary")
                self.assertEqual(world.material_shelf_capacity, world._material_shelf_count())
                self.assertTrue(any(event["type"] == "WAREHOUSE_MATERIAL_RESTOCK" for event in logger.events))
                self.assertTrue(any(event["type"] == "WAREHOUSE_MATERIAL_PICKED" for event in logger.events))
            finally:
                logger.close()

    def test_scrap_queue_batch_limit_and_state(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                for index in range(4):
                    world._push_inspection_scrap_queue(f"SCRAP-{index + 1}")
                batch = world._pop_inspection_scrap_batch(3)
                self.assertEqual(["SCRAP-1", "SCRAP-2", "SCRAP-3"], batch)
                self.assertEqual(1, len(world.inspection_scrap_queue))
                for item_id in batch:
                    self.assertEqual(ItemState.CARRIED_BY_WORKER, world.items[item_id].state)
                self.assertEqual(ItemState.WAITING_SCRAP_DISPOSAL, world.items["SCRAP-4"].state)
            finally:
                logger.close()


if __name__ == "__main__":
    unittest.main()
