from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

from .shell import render_page_shell


STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "when", "then", "than", "have", "has",
    "been", "being", "are", "was", "were", "will", "must", "need", "more", "less", "very", "high", "low",
    "future", "runs", "run", "stage", "stages", "current", "using", "based", "around", "remain", "remains",
    "stable", "persistent", "systemic", "issue", "issues", "guidance", "focus", "planning", "planner", "detector",
    "requires", "require", "suggests", "suggest", "improve", "improves", "improved", "develop", "develops",
    "build", "treat", "prioritize", "mitigate", "need", "needs",
}

CATEGORY_LABELS = {
    "carry_forward": "Carry-Forward",
    "detector_guidance": "Detector Guidance",
    "planner_guidance": "Planner Guidance",
    "open_watchout": "Open Watchout",
    "latest": "Latest",
    "other": "Other",
}

CATEGORY_COLORS = {
    "issue": "#0f5cc0",
    "run": "#64748b",
    "intervention_planner": "#0f8c5b",
    "intervention_detector": "#d97706",
    "outcome": "#7c3aed",
    "carry_forward": "#1d4e89",
    "detector_guidance": "#d97706",
    "planner_guidance": "#0f8c5b",
    "open_watchout": "#c0392b",
    "latest": "#7c3aed",
    "other": "#475569",
}


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


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "item"


def _tokenize(text: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", str(text).lower())
        if len(token) >= 4 and token not in STOPWORDS
    }
    return tokens


def _similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _cluster_records(records: list[dict[str, Any]], *, token_key: str = "tokens", threshold: float = 0.28) -> list[dict[str, Any]]:
    clusters: list[dict[str, Any]] = []
    for record in records:
        tokens = record.get(token_key, set()) if isinstance(record.get(token_key, set()), set) else set()
        best_idx = -1
        best_score = 0.0
        for idx, cluster in enumerate(clusters):
            cluster_tokens = cluster.get("tokens", set()) if isinstance(cluster.get("tokens", set()), set) else set()
            score = _similarity(tokens, cluster_tokens)
            if score > best_score:
                best_score = score
                best_idx = idx
        if best_idx >= 0 and best_score >= threshold:
            cluster = clusters[best_idx]
            cluster["items"].append(record)
            cluster["tokens"] = set(cluster.get("tokens", set())) | tokens
        else:
            clusters.append({"tokens": set(tokens), "items": [record]})
    normalized: list[dict[str, Any]] = []
    for cluster in clusters:
        items = cluster.get("items", []) if isinstance(cluster.get("items", []), list) else []
        if not items:
            continue
        labels = [str(item.get("label", "")).strip() for item in items if str(item.get("label", "")).strip()]
        representative = min(labels, key=len) if labels else "-"
        run_ids: set[str] = set()
        last_seen = 0
        recurrence = 0
        for item in items:
            props = item.get("properties", {}) if isinstance(item.get("properties", {}), dict) else {}
            recurrence += max(1, _safe_int(props.get("recurrence"), 1))
            last_seen = max(last_seen, _safe_int(props.get("last_seen_run"), 0))
            for run_id in _list_or_empty(item.get("run_ids", [])):
                run_ids.add(str(run_id))
        normalized.append(
            {
                "id": _slug(representative),
                "label": representative,
                "tokens": sorted(cluster.get("tokens", set())),
                "items": items,
                "member_count": len(items),
                "runs_seen": sorted(run_ids),
                "run_count": len(run_ids),
                "last_seen_run": last_seen,
                "recurrence": recurrence,
            }
        )
    normalized.sort(key=lambda row: (-int(row.get("run_count", 0)), -int(row.get("last_seen_run", 0)), str(row.get("label", ""))))
    return normalized


def _find_run(manifest: dict[str, Any] | None, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return None
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    target = str(run_id or manifest.get("current_run", "")).strip()
    for row in runs:
        if isinstance(row, dict) and str(row.get("id", "")).strip() == target:
            return row
    return runs[-1] if runs and isinstance(runs[-1], dict) else None


def _load_reflection(path_str: str) -> dict[str, Any]:
    path = Path(str(path_str).strip())
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_graph_records(graph: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, set[str]]]:
    nodes = graph.get("nodes", {}) if isinstance(graph.get("nodes", {}), dict) else {}
    edges = graph.get("edges", []) if isinstance(graph.get("edges", []), list) else []
    observed_runs: dict[str, set[str]] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if str(edge.get("relation", "")) != "observed_in":
            continue
        source = str(edge.get("source", "")).strip()
        target = str(edge.get("target", "")).strip()
        if source and target:
            observed_runs.setdefault(source, set()).add(target.replace("run:", "run_"))
    issue_records: list[dict[str, Any]] = []
    lesson_records: list[dict[str, Any]] = []
    for node in nodes.values():
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type", "")).strip()
        label = str(node.get("label", "")).strip()
        props = node.get("properties", {}) if isinstance(node.get("properties", {}), dict) else {}
        record = {
            "id": str(node.get("id", "")).strip(),
            "label": label,
            "properties": props,
            "tokens": _tokenize(label),
            "run_ids": sorted(observed_runs.get(str(node.get("id", "")).strip(), set())),
        }
        if node_type == "Issue":
            issue_records.append(record)
        elif node_type == "Lesson":
            lesson_records.append(record)
    return issue_records, lesson_records, observed_runs


def _edge_maps(graph: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    nodes = graph.get("nodes", {}) if isinstance(graph.get("nodes", {}), dict) else {}
    edges = graph.get("edges", []) if isinstance(graph.get("edges", []), list) else []
    out_map: dict[str, list[dict[str, Any]]] = {}
    in_map: dict[str, list[dict[str, Any]]] = {}
    for raw in edges:
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source", "")).strip()
        target = str(raw.get("target", "")).strip()
        relation = str(raw.get("relation", "")).strip()
        props = raw.get("properties", {}) if isinstance(raw.get("properties", {}), dict) else {}
        if not source or not target or not relation:
            continue
        edge = {"source": source, "target": target, "relation": relation, "properties": props}
        out_map.setdefault(source, []).append(edge)
        in_map.setdefault(target, []).append(edge)
    return nodes, out_map, in_map


def _issue_table_html(issue_clusters: list[dict[str, Any]]) -> str:
    rows = []
    for cluster in issue_clusters[:12]:
        runs = ", ".join(cluster.get("runs_seen", [])) or "-"
        last_seen_run = int(cluster.get("last_seen_run", 0) or 0)
        last_seen = f"run_{last_seen_run:02d}" if last_seen_run > 0 else "-"
        rows.append(
            f"<tr><td>{html.escape(str(cluster.get('label', '-')))}</td><td>{int(cluster.get('run_count', 0) or 0)}</td><td>{int(cluster.get('recurrence', 0) or 0)}</td><td>{html.escape(last_seen)}</td><td>{html.escape(runs)}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='5'>No recurring issue records.</td></tr>")
    return "<table><thead><tr><th>Issue Cluster</th><th>Runs Seen</th><th>Recurrence</th><th>Last Seen</th><th>Runs</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _lesson_panels(lesson_clusters: list[dict[str, Any]]) -> str:
    by_category: dict[str, list[dict[str, Any]]] = {}
    for cluster in lesson_clusters:
        first = cluster.get("items", [{}])[0] if isinstance(cluster.get("items", [{}]), list) else {}
        props = first.get("properties", {}) if isinstance(first.get("properties", {}), dict) else {}
        category = str(props.get("category", "other"))
        by_category.setdefault(category, []).append(cluster)
    panels: list[str] = []
    mapping = [
        ("carry_forward", "Carry-Forward Lessons"),
        ("detector_guidance", "Detector Guidance"),
        ("planner_guidance", "Planner Guidance"),
        ("open_watchout", "Open Watchouts"),
    ]
    for category, title in mapping:
        items = by_category.get(category, [])[:6]
        body = "".join(
            f"<li><strong>{html.escape(str(cluster.get('label', '-')))}</strong><br><span class='muted'>runs={int(cluster.get('run_count', 0) or 0)} | recurrence={int(cluster.get('recurrence', 0) or 0)}</span></li>"
            for cluster in items
        ) or "<li>No records.</li>"
        panels.append(f"<div class='panel'><h2>{html.escape(title)}</h2><ul class='clean'>{body}</ul></div>")
    return "<section class='section'><div class='grid cards-4'>" + "".join(panels) + "</div></section>"


def _intervention_rows(manifest: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        return []
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    records: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        reflection = _load_reflection(str((run.get("artifacts", {}) or {}).get("run_reflection.json", "")))
        kpi = run.get("kpi", {}) if isinstance(run.get("kpi", {}), dict) else {}
        for category, key in (("detector", "detector_guidance"), ("planner", "planner_guidance"), ("planner_action", "planner_should_have_done")):
            values = reflection.get(key, []) if isinstance(reflection.get(key, []), list) else []
            for text in values:
                clean = str(text).strip()
                if not clean:
                    continue
                records.append(
                    {
                        "label": clean,
                        "category": category,
                        "tokens": _tokenize(clean),
                        "run_id": str(run.get("id", "")).strip(),
                        "products": _safe_int(kpi.get("total_products")),
                        "closure": _safe_float(kpi.get("downstream_closure_ratio")),
                        "coordination": _safe_int(kpi.get("coordination_incident_total")),
                    }
                )
    return records


def _intervention_table(manifest: dict[str, Any] | None) -> str:
    records = _intervention_rows(manifest)
    clusters = _cluster_records(records, threshold=0.26)
    rows = []
    for cluster in clusters[:12]:
        items = cluster.get("items", []) if isinstance(cluster.get("items", []), list) else []
        runs_seen = sorted({str(item.get("run_id", "")).strip() for item in items if str(item.get("run_id", "")).strip()})
        products = [_safe_float(item.get("products")) for item in items]
        closures = [_safe_float(item.get("closure")) for item in items]
        coordination = [_safe_float(item.get("coordination")) for item in items]
        category = str(items[0].get("category", "-") if items else "-")
        rows.append(
            f"<tr><td>{html.escape(category)}</td><td>{html.escape(str(cluster.get('label', '-')))}</td><td>{html.escape(', '.join(runs_seen) or '-')}</td><td>{(sum(products) / len(products)) if products else 0:.2f}</td><td>{(sum(closures) / len(closures)) if closures else 0:.3f}</td><td>{(sum(coordination) / len(coordination)) if coordination else 0:.1f}</td></tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6'>No intervention guidance records.</td></tr>")
    return "<section class='section'><div class='panel'><h2>Intervention Effectiveness (Association View)</h2><p class='muted'>This is not causal attribution. It shows which guidance clusters appeared in which runs and what those runs delivered.</p><table><thead><tr><th>Type</th><th>Guidance Cluster</th><th>Runs</th><th>Avg Products</th><th>Avg Closure</th><th>Avg Coordination Incidents</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>"


def _run_diff_html(manifest: dict[str, Any] | None, current_run_id: str | None) -> str:
    if not isinstance(manifest, dict):
        return ""
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    if not runs:
        return ""
    baseline = runs[0] if isinstance(runs[0], dict) else None
    current = _find_run(manifest, current_run_id)
    if not isinstance(baseline, dict) or not isinstance(current, dict):
        return ""
    base_ref = _load_reflection(str((baseline.get("artifacts", {}) or {}).get("run_reflection.json", "")))
    cur_ref = _load_reflection(str((current.get("artifacts", {}) or {}).get("run_reflection.json", "")))

    def _collect(payload: dict[str, Any]) -> set[str]:
        items: set[str] = set()
        for key in ("carry_forward_lessons", "detector_guidance", "planner_guidance", "open_watchouts"):
            values = payload.get(key, []) if isinstance(payload.get(key, []), list) else []
            for text in values:
                clean = str(text).strip()
                if clean:
                    items.add(clean)
        return items

    base_items = _collect(base_ref)
    cur_items = _collect(cur_ref)
    new_items = sorted(cur_items - base_items)[:6]
    retained = sorted(cur_items & base_items)[:6]
    dropped = sorted(base_items - cur_items)[:6]

    def _list_html(title: str, items: list[str]) -> str:
        body = "".join(f"<li>{html.escape(item)}</li>" for item in items) or "<li>-</li>"
        return f"<div class='panel'><h2>{html.escape(title)}</h2><ul class='clean'>{body}</ul></div>"

    return "<section class='section'><div class='grid cards-3'>" + "".join(
        [
            _list_html("New Since Baseline", new_items),
            _list_html("Retained From Baseline", retained),
            _list_html("Dropped Since Baseline", dropped),
        ]
    ) + "</div></section>"


def _relation_views(issue_clusters: list[dict[str, Any]], lesson_clusters: list[dict[str, Any]], graph: dict[str, Any]) -> dict[str, Any]:
    nodes, out_map, _in_map = _edge_maps(graph)
    lesson_cluster_by_node_id: dict[str, dict[str, Any]] = {}
    for lesson_cluster in lesson_clusters:
        for item in _list_or_empty(lesson_cluster.get("items", [])):
            lesson_cluster_by_node_id[str(item.get("id", "")).strip()] = lesson_cluster
    views: dict[str, Any] = {}
    for cluster in issue_clusters[:10]:
        issue_tokens = set(cluster.get("tokens", []))
        issue_node_ids = [
            str(item.get("id", "")).strip()
            for item in _list_or_empty(cluster.get("items", []))
            if str(item.get("id", "")).strip()
        ]
        related_lessons_map: dict[str, dict[str, Any]] = {}
        intervention_map: dict[str, dict[str, Any]] = {}
        outcome_map: dict[str, dict[str, Any]] = {}
        strategy_map: dict[str, dict[str, Any]] = {}
        runs_seen = set(cluster.get("runs_seen", []))
        used_ontology_edges = False

        for issue_id in issue_node_ids:
            for edge in out_map.get(issue_id, []):
                relation = str(edge.get("relation", ""))
                if relation == "observed_in":
                    runs_seen.add(str(edge.get("target", "")).replace("run:", "run_"))
                    continue
                if relation != "mitigated_by":
                    continue
                target_id = str(edge.get("target", "")).strip()
                target_node = nodes.get(target_id, {}) if isinstance(nodes, dict) else {}
                target_type = str(target_node.get("type", "")).strip() if isinstance(target_node, dict) else ""
                if target_type == "Intervention":
                    used_ontology_edges = True
                    intervention_props = target_node.get("properties", {}) if isinstance(target_node.get("properties", {}), dict) else {}
                    role = str(intervention_props.get("role", "other")).strip() or "other"
                    intervention_entry = intervention_map.setdefault(
                        target_id,
                        {
                            "id": target_id,
                            "label": str(target_node.get("label", target_id)).strip() or target_id,
                            "role": role,
                            "source_key": str(intervention_props.get("source_key", "")).strip(),
                            "runs_seen": [],
                            "run_count": 0,
                            "outcomes": [],
                        },
                    )
                    intervention_runs: set[str] = set(intervention_entry.get("runs_seen", []))
                    for intervention_edge in out_map.get(target_id, []):
                        intervention_relation = str(intervention_edge.get("relation", "")).strip()
                        if intervention_relation == "observed_in":
                            intervention_runs.add(str(intervention_edge.get("target", "")).replace("run:", "run_"))
                            continue
                        if intervention_relation not in {"improved", "worsened"}:
                            continue
                        outcome_id = str(intervention_edge.get("target", "")).strip()
                        outcome_node = nodes.get(outcome_id, {}) if isinstance(nodes, dict) else {}
                        if not isinstance(outcome_node, dict) or str(outcome_node.get("type", "")).strip() != "Outcome":
                            continue
                        outcome_props = outcome_node.get("properties", {}) if isinstance(outcome_node.get("properties", {}), dict) else {}
                        outcome_entry = outcome_map.setdefault(
                            outcome_id,
                            {
                                "id": outcome_id,
                                "label": str(outcome_node.get("label", outcome_id)).strip() or outcome_id,
                                "kind": str(outcome_props.get("kind", "other")).strip() or "other",
                                "severity": str(outcome_props.get("severity", "medium")).strip() or "medium",
                                "relation": intervention_relation,
                            },
                        )
                        linked_outcomes = intervention_entry.get("outcomes", [])
                        if isinstance(linked_outcomes, list) and outcome_id not in linked_outcomes:
                            linked_outcomes.append(outcome_id)
                            intervention_entry["outcomes"] = linked_outcomes
                    intervention_entry["runs_seen"] = sorted(intervention_runs)
                    intervention_entry["run_count"] = len(intervention_runs)
                    continue

                lesson_cluster = lesson_cluster_by_node_id.get(target_id)
                if not lesson_cluster:
                    continue
                used_ontology_edges = True
                lesson_cluster_id = str(lesson_cluster.get("id", "")).strip()
                first_item = lesson_cluster.get("items", [{}])[0] if isinstance(lesson_cluster.get("items", [{}]), list) else {}
                props = first_item.get("properties", {}) if isinstance(first_item.get("properties", {}), dict) else {}
                category = str(props.get("category", "other")).strip() or "other"
                entry = related_lessons_map.setdefault(
                    lesson_cluster_id,
                    {
                        "id": lesson_cluster_id,
                        "label": str(lesson_cluster.get("label", "-")),
                        "score": 0.0,
                        "category": category,
                        "category_label": CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
                        "runs_seen": list(lesson_cluster.get("runs_seen", []))[:6],
                        "run_count": int(lesson_cluster.get("run_count", 0) or 0),
                        "member_count": int(lesson_cluster.get("member_count", 0) or 0),
                        "strategies": [],
                    },
                )
                entry["score"] = max(float(entry.get("score", 0.0)), float((edge.get("properties", {}) or {}).get("score", 0.0) or 0.0))
                for lesson_item in _list_or_empty(lesson_cluster.get("items", [])):
                    lesson_item_id = str(lesson_item.get("id", "")).strip()
                    if not lesson_item_id:
                        continue
                    for lesson_edge in out_map.get(lesson_item_id, []):
                        if str(lesson_edge.get("relation", "")) != "recommended_for":
                            continue
                        strategy_id = str(lesson_edge.get("target", "")).strip()
                        strategy_node = nodes.get(strategy_id, {}) if isinstance(nodes, dict) else {}
                        if not isinstance(strategy_node, dict):
                            continue
                        strategy_map.setdefault(
                            strategy_id,
                            {
                                "id": strategy_id,
                                "label": str(strategy_node.get("label", strategy_id)).strip() or strategy_id,
                                "category": str((strategy_node.get("properties", {}) or {}).get("category", "other")).strip() or "other",
                            },
                        )
                        strategies = entry.get("strategies", [])
                        if isinstance(strategies, list) and strategy_id not in strategies:
                            strategies.append(strategy_id)
                            entry["strategies"] = strategies

        if not related_lessons_map:
            for lesson in lesson_clusters:
                lesson_tokens = set(lesson.get("tokens", []))
                score = _similarity(issue_tokens, lesson_tokens)
                if score < 0.12:
                    continue
                first_item = lesson.get("items", [{}])[0] if isinstance(lesson.get("items", [{}]), list) else {}
                props = first_item.get("properties", {}) if isinstance(first_item.get("properties", {}), dict) else {}
                category = str(props.get("category", "other")).strip() or "other"
                related_lessons_map[str(lesson.get("id", _slug(str(lesson.get('label', 'lesson')))))] = {
                    "id": str(lesson.get("id", _slug(str(lesson.get("label", "lesson"))))),
                    "label": str(lesson.get("label", "-")),
                    "score": round(score, 3),
                    "category": category,
                    "category_label": CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
                    "runs_seen": list(lesson.get("runs_seen", []))[:6],
                    "run_count": int(lesson.get("run_count", 0) or 0),
                    "member_count": int(lesson.get("member_count", 0) or 0),
                    "strategies": [],
                }

        related_lessons = sorted(
            related_lessons_map.values(),
            key=lambda row: (-float(row.get("score", 0.0)), -int(row.get("run_count", 0) or 0), str(row.get("label", ""))),
        )
        interventions = sorted(
            intervention_map.values(),
            key=lambda row: (-int(row.get("run_count", 0) or 0), str(row.get("role", "")), str(row.get("label", ""))),
        )
        outcomes = sorted(
            outcome_map.values(),
            key=lambda row: ({"high": 0, "medium": 1, "low": 2}.get(str(row.get("severity", "medium")), 3), str(row.get("label", ""))),
        )
        views[str(cluster.get("id", "item"))] = {
            "label": str(cluster.get("label", "-")),
            "runs": [{"id": run_id, "label": run_id} for run_id in list(sorted(runs_seen))[:8]],
            "lessons": related_lessons[:10],
            "interventions": interventions[:10],
            "outcomes": outcomes[:10],
            "strategies": list(strategy_map.values())[:6],
            "metrics": {
                "run_count": int(cluster.get("run_count", 0) or 0),
                "recurrence": int(cluster.get("recurrence", 0) or 0),
                "last_seen_run": int(cluster.get("last_seen_run", 0) or 0),
                "member_count": int(cluster.get("member_count", 0) or 0),
                "edge_mode": "ontology_edges" if used_ontology_edges else "semantic_fallback",
            },
        }
    return views


def _relation_graph_html(issue_clusters: list[dict[str, Any]], lesson_clusters: list[dict[str, Any]], graph: dict[str, Any]) -> str:
    views = _relation_views(issue_clusters, lesson_clusters, graph)
    if not views:
        return ""
    first_key = next(iter(views.keys()))
    options = "".join(f"<option value='{html.escape(key)}'>{html.escape(str(value.get('label', key)))}</option>" for key, value in views.items())
    payload = json.dumps(views, ensure_ascii=False).replace("</", "<\\/")
    legend = "".join(
        f"<span style='display:inline-flex;align-items:center;gap:6px;margin-right:14px;'><span style='width:12px;height:12px;border-radius:999px;background:{color};display:inline-block;'></span>{html.escape(label)}</span>"
        for label, color in (
            ("Issue", CATEGORY_COLORS["issue"]),
            ("Observed run", CATEGORY_COLORS["run"]),
            ("Planner intervention", CATEGORY_COLORS["intervention_planner"]),
            ("Detector intervention", CATEGORY_COLORS["intervention_detector"]),
            ("Outcome", CATEGORY_COLORS["outcome"]),
        )
    )
    return f"""
<section class='section'>
  <div class='panel'>
    <h2>Selected Node Network Drill-Down</h2>
    <p class='muted'>The default operating view stays hybrid: tables, trends, and effect summaries remain the primary signal. This network is drill-down only. Pick an issue cluster to inspect actual ontology edges across runs, interventions, and outcomes. Semantic proximity is used only when direct lesson linkage is missing.</p>
    <label class='selector-label'>Focus issue <select id='knowledge-focus' class='selector'>{options}</select></label>
    <div class='muted' style='margin-top:10px;'>{legend}</div>
    <div class='grid cards-2' style='margin-top:14px;align-items:start;'>
      <div class='panel' style='padding:12px;'>
        <div id='knowledge-network-root'></div>
      </div>
      <div class='grid' style='gap:14px;'>
        <div class='panel' id='knowledge-focus-summary'></div>
        <div class='panel' id='knowledge-focus-lessons'></div>
      </div>
    </div>
  </div>
</section>
<script type='application/json' id='knowledge-relation-data'>{payload}</script>
<script>
  const KNOWLEDGE_RELATION_VIEWS = JSON.parse(document.getElementById('knowledge-relation-data').textContent || '{{}}');
  const KNOWLEDGE_COLORS = {json.dumps(CATEGORY_COLORS)};

  function escapeHtml(value) {{
    return String(value ?? '').replace(/[&<>"']/g, (ch) => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
  }}

  function truncateLabel(value, limit) {{
    const text = String(value ?? '');
    return text.length > limit ? text.slice(0, limit - 1) + '…' : text;
  }}

  function layoutY(index, total, height, topPad, bottomPad) {{
    if (total <= 1) return Math.round(height / 2);
    const usable = Math.max(40, height - topPad - bottomPad);
    return Math.round(topPad + (usable * index) / (total - 1));
  }}

  function nodeCircle(cx, cy, radius, fill, label, fullText) {{
    const safeLabel = escapeHtml(label);
    const safeFull = escapeHtml(fullText);
    return `
      <g>
        <title>${{safeFull}}</title>
        <circle cx="${{cx}}" cy="${{cy}}" r="${{radius}}" fill="${{fill}}" stroke="#ffffff" stroke-width="2"></circle>
        <text x="${{cx}}" y="${{cy + 4}}" text-anchor="middle" font-size="11" fill="#ffffff" font-weight="700">${{safeLabel}}</text>
      </g>`;
  }}

  function nodeText(x, y, text, anchor) {{
    return `<text x="${{x}}" y="${{y}}" text-anchor="${{anchor}}" font-size="12" fill="#24364d">${{escapeHtml(text)}}</text>`;
  }}

  function renderKnowledgeFocus(key) {{
    const root = document.getElementById('knowledge-network-root');
    const summaryRoot = document.getElementById('knowledge-focus-summary');
    const lessonsRoot = document.getElementById('knowledge-focus-lessons');
    const view = KNOWLEDGE_RELATION_VIEWS[key];
    if (!root || !summaryRoot || !lessonsRoot || !view) return;

    const runs = Array.isArray(view.runs) ? view.runs : [];
    const lessons = Array.isArray(view.lessons) ? view.lessons : [];
    const interventions = Array.isArray(view.interventions) ? view.interventions : [];
    const outcomes = Array.isArray(view.outcomes) ? view.outcomes : [];
    const height = Math.max(380, 220 + Math.max(runs.length, interventions.length, outcomes.length) * 38);
    const width = 1120;
    const issueX = 380;
    const issueY = Math.round(height / 2);
    const runX = 150;
    const interventionX = 700;
    const outcomeX = 980;

    let lines = '';
    let nodes = '';
    let labels = '';

    runs.forEach((run, idx) => {{
      const y = layoutY(idx, runs.length, height, 60, 60);
      lines += `<line x1="${{runX + 18}}" y1="${{y}}" x2="${{issueX - 42}}" y2="${{issueY}}" stroke="#cbd5e1" stroke-width="2"></line>`;
      nodes += nodeCircle(runX, y, 18, KNOWLEDGE_COLORS.run, truncateLabel(run.label || run.id || 'run', 7), run.label || run.id || 'run');
      labels += nodeText(runX - 30, y + 4, run.label || run.id || 'run', 'end');
    }});

    interventions.forEach((intervention, idx) => {{
      const y = layoutY(idx, interventions.length, height, 44, 44);
      const role = String(intervention.role || 'other');
      const color = role === 'planner' ? KNOWLEDGE_COLORS.intervention_planner : KNOWLEDGE_COLORS.intervention_detector;
      lines += `<line x1="${{issueX + 42}}" y1="${{issueY}}" x2="${{interventionX - 18}}" y2="${{y}}" stroke="#d7dee9" stroke-width="2"></line>`;
      nodes += nodeCircle(interventionX, y, 16, color, truncateLabel(role.toUpperCase(), 6), intervention.label || role);
      labels += nodeText(interventionX + 26, y + 4, truncateLabel(intervention.label || '-', 44), 'start');
    }});

    outcomes.forEach((outcome, idx) => {{
      const y = layoutY(idx, outcomes.length, height, 68, 68);
      nodes += nodeCircle(outcomeX, y, 15, KNOWLEDGE_COLORS.outcome, truncateLabel(String(outcome.severity || 'out').toUpperCase(), 6), outcome.label || 'outcome');
      labels += nodeText(outcomeX + 26, y + 4, truncateLabel(outcome.label || '-', 38), 'start');
    }});

    interventions.forEach((intervention, idx) => {{
      const y = layoutY(idx, interventions.length, height, 44, 44);
      const linkedOutcomes = Array.isArray(intervention.outcomes) ? intervention.outcomes : [];
      linkedOutcomes.forEach((outcomeId) => {{
        const targetIndex = outcomes.findIndex((item) => String(item.id || '') === String(outcomeId || ''));
        if (targetIndex < 0) return;
        const sy = layoutY(targetIndex, outcomes.length, height, 68, 68);
        lines += `<line x1="${{interventionX + 18}}" y1="${{y}}" x2="${{outcomeX - 15}}" y2="${{sy}}" stroke="#d7dee9" stroke-dasharray="4 4" stroke-width="1.5"></line>`;
      }});
    }});

    nodes += nodeCircle(issueX, issueY, 34, KNOWLEDGE_COLORS.issue, 'ISSUE', view.label || 'Issue');
    labels += nodeText(issueX, issueY + 62, truncateLabel(view.label || 'Issue', 72), 'middle');

    root.innerHTML = `
      <svg viewBox="0 0 ${{width}} ${{height}}" style="width:100%;height:auto;background:#fbfdff;border:1px solid #d6deea;border-radius:14px;">
        <defs>
          <filter id="softShadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="#94a3b8" flood-opacity="0.18"></feDropShadow>
          </filter>
        </defs>
        <rect x="0" y="0" width="${{width}}" height="${{height}}" fill="#fbfdff"></rect>
        <g filter="url(#softShadow)">${{lines}}${{nodes}}</g>
        <g>${{labels}}</g>
      </svg>`;

    const metrics = view.metrics || {{}};
    const lastSeen = Number(metrics.last_seen_run || 0) > 0 ? `run_${{String(metrics.last_seen_run).padStart(2, '0')}}` : '-';
    const runsList = runs.map((run) => `<li>${{escapeHtml(run.label || run.id || 'run')}}</li>`).join('') || '<li>-</li>';
    summaryRoot.innerHTML = `
      <h2>Selected Issue Cluster</h2>
      <p><strong>${{escapeHtml(view.label || '-')}}</strong></p>
      <table>
        <tbody>
          <tr><th>Runs seen</th><td>${{escapeHtml(String(metrics.run_count || 0))}}</td></tr>
          <tr><th>Recurrence</th><td>${{escapeHtml(String(metrics.recurrence || 0))}}</td></tr>
          <tr><th>Last seen</th><td>${{escapeHtml(lastSeen)}}</td></tr>
          <tr><th>Cluster members</th><td>${{escapeHtml(String(metrics.member_count || 0))}}</td></tr>
          <tr><th>Relation source</th><td>${{escapeHtml(String(metrics.edge_mode || '-'))}}</td></tr>
        </tbody>
      </table>
      <h3 style="margin-top:14px;">Observed in runs</h3>
      <ul class="clean">${{runsList}}</ul>`;

    const interventionRows = interventions.map((intervention) => {{
      const linkedOutcomes = (Array.isArray(intervention.outcomes) ? intervention.outcomes : [])
        .map((outcomeId) => {{
          const found = outcomes.find((row) => String(row.id || '') === String(outcomeId || ''));
          return found ? String(found.label || outcomeId) : String(outcomeId || '');
        }})
        .filter(Boolean)
        .join(', ') || '-';
      return `
      <tr>
        <td>${{escapeHtml(intervention.role || '-')}}</td>
        <td>${{escapeHtml(intervention.label || '-')}}</td>
        <td>${{escapeHtml(linkedOutcomes)}}</td>
        <td>${{escapeHtml((intervention.runs_seen || []).join(', ') || '-')}}</td>
      </tr>`;
    }}).join('') || '<tr><td colspan="4">No intervention edges linked to this issue.</td></tr>';

    const lessonRows = lessons.map((lesson) => `
      <tr>
        <td>${{escapeHtml(lesson.category_label || lesson.category || '-')}}</td>
        <td>${{escapeHtml(lesson.label || '-')}}</td>
        <td>${{escapeHtml(String(lesson.score ?? '-'))}}</td>
        <td>${{escapeHtml(String(lesson.run_count || 0))}}</td>
      </tr>`).join('') || '<tr><td colspan="4">No related lessons/guidance.</td></tr>';
    lessonsRoot.innerHTML = `
      <h2>Interventions / Lessons</h2>
      <p class="muted">Issue-to-intervention and intervention-to-outcome paths come from actual ontology edges. Lesson rows remain available as supporting guidance context.</p>
      <h3>Intervention Targets</h3>
      <table>
        <thead><tr><th>Role</th><th>Intervention</th><th>Target Outcomes</th><th>Runs</th></tr></thead>
        <tbody>${{interventionRows}}</tbody>
      </table>
      <h3 style="margin-top:14px;">Related Lessons / Guidance</h3>
      <table>
        <thead><tr><th>Category</th><th>Lesson Cluster</th><th>Similarity</th><th>Runs</th></tr></thead>
        <tbody>${{lessonRows}}</tbody>
      </table>`;
  }}

  const focusSelect = document.getElementById('knowledge-focus');
  if (focusSelect) {{
    focusSelect.addEventListener('change', (event) => renderKnowledgeFocus(event.target.value));
    renderKnowledgeFocus(focusSelect.value || {json.dumps(first_key)});
  }}
</script>
"""


def _plotly_fragments(issue_clusters: list[dict[str, Any]]) -> str:
    try:
        import plotly.graph_objects as go
    except Exception:
        return ""
    top = issue_clusters[:8]
    if not top:
        return ""
    fig = go.Figure(
        data=[
            go.Bar(
                x=[str(cluster.get("label", "-"))[:42] for cluster in top],
                y=[int(cluster.get("run_count", 0) or 0) for cluster in top],
                marker_color="#1d4e89",
                text=[", ".join(cluster.get("runs_seen", [])) for cluster in top],
                hovertemplate="%{x}<br>runs=%{y}<br>%{text}<extra></extra>",
            )
        ]
    )
    fig.update_layout(height=360, margin=dict(l=20, r=20, t=40, b=80), title="Recurring Issues by Runs Seen", paper_bgcolor="#ffffff", plot_bgcolor="#fbfdff")
    fig.update_xaxes(title_text="Issue cluster")
    fig.update_yaxes(title_text="Runs seen")
    return "<section class='section'><div class='panel'><h2>Recurring Issue Trend</h2>" + fig.to_html(full_html=False, include_plotlyjs=True) + "</div></section>"


def export_knowledge_dashboard(
    *,
    output_dir: Path,
    graph: dict[str, Any],
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    current_run_id: str | None = None,
    analysis: dict[str, Any] | None = None,
) -> Path:
    output_path = Path(output_dir) / "knowledge_dashboard.html"
    issue_records, lesson_records, _observed_runs = _extract_graph_records(graph)
    issue_clusters = _cluster_records(issue_records, threshold=0.24)
    lesson_clusters = _cluster_records(lesson_records, threshold=0.28)
    current_run = _find_run(manifest, current_run_id)
    node_count = len(graph.get("nodes", {}) if isinstance(graph.get("nodes", {}), dict) else {})
    edge_count = len(graph.get("edges", []) if isinstance(graph.get("edges", []), list) else [])
    cards = [
        ("Ontology Nodes", str(node_count), "Typed knowledge entities currently stored."),
        ("Ontology Edges", str(edge_count), "Relations currently stored in the graph."),
        ("Issue Clusters", str(len(issue_clusters)), "Recurring issue themes after UI-side semantic grouping."),
        ("Lesson Clusters", str(len(lesson_clusters)), "Carry-forward, detector, planner, and watchout themes."),
    ]
    cards_html = "<section class='section'><div class='grid cards-4'>" + "".join(
        f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div><div class='sub'>{html.escape(sub)}</div></div>"
        for label, value, sub in cards
    ) + "</div></section>"
    body = (
        cards_html
        + _plotly_fragments(issue_clusters)
        + "<section class='section'><div class='panel'><h2>Recurring Issues</h2>" + _issue_table_html(issue_clusters) + "</div></section>"
        + _lesson_panels(lesson_clusters)
        + _intervention_table(manifest)
        + _relation_graph_html(issue_clusters, lesson_clusters, graph)
        + _run_diff_html(manifest, current_run_id)
    )
    subtitle = "Ontology-focused view of recurring issues, lesson evolution, guidance clusters, and run-to-run knowledge drift. The default operating screen is hybrid; network is used only as selected-node drill-down."
    if isinstance(current_run, dict):
        subtitle = f"Ontology view focused on {str(current_run.get('label', current_run_id or 'current run'))}. Tables and trend summaries remain the primary operating view, while the network section is used for selected-node drill-down."
    html_text = render_page_shell(
        title="ManSim Knowledge Dashboard",
        current_page_path=output_path,
        manifest=manifest,
        manifest_path=manifest_path,
        current_artifact="knowledge_dashboard.html",
        current_run_id=current_run_id,
        page_title="Knowledge Dashboard",
        page_subtitle=subtitle,
        body_html=body,
    )
    output_path.write_text(html_text, encoding="utf-8")
    return output_path
