from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path
from typing import Any

import yaml


TRAINING_PHASES = {"initial_random", "warm_start_replay", "policy_iteration"}
VALIDATION_PHASES = {"screening_validation", "final_selection_validation"}


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _number(row: dict[str, str], key: str) -> float | None:
    value = str(row.get(key, "")).strip()
    if not value:
        return None
    return float(value)


def _json_canonical(value: object) -> object:
    """Normalize JSON object keys before comparing checkpoint and JSON manifests."""
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))


def audit_training(root: Path, *, load_checkpoints: bool = True) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []

    required = [
        "resolved_config.yaml", "training_summary.json", "episode_metrics.csv",
        "iteration_metrics.csv", "wave_metrics.csv", "checkpoint_selection.csv",
        "checkpoint_manifest.json", "training_dashboard.html", "best.pt", "last.pt",
    ]
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        return {"root": str(root), "status": "fail", "errors": [f"missing files: {missing}"], "warnings": []}

    cfg = yaml.safe_load((root / "resolved_config.yaml").read_text(encoding="utf-8")) or {}
    summary = json.loads((root / "training_summary.json").read_text(encoding="utf-8"))
    episodes = _csv_rows(root / "episode_metrics.csv")
    iterations = _csv_rows(root / "iteration_metrics.csv")
    waves = _csv_rows(root / "wave_metrics.csv")
    selection = _csv_rows(root / "checkpoint_selection.csv")

    if summary.get("status") != "completed":
        errors.append(f"training status is {summary.get('status')!r}, expected 'completed'")
    if summary.get("return_estimator") != "n_step_td":
        errors.append("return_estimator is not n_step_td")

    training_cfg = cfg.get("training", {})
    validation_cfg = cfg.get("validation", {})
    policy_iterations = int(training_cfg.get("policy_iterations", -1))
    expected_training = int(training_cfg.get("initial_random_episodes", 0)) + (
        policy_iterations * int(training_cfg.get("episodes_per_iteration", 0))
    )
    expected_indices = list(range(policy_iterations + 1))
    actual_indices = [int(float(row["iteration"])) for row in iterations]
    if actual_indices != expected_indices:
        errors.append(f"iteration sequence mismatch: {actual_indices} != {expected_indices}")

    training_rows = [row for row in episodes if row.get("phase") in TRAINING_PHASES]
    validation_rows = [row for row in episodes if row.get("phase") in VALIDATION_PHASES]
    if len(training_rows) != expected_training:
        errors.append(f"training episode count mismatch: {len(training_rows)} != {expected_training}")
    if len(training_rows) != int(summary.get("training_episode_count", -1)):
        errors.append("training_summary training_episode_count does not match episode CSV")
    if len(validation_rows) != int(summary.get("validation_episode_count", -1)):
        errors.append("training_summary validation_episode_count does not match episode CSV")

    episode_ids = [int(float(row["episode"])) for row in episodes]
    if len(set(episode_ids)) != len(episode_ids):
        errors.append("episode_metrics.csv contains duplicate episode IDs")
    horizon_min = float(cfg.get("horizon_days", 0)) * 480.0
    for row in episodes:
        if row.get("termination_reason") != "completed_horizon":
            errors.append(f"episode {row.get('episode')} ended as {row.get('termination_reason')}")
        end = _number(row, "simulation_end_min")
        if end is None or abs(end - horizon_min) > 1e-6:
            errors.append(f"episode {row.get('episode')} ended at {end}, expected {horizon_min}")
        products = _number(row, "products")
        raw_return = _number(row, "raw_return")
        if products is None or raw_return is None or abs(products - raw_return) > 1e-6:
            errors.append(f"episode {row.get('episode')} product/reward mismatch")

    partitions = summary.get("seed_partitions", {})
    partition_sets = {
        name: {int(value) for value in partitions.get(name, [])}
        for name in ("training", "screening_validation", "final_selection_validation", "held_out_test")
    }
    names = list(partition_sets)
    for index, left in enumerate(names):
        for right in names[index + 1:]:
            overlap = partition_sets[left] & partition_sets[right]
            if overlap:
                errors.append(f"seed overlap between {left} and {right}: {sorted(overlap)}")
    actual_training_seeds = {int(float(row["seed"])) for row in training_rows}
    if actual_training_seeds != partition_sets["training"]:
        errors.append("actual training seeds do not match checkpoint seed partition")
    for phase, partition in (
        ("screening_validation", "screening_validation"),
        ("final_selection_validation", "final_selection_validation"),
    ):
        actual = {int(float(row["seed"])) for row in episodes if row.get("phase") == phase}
        if actual != partition_sets[partition]:
            errors.append(f"actual {phase} seeds do not match declared partition")

    for row in iterations:
        iteration = int(float(row["iteration"]))
        for key, value in row.items():
            if value == "":
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numeric):
                errors.append(f"iteration {iteration} has non-finite {key}={value}")
        ratio_keys = ("td_off_policy_cut_ratio", "td_terminal_ratio", "td_n_limit_ratio")
        ratios = [_number(row, key) for key in ratio_keys]
        if all(value is not None for value in ratios) and abs(sum(ratios) - 1.0) > 1e-7:
            errors.append(f"iteration {iteration} TD termination ratios do not sum to one")
        current = _number(row, "update_current_episode_count")
        history = _number(row, "update_history_episode_count")
        update = _number(row, "update_episode_count")
        if None not in (current, history, update) and int(current + history) != int(update):
            errors.append(f"iteration {iteration} update episode decomposition is inconsistent")
        if not bool(cfg.get("algorithm", {}).get("allow_wait_action", False)):
            waits = _number(row, "voluntary_wait_count")
            if waits not in (None, 0.0):
                errors.append(f"iteration {iteration} recorded voluntary WAIT while WAIT is disabled")

    completed_waves = [row for row in waves if row.get("status") == "completed"]
    if len(completed_waves) != len(waves):
        errors.append("wave_metrics.csv contains failed or incomplete waves")
    if sum(int(float(row["episode_count"])) for row in completed_waves) != len(episodes):
        errors.append("wave episode counts do not sum to episode_metrics rows")
    phase_wave_counts = Counter(row.get("phase") for row in completed_waves)
    for phase, count in (summary.get("phase_wave_counts", {}) or {}).items():
        if phase_wave_counts.get(phase, 0) != int(count):
            errors.append(f"wave count mismatch for {phase}")
    for row in completed_waves:
        if str(row.get("snapshot_hash_match", "")).lower() != "true":
            errors.append(f"wave {row.get('wave_id')} used inconsistent policy snapshots")
        episode_sum = _number(row, "episode_elapsed_sum_sec")
        wall = _number(row, "wall_sec")
        processes = _number(row, "actual_process_count")
        speedup = _number(row, "effective_speedup")
        efficiency = _number(row, "parallel_efficiency")
        utilization = _number(row, "active_slot_utilization")
        if None not in (episode_sum, wall, processes, speedup, efficiency, utilization) and wall > 0 and processes > 0:
            if abs(speedup - episode_sum / wall) > 2e-5:
                errors.append(f"wave {row.get('wave_id')} speedup is miscomputed")
            if abs(efficiency - speedup / processes) > 2e-5:
                errors.append(f"wave {row.get('wave_id')} efficiency is miscomputed")
            if abs(utilization - episode_sum / (processes * wall)) > 2e-5:
                errors.append(f"wave {row.get('wave_id')} utilization is miscomputed")

    selected = [row for row in selection if str(row.get("selected", "")).lower() == "true"]
    best_iteration = int(summary.get("best_iteration", -1))
    if len(selected) != 1 or int(float(selected[0]["iteration"])) != best_iteration:
        errors.append("checkpoint_selection.csv does not identify training_summary best iteration")
    final_results = summary.get("final_candidate_results", []) or []
    if final_results:
        if len(final_results) != int(validation_cfg.get("final_candidate_count", 1)):
            errors.append("final candidate result count does not match resolved config")
        selected_final = [row for row in final_results if row.get("selected")]
        if len(selected_final) != 1 or int(selected_final[0]["iteration"]) != best_iteration:
            errors.append("final candidate results do not identify the best iteration")
    else:
        warnings.append(
            "legacy training_summary has no final_candidate_results; selection was validated from "
            "checkpoint_selection.csv and best.pt"
        )

    checkpoint_files = sorted((root / "checkpoints").glob("iteration_*.pt"))
    if len(checkpoint_files) != policy_iterations + 1:
        errors.append(f"checkpoint count mismatch: {len(checkpoint_files)} != {policy_iterations + 1}")
    manifest_json = json.loads((root / "checkpoint_manifest.json").read_text(encoding="utf-8"))
    if int(manifest_json.get("iteration", -1)) != best_iteration:
        errors.append("checkpoint_manifest iteration does not match best iteration")

    if load_checkpoints:
        try:
            import torch

            best = torch.load(root / "best.pt", map_location="cpu", weights_only=False)
            if _json_canonical(best.get("manifest")) != _json_canonical(manifest_json):
                errors.append("best.pt manifest differs from checkpoint_manifest.json")
            for name, tensor in best.get("model_state_dict", {}).items():
                if not bool(torch.isfinite(tensor).all()):
                    errors.append(f"best.pt contains non-finite model tensor {name}")
            target_state = best.get("target_model_state_dict")
            if not isinstance(target_state, dict):
                errors.append("best.pt is missing target_model_state_dict")
            elif any(not bool(torch.isfinite(tensor).all()) for tensor in target_state.values()):
                errors.append("best.pt contains non-finite target-network tensors")
        except ModuleNotFoundError:
            warnings.append("PyTorch is unavailable; checkpoint tensors were not audited")

    dashboard = (root / "training_dashboard.html").read_text(encoding="utf-8")
    if "\ufffd" in dashboard:
        errors.append("training dashboard contains Unicode replacement characters")
    for required_text in ("ADP n-step TD", "iteration_metrics.csv", "episode_metrics.csv", "wave_metrics.csv"):
        if required_text not in dashboard:
            errors.append(f"training dashboard is missing {required_text!r}")

    wall = float(summary.get("wall_sec", 0.0) or 0.0)
    measured = sum(float(summary.get(key, 0.0) or 0.0) for key in (
        "training_rollout_sec", "validation_sec", "target_build_sec", "update_sec", "write_sec"
    ))
    if measured > wall * 1.02 + 1.0:
        errors.append(f"time components exceed wall time: components={measured:.3f}, wall={wall:.3f}")

    return {
        "root": str(root),
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "warnings": warnings,
        "training_episode_count": len(training_rows),
        "validation_episode_count": len(validation_rows),
        "iteration_count": len(iterations),
        "wave_count": len(waves),
        "best_iteration": best_iteration,
        "best_validation_completed_products_avg": summary.get("best_validation_completed_products_avg"),
        "training_device": summary.get("training_device"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit an n-step TD ADP training result directory.")
    parser.add_argument("training_root", type=Path)
    parser.add_argument("--skip-checkpoint-load", action="store_true")
    parser.add_argument("--json-output", type=Path, default=None)
    args = parser.parse_args()
    report = audit_training(args.training_root, load_checkpoints=not args.skip_checkpoint_load)
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
