"""Read-only historical ADP learning audit; never uses held-out policy-test data."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.audit_adp_training import audit_training


def rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def analyze(root: Path) -> dict:
    summary = json.loads((root / "training_summary.json").read_text(encoding="utf-8"))
    iterations = rows(root / "iteration_metrics.csv")
    episodes = rows(root / "episode_metrics.csv")
    waves = rows(root / "wave_metrics.csv")
    screening = [r for r in iterations if r.get("validation_products_mean")]
    best = max(screening, key=lambda r: (float(r["validation_products_mean"]), -int(r["iteration"])))
    last = screening[-1]
    seed_products = {}
    for row in episodes:
        if row["phase"] == "screening_validation":
            group = seed_products.setdefault(int(row["iteration"]), {})
            key = (int(row["worker_count"]), int(row["seed"]))
            if key in group:
                raise ValueError(f"Duplicate screening sample: {root}, {row['iteration']}, {key}")
            group[key] = float(row["products"])

    def paired(current: int, reference: int) -> dict:
        a, b = seed_products[current], seed_products[reference]
        if set(a) != set(b):
            raise ValueError(f"Unmatched validation seeds: {root}")
        differences = [a[key] - b[key] for key in sorted(a)]
        mean = statistics.fmean(differences)
        half = 1.96 * statistics.stdev(differences) / math.sqrt(len(differences)) if len(differences) > 1 else None
        return {"reference_iteration": reference, "n": len(differences), "mean": mean,
                "ci95_approx": [mean - half, mean + half] if half is not None else None,
                "win": sum(d > 0 for d in differences), "tie": sum(d == 0 for d in differences),
                "loss": sum(d < 0 for d in differences)}

    costs = []
    for cutoff in (30, 45, 60):
        prefix = [r for r in screening if int(r["iteration"]) <= cutoff]
        if not prefix or cutoff >= int(last["iteration"]):
            continue
        prefix_best = max(prefix, key=lambda r: float(r["validation_products_mean"]))
        # Only sum actual later wave wall times and target/update times.
        # Final selection, pool setup and any hypothetical early-stop evaluation are excluded.
        wave_seconds = sum(float(r["wall_sec"]) for r in waves if r["status"] == "completed"
                           and r["phase"] != "final_selection_validation" and int(r["iteration"]) > cutoff)
        update_seconds = sum(float(r.get(k) or 0) for r in iterations if int(r["iteration"]) > cutoff
                             for k in ("target_build_sec", "update_sec"))
        costs.append({"cutoff": cutoff, "prefix_best_iteration": int(prefix_best["iteration"]),
                      "prefix_best_products": float(prefix_best["validation_products_mean"]),
                      "observed_later_cost_hours": (wave_seconds + update_seconds) / 3600,
                      "missed_screening_gain": float(best["validation_products_mean"]) - float(prefix_best["validation_products_mean"])})
    validity_file = root / "result_validity.json"
    return {
        "root": str(root.resolve()), "worker_counts": summary.get("worker_counts"),
        "validity": json.loads(validity_file.read_text(encoding="utf-8")) if validity_file.exists() else None,
        "historical_wall_hours": summary["wall_sec"] / 3600,
        "screening_best_iteration": int(best["iteration"]), "final_selected_iteration": summary["best_iteration"],
        "screening_best_products": float(best["validation_products_mean"]),
        "screening_last_products": float(last["validation_products_mean"]),
        "last_minus_screening_best": paired(int(last["iteration"]), int(best["iteration"])),
        "screening": [{key: float(row[key]) for key in ("iteration", "validation_products_mean", "validation_products_std",
                        "td_holdout_mse", "greedy_mc_rmse", "greedy_mc_bias") if row.get(key)} for row in screening],
        "hypothetical_cutoffs": costs,
        "artifact_audit": audit_training(root, load_checkpoints=False),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = [analyze(path) for path in args.runs]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    for row in results:
        print(json.dumps({key: row[key] for key in ("root", "screening_best_iteration", "screening_best_products",
              "screening_last_products", "last_minus_screening_best", "hypothetical_cutoffs")}, ensure_ascii=False))
        print("AUDIT", row["artifact_audit"]["status"], row["artifact_audit"]["errors"], row["artifact_audit"]["warnings"])


if __name__ == "__main__":
    main()
