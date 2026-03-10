from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from manufacturing_sim.simulation.scenarios.manufacturing.decision.modes import (
    format_decision_mode_label,
    normalize_decision_mode,
)


def load_artifact_meta(output_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {
        "mode": "unknown",
        "model": "",
        "server_url": "",
        "communication_enabled": None,
        "events_path": str((output_dir / "events.jsonl").resolve()),
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

    return out


def format_run_mode_line(meta: dict[str, Any]) -> str:
    mode = normalize_decision_mode(str(meta.get("mode", "unknown"))) or "unknown"
    if mode == "llm":
        model = str(meta.get("model", "")).strip() or "-"
        server = str(meta.get("server_url", "")).strip() or "-"
        comm = meta.get("communication_enabled", None)
        comm_label = "on" if bool(comm) else "off"
        return f"Run mode: LLM | model={model} | communication={comm_label} | server={server}"

    return f"Run mode: {format_decision_mode_label(mode)}"


def add_plotly_meta_header(fig: Any, *, output_dir: Path, y_top: float = 1.28) -> None:
    meta = load_artifact_meta(output_dir)
    mode = str(meta.get("mode", "unknown")).strip().lower() or "unknown"
    mode_color = "#2a9d8f" if mode == "llm" else "#457b9d"

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
    fig.add_annotation(
        x=0.0,
        y=y_top - 0.10,
        xref="paper",
        yref="paper",
        xanchor="left",
        yanchor="top",
        text=f"<b>{format_run_mode_line(meta)}</b>",
        showarrow=False,
        font=dict(size=13, color=mode_color),
        align="left",
        bgcolor="#f8fafc",
        bordercolor=mode_color,
        borderwidth=1,
        borderpad=6,
    )
