from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .shell import rel_href, render_page_shell


RAW_DEBUG_ARTIFACTS = [
    ("run_reflection.json", "Run Reflection JSON"),
    ("run_reflection.md", "Run Reflection Markdown"),
    ("llm_trace.html", "LLM Trace"),
    ("orchestration_intelligence_dashboard.html", "Orchestration Intelligence"),
    ("events.jsonl", "Events Log"),
]


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


def _find_run(manifest: dict[str, Any] | None, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return None
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    target = str(run_id or manifest.get("current_run", "")).strip()
    for row in runs:
        if isinstance(row, dict) and str(row.get("id", "")).strip() == target:
            return row
    return runs[-1] if runs and isinstance(runs[-1], dict) else None


def _load_json(path: Path | None) -> dict[str, Any]:
    if not isinstance(path, Path):
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_json_list(path: Path | None) -> list[dict[str, Any]]:
    if not isinstance(path, Path):
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _effective_kpi_from_run(run: dict[str, Any] | None, provided: dict[str, Any] | None) -> dict[str, Any]:
    candidate = provided if isinstance(provided, dict) else {}
    if candidate.get("worker_local_response_total") is not None and candidate.get("commitment_dispatch_total") is not None:
        return candidate
    if not isinstance(run, dict):
        return candidate
    artifacts = run.get("artifacts", {}) if isinstance(run.get("artifacts", {}), dict) else {}
    raw_path = str(artifacts.get("kpi.json", "")).strip()
    loaded = _load_json(Path(raw_path)) if raw_path else {}
    return loaded or candidate


def _summary_cards(kpi: dict[str, Any], run_meta: dict[str, Any]) -> str:
    llm_meta = run_meta.get("llm", {}) if isinstance(run_meta.get("llm", {}), dict) else {}
    transport = llm_meta.get("transport_metrics", {}) if isinstance(llm_meta.get("transport_metrics", {}), dict) else {}
    cards = [
        ("Commitment Dispatches", str(_safe_int(kpi.get("commitment_dispatch_total"))), "Authoritative work orders that reached worker execution."),
        ("Worker Local Responses", str(_safe_int(kpi.get("worker_local_response_total"))), "Local recovery actions used to absorb active incidents."),
        ("Coordination Incidents", str(_safe_int(kpi.get("coordination_incident_total"))), "Execution blockers caused by plan/coordination mismatch."),
        ("Unique Replan Blockers", str(_safe_int(kpi.get("unique_replan_blocker_total"))), "Deduplicated blocker states rather than raw spam events."),
        ("Planner Escalations", str(_safe_int(kpi.get("planner_escalation_total"))), "Incidents that escaped local response and required replanning."),
        ("LLM Calls", str(_safe_int(transport.get("total_calls"))), "Total manager-side model calls recorded in run metadata."),
    ]
    return "<section class='section'><div class='grid cards-3'>" + "".join(
        f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div><div class='sub'>{html.escape(sub)}</div></div>"
        for label, value, sub in cards
    ) + "</div></section>"


def _problem_panel(reflection: dict[str, Any]) -> str:
    summary = str(reflection.get("summary", "")).strip() or "No run reflection summary was recorded."
    problems = reflection.get("run_problems", []) if isinstance(reflection.get("run_problems", []), list) else []
    detector = reflection.get("detector_should_have_done", []) if isinstance(reflection.get("detector_should_have_done", []), list) else []
    planner = reflection.get("planner_should_have_done", []) if isinstance(reflection.get("planner_should_have_done", []), list) else []

    def _items(values: list[Any], *, dict_key: str | None = None) -> str:
        rows: list[str] = []
        for raw in values[:6]:
            if dict_key and isinstance(raw, dict):
                text = str(raw.get(dict_key, "")).strip()
            else:
                text = str(raw).strip()
            if text:
                rows.append(f"<li>{html.escape(text)}</li>")
        return "".join(rows) or "<li>-</li>"

    return (
        "<section class='section'><div class='grid cards-3'>"
        f"<div class='panel'><h2>Run Reflection</h2><p>{html.escape(summary)}</p></div>"
        f"<div class='panel'><h2>What Was Wrong</h2><ul class='clean'>{_items(problems, dict_key='issue')}</ul></div>"
        f"<div class='panel'><h2>How Manager Logic Should Change</h2><h3>Detector</h3><ul class='clean'>{_items(detector)}</ul><h3 style='margin-top:14px;'>Planner</h3><ul class='clean'>{_items(planner)}</ul></div>"
        "</div></section>"
    )


def _day_table(daily_summary: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for day in daily_summary:
        if not isinstance(day, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{_safe_int(day.get('day'))}</td>"
            f"<td>{_safe_int(day.get('products'))}</td>"
            f"<td>{_safe_int(day.get('inspection_backlog_end'))}</td>"
            f"<td>{_safe_int(day.get('incident_event_count'))}</td>"
            f"<td>{_safe_int(day.get('coordination_incident_count'))}</td>"
            f"<td>{_safe_int(day.get('unique_replan_blocker_count'))}</td>"
            f"<td>{_safe_int(day.get('planner_escalation_count'))}</td>"
            f"<td>{_safe_int(day.get('commitment_dispatch_task_count'))}</td>"
            f"<td>{_safe_int(day.get('local_response_task_count'))}</td>"
            f"<td>{_safe_int(day.get('plan_revision'))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='10'>No daily summary rows.</td></tr>")
    return "<section class='section'><div class='panel'><h2>Daily Execution Flow</h2><table><thead><tr><th>Day</th><th>Products</th><th>Backlog End</th><th>Incidents</th><th>Coordination</th><th>Blockers</th><th>Escalations</th><th>Dispatch</th><th>Local Response</th><th>Plan Rev</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table></div></section>"


def _latency_panel(run_meta: dict[str, Any]) -> str:
    llm_meta = run_meta.get("llm", {}) if isinstance(run_meta.get("llm", {}), dict) else {}
    transport = llm_meta.get("transport_metrics", {}) if isinstance(llm_meta.get("transport_metrics", {}), dict) else {}
    by_phase = transport.get("by_phase", {}) if isinstance(transport.get("by_phase", {}), dict) else {}
    rows: list[str] = []
    for phase, payload in sorted(by_phase.items()):
        if not isinstance(payload, dict):
            continue
        latency = payload.get("latency_stats_ms", {}) if isinstance(payload.get("latency_stats_ms", {}), dict) else {}
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(phase))}</td>"
            f"<td>{_safe_int(payload.get('calls'))}</td>"
            f"<td>{_safe_float(latency.get('p50_ms')):.1f}</td>"
            f"<td>{_safe_float(latency.get('p95_ms')):.1f}</td>"
            f"<td>{_safe_float(payload.get('avg_retries', payload.get('avg_attempts'))):.2f}</td>"
            f"<td>{html.escape(str(payload.get('backend_health_ok_ratio', '-')))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='6'>No transport metrics available.</td></tr>")
    mode = html.escape(str(run_meta.get("decision_mode", "-")))
    worker_scope = html.escape(str(((run_meta.get("worker_local_response", {}) or {}) if isinstance(run_meta.get("worker_local_response", {}), dict) else {}).get("scope", "-")))
    return (
        f"<section class='section'><div class='panel'><h2>Manager Transport and Execution Context</h2><p class='muted'>mode={mode} | local_response_scope={worker_scope}</p>"
        "<table><thead><tr><th>Phase</th><th>Calls</th><th>p50 ms</th><th>p95 ms</th><th>Avg Retries</th><th>Health OK Ratio</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></section>"
    )


def _raw_links(current_page_path: Path, run: dict[str, Any] | None) -> str:
    if not isinstance(run, dict):
        return ""
    artifacts = run.get("artifacts", {}) if isinstance(run.get("artifacts", {}), dict) else {}
    body = "".join(
        f"<li><a href='{html.escape(rel_href(current_page_path, artifacts.get(filename, '')))}'>{html.escape(label)}</a></li>"
        for filename, label in RAW_DEBUG_ARTIFACTS
        if str(artifacts.get(filename, "")).strip()
    )
    return f"<section class='section'><div class='panel'><h2>Secondary Debug Artifacts</h2><ul class='clean'>{body}</ul></div></section>" if body else ""


def _priority_controller_panels(output_dir: Path) -> str:
    shift_rows = _load_json_list(output_dir / "shift_policy_history.json")
    patch_rows = _load_json_list(output_dir / "incident_patches.json")
    refresh_rows = _load_json_list(output_dir / "strategy_refresh_events.json")
    day_rows = _load_json_list(output_dir / "day_summary_memory.json")

    sections: list[str] = []
    if shift_rows:
        latest = shift_rows[-1]
        roles = latest.get("worker_roles", {}) if isinstance(latest.get("worker_roles", {}), dict) else {}
        weights = latest.get("task_priority_weights", {}) if isinstance(latest.get("task_priority_weights", {}), dict) else {}
        role_lines = "".join(
            f"<li><strong>{html.escape(str(worker))}</strong>: {html.escape(str(role or '-'))}</li>"
            for worker, role in sorted(roles.items())
        ) or "<li>-</li>"
        top_weights = sorted(
            ((str(key), _safe_float(val)) for key, val in weights.items()),
            key=lambda item: item[1],
            reverse=True,
        )[:6]
        weight_rows = "".join(
            f"<tr><td>{html.escape(key)}</td><td>{value:.3f}</td></tr>"
            for key, value in top_weights
        ) or "<tr><td colspan='2'>-</td></tr>"
        sections.append(
            "<section class='section'><div class='grid cards-2'>"
            f"<div class='panel'><h2>Latest Shift Policy</h2><p>{html.escape(str(latest.get('summary', '')).strip() or 'No summary.')}</p><h3>Worker Roles</h3><ul class='clean'>{role_lines}</ul></div>"
            f"<div class='panel'><h2>Top Priority Biases</h2><table><thead><tr><th>Task Family</th><th>Weight</th></tr></thead><tbody>{weight_rows}</tbody></table></div>"
            "</div></section>"
        )
    if patch_rows:
        rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(row.get('event_type', '-')))}</td>"
            f"<td>{_safe_float(row.get('time_min')):.1f}</td>"
            f"<td>{html.escape(str(row.get('summary', '')).strip() or '-')}</td>"
            f"<td>{html.escape(', '.join(str(key) for key in sorted((row.get('priority_updates', {}) if isinstance(row.get('priority_updates', {}), dict) else {}).keys())[:4]) or '-')}</td>"
            f"<td>{'yes' if bool(row.get('refresh_recommended', False)) else 'no'}</td>"
            "</tr>"
            for row in patch_rows[-12:]
        )
        sections.append(
            "<section class='section'><div class='panel'><h2>Incident Patches</h2><table><thead><tr><th>Event</th><th>Time</th><th>Summary</th><th>Touched Priorities</th><th>Refresh</th></tr></thead><tbody>"
            + rows
            + "</tbody></table></div></section>"
        )
    if refresh_rows:
        rows = "".join(
            "<tr>"
            f"<td>{_safe_float(row.get('time_min')):.1f}</td>"
            f"<td>{html.escape(str(row.get('event_type', '-')))}</td>"
            f"<td>{_safe_int(row.get('new_plan_revision'))}</td>"
            f"<td>{html.escape(str((row.get('refresh_context', {}) if isinstance(row.get('refresh_context', {}), dict) else {}).get('patch_summary', '-')))}</td>"
            "</tr>"
            for row in refresh_rows[-12:]
        )
        sections.append(
            "<section class='section'><div class='panel'><h2>Strategy Refresh Events</h2><table><thead><tr><th>Time</th><th>Trigger</th><th>New Revision</th><th>Patch Summary</th></tr></thead><tbody>"
            + rows
            + "</tbody></table></div></section>"
        )
    if day_rows:
        rows = "".join(
            "<tr>"
            f"<td>{_safe_int(row.get('day'))}</td>"
            f"<td>{html.escape('; '.join(str(v) for v in row.get('what_improved', [])[:2]) if isinstance(row.get('what_improved', []), list) else '-')}</td>"
            f"<td>{html.escape('; '.join(str(v) for v in row.get('carry_forward_risks', [])[:2]) if isinstance(row.get('carry_forward_risks', []), list) else '-')}</td>"
            f"<td>{html.escape(', '.join(str(v) for v in row.get('priority_bias_candidates', [])[:4]) if isinstance(row.get('priority_bias_candidates', []), list) else '-')}</td>"
            "</tr>"
            for row in day_rows[-10:]
        )
        sections.append(
            "<section class='section'><div class='panel'><h2>Deterministic Day Summaries</h2><table><thead><tr><th>Day</th><th>Improved</th><th>Carry Forward Risks</th><th>Bias Candidates</th></tr></thead><tbody>"
            + rows
            + "</tbody></table></div></section>"
        )
    return "".join(sections)


def export_reasoning_dashboard(
    *,
    output_dir: Path,
    summary: dict[str, Any] | None = None,
    links: dict[str, str] | None = None,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    current_run_id: str | None = None,
    kpi: dict[str, Any] | None = None,
    daily_summary: list[dict[str, Any]] | None = None,
    reflection: dict[str, Any] | None = None,
    run_meta: dict[str, Any] | None = None,
) -> Path:
    output_path = Path(output_dir) / "reasoning_dashboard.html"
    current_run = _find_run(manifest, current_run_id)
    effective_kpi = _effective_kpi_from_run(current_run, kpi if isinstance(kpi, dict) else None)
    effective_daily = daily_summary if isinstance(daily_summary, list) else (current_run.get("daily", {}).get("rows", []) if isinstance(current_run, dict) and isinstance(current_run.get("daily", {}), dict) else [])
    effective_reflection = reflection if isinstance(reflection, dict) else (current_run.get("reflection", {}) if isinstance(current_run, dict) and isinstance(current_run.get("reflection", {}), dict) else {})
    effective_run_meta = run_meta if isinstance(run_meta, dict) else (current_run.get("run_meta", {}) if isinstance(current_run, dict) and isinstance(current_run.get("run_meta", {}), dict) else {})
    body = (
        _summary_cards(effective_kpi, effective_run_meta)
        + _priority_controller_panels(output_path.parent)
        + _problem_panel(effective_reflection)
        + _day_table(effective_daily)
        + _latency_panel(effective_run_meta)
        + _raw_links(output_path, current_run)
    )
    subtitle = "What detector/planner/worker logic concluded, what actually executed, and where coordination broke down."
    html_text = render_page_shell(
        title="ManSim Reasoning Dashboard",
        current_page_path=output_path,
        manifest=manifest,
        manifest_path=manifest_path,
        current_artifact="reasoning_dashboard.html",
        current_run_id=current_run_id,
        page_title="Reasoning Dashboard",
        page_subtitle=subtitle,
        body_html=body,
    )
    output_path.write_text(html_text, encoding="utf-8")
    return output_path
