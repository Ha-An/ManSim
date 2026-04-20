from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from typing import Any

from .manifest import load_knowledge_sections
from .shell import render_page_shell


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


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


def _slug(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _load_run_reflection(path_str: str) -> dict[str, Any]:
    path = Path(str(path_str).strip())
    payload = _safe_json(path)
    return payload if isinstance(payload, dict) else {}


def _shared_ratio(left: list[str], right: list[str]) -> float:
    left_norm = {_slug(item) for item in left if _slug(item)}
    right_norm = {_slug(item) for item in right if _slug(item)}
    if not left_norm or not right_norm:
        return 0.0
    union = left_norm | right_norm
    if not union:
        return 0.0
    return float(len(left_norm & right_norm)) / float(len(union))


def build_series_analysis(*, parent_output_dir: Path, summary_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    summary_blob: Any = summary_payload
    if not isinstance(summary_blob, dict):
        summary_path = parent_output_dir / "run_series_summary.json"
        summary_blob = _safe_json(summary_path)
    if not isinstance(summary_blob, dict):
        return {
            "knowledge_effect_classification": "mixed",
            "performance_pattern": "insufficient_data",
            "analysis_summary": "run_series_summary.json is missing or invalid.",
            "runs": [],
            "peak_run": {},
            "worst_run": {},
            "baseline_vs_best": {},
            "baseline_vs_final": {},
            "lesson_stability": {},
            "knowledge_sections": load_knowledge_sections(parent_output_dir),
        }

    runs_raw = summary_blob.get("runs", []) if isinstance(summary_blob.get("runs", []), list) else []
    enriched_runs: list[dict[str, Any]] = []
    for row in runs_raw:
        if not isinstance(row, dict):
            continue
        output_dir = Path(str(row.get("output_dir", "")).strip()) if str(row.get("output_dir", "")).strip() else None
        kpi_path = Path(str(row.get("kpi_path", "")).strip()) if str(row.get("kpi_path", "")).strip() else (output_dir / "kpi.json" if output_dir else None)
        daily_path = output_dir / "daily_summary.json" if output_dir else None
        reflection = _load_run_reflection(str(row.get("run_reflection_path", "")).strip())
        kpi = _safe_json(kpi_path) if isinstance(kpi_path, Path) else None
        kpi = kpi if isinstance(kpi, dict) else {}
        daily_payload = _safe_json(daily_path) if isinstance(daily_path, Path) else None
        daily_rows = daily_payload.get("days", []) if isinstance(daily_payload, dict) and isinstance(daily_payload.get("days", []), list) else []
        last_day = daily_rows[-1] if daily_rows else {}
        carry_forward = reflection.get("carry_forward_lessons", []) if isinstance(reflection.get("carry_forward_lessons", []), list) else []
        enriched_runs.append(
            {
                "run_index": _safe_int(row.get("run_index"), 0),
                "id": f"run_{_safe_int(row.get('run_index'), 0):02d}",
                "output_dir": str(row.get("output_dir", "")).strip(),
                "total_products": _safe_int(kpi.get("total_products"), _safe_int(row.get("total_products"), 0)),
                "downstream_closure_ratio": _safe_float(kpi.get("downstream_closure_ratio"), _safe_float(row.get("downstream_closure_ratio"), 0.0)),
                "wall_clock_sec": _safe_float(kpi.get("wall_clock_sec"), _safe_float(row.get("wall_clock_sec"), 0.0)),
                "physical_incident_total": _safe_int(kpi.get("physical_incident_total"), 0),
                "coordination_incident_total": _safe_int(kpi.get("coordination_incident_total"), 0),
                "unique_replan_blocker_total": _safe_int(kpi.get("unique_replan_blocker_total"), 0),
                "planner_escalation_total": _safe_int(kpi.get("planner_escalation_total"), 0),
                "commitment_dispatch_total": _safe_int(kpi.get("commitment_dispatch_total"), 0),
                "completed_product_lead_time_avg_min": _safe_float(kpi.get("completed_product_lead_time_avg_min"), 0.0),
                "product_input_wait_avg_min": _safe_float((kpi.get("buffer_wait_avg_min_including_open", {}) or {}).get("product_input"), 0.0),
                "inspection_backlog_end": _safe_int(last_day.get("inspection_backlog_end"), 0) if isinstance(last_day, dict) else 0,
                "evaluator_enabled": bool(row.get("evaluator_enabled", False)),
                "kpi_path": str(row.get("kpi_path", "")).strip(),
                "run_meta_path": str(row.get("run_meta_path", "")).strip(),
                "run_reflection_path": str(row.get("run_reflection_path", "")).strip(),
                "knowledge_in_path": str(row.get("knowledge_in_path", "")).strip(),
                "knowledge_out_path": str(row.get("knowledge_out_path", "")).strip(),
                "kpi_dashboard_path": str(row.get("kpi_dashboard_path", "")).strip(),
                "replay_dashboard_path": str(row.get("replay_dashboard_path", "")).strip(),
                "knowledge_dashboard_path": str(row.get("knowledge_dashboard_path", "")).strip(),
                "reasoning_dashboard_path": str(row.get("reasoning_dashboard_path", "")).strip(),
                "results_dashboard_path": str(row.get("results_dashboard_path", "")).strip(),
                "run_reflection_markdown_path": str(row.get("run_reflection_markdown_path", "")).strip(),
                "reflection_summary": str(reflection.get("summary", "")).strip(),
                "carry_forward_lessons": [str(item).strip() for item in carry_forward if str(item).strip()],
            }
        )

    knowledge_sections = load_knowledge_sections(parent_output_dir)
    if not enriched_runs:
        return {
            "knowledge_effect_classification": "mixed",
            "performance_pattern": "insufficient_data",
            "analysis_summary": "No completed child runs were recorded.",
            "runs": [],
            "peak_run": {},
            "worst_run": {},
            "baseline_vs_best": {},
            "baseline_vs_final": {},
            "lesson_stability": {},
            "knowledge_sections": knowledge_sections,
        }

    baseline = enriched_runs[0]
    final_run = enriched_runs[-1]
    peak_run = max(enriched_runs, key=lambda row: (float(row["downstream_closure_ratio"]), int(row["total_products"]), -int(row["run_index"])))
    worst_run = min(enriched_runs, key=lambda row: (float(row["downstream_closure_ratio"]), int(row["total_products"]), -int(row["run_index"])))

    positive_signal = any(
        (row["downstream_closure_ratio"] >= baseline["downstream_closure_ratio"] + 0.03)
        or (row["total_products"] >= baseline["total_products"] + 2)
        for row in enriched_runs[1:]
    )
    negative_signal = (
        final_run["downstream_closure_ratio"] <= peak_run["downstream_closure_ratio"] - 0.08
        or final_run["total_products"] <= peak_run["total_products"] - 3
        or final_run["downstream_closure_ratio"] <= baseline["downstream_closure_ratio"] - 0.08
        or final_run["total_products"] <= baseline["total_products"] - 3
    )
    if positive_signal and negative_signal:
        effect = "mixed"
    elif positive_signal:
        effect = "positive"
    else:
        effect = "negative"

    best_index = int(peak_run["run_index"])
    if effect == "positive":
        performance_pattern = "steady_improvement"
    elif positive_signal and negative_signal and best_index <= max(2, len(enriched_runs) - 1):
        performance_pattern = "early_improvement_then_regression"
    elif positive_signal and negative_signal:
        performance_pattern = "volatile"
    else:
        performance_pattern = "regression"

    consecutive_overlap: list[dict[str, Any]] = []
    for left, right in zip(enriched_runs, enriched_runs[1:]):
        shared_ratio = _shared_ratio(left["carry_forward_lessons"], right["carry_forward_lessons"])
        consecutive_overlap.append({"from_run": int(left["run_index"]), "to_run": int(right["run_index"]), "shared_ratio": round(shared_ratio, 6)})

    persistent = knowledge_sections.get("persistent_lessons", [])
    persistent_alignment: list[dict[str, Any]] = []
    for row in enriched_runs:
        shared_ratio = _shared_ratio(row["carry_forward_lessons"], persistent)
        persistent_alignment.append({"run_index": int(row["run_index"]), "shared_ratio": round(shared_ratio, 6)})

    analysis_summary = (
        f"Knowledge impact is {effect}: peak performance arrived at run {int(peak_run['run_index'])}, while the final run finished at {int(final_run['total_products'])} products and {float(final_run['downstream_closure_ratio']):.3f} closure."
        if effect == "mixed"
        else (
            "Knowledge impact is positive: the final run preserved stronger performance than baseline."
            if effect == "positive"
            else "Knowledge impact is negative: later runs did not preserve or improve baseline performance."
        )
    )

    return {
        "knowledge_effect_classification": effect,
        "performance_pattern": performance_pattern,
        "analysis_summary": analysis_summary,
        "requested_run_count": _safe_int(summary_blob.get("requested_run_count"), len(enriched_runs)),
        "completed_run_count": _safe_int(summary_blob.get("completed_run_count"), len(enriched_runs)),
        "runs": enriched_runs,
        "peak_run": {"run_index": int(peak_run["run_index"]), "total_products": int(peak_run["total_products"]), "downstream_closure_ratio": round(float(peak_run["downstream_closure_ratio"]), 6)},
        "worst_run": {"run_index": int(worst_run["run_index"]), "total_products": int(worst_run["total_products"]), "downstream_closure_ratio": round(float(worst_run["downstream_closure_ratio"]), 6)},
        "baseline_vs_best": {"from_run": int(baseline["run_index"]), "to_run": int(peak_run["run_index"]), "products_delta": int(peak_run["total_products"]) - int(baseline["total_products"]), "closure_delta": round(float(peak_run["downstream_closure_ratio"]) - float(baseline["downstream_closure_ratio"]), 6)},
        "baseline_vs_final": {"from_run": int(baseline["run_index"]), "to_run": int(final_run["run_index"]), "products_delta": int(final_run["total_products"]) - int(baseline["total_products"]), "closure_delta": round(float(final_run["downstream_closure_ratio"]) - float(baseline["downstream_closure_ratio"]), 6)},
        "lesson_stability": {"consecutive_overlap": consecutive_overlap, "persistent_alignment": persistent_alignment},
        "knowledge_sections": knowledge_sections,
    }


def _metric_cards_html(analysis: dict[str, Any]) -> str:
    peak = analysis.get("peak_run", {}) if isinstance(analysis.get("peak_run", {}), dict) else {}
    worst = analysis.get("worst_run", {}) if isinstance(analysis.get("worst_run", {}), dict) else {}
    best_delta = analysis.get("baseline_vs_best", {}) if isinstance(analysis.get("baseline_vs_best", {}), dict) else {}
    final_delta = analysis.get("baseline_vs_final", {}) if isinstance(analysis.get("baseline_vs_final", {}), dict) else {}
    cards = [
        ("Classification", str(analysis.get("knowledge_effect_classification", "mixed")), "Net reading of cross-run knowledge impact."),
        ("Pattern", str(analysis.get("performance_pattern", "volatile")), "Whether the series improved steadily or regressed after an early gain."),
        ("Peak Run", f"run_{int(peak.get('run_index', 0) or 0):02d}", "Best closure-focused run in the series."),
        ("Worst Run", f"run_{int(worst.get('run_index', 0) or 0):02d}", "Lowest closure-focused run in the series."),
        ("Baseline to Best", f"{int(best_delta.get('products_delta', 0) or 0):+d} products / {float(best_delta.get('closure_delta', 0.0) or 0.0):+.3f} closure", "Best improvement relative to the first run."),
        ("Baseline to Final", f"{int(final_delta.get('products_delta', 0) or 0):+d} products / {float(final_delta.get('closure_delta', 0.0) or 0.0):+.3f} closure", "Net change relative to the first run."),
    ]
    return "<section class='section'><div class='grid cards-3'>" + "".join(
        f"<div class='card'><div class='label'>{escape(label)}</div><div class='value'>{escape(value)}</div><div class='sub'>{escape(sub)}</div></div>"
        for label, value, sub in cards
    ) + "</div></section>"


def _run_rows_html(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "<tr><td colspan='11'>No runs</td></tr>"
    baseline_products = int(runs[0].get("total_products", 0) or 0)
    baseline_closure = float(runs[0].get("downstream_closure_ratio", 0.0) or 0.0)
    rows_html: list[str] = []
    for row in runs:
        products = int(row.get("total_products", 0) or 0)
        closure = float(row.get("downstream_closure_ratio", 0.0) or 0.0)
        dispatch = int(row.get("commitment_dispatch_total", 0) or 0)
        blockers = int(row.get("unique_replan_blocker_total", 0) or 0)
        escalations = int(row.get("planner_escalation_total", 0) or 0)
        backlog = int(row.get("inspection_backlog_end", 0) or 0)
        run_dir = Path(str(row.get("output_dir", "")).strip()) if str(row.get("output_dir", "")).strip() else None
        results_path = str((run_dir / "results_dashboard.html").resolve()) if run_dir else ""
        rows_html.append(
            "<tr>"
            f"<td>{escape(str(row.get('id', '-')))}</td>"
            f"<td>{products}</td>"
            f"<td>{closure:.3f}</td>"
            f"<td>{dispatch}</td>"
            f"<td>{blockers}</td>"
            f"<td>{escalations}</td>"
            f"<td>{backlog}</td>"
            f"<td>{float(row.get('completed_product_lead_time_avg_min', 0.0) or 0.0):.1f}m</td>"
            f"<td>{products - baseline_products:+d}</td>"
            f"<td>{closure - baseline_closure:+.3f}</td>"
            f"<td><a href='{escape(results_path)}'>Run Hub</a></td>"
            "</tr>"
        )
    return "".join(rows_html)


def _knowledge_panels(analysis: dict[str, Any]) -> str:
    sections = analysis.get("knowledge_sections", {}) if isinstance(analysis.get("knowledge_sections", {}), dict) else {}
    mapping = [
        ("persistent_lessons", "Persistent Lessons"),
        ("latest_lessons", "Latest Lessons"),
        ("detector_guidance", "Detector Guidance"),
        ("planner_guidance", "Planner Guidance"),
        ("open_watchouts", "Open Watchouts"),
    ]
    boxes: list[str] = []
    for key, title in mapping:
        items = sections.get(key, []) if isinstance(sections.get(key, []), list) else []
        body = "".join(f"<li>{escape(str(item))}</li>" for item in items[:6]) or "<li>-</li>"
        boxes.append(f"<div class='panel'><h2>{escape(title)}</h2><ul class='clean'>{body}</ul></div>")
    return "<section class='section'><div class='grid cards-3'>" + "".join(boxes) + "</div></section>"


def _learning_table(analysis: dict[str, Any]) -> str:
    stability = analysis.get("lesson_stability", {}) if isinstance(analysis.get("lesson_stability", {}), dict) else {}
    overlap = stability.get("consecutive_overlap", []) if isinstance(stability.get("consecutive_overlap", []), list) else []
    alignment = stability.get("persistent_alignment", []) if isinstance(stability.get("persistent_alignment", []), list) else []
    left_rows = "".join(
        f"<tr><td>run_{int(row.get('from_run', 0) or 0):02d} to run_{int(row.get('to_run', 0) or 0):02d}</td><td>{float(row.get('shared_ratio', 0.0) or 0.0):.3f}</td></tr>"
        for row in overlap
    ) or "<tr><td colspan='2'>-</td></tr>"
    right_rows = "".join(
        f"<tr><td>run_{int(row.get('run_index', 0) or 0):02d}</td><td>{float(row.get('shared_ratio', 0.0) or 0.0):.3f}</td></tr>"
        for row in alignment
    ) or "<tr><td colspan='2'>-</td></tr>"
    return (
        "<section class='section'><div class='grid cards-3'>"
        "<div class='panel'><h2>Carry-Forward Stability</h2><table><thead><tr><th>Transition</th><th>Shared Ratio</th></tr></thead><tbody>" + left_rows + "</tbody></table></div>"
        "<div class='panel'><h2>Alignment With Persistent Lessons</h2><table><thead><tr><th>Run</th><th>Shared Ratio</th></tr></thead><tbody>" + right_rows + "</tbody></table></div>"
        f"<div class='panel'><h2>Series Interpretation</h2><p>{escape(str(analysis.get('analysis_summary', '-')))}</p></div>"
        "</div></section>"
    )


def export_series_dashboard(
    *,
    parent_output_dir: Path,
    analysis: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
) -> Path | None:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return None

    analysis_payload = analysis if isinstance(analysis, dict) else build_series_analysis(parent_output_dir=parent_output_dir)
    runs = analysis_payload.get("runs", []) if isinstance(analysis_payload.get("runs", []), list) else []
    if not runs:
        return None

    parent_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = parent_output_dir / "series_dashboard.html"
    run_labels = [str(row.get("id", "-")) for row in runs]
    products = [int(row.get("total_products", 0) or 0) for row in runs]
    closures = [float(row.get("downstream_closure_ratio", 0.0) or 0.0) for row in runs]
    incidents = [int(row.get("coordination_incident_total", 0) or 0) for row in runs]
    blockers = [int(row.get("unique_replan_blocker_total", 0) or 0) for row in runs]
    dispatch = [int(row.get("commitment_dispatch_total", 0) or 0) for row in runs]
    lead_time = [float(row.get("completed_product_lead_time_avg_min", 0.0) or 0.0) for row in runs]
    backlog = [int(row.get("inspection_backlog_end", 0) or 0) for row in runs]

    fig = make_subplots(
        rows=3,
        cols=1,
        subplot_titles=(
            "Performance: Products and Closure",
            "Operational Stability: Coordination / Blockers / Dispatch",
            "Knowledge Pressure: Lead Time and Ending Backlog",
        ),
        specs=[[{"secondary_y": True}], [{"secondary_y": True}], [{"secondary_y": True}]],
        vertical_spacing=0.11,
    )
    fig.add_trace(go.Bar(name="Products", x=run_labels, y=products, marker_color="#1d4e89"), row=1, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(name="Closure", x=run_labels, y=closures, mode="lines+markers", line=dict(color="#e76f51", width=3)), row=1, col=1, secondary_y=True)
    fig.add_trace(go.Bar(name="Coordination Incidents", x=run_labels, y=incidents, marker_color="#c0392b"), row=2, col=1, secondary_y=False)
    fig.add_trace(go.Bar(name="Unique Blockers", x=run_labels, y=blockers, marker_color="#f39c12"), row=2, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(name="Dispatch", x=run_labels, y=dispatch, mode="lines+markers", line=dict(color="#0f8c5b", width=3)), row=2, col=1, secondary_y=True)
    fig.add_trace(go.Bar(name="Lead Time", x=run_labels, y=lead_time, marker_color="#5b7cfa"), row=3, col=1, secondary_y=False)
    fig.add_trace(go.Scatter(name="Backlog End", x=run_labels, y=backlog, mode="lines+markers", line=dict(color="#8e44ad", width=3)), row=3, col=1, secondary_y=True)
    fig.update_layout(height=1180, margin=dict(l=40, r=40, t=110, b=60), legend=dict(orientation="h", x=0.5, xanchor="center", y=1.08), paper_bgcolor="#ffffff", plot_bgcolor="#fbfdff")
    fig.update_yaxes(title_text="Products", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Closure", tickformat=".0%", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Incidents / Blockers", row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Dispatch", row=2, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Lead Time (min)", row=3, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Backlog End", row=3, col=1, secondary_y=True)
    fig.update_xaxes(title_text="Run", row=3, col=1)

    body = (
        _metric_cards_html(analysis_payload)
        + "<section class='section'><div class='panel'><h2>Series Change Overview</h2>" + fig.to_html(full_html=False, include_plotlyjs=True) + "</div></section>"
        + "<section class='section'><div class='panel'><h2>Run Comparison</h2><table><thead><tr><th>Run</th><th>Products</th><th>Closure</th><th>Dispatch</th><th>Blockers</th><th>Escalations</th><th>Ending Backlog</th><th>Lead Time</th><th>Delta Products vs Baseline</th><th>Delta Closure vs Baseline</th><th>Go To</th></tr></thead><tbody>" + _run_rows_html(runs) + "</tbody></table></div></section>"
        + _learning_table(analysis_payload)
        + _knowledge_panels(analysis_payload)
    )
    html_text = render_page_shell(
        title="ManSim Series Dashboard",
        current_page_path=output_path,
        manifest=manifest,
        manifest_path=manifest_path,
        current_artifact="series_dashboard.html",
        current_run_id=str(manifest.get("current_run", "")) if isinstance(manifest, dict) else None,
        page_title="Series Dashboard",
        page_subtitle=str(analysis_payload.get("analysis_summary", "")).strip() or "Cross-run view of performance, operating stability, and knowledge carry-forward.",
        body_html=body,
    )
    output_path.write_text(html_text, encoding="utf-8")
    return output_path
