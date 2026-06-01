from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import copy
import tempfile
import unittest
from unittest.mock import patch

import simpy
import yaml

from agents.factory import build_decision_module
from agents.modes import format_decision_mode_label, is_fixed_priority_mode, normalize_decision_mode
from manufacturing_sim.simulation.scenarios.manufacturing.entities import Task
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


def _load_cfg(decision_name: str = "rolling_horizon_aging_priority") -> dict:
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "configs" / "scenario" / "mfg_basic.yaml").read_text(encoding="utf-8"))
    cfg["decision"] = yaml.safe_load(
        (root / "configs" / "decision" / f"{decision_name}.yaml").read_text(encoding="utf-8")
    )
    cfg["heuristic_rules"] = yaml.safe_load((root / "configs" / "heuristic_rules" / "default.yaml").read_text(encoding="utf-8"))
    cfg["humanoidsim"] = yaml.safe_load((root / "configs" / "humanoidsim" / "default.yaml").read_text(encoding="utf-8"))
    cfg["horizon"]["num_days"] = 1
    cfg["horizon"]["minutes_per_day"] = 60
    return cfg


class RollingHorizonDecisionTests(unittest.TestCase):
    def test_mode_registry_recognizes_rolling_horizon_aging_priority(self) -> None:
        self.assertEqual("rolling_horizon_aging_priority", normalize_decision_mode("rolling_horizon_aging_priority"))
        self.assertEqual("rolling_horizon_aging_priority", normalize_decision_mode("rolling_horizon_fixed_priority"))
        self.assertTrue(is_fixed_priority_mode("rolling_horizon_aging_priority"))
        self.assertEqual("Rolling Horizon Aging Priority", format_decision_mode_label("rolling_horizon_aging_priority"))
        module = build_decision_module(experiment_cfg={"decision": {"mode": "rolling_horizon_aging_priority"}}, decision_mode="rolling_horizon_aging_priority")
        self.assertEqual("rolling_horizon_aging_priority", module.decision_mode)
        self.assertTrue(module.static_priority_policy)

    def test_mode_registry_recognizes_rolling_horizon_dedicated_roles(self) -> None:
        self.assertEqual("rolling_horizon_dedicated_roles", normalize_decision_mode("rolling_horizon_dedicated_roles"))
        self.assertTrue(is_fixed_priority_mode("rolling_horizon_dedicated_roles"))
        self.assertEqual("Rolling Horizon Dedicated Roles", format_decision_mode_label("rolling_horizon_dedicated_roles"))
        module = build_decision_module(experiment_cfg={"decision": {"mode": "rolling_horizon_dedicated_roles"}}, decision_mode="rolling_horizon_dedicated_roles")
        self.assertEqual("rolling_horizon_dedicated_roles", module.decision_mode)
        self.assertTrue(module.static_priority_policy)

    def test_general_task_waits_until_window_boundary(self) -> None:
        cfg = _load_cfg()
        self.assertIn("scenario_task_code_priority_order", cfg["decision"]["rolling_horizon"])
        self.assertNotIn("scan_interval_min", cfg["decision"]["rolling_horizon"])
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()

                self.assertIsNone(world.select_task_for_agent(world.agents["A1"]))
                self.assertGreater(len(world.rolling_horizon_pending), 0)
                self.assertTrue(any(event["type"] == "ROLLING_HORIZON_CANDIDATE_COLLECTED" for event in logger.events))

                world.env.run(until=5.0)
                selected = None
                for agent_id in sorted(world.agents.keys()):
                    selected = world.select_task_for_agent(world.agents[agent_id])
                    if selected is not None:
                        break

                self.assertIsNotNone(selected)
                assert selected is not None
                self.assertEqual("rolling_horizon_aging_priority", selected.selection_meta.get("decision_source"))
                self.assertTrue(any(event["type"] == "ROLLING_HORIZON_DISPATCH" for event in logger.events))
            finally:
                logger.close()

    def test_priority_rank_uses_humanoidsim_task_code_not_task_family(self) -> None:
        cfg = _load_cfg()
        cfg["decision"]["rolling_horizon"]["scenario_task_code_priority_order"]["factory_mfg_basic"] = [
            "TRANSFER",
            "REPAIR_MACHINE",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                task = Task(
                    task_id="T-test",
                    task_type="TRANSFER",
                    priority_key="material_supply",
                    priority=2.0,
                    location="warehouse_material_slot_01",
                    payload={"transfer_kind": "material_supply", "station": 1, "transfer_item_id": "MAT-WH-1"},
                    task_code="TRANSFER",
                )

                self.assertEqual(1.0, world._rolling_horizon_priority(task))
            finally:
                logger.close()

    def test_repeated_scans_do_not_duplicate_same_opportunity(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()

                world._rolling_horizon_collect_candidates()
                first_ids = set(world.rolling_horizon_pending.keys())
                first_worker_sets = {
                    opportunity_id: set(entry.get("workers", set()))
                    for opportunity_id, entry in world.rolling_horizon_pending.items()
                }
                first_collected_count = int(world.rolling_horizon_metrics["candidate_collected_count"])

                world._rolling_horizon_collect_candidates()

                self.assertEqual(first_ids, set(world.rolling_horizon_pending.keys()))
                self.assertEqual(first_collected_count, int(world.rolling_horizon_metrics["candidate_collected_count"]))
                for opportunity_id, entry in world.rolling_horizon_pending.items():
                    self.assertEqual(first_worker_sets[opportunity_id], set(entry.get("workers", set())))
            finally:
                logger.close()

    def test_material_supply_candidates_are_generic_station_requests(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()

                tasks = world._candidate_tasks(world.agents["A1"])
                material_tasks = [
                    task
                    for task in tasks
                    if task.payload.get("transfer_kind") == "material_supply"
                ]

                self.assertGreaterEqual(len(material_tasks), 2)
                stations = [int(task.payload.get("station")) for task in material_tasks]
                self.assertEqual(len(stations), len(set(stations)))
                for task in material_tasks:
                    self.assertNotIn("transfer_item_id", task.payload)
                    self.assertNotIn("source_slot_id", task.payload)
                    self.assertNotIn("material_item_id", task.payload)
                    self.assertEqual("available_material_from_source", task.payload.get("item_request", {}).get("selection_policy"))
            finally:
                logger.close()

    def test_rolling_pool_does_not_hold_two_opportunities_for_same_resource(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()

                world._rolling_horizon_collect_candidates()

                seen_resource_keys: set[str] = set()
                for entry in world.rolling_horizon_pending.values():
                    for key in entry.get("exclusive_resource_keys", []):
                        self.assertNotIn(key, seen_resource_keys)
                        seen_resource_keys.add(key)
            finally:
                logger.close()

    def test_unresolved_material_supply_blocks_same_station_next_window(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()

                world._rolling_horizon_collect_candidates()
                station2_entries = [
                    (opportunity_id, entry)
                    for opportunity_id, entry in world.rolling_horizon_pending.items()
                    if entry.get("task_code") == "REPLENISH_MATERIAL"
                    and entry.get("target_station") == 2
                ]
                self.assertEqual(1, len(station2_entries))
                station2_opportunity_id, _station2_entry = station2_entries[0]

                # Simulate the first station-1 replenishment being consumed while
                # the station-2 replenishment remains unresolved in the pool.
                for opportunity_id, entry in list(world.rolling_horizon_pending.items()):
                    if entry.get("task_code") == "REPLENISH_MATERIAL" and entry.get("target_station") == 1:
                        world.rolling_horizon_pending.pop(opportunity_id, None)
                slot1 = world.warehouse_material_shelf_slots.get("warehouse_material_slot_01")
                if isinstance(slot1, dict):
                    slot1["occupied"] = False
                    slot1["material_item_id"] = ""
                world._rolling_horizon_rebuild_pending_resource_index()

                world.env.run(until=15.0)
                world._rolling_horizon_window_index = 3
                world._rolling_horizon_collect_candidates()

                station2_entries_after = [
                    (opportunity_id, entry)
                    for opportunity_id, entry in world.rolling_horizon_pending.items()
                    if entry.get("task_code") == "REPLENISH_MATERIAL"
                    and entry.get("target_station") == 2
                ]
                self.assertEqual(1, len(station2_entries_after))
                self.assertEqual(station2_opportunity_id, station2_entries_after[0][0])
            finally:
                logger.close()

    def test_unresolved_pool_persists_and_blocks_same_resource_next_window(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()

                world._rolling_horizon_collect_candidates()
                self.assertTrue(world.rolling_horizon_pending)
                opportunity_id, entry = next(iter(world.rolling_horizon_pending.items()))
                rolling_signature = dict(entry["rolling_task_signature"])
                original_keys = set(entry.get("exclusive_resource_keys", []))
                self.assertTrue(original_keys)

                for agent in world.agents.values():
                    agent.suspended_task = Task(
                        task_id="suspended",
                        task_type="TRANSFER",
                        priority_key="inter_station_transfer",
                        priority=1.0,
                        location="Station1",
                    )
                world.env.run(until=5.0)
                world._rolling_horizon_update()

                self.assertIn(opportunity_id, world.rolling_horizon_pending)
                for key in original_keys:
                    self.assertEqual(opportunity_id, world.rolling_horizon_pending_resource_index.get(key))

                for agent in world.agents.values():
                    agent.suspended_task = None
                original_station = int(rolling_signature.get("target_station") or 1)
                conflicting = Task(
                    task_id="conflicting-material-supply",
                    task_type="TRANSFER",
                    priority_key="material_supply",
                    priority=86.0,
                    location="Warehouse",
                    payload={
                        "transfer_kind": "material_supply",
                        "station": original_station,
                        "target_station": original_station,
                        "target_type": "station",
                        "target_id": f"station{original_station}",
                        "transfer_item_id": "MAT-WH-CONFLICT",
                        "source_slot_id": "warehouse_material_slot_conflict",
                    },
                    task_code="REPLENISH_MATERIAL",
                )
                conflicting_id = world._rolling_horizon_opportunity_id(conflicting)
                self.assertNotEqual(opportunity_id, conflicting_id)

                with patch.object(world, "_candidate_tasks", return_value=[conflicting]):
                    world._rolling_horizon_collect_candidates()

                self.assertNotIn(conflicting_id, world.rolling_horizon_pending)
                self.assertIn(opportunity_id, world.rolling_horizon_pending)
            finally:
                logger.close()

    def test_window_dispatches_all_feasible_tasks_into_worker_queues(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world.env.run(until=5.0)
                for machine in world.machines.values():
                    machine.broken = True
                    machine.repair_work_remaining_min = 10.0

                tasks = [
                    Task(
                        task_id=f"seed-{machine_id}",
                        task_type="REPAIR_MACHINE",
                        priority_key="repair_machine",
                        priority=120.0,
                        location=f"Station{machine.station}",
                        payload={"machine_id": machine_id, "station": machine.station},
                        task_code="REPAIR_MACHINE",
                    )
                    for machine_id, machine in sorted(world.machines.items())
                ]
                for task in tasks:
                    task.task_id = world._next_task_id_for_task_code(task.task_code)
                    world._sync_task_instance_id(task)
                    opportunity_id = world._rolling_horizon_opportunity_id(task)
                    world.rolling_horizon_pending[opportunity_id] = {
                        "opportunity_id": opportunity_id,
                        "first_window_index": 0,
                        "first_seen_min": 0.0,
                        "last_seen_min": 5.0,
                        "task_id": task.task_id,
                        "task_code": task.task_code,
                        "priority_key": task.priority_key,
                        "task_type": task.task_type,
                        "location": task.location,
                        "base_priority_rank": 1,
                        "effective_priority_rank": 1,
                        "task_signature": world._task_signature(task),
                        "rolling_task_signature": world._rolling_horizon_task_signature(task),
                        "target_type": world._task_target_type(task),
                        "target_id": world._task_target_id(task),
                        "target_station": world._task_target_station(task),
                        "shareable": False,
                        "capacity": 1,
                        "exclusive_resource_keys": world._rolling_horizon_exclusive_resource_keys(task),
                        "role_policy": "shared_pool",
                        "role_owner_agent_id": "",
                        "allowed_worker_ids": [],
                        "workers": set(world.agents.keys()),
                        "tasks_by_worker": {agent_id: copy.deepcopy(task) for agent_id in world.agents.keys()},
                        "last_logged_window_index": None,
                    }
                world._rolling_horizon_rebuild_pending_resource_index()

                world._rolling_horizon_dispatch_window()

                queued = sum(len(queue) for queue in world.rolling_horizon_dispatch_queues.values())
                self.assertEqual(len(tasks), queued)
                self.assertGreaterEqual(max(len(queue) for queue in world.rolling_horizon_dispatch_queues.values()), 2)
                self.assertEqual(len(tasks), int(world.rolling_horizon_metrics["dispatched_task_count"]))
            finally:
                logger.close()

    def test_unstarted_dispatch_queue_requeues_with_stable_task_id(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                queue_entry = {
                    "window_index": 1,
                    "first_window_index": 0,
                    "first_seen_min": 0.0,
                    "opportunity_id": "RHOPP-STABLETEST",
                    "task_id": "MAT-000777",
                    "task_code": "REPLENISH_MATERIAL",
                    "priority_key": "material_supply",
                    "task_type": "TRANSFER",
                    "location": "Warehouse",
                    "base_priority_rank": 3,
                    "effective_priority_rank": 2,
                    "task_signature": {},
                    "rolling_task_signature": {"target_id": "station1"},
                    "exclusive_resource_keys": ["material_supply_station:1"],
                    "assigned_worker_id": "A1",
                    "assigned_at_min": 5.0,
                }
                world.rolling_horizon_dispatch_queues["A1"].append(queue_entry)

                count = world._rolling_horizon_requeue_unstarted_dispatches(1)

                self.assertEqual(1, count)
                self.assertFalse(world.rolling_horizon_dispatch_queues["A1"])
                self.assertIn("RHOPP-STABLETEST", world.rolling_horizon_pending)
                self.assertEqual("MAT-000777", world.rolling_horizon_pending["RHOPP-STABLETEST"]["task_id"])
                self.assertTrue(any(event["type"] == "ROLLING_HORIZON_TASK_REQUEUED" for event in logger.events))
            finally:
                logger.close()

    def test_low_battery_waits_for_rolling_window(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                agent = world.agents["A1"]
                agent.last_battery_swap = -199.0

                task = world.select_task_for_agent(agent)

                self.assertIsNone(task)
                self.assertTrue(
                    any(entry.get("task_code") == "MANAGE_ROBOT_POWER" for entry in world.rolling_horizon_pending.values())
                )

                world.env.run(until=5.0)
                task = world.select_task_for_agent(agent)

                self.assertIsNotNone(task)
                assert task is not None
                self.assertEqual("BATTERY_SWAP", task.task_type)
                self.assertEqual("rolling_horizon_aging_priority", task.selection_meta.get("decision_source"))
            finally:
                logger.close()

    def test_dispatched_opportunity_is_not_recollected_before_task_reservation(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()

                self.assertIsNone(world.select_task_for_agent(world.agents["A1"]))
                world.env.run(until=5.0)

                selected_tasks = []
                for agent_id in sorted(world.agents.keys()):
                    task = world.select_task_for_agent(world.agents[agent_id])
                    if task is not None:
                        selected_tasks.append(task)
                self.assertTrue(selected_tasks)

                dispatch_index = next(
                    index
                    for index, event in enumerate(logger.events)
                    if event["type"] == "ROLLING_HORIZON_DISPATCH"
                    and str(event.get("entity_id", "")).startswith("RHOPP-")
                )
                opportunity_id = str(logger.events[dispatch_index]["entity_id"])
                recollected_after_dispatch = [
                    event
                    for event in logger.events[dispatch_index + 1 :]
                    if event["type"] == "ROLLING_HORIZON_CANDIDATE_COLLECTED"
                    and str(event.get("entity_id", "")) == opportunity_id
                ]

                self.assertEqual([], recollected_after_dispatch)
            finally:
                logger.close()

    def test_dedicated_roles_filter_candidates_by_worker_task_code(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()

                world._rolling_horizon_collect_candidates()
                workers_by_task = {
                    str(entry.get("task_code")): set(entry.get("workers", set()))
                    for entry in world.rolling_horizon_pending.values()
                }

                self.assertIn("REPLENISH_MATERIAL", workers_by_task)
                self.assertEqual({"A1"}, workers_by_task["REPLENISH_MATERIAL"])
                self.assertNotIn("HANDOVER_ITEM", workers_by_task)
                for entry in world.rolling_horizon_pending.values():
                    allowed = set(entry.get("allowed_worker_ids", []))
                    workers = set(entry.get("workers", set()))
                    self.assertTrue(workers)
                    self.assertTrue(workers.issubset(allowed))
            finally:
                logger.close()

    def test_dedicated_roles_route_low_battery_delivery_to_configured_provider(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world.agents["A2"].last_battery_swap = -199.0
                provider_ids = set(world.rolling_horizon_battery_delivery_provider_agent_ids)

                world._rolling_horizon_collect_candidates()
                battery_delivery = [
                    entry
                    for entry in world.rolling_horizon_pending.values()
                    if entry.get("priority_key") in {"battery_delivery_low_battery", "battery_delivery_discharged"}
                ]

                self.assertTrue(battery_delivery)
                for entry in battery_delivery:
                    self.assertEqual(provider_ids, set(entry.get("workers", set())))
                    if len(provider_ids) == 1:
                        self.assertEqual(next(iter(provider_ids)), entry.get("role_owner_agent_id"))
            finally:
                logger.close()

    def test_dedicated_roles_self_battery_swap_preempts_aged_delivery(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                provider_id = world.rolling_horizon_battery_delivery_provider_agent_ids[0]
                receiver_id = world.rolling_horizon_battery_delivery_receiver_agent_ids[0]
                provider = world.agents[provider_id]
                receiver = world.agents[receiver_id]

                receiver.last_battery_swap = -199.0
                world._rolling_horizon_collect_candidates()
                self.assertTrue(
                    any(
                        entry.get("priority_key") in {"battery_delivery_low_battery", "battery_delivery_discharged"}
                        and provider_id in set(entry.get("workers", set()))
                        for entry in world.rolling_horizon_pending.values()
                    )
                )

                provider.last_battery_swap = -199.0
                world._rolling_horizon_collect_candidates()
                self.assertTrue(
                    any(
                        entry.get("task_code") == "MANAGE_ROBOT_POWER"
                        and entry.get("priority_key") == "battery_swap"
                        and provider_id in set(entry.get("workers", set()))
                        for entry in world.rolling_horizon_pending.values()
                    )
                )

                world.env.run(until=5.0)
                world._rolling_horizon_update()
                queue = list(world.rolling_horizon_dispatch_queues[provider_id])

                self.assertTrue(queue)
                self.assertEqual("battery_swap", queue[0].get("priority_key"))
                self.assertEqual("MANAGE_ROBOT_POWER", queue[0].get("task_code"))
            finally:
                logger.close()

    def test_dedicated_roles_make_repair_non_shareable(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                task = Task(
                    task_id="repair",
                    task_type="REPAIR_MACHINE",
                    priority_key="repair_machine",
                    priority=100.0,
                    location="Station1",
                    payload={"machine_id": "S1M1", "station": 1},
                    task_code="REPAIR_MACHINE",
                )

                self.assertFalse(world._task_shareable(task))
                self.assertEqual(1, world._task_capacity(task))
            finally:
                logger.close()


if __name__ == "__main__":
    unittest.main()
