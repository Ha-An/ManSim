from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


FEATURE_SCHEMA_VERSION = "mfg_flow_shop_adp_v9"
GLOBAL_FEATURE_DIM = 35
WORKER_FEATURE_DIM = 16
TASK_FEATURE_DIM = 21
PAIR_FEATURE_DIM = 8


@dataclass
class EncodedDecisionState:
    global_features: np.ndarray
    worker_features: np.ndarray
    task_features: np.ndarray
    pair_features: np.ndarray
    feasibility: np.ndarray
    worker_ids: list[str]
    opportunity_ids: list[str]
    selected_assignment_mask: np.ndarray | None = None
    decision_worker_ids: list[str] = field(default_factory=list)
    tasks_by_worker: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)
    time_min: float = 0.0
    horizon_min: float = 1.0
    review_interval_min: float = 1.0
    wait_action_enabled: bool = False

    def __post_init__(self) -> None:
        if self.selected_assignment_mask is None:
            self.selected_assignment_mask = np.zeros_like(self.feasibility, dtype=bool)
        if not self.decision_worker_ids:
            self.decision_worker_ids = list(self.worker_ids)

    def post_decision(self, assignment: dict[str, str | None]) -> "EncodedDecisionState":
        workers = self.worker_features.copy()
        tasks = self.task_features.copy()
        selected = np.zeros_like(self.feasibility, dtype=bool)
        for worker_id in self.decision_worker_ids:
            opportunity_id = assignment.get(worker_id)
            worker_index = self.worker_ids.index(worker_id)
            workers[worker_index, 12] = 1.0 if opportunity_id else 0.0
            if opportunity_id in self.opportunity_ids:
                task_index = self.opportunity_ids.index(str(opportunity_id))
                workers[worker_index, 13] = tasks[task_index, 0]
                tasks[task_index, 17] += 1.0
                selected[worker_index, task_index] = True

        time_min = float(self.time_min)
        global_features = self.global_features.copy()
        complete_joint_action = all(worker_id in assignment for worker_id in self.decision_worker_ids)
        all_wait = complete_joint_action and all(assignment.get(worker_id) is None for worker_id in self.decision_worker_ids)
        if self.wait_action_enabled and all_wait and self.decision_worker_ids:
            horizon_min = max(1e-9, float(self.horizon_min))
            wait_delay = min(
                max(0.0, float(self.review_interval_min)),
                max(0.0, horizon_min - time_min),
            )
            time_min += wait_delay
            global_features[0] = np.float32(max(-2.0, min(2.0, time_min / horizon_min)))
            global_features[1] = np.float32(
                max(-2.0, min(2.0, (horizon_min - time_min) / horizon_min))
            )
        return EncodedDecisionState(
            global_features=global_features,
            worker_features=workers,
            task_features=tasks,
            pair_features=self.pair_features.copy(),
            feasibility=self.feasibility.copy(),
            selected_assignment_mask=selected,
            worker_ids=list(self.worker_ids),
            opportunity_ids=list(self.opportunity_ids),
            decision_worker_ids=list(self.decision_worker_ids),
            tasks_by_worker=self.tasks_by_worker,
            time_min=time_min,
            horizon_min=float(self.horizon_min),
            review_interval_min=float(self.review_interval_min),
            wait_action_enabled=bool(self.wait_action_enabled),
        )


def serialize_state(state: EncodedDecisionState) -> dict[str, Any]:
    return {
        "global_features": state.global_features.tolist(),
        "worker_features": state.worker_features.tolist(),
        "task_features": state.task_features.tolist(),
        "pair_features": state.pair_features.tolist(),
        "feasibility": state.feasibility.astype(int).tolist(),
        "selected_assignment_mask": state.selected_assignment_mask.astype(int).tolist(),
        "worker_ids": list(state.worker_ids),
        "opportunity_ids": list(state.opportunity_ids),
        "decision_worker_ids": list(state.decision_worker_ids),
        "time_min": float(state.time_min),
        "horizon_min": float(state.horizon_min),
        "review_interval_min": float(state.review_interval_min),
        "wait_action_enabled": bool(state.wait_action_enabled),
    }


def deserialize_state(payload: dict[str, Any]) -> EncodedDecisionState:
    feasibility = np.asarray(payload["feasibility"], dtype=bool)
    return EncodedDecisionState(
        global_features=np.asarray(payload["global_features"], dtype=np.float32),
        worker_features=np.asarray(payload["worker_features"], dtype=np.float32),
        task_features=np.asarray(payload["task_features"], dtype=np.float32),
        pair_features=np.asarray(payload["pair_features"], dtype=np.float32),
        feasibility=feasibility,
        selected_assignment_mask=np.asarray(
            payload.get("selected_assignment_mask", np.zeros_like(feasibility)), dtype=bool
        ),
        worker_ids=[str(value) for value in payload["worker_ids"]],
        opportunity_ids=[str(value) for value in payload["opportunity_ids"]],
        decision_worker_ids=[str(value) for value in payload.get("decision_worker_ids", payload["worker_ids"])],
        time_min=float(payload.get("time_min", 0.0)),
        horizon_min=float(payload.get("horizon_min", max(1.0, float(payload.get("time_min", 0.0))))),
        review_interval_min=float(payload.get("review_interval_min", 1.0)),
        wait_action_enabled=bool(payload.get("wait_action_enabled", False)),
    )
