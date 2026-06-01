from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from .shell import render_page_shell


PRIMARY_METRICS = [
    ("Ship Makespan", "makespan_min", "minutes", False, "Elapsed simulated minutes until every ship surface tile reaches COMPLETE."),
    ("Completed Surface Tiles", "completed_surface_tile_count", "count", True, "Ship exterior tiles that completed welding, surface prep, painting, and inspection."),
    ("Surface Completion", "surface_tile_completion_ratio", "ratio", True, "Share of ship exterior surface tiles in COMPLETE state."),
    ("Ship Reworks", "rework_count", "count", False, "Ship surface tile inspection failures that required rework."),
    ("Ship Quality Pass", "quality_pass_rate", "ratio", True, "Share of ship surface tile inspection attempts that passed."),
    ("Accepted Products", "total_products", "count", True, "Finished products accepted in this run."),
    ("Disposed Scrap", "disposed_scrap_count", "count", True, "Inspection-fail products delivered to ScrapDisposal."),
    ("Shelf Materials", "warehouse_material_shelf_count", "count", True, "Material items currently available on the shared warehouse shelf."),
    ("Shelf Restocks", "warehouse_material_restock_count", "count", True, "Material items restocked at initial fill or day boundary."),
    ("Closure Ratio", "downstream_closure_ratio", "ratio", True, "Share of downstream output that actually closed."),
    ("Throughput / Sim Hour", "throughput_per_sim_hour", "float", True, "Accepted products normalized by simulated hour."),
    ("Machine Utilization", "machine_utilization", "ratio", True, "Processing minutes divided by total machine-minutes."),
    ("Machine Broken Ratio", "machine_broken_ratio", "ratio", False, "Broken minutes divided by total machine-minutes."),
    ("Machine PM Ratio", "machine_pm_ratio", "ratio", False, "Preventive-maintenance minutes divided by total machine-minutes."),
    ("Humanoid Executing Ratio", "humanoid_execution_ratio_avg", "ratio", True, "Average share of worker-minutes with availability=EXECUTING."),
    ("Humanoid Blocked Ratio", "humanoid_blocked_ratio_avg", "ratio", False, "Average share of worker-minutes with availability=BLOCKED."),
    ("Humanoid Unavailable Ratio", "humanoid_unavailable_ratio_avg", "ratio", False, "Average share of worker-minutes with availability=DISABLED or OFFLINE."),
    ("Humanoid Incidents", "humanoid_incident_total", "count", False, "HumanoidSim incident events emitted by workers."),
    ("Handover Items", "handover_item_count", "count", True, "HANDOVER_ITEM executions where a humanoid joined product transport."),
    ("Shared Carry Time", "shared_product_carry_time_min", "minutes", True, "Product carry minutes after a second carrier joined."),
    ("Shared Carry Ratio", "shared_product_carry_ratio", "ratio", True, "Share of product carry time performed with two active carriers."),
    ("Repair Helper Joins", "repair_helper_join_count", "count", True, "Times a second humanoid joined an active machine repair."),
    ("Repair Collaboration Time", "repair_collaboration_time_min", "minutes", True, "Machine repair minutes with two or more humanoids working together."),
    ("Repair Collaboration Ratio", "repair_collaboration_ratio", "ratio", True, "Share of active repair time performed by a repair team larger than one."),
    ("Repair Team Size Avg", "repair_team_size_avg", "float", True, "Time-weighted average repair team size while repair was active."),
    ("Worker Local Responses", "worker_local_response_total", "count", True, "Local recoveries or local reorder attempts taken by workers."),
    ("Worker Discharged Ratio", "agent_discharged_ratio", "ratio", False, "Battery-depletion event time ratio."),
    ("Traffic Collisions", "collision_count", "count", False, "Tile/edge traffic conflicts with overlapping movement windows."),
    ("Traffic Near Misses", "near_miss_count", "count", False, "Movement conflicts closer than the configured traffic headway."),
    ("Edge Conflicts", "edge_conflict_count", "count", False, "Workers crossing the same edge in opposite directions during overlapping windows."),
    ("Path Overlaps", "path_overlap_count", "count", False, "Planned movement paths sharing tiles or edges."),
    ("Wall Clock", "wall_clock_sec", "duration", False, "Real wall-clock runtime for this run."),
    ("Coordination Incidents", "coordination_incident_total", "count", False, "Planning and execution mismatches."),
    ("Unique Replan Blockers", "unique_replan_blocker_total", "count", False, "Distinct blocker states that forced replanning."),
    ("Commitment Dispatches", "commitment_dispatch_total", "count", True, "Commitments that reached worker execution."),
    ("Rolling Windows", "rolling_horizon_window_count", "count", True, "Rolling-horizon planning windows started and visible in replay."),
    ("Rolling Dispatches", "rolling_horizon_dispatched_task_count", "count", True, "Tasks released by rolling-horizon dispatch."),
    ("Rolling Stale Skips", "rolling_horizon_stale_skipped_task_count", "count", False, "Rolling-horizon assignments skipped because the task became infeasible before execution."),
    ("Rolling Requeues", "rolling_horizon_requeued_task_count", "count", False, "Queued rolling-horizon tasks returned to the pool at a later window boundary."),
    ("Rolling Max Queue", "rolling_horizon_max_worker_queue_length", "count", True, "Largest number of not-yet-started rolling tasks queued for one worker."),
    ("Product Lead Time", "completed_product_lead_time_avg_min", "minutes", False, "Average accepted-product completion time."),
]

PRIMARY_METRIC_LOOKUP = {key: (label, key, kind, higher_is_better, description) for (label, key, kind, higher_is_better, description) in PRIMARY_METRICS}
METRIC_GROUPS = {
    "shipyard": ["makespan_min", "completed_surface_tile_count", "surface_tile_completion_ratio", "rework_count", "quality_pass_rate"],
    "item": ["total_products", "disposed_scrap_count", "warehouse_material_shelf_count", "downstream_closure_ratio", "throughput_per_sim_hour", "completed_product_lead_time_avg_min"],
    "machine": ["machine_utilization", "machine_broken_ratio", "machine_pm_ratio", "wall_clock_sec"],
    "worker": ["humanoid_execution_ratio_avg", "humanoid_blocked_ratio_avg", "humanoid_unavailable_ratio_avg", "worker_local_response_total", "commitment_dispatch_total"],
    "incidents": ["humanoid_incident_total", "humanoid_blocked_ratio_avg", "worker_local_response_total", "coordination_incident_total"],
    "collaboration": ["handover_item_count", "shared_product_carry_time_min", "shared_product_carry_ratio", "repair_helper_join_count", "repair_collaboration_time_min", "repair_collaboration_ratio", "repair_team_size_avg"],
    "traffic": ["collision_count", "near_miss_count", "edge_conflict_count", "path_overlap_count"],
    "decision": ["rolling_horizon_window_count", "rolling_horizon_dispatched_task_count", "rolling_horizon_requeued_task_count", "rolling_horizon_max_worker_queue_length", "rolling_horizon_stale_skipped_task_count", "commitment_dispatch_total"],
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


def _format_metric_value(raw_value: Any, kind: str) -> str:
    if raw_value is None or raw_value == "":
        return "pending" if kind == "minutes" else "-"
    return _format_metric(_safe_float(raw_value), kind)


def _format_ratio_percent(value: float) -> str:
    return f"{100.0 * float(value):.1f}%"


def _humanoid_state_axis_defs() -> dict[str, dict[str, Any]]:
    fallback = {
        "availability": {"name": "Availability State", "states": ["AVAILABLE", "ASSIGNED", "EXECUTING", "WAITING", "BLOCKED", "OFFLINE", "DISABLED"]},
        "mobility": {"name": "Mobility State", "states": ["STATIONARY", "NAVIGATING", "DOCKING"]},
        "power": {"name": "Power State", "states": ["POWER_NORMAL", "POWER_LOW", "POWER_CRITICAL", "DEPLETED", "CHARGING"]},
        "manipulation": {"name": "Manipulation State", "states": ["FREE", "REACHING", "HOLDING", "PLACING"]},
    }
    try:
        from humanoidsim import load_state_schema

        schema = load_state_schema()
        return {
            axis_id: {
                "name": axis.name,
                "states": list(axis.states.keys()),
            }
            for axis_id, axis in schema.axes.items()
        }
    except Exception:
        return fallback


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
        raw_value = kpi.get(key)
        cards.append(
            f"<div class='card'><div class='label'>{html.escape(label)}</div><div class='value'>{html.escape(_format_metric_value(raw_value, kind))}</div><div class='sub'>{html.escape(description)}</div></div>"
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
        ("Humanoid incidents", _safe_int(kpi.get("humanoid_incident_total")), "HumanoidSim incident taxonomy events from worker execution."),
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
    state_by_worker = kpi.get("humanoid_state_time_by_worker", {}) if isinstance(kpi.get("humanoid_state_time_by_worker", {}), dict) else {}
    execution_by_worker = kpi.get("humanoid_execution_ratio_by_worker", {}) if isinstance(kpi.get("humanoid_execution_ratio_by_worker", {}), dict) else {}
    unavailable_by_worker = kpi.get("humanoid_unavailable_ratio_by_worker", {}) if isinstance(kpi.get("humanoid_unavailable_ratio_by_worker", {}), dict) else {}
    rows = [("Overall executing", _safe_float(kpi.get("humanoid_execution_ratio_avg")))]
    body = "".join(
        f"<tr><td>{html.escape(worker_id)}</td><td>{_format_ratio_percent(executing)}</td><td>{_format_ratio_percent(unavailable)}</td></tr>"
        for worker_id, executing, unavailable in (
            [(rows[0][0], rows[0][1], _safe_float(kpi.get("humanoid_unavailable_ratio_avg")))]
            + [
                (
                    worker_id,
                    _safe_float(execution_by_worker.get(worker_id)),
                    _safe_float(unavailable_by_worker.get(worker_id)),
                )
                for worker_id in _sorted_worker_ids(state_by_worker)
            ]
        )
    )
    if not body:
        body = "<tr><td colspan='3'>No per-worker humanoid state data.</td></tr>"
    return "<div class='panel'><h2>Humanoid Availability Ratios</h2><p class='muted'>Ratios are derived from HumanoidSim Availability State only.</p><table><thead><tr><th>Worker</th><th>EXECUTING</th><th>DISABLED/OFFLINE</th></tr></thead><tbody>" + body + "</tbody></table></div>"


def _traffic_table(kpi: dict[str, Any]) -> str:
    by_pair = kpi.get("traffic_conflicts_by_worker_pair", {}) if isinstance(kpi.get("traffic_conflicts_by_worker_pair", {}), dict) else {}
    rows = "".join(
        f"<tr><td>{html.escape(str(pair))}</td><td>{_safe_int(count)}</td></tr>"
        for pair, count in sorted(by_pair.items(), key=lambda item: (-_safe_int(item[1]), str(item[0])))
    )
    if not rows:
        rows = "<tr><td colspan='2'>No worker-pair traffic conflicts.</td></tr>"
    return "<div class='panel'><h2>Traffic Conflicts by Worker Pair</h2><p class='muted'>Pairs are recorded directly from AGENT_TRAFFIC_CONFLICT events.</p><table><thead><tr><th>Worker Pair</th><th>Conflicts</th></tr></thead><tbody>" + rows + "</tbody></table></div>"


def _rolling_horizon_table(kpi: dict[str, Any]) -> str:
    payload = kpi.get("rolling_horizon", {}) if isinstance(kpi.get("rolling_horizon", {}), dict) else {}
    dedicated_summary = (
        payload.get("dedicated_role_summary", {})
        if isinstance(payload.get("dedicated_role_summary", {}), dict)
        else {}
    )
    rows = [
        ("Enabled", "yes" if bool(payload.get("enabled", False)) else "no"),
        ("Dedicated Roles", "yes" if bool(payload.get("dedicated_roles", False)) else "no"),
        ("Window", f"{_safe_float(payload.get('window_min')):.1f} min"),
        ("Priority Scope", "HumanoidSim task_code"),
        ("Dispatch Policy", str(payload.get("dispatch_policy", "-"))),
        ("Pending Candidates", str(_safe_int(payload.get("pending_candidate_count")))),
        ("Queued Dispatches", str(_safe_int(payload.get("queued_dispatch_count")))),
        ("Requeued Tasks", str(_safe_int(payload.get("requeued_task_count")))),
        ("Max Worker Queue", str(_safe_int(payload.get("max_worker_queue_length")))),
        ("Role Violations", str(_safe_int(dedicated_summary.get("role_violation_count")))),
        ("Handover Dispatches", str(_safe_int(dedicated_summary.get("handover_dispatch_count")))),
        ("A1 Battery Deliveries", str(_safe_int(dedicated_summary.get("battery_delivery_from_provider_count")))),
    ]
    body = "".join(f"<tr><td>{html.escape(label)}</td><td>{html.escape(value)}</td></tr>" for label, value in rows)
    return "<div class='panel'><h2>Rolling Horizon Dispatch</h2><p class='muted'>This section is populated for rolling_horizon_aging_priority and rolling_horizon_dedicated_roles.</p><table><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>" + body + "</tbody></table></div>"


def _humanoid_incident_recovery_table(kpi: dict[str, Any]) -> str:
    by_code = kpi.get("humanoid_incidents_by_code", {}) if isinstance(kpi.get("humanoid_incidents_by_code", {}), dict) else {}
    protocols = (
        kpi.get("humanoid_incident_recovery_protocol_by_code", {})
        if isinstance(kpi.get("humanoid_incident_recovery_protocol_by_code", {}), dict)
        else {}
    )
    rows: list[str] = []
    for code, count in sorted(by_code.items(), key=lambda item: (-_safe_int(item[1]), str(item[0]))):
        protocol = protocols.get(code, [])
        if isinstance(protocol, list):
            protocol_steps = []
            for step in protocol:
                if isinstance(step, dict):
                    step_code = str(step.get("code", "")).strip()
                    if step_code:
                        protocol_steps.append(step_code)
                elif str(step).strip():
                    protocol_steps.append(str(step))
            protocol_text = " -> ".join(protocol_steps) or "-"
        else:
            protocol_text = str(protocol or "-")
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(code))}</td>"
            f"<td>{_safe_int(count)}</td>"
            f"<td>{html.escape(protocol_text)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append("<tr><td colspan='3'>No humanoid incidents were recorded.</td></tr>")
    return (
        "<div class='panel'>"
        "<h2>Humanoid Incident Recovery Protocols</h2>"
        "<p class='muted'>Recovery sequences come from HumanoidSim incident_schema_core.json and use existing task or primitive codes.</p>"
        "<table><thead><tr><th>Incident Code</th><th>Count</th><th>Recovery Protocol</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


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
    scenario_type = str(kpi.get("scenario_type", "")).strip()
    is_shipyard = scenario_type == "shipyard_basic"

    days = [int(d["day"]) for d in daily_summary]
    daily_products = [float(d.get("products", 0.0) or 0.0) for d in daily_summary]
    daily_scrap_rate = [float(d.get("scrap_rate", 0.0) or 0.0) for d in daily_summary]
    daily_breakdowns = [float(d.get("machine_breakdowns", 0.0) or 0.0) for d in daily_summary]

    stage_tp = kpi.get("stage_throughput", {}) if isinstance(kpi.get("stage_throughput", {}), dict) else {}
    stage_labels = list(stage_tp.keys())
    stage_values = [float(stage_tp.get(label, 0.0) or 0.0) for label in stage_labels]

    humanoid_task_minutes = kpi.get("humanoid_task_minutes", {}) if isinstance(kpi.get("humanoid_task_minutes", {}), dict) else {}
    task_pairs = sorted(((str(task_type), float(minutes)) for task_type, minutes in humanoid_task_minutes.items()), key=lambda item: item[1], reverse=True)
    task_types = [task_type for task_type, _minutes in task_pairs]
    task_values = [minutes for _task_type, minutes in task_pairs]
    task_taxonomy = kpi.get("humanoid_task_taxonomy", {}) if isinstance(kpi.get("humanoid_task_taxonomy", {}), dict) else {}
    task_by_level = task_taxonomy.get("by_level", {}) if isinstance(task_taxonomy.get("by_level", {}), dict) else {}
    task_by_category = task_taxonomy.get("by_category", {}) if isinstance(task_taxonomy.get("by_category", {}), dict) else {}
    primitive_minutes = kpi.get("humanoid_primitive_minutes", {}) if isinstance(kpi.get("humanoid_primitive_minutes", {}), dict) else {}

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

    humanoid_state_by_worker = kpi.get("humanoid_state_time_by_worker", {}) if isinstance(kpi.get("humanoid_state_time_by_worker", {}), dict) else {}
    humanoid_execution_by_worker = kpi.get("humanoid_execution_ratio_by_worker", {}) if isinstance(kpi.get("humanoid_execution_ratio_by_worker", {}), dict) else {}
    worker_labels = _sorted_worker_ids(humanoid_state_by_worker)
    worker_execution_values = [_safe_float(humanoid_execution_by_worker.get(worker_id)) for worker_id in worker_labels]
    axis_defs = _humanoid_state_axis_defs()
    traffic_by_type = kpi.get("traffic_conflicts_by_type", {}) if isinstance(kpi.get("traffic_conflicts_by_type", {}), dict) else {}
    traffic_by_pair = kpi.get("traffic_conflicts_by_worker_pair", {}) if isinstance(kpi.get("traffic_conflicts_by_worker_pair", {}), dict) else {}
    humanoid_incidents_by_category = (
        kpi.get("humanoid_incidents_by_category", {}) if isinstance(kpi.get("humanoid_incidents_by_category", {}), dict) else {}
    )
    humanoid_incidents_by_code = (
        kpi.get("humanoid_incidents_by_code", {}) if isinstance(kpi.get("humanoid_incidents_by_code", {}), dict) else {}
    )
    humanoid_incidents_by_worker = (
        kpi.get("humanoid_incidents_by_worker", {}) if isinstance(kpi.get("humanoid_incidents_by_worker", {}), dict) else {}
    )
    humanoid_incidents_by_severity = (
        kpi.get("humanoid_incidents_by_severity", {}) if isinstance(kpi.get("humanoid_incidents_by_severity", {}), dict) else {}
    )
    item_transport_time = kpi.get("item_transport_time_by_type", {}) if isinstance(kpi.get("item_transport_time_by_type", {}), dict) else {}
    shared_carry_by_worker = kpi.get("shared_product_carry_time_by_worker", {}) if isinstance(kpi.get("shared_product_carry_time_by_worker", {}), dict) else {}
    shared_carry_by_pair = kpi.get("shared_product_carry_time_by_pair", {}) if isinstance(kpi.get("shared_product_carry_time_by_pair", {}), dict) else {}
    repair_team_time_by_size = kpi.get("repair_team_time_by_size", {}) if isinstance(kpi.get("repair_team_time_by_size", {}), dict) else {}
    repair_collab_by_machine = kpi.get("repair_collaboration_time_by_machine", {}) if isinstance(kpi.get("repair_collaboration_time_by_machine", {}), dict) else {}
    repair_helper_by_machine = kpi.get("repair_helper_join_count_by_machine", {}) if isinstance(kpi.get("repair_helper_join_count_by_machine", {}), dict) else {}

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
    product_trace_name = "Surface Tiles" if is_shipyard else "Products"
    product_panel_title = "Daily Surface Tiles" if is_shipyard else "Daily Products"
    products_fig.add_trace(
        go.Bar(
            name=product_trace_name,
            x=days,
            y=daily_products,
            text=[f"{value:.0f}" for value in daily_products],
            textposition="outside",
            marker_color="#1d4e89",
        )
    )
    _common_layout(products_fig, y_title="Count", x_title="Day")
    _add_panel("daily_products", product_panel_title, products_fig)

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
    _common_layout(worker_task_fig, y_title="Minutes", x_title="Humanoid task code")
    _add_panel("worker_task_minutes", "Humanoid Task Minutes", worker_task_fig, "Completed task minutes by HumanoidSim task code.")

    worker_util_fig = go.Figure()
    worker_util_fig.add_trace(go.Bar(name="EXECUTING", x=worker_labels, y=worker_execution_values, text=[_format_ratio_percent(value) for value in worker_execution_values], textposition="outside", marker_color="#4361ee"))
    _common_layout(worker_util_fig, y_title="Ratio", x_title="Worker", tickformat=".0%")
    _add_panel("worker_utilization", "Humanoid Executing Ratio", worker_util_fig, "Execution ratio is availability.EXECUTING / total worker time.")

    task_level_fig = go.Figure()
    task_level_fig.add_trace(go.Bar(name="Task Level", x=list(task_by_level.keys()), y=[_safe_float(v) for v in task_by_level.values()], marker_color="#4d908e"))
    _common_layout(task_level_fig, y_title="Minutes", x_title="HumanoidSim level")
    _add_panel("humanoid_task_level_minutes", "Humanoid Task Minutes by Level", task_level_fig, "Grouped only by HumanoidSim TaskSpec.level.")

    task_category_fig = go.Figure()
    task_category_fig.add_trace(go.Bar(name="Task Category", x=list(task_by_category.keys()), y=[_safe_float(v) for v in task_by_category.values()], marker_color="#f8961e"))
    _common_layout(task_category_fig, y_title="Minutes", x_title="HumanoidSim category", height=430)
    _add_panel("humanoid_task_category_minutes", "Humanoid Task Minutes by Category", task_category_fig, "Grouped only by HumanoidSim catalog category.")

    primitive_fig = go.Figure()
    primitive_pairs = sorted(((str(key), _safe_float(value)) for key, value in primitive_minutes.items()), key=lambda item: item[1], reverse=True)
    primitive_fig.add_trace(go.Bar(name="Primitive", x=[key for key, _ in primitive_pairs], y=[value for _, value in primitive_pairs], marker_color="#577590"))
    _common_layout(primitive_fig, y_title="Minutes", x_title="Primitive call code", height=430)
    _add_panel("humanoid_primitive_minutes", "Humanoid Primitive Minutes", primitive_fig, "Primitive time is paired from HUMANOID_STEP_START/END events.")

    transport_fig = go.Figure()
    transport_pairs = sorted(((str(key), _safe_float(value)) for key, value in item_transport_time.items()), key=lambda item: item[0])
    transport_fig.add_trace(
        go.Bar(
            name="Transport minutes",
            x=[key for key, _ in transport_pairs],
            y=[value for _, value in transport_pairs],
            text=[f"{value:.1f}m" for _, value in transport_pairs],
            textposition="outside",
            marker_color="#00a896",
        )
    )
    _common_layout(transport_fig, y_title="Minutes", x_title="Carried item type", height=360)
    _add_panel(
        "item_transport_time_by_type",
        "Item Transport Time by Type",
        transport_fig,
        "Loaded movement time uses ManSim item weight multipliers: material 1.0, intermediate 1.5, product 2.0 divided by active product carriers.",
    )

    product_collab_fig = go.Figure()
    product_collab_fig.add_trace(
        go.Bar(
            name="Solo product carry",
            x=["Product carry"],
            y=[_safe_float(kpi.get("solo_product_carry_time_min"))],
            text=[f"{_safe_float(kpi.get('solo_product_carry_time_min')):.1f}m"],
            textposition="outside",
            marker_color="#8d99ae",
        )
    )
    product_collab_fig.add_trace(
        go.Bar(
            name="Shared product carry",
            x=["Product carry"],
            y=[_safe_float(kpi.get("shared_product_carry_time_min"))],
            text=[f"{_safe_float(kpi.get('shared_product_carry_time_min')):.1f}m"],
            textposition="outside",
            marker_color="#2a9d8f",
        )
    )
    _common_layout(product_collab_fig, y_title="Minutes", x_title="", barmode="stack", height=330)
    _add_panel(
        "product_carry_collaboration",
        "Product Carry Collaboration",
        product_collab_fig,
        "Product transport minutes split into solo carry and shared carry after HANDOVER_ITEM joined a session.",
    )

    collaboration_counts_fig = go.Figure()
    collaboration_count_labels = ["Handover joins", "Shared carry completed", "Repair helper joins"]
    collaboration_count_values = [
        _safe_float(kpi.get("handover_item_count")),
        _safe_float(kpi.get("shared_product_carry_completed_count")),
        _safe_float(kpi.get("repair_helper_join_count")),
    ]
    collaboration_counts_fig.add_trace(
        go.Bar(
            name="Collaboration events",
            x=collaboration_count_labels,
            y=collaboration_count_values,
            text=[f"{value:.0f}" for value in collaboration_count_values],
            textposition="outside",
            marker_color="#f8961e",
        )
    )
    _common_layout(collaboration_counts_fig, y_title="Count", x_title="", height=330)
    _add_panel(
        "collaboration_event_counts",
        "Collaboration Event Counts",
        collaboration_counts_fig,
        "Counts only explicit collaboration events: product handover joins, shared product transports, and repair helper joins.",
    )

    shared_carry_worker_fig = go.Figure()
    shared_worker_pairs = sorted(((str(key), _safe_float(value)) for key, value in shared_carry_by_worker.items()), key=lambda item: item[0])
    shared_carry_worker_fig.add_trace(
        go.Bar(
            name="Shared carry minutes",
            x=[key for key, _ in shared_worker_pairs],
            y=[value for _, value in shared_worker_pairs],
            text=[f"{value:.1f}m" for _, value in shared_worker_pairs],
            textposition="outside",
            marker_color="#00a896",
        )
    )
    _common_layout(shared_carry_worker_fig, y_title="Minutes", x_title="Worker", height=330)
    _add_panel(
        "shared_carry_time_by_worker",
        "Shared Carry Time by Worker",
        shared_carry_worker_fig,
        "Each worker receives the shared-carry interval minutes for product transports they participated in.",
    )

    shared_carry_pair_fig = go.Figure()
    shared_pair_values = sorted(((str(key), _safe_float(value)) for key, value in shared_carry_by_pair.items()), key=lambda item: item[1], reverse=True)
    shared_carry_pair_fig.add_trace(
        go.Bar(
            name="Shared carry minutes",
            x=[key for key, _ in shared_pair_values],
            y=[value for _, value in shared_pair_values],
            text=[f"{value:.1f}m" for _, value in shared_pair_values],
            textposition="outside",
            marker_color="#4361ee",
        )
    )
    _common_layout(shared_carry_pair_fig, y_title="Minutes", x_title="Worker pair", height=330)
    _add_panel(
        "shared_carry_time_by_pair",
        "Shared Carry Time by Worker Pair",
        shared_carry_pair_fig,
        "Pairs are the carrier ids recorded in PRODUCT_CARRY_COMPLETED events, without extra dashboard grouping.",
    )

    repair_team_size_fig = go.Figure()
    repair_size_pairs = sorted(((str(key), _safe_float(value)) for key, value in repair_team_time_by_size.items()), key=lambda item: int(item[0]) if item[0].isdigit() else 999)
    repair_team_size_fig.add_trace(
        go.Bar(
            name="Repair minutes",
            x=[f"{key} worker" if key == "1" else f"{key} workers" for key, _ in repair_size_pairs],
            y=[value for _, value in repair_size_pairs],
            text=[f"{value:.1f}m" for _, value in repair_size_pairs],
            textposition="outside",
            marker_color="#8338ec",
        )
    )
    _common_layout(repair_team_size_fig, y_title="Minutes", x_title="Repair team size", height=330)
    _add_panel(
        "repair_team_time_by_size",
        "Repair Team Time by Size",
        repair_team_size_fig,
        "Repair time is integrated between repair team events. Team size > 1 is counted as collaboration.",
    )

    repair_machine_fig = go.Figure()
    repair_machine_labels = sorted(set(str(key) for key in repair_collab_by_machine.keys()) | set(str(key) for key in repair_helper_by_machine.keys()), key=_machine_sort_key)
    repair_machine_fig.add_trace(
        go.Bar(
            name="Collaboration minutes",
            x=repair_machine_labels,
            y=[_safe_float(repair_collab_by_machine.get(machine_id)) for machine_id in repair_machine_labels],
            text=[f"{_safe_float(repair_collab_by_machine.get(machine_id)):.1f}m" for machine_id in repair_machine_labels],
            textposition="outside",
            marker_color="#7b2cbf",
        )
    )
    repair_machine_fig.add_trace(
        go.Scatter(
            name="Helper joins",
            x=repair_machine_labels,
            y=[_safe_float(repair_helper_by_machine.get(machine_id)) for machine_id in repair_machine_labels],
            mode="lines+markers+text",
            text=[f"{_safe_float(repair_helper_by_machine.get(machine_id)):.0f}" for machine_id in repair_machine_labels],
            textposition="top center",
            yaxis="y2",
            line=dict(color="#e76f51", width=3),
            marker=dict(size=8),
        )
    )
    _common_layout(repair_machine_fig, y_title="Collaboration minutes", x_title="Machine", height=360)
    repair_machine_fig.update_layout(yaxis2=dict(title="Helper joins", overlaying="y", side="right", rangemode="tozero"))
    _add_panel(
        "repair_collaboration_by_machine",
        "Repair Collaboration by Machine",
        repair_machine_fig,
        "Bars show repair minutes with team size above one; the line shows helper join counts.",
    )

    traffic_type_fig = go.Figure()
    traffic_type_pairs = sorted(((str(key), _safe_float(value)) for key, value in traffic_by_type.items()), key=lambda item: item[0])
    traffic_type_fig.add_trace(
        go.Bar(
            name="Traffic conflicts",
            x=[key for key, _ in traffic_type_pairs],
            y=[value for _, value in traffic_type_pairs],
            text=[f"{value:.0f}" for _, value in traffic_type_pairs],
            textposition="outside",
            marker_color="#e76f51",
        )
    )
    _common_layout(traffic_type_fig, y_title="Count", x_title="Conflict type", height=380)
    _add_panel("traffic_conflicts_by_type", "Traffic Conflicts by Type", traffic_type_fig, "Movement conflicts are observed and logged; v1 does not optimize or suppress movement.")

    traffic_pair_fig = go.Figure()
    traffic_pair_pairs = sorted(((str(key), _safe_float(value)) for key, value in traffic_by_pair.items()), key=lambda item: item[1], reverse=True)
    traffic_pair_fig.add_trace(
        go.Bar(
            name="Worker pair conflicts",
            x=[key for key, _ in traffic_pair_pairs],
            y=[value for _, value in traffic_pair_pairs],
            text=[f"{value:.0f}" for _, value in traffic_pair_pairs],
            textposition="outside",
            marker_color="#f4a261",
        )
    )
    _common_layout(traffic_pair_fig, y_title="Count", x_title="Worker pair", height=360)
    _add_panel("traffic_conflicts_by_pair", "Traffic Conflicts by Worker Pair", traffic_pair_fig, "Pairs are not grouped beyond the worker ids recorded by the simulator.")

    incident_category_fig = go.Figure()
    incident_category_pairs = sorted(
        ((str(key), _safe_float(value)) for key, value in humanoid_incidents_by_category.items()),
        key=lambda item: item[0],
    )
    incident_category_fig.add_trace(
        go.Bar(
            name="Incidents",
            x=[key for key, _ in incident_category_pairs],
            y=[value for _, value in incident_category_pairs],
            text=[f"{value:.0f}" for _, value in incident_category_pairs],
            textposition="outside",
            marker_color="#9d4edd",
        )
    )
    _common_layout(incident_category_fig, y_title="Count", x_title="HumanoidSim incident category", height=390)
    _add_panel(
        "humanoid_incidents_by_category",
        "Humanoid Incidents by Category",
        incident_category_fig,
        "Categories are defined by HumanoidSim incident_schema_core.json; no dashboard-specific regrouping is applied.",
    )

    incident_code_fig = go.Figure()
    incident_code_pairs = sorted(
        ((str(key), _safe_float(value)) for key, value in humanoid_incidents_by_code.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    incident_code_fig.add_trace(
        go.Bar(
            name="Incidents",
            x=[key for key, _ in incident_code_pairs],
            y=[value for _, value in incident_code_pairs],
            text=[f"{value:.0f}" for _, value in incident_code_pairs],
            textposition="outside",
            marker_color="#e76f51",
        )
    )
    _common_layout(incident_code_fig, y_title="Count", x_title="HumanoidSim incident code", height=430)
    _add_panel(
        "humanoid_incidents_by_code",
        "Humanoid Incidents by Code",
        incident_code_fig,
        "Exact incident code counts emitted by HUMANOID_INCIDENT events.",
    )

    incident_worker_fig = go.Figure()
    incident_worker_pairs = sorted(
        ((str(key), _safe_float(value)) for key, value in humanoid_incidents_by_worker.items()),
        key=lambda item: item[0],
    )
    incident_worker_fig.add_trace(
        go.Bar(
            name="Incidents",
            x=[key for key, _ in incident_worker_pairs],
            y=[value for _, value in incident_worker_pairs],
            text=[f"{value:.0f}" for _, value in incident_worker_pairs],
            textposition="outside",
            marker_color="#f4a261",
        )
    )
    _common_layout(incident_worker_fig, y_title="Count", x_title="Worker", height=340)
    _add_panel(
        "humanoid_incidents_by_worker",
        "Humanoid Incidents by Worker",
        incident_worker_fig,
        "Worker-level incident counts use the event entity id recorded by ManSim.",
    )

    incident_severity_fig = go.Figure()
    incident_severity_pairs = sorted(
        ((str(key), _safe_float(value)) for key, value in humanoid_incidents_by_severity.items()),
        key=lambda item: item[0],
    )
    incident_severity_fig.add_trace(
        go.Bar(
            name="Incidents",
            x=[key for key, _ in incident_severity_pairs],
            y=[value for _, value in incident_severity_pairs],
            text=[f"{value:.0f}" for _, value in incident_severity_pairs],
            textposition="outside",
            marker_color="#577590",
        )
    )
    _common_layout(incident_severity_fig, y_title="Count", x_title="Severity", height=340)
    _add_panel(
        "humanoid_incidents_by_severity",
        "Humanoid Incidents by Severity",
        incident_severity_fig,
        "Severity labels come from HumanoidSim incident definitions.",
    )

    for axis_id, axis_def in axis_defs.items():
        state_fig = go.Figure()
        rendered_states: set[str] = set()
        for state_name in axis_def.get("states", []):
            values = [
                _safe_float(
                    (
                        (humanoid_state_by_worker.get(worker_id, {}) if isinstance(humanoid_state_by_worker.get(worker_id, {}), dict) else {})
                        .get(axis_id, {})
                        if isinstance((humanoid_state_by_worker.get(worker_id, {}) if isinstance(humanoid_state_by_worker.get(worker_id, {}), dict) else {}).get(axis_id, {}), dict)
                        else {}
                    ).get(state_name)
                )
                for worker_id in worker_labels
            ]
            if any(value > 0 for value in values):
                state_fig.add_trace(go.Bar(name=state_name, x=worker_labels, y=values))
                rendered_states.add(str(state_name))
        for state_name in axis_def.get("states", []):
            if str(state_name) in rendered_states:
                continue
            state_fig.add_trace(
                go.Bar(
                    name=str(state_name),
                    x=worker_labels or ["-"],
                    y=[0.0 for _ in (worker_labels or ["-"])],
                    visible="legendonly",
                    showlegend=True,
                    hoverinfo="skip",
                )
            )
        _common_layout(state_fig, y_title="Minutes", x_title="Worker", barmode="stack", height=390)
        _add_panel(
            f"humanoid_state_{axis_id}",
            str(axis_def.get("name", axis_id)),
            state_fig,
            "State order and grouping follow HumanoidSim state_schema_core.json.",
        )

    current_run = _find_run(manifest, current_run_id)
    subtitle = "Quantitative run view with stable KPI cards, machine/worker utilization, and detailed charts."
    if isinstance(current_run, dict):
        subtitle = f"Quantitative view for {str(current_run.get('label', current_run_id or 'selected run'))}. KPI cards and tables stay fixed above the charts, and the charts are split by topic so each one has its own legend."
    item_section = _group_section(
        "Shipyard Surface Metrics" if is_shipyard else "Item Metrics",
        "Ship exterior surface-tile completion, rework, quality, and makespan." if is_shipyard else "Production outcome, downstream closure, item flow, and queue waiting time.",
        _summary_cards(kpi, METRIC_GROUPS["shipyard"] if is_shipyard else METRIC_GROUPS["item"]),
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
        "HumanoidSim state axes, task taxonomy, primitive execution, and local response activity.",
        _summary_cards(kpi, METRIC_GROUPS["worker"]),
        "<div class='grid cards-2'>"
        + panel_figures["worker_task_minutes"]
        + panel_figures["worker_utilization"]
        + panel_figures["humanoid_task_level_minutes"]
        + panel_figures["humanoid_task_category_minutes"]
        + panel_figures["humanoid_primitive_minutes"]
        + panel_figures["item_transport_time_by_type"]
        + panel_figures["humanoid_state_availability"]
        + panel_figures["humanoid_state_mobility"]
        + panel_figures["humanoid_state_power"]
        + panel_figures["humanoid_state_manipulation"]
        + "</div>",
    )
    incident_section = _group_section(
        "Humanoid Incidents",
        "Humanoid incidents are generated from the HumanoidSim incident taxonomy and recorded as StateReason plus recovery protocol.",
        _summary_cards(kpi, METRIC_GROUPS["incidents"]),
        "<div class='grid cards-2'>"
        + panel_figures["humanoid_incidents_by_category"]
        + panel_figures["humanoid_incidents_by_code"]
        + panel_figures["humanoid_incidents_by_worker"]
        + panel_figures["humanoid_incidents_by_severity"]
        + _humanoid_incident_recovery_table(kpi)
        + "</div>",
    )
    collaboration_section = _group_section(
        "Worker Collaboration",
        "Collaboration KPIs are derived from explicit handover/product-carry and repair-team events rather than inferred from proximity.",
        _summary_cards(kpi, METRIC_GROUPS["collaboration"]),
        "<div class='grid cards-2'>"
        + panel_figures["collaboration_event_counts"]
        + panel_figures["product_carry_collaboration"]
        + panel_figures["shared_carry_time_by_worker"]
        + panel_figures["shared_carry_time_by_pair"]
        + panel_figures["repair_team_time_by_size"]
        + panel_figures["repair_collaboration_by_machine"]
        + "</div>",
    )
    traffic_section = _group_section(
        "Movement / Traffic Safety",
        "Observed route overlap, near-miss, tile conflict, and edge conflict events. These are reproduced for inspection, not minimized by policy in this version.",
        _summary_cards(kpi, METRIC_GROUPS["traffic"]),
        "<div class='grid cards-2'>"
        + panel_figures["traffic_conflicts_by_type"]
        + panel_figures["traffic_conflicts_by_pair"]
        + _traffic_table(kpi)
        + "</div>",
    )
    decision_section = _group_section(
        "Decision / Dispatch",
        "Decision-mode dispatch metrics, including rolling-horizon windows when a rolling mode is active.",
        _summary_cards(kpi, METRIC_GROUPS["decision"]),
        "<div class='grid cards-2'>" + _rolling_horizon_table(kpi) + "</div>",
    )
    body_html = (
        _series_snapshot(manifest, current_run_id, kpi)
        + _run_horizon_cards(kpi, daily_summary, (current_run.get("run_meta", {}) if isinstance(current_run, dict) and isinstance(current_run.get("run_meta", {}), dict) else {}))
        + item_section
        + machine_section
        + worker_section
        + decision_section
        + incident_section
        + collaboration_section
        + traffic_section
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
