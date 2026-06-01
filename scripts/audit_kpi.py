from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean


PHYSICAL_INCIDENTS = {
    "machine_broken",
    "machine_recovered",
    "worker_discharged",
    "worker_low_battery",
    "buffer_blocked",
    "material_starvation",
    "inspection_congestion",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if raw:
                rows.append(json.loads(raw))
    return rows


def round3(value: float) -> float:
    return round(float(value), 3)


def round4(value: float) -> float:
    return round(float(value), 4)


def round6(value: float) -> float:
    return round(float(value), 6)


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted((float(start), float(end)) for start, end in intervals if float(end) > float(start)):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def interval_total(intervals: list[tuple[float, float]]) -> float:
    return sum(max(0.0, end - start) for start, end in intervals)


def interval_overlap_total(left: list[tuple[float, float]], right: list[tuple[float, float]]) -> float:
    a = merge_intervals(left)
    b = merge_intervals(right)
    total = 0.0
    i = 0
    j = 0
    while i < len(a) and j < len(b):
        ls, le = a[i]
        rs, re = b[j]
        start = max(ls, rs)
        end = min(le, re)
        if end > start:
            total += end - start
        if le <= re:
            i += 1
        else:
            j += 1
    return total


def event_intervals(events: list[dict], start_events: set[str], end_events: set[str], valid_entities: set[str], sim_end: float) -> dict[str, list[tuple[float, float]]]:
    rows: dict[str, list[tuple[float, float]]] = {entity_id: [] for entity_id in sorted(valid_entities)}
    active: dict[str, float] = {}
    for event in events:
        event_type = str(event.get("type", "")).strip()
        if event_type not in start_events and event_type not in end_events:
            continue
        entity_id = str(event.get("entity_id", "")).strip()
        if entity_id not in valid_entities:
            continue
        t = float(event.get("t", 0.0) or 0.0)
        if event_type in start_events:
            active[entity_id] = t
        else:
            start = active.pop(entity_id, None)
            if start is not None and t > start:
                rows[entity_id].append((start, t))
    for entity_id, start in active.items():
        if sim_end > start:
            rows[entity_id].append((start, sim_end))
    return {entity_id: merge_intervals(chunks) for entity_id, chunks in rows.items()}


def humanoid_state_time_from_events(events: list[dict], agent_ids: set[str], sim_end: float) -> dict[str, dict[str, dict[str, float]]]:
    axes = ("availability", "mobility", "power", "manipulation")
    current: dict[str, dict] = {
        agent_id: {
            "availability": "AVAILABLE",
            "mobility": "STATIONARY",
            "power": "POWER_NORMAL",
            "manipulation": "FREE",
        }
        for agent_id in sorted(agent_ids)
    }
    last_t: dict[str, float] = {agent_id: 0.0 for agent_id in current}
    totals: dict[str, dict[str, dict[str, float]]] = {
        agent_id: {axis: defaultdict(float) for axis in axes}
        for agent_id in current
    }

    def add_duration(agent_id: str, end_t: float) -> None:
        start_t = float(last_t.get(agent_id, 0.0))
        duration = max(0.0, float(end_t) - start_t)
        if duration <= 0.0:
            return
        state = current.get(agent_id, {})
        for axis in axes:
            value = str(state.get(axis, "") or "").strip() or "UNKNOWN"
            totals[agent_id][axis][value] += duration

    for event in events:
        agent_id = str(event.get("entity_id", "")).strip()
        if agent_id not in current:
            continue
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        humanoid_state = details.get("humanoid_state")
        if not isinstance(humanoid_state, dict):
            continue
        event_t = float(event.get("t", 0.0) or 0.0)
        add_duration(agent_id, event_t)
        current[agent_id] = dict(humanoid_state)
        last_t[agent_id] = event_t

    for agent_id in current:
        add_duration(agent_id, sim_end)

    return {
        agent_id: {
            axis: {state: round3(duration) for state, duration in sorted(axis_totals.items())}
            for axis, axis_totals in axis_map.items()
        }
        for agent_id, axis_map in totals.items()
    }


def humanoid_axis_totals(by_worker: dict[str, dict[str, dict[str, float]]]) -> dict[str, dict[str, float]]:
    rows: dict[str, dict[str, float]] = {axis: defaultdict(float) for axis in ("availability", "mobility", "power", "manipulation")}
    for worker_rows in by_worker.values():
        for axis, state_rows in worker_rows.items():
            if axis not in rows:
                continue
            for state, minutes in state_rows.items():
                rows[axis][state] += float(minutes or 0.0)
    return {axis: {state: round3(minutes) for state, minutes in sorted(state_rows.items())} for axis, state_rows in rows.items()}


def humanoid_state_ratios(by_worker: dict[str, dict[str, dict[str, float]]]) -> dict[str, dict[str, dict[str, float]]]:
    ratios: dict[str, dict[str, dict[str, float]]] = {}
    for worker_id, worker_rows in by_worker.items():
        ratios[worker_id] = {}
        for axis, state_rows in worker_rows.items():
            total = sum(float(value or 0.0) for value in state_rows.values())
            ratios[worker_id][axis] = {
                state: round6((float(minutes or 0.0) / total) if total > 0.0 else 0.0)
                for state, minutes in sorted(state_rows.items())
            }
    return ratios


def humanoid_execution_ratios(by_worker: dict[str, dict[str, dict[str, float]]]) -> dict[str, float]:
    ratios: dict[str, float] = {}
    for worker_id, worker_rows in by_worker.items():
        availability = worker_rows.get("availability", {})
        total = sum(float(value or 0.0) for value in availability.values())
        ratios[worker_id] = round6((float(availability.get("EXECUTING", 0.0) or 0.0) / total) if total > 0.0 else 0.0)
    return ratios


def humanoid_blocked_ratios(by_worker: dict[str, dict[str, dict[str, float]]]) -> dict[str, float]:
    ratios: dict[str, float] = {}
    for worker_id, worker_rows in by_worker.items():
        availability = worker_rows.get("availability", {})
        total = sum(float(value or 0.0) for value in availability.values())
        ratios[worker_id] = round6((float(availability.get("BLOCKED", 0.0) or 0.0) / total) if total > 0.0 else 0.0)
    return ratios


def humanoid_unavailable_ratios(by_worker: dict[str, dict[str, dict[str, float]]]) -> dict[str, float]:
    ratios: dict[str, float] = {}
    for worker_id, worker_rows in by_worker.items():
        availability = worker_rows.get("availability", {})
        total = sum(float(value or 0.0) for value in availability.values())
        unavailable = float(availability.get("DISABLED", 0.0) or 0.0) + float(availability.get("OFFLINE", 0.0) or 0.0)
        ratios[worker_id] = round6((unavailable / total) if total > 0.0 else 0.0)
    return ratios


def approx_equal(left: float, right: float, tolerance: float = 1e-3) -> bool:
    return abs(float(left) - float(right)) <= tolerance


def compare_scalar(findings: list[str], label: str, actual: float | int, expected: float | int, tolerance: float = 1e-3) -> None:
    if isinstance(actual, int) and isinstance(expected, int):
        if actual != expected:
            findings.append(f"{label}: expected {expected}, found {actual}")
        return
    if not approx_equal(float(actual), float(expected), tolerance):
        findings.append(f"{label}: expected {expected}, found {actual}")


def compare_humanoid_state_metrics(
    findings: list[str],
    *,
    kpi: dict,
    events: list[dict],
    agent_ids: set[str],
    sim_end: float,
) -> None:
    expected_humanoid_state = humanoid_state_time_from_events(events, agent_ids, sim_end)
    observed_humanoid_state = kpi.get("humanoid_state_time_by_worker", {})
    for agent_id in sorted(agent_ids):
        expected_worker = expected_humanoid_state.get(agent_id, {})
        observed_worker = observed_humanoid_state.get(agent_id, {}) if isinstance(observed_humanoid_state, dict) else {}
        for axis, expected_states in expected_worker.items():
            observed_states = observed_worker.get(axis, {}) if isinstance(observed_worker, dict) else {}
            if not isinstance(observed_states, dict):
                observed_states = {}
            for state in sorted(set(expected_states) | set(observed_states)):
                compare_scalar(
                    findings,
                    f"humanoid_state_time_by_worker[{agent_id}][{axis}][{state}]",
                    float(observed_states.get(state, 0.0) or 0.0),
                    expected_states.get(state, 0.0),
                    0.01,
                )
            if not approx_equal(sum(float(v) for v in observed_states.values()), sim_end, 0.01):
                findings.append(f"humanoid_state_time_by_worker[{agent_id}][{axis}] does not sum to sim_end")

    expected_axis_totals = humanoid_axis_totals(expected_humanoid_state)
    observed_axis_totals = kpi.get("humanoid_state_time_by_axis", {})
    for axis, expected_states in expected_axis_totals.items():
        observed_states = observed_axis_totals.get(axis, {}) if isinstance(observed_axis_totals, dict) else {}
        observed_states = observed_states if isinstance(observed_states, dict) else {}
        for state in sorted(set(expected_states) | set(observed_states)):
            compare_scalar(
                findings,
                f"humanoid_state_time_by_axis[{axis}][{state}]",
                float(observed_states.get(state, 0.0) or 0.0),
                expected_states.get(state, 0.0),
                0.01,
            )

    expected_ratios = humanoid_state_ratios(expected_humanoid_state)
    observed_ratios = kpi.get("humanoid_state_ratio_by_worker", {})
    for agent_id, worker_rows in expected_ratios.items():
        observed_worker = observed_ratios.get(agent_id, {}) if isinstance(observed_ratios, dict) else {}
        observed_worker = observed_worker if isinstance(observed_worker, dict) else {}
        for axis, expected_states in worker_rows.items():
            observed_states = observed_worker.get(axis, {}) if isinstance(observed_worker.get(axis, {}), dict) else {}
            for state in sorted(set(expected_states) | set(observed_states)):
                compare_scalar(
                    findings,
                    f"humanoid_state_ratio_by_worker[{agent_id}][{axis}][{state}]",
                    float(observed_states.get(state, 0.0) or 0.0),
                    expected_states.get(state, 0.0),
                    2e-6,
                )

    expected_execution_ratios = humanoid_execution_ratios(expected_humanoid_state)
    expected_blocked_ratios = humanoid_blocked_ratios(expected_humanoid_state)
    expected_unavailable_ratios = humanoid_unavailable_ratios(expected_humanoid_state)
    for agent_id in sorted(agent_ids):
        compare_scalar(
            findings,
            f"humanoid_execution_ratio_by_worker[{agent_id}]",
            kpi.get("humanoid_execution_ratio_by_worker", {}).get(agent_id, 0.0),
            expected_execution_ratios.get(agent_id, 0.0),
            1e-6,
        )
        compare_scalar(
            findings,
            f"humanoid_blocked_ratio_by_worker[{agent_id}]",
            kpi.get("humanoid_blocked_ratio_by_worker", {}).get(agent_id, 0.0),
            expected_blocked_ratios.get(agent_id, 0.0),
            1e-6,
        )
        compare_scalar(
            findings,
            f"humanoid_unavailable_ratio_by_worker[{agent_id}]",
            kpi.get("humanoid_unavailable_ratio_by_worker", {}).get(agent_id, 0.0),
            expected_unavailable_ratios.get(agent_id, 0.0),
            1e-6,
        )
    compare_scalar(
        findings,
        "humanoid_execution_ratio_avg",
        kpi.get("humanoid_execution_ratio_avg", 0.0),
        round6(mean(expected_execution_ratios.values()) if expected_execution_ratios else 0.0),
        1e-6,
    )
    compare_scalar(
        findings,
        "humanoid_blocked_ratio_avg",
        kpi.get("humanoid_blocked_ratio_avg", 0.0),
        round6(mean(expected_blocked_ratios.values()) if expected_blocked_ratios else 0.0),
        1e-6,
    )
    compare_scalar(
        findings,
        "humanoid_unavailable_ratio_avg",
        kpi.get("humanoid_unavailable_ratio_avg", 0.0),
        round6(mean(expected_unavailable_ratios.values()) if expected_unavailable_ratios else 0.0),
        1e-6,
    )


def audit_run(output_dir: Path) -> tuple[list[str], dict]:
    kpi = load_json(output_dir / "kpi.json")
    daily = load_json(output_dir / "daily_summary.json").get("days", [])
    snapshots = load_json(output_dir / "minute_snapshots.json").get("snapshots", [])
    events = load_jsonl(output_dir / "events.jsonl")
    sim_end = float(max((event.get("t", 0.0) or 0.0) for event in events) if events else 0.0)
    run_meta = kpi.get("run_meta", {}) if isinstance(kpi.get("run_meta", {}), dict) else {}
    configured_end = float(run_meta.get("sim_total_min") or run_meta.get("sim_time_min") or 0.0)
    termination_reason = str(kpi.get("termination_reason") or "").strip()
    # Some scenario plugins do not emit a terminal event exactly at the horizon.
    # When a run simply reaches the configured horizon, KPI state integration still
    # uses the full configured simulation time, so the audit should too.
    if configured_end > sim_end and (not bool(kpi.get("terminated")) or termination_reason == "completed_horizon"):
        sim_end = configured_end
    if sim_end <= 0.0:
        sim_end = configured_end
    findings: list[str] = []

    agent_ids = set(kpi.get("agent_discharged_ratio_by_agent", {}).keys())
    if not agent_ids and isinstance(kpi.get("humanoid_state_time_by_worker"), dict):
        agent_ids = {str(worker_id) for worker_id in kpi.get("humanoid_state_time_by_worker", {}).keys()}
    machine_ids = set(kpi.get("machine_time_by_machine", {}).keys())
    num_days = len(daily)

    scenario_type = str(kpi.get("scenario_type") or kpi.get("run_meta", {}).get("scenario_type") or "").strip()
    if scenario_type == "shipyard_basic":
        final_surface_state: dict[str, str] = {}
        completed_at: dict[str, float] = {}
        rework_count = 0
        verify_ends = 0
        for event in events:
            event_type = str(event.get("type", "")).strip()
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            if event_type in {"SHIP_TILE_STATE_CHANGED", "SHIP_SECTION_STATE_CHANGED"}:
                tile_id = str(details.get("work_tile_id") or details.get("section_id") or event.get("entity_id") or "").replace("section_", "")
                state = str(details.get("state") or "")
                if tile_id:
                    final_surface_state[tile_id] = state
                    if state == "COMPLETE":
                        completed_at.setdefault(tile_id, float(event.get("t", 0.0) or 0.0))
                    elif state == "REWORK_REQUIRED":
                        rework_count += 1
            elif event_type == "AGENT_TASK_END" and str(details.get("task_code") or details.get("task_type") or "") == "VERIFY_SHIP_SECTION":
                verify_ends += 1
        completed_count = sum(1 for state in final_surface_state.values() if state == "COMPLETE")
        surface_total = int(kpi.get("surface_tile_count", 0) or 0) or max(1, len(final_surface_state))
        expected_terminated = completed_count >= surface_total and surface_total > 0
        makespan = round3(max(completed_at.values(), default=0.0)) if expected_terminated else None
        compare_scalar(findings, "completed_surface_tile_count", kpi.get("completed_surface_tile_count", kpi.get("completed_section_count", 0)), completed_count, 0.0)
        compare_scalar(findings, "completed_section_count", kpi.get("completed_section_count", 0), completed_count, 0.0)
        compare_scalar(findings, "total_products", kpi.get("total_products", 0), completed_count, 0.0)
        compare_scalar(findings, "surface_tile_completion_ratio", kpi.get("surface_tile_completion_ratio", kpi.get("section_completion_ratio", 0.0)), round6(completed_count / surface_total), 1e-6)
        compare_scalar(findings, "section_completion_ratio", kpi.get("section_completion_ratio", 0.0), round6(completed_count / surface_total), 1e-6)
        if expected_terminated:
            compare_scalar(findings, "makespan_min", kpi.get("makespan_min", 0.0), makespan, 0.01)
        elif kpi.get("makespan_min") is not None:
            findings.append(f"makespan_min: expected null before all surface tiles complete, found {kpi.get('makespan_min')}")
        compare_scalar(findings, "rework_count", kpi.get("rework_count", 0), rework_count, 0.0)
        compare_scalar(findings, "quality_pass_rate", kpi.get("quality_pass_rate", 0.0), round6(completed_count / verify_ends if verify_ends else 0.0), 1e-6)
        if kpi.get("terminated") and kpi.get("termination_reason") != "all_ship_surface_tiles_complete":
            findings.append(f"termination_reason: expected all_ship_surface_tiles_complete, found {kpi.get('termination_reason')}")
        compare_humanoid_state_metrics(findings, kpi=kpi, events=events, agent_ids=agent_ids, sim_end=max(makespan or 0.0, sim_end, 1.0))
        report = {
            "output_dir": str(output_dir),
            "finding_count": len(findings),
            "sim_end": sim_end,
            "scenario_type": scenario_type,
        }
        return findings, report

    total_products = sum(1 for event in events if str(event.get("type", "")).strip() == "COMPLETED_PRODUCT")
    scrap_count = sum(1 for event in events if str(event.get("type", "")).strip() == "SCRAP")
    compare_scalar(findings, "total_products", kpi.get("total_products", 0), total_products, 0.0)
    compare_scalar(findings, "scrap_count", kpi.get("scrap_count", 0), scrap_count, 0.0)
    total_checked = total_products + scrap_count
    compare_scalar(findings, "scrap_rate", kpi.get("scrap_rate", 0.0), round6(scrap_count / total_checked if total_checked else 0.0))

    station_throughput: dict[str, int] = defaultdict(int)
    inspection_passes = 0
    for event in events:
        event_type = str(event.get("type", "")).strip()
        if event_type == "MACHINE_END":
            location = str(event.get("location", "")).strip()
            if location.startswith("Station"):
                station = location.replace("Station", "")
                station_throughput[station] += 1
        elif event_type == "INSPECT_PASS":
            inspection_passes += 1
    if dict(sorted(station_throughput.items(), key=lambda item: int(item[0]))) != {
        str(key): int(value) for key, value in sorted(kpi.get("station_throughput", {}).items(), key=lambda item: int(item[0]))
    }:
        findings.append(f"station_throughput mismatch: expected {dict(station_throughput)}, found {kpi.get('station_throughput', {})}")
    expected_stage = {f"S{int(key)}": int(value) for key, value in station_throughput.items()}
    expected_stage["Inspection"] = inspection_passes
    if expected_stage != kpi.get("stage_throughput", {}):
        findings.append(f"stage_throughput mismatch: expected {expected_stage}, found {kpi.get('stage_throughput', {})}")

    compare_scalar(findings, "avg_daily_products", kpi.get("avg_daily_products", 0.0), round4(total_products / max(1, num_days)))
    compare_scalar(findings, "throughput_per_sim_hour", kpi.get("throughput_per_sim_hour", 0.0), round4(total_products / max(1e-6, sim_end / 60.0)))

    if snapshots:
        expected_avg_wip_material = round4(mean(sum(snapshot.get("material_queue_lengths", {}).values()) for snapshot in snapshots))
        expected_avg_wip_intermediate = round4(mean(sum(snapshot.get("intermediate_queue_lengths", {}).values()) for snapshot in snapshots))
        expected_avg_wip_output = round4(mean(sum(snapshot.get("output_buffer_lengths", {}).values()) for snapshot in snapshots))
        compare_scalar(findings, "avg_wip_material", kpi.get("avg_wip_material", 0.0), expected_avg_wip_material)
        compare_scalar(findings, "avg_wip_intermediate", kpi.get("avg_wip_intermediate", 0.0), expected_avg_wip_intermediate)
        compare_scalar(findings, "avg_wip_output", kpi.get("avg_wip_output", 0.0), expected_avg_wip_output)

    processing_active: dict[str, float] = {}
    pm_active: dict[str, float] = {}
    broken_active: dict[str, float] = {}
    repair_active: dict[str, float] = {}
    processing_min: dict[str, float] = {machine_id: 0.0 for machine_id in machine_ids}
    pm_min: dict[str, float] = {machine_id: 0.0 for machine_id in machine_ids}
    broken_pure_min: dict[str, float] = {machine_id: 0.0 for machine_id in machine_ids}
    repair_min: dict[str, float] = {machine_id: 0.0 for machine_id in machine_ids}
    setup_active: dict[str, tuple[str, float]] = {}
    setup_min: dict[str, float] = {machine_id: 0.0 for machine_id in machine_ids}

    for event in events:
        event_type = str(event.get("type", "")).strip()
        entity_id = str(event.get("entity_id", "")).strip()
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        t = float(event.get("t", 0.0) or 0.0)
        if entity_id in machine_ids:
            if event_type == "MACHINE_START":
                processing_active[entity_id] = t
            elif event_type in {"MACHINE_END", "MACHINE_ABORTED"}:
                start = processing_active.pop(entity_id, None)
                if start is not None and t >= start:
                    processing_min[entity_id] += t - start
            elif event_type == "MACHINE_BROKEN":
                broken_active[entity_id] = t
            elif event_type == "MACHINE_REPAIR_START":
                start = broken_active.pop(entity_id, None)
                if start is not None and t >= start:
                    broken_pure_min[entity_id] += t - start
                repair_active[entity_id] = t
            elif event_type == "MACHINE_REPAIRED":
                start_repair = repair_active.pop(entity_id, None)
                if start_repair is not None and t >= start_repair:
                    repair_min[entity_id] += t - start_repair
                start_broken = broken_active.pop(entity_id, None)
                if start_broken is not None and t >= start_broken:
                    broken_pure_min[entity_id] += t - start_broken
            elif event_type == "MACHINE_PM_START":
                pm_active[entity_id] = t
            elif event_type == "MACHINE_PM_END":
                start = pm_active.pop(entity_id, None)
                if start is not None and t >= start:
                    pm_min[entity_id] += t - start
            elif event_type == "MACHINE_SETUP_START":
                setup_id = str(details.get("setup_id", "")).strip() or f"{entity_id}@{t}"
                setup_active[setup_id] = (entity_id, t)
            elif event_type == "MACHINE_SETUP_END":
                setup_id = str(details.get("setup_id", "")).strip() or f"{entity_id}@{t}"
                active = setup_active.pop(setup_id, None)
                if active is not None:
                    setup_machine_id, start = active
                    if t >= start:
                        setup_min[setup_machine_id] += t - start

    for machine_id, start in processing_active.items():
        if sim_end >= start:
            processing_min[machine_id] += sim_end - start
    for machine_id, start in pm_active.items():
        if sim_end >= start:
            pm_min[machine_id] += sim_end - start
    for machine_id, start in broken_active.items():
        if sim_end >= start:
            broken_pure_min[machine_id] += sim_end - start
    for machine_id, start in repair_active.items():
        if sim_end >= start:
            repair_min[machine_id] += sim_end - start
    for setup_machine_id, start in setup_active.values():
        if sim_end >= start:
            setup_min[setup_machine_id] += sim_end - start
    compare_scalar(findings, "machine_processing_min", kpi.get("machine_processing_min", 0.0), round3(sum(processing_min.values())))
    compare_scalar(findings, "machine_pm_min", kpi.get("machine_pm_min", 0.0), round3(sum(pm_min.values())))

    # Current KPI should align with state-level broken-only metric after the recent separation.
    if not approx_equal(float(kpi.get("machine_broken_min", 0.0)), round3(sum(broken_pure_min.values()))):
        findings.append(
            "machine_broken_min mismatch: KPI does not match pure broken time. "
            f"expected {round3(sum(broken_pure_min.values()))}, found {kpi.get('machine_broken_min', 0.0)}"
        )

    if machine_ids:
        total_machine_time = max(1.0, sim_end * len(machine_ids))
        compare_scalar(findings, "machine_utilization", kpi.get("machine_utilization", 0.0), round6(sum(processing_min.values()) / total_machine_time))
        compare_scalar(findings, "machine_pm_ratio", kpi.get("machine_pm_ratio", 0.0), round6(sum(pm_min.values()) / total_machine_time))
        compare_scalar(findings, "machine_broken_ratio", kpi.get("machine_broken_ratio", 0.0), round6(sum(broken_pure_min.values()) / total_machine_time))

    machine_state_times = kpi.get("machine_state_time_by_machine", {})
    for machine_id, state_minutes in machine_state_times.items():
        total_state_time = sum(float(value) for value in state_minutes.values())
        if not approx_equal(total_state_time, sim_end, 0.01):
            findings.append(f"machine_state_time_by_machine[{machine_id}] does not sum to sim_end: {total_state_time} vs {sim_end}")
        compare_scalar(findings, f"{machine_id}.processing state", state_minutes.get("processing", 0.0), round3(processing_min.get(machine_id, 0.0)))
        compare_scalar(findings, f"{machine_id}.pm state", state_minutes.get("pm", 0.0), round3(pm_min.get(machine_id, 0.0)))
        compare_scalar(findings, f"{machine_id}.setup state", state_minutes.get("setup", 0.0), round3(setup_min.get(machine_id, 0.0)))
        compare_scalar(findings, f"{machine_id}.broken state", state_minutes.get("broken", 0.0), round3(broken_pure_min.get(machine_id, 0.0)))
        compare_scalar(findings, f"{machine_id}.under_repair state", state_minutes.get("under_repair", 0.0), round3(repair_min.get(machine_id, 0.0)))

    incident_total = 0
    physical_total = 0
    coordination_total = 0
    planner_total = 0
    worker_local_total = 0
    commitment_total = 0
    humanoid_task_minutes: dict[str, float] = defaultdict(float)
    task_start_sources: dict[str, str] = {}
    for event in events:
        event_type = str(event.get("type", "")).strip()
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        if event_type == "INCIDENT_EVENT":
            incident_total += 1
            incident_class = str(details.get("incident_class", "")).strip().lower()
            if incident_class in PHYSICAL_INCIDENTS:
                physical_total += 1
            else:
                coordination_total += 1
                if str(details.get("escalation_level", "")).strip().lower() == "planner":
                    planner_total += 1
        elif event_type == "AGENT_TASK_START":
            task_id = str(details.get("task_id", "")).strip()
            if task_id:
                task_start_sources[task_id] = str(details.get("decision_source", "")).strip().lower()
        elif event_type == "AGENT_TASK_END":
            task_id = str(details.get("task_id", "")).strip()
            decision_source = task_start_sources.get(task_id, "")
            if str(details.get("status", "")).strip().lower() == "completed":
                if decision_source == "worker_local_response":
                    worker_local_total += 1
                elif decision_source == "manager_commitment":
                    commitment_total += 1
                task_code = str(details.get("task_code") or details.get("task_type") or "").strip()
                if task_code:
                    humanoid_task_minutes[task_code] += float(details.get("duration", 0.0) or 0.0)
    compare_scalar(findings, "incident_event_total", kpi.get("incident_event_total", 0), incident_total, 0.0)
    compare_scalar(findings, "physical_incident_total", kpi.get("physical_incident_total", 0), physical_total, 0.0)
    compare_scalar(findings, "coordination_incident_total", kpi.get("coordination_incident_total", 0), coordination_total, 0.0)
    compare_scalar(findings, "planner_escalation_total", kpi.get("planner_escalation_total", 0), planner_total, 0.0)
    # The persisted totals are daily-summary scoped, so they intentionally do
    # not include tasks interrupted after the last day boundary.
    compare_scalar(findings, "worker_local_response_total", kpi.get("worker_local_response_total", 0), sum(int(day.get("local_response_task_count", 0) or 0) for day in daily), 0.0)
    compare_scalar(findings, "commitment_dispatch_total", kpi.get("commitment_dispatch_total", 0), sum(int(day.get("commitment_dispatch_task_count", 0) or 0) for day in daily), 0.0)
    for task_code in sorted(set(humanoid_task_minutes) | set(kpi.get("humanoid_task_minutes", {}).keys())):
        compare_scalar(
            findings,
            f"humanoid_task_minutes[{task_code}]",
            float(kpi.get("humanoid_task_minutes", {}).get(task_code, 0.0) or 0.0),
            round3(humanoid_task_minutes.get(task_code, 0.0)),
            0.01,
        )

    discharged_intervals: dict[str, list[tuple[float, float]]] = {agent_id: [] for agent_id in agent_ids}
    active_discharged: dict[str, float] = {}
    for event in events:
        event_type = str(event.get("type", "")).strip()
        agent_id = str(event.get("entity_id", "")).strip()
        t = float(event.get("t", 0.0) or 0.0)
        if agent_id not in agent_ids:
            continue
        if event_type == "AGENT_DISCHARGED":
            active_discharged[agent_id] = t
        elif event_type == "AGENT_RECHARGED":
            start = active_discharged.pop(agent_id, None)
            if start is not None and t > start:
                discharged_intervals[agent_id].append((start, t))
    for agent_id, start in active_discharged.items():
        if sim_end > start:
            discharged_intervals[agent_id].append((start, sim_end))
    discharged_totals = {agent_id: round3(interval_total(merge_intervals(chunks))) for agent_id, chunks in discharged_intervals.items()}
    compare_scalar(findings, "agent_discharged_time_min_total", kpi.get("agent_discharged_time_min_total", 0.0), round3(sum(discharged_totals.values())))
    compare_scalar(findings, "agent_discharged_time_min_avg", kpi.get("agent_discharged_time_min_avg", 0.0), round3(sum(discharged_totals.values()) / max(1, len(agent_ids))))
    total_agent_time = max(1.0, sim_end * max(1, len(agent_ids)))
    compare_scalar(findings, "agent_discharged_ratio", kpi.get("agent_discharged_ratio", 0.0), round6(sum(discharged_totals.values()) / total_agent_time))
    for agent_id in sorted(agent_ids):
        compare_scalar(findings, f"agent_discharged_time_min_by_agent[{agent_id}]", kpi.get("agent_discharged_time_min_by_agent", {}).get(agent_id, 0.0), discharged_totals.get(agent_id, 0.0))
        compare_scalar(findings, f"agent_discharged_ratio_by_agent[{agent_id}]", kpi.get("agent_discharged_ratio_by_agent", {}).get(agent_id, 0.0), round6(discharged_totals.get(agent_id, 0.0) / max(1.0, sim_end)))

    expected_humanoid_state = humanoid_state_time_from_events(events, agent_ids, sim_end)
    observed_humanoid_state = kpi.get("humanoid_state_time_by_worker", {})
    for agent_id in sorted(agent_ids):
        expected_worker = expected_humanoid_state.get(agent_id, {})
        observed_worker = observed_humanoid_state.get(agent_id, {}) if isinstance(observed_humanoid_state, dict) else {}
        for axis, expected_states in expected_worker.items():
            observed_states = observed_worker.get(axis, {}) if isinstance(observed_worker, dict) else {}
            if not isinstance(observed_states, dict):
                observed_states = {}
            for state in sorted(set(expected_states) | set(observed_states)):
                compare_scalar(
                    findings,
                    f"humanoid_state_time_by_worker[{agent_id}][{axis}][{state}]",
                    float(observed_states.get(state, 0.0) or 0.0),
                    expected_states.get(state, 0.0),
                    0.01,
                )
            if not approx_equal(sum(float(v) for v in observed_states.values()), sim_end, 0.01):
                findings.append(f"humanoid_state_time_by_worker[{agent_id}][{axis}] does not sum to sim_end")

    expected_axis_totals = humanoid_axis_totals(expected_humanoid_state)
    observed_axis_totals = kpi.get("humanoid_state_time_by_axis", {})
    for axis, expected_states in expected_axis_totals.items():
        observed_states = observed_axis_totals.get(axis, {}) if isinstance(observed_axis_totals, dict) else {}
        observed_states = observed_states if isinstance(observed_states, dict) else {}
        for state in sorted(set(expected_states) | set(observed_states)):
            compare_scalar(
                findings,
                f"humanoid_state_time_by_axis[{axis}][{state}]",
                float(observed_states.get(state, 0.0) or 0.0),
                expected_states.get(state, 0.0),
                0.01,
            )

    expected_ratios = humanoid_state_ratios(expected_humanoid_state)
    observed_ratios = kpi.get("humanoid_state_ratio_by_worker", {})
    for agent_id, worker_rows in expected_ratios.items():
        observed_worker = observed_ratios.get(agent_id, {}) if isinstance(observed_ratios, dict) else {}
        observed_worker = observed_worker if isinstance(observed_worker, dict) else {}
        for axis, expected_states in worker_rows.items():
            observed_states = observed_worker.get(axis, {}) if isinstance(observed_worker.get(axis, {}), dict) else {}
            for state in sorted(set(expected_states) | set(observed_states)):
                compare_scalar(
                    findings,
                    f"humanoid_state_ratio_by_worker[{agent_id}][{axis}][{state}]",
                    float(observed_states.get(state, 0.0) or 0.0),
                    expected_states.get(state, 0.0),
                    1e-6,
                )

    expected_execution_ratios = humanoid_execution_ratios(expected_humanoid_state)
    expected_blocked_ratios = humanoid_blocked_ratios(expected_humanoid_state)
    expected_unavailable_ratios = humanoid_unavailable_ratios(expected_humanoid_state)
    for agent_id in sorted(agent_ids):
        compare_scalar(
            findings,
            f"humanoid_execution_ratio_by_worker[{agent_id}]",
            kpi.get("humanoid_execution_ratio_by_worker", {}).get(agent_id, 0.0),
            expected_execution_ratios.get(agent_id, 0.0),
            1e-6,
        )
        compare_scalar(
            findings,
            f"humanoid_blocked_ratio_by_worker[{agent_id}]",
            kpi.get("humanoid_blocked_ratio_by_worker", {}).get(agent_id, 0.0),
            expected_blocked_ratios.get(agent_id, 0.0),
            1e-6,
        )
        compare_scalar(
            findings,
            f"humanoid_unavailable_ratio_by_worker[{agent_id}]",
            kpi.get("humanoid_unavailable_ratio_by_worker", {}).get(agent_id, 0.0),
            expected_unavailable_ratios.get(agent_id, 0.0),
            1e-6,
        )
    compare_scalar(
        findings,
        "humanoid_execution_ratio_avg",
        kpi.get("humanoid_execution_ratio_avg", 0.0),
        round6(mean(expected_execution_ratios.values()) if expected_execution_ratios else 0.0),
        1e-6,
    )
    compare_scalar(
        findings,
        "humanoid_blocked_ratio_avg",
        kpi.get("humanoid_blocked_ratio_avg", 0.0),
        round6(mean(expected_blocked_ratios.values()) if expected_blocked_ratios else 0.0),
        1e-6,
    )
    compare_scalar(
        findings,
        "humanoid_unavailable_ratio_avg",
        kpi.get("humanoid_unavailable_ratio_avg", 0.0),
        round6(mean(expected_unavailable_ratios.values()) if expected_unavailable_ratios else 0.0),
        1e-6,
    )

    queue_entries: dict[tuple[str, str, str], float] = {}
    output_entries: dict[tuple[str, str], float] = {}
    queue_wait_totals: dict[str, float] = defaultdict(float)
    queue_wait_counts: dict[str, int] = defaultdict(int)
    output_wait_totals: dict[str, float] = defaultdict(float)
    output_wait_counts: dict[str, int] = defaultdict(int)
    queue_wait_totals_by_bucket: dict[str, float] = defaultdict(float)
    queue_wait_counts_by_bucket: dict[str, int] = defaultdict(int)
    metric_queue_names = ("s1_input", "s1_output", "s2_input", "s2_output", "inspection_input", "inspection_output")

    def output_category(buffer_name: str) -> str:
        station = int(str(buffer_name).rsplit("_", 1)[-1])
        return "product_output" if station >= 2 else "intermediate_output"

    def output_bucket(buffer_name: str) -> str | None:
        station = int(str(buffer_name).rsplit("_", 1)[-1])
        return {1: "s1_output", 2: "s2_output", 4: "inspection_output"}.get(station)

    def input_bucket(queue_entity: str, queue_name: str) -> str | None:
        entity = str(queue_entity).strip().lower()
        if entity == "material_queue_1":
            return "s1_input"
        if entity in {"material_queue_2", "intermediate_queue_2"}:
            return "s2_input"
        if entity == "intermediate_queue_4":
            return "inspection_input"
        return None

    for event in events:
        event_type = str(event.get("type", "")).strip()
        details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
        t = float(event.get("t", 0.0) or 0.0)
        if event_type == "QUEUE_PUSH":
            item_id = str(details.get("item_id", "")).strip()
            queue_name = str(details.get("queue", "")).strip().lower()
            queue_entity = str(event.get("entity_id", "")).strip()
            if item_id and queue_name in {"material", "intermediate", "product"} and queue_entity:
                queue_entries[(queue_entity, queue_name, item_id)] = t
        elif event_type == "QUEUE_POP":
            item_id = str(details.get("item_id", "")).strip()
            queue_name = str(details.get("queue", "")).strip().lower()
            queue_entity = str(event.get("entity_id", "")).strip()
            if item_id and queue_name in {"material", "intermediate", "product"} and queue_entity:
                start = queue_entries.pop((queue_entity, queue_name, item_id), None)
                if start is not None and t >= start:
                    category = {"material": "material_input", "intermediate": "intermediate_input", "product": "product_input"}[queue_name]
                    queue_wait_totals[category] += t - start
                    queue_wait_counts[category] += 1
                    bucket = input_bucket(queue_entity, queue_name)
                    if bucket:
                        queue_wait_totals_by_bucket[bucket] += t - start
                        queue_wait_counts_by_bucket[bucket] += 1
        elif event_type == "ITEM_MOVED":
            item_id = str(event.get("entity_id", "")).strip()
            source_name = str(details.get("from", "")).strip()
            dest_name = str(details.get("to", "")).strip()
            if dest_name.startswith("output_buffer_station_"):
                output_entries[(dest_name, item_id)] = t
            if source_name.startswith("output_buffer_station_"):
                start = output_entries.pop((source_name, item_id), None)
                if start is not None and t >= start:
                    category = output_category(source_name)
                    output_wait_totals[category] += t - start
                    output_wait_counts[category] += 1
                    bucket = output_bucket(source_name)
                    if bucket:
                        queue_wait_totals_by_bucket[bucket] += t - start
                        queue_wait_counts_by_bucket[bucket] += 1

    active_wait_totals: dict[str, float] = defaultdict(float)
    active_wait_counts: dict[str, int] = defaultdict(int)
    active_wait_totals_by_bucket: dict[str, float] = defaultdict(float)
    active_wait_counts_by_bucket: dict[str, int] = defaultdict(int)
    for (queue_entity, queue_name, _item_id), start in queue_entries.items():
        if sim_end >= start:
            category = {"material": "material_input", "intermediate": "intermediate_input", "product": "product_input"}[queue_name]
            active_wait_totals[category] += sim_end - start
            active_wait_counts[category] += 1
            bucket = input_bucket(queue_entity, queue_name)
            if bucket:
                active_wait_totals_by_bucket[bucket] += sim_end - start
                active_wait_counts_by_bucket[bucket] += 1
    for (buffer_name, _item_id), start in output_entries.items():
        if sim_end >= start:
            category = output_category(buffer_name)
            active_wait_totals[category] += sim_end - start
            active_wait_counts[category] += 1
            bucket = output_bucket(buffer_name)
            if bucket:
                active_wait_totals_by_bucket[bucket] += sim_end - start
                active_wait_counts_by_bucket[bucket] += 1

    for key in ("material_input", "intermediate_input", "product_input"):
        compare_scalar(
            findings,
            f"buffer_wait_avg_min[{key}]",
            kpi.get("buffer_wait_avg_min", {}).get(key, 0.0),
            round3(queue_wait_totals[key] / queue_wait_counts[key]) if queue_wait_counts[key] else 0.0,
        )
        compare_scalar(
            findings,
            f"buffer_wait_completed_count[{key}]",
            kpi.get("buffer_wait_completed_count", {}).get(key, 0),
            queue_wait_counts[key],
            0.0,
        )
    for key in ("intermediate_output", "product_output"):
        compare_scalar(
            findings,
            f"buffer_wait_avg_min[{key}]",
            kpi.get("buffer_wait_avg_min", {}).get(key, 0.0),
            round3(output_wait_totals[key] / output_wait_counts[key]) if output_wait_counts[key] else 0.0,
        )
        compare_scalar(
            findings,
            f"buffer_wait_completed_count[{key}]",
            kpi.get("buffer_wait_completed_count", {}).get(key, 0),
            output_wait_counts[key],
            0.0,
        )
    for key in metric_queue_names:
        compare_scalar(
            findings,
            f"buffer_wait_avg_min_by_queue[{key}]",
            kpi.get("buffer_wait_avg_min_by_queue", {}).get(key, 0.0),
            round3(queue_wait_totals_by_bucket[key] / queue_wait_counts_by_bucket[key]) if queue_wait_counts_by_bucket[key] else 0.0,
        )
        compare_scalar(
            findings,
            f"buffer_wait_completed_count_by_queue[{key}]",
            kpi.get("buffer_wait_completed_count_by_queue", {}).get(key, 0),
            queue_wait_counts_by_bucket[key],
            0.0,
        )
        inclusive_den = queue_wait_counts_by_bucket[key] + active_wait_counts_by_bucket[key]
        expected_inclusive = round3((queue_wait_totals_by_bucket[key] + active_wait_totals_by_bucket[key]) / inclusive_den) if inclusive_den else 0.0
        compare_scalar(
            findings,
            f"buffer_wait_avg_min_including_open_by_queue[{key}]",
            kpi.get("buffer_wait_avg_min_including_open_by_queue", {}).get(key, 0.0),
            expected_inclusive,
        )

    # Cross-check high-level summary totals against daily_summary.
    compare_scalar(findings, "daily_summary total products", sum(int(day.get("products", 0) or 0) for day in daily), total_products, 0.0)
    compare_scalar(findings, "daily_summary total scrap", sum(int(day.get("scrap", 0) or 0) for day in daily), scrap_count, 0.0)
    compare_scalar(findings, "daily_summary total inspection passes", sum(int(day.get("inspection_passes", 0) or 0) for day in daily), inspection_passes, 0.0)
    compare_scalar(findings, "daily_summary total incidents", sum(int(day.get("incident_event_count", 0) or 0) for day in daily), incident_total, 0.0)

    report = {
        "output_dir": str(output_dir),
        "finding_count": len(findings),
        "sim_end": sim_end,
    }
    return findings, report


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: audit_kpi.py <output_dir> [<output_dir> ...]")
        return 2
    overall_failures = 0
    for raw_path in sys.argv[1:]:
        output_dir = Path(raw_path)
        findings, report = audit_run(output_dir)
        print(f"=== KPI audit: {output_dir} ===")
        print(json.dumps(report, indent=2))
        for finding in findings:
            print(f"- {finding}")
        hard_failures = [finding for finding in findings if not finding.startswith("semantic_warning:")]
        if hard_failures:
            overall_failures += 1
            print(f"RESULT: FAIL ({len(hard_failures)} hard findings)")
        else:
            print("RESULT: PASS")
        print()
    return 1 if overall_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
