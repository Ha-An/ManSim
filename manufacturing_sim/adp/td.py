"""Compact sequence replay and truncated off-policy n-step afterstate TD."""
from __future__ import annotations

from dataclasses import dataclass
import json
import random
from typing import Any

import numpy as np

from .compact import CompactMCBatch, compact_episode_transitions, merge_compact_batches, select_compact_samples
from .model import predict_values, require_torch
from .policy import greedy_beam_matching, unique_feasible_assignment
from .schema import EncodedDecisionState


@dataclass(frozen=True)
class ReplayTask:
    task_type: str
    payload: dict[str, Any]


@dataclass
class CompactTDData:
    pre: CompactMCBatch
    rewards: Any
    dones: Any
    descriptors: list[dict[str, Any]]

    @property
    def memory_bytes(self) -> int:
        # Metadata is small, immutable-in-use JSON-compatible data, not Task/world objects.
        return self.pre.memory_bytes + sum(
            t.numel() * t.element_size() for t in (self.rewards, self.dones)
        ) + len(json.dumps(self.descriptors, separators=(",", ":")).encode("utf-8"))

    @classmethod
    def from_transitions(cls, rows: list[dict[str, Any]], episode: int, workers: int,
                         repair_capacity: int = 3) -> "CompactTDData":
        torch = require_torch()
        if not rows or not rows[-1]["done"] or any(row["done"] for row in rows[:-1]):
            raise ValueError("TD replay requires a complete, nonempty episode with one terminal transition.")
        descriptors = []
        for index, row in enumerate(rows):
            state = row["state"]
            descriptors.append({
                "worker_ids": tuple(state.worker_ids),
                "opportunity_ids": tuple(state.opportunity_ids),
                "decision_worker_ids": tuple(state.decision_worker_ids),
                "time_min": float(state.time_min), "horizon_min": float(state.horizon_min),
                "review_interval_min": float(state.review_interval_min),
                "wait_action_enabled": bool(state.wait_action_enabled), "decision_index": index,
                "repair_capacity": int(repair_capacity),
                "tasks": tuple(
                    (worker, opportunity, str(task.task_type), tuple(task.payload.get("_adp_resource_keys", [])))
                    for worker, tasks in state.tasks_by_worker.items()
                    for opportunity, task in tasks.items()
                ),
            })
        pre = compact_episode_transitions(
            [{"post_state": row["state"], "raw_reward": 0.0} for row in rows],
            episode_id=episode, worker_count=workers,
        )
        return cls(pre, torch.tensor([r["raw_reward"] for r in rows], dtype=torch.float32),
                   torch.tensor([r["done"] for r in rows], dtype=torch.bool), descriptors)

    def select(self, indices: list[int]) -> "CompactTDData":
        torch = require_torch()
        ix = torch.tensor(indices, dtype=torch.long)
        return CompactTDData(select_compact_samples(self.pre, indices), self.rewards[ix],
                             self.dones[ix], [self.descriptors[i] for i in indices])

    @classmethod
    def merge(cls, rows: list["CompactTDData"], *, padding_shape: tuple[int, int] | None = None) -> "CompactTDData":
        torch = require_torch()
        return cls(merge_compact_batches([r.pre for r in rows], padding_shape=padding_shape),
                   torch.cat([r.rewards for r in rows]), torch.cat([r.dones for r in rows]),
                   [d for row in rows for d in row.descriptors])

    def state(self, index: int) -> EncodedDecisionState:
        d = self.descriptors[index]
        nw, nt = len(d["worker_ids"]), len(d["opportunity_ids"])
        tasks: dict[str, dict[str, ReplayTask]] = {}
        for worker, opportunity, kind, resources in d["tasks"]:
            tasks.setdefault(worker, {})[opportunity] = ReplayTask(kind, {"_adp_resource_keys": resources})
        return EncodedDecisionState(
            global_features=self.pre.global_features[index].numpy(),
            worker_features=self.pre.worker_features[index, :nw].numpy(),
            task_features=self.pre.task_features[index, :nt].numpy(),
            pair_features=self.pre.pair_features[index, :nw, :nt].numpy(),
            feasibility=self.pre.feasibility[index, :nw, :nt].numpy(),
            worker_ids=list(d["worker_ids"]), opportunity_ids=list(d["opportunity_ids"]),
            decision_worker_ids=list(d["decision_worker_ids"]), tasks_by_worker=tasks,
            time_min=d["time_min"], horizon_min=d["horizon_min"],
            review_interval_min=d["review_interval_min"], wait_action_enabled=d["wait_action_enabled"],
        )


def retain_recent_episodes(history: list[CompactMCBatch], capacity: int) -> list[CompactMCBatch]:
    if capacity < 1:
        raise ValueError("replay_capacity_episodes must be positive.")
    ids = sorted({int(e) for batch in history for e in batch.episode_ids.tolist()})[-capacity:]
    keep = set(ids)
    retained = []
    for batch in history:
        indices = [i for i, episode in enumerate(batch.episode_ids.tolist()) if episode in keep]
        if indices:
            retained.append(batch if len(indices) == len(batch) else select_compact_samples(batch, indices))
    return retained


def sample_replay_episodes(
    replay: CompactMCBatch | list[CompactMCBatch],
    *,
    required_episode_ids: set[int],
    episode_count: int,
    rng: random.Random,
) -> CompactMCBatch:
    """Select before merging history, retaining identical RNG/order/padding."""
    batches = replay if isinstance(replay, list) else [replay]
    available = sorted({int(value) for batch in batches for value in batch.episode_ids.tolist()})
    available_set = set(available)
    required = sorted({int(value) for value in required_episode_ids})
    missing = sorted(set(required) - available_set)
    if missing:
        raise ValueError(f"Required replay episodes are unavailable: {missing}")
    if episode_count < len(required):
        raise ValueError("Replay update size cannot be smaller than the required current wave.")
    if episode_count > len(available):
        raise ValueError(
            f"Replay update requests {episode_count} episodes from {len(available)} available."
        )
    optional = [episode_id for episode_id in available if episode_id not in required_episode_ids]
    selected = set(required)
    selected.update(rng.sample(optional, episode_count - len(required)))
    chunks = []
    for batch in batches:
        indices = [index for index, eid in enumerate(batch.episode_ids.tolist()) if int(eid) in selected]
        if indices:
            chunks.append(batch if len(indices) == len(batch) else select_compact_samples(batch, indices))
    # Keep the old full-history padding shape: changing attention widths can
    # perturb nearly tied scores even when padding is correctly masked.
    padding = (
        max(int(batch.worker_features.shape[1]) for batch in batches),
        max(int(batch.task_features.shape[1]) for batch in batches),
    )
    sampled = merge_compact_batches(chunks, padding_shape=padding)
    if sampled.episode_count != episode_count:
        raise RuntimeError("Episode-level replay sampling produced an incomplete update batch.")
    return sampled


def truncated_return_plan(rewards: list[float], dones: list[bool], episodes: list[int],
                          greedy_match: list[bool], n: int) -> tuple[list[float], list[int], list[int], list[str]]:
    """Stop BEFORE an off-policy intermediate action; never cross an episode end."""
    if n < 1 or not (len(rewards) == len(dones) == len(episodes) == len(greedy_match)):
        raise ValueError("Invalid n-step sequence arrays.")
    sums, ends, steps, reasons = [], [], [], []
    for start in range(len(rewards)):
        total, end = 0.0, start
        while True:
            total += rewards[end]
            length = end - start + 1
            if dones[end]:
                endpoint, reason = -1, "terminal"
                break
            end += 1
            if end >= len(rewards) or episodes[end] != episodes[start]:
                raise ValueError("TD sequence was truncated without an actual terminal transition.")
            if length == n:
                endpoint, reason = end, "n_limit"
                break
            if not greedy_match[end]:
                endpoint, reason = end, "off_policy"
                break
        sums.append(total)
        ends.append(endpoint)
        steps.append(length)
        reasons.append(reason)
    return sums, ends, steps, reasons


@dataclass
class TDTargetPlan:
    greedy_posts: CompactMCBatch
    rewards: Any
    endpoints: Any
    steps: list[int]
    reasons: list[str]
    elapsed_min: list[float]

    def targets(self, indices: list[int], target_model: Any, device: Any) -> Any:
        torch = require_torch()
        ix = torch.tensor(indices, dtype=torch.long)
        result = self.rewards[ix].to(device)
        ends = self.endpoints[ix]
        live = (ends >= 0).nonzero().flatten()
        if len(live):
            inputs, _ = self.greedy_posts.model_batch(ends[live].tolist(), device)
            with torch.no_grad():
                result[live.to(device)] += target_model(**inputs)
        return result.detach()


def build_target_plan(samples: CompactMCBatch, model: Any, device: Any, *, n: int,
                      beam_width: int, worker_order_strategy: str, repair_capacity: int = 3) -> TDTargetPlan:
    torch = require_torch()
    data = samples.td_data
    if data is None:
        raise ValueError("n-step training requires transition replay, not fixed MC targets.")
    model.eval()
    posts, matches = [], []
    previous_episode, previous_index = None, -1
    for i, descriptor in enumerate(data.descriptors):
        episode = int(samples.episode_ids[i])
        step = int(descriptor["decision_index"])
        if step != (previous_index + 1 if episode == previous_episode else 0):
            raise ValueError("TD replay must preserve complete ordered episode sequences.")
        previous_episode, previous_index = episode, step
        state = data.state(i)
        offset = step % len(state.worker_ids) if worker_order_strategy == "cyclic" else 0
        order = state.worker_ids[offset:] + state.worker_ids[:offset]
        order = [w for w in order if w in state.decision_worker_ids]
        capacity = int(descriptor.get("repair_capacity", repair_capacity))
        assignment = unique_feasible_assignment(
            state, worker_order=order, repair_capacity=capacity,
            allow_wait_action=state.wait_action_enabled,
        )
        if assignment is None:
            assignment = greedy_beam_matching(
                state, model=model, device=device, beam_width=beam_width,
                repair_capacity=capacity, worker_order=order,
                allow_wait_action=state.wait_action_enabled,
            ).assignment
        # No choice means no online ranking call; target bootstrap still runs.
        post = state.post_decision(assignment)
        nw, nt = len(state.worker_ids), len(state.opportunity_ids)
        matches.append(np.array_equal(post.selected_assignment_mask,
                       samples.selected_assignment_mask[i, :nw, :nt].numpy()))
        # Free transient Task adapters; only compact tensors survive this function.
        post.tasks_by_worker = {}
        posts.append({"post_state": post, "raw_reward": 0.0})
    sums, ends, steps, reasons = truncated_return_plan(
        data.rewards.tolist(), data.dones.tolist(), samples.episode_ids.tolist(), matches, n,
    )
    elapsed = [
        (data.descriptors[end]["time_min"] if end >= 0 else data.descriptors[i]["horizon_min"])
        - data.descriptors[i]["time_min"] for i, end in enumerate(ends)
    ]
    greedy_posts = compact_episode_transitions(posts, episode_id=0, worker_count=0)
    return TDTargetPlan(greedy_posts, torch.tensor(sums, dtype=torch.float32),
                        torch.tensor(ends, dtype=torch.long), steps, reasons, elapsed)


def soft_update(target: Any, online: Any, tau: float) -> None:
    torch = require_torch()
    if not 0 < tau <= 1:
        raise ValueError("target_tau must be in (0, 1].")
    with torch.no_grad():
        for dest, source in zip(target.parameters(), online.parameters(), strict=True):
            dest.lerp_(source, tau)
        for dest, source in zip(target.buffers(), online.buffers(), strict=True):
            dest.copy_(source)


def fit_td_value(*, model: Any, target_model: Any, optimizer: Any, samples: CompactMCBatch,
                 plan: TDTargetPlan, device: Any, batch_size: int, epochs: int,
                 gradient_clip: float, target_tau: float, rng: random.Random) -> dict[str, Any]:
    torch = require_torch()
    # Stable episode split across replay lifetimes: holdout episodes never train.
    train_ids = [i for i, e in enumerate(samples.episode_ids.tolist()) if e % 10 != 0]
    holdout_ids = [i for i, e in enumerate(samples.episode_ids.tolist()) if e % 10 == 0]
    if not train_ids:
        raise ValueError("TD replay contains no training episodes.")
    target_model.eval()
    target_model.requires_grad_(False)
    sgd_steps = 0
    for _ in range(epochs):
        rng.shuffle(train_ids)
        model.train()
        for start in range(0, len(train_ids), batch_size):
            indices = train_ids[start:start + batch_size]
            inputs, _ = samples.model_batch(indices, device)
            # MC targets stored for diagnostics are never used by this loss.
            targets = plan.targets(indices, target_model, device)
            loss = torch.nn.functional.mse_loss(model(**inputs), targets)
            if not bool(torch.isfinite(loss)):
                raise RuntimeError("Non-finite n-step TD loss.")
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_clip, error_if_nonfinite=True)
            optimizer.step()
            soft_update(target_model, model, target_tau)
            sgd_steps += 1
    model.eval()
    result: dict[str, Any] = {"sgd_steps": sgd_steps, "epochs": epochs}
    for name, indices in (("train", train_ids), ("holdout", holdout_ids)):
        squared_sum = 0.0
        with torch.no_grad():
            for start in range(0, len(indices), batch_size):
                ix = indices[start:start + batch_size]
                inputs, _ = samples.model_batch(ix, device)
                error = model(**inputs) - plan.targets(ix, target_model, device)
                squared_sum += float(error.square().sum())
        result[f"td_{name}_mse"] = squared_sum / len(indices) if indices else None
        result[f"td_{name}_sample_count"] = len(indices)
    result.update({
        "effective_n_mean": float(np.mean(plan.steps)),
        "effective_n_min": min(plan.steps), "effective_n_max": max(plan.steps),
        "td_span_min_mean": float(np.mean(plan.elapsed_min)),
        "td_off_policy_cut_ratio": plan.reasons.count("off_policy") / len(plan.reasons),
        "td_terminal_ratio": plan.reasons.count("terminal") / len(plan.reasons),
        "td_n_limit_ratio": plan.reasons.count("n_limit") / len(plan.reasons),
    })
    return result


def greedy_mc_errors(
    model: Any,
    rows: list[dict[str, Any]],
    device: Any,
) -> tuple[int, float, float, float, float]:
    """Independent greedy episode accuracy, reduced before IPC; never trains a model."""
    if model is None:
        return 0, 0.0, 0.0, 0.0, 0.0
    from .compact import _mc_targets

    returns = _mc_targets(rows, 1.0)
    squared_sum, error_sum = 0.0, 0.0
    prediction_sum, target_sum = 0.0, 0.0
    for start in range(0, len(rows), 512):
        predictions = predict_values(model, [r["post_state"] for r in rows[start:start + 512]], device)
        error = predictions.astype(np.float64) - np.array(returns[start:start + 512])
        if not np.isfinite(error).all():
            raise RuntimeError("Non-finite greedy validation predictions.")
        squared_sum += float(np.sum(error ** 2))
        error_sum += float(np.sum(error))
        prediction_sum += float(np.sum(predictions, dtype=np.float64))
        target_sum += float(np.sum(returns[start:start + 512], dtype=np.float64))
    return len(rows), squared_sum, error_sum, prediction_sum, target_sum
