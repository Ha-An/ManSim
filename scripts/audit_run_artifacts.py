from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


REQUIRED_ARTIFACTS = [
    "results_dashboard.html",
    "kpi_dashboard.html",
    "gantt.html",
    "gantt_segments.csv",
    "replay_studio_log.json",
    "replay_studio_layout.json",
    "dashboard_manifest.json",
    "kpi.json",
    "events.jsonl",
]

# Keep this list focused on contracts that the dashboards/replay views require.
# Numerical quality checks belong in simulation tests; this script verifies that
# fresh artifacts can be trusted before a human starts visual inspection.
REQUIRED_KPI_KEYS = [
    "humanoid_state_time_by_worker",
    "humanoid_state_time_by_axis",
    "humanoid_state_ratio_by_worker",
    "humanoid_execution_ratio_by_worker",
    "humanoid_unavailable_ratio_by_worker",
    "humanoid_incident_total",
    "humanoid_incidents_by_code",
    "humanoid_incidents_by_category",
    "humanoid_incidents_by_worker",
    "humanoid_incident_recovery_protocol_by_code",
    "repair_collaboration_time_min",
    "repair_collaboration_episodes",
    "shared_product_carry_time_by_worker",
    "traffic_conflicts_by_type",
    "traffic_conflicts_by_worker_pair",
    "warehouse_material_shelf_count",
    "warehouse_material_shelf_capacity",
    "inspection_scrap_queue_length",
    "disposed_scrap_count",
]

AVAILABILITY_STATES = {
    "AVAILABLE",
    "ASSIGNED",
    "EXECUTING",
    "WAITING",
    "BLOCKED",
    "OFFLINE",
    "DISABLED",
}

MANUFACTURING_SCENARIOS = {"", "factory_mfg_basic", "mfg_flow_shop"}
SHIPYARD_SCENARIOS = {"shipyard_basic"}
MFG_FLOW_SHOP_KPI_KEYS = [
    "battery_service_mode",
    "battery_charge_count",
    "battery_charge_time_min",
    "battery_swap_count",
    "battery_delivery_count",
    "preventive_maintenance_task_count",
    "buffer_capacities",
    "buffer_max_occupancy",
    "buffer_max_reserved_slots",
    "buffer_max_committed_plus_reserved",
    "buffer_overflow_attempt_count",
    "buffer_reservation_failure_count",
    "buffer_reservation_leak_count",
    "machine_blocked_after_service_count",
    "machine_blocked_after_service_min",
    "candidate_count_avg",
    "candidate_count_max",
]
SHIPYARD_KPI_KEYS = [
    "makespan_min",
    "surface_tile_count",
    "completed_surface_tile_count",
    "surface_tile_completion_ratio",
    "welded_surface_tile_count",
    "painted_surface_tile_count",
    "rework_count",
    "quality_pass_rate",
    "worker_utilization_by_worker",
    "incident_count_by_code",
]


def _humanoid_state_axis_values() -> dict[str, list[str]]:
    try:
        from humanoidsim import load_state_schema

        schema = load_state_schema()
        return {
            str(axis): [str(value) for value in getattr(definition, "states", {}).keys()]
            for axis, definition in getattr(schema, "axes", {}).items()
        }
    except Exception:
        return {
            "availability": ["AVAILABLE", "ASSIGNED", "EXECUTING", "WAITING", "BLOCKED", "OFFLINE", "DISABLED"],
            "mobility": ["STATIONARY", "NAVIGATING", "DOCKING"],
            "power": ["POWER_NORMAL", "POWER_LOW", "POWER_CRITICAL", "DEPLETED", "CHARGING"],
            "manipulation": ["FREE", "REACHING", "HOLDING", "PLACING"],
        }


class Audit:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


def _load_json(path: Path, audit: Audit) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        audit.error(f"failed to load JSON {path.name}: {exc}")
        return {}


def _scenario_type(run_dir: Path, kpi: dict[str, Any] | None = None) -> str:
    if isinstance(kpi, dict):
        scenario = str(kpi.get("scenario_type") or "").strip()
        if scenario:
            return scenario
    for name in ["run_meta.json", "replay_studio_log.json"]:
        path = run_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if name == "run_meta.json":
            scenario = str(data.get("scenario_type") or "").strip()
        else:
            metadata = data.get("metadata", {}) if isinstance(data, dict) else {}
            scenario = str(metadata.get("scenario_type") or "").strip() if isinstance(metadata, dict) else ""
        if scenario:
            return scenario
    return ""


def _allows_open_runtime_events(run_dir: Path) -> bool:
    path = run_dir / "kpi.json"
    if not path.exists():
        return False
    try:
        kpi = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(kpi, dict):
        return False
    return kpi.get("terminated") is False


def _iter_events(path: Path, audit: Audit) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    audit.error(f"events.jsonl:{line_no}: invalid JSON: {exc}")
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except Exception as exc:
        audit.error(f"failed to read events.jsonl: {exc}")
    return events


def _event_time(event: dict[str, Any]) -> float:
    try:
        return float(event.get("t", event.get("timestamp", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_worker_id(value: Any) -> bool:
    text = str(value or "").strip().upper()
    return len(text) > 1 and text[0] == "A" and text[1:].isdigit()


def _tile_tuple(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict) and "x" in value and "y" in value:
        try:
            return int(value["x"]), int(value["y"])
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _layout_spatial_contract(layout: dict[str, Any]) -> tuple[int, int, float, set[tuple[int, int]]]:
    grid = layout.get("grid", {}) if isinstance(layout.get("grid", {}), dict) else {}
    width = int(grid.get("width_tiles", 0) or 0)
    height = int(grid.get("height_tiles", 0) or 0)
    tile_time = float(grid.get("tile_time_min", 0.0) or 0.0)
    blocked = {
        tile
        for raw in grid.get("walls", [])
        if (tile := _tile_tuple(raw)) is not None
    }
    doors = {
        tile
        for raw in grid.get("doors", [])
        if (tile := _tile_tuple(raw)) is not None
    }
    blocked.difference_update(doors)
    for footprint in grid.get("object_footprints", []):
        if not isinstance(footprint, dict) or not bool(footprint.get("blocking", False)):
            continue
        try:
            x = int(footprint.get("x", 0))
            y = int(footprint.get("y", 0))
            footprint_width = int(footprint.get("width", 0))
            footprint_height = int(footprint.get("height", 0))
        except (TypeError, ValueError):
            continue
        blocked.update(
            (tile_x, tile_y)
            for tile_x in range(x, x + max(0, footprint_width))
            for tile_y in range(y, y + max(0, footprint_height))
        )
    blocked.difference_update(doors)
    return width, height, tile_time, blocked


def _path_spatial_issues(
    raw_path: Any,
    *,
    width: int,
    height: int,
    blocked: set[tuple[int, int]],
) -> tuple[list[tuple[int, int]], list[str]]:
    if not isinstance(raw_path, list):
        return [], ["path is not a list"]
    path: list[tuple[int, int]] = []
    issues: list[str] = []
    for index, raw_tile in enumerate(raw_path):
        tile = _tile_tuple(raw_tile)
        if tile is None:
            issues.append(f"invalid tile at index {index}: {raw_tile}")
            continue
        path.append(tile)
        if width > 0 and height > 0 and not (0 <= tile[0] < width and 0 <= tile[1] < height):
            issues.append(f"out-of-bounds tile {tile}")
        if tile in blocked:
            issues.append(f"blocking tile {tile}")
    for source, target in zip(path, path[1:]):
        distance = abs(source[0] - target[0]) + abs(source[1] - target[1])
        if distance != 1:
            issues.append(f"non-adjacent edge {source}->{target} (distance={distance})")
    return path, issues


def check_spatial_continuity(
    run_dir: Path,
    events: list[dict[str, Any]],
    audit: Audit,
    *,
    allow_open_moves: bool = False,
) -> None:
    """Validate that recorded grid movement is contiguous and obstacle-safe."""
    layout = _load_json(run_dir / "replay_studio_layout.json", audit)
    if not isinstance(layout, dict):
        return
    width, height, tile_time, blocked = _layout_spatial_contract(layout)
    if width <= 0 or height <= 0:
        audit.error("layout grid has invalid dimensions for spatial audit")
        return

    move_path_count = 0
    tile_segment_count = 0
    state_motion_path_count = 0
    item_floor_tile_count = 0
    issue_count = 0
    issue_examples: list[str] = []
    active_segments: dict[tuple[str, str, int], tuple[tuple[int, int], tuple[int, int]]] = {}
    completed_segments: set[tuple[str, str, int]] = set()
    last_move_tile: dict[tuple[str, str], tuple[int, int]] = {}

    def record(event: dict[str, Any], message: str) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issue_examples) < 10:
            issue_examples.append(f"t={round(_event_time(event), 3)} {event.get('entity_id', '')} {message}")

    for event in events:
        event_type = str(event.get("type") or event.get("event_type") or "")
        details = event.get("details", {})
        details = details if isinstance(details, dict) else {}
        worker_id = str(event.get("entity_id") or "")

        if event_type == "AGENT_MOVE_START" and _is_worker_id(worker_id):
            move_path_count += 1
            path, issues = _path_spatial_issues(
                details.get("path_tiles"), width=width, height=height, blocked=blocked
            )
            for issue in issues:
                record(event, f"move path: {issue}")
            from_tile = _tile_tuple(details.get("from_tile"))
            to_tile = _tile_tuple(details.get("to_tile"))
            if path:
                if from_tile is not None and path[0] != from_tile:
                    record(event, f"move path starts at {path[0]}, expected {from_tile}")
                if to_tile is not None and path[-1] != to_tile:
                    record(event, f"move path ends at {path[-1]}, expected {to_tile}")
                try:
                    multiplier = float(details.get("effective_time_multiplier", 1.0) or 1.0)
                    duration = float(details.get("duration", 0.0) or 0.0)
                    sampled_tile_time = float(details.get("sampled_tile_time_min", 0.0) or 0.0)
                except (TypeError, ValueError):
                    multiplier, duration, sampled_tile_time = 1.0, -1.0, 0.0
                effective_tile_time = sampled_tile_time if sampled_tile_time > 0.0 else tile_time * multiplier
                expected_duration = max(0, len(path) - 1) * effective_tile_time
                if duration >= 0.0 and abs(duration - expected_duration) > max(0.002, effective_tile_time * 0.02):
                    record(event, f"move duration {duration} does not match path duration {round(expected_duration, 6)}")
            elif from_tile != to_tile:
                record(event, "move between distinct tiles has no path_tiles")

        if event_type == "WORKER_STATE_CHANGED" and _is_worker_id(worker_id):
            motion = details.get("motion")
            if isinstance(motion, dict) and isinstance(motion.get("path_tiles"), list):
                state_motion_path_count += 1
                _path, issues = _path_spatial_issues(
                    motion.get("path_tiles"), width=width, height=height, blocked=blocked
                )
                for issue in issues:
                    record(event, f"worker state motion: {issue}")

        if event_type == "ITEM_STATE_CHANGED" and details.get("tile") is not None:
            item_floor_tile_count += 1
            tile = _tile_tuple(details.get("tile"))
            if tile is None:
                record(event, f"item has invalid floor tile {details.get('tile')}")
            elif not (0 <= tile[0] < width and 0 <= tile[1] < height):
                record(event, f"item floor tile is out of bounds: {tile}")
            elif tile in blocked:
                record(event, f"item floor tile is blocked: {tile}")

        if event_type == "AGENT_MOVE_TILE_START" and _is_worker_id(worker_id):
            tile_segment_count += 1
            move_id = str(details.get("move_id") or "")
            try:
                segment_index = int(details.get("segment_index", 0) or 0)
            except (TypeError, ValueError):
                segment_index = 0
            key = (worker_id, move_id, segment_index)
            source = _tile_tuple(details.get("from_tile"))
            target = _tile_tuple(details.get("to_tile"))
            pair_path, issues = _path_spatial_issues(
                [details.get("from_tile"), details.get("to_tile")],
                width=width,
                height=height,
                blocked=blocked,
            )
            for issue in issues:
                record(event, f"tile segment: {issue}")
            if source is not None and target is not None and len(pair_path) == 2:
                if key in active_segments or key in completed_segments:
                    record(event, f"duplicate tile segment start {move_id}:{segment_index}")
                active_segments[key] = (source, target)
                previous = last_move_tile.get((worker_id, move_id))
                if previous is not None and previous != source:
                    record(event, f"move segment chain jumps {previous}->{source} for {move_id}")

        elif event_type == "AGENT_MOVE_TILE_END" and _is_worker_id(worker_id):
            move_id = str(details.get("move_id") or "")
            try:
                segment_index = int(details.get("segment_index", 0) or 0)
            except (TypeError, ValueError):
                segment_index = 0
            key = (worker_id, move_id, segment_index)
            source = _tile_tuple(details.get("from_tile"))
            target = _tile_tuple(details.get("to_tile"))
            started_pair = active_segments.pop(key, None)
            if started_pair is None:
                record(event, f"tile segment end has no matching start {move_id}:{segment_index}")
            elif started_pair != (source, target):
                record(event, f"tile segment end {source}->{target} differs from start {started_pair}")
            completed_segments.add(key)
            if target is not None:
                last_move_tile[(worker_id, move_id)] = target

        elif event_type == "AGENT_MOVE_TILE_CANCELLED" and _is_worker_id(worker_id):
            move_id = str(details.get("move_id") or "")
            try:
                segment_index = int(details.get("segment_index", 0) or 0)
            except (TypeError, ValueError):
                segment_index = 0
            key = (worker_id, move_id, segment_index)
            source = _tile_tuple(details.get("from_tile"))
            target = _tile_tuple(details.get("to_tile"))
            committed = _tile_tuple(details.get("committed_tile"))
            started_pair = active_segments.pop(key, None)
            if started_pair is None:
                record(event, f"tile segment cancellation has no matching start {move_id}:{segment_index}")
            elif started_pair != (source, target):
                record(event, f"tile segment cancellation {source}->{target} differs from start {started_pair}")
            if source is not None and committed != source:
                record(event, f"cancelled tile segment committed {committed} instead of remaining at {source}")
            completed_segments.add(key)
            if source is not None:
                last_move_tile[(worker_id, move_id)] = source

        elif event_type == "AGENT_MOVE_END" and _is_worker_id(worker_id):
            move_id = str(details.get("move_id") or "")
            target = _tile_tuple(details.get("to_tile"))
            previous = last_move_tile.get((worker_id, move_id))
            if previous is not None and target is not None and previous != target:
                record(event, f"move end tile {target} differs from final segment tile {previous}")

    if active_segments:
        message = f"open tile movement segments at horizon: {len(active_segments)}"
        if allow_open_moves:
            audit.warn(message)
        else:
            audit.error(message)
    if issue_count:
        audit.error(f"spatial continuity violations: {issue_count} examples={issue_examples}")
    audit.note(
        "spatial continuity "
        f"move_paths={move_path_count} tile_segments={tile_segment_count} "
        f"state_motion_paths={state_motion_path_count} item_floor_tiles={item_floor_tile_count} "
        f"blocked_tiles={len(blocked)}"
    )


def check_item_transport_continuity(events: list[dict[str, Any]], audit: Audit) -> None:
    """Require physical item moves to be backed by a worker carry/drop transition."""
    events_by_time: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        events_by_time[round(_event_time(event), 6)].append(event)

    carry_count = 0
    moved_count = 0
    issue_count = 0
    examples: list[str] = []
    stationary_items: set[str] = set()

    def record(t: float, message: str) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(examples) < 10:
            examples.append(f"t={t} {message}")

    for t, timestamp_events in events_by_time.items():
        worker_cargo: dict[str, set[str]] = defaultdict(set)
        carry_transitions: set[tuple[str, str]] = set()
        pick_events: set[tuple[str, str]] = set()
        drop_events: set[tuple[str, str]] = set()
        stationary_item_states: set[str] = set()

        for event in timestamp_events:
            event_type = str(event.get("type") or event.get("event_type") or "")
            details = event.get("details", {})
            details = details if isinstance(details, dict) else {}
            entity_id = str(event.get("entity_id") or "")
            if event_type == "WORKER_CARGO_CHANGED" and _is_worker_id(entity_id):
                cargo = details.get("cargo", {})
                cargo = cargo if isinstance(cargo, dict) else {}
                item_ids = cargo.get("item_ids", [])
                if isinstance(item_ids, list):
                    worker_cargo[entity_id].update(str(item_id) for item_id in item_ids if str(item_id))
            elif event_type == "ITEM_STATE_CHANGED":
                item_state = str(details.get("item_state") or "")
                ref = str(details.get("ref") or "")
                if item_state == "CARRIED_BY_WORKER" and _is_worker_id(ref):
                    carry_transitions.add((entity_id, ref))
                    stationary_items.discard(entity_id)
                elif item_state != "CARRIED_BY_WORKER":
                    stationary_item_states.add(entity_id)
                    stationary_items.add(entity_id)
            elif event_type == "AGENT_PICK_ITEM" and _is_worker_id(entity_id):
                item_id = str(details.get("item_id") or "")
                if item_id:
                    pick_events.add((item_id, entity_id))
            elif event_type == "AGENT_DROP_ITEM" and _is_worker_id(entity_id):
                item_id = str(details.get("item_id") or "")
                if item_id:
                    drop_events.add((item_id, entity_id))

        for item_id, worker_id in carry_transitions:
            carry_count += 1
            if item_id not in worker_cargo.get(worker_id, set()):
                record(t, f"item {item_id} is assigned to {worker_id} without matching worker cargo")
        for item_id, worker_id in pick_events:
            if (item_id, worker_id) not in carry_transitions:
                record(t, f"pickup {item_id} by {worker_id} has no carried item state")

        dropped_item_ids = {item_id for item_id, _worker_id in drop_events}
        for event in timestamp_events:
            if str(event.get("type") or event.get("event_type") or "") != "ITEM_MOVED":
                continue
            moved_count += 1
            item_id = str(event.get("entity_id") or "")
            if item_id not in dropped_item_ids:
                record(t, f"item move {item_id} has no matching worker drop")
            if item_id not in stationary_items:
                record(t, f"item move {item_id} has no destination item state")

    if issue_count:
        audit.error(f"item transport continuity violations: {issue_count} examples={examples}")
    audit.note(f"item transport continuity carried={carry_count} moved={moved_count}")


def check_inspection_task_lifecycle(events: list[dict[str, Any]], audit: Audit) -> None:
    """Validate the load -> inspect -> result -> unload contract per product."""

    event_names = {
        "INSPECTION_DESK_ITEM_LOADED",
        "INSPECTION_STARTED",
        "INSPECTION_RESULT_RECORDED",
        "INSPECTION_DESK_ITEM_UNLOADED",
    }
    by_product: dict[str, dict[str, list[tuple[float, dict[str, Any]]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for event in events:
        event_type = str(event.get("type") or event.get("event_type") or "")
        if event_type not in event_names:
            continue
        details = event.get("details", {})
        details = details if isinstance(details, dict) else {}
        product_id = str(details.get("product_id") or event.get("entity_id") or "").strip()
        if product_id:
            by_product[product_id][event_type].append((_event_time(event), details))

    errors: list[str] = []
    completed_count = 0
    recorded_count = 0
    for product_id, lifecycle in sorted(by_product.items()):
        loaded = lifecycle.get("INSPECTION_DESK_ITEM_LOADED", [])
        started = lifecycle.get("INSPECTION_STARTED", [])
        recorded = lifecycle.get("INSPECTION_RESULT_RECORDED", [])
        unloaded = lifecycle.get("INSPECTION_DESK_ITEM_UNLOADED", [])
        if len(loaded) > 1:
            errors.append(f"{product_id} loaded {len(loaded)} times")
        if len(recorded) > 1:
            errors.append(f"{product_id} result recorded {len(recorded)} times")
        if len(unloaded) > 1:
            errors.append(f"{product_id} unloaded {len(unloaded)} times")
        if started and (not loaded or min(t for t, _ in started) < loaded[0][0] - 1e-9):
            errors.append(f"{product_id} inspection started before desk load")
        if recorded:
            recorded_count += 1
            record_time, record_details = recorded[0]
            result = str(record_details.get("inspection_result") or "").upper()
            if result not in {"PASS", "FAIL"}:
                errors.append(f"{product_id} has invalid recorded result {result!r}")
            if not started or record_time < min(t for t, _ in started) - 1e-9:
                errors.append(f"{product_id} result recorded without preceding inspection")
        if unloaded:
            completed_count += 1
            unload_time, unload_details = unloaded[0]
            if not recorded or unload_time < recorded[0][0] - 1e-9:
                errors.append(f"{product_id} unloaded without preceding recorded result")
            else:
                recorded_result = str(recorded[0][1].get("inspection_result") or "").upper()
                unloaded_result = str(unload_details.get("inspection_result") or "").upper()
                if unloaded_result != recorded_result:
                    errors.append(
                        f"{product_id} unload result {unloaded_result!r} differs from {recorded_result!r}"
                    )
                expected_destination = (
                    "inspection_output_queue" if recorded_result == "PASS" else "inspection_scrap_queue"
                )
                if str(unload_details.get("destination") or "") != expected_destination:
                    errors.append(
                        f"{product_id} unload destination {unload_details.get('destination')!r} "
                        f"differs from {expected_destination!r}"
                    )

    if errors:
        audit.error(f"inspection lifecycle violations: {len(errors)} examples={errors[:10]}")
    audit.note(
        "inspection lifecycle "
        f"products={len(by_product)} recorded={recorded_count} unloaded={completed_count}"
    )


def _event_log_enabled(run_dir: Path) -> bool:
    run_meta = _load_json(run_dir / "run_meta.json", Audit())
    event_log = run_meta.get("event_log", {}) if isinstance(run_meta, dict) else {}
    if isinstance(event_log, dict) and "enabled" in event_log:
        return bool(event_log.get("enabled", False))
    return (run_dir / "events.jsonl").exists()


def check_required_files(
    run_dir: Path,
    audit: Audit,
    *,
    require_replay_log: bool = True,
    require_event_log: bool = True,
) -> None:
    required = REQUIRED_ARTIFACTS if require_replay_log else [
        name for name in REQUIRED_ARTIFACTS if name != "replay_studio_log.json"
    ]
    if not require_event_log:
        required = [name for name in required if name != "events.jsonl"]
    for name in required:
        path = run_dir / name
        if not path.exists():
            audit.error(f"missing required artifact: {name}")
        elif path.stat().st_size <= 0:
            audit.error(f"empty required artifact: {name}")


def check_kpi(run_dir: Path, audit: Audit) -> None:
    kpi = _load_json(run_dir / "kpi.json", audit)
    if not isinstance(kpi, dict):
        audit.error("kpi.json root is not an object")
        return
    scenario_type = _scenario_type(run_dir, kpi)
    for key in REQUIRED_KPI_KEYS:
        if key not in kpi:
            audit.error(f"kpi.json missing key: {key}")
    if scenario_type in SHIPYARD_SCENARIOS:
        for key in SHIPYARD_KPI_KEYS:
            if key not in kpi:
                audit.error(f"shipyard kpi.json missing key: {key}")
    if scenario_type == "mfg_flow_shop":
        for key in MFG_FLOW_SHOP_KPI_KEYS:
            if key not in kpi:
                audit.error(f"mfg_flow_shop kpi.json missing key: {key}")
        if str(kpi.get("battery_service_mode", "")).strip().lower() != "dock_charge":
            audit.error("mfg_flow_shop battery_service_mode must be dock_charge")
        for key in ("battery_swap_count", "battery_delivery_count", "preventive_maintenance_task_count", "handover_item_count"):
            if int(kpi.get(key, 0) or 0) != 0:
                audit.error(f"mfg_flow_shop {key} must be zero, got {kpi.get(key)}")
        for key in (
            "buffer_overflow_attempt_count",
            "buffer_reservation_failure_count",
            "buffer_reservation_leak_count",
        ):
            if int(kpi.get(key, 0) or 0) != 0:
                audit.error(f"mfg_flow_shop {key} must be zero, got {kpi.get(key)}")
        capacities = kpi.get("buffer_capacities", {})
        combined_max = kpi.get("buffer_max_committed_plus_reserved", {})
        if isinstance(capacities, dict) and isinstance(combined_max, dict):
            for buffer_id, raw_capacity in capacities.items():
                capacity = int(raw_capacity or 0)
                observed = int(combined_max.get(buffer_id, 0) or 0)
                if capacity <= 0:
                    audit.error(f"mfg_flow_shop buffer {buffer_id} has invalid capacity {capacity}")
                elif observed > capacity:
                    audit.error(
                        f"mfg_flow_shop buffer {buffer_id} exceeded capacity: "
                        f"occupancy+reservations={observed} capacity={capacity}"
                    )
    state_axis = kpi.get("humanoid_state_time_by_axis")
    if isinstance(state_axis, dict):
        for axis in ["availability", "mobility", "power", "manipulation"]:
            if axis not in state_axis:
                audit.error(f"kpi humanoid_state_time_by_axis missing axis: {axis}")
        for axis, states in _humanoid_state_axis_values().items():
            rows = state_axis.get(axis, {}) if isinstance(state_axis.get(axis, {}), dict) else {}
            for state in states:
                if state not in rows:
                    audit.error(f"kpi humanoid_state_time_by_axis[{axis}] missing state: {state}")
    state_by_worker = kpi.get("humanoid_state_time_by_worker")
    if isinstance(state_by_worker, dict):
        for worker_id, worker_rows in state_by_worker.items():
            if not isinstance(worker_rows, dict):
                audit.error(f"kpi humanoid_state_time_by_worker[{worker_id}] is not an object")
                continue
            for axis, states in _humanoid_state_axis_values().items():
                rows = worker_rows.get(axis, {}) if isinstance(worker_rows.get(axis, {}), dict) else {}
                for state in states:
                    if state not in rows:
                        audit.error(f"kpi humanoid_state_time_by_worker[{worker_id}][{axis}] missing state: {state}")
    if int(kpi.get("repair_collaboration_time_min", 0) or 0) < 0:
        audit.error("repair_collaboration_time_min is negative")
    audit.note(
        "kpi "
        f"scenario={scenario_type or 'unknown'} "
        f"products={kpi.get('total_products', 0)} "
        f"surface_tiles={kpi.get('completed_surface_tile_count', kpi.get('completed_section_count', '-'))}/{kpi.get('surface_tile_completion_ratio', kpi.get('section_completion_ratio', '-'))} "
        f"incidents={kpi.get('humanoid_incident_total', 0)} "
        f"repair_collab_min={kpi.get('repair_collaboration_time_min', 0)}"
    )


def _positive_availability_durations(events: list[dict[str, Any]]) -> dict[str, float]:
    max_t = max((_event_time(event) for event in events), default=0.0)
    current: dict[str, str] = {}
    last_t: dict[str, float] = {}
    durations: dict[str, float] = defaultdict(float)
    for event in sorted(events, key=_event_time):
        details = event.get("details", {})
        details = details if isinstance(details, dict) else {}
        state = details.get("humanoid_state")
        if not isinstance(state, dict):
            continue
        worker_id = str(event.get("entity_id", "")).strip()
        if not _is_worker_id(worker_id):
            continue
        t = _event_time(event)
        if worker_id in current:
            durations[current[worker_id]] += max(0.0, t - last_t[worker_id])
        current[worker_id] = str(state.get("availability") or "").strip().upper()
        last_t[worker_id] = t
    for worker_id, availability in current.items():
        durations[availability] += max(0.0, max_t - last_t.get(worker_id, max_t))
    return {key: value for key, value in durations.items() if value > 0.0001}


def check_gantt(run_dir: Path, events: list[dict[str, Any]], audit: Audit, scenario_type: str = "") -> None:
    path = run_dir / "gantt_segments.csv"
    try:
        rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    except Exception as exc:
        audit.error(f"failed to read gantt_segments.csv: {exc}")
        return
    if not rows:
        audit.error("gantt_segments.csv has no rows")
        return
    groups = {row.get("entity_group", "") for row in rows}
    allowed_groups = {"Worker", "Machine"}
    if scenario_type in SHIPYARD_SCENARIOS:
        allowed_groups.update({"Ship Section", "Ship Surface"})
    unexpected_groups = groups - allowed_groups
    if unexpected_groups:
        audit.error(f"gantt has unexpected entity groups: {sorted(unexpected_groups)}")
    product_lanes = [row.get("lane", "") for row in rows if str(row.get("lane", "")).upper().startswith(("PRODUCT", "MAT-", "INT-"))]
    if product_lanes:
        audit.error(f"gantt has item/product lanes: {sorted(set(product_lanes))[:10]}")
    worker_statuses = {row.get("status", "") for row in rows if row.get("entity_group") == "Worker"}
    # CHARGING is the one power-axis state intentionally promoted to a Gantt
    # segment; all other worker statuses must remain Availability values.
    invalid_worker_statuses = worker_statuses - AVAILABILITY_STATES - {"UNKNOWN", "CHARGING"}
    if invalid_worker_statuses:
        audit.error(f"gantt has non-availability worker statuses: {sorted(invalid_worker_statuses)}")
    # A positive-duration availability state in events must be represented in
    # the Gantt data; zero-duration ASSIGNED/WAITING transitions may only appear
    # in the legend and are intentionally ignored here.
    positive_event_states = set(_positive_availability_durations(events))
    missing_positive_states = positive_event_states - worker_statuses
    if missing_positive_states:
        audit.error(f"gantt missing positive-duration availability states: {sorted(missing_positive_states)}")
    html = (run_dir / "gantt.html").read_text(encoding="utf-8", errors="ignore")
    if "payload=" in html:
        audit.warn("gantt hover still contains payload=; tooltip may be too noisy")
    audit.note(f"gantt rows={len(rows)} worker_statuses={sorted(worker_statuses)}")


def check_event_log_consistency(events: list[dict[str, Any]], audit: Audit, *, allow_open_tasks: bool = False) -> None:
    task_starts: dict[tuple[Any, Any, Any, Any], int] = defaultdict(int)
    task_ends: dict[tuple[Any, Any, Any, Any], int] = defaultdict(int)
    step_starts: dict[tuple[Any, Any, Any, Any, Any], int] = defaultdict(int)
    step_ends: dict[tuple[Any, Any, Any, Any, Any], int] = defaultdict(int)
    available_with_task_context = 0
    blocked_without_reason = 0
    recovery_active_nonblocked = 0
    self_traffic_conflicts = 0
    non_upper_incident_codes: set[str] = set()
    rolling_missing_task_id = 0
    rolling_missing_task_code = 0
    last_worker_tile_and_battery: dict[str, tuple[Any, float | None]] = {}
    zero_battery_tile_moves: list[tuple[float, str, Any, Any]] = []
    eps = 1e-6

    for event in events:
        event_type = str(event.get("type") or event.get("event_type") or "")
        details = event.get("details", {})
        details = details if isinstance(details, dict) else {}
        state = details.get("humanoid_state")
        worker_id = str(event.get("entity_id", "")).strip()
        if isinstance(state, dict):
            if state.get("availability") == "AVAILABLE" and state.get("task_context"):
                available_with_task_context += 1
            if state.get("availability") == "BLOCKED" and not state.get("reason"):
                blocked_without_reason += 1
        if event_type == "WORKER_STATE_CHANGED" and _is_worker_id(worker_id):
            tile = details.get("tile")
            battery_raw = details.get("battery_remaining_min")
            battery: float | None
            try:
                battery = float(battery_raw) if battery_raw is not None else None
            except (TypeError, ValueError):
                battery = None
            previous = last_worker_tile_and_battery.get(worker_id)
            if (
                previous is not None
                and tile is not None
                and previous[0] is not None
                and tile != previous[0]
                and battery is not None
                and previous[1] is not None
                and battery <= eps
                and previous[1] <= eps
            ):
                zero_battery_tile_moves.append((_event_time(event), worker_id, previous[0], tile))
            if tile is not None:
                last_worker_tile_and_battery[worker_id] = (tile, battery)

        if event_type == "HUMANOID_TASK_START":
            task_starts[(event.get("entity_id"), details.get("instance_id"), details.get("task_code"), details.get("task_path") or "")] += 1
        elif event_type == "HUMANOID_TASK_END":
            task_ends[(event.get("entity_id"), details.get("instance_id"), details.get("task_code"), details.get("task_path") or "")] += 1
        elif event_type == "HUMANOID_STEP_START":
            if str(details.get("call_level") or "PRIMITIVE_SKILL") == "PRIMITIVE_SKILL":
                step_starts[
                    (
                        event.get("entity_id"),
                        details.get("instance_id"),
                        details.get("step_id"),
                        details.get("primitive_call_code"),
                        details.get("task_path") or "",
                    )
                ] += 1
        elif event_type == "HUMANOID_STEP_END":
            if str(details.get("call_level") or "PRIMITIVE_SKILL") == "PRIMITIVE_SKILL":
                step_ends[
                    (
                        event.get("entity_id"),
                        details.get("instance_id"),
                        details.get("step_id"),
                        details.get("primitive_call_code"),
                        details.get("task_path") or "",
                    )
                ] += 1
        elif event_type == "AGENT_TRAFFIC_CONFLICT":
            primary = str(details.get("primary_worker_id") or "")
            other = str(details.get("other_worker_id") or "")
            worker_ids = [str(item) for item in details.get("worker_ids", []) if str(item)]
            if (primary and primary == other) or len(worker_ids) != len(set(worker_ids)):
                self_traffic_conflicts += 1
        elif event_type == "HUMANOID_INCIDENT":
            incident_code = str(details.get("incident_code") or "")
            if incident_code and incident_code != incident_code.upper():
                non_upper_incident_codes.add(incident_code)
        elif event_type in {
            "ROLLING_HORIZON_CANDIDATE_COLLECTED",
            "ROLLING_HORIZON_DISPATCH",
            "ROLLING_HORIZON_TASK_REQUEUED",
            "ROLLING_HORIZON_TASK_SKIPPED",
        }:
            task_code = str(details.get("task_code") or "").strip()
            # Window summary events intentionally have no task identity. Any
            # task-specific rolling event must keep the stable top-level task id
            # so Replay/KPI/Gantt can follow it across requeue/re-dispatch.
            if task_code:
                if not str(details.get("task_id") or "").strip():
                    rolling_missing_task_id += 1
            if event_type in {"ROLLING_HORIZON_CANDIDATE_COLLECTED", "ROLLING_HORIZON_DISPATCH"}:
                opportunity_id = str(details.get("opportunity_id") or "").strip()
                if opportunity_id and not opportunity_id.startswith("RH-") and not task_code:
                    rolling_missing_task_code += 1

        recovery_context = details.get("recovery_context")
        if isinstance(recovery_context, dict) and recovery_context.get("active") is True:
            if not isinstance(state, dict) or state.get("availability") != "BLOCKED":
                recovery_active_nonblocked += 1

    def _diff_count(left: dict[tuple[Any, ...], int], right: dict[tuple[Any, ...], int]) -> int:
        keys = set(left) | set(right)
        return sum(abs(int(left.get(key, 0)) - int(right.get(key, 0))) for key in keys)

    task_mismatch = _diff_count(task_starts, task_ends)
    step_mismatch = _diff_count(step_starts, step_ends)
    if task_mismatch:
        if allow_open_tasks:
            audit.warn(f"humanoid task start/end mismatch count on open horizon: {task_mismatch}")
        else:
            audit.error(f"humanoid task start/end mismatch count: {task_mismatch}")
    if step_mismatch:
        if allow_open_tasks:
            audit.warn(f"humanoid primitive step start/end mismatch count on open horizon: {step_mismatch}")
        else:
            audit.error(f"humanoid primitive step start/end mismatch count: {step_mismatch}")
    if available_with_task_context:
        audit.error(f"worker state AVAILABLE retains task_context: {available_with_task_context}")
    if blocked_without_reason:
        audit.error(f"worker state BLOCKED without reason: {blocked_without_reason}")
    if recovery_active_nonblocked:
        audit.error(f"active recovery events not BLOCKED: {recovery_active_nonblocked}")
    if self_traffic_conflicts:
        audit.error(f"event log has self traffic conflicts: {self_traffic_conflicts}")
    if non_upper_incident_codes:
        audit.error(f"incident codes are not uppercase: {sorted(non_upper_incident_codes)}")
    if rolling_missing_task_id:
        audit.error(f"rolling task events missing stable task_id: {rolling_missing_task_id}")
    if rolling_missing_task_code:
        audit.error(f"rolling task events missing task_code: {rolling_missing_task_code}")
    if zero_battery_tile_moves:
        examples = [
            f"t={round(t, 3)} {worker_id} {from_tile}->{to_tile}"
            for t, worker_id, from_tile, to_tile in zero_battery_tile_moves[:5]
        ]
        audit.error(f"worker tile changed while battery was already depleted: {len(zero_battery_tile_moves)} examples={examples}")


def check_replay_log(run_dir: Path, audit: Audit, scenario_type: str = "") -> None:
    replay = _load_json(run_dir / "replay_studio_log.json", audit)
    if not isinstance(replay, dict):
        audit.error("replay_studio_log.json root is not an object")
        return
    initial_entities = (
        replay.get("initial_state", {}).get("entities", {})
        if isinstance(replay.get("initial_state", {}), dict)
        else {}
    )
    if scenario_type in MANUFACTURING_SCENARIOS and "completed_product_buffer" not in initial_entities:
        audit.error("replay initial state missing completed_product_buffer")
    if scenario_type in MANUFACTURING_SCENARIOS:
        completed_entity = initial_entities.get("completed_product_buffer", {})
        if isinstance(completed_entity, dict) and completed_entity.get("position") is None:
            audit.error("completed_product_buffer has no replay position")
    events = replay.get("events", [])
    if not isinstance(events, list):
        audit.error("replay events is not a list")
        return

    # Machine overlays are semantic, not decorative: an item on a machine means
    # DONE_WAIT_UNLOAD. WAIT_INPUT must stay visually empty.
    stale_machine_overlay_count = 0
    self_traffic_conflicts: list[str] = []
    missing_humanoid_state_workers: Counter[str] = Counter()
    replay_spatial_issue_count = 0
    replay_spatial_examples: list[str] = []
    replay_move_count = 0
    layout = _load_json(run_dir / "replay_studio_layout.json", audit)
    width, height, _tile_time, blocked = _layout_spatial_contract(layout if isinstance(layout, dict) else {})
    viewport = layout.get("viewport", {}) if isinstance(layout, dict) and isinstance(layout.get("viewport", {}), dict) else {}
    viewport_width = float(viewport.get("width", 0.0) or 0.0)
    viewport_height = float(viewport.get("height", 0.0) or 0.0)

    def replay_position_tile(value: Any) -> tuple[int, int] | None:
        if not isinstance(value, dict) or width <= 0 or height <= 0 or viewport_width <= 0 or viewport_height <= 0:
            return None
        try:
            x = float(value.get("x"))
            y = float(value.get("y"))
        except (TypeError, ValueError):
            return None
        return int(x / (viewport_width / width)), int(y / (viewport_height / height))

    def record_replay_spatial(event: dict[str, Any], message: str) -> None:
        nonlocal replay_spatial_issue_count
        replay_spatial_issue_count += 1
        if len(replay_spatial_examples) < 10:
            replay_spatial_examples.append(f"{event.get('event_id', '')}: {message}")

    for event in events:
        if not isinstance(event, dict):
            continue
        refs = event.get("entity_refs", {})
        refs = refs if isinstance(refs, dict) else {}
        payload = event.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}
        attrs = payload.get("attributes", {})
        attrs = attrs if isinstance(attrs, dict) else {}
        if event.get("event_type") == "entity_moved" and _is_worker_id(refs.get("primary")):
            replay_move_count += 1
            raw_path = payload.get("path")
            if not isinstance(raw_path, list) or len(raw_path) < 2:
                record_replay_spatial(event, "worker entity_moved has no two-point path")
            else:
                path = [replay_position_tile(point) for point in raw_path]
                if any(tile is None for tile in path):
                    record_replay_spatial(event, "worker entity_moved has invalid position")
                else:
                    tiles = [tile for tile in path if tile is not None]
                    for tile in tiles:
                        if not (0 <= tile[0] < width and 0 <= tile[1] < height):
                            record_replay_spatial(event, f"worker path tile is out of bounds: {tile}")
                        elif tile in blocked:
                            record_replay_spatial(event, f"worker path crosses blocking tile: {tile}")
                    for source, target in zip(tiles, tiles[1:]):
                        distance = abs(source[0] - target[0]) + abs(source[1] - target[1])
                        if distance != 1:
                            record_replay_spatial(event, f"worker path jumps {source}->{target}")
                    from_tile = replay_position_tile(payload.get("from"))
                    to_tile = replay_position_tile(payload.get("to"))
                    if from_tile is not None and tiles and tiles[0] != from_tile:
                        record_replay_spatial(event, f"path starts at {tiles[0]}, expected {from_tile}")
                    if to_tile is not None and tiles and tiles[-1] != to_tile:
                        record_replay_spatial(event, f"path ends at {tiles[-1]}, expected {to_tile}")
        if event.get("event_type") == "state_changed" and "machine_state" in attrs:
            machine_state = str(attrs.get("machine_state") or "").upper()
            if attrs.get("wait_visual") and machine_state != "DONE_WAIT_UNLOAD":
                stale_machine_overlay_count += 1
        if event.get("event_type") == "traffic_conflict_detected":
            primary = str(payload.get("primary_worker_id") or refs.get("primary") or "")
            other = str(payload.get("other_worker_id") or "")
            worker_ids = [str(item) for item in payload.get("worker_ids", []) if str(item)]
            if (primary and primary == other) or len(worker_ids) != len(set(worker_ids)):
                self_traffic_conflicts.append(str(event.get("event_id", "")))
        if event.get("event_type") == "state_changed" and _is_worker_id(refs.get("primary")):
            if "humanoid_state" not in attrs:
                # Carry-only visual updates intentionally avoid restating the full
                # HumanoidSim snapshot; the previous snapshot remains authoritative.
                carry_only_keys = {"carrying_item_id", "carrying_item_type", "pose_hint", "cargo"}
                if not set(attrs.keys()).issubset(carry_only_keys):
                    missing_humanoid_state_workers[str(refs.get("primary"))] += 1
    if stale_machine_overlay_count:
        audit.error(f"replay has stale machine wait overlays: {stale_machine_overlay_count}")
    if self_traffic_conflicts:
        audit.error(f"replay has self traffic conflicts: {self_traffic_conflicts[:10]}")
    if missing_humanoid_state_workers:
        audit.warn(f"some worker state_changed events lack humanoid_state: {dict(missing_humanoid_state_workers)}")
    if replay_spatial_issue_count:
        audit.error(
            f"replay spatial continuity violations: {replay_spatial_issue_count} "
            f"examples={replay_spatial_examples}"
        )
    audit.note(f"replay events={len(events)} worker_moves={replay_move_count}")


def check_layout(run_dir: Path, audit: Audit, scenario_type: str = "") -> None:
    layout = _load_json(run_dir / "replay_studio_layout.json", audit)
    nodes = layout.get("nodes", []) if isinstance(layout, dict) else []
    if scenario_type in SHIPYARD_SCENARIOS:
        if not any(isinstance(node, dict) and str(node.get("entity_type") or "") in {"ship_hull", "ship_hull_segment"} for node in nodes):
            audit.error("shipyard layout missing ship hull nodes")
        work_tile_count = sum(
            1
            for node in nodes
            if isinstance(node, dict) and str(node.get("entity_type") or "") == "ship_work_tile"
        )
        if work_tile_count <= 0:
            audit.error("shipyard layout has no ship_work_tile nodes")
    else:
        completed = next((node for node in nodes if isinstance(node, dict) and node.get("entity_id") == "completed_product_buffer"), None)
        if not completed:
            audit.error("layout missing completed_product_buffer node")
        elif completed.get("region_id") != "completed_products_region":
            audit.error(f"completed_product_buffer is in unexpected region: {completed.get('region_id')}")
        if any(isinstance(node, dict) and node.get("entity_id") == "warehouse_buffer" for node in nodes):
            audit.warn("layout still contains warehouse_buffer alias node")
        if scenario_type == "mfg_flow_shop":
            grid = layout.get("grid", {}) if isinstance(layout, dict) else {}
            footprints = grid.get("object_footprints", []) if isinstance(grid, dict) else []
            service_tiles = grid.get("service_tiles", {}) if isinstance(grid, dict) else {}
            docks = [
                row for row in footprints
                if isinstance(row, dict) and str(row.get("object_type", "")) == "charging_dock"
            ]
            workers = [
                node for node in nodes
                if isinstance(node, dict) and _is_worker_id(node.get("entity_id"))
            ]
            if len(docks) != len(workers):
                audit.error(f"mfg_flow_shop dock/worker mismatch: docks={len(docks)} workers={len(workers)}")
            if any(isinstance(row, dict) and str(row.get("object_id", "")) == "battery_rack" for row in footprints):
                audit.error("mfg_flow_shop layout contains battery_rack")
            desks = [
                row for row in footprints
                if isinstance(row, dict) and str(row.get("object_type", "")) == "inspection_desk"
            ]
            if len(desks) != 1:
                audit.error(f"mfg_flow_shop must contain one inspection_desk, got {len(desks)}")
            if len(service_tiles.get("inspection_desk", [])) != 1:
                audit.error("mfg_flow_shop inspection_desk must expose exactly one service tile")
            for worker in workers:
                worker_id = str(worker.get("entity_id", ""))
                dock_tiles = service_tiles.get(f"charging_dock_{worker_id}", [])
                if len(dock_tiles) != 1 or worker.get("tile") != dock_tiles[0]:
                    audit.error(f"mfg_flow_shop {worker_id} does not start on its assigned charging dock")


def check_mfg_flow_shop_contract(run_dir: Path, events: list[dict[str, Any]], audit: Audit) -> None:
    forbidden_event_types = {
        "BATTERY_SWAP",
        "BATTERY_DELIVERED",
        "MACHINE_PM_START",
        "MACHINE_PM_END",
        "ITEM_HANDOFF_STARTED",
        "ITEM_HANDOFF_COMPLETED",
    }
    observed_forbidden = Counter(
        str(event.get("type", "")).strip().upper()
        for event in events
        if str(event.get("type", "")).strip().upper() in forbidden_event_types
    )
    if observed_forbidden:
        audit.error(f"mfg_flow_shop emitted forbidden events: {dict(observed_forbidden)}")
    forbidden_tasks = Counter()
    for event in events:
        if str(event.get("type", "")).strip().upper() != "AGENT_TASK_START":
            continue
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        task_type = str(details.get("task_type", "")).strip().upper()
        task_code = str(details.get("task_code", details.get("humanoid_task_code", ""))).strip().upper()
        if task_type in {"PREVENTIVE_MAINTENANCE", "HANDOVER_ITEM", "BATTERY_SWAP"}:
            forbidden_tasks[task_type] += 1
        if task_code in {"PREVENTIVE_MAINTENANCE", "HANDOVER_ITEM_TO_ROBOT"}:
            forbidden_tasks[task_code] += 1
    if forbidden_tasks:
        audit.error(f"mfg_flow_shop executed forbidden tasks: {dict(forbidden_tasks)}")

    active_desk_owners: set[str] = set()
    max_desk_occupancy = 0
    for event in sorted(events, key=lambda row: float(row.get("t", 0.0) or 0.0)):
        event_type = str(event.get("type", ""))
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        worker_id = str(details.get("worker_id", "")).strip()
        if event_type == "INSPECTION_DESK_OCCUPIED" and worker_id:
            active_desk_owners.add(worker_id)
            max_desk_occupancy = max(max_desk_occupancy, len(active_desk_owners))
        elif event_type == "INSPECTION_DESK_RELEASED" and worker_id:
            active_desk_owners.discard(worker_id)
    if max_desk_occupancy > 1:
        audit.error(f"mfg_flow_shop inspection desk concurrent occupancy={max_desk_occupancy}")

    run_meta = _load_json(run_dir / "run_meta.json", audit)
    stochastic_streams = run_meta.get("stochastic_streams", {}) if isinstance(run_meta, dict) else {}
    if not isinstance(stochastic_streams, dict) or stochastic_streams.get("scheme") != "isolated_v1":
        audit.error("mfg_flow_shop run_meta missing isolated_v1 stochastic stream metadata")
    elif int(stochastic_streams.get("base_seed", -1)) != int(run_meta.get("seed", -2)):
        audit.error("mfg_flow_shop stochastic stream base_seed does not match run seed")
    else:
        required_streams = {"quality", "machine_failure", "humanoid_incident"}
        missing_streams = sorted(required_streams - set(stochastic_streams))
        if missing_streams:
            audit.error(f"mfg_flow_shop stochastic stream metadata missing: {missing_streams}")
    policy = run_meta.get("mfg_flow_shop_task_policy", {}) if isinstance(run_meta, dict) else {}
    supported_modes = {
        "random_feasible_dispatch",
        "immediate_shared",
        "immediate_dedicated_roles",
        "rolling_horizon_shared",
        "rolling_horizon_dedicated_roles",
        "simulation_based_adp",
    }
    decision_mode = str(run_meta.get("decision_mode", "")).strip().lower() if isinstance(run_meta, dict) else ""
    if decision_mode not in supported_modes:
        audit.error(f"mfg_flow_shop unsupported decision mode in run_meta: {decision_mode or '<blank>'}")
    if not isinstance(policy, dict) or not policy:
        audit.error("mfg_flow_shop run_meta missing mfg_flow_shop_task_policy")
        return
    rules = policy.get("rules", []) if isinstance(policy.get("rules", []), list) else []
    expected_roles = {
        1: ("replenish_s1_material", "REPLENISH_MATERIAL (to Station 1)", "REPLENISH_MATERIAL", "exclusive"),
        2: ("replenish_s2_material", "REPLENISH_MATERIAL (to Station 2)", "REPLENISH_MATERIAL", "exclusive"),
        3: ("transfer_s1_to_s2", "TRANSFER (Station 1 to Station 2)", "TRANSFER", "exclusive"),
        4: ("transfer_s2_to_inspection", "TRANSFER (Station 2 to Inspection)", "TRANSFER", "exclusive"),
        5: ("transfer_inspection_to_completed", "TRANSFER (Inspection to CompletedProducts)", "TRANSFER", "exclusive"),
        6: ("dispose_inspection_scrap", "COLLECT_WASTE_OR_SCRAP", "COLLECT_WASTE_OR_SCRAP", "exclusive"),
        7: ("load_s1_material", "LOAD_MACHINE (Station 1, material)", "LOAD_MACHINE", "exclusive"),
        8: ("load_s2_material", "LOAD_MACHINE (Station 2, material)", "LOAD_MACHINE", "exclusive"),
        9: ("load_s2_intermediate", "LOAD_MACHINE (Station 2, intermediate)", "LOAD_MACHINE", "exclusive"),
        10: ("setup_s1", "SETUP_MACHINE (Station 1)", "SETUP_MACHINE", "exclusive"),
        11: ("setup_s2", "SETUP_MACHINE (Station 2)", "SETUP_MACHINE", "exclusive"),
        12: ("unload_s1", "UNLOAD_MACHINE (Station 1)", "UNLOAD_MACHINE", "exclusive"),
        13: ("unload_s2", "UNLOAD_MACHINE (Station 2)", "UNLOAD_MACHINE", "exclusive"),
        14: (
            "load_inspection_desk",
            "LOAD_UNLOAD_TRANSFER_INTERFACE (load inspection desk)",
            "LOAD_UNLOAD_TRANSFER_INTERFACE",
            "exclusive",
        ),
        15: ("inspect_product", "INSPECT_PRODUCT", "INSPECT_PRODUCT", "exclusive"),
        16: (
            "unload_inspection_desk",
            "LOAD_UNLOAD_TRANSFER_INTERFACE (unload inspection desk)",
            "LOAD_UNLOAD_TRANSFER_INTERFACE",
            "exclusive",
        ),
        17: ("battery_charge", "MANAGE_ROBOT_POWER", "MANAGE_ROBOT_POWER", "self_service"),
        18: ("repair_machine", "REPAIR_MACHINE", "REPAIR_MACHINE", "collaborative"),
    }
    rules_by_number = {
        int(row.get("role_number", 0) or 0): row for row in rules if isinstance(row, dict)
    }
    if set(rules_by_number) != set(expected_roles) or len(rules) != 18:
        audit.error(f"mfg_flow_shop role numbers must be exactly 1..18, got {sorted(rules_by_number)}")
    else:
        mismatched_roles = []
        for role_number, expected in expected_roles.items():
            row = rules_by_number[role_number]
            observed = (
                str(row.get("rule_id", "")),
                str(row.get("display_name", "")),
                str(row.get("task_code", "")),
                str(row.get("kind", "")),
            )
            if observed != expected:
                mismatched_roles.append(f"{role_number}:{observed!r}!={expected!r}")
        if mismatched_roles:
            audit.error(f"mfg_flow_shop role definition mismatch: {mismatched_roles[:10]}")
    exclusive_rules = {
        str(row.get("rule_id", "")).strip(): row
        for row in rules
        if isinstance(row, dict) and str(row.get("kind", "exclusive")).strip() == "exclusive"
    }
    if len(exclusive_rules) != 16:
        audit.error(f"mfg_flow_shop expected 16 exclusive task rules, got {len(exclusive_rules)}")
    owner_by_rule = policy.get("owner_by_rule", {}) if isinstance(policy.get("owner_by_rule", {}), dict) else {}
    dedicated = bool(policy.get("dedicated_roles", False))
    workers = policy.get("workers", {}) if isinstance(policy.get("workers", {}), dict) else {}
    worker_ids = sorted(str(worker_id) for worker_id in workers)
    for worker_id, worker_payload in workers.items():
        payload = worker_payload if isinstance(worker_payload, dict) else {}
        assigned = set(str(rule_id) for rule_id in payload.get("assigned_rule_ids", []))
        role_numbers = set(int(number) for number in payload.get("role_numbers", []))
        if not {"battery_charge", "repair_machine"}.issubset(assigned) or not {17, 18}.issubset(role_numbers):
            audit.error(f"mfg_flow_shop worker {worker_id} is missing common roles 17/18")
        if not dedicated and (len(assigned) != 18 or role_numbers != set(range(1, 19))):
            audit.error(f"mfg_flow_shop shared worker {worker_id} does not own all 18 roles")
    if owner_by_rule.get("battery_charge") != "self":
        audit.error("mfg_flow_shop role 17 owner must be self")
    if owner_by_rule.get("repair_machine") != worker_ids:
        audit.error("mfg_flow_shop role 18 owners must include every worker")
    if dedicated:
        missing_owners = sorted(
            rule_id
            for rule_id in exclusive_rules
            if not isinstance(owner_by_rule.get(rule_id), str) or not str(owner_by_rule.get(rule_id, "")).strip()
        )
        if missing_owners:
            audit.error(f"mfg_flow_shop exclusive rules missing owners: {missing_owners}")

    missing_rule_start: list[str] = []
    role_violations: list[str] = []
    for event in events:
        if str(event.get("type", "")).strip().upper() != "AGENT_TASK_START":
            continue
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        selection = details.get("selection", {}) if isinstance(details.get("selection", {}), dict) else {}
        rule_id = str(selection.get("task_rule_id", "")).strip()
        worker_id = str(event.get("entity_id", "")).strip()
        if not rule_id:
            missing_rule_start.append(f"t={event.get('t')} worker={worker_id} task={details.get('task_code')}")
            continue
        if rule_id not in owner_by_rule:
            role_violations.append(f"unknown rule {rule_id} at t={event.get('t')}")
            continue
        rule_row = next((row for row in rules if isinstance(row, dict) and row.get("rule_id") == rule_id), {})
        expected_selection = (
            int(rule_row.get("role_number", 0) or 0),
            str(rule_row.get("task_code", "")),
            str(rule_row.get("display_name", "")),
        )
        observed_selection = (
            int(selection.get("role_number", 0) or 0),
            str(selection.get("role_task_code", "")),
            str(selection.get("role_display_name", "")),
        )
        if observed_selection != expected_selection:
            role_violations.append(
                f"t={event.get('t')} rule={rule_id} metadata={observed_selection!r} expected={expected_selection!r}"
            )
        observed_event = (
            int(details.get("role_number", 0) or 0),
            str(details.get("role_task_code", "")),
            str(details.get("role_display_name", "")),
        )
        if str(details.get("task_rule_id", "")).strip() != rule_id or observed_event != expected_selection:
            role_violations.append(
                f"t={event.get('t')} rule={rule_id} event_metadata={observed_event!r} expected={expected_selection!r}"
            )
        if details.get("role_owner_agent_id") != selection.get("role_owner_agent_id"):
            role_violations.append(
                f"t={event.get('t')} rule={rule_id} event owner differs from selection owner"
            )
        owner = owner_by_rule.get(rule_id)
        if dedicated and rule_id in exclusive_rules and str(owner) != worker_id:
            role_violations.append(
                f"t={event.get('t')} rule={rule_id} worker={worker_id} owner={owner}"
            )
        if rule_id == "battery_charge" and str(owner) == "self" and selection.get("role_owner_agent_id") != worker_id:
            role_violations.append(
                f"t={event.get('t')} self-service battery worker={worker_id} owner={selection.get('role_owner_agent_id')}"
            )
        if rule_id == "repair_machine" and sorted(selection.get("allowed_worker_ids", [])) != worker_ids:
            role_violations.append(
                f"t={event.get('t')} collaborative repair does not allow all workers"
            )
    if missing_rule_start:
        audit.error(f"mfg_flow_shop task starts missing task_rule_id: {missing_rule_start[:10]}")
    for event in events:
        if str(event.get("type", "")).strip().upper() != "AGENT_TASK_END":
            continue
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        selection = details.get("selection", {}) if isinstance(details.get("selection", {}), dict) else {}
        rule_id = str(details.get("task_rule_id", "")).strip()
        rule_row = next((row for row in rules if isinstance(row, dict) and row.get("rule_id") == rule_id), {})
        expected_event = (
            int(rule_row.get("role_number", 0) or 0),
            str(rule_row.get("task_code", "")),
            str(rule_row.get("display_name", "")),
        )
        observed_event = (
            int(details.get("role_number", 0) or 0),
            str(details.get("role_task_code", "")),
            str(details.get("role_display_name", "")),
        )
        if not rule_id or rule_id not in owner_by_rule or observed_event != expected_event:
            role_violations.append(
                f"task end t={event.get('t')} rule={rule_id!r} metadata={observed_event!r} expected={expected_event!r}"
            )
        if selection.get("task_rule_id") != rule_id:
            role_violations.append(f"task end t={event.get('t')} nested/direct rule metadata mismatch")
    if role_violations:
        audit.error(f"mfg_flow_shop role violations: {role_violations[:10]}")



def check_machine_breakdown_contract(
    events: list[dict[str, Any]],
    audit: Audit,
    *,
    require_immediate_repair_candidate: bool = True,
    repair_candidate_deadline_min: float | None = None,
) -> None:
    broken_machines: set[str] = set()
    machine_breaks: list[tuple[str, float]] = []
    immediate_repair_candidates: set[tuple[str, float]] = set()
    repair_candidates_by_machine: dict[str, list[float]] = defaultdict(list)
    completed_setup_while_broken: list[str] = []
    non_repair_dispatch_while_broken: list[str] = []
    rolling_horizon_run = any(
        str(event.get("type", "")).strip().upper() == "ROLLING_HORIZON_WINDOW_START"
        for event in events
    )
    sim_end = max((float(event.get("t", 0.0) or 0.0) for event in events), default=0.0)
    for event in sorted(events, key=lambda row: float(row.get("t", 0.0) or 0.0)):
        event_type = str(event.get("type", "")).strip().upper()
        event_time = float(event.get("t", 0.0) or 0.0)
        machine_id = str(event.get("entity_id", "") or "").strip()
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        if event_type == "MACHINE_BROKEN" and machine_id:
            broken_machines.add(machine_id)
            machine_breaks.append((machine_id, event_time))
            continue
        if event_type == "MACHINE_REPAIRED" and machine_id:
            broken_machines.discard(machine_id)
            continue
        if event_type == "ROLLING_HORIZON_CANDIDATE_COLLECTED" and str(
            details.get("task_code", "")
        ).strip().upper() == "REPAIR_MACHINE":
            signature = details.get("rolling_task_signature", {})
            signature = signature if isinstance(signature, dict) else {}
            repair_machine_id = str(signature.get("machine_id") or signature.get("target_id") or "").strip()
            if repair_machine_id:
                repair_candidates_by_machine[repair_machine_id].append(event_time)
                if str(details.get("collection_trigger", "")).strip().lower() == "machine_broken":
                    immediate_repair_candidates.add((repair_machine_id, event_time))
        if (
            event_type == "MACHINE_SETUP_END"
            and machine_id in broken_machines
            and str(details.get("outcome", "")).strip().lower() == "completed"
        ):
            completed_setup_while_broken.append(f"t={event.get('t')} machine={machine_id}")
        if event_type != "ROLLING_HORIZON_DISPATCH":
            continue
        task_code = str(details.get("task_code", "") or "").strip().upper()
        signature = details.get("rolling_task_signature", {})
        signature = signature if isinstance(signature, dict) else {}
        target_machine_id = str(
            details.get("target_id")
            or signature.get("machine_id")
            or signature.get("target_id")
            or ""
        ).strip()
        if target_machine_id in broken_machines and task_code and task_code != "REPAIR_MACHINE":
            non_repair_dispatch_while_broken.append(
                f"t={event.get('t')} machine={target_machine_id} task={task_code}"
            )

    if completed_setup_while_broken:
        audit.error(f"setup completed while machine was broken: {completed_setup_while_broken[:10]}")
    if non_repair_dispatch_while_broken:
        audit.error(f"non-repair task dispatched to broken machine: {non_repair_dispatch_while_broken[:10]}")
    if rolling_horizon_run and require_immediate_repair_candidate:
        missing_immediate_repairs = [
            f"t={event_time} machine={machine_id}"
            for machine_id, event_time in machine_breaks
            if (machine_id, event_time) not in immediate_repair_candidates
        ]
        if missing_immediate_repairs:
            audit.error(f"machine breakdown missing immediate repair candidate: {missing_immediate_repairs[:10]}")
    if rolling_horizon_run and repair_candidate_deadline_min is not None:
        deadline = max(0.0, float(repair_candidate_deadline_min))
        open_horizon_breaks = [
            (machine_id, event_time)
            for machine_id, event_time in machine_breaks
            if event_time + deadline > sim_end + 1e-6
        ]
        missing_window_repairs = [
            f"t={event_time} machine={machine_id}"
            for machine_id, event_time in machine_breaks
            if event_time + deadline <= sim_end + 1e-6
            if not any(
                event_time - 1e-6 <= candidate_time <= event_time + deadline + 1e-6
                for candidate_time in repair_candidates_by_machine.get(machine_id, [])
            )
        ]
        if open_horizon_breaks:
            audit.note(
                "machine breakdown repair deadlines extend beyond the run horizon: "
                f"{[f't={event_time} machine={machine_id}' for machine_id, event_time in open_horizon_breaks[:10]]}"
            )
        if missing_window_repairs:
            audit.error(
                f"machine breakdown missing repair candidate within {deadline:g} min: "
                f"{missing_window_repairs[:10]}"
            )


def check_strict_periodic_rolling_horizon(
    run_dir: Path,
    events: list[dict[str, Any]],
    audit: Audit,
) -> None:
    window_events = [
        event
        for event in events
        if str(event.get("type", "")).strip().upper() == "ROLLING_HORIZON_WINDOW_START"
    ]
    if not window_events:
        return
    first_details = (
        window_events[0].get("details", {})
        if isinstance(window_events[0].get("details", {}), dict)
        else {}
    )
    scheduler_mode = str(first_details.get("scheduler_mode", "")).strip().lower()
    if scheduler_mode != "strict_periodic":
        audit.error(f"rolling horizon scheduler is not strict_periodic: {scheduler_mode or 'missing'}")
        return
    window_min = float(first_details.get("window_min", 0.0) or 0.0)
    if window_min <= 0.0:
        audit.error("strict rolling horizon has a non-positive window_min")
        return

    periodic_dispatches: list[dict[str, Any]] = []
    immediate_dispatches: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("type", "")).strip().upper() != "ROLLING_HORIZON_DISPATCH":
            continue
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        if details.get("scheduled_boundary_min") is None:
            immediate_dispatches.append(event)
        else:
            periodic_dispatches.append(event)

    if not periodic_dispatches:
        audit.error("strict rolling horizon emitted no periodic dispatch event")
        return
    scheduled_boundaries = sorted(
        {float(event["details"].get("scheduled_boundary_min", -1.0)) for event in periodic_dispatches}
    )
    if abs(scheduled_boundaries[0] - window_min) > 1e-6:
        audit.error(
            f"first strict dispatch is {scheduled_boundaries[0]:g}, expected {window_min:g}"
        )
    invalid_boundaries = [
        value
        for value in scheduled_boundaries
        if value <= 0.0 or abs((value / window_min) - round(value / window_min)) > 1e-7
    ]
    if invalid_boundaries:
        audit.error(f"strict dispatches occurred off periodic boundaries: {invalid_boundaries[:10]}")

    lagged: list[str] = []
    for event in periodic_dispatches:
        details = event.get("details", {})
        scheduled = float(details.get("scheduled_boundary_min", -1.0) or -1.0)
        actual = float(details.get("actual_dispatch_min", -1.0) or -1.0)
        lag = float(details.get("boundary_lag_min", -1.0) or 0.0)
        event_time = float(event.get("t", -1.0) or -1.0)
        if abs(actual - scheduled) > 1e-7 or abs(event_time - actual) > 1e-7 or abs(lag) > 1e-9:
            lagged.append(
                f"scheduled={scheduled:g} actual={actual:g} event={event_time:g} lag={lag:g}"
            )
    if lagged:
        audit.error(f"strict rolling horizon has late/misaligned boundaries: {lagged[:10]}")

    invalid_immediate: list[str] = []
    for event in immediate_dispatches:
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        trigger = str(details.get("collection_trigger", "")).strip().lower()
        task_code = str(details.get("task_code", "")).strip().upper()
        if trigger != "worker_low_battery" or task_code not in {"MANAGE_ROBOT_POWER", "TRANSFER"}:
            invalid_immediate.append(
                f"t={event.get('t')} trigger={trigger or '-'} task={task_code or '-'}"
            )
    if invalid_immediate:
        audit.error(f"non-battery task bypassed strict rolling boundaries: {invalid_immediate[:10]}")

    run_meta_path = run_dir / "run_meta.json"
    if run_meta_path.exists():
        run_meta = _load_json(run_meta_path, audit)
        sim_end = float(
            (run_meta.get("sim_elapsed_min") if isinstance(run_meta, dict) else None)
            or (run_meta.get("sim_total_min") if isinstance(run_meta, dict) else None)
            or 0.0
        )
        if sim_end > 0.0 and any(value >= sim_end - 1e-9 for value in scheduled_boundaries):
            audit.error(
                "strict rolling horizon dispatched at/after the final simulation time: "
                f"sim_end={sim_end:g} boundaries={scheduled_boundaries[-5:]}"
            )

    kpi_path = run_dir / "kpi.json"
    if kpi_path.exists():
        kpi = _load_json(kpi_path, audit)
        rolling = kpi.get("rolling_horizon", {}) if isinstance(kpi, dict) else {}
        rolling = rolling if isinstance(rolling, dict) else {}
        if int(rolling.get("strict_boundary_count", -1) or -1) != len(scheduled_boundaries):
            audit.error(
                "strict boundary KPI mismatch: "
                f"kpi={rolling.get('strict_boundary_count')} events={len(scheduled_boundaries)}"
            )
        if int(rolling.get("late_boundary_count", -1) or 0) != 0:
            audit.error(f"late boundary KPI is nonzero: {rolling.get('late_boundary_count')}")
        if abs(float(rolling.get("max_boundary_lag_min", -1.0) or 0.0)) > 1e-9:
            audit.error(f"max boundary lag KPI is nonzero: {rolling.get('max_boundary_lag_min')}")
    audit.note(
        "strict rolling horizon "
        f"boundaries={len(scheduled_boundaries)} first={scheduled_boundaries[0]:g} "
        f"last={scheduled_boundaries[-1]:g} immediate_battery={len(immediate_dispatches)}"
    )


def audit_run(run_dir: Path, *, require_replay_log: bool = True) -> Audit:
    audit = Audit()
    event_log_enabled = _event_log_enabled(run_dir)
    check_required_files(
        run_dir,
        audit,
        require_replay_log=require_replay_log and event_log_enabled,
        require_event_log=event_log_enabled,
    )
    if not event_log_enabled:
        check_kpi(run_dir, audit)
        check_layout(run_dir, audit, _scenario_type(run_dir))
        audit.note(
            "event log audit skipped because runtime.artifacts.export_events=false; "
            "KPI and compact layout contracts were checked"
        )
        return audit
    events = _iter_events(run_dir / "events.jsonl", audit)
    scenario_type = _scenario_type(run_dir)
    check_kpi(run_dir, audit)
    allow_open_runtime = _allows_open_runtime_events(run_dir)
    check_event_log_consistency(events, audit, allow_open_tasks=allow_open_runtime)
    check_spatial_continuity(run_dir, events, audit, allow_open_moves=allow_open_runtime)
    check_item_transport_continuity(events, audit)
    if scenario_type in MANUFACTURING_SCENARIOS:
        check_inspection_task_lifecycle(events, audit)
    check_gantt(run_dir, events, audit, scenario_type)
    if require_replay_log:
        check_replay_log(run_dir, audit, scenario_type)
    else:
        audit.note("replay log audit skipped; experiment retained core events and compact layout")
    check_layout(run_dir, audit, scenario_type)
    check_strict_periodic_rolling_horizon(run_dir, events, audit)
    if scenario_type in MANUFACTURING_SCENARIOS:
        check_machine_breakdown_contract(
            events,
            audit,
            require_immediate_repair_candidate=False,
            repair_candidate_deadline_min=0.001,
        )
    if scenario_type == "mfg_flow_shop":
        check_mfg_flow_shop_contract(run_dir, events, audit)
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit ManSim run artifacts for dashboard, replay, KPI, and Gantt consistency.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--skip-replay-log", action="store_true")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    audit = audit_run(run_dir, require_replay_log=not args.skip_replay_log)
    print(f"AUDIT {run_dir}")
    for note in audit.notes:
        print(f"NOTE  {note}")
    for warning in audit.warnings:
        print(f"WARN  {warning}")
    for error in audit.errors:
        print(f"ERROR {error}")
    if audit.errors:
        print(f"FAIL errors={len(audit.errors)} warnings={len(audit.warnings)}")
        return 1
    print(f"PASS warnings={len(audit.warnings)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
