"""Compare static fleet workload estimates with archived inventory experiments.

No SimPy execution, learned coefficients, or changes to experiment artifacts.
The estimate is a capacity-planning approximation, not a certified bound.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from experiments.factory_policy_comparison.theoretical_capacity import (  # noqa: E402
    _distribution_value,
    _task_service,
)
from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import TileGridMap  # noqa: E402


DEFAULT_STUDIES = (
    REPO / "experiments/mfg_flow_shop_paper/results/20261004_materials20_adp",
    REPO / "experiments/mfg_flow_shop_paper/results/20261005_150829_materials40_adp",
)


@dataclass(frozen=True)
class Operation:
    code: str
    source: str
    destination: str
    item: str
    count: float


def table(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def csv_out(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def practical_knee(values: dict[int, float], fraction: float = 0.95) -> int:
    """First observed fleet reaching a fraction of the observed maximum.

    This describes the tested interval only; it does not prove saturation.
    """
    threshold = max(values.values()) * fraction
    return min(n for n, value in values.items() if value >= threshold - 1e-9)


def paired_interval(left: dict[int, float], right: dict[int, float]) -> dict:
    if left.keys() != right.keys():
        raise ValueError("Paired comparisons require identical seeds.")
    delta = np.array([right[seed] - left[seed] for seed in sorted(left)])
    rng = np.random.default_rng(20261009)
    means = delta[rng.integers(0, len(delta), size=(10000, len(delta)))].mean(axis=1)
    return {
        "mean_gain": float(delta.mean()),
        "paired_bootstrap_ci95_low": float(np.quantile(means, 0.025)),
        "paired_bootstrap_ci95_high": float(np.quantile(means, 0.975)),
    }


def static_workload(config: dict) -> dict:
    """Expected service, loaded travel, and task-to-task repositioning.

    End/start task types are independently drawn in proportion to recipe
    frequency. This static spatial-mixing assumption is not an observed
    dispatch sequence and is neither a lower nor an upper travel bound.
    """
    scenario = copy.deepcopy(config["scenario"])
    timing = config["task_primitive_timing"]
    scenario.setdefault("map", {})["tile_time_min"] = _distribution_value(
        timing["movement"]["per_tile_min"], "expected"
    )
    machines = int(scenario["factory"]["machines_per_station"])
    grid = TileGridMap.from_world_config(
        scenario, stations=(1, 2), machines_per_station=machines
    )
    quality = 1.0 - float(scenario["quality"]["defect_prob"])
    stock = int(scenario["warehouse"]["material_shelf"]["initial_fill"])
    restock = int(scenario["objective"]["throughput"]["restock_target_fill"])
    if stock != restock or stock <= 0:
        raise ValueError("This analysis requires equal positive initial and daily fill targets.")
    operations: list[Operation] = []

    def add(code: str, source: str, destination: str, item="", count=1.0) -> None:
        operations.append(Operation(code, source, destination, item, count))

    process = {}
    for station in (1, 2):
        process[station] = _distribution_value(
            scenario["factory"]["processing_time"][f"station{station}"], "expected"
        )
        for slot in range(1, stock + 1):
            source = f"warehouse_material_slot_{slot:02d}"
            if source not in grid.objects:
                raise ValueError(f"Missing material slot: {source}")
            add("REPLENISH_MATERIAL", source, f"material_queue_{station}", "material", 1 / stock)
        for index in range(1, machines + 1):
            machine = f"S{station}M{index}"
            count = 1 / machines
            add("LOAD_MACHINE", f"material_queue_{station}", machine, "material", count)
            if station == 2:
                add("LOAD_MACHINE", "intermediate_queue_2", machine, "intermediate", count)
            add("SETUP_MACHINE", machine, machine, count=count)
            add("UNLOAD_MACHINE", machine, f"output_buffer_station_{station}",
                "intermediate" if station == 1 else "product", count)

    add("TRANSFER", "output_buffer_station_1", "intermediate_queue_2", "intermediate")
    add("TRANSFER", "output_buffer_station_2", "intermediate_queue_4", "product")
    add("LOAD_UNLOAD_TRANSFER_INTERFACE", "intermediate_queue_4", "inspection_desk", "product")
    add("INSPECT_PRODUCT", "inspection_desk", "inspection_desk")
    for weight, queue, code, destination in (
        (quality, "inspection_output_queue", "TRANSFER", "warehouse_buffer"),
        (1 - quality, "inspection_scrap_queue", "COLLECT_WASTE_OR_SCRAP", "scrap_disposal_bin"),
    ):
        add("LOAD_UNLOAD_TRANSFER_INTERFACE", "inspection_desk", queue, "product", weight)
        # One scrap per collection is a conservative no-batching approximation.
        add(code, queue, destination, "product", weight)

    production_count = sum(op.count for op in operations)
    failure = scenario["machine_failure"]
    if failure["time_basis"] != "active_processing":
        raise ValueError("Only processing-exposure-based failure is supported.")
    pm = failure["preventive_maintenance"]
    for station in (1, 2):
        for index in range(1, machines + 1):
            machine = f"S{station}M{index}"
            # Use unprotected hazard, not perpetual full PM protection.
            count = process[station] / machines / float(failure["mean_processing_time_to_failure_min"])
            add("REPAIR_MACHINE", machine, machine, count=count)
            if pm["enabled"]:
                count = process[station] / machines / float(pm["due_processing_min"])
                add("PREVENTIVE_MAINTENANCE", machine, machine, count=count)

    services = {
        op.code: _task_service(timing, op.code, basis="expected") for op in operations
    }
    multipliers = timing["movement"]["multipliers"]
    service = sum(op.count * services[op.code] for op in operations)
    loaded = sum(
        op.count * grid.travel_time(op.source, op.destination) * multipliers.get(op.item, 1.0)
        for op in operations
    )
    total_count = sum(op.count for op in operations)
    sources: dict[str, float] = defaultdict(float)
    destinations: dict[str, float] = defaultdict(float)
    for op in operations:
        sources[op.source] += op.count
        destinations[op.destination] += op.count
    empty = sum(
        end_count * start_count * grid.travel_time(end, start)
        for end, end_count in destinations.items()
        for start, start_count in sources.items()
    ) / total_count
    support = sum(
        op.count * services[op.code] for op in operations
        if op.code in {"REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"}
    )
    return {
        "production_tasks_per_attempt": production_count,
        "total_task_frequency_per_attempt": total_count,
        "service_min": service,
        "maintenance_service_min": support,
        "loaded_travel_min": loaded,
        "empty_reposition_min": empty,
        "total_worker_min_per_attempt": service + loaded + empty,
        "direct_worker_min_per_attempt": service + loaded,
        "processing_mean_min": process,
        "source": "archived configuration, task timing, and static layout only",
        "assumptions": [
            "Independent recipe-frequency-weighted task endpoints; no fitted coefficient.",
            "Uniform access to the first initial_fill shelf slots; residual-slot selection may differ.",
            "Static routes exclude path reservations, traffic, and permanent warehouse deadlock.",
            "Repair uses solo service and unprotected hazard; PM uses long-run processing exposure.",
            "Scrap batches of size one; actual batching can reduce work.",
            "Charging availability and startup/resource cycles reuse the archived analytic report.",
            "Finite buffers are NOT modeled dynamically; policy and blocking effects are excluded.",
            "Daily top-ups supply at most target_fill, not necessarily target_fill new items.",
        ],
    }


def predict(capacity_rows: list[dict], workload: dict) -> list[dict]:
    result = []
    for row in capacity_rows:
        n = int(row["worker_count"])
        horizon = float(row["horizon_min"])
        first = float(row["expected_first_product_min"])
        material_cap = float(row["material_capacity_bound"])
        resource_rate = 1 / max(
            float(row[key]) for key in (
                "expected_station1_cycle_min", "expected_station2_cycle_min",
                "expected_inspection_cycle_min",
            )
        )
        availability = float(row["battery_duty_fraction"])
        work = workload["total_worker_min_per_attempt"]
        # Needed average attempt rate to exhaust the finite batch by H.
        target_rate = min(resource_rate, max(0.0, material_cap - 1) / (horizon - first))
        fluid_fleet = target_rate * work / availability
        predicted = []
        for worker_work in (work, workload["direct_worker_min_per_attempt"]):
            cycle_rate = min(resource_rate, n * availability / worker_work)
            attempts = min(material_cap, max(0.0, 1 + (horizon - first) * cycle_rate))
            predicted.append(float(row["quality_yield"]) * attempts)
        result.append({
            "worker_count": n,
            "fluid_fleet_crossover": fluid_fleet,
            "rounded_fleet_crossover": math.ceil(fluid_fleet),
            "material_nominal_good_ceiling": material_cap * float(row["quality_yield"]),
            "old_pipeline_products": float(row["expected_pipeline_good_products"]),
            "old_operational_products": float(row["realistic_expected_products"]),
            "revised_predicted_products": predicted[0],
            "revised_without_empty_travel_products": predicted[1],
            "battery_availability": availability,
            "non_humanoid_resource_attempts_per_min": resource_rate,
            "supply_attempts_per_min": material_cap / horizon,
        })
    return result


def analyze(study: Path) -> dict:
    rows = table(study / "evaluation_raw.csv")
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in rows:
        if row["artifact_audit"] != "pass" or row["kpi_audit"] != "pass":
            raise ValueError(f"Failed run audit: {row['run_id']}")
        groups[row["mode"], int(row["worker_count"])].append(row)
    summaries = {
        (row["mode"], int(row["worker_count"])): row
        for row in table(study / "policy_worker_summary.csv") if row["metric"] == "total_products"
    }
    if groups.keys() != summaries.keys() or len(groups) != 30:
        raise ValueError("Expected six policies and five fleet sizes.")
    means: list[dict] = []
    gains: list[dict] = []
    knees: list[dict] = []
    for (mode, n), group in sorted(groups.items()):
        seeds = [int(row["seed"]) for row in group]
        if sorted(seeds) != list(range(910001, 910101)):
            raise ValueError(f"Missing or duplicate seed: {mode}/{n}")
        products = [float(row["total_products"]) for row in group]
        mean, std = statistics.mean(products), statistics.stdev(products)
        archived = summaries[mode, n]
        if abs(mean - float(archived["mean"])) > 1e-7 or abs(std - float(archived["std_across_seed_units"])) > 1e-7:
            raise ValueError(f"Summary mismatch: {mode}/{n}")
        means.append({
            "mode": mode, "worker_count": n, "seed_count": len(group),
            "products_mean": mean, "products_sd": std,
            **{key: statistics.mean(float(r[key]) for r in group) for key in (
                "humanoid_execution_ratio_avg", "humanoid_blocked_ratio_avg",
                "humanoid_unavailable_ratio_avg",
            )},
        })
    for mode in sorted({key[0] for key in groups}):
        curve = {r["worker_count"]: r["products_mean"] for r in means if r["mode"] == mode}
        knees.append({"mode": mode, "observed_95pct_knee": practical_knee(curve),
                      "observed_peak_worker_count": max(curve, key=curve.get),
                      "observed_peak_products": max(curve.values())})
        for n in range(2, 6):
            left = {int(r["seed"]): float(r["total_products"]) for r in groups[mode, n]}
            right = {int(r["seed"]): float(r["total_products"]) for r in groups[mode, n + 1]}
            gain = paired_interval(left, right)
            gains.append({"mode": mode, "from_workers": n, "to_workers": n + 1,
                          **gain, "relative_gain_percent": 100 * gain["mean_gain"] / curve[n]})
    representative = groups["immediate_shared", 2][0]
    cfg_path = Path(representative["run_dir"]) / ".hydra/config.yaml"
    config = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    capacity = json.loads((study / "theoretical_capacity.json").read_text(encoding="utf-8"))
    workload = static_workload(config)
    predictions = predict(capacity["rows"], workload)
    errors = []
    for mode in sorted({key[0] for key in groups}):
        actual = {r["worker_count"]: r["products_mean"] for r in means if r["mode"] == mode}
        for model in ("old_pipeline_products", "old_operational_products", "revised_predicted_products"):
            errors.append({"mode": mode, "model": model,
                           "mae_products": statistics.mean(abs(p[model] - actual[p["worker_count"]]) for p in predictions),
                           "mape_percent": 100 * statistics.mean(abs(p[model] - actual[p["worker_count"]]) / actual[p["worker_count"]] for p in predictions)})
    # Operational evidence only, explicitly excluded from predictive inputs.
    supply_evidence = []
    for n in range(2, 7):
        kpis = [json.loads((Path(r["run_dir"]) / "kpi.json").read_text(encoding="utf-8"))
                for r in groups["immediate_shared", n]]
        supply_evidence.append({"worker_count": n, **{
            key: statistics.mean(float(k[key]) for k in kpis) for key in (
                "warehouse_material_restock_count", "warehouse_material_shelf_count",
                "total_products", "scrap_count", "disposed_scrap_count", "machine_utilization",
            )
        }})
    return {
        "study": str(study.resolve()), "archived_config": str(cfg_path),
        "runs_checked": len(rows), "summary_checks_passed": len(means) * 2,
        "no_simulation_executed": True, "no_coefficients_fitted": True,
        "prior_formula_provenance": "Archived theoretical_capacity.json; no standalone historical saturation formula found.",
        "knee_definition": "First N with >=95% of maximum mean in tested N=2..6; not proof of global saturation.",
        "workload": workload, "predictions": predictions, "observed": means,
        "knees": knees, "paired_marginals": gains, "prediction_errors": errors,
        "supply_evidence_not_used_for_prediction": supply_evidence,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studies", nargs="+", type=Path, default=list(DEFAULT_STUDIES))
    parser.add_argument("--output", type=Path, default=REPO / "outputs/analysis/inventory_saturation_20261009")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for study in args.studies:
        result = analyze(study)
        prefix = args.output / study.name
        prefix.with_suffix(".json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        for key in ("predictions", "observed", "knees", "paired_marginals", "prediction_errors"):
            csv_out(args.output / f"{study.name}_{key}.csv", result[key])
        print(json.dumps({"study": study.name, "workload": result["workload"],
                          "predictions": result["predictions"], "knees": result["knees"],
                          "errors_immediate_shared": [r for r in result["prediction_errors"] if r["mode"] == "immediate_shared"],
                          "supply_evidence": result["supply_evidence_not_used_for_prediction"]}, indent=2))


if __name__ == "__main__":
    main()
