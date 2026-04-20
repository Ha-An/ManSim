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


def approx_equal(left: float, right: float, tolerance: float = 1e-3) -> bool:
    return abs(float(left) - float(right)) <= tolerance


def compare_scalar(findings: list[str], label: str, actual: float | int, expected: float | int, tolerance: float = 1e-3) -> None:
    if isinstance(actual, int) and isinstance(expected, int):
        if actual != expected:
            findings.append(f"{label}: expected {expected}, found {actual}")
        return
    if not approx_equal(float(actual), float(expected), tolerance):
        findings.append(f"{label}: expected {expected}, found {actual}")


def audit_run(output_dir: Path) -> tuple[list[str], dict]:
    kpi = load_json(output_dir / "kpi.json")
    daily = load_json(output_dir / "daily_summary.json").get("days", [])
    snapshots = load_json(output_dir / "minute_snapshots.json").get("snapshots", [])
    events = load_jsonl(output_dir / "events.jsonl")
    sim_end = float(max((event.get("t", 0.0) or 0.0) for event in events) if events else 0.0)
    if sim_end <= 0.0:
        sim_end = float(kpi.get("run_meta", {}).get("sim_time_min", 0.0) or 0.0)
    findings: list[str] = []

    agent_ids = set(kpi.get("agent_discharged_ratio_by_agent", {}).keys())
    machine_ids = set(kpi.get("machine_time_by_machine", {}).keys())
    num_days = len(daily)

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
    task_minutes: dict[str, float] = defaultdict(float)
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
            if decision_source == "worker_local_response":
                worker_local_total += 1
            elif decision_source == "manager_commitment":
                commitment_total += 1
            if str(details.get("status", "")).strip().lower() == "completed":
                task_type = str(details.get("task_type", "")).strip()
                task_minutes[task_type] += float(details.get("duration", 0.0) or 0.0)
    compare_scalar(findings, "incident_event_total", kpi.get("incident_event_total", 0), incident_total, 0.0)
    compare_scalar(findings, "physical_incident_total", kpi.get("physical_incident_total", 0), physical_total, 0.0)
    compare_scalar(findings, "coordination_incident_total", kpi.get("coordination_incident_total", 0), coordination_total, 0.0)
    compare_scalar(findings, "planner_escalation_total", kpi.get("planner_escalation_total", 0), planner_total, 0.0)
    compare_scalar(findings, "worker_local_response_total", kpi.get("worker_local_response_total", 0), worker_local_total, 0.0)
    compare_scalar(findings, "commitment_dispatch_total", kpi.get("commitment_dispatch_total", 0), commitment_total, 0.0)
    for task_type in sorted(set(task_minutes) | set(kpi.get("agent_task_minutes", {}).keys())):
        compare_scalar(
            findings,
            f"agent_task_minutes[{task_type}]",
            float(kpi.get("agent_task_minutes", {}).get(task_type, 0.0) or 0.0),
            round3(task_minutes.get(task_type, 0.0)),
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
    compare_scalar(findings, "agent_availability_ratio", kpi.get("agent_availability_ratio", 0.0), round6(1.0 - (sum(discharged_totals.values()) / total_agent_time)))
    for agent_id in sorted(agent_ids):
        compare_scalar(findings, f"agent_discharged_time_min_by_agent[{agent_id}]", kpi.get("agent_discharged_time_min_by_agent", {}).get(agent_id, 0.0), discharged_totals.get(agent_id, 0.0))
        compare_scalar(findings, f"agent_discharged_ratio_by_agent[{agent_id}]", kpi.get("agent_discharged_ratio_by_agent", {}).get(agent_id, 0.0), round6(discharged_totals.get(agent_id, 0.0) / max(1.0, sim_end)))

    task_intervals = event_intervals(events, {"AGENT_TASK_START"}, {"AGENT_TASK_END"}, agent_ids, sim_end)
    move_intervals = event_intervals(events, {"AGENT_MOVE_START"}, {"AGENT_MOVE_END", "AGENT_MOVE_INTERRUPTED"}, agent_ids, sim_end)
    worker_state = kpi.get("worker_state_time_by_worker", {})
    worker_util = kpi.get("worker_utilization_by_worker", {})
    for agent_id in sorted(agent_ids):
        working_total = max(0.0, interval_total(task_intervals.get(agent_id, [])) - interval_overlap_total(task_intervals.get(agent_id, []), move_intervals.get(agent_id, [])))
        moving_total = interval_total(move_intervals.get(agent_id, []))
        discharged_total = discharged_totals.get(agent_id, 0.0)
        idle_total = max(0.0, sim_end - working_total - moving_total - discharged_total)
        compare_scalar(findings, f"worker_state[{agent_id}].working_min", worker_state.get(agent_id, {}).get("working_min", 0.0), round3(working_total))
        compare_scalar(findings, f"worker_state[{agent_id}].moving_min", worker_state.get(agent_id, {}).get("moving_min", 0.0), round3(moving_total))
        compare_scalar(findings, f"worker_state[{agent_id}].discharged_min", worker_state.get(agent_id, {}).get("discharged_min", 0.0), round3(discharged_total))
        compare_scalar(findings, f"worker_state[{agent_id}].idle_min", worker_state.get(agent_id, {}).get("idle_min", 0.0), round3(idle_total))
        if not approx_equal(sum(float(v) for v in worker_state.get(agent_id, {}).values()), sim_end, 0.01):
            findings.append(f"worker_state_time_by_worker[{agent_id}] does not sum to sim_end")
        active_total = working_total + moving_total
        compare_scalar(findings, f"worker_util[{agent_id}].util_total", worker_util.get(agent_id, {}).get("util_total", 0.0), round6(active_total / max(1.0, sim_end)))
        available_total = max(0.0, sim_end - discharged_total)
        compare_scalar(findings, f"worker_util[{agent_id}].util_available", worker_util.get(agent_id, {}).get("util_available", 0.0), round6((active_total / available_total) if available_total > 0.0 else 0.0))

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
