from __future__ import annotations

import dataclasses
import random
from unittest.mock import patch

import numpy as np
import pytest
import simpy

from manufacturing_sim.adp.compact import compact_episode_transitions, merge_compact_batches, select_compact_samples
from manufacturing_sim.adp.checkpoint import checkpoint_fingerprint, load_checkpoint, save_checkpoint
from manufacturing_sim.adp.encoding import ADPStateEncoder
from manufacturing_sim.adp.model import build_value_network, collate_states, predict_values, require_torch
from manufacturing_sim.adp.policy import greedy_beam_matching, unique_feasible_assignment
from manufacturing_sim.adp.td import CompactTDData, ReplayTask, build_target_plan, sample_replay_episodes
from manufacturing_sim.adp.train import _compose_episode_cfg, InMemoryEventLogger
from agents.factory import build_decision_module
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld
from test_adp_td import episode, state


@pytest.fixture
def torch():
    result = require_torch()
    result.set_num_threads(1)
    return result


@pytest.fixture
def world():
    cfg = _compose_episode_cfg(worker_count=3, seed=2026, days=1, adp_cfg={"force_random_policy": True})
    return ManufacturingWorld(simpy.Environment(), cfg, InMemoryEventLogger(),
        build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"))


def assert_batch_equal(torch, left, right):
    for field in dataclasses.fields(left):
        if field.name != "td_data":
            assert torch.equal(getattr(left, field.name), getattr(right, field.name)), field.name
    if left.td_data is not None:
        assert_batch_equal(torch, left.td_data.pre, right.td_data.pre)
        assert left.td_data.descriptors == right.td_data.descriptors
        assert torch.equal(left.td_data.rewards, right.td_data.rewards)
        assert torch.equal(left.td_data.dones, right.td_data.dones)


def test_select_before_merge_matches_legacy_rng_order_padding_and_tensors(torch):
    batches = [merge_compact_batches([episode(i)[0] for i in range(1, 4)]), episode(4)[0], episode(5)[0]]
    # Retained but not sampled wide padding must still be preserved.
    batches[0] = merge_compact_batches([batches[0]], padding_shape=(6, 7))
    full = merge_compact_batches(batches)
    for seed in range(10):
        before, after = random.Random(seed), random.Random(seed)
        selected = {5, *before.sample([1, 2, 3, 4], 2)}
        expected = select_compact_samples(full, [i for i, e in enumerate(full.episode_ids.tolist()) if e in selected])
        actual = sample_replay_episodes(batches, required_episode_ids={5}, episode_count=3, rng=after)
        assert before.getstate() == after.getstate()
        assert_batch_equal(torch, expected, actual)


def test_cpu_collation_matches_compact_and_device_inputs(torch):
    states = [state(), state(2)]
    states[0] = states[0].post_decision({"A1": "O1", "A2": None})
    empty = state(3)
    empty.opportunity_ids = []
    empty.task_features = empty.task_features[:0]
    empty.pair_features = empty.pair_features[:, :0]
    empty.feasibility = empty.feasibility[:, :0]
    empty.selected_assignment_mask = empty.selected_assignment_mask[:, :0]
    states.append(empty)
    compact = compact_episode_transitions([{"post_state": s, "raw_reward": 0} for s in states], episode_id=1, worker_count=2)
    for device in ["cpu"] + (["cuda:0"] if torch.cuda.is_available() else []):
        actual = collate_states(states, device)
        expected, _ = compact.model_batch(list(range(3)), device)
        for name in expected:
            assert torch.equal(actual[name], expected[name]), name


def test_unique_assignment_respects_resources_repair_wait_and_worker_order(torch):
    model = build_value_network(embedding_dim=8, heads=2, layers=1)
    for kind in ["TRANSFER", "REPAIR_MACHINE"]:
        for capacity in [1, 2, 3]:
            for order in [["A1", "A2"], ["A2", "A1"]]:
                for wait in [False, True]:
                    s = state()
                    s.tasks_by_worker = {w: {"O1": ReplayTask(kind, {"_adp_resource_keys": ["machine:M"]})} for w in order}
                    unique = unique_feasible_assignment(s, worker_order=order, repair_capacity=capacity, allow_wait_action=wait)
                    if wait:
                        assert unique is None
                    else:
                        reference = greedy_beam_matching(s, model=model, device="cpu", worker_order=order, repair_capacity=capacity)
                        assert unique == reference.assignment
                        assert sum(v is not None for v in unique.values()) == (min(capacity, 2) if kind == "REPAIR_MACHINE" else 1)
    assert unique_feasible_assignment(state(), worker_order=["A1", "A2"], repair_capacity=3, allow_wait_action=False) is None


def test_forced_actions_skip_online_network_but_keep_td_bootstrap(torch):
    rows = []
    for i in range(3):
        s = state(float(i))
        s.tasks_by_worker = {}
        rows.append({"state": s, "post_state": s.post_decision({"A1": None, "A2": None}), "raw_reward": float(i), "done": i == 2})
    batch = compact_episode_transitions(rows, episode_id=1, worker_count=2)
    batch.td_data = CompactTDData.from_transitions(rows, 1, 2)
    model = build_value_network(embedding_dim=8, heads=2, layers=1)
    with patch("manufacturing_sim.adp.td.greedy_beam_matching", side_effect=AssertionError("redundant online call")):
        actual = build_target_plan(batch, model, "cpu", n=1, beam_width=8, worker_order_strategy="cyclic")
    with patch("manufacturing_sim.adp.td.unique_feasible_assignment", return_value=None):
        expected = build_target_plan(batch, model, "cpu", n=1, beam_width=8, worker_order_strategy="cyclic")
    assert_batch_equal(torch, actual.greedy_posts, expected.greedy_posts)
    assert actual.reasons == expected.reasons
    assert torch.equal(actual.endpoints, expected.endpoints)
    assert torch.equal(actual.targets([0, 1, 2], model, "cpu"), expected.targets([0, 1, 2], model, "cpu"))


def test_validation_schema_cached_but_invalid_snapshots_still_fail(world):
    runtime = world.humanoid_runtime
    worker = world.workers["A1"]
    with patch("humanoidsim.state_schema.load_state_schema", side_effect=AssertionError("schema reloaded")):
        for _ in range(3):
            runtime._normalize_state_payload(worker, {})
            runtime.transition_state(worker, "available")
        with pytest.raises(RuntimeError, match="validation issues"):
            runtime._normalize_state_payload(worker, {"availability": "INVALID"})


def test_actual_warehouse_occupancy_and_clock_zero(world):
    world._ensure_material_shelf_slots()
    world._restock_material_shelf(reason="test", target_fill=30)
    before = ADPStateEncoder().encode(world, [], {})
    occupied = [slot for slot in world.warehouse_material_shelf_slots.values() if slot.get("material_item_id")]
    assert occupied
    occupied[0]["material_item_id"] = None
    after = ADPStateEncoder().encode(world, [], {})
    assert float(before.global_features[3] - after.global_features[3]) == pytest.approx(1 / 30)
    worker = world.workers["A1"]
    worker.current_task_code = "INSPECT_PRODUCT"
    worker.current_task_started_at = 0.0
    world.env.run(until=1)
    encoded = ADPStateEncoder().encode(world, [], {})
    expected = max(0.0, world.timing.expected_task_duration("INSPECT_PRODUCT") - 1) / 60
    assert float(encoded.worker_features[0, 14]) == pytest.approx(expected)
    world.product_count = 15
    world.scrap_count = 3
    from manufacturing_sim.adp.schema import COMPLETED_PRODUCT_COUNT_INDEX, REMAINING_HORIZON_INDEX

    encoded = ADPStateEncoder().encode(world, [], {})
    assert float(encoded.global_features[COMPLETED_PRODUCT_COUNT_INDEX]) == pytest.approx(0.5)
    assert float(encoded.global_features[REMAINING_HORIZON_INDEX]) == pytest.approx(479 / 480)


def test_machine_remaining_time_read_is_live_and_nonmutating(world):
    machine = world.machines["S1M1"]
    cycle = world.start_machine_cycle(machine)
    sampled = machine.cycle_remaining_process_min
    world.env.run(until=3)
    assert world.machine_remaining_processing_min(machine) == pytest.approx(sampled - 3)
    assert machine.cycle_remaining_process_min == sampled
    encoded = ADPStateEncoder().encode(world, [], {})
    assert float(encoded.global_features[20]) == pytest.approx((sampled - 3) / machine.process_time_min)
    world.abort_machine_cycle(machine, cycle, "test", elapsed_min=3)
    world.env.run(until=5)
    assert world.machine_remaining_processing_min(machine) == pytest.approx(sampled - 3)
    assert world.start_machine_cycle(machine) == cycle
    world.env.run(until=6)
    assert world.machine_remaining_processing_min(machine) == pytest.approx(sampled - 4)
    assert machine.cycle_sampled_process_min == sampled


def test_duration_reuse_has_identical_battery_risk_metadata(world):
    worker = world.workers["A1"]
    for task in world._candidate_tasks(worker):
        duration = world._task_estimated_duration(worker, task)
        assert world._task_battery_risk_metadata(worker, task) == world._task_battery_risk_metadata(worker, task, estimated_duration=duration)


def test_old_schema_rejected_before_loading_weights(world, torch, tmp_path):
    model = build_value_network(embedding_dim=8, heads=2, layers=1)
    manifest = {**checkpoint_fingerprint(world), "feature_schema_version": "mfg_flow_shop_adp_v9"}
    path = tmp_path / "v9.pt"
    save_checkpoint(path, model=model, optimizer=None, manifest=manifest)
    with pytest.raises(RuntimeError, match="feature_schema_version"):
        load_checkpoint(path, world=world, device="cpu")
