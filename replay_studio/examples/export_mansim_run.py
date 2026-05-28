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
    "CompletedProducts": "completed_products_region",
    "ScrapDisposal": "scrap_disposal_region",
}

QUEUE_META = {
    "material_queue_1": ("queue", "S1 Material Queue"),
    "material_queue_2": ("queue", "S2 Material Queue"),
    "intermediate_queue_2": ("buffer", "S2 Intermediate Queue"),
    "intermediate_queue_4": ("queue", "Inspection Queue"),
    "completed_product_buffer": ("buffer", "Completed Products"),
    "warehouse_buffer": ("buffer", "Completed Buffer"),
    "inspection_scrap_queue": ("queue", "Inspection Scrap Queue"),
    "scrap_disposal_bin": ("buffer", "Scrap Disposal"),
    "warehouse_material_shelf": ("shelf", "Warehouse Material Shelf"),
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
        {"entity_id": "intermediate_queue_4", "entity_type": "queue", "region_id": "inspection_region", "anchor": {"x": 0.20, "y": 0.32}},
        {"entity_id": "inspection_output_queue", "entity_type": "buffer", "region_id": "inspection_region", "anchor": {"x": 0.82, "y": 0.30}},
        {"entity_id": "inspection_scrap_queue", "entity_type": "queue", "region_id": "inspection_region", "anchor": {"x": 0.82, "y": 0.60}},
    ],
}

ROLLING_HORIZON_REPLAY_TYPES = {
    "ROLLING_HORIZON_WINDOW_START": "rolling_horizon_window_started",
    "ROLLING_HORIZON_CANDIDATE_COLLECTED": "rolling_horizon_candidate_collected",
    "ROLLING_HORIZON_DISPATCH": "rolling_horizon_dispatched",
    "ROLLING_HORIZON_TASK_SKIPPED": "rolling_horizon_task_skipped",
    "ROLLING_HORIZON_TASK_REQUEUED": "rolling_horizon_task_requeued",
}

QUEUE_ITEM_TYPE = {
    "material_queue_1": "material",
    "material_queue_2": "material",
    "intermediate_queue_2": "intermediate",
    "intermediate_queue_4": "product",
    "station_1_output_queue": "intermediate",
    "station_2_output_queue": "product",
    "inspection_output_queue": "product",
    "inspection_scrap_queue": "scrap",
    "completed_product_buffer": "product",
    "warehouse_buffer": "product",
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


def initial_warehouse_material_state(raw_events: List[Dict[str, Any]]) -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    shelf_attributes = {"shelf_count": 0, "shelf_capacity": 0}
    slot_attributes: Dict[str, Dict[str, Any]] = {}
    for raw in raw_events:
        if raw.get("type") != "WAREHOUSE_MATERIAL_RESTOCK":
            continue
        details = raw.get("details", {}) if isinstance(raw.get("details", {}), dict) else {}
        if details.get("reason") != "initial_fill":
            continue
        shelf_attributes = {
            "shelf_count": details.get("shelf_count", 0),
            "shelf_capacity": details.get("shelf_capacity", 0),
            "restocked_count": details.get("restocked_count", 0),
        }
        for slot in details.get("slots", []) or []:
            if not isinstance(slot, dict):
                continue
            slot_id = str(slot.get("slot_id", "") or "")
            if not slot_id:
                continue
            slot_attributes[slot_id] = {
                "occupied": True,
                "material_item_id": slot.get("item_id"),
                "item_type": "material",
            }
        break
    return shelf_attributes, slot_attributes


def build_initial_state(
    worker_ids: List[str],
    machine_ids: List[str],
    layout: Dict[str, Any],
    battery_period_min: float,
    repair_total_min: float,
    initial_shelf_attributes: Dict[str, Any] | None = None,
    initial_material_slots: Dict[str, Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    positions = layout_positions(layout)
    entities: Dict[str, Dict[str, Any]] = {}
    material_slot_attrs = initial_material_slots or {}

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
        if queue_id in QUEUE_ITEM_TYPE:
            attributes["item_type"] = QUEUE_ITEM_TYPE[queue_id]
        if queue_id in {"completed_product_buffer", "warehouse_buffer"}:
            attributes = {"completed_count": 0, "item_type": QUEUE_ITEM_TYPE.get(queue_id, "product")}
        if queue_id == "inspection_scrap_queue":
            attributes = {"queue_size": 0, "queue_kind": "scrap", "item_type": "scrap"}
        if queue_id == "scrap_disposal_bin":
            attributes = {"disposed_scrap_count": 0}
        if queue_id == "warehouse_material_shelf":
            attributes = dict(initial_shelf_attributes or {"shelf_count": 0, "shelf_capacity": 0})
        add_entity(queue_id, entity_type, label, "waiting", attributes=attributes)

    for node in layout.get("nodes", []):
        if not isinstance(node, dict):
            continue
        entity_id = str(node.get("entity_id", "") or "")
        entity_type = str(node.get("entity_type", "") or "")
        if entity_id.startswith("warehouse_material_slot_") and entity_id not in entities:
            add_entity(
                entity_id,
                "material_slot",
                entity_id.replace("warehouse_material_slot_", "Material Slot "),
                "waiting",
                attributes=dict(
                    material_slot_attrs.get(
                        entity_id,
                        {
                            "occupied": False,
                            "material_item_id": None,
                            "item_type": "material",
                        },
                    )
                ),
            )

    for queue_id, (entity_type, label, source_ref) in OUTPUT_QUEUE_META.items():
        add_entity(
            queue_id,
            entity_type,
            label,
            "waiting",
            attributes={
                "queue_size": 0,
                "queue_kind": "output",
                "source_ref": source_ref,
                "item_type": QUEUE_ITEM_TYPE.get(queue_id, "product"),
            },
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
        humanoid_state = {
            "humanoid_id": worker_id,
            "availability": "AVAILABLE",
            "mobility": "STATIONARY",
            "power": "POWER_NORMAL",
            "manipulation": "FREE",
            "task_context": None,
            "reason": None,
            "timestamp_s": 0,
            "metadata": {},
        }
        add_entity(
            worker_id,
            "worker",
            f"Worker {worker_id}",
            "idle",
            attributes={
                "battery_pct": 100,
                "battery_period_min": battery_period_min,
                "last_swap_at": 0,
                "humanoid_state": humanoid_state,
            },
        )

    queues = {
        queue_id: {"queue_id": queue_id, "item_ids": [], "updated_at": 0}
        for queue_id in (
            "material_queue_1",
            "material_queue_2",
            "intermediate_queue_2",
            "intermediate_queue_4",
            "inspection_scrap_queue",
            "completed_product_buffer",
            "warehouse_buffer",
        )
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


MIN_VISIBLE_TASK_WINDOW_MIN = 0.1


def humanoid_task_key(worker_id: str, details: Dict[str, Any]) -> tuple[Any, ...]:
    return (
        worker_id,
        details.get("task_id"),
        details.get("instance_id"),
    )


def build_humanoid_task_window_index(raw_events: List[Dict[str, Any]]) -> Dict[tuple[Any, ...], Dict[str, float]]:
    start_index: Dict[tuple[Any, ...], List[float]] = {}
    end_index: Dict[tuple[Any, ...], List[float]] = {}
    for event in raw_events:
        event_type = event.get("type")
        if event_type not in {"HUMANOID_TASK_START", "HUMANOID_TASK_END"}:
            continue
        worker_id = event.get("entity_id")
        details = event.get("details", {})
        if not isinstance(worker_id, str) or not isinstance(details, dict):
            continue
        key = humanoid_task_key(worker_id, details)
        if event_type == "HUMANOID_TASK_START":
            start_index.setdefault(key, []).append(float(event["t"]))
        else:
            end_index.setdefault(key, []).append(float(event["t"]))
    windows: Dict[tuple[Any, ...], Dict[str, float]] = {}
    for key, starts in start_index.items():
        sorted_ends = sorted(end_index.get(key, []))
        for started_at in sorted(starts):
            ended_at = next((candidate for candidate in sorted_ends if candidate >= started_at), None)
            if ended_at is None or ended_at <= started_at:
                ended_at = started_at + MIN_VISIBLE_TASK_WINDOW_MIN
            windows[key] = {"started_at": started_at, "ended_at": ended_at}
    return windows


def find_humanoid_task_window(
    task_window_by_key: Dict[tuple[Any, ...], Dict[str, float]],
    worker_id: str,
    timestamp: float,
    details: Dict[str, Any],
) -> Dict[str, float] | None:
    window = task_window_by_key.get(humanoid_task_key(worker_id, details))
    if not window:
        return None
    ended_at = float(window.get("ended_at", timestamp) or timestamp)
    if ended_at < timestamp:
        return None
    return window


def humanoid_task_window(
    task_window_by_key: Dict[tuple[Any, ...], Dict[str, float]],
    worker_id: str,
    timestamp: float,
    details: Dict[str, Any],
) -> Dict[str, Any]:
    indexed_window = find_humanoid_task_window(task_window_by_key, worker_id, timestamp, details)
    started_at = float(indexed_window.get("started_at", timestamp) if indexed_window else timestamp)
    ended_at = float(indexed_window.get("ended_at", timestamp) if indexed_window else timestamp)
    if ended_at <= timestamp:
        ended_at = timestamp + MIN_VISIBLE_TASK_WINDOW_MIN
    return {
        "started_at": started_at,
        "ended_at": ended_at,
        "task_id": details.get("task_id"),
        "task_code": details.get("task_code"),
    }


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


def convert_events(
    raw_events: List[Dict[str, Any]],
    layout: Dict[str, Any],
    battery_period_min: float,
    repair_total_min: float,
) -> List[Dict[str, Any]]:
    builder = ReplayEventBuilder()
    task_end_by_id = build_task_end_index(raw_events)
    task_window_by_key = build_humanoid_task_window_index(raw_events)
    positions = layout_positions(layout)
    active_tasks: Dict[str, Dict[str, Any]] = {}
    active_move_paths: Dict[str, List[Dict[str, float]]] = {}
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

    def canonical_motion_attributes(details: Dict[str, Any], event_index: int, worker_id: str) -> Dict[str, Any] | None:
        motion = details.get("motion")
        if not isinstance(motion, dict):
            return None
        path_positions = path_tiles_to_positions(layout, motion.get("path_tiles"))
        from_tile_position = tile_to_position(layout, motion.get("from_tile"))
        to_tile_position = tile_to_position(layout, motion.get("to_tile"))
        humanoid_state = details.get("humanoid_state")
        availability = ""
        if isinstance(humanoid_state, dict):
            availability = str(humanoid_state.get("availability") or "").strip().upper()
        non_motion_availability = {"AVAILABLE", "ASSIGNED", "WAITING", "BLOCKED", "OFFLINE", "DISABLED"}
        paused = (
            str(details.get("observation_reason", "") or "").strip() == "path_wait"
            or bool(motion.get("paused", False))
            or availability in non_motion_availability
        )
        from_position = from_tile_position
        to_position = to_tile_position
        if paused:
            # Path-wait and incident/block observations preserve the planned
            # route for context, but the worker must stay fixed on its current
            # tile until a fresh AGENT_MOVE_START resumes motion.
            hold_position = tile_to_position(layout, details.get("tile")) or from_tile_position
            from_position = hold_position
            to_position = hold_position
        if not from_position or not to_position:
            return None
        started_at = float(motion.get("started_at", 0.0) or 0.0)
        ended_at = float(motion.get("ended_at", started_at) or started_at)
        payload = {
            "from": from_position,
            "to": to_position,
            "started_at": started_at,
            "ended_at": ended_at,
        }
        if paused:
            payload["paused"] = True
        if path_positions:
            payload["path"] = [from_position, to_position] if paused else path_positions
            payload["display_path"] = path_positions
        return payload

    def canonical_worker_attributes(details: Dict[str, Any], event_index: int, worker_id: str) -> Dict[str, Any]:
        attrs: Dict[str, Any] = {}
        humanoid_state = details.get("humanoid_state")
        has_active_task_context = False
        if isinstance(humanoid_state, dict):
            attrs["humanoid_state"] = humanoid_state
            task_context = humanoid_state.get("task_context")
            if isinstance(task_context, dict):
                has_active_task_context = bool(str(task_context.get("task_code") or "").strip())
                attrs["current_task_code"] = task_context.get("task_code") or attrs.get("current_task_code")
                attrs["current_task_instance_id"] = task_context.get("task_instance_id") or attrs.get("current_task_instance_id")
                attrs["current_step_id"] = task_context.get("step_id") or attrs.get("current_step_id")
                attrs["current_primitive_call_code"] = task_context.get("primitive_call_code") or attrs.get("current_primitive_call_code")
                attrs["current_execution_status"] = task_context.get("execution_status")
            reason = humanoid_state.get("reason")
            if isinstance(reason, dict):
                attrs["state_reason_code"] = reason.get("code")
                attrs["state_reason_message"] = reason.get("message")
                reason_meta = reason.get("metadata") if isinstance(reason.get("metadata"), dict) else {}
                recovery_context = details.get("recovery_context") if isinstance(details.get("recovery_context"), dict) else {}
                recovery_incident_code = recovery_context.get("incident_code") if isinstance(recovery_context, dict) else None
                incident_code = reason_meta.get("incident_code") or recovery_incident_code
                if incident_code:
                    incident_payload = {
                        "code": incident_code,
                        "category": reason_meta.get("incident_category"),
                        "severity": reason_meta.get("incident_severity") or reason_meta.get("severity"),
                        "description": reason.get("message"),
                        "recovery_protocol": reason_meta.get("recovery_protocol", []),
                        "primitive_call_code": task_context.get("primitive_call_code") if isinstance(task_context, dict) else None,
                        "task_code": task_context.get("task_code") if isinstance(task_context, dict) else None,
                    }
                    attrs["last_humanoid_incident"] = incident_payload
                    attrs["incident_bubble"] = incident_payload
                else:
                    attrs["incident_bubble"] = None
                    attrs["last_humanoid_incident"] = None
            else:
                attrs["incident_bubble"] = None
                attrs["last_humanoid_incident"] = None
        resolved_motion = canonical_motion_attributes(details, event_index, worker_id)
        if resolved_motion:
            attrs["motion"] = resolved_motion
        if "cargo" in details:
            attrs["cargo"] = details.get("cargo")
            cargo = details.get("cargo") if isinstance(details.get("cargo"), dict) else {}
            attrs["carrying_item_id"] = cargo.get("item_id")
            attrs["carrying_item_type"] = cargo.get("item_type")
        if "current_task_id" in details and has_active_task_context:
            attrs["active_task"] = details.get("current_task_id")
        if "current_task_type" in details and has_active_task_context:
            attrs["current_task_type"] = details.get("current_task_type")
        if "current_task_code" in details and has_active_task_context:
            attrs["current_task_code"] = details.get("current_task_code")
        if "current_task_instance_id" in details and has_active_task_context:
            attrs["current_task_instance_id"] = details.get("current_task_instance_id")
        if "current_step_id" in details and has_active_task_context:
            attrs["current_step_id"] = details.get("current_step_id")
        if "current_primitive_call_code" in details and has_active_task_context:
            attrs["current_primitive_call_code"] = details.get("current_primitive_call_code")
        if "task_code" in details and has_active_task_context:
            attrs["current_task_code"] = details.get("task_code")
        if "task_name" in details and has_active_task_context:
            attrs["current_task_name"] = details.get("task_name")
        if "instance_id" in details and has_active_task_context:
            attrs["current_task_instance_id"] = details.get("instance_id")
        if "parent_task_code" in details:
            attrs["current_parent_task_code"] = details.get("parent_task_code")
        if "parent_instance_id" in details:
            attrs["current_parent_task_instance_id"] = details.get("parent_instance_id")
        if "child_task_code" in details:
            attrs["current_child_task_code"] = details.get("child_task_code")
        if "child_task_name" in details:
            attrs["current_child_task_name"] = details.get("child_task_name")
        if "child_instance_id" in details:
            attrs["current_child_task_instance_id"] = details.get("child_instance_id")
        if "task_path" in details:
            attrs["current_task_path"] = details.get("task_path")
        if "depth" in details:
            attrs["current_task_depth"] = details.get("depth")
        if "step_id" in details and has_active_task_context:
            attrs["current_step_id"] = details.get("step_id")
        if "primitive_call_code" in details and has_active_task_context:
            attrs["current_primitive_call_code"] = details.get("primitive_call_code")
        if "recovery_context" in details:
            attrs["current_recovery_context"] = details.get("recovery_context")
        if "task_id" in details and has_active_task_context:
            attrs["active_task"] = details.get("task_id")
        if ("task_id" in details or "current_task_id" in details) and has_active_task_context:
            attrs["task_label"] = details.get("task_id") or details.get("current_task_id")
        if "battery_remaining_min" in details:
            remaining = float(details.get("battery_remaining_min") or 0.0)
            attrs["battery_period_min"] = battery_period_min
            attrs["battery_pct"] = max(0.0, min(100.0, 100.0 * remaining / max(1.0, battery_period_min)))
        return attrs

    def conflict_position_payload(details: Dict[str, Any]) -> Dict[str, Any]:
        payload: Dict[str, Any] = {}
        tile_position = tile_to_position(layout, details.get("tile"))
        if tile_position is not None:
            payload["tile_position"] = tile_position
        edge = details.get("edge") if isinstance(details.get("edge"), dict) else {}
        edge_from = tile_to_position(layout, edge.get("from") if isinstance(edge, dict) else None)
        edge_to = tile_to_position(layout, edge.get("to") if isinstance(edge, dict) else None)
        if edge_from is not None and edge_to is not None:
            payload["edge_from_position"] = edge_from
            payload["edge_to_position"] = edge_to
        other_edge = details.get("other_edge") if isinstance(details.get("other_edge"), dict) else {}
        other_edge_from = tile_to_position(layout, other_edge.get("from") if isinstance(other_edge, dict) else None)
        other_edge_to = tile_to_position(layout, other_edge.get("to") if isinstance(other_edge, dict) else None)
        if other_edge_from is not None and other_edge_to is not None:
            payload["other_edge_from_position"] = other_edge_from
            payload["other_edge_to_position"] = other_edge_to
        return payload

    for index, raw in enumerate(raw_events):
        raw_type = raw["type"]
        timestamp = float(raw["t"])
        entity_id = raw.get("entity_id")
        location = raw.get("location")
        details = raw.get("details", {})

        if raw_type in ROLLING_HORIZON_REPLAY_TYPES:
            rolling_details = details if isinstance(details, dict) else {}
            if raw_type != "ROLLING_HORIZON_WINDOW_START":
                concrete_task_code = str(rolling_details.get("task_code") or rolling_details.get("task_type") or "").strip()
                concrete_opportunity_id = str(rolling_details.get("opportunity_id") or entity_id or "").strip()
                if not concrete_task_code or not concrete_opportunity_id.startswith("RHOPP-"):
                    # Window-level dispatch summaries are useful in the core log,
                    # but the replay task pool expects concrete task opportunities.
                    # Exporting summaries as task rows creates blank table entries.
                    continue
            worker_id = rolling_details.get("worker_id") or rolling_details.get("assigned_worker_id")
            entity_refs: Dict[str, Any] = {"primary": str(entity_id or rolling_details.get("opportunity_id") or raw_type)}
            if worker_id:
                entity_refs["related"] = [str(worker_id)]
                entity_refs["target"] = str(worker_id)
            payload = {
                "core_event_type": raw_type,
                "location": location,
                **rolling_details,
            }
            push(
                ROLLING_HORIZON_REPLAY_TYPES[raw_type],
                timestamp,
                entity_refs,
                payload,
                suffix="rh",
            )
            continue

        if raw_type == "WORKER_STATE_CHANGED" and entity_id:
            tile_position = tile_to_position(layout, details.get("tile"))
            payload: Dict[str, Any] = {
                "attributes": canonical_worker_attributes(details, index, entity_id),
            }
            if tile_position is not None:
                payload["position"] = tile_position
                payload["attributes"]["position_source"] = "simulation_tile"
            else:
                payload["attributes"]["position_source"] = "missing"
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                payload,
            )
            continue

        if raw_type == "WORKER_CARGO_CHANGED" and entity_id:
            cargo = details.get("cargo") if isinstance(details.get("cargo"), dict) else {}
            attrs = canonical_worker_attributes(details, index, entity_id)
            attrs.update(
                {
                    "cargo": cargo,
                    "carrying_item_id": cargo.get("item_id"),
                    "carrying_item_type": cargo.get("item_type"),
                }
            )
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {"attributes": attrs},
            )
            continue

        if raw_type == "HUMANOID_TASK_START" and entity_id:
            attrs = canonical_worker_attributes(details, index, entity_id)
            attrs["task_window"] = humanoid_task_window(task_window_by_key, entity_id, timestamp, details)
            push("state_changed", timestamp, {"primary": entity_id}, {"attributes": attrs})
            continue

        if raw_type == "HUMANOID_TASK_END" and entity_id:
            attrs = canonical_worker_attributes(details, index, entity_id)
            if details.get("parent_task_code"):
                parent_window_details = {
                    "task_id": details.get("parent_task_id"),
                    "instance_id": details.get("parent_instance_id"),
                    "task_code": details.get("parent_task_code"),
                }
                attrs.update(
                    {
                        "current_task_code": details.get("parent_task_code"),
                        "current_task_instance_id": details.get("parent_instance_id"),
                        "current_child_task_code": None,
                        "current_child_task_name": None,
                        "current_child_task_instance_id": None,
                        "current_task_path": None,
                        "current_task_depth": 0,
                        "task_window": humanoid_task_window(task_window_by_key, entity_id, timestamp, parent_window_details),
                    }
                )
            task_context = (attrs.get("humanoid_state") or {}).get("task_context") if isinstance(attrs.get("humanoid_state"), dict) else None
            if not details.get("parent_task_code") and (not isinstance(task_context, dict) or not str(task_context.get("task_code") or "").strip()):
                attrs.update(
                    {
                        "active_task": None,
                        "task_label": None,
                        "current_task_type": None,
                        "current_task_code": None,
                        "current_task_name": None,
                        "current_task_instance_id": None,
                        "current_parent_task_code": None,
                        "current_parent_task_instance_id": None,
                        "current_child_task_code": None,
                        "current_child_task_name": None,
                        "current_child_task_instance_id": None,
                        "current_task_path": None,
                        "current_task_depth": None,
                        "current_execution_status": None,
                    }
                )
            attrs.update(
                {
                    "current_step_id": "",
                    "current_primitive_call_code": "",
                }
            )
            if not details.get("parent_task_code"):
                attrs["task_window"] = None
            push("state_changed", timestamp, {"primary": entity_id}, {"attributes": attrs})
            continue

        if raw_type == "HUMANOID_STEP_START" and entity_id:
            attrs = canonical_worker_attributes(details, index, entity_id)
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                {
                    "attributes": attrs,
                },
            )
            continue

        if raw_type == "HUMANOID_STEP_END" and entity_id:
            attrs = canonical_worker_attributes(details, index, entity_id)
            attrs.update(
                {
                    "current_step_id": "",
                    "current_primitive_call_code": "",
                    "last_step_status": details.get("status", ""),
                    "last_step_id": details.get("step_id", ""),
                    "last_primitive_call_code": details.get("primitive_call_code", ""),
                }
            )
            push("state_changed", timestamp, {"primary": entity_id}, {"attributes": attrs})
            continue

        if raw_type == "HUMANOID_INCIDENT" and entity_id:
            attrs = canonical_worker_attributes(details, index, entity_id)
            incident_payload = {
                "code": details.get("incident_code"),
                "category": details.get("incident_category"),
                "severity": details.get("incident_severity"),
                "description": details.get("description"),
                "recovery_protocol": details.get("recovery_protocol", []),
                "primitive_call_code": details.get("primitive_call_code"),
                "task_code": details.get("task_code"),
            }
            attrs["last_humanoid_incident"] = incident_payload
            attrs["incident_bubble"] = incident_payload
            push("state_changed", timestamp, {"primary": entity_id}, {"attributes": attrs})
            event_type = "error_raised" if str(details.get("incident_severity", "")).lower() in {"error", "critical"} else "warning_raised"
            push(
                event_type,
                timestamp,
                {"primary": entity_id},
                {
                    "label": str(details.get("incident_code", "incident")).replace("_", " "),
                    "incident": incident_payload,
                },
                suffix="incident",
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
                "wait_visual": "completed_output" if machine_state == "DONE_WAIT_UNLOAD" else None,
                "wait_item_kind": machine_output_item_kind(entity_id) if machine_state == "DONE_WAIT_UNLOAD" else None,
            }
            push("state_changed", timestamp, {"primary": entity_id}, {"state": state, "attributes": attrs})
            continue

        if raw_type == "ITEM_STATE_CHANGED" and entity_id:
            item_state = str(details.get("item_state", "CREATED") or "CREATED").upper()
            item_tile = details.get("tile") if isinstance(details.get("tile"), dict) else None
            item_position = tile_to_position(layout, item_tile)
            item_attributes = {
                "item_state": item_state,
                "item_type": details.get("item_type"),
                "ref": details.get("ref"),
            }
            for key in ("source_item_ids", "source_material_ids", "source_intermediate_ids", "transformed_from_item_ids"):
                if isinstance(details.get(key), list):
                    item_attributes[key] = [str(item) for item in details.get(key, []) if str(item).strip()]
            if item_tile is not None:
                item_attributes["tile"] = item_tile
            payload: Dict[str, Any] = {
                "entity_type": "item",
                "label": entity_id,
                # Dropped floor items are rendered as visible waiting entities.
                # Other item lifecycle states stay in attributes and are hidden
                # by the render model unless they are physically on the floor.
                "state": "waiting" if item_state == "DROPPED" else item_state.lower(),
                "attributes": item_attributes,
            }
            if item_position is not None:
                payload["position"] = item_position
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id, "target": details.get("ref")},
                payload,
            )
            continue

        if raw_type == "AGENT_TRAFFIC_CONFLICT" and entity_id:
            worker_ids = details.get("worker_ids", [])
            related = [str(worker_id) for worker_id in worker_ids if str(worker_id).strip() and str(worker_id) != str(entity_id)]
            conflict_payload = {
                "conflict_id": details.get("conflict_id"),
                "conflict_type": details.get("conflict_type"),
                "severity": details.get("severity", "warning"),
                "collision": bool(details.get("collision", False)),
                "primary_worker_id": details.get("primary_worker_id", entity_id),
                "other_worker_id": details.get("other_worker_id"),
                "worker_ids": worker_ids,
                "move_id": details.get("move_id"),
                "other_move_id": details.get("other_move_id"),
                "time_window": details.get("time_window"),
                "tile": details.get("tile"),
                "edge": details.get("edge"),
                "other_edge": details.get("other_edge"),
                "gap_min": details.get("gap_min"),
                "label": f"{details.get('conflict_type', 'TRAFFIC')} {entity_id} {details.get('other_worker_id', '')}",
            }
            conflict_payload.update(conflict_position_payload(details))
            push(
                "traffic_conflict_detected",
                timestamp,
                {"primary": entity_id, "related": related, "source": entity_id, "target": related[0] if related else None},
                conflict_payload,
                suffix="t",
            )
            continue

        if raw_type == "AGENT_MOVE_TILE_START" and entity_id:
            from_position = tile_to_position(layout, details.get("from_tile"))
            to_position = tile_to_position(layout, details.get("to_tile"))
            if from_position and to_position:
                started_at = float(details.get("started_at", timestamp) or timestamp)
                ended_at = float(details.get("ended_at", started_at) or started_at)
                move_id = str(details.get("move_id") or "")
                display_path = active_move_paths.get(move_id) or [from_position, to_position]
                push(
                    "entity_moved",
                    timestamp,
                    {"primary": entity_id},
                    {
                        "from": from_position,
                        "to": to_position,
                        "path": [from_position, to_position],
                        "display_path": display_path,
                        "label": f"tile {details.get('from_tile', '')} -> {details.get('to_tile', '')}",
                        "move_id": details.get("move_id"),
                        "segment_index": details.get("segment_index"),
                    },
                    durative={
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "expected_duration": max(0.0, ended_at - started_at),
                    },
                    suffix=f"tile-start-{details.get('segment_index', 0)}",
                )
            continue

        if raw_type == "AGENT_MOVE_TILE_END" and entity_id:
            to_position = tile_to_position(layout, details.get("to_tile"))
            attrs = canonical_worker_attributes(details, index, entity_id)
            attrs["motion"] = None
            payload: Dict[str, Any] = {"attributes": attrs}
            if to_position is not None:
                payload["position"] = to_position
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                payload,
                suffix=f"tile-end-{details.get('segment_index', 0)}",
            )
            continue

        if raw_type == "AGENT_MOVE_START":
            source = resolve_region_id(details.get("from"))
            target = resolve_region_id(details.get("to"))
            path_positions = path_tiles_to_positions(layout, details.get("path_tiles"))
            move_id = str(details.get("move_id") or "")
            if move_id and path_positions:
                active_move_paths[move_id] = path_positions
            from_position = tile_to_position(layout, details.get("from_tile")) or (positions.get(source) if source else None)
            to_position = tile_to_position(layout, details.get("to_tile")) or (positions.get(target) if target else None)
            if entity_id and source and target and from_position and to_position:
                push(
                    "entity_moved",
                    timestamp,
                    {"primary": entity_id, "source": source, "target": target},
                    {
                        "from": from_position,
                        "to": to_position,
                        "path": path_positions,
                        "display_path": path_positions,
                        "label": f"{details.get('from', '')} -> {details.get('to', '')}",
                        "move_id": details.get("move_id"),
                    },
                    durative={
                        "started_at": timestamp,
                        "ended_at": timestamp + float(details.get("duration", 0)),
                        "expected_duration": float(details.get("duration", 0)),
                    },
                )
            continue

        if raw_type == "AGENT_MOVE_END" and entity_id:
            move_id = str(details.get("move_id") or "")
            if move_id:
                active_move_paths.pop(move_id, None)
            if has_canonical_worker_events:
                continue
            current_task = active_tasks.get(entity_id)
            tile_position = tile_to_position(layout, details.get("tile"))
            payload = {"state": "working" if current_task else "idle"}
            if tile_position is not None:
                payload["position"] = tile_position
            push(
                "state_changed",
                timestamp,
                {"primary": entity_id},
                payload,
            )
            continue

        if raw_type == "AGENT_TASK_START" and entity_id:
            task_id = str(details.get("task_id") or "").strip()
            if task_id:
                push(
                    "rolling_horizon_task_started",
                    timestamp,
                    {"primary": task_id, "target": entity_id, "related": [entity_id]},
                    {
                        "task_id": task_id,
                        "worker_id": entity_id,
                        "task_code": details.get("task_code", ""),
                        "task_type": details.get("task_type", ""),
                        "instance_id": details.get("instance_id", ""),
                    },
                    suffix="rolling-start",
                )
            if has_canonical_worker_events:
                continue
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
                        "current_task_code": details.get("task_code", ""),
                        "current_task_name": details.get("task_name", ""),
                        "current_task_instance_id": details.get("instance_id", ""),
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
            task_id = str(details.get("task_id") or "").strip()
            task_status = str(details.get("status", "completed") or "completed").strip().lower()
            task_reason = str(details.get("reason", "") or "").strip().lower()
            # A battery handover temporarily interrupts the receiver's current task.
            # Keep that task visible as STARTED in rolling-horizon replay tables
            # until the resumed task emits its real completion event.
            temporary_interrupt_reasons = {"battery_swap_wait", "battery_depleted", "horizon_reached"}
            emit_lifecycle_completion = not (
                task_status == "interrupted" and task_reason in temporary_interrupt_reasons
            )
            if task_id and emit_lifecycle_completion:
                push(
                    "rolling_horizon_task_completed",
                    timestamp,
                    {"primary": task_id, "target": entity_id, "related": [entity_id]},
                    {
                        "task_id": task_id,
                        "worker_id": entity_id,
                        "task_code": details.get("task_code", ""),
                        "task_type": details.get("task_type", ""),
                        "instance_id": details.get("instance_id", ""),
                        "status": details.get("status", "completed"),
                        "reason": details.get("reason", ""),
                    },
                    suffix="rolling-end",
                )
            if has_canonical_worker_events:
                continue
            active = active_tasks.pop(entity_id, None)
            push(
                "task_finished",
                timestamp,
                {"primary": entity_id, "target": (active or {}).get("target")},
                {
                    "task_id": task_id,
                    "task_label": (active or {}).get("label", task_label(details)),
                    "next_state": "idle",
                    "attributes": {
                        "last_task_status": details.get("status", "completed"),
                        "last_task_code": details.get("task_code", ""),
                        "last_task_name": details.get("task_name", ""),
                    },
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
                item_type = details.get("queue") or QUEUE_ITEM_TYPE.get(queue_id)
                push(
                    "queue_entered",
                    timestamp,
                    {"primary": details.get("item_id"), "source": resolve_region_id(location), "target": queue_id},
                    {"item_id": details.get("item_id"), "queue_id": queue_id, "item_type": item_type},
                )
            continue

        if raw_type == "QUEUE_POP":
            queue_id = entity_id
            if queue_id in QUEUE_META:
                item_type = details.get("queue") or QUEUE_ITEM_TYPE.get(queue_id)
                push(
                    "queue_exited",
                    timestamp,
                    {"primary": details.get("item_id"), "source": queue_id, "target": resolve_region_id(location)},
                    {"item_id": details.get("item_id"), "queue_id": queue_id, "item_type": item_type},
                )
            continue

        if raw_type == "WAREHOUSE_MATERIAL_RESTOCK":
            push(
                "state_changed",
                timestamp,
                {"primary": "warehouse_material_shelf"},
                {
                    "attributes": {
                        "shelf_count": details.get("shelf_count", 0),
                        "shelf_capacity": details.get("shelf_capacity", 0),
                        "restocked_count": details.get("restocked_count", 0),
                    }
                },
            )
            for index_slot, slot in enumerate(details.get("slots", []) or []):
                if not isinstance(slot, dict):
                    continue
                slot_id = str(slot.get("slot_id", "") or "")
                if not slot_id:
                    continue
                push(
                    "state_changed",
                    timestamp,
                    {"primary": slot_id},
                    {
                        "attributes": {
                            "occupied": True,
                            "material_item_id": slot.get("item_id"),
                            "item_type": "material",
                        }
                    },
                    suffix=f"slot-{index_slot}",
                )
            continue

        if raw_type == "WAREHOUSE_MATERIAL_PICKED":
            slot_id = str(details.get("slot_id", "") or "")
            push(
                "state_changed",
                timestamp,
                {"primary": "warehouse_material_shelf"},
                {
                    "attributes": {
                        "shelf_count": details.get("shelf_count", 0),
                        "shelf_capacity": details.get("shelf_capacity", 0),
                    }
                },
            )
            if slot_id:
                push(
                    "state_changed",
                    timestamp,
                    {"primary": slot_id},
                    {
                        "attributes": {
                            "occupied": False,
                            "material_item_id": None,
                            "item_type": "material",
                        }
                    },
                    suffix="slot",
                )
            continue

        if raw_type == "INSPECTION_SCRAP_QUEUED":
            push(
                "queue_entered",
                timestamp,
                {"primary": entity_id, "source": "inspection_table", "target": "inspection_scrap_queue"},
                {"item_id": entity_id, "queue_id": "inspection_scrap_queue", "queue_size": details.get("queue_length", 0), "item_type": "scrap"},
            )
            continue

        if raw_type == "SCRAP_BATCH_PICKED":
            for index_item, item_id in enumerate(details.get("item_ids", []) or []):
                push(
                    "queue_exited",
                    timestamp,
                    {"primary": item_id, "source": "inspection_scrap_queue", "target": entity_id},
                    {"item_id": item_id, "queue_id": "inspection_scrap_queue", "queue_size": details.get("queue_length", 0), "item_type": "scrap"},
                    suffix=f"scrap-pick-{index_item}",
                )
            continue

        if raw_type == "SCRAP_DISPOSED":
            push(
                "state_changed",
                timestamp,
                {"primary": "scrap_disposal_bin"},
                {
                    "attributes": {
                        "disposed_scrap_count": details.get("disposed_scrap_count", 0),
                        "last_disposed_item_ids": details.get("item_ids", []),
                        "last_disposed_item_count": details.get("item_count", 0),
                    }
                },
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
                        "wait_visual": None,
                        "wait_item_kind": None,
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
            push("message_sent", timestamp, {"source": "inspection_region", "target": "completed_product_buffer", "related": [entity_id] if entity_id else []}, {"message": "COMPLETED"})
            push("state_changed", timestamp, {"primary": "completed_product_buffer"}, {"attributes": {"completed_count": completed_count}}, suffix="b")
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
    initial_shelf_attributes, initial_material_slots = initial_warehouse_material_state(raw_events)

    replay_log = {
        "schema_version": "1.0",
        "metadata": {
            "run_id": str(run_dir.name),
            "title": f"ManSim Existing Run {run_dir.name}",
            "domain": "manufacturing",
            "description": f"Existing ManSim manufacturing run reconstructed from events.jsonl with {len(worker_ids)} workers and {len(machine_ids)} machines.",
            "created_at": run_meta.get("started_at_utc"),
            "decision_mode": run_meta.get("decision_mode"),
            "run_index": run_meta.get("run_index"),
            "total_runs": run_meta.get("total_runs"),
            "total_duration": float(run_meta.get("sim_total_min", run_meta.get("total_days", 0) * run_meta.get("minutes_per_day", 0))),
            "time_unit": "minutes",
            "replay_mode": "strict",
            "position_policy": "simulation_tile_or_motion_only",
            "visual_corrections": False,
        },
        "layout": layout,
        "initial_state": build_initial_state(
            worker_ids,
            machine_ids,
            layout,
            battery_period_min,
            repair_total_min,
            initial_shelf_attributes,
            initial_material_slots,
        ),
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
