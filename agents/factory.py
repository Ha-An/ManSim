from __future__ import annotations

from typing import Any

from .llm_common import OptionalLLMDecisionModule
from .openclaw_adaptive_priority import OpenClawAdaptivePriorityDecisionModule
from .openclaw_orchestrated import OpenClawOrchestratedDecisionModule
from .scripted import FixedTaskAssignmentDecisionModule, ScriptedDecisionModule


def build_decision_module(*, experiment_cfg: dict[str, Any], decision_mode: str) -> Any:
    decision_cfg = experiment_cfg.get("decision", {}) if isinstance(experiment_cfg.get("decision", {}), dict) else {}
    if decision_mode in {"adaptive_priority", "fixed_priority"}:
        return ScriptedDecisionModule(experiment_cfg)
    if decision_mode == "fixed_task_assignment":
        return FixedTaskAssignmentDecisionModule(experiment_cfg)
    if decision_mode not in {"llm_planner", "openclaw_adaptive_priority"}:
        raise ValueError(f"Unsupported decision mode: {decision_mode}")

    llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
    provider = str(llm_cfg.get("provider", "")).strip().lower()
    orchestration_cfg = llm_cfg.get("orchestration", {}) if isinstance(llm_cfg.get("orchestration", {}), dict) else {}
    if decision_mode == "openclaw_adaptive_priority":
        if provider != "openclaw":
            raise RuntimeError("decision.mode=openclaw_adaptive_priority requires decision.llm.provider=openclaw.")
        return OpenClawAdaptivePriorityDecisionModule(
            cfg=experiment_cfg,
            llm_cfg=llm_cfg,
        )
    if provider == "openclaw" and bool(orchestration_cfg.get("enabled", True)):
        return OpenClawOrchestratedDecisionModule(
            cfg=experiment_cfg,
            llm_cfg=llm_cfg,
        )
    return OptionalLLMDecisionModule(
        cfg=experiment_cfg,
        llm_cfg=llm_cfg,
    )
