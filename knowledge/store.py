from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .render import render_knowledge_markdown
from .schema import empty_graph_payload


def _slug(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return normalized or "item"


def _tokenize(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(token) >= 4
    }


def _similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class KnowledgeStore:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.graph_path = self.root_dir / "knowledge_graph.json"
        self.markdown_path = self.root_dir / "KNOWLEDGE.md"
        self.graph: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.graph_path.exists():
            return empty_graph_payload()
        try:
            payload = json.loads(self.graph_path.read_text(encoding="utf-8"))
        except Exception:
            return empty_graph_payload()
        if not isinstance(payload, dict):
            return empty_graph_payload()
        payload.setdefault("meta", empty_graph_payload()["meta"])
        payload.setdefault("nodes", {})
        payload.setdefault("edges", [])
        return payload

    def save(self) -> None:
        self.graph_path.write_text(json.dumps(self.graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def render_markdown(self) -> str:
        markdown = render_knowledge_markdown(self.graph)
        self.markdown_path.write_text(markdown + ("\n" if not markdown.endswith("\n") else ""), encoding="utf-8")
        return markdown

    def ensure_node(self, node_id: str, *, node_type: str, label: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
        nodes = self.graph.setdefault("nodes", {})
        if not isinstance(nodes, dict):
            self.graph["nodes"] = {}
            nodes = self.graph["nodes"]
        current = nodes.get(node_id, {"id": node_id, "type": node_type, "label": label, "properties": {}})
        current["id"] = node_id
        current["type"] = node_type
        current["label"] = label
        prop = current.get("properties", {}) if isinstance(current.get("properties", {}), dict) else {}
        if properties:
            prop.update(properties)
        current["properties"] = prop
        nodes[node_id] = current
        return current

    def add_edge(self, source: str, relation: str, target: str, properties: dict[str, Any] | None = None) -> None:
        edges = self.graph.setdefault("edges", [])
        if not isinstance(edges, list):
            self.graph["edges"] = []
            edges = self.graph["edges"]
        candidate = {
            "source": source,
            "relation": relation,
            "target": target,
            "properties": properties or {},
        }
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            if (
                str(edge.get("source", "")) == source
                and str(edge.get("relation", "")) == relation
                and str(edge.get("target", "")) == target
                and (edge.get("properties", {}) if isinstance(edge.get("properties", {}), dict) else {}) == candidate["properties"]
            ):
                return
        edges.append(candidate)

    def _ensure_strategy_node(self, category: str) -> str:
        clean = str(category).strip() or "other"
        node_id = f"strategy:{_slug(clean)}"
        label = {
            "carry_forward": "Carry-Forward Guidance",
            "latest": "Latest Guidance",
            "detector_guidance": "Detector Guidance",
            "planner_guidance": "Planner Guidance",
            "open_watchout": "Open Watchout",
        }.get(clean, clean.replace("_", " ").title())
        self.ensure_node(
            node_id,
            node_type="StrategyPattern",
            label=label,
            properties={"category": clean},
        )
        return node_id

    def _upsert_intervention(self, text: str, *, role: str, source_key: str, run_index: int) -> str | None:
        clean = str(text).strip()
        if not clean:
            return None
        node_id = f"intervention:{role}:{_slug(clean)}"
        node = self.ensure_node(
            node_id,
            node_type="Intervention",
            label=clean,
            properties={
                "role": role,
                "source_key": source_key,
            },
        )
        properties = node.get("properties", {}) if isinstance(node.get("properties", {}), dict) else {}
        properties["recurrence"] = _safe_int(properties.get("recurrence"), 0) + 1
        properties["last_seen_run"] = int(run_index)
        node["properties"] = properties
        return node_id

    def _upsert_outcome(
        self,
        *,
        outcome_key: str,
        label: str,
        severity: str,
        kind: str,
        run_index: int,
    ) -> str:
        node_id = f"outcome:{_slug(outcome_key)}"
        node = self.ensure_node(
            node_id,
            node_type="Outcome",
            label=label,
            properties={
                "kind": kind,
                "severity": severity,
            },
        )
        properties = node.get("properties", {}) if isinstance(node.get("properties", {}), dict) else {}
        properties["recurrence"] = _safe_int(properties.get("recurrence"), 0) + 1
        properties["last_seen_run"] = int(run_index)
        node["properties"] = properties
        return node_id

    def _derive_run_outcomes(self, *, kpi: dict[str, Any], daily_summary: list[dict[str, Any]], run_index: int) -> list[dict[str, Any]]:
        total_products = _safe_int(kpi.get("total_products"), 0)
        closure = _safe_float(kpi.get("downstream_closure_ratio"), 0.0)
        coordination = _safe_int(kpi.get("coordination_incident_total"), 0)
        broken_ratio = _safe_float(kpi.get("machine_broken_ratio"), 0.0)
        days = max(1, len(daily_summary))
        avg_daily_products = total_products / float(days)
        last_day = daily_summary[-1] if daily_summary and isinstance(daily_summary[-1], dict) else {}
        backlog_end = _safe_int(last_day.get("inspection_backlog_end"), 0)

        rows: list[dict[str, Any]] = []

        if avg_daily_products < 2.0:
            rows.append(
                {
                    "key": "throughput-low",
                    "label": "Throughput remained low across the run.",
                    "kind": "throughput",
                    "severity": "high",
                    "value": round(avg_daily_products, 3),
                }
            )
        elif avg_daily_products < 3.5:
            rows.append(
                {
                    "key": "throughput-constrained",
                    "label": "Throughput remained constrained across the run.",
                    "kind": "throughput",
                    "severity": "medium",
                    "value": round(avg_daily_products, 3),
                }
            )

        if closure < 0.4:
            rows.append(
                {
                    "key": "downstream-closure-low",
                    "label": "Downstream closure remained low.",
                    "kind": "closure",
                    "severity": "high",
                    "value": round(closure, 3),
                }
            )
        elif closure < 0.7:
            rows.append(
                {
                    "key": "downstream-closure-unstable",
                    "label": "Downstream closure remained unstable.",
                    "kind": "closure",
                    "severity": "medium",
                    "value": round(closure, 3),
                }
            )

        if backlog_end >= 8:
            rows.append(
                {
                    "key": "inspection-backlog-high",
                    "label": "Inspection backlog remained high at run end.",
                    "kind": "inspection_backlog",
                    "severity": "high",
                    "value": backlog_end,
                }
            )
        elif backlog_end >= 4:
            rows.append(
                {
                    "key": "inspection-backlog-elevated",
                    "label": "Inspection backlog remained elevated at run end.",
                    "kind": "inspection_backlog",
                    "severity": "medium",
                    "value": backlog_end,
                }
            )

        if coordination >= 300:
            rows.append(
                {
                    "key": "coordination-incidents-high",
                    "label": "Coordination incidents remained high.",
                    "kind": "coordination",
                    "severity": "high",
                    "value": coordination,
                }
            )
        elif coordination >= 150:
            rows.append(
                {
                    "key": "coordination-incidents-elevated",
                    "label": "Coordination incidents remained elevated.",
                    "kind": "coordination",
                    "severity": "medium",
                    "value": coordination,
                }
            )

        if broken_ratio >= 0.12:
            rows.append(
                {
                    "key": "machine-reliability-risk-high",
                    "label": "Machine reliability risk remained high.",
                    "kind": "reliability",
                    "severity": "high",
                    "value": round(broken_ratio, 3),
                }
            )
        elif broken_ratio >= 0.08:
            rows.append(
                {
                    "key": "machine-reliability-risk-elevated",
                    "label": "Machine reliability risk remained elevated.",
                    "kind": "reliability",
                    "severity": "medium",
                    "value": round(broken_ratio, 3),
                }
            )

        outcome_ids: list[dict[str, Any]] = []
        for row in rows:
            outcome_id = self._upsert_outcome(
                outcome_key=str(row["key"]),
                label=str(row["label"]),
                severity=str(row["severity"]),
                kind=str(row["kind"]),
                run_index=run_index,
            )
            outcome_ids.append({"id": outcome_id, **row})
        return outcome_ids

    def _upsert_lesson(self, text: str, *, category: str, run_index: int) -> str | None:
        clean = str(text).strip()
        if not clean:
            return None
        node_id = f"lesson:{category}:{_slug(clean)}"
        node = self.ensure_node(
            node_id,
            node_type="Lesson",
            label=clean,
            properties={
                "category": category,
            },
        )
        properties = node.get("properties", {}) if isinstance(node.get("properties", {}), dict) else {}
        properties["recurrence"] = int(properties.get("recurrence", 0) or 0) + 1
        properties["last_seen_run"] = int(run_index)
        node["properties"] = properties
        return node_id

    def ingest_run(
        self,
        *,
        run_index: int,
        kpi: dict[str, Any],
        daily_summary: list[dict[str, Any]],
        reflection: dict[str, Any] | None,
    ) -> None:
        run_id = f"run:{int(run_index):02d}"
        issue_node_ids: list[str] = []
        self.ensure_node(
            run_id,
            node_type="Run",
            label=f"Run {int(run_index):02d}",
            properties={
                "total_products": int(kpi.get("total_products", 0) or 0),
                "downstream_closure_ratio": float(kpi.get("downstream_closure_ratio", 0.0) or 0.0),
                "machine_broken_ratio": float(kpi.get("machine_broken_ratio", 0.0) or 0.0),
                "machine_pm_ratio": float(kpi.get("machine_pm_ratio", 0.0) or 0.0),
            },
        )
        for row in daily_summary:
            if not isinstance(row, dict):
                continue
            day = int(row.get("day", 0) or 0)
            day_id = f"{run_id}:day:{day:02d}"
            self.ensure_node(
                day_id,
                node_type="Day",
                label=f"Run {int(run_index):02d} Day {day:02d}",
                properties={
                    "products": int(row.get("products", 0) or 0),
                    "inspection_backlog_end": int(row.get("inspection_backlog_end", 0) or 0),
                    "machine_breakdowns": int(row.get("machine_breakdowns", 0) or 0),
                },
            )
            self.add_edge(day_id, "observed_in", run_id, {})

        if not isinstance(reflection, dict):
            return

        outcome_rows = self._derive_run_outcomes(kpi=kpi, daily_summary=daily_summary, run_index=run_index)
        for outcome in outcome_rows:
            self.add_edge(
                str(outcome["id"]),
                "observed_in",
                run_id,
                {"run_index": int(run_index), "value": outcome.get("value"), "kind": outcome.get("kind"), "severity": outcome.get("severity")},
            )

        for raw_problem in reflection.get("run_problems", []) if isinstance(reflection.get("run_problems", []), list) else []:
            if isinstance(raw_problem, dict):
                issue_text = str(raw_problem.get("issue", "")).strip()
                impact_text = str(raw_problem.get("impact", "")).strip()
            else:
                issue_text = str(raw_problem).strip()
                impact_text = ""
            if not issue_text:
                continue
            issue_id = f"issue:{_slug(issue_text)}"
            node = self.ensure_node(
                issue_id,
                node_type="Issue",
                label=issue_text,
                properties={"confidence": "reflector", "current_relevance": "recent"},
            )
            properties = node.get("properties", {}) if isinstance(node.get("properties", {}), dict) else {}
            properties["recurrence"] = int(properties.get("recurrence", 0) or 0) + 1
            properties["last_seen_run"] = int(run_index)
            if impact_text:
                properties["latest_impact"] = impact_text
            node["properties"] = properties
            self.add_edge(issue_id, "observed_in", run_id, {})
            issue_node_ids.append(issue_id)

        category_map = {
            "carry_forward_lessons": ("carry_forward", "latest"),
            "detector_guidance": ("detector_guidance", None),
            "planner_guidance": ("planner_guidance", None),
            "open_watchouts": ("open_watchout", None),
        }
        lesson_node_ids: list[str] = []
        for key, (category, also_latest) in category_map.items():
            values = reflection.get(key, []) if isinstance(reflection.get(key, []), list) else []
            for item in values:
                lesson_node_id = self._upsert_lesson(str(item), category=category, run_index=run_index)
                if lesson_node_id:
                    lesson_node_ids.append(lesson_node_id)
                    self.add_edge(lesson_node_id, "observed_in", run_id, {})
                    self.add_edge(lesson_node_id, "recommended_for", self._ensure_strategy_node(category), {"run_index": int(run_index)})
                if also_latest:
                    latest_node_id = self._upsert_lesson(str(item), category=also_latest, run_index=run_index)
                    if latest_node_id:
                        lesson_node_ids.append(latest_node_id)
                        self.add_edge(latest_node_id, "observed_in", run_id, {})
                        self.add_edge(latest_node_id, "recommended_for", self._ensure_strategy_node(also_latest), {"run_index": int(run_index)})

        intervention_node_ids: list[str] = []
        intervention_specs = [
            ("planner_should_have_done", "planner"),
            ("planner_guidance", "planner"),
            ("detector_should_have_done", "detector"),
            ("detector_guidance", "detector"),
        ]
        for key, role in intervention_specs:
            values = reflection.get(key, []) if isinstance(reflection.get(key, []), list) else []
            for item in values:
                intervention_node_id = self._upsert_intervention(str(item), role=role, source_key=key, run_index=run_index)
                if intervention_node_id:
                    intervention_node_ids.append(intervention_node_id)
                    self.add_edge(intervention_node_id, "observed_in", run_id, {"run_index": int(run_index), "role": role})
                    self.add_edge(intervention_node_id, "recommended_for", self._ensure_strategy_node(f"{role}_intervention"), {"run_index": int(run_index)})

        if intervention_node_ids and outcome_rows:
            outcome_token_index = {str(outcome["id"]): _tokenize(str(outcome["label"])) for outcome in outcome_rows}
            fallback_outcomes = [str(outcome["id"]) for outcome in outcome_rows[:2]]
            for intervention_id in intervention_node_ids:
                node = (self.graph.get("nodes", {}) or {}).get(intervention_id, {}) if isinstance(self.graph.get("nodes", {}), dict) else {}
                intervention_tokens = _tokenize(str(node.get("label", "")))
                linked: list[tuple[float, str]] = []
                for outcome_id, outcome_tokens in outcome_token_index.items():
                    score = _similarity(intervention_tokens, outcome_tokens)
                    if score >= 0.08:
                        linked.append((score, outcome_id))
                linked.sort(key=lambda item: (-item[0], item[1]))
                targets = [outcome_id for _, outcome_id in linked[:3]]
                if not targets:
                    targets = fallback_outcomes
                for outcome_id in targets:
                    self.add_edge(
                        intervention_id,
                        "improved",
                        outcome_id,
                        {"run_index": int(run_index), "source": "reflection_target"},
                    )

        if issue_node_ids and lesson_node_ids:
            lesson_token_index: dict[str, set[str]] = {}
            for lesson_id in lesson_node_ids:
                node = (self.graph.get("nodes", {}) or {}).get(lesson_id, {}) if isinstance(self.graph.get("nodes", {}), dict) else {}
                lesson_token_index[lesson_id] = _tokenize(str(node.get("label", "")))
            for issue_id in issue_node_ids:
                issue_node = (self.graph.get("nodes", {}) or {}).get(issue_id, {}) if isinstance(self.graph.get("nodes", {}), dict) else {}
                issue_tokens = _tokenize(str(issue_node.get("label", "")))
                if not issue_tokens:
                    continue
                scored_lessons: list[tuple[float, str]] = []
                for lesson_id, lesson_tokens in lesson_token_index.items():
                    score = _similarity(issue_tokens, lesson_tokens)
                    if score >= 0.10:
                        scored_lessons.append((score, lesson_id))
                scored_lessons.sort(key=lambda item: (-item[0], item[1]))
                for score, lesson_id in scored_lessons[:4]:
                    self.add_edge(
                        issue_id,
                        "mitigated_by",
                        lesson_id,
                        {"run_index": int(run_index), "score": round(float(score), 3)},
                    )

        if issue_node_ids and intervention_node_ids:
            intervention_token_index: dict[str, set[str]] = {}
            for intervention_id in intervention_node_ids:
                node = (self.graph.get("nodes", {}) or {}).get(intervention_id, {}) if isinstance(self.graph.get("nodes", {}), dict) else {}
                intervention_token_index[intervention_id] = _tokenize(str(node.get("label", "")))
            fallback_interventions = intervention_node_ids[:2]
            for issue_id in issue_node_ids:
                issue_node = (self.graph.get("nodes", {}) or {}).get(issue_id, {}) if isinstance(self.graph.get("nodes", {}), dict) else {}
                issue_tokens = _tokenize(str(issue_node.get("label", "")))
                if not issue_tokens:
                    continue
                scored_interventions: list[tuple[float, str]] = []
                for intervention_id, intervention_tokens in intervention_token_index.items():
                    score = _similarity(issue_tokens, intervention_tokens)
                    if score >= 0.08:
                        scored_interventions.append((score, intervention_id))
                scored_interventions.sort(key=lambda item: (-item[0], item[1]))
                targets = [(score, intervention_id) for score, intervention_id in scored_interventions[:3]]
                if not targets:
                    targets = [(0.0, intervention_id) for intervention_id in fallback_interventions]
                for score, intervention_id in targets:
                    self.add_edge(
                        issue_id,
                        "mitigated_by",
                        intervention_id,
                        {"run_index": int(run_index), "score": round(float(score), 3), "source": "reflection_recommendation"},
                    )
