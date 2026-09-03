from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from .model import predict_values
from .schema import EncodedDecisionState


@dataclass
class ActionSelection:
    assignment: dict[str, str | None]
    predicted_value: float
    candidate_matching_count: int
    policy: str


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
    return ActionSelection(assignment, 0.0, 1, policy_name)


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


def _expand_matchings(
    state: EncodedDecisionState,
    *,
    beam_width: int,
    repair_capacity: int,
    model: Any,
    device: Any,
    allow_wait_action: bool,
) -> list[tuple[dict[str, str | None], float]]:
    beams: list[tuple[dict[str, str | None], dict[str, int], set[str], float]] = [({}, {}, set(), 0.0)]
    for worker_id in state.decision_worker_ids:
        expanded: list[tuple[dict[str, str | None], dict[str, int], set[str], float]] = []
        for assignment, used, used_resources, _ in beams:
            feasible_choices: list[tuple[str | None, set[str]]] = []
            for opportunity in sorted(state.tasks_by_worker.get(worker_id, {})):
                limit = repair_capacity if _task_is_shareable(state, worker_id, opportunity) else 1
                if used.get(opportunity, 0) >= limit:
                    continue
                resources = _resource_keys(state, worker_id, opportunity)
                if resources & used_resources:
                    continue
                feasible_choices.append((opportunity, resources))
            if allow_wait_action or not feasible_choices:
                feasible_choices.insert(0, (None, set()))
            for opportunity, resources in feasible_choices:
                next_assignment = dict(assignment)
                next_assignment[worker_id] = opportunity
                next_used = dict(used)
                if opportunity is not None:
                    next_used[opportunity] = next_used.get(opportunity, 0) + 1
                expanded.append((next_assignment, next_used, set(used_resources) | resources, 0.0))
        if len(expanded) > beam_width:
            partial_states = [state.post_decision(row[0]) for row in expanded]
            values = predict_values(model, partial_states, device)
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
    values = predict_values(model, post_states, device)
    return [
        (row[0], float(value))
        for row, value in zip(beams, values)
    ]


def greedy_beam_matching(
    state: EncodedDecisionState,
    *,
    model: Any,
    device: Any,
    beam_width: int = 64,
    repair_capacity: int = 3,
    allow_wait_action: bool = False,
) -> ActionSelection:
    rows = _expand_matchings(
        state,
        beam_width=max(1, int(beam_width)),
        repair_capacity=max(1, int(repair_capacity)),
        model=model,
        device=device,
        allow_wait_action=bool(allow_wait_action),
    )
    if not rows:
        return ActionSelection({worker_id: None for worker_id in state.decision_worker_ids}, 0.0, 0, "value_beam_search")
    chosen, value = max(rows, key=lambda row: (row[1], str(sorted(row[0].items()))))
    return ActionSelection(chosen, float(value), len(rows), "value_beam_search")
