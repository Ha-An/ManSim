from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from manufacturing_sim.simulation.scenarios.manufacturing.decision.modes import (
    format_decision_mode_label,
    is_llm_mode,
    normalize_decision_mode,
)


def load_artifact_meta(output_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "mode": "unknown",
        "model": "",
        "server_url": "",
        "communication_enabled": None,
        "communication_language": "",
        "events_path": str((output_dir / "events.jsonl").resolve()),
        "wall_clock_human": "",
    }

    run_meta_path = output_dir / "run_meta.json"
    if not run_meta_path.exists():
        return out

    try:
        run_meta = json.loads(run_meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out

    if not isinstance(run_meta, dict):
        return out

    mode = normalize_decision_mode(str(run_meta.get("decision_mode", "")))
    if mode:
        out["mode"] = mode

    llm = run_meta.get("llm", {}) if isinstance(run_meta.get("llm", {}), dict) else {}
    out["model"] = str(llm.get("model", "")).strip()
    out["server_url"] = str(llm.get("server_url", "")).strip()
    if "communication_enabled" in llm:
        out["communication_enabled"] = bool(llm.get("communication_enabled"))
    out["communication_language"] = str(llm.get("communication_language", "")).strip().upper()

    out["wall_clock_human"] = str(run_meta.get("wall_clock_human", "")).strip()

    return out


def format_run_mode_line(meta: dict[str, Any]) -> str:
    mode = normalize_decision_mode(str(meta.get("mode", "unknown"))) or "unknown"
    if is_llm_mode(mode):
        model = str(meta.get("model", "")).strip() or "-"
        server = str(meta.get("server_url", "")).strip() or "-"
        comm = meta.get("communication_enabled", None)
        comm_label = "on" if bool(comm) else "off"
        language = str(meta.get("communication_language", "")).strip().upper()
        language_part = f" | language={language}" if language else ""
        return (
            f"Run mode: {format_decision_mode_label(mode)} | "
            f"model={model} | communication={comm_label}{language_part} | server={server}"
        )

    return f"Run mode: {format_decision_mode_label(mode)}"


def add_plotly_meta_header(fig: Any, *, output_dir: Path, y_top: float = 1.28) -> None:
    meta = load_artifact_meta(output_dir)
    mode = str(meta.get("mode", "unknown")).strip().lower() or "unknown"
    mode_color = "#2a9d8f" if is_llm_mode(mode) else "#457b9d"

    fig.add_annotation(
        x=0.0,
        y=y_top,
        xref="paper",
        yref="paper",
        xanchor="left",
        yanchor="top",
        text=f"<b>events.jsonl</b>: {meta.get('events_path', '-')}",
        showarrow=False,
        font=dict(size=12, color="#334155"),
        align="left",
        bgcolor="#f8fafc",
        bordercolor="#cbd5e1",
        borderwidth=1,
        borderpad=6,
    )
    run_mode_line = format_run_mode_line(meta)
    wall_clock_human = str(meta.get("wall_clock_human", "")).strip()
    if wall_clock_human:
        run_mode_line = f"{run_mode_line} | runtime={wall_clock_human}"

    fig.add_annotation(
        x=0.0,
        y=y_top - 0.10,
        xref="paper",
        yref="paper",
        xanchor="left",
        yanchor="top",
        text=f"<b>{run_mode_line}</b>",
        showarrow=False,
        font=dict(size=13, color=mode_color),
        align="left",
        bgcolor="#f8fafc",
        bordercolor=mode_color,
        borderwidth=1,
        borderpad=6,
    )
