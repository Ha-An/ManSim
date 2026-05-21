from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

import simpy

from manufacturing_sim.simulation.scenarios.manufacturing.entities import Task, Worker, default_humanoid_state_payload


TASK_CODE_BY_PRIORITY_KEY: dict[str, str] = {
    "material_supply": "REPLENISH_MATERIAL",
    "inter_station_transfer": "TRANSFER",
    "battery_swap": "MANAGE_ROBOT_POWER",
    "battery_delivery_low_battery": "TRANSFER",
    "battery_delivery_discharged": "TRANSFER",
    "setup_machine": "SETUP_MACHINE",
    "unload_machine": "UNLOAD_MACHINE",
    "inspect_product": "INSPECT_PRODUCT",
    "repair_machine": "REPAIR_MACHINE",
    "preventive_maintenance": "PREVENTIVE_MAINTENANCE",
    "handover_item": "HANDOVER_ITEM",
    "scrap_disposal": "COLLECT_WASTE_OR_SCRAP",
}


DOMAIN_ACTION_CALLS: dict[str, set[str]] = {
    "TRANSFER": {"GRASP"},
    "MANAGE_ROBOT_POWER": {"EXECUTE_SYSTEM_ACTION"},
    "SETUP_MACHINE": {"EXECUTE_MACHINE_ACTION"},
    "UNLOAD_MACHINE": {"EXECUTE_MACHINE_ACTION"},
    "INSPECT_PRODUCT": {"EXECUTE_QUALITY_ACTION"},
    "REPAIR_MACHINE": {"EXECUTE_MAINTENANCE_ACTION"},
    "PREVENTIVE_MAINTENANCE": {"EXECUTE_MAINTENANCE_ACTION"},
    "HANDOVER_ITEM": {"EXECUTE_HUMAN_COLLABORATION_ACTION"},
    "COLLECT_WASTE_OR_SCRAP": set(),
}


NESTED_DOMAIN_ACTION_CHILD_CALLS: dict[str, set[str]] = {
    "REPLENISH_MATERIAL": {"TRANSFER"},
    "SETUP_MACHINE": {"LOAD_MACHINE"},
    "PREVENTIVE_MAINTENANCE": {"INSPECT_MACHINE"},
    "COLLECT_WASTE_OR_SCRAP": {"TRANSFER"},
}


SUPPORTED_PRIMITIVE_CALLS: set[str] = {
    "CHECK_CONTEXT",
    "CHECK_REQUEST",
    "CHECK_SAFETY_ZONE",
    "CLASSIFY_RESULT",
    "ANNOUNCE_INTENT",
    "ALIGN",
    "CONFIRM_OPERATOR_STATE",
    "CREATE_OR_UPDATE_RECORD",
    "EXECUTE_HUMAN_COLLABORATION_ACTION",
    "EXECUTE_MACHINE_ACTION",
    "EXECUTE_MAINTENANCE_ACTION",
    "EXECUTE_QUALITY_ACTION",
    "EXECUTE_SYSTEM_ACTION",
    "GRASP",
    "INSPECT_OR_DIAGNOSE",
    "LIFT",
    "LOCALIZE_OBJECT",
    "LOG_RESULT",
    "NAVIGATE_TO",
    "PLACE",
    "PRIMITIVE_IDENTIFY_ITEM",
    "READ_MACHINE_STATE",
    "READ_CONTEXT",
    "REACH_TO",
    "RECORD_RESULT",
    "RELEASE",
    "UPDATE_RECORD",
    "UPDATE_INVENTORY_RECORD",
    "VERIFY_TRANSACTION",
    "VERIFY_LEVEL_OR_QUANTITY",
    "VERIFY_LOCKOUT_IF_REQUIRED",
    "VERIFY_MACHINE_STATE",
    "VERIFY_PLACEMENT",
    "VERIFY_ROBOT_STATE",
}


class HumanoidTaskRuntime:
    """Bridge HumanoidSim hierarchy into the ManSim discrete-event world.

    The runner owns catalog/profile validation and emits task/step events. Domain
    side effects still live in the world helper that mutates queues, machines,
    workers, and items; the helper is invoked from the semantic action primitive.
    """

    def __init__(self, world: Any, cfg: dict[str, Any]) -> None:
        self.world = world
        self.cfg = cfg if isinstance(cfg, dict) else {}
        humanoidsim_cfg = self.cfg.get("humanoidsim", {}) if isinstance(self.cfg.get("humanoidsim", {}), dict) else {}
        self.enabled = bool(humanoidsim_cfg.get("enabled", True))
        self.validation_mode = str(humanoidsim_cfg.get("validation_mode", "filter")).strip().lower() or "filter"
        self._imports: dict[str, Any] = {}
        self.catalog: Any | None = None
        self.profiles: dict[str, Any] = {}
        timing_cfg = humanoidsim_cfg.get("primitive_timing", {}) if isinstance(humanoidsim_cfg.get("primitive_timing", {}), dict) else {}
        self.primitive_timing_unit = str(timing_cfg.get("unit", "min")).strip().lower() or "min"
        self.default_primitive_min_duration = self._duration_to_minutes(timing_cfg.get("default_min", 0.0))
        by_call_code = timing_cfg.get("by_call_code", {}) if isinstance(timing_cfg.get("by_call_code", {}), dict) else {}
        self.primitive_min_duration_by_call_code = {
            str(call_code): self._duration_to_minutes(value)
            for call_code, value in by_call_code.items()
        }
        recovery_cfg = humanoidsim_cfg.get("recovery_protocol", {}) if isinstance(humanoidsim_cfg.get("recovery_protocol", {}), dict) else {}
        self.recovery_protocol_enabled = bool(recovery_cfg.get("enabled", True))
        self.recovery_timing_unit = str(recovery_cfg.get("unit", self.primitive_timing_unit)).strip().lower() or self.primitive_timing_unit
        self.default_recovery_step_duration = self._duration_to_minutes_with_unit(
            recovery_cfg.get("default_step_min", self.default_primitive_min_duration),
            self.recovery_timing_unit,
        )
        self.minimum_recovery_step_duration = self._duration_to_minutes_with_unit(
            recovery_cfg.get("minimum_step_min", self.default_recovery_step_duration),
            self.recovery_timing_unit,
        )
        by_recovery_code = recovery_cfg.get("by_code", {}) if isinstance(recovery_cfg.get("by_code", {}), dict) else {}
        self.recovery_duration_by_code = {
            str(code).strip().upper(): self._duration_to_minutes_with_unit(value, self.recovery_timing_unit)
            for code, value in by_recovery_code.items()
            if str(code).strip()
        }
        self.max_recovery_steps_per_incident = max(1, int(recovery_cfg.get("max_steps_per_incident", 8) or 8))
        if not self.enabled:
            return
        self._load_humanoidsim(humanoidsim_cfg)

    @property
    def supported_call_codes(self) -> set[str]:
        return set(SUPPORTED_PRIMITIVE_CALLS)

    def _duration_to_minutes(self, value: Any) -> float:
        return self._duration_to_minutes_with_unit(value, self.primitive_timing_unit)

    @staticmethod
    def _duration_to_minutes_with_unit(value: Any, unit: str) -> float:
        try:
            duration = float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0
        if str(unit).strip().lower() in {"s", "sec", "secs", "second", "seconds"}:
            duration /= 60.0
        return max(0.0, duration)

    def _primitive_min_duration(self, call_code: str) -> float:
        return max(
            0.0,
            float(self.primitive_min_duration_by_call_code.get(str(call_code), self.default_primitive_min_duration) or 0.0),
        )

    def _recovery_step_duration(self, code: str) -> float:
        configured = float(self.recovery_duration_by_code.get(str(code).strip().upper(), self.default_recovery_step_duration) or 0.0)
        return max(float(self.minimum_recovery_step_duration or 0.0), configured)

    def _load_humanoidsim(self, humanoidsim_cfg: dict[str, Any]) -> None:
        try:
            from humanoidsim import (
                StateReason,
                default_humanoid_state,
                load_task_catalog,
                StateTransitionEvent,
                transition_humanoid_state,
                validate_state_snapshot,
                validate_task_sequence,
                HumanoidProfile,
                expand_task_steps,
            )
            from humanoidsim.task_schema import TaskInstance
        except ModuleNotFoundError as exc:
            policy = str(humanoidsim_cfg.get("missing_package_policy", "error")).strip().lower()
            if policy == "disable":
                self.enabled = False
                return
            raise RuntimeError(
                "HumanoidSim integration is enabled, but the `humanoidsim` package is not installed. "
                "Install it with: .\\.venv\\Scripts\\python.exe -m pip install -e ..\\HumanoidSim"
            ) from exc

        catalog_root = humanoidsim_cfg.get("catalog_root")
        self.catalog = load_task_catalog(catalog_root if catalog_root else None)
        self._imports = {
            "HumanoidProfile": HumanoidProfile,
            "StateReason": StateReason,
            "TaskInstance": TaskInstance,
            "default_humanoid_state": default_humanoid_state,
            "validate_task_sequence": validate_task_sequence,
            "StateTransitionEvent": StateTransitionEvent,
            "transition_humanoid_state": transition_humanoid_state,
            "validate_state_snapshot": validate_state_snapshot,
            "expand_task_steps": expand_task_steps,
        }
        profiles_cfg = humanoidsim_cfg.get("profiles", {}) if isinstance(humanoidsim_cfg.get("profiles", {}), dict) else {}
        self.profiles = {
            str(agent_id): HumanoidProfile.from_dict({"humanoid_id": str(agent_id), **(profile if isinstance(profile, dict) else {})})
            for agent_id, profile in profiles_cfg.items()
        }
        for agent_id in self.world.agents.keys():
            self.profiles.setdefault(str(agent_id), self._default_profile(str(agent_id)))

    def _default_profile(self, agent_id: str) -> Any:
        profile_cls = self._imports["HumanoidProfile"]
        return profile_cls.from_dict(
            {
                "humanoid_id": str(agent_id),
                "capabilities": ["*"],
                "max_payload_kg": 25.0,
                "supported_tools": ["*"],
                "supported_vehicles": ["*"],
                "supported_equipment": ["*"],
            }
        )

    def ensure_humanoid_state(self, worker: Worker) -> dict[str, Any]:
        state = worker.humanoid_state if isinstance(worker.humanoid_state, dict) else {}
        if not state or not str(state.get("humanoid_id", "")).strip():
            default_state = self._imports.get("default_humanoid_state")
            if default_state is not None:
                state = default_state(worker.agent_id).to_dict()
            else:
                state = default_humanoid_state_payload(worker.agent_id)
        state["humanoid_id"] = worker.agent_id
        worker.humanoid_state = self._normalize_state_payload(worker, state)
        return copy.deepcopy(worker.humanoid_state)

    def state_payload(self, worker: Worker) -> dict[str, Any]:
        return self.ensure_humanoid_state(worker)

    def transition_state(
        self,
        worker: Worker,
        event_type: str,
        *,
        task: Task | None = None,
        step: dict[str, Any] | None = None,
        status: str = "",
        reason_code: str = "",
        reason_message: str = "",
        source: str = "mansim.state",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        current = self.ensure_humanoid_state(worker)
        event_cls = self._imports["StateTransitionEvent"]
        reason = self._reason(reason_code, source=source, message=reason_message, metadata=metadata) if reason_code else None
        task_context = task or self._task_from_worker(worker)
        event_metadata = self._state_metadata(worker, reason=reason_code, task_id=getattr(task_context, "task_id", None), source=source)
        event_metadata["cargo_present"] = bool(worker.carrying_item_id or getattr(worker, "carrying_item_ids", []))
        event_metadata.update(dict(metadata or {}))
        transition_event = event_cls(
            event_type=event_type,
            task_code=str(getattr(task_context, "task_code", "") or ""),
            task_instance_id=str(getattr(task_context, "instance_id", "") or ""),
            step_id=str((step or {}).get("step_id", "") or worker.current_step_id or ""),
            primitive_call_code=str((step or {}).get("call_code", "") or worker.current_primitive_call_code or ""),
            execution_status=self._execution_status(status, default="RUNNING") if status else None,
            reason=reason,
            timestamp_s=round(float(self.world.env.now), 3),
            metadata=event_metadata,
        )
        try:
            snapshot = self._imports["transition_humanoid_state"](current, transition_event, strict=True)
        except Exception as exc:
            raise RuntimeError(
                f"HumanoidSim state transition failed for worker={worker.agent_id} "
                f"event={event_type} task={getattr(task_context, 'task_code', '')} "
                f"primitive={transition_event.primitive_call_code}: {exc}"
            ) from exc
        worker.humanoid_state = self._normalize_state_payload(worker, snapshot.to_dict())

    def apply_transition_event(self, worker: Worker, transition_event: Any) -> None:
        if not self.enabled:
            return
        current = self.ensure_humanoid_state(worker)
        try:
            snapshot = self._imports["transition_humanoid_state"](current, transition_event, strict=True)
        except Exception as exc:
            event_type = getattr(transition_event, "event_type", "")
            primitive = getattr(transition_event, "primitive_call_code", "")
            task_code = getattr(transition_event, "task_code", "")
            raise RuntimeError(
                f"HumanoidSim state transition failed for worker={worker.agent_id} "
                f"event={event_type} task={task_code} primitive={primitive}: {exc}"
            ) from exc
        worker.humanoid_state = self._normalize_state_payload(worker, snapshot.to_dict())

    def sync_worker_cargo_state(self, worker: Worker, *, destination: str = "") -> None:
        self.transition_state(
            worker,
            "cargo_changed",
            reason_code="cargo_changed",
            source="mansim.cargo",
            metadata={"destination": destination, "cargo_present": bool(worker.carrying_item_id or getattr(worker, "carrying_item_ids", []))},
        )

    def set_disabled_state(self, worker: Worker, *, reason: str = "battery_depleted") -> None:
        self.transition_state(worker, "disabled", reason_code=reason, source="mansim.discharge")

    def set_task_lifecycle_state(self, worker: Worker, task: Task, *, event_type: str, status: str = "") -> None:
        recovery_context = self._recovery_context(task)
        if recovery_context is not None:
            incident_code = str(recovery_context.get("incident_code", "RECOVERY") or "RECOVERY").strip().upper()
            step_code = str(recovery_context.get("step_code", "") or "").strip().upper()
            if event_type == "HUMANOID_TASK_START":
                self.transition_state(
                    worker,
                    "blocked",
                    task=task,
                    status=status or "running",
                    reason_code=incident_code,
                    reason_message=f"Recovery step is running: {step_code}" if step_code else "Recovery step is running.",
                    source="mansim.recovery",
                    metadata={
                        "incident_code": incident_code,
                        "recovery_context": dict(recovery_context),
                    },
                )
                return
            if event_type == "HUMANOID_TASK_END":
                self.transition_state(
                    worker,
                    "blocked",
                    task=task,
                    status=status or "completed",
                    reason_code=incident_code,
                    reason_message=f"Recovery step completed: {step_code}" if step_code else "Recovery step completed.",
                    source="mansim.recovery",
                    metadata={
                        "incident_code": incident_code,
                        "recovery_context": dict(recovery_context),
                    },
                )
                return
        if event_type == "HUMANOID_TASK_START":
            self.transition_state(worker, "task_started", task=task, status=status or "running", source="mansim.humanoid_task")
            return
        if event_type == "HUMANOID_TASK_END":
            if str(status).strip().lower() in {"failed", "skipped", "interrupted"} and self._task_recovered_before_incomplete_end(worker, task):
                return
            current_availability = str((worker.humanoid_state or {}).get("availability", "")).strip().upper()
            if str(status).strip().lower() == "interrupted" and current_availability in {"BLOCKED", "DISABLED"}:
                return
            if worker.discharged:
                self.transition_state(worker, "disabled", task=task, status=status, reason_code="battery_depleted", source="mansim.task_end")
            elif str(status).strip().lower() == "completed":
                self.transition_state(
                    worker,
                    "task_completed",
                    task=task,
                    status=status,
                    reason_code="task_completed",
                    source="mansim.task_end",
                    metadata={"cargo_present": bool(worker.carrying_item_id or getattr(worker, "carrying_item_ids", []))},
                )
            elif str(status).strip().lower() == "failed":
                self.transition_state(worker, "blocked", task=task, status=status, reason_code=status or "task_failed", source="mansim.task_end")
            else:
                self.transition_state(worker, "waiting", task=task, status=status, reason_code=status or "task_waiting", source="mansim.task_end")
            return
        self.transition_state(worker, "task_started", task=task, status=status or "running", source="mansim.humanoid_task")

    def set_step_state(self, worker: Worker, task: Task, step: dict[str, Any], *, event_type: str, status: str) -> None:
        self.transition_state(
            worker,
            "primitive_finished" if event_type == "HUMANOID_STEP_END" else "primitive_started",
            task=task,
            step=step,
            status=status,
            source="mansim.humanoid_step",
        )

    @staticmethod
    def _recovery_context(task: Task) -> dict[str, Any] | None:
        context = (task.humanoid or {}).get("recovery_context")
        return dict(context) if isinstance(context, dict) else None

    @staticmethod
    def _task_recovered_before_incomplete_end(worker: Worker, task: Task) -> bool:
        recovered_attr = str(getattr(worker, "last_recovery_completed_task_id", "") or "").strip()
        if recovered_attr and recovered_attr == str(task.task_id or "").strip():
            return True
        state = worker.humanoid_state if isinstance(worker.humanoid_state, dict) else {}
        metadata = state.get("metadata")
        if not isinstance(metadata, dict) or str(metadata.get("source", "")).strip() != "mansim.recovery_end":
            return False
        task_id = str(task.task_id or "").strip()
        recovered_task_id = str(metadata.get("task_id", "") or "").strip()
        return bool(task_id and task_id == recovered_task_id)

    def _normalize_state_payload(self, worker: Worker, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = copy.deepcopy(payload)
        normalized["humanoid_id"] = worker.agent_id
        normalized.setdefault("availability", "AVAILABLE")
        normalized.setdefault("mobility", "STATIONARY")
        normalized.setdefault("power", "POWER_NORMAL")
        normalized.setdefault("manipulation", "FREE")
        normalized.setdefault("task_context", None)
        normalized.setdefault("reason", None)
        normalized.setdefault("metadata", {})
        try:
            issues = self._imports["validate_state_snapshot"](normalized)
        except Exception as exc:
            raise RuntimeError(f"HumanoidSim state validation failed for {worker.agent_id}: {type(exc).__name__}: {exc}") from exc
        if issues:
            rendered = [
                asdict(issue) if hasattr(issue, "__dataclass_fields__") else dict(issue)
                for issue in issues
            ]
            raise RuntimeError(f"HumanoidSim state validation issues for {worker.agent_id}: {rendered}")
        return normalized

    def _task_context_from_worker(self, worker: Worker) -> dict[str, Any] | None:
        if not any([worker.current_task_id, worker.current_task_code, worker.current_step_id, worker.current_primitive_call_code]):
            return None
        return {
            "task_code": worker.current_task_code or None,
            "task_instance_id": worker.current_task_instance_id or None,
            "step_id": worker.current_step_id or None,
            "primitive_call_code": worker.current_primitive_call_code or None,
            "execution_status": "RUNNING" if worker.current_step_id or worker.current_primitive_call_code else "PENDING",
        }

    def _task_from_worker(self, worker: Worker) -> Task:
        return Task(
            task_id=str(worker.current_child_task_instance_id or worker.current_task_id or ""),
            task_type=str(worker.current_child_task_code or worker.current_task_type or ""),
            priority_key="",
            priority=0.0,
            location=str(getattr(worker, "location", "")),
            task_code=str(worker.current_child_task_code or worker.current_task_code or ""),
            instance_id=str(worker.current_child_task_instance_id or worker.current_task_instance_id or ""),
            assigned_robot_id=worker.agent_id,
            task_spec_name=str(worker.current_child_task_name or ""),
        )

    def _reason(self, code: str, *, source: str, message: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        reason_cls = self._imports.get("StateReason")
        if reason_cls is None:
            return {"code": str(code), "message": str(message), "source": str(source), "metadata": dict(metadata or {})}
        return reason_cls(code=str(code), message=str(message), source=str(source), metadata=dict(metadata or {})).to_dict()

    def _state_metadata(self, worker: Worker, **extra: Any) -> dict[str, Any]:
        metadata = {
            "source": "mansim",
            "battery_remaining_min": round(float(self.world.battery_remaining(worker)), 3),
        }
        metadata.update({key: value for key, value in extra.items() if value not in {None, ""}})
        return metadata

    @staticmethod
    def _execution_status(status: str, *, default: str = "RUNNING") -> str:
        normalized = str(status or "").strip().lower()
        if normalized in {"running", "start", "started"}:
            return "RUNNING"
        if normalized in {"completed", "success", "succeeded"}:
            return "SUCCESS"
        if normalized in {"failed", "error"}:
            return "FAILED"
        if normalized in {"interrupted", "aborted"}:
            return "ABORTED"
        if normalized in {"skipped"}:
            return "SKIPPED"
        return default

    def bind_candidate(self, agent: Worker, task: Task) -> Task | None:
        if not self.enabled or self.catalog is None:
            return task
        priority_key = self.world._task_priority_key(task)
        task_code = TASK_CODE_BY_PRIORITY_KEY.get(priority_key)
        if not task_code:
            return task
        try:
            spec = self.catalog.get(task_code)
        except KeyError:
            self._log_rejected(agent, task, task_code, [{"code": "UNKNOWN_TASK", "message": f"Unknown Humanoid task_code={task_code}"}])
            return None

        instance = self._build_task_instance(agent, task, task_code)
        validation = self._imports["validate_task_sequence"](
            {agent.agent_id: self.profiles.get(agent.agent_id, self._default_profile(agent.agent_id))},
            [instance],
            catalog=self.catalog,
        )
        issues = [self._issue_to_dict(issue) for issue in getattr(validation, "issues", [])]
        if not bool(getattr(validation, "ok", False)):
            self._log_rejected(agent, task, task_code, issues)
            if self.validation_mode == "warn":
                pass
            else:
                return None

        task.task_code = task_code
        task.instance_id = instance.instance_id
        task.assigned_robot_id = agent.agent_id
        task.args = dict(instance.args)
        task.task_spec_name = str(spec.name or task_code)
        task.step_plan = self._step_plan(spec, task.args)
        task.humanoid = {
            "task_code": task_code,
            "task_name": task.task_spec_name,
            "instance_id": task.instance_id,
            "assigned_robot_id": agent.agent_id,
            "validation_ok": bool(getattr(validation, "ok", False)),
            "validation_issues": issues,
            "animation_frames": list(spec.metadata.get("animation", {}).get("frames", [])),
            "required_capabilities": list(getattr(spec, "required_capabilities", []) or []),
        }
        return task

    def _build_task_instance(self, agent: Worker, task: Task, task_code: str) -> Any:
        instance_cls = self._imports["TaskInstance"]
        return instance_cls(
            instance_id=f"{task.task_id}:{task_code}",
            task_code=task_code,
            args=self._args_for_task(agent, task, task_code),
            assigned_robot_id=agent.agent_id,
            priority=int(round(float(task.priority))),
            metadata={
                "mansim_task_id": task.task_id,
                "task_type": task.task_type,
                "priority_key": self.world._task_priority_key(task),
                "payload": dict(task.payload),
            },
        )

    def _args_for_task(self, agent: Worker, task: Task, task_code: str) -> dict[str, Any]:
        payload = task.payload if isinstance(task.payload, dict) else {}
        priority_key = self.world._task_priority_key(task)
        if task_code == "REPLENISH_MATERIAL":
            station = int(payload.get("station", 1) or 1)
            return {
                "item": {"entity_type": "material", "entity_id": payload.get("transfer_item_id") or f"material_station_{station}"},
                "source": "Warehouse",
                "destination": f"material_queue_{station}",
                "rule": {"station": station, "target_level": self.world.inventory_targets.get("material", {}).get(f"station{station}")},
            }
        if task_code == "TRANSFER":
            transfer_kind = str(payload.get("transfer_kind", "")).strip().lower()
            if transfer_kind == "battery_delivery":
                return {
                    "item": {"entity_type": "battery", "entity_id": payload.get("transfer_item_id") or "fresh_battery"},
                    "source": "battery_rack",
                    "destination": str(payload.get("target_agent_id", "")),
                }
            from_station = int(payload.get("from_station", 0) or 0)
            if from_station == self.world.inspection_queue_station:
                destination = "completed_product_buffer"
                item_type = "product"
            else:
                destination = f"intermediate_queue_{from_station + 1}"
                item_type = "intermediate"
            return {
                "item": {"entity_type": item_type, "entity_id": payload.get("transfer_item_id") or f"output_station_{from_station}"},
                "source": f"output_buffer_station_{from_station}",
                "destination": destination,
            }
        if task_code == "MANAGE_ROBOT_POWER":
            return {"robot": agent.agent_id, "action": "swap_battery", "station": "battery_rack", "target_soc": 1.0}
        if task_code == "SETUP_MACHINE":
            return {"machine": str(payload.get("machine_id", "")), "setup_spec": {"station": payload.get("station"), "payload": dict(payload)}}
        if task_code == "UNLOAD_MACHINE":
            machine_id = str(payload.get("machine_id", ""))
            return {
                "machine": machine_id,
                "item": {"entity_type": "machine_output", "entity_id": machine_id},
                "destination": f"output_buffer_station_{payload.get('station', '')}",
            }
        if task_code == "INSPECT_PRODUCT":
            return {
                "target": payload.get("inspection_product_id") or "inspection_input_queue",
                "inspection_plan": {
                    "station": self.world.inspection_queue_station,
                    "defect_prob": self.world.quality_cfg.get("defect_prob"),
                    "base_time_min": self.world.inspection_base_time_min,
                },
            }
        if task_code == "REPAIR_MACHINE":
            return {
                "machine": str(payload.get("machine_id", "")),
                "fault": {"state": "BROKEN", "remaining_min": payload.get("repair_remaining_min")},
                "repair_procedure": {"max_repair_agents": self.world.max_repair_agents, "priority_key": priority_key},
            }
        if task_code == "PREVENTIVE_MAINTENANCE":
            return {"asset": str(payload.get("machine_id", "")), "checklist": {"station": payload.get("station"), "priority_key": priority_key}}
        if task_code == "HANDOVER_ITEM":
            item_id = str(payload.get("item_id") or payload.get("transfer_item_id") or "")
            item_type = str(payload.get("item_type") or "product")
            recipient_id = str(payload.get("recipient_agent_id") or agent.agent_id)
            source_id = str(payload.get("source_agent_id") or "")
            return {
                "item": {"entity_type": item_type, "entity_id": item_id},
                "recipient": {"entity_type": "robot", "entity_id": recipient_id},
                "handover_spec": {
                    "mode": str(payload.get("handover_kind") or "product_collaboration_join"),
                    "source_agent_id": source_id,
                    "recipient_agent_id": recipient_id,
                    "transport_session_id": str(payload.get("transport_session_id") or ""),
                    "destination": str(payload.get("destination") or ""),
                    "max_carriers": int(payload.get("max_carriers", 2) or 2),
                },
            }
        if task_code == "COLLECT_WASTE_OR_SCRAP":
            item_ids = payload.get("item_ids")
            if not isinstance(item_ids, list):
                item_ids = []
            return {
                "item": {
                    "entity_type": "scrap_batch",
                    "entity_ids": [str(item_id) for item_id in item_ids],
                },
                "waste_or_scrap": {
                    "entity_type": "scrap_batch",
                    "entity_ids": [str(item_id) for item_id in item_ids],
                },
                "items": {
                    "entity_type": "scrap_batch",
                    "entity_ids": [str(item_id) for item_id in item_ids],
                },
                "source": str(payload.get("source") or "inspection_scrap_queue"),
                "destination": str(payload.get("destination") or "scrap_disposal_bin"),
                "sorting_rule": {
                    "max_carry_count": int(payload.get("max_carry_count", getattr(self.world, "scrap_transport_max_carry_count", 3)) or 3),
                    "item_type": "product",
                },
            }
        return dict(payload)

    def _step_plan(self, spec: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
        expand = self._imports.get("expand_task_steps")
        if expand is None:
            return [
                {
                    "path": str(step.step_id),
                    "depth": 1,
                    "parent_task_code": str(getattr(spec, "code", "")),
                    "step_id": str(step.step_id),
                    "call_code": str(step.call_code),
                    "call_level": "PRIMITIVE_SKILL",
                    "args": dict(getattr(step, "args", {}) or {}),
                    "depends_on": [str(item) for item in getattr(step, "depends_on", [])],
                    "optional": bool(getattr(step, "optional", False)),
                }
                for step in getattr(spec, "steps", []) or []
            ]
        return [dict(row) for row in expand(str(spec.code), dict(args or {}), catalog=self.catalog)]

    def execute(self, agent: Worker, task: Task):
        if not self.enabled:
            result = yield from self.world._execute_task_domain_action(agent, task)
            return result
        if not task.task_code:
            bound = self.bind_candidate(agent, task)
            if bound is None:
                return False
            task = bound

        self._log_task_event("HUMANOID_TASK_START", agent, task, status="running")
        executed_domain_action = False
        success = True
        end_status = "failed"
        end_logged = False
        active_step: dict[str, Any] | None = None
        active_children: list[tuple[str, Task]] = []
        skipped_prefixes: set[str] = set()
        try:
            steps = list(task.step_plan or [])
            if not steps:
                success = bool((yield from self.world._execute_task_domain_action(agent, task)))
                executed_domain_action = True
                if not success:
                    yield from self._execute_recovery_after_failure(agent, task)
            for step in steps:
                path = str(step.get("path", step.get("step_id", "")) or "")
                if self._path_is_skipped(path, skipped_prefixes):
                    continue
                while active_children and not self._path_is_descendant(path, active_children[-1][0]):
                    _, child_task = active_children.pop()
                    self._log_child_task_event("HUMANOID_TASK_END", agent, task, child_task, step, status="completed")

                call_level = str(step.get("call_level", "PRIMITIVE_SKILL") or "PRIMITIVE_SKILL")
                call_code = str(step.get("call_code", ""))
                if call_level != "PRIMITIVE_SKILL":
                    child_task = self._child_task_from_step(task, step)
                    active_children.append((path, child_task))
                    self._log_child_task_event("HUMANOID_TASK_START", agent, task, child_task, step, status="running")
                    if self._is_nested_domain_action_step(task, step) and not executed_domain_action:
                        active_step = step
                        step_ok = bool((yield from self.world._execute_task_domain_action(agent, task)))
                        executed_domain_action = True
                        active_step = None
                        _, finished_child = active_children.pop()
                        self._log_child_task_event(
                            "HUMANOID_TASK_END",
                            agent,
                            task,
                            finished_child,
                            step,
                            status="completed" if step_ok else "failed",
                        )
                        skipped_prefixes.add(path)
                        if not step_ok:
                            yield from self._execute_recovery_after_failure(agent, task, step)
                            success = False
                            break
                    continue

                if call_code not in SUPPORTED_PRIMITIVE_CALLS:
                    self._log_step_event("HUMANOID_STEP_END", agent, task, step, status="failed", error=f"Unsupported primitive {call_code}")
                    return False
                context_task = active_children[-1][1] if active_children else task
                agent.current_step_id = str(step.get("step_id", ""))
                agent.current_primitive_call_code = call_code
                active_step = step
                self._log_step_event("HUMANOID_STEP_START", agent, context_task, step, status="running", parent_task=task)
                step_ok = yield from self._execute_step(agent, task, step, executed_domain_action, allow_domain_action=not bool(active_children))
                if not active_children and self._is_domain_action_step(task, call_code):
                    executed_domain_action = True
                self._log_step_event("HUMANOID_STEP_END", agent, context_task, step, status="completed" if step_ok else "failed", parent_task=task)
                active_step = None
                if not step_ok:
                    yield from self._execute_recovery_after_failure(agent, task, step)
                    success = False
                    break
            while active_children:
                _, child_task = active_children.pop()
                self._log_child_task_event("HUMANOID_TASK_END", agent, task, child_task, {}, status="completed" if success else "failed")
            if success and not executed_domain_action:
                success = bool((yield from self.world._execute_task_domain_action(agent, task)))
                if not success:
                    yield from self._execute_recovery_after_failure(agent, task)
            end_status = "completed" if success else "failed"
            return bool(success)
        except simpy.Interrupt as intr:
            if active_step is not None:
                interrupted_step = self._interrupted_step_for_agent(agent, active_step)
                self._log_step_event(
                    "HUMANOID_STEP_END",
                    agent,
                    task,
                    interrupted_step,
                    status="interrupted",
                    error=str(intr.cause or "interrupted"),
                )
                active_step = None
            if getattr(agent, "pending_recovery_incident", None) is not None:
                yield from self._execute_pending_recovery_protocol(agent, task)
            self._log_task_event("HUMANOID_TASK_END", agent, task, status="interrupted")
            end_logged = True
            raise
        finally:
            while active_children:
                _, child_task = active_children.pop()
                self._log_child_task_event("HUMANOID_TASK_END", agent, task, child_task, {}, status="interrupted")
            agent.current_step_id = None
            agent.current_primitive_call_code = None
            agent.current_child_task_code = None
            agent.current_child_task_name = None
            agent.current_child_task_instance_id = None
            agent.current_task_path = None
            agent.current_task_depth = 0
            if not end_logged and not agent.discharged:
                self._log_task_event("HUMANOID_TASK_END", agent, task, status=end_status)

    def _execute_step(
        self,
        agent: Worker,
        task: Task,
        step: dict[str, Any],
        executed_domain_action: bool,
        *,
        allow_domain_action: bool = True,
    ):
        call_code = str(step.get("call_code", ""))
        start_t = float(self.world.env.now)
        result = True
        if allow_domain_action and self._is_domain_action_step(task, call_code) and not executed_domain_action:
            result = bool((yield from self.world._execute_task_domain_action(agent, task)))
        elif call_code in {"READ_MACHINE_STATE", "VERIFY_MACHINE_STATE"} and task.payload.get("machine_id"):
            result = str(task.payload.get("machine_id")) in self.world.machines
        elif call_code in {"CHECK_SAFETY_ZONE", "REACH_TO", "VERIFY_LOCKOUT_IF_REQUIRED"}:
            result = True
        elif call_code in {"LOCALIZE_OBJECT", "PRIMITIVE_IDENTIFY_ITEM"}:
            result = not self.world._maybe_random_humanoid_step_incident(agent, task, step, call_code)
        elif call_code in {"GRASP"}:
            result = not self.world._maybe_random_humanoid_step_incident(agent, task, step, call_code)
        elif call_code in {"LOG_RESULT", "UPDATE_RECORD", "CREATE_OR_UPDATE_RECORD", "RECORD_RESULT"}:
            result = True
        elapsed = max(0.0, float(self.world.env.now) - start_t)
        remaining = self._primitive_min_duration(call_code) - elapsed
        if remaining > 1e-9:
            yield self.world.env.timeout(remaining)
        return bool(result)

    @staticmethod
    def _interrupted_step_for_agent(agent: Worker, active_step: dict[str, Any]) -> dict[str, Any]:
        """Use the current domain-internal primitive when an interrupt happens mid-action."""
        step = dict(active_step)
        current_primitive = str(agent.current_primitive_call_code or "").strip()
        active_primitive = str(step.get("call_code", "") or "").strip()
        mobility = str((agent.humanoid_state or {}).get("mobility", "")).strip().upper()
        if mobility == "NAVIGATING" and active_primitive != "NAVIGATE_TO":
            current_primitive = "NAVIGATE_TO"
        elif mobility == "DOCKING" and active_primitive != "ALIGN":
            current_primitive = "ALIGN"
        if current_primitive and current_primitive != active_primitive:
            step["call_code"] = current_primitive
            if agent.current_step_id:
                step["step_id"] = str(agent.current_step_id)
        return step

    def _ensure_pending_recovery_incident_for_failure(
        self,
        agent: Worker,
        task: Task,
        step: dict[str, Any] | None = None,
    ) -> bool:
        """Convert a failed domain action reason into a HumanoidSim incident.

        Some domain helpers return ``False`` with ``task.payload["failure_reason"]``
        instead of emitting ``HUMANOID_INCIDENT`` directly. Recovery must still use
        the HumanoidSim incident taxonomy, so synthesize the canonical incident here
        before the pending recovery runner is invoked.
        """
        if isinstance(getattr(agent, "pending_recovery_incident", None), dict):
            return True
        emit_incident = getattr(self.world, "_emit_humanoid_incident", None)
        if not callable(emit_incident):
            return False
        failure_reason = str((task.payload or {}).get("failure_reason", "") or "").strip()
        if not failure_reason:
            failure_reason = "precondition_failed"
        primitive = str((step or {}).get("call_code", "") or agent.current_primitive_call_code or "").strip().upper()
        context = {
            "failure_reason": failure_reason,
            "task_id": task.task_id,
            "task_code": task.task_code or task.task_type,
        }
        try:
            emit_incident(
                agent,
                failure_reason,
                task=task,
                step=step,
                primitive_call_code=primitive,
                source="mansim.task_failure",
                context=context,
                notify_worker=True,
            )
        except RuntimeError:
            emit_incident(
                agent,
                "UNKNOWN",
                task=task,
                step=step,
                primitive_call_code=primitive,
                source="mansim.task_failure",
                context=context,
                notify_worker=True,
            )
        return isinstance(getattr(agent, "pending_recovery_incident", None), dict)

    def _execute_recovery_after_failure(self, agent: Worker, task: Task, step: dict[str, Any] | None = None):
        self._ensure_pending_recovery_incident_for_failure(agent, task, step)
        result = yield from self._execute_pending_recovery_protocol(agent, task)
        return result

    def _execute_pending_recovery_protocol(self, agent: Worker, parent_task: Task):
        if not self.recovery_protocol_enabled:
            agent.pending_recovery_incident = None
            return False
        incident = agent.pending_recovery_incident if isinstance(agent.pending_recovery_incident, dict) else None
        if not incident:
            return False
        agent.pending_recovery_incident = None
        protocol = incident.get("recovery_protocol", [])
        if not isinstance(protocol, list) or not protocol:
            return False

        steps = [dict(step) for step in protocol if isinstance(step, dict) and str(step.get("code", "")).strip()]
        if not steps:
            return False
        steps = steps[: self.max_recovery_steps_per_incident]
        incident_code = str(incident.get("incident_code", "UNKNOWN") or "UNKNOWN").strip().upper()
        recovery_id = f"{parent_task.instance_id or parent_task.task_id}:recovery:{incident_code}:{int(float(self.world.env.now) * 1000):08d}"
        self.transition_state(
            agent,
            "blocked",
            task=parent_task,
            status="failed",
            reason_code=incident_code,
            source="mansim.recovery_start",
            metadata={"incident_code": incident_code, "recovery_id": recovery_id},
        )
        self.world.logger.log(
            t=self.world.env.now,
            day=self.world.day_for_time(self.world.env.now),
            event_type="HUMANOID_RECOVERY_START",
            entity_id=agent.agent_id,
            location=self.world.agent_display_location(agent),
            details={
                "recovery_id": recovery_id,
                "incident_code": incident_code,
                "parent_task_id": parent_task.task_id,
                "parent_task_code": parent_task.task_code,
                "recovery_protocol": copy.deepcopy(steps),
                "humanoid_state": self.state_payload(agent),
            },
        )
        try:
            for index, step in enumerate(steps, start=1):
                kind = str(step.get("kind", "") or "").strip().lower()
                code = str(step.get("code", "") or "").strip().upper()
                if not code:
                    continue
                recovery_context = {
                    "active": True,
                    "recovery_id": recovery_id,
                    "incident_code": incident_code,
                    "step_index": index,
                    "step_count": len(steps),
                    "step_kind": kind,
                    "step_code": code,
                    "optional": bool(step.get("optional", False)),
                }
                if kind == "primitive":
                    yield from self._execute_recovery_primitive_step(agent, parent_task, step, recovery_context)
                elif kind == "task":
                    yield from self._execute_recovery_task_step(agent, parent_task, step, recovery_context)
        finally:
            if not agent.discharged:
                self.transition_state(
                    agent,
                    "task_completed",
                    task=parent_task,
                    status="completed",
                    reason_code="recovery_completed",
                    source="mansim.recovery_end",
                    metadata={"cargo_present": bool(agent.carrying_item_id or getattr(agent, "carrying_item_ids", []))},
                )
                agent.last_recovery_completed_task_id = str(parent_task.task_id or "")
                agent.last_recovery_completed_at = float(self.world.env.now)
            self.world.logger.log(
                t=self.world.env.now,
                day=self.world.day_for_time(self.world.env.now),
                event_type="HUMANOID_RECOVERY_END",
                entity_id=agent.agent_id,
                location=self.world.agent_display_location(agent),
                details={
                    "recovery_id": recovery_id,
                    "incident_code": incident_code,
                    "parent_task_id": parent_task.task_id,
                    "parent_task_code": parent_task.task_code,
                    "humanoid_state": self.state_payload(agent),
                },
            )
        return True

    def _execute_recovery_task_step(self, agent: Worker, parent_task: Task, step: dict[str, Any], recovery_context: dict[str, Any]):
        code = str(step.get("code", "") or "").strip().upper()
        recovery_task = self._recovery_task_from_step(parent_task, code, recovery_context)
        self._log_task_event("HUMANOID_TASK_START", agent, recovery_task, status="running")
        yield self.world.env.timeout(self._recovery_step_duration(code))
        inactive_context = dict(recovery_context)
        inactive_context["active"] = False
        recovery_task.humanoid["recovery_context"] = inactive_context
        self._log_task_event("HUMANOID_TASK_END", agent, recovery_task, status="completed")

    def _execute_recovery_primitive_step(self, agent: Worker, parent_task: Task, step: dict[str, Any], recovery_context: dict[str, Any]):
        code = str(step.get("code", "") or "").strip().upper()
        recovery_task = self._recovery_task_from_step(parent_task, parent_task.task_code or parent_task.task_type, recovery_context)
        step_row = {
            "path": f"recovery/{recovery_context['incident_code']}/{recovery_context['step_index']:02d}_{code.lower()}",
            "depth": 1,
            "parent_task_code": str(parent_task.task_code or parent_task.task_type or ""),
            "step_id": f"recovery_{recovery_context['step_index']:02d}_{code.lower()}",
            "call_code": code,
            "call_level": "PRIMITIVE_SKILL",
            "args": {},
            "depends_on": [],
            "optional": bool(step.get("optional", False)),
        }
        previous_step_id = agent.current_step_id
        previous_primitive = agent.current_primitive_call_code
        self._log_task_event("HUMANOID_TASK_START", agent, recovery_task, status="running")
        agent.current_step_id = str(step_row["step_id"])
        agent.current_primitive_call_code = code
        self._log_step_event("HUMANOID_STEP_START", agent, recovery_task, step_row, status="running", parent_task=parent_task)
        yield self.world.env.timeout(self._recovery_step_duration(code))
        inactive_context = dict(recovery_context)
        inactive_context["active"] = False
        recovery_task.humanoid["recovery_context"] = inactive_context
        self._log_step_event("HUMANOID_STEP_END", agent, recovery_task, step_row, status="completed", parent_task=parent_task)
        self._log_task_event("HUMANOID_TASK_END", agent, recovery_task, status="completed")
        agent.current_step_id = previous_step_id
        agent.current_primitive_call_code = previous_primitive

    def _recovery_task_from_step(self, parent_task: Task, task_code: str, recovery_context: dict[str, Any]) -> Task:
        code = str(task_code or "RECOVERY").strip().upper()
        recovery_id = str(recovery_context.get("recovery_id", "recovery"))
        step_index = int(recovery_context.get("step_index", 0) or 0)
        return Task(
            task_id=f"{parent_task.task_id}:{recovery_id}:step{step_index:02d}:{code}",
            task_type=code,
            priority_key=parent_task.priority_key,
            priority=parent_task.priority,
            location=parent_task.location,
            payload=dict(parent_task.payload),
            selection_meta=dict(parent_task.selection_meta),
            task_code=code,
            instance_id=f"{parent_task.instance_id or parent_task.task_id}/{recovery_id}/step{step_index:02d}:{code}",
            assigned_robot_id=parent_task.assigned_robot_id or "",
            args={},
            task_spec_name=f"{code} (RECOVERY)",
            step_plan=[],
            humanoid={
                "recovery_context": dict(recovery_context),
                "parent_task_code": parent_task.task_code,
                "parent_instance_id": parent_task.instance_id,
            },
        )

    def _is_domain_action_step(self, task: Task, call_code: str) -> bool:
        return str(call_code) in DOMAIN_ACTION_CALLS.get(str(task.task_code), set())

    def _is_nested_domain_action_step(self, task: Task, step: dict[str, Any]) -> bool:
        return str(step.get("call_code", "")) in NESTED_DOMAIN_ACTION_CHILD_CALLS.get(str(task.task_code), set())

    def _log_task_event(self, event_type: str, agent: Worker, task: Task, *, status: str) -> None:
        self.set_task_lifecycle_state(agent, task, event_type=event_type, status=status)
        self.world.logger.log(
            t=self.world.env.now,
            day=self.world.day_for_time(self.world.env.now),
            event_type=event_type,
            entity_id=agent.agent_id,
            location=self.world.agent_display_location(agent),
            details=self._task_event_details(agent, task, status=status),
        )

    def _log_step_event(
        self,
        event_type: str,
        agent: Worker,
        task: Task,
        step: dict[str, Any],
        *,
        status: str,
        error: str = "",
        parent_task: Task | None = None,
    ) -> None:
        if event_type == "HUMANOID_STEP_END" and str(status).strip().lower() == "interrupted":
            step = self._interrupted_step_for_agent(agent, step)
        if str(step.get("call_level", "PRIMITIVE_SKILL") or "PRIMITIVE_SKILL") == "PRIMITIVE_SKILL":
            self.set_step_state(agent, task, step, event_type=event_type, status=status)
        details = self._task_event_details(agent, task, status=status)
        details.update(
            {
                "step_id": str(step.get("step_id", "")),
                "primitive_call_code": str(step.get("call_code", "")),
                "call_level": str(step.get("call_level", "PRIMITIVE_SKILL")),
                "task_path": str(step.get("path", "")),
                "depth": int(step.get("depth", 0) or 0),
                "parent_task_code": str((parent_task or task).task_code or ""),
                "parent_instance_id": str((parent_task or task).instance_id or ""),
                "depends_on": list(step.get("depends_on", [])),
                "error": error,
            }
        )
        self.world.logger.log(
            t=self.world.env.now,
            day=self.world.day_for_time(self.world.env.now),
            event_type=event_type,
            entity_id=agent.agent_id,
            location=self.world.agent_display_location(agent),
            details=details,
        )

    def _log_child_task_event(
        self,
        event_type: str,
        agent: Worker,
        parent_task: Task,
        child_task: Task,
        step: dict[str, Any],
        *,
        status: str,
    ) -> None:
        path = str(step.get("path", agent.current_task_path or "") or "")
        depth = int(step.get("depth", agent.current_task_depth or 0) or 0)
        if event_type == "HUMANOID_TASK_START":
            agent.current_child_task_code = child_task.task_code
            agent.current_child_task_name = child_task.task_spec_name
            agent.current_child_task_instance_id = child_task.instance_id
            agent.current_task_path = path
            agent.current_task_depth = depth
            self.set_task_lifecycle_state(agent, child_task, event_type=event_type, status=status)

        details = self._task_event_details(agent, child_task, status=status)
        details.update(
            {
                "parent_task_id": parent_task.task_id,
                "parent_task_type": parent_task.task_type,
                "parent_task_code": parent_task.task_code,
                "parent_instance_id": parent_task.instance_id,
                "child_task_code": child_task.task_code,
                "child_task_name": child_task.task_spec_name,
                "child_instance_id": child_task.instance_id,
                "task_path": path,
                "depth": depth,
            }
        )
        self.world.logger.log(
            t=self.world.env.now,
            day=self.world.day_for_time(self.world.env.now),
            event_type=event_type,
            entity_id=agent.agent_id,
            location=self.world.agent_display_location(agent),
            details=details,
        )
        if event_type == "HUMANOID_TASK_END":
            agent.current_child_task_code = None
            agent.current_child_task_name = None
            agent.current_child_task_instance_id = None
            agent.current_task_path = None
            agent.current_task_depth = 0
            self.set_task_lifecycle_state(agent, parent_task, event_type="HUMANOID_TASK_START", status="running")

    def _task_event_details(self, agent: Worker, task: Task, *, status: str) -> dict[str, Any]:
        details = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "priority_key": self.world._task_priority_key(task),
            "task_code": task.task_code,
            "task_name": task.task_spec_name,
            "instance_id": task.instance_id,
            "assigned_robot_id": task.assigned_robot_id,
            "child_task_code": agent.current_child_task_code or "",
            "child_task_name": agent.current_child_task_name or "",
            "child_instance_id": agent.current_child_task_instance_id or "",
            "task_path": agent.current_task_path or "",
            "depth": int(agent.current_task_depth or 0),
            "status": status,
            "args": dict(task.args),
            "payload": dict(task.payload),
            "animation_frames": list((task.humanoid or {}).get("animation_frames", [])),
            "humanoid_state": self.state_payload(agent),
        }
        recovery_context = (task.humanoid or {}).get("recovery_context")
        if isinstance(recovery_context, dict):
            details["recovery_context"] = dict(recovery_context)
        return details

    def _log_rejected(self, agent: Worker, task: Task, task_code: str, issues: list[dict[str, Any]]) -> None:
        self.world.logger.log(
            t=self.world.env.now,
            day=self.world.day_for_time(self.world.env.now),
            event_type="HUMANOID_TASK_REJECTED",
            entity_id=agent.agent_id,
            location=self.world.agent_display_location(agent),
            details={
                "task_id": task.task_id,
                "task_type": task.task_type,
                "priority_key": self.world._task_priority_key(task),
                "task_code": task_code,
                "issues": issues,
                "payload": dict(task.payload),
            },
        )

    def _issue_to_dict(self, issue: Any) -> dict[str, Any]:
        if hasattr(issue, "__dataclass_fields__"):
            return asdict(issue)
        if isinstance(issue, dict):
            return dict(issue)
        return {"message": str(issue)}

    def _child_task_from_step(self, parent_task: Task, step: dict[str, Any]) -> Task:
        task_code = str(step.get("call_code", ""))
        task_name = task_code
        try:
            if self.catalog is not None:
                spec = self.catalog.get(task_code)
                task_name = str(getattr(spec, "name", "") or task_code)
        except KeyError:
            pass
        return Task(
            task_id=f"{parent_task.task_id}:{step.get('step_id', task_code)}",
            task_type=task_code,
            priority_key=parent_task.priority_key,
            priority=parent_task.priority,
            location=parent_task.location,
            payload=dict(parent_task.payload),
            selection_meta=dict(parent_task.selection_meta),
            task_code=task_code,
            instance_id=f"{parent_task.instance_id}/{step.get('step_id', task_code)}:{task_code}",
            assigned_robot_id=parent_task.assigned_robot_id,
            args=dict(step.get("args", {}) if isinstance(step.get("args", {}), dict) else {}),
            task_spec_name=task_name,
            step_plan=[],
            humanoid={
                "parent_task_code": parent_task.task_code,
                "parent_instance_id": parent_task.instance_id,
                "task_path": str(step.get("path", "")),
                "depth": int(step.get("depth", 0) or 0),
            },
        )

    @staticmethod
    def _path_is_descendant(path: str, parent_path: str) -> bool:
        return bool(parent_path) and (path == parent_path or path.startswith(parent_path + "/"))

    @classmethod
    def _path_is_skipped(cls, path: str, skipped_prefixes: set[str]) -> bool:
        return any(cls._path_is_descendant(path, prefix) for prefix in skipped_prefixes)
