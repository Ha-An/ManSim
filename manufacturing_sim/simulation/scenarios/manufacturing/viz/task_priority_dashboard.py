from __future__ import annotations

from pathlib import Path
from typing import Any

from manufacturing_sim.simulation.scenarios.manufacturing.viz.artifact_meta import add_plotly_meta_header


TASK_DEFS: list[tuple[str, str, str]] = [
    ("BATTERY_SWAP", "safety", "battery_swap"),
    ("REPAIR_MACHINE", "blocking", "repair_machine"),
    ("UNLOAD_MACHINE", "blocking", "unload_machine"),
    ("SETUP_MACHINE", "flow", "setup_machine"),
    ("TRANSFER_INTER_STATION", "flow", "transfer"),
    ("TRANSFER_MATERIAL_SUPPLY", "supply", "transfer"),
    ("TRANSFER_BATTERY_DELIVERY_LOW", "safety", "deliver_priority_low_battery"),
    ("TRANSFER_BATTERY_DELIVERY_DISCHARGED", "safety", "deliver_priority_discharged"),
    ("INSPECT_PRODUCT", "quality", "inspect_product"),
    ("PREVENTIVE_MAINTENANCE", "maintenance", "preventive_maintenance"),
]


CAT_KEYS = ["safety", "blocking", "flow", "supply", "quality", "maintenance", "support"]


def _to_float(val: Any, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)


def _to_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return int(default)


def _extract_base_priorities(heuristic_rules: dict[str, Any] | None) -> dict[str, float]:
    rules = heuristic_rules if isinstance(heuristic_rules, dict) else {}
    world = rules.get("world", {}) if isinstance(rules.get("world", {}), dict) else {}
    task_priority = world.get("task_priority", {}) if isinstance(world.get("task_priority", {}), dict) else {}
    battery = world.get("battery", {}) if isinstance(world.get("battery", {}), dict) else {}

    return {
        "battery_swap": _to_float(task_priority.get("battery_swap", 150.0), 150.0),
        "repair_machine": _to_float(task_priority.get("repair_machine", 115.0), 115.0),
        "unload_machine": _to_float(task_priority.get("unload_machine", 110.0), 110.0),
        "setup_machine": _to_float(task_priority.get("setup_machine", 90.0), 90.0),
        "transfer": _to_float(task_priority.get("transfer", 85.0), 85.0),
        "inspect_product": _to_float(task_priority.get("inspect_product", 72.0), 72.0),
        "preventive_maintenance": _to_float(task_priority.get("preventive_maintenance", 65.0), 65.0),
        "deliver_priority_low_battery": _to_float(battery.get("deliver_priority_low_battery", 140.0), 140.0),
        "deliver_priority_discharged": _to_float(battery.get("deliver_priority_discharged", 149.0), 149.0),
    }


def _priority_series(
    events: list[dict[str, Any]] | None,
    heuristic_rules: dict[str, Any] | None,
) -> tuple[list[float], dict[str, list[float]]]:
    rows = events if isinstance(events, list) else []
    base = _extract_base_priorities(heuristic_rules)

    weights = {k: 1.0 for k in CAT_KEYS}
    quality_weight = 1.0

    xs: list[float] = []
    task_hist: dict[str, list[float]] = {name: [] for name, _, _ in TASK_DEFS}

    def snapshot(t: float) -> None:
        xs.append(round(float(t), 3))
        for task_name, category, base_key in TASK_DEFS:
            score = float(base[base_key]) * float(weights.get(category, 1.0))
            if category == "quality":
                score *= float(quality_weight)
            task_hist[task_name].append(score)

    changed = False
    for ev in rows:
        if not isinstance(ev, dict):
            continue
        et = str(ev.get("type", ""))
        t = _to_float(ev.get("t", 0.0), 0.0)
        details = ev.get("details", {}) if isinstance(ev.get("details", {}), dict) else {}

        if et == "PHASE_JOB_ASSIGNMENT":
            tw = details.get("task_weights", {}) if isinstance(details.get("task_weights", {}), dict) else {}
            if tw:
                for c in CAT_KEYS:
                    if c in tw:
                        weights[c] = _to_float(tw[c], weights[c])
                snapshot(t)
                changed = True

        elif et == "CHAT_URGENT":
            upd = details.get("weight_updates", {}) if isinstance(details.get("weight_updates", {}), dict) else {}
            if upd:
                for k, v in upd.items():
                    key = str(k)
                    if key in weights:
                        weights[key] = _to_float(v, weights[key])
                snapshot(t)
                changed = True

        elif et == "CHAT_TOWNHALL":
            norms = details.get("updated_norms", {}) if isinstance(details.get("updated_norms", {}), dict) else {}
            if "quality_weight" in norms:
                quality_weight = _to_float(norms.get("quality_weight"), quality_weight)
                snapshot(t)
                changed = True

    if not changed:
        snapshot(0.0)

    return xs, task_hist


def _day_boundaries(events: list[dict[str, Any]] | None) -> tuple[list[float], list[str]]:
    rows = events if isinstance(events, list) else []
    first_t_by_day: dict[int, float] = {}
    for ev in rows:
        if not isinstance(ev, dict):
            continue
        day = _to_int(ev.get("day", 0), 0)
        if day <= 0:
            continue
        t = _to_float(ev.get("t", 0.0), 0.0)
        prev = first_t_by_day.get(day, None)
        if prev is None or t < prev:
            first_t_by_day[day] = t

    if not first_t_by_day:
        return [], []

    tick_vals = [first_t_by_day[d] for d in sorted(first_t_by_day.keys())]
    tick_text = [f"D{d}" for d in sorted(first_t_by_day.keys())]
    return tick_vals, tick_text


def export_task_priority_dashboard(
    *,
    output_dir: Path,
    events: list[dict[str, Any]] | None,
    heuristic_rules: dict[str, Any] | None,
) -> Path | None:
    try:
        import plotly.graph_objects as go
    except Exception:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "task_priority_dashboard.html"

    x_t, task_hist = _priority_series(events, heuristic_rules)
    day_ticks, day_labels = _day_boundaries(events)

    fig = go.Figure()
    for task_name, ys in task_hist.items():
        fig.add_trace(go.Scatter(name=f"p:{task_name}", x=x_t, y=ys, mode="lines", line=dict(width=2)))

    for x in day_ticks:
        fig.add_vline(x=x, line_width=1, line_dash="dot", line_color="#94a3b8")

    fig.update_layout(
        title=dict(text="Task Priority Trend Dashboard", y=0.97, x=0.02, xanchor="left"),
        height=720,
        legend=dict(orientation="v", yanchor="top", y=0.90, xanchor="left", x=1.01),
        margin=dict(l=40, r=40, t=280, b=40),
    )
    fig.update_xaxes(
        title_text="Sim Time (min) / Day",
        tickmode="array" if day_ticks else "auto",
        tickvals=day_ticks if day_ticks else None,
        ticktext=day_labels if day_ticks else None,
    )
    fig.update_yaxes(title_text="Effective Priority")
    add_plotly_meta_header(fig, output_dir=output_dir, y_top=1.28)

    fig.write_html(str(out_path), include_plotlyjs=True)
    return out_path
