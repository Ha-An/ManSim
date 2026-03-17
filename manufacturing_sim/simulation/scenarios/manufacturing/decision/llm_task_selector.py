from __future__ import annotations

from typing import Any

from .llm_optional import OptionalLLMDecisionModule


class LLMTaskSelectorDecisionModule(OptionalLLMDecisionModule):
    """Hybrid LLM mode.

    The engine still resolves hard constraints and feasible candidates.
    The LLM only chooses the next task from that candidate list.
    """

    def __init__(self, cfg: dict[str, Any], llm_cfg: dict[str, Any] | None = None) -> None:
        super().__init__(cfg=cfg, llm_cfg=llm_cfg)
        selector_cfg = self.llm_cfg.get("task_selector", {}) if isinstance(self.llm_cfg.get("task_selector", {}), dict) else {}
        self.task_selector_max_candidates = max(0, int(selector_cfg.get("max_candidates", 0)))
        self.task_selector_include_score_hints = bool(selector_cfg.get("include_score_hints", False))

    def select_next_task(self, selection_context: dict[str, Any]) -> dict[str, Any]:
        prompt = self._prompt(
            title="Choose the next task for one manufacturing agent.",
            payload=selection_context,
            schema_hint='{"selected_task_id": str, "rationale": str, "decision_focus": [str]}',
        )
        llm_obj = self._call_llm_json(
            user_prompt=prompt,
            system_prompt=self._shared_system_prompt(
                "You are a manufacturing task dispatcher. Hard constraints were already enforced by the simulator.",
                [
                    "This phase chooses exactly one next task for one mobile agent from the feasible candidate list only.",
                    "Choose exactly one task_id from the provided candidate_tasks only.",
                    "Use the structured selector context blocks: agent, plant_state, current_policy, and candidate_tasks. In selector payloads, machines.by_id and peer_agents_by_id are focused detail subsets; use summary counts for full-plant context.",
                    "current_policy includes the shared baseline plus the agent-specific priority profile and effective task-priority weights for this agent.",
                    "Base the choice on candidate feasibility, local plant flow, machine state, agent state, agent experience cues, and battery constraints when provided.",
                ],
            ),
            call_name="select_next_task",
            context={
                "phase": "select_next_task",
                "day": selection_context.get("day"),
                "agent_id": selection_context.get("agent_id"),
                "candidate_count": len(selection_context.get("candidate_tasks", [])) if isinstance(selection_context.get("candidate_tasks", []), list) else len(selection_context.get("candidates", [])) if isinstance(selection_context.get("candidates", []), list) else 0,
            },
        )
        selected_task_id = str(llm_obj.get("selected_task_id", "")).strip()
        if not selected_task_id:
            self._fail("select_next_task response missing selected_task_id.")
        rationale = str(llm_obj.get("rationale", "")).strip()
        decision_focus_raw = llm_obj.get("decision_focus", [])
        decision_focus: list[str] = []
        if isinstance(decision_focus_raw, list):
            for item in decision_focus_raw:
                if isinstance(item, str) and item.strip():
                    decision_focus.append(item.strip())
        return {
            "selected_task_id": selected_task_id,
            "rationale": rationale,
            "decision_focus": decision_focus,
        }
