from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .shell import build_replay_app_url, rel_href, render_page_shell


METRIC_SPECS = [
    ("Products", "total_products", True, "units"),
    ("Closure", "downstream_closure_ratio", True, "ratio"),
    ("Lead Time", "completed_product_lead_time_avg_min", False, "minutes"),
    ("Product Input Wait", "product_input_wait_avg_min", False, "minutes"),
    ("Physical Incidents", "physical_incident_total", False, "count"),
    ("Coordination Incidents", "coordination_incident_total", False, "count"),
    ("Unique Blockers", "unique_replan_blocker_total", False, "count"),
    ("Planner Escalations", "planner_escalation_total", False, "count"),
    ("Commitment Dispatches", "commitment_dispatch_total", True, "count"),
    ("Machine Broken Ratio", "machine_broken_ratio", False, "ratio"),
    ("Machine PM Ratio", "machine_pm_ratio", True, "ratio"),
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


def _run_position(manifest: dict[str, Any] | None, current_run_id: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(manifest, dict):
        return None, None, None
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    if not runs:
        return None, None, None
    baseline = runs[0] if isinstance(runs[0], dict) else None
    current = _find_run(manifest, current_run_id)
    prev = None
    if current is not None:
        current_id = str(current.get("id", "")).strip()
        for idx, row in enumerate(runs):
            if isinstance(row, dict) and str(row.get("id", "")).strip() == current_id:
                if idx > 0 and isinstance(runs[idx - 1], dict):
                    prev = runs[idx - 1]
                break
    return baseline, prev, current


def _kpi_of(run: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(run, dict):
        payload = run.get("kpi", {}) if isinstance(run.get("kpi", {}), dict) else {}
        if payload:
            merged.update(payload)
    if isinstance(fallback, dict):
        merged.update(fallback)
    return merged


def _format_value(value: float, kind: str) -> str:
    if kind == "ratio":
        return f"{value:.3f}"
    if kind == "minutes":
        return f"{value:.1f}m"
    if kind == "count":
        return f"{int(round(value))}"
    return f"{value:.2f}"


def _format_sim_time(run_meta: dict[str, Any] | None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    total_days = _safe_int(payload.get("total_days", 0))
    minutes_per_day = _safe_float(payload.get("minutes_per_day", 0.0))
    sim_total_min = _safe_float(payload.get("sim_total_min", 0.0))
    if total_days > 0 and minutes_per_day > 0:
        return f"{total_days}d / {int(round(minutes_per_day))}m per day"
    if sim_total_min > 0:
        return f"{int(round(sim_total_min))}m"
    return "-"


def _format_executed_until(kpi: dict[str, Any], run_meta: dict[str, Any] | None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    daily_rows = []
    if isinstance(kpi.get("daily_summary_rows", []), list):
        daily_rows = kpi.get("daily_summary_rows", [])
    if isinstance(payload.get("minutes_per_day", None), (int, float)):
        minutes_per_day = _safe_float(payload.get("minutes_per_day", 0.0))
    else:
        minutes_per_day = 0.0
    completed_days = 0
    if isinstance(daily_rows, list) and daily_rows:
        completed_days = max(_safe_int((daily_rows[-1] if isinstance(daily_rows[-1], dict) else {}).get("day", 0), 0), len(daily_rows))
    sim_total_min = _safe_float(payload.get("sim_total_min", 0.0))
    if completed_days > 0 and minutes_per_day > 0:
        executed_min = min(sim_total_min if sim_total_min > 0 else completed_days * minutes_per_day, completed_days * minutes_per_day)
        return f"Day {completed_days} / {int(round(executed_min))}m"
    if sim_total_min > 0:
        return f"0 / {int(round(sim_total_min))}m"
    return "-"


def _metric_delta(current: dict[str, Any], reference: dict[str, Any], key: str, higher_is_better: bool, kind: str) -> tuple[str, str]:
    cur = _safe_float(current.get(key))
    ref = _safe_float(reference.get(key))
    delta = cur - ref
    good = delta >= 0 if higher_is_better else delta <= 0
    cls = "good" if abs(delta) > 1e-9 and good else ("bad" if abs(delta) > 1e-9 else "muted")
    if kind == "ratio":
        text = f"{delta:+.3f}"
    elif kind == "minutes":
        text = f"{delta:+.1f}m"
    else:
        text = f"{delta:+.0f}"
    return text, cls


def _summary_cards(kpi: dict[str, Any], run_meta: dict[str, Any] | None = None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    cards = [
        ("Accepted Products", _format_value(_safe_float(kpi.get("total_products")), "count"), "Finished products accepted in this run."),
        ("Closure Ratio", _format_value(_safe_float(kpi.get("downstream_closure_ratio")), "ratio"), "How much downstream output was actually closed."),
        ("Coordination Incidents", _format_value(_safe_float(kpi.get("coordination_incident_total")), "count"), "Execution friction caused by planning/coordination mismatch."),
        ("Commitment Dispatches", _format_value(_safe_float(kpi.get("commitment_dispatch_total")), "count"), "Planner commitments that reached worker execution."),
        ("Product Lead Time", _format_value(_safe_float(kpi.get("completed_product_lead_time_avg_min")), "minutes"), "Average end-to-end product completion time."),
        ("Product Input Wait", _format_value(_safe_float(kpi.get("product_input_wait_avg_min")), "minutes"), "Average waiting time before inspection/product intake clears."),
        ("Machine Broken Ratio", _format_value(_safe_float(kpi.get("machine_broken_ratio")), "ratio"), "Share of machine time lost to breakdown."),
        ("Machine PM Ratio", _format_value(_safe_float(kpi.get("machine_pm_ratio")), "ratio"), "Share of machine time spent on preventive maintenance."),
        ("Wall Clock", str(payload.get("wall_clock_human", "")).strip() or str(kpi.get("wall_clock_human", "")).strip() or "-", "Actual elapsed execution time for this simulation run."),
        ("Configured Horizon", _format_sim_time(payload), "Configured simulation horizon for this run."),
        ("Executed Until", _format_executed_until(kpi, payload), "How far the simulation actually progressed before completion or termination."),
        ("Termination Reason", str(kpi.get("termination_reason", "")).strip() or ("completed_horizon" if not bool(kpi.get("terminated", False)) else "-"), "Why the run stopped. Completed runs show completed_horizon."),
    ]
    return "<section class='section'><div class='grid cards-4'>" + "".join(
        f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div><div class='sub'>{html.escape(sub)}</div></div>"
        for label, value, sub in cards
    ) + "</div></section>"


def _task_assignment_section(run_meta: dict[str, Any] | None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    task_assignment = payload.get("task_assignment", {}) if isinstance(payload.get("task_assignment", {}), dict) else {}
    allowed = task_assignment.get("allowed_task_families", {}) if isinstance(task_assignment.get("allowed_task_families", {}), dict) else {}
    if not allowed:
        return ""
    cards = []
    for agent_id in sorted(allowed.keys()):
        values = allowed.get(agent_id, [])
        families = ", ".join(str(value).strip() for value in values if str(value).strip()) if isinstance(values, list) else ""
        cards.append(
            f"<div class='card'><div class='label'>{html.escape(agent_id)}</div><div class='value' style='font-size:1rem'>{html.escape(families or 'No production tasks')}</div></div>"
        )
    policy = str(task_assignment.get("battery_exception_policy", "safety_only")).strip() or "safety_only"
    validation = str(task_assignment.get("validation", "error")).strip() or "error"
    return (
        "<section class='section'><div class='panel'><h2>Fixed Task Assignment</h2>"
        f"<p class='sub'>battery_exception_policy={html.escape(policy)}, validation={html.escape(validation)}</p>"
        f"<div class='grid cards-3'>{''.join(cards)}</div></div></section>"
    )


def _render_key_value_table(title: str, rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><th>{html.escape(key)}</th><td>{html.escape(value)}</td></tr>"
        for key, value in rows
        if str(value).strip()
    )
    if not body:
        return ""
    return f"<div class='panel'><h3>{html.escape(title)}</h3><table><tbody>{body}</tbody></table></div>"


def _config_section(run_meta: dict[str, Any] | None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    if not payload:
        return ""

    worker_local = payload.get("worker_local_response", {}) if isinstance(payload.get("worker_local_response", {}), dict) else {}
    initial_norms = payload.get("initial_norms", {}) if isinstance(payload.get("initial_norms", {}), dict) else {}
    llm_meta = payload.get("llm", {}) if isinstance(payload.get("llm", {}), dict) else {}
    orchestration = llm_meta.get("openclaw", {}) if isinstance(llm_meta.get("openclaw", {}), dict) else {}

    top_cards = [
        ("Decision Mode", str(payload.get("decision_mode", "")).strip() or "-"),
        ("Urgent Discuss", "enabled" if bool(payload.get("urgent_discuss_enabled", False)) else "disabled"),
        ("Norms", "enabled" if bool(payload.get("norms_enabled", False)) else "disabled"),
        ("Worker Execution", str(payload.get("worker_execution_mode", "")).strip() or "-"),
    ]

    summary_cards = (
        "<div class='grid cards-4'>"
        + "".join(
            f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value' style='font-size:1.05rem'>{html.escape(value)}</div></div>"
            for label, value in top_cards
        )
        + "</div>"
    )

    worker_rows = [
        ("enabled", str(bool(worker_local.get("enabled", False))).lower()),
        ("scope", str(worker_local.get("scope", "")).strip() or "-"),
        ("max_local_attempts_per_incident", str(worker_local.get("max_local_attempts_per_incident", ""))),
        ("allow_handoff", str(bool(worker_local.get("allow_handoff", False))).lower()),
        ("allow_self_reorder", str(bool(worker_local.get("allow_self_reorder", False))).lower()),
        ("allow_self_recovery", str(bool(worker_local.get("allow_self_recovery", False))).lower()),
        ("blocked_duration_escalation_min", str(worker_local.get("blocked_duration_escalation_min", ""))),
        ("expiry_margin_escalation_min", str(worker_local.get("expiry_margin_escalation_min", ""))),
    ]

    norms_rows = [(str(key), str(value)) for key, value in initial_norms.items()]

    llm_rows: list[tuple[str, str]] = []
    if llm_meta:
        llm_rows.extend(
            [
                ("provider", str(llm_meta.get("provider", "")).strip() or "-"),
                ("model", str(llm_meta.get("model", "")).strip() or "-"),
                ("language", str(llm_meta.get("language", "")).strip() or "-"),
                ("communication_enabled", str(bool(llm_meta.get("communication_enabled", False))).lower()),
                ("coordination_review_enabled", str(bool(llm_meta.get("coordination_review_enabled", False))).lower()),
                ("evaluator_enabled", str(bool(llm_meta.get("evaluator_enabled", False))).lower()),
            ]
        )

    openclaw_rows: list[tuple[str, str]] = []
    if orchestration:
        backend = orchestration.get("backend", {}) if isinstance(orchestration.get("backend", {}), dict) else {}
        openclaw_rows.extend(
            [
                ("profile_name", str(orchestration.get("profile_name", "")).strip() or "-"),
                ("session_namespace", str(orchestration.get("session_namespace", "")).strip() or "-"),
                ("manager_agent_id", str(orchestration.get("manager_agent_id", "")).strip() or "-"),
                ("worker_agent_ids", ", ".join(str(v).strip() for v in orchestration.get("worker_agent_ids", []) if str(v).strip()) or "-"),
                ("backend.provider", str(backend.get("provider", "")).strip() or "-"),
                ("backend.model", str(backend.get("model_name", backend.get("model", ""))).strip() or "-"),
                ("backend.base_url", str(backend.get("effective_base_url", backend.get("base_url", ""))).strip() or "-"),
            ]
        )

    body = (
        "<section class='section'><div class='panel'><h2>Current Run Configuration</h2>"
        "<p class='sub'>This section shows the effective runtime settings captured in <code class='inline'>run_meta.json</code> for the current simulation.</p>"
        f"{summary_cards}</div></section>"
        "<section class='section'><div class='grid cards-2'>"
        f"{_render_key_value_table('Worker Local Response', worker_rows)}"
        f"{_render_key_value_table('Initial Norms', norms_rows)}"
        "</div></section>"
    )

    if llm_rows or openclaw_rows:
        body += "<section class='section'><div class='grid cards-2'>"
        body += _render_key_value_table("LLM Settings", llm_rows)
        body += _render_key_value_table("OpenClaw Runtime", openclaw_rows)
        body += "</div></section>"
    return body


def _artifact_cards(*, current_page_path: Path, run: dict[str, Any] | None, manifest: dict[str, Any] | None, manifest_path: Path | None) -> str:
    if not isinstance(run, dict):
        return ""
    artifacts = run.get("artifacts", {}) if isinstance(run.get("artifacts", {}), dict) else {}
    run_meta = run.get("run_meta", {}) if isinstance(run.get("run_meta", {}), dict) else {}
    decision_mode = str(run_meta.get("decision_mode", "")).strip().lower()
    total_runs = _safe_int(run_meta.get("total_runs", 1), 1)
    show_reasoning = decision_mode in {"llm_planner", "openclaw_adaptive_priority"}
    show_task_priority = decision_mode in {"adaptive_priority", "fixed_priority", "fixed_task_assignment", "openclaw_adaptive_priority"}
    show_knowledge = decision_mode == "llm_planner" or (decision_mode == "openclaw_adaptive_priority" and total_runs > 1)
    replay_href = build_replay_app_url(
        port=int(manifest.get("streamlit_preferred_port", 8505) or 8505) if isinstance(manifest, dict) else 8505,
        manifest_path=manifest_path,
        run_id=str(run.get("id", "")).strip(),
        events_path=Path(str(artifacts.get("events.jsonl", ""))) if str(artifacts.get("events.jsonl", "")).strip() else None,
        series_root=Path(str(manifest.get("series_root", ""))) if isinstance(manifest, dict) and str(manifest.get("series_root", "")).strip() else None,
    )
    cards = [
        ("KPI Dashboard", rel_href(current_page_path, artifacts.get("kpi_dashboard.html", "")), "Quantitative view with detailed charts and day-level trends."),
        ("Replay App", replay_href, "Animated factory replay with run selector, entity highlight, and time scrubber."),
        ("Gantt", rel_href(current_page_path, artifacts.get("gantt.html", "")), "Task and machine timeline for the selected run."),
    ]
    if show_task_priority:
        cards.append(
            ("Task Priority", rel_href(current_page_path, artifacts.get("task_priority_dashboard.html", "")), "Task-family priority, worker-specific weights, and priority evolution for priority-driven execution.")
        )
    if show_reasoning:
        cards.append(
            ("Reasoning", rel_href(current_page_path, artifacts.get("reasoning_dashboard.html", "")), "Shift policy, review memory, execution flow, and blocker/latency context.")
        )
    if show_knowledge:
        cards.append(
            ("Knowledge", rel_href(current_page_path, artifacts.get("knowledge_dashboard.html", "")), "Ontology view: recurring issues, lessons, and run-to-run diffs.")
        )
    if isinstance(manifest, dict) and not bool(manifest.get("single_run", True)):
        cards.append(
            (
                "Series",
                rel_href(current_page_path, Path(str(manifest.get("series_root", ""))) / "series_dashboard.html"),
                "Cross-run comparison across performance, stability, and knowledge drift.",
            )
        )
    return (
        "<section class='section'><div class='panel'><h2>Primary Views</h2><div class='artifact-grid'>"
        + "".join(
            f"<a class='artifact-card' href='{html.escape(href)}'{(' target=\"_blank\" rel=\"noopener noreferrer\"' if href.startswith(('http://', 'https://')) else '')}><strong>{html.escape(label)}</strong><span>{html.escape(desc)}</span></a>"
            for label, href, desc in cards
        )
        + "</div></div></section>"
    )


def _changes_section(current_kpi: dict[str, Any], baseline_kpi: dict[str, Any], prev_kpi: dict[str, Any] | None) -> str:
    improvements: list[str] = []
    regressions: list[str] = []
    for label, key, higher_is_better, kind in METRIC_SPECS:
        current_val = _safe_float(current_kpi.get(key))
        baseline_val = _safe_float(baseline_kpi.get(key))
        delta = current_val - baseline_val
        if abs(delta) < 1e-9:
            continue
        good = delta > 0 if higher_is_better else delta < 0
        delta_text = _format_value(abs(delta), kind)
        sentence = f"{label}: {'improved' if good else 'worsened'} vs baseline by {delta_text}."
        (improvements if good else regressions).append(sentence)
    if prev_kpi:
        for label, key, higher_is_better, kind in METRIC_SPECS[:5]:
            current_val = _safe_float(current_kpi.get(key))
            prev_val = _safe_float(prev_kpi.get(key))
            delta = current_val - prev_val
            if abs(delta) < 1e-9:
                continue
            good = delta > 0 if higher_is_better else delta < 0
            delta_text = _format_value(abs(delta), kind)
            sentence = f"{label}: {'better' if good else 'worse'} than previous run by {delta_text}."
            (improvements if good else regressions).append(sentence)
    improvements = improvements[:5] or ["No material improvement relative to the current baseline reference."]
    regressions = regressions[:5] or ["No material regression relative to the current baseline reference."]
    return (
        "<section class='section'><div class='grid cards-2'>"
        "<div class='panel'><h2>What Improved</h2><ul class='clean'>"
        + "".join(f"<li>{html.escape(item)}</li>" for item in improvements)
        + "</ul></div><div class='panel'><h2>What Regressed</h2><ul class='clean'>"
        + "".join(f"<li>{html.escape(item)}</li>" for item in regressions)
        + "</ul></div></div></section>"
    )


def _run_delta_table(current_kpi: dict[str, Any], baseline_kpi: dict[str, Any], prev_kpi: dict[str, Any] | None) -> str:
    rows: list[str] = []
    for label, key, higher_is_better, kind in METRIC_SPECS:
        value_text = _format_value(_safe_float(current_kpi.get(key)), kind)
        baseline_text, baseline_cls = _metric_delta(current_kpi, baseline_kpi, key, higher_is_better, kind)
        if prev_kpi:
            prev_text, prev_cls = _metric_delta(current_kpi, prev_kpi, key, higher_is_better, kind)
        else:
            prev_text, prev_cls = "-", "muted"
        rows.append(
            f"<tr><td>{html.escape(label)}</td><td>{html.escape(value_text)}</td><td class='{baseline_cls}'>{html.escape(baseline_text)}</td><td class='{prev_cls}'>{html.escape(prev_text)}</td></tr>"
        )
    return (
        "<section class='section'><div class='panel'><h2>Run Delta</h2><table><thead><tr><th>Metric</th><th>Current</th><th>vs Baseline</th><th>vs Previous</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div></section>"
    )


def _risk_section(reflection: dict[str, Any] | None) -> str:
    payload = reflection if isinstance(reflection, dict) else {}
    problems = payload.get("run_problems", []) if isinstance(payload.get("run_problems", []), list) else []
    watchouts = payload.get("open_watchouts", []) if isinstance(payload.get("open_watchouts", []), list) else []
    items: list[str] = []
    for raw in problems:
        if isinstance(raw, dict):
            text = str(raw.get("issue", "")).strip()
        else:
            text = str(raw).strip()
        if text:
            items.append(text)
    for text in watchouts:
        clean = str(text).strip()
        if clean:
            items.append(clean)
    items = items[:6] or ["No reflector risk summary was recorded for this run."]
    return "<section class='section'><div class='panel'><h2>Top Bottlenecks and Risks</h2><ul class='clean'>" + "".join(
        f"<li>{html.escape(item)}</li>" for item in items
    ) + "</ul></div></section>"


def _raw_artifacts(current_page_path: Path, run: dict[str, Any] | None) -> str:
    if not isinstance(run, dict):
        return ""
    artifacts = run.get("artifacts", {}) if isinstance(run.get("artifacts", {}), dict) else {}
    raw_links = [
        ("run_reflection.json", artifacts.get("run_reflection.json", "")),
        ("run_reflection.md", artifacts.get("run_reflection.md", "")),
        ("events.jsonl", artifacts.get("events.jsonl", "")),
        ("run_meta.json", artifacts.get("run_meta.json", "")),
        ("daily_summary.json", artifacts.get("daily_summary.json", "")),
        ("kpi.json", artifacts.get("kpi.json", "")),
    ]
    items = "".join(
        f"<li><a href='{html.escape(rel_href(current_page_path, path))}'>{html.escape(label)}</a></li>"
        for label, path in raw_links
        if str(path).strip()
    )
    return f"<section class='section'><div class='panel'><h2>Raw Artifacts</h2><ul class='clean'>{items}</ul></div></section>" if items else ""


def export_results_dashboard(
    *,
    output_dir: Path,
    kpi: dict[str, Any],
    links: dict[str, str] | None = None,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    current_run_id: str | None = None,
    analysis: dict[str, Any] | None = None,
    reflection: dict[str, Any] | None = None,
    run_meta: dict[str, Any] | None = None,
) -> Path:
    output_path = Path(output_dir) / "results_dashboard.html"
    baseline_run, prev_run, current_run = _run_position(manifest, current_run_id)
    current_kpi = _kpi_of(current_run, kpi)
    if isinstance(current_run, dict):
        daily_blob = current_run.get("daily", {}) if isinstance(current_run.get("daily", {}), dict) else {}
        daily_rows = daily_blob.get("rows", []) if isinstance(daily_blob.get("rows", []), list) else []
        if daily_rows:
            current_kpi["daily_summary_rows"] = daily_rows
    baseline_kpi = _kpi_of(baseline_run, kpi)
    prev_kpi = _kpi_of(prev_run) if prev_run else None
    subtitle = "Current run summary, cross-run deltas, primary operating views, and the main risk picture."
    multi_run = isinstance(manifest, dict) and not bool(manifest.get("single_run", True))
    if multi_run and isinstance(analysis, dict) and str(analysis.get("analysis_summary", "")).strip():
        subtitle = str(analysis.get("analysis_summary", "")).strip()
    body = (
        _summary_cards(current_kpi, run_meta)
        + _config_section(run_meta)
        + _task_assignment_section(run_meta)
        + _artifact_cards(current_page_path=output_path, run=current_run, manifest=manifest, manifest_path=manifest_path)
        + _run_delta_table(current_kpi, baseline_kpi, prev_kpi)
        + _changes_section(current_kpi, baseline_kpi, prev_kpi)
        + _risk_section(reflection if isinstance(reflection, dict) else (current_run.get("reflection", {}) if isinstance(current_run, dict) else {}))
        + _raw_artifacts(output_path, current_run)
    )
    html_text = render_page_shell(
        title="ManSim Results Hub",
        current_page_path=output_path,
        manifest=manifest,
        manifest_path=manifest_path,
        current_artifact="results_dashboard.html",
        current_run_id=current_run_id,
        page_title="Results Hub",
        page_subtitle=subtitle,
        body_html=body,
    )
    output_path.write_text(html_text, encoding="utf-8")
    return output_path
