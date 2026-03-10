from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from manufacturing_sim.simulation.scenarios.manufacturing.decision.modes import (
    format_decision_mode_label,
    normalize_decision_mode,
)

ZONE_LAYOUT: dict[str, dict[str, float]] = {
    "Station1": {"x0": 4.2, "x1": 5.9, "y0": 1.05, "y1": 2.25},
    "Station2": {"x0": 6.1, "x1": 7.8, "y0": 1.05, "y1": 2.25},
    "Inspection": {"x0": 8.0, "x1": 9.7, "y0": 1.05, "y1": 2.25},
    # Repositioned for 2-station flow (Station1 -> Station2 -> Inspection)
    "Warehouse": {"x0": 6.1, "x1": 7.8, "y0": 2.45, "y1": 3.25},
    "BatteryStation": {"x0": 6.1, "x1": 7.8, "y0": 0.15, "y1": 0.95},
}

ROUTE_EDGES: list[tuple[str, str]] = [
    # Station-to-station routes
    ("Station1", "Station2"),
    ("Station1", "Inspection"),
    ("Station2", "Inspection"),
    # Warehouse supply routes
    ("Warehouse", "Station1"),
    ("Warehouse", "Station2"),
    ("Warehouse", "Inspection"),
    # Battery logistics routes
    ("BatteryStation", "Warehouse"),
    ("BatteryStation", "Station1"),
    ("BatteryStation", "Station2"),
    ("BatteryStation", "Inspection"),
]

AGENT_STATUS_COLOR = {
    "IDLE": "#95a5a6",
    "MOVING": "#f5b041",
    "WORKING": "#27ae60",
    "DISCHARGED": "#e74c3c",
}
AGENT_MARKER_SYMBOL = "circle"
MACHINE_MARKER_SYMBOL = "square"

MACHINE_STATUS_COLOR = {
    "IDLE": "#95a5a6",
    "RUNNING": "#27ae60",
    "DOWN": "#e74c3c",
    "FINISHED_WAIT_UNLOAD": "#f39c12",
}

CARGO_COLOR = {
    "material": "#8e44ad",
    "component": "#16a085",
    "product": "#e67e22",
    "battery": "#f1c40f",
    "battery_fresh": "#f1c40f",
    "battery_spent": "#7f8c8d",
}

CARGO_MARKER_SYMBOL = {
    "material": "diamond",
    "component": "diamond",
    "product": "diamond",
    "battery": "triangle-up",
    "battery_fresh": "triangle-up",
    "battery_spent": "triangle-down",
}

TASK_DEFAULT_DURATION_MIN = {
    "SETUP_MACHINE": 8.0,
    "UNLOAD_MACHINE": 2.5,
    "TRANSFER": 10.0,
    "INSPECT_PRODUCT": 8.0,
    "REPAIR_MACHINE": 120.0,
    "PREVENTIVE_MAINTENANCE": 30.0,
    "BATTERY_SWAP": 9.0,
}

def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "manufacturing_sim").exists():
            return parent
    return here.parents[5]


def _latest_events_path(root: Path) -> Path | None:
    candidates = list(root.glob("outputs/*/*/events.jsonl"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@st.cache_data(show_spinner=False)
def _load_battery_period_min(events_path_str: str, default: float = 180.0) -> float:
    events_path = Path(events_path_str)
    hydra_cfg_path = events_path.parent / ".hydra" / "config.yaml"
    try:
        text = hydra_cfg_path.read_text(encoding="utf-8")
    except OSError:
        return float(default)
    m = re.search(r"(?m)^\s*battery_swap_period_min\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$", text)
    if not m:
        return float(default)
    try:
        value = float(m.group(1))
    except ValueError:
        return float(default)
    return value if value > 0 else float(default)


@st.cache_data(show_spinner=False)
def _load_run_meta(events_path_str: str) -> dict[str, Any]:
    out = {"mode": "unknown", "model": "", "server_url": "", "communication_enabled": None}
    events_path = Path(events_path_str)

    run_meta_path = events_path.parent / "run_meta.json"
    if run_meta_path.exists():
        try:
            run_meta_obj = json.loads(run_meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            run_meta_obj = {}
        if isinstance(run_meta_obj, dict):
            mode = normalize_decision_mode(str(run_meta_obj.get("decision_mode", "")))
            if mode:
                out["mode"] = mode
            llm = run_meta_obj.get("llm", {}) if isinstance(run_meta_obj.get("llm", {}), dict) else {}
            out["model"] = str(llm.get("model", "")).strip()
            out["server_url"] = str(llm.get("server_url", "")).strip()
            if "communication_enabled" in llm:
                out["communication_enabled"] = bool(llm.get("communication_enabled"))
            if out["mode"] != "unknown":
                return out

    hydra_cfg_path = events_path.parent / ".hydra" / "config.yaml"
    try:
        text = hydra_cfg_path.read_text(encoding="utf-8")
    except OSError:
        return out

    m_mode = re.search(r"(?m)^\s{2}mode\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", text)
    if m_mode:
        out["mode"] = str(m_mode.group(1)).strip().lower()
    m_model = re.search(r'(?m)^\s{4}model\s*:\s*"?([^"\n]+)"?\s*$', text)
    if m_model:
        out["model"] = str(m_model.group(1)).strip()
    m_server = re.search(r'(?m)^\s{4}server_url\s*:\s*"?([^"\n]+)"?\s*$', text)
    if m_server:
        out["server_url"] = str(m_server.group(1)).strip()
    m_comm = re.search(r"(?m)^\s{6}enabled\s*:\s*(true|false)\s*$", text)
    if m_comm:
        out["communication_enabled"] = str(m_comm.group(1)).strip().lower() == "true"
    return out


@st.cache_data(show_spinner=False)
def _load_events(path_str: str) -> pd.DataFrame:
    path = Path(path_str)
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for seq, line in enumerate(fp):
            row = json.loads(line)
            row["_seq"] = seq
            rows.append(row)
    if not rows:
        return pd.DataFrame(columns=["t", "day", "type", "entity_id", "location", "details"])

    df = pd.DataFrame(rows)
    df["t"] = df["t"].astype(float)
    df["day"] = df["day"].astype(int)
    df["details"] = df["details"].apply(lambda x: x if isinstance(x, dict) else {})
    # Keep stable in-file order for same-timestamp events to avoid state glitches.
    df = df.sort_values(["t", "_seq"], kind="mergesort").reset_index(drop=True)
    return df


@st.cache_data(show_spinner=False)
def _load_optional_json(path_str: str) -> dict[str, Any]:
    path = Path(path_str)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def _station_from_machine(machine_id: str) -> int | None:
    m = re.match(r"S(\d+)M\d+", machine_id)
    if not m:
        return None
    return int(m.group(1))


def _build_task_duration_reference(events_df: pd.DataFrame) -> dict[str, float]:
    starts: dict[tuple[str, str], tuple[float, str]] = {}
    durations: dict[str, list[float]] = defaultdict(list)

    for row in events_df.itertuples(index=False):
        et = row.type
        entity = row.entity_id
        details = row.details
        task_id = str(details.get("task_id", ""))
        task_type = str(details.get("task_type", ""))
        if et == "AGENT_TASK_START" and task_id:
            starts[(entity, task_id)] = (float(row.t), task_type)
        elif et == "AGENT_TASK_END" and task_id:
            key = (entity, task_id)
            if key in starts:
                start_t, start_task_type = starts.pop(key)
                duration = max(0.0, float(row.t) - start_t)
                durations[start_task_type or task_type].append(duration)

    reference: dict[str, float] = dict(TASK_DEFAULT_DURATION_MIN)
    for task_type, values in durations.items():
        if values:
            reference[task_type] = float(pd.Series(values).median())
    return reference


def _task_target(task_type: str, payload: dict[str, Any]) -> str:
    if "machine_id" in payload:
        return str(payload["machine_id"])
    if task_type == "BATTERY_SWAP":
        target_id = str(payload.get("target_agent_id", ""))
        if target_id:
            return f"{target_id}->{target_id}"
        return "-"
    if task_type == "TRANSFER":
        transfer_kind = str(payload.get("transfer_kind", "")).lower()
        if transfer_kind == "battery_delivery":
            target_agent_id = str(payload.get("target_agent_id", ""))
            return f"Battery->" + (target_agent_id if target_agent_id else "Agent")
        if transfer_kind == "material_supply":
            station = int(payload.get("station", 0))
            return f"Warehouse->Station{station}"
        from_station = int(payload.get("from_station", 0))
        if from_station == 4:
            return "Inspection->Warehouse"
        if from_station >= 2:
            return f"Station{from_station}->Inspection"
        to_station = from_station + 1
        return f"Station{from_station}->Station{to_station}"
    if "station" in payload:
        station = int(payload["station"])
        return "Inspection" if station == 4 else f"Station{station}"
    if "target_agent_id" in payload:
        return str(payload["target_agent_id"])
    return "-"


def _task_carrying(task_type: str, payload: dict[str, Any]) -> str:
    if task_type == "TRANSFER":
        transfer_kind = str(payload.get("transfer_kind", "")).lower()
        if transfer_kind == "battery_delivery":
            return "battery_fresh"
        if transfer_kind == "material_supply":
            return "material"
        from_station = int(payload.get("from_station", 0))
        return "product" if from_station in {2, 4} else "component"
    if task_type == "UNLOAD_MACHINE":
        station = int(payload.get("station", 0) or (_station_from_machine(str(payload.get("machine_id", ""))) or 0))
        return "product" if station == 2 else "component"
    if task_type == "INSPECT_PRODUCT":
        # Queue 4 is product queue (final item before pass/fail)
        return "product"
    if task_type == "SETUP_MACHINE":
        # Setup now enforces one-item carry at a time.
        has_material = bool(payload.get("material_id"))
        has_component = bool(payload.get("component_id"))
        if has_component and not has_material:
            return "component"
        return "material"
    return "-"


def _target_zone(task_type: str, payload: dict[str, Any], fallback_zone: str) -> str:
    if "machine_id" in payload:
        station = _station_from_machine(str(payload["machine_id"]))
        return fallback_zone if station is None else f"Station{station}"
    if "station" in payload:
        station = int(payload["station"])
        if station == 4:
            return "Inspection"
        return f"Station{station}"
    if task_type == "TRANSFER":
        transfer_kind = str(payload.get("transfer_kind", "")).lower()
        if transfer_kind == "battery_delivery":
            return "BatteryStation"
        if transfer_kind == "material_supply":
            station = int(payload.get("station", 1))
            return f"Station{station}"
        from_station = int(payload.get("from_station", 1))
        if from_station == 4:
            return "Warehouse"
        if from_station >= 2:
            return "Inspection"
        to_station = from_station + 1
        return f"Station{to_station}"
    if task_type == "BATTERY_SWAP":
        return "BatteryStation"
    if task_type == "INSPECT_PRODUCT":
        return "Inspection"
    if fallback_zone in ZONE_LAYOUT:
        return fallback_zone
    return "Warehouse"


def _canonical_zone(zone: str) -> str:
    if zone in ZONE_LAYOUT:
        return zone
    if zone in {"Home", "TownHall"}:
        return "Warehouse"
    return "Warehouse"


def _compute_machine_states(events_df: pd.DataFrame, current_t: float) -> dict[str, str]:
    filtered = events_df[events_df["t"] <= current_t]

    machine_ids: set[str] = set()
    for row in events_df.itertuples(index=False):
        entity = str(row.entity_id)
        details = row.details if isinstance(row.details, dict) else {}
        if entity.startswith("S") and "M" in entity:
            machine_ids.add(entity)
        payload = details.get("payload", {})
        if isinstance(payload, dict):
            machine_id = payload.get("machine_id")
            if isinstance(machine_id, str) and machine_id.startswith("S") and "M" in machine_id:
                machine_ids.add(machine_id)

    machine_states: dict[str, str] = {mid: "IDLE" for mid in machine_ids}

    for row in filtered.itertuples(index=False):
        et = row.type
        entity = str(row.entity_id)
        details = row.details

        if entity.startswith("S") and "M" in entity:
            machine_states.setdefault(entity, "IDLE")
            if et == "MACHINE_START":
                machine_states[entity] = "RUNNING"
            elif et == "MACHINE_END":
                machine_states[entity] = "FINISHED_WAIT_UNLOAD"
            elif et == "MACHINE_BROKEN":
                machine_states[entity] = "DOWN"
            elif et == "MACHINE_REPAIRED":
                machine_states[entity] = "IDLE"
            elif et == "MACHINE_PM_START":
                machine_states[entity] = "DOWN"
            elif et == "MACHINE_PM_END":
                # PM end should not clear a real breakdown state.
                if machine_states.get(entity) != "DOWN":
                    machine_states[entity] = "IDLE"
            elif et == "MACHINE_ABORTED":
                machine_states[entity] = "DOWN"

        if et == "AGENT_TASK_END" and details.get("task_type") == "UNLOAD_MACHINE" and details.get("status") == "completed":
            machine_id = str(details.get("payload", {}).get("machine_id", ""))
            if machine_id:
                # Successful unload releases finished-wait-unload -> idle, but not down.
                if machine_states.get(machine_id) != "DOWN":
                    machine_states[machine_id] = "IDLE"

        if et in {"AGENT_TASK_START", "AGENT_TASK_END"}:
            machine_id = str(details.get("payload", {}).get("machine_id", ""))
            if machine_id:
                machine_states.setdefault(machine_id, "IDLE")

    return machine_states


def _compute_machine_inputs(events_df: pd.DataFrame, current_t: float) -> dict[str, dict[str, Any]]:
    filtered = events_df[events_df["t"] <= current_t]

    machine_ids: set[str] = set()
    for row in events_df.itertuples(index=False):
        entity = str(row.entity_id)
        details = row.details if isinstance(row.details, dict) else {}
        if entity.startswith("S") and "M" in entity:
            machine_ids.add(entity)
        payload = details.get("payload", {})
        if isinstance(payload, dict):
            machine_id = str(payload.get("machine_id", ""))
            if machine_id.startswith("S") and "M" in machine_id:
                machine_ids.add(machine_id)
        drop_to = str(details.get("to", ""))
        if drop_to.startswith("S") and "M" in drop_to:
            machine_ids.add(drop_to)

    slots: dict[str, dict[str, Any]] = {
        mid: {"material": False, "component": False, "output": ""}
        for mid in machine_ids
    }

    def _has_val(v: Any) -> bool:
        s = str(v).strip()
        return s not in {"", "None", "null"}

    for row in filtered.itertuples(index=False):
        et = str(row.type)
        details = row.details if isinstance(row.details, dict) else {}

        if et == "AGENT_DROP_ITEM":
            machine_id = str(details.get("to", ""))
            item_type = str(details.get("item_type", "")).strip().lower()
            if machine_id in slots:
                if item_type == "material":
                    slots[machine_id]["material"] = True
                    slots[machine_id]["output"] = ""
                elif item_type == "component":
                    slots[machine_id]["component"] = True
                    slots[machine_id]["output"] = ""

        elif et == "MACHINE_START":
            machine_id = str(row.entity_id)
            if machine_id in slots:
                slots[machine_id]["material"] = _has_val(details.get("input_material"))
                slots[machine_id]["component"] = _has_val(details.get("input_component"))
                slots[machine_id]["output"] = ""

        elif et == "MACHINE_END":
            machine_id = str(row.entity_id)
            if machine_id in slots:
                slots[machine_id]["material"] = False
                slots[machine_id]["component"] = False
                output_id = str(details.get("output_component", "")).strip()
                if output_id.upper().startswith("PRODUCT"):
                    slots[machine_id]["output"] = "product"
                elif output_id:
                    slots[machine_id]["output"] = "component"
                else:
                    slots[machine_id]["output"] = "product"

        elif et == "ITEM_MOVED":
            source = str(details.get("from", "")).strip()
            if source in slots:
                slots[source]["output"] = ""

        elif et == "MACHINE_ABORTED":
            machine_id = str(row.entity_id)
            if machine_id in slots:
                slots[machine_id]["material"] = False
                slots[machine_id]["component"] = False
                slots[machine_id]["output"] = ""

    return slots


def _machine_status_label(status: str) -> str:
    if status == "IDLE":
        return "Idle"
    if status == "RUNNING":
        return "Running"
    if status == "DOWN":
        return "Down"
    if status == "FINISHED_WAIT_UNLOAD":
        return "Finished-wait-unload"
    return status


def _agent_status_label(status: str) -> str:
    return str(status).strip().lower().capitalize()


def _cargo_symbol(item_kind: str) -> str:
    return CARGO_MARKER_SYMBOL.get(item_kind, "diamond")


def _edge_zone_label(from_zone: str, to_zone: str, progress: float) -> str:
    p = int(min(100, max(0, round(float(progress) * 100))))
    return f"In-Transit({from_zone}->{to_zone}) {p}%"


def _simulation_datetime(events_df: pd.DataFrame, current_t: float) -> tuple[int, str]:
    filtered = events_df[events_df["t"] <= current_t]
    if filtered.empty:
        return 1, "2026-01-01 00:00"

    current_day = int(filtered["day"].max())
    day_starts = events_df.groupby("day")["t"].min().to_dict()
    day_start_t = float(day_starts.get(current_day, 0.0))
    minute_of_day = max(0.0, current_t - day_start_t)
    minute_of_day_int = int(minute_of_day)
    hour = minute_of_day_int // 60
    minute = minute_of_day_int % 60
    base_date = datetime(2026, 1, 1) + timedelta(days=current_day - 1)
    return current_day, f"{base_date.strftime('%Y-%m-%d')} {hour:02d}:{minute:02d}"


def _compute_queue_snapshot(events_df: pd.DataFrame, current_t: float) -> dict[str, dict[int, list[str]]]:
    filtered = events_df[events_df["t"] <= current_t]
    material: dict[int, list[str]] = {1: [], 2: []}
    # Station1 is material-only; component/product queues start from Station2.
    component: dict[int, list[str]] = {2: [], 4: []}
    output_buffer: dict[int, list[str]] = {1: [], 2: [], 4: []}
    warehouse_completed: list[str] = []
    active_transfer_from_station: dict[str, int] = {}

    for row in filtered.itertuples(index=False):
        et = str(row.type)
        details = row.details if isinstance(row.details, dict) else {}

        if et == "AGENT_TASK_START":
            agent_id = str(row.entity_id)
            task_type = str(details.get("task_type", ""))
            payload = details.get("payload", {}) if isinstance(details.get("payload", {}), dict) else {}
            if task_type == "TRANSFER" and str(payload.get("transfer_kind", "")).lower() == "inter_station":
                from_station = int(payload.get("from_station", 0) or 0)
                if from_station in output_buffer:
                    active_transfer_from_station[agent_id] = from_station
            continue

        if et == "AGENT_TASK_END":
            agent_id = str(row.entity_id)
            active_transfer_from_station.pop(agent_id, None)
            continue

        if et == "AGENT_PICK_ITEM":
            agent_id = str(row.entity_id)
            item_id = str(details.get("item_id", ""))
            from_station = active_transfer_from_station.get(agent_id)
            if from_station in output_buffer and item_id:
                items = output_buffer[from_station]
                if item_id in items:
                    items.remove(item_id)
            continue

        if et == "ITEM_MOVED":
            item_id = str(row.entity_id)
            from_loc = str(details.get("from", ""))
            to_loc = str(details.get("to", ""))
            item_type = str(details.get("item_type", "")).strip().lower()
            m_to = re.match(r"output_buffer_station_(\d+)", to_loc)
            m_from = re.match(r"output_buffer_station_(\d+)", from_loc)
            if m_to:
                st = int(m_to.group(1))
                if st in output_buffer:
                    output_buffer[st].append(item_id)
            if m_from:
                st = int(m_from.group(1))
                if st in output_buffer:
                    items = output_buffer[st]
                    if item_id in items:
                        items.remove(item_id)
            is_product = item_type == "product" or item_id.upper().startswith("PRODUCT")
            if is_product and to_loc == "Warehouse":
                warehouse_completed.append(item_id)
            if is_product and from_loc == "Warehouse":
                if item_id in warehouse_completed:
                    warehouse_completed.remove(item_id)
                elif warehouse_completed:
                    warehouse_completed.pop(0)
            continue

        if et not in {"QUEUE_PUSH", "QUEUE_POP"}:
            continue
        entity = str(row.entity_id)
        queue_kind = str(details.get("queue", ""))
        item_id = str(details.get("item_id", ""))
        m = re.match(r"(material|component)_queue_(\d+)", entity)
        if not m:
            continue
        station = int(m.group(2))

        target = material if queue_kind == "material" else component
        if station not in target:
            continue

        items = target[station]
        if et == "QUEUE_PUSH":
            items.append(item_id)
        else:
            if item_id in items:
                items.remove(item_id)
            elif items:
                items.pop(0)

    return {
        "material": material,
        "component": component,
        "output_buffer": output_buffer,
        "warehouse_completed": warehouse_completed,
    }


def _format_inventory(items: list[str]) -> str:
    return str(len(items))


def _compute_agent_states(
    events_df: pd.DataFrame,
    current_t: float,
    battery_period_min: float,
) -> pd.DataFrame:
    transfer_task_types = {"TRANSFER"}

    filtered = events_df[events_df["t"] <= current_t]
    duration_ref = _build_task_duration_reference(events_df)

    agent_ids = sorted(
        {
            str(eid)
            for eid in events_df["entity_id"].unique().tolist()
            if str(eid).startswith("A")
        }
    )
    if not agent_ids:
        return pd.DataFrame(
            columns=[
                "agent_id",
                "zone",
                "location_mode",
                "task",
                "target",
                "battery",
                "carrying",
                "status",
                "down_reason",
                "eta",
                "from_zone",
                "to_zone",
                "move_progress",
                "zone_enter_t",
            ]
        )

    states: dict[str, dict[str, Any]] = {
        aid: {
            "agent_id": aid,
            "zone": "Warehouse",
            "location_mode": "zone",
            "task": "-",
            "target": "-",
            "carrying": "-",
            "status": "IDLE",
            "down_reason": "",
            "eta": 0.0,
            "battery": battery_period_min,
            "from_zone": "Warehouse",
            "to_zone": "Warehouse",
            "move_progress": 0.0,
            "zone_enter_t": 0.0,
        }
        for aid in agent_ids
    }
    active_tasks: dict[str, dict[str, Any]] = {}
    active_moves: dict[str, dict[str, Any]] = {}
    # Keep frozen in-transit snapshots for move interruptions so replay can
    # render "stopped mid-route" without snapping to a zone.
    paused_in_transit: dict[str, dict[str, Any]] = {}
    discharged_agents: dict[str, str] = {}
    battery_wait_from: dict[str, str] = {}
    last_swap: dict[str, float] = {aid: 0.0 for aid in agent_ids}
    carrying_now: dict[str, str] = {aid: "-" for aid in agent_ids}

    for row in filtered.itertuples(index=False):
        et = row.type
        aid = str(row.entity_id)
        details = row.details
        location = str(row.location) if row.location else ""

        if aid in states and location:
            new_zone = _canonical_zone(location)
            prev_zone = _canonical_zone(str(states[aid]["zone"]))
            states[aid]["zone"] = new_zone
            states[aid]["location_mode"] = "zone"
            if prev_zone != new_zone:
                states[aid]["zone_enter_t"] = float(row.t)

        if et == "AGENT_TASK_START" and aid in states:
            task_type = str(details.get("task_type", "-"))
            payload = details.get("payload", {}) if isinstance(details.get("payload", {}), dict) else {}
            paused_in_transit.pop(aid, None)
            active_tasks[aid] = {
                "task_id": str(details.get("task_id", "")),
                "task_type": task_type,
                "payload": payload,
                "start_t": float(row.t),
                "start_zone": states[aid]["zone"],
            }
        elif et == "AGENT_MOVE_INTERRUPTED" and aid in states:
            move = active_moves.pop(aid, None)
            from_zone = _canonical_zone(
                str(details.get("from", move.get("from_zone", states[aid]["zone"]) if isinstance(move, dict) else states[aid]["zone"]))
            )
            to_zone = _canonical_zone(
                str(details.get("to", move.get("to_zone", from_zone) if isinstance(move, dict) else from_zone))
            )
            progress = float(details.get("progress", 0.0))
            if not (0.0 <= progress <= 1.0) and isinstance(move, dict):
                move_start_t = float(move.get("start_t", row.t))
                move_end_t = float(move.get("end_t", move_start_t))
                move_duration = max(1e-6, move_end_t - move_start_t)
                progress = min(1.0, max(0.0, (float(row.t) - move_start_t) / move_duration))
            progress = min(1.0, max(0.0, progress))
            paused_in_transit[aid] = {
                "from_zone": from_zone,
                "to_zone": to_zone,
                "progress": round(progress, 4),
            }
            states[aid]["zone"] = _edge_zone_label(from_zone, to_zone, progress)
            states[aid]["location_mode"] = "edge"
            states[aid]["from_zone"] = from_zone
            states[aid]["to_zone"] = to_zone
            states[aid]["move_progress"] = round(progress, 4)
        elif et == "AGENT_TASK_END" and aid in states:
            task_id = str(details.get("task_id", ""))
            end_status = str(details.get("status", ""))
            end_reason = str(details.get("reason", ""))
            active = active_tasks.get(aid)
            if active and active["task_id"] == task_id:
                active_tasks.pop(aid, None)
            move = active_moves.pop(aid, None)
            preserve_carrying = end_status == "interrupted" and end_reason in {"battery_swap_wait", "battery_depleted"}
            if not preserve_carrying:
                # Defensive reset: completed/failed task end should not leak carrying.
                carrying_now[aid] = "-"
            if (
                move is not None
                and end_status == "interrupted"
                and end_reason in {"battery_swap_wait", "battery_depleted"}
            ):
                move_start_t = float(move.get("start_t", row.t))
                move_end_t = float(move.get("end_t", move_start_t))
                move_duration = max(1e-6, move_end_t - move_start_t)
                progress = min(1.0, max(0.0, (float(row.t) - move_start_t) / move_duration))
                from_zone = _canonical_zone(str(move.get("from_zone", states[aid]["zone"])))
                to_zone = _canonical_zone(str(move.get("to_zone", from_zone)))
                paused_in_transit[aid] = {
                    "from_zone": from_zone,
                    "to_zone": to_zone,
                    "progress": round(progress, 4),
                }
                states[aid]["zone"] = _edge_zone_label(from_zone, to_zone, progress)
                states[aid]["location_mode"] = "edge"
                states[aid]["from_zone"] = from_zone
                states[aid]["to_zone"] = to_zone
                states[aid]["move_progress"] = round(progress, 4)
            elif location:
                states[aid]["zone"] = _canonical_zone(location)
                states[aid]["location_mode"] = "zone"
        elif et == "AGENT_MOVE_START" and aid in states:
            move_from = _canonical_zone(str(details.get("from", states[aid]["zone"])))
            move_to = _canonical_zone(str(details.get("to", move_from)))
            duration = max(0.0, float(details.get("duration", 0.0)))
            start_t = float(row.t)
            paused_in_transit.pop(aid, None)
            active_moves[aid] = {
                "from_zone": move_from,
                "to_zone": move_to,
                "start_t": start_t,
                "end_t": start_t + duration,
            }
            states[aid]["zone"] = move_from
            states[aid]["location_mode"] = "edge"
        elif et == "AGENT_MOVE_END" and aid in states:
            move_to = _canonical_zone(str(details.get("to", states[aid]["zone"])))
            active_moves.pop(aid, None)
            paused_in_transit.pop(aid, None)
            if _canonical_zone(str(states[aid]["zone"])) != move_to:
                states[aid]["zone_enter_t"] = float(row.t)
            states[aid]["zone"] = move_to
            states[aid]["location_mode"] = "zone"
        elif et == "AGENT_RELOCATED" and aid in states:
            move_to = _canonical_zone(str(details.get("to", states[aid]["zone"])))
            paused_in_transit.pop(aid, None)
            if _canonical_zone(str(states[aid]["zone"])) != move_to:
                states[aid]["zone_enter_t"] = float(row.t)
            states[aid]["zone"] = move_to
            states[aid]["location_mode"] = "zone"
        elif et in {"AGENT_DISCHARGED", "AGENT_FAILED"} and aid in states:
            discharged_agents[aid] = str(details.get("reason", "battery_depleted"))
            active_moves.pop(aid, None)
            if aid not in paused_in_transit and location:
                states[aid]["zone"] = _canonical_zone(location)
                states[aid]["location_mode"] = "zone"
        elif et in {"AGENT_RECHARGED", "AGENT_REPAIRED"}:
            repaired_agent = aid if aid in states else str(details.get("target_agent_id", ""))
            if repaired_agent in states:
                discharged_agents.pop(repaired_agent, None)
                states[repaired_agent]["down_reason"] = ""
                paused_in_transit.pop(repaired_agent, None)
        elif et == "BATTERY_SWAP":
            target = str(details.get("target_agent_id", aid))
            if target in states:
                last_swap[target] = float(row.t)
                # Self swap (or explicit target swap) also clears discharged state.
                discharged_agents.pop(target, None)
                states[target]["down_reason"] = ""
                battery_wait_from.pop(target, None)
        elif et == "BATTERY_DELIVERED":
            target = str(details.get("target_agent_id", ""))
            if target in states:
                battery_wait_from.pop(target, None)
        elif et == "BATTERY_SWAP_WAIT_START" and aid in states:
            helper = str(details.get("from_agent_id", "")).strip()
            battery_wait_from[aid] = helper
        elif et == "BATTERY_SWAP_WAIT_END" and aid in states:
            battery_wait_from.pop(aid, None)
        elif et == "AGENT_PICK_ITEM" and aid in states:
            item_type = str(details.get("item_type", "")).strip().lower()
            carrying_now[aid] = item_type if item_type else "-"
        elif et == "AGENT_DROP_ITEM" and aid in states:
            carrying_now[aid] = "-"

    for aid in agent_ids:
        elapsed_since_swap = max(0.0, current_t - float(last_swap.get(aid, 0.0)))
        battery_remaining = max(0.0, battery_period_min - elapsed_since_swap)
        states[aid]["battery"] = round(battery_remaining, 2)

        if aid in discharged_agents:
            states[aid]["status"] = "DISCHARGED"
            states[aid]["down_reason"] = discharged_agents[aid]
            states[aid]["task"] = "-"
            states[aid]["target"] = "-"
            states[aid]["carrying"] = carrying_now.get(aid, "-")
            states[aid]["eta"] = 0.0
            states[aid]["battery"] = 0.0
            paused = paused_in_transit.get(aid)
            if paused is not None:
                from_zone = _canonical_zone(str(paused.get("from_zone", "Warehouse")))
                to_zone = _canonical_zone(str(paused.get("to_zone", from_zone)))
                progress = float(paused.get("progress", 0.0))
                progress = min(1.0, max(0.0, progress))
                states[aid]["zone"] = _edge_zone_label(from_zone, to_zone, progress)
                states[aid]["location_mode"] = "edge"
                states[aid]["from_zone"] = from_zone
                states[aid]["to_zone"] = to_zone
                states[aid]["move_progress"] = round(progress, 4)
                states[aid]["target"] = f"{from_zone}->{to_zone}"
            else:
                states[aid]["location_mode"] = "zone"
                states[aid]["from_zone"] = states[aid]["zone"]
                states[aid]["to_zone"] = states[aid]["zone"]
                states[aid]["move_progress"] = 0.0
            continue

        if aid in battery_wait_from:
            paused = paused_in_transit.get(aid)
            if paused is not None:
                from_zone = _canonical_zone(str(paused.get("from_zone", "Warehouse")))
                to_zone = _canonical_zone(str(paused.get("to_zone", from_zone)))
                progress = min(1.0, max(0.0, float(paused.get("progress", 0.0))))
                states[aid]["status"] = "IDLE"
                states[aid]["task"] = "-"
                states[aid]["target"] = "-"
                states[aid]["carrying"] = carrying_now.get(aid, "-")
                states[aid]["eta"] = 0.0
                states[aid]["zone"] = _edge_zone_label(from_zone, to_zone, progress)
                states[aid]["location_mode"] = "edge"
                states[aid]["from_zone"] = from_zone
                states[aid]["to_zone"] = to_zone
                states[aid]["move_progress"] = round(progress, 4)
                continue
            helper = battery_wait_from.get(aid, "")
            receiver_zone = _canonical_zone(str(states[aid]["zone"]))
            helper_zone = _canonical_zone(str(states.get(helper, {}).get("zone", ""))) if helper else ""
            helper_move = active_moves.get(helper) if helper else None
            helper_active = active_tasks.get(helper) if helper else None
            helper_payload = helper_active["payload"] if isinstance(helper_active, dict) else {}
            helper_transfer_kind = str(helper_payload.get("transfer_kind", "")).lower() if isinstance(helper_payload, dict) else ""
            helper_target = str(helper_payload.get("target_agent_id", "")) if isinstance(helper_payload, dict) else ""
            handover_in_progress = (
                bool(helper)
                and helper_move is None
                and receiver_zone
                and helper_zone == receiver_zone
                and isinstance(helper_active, dict)
                and str(helper_active.get("task_type", "")) == "TRANSFER"
                and helper_transfer_kind == "battery_delivery"
                and helper_target == aid
            )
            if handover_in_progress:
                states[aid]["status"] = "WORKING"
                states[aid]["task"] = "BATTERY_SWAP"
                states[aid]["target"] = f"{helper}->{aid}" if helper else f"{aid}->{aid}"
            else:
                # Waiting for helper arrival: receiver is paused but not yet swapping.
                states[aid]["status"] = "IDLE"
                states[aid]["task"] = "-"
                states[aid]["target"] = "-"
            states[aid]["carrying"] = carrying_now.get(aid, "-")
            states[aid]["eta"] = 0.0
            states[aid]["location_mode"] = "zone"
            states[aid]["from_zone"] = receiver_zone
            states[aid]["to_zone"] = receiver_zone
            states[aid]["move_progress"] = 1.0
            continue

        active = active_tasks.get(aid)
        move = active_moves.get(aid)
        paused = paused_in_transit.get(aid)
        if not active:
            if move is None and paused is not None:
                from_zone = _canonical_zone(str(paused.get("from_zone", "Warehouse")))
                to_zone = _canonical_zone(str(paused.get("to_zone", from_zone)))
                progress = min(1.0, max(0.0, float(paused.get("progress", 0.0))))
                states[aid]["status"] = "IDLE"
                states[aid]["task"] = "-"
                states[aid]["target"] = "-"
                states[aid]["carrying"] = carrying_now.get(aid, "-")
                states[aid]["eta"] = 0.0
                states[aid]["zone"] = _edge_zone_label(from_zone, to_zone, progress)
                states[aid]["location_mode"] = "edge"
                states[aid]["from_zone"] = from_zone
                states[aid]["to_zone"] = to_zone
                states[aid]["move_progress"] = round(progress, 4)
                continue
            if move is None:
                states[aid]["status"] = "IDLE"
                states[aid]["task"] = "-"
                states[aid]["target"] = "-"
                states[aid]["carrying"] = carrying_now.get(aid, "-")
                states[aid]["eta"] = 0.0
                states[aid]["zone"] = _canonical_zone(str(states[aid]["zone"]))
                states[aid]["location_mode"] = "zone"
                states[aid]["from_zone"] = states[aid]["zone"]
                states[aid]["to_zone"] = states[aid]["zone"]
                states[aid]["move_progress"] = 0.0
                continue
            # Defensive fallback: movement exists without an active task.
            states[aid]["task"] = ""
            states[aid]["carrying"] = carrying_now.get(aid, "-")
            states[aid]["eta"] = round(max(0.0, float(move.get("end_t", current_t)) - current_t), 2)
            states[aid]["status"] = "MOVING"
            states[aid]["zone"] = str(move.get("from_zone", states[aid]["zone"]))
            states[aid]["location_mode"] = "edge"
            states[aid]["from_zone"] = states[aid]["zone"]
            states[aid]["to_zone"] = str(move.get("to_zone", states[aid]["zone"]))
            states[aid]["target"] = f"{states[aid]['from_zone']}->{states[aid]['to_zone']}"
            move_start_t = float(move.get("start_t", current_t))
            move_end_t = float(move.get("end_t", move_start_t))
            move_duration = max(1e-6, move_end_t - move_start_t)
            states[aid]["move_progress"] = round(min(1.0, max(0.0, (current_t - move_start_t) / move_duration)), 4)
            continue

        task_type = active["task_type"]
        payload = active["payload"]
        start_t = float(active["start_t"])
        est_duration = float(duration_ref.get(task_type, 10.0))
        elapsed = max(0.0, current_t - start_t)
        eta_task = max(0.0, est_duration - elapsed)

        states[aid]["task"] = task_type
        states[aid]["target"] = _task_target(task_type, payload)
        states[aid]["carrying"] = carrying_now.get(aid, "-")
        transfer_kind = str(payload.get("transfer_kind", "")).lower() if isinstance(payload, dict) else ""
        if (
            task_type == "TRANSFER"
            and transfer_kind == "battery_delivery"
            and states[aid]["carrying"] == "-"
            and bool(payload.get("battery_loaded", False))
        ):
            # Replay safety: show loaded fresh battery even if PICK event is missing.
            states[aid]["carrying"] = "battery_fresh"
        states[aid]["eta"] = round(eta_task, 2)

        source_zone = _canonical_zone(str(active.get("start_zone", states[aid]["zone"])))
        target_zone = _target_zone(task_type, payload, source_zone)
        if move is not None:
            move_start_t = float(move.get("start_t", current_t))
            move_end_t = float(move.get("end_t", move_start_t))
            move_duration = max(1e-6, move_end_t - move_start_t)
            move_progress = min(1.0, max(0.0, (current_t - move_start_t) / move_duration))
            move_from_zone = _canonical_zone(str(move.get("from_zone", source_zone)))
            move_to_zone = _canonical_zone(str(move.get("to_zone", target_zone)))
            eta_move = max(0.0, move_end_t - current_t)
            states[aid]["status"] = "MOVING"
            states[aid]["zone"] = move_from_zone
            states[aid]["location_mode"] = "edge"
            states[aid]["from_zone"] = move_from_zone
            states[aid]["to_zone"] = move_to_zone
            states[aid]["move_progress"] = round(move_progress, 4)
            states[aid]["eta"] = round(max(eta_task, eta_move), 2)
            # When moving, target is always FROM -> TO.
            states[aid]["target"] = f"{move_from_zone}->{move_to_zone}"
            # UI rule: MOVING is shown with TRANSFER only; otherwise leave task blank.
            if task_type in transfer_task_types:
                states[aid]["task"] = "TRANSFER"
            else:
                states[aid]["task"] = ""
                states[aid]["carrying"] = "-"
        else:
            states[aid]["location_mode"] = "zone"
            if task_type in transfer_task_types:
                transfer_kind = str(payload.get("transfer_kind", "")).lower()
                if transfer_kind == "battery_delivery":
                    # Assisted battery delivery (sender side). Show BATTERY_SWAP
                    # only when sender and receiver are co-located for handover.
                    target_agent_id = str(payload.get("target_agent_id", "")).strip()
                    sender_zone = _canonical_zone(str(states[aid]["zone"]))
                    receiver_zone = _canonical_zone(str(states.get(target_agent_id, {}).get("zone", "")))
                    receiver_move = active_moves.get(target_agent_id) if target_agent_id else None
                    handover_in_progress = (
                        bool(target_agent_id)
                        and receiver_zone == sender_zone
                        and receiver_move is None
                    )
                    if handover_in_progress:
                        states[aid]["status"] = "WORKING"
                        states[aid]["task"] = "BATTERY_SWAP"
                        if target_agent_id:
                            states[aid]["target"] = f"{aid}->{target_agent_id}"
                        else:
                            states[aid]["target"] = f"{aid}->{aid}"
                    else:
                        # Before handover starts, do not show BATTERY_SWAP.
                        states[aid]["status"] = "IDLE"
                        states[aid]["task"] = "-"
                        states[aid]["target"] = "-"
                        states[aid]["carrying"] = carrying_now.get(aid, "-")
                    # During handover (no active movement), sender should stay where
                    # the target agent is, not jump to BatteryStation.
                    states[aid]["zone"] = sender_zone
                    states[aid]["from_zone"] = states[aid]["zone"]
                    states[aid]["to_zone"] = states[aid]["zone"]
                    states[aid]["move_progress"] = 1.0
                else:
                    # UI rule: TRANSFER is always represented as MOVING.
                    states[aid]["status"] = "MOVING"
                    states[aid]["zone"] = _canonical_zone(target_zone)
                    states[aid]["from_zone"] = states[aid]["zone"]
                    states[aid]["to_zone"] = states[aid]["zone"]
                    states[aid]["move_progress"] = 1.0
                    states[aid]["target"] = f"{states[aid]['from_zone']}->{states[aid]['to_zone']}"
            else:
                states[aid]["status"] = "WORKING"
                # Non-transfer tasks may hide move animation; show worker at task target zone.
                states[aid]["zone"] = _canonical_zone(target_zone)
                states[aid]["from_zone"] = states[aid]["zone"]
                states[aid]["to_zone"] = states[aid]["zone"]
                states[aid]["move_progress"] = 1.0

    return pd.DataFrame(list(states.values()))


def _draw_factory_map(
    agent_df: pd.DataFrame,
    machine_states: dict[str, str],
    machine_inputs: dict[str, dict[str, bool]],
    queue_snapshot: dict[str, dict[int, list[str]]],
    sim_datetime_label: str,
    completed_products: int,
) -> go.Figure:
    def zone_center(zone_name: str) -> tuple[float, float]:
        z = ZONE_LAYOUT.get(zone_name, ZONE_LAYOUT["Warehouse"])
        return ((z["x0"] + z["x1"]) / 2.0, (z["y0"] + z["y1"]) / 2.0)

    def shortest_zone_path(src_zone: str, dst_zone: str) -> list[str]:
        src = _canonical_zone(src_zone)
        dst = _canonical_zone(dst_zone)
        if src == dst:
            return [src]
        graph: dict[str, list[str]] = defaultdict(list)
        for a, b in ROUTE_EDGES:
            graph[a].append(b)
            graph[b].append(a)
        visited = {src}
        q: deque[tuple[str, list[str]]] = deque([(src, [src])])
        while q:
            node, path = q.popleft()
            for nxt in graph.get(node, []):
                if nxt in visited:
                    continue
                npath = path + [nxt]
                if nxt == dst:
                    return npath
                visited.add(nxt)
                q.append((nxt, npath))
        return [src, dst]

    def interpolate_on_route(src_zone: str, dst_zone: str, progress: float) -> tuple[float, float]:
        path = shortest_zone_path(src_zone, dst_zone)
        points = [zone_center(z) for z in path]
        if len(points) == 1:
            return points[0]
        lengths: list[float] = []
        total = 0.0
        for i in range(len(points) - 1):
            x0, y0 = points[i]
            x1, y1 = points[i + 1]
            seg = max(1e-9, ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5)
            lengths.append(seg)
            total += seg
        target = min(1.0, max(0.0, progress)) * total
        acc = 0.0
        for i, seg in enumerate(lengths):
            if target <= acc + seg:
                local = (target - acc) / seg
                x0, y0 = points[i]
                x1, y1 = points[i + 1]
                return (x0 + (x1 - x0) * local, y0 + (y1 - y0) * local)
            acc += seg
        return points[-1]

    def rounded_rect_path(x0: float, y0: float, x1: float, y1: float, radius: float) -> str:
        r = max(0.0, min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        return (
            f"M {x0 + r},{y0} "
            f"L {x1 - r},{y0} Q {x1},{y0} {x1},{y0 + r} "
            f"L {x1},{y1 - r} Q {x1},{y1} {x1 - r},{y1} "
            f"L {x0 + r},{y1} Q {x0},{y1} {x0},{y1 - r} "
            f"L {x0},{y0 + r} Q {x0},{y0} {x0 + r},{y0} Z"
        )

    fig = go.Figure()
    queue_x: list[float] = []
    queue_y: list[float] = []
    queue_c: list[str] = []
    queue_h: list[str] = []

    def add_queue_markers(
        items: list[str],
        item_kind: str,
        anchor_x: float,
        anchor_y: float,
        queue_label: str,
        right_align: bool = False,
    ) -> None:
        visible_items = items[:5]
        for idx, item_id in enumerate(visible_items):
            x = anchor_x - idx * 0.06 if right_align else anchor_x + idx * 0.06
            queue_x.append(x)
            queue_y.append(anchor_y)
            queue_c.append(CARGO_COLOR.get(item_kind, CARGO_COLOR["component"]))
            queue_h.append(f"{queue_label}<br>item_id={item_id}<br>item_type={item_kind}")
        if len(items) > 5:
            ellipsis_x = anchor_x - 5 * 0.06 if right_align else anchor_x - 0.06
            fig.add_annotation(
                x=ellipsis_x,
                y=anchor_y,
                text="...",
                showarrow=False,
                font=dict(size=10, color="#5d6d7e"),
            )

    # Route interpolation stays enabled, but route lines are hidden on the map.

    for zone_name, z in ZONE_LAYOUT.items():
        zone_fill = "rgba(236,240,241,0.6)"
        if zone_name == "Warehouse":
            zone_fill = "rgba(215,229,245,0.78)"
            fig.add_shape(
                type="path",
                path=rounded_rect_path(z["x0"], z["y0"], z["x1"], z["y1"], radius=0.08),
                line=dict(color="#34495e", width=1),
                fillcolor=zone_fill,
                layer="below",
            )
        elif zone_name == "BatteryStation":
            zone_fill = "rgba(247,236,214,0.82)"
            fig.add_shape(
                type="path",
                path=rounded_rect_path(z["x0"], z["y0"], z["x1"], z["y1"], radius=0.08),
                line=dict(color="#34495e", width=1),
                fillcolor=zone_fill,
                layer="below",
            )
        else:
            fig.add_shape(
                type="rect",
                x0=z["x0"],
                x1=z["x1"],
                y0=z["y0"],
                y1=z["y1"],
                line=dict(color="#34495e", width=1),
                fillcolor=zone_fill,
                layer="below",
            )
        fig.add_annotation(
            x=(z["x0"] + z["x1"]) / 2.0,
            y=(z["y0"] - 0.05) if zone_name == "BatteryStation" else (z["y1"] + 0.12),
            text=zone_name,
            showarrow=False,
            font=dict(size=11, color="#2c3e50"),
        )
        if zone_name == "Warehouse":
            completed_in_warehouse = queue_snapshot.get("warehouse_completed", [])
            fig.add_annotation(
                x=(z["x0"] + z["x1"]) / 2.0,
                y=z["y0"] + 0.08,
                text=f"Completed Products: {_format_inventory(completed_in_warehouse)}",
                showarrow=False,
                font=dict(size=10, color="#34495e"),
            )

        if zone_name.startswith("Station"):
            station = int(zone_name.replace("Station", ""))
            mat_items = queue_snapshot["material"].get(station, [])
            if station == 1:
                queue_text = f"Material Queue: {_format_inventory(mat_items)}"
                add_queue_markers(
                    items=mat_items,
                    item_kind="material",
                    anchor_x=z["x0"] + 0.18,
                    anchor_y=((z["y0"] + z["y1"]) / 2.0),
                    queue_label=f"Station{station} Material Queue",
                    right_align=False,
                )
            else:
                comp_items = queue_snapshot["component"].get(station, [])
                queue_text = (
                    f"Material Queue: {_format_inventory(mat_items)}<br>"
                    f"Component Queue: {_format_inventory(comp_items)}"
                )
                add_queue_markers(
                    items=mat_items,
                    item_kind="material",
                    anchor_x=z["x0"] + 0.18,
                    anchor_y=((z["y0"] + z["y1"]) / 2.0) + 0.06,
                    queue_label=f"Station{station} Material Queue",
                    right_align=False,
                )
                add_queue_markers(
                    items=comp_items,
                    item_kind="component",
                    anchor_x=z["x0"] + 0.18,
                    anchor_y=((z["y0"] + z["y1"]) / 2.0) - 0.06,
                    queue_label=f"Station{station} Component Queue",
                    right_align=False,
                )
            fig.add_annotation(
                x=z["x0"] + 0.08,
                y=z["y0"] + 0.06,
                text=queue_text,
                showarrow=False,
                align="left",
                xanchor="left",
                yanchor="bottom",
                font=dict(size=9, color="#34495e"),
            )
        elif zone_name == "Inspection":
            inspect_items = queue_snapshot["component"].get(4, [])
            queue_text = f"Product Queue: {_format_inventory(inspect_items)}"
            add_queue_markers(
                items=inspect_items,
                item_kind="product",
                anchor_x=z["x0"] + 0.14,
                anchor_y=z["y0"] + 0.38,
                queue_label="Inspection Product Queue",
            )
            fig.add_annotation(
                x=z["x0"] + 0.08,
                y=z["y0"] + 0.06,
                text=queue_text,
                showarrow=False,
                align="left",
                xanchor="left",
                yanchor="bottom",
                font=dict(size=9, color="#34495e"),
            )
    if queue_x:
        fig.add_trace(
            go.Scatter(
                x=queue_x,
                y=queue_y,
                mode="markers",
                marker=dict(size=6, color=queue_c, symbol="diamond", line=dict(width=1, color="#1f2d3d")),
                hovertext=queue_h,
                hovertemplate="%{hovertext}<extra></extra>",
                name="Queue Items",
                showlegend=False,
            )
        )

    # Machines
    machine_positions: dict[str, tuple[float, float]] = {}
    station_counts: dict[int, int] = defaultdict(int)
    for machine_id in sorted(machine_states):
        station = _station_from_machine(machine_id)
        if station is None or station not in (1, 2):
            continue
        zone_name = f"Station{station}"
        z = ZONE_LAYOUT[zone_name]
        idx = station_counts[station]
        station_counts[station] += 1
        # Place machine markers on the right side of each station box,
        # stacked top/bottom (vertical layout).
        x = z["x1"] - 0.42
        y = z["y1"] - 0.38 - (idx % 2) * 0.38
        machine_positions[machine_id] = (x, y)

    output_buffer_snapshot = queue_snapshot.get("output_buffer", {})
    for station in (1, 2, 4):
        zone_name = "Inspection" if station == 4 else f"Station{station}"
        z = ZONE_LAYOUT[zone_name]
        ob_items = output_buffer_snapshot.get(station, [])
        fig.add_annotation(
            x=z["x1"] - 0.08,
            y=z["y0"] + 0.06,
            text=f"Output Buffer: {_format_inventory(ob_items)}",
            showarrow=False,
            align="right",
            xanchor="right",
            yanchor="bottom",
            font=dict(size=9, color="#34495e"),
        )

    if machine_positions:
        mx: list[float] = []
        my: list[float] = []
        mtext: list[str] = []
        mcolor: list[str] = []
        for machine_id, (x, y) in machine_positions.items():
            status = machine_states.get(machine_id, "IDLE")
            status_label = _machine_status_label(status)
            mx.append(x)
            my.append(y)
            mcolor.append(MACHINE_STATUS_COLOR.get(status, "#7f8c8d"))
            mtext.append(f"{machine_id}<br>status={status_label}")
        fig.add_trace(
            go.Scatter(
                x=mx,
                y=my,
                mode="markers+text",
                text=[mid for mid in sorted(machine_positions.keys())],
                textposition="middle right",
                marker=dict(size=14, color=mcolor, symbol=MACHINE_MARKER_SYMBOL),
                hovertemplate="%{text}<extra></extra>",
                name="Machines",
            )
        )

        ob_x: list[float] = []
        ob_y: list[float] = []
        ob_c: list[str] = []
        ob_h: list[str] = []
        for station in (1, 2, 4):
            ob_items = output_buffer_snapshot.get(station, [])
            if not ob_items:
                continue
            if station == 4:
                z = ZONE_LAYOUT["Inspection"]
                anchor_x = z["x1"] - 0.20
                anchor_y = (z["y0"] + z["y1"]) / 2.0
            else:
                station_machine_xy = [
                    xy
                    for mid, xy in machine_positions.items()
                    if _station_from_machine(mid) == station
                ]
                if station_machine_xy:
                    anchor_x = max(x for x, _ in station_machine_xy) + 0.08
                    anchor_y = sum(y for _, y in station_machine_xy) / float(len(station_machine_xy))
                else:
                    z = ZONE_LAYOUT[f"Station{station}"]
                    anchor_x = z["x1"] - 0.20
                    anchor_y = (z["y0"] + z["y1"]) / 2.0

            visible_items = ob_items[:5]
            for idx, item_id in enumerate(visible_items):
                dx = idx * 0.06
                item_upper = str(item_id).upper()
                item_kind = "product" if item_upper.startswith("PRODUCT") else "component"
                ob_x.append(anchor_x + dx)
                ob_y.append(anchor_y)
                ob_c.append(CARGO_COLOR.get(item_kind, CARGO_COLOR["component"]))
                ob_h.append(
                    f"station=Station{station}<br>output_buffer_item={item_id}<br>item_type={item_kind}"
                )

            if len(ob_items) > len(visible_items):
                fig.add_annotation(
                    x=anchor_x - 0.06,
                    y=anchor_y,
                    text="...",
                    showarrow=False,
                    font=dict(size=8, color="#5d6d7e"),
                )

        if ob_x:
            fig.add_trace(
                go.Scatter(
                    x=ob_x,
                    y=ob_y,
                    mode="markers",
                    marker=dict(size=6, color=ob_c, symbol="diamond", line=dict(width=1, color="#1f2d3d")),
                    hovertext=ob_h,
                    hovertemplate="%{hovertext}<extra></extra>",
                    name="Output Buffer",
                    showlegend=False,
                )
            )

        # Visualize machine-internal loaded inputs (material/component).
        in_x: list[float] = []
        in_y: list[float] = []
        in_c: list[str] = []
        in_h: list[str] = []
        for machine_id, (mx0, my0) in machine_positions.items():
            slots = machine_inputs.get(machine_id, {})
            if slots.get("material", False):
                in_x.append(mx0 - 0.028)
                in_y.append(my0 + 0.028)
                in_c.append(CARGO_COLOR["material"])
                in_h.append(f"{machine_id}<br>in_machine=material")
            if slots.get("component", False):
                in_x.append(mx0 - 0.028)
                in_y.append(my0 - 0.028)
                in_c.append(CARGO_COLOR["component"])
                in_h.append(f"{machine_id}<br>in_machine=component")
            output_kind = str(slots.get("output", "")).strip().lower()
            if output_kind in {"component", "product"}:
                in_x.append(mx0 + 0.03)
                in_y.append(my0)
                in_c.append(CARGO_COLOR.get(output_kind, CARGO_COLOR["product"]))
                in_h.append(f"{machine_id}<br>in_machine_output={output_kind}")
        if in_x:
            fig.add_trace(
                go.Scatter(
                    x=in_x,
                    y=in_y,
                    mode="markers",
                    marker=dict(size=6, color=in_c, symbol="diamond", line=dict(width=1, color="#1f2d3d")),
                    hovertext=in_h,
                    hovertemplate="%{hovertext}<extra></extra>",
                    name="Machine Inputs",
                    showlegend=False,
                )
            )

    # Agents
    if not agent_df.empty:
        offsets = [(-0.18, -0.18), (0.18, -0.18), (-0.18, 0.18), (0.18, 0.18), (0.0, 0.0), (0.0, 0.28)]
        machine_adjacent_tasks = {"UNLOAD_MACHINE", "SETUP_MACHINE", "PREVENTIVE_MAINTENANCE", "REPAIR_MACHINE"}
        machine_adjacent_offsets = [(0.11, 0.0), (0.11, 0.07), (0.11, -0.07), (0.03, 0.1), (0.03, -0.1)]
        # Keep battery-swap sender/receiver visually attached in the same zone.
        battery_pair_anchor: dict[tuple[str, str], tuple[float, float]] = {}
        battery_pair_pos: dict[str, tuple[float, float]] = {}
        zone_pair_counts: dict[str, int] = defaultdict(int)
        pair_anchor_offsets = [(-0.06, -0.05), (0.06, -0.05), (-0.06, 0.05), (0.06, 0.05)]

        for row in agent_df.sort_values("agent_id").itertuples(index=False):
            if str(row.status) != "WORKING" or str(row.task) != "BATTERY_SWAP":
                continue
            target = str(row.target)
            if "->" not in target:
                continue
            sender, receiver = [s.strip() for s in target.split("->", 1)]
            if not sender or not receiver or sender == receiver:
                continue
            pair_key = tuple(sorted((sender, receiver)))
            zone_name = _canonical_zone(str(row.zone))
            if pair_key not in battery_pair_anchor:
                cx, cy = zone_center(zone_name)
                pidx = zone_pair_counts[zone_name]
                zone_pair_counts[zone_name] += 1
                pdx, pdy = pair_anchor_offsets[pidx % len(pair_anchor_offsets)]
                battery_pair_anchor[pair_key] = (cx + pdx, cy + pdy)
            ax0, ay0 = battery_pair_anchor[pair_key]
            if str(row.agent_id) == sender:
                battery_pair_pos[str(row.agent_id)] = (ax0 - 0.018, ay0)
            elif str(row.agent_id) == receiver:
                battery_pair_pos[str(row.agent_id)] = (ax0 + 0.018, ay0)

        # For machine-side tasks, pin agents next to the target machine marker.
        machine_task_pos_by_agent: dict[str, tuple[float, float]] = {}
        machine_task_counts: dict[str, int] = defaultdict(int)
        for row in agent_df.sort_values(["zone_enter_t", "agent_id"], kind="mergesort").itertuples(index=False):
            if str(row.status) != "WORKING":
                continue
            task = str(row.task).strip().upper()
            if task not in machine_adjacent_tasks:
                continue
            machine_id = str(row.target).strip()
            if machine_id not in machine_positions:
                continue
            idx = machine_task_counts[machine_id]
            machine_task_counts[machine_id] += 1
            mdx, mdy = machine_adjacent_offsets[idx % len(machine_adjacent_offsets)]
            mx0, my0 = machine_positions[machine_id]
            machine_task_pos_by_agent[str(row.agent_id)] = (mx0 + mdx, my0 + mdy)

        # Stable slot allocation inside each zone: earlier arrival keeps position.
        static_slot_offset_by_agent: dict[str, tuple[float, float]] = {}
        static_df = agent_df[(agent_df["status"] != "MOVING") & (agent_df["location_mode"] != "edge")].copy()
        reserved_ids = set(battery_pair_pos.keys()) | set(machine_task_pos_by_agent.keys())
        if reserved_ids:
            static_df = static_df[~static_df["agent_id"].isin(reserved_ids)]
        if not static_df.empty:
            for zone_name, zone_group in static_df.groupby("zone", sort=False):
                ordered = zone_group.sort_values(["zone_enter_t", "agent_id"], kind="mergesort")
                for idx, row in enumerate(ordered.itertuples(index=False)):
                    static_slot_offset_by_agent[str(row.agent_id)] = offsets[idx % len(offsets)]

        ax: list[float] = []
        ay: list[float] = []
        acolor: list[str] = []
        asymbol: list[str] = []
        atext: list[str] = []
        ahovers: list[str] = []
        cargo_x: list[float] = []
        cargo_y: list[float] = []
        cargo_color: list[str] = []
        cargo_symbol: list[str] = []
        cargo_hovers: list[str] = []

        for row in agent_df.sort_values("agent_id").itertuples(index=False):
            status = str(row.status)
            location_mode = str(getattr(row, "location_mode", "zone"))

            if location_mode == "edge":
                src_zone = _canonical_zone(str(row.from_zone))
                dst_zone = _canonical_zone(str(row.to_zone))
                p = float(row.move_progress)
                x, y = interpolate_on_route(src_zone, dst_zone, p)
            elif str(row.agent_id) in battery_pair_pos:
                x, y = battery_pair_pos[str(row.agent_id)]
            elif str(row.agent_id) in machine_task_pos_by_agent:
                x, y = machine_task_pos_by_agent[str(row.agent_id)]
            else:
                zone_name = _canonical_zone(str(row.zone))
                center_x, center_y = zone_center(zone_name)
                dx, dy = static_slot_offset_by_agent.get(str(row.agent_id), offsets[0])
                x = center_x + dx
                y = center_y + dy

            ax.append(x)
            ay.append(y)
            acolor.append(AGENT_STATUS_COLOR.get(status, "#7f8c8d"))
            asymbol.append(AGENT_MARKER_SYMBOL)
            atext.append(str(row.agent_id))
            ahovers.append(
                "<br>".join(
                    [
                        f"agent_id={row.agent_id}",
                        f"battery_remaining={row.battery:.1f} min",
                        f"current_task_type={row.task}",
                        f"carrying={row.carrying}",
                        f"eta={row.eta:.1f} min",
                        f"status={row.status}",
                        f"target={row.target}",
                    ]
                )
            )
            carrying = str(row.carrying).strip().lower()
            if carrying == "material+component":
                cargo_x.extend([x - 0.035, x + 0.035])
                cargo_y.extend([y, y])
                cargo_color.extend([CARGO_COLOR["material"], CARGO_COLOR["component"]])
                cargo_symbol.extend([_cargo_symbol("material"), _cargo_symbol("component")])
                cargo_hovers.extend(
                    [
                        f"agent_id={row.agent_id}<br>carrying=material",
                        f"agent_id={row.agent_id}<br>carrying=component",
                    ]
                )
            elif carrying in CARGO_COLOR:
                cargo_x.append(x)
                cargo_y.append(y)
                cargo_color.append(CARGO_COLOR[carrying])
                cargo_symbol.append(_cargo_symbol(carrying))
                cargo_hovers.append(f"agent_id={row.agent_id}<br>carrying={carrying}")

        fig.add_trace(
            go.Scatter(
                x=ax,
                y=ay,
                mode="markers+text",
                text=atext,
                textposition="top center",
                marker=dict(size=17, color=acolor, symbol=asymbol, line=dict(width=1, color="#1f2d3d")),
                hovertext=ahovers,
                hovertemplate="%{hovertext}<extra></extra>",
                name="Agents",
            )
        )
        if cargo_x:
            fig.add_trace(
                go.Scatter(
                    x=cargo_x,
                    y=cargo_y,
                    mode="markers",
                    marker=dict(size=6, color=cargo_color, symbol=cargo_symbol, line=dict(width=1, color="#1f2d3d")),
                    hovertext=cargo_hovers,
                    hovertemplate="%{hovertext}<extra></extra>",
                    name="Cargo",
                    showlegend=False,
                )
            )

    fig.update_layout(
        title=f"Factory Map | Sim Time: {sim_datetime_label} | Completed Products: {completed_products}",
        showlegend=False,
        xaxis=dict(visible=False, range=[4.0, 10.0]),
        yaxis=dict(visible=False, range=[0.05, 3.45]),
        height=430,
        margin=dict(l=6, r=6, t=55, b=4),
    )
    return fig




def _collect_townhall_events(events_df: pd.DataFrame, current_t: float) -> list[dict[str, Any]]:
    filtered = events_df[(events_df["t"] <= current_t) & (events_df["type"] == "CHAT_TOWNHALL")]
    sessions: list[dict[str, Any]] = []

    for row in filtered.itertuples(index=False):
        details = row.details if isinstance(row.details, dict) else {}
        trace = details.get("discussion_trace", [])

        transcript: list[dict[str, Any]] = []
        moderator_summary = ""
        rounds = 0
        memory_update: dict[str, Any] = {}

        if isinstance(trace, list):
            for item in trace:
                if not isinstance(item, dict):
                    continue
                if "agent_id" in item:
                    try:
                        ridx = int(item.get("round", 0))
                    except (TypeError, ValueError):
                        ridx = 0
                    rounds = max(rounds, ridx)
                    transcript.append(
                        {
                            "round": ridx,
                            "agent_id": str(item.get("agent_id", "")).strip(),
                            "utterance": str(item.get("utterance", "")).strip(),
                            "proposal": item.get("proposal", {}) if isinstance(item.get("proposal", {}), dict) else {},
                        }
                    )
                    continue

                role = str(item.get("role", "")).strip().lower()
                summary = str(item.get("summary", "")).strip()
                if role == "moderator" or summary:
                    if summary:
                        moderator_summary = summary
                    try:
                        rounds = max(rounds, int(item.get("rounds", rounds)))
                    except (TypeError, ValueError):
                        pass
                    if isinstance(item.get("memory_update", {}), dict):
                        memory_update = item.get("memory_update", {})

        day_summary = details.get("day_summary", {}) if isinstance(details.get("day_summary", {}), dict) else {}
        updated_norms = details.get("updated_norms", {}) if isinstance(details.get("updated_norms", {}), dict) else {}
        communication_enabled = bool(details.get("communication_enabled", False))

        sessions.append(
            {
                "t": float(row.t),
                "day": int(row.day),
                "communication_enabled": communication_enabled,
                "rounds": int(rounds),
                "messages": len(transcript),
                "transcript": transcript,
                "moderator_summary": moderator_summary,
                "day_summary": day_summary,
                "updated_norms": updated_norms,
                "memory_update": memory_update,
            }
        )

    return sessions


def _phase_details_for_day(events_df: pd.DataFrame, day: int, current_t: float) -> tuple[dict[str, Any], dict[str, Any]]:
    filtered = events_df[(events_df["t"] <= current_t) & (events_df["day"] == int(day))]
    strategy: dict[str, Any] = {}
    assignment: dict[str, Any] = {}

    for row in filtered.itertuples(index=False):
        details = row.details if isinstance(row.details, dict) else {}
        if row.type == "PHASE_STRATEGY" and not strategy:
            strategy = details
        elif row.type == "PHASE_JOB_ASSIGNMENT" and not assignment:
            assignment = details
        if strategy and assignment:
            break

    return strategy, assignment


def _render_townhall_conversation_panel(events_df: pd.DataFrame, current_t: float) -> None:
    st.markdown("### Townhall Conversation")
    sessions = _collect_townhall_events(events_df, current_t)
    if not sessions:
        st.info("No townhall conversation is available at the current replay time.")
        return

    overview_rows = [
        {
            "day": s["day"],
            "time(min)": round(float(s["t"]), 1),
            "communication": "on" if s["communication_enabled"] else "off",
            "rounds": int(s["rounds"]),
            "messages": int(s["messages"]),
            "has_summary": bool(str(s.get("moderator_summary", "")).strip()),
        }
        for s in sessions
    ]
    st.dataframe(pd.DataFrame(overview_rows), use_container_width=True, hide_index=True)

    option_indices = list(range(len(sessions)))
    selected_idx = st.selectbox(
        "Select townhall session",
        options=option_indices,
        index=len(option_indices) - 1,
        key="townhall_session_idx",
        format_func=lambda i: (
            f"Day {sessions[i]['day']} | t={sessions[i]['t']:.1f} | "
            f"rounds {sessions[i]['rounds']} | messages {sessions[i]['messages']}"
        ),
    )
    session = sessions[int(selected_idx)]
    strategy, assignment = _phase_details_for_day(events_df, int(session["day"]), current_t)

    left_col, right_col = st.columns([2.2, 1.3])

    with left_col:
        transcript = session.get("transcript", [])
        if not transcript:
            st.info("This session has no per-agent transcript.")
        else:
            transcript_rows = [
                {
                    "round": int(msg.get("round", 0)),
                    "agent": str(msg.get("agent_id", "")).strip(),
                    "utterance": str(msg.get("utterance", "")).strip(),
                    "has_proposal": bool(msg.get("proposal", {})),
                }
                for msg in transcript
            ]
            st.dataframe(pd.DataFrame(transcript_rows), use_container_width=True, hide_index=True)

            round_ids = sorted({int(msg.get("round", 0)) for msg in transcript if int(msg.get("round", 0)) > 0})
            if round_ids:
                tabs = st.tabs([f"Round {rid}" for rid in round_ids])
                for tab, rid in zip(tabs, round_ids):
                    with tab:
                        for msg in transcript:
                            if int(msg.get("round", 0)) != rid:
                                continue
                            speaker = str(msg.get("agent_id", "")).strip() or "Agent"
                            utterance = str(msg.get("utterance", "")).strip() or "-"
                            st.markdown(f"**{speaker}**")
                            st.write(utterance)
                            proposal = msg.get("proposal", {})
                            if isinstance(proposal, dict) and proposal:
                                with st.expander(f"{speaker} proposal", expanded=False):
                                    st.json(proposal)
            else:
                for msg in transcript:
                    speaker = str(msg.get("agent_id", "")).strip() or "Agent"
                    utterance = str(msg.get("utterance", "")).strip() or "-"
                    st.markdown(f"**{speaker}**")
                    st.write(utterance)
                    proposal = msg.get("proposal", {})
                    if isinstance(proposal, dict) and proposal:
                        with st.expander(f"{speaker} proposal", expanded=False):
                            st.json(proposal)

        moderator_summary = str(session.get("moderator_summary", "")).strip()
        if moderator_summary:
            st.markdown("**Moderator Summary**")
            st.info(moderator_summary)

    with right_col:
        day_summary = session.get("day_summary", {}) if isinstance(session.get("day_summary", {}), dict) else {}
        products = int(day_summary.get("products", 0))
        scrap = int(day_summary.get("scrap", 0))
        breakdowns = int(day_summary.get("machine_breakdowns", 0))
        backlog = int(day_summary.get("inspection_backlog_end", 0))

        m1, m2 = st.columns(2)
        m1.metric("Products", products)
        m2.metric("Scrap", scrap)
        m3, m4 = st.columns(2)
        m3.metric("Breakdowns", breakdowns)
        m4.metric("Inspection Backlog", backlog)

        with st.expander("Daily Summary JSON", expanded=False):
            st.json(day_summary)
        if strategy:
            with st.expander("Day Start Strategy", expanded=False):
                st.json(strategy)
        if assignment:
            with st.expander("Day Start Job Assignment", expanded=False):
                st.json(assignment)

        updated_norms = session.get("updated_norms", {}) if isinstance(session.get("updated_norms", {}), dict) else {}
        if updated_norms:
            with st.expander("Updated Norms", expanded=False):
                st.json(updated_norms)

        memory_update = session.get("memory_update", {}) if isinstance(session.get("memory_update", {}), dict) else {}
        if memory_update:
            with st.expander("Memory Update", expanded=False):
                st.json(memory_update)

def _render_kpi_panel(output_dir: Path) -> None:
    kpi_path = output_dir / "kpi.json"
    daily_path = output_dir / "daily_summary.json"
    kpi = _load_optional_json(str(kpi_path))
    daily_payload = _load_optional_json(str(daily_path))
    daily_rows = daily_payload.get("days", []) if isinstance(daily_payload, dict) else []

    if not isinstance(kpi, dict) or not daily_rows:
        st.info("KPI artifacts were not found for this output directory.")
        return

    st.subheader("KPI Charts")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Products", int(kpi.get("total_products", 0)))
    m2.metric("Scrap Count", int(kpi.get("scrap_count", 0)))
    m3.metric("Scrap Rate", f"{float(kpi.get('scrap_rate', 0.0)):.4f}")
    m4.metric("Machine Utilization", f"{float(kpi.get('machine_utilization', 0.0)):.4f}")

    daily_df = pd.DataFrame(daily_rows).sort_values("day")
    daily_fig = go.Figure()
    daily_fig.add_trace(go.Bar(x=daily_df["day"], y=daily_df["products"], name="products"))
    daily_fig.add_trace(go.Bar(x=daily_df["day"], y=daily_df["scrap"], name="scrap"))
    daily_fig.update_layout(barmode="group", title="Daily Products / Scrap", xaxis_title="day", yaxis_title="count")
    st.plotly_chart(daily_fig, use_container_width=True)

    breakdown_fig = go.Figure()
    breakdown_fig.add_trace(go.Scatter(x=daily_df["day"], y=daily_df["scrap_rate"], mode="lines+markers", name="scrap_rate"))
    breakdown_fig.add_trace(go.Scatter(x=daily_df["day"], y=daily_df["machine_breakdowns"], mode="lines+markers", name="breakdowns"))
    breakdown_fig.update_layout(title="Daily Scrap Rate / Machine Breakdowns", xaxis_title="day", yaxis_title="value")
    st.plotly_chart(breakdown_fig, use_container_width=True)

    tp = kpi.get("station_throughput", {})
    if isinstance(tp, dict) and tp:
        tp_df = pd.DataFrame({"station": [str(k) for k in tp.keys()], "throughput": [float(v) for v in tp.values()]})
        tp_fig = go.Figure(data=[go.Bar(x=tp_df["station"], y=tp_df["throughput"])])
        tp_fig.update_layout(title="Station Throughput", xaxis_title="station", yaxis_title="count")
        st.plotly_chart(tp_fig, use_container_width=True)

    task_minutes = kpi.get("agent_task_minutes", {})
    if isinstance(task_minutes, dict) and task_minutes:
        tm_df = pd.DataFrame({"task": list(task_minutes.keys()), "minutes": [float(v) for v in task_minutes.values()]})
        tm_df = tm_df.sort_values("minutes", ascending=False)
        tm_fig = go.Figure(data=[go.Bar(x=tm_df["task"], y=tm_df["minutes"])])
        tm_fig.update_layout(title="Agent Task Minutes", xaxis_title="task", yaxis_title="minutes")
        st.plotly_chart(tm_fig, use_container_width=True)


def _render_map_legend() -> None:
    st.markdown("<div style='font-size:12px;font-weight:700;margin-top:2px;'>Agents</div>", unsafe_allow_html=True)
    for status, color in AGENT_STATUS_COLOR.items():
        label = _agent_status_label(status)
        st.markdown(
            f"<div style='line-height:1.05;margin:1px 0;'>"
            f"<span style='display:inline-block;width:10px;height:10px;background:{color};"
            f"border:1px solid #333;border-radius:50%;margin-right:6px;vertical-align:middle;'></span>"
            f"<span style='font-size:12px;vertical-align:middle;'>{label}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='font-size:12px;font-weight:700;margin-top:6px;'>Machines</div>", unsafe_allow_html=True)
    for raw_status, color in MACHINE_STATUS_COLOR.items():
        label = _machine_status_label(raw_status)
        st.markdown(
            f"<div style='line-height:1.05;margin:1px 0;'>"
            f"<span style='display:inline-block;width:10px;height:10px;background:{color};"
            f"border:1px solid #333;border-radius:0;margin-right:6px;vertical-align:middle;'></span>"
            f"<span style='font-size:12px;vertical-align:middle;'>{label}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
    st.markdown("<div style='font-size:12px;font-weight:700;margin-top:6px;'>Cargo Items</div>", unsafe_allow_html=True)
    cargo_labels = {
        "material": "Material",
        "component": "Component",
        "product": "Product",
    }
    cargo_order = ["material", "component", "product"]
    for cargo_kind in cargo_order:
        if cargo_kind not in CARGO_COLOR:
            continue
        color = CARGO_COLOR[cargo_kind]
        st.markdown(
            f"<div style='line-height:1.05;margin:1px 0;'>"
            f"<span style='display:inline-block;width:10px;height:10px;background:{color};"
            f"border:1px solid #333;transform:rotate(45deg);margin-right:8px;vertical-align:middle;'></span>"
            f"<span style='font-size:12px;vertical-align:middle;'>{cargo_labels[cargo_kind]}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )

    st.markdown("<div style='font-size:12px;font-weight:700;margin-top:6px;'>Batteries</div>", unsafe_allow_html=True)
    battery_labels = {
        "battery_fresh": "Battery (Fresh)",
        "battery_spent": "Battery (Spent)",
        "battery": "Battery",
    }
    battery_order = ["battery_fresh", "battery_spent", "battery"]
    for battery_kind in battery_order:
        if battery_kind not in CARGO_COLOR:
            continue
        if battery_kind == "battery" and "battery_fresh" in CARGO_COLOR:
            continue
        color = CARGO_COLOR[battery_kind]
        if battery_kind == "battery_spent":
            shape_css = "clip-path:polygon(0% 0%,100% 0%,50% 100%);"
        else:
            shape_css = "clip-path:polygon(50% 0%,0% 100%,100% 100%);"
        st.markdown(
            f"<div style='line-height:1.05;margin:1px 0;'>"
            f"<span style='display:inline-block;width:11px;height:11px;background:{color};"
            f"border:1px solid #333;{shape_css}margin-right:8px;vertical-align:middle;'></span>"
            f"<span style='font-size:12px;vertical-align:middle;'>{battery_labels[battery_kind]}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )


def main() -> None:
    st.set_page_config(page_title="Manufacturing Replay", layout="wide")
    st.title("Manufacturing Simulation Replay Dashboard")

    root = _repo_root()
    latest = _latest_events_path(root)
    query_events_path = ""
    try:
        query_events = st.query_params.get("events_path", "")
        if isinstance(query_events, list):
            query_events = query_events[0] if query_events else ""
        query_events_path = unquote(str(query_events)).strip()
    except Exception:
        query_events_path = ""

    default_events_path = query_events_path or (str(latest) if latest is not None else "events.jsonl")

    events_path_input = st.text_input("events.jsonl path", value=default_events_path)
    events_path = Path(events_path_input)
    if not events_path.exists():
        st.warning("Provide a valid events.jsonl path.")
        st.stop()

    events_df = _load_events(str(events_path))
    if events_df.empty:
        st.warning("No events found.")
        st.stop()

    run_meta = _load_run_meta(str(events_path))
    mode = str(run_meta.get("mode", "unknown")).strip().lower() or "unknown"
    if mode == "llm":
        comm = run_meta.get("communication_enabled", None)
        comm_label = "on" if bool(comm) else "off"
        st.info(
            f"Run mode: LLM | model={run_meta.get('model', '-') or '-'} | communication={comm_label} | server={run_meta.get('server_url', '-') or '-'}"
        )
    else:
        st.info(f"Run mode: {format_decision_mode_label(mode)}")

    max_t = float(events_df["t"].max())
    if "replay_t" not in st.session_state:
        st.session_state.replay_t = 0.0
    if "playing" not in st.session_state:
        st.session_state.playing = False
    if "speed" not in st.session_state:
        st.session_state.speed = 2.0
    if "events_path" not in st.session_state:
        st.session_state.events_path = events_path_input
    if st.session_state.events_path != events_path_input:
        st.session_state.events_path = events_path_input
        st.session_state.replay_t = 0.0
        st.session_state.playing = False

    st.session_state.speed = st.select_slider(
        "Speed (minute/frame)",
        options=[0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0],
        value=float(st.session_state.speed),
    )

    slider_t = st.slider(
        "Time (minute)",
        min_value=0.0,
        max_value=max_t,
        value=float(st.session_state.replay_t),
        step=0.5,
    )
    st.session_state.replay_t = float(slider_t)
    current_t = float(st.session_state.replay_t)

    machine_states = _compute_machine_states(events_df, current_t)
    machine_inputs = _compute_machine_inputs(events_df, current_t)
    battery_period_min = _load_battery_period_min(str(events_path))
    agent_df = _compute_agent_states(events_df, current_t, battery_period_min=battery_period_min)
    queue_snapshot = _compute_queue_snapshot(events_df, current_t)
    sim_day, sim_datetime_label = _simulation_datetime(events_df, current_t)
    completed_products = int((events_df[(events_df["t"] <= current_t) & (events_df["type"] == "INSPECT_PASS")]).shape[0])

    st.markdown("### Factory Map")
    map_col, legend_col = st.columns([3.2, 1.0])
    with map_col:
        meta_l, meta_r = st.columns([3.0, 1.2])
        with meta_l:
            st.caption(f"Current simulation day/time: Day {sim_day}, {sim_datetime_label}")
        with meta_r:
            st.markdown(
                f"<div style='font-size:12px;color:#6b7280;'>Completed Products</div>"
                f"<div style='font-size:28px;font-weight:700;line-height:1.05;'>{completed_products}</div>",
                unsafe_allow_html=True,
            )
        map_fig = _draw_factory_map(
            agent_df=agent_df,
            machine_states=machine_states,
            machine_inputs=machine_inputs,
            queue_snapshot=queue_snapshot,
            sim_datetime_label=f"Day {sim_day}, {sim_datetime_label}",
            completed_products=completed_products,
        )
        st.plotly_chart(map_fig, use_container_width=True)
    with legend_col:
        btn1, btn2 = st.columns(2)
        with btn1:
            toggle_label = "Pause" if st.session_state.playing else "Play"
            if st.button(toggle_label, key="play_pause_toggle", use_container_width=True):
                st.session_state.playing = not bool(st.session_state.playing)
        with btn2:
            if st.button("Reset", use_container_width=True):
                st.session_state.replay_t = 0.0
                st.session_state.playing = False
        st.markdown("<div style='height:14px;'></div>", unsafe_allow_html=True)
        _render_map_legend()

    if agent_df.empty:
        st.info("No agent data at current time.")
    else:
        display_df = agent_df.sort_values("agent_id").reset_index(drop=True).copy()
        edge_mask = display_df["location_mode"] == "edge"
        if edge_mask.any():
            display_df.loc[edge_mask, "zone"] = display_df.loc[edge_mask].apply(
                lambda r: f"{r['from_zone']}->{r['to_zone']} ({int(float(r['move_progress']) * 100)}%)",
                axis=1,
            )
        display_df = display_df[
            ["agent_id", "zone", "status", "task", "target", "battery", "carrying", "down_reason", "eta"]
        ].copy()
        st.dataframe(display_df, use_container_width=True, hide_index=True)

    _render_townhall_conversation_panel(events_df, current_t)

    st.markdown("### Recent Events")
    recent_df = events_df[events_df["t"] <= current_t].tail(40)
    st.dataframe(recent_df, use_container_width=True, hide_index=True)

    output_dir = events_path.parent
    _render_kpi_panel(output_dir)

    if st.session_state.playing:
        next_t = min(max_t, current_t + float(st.session_state.speed))
        st.session_state.replay_t = next_t
        if next_t >= max_t:
            st.session_state.playing = False
        time.sleep(0.25)
        st.rerun()


if __name__ == "__main__":
    main()








