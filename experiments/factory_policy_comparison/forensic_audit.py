from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


EPS = 1e-6
SUCCESS_STATUSES = {"completed", "skipped_existing"}


@dataclass
class RunResult:
    run_dir: str
    objective_mode: str
    mode: str
    worker_count: int
    seed: int
    events_available: bool = False
    event_count: int = 0
    last_event_min: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.errors


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return data


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _as_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _event_details(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details", {})
    return details if isinstance(details, dict) else {}


def _tile(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict) and "x" in value and "y" in value:
        try:
            return int(value["x"]), int(value["y"])
        except (TypeError, ValueError):
            return None
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _is_worker_id(value: str) -> bool:
    return value.startswith("A") and value[1:].isdigit()


def _discover_runs(output_root: Path) -> list[Path]:
    modern = list(output_root.glob("runs/*/*/workers_*/seed_*"))
    legacy = list(output_root.glob("runs/*/workers_*/seed_*"))
    return sorted({path.resolve() for path in modern + legacy if (path / "kpi.json").exists()})


def _identity(run_dir: Path, run_meta: dict[str, Any]) -> tuple[str, str, int, int]:
    objective = str(run_meta.get("objective_mode") or "scenario_default")
    mode = str(run_meta.get("decision_mode") or "")
    workers = 0
    seed = int(run_meta.get("seed", 0) or 0)
    for part in run_dir.parts:
        if part.startswith("workers_"):
            workers = int(part.removeprefix("workers_"))
        elif part.startswith("seed_") and not seed:
            seed = int(part.removeprefix("seed_"))
    return objective, mode, workers, seed


def _add(result: RunResult, message: str, *, warning: bool = False) -> None:
    target = result.warnings if warning else result.errors
    if len(target) < 1000:
        target.append(message)


def _close_task(
    result: RunResult,
    active: dict[tuple[str, str], float],
    worker_id: str,
    task_id: str,
    event_time: float,
) -> None:
    key = (worker_id, task_id)
    started = active.pop(key, None)
    if started is None:
        _add(result, f"task end without start: worker={worker_id} task={task_id} t={event_time:g}")
    elif event_time + EPS < started:
        _add(result, f"task end precedes start: worker={worker_id} task={task_id}")


def audit_run(run_dir: Path) -> RunResult:
    kpi = _read_json(run_dir / "kpi.json")
    run_meta = _read_json(run_dir / "run_meta.json")
    objective, mode, worker_count, seed = _identity(run_dir, run_meta)
    result = RunResult(str(run_dir), objective, mode, worker_count, seed)
    sim_end = float(kpi.get("sim_elapsed_min", run_meta.get("sim_elapsed_min", 0.0)) or 0.0)
    minutes_per_day = float(run_meta.get("minutes_per_day", 240.0) or 240.0)
    expected_days = int(run_meta.get("configured_throughput_days", 0) or 0)
    terminated = bool(kpi.get("terminated", False))
    allow_open = not terminated

    if str(run_meta.get("scenario_type", "")).strip() == "mfg_flow_shop":
        for key in (
            "buffer_overflow_attempt_count",
            "buffer_reservation_failure_count",
            "buffer_reservation_leak_count",
        ):
            if int(kpi.get(key, 0) or 0) != 0:
                _add(result, f"finite-buffer invariant metric is nonzero: {key}={kpi.get(key)}")
        capacities = kpi.get("buffer_capacities", {})
        combined_max = kpi.get("buffer_max_committed_plus_reserved", {})
        if isinstance(capacities, dict) and isinstance(combined_max, dict):
            for buffer_id, raw_capacity in capacities.items():
                capacity = int(raw_capacity or 0)
                observed = int(combined_max.get(buffer_id, 0) or 0)
                if capacity <= 0 or observed > capacity:
                    _add(
                        result,
                        f"finite-buffer capacity violation: {buffer_id} "
                        f"occupancy+reservations={observed} capacity={capacity}",
                    )

    event_counts: Counter[str] = Counter()
    active_tasks: dict[tuple[str, str], float] = {}
    active_segments: dict[tuple[str, str, int], tuple[tuple[int, int], tuple[int, int]]] = {}
    active_charges: dict[str, dict[str, Any]] = {}
    active_recoveries: set[tuple[str, str]] = set()
    carried_by: dict[str, str] = {}
    desk_item: str | None = None
    desk_results: dict[str, str] = {}
    inspection_times: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    restocks: list[tuple[float, str, int]] = []
    periodic_boundaries: set[float] = set()
    rolling_immediate_invalid: list[str] = []
    completed_products = 0
    disposed_scrap = 0
    charge_time = 0.0
    collision_events = 0
    last_time = -math.inf

    events_path = run_dir / "events.jsonl"
    result.events_available = events_path.exists()
    if not result.events_available:
        _add(
            result,
            "events.jsonl was not exported; event-level lifecycle, movement, and KPI cross-checks were skipped",
            warning=True,
        )
    event_stream = events_path.open(encoding="utf-8") if result.events_available else io.StringIO("")
    with event_stream as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                _add(result, f"invalid JSON at line {line_no}: {exc}")
                continue
            if not isinstance(event, dict):
                _add(result, f"non-object event at line {line_no}")
                continue
            result.event_count += 1
            event_type = str(event.get("type") or event.get("event_type") or "")
            event_counts[event_type] += 1
            details = _event_details(event)
            worker_id = str(event.get("entity_id") or "")
            event_time = _as_float(event.get("t"))
            if event_time is None:
                _add(result, f"missing/non-finite event time at line {line_no}")
                continue
            if event_time + EPS < last_time:
                _add(result, f"event time regressed at line {line_no}: {last_time:g}->{event_time:g}")
            # Event timestamps are exported at millisecond precision while
            # run_meta keeps the full SimPy value.
            if event_time < -0.001 or event_time > sim_end + 0.001:
                _add(result, f"event outside simulation interval at line {line_no}: {event_time:g}/{sim_end:g}")
            last_time = max(last_time, event_time)

            if event_type == "AGENT_TASK_START":
                task_id = str(details.get("task_id") or "")
                key = (worker_id, task_id)
                if not task_id:
                    _add(result, f"task start missing task_id at t={event_time:g}")
                elif key in active_tasks:
                    _add(result, f"overlapping task start: worker={worker_id} task={task_id} t={event_time:g}")
                else:
                    active_tasks[key] = event_time
            elif event_type == "AGENT_TASK_END":
                _close_task(result, active_tasks, worker_id, str(details.get("task_id") or ""), event_time)

            if event_type == "AGENT_MOVE_TILE_START":
                move_id = str(details.get("move_id") or "")
                segment = int(details.get("segment_index", 0) or 0)
                key = (worker_id, move_id, segment)
                source, target = _tile(details.get("from_tile")), _tile(details.get("to_tile"))
                if source is None or target is None:
                    _add(result, f"invalid movement tile: worker={worker_id} move={move_id}:{segment}")
                elif abs(source[0] - target[0]) + abs(source[1] - target[1]) != 1:
                    _add(result, f"non-adjacent movement: worker={worker_id} {source}->{target}")
                if key in active_segments:
                    _add(result, f"duplicate movement segment start: {key}")
                elif source is not None and target is not None:
                    active_segments[key] = (source, target)
            elif event_type in {"AGENT_MOVE_TILE_END", "AGENT_MOVE_TILE_CANCELLED"}:
                move_id = str(details.get("move_id") or "")
                segment = int(details.get("segment_index", 0) or 0)
                key = (worker_id, move_id, segment)
                pair = active_segments.pop(key, None)
                observed = (_tile(details.get("from_tile")), _tile(details.get("to_tile")))
                if pair is None:
                    _add(result, f"movement segment end without start: {key}")
                elif observed != pair:
                    _add(result, f"movement segment pair changed: {key} {pair}->{observed}")

            if event_type == "WORKER_STATE_CHANGED":
                battery = _as_float(details.get("battery_remaining_min"))
                if battery is not None and battery < -EPS:
                    _add(result, f"negative battery: worker={worker_id} t={event_time:g} value={battery:g}")

            if event_type == "ITEM_STATE_CHANGED":
                item_id = str(details.get("item_id") or event.get("entity_id") or "")
                state = str(details.get("item_state") or "")
                ref = str(details.get("ref") or "")
                previous = carried_by.get(item_id)
                if state == "CARRIED_BY_WORKER":
                    if not _is_worker_id(ref):
                        _add(result, f"carried item has invalid worker ref: item={item_id} ref={ref}")
                    elif previous is not None and previous != ref:
                        _add(result, f"item carried by two workers: item={item_id} {previous}/{ref}")
                    if _is_worker_id(ref):
                        carried_by[item_id] = ref
                else:
                    carried_by.pop(item_id, None)

            if event_type == "BATTERY_CHARGE_STARTED":
                expected_dock = f"charging_dock_{worker_id}"
                dock = str(details.get("charging_dock_id") or "")
                start_soc = _as_float(details.get("start_soc"))
                target_soc = _as_float(details.get("target_soc"))
                sampled_full = _as_float(details.get("sampled_full_charge_min"))
                planned = _as_float(details.get("charge_duration_min"))
                if worker_id in active_charges:
                    _add(result, f"overlapping charge session: worker={worker_id} t={event_time:g}")
                if dock != expected_dock or str(event.get("location") or "") != expected_dock:
                    _add(result, f"charge outside assigned dock: worker={worker_id} dock={dock} location={event.get('location')}")
                if start_soc is None or target_soc is None or not (-EPS <= start_soc <= target_soc <= 1.0 + EPS):
                    _add(result, f"invalid charge SOC: worker={worker_id} start={start_soc} target={target_soc}")
                if None not in (start_soc, target_soc, sampled_full, planned):
                    expected_duration = sampled_full * (target_soc - start_soc)
                    if abs(planned - expected_duration) > 2e-5:
                        _add(result, f"charge duration mismatch: worker={worker_id} planned={planned:g} expected={expected_duration:g}")
                active_charges[worker_id] = {
                    "start": event_time,
                    "task_id": str(details.get("task_id") or ""),
                    "dock": dock,
                    "planned": planned or 0.0,
                }
            elif event_type in {"BATTERY_CHARGE_COMPLETED", "BATTERY_CHARGE_INTERRUPTED"}:
                session = active_charges.pop(worker_id, None)
                if session is None:
                    _add(result, f"charge end without start: worker={worker_id} t={event_time:g}")
                else:
                    elapsed = event_time - float(session["start"])
                    if str(details.get("charging_dock_id") or "") != session["dock"]:
                        _add(result, f"charge dock changed: worker={worker_id}")
                    if event_type == "BATTERY_CHARGE_COMPLETED":
                        observed = _as_float(details.get("charge_duration_min")) or 0.0
                        if abs(elapsed - observed) > 0.002 or abs(observed - float(session["planned"])) > 0.002:
                            _add(result, f"completed charge timing mismatch: worker={worker_id}")
                        charge_time += observed
                    else:
                        observed = _as_float(details.get("actual_charge_duration_min")) or 0.0
                        if abs(elapsed - observed) > 0.002 or observed > float(session["planned"]) + 0.002:
                            _add(result, f"interrupted charge timing mismatch: worker={worker_id}")
                        charge_time += observed

            if event_type == "HUMANOID_RECOVERY_START":
                recovery_id = str(details.get("recovery_id") or "")
                key = (worker_id, recovery_id)
                if key in active_recoveries:
                    _add(result, f"duplicate recovery start: {key}")
                active_recoveries.add(key)
            elif event_type == "HUMANOID_RECOVERY_END":
                key = (worker_id, str(details.get("recovery_id") or ""))
                if key not in active_recoveries:
                    _add(result, f"recovery end without start: {key}")
                active_recoveries.discard(key)

            if event_type in {
                "INSPECTION_DESK_ITEM_LOADED",
                "INSPECTION_STARTED",
                "INSPECTION_RESULT_RECORDED",
                "INSPECTION_DESK_ITEM_UNLOADED",
            }:
                product_id = str(details.get("product_id") or event.get("entity_id") or "")
                inspection_times[product_id][event_type].append(event_time)
                if event_type == "INSPECTION_DESK_ITEM_LOADED":
                    if desk_item is not None:
                        _add(result, f"inspection desk double load: {desk_item}/{product_id} t={event_time:g}")
                    desk_item = product_id
                elif event_type == "INSPECTION_STARTED":
                    if desk_item != product_id:
                        _add(result, f"inspection started for non-desk item: desk={desk_item} product={product_id}")
                elif event_type == "INSPECTION_RESULT_RECORDED":
                    inspection_result = str(details.get("inspection_result") or "").upper()
                    if desk_item != product_id or inspection_result not in {"PASS", "FAIL"}:
                        _add(result, f"invalid inspection result: desk={desk_item} product={product_id} result={inspection_result}")
                    if product_id in desk_results:
                        _add(result, f"inspection result sampled twice: product={product_id}")
                    desk_results[product_id] = inspection_result
                else:
                    inspection_result = str(details.get("inspection_result") or "").upper()
                    destination = str(details.get("destination") or "")
                    expected_destination = "inspection_output_queue" if inspection_result == "PASS" else "inspection_scrap_queue"
                    if desk_item != product_id or desk_results.get(product_id) != inspection_result:
                        _add(result, f"inspection unload state mismatch: product={product_id}")
                    if destination != expected_destination:
                        _add(result, f"inspection unload destination mismatch: product={product_id} destination={destination}")
                    desk_item = None

            if event_type == "WAREHOUSE_MATERIAL_RESTOCK":
                restocks.append((event_time, str(details.get("reason") or ""), int(details.get("restocked_count", 0) or 0)))
            elif event_type == "COMPLETED_PRODUCT":
                completed_products += 1
            elif event_type == "SCRAP_DISPOSED":
                disposed_scrap += int(details.get("item_count", 0) or 0)
            elif event_type == "AGENT_TRAFFIC_CONFLICT" and bool(details.get("collision", False)):
                collision_events += 1

            if event_type == "ROLLING_HORIZON_DISPATCH":
                scheduled = _as_float(details.get("scheduled_boundary_min"))
                if scheduled is not None:
                    periodic_boundaries.add(scheduled)
                    actual = _as_float(details.get("actual_dispatch_min"))
                    lag = _as_float(details.get("boundary_lag_min"))
                    if actual is None or abs(actual - scheduled) > EPS or abs(event_time - scheduled) > EPS or (lag is not None and abs(lag) > EPS):
                        _add(result, f"late/misaligned rolling dispatch: scheduled={scheduled} actual={actual} event={event_time}")
                else:
                    trigger = str(details.get("collection_trigger") or "")
                    task_code = str(details.get("task_code") or "")
                    if trigger != "worker_low_battery" or task_code not in {"MANAGE_ROBOT_POWER", "TRANSFER"}:
                        rolling_immediate_invalid.append(f"t={event_time:g}:{trigger}:{task_code}")

    result.last_event_min = 0.0 if last_time == -math.inf else last_time
    result.counts = dict(event_counts)
    if result.events_available and abs(result.last_event_min - sim_end) > 0.002:
        _add(result, f"last event differs from sim end: event={result.last_event_min:g} sim={sim_end:g}")

    for product_id, lifecycle in inspection_times.items():
        for event_type in ("INSPECTION_DESK_ITEM_LOADED", "INSPECTION_RESULT_RECORDED", "INSPECTION_DESK_ITEM_UNLOADED"):
            if len(lifecycle.get(event_type, [])) > 1:
                _add(result, f"duplicate {event_type}: product={product_id}")
        ordered = [
            lifecycle.get("INSPECTION_DESK_ITEM_LOADED", [math.inf])[0],
            lifecycle.get("INSPECTION_STARTED", [math.inf])[0],
            lifecycle.get("INSPECTION_RESULT_RECORDED", [math.inf])[0],
            lifecycle.get("INSPECTION_DESK_ITEM_UNLOADED", [math.inf])[0],
        ]
        finite = [value for value in ordered if math.isfinite(value)]
        if finite != sorted(finite):
            _add(result, f"inspection lifecycle order violation: product={product_id} times={ordered}")

    if result.events_available:
        for label, observed, expected in (
            ("completed products", completed_products, int(kpi.get("total_products", 0) or 0)),
            ("disposed scrap", disposed_scrap, int(kpi.get("disposed_scrap_count", 0) or 0)),
            ("charge starts", event_counts["BATTERY_CHARGE_STARTED"], int(kpi.get("battery_charge_started_count", 0) or 0)),
            ("charge completions", event_counts["BATTERY_CHARGE_COMPLETED"], int(kpi.get("battery_charge_count", 0) or 0)),
            ("humanoid incidents", event_counts["HUMANOID_INCIDENT"], int(kpi.get("humanoid_incident_total", 0) or 0)),
            ("collisions", collision_events, int(kpi.get("collision_count", 0) or 0)),
        ):
            if observed != expected:
                _add(result, f"KPI count mismatch for {label}: events={observed} kpi={expected}")
        if abs(charge_time - float(kpi.get("battery_charge_time_min", 0.0) or 0.0)) > 0.01:
            _add(result, f"KPI charge time mismatch: events={charge_time:g} kpi={kpi.get('battery_charge_time_min')}")
        restocked_count = sum(count for _time, _reason, count in restocks)
        if restocked_count != int(kpi.get("warehouse_material_restock_count", 0) or 0):
            _add(result, f"KPI restock mismatch: events={restocked_count} kpi={kpi.get('warehouse_material_restock_count')}")

    rolling = kpi.get("rolling_horizon", {}) if isinstance(kpi.get("rolling_horizon", {}), dict) else {}
    rolling_enabled = bool(rolling.get("enabled", False))
    if mode.startswith("rolling_horizon_") != rolling_enabled:
        _add(result, f"rolling enabled mismatch: mode={mode} enabled={rolling_enabled}")
    if rolling_enabled and result.events_available:
        window_min = float(rolling.get("window_min", 0.0) or 0.0)
        invalid = [value for value in periodic_boundaries if value <= 0 or abs(value / window_min - round(value / window_min)) > EPS]
        if invalid:
            _add(result, f"off-grid rolling boundaries: {sorted(invalid)[:10]}")
        expected_boundaries = {round(index * window_min, 9) for index in range(1, int(math.ceil(sim_end / window_min))) if index * window_min < sim_end - EPS}
        rounded_observed = {round(value, 9) for value in periodic_boundaries}
        if rounded_observed != expected_boundaries:
            missing = sorted(expected_boundaries - rounded_observed)[:10]
            extra = sorted(rounded_observed - expected_boundaries)[:10]
            _add(result, f"rolling boundary coverage mismatch: missing={missing} extra={extra}")
        if len(periodic_boundaries) != int(rolling.get("strict_boundary_count", 0) or 0):
            _add(result, f"rolling boundary KPI mismatch: events={len(periodic_boundaries)} kpi={rolling.get('strict_boundary_count')}")
        if rolling_immediate_invalid:
            _add(result, f"invalid immediate rolling dispatches: {rolling_immediate_invalid[:5]}")
    elif result.events_available and any(name.startswith("ROLLING_HORIZON_") for name in event_counts):
        _add(result, "immediate policy emitted rolling-horizon events")

    if objective == "maximize_throughput":
        expected_end = expected_days * minutes_per_day
        if abs(sim_end - expected_end) > EPS or str(kpi.get("termination_reason")) != "completed_horizon":
            _add(result, f"throughput termination mismatch: sim={sim_end:g}/{expected_end:g} reason={kpi.get('termination_reason')}")
        if result.events_available:
            expected_times = [round(index * minutes_per_day, 6) for index in range(expected_days)]
            observed_times = [round(time, 6) for time, _reason, _count in restocks]
            if observed_times != expected_times:
                _add(result, f"throughput restock schedule mismatch: observed={observed_times} expected={expected_times}")
    elif objective == "minimize_makespan":
        if str(kpi.get("termination_reason")) != "initial_material_batch_terminal_complete":
            _add(result, f"makespan termination mismatch: {kpi.get('termination_reason')}")
        if result.events_available and (len(restocks) != 1 or abs(restocks[0][0]) > EPS or restocks[0][1] != "initial_fill"):
            _add(result, f"makespan has invalid restocks: {restocks}")
        initial = int(kpi.get("initial_batch_material_count", 0) or 0)
        terminal = int(kpi.get("initial_batch_terminal_material_count", 0) or 0)
        if initial != 30 or terminal != initial or abs(float(kpi.get("initial_batch_progress_ratio", 0.0) or 0.0) - 1.0) > EPS:
            _add(result, f"incomplete makespan batch: initial={initial} terminal={terminal} ratio={kpi.get('initial_batch_progress_ratio')}")
        if result.events_available and desk_item is not None:
            _add(result, f"makespan ended with item on inspection desk: {desk_item}")

    open_counts = {
        "tasks": len(active_tasks),
        "move_segments": len(active_segments),
        "charges": len(active_charges),
        "recoveries": len(active_recoveries),
    }
    if any(open_counts.values()):
        _add(result, f"open runtime records at end: {open_counts}", warning=allow_open)
    return result


def _float_values(rows: Iterable[dict[str, str]], key: str) -> list[float]:
    values = [_as_float(row.get(key)) for row in rows]
    return [value for value in values if value is not None]


def _same_number(actual: str, expected: float | str, tolerance: float = 5e-6) -> bool:
    if expected == "":
        return actual == ""
    value = _as_float(actual)
    return value is not None and abs(value - float(expected)) <= tolerance


def audit_aggregates(output_root: Path) -> list[str]:
    errors: list[str] = []
    raw = [row for row in _read_csv(output_root / "comparison_summary.csv") if row.get("comparison_eligible", "").lower() == "true"]
    worker_rows = _read_csv(output_root / "mode_worker_summary.csv")
    mode_rows = _read_csv(output_root / "mode_summary.csv")
    numeric_metrics = [
        key
        for key in raw[0]
        if key not in {"run_dir", "objective_mode", "mode", "worker_count", "seed", "scenario", "status", "artifact_audit_status", "kpi_audit_status", "fairness_pass", "comparison_eligible", "objective_status", "makespan_status", "timing_profile_fingerprint"}
        and any(_as_float(row.get(key)) is not None for row in raw)
    ] if raw else []

    def validate(summary_rows: list[dict[str, str]], group_fields: tuple[str, ...]) -> None:
        for summary in summary_rows:
            group = [
                row for row in raw
                if all(str(row.get(field, "")) == str(summary.get(field, "")) for field in group_fields)
            ]
            if int(summary.get("comparison_run_count", 0) or 0) != len(group):
                errors.append(f"aggregate count mismatch {group_fields}={tuple(summary.get(f) for f in group_fields)}")
            for metric in numeric_metrics:
                values = _float_values(group, metric)
                expected = {
                    "mean": statistics.fmean(values) if values else "",
                    "std": statistics.stdev(values) if len(values) > 1 else "",
                    "min": min(values) if values else "",
                    "max": max(values) if values else "",
                }
                for stat, target in expected.items():
                    key = f"{metric}.{stat}"
                    if key in summary and not _same_number(summary[key], target):
                        errors.append(f"aggregate mismatch {group_fields}={tuple(summary.get(f) for f in group_fields)} {key}: {summary[key]} != {target}")

    validate(worker_rows, ("objective_mode", "mode", "worker_count"))
    validate(mode_rows, ("objective_mode", "mode"))

    grouped = defaultdict(list)
    for row in worker_rows:
        grouped[(row.get("objective_mode", ""), row.get("mode", ""))].append(row)
    for (objective, mode), rows in grouped.items():
        rows.sort(key=lambda row: int(row.get("worker_count", 0) or 0))
        for previous, current in zip(rows, rows[1:]):
            worker_delta = int(current["worker_count"]) - int(previous["worker_count"])
            if objective == "maximize_throughput":
                expected = (_as_float(current.get("throughput_per_sim_hour.mean")) - _as_float(previous.get("throughput_per_sim_hour.mean"))) / worker_delta
                if not _same_number(current.get("throughput_per_sim_hour.marginal_gain", ""), expected):
                    errors.append(f"marginal throughput mismatch {mode} workers={current['worker_count']}")
            elif objective == "minimize_makespan":
                expected = (_as_float(previous.get("makespan_min.mean")) - _as_float(current.get("makespan_min.mean"))) / worker_delta
                if not _same_number(current.get("makespan_min.marginal_reduction", ""), expected):
                    errors.append(f"marginal makespan mismatch {mode} workers={current['worker_count']}")

    html_path = output_root / "comparison_dashboard.html"
    html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    for token in ("nan", "infinity", "undefined"):
        if token in html.lower():
            errors.append(f"dashboard contains invalid numeric token: {token}")
    throughput_metrics = (
        "throughput_per_sim_hour.mean", "total_products.mean",
        "throughput_per_sim_hour.marginal_gain", "humanoid_incident_total.mean",
        "humanoid_blocked_ratio_avg.mean", "otc.mean",
    )
    makespan_metrics = (
        "makespan_min.mean", "makespan_min.marginal_reduction",
        "initial_batch_yield_ratio.mean", "humanoid_incident_total.mean",
        "humanoid_blocked_ratio_avg.mean", "otc.mean",
    )
    expected_chart_points = sum(
        _as_float(row.get(metric)) is not None
        for row in worker_rows
        for metric in (throughput_metrics if row.get("objective_mode") == "maximize_throughput" else makespan_metrics)
    )
    if html.count("<circle ") != expected_chart_points:
        errors.append(
            f"dashboard chart point count mismatch: {html.count('<circle ')} != {expected_chart_points}"
        )
    for row in _read_csv(output_root / "run_status.csv"):
        if row.get("status") not in SUCCESS_STATUSES:
            errors.append(f"dashboard source contains failed run: {row.get('run_id')}={row.get('status')}")
    return errors


def write_reports(output_root: Path, results: list[RunResult], aggregate_errors: list[str]) -> None:
    report = {
        "output_root": str(output_root),
        "run_count": len(results),
        "passed_run_count": sum(result.passed for result in results),
        "failed_run_count": sum(not result.passed for result in results),
        "warning_run_count": sum(bool(result.warnings) for result in results),
        "aggregate_error_count": len(aggregate_errors),
        "aggregate_errors": aggregate_errors,
        "runs": [asdict(result) | {"passed": result.passed} for result in results],
    }
    (output_root / "forensic_audit_summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    columns = [
        "run_dir", "objective_mode", "mode", "worker_count", "seed", "events_available", "event_count",
        "last_event_min", "passed", "error_count", "warning_count", "errors", "warnings",
    ]
    with (output_root / "forensic_audit_runs.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            writer.writerow({
                **{key: getattr(result, key) for key in columns[:8]},
                "passed": result.passed,
                "error_count": len(result.errors),
                "warning_count": len(result.warnings),
                "errors": " | ".join(result.errors),
                "warnings": " | ".join(result.warnings),
            })


def main() -> int:
    parser = argparse.ArgumentParser(description="Deep, independent audit of policy experiment logs and aggregates.")
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    run_dirs = _discover_runs(output_root)
    results: list[RunResult] = []
    for index, run_dir in enumerate(run_dirs, start=1):
        result = audit_run(run_dir)
        results.append(result)
        if index % 12 == 0 or index == len(run_dirs):
            print(f"audited {index}/{len(run_dirs)} runs; failures={sum(not item.passed for item in results)}", flush=True)
    aggregate_errors = audit_aggregates(output_root)
    write_reports(output_root, results, aggregate_errors)
    failed = sum(not result.passed for result in results)
    print(
        f"forensic audit: runs={len(results)} failed={failed} "
        f"warnings={sum(bool(result.warnings) for result in results)} "
        f"aggregate_errors={len(aggregate_errors)}"
    )
    return 1 if failed or aggregate_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
