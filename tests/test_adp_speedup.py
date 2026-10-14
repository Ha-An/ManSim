from __future__ import annotations

import copy
import dataclasses
import random
from unittest.mock import patch

import numpy as np
import pytest

from manufacturing_sim.adp import train
from manufacturing_sim.adp.compact import compact_episode_transitions, merge_compact_batches
from manufacturing_sim.adp.model import build_value_network, require_torch
from manufacturing_sim.adp.policy import greedy_beam_matching, greedy_beam_matching_many
from manufacturing_sim.adp.schema import (EncodedDecisionState, GLOBAL_FEATURE_DIM, WORKER_FEATURE_DIM,
                                         TASK_FEATURE_DIM, PAIR_FEATURE_DIM)
from manufacturing_sim.adp.td import CompactTDData, ReplayTask, build_target_plan, fit_td_value


@pytest.fixture
def torch():
    torch = require_torch()
    torch.set_num_threads(1)
    torch.manual_seed(123)
    return torch


def states_for_test():
    rng = np.random.default_rng(44)
    states = []
    for count, tasks in [(2, 4), (3, 5), (6, 4), (3, 0), (2, 4), (3, 5)]:
        workers = [f"A{i+1}" for i in range(count)]
        opportunities = [f"O{i+1}" for i in range(tasks)]
        s = EncodedDecisionState(
            global_features=rng.random(GLOBAL_FEATURE_DIM).astype(np.float32),
            worker_features=rng.random((count, WORKER_FEATURE_DIM)).astype(np.float32),
            task_features=rng.random((tasks, TASK_FEATURE_DIM)).astype(np.float32),
            pair_features=rng.random((count, tasks, PAIR_FEATURE_DIM)).astype(np.float32),
            feasibility=np.ones((count, tasks), bool), worker_ids=workers, opportunity_ids=opportunities,
            decision_worker_ids=workers[::2] if count == 6 else workers,
            time_min=0, horizon_min=100, wait_action_enabled=len(states) % 2 == 0,
            tasks_by_worker={w: {o: ReplayTask("REPAIR_MACHINE" if o == "O1" else "TRANSFER",
                {"_adp_resource_keys": ["machine:M"] if o == "O1" else [f"item:{o}"]})
                for o in opportunities} for w in workers},
        )
        states.append(s)
    return states


@pytest.mark.parametrize("device", ["cpu", "cuda:0"])
@pytest.mark.parametrize("width", [2, 64])
def test_batched_search_matches_reference_with_padding_conflicts_and_partial_workers(torch, device, width):
    if device.startswith("cuda") and not torch.cuda.is_available():
        pytest.skip("CUDA unavailable")
    model = build_value_network(embedding_dim=16, heads=2, layers=1).to(device).eval()
    states = states_for_test()
    orders = [s.decision_worker_ids[::-1] for s in states]
    reference = [greedy_beam_matching(s, model=model, device=device, beam_width=width,
                 worker_order=order, repair_capacity=2, allow_wait_action=s.wait_action_enabled)
                 for s, order in zip(states, orders)]
    actual = greedy_beam_matching_many(states, model=model, device=device, beam_width=width,
                                     worker_orders=orders, repair_capacities=[2] * len(states),
                                     candidate_batch_size=7)
    for before, after in zip(reference, actual):
        assert before.assignment == after.assignment
        assert before.worker_order == after.worker_order
        assert before.candidate_matching_count == after.candidate_matching_count
        assert after.predicted_value == pytest.approx(before.predicted_value, abs=1e-5, rel=1e-6)


def test_exact_ties_use_scalar_tie_break(torch):
    model = build_value_network(embedding_dim=8, heads=2, layers=1).eval()
    for p in model.parameters():
        p.data.zero_()
    states = states_for_test()[:2]
    stats = {}
    actual = greedy_beam_matching_many(states, model=model, device="cpu", beam_width=2,
        worker_orders=[s.decision_worker_ids for s in states], repair_capacities=[3, 3], stats=stats)
    assert stats["scalar_tie_fallbacks"] == 2
    for s, result in zip(states, actual):
        assert result.assignment == greedy_beam_matching(s, model=model, device="cpu", beam_width=2,
                                                       allow_wait_action=s.wait_action_enabled).assignment


@pytest.mark.parametrize("device", ["cpu", "cuda:0"])
def test_full_td_plan_and_update_equivalence(torch, device):
    if device.startswith("cuda") and not torch.cuda.is_available():
        pytest.skip("CUDA unavailable")
    batches = []
    for eid in [1, 10]:
        rows = []
        for i, s in enumerate(states_for_test()):
            s.time_min = float(i)
            assignment = {w: None for w in s.decision_worker_ids}
            if s.opportunity_ids:
                assignment[s.decision_worker_ids[0]] = s.opportunity_ids[0]
            rows.append(dict(state=s, post_state=s.post_decision(assignment), raw_reward=float(i % 2), done=i == 5))
        batch = compact_episode_transitions(rows, episode_id=eid, worker_count=3)
        batch.td_data = CompactTDData.from_transitions(rows, eid, 3)
        batches.append(batch)
    samples = merge_compact_batches(batches)
    initial = build_value_network(embedding_dim=8, heads=2, layers=1).to(device).eval()
    plans, models = [], []
    for size in [1, 64]:
        model, target = copy.deepcopy(initial), copy.deepcopy(initial)
        plan = build_target_plan(samples, model, device, n=3, beam_width=2,
                                 worker_order_strategy="cyclic", state_batch_size=size)
        fit_td_value(model=model, target_model=target, optimizer=torch.optim.Adam(model.parameters(), lr=1e-4),
                     samples=samples, plan=plan, device=device, batch_size=4, epochs=2, gradient_clip=.5,
                     target_tau=.03, rng=random.Random(88))
        plans.append(plan)
        models.append(model)
    assert plans[0].steps == plans[1].steps
    assert plans[0].reasons == plans[1].reasons
    assert torch.equal(plans[0].endpoints, plans[1].endpoints)
    assert torch.equal(plans[0].greedy_posts.selected_assignment_mask, plans[1].greedy_posts.selected_assignment_mask)
    for field in dataclasses.fields(plans[0].greedy_posts):
        if field.name != "td_data":
            assert torch.equal(getattr(plans[0].greedy_posts, field.name), getattr(plans[1].greedy_posts, field.name))
    for a, b in zip(models[0].parameters(), models[1].parameters()):
        assert torch.equal(a, b)


def test_persistent_worker_loads_actual_weights_and_rejects_wrong_hash(torch):
    cfg = dict(embedding_dim=8, heads=2, layers=1)
    train._initialize_rollout_process(None, cfg, 1)
    model = build_value_network(**cfg).eval()
    state, snapshot = train._snapshot_state_dict(model)
    job = train.RolloutJob(1, "test", 0, 3, 123, 1, .2, False, {}, True, 1., "wave", snapshot)
    dummy = train.EpisodeResult(1, "test", 3, 123, 1, 0, 1, 2, {})
    with patch.object(train, "_execute_rollout_job", return_value=(dummy, None)):
        result, _ = train._execute_snapshot_rollout(job, state)
        assert result.snapshot_hash == snapshot
        with torch.no_grad():
            model.value_head[-1].bias.add_(.5)
        new_state, new_hash = train._snapshot_state_dict(model)
        result, _ = train._execute_snapshot_rollout(dataclasses.replace(job, snapshot_hash=new_hash), new_state)
        assert result.snapshot_hash == new_hash != snapshot
        assert train._snapshot_state_dict(train._ROLLOUT_MODEL)[1] == new_hash
        with pytest.raises(RuntimeError, match="hash"):
            train._execute_snapshot_rollout(dataclasses.replace(job, snapshot_hash="incorrect"), state)
        result, _ = train._execute_snapshot_rollout(dataclasses.replace(job, force_random=True, snapshot_hash="RANDOM"), None)
        assert result.snapshot_hash == "RANDOM"
        assert train._ROLLOUT_MODEL is None


def test_pool_lifecycle_and_configuration(torch):
    cfg = dict(embedding_dim=8, heads=2, layers=1)
    pool = train.RolloutPool(process_count=2, model_cfg=cfg, torch_threads=1)
    pool.validate(2, cfg, 1, "spawn")
    with pytest.raises(ValueError, match="configuration"):
        pool.validate(3, cfg, 1, "spawn")
    pool.close()
    with pytest.raises(RuntimeError, match="closed"):
        pool.validate(2, cfg, 1, "spawn")


def test_target_batch_config_rejects_invalid_sizes():
    from omegaconf import OmegaConf
    from manufacturing_sim.adp.td_train import validate_td_config
    cfg = OmegaConf.to_container(OmegaConf.load("configs/adp/mfg_flow_shop_throughput.yaml"), resolve=True)
    for key in ("target_state_batch_size", "target_candidate_batch_size"):
        changed = copy.deepcopy(cfg)
        changed["training"][key] = 0
        with pytest.raises(ValueError, match=key):
            validate_td_config(changed)


def test_benchmark_optimizer_trials_do_not_mutate_checkpoint(torch):
    from scripts.benchmark_adp_speedup import independent_optimizer
    model = torch.nn.Linear(2, 1)
    original = torch.optim.Adam(model.parameters())
    model(torch.ones(1, 2)).sum().backward()
    original.step()
    checkpoint = copy.deepcopy(original.state_dict())
    for _ in range(2):
        trial_model = copy.deepcopy(model)
        trial = independent_optimizer(trial_model, checkpoint)
        trial_model(torch.ones(1, 2)).sum().backward()
        trial.step()
        assert all(float(state["step"]) == 2 for state in trial.state.values())
        assert all(float(state["step"]) == 1 for state in checkpoint["state"].values())
