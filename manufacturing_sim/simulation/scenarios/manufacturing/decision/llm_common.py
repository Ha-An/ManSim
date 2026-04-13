from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import (
    TASK_PRIORITY_KEYS,
    AgentExperienceState,
    DecisionModule,
    JobPlan,
    StrategyState,
    default_agent_priority_multipliers,
    default_task_priority_weights,
)
from .openclaw_client import OpenClawClient


class OptionalLLMDecisionModule(DecisionModule):
    # Legacy planner path retained for non-orchestrated compatibility.
    # The active OpenClaw worker/manager flow lives in openclaw_orchestrated.py.
    TOWNHALL_STAGE_SPECS = (
        {
            "stage_id": "diagnose",
            "step": 1,
            "label": "Step 1: Diagnose problems and share current state.",
            "focus": "Surface the most important operational problems and share concrete state evidence.",
            "expected_contributions": ("new_evidence",),
        },
        {
            "stage_id": "critique",
            "step": 2,
            "label": "Step 2: Critique prior proposals.",
            "focus": "Challenge weak assumptions, missing evidence, or operational blind spots in prior proposals.",
            "expected_contributions": ("proposal_weakness", "new_evidence"),
        },
        {
            "stage_id": "alternatives",
            "step": 3,
            "label": "Step 3: Propose alternatives.",
            "focus": "Offer alternative strategies, especially by using different task families from earlier proposals.",
            "expected_contributions": ("alternative_task_family", "new_evidence"),
        },
        {
            "stage_id": "tradeoff",
            "step": 4,
            "label": "Step 4: Compare trade-offs.",
            "focus": "Compare short-term gains against long-term reliability, quality, and battery resilience.",
            "expected_contributions": ("short_term_vs_long_term_tradeoff", "proposal_weakness"),
        },
        {
            "stage_id": "synthesis",
            "step": 5,
            "label": "Step 5: Converge on an executable agreement.",
            "focus": "Narrow the discussion into an implementable agreement with explicit rationale and unresolved risks.",
            "expected_contributions": (
                "new_evidence",
                "proposal_weakness",
                "alternative_task_family",
                "short_term_vs_long_term_tradeoff",
            ),
        },
    )
    TOWNHALL_CONTRIBUTION_TYPES = (
        "new_evidence",
        "proposal_weakness",
        "alternative_task_family",
        "short_term_vs_long_term_tradeoff",
    )

    def __init__(self, cfg: dict[str, Any], llm_cfg: dict[str, Any] | None = None) -> None:
        self.cfg = cfg
        self.llm_cfg = llm_cfg or {}
        decision_cfg = cfg.get("decision", {}) if isinstance(cfg.get("decision", {}), dict) else {}
        rules_root = cfg.get("heuristic_rules", {}) if isinstance(cfg.get("heuristic_rules", {}), dict) else {}
        self.decision_rules = rules_root.get("decision", {}) if isinstance(rules_root.get("decision", {}), dict) else {}
        self.allowed_task_priority_keys = tuple(TASK_PRIORITY_KEYS)
        self.allowed_norm_keys = (
            "min_pm_per_machine_per_day",
            "inspect_product_priority_weight",
            "inspection_backlog_target",
            "max_output_buffer_target",
            "battery_reserve_min",
        )
        norms_cfg = decision_cfg.get("norms", {}) if isinstance(decision_cfg.get("norms", {}), dict) else {}
        self.norms_enabled = bool(norms_cfg.get("enabled", True))

        self.agent_priority_multiplier_min = float(self._decision_rule("llm_guardrails.agent_priority_multiplier_min", 0.75))
        self.agent_priority_multiplier_max = max(
            self.agent_priority_multiplier_min,
            float(self._decision_rule("llm_guardrails.agent_priority_multiplier_max", 1.5)),
        )
        self.agent_priority_decay = float(self._decision_rule("llm_guardrails.agent_priority_decay", 0.08))
        self.agent_priority_llm_blend = float(self._decision_rule("llm_guardrails.agent_priority_llm_blend", 0.6))
        self.agent_priority_completion_gain = float(self._decision_rule("llm_guardrails.agent_priority_completion_gain", 0.03))
        self.agent_priority_minutes_gain = float(self._decision_rule("llm_guardrails.agent_priority_minutes_gain", 0.001))
        self.agent_priority_interruption_penalty = float(self._decision_rule("llm_guardrails.agent_priority_interruption_penalty", 0.04))
        self.agent_priority_skip_penalty = float(self._decision_rule("llm_guardrails.agent_priority_skip_penalty", 0.03))

        guardrails = self.decision_rules.get("llm_guardrails", {}) if isinstance(self.decision_rules.get("llm_guardrails", {}), dict) else {}
        self.task_priority_weight_min = float(guardrails.get("task_priority_weight_min", 0.5))
        self.task_priority_weight_max = max(self.task_priority_weight_min, float(guardrails.get("task_priority_weight_max", 3.0)))
        self.urgent_priority_update_min = float(guardrails.get("urgent_priority_update_min", self.task_priority_weight_min))
        self.urgent_priority_update_max = max(self.urgent_priority_update_min, float(guardrails.get("urgent_priority_update_max", self.task_priority_weight_max)))
        self.quota_min = int(guardrails.get("quota_min", 0))
        quota_max_raw = guardrails.get("quota_max", {})
        default_quota_max = {
            "warehouse_material_runs": 40,
            "setup_runs": 80,
            "transfer_runs": 80,
            "inspection_runs": 80,
            "pm_runs": 24,
        }
        self.quota_max: dict[str, int] = dict(default_quota_max)
        if isinstance(quota_max_raw, dict):
            for key, value in quota_max_raw.items():
                try:
                    self.quota_max[str(key)] = max(self.quota_min, int(value))
                except (TypeError, ValueError):
                    continue
        self.min_pm_norm_min = int(guardrails.get("min_pm_per_machine_per_day_min", 1))
        self.min_pm_norm_max = max(self.min_pm_norm_min, int(guardrails.get("min_pm_per_machine_per_day_max", 4)))
        self.inspect_product_priority_weight_min = float(guardrails.get("inspect_product_priority_weight_min", 0.5))
        self.inspect_product_priority_weight_max = max(
            self.inspect_product_priority_weight_min,
            float(guardrails.get("inspect_product_priority_weight_max", 2.0)),
        )
        self.inspection_backlog_target_min = int(guardrails.get("inspection_backlog_target_min", 2))
        self.inspection_backlog_target_max = max(
            self.inspection_backlog_target_min,
            int(guardrails.get("inspection_backlog_target_max", 20)),
        )
        self.max_output_buffer_target_min = int(guardrails.get("max_output_buffer_target_min", 1))
        self.max_output_buffer_target_max = max(
            self.max_output_buffer_target_min,
            int(guardrails.get("max_output_buffer_target_max", 12)),
        )
        self.battery_reserve_min_min = float(guardrails.get("battery_reserve_min_min", 20.0))
        self.battery_reserve_min_max = max(
            self.battery_reserve_min_min,
            float(guardrails.get("battery_reserve_min_max", 90.0)),
        )
        self.enabled = bool(self.llm_cfg.get("enabled", True))
        self.provider = str(self.llm_cfg.get("provider", "openai_compatible")).strip().lower()
        self.server_url = str(self.llm_cfg.get("server_url", "http://localhost:8000/v1")).strip()
        self.model = str(self.llm_cfg.get("model", "")).strip()
        self.timeout_sec = int(self.llm_cfg.get("timeout_sec", 30))
        self.temperature = float(self.llm_cfg.get("temperature", 0.2))
        self.max_tokens = int(self.llm_cfg.get("max_tokens", 800))
        self.api_key = str(self.llm_cfg.get("api_key", "")).strip() or str(os.getenv("OPENAI_API_KEY", "")).strip()

        comm_cfg = self.llm_cfg.get("communication", {}) if isinstance(self.llm_cfg.get("communication", {}), dict) else {}
        # communication.* remains a compatibility bucket. The current default planner path
        # uses worker reports and manager reviews instead of the old townhall loop.
        self.language = self._normalize_communication_language(self.llm_cfg.get("language", comm_cfg.get("language", "ENG")))
        self.communication_enabled = bool(comm_cfg.get("enabled", True))
        self.comm_rounds = max(1, int(comm_cfg.get("rounds", 2)))
        self.comm_max_transcript = max(1, int(comm_cfg.get("max_transcript_messages", 24)))
        self.communication_language = self.language

        num_agents = int((cfg.get("factory", {}) or {}).get("num_agents", 4))
        self.agent_ids = [f"A{i}" for i in range(1, num_agents + 1)]

        openclaw_cfg = self.llm_cfg.get("openclaw", {}) if isinstance(self.llm_cfg.get("openclaw", {}), dict) else {}
        self.gateway_url = str(openclaw_cfg.get("gateway_url", self.llm_cfg.get("gateway_url", self.server_url))).strip() or self.server_url
        self.openclaw_profile_name = str(openclaw_cfg.get("profile_name", "mansim_repo")).strip() or "mansim_repo"
        self.openclaw_profile_config_path = str(openclaw_cfg.get("profile_config_path", "")).strip()
        self.openclaw_session_namespace = str(openclaw_cfg.get("session_namespace", "mansim")).strip() or "mansim_repo"
        self.openclaw_manager_agent_id = self._normalize_openclaw_agent_id(
            openclaw_cfg.get("manager_agent_id", "MANAGER"),
            default="MANAGER",
        )
        worker_agent_ids = openclaw_cfg.get("worker_agent_ids", []) if isinstance(openclaw_cfg.get("worker_agent_ids", []), list) else []
        parsed_worker_ids = [
            self._normalize_openclaw_agent_id(item)
            for item in worker_agent_ids
            if str(item).strip()
        ]
        self.openclaw_worker_agent_ids = parsed_worker_ids if parsed_worker_ids else list(self.agent_ids)
        self.openclaw_workspace_root = str(openclaw_cfg.get("workspace_root", "openclaw/workspaces")).strip() or "openclaw/workspaces"
        self.openclaw_transport = str(openclaw_cfg.get("transport", "native_local")).strip().lower() or "native_local"
        self.openclaw_backend_health_probe_interval_sec = float(openclaw_cfg.get("backend_health_probe_interval_sec", 30))
        if self.openclaw_backend_health_probe_interval_sec <= 0:
            self.openclaw_backend_health_probe_interval_sec = 30.0
        self.openclaw_backend_health_probe_timeout_sec = float(openclaw_cfg.get("backend_health_probe_timeout_sec", 2.5))
        if self.openclaw_backend_health_probe_timeout_sec <= 0:
            self.openclaw_backend_health_probe_timeout_sec = 2.5
        self.openclaw_runtime_recovery_probe_retries = max(1, int(openclaw_cfg.get("runtime_recovery_probe_retries", 2) or 2))
        self.openclaw_runtime_recovery_restarts = max(0, int(openclaw_cfg.get("runtime_recovery_restarts", 1) or 1))
        self.openclaw_runtime_recovery_backoff_sec = float(openclaw_cfg.get("runtime_recovery_backoff_sec", 1.5) or 1.5)
        if self.openclaw_runtime_recovery_backoff_sec < 0:
            self.openclaw_runtime_recovery_backoff_sec = 0.0
        self.openclaw_runtime_readiness_timeout_sec = float(openclaw_cfg.get("runtime_readiness_timeout_sec", 12.0) or 12.0)
        if self.openclaw_runtime_readiness_timeout_sec <= 0:
            self.openclaw_runtime_readiness_timeout_sec = 12.0
        self._openclaw_health_cache = None
        self._openclaw_health_last_checked = 0.0
        self.openclaw_native_thinking = self._normalize_native_thinking_level(openclaw_cfg.get("native_thinking", "minimal"), default="minimal")
        phase_transport_cfg = openclaw_cfg.get("phase_transport", {}) if isinstance(openclaw_cfg.get("phase_transport", {}), dict) else {}
        self.openclaw_phase_transport_default = str(phase_transport_cfg.get("default", self.openclaw_transport)).strip().lower() or self.openclaw_transport
        self.openclaw_native_phase_names = {str(item).strip() for item in phase_transport_cfg.get("native_local_phases", []) if str(item).strip()}
        self.openclaw_chat_phase_names = {str(item).strip() for item in phase_transport_cfg.get("chat_compat_phases", []) if str(item).strip()}
        if self.provider == "openclaw":
            if self.openclaw_transport != "native_local":
                self._fail("OpenClaw native_local-only mode: decision.llm.openclaw.transport must be native_local.")
            if self.openclaw_phase_transport_default != "native_local":
                self._fail("OpenClaw native_local-only mode: decision.llm.openclaw.phase_transport.default must be native_local.")
            if self.openclaw_chat_phase_names:
                self._fail("OpenClaw native_local-only mode: decision.llm.openclaw.phase_transport.chat_compat_phases is not supported.")
        prompt_cfg = openclaw_cfg.get("prompt", {}) if isinstance(openclaw_cfg.get("prompt", {}), dict) else {}
        self.openclaw_compact_system_prompt = bool(prompt_cfg.get("compact_system_prompt", True))
        orchestration_cfg = self.llm_cfg.get("orchestration", {}) if isinstance(self.llm_cfg.get("orchestration", {}), dict) else {}
        self.orchestration_enabled = bool(orchestration_cfg.get("enabled", self.provider == "openclaw"))
        self.daily_review_enabled = bool(orchestration_cfg.get("daily_review_enabled", True))
        self.incident_replan_enabled = bool(orchestration_cfg.get("incident_replan_enabled", True))
        self.worker_briefing_enabled = bool(orchestration_cfg.get("worker_briefing_enabled", True))
        self.worker_queue_limit = max(1, int(orchestration_cfg.get("worker_queue_limit", 4) or 4))
        backend_cfg = openclaw_cfg.get("backend", {}) if isinstance(openclaw_cfg.get("backend", {}), dict) else {}
        self.openclaw_backend = {
            "provider": str(backend_cfg.get("provider", "vllm")).strip().lower() or "vllm",
            "model": str(backend_cfg.get("model", "mansim-gemma4-e4b")).strip() or "mansim-gemma4-e4b",
            "model_name": str(backend_cfg.get("model_name", "Gemma 4 E4B IT")).strip() or "Gemma 4 E4B IT",
            "base_url": str(backend_cfg.get("base_url", "http://127.0.0.1:8000/v1")).strip() or "http://127.0.0.1:8000/v1",
            "api": str(backend_cfg.get("api", "openai-completions")).strip() or "openai-completions",
            "api_key": str(backend_cfg.get("api_key", "vllm-local")).strip() or "vllm-local",
            "context_window": max(1024, int(backend_cfg.get("context_window", 32768))),
            "max_output_tokens": max(256, int(backend_cfg.get("max_output_tokens", 4096))),
            "reasoning": bool(backend_cfg.get("reasoning", False)),
        }
        self.openclaw_run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.openclaw_runtime_root: Path | None = None
        self.openclaw_runtime_workspace_root: Path | None = None
        self.openclaw_runtime_workspace_aliases: dict[str, str] = {}
        self.openclaw_runtime_state_root: Path | None = None
        self.openclaw_runtime_facts_root: Path | None = None
        self.openclaw_gateway_log_path: Path | None = None
        self._openclaw_chat_fallback_ready = False
        self._openclaw_health_cache = None
        self._openclaw_health_last_checked = 0.0

        mem_cfg = self.llm_cfg.get("memory", {}) if isinstance(self.llm_cfg.get("memory", {}), dict) else {}
        self.memory_window_days = max(1, int(mem_cfg.get("history_window_days", 7)))
        self.include_agent_memory = bool(mem_cfg.get("include_agent_memory", True))

        self._last_discussion_trace: list[dict[str, Any]] = []
        # These caches are still kept for trace export and backward-compatible prompt helpers.
        # Run-local OpenClaw workspaces are the durable per-run memory artifacts.
        self.shared_norms_memory: list[dict[str, Any]] = []
        # These in-memory structures are now trace/export caches. In orchestrated OpenClaw runs, run-local workspace files are the durable within-run memory artifacts.
        self.shared_discussion_memory: list[dict[str, Any]] = []
        self.agent_memories: dict[str, list[dict[str, Any]]] = {aid: [] for aid in self.agent_ids}
        self.agent_experience_memory: dict[str, list[dict[str, Any]]] = {aid: [] for aid in self.agent_ids}
        self.agent_priority_multipliers: dict[str, dict[str, float]] = default_agent_priority_multipliers(self.agent_ids)
        self._last_agent_priority_update_trace: dict[str, Any] = {}
        self._llm_exchange_records: list[dict[str, Any]] = []
        self._llm_call_seq = 0
        self._llm_exchange_lock = threading.Lock()
        self._latest_worker_reports: dict[str, dict[str, Any]] = {}
        self._latest_manager_review: dict[str, Any] = {}
        self._active_orchestration_plan: dict[str, Any] = {}
        self._active_job_plan_snapshot: JobPlan | None = None

        if not self.enabled:
            self._fail("decision.mode=llm but llm.enabled=false.")
        if self.provider not in {"openai_compatible", "openclaw"}:
            self._fail(f"Unsupported llm.provider: {self.provider}")
        target_url = self.gateway_url if self.provider == "openclaw" else self.server_url
        if not target_url:
            self._fail("llm.server_url/gateway_url is empty.")
        if not self.model:
            self._fail("llm.model is empty.")
        self.server_url = target_url
        self.openclaw_client = None
        if self.provider == "openclaw":
            self.openclaw_client = OpenClawClient(
                gateway_url=self.server_url,
                model=self.model,
                timeout_sec=self.timeout_sec,
                api_key=self.api_key,
                profile_name=self.openclaw_profile_name,
                profile_config_path=self.openclaw_profile_config_path,
                backend=self.openclaw_backend,
            )

    @staticmethod
    def _fmt_number(value: float) -> str:
        numeric = float(value)
        if numeric.is_integer():
            return str(int(numeric))
        return f"{numeric:g}"

    @staticmethod
    def _normalize_communication_language(value: Any) -> str:
        raw = str(value or "ENG").strip().upper()
        if raw in {"KOR", "KO", "KOREAN", "KR"}:
            return "KOR"
        return "ENG"

    @staticmethod
    def _normalize_native_thinking_level(value: Any, default: str = "minimal") -> str:
        if isinstance(value, bool):
            return "off" if value is False else str(default or "minimal").strip().lower() or "minimal"
        raw = str(value or default or "minimal").strip().lower()
        allowed = {"off", "minimal", "low", "medium", "high", "adaptive"}
        return raw if raw in allowed else (str(default or "minimal").strip().lower() or "minimal")

    @staticmethod
    def _normalize_openclaw_agent_id(value: Any, default: str = "") -> str:
        raw = str(value or default).strip()
        if not raw:
            raw = str(default).strip()
        return raw.upper() if raw else ""

    def _communication_language_name(self) -> str:
        return "Korean" if self.language == "KOR" else "English"

    def _communication_language_instruction(self, fields: list[str] | tuple[str, ...] | None = None) -> str:
        field_text = ", ".join(str(item) for item in (fields or []))
        target = field_text if field_text else "all natural-language text fields"
        return (
            f"Write {target} in {self._communication_language_name()}. Keep JSON keys, enum values, IDs, task priority keys, norm keys, and machine/agent IDs in English."
        )

    def _openclaw_enabled(self) -> bool:
        return self.provider == "openclaw" and self.openclaw_client is not None

    def _openclaw_transport_for_call(self, call_name: str) -> str:
        if not self._openclaw_enabled():
            return "chat_compat"
        # OpenClaw is intentionally native-local only in this codebase.
        return "native_local"

    def _openclaw_uses_transport(self, transport: str) -> bool:
        if not self._openclaw_enabled():
            return False
        mode = str(transport or "").strip().lower()
        return mode == "native_local"

    def _openclaw_backend_health(self, *, force: bool = False) -> dict[str, Any]:
        if not self._openclaw_enabled() or self.openclaw_client is None:
            return {"ok": True, "checked_at": time.time(), "gateway": {"ok": True}, "backend": {"ok": True}, "reason": None}
        now = time.time()
        interval = float(self.openclaw_backend_health_probe_interval_sec or 30.0)
        if interval <= 0:
            interval = 30.0
        cached = self._openclaw_health_cache if isinstance(self._openclaw_health_cache, dict) else None
        if not force and cached is not None and (now - float(self._openclaw_health_last_checked or 0.0)) < interval:
            return dict(cached)
        try:
            probe = self.openclaw_client.probe_runtime_health(timeout_sec=float(self.openclaw_backend_health_probe_timeout_sec or 2.5))
        except Exception as exc:
            probe = {
                "ok": False,
                "checked_at": now,
                "gateway": {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                "backend": {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                "reason": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(probe, dict):
            probe = {"ok": False, "checked_at": now, "gateway": {"ok": False}, "backend": {"ok": False}, "reason": "invalid_health_payload"}
        probe = dict(probe)
        probe.setdefault("checked_at", now)
        probe.setdefault("reason", None)
        probe.setdefault("gateway", {"ok": False})
        probe.setdefault("backend", {"ok": False})
        self._openclaw_health_cache = dict(probe)
        self._openclaw_health_last_checked = now
        return dict(probe)

    def _openclaw_health_metrics(self) -> dict[str, Any]:
        return self._openclaw_health_cache if isinstance(self._openclaw_health_cache, dict) else {"ok": False}

    def _clear_openclaw_health_cache(self) -> None:
        self._openclaw_health_cache = None
        self._openclaw_health_last_checked = 0.0

    def _openclaw_native_readycheck(self, *, agent_id: str, call_name: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._openclaw_enabled() or self.openclaw_client is None:
            return {"ok": True, "reason": "openclaw_disabled"}
        ready_context = dict(context or {})
        ready_context["healthcheck"] = True
        session_key = self._openclaw_turn_session_key(agent_id, f"{call_name}_readycheck", ready_context)
        started = time.time()
        original_timeout = int(getattr(self.openclaw_client, "timeout_sec", self.timeout_sec) or self.timeout_sec)
        try:
            self.openclaw_client.timeout_sec = max(1, int(self.openclaw_runtime_readiness_timeout_sec or original_timeout))
            payload, content, _url, _headers, _body = self.openclaw_client.native_agent_turn(
                system_prompt=(
                    "Native runtime readiness check. Return exactly one JSON object with the field status set to ready. "
                    "Do not add prose, markdown, or extra keys."
                ),
                user_prompt='Return exactly {"status":"ready"}.',
                agent_id=agent_id,
                session_key=session_key,
                thinking="off",
            )
            parsed = self._extract_json_object(content)
            ok = isinstance(parsed, dict) and str(parsed.get("status", "")).strip().lower() == "ready"
            return {
                "ok": ok,
                "latency_ms": round((time.time() - started) * 1000.0, 3),
                "parsed": parsed if isinstance(parsed, dict) else {},
                "response_text": str(content or "")[:240],
                "reason": "ready" if ok else "invalid_readycheck_payload",
                "response": payload if isinstance(payload, dict) else {},
            }
        except Exception as exc:
            return {
                "ok": False,
                "latency_ms": round((time.time() - started) * 1000.0, 3),
                "parsed": {},
                "response_text": "",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        finally:
            try:
                self.openclaw_client.timeout_sec = original_timeout
            except Exception:
                pass

    def _openclaw_recover_runtime(self, *, call_name: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._openclaw_enabled() or self.openclaw_client is None:
            return {"ok": True, "reason": "openclaw_disabled", "attempts": []}
        agent_id = self._openclaw_agent_for_call(call_name, context)
        attempts: list[dict[str, Any]] = []
        probe_retries = max(1, int(self.openclaw_runtime_recovery_probe_retries or 1))
        restart_budget = max(0, int(self.openclaw_runtime_recovery_restarts or 0))
        backoff_sec = max(0.0, float(self.openclaw_runtime_recovery_backoff_sec or 0.0))

        def _probe_then_ready(stage: str) -> tuple[dict[str, Any], dict[str, Any]]:
            probe = self._openclaw_backend_health(force=True)
            ready = self._openclaw_native_readycheck(agent_id=agent_id, call_name=call_name, context=context)
            attempts.append({"stage": stage, "probe": dict(probe), "readycheck": dict(ready)})
            return probe, ready

        for probe_idx in range(probe_retries):
            probe, ready = _probe_then_ready(f"probe_{probe_idx + 1}")
            if ready.get("ok", False):
                self._clear_openclaw_health_cache()
                refreshed = self._openclaw_backend_health(force=True)
                return {
                    "ok": True,
                    "reason": "ready_after_probe" if probe.get("ok", False) else "probe_false_negative",
                    "attempts": attempts,
                    "backend_health": dict(refreshed),
                }
            if probe_idx < probe_retries - 1 and backoff_sec > 0:
                time.sleep(backoff_sec)

        for restart_idx in range(restart_budget):
            restart_info: dict[str, Any]
            try:
                restart_info = dict(self.openclaw_client.restart_gateway())
            except Exception as exc:
                restart_info = {"ok": False, "reason": f"restart_failed:{type(exc).__name__}:{exc}"}
            attempts.append({"stage": f"restart_{restart_idx + 1}", "restart": restart_info})
            self._clear_openclaw_health_cache()
            if backoff_sec > 0:
                time.sleep(backoff_sec)
            probe, ready = _probe_then_ready(f"post_restart_{restart_idx + 1}")
            if ready.get("ok", False):
                self._clear_openclaw_health_cache()
                refreshed = self._openclaw_backend_health(force=True)
                return {
                    "ok": True,
                    "reason": "ready_after_restart" if probe.get("ok", False) else "ready_after_restart_probe_false_negative",
                    "attempts": attempts,
                    "backend_health": dict(refreshed),
                }

        final_health = self._openclaw_backend_health(force=True)
        return {
            "ok": False,
            "reason": str(final_health.get("reason", "runtime_health_check_failed") or "runtime_health_check_failed"),
            "attempts": attempts,
            "backend_health": dict(final_health),
        }

    def _openclaw_agent_for_call(self, call_name: str, context: dict[str, Any] | None = None) -> str:
        ctx = context or {}
        if call_name in {"agent_reflect", "agent_propose_jobs", "townhall_round", "worker_daily_report"}:
            agent_id = self._normalize_openclaw_agent_id(ctx.get("agent_id", ""))
            if agent_id in self.openclaw_worker_agent_ids:
                return agent_id
        return self.openclaw_manager_agent_id

    def _openclaw_session_key(self, agent_id: str) -> str:
        raw = f"{self.openclaw_session_namespace}-{self.openclaw_run_id}-{agent_id}"
        return OpenClawClient.sanitize_session_id(raw)

    def _openclaw_turn_session_key(self, agent_id: str, call_name: str, context: dict[str, Any] | None = None) -> str:
        ctx = context or {}
        suffix_parts = [str(call_name or "turn").strip() or "turn"]
        if ctx.get("day") not in {None, ""}:
            suffix_parts.append(f"d{ctx.get('day')}")
        if ctx.get("round") not in {None, ""}:
            suffix_parts.append(f"r{ctx.get('round')}")
        if ctx.get("event_type") not in {None, ""}:
            suffix_parts.append(str(ctx.get("event_type")))
        raw = f"{self.openclaw_session_namespace}-{self.openclaw_run_id}-{agent_id}-{'-'.join(suffix_parts)}"
        return OpenClawClient.sanitize_session_id(raw)

    def _reset_run_state(self) -> None:
        self.openclaw_run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._last_discussion_trace = []
        self.shared_norms_memory = []
        self.shared_discussion_memory = []
        self.agent_memories = {aid: [] for aid in self.agent_ids}
        self.agent_experience_memory = {aid: [] for aid in self.agent_ids}
        self.agent_priority_multipliers = default_agent_priority_multipliers(self.agent_ids)
        self._last_agent_priority_update_trace = {}
        self._llm_exchange_records = []
        self._llm_call_seq = 0
        self._latest_worker_reports = {}
        self._latest_manager_review = {}
        self._active_orchestration_plan = {}
        self._active_job_plan_snapshot = None
        self.openclaw_runtime_root = None
        self.openclaw_runtime_workspace_root = None
        self.openclaw_runtime_workspace_aliases = {}
        self.openclaw_runtime_state_root = None
        self.openclaw_runtime_facts_root = None
        self.openclaw_gateway_log_path = None
        self._openclaw_chat_fallback_ready = False
        self._openclaw_health_cache = None
        self._openclaw_health_last_checked = 0.0

    def prepare_run_context(self, output_root: Path | str) -> dict[str, Any]:
        self._reset_run_state()
        if not self._openclaw_enabled():
            return {"run_id": self.openclaw_run_id}
        runtime_info = self.openclaw_client.prepare_run_runtime(
            output_root=Path(output_root),
            worker_agent_ids=list(self.openclaw_worker_agent_ids),
            manager_agent_id=self.openclaw_manager_agent_id,
            workspace_template_root=self.openclaw_workspace_root,
        )
        gateway_info: dict[str, Any] = {"status": "skipped", "transport": self.openclaw_transport}
        self.openclaw_runtime_root = Path(runtime_info["runtime_root"])
        self.openclaw_runtime_workspace_root = Path(runtime_info["workspace_root"])
        self.openclaw_runtime_workspace_aliases = {str(key).strip().upper(): str(value).strip().upper() for key, value in (runtime_info.get("workspace_aliases", {}) or {}).items() if str(key).strip() and str(value).strip()}
        self.openclaw_runtime_state_root = Path(runtime_info["state_root"])
        self.openclaw_runtime_facts_root = Path(runtime_info["facts_root"])
        self.openclaw_gateway_log_path = Path(runtime_info["gateway_log_path"])
        self._seed_openclaw_run_context()
        gateway_info = self.openclaw_client.restart_gateway()
        self._openclaw_chat_fallback_ready = False
        if self._openclaw_transport_for_call("prepare_runtime") != "native_local":
            self._fail("OpenClaw native_local-only mode guard: non-native transport requested during runtime prepare.")
        merged = dict(runtime_info)
        merged["gateway"] = gateway_info
        merged["run_id"] = self.openclaw_run_id
        return merged

    def _openclaw_workspace_path(self, agent_id: str) -> Path | None:
        if not self._openclaw_enabled() or self.openclaw_runtime_workspace_root is None:
            return None
        normalized = self._normalize_openclaw_agent_id(agent_id, default="")
        if not normalized:
            return None
        alias = self.openclaw_runtime_workspace_aliases.get(normalized, normalized)
        return self.openclaw_runtime_workspace_root / alias

    def _openclaw_write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _openclaw_render_markdown(self, title: str, sections: list[tuple[str, Any]]) -> str:
        lines = [f"# {title}", ""]
        for heading, content in sections:
            lines.append(f"## {heading}")
            if isinstance(content, (dict, list)):
                lines.append("```json")
                lines.append(json.dumps(content, indent=2, ensure_ascii=False))
                lines.append("```")
            else:
                text = str(content or "").strip()
                lines.append(text if text else "-")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _openclaw_write_markdown(self, path: Path, title: str, sections: list[tuple[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._openclaw_render_markdown(title, sections), encoding="utf-8")

    def _compact_recent_prompt_capsules(self, entries: Any, limit: int = 3) -> list[dict[str, Any]]:
        if not isinstance(entries, list):
            return []
        capsules: list[dict[str, Any]] = []
        for item in entries[-max(1, int(limit or 3)) :]:
            if not isinstance(item, dict):
                continue
            capsule: dict[str, Any] = {
                "day": int(item.get("day", 0) or 0),
            }
            summary = self._truncate_prompt_text(
                item.get("summary", item.get("system_focus", item.get("synthesis_summary", ""))),
                max_len=180,
            )
            if summary:
                capsule["summary"] = summary
            focus_tasks = item.get("focus_tasks", []) if isinstance(item.get("focus_tasks", []), list) else []
            if focus_tasks:
                capsule["focus_tasks"] = self._normalize_memory_text_list(focus_tasks, max_items=3, max_len=80)
            changed_norm_keys = item.get("changed_norm_keys", []) if isinstance(item.get("changed_norm_keys", []), list) else []
            if changed_norm_keys:
                capsule["changed_norm_keys"] = [str(key) for key in changed_norm_keys[:4]]
            recent_points = item.get("recent_points", []) if isinstance(item.get("recent_points", []), list) else []
            if recent_points:
                capsule["recent_points"] = self._normalize_memory_text_list(recent_points, max_items=2, max_len=90)
            capsules.append(capsule)
        return capsules

    @staticmethod
    def _openclaw_prompt_memory_sections(
        *,
        run_scope: str,
        prompt_memory: dict[str, Any],
        current_commitment: dict[str, Any] | None = None,
        raw_history_files: dict[str, Any] | None = None,
    ) -> list[tuple[str, Any]]:
        sections: list[tuple[str, Any]] = [("Run Scope", run_scope), ("Compressed Prompt Memory", prompt_memory)]
        if isinstance(current_commitment, dict):
            sections.append(("Current Commitment", current_commitment))
        if isinstance(raw_history_files, dict):
            sections.append(("Raw History Files", raw_history_files))
        return sections

    def _seed_openclaw_run_context(self) -> None:
        if not self._openclaw_enabled():
            return
        horizon_cfg = self.cfg.get("horizon", {}) if isinstance(self.cfg.get("horizon", {}), dict) else {}
        run_context = {
            "run_id": self.openclaw_run_id,
            "language": self.language,
            "objective": "Maximize accepted finished products within the current simulation horizon.",
            "total_days": int(horizon_cfg.get("num_days", 0) or 0),
            "minutes_per_day": int(horizon_cfg.get("minutes_per_day", 0) or 0),
            "worker_agent_ids": list(self.openclaw_worker_agent_ids),
            "manager_agent_id": self.openclaw_manager_agent_id,
            "run_isolation": True,
        }
        for agent_id in list(self.openclaw_worker_agent_ids) + [self.openclaw_manager_agent_id]:
            workspace = self._openclaw_workspace_path(agent_id)
            if workspace is None:
                continue
            self._openclaw_write_json(workspace / "facts" / "run_context.json", {**run_context, "agent_id": agent_id})
            self._openclaw_write_markdown(
                workspace / "RUN_CONTEXT.md",
                f"{agent_id} Run Context",
                [
                    ("Scope", "This workspace is isolated to the current simulation run only. Do not assume any prior-run continuity."),
                    ("Objective", run_context["objective"]),
                    ("Run Metadata", {**run_context, "agent_id": agent_id}),
                ],
            )
            self._openclaw_write_markdown(
                workspace / "memory" / "rolling_summary.md",
                f"{agent_id} Rolling Summary",
                [
                    ("Run Scope", "This file only accumulates memory for the current simulation run."),
                    ("Latest Summary", {"status": "initialized", "run_id": self.openclaw_run_id}),
                ],
            )
            self._openclaw_write_json(workspace / "beliefs" / "current_beliefs.json", {"status": "initialized", "agent_id": agent_id, "run_id": self.openclaw_run_id})
            self._openclaw_write_json(workspace / "commitments" / "current_commitment.json", {"status": "initialized", "agent_id": agent_id, "run_id": self.openclaw_run_id})
            self._openclaw_write_json(workspace / "memory" / "semantic" / "current.json", {"status": "initialized", "agent_id": agent_id, "run_id": self.openclaw_run_id})

    def _warm_openclaw_agents(self) -> None:
        # chat_compat path is intentionally removed. Native local sessions are warmed by
        # module-specific native warm-up routines.
        self._fail("OpenClaw chat_compat path is disabled. Use native_local only.")

    def _sync_openclaw_workspace_memory(
        self,
        *,
        day_summary: dict[str, Any],
        updated_norms: dict[str, Any],
        transcript: list[dict[str, Any]],
        summary: str,
        personal_conclusions: dict[str, dict[str, Any]] | None = None,
        agent_memory_updates: dict[str, dict[str, Any]] | None = None,
        moderator_memory_update: dict[str, Any] | None = None,
    ) -> None:
        if not self._openclaw_enabled():
            return
        day = int(day_summary.get("day", 0) or 0)
        day_view = self._day_summary_prompt_view(day_summary)
        shared_memory = self._memory_context("townhall", for_openclaw_workspace=True)
        highlights = self._townhall_recent_highlights(transcript[-self.comm_max_transcript :], limit=2) if transcript else {
            "consensus_proposals": [],
            "conflicting_proposals": [],
            "latest_points": [],
            "stage_progress": [],
        }
        conclusions = personal_conclusions if isinstance(personal_conclusions, dict) else {}
        normalized_agent_updates = agent_memory_updates if isinstance(agent_memory_updates, dict) else {}
        normalized_moderator_update = moderator_memory_update if isinstance(moderator_memory_update, dict) else {}

        for aid in self.agent_ids:
            workspace = self._openclaw_workspace_path(aid)
            if workspace is None:
                continue
            latest_memory = self.agent_memories.get(aid, [])[-1] if self.agent_memories.get(aid) else {}
            latest_experience = self.agent_experience_memory.get(aid, [])[-1] if self.agent_experience_memory.get(aid) else {}
            personal_conclusion = conclusions.get(aid, {}) if isinstance(conclusions.get(aid, {}), dict) else {}
            memory_update = self._normalize_agent_memory_update(
                aid,
                normalized_agent_updates.get(aid),
                fallback_summary=personal_conclusion.get("summary", summary),
                fallback_focus_tasks=list(personal_conclusion.get("focus_tasks", [])) if isinstance(personal_conclusion.get("focus_tasks", []), list) else [],
                fallback_commitment=personal_conclusion.get("commitment", ""),
            )
            rolling_summary = dict(memory_update.get("rolling_summary", {}))
            rolling_summary["day"] = day
            rolling_summary["current_priority_profile"] = self.agent_priority_multipliers.get(aid, {})
            rolling_summary["top_completed_task_families"] = list(latest_experience.get("top_completed_task_families", []))[:3]
            rolling_summary["contribution_signals"] = latest_experience.get("contribution_signals", {})
            recent_capsules = self._compact_recent_prompt_capsules(self.agent_memories.get(aid, []), limit=3)
            prompt_memory = {
                "beliefs": memory_update.get("beliefs", {}),
                "rolling_summary": rolling_summary,
                "recent_day_capsules": recent_capsules,
                "shared_memory_snapshot": {
                    "recent_norm_changes": list(shared_memory.get("recent_norm_changes", []))[:2],
                    "recent_issue_summary": list(shared_memory.get("recent_issue_summary", []))[:2],
                },
            }

            self._openclaw_write_json(workspace / "facts" / "current_day_summary.json", day_view)
            self._openclaw_write_json(workspace / "facts" / "current_norms.json", updated_norms)
            self._openclaw_write_json(workspace / "facts" / "current_priority_profile.json", self.agent_priority_multipliers.get(aid, {}))
            self._openclaw_write_json(workspace / "facts" / "current_personal_conclusion.json", personal_conclusion)
            self._openclaw_write_json(workspace / "facts" / "current_shared_memory.json", shared_memory)
            self._openclaw_write_json(workspace / "facts" / "current_memory_update.json", memory_update)
            self._openclaw_write_json(workspace / "facts" / "memory_update_history" / f"day_{day:02d}.json", memory_update)
            self._openclaw_write_json(workspace / "beliefs" / "current_beliefs.json", memory_update.get("beliefs", {}))
            self._openclaw_write_json(workspace / "beliefs" / "history" / f"day_{day:02d}.json", memory_update.get("beliefs", {}))
            self._openclaw_write_json(workspace / "commitments" / "current_commitment.json", memory_update.get("commitment", {}))
            self._openclaw_write_json(workspace / "commitments" / "history" / f"day_{day:02d}.json", memory_update.get("commitment", {}))
            self._openclaw_write_json(workspace / "memory" / "episodic" / f"day_{day:02d}.json", memory_update.get("episodic_entry", {}))
            self._openclaw_write_json(workspace / "memory" / "semantic" / "current.json", memory_update.get("semantic_memory", {}))
            self._openclaw_write_markdown(
                workspace / "memory" / "episodic" / f"day_{day:02d}.md",
                f"{aid} Day {day} Episodic Memory",
                [("Episode", memory_update.get("episodic_entry", {})), ("Day Summary", day_view)],
            )
            self._openclaw_write_markdown(
                workspace / "memory" / "daily" / f"day_{day:02d}.md",
                f"{aid} Day {day} Memory",
                [
                    ("Day Summary", day_view),
                    ("Latest Experience", latest_experience),
                    ("Latest Operations Review Memory", latest_memory),
                    ("Personal Conclusion", personal_conclusion),
                    ("Beliefs", memory_update.get("beliefs", {})),
                    ("Commitments", memory_update.get("commitment", {})),
                    ("Rolling Summary", rolling_summary),
                ],
            )
            self._openclaw_write_markdown(workspace / "memory" / "semantic" / "specialization.md", f"{aid} Specialization Memory", [("Specialization", memory_update.get("semantic_memory", {}).get("specialization", []))])
            self._openclaw_write_markdown(workspace / "memory" / "semantic" / "heuristics.md", f"{aid} Heuristics Memory", [("Heuristics", memory_update.get("semantic_memory", {}).get("heuristics", []))])
            self._openclaw_write_markdown(workspace / "memory" / "semantic" / "anti_patterns.md", f"{aid} Anti-Patterns Memory", [("Anti-Patterns", memory_update.get("semantic_memory", {}).get("anti_patterns", []))])
            self._openclaw_write_markdown(
                workspace / "memory" / "rolling_summary.md",
                f"{aid} Rolling Summary",
                [
                    ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                    ("Latest Rolling Summary", rolling_summary),
                    ("Recent Day Capsules", recent_capsules),
                    ("Shared Memory Snapshot", prompt_memory["shared_memory_snapshot"]),
                ],
            )
            self._openclaw_write_markdown(
                workspace / "MEMORY.md",
                f"{aid} Memory",
                self._openclaw_prompt_memory_sections(
                    run_scope="This workspace memory is scoped to the current run only. When a new simulation run starts, both the session and the memory files are rebuilt from scratch.",
                    prompt_memory=prompt_memory,
                    current_commitment=memory_update.get("commitment", {}),
                    raw_history_files={
                        "daily": f"memory/daily/day_{day:02d}.md",
                        "episodic": f"memory/episodic/day_{day:02d}.md",
                        "semantic_specialization": "memory/semantic/specialization.md",
                        "semantic_heuristics": "memory/semantic/heuristics.md",
                        "semantic_anti_patterns": "memory/semantic/anti_patterns.md",
                    },
                ),
            )

        moderator_workspace = self._openclaw_workspace_path(self.openclaw_manager_agent_id)
        if moderator_workspace is not None:
            moderator_memory = self._normalize_moderator_memory_update(normalized_moderator_update, fallback_summary=summary)
            moderator_summary = dict(moderator_memory.get("shared_rolling_summary", {}))
            moderator_summary["day"] = day
            moderator_summary["updated_norms"] = updated_norms
            moderator_summary["highlights"] = highlights
            moderator_summary["shared_memory"] = shared_memory
            recent_capsules = self._compact_recent_prompt_capsules(self.shared_discussion_memory, limit=3)
            prompt_memory = {
                "shared_beliefs": moderator_memory.get("shared_beliefs", {}),
                "shared_rolling_summary": moderator_summary,
                "recent_day_capsules": recent_capsules,
                "norm_memory_snapshot": list(shared_memory.get("recent_norm_changes", []))[:2],
            }
            self._openclaw_write_json(moderator_workspace / "facts" / "current_day_summary.json", day_view)
            self._openclaw_write_json(moderator_workspace / "facts" / "current_norms.json", updated_norms)
            self._openclaw_write_json(moderator_workspace / "facts" / "current_transcript.json", transcript[-self.comm_max_transcript :])
            self._openclaw_write_json(moderator_workspace / "facts" / "current_personal_conclusions.json", conclusions)
            self._openclaw_write_json(moderator_workspace / "facts" / "current_memory_update.json", moderator_memory)
            self._openclaw_write_json(moderator_workspace / "facts" / "memory_update_history" / f"day_{day:02d}.json", moderator_memory)
            self._openclaw_write_json(moderator_workspace / "beliefs" / "current_beliefs.json", moderator_memory.get("shared_beliefs", {}))
            self._openclaw_write_json(moderator_workspace / "beliefs" / "history" / f"day_{day:02d}.json", moderator_memory.get("shared_beliefs", {}))
            self._openclaw_write_json(moderator_workspace / "commitments" / "current_commitment.json", moderator_memory.get("shared_commitments", {}))
            self._openclaw_write_json(moderator_workspace / "commitments" / "history" / f"day_{day:02d}.json", moderator_memory.get("shared_commitments", {}))
            self._openclaw_write_json(moderator_workspace / "memory" / "episodic" / f"day_{day:02d}.json", moderator_memory.get("episodic_entry", {}))
            self._openclaw_write_json(moderator_workspace / "memory" / "semantic" / "current.json", moderator_memory.get("shared_semantic_memory", {}))
            self._openclaw_write_markdown(moderator_workspace / "memory" / "episodic" / f"day_{day:02d}.md", f"{self.openclaw_manager_agent_id} Day {day} Episodic Memory", [("Episode", moderator_memory.get("episodic_entry", {})), ("Operations Review Highlights", highlights)])
            self._openclaw_write_markdown(moderator_workspace / "memory" / "daily" / f"day_{day:02d}.md", f"{self.openclaw_manager_agent_id} Day {day} Memory", [("Day Summary", day_view), ("Shared Beliefs", moderator_memory.get("shared_beliefs", {})), ("Shared Commitments", moderator_memory.get("shared_commitments", {})), ("Operations Review Summary", moderator_summary), ("Personal Conclusions", conclusions)])
            self._openclaw_write_markdown(moderator_workspace / "memory" / "semantic" / "coordination_notes.md", f"{self.openclaw_manager_agent_id} Coordination Notes", [("Coordination Notes", moderator_memory.get("shared_semantic_memory", {}).get("coordination_notes", []))])
            self._openclaw_write_markdown(moderator_workspace / "memory" / "semantic" / "heuristics.md", f"{self.openclaw_manager_agent_id} Heuristics", [("Heuristics", moderator_memory.get("shared_semantic_memory", {}).get("heuristics", []))])
            self._openclaw_write_markdown(moderator_workspace / "memory" / "semantic" / "anti_patterns.md", f"{self.openclaw_manager_agent_id} Anti-Patterns", [("Anti-Patterns", moderator_memory.get("shared_semantic_memory", {}).get("anti_patterns", []))])
            self._openclaw_write_markdown(moderator_workspace / "memory" / "semantic" / "unresolved_disagreements.md", f"{self.openclaw_manager_agent_id} Unresolved Disagreements", [("Unresolved Disagreements", moderator_memory.get("shared_semantic_memory", {}).get("unresolved_disagreements", []))])
            self._openclaw_write_markdown(
                moderator_workspace / "memory" / "rolling_summary.md",
                f"{self.openclaw_manager_agent_id} Rolling Summary",
                [
                    ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                    ("Latest Manager Summary", moderator_summary),
                    ("Recent Day Capsules", recent_capsules),
                    ("Norm Memory Snapshot", prompt_memory.get("norm_memory_snapshot", [])),
                ],
            )
            self._openclaw_write_markdown(
                moderator_workspace / "MEMORY.md",
                f"{self.openclaw_manager_agent_id} Memory",
                self._openclaw_prompt_memory_sections(
                    run_scope="This workspace memory is scoped to the current run only. When a new simulation run starts, it is rebuilt from the initial state.",
                    prompt_memory=prompt_memory,
                    current_commitment=moderator_memory.get("shared_commitments", {}),
                    raw_history_files={
                        "daily": f"memory/daily/day_{day:02d}.md",
                        "episodic": f"memory/episodic/day_{day:02d}.md",
                        "semantic_coordination_notes": "memory/semantic/coordination_notes.md",
                        "semantic_heuristics": "memory/semantic/heuristics.md",
                        "semantic_anti_patterns": "memory/semantic/anti_patterns.md",
                        "semantic_unresolved_disagreements": "memory/semantic/unresolved_disagreements.md",
                    },
                ),
            )

    def _processing_station_ids(self) -> list[int]:
        factory_cfg = self.cfg.get("factory", {}) if isinstance(self.cfg.get("factory", {}), dict) else {}
        process_cfg = factory_cfg.get("processing_time_min", {}) if isinstance(factory_cfg.get("processing_time_min", {}), dict) else {}
        stations: list[int] = []
        for key in process_cfg:
            key_str = str(key)
            if not key_str.startswith("station"):
                continue
            suffix = key_str.replace("station", "", 1)
            if suffix.isdigit():
                stations.append(int(suffix))
        stations.sort()
        return stations

    def _shared_system_prompt(
        self,
        role_summary: str,
        phase_guidance: list[str] | None = None,
        *,
        include_task_family_semantics: bool = True,
        include_norm_semantics: bool = True,
        task_family_subset: list[str] | tuple[str, ...] | None = None,
        compact: bool = False,
    ) -> str:
        factory_cfg = self.cfg.get("factory", {}) if isinstance(self.cfg.get("factory", {}), dict) else {}
        movement_cfg = self.cfg.get("movement", {}) if isinstance(self.cfg.get("movement", {}), dict) else {}
        machine_failure_cfg = self.cfg.get("machine_failure", {}) if isinstance(self.cfg.get("machine_failure", {}), dict) else {}
        agent_cfg = self.cfg.get("agent", {}) if isinstance(self.cfg.get("agent", {}), dict) else {}
        stations = self._processing_station_ids()
        station_names = [f"Station{station}" for station in stations]
        plant_flow = " -> ".join(station_names + ["Inspection"]) if station_names else "Inspection"
        processing_cfg = factory_cfg.get("processing_time_min", {}) if isinstance(factory_cfg.get("processing_time_min", {}), dict) else {}
        cycle_text = ", ".join(f"S{station} {self._fmt_number(float(processing_cfg.get(f'station{station}', 0.0)))}m" for station in stations)
        inspection_base = self._fmt_number(float(factory_cfg.get("inspection_base_time_min", 0.0)))
        inspection_min = self._fmt_number(float(factory_cfg.get("inspection_min_time_min", 0.0)))
        warehouse_to_station = self._fmt_number(float(movement_cfg.get("warehouse_to_station_min", 0.0)))
        station_to_station = self._fmt_number(float(movement_cfg.get("station_to_station_min", 0.0)))
        setup_min = self._fmt_number(float(movement_cfg.get("setup_min", 0.0)))
        unload_min = self._fmt_number(float(movement_cfg.get("unload_min", 0.0)))
        repair_min = self._fmt_number(float(machine_failure_cfg.get("repair_time_min", 0.0)))
        pm_min = self._fmt_number(float(machine_failure_cfg.get("pm_time_min", 0.0)))
        mean_ttf = self._fmt_number(float(machine_failure_cfg.get("mean_time_to_fail_min", 0.0)))
        battery_period = self._fmt_number(float(agent_cfg.get("battery_swap_period_min", 0.0)))
        battery_pickup = self._fmt_number(float(agent_cfg.get("battery_pickup_time_min", 0.0)))
        battery_delivery_extra = self._fmt_number(float(agent_cfg.get("battery_delivery_extra_min", 0.0)))
        horizon_days = int(((self.cfg.get("horizon", {}) or {}).get("num_days", 0) or 0))

        task_family_semantics = {
            "battery_swap": "swap once a fresh battery is available.",
            "battery_delivery_low_battery": "bring a fresh battery to an active low-battery agent.",
            "battery_delivery_discharged": "rescue a discharged agent with a fresh battery.",
            "repair_machine": "repair a broken machine to restore capacity.",
            "unload_machine": "remove processed output so downstream flow can continue.",
            "setup_machine": "set up a waiting machine so processing can start.",
            "inter_station_transfer": "move items between stations, inspection, and warehouse.",
            "material_supply": "replenish station material from warehouse stock.",
            "inspect_product": "inspect a finished-product candidate at Inspection.",
            "preventive_maintenance": "spend time now to reduce future breakdown risk temporarily.",
        }
        task_family_order = list(task_family_semantics.keys())
        selected_task_families = [key for key in (list(task_family_subset) if task_family_subset else task_family_order) if key in task_family_semantics]
        task_family_lines = [f"- {key}: {task_family_semantics[key]}" for key in selected_task_families]

        if compact:
            plant_lines = [
                f"- Flow: {plant_flow}; workers: {', '.join(self.agent_ids)}.",
                f"- Cycles: {cycle_text or '-'}; inspection {inspection_base}/sqrt(parallel), floor {inspection_min}.",
                f"- Travel/setup/unload/repair/PM: {warehouse_to_station}/{station_to_station}/{setup_min}/{unload_min}/{repair_min}/{pm_min} min.",
                f"- Reliability/battery: mean TTF {mean_ttf}; swap {battery_period}; pickup {battery_pickup}; delivery {battery_delivery_extra}.",
            ]
            naming_lines = ["- IDs remain English: A#, SXMY, Station#, Inspection, Warehouse, BatteryStation."]
            constraint_lines = ["- Choose only feasible tasks and existing resources.", "- Infer urgency from current evidence only."]
            language_lines = [f"- Natural-language values in {self._communication_language_name()}; JSON keys/IDs stay English."]
            norm_lines = [
                "- Norms are planning references, not hard constraints.",
                "- Key norms: PM baseline, inspection weight, inspection backlog target, output-buffer target, battery reserve.",
            ]
            workspace_lines = [
                "- facts/*.json are simulator-written evidence.",
                "- beliefs/current_beliefs.json, commitments/current_commitment.json, MEMORY.md, and memory/* store within-run memory.",
            ]
        else:
            plant_lines = [
                f"- Flow: {plant_flow}.",
                f"- Workers: {', '.join(self.agent_ids)}.",
                f"- Cycles: {cycle_text or '-'}; inspection {inspection_base}/sqrt(parallel inspectors), floor {inspection_min} min.",
                f"- Travel/timing: warehouse-station {warehouse_to_station}; station-station/inspection {station_to_station}; setup {setup_min}; unload {unload_min}; repair {repair_min}; PM {pm_min}.",
                f"- Reliability/battery: mean TTF {mean_ttf}; swap period {battery_period}; pickup {battery_pickup}; delivery overhead {battery_delivery_extra}.",
            ]
            naming_lines = [
                "- Agent IDs use A#; machine IDs use SXMY.",
                "- Locations use Warehouse, Station1..StationN, Inspection, BatteryStation, and CoordinationReview.",
            ]
            constraint_lines = [
                "- Choose only feasible tasks. Do not invent tasks, machines, agents, queues, or process stages.",
                "- Infer flow pressure from the current state only; do not assume a fixed bottleneck or overreact without evidence.",
            ]
            language_lines = [
                f"- Write all natural-language text fields in {self._communication_language_name()}.",
                "- Keep JSON keys, enum values, IDs, task priority keys, norm keys, and machine/agent IDs in English.",
            ]
            norm_lines = [
                "- Norms are persistent team-level planning references, not hard constraints.",
                "- min_pm_per_machine_per_day: PM baseline.",
                "- inspect_product_priority_weight: inspection baseline.",
                "- inspection_backlog_target: preferred backlog cap.",
                "- max_output_buffer_target: preferred output-buffer cap.",
                "- battery_reserve_min: preferred minimum battery reserve.",
            ]
            workspace_lines = [
                "- This workspace is isolated to the current simulation run; do not assume continuity beyond it.",
                "- facts/*.json contain simulator-written run-local evidence.",
                "- beliefs/current_beliefs.json, commitments/current_commitment.json, MEMORY.md, memory/episodic/, memory/semantic/, and memory/daily/ store within-run memory; let current facts override stale memory.",
            ]

        prompt_sections = [
            role_summary.strip(),
            f"Global objective:\n- Maximize the number of accepted finished products completed within the full simulation horizon of {horizon_days} days.",
            "Plant summary:\n" + "\n".join(plant_lines),
            "Naming and JSON conventions:\n" + "\n".join(naming_lines),
            "Core constraints:\n" + "\n".join(constraint_lines),
            "Response language:\n" + "\n".join(language_lines),
        ]
        if include_task_family_semantics and task_family_lines:
            prompt_sections.insert(3, "Task family semantics:\n" + "\n".join(task_family_lines))
        if self.norms_enabled and include_norm_semantics:
            prompt_sections.insert(3, "Shared norm semantics:\n" + "\n".join(norm_lines))
        if self._openclaw_enabled():
            prompt_sections.append("OpenClaw workspace context:\n" + "\n".join(workspace_lines))
        if phase_guidance:
            prompt_sections.append("Phase-specific instructions:\n" + "\n".join(f"- {line}" for line in phase_guidance))
        prompt_sections.append(
            "Output discipline:\n"
            "- Return exactly one JSON object that follows the requested schema.\n"
            "- Do not add markdown, code fences, comments, or extra prose outside the JSON object."
        )
        return "\n\n".join(section for section in prompt_sections if section.strip())

    def _coordination_task_family_subset(self) -> tuple[str, ...]:
        return (
            "repair_machine",
            "unload_machine",
            "inter_station_transfer",
            "material_supply",
            "inspect_product",
            "preventive_maintenance",
            "battery_delivery_low_battery",
            "battery_delivery_discharged",
        )

    def _townhall_task_family_subset(self) -> tuple[str, ...]:
        return self._coordination_task_family_subset()

    def _decision_rule(self, dotted_path: str, default: Any) -> Any:
        node: Any = self.decision_rules
        for key in dotted_path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def _fail(self, message: str) -> None:
        warnings.warn(f"[LLM WARNING] {message}")
        raise RuntimeError(f"[LLM WARNING] {message}")

    def consume_last_discussion_trace(self) -> list[dict[str, Any]]:
        out = list(self._last_discussion_trace)
        self._last_discussion_trace = []
        return out

    def is_communication_enabled(self) -> bool:
        return bool(self.communication_enabled)

    def get_llm_exchange_records(self) -> list[dict[str, Any]]:
        return list(self._llm_exchange_records)

    def consume_last_agent_priority_update_trace(self) -> dict[str, Any]:
        out = dict(self._last_agent_priority_update_trace)
        self._last_agent_priority_update_trace = {}
        return out

    def get_agent_priority_multipliers(self) -> dict[str, dict[str, float]]:
        return {agent_id: dict(values) for agent_id, values in self.agent_priority_multipliers.items()}

    def _record_llm_exchange(self, record: dict[str, Any]) -> None:
        with self._llm_exchange_lock:
            self._llm_exchange_records.append(record)
            runtime_root = getattr(self, "openclaw_runtime_root", None)
            if runtime_root is None:
                return
            try:
                trace_dir = Path(runtime_root) / "trace"
                trace_dir.mkdir(parents=True, exist_ok=True)
                trace_path = trace_dir / "llm_exchange_live.jsonl"
                with trace_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception:
                pass

    def _clone_agent_priority_multipliers(self, src: dict[str, dict[str, float]] | None = None) -> dict[str, dict[str, float]]:
        baseline = src if isinstance(src, dict) else self.agent_priority_multipliers
        cloned = default_agent_priority_multipliers(self.agent_ids)
        for agent_id, values in baseline.items():
            if agent_id not in cloned or not isinstance(values, dict):
                continue
            for key in self.allowed_task_priority_keys:
                if key in values:
                    cloned[agent_id][key] = round(
                        self._clamp_float(values.get(key), self.agent_priority_multiplier_min, self.agent_priority_multiplier_max, 1.0),
                        3,
                    )
        return cloned

    def _summarize_agent_priority_profiles(
        self,
        src: dict[str, dict[str, float]] | None = None,
        top_n: int = 2,
        *,
        include_full: bool = False,
        non_neutral_only: bool = False,
        neutral_epsilon: float = 0.03,
    ) -> dict[str, Any]:
        profiles = self._clone_agent_priority_multipliers(src)
        summary: dict[str, Any] = {}
        for agent_id, values in profiles.items():
            ranked = sorted(values.items(), key=lambda item: abs(float(item[1]) - 1.0), reverse=True)
            max_delta = max((abs(float(value) - 1.0) for _, value in values.items()), default=0.0)
            if non_neutral_only and max_delta < float(neutral_epsilon):
                continue
            entry = {
                "top_biases": [
                    {"priority_key": key, "multiplier": round(float(value), 3)}
                    for key, value in ranked[:top_n]
                ],
            }
            if include_full:
                entry["full"] = {key: round(float(value), 3) for key, value in values.items()}
            summary[agent_id] = entry
        return summary

    def _sanitize_agent_priority_profile_updates(self, src: Any) -> dict[str, dict[str, float]]:
        updates: dict[str, dict[str, float]] = {}
        if not isinstance(src, dict):
            return updates
        for agent_id in self.agent_ids:
            raw = src.get(agent_id)
            if not isinstance(raw, dict):
                continue
            cleaned: dict[str, float] = {}
            for key in self.allowed_task_priority_keys:
                if key in raw:
                    cleaned[key] = round(
                        self._clamp_float(raw.get(key), self.agent_priority_multiplier_min, self.agent_priority_multiplier_max, 1.0),
                        3,
                    )
            if cleaned:
                updates[agent_id] = cleaned
        return updates

    def _apply_agent_priority_target_updates(
        self,
        base_profiles: dict[str, dict[str, float]],
        updates: dict[str, dict[str, float]],
        *,
        blend: float,
    ) -> dict[str, dict[str, float]]:
        blended = self._clone_agent_priority_multipliers(base_profiles)
        blend_ratio = min(max(float(blend), 0.0), 1.0)
        for agent_id, key_updates in updates.items():
            if agent_id not in blended:
                continue
            for key, target in key_updates.items():
                current = float(blended[agent_id].get(key, 1.0))
                merged = current + (float(target) - current) * blend_ratio
                blended[agent_id][key] = round(
                    self._clamp_float(merged, self.agent_priority_multiplier_min, self.agent_priority_multiplier_max, current),
                    3,
                )
        return blended

    def _experience_adjusted_agent_profiles(self, day_summary: dict[str, Any]) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
        # Decay yesterday's per-agent bias toward 1.0, then nudge each task family
        # using the finished day's completions, minutes, interruptions, and skips.
        profiles = self._clone_agent_priority_multipliers()
        agent_experience = day_summary.get("agent_experience", {}) if isinstance(day_summary.get("agent_experience", {}), dict) else {}
        trace: dict[str, Any] = {}
        for agent_id in self.agent_ids:
            row = profiles.setdefault(agent_id, default_task_priority_weights())
            raw = agent_experience.get(agent_id, {}) if isinstance(agent_experience.get(agent_id, {}), dict) else {}
            completed = raw.get("completed_counts", {}) if isinstance(raw.get("completed_counts", {}), dict) else {}
            completed_minutes = raw.get("completed_minutes", {}) if isinstance(raw.get("completed_minutes", {}), dict) else {}
            interrupted = raw.get("interrupted_counts", {}) if isinstance(raw.get("interrupted_counts", {}), dict) else {}
            skipped = raw.get("skipped_counts", {}) if isinstance(raw.get("skipped_counts", {}), dict) else {}
            changed: dict[str, dict[str, float]] = {}
            for key in self.allowed_task_priority_keys:
                current = float(row.get(key, 1.0))
                decayed = 1.0 + (current - 1.0) * (1.0 - self.agent_priority_decay)
                completed_count = int(completed.get(key, 0) or 0)
                minute_gain = float(completed_minutes.get(key, 0.0) or 0.0) * self.agent_priority_minutes_gain
                gain = min(0.18, completed_count * self.agent_priority_completion_gain + minute_gain)
                interrupted_count = int(interrupted.get(key, 0) or 0)
                skipped_count = int(skipped.get(key, 0) or 0)
                penalty = min(0.14, interrupted_count * self.agent_priority_interruption_penalty + skipped_count * self.agent_priority_skip_penalty)
                target = decayed * (1.0 + gain - penalty)
                row[key] = round(
                    self._clamp_float(target, self.agent_priority_multiplier_min, self.agent_priority_multiplier_max, current),
                    3,
                )
                if abs(float(row[key]) - current) >= 0.001:
                    changed[key] = {"from": round(current, 3), "to": round(float(row[key]), 3)}
            trace[agent_id] = {
                "changed_keys": sorted(changed.keys()),
                "changes": changed,
                "top_completed_task_families": raw.get("top_completed_task_families", []),
            }
        return profiles, trace

    def _agent_experience_prompt_payload(self, day_summary: dict[str, Any]) -> dict[str, Any]:
        agent_experience = day_summary.get("agent_experience", {}) if isinstance(day_summary.get("agent_experience", {}), dict) else {}
        payload: dict[str, Any] = {}
        for agent_id in self.agent_ids:
            raw = agent_experience.get(agent_id, {}) if isinstance(agent_experience.get(agent_id, {}), dict) else {}
            item = {
                "today": {
                    "top_completed_task_families": list(raw.get("top_completed_task_families", []))[:2],
                    "contribution_signals": dict(raw.get("contribution_signals", {})),
                    "recent_task_events": list(raw.get("recent_task_events", []))[:1],
                },
            }
            if not self._openclaw_enabled():
                item["recent_memory"] = self.agent_experience_memory.get(agent_id, [])[-1:]
            payload[agent_id] = item
        return payload

    def _agent_prompt_memory(self, agent_id: str) -> dict[str, Any]:
        if not self.include_agent_memory or self._openclaw_enabled():
            return {}
        payload: dict[str, Any] = {}
        townhall_memory = self.agent_memories.get(agent_id, [])[-1:]
        experience_memory = self.agent_experience_memory.get(agent_id, [])[-1:]
        if townhall_memory:
            payload["townhall_memory"] = townhall_memory
            latest = townhall_memory[-1] if isinstance(townhall_memory[-1], dict) else {}
            if isinstance(latest.get("personal_conclusion", {}), dict) and latest.get("personal_conclusion"):
                payload["latest_personal_conclusion"] = dict(latest.get("personal_conclusion", {}))
        if experience_memory:
            payload["experience_memory"] = experience_memory
        return payload

    def _normalize_personal_conclusion(self, agent_id: str, src: Any) -> dict[str, Any]:
        raw = src if isinstance(src, dict) else {}
        summary = self._truncate_prompt_text(raw.get("summary", ""), max_len=220)
        focus_tasks = [
            str(item).strip()
            for item in raw.get("focus_tasks", [])
            if str(item).strip() in self.allowed_task_priority_keys
        ][:3] if isinstance(raw.get("focus_tasks", []), list) else []
        cautions = [
            self._truncate_prompt_text(item, max_len=140)
            for item in raw.get("cautions", [])
            if str(item).strip()
        ][:3] if isinstance(raw.get("cautions", []), list) else []
        commitment = self._truncate_prompt_text(raw.get("commitment", ""), max_len=180)
        return {
            "agent_id": agent_id,
            "summary": summary,
            "focus_tasks": focus_tasks,
            "cautions": cautions,
            "commitment": commitment,
        }


    def _normalize_memory_text_list(self, raw: Any, *, max_items: int = 4, max_len: int = 180) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [
            self._truncate_prompt_text(item, max_len=max_len)
            for item in raw
            if str(item).strip()
        ][:max_items]

    def _sanitize_memory_focus_tasks(self, raw: Any, *, max_items: int = 3) -> list[str]:
        if not isinstance(raw, list):
            return []
        return [
            str(item).strip()
            for item in raw
            if str(item).strip() in self.allowed_task_priority_keys
        ][:max_items]

    def _normalize_agent_memory_update(
        self,
        agent_id: str,
        src: Any,
        *,
        fallback_summary: str = "",
        fallback_focus_tasks: list[str] | None = None,
        fallback_commitment: str = "",
    ) -> dict[str, Any]:
        raw = src if isinstance(src, dict) else {}
        beliefs_raw = raw.get("beliefs", {}) if isinstance(raw.get("beliefs", {}), dict) else {}
        commitment_raw = raw.get("commitment", {}) if isinstance(raw.get("commitment", {}), dict) else {}
        episodic_raw = raw.get("episodic_entry", {}) if isinstance(raw.get("episodic_entry", {}), dict) else {}
        semantic_raw = raw.get("semantic_memory", {}) if isinstance(raw.get("semantic_memory", {}), dict) else {}
        rolling_raw = raw.get("rolling_summary", {}) if isinstance(raw.get("rolling_summary", {}), dict) else {}
        fallback_focus = list(fallback_focus_tasks or [])[:3]
        fallback_summary_text = self._truncate_prompt_text(fallback_summary, max_len=220)
        fallback_commitment_text = self._truncate_prompt_text(fallback_commitment, max_len=180)
        return {
            "agent_id": agent_id,
            "beliefs": {
                "agent_id": agent_id,
                "operational_focus": self._truncate_prompt_text(beliefs_raw.get("operational_focus", fallback_summary_text), max_len=220),
                "key_risks": self._normalize_memory_text_list(beliefs_raw.get("key_risks", []), max_items=3, max_len=160),
                "priority_hypotheses": self._normalize_memory_text_list(beliefs_raw.get("priority_hypotheses", []), max_items=3, max_len=160),
            },
            "commitment": {
                "agent_id": agent_id,
                "summary": self._truncate_prompt_text(commitment_raw.get("summary", fallback_commitment_text or fallback_summary_text), max_len=180),
                "focus_tasks": self._sanitize_memory_focus_tasks(commitment_raw.get("focus_tasks", fallback_focus)),
                "success_signals": self._normalize_memory_text_list(commitment_raw.get("success_signals", []), max_items=3, max_len=140),
            },
            "episodic_entry": {
                "agent_id": agent_id,
                "title": self._truncate_prompt_text(episodic_raw.get("title", f"Day memory for {agent_id}"), max_len=120),
                "summary": self._truncate_prompt_text(episodic_raw.get("summary", fallback_summary_text), max_len=220),
                "lessons": self._normalize_memory_text_list(episodic_raw.get("lessons", []), max_items=4, max_len=160),
                "evidence": self._normalize_memory_text_list(episodic_raw.get("evidence", []), max_items=4, max_len=160),
            },
            "semantic_memory": {
                "agent_id": agent_id,
                "specialization": self._normalize_memory_text_list(semantic_raw.get("specialization", []), max_items=4, max_len=150),
                "heuristics": self._normalize_memory_text_list(semantic_raw.get("heuristics", []), max_items=4, max_len=150),
                "anti_patterns": self._normalize_memory_text_list(semantic_raw.get("anti_patterns", []), max_items=4, max_len=150),
            },
            "rolling_summary": {
                "agent_id": agent_id,
                "summary": self._truncate_prompt_text(rolling_raw.get("summary", fallback_summary_text), max_len=220),
                "focus_tasks": self._sanitize_memory_focus_tasks(rolling_raw.get("focus_tasks", fallback_focus)),
                "cautions": self._normalize_memory_text_list(rolling_raw.get("cautions", []), max_items=3, max_len=140),
                "commitment": self._truncate_prompt_text(rolling_raw.get("commitment", fallback_commitment_text or fallback_summary_text), max_len=180),
            },
        }

    def _normalize_moderator_memory_update(
        self,
        src: Any,
        *,
        fallback_summary: str = "",
        fallback_focus_tasks: list[str] | None = None,
    ) -> dict[str, Any]:
        raw = src if isinstance(src, dict) else {}
        beliefs_raw = raw.get("shared_beliefs", {}) if isinstance(raw.get("shared_beliefs", {}), dict) else {}
        commitment_raw = raw.get("shared_commitments", {}) if isinstance(raw.get("shared_commitments", {}), dict) else {}
        episodic_raw = raw.get("episodic_entry", {}) if isinstance(raw.get("episodic_entry", {}), dict) else {}
        semantic_raw = raw.get("shared_semantic_memory", {}) if isinstance(raw.get("shared_semantic_memory", {}), dict) else {}
        rolling_raw = raw.get("shared_rolling_summary", {}) if isinstance(raw.get("shared_rolling_summary", {}), dict) else {}
        fallback_focus = list(fallback_focus_tasks or [])[:3]
        fallback_summary_text = self._truncate_prompt_text(fallback_summary, max_len=220)
        return {
            "shared_beliefs": {
                "system_focus": self._truncate_prompt_text(beliefs_raw.get("system_focus", fallback_summary_text), max_len=220),
                "key_risks": self._normalize_memory_text_list(beliefs_raw.get("key_risks", []), max_items=4, max_len=160),
                "priority_hypotheses": self._normalize_memory_text_list(beliefs_raw.get("priority_hypotheses", []), max_items=4, max_len=160),
            },
            "shared_commitments": {
                "summary": self._truncate_prompt_text(commitment_raw.get("summary", fallback_summary_text), max_len=180),
                "focus_tasks": self._sanitize_memory_focus_tasks(commitment_raw.get("focus_tasks", fallback_focus)),
                "success_signals": self._normalize_memory_text_list(commitment_raw.get("success_signals", []), max_items=4, max_len=140),
            },
            "episodic_entry": {
                "title": self._truncate_prompt_text(episodic_raw.get("title", "Moderator day memory"), max_len=120),
                "summary": self._truncate_prompt_text(episodic_raw.get("summary", fallback_summary_text), max_len=220),
                "lessons": self._normalize_memory_text_list(episodic_raw.get("lessons", []), max_items=4, max_len=160),
                "evidence": self._normalize_memory_text_list(episodic_raw.get("evidence", []), max_items=4, max_len=160),
            },
            "shared_semantic_memory": {
                "coordination_notes": self._normalize_memory_text_list(semantic_raw.get("coordination_notes", []), max_items=4, max_len=150),
                "heuristics": self._normalize_memory_text_list(semantic_raw.get("heuristics", []), max_items=4, max_len=150),
                "anti_patterns": self._normalize_memory_text_list(semantic_raw.get("anti_patterns", []), max_items=4, max_len=150),
                "unresolved_disagreements": self._normalize_memory_text_list(semantic_raw.get("unresolved_disagreements", []), max_items=4, max_len=150),
            },
            "shared_rolling_summary": {
                "summary": self._truncate_prompt_text(rolling_raw.get("summary", fallback_summary_text), max_len=220),
                "focus_tasks": self._sanitize_memory_focus_tasks(rolling_raw.get("focus_tasks", fallback_focus)),
                "open_questions": self._normalize_memory_text_list(rolling_raw.get("open_questions", []), max_items=4, max_len=140),
                "commitment": self._truncate_prompt_text(rolling_raw.get("commitment", fallback_summary_text), max_len=180),
            },
        }

    def _append_bounded(self, seq: list[dict[str, Any]], item: dict[str, Any]) -> None:
        seq.append(item)
        if len(seq) > self.memory_window_days:
            del seq[: len(seq) - self.memory_window_days]

    def _issue_summary(self, day_summary: dict[str, Any]) -> dict[str, Any]:
        summary = day_summary if isinstance(day_summary, dict) else {}
        return {
            "products": int(summary.get("products", 0)),
            "scrap_rate": round(float(summary.get("scrap_rate", 0.0)), 4),
            "machine_breakdowns": int(summary.get("machine_breakdowns", 0)),
            "inspection_backlog_end": int(summary.get("inspection_backlog_end", 0)),
            "avg_wip_material": round(float(summary.get("avg_wip_material", 0.0)), 3),
            "avg_wip_intermediate": round(float(summary.get("avg_wip_intermediate", 0.0)), 3),
            "station1_completions": int(summary.get("station1_completions", 0) or 0),
            "station2_completions": int(summary.get("station2_completions", 0) or 0),
            "inspection_passes": int(summary.get("inspection_passes", 0) or 0),
            "inspect_product_task_count": int(summary.get("inspect_product_task_count", 0) or 0),
            "station1_output_buffer_end": int(summary.get("station1_output_buffer_end", 0) or 0),
            "station2_output_buffer_end": int(summary.get("station2_output_buffer_end", 0) or 0),
            "agent_discharged_count": int(summary.get("agent_discharged_count", 0) or 0),
            "battery_delivery_count": int(summary.get("battery_delivery_count", 0) or 0),
            "days_since_last_product": int(summary.get("days_since_last_product", 0) or 0),
        }


    def _day_summary_prompt_view(self, day_summary: dict[str, Any]) -> dict[str, Any]:
        summary = day_summary if isinstance(day_summary, dict) else {}
        task_minutes = summary.get("task_minutes", {}) if isinstance(summary.get("task_minutes", {}), dict) else {}
        top_task_minutes = sorted(
            ((str(key), float(value or 0.0)) for key, value in task_minutes.items()),
            key=lambda item: float(item[1]),
            reverse=True,
        )
        compact = {
            "day": int(summary.get("day", 0) or 0),
            "products": int(summary.get("products", 0) or 0),
            "scrap_rate": round(float(summary.get("scrap_rate", 0.0) or 0.0), 4),
            "machine_breakdowns": int(summary.get("machine_breakdowns", 0) or 0),
            "avg_wip_material": round(float(summary.get("avg_wip_material", 0.0) or 0.0), 3),
            "avg_wip_intermediate": round(float(summary.get("avg_wip_intermediate", 0.0) or 0.0), 3),
            "inspection_backlog_end": int(summary.get("inspection_backlog_end", 0) or 0),
            "station1_completions": int(summary.get("station1_completions", 0) or 0),
            "station2_completions": int(summary.get("station2_completions", 0) or 0),
            "inspection_passes": int(summary.get("inspection_passes", 0) or 0),
            "inspect_product_task_count": int(summary.get("inspect_product_task_count", 0) or 0),
            "station1_output_buffer_end": int(summary.get("station1_output_buffer_end", 0) or 0),
            "station2_output_buffer_end": int(summary.get("station2_output_buffer_end", 0) or 0),
            "agent_discharged_count": int(summary.get("agent_discharged_count", 0) or 0),
            "battery_delivery_count": int(summary.get("battery_delivery_count", 0) or 0),
            "days_since_last_product": int(summary.get("days_since_last_product", 0) or 0),
            "top_task_minutes": [
                {"task_type": task_type, "minutes": round(minutes, 3)}
                for task_type, minutes in top_task_minutes[:4]
                if minutes > 0.0
            ],
        }
        pruned = self._prune_prompt_value(compact)
        return pruned if isinstance(pruned, dict) else {}

    def _discussion_signature(self, item: dict[str, Any]) -> str:
        signature = {
            "changed_norm_keys": item.get("changed_norm_keys", []),
            "issue_summary": item.get("issue_summary", {}),
            "consensus_proposals": [
                {
                    "proposed_norm_keys": proposal.get("proposed_norm_keys", []),
                    "proposed_priority_keys": proposal.get("proposed_priority_keys", []),
                    "supporting_agents": proposal.get("supporting_agents", []),
                }
                for proposal in item.get("consensus_proposals", [])
            ],
            "conflicting_proposals": [
                {
                    "proposed_norm_keys": proposal.get("proposed_norm_keys", []),
                    "proposed_priority_keys": proposal.get("proposed_priority_keys", []),
                }
                for proposal in item.get("conflicting_proposals", [])
            ],
        }
        return json.dumps(signature, ensure_ascii=False, sort_keys=True)

    def _memory_context(self, phase: str = "plan", *, for_openclaw_workspace: bool = False) -> dict[str, Any]:
        if self._openclaw_enabled() and not for_openclaw_workspace:
            return {}
        context: dict[str, Any] = {}
        if self.norms_enabled and self.shared_norms_memory:
            recent_norm_changes = []
            for item in self.shared_norms_memory[-2:]:
                delta = item.get("delta", {}) if isinstance(item.get("delta", {}), dict) else {}
                if not delta:
                    continue
                recent_norm_changes.append(
                    {
                        "day": int(item.get("day", 0) or 0),
                        "changed_keys": sorted(str(key) for key in delta.keys()),
                        "delta": delta,
                    }
                )
            if recent_norm_changes:
                context["recent_norm_changes"] = recent_norm_changes
        if self.shared_discussion_memory:
            context["recent_issue_summary"] = [
                {
                    "day": int(item.get("day", 0) or 0),
                    "issue_summary": item.get("issue_summary", {}),
                    "changed_norm_keys": item.get("changed_norm_keys", []),
                    "consensus_proposals": item.get("consensus_proposals", []),
                    "conflicting_proposals": item.get("conflicting_proposals", []),
                }
                for item in self.shared_discussion_memory[-2:]
            ]
        if phase == "townhall" and self.norms_enabled and self.shared_norms_memory:
            latest = self.shared_norms_memory[-1]
            if isinstance(latest, dict) and isinstance(latest.get("norms", {}), dict):
                context["latest_norm_snapshot"] = latest.get("norms", {})
        return context

    def _prune_prompt_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            compact: dict[str, Any] = {}
            for key, item in value.items():
                pruned = self._prune_prompt_value(item)
                if pruned is None:
                    continue
                if isinstance(pruned, (dict, list)) and not pruned:
                    continue
                compact[key] = pruned
            return compact
        if isinstance(value, list):
            compact_list = []
            for item in value:
                pruned = self._prune_prompt_value(item)
                if pruned is None:
                    continue
                if isinstance(pruned, (dict, list)) and not pruned:
                    continue
                compact_list.append(pruned)
            return compact_list
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def _llm_observation_view(self, observation: dict[str, Any]) -> dict[str, Any]:
        allowed_keys = ("time", "queues", "machines", "agents", "flow", "recent_history", "trends")
        trimmed = {key: observation.get(key) for key in allowed_keys if key in observation}
        time_block = trimmed.get("time", {})
        if isinstance(time_block, dict):
            trimmed["time"] = {
                key: time_block.get(key)
                for key in ("day", "total_days", "days_remaining", "day_progress", "horizon_remaining_min")
                if key in time_block
            }
        flow_block = trimmed.get("flow", {})
        if isinstance(flow_block, dict):
            flow_block = dict(flow_block)
            flow_block.pop("output_waiting_transfer", None)
            trimmed["flow"] = flow_block
        pruned = self._prune_prompt_value(trimmed)
        return pruned if isinstance(pruned, dict) else {}

    def _planner_machine_focus(self, observation: dict[str, Any]) -> dict[str, Any]:
        machines = observation.get("machines", {}) if isinstance(observation.get("machines", {}), dict) else {}
        by_id = machines.get("by_id", {}) if isinstance(machines.get("by_id", {}), dict) else {}
        focus: dict[str, Any] = {}
        for machine_id, raw in by_id.items():
            data = raw if isinstance(raw, dict) else {}
            owners = data.get("owners", {}) if isinstance(data.get("owners", {}), dict) else {}
            wait_reasons = data.get("wait_reasons", []) if isinstance(data.get("wait_reasons", []), list) else []
            include = bool(
                data.get("broken", False)
                or data.get("has_output_waiting_unload", False)
                or any(owners.values())
                or "ready_for_setup" in wait_reasons
                or str(data.get("state", "")) in {"BROKEN", "UNDER_REPAIR", "UNDER_PM", "DONE_WAIT_UNLOAD"}
            )
            if not include:
                continue
            focus[str(machine_id)] = {
                "station": data.get("station"),
                "state": data.get("state"),
                "broken": bool(data.get("broken", False)),
                "has_output_waiting_unload": bool(data.get("has_output_waiting_unload", False)),
                "wait_reasons": wait_reasons,
                "owners": owners,
                "minutes_since_last_pm": data.get("minutes_since_last_pm"),
                "minutes_since_failure_started": data.get("minutes_since_failure_started"),
            }
        return focus

    def _planner_agent_focus(self, observation: dict[str, Any]) -> dict[str, Any]:
        agents = observation.get("agents", {}) if isinstance(observation.get("agents", {}), dict) else {}
        by_id = agents.get("by_id", {}) if isinstance(agents.get("by_id", {}), dict) else {}
        focus: dict[str, Any] = {}
        for agent_id, raw in by_id.items():
            data = raw if isinstance(raw, dict) else {}
            include = bool(
                data.get("low_battery", False)
                or data.get("discharged", False)
                or data.get("awaiting_battery_from")
                or data.get("in_transit")
                or data.get("current_task_type")
            )
            if not include:
                continue
            focus[str(agent_id)] = {
                "location": data.get("location"),
                "status": data.get("status"),
                "battery_remaining_min": data.get("battery_remaining_min"),
                "low_battery": bool(data.get("low_battery", False)),
                "discharged": bool(data.get("discharged", False)),
                "awaiting_battery_from": data.get("awaiting_battery_from"),
                "current_task_type": data.get("current_task_type"),
                "carrying_item_type": data.get("carrying_item_type"),
                "in_transit": data.get("in_transit"),
            }
        return focus


    def _planner_observation_view(self, observation: dict[str, Any]) -> dict[str, Any]:
        base = self._llm_observation_view(observation)
        if not isinstance(base, dict):
            return {}
        machines = base.get("machines", {}) if isinstance(base.get("machines", {}), dict) else {}
        agents = base.get("agents", {}) if isinstance(base.get("agents", {}), dict) else {}
        planner_view = dict(base)
        planner_view["machines"] = {
            "summary": machines.get("summary", {}),
            "wait_reason_summary": machines.get("wait_reason_summary", {}),
            "focus_by_id": self._planner_machine_focus(observation),
        }
        planner_view["agents"] = {
            "summary": agents.get("summary", {}),
            "focus_by_id": self._planner_agent_focus(observation),
        }
        pruned = self._prune_prompt_value(planner_view)
        return pruned if isinstance(pruned, dict) else {}


    def _strategy_prompt_payload(self, strategy: StrategyState) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if getattr(strategy, "summary", ""):
            payload["summary"] = str(strategy.summary).strip()
        diagnosis = dict(getattr(strategy, "diagnosis", {}) or {})
        top_bottlenecks = diagnosis.get("top_bottlenecks", []) if isinstance(diagnosis.get("top_bottlenecks", []), list) else []
        top_limit = max(1, min(3, int(getattr(self, "detector_max_top_bottlenecks", 3) or 3)))
        if top_bottlenecks:
            payload["top_bottlenecks"] = top_bottlenecks[:top_limit]
        if not payload and strategy.notes:
            payload["notes"] = list(strategy.notes[:4])
        return payload

    def _job_plan_prompt_payload(self, job_plan: JobPlan) -> dict[str, Any]:
        ranked = sorted(
            ((str(key), float(value or 0.0)) for key, value in job_plan.task_priority_weights.items()),
            key=lambda item: float(item[1]),
            reverse=True,
        )
        return {
            "task_priority_weights": {key: round(float(value), 3) for key, value in job_plan.task_priority_weights.items()},
            "quotas": {key: int(value) for key, value in job_plan.quotas.items()},
            "rationale": str(job_plan.rationale or "").strip(),
            "top_weighted_tasks": [
                {"priority_key": key, "weight": round(float(value), 3)}
                for key, value in ranked[:3]
            ],
        }

    def _flatten_diagnosis_to_notes(self, summary: str, diagnosis: dict[str, Any]) -> list[str]:
        notes: list[str] = []
        if summary.strip():
            notes.append(f"Summary: {summary.strip()}")

        top_bottlenecks = diagnosis.get("top_bottlenecks", []) if isinstance(diagnosis.get("top_bottlenecks", []), list) else []
        if top_bottlenecks:
            rendered = []
            for item in top_bottlenecks[:3]:
                if isinstance(item, dict):
                    name = str(item.get("name", item.get("signal", ""))).strip()
                    rank = str(item.get("rank", "")).strip()
                    severity = str(item.get("severity", "")).strip()
                    why = str(item.get("why_it_limits_output", item.get("why_now", ""))).strip()
                    evidence = item.get("evidence", []) if isinstance(item.get("evidence", []), list) else []
                    evidence_bits = []
                    for ev in evidence[:2]:
                        if isinstance(ev, dict):
                            metric = str(ev.get("metric", ev.get("signal", ""))).strip()
                            value = str(ev.get("value", "")).strip()
                            pair = ":".join(part for part in (metric, value) if part)
                            if pair:
                                evidence_bits.append(pair)
                    line = name
                    if rank:
                        line = f"#{rank} {line}".strip()
                    if severity:
                        line = f"{line} [{severity}]".strip()
                    if evidence_bits:
                        line = f"{line}: {'; '.join(evidence_bits)}".strip()
                    if why:
                        line = f"{line} ({why})".strip()
                    if line:
                        rendered.append(line)
                else:
                    raw = str(item).strip()
                    if raw:
                        rendered.append(raw)
            if rendered:
                notes.append("Bottlenecks: " + "; ".join(rendered))

        return notes

    def _truncate_prompt_text(self, text: Any, max_len: int = 180) -> str:
        raw = str(text or "").strip()
        if len(raw) <= max_len:
            return raw
        return raw[: max_len - 3].rstrip() + "..."

    def _townhall_stage_lookup(self, stage_id: str) -> dict[str, Any]:
        stage_key = str(stage_id or "").strip().lower()
        for spec in self.TOWNHALL_STAGE_SPECS:
            if spec["stage_id"] == stage_key:
                return dict(spec)
        return dict(self.TOWNHALL_STAGE_SPECS[0])

    def _default_townhall_round_plan(self) -> list[dict[str, Any]]:
        # Fallback stays simple and short: diagnose first, synthesize last, and only
        # insert middle stages when the configured round budget allows it.
        if self.comm_rounds <= 0:
            return []
        fallback_stage_ids = ["diagnose"]
        if self.comm_rounds >= 2:
            fallback_stage_ids.append("synthesis")
        if self.comm_rounds >= 3:
            fallback_stage_ids.insert(1, "critique")
        if self.comm_rounds >= 4:
            fallback_stage_ids.insert(2, "alternatives")
        if self.comm_rounds >= 5:
            fallback_stage_ids.insert(3, "tradeoff")
        fallback_stage_ids = fallback_stage_ids[: self.comm_rounds]
        plan: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for stage_id in fallback_stage_ids:
            counts[stage_id] = counts.get(stage_id, 0) + 1
        positions: dict[str, int] = {}
        for round_no, stage_id in enumerate(fallback_stage_ids, start=1):
            spec = self._townhall_stage_lookup(stage_id)
            positions[stage_id] = positions.get(stage_id, 0) + 1
            plan.append(
                {
                    "round": round_no,
                    "stage_id": spec["stage_id"],
                    "step": spec["step"],
                    "label": spec["label"],
                    "focus": spec["focus"],
                    "stage_round_index": positions[stage_id],
                    "stage_round_count": counts.get(stage_id, 1),
                }
            )
        return plan

    def _build_townhall_round_plan(self, llm_obj: dict[str, Any], fallback: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
        raw_plan = llm_obj.get("round_plan", [])
        round_items = raw_plan if isinstance(raw_plan, list) else []
        built: list[dict[str, Any]] = []
        seen_rounds: set[int] = set()
        prev_step = 0
        for raw_item in round_items:
            if not isinstance(raw_item, dict):
                continue
            try:
                round_no = int(raw_item.get("round", 0) or 0)
            except (TypeError, ValueError):
                continue
            if round_no < 1 or round_no > self.comm_rounds or round_no in seen_rounds:
                continue
            stage_spec = self._townhall_stage_lookup(str(raw_item.get("stage_id", "")))
            if stage_spec["step"] < prev_step:
                continue
            objective = self._truncate_prompt_text(raw_item.get("objective", stage_spec["focus"]), max_len=220)
            if not objective:
                objective = stage_spec["focus"]
            built.append(
                {
                    "round": round_no,
                    "stage_id": stage_spec["stage_id"],
                    "step": stage_spec["step"],
                    "label": stage_spec["label"],
                    "focus": objective,
                    "stage_round_index": 1,
                    "stage_round_count": 1,
                }
            )
            seen_rounds.add(round_no)
            prev_step = stage_spec["step"]
        built.sort(key=lambda item: int(item["round"]))
        if not built:
            return fallback, self._truncate_prompt_text(llm_obj.get("moderator_note", ""), max_len=200)
        expected_rounds = list(range(1, len(built) + 1))
        actual_rounds = [int(item["round"]) for item in built]
        if actual_rounds != expected_rounds:
            return fallback, self._truncate_prompt_text(llm_obj.get("moderator_note", ""), max_len=200)
        if built[0]["stage_id"] != "diagnose":
            return fallback, self._truncate_prompt_text(llm_obj.get("moderator_note", ""), max_len=200)
        if built[-1]["stage_id"] != "synthesis":
            return fallback, self._truncate_prompt_text(llm_obj.get("moderator_note", ""), max_len=200)
        counts: dict[str, int] = {}
        for item in built:
            counts[item["stage_id"]] = counts.get(item["stage_id"], 0) + 1
        positions: dict[str, int] = {}
        for item in built:
            sid = item["stage_id"]
            positions[sid] = positions.get(sid, 0) + 1
            item["stage_round_index"] = positions[sid]
            item["stage_round_count"] = counts.get(sid, 1)
        return built, self._truncate_prompt_text(llm_obj.get("moderator_note", ""), max_len=200)

    def _townhall_round_plan(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        fallback = self._default_townhall_round_plan()
        stage_catalog = [
            {
                "stage_id": spec["stage_id"],
                "step": spec["step"],
                "label": spec["label"],
                "focus": spec["focus"],
            }
            for spec in self.TOWNHALL_STAGE_SPECS
        ]
        prompt = self._prompt(
            title="Plan the purpose of each townhall round for this day.",
            payload={
                "day_summary": self._day_summary_prompt_view(day_summary),
                "current_norms": norms,
                "language": self.communication_language,
                "max_rounds": self.comm_rounds,
                "available_stages": stage_catalog,
                "memory": self._memory_context("townhall"),
                "guardrails": {
                    "ordered_stages": [spec["stage_id"] for spec in self.TOWNHALL_STAGE_SPECS],
                    "round_plan_may_use_only_needed_stages": True,
                    "must_start_with": "diagnose",
                    "must_end_with": "synthesis",
                    "prefer_shortest_actionable_plan": True,
                    "repeat_stages_only_to_deepen_discussion": True,
                },
            },
            schema_hint='{"round_plan": [{"round": int, "stage_id": "one_of[diagnose,critique,alternatives,tradeoff,synthesis]", "objective": str}], "moderator_note": str}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt=self._shared_system_prompt(
                "You are a manufacturing townhall moderator planning the purpose of each discussion round.",
                [
                    "Use at most the configured maximum rounds and prefer the shortest actionable plan that still resolves the day's real issues.",
                    "Infer from the day_summary itself whether the discussion can end after diagnose -> synthesis or needs critique, alternatives, or tradeoff rounds.",
                    "Keep any used stages in the order diagnose -> critique -> alternatives -> tradeoff -> synthesis, but skip unnecessary middle stages whenever they do not add new information.",
                    "Extra rounds without genuinely new disagreement, evidence, or trade-off analysis are a planning error.",
                    "Start with diagnose and end with synthesis. Use only the stages that are actually needed for this day.",
                    self._communication_language_instruction(["moderator_note", "round_plan[].objective"]),
                ],
                include_task_family_semantics=False,
                include_norm_semantics=False,
            ),
            call_name="townhall_round_plan",
            context={"phase": "townhall_round_plan", "day": day_summary.get("day"), "max_rounds": self.comm_rounds},
        )
        return self._build_townhall_round_plan(llm_obj, fallback)

    def _default_townhall_contribution_type(self, stage_id: str) -> str:
        stage_spec = self._townhall_stage_lookup(stage_id)
        expected = stage_spec.get("expected_contributions", ())
        if isinstance(expected, (list, tuple)) and expected:
            contribution = str(expected[0]).strip()
            if contribution in self.TOWNHALL_CONTRIBUTION_TYPES:
                return contribution
        return "new_evidence"

    def _townhall_recent_highlights(self, transcript: list[dict[str, Any]], limit: int = 6) -> dict[str, Any]:
        if not transcript:
            return {
                "message_count": 0,
                "consensus_proposals": [],
                "conflicting_proposals": [],
                "latest_points": [],
                "stage_progress": [],
            }

        recent_window = transcript[-self.comm_max_transcript :]
        proposal_groups: dict[str, dict[str, Any]] = {}
        latest_by_agent: dict[str, dict[str, Any]] = {}

        for message in recent_window:
            aid = str(message.get("agent_id", "")).strip()
            if aid:
                latest_by_agent[aid] = message
            proposal = message.get("proposal", {}) if isinstance(message.get("proposal", {}), dict) else {}
            norm_updates = proposal.get("norm_updates", {}) if isinstance(proposal.get("norm_updates", {}), dict) else {}
            priority_updates = proposal.get("priority_updates", {}) if isinstance(proposal.get("priority_updates", {}), dict) else {}
            if not norm_updates and not priority_updates:
                continue
            proposal_key = json.dumps(
                {
                    "norm_updates": norm_updates,
                    "priority_updates": priority_updates,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            grouped = proposal_groups.setdefault(
                proposal_key,
                {
                    "round": int(message.get("round", 0) or 0),
                    "stage_id": str(message.get("stage_id", "")).strip(),
                    "proposed_norm_keys": sorted(str(key) for key in norm_updates.keys()),
                    "proposed_priority_keys": sorted(str(key) for key in priority_updates.keys()),
                    "supporting_agents": [],
                    "sample_utterance": self._truncate_prompt_text(message.get("utterance", ""), max_len=180),
                },
            )
            if aid and aid not in grouped["supporting_agents"]:
                grouped["supporting_agents"].append(aid)

        ranked_groups = sorted(
            proposal_groups.values(),
            key=lambda item: (len(item.get("supporting_agents", [])), item.get("round", 0)),
        )
        consensus: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for item in ranked_groups:
            proposal_entry = {
                "round": int(item.get("round", 0) or 0),
                "stage_id": str(item.get("stage_id", "")).strip(),
                "proposed_norm_keys": item.get("proposed_norm_keys", []),
                "proposed_priority_keys": item.get("proposed_priority_keys", []),
                "supporting_agents": sorted(item.get("supporting_agents", [])),
                "sample_utterance": item.get("sample_utterance", ""),
            }
            if len(proposal_entry["supporting_agents"]) >= 2:
                consensus.append(proposal_entry)
            else:
                conflicts.append(proposal_entry)

        latest_points = [
            {
                "round": int(message.get("round", 0) or 0),
                "stage_id": str(message.get("stage_id", "")).strip(),
                "agent_id": aid,
                "contribution_type": str(message.get("contribution_type", "")).strip(),
                "novelty_basis": self._truncate_prompt_text(message.get("novelty_basis", ""), max_len=120),
                "utterance_summary": self._truncate_prompt_text(message.get("utterance", ""), max_len=140),
            }
            for aid, message in sorted(latest_by_agent.items())
        ]
        stage_progress: list[dict[str, Any]] = []
        stage_seen: set[str] = set()
        for message in reversed(recent_window):
            stage_id = str(message.get("stage_id", "")).strip()
            if not stage_id or stage_id in stage_seen:
                continue
            spec = self._townhall_stage_lookup(stage_id)
            stage_progress.append(
                {
                    "stage_id": stage_id,
                    "label": spec["label"],
                    "latest_round": int(message.get("round", 0) or 0),
                }
            )
            stage_seen.add(stage_id)
        stage_progress.reverse()
        return {
            "message_count": len(transcript),
            "consensus_proposals": consensus[-limit:],
            "conflicting_proposals": conflicts[-limit:],
            "latest_points": latest_points[-limit:],
            "stage_progress": stage_progress[-limit:],
        }



    def _update_memory(
        self,
        day_summary: dict[str, Any],
        updated_norms: dict[str, Any],
        transcript: list[dict[str, Any]],
        summary: str,
        personal_conclusions: dict[str, dict[str, Any]] | None = None,
        agent_memory_updates: dict[str, dict[str, Any]] | None = None,
        moderator_memory_update: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        day = int(day_summary.get("day", len(self.shared_discussion_memory) + 1))
        previous_norms = {}
        if self.norms_enabled and self.shared_norms_memory:
            latest = self.shared_norms_memory[-1]
            if isinstance(latest, dict) and isinstance(latest.get("norms", {}), dict):
                previous_norms = dict(latest.get("norms", {}))

        norm_delta: dict[str, Any] = {}
        if self.norms_enabled:
            for key, new_value in updated_norms.items():
                old_value = previous_norms.get(key)
                if old_value != new_value:
                    norm_delta[str(key)] = {"from": old_value, "to": new_value}
            norms_item = {"day": day, "norms": dict(updated_norms), "delta": norm_delta}
            self._append_bounded(self.shared_norms_memory, norms_item)

        highlights = self._townhall_recent_highlights(transcript, limit=3) if transcript else {
            "consensus_proposals": [],
            "conflicting_proposals": [],
        }
        normalized_agent_updates = agent_memory_updates if isinstance(agent_memory_updates, dict) else {}
        normalized_moderator_update = moderator_memory_update if isinstance(moderator_memory_update, dict) else {}
        discussion_item = {
            "day": day,
            "issue_summary": self._issue_summary(day_summary),
            "changed_norm_keys": sorted(norm_delta.keys()),
            "consensus_proposals": highlights.get("consensus_proposals", [])[:2],
            "conflicting_proposals": highlights.get("conflicting_proposals", [])[:2],
            "synthesis_summary": self._truncate_prompt_text(summary, max_len=220),
            "moderator_memory_update": normalized_moderator_update,
        }
        discussion_signature = self._discussion_signature(discussion_item)
        previous_signature = ""
        if self.shared_discussion_memory and isinstance(self.shared_discussion_memory[-1], dict):
            previous_signature = str(self.shared_discussion_memory[-1].get("signature", ""))
        duplicate_skipped = False
        if discussion_signature != previous_signature:
            discussion_item["signature"] = discussion_signature
            self._append_bounded(self.shared_discussion_memory, discussion_item)
        else:
            duplicate_skipped = True

        if self.include_agent_memory:
            agent_experience = day_summary.get("agent_experience", {}) if isinstance(day_summary.get("agent_experience", {}), dict) else {}
            conclusions = personal_conclusions if isinstance(personal_conclusions, dict) else {}
            for aid in self.agent_ids:
                utterances = [
                    self._truncate_prompt_text(msg.get("utterance", ""), max_len=120)
                    for msg in transcript
                    if str(msg.get("agent_id", "")).strip() == aid and str(msg.get("utterance", "")).strip()
                ]
                agent_item = {
                    "day": day,
                    "summary": self._truncate_prompt_text(summary, max_len=160),
                    "recent_points": utterances[-2:],
                    "current_priority_profile": self.agent_priority_multipliers.get(aid, {}),
                }
                conclusion = conclusions.get(aid, {}) if isinstance(conclusions.get(aid, {}), dict) else {}
                if conclusion:
                    agent_item["personal_conclusion"] = conclusion
                if isinstance(normalized_agent_updates.get(aid), dict):
                    agent_item["memory_update"] = normalized_agent_updates.get(aid)
                self._append_bounded(self.agent_memories[aid], agent_item)
                raw_experience = agent_experience.get(aid, {}) if isinstance(agent_experience.get(aid, {}), dict) else {}
                experience_item = {
                    "day": day,
                    "top_completed_task_families": raw_experience.get("top_completed_task_families", []),
                    "contribution_signals": raw_experience.get("contribution_signals", {}),
                    "recent_task_events": raw_experience.get("recent_task_events", []),
                    "current_priority_profile": self.agent_priority_multipliers.get(aid, {}),
                }
                if isinstance(normalized_agent_updates.get(aid), dict):
                    experience_item["memory_update"] = normalized_agent_updates.get(aid)
                self._append_bounded(self.agent_experience_memory[aid], experience_item)

        self._sync_openclaw_workspace_memory(
            day_summary=day_summary,
            updated_norms=updated_norms,
            transcript=transcript,
            summary=summary,
            personal_conclusions=personal_conclusions,
            agent_memory_updates=normalized_agent_updates,
            moderator_memory_update=normalized_moderator_update,
        )

        return {
            "day": day,
            "memory_window_days": self.memory_window_days,
            "norms_memory_size": len(self.shared_norms_memory),
            "discussion_memory_size": len(self.shared_discussion_memory),
            "changed_norm_keys": sorted(norm_delta.keys()),
            "duplicate_discussion_skipped": duplicate_skipped,
            "agent_memory_update_count": len(normalized_agent_updates),
            "moderator_memory_update_present": bool(normalized_moderator_update),
        }

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped:
            return None

        for candidate in self._json_candidates(stripped):
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
        return None

    def _json_candidates(self, text: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def _push(value: str) -> None:
            item = value.strip()
            if item and item not in seen:
                seen.add(item)
                candidates.append(item)

        _push(text)
        cleaned = self._strip_code_fences(text)
        _push(cleaned)

        balanced = self._extract_first_balanced_object(cleaned)
        if balanced:
            _push(balanced)

        for item in list(candidates):
            repaired = self._repair_json_like_text(item)
            _push(repaired)
            repaired_balanced = self._extract_first_balanced_object(repaired)
            if repaired_balanced:
                _push(repaired_balanced)

        return candidates

    def _strip_code_fences(self, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)
        return stripped.strip()

    def _extract_first_balanced_object(self, text: str) -> str:
        start = text.find("{")
        if start < 0:
            return ""
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : idx + 1]
        return ""
    def _repair_json_like_text(self, text: str) -> str:
        repaired = text.strip()
        repaired = repaired.replace("\ufeff", "")
        repaired = repaired.replace(chr(8220), """).replace(chr(8221), """)
        repaired = repaired.replace(chr(8216), "'").replace(chr(8217), "'")
        repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
        repaired = re.sub(r"\bNone\b", "null", repaired)
        repaired = re.sub(r"\bTrue\b", "true", repaired)
        repaired = re.sub(r"\bFalse\b", "false", repaired)
        return repaired.strip()

    def _native_prompt_from_messages(self, messages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "user")).strip().upper() or "USER"
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            parts.append(f"{role}:\n{content}")
        return "\n\n".join(parts).strip()

    def _call_llm_json(
        self,
        user_prompt: str,
        system_prompt: str,
        *,
        call_name: str,
        context: dict[str, Any] | None = None,
        required_keys: list[str] | tuple[str, ...] | dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers_for_log = {"Content-Type": "application/json"}
        started_ts = time.time()
        started_at_utc = datetime.now(timezone.utc).isoformat()
        with self._llm_exchange_lock:
            self._llm_call_seq += 1
            call_id = self._llm_call_seq
        repair_instruction = (
            "Your previous reply was not valid JSON. Return only one valid JSON object that matches the requested "
            "schema. Start with { and end with }. Do not add markdown, code fences, comments, explanations, or trailing commas."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        payload: dict[str, Any] | None = None
        content = ""
        error_message = ""
        status = "ok"
        body: dict[str, Any] = {}
        url = self.server_url.rstrip("/") + "/chat/completions"
        call_transport = self._openclaw_transport_for_call(call_name) if self._openclaw_enabled() else "chat_compat"
        current_transport = call_transport
        if self._openclaw_enabled() and current_transport != "native_local":
            raise RuntimeError("OpenClaw native_local-only mode guard: non-native transport requested during call.")
        native_fallback_used = False
        native_default_contract_used = False
        native_default_contract_fields: list[str] = []
        native_default_contract_reason = ""
        native_repair_prompt = ""
        transport_attempts: list[str] = []
        attempts = 0
        expected_contract: dict[str, str] = {}
        if isinstance(required_keys, dict):
            expected_contract = {str(key).strip(): str(value).strip() for key, value in required_keys.items() if str(key).strip()}
        else:
            expected_contract = {str(item).strip(): "" for item in (required_keys or []) if str(item).strip()}
        placeholder_values = {"ready", "ok", "okay", "acknowledged", "understood", "heartbeat_ok"}

        def _default_contract_value(type_hint: str) -> Any:
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

        def _coerce_contract_value(value: Any, type_hint: str) -> Any:
            hint = str(type_hint or "").strip().lower()
            if hint.startswith("list"):
                if isinstance(value, list):
                    return value
                if value in {None, ""}:
                    return []
                return [str(value)]
            if hint.startswith("dict"):
                return value if isinstance(value, dict) else {}
            if hint.startswith("bool"):
                return bool(value)
            if hint.startswith("float"):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return 0.0
            if hint.startswith("int"):
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
            return str(value) if value not in {None} else ""

        def _default_contract() -> dict[str, Any]:
            return {
                str(key).strip(): _default_contract_value(type_hint)
                for key, type_hint in expected_contract.items()
            }

        def _native_contract_repair_prompt(contract_issues: list[str] | None = None) -> str:
            issue_text = ", ".join(contract_issues or []) or "invalid_json"
            phase_name = str((context or {}).get("phase", call_name or "")).strip().lower()
            if phase_name == "manager_bottleneck_detector":
                detector_count = max(1, min(3, int(getattr(self, "detector_max_top_bottlenecks", 3) or 3)))
                return (
                    "Previous reply violated the bottleneck detector JSON contract ("
                    + issue_text
                    + "). Re-read facts/current_request.json and facts/current_response_template.json in your workspace. "
                    + "Return exactly one JSON object only. "
                    + f"For the bottleneck detector phase, emit summary as a short bottleneck diagnosis string and top_bottlenecks as a list containing exactly {detector_count} objects. "
                    + "Each top_bottlenecks entry must include name, rank, severity, evidence[{metric,value}], and why_it_limits_output. "
                    + "Do not output watchouts, candidate_actions, reason_trace, worker_roles, or category risk lists. "
                    + "If evidence is thin, still return the exact number of bottlenecks by including weaker lower-severity constraints instead of returning fewer entries. "
                    + "If evidence is missing, use [] or {} with the correct type, never placeholder prose."
                )
            if phase_name == "manager_diagnosis_evaluator":
                return (
                    "Previous reply violated the diagnosis evaluator JSON contract ("
                    + issue_text
                    + "). Re-read facts/current_request.json and facts/current_response_template.json in your workspace. "
                    + "Return exactly one JSON object only. "
                    + "For manager_diagnosis_evaluator, emit only verdict, summary, and revision_requests. "
                    + "verdict must be accept or request_revision. "
                    + "If verdict=accept, revision_requests must be an empty list. "
                    + "If verdict=request_revision, revision_requests must contain actionable objects with target_rank, issue_type, issue, requested_change, and evidence. "
                    + "Do not output plans, worker assignments, reason_trace, or generic prose."
                )
            if phase_name == "manager_daily_planner":
                return (
                    "Previous reply violated the daily planner JSON contract ("
                    + issue_text
                    + "). Re-read facts/current_request.json and facts/current_response_template.json in your workspace. "
                    + "Return exactly one JSON object only. "
                    + "For manager_daily_planner, emit only plan_mode, weight_updates, queue_add, reason_trace, and detector_alignment. "
                    + "detector_alignment must be follow, partial_override, or override. "
                    + "Use execution_state, closure_signals, constraint_signals, and detector_hypothesis as evidence. "
                    + "If a detector bottleneck conflicts with stronger closure evidence, you may override it in reason_trace using detector_relation. "
                    + "Do not output mailbox_add, agent_multiplier_updates, maintain_current_plan, stability_reason, or generic prose reasoning. Use the correct empty type only."
                )
            return (
                "Previous reply violated the required JSON contract ("
                + issue_text
                + "). Re-read facts/current_request.json and facts/current_response_template.json in your workspace. "
                + "Return exactly one JSON object matching current_response_template.json. "
                + "Fill every required key. If evidence is missing, use the correct empty value type instead of placeholder text. "
                + "Do not output prose, questions, markdown, HEARTBEAT_OK, ready, ok, acknowledged, or understood."
            )

        def _exchange_record(*, parsed: dict[str, Any], error: str) -> dict[str, Any]:
            transport_used = transport_attempts[-1] if transport_attempts else call_transport
            return {
                "call_id": call_id,
                "call_name": call_name,
                "status": status,
                "started_at_utc": started_at_utc,
                "latency_sec": round(time.time() - started_ts, 3),
                "latency_ms": round((time.time() - started_ts) * 1000.0, 2),
                "attempt_count": int(attempts),
                "attempt_durations_ms": list(attempt_latencies_ms),
                "repair_turn_count": max(0, int(attempts) - 1),
                "context": context or {},
                "transport_requested": call_transport,
                "transport_used": transport_used,
                "transport_attempts": list(transport_attempts),
                "native_fallback_used": bool(native_fallback_used),
                "native_default_contract_used": bool(native_default_contract_used),
                "native_default_contract_fields": list(native_default_contract_fields),
                "native_default_contract_reason": str(native_default_contract_reason or ""),
                "backend_health": dict(latest_backend_health) if isinstance(latest_backend_health, dict) else {},
                "backend_health_ok": bool(latest_backend_health.get("ok", False)) if isinstance(latest_backend_health, dict) else False,
                "backend_health_reason": str(latest_backend_health.get("reason", "") or ""),
                "attempt_errors": list(attempt_errors),
                "request": {
                    "url": url,
                    "headers": headers_for_log,
                    "payload": body,
                },
                "response": payload if isinstance(payload, dict) else {},
                "response_text": content,
                "parsed": parsed,
                "error": error,
            }

        attempt_latencies_ms: list[float] = []
        attempt_errors: list[str] = []
        latest_backend_health: dict[str, Any] = {}

        def _return_default(reason: str, *, fields: list[str] | None = None) -> dict[str, Any]:
            nonlocal status
            nonlocal native_default_contract_used
            nonlocal native_default_contract_reason
            nonlocal native_default_contract_fields
            nonlocal error_message
            status = "error"
            error_message = reason
            if self._openclaw_enabled():
                native_default_contract_used = False
                native_default_contract_fields = list(fields if fields is not None else expected_contract.keys()) if expected_contract else []
                native_default_contract_reason = reason[:240]
                self._record_llm_exchange(_exchange_record(parsed={}, error=error_message))
                raise RuntimeError(reason)
            if expected_contract:
                native_default_contract_used = True
                native_default_contract_fields = list(fields if fields is not None else expected_contract.keys())
                native_default_contract_reason = reason[:240]
                fallback = _default_contract()
                self._record_llm_exchange(_exchange_record(parsed=fallback, error=error_message))
                return fallback
            self._record_llm_exchange(_exchange_record(parsed={}, error=error_message))
            return {}

        try:
            while attempts < 3:
                attempt_start = time.time()
                transport_attempts.append(current_transport)

                if self._openclaw_enabled():
                    if current_transport != "native_local":
                        return _return_default(
                            "OpenClaw native_local-only mode guard: non-native transport requested during call.",
                            fields=list(expected_contract.keys()),
                        )
                    latest_backend_health = self._openclaw_backend_health()
                    if not latest_backend_health.get("ok", False):
                        attempt_errors.append(f"health_check_failed:{latest_backend_health.get('reason', '')}")
                        recovery = self._openclaw_recover_runtime(call_name=call_name, context=context)
                        latest_backend_health = dict(recovery.get("backend_health", latest_backend_health)) if isinstance(recovery, dict) else dict(latest_backend_health)
                        if isinstance(recovery, dict):
                            attempt_errors.append(f"health_recovery:{recovery.get('reason', '')}")
                            for recovery_attempt in recovery.get("attempts", []) or []:
                                stage = str((recovery_attempt or {}).get("stage", "recovery")).strip() or "recovery"
                                ready = (recovery_attempt or {}).get("readycheck", {}) if isinstance(recovery_attempt, dict) else {}
                                if isinstance(ready, dict) and ready:
                                    attempt_errors.append(f"{stage}:{ready.get('reason', '')}")
                        if not latest_backend_health.get("ok", False):
                            attempt_errors.append("health_recovery_proceeding_with_native_call")

                    agent_id = self._openclaw_agent_for_call(call_name, context)
                    session_key = self._openclaw_turn_session_key(agent_id, call_name, context)
                    native_user_prompt = user_prompt if attempts == 0 else (native_repair_prompt or _native_contract_repair_prompt())
                    try:
                        payload, content, url, headers_for_log, body = self.openclaw_client.native_agent_turn(
                            system_prompt=system_prompt,
                            user_prompt=native_user_prompt,
                            agent_id=agent_id,
                            session_key=session_key,
                            thinking=self.openclaw_native_thinking,
                        )
                    except Exception as exc:
                        attempt_errors.append(f"native_attempt:{type(exc).__name__}:{exc}")
                        attempt_latencies_ms.append(round((time.time() - attempt_start) * 1000.0, 3))
                        native_repair_prompt = _native_contract_repair_prompt([f"transport_exception:{type(exc).__name__}"])
                        if attempts < 2:
                            attempts += 1
                            continue
                        return _return_default(
                            f"Native call failed before satisfying JSON contract after retries: {type(exc).__name__}: {exc}",
                            fields=list(expected_contract.keys()),
                        )
                else:
                    headers_for_log = {"Content-Type": "application/json"}
                    req_headers = {"Content-Type": "application/json"}
                    if self.api_key:
                        req_headers["Authorization"] = f"Bearer {self.api_key}"
                        headers_for_log["Authorization"] = "Bearer ***"
                    body = {
                        "model": self.model,
                        "temperature": self.temperature,
                        "max_tokens": self.max_tokens,
                        "messages": messages,
                    }
                    raw = json.dumps(body).encode("utf-8")
                    req = urllib.request.Request(url=url, data=raw, headers=req_headers, method="POST")
                    try:
                        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                            payload = json.loads(resp.read().decode("utf-8"))
                        try:
                            content = str(payload["choices"][0]["message"]["content"])
                        except (KeyError, IndexError, TypeError) as exc:
                            return _return_default(f"LLM response format error: {exc}", fields=list(expected_contract.keys()))
                    except Exception as exc:
                        attempt_errors.append(f"chat_attempt:{type(exc).__name__}:{exc}")
                        attempt_latencies_ms.append(round((time.time() - attempt_start) * 1000.0, 3))
                        if attempts < 2 and current_transport == "chat_compat":
                            attempts += 1
                            messages = messages + [
                                {"role": "assistant", "content": content},
                                {"role": "user", "content": repair_instruction},
                            ]
                            continue
                        return _return_default(
                            f"LLM call failed before satisfying JSON contract: {type(exc).__name__}: {exc}",
                            fields=list(expected_contract.keys()),
                        )

                attempt_latencies_ms.append(round((time.time() - attempt_start) * 1000.0, 3))
                parsed = self._extract_json_object(content)

                if isinstance(parsed, dict):
                    if expected_contract:
                        normalized: dict[str, Any] = {}
                        contract_issues: list[str] = []
                        default_fields: list[str] = []
                        for key, type_hint in expected_contract.items():
                            hint = str(type_hint or "").strip().lower()
                            raw_value = parsed.get(key) if key in parsed else None
                            if key not in parsed:
                                normalized[key] = _default_contract_value(type_hint)
                                contract_issues.append(f"missing:{key}")
                                default_fields.append(key)
                                continue

                            coerced = _coerce_contract_value(raw_value, type_hint)
                            if hint.startswith("list") and not isinstance(raw_value, list):
                                contract_issues.append(f"coerced_list:{key}")
                                default_fields.append(key)
                            elif hint.startswith("list[") and isinstance(raw_value, list):
                                if "dict" in hint and any(not isinstance(item, dict) for item in raw_value):
                                    contract_issues.append(f"coerced_list_dict:{key}")
                                    default_fields.append(key)
                                elif "str" in hint and any(not isinstance(item, str) for item in raw_value):
                                    contract_issues.append(f"coerced_list_str:{key}")
                                    default_fields.append(key)
                            elif hint.startswith("dict") and not isinstance(raw_value, dict):
                                contract_issues.append(f"coerced_dict:{key}")
                                default_fields.append(key)
                            elif hint.startswith("bool") and not isinstance(raw_value, bool):
                                contract_issues.append(f"coerced_bool:{key}")
                                default_fields.append(key)
                            elif hint.startswith("float") and not isinstance(raw_value, (int, float)):
                                contract_issues.append(f"coerced_float:{key}")
                                default_fields.append(key)
                            elif hint.startswith("int") and not isinstance(raw_value, int):
                                contract_issues.append(f"coerced_int:{key}")
                                default_fields.append(key)
                            elif hint.startswith("str") and str(coerced).strip().lower() in placeholder_values:
                                coerced = ""
                                contract_issues.append(f"placeholder:{key}")
                                default_fields.append(key)
                            normalized[key] = coerced

                        if contract_issues:
                            if attempts < 2:
                                native_repair_prompt = _native_contract_repair_prompt(contract_issues)
                                attempts += 1
                                if self._openclaw_enabled():
                                    continue
                                messages = messages + [
                                    {"role": "assistant", "content": content},
                                    {"role": "user", "content": repair_instruction},
                                ]
                                continue
                            return _return_default(
                                f"Native JSON contract violation: {' ,'.join(contract_issues)[:240]}",
                                fields=default_fields,
                            )
                        self._record_llm_exchange(_exchange_record(parsed=normalized, error=""))
                        return normalized

                    self._record_llm_exchange(_exchange_record(parsed=parsed, error=""))
                    return parsed

                if attempts >= 2:
                    return _return_default(
                        "JSON parse failed after retries." if self._openclaw_enabled() else "Failed to parse JSON object from LLM response.",
                        fields=list(expected_contract.keys()),
                    )

                attempts += 1
                if self._openclaw_enabled():
                    native_repair_prompt = _native_contract_repair_prompt(["invalid_json"])
                else:
                    messages = messages + [
                        {"role": "assistant", "content": content},
                        {"role": "user", "content": repair_instruction},
                    ]
        except RuntimeError as exc:
            if self._openclaw_enabled():
                raise
            return _return_default(
                f"LLM call failed: {type(exc).__name__}: {exc}",
                fields=list(expected_contract.keys()),
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return _return_default(
                f"Native call failed before satisfying JSON contract: {type(exc).__name__}: {exc}" if self._openclaw_enabled() and expected_contract else f"LLM call failed: {type(exc).__name__}: {exc}",
                fields=list(expected_contract.keys()),
            )
        except Exception as exc:
            return _return_default(
                f"Unexpected LLM failure: {type(exc).__name__}: {exc}",
                fields=list(expected_contract.keys()),
            )

        return _return_default(
            "LLM call exhausted retry budget without a valid response.",
            fields=list(expected_contract.keys()),
        )
    @staticmethod
    def _as_float_map(src: Any, base: dict[str, float]) -> dict[str, float]:
        out = dict(base)
        if isinstance(src, dict):
            for k, v in src.items():
                try:
                    out[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
        return out

    @staticmethod
    def _as_int_map(src: Any, base: dict[str, int]) -> dict[str, int]:
        out = dict(base)
        if isinstance(src, dict):
            for k, v in src.items():
                try:
                    out[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        return out

    @staticmethod
    def _as_str_list(src: Any, fallback: list[str]) -> list[str]:
        if not isinstance(src, list):
            return fallback
        out: list[str] = []
        for x in src:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out if out else fallback

    @staticmethod
    def _clamp_float(value: Any, lower: float, upper: float, fallback: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return float(fallback)
        return float(min(max(parsed, lower), upper))

    @staticmethod
    def _clamp_int(value: Any, lower: int, upper: int, fallback: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return int(fallback)
        return int(min(max(parsed, lower), upper))

    def _sanitize_task_priority_weights(self, src: Any, fallback: dict[str, float]) -> dict[str, float]:
        out = dict(fallback)
        if not isinstance(src, dict):
            return out
        for key in self.allowed_task_priority_keys:
            if key in src:
                out[key] = self._clamp_float(
                    src.get(key),
                    self.task_priority_weight_min,
                    self.task_priority_weight_max,
                    out.get(key, 1.0),
                )
        return out

    def _sanitize_priority_updates(self, src: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        if not isinstance(src, dict):
            return out
        for key in self.allowed_task_priority_keys:
            if key in src:
                out[key] = self._clamp_float(
                    src.get(key),
                    self.urgent_priority_update_min,
                    self.urgent_priority_update_max,
                    1.0,
                )
        return out

    def _sanitize_quotas(self, src: Any, fallback: dict[str, int]) -> dict[str, int]:
        out = dict(fallback)
        if not isinstance(src, dict):
            return out
        for key, fallback_value in fallback.items():
            if key in src:
                out[key] = self._clamp_int(
                    src.get(key),
                    self.quota_min,
                    int(self.quota_max.get(key, max(self.quota_min, fallback_value))),
                    fallback_value,
                )
        return out

    def _sanitize_norms(self, src: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        if not self.norms_enabled:
            return {}
        updated = dict(fallback)
        if not isinstance(src, dict):
            return updated
        if "min_pm_per_machine_per_day" in src:
            updated["min_pm_per_machine_per_day"] = self._clamp_int(
                src.get("min_pm_per_machine_per_day"),
                self.min_pm_norm_min,
                self.min_pm_norm_max,
                int(fallback.get("min_pm_per_machine_per_day", self.min_pm_norm_min)),
            )
        if "inspect_product_priority_weight" in src:
            updated["inspect_product_priority_weight"] = round(
                self._clamp_float(
                    src.get("inspect_product_priority_weight"),
                    self.inspect_product_priority_weight_min,
                    self.inspect_product_priority_weight_max,
                    float(fallback.get("inspect_product_priority_weight", 1.0)),
                ),
                3,
            )
        if "inspection_backlog_target" in src:
            updated["inspection_backlog_target"] = self._clamp_int(
                src.get("inspection_backlog_target"),
                self.inspection_backlog_target_min,
                self.inspection_backlog_target_max,
                int(fallback.get("inspection_backlog_target", self.inspection_backlog_target_min)),
            )
        if "max_output_buffer_target" in src:
            updated["max_output_buffer_target"] = self._clamp_int(
                src.get("max_output_buffer_target"),
                self.max_output_buffer_target_min,
                self.max_output_buffer_target_max,
                int(fallback.get("max_output_buffer_target", self.max_output_buffer_target_min)),
            )
        if "battery_reserve_min" in src:
            updated["battery_reserve_min"] = round(
                self._clamp_float(
                    src.get("battery_reserve_min"),
                    self.battery_reserve_min_min,
                    self.battery_reserve_min_max,
                    float(fallback.get("battery_reserve_min", self.battery_reserve_min_min)),
                ),
                3,
            )
        return updated

    def _llm_guardrails_payload(self, phase: str = "plan") -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if phase in {"plan", "urgent", "townhall", "coordination", "norms"}:
            payload["allowed_task_priority_keys"] = list(self.allowed_task_priority_keys)
        if phase in {"townhall", "coordination", "norms", "plan"}:
            payload["allowed_agent_ids"] = list(self.agent_ids)
            payload["agent_priority_multiplier_range"] = {
                "min": self.agent_priority_multiplier_min,
                "max": self.agent_priority_multiplier_max,
            }
        if phase == "plan":
            payload["task_priority_weight_range"] = {
                "min": self.task_priority_weight_min,
                "max": self.task_priority_weight_max,
            }
            payload["allowed_quota_keys"] = list(self.quota_max.keys())
            payload["quota_range"] = {
                "min": self.quota_min,
                "max_by_key": dict(self.quota_max),
            }
        elif phase == "urgent":
            payload["priority_update_range"] = {
                "min": self.urgent_priority_update_min,
                "max": self.urgent_priority_update_max,
            }
        if self.norms_enabled and phase in {"norms", "townhall", "coordination"}:
            payload["allowed_norm_keys"] = list(self.allowed_norm_keys)
            payload["norm_ranges"] = {
                "min_pm_per_machine_per_day": {
                    "min": self.min_pm_norm_min,
                    "max": self.min_pm_norm_max,
                },
                "inspect_product_priority_weight": {
                    "min": self.inspect_product_priority_weight_min,
                    "max": self.inspect_product_priority_weight_max,
                },
                "inspection_backlog_target": {
                    "min": self.inspection_backlog_target_min,
                    "max": self.inspection_backlog_target_max,
                },
                "max_output_buffer_target": {
                    "min": self.max_output_buffer_target_min,
                    "max": self.max_output_buffer_target_max,
                },
                "battery_reserve_min": {
                    "min": self.battery_reserve_min_min,
                    "max": self.battery_reserve_min_max,
                },
            }
        return payload

    def _default_job_plan(self, norms: dict[str, Any] | None = None, observation: dict[str, Any] | None = None) -> JobPlan:
        weights = default_task_priority_weights()
        configured_weights = self._decision_rule("propose_jobs.base_task_priority_weights", {})
        weights = self._sanitize_task_priority_weights(configured_weights, weights)

        quotas = {
            "warehouse_material_runs": 20,
            "setup_runs": 40,
            "transfer_runs": 40,
            "inspection_runs": 35,
            "pm_runs": 6,
        }
        configured_quotas = self._decision_rule("propose_jobs.base_quotas", {})
        quotas = self._sanitize_quotas(configured_quotas, quotas)

        norm_state = norms if self.norms_enabled and isinstance(norms, dict) else {}
        pm_multiplier = max(1, int(self._decision_rule("propose_jobs.pm_runs_per_machine_multiplier", 1)))
        pm_per_day = self._clamp_int(
            norm_state.get("min_pm_per_machine_per_day", self.min_pm_norm_min),
            self.min_pm_norm_min,
            self.min_pm_norm_max,
            self.min_pm_norm_min,
        )
        inspection_backlog_target = self._clamp_int(
            norm_state.get("inspection_backlog_target", 8),
            self.inspection_backlog_target_min,
            self.inspection_backlog_target_max,
            8,
        )
        output_buffer_target = self._clamp_int(
            norm_state.get("max_output_buffer_target", 4),
            self.max_output_buffer_target_min,
            self.max_output_buffer_target_max,
            4,
        )
        battery_reserve_min = self._clamp_float(
            norm_state.get("battery_reserve_min", 50.0),
            self.battery_reserve_min_min,
            self.battery_reserve_min_max,
            50.0,
        )
        quotas["pm_runs"] = self._clamp_int(
            pm_per_day * pm_multiplier * 6,
            self.quota_min,
            int(self.quota_max.get("pm_runs", 24)),
            quotas.get("pm_runs", 6),
        )
        inspect_weight = self._clamp_float(
            norm_state.get("inspect_product_priority_weight", 1.0),
            self.inspect_product_priority_weight_min,
            self.inspect_product_priority_weight_max,
            1.0,
        )
        weights["inspect_product"] = round(
            self._clamp_float(
                weights.get("inspect_product", 1.0) * inspect_weight,
                self.task_priority_weight_min,
                self.task_priority_weight_max,
                weights.get("inspect_product", 1.0),
            ),
            3,
        )

        obs = observation if isinstance(observation, dict) else {}
        inspection_backlog = int(obs.get("inspection_backlog", 0))
        flow_obs = obs.get("flow", {}) if isinstance(obs.get("flow", {}), dict) else {}
        output_waiting = flow_obs.get("output_waiting_transfer", {}) if isinstance(flow_obs.get("output_waiting_transfer", {}), dict) else {}
        max_output_buffer = max((int(value) for value in output_waiting.values()), default=0)
        agents_obs = obs.get("agents", {}) if isinstance(obs.get("agents", {}), dict) else {}
        agents_by_id = agents_obs.get("by_id", {}) if isinstance(agents_obs.get("by_id", {}), dict) else {}
        active_batteries = [
            float(data.get("battery_remaining_min", 0.0))
            for data in agents_by_id.values()
            if not bool(data.get("discharged", False))
        ]
        min_battery = min(active_batteries) if active_batteries else battery_reserve_min

        if inspection_backlog > inspection_backlog_target:
            overload = min(1.0, (inspection_backlog - inspection_backlog_target) / max(1.0, float(inspection_backlog_target)))
            weights["inspect_product"] = round(
                self._clamp_float(
                    weights.get("inspect_product", 1.0) * (1.0 + 0.35 * overload),
                    self.task_priority_weight_min,
                    self.task_priority_weight_max,
                    weights.get("inspect_product", 1.0),
                ),
                3,
            )
            inspection_bonus = max(5, int(round((inspection_backlog - inspection_backlog_target) * 1.5)))
            quotas["inspection_runs"] = self._clamp_int(
                quotas.get("inspection_runs", 35) + inspection_bonus,
                self.quota_min,
                int(self.quota_max.get("inspection_runs", 80)),
                quotas.get("inspection_runs", 35),
            )

        if max_output_buffer > output_buffer_target:
            overload = min(1.0, (max_output_buffer - output_buffer_target) / max(1.0, float(output_buffer_target)))
            for key, gain in (("unload_machine", 0.30), ("inter_station_transfer", 0.25)):
                weights[key] = round(
                    self._clamp_float(
                        weights.get(key, 1.0) * (1.0 + gain * overload),
                        self.task_priority_weight_min,
                        self.task_priority_weight_max,
                        weights.get(key, 1.0),
                    ),
                    3,
                )
            transfer_bonus = max(5, int(round((max_output_buffer - output_buffer_target) * 1.25)))
            quotas["transfer_runs"] = self._clamp_int(
                quotas.get("transfer_runs", 40) + transfer_bonus,
                self.quota_min,
                int(self.quota_max.get("transfer_runs", 80)),
                quotas.get("transfer_runs", 40),
            )

        if min_battery < battery_reserve_min:
            deficit = min(1.0, (battery_reserve_min - min_battery) / max(1.0, battery_reserve_min))
            for key, gain in (
                ("battery_swap", 0.30),
                ("battery_delivery_low_battery", 0.25),
                ("battery_delivery_discharged", 0.20),
            ):
                weights[key] = round(
                    self._clamp_float(
                        weights.get(key, 1.0) * (1.0 + gain * deficit),
                        self.task_priority_weight_min,
                        self.task_priority_weight_max,
                        weights.get(key, 1.0),
                    ),
                    3,
                )
        return JobPlan(task_priority_weights=weights, quotas=quotas, rationale="llm_default", agent_priority_multipliers=self._clone_agent_priority_multipliers())

    def _build_strategy(self, llm_obj: dict[str, Any], fallback: StrategyState) -> StrategyState:
        diagnosis_keys = ("flow_risks", "maintenance_risks", "inspection_risks", "battery_risks", "evidence")
        diagnosis: dict[str, list[str]] = {}
        fallback_diagnosis = fallback.diagnosis if isinstance(getattr(fallback, "diagnosis", {}), dict) else {}
        for key in diagnosis_keys:
            diagnosis[key] = self._as_str_list(llm_obj.get(key), fallback_diagnosis.get(key, []))
        summary = str(llm_obj.get("summary", getattr(fallback, "summary", ""))).strip()
        notes = self._flatten_diagnosis_to_notes(summary, diagnosis)
        if not notes:
            notes = self._as_str_list(llm_obj.get("notes"), fallback.notes)
        return StrategyState(notes=notes, summary=summary, diagnosis=diagnosis)

    def _build_job_plan(self, llm_obj: dict[str, Any], fallback: JobPlan) -> JobPlan:
        if "task_priority_weights" not in llm_obj or "quotas" not in llm_obj:
            self._fail("propose_jobs response missing task_priority_weights/quotas.")
        weights = self._sanitize_task_priority_weights(llm_obj.get("task_priority_weights"), fallback.task_priority_weights)
        quotas = self._sanitize_quotas(llm_obj.get("quotas"), fallback.quotas)
        rationale = str(llm_obj.get("rationale", fallback.rationale))
        return JobPlan(task_priority_weights=weights, quotas=quotas, rationale=rationale, agent_priority_multipliers=self._clone_agent_priority_multipliers())

    def _build_norms(self, llm_obj: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        return self._sanitize_norms(llm_obj, fallback)

    def _build_urgent(self, llm_obj: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        return {"priority_updates": self._sanitize_priority_updates(llm_obj.get("priority_updates"))}

    def _prompt(self, title: str, payload: dict[str, Any], schema_hint: str) -> str:
        prompt_payload = dict(payload) if isinstance(payload, dict) else payload
        if isinstance(prompt_payload, dict) and "language" not in prompt_payload:
            prompt_payload["language"] = self.language
        compact_payload = self._prune_prompt_value(prompt_payload)
        return (
            f"{title}\n"
            f"Input JSON:\n{json.dumps(compact_payload, ensure_ascii=False)}\n\n"
            f"Return JSON schema:\n{schema_hint}\n"
        )

    def _placeholder_strategy(self) -> StrategyState:
        return StrategyState(
            notes=["Awaiting manager daily plan."],
            summary="Awaiting manager daily plan.",
            diagnosis={},
            orchestration_context={"mode": "orchestration_pending"},
        )

    def _sanitize_focus_tasks(self, src: Any, *, limit: int = 4) -> list[str]:
        return [item for item in self._as_str_list(src, []) if item in self.allowed_task_priority_keys][:limit]

    def _normalize_worker_report(self, agent_id: str, src: Any, fallback_summary: str = "") -> dict[str, Any]:
        raw = src if isinstance(src, dict) else {}
        summary = self._truncate_prompt_text(raw.get("summary", fallback_summary), max_len=220)
        return {
            "agent_id": agent_id,
            "summary": summary,
            "completed_work": self._as_str_list(raw.get("completed_work"), []),
            "blocked_work": self._as_str_list(raw.get("blocked_work"), []),
            "local_observations": self._as_str_list(raw.get("local_observations"), []),
            "handover_events": self._as_str_list(raw.get("handover_events"), []),
            "suggested_focus": self._sanitize_focus_tasks(raw.get("suggested_focus"), limit=4),
            "next_day_role_preference": self._truncate_prompt_text(raw.get("next_day_role_preference", ""), max_len=96),
            "reason_trace": self._as_str_list(raw.get("reason_trace"), []),
        }

    def _sanitize_agent_roles(self, src: Any) -> dict[str, str]:
        roles: dict[str, str] = {}
        if not isinstance(src, dict):
            return roles
        for agent_id in self.agent_ids:
            value = self._truncate_prompt_text(src.get(agent_id, ""), max_len=96)
            if value:
                roles[agent_id] = value
        return roles

    def _sanitize_personal_queues(self, src: Any, *, day: int) -> dict[str, list[dict[str, Any]]]:
        queues: dict[str, list[dict[str, Any]]] = {agent_id: [] for agent_id in self.agent_ids}
        if not isinstance(src, dict):
            return queues
        allowed_target_types = {"none", "station", "machine", "agent", "location"}
        for agent_id in self.agent_ids:
            raw_items = src.get(agent_id, [])
            if not isinstance(raw_items, list):
                continue
            for idx, raw_item in enumerate(raw_items[: self.worker_queue_limit]):
                if not isinstance(raw_item, dict):
                    continue
                task_family = str(raw_item.get("task_family", "")).strip()
                if task_family not in self.allowed_task_priority_keys:
                    continue
                target_type = str(raw_item.get("target_type", "none")).strip().lower() or "none"
                if target_type not in allowed_target_types:
                    target_type = "none"
                target_station = None
                if target_type == "station":
                    try:
                        target_station = int(raw_item.get("target_station"))
                    except (TypeError, ValueError):
                        continue
                expires_at_day = None
                if raw_item.get("expires_at_day") not in {None, ""}:
                    try:
                        expires_at_day = max(day, int(raw_item.get("expires_at_day")))
                    except (TypeError, ValueError):
                        expires_at_day = None
                dependency_ids = []
                if isinstance(raw_item.get("dependency_ids"), list):
                    dependency_ids = [str(item).strip() for item in raw_item.get("dependency_ids", []) if str(item).strip()][:4]
                handover_to = str(raw_item.get("handover_to", "")).strip().upper()
                if handover_to not in self.agent_ids:
                    handover_to = ""
                queues[agent_id].append(
                    {
                        "order_id": str(raw_item.get("order_id", f"{agent_id}-D{day:02d}-WO{idx + 1}")),
                        "task_family": task_family,
                        "priority": round(self._clamp_float(raw_item.get("priority", 1.0), 0.8, 3.0, 1.0), 3),
                        "target_type": target_type,
                        "target_id": self._truncate_prompt_text(raw_item.get("target_id", ""), max_len=64),
                        "target_station": target_station,
                        "dependency_ids": dependency_ids,
                        "parallel_group": self._truncate_prompt_text(raw_item.get("parallel_group", ""), max_len=48),
                        "handover_to": handover_to,
                        "expires_at_day": expires_at_day,
                        "reason": self._truncate_prompt_text(raw_item.get("reason", ""), max_len=180),
                    }
                )
        return queues

    def _sanitize_mailbox_plan(self, src: Any, *, day: int) -> dict[str, list[dict[str, Any]]]:
        mailboxes: dict[str, list[dict[str, Any]]] = {agent_id: [] for agent_id in self.agent_ids}
        if not isinstance(src, dict):
            return mailboxes
        allowed_target_types = {"none", "station", "machine", "agent", "location"}
        for agent_id in self.agent_ids:
            raw_items = src.get(agent_id, [])
            if not isinstance(raw_items, list):
                continue
            for idx, raw_item in enumerate(raw_items[: self.worker_queue_limit]):
                if not isinstance(raw_item, dict):
                    continue
                task_family = str(raw_item.get("task_family", "")).strip()
                if task_family and task_family not in self.allowed_task_priority_keys:
                    continue
                target_type = str(raw_item.get("target_type", "none")).strip().lower() or "none"
                if target_type not in allowed_target_types:
                    target_type = "none"
                target_station = None
                if target_type == "station" and raw_item.get("target_station") not in {None, ""}:
                    try:
                        target_station = int(raw_item.get("target_station"))
                    except (TypeError, ValueError):
                        continue
                from_agent = str(raw_item.get("from_agent", self.openclaw_manager_agent_id)).strip().upper() or self.openclaw_manager_agent_id
                to_agent = str(raw_item.get("to_agent", agent_id)).strip().upper() or agent_id
                if to_agent != agent_id:
                    to_agent = agent_id
                mailboxes[agent_id].append(
                    {
                        "message_id": str(raw_item.get("message_id", f"{agent_id}-D{day:02d}-MSG{idx + 1}")),
                        "from_agent": from_agent,
                        "to_agent": to_agent,
                        "message_type": self._truncate_prompt_text(raw_item.get("message_type", "handover"), max_len=48) or "handover",
                        "task_family": task_family,
                        "target_type": target_type,
                        "target_id": self._truncate_prompt_text(raw_item.get("target_id", ""), max_len=64),
                        "target_station": target_station,
                        "priority": self._clamp_int(raw_item.get("priority", 1), 1, 5, 1),
                        "body": self._truncate_prompt_text(raw_item.get("body", ""), max_len=220),
                    }
                )
        return mailboxes

    def _sanitize_parallel_groups(self, src: Any) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        if not isinstance(src, list):
            return groups
        for idx, raw_group in enumerate(src[: max(1, len(self.agent_ids))]):
            if not isinstance(raw_group, dict):
                continue
            members = [str(item).strip().upper() for item in raw_group.get("members", []) if str(item).strip().upper() in self.agent_ids]
            groups.append(
                {
                    "group_id": str(raw_group.get("group_id", f"G{idx + 1}")),
                    "members": members,
                    "focus_tasks": self._sanitize_focus_tasks(raw_group.get("focus_tasks"), limit=4),
                    "reason": self._truncate_prompt_text(raw_group.get("reason", ""), max_len=160),
                }
            )
        return groups
    def _sanitize_reason_trace(self, src: Any) -> list[dict[str, Any]]:
        trace: list[dict[str, Any]] = []
        if not isinstance(src, list):
            return trace
        for raw in src[:16]:
            if not isinstance(raw, dict):
                continue
            trace.append(
                {
                    "issue": self._truncate_prompt_text(raw.get("issue", ""), max_len=140),
                    "decision": self._truncate_prompt_text(raw.get("decision", ""), max_len=180),
                    "assigned_agents": [str(item).strip().upper() for item in raw.get("assigned_agents", []) if str(item).strip().upper() in self.agent_ids],
                    "reason": self._truncate_prompt_text(raw.get("reason", ""), max_len=180),
                }
            )
        return trace

    def _build_orchestration_outputs(
        self,
        llm_obj: dict[str, Any],
        fallback: JobPlan,
        *,
        day: int,
    ) -> tuple[StrategyState, JobPlan]:
        summary = self._truncate_prompt_text(
            llm_obj.get("strategy_summary", llm_obj.get("manager_summary", llm_obj.get("rationale", fallback.rationale))),
            max_len=220,
        )
        diagnosis = {
            "flow_risks": self._as_str_list(llm_obj.get("flow_risks"), []),
            "maintenance_risks": self._as_str_list(llm_obj.get("maintenance_risks"), []),
            "inspection_risks": self._as_str_list(llm_obj.get("inspection_risks"), []),
            "battery_risks": self._as_str_list(llm_obj.get("battery_risks"), []),
        }
        notes = self._flatten_diagnosis_to_notes(summary, diagnosis)
        strategy = StrategyState(
            notes=notes[:8],
            summary=summary,
            diagnosis=diagnosis,
            orchestration_context={
                "reason_trace": self._sanitize_reason_trace(llm_obj.get("reason_trace")),
            },
        )
        weights = self._sanitize_task_priority_weights(llm_obj.get("task_priority_weights"), fallback.task_priority_weights)
        quotas = self._sanitize_quotas(llm_obj.get("quotas"), fallback.quotas)
        multipliers = self._clone_agent_priority_multipliers()
        raw_multipliers = self._sanitize_agent_priority_profile_updates(llm_obj.get("agent_priority_multipliers"))
        for agent_id, values in raw_multipliers.items():
            multipliers.setdefault(agent_id, {})
            multipliers[agent_id].update(values)
        agent_roles = self._sanitize_agent_roles(llm_obj.get("agent_roles"))
        personal_queues = self._sanitize_personal_queues(llm_obj.get("personal_queues"), day=day)
        mailbox = self._sanitize_mailbox_plan(llm_obj.get("mailbox"), day=day)
        parallel_groups = self._sanitize_parallel_groups(llm_obj.get("parallel_groups"))
        reason_trace = self._sanitize_reason_trace(llm_obj.get("reason_trace"))
        job_plan = JobPlan(
            task_priority_weights=weights,
            quotas=quotas,
            rationale=self._truncate_prompt_text(llm_obj.get("rationale", fallback.rationale), max_len=220),
            agent_priority_multipliers=multipliers,
            agent_roles=agent_roles,
            personal_queues=personal_queues,
            mailbox=mailbox,
            parallel_groups=parallel_groups,
            reason_trace=reason_trace,
            manager_summary=self._truncate_prompt_text(llm_obj.get("manager_summary", summary), max_len=220),
        )
        return strategy, job_plan

    def _sync_openclaw_orchestration_workspace(
        self,
        *,
        day_summary: dict[str, Any],
        norms: dict[str, Any],
        worker_reports: dict[str, dict[str, Any]],
        manager_review: dict[str, Any],
    ) -> None:
        if not self._openclaw_enabled():
            return
        day = int(day_summary.get("day", 0) or 0)
        day_view = self._day_summary_prompt_view(day_summary)
        for agent_id in self.agent_ids:
            workspace = self._openclaw_workspace_path(agent_id)
            if workspace is None:
                continue
            report = worker_reports.get(agent_id, {}) if isinstance(worker_reports.get(agent_id, {}), dict) else {}
            belief_payload = {
                "operational_focus": str(report.get("summary", "")),
                "key_risks": list(report.get("local_observations", []))[:4],
                "priority_hypotheses": list(report.get("suggested_focus", []))[:4],
            }
            commitment_payload = {
                "summary": str(report.get("summary", "")),
                "focus_tasks": list(report.get("suggested_focus", []))[:4],
                "success_signals": list(report.get("completed_work", []))[:3],
            }
            semantic_payload = {
                "specialization": [str(report.get("next_day_role_preference", ""))] if str(report.get("next_day_role_preference", "")).strip() else [],
                "heuristics": list(report.get("reason_trace", []))[:4],
                "anti_patterns": list(report.get("blocked_work", []))[:4],
            }
            self._openclaw_write_json(workspace / "facts" / "current_day_summary.json", day_view)
            self._openclaw_write_json(workspace / "facts" / "current_worker_report.json", report)
            self._openclaw_write_json(workspace / "facts" / "worker_reports" / f"day_{day:02d}.json", report)
            self._openclaw_write_json(workspace / "beliefs" / "current_beliefs.json", belief_payload)
            self._openclaw_write_json(workspace / "beliefs" / "history" / f"day_{day:02d}.json", belief_payload)
            self._openclaw_write_json(workspace / "commitments" / "current_commitment.json", commitment_payload)
            self._openclaw_write_json(workspace / "commitments" / "history" / f"day_{day:02d}.json", commitment_payload)
            self._openclaw_write_json(workspace / "memory" / "semantic" / "current.json", semantic_payload)
            self._openclaw_write_json(workspace / "memory" / "episodic" / f"day_{day:02d}.json", {"summary": report.get("summary", ""), "completed_work": report.get("completed_work", []), "blocked_work": report.get("blocked_work", [])})
            self._openclaw_write_markdown(workspace / "memory" / "daily" / f"day_{day:02d}.md", f"{agent_id} Day {day} Report", [("Day Summary", day_view), ("Worker Report", report)])
            self._openclaw_write_markdown(
                workspace / "memory" / "rolling_summary.md",
                f"{agent_id} Rolling Summary",
                [
                    ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                    ("Latest Beliefs", belief_payload),
                    ("Current Commitment", commitment_payload),
                    ("Semantic Memory", semantic_payload),
                ],
            )
            self._openclaw_write_markdown(
                workspace / "MEMORY.md",
                f"{agent_id} Memory",
                self._openclaw_prompt_memory_sections(
                    run_scope="This workspace memory is scoped to the current run only.",
                    prompt_memory={"beliefs": belief_payload, "semantic_memory": semantic_payload},
                    current_commitment=commitment_payload,
                    raw_history_files={"daily": f"memory/daily/day_{day:02d}.md", "episodic": f"memory/episodic/day_{day:02d}.json", "report": f"reports/day_{day:02d}_report.json"},
                ),
            )
        manager_workspace = self._openclaw_workspace_path(self.openclaw_manager_agent_id)
        if manager_workspace is None:
            return
        shared_beliefs = {
            "system_focus": str(manager_review.get("review_summary", manager_review.get("summary", ""))),
            "key_risks": self._as_str_list(manager_review.get("global_risks"), []),
            "priority_hypotheses": []
        }
        shared_commitments = {
            "summary": str(manager_review.get("review_summary", manager_review.get("summary", ""))),
            "focus_tasks": [],
            "success_signals": [],
        }
        shared_semantic = {
            "coordination_notes": self._as_str_list(manager_review.get("coordination_notes"), []),
            "heuristics": self._as_str_list(manager_review.get("reason_trace"), []),
            "anti_patterns": [],
            "unresolved_disagreements": [],
        }
        self._openclaw_write_json(manager_workspace / "facts" / "current_day_summary.json", day_view)
        self._openclaw_write_json(manager_workspace / "facts" / "current_norms.json", norms)
        self._openclaw_write_json(manager_workspace / "facts" / "current_worker_reports.json", worker_reports)
        self._openclaw_write_json(manager_workspace / "facts" / "daily_review" / f"day_{day:02d}.json", manager_review)
        self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", shared_beliefs)
        self._openclaw_write_json(manager_workspace / "beliefs" / "history" / f"day_{day:02d}.json", shared_beliefs)
        self._openclaw_write_json(manager_workspace / "commitments" / "current_commitment.json", shared_commitments)
        self._openclaw_write_json(manager_workspace / "commitments" / "history" / f"day_{day:02d}.json", shared_commitments)
        self._openclaw_write_json(manager_workspace / "memory" / "semantic" / "current.json", shared_semantic)
        self._openclaw_write_markdown(manager_workspace / "memory" / "daily" / f"day_{day:02d}.md", f"{self.openclaw_manager_agent_id} Day {day} Review", [("Worker Reports", worker_reports), ("Daily Review", manager_review)])
        self._openclaw_write_markdown(
            manager_workspace / "memory" / "rolling_summary.md",
            f"{self.openclaw_manager_agent_id} Rolling Summary",
            [
                ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                ("Latest Manager Summary", {"summary": str(manager_review.get("review_summary", manager_review.get("summary", ""))).strip(), "key_risks": shared_beliefs.get("key_risks", []), "coordination_notes": shared_semantic.get("coordination_notes", [])}),
            ],
        )
        self._openclaw_write_markdown(
            manager_workspace / "MEMORY.md",
            f"{self.openclaw_manager_agent_id} Memory",
            self._openclaw_prompt_memory_sections(
                run_scope="This workspace memory is scoped to the current run only.",
                prompt_memory={"shared_beliefs": shared_beliefs, "shared_semantic_memory": shared_semantic},
                current_commitment=shared_commitments,
                raw_history_files={"daily": f"memory/daily/day_{day:02d}.md", "daily_review": f"facts/daily_review/day_{day:02d}.json"},
            ),
        )

    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        plan_day = None
        try:
            plan_day = int((self._active_orchestration_plan or {}).get("day", 0) or 0)
        except (TypeError, ValueError):
            plan_day = 0
        try:
            observation_day = int(observation.get("day", 0) or 0)
        except (TypeError, ValueError):
            observation_day = 0
        if plan_day == observation_day:
            cached = self._active_orchestration_plan.get("strategy")
            if isinstance(cached, StrategyState):
                return cached
        return self._placeholder_strategy()

    def propose_jobs(
        self,
        observation: dict[str, Any],
        strategy: StrategyState,
        norms: dict[str, Any],
    ) -> JobPlan:
        fallback = self._default_job_plan(norms, observation)
        prompt = self._prompt(
            title="Build tomorrow's orchestrated execution plan from the current plant state and the latest worker reports.",
            payload={
                "observation": self._planner_observation_view(observation),
                "current_norms": norms,
                "latest_worker_reports": self._latest_worker_reports,
                "latest_manager_review": self._latest_manager_review,
                "current_agent_priority_profiles": self._summarize_agent_priority_profiles(include_full=True),
                "guardrails": self._llm_guardrails_payload("plan"),
            },
            schema_hint='{"strategy_summary": str, "flow_risks": [str], "maintenance_risks": [str], "inspection_risks": [str], "battery_risks": [str], "task_priority_weights": "map<allowed_task_priority_key,float>", "quotas": "map<allowed_quota_key,int>", "agent_priority_multipliers": "map<allowed_agent_id,map<allowed_task_priority_key,float>>", "agent_roles": "map<allowed_agent_id,str>", "personal_queues": "map<allowed_agent_id,[work_order]>", "mailbox": "map<allowed_agent_id,[handover_message]>", "parallel_groups": [object], "reason_trace": [object], "manager_summary": str, "rationale": str}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt=self._shared_system_prompt(
                "You are the OpenClaw manager for ManSim. Design the next-day execution structure for the worker agents.",
                [
                    "Decompose complex plant problems into concrete worker-level work orders.",
                    "Assign task emphasis per worker by using both agent_priority_multipliers and personal_queues.",
                    "Use mailbox messages only when explicit handover or coordination is needed.",
                    "Parallelize only when the work is truly independent.",
                    "Keep reason_trace concrete enough that the simulator can explain why work was assigned.",
                    self._communication_language_instruction(["strategy_summary", "manager_summary", "rationale"]),
                ],
            ),
            call_name="manager_daily_planner",
            context={"phase": "manager_daily_planner", "day": observation.get("day")},
        )
        plan_day = 0
        try:
            plan_day = int(observation.get("day", 0) or 0)
        except (TypeError, ValueError):
            plan_day = 0
        built_strategy, job_plan = self._build_orchestration_outputs(llm_obj, fallback, day=max(1, plan_day))
        strategy.notes = list(built_strategy.notes)
        strategy.summary = built_strategy.summary
        strategy.diagnosis = dict(built_strategy.diagnosis)
        strategy.orchestration_context = dict(built_strategy.orchestration_context)
        self._active_orchestration_plan = {
            "day": max(1, plan_day),
            "strategy": built_strategy,
            "job_plan": job_plan,
            "worker_reports": dict(self._latest_worker_reports),
            "review": dict(self._latest_manager_review),
        }
        self._active_job_plan_snapshot = job_plan
        return job_plan

    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        self._last_discussion_trace = []
        fallback = dict(norms)
        day = int(day_summary.get("day", len(self.shared_discussion_memory) + 1) or 0)
        agent_experience_payload = self._agent_experience_prompt_payload(day_summary)
        active_job_plan = self._active_job_plan_snapshot
        worker_reports: dict[str, dict[str, Any]] = {}

        for aid in self.agent_ids:
            current_queue = []
            current_mailbox = []
            current_role = ""
            if isinstance(active_job_plan, JobPlan):
                current_queue = list(active_job_plan.personal_queues.get(aid, [])) if isinstance(active_job_plan.personal_queues, dict) else []
                current_mailbox = list(active_job_plan.mailbox.get(aid, [])) if isinstance(active_job_plan.mailbox, dict) else []

                current_role = str(active_job_plan.agent_roles.get(aid, "")) if isinstance(active_job_plan.agent_roles, dict) else ""
            prompt = self._prompt(
                title=f"{aid}: report your own local experience from today's work.",
                payload={
                    "agent_id": aid,
                    "day_summary": self._day_summary_prompt_view(day_summary),
                    "agent_experience": agent_experience_payload.get(aid, {}),
                    "current_role": current_role,
                    "current_personal_queue": current_queue,
                    "current_mailbox": current_mailbox,
                },
                schema_hint='{"summary": str, "completed_work": [str], "blocked_work": [str], "local_observations": [str], "handover_events": [str], "suggested_focus": ["allowed_task_priority_key"], "next_day_role_preference": str, "reason_trace": [str]}',
            )
            llm_obj = self._call_llm_json(
                user_prompt=prompt,
                system_prompt=self._shared_system_prompt(
                    f"You are {aid}, a worker agent reporting your own daily experience to the OpenClaw manager.",
                    [
                        "Report your own local execution experience rather than trying to produce a team consensus.",
                        "Ground the report in the work you completed, the work that blocked, and the handovers you observed.",
                        "Do not invent hidden system state or unavailable resources.",
                        self._communication_language_instruction(["summary"]),
                    ],
                ),
                call_name="worker_daily_report",
                context={"phase": "worker_daily_report", "day": day_summary.get("day"), "agent_id": aid},
            )
            report = self._normalize_worker_report(aid, llm_obj, fallback_summary=str(day_summary.get("summary", "")))
            worker_reports[aid] = report
            self._last_discussion_trace.append({"type": "worker_daily_report", "agent_id": aid, "report": report})
            self._append_bounded(
                self.agent_memories[aid],
                {"day": day, "summary": report.get("summary", ""), "recent_points": report.get("local_observations", []), "worker_report": report},
            )

        prompt = self._prompt(
            title="Review the worker reports and produce the manager's next-day strategic review.",
            payload={
                "day_summary": self._day_summary_prompt_view(day_summary),
                "worker_reports": worker_reports,
                "current_norms": norms,
                "current_agent_priority_profiles": self._summarize_agent_priority_profiles(include_full=True),
                "guardrails": self._llm_guardrails_payload("norms"),
            },
            schema_hint='{"updated_norms": "map<allowed_norm_key,number>", "review_summary": str, "global_risks": [str], "coordination_notes": [str], "reason_trace": [str]}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt=self._shared_system_prompt(
                "You are the OpenClaw manager. Review worker reports and prepare the strategic direction for tomorrow.",
                [
                    "Synthesize the worker reports into one next-day strategic review.",
                    "Update norms only when the evidence is strong enough to justify changing shared behavior.",
                    self._communication_language_instruction(["review_summary"]),
                ],
            ),
            call_name="manager_daily_review",
            context={"phase": "manager_daily_review", "day": day_summary.get("day")},
        )
        sanitized_norms = self._build_norms(llm_obj.get("updated_norms", llm_obj), fallback)
        manager_review = {
            "day": day,
            "review_summary": self._truncate_prompt_text(llm_obj.get("review_summary", ""), max_len=220),
            "global_risks": self._as_str_list(llm_obj.get("global_risks"), []),
            "coordination_notes": self._as_str_list(llm_obj.get("coordination_notes"), []),
            "reason_trace": self._as_str_list(llm_obj.get("reason_trace"), []),
            "updated_norms": sanitized_norms,
        }
        self._latest_worker_reports = worker_reports
        self._latest_manager_review = manager_review
        self._append_bounded(
            self.shared_discussion_memory,
            {
                "day": day,
                "summary": manager_review.get("review_summary", ""),
                "signature": manager_review.get("review_summary", ""),
                "worker_reports": worker_reports,
            },
        )
        self._last_discussion_trace.append({"type": "manager_daily_review", "review": manager_review})
        self._sync_openclaw_orchestration_workspace(
            day_summary=day_summary,
            norms=sanitized_norms,
            worker_reports=worker_reports,
            manager_review=manager_review,
        )
        return sanitized_norms

    def urgent_discuss(self, event: dict[str, Any], local_state: dict[str, Any]) -> dict[str, Any]:
        fallback = {"priority_updates": {}}
        prompt = self._prompt(
            title="Urgent discussion for incident response.",
            payload={
                "event": event,
                "local_state": local_state,
                "memory": self._memory_context("urgent"),
                "guardrails": self._llm_guardrails_payload("urgent"),
            },
            schema_hint='{"priority_updates": "map<allowed_task_priority_key,float>"}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt=self._shared_system_prompt(
                "You are an urgent manufacturing response coordinator.",
                [
                    "This phase handles a disruptive event such as a machine breakdown or battery emergency.",
                    "Use only the allowed direct task priority keys.",
                    "Stay within the provided update range and make only moderate temporary adjustments.",
                    "Do not rewrite the whole policy when the event only justifies a local correction.",
                ],
            ),
            call_name="urgent_discuss",
            context={"phase": "urgent_discuss", "event_type": event.get("event_type", "")},
        )
        return self._build_urgent(llm_obj, fallback)


























