from __future__ import annotations

from pathlib import Path
from typing import Any


def export_kpi_dashboard(
    *,
    kpi: dict[str, Any],
    daily_summary: list[dict[str, Any]],
    output_dir: Path,
) -> Path | None:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    dashboard_path = output_dir / "kpi_dashboard.html"

    days = [int(d["day"]) for d in daily_summary]
    daily_products = [float(d["products"]) for d in daily_summary]
    daily_scrap = [float(d["scrap"]) for d in daily_summary]
    daily_scrap_rate = [float(d["scrap_rate"]) for d in daily_summary]
    daily_breakdowns = [float(d["machine_breakdowns"]) for d in daily_summary]

    station_tp = kpi.get("station_throughput", {})
    stations = [str(k) for k in sorted(station_tp, key=lambda x: int(x))]
    station_values = [float(station_tp[k]) for k in sorted(station_tp, key=lambda x: int(x))]

    agent_task_minutes = kpi.get("agent_task_minutes", {})
    task_types = list(agent_task_minutes.keys())
    task_values = [float(agent_task_minutes[k]) for k in task_types]

    fig = make_subplots(
        rows=2,
        cols=2,
        subplot_titles=(
            "Daily Products vs Scrap",
            "Daily Scrap Rate & Breakdowns",
            "Station Throughput",
            "Agent Task Minutes",
        ),
        specs=[[{"secondary_y": False}, {"secondary_y": True}], [{"secondary_y": False}, {"secondary_y": False}]],
    )

    fig.add_trace(
        go.Bar(name="Products", x=days, y=daily_products, marker_color="#2a9d8f"),
        row=1,
        col=1,
    )
    fig.add_trace(
        go.Bar(name="Scrap", x=days, y=daily_scrap, marker_color="#e76f51"),
        row=1,
        col=1,
    )

    fig.add_trace(
        go.Scatter(name="Scrap Rate", x=days, y=daily_scrap_rate, mode="lines+markers", line=dict(color="#264653")),
        row=1,
        col=2,
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(name="Breakdowns", x=days, y=daily_breakdowns, mode="lines+markers", line=dict(color="#f4a261")),
        row=1,
        col=2,
        secondary_y=True,
    )

    fig.add_trace(
        go.Bar(name="Throughput", x=stations, y=station_values, marker_color="#457b9d"),
        row=2,
        col=1,
    )

    fig.add_trace(
        go.Bar(name="Task Minutes", x=task_types, y=task_values, marker_color="#8ecae6"),
        row=2,
        col=2,
    )

    fig.update_layout(
        title_text=f"Manufacturing KPI Dashboard (total_products={kpi.get('total_products', 0)})",
        barmode="group",
        height=900,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1.0),
        margin=dict(l=40, r=40, t=80, b=40),
    )
    fig.update_xaxes(title_text="Day", row=1, col=1)
    fig.update_yaxes(title_text="Count", row=1, col=1)
    fig.update_xaxes(title_text="Day", row=1, col=2)
    fig.update_yaxes(title_text="Scrap Rate", row=1, col=2, secondary_y=False)
    fig.update_yaxes(title_text="Breakdowns", row=1, col=2, secondary_y=True)
    fig.update_xaxes(title_text="Station", row=2, col=1)
    fig.update_yaxes(title_text="Components", row=2, col=1)
    fig.update_xaxes(title_text="Task Type", row=2, col=2)
    fig.update_yaxes(title_text="Minutes", row=2, col=2)

    fig.write_html(str(dashboard_path), include_plotlyjs=True)
    return dashboard_path
