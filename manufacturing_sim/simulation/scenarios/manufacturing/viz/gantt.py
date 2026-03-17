from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from manufacturing_sim.simulation.scenarios.manufacturing.viz.artifact_meta import add_plotly_meta_header


AGENT_TASK_TYPES_7 = [
    "BATTERY_SWAP",
    "REPAIR_MACHINE",
    "UNLOAD_MACHINE",
    "SETUP_MACHINE",
    "TRANSFER",
    "INSPECT_PRODUCT",
    "PREVENTIVE_MAINTENANCE",
]


def _details(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("details", {})
    return data if isinstance(data, dict) else {}


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


def _agent_task_mix(events: list[dict[str, Any]]) -> dict[str, str]:
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {k: 0 for k in AGENT_TASK_TYPES_7})
    for ev in events:
        if str(ev.get("type", "")) != "AGENT_TASK_END":
            continue
        agent = str(ev.get("entity_id", ""))
        task_type = str(_details(ev).get("task_type", "")).upper()
        if task_type in counts[agent]:
            counts[agent][task_type] += 1
    return {
        agent: " | ".join(f"{task}:{vals.get(task, 0)}" for task in AGENT_TASK_TYPES_7) for agent, vals in counts.items()
    }


def export_gantt(events: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    agent_moving = _pair_intervals(
        events,
        "AGENT_MOVE_START",
        {"AGENT_MOVE_END", "AGENT_MOVE_INTERRUPTED"},
        _entity_key,
        "MOVING",
        lambda _s, _e: "MOVE",
        "Agent",
    )
    agent_working = _pair_intervals(
        events,
        "AGENT_TASK_START",
        "AGENT_TASK_END",
        _task_key,
        "WORKING",
        lambda s, _e: str(_details(s).get("task_type", "AGENT_TASK")),
        "Agent",
    )
    agent_discharged = _pair_intervals(
        events,
        "AGENT_DISCHARGED",
        "AGENT_RECHARGED",
        _entity_key,
        "DISCHARGED",
        lambda _s, _e: "BATTERY_EMPTY",
        "Agent",
    )

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
        agent_moving
        + agent_working
        + agent_discharged
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
        (output_dir / "gantt.html").write_text(
            "<html><body><h3>No gantt segments were generated.</h3></body></html>",
            encoding="utf-8",
        )
        return

    try:
        import pandas as pd
        import plotly.express as px
    except Exception:
        return

    agent_mix = _agent_task_mix(events)
    df_rows: list[dict[str, Any]] = []
    for r in rows:
        start_event = r["start_event"]
        end_event = r["end_event"]
        sd = _details(start_event)
        ed = _details(end_event)
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
                "task_type": str(sd.get("task_type", "")),
                "priority_key": str(sd.get("priority_key", "")),
                "reason": str(ed.get("reason", "")),
                "payload": payload_str,
                "cycle_id": str(sd.get("cycle_id", ed.get("cycle_id", ""))),
                "input_material": str(sd.get("input_material", "")),
                "input_intermediate": str(sd.get("input_intermediate", "")),
                "output_intermediate": str(ed.get("output_intermediate", "")),
                "unload_agent": str(ed.get("unload_agent", "")),
                "unload_task_id": str(ed.get("unload_task_id", "")),
                "agent_task_mix": agent_mix.get(lane, " | ".join(f"{k}:0" for k in AGENT_TASK_TYPES_7)),
                "start_day": int(start_event.get("day", 0) or 0),
                "end_day": int(end_event.get("day", 0) or 0),
                "start_location": str(start_event.get("location", "")),
                "end_location": str(end_event.get("location", "")),
            }
        )

    df = pd.DataFrame(df_rows)
    base_time = datetime(2000, 1, 1, 0, 0, 0)
    df["start_dt"] = base_time + pd.to_timedelta(df["start"], unit="m")
    df["end_dt"] = base_time + pd.to_timedelta(df["end"], unit="m")

    color_map = {
        # Match replay UI legend colors exactly
        "MOVING": "#f5b041",
        "WORKING": "#27ae60",
        "DISCHARGED": "#e74c3c",
        "RUNNING": "#27ae60",
        "DOWN": "#e74c3c",
        "FINISHED-WAIT-UNLOAD": "#f39c12",
    }
    status_order = [
        # Draw WORKING first and MOVING on top so the two are visually distinct.
        "WORKING",
        "MOVING",
        "DISCHARGED",
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
        hover_data={
            "entity_group": True,
            "status": True,
            "interval_type": True,
            "task_id": True,
            "task_type": True,
            "priority_key": True,
            "reason": True,
            "payload": True,
            "cycle_id": True,
            "input_material": True,
            "input_intermediate": True,
            "output_intermediate": True,
            "unload_agent": True,
            "unload_task_id": True,
            "agent_task_mix": True,
            "start_day": True,
            "end_day": True,
            "start_location": True,
            "end_location": True,
            "start": ":.2f",
            "end": ":.2f",
            "duration": ":.2f",
            "start_dt": False,
            "end_dt": False,
            "lane": False,
        },
    )

    agent_statuses = {"MOVING", "WORKING", "DISCHARGED"}
    machine_statuses = {"RUNNING", "DOWN", "FINISHED-WAIT-UNLOAD"}
    seen_agent_group = False
    seen_machine_group = False
    for trace in fig.data:
        status_name = str(getattr(trace, "name", ""))
        if status_name in agent_statuses:
            trace.legendgroup = "Agent"
            if not seen_agent_group:
                trace.legendgrouptitle = {"text": "Agent"}
                seen_agent_group = True
        elif status_name in machine_statuses:
            trace.legendgroup = "Machine"
            if not seen_machine_group:
                trace.legendgrouptitle = {"text": "Machine"}
                seen_machine_group = True

    fig.update_yaxes(autorange="reversed", title_text="Resource")
    fig.update_xaxes(title_text="Simulation Time", tickformat="%Y-%m-%d %H:%M")
    add_plotly_meta_header(fig, output_dir=output_dir, y_top=1.12)
    fig.update_layout(height=980, legend_title_text="", legend_traceorder="grouped", margin=dict(l=40, r=40, t=140, b=40))
    fig.write_html(str(output_dir / "gantt.html"), include_plotlyjs=True)
