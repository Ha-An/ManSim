from __future__ import annotations

import json
import re
from pathlib import Path
from statistics import median
from typing import Any

from omegaconf import OmegaConf

ZONE_LAYOUT: dict[str, dict[str, float]] = {
    "Station1": {"x0": 0.80, "x1": 3.04, "y0": 1.00, "y1": 2.62},
    "Station2": {"x0": 3.30, "x1": 5.54, "y0": 1.00, "y1": 2.62},
    "Inspection": {"x0": 5.80, "x1": 8.04, "y0": 1.00, "y1": 2.62},
    "Warehouse": {"x0": 3.30, "x1": 5.45, "y0": 0.54, "y1": 0.86},
    "BatteryStation": {"x0": 3.30, "x1": 5.45, "y0": 2.84, "y1": 3.16},
}

ROUTE_EDGES: list[tuple[str, str]] = [
    ("Station1", "Station2"),
    ("Station1", "Inspection"),
    ("Station2", "Inspection"),
    ("Warehouse", "Station1"),
    ("Warehouse", "Station2"),
    ("Warehouse", "Inspection"),
    ("BatteryStation", "Warehouse"),
    ("BatteryStation", "Station1"),
    ("BatteryStation", "Station2"),
    ("BatteryStation", "Inspection"),
]

TASK_DEFAULT_DURATION_MIN: dict[str, float] = {
    "BATTERY_SWAP": 9.0,
    "BATTERY_DELIVERY": 10.0,
    "BATTERY_DELIVERY_LOW_BATTERY": 10.0,
    "BATTERY_DELIVERY_DISCHARGED": 10.0,
    "REPAIR_MACHINE": 20.0,
    "UNLOAD_MACHINE": 2.0,
    "SETUP_MACHINE": 3.0,
    "TRANSFER": 10.0,
    "INSPECT_PRODUCT": 10.0,
    "PREVENTIVE_MAINTENANCE": 30.0,
}

IMPORTANT_VISUAL_EVENTS = {
    "MACHINE_BROKEN": "FAULT",
    "ITEM_HANDOFF_COMPLETED": "Handoff",
    "COMPLETED_PRODUCT": "Completed",
    "INSPECT_PASS": "Pass",
}


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _nested_get(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _load_config(output_dir: Path) -> dict[str, Any]:
    cfg_path = output_dir / ".hydra" / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        payload = OmegaConf.to_container(OmegaConf.load(cfg_path), resolve=False)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _station_process_times(cfg: dict[str, Any]) -> dict[int, float]:
    process_blob = _nested_get(cfg, "scenario", "factory", "processing_time_min", default={})
    out: dict[int, float] = {}
    if isinstance(process_blob, dict):
        for raw_key, raw_value in process_blob.items():
            match = re.match(r"station(\d+)", str(raw_key))
            if match:
                out[int(match.group(1))] = _safe_float(raw_value)
    return out


def _machine_registry(events: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    processing_times = _station_process_times(cfg)
    machines_per_station = _safe_int(_nested_get(cfg, "scenario", "factory", "machines_per_station", default=0), 0)
    observed: set[str] = set()
    for event in events:
        entity_id = str(event.get("entity_id", "")).strip()
        if re.fullmatch(r"S\d+M\d+", entity_id):
            observed.add(entity_id)
    registry: dict[str, dict[str, Any]] = {}
    for station, process_time in processing_times.items():
        if machines_per_station <= 0:
            continue
        for index in range(1, machines_per_station + 1):
            machine_id = f"S{station}M{index}"
            registry[machine_id] = {
                "machine_id": machine_id,
                "station": station,
                "process_time_min": process_time,
            }
    for machine_id in observed:
        station_match = re.match(r"S(\d+)M(\d+)", machine_id)
        if not station_match:
            continue
        station = int(station_match.group(1))
        registry.setdefault(
            machine_id,
            {
                "machine_id": machine_id,
                "station": station,
                "process_time_min": processing_times.get(station, 0.0),
            },
        )
    return sorted(registry.values(), key=lambda row: (int(row.get("station", 0)), str(row.get("machine_id", ""))))


def _agent_ids(events: list[dict[str, Any]], run_meta: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    configured = _nested_get(run_meta, "llm", "openclaw", "worker_agent_ids", default=[])
    ids: list[str] = []
    if isinstance(configured, list):
        ids.extend(str(value).strip() for value in configured if str(value).strip())
    if ids:
        return ids
    num_agents = _safe_int(_nested_get(cfg, "scenario", "factory", "num_agents", default=0), 0)
    if num_agents > 0:
        return [f"A{index}" for index in range(1, num_agents + 1)]
    observed: set[str] = set()
    for event in events:
        entity_id = str(event.get("entity_id", "")).strip()
        if re.fullmatch(r"A\d+", entity_id):
            observed.add(entity_id)
    return sorted(observed)


def _task_duration_reference(events: list[dict[str, Any]]) -> dict[str, float]:
    active: dict[tuple[str, str], tuple[float, str]] = {}
    durations: dict[str, list[float]] = {}
    for event in events:
        event_type = str(event.get("type", "")).strip()
        entity_id = str(event.get("entity_id", "")).strip()
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        task_id = str(details.get("task_id", "")).strip()
        task_type = str(details.get("task_type", "")).strip().upper()
        if event_type == "AGENT_TASK_START" and task_id:
            active[(entity_id, task_id)] = (_safe_float(event.get("t")), task_type)
        elif event_type == "AGENT_TASK_END" and task_id:
            started = active.pop((entity_id, task_id), None)
            if started is None:
                continue
            started_at, started_task = started
            duration = max(0.0, _safe_float(event.get("t")) - started_at)
            durations.setdefault(started_task or task_type, []).append(duration)
    reference = dict(TASK_DEFAULT_DURATION_MIN)
    for task_type, values in durations.items():
        if values:
            reference[task_type] = float(median(values))
    return reference


def _max_event_time(events: list[dict[str, Any]]) -> float:
    if not events:
        return 0.0
    return max(_safe_float(event.get("t")) for event in events)


def _machine_labels(machine_registry: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in machine_registry:
        machine_id = str(row.get("machine_id", "")).strip()
        station = _safe_int(row.get("station"), 0)
        out[machine_id] = f"{machine_id} / Station {station}"
    return out


def _worker_labels(agent_ids: list[str]) -> dict[str, str]:
    return {agent_id: f"Worker {agent_id}" for agent_id in agent_ids}


def _build_payload(output_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    run_meta = _load_json(output_dir / "run_meta.json")
    kpi = _load_json(output_dir / "kpi.json")
    daily_summary = _load_json(output_dir / "daily_summary.json")
    cfg = _load_config(output_dir)

    machine_registry = _machine_registry(events, cfg)
    agent_ids = _agent_ids(events, run_meta, cfg)
    battery_period = _safe_float(_nested_get(cfg, "scenario", "agent", "battery_swap_period_min", default=180.0), 180.0)
    movement = {
        "warehouse_to_station_min": _safe_float(_nested_get(cfg, "scenario", "movement", "warehouse_to_station_min", default=8.0), 8.0),
        "station_to_station_min": _safe_float(_nested_get(cfg, "scenario", "movement", "station_to_station_min", default=5.0), 5.0),
        "setup_min": _safe_float(_nested_get(cfg, "scenario", "movement", "setup_min", default=3.0), 3.0),
        "unload_min": _safe_float(_nested_get(cfg, "scenario", "movement", "unload_min", default=2.0), 2.0),
        "to_battery_station_min": _safe_float(_nested_get(cfg, "scenario", "movement", "to_battery_station_min", default=4.0), 4.0),
        "default_min": _safe_float(_nested_get(cfg, "scenario", "movement", "default_min", default=6.0), 6.0),
    }
    total_days = _safe_int(run_meta.get("total_days"), _safe_int(_nested_get(cfg, "scenario", "horizon", "num_days", default=0), 0))
    minutes_per_day = _safe_float(run_meta.get("minutes_per_day"), _safe_float(_nested_get(cfg, "scenario", "horizon", "minutes_per_day", default=0.0), 0.0))
    return {
        "meta": {
            "run_id": output_dir.name,
            "output_dir": str(output_dir.resolve()),
            "decision_mode": str(run_meta.get("decision_mode", "")).strip(),
            "model": str(_nested_get(run_meta, "llm", "model", default="")).strip(),
            "server_url": str(_nested_get(run_meta, "llm", "server_url", default="")).strip(),
            "wall_clock_human": str(run_meta.get("wall_clock_human", "")).strip(),
            "total_products": _safe_int(kpi.get("total_products"), 0),
            "downstream_closure_ratio": _safe_float(kpi.get("downstream_closure_ratio"), 0.0),
            "total_days": total_days,
            "minutes_per_day": minutes_per_day,
            "events_count": len(events),
            "max_time": _max_event_time(events),
        },
        "factory": {
            "agent_ids": agent_ids,
            "machine_registry": machine_registry,
            "battery_swap_period_min": battery_period,
            "movement": movement,
            "repair_time_min": _safe_float(_nested_get(cfg, "scenario", "machine_failure", "repair_time_min", default=20.0), 20.0),
            "pm_time_min": _safe_float(_nested_get(cfg, "scenario", "machine_failure", "pm_time_min", default=30.0), 30.0),
        },
        "daily": {
            "rows": daily_summary.get("days", []) if isinstance(daily_summary.get("days", []), list) else [],
        },
        "derived": {
            "task_duration_reference": _task_duration_reference(events),
            "zone_layout": ZONE_LAYOUT,
            "route_edges": ROUTE_EDGES,
            "machine_labels": _machine_labels(machine_registry),
            "worker_labels": _worker_labels(agent_ids),
            "important_visual_events": IMPORTANT_VISUAL_EVENTS,
        },
        "events": events,
    }


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Operations Replay</title>
  <style>
    :root {
      --bg: #f4f7fb; --panel: #ffffff; --line: rgba(54, 78, 110, .14); --ink: #172435;
      --muted: #5f6f84; --accent: #3b82f6; --accent-2: #60a5fa; --good: #16a34a;
      --warn: #d97706; --danger: #dc2626; --repair: #7c3aed; --pm: #0284c7;
      --material: #8f65ff; --intermediate: #1cb9a0; --product: #f2a64a; --shadow: 0 18px 36px rgba(0,0,0,.28);
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; background: #ffffff; color: var(--ink); font-family: "Segoe UI", Arial, sans-serif; }
    body { padding: 18px; }
    button, select, input { font: inherit; }
    .app { display: grid; gap: 16px; }
    .headline { display: flex; justify-content: space-between; align-items: end; gap: 16px; flex-wrap: wrap; }
    .title-block h1 { margin: 0; font-size: 1.55rem; }
    .title-block p { margin: 6px 0 0; color: var(--muted); }
    .status-pill { display: inline-flex; align-items: center; padding: 8px 12px; border-radius: 999px; background: rgba(59,130,246,.08); border: 1px solid rgba(59,130,246,.18); color: #22446e; }
    .summary-strip { display: none; }
    .summary-card { background: linear-gradient(180deg, rgba(17,28,44,.98), rgba(12,20,31,.98)); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; box-shadow: var(--shadow); }
    .summary-card .label { color: var(--muted); font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; }
    .summary-card .value { margin-top: 6px; font-size: 1.12rem; font-weight: 700; }
    .controls { display: grid; gap: 10px; background: #ffffff; border: 1px solid var(--line); border-radius: 16px; padding: 14px 16px; box-shadow: 0 8px 24px rgba(28,44,68,.08); }
    .controls-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .controls button, .controls select { background: #ffffff; color: var(--ink); border: 1px solid rgba(117,158,220,.28); border-radius: 10px; padding: 8px 12px; cursor: pointer; }
    .controls .spacer { flex: 1 1 auto; }
    .time-readout { font-variant-numeric: tabular-nums; color: #294567; font-weight: 600; }
    .range-wrap { display: grid; gap: 8px; }
    input[type="range"] { width: 100%; accent-color: var(--accent); }
    .main-grid { display: grid; grid-template-columns: minmax(0,2.1fr) minmax(280px,.68fr); gap: 16px; align-items: start; }
    .panel { background: #ffffff; border: 1px solid var(--line); border-radius: 18px; box-shadow: 0 8px 24px rgba(28,44,68,.08); overflow: hidden; }
    .panel-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--line); }
    .panel-header h2 { margin: 0; font-size: 1rem; }
    .panel-header p { margin: 4px 0 0; color: var(--muted); font-size: .88rem; }
    .factory-stage { padding: 14px; }
    .factory-canvas { position: relative; height: 0; border-radius: 18px; background: #ffffff; border: 1px solid rgba(138,173,224,.18); overflow: hidden; }
    .factory-svg { position: absolute; inset: 0; width: 100%; height: 100%; display: none; }
    .route-line { stroke: transparent; stroke-width: 0; stroke-linecap: round; fill: none; }
    .zones-layer, .queue-layer, .machines-layer, .worker-layer, .floating-layer { position: absolute; inset: 0; }
    .zone { position: absolute; border: 1px solid rgba(142,177,228,.28); background: #f8fbff; border-radius: 18px; }
    .zone-warehouse { background: #eef4fb; }
    .zone-battery { background: #eef4fb; }
    .zone-label { position: absolute; font-size: 12px; color: #20344e; font-weight: 600; z-index: 1; }
    .queue-text { position: absolute; font-size: 10px; color: var(--muted); line-height: 1.25; max-width: 108px; z-index: 1; }
    .machine-node { position: absolute; width: 60px; height: 74px; padding: 4px 5px; border-radius: 10px; background: rgba(255,255,255,.96); border: 1px solid rgba(158,186,226,.26); box-shadow: 0 4px 10px rgba(28,44,68,.08); }
    .machine-node { z-index: 2; }
    .machine-node .head { display: flex; justify-content: space-between; gap: 6px; align-items: center; }
    .machine-node .name { font-size: 9px; font-weight: 700; letter-spacing: .01em; }
    .status { font-size: 8px; font-weight: 700; padding: 2px 4px; border-radius: 999px; border: 1px solid transparent; white-space: nowrap; }
    .status.processing { color: #166534; background: rgba(22,163,74,.08); border-color: rgba(22,163,74,.16); }
    .status.wait { color: #92400e; background: rgba(217,119,6,.08); border-color: rgba(217,119,6,.16); }
    .status.idle { color: #334155; background: rgba(148,163,184,.08); border-color: rgba(148,163,184,.16); }
    .status.danger { color: #991b1b; background: rgba(220,38,38,.08); border-color: rgba(220,38,38,.16); }
    .status.repair { color: #5b21b6; background: rgba(124,58,237,.08); border-color: rgba(124,58,237,.16); }
    .status.pm { color: #0c4a6e; background: rgba(2,132,199,.08); border-color: rgba(2,132,199,.16); }
    .progress-track { position: relative; margin-top: 5px; height: 4px; background: rgba(255,255,255,.07); border-radius: 999px; overflow: hidden; border: 1px solid rgba(255,255,255,.04); }
    .progress-fill { position: absolute; inset: 0 auto 0 0; height: 100%; border-radius: inherit; }
    .processing-fill { background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
    .work-fill { background: linear-gradient(90deg, #7cb2ff, var(--accent)); }
    .battery-fill { background: linear-gradient(90deg, #ff8f66, #ffd76d, #44cf78); }
    .warn-fill { background: rgba(242,196,91,.65); width: 100%; }
    .machine-progress { margin-top: 4px; }
    .machine-slot-row { display: grid; grid-template-columns: 8px 1fr 8px; gap: 4px; align-items: center; margin-top: 4px; }
    .machine-slot { width: 8px; height: 8px; border-radius: 50%; border: 1px solid rgba(255,255,255,.08); background: rgba(255,255,255,.04); }
    .machine-slot.off { opacity: .24; }
    .machine-slot.material { background: rgba(143,101,255,.82); }
    .machine-slot.intermediate { background: rgba(28,185,160,.82); }
    .machine-slot.product { background: rgba(242,166,74,.82); }
    .machine-flow { position: relative; height: 2px; border-radius: 999px; background: rgba(255,255,255,.06); }
    .machine-flow-dot { position: absolute; top: 50%; width: 6px; height: 6px; margin-top: -3px; border-radius: 50%; background: var(--accent); transform: translateX(-50%); opacity: .9; }
    .item-marker { position: absolute; width: 10px; height: 10px; transform: translate(-50%, -50%) rotate(45deg); border-radius: 2px; border: 1px solid rgba(15,24,36,.9); }
    .item-marker.material { background: var(--material); }
    .item-marker.intermediate { background: var(--intermediate); }
    .item-marker.product { background: var(--product); }
    .item-marker.completed { background: var(--good); }
    .item-ellipsis { position: absolute; transform: translate(-50%, -50%); font-size: 10px; color: var(--muted); }
    .worker-chip { position: absolute; min-width: 44px; max-width: 56px; padding: 5px 6px; border-radius: 10px; background: rgba(255,255,255,.96); border: 1px solid rgba(135,172,222,.28); box-shadow: 0 6px 12px rgba(28,44,68,.08); }
    .worker-chip .head { display: flex; justify-content: space-between; gap: 8px; align-items: center; }
    .worker-chip .name { font-size: 10px; font-weight: 700; }
    .worker-chip .status-text { font-size: 8px; color: var(--muted); }
    .worker-chip.idle { border-color: rgba(160,190,235,.18); }
    .worker-chip.moving { border-color: rgba(242,196,91,.24); }
    .worker-chip.working { border-color: rgba(53,193,111,.24); }
    .worker-chip.discharged { border-color: rgba(239,107,107,.28); }
    .floating-layer { pointer-events: none; }
    .float-label { position: absolute; padding: 6px 10px; border-radius: 999px; background: rgba(255,255,255,.96); border: 1px solid rgba(32,52,78,.12); color: #172435; font-size: 11px; font-weight: 700; transform: translate(-50%, -50%); opacity: .92; animation: floatFade 1.2s ease-out forwards; }
    .float-label.fault { color: #ffd8d8; border-color: rgba(239,107,107,.34); }
    .float-label.completed { color: #d9ffe7; border-color: rgba(53,193,111,.34); }
    .float-label.pass { color: #d8f6ff; border-color: rgba(94,200,255,.34); }
    .float-label.handoff { color: #fff0c4; border-color: rgba(242,196,91,.34); }
    @keyframes floatFade { 0% { opacity: 0; transform: translate(-50%, -38%); } 15% { opacity: .94; transform: translate(-50%, -50%); } 100% { opacity: 0; transform: translate(-50%, -92%); } }
    .side-column { display: grid; gap: 16px; align-content: start; }
    .workers-body, .events-body { padding: 14px 16px 16px; }
    .worker-grid { display: grid; gap: 12px; }
    .worker-card { border-radius: 14px; padding: 12px; background: #ffffff; border: 1px solid rgba(144,174,218,.18); display: grid; gap: 10px; }
    .worker-card .head { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
    .worker-card .name { font-weight: 700; }
    .worker-card .loc { color: var(--muted); font-size: .8rem; }
    .bar-label { display: flex; justify-content: space-between; gap: 8px; font-size: .78rem; color: var(--muted); }
    .bar { position: relative; height: 8px; background: rgba(255,255,255,.06); border-radius: 999px; overflow: hidden; border: 1px solid rgba(255,255,255,.05); }
    .bar > span { display: block; height: 100%; border-radius: inherit; }
    .worker-meta { display: flex; gap: 8px; flex-wrap: wrap; }
    .tag { padding: 4px 8px; border-radius: 999px; background: rgba(255,255,255,.05); color: #d5e5fb; font-size: .74rem; border: 1px solid rgba(255,255,255,.06); }
    .events-list { display: grid; gap: 8px; }
    .event-row { border-radius: 12px; background: #ffffff; border: 1px solid rgba(146,174,216,.18); padding: 10px 12px; display: grid; gap: 5px; }
    .event-row .top { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
    .event-row .type { font-weight: 700; font-size: .82rem; }
    .event-row .time { color: var(--muted); font-size: .78rem; font-variant-numeric: tabular-nums; }
    .event-row .sub { color: var(--muted); font-size: .76rem; }
    .event-row .details { font-size: .75rem; color: #d6e4f8; white-space: pre-wrap; line-height: 1.35; }
    .error-banner { background: rgba(239,107,107,.12); border: 1px solid rgba(239,107,107,.28); color: #ffd6d6; border-radius: 14px; padding: 14px 16px; }
    @media (max-width: 1280px) { .main-grid { grid-template-columns: 1fr; } }
    @media (max-width: 760px) { body { padding: 12px; } }
  </style>
</head>
<body>
  <div id="app" class="app">
    <div id="error-banner"></div>
    <header class="topbar">
      <div class="headline">
        <div class="title-block">
          <h1>Operations Replay</h1>
          <p id="subtitle">Loading replay payload...</p>
        </div>
        <div class="status-pill" id="run-pill">Replay payload</div>
      </div>
      <div class="summary-strip" id="summary-strip"></div>
    </header>
    <section class="controls">
      <div class="controls-row">
        <button id="play-btn" type="button">Play</button>
        <button id="reset-btn" type="button">Reset</button>
        <label>Speed
          <select id="speed-select">
            <option value="0.1">0.1x</option>
            <option value="0.5">0.5x</option>
            <option value="1" selected>1x</option>
            <option value="2">2x</option>
            <option value="5">5x</option>
            <option value="10">10x</option>
          </select>
        </label>
        <div class="spacer"></div>
        <div class="time-readout" id="time-readout">t=0.0m / Day 1 / 00:00</div>
      </div>
      <div class="range-wrap">
        <input id="time-slider" type="range" min="0" max="0" step="0.1" value="0">
      </div>
    </section>
    <main class="main-grid">
        <section class="panel">
          <div class="factory-stage">
            <div class="factory-canvas" id="factory-canvas">
            <svg class="factory-svg" id="factory-svg" viewBox="0 0 1000 760" preserveAspectRatio="none"></svg>
            <div id="zones-layer" class="zones-layer"></div>
            <div id="queue-layer" class="queue-layer"></div>
            <div id="machines-layer" class="machines-layer"></div>
            <div id="worker-layer" class="worker-layer"></div>
            <div id="floating-layer" class="floating-layer"></div>
          </div>
        </div>
      </section>
      <aside class="side-column">
        <section class="panel">
          <div class="workers-body">
            <div id="worker-grid" class="worker-grid"></div>
          </div>
        </section>
        <section class="panel">
          <div class="panel-header">
            <div>
              <h2>Recent Events</h2>
              <p>Latest domain events around the current replay position.</p>
            </div>
          </div>
          <div class="events-body">
            <div id="events-list" class="events-list"></div>
          </div>
        </section>
      </aside>
    </main>
  </div>
  <script>
    const PAYLOAD_URL = new URL("./operations_replay.json", window.location.href).toString();
    const BASE_MINUTES_PER_SECOND = 12;
    const FLOAT_TTL_MIN = 3.0;
    const RECENT_EVENT_LIMIT = 18;
    const MACHINE_NODE_WIDTH = 60;
    const MACHINE_NODE_HEIGHT = 74;
    const CANVAS_WIDTH = 1000;
    const CANVAS_HEIGHT = 760;
    const MAP_MARGIN_X = 42;
    const MAP_MARGIN_TOP = 34;
    const MAP_MARGIN_BOTTOM = 42;
    const LAYOUT_PAD_LEFT = 0.40;
    const LAYOUT_PAD_RIGHT = 1.10;
    const LAYOUT_PAD_TOP = 0.28;
    const LAYOUT_PAD_BOTTOM = 0.55;
    let activeLayoutMetrics = null;
    const MACHINE_STATUS_LABELS = { WAIT_INPUT: "WAIT_INPUT", PROCESSING: "PROCESSING", DONE_WAIT_UNLOAD: "WAIT_UNLOAD", SETUP: "SETUP", BROKEN: "BROKEN", UNDER_REPAIR: "REPAIR", UNDER_PM: "PM", IDLE: "IDLE" };
    const WORKER_STATUS_LABELS = { IDLE: "IDLE", MOVING: "MOVING", WORKING: "WORKING", DISCHARGED: "DISCHARGED" };
    const SHORT_ITEM = { material: "MAT", intermediate: "INT", product: "PRD", battery: "BAT", battery_fresh: "BAT", battery_spent: "BAT", unknown: "--" };
    let payload = null;
    let playing = false;
    let currentTime = 0;
    let speed = 1;
    let lastFrameTs = null;
    const elements = {
      subtitle: document.getElementById("subtitle"), runPill: document.getElementById("run-pill"),
      summaryStrip: document.getElementById("summary-strip"), playBtn: document.getElementById("play-btn"),
      resetBtn: document.getElementById("reset-btn"),
      speedSelect: document.getElementById("speed-select"), timeReadout: document.getElementById("time-readout"),
      timeSlider: document.getElementById("time-slider"), factoryCanvas: document.getElementById("factory-canvas"), factorySvg: document.getElementById("factory-svg"),
      zonesLayer: document.getElementById("zones-layer"), queueLayer: document.getElementById("queue-layer"),
      machinesLayer: document.getElementById("machines-layer"), workerLayer: document.getElementById("worker-layer"),
      floatingLayer: document.getElementById("floating-layer"), workerGrid: document.getElementById("worker-grid"),
      eventsList: document.getElementById("events-list"), errorBanner: document.getElementById("error-banner")
    };
    const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
    function escapeHtml(value) { return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\\\"/g, "&quot;").replace(/'/g, "&#39;"); }
    const fmtMinutes = (value) => `${Number(value || 0).toFixed(1)}m`;
    const fmtRatio = (value) => Number(value || 0).toFixed(3);
    function fmtSimClock(totalMinutes, minutesPerDay) { const minuteOfDay = minutesPerDay > 0 ? totalMinutes % minutesPerDay : totalMinutes; const hours = Math.floor(minuteOfDay / 60); const minutes = Math.floor(minuteOfDay % 60); return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`; }
    function dayAtTime(totalMinutes, minutesPerDay) { return (!minutesPerDay || minutesPerDay <= 0) ? 1 : Math.floor(totalMinutes / minutesPerDay) + 1; }
    function itemKindFromId(itemId) { const text = String(itemId || "").toUpperCase(); if (text.startsWith("PRODUCT")) return "product"; if (text.startsWith("INTERMEDIATE")) return "intermediate"; if (text.startsWith("MATERIAL")) return "material"; return "intermediate"; }
    function shortItemLabel(raw) { const key = String(raw || "").toLowerCase(); return SHORT_ITEM[key] || String(raw || "--").slice(0, 3).toUpperCase(); }
    function queueBucketFromEntity(entityId) { const text = String(entityId || ""); let m = text.match(/^material_queue_(\d+)$/); if (m) return { kind: "material", station: Number(m[1]) }; m = text.match(/^intermediate_queue_(\d+)$/); if (m) return { kind: "intermediate", station: Number(m[1]) }; return null; }
    function inferLocationZone(raw) {
      const text = String(raw || "");
      if (!text) return "Warehouse";
      if (payload && payload.derived.zone_layout[text]) return text;
      const lower = text.toLowerCase();
      if (lower.includes("warehouse")) return "Warehouse";
      if (lower.includes("battery")) return "BatteryStation";
      if (lower.includes("inspection")) return "Inspection";
      const stationMatch = text.match(/station(\d+)/i);
      if (stationMatch) return Number(stationMatch[1]) === 1 ? "Station1" : Number(stationMatch[1]) === 2 ? "Station2" : "Inspection";
      if (/^S1M\d+$/i.test(text)) return "Station1";
      if (/^S2M\d+$/i.test(text)) return "Station2";
      return "Warehouse";
    }
    function logicalExtents() {
      const values = Object.values(payload.derived.zone_layout || {});
      const xs = values.flatMap((z) => [Number(z.x0 || 0), Number(z.x1 || 0)]);
      const ys = values.flatMap((z) => [Number(z.y0 || 0), Number(z.y1 || 0)]);
      return {
        minX: Math.min(...xs),
        maxX: Math.max(...xs),
        minY: Math.min(...ys),
        maxY: Math.max(...ys),
      };
    }
    function layoutMetrics() {
      const ext = logicalExtents();
      const width = Math.max(elements.factoryCanvas?.clientWidth || 0, 1000);
      const paddedMinX = ext.minX - LAYOUT_PAD_LEFT;
      const paddedMaxX = ext.maxX + LAYOUT_PAD_RIGHT;
      const paddedMinY = ext.minY - LAYOUT_PAD_TOP;
      const paddedMaxY = ext.maxY + LAYOUT_PAD_BOTTOM;
      const logicalWidth = Math.max(paddedMaxX - paddedMinX, 0.001);
      const logicalHeight = Math.max(paddedMaxY - paddedMinY, 0.001);
      const usableWidth = Math.max(width - (MAP_MARGIN_X * 2), 320);
      const scale = usableWidth / logicalWidth;
      const height = Math.max(Math.ceil((logicalHeight * scale) + MAP_MARGIN_TOP + MAP_MARGIN_BOTTOM), 320);
      const originX = MAP_MARGIN_X - (paddedMinX * scale);
      const originY = MAP_MARGIN_TOP - (paddedMinY * scale);
      return { width, height, scale, originX, originY };
    }
    function syncCanvasLayout() {
      const metrics = layoutMetrics();
      activeLayoutMetrics = metrics;
      elements.factoryCanvas.style.height = `${metrics.height}px`;
      elements.factorySvg.setAttribute("viewBox", `0 0 ${metrics.width} ${metrics.height}`);
      return metrics;
    }
    function projectPoint(x, y) {
      const metrics = activeLayoutMetrics || layoutMetrics();
      const px = metrics.originX + (x * metrics.scale);
      const py = metrics.originY + (y * metrics.scale);
      return { x: px, y: py };
    }
    function zoneBounds(zone) {
      const layout = payload.derived.zone_layout[zone] || payload.derived.zone_layout.Warehouse;
      const p0 = projectPoint(Number(layout.x0 || 0), Number(layout.y0 || 0));
      const p1 = projectPoint(Number(layout.x1 || 0), Number(layout.y1 || 0));
      return { x0: p0.x, x1: p1.x, y0: p0.y, y1: p1.y };
    }
    function zoneCenter(zone) { const rect = zoneBounds(zone); return { x: (rect.x0 + rect.x1) / 2, y: (rect.y0 + rect.y1) / 2 }; }
    function stationSubLayout(zoneName) {
      const zone = zoneBounds(zoneName);
      const width = zone.x1 - zone.x0;
      const height = zone.y1 - zone.y0;
      const contentTop = zone.y0 + Math.max(74, height * 0.34);
      const contentBottom = zone.y1 - 14;
      const queueX = zone.x0 + 28;
      const queueTopY = contentTop + 10;
      const queueMidY = Math.min(contentBottom - 28, contentTop + 54);
      const outputX = zone.x0 + Math.max(104, width * 0.34);
      const outputY = contentTop + 24;
      const workerLaneX = zone.x0 + Math.max(134, width * 0.48);
      const workerTopY = contentTop + 16;
      const workerGap = 58;
      const machineX = zone.x1 - MACHINE_NODE_WIDTH - 20;
      const topMachineY = contentTop;
      const bottomMachineY = Math.min(contentBottom - MACHINE_NODE_HEIGHT, topMachineY + MACHINE_NODE_HEIGHT + 18);
      return {
        zone,
        width,
        height,
        queueX,
        queueTopY,
        queueMidY,
        outputX,
        outputY,
        workerLaneX,
        workerTopY,
        workerGap,
        machineX,
        topMachineY,
        bottomMachineY,
      };
    }
    function binaryEventIndex(events, time) { let lo = 0, hi = events.length - 1, ans = -1; while (lo <= hi) { const mid = Math.floor((lo + hi) / 2); if (Number(events[mid].t || 0) <= time) { ans = mid; lo = mid + 1; } else { hi = mid - 1; } } return ans; }
    function shortestZonePath(srcZone, dstZone) {
      const src = inferLocationZone(srcZone), dst = inferLocationZone(dstZone);
      if (src === dst) return [src];
      const graph = {};
      for (const [a, b] of payload.derived.route_edges) { if (!graph[a]) graph[a] = []; if (!graph[b]) graph[b] = []; graph[a].push(b); graph[b].push(a); }
      const visited = new Set([src]);
      const queue = [[src, [src]]];
      while (queue.length) {
        const [node, path] = queue.shift();
        for (const next of graph[node] || []) {
          if (visited.has(next)) continue;
          const nextPath = path.concat([next]);
          if (next === dst) return nextPath;
          visited.add(next);
          queue.push([next, nextPath]);
        }
      }
      return [src, dst];
    }
    function interpolateOnRoute(srcZone, dstZone, progress) {
      const path = shortestZonePath(srcZone, dstZone);
      const points = path.map(zoneCenter);
      if (points.length === 1) return points[0];
      let total = 0; const lengths = [];
      for (let i = 0; i < points.length - 1; i += 1) { const dx = points[i + 1].x - points[i].x; const dy = points[i + 1].y - points[i].y; const seg = Math.max(1e-9, Math.sqrt(dx * dx + dy * dy)); lengths.push(seg); total += seg; }
      const target = clamp(progress, 0, 1) * total;
      let acc = 0;
      for (let i = 0; i < lengths.length; i += 1) { const seg = lengths[i]; if (target <= acc + seg) { const local = (target - acc) / seg; return { x: points[i].x + (points[i + 1].x - points[i].x) * local, y: points[i].y + (points[i + 1].y - points[i].y) * local }; } acc += seg; }
      return points[points.length - 1];
    }
    function initialState() {
      const machines = {};
      for (const row of payload.factory.machine_registry || []) { const machineId = String(row.machine_id || ""); machines[machineId] = { machine_id: machineId, station: Number(row.station || 0), process_time_min: Number(row.process_time_min || 0), status: "IDLE", phaseStartAt: null, phaseDuration: null, slots: { material: false, intermediate: false, output: "" } }; }
      const workers = {};
      for (const agentId of payload.factory.agent_ids || []) { workers[agentId] = { agent_id: agentId, zone: "Warehouse", status: "IDLE", activeTask: null, taskStartedAt: null, taskDuration: null, lastBatterySwap: 0, discharged: false, carrying: "", move: null, pausedMove: null }; }
      return { machines, workers, queues: { material: { 1: [], 2: [] }, intermediate: { 2: [], 4: [] }, output_buffer: { 1: [], 2: [], 4: [] }, warehouse_completed: [] }, activeTransferFromStation: {}, completedProducts: 0, recentEvents: [], floatLabels: [] };
    }
    function ensureWorker(state, agentId) { if (!state.workers[agentId]) state.workers[agentId] = { agent_id: agentId, zone: "Warehouse", status: "IDLE", activeTask: null, taskStartedAt: null, taskDuration: null, lastBatterySwap: 0, discharged: false, carrying: "", move: null, pausedMove: null }; return state.workers[agentId]; }
    function ensureMachine(state, machineId) { if (!state.machines[machineId]) { const match = String(machineId).match(/^S(\d+)M(\d+)$/); state.machines[machineId] = { machine_id: machineId, station: match ? Number(match[1]) : 0, process_time_min: 0, status: "IDLE", phaseStartAt: null, phaseDuration: null, slots: { material: false, intermediate: false, output: "" } }; } return state.machines[machineId]; }
    function addRecentEvent(state, event) { state.recentEvents.push(event); if (state.recentEvents.length > RECENT_EVENT_LIMIT * 3) state.recentEvents = state.recentEvents.slice(-RECENT_EVENT_LIMIT * 3); }
    function addFloatLabel(state, event, now) {
      const label = payload.derived.important_visual_events[event.type];
      if (!label) return;
      const age = now - Number(event.t || 0);
      if (age > FLOAT_TTL_MIN) return;
      let zone = inferLocationZone(event.location);
      if (/^S\d+M\d+$/.test(String(event.entity_id || ""))) zone = inferLocationZone(event.entity_id);
      if (/^A\d+$/.test(String(event.entity_id || ""))) { const worker = state.workers[event.entity_id]; if (worker) zone = worker.zone || zone; }
      state.floatLabels.push({ text: label, zone, age });
    }
    function applyQueuePush(state, entityId, details) { const bucket = queueBucketFromEntity(entityId); if (!bucket || !(bucket.station in state.queues[bucket.kind])) return; state.queues[bucket.kind][bucket.station].push(String(details.item_id || "")); }
    function applyQueuePop(state, entityId, details) { const bucket = queueBucketFromEntity(entityId); if (!bucket || !(bucket.station in state.queues[bucket.kind])) return; const items = state.queues[bucket.kind][bucket.station]; const itemId = String(details.item_id || ""); const index = items.indexOf(itemId); if (index >= 0) items.splice(index, 1); else if (items.length) items.shift(); }
    function applyEvent(state, event, now) {
      const type = String(event.type || ""), entityId = String(event.entity_id || ""), details = event.details && typeof event.details === "object" ? event.details : {};
      addRecentEvent(state, event);
      addFloatLabel(state, event, now);
      if (type === "QUEUE_PUSH") return applyQueuePush(state, entityId, details);
      if (type === "QUEUE_POP") return applyQueuePop(state, entityId, details);
      if (type === "COMPLETED_PRODUCT") { state.completedProducts += 1; return; }
      if (type === "AGENT_TASK_START") {
        const worker = ensureWorker(state, entityId), taskType = String(details.task_type || "").toUpperCase(), payloadRow = details.payload && typeof details.payload === "object" ? details.payload : {};
        worker.activeTask = { task_id: String(details.task_id || ""), task_type: taskType, payload: payloadRow, target: String(payloadRow.machine_id || payloadRow.target_agent_id || payloadRow.station || "") };
        worker.taskStartedAt = Number(event.t || 0); worker.taskDuration = Number(payload.derived.task_duration_reference[taskType] || payload.factory.movement.default_min || 6); worker.status = worker.discharged ? "DISCHARGED" : "WORKING";
        if (taskType === "TRANSFER" && String(payloadRow.transfer_kind || "").toLowerCase() === "inter_station") { const fromStation = Number(payloadRow.from_station || 0); if (fromStation in state.queues.output_buffer) state.activeTransferFromStation[entityId] = fromStation; }
        return;
      }
      if (type === "AGENT_TASK_END") {
        const worker = ensureWorker(state, entityId), taskType = String(details.task_type || "").toUpperCase(), payloadRow = details.payload && typeof details.payload === "object" ? details.payload : {};
        delete state.activeTransferFromStation[entityId];
        if (taskType === "UNLOAD_MACHINE" && payloadRow.machine_id) { const machine = ensureMachine(state, String(payloadRow.machine_id)); if (machine.status !== "BROKEN") { machine.status = "IDLE"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = null; machine.slots.output = ""; } }
        if (taskType === "BATTERY_SWAP") { worker.lastBatterySwap = Number(event.t || 0); worker.discharged = false; }
        if (taskType === "TRANSFER" || taskType === "INSPECT_PRODUCT") worker.carrying = "";
        worker.activeTask = null; worker.taskStartedAt = null; worker.taskDuration = null; worker.status = worker.discharged ? "DISCHARGED" : (worker.move || worker.pausedMove ? "MOVING" : "IDLE");
        return;
      }
      if (type === "AGENT_MOVE_START") { const worker = ensureWorker(state, entityId); worker.move = { from: inferLocationZone(details.from || worker.zone || "Warehouse"), to: inferLocationZone(details.to || event.location || worker.zone || "Warehouse"), startedAt: Number(event.t || 0), duration: Math.max(0.1, Number(details.duration || payload.factory.movement.default_min || 6)) }; worker.pausedMove = null; worker.status = worker.discharged ? "DISCHARGED" : "MOVING"; return; }
      if (type === "AGENT_MOVE_INTERRUPTED") { const worker = ensureWorker(state, entityId), move = worker.move; const from = inferLocationZone(details.from || (move ? move.from : worker.zone)); const to = inferLocationZone(details.to || (move ? move.to : worker.zone)); let progress = Number(details.progress || 0); if (!(progress >= 0 && progress <= 1) && move) progress = clamp((Number(event.t || 0) - move.startedAt) / Math.max(0.1, move.duration), 0, 1); worker.pausedMove = { from, to, progress: clamp(progress, 0, 1) }; worker.move = null; worker.status = worker.discharged ? "DISCHARGED" : "MOVING"; return; }
      if (type === "AGENT_MOVE_END") { const worker = ensureWorker(state, entityId); worker.zone = inferLocationZone(details.to || event.location || worker.zone || "Warehouse"); worker.move = null; worker.pausedMove = null; worker.status = worker.discharged ? "DISCHARGED" : (worker.activeTask ? "WORKING" : "IDLE"); return; }
      if (type === "AGENT_DISCHARGED") { const worker = ensureWorker(state, entityId); worker.discharged = true; worker.status = "DISCHARGED"; return; }
      if (type === "AGENT_RECHARGED" || type === "BATTERY_SWAP" || type === "BATTERY_DELIVERED") { const targetId = String(details.target_agent_id || entityId || ""); const worker = ensureWorker(state, targetId); worker.lastBatterySwap = Number(event.t || 0); worker.discharged = false; if (worker.status === "DISCHARGED") worker.status = worker.activeTask ? "WORKING" : (worker.move || worker.pausedMove ? "MOVING" : "IDLE"); return; }
      if (type === "AGENT_PICK_ITEM") { const worker = ensureWorker(state, entityId); worker.carrying = String(details.item_type || "").toLowerCase() || "unknown"; const fromStation = state.activeTransferFromStation[entityId]; const itemId = String(details.item_id || ""); if (fromStation in state.queues.output_buffer && itemId) { const items = state.queues.output_buffer[fromStation]; const index = items.indexOf(itemId); if (index >= 0) items.splice(index, 1); } return; }
      if (type === "AGENT_DROP_ITEM") { const worker = ensureWorker(state, entityId), machineId = String(details.to || ""), itemType = String(details.item_type || "").toLowerCase(); worker.carrying = ""; if (machineId in state.machines) { const machine = ensureMachine(state, machineId); if (itemType === "material") machine.slots.material = true; if (itemType === "intermediate") machine.slots.intermediate = true; machine.slots.output = ""; } return; }
      if (type === "ITEM_MOVED") {
        const itemId = entityId, fromLoc = String(details.from || ""), toLoc = String(details.to || ""), itemType = String(details.item_type || "").toLowerCase();
        const outputTo = toLoc.match(/^output_buffer_station_(\d+)$/), outputFrom = fromLoc.match(/^output_buffer_station_(\d+)$/);
        if (outputTo) { const station = Number(outputTo[1]); if (station in state.queues.output_buffer) state.queues.output_buffer[station].push(itemId); }
        if (outputFrom) { const station = Number(outputFrom[1]); if (station in state.queues.output_buffer) { const items = state.queues.output_buffer[station]; const index = items.indexOf(itemId); if (index >= 0) items.splice(index, 1); } }
        if (fromLoc in state.machines) state.machines[fromLoc].slots.output = "";
        const kind = itemType || itemKindFromId(itemId);
        if (kind === "product" && toLoc === "Warehouse") { state.queues.warehouse_completed.push(itemId); state.completedProducts = state.queues.warehouse_completed.length; }
        if (kind === "product" && fromLoc === "Warehouse") { const items = state.queues.warehouse_completed; const index = items.indexOf(itemId); if (index >= 0) items.splice(index, 1); else if (items.length) items.shift(); state.completedProducts = items.length; }
        return;
      }
      if (type === "MACHINE_SETUP_START") { const machine = ensureMachine(state, entityId); machine.status = "SETUP"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = Number(payload.factory.movement.setup_min || 3); return; }
      if (type === "MACHINE_SETUP_END") { const machine = ensureMachine(state, entityId); machine.status = String(details.outcome || "").toLowerCase() === "completed" ? "IDLE" : "WAIT_INPUT"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = null; return; }
      if (type === "MACHINE_START") { const machine = ensureMachine(state, entityId); machine.status = "PROCESSING"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = Number(machine.process_time_min || 0); machine.slots.material = Boolean(details.input_material); machine.slots.intermediate = Boolean(details.input_intermediate); machine.slots.output = ""; return; }
      if (type === "MACHINE_END") { const machine = ensureMachine(state, entityId); machine.status = "DONE_WAIT_UNLOAD"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = Number(payload.factory.movement.unload_min || 2); machine.slots.material = false; machine.slots.intermediate = false; machine.slots.output = String(details.output_intermediate || "").toUpperCase().startsWith("PRODUCT") ? "product" : (machine.station >= 2 ? "product" : "intermediate"); return; }
      if (type === "MACHINE_BROKEN") { const machine = ensureMachine(state, entityId); machine.status = "BROKEN"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = Number(payload.factory.repair_time_min || 20); return; }
      if (type === "MACHINE_REPAIR_START") { const machine = ensureMachine(state, entityId); machine.status = "UNDER_REPAIR"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = Number(payload.factory.repair_time_min || 20); return; }
      if (type === "MACHINE_REPAIRED") { const machine = ensureMachine(state, entityId); machine.status = "IDLE"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = null; return; }
      if (type === "MACHINE_PM_START") { const machine = ensureMachine(state, entityId); machine.status = "UNDER_PM"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = Number(payload.factory.pm_time_min || 30); return; }
      if (type === "MACHINE_PM_END") { const machine = ensureMachine(state, entityId); machine.status = "IDLE"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = null; return; }
      if (type === "MACHINE_ABORTED") { const machine = ensureMachine(state, entityId); machine.status = "BROKEN"; machine.phaseStartAt = Number(event.t || 0); machine.phaseDuration = Number(payload.factory.repair_time_min || 20); machine.slots.material = false; machine.slots.intermediate = false; machine.slots.output = ""; }
    }
    function buildStateAt(time) { const state = initialState(); const idx = binaryEventIndex(payload.events, time); for (let i = 0; i <= idx; i += 1) applyEvent(state, payload.events[i], time); state.recentEvents = state.recentEvents.slice(-RECENT_EVENT_LIMIT).reverse(); return state; }
    function machineStatusClass(status) {
      if (status === "PROCESSING") return "processing";
      if (status === "SETUP" || status === "DONE_WAIT_UNLOAD" || status === "WAIT_INPUT") return "wait";
      if (status === "BROKEN") return "danger";
      if (status === "UNDER_REPAIR") return "repair";
      if (status === "UNDER_PM") return "pm";
      return "idle";
    }
    function machineProgress(machine) { if (machine.phaseStartAt === null || machine.phaseDuration === null || machine.phaseDuration <= 0) return null; return clamp((currentTime - machine.phaseStartAt) / machine.phaseDuration, 0, 1); }
    function batteryRatio(worker) { const period = Math.max(Number(payload.factory.battery_swap_period_min || 180), 1); if (worker.discharged) return 0; const elapsed = Math.max(0, currentTime - Number(worker.lastBatterySwap || 0)); return clamp(1 - (elapsed / period), 0, 1); }
    function taskProgress(worker) { if (!worker.activeTask || worker.taskStartedAt === null || !worker.taskDuration) return null; return clamp((currentTime - worker.taskStartedAt) / worker.taskDuration, 0, 1); }
    function machineCardPosition(machineId) {
      const machine = payload.factory.machine_registry.find((row) => row.machine_id === machineId);
      if (!machine) return { x: 0, y: 0 };
      const station = Number(machine.station || 0);
      const zoneName = station === 1 ? "Station1" : station === 2 ? "Station2" : "Inspection";
      const zone = zoneBounds(zoneName);
      const peers = payload.factory.machine_registry.filter((row) => Number(row.station || 0) === station).map((row) => row.machine_id).sort();
      const index = Math.max(0, peers.indexOf(machineId));
      const nodeWidth = MACHINE_NODE_WIDTH;
      const nodeHeight = MACHINE_NODE_HEIGHT;
      if (zoneName === "Station1" || zoneName === "Station2") {
        const layout = stationSubLayout(zoneName);
        return {
          x: layout.machineX,
          y: index === 0 ? layout.topMachineY : layout.bottomMachineY,
        };
      }
      const zoneHeight = zone.y1 - zone.y0;
      const padX = 16;
      const labelBand = Math.max(46, zoneHeight * 0.20);
      const padBottom = 12;
      const topY = zone.y0 + labelBand;
      const bottomY = zone.y1 - padBottom - nodeHeight;
      const count = Math.max(peers.length, 1);
      let y;
      if (count === 1) {
        y = (topY + bottomY) / 2;
      } else {
        y = count === 2
          ? (index === 0 ? topY : bottomY)
          : topY + (((bottomY - topY) / Math.max(count - 1, 1)) * index);
      }
      return {
        x: zone.x1 - nodeWidth - padX,
        y: Math.min(Math.max(topY, y), bottomY),
      };
    }
    function workerMachineTarget(worker) {
      if (!(worker.activeTask && worker.activeTask.task_type)) return "";
      const taskType = String(worker.activeTask.task_type).toUpperCase();
      if (!new Set(["LOAD_MACHINE", "UNLOAD_MACHINE", "SETUP_MACHINE", "PREVENTIVE_MAINTENANCE", "REPAIR_MACHINE"]).has(taskType)) return "";
      return String(worker.activeTask.payload.machine_id || worker.activeTask.target || "");
    }
    function workerPlacementKind(worker) {
      if (worker.move || worker.pausedMove) return "move";
      if (workerMachineTarget(worker)) return "machine";
      return "zone";
    }
    function machineWorkerSlots(machineId, count) {
      const pos = machineCardPosition(machineId);
      const slots = [
        { x: pos.x - 26, y: pos.y + 10 },
        { x: pos.x - 26, y: pos.y + 30 },
        { x: pos.x + 84, y: pos.y + 10 },
        { x: pos.x + 84, y: pos.y + 30 },
        { x: pos.x + 29, y: pos.y - 18 },
        { x: pos.x + 29, y: pos.y + 54 },
      ];
      return Array.from({ length: count }, (_, index) => slots[index] || { x: pos.x + 29, y: pos.y + 54 + ((index - slots.length + 1) * 20) });
    }
    function zoneWorkerSlots(zoneName, count) {
      if (zoneName === "Station1" || zoneName === "Station2") {
        const layout = stationSubLayout(zoneName);
        return Array.from({ length: count }, (_, index) => ({
          x: layout.workerLaneX + ((index % 2) * 56),
          y: layout.workerTopY + (Math.floor(index / 2) * layout.workerGap),
        }));
      }
      const zone = zoneBounds(zoneName || "Warehouse");
      const width = zone.x1 - zone.x0;
      const height = zone.y1 - zone.y0;
      const cx = (zone.x0 + zone.x1) / 2;
      const cy = (zone.y0 + zone.y1) / 2;
      const spacing = Math.max(44, Math.min(68, width * 0.18));
      const topBand = zone.y0 + Math.max(28, height * 0.34);
      const lowerBand = zone.y0 + Math.max(48, height * 0.58);
      const positions = [];
      if (count <= 1) {
        return [{ x: cx, y: cy }];
      }
      if (count === 2) {
        return [
          { x: cx - spacing * 0.55, y: cy },
          { x: cx + spacing * 0.55, y: cy },
        ];
      }
      if (count === 3) {
        return [
          { x: cx - spacing, y: cy },
          { x: cx, y: topBand },
          { x: cx + spacing, y: cy },
        ];
      }
      const rowCapacity = 3;
      const rowGap = Math.max(22, height * 0.18);
      for (let index = 0; index < count; index += 1) {
        const row = Math.floor(index / rowCapacity);
        const col = index % rowCapacity;
        const colsInRow = Math.min(rowCapacity, count - (row * rowCapacity));
        const rowWidth = (colsInRow - 1) * spacing;
        positions.push({
          x: cx - (rowWidth / 2) + (col * spacing),
          y: lowerBand + (row * rowGap),
        });
      }
      return positions;
    }
    function workerPosition(worker) {
      if (worker.move) { const progress = clamp((currentTime - worker.move.startedAt) / Math.max(worker.move.duration || 1, 0.1), 0, 1); return interpolateOnRoute(worker.move.from, worker.move.to, progress); }
      if (worker.pausedMove) return interpolateOnRoute(worker.pausedMove.from, worker.pausedMove.to, worker.pausedMove.progress);
      const machineId = workerMachineTarget(worker);
      if (machineId) { const pos = machineCardPosition(machineId); return { x: pos.x + 58, y: pos.y + 18 }; }
      return zoneCenter(worker.zone || "Warehouse");
    }
    function renderZones(state) {
      const zoneHtml = [], textHtml = [];
      for (const [zoneName] of Object.entries(payload.derived.zone_layout)) {
        const rect = zoneBounds(zoneName);
        const zoneClass = zoneName === "Warehouse" ? "zone zone-warehouse" : zoneName === "BatteryStation" ? "zone zone-battery" : "zone";
        zoneHtml.push(`<div class="${zoneClass}" style="left:${rect.x0}px;top:${rect.y0}px;width:${rect.x1 - rect.x0}px;height:${rect.y1 - rect.y0}px"></div>`);
        textHtml.push(`<div class="zone-label" style="left:${rect.x0 + 10}px;top:${rect.y0 - 20}px">${escapeHtml(zoneName)}</div>`);
        let queueText = "";
        if (zoneName === "Station1") queueText = `Material Queue: ${state.queues.material[1].length}<br>Output Buffer: ${state.queues.output_buffer[1].length}`;
        else if (zoneName === "Station2") queueText = `Material Queue: ${state.queues.material[2].length}<br>Intermediate Queue: ${state.queues.intermediate[2].length}<br>Output Buffer: ${state.queues.output_buffer[2].length}`;
        else if (zoneName === "Inspection") queueText = `Product Queue: ${state.queues.intermediate[4].length}<br>Pass Output: ${state.queues.output_buffer[4].length}`;
        else if (zoneName === "Warehouse") queueText = `Completed Products: ${state.queues.warehouse_completed.length}`;
        else queueText = `Battery swap period: ${fmtMinutes(payload.factory.battery_swap_period_min || 0)}`;
        textHtml.push(`<div class="queue-text" style="left:${rect.x0 + 8}px;top:${rect.y0 + 6}px">${queueText}</div>`);
      }
      elements.zonesLayer.innerHTML = zoneHtml.join("") + textHtml.join("");
    }
    function renderQueues(state) {
      const html = [];
      function addMarkers(items, anchorX, anchorY, rightAlign, cls) {
        const visible = items.slice(0, 5);
        visible.forEach((itemId, index) => {
          const x = rightAlign ? anchorX - index * 12 : anchorX + index * 12;
          const itemClass = cls === "auto" ? itemKindFromId(itemId) : cls;
          html.push(`<div class="item-marker ${escapeHtml(itemClass)}" title="${escapeHtml(itemId)}" style="left:${x}px;top:${anchorY}px"></div>`);
        });
        if (items.length > visible.length) {
          const x = rightAlign ? anchorX - visible.length * 12 : anchorX + visible.length * 12;
          html.push(`<div class="item-ellipsis" style="left:${x}px;top:${anchorY}px">...</div>`);
        }
      }
      const station1 = zoneBounds("Station1"), station2 = zoneBounds("Station2"), inspection = zoneBounds("Inspection"), warehouse = zoneBounds("Warehouse");
      const s1Layout = stationSubLayout("Station1");
      const s2Layout = stationSubLayout("Station2");
      const s1h = station1.y1 - station1.y0;
      const s2h = station2.y1 - station2.y0;
      const ih = inspection.y1 - inspection.y0;
      const wh = warehouse.y1 - warehouse.y0;
      addMarkers(state.queues.material[1], s1Layout.queueX, s1Layout.queueTopY, false, "material");
      addMarkers(state.queues.material[2], s2Layout.queueX, s2Layout.queueTopY, false, "material");
      addMarkers(state.queues.intermediate[2], s2Layout.queueX, s2Layout.queueMidY, false, "intermediate");
      addMarkers(state.queues.intermediate[4], inspection.x0 + 24, inspection.y0 + (ih * 0.32), false, "product");
      addMarkers(state.queues.output_buffer[1], s1Layout.outputX, s1Layout.outputY, false, "auto");
      addMarkers(state.queues.output_buffer[2], s2Layout.outputX, s2Layout.outputY, false, "auto");
      addMarkers(state.queues.output_buffer[4], inspection.x0 + 24, inspection.y0 + (ih * 0.52), false, "auto");
      addMarkers(state.queues.warehouse_completed, warehouse.x0 + 24, warehouse.y0 + (wh * 0.55), false, "completed");
      elements.queueLayer.innerHTML = html.join("");
    }
    function renderMachines(state) {
      const html = [];
      const machines = Object.values(state.machines).sort((a, b) => String(a.machine_id).localeCompare(String(b.machine_id)));
      for (const machine of machines) {
        const pos = machineCardPosition(machine.machine_id), status = machine.status || "IDLE", progress = machineProgress(machine);
        let progressHtml = `<div class="progress-track"><div class="progress-fill warn-fill"></div></div>`;
        if (status === "PROCESSING" && progress !== null) progressHtml = `<div class="progress-track"><div class="progress-fill processing-fill" style="width:${(progress * 100).toFixed(1)}%"></div></div>`;
        else if ((status === "SETUP" || status === "UNDER_REPAIR" || status === "UNDER_PM") && progress !== null) progressHtml = `<div class="progress-track"><div class="progress-fill work-fill" style="width:${(progress * 100).toFixed(1)}%"></div></div>`;
        const outputKind = machine.slots.output || "unknown";
        const flowDot = status === "PROCESSING" && progress !== null ? `<div class="machine-flow-dot" style="left:${(progress * 100).toFixed(1)}%"></div>` : "";
        const shortStatus = status === "PROCESSING" ? "RUN" : status === "DONE_WAIT_UNLOAD" ? "WAIT" : status === "UNDER_REPAIR" ? "FIX" : status === "UNDER_PM" ? "PM" : status === "BROKEN" ? "DOWN" : status === "SETUP" ? "SET" : status === "WAIT_INPUT" ? "WAIT" : "IDLE";
        html.push(`<article class="machine-node" style="left:${pos.x}px;top:${pos.y}px"><div class="head"><div class="name">${escapeHtml(machine.machine_id)}</div><div class="status ${machineStatusClass(status)}">${escapeHtml(shortStatus)}</div></div><div class="machine-progress">${progressHtml}</div><div class="machine-slot-row"><div class="machine-slot material ${machine.slots.material ? "" : "off"}"></div><div class="machine-flow">${flowDot}</div><div class="machine-slot ${escapeHtml(outputKind)} ${machine.slots.output ? "" : "off"}"></div></div><div class="machine-slot-row"><div class="machine-slot intermediate ${machine.slots.intermediate ? "" : "off"}"></div><div></div><div></div></div></article>`);
      }
      elements.machinesLayer.innerHTML = html.join("");
    }
    function spreadOffsets(count) {
      if (count <= 1) return [{ dx: 0, dy: 0 }];
      if (count === 2) return [{ dx: -42, dy: 0 }, { dx: 42, dy: 0 }];
      if (count === 3) return [{ dx: -48, dy: 10 }, { dx: 0, dy: -24 }, { dx: 48, dy: 10 }];
      if (count === 4) return [{ dx: -52, dy: -10 }, { dx: 52, dy: -10 }, { dx: -52, dy: 22 }, { dx: 52, dy: 22 }];
      return [
        { dx: 0, dy: -28 },
        { dx: -56, dy: 2 },
        { dx: 56, dy: 2 },
        { dx: -32, dy: 30 },
        { dx: 32, dy: 30 },
        { dx: 0, dy: 56 },
      ];
    }
    function clampChipPoint(pos) {
      const metrics = activeLayoutMetrics || layoutMetrics();
      return {
        x: clamp(pos.x, 34, metrics.width - 34),
        y: clamp(pos.y, 26, metrics.height - 24),
      };
    }
    function renderWorkers(state) {
      const chips = [], cards = [];
      const workers = Object.values(state.workers).sort((a, b) => String(a.agent_id).localeCompare(String(b.agent_id)));
      const movingBuckets = {};
      const zoneGroups = {};
      const machineGroups = {};
      workers.forEach((worker) => {
        const placement = workerPlacementKind(worker);
        if (placement === "move") {
          const pos = workerPosition(worker);
          const key = `${Math.round(pos.x / 22)}:${Math.round(pos.y / 22)}`;
          if (!movingBuckets[key]) movingBuckets[key] = [];
          movingBuckets[key].push({ worker, pos });
          return;
        }
        if (placement === "machine") {
          const machineId = workerMachineTarget(worker);
          if (!machineGroups[machineId]) machineGroups[machineId] = [];
          machineGroups[machineId].push(worker);
          return;
        }
        const zone = worker.zone || "Warehouse";
        if (!zoneGroups[zone]) zoneGroups[zone] = [];
        zoneGroups[zone].push(worker);
      });
      const chipEntries = [];
      Object.values(movingBuckets).forEach((entries) => {
        const offsets = spreadOffsets(entries.length);
        entries.forEach((entry, index) => {
          const offset = offsets[index] || { dx: 0, dy: index * 14 };
          chipEntries.push({ worker: entry.worker, pos: clampChipPoint({ x: entry.pos.x + offset.dx, y: entry.pos.y + offset.dy }) });
        });
      });
      Object.entries(machineGroups).forEach(([machineId, groupedWorkers]) => {
        const slots = machineWorkerSlots(machineId, groupedWorkers.length);
        groupedWorkers.forEach((worker, index) => {
          chipEntries.push({ worker, pos: clampChipPoint(slots[index] || slots[slots.length - 1]) });
        });
      });
      Object.entries(zoneGroups).forEach(([zone, groupedWorkers]) => {
        const slots = zoneWorkerSlots(zone, groupedWorkers.length);
        groupedWorkers.forEach((worker, index) => {
          chipEntries.push({ worker, pos: clampChipPoint(slots[index] || slots[slots.length - 1]) });
        });
      });
      for (const entry of chipEntries.sort((a, b) => String(a.worker.agent_id).localeCompare(String(b.worker.agent_id)))) {
        const worker = entry.worker;
        const pos = entry.pos;
        const batteryPct = (batteryRatio(worker) * 100).toFixed(1), taskPct = taskProgress(worker), statusClass = worker.status === "WORKING" ? "working" : worker.status === "MOVING" ? "moving" : worker.status === "DISCHARGED" ? "discharged" : "idle";
        chips.push(`<div class="worker-chip ${statusClass}" style="left:${pos.x - 24}px;top:${pos.y - 13}px"><div class="head"><span class="name">${escapeHtml(worker.agent_id)}</span><span class="status-text">${escapeHtml(worker.status === "WORKING" ? "W" : worker.status === "MOVING" ? "M" : worker.status === "DISCHARGED" ? "D" : "I")}</span></div><div class="progress-track"><div class="progress-fill battery-fill" style="width:${batteryPct}%"></div></div></div>`);
        cards.push(`<article class="worker-card"><div class="head"><div><div class="name">${escapeHtml(payload.derived.worker_labels[worker.agent_id] || worker.agent_id)}</div><div class="loc">${escapeHtml(worker.zone || "Warehouse")}</div></div><div class="status ${statusClass}">${escapeHtml(WORKER_STATUS_LABELS[worker.status] || worker.status)}</div></div><div><div class="bar-label"><span>Battery</span><span>${batteryPct}%</span></div><div class="bar"><span class="progress-fill battery-fill" style="width:${batteryPct}%"></span></div></div><div><div class="bar-label"><span>${escapeHtml(worker.activeTask ? worker.activeTask.task_type : "No active task")}</span><span>${taskPct === null ? "-" : `${(taskPct * 100).toFixed(1)}%`}</span></div><div class="bar"><span class="progress-fill work-fill" style="width:${taskPct === null ? 0 : (taskPct * 100).toFixed(1)}%"></span></div></div><div class="worker-meta"><span class="tag">Carry ${escapeHtml(shortItemLabel(worker.carrying || "unknown"))}</span><span class="tag">Target ${escapeHtml(worker.activeTask ? String(worker.activeTask.target || "-") : "-")}</span><span class="tag">Last swap ${fmtMinutes(worker.lastBatterySwap || 0)}</span></div></article>`);
      }
      elements.workerLayer.innerHTML = chips.join("");
      elements.workerGrid.innerHTML = cards.join("");
    }
    function renderFloatingLabels(state) {
      const labels = state.floatLabels.filter((label) => label.age <= FLOAT_TTL_MIN).map((label, index) => {
        const center = zoneCenter(label.zone || "Warehouse"), className = label.text.toLowerCase();
        return `<div class="float-label ${escapeHtml(className)}" style="left:${center.x + (index % 2 === 0 ? -18 : 18)}px;top:${center.y - 18 - ((index % 3) * 16)}px">${escapeHtml(label.text)}</div>`;
      });
      elements.floatingLayer.innerHTML = labels.join("");
    }
    function renderRecentEvents(state) {
      elements.eventsList.innerHTML = state.recentEvents.map((event) => {
        const details = event.details && Object.keys(event.details).length ? JSON.stringify(event.details) : "";
        return `<article class="event-row"><div class="top"><span class="type">${escapeHtml(event.type)}</span><span class="time">t=${Number(event.t || 0).toFixed(1)}m</span></div><div class="sub">Day ${escapeHtml(String(event.day || 0))} / ${escapeHtml(String(event.entity_id || "-"))} / ${escapeHtml(String(event.location || "-"))}</div><div class="details">${escapeHtml(details)}</div></article>`;
      }).join("");
    }
    function drawRoutes() {
      syncCanvasLayout();
      elements.factorySvg.innerHTML = "";
    }
    function updateHeader(state) {
      const meta = payload.meta, day = dayAtTime(currentTime, meta.minutes_per_day);
      elements.timeReadout.textContent = `t=${currentTime.toFixed(1)}m / Day ${day} / ${fmtSimClock(currentTime, meta.minutes_per_day)}`;
      elements.subtitle.textContent = `${meta.decision_mode || "unknown"} / ${meta.model || "model n/a"} / ${meta.total_days} days / ${meta.minutes_per_day} minutes per day`;
      elements.runPill.textContent = `${meta.run_id} / ${meta.wall_clock_human || "wall n/a"}`;
      elements.summaryStrip.innerHTML = "";
    }
    function renderAll() { if (!payload) return; const state = buildStateAt(currentTime); updateHeader(state); renderZones(state); renderQueues(state); renderMachines(state); renderWorkers(state); renderRecentEvents(state); renderFloatingLabels(state); }
    function syncPlayButton() { elements.playBtn.textContent = playing ? "Pause" : "Play"; }
    function tick(ts) { if (!playing || !payload) return; if (lastFrameTs === null) lastFrameTs = ts; const deltaSec = (ts - lastFrameTs) / 1000; lastFrameTs = ts; currentTime = clamp(currentTime + (deltaSec * BASE_MINUTES_PER_SECOND * speed), 0, Number(payload.meta.max_time || 0)); elements.timeSlider.value = String(currentTime); renderAll(); if (currentTime >= Number(payload.meta.max_time || 0)) { playing = false; lastFrameTs = null; syncPlayButton(); return; } window.requestAnimationFrame(tick); }
    function setError(message) { elements.errorBanner.innerHTML = message ? `<div class="error-banner">${escapeHtml(message)}</div>` : ""; }
    function bindControls() {
      elements.playBtn.addEventListener("click", () => {
        if (!payload) return;
        playing = !playing;
        lastFrameTs = null;
        syncPlayButton();
        if (playing) window.requestAnimationFrame(tick);
      });
      elements.resetBtn.addEventListener("click", () => { playing = false; lastFrameTs = null; currentTime = 0; elements.timeSlider.value = "0"; syncPlayButton(); renderAll(); });
      elements.speedSelect.addEventListener("change", (event) => { speed = Number(event.target.value || 1); });
      elements.timeSlider.addEventListener("input", (event) => { playing = false; lastFrameTs = null; currentTime = Number(event.target.value || 0); syncPlayButton(); renderAll(); });
    }
    async function init() {
      bindControls();
      try {
        const response = await fetch(PAYLOAD_URL, { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        payload = await response.json();
      } catch (error) {
        setError(`Failed to load operations_replay.json. This standalone uses sibling JSON fetch and may be blocked by browser file:// policy. (${String(error)})`);
        return;
      }
      setError("");
      drawRoutes();
      currentTime = 0;
      elements.timeSlider.max = String(payload.meta.max_time || 0);
      elements.timeSlider.value = "0";
      syncPlayButton();
      renderAll();
      window.addEventListener("resize", () => { if (!payload) return; syncCanvasLayout(); drawRoutes(); renderAll(); });
    }
    init();
  </script>
</body>
</html>
"""


def export_operations_replay(*, output_dir: Path, events: list[dict[str, Any]]) -> Path:
    output_dir = Path(output_dir)
    payload = _build_payload(output_dir, events)
    json_path = output_dir / "operations_replay.json"
    html_path = output_dir / "operations_replay.html"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    html_path.write_text(HTML_TEMPLATE, encoding="utf-8")
    return html_path
