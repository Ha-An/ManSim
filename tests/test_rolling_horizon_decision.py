from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
import copy
import tempfile
import unittest
from unittest.mock import patch

import simpy
import yaml

from agents.factory import build_decision_module
from agents.modes import (
    format_decision_mode_label,
    is_fixed_priority_mode,
    is_rolling_horizon_mode,
    normalize_decision_mode,
)
from manufacturing_sim.simulation.scenarios.manufacturing.entities import ItemState, MachineState, Task
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.throughput_policy import ThroughputOptimizerUnavailable
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld
from manufacturing_sim.simulation.rolling_horizon import strict_periodic_rolling_horizon_loop


def _load_cfg(decision_name: str = "rolling_horizon_aging_priority") -> dict:
    root = Path(__file__).resolve().parents[1]
    cfg = yaml.safe_load((root / "configs" / "scenario" / "factory_mfg_basic.yaml").read_text(encoding="utf-8"))
    cfg["task_primitive_timing"] = yaml.safe_load(
        (root / "configs" / "task_primitive_timing" / "factory_mfg_basic.yaml").read_text(encoding="utf-8")
    )
    cfg["decision"] = yaml.safe_load(
        (root / "configs" / "decision" / f"{decision_name}.yaml").read_text(encoding="utf-8")
    )
    cfg["heuristic_rules"] = yaml.safe_load((root / "configs" / "heuristic_rules" / "default.yaml").read_text(encoding="utf-8"))
    cfg["humanoidsim"] = yaml.safe_load((root / "configs" / "humanoidsim" / "default.yaml").read_text(encoding="utf-8"))
    cfg["horizon"]["num_days"] = 1
    cfg["horizon"]["minutes_per_day"] = 60
    return cfg


class RollingHorizonDecisionTests(unittest.TestCase):
    def test_mode_registry_recognizes_four_mfg_flow_shop_representatives(self) -> None:
        expected = {
            "immediate_shared": ("Immediate Shared", False),
            "immediate_dedicated_roles": ("Immediate Dedicated Roles", False),
            "rolling_horizon_shared": ("Rolling Horizon Shared", True),
            "rolling_horizon_dedicated_roles": ("Rolling Horizon Dedicated Roles", True),
        }
        for mode, (label, rolling) in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(mode, normalize_decision_mode(mode))
                self.assertTrue(is_fixed_priority_mode(mode))
                self.assertEqual(rolling, is_rolling_horizon_mode(mode))
                self.assertEqual(label, format_decision_mode_label(mode))
                module = build_decision_module(
                    experiment_cfg={"decision": {"mode": mode}},
                    decision_mode=mode,
                )
                self.assertEqual(mode, module.decision_mode)
                self.assertTrue(module.static_priority_policy)

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

    def test_mode_registry_recognizes_throughput_policy_modes(self) -> None:
        self.assertEqual("bottleneck_aware_dispatch", normalize_decision_mode("bottleneck_aware_dispatch"))
        self.assertTrue(is_fixed_priority_mode("bottleneck_aware_dispatch"))
        self.assertEqual("Bottleneck-Aware Dispatch", format_decision_mode_label("bottleneck_aware_dispatch"))
        bottleneck_module = build_decision_module(
            experiment_cfg={"decision": {"mode": "bottleneck_aware_dispatch"}},
            decision_mode="bottleneck_aware_dispatch",
        )
        self.assertEqual("bottleneck_aware_dispatch", bottleneck_module.decision_mode)

        self.assertEqual("rolling_horizon_throughput_optimizer", normalize_decision_mode("rolling_horizon_throughput_optimizer"))
        self.assertTrue(is_fixed_priority_mode("rolling_horizon_throughput_optimizer"))
        self.assertEqual("Rolling Horizon Throughput Optimizer", format_decision_mode_label("rolling_horizon_throughput_optimizer"))
        optimizer_module = build_decision_module(
            experiment_cfg={"decision": {"mode": "rolling_horizon_throughput_optimizer"}},
            decision_mode="rolling_horizon_throughput_optimizer",
        )
        self.assertEqual("rolling_horizon_throughput_optimizer", optimizer_module.decision_mode)
        self.assertTrue(optimizer_module.static_priority_policy)

    def test_throughput_optimizer_requires_ortools_at_world_start(self) -> None:
        cfg = _load_cfg("rolling_horizon_throughput_optimizer")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                with patch(
                    "manufacturing_sim.simulation.scenarios.manufacturing.world.require_cp_sat",
                    side_effect=ThroughputOptimizerUnavailable("missing ortools"),
                ):
                    with self.assertRaises(ThroughputOptimizerUnavailable):
                        ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
            finally:
                logger.close()

    def test_throughput_optimizer_defaults_to_deterministic_solver_settings(self) -> None:
        cfg = _load_cfg("rolling_horizon_throughput_optimizer")
        self.assertEqual(1, cfg["decision"]["rolling_horizon"]["optimizer"]["num_search_workers"])
        self.assertIsNone(cfg["decision"]["rolling_horizon"]["optimizer"]["random_seed"])
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                self.assertEqual(int(cfg.get("seed", 7)), world.seed)
            finally:
                logger.close()

    def test_legacy_scheduler_fields_are_tolerated_but_runtime_is_strict(self) -> None:
        cfg = _load_cfg()
        cfg["decision"]["rolling_horizon"]["scheduler_mode"] = "worker_triggered"
        cfg["decision"]["rolling_horizon"]["candidate_collection_mode"] = "polling"
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                self.assertEqual("worker_triggered", world.rolling_horizon_configured_scheduler_mode)
                self.assertEqual("polling", world.rolling_horizon_configured_candidate_collection_mode)
                self.assertEqual("strict_periodic", world.rolling_horizon_scheduler_mode)
                self.assertEqual(
                    "event_with_boundary_reconciliation",
                    world.rolling_horizon_candidate_collection_mode,
                )
            finally:
                logger.close()

    def test_bottleneck_score_prefers_broken_machine_repair(self) -> None:
        cfg = _load_cfg("bottleneck_aware_dispatch")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                machine = world.machines["S1M1"]
                machine.broken = True
                machine.state = MachineState.BROKEN
                repair = Task(
                    task_id="RM-test",
                    task_type="REPAIR_MACHINE",
                    priority_key="repair_machine",
                    priority=120.0,
                    location="Station1",
                    payload={"machine_id": machine.machine_id, "station": machine.station},
                    task_code="REPAIR_MACHINE",
                )
                transfer = Task(
                    task_id="TR-test",
                    task_type="TRANSFER",
                    priority_key="inter_station_transfer",
                    priority=90.0,
                    location="Station1",
                    payload={"transfer_kind": "inter_station", "from_station": 1},
                    task_code="TRANSFER",
                )
                agent = world.agents["A1"]
                self.assertGreater(world._throughput_score(agent, repair), world._throughput_score(agent, transfer))
            finally:
                logger.close()

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

                world.env.process(strict_periodic_rolling_horizon_loop(world.env, world))
                world.env.run(until=0.000001)

                self.assertIsNone(world.select_task_for_agent(world.agents["A1"]))
                self.assertGreater(len(world.rolling_horizon_pending), 0)
                self.assertTrue(any(event["type"] == "ROLLING_HORIZON_CANDIDATE_COLLECTED" for event in logger.events))

                world.env.run(until=5.000001)
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

    def test_strict_coordinator_dispatches_exactly_without_worker_polling(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(reason="initial_fill", target_fill=world.material_shelf_initial_fill)
                for station in world.stations:
                    world.material_queues[station].clear()
                for agent in world.agents.values():
                    agent.current_task_id = "BUSY-UNTIL-8"
                    agent.humanoid_state["availability"] = "EXECUTING"

                env.process(strict_periodic_rolling_horizon_loop(env, world))
                env.run(until=15.000001)

                periodic_dispatches = [
                    event
                    for event in logger.events
                    if event["type"] == "ROLLING_HORIZON_DISPATCH"
                    and event["details"].get("scheduled_boundary_min") is not None
                ]
                boundary_times = sorted({float(event["details"]["scheduled_boundary_min"]) for event in periodic_dispatches})
                self.assertEqual([5.0, 10.0, 15.0], boundary_times)
                self.assertTrue(all(float(event["details"]["boundary_lag_min"]) == 0.0 for event in periodic_dispatches))
                self.assertEqual(3, world.rolling_horizon_metrics["strict_boundary_count"])
                self.assertEqual(0, world.rolling_horizon_metrics["late_boundary_count"])
            finally:
                logger.close()

    def test_boundary_observes_normal_priority_machine_failure_at_same_time(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=4))
                machine = world.machines["S1M1"]

                def break_at_boundary():
                    yield env.timeout(5.0)
                    world.break_machine(machine, reason="same_boundary_test")

                env.process(strict_periodic_rolling_horizon_loop(env, world))
                env.process(break_at_boundary())
                env.run(until=5.000001)

                broken_event = next(event for event in logger.events if event["type"] == "MACHINE_BROKEN")
                repair_dispatch = next(
                    event
                    for event in logger.events
                    if event["type"] == "ROLLING_HORIZON_DISPATCH"
                    and event["details"].get("task_code") == "REPAIR_MACHINE"
                    and event["details"].get("target_id") == machine.machine_id
                )
                self.assertEqual(5.0, broken_event["t"])
                self.assertEqual(5.0, repair_dispatch["t"])
                self.assertEqual(5.0, repair_dispatch["details"].get("scheduled_boundary_min"))
                self.assertEqual(0.0, repair_dispatch["details"].get("boundary_lag_min"))
                self.assertFalse(repair_dispatch["details"].get("urgent_dispatch", False))
            finally:
                logger.close()

    def test_non_available_worker_drains_battery_twice_as_fast(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=4))
                agent = world.agents["A1"]

                self.assertAlmostEqual(world.battery_swap_period_min, world.battery_remaining(agent), places=3)
                env.run(until=10.0)
                available_drop = 10.0 * world.battery_available_rate_multiplier
                self.assertAlmostEqual(world.battery_swap_period_min - available_drop, world.battery_remaining(agent), places=3)

                agent.humanoid_state["availability"] = "EXECUTING"
                env.run(until=20.0)
                non_available_drop = 10.0 * world.battery_non_available_rate_multiplier
                self.assertAlmostEqual(
                    world.battery_swap_period_min - available_drop - non_available_drop,
                    world.battery_remaining(agent),
                    places=3,
                )
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

    def test_low_battery_bypasses_rolling_window_without_preemption(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                agent = world.agents["A1"]
                world._ensure_battery_accounting(agent, 0.0)
                agent.battery_remaining_budget_min = world._battery_mandatory_threshold(agent) - 1.0
                agent.battery_last_accounted_at = 0.0

                world._emit_low_battery_alert_if_needed(agent)
                self.assertTrue(world.rolling_horizon_dispatch_queues[agent.agent_id])
                task = world.select_task_for_agent(agent)

                self.assertIsNotNone(task)
                assert task is not None
                self.assertEqual("BATTERY_SWAP", task.task_type)
                self.assertEqual("rolling_horizon_aging_priority", task.selection_meta.get("decision_source"))
                self.assertTrue(task.selection_meta.get("urgent_dispatch"))
            finally:
                logger.close()

    def test_low_battery_event_queues_urgent_battery_task_next(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                provider = world.agents["A3"]
                world.rolling_horizon_dispatch_queues["A3"].append(
                    {
                        "opportunity_id": "RHOPP-NORMAL",
                        "task_id": "TR-000001",
                        "task_code": "TRANSFER",
                        "priority_key": "inter_station",
                        "task_type": "TRANSFER",
                        "exclusive_resource_keys": [],
                        "assigned_worker_id": "A3",
                    }
                )
                world._ensure_battery_accounting(provider, 0.0)
                provider.battery_remaining_budget_min = world.battery_swap_period_min * 0.25
                provider.battery_last_accounted_at = 0.0

                world._emit_low_battery_alert_if_needed(provider)

                queue = list(world.rolling_horizon_dispatch_queues["A3"])
                self.assertGreaterEqual(len(queue), 2)
                self.assertEqual("MANAGE_ROBOT_POWER", queue[0].get("task_code"))
                self.assertTrue(queue[0].get("urgent_dispatch"))
                self.assertEqual("worker_low_battery", queue[0].get("collection_trigger"))
                self.assertEqual("TRANSFER", queue[1].get("task_code"))
                event = next(
                    row
                    for row in logger.events
                    if row["type"] == "ROLLING_HORIZON_CANDIDATE_COLLECTED"
                    and row["details"].get("task_code") == "MANAGE_ROBOT_POWER"
                )
                self.assertEqual("worker_low_battery", event["details"].get("collection_trigger"))
                self.assertTrue(event["details"].get("immediate_trigger"))
                dispatch_event = next(
                    row
                    for row in logger.events
                    if row["type"] == "ROLLING_HORIZON_DISPATCH"
                    and row["details"].get("task_code") == "MANAGE_ROBOT_POWER"
                )
                self.assertTrue(dispatch_event["details"].get("urgent_dispatch"))
            finally:
                logger.close()

    def test_machine_broken_event_waits_for_next_periodic_boundary(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                machine = world.machines["S1M1"]
                world.env.process(strict_periodic_rolling_horizon_loop(world.env, world))
                world.env.run(until=0.000001)

                world.break_machine(machine, reason="unit_test")
                world.env.run(until=0.000002)

                self.assertFalse(
                    any(
                        entry.get("task_code") == "REPAIR_MACHINE"
                        for queue in world.rolling_horizon_dispatch_queues.values()
                        for entry in queue
                    )
                )
                pending_repairs = [
                    entry
                    for entry in world.rolling_horizon_pending.values()
                    if entry.get("task_code") == "REPAIR_MACHINE"
                    and entry.get("target_id") == machine.machine_id
                ]
                self.assertEqual(1, len(pending_repairs))
                self.assertFalse(pending_repairs[0].get("immediate_trigger", False))

                world.env.run(until=5.000001)
                repair_entries = [
                    entry
                    for entry in world.rolling_horizon_dispatch_queues["A2"]
                    if entry.get("task_code") == "REPAIR_MACHINE"
                    and entry.get("target_id") == machine.machine_id
                ]
                self.assertEqual(1, len(repair_entries))
                self.assertFalse(repair_entries[0].get("urgent_dispatch", False))
                dispatch_event = next(
                    row
                    for row in logger.events
                    if row["type"] == "ROLLING_HORIZON_DISPATCH"
                    and row["details"].get("task_code") == "REPAIR_MACHINE"
                )
                self.assertEqual(5.0, dispatch_event["details"].get("scheduled_boundary_min"))
                self.assertEqual(0.0, dispatch_event["details"].get("boundary_lag_min"))
            finally:
                logger.close()

    def test_machine_break_invalidates_unstarted_work_before_periodic_repair(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                machine = world.machines["S1M1"]
                machine_key = f"machine:{machine.machine_id}"
                pending_entry = {
                    "opportunity_id": "RHOPP-PENDING-SETUP",
                    "task_id": "SET-TEST-PENDING",
                    "task_code": "SETUP_MACHINE",
                    "task_type": "SETUP_MACHINE",
                    "priority_key": "setup_machine",
                    "location": "Station1",
                    "target_type": "machine",
                    "target_id": machine.machine_id,
                    "rolling_task_signature": {
                        "task_code": "SETUP_MACHINE",
                        "task_type": "SETUP_MACHINE",
                        "target_type": "machine",
                        "target_id": machine.machine_id,
                        "machine_id": machine.machine_id,
                    },
                    "exclusive_resource_keys": [machine_key],
                    "workers": {"A2"},
                }
                queued_entry = {
                    **pending_entry,
                    "opportunity_id": "RHOPP-QUEUED-LOAD",
                    "task_id": "LOAD-TEST-QUEUED",
                    "task_code": "LOAD_MACHINE",
                    "task_type": "LOAD_MACHINE",
                    "priority_key": "load_machine",
                    "rolling_task_signature": {
                        **pending_entry["rolling_task_signature"],
                        "task_code": "LOAD_MACHINE",
                        "task_type": "LOAD_MACHINE",
                    },
                    "assigned_worker_id": "A2",
                }
                queued_entry.pop("workers", None)
                world.rolling_horizon_pending[pending_entry["opportunity_id"]] = pending_entry
                world.rolling_horizon_pending_resource_index[machine_key] = pending_entry["opportunity_id"]
                world.rolling_horizon_dispatch_queues["A2"].append(queued_entry)

                active_worker = world.agents["A1"]
                active_worker.current_task_id = "SET-ACTIVE"
                active_worker.current_task_type = "SETUP_MACHINE"
                active_worker.current_task_code = "SETUP_MACHINE"
                active_worker.current_task_payload = {"machine_id": machine.machine_id, "station": machine.station}

                world.env.process(strict_periodic_rolling_horizon_loop(world.env, world))
                world.env.run(until=0.000001)
                world.break_machine(machine, reason="unit_test_with_existing_work")
                world.env.run(until=0.000002)

                self.assertNotIn(pending_entry["opportunity_id"], world.rolling_horizon_pending)
                self.assertFalse(
                    any(entry.get("task_code") != "REPAIR_MACHINE" for entry in world.rolling_horizon_dispatch_queues["A2"])
                )
                self.assertFalse(world.rolling_horizon_dispatch_queues["A2"])
                self.assertTrue(
                    any(
                        entry.get("task_code") == "REPAIR_MACHINE"
                        and entry.get("target_id") == machine.machine_id
                        for entry in world.rolling_horizon_pending.values()
                    )
                )

                world.env.run(until=5.000001)
                repair_entries = [
                    entry
                    for entry in world.rolling_horizon_dispatch_queues["A2"]
                    if entry.get("task_code") == "REPAIR_MACHINE"
                    and entry.get("target_id") == machine.machine_id
                ]
                self.assertEqual(1, len(repair_entries))
                self.assertFalse(repair_entries[0].get("urgent_dispatch", False))
                invalidated = [
                    event
                    for event in logger.events
                    if event["type"] == "ROLLING_HORIZON_TASK_SKIPPED"
                    and event["details"].get("reason") == "machine_broken_invalidated"
                ]
                self.assertGreaterEqual(len(invalidated), 2)
                broken_t = next(event["t"] for event in logger.events if event["type"] == "MACHINE_BROKEN")
                dispatch_t = next(
                    event["t"]
                    for event in logger.events
                    if event["type"] == "ROLLING_HORIZON_DISPATCH"
                    and event["details"].get("task_code") == "REPAIR_MACHINE"
                )
                self.assertLess(broken_t, dispatch_t)
                self.assertEqual(5.0, dispatch_t)
            finally:
                logger.close()

    def test_urgent_dispatch_survives_window_requeue_until_started(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                provider = world.agents["A3"]
                world._ensure_battery_accounting(provider, 0.0)
                provider.battery_remaining_budget_min = world.battery_swap_period_min * 0.25
                provider.battery_last_accounted_at = 0.0
                world._emit_low_battery_alert_if_needed(provider)

                self.assertTrue(world.rolling_horizon_dispatch_queues["A3"])
                urgent_id = world.rolling_horizon_dispatch_queues["A3"][0]["opportunity_id"]
                requeued = world._rolling_horizon_requeue_unstarted_dispatches(1)

                self.assertEqual(0, requeued)
                self.assertTrue(world.rolling_horizon_dispatch_queues["A3"])
                self.assertEqual(urgent_id, world.rolling_horizon_dispatch_queues["A3"][0]["opportunity_id"])
                self.assertNotIn(urgent_id, world.rolling_horizon_pending)
            finally:
                logger.close()

    def test_stale_urgent_battery_dispatch_is_removed_at_boundary(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                provider = world.agents["A3"]
                world._ensure_battery_accounting(provider, 0.0)
                provider.battery_remaining_budget_min = world.battery_swap_period_min * 0.25
                provider.battery_last_accounted_at = 0.0
                world._emit_low_battery_alert_if_needed(provider)
                self.assertTrue(world.rolling_horizon_dispatch_queues["A3"])

                provider.battery_remaining_budget_min = world.battery_swap_period_min
                provider.battery_last_accounted_at = float(world.env.now)
                requeued = world._rolling_horizon_requeue_unstarted_dispatches(1)

                self.assertEqual(0, requeued)
                self.assertFalse(world.rolling_horizon_dispatch_queues["A3"])
                self.assertTrue(
                    any(
                        event["type"] == "ROLLING_HORIZON_TASK_SKIPPED"
                        and event["details"].get("reason") == "urgent_condition_no_longer_active"
                        for event in logger.events
                    )
                )
            finally:
                logger.close()

    def test_battery_delivery_opportunity_id_ignores_moving_receiver_location(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                first = Task(
                    task_id="TR-A",
                    task_type="TRANSFER",
                    priority_key="battery_delivery_low_battery",
                    priority=140.0,
                    location="Station1->Warehouse(37%)",
                    payload={"transfer_kind": "battery_delivery", "target_agent_id": "A1"},
                    task_code="TRANSFER",
                    assigned_robot_id="A3",
                )
                second = copy.deepcopy(first)
                second.task_id = "TR-B"
                second.location = "Warehouse->Station2(79%)"

                self.assertEqual(
                    world._rolling_horizon_opportunity_id(first),
                    world._rolling_horizon_opportunity_id(second),
                )
                self.assertEqual("", world._rolling_horizon_task_signature(first)["location"])
            finally:
                logger.close()

    def test_move_tile_events_refresh_battery_snapshot(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=4))
                agent = world.agents["A3"]
                start_battery = world.battery_remaining(agent)

                env.process(world.move_agent(agent, "Warehouse"))
                env.run(until=1.0)

                tile_events = [
                    event
                    for event in logger.events
                    if event["type"] in {"AGENT_MOVE_TILE_START", "AGENT_MOVE_TILE_END"}
                    and event["entity_id"] == "A3"
                ]
                self.assertGreaterEqual(len(tile_events), 2)
                observed = [
                    float(event["details"]["humanoid_state"]["metadata"]["battery_remaining_min"])
                    for event in tile_events
                ]
                self.assertLess(min(observed), start_battery)
                self.assertGreater(len({round(value, 3) for value in observed}), 1)
            finally:
                logger.close()

    def test_snapshot_loop_refreshes_available_idle_battery_snapshot(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=4))
                world.bootstrap()

                env.run(until=2.1)

                observations = [
                    event
                    for event in logger.events
                    if event["type"] == "WORKER_STATE_CHANGED"
                    and event["entity_id"] == "A1"
                    and event["details"].get("observation_reason") == "snapshot_tick"
                ]
                self.assertGreaterEqual(len(observations), 2)
                self.assertEqual(
                    "AVAILABLE",
                    str(observations[0]["details"]["humanoid_state"].get("availability", "")).upper(),
                )
                battery_values = [
                    float(event["details"]["humanoid_state"]["metadata"]["battery_remaining_min"])
                    for event in observations
                ]
                self.assertLess(battery_values[-1], battery_values[0])
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

                world.env.process(strict_periodic_rolling_horizon_loop(world.env, world))
                world.env.run(until=5.000001)

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

    def test_active_load_machine_item_blocks_second_load_candidate(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                for station in world.stations:
                    world.material_queues[station].clear()
                    if station in world.intermediate_queues:
                        world.intermediate_queues[station].clear()
                world.material_queues[1].append("MAT-WH-1")

                station_one_machines = sorted(
                    [machine for machine in world.machines.values() if machine.station == 1],
                    key=lambda machine: machine.machine_id,
                )
                self.assertGreaterEqual(len(station_one_machines), 2)
                active_machine, open_machine = station_one_machines[:2]
                for machine in station_one_machines:
                    machine.broken = False
                    machine.state = MachineState.WAIT_INPUT
                    machine.input_material = None
                    machine.input_intermediate = None
                    machine.output_intermediate = None
                    machine.setup_owner = None

                active_machine.setup_owner = "A2"
                active_agent = world.agents["A2"]
                active_agent.current_task_id = "LOAD-000260"
                active_agent.current_task_type = "LOAD_MACHINE"
                active_agent.current_task_code = "LOAD_MACHINE"
                active_agent.current_task_payload = {
                    "machine_id": active_machine.machine_id,
                    "station": 1,
                    "load_slot": "material",
                    "item_type": "material",
                    "item_id": "MAT-WH-1",
                    "material_id": "MAT-WH-1",
                    "source": "material_queue_1",
                    "_reserved_item_ids": ["MAT-WH-1"],
                }
                world.item_reservations["MAT-WH-1"] = {
                    "item_id": "MAT-WH-1",
                    "agent_id": "A2",
                    "task_id": "LOAD-000260",
                    "task_type": "LOAD_MACHINE",
                    "task_code": "LOAD_MACHINE",
                    "source": "material_queue_1",
                    "ref": "1",
                    "item_type": "material",
                    "reserved_at": float(world.env.now),
                }

                for agent_id in ("A1", "A2"):
                    candidates = world._candidate_tasks(world.agents[agent_id])
                    duplicate_loads = [
                        task
                        for task in candidates
                        if task.task_type == "LOAD_MACHINE"
                        and task.payload.get("machine_id") == open_machine.machine_id
                        and task.payload.get("item_id") == "MAT-WH-1"
                    ]
                    self.assertEqual([], duplicate_loads)
            finally:
                logger.close()

    def test_rolling_horizon_queued_load_runs_when_original_resources_still_valid(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                for station in world.stations:
                    world.material_queues[station].clear()
                    if station in world.intermediate_queues:
                        world.intermediate_queues[station].clear()
                world.material_queues[1].append("MAT-WH-3")
                machine = sorted(
                    [candidate for candidate in world.machines.values() if candidate.station == 1],
                    key=lambda item: item.machine_id,
                )[1]
                machine.broken = False
                machine.state = MachineState.WAIT_INPUT
                machine.input_material = None
                machine.input_intermediate = None
                machine.output_intermediate = None
                machine.setup_owner = None

                world.rolling_horizon_dispatch_queues["A2"] = deque(
                    [
                        {
                            "opportunity_id": "RHOPP-queued-load",
                            "task_id": "LOAD-000594",
                            "task_code": "LOAD_MACHINE",
                            "task_type": "LOAD_MACHINE",
                            "priority_key": "load_machine",
                            "priority": 105.0,
                            "location": "Station1",
                            "effective_priority_rank": 1,
                            "rolling_task_signature": {
                                "task_code": "LOAD_MACHINE",
                                "task_type": "LOAD_MACHINE",
                                "target_type": "machine",
                                "target_id": machine.machine_id,
                                "target_station": 1,
                                "location": "Station1",
                                "load_slot": "material",
                                "machine_id": machine.machine_id,
                                "item_id": "MAT-WH-3",
                                "source": "material_queue_1",
                            },
                        }
                    ]
                )

                selected = world._select_rolling_horizon_task(world.agents["A2"], [])

                self.assertIsNotNone(selected)
                assert selected is not None
                self.assertEqual("LOAD-000594", selected.task_id)
                self.assertEqual(machine.machine_id, selected.payload["machine_id"])
                self.assertEqual("MAT-WH-3", selected.payload["item_id"])
                self.assertEqual("rolling_horizon_dispatch_reconstructed", selected.selection_meta.get("fallback_reason"))
                self.assertFalse(any(event["type"] == "ROLLING_HORIZON_TASK_SKIPPED" for event in logger.events))
            finally:
                logger.close()

    def test_machine_abort_retains_wip_sample_and_remaining_progress(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                machine = next(iter(world.machines.values()))
                machine.input_material = "MAT-WIP-1"
                machine.input_intermediate = None
                machine.setup_ready = True
                world._set_item_state("MAT-WIP-1", ItemState.PROCESSING, location=f"Station{machine.station}", ref=machine.machine_id, item_type="material")
                cycle_id = world.start_machine_cycle(machine)
                sampled_process_min = machine.cycle_sampled_process_min
                distribution = world.processing_time_distribution[machine.station]
                self.assertGreaterEqual(sampled_process_min, distribution.minimum)
                self.assertLessEqual(sampled_process_min, distribution.maximum)
                machine.broken = True

                world.abort_machine_cycle(machine, cycle_id, "machine_breakdown", elapsed_min=3.5)

                self.assertEqual("MAT-WIP-1", machine.input_material)
                self.assertTrue(machine.setup_ready)
                self.assertAlmostEqual(sampled_process_min - 3.5, machine.cycle_remaining_process_min)
                self.assertEqual(ItemState.LOADED_ON_MACHINE, world.items["MAT-WIP-1"].state)
                aborted = [event for event in logger.events if event["type"] == "MACHINE_ABORTED"]
                self.assertEqual("resume_after_repair", aborted[-1]["details"]["progress_policy"])
                self.assertEqual("MAT-WIP-1", aborted[-1]["details"]["retained_input_material"])
                machine.broken = False
                self.assertEqual(cycle_id, world.start_machine_cycle(machine))
                self.assertAlmostEqual(sampled_process_min, machine.cycle_sampled_process_min)
                self.assertAlmostEqual(sampled_process_min - 3.5, machine.cycle_remaining_process_min)
            finally:
                logger.close()

    def test_active_repair_machine_is_observed_as_under_repair(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                machine = world.machines["S2M2"]
                machine.state = MachineState.BROKEN
                machine.broken = True
                machine.repair_team = ["A2"]
                machine.repair_work_remaining_min = 20.0

                self.assertEqual(MachineState.UNDER_REPAIR, world._observed_machine_state(machine))
                self.assertEqual("under_repair", world._machine_state_bucket(machine))
                observation = world._machine_observation()["by_id"]["S2M2"]
                self.assertEqual("UNDER_REPAIR", observation["state"])

                world.capture_snapshot()

                self.assertEqual("UNDER_REPAIR", world.minute_snapshots[-1]["machine_states"]["S2M2"])
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
                receiver = world.agents["A2"]
                world._ensure_battery_accounting(receiver, 0.0)
                receiver.battery_remaining_budget_min = world._battery_delivery_trigger_threshold(receiver) - 1.0
                receiver.battery_last_accounted_at = 0.0
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

                world._ensure_battery_accounting(receiver, 0.0)
                receiver.battery_remaining_budget_min = world._battery_delivery_trigger_threshold(receiver) - 1.0
                receiver.battery_last_accounted_at = 0.0
                world._rolling_horizon_collect_candidates()
                self.assertTrue(
                    any(
                        entry.get("priority_key") in {"battery_delivery_low_battery", "battery_delivery_discharged"}
                        and provider_id in set(entry.get("workers", set()))
                        for entry in world.rolling_horizon_pending.values()
                    )
                )

                world._ensure_battery_accounting(provider, 0.0)
                provider.battery_remaining_budget_min = world._battery_mandatory_threshold(provider) - 1.0
                provider.battery_last_accounted_at = 0.0
                world._rolling_horizon_collect_candidates()
                self.assertTrue(
                    any(
                        entry.get("task_code") == "MANAGE_ROBOT_POWER"
                        and entry.get("priority_key") == "battery_swap"
                        and provider_id in set(entry.get("workers", set()))
                        for entry in world.rolling_horizon_pending.values()
                    )
                )

                world.env.process(strict_periodic_rolling_horizon_loop(world.env, world))
                world.env.run(until=5.000001)
                queue = list(world.rolling_horizon_dispatch_queues[provider_id])

                self.assertTrue(queue)
                self.assertEqual("battery_swap", queue[0].get("priority_key"))
                self.assertEqual("MANAGE_ROBOT_POWER", queue[0].get("task_code"))
            finally:
                logger.close()

    def test_dedicated_role_battery_receiver_keeps_production_candidates_while_low(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                receiver = world.agents["A1"]
                receiver.last_battery_swap = -199.0
                production_task = Task(
                    task_id="MAT-TEST",
                    task_type="TRANSFER",
                    priority_key="material_supply",
                    priority=85.0,
                    location="Warehouse",
                    payload={"transfer_kind": "material_supply", "station": 1},
                    task_code="REPLENISH_MATERIAL",
                    instance_id="MAT-TEST",
                    assigned_robot_id="A1",
                )

                filtered = world._filter_candidates_for_agent(receiver, [production_task])

                self.assertEqual([production_task], filtered)
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

    def test_dedicated_roles_collect_repair_pool_even_when_repair_owner_disabled(self) -> None:
        cfg = _load_cfg("rolling_horizon_dedicated_roles")
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=4))
                repair_owner = world.agents["A2"]
                repair_owner.discharged = True
                for machine_id in ("S1M2", "S2M1"):
                    machine = world.machines[machine_id]
                    machine.broken = True
                    machine.repair_work_remaining_min = 20.0

                world._rolling_horizon_collect_candidates()

                repair_entries = [
                    entry
                    for entry in world.rolling_horizon_pending.values()
                    if entry.get("task_code") == "REPAIR_MACHINE"
                ]
                repaired_machine_ids = {
                    str(entry.get("rolling_task_signature", {}).get("machine_id"))
                    for entry in repair_entries
                }
                self.assertTrue({"S1M2", "S2M1"}.issubset(repaired_machine_ids))
                for entry in repair_entries:
                    if str(entry.get("rolling_task_signature", {}).get("machine_id")) in {"S1M2", "S2M1"}:
                        self.assertEqual({"A2"}, set(entry.get("workers", set())))
                        self.assertEqual({"A2"}, set(entry.get("allowed_worker_ids", [])))
            finally:
                logger.close()


if __name__ == "__main__":
    unittest.main()
