from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Any

from .base import JobPlan, StrategyState, default_agent_priority_multipliers, default_task_priority_weights
from .llm_common import OptionalLLMDecisionModule


class OpenClawOrchestratedDecisionModule(OptionalLLMDecisionModule):
    """OpenClaw 오케스트레이터가 전역 계획을 만들고, 작업자는 로컬로 실행하는 모듈."""

    def __init__(self, cfg: dict[str, Any], llm_cfg: dict[str, Any] | None = None) -> None:
        super().__init__(cfg=cfg, llm_cfg=llm_cfg)
        if self.provider != "openclaw" or self.openclaw_client is None:
            self._fail("OpenClawOrchestratedDecisionModule requires llm.provider=openclaw.")
        orch_cfg = self.llm_cfg.get("orchestration", {}) if isinstance(self.llm_cfg.get("orchestration", {}), dict) else {}
        self.orchestration_enabled = bool(orch_cfg.get("enabled", True))
        self.worker_queue_limit = max(1, int(orch_cfg.get("worker_queue_limit", 4)))
        self.max_parallel_groups = max(1, int(orch_cfg.get("max_parallel_groups", 3)))
        self.native_thinking = self._normalize_native_thinking_level(orch_cfg.get("thinking", "off"), default="off")
        self.incident_replan_enabled = False
        self.report_item_limit = max(1, int(orch_cfg.get("report_item_limit", 4)))
        self.parallel_worker_calls = bool(orch_cfg.get("parallel_worker_calls", False))
        self.openclaw_manager_agent_id = self._normalize_openclaw_agent_id(
            orch_cfg.get("manager_agent_id", self.openclaw_manager_agent_id),
            default="MANAGER",
        )
        self.manager_agent_id = self.openclaw_manager_agent_id
        self.communication_enabled = False
        self.comm_rounds = 0
        self.last_manager_review: dict[str, Any] = {}
        self.last_worker_reports: dict[str, Any] = {}
        self.current_job_plan = self._empty_current_job_plan()

    def _reset_run_state(self) -> None:
        super()._reset_run_state()
        self.last_manager_review = {}
        self.last_worker_reports = {}
        self.current_job_plan = self._empty_current_job_plan()

    def _empty_current_job_plan(self) -> JobPlan:
        return JobPlan(
            task_priority_weights=default_task_priority_weights(),
            quotas={},
            rationale="orchestrated-default",
            agent_priority_multipliers=default_agent_priority_multipliers(self.agent_ids),
            personal_queues={aid: [] for aid in self.agent_ids},
            mailbox={aid: [] for aid in self.agent_ids},
            parallel_groups=[],
            reason_trace=[],
            manager_summary="",
        )

    def _phase_runtime_agent_suffix(self) -> str:
        raw = str(self.openclaw_run_id or "run").strip().upper().replace(":", "").replace("-", "")
        return raw or "RUN"

    def _simulation_total_days(self) -> int:
        horizon_cfg = self.cfg.get("horizon", {}) if isinstance(self.cfg.get("horizon", {}), dict) else {}
        return max(1, int(horizon_cfg.get("num_days", 1) or 1))

    def _build_day_scoped_runtime_agent_id(self, phase: str, day: int | None = None) -> str:
        # OpenClaw local agent는 같은 agent id의 main session을 재사용하므로,
        # reflect/plan은 day별 agent id를 써서 세션 오염을 차단한다.
        suffix = self._phase_runtime_agent_suffix()
        phase_key = str(phase or "").strip().lower()
        if phase_key not in {"manager_bottleneck_detector", "manager_daily_planner"}:
            return self.manager_agent_id
        safe_day = max(1, int(day or 1))
        prefix = "MANAGER_BOTTLENECK_DETECTOR" if phase_key == "manager_bottleneck_detector" else "MANAGER_DAILY_PLANNER"
        return f"{prefix}_{suffix}_D{safe_day}"

    def _build_phase_runtime_agent_ids(self) -> dict[str, str]:
        # run마다, 그리고 day마다 reflect/plan 전용 agent를 새로 만들어
        # OpenClaw가 오래된 agent main session을 재사용하지 못하게 한다.
        ids: dict[str, str] = {}
        for day in range(1, self._simulation_total_days() + 1):
            ids[f"{self.manager_agent_id}:manager_bottleneck_detector:d{day}"] = self._build_day_scoped_runtime_agent_id(
                "manager_bottleneck_detector",
                day,
            )
            ids[f"{self.manager_agent_id}:manager_daily_planner:d{day}"] = self._build_day_scoped_runtime_agent_id(
                "manager_daily_planner",
                day,
            )
        return ids

    def _runtime_agent_workspace_aliases(self) -> dict[str, str]:
        # reflect와 plan은 서로 다른 workspace를 사용해 메모리 오염을 막는다.
        aliases: dict[str, str] = {}
        for day in range(1, self._simulation_total_days() + 1):
            aliases[self._build_day_scoped_runtime_agent_id("manager_bottleneck_detector", day)] = "MANAGER_BOTTLENECK_DETECTOR"
            aliases[self._build_day_scoped_runtime_agent_id("manager_daily_planner", day)] = "MANAGER_DAILY_PLANNER"
        for aid in self.agent_ids:
            upper = self._normalize_openclaw_agent_id(aid)
            aliases[upper] = upper
        return aliases

    def _openclaw_agent_for_call(self, call_name: str, context: dict[str, Any] | None = None) -> str:
        ctx = context or {}
        day = max(1, int(ctx.get("day", 1) or 1))
        if call_name == "manager_bottleneck_detector":
            return self.phase_runtime_agent_ids.get(
                f"{self.manager_agent_id}:manager_bottleneck_detector:d{day}",
                self._build_day_scoped_runtime_agent_id("manager_bottleneck_detector", day),
            )
        if call_name == "manager_daily_planner":
            return self.phase_runtime_agent_ids.get(
                f"{self.manager_agent_id}:manager_daily_planner:d{day}",
                self._build_day_scoped_runtime_agent_id("manager_daily_planner", day),
            )
        return self.manager_agent_id

    def _phase_workspace_path(self, runtime_agent_id: str) -> Path | None:
        return self._openclaw_workspace_path(runtime_agent_id)

    def _phase_runtime_agent_id(self, call_name: str, context: dict[str, Any] | None = None) -> str:
        return self._openclaw_agent_for_call(call_name, context)

    def _phase_workspace_for_call(self, call_name: str, context: dict[str, Any] | None = None) -> Path | None:
        return self._phase_workspace_path(self._phase_runtime_agent_id(call_name, context))

    def _assert_native_workspace_inputs_ready(self, runtime_agent_id: str, phase: str) -> None:
        # 실제 native turn이 읽을 workspace에 request/template가 비어 있지 않은지 사전 확인한다.
        workspace = self._phase_workspace_path(runtime_agent_id)
        if workspace is None:
            self._fail(f"OpenClaw workspace missing for phase={phase} runtime_agent_id={runtime_agent_id}.")

        request_path = workspace / 'facts' / 'current_request.json'
        template_path = workspace / 'facts' / 'current_response_template.json'
        user_path = workspace / 'USER.md'
        alias = self.openclaw_runtime_workspace_aliases.get(str(runtime_agent_id).strip().upper(), str(runtime_agent_id).strip().upper())

        def _read(path_obj: Path) -> str:
            try:
                return path_obj.read_text(encoding='utf-8', errors='replace').strip()
            except OSError:
                return ''

        request_text = _read(request_path)
        template_text = _read(template_path)

        problems: list[str] = []
        if not request_text or request_text == '{}':
            problems.append('current_request_empty')
        if not template_text or template_text == '{}':
            problems.append('current_response_template_empty')

        if problems:
            self._fail(
                'OpenClaw workspace input validation failed: '
                + ','.join(problems)
                + f' | phase={phase} | runtime_agent_id={runtime_agent_id} | workspace_alias={alias} | workspace={workspace}'
            )


    def prepare_run_context(self, output_root: Path | str) -> dict[str, Any]:
        # Create a fresh run-local OpenClaw runtime and warm the sessions before the day loop starts.
        self._reset_run_state()
        self.phase_runtime_agent_ids = self._build_phase_runtime_agent_ids()
        runtime_info = self.openclaw_client.prepare_run_runtime(
            output_root=Path(output_root),
            worker_agent_ids=list(self.openclaw_worker_agent_ids),
            manager_agent_id=self.manager_agent_id,
            workspace_template_root=self.openclaw_workspace_root,
            agent_workspace_aliases=self._runtime_agent_workspace_aliases(),
        )
        self.openclaw_runtime_root = Path(runtime_info["runtime_root"])
        self.openclaw_runtime_workspace_root = Path(runtime_info["workspace_root"])
        self.openclaw_runtime_workspace_aliases = {
            str(key).strip().upper(): str(value).strip().upper()
            for key, value in (runtime_info.get("workspace_aliases", {}) or {}).items()
            if str(key).strip() and str(value).strip()
        }
        self.openclaw_runtime_state_root = Path(runtime_info["state_root"])
        self.openclaw_runtime_facts_root = Path(runtime_info["facts_root"])
        self.openclaw_gateway_log_path = Path(runtime_info["gateway_log_path"])
        self._seed_openclaw_run_context()
        gateway_info: dict[str, Any] = self.openclaw_client.restart_gateway()
        prepare_transport = self._openclaw_transport_for_call("prepare_runtime")
        self._openclaw_chat_fallback_ready = False
        if prepare_transport != "native_local":
            self._fail("OpenClaw native_local-only mode guard: non-native transport requested during runtime prepare.")
        merged = dict(runtime_info)
        merged["gateway"] = gateway_info
        merged["run_id"] = self.openclaw_run_id
        merged["transport"] = self.openclaw_transport
        return merged
    def _warm_native_openclaw_agents(self) -> None:
        native_runtime_agents = sorted({*self.phase_runtime_agent_ids.values()})
        for agent_id in native_runtime_agents:
            try:
                self.openclaw_client.native_agent_turn(
                    system_prompt="You are warming up a fresh native local session.",
                    user_prompt='Return exactly {"status":"ready"}.',
                    agent_id=agent_id,
                    session_key=f"{self._openclaw_session_key(agent_id)}:warmup",
                    thinking="off",
                )
            except Exception as exc:
                self._fail(f"OpenClaw native warm-up failed for {agent_id}: {type(exc).__name__}: {exc}")
    def _call_llm_json(
        self,
        user_prompt: str,
        system_prompt: str,
        *,
        call_name: str,
        context: dict[str, Any] | None = None,
        required_keys: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        return super()._call_llm_json(
            user_prompt,
            system_prompt,
            call_name=call_name,
            context=context,
            required_keys=required_keys,
        )

    @staticmethod
    def _native_field_contract(required_fields: dict[str, str]) -> dict[str, Any]:
        return {
            "required_keys": list(required_fields.keys()),
            "field_types": dict(required_fields),
        }

    @staticmethod
    def _native_default_contract_value(type_hint: str) -> Any:
        hint = str(type_hint or "").strip().lower()
        if hint.startswith("list"):
            return []
        if hint.startswith("dict"):
            return {}
        if hint.startswith("bool"):
            return False
        if hint.startswith("float"):
            return 0.0
        if hint.startswith("int"):
            return 0
        return ""

    def _native_response_template(self, required_fields: dict[str, str]) -> dict[str, Any]:
        return {key: self._native_default_contract_value(type_hint) for key, type_hint in required_fields.items()}

    @staticmethod
    def _native_contract_example(required_fields: dict[str, str]) -> dict[str, Any]:
        example: dict[str, Any] = {}
        for key, type_hint in required_fields.items():
            hint = str(type_hint or "").strip().lower()
            if hint.startswith("list"):
                example[str(key)] = []
            elif hint.startswith("dict"):
                example[str(key)] = {}
            elif hint.startswith("bool"):
                example[str(key)] = False
            elif hint.startswith("float"):
                example[str(key)] = 0.0
            elif hint.startswith("int"):
                example[str(key)] = 0
            else:
                example[str(key)] = ""
        return example

    def _native_phase_directives(self, phase: str) -> list[str]:
        directives = {
            "manager_bottleneck_detector": [
                "Rank the bottlenecks that most limit accepted finished product completion.",
                "Prefer output-closure bottlenecks over merely visible machine-local issues when the two conflict.",
            ],
            "manager_daily_planner": [
                "Build an evidence-driven day plan that can follow or override detector hypotheses.",
            ],
        }
        return list(directives.get(str(phase or "").strip(), []))
    def _native_turn_prompts(
        self,
        *,
        agent_id: str,
        phase: str,
        role_summary: str,
        input_payload: dict[str, Any],
        required_fields: dict[str, str],
        instructions: list[str],
        history_tag: str,
    ) -> tuple[str, str, dict[str, str]]:
        workspace = self._openclaw_workspace_path(agent_id)
        response_template = self._native_response_template(required_fields)
        request_payload = {
            "phase": phase,
            "language": self.language,
            "role": role_summary,
            "input": self._prune_prompt_value(input_payload),
            "required_keys": list(required_fields.keys()),
            "instructions": [str(item).strip() for item in instructions if str(item).strip()],
            "response_rule": "Return exactly one JSON object matching current_response_template.json.",
            "language_rule": f"Natural-language values must be in {self._communication_language_name()}. JSON keys and IDs stay in English.",
        }
        if str(phase) == "manager_daily_planner":
            request_payload["language"] = "ENG"
            request_payload["language_rule"] = "Natural-language values must be in English. JSON keys and IDs stay in English."
        if str(phase) == "manager_bottleneck_detector":
            request_payload["bottleneck_contract"] = {
                "top_bottlenecks_entry": {
                    "name": "str",
                    "rank": "1..3",
                    "severity": "low|medium|high",
                    "evidence": [{"metric": "str", "value": "number|string|bool"}],
                    "why_it_limits_output": "str",
                },
                "candidate_actions_entry": {
                    "task_family": "allowed_task_priority_key",
                    "linked_bottleneck": "top_bottlenecks.name",
                    "why": "str",
                },
            }
            request_payload["examples"] = {
                "ranked_bottleneck_example": {
                    "summary": "Inspection is the main closure bottleneck right now.",
                    "top_bottlenecks": [
                        {
                            "name": "inspection_backlog",
                            "rank": 1,
                            "severity": "high",
                            "evidence": [
                                {"metric": "inspection_backlog", "value": 6},
                                {"metric": "completed_products_last_window", "value": 0},
                                {"metric": "active_inspection_agents", "value": 0},
                            ],
                            "why_it_limits_output": "Finished products are accumulating before acceptance, so closure is blocked.",
                        }
                    ],
                    "candidate_actions": [
                        {"task_family": "inspect_product", "linked_bottleneck": "inspection_backlog", "why": "Reduce the acceptance bottleneck."},
                        {"task_family": "unload_machine", "linked_bottleneck": "inspection_backlog", "why": "Keep new finished output from stacking behind the closure bottleneck."},
                    ],
                    "reason_trace": [
                        {
                            "decision": "rank inspection backlog as primary bottleneck",
                            "reason": "Accepted finished-product closure is being blocked downstream.",
                            "evidence": [
                                {"signal": "inspection_backlog", "value": 6, "source": "observation"},
                                {"signal": "completed_products_last_window", "value": 0, "source": "observation"},
                            ],
                            "affected_agents": ["A1", "A2", "A3"],
                            "task_families": ["inspect_product", "unload_machine"],
                        }
                    ],
                }
            }
        if str(phase) == "manager_daily_planner":
            request_payload["reason_trace_contract"] = {
                "entry": {
                    "decision": "maintain|adjust",
                    "reason": "str",
                    "evidence": [{"signal": "str", "value": "number|string|bool", "source": "execution_state|closure_signals|constraint_signals|candidate_orders|detector_hypothesis|guardrails"}],
                    "affected_agents": ["A1|A2|A3"],
                    "task_families": ["allowed_task_priority_key"],
                    "detector_relation": "follow|reject|deprioritize",
                }
            }
            request_payload["decision_contract"] = [
                "Do not echo an empty template.",
                "Plan from today's evidence, not from yesterday's inertia.",
                "Treat detector_hypothesis as a hypothesis, not as binding truth.",
                "If candidate_orders is non-empty, queue_add must include at least one candidate action.",
                "Prefer queue_add over generic weight changes when a concrete next action is already visible.",
                "Use maintain only when no materially stronger intervention is justified by today's evidence.",
                "Set detector_alignment to follow, partial_override, or override.",
            ]
            request_payload["examples"] = {
                "inspection_led_example": {
                    "plan_mode": "adjust",
                    "weight_updates": {"inspect_product": 1.35},
                    "queue_add": {"A1": [{"task_family": "inspect_product", "target_type": "station", "target_id": "inspection", "target_station": 1, "reason": "Pull the inspection backlog first."}]},
                    "reason_trace": [{"decision": "adjust", "reason": "Inspection backlog is the strongest closure limiter today.", "evidence": [{"signal": "inspection_backlog", "value": 6, "source": "closure_signals"}, {"signal": "completed_products_last_window", "value": 0, "source": "closure_signals"}], "affected_agents": ["A1"], "task_families": ["inspect_product"], "detector_relation": "follow"}],
                    "detector_alignment": "follow",
                },
                "unload_led_example": {
                    "plan_mode": "adjust",
                    "weight_updates": {"unload_machine": 1.2},
                    "queue_add": {"A2": [{"task_family": "unload_machine", "target_type": "station", "target_id": "station2", "target_station": 2, "reason": "Clear the station2 output buffer."}]},
                    "reason_trace": [{"decision": "adjust", "reason": "Output buffers are limiting closure more than supply right now.", "evidence": [{"signal": "station2_output_buffer", "value": 3, "source": "closure_signals"}, {"signal": "inspection_backlog", "value": 1, "source": "closure_signals"}], "affected_agents": ["A2"], "task_families": ["unload_machine"], "detector_relation": "deprioritize"}],
                    "detector_alignment": "partial_override",
                },
                "repair_led_example": {
                    "plan_mode": "adjust",
                    "weight_updates": {"repair_machine": 1.35},
                    "queue_add": {"A3": [{"task_family": "repair_machine", "target_type": "machine", "target_id": "S2M2", "target_station": 2, "reason": "Repair the broken station2 machine first."}]},
                    "reason_trace": [{"decision": "adjust", "reason": "Breakdown evidence is stronger than the closure alternatives today.", "evidence": [{"signal": "broken_machines", "value": 1, "source": "constraint_signals"}, {"signal": "station2_output_buffer", "value": 2, "source": "closure_signals"}], "affected_agents": ["A3"], "task_families": ["repair_machine"], "detector_relation": "follow"}],
                    "detector_alignment": "follow",
                },
                "stable_example": {
                    "plan_mode": "maintain",
                    "weight_updates": {},
                    "queue_add": {},
                    "reason_trace": [{"decision": "maintain", "reason": "Today's closure and constraint signals do not justify a stronger intervention than the active plan.", "evidence": [{"signal": "inspection_backlog", "value": 0, "source": "closure_signals"}, {"signal": "broken_machines", "value": 0, "source": "constraint_signals"}], "affected_agents": [], "task_families": [], "detector_relation": "follow"}],
                    "detector_alignment": "follow",
                }
            }
        if workspace is not None:
            self._openclaw_write_json(workspace / "facts" / "current_request.json", request_payload)
            self._openclaw_write_json(workspace / "facts" / "request_history" / f"{history_tag}.json", request_payload)
            self._openclaw_write_json(workspace / "facts" / "current_response_template.json", response_template)
            (workspace / "facts" / "current_phase.txt").write_text(str(phase), encoding="utf-8")
        system_prompt = "Native-local simulator turn. Use workspace facts only. Return one JSON object only."
        user_prompt = f"Execute {phase}. Fill current_response_template.json exactly."
        if str(phase) == "manager_daily_planner":
            user_prompt = (
                "Execute manager_daily_planner. Validate detector_hypothesis against today's evidence, then return an evidence-driven day plan. "
                "Return exactly one JSON object with plan_mode, weight_updates, queue_add, reason_trace, and detector_alignment."
            )
        return system_prompt, user_prompt, dict(required_fields)

    def _worker_local_observation_view(self, observation: dict[str, Any], agent_id: str) -> dict[str, Any]:
        base = self._planner_observation_view(observation)
        agents = observation.get("agents", {}) if isinstance(observation.get("agents", {}), dict) else {}
        by_id = agents.get("by_id", {}) if isinstance(agents.get("by_id", {}), dict) else {}
        agent_state = by_id.get(agent_id, {}) if isinstance(by_id.get(agent_id, {}), dict) else {}
        queues = observation.get("queues", {}) if isinstance(observation.get("queues", {}), dict) else {}
        machines = observation.get("machines", {}) if isinstance(observation.get("machines", {}), dict) else {}
        machine_by_id = machines.get("by_id", {}) if isinstance(machines.get("by_id", {}), dict) else {}
        location = str(agent_state.get("location", ""))
        nearby_station = int(location.removeprefix("Station")) if location.startswith("Station") and location.removeprefix("Station").isdigit() else None
        if nearby_station is not None:
            nearby_queues = {
                "material_input": (queues.get("material", {}) or {}).get(f"station{nearby_station}_input", 0),
                "intermediate_input": (queues.get("intermediate", {}) or {}).get(f"station{nearby_station}_input", 0),
                "output_buffer": (queues.get("output_buffers", {}) or {}).get(f"station{nearby_station}_output_buffer", 0),
            }
        elif location == "Inspection":
            nearby_queues = dict((queues.get("inspection", {}) or {}))
        else:
            nearby_queues = {}
        nearby_machines = {}
        if nearby_station is not None:
            for machine_id, raw in machine_by_id.items():
                data = raw if isinstance(raw, dict) else {}
                if int(data.get("station_index", 0) or 0) == nearby_station:
                    nearby_machines[str(machine_id)] = {
                        "state": data.get("state"),
                        "broken": bool(data.get("broken", False)),
                        "wait_reasons": data.get("wait_reasons", []),
                        "has_output_waiting_unload": bool(data.get("has_output_waiting_unload", False)),
                    }
        return self._prune_prompt_value(
            {
                "time": base.get("time", {}),
                "agent_id": agent_id,
                "self_state": {
                    "location": agent_state.get("location"),
                    "status": agent_state.get("status"),
                    "battery_remaining_min": agent_state.get("battery_remaining_min"),
                    "low_battery": bool(agent_state.get("low_battery", False)),
                    "discharged": bool(agent_state.get("discharged", False)),
                    "current_task_type": agent_state.get("current_task_type"),
                    "carrying_item_type": agent_state.get("carrying_item_type"),
                },
                "nearby_station": nearby_station,
                "nearby_queues": nearby_queues,
                "nearby_machines": nearby_machines,
                "global_signals": self._worker_local_signals(observation),
            }
        ) or {}

    def _worker_queue_summary(self, agent_id: str, plan: JobPlan | None = None) -> list[dict[str, Any]]:
        job_plan = plan or self.current_job_plan
        queue = job_plan.personal_queues.get(agent_id, []) if isinstance(job_plan.personal_queues, dict) else []
        out: list[dict[str, Any]] = []
        for item in queue[: self.worker_queue_limit]:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "order_id": str(item.get("order_id", "")),
                    "task_family": str(item.get("task_family", "")),
                    "priority": round(float(item.get("priority", 1.0) or 1.0), 3),
                    "target_type": str(item.get("target_type", "none")),
                    "target_id": str(item.get("target_id", "")),
                    "target_station": item.get("target_station"),
                    "handover_to": str(item.get("handover_to", "")),
                    "reason": self._truncate_prompt_text(item.get("reason", ""), max_len=120),
                }
            )
        return out

    def _worker_local_signals(self, observation: dict[str, Any]) -> dict[str, Any]:
        flow = observation.get("flow", {}) if isinstance(observation.get("flow", {}), dict) else {}
        queues = observation.get("queues", {}) if isinstance(observation.get("queues", {}), dict) else {}
        machines = observation.get("machines", {}) if isinstance(observation.get("machines", {}), dict) else {}
        agents = observation.get("agents", {}) if isinstance(observation.get("agents", {}), dict) else {}
        summary = machines.get("summary", {}) if isinstance(machines.get("summary", {}), dict) else {}
        summary_all = summary.get("all", {}) if isinstance(summary.get("all", {}), dict) else {}
        agent_summary = agents.get("summary", {}) if isinstance(agents.get("summary", {}), dict) else {}
        inspection = queues.get("inspection", {}) if isinstance(queues.get("inspection", {}), dict) else {}
        output_buffers = queues.get("output_buffers", {}) if isinstance(queues.get("output_buffers", {}), dict) else {}
        return self._prune_prompt_value(
            {
                "inspection_backlog": int(inspection.get("backlog", inspection.get("inspection_input", 0)) or 0),
                "station1_output_buffer": int(output_buffers.get("station1_output_buffer", queues.get("station1_output_buffer", 0)) or 0),
                "station2_output_buffer": int(output_buffers.get("station2_output_buffer", queues.get("station2_output_buffer", 0)) or 0),
                "products_completed": int(flow.get("finished_products", flow.get("products_completed_total", 0)) or 0),
                "broken_machines": int(summary.get("broken", summary_all.get("broken", 0)) or 0),
                "active_repairs": int(summary.get("under_repair", summary_all.get("under_repair", 0)) or 0),
                "low_battery_agents": int(agent_summary.get("low_battery", 0) or 0),
                "discharged_agents": int(agent_summary.get("discharged", 0) or 0),
            }
        ) or {}

    def _worker_experience_prompt_view(self, raw: dict[str, Any]) -> dict[str, Any]:
        src = raw if isinstance(raw, dict) else {}
        top_completed = src.get("top_completed_task_families", []) if isinstance(src.get("top_completed_task_families", []), list) else []
        recent_events = src.get("recent_task_events", []) if isinstance(src.get("recent_task_events", []), list) else []
        contribution = src.get("contribution_signals", {}) if isinstance(src.get("contribution_signals", {}), dict) else {}
        return self._prune_prompt_value(
            {
                "top_completed_task_families": [
                    {
                        "priority_key": str(item.get("priority_key", "")),
                        "completed_minutes": round(float(item.get("completed_minutes", 0.0) or 0.0), 1),
                        "completed_count": int(item.get("completed_count", 0) or 0),
                    }
                    for item in top_completed[:3]
                    if isinstance(item, dict)
                ],
                "contribution_signals": {str(key): int(value or 0) for key, value in contribution.items()},
                "recent_task_events": [
                    {
                        "priority_key": str(item.get("priority_key", "")),
                        "status": str(item.get("status", "")),
                        "duration": round(float(item.get("duration", 0.0) or 0.0), 1),
                    }
                    for item in recent_events[-3:]
                    if isinstance(item, dict)
                ],
            }
        ) or {}

    # Worker phases are independent, so this helper can fan them out in parallel when the
    # config allows it. Manager calls remain serialized to keep reasoning traces stable.
    def _parallel_worker_call_map(self, worker_items: list[tuple[str, str, str, str, dict[str, Any], dict[str, str]]]) -> dict[str, dict[str, Any]]:
        # Worker-local phases are independent, so they can be parallelized without changing world determinism.
        if not worker_items:
            return {}
        if not self.parallel_worker_calls or len(worker_items) <= 1:
            results: dict[str, dict[str, Any]] = {}
            for agent_id, prompt, system_prompt, call_name, context, required_keys in worker_items:
                results[agent_id] = self._call_llm_json(prompt, system_prompt, call_name=call_name, context=context, required_keys=required_keys)
            return results
        max_workers = max(1, min(len(worker_items), len(self.agent_ids), 4))
        results: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mansim-worker-llm") as executor:
            future_map = {
                executor.submit(self._call_llm_json, prompt, system_prompt, call_name=call_name, context=context, required_keys=required_keys): agent_id
                for agent_id, prompt, system_prompt, call_name, context, required_keys in worker_items
            }
            for future in as_completed(future_map):
                agent_id = future_map[future]
                results[agent_id] = future.result()
        return results
    def _normalize_task_family_alias(self, task_family: Any) -> str:
        raw = str(task_family or "").strip()
        aliases = {
            "deliver_material": "material_supply",
            "material_delivery": "material_supply",
            "transfer_material": "material_supply",
            "procure_material": "material_supply",
            "fetch_material": "material_supply",
            "supply_material": "material_supply",
            "deliver_intermediate_input": "inter_station_transfer",
            "transfer_intermediate_input": "inter_station_transfer",
            "move_intermediate_input": "inter_station_transfer",
            "inspect_items": "inspect_product",
            "inspect_products": "inspect_product",
        }
        normalized = aliases.get(raw, raw)
        lower = normalized.lower()
        if lower not in self.allowed_task_priority_keys:
            if "material" in lower and all(token not in lower for token in ("battery", "inspect", "repair")):
                normalized = "material_supply"
            elif "intermediate" in lower or ("transfer" in lower and "battery" not in lower):
                normalized = "inter_station_transfer"
            elif "inspect" in lower:
                normalized = "inspect_product"
        return normalized if normalized in self.allowed_task_priority_keys else raw

    def _sanitize_personal_queues(self, src: Any) -> dict[str, list[dict[str, Any]]]:
        out = {agent_id: [] for agent_id in self.agent_ids}
        if not isinstance(src, dict):
            return out
        valid_target_types = {"none", "station", "machine", "agent", "item", "location"}
        for agent_id in self.agent_ids:
            raw_list = src.get(agent_id, [])
            if not isinstance(raw_list, list):
                continue
            cleaned: list[dict[str, Any]] = []
            for idx, item in enumerate(raw_list[: self.worker_queue_limit], start=1):
                if not isinstance(item, dict):
                    continue
                task_family = self._normalize_task_family_alias(item.get("task_family", ""))
                if task_family not in self.allowed_task_priority_keys:
                    continue
                target_type = str(item.get("target_type", "none")).strip().lower() or "none"
                if target_type not in valid_target_types:
                    target_type = "none"
                try:
                    target_station = int(item.get("target_station")) if item.get("target_station") not in {None, ""} else None
                except (TypeError, ValueError):
                    target_station = None
                handover_to = self._normalize_openclaw_agent_id(item.get("handover_to", ""), default="")
                if handover_to not in self.agent_ids:
                    handover_to = ""
                try:
                    expires_at_day = int(item.get("expires_at_day")) if item.get("expires_at_day") not in {None, ""} else None
                except (TypeError, ValueError):
                    expires_at_day = None
                cleaned.append(
                    {
                        "order_id": str(item.get("order_id", f"WO-{agent_id}-{idx}"))[:64],
                        "task_family": task_family,
                        "priority": round(self._clamp_float(item.get("priority"), 0.5, self.task_priority_weight_max, 1.0), 3),
                        "target_type": target_type,
                        "target_id": str(item.get("target_id", ""))[:64],
                        "target_station": target_station,
                        "dependency_ids": self._as_str_list(item.get("dependency_ids"), [])[:4],
                        "parallel_group": str(item.get("parallel_group", ""))[:64],
                        "handover_to": handover_to,
                        "expires_at_day": expires_at_day,
                        "reason": self._truncate_prompt_text(item.get("reason", ""), max_len=180),
                    }
                )
            out[agent_id] = cleaned
        return out

    def _sanitize_mailbox(self, src: Any) -> dict[str, list[dict[str, Any]]]:
        out = {agent_id: [] for agent_id in self.agent_ids}
        if not isinstance(src, dict):
            return out
        valid_message_types = {"handover", "coordination", "watchout", "dependency", "assist_request"}
        valid_target_types = {"none", "station", "machine", "agent", "item", "location"}
        for agent_id in self.agent_ids:
            raw_list = src.get(agent_id, [])
            if not isinstance(raw_list, list):
                continue
            cleaned: list[dict[str, Any]] = []
            for idx, item in enumerate(raw_list[: self.worker_queue_limit], start=1):
                if not isinstance(item, dict):
                    continue
                to_agent = self._normalize_openclaw_agent_id(item.get("to_agent", agent_id), default=agent_id)
                if to_agent != agent_id:
                    to_agent = agent_id
                message_type = str(item.get("message_type", "coordination")).strip().lower() or "coordination"
                if message_type not in valid_message_types:
                    message_type = "coordination"
                task_family = self._normalize_task_family_alias(item.get("task_family", ""))
                if task_family and task_family not in self.allowed_task_priority_keys:
                    task_family = ""
                target_type = str(item.get("target_type", "none")).strip().lower() or "none"
                if target_type not in valid_target_types:
                    target_type = "none"
                try:
                    target_station = int(item.get("target_station")) if item.get("target_station") not in {None, ""} else None
                except (TypeError, ValueError):
                    target_station = None
                cleaned.append(
                    {
                        "message_id": str(item.get("message_id", f"MSG-{agent_id}-{idx}"))[:64],
                        "from_agent": self._normalize_openclaw_agent_id(item.get("from_agent", self.manager_agent_id), default=self.manager_agent_id),
                        "to_agent": to_agent,
                        "message_type": message_type,
                        "task_family": task_family,
                        "target_type": target_type,
                        "target_id": str(item.get("target_id", ""))[:64],
                        "target_station": target_station,
                        "priority": self._clamp_int(item.get("priority"), 1, 5, 1),
                        "body": self._truncate_prompt_text(item.get("body", ""), max_len=180),
                    }
                )
            out[agent_id] = cleaned
        return out

    def _sanitize_parallel_groups(self, src: Any) -> list[dict[str, Any]]:
        if not isinstance(src, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for idx, item in enumerate(src[: self.max_parallel_groups], start=1):
            if not isinstance(item, dict):
                continue
            cleaned.append(
                {
                    "group_id": str(item.get("group_id", f"PG-{idx}"))[:64],
                    "summary": self._truncate_prompt_text(item.get("summary", ""), max_len=180),
                    "agents": [aid for aid in self._as_str_list(item.get("agents"), []) if aid in self.agent_ids][:3],
                    "order_ids": self._as_str_list(item.get("order_ids"), [])[:8],
                }
            )
        return cleaned

    def _sanitize_reason_evidence(self, src: Any) -> list[dict[str, Any]]:
        if not isinstance(src, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for item in src[:6]:
            if isinstance(item, dict):
                signal = self._truncate_prompt_text(item.get("signal", item.get("metric", item.get("name", ""))), max_len=64)
                source = self._truncate_prompt_text(item.get("source", item.get("origin", "observation")), max_len=48) or "observation"
                value = item.get("value")
                if signal:
                    cleaned.append({"signal": signal, "value": value, "source": source})
                continue
            text = self._truncate_prompt_text(item, max_len=120)
            if text:
                cleaned.append({"signal": "text_note", "value": text, "source": "llm_text"})
        return cleaned

    def _sanitize_detector_alignment(self, value: Any) -> str:
        candidate = str(value or "").strip().lower()
        return candidate if candidate in {"follow", "partial_override", "override"} else "follow"

    def _sanitize_reason_trace(self, src: Any) -> list[dict[str, Any]]:
        if not isinstance(src, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for item in src[:8]:
            if not isinstance(item, dict):
                continue
            decision = self._truncate_prompt_text(item.get("decision", item.get("action", "")), max_len=48)
            relation = str(item.get("detector_relation", "follow")).strip().lower()
            if relation not in {"follow", "reject", "deprioritize"}:
                relation = "follow"
            cleaned.append(
                {
                    "decision": decision,
                    "reason": self._truncate_prompt_text(item.get("reason", ""), max_len=220),
                    "evidence": self._sanitize_reason_evidence(item.get("evidence")),
                    "affected_agents": [aid for aid in self._as_str_list(item.get("affected_agents"), []) if aid in self.agent_ids][:3],
                    "task_families": [self._normalize_task_family_alias(task) for task in self._as_str_list(item.get("task_families"), []) if self._normalize_task_family_alias(task) in self.allowed_task_priority_keys][:4],
                    "detector_relation": relation,
                }
            )
        return cleaned

    def _weight_focus_summary(self, weights: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
        ranked = []
        for key, value in (weights.items() if isinstance(weights, dict) else []):
            task_family = str(key).strip()
            if task_family not in self.allowed_task_priority_keys:
                continue
            ranked.append((task_family, self._clamp_float(value, 0.0, self.task_priority_weight_max, 0.0)))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return [{"task_family": task_family, "weight": round(weight, 3)} for task_family, weight in ranked[:limit] if weight > 0.0]

    def _plan_has_structured_dispatch(self, plan: JobPlan) -> bool:
        for agent_id in self.agent_ids:
            queue = plan.personal_queues.get(agent_id, []) if isinstance(plan.personal_queues.get(agent_id, []), list) else []
            mailbox = plan.mailbox.get(agent_id, []) if isinstance(plan.mailbox.get(agent_id, []), list) else []
            if queue or mailbox:
                return True
        return False

    def _dispatch_opportunity_exists(self, observation: dict[str, Any]) -> bool:
        signals = self._worker_local_signals(observation)
        if int(signals.get("broken_machines", 0) or 0) > 0:
            return True
        if int(signals.get("station1_output_buffer", 0) or 0) > 0:
            return True
        if int(signals.get("station2_output_buffer", 0) or 0) > 0:
            return True
        if int(signals.get("inspection_backlog", 0) or 0) > 0:
            return True
        machines = observation.get("machines", {}) if isinstance(observation.get("machines", {}), dict) else {}
        focus_by_id = machines.get("focus_by_id", {}) if isinstance(machines.get("focus_by_id", {}), dict) else {}
        for payload in focus_by_id.values():
            if not isinstance(payload, dict):
                continue
            owners = payload.get("owners", {}) if isinstance(payload.get("owners", {}), dict) else {}
            wait_reasons = payload.get("wait_reasons", []) if isinstance(payload.get("wait_reasons", []), list) else []
            if payload.get("broken") and str(owners.get("repair", "")).strip() in self.agent_ids:
                return True
            if "ready_for_setup" in wait_reasons and str(owners.get("setup", "")).strip() in self.agent_ids:
                return True
        return False

    def _fallback_dispatch_payload(self, observation: dict[str, Any]) -> dict[str, Any]:
        queues: dict[str, list[dict[str, Any]]] = {aid: [] for aid in self.agent_ids}
        mailbox: dict[str, list[dict[str, Any]]] = {aid: [] for aid in self.agent_ids}
        machines = observation.get("machines", {}) if isinstance(observation.get("machines", {}), dict) else {}
        focus_by_id = machines.get("focus_by_id", {}) if isinstance(machines.get("focus_by_id", {}), dict) else {}
        queue_index = {aid: 1 for aid in self.agent_ids}
        mailbox_index = {aid: 1 for aid in self.agent_ids}
        station_orders: dict[int, list[tuple[str, str, str]]] = {}

        def _station_num(raw: Any) -> int | None:
            text = str(raw or "").strip()
            if text.lower().startswith("station"):
                suffix = text[7:]
                return int(suffix) if suffix.isdigit() else None
            try:
                return int(text)
            except (TypeError, ValueError):
                return None

        def add_queue(agent_id: str, task_family: str, *, target_type: str, target_id: str, target_station: int | None, reason: str) -> None:
            if agent_id not in self.agent_ids:
                return
            order_id = f"WO-{agent_id}-{queue_index[agent_id]}"
            queue_index[agent_id] += 1
            queues[agent_id].append(
                {
                    "order_id": order_id,
                    "task_family": task_family,
                    "priority": 1.35,
                    "target_type": target_type,
                    "target_id": target_id,
                    "target_station": target_station,
                    "dependency_ids": [],
                    "parallel_group": "",
                    "handover_to": "",
                    "expires_at_day": None,
                    "reason": reason,
                }
            )
            if target_station is not None:
                station_orders.setdefault(target_station, []).append((agent_id, task_family, order_id))

        for machine_id, payload in focus_by_id.items():
            if not isinstance(payload, dict):
                continue
            owners = payload.get("owners", {}) if isinstance(payload.get("owners", {}), dict) else {}
            wait_reasons = payload.get("wait_reasons", []) if isinstance(payload.get("wait_reasons", []), list) else []
            station = _station_num(payload.get("station"))
            repair_owner = str(owners.get("repair", "")).strip().upper()
            setup_owner = str(owners.get("setup", "")).strip().upper()
            if bool(payload.get("broken")) and repair_owner in self.agent_ids:
                add_queue(repair_owner, "repair_machine", target_type="machine", target_id=str(machine_id), target_station=station, reason=f"Repair {machine_id} because it is broken.")
            if "ready_for_setup" in wait_reasons and setup_owner in self.agent_ids:
                add_queue(setup_owner, "setup_machine", target_type="machine", target_id=str(machine_id), target_station=station, reason=f"Setup {machine_id} because it is ready for setup.")

        signals = self._worker_local_signals(observation)
        inspection_backlog = int(signals.get("inspection_backlog", 0) or 0)
        if inspection_backlog > 0:
            add_queue(self.agent_ids[0], "inspect_product", target_type="station", target_id="inspection", target_station=self.inspection_queue_station if hasattr(self, 'inspection_queue_station') else None, reason=f"Pull inspection backlog={inspection_backlog}.")
        for station_idx, key in ((1, "station1_output_buffer"), (2, "station2_output_buffer")):
            count = int(signals.get(key, 0) or 0)
            if count > 0:
                add_queue(self.agent_ids[min(1, len(self.agent_ids)-1)], "unload_machine", target_type="station", target_id=f"station{station_idx}", target_station=station_idx, reason=f"Unload station{station_idx} because output_buffer={count}.")

        for station, items in station_orders.items():
            task_map = {task_family: (agent_id, order_id) for agent_id, task_family, order_id in items}
            if "repair_machine" in task_map and "setup_machine" in task_map:
                setup_agent, _ = task_map["setup_machine"]
                repair_agent, _ = task_map["repair_machine"]
                msg_id = f"MSG-{setup_agent}-{mailbox_index[setup_agent]}"
                mailbox_index[setup_agent] += 1
                mailbox[setup_agent].append(
                    {
                        "message_id": msg_id,
                        "from_agent": self.manager_agent_id,
                        "to_agent": setup_agent,
                        "message_type": "dependency",
                        "task_family": "setup_machine",
                        "target_type": "station",
                        "target_id": f"station{station}",
                        "target_station": station,
                        "priority": 2,
                        "body": f"Wait for {repair_agent} to finish repair on station{station}, then start setup.",
                    }
                )

        return {"personal_queues": queues, "mailbox": mailbox}


    def _detector_packet(self, observation: dict[str, Any]) -> dict[str, Any]:
        planner_view = self._planner_observation_view(observation)
        time_view = planner_view.get("time", {}) if isinstance(planner_view.get("time", {}), dict) else {}
        queues = planner_view.get("queues", {}) if isinstance(planner_view.get("queues", {}), dict) else {}
        machines = planner_view.get("machines", {}) if isinstance(planner_view.get("machines", {}), dict) else {}
        machine_summary = machines.get("summary", {}) if isinstance(machines.get("summary", {}), dict) else {}
        wait_summary = machines.get("wait_reason_summary", {}) if isinstance(machines.get("wait_reason_summary", {}), dict) else {}
        focus_by_id = machines.get("focus_by_id", {}) if isinstance(machines.get("focus_by_id", {}), dict) else {}
        agents = planner_view.get("agents", {}) if isinstance(planner_view.get("agents", {}), dict) else {}
        agent_summary = agents.get("summary", {}) if isinstance(agents.get("summary", {}), dict) else {}
        agent_focus = agents.get("focus_by_id", {}) if isinstance(agents.get("focus_by_id", {}), dict) else {}
        flow = planner_view.get("flow", {}) if isinstance(planner_view.get("flow", {}), dict) else {}
        recent = planner_view.get("recent_history", {}) if isinstance(planner_view.get("recent_history", {}), dict) else {}
        trends = planner_view.get("trends", {}) if isinstance(planner_view.get("trends", {}), dict) else {}
        signals = self._worker_local_signals(observation)

        return {
            "objective": {
                "goal": "Maximize accepted finished products within the remaining simulation horizon.",
                "day": time_view.get("day"),
                "days_remaining": time_view.get("days_remaining"),
                "horizon_remaining_min": time_view.get("horizon_remaining_min"),
            },
            "throughput_closure_state": {
                "inspection_backlog": int(signals.get("inspection_backlog", 0) or 0),
                "station1_output_buffer": int(signals.get("station1_output_buffer", 0) or 0),
                "station2_output_buffer": int(signals.get("station2_output_buffer", 0) or 0),
                "completed_products_total": int(signals.get("products_completed", 0) or 0),
                "completed_products_last_window": int(trends.get("completed_products_last_window", 0) or 0),
                "inspection_passes_last_window": int((trends.get("stage_completions_last_window", {}) or {}).get("inspection_pass", 0) or 0),
                "active_inspection_agents": int(flow.get("active_inspection_agents", 0) or 0),
                "inspection_input_queue": int(((queues.get("inspection", {}) or {}).get("inspection_input", 0)) or 0),
            },
            "constraint_state": {
                "machine_constraints": {
                    "wait_input_total": int((machine_summary.get("all", {}) or {}).get("wait_input", 0) or 0),
                    "finished_wait_unload_total": int((machine_summary.get("all", {}) or {}).get("finished_wait_unload", 0) or 0),
                    "missing_material": int((wait_summary.get("all", {}) or {}).get("missing_material", 0) or 0),
                    "missing_intermediate_input": int((wait_summary.get("all", {}) or {}).get("missing_intermediate_input", 0) or 0),
                    "waiting_unload": int((wait_summary.get("all", {}) or {}).get("waiting_unload", 0) or 0),
                    "ready_for_setup": int((wait_summary.get("all", {}) or {}).get("ready_for_setup", 0) or 0),
                    "broken": int((wait_summary.get("all", {}) or {}).get("broken", 0) or 0),
                },
                "worker_constraints": {
                    "low_battery_agents": int(signals.get("low_battery_agents", 0) or 0),
                    "discharged_agents": int(signals.get("discharged_agents", 0) or 0),
                    "idle_agents": int(agent_summary.get("idle", 0) or 0),
                },
            },
            "supporting_detail": {
                "material_queues": queues.get("material", {}),
                "intermediate_queues": queues.get("intermediate", {}),
                "machines_waiting_unload": flow.get("machines_waiting_unload", {}),
                "broken_machine_count": int(flow.get("broken_machine_count", 0) or 0),
                "last_day_products": int(recent.get("last_day_products", 0) or 0),
                "queue_delta": trends.get("queue_delta", {}),
                "machine_focus": list({k: v for k, v in focus_by_id.items() if isinstance(v, dict)}.values())[:4],
                "agent_focus": list({k: v for k, v in agent_focus.items() if isinstance(v, dict) and (v.get("low_battery") or v.get("awaiting_battery_from") or str(v.get("current_task_type", "")).strip())}.values())[:3],
            },
        }

    def _planning_packet(self, observation: dict[str, Any], strategy: StrategyState, norms: dict[str, Any]) -> dict[str, Any]:
        planner_view = self._planner_observation_view(observation)
        time_view = planner_view.get("time", {}) if isinstance(planner_view.get("time", {}), dict) else {}
        queues = planner_view.get("queues", {}) if isinstance(planner_view.get("queues", {}), dict) else {}
        flow = planner_view.get("flow", {}) if isinstance(planner_view.get("flow", {}), dict) else {}
        machines = planner_view.get("machines", {}) if isinstance(planner_view.get("machines", {}), dict) else {}
        wait_summary = machines.get("wait_reason_summary", {}) if isinstance(machines.get("wait_reason_summary", {}), dict) else {}
        trends = planner_view.get("trends", {}) if isinstance(planner_view.get("trends", {}), dict) else {}
        signals = self._worker_local_signals(observation)
        dispatch = self._fallback_dispatch_payload(observation)

        candidate_orders: list[dict[str, Any]] = []
        for agent_id in self.agent_ids:
            for item in (dispatch.get("personal_queues", {}) or {}).get(agent_id, [])[:2]:
                if not isinstance(item, dict):
                    continue
                candidate_orders.append(
                    {
                        "agent_id": agent_id,
                        "task_family": str(item.get("task_family", "")).strip(),
                        "target_type": str(item.get("target_type", "none")).strip(),
                        "target_id": str(item.get("target_id", "")).strip(),
                        "target_station": item.get("target_station"),
                        "reason": self._truncate_prompt_text(item.get("reason", ""), max_len=120),
                    }
                )

        detector_diagnosis = self._strategy_prompt_payload(strategy)
        detector_top_bottlenecks = detector_diagnosis.get("top_bottlenecks", []) if isinstance(detector_diagnosis.get("top_bottlenecks", []), list) else []
        detector_candidate_actions = detector_diagnosis.get("candidate_actions", []) if isinstance(detector_diagnosis.get("candidate_actions", []), list) else []

        return {
            "objective": {
                "goal": "Maximize accepted finished-product completion over the remaining horizon.",
                "day": int(time_view.get("day", observation.get("day", 0)) or 0),
                "days_remaining": int(time_view.get("days_remaining", 0) or 0),
            },
            "execution_state": {
                "days_remaining": int(time_view.get("days_remaining", 0) or 0),
                "current_weights": dict(self.current_job_plan.task_priority_weights),
                "current_personal_queues": {aid: list(self.current_job_plan.personal_queues.get(aid, []))[:2] for aid in self.agent_ids},
                "current_agent_multipliers": {aid: dict(self.current_job_plan.agent_priority_multipliers.get(aid, {})) for aid in self.agent_ids},
            },
            "closure_signals": {
                "inspection_backlog": int(signals.get("inspection_backlog", 0) or 0),
                "station1_output_buffer": int(signals.get("station1_output_buffer", 0) or 0),
                "station2_output_buffer": int(signals.get("station2_output_buffer", 0) or 0),
                "completed_products_last_window": int(trends.get("completed_products_last_window", 0) or 0),
                "inspection_passes_last_window": int((trends.get("stage_completions_last_window", {}) or {}).get("inspection_pass", 0) or 0),
                "active_inspection_agents": int(flow.get("active_inspection_agents", 0) or 0),
            },
            "constraint_signals": {
                "missing_material": int((wait_summary.get("all", {}) or {}).get("missing_material", 0) or 0),
                "missing_intermediate_input": int((wait_summary.get("all", {}) or {}).get("missing_intermediate_input", 0) or 0),
                "waiting_unload": int((wait_summary.get("all", {}) or {}).get("waiting_unload", 0) or 0),
                "broken_machines": int(signals.get("broken_machines", 0) or 0),
                "low_battery_agents": int(signals.get("low_battery_agents", 0) or 0),
            },
            "candidate_orders": candidate_orders[:6],
            "detector_hypothesis": {
                "top_bottlenecks": detector_top_bottlenecks[:4],
                "candidate_actions": detector_candidate_actions[:4],
            },
            "guardrails": {
                **self._llm_guardrails_payload("plan"),
                "allowed_target_stations": [1, 2],
                "norm_targets": norms if isinstance(norms, dict) else {},
            },
        }

    def _plan_has_actionable_change(self, plan: JobPlan, fallback: JobPlan) -> bool:
        for key in self.allowed_task_priority_keys:
            if abs(float(plan.task_priority_weights.get(key, 1.0)) - float(fallback.task_priority_weights.get(key, 1.0))) > 1e-6:
                return True
        for agent_id in self.agent_ids:
            plan_row = plan.agent_priority_multipliers.get(agent_id, {}) if isinstance(plan.agent_priority_multipliers.get(agent_id, {}), dict) else {}
            fallback_row = fallback.agent_priority_multipliers.get(agent_id, {}) if isinstance(fallback.agent_priority_multipliers.get(agent_id, {}), dict) else {}
            for key in self.allowed_task_priority_keys:
                if abs(float(plan_row.get(key, 1.0)) - float(fallback_row.get(key, 1.0))) > 1e-6:
                    return True
            plan_queue = plan.personal_queues.get(agent_id, []) if isinstance(plan.personal_queues.get(agent_id, []), list) else []
            fallback_queue = fallback.personal_queues.get(agent_id, []) if isinstance(fallback.personal_queues.get(agent_id, []), list) else []
            if plan_queue != fallback_queue:
                return True
            plan_mailbox = plan.mailbox.get(agent_id, []) if isinstance(plan.mailbox.get(agent_id, []), list) else []
            fallback_mailbox = fallback.mailbox.get(agent_id, []) if isinstance(fallback.mailbox.get(agent_id, []), list) else []
            if plan_mailbox != fallback_mailbox:
                return True
        return False

    def _synthesize_actionable_plan_payload(self, observation: dict[str, Any], strategy: StrategyState, fallback: JobPlan) -> dict[str, Any]:
        signals = self._worker_local_signals(observation)
        weights = dict(fallback.task_priority_weights)
        reason_trace: list[dict[str, Any]] = []

        def promote(task_family: str, delta: float, reason: str, evidence: list[str]) -> None:
            if task_family not in self.allowed_task_priority_keys:
                return
            weights[task_family] = round(min(self.task_priority_weight_max, max(self.task_priority_weight_min, float(weights.get(task_family, 1.0)) + float(delta))), 3)
            reason_trace.append(
                {
                    "reason": reason,
                    "evidence": evidence[:5],
                    "affected_agents": list(self.agent_ids[:3]),
                    "task_families": [task_family],
                }
            )

        inspection_backlog = int(signals.get("inspection_backlog", 0) or 0)
        station1_output = int(signals.get("station1_output_buffer", 0) or 0)
        station2_output = int(signals.get("station2_output_buffer", 0) or 0)
        broken_machines = int(signals.get("broken_machines", 0) or 0)
        low_battery_agents = int(signals.get("low_battery_agents", 0) or 0)
        discharged_agents = int(signals.get("discharged_agents", 0) or 0)
        products_completed = int(signals.get("products_completed", 0) or 0)

        if inspection_backlog > 0:
            promote("inspect_product", 0.45, "Inspection backlog exists, so finished-product inspection must be pulled forward.", [f"inspection_backlog={inspection_backlog}"])
        if station1_output > 0 or station2_output > 0:
            promote("unload_machine", 0.35, "Output buffer is accumulating, so unload should be prioritized to unblock flow.", [f"station1_output_buffer={station1_output}", f"station2_output_buffer={station2_output}"])
        if broken_machines > 0:
            promote("repair_machine", 0.55, "Broken machines are directly reducing plant capacity.", [f"broken_machines={broken_machines}"])
        if discharged_agents > 0:
            promote("battery_delivery_discharged", 0.5, "A discharged worker blocks execution and needs immediate battery delivery.", [f"discharged_agents={discharged_agents}"])
        if low_battery_agents > 0:
            promote("battery_delivery_low_battery", 0.35, "Low-battery workers should be supported before discharge.", [f"low_battery_agents={low_battery_agents}"])
        if products_completed <= 0 and not reason_trace:
            promote("material_supply", 0.25, "No products have been completed yet, so feed and startup flow must be accelerated.", [f"products_completed={products_completed}"])
            promote("setup_machine", 0.2, "No completed products yet implies startup/setup friction remains.", [f"products_completed={products_completed}"])

        if not reason_trace:
            weights["material_supply"] = round(min(self.task_priority_weight_max, max(self.task_priority_weight_min, float(weights.get("material_supply", 1.0)) + 0.15)), 3)
            reason_trace.append(
                {
                    "reason": "Fallback plan: keep material moving when no sharper bottleneck is detected.",
                    "evidence": ["no_explicit_bottleneck_detected"],
                    "affected_agents": list(self.agent_ids[:3]),
                    "task_families": ["material_supply"],
                }
            )

        dispatch = self._fallback_dispatch_payload(observation)
        summary = "Actionable fallback plan synthesized from plant bottlenecks because MANAGER returned an inert plan."
        rationale = "Use observed backlog, machine downtime, battery risk, and machine ownership cues to force operationally meaningful priority and dispatch changes."
        return {
            "task_priority_weights": weights,
            "personal_queues": dispatch.get("personal_queues", {}),
            "mailbox": dispatch.get("mailbox", {}),
            "reason_trace": reason_trace,
            "manager_summary": summary,
            "rationale": rationale,
        }

    def _is_explicit_stable_plan(self, llm_obj: dict[str, Any]) -> bool:
        if not isinstance(llm_obj, dict):
            return False
        if bool(llm_obj.get("maintain_current_plan", False)):
            return True
        stability_reason = str(llm_obj.get("stability_reason", "")).strip()
        rationale = str(llm_obj.get("rationale", "")).strip()
        summary = str(llm_obj.get("manager_summary", "")).strip()
        return bool(stability_reason and (rationale or summary))

    def _plan_has_explicit_reasoning(self, llm_obj: dict[str, Any]) -> bool:
        if not isinstance(llm_obj, dict):
            return False
        if isinstance(llm_obj.get("reason_trace"), list) and any(isinstance(item, dict) and str(item.get("reason", "")).strip() for item in llm_obj.get("reason_trace", [])):
            return True
        rationale = str(llm_obj.get("rationale", "")).strip()
        summary = str(llm_obj.get("manager_summary", "")).strip()
        stability_reason = str(llm_obj.get("stability_reason", "")).strip()
        return bool(rationale or summary or stability_reason)

    def _synthesize_plan_reasoning(self, candidate: dict[str, Any], observation: dict[str, Any], strategy: StrategyState, fallback: JobPlan) -> dict[str, Any]:
        merged = dict(candidate)
        plan = self._build_orchestrated_job_plan(merged, fallback, strategy)
        signals = self._worker_local_signals(observation)
        changed_families: list[str] = []
        for key in self.allowed_task_priority_keys:
            if abs(float(plan.task_priority_weights.get(key, 1.0)) - float(fallback.task_priority_weights.get(key, 1.0))) > 1e-6:
                changed_families.append(key)
        if not changed_families and isinstance(plan.reason_trace, list):
            for entry in plan.reason_trace:
                if not isinstance(entry, dict):
                    continue
                for family in self._as_str_list(entry.get("task_families"), []):
                    if family in self.allowed_task_priority_keys and family not in changed_families:
                        changed_families.append(family)
                if changed_families:
                    break
        evidence: list[str] = []
        for field in ("inspection_backlog", "station1_output_buffer", "station2_output_buffer", "broken_machines", "low_battery_agents", "discharged_agents", "products_completed"):
            value = signals.get(field, None)
            if value not in {None, 0, 0.0, ""}:
                evidence.append(f"{field}={value}")
        if not evidence:
            evidence.append("plant_state_stable")
        if not merged.get("manager_summary"):
            if changed_families:
                merged["manager_summary"] = "Adjusted plan based on observed plant bottlenecks: " + ", ".join(changed_families[:3])
            else:
                merged["manager_summary"] = str(strategy.summary or "Kept the current plan based on observed plant state.")
        if not merged.get("rationale"):
            if changed_families:
                merged["rationale"] = "Plan changes were made in response to observed bottlenecks and throughput risks."
            else:
                merged["rationale"] = "Current plan is maintained because no stronger bottleneck-specific change was justified by the observed state."
        merged.setdefault("detector_alignment", "follow")
        if not isinstance(merged.get("reason_trace"), list) or not merged.get("reason_trace"):
            merged["reason_trace"] = [
                {
                    "decision": "adjust" if changed_families else "maintain",
                    "reason": str(merged.get("rationale", "decision_reasoning_missing")).strip() or "decision_reasoning_missing",
                    "evidence": evidence[:5],
                    "affected_agents": list(self.agent_ids[:3]),
                    "task_families": changed_families[:4],
                    "detector_relation": "follow",
                }
            ]
        return merged

    def _ensure_actionable_manager_plan(self, llm_obj: dict[str, Any], observation: dict[str, Any], strategy: StrategyState, fallback: JobPlan) -> tuple[dict[str, Any], bool]:
        candidate = dict(llm_obj) if isinstance(llm_obj, dict) else {}
        plan = self._build_orchestrated_job_plan(candidate, fallback, strategy)
        inert = not self._plan_has_actionable_change(plan, fallback) and not self._is_explicit_stable_plan(candidate)
        return candidate, inert

    def _build_orchestrated_job_plan(self, llm_obj: dict[str, Any], fallback: JobPlan, strategy: StrategyState) -> JobPlan:
        weight_src = llm_obj.get("weight_updates", llm_obj.get("task_priority_weights"))
        multiplier_src = llm_obj.get("agent_multiplier_updates", {})
        queue_src = llm_obj.get("queue_add", llm_obj.get("personal_queues"))
        mailbox_src = llm_obj.get("mailbox_add", {})
        plan = JobPlan(
            task_priority_weights=self._sanitize_task_priority_weights(weight_src, fallback.task_priority_weights),
            quotas=self._sanitize_quotas(llm_obj.get("quotas"), fallback.quotas),
            rationale=str(llm_obj.get("rationale", fallback.rationale or "")).strip(),
            agent_priority_multipliers=self._clone_agent_priority_multipliers(),
        )
        plan.agent_priority_multipliers = self._apply_agent_priority_target_updates(
            fallback.agent_priority_multipliers if isinstance(fallback.agent_priority_multipliers, dict) else self.agent_priority_multipliers,
            self._sanitize_agent_priority_profile_updates(multiplier_src),
            blend=self.agent_priority_llm_blend,
        )
        plan.personal_queues = self._sanitize_personal_queues(queue_src)
        plan.mailbox = self._sanitize_mailbox(mailbox_src)
        plan.parallel_groups = self._sanitize_parallel_groups(llm_obj.get("parallel_groups"))
        plan.reason_trace = self._sanitize_reason_trace(llm_obj.get("reason_trace"))
        plan.detector_alignment = self._sanitize_detector_alignment(llm_obj.get("detector_alignment"))
        plan.manager_summary = self._truncate_prompt_text(llm_obj.get("manager_summary", llm_obj.get("rationale", strategy.summary or "")), max_len=300)
        plan.ensure_agent_priority_multipliers(self.agent_ids)
        plan.ensure_personal_queues(self.agent_ids)
        plan.ensure_mailbox(self.agent_ids)
        return plan

    def _sync_orchestration_reflection_workspace(self, *, observation: dict[str, Any], strategy: StrategyState) -> None:
        if not self._openclaw_enabled():
            return
        day = int((observation.get("time", {}) or {}).get("day", observation.get("day", 0)) or 0)
        orchestration_ctx = strategy.orchestration_context if isinstance(strategy.orchestration_context, dict) else {}
        reflect_payload = {
            "day": day,
            "summary": strategy.summary,
            "diagnosis": dict(strategy.diagnosis),
            "reason_trace": list(orchestration_ctx.get("reason_trace", [])),
            "top_bottlenecks": list(strategy.diagnosis.get("top_bottlenecks", [])),
            "watchouts": list(strategy.diagnosis.get("watchouts", [])),
            "candidate_actions": list(strategy.diagnosis.get("candidate_actions", [])),
            "priority_candidates": self._weight_focus_summary(self.current_job_plan.task_priority_weights),
        }
        reflect_memory = {
            "day": day,
            "top_bottlenecks": list(strategy.diagnosis.get("top_bottlenecks", [])),
            "watchouts": list(strategy.diagnosis.get("watchouts", [])),
            "candidate_actions": list(strategy.diagnosis.get("candidate_actions", [])),
            "priority_candidates": self._weight_focus_summary(self.current_job_plan.task_priority_weights),
            "reason_trace": list(orchestration_ctx.get("reason_trace", [])),
        }
        manager_workspace = self._phase_workspace_for_call("manager_bottleneck_detector", {"phase": "manager_bottleneck_detector", "day": day})
        if manager_workspace is None:
            return
        observation_view = self._planner_observation_view(observation)
        self._openclaw_write_json(manager_workspace / "facts" / "current_reflect.json", reflect_payload)
        self._openclaw_write_json(manager_workspace / "facts" / "reflect_history" / f"day_{day:02d}.json", reflect_payload)
        self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_reflect.json", reflect_payload)
        self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", reflect_memory)
        self._openclaw_write_json(manager_workspace / "beliefs" / "history" / f"day_{day:02d}_reflect.json", reflect_memory)
        self._openclaw_write_json(manager_workspace / "memory" / "episodic" / f"day_{day:02d}_reflect.json", {"reflection": reflect_payload, "observation": observation_view})
        self._openclaw_write_markdown(manager_workspace / "memory" / "daily" / f"day_{day:02d}_reflect.md", f"{self.manager_agent_id} Day {day} ?? ?? ??", [("?? ??", observation_view), ("?? ??", reflect_payload), ("??? ???", reflect_memory)])
        self._openclaw_write_markdown(manager_workspace / "memory" / "rolling_summary.md", f"{self.manager_agent_id} ?? ?? ?? ??", [("? ??", "? ?? ?? ??????? ?? ????? run ???? ?? ???? ????."), ("?? ?? ??", reflect_payload), ("??? ???", reflect_memory)])
        self._openclaw_write_markdown(manager_workspace / "MEMORY.md", f"{self.manager_agent_id} ?? ?? ???", [("? ??", "? ?? ?? ??????? ?? run ???? ?? ???? ????."), ("?? ?? ??", reflect_payload), ("??? ???", reflect_memory)])

    def _sync_orchestration_plan_workspace(self, observation: dict[str, Any], strategy: StrategyState, job_plan: JobPlan) -> None:
        if not self._openclaw_enabled():
            return
        day = int((observation.get("time", {}) or {}).get("day", observation.get("day", 0)) or 0)
        plan_payload = {
            "day": day,
            "summary": job_plan.manager_summary,
            "task_priority_weights": dict(job_plan.task_priority_weights),
            "agent_priority_multipliers": {aid: dict(job_plan.agent_priority_multipliers.get(aid, {})) for aid in self.agent_ids},
            "personal_queues": dict(job_plan.personal_queues),
            "mailbox": dict(job_plan.mailbox),
            "parallel_groups": list(job_plan.parallel_groups),
            "reason_trace": list(job_plan.reason_trace),
        }
        for aid in self.agent_ids:
            workspace = self._openclaw_workspace_path(aid)
            if workspace is None:
                continue
            self._openclaw_write_json(workspace / "facts" / "current_personal_queue.json", job_plan.personal_queues.get(aid, []))
            self._openclaw_write_json(workspace / "facts" / "current_mailbox.json", job_plan.mailbox.get(aid, []))
            self._openclaw_write_json(workspace / "plans" / f"day_{day:02d}_queue.json", job_plan.personal_queues.get(aid, []))
            self._openclaw_write_json(workspace / "mailboxes" / f"day_{day:02d}.json", job_plan.mailbox.get(aid, []))
        manager_workspace = self._phase_workspace_for_call("manager_daily_planner", {"phase": "manager_daily_planner", "day": day})
        if manager_workspace is not None:
            plan_memory = {
                "day": day,
                "summary": job_plan.manager_summary,
                "task_priority_weights": dict(job_plan.task_priority_weights),
                "agent_priority_multipliers": {aid: dict(job_plan.agent_priority_multipliers.get(aid, {})) for aid in self.agent_ids},
                "personal_queues": dict(job_plan.personal_queues),
                "mailbox": dict(job_plan.mailbox),
                "reason_trace": list(job_plan.reason_trace),
                "detector_alignment": str(getattr(job_plan, "detector_alignment", "follow")),
            }
            self._openclaw_write_json(manager_workspace / "facts" / "current_plan.json", plan_payload)
            self._openclaw_write_json(manager_workspace / "facts" / "plan_history" / f"day_{day:02d}.json", plan_payload)
            self._openclaw_write_json(manager_workspace / "plans" / f"day_{day:02d}_plan.json", plan_payload)
            self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_plan.json", plan_payload)
            self._openclaw_write_json(manager_workspace / "trace" / f"day_{day:02d}_reason_trace.json", job_plan.reason_trace)
            self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", plan_memory)
            self._openclaw_write_json(manager_workspace / "beliefs" / "history" / f"day_{day:02d}_plan.json", plan_memory)
            self._openclaw_write_json(manager_workspace / "memory" / "episodic" / f"day_{day:02d}_plan.json", {"plan": plan_payload})
            self._openclaw_write_markdown(manager_workspace / "memory" / "daily" / f"day_{day:02d}_plan.md", f"{self.manager_agent_id} Day {day} ?? ??", [("??", plan_payload), ("?? ???", plan_memory)])
            self._openclaw_write_markdown(manager_workspace / "memory" / "rolling_summary.md", f"{self.manager_agent_id} ?? ?? ??", [("? ??", "? ?? ??????? ?? ????? run ???? ?? ???? ????."), ("?? ??", plan_payload), ("?? ???", plan_memory)])
            self._openclaw_write_markdown(manager_workspace / "MEMORY.md", f"{self.manager_agent_id} ?? ???", [("? ??", "? ?? ??????? ?? run ???? ?? ?? ???? ????."), ("?? ??", plan_payload), ("?? ???", plan_memory)])

    def _deterministic_review_watchouts(self, day_summary: dict[str, Any]) -> list[str]:
        watchouts: list[str] = []
        if int(day_summary.get("inspection_backlog_end", 0) or 0) > 0:
            watchouts.append(f"inspection_backlog_end={int(day_summary.get('inspection_backlog_end', 0) or 0)}")
        if int(day_summary.get("station1_output_buffer_end", 0) or 0) > 0:
            watchouts.append(f"station1_output_buffer_end={int(day_summary.get('station1_output_buffer_end', 0) or 0)}")
        if int(day_summary.get("station2_output_buffer_end", 0) or 0) > 0:
            watchouts.append(f"station2_output_buffer_end={int(day_summary.get('station2_output_buffer_end', 0) or 0)}")
        if int(day_summary.get("machine_breakdowns", 0) or 0) > 0:
            watchouts.append(f"machine_breakdowns={int(day_summary.get('machine_breakdowns', 0) or 0)}")
        if int(day_summary.get("agent_discharged_count", 0) or 0) > 0:
            watchouts.append(f"agent_discharged_count={int(day_summary.get('agent_discharged_count', 0) or 0)}")
        if not watchouts:
            watchouts.append("no_critical_bottleneck_detected")
        return watchouts[:6]

    def _build_deterministic_daily_review(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        day = int(day_summary.get("day", 0) or 0)
        watchouts = self._deterministic_review_watchouts(day_summary)
        summary = self._truncate_prompt_text(
            self.current_job_plan.manager_summary or "; ".join(watchouts),
            max_len=320,
        )
        review = {
            "day": day,
            "summary": summary,
            "task_priority_weights": dict(self.current_job_plan.task_priority_weights),
            "agent_priority_multipliers": {aid: dict(self.current_job_plan.agent_priority_multipliers.get(aid, {})) for aid in self.agent_ids},
            "personal_queues": dict(self.current_job_plan.personal_queues),
            "mailbox": dict(self.current_job_plan.mailbox),
            "reason_trace": list(self.current_job_plan.reason_trace),
            "detector_alignment": str(getattr(self.current_job_plan, "detector_alignment", "follow")),
            "watchouts": watchouts,
            "updated_norms": dict(norms if isinstance(norms, dict) else {}),
            "review_mode": "deterministic_from_day_summary",
        }
        return review

    def _sync_orchestration_review_workspace(
        self,
        *,
        day_summary: dict[str, Any],
        updated_norms: dict[str, Any],
        worker_reports: dict[str, dict[str, Any]],
        review: dict[str, Any],
    ) -> None:
        if not self._openclaw_enabled():
            return
        day = int(day_summary.get("day", 0) or 0)
        compact_day = self._day_summary_prompt_view(day_summary)
        for aid in self.agent_ids:
            workspace = self._openclaw_workspace_path(aid)
            if workspace is None:
                continue
            report = worker_reports.get(aid, {}) if isinstance(worker_reports.get(aid, {}), dict) else {}
            queue = review.get("personal_queues", {}).get(aid, []) if isinstance(review.get("personal_queues", {}), dict) else []
            mailbox = review.get("mailbox", {}).get(aid, []) if isinstance(review.get("mailbox", {}), dict) else []
            commitment = {
                "day": day,
                "summary": report.get("commitment", review.get("summary", "")),
                "focus_tasks": [str(item.get("task_family", "")).strip() for item in queue[:3] if isinstance(item, dict) and str(item.get("task_family", "")).strip()],
                "coordination_notes": [str(item.get("body", "")).strip() for item in mailbox[:3] if isinstance(item, dict) and str(item.get("body", "")).strip()],
            }
            beliefs = {
                "day": day,
                "local_beliefs": report.get("beliefs", []),
                "watchouts": review.get("watchouts", []),
                "priority_weights": review.get("task_priority_weights", {}),
            }
            semantic_memory = {
                "day": day,
                "specialization": commitment.get("focus_tasks", []),
                "heuristics": [entry.get("reason", "") for entry in review.get("reason_trace", []) if isinstance(entry, dict) and str(entry.get("reason", "")).strip()],
                "anti_patterns": report.get("blocked", []),
            }
            self._openclaw_write_json(workspace / "reports" / f"day_{day:02d}_report.json", report)
            self._openclaw_write_json(workspace / "facts" / "current_daily_report.json", report)
            self._openclaw_write_json(workspace / "facts" / "report_history" / f"day_{day:02d}.json", report)
            self._openclaw_write_json(workspace / "beliefs" / "current_beliefs.json", beliefs)
            self._openclaw_write_json(workspace / "beliefs" / "history" / f"day_{day:02d}.json", beliefs)
            self._openclaw_write_json(workspace / "commitments" / "current_commitment.json", commitment)
            self._openclaw_write_json(workspace / "commitments" / "history" / f"day_{day:02d}.json", commitment)
            self._openclaw_write_json(workspace / "memory" / "episodic" / f"day_{day:02d}.json", {"report": report, "day_summary": compact_day})
            self._openclaw_write_json(workspace / "memory" / "semantic" / "current.json", semantic_memory)
            self._openclaw_write_markdown(workspace / "memory" / "daily" / f"day_{day:02d}.md", f"{aid} Day {day} ??", [("?? ??", compact_day), ("??? ??", report), ("??", beliefs), ("??", commitment), ("?? ???", semantic_memory)])
        manager_workspace = self._phase_workspace_for_call("manager_daily_planner", {"phase": "manager_daily_planner", "day": day})
        if manager_workspace is not None:
            review_payload = {
                "day": day,
                "summary": review.get("summary", ""),
                "updated_norms": updated_norms,
                "task_priority_weights": review.get("task_priority_weights", {}),
                "agent_priority_multipliers": review.get("agent_priority_multipliers", {}),
                "personal_queues": review.get("personal_queues", {}),
                "mailbox": review.get("mailbox", {}),
                "reason_trace": review.get("reason_trace", []),
                "detector_alignment": review.get("detector_alignment", "follow"),
                "watchouts": review.get("watchouts", []),
            }
            review_memory = {
                "day": day,
                "summary": review.get("summary", ""),
                "watchouts": review.get("watchouts", []),
                "task_priority_weights": review.get("task_priority_weights", {}),
                "agent_priority_multipliers": review.get("agent_priority_multipliers", {}),
                "personal_queues": review.get("personal_queues", {}),
                "mailbox": review.get("mailbox", {}),
                "reason_trace": review.get("reason_trace", []),
                "detector_alignment": review.get("detector_alignment", "follow"),
                "updated_norms": updated_norms,
            }
            self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_worker_reports.json", worker_reports)
            self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_review.json", review_payload)
            self._openclaw_write_json(manager_workspace / "facts" / "current_review.json", review_payload)
            self._openclaw_write_json(manager_workspace / "facts" / "review_history" / f"day_{day:02d}.json", review_payload)
            self._openclaw_write_json(manager_workspace / "plans" / f"day_{day:02d}_review.json", review_payload)
            self._openclaw_write_json(manager_workspace / "trace" / f"day_{day:02d}_review_reason_trace.json", review.get("reason_trace", []))
            self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", review_memory)
            self._openclaw_write_json(manager_workspace / "beliefs" / "history" / f"day_{day:02d}_review.json", review_memory)
            self._openclaw_write_json(manager_workspace / "memory" / "episodic" / f"day_{day:02d}_review.json", {"review": review_payload, "worker_reports": worker_reports})
            self._openclaw_write_markdown(manager_workspace / "memory" / "daily" / f"day_{day:02d}_review.md", f"{self.manager_agent_id} Day {day} ??", [("??", review_payload), ("??? ??", worker_reports), ("?? ???", review_memory)])
            self._openclaw_write_markdown(manager_workspace / "memory" / "rolling_summary.md", f"{self.manager_agent_id} ?? ?? ??", [("? ??", "? ?? ??????? ?? ????? run ???? ??/?? ???? ????."), ("?? ?? ??", review_payload), ("?? ???", review_memory)])
            self._openclaw_write_markdown(manager_workspace / "MEMORY.md", f"{self.manager_agent_id} ?? ???", [("? ??", "? ?????? ???? ?? run ???? ??/?? ???? ????."), ("?? ?? ??", review_payload), ("?? ???", review_memory)])
    # Day start: current-state bottleneck diagnosis.
    # This phase should identify only the few constraints that most limit
    # accepted finished product completion right now, before any daily planning.
    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        runtime_agent_id = self._phase_runtime_agent_id("manager_bottleneck_detector", {"phase": "manager_bottleneck_detector", "day": observation.get("day")})
        system_prompt, prompt, required_keys = self._native_turn_prompts(
            agent_id=runtime_agent_id,
            phase="manager_bottleneck_detector",
            role_summary="You are BOTTLENECK_DETECTOR, a ranking-focused diagnostic agent that identifies the few constraints that most limit accepted finished product completion.",
            input_payload=self._detector_packet(observation),
            required_fields={
                "summary": "str",
                "top_bottlenecks": "list[dict]",
                "candidate_actions": "list[dict]",
                "reason_trace": "list[dict]",
            },
            instructions=[
                "Rank the 2-3 constraints whose removal would most improve accepted finished product completion today. Re-rank from scratch for today instead of preserving yesterday's winner.",
                "Start from throughput_closure_state first, then use constraint_state and supporting_detail only as supporting evidence.",
                "Before selecting rank 1, compare at least two competing bottleneck hypotheses using today's evidence.",
                "Prefer closure bottlenecks over merely visible machine-local anomalies when the two conflict, and lower the rank of any bottleneck whose evidence is weak today.",
                "top_bottlenecks must be a ranked list of objects with name, rank, severity, evidence[{metric,value}], and why_it_limits_output.",
                "candidate_actions must be a compact list of task-family suggestions only, with task_family, linked_bottleneck, and why.",
                "Do not assign workers. Do not build queues. Do not emit target_type, target_id, or target_station here.",
                "Each reason_trace item must be an object with decision, reason, evidence[{signal,value,source}], affected_agents, and task_families.",
                "Copy evidence values from today's state only. Return compact diagnosis only and do not narrate the full plant state back.",
            ],
            history_tag=f"day_{int(observation.get('day', 0) or 0):02d}_manager_bottleneck_detector",
        )
        self._assert_native_workspace_inputs_ready(runtime_agent_id, "manager_bottleneck_detector")
        llm_obj = self._call_llm_json(
            prompt,
            system_prompt,
            call_name="manager_bottleneck_detector",
            context={"phase": "manager_bottleneck_detector", "day": observation.get("day")},
            required_keys=required_keys,
        )
        diagnosis = {
            "top_bottlenecks": llm_obj.get("top_bottlenecks", []) if isinstance(llm_obj.get("top_bottlenecks", []), list) else [],
            "candidate_actions": llm_obj.get("candidate_actions", []) if isinstance(llm_obj.get("candidate_actions", []), list) else [],
            "watchouts": [],
        }
        summary = self._truncate_prompt_text(
            llm_obj.get("summary", "")
            or self._synthesize_plan_reasoning({}, observation, StrategyState(summary="", diagnosis={}, orchestration_context={}), self.current_job_plan).get(
                "manager_summary",
                "No bottleneck diagnosis provided.",
            ),
            max_len=320,
        )
        strategy = StrategyState(
            notes=self._flatten_diagnosis_to_notes(summary, diagnosis),
            summary=summary,
            diagnosis=diagnosis,
            orchestration_context={
                "reason_trace": self._sanitize_reason_trace(llm_obj.get("reason_trace")),
            },
        )
        self._sync_orchestration_reflection_workspace(observation=observation, strategy=strategy)
        return strategy
    # MANAGER turns the shared diagnosis into executable runtime context: shared
    # weights, personal queues, mailbox messages, parallel groups, and worker briefings.
    def propose_jobs(self, observation: dict[str, Any], strategy: StrategyState, norms: dict[str, Any]) -> JobPlan:
        # MANAGER converts the global diagnosis into concrete queues, handovers, and shared focus for the next day.
        fallback = self._default_job_plan(norms, observation)
        fallback.personal_queues = {aid: [] for aid in self.agent_ids}
        fallback.mailbox = {aid: [] for aid in self.agent_ids}
        runtime_agent_id = self._phase_runtime_agent_id("manager_daily_planner", {"phase": "manager_daily_planner", "day": observation.get("day")})
        system_prompt, prompt, required_keys = self._native_turn_prompts(
            agent_id=runtime_agent_id,
            phase="manager_daily_planner",
            role_summary="You are MANAGER_DAILY_PLANNER, an independent operating planner that validates detector hypotheses against today's execution evidence and issues authoritative day plans.",
            input_payload=self._planning_packet(observation, strategy, norms),
            required_fields={
                "plan_mode": "str",
                "weight_updates": "dict[str, float]",
                "queue_add": "dict[str, list]",
                "reason_trace": "list[dict]",
                "detector_alignment": "str",
            },
            instructions=[
                "Plan from today's evidence only. Do not preserve yesterday's focus unless today's evidence still supports it.",
                "Treat detector_hypothesis as a hypothesis to confirm, partially override, or override.",
                "Prefer worker-specific queue_add over generic weight changes when a concrete next action is visible.",
                "Use maintain only when today's evidence shows no materially stronger intervention than the active plan.",
                "Choose the intervention that most improves accepted finished-product completion over the remaining horizon.",
                "Use only task_family names from guardrails.allowed_task_priority_keys in weight_updates, queue_add, and reason_trace.",
                "If detector_hypothesis conflicts with stronger closure_signals or candidate_orders, you may reject or deprioritize it in reason_trace.",
                "If plan_mode=adjust, at least one of weight_updates or queue_add must be non-empty.",
                "Each reason_trace item must include detector_relation as follow, reject, or deprioritize.",
            ],
            history_tag=f"day_{int(observation.get('day', 0) or 0):02d}_manager_daily_planner",
        )
        self._assert_native_workspace_inputs_ready(runtime_agent_id, "manager_daily_planner")
        llm_obj = self._call_llm_json(
            prompt,
            system_prompt,
            call_name="manager_daily_planner",
            context={"phase": "manager_daily_planner", "day": observation.get("day")},
            required_keys=required_keys,
        )
        llm_obj, inert_plan_detected = self._ensure_actionable_manager_plan(llm_obj, observation, strategy, fallback)
        plan = self._build_orchestrated_job_plan(llm_obj, fallback, strategy)
        self.current_job_plan = plan
        self.agent_priority_multipliers = self._clone_agent_priority_multipliers(plan.agent_priority_multipliers)
        self._sync_orchestration_plan_workspace(observation, strategy, plan)
        return plan
    # The old multi-round townhall is replaced by worker daily reports plus a single
    # manager daily review that updates tomorrow's coordination structure.
    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        day = int(day_summary.get("day", 0) or 0)
        updated_norms = dict(norms if isinstance(norms, dict) else {})
        review = self._build_deterministic_daily_review(day_summary, updated_norms)
        self.last_worker_reports = {}
        self.last_manager_review = dict(review)
        self.current_job_plan.mailbox = dict(review.get("mailbox", {}))
        self.current_job_plan.reason_trace = list(review.get("reason_trace", []))
        self.shared_discussion_memory.append({
            "day": day,
            "issue_summary": {"top_priorities": self._weight_focus_summary(self.current_job_plan.task_priority_weights, limit=3), "watchouts": list(review.get("watchouts", []))},
            "changed_norm_keys": sorted(str(key) for key in updated_norms.keys()),
            "consensus_proposals": [],
            "conflicting_proposals": [],
        })
        if self.norms_enabled:
            prior_norms = self.shared_norms_memory[-1].get("norms", {}) if self.shared_norms_memory else norms
            delta = {}
            if isinstance(prior_norms, dict):
                for key, value in updated_norms.items():
                    if prior_norms.get(key) != value:
                        delta[key] = value
            self.shared_norms_memory.append({"day": day, "norms": dict(updated_norms), "delta": delta})
        self._last_discussion_trace = [{"day": day, "type": "deterministic_daily_review", "review": review}]
        self._sync_orchestration_review_workspace(day_summary=day_summary, updated_norms=updated_norms, worker_reports={}, review=review)
        return updated_norms

    # 긴급 재계획은 현재 비활성화한다. 런타임 안정화 전까지는 day-start reflect/plan 두 단계만 사용한다.
    def urgent_discuss(self, event: dict[str, Any], local_state: dict[str, Any]) -> dict[str, Any]:
        return {"priority_updates": {}}









































