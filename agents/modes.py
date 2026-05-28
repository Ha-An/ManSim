from __future__ import annotations

from typing import Final


_MODE_ALIASES: Final[dict[str, str]] = {
    "adaptive_priority": "adaptive_priority",
    "fixed_priority": "fixed_priority",
    "rolling_horizon_fixed_priority": "rolling_horizon_aging_priority",
    "rolling_horizon_aging_priority": "rolling_horizon_aging_priority",
    "rolling_horizon_dedicated_roles": "rolling_horizon_dedicated_roles",
    "fixed_task_assignment": "fixed_task_assignment",
    "llm_planner": "llm_planner",
    "openclaw_adaptive_priority": "openclaw_adaptive_priority",
}

_MODE_LABELS: Final[dict[str, str]] = {
    "adaptive_priority": "Adaptive Priority",
    "fixed_priority": "Fixed Priority",
    "rolling_horizon_aging_priority": "Rolling Horizon Aging Priority",
    "rolling_horizon_dedicated_roles": "Rolling Horizon Dedicated Roles",
    "fixed_task_assignment": "Fixed Task Assignment",
    "llm_planner": "LLM Planner",
    "openclaw_adaptive_priority": "OpenClaw Adaptive Priority",
}


def normalize_decision_mode(value: str | None, default: str = "adaptive_priority") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return _MODE_ALIASES.get(raw, raw)


def format_decision_mode_label(value: str | None) -> str:
    mode = normalize_decision_mode(value)
    return _MODE_LABELS.get(mode, mode.upper())


def is_fixed_priority_mode(value: str | None) -> bool:
    return normalize_decision_mode(value) in {"fixed_priority", "rolling_horizon_aging_priority", "rolling_horizon_dedicated_roles"}


def is_rolling_horizon_mode(value: str | None) -> bool:
    return normalize_decision_mode(value) in {"rolling_horizon_aging_priority", "rolling_horizon_dedicated_roles"}


def is_llm_mode(value: str | None) -> bool:
    return normalize_decision_mode(value) in {"llm_planner", "openclaw_adaptive_priority"}
