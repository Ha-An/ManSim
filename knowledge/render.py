from __future__ import annotations

from typing import Any


def render_knowledge_markdown(graph: dict[str, Any]) -> str:
    nodes = graph.get("nodes", {}) if isinstance(graph.get("nodes", {}), dict) else {}
    lessons = [node for node in nodes.values() if isinstance(node, dict) and str(node.get("type", "")) == "Lesson"]
    lessons.sort(
        key=lambda node: (
            -int(node.get("properties", {}).get("recurrence", 0) or 0),
            -int(node.get("properties", {}).get("last_seen_run", 0) or 0),
            str(node.get("label", "")),
        )
    )
    latest_run_seen = max(
        [int(node.get("properties", {}).get("last_seen_run", 0) or 0) for node in lessons],
        default=0,
    )

    def _lesson_lines(category: str, limit: int) -> list[str]:
        rows: list[str] = []
        for lesson in lessons:
            properties = lesson.get("properties", {}) if isinstance(lesson.get("properties", {}), dict) else {}
            if str(properties.get("category", "")) != category:
                continue
            if category == "latest" and int(properties.get("last_seen_run", 0) or 0) != latest_run_seen:
                continue
            label = str(lesson.get("label", "")).strip()
            if label:
                rows.append(f"- {label}")
            if len(rows) >= limit:
                break
        return rows or ["- No recorded lessons yet."]

    parts = [
        "# Run-Series Knowledge",
        "",
        "## Run-Series Scope",
        "Source of truth is the ontology graph. This markdown is a compact render for agent prompting.",
        "",
        "## Persistent Lessons",
        *_lesson_lines("carry_forward", 5),
        "",
        "## Latest Lessons",
        *_lesson_lines("latest", 3),
        "",
        "## Detector Guidance",
        *_lesson_lines("detector_guidance", 4),
        "",
        "## Planner Guidance",
        *_lesson_lines("planner_guidance", 4),
        "",
        "## Open Watchouts",
        *_lesson_lines("open_watchout", 4),
        "",
    ]
    return "\n".join(parts)
