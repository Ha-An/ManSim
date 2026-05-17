from __future__ import annotations

import csv
import html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .artifact_meta import add_plotly_meta_header
from .shell import render_page_shell


AVAILABILITY_STATES = [
    "AVAILABLE",
    "ASSIGNED",
    "EXECUTING",
    "WAITING",
    "BLOCKED",
    "OFFLINE",
    "DISABLED",
]

def _details(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("details", {})
    return data if isinstance(data, dict) else {}


def _humanoid_state(event: dict[str, Any]) -> dict[str, Any] | None:
    state = _details(event).get("humanoid_state")
    return state if isinstance(state, dict) else None


def _is_worker_id(raw: Any) -> bool:
    text = str(raw or "").strip()
    return len(text) > 1 and text[0].upper() == "A" and text[1:].isdigit()


def _worker_sort_key(raw: str) -> tuple[int, str]:
    text = str(raw)
    suffix = text[1:] if text.upper().startswith("A") else text
    if suffix.isdigit():
        return (0, f"{int(suffix):06d}")
    return (1, text)


def _entity_key(event: dict[str, Any]) -> tuple[str, str]:
    return (str(event.get("entity_id", "")), "default")


def _task_key(event: dict[str, Any]) -> tuple[str, str]:
    d = _details(event)
    task_id = d.get("task_id") or "default"
    return (str(event.get("entity_id", "")), str(task_id))


def _cycle_key(event: dict[str, Any]) -> tuple[str, str]:
    d = _details(event)
    cycle_id = d.get("cycle_id") or "default"
    return (str(event.get("entity_id", "")), str(cycle_id))


def _pair_intervals(
    events: list[dict[str, Any]],
    start_type: str,
    end_types: str | Iterable[str],
    key_fn: Any,
    status_label: str,
    interval_type_fn: Any,
    entity_group: str,
) -> list[dict[str, Any]]:
    end_type_set = {end_types} if isinstance(end_types, str) else set(end_types)
    active: dict[tuple[str, str], dict[str, Any]] = {}
    intervals: list[dict[str, Any]] = []

    for event in events:
        et = str(event.get("type", ""))
        key = key_fn(event)

        if et == start_type:
            active[key] = event
            continue

        if et not in end_type_set:
            continue
        start_event = active.pop(key, None)
        if start_event is None:
            continue

        start_t = float(start_event.get("t", 0.0))
        end_t = float(event.get("t", 0.0))
        if end_t <= start_t:
            continue

        intervals.append(
            {
                "lane": str(start_event.get("entity_id", "")),
                "entity_group": entity_group,
                "status": status_label,
                "start": start_t,
                "end": end_t,
                "duration": end_t - start_t,
                "interval_type": str(interval_type_fn(start_event, event)),
                "start_event": start_event,
                "end_event": event,
            }
        )

    return intervals


def _state_context(state: dict[str, Any]) -> dict[str, str]:
    context = state.get("task_context")
    context = context if isinstance(context, dict) else {}
    reason = state.get("reason")
    reason = reason if isinstance(reason, dict) else {}
    metadata = state.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "task_code": str(context.get("task_code") or ""),
        "task_instance_id": str(context.get("task_instance_id") or ""),
        "step_id": str(context.get("step_id") or ""),
        "primitive_call_code": str(context.get("primitive_call_code") or ""),
        "execution_status": str(context.get("execution_status") or ""),
        "reason_code": str(reason.get("code") or ""),
        "reason_message": str(reason.get("message") or ""),
        "reason_source": str(reason.get("source") or ""),
        "state_source": str(metadata.get("source") or ""),
        "state_task_id": str(metadata.get("task_id") or ""),
    }


def _state_signature(state: dict[str, Any]) -> tuple[str, ...]:
    # Gantt worker rows are intentionally availability-first.  Other axes and
    # task context remain in hover metadata, but they should not fragment the
    # lane into primitive-sized slices.
    return (str(state.get("availability") or ""),)


def _hover_line(label: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return f"<br>{html.escape(label)}={html.escape(text)}"


def _build_worker_availability(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build non-overlapping worker Gantt rows from HumanoidSim availability state.

    Worker state is now a multi-axis HumanoidStateSnapshot.  The Gantt lane uses
    only the availability axis for color/status and keeps task/primitive context
    in hover metadata.
    """

    sim_end = max((float(event.get("t", 0.0) or 0.0) for event in events), default=0.0)
    worker_ids = {
        str(event.get("entity_id", "")).strip()
        for event in events
        if _is_worker_id(event.get("entity_id"))
    }
    current: dict[str, dict[str, Any]] = {
        worker_id: {
            "humanoid_id": worker_id,
            "availability": "AVAILABLE",
            "mobility": "STATIONARY",
            "power": "POWER_NORMAL",
            "manipulation": "FREE",
            "task_context": None,
            "reason": None,
            "metadata": {},
        }
        for worker_id in sorted(worker_ids, key=_worker_sort_key)
    }
    last_t: dict[str, float] = {worker_id: 0.0 for worker_id in current}
    last_event: dict[str, dict[str, Any]] = {
        worker_id: {
            "t": 0.0,
            "day": 0,
            "type": "HUMANOID_STATE_INITIAL",
            "entity_id": worker_id,
            "location": "",
            "details": {"humanoid_state": current[worker_id]},
        }
        for worker_id in current
    }
    intervals: list[dict[str, Any]] = []

    def add_interval(worker_id: str, end_t: float, end_event: dict[str, Any]) -> None:
        start_t = float(last_t.get(worker_id, 0.0))
        duration = max(0.0, float(end_t) - start_t)
        if duration <= 0.0:
            return
        state = dict(current.get(worker_id, {}))
        availability = str(state.get("availability") or "AVAILABLE").strip().upper() or "AVAILABLE"
        if availability not in AVAILABILITY_STATES:
            availability = "UNKNOWN"
        details = dict(_details(last_event.get(worker_id, {})))
        details["humanoid_state"] = state
        start_event = dict(last_event.get(worker_id, {}))
        start_event["details"] = details
        intervals.append(
            {
                "lane": worker_id,
                "entity_group": "Worker",
                "status": availability,
                "start": start_t,
                "end": float(end_t),
                "duration": duration,
                "interval_type": availability,
                "start_event": start_event,
                "end_event": end_event,
            }
        )

    for event in sorted(events, key=lambda item: float(item.get("t", 0.0) or 0.0)):
        state = _humanoid_state(event)
        if state is None:
            continue
        worker_id = str(event.get("entity_id", "")).strip()
        if worker_id not in current:
            continue
        event_t = float(event.get("t", 0.0) or 0.0)
        if _state_signature(state) == _state_signature(current.get(worker_id, {})):
            current[worker_id] = dict(state)
            continue
        add_interval(worker_id, event_t, event)
        current[worker_id] = dict(state)
        last_t[worker_id] = event_t
        last_event[worker_id] = event

    for worker_id in sorted(current, key=_worker_sort_key):
        add_interval(
            worker_id,
            sim_end,
            {
                "t": sim_end,
                "day": 0,
                "type": "HUMANOID_STATE_FINAL",
                "entity_id": worker_id,
                "location": "",
                "details": {"humanoid_state": current[worker_id]},
            },
        )

    return intervals


def _build_finished_wait_unload(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    machine_ends: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unload_starts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    machine_starts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sim_end = 0.0

    for ev in events:
        t = float(ev.get("t", 0.0))
        sim_end = max(sim_end, t)
        et = str(ev.get("type", ""))
        entity_id = str(ev.get("entity_id", ""))
        d = _details(ev)
        if et == "MACHINE_END":
            machine_ends[entity_id].append(ev)
        elif et == "MACHINE_START":
            machine_starts[entity_id].append(ev)
        elif et == "AGENT_TASK_START" and str(d.get("task_type", "")) == "UNLOAD_MACHINE":
            payload = d.get("payload", {})
            if isinstance(payload, dict):
                machine_id = str(payload.get("machine_id", "")).strip()
                if machine_id:
                    unload_starts[machine_id].append(ev)

    intervals: list[dict[str, Any]] = []
    for machine_id, end_events in machine_ends.items():
        end_events_sorted = sorted(end_events, key=lambda e: float(e.get("t", 0.0)))
        unloads_sorted = sorted(unload_starts.get(machine_id, []), key=lambda e: float(e.get("t", 0.0)))
        starts_sorted = sorted(machine_starts.get(machine_id, []), key=lambda e: float(e.get("t", 0.0)))

        unload_idx = 0
        for end_ev in end_events_sorted:
            end_t = float(end_ev.get("t", 0.0))

            while unload_idx < len(unloads_sorted) and float(unloads_sorted[unload_idx].get("t", 0.0)) < end_t:
                unload_idx += 1

            unload_ev = unloads_sorted[unload_idx] if unload_idx < len(unloads_sorted) else None
            unload_t = float(unload_ev.get("t", 0.0)) if unload_ev is not None else None
            if unload_ev is not None:
                unload_idx += 1

            next_start_t = None
            for st_ev in starts_sorted:
                st = float(st_ev.get("t", 0.0))
                if st > end_t:
                    next_start_t = st
                    break

            candidates = [v for v in [unload_t, next_start_t, sim_end] if v is not None]
            wait_end = min(candidates) if candidates else sim_end
            if wait_end <= end_t:
                continue

            end_marker = {
                "t": wait_end,
                "day": end_ev.get("day", 0),
                "type": "WAIT_UNLOAD_END",
                "entity_id": machine_id,
                "location": end_ev.get("location", ""),
                "details": {
                    "unload_agent": str(unload_ev.get("entity_id", "")) if unload_ev else "",
                    "unload_task_id": str(_details(unload_ev).get("task_id", "")) if unload_ev else "",
                    "until": "unload_start" if unload_t is not None and wait_end == unload_t else "next_cycle_or_end",
                },
            }
            intervals.append(
                {
                    "lane": machine_id,
                    "entity_group": "Machine",
                    "status": "FINISHED-WAIT-UNLOAD",
                    "start": end_t,
                    "end": wait_end,
                    "duration": wait_end - end_t,
                    "interval_type": "WAIT_UNLOAD",
                    "start_event": end_ev,
                    "end_event": end_marker,
                }
            )

    return intervals


def _humanoid_task_mix(events: list[dict[str, Any]]) -> dict[str, str]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for ev in events:
        if str(ev.get("type", "")) != "AGENT_TASK_END":
            continue
        agent = str(ev.get("entity_id", ""))
        task_code = str(_details(ev).get("task_code") or _details(ev).get("task_type") or "").upper()
        if task_code and str(_details(ev).get("status", "")).strip().lower() == "completed":
            counts[agent][task_code] += 1
    return {agent: " | ".join(f"{task}:{count}" for task, count in sorted(vals.items())) for agent, vals in counts.items()}


def export_gantt(
    events: list[dict[str, Any]],
    output_dir: Path,
    *,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    current_run_id: str | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    worker_availability = _build_worker_availability(events)

    machine_running = _pair_intervals(
        events,
        "MACHINE_START",
        {"MACHINE_END", "MACHINE_ABORTED"},
        _cycle_key,
        "RUNNING",
        lambda _s, _e: "MACHINE_PROCESSING",
        "Machine",
    )
    machine_down_break = _pair_intervals(
        events,
        "MACHINE_BROKEN",
        "MACHINE_REPAIRED",
        _entity_key,
        "DOWN",
        lambda _s, _e: "MACHINE_BROKEN",
        "Machine",
    )
    machine_down_pm = _pair_intervals(
        events,
        "MACHINE_PM_START",
        "MACHINE_PM_END",
        _entity_key,
        "DOWN",
        lambda _s, _e: "PREVENTIVE_MAINTENANCE",
        "Machine",
    )
    machine_wait_unload = _build_finished_wait_unload(events)

    rows = (
        worker_availability
        + machine_running
        + machine_down_break
        + machine_down_pm
        + machine_wait_unload
    )

    csv_path = output_dir / "gantt_segments.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "entity_group",
                "lane",
                "status",
                "start",
                "end",
                "duration",
                "interval_type",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "entity_group": row["entity_group"],
                    "lane": row["lane"],
                    "status": row["status"],
                    "start": row["start"],
                    "end": row["end"],
                    "duration": round(row["duration"], 3),
                    "interval_type": row["interval_type"],
                }
            )

    if not rows:
        gantt_path = output_dir / "gantt.html"
        gantt_path.write_text(
            render_page_shell(
                title="ManSim Gantt",
                current_page_path=gantt_path,
                manifest=manifest,
                manifest_path=manifest_path,
                current_artifact="gantt.html",
                current_run_id=current_run_id,
                page_title="Gantt",
                page_subtitle="Task and machine timeline for the selected run.",
                body_html="<section class='section'><div class='panel'><h2>Gantt Timeline</h2><p>No gantt segments were generated.</p></div></section>",
            ),
            encoding="utf-8",
        )
        return

    try:
        import pandas as pd
        import plotly.express as px
        import plotly.graph_objects as go
    except Exception:
        return

    task_mix = _humanoid_task_mix(events)
    df_rows: list[dict[str, Any]] = []
    for r in rows:
        start_event = r["start_event"]
        end_event = r["end_event"]
        sd = _details(start_event)
        ed = _details(end_event)
        state = sd.get("humanoid_state")
        state = state if isinstance(state, dict) else {}
        state_ctx = _state_context(state)
        payload = sd.get("payload", ed.get("payload", {}))
        payload_str = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else str(payload)
        lane = str(r["lane"])

        df_rows.append(
            {
                "lane": lane,
                "entity_group": r["entity_group"],
                "status": r["status"],
                "start": float(r["start"]),
                "end": float(r["end"]),
                "duration": float(r["duration"]),
                "interval_type": str(r["interval_type"]),
                "task_id": str(sd.get("task_id", ed.get("task_id", ""))),
                "task_code": str(state_ctx["task_code"] or sd.get("task_code", "")),
                "task_instance_id": str(state_ctx["task_instance_id"] or sd.get("instance_id", "")),
                "step_id": str(state_ctx["step_id"] or sd.get("step_id", "")),
                "primitive_call_code": str(state_ctx["primitive_call_code"] or sd.get("primitive_call_code", "")),
                "execution_status": str(state_ctx["execution_status"]),
                "availability": str(state.get("availability", "")),
                "mobility": str(state.get("mobility", "")),
                "power": str(state.get("power", "")),
                "manipulation": str(state.get("manipulation", "")),
                "priority_key": str(sd.get("priority_key", "")),
                "reason": str(state_ctx["reason_code"] or ed.get("reason", "")),
                "reason_message": str(state_ctx["reason_message"]),
                "reason_source": str(state_ctx["reason_source"]),
                "payload": payload_str,
                "cycle_id": str(sd.get("cycle_id", ed.get("cycle_id", ""))),
                "input_material": str(sd.get("input_material", "")),
                "input_intermediate": str(sd.get("input_intermediate", "")),
                "output_intermediate": str(ed.get("output_intermediate", "")),
                "unload_agent": str(ed.get("unload_agent", "")),
                "unload_task_id": str(ed.get("unload_task_id", "")),
                "humanoid_task_mix": task_mix.get(lane, ""),
                "start_day": int(start_event.get("day", 0) or 0),
                "end_day": int(end_event.get("day", 0) or 0),
                "start_location": str(start_event.get("location", "")),
                "end_location": str(end_event.get("location", "")),
            }
        )

    df = pd.DataFrame(df_rows)
    hover_summaries: list[str] = []
    for row in df_rows:
        lines = [
            f"<b>{html.escape(str(row['lane']))}</b>",
            f"<br>Group={html.escape(str(row['entity_group']))}",
            f"<br>Status={html.escape(str(row['status']))}",
        ]
        if row["entity_group"] == "Worker":
            lines.append(_hover_line("Task", row.get("task_code", "")))
            lines.append(_hover_line("Primitive", row.get("primitive_call_code", "")))
            lines.append(_hover_line("Mobility", row.get("mobility", "")))
            lines.append(_hover_line("Reason", row.get("reason", "")))
        else:
            lines.append(_hover_line("Cycle", row.get("cycle_id", "")))
            lines.append(_hover_line("Unload agent", row.get("unload_agent", "")))
        lines.extend(
            [
                f"<br>Start={float(row['start']):.2f} min",
                f"<br>End={float(row['end']):.2f} min",
                f"<br>Duration={float(row['duration']):.2f} min",
            ]
        )
        hover_summaries.append("".join(lines))
    df["hover_summary"] = hover_summaries
    base_time = datetime(2000, 1, 1, 0, 0, 0)
    df["start_dt"] = base_time + pd.to_timedelta(df["start"], unit="m")
    df["end_dt"] = base_time + pd.to_timedelta(df["end"], unit="m")

    color_map = {
        "AVAILABLE": "#d8dee9",
        "ASSIGNED": "#90caf9",
        "EXECUTING": "#27ae60",
        "WAITING": "#f5b041",
        "BLOCKED": "#e67e22",
        "OFFLINE": "#95a5a6",
        "DISABLED": "#e74c3c",
        "UNKNOWN": "#6c757d",
        "RUNNING": "#27ae60",
        "DOWN": "#e74c3c",
        "FINISHED-WAIT-UNLOAD": "#f39c12",
    }
    status_order = AVAILABILITY_STATES + [
        "UNKNOWN",
        "RUNNING",
        "DOWN",
        "FINISHED-WAIT-UNLOAD",
    ]

    fig = px.timeline(
        df,
        x_start="start_dt",
        x_end="end_dt",
        y="lane",
        color="status",
        color_discrete_map=color_map,
        category_orders={"status": status_order},
        custom_data=["hover_summary"],
    )

    rendered_statuses = {str(status) for status in df["status"].dropna().unique()}
    for state_name in AVAILABILITY_STATES:
        if state_name in rendered_statuses:
            continue
        fig.add_trace(
            go.Bar(
                name=state_name,
                x=[0],
                y=[""],
                orientation="h",
                marker_color=color_map.get(state_name, "#6c757d"),
                visible="legendonly",
                showlegend=True,
                hoverinfo="skip",
            )
        )

    worker_statuses = set(AVAILABILITY_STATES) | {"UNKNOWN"}
    machine_statuses = {"RUNNING", "DOWN", "FINISHED-WAIT-UNLOAD"}
    seen_worker_group = False
    seen_machine_group = False
    for trace in fig.data:
        status_name = str(getattr(trace, "name", ""))
        if status_name in worker_statuses:
            if str(getattr(trace, "hoverinfo", "")) != "skip":
                trace.hovertemplate = "%{customdata[0]}<extra></extra>"
            trace.legendgroup = "Worker Availability"
            if not seen_worker_group:
                trace.legendgrouptitle = {"text": "Worker Availability"}
                seen_worker_group = True
        elif status_name in machine_statuses:
            if str(getattr(trace, "hoverinfo", "")) != "skip":
                trace.hovertemplate = "%{customdata[0]}<extra></extra>"
            trace.legendgroup = "Machine"
            if not seen_machine_group:
                trace.legendgrouptitle = {"text": "Machine"}
                seen_machine_group = True

    fig.update_yaxes(autorange="reversed", title_text="Resource")
    fig.update_xaxes(title_text="Simulation Time", tickformat="%Y-%m-%d %H:%M")
    add_plotly_meta_header(fig, output_dir=output_dir, y_top=1.12)
    fig.update_layout(height=980, legend_title_text="", legend_traceorder="grouped", margin=dict(l=40, r=40, t=140, b=40))
    gantt_path = output_dir / "gantt.html"
    html_text = render_page_shell(
        title="ManSim Gantt",
        current_page_path=gantt_path,
        manifest=manifest,
        manifest_path=manifest_path,
        current_artifact="gantt.html",
        current_run_id=current_run_id,
        page_title="Gantt",
        page_subtitle="Timeline view of worker Availability State, machine states, and wait-unload windows.",
        body_html=f"<section class='section'><div class='panel'>{fig.to_html(full_html=False, include_plotlyjs=True)}</div></section>",
    )
    gantt_path.write_text(html_text, encoding="utf-8")
