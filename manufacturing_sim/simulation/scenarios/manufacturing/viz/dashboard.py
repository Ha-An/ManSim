from __future__ import annotations

from pathlib import Path
from typing import Any

from manufacturing_sim.simulation.scenarios.manufacturing.viz.artifact_meta import add_plotly_meta_header


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_minutes(minutes: float) -> str:
    total_minutes = max(0.0, float(minutes))
    hours = int(total_minutes // 60)
    remaining_minutes = total_minutes - (hours * 60)
    if hours:
        return f"{hours}h {remaining_minutes:.1f}m"
    return f"{remaining_minutes:.1f}m"


def _sorted_agent_ids(mapping: dict[str, Any]) -> list[str]:
    def _agent_key(raw: str) -> tuple[int, str]:
        name = str(raw)
        suffix = name[1:] if name.upper().startswith("A") else name
        if suffix.isdigit():
            return (0, f"{int(suffix):06d}")
        return (1, name)

    return sorted((str(key) for key in mapping.keys()), key=_agent_key)


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
    daily_products = [float(d.get("products", 0.0) or 0.0) for d in daily_summary]
    daily_scrap = [float(d.get("scrap", 0.0) or 0.0) for d in daily_summary]
    daily_scrap_rate = [float(d.get("scrap_rate", 0.0) or 0.0) for d in daily_summary]
    daily_breakdowns = [float(d.get("machine_breakdowns", 0.0) or 0.0) for d in daily_summary]

    station_tp = kpi.get("station_throughput", {}) if isinstance(kpi.get("station_throughput", {}), dict) else {}
    station_keys = sorted(station_tp, key=lambda x: int(x))
    stations = [f"S{int(k)}" for k in station_keys]
    station_values = [float(station_tp[k]) for k in station_keys]

    agent_task_minutes = kpi.get("agent_task_minutes", {}) if isinstance(kpi.get("agent_task_minutes", {}), dict) else {}
    task_pairs = sorted(
        ((str(task_type), float(minutes)) for task_type, minutes in agent_task_minutes.items()),
        key=lambda item: item[1],
        reverse=True,
    )
    task_types = [task_type for task_type, _minutes in task_pairs]
    task_values = [minutes for _task_type, minutes in task_pairs]

    machine_ratio_by_station = kpi.get("machine_ratio_by_station", {}) if isinstance(kpi.get("machine_ratio_by_station", {}), dict) else {}
    machine_ratio_labels = ["Overall"] + [f"S{int(str(key).replace('station', ''))}" for key in sorted(machine_ratio_by_station, key=lambda raw: int(str(raw).replace('station', '')))]
    machine_processing = [float(kpi.get("machine_utilization", 0.0) or 0.0)]
    machine_broken = [float(kpi.get("machine_broken_ratio", 0.0) or 0.0)]
    machine_pm = [float(kpi.get("machine_pm_ratio", 0.0) or 0.0)]
    machine_other = [float(kpi.get("machine_other_ratio", 0.0) or 0.0)]
    for station_key in sorted(machine_ratio_by_station, key=lambda raw: int(str(raw).replace("station", ""))):
        station_metrics = machine_ratio_by_station.get(station_key, {}) if isinstance(machine_ratio_by_station.get(station_key, {}), dict) else {}
        machine_processing.append(float(station_metrics.get("processing", 0.0) or 0.0))
        machine_broken.append(float(station_metrics.get("broken", 0.0) or 0.0))
        machine_pm.append(float(station_metrics.get("pm", 0.0) or 0.0))
        machine_other.append(float(station_metrics.get("other", 0.0) or 0.0))

    buffer_wait_avg = kpi.get("buffer_wait_avg_min_including_open", kpi.get("buffer_wait_avg_min", {}))
    buffer_wait_avg = buffer_wait_avg if isinstance(buffer_wait_avg, dict) else {}
    buffer_wait_completed = kpi.get("buffer_wait_completed_count", {}) if isinstance(kpi.get("buffer_wait_completed_count", {}), dict) else {}
    buffer_wait_open = kpi.get("buffer_wait_open_count", {}) if isinstance(kpi.get("buffer_wait_open_count", {}), dict) else {}
    buffer_wait_keys = [
        ("material_input", "Material input"),
        ("intermediate_input", "Intermediate input"),
        ("product_input", "Product input"),
        ("intermediate_output", "Intermediate output"),
        ("product_output", "Product output"),
    ]
    buffer_wait_labels = [label for _key, label in buffer_wait_keys]
    buffer_wait_values = [float(buffer_wait_avg.get(key, 0.0) or 0.0) for key, _label in buffer_wait_keys]
    buffer_wait_text = [
        f"{float(buffer_wait_avg.get(key, 0.0) or 0.0):.1f}m<br>(closed={int(buffer_wait_completed.get(key, 0) or 0)}, open={int(buffer_wait_open.get(key, 0) or 0)})"
        for key, _label in buffer_wait_keys
    ]

    discharged_by_agent = kpi.get("agent_discharged_time_min_by_agent", {}) if isinstance(kpi.get("agent_discharged_time_min_by_agent", {}), dict) else {}
    discharged_ratio_by_agent = kpi.get("agent_discharged_ratio_by_agent", {}) if isinstance(kpi.get("agent_discharged_ratio_by_agent", {}), dict) else {}
    agent_labels = _sorted_agent_ids(discharged_by_agent)
    agent_discharged_values = [float(discharged_by_agent.get(agent_id, 0.0) or 0.0) for agent_id in agent_labels]
    agent_discharged_text = [
        f"{float(discharged_by_agent.get(agent_id, 0.0) or 0.0):.1f}m<br>({100.0 * float(discharged_ratio_by_agent.get(agent_id, 0.0) or 0.0):.1f}%)"
        for agent_id in agent_labels
    ]

    fig = make_subplots(
        rows=4,
        cols=2,
        subplot_titles=(
            "Daily Products vs Scrap",
            "Daily Scrap Rate",
            "Daily Machine Breakdowns",
            "Station Throughput",
            "Machine Time Ratios",
            "Buffer Waiting Times",
            "Agent Task Minutes",
            "Agent Discharged Time",
        ),
        specs=[[{}, {}], [{}, {}], [{}, {}], [{}, {}]],
    )

    fig.add_trace(go.Bar(name="Products", x=days, y=daily_products, marker_color="#1d4e89"), row=1, col=1)
    fig.add_trace(go.Bar(name="Scrap", x=days, y=daily_scrap, marker_color="#d1495b"), row=1, col=1)
    fig.add_trace(
        go.Scatter(
            name="Scrap Rate",
            x=days,
            y=daily_scrap_rate,
            mode="lines+markers",
            line=dict(color="#3c6e71", width=3),
            marker=dict(size=8),
        ),
        row=1,
        col=2,
    )
    fig.add_trace(go.Bar(name="Breakdowns", x=days, y=daily_breakdowns, marker_color="#edae49"), row=2, col=1)
    fig.add_trace(go.Bar(name="Throughput", x=stations, y=station_values, marker_color="#00798c"), row=2, col=2)
    fig.add_trace(go.Bar(name="Processing", x=machine_ratio_labels, y=machine_processing, marker_color="#2a9d8f"), row=3, col=1)
    fig.add_trace(go.Bar(name="Broken", x=machine_ratio_labels, y=machine_broken, marker_color="#e76f51"), row=3, col=1)
    fig.add_trace(go.Bar(name="PM", x=machine_ratio_labels, y=machine_pm, marker_color="#f4a261"), row=3, col=1)
    fig.add_trace(go.Bar(name="Other", x=machine_ratio_labels, y=machine_other, marker_color="#8d99ae"), row=3, col=1)
    fig.add_trace(
        go.Bar(
            name="Avg wait",
            x=buffer_wait_labels,
            y=buffer_wait_values,
            text=buffer_wait_text,
            textposition="outside",
            marker_color="#7b2cbf",
            hovertemplate="%{x}: %{y:.2f} min<extra></extra>",
        ),
        row=3,
        col=2,
    )
    fig.add_trace(go.Bar(name="Task Minutes", x=task_types, y=task_values, marker_color="#90be6d"), row=4, col=1)
    fig.add_trace(
        go.Bar(
            name="Discharged",
            x=agent_labels,
            y=agent_discharged_values,
            text=agent_discharged_text,
            textposition="outside",
            marker_color="#bc4749",
            hovertemplate="%{x}: %{y:.2f} min<extra></extra>",
        ),
        row=4,
        col=2,
    )

    wall_clock_sec = float(kpi.get("wall_clock_sec", 0.0) or 0.0)
    llm_transport = kpi.get("llm_transport_metrics", {}) if isinstance(kpi.get("llm_transport_metrics", {}), dict) else {}
    summary_lines = [
        f"<b>Total products</b>: {int(float(kpi.get('total_products', 0) or 0))} | <b>Scrap rate</b>: {100.0 * float(kpi.get('scrap_rate', 0.0) or 0.0):.2f}% | <b>Throughput</b>: {float(kpi.get('throughput_per_sim_hour', 0.0) or 0.0):.2f}/sim hr | <b>Wall-clock</b>: {_format_duration(wall_clock_sec)}",
        f"<b>Machine</b>: util {100.0 * float(kpi.get('machine_utilization', 0.0) or 0.0):.1f}% / broken {100.0 * float(kpi.get('machine_broken_ratio', 0.0) or 0.0):.1f}% / PM {100.0 * float(kpi.get('machine_pm_ratio', 0.0) or 0.0):.1f}% / other {100.0 * float(kpi.get('machine_other_ratio', 0.0) or 0.0):.1f}%",
        f"<b>Agents</b>: availability {100.0 * float(kpi.get('agent_availability_ratio', 0.0) or 0.0):.1f}% | total discharged {_format_minutes(float(kpi.get('agent_discharged_time_min_total', 0.0) or 0.0))} | avg/agent {_format_minutes(float(kpi.get('agent_discharged_time_min_avg', 0.0) or 0.0))}",
        f"<b>Buffers</b>: avg WIP material {float(kpi.get('avg_wip_material', 0.0) or 0.0):.2f}, intermediate {float(kpi.get('avg_wip_intermediate', 0.0) or 0.0):.2f}, output {float(kpi.get('avg_wip_output', 0.0) or 0.0):.2f} | <b>Downstream closure</b>: {100.0 * float(kpi.get('downstream_closure_ratio', 0.0) or 0.0):.1f}%",
    ]
    if llm_transport:
        summary_lines.append(
            f"<b>LLM transport</b>: native requested {int(llm_transport.get('requested_native_local', 0) or 0)} / native used {int(llm_transport.get('used_native_local', 0) or 0)} / chat used {int(llm_transport.get('used_chat_compat', 0) or 0)} / fallback {100.0 * float(llm_transport.get('native_fallback_ratio', 0.0) or 0.0):.1f}% / contract-default {int(llm_transport.get('native_default_contract_count', 0) or 0)} ({100.0 * float(llm_transport.get('native_default_contract_ratio', 0.0) or 0.0):.1f}%)"
        )
    summary_text = "<br>".join(summary_lines)

    title = f"Manufacturing KPI Dashboard (total_products={kpi.get('total_products', 0)})"
    fig.update_layout(
        title=dict(text=title, y=0.988, x=0.02, xanchor="left"),
        barmode="group",
        height=1700,
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.06,
            xanchor="center",
            x=0.5,
        ),
        margin=dict(l=50, r=50, t=420, b=130),
        plot_bgcolor="#fbfbfd",
        paper_bgcolor="#ffffff",
    )

    fig.add_annotation(
        x=0.0,
        y=1.20,
        xref="paper",
        yref="paper",
        xanchor="left",
        yanchor="top",
        showarrow=False,
        align="left",
        text=summary_text,
        bordercolor="#d8dee9",
        borderwidth=1,
        borderpad=10,
        bgcolor="#f8fafc",
        font=dict(size=13),
    )

    fig.update_xaxes(title_text="Day", row=1, col=1)
    fig.update_yaxes(title_text="Count", row=1, col=1)
    fig.update_xaxes(title_text="Day", row=1, col=2)
    fig.update_yaxes(title_text="Scrap rate", tickformat=".0%", range=[0, max(0.05, max(daily_scrap_rate, default=0.0) * 1.2)], row=1, col=2)
    fig.update_xaxes(title_text="Day", row=2, col=1)
    fig.update_yaxes(title_text="Breakdowns", row=2, col=1)
    fig.update_xaxes(title_text="Station", row=2, col=2)
    fig.update_yaxes(title_text="Completed units", row=2, col=2)
    fig.update_xaxes(title_text="Scope", row=3, col=1)
    fig.update_yaxes(title_text="Ratio", tickformat=".0%", range=[0, 1], row=3, col=1)
    fig.update_xaxes(title_text="Buffer type", row=3, col=2)
    fig.update_yaxes(title_text="Average wait (min)", row=3, col=2)
    fig.update_xaxes(title_text="Task type", row=4, col=1)
    fig.update_yaxes(title_text="Minutes", row=4, col=1)
    fig.update_xaxes(title_text="Agent", row=4, col=2)
    fig.update_yaxes(title_text="Discharged time (min)", row=4, col=2)

    add_plotly_meta_header(fig, output_dir=output_dir, y_top=1.34)

    fig.write_html(str(dashboard_path), include_plotlyjs=True)
    return dashboard_path
