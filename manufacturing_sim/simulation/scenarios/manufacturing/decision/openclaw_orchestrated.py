from __future__ import annotations

import json
import re
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
        detector_cfg = orch_cfg.get("detector", {}) if isinstance(orch_cfg.get("detector", {}), dict) else {}
        self.detector_max_top_bottlenecks = self._clamp_int(detector_cfg.get("max_top_bottlenecks", 3), 1, 3, 3)
        evaluator_cfg = orch_cfg.get("evaluator", {}) if isinstance(orch_cfg.get("evaluator", {}), dict) else {}
        self.evaluator_enabled = bool(evaluator_cfg.get("enabled", False))
        self.evaluator_max_revision_requests = self._clamp_int(evaluator_cfg.get("max_revision_requests", 2), 0, 5, 2)
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
        self.last_diagnosis_review: dict[str, Any] = {}
        self.machine_recurrence_history: dict[str, dict[str, Any]] = {}
        self.detector_prompt_history: list[dict[str, Any]] = []
        self.evaluator_prompt_history: list[dict[str, Any]] = []
        self.planner_prompt_history: list[dict[str, Any]] = []
        self.detector_run_history: list[dict[str, Any]] = []
        self.evaluator_run_history: list[dict[str, Any]] = []
        self.planner_run_history: list[dict[str, Any]] = []
        self.reflector_run_history: list[dict[str, Any]] = []
        self.current_job_plan = self._empty_current_job_plan()
        series_cfg = self.cfg.get("_run_series", {}) if isinstance(self.cfg.get("_run_series", {}), dict) else {}
        self.run_series_index = max(1, int(series_cfg.get("run_index", 1) or 1))
        self.run_series_total = max(self.run_series_index, int(series_cfg.get("total_runs", 1) or 1))
        parent_output_raw = str(series_cfg.get("parent_output_dir", "")).strip()
        knowledge_path_raw = str(series_cfg.get("knowledge_path", "")).strip()
        knowledge_history_dir_raw = str(series_cfg.get("knowledge_history_dir", "")).strip()
        self.run_series_parent_output_dir = Path(parent_output_raw) if parent_output_raw else None
        self.series_knowledge_path = Path(knowledge_path_raw) if knowledge_path_raw else None
        self.series_knowledge_history_dir = Path(knowledge_history_dir_raw) if knowledge_history_dir_raw else None
        self.series_knowledge_text = ""
        self.run_output_root: Path | None = None

    def _reset_run_state(self) -> None:
        super()._reset_run_state()
        self.last_manager_review = {}
        self.last_worker_reports = {}
        self.last_diagnosis_review = {}
        self.machine_recurrence_history = {}
        self.detector_prompt_history = []
        self.evaluator_prompt_history = []
        self.planner_prompt_history = []
        self.detector_run_history = []
        self.evaluator_run_history = []
        self.planner_run_history = []
        self.reflector_run_history = []
        self.current_job_plan = self._empty_current_job_plan()
        self.series_knowledge_text = ""
        self.run_output_root = None

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

    def _knowledge_stub_markdown(self) -> str:
        return "\n".join(
            [
                "# Run-Series Knowledge",
                "",
                "## Run-Series Scope",
                "No prior cross-run knowledge has been accumulated yet.",
                "",
                "## Persistent Lessons",
                "- No persistent lessons recorded yet.",
                "",
                "## Latest Lessons",
                "- No latest lessons recorded yet.",
                "",
                "## Detector Guidance",
                "- No detector guidance recorded yet.",
                "",
                "## Planner Guidance",
                "- No planner guidance recorded yet.",
                "",
                "## Open Watchouts",
                "- No open watchouts recorded yet.",
                "",
            ]
        )

    def _load_series_knowledge_text(self) -> str:
        if self.series_knowledge_path is None:
            return self._knowledge_stub_markdown()
        try:
            text = self.series_knowledge_path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        return text or self._knowledge_stub_markdown()

    @staticmethod
    def _parse_markdown_json_array_section(markdown: str, section_title: str) -> list[str]:
        if not isinstance(markdown, str) or not markdown.strip() or not str(section_title).strip():
            return []
        pattern = re.compile(
            rf"##\s+{re.escape(section_title)}\s+```json\s*(\[[\s\S]*?\])\s*```",
            re.IGNORECASE,
        )
        match = pattern.search(markdown)
        if not match:
            return []
        try:
            payload = json.loads(match.group(1))
        except Exception:
            return []
        rows: list[str] = []
        if not isinstance(payload, list):
            return rows
        for raw in payload:
            text = str(raw).strip()
            if text:
                rows.append(text)
        return rows

    def _parse_series_knowledge_sections(self, markdown: str) -> dict[str, list[str]]:
        persistent = self._parse_markdown_json_array_section(markdown, "Persistent Lessons")
        if not persistent:
            persistent = self._parse_markdown_json_array_section(markdown, "Carry-Forward Lessons")
        return {
            "persistent_lessons": persistent,
            "latest_lessons": self._parse_markdown_json_array_section(markdown, "Latest Lessons"),
            "detector_guidance": self._parse_markdown_json_array_section(markdown, "Detector Guidance"),
            "planner_guidance": self._parse_markdown_json_array_section(markdown, "Planner Guidance"),
            "open_watchouts": self._parse_markdown_json_array_section(markdown, "Open Watchouts"),
        }

    @staticmethod
    def _merge_deduped_strings(
        current_items: list[str],
        prior_items: list[str],
        *,
        limit: int,
    ) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for raw in [*current_items, *prior_items]:
            text = str(raw).strip()
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(text)
            if len(merged) >= max(1, int(limit or 1)):
                break
        return merged

    def _manager_knowledge_workspace_aliases(self) -> list[str]:
        aliases = ["MANAGER_BOTTLENECK_DETECTOR", "MANAGER_DAILY_PLANNER", "MANAGER_RUN_REFLECTOR"]
        if self.evaluator_enabled:
            aliases.append("MANAGER_DIAGNOSIS_EVALUATOR")
        return aliases

    def _seed_cross_run_knowledge(self) -> None:
        if not self._openclaw_enabled() or self.openclaw_runtime_workspace_root is None:
            return
        knowledge_text = self._load_series_knowledge_text()
        self.series_knowledge_text = knowledge_text
        series_payload = {
            "run_index": int(self.run_series_index),
            "total_runs": int(self.run_series_total),
            "knowledge_path": str(self.series_knowledge_path.resolve()) if self.series_knowledge_path is not None else "",
        }
        for workspace_alias in self._manager_knowledge_workspace_aliases():
            workspace = self.openclaw_runtime_workspace_root / workspace_alias
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "KNOWLEDGE.md").write_text(knowledge_text + ("\n" if not knowledge_text.endswith("\n") else ""), encoding="utf-8")
            self._openclaw_write_json(workspace / "facts" / "run_series_context.json", series_payload)

    def _phase_runtime_agent_suffix(self) -> str:
        raw = str(self.openclaw_run_id or "run").strip().upper().replace(":", "").replace("-", "")
        return raw or "RUN"

    def _simulation_total_days(self) -> int:
        horizon_cfg = self.cfg.get("horizon", {}) if isinstance(self.cfg.get("horizon", {}), dict) else {}
        return max(1, int(horizon_cfg.get("num_days", 1) or 1))

    def _sanitize_detector_top_bottlenecks(self, src: Any) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        if not isinstance(src, list):
            return sanitized
        for raw in src:
            if not isinstance(raw, dict):
                continue
            name = self._truncate_prompt_text(raw.get("name", raw.get("signal", "")), max_len=72)
            if not name:
                continue
            severity = str(raw.get("severity", "medium")).strip().lower()
            if severity not in {"low", "medium", "high"}:
                severity = "medium"
            evidence: list[dict[str, Any]] = []
            raw_evidence = raw.get("evidence", []) if isinstance(raw.get("evidence", []), list) else []
            for item in raw_evidence[:4]:
                if not isinstance(item, dict):
                    continue
                metric = self._truncate_prompt_text(item.get("metric", item.get("signal", "")), max_len=72)
                if not metric:
                    continue
                evidence.append(
                    {
                        "metric": metric,
                        "value": item.get("value"),
                    }
                )
            sanitized.append(
                {
                    "name": name,
                    "rank": len(sanitized) + 1,
                    "severity": severity,
                    "evidence": evidence,
                    "why_it_limits_output": self._truncate_prompt_text(
                        raw.get("why_it_limits_output", raw.get("why_now", "")),
                        max_len=240,
                    ),
                }
            )
            if len(sanitized) >= self.detector_max_top_bottlenecks:
                break
        for idx, item in enumerate(sanitized, start=1):
            item["rank"] = idx
        return sanitized

    @staticmethod
    def _append_prompt_history(seq: list[dict[str, Any]], item: dict[str, Any], limit: int = 3) -> None:
        if not isinstance(item, dict):
            return
        seq.append(item)
        if len(seq) > limit:
            del seq[: len(seq) - limit]

    def _compact_bottleneck_list(self, src: Any, limit: int = 3) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in self._sanitize_detector_top_bottlenecks(src)[: max(1, int(limit or 3))]:
            evidence = item.get("evidence", []) if isinstance(item.get("evidence", []), list) else []
            compact.append(
                {
                    "rank": int(item.get("rank", len(compact) + 1) or (len(compact) + 1)),
                    "name": str(item.get("name", "")).strip(),
                    "severity": str(item.get("severity", "medium")).strip() or "medium",
                    "evidence": [
                        self._truncate_prompt_text(
                            f"{str(ev.get('metric', '')).strip()}={ev.get('value')}",
                            max_len=72,
                        )
                        for ev in evidence[:3]
                        if isinstance(ev, dict) and str(ev.get("metric", "")).strip()
                    ],
                }
            )
        return compact

    def _compact_machine_recurrence_summary(self, recurrence_summary: dict[str, Any], limit: int = 5) -> dict[str, Any]:
        summary = recurrence_summary if isinstance(recurrence_summary, dict) else {}
        rows = summary.get("top_recurrence_machines", []) if isinstance(summary.get("top_recurrence_machines", []), list) else []
        compact_rows: list[dict[str, Any]] = []
        for item in rows[: max(1, int(limit or 5))]:
            if not isinstance(item, dict):
                continue
            compact_rows.append(
                {
                    "machine_id": str(item.get("machine_id", "")).strip(),
                    "station": item.get("station"),
                    "broken_day_count": int(item.get("broken_day_count", 0) or 0),
                    "consecutive_broken_days": int(item.get("consecutive_broken_days", 0) or 0),
                    "current_broken": bool(item.get("current_broken", False)),
                    "latest_minutes_since_last_pm": item.get("latest_minutes_since_last_pm"),
                }
            )
        return {
            "machines_with_repeat_breakdowns": int(summary.get("machines_with_repeat_breakdowns", 0) or 0),
            "currently_broken_machines": [str(item) for item in list(summary.get("currently_broken_machines", []))[:6]],
            "top_recurrence_machines": compact_rows,
        }

    def _compact_recurring_issue_summary(self, diagnosis_entries: Any, limit: int = 5) -> list[dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        entries = diagnosis_entries if isinstance(diagnosis_entries, list) else []
        for raw_entry in entries:
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            day = int(entry.get("day", 0) or 0)
            for bottleneck in self._compact_bottleneck_list(entry.get("top_bottlenecks", []), limit=5):
                if not isinstance(bottleneck, dict):
                    continue
                name = str(bottleneck.get("name", "")).strip()
                if not name:
                    continue
                item = stats.setdefault(
                    name,
                    {
                        "issue_name": name,
                        "days_seen": [],
                        "count": 0,
                        "best_rank": 99,
                        "recent_severities": [],
                    },
                )
                if day and day not in item["days_seen"]:
                    item["days_seen"].append(day)
                item["count"] = int(item.get("count", 0) or 0) + 1
                item["best_rank"] = min(int(item.get("best_rank", 99) or 99), int(bottleneck.get("rank", 99) or 99))
                severity = str(bottleneck.get("severity", "medium")).strip() or "medium"
                recent_severities = item.get("recent_severities", [])
                if isinstance(recent_severities, list) and severity not in recent_severities:
                    recent_severities.append(severity)
                    item["recent_severities"] = recent_severities[:3]
        rows: list[dict[str, Any]] = []
        for item in stats.values():
            days_seen = sorted(int(day) for day in item.get("days_seen", []) if int(day or 0) > 0)
            repeat_count = len(days_seen)
            if repeat_count < 2:
                continue
            rows.append(
                {
                    "issue_name": str(item.get("issue_name", "")).strip(),
                    "repeat_day_count": repeat_count,
                    "days_seen": days_seen[-3:],
                    "best_rank": int(item.get("best_rank", 99) or 99),
                    "recent_severities": list(item.get("recent_severities", []))[:3],
                    "still_active_in_latest": bool(days_seen and entries and int((entries[-1] if isinstance(entries[-1], dict) else {}).get("day", 0) or 0) == days_seen[-1]),
                }
            )
        rows.sort(
            key=lambda item: (
                -int(item.get("repeat_day_count", 0) or 0),
                int(item.get("best_rank", 99) or 99),
                str(item.get("issue_name", "")),
            )
        )
        return rows[: max(1, int(limit or 5))]

    def _compact_plan_focus(self, weights: Any, queues: Any) -> dict[str, Any]:
        weight_map = weights if isinstance(weights, dict) else {}
        ranked = sorted(
            (
                (str(key), float(value or 0.0))
                for key, value in weight_map.items()
                if str(key).strip() in self.allowed_task_priority_keys
            ),
            key=lambda item: float(item[1]),
            reverse=True,
        )
        queue_map = queues if isinstance(queues, dict) else {}
        queue_focus: dict[str, list[str]] = {}
        for agent_id in self.agent_ids:
            orders = queue_map.get(agent_id, []) if isinstance(queue_map.get(agent_id, []), list) else []
            compact_orders: list[str] = []
            for order in orders[:2]:
                if not isinstance(order, dict):
                    continue
                task = str(order.get("task_family", "")).strip()
                if not task:
                    continue
                target = str(order.get("target_id", "")).strip()
                compact_orders.append(f"{task}@{target}" if target else task)
            if compact_orders:
                queue_focus[agent_id] = compact_orders
        return {
            "top_weighted_tasks": [
                {"task_family": task_family, "weight": round(weight, 3)}
                for task_family, weight in ranked[:3]
            ],
            "queue_focus": queue_focus,
        }

    def _detector_prompt_memory_payload(
        self,
        latest_entry: dict[str, Any],
        recurrence_summary: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        recent_trend = [
            {
                "day": int(item.get("day", 0) or 0),
                "summary": str(item.get("summary", "")).strip(),
                "top_bottlenecks": self._compact_bottleneck_list(item.get("top_bottlenecks", []), limit=3),
            }
            for item in self.detector_prompt_history[-3:]
            if isinstance(item, dict)
        ]
        repeated_counts: dict[str, int] = {}
        for item in recent_trend:
            for bottleneck in item.get("top_bottlenecks", []):
                name = str((bottleneck or {}).get("name", "")).strip()
                if name:
                    repeated_counts[name] = repeated_counts.get(name, 0) + 1
        recurring_issue_summary = self._compact_recurring_issue_summary(recent_trend, limit=5)
        persistent_watchouts = [
            f"recurring_bottleneck={name}"
            for name, count in sorted(repeated_counts.items(), key=lambda entry: (-entry[1], entry[0]))
            if count >= 2
        ]
        recurring_machine_summary = self._compact_machine_recurrence_summary(recurrence_summary, limit=5)
        if int(recurring_machine_summary.get("machines_with_repeat_breakdowns", 0) or 0) > 0:
            persistent_watchouts.append(
                f"repeat_breakdown_machines={int(recurring_machine_summary.get('machines_with_repeat_breakdowns', 0) or 0)}"
            )
        payload = {
            "latest_diagnosis": latest_entry,
            "recent_diagnosis_trend": recent_trend,
            "recurring_issue_summary": recurring_issue_summary,
            "recurring_machine_breakdown_summary": recurring_machine_summary,
            "persistent_watchouts": persistent_watchouts[:5],
        }
        commitment = {
            "summary": str(latest_entry.get("summary", "")).strip(),
            "focus_items": [str(item.get("name", "")).strip() for item in latest_entry.get("top_bottlenecks", [])[:3] if isinstance(item, dict) and str(item.get("name", "")).strip()],
            "watchouts": payload["persistent_watchouts"],
        }
        return payload, commitment, recurring_machine_summary

    def _evaluator_prompt_memory_payload(
        self,
        latest_entry: dict[str, Any],
        recurrence_summary: dict[str, Any],
        diagnosis_review: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        recurring_issue_summary = self._compact_recurring_issue_summary(self.detector_prompt_history[-3:], limit=5)
        recent_reviews = [
            {
                "day": int(item.get("day", 0) or 0),
                "review_status": str(item.get("review_status", "")).strip(),
                "final_verdict": str(item.get("final_verdict", "")).strip(),
                "review_rounds": int(item.get("review_rounds", 0) or 0),
                "summary": str(item.get("summary", "")).strip(),
            }
            for item in self.evaluator_prompt_history[-3:]
            if isinstance(item, dict)
        ]
        unresolved_watchouts: list[str] = []
        evaluator_reviews = diagnosis_review.get("evaluator_reviews", []) if isinstance(diagnosis_review.get("evaluator_reviews", []), list) else []
        for review in evaluator_reviews[-2:]:
            if not isinstance(review, dict):
                continue
            for req in list(review.get("revision_requests", []))[:2]:
                if not isinstance(req, dict):
                    continue
                issue = self._truncate_prompt_text(req.get("issue", ""), max_len=96)
                if issue:
                    unresolved_watchouts.append(issue)
        recurring_machine_summary = self._compact_machine_recurrence_summary(recurrence_summary, limit=5)
        payload = {
            "latest_review": latest_entry,
            "recent_review_rounds": recent_reviews,
            "recurring_issue_summary": recurring_issue_summary,
            "recurring_machine_summary": recurring_machine_summary,
            "unresolved_diagnosis_watchouts": unresolved_watchouts[:5],
        }
        commitment = {
            "summary": str(latest_entry.get("summary", "")).strip(),
            "quality_focus": [
                "ranking_quality",
                "evidence_quality",
                "severity_calibration",
                "explanation_quality",
            ],
            "watchouts": payload["unresolved_diagnosis_watchouts"],
        }
        return payload, commitment, recurring_machine_summary

    def _planner_prompt_memory_payload(self, latest_entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        recent_plans = [
            {
                "day": int(item.get("day", 0) or 0),
                "summary": str(item.get("summary", "")).strip(),
                **self._compact_plan_focus(item.get("task_priority_weights", {}), item.get("personal_queues", {})),
            }
            for item in self.planner_prompt_history[-3:]
            if isinstance(item, dict)
        ]
        repair_heavy_days = sum(
            1
            for item in recent_plans
            if any(str(entry.get("task_family", "")).strip() == "repair_machine" for entry in item.get("top_weighted_tasks", []))
        )
        empty_queue_days = sum(1 for item in recent_plans if not item.get("queue_focus"))
        watchouts: list[str] = []
        if repair_heavy_days >= 2:
            watchouts.append("repeated_repair_heavy_focus")
        if empty_queue_days >= 2:
            watchouts.append("repeated_sparse_queue_focus")
        latest_focus = self._compact_plan_focus(latest_entry.get("task_priority_weights", {}), latest_entry.get("personal_queues", {}))
        payload = {
            "latest_plan_focus": {
                "day": int(latest_entry.get("day", 0) or 0),
                "summary": str(latest_entry.get("summary", "")).strip(),
                **latest_focus,
                "detector_alignment": str(latest_entry.get("detector_alignment", "follow")).strip() or "follow",
            },
            "recent_plan_trend": recent_plans,
            "watchouts": watchouts[:5],
        }
        commitment = {
            "summary": str(latest_entry.get("summary", "")).strip(),
            "focus_tasks": [str(item.get("task_family", "")).strip() for item in latest_focus.get("top_weighted_tasks", []) if str(item.get("task_family", "")).strip()],
            "watchouts": payload["watchouts"],
        }
        return payload, commitment

    def _reflector_prompt_memory_payload(self, latest_entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        payload = {
            "latest_reflection": latest_entry,
            "recent_reflections": list(self.reflector_run_history[-3:]),
        }
        commitment = {
            "summary": str(latest_entry.get("summary", "")).strip(),
            "focus_items": list(latest_entry.get("carry_forward_lessons", []))[:5],
        }
        return payload, commitment

    def _detector_turn_bundle(
        self,
        observation: dict[str, Any],
        *,
        retry: bool = False,
        prior_detector_draft: dict[str, Any] | None = None,
        evaluator_feedback: dict[str, Any] | None = None,
        revision_index: int = 0,
    ) -> tuple[str, str, str, dict[str, str]]:
        day = int(observation.get("day", 0) or 0)
        runtime_agent_id = self._phase_runtime_agent_id("manager_bottleneck_detector", {"phase": "manager_bottleneck_detector", "day": day})
        detector_count = self.detector_max_top_bottlenecks
        instructions = [
            f"Return exactly {detector_count} ranked bottlenecks, no more and no fewer.",
            "If fewer than that many strong bottlenecks exist, still include weaker secondary or tertiary constraints with lower severity instead of returning fewer entries.",
            "Rank the bottlenecks that most limit accepted finished-product completion over the remaining horizon, using the current request payload as primary evidence.",
            "Re-read KNOWLEDGE.md, MEMORY.md, and memory/rolling_summary.md before ranking so recurring or chronic constraints are not missed.",
            "Use run-local memory and cross-run knowledge to check persistence and recurrence, but let current facts override stale memory or stale prior guidance when they conflict.",
            "Before selecting rank 1, compare at least two competing bottleneck hypotheses using current facts plus relevant run-local memory.",
            "Recurring or chronic constraints may rank above a purely immediate issue when current facts show they are still materially limiting productivity.",
            f"top_bottlenecks must contain exactly {detector_count} objects, each with name, rank, severity, evidence[{{metric,value}}], and why_it_limits_output.",
            "Copy evidence values from the current request payload only. Use memory to inform ranking, not to invent unsupported evidence values.",
            "Return compact diagnosis only and do not narrate the full plant state back.",
        ]
        if retry:
            instructions.insert(
                0,
                f"Your previous reply did not contain exactly {detector_count} valid top_bottlenecks entries. Re-read the current facts and return exactly {detector_count} valid entries this time.",
            )
        if evaluator_feedback:
            instructions.insert(0, "You are revising a detector draft after evaluator review. Address every revision request directly and only keep a bottleneck unchanged if the current request facts still support it.")
            instructions.insert(1, "Use evaluator_feedback.revision_requests as mandatory quality corrections, not as optional suggestions.")
        system_prompt, prompt, required_keys = self._native_turn_prompts(
            agent_id=runtime_agent_id,
            phase="manager_bottleneck_detector",
            role_summary="You are BOTTLENECK_DETECTOR, a ranking-focused diagnostic manager whose local objective is to identify and rank the constraints that most limit accepted finished-product completion over the remaining horizon.",
            input_payload=self._detector_packet(
                observation,
                prior_detector_draft=prior_detector_draft,
                evaluator_feedback=evaluator_feedback,
                revision_index=revision_index,
            ),
            required_fields={
                "summary": "str",
                "top_bottlenecks": "list[dict]",
            },
            instructions=instructions,
            history_tag=f"day_{day:02d}_manager_bottleneck_detector{'_rev' + str(revision_index) if revision_index > 0 else ''}{'_retry1' if retry else ''}",
        )
        return runtime_agent_id, system_prompt, prompt, required_keys

    def _reflector_turn_bundle(
        self,
        run_packet: dict[str, Any],
    ) -> tuple[str, str, str, dict[str, str]]:
        runtime_agent_id = self._phase_runtime_agent_id("manager_run_reflector", {"phase": "manager_run_reflector"})
        system_prompt, prompt, required_keys = self._native_turn_prompts(
            agent_id=runtime_agent_id,
            phase="manager_run_reflector",
            role_summary="You are MANAGER_RUN_REFLECTOR, a run-level reflection manager whose local objective is to review the completed run, identify what detector and planner should have done better, and distill compact carry-forward knowledge for the next run.",
            input_payload=run_packet,
            required_fields={
                "summary": "str",
                "run_problems": "list[str]",
                "detector_should_have_done": "list[str]",
                "planner_should_have_done": "list[str]",
                "carry_forward_lessons": "list[str]",
                "detector_guidance": "list[str]",
                "planner_guidance": "list[str]",
                "open_watchouts": "list[str]",
            },
            instructions=[
                "Review the completed run holistically, not as isolated day snapshots.",
                "Use current run artifacts plus prior KNOWLEDGE.md to identify what should change in the next run.",
                "Keep the reflection compact. Summary should be at most two short sentences.",
                "Return at most three items for run_problems, detector_should_have_done, planner_should_have_done, carry_forward_lessons, detector_guidance, planner_guidance, and open_watchouts.",
                "Each list item should be one concrete sentence, ideally under 160 characters.",
                "Detector guidance must describe better diagnosis choices; planner guidance must describe better execution-planning choices.",
                "Do not restate the full KPI blob. Distill only the few changes that should matter next run.",
            ],
            history_tag=f"run_{max(1, int(self.run_series_index)):02d}_manager_run_reflector",
        )
        return runtime_agent_id, system_prompt, prompt, required_keys

    def _evaluator_turn_bundle(
        self,
        observation: dict[str, Any],
        detector_draft: dict[str, Any],
        *,
        round_index: int,
    ) -> tuple[str, str, str, dict[str, str]]:
        day = int(observation.get("day", 0) or 0)
        runtime_agent_id = self._phase_runtime_agent_id("manager_diagnosis_evaluator", {"phase": "manager_diagnosis_evaluator", "day": day})
        system_prompt, prompt, required_keys = self._native_turn_prompts(
            agent_id=runtime_agent_id,
            phase="manager_diagnosis_evaluator",
            role_summary="You are MANAGER_DIAGNOSIS_EVALUATOR, an independent review manager whose local objective is to verify that the detector draft is sufficiently grounded and planning-ready before it reaches the planner.",
            input_payload=self._evaluator_packet(observation, detector_draft, round_index=round_index),
            required_fields={
                "verdict": "str",
                "summary": "str",
                "revision_requests": "list[dict]",
            },
            instructions=[
                "Review diagnosis quality only. Do not produce a day plan, worker assignment, or priority weights.",
                "Return verdict=accept only if the detector draft is sufficiently grounded for planning.",
                "Return verdict=request_revision if the detector draft has a concrete deficiency in ranking, evidence quality, severity calibration, or explanation quality.",
                "When requesting revision, each revision_requests item must explain the deficiency and the requested correction in a way the detector can act on deterministically.",
                "Ground the review in the current request payload, KNOWLEDGE.md, and relevant run-local memory artifacts such as MEMORY.md and memory/rolling_summary.md.",
                "If a materially repeated issue remains supported by current facts, do not accept a detector draft that omits it, under-ranks it beneath weaker one-off issues, or fails to explain why the repeated issue is not currently limiting productivity.",
                "Treat unresolved recurrence as a stricter review concern than a one-off issue because repetition is evidence that the diagnosis may still be missing a durable limiter.",
                "If verdict=accept, revision_requests must be empty.",
            ],
            history_tag=f"day_{day:02d}_manager_diagnosis_evaluator_r{max(1, int(round_index or 1))}",
        )
        return runtime_agent_id, system_prompt, prompt, required_keys

    def _build_day_scoped_runtime_agent_id(self, phase: str, day: int | None = None) -> str:
        # OpenClaw local agent는 같은 agent id의 main session을 재사용하므로,
        # reflect/plan은 day별 agent id를 써서 세션 오염을 차단한다.
        suffix = self._phase_runtime_agent_suffix()
        phase_key = str(phase or "").strip().lower()
        if phase_key not in {"manager_bottleneck_detector", "manager_diagnosis_evaluator", "manager_daily_planner", "manager_run_reflector"}:
            return self.manager_agent_id
        if phase_key == "manager_run_reflector":
            return f"MANAGER_RUN_REFLECTOR_{suffix}"
        safe_day = max(1, int(day or 1))
        if phase_key == "manager_bottleneck_detector":
            prefix = "MANAGER_BOTTLENECK_DETECTOR"
        elif phase_key == "manager_diagnosis_evaluator":
            prefix = "MANAGER_DIAGNOSIS_EVALUATOR"
        else:
            prefix = "MANAGER_DAILY_PLANNER"
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
            ids[f"{self.manager_agent_id}:manager_diagnosis_evaluator:d{day}"] = self._build_day_scoped_runtime_agent_id(
                "manager_diagnosis_evaluator",
                day,
            )
            ids[f"{self.manager_agent_id}:manager_daily_planner:d{day}"] = self._build_day_scoped_runtime_agent_id(
                "manager_daily_planner",
                day,
            )
        ids[f"{self.manager_agent_id}:manager_run_reflector"] = self._build_day_scoped_runtime_agent_id("manager_run_reflector")
        return ids

    def _runtime_agent_workspace_aliases(self) -> dict[str, str]:
        # reflect와 plan은 서로 다른 workspace를 사용해 메모리 오염을 막는다.
        aliases: dict[str, str] = {}
        for day in range(1, self._simulation_total_days() + 1):
            aliases[self._build_day_scoped_runtime_agent_id("manager_bottleneck_detector", day)] = "MANAGER_BOTTLENECK_DETECTOR"
            aliases[self._build_day_scoped_runtime_agent_id("manager_diagnosis_evaluator", day)] = "MANAGER_DIAGNOSIS_EVALUATOR"
            aliases[self._build_day_scoped_runtime_agent_id("manager_daily_planner", day)] = "MANAGER_DAILY_PLANNER"
        aliases[self._build_day_scoped_runtime_agent_id("manager_run_reflector")] = "MANAGER_RUN_REFLECTOR"
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
        if call_name == "manager_diagnosis_evaluator":
            return self.phase_runtime_agent_ids.get(
                f"{self.manager_agent_id}:manager_diagnosis_evaluator:d{day}",
                self._build_day_scoped_runtime_agent_id("manager_diagnosis_evaluator", day),
            )
        if call_name == "manager_daily_planner":
            return self.phase_runtime_agent_ids.get(
                f"{self.manager_agent_id}:manager_daily_planner:d{day}",
                self._build_day_scoped_runtime_agent_id("manager_daily_planner", day),
            )
        if call_name == "manager_run_reflector":
            return self.phase_runtime_agent_ids.get(
                f"{self.manager_agent_id}:manager_run_reflector",
                self._build_day_scoped_runtime_agent_id("manager_run_reflector"),
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
        self.run_output_root = Path(output_root)
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
        self._seed_cross_run_knowledge()
        gateway_info: dict[str, Any] = self.openclaw_client.restart_gateway()
        prepare_transport = self._openclaw_transport_for_call("prepare_runtime")
        self._openclaw_chat_fallback_ready = False
        if prepare_transport != "native_local":
            self._fail("OpenClaw native_local-only mode guard: non-native transport requested during runtime prepare.")
        merged = dict(runtime_info)
        merged["gateway"] = gateway_info
        merged["run_id"] = self.openclaw_run_id
        merged["transport"] = self.openclaw_transport
        merged["knowledge_in_path"] = str(self.series_knowledge_path.resolve()) if self.series_knowledge_path is not None else ""
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
                "Rank the bottlenecks that most limit accepted finished-product completion over the remaining horizon.",
                "Use current request facts, relevant run-local memory, and cross-run knowledge, with current facts taking priority over stale memory or stale prior guidance.",
            ],
            "manager_diagnosis_evaluator": [
                "Review detector output quality only. Do not plan tasks or assign workers.",
                "Accept only if the detector diagnosis is sufficiently grounded and planning-ready.",
            ],
            "manager_daily_planner": [
                "Build an evidence-driven day plan from current request facts, relevant run-local memory, and cross-run knowledge.",
            ],
            "manager_run_reflector": [
                "Review the completed run and extract portable lessons for the next run.",
                "Focus on what detector and planner should do differently next time, not on rewriting the whole run log.",
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
            detector_count = self.detector_max_top_bottlenecks
            example_entries = [
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
                },
                {
                    "name": "station2_output_buffer",
                    "rank": 2,
                    "severity": "medium",
                    "evidence": [
                        {"metric": "station2_output_buffer", "value": 3},
                        {"metric": "inspection_backlog", "value": 6},
                    ],
                    "why_it_limits_output": "Completed units are stacking at station2 and cannot reach inspection quickly enough.",
                },
                {
                    "name": "missing_material",
                    "rank": 3,
                    "severity": "low",
                    "evidence": [
                        {"metric": "missing_material", "value": 2},
                        {"metric": "wait_input_total", "value": 4},
                    ],
                    "why_it_limits_output": "Input starvation is limiting how quickly upstream machines can refill the downstream flow.",
                },
            ]
            request_payload["bottleneck_contract"] = {
                "top_bottlenecks_entry": {
                    "name": "str",
                    "rank": f"1..{detector_count}",
                    "severity": "low|medium|high",
                    "evidence": [{"metric": "str", "value": "number|string|bool"}],
                    "why_it_limits_output": "str",
                },
            }
            request_payload["count_rule"] = {
                "required_count": detector_count,
                "allowed_range": "1..3",
                "behavior": "Return exactly required_count bottlenecks. If evidence is thin, include weaker lower-severity constraints instead of returning fewer entries.",
            }
            request_payload["examples"] = {
                "ranked_bottleneck_example": {
                    "summary": "Inspection is the main closure bottleneck right now.",
                    "top_bottlenecks": example_entries[:detector_count],
                }
            }
        if str(phase) == "manager_diagnosis_evaluator":
            request_payload["review_contract"] = {
                "verdict": "accept|request_revision",
                "revision_requests_entry": {
                    "target_rank": f"1..{self.detector_max_top_bottlenecks}",
                    "issue_type": "str",
                    "issue": "str",
                    "requested_change": "str",
                    "evidence": [{"signal": "str", "value": "number|string|bool", "source": "throughput_closure_state|constraint_state|supporting_detail|detector_draft"}],
                },
            }
            request_payload["decision_contract"] = [
                "Review detector_draft quality only; do not propose task plans.",
                "Accept only when the detector draft is sufficiently grounded for planning.",
                "If verdict=request_revision, revision_requests must contain at least one actionable correction.",
                "If verdict=accept, revision_requests must be empty.",
            ]
            request_payload["examples"] = {
                "accept_example": {
                    "verdict": "accept",
                    "summary": "The detector draft is sufficiently grounded for planning. Rank 1 and Rank 2 are supported by the strongest current request evidence and the review is consistent with run-local memory.",
                    "revision_requests": [],
                },
                "request_revision_example": {
                    "verdict": "request_revision",
                    "summary": "The diagnosis needs revision because the second-ranked bottleneck is weaker than a visible waiting_unload constraint in the current state.",
                    "revision_requests": [
                        {
                            "target_rank": 2,
                            "issue_type": "weak_ranking",
                            "issue": "Rank 2 is assigned to a weak queue signal while waiting_unload evidence is stronger today.",
                            "requested_change": "Re-rank the stronger waiting_unload constraint above the current rank-2 bottleneck or justify the current rank order with stronger evidence.",
                            "evidence": [
                                {"signal": "machine_constraints.waiting_unload", "value": 2, "source": "constraint_state"},
                                {"signal": "station2_output_buffer", "value": 0, "source": "throughput_closure_state"},
                            ],
                        }
                    ],
                },
            }
        if str(phase) == "manager_daily_planner":
            request_payload["reason_trace_contract"] = {
                "entry": {
                    "decision": "maintain|adjust",
                    "reason": "str",
                    "evidence": [{"signal": "str", "value": "number|string|bool", "source": "execution_state|closure_signals|constraint_signals|detector_hypothesis|guardrails"}],
                    "affected_agents": ["A1|A2|A3"],
                    "task_families": ["allowed_task_priority_key"],
                    "detector_relation": "follow|reject|deprioritize",
                }
            }
            request_payload["queue_add_contract"] = {
                "shape": {
                    "A1|A2|A3": [
                        {
                            "task_family": "allowed_task_priority_key",
                            "target_type": "none|station|machine|agent|item|location",
                            "target_id": "str",
                            "target_station": "1|2|null",
                            "reason": "str",
                        }
                    ]
                },
                "rule": "When guardrails.dispatch_expectation.dispatch_opportunity_exists is true, queue_add must include at least one worker-specific work order.",
            }
            request_payload["decision_contract"] = [
                "Do not echo an empty template.",
                "Plan from the current request evidence, relevant run-local memory, and cross-run knowledge, not from inertia alone.",
                "Treat detector_hypothesis as the reviewed diagnosis packet for the current day, not as binding truth.",
                "Prefer queue_add over generic weight changes when a concrete next action is clearly justified by current evidence.",
                "Use maintain only when no materially stronger intervention is justified by current evidence and relevant run-local memory.",
                "Set detector_alignment to follow, partial_override, or override.",
                "If guardrails.dispatch_expectation.dispatch_opportunity_exists is true, queue_add must contain at least one worker-specific work order.",
            ]
        if str(phase) == "manager_run_reflector":
            request_payload["knowledge_contract"] = {
                "run_problems": ["str"],
                "detector_should_have_done": ["str"],
                "planner_should_have_done": ["str"],
                "carry_forward_lessons": ["str"],
                "detector_guidance": ["str"],
                "planner_guidance": ["str"],
                "open_watchouts": ["str"],
            }
            request_payload["decision_contract"] = [
                "Review the completed run at the run level, not day by day in isolation.",
                "Identify the main failure patterns or missed opportunities that matter for the next run.",
                "Detector guidance must explain how bottleneck ranking should improve next run.",
                "Planner guidance must explain how queue or weight decisions should improve next run.",
                "Carry_forward_lessons should stay compact and reusable in the next run.",
            ]
        if workspace is not None:
            self._openclaw_write_json(workspace / "facts" / "current_request.json", request_payload)
            self._openclaw_write_json(workspace / "facts" / "request_history" / f"{history_tag}.json", request_payload)
            self._openclaw_write_json(workspace / "facts" / "current_response_template.json", response_template)
            (workspace / "facts" / "current_phase.txt").write_text(str(phase), encoding="utf-8")
        system_prompt = "Native-local simulator turn. Use workspace facts only. Return one JSON object only."
        user_prompt = f"Execute {phase}. Fill current_response_template.json exactly."
        if str(phase) == "manager_bottleneck_detector":
            user_prompt = (
                f"Execute manager_bottleneck_detector. Return exactly {self.detector_max_top_bottlenecks} ranked bottlenecks plus one summary. "
                "Re-read KNOWLEDGE.md, MEMORY.md, and memory/rolling_summary.md in your workspace before finalizing the diagnosis. "
                "Do not output candidate_actions or reason_trace."
            )
        if str(phase) == "manager_diagnosis_evaluator":
            user_prompt = (
                "Execute manager_diagnosis_evaluator. Review detector_draft quality and return exactly one JSON object with verdict, summary, and revision_requests. "
                "Re-read KNOWLEDGE.md, MEMORY.md, and memory/rolling_summary.md in your workspace before judging diagnosis quality. "
                "Do not output a plan or any extra prose."
            )
        if str(phase) == "manager_daily_planner":
            user_prompt = (
                "Execute manager_daily_planner. Validate detector_hypothesis against current execution evidence, relevant run-local memory, and cross-run knowledge, then return an evidence-driven day plan. "
                "Re-read KNOWLEDGE.md, MEMORY.md, and memory/rolling_summary.md in your workspace before finalizing the plan. "
                "Return exactly one JSON object with plan_mode, weight_updates, queue_add, reason_trace, and detector_alignment."
            )
        if str(phase) == "manager_run_reflector":
            user_prompt = (
                "Execute manager_run_reflector. Review the completed run, compare it against prior KNOWLEDGE.md, and return exactly one JSON object with summary, run_problems, detector_should_have_done, planner_should_have_done, carry_forward_lessons, detector_guidance, planner_guidance, and open_watchouts. "
                "Re-read KNOWLEDGE.md, MEMORY.md, and memory/rolling_summary.md in your workspace before finalizing the reflection. "
                "Keep the guidance compact, actionable, and reusable in the next run."
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

    def _sanitize_review_status(self, value: Any) -> str:
        candidate = str(value or "").strip().lower()
        return candidate if candidate in {"approved", "max_revisions_exhausted"} else "approved"

    def _sanitize_evaluator_verdict(self, value: Any) -> str:
        candidate = str(value or "").strip().lower()
        return candidate if candidate in {"accept", "request_revision"} else "request_revision"

    def _sanitize_evaluator_revision_requests(self, src: Any) -> list[dict[str, Any]]:
        if not isinstance(src, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for item in src[:6]:
            if not isinstance(item, dict):
                continue
            issue = self._truncate_prompt_text(item.get("issue", item.get("problem", "")), max_len=220)
            requested_change = self._truncate_prompt_text(item.get("requested_change", item.get("revision", item.get("fix", ""))), max_len=220)
            issue_type = self._truncate_prompt_text(item.get("issue_type", item.get("category", "quality_gap")), max_len=64) or "quality_gap"
            target_rank = self._clamp_int(item.get("target_rank", item.get("rank", 1)), 1, self.detector_max_top_bottlenecks, 1)
            if not issue:
                continue
            if not requested_change:
                requested_change = "Revise this bottleneck using the strongest current-request evidence and clarify why it limits accepted finished-product completion over the remaining horizon."
            cleaned.append(
                {
                    "target_rank": target_rank,
                    "issue_type": issue_type,
                    "issue": issue,
                    "requested_change": requested_change,
                    "evidence": self._sanitize_reason_evidence(item.get("evidence")),
                }
            )
        return cleaned

    def _sanitize_evaluator_review(self, llm_obj: dict[str, Any], *, round_index: int) -> dict[str, Any]:
        verdict = self._sanitize_evaluator_verdict(llm_obj.get("verdict"))
        summary = self._truncate_prompt_text(
            llm_obj.get("summary", ""),
            max_len=260,
        ) or (
            "Detector diagnosis accepted as sufficiently grounded for planning."
            if verdict == "accept"
            else "Detector diagnosis needs revision before planning."
        )
        revision_requests = self._sanitize_evaluator_revision_requests(llm_obj.get("revision_requests"))
        if verdict == "accept":
            revision_requests = []
        elif not revision_requests:
            revision_requests = [
                {
                    "target_rank": 1,
                    "issue_type": "insufficient_revision_specificity",
                    "issue": "The evaluator rejected the detector diagnosis but did not provide enough actionable revision detail.",
                    "requested_change": "Re-rank the strongest bottlenecks using current-request evidence and make the weakest current bottleneck more specific and better grounded.",
                    "evidence": [],
                }
            ]
        return {
            "round_index": max(1, int(round_index)),
            "verdict": verdict,
            "summary": summary,
            "revision_requests": revision_requests,
        }

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


    def _detector_packet(
        self,
        observation: dict[str, Any],
        *,
        prior_detector_draft: dict[str, Any] | None = None,
        evaluator_feedback: dict[str, Any] | None = None,
        revision_index: int = 0,
    ) -> dict[str, Any]:
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

        packet = {
            "objective": {
                "global_goal": "Maximize accepted finished products within the remaining simulation horizon.",
            },
            "time_context": {
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
        if prior_detector_draft:
            packet["prior_detector_draft"] = {
                "summary": str(prior_detector_draft.get("summary", "")).strip(),
                "top_bottlenecks": self._sanitize_detector_top_bottlenecks(prior_detector_draft.get("top_bottlenecks", [])),
            }
        if evaluator_feedback:
            packet["evaluator_feedback"] = {
                "round_index": int(evaluator_feedback.get("round_index", 0) or 0),
                "summary": str(evaluator_feedback.get("summary", "")).strip(),
                "revision_requests": list(evaluator_feedback.get("revision_requests", []))[:6],
            }
        if prior_detector_draft or evaluator_feedback:
            packet["review_context"] = {
                "revision_index": max(0, int(revision_index or 0)),
                "max_revision_requests": int(self.evaluator_max_revision_requests),
            }
        return packet

    def _evaluator_packet(
        self,
        observation: dict[str, Any],
        detector_draft: dict[str, Any],
        *,
        round_index: int,
    ) -> dict[str, Any]:
        packet = self._detector_packet(observation)
        packet["detector_draft"] = {
            "summary": str(detector_draft.get("summary", "")).strip(),
            "top_bottlenecks": self._sanitize_detector_top_bottlenecks(detector_draft.get("top_bottlenecks", [])),
        }
        packet["review_context"] = {
            "round_index": max(1, int(round_index or 1)),
            "max_revision_requests": int(self.evaluator_max_revision_requests),
        }
        return packet

    def _planning_packet(self, observation: dict[str, Any], strategy: StrategyState, norms: dict[str, Any]) -> dict[str, Any]:
        planner_view = self._planner_observation_view(observation)
        time_view = planner_view.get("time", {}) if isinstance(planner_view.get("time", {}), dict) else {}
        flow = planner_view.get("flow", {}) if isinstance(planner_view.get("flow", {}), dict) else {}
        machines = planner_view.get("machines", {}) if isinstance(planner_view.get("machines", {}), dict) else {}
        wait_summary = machines.get("wait_reason_summary", {}) if isinstance(machines.get("wait_reason_summary", {}), dict) else {}
        trends = planner_view.get("trends", {}) if isinstance(planner_view.get("trends", {}), dict) else {}
        signals = self._worker_local_signals(observation)
        dispatch_opportunity_exists = self._dispatch_opportunity_exists(observation)

        detector_diagnosis = self._strategy_prompt_payload(strategy)
        detector_top_bottlenecks = self._sanitize_detector_top_bottlenecks(detector_diagnosis.get("top_bottlenecks", []))
        review_context = strategy.orchestration_context.get("diagnosis_review", {}) if isinstance(strategy.orchestration_context.get("diagnosis_review", {}), dict) else {}

        return {
            "objective": {
                "global_goal": "Maximize accepted finished-product completion over the remaining horizon.",
            },
            "time_context": {
                "day": int(time_view.get("day", observation.get("day", 0)) or 0),
                "days_remaining": int(time_view.get("days_remaining", 0) or 0),
                "horizon_remaining_min": float(time_view.get("horizon_remaining_min", 0.0) or 0.0),
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
            "detector_hypothesis": {
                "summary": str(detector_diagnosis.get("summary", "")).strip(),
                "top_bottlenecks": detector_top_bottlenecks[: self.detector_max_top_bottlenecks],
                "review_status": self._sanitize_review_status(review_context.get("review_status", "approved")),
                "review_rounds": max(0, int(review_context.get("review_rounds", 0) or 0)),
            },
            "guardrails": {
                **self._llm_guardrails_payload("plan"),
                "allowed_target_stations": [1, 2],
                "allowed_target_types": ["none", "station", "machine", "agent", "item", "location"],
                "queue_add_entry_contract": {
                    "task_family": "allowed_task_priority_key",
                    "target_type": "none|station|machine|agent|item|location",
                    "target_id": "str",
                    "target_station": "1|2|null",
                    "reason": "str",
                },
                "dispatch_expectation": {
                    "dispatch_opportunity_exists": dispatch_opportunity_exists,
                    "min_queue_add_orders_when_true": 1 if dispatch_opportunity_exists else 0,
                },
                "norm_targets": norms if isinstance(norms, dict) else {},
            },
        }

    def _aggregate_decision_source_summary(self, daily_summaries: list[dict[str, Any]]) -> dict[str, Any]:
        source_counts: dict[str, int] = {}
        manager_queue_skipped_total = 0
        for day_summary in daily_summaries:
            if not isinstance(day_summary, dict):
                continue
            manager_queue_skipped_total += int(day_summary.get("manager_queue_skipped_total", 0) or 0)
            agent_experience = day_summary.get("agent_experience", {}) if isinstance(day_summary.get("agent_experience", {}), dict) else {}
            for raw_agent in agent_experience.values():
                agent_blob = raw_agent if isinstance(raw_agent, dict) else {}
                counts = agent_blob.get("decision_source_counts", {}) if isinstance(agent_blob.get("decision_source_counts", {}), dict) else {}
                for key, value in counts.items():
                    source = str(key).strip()
                    if not source:
                        continue
                    source_counts[source] = int(source_counts.get(source, 0) or 0) + int(value or 0)
        return {
            "decision_source_counts": dict(sorted(source_counts.items(), key=lambda item: (-int(item[1]), item[0]))),
            "manager_queue_skipped_total": manager_queue_skipped_total,
        }

    def _startup_zero_output_days(self, daily_summaries: list[dict[str, Any]]) -> int:
        count = 0
        for item in daily_summaries:
            if not isinstance(item, dict):
                continue
            if int(item.get("products", 0) or 0) > 0:
                break
            count += 1
        return count

    def _compact_detector_run_trend(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.detector_run_history:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "day": int(item.get("day", 0) or 0),
                    "summary": self._truncate_prompt_text(item.get("summary", ""), max_len=140),
                    "top_bottlenecks": self._compact_bottleneck_list(item.get("top_bottlenecks", []), limit=3),
                }
            )
        return rows

    def _compact_planner_run_trend(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.planner_run_history:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "day": int(item.get("day", 0) or 0),
                    "summary": self._truncate_prompt_text(item.get("summary", ""), max_len=140),
                    **self._compact_plan_focus(item.get("task_priority_weights", {}), item.get("personal_queues", {})),
                    "detector_alignment": str(item.get("detector_alignment", "follow")).strip() or "follow",
                }
            )
        return rows

    def _compact_evaluator_run_trend(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in self.evaluator_run_history:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "day": int(item.get("day", 0) or 0),
                    "review_status": str(item.get("review_status", "")).strip(),
                    "review_rounds": int(item.get("review_rounds", 0) or 0),
                    "final_verdict": str(item.get("final_verdict", "")).strip(),
                    "summary": self._truncate_prompt_text(item.get("summary", ""), max_len=140),
                }
            )
        return rows

    def _reflector_packet(
        self,
        *,
        kpi: dict[str, Any],
        daily_summaries: list[dict[str, Any]],
        run_meta: dict[str, Any],
    ) -> dict[str, Any]:
        recurring_issues = self._compact_recurring_issue_summary(self.detector_run_history, limit=5)
        decision_sources = self._aggregate_decision_source_summary(daily_summaries)
        repair_minutes = float((kpi.get("agent_task_minutes", {}) or {}).get("REPAIR_MACHINE", 0.0) or 0.0)
        pm_minutes = float((kpi.get("agent_task_minutes", {}) or {}).get("PREVENTIVE_MAINTENANCE", 0.0) or 0.0)
        daily_outcomes: list[dict[str, Any]] = []
        for item in daily_summaries[-5:]:
            if not isinstance(item, dict):
                continue
            daily_outcomes.append(
                {
                    "day": int(item.get("day", 0) or 0),
                    "products": int(item.get("products", 0) or 0),
                    "machine_breakdowns": int(item.get("machine_breakdowns", 0) or 0),
                    "inspection_backlog_end": int(item.get("inspection_backlog_end", 0) or 0),
                    "manager_queue_skipped_total": int(item.get("manager_queue_skipped_total", 0) or 0),
                }
            )
        return {
            "run_context": {
                "run_index": int(self.run_series_index),
                "total_runs": int(self.run_series_total),
                "decision_mode": str(run_meta.get("decision_mode", "")).strip() or "llm_planner",
                "evaluator_enabled": bool(((run_meta.get("llm", {}) if isinstance(run_meta.get("llm", {}), dict) else {}).get("evaluator_enabled", self.evaluator_enabled))),
            },
            "prior_knowledge": self._truncate_prompt_text(self.series_knowledge_text or self._load_series_knowledge_text(), max_len=4500),
            "performance_summary": {
                "kpi_snapshot": {
                    "total_products": int(kpi.get("total_products", 0) or 0),
                    "downstream_closure_ratio": float(kpi.get("downstream_closure_ratio", 0.0) or 0.0),
                    "machine_broken_ratio": float(kpi.get("machine_broken_ratio", 0.0) or 0.0),
                    "machine_pm_ratio": float(kpi.get("machine_pm_ratio", 0.0) or 0.0),
                    "avg_daily_products": float(kpi.get("avg_daily_products", 0.0) or 0.0),
                },
                "daily_outcomes": daily_outcomes,
            },
            "manager_behavior_summary": {
                "detector_top_bottleneck_trend": self._compact_detector_run_trend()[-4:],
                "planner_plan_trend": self._compact_planner_run_trend()[-4:],
                "evaluator_review_summary": self._compact_evaluator_run_trend() if self.evaluator_enabled else [],
            },
            "notable_failures": {
                "startup_zero_output_days": self._startup_zero_output_days(daily_summaries),
                "recurring_issue_trend": recurring_issues,
                "repair_vs_pm": {
                    "repair_minutes": round(repair_minutes, 3),
                    "preventive_maintenance_minutes": round(pm_minutes, 3),
                },
                "manager_execution_gap": decision_sources,
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
        review_context = strategy.orchestration_context.get("diagnosis_review", {}) if isinstance(strategy.orchestration_context.get("diagnosis_review", {}), dict) else {}
        reflect_payload = {
            "day": day,
            "summary": strategy.summary,
            "top_bottlenecks": list(strategy.diagnosis.get("top_bottlenecks", [])),
            "review_status": self._sanitize_review_status(review_context.get("review_status", "approved")),
            "review_rounds": max(0, int(review_context.get("review_rounds", 0) or 0)),
        }
        reflect_memory = {
            "day": day,
            "summary": strategy.summary,
            "top_bottlenecks": list(strategy.diagnosis.get("top_bottlenecks", [])),
            "review_status": reflect_payload["review_status"],
            "review_rounds": reflect_payload["review_rounds"],
        }
        self.detector_run_history.append(dict(reflect_payload))
        recurrence_summary = self._machine_recurrence_summary(observation)
        latest_entry = {
            "day": day,
            "summary": strategy.summary,
            "top_bottlenecks": self._compact_bottleneck_list(strategy.diagnosis.get("top_bottlenecks", []), limit=self.detector_max_top_bottlenecks),
            "review_status": reflect_payload["review_status"],
            "review_rounds": reflect_payload["review_rounds"],
        }
        self._append_prompt_history(self.detector_prompt_history, latest_entry, limit=3)
        prompt_memory, commitment_payload, recurrence_memory = self._detector_prompt_memory_payload(latest_entry, recurrence_summary)
        manager_workspace = self._phase_workspace_for_call("manager_bottleneck_detector", {"phase": "manager_bottleneck_detector", "day": day})
        if manager_workspace is None:
            return
        observation_view = self._planner_observation_view(observation)
        self._openclaw_write_json(manager_workspace / "facts" / "current_reflect.json", reflect_payload)
        self._openclaw_write_json(manager_workspace / "facts" / "reflect_history" / f"day_{day:02d}.json", reflect_payload)
        self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_reflect.json", reflect_payload)
        self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", prompt_memory)
        self._openclaw_write_json(manager_workspace / "beliefs" / "history" / f"day_{day:02d}_reflect.json", reflect_memory)
        self._openclaw_write_json(manager_workspace / "commitments" / "current_commitment.json", commitment_payload)
        self._openclaw_write_json(manager_workspace / "commitments" / "history" / f"day_{day:02d}_reflect.json", commitment_payload)
        self._openclaw_write_json(manager_workspace / "memory" / "episodic" / f"day_{day:02d}_reflect.json", {"reflection": reflect_payload, "observation": observation_view})
        self._openclaw_write_markdown(manager_workspace / "memory" / "daily" / f"day_{day:02d}_reflect.md", f"{self.manager_agent_id} Day {day} Reflect", [("Observation Snapshot", observation_view), ("Bottleneck Diagnosis", reflect_payload), ("Reflect Memory", reflect_memory)])
        self._openclaw_write_markdown(
            manager_workspace / "memory" / "rolling_summary.md",
            f"{self.manager_agent_id} Rolling Summary",
            [
                ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                ("Latest Diagnosis", latest_entry),
                ("Recent Diagnosis Trend", prompt_memory.get("recent_diagnosis_trend", [])),
                ("Recurring Issue Summary", prompt_memory.get("recurring_issue_summary", [])),
                ("Recurring Machine Breakdown Summary", recurrence_memory),
                ("Persistent Watchouts", prompt_memory.get("persistent_watchouts", [])),
            ],
        )
        self._openclaw_write_markdown(
            manager_workspace / "MEMORY.md",
            f"{self.manager_agent_id} Memory",
            [
                ("Run Scope", "This workspace memory is scoped to the current run only and is rebuilt at the next run start."),
                ("Compressed Prompt Memory", prompt_memory),
                ("Current Commitment", commitment_payload),
                ("Raw History Files", {"daily": f"memory/daily/day_{day:02d}_reflect.md", "episodic": f"memory/episodic/day_{day:02d}_reflect.json", "report": f"reports/day_{day:02d}_reflect.json"}),
            ],
        )

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
            "detector_alignment": str(getattr(job_plan, "detector_alignment", "follow")),
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
            self.planner_run_history.append(dict(plan_payload))
            latest_entry = {
                "day": day,
                "summary": job_plan.manager_summary,
                "task_priority_weights": dict(job_plan.task_priority_weights),
                "personal_queues": dict(job_plan.personal_queues),
                "detector_alignment": str(getattr(job_plan, "detector_alignment", "follow")),
            }
            self._append_prompt_history(self.planner_prompt_history, latest_entry, limit=3)
            prompt_memory, commitment_payload = self._planner_prompt_memory_payload(latest_entry)
            self._openclaw_write_json(manager_workspace / "facts" / "current_plan.json", plan_payload)
            self._openclaw_write_json(manager_workspace / "facts" / "plan_history" / f"day_{day:02d}.json", plan_payload)
            self._openclaw_write_json(manager_workspace / "plans" / f"day_{day:02d}_plan.json", plan_payload)
            self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_plan.json", plan_payload)
            self._openclaw_write_json(manager_workspace / "trace" / f"day_{day:02d}_reason_trace.json", job_plan.reason_trace)
            self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", prompt_memory)
            self._openclaw_write_json(manager_workspace / "beliefs" / "history" / f"day_{day:02d}_plan.json", plan_memory)
            self._openclaw_write_json(manager_workspace / "commitments" / "current_commitment.json", commitment_payload)
            self._openclaw_write_json(manager_workspace / "commitments" / "history" / f"day_{day:02d}_plan.json", commitment_payload)
            self._openclaw_write_json(manager_workspace / "memory" / "episodic" / f"day_{day:02d}_plan.json", {"plan": plan_payload})
            self._openclaw_write_markdown(manager_workspace / "memory" / "daily" / f"day_{day:02d}_plan.md", f"{self.manager_agent_id} Day {day} Plan", [("Plan Payload", plan_payload), ("Planner Memory", plan_memory)])
            self._openclaw_write_markdown(
                manager_workspace / "memory" / "rolling_summary.md",
                f"{self.manager_agent_id} Rolling Summary",
                [
                    ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                    ("Latest Plan Focus", prompt_memory.get("latest_plan_focus", {})),
                    ("Recent Plan Trend", prompt_memory.get("recent_plan_trend", [])),
                    ("Watchouts", prompt_memory.get("watchouts", [])),
                ],
            )
            self._openclaw_write_markdown(
                manager_workspace / "MEMORY.md",
                f"{self.manager_agent_id} Memory",
                [
                    ("Run Scope", "This workspace memory is scoped to the current run only and is rebuilt at the next run start."),
                    ("Compressed Prompt Memory", prompt_memory),
                    ("Current Commitment", commitment_payload),
                    ("Raw History Files", {"daily": f"memory/daily/day_{day:02d}_plan.md", "episodic": f"memory/episodic/day_{day:02d}_plan.json", "report": f"reports/day_{day:02d}_plan.json", "trace": f"trace/day_{day:02d}_reason_trace.json"}),
                ],
            )

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
            self._openclaw_write_markdown(workspace / "memory" / "daily" / f"day_{day:02d}.md", f"{aid} Day {day} Review", [("Day Summary", compact_day), ("Worker Report", report), ("Beliefs", beliefs), ("Commitment", commitment), ("Semantic Memory", semantic_memory)])
            self._openclaw_write_markdown(
                workspace / "memory" / "rolling_summary.md",
                f"{aid} Rolling Summary",
                [
                    ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                    ("Latest Beliefs", beliefs),
                    ("Current Commitment", commitment),
                    ("Semantic Memory", semantic_memory),
                ],
            )
            self._openclaw_write_markdown(
                workspace / "MEMORY.md",
                f"{aid} Memory",
                [
                    ("Run Scope", "This workspace memory is scoped to the current run only."),
                    ("Compressed Prompt Memory", {"beliefs": beliefs, "commitment": commitment, "semantic_memory": semantic_memory}),
                    ("Raw History Files", {"daily": f"memory/daily/day_{day:02d}.md", "episodic": f"memory/episodic/day_{day:02d}.json", "report": f"reports/day_{day:02d}_report.json"}),
                ],
            )
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
            prompt_beliefs = {
                "review_summary": str(review.get("summary", "")).strip(),
                "watchouts": list(review.get("watchouts", []))[:5],
                "priority_focus": self._compact_plan_focus(review.get("task_priority_weights", {}), review.get("personal_queues", {})),
                "detector_alignment": str(review.get("detector_alignment", "follow")).strip() or "follow",
            }
            self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_worker_reports.json", worker_reports)
            self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_review.json", review_payload)
            self._openclaw_write_json(manager_workspace / "facts" / "current_review.json", review_payload)
            self._openclaw_write_json(manager_workspace / "facts" / "review_history" / f"day_{day:02d}.json", review_payload)
            self._openclaw_write_json(manager_workspace / "plans" / f"day_{day:02d}_review.json", review_payload)
            self._openclaw_write_json(manager_workspace / "trace" / f"day_{day:02d}_review_reason_trace.json", review.get("reason_trace", []))
            self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", prompt_beliefs)
            self._openclaw_write_json(manager_workspace / "beliefs" / "history" / f"day_{day:02d}_review.json", review_memory)
            self._openclaw_write_json(
                manager_workspace / "commitments" / "current_commitment.json",
                {
                    "summary": str(review.get("summary", "")).strip(),
                    "focus_tasks": [str(key) for key, value in sorted((review.get("task_priority_weights", {}) or {}).items(), key=lambda item: float(item[1] or 0.0), reverse=True)[:3] if float(value or 0.0) > 0.0],
                    "watchouts": list(review.get("watchouts", []))[:5],
                },
            )
            self._openclaw_write_json(
                manager_workspace / "commitments" / "history" / f"day_{day:02d}_review.json",
                {
                    "summary": str(review.get("summary", "")).strip(),
                    "watchouts": list(review.get("watchouts", []))[:5],
                },
            )
            self._openclaw_write_json(manager_workspace / "memory" / "episodic" / f"day_{day:02d}_review.json", {"review": review_payload, "worker_reports": worker_reports})
            self._openclaw_write_markdown(manager_workspace / "memory" / "daily" / f"day_{day:02d}_review.md", f"{self.manager_agent_id} Day {day} Review", [("Review Payload", review_payload), ("Worker Reports", worker_reports), ("Review Memory", review_memory)])
            self._openclaw_write_markdown(
                manager_workspace / "memory" / "rolling_summary.md",
                f"{self.manager_agent_id} Rolling Summary",
                [
                    ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                    ("Latest Review", {"day": day, "summary": review.get("summary", ""), "watchouts": list(review.get("watchouts", []))[:5], "detector_alignment": review.get("detector_alignment", "follow")}),
                    ("Priority Focus", self._compact_plan_focus(review.get("task_priority_weights", {}), review.get("personal_queues", {}))),
                ],
            )
            self._openclaw_write_markdown(
                manager_workspace / "MEMORY.md",
                f"{self.manager_agent_id} Memory",
                [
                    ("Run Scope", "This workspace memory is scoped to the current run only and is rebuilt at the next run start."),
                    ("Compressed Prompt Memory", {"review_summary": str(review.get("summary", "")).strip(), "watchouts": list(review.get("watchouts", []))[:5], "priority_focus": self._compact_plan_focus(review.get("task_priority_weights", {}), review.get("personal_queues", {}))}),
                    ("Raw History Files", {"daily": f"memory/daily/day_{day:02d}_review.md", "episodic": f"memory/episodic/day_{day:02d}_review.json", "report": f"reports/day_{day:02d}_review.json"}),
                ],
            )

    def _synthesize_detector_summary(self, top_bottlenecks: list[dict[str, Any]]) -> str:
        if not top_bottlenecks:
            return "No high-confidence bottleneck was identified from the current request facts."
        primary = top_bottlenecks[0] if isinstance(top_bottlenecks[0], dict) else {}
        name = str(primary.get("name", "primary_bottleneck")).strip() or "primary_bottleneck"
        why = str(primary.get("why_it_limits_output", "")).strip()
        if why:
            return self._truncate_prompt_text(f"The primary bottleneck is {name}. {why}", max_len=320)
        return self._truncate_prompt_text(f"The primary bottleneck is {name}.", max_len=320)

    def _normalize_detector_draft_payload(self, llm_obj: dict[str, Any]) -> dict[str, Any]:
        top_bottlenecks = self._sanitize_detector_top_bottlenecks(llm_obj.get("top_bottlenecks"))
        summary = self._truncate_prompt_text(llm_obj.get("summary", ""), max_len=320) or self._synthesize_detector_summary(top_bottlenecks)
        return {
            "summary": summary,
            "top_bottlenecks": top_bottlenecks,
        }

    def _call_detector_draft(
        self,
        observation: dict[str, Any],
        *,
        prior_detector_draft: dict[str, Any] | None = None,
        evaluator_feedback: dict[str, Any] | None = None,
        revision_index: int = 0,
    ) -> dict[str, Any]:
        runtime_agent_id, system_prompt, prompt, required_keys = self._detector_turn_bundle(
            observation,
            retry=False,
            prior_detector_draft=prior_detector_draft,
            evaluator_feedback=evaluator_feedback,
            revision_index=revision_index,
        )
        self._assert_native_workspace_inputs_ready(runtime_agent_id, "manager_bottleneck_detector")
        llm_obj = self._call_llm_json(
            prompt,
            system_prompt,
            call_name="manager_bottleneck_detector",
            context={"phase": "manager_bottleneck_detector", "day": observation.get("day")},
            required_keys=required_keys,
        )
        payload = self._normalize_detector_draft_payload(llm_obj)
        payload["revision_index"] = max(0, int(revision_index or 0))
        return payload

    def _call_evaluator_review(
        self,
        observation: dict[str, Any],
        detector_draft: dict[str, Any],
        *,
        round_index: int,
    ) -> dict[str, Any]:
        runtime_agent_id, system_prompt, prompt, required_keys = self._evaluator_turn_bundle(
            observation,
            detector_draft,
            round_index=round_index,
        )
        self._assert_native_workspace_inputs_ready(runtime_agent_id, "manager_diagnosis_evaluator")
        llm_obj = self._call_llm_json(
            prompt,
            system_prompt,
            call_name="manager_diagnosis_evaluator",
            context={"phase": "manager_diagnosis_evaluator", "day": observation.get("day")},
            required_keys=required_keys,
        )
        return self._sanitize_evaluator_review(llm_obj, round_index=round_index)

    def _sanitize_reflector_output(self, llm_obj: dict[str, Any]) -> dict[str, Any]:
        def _string_list(value: Any, *, limit: int = 3, max_len: int = 160) -> list[str]:
            rows: list[str] = []
            if isinstance(value, list):
                items = value
            else:
                items = []
            for raw in items[: max(1, int(limit or 1))]:
                text = self._truncate_prompt_text(raw, max_len=max_len)
                if text:
                    rows.append(text)
            return rows

        run_problems: list[dict[str, Any]] = []
        raw_problems = llm_obj.get("run_problems", []) if isinstance(llm_obj.get("run_problems", []), list) else []
        for raw in raw_problems[:3]:
            if isinstance(raw, dict):
                issue = self._truncate_prompt_text(raw.get("issue", raw.get("problem", "")), max_len=150)
                impact = self._truncate_prompt_text(raw.get("impact", ""), max_len=150)
                if issue:
                    row = {"issue": issue}
                    if impact:
                        row["impact"] = impact
                    run_problems.append(row)
            else:
                issue = self._truncate_prompt_text(raw, max_len=150)
                if issue:
                    run_problems.append({"issue": issue})
        return {
            "summary": str(llm_obj.get("summary", "")).strip(),
            "run_problems": run_problems,
            "detector_should_have_done": _string_list(llm_obj.get("detector_should_have_done", [])),
            "planner_should_have_done": _string_list(llm_obj.get("planner_should_have_done", [])),
            "carry_forward_lessons": _string_list(llm_obj.get("carry_forward_lessons", []), limit=3),
            "detector_guidance": _string_list(llm_obj.get("detector_guidance", []), limit=3),
            "planner_guidance": _string_list(llm_obj.get("planner_guidance", []), limit=3),
            "open_watchouts": _string_list(llm_obj.get("open_watchouts", []), limit=3),
        }

    def _call_run_reflector(self, run_packet: dict[str, Any]) -> dict[str, Any]:
        runtime_agent_id, system_prompt, prompt, required_keys = self._reflector_turn_bundle(run_packet)
        self._assert_native_workspace_inputs_ready(runtime_agent_id, "manager_run_reflector")
        llm_obj = self._call_llm_json(
            prompt,
            system_prompt,
            call_name="manager_run_reflector",
            context={"phase": "manager_run_reflector", "run_index": self.run_series_index},
            required_keys=required_keys,
        )
        return self._sanitize_reflector_output(llm_obj)

    def _run_reviewed_detector_cycle(self, observation: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        detector_drafts: list[dict[str, Any]] = []
        evaluator_reviews: list[dict[str, Any]] = []
        detector_draft = self._call_detector_draft(observation, revision_index=0)
        detector_drafts.append(dict(detector_draft))

        if not self.evaluator_enabled:
            meta = {
                "enabled": False,
                "review_status": "approved",
                "review_rounds": 0,
                "max_revision_requests": int(self.evaluator_max_revision_requests),
                "final_verdict": "accept",
                "detector_drafts": detector_drafts,
                "evaluator_reviews": evaluator_reviews,
            }
            return detector_draft, meta

        final_status = "max_revisions_exhausted"
        final_verdict = "request_revision"
        max_evaluator_rounds = max(1, int(self.evaluator_max_revision_requests) + 1)
        for round_index in range(1, max_evaluator_rounds + 1):
            evaluator_review = self._call_evaluator_review(observation, detector_draft, round_index=round_index)
            evaluator_reviews.append(dict(evaluator_review))
            final_verdict = str(evaluator_review.get("verdict", "request_revision"))
            if final_verdict == "accept":
                final_status = "approved"
                break
            if round_index > int(self.evaluator_max_revision_requests):
                final_status = "max_revisions_exhausted"
                break
            detector_draft = self._call_detector_draft(
                observation,
                prior_detector_draft=detector_draft,
                evaluator_feedback=evaluator_review,
                revision_index=round_index,
            )
            detector_drafts.append(dict(detector_draft))

        meta = {
            "enabled": True,
            "review_status": self._sanitize_review_status(final_status),
            "review_rounds": len(evaluator_reviews),
            "max_revision_requests": int(self.evaluator_max_revision_requests),
            "final_verdict": final_verdict,
            "detector_drafts": detector_drafts,
            "evaluator_reviews": evaluator_reviews,
        }
        return detector_draft, meta

    def _machine_recurrence_summary(self, observation: dict[str, Any]) -> dict[str, Any]:
        day = int((observation.get("time", {}) or {}).get("day", observation.get("day", 0)) or 0)
        machines = observation.get("machines", {}) if isinstance(observation.get("machines", {}), dict) else {}
        by_id = machines.get("by_id", {}) if isinstance(machines.get("by_id", {}), dict) else {}
        for machine_id, raw in by_id.items():
            data = raw if isinstance(raw, dict) else {}
            item = self.machine_recurrence_history.setdefault(
                str(machine_id),
                {
                    "station": data.get("station"),
                    "days_observed": 0,
                    "broken_day_count": 0,
                    "consecutive_broken_days": 0,
                    "last_observed_day": 0,
                    "last_broken_day": None,
                    "current_broken": False,
                    "current_state": "",
                    "latest_minutes_since_last_pm": None,
                    "latest_minutes_since_failure_started": None,
                    "latest_wait_reasons": [],
                    "owners": {},
                },
            )
            if int(item.get("last_observed_day", 0) or 0) == day:
                continue
            broken = bool(data.get("broken", False))
            item["station"] = data.get("station")
            item["days_observed"] = int(item.get("days_observed", 0) or 0) + 1
            item["current_broken"] = broken
            item["current_state"] = str(data.get("state", "")).strip()
            item["latest_minutes_since_last_pm"] = data.get("minutes_since_last_pm")
            item["latest_minutes_since_failure_started"] = data.get("minutes_since_failure_started")
            item["latest_wait_reasons"] = list(data.get("wait_reasons", [])) if isinstance(data.get("wait_reasons", []), list) else []
            item["owners"] = dict(data.get("owners", {})) if isinstance(data.get("owners", {}), dict) else {}
            item["last_observed_day"] = day
            if broken:
                item["broken_day_count"] = int(item.get("broken_day_count", 0) or 0) + 1
                item["consecutive_broken_days"] = int(item.get("consecutive_broken_days", 0) or 0) + 1
                item["last_broken_day"] = day
            else:
                item["consecutive_broken_days"] = 0

        rows: list[dict[str, Any]] = []
        for machine_id, item in self.machine_recurrence_history.items():
            broken_day_count = int(item.get("broken_day_count", 0) or 0)
            current_broken = bool(item.get("current_broken", False))
            if broken_day_count <= 0 and not current_broken:
                continue
            rows.append(
                {
                    "machine_id": machine_id,
                    "station": item.get("station"),
                    "days_observed": int(item.get("days_observed", 0) or 0),
                    "broken_day_count": broken_day_count,
                    "consecutive_broken_days": int(item.get("consecutive_broken_days", 0) or 0),
                    "last_broken_day": item.get("last_broken_day"),
                    "current_broken": current_broken,
                    "current_state": item.get("current_state"),
                    "latest_minutes_since_last_pm": item.get("latest_minutes_since_last_pm"),
                    "latest_minutes_since_failure_started": item.get("latest_minutes_since_failure_started"),
                    "latest_wait_reasons": list(item.get("latest_wait_reasons", []))[:4],
                }
            )
        rows.sort(
            key=lambda item: (
                0 if bool(item.get("current_broken", False)) else 1,
                -int(item.get("broken_day_count", 0) or 0),
                -int(item.get("consecutive_broken_days", 0) or 0),
                -(float(item.get("latest_minutes_since_last_pm", 0.0) or 0.0)),
                str(item.get("machine_id", "")),
            )
        )
        repeat_breakdowns = [row for row in rows if int(row.get("broken_day_count", 0) or 0) >= 2]
        return {
            "day": day,
            "machines_with_any_broken_history": len(rows),
            "machines_with_repeat_breakdowns": len(repeat_breakdowns),
            "currently_broken_machines": [row.get("machine_id") for row in rows if bool(row.get("current_broken", False))][:6],
            "top_recurrence_machines": rows[:6],
        }

    def _sync_orchestration_evaluator_workspace(
        self,
        *,
        observation: dict[str, Any],
        diagnosis_payload: dict[str, Any],
        diagnosis_review: dict[str, Any],
    ) -> None:
        if not self._openclaw_enabled():
            return
        day = int((observation.get("time", {}) or {}).get("day", observation.get("day", 0)) or 0)
        manager_workspace = self._phase_workspace_for_call("manager_diagnosis_evaluator", {"phase": "manager_diagnosis_evaluator", "day": day})
        if manager_workspace is None:
            return
        recurrence_summary = self._machine_recurrence_summary(observation)
        review_payload = {
            "day": day,
            "enabled": bool(diagnosis_review.get("enabled", False)),
            "review_status": self._sanitize_review_status(diagnosis_review.get("review_status", "approved")),
            "review_rounds": max(0, int(diagnosis_review.get("review_rounds", 0) or 0)),
            "max_revision_requests": int(diagnosis_review.get("max_revision_requests", self.evaluator_max_revision_requests) or self.evaluator_max_revision_requests),
            "final_verdict": str(diagnosis_review.get("final_verdict", "accept")).strip() or "accept",
            "summary": str(diagnosis_payload.get("summary", "")).strip(),
            "top_bottlenecks": list(diagnosis_payload.get("top_bottlenecks", [])),
            "recurring_issue_summary": self._compact_recurring_issue_summary(self.detector_prompt_history[-3:], limit=5),
            "machine_recurrence_summary": recurrence_summary,
            "evaluator_reviews": list(diagnosis_review.get("evaluator_reviews", [])),
        }
        review_memory = dict(review_payload)
        self.evaluator_run_history.append(dict(review_payload))
        latest_entry = {
            "day": day,
            "review_status": review_payload["review_status"],
            "final_verdict": review_payload["final_verdict"],
            "review_rounds": review_payload["review_rounds"],
            "summary": review_payload["summary"],
        }
        self._append_prompt_history(self.evaluator_prompt_history, latest_entry, limit=3)
        prompt_memory, commitment_payload, recurrence_memory = self._evaluator_prompt_memory_payload(latest_entry, recurrence_summary, diagnosis_review)
        observation_view = self._planner_observation_view(observation)
        self._openclaw_write_json(manager_workspace / "facts" / "current_evaluation.json", review_payload)
        self._openclaw_write_json(manager_workspace / "facts" / "evaluation_history" / f"day_{day:02d}.json", review_payload)
        self._openclaw_write_json(manager_workspace / "reports" / f"day_{day:02d}_evaluation.json", review_payload)
        self._openclaw_write_json(manager_workspace / "trace" / f"day_{day:02d}_detector_drafts.json", diagnosis_review.get("detector_drafts", []))
        self._openclaw_write_json(manager_workspace / "trace" / f"day_{day:02d}_evaluator_reviews.json", diagnosis_review.get("evaluator_reviews", []))
        self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", prompt_memory)
        self._openclaw_write_json(manager_workspace / "beliefs" / "history" / f"day_{day:02d}_evaluation.json", review_memory)
        self._openclaw_write_json(manager_workspace / "commitments" / "current_commitment.json", commitment_payload)
        self._openclaw_write_json(manager_workspace / "commitments" / "history" / f"day_{day:02d}_evaluation.json", commitment_payload)
        self._openclaw_write_json(manager_workspace / "memory" / "episodic" / f"day_{day:02d}_evaluation.json", {"evaluation": review_payload, "observation": observation_view})
        self._openclaw_write_markdown(manager_workspace / "memory" / "daily" / f"day_{day:02d}_evaluation.md", f"{self.manager_agent_id} Day {day} Evaluation", [("Observation Snapshot", observation_view), ("Final Reviewed Diagnosis", review_payload), ("Recurring Issue Summary", review_payload.get("recurring_issue_summary", [])), ("Machine Recurrence Summary", recurrence_summary), ("Detector Drafts", diagnosis_review.get("detector_drafts", [])), ("Evaluator Reviews", diagnosis_review.get("evaluator_reviews", []))])
        self._openclaw_write_markdown(
            manager_workspace / "memory" / "rolling_summary.md",
            f"{self.manager_agent_id} Rolling Summary",
            [
                ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                ("Latest Review", prompt_memory.get("latest_review", {})),
                ("Recent Review Rounds", prompt_memory.get("recent_review_rounds", [])),
                ("Recurring Issue Summary", prompt_memory.get("recurring_issue_summary", [])),
                ("Recurring Machine Summary", recurrence_memory),
                ("Unresolved Diagnosis Watchouts", prompt_memory.get("unresolved_diagnosis_watchouts", [])),
            ],
        )
        self._openclaw_write_markdown(
            manager_workspace / "MEMORY.md",
            f"{self.manager_agent_id} Memory",
            [
                ("Run Scope", "This workspace memory is scoped to the current run only and is rebuilt at the next run start."),
                ("Compressed Prompt Memory", prompt_memory),
                ("Current Commitment", commitment_payload),
                ("Raw History Files", {"daily": f"memory/daily/day_{day:02d}_evaluation.md", "episodic": f"memory/episodic/day_{day:02d}_evaluation.json", "report": f"reports/day_{day:02d}_evaluation.json", "trace_reviews": f"trace/day_{day:02d}_evaluator_reviews.json"}),
            ],
        )

    def _render_reflector_archive_markdown(self, reflection_payload: dict[str, Any]) -> str:
        return self._openclaw_render_markdown(
            f"{self.manager_agent_id} Run Reflection",
            [
                ("Run Index", {"run_index": int(self.run_series_index), "total_runs": int(self.run_series_total)}),
                ("Summary", str(reflection_payload.get("summary", "")).strip()),
                ("Run Problems", list(reflection_payload.get("run_problems", []))),
                ("Detector Should Have Done", list(reflection_payload.get("detector_should_have_done", []))),
                ("Planner Should Have Done", list(reflection_payload.get("planner_should_have_done", []))),
                ("Carry-Forward Lessons", list(reflection_payload.get("carry_forward_lessons", []))),
                ("Detector Guidance", list(reflection_payload.get("detector_guidance", []))),
                ("Planner Guidance", list(reflection_payload.get("planner_guidance", []))),
                ("Open Watchouts", list(reflection_payload.get("open_watchouts", []))),
            ],
        )

    def _render_series_knowledge_markdown(self, reflection_payload: dict[str, Any]) -> str:
        prior_sections = self._parse_series_knowledge_sections(self.series_knowledge_text or self._load_series_knowledge_text())
        latest_lessons = list(reflection_payload.get("carry_forward_lessons", []))[:3]
        persistent_lessons = self._merge_deduped_strings(
            latest_lessons,
            prior_sections.get("persistent_lessons", []),
            limit=5,
        )
        scope_text = (
            f"Knowledge accumulated for serial run {int(self.run_series_index)}/{int(self.run_series_total)}. "
            "Use persistent lessons as long-lived prior knowledge and latest lessons as the newest run-specific additions. "
            "Let clearly stronger current facts override both."
        )
        return self._openclaw_render_markdown(
            "Run-Series Knowledge",
            [
                ("Run-Series Scope", scope_text),
                ("Persistent Lessons", persistent_lessons),
                ("Latest Lessons", latest_lessons),
                ("Detector Guidance", list(reflection_payload.get("detector_guidance", []))[:3]),
                ("Planner Guidance", list(reflection_payload.get("planner_guidance", []))[:3]),
                ("Open Watchouts", list(reflection_payload.get("open_watchouts", []))[:3]),
            ],
        )

    def _sync_orchestration_reflector_workspace(
        self,
        *,
        run_packet: dict[str, Any],
        reflection_payload: dict[str, Any],
        knowledge_markdown: str,
    ) -> None:
        if not self._openclaw_enabled():
            return
        manager_workspace = self._phase_workspace_for_call("manager_run_reflector", {"phase": "manager_run_reflector"})
        if manager_workspace is None:
            return
        latest_entry = {
            "run_index": int(self.run_series_index),
            "summary": str(reflection_payload.get("summary", "")).strip(),
            "carry_forward_lessons": list(reflection_payload.get("carry_forward_lessons", []))[:5],
        }
        self.reflector_run_history.append(dict(latest_entry))
        prompt_memory, commitment_payload = self._reflector_prompt_memory_payload(latest_entry)
        self._openclaw_write_json(manager_workspace / "facts" / "current_reflection.json", reflection_payload)
        self._openclaw_write_json(manager_workspace / "reports" / f"run_{int(self.run_series_index):02d}_reflection.json", reflection_payload)
        self._openclaw_write_json(manager_workspace / "beliefs" / "current_beliefs.json", prompt_memory)
        self._openclaw_write_json(manager_workspace / "commitments" / "current_commitment.json", commitment_payload)
        self._openclaw_write_json(manager_workspace / "memory" / "episodic" / f"run_{int(self.run_series_index):02d}_reflection.json", {"reflection": reflection_payload, "packet": run_packet})
        self._openclaw_write_markdown(
            manager_workspace / "memory" / "daily" / f"run_{int(self.run_series_index):02d}_reflection.md",
            f"{self.manager_agent_id} Run {int(self.run_series_index)} Reflection",
            [
                ("Reflector Input", run_packet),
                ("Reflector Output", reflection_payload),
            ],
        )
        self._openclaw_write_markdown(
            manager_workspace / "memory" / "rolling_summary.md",
            f"{self.manager_agent_id} Rolling Summary",
            [
                ("Run Scope", "This file stores compact prompt-facing memory for the current simulation run only."),
                ("Latest Reflection", prompt_memory.get("latest_reflection", {})),
                ("Recent Reflections", prompt_memory.get("recent_reflections", [])),
            ],
        )
        self._openclaw_write_markdown(
            manager_workspace / "MEMORY.md",
            f"{self.manager_agent_id} Memory",
            [
                ("Run Scope", "This workspace memory is scoped to the current run only and is rebuilt at the next run start."),
                ("Compressed Prompt Memory", prompt_memory),
                ("Current Commitment", commitment_payload),
                ("Raw History Files", {"daily": f"memory/daily/run_{int(self.run_series_index):02d}_reflection.md", "episodic": f"memory/episodic/run_{int(self.run_series_index):02d}_reflection.json", "report": f"reports/run_{int(self.run_series_index):02d}_reflection.json"}),
            ],
        )
        (manager_workspace / "KNOWLEDGE.md").write_text(knowledge_markdown + ("\n" if not knowledge_markdown.endswith("\n") else ""), encoding="utf-8")

    def reflect_run(
        self,
        *,
        output_root: Path,
        kpi: dict[str, Any],
        daily_summaries: list[dict[str, Any]],
        run_meta: dict[str, Any],
    ) -> dict[str, Any]:
        run_packet = self._reflector_packet(kpi=kpi, daily_summaries=daily_summaries, run_meta=run_meta)
        reflection_payload = self._call_run_reflector(run_packet)
        knowledge_path = self.series_knowledge_path or (output_root / "knowledge.md")
        knowledge_history_dir = self.series_knowledge_history_dir or (knowledge_path.parent / "knowledge_history")
        knowledge_history_dir.mkdir(parents=True, exist_ok=True)
        archive_markdown = self._render_reflector_archive_markdown(reflection_payload)
        knowledge_markdown = self._render_series_knowledge_markdown(reflection_payload)
        archive_path = knowledge_history_dir / f"run_{int(self.run_series_index):02d}_reflection.md"
        archive_path.write_text(archive_markdown + ("\n" if not archive_markdown.endswith("\n") else ""), encoding="utf-8")
        knowledge_path.parent.mkdir(parents=True, exist_ok=True)
        knowledge_path.write_text(knowledge_markdown + ("\n" if not knowledge_markdown.endswith("\n") else ""), encoding="utf-8")
        self.series_knowledge_text = knowledge_markdown
        child_json_path = output_root / "run_reflection.json"
        child_md_path = output_root / "run_reflection.md"
        child_json_path.write_text(json.dumps(reflection_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        child_md_path.write_text(archive_markdown + ("\n" if not archive_markdown.endswith("\n") else ""), encoding="utf-8")
        self._sync_orchestration_reflector_workspace(run_packet=run_packet, reflection_payload=reflection_payload, knowledge_markdown=knowledge_markdown)
        return {
            "run_reflection": reflection_payload,
            "run_reflection_path": str(child_json_path.resolve()),
            "run_reflection_markdown_path": str(child_md_path.resolve()),
            "knowledge_in_path": str(knowledge_path.resolve()),
            "knowledge_out_path": str(knowledge_path.resolve()),
            "knowledge_archive_path": str(archive_path.resolve()),
        }

    # Day start: rank the constraints that most limit accepted finished-product
    # completion over the remaining horizon, using current facts plus run-local memory.
    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        diagnosis_payload, diagnosis_review = self._run_reviewed_detector_cycle(observation)
        diagnosis = {
            "top_bottlenecks": list(diagnosis_payload.get("top_bottlenecks", [])),
        }
        summary = str(diagnosis_payload.get("summary", "")).strip() or self._synthesize_detector_summary(diagnosis["top_bottlenecks"])
        strategy = StrategyState(
            notes=self._flatten_diagnosis_to_notes(summary, diagnosis),
            summary=summary,
            diagnosis=diagnosis,
            orchestration_context={"diagnosis_review": diagnosis_review},
        )
        self.last_diagnosis_review = dict(diagnosis_review)
        self._sync_orchestration_reflection_workspace(observation=observation, strategy=strategy)
        self._sync_orchestration_evaluator_workspace(
            observation=observation,
            diagnosis_payload={"summary": summary, "top_bottlenecks": diagnosis["top_bottlenecks"]},
            diagnosis_review=diagnosis_review,
        )
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
            role_summary="You are MANAGER_DAILY_PLANNER, an independent operating planner whose local objective is to convert the reviewed diagnosis plus current execution evidence into the highest-leverage executable day plan for accepted finished-product completion.",
            input_payload=self._planning_packet(observation, strategy, norms),
            required_fields={
                "plan_mode": "str",
                "weight_updates": "dict[str, float]",
                "queue_add": "dict[str, list]",
                "reason_trace": "list[dict]",
                "detector_alignment": "str",
            },
            instructions=[
                "Plan from current execution evidence. Do not preserve yesterday's focus unless current evidence and relevant run-local memory still support it.",
                "Treat detector_hypothesis as the reviewed diagnosis packet for the current day. You may still follow, partially override, or override it when current execution evidence is stronger.",
                "Prefer worker-specific queue_add over generic weight changes when current evidence already supports a concrete next action.",
                "Use maintain only when current evidence and relevant run-local memory show no materially stronger intervention than the active plan.",
                "Choose the intervention that most improves accepted finished-product completion over the remaining horizon.",
                "Use only task_family names from guardrails.allowed_task_priority_keys in weight_updates, queue_add, and reason_trace.",
                "If detector_hypothesis conflicts with stronger closure_signals or constraint_signals, you may reject or deprioritize it in reason_trace.",
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









































