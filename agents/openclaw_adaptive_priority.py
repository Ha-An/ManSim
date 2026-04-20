from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import JobPlan, StrategyState
from .openclaw_orchestrated import OpenClawOrchestratedDecisionModule
from .scripted import ScriptedDecisionModule

class OpenClawAdaptivePriorityDecisionModule(OpenClawOrchestratedDecisionModule):
    """OpenClaw managers tune priority policy while workers stay deterministic."""

    def __init__(self, cfg: dict[str, Any], llm_cfg: dict[str, Any] | None = None) -> None:
        super().__init__(cfg=cfg, llm_cfg=llm_cfg)
        self.incident_replan_enabled = False
        self.evaluator_enabled = False
        self.communication_enabled = False
        self.comm_rounds = 0
        orch_cfg = self.llm_cfg.get("orchestration", {}) if isinstance(self.llm_cfg.get("orchestration", {}), dict) else {}
        strategy_cfg = orch_cfg.get("strategy", {}) if isinstance(orch_cfg.get("strategy", {}), dict) else {}
        review_cfg = orch_cfg.get("review", {}) if isinstance(orch_cfg.get("review", {}), dict) else {}
        self.refresh_after_patch_count = max(1, int(strategy_cfg.get("refresh_after_patch_count", 2) or 2))
        self.review_enabled = bool(review_cfg.get("enabled", True))
        self.reviewer_max_prevention_targets = max(1, int(review_cfg.get("max_prevention_targets", 2) or 2))
        self.reviewer_max_failure_modes = max(1, int(review_cfg.get("max_failure_modes", 3) or 3))
        self.allowed_worker_roles = (
            "intake_runner",
            "reliability_guard",
            "inspection_closer",
            "battery_support",
            "flow_support",
        )
        self.shift_policy_history: list[dict[str, Any]] = []
        self.day_summary_memory: list[dict[str, Any]] = []
        self.day_review_memory: list[dict[str, Any]] = []
        self.current_shift_policy: dict[str, Any] = {}
        self.scripted_baseline = ScriptedDecisionModule(cfg)
        self.priority_blend_alpha = self._safe_float(strategy_cfg.get("priority_blend_alpha", 0.35), 0.35)
        self.agent_blend_alpha = self._safe_float(strategy_cfg.get("agent_blend_alpha", 0.40), 0.40)
        self.allowed_support_intents = (
            "closeout_support",
            "reliability_cover",
            "battery_cover",
            "flow_cover",
        )
        self.allowed_prevention_targets = (
            "closeout_gap",
            "battery_instability",
            "reliability_instability",
            "flow_blockage",
            "s2_underfeed",
        )
        self.allowed_failure_modes = self.allowed_prevention_targets
        self.allowed_daily_target_keys = (
            "min_accepted_products_today",
            "max_closeout_gap_end",
            "max_discharged_workers",
        )
        if not self._knowledge_enabled():
            self.series_knowledge_path = None
            self.series_knowledge_history_dir = None
            self.series_knowledge_text = ""

    def _knowledge_enabled(self) -> bool:
        return int(self.run_series_total) > 1

    def _reset_run_state(self) -> None:
        super()._reset_run_state()
        self.shift_policy_history = []
        self.day_summary_memory = []
        self.day_review_memory = []
        self.current_shift_policy = {}
        if not self._knowledge_enabled():
            self.series_knowledge_text = ""

    def _manager_knowledge_workspace_aliases(self) -> list[str]:
        if not self._knowledge_enabled():
            return []
        return ["MANAGER_SHIFT_STRATEGIST", "MANAGER_DAILY_REVIEWER", "MANAGER_RUN_REFLECTOR"]

    def _build_day_scoped_runtime_agent_id(self, phase: str, day: int | None = None) -> str:
        suffix = self._phase_runtime_agent_suffix()
        phase_key = str(phase or "").strip().lower()
        safe_day = max(1, int(day or 1))
        if phase_key == "manager_shift_strategist":
            return f"MANAGER_SHIFT_STRATEGIST_{suffix}_D{safe_day}"
        if phase_key == "manager_daily_reviewer":
            return f"MANAGER_DAILY_REVIEWER_{suffix}_D{safe_day}"
        if phase_key == "manager_run_reflector":
            return f"MANAGER_RUN_REFLECTOR_{suffix}"
        return super()._build_day_scoped_runtime_agent_id(phase, day)

    def _build_phase_runtime_agent_ids(self) -> dict[str, str]:
        ids: dict[str, str] = {}
        for day in range(1, self._simulation_total_days() + 1):
            ids[f"{self.manager_agent_id}:manager_shift_strategist:d{day}"] = self._build_day_scoped_runtime_agent_id("manager_shift_strategist", day)
            ids[f"{self.manager_agent_id}:manager_daily_reviewer:d{day}"] = self._build_day_scoped_runtime_agent_id("manager_daily_reviewer", day)
        if self._knowledge_enabled():
            ids[f"{self.manager_agent_id}:manager_run_reflector"] = self._build_day_scoped_runtime_agent_id("manager_run_reflector")
        return ids

    def _runtime_agent_workspace_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for day in range(1, self._simulation_total_days() + 1):
            aliases[self._build_day_scoped_runtime_agent_id("manager_shift_strategist", day)] = "MANAGER_SHIFT_STRATEGIST"
            aliases[self._build_day_scoped_runtime_agent_id("manager_daily_reviewer", day)] = "MANAGER_DAILY_REVIEWER"
        if self._knowledge_enabled():
            aliases[self._build_day_scoped_runtime_agent_id("manager_run_reflector")] = "MANAGER_RUN_REFLECTOR"
        for aid in self.agent_ids:
            upper = self._normalize_openclaw_agent_id(aid)
            aliases[upper] = upper
        aliases[self.manager_agent_id] = self.manager_agent_id
        return aliases

    def _openclaw_agent_for_call(self, call_name: str, context: dict[str, Any] | None = None) -> str:
        ctx = context or {}
        day = max(1, int(ctx.get("day", 1) or 1))
        if call_name == "manager_shift_strategist":
            return self.phase_runtime_agent_ids.get(
                f"{self.manager_agent_id}:manager_shift_strategist:d{day}",
                self._build_day_scoped_runtime_agent_id("manager_shift_strategist", day),
            )
        if call_name == "manager_daily_reviewer":
            return self.phase_runtime_agent_ids.get(
                f"{self.manager_agent_id}:manager_daily_reviewer:d{day}",
                self._build_day_scoped_runtime_agent_id("manager_daily_reviewer", day),
            )
        if call_name == "manager_run_reflector":
            return self.phase_runtime_agent_ids.get(
                f"{self.manager_agent_id}:manager_run_reflector",
                self._build_day_scoped_runtime_agent_id("manager_run_reflector"),
            )
        return super()._openclaw_agent_for_call(call_name, context)

    def prepare_run_context(self, output_root: Path | str) -> dict[str, Any]:
        self._reset_run_state()
        self.run_output_root = Path(output_root)
        self.phase_runtime_agent_ids = self._build_phase_runtime_agent_ids()
        runtime_info = self.openclaw_client.prepare_run_runtime(
            output_root=Path(output_root),
            worker_agent_ids=[],
            manager_agent_id=self.manager_agent_id,
            workspace_template_root=self.openclaw_workspace_root,
            agent_workspace_aliases=self._runtime_agent_workspace_aliases(),
            runtime_agent_ids=list(self._runtime_agent_workspace_aliases().keys()),
            prune_unused_agents=True,
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
        self._seed_cross_run_knowledge()
        gateway_info: dict[str, Any] = self.openclaw_client.restart_gateway()
        merged = dict(runtime_info)
        merged["gateway"] = gateway_info
        merged["run_id"] = self.openclaw_run_id
        merged["transport"] = self.openclaw_transport
        merged["knowledge_in_path"] = str(self.series_knowledge_path.resolve()) if self.series_knowledge_path is not None else ""
        return merged

    def _native_phase_directives(self, phase: str) -> list[str]:
        directives = {
            "manager_shift_strategist": [
                "Create a day-start intent-only policy for deterministic priority execution.",
                "Do not emit commitments, queue-like work orders, or low-level priority maps.",
                "Choose only canonical roles, prevention targets, support intent, and daily targets.",
                "Do not assign all workers to the same generic role.",
                "Do not demote battery resilience below material flow when battery risk is active.",
            ],
            "manager_daily_reviewer": [
                "Review the completed day and return diagnosis only.",
                "Do not restate raw metrics that already appear in the request packet.",
                "Return only target misses, failure labels, recommended prevention targets, support pair guidance, role-change advice, and carry-forward risks.",
                "Focus on what tomorrow's strategist should change, not on replaying today's facts.",
            ],
        }
        if str(phase or "").strip() in directives:
            return list(directives[str(phase).strip()])
        return super()._native_phase_directives(phase)

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
        if str(phase) not in {"manager_shift_strategist", "manager_daily_reviewer"}:
            return super()._native_turn_prompts(
                agent_id=agent_id,
                phase=phase,
                role_summary=role_summary,
                input_payload=input_payload,
                required_fields=required_fields,
                instructions=instructions,
                history_tag=history_tag,
            )
        workspace = self._openclaw_workspace_path(agent_id)
        response_template = self._native_response_template(required_fields)
        request_payload = {
            "phase": phase,
            "language": "ENG",
            "role": role_summary,
            "input": self._prune_prompt_value(input_payload),
            "required_keys": list(required_fields.keys()),
            "instructions": [str(item).strip() for item in instructions if str(item).strip()],
            "response_rule": "Return exactly one JSON object matching current_response_template.json.",
            "language_rule": "Natural-language values must be in English. JSON keys and IDs stay in English.",
        }
        if str(phase) == "manager_shift_strategist":
            request_payload["policy_contract"] = {
                "worker_roles": {agent: "one_of[intake_runner,reliability_guard,inspection_closer,battery_support,flow_support]" for agent in self.agent_ids},
                "operating_focus": "one_of[flow,reliability,closeout,battery]",
                "late_horizon_mode": "one_of[normal,closeout_drive,reliability_guarded_closeout,battery_guarded_closeout]",
                "role_plan": {agent: {"role": "canonical_role", "reason": "short English rationale"} for agent in self.agent_ids},
                "support_plan": {
                    "primary_support_pair": "A1->A3",
                    "support_intent": "one_of[closeout_support,reliability_cover,battery_cover,flow_cover]",
                    "reason": "short English rationale",
                },
                "prevention_targets": ["one_of[closeout_gap,battery_instability,reliability_instability,flow_blockage,s2_underfeed]"],
                "daily_targets": {
                    "min_accepted_products_today": "int",
                    "max_closeout_gap_end": "int",
                    "max_discharged_workers": "int",
                },
                "plan_revision": "int",
                "limits": {"max_prevention_targets": 2, "max_daily_targets": 3},
            }
        else:
            request_payload["review_contract"] = {
                "target_misses": ["one_of[min_accepted_products_today,max_closeout_gap_end,max_discharged_workers]"],
                "top_failure_modes": ["one_of[closeout_gap,battery_instability,reliability_instability,flow_blockage,s2_underfeed]"],
                "recommended_prevention_targets": ["one_of[closeout_gap,battery_instability,reliability_instability,flow_blockage,s2_underfeed]"],
                "recommended_support_pair": "A1->A3",
                "role_change_advice": {"A3": "one_of[intake_runner,reliability_guard,inspection_closer,battery_support,flow_support]"},
                "carry_forward_risks": ["short English string"],
                "limits": {"max_target_misses": 3, "max_failure_modes": 3, "max_prevention_targets": 2, "max_risks": 4},
            }
        if workspace is not None:
            self._openclaw_write_json(workspace / "facts" / "current_request.json", request_payload)
            self._openclaw_write_json(workspace / "facts" / "request_history" / f"{history_tag}.json", request_payload)
            self._openclaw_write_json(workspace / "facts" / "current_response_template.json", response_template)
            (workspace / "facts" / "current_phase.txt").write_text(str(phase), encoding="utf-8")
        system_prompt = "Native-local simulator turn. Use workspace facts only. Return one JSON object only."
        if str(phase) == "manager_shift_strategist":
            user_prompt = "Execute manager_shift_strategist. Keep output compact and intent-only. Return worker_roles for A1/A2/A3, operating_focus, late_horizon_mode, role_plan, support_plan, prevention_targets, daily_targets, and plan_revision. Do not emit task_priority_weights, agent_priority_multipliers, mailbox_seed, commitments, or personal queues. Keep exactly one clear inspection_closer. Use previous_day_review to decide what should be prevented today. Choose at most two prevention_targets and one primary_support_pair."
        else:
            user_prompt = "Execute manager_daily_reviewer. Keep output compact and diagnosis-only. Do not repeat raw metrics from the request. Return only target_misses, top_failure_modes, recommended_prevention_targets, recommended_support_pair, role_change_advice, and carry_forward_risks. Focus on what tomorrow's strategist should change."
        return system_prompt, user_prompt, dict(required_fields)

    def _backend_direct_turn_prompts(
        self,
        *,
        phase: str,
        role_summary: str,
        input_payload: dict[str, Any],
        required_fields: dict[str, str],
        instructions: list[str],
    ) -> tuple[str, str, dict[str, str]]:
        phase_name = str(phase or "").strip().lower()
        packet = self._prune_prompt_value(input_payload) or {}
        task_families = list(self.allowed_task_priority_keys)
        request_blob = self._truncate_prompt_text(json.dumps(packet, ensure_ascii=False, indent=2), max_len=7000)
        if phase_name == "manager_shift_strategist":
            template = {
                "summary": "short English summary under 18 words",
                "worker_roles": {"A1": "intake_runner", "A2": "reliability_guard", "A3": "inspection_closer"},
                "operating_focus": "closeout",
                "late_horizon_mode": "closeout_drive",
                "role_plan": {"A1": {"role": "intake_runner", "reason": "short English rationale"}},
                "support_plan": {"primary_support_pair": "A1->A3", "support_intent": "closeout_support", "reason": "short English rationale"},
                "prevention_targets": ["closeout_gap", "battery_instability"],
                "daily_targets": {"min_accepted_products_today": 4, "max_closeout_gap_end": 1, "max_discharged_workers": 0},
                "plan_revision": 1,
            }
            extra_rules = [
                "worker_roles values must be short role strings only. Do not nest objects under worker_roles.",
                "Use only these canonical role names: intake_runner, reliability_guard, inspection_closer, battery_support, flow_support.",
                "Reason in terms of backlog control, accepted-product close-out, battery resilience, and late-horizon tradeoffs.",
                "operating_focus is a top-level short label from flow, reliability, closeout, battery.",
                "late_horizon_mode is a top-level short label from normal, closeout_drive, reliability_guarded_closeout, battery_guarded_closeout.",
                "role_plan and support_plan should explain how today's role mix prevents known failure modes.",
                "support_plan.primary_support_pair must be one pair such as A1->A3.",
                "support_plan.support_intent must be one of closeout_support, reliability_cover, battery_cover, flow_cover.",
                "prevention_targets is a short list using only closeout_gap, battery_instability, reliability_instability, flow_blockage, s2_underfeed.",
                "daily_targets may include min_accepted_products_today, max_closeout_gap_end, and max_discharged_workers only.",
                "Keep exactly one inspection_closer unless inspection backlog is severe enough to justify a second temporary closer.",
                "Prefer A1 as intake_runner or flow_support. Only assign A1 to battery_support or reliability_guard when downstream flow is already stable and the support risk clearly dominates.",
                "Do not assign both A1 and A2 to support roles unless the request packet shows compound battery and reliability pressure.",
                "Maintain role continuity from the previous day unless previous_day_review shows the current role mix failed.",
                "Use only these worker IDs: A1, A2, A3.",
                "Keep the whole JSON compact so it fits well within the token budget.",
            ]
        elif phase_name == "manager_daily_reviewer":
            template = {
                "target_misses": ["max_closeout_gap_end"],
                "top_failure_modes": ["closeout_gap", "battery_instability"],
                "recommended_prevention_targets": ["closeout_gap", "battery_instability"],
                "recommended_support_pair": "A1->A3",
                "role_change_advice": {"A3": "inspection_closer"},
                "carry_forward_risks": ["inspection output stayed open late"],
            }
            extra_rules = [
                "Do not repeat raw metrics that already appear in the request packet.",
                "target_misses must use only min_accepted_products_today, max_closeout_gap_end, max_discharged_workers.",
                "top_failure_modes and recommended_prevention_targets must use only closeout_gap, battery_instability, reliability_instability, flow_blockage, s2_underfeed.",
                "recommended_support_pair must be one pair such as A1->A3.",
                "role_change_advice values must be canonical role names only.",
                "Carry-forward risks should be short English strings, not raw metric dumps.",
                "Keep the whole JSON compact so it fits well within the token budget.",
            ]
        else:
            return super()._backend_direct_turn_prompts(
                phase=phase,
                role_summary=role_summary,
                input_payload=input_payload,
                required_fields=required_fields,
                instructions=instructions,
            )
        template_blob = json.dumps(template, ensure_ascii=False, indent=2)
        system_prompt = self._shared_system_prompt(
            role_summary,
            phase_guidance=[
                *instructions,
                *extra_rules,
                "Use the request packet as the only source of current-state evidence.",
                "Return one compact JSON object that exactly matches the response template shape.",
            ],
            include_task_family_semantics=True,
            include_norm_semantics=True,
            task_family_subset=task_families,
            compact=True,
        )
        user_prompt = (
            f"Execute {phase_name} for the manufacturing simulation.\n"
            f"Allowed task families: {', '.join(task_families)}\n"
            "Return exactly one JSON object with the same top-level keys and value types as RESPONSE_TEMPLATE.\n"
            "If evidence is missing, keep the correct empty type instead of inventing placeholder business tasks.\n"
            "REQUEST_PACKET:\n"
            f"{request_blob}\n\n"
            "RESPONSE_TEMPLATE:\n"
            f"{template_blob}"
        )
        return system_prompt, user_prompt, dict(required_fields)

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return int(default)

    def _latest_day_summary_memory(self) -> dict[str, Any]:
        return dict(self.day_summary_memory[-1]) if self.day_summary_memory else {}

    def _latest_day_review_memory(self) -> dict[str, Any]:
        return dict(self.day_review_memory[-1]) if self.day_review_memory else {}

    def _knowledge_prompt_blob(self) -> str:
        if not self._knowledge_enabled():
            return ""
        return self._truncate_prompt_text(self.series_knowledge_text or self._load_series_knowledge_text(), max_len=4000)

    def _canonical_role_name(self, value: Any) -> str:
        text = self._truncate_prompt_text(value, max_len=64).strip().lower()
        if not text:
            return ""
        aliases = {
            "intake_runner": {"intake_runner", "intake", "flow_intake", "material_supplier", "material_supply", "intake-flow", "intake_runner_flow"},
            "reliability_guard": {"reliability_guard", "reliability", "repair_guard", "maintenance_guard", "pm_guard", "maintenance", "repair"},
            "inspection_closer": {"inspection_closer", "inspection", "quality", "quality_closer", "closeout", "inspection_closeout", "closer"},
            "battery_support": {"battery_support", "battery", "battery_rescue", "battery_guard", "energy_support"},
            "flow_support": {"flow_support", "flow", "transfer_support", "material_support", "unload_support"},
        }
        for canonical, names in aliases.items():
            if text in names:
                return canonical
        if any(token in text for token in ("inspect", "quality", "clos")):
            return "inspection_closer"
        if "battery" in text or "energy" in text:
            return "battery_support"
        if any(token in text for token in ("reliability", "repair", "maintenance", "pm")):
            return "reliability_guard"
        if any(token in text for token in ("intake", "material", "supply")):
            return "intake_runner"
        if any(token in text for token in ("flow", "transfer", "unload")):
            return "flow_support"
        return ""

    def _sanitize_worker_roles(self, src: Any) -> dict[str, str]:
        out = {agent_id: "" for agent_id in self.agent_ids}
        if not isinstance(src, dict):
            return out
        for agent_id in self.agent_ids:
            text = self._canonical_role_name(src.get(agent_id, ""))
            if text:
                out[agent_id] = text
        return out

    def _role_bucket(self, role: str) -> str:
        text = self._canonical_role_name(role)
        if text == "inspection_closer":
            return "inspection"
        if text in {"reliability_guard", "battery_support"}:
            return "support"
        if text in {"intake_runner", "flow_support"}:
            return "flow"
        return "generic"

    def _normalize_operating_focus(self, value: Any) -> str:
        text = self._truncate_prompt_text(value, max_len=48).strip().lower()
        aliases = {
            "flow": {"flow", "throughput", "intake", "material_flow"},
            "reliability": {"reliability", "repair", "maintenance", "stability"},
            "closeout": {"closeout", "inspection", "closure", "accepted_product_closeout"},
            "battery": {"battery", "energy", "battery_recovery"},
        }
        for canonical, names in aliases.items():
            if text in names:
                return canonical
        return ""

    def _normalize_late_horizon_mode(self, value: Any) -> str:
        text = self._truncate_prompt_text(value, max_len=64).strip().lower()
        aliases = {
            "normal": {"normal", "baseline"},
            "closeout_drive": {"closeout_drive", "closeout", "closure_drive"},
            "reliability_guarded_closeout": {"reliability_guarded_closeout", "reliability_closeout", "guarded_closeout"},
            "battery_guarded_closeout": {"battery_guarded_closeout", "battery_closeout", "battery_guarded"},
        }
        for canonical, names in aliases.items():
            if text in names:
                return canonical
        return ""

    def _normalize_support_intent(self, value: Any) -> str:
        text = self._truncate_prompt_text(value, max_len=48).strip().lower()
        aliases = {
            "closeout_support": {"closeout_support", "closeout", "inspection_support", "closeout_cover"},
            "reliability_cover": {"reliability_cover", "reliability_support", "repair_cover", "maintenance_cover"},
            "battery_cover": {"battery_cover", "battery_support", "energy_cover", "battery_rescue"},
            "flow_cover": {"flow_cover", "flow_support", "intake_cover", "material_flow_cover"},
        }
        for canonical, names in aliases.items():
            if text in names:
                return canonical
        return ""

    def _normalize_prevention_target(self, value: Any) -> str:
        text = self._truncate_prompt_text(value, max_len=48).strip().lower()
        aliases = {
            "closeout_gap": {"closeout_gap", "late_closeout_gap", "inspection_output_open", "closure_gap"},
            "battery_instability": {"battery_instability", "battery_risk", "battery_cluster", "discharged_workers"},
            "reliability_instability": {"reliability_instability", "repair_risk", "breakdown_risk", "reliability_cluster"},
            "flow_blockage": {"flow_blockage", "buffer_blockage", "flow_cluster", "blocking_flow"},
            "s2_underfeed": {"s2_underfeed", "downstream_underfeed", "station2_underfeed"},
        }
        for canonical, names in aliases.items():
            if text in names:
                return canonical
        return ""

    def _sanitize_prevention_targets(self, src: Any, *, limit: int | None = None) -> list[str]:
        values = src if isinstance(src, list) else [src]
        out: list[str] = []
        max_items = max(1, int(limit or self.reviewer_max_prevention_targets or 2))
        for item in values:
            normalized = self._normalize_prevention_target(item)
            if normalized and normalized not in out:
                out.append(normalized)
            if len(out) >= max_items:
                break
        return out

    def _sanitize_daily_targets(self, src: Any) -> dict[str, int]:
        blob = src if isinstance(src, dict) else {}
        out: dict[str, int] = {}
        if "min_accepted_products_today" in blob:
            out["min_accepted_products_today"] = self._clamp_int(blob.get("min_accepted_products_today"), 0, 20, 0)
        if "max_closeout_gap_end" in blob:
            out["max_closeout_gap_end"] = self._clamp_int(blob.get("max_closeout_gap_end"), 0, 10, 1)
        if "max_discharged_workers" in blob:
            out["max_discharged_workers"] = self._clamp_int(blob.get("max_discharged_workers"), 0, len(self.agent_ids), 0)
        return out

    def _parse_support_pair(self, value: Any) -> tuple[str, str]:
        text = self._truncate_prompt_text(value, max_len=32).strip().upper()
        if "->" not in text:
            return "", ""
        left, right = [part.strip() for part in text.split("->", 1)]
        if left in self.agent_ids and right in self.agent_ids and left != right:
            return left, right
        return "", ""

    def _sanitize_support_plan(
        self,
        src: Any,
        *,
        worker_roles: dict[str, str],
        operating_focus: str,
        prevention_targets: list[str],
        previous_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blob = src if isinstance(src, dict) else {}
        previous_review = previous_review if isinstance(previous_review, dict) else {}
        pair_source, pair_target = self._parse_support_pair(blob.get("primary_support_pair", ""))
        if not pair_source or not pair_target:
            review_pair = str(previous_review.get("recommended_support_pair", "")).strip()
            pair_source, pair_target = self._parse_support_pair(review_pair)
        if not pair_source or not pair_target:
            if "closeout_gap" in prevention_targets or operating_focus == "closeout":
                pair_source, pair_target = "A1", "A3"
            elif "battery_instability" in prevention_targets or operating_focus == "battery":
                pair_source, pair_target = "A1", "A2"
            elif "reliability_instability" in prevention_targets or operating_focus == "reliability":
                pair_source, pair_target = "A1", "A2"
            else:
                pair_source, pair_target = "A1", "A3"
        support_intent = self._normalize_support_intent(blob.get("support_intent", ""))
        if "closeout_gap" in prevention_targets or operating_focus == "closeout":
            support_intent = "closeout_support"
            pair_source, pair_target = "A1", "A3"
        elif "battery_instability" in prevention_targets or operating_focus == "battery":
            if not support_intent or support_intent == "flow_cover":
                support_intent = "battery_cover"
            pair_source, pair_target = "A1", "A2"
        elif "reliability_instability" in prevention_targets or operating_focus == "reliability":
            if not support_intent or support_intent == "flow_cover":
                support_intent = "reliability_cover"
            pair_source, pair_target = "A1", "A2"
        elif not support_intent:
            support_intent = "flow_cover"
        return {
            "primary_support_pair": f"{pair_source}->{pair_target}",
            "support_intent": support_intent,
            "reason": self._truncate_prompt_text(blob.get("reason", previous_review.get("review_summary", "")), max_len=120),
        }

    def _stabilize_compiled_roles(
        self,
        roles: dict[str, str],
        *,
        prevention_targets: list[str],
        signals: dict[str, Any],
        support_intent: str,
    ) -> dict[str, str]:
        stable = {agent_id: self._canonical_role_name(roles.get(agent_id, "")) or self._role_defaults().get(agent_id, "flow_support") for agent_id in self.agent_ids}
        stable["A3"] = "inspection_closer"

        reliability_risk = bool("reliability_instability" in prevention_targets or int(signals.get("broken_machines", 0) or 0) > 0)
        battery_risk = bool(int(signals.get("discharged_agents", 0) or 0) > 0 or int(signals.get("low_battery_agents", 0) or 0) >= 2)
        closeout_risk = bool("closeout_gap" in prevention_targets)

        if reliability_risk:
            stable["A2"] = "reliability_guard"
        elif battery_risk:
            stable["A2"] = "battery_support"
        elif stable.get("A2") == "battery_support":
            stable["A2"] = "reliability_guard"

        if support_intent == "closeout_support" or closeout_risk:
            if stable.get("A1") not in {"intake_runner", "flow_support"}:
                stable["A1"] = "intake_runner"

        return self._enforce_role_coverage(stable)

    def _sanitize_role_change_advice(self, src: Any) -> dict[str, str]:
        blob = src if isinstance(src, dict) else {}
        out: dict[str, str] = {}
        for agent_id in self.agent_ids:
            normalized = self._canonical_role_name(blob.get(agent_id, ""))
            if normalized:
                out[agent_id] = normalized
        return out

    def _sanitize_reviewer_output(self, src: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        blob = src if isinstance(src, dict) else {}
        out = {
            "target_misses": [],
            "top_failure_modes": [],
            "recommended_prevention_targets": [],
            "recommended_support_pair": "",
            "role_change_advice": {},
            "carry_forward_risks": [],
        }
        for key in ("min_accepted_products_today", "max_closeout_gap_end", "max_discharged_workers"):
            if key in self._as_str_list(blob.get("target_misses"), []):
                out["target_misses"].append(key)
        if not out["target_misses"]:
            out["target_misses"] = list(fallback.get("target_misses", []))
        for item in self._as_str_list(blob.get("top_failure_modes"), []):
            normalized = self._normalize_prevention_target(item)
            if normalized and normalized not in out["top_failure_modes"]:
                out["top_failure_modes"].append(normalized)
            if len(out["top_failure_modes"]) >= self.reviewer_max_failure_modes:
                break
        if not out["top_failure_modes"]:
            out["top_failure_modes"] = list(fallback.get("top_failure_modes", []))
        out["recommended_prevention_targets"] = self._sanitize_prevention_targets(
            blob.get("recommended_prevention_targets", fallback.get("recommended_prevention_targets", [])),
            limit=self.reviewer_max_prevention_targets,
        )
        pair_source, pair_target = self._parse_support_pair(blob.get("recommended_support_pair", fallback.get("recommended_support_pair", "")))
        if pair_source and pair_target:
            out["recommended_support_pair"] = f"{pair_source}->{pair_target}"
        else:
            out["recommended_support_pair"] = str(fallback.get("recommended_support_pair", "")).strip()
        out["role_change_advice"] = self._sanitize_role_change_advice(blob.get("role_change_advice", fallback.get("role_change_advice", {})))
        risks = [self._truncate_prompt_text(item, max_len=96) for item in self._as_str_list(blob.get("carry_forward_risks"), []) if self._truncate_prompt_text(item, max_len=96)]
        out["carry_forward_risks"] = risks[:4] if risks else list(fallback.get("carry_forward_risks", []))[:4]
        return out

    def _enforce_role_coverage(self, roles: dict[str, str]) -> dict[str, str]:
        defaults = self._role_defaults()
        final = {
            agent_id: self._canonical_role_name(roles.get(agent_id, "")) or defaults.get(agent_id, "flow_support")
            for agent_id in self.agent_ids
        }
        buckets = {agent_id: self._role_bucket(role) for agent_id, role in final.items()}
        if {"flow", "support", "inspection"}.issubset(set(buckets.values())):
            return final
        if "flow" not in set(buckets.values()) and "A1" in final:
            final["A1"] = defaults["A1"]
        if "support" not in set(self._role_bucket(role) for role in final.values()) and "A2" in final:
            final["A2"] = defaults["A2"]
        if "inspection" not in set(self._role_bucket(role) for role in final.values()) and "A3" in final:
            final["A3"] = defaults["A3"]
        return final

    def _next_plan_revision(self) -> int:
        current = max(
            int(getattr(self.current_job_plan, "plan_revision", 0) or 0),
            self._safe_int(self.current_shift_policy.get("plan_revision", 0), 0),
        )
        return current + 1

    def _default_reason_trace(self, *, summary: str, focus_tasks: list[str], decision: str = "adjust") -> list[dict[str, Any]]:
        return [{
            "decision": decision,
            "reason": self._truncate_prompt_text(summary, max_len=220) or "priority policy updated from current operating evidence.",
            "evidence": [],
            "affected_agents": list(self.agent_ids[:3]),
            "task_families": [task for task in focus_tasks if task in self.allowed_task_priority_keys][:4],
            "detector_relation": "follow",
        }]

    def _merge_mailbox_payload(self, base_mailbox: Any, patch_mailbox: Any) -> dict[str, list[dict[str, Any]]]:
        base = self._sanitize_mailbox(base_mailbox)
        patch = self._sanitize_mailbox(patch_mailbox)
        merged: dict[str, list[dict[str, Any]]] = {agent_id: list(base.get(agent_id, [])) for agent_id in self.agent_ids}
        for agent_id in self.agent_ids:
            if patch.get(agent_id):
                merged[agent_id] = list(patch.get(agent_id, []))
        return merged

    def _top_task_keys(self, weights: dict[str, Any], limit: int = 3) -> list[str]:
        return [str(item.get("task_family", "")).strip() for item in self._weight_focus_summary(weights, limit=limit) if str(item.get("task_family", "")).strip()]

    def _compact_opportunity_focus(self, observation: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in self._current_opportunity_rows(observation)[: max(1, limit)]:
            if not isinstance(item, dict):
                continue
            target = item.get("target", {}) if isinstance(item.get("target", {}), dict) else {}
            entry: dict[str, Any] = {
                "opportunity_id": str(item.get("opportunity_id", "")).strip(),
                "task_family": str(item.get("task_family", "")).strip(),
                "target": {
                    "target_type": str(target.get("target_type", "none")).strip().lower() or "none",
                    "target_id": self._truncate_prompt_text(target.get("target_id", ""), max_len=32),
                    "target_station": target.get("target_station"),
                },
                "impact": round(float(item.get("expected_output_impact", 0.0) or 0.0), 1),
                "location": self._truncate_prompt_text(item.get("location", ""), max_len=32),
            }
            owners = [aid for aid in self._as_str_list(item.get("owners"), []) if aid in self.agent_ids][:2]
            if owners:
                entry["owners"] = owners
            compact.append(self._prune_prompt_value(entry) or {})
        return [item for item in compact if isinstance(item, dict) and item]

    def _compact_agent_priority_focus(
        self,
        multipliers: dict[str, dict[str, float]] | Any,
        *,
        limit_per_agent: int = 2,
    ) -> dict[str, list[dict[str, Any]]]:
        compact: dict[str, list[dict[str, Any]]] = {}
        src = multipliers if isinstance(multipliers, dict) else {}
        for agent_id in self.agent_ids:
            row = src.get(agent_id, {}) if isinstance(src.get(agent_id, {}), dict) else {}
            focus = [item for item in self._weight_focus_summary(row, limit=max(1, limit_per_agent + 1)) if float(item.get("weight", 0.0) or 0.0) > 1.0]
            if not focus:
                continue
            compact[agent_id] = focus[: max(1, limit_per_agent)]
        return compact

    def _compact_mailbox_focus(self, mailbox: Any, *, limit_per_agent: int = 1) -> dict[str, list[dict[str, Any]]]:
        cleaned = self._sanitize_mailbox(mailbox)
        compact: dict[str, list[dict[str, Any]]] = {}
        for agent_id in self.agent_ids:
            items = cleaned.get(agent_id, []) if isinstance(cleaned.get(agent_id, []), list) else []
            focus: list[dict[str, Any]] = []
            for item in items[: max(1, limit_per_agent)]:
                if not isinstance(item, dict):
                    continue
                row = {
                    "message_type": str(item.get("message_type", "")).strip(),
                    "task_family": str(item.get("task_family", "")).strip(),
                    "target_type": str(item.get("target_type", "")).strip(),
                    "target_id": self._truncate_prompt_text(item.get("target_id", ""), max_len=32),
                    "target_station": item.get("target_station"),
                    "priority": int(item.get("priority", 1) or 1),
                    "remaining_uses": int(item.get("remaining_uses", 1) or 1),
                    "body": self._truncate_prompt_text(item.get("body", ""), max_len=64),
                }
                pruned = self._prune_prompt_value(row)
                if isinstance(pruned, dict) and pruned:
                    focus.append(pruned)
            if focus:
                compact[agent_id] = focus
        return compact

    def _compact_policy_snapshot(self, *, include_mailbox: bool) -> dict[str, Any]:
        plan = self.current_job_plan if isinstance(getattr(self, "current_job_plan", None), JobPlan) else None
        weights = dict(getattr(plan, "task_priority_weights", {}) or {})
        multipliers = {aid: dict((getattr(plan, "agent_priority_multipliers", {}) or {}).get(aid, {})) for aid in self.agent_ids}
        roles = dict(getattr(plan, "agent_roles", {}) or {})
        guidance = dict(getattr(plan, "incident_guidance", {}) or {})
        payload: dict[str, Any] = {
            "top_task_priority_weights": self._weight_focus_summary(weights, limit=5),
            "agent_priority_focus": self._compact_agent_priority_focus(multipliers, limit_per_agent=2),
            "worker_roles": {aid: str(role).strip() for aid, role in roles.items() if str(role).strip()},
            "operating_focus": str(guidance.get("operating_focus", "")).strip(),
            "late_horizon_mode": str(guidance.get("late_horizon_mode", "")).strip(),
            "support_plan": self._compact_event_details(guidance.get("support_plan", {}), limit=4),
            "prevention_targets": list(guidance.get("prevention_targets", []))[:2] if isinstance(guidance.get("prevention_targets", []), list) else [],
            "daily_targets": self._compact_event_details(guidance.get("daily_targets", {}), limit=3),
            "plan_revision": int(getattr(plan, "plan_revision", 0) or 0),
        }
        if include_mailbox:
            payload["mailbox_focus"] = self._compact_mailbox_focus(getattr(plan, "mailbox", {}), limit_per_agent=1)
        return self._prune_prompt_value(payload) or {}

    def _compact_event_details(self, details: Any, *, limit: int = 6) -> dict[str, Any]:
        src = details if isinstance(details, dict) else {}
        compact: dict[str, Any] = {}
        for idx, (key, value) in enumerate(src.items()):
            if idx >= max(1, limit):
                break
            field = str(key).strip()
            if not field:
                continue
            if isinstance(value, (bool, int, float)):
                compact[field] = value
            elif isinstance(value, str):
                text = self._truncate_prompt_text(value, max_len=96)
                if text:
                    compact[field] = text
            elif isinstance(value, list):
                compact[field] = [
                    self._truncate_prompt_text(item, max_len=48) if isinstance(item, str) else item
                    for item in value[:3]
                ]
            elif isinstance(value, dict):
                nested: dict[str, Any] = {}
                for nested_idx, (nested_key, nested_value) in enumerate(value.items()):
                    if nested_idx >= 3:
                        break
                    nested_name = str(nested_key).strip()
                    if not nested_name:
                        continue
                    if isinstance(nested_value, (bool, int, float)):
                        nested[nested_name] = nested_value
                    elif isinstance(nested_value, str):
                        text = self._truncate_prompt_text(nested_value, max_len=64)
                        if text:
                            nested[nested_name] = text
                if nested:
                    compact[field] = nested
        return self._prune_prompt_value(compact) or {}

    def _role_defaults(self) -> dict[str, str]:
        defaults = {"A1": "intake_runner", "A2": "reliability_guard", "A3": "inspection_closer"}
        return {agent_id: defaults.get(agent_id, "flow_support") for agent_id in self.agent_ids}

    def _scripted_baseline_plan(self, observation: dict[str, Any], norms: dict[str, Any] | None = None) -> JobPlan:
        base_norms = dict(norms) if isinstance(norms, dict) else {}
        return self.scripted_baseline.propose_jobs(observation, StrategyState(), base_norms)

    def _limit_task_priority_targets(
        self,
        proposed: dict[str, float],
        base: dict[str, float],
        *,
        max_updates: int,
    ) -> dict[str, float]:
        if not isinstance(proposed, dict):
            return {}
        changed = [
            (task_family, float(value))
            for task_family, value in proposed.items()
            if task_family in self.allowed_task_priority_keys and abs(float(value) - float(base.get(task_family, 1.0))) > 1e-9
        ]
        changed.sort(key=lambda item: abs(float(item[1]) - float(base.get(item[0], 1.0))), reverse=True)
        return {task_family: value for task_family, value in changed[: max(1, int(max_updates or 1))]}

    def _limit_agent_priority_targets(
        self,
        updates: dict[str, dict[str, float]],
        base: dict[str, dict[str, float]],
        *,
        max_entries: int,
    ) -> dict[str, dict[str, float]]:
        if not isinstance(updates, dict):
            return {}
        ranked: list[tuple[str, str, float]] = []
        for agent_id, row in updates.items():
            if agent_id not in self.agent_ids or not isinstance(row, dict):
                continue
            for task_family, value in row.items():
                if task_family not in self.allowed_task_priority_keys:
                    continue
                base_value = float((base.get(agent_id, {}) if isinstance(base.get(agent_id, {}), dict) else {}).get(task_family, 1.0))
                ranked.append((agent_id, task_family, float(value), abs(float(value) - base_value)))
        ranked.sort(key=lambda item: item[3], reverse=True)
        trimmed: dict[str, dict[str, float]] = {}
        for agent_id, task_family, value, _delta in ranked[: max(1, int(max_entries or 1))]:
            trimmed.setdefault(agent_id, {})[task_family] = value
        return trimmed

    def _limit_mailbox_messages(self, mailbox: dict[str, list[dict[str, Any]]], *, max_messages: int) -> dict[str, list[dict[str, Any]]]:
        cleaned = self._sanitize_mailbox(mailbox)
        ranked: list[tuple[int, int, str, str, dict[str, Any]]] = []
        for agent_idx, agent_id in enumerate(self.agent_ids):
            for msg_idx, item in enumerate(cleaned.get(agent_id, [])):
                if not isinstance(item, dict):
                    continue
                task_family = str(item.get("task_family", "")).strip()
                ranked.append((-self._clamp_int(item.get("priority"), 1, 5, 1), agent_idx * 100 + msg_idx, agent_id, task_family, item))
        ranked.sort(key=lambda item: (item[0], item[1]))
        limited: dict[str, list[dict[str, Any]]] = {agent_id: [] for agent_id in self.agent_ids}
        selected: list[tuple[int, int, str, str, dict[str, Any]]] = []
        seen_families: set[str] = set()
        budget = max(0, int(max_messages or 0))
        for entry in ranked:
            if len(selected) >= budget:
                break
            _neg_pri, _order, _agent_id, task_family, _item = entry
            if task_family and task_family not in seen_families:
                selected.append(entry)
                seen_families.add(task_family)
        if len(selected) < budget:
            for entry in ranked:
                if len(selected) >= budget:
                    break
                if entry in selected:
                    continue
                selected.append(entry)
        for _neg_pri, _order, agent_id, _task_family, item in selected:
            limited.setdefault(agent_id, []).append(dict(item))
        return limited

    def _coalesce_worker_roles(self, src: Any) -> dict[str, str]:
        defaults = self._role_defaults()
        sanitized = self._sanitize_worker_roles(src)
        merged = {
            agent_id: sanitized.get(agent_id) or defaults.get(agent_id, "flow_support")
            for agent_id in self.agent_ids
        }
        return self._enforce_role_coverage(merged)

    def _apply_role_bias_defaults(
        self,
        agent_multipliers: dict[str, dict[str, float]],
        worker_roles: dict[str, str],
    ) -> dict[str, dict[str, float]]:
        rows = self._clone_agent_priority_multipliers(agent_multipliers)
        for agent_id in self.agent_ids:
            role = str(worker_roles.get(agent_id, "")).strip().lower()
            bucket = self._role_bucket(role)
            row = rows.setdefault(agent_id, {})
            if role == "inspection_closer" or bucket == "inspection":
                row["inspect_product"] = max(float(row.get("inspect_product", 1.0)), 1.45)
                row["unload_machine"] = max(float(row.get("unload_machine", 1.0)), 1.18)
            elif role == "battery_support":
                row["battery_swap"] = max(float(row.get("battery_swap", 1.0)), 1.18)
                row["battery_delivery_discharged"] = max(float(row.get("battery_delivery_discharged", 1.0)), 1.4)
                row["battery_delivery_low_battery"] = max(float(row.get("battery_delivery_low_battery", 1.0)), 1.28)
                row["repair_machine"] = max(float(row.get("repair_machine", 1.0)), 1.12)
                row["preventive_maintenance"] = max(float(row.get("preventive_maintenance", 1.0)), 1.08)
            elif role == "reliability_guard":
                row["repair_machine"] = max(float(row.get("repair_machine", 1.0)), 1.35)
                row["preventive_maintenance"] = max(float(row.get("preventive_maintenance", 1.0)), 1.25)
                row["battery_delivery_discharged"] = max(float(row.get("battery_delivery_discharged", 1.0)), 1.18)
            elif role == "intake_runner":
                row["material_supply"] = max(float(row.get("material_supply", 1.0)), 1.25)
                row["inter_station_transfer"] = max(float(row.get("inter_station_transfer", 1.0)), 1.18)
                row["unload_machine"] = max(float(row.get("unload_machine", 1.0)), 1.12)
            elif role == "flow_support" or bucket == "flow":
                row["material_supply"] = max(float(row.get("material_supply", 1.0)), 1.15)
                row["inter_station_transfer"] = max(float(row.get("inter_station_transfer", 1.0)), 1.2)
                row["unload_machine"] = max(float(row.get("unload_machine", 1.0)), 1.15)
        return rows

    def _blend_priority_weights(self, proposed: Any, base: dict[str, float]) -> dict[str, float]:
        sanitized = self._sanitize_task_priority_weights(proposed, dict(base))
        limited = self._limit_task_priority_targets(sanitized, base, max_updates=5)
        protected = {"battery_swap", "battery_delivery_low_battery", "battery_delivery_discharged", "repair_machine", "preventive_maintenance"}
        merged: dict[str, float] = {}
        for task_family, base_value in base.items():
            target = self._safe_float(limited.get(task_family, base_value), base_value)
            blended = float(base_value) + self.priority_blend_alpha * (target - float(base_value))
            if task_family in protected:
                blended = max(float(base_value), blended)
            merged[task_family] = round(self._clamp_float(blended, self.task_priority_weight_min, self.task_priority_weight_max, float(base_value)), 3)
        return merged

    def _blend_agent_priority_updates(
        self,
        base: dict[str, dict[str, float]],
        updates: Any,
    ) -> dict[str, dict[str, float]]:
        merged = self._clone_agent_priority_multipliers(base)
        sanitized = self._sanitize_agent_priority_profile_updates(updates)
        limited = self._limit_agent_priority_targets(sanitized, merged, max_entries=4)
        protected = {"battery_swap", "battery_delivery_low_battery", "battery_delivery_discharged", "repair_machine", "preventive_maintenance"}
        for agent_id, row in limited.items():
            current = merged.setdefault(agent_id, {})
            for task_family, target in row.items():
                base_value = float(current.get(task_family, 1.0))
                blended = base_value + self.agent_blend_alpha * (float(target) - base_value)
                clamped = self._clamp_float(blended, self.agent_priority_multiplier_min, self.agent_priority_multiplier_max, base_value)
                if task_family in protected:
                    clamped = max(base_value, clamped)
                current[task_family] = round(clamped, 3)
        return merged

    def _inspection_pressure(self, observation: dict[str, Any]) -> tuple[int, int]:
        queues = observation.get("queues", {}) if isinstance(observation.get("queues", {}), dict) else {}
        inspection = queues.get("inspection", {}) if isinstance(queues.get("inspection", {}), dict) else {}
        backlog = self._safe_int(inspection.get("inspection_input", observation.get("inspection_backlog", 0)), 0)
        pass_output = self._safe_int(inspection.get("inspection_pass_output", 0), 0)
        return backlog, pass_output

    def _closeout_state(self, observation: dict[str, Any]) -> dict[str, Any]:
        backlog, pass_output = self._inspection_pressure(observation)
        flow = observation.get("flow", {}) if isinstance(observation.get("flow", {}), dict) else {}
        time_ctx = observation.get("time", {}) if isinstance(observation.get("time", {}), dict) else {}
        accepted_products = self._safe_int(flow.get("products_completed_total", flow.get("finished_products", 0)), 0)
        inspection_completed = accepted_products + pass_output
        day_progress = self._safe_float(time_ctx.get("day_progress", 0.0), 0.0)
        days_remaining = self._safe_int(time_ctx.get("days_remaining", 0), 0)
        pressure = "high" if pass_output > 0 and (days_remaining <= 1 or day_progress >= 0.75) else "medium" if pass_output > 0 else "low"
        last_summary = self._latest_day_summary_memory()
        return {
            "inspection_backlog": backlog,
            "inspection_output_open_count": pass_output,
            "accepted_products": accepted_products,
            "inspection_completed": inspection_completed,
            "closure_gap": max(0, inspection_completed - accepted_products),
            "inspection_output_wait_avg": self._safe_float(last_summary.get("inspection_output_wait_avg", 0.0), 0.0),
            "late_horizon_closeout_pressure": pressure,
        }

    def _current_policy_focus_summary(self) -> str:
        snapshot = self._compact_policy_snapshot(include_mailbox=False)
        top_tasks = [str(item.get("task_family", "")).strip() for item in list(snapshot.get("top_task_priority_weights", []))[:3] if isinstance(item, dict)]
        roles = [f"{agent_id}:{role}" for agent_id, role in dict(snapshot.get("worker_roles", {})).items() if str(role).strip()]
        operating_focus = str(snapshot.get("operating_focus", "")).strip()
        late_horizon_mode = str(snapshot.get("late_horizon_mode", "")).strip()
        prevention_targets = [str(item).strip() for item in list(snapshot.get("prevention_targets", []))[:2] if str(item).strip()]
        pieces: list[str] = []
        if operating_focus:
            pieces.append(f"operating_focus={operating_focus}")
        if late_horizon_mode:
            pieces.append(f"late_horizon_mode={late_horizon_mode}")
        if prevention_targets:
            pieces.append("prevent=" + ",".join(prevention_targets))
        if top_tasks:
            pieces.append("focus=" + ",".join(top_tasks))
        if roles:
            pieces.append("roles=" + ",".join(roles[:3]))
        return "; ".join(pieces)

    def _apply_inspection_close_guardrails(self, plan: JobPlan, observation: dict[str, Any]) -> JobPlan:
        backlog, pass_output = self._inspection_pressure(observation)
        day = self._safe_int(observation.get("day", 0), 0)
        if day >= 2:
            plan.task_priority_weights["inspect_product"] = round(max(float(plan.task_priority_weights.get("inspect_product", 1.0)), 1.12), 3)
        if backlog > 0:
            inspect_floor = 1.12 if backlog == 1 else 1.18 if backlog <= 3 else 1.25
            plan.task_priority_weights["inspect_product"] = round(max(float(plan.task_priority_weights.get("inspect_product", 1.0)), inspect_floor), 3)
            plan.agent_priority_multipliers.setdefault("A3", {})
            plan.agent_priority_multipliers["A3"]["inspect_product"] = round(max(float(plan.agent_priority_multipliers["A3"].get("inspect_product", 1.0)), 1.58 if backlog >= 2 else 1.5), 3)
            if backlog >= 1:
                plan.agent_priority_multipliers.setdefault("A1", {})
                plan.agent_priority_multipliers["A1"]["inspect_product"] = round(max(float(plan.agent_priority_multipliers["A1"].get("inspect_product", 1.0)), 1.12 if day >= 4 else 1.08), 3)
        if pass_output > 0:
            unload_floor = 1.12 if pass_output == 1 else 1.2 if pass_output <= 3 else 1.28
            plan.task_priority_weights["unload_machine"] = round(max(float(plan.task_priority_weights.get("unload_machine", 1.0)), unload_floor), 3)
            for agent_id, floor in (("A3", 1.32 if day >= 4 else 1.28), ("A1", 1.2 if day >= 4 else 1.15)):
                plan.agent_priority_multipliers.setdefault(agent_id, {})
                plan.agent_priority_multipliers[agent_id]["unload_machine"] = round(max(float(plan.agent_priority_multipliers[agent_id].get("unload_machine", 1.0)), floor), 3)
        return plan

    def _mailbox_has_task(self, mailbox: dict[str, list[dict[str, Any]]], agent_id: str, task_family: str) -> bool:
        items = mailbox.get(agent_id, []) if isinstance(mailbox, dict) else []
        if not isinstance(items, list):
            return False
        normalized = self._normalize_task_family_alias(task_family)
        return any(
            isinstance(item, dict)
            and self._normalize_task_family_alias(item.get("task_family", "")) == normalized
            and str(item.get("message_type", "")).strip().lower() in {"assist_request", "focus_window"}
            for item in items
        )

    def _ensure_mailbox_assist(
        self,
        mailbox: dict[str, list[dict[str, Any]]],
        *,
        agent_id: str,
        task_family: str,
        body: str,
        priority: int = 1,
        message_type: str = "assist_request",
        remaining_uses: int = 1,
    ) -> None:
        if agent_id not in self.agent_ids:
            return
        normalized = self._normalize_task_family_alias(task_family)
        if not normalized or normalized not in self.allowed_task_priority_keys:
            return
        normalized_message_type = str(message_type or "assist_request").strip().lower() or "assist_request"
        if normalized_message_type not in {"assist_request", "focus_window"}:
            normalized_message_type = "assist_request"
        mailbox.setdefault(agent_id, [])
        if self._mailbox_has_task(mailbox, agent_id, normalized):
            return
        mailbox[agent_id].append(
            {
                "message_id": f"MSG-{agent_id}-{len(mailbox[agent_id]) + 1}",
                "from_agent": self.manager_agent_id,
                "to_agent": agent_id,
                "message_type": normalized_message_type,
                "task_family": normalized,
                "target_type": "none",
                "target_id": "",
                "target_station": None,
                "priority": self._clamp_int(priority, 1, 5, 1),
                "remaining_uses": self._clamp_int(remaining_uses, 1, 3, 2 if normalized_message_type == "focus_window" else 1),
                "body": self._truncate_prompt_text(body, max_len=180),
            }
        )

    def _apply_mailbox_guardrails(self, plan: JobPlan, observation: dict[str, Any]) -> JobPlan:
        mailbox = self._sanitize_mailbox(getattr(plan, "mailbox", {}))
        backlog, pass_output = self._inspection_pressure(observation)
        day = self._safe_int(observation.get("day", 0), 0)
        signals = self._worker_local_signals(observation)
        low_battery_agents = self._safe_int(signals.get("low_battery_agents", 0), 0)
        discharged_agents = self._safe_int(signals.get("discharged_agents", 0), 0)

        if backlog > 0:
            self._ensure_mailbox_assist(
                mailbox,
                agent_id="A3",
                task_family="inspect_product",
                body="Prioritize inspection backlog items until the queue clears.",
                message_type="focus_window" if day >= 4 else "assist_request",
                remaining_uses=2 if day >= 4 else 1,
            )
            if day >= 4:
                self._ensure_mailbox_assist(
                    mailbox,
                    agent_id="A2",
                    task_family="inspect_product",
                    body="Assist inspection closure to prevent end-of-horizon backlog carryover.",
                    message_type="focus_window",
                    remaining_uses=2,
                )
        if pass_output > 0 and day >= 4:
            self._ensure_mailbox_assist(
                mailbox,
                agent_id="A3",
                task_family="unload_machine",
                body="Prioritize unload tasks that convert inspection pass output into accepted products.",
                message_type="focus_window",
                remaining_uses=2,
            )
        if (low_battery_agents > 0 or discharged_agents > 0) and day >= 4:
            self._ensure_mailbox_assist(
                mailbox,
                agent_id="A2",
                task_family="battery_swap",
                body="Stabilize battery margin first when final-day battery risk is active.",
                message_type="focus_window",
                remaining_uses=2,
            )
        plan.mailbox = mailbox
        return plan

    def _strategy_packet(
        self,
        observation: dict[str, Any],
        *,
        refresh_context: dict[str, Any] | None = None,
        norms: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        planner_view = self._planner_observation_view(observation)
        time_view = planner_view.get("time", {}) if isinstance(planner_view.get("time", {}), dict) else {}
        signals = self._worker_local_signals(observation)
        closeout = self._closeout_state(observation)
        previous_day_review = self._latest_day_review_memory()
        packet = {
            "objective": {"global_goal": "Maximize accepted finished products over the remaining horizon."},
            "time_context": {
                "day": int(time_view.get("day", observation.get("day", 0)) or 0),
                "days_remaining": int(time_view.get("days_remaining", 0) or 0),
                "horizon_remaining_min": float(time_view.get("horizon_remaining_min", 0.0) or 0.0),
            },
            "operating_state": {
                "inspection_backlog": int(signals.get("inspection_backlog", 0) or 0),
                "station1_output_buffer": int(signals.get("station1_output_buffer", 0) or 0),
                "station2_output_buffer": int(signals.get("station2_output_buffer", 0) or 0),
                "products_completed": int(signals.get("products_completed", 0) or 0),
                "broken_machines": int(signals.get("broken_machines", 0) or 0),
                "low_battery_agents": int(signals.get("low_battery_agents", 0) or 0),
                "discharged_agents": int(signals.get("discharged_agents", 0) or 0),
                "closure_gap": int(closeout.get("closure_gap", 0) or 0),
                "inspection_output_open_count": int(closeout.get("inspection_output_open_count", 0) or 0),
                "inspection_output_wait_avg": float(closeout.get("inspection_output_wait_avg", 0.0) or 0.0),
                "late_horizon_closeout_pressure": str(closeout.get("late_horizon_closeout_pressure", "low")),
            },
            "opportunities": self._compact_opportunity_focus(observation, limit=8),
            "current_policy": self._compact_policy_snapshot(include_mailbox=True),
            "current_policy_focus_summary": self._current_policy_focus_summary(),
            "previous_day_review": previous_day_review,
            "norm_targets": dict(norms if isinstance(norms, dict) else {}),
            "refresh_context": self._compact_event_details(refresh_context or {}, limit=5),
        }
        if self._knowledge_enabled():
            packet["knowledge"] = self._knowledge_prompt_blob()
        return self._prune_prompt_value(packet) or {}

    def _reviewer_packet(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        day = self._safe_int(day_summary.get("day", 0), 0)
        current_policy = self._compact_policy_snapshot(include_mailbox=True)
        daily_targets = dict((getattr(self.current_job_plan, "incident_guidance", {}) or {}).get("daily_targets", {}))
        products_today = self._safe_int(day_summary.get("products", 0), 0)
        closeout_gap_end = max(0, self._safe_int(day_summary.get("inspection_passes", 0), 0) - products_today)
        discharged_today = self._safe_int(day_summary.get("agent_discharged_count", 0), 0)
        target_achievement: dict[str, dict[str, Any]] = {}
        if "min_accepted_products_today" in daily_targets:
            min_target = int(daily_targets.get("min_accepted_products_today", 0) or 0)
            target_achievement["min_accepted_products_today"] = {
                "target": min_target,
                "actual": products_today,
                "achieved": products_today >= min_target,
            }
        if "max_closeout_gap_end" in daily_targets:
            target_achievement["max_closeout_gap_end"] = {
                "target": int(daily_targets.get("max_closeout_gap_end", 0) or 0),
                "actual": closeout_gap_end,
                "achieved": closeout_gap_end <= int(daily_targets.get("max_closeout_gap_end", 0) or 0),
            }
        if "max_discharged_workers" in daily_targets:
            target_achievement["max_discharged_workers"] = {
                "target": int(daily_targets.get("max_discharged_workers", 0) or 0),
                "actual": discharged_today,
                "achieved": discharged_today <= int(daily_targets.get("max_discharged_workers", 0) or 0),
            }
        packet = {
            "objective": {"global_goal": "Diagnose what tomorrow's strategist should change so throughput and variance improve without a midday LLM loop."},
            "time_context": {"day": day, "days_remaining": max(0, self._simulation_total_days() - day)},
            "day_summary": self._prune_prompt_value(day_summary) or {},
            "compiled_policy_snapshot": current_policy,
            "daily_target_achievement": target_achievement,
            "norm_targets": dict(norms if isinstance(norms, dict) else {}),
            "previous_day_review": self._latest_day_review_memory(),
        }
        return self._prune_prompt_value(packet) or {}

    def _build_priority_policy_plan(
        self,
        payload: dict[str, Any],
        fallback: JobPlan,
        observation: dict[str, Any],
        *,
        mailbox_key: str = "mailbox_seed",
    ) -> JobPlan:
        plan = JobPlan(
            task_priority_weights=self._blend_priority_weights(payload.get("task_priority_weights"), dict(fallback.task_priority_weights)),
            quotas=self._sanitize_quotas(payload.get("quotas"), dict(fallback.quotas)),
            rationale=self._truncate_prompt_text(payload.get("summary", payload.get("rationale", fallback.rationale or "priority policy updated")), max_len=240),
            agent_priority_multipliers=self._clone_agent_priority_multipliers(fallback.agent_priority_multipliers),
        )
        plan.agent_roles = self._coalesce_worker_roles(payload.get("worker_roles", payload.get("agent_roles", {})))
        plan.agent_priority_multipliers = self._apply_role_bias_defaults(plan.agent_priority_multipliers, plan.agent_roles)
        plan.agent_priority_multipliers = self._blend_agent_priority_updates(
            plan.agent_priority_multipliers,
            payload.get("agent_priority_multipliers", payload.get("agent_priority_updates", {})),
        )
        plan.mailbox = self._limit_mailbox_messages(
            self._sanitize_mailbox(payload.get(mailbox_key, payload.get("mailbox", {}))),
            max_messages=3,
        )
        plan.incident_strategy = {}
        plan.incident_guidance = {
            "operating_focus": self._normalize_operating_focus(payload.get("operating_focus", "")),
            "late_horizon_mode": self._normalize_late_horizon_mode(payload.get("late_horizon_mode", "")),
            "role_plan": self._compact_event_details(payload.get("role_plan", {}), limit=4),
            "support_plan": self._compact_event_details(payload.get("support_plan", {}), limit=4),
            "prevention_targets": self._sanitize_prevention_targets(payload.get("prevention_targets", []), limit=2),
            "daily_targets": self._sanitize_daily_targets(payload.get("daily_targets", {})),
            "review_applied": self._compact_event_details(payload.get("previous_day_review", {}), limit=6),
        }
        plan.manager_summary = self._truncate_prompt_text(payload.get("summary", payload.get("manager_summary", "")), max_len=300)
        focus_tasks = self._top_task_keys(plan.task_priority_weights)
        plan.reason_trace = list(payload.get("reason_trace", [])) if isinstance(payload.get("reason_trace", []), list) else self._default_reason_trace(summary=plan.manager_summary or plan.rationale, focus_tasks=focus_tasks)
        plan.plan_revision = max(self._safe_int(payload.get("plan_revision", 0), 0), self._next_plan_revision())
        plan.commitments = {aid: [] for aid in self.agent_ids}
        plan.personal_queues = {aid: [] for aid in self.agent_ids}
        plan.incident_work_orders = {aid: [] for aid in self.agent_ids}
        plan = self._apply_inspection_close_guardrails(plan, observation)
        plan = self._apply_mailbox_guardrails(plan, observation)
        plan.ensure_runtime_context(self.agent_ids)
        return plan

    def _apply_weight_floor(self, weights: dict[str, float], task_family: str, floor: float) -> None:
        if task_family not in self.allowed_task_priority_keys:
            return
        weights[task_family] = round(max(float(weights.get(task_family, 1.0)), float(floor)), 3)

    def _apply_agent_floor(self, agent_targets: dict[str, dict[str, float]], agent_id: str, task_family: str, floor: float) -> None:
        if agent_id not in self.agent_ids or task_family not in self.allowed_task_priority_keys:
            return
        row = agent_targets.setdefault(agent_id, {})
        row[task_family] = round(max(float(row.get(task_family, 1.0)), float(floor)), 3)

    def _focus_bundle_targets(self, focus: str) -> dict[str, float]:
        bundles = {
            "flow": {"material_supply": 1.25, "inter_station_transfer": 1.22, "unload_machine": 1.15},
            "reliability": {"repair_machine": 1.35, "preventive_maintenance": 1.25, "unload_machine": 1.12},
            "closeout": {"inspect_product": 1.35, "unload_machine": 1.22, "inter_station_transfer": 1.15},
            "battery": {"battery_swap": 1.55, "battery_delivery_low_battery": 1.25, "battery_delivery_discharged": 1.65},
        }
        return dict(bundles.get(focus, {}))

    def _prevention_bundle_targets(self, target: str) -> dict[str, float]:
        bundles = {
            "closeout_gap": {"inspect_product": 1.38, "unload_machine": 1.25, "inter_station_transfer": 1.16},
            "battery_instability": {"battery_swap": 1.6, "battery_delivery_low_battery": 1.28, "battery_delivery_discharged": 1.7},
            "reliability_instability": {"repair_machine": 1.4, "preventive_maintenance": 1.25, "unload_machine": 1.14},
            "flow_blockage": {"material_supply": 1.22, "inter_station_transfer": 1.25, "unload_machine": 1.18},
            "s2_underfeed": {"material_supply": 1.2, "inter_station_transfer": 1.24, "unload_machine": 1.16},
        }
        return dict(bundles.get(target, {}))

    def _reviewer_defaults_for_day(self, observation: dict[str, Any]) -> dict[str, int]:
        day = self._safe_int(observation.get("day", 0), 0)
        backlog, pass_output = self._inspection_pressure(observation)
        defaults = {
            "min_accepted_products_today": 4 if day >= 4 else 3 if day >= 2 else 2,
            "max_closeout_gap_end": 0 if day >= 4 else 1,
            "max_discharged_workers": 0,
        }
        if backlog <= 0 and pass_output <= 0 and day < 4:
            defaults["max_closeout_gap_end"] = 1
        return defaults

    def _compile_strategy_directive_payload(
        self,
        directive: dict[str, Any],
        fallback: JobPlan,
        observation: dict[str, Any],
        *,
        previous_review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous_review = previous_review if isinstance(previous_review, dict) else {}
        closeout = self._closeout_state(observation)
        signals = self._worker_local_signals(observation)
        default_roles = self._role_defaults()
        roles = self._coalesce_worker_roles(
            directive.get("worker_roles", previous_review.get("role_change_advice", default_roles))
        )
        operating_focus = self._normalize_operating_focus(directive.get("operating_focus", "")) or (
            "closeout"
            if "closeout_gap" in self._sanitize_prevention_targets(previous_review.get("recommended_prevention_targets", []), limit=2)
            else "battery"
            if int(signals.get("low_battery_agents", 0) or 0) > 0 or int(signals.get("discharged_agents", 0) or 0) > 0
            else "reliability"
            if int(signals.get("broken_machines", 0) or 0) > 0
            else "flow"
        )
        late_horizon_mode = self._normalize_late_horizon_mode(directive.get("late_horizon_mode", ""))
        if not late_horizon_mode:
            late_horizon_mode = "closeout_drive" if self._safe_int(observation.get("day", 0), 0) >= max(1, self._simulation_total_days() - 1) else "normal"
        prevention_targets = self._sanitize_prevention_targets(
            directive.get("prevention_targets", previous_review.get("recommended_prevention_targets", [])),
            limit=2,
        )
        daily_targets = self._reviewer_defaults_for_day(observation)
        daily_targets.update(self._sanitize_daily_targets(directive.get("daily_targets", {})))
        support_plan = self._sanitize_support_plan(
            directive.get("support_plan", {}),
            worker_roles=roles,
            operating_focus=operating_focus,
            prevention_targets=prevention_targets,
            previous_review=previous_review,
        )
        day = self._safe_int(observation.get("day", 0), 0)
        backlog, pass_output = self._inspection_pressure(observation)
        if (
            day <= 2
            and operating_focus == "flow"
            and late_horizon_mode == "normal"
            and "closeout_gap" not in prevention_targets
            and int(closeout.get("closure_gap", 0) or 0) <= 1
            and pass_output <= 0
        ):
            support_plan["primary_support_pair"] = "A1->A2"
            support_plan["support_intent"] = (
                "reliability_cover"
                if "reliability_instability" in prevention_targets or "s2_underfeed" in prevention_targets
                else "flow_cover"
            )
        roles = self._stabilize_compiled_roles(
            roles,
            prevention_targets=prevention_targets,
            signals=signals,
            support_intent=str(support_plan.get("support_intent", "")).strip(),
        )
        weights = dict(fallback.task_priority_weights)
        for task_family, floor in self._focus_bundle_targets(operating_focus).items():
            self._apply_weight_floor(weights, task_family, floor)
        for target in prevention_targets:
            for task_family, floor in self._prevention_bundle_targets(target).items():
                self._apply_weight_floor(weights, task_family, floor)
        if late_horizon_mode == "closeout_drive":
            for task_family, floor in {"inspect_product": 1.42, "unload_machine": 1.28, "inter_station_transfer": 1.18}.items():
                self._apply_weight_floor(weights, task_family, floor)
        elif late_horizon_mode == "reliability_guarded_closeout":
            for task_family, floor in {"inspect_product": 1.35, "unload_machine": 1.22, "repair_machine": 1.25, "preventive_maintenance": 1.18}.items():
                self._apply_weight_floor(weights, task_family, floor)
        elif late_horizon_mode == "battery_guarded_closeout":
            for task_family, floor in {"inspect_product": 1.32, "unload_machine": 1.2, "battery_swap": 1.55, "battery_delivery_discharged": 1.65}.items():
                self._apply_weight_floor(weights, task_family, floor)
        if int(daily_targets.get("max_discharged_workers", 0)) <= 0 and (
            int(signals.get("low_battery_agents", 0) or 0) > 0 or int(signals.get("discharged_agents", 0) or 0) > 0
        ):
            for task_family, floor in {"battery_swap": 1.55, "battery_delivery_low_battery": 1.25, "battery_delivery_discharged": 1.6}.items():
                self._apply_weight_floor(weights, task_family, floor)
        if (
            day >= 4
            or pass_output > 0
            or int(closeout.get("closure_gap", 0) or 0) >= 2
            or ("closeout_gap" in prevention_targets and day >= 3)
        ):
            for task_family, floor in {"inspect_product": 1.35, "unload_machine": 1.22}.items():
                self._apply_weight_floor(weights, task_family, floor)

        agent_targets = self._apply_role_bias_defaults(
            self._clone_agent_priority_multipliers(fallback.agent_priority_multipliers),
            roles,
        )
        if "closeout_gap" in prevention_targets or operating_focus == "closeout":
            self._apply_agent_floor(agent_targets, "A3", "inspect_product", 1.5)
            self._apply_agent_floor(agent_targets, "A3", "unload_machine", 1.28)
            self._apply_agent_floor(agent_targets, "A1", "unload_machine", 1.16)
            self._apply_agent_floor(agent_targets, "A1", "inter_station_transfer", 1.18)
            self._apply_agent_floor(agent_targets, "A2", "repair_machine", 1.35)
        if "battery_instability" in prevention_targets or operating_focus == "battery":
            self._apply_agent_floor(agent_targets, "A2", "battery_swap", 1.22)
            self._apply_agent_floor(agent_targets, "A2", "battery_delivery_discharged", 1.45)
            self._apply_agent_floor(agent_targets, "A1", "battery_delivery_low_battery", 1.18)
        if "reliability_instability" in prevention_targets or operating_focus == "reliability":
            self._apply_agent_floor(agent_targets, "A2", "repair_machine", 1.4)
            self._apply_agent_floor(agent_targets, "A2", "preventive_maintenance", 1.28)
            self._apply_agent_floor(agent_targets, "A1", "unload_machine", 1.12)
        if "flow_blockage" in prevention_targets or operating_focus == "flow":
            self._apply_agent_floor(agent_targets, "A1", "material_supply", 1.22)
            self._apply_agent_floor(agent_targets, "A1", "inter_station_transfer", 1.2)
            self._apply_agent_floor(agent_targets, "A2", "unload_machine", 1.16)

        mailbox: dict[str, list[dict[str, Any]]] = {aid: [] for aid in self.agent_ids}
        support_source, support_target = self._parse_support_pair(support_plan.get("primary_support_pair", ""))
        support_intent = str(support_plan.get("support_intent", "")).strip()
        day = self._safe_int(observation.get("day", 0), 0)
        backlog, pass_output = self._inspection_pressure(observation)
        low_battery_agents = int(signals.get("low_battery_agents", 0) or 0)
        discharged_agents = int(signals.get("discharged_agents", 0) or 0)
        reliability_risk = bool("reliability_instability" in prevention_targets or int(signals.get("broken_machines", 0) or 0) > 0)
        battery_risk = bool("battery_instability" in prevention_targets or discharged_agents > 0)
        if support_intent == "closeout_support" and support_source and support_target:
            for task_family, floor in {"inspect_product": 1.22, "unload_machine": 1.14, "inter_station_transfer": 1.12}.items():
                self._apply_weight_floor(weights, task_family, floor)
            self._apply_agent_floor(agent_targets, support_target, "inspect_product", 1.5)
            self._apply_agent_floor(agent_targets, support_target, "unload_machine", 1.28)
            self._apply_agent_floor(agent_targets, support_source, "unload_machine", 1.16)
            self._apply_agent_floor(agent_targets, support_source, "inter_station_transfer", 1.18)
            self._ensure_mailbox_assist(
                mailbox,
                agent_id=support_target,
                task_family="inspect_product",
                body="Hold inspection close-out until accepted-product conversion is stable.",
                message_type="focus_window" if day >= 2 or backlog > 0 else "assist_request",
                remaining_uses=2 if day >= 2 or backlog > 0 else 1,
            )
            self._ensure_mailbox_assist(
                mailbox,
                agent_id=support_source,
                task_family="unload_machine" if pass_output > 0 or day >= 3 else "inter_station_transfer",
                body="Support close-out by clearing downstream conversion work before generic flow tasks.",
                message_type="focus_window" if day >= 3 or pass_output > 0 else "assist_request",
                remaining_uses=2 if day >= 3 or pass_output > 0 else 1,
            )
            if day >= 4 or int(closeout.get("closure_gap", 0) or 0) >= 2:
                self._ensure_mailbox_assist(
                    mailbox,
                    agent_id="A2",
                    task_family="inspect_product",
                    body="Assist inspection closure only after reliability-critical actions are covered.",
                    message_type="focus_window",
                    remaining_uses=2,
                )
        elif support_intent == "reliability_cover" and support_source and support_target:
            self._apply_agent_floor(agent_targets, support_target, "repair_machine", 1.38)
            self._apply_agent_floor(agent_targets, support_target, "preventive_maintenance", 1.24)
            self._ensure_mailbox_assist(
                mailbox,
                agent_id=support_target,
                task_family="repair_machine" if int(signals.get("broken_machines", 0) or 0) > 0 else "preventive_maintenance",
                body="Hold reliability coverage until machine risk is stable.",
                message_type="focus_window" if int(signals.get("broken_machines", 0) or 0) > 0 else "assist_request",
                remaining_uses=2 if int(signals.get("broken_machines", 0) or 0) > 0 else 1,
            )
        elif support_intent == "battery_cover" and support_source:
            helper_family = "battery_delivery_discharged" if int(signals.get("discharged_agents", 0) or 0) > 0 else "battery_delivery_low_battery" if int(signals.get("low_battery_agents", 0) or 0) > 0 else "battery_swap"
            self._apply_agent_floor(agent_targets, support_source, helper_family, 1.3)
            self._ensure_mailbox_assist(
                mailbox,
                agent_id=support_source,
                task_family=helper_family,
                body="Preserve battery margin before non-critical flow work.",
                message_type="focus_window" if day >= 4 else "assist_request",
                remaining_uses=2 if day >= 4 else 1,
            )
        elif support_intent == "flow_cover" and support_source:
            self._apply_agent_floor(agent_targets, support_source, "material_supply", 1.18)
            self._apply_agent_floor(agent_targets, support_source, "inter_station_transfer", 1.2)
            self._ensure_mailbox_assist(
                mailbox,
                agent_id=support_source,
                task_family="inter_station_transfer" if int(signals.get("station2_output_buffer", 0) or 0) > 0 else "material_supply",
                body="Cover downstream flow first when blockage or underfeed risk is active.",
                message_type="assist_request",
                remaining_uses=1,
            )
        if (
            day >= 3
            and roles.get("A3") == "inspection_closer"
            and (low_battery_agents > 0 or discharged_agents > 0 or "battery_instability" in prevention_targets)
        ):
            battery_helper_family = (
                "battery_delivery_discharged"
                if discharged_agents > 0
                else "battery_delivery_low_battery"
                if low_battery_agents > 0
                else "battery_swap"
            )
            for task_family, floor in {
                "battery_swap": 1.18,
                "battery_delivery_low_battery": 1.2,
                "battery_delivery_discharged": 1.28,
            }.items():
                self._apply_weight_floor(weights, task_family, floor)
            self._apply_agent_floor(agent_targets, "A2", "battery_swap", 1.18)
            self._apply_agent_floor(agent_targets, "A2", "battery_delivery_low_battery", 1.24)
            self._apply_agent_floor(agent_targets, "A2", "battery_delivery_discharged", 1.42)
            self._ensure_mailbox_assist(
                mailbox,
                agent_id="A2",
                task_family=battery_helper_family,
                body="Protect the inspection closer battery margin before avoidable discharge stalls downstream close-out.",
                message_type="focus_window" if day >= 4 or discharged_agents > 0 else "assist_request",
                remaining_uses=2 if day >= 4 or discharged_agents > 0 else 1,
            )

        summary = self._truncate_prompt_text(
            directive.get("summary", ""),
            max_len=220,
        ) or f"Intent-only shift policy compiled for {operating_focus} with {late_horizon_mode}."
        return {
            "summary": summary,
            "worker_roles": roles,
            "operating_focus": operating_focus,
            "late_horizon_mode": late_horizon_mode,
            "role_plan": directive.get("role_plan", {}),
            "support_plan": support_plan,
            "prevention_targets": prevention_targets,
            "daily_targets": daily_targets,
            "task_priority_weights": weights,
            "agent_priority_multipliers": agent_targets,
            "mailbox_seed": mailbox,
            "plan_revision": max(self._safe_int(directive.get("plan_revision", 0), 0), self._next_plan_revision()),
            "previous_day_review": previous_review,
        }

    def _deterministic_shift_fallback(self, observation: dict[str, Any], norms: dict[str, Any] | None = None) -> dict[str, Any]:
        signals = self._worker_local_signals(observation)
        closeout = self._closeout_state(observation)
        day = self._safe_int(observation.get("day", 0), 0)
        worker_roles = self._role_defaults()
        operating_focus = "flow"
        late_horizon_mode = "normal"
        prevention_targets: list[str] = []
        if int(signals.get("broken_machines", 0) or 0) >= 1:
            operating_focus = "reliability"
            prevention_targets.append("reliability_instability")
        elif int(signals.get("low_battery_agents", 0) or 0) > 0 or int(signals.get("discharged_agents", 0) or 0) > 0:
            operating_focus = "battery"
            prevention_targets.append("battery_instability")
        elif int(signals.get("inspection_backlog", 0) or 0) >= 2:
            operating_focus = "closeout"
            prevention_targets.append("closeout_gap")
        elif int(signals.get("station1_output_buffer", 0) or 0) > 0 or int(signals.get("station2_output_buffer", 0) or 0) > 0:
            prevention_targets.append("flow_blockage")
        if day >= max(1, self._simulation_total_days() - 1) and int(signals.get("inspection_backlog", 0) or 0) > 0:
            late_horizon_mode = "closeout_drive"
        return {
            "summary": "Deterministic strategist fallback generated from current operating state.",
            "worker_roles": worker_roles,
            "operating_focus": operating_focus,
            "late_horizon_mode": late_horizon_mode,
            "role_plan": {aid: {"role": role, "reason": f"default_{role}"} for aid, role in worker_roles.items()},
            "support_plan": {
                "primary_support_pair": "A1->A3" if late_horizon_mode == "closeout_drive" or operating_focus == "closeout" else "A1->A2",
                "support_intent": "closeout_support" if late_horizon_mode == "closeout_drive" or operating_focus == "closeout" else "battery_cover" if operating_focus == "battery" else "reliability_cover" if operating_focus == "reliability" else "flow_cover",
                "reason": "fallback_pairing",
            },
            "prevention_targets": prevention_targets[:2],
            "daily_targets": self._reviewer_defaults_for_day(observation),
            "plan_revision": self._next_plan_revision(),
        }

    def _compile_day_summary(self, day_summary: dict[str, Any]) -> dict[str, Any]:
        day = self._safe_int(day_summary.get("day", 0), 0)
        products = self._safe_int(day_summary.get("products", 0), 0)
        backlog = self._safe_int(day_summary.get("inspection_backlog_end", 0), 0)
        breakdowns = self._safe_int(day_summary.get("machine_breakdowns", 0), 0)
        discharged = self._safe_int(day_summary.get("agent_discharged_count", 0), 0)
        inspection_passes = self._safe_int(day_summary.get("inspection_passes", 0), 0)
        station1_buffer_end = self._safe_int(day_summary.get("station1_output_buffer_end", 0), 0)
        station2_buffer_end = self._safe_int(day_summary.get("station2_output_buffer_end", 0), 0)
        station2_completions = self._safe_int(day_summary.get("station2_completions", 0), 0)
        queue_waits = day_summary.get("buffer_wait_avg_min_by_queue", {}) if isinstance(day_summary.get("buffer_wait_avg_min_by_queue", {}), dict) else {}
        inspection_output_wait_avg = self._safe_float(queue_waits.get("inspection_output", 0.0), 0.0)
        inspection_output_open = max(0, inspection_passes - products)
        improved: list[str] = []
        worsened: list[str] = []
        risks: list[str] = []
        biases: list[str] = []
        critique_hints: list[str] = []
        if products > 0:
            improved.append(f"Produced {products} accepted products today.")
        else:
            worsened.append("No accepted products closed today.")
        if backlog > 0:
            risks.append(f"Inspection backlog ended at {backlog}.")
            biases.append("inspect_product")
            critique_hints.append("Start the next day with stronger inspection closure focus.")
        if inspection_output_open > 0:
            worsened.append(f"{inspection_output_open} inspection-pass items did not close into accepted products.")
            risks.append(f"Open close-out gap={inspection_output_open}.")
            biases.extend(["inspect_product", "unload_machine"])
            critique_hints.append("Convert inspection pass output into accepted products earlier in the day.")
            if day >= max(1, self._simulation_total_days() - 1):
                critique_hints.append("On late-horizon days, spend mailbox assists on inspect_product or unload_machine support before generic material supply.")
        if breakdowns > 0:
            risks.append(f"Machine breakdowns={breakdowns}.")
            biases.extend(["repair_machine", "preventive_maintenance"])
            critique_hints.append("Keep one worker oriented toward reliability coverage when breakdowns recur.")
        if discharged > 0:
            risks.append(f"Discharged workers={discharged}.")
            biases.extend(["battery_swap", "battery_delivery_discharged"])
            critique_hints.append("Preserve more battery margin before late-horizon close-out pressure spikes.")
        if station1_buffer_end > 0 or station2_buffer_end > 0:
            risks.append(f"Output buffers remained open at day end (S1={station1_buffer_end}, S2={station2_buffer_end}).")
            biases.extend(["material_supply", "inter_station_transfer", "unload_machine"])
            critique_hints.append("Cover downstream flow earlier when buffers begin to accumulate.")
        if station2_completions <= 0 and station1_buffer_end > 0:
            critique_hints.append("Protect station-2 feed before generic upstream flow if downstream completions stall.")
        if not risks:
            improved.append("No major closing-risk signal remained at day end.")
        top_failure_modes: list[str] = []
        if inspection_output_open > 0:
            top_failure_modes.append("closeout_gap")
        if breakdowns > 0:
            top_failure_modes.append("reliability_instability")
        if discharged > 0:
            top_failure_modes.append("battery_instability")
        if station1_buffer_end > 0 or station2_buffer_end > 0:
            top_failure_modes.append("flow_blockage")
        if station2_completions <= 0 and station1_buffer_end > 0:
            top_failure_modes.append("s2_underfeed")
        return {
            "day": day,
            "what_improved": improved[:3],
            "what_worsened": worsened[:3],
            "carry_forward_risks": risks[:4],
            "priority_bias_candidates": list(dict.fromkeys([bias for bias in biases if bias in self.allowed_task_priority_keys]))[:5],
            "open_closeout_gap": inspection_output_open,
            "inspection_output_wait_avg": round(inspection_output_wait_avg, 3),
            "inspection_passes": inspection_passes,
            "top_failure_modes": list(dict.fromkeys(top_failure_modes))[:4],
            "top_recovery_actions": [],
            "current_policy_focus_summary": self._current_policy_focus_summary(),
            "policy_critique_hints": critique_hints[:3],
            "next_day_prevention_hints": critique_hints[:3],
        }

    def _sync_shift_workspace(self, *, observation: dict[str, Any], policy: dict[str, Any], request_packet: dict[str, Any]) -> None:
        day = self._safe_int(observation.get("day", (observation.get("time", {}) if isinstance(observation.get("time", {}), dict) else {}).get("day", 0)), 0)
        workspace = self._phase_workspace_for_call("manager_shift_strategist", {"phase": "manager_shift_strategist", "day": day})
        if workspace is None:
            return
        self._openclaw_write_json(workspace / "reports" / f"day_{day:02d}_shift_policy.json", policy)
        self._openclaw_write_json(workspace / "facts" / "current_shift_policy.json", policy)
        self._openclaw_write_json(workspace / "memory" / "episodic" / f"day_{day:02d}_shift_policy.json", {"request": request_packet, "policy": policy})
        self._openclaw_write_markdown(
            workspace / "memory" / "rolling_summary.md",
            "MANAGER_SHIFT_STRATEGIST Rolling Summary",
            [
                ("Run Scope", "This workspace stores intent-only day-start policy memory for the current run only."),
                ("Latest Shift Policy", policy),
                ("Previous Day Review", self._latest_day_review_memory()),
            ],
        )

    def _sync_reviewer_workspace(self, *, day: int, summary: dict[str, Any], review: dict[str, Any], request_packet: dict[str, Any]) -> None:
        workspace = self._phase_workspace_for_call("manager_daily_reviewer", {"phase": "manager_daily_reviewer", "day": max(1, day)})
        if workspace is None:
            return
        payload = {"request": request_packet, "day_summary": summary, "review": review}
        self._openclaw_write_json(workspace / "reports" / f"day_{day:02d}_reviewer_report.json", review)
        self._openclaw_write_json(workspace / "facts" / "current_day_summary.json", summary)
        self._openclaw_write_json(workspace / "facts" / "current_reviewer_report.json", review)
        self._openclaw_write_json(workspace / "memory" / "episodic" / f"day_{day:02d}_review_cycle.json", payload)
        self._openclaw_write_markdown(
            workspace / "memory" / "rolling_summary.md",
            "MANAGER_DAILY_REVIEWER Rolling Summary",
            [
                ("Run Scope", "This workspace stores day-end diagnostic review memory for the current run only."),
                ("Latest Day Summary", summary),
                ("Latest Reviewer Report", review),
            ],
        )

    def _sync_day_summary_workspace(self, summary: dict[str, Any]) -> None:
        day = self._safe_int(summary.get("day", 0), 0)
        workspace = self._phase_workspace_for_call("manager_daily_reviewer", {"phase": "manager_daily_reviewer", "day": max(1, day)})
        if workspace is None:
            return
        self._openclaw_write_json(workspace / "reports" / f"day_{day:02d}_day_summary.json", summary)
        self._openclaw_write_json(workspace / "facts" / "current_day_summary.json", summary)

    def _write_operating_artifacts(self) -> None:
        if self.run_output_root is None:
            return
        self.run_output_root.mkdir(parents=True, exist_ok=True)
        (self.run_output_root / "shift_policy_history.json").write_text(json.dumps(self.shift_policy_history, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (self.run_output_root / "day_summary_memory.json").write_text(json.dumps(self.day_summary_memory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (self.run_output_root / "day_review_memory.json").write_text(json.dumps(self.day_review_memory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _invoke_shift_strategist(self, observation: dict[str, Any], *, norms: dict[str, Any] | None = None, refresh_context: dict[str, Any] | None = None) -> dict[str, Any]:
        request_packet = self._strategy_packet(observation, refresh_context=refresh_context, norms=norms)
        day = self._safe_int((request_packet.get("time_context", {}) if isinstance(request_packet.get("time_context", {}), dict) else {}).get("day", observation.get("day", 0)), 0)
        strategist_transport = self._openclaw_transport_for_call("manager_shift_strategist")
        strategist_required_fields = {
            "summary": "str",
            "worker_roles": "dict[str, str]",
            "operating_focus": "str",
            "late_horizon_mode": "str",
            "role_plan": "dict[str, Any]",
            "support_plan": "dict[str, Any]",
            "prevention_targets": "list[str]",
            "daily_targets": "dict[str, int]",
            "plan_revision": "int",
        }
        strategist_instructions = [
            "You own the day-start operating intent only. The deterministic policy compiler will derive low-level weights and mailbox actions.",
            "Do not emit commitments, task_priority_weights, agent_priority_multipliers, or mailbox_seed.",
            "Use canonical worker roles only: intake_runner, reliability_guard, inspection_closer, battery_support, flow_support.",
            "Use at least two distinct worker roles across A1/A2/A3.",
            "Keep exactly one inspection_closer unless inspection backlog is severe enough to justify a second temporary closer.",
            "Prefer A1 as intake_runner or flow_support unless the request packet shows that support pressure is clearly dominant elsewhere.",
            "Use previous_day_review as diagnosis-only feedback. Translate it into prevention_targets and support_plan rather than repeating yesterday's facts.",
            "Choose at most two prevention_targets and at most three daily_targets.",
            "When close-out pressure is high, support_plan should usually pair a support worker into the inspection/closeout lane instead of generic flow support.",
        ]
        fallback = self._scripted_baseline_plan(observation, norms)
        previous_review = self._latest_day_review_memory()
        runtime_agent_id = self._phase_runtime_agent_id("manager_shift_strategist", {"phase": "manager_shift_strategist", "day": day})
        if strategist_transport == "native_local":
            system_prompt, prompt, required_keys = self._native_turn_prompts(
                agent_id=runtime_agent_id,
                phase="manager_shift_strategist",
                role_summary="You are MANAGER_SHIFT_STRATEGIST. Build a compact day-start operating intent for deterministic execution.",
                input_payload=request_packet,
                required_fields=strategist_required_fields,
                instructions=strategist_instructions,
                history_tag=f"day_{day:02d}_manager_shift_strategist",
            )
            self._assert_native_workspace_inputs_ready(runtime_agent_id, "manager_shift_strategist")
        else:
            system_prompt, prompt, required_keys = self._backend_direct_turn_prompts(
                phase="manager_shift_strategist",
                role_summary="You are MANAGER_SHIFT_STRATEGIST. Build a compact day-start operating intent for deterministic execution.",
                input_payload=request_packet,
                required_fields=strategist_required_fields,
                instructions=strategist_instructions,
            )
        try:
            directive = self._call_llm_json(prompt, system_prompt, call_name="manager_shift_strategist", context={"phase": "manager_shift_strategist", "day": day}, required_keys=required_keys)
        except RuntimeError:
            directive = self._deterministic_shift_fallback(observation, norms)
        compiled = self._compile_strategy_directive_payload(
            directive,
            fallback,
            observation,
            previous_review=previous_review,
        )
        plan = self._build_priority_policy_plan(compiled, fallback, observation, mailbox_key="mailbox_seed")
        payload = {
            "summary": plan.manager_summary or plan.rationale,
            "worker_roles": dict(plan.agent_roles),
            "operating_focus": str((plan.incident_guidance or {}).get("operating_focus", "")).strip(),
            "late_horizon_mode": str((plan.incident_guidance or {}).get("late_horizon_mode", "")).strip(),
            "role_plan": dict((plan.incident_guidance or {}).get("role_plan", {})) if isinstance((plan.incident_guidance or {}).get("role_plan", {}), dict) else {},
            "support_plan": dict((plan.incident_guidance or {}).get("support_plan", {})) if isinstance((plan.incident_guidance or {}).get("support_plan", {}), dict) else {},
            "prevention_targets": list((plan.incident_guidance or {}).get("prevention_targets", [])),
            "daily_targets": dict((plan.incident_guidance or {}).get("daily_targets", {})) if isinstance((plan.incident_guidance or {}).get("daily_targets", {}), dict) else {},
            "task_priority_weights": dict(plan.task_priority_weights),
            "agent_priority_multipliers": {aid: dict(plan.agent_priority_multipliers.get(aid, {})) for aid in self.agent_ids},
            "mailbox_seed": dict(plan.mailbox),
            "plan_revision": int(plan.plan_revision),
            "previous_day_review": dict(previous_review),
        }
        self._sync_shift_workspace(observation=observation, policy=payload, request_packet=request_packet)
        return payload

    def _deterministic_daily_reviewer(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        packet = self._reviewer_packet(day_summary, norms)
        achievement = packet.get("daily_target_achievement", {}) if isinstance(packet.get("daily_target_achievement", {}), dict) else {}
        target_misses = [
            str(key).strip()
            for key, info in achievement.items()
            if isinstance(info, dict) and not bool(info.get("achieved", False))
        ]
        failure_modes: list[str] = []
        closeout_gap_end = max(
            0,
            self._safe_int(day_summary.get("inspection_passes", 0), 0) - self._safe_int(day_summary.get("products", 0), 0),
        )
        if closeout_gap_end > int(((achievement.get("max_closeout_gap_end", {}) if isinstance(achievement.get("max_closeout_gap_end", {}), dict) else {}).get("target", 1) or 1)):
            failure_modes.append("closeout_gap")
        if self._safe_int(day_summary.get("agent_discharged_count", 0), 0) > 0:
            failure_modes.append("battery_instability")
        if self._safe_int(day_summary.get("machine_breakdown_count", 0), 0) > 0 or self._safe_float(day_summary.get("broken_machine_time_min", 0.0), 0.0) > 0.0:
            failure_modes.append("reliability_instability")
        if self._safe_int(day_summary.get("station2_completions", 0), 0) <= 0:
            failure_modes.append("s2_underfeed")
        if self._safe_int(day_summary.get("inspection_backlog_end", 0), 0) > 0 or self._safe_int(day_summary.get("station1_output_buffer_end", 0), 0) > 2:
            failure_modes.append("flow_blockage")
        failure_modes = self._sanitize_prevention_targets(failure_modes, limit=self.reviewer_max_failure_modes)
        recommended_prevention_targets = self._sanitize_prevention_targets(
            target_misses + failure_modes,
            limit=self.reviewer_max_prevention_targets,
        )
        if "closeout_gap" in recommended_prevention_targets:
            support_pair = "A1->A3"
        elif "battery_instability" in recommended_prevention_targets:
            support_pair = "A1->A2"
        elif "reliability_instability" in recommended_prevention_targets:
            support_pair = "A3->A2"
        else:
            support_pair = "A1->A3"
        role_change_advice: dict[str, str] = {}
        if "closeout_gap" in recommended_prevention_targets:
            role_change_advice["A3"] = "inspection_closer"
        if "reliability_instability" in recommended_prevention_targets and "A2" not in role_change_advice:
            role_change_advice["A2"] = "reliability_guard"
        carry_forward_risks: list[str] = []
        if closeout_gap_end > 0:
            carry_forward_risks.append("inspection output remained open at end of day")
        if self._safe_int(day_summary.get("agent_discharged_count", 0), 0) > 0:
            carry_forward_risks.append("battery discharge interrupted throughput")
        if self._safe_float(day_summary.get("broken_machine_time_min", 0.0), 0.0) > 0.0:
            carry_forward_risks.append("machine reliability consumed productive time")
        if self._safe_int(day_summary.get("station2_completions", 0), 0) <= 0:
            carry_forward_risks.append("stage 2 completions were weak")
        review = {
            "target_misses": target_misses[:3],
            "top_failure_modes": failure_modes,
            "recommended_prevention_targets": recommended_prevention_targets,
            "recommended_support_pair": support_pair,
            "role_change_advice": role_change_advice,
            "carry_forward_risks": carry_forward_risks[:4],
        }
        return self._sanitize_reviewer_output(review, fallback=review)

    def _invoke_daily_reviewer(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        request_packet = self._reviewer_packet(day_summary, norms)
        day = self._safe_int(day_summary.get("day", 0), 0)
        reviewer_transport = self._openclaw_transport_for_call("manager_daily_reviewer")
        reviewer_required_fields = {
            "target_misses": "list[str]",
            "top_failure_modes": "list[str]",
            "recommended_prevention_targets": "list[str]",
            "recommended_support_pair": "str",
            "role_change_advice": "dict[str, str]",
            "carry_forward_risks": "list[str]",
        }
        reviewer_instructions = [
            "Do diagnosis only. Do not repeat raw day summary metrics back to the strategist.",
            "Label only the main failure modes and keep the list short.",
            "Recommended prevention targets must use the canonical target vocabulary only.",
            "Recommended support pair must be exactly one pair such as A1->A3.",
            "Role change advice should be sparse and only name canonical roles when tomorrow should clearly keep or restore one.",
        ]
        runtime_agent_id = self._phase_runtime_agent_id("manager_daily_reviewer", {"phase": "manager_daily_reviewer", "day": day})
        if reviewer_transport == "native_local":
            system_prompt, prompt, required_keys = self._native_turn_prompts(
                agent_id=runtime_agent_id,
                phase="manager_daily_reviewer",
                role_summary="You are MANAGER_DAILY_REVIEWER. Diagnose today's execution and tell tomorrow's strategist what to change.",
                input_payload=request_packet,
                required_fields=reviewer_required_fields,
                instructions=reviewer_instructions,
                history_tag=f"day_{day:02d}_manager_daily_reviewer",
            )
            self._assert_native_workspace_inputs_ready(runtime_agent_id, "manager_daily_reviewer")
        else:
            system_prompt, prompt, required_keys = self._backend_direct_turn_prompts(
                phase="manager_daily_reviewer",
                role_summary="You are MANAGER_DAILY_REVIEWER. Diagnose today's execution and tell tomorrow's strategist what to change.",
                input_payload=request_packet,
                required_fields=reviewer_required_fields,
                instructions=reviewer_instructions,
            )
        fallback = self._deterministic_daily_reviewer(day_summary, norms)
        try:
            llm_obj = self._call_llm_json(prompt, system_prompt, call_name="manager_daily_reviewer", context={"phase": "manager_daily_reviewer", "day": day}, required_keys=required_keys)
        except RuntimeError:
            llm_obj = fallback
        review = self._sanitize_reviewer_output(llm_obj, fallback=fallback)
        self._sync_reviewer_workspace(day=day, summary=day_summary, review=review, request_packet=request_packet)
        return review

    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        shift_policy = self._invoke_shift_strategist(observation, norms=getattr(self, "norms", {}))
        self.current_shift_policy = dict(shift_policy)
        self.shift_policy_history.append({"day": self._safe_int(observation.get("day", 0), 0), **shift_policy})
        self._write_operating_artifacts()
        top_tasks = self._top_task_keys(shift_policy.get("task_priority_weights", {}))
        return StrategyState(
            notes=[f"top_focus={', '.join(top_tasks)}"] if top_tasks else [],
            summary=str(shift_policy.get("summary", "")).strip(),
            diagnosis={"worker_roles": [f"{agent_id}:{role}" for agent_id, role in dict(shift_policy.get("worker_roles", {})).items() if str(role).strip()]},
            orchestration_context={
                "shift_policy": dict(shift_policy),
                "plan_revision": int(shift_policy.get("plan_revision", 0) or 0),
                "policy_revision": int(shift_policy.get("plan_revision", 0) or 0),
                "day_summary_memory": self._latest_day_summary_memory(),
                "previous_day_review": self._latest_day_review_memory(),
                "knowledge_enabled": self._knowledge_enabled(),
            },
        )

    def propose_jobs(self, observation: dict[str, Any], strategy: StrategyState, norms: dict[str, Any]) -> JobPlan:
        fallback = self._scripted_baseline_plan(observation, norms)
        shift_policy = strategy.orchestration_context.get("shift_policy", {}) if isinstance(strategy.orchestration_context, dict) else {}
        if not isinstance(shift_policy, dict) or not shift_policy:
            shift_policy = self._invoke_shift_strategist(observation, norms=norms)
            self.current_shift_policy = dict(shift_policy)
        plan = self._build_priority_policy_plan(shift_policy, fallback, observation, mailbox_key="mailbox_seed")
        self.current_job_plan = plan
        self.agent_priority_multipliers = self._clone_agent_priority_multipliers(plan.agent_priority_multipliers)
        self.current_shift_policy = dict(shift_policy)
        self._write_operating_artifacts()
        return plan

    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        updated_norms = dict(norms if isinstance(norms, dict) else {})
        compiled = self._compile_day_summary(day_summary)
        self.day_summary_memory.append(compiled)
        if len(self.day_summary_memory) > max(1, int(self.memory_window_days or 7)):
            del self.day_summary_memory[: len(self.day_summary_memory) - max(1, int(self.memory_window_days or 7))]
        self._sync_day_summary_workspace(compiled)
        review = self._invoke_daily_reviewer(compiled, updated_norms) if self.review_enabled else self._deterministic_daily_reviewer(compiled, updated_norms)
        self.day_review_memory.append(review)
        if len(self.day_review_memory) > max(1, int(self.memory_window_days or 7)):
            del self.day_review_memory[: len(self.day_review_memory) - max(1, int(self.memory_window_days or 7))]
        day = self._safe_int(compiled.get("day", 0), 0)
        self.shared_discussion_memory.append(
            {
                "day": day,
                "issue_summary": {
                    "top_failure_modes": list(review.get("top_failure_modes", [])),
                    "target_misses": list(review.get("target_misses", [])),
                },
                "consensus_proposals": list(review.get("recommended_prevention_targets", [])),
                "conflicting_proposals": [],
            }
        )
        self._last_discussion_trace = [{"day": day, "type": "daily_reviewer", "review": review}]
        self._write_operating_artifacts()
        return updated_norms

    def urgent_discuss(self, event: dict[str, Any], local_state: dict[str, Any]) -> dict[str, Any]:
        return {}

    def reflect_run(self, *, output_root: Path, kpi: dict[str, Any], daily_summaries: list[dict[str, Any]], run_meta: dict[str, Any]) -> dict[str, Any]:
        if not self._knowledge_enabled():
            return {}
        return super().reflect_run(output_root=output_root, kpi=kpi, daily_summaries=daily_summaries, run_meta=run_meta)
