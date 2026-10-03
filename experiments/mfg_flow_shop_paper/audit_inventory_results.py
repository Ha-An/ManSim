"""Independent saved-artifact checks; deliberate warehouse deadlocks are allowed."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
import csv
import json
import math
from pathlib import Path
import statistics
import time


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def queue_count(snapshot: dict, buffer_id: str) -> int:
    for prefix, key in (
        ("material_queue_", "material_queue_lengths"),
        ("intermediate_queue_", "intermediate_queue_lengths"),
        ("output_buffer_station_", "output_buffer_lengths"),
    ):
        if buffer_id.startswith(prefix):
            station = buffer_id.removeprefix(prefix)
            values = snapshot.get(key, {})
            return int(values.get(station, values.get(int(station), 0)))
    if buffer_id == "inspection_scrap_queue":
        return int(snapshot.get("inspection_scrap_queue_length", 0))
    raise ValueError(buffer_id)


def gantt_state_checks(rows: list[dict], axes: dict) -> list[tuple[str, float, float]]:
    """Legacy CSV loses availability under the charging display overlay."""
    checks = []
    totals = defaultdict(float)
    charging = 0.0
    exact = all(row.get("availability") for row in rows)
    for row in rows:
        duration = float(row["end"]) - float(row["start"])
        if row["status"] == "CHARGING":
            charging += duration
        if exact or row["status"] != "CHARGING":
            totals[row["availability"] if exact else row["status"]] += duration
    checks.append(("gantt_charging_time", charging, axes["power"].get("CHARGING", 0)))
    availability = axes["availability"]
    for state in set(totals) | set(availability):
        actual, expected = totals.get(state, 0), availability.get(state, 0)
        if exact:
            checks.append(("gantt_vs_state_time", actual, expected))
        else:
            checks.append(("gantt_noncharging_excess", max(0, actual - expected), 0))
    if not exact:
        checks.append(("gantt_hidden_availability", sum(availability.values()) - sum(totals.values()), charging))
    return checks


def audit_run(source: dict) -> dict:
    run = Path(source["run_dir"])
    findings: dict[str, dict] = {}

    def fail(code: str, detail):
        row = findings.setdefault(code, {"count": 0, "examples": []})
        row["count"] += 1
        if len(row["examples"]) < 3:
            row["examples"].append(detail)

    def close(code: str, actual: float, expected: float, tolerance=0.005):
        if not math.isfinite(float(actual)) or abs(float(actual) - float(expected)) > tolerance:
            fail(code, {"actual": actual, "expected": expected})

    kpi = read_json(run / "kpi.json")
    horizon = float(kpi["sim_elapsed_min"])
    worker_count = int(source["worker_count"])
    days = read_json(run / "daily_summary.json")["days"]
    close("daily_products", sum(d["products"] for d in days), kpi["total_products"])
    close("daily_scrap", sum(d["scrap"] for d in days), kpi["scrap_count"])
    close("daily_disposed", sum(d["disposed_scrap"] for d in days), kpi["disposed_scrap_count"])
    close("throughput_hour", kpi["throughput_per_sim_hour"], kpi["total_products"] * 60 / horizon)
    close("throughput_day", kpi["avg_daily_products"], kpi["total_products"] / len(days))
    close("incident_partition", kpi["incident_event_total"], kpi["physical_incident_total"] + kpi["coordination_incident_total"])
    for name in ("buffer_overflow_attempt_count", "buffer_reservation_leak_count", "collision_count"):
        if kpi.get(name, 0):
            fail(name, kpi[name])
    state_times = kpi["humanoid_state_time_by_worker"]
    close("worker_count", len(state_times), worker_count)
    for worker, axes in state_times.items():
        for axis, times in axes.items():
            close("state_axis_coverage", sum(times.values()), horizon, 0.01)
            if any(v < 0 for v in times.values()):
                fail("negative_state_time", [worker, axis, times])
        for name, states in (("execution", ("EXECUTING",)), ("blocked", ("BLOCKED",)), ("unavailable", ("OFFLINE", "DISABLED"))):
            expected = sum(axes["availability"].get(s, 0) for s in states) / horizon
            close(f"{name}_ratio", kpi[f"humanoid_{name}_ratio_by_worker"][worker], expected, 2e-6)
    for name in ("execution", "blocked", "unavailable"):
        close(f"{name}_ratio_mean", kpi[f"humanoid_{name}_ratio_avg"], statistics.mean(kpi[f"humanoid_{name}_ratio_by_worker"].values()), 2e-6)
    gantt = read_csv(run / "gantt_segments.csv")
    intervals = defaultdict(list)
    for row in gantt:
        start, end, duration = (float(row[key]) for key in ("start", "end", "duration"))
        if start < -0.002 or end > horizon + 0.002 or end < start:
            fail("gantt_bounds", row)
        close("gantt_duration", duration, end-start, 0.003)
        if row["entity_group"] == "Worker":
            intervals[row["lane"]].append(row)
    for worker, rows in intervals.items():
        prev_end = 0.0
        for row in sorted(rows, key=lambda r: float(r["start"])):
            start, end = float(row["start"]), float(row["end"])
            close("gantt_contiguity", start, prev_end, 0.003)
            prev_end = end
        close("gantt_horizon", prev_end, horizon, 0.003)
        for code, actual, expected in gantt_state_checks(rows, state_times[worker]):
            close(code, actual, expected, 0.05)

    snapshots = read_json(run / "minute_snapshots.json")["snapshots"]
    previous = None
    means = defaultdict(list)
    observed_disabled = set()
    for snapshot in snapshots:
        t = float(snapshot["t"])
        if previous is not None and t <= float(previous["t"]):
            fail("snapshot_time_order", t)
        for buffer_id, capacity in snapshot["buffer_capacities"].items():
            count = queue_count(snapshot, buffer_id)
            reserved = snapshot.get("buffer_reserved_counts", {}).get(buffer_id, 0)
            means[buffer_id].append(count)
            if count < 0 or reserved < 0 or count + reserved > capacity:
                fail("snapshot_buffer_capacity", [t, buffer_id, count, reserved, capacity])
        if not 0 <= snapshot["warehouse_material_shelf_count"] <= snapshot["warehouse_material_shelf_capacity"]:
            fail("warehouse_capacity", t)
        if snapshot.get("inspection_active_agents", 0) > 1:
            fail("inspection_exclusivity", t)
        tiles = snapshot["worker_tiles"]
        if len({(v["x"], v["y"]) for v in tiles.values()}) != len(tiles):
            fail("worker_tile_overlap", [t, tiles])
        for worker, state in snapshot["humanoid_states"].items():
            metadata = state.get("metadata") or {}
            battery = metadata.get("battery_remaining_min")
            capacity = metadata.get("battery_period_min")
            if battery is not None and capacity is not None and not -0.002 <= battery <= capacity + 0.002:
                fail("battery_bounds", [t, worker, battery, capacity])
            if state.get("availability") == "DISABLED":
                observed_disabled.add(worker)
            if previous is not None:
                before = previous["humanoid_states"][worker]
                p, q = previous["worker_tiles"][worker], tiles[worker]
                distance = abs(p["x"]-q["x"]) + abs(p["y"]-q["y"])
                elapsed = t-float(previous["t"])
                returned = before.get("availability") == "DISABLED" and state.get("availability") != "DISABLED"
                if before.get("availability") == state.get("availability") == "DISABLED" and distance:
                    fail("disabled_worker_moved", [t, worker, p, q])
                # At most 12.5 tile edges/minute with the configured 0.08 min minimum.
                if not returned and distance > math.ceil(elapsed/0.08) + 1:
                    fail("snapshot_motion_bound", [t, worker, distance, elapsed])
                if returned:
                    day_len = float(kpi["run_meta"]["minutes_per_day"])
                    boundary = math.floor(t/day_len) * day_len
                    if not float(previous["t"]) <= boundary <= t:
                        fail("recovery_outside_day_boundary", [t, worker])
        previous = snapshot
    close("snapshot_end", snapshots[-1]["t"], horizon)
    for buffer_id, values in means.items():
        close("buffer_snapshot_mean", kpi["buffer_avg_occupancy"][buffer_id], statistics.mean(values), 1e-6)
    return {"run_id": source["run_id"], "run_dir": str(run), "findings": findings,
            "products": kpi["total_products"], "snapshot_count": len(snapshots),
            "workers_observed_disabled": sorted(observed_disabled)}


def audit_statistics(prepared: Path) -> dict:
    """Recompute saved aggregates independently of the dashboard summarizers."""
    errors = []
    checks = 0

    def equal(label, actual, expected, tolerance=1e-6):
        nonlocal checks
        checks += 1
        if actual in (None, "") or not math.isfinite(float(actual)) or abs(float(actual)-expected) > tolerance:
            errors.append({"check": label, "actual": actual, "expected": expected})

    def numbers(rows, metric):
        return [float(r[metric]) for r in rows if r.get(metric) not in (None, "") and math.isfinite(float(r[metric]))]

    raw = read_csv(prepared / "evaluation_raw.csv")
    groups = defaultdict(list)
    by_run = {}
    for row in raw:
        groups[(row["mode"], row["worker_count"])].append(row)
        by_run[str(Path(row["run_dir"]).resolve())] = row
    equal("unique_run_paths", len(by_run), len(raw))
    paper = read_csv(prepared / "policy_worker_summary.csv")
    # These experiments have one ADP checkpoint per fleet, not repeated training.
    for row in paper:
        rows = groups[(row["mode"], row["worker_count"])]
        equal("unique_seed_units", len({r["seed"] for r in rows}), len(rows))
        values = numbers(rows, row["metric"])
        for key, expected in (("mean", statistics.mean(values)), ("std_across_seed_units", statistics.stdev(values) if len(values)>1 else 0),
                              ("min", min(values)), ("max", max(values)), ("environment_seed_count", len(values))):
            equal(f"paper:{row['mode']}:{row['worker_count']}:{row['metric']}:{key}", row[key], expected)
        if not min(values)-1e-6 <= float(row["ci95_low"]) <= float(row["ci95_high"]) <= max(values)+1e-6:
            errors.append({"check": "bootstrap_interval_support", "row": row})
    dash = prepared / "policy_comparison_dashboard"
    display = read_csv(dash / "comparison_summary.csv")
    equal("dashboard_run_count", len(display), len(raw))
    display_groups = defaultdict(list)
    for row in display:
        display_groups[(row["mode"], row["worker_count"])].append(row)
        source = by_run[str(Path(row["run_dir"]).resolve())]
        kpi = read_json(Path(row["run_dir"]) / "kpi.json")
        for metric, value in source.items():
            if metric in kpi and isinstance(kpi[metric], (int, float)) and not isinstance(kpi[metric], bool):
                equal(f"raw_kpi:{source['run_id']}:{metric}", value, kpi[metric])
        for metric, value in row.items():
            if metric in kpi and isinstance(kpi[metric], (int, float)) and not isinstance(kpi[metric], bool):
                equal(f"dashboard_kpi:{source['run_id']}:{metric}", value, kpi[metric])
    summary = read_csv(dash / "mode_worker_summary.csv")
    for row in summary:
        group = display_groups[(row["mode"], row["worker_count"])]
        for key in row:
            if not key.endswith(".mean"):
                continue
            metric = key[:-5]
            values = numbers(group, metric)
            equal(f"dashboard_count:{metric}", row[metric+".count"], len(values))
            if values:
                for suffix, value in (("mean", statistics.mean(values)), ("std", statistics.stdev(values) if len(values)>1 else 0), ("min", min(values)), ("max", max(values))):
                    if suffix == "std" and len(values) == 1 and row[metric+".std"] == "":
                        continue
                    equal(f"dashboard_aggregate:{row['mode']}:{row['worker_count']}:{metric}:{suffix}", row[metric+"."+suffix], value)
            elif any(row.get(metric+"."+s) not in (None, "") for s in ("mean", "std", "min", "max")):
                errors.append({"check": "missing_values_replaced_with_numbers", "metric": metric})
        previous = sorted(int(n) for mode, n in display_groups if mode == row["mode"] and int(n) < int(row["worker_count"]))
        if previous:
            smaller = {r["seed"]: float(r["throughput_per_sim_hour"]) for r in display_groups[(row["mode"], str(previous[-1]))]}
            delta = [(float(r["throughput_per_sim_hour"])-smaller[r["seed"]])/(int(row["worker_count"])-previous[-1]) for r in group if r["seed"] in smaller]
            equal("marginal_mean", row["throughput_per_sim_hour.marginal_gain"], statistics.mean(delta))
            equal("marginal_paired_sd", row["throughput_per_sim_hour.marginal_gain.std"], statistics.stdev(delta))
    for row in read_csv(dash / "mode_summary.csv"):
        group = [r for r in display if r["mode"] == row["mode"]]
        for key in row:
            if key.endswith(".mean"):
                values = numbers(group, key[:-5])
                if values:
                    equal(f"pooled_mean:{row['mode']}:{key}", row[key], statistics.mean(values))
    for row in read_csv(prepared / "paired_contrasts.csv"):
        if row["scope"] != "worker":
            continue
        adp = {r["seed"]: float(r["total_products"]) for r in groups[("simulation_based_adp", row["worker_count"]) ]}
        base = {r["seed"]: float(r["total_products"]) for r in groups[(row["baseline_mode"], row["worker_count"]) ]}
        seeds = sorted(set(adp) & set(base))
        delta = [adp[s]-base[s] for s in seeds]
        equal("paired_mean", row["adp_minus_baseline_mean"], statistics.mean(delta))
        equal("paired_relative", row["relative_improvement_pct"], 100*statistics.mean(delta)/statistics.mean(base[s] for s in seeds))
        for key, expected in (("win_count", sum(x>0 for x in delta)), ("tie_count", sum(x==0 for x in delta)), ("loss_count", sum(x<0 for x in delta)), ("seed_count", len(seeds))):
            equal(key, row[key], expected)
    return {"prepared": str(prepared), "checks": checks, "errors": errors, "metric_groups": len(paper), "run_count": len(raw)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--statistics-only", action="store_true")
    args = parser.parse_args()
    if args.statistics_only:
        reports = [audit_statistics(prepared) for prepared in args.prepared]
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "statistics_audit.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(json.dumps([{k:v for k,v in r.items() if k != "errors"} | {"error_count": len(r["errors"])} for r in reports]), flush=True)
        return int(any(r["errors"] for r in reports))
    sources = []
    for prepared in args.prepared:
        sources.extend(read_csv(prepared / "evaluation_plan.csv"))
    if args.limit is not None:
        sources = sources[:args.limit]
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    totals = Counter()
    with ProcessPoolExecutor(max_workers=args.jobs) as pool, (args.output / "run_audit.jsonl").open("w", encoding="utf-8") as handle:
        for index, row in enumerate(pool.map(audit_run, sources, chunksize=2), 1):
            handle.write(json.dumps(row) + "\n")
            totals.update(row["findings"].keys())
            if index % 100 == 0 or index == len(sources):
                handle.flush()
                print(json.dumps({"checked": index, "total": len(sources), "finding_runs_by_code": totals,
                                  "elapsed_sec": round(time.perf_counter()-started, 1)}), flush=True)
    (args.output / "audit_summary.json").write_text(json.dumps({
        "runs": len(sources), "finding_runs_by_code": totals, "elapsed_sec": time.perf_counter()-started,
        "scope": "Saved snapshots, Gantt, daily totals, KPI consistency. Deliberate deadlocks are allowed. Minute snapshots cannot prove per-edge continuity or item custody."
    }, indent=2), encoding="utf-8")
    return int(bool(totals))


if __name__ == "__main__":
    raise SystemExit(main())
