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
    for key in REQUIRED_KPI_KEYS:
        if key not in kpi:
            audit.error(f"kpi.json missing key: {key}")
    state_axis = kpi.get("humanoid_state_time_by_axis")
    if isinstance(state_axis, dict):
        for axis in ["availability", "mobility", "power", "manipulation"]:
            if axis not in state_axis:
                audit.error(f"kpi humanoid_state_time_by_axis missing axis: {axis}")
    if int(kpi.get("repair_collaboration_time_min", 0) or 0) < 0:
        audit.error("repair_collaboration_time_min is negative")
    audit.note(
        "kpi "
        f"products={kpi.get('total_products', 0)} "
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


def check_gantt(run_dir: Path, events: list[dict[str, Any]], audit: Audit) -> None:
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
    unexpected_groups = groups - {"Worker", "Machine"}
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


def check_replay_log(run_dir: Path, audit: Audit) -> None:
    replay = _load_json(run_dir / "replay_studio_log.json", audit)
    if not isinstance(replay, dict):
        audit.error("replay_studio_log.json root is not an object")
        return
    initial_entities = (
        replay.get("initial_state", {}).get("entities", {})
        if isinstance(replay.get("initial_state", {}), dict)
        else {}
    )
    if "completed_product_buffer" not in initial_entities:
        audit.error("replay initial state missing completed_product_buffer")
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
    for event in events:
        if not isinstance(event, dict):
            continue
        refs = event.get("entity_refs", {})
        refs = refs if isinstance(refs, dict) else {}
        payload = event.get("payload", {})
        payload = payload if isinstance(payload, dict) else {}
        attrs = payload.get("attributes", {})
        attrs = attrs if isinstance(attrs, dict) else {}
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
                missing_humanoid_state_workers[str(refs.get("primary"))] += 1
    if stale_machine_overlay_count:
        audit.error(f"replay has stale machine wait overlays: {stale_machine_overlay_count}")
    if self_traffic_conflicts:
        audit.error(f"replay has self traffic conflicts: {self_traffic_conflicts[:10]}")
    if missing_humanoid_state_workers:
        audit.warn(f"some worker state_changed events lack humanoid_state: {dict(missing_humanoid_state_workers)}")
    audit.note(f"replay events={len(events)}")


def check_layout(run_dir: Path, audit: Audit) -> None:
    layout = _load_json(run_dir / "replay_studio_layout.json", audit)
    nodes = layout.get("nodes", []) if isinstance(layout, dict) else []
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
    check_kpi(run_dir, audit)
    check_gantt(run_dir, events, audit)
    check_replay_log(run_dir, audit)
    check_layout(run_dir, audit)
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
