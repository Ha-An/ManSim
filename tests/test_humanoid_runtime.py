from __future__ import annotations

from pathlib import Path
import itertools
from types import SimpleNamespace
import unittest

import simpy
import yaml

from humanoidsim import HumanoidProfile, expand_task_steps, load_task_catalog, validate_task_sequence
from humanoidsim.task_schema import TaskInstance

from manufacturing_sim.simulation.scenarios.manufacturing.humanoid_runtime import (
    HumanoidTaskRuntime,
    SUPPORTED_PRIMITIVE_CALLS,
    TASK_CODE_BY_PRIORITY_KEY,
)
from manufacturing_sim.simulation.scenarios.manufacturing.entities import Task, Worker
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


SELECTED_TASK_ARGS = {
    "REPLENISH_MATERIAL": {
        "item": {"entity_type": "material", "entity_id": "material_station_1"},
        "source": "Warehouse",
        "destination": "material_queue_1",
        "rule": {"station": 1, "target_level": 10},
    },
    "TRANSFER": {
        "item": {"entity_type": "product", "entity_id": "inspection_output"},
        "source": "inspection_output",
        "destination": "Warehouse",
    },
    "MANAGE_ROBOT_POWER": {
        "robot": "A1",
        "action": "swap_battery",
        "station": "battery_rack",
        "target_soc": 1.0,
    },
    "SETUP_MACHINE": {
        "machine": "S1M1",
        "setup_spec": {"station": 1},
    },
    "UNLOAD_MACHINE": {
        "machine": "S1M1",
        "item": {"entity_type": "machine_output", "entity_id": "S1M1"},
        "destination": "output_buffer_station_1",
    },
    "INSPECT_PRODUCT": {
        "target": "inspection_input_queue",
        "inspection_plan": {"station": 2, "defect_prob": 0.05},
    },
    "REPAIR_MACHINE": {
        "machine": "S1M1",
        "fault": {"state": "BROKEN", "remaining_min": 20.0},
        "repair_procedure": {"max_repair_agents": 3},
    },
    "PREVENTIVE_MAINTENANCE": {
        "asset": "S1M1",
        "checklist": {"station": 1},
    },
    "HANDOVER_ITEM": {
        "item": {"entity_type": "product", "entity_id": "PRODUCT-1"},
        "recipient": {"entity_type": "robot", "entity_id": "A1"},
        "handover_spec": {
            "mode": "product_collaboration_join",
            "source_agent_id": "A2",
            "recipient_agent_id": "A1",
            "transport_session_id": "PTX-000001",
            "destination": "warehouse_buffer",
            "max_carriers": 2,
        },
    },
    "COLLECT_WASTE_OR_SCRAP": {
        "item": {"entity_type": "scrap_batch", "entity_ids": ["SCRAP-1"]},
        "waste_or_scrap": {"entity_type": "scrap_batch", "entity_ids": ["SCRAP-1"]},
        "items": {"entity_type": "scrap_batch", "entity_ids": ["SCRAP-1"]},
        "source": "inspection_scrap_queue",
        "destination": "scrap_disposal_bin",
        "sorting_rule": {"max_carry_count": 3, "item_type": "product"},
    },
}


class HumanoidRuntimeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_task_catalog()
        cfg_path = Path(__file__).resolve().parents[1] / "configs" / "humanoidsim" / "default.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        cls.profile = HumanoidProfile.from_dict(cfg["profiles"]["A1"])

    def test_selected_task_codes_exist(self) -> None:
        for task_code in sorted(set(TASK_CODE_BY_PRIORITY_KEY.values())):
            with self.subTest(task_code=task_code):
                self.assertIsNotNone(self.catalog.get(task_code))

    def test_selected_primitives_are_supported(self) -> None:
        for task_code in sorted(set(TASK_CODE_BY_PRIORITY_KEY.values())):
            with self.subTest(task_code=task_code):
                rows = expand_task_steps(task_code, SELECTED_TASK_ARGS[task_code], catalog=self.catalog)
                missing = [
                    str(row.get("call_code"))
                    for row in rows
                    if row.get("call_level") == "PRIMITIVE_SKILL" and str(row.get("call_code")) not in SUPPORTED_PRIMITIVE_CALLS
                ]
                self.assertEqual([], missing)

    def test_nested_step_plan_preserves_child_task_rows(self) -> None:
        worker = Worker(worker_id="A1")
        world = SimpleNamespace(
            env=SimpleNamespace(now=0.0),
            agents={"A1": worker},
            battery_remaining=lambda _worker: 100.0,
            _task_priority_key=lambda _task: "material_supply",
            inventory_targets={"material": {"station1": 10}},
        )
        runtime = HumanoidTaskRuntime(world, {"humanoidsim": {"enabled": True}})
        task = Task(
            task_id="TASK-1",
            task_type="REPLENISH_MATERIAL",
            priority_key="material_supply",
            priority=1.0,
            location="Warehouse",
            payload={"station": 1},
        )
        bound = runtime.bind_candidate(worker, task)
        self.assertIsNotNone(bound)
        assert bound is not None
        transfer_rows = [row for row in bound.step_plan if row.get("call_code") == "TRANSFER"]
        self.assertEqual(1, len(transfer_rows))
        self.assertEqual("ATOMIC_TASK", transfer_rows[0].get("call_level"))
        self.assertTrue(any(row.get("parent_task_code") == "TRANSFER" and row.get("call_code") == "GRASP" for row in bound.step_plan))

    def test_default_profile_validates_selected_task_subset(self) -> None:
        instances = [
            TaskInstance(
                instance_id=f"test-{task_code}",
                task_code=task_code,
                args=SELECTED_TASK_ARGS[task_code],
                assigned_robot_id="A1",
            )
            for task_code in sorted(set(TASK_CODE_BY_PRIORITY_KEY.values()))
        ]
        result = validate_task_sequence({"A1": self.profile}, instances, catalog=self.catalog)
        self.assertTrue(result.ok, [issue.to_dict() for issue in result.issues])

    def test_missing_capability_rejects_power_management(self) -> None:
        limited = HumanoidProfile.from_dict(
            {
                "humanoid_id": "A1",
                "capabilities": ["navigation"],
                "supported_tools": ["*"],
                "supported_vehicles": ["*"],
                "supported_equipment": ["*"],
            }
        )
        instance = TaskInstance(
            instance_id="test-power",
            task_code="MANAGE_ROBOT_POWER",
            args=SELECTED_TASK_ARGS["MANAGE_ROBOT_POWER"],
            assigned_robot_id="A1",
        )
        result = validate_task_sequence({"A1": limited}, [instance], catalog=self.catalog)
        self.assertFalse(result.ok)

    def test_humanoid_state_bridge_tracks_task_and_primitive(self) -> None:
        worker = Worker(worker_id="A1")
        world = SimpleNamespace(
            env=SimpleNamespace(now=0.0),
            agents={"A1": worker},
            battery_remaining=lambda _worker: 100.0,
        )
        runtime = HumanoidTaskRuntime(world, {"humanoidsim": {"enabled": True}})
        task = Task(
            task_id="TASK-1",
            task_type="TRANSFER",
            priority_key="inter_station_transfer",
            priority=1.0,
            location="Warehouse",
            task_code="TRANSFER",
            instance_id="TASK-1:TRANSFER",
            assigned_robot_id="A1",
        )
        worker.current_task_id = task.task_id
        worker.current_task_type = task.task_type
        worker.current_task_code = task.task_code
        worker.current_task_instance_id = task.instance_id

        runtime.transition_state(worker, "task_assigned", task=task, reason_code="task_selected", source="test")
        self.assertEqual(worker.humanoid_state["availability"], "ASSIGNED")
        self.assertEqual(worker.humanoid_state["task_context"]["task_code"], "TRANSFER")

        runtime.set_step_state(worker, task, {"step_id": "s1", "call_code": "NAVIGATE_TO"}, event_type="HUMANOID_STEP_START", status="running")
        self.assertEqual(worker.humanoid_state["availability"], "EXECUTING")
        self.assertEqual(worker.humanoid_state["mobility"], "NAVIGATING")
        self.assertEqual(worker.humanoid_state["task_context"]["primitive_call_code"], "NAVIGATE_TO")

        runtime.set_step_state(worker, task, {"step_id": "s1", "call_code": "NAVIGATE_TO"}, event_type="HUMANOID_STEP_END", status="completed")
        self.assertEqual(worker.humanoid_state["mobility"], "STATIONARY")

        runtime.set_step_state(worker, task, {"step_id": "s2", "call_code": "GRASP"}, event_type="HUMANOID_STEP_START", status="running")
        self.assertEqual(worker.humanoid_state["manipulation"], "HOLDING")

        runtime.set_disabled_state(worker, reason="battery_depleted")
        self.assertEqual(worker.humanoid_state["availability"], "DISABLED")
        self.assertEqual(worker.humanoid_state["power"], "DEPLETED")
        self.assertEqual(worker.humanoid_state["reason"]["code"], "battery_depleted")

    def test_domain_internal_primitive_hint_updates_current_context(self) -> None:
        worker = Worker(worker_id="A1")
        world = ManufacturingWorld.__new__(ManufacturingWorld)
        world.env = SimpleNamespace(now=0.0)
        world.agents = {"A1": worker}
        world.battery_remaining = lambda _worker: 100.0
        runtime = HumanoidTaskRuntime(world, {"humanoidsim": {"enabled": True}})
        world.humanoid_runtime = runtime
        worker.current_task_id = "TASK-1"
        worker.current_task_type = "INSPECT_PRODUCT"
        worker.current_task_code = "INSPECT_PRODUCT"
        worker.current_task_instance_id = "TASK-1:INSPECT_PRODUCT"
        worker.current_step_id = "s03_execute_quality_action"
        worker.current_primitive_call_code = "EXECUTE_QUALITY_ACTION"

        ManufacturingWorld._set_humanoid_primitive_hint(world, worker, "NAVIGATE_TO")

        self.assertEqual(worker.current_primitive_call_code, "NAVIGATE_TO")
        self.assertEqual(worker.humanoid_state["mobility"], "NAVIGATING")
        self.assertEqual(worker.humanoid_state["task_context"]["primitive_call_code"], "NAVIGATE_TO")

    def test_non_domain_primitive_consumes_minimum_duration(self) -> None:
        env = simpy.Environment()
        worker = Worker(worker_id="A1")
        world = SimpleNamespace(
            env=env,
            agents={"A1": worker},
            battery_remaining=lambda _worker: 100.0,
        )
        runtime = HumanoidTaskRuntime(world, {"humanoidsim": {"enabled": True, "primitive_timing": {"unit": "min", "default_min": 0.1}}})
        task = Task(
            task_id="TASK-1",
            task_type="REPLENISH_MATERIAL",
            priority_key="material_supply",
            priority=1.0,
            location="Warehouse",
            task_code="REPLENISH_MATERIAL",
            instance_id="TASK-1:REPLENISH_MATERIAL",
            assigned_robot_id="A1",
        )
        result_holder: dict[str, bool] = {}

        def run_step():
            result_holder["ok"] = yield from runtime._execute_step(worker, task, {"step_id": "s01", "call_code": "CHECK_REQUEST"}, False)

        env.process(run_step())
        env.run()
        self.assertTrue(result_holder["ok"])
        self.assertAlmostEqual(env.now, 0.1)

    def test_product_transport_multiplier_divides_after_helper_join(self) -> None:
        world = ManufacturingWorld.__new__(ManufacturingWorld)
        world.item_transport_weight_multiplier = {
            "material": 1.0,
            "intermediate": 1.5,
            "product": 2.0,
        }
        world.product_collaboration_divide_time = True
        world.product_transport_sessions = {}
        world.product_transport_session_by_worker = {}

        worker = Worker(worker_id="A1", carrying_item_id="PRODUCT-1", carrying_item_type="product")
        world.workers = {"A1": worker, "A2": Worker(worker_id="A2")}
        worker.transport_session_id = "PTX-1"
        world.product_transport_session_by_worker["A1"] = "PTX-1"
        world.product_transport_sessions["PTX-1"] = {"status": "active", "carrier_ids": ["A1"]}

        self.assertAlmostEqual(world._current_transport_time_multiplier(worker), 2.0)
        world.product_transport_sessions["PTX-1"]["carrier_ids"] = ["A1", "A2"]
        self.assertAlmostEqual(world._current_transport_time_multiplier(worker), 1.0)

        worker.transport_session_id = None
        worker.carrying_item_type = "intermediate"
        self.assertAlmostEqual(world._current_transport_time_multiplier(worker), 1.5)

    def test_handover_candidate_only_for_active_product_transport(self) -> None:
        world = ManufacturingWorld.__new__(ManufacturingWorld)
        world.product_collaboration_enabled = True
        world.product_collaboration_max_carriers = 2
        world.product_collaboration_divide_time = True
        world.task_counter = itertools.count(1)
        world.env = SimpleNamespace(now=10.0)
        world.grid_map = None
        world.product_transport_sessions = {
            "PTX-1": {
                "session_id": "PTX-1",
                "status": "active",
                "item_id": "PRODUCT-1",
                "item_type": "product",
                "primary_worker_id": "A1",
                "carrier_ids": ["A1"],
                "destination": "warehouse_buffer",
                "max_carriers": 2,
            }
        }
        world.product_transport_session_by_item = {"PRODUCT-1": "PTX-1"}
        world.product_transport_session_by_worker = {"A1": "PTX-1"}
        primary = Worker(worker_id="A1", location="Inspection", carrying_item_id="PRODUCT-1", carrying_item_type="product")
        helper = Worker(worker_id="A2", location="Warehouse")
        world.workers = {"A1": primary, "A2": helper}
        world.agents = world.workers
        world._product_session_has_remaining_path = lambda _session: True  # type: ignore[method-assign]
        world._product_session_join_feasible = lambda _agent, _session: True  # type: ignore[method-assign]

        candidates = world._handover_item_candidates(helper, 100.0)

        self.assertEqual(1, len(candidates))
        self.assertEqual("HANDOVER_ITEM", candidates[0].task_type)
        self.assertEqual("handover_item", candidates[0].priority_key)
        self.assertEqual("PTX-1", candidates[0].payload["transport_session_id"])

    def test_handover_candidate_rejects_stale_product_transport(self) -> None:
        world = ManufacturingWorld.__new__(ManufacturingWorld)
        world.product_collaboration_enabled = True
        world.product_collaboration_max_carriers = 2
        world.product_collaboration_divide_time = True
        world.task_counter = itertools.count(1)
        world.env = SimpleNamespace(now=10.0)
        world.grid_map = None
        world.product_transport_sessions = {
            "PTX-1": {
                "session_id": "PTX-1",
                "status": "active",
                "item_id": "PRODUCT-1",
                "item_type": "product",
                "primary_worker_id": "A1",
                "carrier_ids": ["A1"],
                "destination": "warehouse_buffer",
                "max_carriers": 2,
            }
        }
        primary = Worker(worker_id="A1", location="Inspection", carrying_item_id="PRODUCT-1", carrying_item_type="product")
        helper = Worker(worker_id="A2", location="Warehouse")
        world.workers = {"A1": primary, "A2": helper}
        world.agents = world.workers
        world._product_session_has_remaining_path = lambda _session: True  # type: ignore[method-assign]
        world._product_session_join_feasible = lambda _agent, _session: False  # type: ignore[method-assign]

        self.assertEqual([], world._handover_item_candidates(helper, 100.0))

    def test_transport_metrics_include_shared_carry_collaboration(self) -> None:
        world = ManufacturingWorld.__new__(ManufacturingWorld)
        world.logger = SimpleNamespace(
            events=[
                {
                    "t": 5.0,
                    "type": "PRODUCT_CARRY_JOINED",
                    "entity_id": "PRODUCT-1",
                    "details": {"helper_worker_id": "A2", "carrier_ids": ["A1", "A2"]},
                },
                {
                    "t": 12.0,
                    "type": "PRODUCT_CARRY_COMPLETED",
                    "entity_id": "PRODUCT-1",
                    "details": {
                        "duration": 12.0,
                        "shared_duration": 7.0,
                        "carrier_count": 2,
                        "carrier_ids": ["A1", "A2"],
                    },
                },
            ]
        )

        metrics = world._transport_metrics()

        self.assertEqual(1, metrics["handover_item_count"])
        self.assertEqual(1, metrics["shared_product_carry_completed_count"])
        self.assertAlmostEqual(12.0, metrics["product_carry_time_min"])
        self.assertAlmostEqual(7.0, metrics["shared_product_carry_time_min"])
        self.assertAlmostEqual(5.0, metrics["solo_product_carry_time_min"])
        self.assertAlmostEqual(7.0 / 12.0, metrics["shared_product_carry_ratio"], places=6)
        self.assertEqual({"A1": 7.0, "A2": 7.0}, metrics["shared_product_carry_time_by_worker"])
        self.assertEqual({"A1 / A2": 7.0}, metrics["shared_product_carry_time_by_pair"])

    def test_repair_collaboration_metrics_integrate_team_size(self) -> None:
        world = ManufacturingWorld.__new__(ManufacturingWorld)
        world.env = SimpleNamespace(now=20.0)
        world.logger = SimpleNamespace(
            events=[
                {
                    "t": 0.0,
                    "type": "MACHINE_REPAIR_START",
                    "entity_id": "S1M1",
                    "details": {"by": "A1", "repair_team": ["A1"], "repair_team_size": 1},
                },
                {
                    "t": 5.0,
                    "type": "MACHINE_REPAIR_HELPER_JOIN",
                    "entity_id": "S1M1",
                    "details": {"by": "A2", "repair_team": ["A1", "A2"], "repair_team_size": 2},
                },
                {
                    "t": 15.0,
                    "type": "MACHINE_REPAIR_HELPER_LEAVE",
                    "entity_id": "S1M1",
                    "details": {"by": "A2", "repair_team": ["A1"], "repair_team_size": 1},
                },
                {
                    "t": 20.0,
                    "type": "MACHINE_REPAIRED",
                    "entity_id": "S1M1",
                    "details": {"by": "A1", "repair_team": ["A1"], "repair_team_size": 1},
                },
            ]
        )

        metrics = world._repair_collaboration_metrics()

        self.assertEqual(1, metrics["repair_helper_join_count"])
        self.assertEqual({"S1M1": 1}, metrics["repair_helper_join_count_by_machine"])
        self.assertEqual({"A2": 1}, metrics["repair_helper_join_count_by_worker"])
        self.assertEqual({"1": 10.0, "2": 10.0}, metrics["repair_team_time_by_size"])
        self.assertAlmostEqual(10.0, metrics["repair_collaboration_time_min"])
        self.assertAlmostEqual(10.0, metrics["repair_solo_time_min"])
        self.assertAlmostEqual(0.5, metrics["repair_collaboration_ratio"])
        self.assertAlmostEqual(1.5, metrics["repair_team_size_avg"])
        self.assertEqual({"S1M1": 10.0}, metrics["repair_collaboration_time_by_machine"])
        self.assertEqual({"A1": 10.0, "A2": 10.0}, metrics["repair_collaboration_time_by_worker"])
        episodes = metrics["repair_collaboration_episodes"]
        self.assertEqual(1, len(episodes))
        self.assertEqual("S1M1", episodes[0]["machine_id"])
        self.assertAlmostEqual(0.0, episodes[0]["started_at"])
        self.assertAlmostEqual(20.0, episodes[0]["ended_at"])
        self.assertAlmostEqual(20.0, episodes[0]["active_repair_time_min"])
        self.assertAlmostEqual(10.0, episodes[0]["collaboration_time_min"])
        self.assertEqual(2, episodes[0]["max_team_size"])
        self.assertEqual(1, episodes[0]["helper_join_count"])
        self.assertEqual({"1": 10.0, "2": 10.0}, episodes[0]["team_time_by_size"])
        self.assertEqual(["A1"], episodes[0]["final_team"])
        self.assertEqual("completed", episodes[0]["status"])

    def test_precondition_failed_task_end_sets_blocked_availability(self) -> None:
        events: list[dict] = []
        world = ManufacturingWorld.__new__(ManufacturingWorld)
        world.env = SimpleNamespace(now=12.0)
        world.logger = SimpleNamespace(log=lambda **payload: events.append(payload))
        world.task_records = []
        world.product_transport_session_by_worker = {}
        world.product_transport_sessions = {}
        world.day_for_time = lambda _t: 1  # type: ignore[method-assign]
        world.worker_display_location = lambda worker: worker.location  # type: ignore[method-assign]
        world.battery_remaining = lambda _worker: 100.0  # type: ignore[method-assign]
        worker = Worker(worker_id="A2", location="warehouse_material_slot_01")
        world.agents = {"A2": worker}
        world.humanoid_runtime = HumanoidTaskRuntime(world, {"humanoidsim": {"enabled": True}})
        task = Task(
            task_id="MAT-1",
            task_type="TRANSFER",
            priority_key="material_supply",
            priority=85.0,
            location="Warehouse",
            payload={"transfer_kind": "material_supply", "station": 1},
            task_code="REPLENISH_MATERIAL",
            instance_id="MAT-1",
            assigned_robot_id="A2",
            task_spec_name="REPLENISH_MATERIAL",
        )

        world.finish_agent_task(worker, task, start_t=10.0, status="skipped", reason="material_shelf_slot_empty")

        self.assertEqual("BLOCKED", worker.humanoid_state["availability"])
        task_end = next(event for event in events if event["event_type"] == "AGENT_TASK_END")
        self.assertEqual("material_shelf_slot_empty", task_end["details"]["reason"])
        self.assertEqual("skipped", world.task_records[-1]["status"])
        self.assertEqual("material_supply", world.task_records[-1]["priority_key"])

    def test_legacy_state_contract_removed(self) -> None:
        root = Path(__file__).resolve().parents[1]
        checked_roots = ["manufacturing_sim", "dashboards", "replay_studio/src", "replay_studio/examples", "docs", "README.md"]
        forbidden = ["Worker" + "State", "worker" + "_state_time" + "_by_worker", "state_for_worker" + "_state"]
        offenders: list[str] = []
        for relative in checked_roots:
            path = root / relative
            files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
            for file_path in files:
                if file_path.suffix not in {".py", ".md", ".ts", ".tsx"}:
                    continue
                text = file_path.read_text(encoding="utf-8", errors="ignore")
                for token in forbidden:
                    if token in text:
                        offenders.append(f"{file_path.relative_to(root)}:{token}")
        self.assertEqual([], offenders)


if __name__ == "__main__":
    unittest.main()
