from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime, timezone
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


class OptionalLLMDecisionModule(DecisionModule):
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
        self.communication_enabled = bool(comm_cfg.get("enabled", True))
        self.comm_rounds = max(1, int(comm_cfg.get("rounds", 2)))
        self.comm_max_transcript = max(1, int(comm_cfg.get("max_transcript_messages", 24)))
        self.communication_language = self._normalize_communication_language(comm_cfg.get("language", "ENG"))

        num_agents = int((cfg.get("factory", {}) or {}).get("num_agents", 4))
        self.agent_ids = [f"A{i}" for i in range(1, num_agents + 1)]

        mem_cfg = self.llm_cfg.get("memory", {}) if isinstance(self.llm_cfg.get("memory", {}), dict) else {}
        self.memory_window_days = max(1, int(mem_cfg.get("history_window_days", 7)))
        self.include_agent_memory = bool(mem_cfg.get("include_agent_memory", True))

        self._last_discussion_trace: list[dict[str, Any]] = []
        self.shared_norms_memory: list[dict[str, Any]] = []
        self.shared_discussion_memory: list[dict[str, Any]] = []
        self.agent_memories: dict[str, list[dict[str, Any]]] = {aid: [] for aid in self.agent_ids}
        self.agent_experience_memory: dict[str, list[dict[str, Any]]] = {aid: [] for aid in self.agent_ids}
        self.agent_priority_multipliers: dict[str, dict[str, float]] = default_agent_priority_multipliers(self.agent_ids)
        self._last_agent_priority_update_trace: dict[str, Any] = {}
        self._llm_exchange_records: list[dict[str, Any]] = []
        self._llm_call_seq = 0

        if not self.enabled:
            self._fail("decision.mode=llm but llm.enabled=false.")
        if self.provider != "openai_compatible":
            self._fail(f"Unsupported llm.provider: {self.provider}")
        if not self.server_url:
            self._fail("llm.server_url is empty.")
        if not self.model:
            self._fail("llm.model is empty.")

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

    def _communication_language_name(self) -> str:
        return "Korean" if self.communication_language == "KOR" else "English"

    def _communication_language_instruction(self, fields: list[str] | tuple[str, ...] | None = None) -> str:
        field_text = ", ".join(str(item) for item in (fields or []))
        target = field_text if field_text else "all natural-language text fields"
        return (
            f"Write {target} in {self._communication_language_name()}. Keep JSON keys, enum values, IDs, task priority keys, norm keys, and machine/agent IDs in English."
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
    ) -> str:
        factory_cfg = self.cfg.get("factory", {}) if isinstance(self.cfg.get("factory", {}), dict) else {}
        movement_cfg = self.cfg.get("movement", {}) if isinstance(self.cfg.get("movement", {}), dict) else {}
        machine_failure_cfg = self.cfg.get("machine_failure", {}) if isinstance(self.cfg.get("machine_failure", {}), dict) else {}
        agent_cfg = self.cfg.get("agent", {}) if isinstance(self.cfg.get("agent", {}), dict) else {}
        stations = self._processing_station_ids()
        station_names = [f"Station{station}" for station in stations]
        plant_flow = " -> ".join(station_names + ["Inspection"]) if station_names else "Inspection"
        machines_per_station = max(1, int(factory_cfg.get("machines_per_station", 1)))
        processing_cfg = factory_cfg.get("processing_time_min", {}) if isinstance(factory_cfg.get("processing_time_min", {}), dict) else {}
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

        plant_lines = [f"- Flow: {plant_flow}.", f"- Agents: {', '.join(self.agent_ids)}."]
        for idx, station in enumerate(stations):
            proc_min = self._fmt_number(float(processing_cfg.get(f"station{station}", 0.0)))
            machine_ids = ", ".join(f"S{station}M{midx}" for midx in range(1, machines_per_station + 1))
            station_input = "warehouse material" if idx == 0 else f"Station{stations[idx - 1]} output + local material"
            station_output = "candidate for Inspection" if idx == len(stations) - 1 else f"intermediate for Station{stations[idx + 1]}"
            plant_lines.append(
                f"- Station{station}: {machine_ids}; input={station_input}; output={station_output}; cycle={proc_min} min."
            )
        plant_lines.extend(
            [
                f"- Inspection: input=last-station output; output=accepted product or scrap; time={inspection_base}/sqrt(parallel inspectors), floor={inspection_min} min.",
                f"- Travel/timing: warehouse-station {warehouse_to_station}; station-station/inspection {station_to_station}; setup {setup_min}; unload {unload_min}; repair {repair_min}; PM {pm_min}.",
                f"- Reliability/battery: mean TTF {mean_ttf}; swap period {battery_period}; battery pickup {battery_pickup}; delivery overhead {battery_delivery_extra}.",
            ]
        )

        task_family_semantics = {
            "battery_swap": "perform the swap once a fresh battery is available.",
            "battery_delivery_low_battery": "bring a fresh battery to an active low-battery agent.",
            "battery_delivery_discharged": "rescue a discharged agent with a fresh battery.",
            "repair_machine": "repair a broken machine to restore capacity.",
            "unload_machine": "remove processed output from a machine so downstream flow can continue.",
            "setup_machine": "load or set up a waiting machine so processing can start.",
            "inter_station_transfer": "move intermediate or finished items between stations, inspection, and warehouse.",
            "material_supply": "replenish station material from warehouse stock.",
            "inspect_product": "inspect a finished-product candidate at Inspection.",
            "preventive_maintenance": "spend time now to reduce future breakdown risk temporarily.",
        }
        task_family_order = list(task_family_semantics.keys())
        selected_task_families = [
            key for key in (list(task_family_subset) if task_family_subset else task_family_order) if key in task_family_semantics
        ]
        task_family_lines = [f"- {key}: {task_family_semantics[key]}" for key in selected_task_families]

        prompt_sections = [
            role_summary.strip(),
            f"Global objective:\n- Maximize the number of accepted finished products completed within the full simulation horizon of {horizon_days} days.",
            "Plant summary:\n" + "\n".join(plant_lines),
            "Naming and JSON conventions:\n"
            + "\n".join(
                [
                    "- Agent IDs use A#; machine IDs use SXMY (for example, A2, S1M1, S2M2).",
                    "- Locations use Warehouse, Station1..StationN, Inspection, BatteryStation, and TownHall.",
                ]
            ),
            "Core constraints:\n"
            + "\n".join(
                [
                    "- Choose only feasible tasks. Do not invent tasks, machines, agents, queues, or process stages.",
                    "- Infer flow pressure from the current state only; do not assume a fixed bottleneck or overreact without evidence.",
                ]
            ),
        ]
        if include_task_family_semantics:
            prompt_sections.insert(3, "Task family semantics:\n" + "\n".join(task_family_lines))
        if self.norms_enabled and include_norm_semantics:
            prompt_sections.insert(
                3,
                "Shared norm semantics:\n"
                + "\n".join(
                    [
                        "- Norms are persistent team-level planning references, not hard constraints.",
                        "- min_pm_per_machine_per_day: PM baseline.",
                        "- inspect_product_priority_weight: inspection baseline.",
                        "- inspection_backlog_target: preferred backlog cap.",
                        "- max_output_buffer_target: preferred output-buffer cap.",
                        "- battery_reserve_min: preferred minimum battery reserve.",
                    ]
                ),
            )
        if phase_guidance:
            prompt_sections.append("Phase-specific instructions:\n" + "\n".join(f"- {line}" for line in phase_guidance))
        prompt_sections.append(
            "Output discipline:\n"
            "- Return exactly one JSON object that follows the requested schema.\n"
            "- Do not add markdown, code fences, comments, or extra prose outside the JSON object."
        )
        return "\n\n".join(section for section in prompt_sections if section.strip())

    def _townhall_task_family_subset(self) -> tuple[str, ...]:
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

    def selector_agent_experience_view(self, agent_id: str) -> dict[str, Any]:
        aid = str(agent_id).strip()
        latest_experience = self.agent_experience_memory.get(aid, [])[-1] if self.agent_experience_memory.get(aid) else {}
        latest_townhall = self.agent_memories.get(aid, [])[-1] if self.agent_memories.get(aid) else {}
        view: dict[str, Any] = {}
        if isinstance(latest_experience, dict):
            view["latest_experience"] = {
                "day": int(latest_experience.get("day", 0) or 0),
                "top_completed_task_families": list(latest_experience.get("top_completed_task_families", []))[:2],
                "contribution_signals": dict(latest_experience.get("contribution_signals", {})),
                "recent_task_events": list(latest_experience.get("recent_task_events", []))[:1],
            }
        if isinstance(latest_townhall, dict):
            recent_points = [str(item).strip() for item in latest_townhall.get("recent_points", []) if str(item).strip()]
            if recent_points:
                view["latest_townhall_points"] = recent_points[:1]
        return view

    def _record_llm_exchange(self, record: dict[str, Any]) -> None:
        self._llm_exchange_records.append(record)

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
            memory = self.agent_experience_memory.get(agent_id, [])[-1:]
            payload[agent_id] = {
                "today": {
                    "top_completed_task_families": list(raw.get("top_completed_task_families", []))[:2],
                    "contribution_signals": dict(raw.get("contribution_signals", {})),
                    "recent_task_events": list(raw.get("recent_task_events", []))[:1],
                },
                "recent_memory": memory,
            }
        return payload

    def _record_llm_exchange(self, record: dict[str, Any]) -> None:
        self._llm_exchange_records.append(record)


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

    def _memory_context(self, phase: str = "plan") -> dict[str, Any]:
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
        for key in ("flow_risks", "maintenance_risks", "inspection_risks", "battery_risks"):
            values = diagnosis.get(key, []) if isinstance(diagnosis.get(key, []), list) else []
            cleaned = [str(item).strip() for item in values if str(item).strip()]
            if cleaned:
                payload[key] = cleaned[:4]
        if not payload and strategy.notes:
            payload["notes"] = list(strategy.notes[:4])
        return payload

    def _flatten_diagnosis_to_notes(self, summary: str, diagnosis: dict[str, list[str]]) -> list[str]:
        labels = {
            "flow_risks": "Flow",
            "maintenance_risks": "Maintenance",
            "inspection_risks": "Inspection",
            "battery_risks": "Battery",
            "evidence": "Evidence",
        }
        notes: list[str] = []
        if summary.strip():
            notes.append(f"Summary: {summary.strip()}")
        for key in ("flow_risks", "maintenance_risks", "inspection_risks", "battery_risks", "evidence"):
            items = diagnosis.get(key, []) if isinstance(diagnosis.get(key, []), list) else []
            cleaned = [str(item).strip() for item in items if str(item).strip()]
            if cleaned:
                notes.append(f"{labels[key]}: " + "; ".join(cleaned[:3]))
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
        discussion_item = {
            "day": day,
            "issue_summary": self._issue_summary(day_summary),
            "changed_norm_keys": sorted(norm_delta.keys()),
            "consensus_proposals": highlights.get("consensus_proposals", [])[:2],
            "conflicting_proposals": highlights.get("conflicting_proposals", [])[:2],
            "synthesis_summary": self._truncate_prompt_text(summary, max_len=220),
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
                self._append_bounded(self.agent_memories[aid], agent_item)
                raw_experience = agent_experience.get(aid, {}) if isinstance(agent_experience.get(aid, {}), dict) else {}
                experience_item = {
                    "day": day,
                    "top_completed_task_families": raw_experience.get("top_completed_task_families", []),
                    "contribution_signals": raw_experience.get("contribution_signals", {}),
                    "recent_task_events": raw_experience.get("recent_task_events", []),
                    "current_priority_profile": self.agent_priority_multipliers.get(aid, {}),
                }
                self._append_bounded(self.agent_experience_memory[aid], experience_item)

        return {
            "day": day,
            "memory_window_days": self.memory_window_days,
            "norms_memory_size": len(self.shared_norms_memory),
            "discussion_memory_size": len(self.shared_discussion_memory),
            "changed_norm_keys": sorted(norm_delta.keys()),
            "duplicate_discussion_skipped": duplicate_skipped,
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

    def _call_llm_json(
        self,
        user_prompt: str,
        system_prompt: str,
        *,
        call_name: str,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.server_url.rstrip("/") + "/chat/completions"
        headers_for_log = {"Content-Type": "application/json"}
        if self.api_key:
            headers_for_log["Authorization"] = "Bearer ***"

        started_ts = time.time()
        started_at_utc = datetime.now(timezone.utc).isoformat()
        self._llm_call_seq += 1
        call_id = self._llm_call_seq
        repair_instruction = (
            "Your previous reply was not valid JSON. Return only one valid JSON object that matches the requested "
            "schema in the user prompt. Start with { and end with }. Do not add markdown, code fences, comments, "
            "explanations, or trailing commas."
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        req_headers = {"Content-Type": "application/json"}
        if self.api_key:
            req_headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] | None = None
        content = ""
        error_message = ""
        status = "ok"
        body: dict[str, Any] = {}
        try:
            attempts = 0
            while attempts < 2:
                attempts += 1
                body = {
                    "model": self.model,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                    "messages": messages,
                }
                raw = json.dumps(body).encode("utf-8")
                req = urllib.request.Request(url=url, data=raw, headers=req_headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                try:
                    content = str(payload["choices"][0]["message"]["content"])
                except (KeyError, IndexError, TypeError) as exc:
                    status = "error"
                    error_message = f"LLM response format error: {exc}"
                    self._record_llm_exchange(
                        {
                            "call_id": call_id,
                            "call_name": call_name,
                            "status": status,
                            "started_at_utc": started_at_utc,
                            "latency_sec": round(time.time() - started_ts, 3),
                            "context": context or {},
                            "request": {
                                "url": url,
                                "headers": headers_for_log,
                                "payload": body,
                            },
                            "response": payload if isinstance(payload, dict) else {},
                            "response_text": content,
                            "parsed": {},
                            "error": error_message,
                        }
                    )
                    self._fail(error_message)

                parsed = self._extract_json_object(content)
                if isinstance(parsed, dict):
                    self._record_llm_exchange(
                        {
                            "call_id": call_id,
                            "call_name": call_name,
                            "status": status,
                            "started_at_utc": started_at_utc,
                            "latency_sec": round(time.time() - started_ts, 3),
                            "context": context or {},
                            "request": {
                                "url": url,
                                "headers": headers_for_log,
                                "payload": body,
                            },
                            "response": payload if isinstance(payload, dict) else {},
                            "response_text": content,
                            "parsed": parsed,
                            "error": "",
                        }
                    )
                    return parsed

                if attempts >= 3:
                    status = "error"
                    error_message = "Failed to parse JSON object from LLM response."
                    self._record_llm_exchange(
                        {
                            "call_id": call_id,
                            "call_name": call_name,
                            "status": status,
                            "started_at_utc": started_at_utc,
                            "latency_sec": round(time.time() - started_ts, 3),
                            "context": context or {},
                            "request": {
                                "url": url,
                                "headers": headers_for_log,
                                "payload": body,
                            },
                            "response": payload if isinstance(payload, dict) else {},
                            "response_text": content,
                            "parsed": {},
                            "error": error_message,
                        }
                    )
                    self._fail(error_message)

                messages = messages + [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": repair_instruction},
                ]
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            status = "error"
            error_message = f"LLM call failed: {exc}"
            self._record_llm_exchange(
                {
                    "call_id": call_id,
                    "call_name": call_name,
                    "status": status,
                    "started_at_utc": started_at_utc,
                    "latency_sec": round(time.time() - started_ts, 3),
                    "context": context or {},
                    "request": {
                        "url": url,
                        "headers": headers_for_log,
                        "payload": body,
                    },
                    "response": payload if isinstance(payload, dict) else {},
                    "response_text": content,
                    "parsed": {},
                    "error": error_message,
                }
            )
            self._fail(error_message)

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
        if phase in {"plan", "urgent", "townhall", "norms"}:
            payload["allowed_task_priority_keys"] = list(self.allowed_task_priority_keys)
        if phase in {"townhall", "norms", "plan", "selector"}:
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
        if self.norms_enabled and phase in {"norms", "townhall"}:
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
        compact_payload = self._prune_prompt_value(payload)
        return (
            f"{title}\n"
            f"Input JSON:\n{json.dumps(compact_payload, ensure_ascii=False)}\n\n"
            f"Return JSON schema:\n{schema_hint}\n"
        )

    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        fallback = StrategyState(notes=[], summary="", diagnosis={})
        prompt = self._prompt(
            title="Diagnose today's plant-level operating risks from observation.",
            payload={
                "observation": self._planner_observation_view(observation),
            },
            schema_hint='{"summary": str, "flow_risks": [str], "maintenance_risks": [str], "inspection_risks": [str], "battery_risks": [str], "evidence": [str]}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt=self._shared_system_prompt(
                "You are a manufacturing strategy planner responsible for plant-level daily diagnosis.",
                [
                    "This phase is diagnosis, not direct task dispatch.",
                    "Return a structured diagnosis with summary, flow_risks, maintenance_risks, inspection_risks, battery_risks, and evidence.",
                    "Keep each list short, concrete, and grounded in the current plant state only.",
                    "Do not assume any fixed bottleneck station or hidden system state.",
                ],
                include_task_family_semantics=False,
                include_norm_semantics=False,
            ),
            call_name="reflect",
            context={"phase": "reflect", "day": observation.get("day")},
        )
        return self._build_strategy(llm_obj, fallback)

    def propose_jobs(
        self,
        observation: dict[str, Any],
        strategy: StrategyState,
        norms: dict[str, Any],
    ) -> JobPlan:
        fallback = self._default_job_plan(norms, observation)
        prompt = self._prompt(
            title="Propose direct task-priority plan for this day.",
            payload={
                "observation": self._planner_observation_view(observation),
                "diagnosis": self._strategy_prompt_payload(strategy),
                "current_norms": norms,
                "current_agent_priority_profiles": self._summarize_agent_priority_profiles(non_neutral_only=True),
                "guardrails": self._llm_guardrails_payload("plan"),
                "memory": self._memory_context("plan"),
            },
            schema_hint='{"task_priority_weights": "map<allowed_task_priority_key,float>", "quotas": "map<allowed_quota_key,int>", "rationale": str}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt=self._shared_system_prompt(
                "You are a manufacturing operations planner responsible for daily direct task-priority planning.",
                [
                    "This phase sets the shared baseline task-family priority weights and daily quotas for the team.",
                    "Read the observation, structured diagnosis, and current agent priority profiles directly; do not treat memory or norms as mandatory prescriptions.",
                    "The shared baseline should complement existing agent specializations instead of redundantly pushing every agent toward the same task family.",
                    "Use only the allowed task priority keys and quota keys.",
                    "Keep values within the provided guardrail ranges.",
                    "Do not escalate weights or quotas without clear evidence in the current observation.",
                    self._communication_language_instruction(["rationale"]),
                ],
            ),
            call_name="propose_jobs",
            context={"phase": "propose_jobs", "day": observation.get("day")},
        )
        return self._build_job_plan(llm_obj, fallback)

    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        self._last_discussion_trace = []
        fallback = dict(norms)
        agent_experience_payload = self._agent_experience_prompt_payload(day_summary)
        current_agent_profiles = self._clone_agent_priority_multipliers()

        def _apply_agent_updates(raw_updates: Any) -> dict[str, Any]:
            experience_profiles, experience_trace = self._experience_adjusted_agent_profiles(day_summary)
            llm_updates = self._sanitize_agent_priority_profile_updates(raw_updates)
            # Experience stays primary. The LLM can steer each agent profile, but only by
            # blending toward a bounded target instead of replacing the overlay outright.
            merged_profiles = self._apply_agent_priority_target_updates(
                experience_profiles,
                llm_updates,
                blend=self.agent_priority_llm_blend,
            )
            previous_profiles = self._clone_agent_priority_multipliers(self.agent_priority_multipliers)
            self.agent_priority_multipliers = merged_profiles
            profile_delta: dict[str, Any] = {}
            for agent_id in self.agent_ids:
                before = previous_profiles.get(agent_id, {})
                after = merged_profiles.get(agent_id, {})
                changed = {
                    key: {"from": round(float(before.get(key, 1.0)), 3), "to": round(float(after.get(key, 1.0)), 3)}
                    for key in self.allowed_task_priority_keys
                    if abs(float(after.get(key, 1.0)) - float(before.get(key, 1.0))) >= 0.001
                }
                if changed:
                    profile_delta[agent_id] = changed
            self._last_agent_priority_update_trace = {
                "experience_trace": experience_trace,
                "llm_updates": llm_updates,
                "profile_delta": profile_delta,
                "agent_priority_profiles": self._clone_agent_priority_multipliers(),
            }
            return self._last_agent_priority_update_trace

        if not self.communication_enabled:
            prompt = self._prompt(
                title="Update shared norms and per-agent task priority profiles after reviewing the day summary.",
                payload={
                    "day_summary": self._day_summary_prompt_view(day_summary),
                    "current_norms": norms,
                    "current_agent_priority_profiles": self._summarize_agent_priority_profiles(current_agent_profiles, non_neutral_only=True),
                    "agent_experience": agent_experience_payload,
                    "memory": self._memory_context("norms"),
                    "language": self.communication_language,
                    "guardrails": self._llm_guardrails_payload("norms"),
                    "communication_enabled": False,
                },
                schema_hint='{"updated_norms": "map<allowed_norm_key,number>", "agent_priority_updates": "map<allowed_agent_id,map<allowed_task_priority_key,float>>", "summary": str}',
            )
            llm_obj = self._call_llm_json(
                user_prompt=prompt,
                system_prompt=self._shared_system_prompt(
                    "You are a manufacturing townhall moderator responsible for updating shared norms and per-agent task specializations.",
                    [
                        "This phase reviews the day summary and per-agent experience to update shared norms and per-agent task-priority profiles.",
                        "Keep agent-specific profiles close to neutral unless repeated experience justifies specialization.",
                        "Use only the allowed norm keys, task priority keys, and agent IDs.",
                        "If evidence is weak or ambiguous, keep the fallback norms and current agent profiles.",
                        self._communication_language_instruction(["summary"]),
                    ],
                ),
                call_name="discuss_norm_update",
                context={"phase": "discuss_norm_update", "day": day_summary.get("day"), "communication_enabled": False},
            )
            updated_norms_raw = llm_obj.get("updated_norms") if isinstance(llm_obj, dict) else {}
            summary = str(llm_obj.get("summary", "")).strip() if isinstance(llm_obj, dict) else ""
            sanitized_norms = self._build_norms(updated_norms_raw, fallback)
            agent_priority_trace = _apply_agent_updates(llm_obj.get("agent_priority_updates") if isinstance(llm_obj, dict) else {})
            memory_update = self._update_memory(day_summary, sanitized_norms, [], summary)
            self._last_discussion_trace.append(
                {
                    "mode": "communication_off",
                    "summary": summary,
                    "rounds": 0,
                    "messages": 0,
                    "memory_update": memory_update,
                    "agent_priority_update_trace": agent_priority_trace,
                }
            )
            return sanitized_norms

        transcript: list[dict[str, Any]] = []
        round_plan, moderator_note = self._townhall_round_plan(day_summary, norms)
        self._last_discussion_trace.append(
            {
                "role": "moderator",
                "mode": "round_plan",
                "round_plan": round_plan,
                "moderator_note": moderator_note,
            }
        )
        for round_spec in round_plan:
            ridx = int(round_spec.get("round", 0) or 0)
            stage_spec = self._townhall_stage_lookup(round_spec.get("stage_id", ""))
            for aid in self.agent_ids:
                trimmed = transcript[-self.comm_max_transcript :]
                agent_memory = self.agent_memories.get(aid, [])[-1:] if self.include_agent_memory else []
                peer_priority_summary = self._summarize_agent_priority_profiles(
                    {peer_id: current_agent_profiles.get(peer_id, {}) for peer_id in self.agent_ids if peer_id != aid},
                    top_n=1,
                    non_neutral_only=True,
                )
                prompt = self._prompt(
                    title=f"Townhall round {ridx}, speaker {aid}, {stage_spec['label']}",
                    payload={
                        "agent_id": aid,
                        "round": ridx,
                        "round_stage": {
                            "stage_id": stage_spec["stage_id"],
                            "step": stage_spec["step"],
                            "label": stage_spec["label"],
                            "objective": str(round_spec.get("focus", stage_spec["focus"])).strip() or stage_spec["focus"],
                            "stage_round_index": int(round_spec.get("stage_round_index", 1) or 1),
                            "stage_round_count": int(round_spec.get("stage_round_count", 1) or 1),
                            "moderator_note": moderator_note,
                        },
                        "day_summary": self._day_summary_prompt_view(day_summary),
                        "current_norms": norms,
                        "language": self.communication_language,
                        "guardrails": self._llm_guardrails_payload("townhall"),
                        "shared_memory": self._memory_context("townhall"),
                        "agent_memory": agent_memory,
                        "agent_experience": agent_experience_payload.get(aid, {}),
                        "speaker_priority_profile": self._summarize_agent_priority_profiles(
                            {aid: current_agent_profiles.get(aid, {})},
                            non_neutral_only=True,
                        ).get(aid, {}),
                        "peer_specialization_summary": peer_priority_summary,
                        "recent_highlights": self._townhall_recent_highlights(trimmed, limit=2),
                    },
                    schema_hint='{"utterance": str, "contribution_type": "one_of[new_evidence,proposal_weakness,alternative_task_family,short_term_vs_long_term_tradeoff]", "novelty_basis": str, "proposal": {"norm_updates": "map<allowed_norm_key,number>", "priority_updates": "map<allowed_task_priority_key,float>"}}',
                )
                llm_obj = self._call_llm_json(
                    user_prompt=prompt,
                    system_prompt=self._shared_system_prompt(
                        f"You are {aid}, one mobile agent participating in a manufacturing townhall discussion.",
                        [
                            f"This round follows {stage_spec['label']}",
                            "Produce one concise utterance and an optional proposal as JSON.",
                            "Use only the allowed norm keys and direct task priority keys.",
                            "Ground arguments in the day summary, your own experience, and recent highlights, not vague preference.",
                            "Use peer_specialization_summary only as lightweight context; do not mirror it automatically.",
                            "Do not keep ratcheting values upward without clear new evidence from the day summary.",
                            "Your utterance must contain at least one of these: new evidence, a weakness in a prior proposal, an alternative using different task families, or a short-term vs long-term trade-off.",
                            "Do not merely agree, restate prior points, or paraphrase the transcript.",
                            f"Prefer contribution types compatible with this round: {', '.join(stage_spec['expected_contributions'])}.",
                            self._communication_language_instruction(["utterance", "novelty_basis"]),
                        ],
                        task_family_subset=self._townhall_task_family_subset(),
                    ),
                    call_name="townhall_round",
                    context={
                        "phase": "townhall_round",
                        "day": day_summary.get("day"),
                        "round": ridx,
                        "agent_id": aid,
                        "stage_id": stage_spec["stage_id"],
                    },
                )
                utterance = str(llm_obj.get("utterance", "")).strip()
                if not utterance:
                    self._fail("discussion utterance is empty.")
                contribution_type = str(llm_obj.get("contribution_type", "")).strip()
                if contribution_type not in self.TOWNHALL_CONTRIBUTION_TYPES:
                    contribution_type = self._default_townhall_contribution_type(stage_spec["stage_id"])
                novelty_basis = str(llm_obj.get("novelty_basis", "")).strip()
                if not novelty_basis:
                    novelty_basis = self._truncate_prompt_text(utterance, max_len=120)
                proposal = llm_obj.get("proposal", {})
                if not isinstance(proposal, dict):
                    proposal = {}
                transcript.append(
                    {
                        "round": ridx,
                        "stage_id": stage_spec["stage_id"],
                        "stage_label": stage_spec["label"],
                        "agent_id": aid,
                        "utterance": utterance,
                        "contribution_type": contribution_type,
                        "novelty_basis": novelty_basis,
                        "proposal": proposal,
                    }
                )

        synthesis_prompt = self._prompt(
            title="Synthesize townhall transcript into updated shared norms and per-agent task priority profiles.",
            payload={
                "day_summary": self._day_summary_prompt_view(day_summary),
                "current_norms": norms,
                "current_agent_priority_profiles": self._summarize_agent_priority_profiles(current_agent_profiles, non_neutral_only=True),
                "agent_experience": agent_experience_payload,
                "guardrails": self._llm_guardrails_payload("norms"),
                "memory": self._memory_context("norms"),
                "language": self.communication_language,
                "round_plan": round_plan,
                "recent_highlights": self._townhall_recent_highlights(transcript[-self.comm_max_transcript :], limit=2),
            },
            schema_hint='{"updated_norms": "map<allowed_norm_key,number>", "agent_priority_updates": "map<allowed_agent_id,map<allowed_task_priority_key,float>>", "summary": str}',
        )
        synthesis = self._call_llm_json(
            user_prompt=synthesis_prompt,
            system_prompt=self._shared_system_prompt(
                "You are a manufacturing townhall moderator responsible for synthesizing discussion into stable shared norms and per-agent specializations.",
                [
                    "Build consensus and return updated_norms plus per-agent task-priority profile updates as JSON.",
                    "Agent-specific profiles should diverge only when repeated experience and the discussion both support specialization.",
                    "Return only the allowed norm keys, task priority keys, and agent IDs, and keep all values within the provided ranges.",
                    "Reject noisy one-off reactions and keep fallback norms or existing agent profiles if the evidence is weak.",
                    "Use the round plan to respect the progression from diagnosis to critique, alternatives, trade-off comparison, and executable agreement.",
                    self._communication_language_instruction(["summary"]),
                ],
                task_family_subset=self._townhall_task_family_subset(),
            ),
            call_name="townhall_synthesis",
            context={"phase": "townhall_synthesis", "day": day_summary.get("day"), "rounds": len(round_plan), "max_rounds": self.comm_rounds},
        )
        updated_norms_raw = synthesis.get("updated_norms") if isinstance(synthesis, dict) else {}
        norms_fallback_used = not isinstance(updated_norms_raw, dict)
        if norms_fallback_used:
            updated_norms_raw = dict(fallback)
        summary = str(synthesis.get("summary", "")).strip() if isinstance(synthesis, dict) else ""
        if not summary:
            summary = "Townhall synthesis returned no summary; kept previous norms."
        updated_norms = self._build_norms(updated_norms_raw, fallback)
        agent_priority_trace = _apply_agent_updates(synthesis.get("agent_priority_updates") if isinstance(synthesis, dict) else {})
        memory_update = self._update_memory(day_summary, updated_norms, transcript, summary)
        self._last_discussion_trace = transcript + [
            {
                "role": "moderator",
                "summary": summary,
                "rounds": len(round_plan),
                "messages": len(transcript),
                "communication_enabled": True,
                "norms_fallback_used": norms_fallback_used,
                "memory_update": memory_update,
                "agent_priority_update_trace": agent_priority_trace,
            }
        ]
        return updated_norms

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
