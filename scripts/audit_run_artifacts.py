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

MANUFACTURING_SCENARIOS = {"", "factory_mfg_basic"}
SHIPYARD_SCENARIOS = {"shipyard_basic"}
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
                except (TypeError, ValueError):
                    multiplier, duration = 1.0, -1.0
                expected_duration = max(0, len(path) - 1) * tile_time * multiplier
                if duration >= 0.0 and abs(duration - expected_duration) > max(0.002, tile_time * 0.02):
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
                elif item_state != "CARRIED_BY_WORKER":
                    stationary_item_states.add(entity_id)
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
            if item_id not in stationary_item_states:
                record(t, f"item move {item_id} has no destination item state")

    if issue_count:
        audit.error(f"item transport continuity violations: {issue_count} examples={examples}")
    audit.note(f"item transport continuity carried={carry_count} moved={moved_count}")


def check_required_files(run_dir: Path, audit: Audit) -> None:
    for name in REQUIRED_ARTIFACTS:
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
    invalid_worker_statuses = worker_statuses - AVAILABILITY_STATES - {"UNKNOWN"}
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


def audit_run(run_dir: Path) -> Audit:
    audit = Audit()
    check_required_files(run_dir, audit)
    events = _iter_events(run_dir / "events.jsonl", audit)
    scenario_type = _scenario_type(run_dir)
    check_kpi(run_dir, audit)
    allow_open_runtime = _allows_open_runtime_events(run_dir)
    check_event_log_consistency(events, audit, allow_open_tasks=allow_open_runtime)
    check_spatial_continuity(run_dir, events, audit, allow_open_moves=allow_open_runtime)
    check_item_transport_continuity(events, audit)
    check_gantt(run_dir, events, audit, scenario_type)
    check_replay_log(run_dir, audit, scenario_type)
    check_layout(run_dir, audit, scenario_type)
    return audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit ManSim run artifacts for dashboard, replay, KPI, and Gantt consistency.")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    audit = audit_run(run_dir)
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
