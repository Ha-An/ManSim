from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
import simpy

from manufacturing_sim.simulation.scenarios.registry import scenario_type
from manufacturing_sim.simulation.scenarios.manufacturing.entities import Task
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.shipyard.grid_map import ShipyardTileGridMap
from manufacturing_sim.simulation.scenarios.shipyard.run import run
from manufacturing_sim.simulation.scenarios.shipyard.world import ShipyardWorld


class ShipyardScenarioTests(unittest.TestCase):
    def _load_cfg(self) -> dict:
        cfg = yaml.safe_load(Path("configs/scenario/shipyard_basic.yaml").read_text(encoding="utf-8"))
        decision = yaml.safe_load(Path("configs/decision/rolling_horizon_dedicated_roles.yaml").read_text(encoding="utf-8"))
        cfg["decision"] = decision
        cfg["seed"] = 2026
        return cfg

    def test_scenario_aliases(self) -> None:
        self.assertEqual(scenario_type({"name": "mfg_basic"}), "factory_mfg_basic")
        self.assertEqual(scenario_type({"type": "shipyard"}), "shipyard_basic")

    def test_shipyard_map_surface_tiles_are_valid(self) -> None:
        cfg = self._load_cfg()
        grid = ShipyardTileGridMap.from_world_config(cfg)
        self.assertEqual(grid.width_tiles, 100)
        self.assertEqual(grid.height_tiles, 70)
        self.assertGreaterEqual(len(grid.work_tiles), 100)
        self.assertLessEqual(len(grid.work_tiles), 130)
        self.assertTrue(any(obj.object_type == "ship_hull_segment" for obj in grid.objects.values()))
        self.assertNotIn("ToolCrib", grid.zones)
        self.assertEqual((4, 5, 18, 8), (grid.zones["PaintSupply"].x, grid.zones["PaintSupply"].y, grid.zones["PaintSupply"].width, grid.zones["PaintSupply"].height))
        self.assertEqual(2, grid.cart_count)
        self.assertEqual(6, len(grid.cart_parking_spots))
        self.assertEqual(grid.cart_parking_spots["CART-PARK-01"].tile, grid.initial_cart_tile("CART-01"))
        self.assertEqual(grid.cart_parking_spots["CART-PARK-04"].tile, grid.initial_cart_tile("CART-02"))
        self.assertTrue(grid.cart_route_tiles)
        for source_name in ("MaterialYard", "PaintSupply"):
            source_tile = grid.cart_source_tile(source_name)
            self.assertIsNotNone(source_tile)
            zone = grid.zones[source_name]
            self.assertGreaterEqual(source_tile[0], zone.x)
            self.assertLessEqual(source_tile[0], zone.x1)
            self.assertGreaterEqual(source_tile[1], zone.y)
            self.assertLessEqual(source_tile[1], zone.y1)
            self.assertIn(source_tile, grid.cart_route_tiles)
            route = grid.find_cart_route_path(grid.initial_cart_tile("CART-01"), source_tile, footprint_tiles=2)
            self.assertGreater(len(route), 2)
            self.assertTrue(
                all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip(route, route[1:])),
                route,
            )
        for work_tile in grid.work_tiles.values():
            self.assertIn(work_tile.tile, grid.ship_hull_tiles)
            self.assertTrue(work_tile.service_tiles)
            for tile in work_tile.service_tiles:
                self.assertTrue(grid.in_bounds(tile))
                self.assertTrue(grid.passable(tile))
                self.assertEqual(abs(tile[0] - work_tile.tile[0]) + abs(tile[1] - work_tile.tile[1]), 1)

    def test_shipyard_uses_scenario_specific_dedicated_roles(self) -> None:
        cfg = self._load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ShipyardWorld(env=simpy.Environment(), cfg=cfg, logger=logger)
                self.assertEqual(["OPERATE_VEHICLE_TRANSPORT", "TRANSFER", "WELD_SEAM", "MANAGE_ROBOT_POWER"], world._allowed_task_codes("A1"))
                self.assertEqual(["WELD_SEAM", "PREPARE_SURFACE"], world._allowed_task_codes("A2"))
                self.assertIn("OPERATE_VEHICLE_TRANSPORT", world._allowed_task_codes("A3"))
                self.assertIn("PAINT_SURFACE", world._allowed_task_codes("A3"))
            finally:
                logger.close()

    def test_shipyard_collects_batch_tasks_for_each_empty_cart(self) -> None:
        cfg = self._load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ShipyardWorld(env=simpy.Environment(), cfg=cfg, logger=logger)
                world._rolling_horizon_start_window(0.0)
                world._rolling_horizon_collect_candidates()
                cart_entries = [
                    entry
                    for entry in world.rolling_horizon_pending.values()
                    if entry["task"].task_code == "OPERATE_VEHICLE_TRANSPORT"
                ]
                self.assertGreaterEqual(len(cart_entries), 2)
                self.assertEqual({"CART-01", "CART-02"}, {entry["task"].payload["vehicle_id"] for entry in cart_entries[:2]})

                world._rolling_horizon_dispatch_pending()
                assigned = {
                    entry["assigned_worker_id"]
                    for queue in world.rolling_horizon_dispatch_queues.values()
                    for entry in queue
                    if entry["task"].task_code == "OPERATE_VEHICLE_TRANSPORT"
                }
                self.assertIn("A1", assigned)
                self.assertIn("A3", assigned)
            finally:
                logger.close()

    def test_shipyard_dispatch_load_counts_current_task(self) -> None:
        cfg = self._load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ShipyardWorld(env=simpy.Environment(), cfg=cfg, logger=logger)
                world._rolling_horizon_start_window(0.0)
                world._rolling_horizon_collect_candidates()
                entry = next(
                    entry
                    for entry in world.rolling_horizon_pending.values()
                    if entry["task"].task_code == "OPERATE_VEHICLE_TRANSPORT"
                )
                world.workers["A3"].current_task_id = "BUSY"
                self.assertEqual("A1", world._rolling_horizon_choose_worker(entry))
            finally:
                logger.close()

    def test_shipyard_rolling_horizon_uses_aging_effective_rank(self) -> None:
        cfg = self._load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ShipyardWorld(env=simpy.Environment(), cfg=cfg, logger=logger)
                world.rolling_horizon_window_index = 8
                task = world._battery_task("A3", action="self_swap")
                task.task_code = "VERIFY_SHIP_SECTION"
                task.task_type = "VERIFY_SHIP_SECTION"
                entry = {
                    "task": task,
                    "allowed_worker_ids": ["A3"],
                    "first_window_index": 3,
                }
                self.assertEqual(4, world._task_rank("A3", "VERIFY_SHIP_SECTION"))
                self.assertEqual(1, world._rolling_horizon_effective_rank(entry, "A3"))
            finally:
                logger.close()

    def test_shipyard_battery_policy_updates_power_axis(self) -> None:
        cfg = self._load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ShipyardWorld(env=env, cfg=cfg, logger=logger)
                worker = world.workers["A2"]
                env.run(until=200)
                world._emit_worker_state(worker)
                self.assertEqual("POWER_LOW", worker.humanoid_state["power"])
                env.run(until=230)
                world._emit_worker_state(worker)
                self.assertEqual("POWER_CRITICAL", worker.humanoid_state["power"])
            finally:
                logger.close()

    def test_shipyard_low_provider_prioritizes_self_swap_before_delivery(self) -> None:
        cfg = self._load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ShipyardWorld(env=simpy.Environment(), cfg=cfg, logger=logger)
                for worker in world.workers.values():
                    worker.last_battery_swap = -195.0

                world._rolling_horizon_start_window(0.0)
                world._rolling_horizon_collect_candidates()
                battery_entries = [
                    entry
                    for entry in world.rolling_horizon_pending.values()
                    if entry["task"].task_code == "MANAGE_ROBOT_POWER"
                ]
                targets = {
                    str(entry["task"].payload.get("receiver_id") or entry["task"].assigned_robot_id)
                    for entry in battery_entries
                }
                self.assertEqual({"A1", "A3"}, targets)

                world._rolling_horizon_dispatch_pending()
                queued_battery = [
                    entry
                    for queue in world.rolling_horizon_dispatch_queues.values()
                    for entry in queue
                    if entry["task"].task_code == "MANAGE_ROBOT_POWER"
                ]
                self.assertEqual(2, len(queued_battery))
                self.assertEqual(
                    1,
                    sum(1 for entry in world.rolling_horizon_dispatch_queues["A3"] if entry["task"].task_code == "MANAGE_ROBOT_POWER"),
                )
                self.assertTrue(
                    all(
                        not queue or queue[0]["task"].task_code == "MANAGE_ROBOT_POWER"
                        for queue in world.rolling_horizon_dispatch_queues.values()
                    )
                )
            finally:
                logger.close()

    def test_shipyard_battery_delivery_targets_receiver_destination(self) -> None:
        cfg = self._load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ShipyardWorld(env=simpy.Environment(), cfg=cfg, logger=logger)
                receiver = world.workers["A2"]
                receiver.tile = (20, 20)
                receiver.movement_target_tile = (30, 24)
                task = world._battery_task("A3", action="battery_delivery", receiver_id="A2")
                self.assertEqual({"x": 30, "y": 24}, task.payload["target_tile"])
                self.assertEqual((30, 24), world._battery_delivery_destination(receiver))
            finally:
                logger.close()

    def test_reserved_cart_supply_task_remains_feasible_for_own_reservation(self) -> None:
        cfg = self._load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ShipyardWorld(env=simpy.Environment(), cfg=cfg, logger=logger)
                work_tile = next(iter(world.work_tiles.values()))
                work_tile.state = "SURFACE_PREPARED"
                work_tile.paint_supply_ready = False
                cart = world.carts["CART-01"]
                cart.status = "parked"
                cart.parking_spot_id = "CART-PARK-01"
                cart.inventory_kind = "paint_can"
                cart.inventory_count = 1
                cart.reserved_count = 1
                task = Task(
                    task_id="TR-TEST",
                    task_type="TRANSFER",
                    priority_key="transfer",
                    priority=0,
                    location=work_tile.entity_id,
                    task_code="TRANSFER",
                    instance_id="TR-TEST",
                    payload={
                        "work_tile_id": work_tile.work_tile_id,
                        "ship_tile_id": work_tile.entity_id,
                        "target": work_tile.entity_id,
                        "transfer_kind": "cart_supply",
                        "item_type": "paint_can",
                        "source_cart_id": cart.cart_id,
                        "_cart_reserved": True,
                    },
                )
                self.assertTrue(world._task_still_feasible(task))
                self.assertEqual(cart.cart_id, task.payload["source"])
                self.assertEqual(cart.parking_spot_id, task.payload["parking_spot_id"])
            finally:
                logger.close()

    def test_shipyard_one_day_run_generates_kpis(self) -> None:
        cfg = self._load_cfg()
        cfg["horizon"]["num_days"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            result = run(experiment_cfg=cfg, output_dir=Path(tmp))
            kpi = result["kpi"]
            self.assertEqual(kpi["scenario_type"], "shipyard_basic")
            self.assertIn("makespan_min", kpi)
            self.assertIn("completed_surface_tile_count", kpi)
            self.assertIn("cart_trip_count", kpi)
            events_text = (Path(tmp) / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("CART_BATCH_LOADED", events_text)
            self.assertIn("SHIP_TILE_STATE_CHANGED", events_text)
            self.assertTrue((Path(tmp) / "events.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
