from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
import math
import statistics
from pathlib import Path
import sys
from typing import Any

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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


def _nonfinite_metric_errors(rows, name: str) -> list[str]:
    errors = []
    for index, row in enumerate(rows):
        for key, value in row.items():
            # Hex digests can resemble an overflowing scientific-notation float.
            if key.endswith("_hash"):
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numeric):
                errors.append(f"{name} row {index}: non-finite {key}={value}")
    return errors


def audit_metric_aggregates(episodes, iterations, waves, summary, selection) -> list[str]:
    """Recompute reported statistics from episode records, not from other summaries."""
    errors: list[str] = []

    def check(row, key, expected, context, tolerance=1e-6):
        actual = _number(row, key)
        if ((actual is None) != (expected is None)
                or actual is not None and (not math.isfinite(actual)
                    or not math.isclose(actual, expected, abs_tol=tolerance, rel_tol=1e-8))):
            errors.append(f"{context}: {key}={actual}, recomputed={expected}")

    def moments(rows):
        values = [float(row['products']) for row in rows]
        return statistics.fmean(values), statistics.stdev(values) if len(values) > 1 else 0.0

    grouped, by_wave = defaultdict(list), defaultdict(list)
    for row in episodes:
        grouped[(row['phase'], int(float(row['iteration'])))].append(row)
        by_wave[row.get('wave_id')].append(row)
    for row in iterations:
        i = int(float(row['iteration']))
        context = f"iteration {i}"
        training = [e for phase in TRAINING_PHASES for e in grouped[(phase, i)]]
        validation = grouped[('screening_validation', i)]
        if training:
            mean, std = moments(training)
            check(row, 'rollout_products_mean', mean, context)
            check(row, 'rollout_products_std', std, context)
            check(row, 'batch_episode_count', len(training), context)
            if 'decision_count_mean' in row:
                check(row, 'decision_count_mean', statistics.fmean(float(e['decision_count']) for e in training), context)
            if 'voluntary_wait_count' in row:
                check(row, 'voluntary_wait_count', sum(float(e['candidate_available_wait_count']) for e in training), context)
        else:
            errors.append(f'{context}: no training episodes')
        if validation:
            mean, std = moments(validation)
            check(row, 'validation_products_mean', mean, context)
            check(row, 'validation_products_std', std, context)
            check(row, 'validation_episode_count', len(validation), context)
            if all('greedy_mc_sample_count' in e for e in validation):
                count = sum(int(e['greedy_mc_sample_count']) for e in validation)
                check(row, 'greedy_mc_sample_count', count, context)
                sums = {key: sum(float(e[key]) for e in validation) for key in (
                    'greedy_mc_squared_error_sum', 'greedy_mc_error_sum',
                    'greedy_mc_prediction_sum', 'greedy_mc_target_sum')}
                for metric, total in [('greedy_mc_rmse', 'greedy_mc_squared_error_sum'),
                                      ('greedy_mc_bias', 'greedy_mc_error_sum'),
                                      ('greedy_mc_prediction_mean', 'greedy_mc_prediction_sum'),
                                      ('greedy_mc_target_mean', 'greedy_mc_target_sum')]:
                    expected = sums[total] / count if count else None
                    if metric == 'greedy_mc_rmse' and expected is not None:
                        expected = math.sqrt(expected)
                    check(row, metric, expected, context)
        elif _number(row, 'validation_products_mean') is not None:
            errors.append(f'{context}: validation mean has no episode records')

    for row in waves:
        name = row.get('wave_id')
        members = by_wave.get(name, [])
        check(row, 'episode_count', len(members), f'wave {name}')
        if not members:
            continue
        if any(e['phase'] != row['phase'] or int(e['iteration']) != int(row['iteration']) for e in members):
            errors.append(f'wave {name}: episode phase/iteration mismatch')
        if any(e.get('snapshot_hash') != row.get('snapshot_hash') for e in members):
            errors.append(f'wave {name}: actual episode snapshot hash mismatch')
        elapsed = [float(e['elapsed_sec']) for e in members]
        for key, value in [('episode_elapsed_sum_sec', sum(elapsed)),
                           ('episode_elapsed_avg_sec', statistics.fmean(elapsed)),
                           ('episode_elapsed_min_sec', min(elapsed)),
                           ('episode_elapsed_max_sec', max(elapsed))]:
            if key in row:
                check(row, key, value, f'wave {name}', tolerance=2e-5)

    final = []
    for row in selection:
        i = int(float(row['iteration']))
        screening = grouped[('screening_validation', i)]
        if screening:
            mean, std = moments(screening)
            check(row, 'screening_mean', mean, f'selection {i}')
            check(row, 'screening_std', std, f'selection {i}')
        records = grouped[('final_selection_validation', i)]
        if records:
            mean, std = moments(records)
            check(row, 'final_mean', mean, f'selection {i}')
            check(row, 'final_std', std, f'selection {i}')
            final.append((i, mean, std))
            it_row = next((r for r in iterations if int(float(r['iteration'])) == i), {})
            check(it_row, 'final_validation_products_mean', mean, f'iteration {i}')
            check(it_row, 'final_validation_products_std', std, f'iteration {i}')
        elif _number(row, 'final_mean') is not None:
            errors.append(f'selection {i}: final mean has no episode records')
    if final:
        best, mean, std = min(final, key=lambda r: (-r[1], r[2], r[0]))
        check(summary, 'best_iteration', best, 'summary')
        check(summary, 'best_validation_completed_products_avg', mean, 'summary')
        check(summary, 'best_validation_completed_products_std', std, 'summary')
    for key in ('target_build_sec', 'update_sec'):
        if key in summary:
            check(summary, key, sum(_number(row, key) or 0 for row in iterations), 'summary', tolerance=.001)
    return errors


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

    for name, rows in [('episodes', episodes), ('iterations', iterations), ('waves', waves), ('selection', selection)]:
        errors.extend(_nonfinite_metric_errors(rows, name))
    if errors:
        return {'root': str(root), 'status': 'fail', 'errors': errors, 'warnings': warnings}
    try:
        errors.extend(audit_metric_aggregates(episodes, iterations, waves, summary, selection))
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f'metric aggregation audit failed: {exc}')
    if summary.get('wait_action_enabled') is False and any(
        int(row.get('idle_metrics_version') or 1) < 2 for row in episodes
    ):
        warnings.append('Legacy WAIT-disabled logs did not count forced idle; zero idle counts are unavailable data, not measured absence.')

    if summary.get("status") != "completed":
        errors.append(f"training status is {summary.get('status')!r}, expected 'completed'")
    if summary.get("return_estimator") != "n_step_td":
        errors.append("return_estimator is not n_step_td")

    training_cfg = cfg.get("training", {})
    validation_cfg = cfg.get("validation", {})
    max_iterations = int(training_cfg.get("policy_iterations", -1))
    policy_iterations = int(summary.get("completed_policy_iterations", max_iterations))
    stop_config = training_cfg.get("early_stopping", {})
    if not 0 <= policy_iterations <= max_iterations:
        errors.append("completed_policy_iterations is outside configured iteration budget")
    if policy_iterations < max_iterations:
        if not stop_config.get("enabled") or not summary.get("early_stopped") or summary.get("stop_reason") != "production_plateau":
            errors.append("shortened training has no declared production early stop")
    elif summary.get("early_stopped"):
        errors.append("early_stopped is true although the full iteration budget was used")
    expected_training = int(training_cfg.get("initial_random_episodes", 0)) + (
        policy_iterations * int(training_cfg.get("episodes_per_iteration", 0))
    )
    expected_indices = list(range(policy_iterations + 1))
    actual_indices = [int(float(row["iteration"])) for row in iterations]
    if actual_indices != expected_indices:
        errors.append(f"iteration sequence mismatch: {actual_indices} != {expected_indices}")

    training_rows = [row for row in episodes if row.get("phase") in TRAINING_PHASES]
    validation_rows = [row for row in episodes if row.get("phase") in VALIDATION_PHASES]
    if stop_config.get("enabled"):
        from manufacturing_sim.adp.early_stopping import ProductionEarlyStopping, validate_early_stopping
        try:
            validate_early_stopping(stop_config, max_iterations=max_iterations,
                                   seed_count=int(validation_cfg["screening_seed_count"]))
            stopper = ProductionEarlyStopping(stop_config)
            last_check = {}
            for row in iterations:
                i = int(float(row["iteration"]))
                samples = [r for r in validation_rows if r["phase"] == "screening_validation" and int(float(r["iteration"])) == i]
                if not samples:
                    continue
                products = {int(r["seed"]): float(r["products"]) for r in samples}
                if len(products) != len(samples) or len(products) != int(validation_cfg["screening_seed_count"]):
                    raise ValueError("Incomplete or duplicated screening seeds for early stop")
                last_check = stopper.observe(i, products)
                for key in ("early_stop_paired_mean", "early_stop_paired_ci_upper"):
                    actual, expected = _number(row, key), last_check[key]
                    if (actual is None) != (expected is None) or (actual is not None and abs(actual - expected) > 1e-6):
                        errors.append(f"iteration {i} has an incorrect {key}")
                if last_check["early_stop_triggered"] and i < policy_iterations:
                    errors.append(f"training continued past its early stop at iteration {i}")
            if policy_iterations < max_iterations and not last_check.get("early_stop_triggered"):
                errors.append("production early stop could not be reproduced from screening samples")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"early stopping audit failed: {exc}")
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
        checkpoint_groups = defaultdict(list)
        for row in episodes:
            if row.get('phase') == phase:
                checkpoint_groups[int(float(row['iteration']))].append(row)
        for i, records in checkpoint_groups.items():
            seeds = [int(float(row['seed'])) for row in records]
            if len(seeds) != len(set(seeds)) or set(seeds) != partition_sets[partition]:
                errors.append(f'{phase} iteration {i} has incomplete or duplicate seeds')

    configured_workers = {int(w) for w in cfg.get('worker_counts', [])}
    if {int(float(row['worker_count'])) for row in episodes} != configured_workers:
        errors.append('episode worker counts disagree with resolved configuration')

    for row in iterations:
        iteration = int(float(row["iteration"]))
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
        available = sum(bool(row.get("validation_products_mean")) for row in iterations)
        if len(final_results) != min(available, int(validation_cfg.get("final_candidate_count", 1))):
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
    for field, source in [('validation_completed_products_avg', 'best_validation_completed_products_avg'),
                          ('validation_completed_products_std', 'best_validation_completed_products_std')]:
        value, expected = manifest_json.get(field), summary.get(source)
        if value is not None and expected is not None and not math.isclose(float(value), float(expected), abs_tol=1e-6):
            errors.append(f'checkpoint manifest {field} disagrees with the selected validation result')

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
            last = torch.load(root / 'last.pt', map_location='cpu', weights_only=False)
            for name, payload, index in [('best', best, best_iteration), ('last', last, policy_iterations)]:
                if int(payload.get('manifest', {}).get('iteration', -1)) != index:
                    errors.append(f'{name}.pt has an incorrect iteration')
                checkpoint_path = root / 'checkpoints' / f'iteration_{index:03d}.pt'
                if not checkpoint_path.is_file():
                    errors.append(f'missing iteration checkpoint: {checkpoint_path.name}')
                    continue
                original = torch.load(checkpoint_path, map_location='cpu', weights_only=False)
                actual_state, expected_state = payload.get('model_state_dict', {}), original.get('model_state_dict', {})
                if not actual_state or actual_state.keys() != expected_state.keys() or any(
                    not torch.equal(tensor, expected_state[key]) for key, tensor in actual_state.items()
                ):
                    errors.append(f'{name}.pt model does not match iteration {index}')
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
