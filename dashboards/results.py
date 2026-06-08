from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .shell import render_page_shell


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


def _is_manager_mode(decision_mode: str) -> bool:
    return str(decision_mode).strip().lower() in {"llm_planner", "openclaw_adaptive_priority"}


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


def _format_optional_value(value: Any, kind: str) -> str:
    if value is None or value == "":
        return "pending" if kind == "minutes" else "-"
    return _format_value(_safe_float(value), kind)


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


def _summary_cards(kpi: dict[str, Any], run_meta: dict[str, Any] | None = None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    scenario_type = str(kpi.get("scenario_type") or payload.get("scenario_type") or "").strip()
    scenario_label = scenario_type or "-"
    if scenario_type == "shipyard_basic":
        output_card = (
            "Completed Surface Tiles",
            _format_value(_safe_float(kpi.get("completed_surface_tile_count", kpi.get("completed_section_count", kpi.get("total_products")))), "count"),
            "Ship exterior tiles that completed welding, surface preparation, painting, and inspection.",
        )
        lead_time_card = (
            "Ship Makespan",
            _format_optional_value(kpi.get("makespan_min", kpi.get("completed_product_lead_time_avg_min")), "minutes"),
            "Elapsed simulated minutes until every ship surface tile reaches COMPLETE.",
        )
        input_wait_card = (
            "Surface Completion",
            _format_value(_safe_float(kpi.get("surface_tile_completion_ratio", kpi.get("section_completion_ratio", kpi.get("downstream_closure_ratio")))), "ratio"),
            "Share of ship exterior surface tiles in COMPLETE state.",
        )
    else:
        output_card = ("Accepted Products", _format_value(_safe_float(kpi.get("total_products")), "count"), "Finished products accepted in this run.")
        lead_time_card = ("Product Lead Time", _format_value(_safe_float(kpi.get("completed_product_lead_time_avg_min")), "minutes"), "Average end-to-end product completion time.")
        input_wait_card = ("Product Input Wait", _format_value(_safe_float(kpi.get("product_input_wait_avg_min")), "minutes"), "Average waiting time before inspection/product intake clears.")
    cards = [
        ("Scenario", scenario_label, "Scenario plugin used for this run."),
        output_card,
        ("Closure Ratio", _format_value(_safe_float(kpi.get("downstream_closure_ratio")), "ratio"), "How much downstream output was actually closed."),
        lead_time_card,
        input_wait_card,
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
    llm_meta = payload.get("llm", {}) if isinstance(payload.get("llm", {}), dict) else {}
    orchestration = llm_meta.get("openclaw", {}) if isinstance(llm_meta.get("openclaw", {}), dict) else {}
    decision_mode = str(payload.get("decision_mode", "")).strip().lower()
    manager_mode = _is_manager_mode(decision_mode)

    top_cards = [
        ("Decision Mode", str(payload.get("decision_mode", "")).strip() or "-"),
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

    llm_rows: list[tuple[str, str]] = []
    if manager_mode and llm_meta:
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
    if manager_mode and orchestration:
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
        "</div></section>"
    )

    if llm_rows or openclaw_rows:
        body += "<section class='section'><div class='grid cards-2'>"
        body += _render_key_value_table("LLM Settings", llm_rows)
        body += _render_key_value_table("OpenClaw Runtime", openclaw_rows)
        body += "</div></section>"
    return body


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
    subtitle = "Current run summary and effective runtime configuration."
    multi_run = isinstance(manifest, dict) and not bool(manifest.get("single_run", True))
    if multi_run and isinstance(analysis, dict) and str(analysis.get("analysis_summary", "")).strip():
        subtitle = str(analysis.get("analysis_summary", "")).strip()
    body = (
        _summary_cards(current_kpi, run_meta)
        + _config_section(run_meta)
        + _task_assignment_section(run_meta)
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
