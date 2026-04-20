from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .shell import render_page_shell


PRIMARY_METRICS = [
    ("Accepted Products", "total_products", "count", True, "Finished products accepted in this run."),
    ("Closure Ratio", "downstream_closure_ratio", "ratio", True, "Share of downstream output that actually closed."),
    ("Throughput / Sim Hour", "throughput_per_sim_hour", "float", True, "Accepted products normalized by simulated hour."),
    ("Machine Utilization", "machine_utilization", "ratio", True, "Processing minutes divided by total machine-minutes."),
    ("Machine Broken Ratio", "machine_broken_ratio", "ratio", False, "Broken minutes divided by total machine-minutes."),
    ("Machine PM Ratio", "machine_pm_ratio", "ratio", False, "Preventive-maintenance minutes divided by total machine-minutes."),
    ("Worker Availability", "agent_availability_ratio", "ratio", True, "Share of worker-minutes not spent discharged."),
    ("Worker Local Responses", "worker_local_response_total", "count", True, "Local recoveries or local reorder attempts taken by workers."),
    ("Worker Discharged Ratio", "agent_discharged_ratio", "ratio", False, "Share of worker-minutes spent discharged."),
    ("Wall Clock", "wall_clock_sec", "duration", False, "Real wall-clock runtime for this run."),
    ("Coordination Incidents", "coordination_incident_total", "count", False, "Planning and execution mismatches."),
    ("Unique Replan Blockers", "unique_replan_blocker_total", "count", False, "Distinct blocker states that forced replanning."),
    ("Commitment Dispatches", "commitment_dispatch_total", "count", True, "Commitments that reached worker execution."),
    ("Product Lead Time", "completed_product_lead_time_avg_min", "minutes", False, "Average accepted-product completion time."),
]

PRIMARY_METRIC_LOOKUP = {key: (label, key, kind, higher_is_better, description) for (label, key, kind, higher_is_better, description) in PRIMARY_METRICS}
METRIC_GROUPS = {
    "item": ["total_products", "downstream_closure_ratio", "throughput_per_sim_hour", "completed_product_lead_time_avg_min"],
    "machine": ["machine_utilization", "machine_broken_ratio", "machine_pm_ratio", "wall_clock_sec"],
    "worker": ["agent_availability_ratio", "worker_local_response_total", "agent_discharged_ratio", "commitment_dispatch_total"],
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


def _format_duration(seconds: float) -> str:
    raw_seconds = max(0.0, float(seconds))
    if raw_seconds < 1.0:
        return f"{raw_seconds:.3f}s"
    if raw_seconds < 10.0:
        return f"{raw_seconds:.2f}s"
    total_seconds = max(0, int(round(raw_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_configured_horizon(run_meta: dict[str, Any] | None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    total_days = _safe_int(payload.get("total_days", 0), 0)
    minutes_per_day = _safe_float(payload.get("minutes_per_day", 0.0), 0.0)
    sim_total_min = _safe_float(payload.get("sim_total_min", 0.0), 0.0)
    if total_days > 0 and minutes_per_day > 0:
        return f"{total_days}d / {int(round(minutes_per_day))}m per day"
    if sim_total_min > 0:
        return f"{int(round(sim_total_min))}m"
    return "-"


def _format_executed_until(daily_summary: list[dict[str, Any]], run_meta: dict[str, Any] | None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    minutes_per_day = _safe_float(payload.get("minutes_per_day", 0.0), 0.0)
    completed_days = max((_safe_int((row if isinstance(row, dict) else {}).get("day", 0), 0) for row in daily_summary), default=0)
    if completed_days > 0 and minutes_per_day > 0:
        return f"Day {completed_days} / {int(round(completed_days * minutes_per_day))}m"
    return "-"


def _format_metric(value: float, kind: str) -> str:
    if kind == "ratio":
        return f"{value:.3f}"
    if kind == "minutes":
        return f"{value:.1f}m"
    if kind == "duration":
        return _format_duration(value)
    if kind == "float":
        return f"{value:.2f}"
    return f"{int(round(value))}"


def _format_ratio_percent(value: float) -> str:
    return f"{100.0 * float(value):.1f}%"


def _sorted_worker_ids(mapping: dict[str, Any]) -> list[str]:
    def _worker_key(raw: str) -> tuple[int, str]:
        name = str(raw)
        suffix = name[1:] if name.upper().startswith("A") else name
        if suffix.isdigit():
            return (0, f"{int(suffix):06d}")
        return (1, name)

    return sorted((str(key) for key in mapping.keys()), key=_worker_key)


def _machine_sort_key(raw: str) -> tuple[int, int, str]:
    text = str(raw)
    station = 999
    machine = 999
    if text.startswith("S") and "M" in text:
        left, right = text.split("M", 1)
        try:
            station = int(left[1:])
        except ValueError:
            station = 999
        try:
            machine = int(right)
        except ValueError:
            machine = 999
    return (station, machine, text)


def _find_run(manifest: dict[str, Any] | None, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return None
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    target = str(run_id or manifest.get("current_run", "")).strip()
    for row in runs:
        if isinstance(row, dict) and str(row.get("id", "")).strip() == target:
            return row
    return runs[-1] if runs and isinstance(runs[-1], dict) else None


def _kpi_of(run: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = run.get("kpi", {}) if isinstance(run, dict) and isinstance(run.get("kpi", {}), dict) else {}
    merged: dict[str, Any] = {}
    if payload:
        merged.update(payload)
    if isinstance(fallback, dict):
        merged.update(fallback)
    return merged


def _run_context(manifest: dict[str, Any] | None, current_run_id: str | None, kpi: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(manifest, dict):
        return None, None, None
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    baseline = runs[0] if runs and isinstance(runs[0], dict) else None
    current = _find_run(manifest, current_run_id)
    previous = None
    if current is not None:
        current_id = str(current.get("id", "")).strip()
        for idx, row in enumerate(runs):
            if isinstance(row, dict) and str(row.get("id", "")).strip() == current_id:
                if idx > 0 and isinstance(runs[idx - 1], dict):
                    previous = runs[idx - 1]
                break
        current.setdefault("kpi", {})
        if isinstance(current.get("kpi", {}), dict):
            current["kpi"] = _kpi_of(current, kpi)
    return baseline, previous, current


def _summary_cards(kpi: dict[str, Any], metric_keys: list[str] | None = None) -> str:
    selected_keys = metric_keys or [key for _label, key, _kind, _higher_is_better, _description in PRIMARY_METRICS]
    cards = []
    for key in selected_keys:
        metric = PRIMARY_METRIC_LOOKUP.get(key)
        if metric is None:
            continue
        label, key, kind, _higher_is_better, description = metric
        value = _safe_float(kpi.get(key))
        cards.append(
            f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(_format_metric(value, kind))}</div><div class='sub'>{html.escape(description)}</div></div>"
        )
    return "<div class='grid cards-4'>" + "".join(cards) + "</div>"


def _run_horizon_cards(kpi: dict[str, Any], daily_summary: list[dict[str, Any]], run_meta: dict[str, Any] | None) -> str:
    payload = run_meta if isinstance(run_meta, dict) else {}
    cards = [
        ("Configured Horizon", _format_configured_horizon(payload), "Configured simulation horizon from run metadata."),
        ("Executed Until", _format_executed_until(daily_summary, payload), "How far the run actually progressed before stop."),
        (
            "Termination Reason",
            str(kpi.get("termination_reason", "")).strip() or ("completed_horizon" if not bool(kpi.get("terminated", False)) else "-"),
            "Why the run stopped. Completed runs show completed_horizon.",
        ),
        ("Wall Clock", _format_duration(_safe_float(kpi.get("wall_clock_sec", 0.0))), "Actual elapsed execution time for this run."),
    ]
    return "<section class='section'><div class='grid cards-4'>" + "".join(
        f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div><div class='sub'>{html.escape(sub)}</div></div>"
        for label, value, sub in cards
    ) + "</div></section>"


def _group_section(title: str, description: str, cards_html: str, body_html: str) -> str:
    desc = f"<p class='page-subtitle'>{html.escape(description)}</p>" if description else ""
    return (
        "<section class='section'>"
        f"<div class='panel'><h2>{html.escape(title)}</h2>{desc}</div>"
        + cards_html
        + body_html
        + "</section>"
    )


def _series_snapshot(manifest: dict[str, Any] | None, current_run_id: str | None, kpi: dict[str, Any]) -> str:
    baseline, previous, current = _run_context(manifest, current_run_id, kpi)
    if not isinstance(current, dict):
        return ""
    analysis = manifest.get("analysis", {}) if isinstance(manifest, dict) and isinstance(manifest.get("analysis", {}), dict) else {}
    peak = analysis.get("peak_run", {}) if isinstance(analysis.get("peak_run", {}), dict) else {}
    worst = analysis.get("worst_run", {}) if isinstance(analysis.get("worst_run", {}), dict) else {}
    runs = manifest.get("runs", []) if isinstance(manifest, dict) and isinstance(manifest.get("runs", []), list) else []
    peak_run = None
    worst_run = None
    peak_index = int(peak.get("run_index", 0) or 0)
    worst_index = int(worst.get("run_index", 0) or 0)
    for row in runs:
        if not isinstance(row, dict):
            continue
        run_index = int(row.get("run_index", 0) or 0)
        if run_index == peak_index:
            peak_run = row
        if run_index == worst_index:
            worst_run = row
    current_label = str(current.get("label", current.get("id", current_run_id or "current")))
    cards = [
        ("Selected Run", current_label, "Current run shown on this page."),
        (
            "Baseline Run",
            str((baseline or {}).get("label", "-")),
            "Reference run used for series context." if baseline else "No baseline reference.",
        ),
        (
            "Previous Run",
            str((previous or {}).get("label", "-")),
            "Immediate previous run used for regression context." if previous else "No previous run reference.",
        ),
        (
            "Series Classification",
            str(analysis.get("knowledge_effect_classification", "single_run")),
            str(analysis.get("performance_pattern", "single_run")),
        ),
    ]
    extra = []
    if isinstance(manifest, dict) and not bool(manifest.get("single_run", True)):
        peak_kpi = (peak_run or {}).get("kpi", {}) if isinstance((peak_run or {}).get("kpi", {}), dict) else {}
        worst_kpi = (worst_run or {}).get("kpi", {}) if isinstance((worst_run or {}).get("kpi", {}), dict) else {}
        extra = [
            (
                "Peak Run",
                str((peak_run or {}).get("label", f"run_{peak_index:02d}" if peak_index > 0 else "-")),
                f"Products={int(peak_kpi.get('total_products', 0) or 0)} | Closure={_safe_float(peak_kpi.get('downstream_closure_ratio')):.3f}",
            ),
            (
                "Worst Run",
                str((worst_run or {}).get("label", f"run_{worst_index:02d}" if worst_index > 0 else "-")),
                f"Products={int(worst_kpi.get('total_products', 0) or 0)} | Closure={_safe_float(worst_kpi.get('downstream_closure_ratio')):.3f}",
            ),
        ]
    body = "".join(
        f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(value)}</div><div class='sub'>{html.escape(sub)}</div></div>"
        for label, value, sub in cards + extra
    )
    columns = "cards-4" if len(cards + extra) >= 4 else "cards-2"
    return f"<section class='section'><div class='grid {columns}'>{body}</div></section>"


def _incident_table(kpi: dict[str, Any]) -> str:
    rows = [
        ("Physical incidents", _safe_int(kpi.get("physical_incident_total")), "Machine, worker, buffer, and supply-side events."),
        ("Coordination incidents", _safe_int(kpi.get("coordination_incident_total")), "Execution friction caused by planning continuity failures."),
        ("Unique replan blockers", _safe_int(kpi.get("unique_replan_blocker_total")), "Distinct blocker states after deduplication."),
        ("Planner escalations", _safe_int(kpi.get("planner_escalation_total")), "How often local handling escalated to planner replanning."),
        ("Worker local responses", _safe_int(kpi.get("worker_local_response_total")), "Local recoveries or local reorder attempts taken by workers."),
        ("Commitment dispatches", _safe_int(kpi.get("commitment_dispatch_total")), "Commitments successfully matched to execution."),
    ]
    body = "".join(
        f"<tr><td>{html.escape(label)}</td><td>{value}</td><td>{html.escape(desc)}</td></tr>" for label, value, desc in rows
    )
    return "<div class='panel'><h2>Incident and Coordination Breakdown</h2><table><thead><tr><th>Metric</th><th>Value</th><th>Meaning</th></tr></thead><tbody>" + body + "</tbody></table></div>"


def _machine_table(kpi: dict[str, Any]) -> str:
    machine_utilization = kpi.get("machine_utilization_by_machine", {}) if isinstance(kpi.get("machine_utilization_by_machine", {}), dict) else {}
    rows = [("Overall", _safe_float(kpi.get("machine_utilization")))]
    for machine_id in sorted(machine_utilization, key=_machine_sort_key):
        util_metrics = machine_utilization.get(machine_id, {}) if isinstance(machine_utilization.get(machine_id, {}), dict) else {}
        rows.append((machine_id, _safe_float(util_metrics.get("util_total"))))
    body = "".join(
        f"<tr><td>{html.escape(machine_id)}</td><td>{_format_ratio_percent(util_total)}</td></tr>"
        for (machine_id, util_total) in rows
    )
    if not body:
        body = "<tr><td colspan='2'>No per-machine utilization data.</td></tr>"
    return "<div class='panel'><h2>Machine Utilization</h2><p class='muted'>Utilization = processing minutes / total simulated minutes.</p><table><thead><tr><th>Machine</th><th>Utilization</th></tr></thead><tbody>" + body + "</tbody></table></div>"


def _worker_table(kpi: dict[str, Any]) -> str:
    state_by_worker = kpi.get("worker_state_time_by_worker", {}) if isinstance(kpi.get("worker_state_time_by_worker", {}), dict) else {}
    util_by_worker = kpi.get("worker_utilization_by_worker", {}) if isinstance(kpi.get("worker_utilization_by_worker", {}), dict) else {}
    total_working = 0.0
    total_moving = 0.0
    for worker_id in state_by_worker:
        row = state_by_worker.get(worker_id, {}) if isinstance(state_by_worker.get(worker_id, {}), dict) else {}
        total_working += _safe_float(row.get("working_min"))
        total_moving += _safe_float(row.get("moving_min"))
    total_worker_time = sum(
        _safe_float((state_by_worker.get(worker_id, {}) if isinstance(state_by_worker.get(worker_id, {}), dict) else {}).get("working_min"))
        + _safe_float((state_by_worker.get(worker_id, {}) if isinstance(state_by_worker.get(worker_id, {}), dict) else {}).get("moving_min"))
        + _safe_float((state_by_worker.get(worker_id, {}) if isinstance(state_by_worker.get(worker_id, {}), dict) else {}).get("discharged_min"))
        + _safe_float((state_by_worker.get(worker_id, {}) if isinstance(state_by_worker.get(worker_id, {}), dict) else {}).get("idle_min"))
        for worker_id in state_by_worker
    )
    active_total = total_working + total_moving
    rows = [("Overall", (active_total / total_worker_time) if total_worker_time > 0.0 else 0.0)]
    body = "".join(
        f"<tr><td>{html.escape(worker_id)}</td><td>{_format_ratio_percent(util_total)}</td></tr>"
        for worker_id, util_total in (
            rows
            + [
                (
                    worker_id,
                    _safe_float((util_by_worker.get(worker_id, {}) if isinstance(util_by_worker.get(worker_id, {}), dict) else {}).get("util_total")),
                )
                for worker_id in _sorted_worker_ids(state_by_worker)
            ]
        )
    )
    if not body:
        body = "<tr><td colspan='2'>No per-worker utilization data.</td></tr>"
    return "<div class='panel'><h2>Worker Utilization</h2><p class='muted'>Utilization = (working minutes + moving minutes) / total simulated minutes.</p><table><thead><tr><th>Worker</th><th>Utilization</th></tr></thead><tbody>" + body + "</tbody></table></div>"


def _figure_html(fig: Any, *, include_plotlyjs: bool) -> str:
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs, config={"displaylogo": False, "responsive": True})


def _panel_figure_html(title: str, fig_html: str, description: str = "") -> str:
    desc = f"<p class='muted'>{html.escape(description)}</p>" if description else ""
    return f"<div class='panel'><h2>{html.escape(title)}</h2>{desc}{fig_html}</div>"


def export_kpi_dashboard(
    *,
    kpi: dict[str, Any],
    daily_summary: list[dict[str, Any]],
    output_dir: Path,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    current_run_id: str | None = None,
) -> Path | None:
    try:
        import plotly.graph_objects as go
    except Exception:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    dashboard_path = output_dir / "kpi_dashboard.html"

    days = [int(d["day"]) for d in daily_summary]
    daily_products = [float(d.get("products", 0.0) or 0.0) for d in daily_summary]
    daily_scrap_rate = [float(d.get("scrap_rate", 0.0) or 0.0) for d in daily_summary]
    daily_breakdowns = [float(d.get("machine_breakdowns", 0.0) or 0.0) for d in daily_summary]

    stage_tp = kpi.get("stage_throughput", {}) if isinstance(kpi.get("stage_throughput", {}), dict) else {}
    stage_labels = list(stage_tp.keys())
    stage_values = [float(stage_tp.get(label, 0.0) or 0.0) for label in stage_labels]

    agent_task_minutes = kpi.get("agent_task_minutes", {}) if isinstance(kpi.get("agent_task_minutes", {}), dict) else {}
    task_pairs = sorted(((str(task_type), float(minutes)) for task_type, minutes in agent_task_minutes.items()), key=lambda item: item[1], reverse=True)
    task_types = [task_type for task_type, _minutes in task_pairs]
    task_values = [minutes for _task_type, minutes in task_pairs]

    machine_state_by_machine = kpi.get("machine_state_time_by_machine", {}) if isinstance(kpi.get("machine_state_time_by_machine", {}), dict) else {}
    machine_util_by_machine = kpi.get("machine_utilization_by_machine", {}) if isinstance(kpi.get("machine_utilization_by_machine", {}), dict) else {}
    machine_labels = [str(key) for key in sorted(machine_state_by_machine, key=_machine_sort_key)]
    machine_util_total = [
        _safe_float((machine_util_by_machine.get(machine_id, {}) if isinstance(machine_util_by_machine.get(machine_id, {}), dict) else {}).get("util_total"))
        for machine_id in machine_labels
    ]

    buffer_wait_avg = kpi.get("buffer_wait_avg_min_including_open_by_queue", kpi.get("buffer_wait_avg_min_by_queue", {}))
    buffer_wait_avg = buffer_wait_avg if isinstance(buffer_wait_avg, dict) else {}
    buffer_wait_closed_counts = (
        kpi.get("buffer_wait_completed_count_by_queue", {})
        if isinstance(kpi.get("buffer_wait_completed_count_by_queue", {}), dict)
        else {}
    )
    buffer_wait_open_counts = (
        kpi.get("buffer_wait_open_count_by_queue", {})
        if isinstance(kpi.get("buffer_wait_open_count_by_queue", {}), dict)
        else {}
    )
    buffer_wait_keys = [
        ("s1_input", "S1 input queue"),
        ("s1_output", "S1 output queue"),
        ("s2_input", "S2 input queue"),
        ("s2_output", "S2 output queue"),
        ("inspection_input", "Inspection input queue"),
        ("inspection_output", "Inspection output queue"),
    ]
    buffer_wait_labels = [label for _key, label in buffer_wait_keys]
    buffer_wait_values = [float(buffer_wait_avg.get(key, 0.0) or 0.0) for key, _label in buffer_wait_keys]
    buffer_wait_closed_values = [float(buffer_wait_closed_counts.get(key, 0) or 0.0) for key, _label in buffer_wait_keys]
    buffer_wait_open_values = [float(buffer_wait_open_counts.get(key, 0) or 0.0) for key, _label in buffer_wait_keys]

    worker_state_by_worker = kpi.get("worker_state_time_by_worker", {}) if isinstance(kpi.get("worker_state_time_by_worker", {}), dict) else {}
    worker_util_by_worker = kpi.get("worker_utilization_by_worker", {}) if isinstance(kpi.get("worker_utilization_by_worker", {}), dict) else {}
    worker_labels = _sorted_worker_ids(worker_state_by_worker)
    worker_working_values = [
        _safe_float((worker_state_by_worker.get(worker_id, {}) if isinstance(worker_state_by_worker.get(worker_id, {}), dict) else {}).get("working_min"))
        for worker_id in worker_labels
    ]
    worker_moving_values = [
        _safe_float((worker_state_by_worker.get(worker_id, {}) if isinstance(worker_state_by_worker.get(worker_id, {}), dict) else {}).get("moving_min"))
        for worker_id in worker_labels
    ]
    worker_discharged_values = [
        _safe_float((worker_state_by_worker.get(worker_id, {}) if isinstance(worker_state_by_worker.get(worker_id, {}), dict) else {}).get("discharged_min"))
        for worker_id in worker_labels
    ]
    worker_idle_values = [
        _safe_float((worker_state_by_worker.get(worker_id, {}) if isinstance(worker_state_by_worker.get(worker_id, {}), dict) else {}).get("idle_min"))
        for worker_id in worker_labels
    ]
    worker_util_total = [
        _safe_float((worker_util_by_worker.get(worker_id, {}) if isinstance(worker_util_by_worker.get(worker_id, {}), dict) else {}).get("util_total"))
        for worker_id in worker_labels
    ]

    panel_figures: dict[str, str] = {}
    include_plotlyjs = True

    def _common_layout(fig: Any, *, y_title: str, x_title: str = "", tickformat: str | None = None, barmode: str = "group", height: int = 360) -> None:
        fig.update_layout(
            barmode=barmode,
            height=height,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
            margin=dict(l=50, r=30, t=25, b=55),
            plot_bgcolor="#fbfbfd",
            paper_bgcolor="#ffffff",
        )
        fig.update_xaxes(title_text=x_title)
        fig.update_yaxes(title_text=y_title, tickformat=tickformat)

    def _add_panel(key: str, title: str, fig: Any, description: str = "") -> None:
        nonlocal include_plotlyjs
        panel_figures[key] = _panel_figure_html(title, _figure_html(fig, include_plotlyjs=include_plotlyjs), description)
        include_plotlyjs = False

    products_fig = go.Figure()
    products_fig.add_trace(go.Bar(name="Products", x=days, y=daily_products, text=[f"{value:.0f}" for value in daily_products], textposition="outside", marker_color="#1d4e89"))
    _common_layout(products_fig, y_title="Count", x_title="Day")
    _add_panel("daily_products", "Daily Products", products_fig)

    scrap_rate_fig = go.Figure()
    scrap_rate_fig.add_trace(
        go.Scatter(
            name="Scrap Rate",
            x=days,
            y=daily_scrap_rate,
            mode="lines+markers+text",
            text=[_format_ratio_percent(value) for value in daily_scrap_rate],
            textposition="top center",
            line=dict(color="#3c6e71", width=3),
            marker=dict(size=8),
        )
    )
    _common_layout(scrap_rate_fig, y_title="Scrap rate", x_title="Day", tickformat=".0%")
    _add_panel("daily_scrap_rate", "Daily Scrap Rate", scrap_rate_fig)

    breakdowns_fig = go.Figure()
    breakdowns_fig.add_trace(go.Bar(name="Breakdowns", x=days, y=daily_breakdowns, text=[f"{value:.0f}" for value in daily_breakdowns], textposition="outside", marker_color="#edae49"))
    _common_layout(breakdowns_fig, y_title="Breakdowns", x_title="Day")
    _add_panel("daily_machine_breakdowns", "Daily Machine Breakdowns", breakdowns_fig)

    throughput_fig = go.Figure()
    throughput_fig.add_trace(go.Bar(name="Throughput", x=stage_labels, y=stage_values, text=[f"{value:.0f}" for value in stage_values], textposition="outside", marker_color="#00798c"))
    _common_layout(throughput_fig, y_title="Completed units", x_title="Stage")
    _add_panel("stage_throughput", "Stage Throughput", throughput_fig, "Inspection uses inspection-pass count.")

    machine_util_fig = go.Figure()
    machine_util_fig.add_trace(go.Bar(name="Utilization", x=machine_labels, y=machine_util_total, text=[_format_ratio_percent(value) for value in machine_util_total], textposition="outside", marker_color="#2a9d8f"))
    _common_layout(machine_util_fig, y_title="Utilization", x_title="Machine", tickformat=".0%")
    _add_panel("machine_utilization", "Machine Utilization", machine_util_fig, "Utilization = processing minutes / total simulated minutes.")

    machine_state_fig = go.Figure()
    machine_state_series = [
        ("Processing", "processing", "#2a9d8f"),
        ("Broken", "broken", "#e76f51"),
        ("PM", "pm", "#f4a261"),
        ("Setup", "setup", "#3a86ff"),
        ("Under Repair", "under_repair", "#8338ec"),
        ("Ready", "idle", "#ef476f"),
        ("Wait Input", "wait_input", "#8d99ae"),
        ("Wait Unload", "done_wait_unload", "#6c757d"),
    ]
    machine_totals = [0.0 for _ in machine_labels]
    for label, key, color in machine_state_series:
        series_values = [
            _safe_float((machine_state_by_machine.get(machine_id, {}) if isinstance(machine_state_by_machine.get(machine_id, {}), dict) else {}).get(key))
            for machine_id in machine_labels
        ]
        machine_totals = [current + value for current, value in zip(machine_totals, series_values)]
        machine_state_fig.add_trace(go.Bar(name=label, x=machine_labels, y=series_values, marker_color=color))
    machine_state_fig.add_trace(go.Scatter(name="Total", x=machine_labels, y=machine_totals, mode="text", text=[f"{value:.1f}m" for value in machine_totals], textposition="top center", showlegend=False))
    _common_layout(machine_state_fig, y_title="Minutes", x_title="Machine", barmode="stack", height=420)
    _add_panel(
        "machine_state_minutes",
        "Machine State Minutes",
        machine_state_fig,
        "WAIT INPUT means the machine cannot start because required input material or intermediate is missing. READY means all required inputs are already loaded and the machine is ready to start processing on the next lifecycle tick.",
    )

    buffer_fig = go.Figure()
    buffer_fig.add_trace(go.Bar(name="Avg wait", x=buffer_wait_labels, y=buffer_wait_values, text=[f"{value:.1f}m" for value in buffer_wait_values], textposition="outside", marker_color="#7b2cbf", hovertemplate="%{x}: %{y:.2f} min<extra></extra>"))
    _common_layout(buffer_fig, y_title="Average wait (min)", x_title="Buffer type")
    _add_panel(
        "buffer_waiting",
        "Buffer Waiting Avg Time",
        buffer_fig,
        "Average queue wait in minutes. This view includes both closed samples and items still open at the end of the run.",
    )

    buffer_count_fig = go.Figure()
    buffer_count_fig.add_trace(
        go.Bar(
            name="Closed samples",
            x=buffer_wait_labels,
            y=buffer_wait_closed_values,
            text=[f"{value:.0f}" for value in buffer_wait_closed_values],
            textposition="outside",
            marker_color="#118ab2",
        )
    )
    buffer_count_fig.add_trace(
        go.Bar(
            name="Open samples",
            x=buffer_wait_labels,
            y=buffer_wait_open_values,
            text=[f"{value:.0f}" for value in buffer_wait_open_values],
            textposition="outside",
            marker_color="#ef476f",
        )
    )
    _common_layout(buffer_count_fig, y_title="Sample count", x_title="Buffer type")
    _add_panel(
        "buffer_waiting_counts",
        "Buffer Waiting Sample Counts",
        buffer_count_fig,
        "Closed samples are items that exited the queue during the run. Open samples are items still waiting in the queue at the end of the run.",
    )

    worker_task_fig = go.Figure()
    worker_task_fig.add_trace(go.Bar(name="Task Minutes", x=task_types, y=task_values, text=[f"{value:.1f}m" for value in task_values], textposition="outside", marker_color="#90be6d"))
    _common_layout(worker_task_fig, y_title="Minutes", x_title="Task type")
    _add_panel("worker_task_minutes", "Worker Task Minutes", worker_task_fig, "Completed task minutes aggregated across workers.")

    worker_util_fig = go.Figure()
    worker_util_fig.add_trace(go.Bar(name="Utilization", x=worker_labels, y=worker_util_total, text=[_format_ratio_percent(value) for value in worker_util_total], textposition="outside", marker_color="#4361ee"))
    _common_layout(worker_util_fig, y_title="Utilization", x_title="Worker", tickformat=".0%")
    _add_panel("worker_utilization", "Worker Utilization", worker_util_fig, "Utilization = (working minutes + moving minutes) / total simulated minutes.")

    worker_state_fig = go.Figure()
    worker_state_series = [
        ("Working", worker_working_values, "#264653"),
        ("Moving", worker_moving_values, "#2a9d8f"),
        ("Discharged", worker_discharged_values, "#bc4749"),
        ("Idle", worker_idle_values, "#adb5bd"),
    ]
    worker_totals = [0.0 for _ in worker_labels]
    for label, values, color in worker_state_series:
        worker_totals = [current + value for current, value in zip(worker_totals, values)]
        worker_state_fig.add_trace(go.Bar(name=label, x=worker_labels, y=values, marker_color=color))
    worker_state_fig.add_trace(go.Scatter(name="Total", x=worker_labels, y=worker_totals, mode="text", text=[f"{value:.1f}m" for value in worker_totals], textposition="top center", showlegend=False))
    _common_layout(worker_state_fig, y_title="Minutes", x_title="Worker", barmode="stack", height=400)
    _add_panel(
        "worker_state_minutes",
        "Worker State Minutes",
        worker_state_fig,
        "Moving is counted from move intervals only. Working is task time after subtracting any overlap with move intervals, so transfer travel stays in Moving rather than being double-counted.",
    )

    current_run = _find_run(manifest, current_run_id)
    subtitle = "Quantitative run view with stable KPI cards, machine/worker utilization, and detailed charts."
    if isinstance(current_run, dict):
        subtitle = f"Quantitative view for {str(current_run.get('label', current_run_id or 'selected run'))}. KPI cards and tables stay fixed above the charts, and the charts are split by topic so each one has its own legend."
    item_section = _group_section(
        "Item Metrics",
        "Production outcome, downstream closure, item flow, and queue waiting time.",
        _summary_cards(kpi, METRIC_GROUPS["item"]),
        "<div class='grid cards-2'>"
        + panel_figures["daily_products"]
        + panel_figures["daily_scrap_rate"]
        + panel_figures["stage_throughput"]
        + panel_figures["buffer_waiting"]
        + panel_figures["buffer_waiting_counts"]
        + "</div>",
    )
    machine_section = _group_section(
        "Machine Metrics",
        "Machine utilization, machine-state distribution, and reliability-related signals.",
        _summary_cards(kpi, METRIC_GROUPS["machine"]),
        "<div class='grid cards-2'>"
        + _incident_table(kpi)
        + panel_figures["daily_machine_breakdowns"]
        + panel_figures["machine_utilization"]
        + panel_figures["machine_state_minutes"]
        + "</div>",
    )
    worker_section = _group_section(
        "Worker Metrics",
        "Worker availability, local response activity, task execution mix, and utilization.",
        _summary_cards(kpi, METRIC_GROUPS["worker"]),
        "<div class='grid cards-2'>"
        + panel_figures["worker_task_minutes"]
        + panel_figures["worker_utilization"]
        + panel_figures["worker_state_minutes"]
        + "</div>",
    )
    body_html = (
        _series_snapshot(manifest, current_run_id, kpi)
        + _run_horizon_cards(kpi, daily_summary, (current_run.get("run_meta", {}) if isinstance(current_run, dict) and isinstance(current_run.get("run_meta", {}), dict) else {}))
        + item_section
        + machine_section
        + worker_section
    )
    html_text = render_page_shell(
        title="ManSim KPI Dashboard",
        current_page_path=dashboard_path,
        manifest=manifest,
        manifest_path=manifest_path,
        current_artifact="kpi_dashboard.html",
        current_run_id=current_run_id,
        page_title="KPI Dashboard",
        page_subtitle=subtitle,
        body_html=body_html,
    )
    dashboard_path.write_text(html_text, encoding="utf-8")
    return dashboard_path
