from __future__ import annotations

import math
import random
from collections import defaultdict, deque
from time import perf_counter
from typing import Any

import simpy

from manufacturing_sim.simulation.rolling_horizon import PostStateTimeout

from .checkpoint import load_checkpoint
from .encoding import ADPStateEncoder
from .model import require_torch
from .policy import (
    ActionSelection,
    greedy_beam_matching,
    probe_feasible_matchings,
    random_feasible_matching,
)
from .schema import EncodedDecisionState, serialize_state


class ADPDecisionCoordinator:
    """Event-driven joint assignment coordinator for mfg_flow_shop."""

    def __init__(self, world: Any, cfg: dict[str, Any]) -> None:
        self.world = world
        self.env = world.env
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.training = bool(self.cfg.get("training", False))
        if "random_wait_probability" in self.cfg:
            raise ValueError(
                "decision.adp.random_wait_probability was removed; use allow_wait_action to enable or disable WAIT."
            )
        if "potential_shaping" in self.cfg:
            raise ValueError("decision.adp.potential_shaping was removed; ADP now uses raw completed-product rewards.")
        self.max_review_interval_min = max(0.1, float(self.cfg.get("max_review_interval_min", 5.0) or 5.0))
        self.beam_width = max(1, int(self.cfg.get("beam_width", 64) or 64))
        self.worker_order_strategy = str(
            self.cfg.get("worker_order_strategy", "cyclic") or "cyclic"
        ).strip().lower()
        if self.worker_order_strategy not in {"cyclic", "fixed"}:
            raise ValueError(
                "decision.adp.worker_order_strategy must be 'cyclic' or 'fixed'."
            )
        self.exploration_epsilon = min(1.0, max(0.0, float(self.cfg.get("exploration_epsilon", 0.0) or 0.0)))
        self.allow_wait_action = bool(self.cfg.get("allow_wait_action", False))
        self.rng = random.Random(int(world.seed) ^ 0xAD92026)
        self.encoder = ADPStateEncoder(
            review_interval_min=self.max_review_interval_min,
            allow_wait_action=self.allow_wait_action,
        )
        self.dispatch_queues: dict[str, deque[Any]] = defaultdict(deque)
        self.dispatch_events: dict[str, simpy.Event] = {worker_id: self.env.event() for worker_id in world.workers}
        self.requested_triggers: set[str] = set()
        self.first_seen_by_opportunity: dict[str, float] = {}
        self.decision_scheduled = False
        self.model: Any | None = None
        self.device: Any = "cpu"
        self.checkpoint_manifest: dict[str, Any] = {}
        self.force_random_policy = bool(self.cfg.get("force_random_policy", self.training))
        self.transitions: list[dict[str, Any]] = []
        self.episode_finalized = False
        self.previous_pre_state: EncodedDecisionState | None = None
        self.previous_post_state: EncodedDecisionState | None = None
        self.previous_action: dict[str, str | None] | None = None
        self.previous_value_policy_selected = False
        self.previous_product_count = int(world.product_count)
        self.consecutive_all_wait_decisions = 0
        self.consecutive_candidate_all_wait_decisions = 0
        self.action_history: list[dict[str, str | None]] = []
        self.probe_records: list[dict[str, Any]] = []
        self.probe_target_product_count: int | None = None
        self._probe_capture_thresholds = sorted(
            {
                max(1, int(value))
                for value in self.cfg.get("_probe_capture_decision_thresholds", [])
            }
        )
        self._probe_captured_thresholds: set[int] = set()
        self._probe_candidate_limit = max(
            2, int(self.cfg.get("_probe_candidate_limit", 6) or 6)
        )
        self._probe_forced_action_script = [
            {str(worker_id): opportunity for worker_id, opportunity in row.items()}
            for row in self.cfg.get("_probe_forced_action_script", [])
            if isinstance(row, dict)
        ]
        self._probe_target_decision_number = int(
            self.cfg.get("_probe_target_decision_number", 0) or 0
        )
        self.metrics: dict[str, Any] = {
            "decision_count": 0,
            "wait_count": 0,
            "candidate_available_wait_count": 0,
            "no_candidate_unassigned_count": 0,
            "assigned_task_count": 0,
            "joint_all_wait_count": 0,
            "joint_all_wait_with_candidate_count": 0,
            "joint_no_candidate_count": 0,
            "max_consecutive_all_wait_decisions": 0,
            "max_consecutive_candidate_all_wait_decisions": 0,
            "candidate_total": 0,
            "feasible_pair_total": 0,
            "candidate_matching_total": 0,
            "beam_value_entropy_sum": 0.0,
            "beam_value_entropy_decision_count": 0,
            "inference_latency_ms_total": 0.0,
            "inference_latency_ms_max": 0.0,
        }
        if not self.training and not self.force_random_policy:
            self._load_configured_checkpoint()

    def _load_configured_checkpoint(self) -> None:
        torch = require_torch()
        requested = str(self.cfg.get("device", "auto") or "auto").strip().lower()
        self.device = torch.device("cuda" if requested == "auto" and torch.cuda.is_available() else "cpu" if requested == "auto" else requested)
        checkpoint_path = str(self.cfg.get("checkpoint_path", "") or "").strip()
        if not checkpoint_path:
            raise RuntimeError("decision.adp.checkpoint_path is required for simulation_based_adp inference.")
        self.model, self.checkpoint_manifest = load_checkpoint(
            checkpoint_path,
            world=self.world,
            device=self.device,
            wait_action_enabled=self.allow_wait_action,
            worker_order_strategy=self.worker_order_strategy,
        )
        training_meta = self.checkpoint_manifest.get("training", {})
        trained_review = float(training_meta.get("max_review_interval_min", self.max_review_interval_min))
        if not math.isclose(trained_review, self.max_review_interval_min, rel_tol=0.0, abs_tol=1e-9):
            raise RuntimeError(
                "ADP checkpoint decision cadence mismatch: "
                f"trained max_review_interval_min={trained_review}, "
                f"runtime={self.max_review_interval_min}"
            )

    def set_training_policy(
        self,
        *,
        model: Any | None,
        device: Any = "cpu",
        force_random: bool = False,
        epsilon: float = 0.0,
    ) -> None:
        self.model = model
        self.device = device
        self.force_random_policy = bool(force_random)
        self.exploration_epsilon = min(1.0, max(0.0, float(epsilon)))

    def start(self) -> None:
        self.env.process(self._review_loop())
        self.request("bootstrap")

    def request(self, trigger: str) -> None:
        if self.world.terminated:
            return
        self.requested_triggers.add(str(trigger or "state_change"))
        if self.decision_scheduled:
            return
        self.decision_scheduled = True
        self.env.process(self._decision_process())

    def _review_loop(self):
        while not self.world.terminated:
            yield self.env.timeout(self.max_review_interval_min)
            self.request("max_review_interval")

    def _decision_process(self):
        yield PostStateTimeout(self.env, 0.0)
        self.decision_scheduled = False
        triggers = sorted(self.requested_triggers)
        self.requested_triggers.clear()
        self._decide(triggers)

    def dispatch_event(self, worker_id: str) -> simpy.Event:
        event = self.dispatch_events.get(worker_id)
        if event is None or event.triggered:
            event = self.env.event()
            self.dispatch_events[worker_id] = event
        return event

    def pop_task(self, worker_id: str) -> Any | None:
        queue = self.dispatch_queues.get(worker_id)
        if not queue:
            return None
        task = queue.popleft()
        self.dispatch_events[worker_id] = self.env.event()
        return task

    def _wake(self, worker_id: str) -> None:
        event = self.dispatch_events.get(worker_id)
        if event is not None and not event.triggered:
            event.succeed({"worker_id": worker_id, "t": float(self.env.now)})

    def _idle_workers(self) -> list[Any]:
        rows = []
        for worker in self.world.workers.values():
            if self.dispatch_queues.get(worker.agent_id):
                continue
            if worker.current_task_id or worker.suspended_task is not None or worker.awaiting_battery_from is not None:
                continue
            if worker.transport_session_id is not None or worker.charging_started_at is not None:
                continue
            if worker.discharged and self.world.mandatory_task_for_agent(worker) is None:
                continue
            rows.append(worker)
        return sorted(rows, key=lambda worker: worker.agent_id)

    def _candidate_map(self, workers: list[Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        mandatory: dict[str, Any] = {}
        active_resources = set(self.world._rolling_horizon_active_resource_index())
        for worker in workers:
            mandatory_task = self.world.mandatory_task_for_agent(worker)
            if mandatory_task is not None:
                mandatory[worker.agent_id] = self.world._bind_humanoid_candidate_for_agent(worker, mandatory_task)
                result[worker.agent_id] = {}
                continue
            candidates = self.world._bind_humanoid_candidates_for_agent(
                worker,
                self.world._filter_candidates_for_agent(worker, self.world._candidate_tasks(worker)),
            )
            candidates = [task for task in candidates if self.world._task_item_dependencies_available(task, worker)]
            mapped: dict[str, Any] = {}
            for task in candidates:
                opportunity = self.world._rolling_horizon_opportunity_id(task)
                first_seen = self.first_seen_by_opportunity.setdefault(opportunity, float(self.env.now))
                task.payload["first_seen_min"] = float(first_seen)
                resources = self.world._rolling_horizon_exclusive_resource_keys(task)
                blocking = [key for key in resources if key in active_resources and not (str(task.task_type).upper() == "REPAIR_MACHINE" and key.startswith("machine:"))]
                if blocking:
                    continue
                task.payload["_adp_resource_keys"] = list(resources)
                mapped.setdefault(opportunity, task)
            result[worker.agent_id] = mapped
        return result, mandatory

    def _close_previous_transition(self, next_state: EncodedDecisionState | None, *, done: bool) -> None:
        if self.previous_post_state is None or self.previous_pre_state is None or self.previous_action is None:
            return
        raw_reward = int(self.world.product_count) - int(self.previous_product_count)
        self.transitions.append(
            {
                "state": self.previous_pre_state,
                "action": dict(self.previous_action),
                "post_state": self.previous_post_state,
                "raw_reward": float(raw_reward),
                "value_policy_selected": bool(self.previous_value_policy_selected),
                "next_state": next_state,
                "done": bool(done),
            }
        )
        self.previous_product_count = int(self.world.product_count)

    def _select(self, state: EncodedDecisionState) -> ActionSelection:
        if self.force_random_policy or self.model is None or self.rng.random() < self.exploration_epsilon:
            return random_feasible_matching(
                state,
                rng=self.rng,
                repair_capacity=self.world.max_repair_agents,
                allow_wait_action=self.allow_wait_action,
            )
        worker_order = self._beam_worker_order(state)
        return greedy_beam_matching(
            state,
            model=self.model,
            device=self.device,
            beam_width=self.beam_width,
            repair_capacity=self.world.max_repair_agents,
            allow_wait_action=self.allow_wait_action,
            worker_order=worker_order,
        )

    def _beam_worker_order(self, state: EncodedDecisionState) -> list[str]:
        decision_workers = set(state.decision_worker_ids)
        fleet_order = list(state.worker_ids)
        if self.worker_order_strategy == "fixed" or not fleet_order:
            return [worker_id for worker_id in fleet_order if worker_id in decision_workers]
        offset = int(self.metrics["decision_count"]) % len(fleet_order)
        circular = fleet_order[offset:] + fleet_order[:offset]
        return [worker_id for worker_id in circular if worker_id in decision_workers]

    def _forced_probe_selection(
        self,
        state: EncodedDecisionState,
        mandatory: dict[str, Any],
        decision_number: int,
    ) -> ActionSelection | None:
        if decision_number > len(self._probe_forced_action_script):
            return None
        configured = self._probe_forced_action_script[decision_number - 1]
        assignment = {
            worker_id: configured.get(worker_id)
            for worker_id in state.decision_worker_ids
        }
        for worker_id, opportunity in assignment.items():
            if opportunity == "__MANDATORY__":
                if worker_id not in mandatory:
                    raise RuntimeError(
                        "ADP fixed probe replay diverged: mandatory task is missing for "
                        f"worker={worker_id}, decision={decision_number}."
                    )
                continue
            if opportunity is not None and opportunity not in state.tasks_by_worker.get(worker_id, {}):
                raise RuntimeError(
                    "ADP fixed probe replay diverged: opportunity is unavailable for "
                    f"worker={worker_id}, opportunity={opportunity}, decision={decision_number}."
                )
        return ActionSelection(
            assignment=assignment,
            predicted_value=0.0,
            candidate_matching_count=1,
            policy="fixed_counterfactual_probe_replay",
            worker_order=list(state.decision_worker_ids),
        )

    def _capture_probe_state(
        self,
        state: EncodedDecisionState,
        *,
        decision_number: int,
        mandatory: dict[str, Any],
    ) -> None:
        if mandatory or not self._probe_capture_thresholds:
            return
        pending = [
            threshold
            for threshold in self._probe_capture_thresholds
            if threshold not in self._probe_captured_thresholds
            and decision_number >= threshold
        ]
        if not pending:
            return
        candidates = probe_feasible_matchings(
            state,
            rng=random.Random(int(self.world.seed) ^ (decision_number << 8) ^ 0xC0FFEE),
            candidate_limit=self._probe_candidate_limit,
            repair_capacity=self.world.max_repair_agents,
            allow_wait_action=self.allow_wait_action,
        )
        if len(candidates) < 2:
            return
        threshold = pending[0]
        self._probe_captured_thresholds.add(threshold)
        self.probe_records.append(
            {
                "threshold": threshold,
                "decision_number": decision_number,
                "time_min": float(self.env.now),
                "products_before": int(self.world.product_count),
                "prefix_actions": [dict(row) for row in self.action_history],
                "candidates": [
                    {
                        "candidate_id": index,
                        "assignment": dict(assignment),
                        "post_state": serialize_state(state.post_decision(assignment)),
                    }
                    for index, assignment in enumerate(candidates)
                ],
            }
        )

    def _decide(self, triggers: list[str]) -> None:
        workers = self._idle_workers()
        if not workers:
            return
        tasks_by_worker, mandatory = self._candidate_map(workers)
        state = self.encoder.encode(self.world, workers, tasks_by_worker)
        self._close_previous_transition(state, done=False)
        decision_number = int(self.metrics["decision_count"]) + 1
        self._capture_probe_state(
            state,
            decision_number=decision_number,
            mandatory=mandatory,
        )
        if decision_number == self._probe_target_decision_number:
            self.probe_target_product_count = int(self.world.product_count)
            expected = self.cfg.get("_probe_expected_pre_state")
            if expected is not None and serialize_state(state) != expected:
                raise RuntimeError("ADP counterfactual replay pre-state mismatch.")
        started = perf_counter()
        selection = self._forced_probe_selection(
            state,
            mandatory,
            decision_number,
        ) or self._select(state)
        if decision_number == self._probe_target_decision_number:
            rng_state = self.cfg.get("_probe_policy_rng_state_after_selection")
            if rng_state is not None:
                # Prefix replay bypasses policy sampling; restore the captured continuation.
                self.rng.setstate((int(rng_state[0]), tuple(rng_state[1]), rng_state[2]))
            expected_post = self.cfg.get("_probe_expected_post_state")
            if expected_post is not None and serialize_state(state.post_decision(selection.assignment)) != expected_post:
                raise RuntimeError("ADP counterfactual replay post-state mismatch.")
            if "_probe_future_seed" in self.cfg:
                from .value_validation import reseed_future_draws

                reseed_future_draws(self.world, int(self.cfg["_probe_future_seed"]))
        value_policy_selected = selection.policy == "value_beam_search" and not mandatory
        for worker_id, task in mandatory.items():
            if task is not None:
                selection.assignment[worker_id] = "__MANDATORY__"
        if self.probe_records and self.probe_records[-1]["decision_number"] == decision_number:
            self.probe_records[-1].update(
                {
                    "pre_state": serialize_state(state),
                    "selected_assignment": dict(selection.assignment),
                    "selected_post_state": serialize_state(state.post_decision(selection.assignment)),
                    "policy_rng_state_after_selection": self.rng.getstate(),
                }
            )
        latency_ms = (perf_counter() - started) * 1000.0
        assigned: dict[str, str] = {}
        for worker in workers:
            opportunity = selection.assignment.get(worker.agent_id)
            task = mandatory.get(worker.agent_id) if opportunity == "__MANDATORY__" else tasks_by_worker.get(worker.agent_id, {}).get(str(opportunity))
            if task is None:
                continue
            task = self.world._annotate_task_selection(
                task,
                decision_source=(
                    "random_feasible_dispatch" if self.force_random_policy else "simulation_based_adp"
                ),
                decision_rule="mandatory_safety" if opportunity == "__MANDATORY__" else selection.policy,
                rationale="Joint event-driven worker-task assignment from the ADP policy.",
                candidate_count=len(state.opportunity_ids),
                score_hint=selection.predicted_value,
            )
            task.selection_meta.update(
                {
                    "adp_trigger": list(triggers),
                    "adp_predicted_value": round(float(selection.predicted_value), 6),
                    "adp_candidate_matching_count": int(selection.candidate_matching_count),
                }
            )
            finalized = self.world._finalize_selected_task(worker, task)
            if finalized is None:
                continue
            self.dispatch_queues[worker.agent_id].append(finalized)
            assigned[worker.agent_id] = str(opportunity)
            self.metrics["assigned_task_count"] += 1
            self._wake(worker.agent_id)
        self.metrics["decision_count"] += 1
        self.metrics["candidate_total"] += len(state.opportunity_ids)
        self.metrics["feasible_pair_total"] += int(state.feasibility.sum())
        self.metrics["candidate_matching_total"] += int(selection.candidate_matching_count)
        if (
            value_policy_selected
            and selection.candidate_value_entropy is not None
            and int(selection.candidate_value_count) >= 2
        ):
            self.metrics["beam_value_entropy_sum"] += float(
                selection.candidate_value_entropy
            )
            self.metrics["beam_value_entropy_decision_count"] += 1
        self.metrics["inference_latency_ms_total"] += latency_ms
        self.metrics["inference_latency_ms_max"] = max(float(self.metrics["inference_latency_ms_max"]), latency_ms)
        unassigned_workers = [
            worker_id for worker_id, value in selection.assignment.items() if value is None
        ]
        wait_workers = unassigned_workers if self.allow_wait_action else []
        candidate_available_wait_workers = [
            worker_id
            for worker_id in wait_workers
            if bool(state.tasks_by_worker.get(worker_id, {}))
        ]
        no_candidate_unassigned_workers = [
            worker_id
            for worker_id in wait_workers
            if not state.tasks_by_worker.get(worker_id, {})
        ]
        self.metrics["wait_count"] += len(wait_workers)
        self.metrics["candidate_available_wait_count"] += len(
            candidate_available_wait_workers
        )
        self.metrics["no_candidate_unassigned_count"] += len(
            no_candidate_unassigned_workers
        )
        all_wait = bool(workers) and len(wait_workers) == len(workers)
        all_wait_with_candidate = all_wait and bool(int(state.feasibility.sum()))
        all_wait_without_candidate = all_wait and not bool(int(state.feasibility.sum()))
        if all_wait:
            self.metrics["joint_all_wait_count"] += 1
            self.consecutive_all_wait_decisions += 1
            self.metrics["max_consecutive_all_wait_decisions"] = max(
                int(self.metrics["max_consecutive_all_wait_decisions"]),
                self.consecutive_all_wait_decisions,
            )
        else:
            self.consecutive_all_wait_decisions = 0
        if all_wait_with_candidate:
            self.metrics["joint_all_wait_with_candidate_count"] += 1
            self.consecutive_candidate_all_wait_decisions += 1
            self.metrics["max_consecutive_candidate_all_wait_decisions"] = max(
                int(self.metrics["max_consecutive_candidate_all_wait_decisions"]),
                self.consecutive_candidate_all_wait_decisions,
            )
        else:
            self.consecutive_candidate_all_wait_decisions = 0
        if all_wait_without_candidate:
            self.metrics["joint_no_candidate_count"] += 1
        self.world.logger.log(
            t=self.env.now,
            day=self.world.day_for_time(self.env.now),
            event_type="ADP_JOINT_DECISION",
            entity_id=f"ADP-{int(self.metrics['decision_count']):06d}",
            location="CoordinationReview",
            details={
                "trigger": triggers,
                "idle_workers": [worker.agent_id for worker in workers],
                "feasible_pair_count": int(state.feasibility.sum()),
                "candidate_task_count": len(state.opportunity_ids),
                "candidate_matching_count": int(selection.candidate_matching_count),
                "assignment": assigned,
                "wait_workers": wait_workers,
                "candidate_available_wait_workers": candidate_available_wait_workers,
                "no_candidate_unassigned_workers": no_candidate_unassigned_workers,
                "unassigned_workers": unassigned_workers,
                "all_wait": all_wait,
                "all_wait_with_candidate": all_wait_with_candidate,
                "all_wait_without_candidate": all_wait_without_candidate,
                "wait_action_enabled": self.allow_wait_action,
                "predicted_value": round(float(selection.predicted_value), 6),
                "beam_value_entropy": (
                    round(float(selection.candidate_value_entropy), 6)
                    if selection.candidate_value_entropy is not None
                    else None
                ),
                "beam_value_candidate_count": int(selection.candidate_value_count),
                "inference_latency_ms": round(latency_ms, 6),
                "policy": selection.policy,
                "worker_order_strategy": self.worker_order_strategy,
                "worker_order": list(selection.worker_order),
                "worker_order_start": (
                    selection.worker_order[0] if selection.worker_order else ""
                ),
            },
        )
        self.previous_pre_state = state
        self.previous_action = dict(selection.assignment)
        self.previous_post_state = state.post_decision(selection.assignment)
        self.previous_value_policy_selected = bool(value_policy_selected)
        self.previous_product_count = int(self.world.product_count)
        self.action_history.append(dict(selection.assignment))

    def finalize_episode(self) -> list[dict[str, Any]]:
        if self.episode_finalized:
            return self.transitions
        self._close_previous_transition(None, done=True)
        self.previous_pre_state = None
        self.previous_post_state = None
        self.previous_action = None
        self.previous_value_policy_selected = False
        self.episode_finalized = True
        return self.transitions

    def summary(self) -> dict[str, Any]:
        decisions = max(1, int(self.metrics["decision_count"]))
        return {
            "enabled": True,
            "checkpoint_id": str(self.checkpoint_manifest.get("checkpoint_id", "training" if self.training else "")),
            "checkpoint_path": str(self.checkpoint_manifest.get("checkpoint_path", "")),
            "validation_completed_products_avg": float(self.checkpoint_manifest.get("validation_completed_products_avg", 0.0) or 0.0),
            "reward_mode": self.checkpoint_manifest.get(
                "reward_mode", "completed_product_td" if self.cfg.get("_collect_td_replay") else "completed_product_mc"
            ),
            "return_estimator": self.checkpoint_manifest.get(
                "return_estimator", "n_step_td" if self.cfg.get("_collect_td_replay") else "monte_carlo"
            ),
            "worker_order_strategy": self.worker_order_strategy,
            "wait_action_enabled": self.allow_wait_action,
            "decision_count": int(self.metrics["decision_count"]),
            "wait_count": int(self.metrics["wait_count"]),
            "candidate_available_wait_count": int(
                self.metrics["candidate_available_wait_count"]
            ),
            "no_candidate_unassigned_count": int(
                self.metrics["no_candidate_unassigned_count"]
            ),
            "assigned_task_count": int(self.metrics["assigned_task_count"]),
            "worker_wait_ratio": float(self.metrics["wait_count"])
            / max(1, int(self.metrics["wait_count"]) + int(self.metrics["assigned_task_count"])),
            "candidate_available_wait_ratio": float(
                self.metrics["candidate_available_wait_count"]
            )
            / max(
                1,
                int(self.metrics["wait_count"])
                + int(self.metrics["assigned_task_count"]),
            ),
            "no_candidate_unassigned_ratio": float(
                self.metrics["no_candidate_unassigned_count"]
            )
            / max(
                1,
                int(self.metrics["wait_count"])
                + int(self.metrics["assigned_task_count"]),
            ),
            "joint_all_wait_count": int(self.metrics["joint_all_wait_count"]),
            "joint_all_wait_ratio": float(self.metrics["joint_all_wait_count"]) / decisions,
            "joint_all_wait_with_candidate_count": int(
                self.metrics["joint_all_wait_with_candidate_count"]
            ),
            "joint_all_wait_with_candidate_ratio": float(
                self.metrics["joint_all_wait_with_candidate_count"]
            )
            / decisions,
            "joint_no_candidate_count": int(self.metrics["joint_no_candidate_count"]),
            "joint_no_candidate_ratio": float(self.metrics["joint_no_candidate_count"])
            / decisions,
            "max_consecutive_all_wait_decisions": int(self.metrics["max_consecutive_all_wait_decisions"]),
            "max_consecutive_candidate_all_wait_decisions": int(
                self.metrics["max_consecutive_candidate_all_wait_decisions"]
            ),
            "avg_candidate_count": float(self.metrics["candidate_total"]) / decisions,
            "avg_feasible_pair_count": float(self.metrics["feasible_pair_total"]) / decisions,
            "avg_candidate_matching_count": float(self.metrics["candidate_matching_total"]) / decisions,
            "beam_value_entropy_avg": float(self.metrics["beam_value_entropy_sum"])
            / max(1, int(self.metrics["beam_value_entropy_decision_count"])),
            "beam_value_entropy_decision_count": int(
                self.metrics["beam_value_entropy_decision_count"]
            ),
            "inference_latency_ms_avg": float(self.metrics["inference_latency_ms_total"]) / decisions,
            "inference_latency_ms_max": float(self.metrics["inference_latency_ms_max"]),
        }
