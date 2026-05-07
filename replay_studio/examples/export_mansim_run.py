from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List
import yaml

from python_event_builder import ReplayEventBuilder

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import TileGridMap
except Exception:  # pragma: no cover - keeps standalone demo exporter usable.
    TileGridMap = None  # type: ignore[assignment]


REGION_ID = {
    "Home": "warehouse_region",
    "Warehouse": "warehouse_region",
    "Station1": "station_1_region",
    "Station2": "station_2_region",
    "Inspection": "inspection_region",
    "BatteryStation": "battery_station_region",
}

QUEUE_META = {
    "material_queue_1": ("queue", "S1 Material Queue"),
    "material_queue_2": ("queue", "S2 Material Queue"),
    "intermediate_queue_2": ("buffer", "S2 Transfer Queue"),
    "intermediate_queue_4": ("queue", "Inspection Queue"),
    "warehouse_buffer": ("buffer", "Completed Buffer"),
    "battery_rack": ("charger", "Battery Rack"),
}

OUTPUT_QUEUE_META = {
    "station_1_output_queue": ("buffer", "S1 Output Queue", "output_buffer_station_1"),
    "station_2_output_queue": ("buffer", "S2 Output Queue", "output_buffer_station_2"),
    "inspection_output_queue": ("buffer", "Inspection Output", "output_buffer_station_4"),
}

LAYOUT_TEMPLATE: Dict[str, Any] = {
    "source_priority": ["config", "auto"],
    "viewport": {"width": 1600, "height": 960},
    "regions": [
        {
            "region_id": "warehouse_region",
            "label": "Warehouse",
            "kind": "storage",
            "position": {"x": 590, "y": 100},
            "size": {"width": 420, "height": 170},
            "accent": "#86b6ff",
        },
        {
            "region_id": "station_1_region",
            "label": "Station 1",
            "kind": "station",
            "position": {"x": 70, "y": 340},
            "size": {"width": 450, "height": 290},
            "accent": "#8fb6ff",
        },
        {
            "region_id": "station_2_region",
            "label": "Station 2",
            "kind": "station",
            "position": {"x": 560, "y": 340},
            "size": {"width": 450, "height": 290},
            "accent": "#8fb6ff",
        },
        {
            "region_id": "inspection_region",
            "label": "Inspection",
            "kind": "inspection",
            "position": {"x": 1090, "y": 340},
            "size": {"width": 450, "height": 290},
            "accent": "#adc0eb",
        },
        {
            "region_id": "battery_station_region",
            "label": "Battery Station",
            "kind": "battery",
            "position": {"x": 590, "y": 680},
            "size": {"width": 420, "height": 140},
            "accent": "#96c3f7",
        },
    ],
    "nodes": [
        {"entity_id": "material_queue_1", "entity_type": "queue", "region_id": "station_1_region", "anchor": {"x": 0.18, "y": 0.46}},
        {"entity_id": "station_1_output_queue", "entity_type": "buffer", "region_id": "station_1_region", "anchor": {"x": 0.84, "y": 0.46}},
        {"entity_id": "S1M1", "entity_type": "machine", "region_id": "station_1_region", "anchor": {"x": 0.60, "y": 0.36}},
        {"entity_id": "S1M2", "entity_type": "machine", "region_id": "station_1_region", "anchor": {"x": 0.60, "y": 0.72}},
        {"entity_id": "material_queue_2", "entity_type": "queue", "region_id": "station_2_region", "anchor": {"x": 0.18, "y": 0.30}},
        {"entity_id": "intermediate_queue_2", "entity_type": "buffer", "region_id": "station_2_region", "anchor": {"x": 0.18, "y": 0.60}},
        {"entity_id": "station_2_output_queue", "entity_type": "buffer", "region_id": "station_2_region", "anchor": {"x": 0.84, "y": 0.46}},
        {"entity_id": "S2M1", "entity_type": "machine", "region_id": "station_2_region", "anchor": {"x": 0.60, "y": 0.36}},
        {"entity_id": "S2M2", "entity_type": "machine", "region_id": "station_2_region", "anchor": {"x": 0.60, "y": 0.72}},
        {"entity_id": "intermediate_queue_4", "entity_type": "queue", "region_id": "inspection_region", "anchor": {"x": 0.20, "y": 0.46}},
        {"entity_id": "inspection_output_queue", "entity_type": "buffer", "region_id": "inspection_region", "anchor": {"x": 0.82, "y": 0.46}},
    ],
}


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def parse_worker_ids(run_meta: Dict[str, Any], raw_events: List[Dict[str, Any]]) -> List[str]:
    worker_ids = run_meta.get("llm", {}).get("openclaw", {}).get("worker_agent_ids", [])
    if worker_ids:
        return list(worker_ids)
    discovered = sorted(
        {
            event.get("entity_id")
            for event in raw_events
            if isinstance(event.get("entity_id"), str) and event["entity_id"].startswith("A")
        }
    )
    return [worker_id for worker_id in discovered if worker_id[1:].isdigit()]


def parse_machine_ids(raw_events: List[Dict[str, Any]]) -> List[str]:
    machine_ids = sorted(
        {
            event.get("entity_id")
            for event in raw_events
            if isinstance(event.get("entity_id"), str)
            and len(event["entity_id"]) == 4
            and event["entity_id"].startswith("S")
            and "M" in event["entity_id"]
        }
    )
    return machine_ids


def _stations_from_scenario(scenario_cfg: Dict[str, Any]) -> List[int]:
    factory_cfg = scenario_cfg.get("factory", {}) if isinstance(scenario_cfg.get("factory", {}), dict) else {}
    processing = factory_cfg.get("processing_time_min", {}) if isinstance(factory_cfg.get("processing_time_min", {}), dict) else {}
    stations: List[int] = []
    for key in processing:
        text = str(key)
        if text.startswith("station") and text.replace("station", "", 1).isdigit():
            stations.append(int(text.replace("station", "", 1)))
    return sorted(stations) or [1, 2]


def build_layout(worker_ids: List[str], scenario_cfg: Dict[str, Any] | None = None) -> Dict[str, Any]:
    scenario_cfg = scenario_cfg if isinstance(scenario_cfg, dict) else {}
    map_cfg = scenario_cfg.get("map", {}) if isinstance(scenario_cfg.get("map", {}), dict) else {}
    if TileGridMap is not None and bool(map_cfg.get("enabled", True)):
        factory_cfg = scenario_cfg.get("factory", {}) if isinstance(scenario_cfg.get("factory", {}), dict) else {}
        try:
            grid = TileGridMap.from_world_config(
                scenario_cfg,
                stations=_stations_from_scenario(scenario_cfg),
                machines_per_station=int(factory_cfg.get("machines_per_station", 2) or 2),
            )
            return grid.to_replay_layout(worker_ids)
        except Exception:
            pass

    layout: Dict[str, Any] = {
        "source_priority": list(LAYOUT_TEMPLATE["source_priority"]),
        "viewport": dict(LAYOUT_TEMPLATE["viewport"]),
        "regions": [dict(region) for region in LAYOUT_TEMPLATE["regions"]],
        "nodes": [dict(node) for node in LAYOUT_TEMPLATE["nodes"]],
    }
    worker_anchors = [0.30, 0.50, 0.70]
    for index, worker_id in enumerate(worker_ids):
        layout["nodes"].append(
            {
                "entity_id": worker_id,
                "entity_type": "worker",
                "region_id": "warehouse_region",
                "anchor": {"x": worker_anchors[index % len(worker_anchors)], "y": 0.58},
            }
        )
    return layout


def layout_positions(layout: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    positions: Dict[str, Dict[str, float]] = {}
    regions = {region["region_id"]: region for region in layout.get("regions", [])}
    for region_id, region in regions.items():
        positions[region_id] = {
            "x": region["position"]["x"] + region["size"]["width"] / 2,
            "y": region["position"]["y"] + region["size"]["height"] / 2,
        }
    for node in layout.get("nodes", []):
        if "position" in node and node["position"] is not None:
            positions[node["entity_id"]] = dict(node["position"])
            continue
        region = regions.get(node.get("region_id"))
        anchor = node.get("anchor")
        if region and anchor:
            positions[node["entity_id"]] = {
                "x": region["position"]["x"] + region["size"]["width"] * anchor["x"],
                "y": region["position"]["y"] + region["size"]["height"] * anchor["y"],
            }
    return positions


def tile_to_position(layout: Dict[str, Any], tile: Dict[str, Any] | None) -> Dict[str, float] | None:
    if not isinstance(tile, dict):
        return None
    grid = layout.get("grid", {}) if isinstance(layout.get("grid", {}), dict) else {}
    width_tiles = float(grid.get("width_tiles", 0) or 0)
    height_tiles = float(grid.get("height_tiles", 0) or 0)
    viewport = layout.get("viewport", {}) if isinstance(layout.get("viewport", {}), dict) else {}
    viewport_w = float(viewport.get("width", 1600) or 1600)
    viewport_h = float(viewport.get("height", 960) or 960)
    if width_tiles <= 0 or height_tiles <= 0:
        return None
    try:
        x = int(tile.get("x"))
        y = int(tile.get("y"))
    except Exception:
        return None
    return {"x": (x + 0.5) * (viewport_w / width_tiles), "y": (y + 0.5) * (viewport_h / height_tiles)}


def path_tiles_to_positions(layout: Dict[str, Any], tiles: Any) -> List[Dict[str, float]]:
    if not isinstance(tiles, list):
        return []
    positions: List[Dict[str, float]] = []
    for tile in tiles:
        position = tile_to_position(layout, tile)
        if position is not None:
            positions.append(position)
    return positions


def build_initial_state(
    worker_ids: List[str],
    machine_ids: List[str],
    layout: Dict[str, Any],
    battery_period_min: float,
    repair_total_min: float,
) -> Dict[str, Any]:
    positions = layout_positions(layout)
    entities: Dict[str, Dict[str, Any]] = {}

    def add_entity(entity_id: str, entity_type: str, label: str, state: str, *, attributes: Dict[str, Any] | None = None) -> None:
        entities[entity_id] = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "state": state,
            "label": label,
            "position": positions.get(entity_id),
            "attributes": attributes or {},
            "relations": {},
            "updated_at": 0,
        }

    for queue_id, (entity_type, label) in QUEUE_META.items():
        attributes = {"queue_size": 0}
        if queue_id == "warehouse_buffer":
            attributes = {"completed_count": 0}
        add_entity(queue_id, entity_type, label, "waiting", attributes=attributes)

    for queue_id, (entity_type, label, derived_from_queue) in OUTPUT_QUEUE_META.items():
        add_entity(
            queue_id,
            entity_type,
            label,
            "waiting",
            attributes={"queue_size": 0, "queue_kind": "output", "derived_from_queue": derived_from_queue},
        )

    for machine_id in machine_ids:
        add_entity(
            machine_id,
            "machine",
            machine_id,
            "idle",
            attributes={
                "utilization": 0,
                "repair_team": [],
                "repair_team_size": 0,
                "repair_remaining_min": 0.0,
                "repair_total_min": repair_total_min,
                "repair_window": None,
            },
        )

    for worker_id in worker_ids:
        add_entity(
            worker_id,
            "worker",
            f"Worker {worker_id}",
            "idle",
            attributes={"battery_pct": 100, "battery_period_min": battery_period_min, "last_swap_at": 0},
        )

    queues = {
        queue_id: {"queue_id": queue_id, "item_ids": [], "updated_at": 0}
        for queue_id in ("material_queue_1", "material_queue_2", "intermediate_queue_2", "intermediate_queue_4", "warehouse_buffer")
    }
    return {
        "timestamp": 0,
        "entities": entities,
        "resources": {},
        "queues": queues,
        "annotations": [],
    }


def build_task_end_index(raw_events: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {
        event["details"]["task_id"]: event
        for event in raw_events
        if event["type"] == "AGENT_TASK_END" and isinstance(event.get("details", {}).get("task_id"), str)
    }


def resolve_region_id(location: str | None) -> str | None:
    if not location:
        return None
    return REGION_ID.get(location)


def find_move_end_time(raw_events: List[Dict[str, Any]], start_index: int, entity_id: str, target_location: str) -> float | None:
    start_t = float(raw_events[start_index].get("t", 0.0) or 0.0)
    for offset in range(start_index + 1, len(raw_events)):
        candidate = raw_events[offset]
        if candidate["entity_id"] != entity_id:
            continue
        if candidate["type"] == "AGENT_MOVE_INTERRUPTED":
            return float(candidate["t"])
        if candidate["type"] == "AGENT_MOVE_START":
            same_start_move = (
                abs(float(candidate.get("t", 0.0) or 0.0) - start_t) <= 1e-9
                and candidate.get("details", {}).get("to", candidate.get("location")) == target_location
            )
            if same_start_move:
                continue
            return None
        if candidate["type"] != "AGENT_MOVE_END":
            continue
        if candidate["details"].get("to", candidate.get("location")) != target_location:
            continue
        return float(candidate["t"])
    return None


def find_machine_phase_end_time(raw_events: List[Dict[str, Any]], start_index: int, machine_id: str, start_type: str) -> float | None:
    end_type_map = {
        "MACHINE_START": {"MACHINE_END", "MACHINE_ABORTED", "MACHINE_BROKEN"},
        "MACHINE_SETUP_START": {"MACHINE_SETUP_END", "MACHINE_ABORTED", "MACHINE_BROKEN"},
        "MACHINE_REPAIR_START": {"MACHINE_REPAIRED"},
        "MACHINE_PM_START": {"MACHINE_PM_END"},
    }
    expected_end_types = end_type_map.get(start_type, set())
    if not expected_end_types:
        return None
    for offset in range(start_index + 1, len(raw_events)):
        candidate = raw_events[offset]
        if candidate.get("entity_id") != machine_id:
            continue
        if candidate.get("type") in expected_end_types:
            return float(candidate["t"])
    return None


def task_target(details: Dict[str, Any]) -> str | None:
    payload = details.get("payload", {})
    task_type = details.get("task_type")
    if task_type == "TRANSFER":
        kind = payload.get("transfer_kind")
        station = payload.get("station")
        if kind == "material_supply" and station in (1, 2):
            return f"material_queue_{station}"
        if kind == "inter_station_transfer":
            return "intermediate_queue_2"
        if kind in {"battery_delivery_low_battery", "battery_delivery_discharged"}:
            return "battery_rack"
        return resolve_region_id(details.get("location")) or "warehouse_buffer"
    if task_type in {"SETUP_MACHINE", "REPAIR_MACHINE", "UNLOAD_MACHINE", "PREVENTIVE_MAINTENANCE"}:
        return payload.get("machine_id")
    if task_type == "BATTERY_SWAP":
        return "battery_rack"
    if task_type == "INSPECT_PRODUCT":
        return "inspection_region"
    return None


def task_label(details: Dict[str, Any]) -> str:
    task_type = str(details.get("task_type", "task"))
    payload = details.get("payload", {})
    if task_type == "TRANSFER":
        kind = payload.get("transfer_kind")
        station = payload.get("station")
        if kind == "material_supply" and station:
            return f"Supply S{station}"
        if kind == "inter_station_transfer":
            return "Handoff"
        if kind == "battery_delivery_low_battery":
            return "Battery Assist"
        if kind == "battery_delivery_discharged":
            return "Emergency Battery"
    if task_type == "SETUP_MACHINE":
        return f"Setup {payload.get('machine_id', '')}".strip()
    if task_type == "REPAIR_MACHINE":
        return f"Repair {payload.get('machine_id', '')}".strip()
    if task_type == "UNLOAD_MACHINE":
        return f"Unload {payload.get('machine_id', '')}".strip()
    if task_type == "PREVENTIVE_MAINTENANCE":
        return f"PM {payload.get('machine_id', '')}".strip()
    if task_type == "BATTERY_SWAP":
        return "Swap Battery"
    if task_type == "INSPECT_PRODUCT":
        return "Inspect Product"
    return task_type.replace("_", " ").title()


def incident_severity(incident_class: str) -> str:
    if incident_class in {"machine_broken", "deadlock_detected"}:
        return "error"
    return "warning"


def machine_station(machine_id: str | None) -> int | None:
    if not machine_id or len(machine_id) < 2 or not machine_id.startswith("S"):
        return None
    try:
        return int(machine_id[1])
    except Exception:
        return None


def machine_output_item_kind(machine_id: str | None) -> str:
    station = machine_station(machine_id)
    if station == 1:
        return "intermediate"
    return "product"


def machine_prep_item_kind(machine_id: str | None) -> str:
    station = machine_station(machine_id)
    if station == 2:
        return "intermediate"
    return "material"


def worker_region_slot(region_id: str | None, entity_id: str, layout: Dict[str, Any]) -> Dict[str, float] | None:
    if not region_id:
        return None
    regions = {region["region_id"]: region for region in layout.get("regions", [])}
    region = regions.get(region_id)
    if not region:
        return None
    try:
        index = max(0, int(entity_id[1:]) - 1)
    except Exception:
        index = 0
    offsets = [0.30, 0.50, 0.70]
    return {
        "x": region["position"]["x"] + region["size"]["width"] * offsets[index % len(offsets)],
        "y": region["position"]["y"] + region["size"]["height"] * 0.58,
    }


def convert_events(
    raw_events: List[Dict[str, Any]],
    layout: Dict[str, Any],
    battery_period_min: float,
    repair_total_min: float,
) -> List[Dict[str, Any]]:
    builder = ReplayEventBuilder()
    task_end_by_id = build_task_end_index(raw_events)
    positions = layout_positions(layout)
    active_tasks: Dict[str, Dict[str, Any]] = {}
    completed_count = 0
    output_buffer_alias = {
        "output_buffer_station_1": "station_1_output_queue",
        "output_buffer_station_2": "station_2_output_queue",
        "output_buffer_station_4": "inspection_output_queue",
    }
    output_buffer_counts = {alias: 0 for alias in output_buffer_alias.values()}
    converted: List[Dict[str, Any]] = []
    has_canonical_worker_events = any(
        event.get("type") in {"WORKER_STATE_CHANGED", "WORKER_CARGO_CHANGED"} for event in raw_events
    )

    def push(event_type: str, timestamp: float, entity_refs: Dict[str, Any], payload: Dict[str, Any], *, durative: Dict[str, Any] | None = None, suffix: str = "a") -> None:
        converted.append(
            builder.build(
                event_id=f"mansim-{len(converted)+1:05d}-{suffix}",
                timestamp=timestamp,
                event_type=event_type,
                entity_refs=entity_refs,
                payload=payload,
                durative=durative,
            )
        )

    def push_output_buffer_state(timestamp: float, alias: str) -> None:
        push(
            "state_changed",
            timestamp,
            {"primary": alias},
            {"attributes": {"queue_size": output_buffer_counts[alias]}},
            suffix="o",
        )

    def repair_window_from_details(timestamp: float, details: Dict[str, Any]) -> Dict[str, float] | None:
        remaining = float(details.get("repair_remaining_min", repair_total_min) or repair_total_min)
        team_size = max(1, int(details.get("repair_team_size", 1) or 1))
        return {
            "started_at": timestamp,
            "ended_at": timestamp + max(0.0, remaining / team_size),
        }

    def repair_attributes(details: Dict[str, Any], timestamp: float) -> Dict[str, Any]:
        team = details.get("repair_team", [])
        if not isinstance(team, list):
            team = []
        team_ids = [str(member) for member in team if str(member).strip()]
        team_size = int(details.get("repair_team_size", len(team_ids)) or len(team_ids))
        remaining = float(details.get("repair_remaining_min", repair_total_min) or repair_total_min)
        total = float(details.get("repair_total_min", repair_total_min) or repair_total_min)
        return {
            "utilization": 0.0,
            "phase": "repair",
            "wait_visual": None,
            "wait_item_kind": None,
            "machine_state": "UNDER_REPAIR" if team_size > 0 else "BROKEN",
            "repair_team": team_ids,
            "repair_team_size": team_size,
            "repair_remaining_min": remaining,
            "repair_total_min": total,
            "repair_window": repair_window_from_details(timestamp, details) if team_size > 0 else None,
            "process_window": None,
        }

    def state_for_worker_state(worker_state: str) -> str:
        normalized = worker_state.strip().upper()
        if normalized == "MOVING":
            return "moving"
        if normalized in {"DISCHARGED"}:
            return "error"
        if normalized in {"IDLE"}:
            return "idle"
        if normalized in {"WAITING"}:
            return "waiting"
        if normalized in {"BATTERY_SWAPPING", "BATTERY_DELIVERING"}:
            return "charging"
        if normalized in {"REPAIRING_MACHINE", "PREVENTIVE_MAINTENANCE"}:
            return "maintenance"
        return "working"

    def canonical_motion_attributes(details: Dict[str, Any], event_index: int, worker_id: str) -> Dict[str, Any] | None:
        motion = details.get("motion")
        if not isinstance(motion, dict):
            return None
        path_positions = path_tiles_to_positions(layout, motion.get("path_tiles"))
        from_tile_position = tile_to_position(layout, motion.get("from_tile"))
        to_tile_position = tile_to_position(layout, motion.get("to_tile"))
        from_region = resolve_region_id(str(motion.get("from") or ""))
        to_region = resolve_region_id(str(motion.get("to") or ""))
        from_position = from_tile_position or positions.get(from_region or "")
        to_position = to_tile_position or positions.get(to_region or "")
        if not from_position or not to_position:
            return None
        started_at = float(motion.get("started_at", 0.0) or 0.0)
        planned_ended_at = float(motion.get("ended_at", started_at) or started_at)
        actual_ended_at = find_move_end_time(raw_events, event_index, worker_id, str(motion.get("to") or ""))
        ended_at = actual_ended_at if actual_ended_at is not None else planned_ended_at
        payload = {
            "from": from_position,
            "to": to_position,
            "started_at": started_at,
            "ended_at": ended_at,
        }
        if path_positions:
            payload["path"] = path_positions
        return payload

    def canonical_worker_attributes(details: Dict[str, Any], event_index: int, worker_id: str) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {}
        if "worker_state" in details:
            attrs["worker_state"] = details.get("worker_state")
        resolved_motion = canonical_motion_attributes(details, event_index, worker_id)
        if resolved_motion:
            attrs["motion"] = resolved_motion
        if "cargo" in details:
            attrs["cargo"] = details.get("cargo")
            cargo = details.get("cargo") if isinstance(details.get("cargo"), dict) else {}
            attrs["carrying_item_id"] = cargo.get("item_id")
            attrs["carrying_item_type"] = cargo.get("item_type")
        if "current_task_id" in details:
            attrs["active_task"] = details.get("current_task_id")
        if "current_task_type" in details:
            attrs["current_task_type"] = details.get("current_task_type")
        if "task_id" in details:
            attrs["active_task"] = details.get("task_id")
        if "task_id" in details or "current_task_id" in details:
            attrs["task_label"] = details.get("task_id") or details.get("current_task_id")
        if "battery_remaining_min" in details:
            remaining = float(details.get("battery_remaining_min") or 0.0)
            attrs["battery_period_min"] = battery_period_min
            attrs["battery_pct"] = max(0.0, min(100.0, 100.0 * remaining / max(1.0, battery_period_min)))
        return attrs

    for index, raw in enumerate(raw_events):
        raw_type = raw["type"]
        timestamp = float(raw["t"])
        entity_id = raw.get("entity_id")
        location = raw.get("location")
        details = raw.get("details", {})

        if raw_type == "WORKER_STATE_CHANGED" and entity_id:
            worker_state = str(details.get("worker_state", "IDLE"))
            region_id = resolve_region_id(location)
            tile_position = tile_to_position(layout, details.get("tile"))
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": state_for_worker_state(worker_state),
                    "position": tile_position
                    or worker_region_slot(region_id, entity_id, layout)
                    or positions.get(region_id or "", positions.get("warehouse_region")),
                    "attributes": canonical_worker_attributes(details, index, entity_id),
                },
            )
            continue

        if raw_type == "WORKER_CARGO_CHANGED" and entity_id:
            cargo = details.get("cargo") if isinstance(details.get("cargo"), dict) else {}
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "attributes": {
                        "cargo": cargo,
                        "carrying_item_id": cargo.get("item_id"),
                        "carrying_item_type": cargo.get("item_type"),
                    },
                },
            )
            continue

        if raw_type == "MACHINE_STATE_CHANGED" and entity_id:
            machine_state = str(details.get("machine_state", "WAIT_INPUT"))
            state = "idle"
            if machine_state in {"PROCESSING", "SETUP"}:
                state = "working"
            elif machine_state in {"DONE_WAIT_UNLOAD", "WAIT_INPUT"}:
                state = "waiting"
            elif machine_state == "BROKEN":
                state = "error"
            elif machine_state in {"UNDER_REPAIR", "UNDER_PM"}:
                state = "maintenance"
            attrs = {
                "machine_state": machine_state,
                "active_worker_ids": details.get("active_worker_ids", []),
                "repair_team_size": details.get("repair_team_size", 0),
                "repair_remaining_min": details.get("repair_remaining_min", 0.0),
                "input_item_id": details.get("input_item_id"),
                "output_item_id": details.get("output_item_id"),
            }
            push("state_changed", timestamp, {"primary": entity_id}, {"state": state, "attributes": attrs})
            continue

        if raw_type == "ITEM_STATE_CHANGED" and entity_id:
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id, "target": details.get("ref")},
                {
                    "state": str(details.get("item_state", "CREATED")).lower(),
                    "attributes": {
                        "item_state": details.get("item_state"),
                        "item_type": details.get("item_type"),
                        "ref": details.get("ref"),
                    },
                },
            )
            continue

        if raw_type == "AGENT_MOVE_START":
            if has_canonical_worker_events:
                continue
            source = resolve_region_id(details.get("from"))
            target = resolve_region_id(details.get("to"))
            path_positions = path_tiles_to_positions(layout, details.get("path_tiles"))
            from_position = tile_to_position(layout, details.get("from_tile")) or (positions.get(source) if source else None)
            to_position = tile_to_position(layout, details.get("to_tile")) or (positions.get(target) if target else None)
            if entity_id and source and target and from_position and to_position:
                end_time = find_move_end_time(raw_events, index, entity_id, details.get("to"))
                push(
                    "entity_moved",
                    timestamp,
                    {"primary": entity_id, "source": source, "target": target},
                    {
                        "from": from_position,
                        "to": to_position,
                        "path": path_positions,
                        "label": f"{details.get('from', '')} -> {details.get('to', '')}",
                    },
                    durative={
                        "started_at": timestamp,
                        "ended_at": end_time or (timestamp + float(details.get("duration", 0))),
                        "expected_duration": float(details.get("duration", 0)),
                    },
                )
            continue

        if raw_type == "AGENT_MOVE_END" and entity_id:
            if has_canonical_worker_events:
                continue
            current_task = active_tasks.get(entity_id)
            region_id = resolve_region_id(location)
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "working" if current_task else "idle",
                    "position": worker_region_slot(region_id, entity_id, layout) or positions.get(region_id or "", positions.get("warehouse_region")),
                },
            )
            continue

        if raw_type == "AGENT_TASK_START" and entity_id:
            if has_canonical_worker_events:
                continue
            task_id = details.get("task_id")
            target = task_target(details)
            label = task_label(details)
            task_end = task_end_by_id.get(task_id, {})
            duration = float(task_end.get("details", {}).get("duration", 0) or 0)
            end_time = timestamp + duration if duration > 0 else None
            active_tasks[entity_id] = {"task_id": task_id, "target": target, "label": label}
            push(
                "task_started",
                timestamp,
                {"primary": entity_id, "target": target},
                {
                    "task_id": task_id,
                    "task_label": label,
                    "attributes": {
                        "task_kind": details.get("task_type", ""),
                        "task_role": details.get("agent_role", ""),
                    },
                },
                durative={
                    "started_at": timestamp,
                    "ended_at": end_time or timestamp,
                    "expected_duration": duration,
                },
            )
            continue

        if raw_type == "AGENT_PICK_ITEM" and entity_id:
            if has_canonical_worker_events:
                continue
            item_type = details.get("item_type")
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "attributes": {
                        "carrying_item_id": details.get("item_id"),
                        "carrying_item_type": item_type,
                        "pose_hint": "carry" if item_type and str(item_type).lower() != "battery" else None,
                    }
                },
            )
            continue

        if raw_type == "AGENT_DROP_ITEM" and entity_id:
            if has_canonical_worker_events:
                continue
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "attributes": {
                        "carrying_item_id": None,
                        "carrying_item_type": None,
                        "pose_hint": None,
                    }
                },
            )
            continue

        if raw_type == "AGENT_TASK_END" and entity_id:
            if has_canonical_worker_events:
                continue
            task_id = details.get("task_id")
            active = active_tasks.pop(entity_id, None)
            push(
                "task_finished",
                timestamp,
                {"primary": entity_id, "target": (active or {}).get("target")},
                {
                    "task_id": task_id,
                    "task_label": (active or {}).get("label", task_label(details)),
                    "next_state": "idle",
                    "attributes": {"last_task_status": details.get("status", "completed")},
                },
            )
            continue

        if raw_type == "AGENT_DISCHARGED" and entity_id:
            if has_canonical_worker_events:
                continue
            push(
                "warning_raised",
                timestamp,
                {"primary": entity_id, "related": [resolve_region_id(location)] if resolve_region_id(location) else []},
                {"label": f"{entity_id} discharged", "severity": "error"},
            )
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "error",
                    "attributes": {
                        "battery_pct": 0,
                        "battery_period_min": battery_period_min,
                        "pose_hint": "discharged",
                        "task_label": "Battery Discharged",
                    },
                },
                suffix="b",
            )
            continue

        if raw_type == "AGENT_RECHARGED" and entity_id:
            if has_canonical_worker_events:
                continue
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {"state": "idle", "attributes": {"battery_pct": 100, "battery_period_min": battery_period_min, "last_swap_at": timestamp, "pose_hint": "idle"}},
            )
            continue

        if raw_type == "QUEUE_PUSH":
            queue_id = entity_id
            if queue_id in QUEUE_META:
                push(
                    "queue_entered",
                    timestamp,
                    {"primary": details.get("item_id"), "source": resolve_region_id(location), "target": queue_id},
                    {"item_id": details.get("item_id"), "queue_id": queue_id},
                )
            continue

        if raw_type == "QUEUE_POP":
            queue_id = entity_id
            if queue_id in QUEUE_META:
                push(
                    "queue_exited",
                    timestamp,
                    {"primary": details.get("item_id"), "source": queue_id, "target": resolve_region_id(location)},
                    {"item_id": details.get("item_id"), "queue_id": queue_id},
                )
            continue

        if raw_type == "ITEM_MOVED":
            source = details.get("from")
            target = details.get("to")
            source_alias = output_buffer_alias.get(source)
            target_alias = output_buffer_alias.get(target)
            if source_alias:
                output_buffer_counts[source_alias] = max(0, output_buffer_counts[source_alias] - 1)
                push_output_buffer_state(timestamp, source_alias)
            if target_alias:
                output_buffer_counts[target_alias] += 1
                push_output_buffer_state(timestamp, target_alias)
            if source_alias or target_alias:
                continue

        if raw_type == "MACHINE_START" and entity_id:
            end_time = find_machine_phase_end_time(raw_events, index, entity_id, raw_type)
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "working",
                    "attributes": {
                        "utilization": 0.92,
                        "phase": "process",
                        "wait_visual": None,
                        "wait_item_kind": None,
                        "machine_state": "PROCESSING",
                        "process_window": {
                            "started_at": timestamp,
                            "ended_at": end_time or timestamp,
                        },
                    },
                },
            )
            continue

        if raw_type == "MACHINE_END" and entity_id:
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "waiting",
                    "attributes": {
                        "utilization": 0.0,
                        "process_window": None,
                        "machine_state": "DONE_WAIT_UNLOAD",
                        "wait_visual": "completed_output",
                        "wait_item_kind": machine_output_item_kind(entity_id),
                    },
                },
            )
            continue

        if raw_type == "MACHINE_ABORTED" and entity_id:
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "blocked",
                    "attributes": {
                        "utilization": 0.0,
                        "process_window": None,
                        "machine_state": "BLOCKED",
                        "wait_visual": None,
                        "wait_item_kind": None,
                    },
                },
            )
            continue

        if raw_type == "MACHINE_SETUP_START" and entity_id:
            end_time = find_machine_phase_end_time(raw_events, index, entity_id, raw_type)
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "working",
                    "attributes": {
                        "utilization": 0.35,
                        "phase": "setup",
                        "wait_visual": None,
                        "wait_item_kind": None,
                        "machine_state": "SETUP",
                        "process_window": {
                            "started_at": timestamp,
                            "ended_at": end_time or timestamp,
                        },
                    },
                },
            )
            continue

        if raw_type == "MACHINE_SETUP_END" and entity_id:
            outcome = details.get("outcome")
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "waiting" if outcome == "missing_material" else "idle",
                    "attributes": {
                        "utilization": 0.0,
                        "phase": "ready",
                        "setup_outcome": outcome,
                        "process_window": None,
                        "machine_state": "WAIT_INPUT" if outcome == "missing_material" else "IDLE",
                        "wait_visual": "prep_wait" if outcome == "missing_material" else None,
                        "wait_item_kind": machine_prep_item_kind(entity_id) if outcome == "missing_material" else None,
                    },
                },
            )
            continue

        if raw_type == "MACHINE_BROKEN" and entity_id:
            related = [resolve_region_id(location)] if resolve_region_id(location) else []
            push("warning_raised", timestamp, {"primary": entity_id, "related": related}, {"label": f"{entity_id} fault", "severity": "error"})
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "error",
                    "attributes": {
                        "utilization": 0.0,
                        "process_window": None,
                        "machine_state": "BROKEN",
                        "repair_team": [],
                        "repair_team_size": 0,
                        "repair_remaining_min": repair_total_min,
                        "repair_total_min": repair_total_min,
                        "repair_window": None,
                        "wait_visual": None,
                        "wait_item_kind": None,
                    },
                },
                suffix="b",
            )
            continue

        if raw_type == "MACHINE_REPAIR_START" and entity_id:
            push("maintenance_started", timestamp, {"primary": entity_id, "target": details.get("by")}, {"label": f"Repair {entity_id}"})
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "maintenance",
                    "attributes": repair_attributes(details, timestamp),
                },
                suffix="b",
            )
            continue

        if raw_type == "MACHINE_REPAIR_HELPER_JOIN" and entity_id:
            helper_id = details.get("by")
            if helper_id:
                push(
                    "collaboration_started",
                    timestamp,
                    {"primary": entity_id, "source": entity_id, "target": helper_id},
                    {"label": f"{entity_id} helper join"},
                )
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "maintenance",
                    "attributes": repair_attributes(details, timestamp),
                },
                suffix="b",
            )
            continue

        if raw_type == "MACHINE_REPAIR_HELPER_LEAVE" and entity_id:
            helper_id = details.get("by")
            if helper_id:
                push(
                    "collaboration_finished",
                    timestamp,
                    {"primary": entity_id, "source": entity_id, "target": helper_id},
                    {"label": f"{entity_id} helper leave"},
                )
            remaining_team = int(details.get("repair_team_size", 0) or 0)
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "maintenance" if remaining_team > 0 else "error",
                    "attributes": repair_attributes(details, timestamp),
                },
                suffix="b",
            )
            continue

        if raw_type == "MACHINE_REPAIRED" and entity_id:
            push("maintenance_finished", timestamp, {"primary": entity_id}, {"label": f"Repair complete {entity_id}"})
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "waiting",
                    "attributes": {
                        "utilization": 0.0,
                        "process_window": None,
                        "repair_team": [],
                        "repair_team_size": 0,
                        "repair_remaining_min": 0.0,
                        "repair_total_min": float(details.get("repair_total_min", repair_total_min) or repair_total_min),
                        "repair_window": None,
                        "machine_state": "WAIT_INPUT",
                        "wait_visual": None,
                        "wait_item_kind": None,
                    },
                },
                suffix="b",
            )
            continue

        if raw_type == "MACHINE_PM_START" and entity_id:
            end_time = find_machine_phase_end_time(raw_events, index, entity_id, raw_type)
            push("maintenance_started", timestamp, {"primary": entity_id}, {"label": f"PM {entity_id}"})
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "maintenance",
                    "attributes": {
                        "utilization": 0.0,
                        "phase": "pm",
                        "wait_visual": None,
                        "wait_item_kind": None,
                        "machine_state": "UNDER_PM",
                        "process_window": {"started_at": timestamp, "ended_at": end_time or timestamp},
                    },
                },
                suffix="b",
            )
            continue

        if raw_type == "MACHINE_PM_END" and entity_id:
            push("maintenance_finished", timestamp, {"primary": entity_id}, {"label": f"PM done {entity_id}"})
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "state": "idle",
                    "attributes": {
                        "utilization": 0.0,
                        "process_window": None,
                        "machine_state": "IDLE",
                        "wait_visual": None,
                        "wait_item_kind": None,
                    },
                },
                suffix="b",
            )
            continue

        if raw_type == "BATTERY_SWAP" and entity_id:
            push(
                "charging_finished",
                timestamp,
                {"primary": entity_id, "source": "battery_rack", "target": entity_id},
                {"battery_pct": 100, "attributes": {"battery_pct": 100, "battery_period_min": battery_period_min, "last_swap_at": timestamp}},
            )
            continue

        if raw_type == "INSPECT_PASS":
            inspector = details.get("inspector")
            push("message_sent", timestamp, {"source": inspector, "target": "intermediate_queue_4", "related": [entity_id] if entity_id else []}, {"message": "PASS"})
            continue

        if raw_type == "COMPLETED_PRODUCT":
            completed_count += 1
            push("message_sent", timestamp, {"source": "inspection_region", "target": "warehouse_buffer", "related": [entity_id] if entity_id else []}, {"message": "COMPLETED"})
            push("state_changed", timestamp, {"primary": "warehouse_buffer"}, {"attributes": {"completed_count": completed_count}}, suffix="b")
            continue

        if raw_type == "INCIDENT_EVENT":
            incident_class = str(details.get("incident_class", "warning"))
            affected = details.get("affected_entities", [])
            primary = None
            related: List[str] = []
            for candidate in affected:
                if candidate in REGION_ID.values():
                    related.append(candidate)
                elif candidate in REGION_ID:
                    related.append(REGION_ID[candidate])
                elif isinstance(candidate, str) and candidate.startswith("S"):
                    primary = candidate
                elif isinstance(candidate, str) and candidate.startswith("Station"):
                    region_id = resolve_region_id(candidate)
                    if region_id:
                        related.append(region_id)
            primary = primary or (related[0] if related else None)
            event_type = "error_raised" if incident_severity(incident_class) == "error" else "warning_raised"
            push(event_type, timestamp, {"primary": primary, "related": [item for item in related if item and item != primary]}, {"label": incident_class.replace("_", " ")})
            continue

    return converted


def load_scenario_runtime(run_dir: Path) -> Dict[str, float]:
    runtime = {
        "battery_period_min": 200.0,
        "repair_time_min": 20.0,
    }
    config_path = run_dir / ".hydra" / "config.yaml"
    if config_path.exists():
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            scenario_cfg = config.get("scenario", {}) if isinstance(config.get("scenario", {}), dict) else {}
            agent_cfg = scenario_cfg.get("agent", {}) if isinstance(scenario_cfg.get("agent", {}), dict) else {}
            machine_failure_cfg = scenario_cfg.get("machine_failure", {}) if isinstance(scenario_cfg.get("machine_failure", {}), dict) else {}
            battery_period = agent_cfg.get("battery_swap_period_min")
            repair_time = machine_failure_cfg.get("repair_time_min")
            if battery_period is not None:
                runtime["battery_period_min"] = float(battery_period)
            if repair_time is not None:
                runtime["repair_time_min"] = float(repair_time)
        except Exception:
            return runtime
    return runtime


def load_scenario_config(run_dir: Path) -> Dict[str, Any]:
    config_path = run_dir / ".hydra" / "config.yaml"
    if not config_path.exists():
        return {}
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    scenario_cfg = config.get("scenario", {}) if isinstance(config.get("scenario", {}), dict) else {}
    return scenario_cfg


def export_run(run_dir: Path, output_log: Path, output_layout: Path) -> None:
    raw_events = load_jsonl(run_dir / "events.jsonl")
    run_meta = load_json(run_dir / "run_meta.json")
    worker_ids = parse_worker_ids(run_meta, raw_events)
    machine_ids = parse_machine_ids(raw_events)
    scenario_cfg = load_scenario_config(run_dir)
    runtime_cfg = load_scenario_runtime(run_dir)
    battery_period_min = float(runtime_cfg.get("battery_period_min", 200.0))
    repair_total_min = float(runtime_cfg.get("repair_time_min", 20.0))
    layout = build_layout(worker_ids, scenario_cfg)
    converted_events = convert_events(raw_events, layout, battery_period_min, repair_total_min)

    replay_log = {
        "schema_version": "1.0",
        "metadata": {
            "run_id": str(run_dir.name),
            "title": f"ManSim Existing Run {run_dir.name}",
            "domain": "manufacturing",
            "description": f"Existing ManSim manufacturing run reconstructed from events.jsonl with {len(worker_ids)} workers and {len(machine_ids)} machines.",
            "created_at": run_meta.get("started_at_utc"),
            "total_duration": float(run_meta.get("sim_total_min", run_meta.get("total_days", 0) * run_meta.get("minutes_per_day", 0))),
            "time_unit": "minutes",
        },
        "layout": layout,
        "initial_state": build_initial_state(worker_ids, machine_ids, layout, battery_period_min, repair_total_min),
        "events": converted_events,
    }

    output_log.parent.mkdir(parents=True, exist_ok=True)
    output_layout.parent.mkdir(parents=True, exist_ok=True)
    output_log.write_text(json.dumps(replay_log, indent=2), encoding="utf-8")
    output_layout.write_text(json.dumps(layout, indent=2), encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Convert an existing ManSim output run into replay_studio JSON.")
    parser.add_argument("--run-dir", required=True, help="Path to a ManSim output run directory containing events.jsonl and run_meta.json")
    parser.add_argument("--output-log", required=True, help="Destination replay JSON file")
    parser.add_argument("--output-layout", required=True, help="Destination layout JSON file")
    args = parser.parse_args(list(argv) if argv is not None else None)

    export_run(Path(args.run_dir), Path(args.output_log), Path(args.output_layout))


if __name__ == "__main__":
    main()
