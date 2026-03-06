from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def _pair_intervals(
    events: list[dict[str, Any]],
    start_type: str,
    end_types: str | Iterable[str],
    *,
    interval_type: str | None = None,
) -> list[dict[str, Any]]:
    end_type_set = {end_types} if isinstance(end_types, str) else set(end_types)
    active: dict[tuple[str, str], dict[str, Any]] = {}
    intervals: list[dict[str, Any]] = []
    for event in events:
        et = event["type"]
        entity_id = event["entity_id"]
        details = event.get("details", {})
        task_id = details.get("task_id") or details.get("cycle_id") or "default"
        key = (entity_id, str(task_id))
        if et == start_type:
            active[key] = event
        elif et in end_type_set and key in active:
            start_event = active.pop(key)
            inferred_type = details.get("task_type") or start_event.get("details", {}).get("task_type") or start_type
            status = details.get("status", "completed")
            if et == "MACHINE_ABORTED":
                status = "aborted"
            intervals.append(
                {
                    "lane": entity_id,
                    "start": float(start_event["t"]),
                    "end": float(event["t"]),
                    "type": interval_type or inferred_type,
                    "status": status,
                    "meta": details,
                }
            )
    return intervals


def export_gantt(events: list[dict[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    agent_intervals = _pair_intervals(events, "AGENT_TASK_START", "AGENT_TASK_END")

    machine_processing_intervals = _pair_intervals(
        events,
        "MACHINE_START",
        {"MACHINE_END", "MACHINE_ABORTED"},
        interval_type="MACHINE_PROCESSING",
    )
    machine_broken_intervals = _pair_intervals(
        events,
        "MACHINE_BROKEN",
        "MACHINE_REPAIRED",
        interval_type="MACHINE_BROKEN",
    )
    machine_pm_intervals = _pair_intervals(
        events,
        "MACHINE_PM_START",
        "MACHINE_PM_END",
        interval_type="PREVENTIVE_MAINTENANCE",
    )

    rows = agent_intervals + machine_processing_intervals + machine_broken_intervals + machine_pm_intervals

    csv_path = output_dir / "gantt_segments.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=["lane", "start", "end", "duration", "type", "status"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "lane": row["lane"],
                    "start": row["start"],
                    "end": row["end"],
                    "duration": round(row["end"] - row["start"], 3),
                    "type": row["type"],
                    "status": row["status"],
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

    df = pd.DataFrame(
        [
            {
                "lane": r["lane"],
                "start": r["start"],
                "end": r["end"],
                "type": r["type"],
                "status": r["status"],
            }
            for r in rows
        ]
    )
    base_time = datetime(2000, 1, 1, 0, 0, 0)
    df["start_dt"] = base_time + pd.to_timedelta(df["start"], unit="m")
    df["end_dt"] = base_time + pd.to_timedelta(df["end"], unit="m")

    fig = px.timeline(df, x_start="start_dt", x_end="end_dt", y="lane", color="type", hover_data=["status", "start", "end"])
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title_text="Simulation Time", tickformat="%Y-%m-%d %H:%M")
    fig.write_html(str(output_dir / "gantt.html"), include_plotlyjs=True)
