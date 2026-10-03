"""Independent checks and optional logged reproductions of a prepared study."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from experiments.mfg_flow_shop_paper.run_evaluation import _run
from experiments.mfg_flow_shop_paper.audit_inventory_results import gantt_state_checks


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def audit_run(row: dict[str, str]) -> dict[str, object]:
    directory = Path(row["run_dir"])
    kpi = read_json(directory / "kpi.json")
    errors: list[str] = []
    def equal(label, actual, expected, tolerance=1e-5):
        if not math.isfinite(float(actual)) or abs(float(actual) - float(expected)) > tolerance:
            errors.append(f"{label}: {actual} != {expected}")

    end = float(kpi["sim_elapsed_min"])
    days = read_json(directory / "daily_summary.json")["days"]
    equal("daily products", sum(day.get("products", 0) for day in days), kpi["total_products"])
    equal("throughput", kpi["throughput_per_sim_hour"], kpi["total_products"] * 60 / end)
    equal("daily average", kpi["avg_daily_products"], kpi["total_products"] / len(days))
    def check_finite(value, key="kpi"):
        if isinstance(value, float) and not math.isfinite(value):
            errors.append(f"nonfinite: {key}")
        elif isinstance(value, dict):
            for name, child in value.items():
                check_finite(child, f"{key}.{name}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                check_finite(child, f"{key}[{index}]")
    check_finite(kpi)
    lanes = defaultdict(list)
    for segment in read_csv(directory / "gantt_segments.csv"):
        lanes[(segment["entity_group"], segment["lane"])].append(segment)
    worker_lanes = 0
    for (group, lane), segments in lanes.items():
        previous = 0.0
        for segment in sorted(segments, key=lambda item: float(item["start"])):
            start, stop = float(segment["start"]), float(segment["end"])
            if group == "Worker":
                equal(f"gantt continuity {lane}", start, previous, 0.002)
            elif start < previous - 0.002:
                errors.append(f"gantt overlapping machine interval: {lane}")
            equal(f"gantt duration {lane}", segment["duration"], stop - start, 0.002)
            if stop < start or stop > end + 0.002:
                errors.append(f"gantt invalid interval: {lane} {start}..{stop}")
            previous = stop
        if group == "Worker":
            equal(f"gantt horizon {lane}", previous, end, 0.002)
            for label, actual, expected in gantt_state_checks(segments, kpi["humanoid_state_time_by_worker"][lane]):
                equal(f"{label} {lane}", actual, expected, 0.01)
        worker_lanes += group == "Worker"
    equal("gantt worker count", worker_lanes, row["worker_count"])
    for name, statuses in (("execution", {"EXECUTING"}), ("blocked", {"BLOCKED"}), ("unavailable", {"OFFLINE", "DISABLED"})):
        metric = f"humanoid_{name}_ratio_avg"
        duration = sum(axes["availability"].get(s, 0) for axes in kpi["humanoid_state_time_by_worker"].values() for s in statuses)
        equal(f"state integral {metric}", duration / (end * worker_lanes), kpi[metric], 2e-6)
    for key in ("buffer_overflow_attempt_count", "buffer_reservation_leak_count", "rolling_horizon_late_boundary_count"):
        if kpi.get(key, 0):
            errors.append(f"{key}={kpi[key]}")
    for path in directory.glob("*.log"):
        content = path.read_text(encoding="utf-8", errors="replace")
        if re.search(r"Traceback \(most recent call last\)|\b(?:RuntimeError|AssertionError|ERROR|CRITICAL):", content):
            errors.append(f"error in {path.name}")
    return {"run_id": row["run_id"], "errors": errors, "gantt_lanes": len(lanes),
            "event_log_available": (directory / "events.jsonl").is_file()}


def reproduce(prepared: Path, output: Path) -> list[dict[str, object]]:
    rows = read_csv(prepared / "evaluation_plan.csv")
    selected = [row for row in rows if (int(row["worker_count"]), int(row["seed"])) == (3, 910001)
                or (row["mode"], int(row["worker_count"]), int(row["seed"])) in {
                    ("simulation_based_adp", 5, 910017), ("simulation_based_adp", 6, 910015),
                    ("random_feasible_dispatch", 2, 910001)}]
    jobs = []
    for source in selected:
        row = dict(source)
        directory = output / "logged_reproductions" / row["run_id"]
        row["run_dir"] = str(directory)
        command = [argument for argument in json.loads(row["command_json"])
                   if not argument.startswith(("hydra.run.dir=", "runtime.artifacts.export_events="))]
        command += ["runtime.artifacts.export_events=true", f"hydra.run.dir={directory.as_posix()}"]
        row["command_json"] = json.dumps(command)
        jobs.append((source, row))
    results = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_run, row, False, True, True): (source, row) for source, row in jobs}
        for future in as_completed(futures):
            source, row = futures[future]
            result = future.result()
            if (Path(row["run_dir"]) / "kpi.json").is_file():
                old = read_json(Path(source["run_dir"]) / "kpi.json")
                new = read_json(Path(row["run_dir"]) / "kpi.json")
                result["original_products"] = old["total_products"]
                result["reproduced_products"] = new["total_products"]
                result["changed_kpi_keys"] = [key for key in old if old[key] != new.get(key)]
            results.append(result)
            (output / "reproduction_report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
            print(json.dumps(result), flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reproduce", action="store_true")
    args = parser.parse_args()
    output = args.output or args.prepared / f"deep_audit_{datetime.now():%Y%m%d_%H%M%S}"
    output.mkdir(parents=True, exist_ok=True)
    if args.reproduce:
        results = reproduce(args.prepared.resolve(), output.resolve())
        return int(any(row["status"] not in {"completed", "skipped_existing"} for row in results))
    results = []
    for index, row in enumerate(read_csv(args.prepared / "evaluation_plan.csv"), 1):
        try:
            results.append(audit_run(row))
        except Exception as exc:
            results.append({"run_id": row["run_id"], "errors": [f"{type(exc).__name__}: {exc}"]})
        if index % 50 == 0:
            print(f"Audited {index} runs", flush=True)
    report = {"run_count": len(results), "failed_run_count": sum(bool(row["errors"]) for row in results), "runs": results}
    (output / "source_artifact_audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "runs"}), flush=True)
    return int(report["failed_run_count"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
