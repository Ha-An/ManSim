"""Independent, diagnostic-only MC calibration and paired action continuations."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import random
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

DEFAULTS = {
    "enabled": False,
    "checkpoints": ["initial", "best", "last"],
    "mc_seeds": [942001, 942002, 942003],
    "action_checkpoints": ["best"],
    "action_seeds": [943001],
    "decision_thresholds": [20, 100],
    "candidates_per_state": 3,
    "future_seeds": [944001, 944002, 944003, 944004, 944005],
    "process_count": 10,
}


def regression_stats(predicted: Any, actual: Any) -> dict[str, Any]:
    p, y = np.asarray(predicted, dtype=float), np.asarray(actual, dtype=float)
    if p.ndim != 1 or p.shape != y.shape or not len(y) or not np.isfinite(p).all() or not np.isfinite(y).all():
        raise ValueError("Finite, nonempty paired prediction and target vectors required.")
    e = p - y
    return {"sample_count": len(y), "mse": float(np.mean(e ** 2)),
            "rmse": float(np.sqrt(np.mean(e ** 2))), "bias": float(e.mean())}


def score_episode(model: Any, transitions: list[dict], device: Any) -> list[dict]:
    from .model import predict_values
    from .schema import REMAINING_HORIZON_INDEX
    from .train import monte_carlo_samples

    samples = monte_carlo_samples(transitions)
    rows = []
    for start in range(0, len(samples), 256):
        chunk = samples[start:start + 256]
        predictions = predict_values(model, [state for state, _ in chunk], device=device)
        for offset, ((state, target), prediction) in enumerate(zip(chunk, predictions)):
            rows.append({"decision": start + offset + 1,
                         "remaining_fraction": float(state.global_features[REMAINING_HORIZON_INDEX]),
                         "target": float(target), "prediction": float(prediction)})
    regression_stats([r["prediction"] for r in rows], [r["target"] for r in rows])
    return rows


def time_only_predictions(rows: list[dict]) -> list[float]:
    """Leave-one-episode-seed-out; no target from the evaluated seed is fitted."""
    seeds = sorted({int(row["seed"]) for row in rows})
    if len(seeds) < 2:
        raise ValueError("Time-only baseline requires at least two independent seeds.")
    coefficients = {}
    for seed in seeds:
        other = [r for r in rows if int(r["seed"]) != seed]
        x = np.asarray([[1., r["remaining_fraction"], r["remaining_fraction"] ** 2] for r in other])
        coefficients[seed] = np.linalg.lstsq(x, [r["target"] for r in other], rcond=None)[0]
    return [max(0., float(np.dot(coefficients[int(r["seed"])],
                                [1., r["remaining_fraction"], r["remaining_fraction"] ** 2]))) for r in rows]


def paired_action_loss(returns: Any, *, bootstrap_seed: int = 2026) -> dict:
    """Columns share future seeds; row zero is the actual beam-selected action.

    Centered simultaneous bootstrap prevents a positive lower bound arising
    solely from taking the maximum of noisy candidate means.
    """
    values = np.asarray(returns, dtype=float)
    if values.ndim != 2 or values.shape[0] < 2 or values.shape[1] < 3 or not np.isfinite(values).all():
        raise ValueError("Action validation requires >=2 actions and >=3 paired future draws.")
    differences = values[1:] - values[0]
    means = differences.mean(axis=1)
    rng = np.random.default_rng(bootstrap_seed)
    indices = rng.integers(0, values.shape[1], size=(4000, values.shape[1]))
    centered = differences[:, indices].mean(axis=2) - means[:, None]
    radius = float(np.quantile(np.max(abs(centered), axis=0), .95))
    loss = max(0., float(means.max()))
    candidate_means = values.mean(axis=1)

    pairwise = np.asarray(
        [values[high] - values[low]
         for low in range(values.shape[0])
         for high in range(low + 1, values.shape[0])],
        dtype=float,
    )
    pairwise_means = pairwise.mean(axis=1)
    pairwise_centered = pairwise[:, indices].mean(axis=2) - pairwise_means[:, None]
    spread_radius = float(np.quantile(np.max(abs(pairwise_centered), axis=0), .95))
    action_spread = float(candidate_means.max() - candidate_means.min())
    best_candidate_id = int(np.argmax(candidate_means))
    worst_candidate_id = int(np.argmin(candidate_means))
    best_worst_differences = values[best_candidate_id] - values[worst_candidate_id]
    action_effect_se = float(np.std(best_worst_differences, ddof=1) / np.sqrt(values.shape[1]))
    return {"observed_selection_loss": loss,
            "ci95_low": max(0., float(means.max()) - radius),
            "ci95_high": max(0., float(means.max()) + radius),
            "empirical_action_spread": action_spread,
            "action_spread_ci95_low": max(0., action_spread - spread_radius),
            "action_spread_ci95_high": action_spread + spread_radius,
            "action_effect_standard_error": action_effect_se,
            "empirical_best_candidate_id": best_candidate_id,
            "empirical_worst_candidate_id": worst_candidate_id,
            "candidate_mean_returns": candidate_means.tolist(),
            "paired_difference_means": means.tolist(), "repeats": values.shape[1],
            "candidate_count": values.shape[0],
            "ci_method": "centered_paired_bootstrap_simultaneous_candidates_approximate"}


def rank_correlation(predicted: Any, actual: Any) -> float:
    """Spearman correlation for a small candidate set with deterministic tie ranks."""
    left, right = np.asarray(predicted, dtype=float), np.asarray(actual, dtype=float)
    if left.ndim != 1 or left.shape != right.shape or len(left) < 2:
        raise ValueError("At least two paired candidate values are required.")

    def ranks(values: np.ndarray) -> np.ndarray:
        order = np.argsort(values, kind="stable")
        result = np.empty(len(values), dtype=float)
        position = 0
        while position < len(values):
            end = position + 1
            while end < len(values) and values[order[end]] == values[order[position]]:
                end += 1
            result[order[position:end]] = (position + end - 1) / 2.0
            position = end
        return result

    left_rank, right_rank = ranks(left), ranks(right)
    if np.std(left_rank) == 0.0 or np.std(right_rank) == 0.0:
        return 0.0
    return float(np.corrcoef(left_rank, right_rank)[0, 1])


def reseed_future_draws(world: Any, seed: int) -> None:
    """Diagnostic branch only. Keep scheduled events, active samples and caches."""
    from manufacturing_sim.simulation.scenarios.manufacturing.world import _stable_random_stream

    world._diagnostic_sampling_seed = int(seed)
    world.rng = random.Random(int(seed))
    world.quality_rng = _stable_random_stream(seed, f"{world.scenario_key}:quality")
    world.machine_failure_rngs.clear()
    world.humanoid_incident_rngs.clear()
    world.timing.seed = int(seed)
    world.adp_coordinator.rng = random.Random(int(seed) ^ 0xAD92026)


def validate_config(config: dict, occupied_seeds: set[int]) -> dict:
    cfg = {**DEFAULTS, **config}
    used = set(occupied_seeds)
    for name in ("mc_seeds", "action_seeds", "future_seeds"):
        seeds = [int(v) for v in cfg[name]]
        minimum = {"mc_seeds": 2, "action_seeds": 1, "future_seeds": 3}[name]
        if len(seeds) < minimum or len(seeds) != len(set(seeds)) or used.intersection(seeds):
            raise ValueError(f"{name}: require >= {minimum} unique seeds disjoint from training/selection/test/diagnostics.")
        cfg[name] = seeds
        used.update(seeds)
    thresholds = [int(v) for v in cfg["decision_thresholds"]]
    if not thresholds or any(v < 1 for v in thresholds) or len(set(thresholds)) != len(thresholds):
        raise ValueError("Decision thresholds must be distinct positive integers.")
    cfg["decision_thresholds"] = sorted(thresholds)
    cfg["process_count"] = int(cfg["process_count"])
    cfg["candidates_per_state"] = int(cfg["candidates_per_state"])
    if not 1 <= cfg["process_count"] <= 10 or cfg["candidates_per_state"] < 2:
        raise ValueError("Require 1..10 processes and at least two candidates.")
    return cfg


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_iterations(labels: list, summary: dict, available: list[int]) -> list[int]:
    aliases = {"initial": min(available), "last": max(available), "best": int(summary["best_iteration"])}
    result = sorted({aliases[str(v)] if str(v) in aliases else int(v) for v in labels})
    if not result or not set(result).issubset(available):
        raise ValueError("Diagnostic checkpoint iteration is unavailable.")
    return result


def run_validation(training_dir: Path, config: dict | None = None) -> Path:
    """Post-training evaluation, never used for gradient/epoch/checkpoint selection."""
    import simpy
    from agents.factory import build_decision_module
    from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld
    from .checkpoint import load_checkpoint
    from .model import predict_values, require_torch
    from .schema import deserialize_state
    from .train import (InMemoryEventLogger, RolloutJob, _compose_episode_cfg,
                        _run_rollout_jobs_parallel, _snapshot_state_dict, _write_csv)

    started = time.perf_counter()
    training_dir = training_dir.resolve()
    summary = json.loads((training_dir / "training_summary.json").read_text(encoding="utf-8"))
    previous_episodes = _read_csv(training_dir / "episode_metrics.csv")
    occupied = {int(row["seed"]) for row in previous_episodes}
    for partition in summary.get("seed_partitions", {}).values():
        if isinstance(partition, dict):
            occupied.update(int(v) for v in partition.get("values", partition.get("seeds", [])))
    cfg = validate_config({**summary.get("independent_value_validation_config", {}), **(config or {})}, occupied)
    # Reaching this function means the optional diagnostic is being executed,
    # even when the training profile kept automatic execution disabled.
    cfg["enabled"] = True
    available = sorted(int(p.stem.split("_")[-1]) for p in (training_dir / "checkpoints").glob("iteration_*.pt"))
    mc_iterations = _resolve_iterations(cfg["checkpoints"], summary, available)
    action_iterations = _resolve_iterations(cfg["action_checkpoints"], summary, available)
    output = training_dir / "value_validation" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output.mkdir(parents=True, exist_ok=False)
    cfg.update(mc_iterations=mc_iterations, action_iterations=action_iterations,
               horizon_days=int(summary["horizon_days"]), worker_counts=summary["worker_counts"])
    (output / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    episodes, waves, predictions, value_rows, action_rows, branch_rows = [], [], [], [], [], []
    paths = {i: training_dir / "checkpoints" / f"iteration_{i:03d}.pt"
             for i in set(mc_iterations + action_iterations)}
    hashes = {i: _file_hash(p) for i, p in paths.items()}
    torch = require_torch()
    torch.set_num_threads(1)
    job_number = 0

    def persist(status: str, error: str = "") -> dict:
        payload = {"status": status, "error": error, "contract": "independent_greedy_mc_and_paired_future_actions_v1",
                   "continuation_policy": "evaluated_checkpoint_greedy_epsilon_0",
                   "training_targets_continuation_matched": False,
                   "used_for_training_or_selection": False, "seed_overlap": False,
                   "checkpoint_hashes_unchanged": all(_file_hash(paths[i]) == h for i, h in hashes.items()),
                   "episode_count": len(episodes), "wave_count": len(waves),
                   "wall_sec": time.perf_counter() - started, "process_count": cfg["process_count"],
                   "config": cfg, "value_rows": value_rows, "action_rows": action_rows,
                   "conditional_future_contract": "Only draws after branch are resampled; existing latent samples, progress and scheduled events are retained.",
                   "candidate_scope": "Actual beam action plus sampled feasible alternatives; not exhaustive/global optimum."}
        _write_csv(output / "prediction_samples.csv", predictions)
        _write_csv(output / "value_metrics.csv", value_rows)
        _write_csv(output / "action_metrics.csv", action_rows)
        _write_csv(output / "action_continuations.csv", branch_rows)
        (output / "summary.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        return payload

    try:
        for iteration in sorted(paths):
            runtime = {"training": True, "force_random_policy": False,
                       "allow_wait_action": bool(summary.get("wait_action_enabled", False)),
                       "worker_order_strategy": summary.get("worker_order_strategy", "cyclic"),
                       "max_review_interval_min": float(summary.get("max_review_interval_min", 1.0))}
            world_cfg = _compose_episode_cfg(worker_count=int(cfg["worker_counts"][0]), seed=cfg["mc_seeds"][0],
                                             days=cfg["horizon_days"], adp_cfg=runtime)
            world = ManufacturingWorld(simpy.Environment(), world_cfg, InMemoryEventLogger(),
                                       build_decision_module(experiment_cfg=world_cfg, decision_mode="simulation_based_adp"))
            model, manifest = load_checkpoint(paths[iteration], world=world, device="cpu",
                                              wait_action_enabled=runtime["allow_wait_action"],
                                              worker_order_strategy=runtime["worker_order_strategy"])
            del world
            runtime["beam_width"] = int(manifest["model"]["beam_width"])
            snapshot, snapshot_hash = _snapshot_state_dict(model)

            def job(worker_count, seed, phase, extra=None):
                nonlocal job_number
                job_number += 1
                return RolloutJob(episode=job_number, phase=phase, iteration=iteration, worker_count=int(worker_count),
                                  seed=int(seed), days=cfg["horizon_days"], epsilon=0., force_random=False,
                                  adp_cfg={**runtime, **(extra or {})}, collect_compact_samples=False, gamma=1.,
                                  wave_id=phase, snapshot_hash=snapshot_hash)

            def collect(jobs):
                jobs = [replace(j, wave_id=f"{j.phase}-I{iteration}-W{index // cfg['process_count'] + 1}")
                        for index, j in enumerate(jobs)]
                print(f"Value validation I{iteration}: {jobs[0].phase}, {len(jobs)} episodes", flush=True)
                results, compact, _ = _run_rollout_jobs_parallel(
                    jobs=jobs, model_state=snapshot, model_cfg=manifest["model"],
                    process_count=cfg["process_count"], wave_size=cfg["process_count"], start_method="spawn",
                    torch_threads=1, output_dir=output, episode_rows=episodes, wave_rows=waves)
                if len(compact):
                    raise RuntimeError("Diagnostic samples must not enter a compact training batch.")
                expected_map = manifest.get("environment_fingerprints_by_worker_count", {})
                for result in results:
                    expected = expected_map.get(str(result.worker_count), manifest["environment_fingerprint"])
                    if result.fingerprint["environment_fingerprint"] != expected:
                        raise RuntimeError("Diagnostic environment fingerprint mismatch.")
                    if result.termination_reason != "completed_horizon" or result.raw_return != result.products:
                        raise RuntimeError("Incomplete diagnostic episode or raw reward mismatch.")
                return results

            if iteration in mc_iterations:
                results = collect([job(w, seed, "independent_value_mc", {"_value_validation_predictions": True})
                                   for w in cfg["worker_counts"] for seed in cfg["mc_seeds"]])
                for result in results:
                    for row in result.value_validation_samples or []:
                        predictions.append({"iteration": iteration, "worker_count": result.worker_count,
                                            "seed": result.seed, **row})
                for worker in cfg["worker_counts"]:
                    rows = [r for r in predictions if r["iteration"] == iteration and r["worker_count"] == worker]
                    actual = [r["target"] for r in rows]
                    metrics = regression_stats([r["prediction"] for r in rows], actual)
                    baseline = regression_stats(time_only_predictions(rows), actual)
                    value_rows.append({"iteration": iteration, "worker_count": worker, "episode_count": len(cfg["mc_seeds"]),
                                       **metrics, "time_only_rmse": baseline["rmse"], "snapshot_hash": snapshot_hash})
                persist("running")

            if iteration in action_iterations:
                captures = collect([job(w, seed, "action_state_capture", {
                    "_probe_capture_decision_thresholds": cfg["decision_thresholds"],
                    "_probe_candidate_limit": cfg["candidates_per_state"]})
                    for w in cfg["worker_counts"] for seed in cfg["action_seeds"]])
                plans, state_metadata = [], {}
                for origin in captures:
                    if len(origin.probe_records or []) != len(cfg["decision_thresholds"]):
                        raise RuntimeError("Could not capture every configured multi-action decision; no zero placeholder is allowed.")
                    for record in origin.probe_records:
                        key = f"I{iteration}-workers{origin.worker_count}-seed{origin.seed}-d{record['decision_number']}"
                        choices = [{"assignment": record["selected_assignment"], "post_state": record["selected_post_state"]}]
                        for candidate in record["candidates"]:
                            if all(candidate["assignment"] != prior["assignment"] for prior in choices):
                                choices.append(candidate)
                        choices = choices[:cfg["candidates_per_state"]]
                        estimates = predict_values(model, [deserialize_state(c["post_state"]) for c in choices], device="cpu")
                        state_metadata[key] = (origin.worker_count, record, choices, [float(v) for v in estimates])
                        for candidate_id, choice in enumerate(choices):
                            for future_seed in cfg["future_seeds"]:
                                plans.append(job(origin.worker_count, origin.seed, "paired_action_continuation", {
                                    "_probe_forced_action_script": record["prefix_actions"] + [choice["assignment"]],
                                    "_probe_target_decision_number": record["decision_number"],
                                    "_probe_expected_pre_state": record["pre_state"],
                                    "_probe_expected_post_state": choice["post_state"],
                                    "_probe_future_seed": future_seed,
                                    "_probe_id": key, "_probe_candidate_id": candidate_id}))
                results = collect(plans)
                for result, plan in zip(results, plans):
                    if result.episode != plan.episode or result.probe_target_product_count is None:
                        raise RuntimeError("Paired result identity/target mismatch.")
                    record = state_metadata[result.probe_id][1]
                    if result.probe_target_product_count != record["products_before"]:
                        raise RuntimeError("Raw product count changed during prefix replay.")
                    branch_rows.append({"iteration": iteration, "state_id": result.probe_id,
                                        "worker_count": result.worker_count, "candidate_id": result.probe_candidate_id,
                                        "future_seed": plan.adp_cfg["_probe_future_seed"],
                                        "target": result.products - result.probe_target_product_count,
                                        "products_before": result.probe_target_product_count, "products": result.products})
                for key, (worker, record, choices, estimates) in state_metadata.items():
                    returns = [[next(r["target"] for r in branch_rows if r["state_id"] == key
                                     and r["candidate_id"] == c and r["future_seed"] == seed)
                                for seed in cfg["future_seeds"]] for c in range(len(choices))]
                    action_result = paired_action_loss(returns)
                    empirical_means = action_result["candidate_mean_returns"]
                    predicted_best = int(np.argmax(estimates))
                    action_rows.append({"iteration": iteration, "worker_count": worker, "state_id": key,
                                        "time_min": record["time_min"], "predicted_values": estimates,
                                        "predicted_action_spread": float(max(estimates) - min(estimates)),
                                        "predicted_best_candidate_id": predicted_best,
                                        "selected_is_predicted_best_among_sampled": predicted_best == 0,
                                        "predicted_empirical_rank_correlation": rank_correlation(estimates, empirical_means),
                                        "candidate_assignments": [choice["assignment"] for choice in choices],
                                        "beam_selected_candidate_id": 0,
                                        "snapshot_hash": snapshot_hash, **action_result})
                persist("running")
        payload = persist("completed")
        if not payload["checkpoint_hashes_unchanged"]:
            raise RuntimeError("Checkpoint bytes changed during read-only diagnostics.")
        (output / "audit.json").write_text(json.dumps(audit_result(output), indent=2), encoding="utf-8")
        (training_dir / "value_validation_latest.json").write_text(
            json.dumps({"summary": str((output / "summary.json").relative_to(training_dir))}), encoding="utf-8")
    except Exception as exc:
        persist("failed", repr(exc))
        raise
    return output


def audit_result(output: Path) -> dict:
    data = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    if data["status"] != "completed" or not data["checkpoint_hashes_unchanged"]:
        raise ValueError("A completed, read-only diagnostic result is required.")
    predictions = _read_csv(output / "prediction_samples.csv")
    branches = _read_csv(output / "action_continuations.csv")
    for row in data["value_rows"]:
        samples = [r for r in predictions if int(r["iteration"]) == row["iteration"]
                   and int(r["worker_count"]) == row["worker_count"]]
        numeric = [{**r, "target": float(r["target"]), "remaining_fraction": float(r["remaining_fraction"])} for r in samples]
        check = regression_stats([float(r["prediction"]) for r in samples], [r["target"] for r in numeric])
        check["time_only_rmse"] = regression_stats(time_only_predictions(numeric), [r["target"] for r in numeric])["rmse"]
        for key, value in check.items():
            if not np.isclose(value, row[key], rtol=1e-10, atol=1e-10):
                raise ValueError(f"Independent MC aggregate mismatch: {key}")
    for row in data["action_rows"]:
        rows = [r for r in branches if r["state_id"] == row["state_id"]]
        mapping = {(int(r["candidate_id"]), int(r["future_seed"])): float(r["target"]) for r in rows}
        if len(rows) != len(mapping) or len(rows) != row["candidate_count"] * row["repeats"]:
            raise ValueError("Duplicate or missing paired continuations.")
        returns = [[mapping[c, seed] for seed in data["config"]["future_seeds"]] for c in range(row["candidate_count"])]
        for key, value in paired_action_loss(returns).items():
            if row[key] != value:
                raise ValueError(f"Paired action aggregate mismatch: {key}")
        if any(float(r["products"]) - float(r["products_before"]) != float(r["target"]) for r in rows):
            raise ValueError("Paired continuation raw reward mismatch.")
    return {"status": "passed", "prediction_samples": len(predictions), "paired_continuations": len(branches),
            "value_groups": len(data["value_rows"]), "action_states": len(data["action_rows"])}


def dashboard_panels(training_dir: Path) -> str:
    from .train import _chart_panel, _svg_chart

    pointer = training_dir / "value_validation_latest.json"
    title = "<div id='adp-independent-validation' class='chart-section-title'><h2>2. 독립 가치 검증과 행동 선택 검증</h2></div>"
    if not pointer.exists():
        return title + "<p style='grid-column:1/-1'>독립 MC 및 반복 행동 검증 미실행. 기존 batch MSE를 독립 검증 결과로 해석하지 마세요.</p>"
    path = (training_dir / json.loads(pointer.read_text(encoding="utf-8"))["summary"]).resolve()
    if not path.is_relative_to(training_dir.resolve()):
        raise ValueError("Diagnostic summary must stay inside its training directory.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["status"] != "completed":
        return title + "<p style='grid-column:1/-1'>독립 검증이 완료되지 않았습니다. 결측값은 0이 아닙니다.</p>"
    result = [title, f"<p style='grid-column:1/-1'>학습·epoch·checkpoint 선정에 미사용. 추가 진단 {data['episode_count']} episode / "
              f"{data['wave_count']} wave / {data['wall_sec']:.1f}초, 최대 {data['process_count']} CPU process. "
              "기존 학습 시간·wave에 포함되지 않는 별도 진단입니다.</p>"]
    if len(data["config"]["mc_seeds"]) < 3 or len(data["config"]["future_seeds"]) < 5:
        result.append("<p class='warning' style='grid-column:1/-1'>소형 검증 결과입니다. 코드·집계 확인용 작은 표본이며 수렴이나 정책 우위를 입증하지 않습니다.</p>")
    for worker in data["config"]["worker_counts"]:
        rows = sorted([r for r in data["value_rows"] if r["worker_count"] == worker], key=lambda r: r["iteration"])
        if rows:
            result.append(_chart_panel(
                f"독립 Greedy MC 가치 예측 오차 · Worker {worker}",
                _svg_chart([("가치망 RMSE", [r["rmse"] for r in rows], "#43a5ff"),
                            ("시간만 사용한 기준 RMSE", [r["time_only_rmse"] for r in rows], "#9caebf"),
                            ("평균 편향 (예측−실측)", [r["bias"] for r in rows], "#ff8b72")],
                           x_values=[r["iteration"] for r in rows], x_label="평가한 checkpoint iteration",
                           y_label="MC 예측 오차 (개)", include_zero=True),
                "RMSE는 낮을수록 좋습니다. 편향이 양수이면 과대평가, 음수이면 과소평가입니다. 시간 기준보다 오차가 작으면 시간 경과 이외의 정보도 예측에 기여한다는 근거입니다.",
                f"Checkpoint 자체의 greedy 행동과 동일 greedy 후속 정책으로 새 seed {len(data['config']['mc_seeds'])}개를 실행했습니다. "
                "학습 당시 epsilon-greedy 후속 정책의 가치와 다른, 배포 정책 기준의 보정 정확도입니다. "
                "시간 기준은 평가 중인 seed를 제외하고 적합합니다. 오차는 decision 표본 가중 평균이며, MC 확률 잡음도 포함합니다. 평가 seed는 고정되지만 checkpoint별 방문 상태는 달라집니다."))
        actions = [r for r in data["action_rows"] if r["worker_count"] == worker]
        if actions:
            indices = sorted({r["iteration"] for r in actions})
            groups = [[r for r in actions if r["iteration"] == i] for i in indices]
            averages = lambda key: [float(np.mean([r[key] for r in group])) for group in groups]
            result.append(_chart_panel(
                f"동일 상태 반복 MC 행동 선택 손실 · Worker {worker}",
                _svg_chart([("관측 평균 선택 손실", averages("observed_selection_loss"), "#f5b85b")],
                           x_values=indices, x_label="평가한 checkpoint iteration",
                           y_label="선택 손실 (제품 수)", include_zero=True,
                           error_ranges={"관측 평균 선택 손실": (averages("ci95_low"), averages("ci95_high"))}),
                "실제 beam 선택 행동보다 다른 후보가 이후 생산량을 더 높였는지 확인합니다. 손실이 크고 구간 하한도 0보다 높으면 관측한 상태·후보에서 선택 개선 여지가 있다는 근거입니다. 0에 가까워도 전역 최적 정책임을 뜻하지 않습니다.",
                f"상태별 후보 {data['config']['candidates_per_state']}개 이하, 후보당 미래 seed {len(data['config']['future_seeds'])}개로 반복합니다. "
                "같은 상태의 모든 후보는 같은 미래 seed 집합과 평가 checkpoint의 greedy 후속 정책을 사용합니다. "
                "이미 샘플된 진행 작업·고장 잔여량은 유지하고 새 확률 추첨만 바꿉니다. "
                "오차막대는 후보 최대값 선택 편향을 고려한 paired bootstrap 근사 구간의 상태별 평균 경계이며 모집단 일반화 CI가 아닙니다. "
                "표본이 작으면 불확실성이 크고, 구간이 0을 포함하면 순위 우위를 확정할 수 없습니다. "
                "손실에는 가치 예측과 beam 탐색 모두가 영향을 줄 수 있습니다. 상태·반복별 원자료는 별도 CSV에 보존합니다."))
    link = html.escape(path.relative_to(training_dir.resolve()).as_posix(), quote=True)
    result.append(f"<p style='grid-column:1/-1'><a href='{link}'>독립 검증 설정·계산 결과 JSON</a></p>")
    return "\n".join(result)


def refresh_dashboard(training_dir: Path) -> Path:
    from .train import render_training_dashboard

    return render_training_dashboard(training_dir, _read_csv(training_dir / "episode_metrics.csv"),
                                     _read_csv(training_dir / "iteration_metrics.csv"),
                                     _read_csv(training_dir / "wave_metrics.csv"),
                                     json.loads((training_dir / "training_summary.json").read_text(encoding="utf-8")))


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only independent ADP value/action validation; no retraining.")
    parser.add_argument("--training-dir", type=Path, required=True)
    parser.add_argument("--iterations", type=int, nargs="+")
    parser.add_argument("--processes", type=int, default=10)
    parser.add_argument("--action-seeds", type=int, nargs="+")
    parser.add_argument("--decision-thresholds", type=int, nargs="+")
    parser.add_argument("--future-seeds", type=int, nargs="+")
    parser.add_argument("--candidates-per-state", type=int)
    parser.add_argument("--smoke", action="store_true", help="One checkpoint, two MC seeds, one state, two actions, three repeats.")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    cfg = {"process_count": args.processes}
    for key in ("action_seeds", "decision_thresholds", "future_seeds", "candidates_per_state"):
        value = getattr(args, key)
        if value is not None:
            cfg[key] = value
    if args.iterations:
        cfg.update(checkpoints=args.iterations, action_checkpoints=args.iterations)
    if args.smoke:
        checkpoint = (args.iterations or ["best"])[0]
        cfg.update(checkpoints=[checkpoint], action_checkpoints=[checkpoint], mc_seeds=DEFAULTS["mc_seeds"][:2],
                   decision_thresholds=[20], candidates_per_state=2, future_seeds=DEFAULTS["future_seeds"][:3])
    if not args.render_only:
        print(run_validation(args.training_dir, cfg))
    print(refresh_dashboard(args.training_dir.resolve()))


if __name__ == "__main__":
    main()
