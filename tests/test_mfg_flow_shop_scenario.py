from __future__ import annotations

from pathlib import Path
from statistics import mean, pstdev
from types import SimpleNamespace
import tempfile
import unittest

import simpy
import yaml

from agents.base import JobPlan, StrategyState, default_task_priority_weights
from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import TileGridMap
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.entities import Item, ItemState, MachineState, Task
from manufacturing_sim.simulation.scenarios.manufacturing.processes import machine_lifecycle
from manufacturing_sim.simulation.scenarios.manufacturing.task_rules import MfgFlowShopTaskPolicy
from manufacturing_sim.simulation.scenarios.manufacturing.world import (
    MFG_FLOW_SHOP_TASK_CODES,
    ManufacturingWorld,
    resolve_manufacturing_objective,
)
from manufacturing_sim.simulation.scenarios.registry import scenario_type
from manufacturing_sim.simulation.rolling_horizon import strict_periodic_rolling_horizon_loop


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_ROLE_DEFINITIONS = [
    (1, "replenish_s1_material", "REPLENISH_MATERIAL (to Station 1)", "REPLENISH_MATERIAL", "exclusive"),
    (2, "replenish_s2_material", "REPLENISH_MATERIAL (to Station 2)", "REPLENISH_MATERIAL", "exclusive"),
    (3, "transfer_s1_to_s2", "TRANSFER (Station 1 to Station 2)", "TRANSFER", "exclusive"),
    (4, "transfer_s2_to_inspection", "TRANSFER (Station 2 to Inspection)", "TRANSFER", "exclusive"),
    (5, "transfer_inspection_to_completed", "TRANSFER (Inspection to CompletedProducts)", "TRANSFER", "exclusive"),
    (6, "dispose_inspection_scrap", "COLLECT_WASTE_OR_SCRAP", "COLLECT_WASTE_OR_SCRAP", "exclusive"),
    (7, "load_s1_material", "LOAD_MACHINE (Station 1, material)", "LOAD_MACHINE", "exclusive"),
    (8, "load_s2_material", "LOAD_MACHINE (Station 2, material)", "LOAD_MACHINE", "exclusive"),
    (9, "load_s2_intermediate", "LOAD_MACHINE (Station 2, intermediate)", "LOAD_MACHINE", "exclusive"),
    (10, "setup_s1", "SETUP_MACHINE (Station 1)", "SETUP_MACHINE", "exclusive"),
    (11, "setup_s2", "SETUP_MACHINE (Station 2)", "SETUP_MACHINE", "exclusive"),
    (12, "unload_s1", "UNLOAD_MACHINE (Station 1)", "UNLOAD_MACHINE", "exclusive"),
    (13, "unload_s2", "UNLOAD_MACHINE (Station 2)", "UNLOAD_MACHINE", "exclusive"),
    (
        14,
        "load_inspection_desk",
        "LOAD_UNLOAD_TRANSFER_INTERFACE (load inspection desk)",
        "LOAD_UNLOAD_TRANSFER_INTERFACE",
        "exclusive",
    ),
    (15, "inspect_product", "INSPECT_PRODUCT", "INSPECT_PRODUCT", "exclusive"),
    (
        16,
        "unload_inspection_desk",
        "LOAD_UNLOAD_TRANSFER_INTERFACE (unload inspection desk)",
        "LOAD_UNLOAD_TRANSFER_INTERFACE",
        "exclusive",
    ),
    (17, "battery_charge", "MANAGE_ROBOT_POWER", "MANAGE_ROBOT_POWER", "self_service"),
    (18, "repair_machine", "REPAIR_MACHINE", "REPAIR_MACHINE", "collaborative"),
    (19, "preventive_maintenance", "PREVENTIVE_MAINTENANCE", "PREVENTIVE_MAINTENANCE", "exclusive"),
]


def _load_cfg() -> dict:
    cfg = yaml.safe_load((ROOT / "configs" / "scenario" / "mfg_flow_shop.yaml").read_text(encoding="utf-8"))
    cfg["task_primitive_timing"] = yaml.safe_load(
        (ROOT / "configs" / "task_primitive_timing" / "mfg_flow_shop.yaml").read_text(encoding="utf-8")
    )
    cfg["horizon"]["num_days"] = 1
    cfg["seed"] = 2026
    cfg["humanoidsim"] = {"enabled": True, "validation_mode": "warn"}
    cfg["decision"] = yaml.safe_load(
        (ROOT / "configs" / "decision" / "rolling_horizon_dedicated_roles.yaml").read_text(encoding="utf-8")
    )
    return cfg


class MfgFlowShopScenarioTests(unittest.TestCase):
    @staticmethod
    def _job_plan() -> JobPlan:
        return JobPlan(task_priority_weights=default_task_priority_weights(), quotas={})

    def test_registry_and_exact_task_set(self) -> None:
        cfg = _load_cfg()
        self.assertEqual("mfg_flow_shop", scenario_type(cfg))
        self.assertEqual(MFG_FLOW_SHOP_TASK_CODES, set(cfg["factory"]["enabled_task_codes"]))
        self.assertEqual(2, cfg["factory"]["machines_per_station"])
        self.assertEqual(480, cfg["horizon"]["minutes_per_day"])
        self.assertEqual(
            {"distribution": "triangular", "min": 7.0, "mode": 8.5, "max": 11.0},
            cfg["factory"]["processing_time"]["station1"],
        )
        self.assertEqual(
            {"distribution": "triangular", "min": 10.0, "mode": 12.5, "max": 16.0},
            cfg["factory"]["processing_time"]["station2"],
        )
        self.assertEqual(4, cfg["factory"]["buffers"]["station1"]["material_input_capacity"])
        self.assertEqual(2, cfg["factory"]["buffers"]["station2"]["output_capacity"])
        self.assertEqual("active_processing", cfg["machine_failure"]["time_basis"])
        self.assertEqual(300, cfg["machine_failure"]["mean_processing_time_to_failure_min"])
        self.assertEqual(
            {
                "enabled": True,
                "due_processing_min": 240,
                "protected_processing_min": 240,
                "hazard_multiplier": 0.5,
            },
            cfg["machine_failure"]["preventive_maintenance"],
        )
        self.assertEqual("policy_decides", cfg["worker"]["battery_safety"]["assignment_mode"])
        self.assertEqual("next_day_start", cfg["worker"]["depleted_recovery"]["schedule"])

    def test_active_processing_failure_exposure_respects_pm_hazard(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                machine = world.machines["S1M1"]
                machine.failure_exposure_budget_min = 10.0
                machine.failure_exposure_used_min = 0.0
                machine.pm_protected_processing_remaining_min = 4.0

                world.record_machine_processing_exposure(machine, 4.0)
                self.assertEqual(2.0, machine.failure_exposure_used_min)
                self.assertEqual(4.0, machine.failure_cycle_processing_min)
                self.assertEqual(0.0, machine.pm_protected_processing_remaining_min)
                self.assertEqual(4.0, machine.pm_protected_processing_total_min)

                world.record_machine_processing_exposure(machine, 8.0)
                self.assertTrue(world.machine_failure_threshold_reached(machine))
                self.assertEqual(12.0, machine.failure_cycle_processing_min)
                self.assertEqual(
                    1,
                    sum(
                        event["type"] == "MACHINE_PM_EFFECT_EXPIRED"
                        for event in logger.events
                    ),
                )

                machine.processing_since_maintenance_min = 123.0
                machine.pm_protected_processing_remaining_min = 17.0
                world.reset_machine_failure_exposure(machine, reason="repair_completed")
                self.assertEqual(123.0, machine.processing_since_maintenance_min)
                self.assertEqual(17.0, machine.pm_protected_processing_remaining_min)
                self.assertEqual(0.0, machine.failure_exposure_used_min)
                self.assertEqual(0.0, machine.failure_cycle_processing_min)
                self.assertGreater(machine.failure_exposure_budget_min, 0.0)
            finally:
                logger.close()

    def test_repair_urgency_uses_station_capacity_demand_and_retained_wip(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                first = world.machines["S1M1"]
                second = world.machines["S1M2"]
                first.broken = True
                first.state = MachineState.BROKEN
                first.active_cycle_id = "cycle-with-wip"
                for index in range(2):
                    item_id = f"MAT-URGENCY-{index}"
                    world.items[item_id] = Item(item_id, "material", 0.0)
                    world.material_queues[1].append(item_id)

                high = world.repair_urgency(first, emit_event=False)
                self.assertEqual("high", high["repair_urgency_tier"])
                self.assertAlmostEqual(0.375, high["repair_urgency_score"])

                second.broken = True
                second.state = MachineState.BROKEN
                critical = world.repair_urgency(first, emit_event=False)
                self.assertEqual("critical", critical["repair_urgency_tier"])
                self.assertEqual(1.0, critical["repair_urgency_score"])
                self.assertEqual(1.0, critical["repair_urgency_components"]["station_outage"])
            finally:
                logger.close()

    def test_critical_repair_bypasses_rolling_boundary_without_preemption(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                world.env.process(strict_periodic_rolling_horizon_loop(world.env, world))
                world.env.run(until=0.000001)

                world.break_machine(world.machines["S1M1"], reason="unit_test_first_loss")
                self.assertFalse(
                    any(
                        entry.get("urgent_dispatch")
                        for queue in world.rolling_horizon_dispatch_queues.values()
                        for entry in queue
                    )
                )

                world.break_machine(world.machines["S1M2"], reason="unit_test_station_outage")
                urgent_repairs = [
                    entry
                    for queue in world.rolling_horizon_dispatch_queues.values()
                    for entry in queue
                    if entry.get("task_code") == "REPAIR_MACHINE"
                    and entry.get("urgent_dispatch")
                ]
                self.assertTrue(urgent_repairs)
                self.assertTrue(
                    all(entry.get("repair_urgency_tier") == "critical" for entry in urgent_repairs)
                )
                self.assertTrue(
                    all(entry.get("scheduled_boundary_min") is None for entry in urgent_repairs)
                )
                self.assertTrue(
                    all(float(entry.get("actual_dispatch_min", -1.0)) < 5.0 for entry in urgent_repairs)
                )
                self.assertEqual(
                    0,
                    sum(
                        bool(agent.current_task_id)
                        for agent in world.agents.values()
                    ),
                )
            finally:
                logger.close()

    def test_policy_decides_keeps_negative_battery_margin_task(self) -> None:
        cfg = _load_cfg()
        cfg["decision"] = yaml.safe_load(
            (ROOT / "configs" / "decision" / "immediate_shared.yaml").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                worker = world.agents["A1"]
                worker.battery_remaining_budget_min = 0.1
                task = Task(
                    task_id="RISKY-SETUP",
                    task_type="SETUP_MACHINE",
                    priority_key="setup_machine",
                    priority=1.0,
                    location="S1M1",
                    payload={"machine_id": "S1M1", "station": 1},
                    task_code="SETUP_MACHINE",
                )

                filtered = world._filter_candidates_for_agent(worker, [task])

                self.assertEqual([task], filtered)
                risk = task.selection_meta["battery_risk"]
                self.assertTrue(risk["battery_depletion_risk"])
                self.assertLess(risk["expected_battery_margin_min"], 0.0)
                self.assertIsNone(world._select_battery_safety_task([task], worker))
            finally:
                logger.close()

    def test_parallel_machines_and_finite_buffer_contract(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                self.assertEqual(["S1M1", "S1M2"], world.machines_by_station[1])
                self.assertEqual(["S2M1", "S2M2"], world.machines_by_station[2])
                self.assertEqual(4, world._buffer_capacity("material_queue_1"))
                self.assertEqual(2, world._buffer_capacity("output_buffer_station_1"))
                self.assertEqual(3, world._buffer_capacity("intermediate_queue_4"))
                for index in range(4):
                    item_id = f"MAT-CAP-{index}"
                    world.items[item_id] = Item(item_id, "material", 0.0)
                    self.assertTrue(world._push_material_queue(1, item_id))
                overflow_id = "MAT-CAP-OVERFLOW"
                world.items[overflow_id] = Item(overflow_id, "material", 0.0)
                self.assertFalse(world._push_material_queue(1, overflow_id))
                self.assertEqual(4, len(world.material_queues[1]))
                self.assertEqual(1, world.buffer_metrics["overflow_attempt_count"])
            finally:
                logger.close()

    def test_parallel_machines_can_process_at_the_same_time(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(
                    env, cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                for index, machine_id in enumerate(("S1M1", "S1M2"), start=1):
                    item_id = f"MAT-PARALLEL-{index}"
                    world.items[item_id] = Item(item_id, "material", 0.0)
                    machine = world.machines[machine_id]
                    machine.input_material = item_id
                    machine.setup_ready = True
                    env.process(machine_lifecycle(env, world, machine_id))

                env.run(until=0.1)

                self.assertEqual(MachineState.PROCESSING, world.machines["S1M1"].state)
                self.assertEqual(MachineState.PROCESSING, world.machines["S1M2"].state)
                self.assertNotEqual(
                    world.machines["S1M1"].active_cycle_id,
                    world.machines["S1M2"].active_cycle_id,
                )
            finally:
                logger.close()

    def test_inbound_reservations_share_the_finite_buffer_capacity(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                for index in range(3):
                    item_id = f"MAT-RESERVED-{index}"
                    world.items[item_id] = Item(item_id, "material", 0.0)
                    self.assertTrue(world._push_material_queue(1, item_id))

                first = Task(
                    "MAT-RESERVE-1",
                    "TRANSFER",
                    "replenish_material",
                    1.0,
                    "Warehouse",
                    payload={"transfer_kind": "material_supply", "station": 1},
                )
                second = Task(
                    "MAT-RESERVE-2",
                    "TRANSFER",
                    "replenish_material",
                    1.0,
                    "Warehouse",
                    payload={"transfer_kind": "material_supply", "station": 1},
                )
                self.assertTrue(world._reserve_task_buffer_slot(world.agents["A1"], first))
                self.assertFalse(world._reserve_task_buffer_slot(world.agents["A2"], second))
                self.assertEqual(
                    4,
                    world.buffer_metrics["max_committed_plus_reserved_by_buffer"]["material_queue_1"],
                )
                world._release_task_buffer_slot(first, reason="test")
                self.assertEqual(0, world._buffer_reserved_count("material_queue_1"))
            finally:
                logger.close()

    def test_rolling_queue_head_can_claim_its_preassigned_buffer_slot(self) -> None:
        cfg = _load_cfg()
        cfg["decision"] = yaml.safe_load(
            (ROOT / "configs" / "decision" / "rolling_horizon_shared.yaml").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                world._ensure_material_shelf_slots()
                world._restock_material_shelf(
                    reason="test_initial_fill",
                    target_fill=world.material_shelf_initial_fill,
                )
                world._rolling_horizon_collect_candidates(collection_trigger="test")
                world._rolling_horizon_dispatch_window()
                worker_id, queue = next(
                    (worker_id, queue)
                    for worker_id, queue in world.rolling_horizon_dispatch_queues.items()
                    if queue and queue[0].get("task_code") == "REPLENISH_MATERIAL"
                )
                agent = world.agents[worker_id]
                opportunity_id = str(queue[0]["opportunity_id"])

                selected = world.select_task_for_agent(agent)

                self.assertIsNotNone(selected)
                self.assertEqual(opportunity_id, world._rolling_horizon_opportunity_id(selected))
                self.assertTrue(
                    world._task_has_buffer_reservation(
                        selected,
                        str(selected.payload["destination_buffer_id"]),
                    )
                )
            finally:
                logger.close()

    def test_rolling_kpi_reports_only_active_task_and_rule_priorities(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                rolling = world.finalize_kpis()["rolling_horizon"]

                self.assertEqual(
                    world.mfg_flow_task_policy.priority_order,
                    rolling["task_rule_priority_order"],
                )
                self.assertTrue(
                    set(rolling["task_code_priority_order"]).issubset(MFG_FLOW_SHOP_TASK_CODES)
                )
                self.assertNotIn("HANDOVER_ITEM", rolling["task_code_priority_order"])
                self.assertIn("PREVENTIVE_MAINTENANCE", rolling["task_code_priority_order"])
            finally:
                logger.close()

    def test_stochastic_streams_are_policy_independent_and_isolated(self) -> None:
        cfg_shared = _load_cfg()
        cfg_shared["decision"] = yaml.safe_load(
            (ROOT / "configs" / "decision" / "immediate_shared.yaml").read_text(encoding="utf-8")
        )
        cfg_dedicated = _load_cfg()
        cfg_dedicated["decision"] = yaml.safe_load(
            (ROOT / "configs" / "decision" / "rolling_horizon_dedicated_roles.yaml").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            logger_shared = EventLogger(Path(tmp) / "shared")
            logger_dedicated = EventLogger(Path(tmp) / "dedicated")
            try:
                shared = ManufacturingWorld(
                    simpy.Environment(), cfg_shared, logger_shared, SimpleNamespace(worker_queue_limit=8)
                )
                dedicated = ManufacturingWorld(
                    simpy.Environment(), cfg_dedicated, logger_dedicated, SimpleNamespace(worker_queue_limit=8)
                )
                self.assertEqual("isolated_v1", shared.stochastic_streams["scheme"])
                self.assertEqual(
                    [shared.quality_rng.random() for _ in range(12)],
                    [dedicated.quality_rng.random() for _ in range(12)],
                )
                for machine_id in sorted(shared.machines):
                    shared_machine = shared.machines[machine_id]
                    dedicated_machine = dedicated.machines[machine_id]
                    self.assertEqual(
                        [shared.sample_machine_failure_delay(shared_machine, 1 / 300.0) for _ in range(5)],
                        [dedicated.sample_machine_failure_delay(dedicated_machine, 1 / 300.0) for _ in range(5)],
                    )
                for _ in range(100):
                    shared._humanoid_incident_random("A1", "ITEM_DROPPED")
                self.assertEqual(shared.quality_rng.random(), dedicated.quality_rng.random())
                self.assertEqual(
                    shared.sample_machine_failure_delay(shared.machines["S1M1"], 1 / 300.0),
                    dedicated.sample_machine_failure_delay(dedicated.machines["S1M1"], 1 / 300.0),
                )
            finally:
                logger_shared.close()
                logger_dedicated.close()

    def test_dropped_machine_output_recovery_restores_real_output_buffer(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                agent = world.agents["A1"]
                product_id = "PRODUCT-RECOVERED"
                parent_task = Task(
                    task_id="UL-RECOVERY",
                    task_type="UNLOAD_MACHINE",
                    priority_key="unload_machine",
                    priority=1.0,
                    location="Station2",
                    payload={"machine_id": "S2M1", "station": 2},
                )

                placed_location = world._place_recovered_dropped_item(
                    agent,
                    product_id,
                    "product",
                    "output_buffer_station_2",
                    parent_task,
                )

                self.assertEqual("Station2", placed_location)
                self.assertEqual([product_id], list(world.output_buffers[2]))
                self.assertEqual(ItemState.IN_OUTPUT_BUFFER, world.items[product_id].state)
                self.assertEqual("output_buffer_station_2", world.items[product_id].metadata["state_ref"])
                self.assertEqual(
                    product_id,
                    world._first_unreserved_queue_item(world.output_buffers[2], agent.agent_id),
                )
            finally:
                logger.close()

    def test_dropped_inspection_load_recovery_places_product_on_desk(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                agent = world.agents["A1"]
                product_id = "PRODUCT-INSPECTION-RECOVERED"
                parent_task = Task(
                    task_id="IFT-RECOVERY",
                    task_type="LOAD_UNLOAD_TRANSFER_INTERFACE",
                    priority_key="load_inspection_desk",
                    priority=1.0,
                    location="Inspection",
                    payload={
                        "interface_action": "load",
                        "inspection_product_id": product_id,
                        "source": "intermediate_queue_4",
                        "destination": world.inspection_workstation_id,
                    },
                )

                destination = world._dropped_item_recovery_destination(
                    parent_task,
                    "product",
                    {"destination": world.inspection_workstation_id},
                    {},
                )
                placed_location = world._place_recovered_dropped_item(
                    agent,
                    product_id,
                    "product",
                    destination,
                    parent_task,
                )

                self.assertEqual(world.inspection_workstation_id, destination)
                self.assertEqual("Inspection", placed_location)
                self.assertEqual([], list(world.intermediate_queues[world.inspection_queue_station]))
                self.assertEqual(product_id, world.inspection_desk_item_id)
                self.assertEqual("STAGED_FOR_INSPECTION", world.inspection_desk_state)
                self.assertEqual(ItemState.STAGED_FOR_INSPECTION, world.items[product_id].state)
                self.assertEqual(world.inspection_workstation_id, world.items[product_id].metadata["state_ref"])
            finally:
                logger.close()

    def test_dropped_inspection_unload_recovery_empties_desk_once(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                agent = world.agents["A1"]
                product_id = "PRODUCT-UNLOAD-RECOVERED"
                world.items[product_id] = Item(
                    item_id=product_id,
                    item_type="product",
                    created_at=0.0,
                    metadata={"inspection_result": "PASS"},
                )
                world._set_inspection_desk_state(
                    "INSPECTED_WAITING_UNLOAD",
                    item_id=product_id,
                    result="PASS",
                    reason="test_setup",
                )
                parent_task = Task(
                    task_id="IFT-UNLOAD-RECOVERY",
                    task_type="LOAD_UNLOAD_TRANSFER_INTERFACE",
                    priority_key="unload_inspection_desk",
                    priority=1.0,
                    location="Inspection",
                    payload={
                        "interface_action": "unload",
                        "inspection_product_id": product_id,
                        "inspection_result": "PASS",
                        "source": world.inspection_workstation_id,
                        "destination": "inspection_output_queue",
                    },
                )

                destination = world._dropped_item_recovery_destination(
                    parent_task,
                    "product",
                    {"destination": world.inspection_workstation_id},
                    {},
                )
                first_location = world._place_recovered_dropped_item(
                    agent, product_id, "product", destination, parent_task
                )
                second_location = world._place_recovered_dropped_item(
                    agent, product_id, "product", destination, parent_task
                )

                self.assertEqual("inspection_output_queue", destination)
                self.assertEqual("Inspection", first_location)
                self.assertEqual(first_location, second_location)
                self.assertEqual("EMPTY", world.inspection_desk_state)
                self.assertIsNone(world.inspection_desk_item_id)
                self.assertEqual([product_id], list(world.output_buffers[world.inspection_queue_station]))
                self.assertEqual(ItemState.WAITING_INSPECTION_OUTPUT, world.items[product_id].state)
                self.assertEqual(
                    1,
                    sum(event["type"] == "INSPECTION_DESK_ITEM_UNLOADED" for event in logger.events),
                )
            finally:
                logger.close()

    def test_dropped_scrap_recovery_emits_disposal_event(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                agent = world.agents["A1"]
                product_id = "PRODUCT-SCRAP-RECOVERED"
                parent_task = Task(
                    task_id="SCRAP-RECOVERY",
                    task_type="COLLECT_WASTE_OR_SCRAP",
                    priority_key="collect_waste_or_scrap",
                    priority=1.0,
                    location="Inspection",
                    payload={"item_ids": [product_id]},
                )

                placed_location = world._place_recovered_dropped_item(
                    agent,
                    product_id,
                    "product",
                    "scrap_disposal_bin",
                    parent_task,
                )

                self.assertEqual("ScrapDisposal", placed_location)
                self.assertEqual(ItemState.SCRAPPED, world.items[product_id].state)
                disposal_events = [event for event in logger.events if event["type"] == "SCRAP_DISPOSED"]
                self.assertEqual(1, len(disposal_events))
                self.assertEqual([product_id], disposal_events[0]["details"]["item_ids"])
                self.assertEqual("dropped_item_recovery", disposal_events[0]["details"]["source"])
            finally:
                logger.close()

    def test_inspection_is_three_independent_top_level_tasks(self) -> None:
        cfg = _load_cfg()
        cfg["decision"] = yaml.safe_load(
            (ROOT / "configs" / "decision" / "immediate_shared.yaml").read_text(encoding="utf-8")
        )
        cfg["humanoid_incidents"]["enabled"] = False
        cfg["quality"]["defect_prob"] = 0.0
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=8))
                worker = world.agents["A1"]
                product_id = "PRODUCT-THREE-STAGE"
                world.items[product_id] = Item(
                    item_id=product_id,
                    item_type="product",
                    created_at=0.0,
                )
                world._push_intermediate_queue(world.inspection_queue_station, product_id)

                def execute_candidate(priority_key: str) -> Task:
                    candidate = next(
                        task for task in world._candidate_tasks(worker)
                        if task.priority_key == priority_key
                    )
                    bound = world.humanoid_runtime.bind_candidate(worker, candidate)
                    self.assertIsNotNone(bound)
                    selected = world._finalize_selected_task(worker, bound)
                    self.assertIsNotNone(selected)
                    assert selected is not None
                    started_at = float(env.now)
                    world.start_agent_task(worker, selected, started_at)
                    process = env.process(world.execute_task(worker, selected))
                    completed = env.run(until=process)
                    world.finish_agent_task(
                        worker,
                        selected,
                        started_at,
                        "completed" if completed else "skipped",
                    )
                    self.assertTrue(completed, selected.payload)
                    return selected

                load_task = execute_candidate("load_inspection_desk")
                self.assertEqual("LOAD_UNLOAD_TRANSFER_INTERFACE", load_task.task_code)
                self.assertEqual("load", load_task.payload["interface_action"])
                self.assertEqual("STAGED_FOR_INSPECTION", world.inspection_desk_state)
                self.assertEqual(product_id, world.inspection_desk_item_id)
                self.assertNotIn(product_id, world.intermediate_queues[world.inspection_queue_station])

                inspect_task = execute_candidate("inspect_product")
                self.assertEqual("INSPECT_PRODUCT", inspect_task.task_code)
                self.assertEqual("INSPECTED_WAITING_UNLOAD", world.inspection_desk_state)
                self.assertEqual("PASS", world.inspection_desk_result)
                self.assertEqual("PASS", world.items[product_id].metadata["inspection_result"])

                unload_task = execute_candidate("unload_inspection_desk")
                self.assertEqual("LOAD_UNLOAD_TRANSFER_INTERFACE", unload_task.task_code)
                self.assertEqual("unload", unload_task.payload["interface_action"])
                self.assertEqual("EMPTY", world.inspection_desk_state)
                self.assertIsNone(world.inspection_desk_item_id)
                self.assertEqual([product_id], list(world.output_buffers[world.inspection_queue_station]))

                top_level_starts = [
                    event["details"].get("task_code")
                    for event in logger.events
                    if event["type"] == "HUMANOID_TASK_START"
                ]
                self.assertEqual(
                    [
                        "LOAD_UNLOAD_TRANSFER_INTERFACE",
                        "INSPECT_PRODUCT",
                        "LOAD_UNLOAD_TRANSFER_INTERFACE",
                    ],
                    top_level_starts,
                )
                self.assertEqual(
                    1,
                    sum(event["type"] == "INSPECTION_RESULT_RECORDED" for event in logger.events),
                )
            finally:
                logger.close()

    def test_interrupted_classification_keeps_result_but_requires_record_step(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                agent = world.agents["A1"]
                product_id = "PRODUCT-CLASSIFIED-NOT-RECORDED"
                world.items[product_id] = Item(
                    item_id=product_id,
                    item_type="product",
                    created_at=0.0,
                    metadata={"inspection_result": "PASS"},
                )
                world.inspection_owner = agent.agent_id
                world.inspection_active_agents = 1
                world._set_inspection_desk_state(
                    "INSPECTING",
                    item_id=product_id,
                    result="PASS",
                    reason="test_classified",
                )

                world._release_inspection_desk(
                    agent.agent_id,
                    task_id="INS-INTERRUPTED",
                    product_id=product_id,
                    reason="interrupted:test",
                )

                self.assertEqual("STAGED_FOR_INSPECTION", world.inspection_desk_state)
                self.assertIsNone(world.inspection_desk_result)
                self.assertEqual("PASS", world.items[product_id].metadata["inspection_result"])
                candidate_keys = {task.priority_key for task in world._candidate_tasks(agent)}
                self.assertIn("inspect_product", candidate_keys)
                self.assertNotIn("unload_inspection_desk", candidate_keys)
            finally:
                logger.close()

    def test_horizon_cleanup_releases_active_inspection_desk(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                agent = world.agents["A1"]
                task = Task(
                    task_id="INS-HORIZON",
                    task_type="INSPECT_PRODUCT",
                    priority_key="inspect_product",
                    priority=1.0,
                    location="Inspection",
                    payload={
                        "inspection_product_id": "PRODUCT-HORIZON",
                        "_reserved_owner": {
                            "kind": "inspection",
                            "ref": "inspection",
                            "agent_id": agent.agent_id,
                        },
                    },
                )
                world.inspection_owner = agent.agent_id
                world.inspection_active_agents = 1

                world._release_task_domain_owner(agent, task, reason="interrupted:horizon_reached")

                self.assertIsNone(world.inspection_owner)
                self.assertEqual(0, world.inspection_active_agents)
                release_events = [event for event in logger.events if event["type"] == "INSPECTION_DESK_RELEASED"]
                self.assertEqual(1, len(release_events))
                self.assertEqual("interrupted:horizon_reached", release_events[0]["details"]["reason"])
            finally:
                logger.close()

    def test_four_representative_policy_configs_share_one_rule_definition(self) -> None:
        modes = [
            "immediate_shared",
            "immediate_dedicated_roles",
            "rolling_horizon_shared",
            "rolling_horizon_dedicated_roles",
        ]
        signatures = []
        for mode in modes:
            decision = yaml.safe_load(
                (ROOT / "configs" / "decision" / f"{mode}.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(mode, decision["mode"])
            policy = decision["mfg_flow_shop_policy"]
            signatures.append((policy["task_rules"], policy["priority_order"]))
        self.assertTrue(all(signature == signatures[0] for signature in signatures[1:]))
        observed = [
            (
                row["role_number"], row["id"], row["display_name"],
                row["task_code"], row["kind"],
            )
            for row in signatures[0][0]
        ]
        self.assertEqual(EXPECTED_ROLE_DEFINITIONS, observed)
        self.assertEqual(17, sum(1 for row in signatures[0][0] if row.get("kind", "exclusive") == "exclusive"))

    def test_role_contract_requires_at_least_two_workers(self) -> None:
        cfg = _load_cfg()
        cfg["factory"]["num_workers"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                with self.assertRaisesRegex(ValueError, "at least two workers"):
                    ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
            finally:
                logger.close()

    def test_role_contract_rejects_duplicate_numbers_and_invalid_common_roles(self) -> None:
        cfg = _load_cfg()
        cfg["decision"]["mfg_flow_shop_policy"]["task_rules"][1]["role_number"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                with self.assertRaisesRegex(ValueError, "role numbers 1..19 exactly once"):
                    ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
            finally:
                logger.close()

        cfg = _load_cfg()
        role_17 = next(
            row for row in cfg["decision"]["mfg_flow_shop_policy"]["task_rules"]
            if row["role_number"] == 17
        )
        role_17["kind"] = "exclusive"
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                with self.assertRaisesRegex(ValueError, "role 17 must be MANAGE_ROBOT_POWER/self_service"):
                    ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
            finally:
                logger.close()

    def test_role_contract_requires_collaborative_repair_capacity(self) -> None:
        cfg = _load_cfg()
        cfg["machine_failure"]["max_repair_agents"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                with self.assertRaisesRegex(ValueError, "max_repair_agents must be at least 2"):
                    ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
            finally:
                logger.close()

    def test_mfg_flow_shop_rejects_non_representative_decision_modes(self) -> None:
        cfg = _load_cfg()
        cfg["decision"] = {"mode": "adaptive_priority"}
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                with self.assertRaisesRegex(ValueError, "four representative decision modes"):
                    ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
            finally:
                logger.close()

    def test_pinned_owner_is_applied_before_auto_lpt(self) -> None:
        cfg = _load_cfg()
        for row in cfg["decision"]["mfg_flow_shop_policy"]["task_rules"]:
            if row["id"] == "inspect_product":
                row["owner"] = "A2"
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                policy = world.mfg_flow_task_policy
                self.assertEqual("A2", policy.exclusive_owner_by_rule["inspect_product"])
                metric = next(row for row in policy.summary()["rules"] if row["rule_id"] == "inspect_product")
                self.assertEqual("fixed", metric["assignment_source"])
            finally:
                logger.close()

    def test_granular_transfer_and_load_rules_control_worker_eligibility(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                policy = world.mfg_flow_task_policy
                tasks = [
                    Task(
                        task_id="TR-S1",
                        task_type="TRANSFER",
                        priority_key="inter_station_transfer",
                        priority=1.0,
                        location="Station1",
                        payload={"transfer_kind": "inter_station", "from_station": 1},
                        task_code="TRANSFER",
                    ),
                    Task(
                        task_id="LOAD-S2-MAT",
                        task_type="LOAD_MACHINE",
                        priority_key="load_machine",
                        priority=1.0,
                        location="Station2",
                        payload={"station": 2, "load_slot": "material", "machine_id": "S2M1"},
                        task_code="LOAD_MACHINE",
                    ),
                    Task(
                        task_id="LOAD-S2-INT",
                        task_type="LOAD_MACHINE",
                        priority_key="load_machine",
                        priority=1.0,
                        location="Station2",
                        payload={"station": 2, "load_slot": "intermediate", "machine_id": "S2M1"},
                        task_code="LOAD_MACHINE",
                    ),
                ]
                expected_rules = ["transfer_s1_to_s2", "load_s2_material", "load_s2_intermediate"]
                for task, expected_rule in zip(tasks, expected_rules):
                    rule = world._mfg_flow_task_rule(task)
                    self.assertEqual(expected_rule, rule.rule_id)
                    self.assertEqual(
                        [policy.exclusive_owner_by_rule[expected_rule]],
                        world._mfg_flow_allowed_worker_ids_for_task(task),
                    )
            finally:
                logger.close()

        cfg = _load_cfg()
        cfg["decision"] = yaml.safe_load(
            (ROOT / "configs" / "decision" / "immediate_shared.yaml").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                task = Task(
                    task_id="TR-SHARED",
                    task_type="TRANSFER",
                    priority_key="inter_station_transfer",
                    priority=1.0,
                    location="Station1",
                    payload={"transfer_kind": "inter_station", "from_station": 1},
                    task_code="TRANSFER",
                )
                self.assertEqual(sorted(world.agents), world._mfg_flow_allowed_worker_ids_for_task(task))
            finally:
                logger.close()

    def test_default_objective_is_fixed_horizon_throughput(self) -> None:
        cfg = _load_cfg()
        resolved = resolve_manufacturing_objective(cfg)

        self.assertEqual("maximize_throughput", resolved["mode"])
        self.assertEqual(cfg["horizon"]["num_days"], resolved["run_day_limit"])
        self.assertEqual(1, resolved["restock_interval_days"])
        self.assertEqual(30, resolved["restock_target_fill"])

    def test_makespan_objective_uses_separate_safety_limit(self) -> None:
        cfg = _load_cfg()
        cfg["objective"]["mode"] = "minimize_makespan"
        resolved = resolve_manufacturing_objective(cfg)

        self.assertEqual(30, resolved["run_day_limit"])
        self.assertEqual("product_or_disposed_scrap", resolved["terminal_policy"])

    def test_map_has_worker_docks_single_machines_and_one_inspection_desk(self) -> None:
        cfg = _load_cfg()
        grid = TileGridMap.from_world_config(cfg, stations=[1, 2], machines_per_station=1)

        self.assertIn("S1M1", grid.objects)
        self.assertIn("S2M1", grid.objects)
        self.assertNotIn("S1M2", grid.objects)
        self.assertNotIn("S2M2", grid.objects)
        self.assertNotIn("battery_rack", grid.objects)
        self.assertIn("inspection_desk", grid.objects)
        self.assertEqual(1, len(grid.service_tiles["inspection_desk"]))
        desk_tiles = set(grid.service_tiles["inspection_desk"])
        staging_tiles = set(grid.service_tiles["inspection_staging"])
        self.assertGreaterEqual(len(staging_tiles), cfg["factory"]["num_workers"])
        self.assertTrue(staging_tiles.isdisjoint(desk_tiles))
        self.assertTrue(all(grid.is_passable_static(tile) for tile in staging_tiles))
        other_object_service_tiles = {
            tile
            for object_id, tiles in grid.service_tiles.items()
            if object_id not in {"inspection_desk", "inspection_staging"}
            for tile in tiles
        }
        self.assertTrue(staging_tiles.isdisjoint(other_object_service_tiles))

        dock_tiles = set()
        for index in range(1, cfg["factory"]["num_workers"] + 1):
            worker_id = f"A{index}"
            dock_id = f"charging_dock_{worker_id}"
            self.assertIn(dock_id, grid.objects)
            self.assertEqual("charging_dock", grid.objects[dock_id].object_type)
            self.assertEqual([grid.initial_worker_tile(worker_id)], grid.service_tiles[dock_id])
            self.assertTrue(grid.is_passable_static(grid.initial_worker_tile(worker_id)))
            dock_tiles.add(grid.initial_worker_tile(worker_id))
        self.assertEqual(cfg["factory"]["num_workers"], len(dock_tiles))

    def test_completed_inspection_stage_physically_vacates_desk_service_tile(self) -> None:
        cfg = _load_cfg()
        cfg["humanoid_incidents"]["enabled"] = False
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(
                    env, cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                worker = world.agents["A1"]
                desk_tile = world.grid_map.service_tiles[world.inspection_workstation_id][0]
                world.grid_map.move_worker(worker.agent_id, desk_tile)
                worker.tile = desk_tile
                worker.location = "Inspection"
                task = Task(
                    task_id="IFT-EGRESS",
                    task_type="LOAD_UNLOAD_TRANSFER_INTERFACE",
                    priority_key="load_inspection_desk",
                    priority=1.0,
                    location="Inspection",
                    payload={"interface_action": "load"},
                    task_code="LOAD_UNLOAD_TRANSFER_INTERFACE",
                )

                process = env.process(world.vacate_completed_task_service_tile(worker, task))
                env.run(until=process)

                self.assertNotEqual(desk_tile, worker.tile)
                self.assertIn(worker.tile, world.grid_map.service_tiles["inspection_staging"])
                move_starts = [event for event in logger.events if event["type"] == "AGENT_MOVE_START"]
                move_ends = [event for event in logger.events if event["type"] == "AGENT_MOVE_END"]
                vacated = [
                    event for event in logger.events
                    if event["type"] == "INSPECTION_WORKSTATION_VACATED"
                ]
                self.assertEqual(1, len(move_starts))
                self.assertEqual(1, len(move_ends))
                self.assertEqual(1, len(vacated))
                self.assertEqual(
                    {"x": desk_tile[0], "y": desk_tile[1]},
                    vacated[0]["details"]["from_tile"],
                )
                self.assertEqual(
                    {"x": worker.tile[0], "y": worker.tile[1]},
                    vacated[0]["details"]["to_tile"],
                )
                self.assertGreater(float(env.now), 0.0)
            finally:
                logger.close()

    def test_recovered_inspection_interruption_vacates_before_owner_release(self) -> None:
        cfg = _load_cfg()
        cfg["humanoid_incidents"]["enabled"] = False
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(
                    env, cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                worker = world.agents["A1"]
                product_id = "PRODUCT-RECOVERED-AT-DESK"
                desk_tile = world.grid_map.service_tiles[world.inspection_workstation_id][0]
                world.grid_map.move_worker(worker.agent_id, desk_tile)
                worker.tile = desk_tile
                worker.location = "Inspection"
                world.items[product_id] = Item(
                    item_id=product_id,
                    item_type="product",
                    created_at=0.0,
                )
                world._set_inspection_desk_state(
                    "STAGED_FOR_INSPECTION",
                    item_id=product_id,
                    reason="test_recovered_drop",
                )
                world.inspection_owner = worker.agent_id
                task = Task(
                    task_id="IFT-RECOVERED-DROP",
                    task_type="LOAD_UNLOAD_TRANSFER_INTERFACE",
                    priority_key="load_inspection_desk",
                    priority=1.0,
                    location="Inspection",
                    payload={
                        "interface_action": "load",
                        "inspection_product_id": product_id,
                        "_inspection_interface_placed": True,
                        "_reserved_owner": {
                            "kind": "inspection_workstation",
                            "ref": "inspection",
                            "agent_id": worker.agent_id,
                        },
                    },
                    task_code="LOAD_UNLOAD_TRANSFER_INTERFACE",
                )

                world.handle_task_interruption(worker, task, "ITEM_DROPPED")
                self.assertEqual(worker.agent_id, world.inspection_owner)

                process = env.process(world.vacate_completed_task_service_tile(worker, task))
                env.run(until=process)
                self.assertEqual(worker.agent_id, world.inspection_owner)
                self.assertIn(worker.tile, world.grid_map.service_tiles["inspection_staging"])

                world.finish_agent_task(
                    worker,
                    task,
                    start_t=0.0,
                    status="interrupted",
                    reason="ITEM_DROPPED",
                )
                self.assertIsNone(world.inspection_owner)
                event_types = [event["type"] for event in logger.events]
                self.assertLess(
                    event_types.index("INSPECTION_WORKSTATION_VACATED"),
                    event_types.index("AGENT_TASK_END"),
                )
            finally:
                logger.close()

    def test_dropped_item_interruption_does_not_rollback_into_full_origin_buffer(self) -> None:
        cfg = _load_cfg()
        cfg["humanoid_incidents"]["enabled"] = False
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(),
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                worker = world.agents["A1"]
                dropped_product_id = "PRODUCT-DROPPED-FULL-ORIGIN"
                output_capacity = world._buffer_capacity("output_buffer_station_2")
                self.assertIsNotNone(output_capacity)
                world.output_buffers[2].extend(
                    f"PRODUCT-FILLER-{index}" for index in range(int(output_capacity))
                )
                original_buffer = list(world.output_buffers[2])
                world.dropped_items[dropped_product_id] = {
                    "item_id": dropped_product_id,
                    "item_type": "product",
                    "tile": worker.tile,
                }
                transfer = Task(
                    task_id="TRANSFER-DROPPED-FULL-ORIGIN",
                    task_type="TRANSFER",
                    priority_key="inter_station_transfer",
                    priority=1.0,
                    location="Inspection",
                    payload={
                        "transfer_kind": "inter_station",
                        "from_station": 2,
                        "transfer_item_id": dropped_product_id,
                    },
                    task_code="TRANSFER",
                )

                world.handle_task_interruption(worker, transfer, "ITEM_DROPPED")

                self.assertEqual(original_buffer, list(world.output_buffers[2]))
                self.assertIn(dropped_product_id, world.dropped_items)
                self.assertNotIn(dropped_product_id, world.output_buffers[2])
            finally:
                logger.close()

    def test_map_rejects_more_workers_than_dedicated_dock_capacity(self) -> None:
        cfg = _load_cfg()
        cfg["factory"]["num_workers"] = 100

        with self.assertRaisesRegex(ValueError, "dedicated charging docks"):
            TileGridMap.from_world_config(cfg, stations=[1, 2], machines_per_station=1)

    def test_world_disables_swap_delivery_and_handover_but_enables_pm(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(),
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                self.assertTrue(world.battery_direct_charge_enabled)
                self.assertFalse(world.battery_delivery_enabled)
                self.assertFalse(world.battery_swap_enabled)
                self.assertTrue(world.preventive_maintenance_enabled)
                self.assertEqual(MFG_FLOW_SHOP_TASK_CODES, world.enabled_task_codes)
                self.assertEqual("BatteryStation", world.agents["A1"].location)
                self.assertEqual(
                    {
                        "battery_charge",
                        "inspect_product",
                        "inter_station_transfer",
                        "load_machine",
                        "material_supply",
                        "preventive_maintenance",
                        "repair_machine",
                        "scrap_disposal",
                        "setup_machine",
                        "unload_machine",
                    },
                    set(world.current_agent_priority_multipliers("A1")),
                )
                self.assertLessEqual(world._battery_monitor_sleep_min(world.agents["A1"]), 1.0)
                candidates = [
                    task
                    for worker in world.agents.values()
                    for task in world._candidate_tasks(worker)
                ]
                codes = {world._task_code(task) for task in candidates}
                self.assertNotIn("PREVENTIVE_MAINTENANCE", codes)
                self.assertNotIn("HANDOVER_ITEM", codes)
            finally:
                logger.close()

    def test_material_replenishment_is_machine_demand_driven_without_target_stock(self) -> None:
        cfg = _load_cfg()
        self.assertNotIn("inventory_targets", cfg)
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8)
                )
                world.bootstrap()
                agent = world.agents["A1"]

                initial_supply = {
                    int(task.payload["station"]): task
                    for task in world._candidate_tasks(agent)
                    if task.payload.get("transfer_kind") == "material_supply"
                }
                self.assertEqual({1, 2}, set(initial_supply))
                self.assertTrue(
                    all(task.payload.get("supply_policy") == "machine_demand" for task in initial_supply.values())
                )
                self.assertTrue(all("target_level" not in task.payload for task in initial_supply.values()))
                bound = world.humanoid_runtime.bind_candidate(agent, initial_supply[1])
                self.assertIsNotNone(bound)
                self.assertEqual(
                    {"station": 1, "supply_policy": "machine_demand"},
                    bound.args["rule"],
                )

                world._warehouse_push_material(1)
                supply_tasks = [
                    task
                    for task in world._candidate_tasks(agent)
                    if task.payload.get("transfer_kind") == "material_supply"
                    and int(task.payload["station"]) == 1
                ]
                self.assertEqual(1, len(supply_tasks))
                world._warehouse_push_material(1)
                supply_stations = {
                    int(task.payload["station"])
                    for task in world._candidate_tasks(agent)
                    if task.payload.get("transfer_kind") == "material_supply"
                }
                self.assertNotIn(1, supply_stations)

                world.material_queues[1].clear()
                agent.current_task_id = "LOAD-IN-FLIGHT"
                agent.current_task_type = "LOAD_MACHINE"
                agent.current_task_payload = {
                    "machine_id": "S1M1",
                    "station": 1,
                    "load_slot": "material",
                }
                supply_tasks = [
                    task
                    for task in world._candidate_tasks(agent)
                    if task.payload.get("transfer_kind") == "material_supply"
                    and int(task.payload["station"]) == 1
                ]
                self.assertEqual(1, len(supply_tasks), "S1M2 still has one unmet material demand")
            finally:
                logger.close()

    def test_task_assignment_replaces_stale_destination_metadata(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(),
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                worker = world.agents["A2"]
                worker.humanoid_state["metadata"] = {"destination": "S1M1"}
                task = Task(
                    task_id="UL-TEST",
                    task_type="UNLOAD_MACHINE",
                    priority_key="unload_machine",
                    priority=1.0,
                    location="Station2",
                    payload={"machine_id": "S2M1", "station": 2},
                    task_code="UNLOAD_MACHINE",
                    instance_id="UL-TEST:UNLOAD_MACHINE",
                    assigned_robot_id="A2",
                    args={"machine": "S2M1", "destination": "output_buffer_station_2"},
                )

                world.start_agent_task(worker, task, 0.0)

                self.assertEqual("S2M1", worker.humanoid_state["metadata"]["destination"])
            finally:
                logger.close()

    def test_horizon_interruption_preserves_direct_role_event_metadata(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(),
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                rule_id = "inspect_product"
                worker_id = world.mfg_flow_task_policy.exclusive_owner_by_rule[rule_id]
                worker = world.agents[worker_id]
                task = Task(
                    task_id="INS-HORIZON",
                    task_type="INSPECT_PRODUCT",
                    priority_key="inspect_product",
                    priority=1.0,
                    location="Inspection",
                    task_code="INSPECT_PRODUCT",
                    instance_id="INS-HORIZON:INSPECT_PRODUCT",
                    assigned_robot_id=worker_id,
                )
                selected = world._finalize_selected_task(worker, task)
                self.assertIsNotNone(selected)
                world.start_agent_task(worker, selected, 0.0)

                world.close_open_activity_at_horizon()

                role_events = [
                    event for event in logger.events
                    if event["type"] in {"AGENT_TASK_START", "AGENT_TASK_END"}
                ]
                self.assertEqual(2, len(role_events))
                for event in role_events:
                    details = event["details"]
                    self.assertEqual(rule_id, details["task_rule_id"])
                    self.assertEqual(15, details["role_number"])
                    self.assertEqual("INSPECT_PRODUCT", details["role_task_code"])
                    self.assertEqual(worker_id, details["role_owner_agent_id"])
            finally:
                logger.close()

    def test_direct_charge_soc_increases_linearly(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(
                    env,
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                worker = world.agents["A1"]
                worker.battery_remaining_budget_min = 40.0
                worker.battery_last_accounted_at = 0.0
                worker.battery_accounting_swap_at = worker.last_battery_swap
                worker.charging_started_at = 0.0
                worker.charging_start_budget_min = 40.0
                worker.charging_target_budget_min = 200.0
                worker.charging_duration_min = 10.0

                env.run(until=5.0)
                self.assertAlmostEqual(120.0, world.battery_remaining(worker), places=6)
                env.run(until=10.0)
                self.assertAlmostEqual(200.0, world.battery_remaining(worker), places=6)
            finally:
                logger.close()

    def test_depleted_worker_waits_until_next_day_recovery(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(
                    env,
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                worker = world.agents["A1"]
                dock_id = world._assigned_charging_dock(worker)
                worker.tile = world.grid_map.service_tiles[dock_id][0]
                worker.battery_remaining_budget_min = 0.0
                worker.battery_last_accounted_at = 0.0
                worker.battery_accounting_swap_at = worker.last_battery_swap
                world.discharge_agent(worker, reason="test_depleted", interrupt_process=False)

                self.assertIsNone(world.mandatory_task_for_agent(worker))
                self.assertEqual(2, worker.depleted_recovery_due_day)

                ordinary_task = Task(
                    task_id="LOAD-DISALLOWED",
                    task_type="LOAD_MACHINE",
                    priority_key="load_machine",
                    priority=1.0,
                    location="Station1",
                )
                with self.assertRaisesRegex(RuntimeError, "only charging"):
                    world.start_agent_task(worker, ordinary_task, 0.0)

                env.run(until=world.minutes_per_day)
                world.start_day(2, StrategyState(), self._job_plan())
                self.assertFalse(worker.discharged)
                self.assertAlmostEqual(world.battery_swap_period_min, world.battery_remaining(worker))
                self.assertEqual(world.grid_map.initial_worker_tile(worker.agent_id), worker.tile)
                self.assertEqual("AVAILABLE", worker.humanoid_state["availability"])
                self.assertEqual("POWER_NORMAL", worker.humanoid_state["power"])
                self.assertEqual(
                    1,
                    sum(1 for event in logger.events if event["type"] == "WORKER_RETURNED_NEXT_DAY"),
                )
                event_types = [event["type"] for event in logger.events]
                returned_index = event_types.index("WORKER_RETURNED_NEXT_DAY")
                next_state_index = next(
                    index
                    for index in range(returned_index + 1, len(logger.events))
                    if logger.events[index]["type"] == "WORKER_STATE_CHANGED"
                    and logger.events[index]["entity_id"] == worker.agent_id
                )
                self.assertLess(returned_index, next_state_index)
                returned = logger.events[returned_index]
                self.assertEqual("scheduled_external_recovery", returned["details"]["relocation_kind"])
                dock_tile = world.grid_map.initial_worker_tile(worker.agent_id)
                self.assertEqual(
                    {"x": dock_tile[0], "y": dock_tile[1]},
                    returned["details"]["to_tile"],
                )
            finally:
                logger.close()

    def test_interrupted_direct_charge_preserves_soc_and_clears_charging_state(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(
                    env,
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                worker = world.agents["A1"]
                dock_id = world._assigned_charging_dock(worker)
                worker.tile = world.grid_map.service_tiles[dock_id][0]
                worker.battery_remaining_budget_min = 40.0
                worker.battery_last_accounted_at = 0.0
                worker.battery_accounting_swap_at = worker.last_battery_swap
                task = Task(
                    task_id="BAT-INTERRUPTED",
                    task_type="BATTERY_CHARGE",
                    priority_key="battery_charge",
                    priority=1.0,
                    location=dock_id,
                    task_code="MANAGE_ROBOT_POWER",
                    instance_id="BAT-INTERRUPTED:MANAGE_ROBOT_POWER",
                    assigned_robot_id=worker.agent_id,
                    payload={"charging_dock_id": dock_id, "target_soc": 1.0},
                )

                def interrupt_charge():
                    process = env.process(world._execute_task_domain_action(worker, task))
                    yield env.timeout(2.0)
                    process.interrupt("simulation_end")
                    try:
                        yield process
                    except simpy.Interrupt:
                        pass

                env.process(interrupt_charge())
                env.run()

                event_types = [event["type"] for event in logger.events]
                self.assertIn("BATTERY_CHARGE_STARTED", event_types)
                self.assertIn("BATTERY_CHARGE_INTERRUPTED", event_types)
                self.assertNotIn("BATTERY_CHARGE_COMPLETED", event_types)
                self.assertGreater(worker.battery_remaining_budget_min, 40.0)
                self.assertIsNone(worker.charging_started_at)
                self.assertNotEqual("CHARGING", worker.humanoid_state["power"])
                charge_start = next(event for event in logger.events if event["type"] == "BATTERY_CHARGE_STARTED")
                charge_end = next(event for event in logger.events if event["type"] == "BATTERY_CHARGE_INTERRUPTED")
                actual_charge_time = float(charge_end["t"]) - float(charge_start["t"])
                self.assertGreater(actual_charge_time, 0.0)
                self.assertAlmostEqual(
                    actual_charge_time,
                    world._battery_service_metrics()["battery_charge_time_min"],
                )
            finally:
                logger.close()

    def test_horizon_close_interrupts_open_direct_charge(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(
                    env,
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                worker = world.agents["A1"]
                dock_id = world._assigned_charging_dock(worker)
                worker.tile = world.grid_map.service_tiles[dock_id][0]
                worker.battery_remaining_budget_min = 40.0
                worker.battery_last_accounted_at = 0.0
                worker.battery_accounting_swap_at = worker.last_battery_swap
                task = Task(
                    task_id="BAT-HORIZON",
                    task_type="BATTERY_CHARGE",
                    priority_key="battery_charge",
                    priority=1.0,
                    location=dock_id,
                    task_code="MANAGE_ROBOT_POWER",
                    instance_id="BAT-HORIZON:MANAGE_ROBOT_POWER",
                    assigned_robot_id=worker.agent_id,
                    payload={"charging_dock_id": dock_id, "target_soc": 1.0},
                )
                world.start_agent_task(worker, task, 0.0)
                env.process(world._execute_task_domain_action(worker, task))
                env.run(until=2.0)
                self.assertEqual("CHARGING", worker.humanoid_state["power"])

                world.close_open_activity_at_horizon()

                interrupted = [
                    event for event in logger.events if event["type"] == "BATTERY_CHARGE_INTERRUPTED"
                ]
                self.assertEqual(1, len(interrupted))
                self.assertEqual("horizon_reached", interrupted[0]["details"]["reason"])
                self.assertAlmostEqual(2.0, interrupted[0]["details"]["actual_charge_duration_min"])
                self.assertIsNone(worker.charging_started_at)
                self.assertNotEqual("CHARGING", worker.humanoid_state["power"])
                task_end = next(event for event in logger.events if event["type"] == "AGENT_TASK_END")
                self.assertNotEqual("CHARGING", task_end["details"]["humanoid_state"]["power"])
                self.assertAlmostEqual(2.0, world._battery_service_metrics()["battery_charge_time_min"])
            finally:
                logger.close()

    def test_battery_charge_metrics_include_interrupted_session_time(self) -> None:
        events = [
            {
                "t": 2.0,
                "type": "BATTERY_CHARGE_STARTED",
                "entity_id": "A1",
                "details": {"task_id": "BAT-1", "charging_dock_id": "charging_dock_A1"},
            },
            {
                "t": 6.0,
                "type": "BATTERY_CHARGE_COMPLETED",
                "entity_id": "A1",
                "details": {"task_id": "BAT-1", "charge_duration_min": 4.0},
            },
            {
                "t": 8.0,
                "type": "BATTERY_CHARGE_STARTED",
                "entity_id": "A2",
                "details": {"task_id": "BAT-2", "charging_dock_id": "charging_dock_A2"},
            },
            {
                "t": 10.0,
                "type": "AGENT_TASK_END",
                "entity_id": "A2",
                "details": {
                    "task_id": "BAT-2",
                    "status": "interrupted",
                    "payload": {"action": "dock_charge"},
                },
            },
        ]
        world = object.__new__(ManufacturingWorld)
        world.logger = SimpleNamespace(events=events)
        world.env = SimpleNamespace(now=10.0)
        world.battery_service_mode = "dock_charge"

        sessions = world._battery_charge_sessions()
        metrics = world._battery_service_metrics()

        self.assertEqual(2, len(sessions))
        self.assertEqual(2, metrics["battery_charge_started_count"])
        self.assertEqual(1, metrics["battery_charge_count"])
        self.assertAlmostEqual(6.0, metrics["battery_charge_time_min"])
        self.assertEqual({"A1": 1}, metrics["battery_charge_count_by_worker"])
        self.assertEqual({"A1": 4.0, "A2": 2.0}, metrics["battery_charge_time_min_by_worker"])
        self.assertAlmostEqual(3.0, world._battery_charge_time_in_interval(sessions, 0.0, 5.0))
        self.assertAlmostEqual(3.0, world._battery_charge_time_in_interval(sessions, 5.0, 10.0))

    def test_completed_machine_cycle_marks_inputs_as_transformed(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(),
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                machine = world.machines["S1M1"]
                material_id = "MAT-TEST-1"
                world.items[material_id] = Item(item_id=material_id, item_type="material", created_at=0.0)
                machine.input_material = material_id
                machine.active_cycle_id = "CYCLE-TEST"
                machine.cycle_sampled_process_min = 20.0
                machine.cycle_remaining_process_min = 0.0

                world.complete_machine_cycle(machine, "CYCLE-TEST")

                output_id = str(machine.output_intermediate)
                self.assertTrue(output_id.startswith("INT-S1-"))
                self.assertEqual(ItemState.TRANSFORMED, world.items[material_id].state)
                self.assertEqual(output_id, world.items[material_id].metadata["transformed_to_item_id"])
                transformed_events = [
                    event
                    for event in logger.events
                    if event["type"] == "ITEM_STATE_CHANGED" and event["entity_id"] == material_id
                ]
                self.assertEqual("TRANSFORMED", transformed_events[-1]["details"]["item_state"])
                self.assertEqual(output_id, transformed_events[-1]["details"]["transformed_to_item_id"])
            finally:
                logger.close()

    def test_setup_aborts_without_overwriting_broken_machine_state(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=8))
                machine = world.machines["S1M1"]
                worker = world.agents["A2"]
                material_id = "MAT-SETUP-BREAK"
                world.items[material_id] = Item(item_id=material_id, item_type="material", created_at=0.0)
                machine.input_material = material_id
                machine.setup_ready = False
                machine.state = MachineState.WAIT_INPUT
                task = Task(
                    task_id="SET-BREAK-TEST",
                    task_type="SETUP_MACHINE",
                    priority_key="setup_machine",
                    priority=1.0,
                    location="Station1",
                    payload={"machine_id": machine.machine_id, "station": machine.station},
                    task_code="SETUP_MACHINE",
                    instance_id="SET-BREAK-TEST:SETUP_MACHINE",
                    assigned_robot_id=worker.agent_id,
                )

                def break_during_setup():
                    while machine.state != MachineState.SETUP:
                        yield env.timeout(0.01)
                    yield env.timeout(0.01)
                    world.break_machine(machine, reason="unit_test_during_setup")

                setup_process = env.process(world._execute_task_domain_action(worker, task))
                env.process(break_during_setup())
                result = env.run(until=setup_process)

                self.assertFalse(result)
                self.assertTrue(machine.broken)
                self.assertEqual(MachineState.BROKEN, machine.state)
                self.assertFalse(machine.setup_ready)
                setup_end = [event for event in logger.events if event["type"] == "MACHINE_SETUP_END"]
                self.assertEqual("aborted_machine_broken", setup_end[-1]["details"]["outcome"])
                self.assertFalse(
                    any(
                        event["type"] == "MACHINE_STATE_CHANGED"
                        and event["details"].get("reason") == "setup_completed"
                        for event in logger.events
                    )
                )
            finally:
                logger.close()

    def test_machine_lifecycle_preserves_pm_and_does_not_process_reserved_machine(self) -> None:
        for loaded in (False, True):
            with self.subTest(loaded=loaded), tempfile.TemporaryDirectory() as tmp:
                logger = EventLogger(Path(tmp))
                try:
                    env = simpy.Environment()
                    world = ManufacturingWorld(env, _load_cfg(), logger, SimpleNamespace(worker_queue_limit=8))
                    machine = world.machines["S1M1"]
                    machine.state = MachineState.UNDER_PM
                    machine.pm_owner = "A1"
                    machine.input_material = "MAT-PM" if loaded else None
                    machine.setup_ready = loaded
                    env.process(machine_lifecycle(env, world, machine.machine_id))
                    env.run(until=2.5)
                    self.assertEqual(MachineState.UNDER_PM, machine.state)
                    self.assertFalse(any(e["type"] == "MACHINE_START" for e in logger.events))
                    self.assertEqual(0.0, machine.total_processing_min)
                finally:
                    logger.close()

    def test_interrupted_pm_releases_machine_and_records_partial_downtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, _load_cfg(), logger, SimpleNamespace(worker_queue_limit=8))
                machine = world.machines["S1M1"]
                machine.state = MachineState.WAIT_INPUT
                machine.input_material = "MAT-PM-RETAINED"
                machine.setup_ready = True
                worker = world.agents["A1"]
                def no_movement(*_args, **_kwargs):
                    yield from ()
                world.move_agent = no_movement
                world._dock_agent_at_target = no_movement
                world._confirm_object_service_tile = lambda *_args: True
                task = Task(task_id="PM-INTERRUPTED", task_type="PREVENTIVE_MAINTENANCE",
                            priority_key="preventive_maintenance", priority=1, location="Station1",
                            payload={"machine_id": "S1M1", "station": 1}, task_code="PREVENTIVE_MAINTENANCE")
                process = env.process(world._execute_task_domain_action(worker, task))
                def interrupt():
                    yield env.timeout(2)
                    process.interrupt("battery_depleted")
                env.process(interrupt())
                with self.assertRaises(simpy.Interrupt):
                    env.run(until=process)
                self.assertIsNone(machine.pm_owner)
                self.assertEqual(MachineState.WAIT_INPUT, machine.state)
                self.assertEqual("MAT-PM-RETAINED", machine.input_material)
                self.assertTrue(machine.setup_ready)
                self.assertEqual(0, machine.pm_count)
                self.assertEqual(0, machine.pm_protected_processing_remaining_min)
                self.assertEqual(2, machine.total_pm_min)
                ends = [event for event in logger.events if event["type"] == "MACHINE_PM_END"]
                self.assertEqual(1, len(ends))
                self.assertEqual("interrupted", ends[0]["details"]["outcome"])
            finally:
                logger.close()

    def test_machine_lifecycle_preserves_under_repair_state(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=8))
                machine = world.machines["S1M1"]
                machine.broken = True
                machine.state = MachineState.UNDER_REPAIR
                machine.repair_team = ["A1"]
                env.process(machine_lifecycle(env, world, machine.machine_id))

                env.run(until=0.5)

                self.assertEqual(MachineState.UNDER_REPAIR, machine.state)
                self.assertFalse(
                    any(
                        event["type"] == "MACHINE_STATE_CHANGED"
                        and event["details"].get("machine_state") == "BROKEN"
                        for event in logger.events
                    )
                )
            finally:
                logger.close()

    def test_machine_processing_kpi_includes_resumed_segment(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                env = simpy.Environment()
                world = ManufacturingWorld(env, cfg, logger, SimpleNamespace(worker_queue_limit=8))
                env.run(until=10.0)
                common = {"cycle_id": "CYCLE-TEST"}
                logger.log(t=1.0, day=1, event_type="MACHINE_START", entity_id="S1M1", location="Station1", details=common)
                logger.log(t=3.0, day=1, event_type="MACHINE_ABORTED", entity_id="S1M1", location="Station1", details=common)
                logger.log(t=5.0, day=1, event_type="MACHINE_RESUME", entity_id="S1M1", location="Station1", details=common)
                logger.log(t=8.0, day=1, event_type="MACHINE_END", entity_id="S1M1", location="Station1", details=common)

                metrics = world._machine_time_metrics()

                self.assertAlmostEqual(5.0, metrics["time_by_machine"]["S1M1"]["processing_min"])
            finally:
                logger.close()

    def test_dedicated_roles_are_balanced_without_missing_or_duplicate_rules(self) -> None:
        baseline_owners = None
        for worker_count in range(3, 9):
            cfg = _load_cfg()
            cfg["factory"]["num_workers"] = worker_count
            with self.subTest(worker_count=worker_count), tempfile.TemporaryDirectory() as tmp:
                logger = EventLogger(Path(tmp))
                try:
                    world = ManufacturingWorld(
                        simpy.Environment(),
                        cfg,
                        logger,
                        SimpleNamespace(worker_queue_limit=8),
                    )
                    policy = world.mfg_flow_task_policy
                    self.assertIsNotNone(policy)
                    summary = policy.summary()
                    exclusive_rules = {
                        row["rule_id"] for row in summary["rules"] if row["kind"] == "exclusive"
                    }
                    self.assertEqual(17, len(exclusive_rules))
                    self.assertEqual(exclusive_rules, set(policy.exclusive_owner_by_rule))
                    assigned = [
                        rule_id
                        for payload in summary["workers"].values()
                        for rule_id in payload["exclusive_rule_ids"]
                    ]
                    self.assertEqual(len(exclusive_rules), len(assigned))
                    self.assertEqual(exclusive_rules, set(assigned))
                    self.assertEqual(0, summary["validation"]["duplicate_exclusive_owner_count"])
                    for worker_id, payload in summary["workers"].items():
                        self.assertIn("MANAGE_ROBOT_POWER", payload["task_codes"])
                        self.assertIn("REPAIR_MACHINE", payload["task_codes"])
                        self.assertTrue({"battery_charge", "repair_machine"}.issubset(payload["assigned_rule_ids"]))
                        self.assertTrue({17, 18}.issubset(payload["role_numbers"]))
                        self.assertIn(worker_id, world.agents)
                    if worker_count == 3:
                        baseline_owners = dict(policy.exclusive_owner_by_rule)
                finally:
                    logger.close()

        cfg = _load_cfg()
        cfg["seed"] = 9999
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                self.assertEqual(baseline_owners, world.mfg_flow_task_policy.exclusive_owner_by_rule)
            finally:
                logger.close()

    def test_lpt_balance_is_no_worse_than_supply_machine_flow_cycle(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                policy = world.mfg_flow_task_policy
                lpt_loads = list(policy.worker_expected_busy_min.values())
                legacy_groups = [
                    {"replenish_s1_material", "replenish_s2_material"},
                    {
                        "load_s1_material", "setup_s1", "unload_s1", "load_s2_material",
                        "load_s2_intermediate", "setup_s2", "unload_s2",
                    },
                    {
                        "transfer_s1_to_s2", "transfer_s2_to_inspection", "load_inspection_desk",
                        "inspect_product", "unload_inspection_desk",
                        "transfer_inspection_to_completed", "dispose_inspection_scrap",
                    },
                ]
                legacy_loads = [
                    sum(float(policy.rule_metrics[rule_id]["expected_busy_min"]) for rule_id in group)
                    for group in legacy_groups
                ]
                lpt_cv = pstdev(lpt_loads) / mean(lpt_loads)
                legacy_cv = pstdev(legacy_loads) / mean(legacy_loads)
                self.assertLessEqual(lpt_cv, legacy_cv)
            finally:
                logger.close()

    def test_lpt_overflow_workers_receive_no_exclusive_production_rules(self) -> None:
        decision = yaml.safe_load(
            (ROOT / "configs" / "decision" / "immediate_dedicated_roles.yaml").read_text(encoding="utf-8")
        )

        class TimingStub:
            @staticmethod
            def expected_task_duration(_task_code: str) -> float:
                return 1.0

            @staticmethod
            def multiplier(_item_type: str, default: float = 1.0) -> float:
                return default

        fake_world = SimpleNamespace(
            material_shelf_initial_fill=30,
            objective_mode="maximize_throughput",
            configured_throughput_days=5,
            throughput_restock_interval_days=1,
            throughput_restock_target_fill=30,
            minutes_per_day=240,
            processing_time_min={1: 20.0, 2: 30.0},
            timing=TimingStub(),
            quality_cfg={"defect_prob": 0.1},
            scrap_transport_max_carry_count=3,
            inspection_workstation_id="inspection_desk",
            travel_time=lambda _source, _destination: 1.0,
        )
        policy = MfgFlowShopTaskPolicy(
            world=fake_world,
            decision_mode="immediate_dedicated_roles",
            cfg=decision["mfg_flow_shop_policy"],
            worker_ids=[f"A{index}" for index in range(1, 19)],
        )
        empty_workers = [
            worker_id for worker_id, rule_ids in policy.worker_exclusive_rules.items() if not rule_ids
        ]
        self.assertEqual(1, len(empty_workers))
        for worker_id in empty_workers:
            self.assertEqual(
                {"MANAGE_ROBOT_POWER", "REPAIR_MACHINE"},
                set(policy.summary()["workers"][worker_id]["task_codes"]),
            )
            self.assertEqual({17, 18}, set(policy.summary()["workers"][worker_id]["role_numbers"]))

    def test_scrap_candidate_maps_to_role_six(self) -> None:
        cfg = _load_cfg()
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                item_id = "PRODUCT-SCRAP-ROLE"
                world.items[item_id] = Item(item_id=item_id, item_type="product", created_at=0.0)
                world.inspection_scrap_queue.append(item_id)
                candidates = world._candidate_tasks(world.agents["A1"])
                task = next(task for task in candidates if task.priority_key == "scrap_disposal")
                rule = world._mfg_flow_task_rule(task)

                self.assertEqual(6, rule.role_number)
                self.assertEqual("COLLECT_WASTE_OR_SCRAP", rule.task_code)
                self.assertEqual("dispose_inspection_scrap", rule.rule_id)
            finally:
                logger.close()

    def test_makespan_tracks_initial_material_lineage_until_product_or_disposal(self) -> None:
        cfg = _load_cfg()
        cfg["objective"]["mode"] = "minimize_makespan"
        cfg["warehouse"]["material_shelf"].update({"capacity": 4, "initial_fill": 4})
        cfg["objective"]["throughput"]["restock_target_fill"] = 4
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(),
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                world.bootstrap()
                material_ids = sorted(world.initial_batch_material_ids)
                self.assertEqual(4, len(material_ids))

                accepted_id = "PRODUCT-ACCEPTED"
                world.items[accepted_id] = Item(item_id=accepted_id, item_type="product", created_at=0.0)
                world.items[accepted_id].metadata["source_material_ids"] = material_ids[:2]
                world._record_initial_batch_terminal_output(accepted_id, outcome="accepted_product")
                self.assertFalse(world.terminated)
                self.assertEqual(2, len(world.initial_batch_terminal_material_ids))

                scrap_id = "PRODUCT-SCRAP"
                world.items[scrap_id] = Item(item_id=scrap_id, item_type="product", created_at=0.0)
                world.items[scrap_id].metadata["source_material_ids"] = material_ids[2:]
                self.assertFalse(world.terminated)
                world._record_initial_batch_terminal_output(scrap_id, outcome="disposed_scrap")

                self.assertTrue(world.terminated)
                self.assertEqual("initial_material_batch_terminal_complete", world.termination_reason)
                kpi = world.finalize_kpis()
                self.assertEqual("complete", kpi["objective_status"])
                self.assertEqual("complete", kpi["makespan_status"])
                self.assertEqual(4, kpi["initial_batch_terminal_material_count"])
                self.assertEqual(1, kpi["initial_batch_accepted_product_count"])
                self.assertEqual(1, kpi["initial_batch_disposed_scrap_count"])
            finally:
                logger.close()

    def test_makespan_balances_initial_materials_across_processing_stations(self) -> None:
        cfg = _load_cfg()
        cfg["objective"]["mode"] = "minimize_makespan"
        cfg["warehouse"]["material_shelf"].update({"capacity": 4, "initial_fill": 4})
        cfg["objective"]["throughput"]["restock_target_fill"] = 4
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(
                    simpy.Environment(),
                    cfg,
                    logger,
                    SimpleNamespace(worker_queue_limit=8),
                )
                world.bootstrap()

                assigned_counts = {
                    station: sum(
                        1
                        for assigned_station in world.initial_batch_material_station.values()
                        if assigned_station == station
                    )
                    for station in world.stations
                }
                self.assertEqual({1: 2, 2: 2}, assigned_counts)
                self.assertEqual(2, world._material_shelf_count_for_station(1))
                self.assertEqual(2, world._material_shelf_count_for_station(2))

                supply_tasks = {
                    int(task.payload["station"]): task
                    for task in world._candidate_tasks(world.agents["A1"])
                    if task.payload.get("transfer_kind") == "material_supply"
                }
                self.assertEqual({1, 2}, set(supply_tasks))
                for station, task in supply_tasks.items():
                    slot = world._bind_available_material_shelf_slot(world.agents["A1"], task)
                    self.assertIsNotNone(slot)
                    item_id = str(task.payload["material_item_id"])
                    self.assertEqual(station, world.initial_batch_material_station[item_id])
                    world._release_task_item_reservations(task, reason="test_cleanup")
            finally:
                logger.close()

    def test_makespan_rejects_station_inventory_and_unbalanced_initial_batch(self) -> None:
        cfg = _load_cfg()
        cfg["objective"]["mode"] = "minimize_makespan"
        cfg["initial_inventory"]["material"]["station1"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                with self.assertRaisesRegex(ValueError, "initial_inventory.material"):
                    world.bootstrap()
            finally:
                logger.close()

        cfg = _load_cfg()
        cfg["objective"]["mode"] = "minimize_makespan"
        cfg["warehouse"]["material_shelf"].update({"capacity": 29, "initial_fill": 29})
        cfg["objective"]["throughput"]["restock_target_fill"] = 29
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                with self.assertRaisesRegex(ValueError, "must be divisible"):
                    world.bootstrap()
            finally:
                logger.close()

    def test_objective_controls_day_boundary_restock(self) -> None:
        for mode, expect_restock in (("maximize_throughput", True), ("minimize_makespan", False)):
            cfg = _load_cfg()
            cfg["objective"]["mode"] = mode
            cfg["warehouse"]["material_shelf"].update({"capacity": 4, "initial_fill": 4})
            cfg["objective"]["throughput"]["restock_target_fill"] = 4
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                logger = EventLogger(Path(tmp))
                try:
                    world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                    world.bootstrap()
                    occupied = [slot for _, slot in sorted(world.warehouse_material_shelf_slots.items()) if slot.get("material_item_id")]
                    for slot in occupied[:2]:
                        slot["material_item_id"] = None
                        slot["occupied"] = False

                    world.start_day(2, StrategyState(), self._job_plan())
                    expected_count = 4 if expect_restock else 2
                    self.assertEqual(expected_count, world._material_shelf_count())
                    boundary_events = [
                        event
                        for event in logger.events
                        if event.get("type") == "WAREHOUSE_MATERIAL_RESTOCK"
                        and event.get("details", {}).get("reason") == "throughput_day_boundary"
                    ]
                    self.assertEqual(1 if expect_restock else 0, len(boundary_events))
                finally:
                    logger.close()

    def test_makespan_safety_limit_is_incomplete(self) -> None:
        cfg = _load_cfg()
        cfg["objective"]["mode"] = "minimize_makespan"
        with tempfile.TemporaryDirectory() as tmp:
            logger = EventLogger(Path(tmp))
            try:
                world = ManufacturingWorld(simpy.Environment(), cfg, logger, SimpleNamespace(worker_queue_limit=8))
                world.bootstrap()
                world.terminate_at_objective_limit()
                kpi = world.finalize_kpis()
                self.assertEqual("makespan_max_days_reached", world.termination_reason)
                self.assertEqual("incomplete", kpi["objective_status"])
                self.assertEqual("incomplete", kpi["makespan_status"])
                self.assertIsNone(kpi["makespan_min"])
            finally:
                logger.close()


if __name__ == "__main__":
    unittest.main()
