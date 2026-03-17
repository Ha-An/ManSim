from __future__ import annotations

from pathlib import Path
from typing import Any

from manufacturing_sim.simulation.scenarios.manufacturing.viz.artifact_meta import add_plotly_meta_header

TASK_DEFS: list[tuple[str, str]] = [
    ("BATTERY_SWAP", "battery_swap"),
    ("BATTERY_DELIVERY_LOW", "battery_delivery_low_battery"),
    ("BATTERY_DELIVERY_DISCHARGED", "battery_delivery_discharged"),
    ("REPAIR_MACHINE", "repair_machine"),
    ("UNLOAD_MACHINE", "unload_machine"),
    ("SETUP_MACHINE", "setup_machine"),
    ("INTER_STATION_TRANSFER", "inter_station_transfer"),
    ("MATERIAL_SUPPLY", "material_supply"),
    ("INSPECT_PRODUCT", "inspect_product"),
    ("PREVENTIVE_MAINTENANCE", "preventive_maintenance"),
]

DEFAULT_BASE_PRIORITIES = {
    "battery_swap": 150.0,
    "battery_delivery_low_battery": 140.0,
    "battery_delivery_discharged": 149.0,
    "repair_machine": 115.0,
    "unload_machine": 110.0,
    "setup_machine": 90.0,
    "inter_station_transfer": 85.0,
    "material_supply": 85.0,
    "inspect_product": 72.0,
    "preventive_maintenance": 65.0,
}

TASK_COLORS = {
    "BATTERY_SWAP": "#636EFA",
    "BATTERY_DELIVERY_LOW": "#EF553B",
    "BATTERY_DELIVERY_DISCHARGED": "#00CC96",
    "REPAIR_MACHINE": "#AB63FA",
    "UNLOAD_MACHINE": "#FFA15A",
    "SETUP_MACHINE": "#19D3F3",
    "INTER_STATION_TRANSFER": "#FF6692",
    "MATERIAL_SUPPLY": "#B6E880",
    "INSPECT_PRODUCT": "#FF97FF",
    "PREVENTIVE_MAINTENANCE": "#FECB52",
}


def _to_float(val: Any, default: float) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)


def _extract_base_priorities(heuristic_rules: dict[str, Any] | None) -> dict[str, float]:
    rules = heuristic_rules if isinstance(heuristic_rules, dict) else {}
    world = rules.get("world", {}) if isinstance(rules.get("world", {}), dict) else {}
    task_priority = world.get("task_priority", {}) if isinstance(world.get("task_priority", {}), dict) else {}
    base = dict(DEFAULT_BASE_PRIORITIES)
    for key, default in DEFAULT_BASE_PRIORITIES.items():
        base[key] = _to_float(task_priority.get(key, default), default)
    return base


def _sorted_days(events: list[dict[str, Any]] | None) -> list[int]:
    return sorted(
        {
            int(ev.get("day", 0) or 0)
            for ev in (events or [])
            if isinstance(ev, dict) and str(ev.get("type", "")) == "PHASE_JOB_ASSIGNMENT" and int(ev.get("day", 0) or 0) > 0
        }
    )


def _priority_snapshots(
    events: list[dict[str, Any]] | None,
    heuristic_rules: dict[str, Any] | None,
) -> tuple[list[int], dict[str, list[float]], dict[str, dict[str, list[float]]], dict[str, dict[str, list[float]]]]:
    # Reconstruct the shared day-level baseline and each agent's overlay so the dashboard
    # can show both the team policy and per-agent divergence over time.
    rows = events if isinstance(events, list) else []
    base = _extract_base_priorities(heuristic_rules)
    days = _sorted_days(rows)
    if not days:
        days = [1]

    shared_hist: dict[str, list[float]] = {name: [] for name, _ in TASK_DEFS}
    agent_delta_hist: dict[str, dict[str, list[float]]] = {}
    agent_effective_hist: dict[str, dict[str, list[float]]] = {}

    assignments: dict[int, dict[str, Any]] = {}
    for ev in rows:
        if not isinstance(ev, dict) or str(ev.get("type", "")) != "PHASE_JOB_ASSIGNMENT":
            continue
        day = int(ev.get("day", 0) or 0)
        if day <= 0:
            continue
        details = ev.get("details", {}) if isinstance(ev.get("details", {}), dict) else {}
        assignments[day] = details

    last_shared = {key: 1.0 for _, key in TASK_DEFS}
    last_agent_multiplier: dict[str, dict[str, float]] = {}
    last_agent_effective: dict[str, dict[str, float]] = {}

    for day in days:
        details = assignments.get(day, {}) if isinstance(assignments.get(day, {}), dict) else {}
        shared_raw = details.get("shared_task_priority_weights", details.get("task_priority_weights", {}))
        if isinstance(shared_raw, dict):
            for _, key in TASK_DEFS:
                if key in shared_raw:
                    last_shared[key] = _to_float(shared_raw.get(key), last_shared[key])

        raw_multiplier = details.get("agent_priority_multipliers", {}) if isinstance(details.get("agent_priority_multipliers", {}), dict) else {}
        for agent_id, row in raw_multiplier.items():
            if not isinstance(row, dict):
                continue
            agent_row = last_agent_multiplier.setdefault(str(agent_id), {key: 1.0 for _, key in TASK_DEFS})
            for _, key in TASK_DEFS:
                if key in row:
                    agent_row[key] = _to_float(row.get(key), agent_row[key])

        raw_effective = details.get("agent_effective_task_priority_weights", {}) if isinstance(details.get("agent_effective_task_priority_weights", {}), dict) else {}
        for agent_id in sorted(set(last_agent_multiplier.keys()) | set(raw_effective.keys())):
            row_effective = last_agent_effective.setdefault(str(agent_id), {key: last_shared[key] for _, key in TASK_DEFS})
            if isinstance(raw_effective.get(agent_id), dict):
                for _, key in TASK_DEFS:
                    if key in raw_effective[agent_id]:
                        row_effective[key] = _to_float(raw_effective[agent_id].get(key), row_effective[key])
                    else:
                        row_effective[key] = last_shared[key] * float(last_agent_multiplier.get(agent_id, {}).get(key, 1.0))
            else:
                for _, key in TASK_DEFS:
                    row_effective[key] = last_shared[key] * float(last_agent_multiplier.get(agent_id, {}).get(key, 1.0))

        for task_name, key in TASK_DEFS:
            shared_hist[task_name].append(float(base[key]) * float(last_shared[key]))

        for agent_id in sorted(last_agent_effective.keys()):
            agent_delta_hist.setdefault(agent_id, {name: [] for name, _ in TASK_DEFS})
            agent_effective_hist.setdefault(agent_id, {name: [] for name, _ in TASK_DEFS})
            for task_name, key in TASK_DEFS:
                shared_effective = float(base[key]) * float(last_shared[key])
                agent_effective = float(base[key]) * float(last_agent_effective[agent_id].get(key, last_shared[key]))
                agent_effective_hist[agent_id][task_name].append(agent_effective)
                agent_delta_hist[agent_id][task_name].append(agent_effective - shared_effective)

    return days, shared_hist, agent_delta_hist, agent_effective_hist


def export_task_priority_dashboard(*, output_dir: Path, events: list[dict[str, Any]] | None, heuristic_rules: dict[str, Any] | None) -> Path | None:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "task_priority_dashboard.html"
    days, shared_hist, agent_delta_hist, agent_effective_hist = _priority_snapshots(events, heuristic_rules)
    x_labels = [f"D{day}" for day in days]
    agent_ids = sorted(agent_effective_hist.keys())
    row_count = 1 + len(agent_ids)
    row_heights = [0.26] + ([0.74 / max(1, len(agent_ids))] * len(agent_ids))
    subplot_titles = ["Shared Baseline Effective Priority"] + [f"{agent_id} Effective Priority" for agent_id in agent_ids]

    fig = make_subplots(
        rows=row_count,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.045,
        row_heights=row_heights,
        subplot_titles=subplot_titles,
    )

    for task_name, _key in TASK_DEFS:
        fig.add_trace(
            go.Scatter(
                x=x_labels,
                y=shared_hist[task_name],
                mode="lines+markers",
                name=f"p:{task_name}",
                legendgroup=task_name,
                line=dict(color=TASK_COLORS.get(task_name, "#1f77b4"), width=2.5),
                marker=dict(size=6),
                hovertemplate="Day=%{x}<br>Task=%{fullData.legendgroup}<br>Shared effective priority=%{y:.2f}<extra></extra>",
                showlegend=True,
            ),
            row=1,
            col=1,
        )

    for offset, agent_id in enumerate(agent_ids, start=2):
        for task_name, _key in TASK_DEFS:
            deltas = agent_delta_hist.get(agent_id, {}).get(task_name, [0.0] * len(days))
            fig.add_trace(
                go.Scatter(
                    x=x_labels,
                    y=agent_effective_hist[agent_id][task_name],
                    mode="lines+markers",
                    name=f"p:{task_name}",
                    legendgroup=task_name,
                    line=dict(color=TASK_COLORS.get(task_name, "#1f77b4"), width=2.2),
                    marker=dict(size=5),
                    customdata=deltas,
                    hovertemplate="Day=%{x}<br>Task=%{fullData.legendgroup}<br>Agent effective priority=%{y:.2f}<br>Delta vs shared=%{customdata:.2f}<extra></extra>",
                    showlegend=False,
                ),
                row=offset,
                col=1,
            )

    fig.update_layout(
        title=dict(text="Agent-Specific Task Priority Dashboard", y=0.985, x=0.02, xanchor="left"),
        height=max(1500, 420 + 420 * max(1, len(agent_ids))),
        margin=dict(l=70, r=320, t=320, b=90),
        legend=dict(
            orientation="v",
            yanchor="top",
            y=0.97,
            xanchor="left",
            x=1.02,
            bgcolor="rgba(255,255,255,0.88)",
            bordercolor="#cbd5e1",
            borderwidth=1,
            tracegroupgap=6,
        ),
        hovermode="x unified",
    )
    fig.update_xaxes(title_text="Sim Time (min) / Day", row=row_count, col=1)
    for row in range(1, row_count + 1):
        fig.update_yaxes(title_text="Effective Priority", row=row, col=1)
    add_plotly_meta_header(fig, output_dir=output_dir, y_top=1.30)
    fig.write_html(str(out_path), include_plotlyjs=True)
    return out_path
