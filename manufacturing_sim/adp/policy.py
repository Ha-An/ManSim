from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from .model import predict_values
from .schema import EncodedDecisionState


@dataclass
class ActionSelection:
    assignment: dict[str, str | None]
    predicted_value: float
    candidate_matching_count: int
    policy: str
    worker_order: list[str] = field(default_factory=list)
    candidate_value_entropy: float | None = None
    candidate_value_count: int = 0


def normalized_beam_value_entropy(values: list[float]) -> float | None:
    """Return scale-invariant Shannon entropy for a set of beam values."""

    if len(values) < 2:
        return None
    mean = sum(float(value) for value in values) / len(values)
    variance = sum((float(value) - mean) ** 2 for value in values) / len(values)
    scale = math.sqrt(variance)
    if scale <= 1e-12:
        return 1.0
    logits = [(float(value) - mean) / scale for value in values]
    peak = max(logits)
    weights = [math.exp(value - peak) for value in logits]
    total = sum(weights)
    probabilities = [weight / total for weight in weights]
    entropy = -sum(probability * math.log(probability) for probability in probabilities)
    return float(max(0.0, min(1.0, entropy / math.log(len(probabilities)))))


def _task_is_shareable(state: EncodedDecisionState, worker_id: str, opportunity_id: str) -> bool:
    task = state.tasks_by_worker.get(worker_id, {}).get(opportunity_id)
    return bool(task is not None and str(task.task_type).upper() == "REPAIR_MACHINE")


def _resource_keys(state: EncodedDecisionState, worker_id: str, opportunity_id: str) -> set[str]:
    task = state.tasks_by_worker.get(worker_id, {}).get(opportunity_id)
    if task is None:
        return set()
    values = task.payload.get("_adp_resource_keys", []) if isinstance(task.payload, dict) else []
    keys = {str(value) for value in values if str(value)}
    if _task_is_shareable(state, worker_id, opportunity_id):
        keys = {key for key in keys if not key.startswith("machine:")}
    return keys


def random_feasible_matching(
    state: EncodedDecisionState,
    *,
    rng: random.Random,
    repair_capacity: int = 3,
    allow_wait_action: bool = False,
) -> ActionSelection:
    assignment: dict[str, str | None] = {worker_id: None for worker_id in state.decision_worker_ids}
    used: dict[str, int] = {}
    used_resources: set[str] = set()
    workers = list(state.decision_worker_ids)
    rng.shuffle(workers)
    for worker_id in workers:
        feasible: list[str] = []
        for opportunity in sorted(state.tasks_by_worker.get(worker_id, {})):
            shareable = _task_is_shareable(state, worker_id, opportunity)
            limit = repair_capacity if shareable else 1
            if used.get(opportunity, 0) >= limit:
                continue
            resources = _resource_keys(state, worker_id, opportunity)
            if resources & used_resources:
                continue
            feasible.append(opportunity)
        choices: list[str | None] = ([None] if allow_wait_action else []) + feasible
        choice = rng.choice(choices) if choices else None
        if choice is None:
            continue
        assignment[worker_id] = choice
        used[choice] = used.get(choice, 0) + 1
        used_resources.update(_resource_keys(state, worker_id, choice))
    policy_name = (
        "uniform_random_feasible_with_wait"
        if allow_wait_action
        else "uniform_random_feasible_no_wait"
    )
    return ActionSelection(assignment, 0.0, 1, policy_name, worker_order=list(workers))


def probe_feasible_matchings(
    state: EncodedDecisionState,
    *,
    rng: random.Random,
    candidate_limit: int,
    repair_capacity: int = 3,
    allow_wait_action: bool = False,
) -> list[dict[str, str | None]]:
    """Sample a deterministic, diverse feasible set for counterfactual diagnostics."""

    limit = max(2, int(candidate_limit))
    rows: list[dict[str, str | None]] = []
    seen: set[tuple[tuple[str, str | None], ...]] = set()

    def add(assignment: dict[str, str | None]) -> None:
        normalized = {
            worker_id: assignment.get(worker_id)
            for worker_id in state.decision_worker_ids
        }
        key = tuple(sorted(normalized.items()))
        if key not in seen:
            seen.add(key)
            rows.append(normalized)

    if allow_wait_action:
        add({worker_id: None for worker_id in state.decision_worker_ids})

    # Include a deterministic work-conserving candidate before random sampling.
    assignment: dict[str, str | None] = {}
    used: dict[str, int] = {}
    used_resources: set[str] = set()
    for worker_id in state.decision_worker_ids:
        chosen: str | None = None
        for opportunity in sorted(state.tasks_by_worker.get(worker_id, {})):
            limit_for_task = (
                repair_capacity if _task_is_shareable(state, worker_id, opportunity) else 1
            )
            if used.get(opportunity, 0) >= limit_for_task:
                continue
            resources = _resource_keys(state, worker_id, opportunity)
            if resources & used_resources:
                continue
            chosen = opportunity
            used[opportunity] = used.get(opportunity, 0) + 1
            used_resources.update(resources)
            break
        assignment[worker_id] = chosen
    add(assignment)

    attempts = max(64, limit * 32)
    for _ in range(attempts):
        if len(rows) >= limit:
            break
        add(
            random_feasible_matching(
                state,
                rng=rng,
                repair_capacity=repair_capacity,
                allow_wait_action=allow_wait_action,
            ).assignment
        )
    return rows[:limit]


def _feasible_choices(
    state: EncodedDecisionState, worker_id: str, used: dict[str, int],
    used_resources: set[str], repair_capacity: int, allow_wait_action: bool,
) -> list[tuple[str | None, set[str]]]:
    choices: list[tuple[str | None, set[str]]] = []
    for opportunity in sorted(state.tasks_by_worker.get(worker_id, {})):
        limit = repair_capacity if _task_is_shareable(state, worker_id, opportunity) else 1
        if used.get(opportunity, 0) >= limit:
            continue
        resources = _resource_keys(state, worker_id, opportunity)
        if not resources & used_resources:
            choices.append((opportunity, resources))
    if allow_wait_action or not choices:
        choices.insert(0, (None, set()))
    return choices


def unique_feasible_assignment(
    state: EncodedDecisionState, *, worker_order: list[str], repair_capacity: int,
    allow_wait_action: bool,
) -> dict[str, str | None] | None:
    """Return the sole beam action, or None as soon as a choice needs scoring."""
    if set(worker_order) != set(state.decision_worker_ids) or len(worker_order) != len(state.decision_worker_ids):
        raise ValueError("worker_order must contain every decision worker exactly once")
    assignment: dict[str, str | None] = {}
    used: dict[str, int] = {}
    resources: set[str] = set()
    for worker_id in worker_order:
        choices = _feasible_choices(state, worker_id, used, resources, max(1, repair_capacity), allow_wait_action)
        if len(choices) != 1:
            return None
        opportunity, keys = choices[0]
        assignment[worker_id] = opportunity
        if opportunity is not None:
            used[opportunity] = used.get(opportunity, 0) + 1
        resources.update(keys)
    return assignment


def _matching_score_requests(
    state: EncodedDecisionState,
    *,
    beam_width: int,
    repair_capacity: int,
    allow_wait_action: bool,
    worker_order: list[str] | None = None,
):
    beams: list[tuple[dict[str, str | None], dict[str, int], set[str], float]] = [({}, {}, set(), 0.0)]
    ordered_workers = list(worker_order or state.decision_worker_ids)
    if set(ordered_workers) != set(state.decision_worker_ids) or len(ordered_workers) != len(
        state.decision_worker_ids
    ):
        raise ValueError("worker_order must contain every decision worker exactly once")
    for worker_id in ordered_workers:
        expanded: list[tuple[dict[str, str | None], dict[str, int], set[str], float]] = []
        for assignment, used, used_resources, _ in beams:
            feasible_choices = _feasible_choices(
                state, worker_id, used, used_resources, repair_capacity, allow_wait_action,
            )
            for opportunity, resources in feasible_choices:
                next_assignment = dict(assignment)
                next_assignment[worker_id] = opportunity
                next_used = dict(used)
                if opportunity is not None:
                    next_used[opportunity] = next_used.get(opportunity, 0) + 1
                expanded.append((next_assignment, next_used, set(used_resources) | resources, 0.0))
        if len(expanded) > beam_width:
            partial_states = [state.post_decision(row[0]) for row in expanded]
            values = yield partial_states, beam_width
            ranked = sorted(
                zip(expanded, values),
                key=lambda row: (
                    float(row[1]),
                    str(sorted(row[0][0].items())),
                ),
                reverse=True,
            )[:beam_width]
            beams = [
                (entry[0], entry[1], entry[2], float(value))
                for entry, value in ranked
            ]
        else:
            beams = expanded
    post_states = [state.post_decision(row[0]) for row in beams]
    values = yield post_states, 1
    return [
        (row[0], float(value))
        for row, value in zip(beams, values)
    ]


def _expand_matchings(state: EncodedDecisionState, *, model: Any, device: Any, **kwargs):
    search = _matching_score_requests(state, **kwargs)
    request = next(search)
    while True:
        states, _ = request
        values = predict_values(model, states, device)
        try:
            request = search.send(values)
        except StopIteration as done:
            return done.value


def _select_matching(rows, ordered_workers: list[str]) -> ActionSelection:
    if not rows:
        return ActionSelection({w: None for w in ordered_workers}, 0.0, 0,
                               "value_beam_search", worker_order=ordered_workers)
    chosen, value = max(rows, key=lambda row: (row[1], str(sorted(row[0].items()))))
    values = [float(value) for _, value in rows]
    return ActionSelection(chosen, float(value), len(rows), "value_beam_search",
                           worker_order=ordered_workers,
                           candidate_value_entropy=normalized_beam_value_entropy(values),
                           candidate_value_count=len(values))


def greedy_beam_matching_many(
    states: list[EncodedDecisionState], *, model: Any, device: Any, beam_width: int,
    worker_orders: list[list[str]], repair_capacities: list[int],
    candidate_batch_size: int = 1024, stats: dict[str, int] | None = None,
) -> list[ActionSelection]:
    """Batch independent searches, preserving masks and scalar near-tie ordering."""
    if not (len(states) == len(worker_orders) == len(repair_capacities)):
        raise ValueError("Batched beam inputs must have equal lengths.")
    if candidate_batch_size < 1:
        raise ValueError("candidate_batch_size must be positive.")
    searches, requests, results = {}, {}, {}
    for i, state in enumerate(states):
        search = _matching_score_requests(
            state, beam_width=max(1, beam_width), worker_order=worker_orders[i],
            repair_capacity=max(1, repair_capacities[i]), allow_wait_action=state.wait_action_enabled,
        )
        searches[i] = search
        requests[i] = next(search)
    while requests:
        # Identical padding widths avoid changing the attention problem itself.
        groups: dict[tuple[int, int], list[int]] = {}
        for i in requests:
            key = (len(states[i].worker_ids), len(states[i].opportunity_ids))
            groups.setdefault(key, []).append(i)
        scored = {}
        for ids in groups.values():
            flat, slices = [], {}
            for i in ids:
                begin = len(flat)
                flat.extend(requests[i][0])
                slices[i] = (begin, len(flat))
            values = []
            for start in range(0, len(flat), candidate_batch_size):
                values.extend(predict_values(model, flat[start:start + candidate_batch_size], device))
                if stats is not None:
                    stats["batched_score_calls"] = stats.get("batched_score_calls", 0) + 1
            for i, (begin, end) in slices.items():
                scored[i] = values[begin:end]
        next_requests = {}
        for i, (_, keep) in requests.items():
            values = scored[i]
            ranked = sorted((float(value) for value in values), reverse=True)
            near_tie = (len(ranked) > keep and
                        ranked[keep - 1] - ranked[keep] <=
                        1e-5 + 1e-6 * max(abs(ranked[keep - 1]), abs(ranked[keep])))
            if near_tie:
                searches[i].close()
                results[i] = greedy_beam_matching(
                    states[i], model=model, device=device, beam_width=beam_width,
                    repair_capacity=repair_capacities[i], worker_order=worker_orders[i],
                    allow_wait_action=states[i].wait_action_enabled,
                )
                if stats is not None:
                    stats["scalar_tie_fallbacks"] = stats.get("scalar_tie_fallbacks", 0) + 1
                continue
            try:
                next_requests[i] = searches[i].send(values)
            except StopIteration as done:
                results[i] = _select_matching(done.value, worker_orders[i])
        requests = next_requests
    return [results[i] for i in range(len(states))]


def greedy_beam_matching(
    state: EncodedDecisionState,
    *,
    model: Any,
    device: Any,
    beam_width: int = 64,
    repair_capacity: int = 3,
    allow_wait_action: bool = False,
    worker_order: list[str] | None = None,
) -> ActionSelection:
    ordered_workers = list(worker_order or state.decision_worker_ids)
    rows = _expand_matchings(
        state,
        beam_width=max(1, int(beam_width)),
        repair_capacity=max(1, int(repair_capacity)),
        model=model,
        device=device,
        allow_wait_action=bool(allow_wait_action),
        worker_order=ordered_workers,
    )
    return _select_matching(rows, ordered_workers)
