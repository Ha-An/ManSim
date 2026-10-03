"""Read-only checkpoint evaluation; no optimization or checkpoint selection."""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
    os.environ[name] = "1"

import numpy as np
import simpy

from agents.factory import build_decision_module
from manufacturing_sim.adp.checkpoint import load_checkpoint
from manufacturing_sim.adp.encoding import ADPStateEncoder
from manufacturing_sim.adp.model import require_torch
from manufacturing_sim.adp.train import (
    InMemoryEventLogger,
    _compose_episode_cfg,
    _write_csv,
    run_training_episode,
)
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld

RUNTIME = {
    "training": True, "force_random_policy": False, "allow_wait_action": False,
    "worker_order_strategy": "cyclic", "max_review_interval_min": 1.0,
    "beam_width": 64,
}
PREDICTORS = (0, 1, 10)
CONDITIONS = (
    ("random_for_I0", None, 1.0),
    ("I0_eps02_for_I1", 0, 0.2),
    ("I9_eps01_for_I10", 9, 0.1),
    ("I0_greedy", 0, 0.0),
    ("I1_greedy", 1, 0.0),
    ("I10_greedy", 10, 0.0),
)
_MODELS = {}
_MANIFESTS = {}


def regression_stats(predictions, targets) -> dict:
    predicted = np.asarray(predictions, dtype=float)
    actual = np.asarray(targets, dtype=float)
    if predicted.shape != actual.shape or predicted.size == 0:
        raise ValueError("Nonempty paired predictions and targets are required")
    if not np.isfinite(predicted).all() or not np.isfinite(actual).all():
        raise ValueError("Nonfinite regression input")
    error = predicted - actual
    mse = float(np.mean(error ** 2))
    variance = float(np.var(actual))
    return {
        "sample_count": actual.size, "mse": mse, "rmse": mse ** 0.5,
        "mae": float(np.mean(abs(error))), "bias": float(np.mean(error)),
        "prediction_std": float(np.std(predicted)), "target_std": variance ** 0.5,
        "r2": 1.0 - mse / variance if variance > 1e-12 else None,
    }


def cross_episode_baselines(rows: list[dict]) -> tuple[list[float], list[float]]:
    """Fit only on other seeds; neither baseline receives task/action features."""
    forecasts, temporal = [], []
    seed_products = {row["seed"]: row["products"] for row in rows}
    for row in rows:
        other_products = [v for k, v in seed_products.items() if k != row["seed"]]
        if not other_products:
            raise ValueError("At least two independent episode seeds are required")
        forecasts.append(max(0.0, float(np.mean(other_products)) - row["products_before"]))
    coefficients = {}
    for seed in seed_products:
        training = [row for row in rows if row["seed"] != seed]
        x = np.asarray([[1., row["remaining_fraction"], row["remaining_fraction"] ** 2]
                        for row in training])
        y = np.asarray([row["target"] for row in training])
        coefficients[seed] = np.linalg.lstsq(x, y, rcond=None)[0]
    for row in rows:
        r = row["remaining_fraction"]
        temporal.append(max(0.0, float(np.dot([1., r, r * r], coefficients[row["seed"]]))))
    return forecasts, temporal


def _initialize() -> None:
    torch = require_torch()
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)


def _load_models(source: Path, seed: int) -> None:
    if _MODELS:
        return
    cfg = _compose_episode_cfg(worker_count=3, seed=seed, days=5, adp_cfg=RUNTIME)
    world = ManufacturingWorld(
        simpy.Environment(), cfg, InMemoryEventLogger(),
        build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"),
    )
    for iteration in sorted(set(PREDICTORS) | {9}):
        model, manifest = load_checkpoint(
            source / "checkpoints" / f"iteration_{iteration:03d}.pt", world=world,
            device="cpu", wait_action_enabled=False, worker_order_strategy="cyclic",
        )
        _MODELS[iteration] = model
        _MANIFESTS[iteration] = manifest
    del world
    gc.collect()


def _episode(job: dict) -> tuple[dict, list[dict]]:
    started = time.perf_counter()
    torch = require_torch()
    _load_models(Path(job["source"]), job["seed"])
    behavior = job["behavior_iteration"]
    counts = []
    original_encode = ADPStateEncoder.encode

    def record_count(encoder, world, workers, tasks_by_worker):
        counts.append(int(world.product_count))
        return original_encode(encoder, world, workers, tasks_by_worker)

    # Record the raw count without changing the actual encoded policy input.
    ADPStateEncoder.encode = record_count
    try:
        result, samples = run_training_episode(
            episode=job["episode"], phase=job["condition"], worker_count=3,
            seed=job["seed"], days=5, model=_MODELS.get(behavior), device="cpu",
            force_random=behavior is None, epsilon=job["epsilon"],
            adp_cfg=RUNTIME, collect_compact_samples=True, gamma=1.0,
        )
    finally:
        ADPStateEncoder.encode = original_encode
    assert result.simulation_end_min == 2400.0
    assert result.termination_reason == "completed_horizon"
    assert result.raw_return == result.products
    assert result.wait_count == 0 and result.joint_all_wait_count == 0
    assert samples is not None and len(samples) == result.decisions
    target = samples.targets.numpy().astype(float)
    assert len(counts) == len(samples)
    before = np.asarray(counts, dtype=float)
    encoded_count = samples.global_features[:, 2].numpy().astype(float) * 30.0
    clipped_count = int(np.count_nonzero(abs(encoded_count - before) > 1e-4))
    identity_error = float(np.max(abs(target - (result.products - before))))
    assert identity_error < 1e-4, identity_error
    prediction = {}
    for iteration in PREDICTORS:
        pieces = []
        model = _MODELS[iteration]
        model.eval()
        with torch.no_grad():
            for start in range(0, len(samples), 256):
                batch, _ = samples.model_batch(list(range(start, min(start + 256, len(samples)))), "cpu")
                pieces.append(model(**batch).numpy())
        prediction[iteration] = np.concatenate(pieces)
        assert np.isfinite(prediction[iteration]).all()
    rows = []
    for index in range(len(samples)):
        rows.append({
            "condition": job["condition"], "seed": job["seed"], "decision": index + 1,
            "time_min": float(samples.global_features[index, 0]) * 2400.,
            "remaining_fraction": float(samples.global_features[index, 1]),
            "products": result.products, "products_before": float(before[index]),
            "encoded_products_before": float(encoded_count[index]),
            "target": float(target[index]),
            "selected_edges": int(samples.selected_assignment_mask[index].sum()),
            "feasible_edges": int(samples.feasibility[index].sum()),
            **{f"prediction_I{iteration}": float(prediction[iteration][index]) for iteration in PREDICTORS},
        })
    meta = {
        **job, "products": result.products, "decisions": result.decisions,
        "raw_return": result.raw_return, "end_min": result.simulation_end_min,
        "termination_reason": result.termination_reason, "wait_count": result.wait_count,
        "target_identity_max_error": identity_error,
        "product_count_clipped_samples": clipped_count,
        "elapsed_sec": time.perf_counter() - started,
        "pid": os.getpid(), "checkpoint_environment_verified": True,
    }
    del samples, prediction
    gc.collect()
    episode_output = Path(job["output"]) / f"episode_{job['episode']:03d}"
    _write_csv(episode_output.with_suffix(".csv"), rows)
    episode_output.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta, rows


def summarize(output: Path, episodes: list[dict], samples: list[dict]) -> dict:
    metrics = []
    production = {}
    for name, _, _ in CONDITIONS:
        rows = [row for row in samples if row["condition"] == name]
        if not rows:
            continue
        products = [row["products"] for row in episodes if row["condition"] == name]
        production[name] = {"products": products, "mean": float(np.mean(products)),
                            "std": float(np.std(products, ddof=1))}
        actual = [row["target"] for row in rows]
        for iteration in PREDICTORS:
            values = [row[f"prediction_I{iteration}"] for row in rows]
            metrics.append({"condition": name, "predictor": f"I{iteration}",
                            **regression_stats(values, actual)})
        forecast, time_only = cross_episode_baselines(rows)
        for predictor, values in (("LOSO_mean_final_minus_current", forecast),
                                  ("LOSO_time_only_quadratic", time_only)):
            metrics.append({"condition": name, "predictor": predictor,
                            **regression_stats(values, actual)})
    _write_csv(output / "regression_metrics.csv", metrics)
    summary = {
        "production": production, "regression": metrics,
        "episode_count": len(episodes), "sample_count": len(samples),
        "no_retraining": True, "no_checkpoint_selection": True,
        "notes": [
            "MC targets belong to the named continuation policy, not necessarily the predictor's greedy policy.",
            "Samples within an episode are correlated; seeds, not decisions, are independent replications.",
            "LOSO baselines fit only on other episode seeds of the same continuation condition.",
            "No counterfactual action ranking or true expected-value oracle is measured here.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def audit_saved_result(output: Path) -> dict:
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    config = json.loads((output / "config.json").read_text(encoding="utf-8"))
    with (output / "prediction_samples.csv").open(encoding="utf-8-sig", newline="") as file:
        samples = list(csv.DictReader(file))
    with (output / "episode_metrics.csv").open(encoding="utf-8-sig", newline="") as file:
        episodes = list(csv.DictReader(file))
    assert len(episodes) == len(config["jobs"]) and len(samples) == summary["sample_count"]
    assert {(row["condition"], int(row["seed"])) for row in episodes} == {
        (row["condition"], row["seed"]) for row in config["jobs"]}
    assert all(float(row["target"]) == float(row["products"]) - float(row["products_before"])
               for row in samples)
    assert all(float(row["end_min"]) == 2400.0 and int(row["wait_count"]) == 0
               and row["termination_reason"] == "completed_horizon" for row in episodes)
    for metric in summary["regression"]:
        if metric["predictor"] not in {f"I{i}" for i in PREDICTORS}:
            continue
        selected = [row for row in samples if row["condition"] == metric["condition"]]
        errors = [float(row[f"prediction_{metric['predictor']}"]) - float(row["target"])
                  for row in selected]
        assert all(math.isfinite(value) for value in errors)
        mse = math.fsum(value * value for value in errors) / len(errors)
        assert math.isclose(mse, metric["mse"], rel_tol=1e-10)
    source = Path(episodes[0]["source"])
    with (source / "episode_metrics.csv").open(encoding="utf-8-sig", newline="") as file:
        old_episodes = list(csv.DictReader(file))
    with (source / "iteration_metrics.csv").open(encoding="utf-8-sig", newline="") as file:
        iterations = list(csv.DictReader(file))
    historical = []
    for iteration in iterations:
        index = int(iteration["iteration"])
        phase = "initial_random" if index == 0 else f"policy_iteration_{index}"
        selected = [row for row in old_episodes if row["phase"] == phase]
        count = sum(int(row["compact_sample_count"]) for row in selected)
        assert count == int(iteration["sample_count"])
        mean = math.fsum(int(row["compact_sample_count"]) * float(row["products"])
                         for row in selected) / count
        variance = math.fsum(int(row["compact_sample_count"]) * (float(row["products"]) - mean) ** 2
                             for row in selected) / count
        mse = float(iteration["mc_rmse"]) ** 2
        historical.append({
            "iteration": index, "sample_count": count,
            "holdout_mse": float(iteration["mc_loss"]), "full_batch_mse": mse,
            "full_batch_return_r2": 1. - mse / float(iteration["target_std"]) ** 2,
            "full_batch_final_product_std": variance ** .5,
            "full_batch_final_product_r2": 1. - mse / variance,
            "screening_products": float(iteration["validation_products_avg"]),
        })
    _write_csv(output / "historical_target_scale.csv", historical)
    result = {
        "status": "passed", "episode_count": len(episodes), "sample_count": len(samples),
        "target_identity_max_error": max(abs(float(row["target"]) -
            (float(row["products"]) - float(row["products_before"]))) for row in samples),
        "source_checkpoints_unchanged": summary["source_checkpoints_unchanged"],
        "products_feature_clipped_samples": sum(int(row["product_count_clipped_samples"]) for row in episodes),
        "sample_mse_recomputed_independently": True, "historical_sample_counts_match": True,
        "no_retraining": True,
        "limitations": "Three seeds per continuation policy; no expected action-ranking oracle or replay spatial audit.",
    }
    (output / "audit_summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="outputs/adp_training_reliability_v8_full/20260911_214109")
    parser.add_argument("--seeds", nargs="+", type=int, default=[930001, 930002, 930003])
    parser.add_argument("--processes", type=int, default=10)
    args = parser.parse_args()
    if len(set(args.seeds)) != len(args.seeds) or len(args.seeds) < 2:
        parser.error("Use at least two distinct episode seeds")
    if not 1 <= args.processes <= 10:
        parser.error("This diagnostic permits 1 to 10 processes")
    source = (ROOT / args.source).resolve()
    old = json.loads((source / "training_summary.json").read_text(encoding="utf-8"))
    used = {int(seed) for partition in old.get("seed_partitions", {}).values()
            if isinstance(partition, dict) for seed in partition.get("values", [])}
    if used & set(args.seeds):
        raise ValueError("Diagnostic seeds overlap original training or validation")
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in (source / "checkpoints").glob("*.pt")}
    output = ROOT / "outputs" / "adp_value_learning_audit" / datetime.now().strftime("%Y%m%d_%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    jobs = [{"source": str(source), "episode": index * len(args.seeds) + j,
             "output": str(output),
             "condition": name, "behavior_iteration": iteration, "epsilon": epsilon, "seed": seed}
            for index, (name, iteration, epsilon) in enumerate(CONDITIONS)
            for j, seed in enumerate(args.seeds)]
    (output / "config.json").write_text(json.dumps({"jobs": jobs, "processes": args.processes}, indent=2), encoding="utf-8")
    print(f"OUTPUT {output}\nRead-only audit: {len(jobs)} episodes, {args.processes} CPU processes", flush=True)
    started = time.perf_counter()
    episodes, all_samples = [], []
    with ProcessPoolExecutor(max_workers=args.processes, mp_context=multiprocessing.get_context("spawn"),
                             initializer=_initialize) as pool:
        pending = [pool.submit(_episode, job) for job in jobs]
        errors = []
        for future in as_completed(pending):
            try:
                episode, rows = future.result()
            except Exception as exc:
                errors.append(repr(exc))
                print(f"ERROR {exc!r}", flush=True)
                continue
            episodes.append(episode)
            all_samples.extend(rows)
            _write_csv(output / "episode_metrics.csv", sorted(episodes, key=lambda row: row["episode"]))
            print(f"DONE {episode['condition']} seed={episode['seed']} products={episode['products']} "
                  f"samples={len(rows)} ({len(episodes)}/{len(jobs)})", flush=True)
    if errors:
        (output / "errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")
        raise RuntimeError(f"{len(errors)} diagnostic episodes failed; completed episode files preserved")
    episodes.sort(key=lambda row: row["episode"])
    all_samples.sort(key=lambda row: (row["condition"], row["seed"], row["decision"]))
    _write_csv(output / "prediction_samples.csv", all_samples)
    summary = summarize(output, episodes, all_samples)
    summary["wall_sec"] = time.perf_counter() - started
    summary["source_checkpoints_unchanged"] = all(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest for path, digest in hashes.items())
    assert summary["source_checkpoints_unchanged"]
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    audit_saved_result(output)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
