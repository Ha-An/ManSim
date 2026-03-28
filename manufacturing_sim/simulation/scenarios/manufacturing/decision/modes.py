from __future__ import annotations

from typing import Final


_MODE_ALIASES: Final[dict[str, str]] = {
    "adaptive_priority": "adaptive_priority",
    "fixed_priority": "fixed_priority",
    "llm_planner": "llm_planner",
}

_MODE_LABELS: Final[dict[str, str]] = {
    "adaptive_priority": "Adaptive Priority",
    "fixed_priority": "Fixed Priority",
    "llm_planner": "LLM Planner",
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
    return normalize_decision_mode(value) == "fixed_priority"


def is_llm_mode(value: str | None) -> bool:
    return normalize_decision_mode(value) == "llm_planner"
