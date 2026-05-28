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
        spacer_ids = sorted(object_id for object_id in grid.objects if object_id.startswith("warehouse_material_shelf_spacer_"))
        wall_ids = sorted(object_id for object_id in grid.objects if object_id.startswith("warehouse_material_shelf_wall_"))
        self.assertEqual(27, len(spacer_ids))
        self.assertEqual(3, len(wall_ids))
        for row_index, wall_id in enumerate(wall_ids, start=1):
            wall = grid.objects[wall_id]
            self.assertEqual(1, wall.height)
            row_spacers = [grid.objects[object_id] for object_id in spacer_ids if f"spacer_{row_index:02d}_" in object_id]
            self.assertEqual(9, len(row_spacers))
            self.assertTrue(row_spacers)
            self.assertEqual(row_spacers[0].y - 1, wall.y)
            self.assertEqual(grid.zones["Warehouse"].x1 - 1, wall.x + wall.width - 1)
            for x in range(wall.x, wall.x + wall.width):
                self.assertIn((x, wall.y), grid.walls)
                self.assertFalse(grid.is_passable_static((x, wall.y)))
            for spacer in row_spacers:
                self.assertEqual("shelf_low_wall", spacer.object_type)
                self.assertEqual(1, spacer.width)
                self.assertEqual(1, spacer.height)
                self.assertIn((spacer.x, spacer.y), grid.walls)
                self.assertFalse(grid.is_passable_static((spacer.x, spacer.y)))
                self.assertTrue(grid.is_passable_static((spacer.x, spacer.y + 1)))
        self.assertEqual(grid.zones["Warehouse"].y + 2, grid.objects["warehouse_material_slot_01"].y)
        self.assertEqual(grid.zones["Warehouse"].x1 - 1, grid.objects["warehouse_material_slot_01"].x)
        self.assertEqual(grid.objects["warehouse_material_shelf_wall_01"].x, grid.objects["warehouse_material_slot_10"].x)
        for slot_id in slot_ids:
            self.assertTrue(grid.service_tiles.get(slot_id), slot_id)
            self.assertTrue(
                grid.destination_tiles(slot_id, worker_id="A1", from_tile=grid.initial_worker_tile("A1")),
                slot_id,
            )
            slot = grid.objects[slot_id]
            self.assertNotIn((slot.x, slot.y), grid.walls)
            self.assertFalse(grid.is_passable_static((slot.x, slot.y)))
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

    def test_material_supply_candidates_are_generic_until_execution(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=3)
                for station in world.stations:
                    world.material_queues[station].clear()

                first_task = next(
                    task
                    for task in world._candidate_tasks(world.agents["A1"])
                    if task.priority_key == "material_supply"
                )
                reserved_station = first_task.payload["station"]
                self.assertNotIn("transfer_item_id", first_task.payload)
                self.assertNotIn("source_slot_id", first_task.payload)
                self.assertEqual("available_material_from_source", first_task.payload.get("item_request", {}).get("selection_policy"))
                self.assertIs(world._finalize_selected_task(world.agents["A1"], first_task), first_task)

                second_material_tasks = [
                    task
                    for task in world._candidate_tasks(world.agents["A2"])
                    if task.priority_key == "material_supply"
                ]
                second_stations = {task.payload["station"] for task in second_material_tasks}
                self.assertNotIn(reserved_station, second_stations)
            finally:
                logger.close()

    def test_output_transfer_candidates_skip_reserved_buffer_items(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world.output_buffers[1].append("INT-1")
                world.output_buffers[1].append("INT-2")
                first_task = next(
                    task
                    for task in world._candidate_tasks(world.agents["A1"])
                    if task.priority_key == "inter_station_transfer" and task.payload.get("from_station") == 1
                )
                self.assertEqual("INT-1", first_task.payload["transfer_item_id"])
                self.assertTrue(world._reserve_task_items(world.agents["A1"], first_task))

                second_task = next(
                    task
                    for task in world._candidate_tasks(world.agents["A2"])
                    if task.priority_key == "inter_station_transfer" and task.payload.get("from_station") == 1
                )
                self.assertEqual("INT-2", second_task.payload["transfer_item_id"])
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
