from __future__ import annotations

import argparse
import copy
import hashlib
import io
import csv
import gc
import html as html_lib
import json
import math
import multiprocessing
import os
import platform
import random
import socket
import statistics
import sys
import time
import webbrowser
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import simpy

from manufacturing_sim import __version__ as mansim_version
from hydra import compose, initialize_config_dir
from omegaconf import OmegaConf

from agents.factory import build_decision_module
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld
from runtime.compat import build_legacy_experiment_cfg

from .checkpoint import checkpoint_fingerprint, save_checkpoint
from .compact import CompactMCBatch, compact_episode_transitions, merge_compact_batches, stratified_episode_split
from .model import build_value_network, predict_values, require_torch
from .ood_diagnostics import build_ood_support_bank, evaluate_ood_selected_actions
from .schema import (
    FEATURE_SCHEMA_VERSION,
    EncodedDecisionState,
    deserialize_state,
)


class InMemoryEventLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.closed = False

    def log(self, *, t: float, day: int, event_type: str, entity_id: str = "", location: str = "", details: dict[str, Any] | None = None) -> None:
        if not self.closed:
            self.events.append(
                {"t": round(float(t), 6), "day": int(day), "type": event_type, "entity_id": entity_id, "location": location, "details": details or {}}
            )

    def write_json(self, filename: str, payload: dict[str, Any]) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@dataclass
class EpisodeResult:
    episode: int
    phase: str
    worker_count: int
    seed: int
    products: int
    scrap: int
    raw_return: float
    decisions: int
    fingerprint: dict[str, Any]
    wait_count: int = 0
    candidate_available_wait_count: int = 0
    no_candidate_unassigned_count: int = 0
    assigned_task_count: int = 0
    joint_all_wait_count: int = 0
    joint_all_wait_with_candidate_count: int = 0
    joint_no_candidate_count: int = 0
    max_consecutive_all_wait_decisions: int = 0
    max_consecutive_candidate_all_wait_decisions: int = 0
    compact_sample_count: int = 0
    compact_memory_bytes: int = 0
    iteration: int = 0
    wave_id: str = ""
    process_slot: str = ""
    elapsed_sec: float = 0.0
    child_peak_rss_mib: float = 0.0
    snapshot_hash: str = ""
    probe_records: list[dict[str, Any]] | None = None
    probe_target_product_count: int | None = None
    probe_id: str = ""
    probe_candidate_id: int = -1


@dataclass(frozen=True)
class RolloutJob:
    episode: int
    phase: str
    iteration: int
    worker_count: int
    seed: int
    days: int
    epsilon: float
    force_random: bool
    adp_cfg: dict[str, Any]
    collect_compact_samples: bool
    gamma: float
    wave_id: str
    snapshot_hash: str


@dataclass
class RolloutBatchMetrics:
    episode_count: int = 0
    wave_count: int = 0
    wall_sec: float = 0.0
    merge_sec: float = 0.0
    coordination_ipc_overhead_sec: float = 0.0
    episode_elapsed_sum_sec: float = 0.0
    episode_elapsed_avg_sec: float = 0.0
    episodes_per_hour: float = 0.0
    effective_speedup: float = 0.0
    parallel_efficiency: float = 0.0
    active_slot_utilization: float = 0.0
    ipc_payload_mib: float = 0.0
    child_peak_rss_avg_mib: float = 0.0
    child_peak_rss_max_mib: float = 0.0
    actual_process_count: int = 0


_ROLLOUT_MODEL: Any | None = None
_ROLLOUT_DEVICE: Any | None = None


def _snapshot_state_dict(model: Any | None) -> tuple[dict[str, Any] | None, str]:
    if model is None:
        return None, "RANDOM"
    torch = require_torch()
    state = {name: value.detach().cpu().clone() for name, value in model.state_dict().items()}
    digest = hashlib.sha256()
    for name in sorted(state):
        tensor = state[name].contiguous()
        digest.update(name.encode("utf-8"))
        digest.update(str(tensor.dtype).encode("ascii"))
        digest.update(str(tuple(tensor.shape)).encode("ascii"))
        buffer = io.BytesIO()
        torch.save(tensor, buffer)
        digest.update(buffer.getvalue())
    return state, digest.hexdigest()[:16]


def _initialize_rollout_process(
    model_state: dict[str, Any] | None,
    model_cfg: dict[str, Any],
    torch_threads: int,
) -> None:
    global _ROLLOUT_MODEL, _ROLLOUT_DEVICE
    thread_count = max(1, int(torch_threads))
    os.environ["OMP_NUM_THREADS"] = str(thread_count)
    os.environ["MKL_NUM_THREADS"] = str(thread_count)
    os.environ["OPENBLAS_NUM_THREADS"] = str(thread_count)
    torch = require_torch()
    torch.set_num_threads(thread_count)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    _ROLLOUT_DEVICE = torch.device("cpu")
    _ROLLOUT_MODEL = None
    if model_state is not None:
        _ROLLOUT_MODEL = build_value_network(
            embedding_dim=int(model_cfg["embedding_dim"]),
            heads=int(model_cfg["heads"]),
            layers=int(model_cfg["layers"]),
        ).to(_ROLLOUT_DEVICE)
        _ROLLOUT_MODEL.load_state_dict(model_state)
        _ROLLOUT_MODEL.eval()


def _execute_rollout_job(job: RolloutJob) -> tuple[EpisodeResult, CompactMCBatch | None]:
    started = time.perf_counter()
    result, compact = run_training_episode(
        episode=job.episode,
        phase=job.phase,
        worker_count=job.worker_count,
        seed=job.seed,
        days=job.days,
        model=None if job.force_random else _ROLLOUT_MODEL,
        device=_ROLLOUT_DEVICE,
        force_random=job.force_random,
        epsilon=job.epsilon,
        adp_cfg=job.adp_cfg,
        collect_compact_samples=job.collect_compact_samples,
        gamma=job.gamma,
    )
    result.iteration = int(job.iteration)
    result.wave_id = job.wave_id
    result.process_slot = multiprocessing.current_process().name
    result.elapsed_sec = time.perf_counter() - started
    result.child_peak_rss_mib = _process_rss_mib(peak=True)
    result.snapshot_hash = job.snapshot_hash
    return result, compact


def _wave_chunks(jobs: list[RolloutJob], wave_size: int) -> list[list[RolloutJob]]:
    size = max(1, int(wave_size))
    return [jobs[index : index + size] for index in range(0, len(jobs), size)]


def _wave_performance_row(
    *,
    wave_jobs: list[RolloutJob],
    results: list[EpisodeResult],
    wave_wall_sec: float,
    configured_process_count: int,
    status: str = "completed",
    error: str = "",
) -> dict[str, Any]:
    elapsed = [float(result.elapsed_sec) for result in results]
    active_processes = max(1, min(int(configured_process_count), len(wave_jobs)))
    elapsed_sum = sum(elapsed)
    speedup = elapsed_sum / max(1e-9, float(wave_wall_sec))
    efficiency = speedup / active_processes
    process_names = sorted({result.process_slot for result in results if result.process_slot})
    phases = sorted({str(job.phase) for job in wave_jobs if str(job.phase)})
    phase = phases[0] if len(phases) == 1 else "checkpoint_diagnostic"
    return {
        "wave_id": wave_jobs[0].wave_id if wave_jobs else "",
        "iteration": wave_jobs[0].iteration if wave_jobs else 0,
        "phase": phase,
        "phase_components": ",".join(phases),
        "status": status,
        "error": error,
        "episode_start": min((job.episode for job in wave_jobs), default=0),
        "episode_end": max((job.episode for job in wave_jobs), default=0),
        "episode_count": len(wave_jobs),
        "configured_process_count": int(configured_process_count),
        "active_process_count": active_processes,
        "actual_process_count": len(process_names),
        "process_slots": ",".join(process_names),
        "wall_sec": round(float(wave_wall_sec), 6),
        "episode_elapsed_sum_sec": round(elapsed_sum, 6),
        "episode_elapsed_avg_sec": round(statistics.fmean(elapsed) if elapsed else 0.0, 6),
        "episode_elapsed_min_sec": round(min(elapsed) if elapsed else 0.0, 6),
        "episode_elapsed_max_sec": round(max(elapsed) if elapsed else 0.0, 6),
        "episodes_per_hour": round(len(results) * 3600.0 / max(1e-9, float(wave_wall_sec)), 6),
        "effective_speedup": round(speedup, 6),
        "parallel_efficiency": round(efficiency, 6),
        "active_slot_utilization": round(efficiency, 6),
        "coordination_ipc_overhead_sec": round(
            max(0.0, float(wave_wall_sec) - (max(elapsed) if elapsed else 0.0)), 6
        ),
        "ipc_payload_mib": round(
            sum(result.compact_memory_bytes for result in results) / (1024.0**2), 6
        ),
        "child_peak_rss_avg_mib": round(
            statistics.fmean(result.child_peak_rss_mib for result in results) if results else 0.0, 6
        ),
        "child_peak_rss_max_mib": round(
            max((result.child_peak_rss_mib for result in results), default=0.0), 6
        ),
        "snapshot_hash": wave_jobs[0].snapshot_hash if wave_jobs else "",
        "snapshot_hash_match": len({result.snapshot_hash for result in results}) <= 1,
    }


def _compose_episode_cfg(*, worker_count: int, seed: int, days: int, adp_cfg: dict[str, Any]) -> dict[str, Any]:
    config_dir = str((Path(__file__).resolve().parents[2] / "configs").resolve())
    with initialize_config_dir(version_base=None, config_dir=config_dir):
        cfg = compose(
            config_name="config",
            overrides=[
                "scenario=mfg_flow_shop",
                "decision=simulation_based_adp",
                "scenario.objective.mode=maximize_throughput",
                f"scenario.horizon.num_days={int(days)}",
                f"scenario.factory.num_workers={int(worker_count)}",
                f"seed={int(seed)}",
                "decision.adp.training=true",
                "decision.adp.force_random_policy=true",
                "runtime.ui.auto_open_results=false",
                "runtime.ui.auto_start_replay_studio_3d=false",
            ],
        )
    payload = build_legacy_experiment_cfg(cfg)
    payload["decision"]["adp"].update(adp_cfg)
    payload["decision"]["adp"]["training"] = True
    return payload


def run_training_episode(
    *,
    episode: int,
    phase: str,
    worker_count: int,
    seed: int,
    days: int,
    model: Any | None,
    device: Any,
    force_random: bool,
    epsilon: float,
    adp_cfg: dict[str, Any],
    collect_compact_samples: bool = True,
    gamma: float = 1.0,
) -> tuple[EpisodeResult, CompactMCBatch | None]:
    cfg = _compose_episode_cfg(worker_count=worker_count, seed=seed, days=days, adp_cfg=adp_cfg)
    logger = InMemoryEventLogger()
    env = simpy.Environment()
    decision_module = build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp")
    world = ManufacturingWorld(env=env, cfg=cfg, logger=logger, decision_module=decision_module)
    world.adp_coordinator.set_training_policy(
        model=model,
        device=device,
        force_random=force_random,
        epsilon=epsilon,
    )
    world.bootstrap()
    last_summary: dict[str, Any] | None = None
    for day in range(1, world.num_days + 1):
        world.prepare_objective_day(day)
        observation = world.build_observation(last_summary)
        strategy = decision_module.reflect(observation)
        job_plan = decision_module.propose_jobs(observation, strategy, {})
        world.start_day(day, strategy, job_plan)
        day_end = day * world.minutes_per_day
        if env.now < day_end and not world.terminated:
            stop = env.any_of([world.termination_event, env.timeout(day_end - env.now)])
            env.run(until=stop)
        last_summary = world.finalize_day(day)
        if world.terminated:
            break
    if not world.termination_reason:
        world.termination_reason = "completed_horizon"
    transitions = world.adp_coordinator.finalize_episode()
    raw_return = sum(float(row["raw_reward"]) for row in transitions)
    compact_batch = (
        compact_episode_transitions(
            transitions,
            episode_id=episode,
            worker_count=worker_count,
            gamma=gamma,
        )
        if collect_compact_samples
        else None
    )
    result = EpisodeResult(
        episode=episode,
        phase=phase,
        worker_count=worker_count,
        seed=seed,
        products=int(world.product_count),
        scrap=int(world.scrap_count),
        raw_return=raw_return,
        decisions=int(world.adp_coordinator.metrics["decision_count"]),
        fingerprint=checkpoint_fingerprint(world),
        wait_count=int(world.adp_coordinator.metrics["wait_count"]),
        candidate_available_wait_count=int(
            world.adp_coordinator.metrics["candidate_available_wait_count"]
        ),
        no_candidate_unassigned_count=int(
            world.adp_coordinator.metrics["no_candidate_unassigned_count"]
        ),
        assigned_task_count=int(world.adp_coordinator.metrics["assigned_task_count"]),
        joint_all_wait_count=int(world.adp_coordinator.metrics["joint_all_wait_count"]),
        joint_all_wait_with_candidate_count=int(
            world.adp_coordinator.metrics["joint_all_wait_with_candidate_count"]
        ),
        joint_no_candidate_count=int(
            world.adp_coordinator.metrics["joint_no_candidate_count"]
        ),
        max_consecutive_all_wait_decisions=int(
            world.adp_coordinator.metrics["max_consecutive_all_wait_decisions"]
        ),
        max_consecutive_candidate_all_wait_decisions=int(
            world.adp_coordinator.metrics[
                "max_consecutive_candidate_all_wait_decisions"
            ]
        ),
        compact_sample_count=len(compact_batch) if compact_batch is not None else 0,
        compact_memory_bytes=compact_batch.memory_bytes if compact_batch is not None else 0,
        probe_records=list(world.adp_coordinator.probe_records),
        probe_target_product_count=world.adp_coordinator.probe_target_product_count,
        probe_id=str(adp_cfg.get("_probe_id", "") or ""),
        probe_candidate_id=int(adp_cfg.get("_probe_candidate_id", -1)),
    )
    transitions.clear()
    logger.close()
    logger.events.clear()
    del world
    del env
    gc.collect()
    return result, compact_batch


def _run_rollout_jobs_parallel(
    *,
    jobs: list[RolloutJob],
    model_state: dict[str, Any] | None,
    model_cfg: dict[str, Any],
    process_count: int,
    wave_size: int,
    start_method: str,
    torch_threads: int,
    output_dir: Path,
    episode_rows: list[dict[str, Any]],
    wave_rows: list[dict[str, Any]],
) -> tuple[list[EpisodeResult], CompactMCBatch, RolloutBatchMetrics]:
    if not jobs:
        return [], merge_compact_batches([]), RolloutBatchMetrics()
    max_processes = max(1, int(process_count))
    chunks = _wave_chunks(jobs, min(max_processes, max(1, int(wave_size))))
    all_results: list[EpisodeResult] = []
    compact_episodes: list[CompactMCBatch] = []
    batch_started = time.perf_counter()
    context = multiprocessing.get_context(start_method)
    with ProcessPoolExecutor(
        max_workers=max_processes,
        mp_context=context,
        initializer=_initialize_rollout_process,
        initargs=(model_state, model_cfg, torch_threads),
    ) as executor:
        for wave_jobs in chunks:
            wave_started = time.perf_counter()
            futures = {executor.submit(_execute_rollout_job, job): job for job in wave_jobs}
            wave_results: list[EpisodeResult] = []
            wave_compact: list[CompactMCBatch] = []
            try:
                for future in as_completed(futures):
                    result, compact = future.result()
                    wave_results.append(result)
                    if compact is not None:
                        wave_compact.append(compact)
            except BaseException as exc:
                cancelled_count = 0
                for future in futures:
                    cancelled_count += int(future.cancel())
                wave_wall = time.perf_counter() - wave_started
                failed_row = _wave_performance_row(
                    wave_jobs=wave_jobs,
                    results=wave_results,
                    wave_wall_sec=wave_wall,
                    configured_process_count=max_processes,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                failed_row["completed_episode_count"] = len(wave_results)
                failed_row["cancelled_episode_count"] = cancelled_count
                wave_rows.append(failed_row)
                _write_csv(output_dir / "episode_metrics.csv", episode_rows)
                _write_csv(output_dir / "wave_metrics.csv", wave_rows)
                partial_summary = {
                    "training_episode_count": sum(
                        1 for row in episode_rows if row.get("phase") != "validation"
                    ),
                    "validation_episode_count": sum(
                        1 for row in episode_rows if row.get("phase") == "validation"
                    ),
                    "configured_process_count": max_processes,
                    "actual_process_count_max": max(
                        (int(row.get("actual_process_count", 0)) for row in wave_rows), default=0
                    ),
                    "wave_size": wave_size,
                    "multiprocessing_start_method": start_method,
                    "rollout_device": "cpu",
                    "training_device": "unknown",
                    "torch_threads_per_process": torch_threads,
                    "total_wave_count": len(wave_rows),
                    "failed_wave_count": 1,
                    "cancelled_episode_count": cancelled_count,
                    "retried_episode_count": 0,
                    "failure": failed_row["error"],
                }
                (output_dir / "training_summary.json").write_text(
                    json.dumps(partial_summary, indent=2), encoding="utf-8"
                )
                render_training_dashboard(output_dir, episode_rows, [], wave_rows, partial_summary)
                raise RuntimeError(
                    f"ADP rollout wave {wave_jobs[0].wave_id} failed; no partial MC update was applied."
                ) from exc
            wave_wall = time.perf_counter() - wave_started
            wave_results.sort(key=lambda item: item.episode)
            wave_compact.sort(
                key=lambda item: int(item.episode_ids[0].item()) if len(item) else 0
            )
            expected_hash = wave_jobs[0].snapshot_hash
            observed_hashes = {result.snapshot_hash for result in wave_results}
            if observed_hashes != {expected_hash}:
                raise RuntimeError(
                    f"ADP rollout wave {wave_jobs[0].wave_id} used inconsistent policy snapshots: "
                    f"expected={expected_hash}, observed={sorted(observed_hashes)}"
                )
            all_results.extend(wave_results)
            compact_episodes.extend(wave_compact)
            episode_rows.extend(_episode_metric_row(result) for result in wave_results)
            wave_rows.append(
                _wave_performance_row(
                    wave_jobs=wave_jobs,
                    results=wave_results,
                    wave_wall_sec=wave_wall,
                    configured_process_count=max_processes,
                )
            )
            _write_csv(output_dir / "episode_metrics.csv", episode_rows)
            _write_csv(output_dir / "wave_metrics.csv", wave_rows)
    all_results.sort(key=lambda item: item.episode)
    compact_episodes.sort(
        key=lambda item: int(item.episode_ids[0].item()) if len(item) else 0
    )
    merge_started = time.perf_counter()
    compact_batch = merge_compact_batches(compact_episodes)
    merge_sec = time.perf_counter() - merge_started
    compact_episodes.clear()
    gc.collect()
    elapsed_values = [float(result.elapsed_sec) for result in all_results]
    relevant_wave_rows = wave_rows[-len(chunks) :]
    wave_wall_sum = sum(float(row["wall_sec"]) for row in relevant_wave_rows)
    elapsed_sum = sum(elapsed_values)
    slot_capacity = sum(
        float(row["wall_sec"]) * max(1, int(row["active_process_count"]))
        for row in relevant_wave_rows
    )
    speedup = elapsed_sum / max(1e-9, wave_wall_sum)
    metrics = RolloutBatchMetrics(
        episode_count=len(all_results),
        wave_count=len(chunks),
        wall_sec=time.perf_counter() - batch_started,
        merge_sec=merge_sec,
        coordination_ipc_overhead_sec=sum(
            float(row["coordination_ipc_overhead_sec"]) for row in relevant_wave_rows
        ),
        episode_elapsed_sum_sec=elapsed_sum,
        episode_elapsed_avg_sec=statistics.fmean(elapsed_values) if elapsed_values else 0.0,
        episodes_per_hour=len(all_results) * 3600.0 / max(1e-9, wave_wall_sum),
        effective_speedup=speedup,
        # Partial validation waves use fewer slots than the configured pool.
        parallel_efficiency=elapsed_sum / max(1e-9, slot_capacity),
        active_slot_utilization=elapsed_sum / max(1e-9, slot_capacity),
        ipc_payload_mib=sum(result.compact_memory_bytes for result in all_results) / (1024.0**2),
        child_peak_rss_avg_mib=(
            statistics.fmean(result.child_peak_rss_mib for result in all_results)
            if all_results
            else 0.0
        ),
        child_peak_rss_max_mib=max(
            (result.child_peak_rss_mib for result in all_results), default=0.0
        ),
        actual_process_count=max(
            (int(row["actual_process_count"]) for row in relevant_wave_rows), default=0
        ),
    )
    return all_results, compact_batch, metrics


def monte_carlo_samples(transitions: list[dict[str, Any]], gamma: float = 1.0) -> list[tuple[EncodedDecisionState, float]]:
    target = 0.0
    rows: list[tuple[EncodedDecisionState, float]] = []
    for transition in reversed(transitions):
        target = float(transition["raw_reward"]) + float(gamma) * target
        rows.append((transition["post_state"], target))
    rows.reverse()
    return rows


def _mc_regression_loss(predictions: Any, targets: Any) -> Any:
    torch = require_torch()
    return torch.nn.functional.mse_loss(predictions, targets)


def fit_mc_value(
    *,
    model: Any,
    optimizer: Any,
    samples: CompactMCBatch,
    device: Any,
    batch_size: int,
    max_epochs: int,
    gradient_clip: float,
    patience: int,
    validation_episode_fraction: float,
    rng: random.Random,
) -> tuple[float, int]:
    torch = require_torch()
    if not len(samples):
        return 0.0, 0
    train_indices, validation_indices = stratified_episode_split(
        samples,
        validation_fraction=validation_episode_fraction,
        rng=rng,
    )
    if not train_indices:
        train_indices = list(validation_indices)
        validation_indices = []
    best_loss = math.inf
    best_state: dict[str, Any] | None = None
    best_optimizer_state: dict[str, Any] | None = None
    stale_epochs = 0
    completed_epochs = 0
    for epoch in range(max(1, int(max_epochs))):
        rng.shuffle(train_indices)
        model.train()
        for start in range(0, len(train_indices), max(1, int(batch_size))):
            batch_indices = train_indices[start : start + max(1, int(batch_size))]
            batch, targets = samples.model_batch(batch_indices, device)
            predictions = model(**batch)
            loss = _mc_regression_loss(predictions, targets)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), float(gradient_clip))
            optimizer.step()
        completed_epochs = epoch + 1
        evaluation_indices = validation_indices or train_indices
        model.eval()
        weighted_loss_sum = 0.0
        evaluated_sample_count = 0
        with torch.no_grad():
            for start in range(0, len(evaluation_indices), max(1, int(batch_size))):
                batch_indices = evaluation_indices[start : start + max(1, int(batch_size))]
                batch, targets = samples.model_batch(batch_indices, device)
                batch_loss = float(_mc_regression_loss(model(**batch), targets).item())
                weighted_loss_sum += batch_loss * len(batch_indices)
                evaluated_sample_count += len(batch_indices)
        current = weighted_loss_sum / evaluated_sample_count if evaluated_sample_count else 0.0
        if current + 1e-9 < best_loss:
            best_loss = current
            best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
            best_optimizer_state = copy.deepcopy(optimizer.state_dict())
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= max(1, int(patience)):
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    if best_optimizer_state is not None:
        optimizer.load_state_dict(best_optimizer_state)
    return float(best_loss if math.isfinite(best_loss) else 0.0), completed_epochs


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row}) if rows else ["status"]
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _format_axis_tick(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return f"{int(round(value)):,}"
    if abs(value) >= 1000.0:
        return f"{value:,.0f}"
    if abs(value) >= 10.0:
        return f"{value:.1f}"
    if abs(value) >= 1.0:
        return f"{value:.2f}"
    return f"{value:.3f}"


def _svg_chart(
    series: list[tuple[str, list[float], str]],
    *,
    x_values: list[float] | None,
    x_label: str,
    y_label: str,
    include_zero: bool = False,
    error_ranges: dict[str, tuple[list[float], list[float]]] | None = None,
    dashed_series: set[str] | None = None,
    width: int = 720,
    height: int = 250,
) -> str:
    non_empty = [(name, values, color) for name, values, color in series if values]
    if not non_empty:
        return "<div class='empty'>No data</div>"

    point_count = max(len(values) for _, values, _ in non_empty)
    xs = list(x_values or range(point_count))
    if len(xs) < point_count:
        xs.extend(float(index) for index in range(len(xs), point_count))
    xs = [float(value) for value in xs[:point_count]]
    all_y = [float(value) for _, values, _ in non_empty for value in values]
    for name, _, _ in non_empty:
        if error_ranges and name in error_ranges:
            lows, highs = error_ranges[name]
            all_y.extend(float(value) for value in lows)
            all_y.extend(float(value) for value in highs)
    low = min(all_y)
    high = max(all_y)
    if include_zero:
        low = min(0.0, low)
        high = max(0.0, high)
    if math.isclose(low, high, abs_tol=1e-12):
        padding = max(1.0, abs(low) * 0.1)
        low -= padding
        high += padding

    x_low = min(xs)
    x_high = max(xs)
    if math.isclose(x_low, x_high, abs_tol=1e-12):
        x_low -= 0.5
        x_high += 0.5

    margin_left, margin_right, margin_top, margin_bottom = 78.0, 20.0, 22.0, 58.0
    plot_width = width - margin_left - margin_right
    plot_height = height - margin_top - margin_bottom

    def map_x(value: float) -> float:
        return margin_left + plot_width * (value - x_low) / (x_high - x_low)

    def map_y(value: float) -> float:
        return margin_top + plot_height * (high - value) / (high - low)

    y_grid: list[str] = []
    for index in range(5):
        value = low + (high - low) * index / 4.0
        y = map_y(value)
        y_grid.append(
            f"<line x1='{margin_left:.1f}' y1='{y:.1f}' x2='{width - margin_right:.1f}' y2='{y:.1f}' class='grid-line'/>"
            f"<text x='{margin_left - 10:.1f}' y='{y + 4:.1f}' class='tick y-tick'>{html_lib.escape(_format_axis_tick(value))}</text>"
        )

    x_grid: list[str] = []
    x_tick_count = min(6, point_count)
    x_tick_indexes = sorted(
        {int(round(index * (point_count - 1) / max(1, x_tick_count - 1))) for index in range(x_tick_count)}
    )
    for index in x_tick_indexes:
        value = xs[index]
        x = map_x(value)
        x_grid.append(
            f"<line x1='{x:.1f}' y1='{margin_top:.1f}' x2='{x:.1f}' y2='{height - margin_bottom:.1f}' class='grid-line'/>"
            f"<text x='{x:.1f}' y='{height - margin_bottom + 20:.1f}' class='tick x-tick'>{html_lib.escape(_format_axis_tick(value))}</text>"
        )

    lines: list[str] = []
    legend: list[str] = []
    for name, values, color in non_empty:
        coordinates = [
            (map_x(xs[index]), map_y(float(value)), float(value))
            for index, value in enumerate(values)
        ]
        if error_ranges and name in error_ranges:
            lows, highs = error_ranges[name]
            for index in range(min(len(coordinates), len(lows), len(highs))):
                x, _, _ = coordinates[index]
                y_low = map_y(float(lows[index]))
                y_high = map_y(float(highs[index]))
                lines.append(
                    f"<line x1='{x:.1f}' y1='{y_low:.1f}' x2='{x:.1f}' y2='{y_high:.1f}' "
                    f"stroke='{color}' stroke-width='1.4' opacity='0.55'/>"
                    f"<line x1='{x - 3:.1f}' y1='{y_low:.1f}' x2='{x + 3:.1f}' y2='{y_low:.1f}' "
                    f"stroke='{color}' stroke-width='1.4' opacity='0.55'/>"
                    f"<line x1='{x - 3:.1f}' y1='{y_high:.1f}' x2='{x + 3:.1f}' y2='{y_high:.1f}' "
                    f"stroke='{color}' stroke-width='1.4' opacity='0.55'/>"
                )
        points = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in coordinates)
        dash = " stroke-dasharray='8 6'" if dashed_series and name in dashed_series else ""
        lines.append(
            f"<polyline fill='none' stroke='{color}' stroke-width='3'{dash} points='{points}'/>"
        )
        if len(coordinates) == 1:
            x, y, value = coordinates[0]
            label_y = min(height - margin_bottom - 8.0, max(margin_top + 16.0, y + 18.0))
            lines.append(
                f"<circle cx='{x:.1f}' cy='{y:.1f}' r='6' fill='{color}' stroke='#ffffff' stroke-width='2'>"
                f"<title>{html_lib.escape(name)}: {html_lib.escape(_format_axis_tick(value))}</title></circle>"
                f"<text x='{x:.1f}' y='{label_y:.1f}' class='point-label'>{html_lib.escape(_format_axis_tick(value))}</text>"
            )
        legend_style = (
            f"border-top:3px dashed {color};background:transparent;height:0"
            if dashed_series and name in dashed_series
            else f"background:{color}"
        )
        legend.append(f"<span><i style='{legend_style}'></i>{html_lib.escape(name)}</span>")

    return (
        "<div class='chart-wrap'>"
        f"<svg class='metric-chart' viewBox='0 0 {width} {height}' role='img' "
        f"aria-label='{html_lib.escape(y_label)} by {html_lib.escape(x_label)}'>"
        + "".join(y_grid)
        + "".join(x_grid)
        + f"<line x1='{margin_left:.1f}' y1='{height - margin_bottom:.1f}' x2='{width - margin_right:.1f}' y2='{height - margin_bottom:.1f}' class='axis-line'/>"
        + f"<line x1='{margin_left:.1f}' y1='{margin_top:.1f}' x2='{margin_left:.1f}' y2='{height - margin_bottom:.1f}' class='axis-line'/>"
        + "".join(lines)
        + f"<text x='{margin_left + plot_width / 2:.1f}' y='{height - 8:.1f}' class='axis-label x-axis-label'>{html_lib.escape(x_label)}</text>"
        + f"<text x='18' y='{margin_top + plot_height / 2:.1f}' class='axis-label y-axis-label' transform='rotate(-90 18 {margin_top + plot_height / 2:.1f})'>{html_lib.escape(y_label)}</text>"
        + "</svg>"
        + "<div class='chart-legend'>"
        + "".join(legend)
        + "</div></div>"
    )


def _chart_panel(title: str, chart: str, diagnosis: str, detail: str = "") -> str:
    detail_html = f"<p class='chart-detail'>{html_lib.escape(detail)}</p>" if detail else ""
    return (
        "<section class='panel chart-panel'>"
        f"<h2>{html_lib.escape(title)}</h2>{chart}"
        f"<p class='chart-diagnosis'><strong>해석:</strong> {html_lib.escape(diagnosis)}</p>"
        f"{detail_html}</section>"
    )


def render_training_dashboard(
    output_dir: Path,
    episode_rows: list[dict[str, Any]],
    iteration_rows: list[dict[str, Any]],
    wave_rows: list[dict[str, Any]],
    summary: dict[str, Any],
) -> Path:
    training_episode_rows = [
        row
        for row in episode_rows
        if str(row.get("phase", "")) == "initial_random"
        or str(row.get("phase", "")).startswith("policy_iteration_")
    ]
    completed_waves = [row for row in wave_rows if row.get("status") == "completed"]
    training_iterations = sorted({int(row.get("iteration", 0)) for row in training_episode_rows})
    explicit_wait_metrics = any(
        "candidate_available_wait_ratio" in row for row in episode_rows
    )
    worker_wait_ratio_by_iteration: list[float] = []
    no_candidate_ratio_by_iteration: list[float] = []
    joint_all_wait_ratio_by_iteration: list[float] = []
    max_consecutive_all_wait_by_iteration: list[float] = []
    worker_wait_key = (
        "candidate_available_wait_ratio" if explicit_wait_metrics else "worker_wait_ratio"
    )
    joint_wait_key = (
        "joint_all_wait_with_candidate_ratio"
        if explicit_wait_metrics
        else "joint_all_wait_ratio"
    )
    consecutive_wait_key = (
        "max_consecutive_candidate_all_wait_decisions"
        if explicit_wait_metrics
        else "max_consecutive_all_wait_decisions"
    )
    for iteration in training_iterations:
        rows = [row for row in training_episode_rows if int(row.get("iteration", 0)) == iteration]
        worker_wait_ratio_by_iteration.append(
            statistics.fmean(float(row.get(worker_wait_key, 0.0)) for row in rows) if rows else 0.0
        )
        no_candidate_ratio_by_iteration.append(
            statistics.fmean(
                float(row.get("no_candidate_unassigned_ratio", 0.0)) for row in rows
            )
            if explicit_wait_metrics and rows
            else 0.0
        )
        joint_all_wait_ratio_by_iteration.append(
            statistics.fmean(float(row.get(joint_wait_key, 0.0)) for row in rows) if rows else 0.0
        )
        max_consecutive_all_wait_by_iteration.append(
            max((float(row.get(consecutive_wait_key, 0.0)) for row in rows), default=0.0)
        )
    rollout_source_checkpoint_x = [float(iteration - 1) for iteration in training_iterations]
    checkpoint_wait_rows = [
        row
        for row in episode_rows
        if str(row.get("phase", "")) in {"checkpoint_validation", "screening_validation"}
    ]
    checkpoint_wait_iterations = sorted(
        {int(row.get("iteration", 0)) for row in checkpoint_wait_rows}
    )
    checkpoint_worker_wait_ratio: list[float] = []
    checkpoint_no_candidate_ratio: list[float] = []
    checkpoint_joint_wait_ratio: list[float] = []
    for iteration in checkpoint_wait_iterations:
        rows = [
            row
            for row in checkpoint_wait_rows
            if int(row.get("iteration", 0)) == iteration
        ]
        checkpoint_worker_wait_ratio.append(
            statistics.fmean(float(row.get(worker_wait_key, 0.0)) for row in rows)
            if rows
            else 0.0
        )
        checkpoint_no_candidate_ratio.append(
            statistics.fmean(
                float(row.get("no_candidate_unassigned_ratio", 0.0)) for row in rows
            )
            if explicit_wait_metrics and rows
            else 0.0
        )
        checkpoint_joint_wait_ratio.append(
            statistics.fmean(float(row.get(joint_wait_key, 0.0)) for row in rows)
            if rows
            else 0.0
        )
    exploratory_checkpoint_mean: list[float] = []
    exploratory_checkpoint_std: list[float] = []
    for rollout_iteration in sorted(iteration for iteration in training_iterations if iteration > 0):
        rows = [
            row
            for row in training_episode_rows
            if int(row.get("iteration", 0)) == rollout_iteration
        ]
        mean, std = _mean_and_std([float(row.get("products", 0)) for row in rows])
        exploratory_checkpoint_mean.append(mean)
        exploratory_checkpoint_std.append(std)
    random_rows = [row for row in training_episode_rows if row.get("phase") == "initial_random"]
    random_baseline_mean, random_baseline_std = _mean_and_std(
        [float(row.get("products", 0)) for row in random_rows]
    )
    fit_diagnostics_available = bool(summary.get("fit_diagnostics_available", True))
    update_timing_available = bool(summary.get("update_timing_available", True))
    gpu_memory_diagnostics_available = bool(
        summary.get("gpu_memory_diagnostics_available", True)
    )
    process_memory_diagnostics_available = bool(
        summary.get("process_memory_diagnostics_available", True)
    )
    wall_clock_complete = bool(summary.get("wall_clock_complete", True))
    wait_action_enabled = bool(summary.get("wait_action_enabled", False))
    losses = (
        [float(row.get("mc_loss", 0)) for row in iteration_rows]
        if fit_diagnostics_available
        else []
    )
    mae_values = (
        [float(row.get("mc_mae", 0)) for row in iteration_rows]
        if fit_diagnostics_available
        else []
    )
    rmse_values = (
        [float(row.get("mc_rmse", 0)) for row in iteration_rows]
        if fit_diagnostics_available
        else []
    )
    prediction_std_values = (
        [float(row.get("prediction_std", 0)) for row in iteration_rows]
        if fit_diagnostics_available
        else []
    )
    target_std_values = (
        [float(row.get("target_std", 0)) for row in iteration_rows]
        if fit_diagnostics_available
        else []
    )
    action_rank_correlation_values = [
        float(row.get("action_rank_correlation", 0)) for row in iteration_rows
    ]
    candidate_top1_agreement_values = [
        float(row.get("candidate_top1_agreement", 0)) for row in iteration_rows
    ]
    selected_action_regret_values = [
        float(row.get("selected_action_regret", 0)) for row in iteration_rows
    ]
    ood_rows = [
        row
        for row in iteration_rows
        if str(row.get("ood_diagnostics_available", False)).strip().lower() == "true"
    ]
    ood_x = [float(row.get("iteration", 0)) - 1.0 for row in ood_rows]
    ood_selection_rate_values = [
        100.0 * float(row.get("ood_selection_rate", 0.0)) for row in ood_rows
    ]
    ood_excess_rows = [
        row for row in ood_rows if str(row.get("ood_overestimation_excess", "")).strip()
    ]
    ood_excess_x = [float(row.get("iteration", 0)) - 1.0 for row in ood_excess_rows]
    ood_overestimation_values = [
        float(row.get("ood_overestimation_excess", 0.0)) for row in ood_excess_rows
    ]
    epsilon_values = [float(row.get("epsilon", 0)) for row in iteration_rows]
    iteration_x = [float(row.get("iteration", index)) for index, row in enumerate(iteration_rows)]
    validation_rows = [row for row in iteration_rows if bool(row.get("validation_performed", False))]
    validation = [float(row.get("validation_products_avg", 0)) for row in validation_rows]
    validation_x = [float(row.get("iteration", index)) for index, row in enumerate(validation_rows)]
    validation_ci_low = [float(row.get("validation_products_ci95_low", 0)) for row in validation_rows]
    validation_ci_high = [float(row.get("validation_products_ci95_high", 0)) for row in validation_rows]
    train_eval_rows = [row for row in iteration_rows if bool(row.get("train_eval_performed", False))]
    train_eval = [float(row.get("train_eval_products_avg", 0)) for row in train_eval_rows]
    train_eval_x = [float(row.get("iteration", index)) for index, row in enumerate(train_eval_rows)]
    train_eval_ci_low = [float(row.get("train_eval_products_ci95_low", 0)) for row in train_eval_rows]
    train_eval_ci_high = [float(row.get("train_eval_products_ci95_high", 0)) for row in train_eval_rows]
    compact_memory = [float(row.get("compact_batch_mib", 0)) for row in iteration_rows]
    process_memory = (
        [float(row.get("process_rss_mib", 0)) for row in iteration_rows]
        if process_memory_diagnostics_available
        else []
    )
    rollout_times = [float(row.get("rollout_wall_sec", 0)) for row in iteration_rows]
    update_times = (
        [float(row.get("mc_update_sec", 0)) for row in iteration_rows]
        if update_timing_available
        else []
    )
    gpu_peak_memory = (
        [float(row.get("gpu_peak_allocated_mib", 0)) for row in iteration_rows]
        if gpu_memory_diagnostics_available
        else []
    )
    wave_times = [float(row.get("wall_sec", 0)) for row in completed_waves]
    wave_rates = [float(row.get("episodes_per_hour", 0)) for row in completed_waves]
    wave_x = [float(index) for index in range(1, len(completed_waves) + 1)]
    worker_counts = sorted({int(row.get("worker_count", 0)) for row in episode_rows if int(row.get("worker_count", 0)) > 0})
    latest_validation = validation_rows[-1] if validation_rows else {}
    selection_path = output_dir / "checkpoint_selection.csv"
    selection_rows: list[dict[str, str]] = []
    if selection_path.exists():
        with selection_path.open("r", newline="", encoding="utf-8-sig") as handle:
            selection_rows = list(csv.DictReader(handle))
    worker_rows = []
    for worker_count in worker_counts:
        training_values = [
            float(row.get("products", 0))
            for row in training_episode_rows
            if int(row.get("worker_count", 0)) == worker_count
        ]
        elapsed_values = [
            float(row.get("elapsed_sec", 0))
            for row in episode_rows
            if int(row.get("worker_count", 0)) == worker_count
        ]
        worker_rows.append(
            "<tr>"
            f"<td>{worker_count}</td><td>{statistics.fmean(training_values) if training_values else 0.0:.3f}</td>"
            f"<td>{float(latest_validation.get(f'validation_products_avg_workers_{worker_count}', 0.0)):.3f}</td>"
            f"<td>{statistics.fmean(elapsed_values) if elapsed_values else 0.0:.2f}s</td>"
            "</tr>"
        )
    worker_table = (
        "<table><thead><tr><th>Workers</th><th>Training products avg</th>"
        "<th>Latest validation avg</th><th>Episode avg</th></tr></thead><tbody>"
        + "".join(worker_rows)
        + "</tbody></table>"
    )
    phase_waves = summary.get("phase_wave_counts", {})
    checkpoint_diagnostic_waves = int(
        phase_waves.get(
            "checkpoint_diagnostic",
            phase_waves.get("screening_validation", phase_waves.get("validation", 0)),
        )
    )
    device_meta = summary.get("training_device_metadata", {})
    environment_meta = summary.get("runtime_environment", {})
    loss_label = "MSE"
    latest_ood_row = ood_rows[-1] if ood_rows else {}
    cards = "".join(
        f"<div class='card'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in [
            ("Training Episodes", summary.get("training_episode_count", 0)),
            ("Validation Episodes", summary.get("validation_episode_count", 0)),
            ("Train-Eval Episodes", summary.get("train_evaluation_episode_count", 0)),
            ("Best Validation Products", f"{float(summary.get('best_validation_completed_products_avg', 0)):.3f}"),
            ("Best Iteration", summary.get("best_iteration", "-")),
            ("Policy Updates", summary.get("policy_iterations", "-")),
            ("Episodes / Update", summary.get("episodes_per_iteration", "-")),
            ("MC Loss", loss_label),
            ("Reward", "Completed-product MC"),
            ("WAIT Action", "Enabled" if wait_action_enabled else "Disabled"),
            ("Learning Rate", f"{float(summary.get('learning_rate', 0)):.1e}"),
            ("Max Epochs / Update", summary.get("max_epochs_per_iteration", "-")),
            (
                "Latest OOD Selection Rate",
                f"{100.0 * float(latest_ood_row.get('ood_selection_rate', 0.0)):.2f}%"
                if latest_ood_row
                else "N/A",
            ),
            (
                "Latest OOD Overestimation Excess",
                f"{float(latest_ood_row.get('ood_overestimation_excess')):.3f} products"
                if str(latest_ood_row.get("ood_overestimation_excess", "")).strip()
                else "N/A",
            ),
            (
                "Epsilon",
                f"{float(summary.get('epsilon_start', 0)):.2f} -> "
                f"{float(summary.get('epsilon_end', 0)):.2f}",
            ),
            ("Random Baseline", f"{random_baseline_mean:.3f} +/- {random_baseline_std:.3f}"),
            (
                "Max Consecutive All-WAIT",
                f"{max(max_consecutive_all_wait_by_iteration, default=0.0):.0f}",
            ),
            ("Peak Compact Batch", f"{float(summary.get('peak_compact_batch_mib', 0)):.2f} MiB"),
            (
                "Parent Peak RSS",
                f"{float(summary.get('observed_peak_process_rss_mib', 0)):.1f} MiB"
                if process_memory_diagnostics_available
                else "N/A (recovered run)",
            ),
            ("Child Peak RSS", f"{float(summary.get('peak_child_rss_mib', 0)):.1f} MiB"),
            ("Effective Speedup", f"{float(summary.get('effective_speedup', 0)):.2f}x"),
            ("Parallel Efficiency", f"{100.0 * float(summary.get('parallel_efficiency', 0)):.1f}%"),
            ("Episodes / Hour", f"{float(summary.get('episodes_per_hour', 0)):.1f}"),
            (
                "Total Wall Time" if wall_clock_complete else "Observed Simulation Wall",
                f"{float(summary.get('total_wall_clock_sec', 0)) / 3600.0:.2f} h",
            ),
            ("Training Device", device_meta.get("resolved_device", summary.get("training_device", "-"))),
            ("GPU", device_meta.get("gpu_name", "-")),
            (
                "GPU Peak Allocated",
                f"{float(summary.get('gpu_peak_allocated_mib', 0)):.1f} MiB"
                if gpu_memory_diagnostics_available
                else "N/A (recovered run)",
            ),
            ("Checkpoint", summary.get("best_checkpoint", "-")),
        ]
    )
    def screening_cell(row: dict[str, Any]) -> str:
        if not bool(row.get("validation_performed", False)):
            return "-"
        return (
            f"{float(row.get('validation_products_avg', 0)):.3f} "
            f"[{float(row.get('validation_products_ci95_low', 0)):.3f}, "
            f"{float(row.get('validation_products_ci95_high', 0)):.3f}]"
        )

    def train_eval_cell(row: dict[str, Any]) -> str:
        if not bool(row.get("train_eval_performed", False)):
            return "-"
        return (
            f"{float(row.get('train_eval_products_avg', 0)):.3f} "
            f"[{float(row.get('train_eval_products_ci95_low', 0)):.3f}, "
            f"{float(row.get('train_eval_products_ci95_high', 0)):.3f}]"
        )

    def fit_cell(row: dict[str, Any], key: str) -> str:
        return f"{float(row.get(key, 0)):.3f}" if fit_diagnostics_available else "n/a"

    def update_time_cell(row: dict[str, Any]) -> str:
        return (
            f"{float(row.get('mc_update_sec', 0)):.2f}s"
            if update_timing_available
            else "n/a"
        )

    def ood_cell(row: dict[str, Any], key: str, *, percent: bool = False) -> str:
        value = str(row.get(key, "")).strip()
        if not value:
            return "-"
        numeric = float(value)
        return f"{100.0 * numeric:.2f}%" if percent else f"{numeric:.3f}"

    iteration_table_rows = "".join(
        "<tr>"
        f"<td>{int(row.get('iteration', 0))}</td>"
        f"<td>{int(row.get('batch_episode_count', 0))}</td>"
        f"<td>{int(row.get('sample_count', 0))}</td>"
        f"<td>{float(row.get('compact_batch_mib', 0)):.3f}</td>"
        f"<td>{float(row.get('rollout_wall_sec', 0)):.2f}s</td>"
        f"<td>{update_time_cell(row)}</td>"
        f"<td>{fit_cell(row, 'mc_mae')}</td>"
        f"<td>{fit_cell(row, 'mc_rmse')}</td>"
        f"<td>{float(row.get('epsilon', 0)):.3f}</td>"
        f"<td>{ood_cell(row, 'ood_selection_rate', percent=True)}</td>"
        f"<td>{ood_cell(row, 'ood_overestimation_excess')}</td>"
        f"<td>{train_eval_cell(row)}</td>"
        f"<td>{screening_cell(row)}</td>"
        "</tr>"
        for row in iteration_rows
    )
    iteration_table = (
        "<table><thead><tr><th>Update</th><th>Episodes</th><th>MC samples</th>"
        "<th>Compact MiB</th><th>Rollout</th><th>GPU update</th><th>MAE</th><th>RMSE</th><th>Epsilon</th>"
        "<th>OOD 선택률</th><th>OOD 추가 과대평가</th>"
        "<th>Train-eval mean [95% CI]</th><th>Held-out validation mean [95% CI]</th></tr></thead><tbody>"
        + iteration_table_rows
        + "</tbody></table>"
    )
    parallel_config_table = (
        "<table><tbody>"
        f"<tr><th>Configured / actual processes</th><td>{int(summary.get('configured_process_count', 0))} / {int(summary.get('actual_process_count_max', 0))}</td></tr>"
        f"<tr><th>Wave size</th><td>{int(summary.get('wave_size', 0))}</td></tr>"
        f"<tr><th>Start method</th><td>{html_lib.escape(str(summary.get('multiprocessing_start_method', '-')))}</td></tr>"
        f"<tr><th>Rollout / training device</th><td>{html_lib.escape(str(summary.get('rollout_device', '-')))} / {html_lib.escape(str(summary.get('training_device', '-')))}</td></tr>"
        f"<tr><th>CUDA required</th><td>{'yes' if bool(summary.get('require_cuda_training', False)) else 'no'}</td></tr>"
        f"<tr><th>GPU index / name</th><td>{html_lib.escape(str(device_meta.get('gpu_index', '-')))} / {html_lib.escape(str(device_meta.get('gpu_name', '-')))}</td></tr>"
        f"<tr><th>GPU memory</th><td>{float(device_meta.get('gpu_total_memory_mib', 0)):.1f} MiB</td></tr>"
        f"<tr><th>PyTorch / CUDA / cuDNN</th><td>{html_lib.escape(str(device_meta.get('torch_version', '-')))} / {html_lib.escape(str(device_meta.get('cuda_runtime_version', '-')))} / {html_lib.escape(str(device_meta.get('cudnn_version', '-')))}</td></tr>"
        f"<tr><th>GPU count / compute capability</th><td>{int(device_meta.get('gpu_count', 0))} / {html_lib.escape(str(device_meta.get('gpu_compute_capability', '-')))}</td></tr>"
        f"<tr><th>CPU / logical processors</th><td>{html_lib.escape(str(environment_meta.get('cpu_model', '-')))} / {int(environment_meta.get('logical_cpu_count', 0))}</td></tr>"
        f"<tr><th>Host / OS</th><td>{html_lib.escape(str(environment_meta.get('host_name', '-')))} / {html_lib.escape(str(environment_meta.get('operating_system', '-')))}</td></tr>"
        f"<tr><th>Python</th><td>{html_lib.escape(str(environment_meta.get('python_version', '-')))}</td></tr>"
        f"<tr><th>Torch threads per process</th><td>{int(summary.get('torch_threads_per_process', 0))}</td></tr>"
        f"<tr><th>Initial / policy / checkpoint diagnostic / final waves</th><td>{int(phase_waves.get('initial_random', 0))} / {int(phase_waves.get('policy_iteration', 0))} / {checkpoint_diagnostic_waves} / {int(phase_waves.get('final_selection_validation', 0))}</td></tr>"
        f"<tr><th>Total waves</th><td>{int(summary.get('total_wave_count', 0))}</td></tr>"
        f"<tr><th>Active-slot utilization</th><td>{100.0 * float(summary.get('active_slot_utilization', 0)):.1f}%</td></tr>"
        f"<tr><th>IPC payload</th><td>{float(summary.get('ipc_payload_mib', 0)):.2f} MiB</td></tr>"
        f"<tr><th>Failed / cancelled / retried</th><td>{int(summary.get('failed_wave_count', 0))} / {int(summary.get('cancelled_episode_count', 0))} / {int(summary.get('retried_episode_count', 0))}</td></tr>"
        "</tbody></table>"
    )
    time_parts = [
        ("고정 행동 probe", float(summary.get("fixed_action_probe_wall_sec", 0)), "#4dc6c6"),
        ("OOD 진단", float(summary.get("ood_diagnostic_sec", 0)), "#ff8b72"),
        ("Training rollout", float(summary.get("training_rollout_sec", 0)), "#43a5ff"),
        ("Validation", float(summary.get("validation_wall_sec", 0)), "#d78cff"),
        ("Finalization", float(summary.get("checkpoint_dashboard_write_sec", 0)), "#f5b85b"),
    ]
    if update_timing_available:
        time_parts.insert(2, ("GPU update", float(summary.get("mc_update_sec", 0)), "#72d69b"))
    known_time = sum(value for _, value, _ in time_parts)
    total_time = max(float(summary.get("total_wall_clock_sec", 0)), known_time, 1e-9)
    other_time = max(0.0, total_time - known_time)
    time_parts.append(("Other", other_time, "#6f7f93"))
    time_bar = "<div class='stack'>" + "".join(
        f"<div style='width:{100.0 * value / total_time:.3f}%;background:{color}' title='{html_lib.escape(label)}: {value:.2f}s'></div>"
        for label, value, color in time_parts
        if value > 0
    ) + "</div><div class='legend'>" + "".join(
        f"<span><i style='background:{color}'></i>{html_lib.escape(label)} {value:.1f}s</span>"
        for label, value, color in time_parts
    ) + "</div>"
    slow_episodes = sorted(episode_rows, key=lambda row: float(row.get("elapsed_sec", 0)), reverse=True)[:10]
    slow_episode_table = (
        "<table><thead><tr><th>Episode</th><th>Phase</th><th>Workers</th><th>Seed</th><th>Elapsed</th><th>Child RSS</th></tr></thead><tbody>"
        + "".join(
            "<tr>"
            f"<td>{int(row.get('episode', 0))}</td><td>{html_lib.escape(str(row.get('phase', '')))}</td>"
            f"<td>{int(row.get('worker_count', 0))}</td><td>{int(row.get('seed', 0))}</td>"
            f"<td>{float(row.get('elapsed_sec', 0)):.2f}s</td><td>{float(row.get('child_peak_rss_mib', 0)):.1f} MiB</td>"
            "</tr>"
            for row in slow_episodes
        )
        + "</tbody></table>"
    )
    slow_waves = sorted(completed_waves, key=lambda row: float(row.get("wall_sec", 0)), reverse=True)[:10]
    slow_wave_table = (
        "<table><thead><tr><th>Wave</th><th>Phase</th><th>Episodes</th><th>Wall</th><th>Speedup</th><th>Efficiency</th></tr></thead><tbody>"
        + "".join(
            "<tr>"
            f"<td>{html_lib.escape(str(row.get('wave_id', '')))}</td><td>{html_lib.escape(str(row.get('phase', '')))}</td>"
            f"<td>{int(row.get('episode_count', 0))}</td><td>{float(row.get('wall_sec', 0)):.2f}s</td>"
            f"<td>{float(row.get('effective_speedup', 0)):.2f}x</td><td>{100.0 * float(row.get('parallel_efficiency', 0)):.1f}%</td>"
            "</tr>"
            for row in slow_waves
        )
        + "</tbody></table>"
    )
    failed_waves = [row for row in wave_rows if row.get("status") == "failed"]
    failed_wave_table = (
        "<div class='empty'>No failed waves</div>"
        if not failed_waves
        else (
            "<table><thead><tr><th>Wave</th><th>Phase</th><th>Completed</th><th>Cancelled</th><th>Error</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td>{html_lib.escape(str(row.get('wave_id', '')))}</td>"
                f"<td>{html_lib.escape(str(row.get('phase', '')))}</td>"
                f"<td>{int(row.get('completed_episode_count', 0))}</td>"
                f"<td>{int(row.get('cancelled_episode_count', 0))}</td>"
                f"<td>{html_lib.escape(str(row.get('error', '')))}</td>"
                "</tr>"
                for row in failed_waves
            )
            + "</tbody></table>"
        )
    )
    checkpoint_table = (
        "<div class='empty'>No final checkpoint selection data</div>"
        if not selection_rows
        else (
            "<table><thead><tr><th>Iteration</th><th>Screen rank</th><th>Train-eval mean +/- std</th><th>Screen mean +/- std</th>"
            "<th>Final mean +/- std</th><th>Final 95% CI</th><th>Selected</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td>{int(float(row.get('iteration', 0)))}</td>"
                f"<td>{int(float(row.get('screening_rank', 0)))}</td>"
                f"<td>{float(row.get('train_eval_mean_products', 0) or 0):.3f} +/- {float(row.get('train_eval_std_products', 0) or 0):.3f}</td>"
                f"<td>{float(row.get('screening_mean_products', 0)):.3f} +/- {float(row.get('screening_std_products', 0)):.3f}</td>"
                f"<td>{float(row.get('final_mean_products', 0)):.3f} +/- {float(row.get('final_std_products', 0)):.3f}</td>"
                f"<td>[{float(row.get('final_ci95_low', 0)):.3f}, {float(row.get('final_ci95_high', 0)):.3f}]</td>"
                f"<td>{html_lib.escape(str(row.get('selected_best', 'False')))}</td>"
                "</tr>"
                for row in sorted(selection_rows, key=lambda item: int(float(item.get("screening_rank", 0))))
            )
            + "</tbody></table>"
        )
    )
    seed_meta = summary.get("seed_partitions", {}) if isinstance(summary.get("seed_partitions", {}), dict) else {}
    seed_table = "<table><thead><tr><th>Partition</th><th>Count</th><th>Range</th><th>Hash</th></tr></thead><tbody>" + "".join(
        f"<tr><td>{html_lib.escape(name)}</td><td>{int(values.get('count', 0))}</td>"
        f"<td>{html_lib.escape(str(values.get('min', '-')))}..{html_lib.escape(str(values.get('max', '-')))}</td>"
        f"<td><code>{html_lib.escape(str(values.get('hash', '-')))}</code></td></tr>"
        for name, values in seed_meta.items()
        if isinstance(values, dict) and "count" in values
    ) + "</tbody></table>"
    checkpoint_x = train_eval_x or validation_x
    checkpoint_series: list[tuple[str, list[float], str]] = []
    checkpoint_error_ranges: dict[str, tuple[list[float], list[float]]] = {}
    if train_eval:
        checkpoint_series.append(("Train-eval (epsilon=0)", train_eval, "#43a5ff"))
        checkpoint_error_ranges["Train-eval (epsilon=0)"] = (
            train_eval_ci_low,
            train_eval_ci_high,
        )
    if validation:
        checkpoint_series.append(("Held-out validation (epsilon=0)", validation, "#d78cff"))
        checkpoint_error_ranges["Held-out validation (epsilon=0)"] = (
            validation_ci_low,
            validation_ci_high,
        )
    if train_eval and exploratory_checkpoint_mean:
        checkpoint_series.append(
            ("Exploratory rollout for next update", exploratory_checkpoint_mean, "#91a3b8")
        )
    wait_metric_name = (
        "후보가 있는 Worker의 미배정 비율"
        if explicit_wait_metrics
        else "Worker 미배정 비율(legacy)"
    )
    no_candidate_metric_name = "후보 없음에 따른 Worker 미배정 비율"
    joint_wait_metric_name = (
        "후보가 있는데 전원 WAIT한 비율"
        if explicit_wait_metrics
        else "전원 미배정 비율(legacy)"
    )
    wait_metric_detail = (
        "실행 가능한 후보가 있는데 WAIT를 고른 경우와 후보가 없어 미배정된 경우를 분리한 지표입니다."
        if explicit_wait_metrics
        else "이 학습 결과는 이전 집계 계약으로 생성되어 명시적 WAIT와 후보 없음에 따른 미배정이 합산되어 있습니다."
    )
    wait_panel = (
        _chart_panel(
            "업데이트 입력 Rollout의 WAIT/미배정 행동",
            _svg_chart(
                [
                    (wait_metric_name, worker_wait_ratio_by_iteration, "#72d69b"),
                    *(
                        [
                            (
                                no_candidate_metric_name,
                                no_candidate_ratio_by_iteration,
                                "#43a5ff",
                            )
                        ]
                        if explicit_wait_metrics
                        else []
                    ),
                    (joint_wait_metric_name, joint_all_wait_ratio_by_iteration, "#ff8b72"),
                ],
                x_values=rollout_source_checkpoint_x,
                x_label="Rollout 생성 checkpoint (-1=초기 무작위)",
                y_label="WAIT 또는 미배정 비율",
                include_zero=True,
            ),
            "x=k의 값은 checkpoint k가 다음 업데이트 k+1에 사용할 MC 표본을 생성할 때의 행동입니다. 따라서 이 그래프를 업데이트 후 checkpoint k+1의 생산량과 직접 비교하면 안 됩니다.",
            wait_metric_detail,
        )
        + (
            _chart_panel(
                "업데이트 직후 Checkpoint Validation의 WAIT/미배정 행동",
                _svg_chart(
                    [
                        (wait_metric_name, checkpoint_worker_wait_ratio, "#72d69b"),
                        *(
                            [
                                (
                                    no_candidate_metric_name,
                                    checkpoint_no_candidate_ratio,
                                    "#43a5ff",
                                )
                            ]
                            if explicit_wait_metrics
                            else []
                        ),
                        (joint_wait_metric_name, checkpoint_joint_wait_ratio, "#ff8b72"),
                    ],
                    x_values=[float(value) for value in checkpoint_wait_iterations],
                    x_label="업데이트 직후 checkpoint",
                    y_label="WAIT 또는 미배정 비율",
                    include_zero=True,
                ),
                "생산량 validation과 동일한 epsilon=0 checkpoint 실행에서 측정한 값입니다. 특정 checkpoint의 생산량 붕괴와 WAIT 행동을 판단할 때 이 그래프를 사용해야 합니다.",
                wait_metric_detail,
            )
            if checkpoint_wait_iterations
            else ""
        )
        if wait_action_enabled
        else _chart_panel(
            "WAIT 행동 비활성화",
            "<div class='empty'>WAIT is not included in the ADP or Random Feasible action set.</div>",
            "충돌 없이 수행할 태스크가 없으면 worker가 미할당 상태로 남을 수 있지만, 이는 명시적 WAIT 선택으로 집계하지 않습니다.",
        )
    )
    checkpoint_detail = (
        "실선은 동일한 고정 checkpoint와 epsilon=0을 사용하며 오차 막대는 95% 신뢰구간입니다. "
        "점선은 탐색이 포함된 다음 업데이트용 MC rollout입니다. "
        f"초기 무작위 정책 기준은 {random_baseline_mean:.3f} +/- {random_baseline_std:.3f}개입니다."
    )
    chart_grid = "".join(
        [
            "<div class='chart-section-title'><h2>1. 정책 성능과 미관측 행동 진단</h2>"
            "<p>업데이트 후 생산 성능과, 직전 on-policy 학습 범위를 벗어난 greedy 행동 선택을 먼저 확인합니다.</p></div>",
            _chart_panel(
                "업데이트 후 Checkpoint 생산량",
                _svg_chart(
                    checkpoint_series,
                    x_values=checkpoint_x,
                    x_label="가치함수 업데이트 후 checkpoint",
                    y_label="5일 episode 완료 제품 수",
                    include_zero=True,
                    error_ranges=checkpoint_error_ranges,
                    dashed_series={"Exploratory rollout for next update"},
                ),
                "같은 checkpoint의 train-eval과 미사용 validation 생산량을 비교합니다. 두 곡선의 간격이 커지면 과적합, 함께 급락하면 정책 불안정 가능성이 큽니다.",
                checkpoint_detail,
            ),
            _chart_panel(
                "미관측 행동 선택률",
                _svg_chart(
                    [("OOD 선택률", ood_selection_rate_values, "#ff8b72")],
                    x_values=ood_x,
                    x_label="Rollout 생성 checkpoint",
                    y_label="Greedy 선택 중 OOD 비율(%)",
                    include_zero=True,
                ),
                "가치망이 greedy로 선택한 행동 중 직전 on-policy 학습 batch의 95% support 범위를 벗어난 비율입니다. 값이 급증하면 정책이 학습 근거가 약한 행동 영역으로 이동했다는 뜻입니다.",
                "탐색으로 무작위 선택된 행동과 mandatory battery 행동은 제외합니다. 초기 무작위 batch에는 이전 support가 없어 값이 없습니다.",
            ),
            _chart_panel(
                "미관측 행동 추가 과대평가량",
                _svg_chart(
                    [("OOD 추가 과대평가", ood_overestimation_values, "#d78cff")],
                    x_values=ood_excess_x,
                    x_label="Rollout 생성 checkpoint",
                    y_label="추가 예측오차(완료 제품 수)",
                    include_zero=True,
                ),
                "OOD 행동의 평균 가치 예측오차에서 관측 범위 내 행동의 평균 예측오차를 뺀 값입니다. 양수이면 미관측 행동을 상대적으로 더 낙관적으로 평가했다는 뜻입니다.",
                "미관측 행동 선택률과 이 값이 함께 상승하면서 validation 생산량이 하락하면 미관측 행동 과대평가가 정책 저하 원인이라는 근거가 됩니다.",
            ),
            "<div class='chart-section-title'><h2>2. 행동 선택 품질</h2>"
            "<p>제한된 반사실 후보집합의 순위 품질과 실제 WAIT 선택을 확인합니다.</p></div>",
            _chart_panel(
                "후보 행동 가치 순위 상관계수",
                _svg_chart(
                    [("Spearman 상관계수", action_rank_correlation_values, "#72d69b")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="평균 순위 상관계수",
                    include_zero=True,
                ),
                "예측한 행동 순위와 반사실 MC 성과 순위의 일치도를 나타냅니다. 1에 가까울수록 좋고, 0은 순위 관계가 약하며, 음수는 반대 방향으로 평가한다는 뜻입니다.",
                "후보별 실제 return이 모두 같은 비정보 상태는 이 평균에서 제외합니다.",
            ),
            _chart_panel(
                "후보집합 최선 행동 Top-1 일치율",
                _svg_chart(
                    [("Top-1 일치율", candidate_top1_agreement_values, "#43a5ff")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="Top-1 일치율",
                    include_zero=True,
                ),
                "가치망이 가장 높게 평가한 행동이 probe 후보집합에서 가장 큰 경험적 return을 얻었는지 측정합니다. 1에 가까울수록 좋습니다.",
                "수학적 전역 최적행동이 아니라, 대시보드에 기록된 제한된 후보집합 안의 경험적 최선입니다.",
            ),
            _chart_panel(
                "선택 행동 Regret",
                _svg_chart(
                    [("평균 regret", selected_action_regret_values, "#ff8b72")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="완료 제품 수 차이",
                    include_zero=True,
                ),
                "가치망이 고른 행동의 경험적 return과 후보집합 최선 return의 차이입니다. 0에 가까울수록 좋으며, 값이 커지면 행동 선택의 운영 손실이 커졌다는 뜻입니다.",
            ),
            wait_panel,
            "<div class='chart-section-title'><h2>3. 가치함수 학습 상태</h2>"
            "<p>현재 on-policy MC batch에 대한 적합도와 return 분산을 확인합니다. 낮은 오차만으로 좋은 행동 순위를 보장하지는 않습니다.</p></div>",
            _chart_panel(
                (
                    f"Post-Decision 가치함수 Holdout {loss_label}"
                    if fit_diagnostics_available
                    else f"Post-Decision Value Holdout {loss_label} (Unavailable After Recovery)"
                ),
                _svg_chart(
                    [(f"{loss_label} loss", losses, "#f5b85b")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label=f"Episode 분할 holdout {loss_label}",
                    include_zero=True,
                ),
                (
                    "현재 on-policy batch의 episode 단위 holdout에서 early stopping용 적합 오차를 측정합니다. 아래 MAE/RMSE와 표본 범위가 달라 제곱값이 정확히 일치하지 않으며, 낮은 loss만으로 dispatch 정책 개선을 증명할 수는 없습니다."
                    if fit_diagnostics_available
                    else "중단된 실행에서 업데이트별 loss가 보존되지 않았습니다. 누락값을 0으로 해석하면 안 됩니다."
                ),
            ),
            _chart_panel(
                (
                    "Monte Carlo 가치 예측 오차"
                    if fit_diagnostics_available
                    else "Monte Carlo Value Prediction Error (Unavailable After Recovery)"
                ),
                _svg_chart(
                    [("MAE", mae_values, "#ff8b72"), ("RMSE", rmse_values, "#f5b85b")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="Return 예측 오차",
                    include_zero=True,
                ),
                (
                    "MAE와 RMSE는 감소하거나 안정되어야 합니다. RMSE와 MAE 간격이 커지면 일부 상태에서 큰 예측오차가 발생한다는 뜻입니다."
                    if fit_diagnostics_available
                    else "중단된 실행에서 MAE/RMSE가 보존되지 않았습니다. 사용할 수 없는 값이지 0이 아닙니다."
                ),
            ),
            _chart_panel(
                (
                    "예측 Return과 목표 Return의 분산 비교"
                    if fit_diagnostics_available
                    else "Predicted vs Target Return Dispersion (Unavailable After Recovery)"
                ),
                _svg_chart(
                    [
                        ("Prediction std", prediction_std_values, "#43a5ff"),
                        ("MC target std", target_std_values, "#72d69b"),
                    ],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="Return 표준편차",
                    include_zero=True,
                ),
                (
                    "예측값의 산포가 목표값의 산포를 대체로 따라야 합니다. 예측 표준편차가 0에 가까워지면 모든 행동을 비슷하게 평가하는 가치함수 붕괴를 의심해야 합니다."
                    if fit_diagnostics_available
                    else "중단된 실행에서 예측값과 MC 목표값의 산포가 보존되지 않았습니다. 누락값은 0이 아닙니다."
                ),
            ),
            _chart_panel(
                "탐색 확률 변화",
                _svg_chart(
                    [("Epsilon", epsilon_values, "#d78cff")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="Epsilon 확률",
                    include_zero=True,
                ),
                "탐색 확률이 의도대로 감소하는지 확인합니다. 너무 일찍 결정론적 정책이 되지 않으면서 validation 성능이 유지되거나 개선되어야 합니다.",
            ),
            "<div class='chart-section-title'><h2>4. 계산 성능과 메모리</h2>"
            "<p>정책 품질과 분리하여 CPU rollout, GPU update, compact batch와 process 자원 사용을 점검합니다.</p></div>",
            _chart_panel(
                "CPU Rollout Wave 소요시간",
                _svg_chart(
                    [("Wave wall time", wave_times, "#43a5ff")],
                    x_values=wave_x,
                    x_label="완료된 rollout wave",
                    y_label="실제 경과시간(초)",
                    include_zero=True,
                ),
                "느린 wave와 자원 경합을 진단합니다. 지속적인 상승은 CPU 포화, 메모리 압박 또는 episode 실행시간 증가를 뜻할 수 있습니다.",
            ),
            _chart_panel(
                "CPU Rollout 처리율",
                _svg_chart(
                    [("Episode rate", wave_rates, "#72d69b")],
                    x_values=wave_x,
                    x_label="완료된 rollout wave",
                    y_label="시간당 episode 수",
                    include_zero=True,
                ),
                "병렬 표본 생성 속도를 나타냅니다. 지속적으로 하락하면 rollout 비용 증가 또는 호스트 자원 경합을 점검해야 합니다.",
            ),
            _chart_panel(
                "정책 업데이트별 Rollout 수집시간",
                _svg_chart(
                    [("Rollout time", rollout_times, "#58c8c0")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="실제 경과시간(초)",
                    include_zero=True,
                ),
                "시뮬레이션 데이터 수집 비용과 신경망 업데이트 비용을 분리해 보여줍니다.",
            ),
            _chart_panel(
                (
                    "GPU 가치망 업데이트 시간"
                    if update_timing_available
                    else "GPU Value-Network Update Time (Unavailable After Recovery)"
                ),
                _svg_chart(
                    [("GPU update time", update_times, "#f5b85b")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="실제 경과시간(초)",
                    include_zero=True,
                ),
                (
                    "신경망 최적화 비용을 추적합니다. 갑작스러운 증가는 batch 크기, epoch 수 또는 GPU 경합 변화를 뜻할 수 있습니다."
                    if update_timing_available
                    else "GPU 업데이트는 수행되었지만 중단 전에 시간이 저장되지 않았습니다. 0초로 해석하면 안 됩니다."
                ),
            ),
            _chart_panel(
                (
                    "정책 업데이트별 GPU 최대 할당 메모리"
                    if gpu_memory_diagnostics_available
                    else "GPU Peak Allocated Memory by Policy Update (Unavailable After Recovery)"
                ),
                _svg_chart(
                    [("GPU peak", gpu_peak_memory, "#d78cff")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="할당 메모리(MiB)",
                    include_zero=True,
                ),
                (
                    "업데이트가 진행될수록 단조 증가하면 해제되지 않은 GPU tensor 또는 메모리 누수를 의심해야 합니다."
                    if gpu_memory_diagnostics_available
                    else "중단 전에 업데이트별 GPU 메모리가 저장되지 않았습니다. 누락값은 메모리 사용량 0을 뜻하지 않습니다."
                ),
            ),
            _chart_panel(
                "정책 업데이트별 Compact MC Batch 메모리",
                _svg_chart(
                    [("Compact batch", compact_memory, "#58c8c0")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="Compact batch 크기(MiB)",
                    include_zero=True,
                ),
                "의사결정 수에 따라 달라질 수 있지만 각 on-policy 업데이트가 끝난 뒤 해제되어야 합니다.",
            ),
            _chart_panel(
                (
                    "정책 업데이트별 Parent Process 메모리"
                    if process_memory_diagnostics_available
                    else "Parent Process Memory by Policy Update (Unavailable After Recovery)"
                ),
                _svg_chart(
                    [("Parent RSS", process_memory, "#ff8b72")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="상주 메모리(MiB)",
                    include_zero=True,
                ),
                (
                    "Compact batch 해제 후에도 계속 상승하면 남은 객체 참조나 cache를 점검해야 합니다."
                    if process_memory_diagnostics_available
                    else "중단 전에 업데이트별 parent RSS가 저장되지 않았습니다. 누락값은 메모리 사용량 0이 아닙니다."
                ),
            ),
        ]
    )
    recovery_banner = (
        "<section class='warning'><strong>Recovered final selection:</strong> "
        + html_lib.escape(str(summary.get("recovery_note", "")))
        + "</section>"
        if bool(summary.get("recovered_after_interruption", False))
        else ""
    )
    html = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>ManSim ADP Training</title>
<style>body{{margin:0;background:#08111f;color:#e8f1ff;font:14px Segoe UI,Arial;overflow-x:hidden}}main{{max-width:1480px;min-width:0;margin:auto;padding:28px}}h1{{letter-spacing:0;overflow-wrap:anywhere}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.card,.panel{{min-width:0;border:1px solid #294567;background:#101d31;border-radius:6px;padding:16px}}.warning{{margin:12px 0;padding:14px;border:1px solid #b88932;background:#2b2312;color:#ffe4a3;border-radius:6px;overflow-wrap:anywhere}}.card span{{display:block;color:#8fb2de}}.card strong{{font-size:20px;overflow-wrap:anywhere}}.grid{{min-width:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:14px}}.chart-section-title{{grid-column:1/-1;border-top:1px solid #294567;padding:18px 4px 0;margin-top:4px}}.chart-section-title h2{{margin:0 0 6px}}.chart-section-title p{{margin:0;color:#91add0}}.metric-chart{{width:100%;height:auto;aspect-ratio:720/250;background:#0b1728}}.grid-line{{stroke:#223752;stroke-width:1}}.axis-line{{stroke:#86a5c9;stroke-width:1.4}}.tick{{fill:#9eb5d1;font-size:11px}}.x-tick{{text-anchor:middle}}.y-tick{{text-anchor:end}}.axis-label{{fill:#d8e8fa;font-size:13px;font-weight:600}}.x-axis-label,.y-axis-label{{text-anchor:middle}}.point-label{{fill:#ffffff;font-size:12px;font-weight:700;text-anchor:middle}}.chart-legend{{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;color:#b8cae0}}.chart-legend i{{display:inline-block;width:12px;height:3px;margin:0 6px 3px 0}}.chart-diagnosis{{color:#d5e3f4;line-height:1.5;margin:12px 0 0}}.chart-detail{{color:#91add0;line-height:1.45;margin:6px 0 0}}code{{color:#77d3a8;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #294567;text-align:right}}th:first-child,td:first-child{{text-align:left}}.stack{{height:28px;display:flex;background:#0b1728;overflow:hidden;border-radius:4px}}.stack div{{min-width:2px}}.legend{{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;color:#a9bfdb}}.legend i{{display:inline-block;width:10px;height:10px;margin-right:5px}}@media(max-width:900px){{main{{padding:18px}}.grid{{grid-template-columns:1fr}}.chart-section-title{{grid-column:1}}.panel{{overflow-x:auto}}.chart-panel{{overflow-x:hidden}}table{{min-width:620px}}}}@media(max-width:600px){{main{{padding:14px}}.cards{{grid-template-columns:1fr}}.card strong{{font-size:18px}}}}</style></head><body><main>
<h1>Simulation-Based ADP 학습 대시보드</h1><p>현재 on-policy Monte Carlo batch만 학습하며, 각 업데이트가 끝나면 compact episode tensor를 폐기합니다.</p>
{recovery_banner}<div class='cards'>{cards}</div><div class='grid'>
<section class='panel'><h2>병렬 실행 설정</h2>{parallel_config_table}</section>
<section class='panel'><h2>전체 시간 구성</h2>{time_bar}<p>Compact 병합: {float(summary.get('compact_merge_sec', 0)):.2f}s · IPC/조정 overhead: {float(summary.get('coordination_ipc_overhead_sec', 0)):.2f}s</p>{"<p class='chart-detail'>복구된 실행은 원래 parent wall clock과 GPU update 시간이 없어 저장된 rollout·validation wave 시간만 표시합니다.</p>" if not wall_clock_complete else ""}</section>
{chart_grid}
</div><section class='panel' style='margin-top:14px'><h2>Checkpoint 선정 결과</h2>{checkpoint_table}</section><section class='panel' style='margin-top:14px'><h2>Seed 분할</h2>{seed_table}</section><section class='panel' style='margin-top:14px'><h2>On-Policy 업데이트 Batch</h2>{iteration_table}</section><section class='panel' style='margin-top:14px'><h2>Worker 수별 성능</h2>{worker_table}</section><div class='grid'><section class='panel'><h2>가장 느린 Episode</h2>{slow_episode_table}</section><section class='panel'><h2>가장 느린 Wave</h2>{slow_wave_table}</section></div><section class='panel' style='margin-top:14px'><h2>실패한 Wave</h2>{failed_wave_table}</section><p>Feature schema: <code>{FEATURE_SCHEMA_VERSION}</code></p></main></body></html>"""
    path = output_dir / "training_dashboard.html"
    path.write_text(html, encoding="utf-8")
    return path


def _linear(start: float, end: float, index: int, total: int) -> float:
    return float(start if total <= 1 else start + (end - start) * index / (total - 1))


def _process_rss_mib(*, peak: bool = False) -> float:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
            value = counters.PeakWorkingSetSize if peak else counters.WorkingSetSize
            return float(value) / (1024.0**2)
        return 0.0
    try:
        import resource

        usage = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return usage / (1024.0**2 if os.uname().sysname == "Darwin" else 1024.0)
    except (ImportError, AttributeError):
        return 0.0


def _episode_metric_row(result: EpisodeResult) -> dict[str, Any]:
    return {
        "episode": result.episode,
        "phase": result.phase,
        "iteration": result.iteration,
        "wave_id": result.wave_id,
        "process_slot": result.process_slot,
        "worker_count": result.worker_count,
        "seed": result.seed,
        "products": result.products,
        "scrap": result.scrap,
        "raw_return": result.raw_return,
        "decision_count": result.decisions,
        "wait_count": result.wait_count,
        "candidate_available_wait_count": result.candidate_available_wait_count,
        "no_candidate_unassigned_count": result.no_candidate_unassigned_count,
        "assigned_task_count": result.assigned_task_count,
        "worker_wait_ratio": result.wait_count / max(1, result.wait_count + result.assigned_task_count),
        "candidate_available_wait_ratio": result.candidate_available_wait_count
        / max(1, result.wait_count + result.assigned_task_count),
        "no_candidate_unassigned_ratio": result.no_candidate_unassigned_count
        / max(1, result.wait_count + result.assigned_task_count),
        "joint_all_wait_count": result.joint_all_wait_count,
        "joint_all_wait_ratio": result.joint_all_wait_count / max(1, result.decisions),
        "joint_all_wait_with_candidate_count": result.joint_all_wait_with_candidate_count,
        "joint_all_wait_with_candidate_ratio": result.joint_all_wait_with_candidate_count
        / max(1, result.decisions),
        "joint_no_candidate_count": result.joint_no_candidate_count,
        "joint_no_candidate_ratio": result.joint_no_candidate_count
        / max(1, result.decisions),
        "max_consecutive_all_wait_decisions": result.max_consecutive_all_wait_decisions,
        "max_consecutive_candidate_all_wait_decisions": (
            result.max_consecutive_candidate_all_wait_decisions
        ),
        "compact_sample_count": result.compact_sample_count,
        "compact_memory_mib": round(result.compact_memory_bytes / (1024.0**2), 6),
        "elapsed_sec": round(result.elapsed_sec, 6),
        "child_peak_rss_mib": round(result.child_peak_rss_mib, 6),
        "snapshot_hash": result.snapshot_hash,
    }


def _balanced_worker_schedule(worker_counts: list[int], episode_count: int) -> list[int]:
    if not worker_counts:
        raise ValueError("ADP training requires at least one worker count.")
    return [int(worker_counts[index % len(worker_counts)]) for index in range(max(0, int(episode_count)))]


def _should_validate(
    iteration: int,
    policy_iterations: int,
    interval: int,
    *,
    include_initial: bool = True,
) -> bool:
    return bool(
        (bool(include_initial) and int(iteration) == 0)
        or int(iteration) == int(policy_iterations)
        or (
            int(iteration) > 0
            and int(iteration) % max(1, int(interval)) == 0
        )
    )


def _seed_digest(values: list[int]) -> str:
    payload = ",".join(str(int(value)) for value in values)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:16]


def _validate_seed_partitions(
    *,
    training_seeds: list[int],
    screening_seeds: list[int],
    final_selection_seeds: list[int],
    held_out_seeds: list[int],
    enforce_disjoint: bool,
) -> dict[str, Any]:
    partitions = {
        "training": [int(value) for value in training_seeds],
        "screening_validation": [int(value) for value in screening_seeds],
        "final_selection_validation": [int(value) for value in final_selection_seeds],
        "held_out_test": [int(value) for value in held_out_seeds],
    }
    duplicate_within = {
        name: sorted({value for value in values if values.count(value) > 1})
        for name, values in partitions.items()
        if len(values) != len(set(values))
    }
    overlap: dict[str, list[int]] = {}
    names = list(partitions)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            shared = sorted(set(partitions[left]) & set(partitions[right]))
            if shared:
                overlap[f"{left}__{right}"] = shared
    if enforce_disjoint and (duplicate_within or overlap):
        raise ValueError(
            "ADP seed partitions must be unique and disjoint: "
            + json.dumps({"duplicates": duplicate_within, "overlap": overlap}, sort_keys=True)
        )
    return {
        name: {
            "count": len(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "hash": _seed_digest(values),
            "values": values,
        }
        for name, values in partitions.items()
    } | {"disjoint": not duplicate_within and not overlap, "overlap": overlap}


def _seed_partition_row(values: list[int], **extra: Any) -> dict[str, Any]:
    normalized = [int(value) for value in values]
    return {
        "count": len(normalized),
        "min": min(normalized) if normalized else None,
        "max": max(normalized) if normalized else None,
        "hash": _seed_digest(normalized),
        "values": normalized,
        **extra,
    }


def _mc_fit_diagnostics(model: Any, samples: CompactMCBatch, device: Any, batch_size: int) -> dict[str, float]:
    torch = require_torch()
    if not len(samples):
        return {"mae": 0.0, "rmse": 0.0, "prediction_std": 0.0, "target_std": 0.0}
    predictions: list[Any] = []
    targets: list[Any] = []
    model.eval()
    with torch.no_grad():
        for start in range(0, len(samples), max(1, int(batch_size))):
            indices = list(range(start, min(len(samples), start + max(1, int(batch_size)))))
            batch, target = samples.model_batch(indices, device)
            predictions.append(model(**batch).detach().cpu())
            targets.append(target.detach().cpu())
    predicted = torch.cat(predictions)
    actual = torch.cat(targets)
    error = predicted - actual
    return {
        "mae": float(error.abs().mean().item()),
        "rmse": float(torch.sqrt((error * error).mean()).item()),
        "prediction_std": float(predicted.std(unbiased=False).item()),
        "target_std": float(actual.std(unbiased=False).item()),
    }


def _mean_and_std(values: list[int | float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    numeric = [float(value) for value in values]
    return statistics.fmean(numeric), statistics.stdev(numeric) if len(numeric) > 1 else 0.0


def _average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and math.isclose(
            values[order[end]], values[order[cursor]], rel_tol=0.0, abs_tol=1e-12
        ):
            end += 1
        average_rank = (cursor + 1 + end) / 2.0
        for position in range(cursor, end):
            ranks[order[position]] = average_rank
        cursor = end
    return ranks


def _pearson_correlation(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return 0.0
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale <= 1e-12 or right_scale <= 1e-12:
        return 0.0
    return numerator / (left_scale * right_scale)


def evaluate_fixed_action_probe(
    model: Any,
    probe_rows: list[dict[str, Any]],
    device: Any,
) -> dict[str, float | int]:
    if not probe_rows:
        return {
            "action_rank_correlation": 0.0,
            "candidate_top1_agreement": 0.0,
            "selected_action_regret": 0.0,
            "probe_candidate_count": 0,
            "probe_state_count": 0,
            "probe_informative_state_count": 0,
        }
    states = [deserialize_state(dict(row["post_state"])) for row in probe_rows]
    predictions = [float(value) for value in predict_values(model, states, device)]
    targets = [float(row["mc_target"]) for row in probe_rows]
    grouped_indices: dict[str, list[int]] = {}
    for index, row in enumerate(probe_rows):
        grouped_indices.setdefault(str(row["probe_id"]), []).append(index)

    correlations: list[float] = []
    agreements: list[float] = []
    regrets: list[float] = []
    for indices in grouped_indices.values():
        group_predictions = [predictions[index] for index in indices]
        group_targets = [targets[index] for index in indices]
        predicted_best_local = max(
            range(len(indices)),
            key=lambda index: (group_predictions[index], -index),
        )
        target_best = max(group_targets)
        target_worst = min(group_targets)
        if math.isclose(target_best, target_worst, rel_tol=0.0, abs_tol=1e-12):
            continue
        correlations.append(
            _pearson_correlation(
                _average_ranks(group_predictions),
                _average_ranks(group_targets),
            )
        )
        selected_target = group_targets[predicted_best_local]
        agreements.append(
            1.0
            if math.isclose(selected_target, target_best, rel_tol=0.0, abs_tol=1e-12)
            else 0.0
        )
        regrets.append(target_best - selected_target)

    return {
        "action_rank_correlation": statistics.fmean(correlations) if correlations else 0.0,
        "candidate_top1_agreement": statistics.fmean(agreements) if agreements else 0.0,
        "selected_action_regret": statistics.fmean(regrets) if regrets else 0.0,
        "probe_candidate_count": len(probe_rows),
        "probe_state_count": len(grouped_indices),
        "probe_informative_state_count": len(correlations),
    }


def _resolve_training_device(torch: Any, requested: str, *, require_cuda: bool) -> Any:
    normalized = str(requested or "auto").strip().lower()
    if normalized == "auto":
        normalized = "cuda:0" if torch.cuda.is_available() else "cpu"
    if require_cuda and not normalized.startswith("cuda"):
        raise RuntimeError(
            "ADP network updates require CUDA, but the requested training device is "
            f"{normalized!r}. Use a CUDA device such as cuda:0."
        )
    if normalized.startswith("cuda"):
        if not torch.cuda.is_available():
            raise RuntimeError(
                "ADP network updates require a CUDA-enabled PyTorch build, but "
                "torch.cuda.is_available() is False. Install requirements-adp.txt "
                "and verify CUDA before starting the 600-episode training run."
            )
        device = torch.device(normalized)
        torch.cuda.set_device(device)
        return device
    return torch.device(normalized)


def _training_device_metadata(torch: Any, device: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "requested_backend": str(device.type),
        "resolved_device": str(device),
        "torch_version": str(torch.__version__),
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_runtime_version": str(torch.version.cuda or ""),
        "cudnn_version": int(torch.backends.cudnn.version() or 0),
        "gpu_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
    }
    if device.type == "cuda":
        index = int(device.index if device.index is not None else torch.cuda.current_device())
        properties = torch.cuda.get_device_properties(index)
        metadata.update(
            {
                "gpu_index": index,
                "gpu_name": str(properties.name),
                "gpu_total_memory_mib": round(float(properties.total_memory) / (1024.0**2), 3),
                "gpu_compute_capability": f"{properties.major}.{properties.minor}",
            }
        )
    return metadata


def train(config_path: Path, args: argparse.Namespace) -> Path:
    training_started = time.perf_counter()
    torch = require_torch()
    cfg = OmegaConf.to_container(OmegaConf.load(config_path), resolve=True)
    if not isinstance(cfg, dict):
        raise ValueError("ADP config must be a YAML mapping.")
    algorithm = cfg.get("algorithm", {})
    if (
        str(algorithm.get("return_estimator", "")).lower() != "monte_carlo"
        or bool(algorithm.get("n_step_enabled", False))
        or bool(algorithm.get("td_bootstrap_enabled", False))
    ):
        raise ValueError("Simulation-Based ADP supports complete Monte Carlo returns only; n-step and TD bootstrap must be disabled.")
    allow_wait_action = bool(algorithm.get("allow_wait_action", False))
    random_policy_name = (
        "uniform_random_feasible_with_wait"
        if allow_wait_action
        else "uniform_random_feasible_no_wait"
    )
    configured_initial_policy = str(algorithm.get("initial_policy", random_policy_name)).strip()
    if configured_initial_policy != random_policy_name:
        raise ValueError(
            "algorithm.initial_policy does not match allow_wait_action: "
            f"expected {random_policy_name!r}, got {configured_initial_policy!r}."
        )
    training = cfg["training"]
    validation_cfg = cfg.get("validation", {}) if isinstance(cfg.get("validation", {}), dict) else {}
    diagnostics_cfg = cfg.get("diagnostics", {}) if isinstance(cfg.get("diagnostics", {}), dict) else {}
    fixed_probe_cfg = (
        diagnostics_cfg.get("fixed_action_probe", {})
        if isinstance(diagnostics_cfg.get("fixed_action_probe", {}), dict)
        else {}
    )
    fixed_probe_enabled = bool(fixed_probe_cfg.get("enabled", True))
    fixed_probe_seeds = [int(value) for value in fixed_probe_cfg.get("seeds", [])]
    fixed_probe_thresholds = [
        max(1, int(value))
        for value in fixed_probe_cfg.get("decision_thresholds", [20, 100, 300, 600])
    ]
    fixed_probe_candidate_limit = max(
        2, int(fixed_probe_cfg.get("candidates_per_state", 6) or 6)
    )
    if fixed_probe_enabled and (not fixed_probe_seeds or not fixed_probe_thresholds):
        raise ValueError(
            "diagnostics.fixed_action_probe requires non-empty seeds and decision_thresholds."
        )
    ood_cfg = (
        diagnostics_cfg.get("ood_support", {})
        if isinstance(diagnostics_cfg.get("ood_support", {}), dict)
        else {}
    )
    ood_enabled = bool(ood_cfg.get("enabled", True))
    ood_reference_samples = max(64, int(ood_cfg.get("reference_samples", 1024)))
    ood_calibration_samples = max(64, int(ood_cfg.get("calibration_samples", 1024)))
    ood_evaluation_samples = max(64, int(ood_cfg.get("evaluation_samples", 4096)))
    ood_quantile = float(ood_cfg.get("quantile", 0.95))
    if not 0.5 <= ood_quantile < 1.0:
        raise ValueError("diagnostics.ood_support.quantile must be in [0.5, 1.0).")
    model_cfg = cfg["model"]
    rollout_cfg = cfg.get("rollout", {})
    runtime = cfg["runtime"]
    worker_counts = [int(value) for value in (args.worker_counts or cfg["worker_counts"])]
    initial_random = int(args.initial_random_episodes if args.initial_random_episodes is not None else training["initial_random_episodes"])
    policy_iterations = int(args.policy_iterations if args.policy_iterations is not None else training["policy_iterations"])
    episodes_per_iteration = int(args.episodes_per_iteration if args.episodes_per_iteration is not None else training["episodes_per_iteration"])
    loss_type = str(training.get("loss_type", "mse")).strip().lower()
    if loss_type != "mse":
        raise ValueError("training.loss_type is fixed to 'mse'.")
    if "huber_delta" in training:
        raise ValueError("training.huber_delta was removed; Simulation-Based ADP uses MSE only.")
    if "random_wait_probability" in training:
        raise ValueError(
            "training.random_wait_probability was removed; use algorithm.allow_wait_action instead."
        )
    if "potential_shaping" in cfg:
        raise ValueError("potential_shaping was removed; ADP uses raw completed-product rewards.")
    days = int(args.days if args.days is not None else cfg["horizon_days"])
    screening_interval = max(
        1,
        int(validation_cfg.get("screening_interval_iterations", training.get("validation_interval_iterations", 5))),
    )
    screening_include_initial = bool(validation_cfg.get("screening_include_initial", True))
    combined_checkpoint_evaluation = bool(
        validation_cfg.get("combined_checkpoint_evaluation", False)
    )
    train_eval_seed_count = max(
        1,
        int(validation_cfg.get("train_eval_seed_count", 10)),
    )
    screening_seed_count = max(
        1,
        int(
            args.validation_episodes_per_worker
            if args.validation_episodes_per_worker is not None
            else validation_cfg.get("screening_seed_count", training.get("validation_seeds_per_worker", 5))
        ),
    )
    final_candidate_count = max(1, int(validation_cfg.get("final_candidate_count", 1)))
    final_selection_seed_count = max(
        1,
        int(validation_cfg.get("final_selection_seed_count", screening_seed_count)),
    )
    training_episode_target = initial_random + policy_iterations * episodes_per_iteration
    training_seeds = [int(cfg["base_seed"]) + index for index in range(training_episode_target)]
    seed_cfg = cfg.get("seed_partitions", {}) if isinstance(cfg.get("seed_partitions", {}), dict) else {}
    train_eval_seeds = [int(value) for value in seed_cfg.get("training_evaluation", [])]
    if combined_checkpoint_evaluation and not train_eval_seeds:
        train_eval_seeds = list(training_seeds[:train_eval_seed_count])
    screening_seeds = [int(value) for value in seed_cfg.get("screening_validation", [])]
    if args.validation_episodes_per_worker is not None and screening_seeds:
        if len(screening_seeds) < screening_seed_count:
            raise ValueError(
                f"screening validation override requests {screening_seed_count} seeds, "
                f"but the configured partition contains {len(screening_seeds)}"
            )
        screening_seeds = screening_seeds[:screening_seed_count]
    if not screening_seeds:
        screening_seeds = [int(cfg["base_seed"]) + 100000 + 300 + index for index in range(screening_seed_count)]
    final_selection_seeds = [int(value) for value in seed_cfg.get("final_selection_validation", [])]
    if not final_selection_seeds:
        final_selection_seeds = list(screening_seeds[:final_selection_seed_count])
    held_out_seeds = [int(value) for value in seed_cfg.get("held_out_test", [])]
    if len(screening_seeds) != screening_seed_count:
        raise ValueError(
            f"screening validation requires {screening_seed_count} seeds, got {len(screening_seeds)}"
        )
    if len(final_selection_seeds) != final_selection_seed_count:
        raise ValueError(
            f"final selection validation requires {final_selection_seed_count} seeds, "
            f"got {len(final_selection_seeds)}"
        )
    if combined_checkpoint_evaluation:
        if screening_interval != 1 or not screening_include_initial:
            raise ValueError(
                "Combined checkpoint evaluation requires screening_interval_iterations=1 "
                "and screening_include_initial=true so checkpoints 0..N are all evaluated."
            )
        if len(train_eval_seeds) != train_eval_seed_count:
            raise ValueError(
                f"checkpoint train-eval requires {train_eval_seed_count} seeds, "
                f"got {len(train_eval_seeds)}"
            )
        if not set(train_eval_seeds).issubset(set(training_seeds)):
            raise ValueError(
                "checkpoint train-eval seeds must be a subset of the training seed partition."
            )
        external_eval_seeds = set(screening_seeds) | set(final_selection_seeds) | set(held_out_seeds)
        overlap = sorted(set(train_eval_seeds) & external_eval_seeds)
        if overlap:
            raise ValueError(
                "checkpoint train-eval seeds must not overlap validation or held-out seeds: "
                f"{overlap}"
            )
    seed_partition_meta = _validate_seed_partitions(
        training_seeds=training_seeds,
        screening_seeds=screening_seeds,
        final_selection_seeds=final_selection_seeds,
        held_out_seeds=held_out_seeds,
        enforce_disjoint=bool(seed_cfg.get("enforce_disjoint", False)),
    )
    if fixed_probe_enabled:
        occupied_seeds = (
            set(training_seeds)
            | set(screening_seeds)
            | set(final_selection_seeds)
            | set(held_out_seeds)
        )
        overlap = sorted(set(fixed_probe_seeds) & occupied_seeds)
        if overlap:
            raise ValueError(
                "Fixed action probe seeds must not overlap training, validation, or held-out seeds: "
                f"{overlap}"
            )
        seed_partition_meta["fixed_action_probe"] = _seed_partition_row(
            fixed_probe_seeds,
            relationship="disjoint_empirical_counterfactual_diagnostic",
        )
    if combined_checkpoint_evaluation:
        seed_partition_meta["training_evaluation"] = _seed_partition_row(
            train_eval_seeds,
            relationship="subset_of_training",
        )
    requested_device = str(args.device or runtime.get("device", "cuda:0")).lower()
    require_cuda_training = bool(runtime.get("require_cuda_training", True))
    device = _resolve_training_device(
        torch,
        requested_device,
        require_cuda=require_cuda_training,
    )
    training_device_meta = _training_device_metadata(torch, device)
    runtime_environment_meta = {
        "host_name": socket.gethostname(),
        "operating_system": platform.platform(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "cpu_model": platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
        "logical_cpu_count": int(os.cpu_count() or 0),
    }
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
    if not bool(rollout_cfg.get("parallel", True)):
        raise ValueError("Simulation-Based ADP training requires rollout.parallel=true.")
    rollout_process_count = max(
        1,
        int(
            args.rollout_processes
            if args.rollout_processes is not None
            else rollout_cfg.get("process_count", 10)
        ),
    )
    rollout_wave_size = max(1, int(rollout_cfg.get("wave_size", rollout_process_count)))
    if rollout_wave_size > rollout_process_count:
        raise ValueError("rollout.wave_size cannot exceed rollout.process_count.")
    checkpoint_diagnostic_episode_count = len(worker_counts) * (
        train_eval_seed_count + screening_seed_count
    )
    if combined_checkpoint_evaluation and checkpoint_diagnostic_episode_count > rollout_wave_size:
        raise ValueError(
            "Combined checkpoint evaluation must fit in one rollout wave: "
            f"requires {checkpoint_diagnostic_episode_count} slots, wave_size={rollout_wave_size}."
        )
    rollout_device = str(rollout_cfg.get("device", "cpu")).strip().lower()
    if rollout_device != "cpu":
        raise ValueError("Parallel ADP rollout workers must use rollout.device=cpu.")
    rollout_start_method = str(rollout_cfg.get("start_method", "spawn")).strip().lower()
    if rollout_start_method != "spawn":
        raise ValueError("Windows-safe ADP rollout requires rollout.start_method=spawn.")
    rollout_torch_threads = max(1, int(rollout_cfg.get("torch_threads_per_process", 1)))
    if not bool(rollout_cfg.get("deterministic_result_merge", True)):
        raise ValueError("Parallel ADP rollout requires deterministic_result_merge=true.")
    output_root = Path(args.output or runtime["output_root"])
    output_dir = output_root / datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    model = build_value_network(
        embedding_dim=int(model_cfg["embedding_dim"]), heads=int(model_cfg["heads"]), layers=int(model_cfg["layers"])
    ).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=float(training["learning_rate"]))
    rng = random.Random(int(cfg["base_seed"]))
    adp_runtime_cfg = {
        "beam_width": int(model_cfg["beam_width"]),
        "max_review_interval_min": float(training.get("max_review_interval_min", 1.0)),
        "allow_wait_action": allow_wait_action,
    }
    replay_scope = str(training.get("replay_scope", "current_iteration")).strip().lower()
    compact_enabled = bool(training.get("compact_episode_tensors", True))
    release_after_update = bool(training.get("release_samples_after_update", True))
    if replay_scope != "current_iteration" or not compact_enabled or not release_after_update:
        raise ValueError(
            "Simulation-Based ADP training requires replay_scope=current_iteration, "
            "compact_episode_tensors=true, and release_samples_after_update=true."
        )
    validation_episode_fraction = min(0.5, max(0.0, float(training.get("validation_episode_fraction", 0.20))))
    gamma = float(training.get("gamma", 1.0))
    episode_rows: list[dict[str, Any]] = []
    iteration_rows: list[dict[str, Any]] = []
    wave_rows: list[dict[str, Any]] = []
    episode_index = 0
    fingerprint: dict[str, Any] | None = None
    peak_compact_batch_mib = 0.0
    peak_process_rss_mib = _process_rss_mib(peak=True)
    peak_child_rss_mib = 0.0
    total_training_rollout_sec = 0.0
    total_validation_sec = 0.0
    total_merge_sec = 0.0
    total_update_sec = 0.0
    total_fixed_probe_sec = 0.0
    ood_support_bank = None

    def collect_batch(
        count: int,
        *,
        iteration: int,
        phase: str,
        force_random: bool,
        epsilon: float,
        active_model: Any | None,
    ) -> tuple[CompactMCBatch, RolloutBatchMetrics]:
        nonlocal episode_index, fingerprint, peak_compact_batch_mib, peak_process_rss_mib
        nonlocal peak_child_rss_mib, total_training_rollout_sec, total_merge_sec
        model_state, snapshot_hash = _snapshot_state_dict(None if force_random else active_model)
        schedule = _balanced_worker_schedule(worker_counts, count)
        jobs: list[RolloutJob] = []
        for local_index, worker_count in enumerate(schedule):
            absolute_episode = episode_index + local_index + 1
            wave_number = local_index // rollout_wave_size + 1
            jobs.append(
                RolloutJob(
                    episode=absolute_episode,
                    phase=phase,
                    iteration=iteration,
                    worker_count=worker_count,
                    seed=int(cfg["base_seed"]) + absolute_episode - 1,
                    days=days,
                    epsilon=epsilon,
                    force_random=force_random,
                    adp_cfg=adp_runtime_cfg,
                    collect_compact_samples=True,
                    gamma=gamma,
                    wave_id=f"{phase}-I{iteration:02d}-W{wave_number:02d}",
                    snapshot_hash=snapshot_hash,
                )
            )
        results, compact_batch, batch_metrics = _run_rollout_jobs_parallel(
            jobs=jobs,
            model_state=model_state,
            model_cfg=model_cfg,
            process_count=rollout_process_count,
            wave_size=rollout_wave_size,
            start_method=rollout_start_method,
            torch_threads=rollout_torch_threads,
            output_dir=output_dir,
            episode_rows=episode_rows,
            wave_rows=wave_rows,
        )
        for result in results:
            fingerprint = fingerprint or result.fingerprint
        episode_index += len(results)
        total_training_rollout_sec += batch_metrics.wall_sec
        total_merge_sec += batch_metrics.merge_sec
        peak_child_rss_mib = max(peak_child_rss_mib, batch_metrics.child_peak_rss_max_mib)
        compact_mib = compact_batch.memory_bytes / (1024.0**2)
        peak_compact_batch_mib = max(peak_compact_batch_mib, compact_mib)
        peak_process_rss_mib = max(peak_process_rss_mib, _process_rss_mib(peak=True))
        return compact_batch, batch_metrics

    validation_episode_count = 0
    train_evaluation_episode_count = 0
    evaluation_episode_index = 0

    def validate_policy(
        iteration: int,
        *,
        active_model: Any,
        seeds: list[int],
        phase: str,
    ) -> tuple[float, float, dict[int, float], RolloutBatchMetrics]:
        nonlocal validation_episode_count, evaluation_episode_index
        nonlocal peak_process_rss_mib, peak_child_rss_mib
        nonlocal total_validation_sec, total_merge_sec
        validation_products_by_worker: dict[int, list[int]] = {worker_count: [] for worker_count in worker_counts}
        model_state, snapshot_hash = _snapshot_state_dict(active_model)
        jobs: list[RolloutJob] = []
        local_index = 0
        for worker_count in worker_counts:
            for validation_index, validation_seed in enumerate(seeds):
                wave_number = local_index // rollout_wave_size + 1
                jobs.append(
                    RolloutJob(
                        episode=1_000_000 + evaluation_episode_index + local_index + 1,
                        phase=phase,
                        iteration=iteration,
                        worker_count=worker_count,
                        seed=int(validation_seed),
                        days=days,
                        epsilon=0.0,
                        force_random=False,
                        adp_cfg=adp_runtime_cfg,
                        collect_compact_samples=False,
                        gamma=gamma,
                        wave_id=f"{phase}-I{iteration:02d}-W{wave_number:02d}",
                        snapshot_hash=snapshot_hash,
                    )
                )
                local_index += 1
        results, compact_batch, batch_metrics = _run_rollout_jobs_parallel(
            jobs=jobs,
            model_state=model_state,
            model_cfg=model_cfg,
            process_count=rollout_process_count,
            wave_size=rollout_wave_size,
            start_method=rollout_start_method,
            torch_threads=rollout_torch_threads,
            output_dir=output_dir,
            episode_rows=episode_rows,
            wave_rows=wave_rows,
        )
        if len(compact_batch):
            raise RuntimeError("Validation episodes must not retain compact training samples.")
        del compact_batch
        for result in results:
            validation_products_by_worker[result.worker_count].append(result.products)
        evaluation_episode_index += len(results)
        validation_episode_count += len(results)
        total_validation_sec += batch_metrics.wall_sec
        total_merge_sec += batch_metrics.merge_sec
        peak_child_rss_mib = max(peak_child_rss_mib, batch_metrics.child_peak_rss_max_mib)
        peak_process_rss_mib = max(peak_process_rss_mib, _process_rss_mib(peak=True))
        worker_averages = {
            worker_count: statistics.fmean(values) if values else 0.0
            for worker_count, values in validation_products_by_worker.items()
        }
        all_products = [value for values in validation_products_by_worker.values() for value in values]
        average, std = _mean_and_std(all_products)
        return average, std, worker_averages, batch_metrics

    def evaluate_checkpoint(
        iteration: int,
        *,
        active_model: Any,
    ) -> tuple[dict[str, dict[str, Any]], RolloutBatchMetrics]:
        nonlocal validation_episode_count, train_evaluation_episode_count
        nonlocal evaluation_episode_index, peak_process_rss_mib, peak_child_rss_mib
        nonlocal total_validation_sec, total_merge_sec
        model_state, snapshot_hash = _snapshot_state_dict(active_model)
        partitions = (
            ("checkpoint_train_eval", train_eval_seeds),
            ("checkpoint_validation", screening_seeds),
        )
        jobs: list[RolloutJob] = []
        local_index = 0
        for phase, seeds in partitions:
            for worker_count in worker_counts:
                for evaluation_seed in seeds:
                    jobs.append(
                        RolloutJob(
                            episode=1_000_000 + evaluation_episode_index + local_index + 1,
                            phase=phase,
                            iteration=iteration,
                            worker_count=worker_count,
                            seed=int(evaluation_seed),
                            days=days,
                            epsilon=0.0,
                            force_random=False,
                            adp_cfg=adp_runtime_cfg,
                            collect_compact_samples=False,
                            gamma=gamma,
                            wave_id=f"checkpoint_diagnostic-I{iteration:02d}-W01",
                            snapshot_hash=snapshot_hash,
                        )
                    )
                    local_index += 1
        results, compact_batch, batch_metrics = _run_rollout_jobs_parallel(
            jobs=jobs,
            model_state=model_state,
            model_cfg=model_cfg,
            process_count=rollout_process_count,
            wave_size=rollout_wave_size,
            start_method=rollout_start_method,
            torch_threads=rollout_torch_threads,
            output_dir=output_dir,
            episode_rows=episode_rows,
            wave_rows=wave_rows,
        )
        if len(compact_batch):
            raise RuntimeError("Checkpoint evaluation episodes must not retain training samples.")
        del compact_batch
        evaluation_episode_index += len(results)
        train_evaluation_episode_count += sum(
            1 for result in results if result.phase == "checkpoint_train_eval"
        )
        validation_episode_count += sum(
            1 for result in results if result.phase == "checkpoint_validation"
        )
        total_validation_sec += batch_metrics.wall_sec
        total_merge_sec += batch_metrics.merge_sec
        peak_child_rss_mib = max(peak_child_rss_mib, batch_metrics.child_peak_rss_max_mib)
        peak_process_rss_mib = max(peak_process_rss_mib, _process_rss_mib(peak=True))

        summaries: dict[str, dict[str, Any]] = {}
        for phase, seeds in partitions:
            phase_results = [result for result in results if result.phase == phase]
            products = [result.products for result in phase_results]
            mean, std = _mean_and_std(products)
            summaries[phase] = {
                "mean": mean,
                "std": std,
                "sample_count": len(products),
                "seed_count": len(seeds),
                "by_worker": {
                    worker_count: _mean_and_std(
                        [
                            result.products
                            for result in phase_results
                            if result.worker_count == worker_count
                        ]
                    )[0]
                    for worker_count in worker_counts
                },
            }
        return summaries, batch_metrics

    def build_fixed_action_probe() -> list[dict[str, Any]]:
        nonlocal evaluation_episode_index, total_fixed_probe_sec
        nonlocal peak_process_rss_mib, peak_child_rss_mib, total_merge_sec
        if not fixed_probe_enabled:
            return []
        started = time.perf_counter()
        capture_jobs: list[RolloutJob] = []
        for local_index, probe_seed in enumerate(fixed_probe_seeds):
            capture_cfg = {
                **adp_runtime_cfg,
                "_probe_capture_decision_thresholds": list(fixed_probe_thresholds),
                "_probe_candidate_limit": fixed_probe_candidate_limit,
            }
            capture_jobs.append(
                RolloutJob(
                    episode=2_000_000 + local_index,
                    phase="fixed_probe_capture",
                    iteration=-1,
                    worker_count=worker_counts[0],
                    seed=probe_seed,
                    days=days,
                    epsilon=1.0,
                    force_random=True,
                    adp_cfg=capture_cfg,
                    collect_compact_samples=False,
                    gamma=gamma,
                    wave_id=f"fixed-probe-capture-W{local_index // rollout_wave_size + 1:02d}",
                    snapshot_hash="RANDOM",
                )
            )
        capture_results, capture_compact, capture_metrics = _run_rollout_jobs_parallel(
            jobs=capture_jobs,
            model_state=None,
            model_cfg=model_cfg,
            process_count=rollout_process_count,
            wave_size=rollout_wave_size,
            start_method=rollout_start_method,
            torch_threads=rollout_torch_threads,
            output_dir=output_dir,
            episode_rows=episode_rows,
            wave_rows=wave_rows,
        )
        if len(capture_compact):
            raise RuntimeError("Fixed probe capture must not retain compact training samples.")
        del capture_compact
        total_merge_sec += capture_metrics.merge_sec
        peak_child_rss_mib = max(peak_child_rss_mib, capture_metrics.child_peak_rss_max_mib)

        captured: list[dict[str, Any]] = []
        for result in capture_results:
            records = result.probe_records or []
            if len(records) != len(fixed_probe_thresholds):
                raise RuntimeError(
                    "Fixed action probe could not capture every configured state: "
                    f"seed={result.seed}, expected={len(fixed_probe_thresholds)}, got={len(records)}"
                )
            for record in records:
                probe_id = f"S{result.seed}-D{int(record['decision_number']):05d}"
                captured.append(
                    {
                        **record,
                        "probe_id": probe_id,
                        "seed": int(result.seed),
                    }
                )

        branch_jobs: list[RolloutJob] = []
        candidate_payloads: dict[tuple[str, int], dict[str, Any]] = {}
        for record in captured:
            prefix = [dict(row) for row in record["prefix_actions"]]
            for candidate in record["candidates"]:
                candidate_id = int(candidate["candidate_id"])
                probe_id = str(record["probe_id"])
                candidate_payloads[(probe_id, candidate_id)] = {
                    "probe_id": probe_id,
                    "candidate_id": candidate_id,
                    "seed": int(record["seed"]),
                    "threshold": int(record["threshold"]),
                    "decision_number": int(record["decision_number"]),
                    "time_min": float(record["time_min"]),
                    "products_before": int(record["products_before"]),
                    "assignment": dict(candidate["assignment"]),
                    "post_state": dict(candidate["post_state"]),
                }
                branch_cfg = {
                    **adp_runtime_cfg,
                    "_probe_forced_action_script": prefix + [dict(candidate["assignment"])],
                    "_probe_target_decision_number": int(record["decision_number"]),
                    "_probe_id": probe_id,
                    "_probe_candidate_id": candidate_id,
                }
                local_index = len(branch_jobs)
                branch_jobs.append(
                    RolloutJob(
                        episode=2_100_000 + local_index,
                        phase="fixed_probe_counterfactual",
                        iteration=-1,
                        worker_count=worker_counts[0],
                        seed=int(record["seed"]),
                        days=days,
                        epsilon=1.0,
                        force_random=True,
                        adp_cfg=branch_cfg,
                        collect_compact_samples=False,
                        gamma=gamma,
                        wave_id=(
                            f"fixed-probe-counterfactual-W"
                            f"{local_index // rollout_wave_size + 1:02d}"
                        ),
                        snapshot_hash="RANDOM",
                    )
                )
        branch_results, branch_compact, branch_metrics = _run_rollout_jobs_parallel(
            jobs=branch_jobs,
            model_state=None,
            model_cfg=model_cfg,
            process_count=rollout_process_count,
            wave_size=rollout_wave_size,
            start_method=rollout_start_method,
            torch_threads=rollout_torch_threads,
            output_dir=output_dir,
            episode_rows=episode_rows,
            wave_rows=wave_rows,
        )
        if len(branch_compact):
            raise RuntimeError("Fixed counterfactual probe must not retain compact samples.")
        del branch_compact
        total_merge_sec += branch_metrics.merge_sec
        peak_child_rss_mib = max(peak_child_rss_mib, branch_metrics.child_peak_rss_max_mib)
        probe_rows: list[dict[str, Any]] = []
        for result in branch_results:
            key = (result.probe_id, int(result.probe_candidate_id))
            if key not in candidate_payloads:
                raise RuntimeError(f"Unknown fixed probe branch result: {key}")
            if result.probe_target_product_count is None:
                raise RuntimeError(
                    "Fixed probe replay did not reach its target decision: "
                    f"probe={result.probe_id}, candidate={result.probe_candidate_id}"
                )
            row = dict(candidate_payloads[key])
            row.update(
                {
                    "terminal_products": int(result.products),
                    "target_start_products": int(result.probe_target_product_count),
                    "mc_target": int(result.products)
                    - int(result.probe_target_product_count),
                }
            )
            probe_rows.append(row)
        probe_rows.sort(key=lambda row: (str(row["probe_id"]), int(row["candidate_id"])))
        (output_dir / "fixed_action_probe.json").write_text(
            json.dumps(
                {
                    "contract": "fixed_seed_empirical_counterfactual_mc",
                    "global_optimum_claimed": False,
                    "continuation_policy": random_policy_name,
                    "review_interval_min": adp_runtime_cfg["max_review_interval_min"],
                    "probe_state_count": len(captured),
                    "candidate_count": len(probe_rows),
                    "rows": probe_rows,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        evaluation_episode_index += len(capture_results) + len(branch_results)
        total_fixed_probe_sec += time.perf_counter() - started
        peak_process_rss_mib = max(peak_process_rss_mib, _process_rss_mib(peak=True))
        return probe_rows

    fixed_action_probe_rows = build_fixed_action_probe()

    current_batch, current_rollout_metrics = collect_batch(
        initial_random,
        iteration=0,
        phase="initial_random",
        force_random=True,
        epsilon=1.0,
        active_model=None,
    )
    checkpoints_dir = output_dir / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_manifests: dict[int, dict[str, Any]] = {}
    screening_rows: list[dict[str, Any]] = []
    last_validation = 0.0
    last_validation_std = 0.0
    last_validation_by_worker = {worker_count: 0.0 for worker_count in worker_counts}
    last_train_eval = 0.0
    last_train_eval_std = 0.0
    last_train_eval_by_worker = {worker_count: 0.0 for worker_count in worker_counts}
    for iteration in range(policy_iterations + 1):
        batch_episode_count = current_batch.episode_count
        batch_sample_count = len(current_batch)
        compact_batch_mib = current_batch.memory_bytes / (1024.0**2)
        ood_started = time.perf_counter()
        ood_metrics = evaluate_ood_selected_actions(
            model,
            current_batch,
            ood_support_bank if ood_enabled else None,
            device=device,
            batch_size=int(training["batch_size"]),
            seed=int(cfg["base_seed"]) ^ (iteration << 12) ^ 0x00D2026,
            evaluation_samples=ood_evaluation_samples,
        )
        next_ood_support_bank = (
            build_ood_support_bank(
                current_batch,
                device=device,
                seed=int(cfg["base_seed"]) ^ (iteration << 8) ^ 0x5A770,
                reference_samples=ood_reference_samples,
                calibration_samples=ood_calibration_samples,
                quantile=ood_quantile,
            )
            if ood_enabled
            else None
        )
        ood_diagnostic_sec = time.perf_counter() - ood_started
        if device.type == "cuda":
            torch.cuda.reset_peak_memory_stats(device)
        update_started = time.perf_counter()
        loss, epochs = fit_mc_value(
            model=model,
            optimizer=optimizer,
            samples=current_batch,
            device=device,
            batch_size=int(training["batch_size"]),
            max_epochs=int(training["max_epochs_per_iteration"]),
            gradient_clip=float(training["gradient_clip"]),
            patience=int(training["early_stopping_patience"]),
            validation_episode_fraction=validation_episode_fraction,
            rng=rng,
        )
        fit_diagnostics = _mc_fit_diagnostics(
            model,
            current_batch,
            device,
            int(training["batch_size"]),
        )
        fixed_probe_metrics = evaluate_fixed_action_probe(
            model,
            fixed_action_probe_rows,
            device,
        )
        update_sec = time.perf_counter() - update_started
        total_update_sec += update_sec
        gpu_allocated_mib = (
            float(torch.cuda.memory_allocated(device)) / (1024.0**2)
            if device.type == "cuda"
            else 0.0
        )
        gpu_reserved_mib = (
            float(torch.cuda.memory_reserved(device)) / (1024.0**2)
            if device.type == "cuda"
            else 0.0
        )
        gpu_peak_allocated_mib = (
            float(torch.cuda.max_memory_allocated(device)) / (1024.0**2)
            if device.type == "cuda"
            else 0.0
        )
        gpu_peak_reserved_mib = (
            float(torch.cuda.max_memory_reserved(device)) / (1024.0**2)
            if device.type == "cuda"
            else 0.0
        )
        peak_process_rss_mib = max(peak_process_rss_mib, _process_rss_mib(peak=True))
        validation_performed = (
            True
            if combined_checkpoint_evaluation
            else _should_validate(
                iteration,
                policy_iterations,
                screening_interval,
                include_initial=screening_include_initial,
            )
        )
        validation_metrics = RolloutBatchMetrics()
        if validation_performed:
            if combined_checkpoint_evaluation:
                checkpoint_evaluation, validation_metrics = evaluate_checkpoint(
                    iteration,
                    active_model=model,
                )
                train_eval_summary = checkpoint_evaluation["checkpoint_train_eval"]
                validation_summary = checkpoint_evaluation["checkpoint_validation"]
                last_train_eval = float(train_eval_summary["mean"])
                last_train_eval_std = float(train_eval_summary["std"])
                last_train_eval_by_worker = dict(train_eval_summary["by_worker"])
                last_validation = float(validation_summary["mean"])
                last_validation_std = float(validation_summary["std"])
                last_validation_by_worker = dict(validation_summary["by_worker"])
            else:
                (
                    last_validation,
                    last_validation_std,
                    last_validation_by_worker,
                    validation_metrics,
                ) = validate_policy(
                    iteration,
                    active_model=model,
                    seeds=screening_seeds,
                    phase="screening_validation",
                )
        train_eval_sample_count = len(train_eval_seeds) * len(worker_counts)
        validation_sample_count = len(screening_seeds) * len(worker_counts)
        iteration_row = {
            "iteration": iteration,
            "phase": "initial_random" if iteration == 0 else f"policy_iteration_{iteration}",
            "mc_loss": loss,
            "mc_mae": round(fit_diagnostics["mae"], 6),
            "mc_rmse": round(fit_diagnostics["rmse"], 6),
            "prediction_std": round(fit_diagnostics["prediction_std"], 6),
            "target_std": round(fit_diagnostics["target_std"], 6),
            "action_rank_correlation": round(
                float(fixed_probe_metrics["action_rank_correlation"]), 6
            ),
            "candidate_top1_agreement": round(
                float(fixed_probe_metrics["candidate_top1_agreement"]), 6
            ),
            "selected_action_regret": round(
                float(fixed_probe_metrics["selected_action_regret"]), 6
            ),
            "ood_diagnostics_available": bool(ood_metrics["available"]),
            "ood_selection_rate": (
                round(float(ood_metrics["ood_selection_rate"]), 6)
                if ood_metrics["ood_selection_rate"] is not None
                else ""
            ),
            "ood_overestimation_excess": (
                round(float(ood_metrics["ood_overestimation_excess"]), 6)
                if ood_metrics["ood_overestimation_excess"] is not None
                else ""
            ),
            "ood_evaluated_selection_count": int(ood_metrics["evaluated_selection_count"]),
            "ood_selection_count": int(ood_metrics["ood_selection_count"]),
            "ood_in_support_selection_count": int(ood_metrics["in_support_selection_count"]),
            "ood_support_distance_threshold": (
                round(float(ood_metrics["support_distance_threshold"]), 6)
                if ood_metrics["support_distance_threshold"] is not None
                else ""
            ),
            "ood_diagnostic_sec": round(ood_diagnostic_sec, 6),
            "probe_state_count": int(fixed_probe_metrics["probe_state_count"]),
            "probe_candidate_count": int(fixed_probe_metrics["probe_candidate_count"]),
            "probe_informative_state_count": int(
                fixed_probe_metrics["probe_informative_state_count"]
            ),
            "epochs": epochs,
            "batch_episode_count": batch_episode_count,
            "sample_count": batch_sample_count,
            "compact_batch_mib": round(compact_batch_mib, 6),
            "process_rss_mib": round(_process_rss_mib(), 6),
            "epsilon": 1.0 if iteration == 0 else _linear(
                float(training["epsilon_start"]),
                float(training["epsilon_end"]),
                iteration - 1,
                policy_iterations,
            ),
            "rollout_wall_sec": round(current_rollout_metrics.wall_sec, 6),
            "rollout_wave_count": current_rollout_metrics.wave_count,
            "rollout_episodes_per_hour": round(current_rollout_metrics.episodes_per_hour, 6),
            "rollout_effective_speedup": round(current_rollout_metrics.effective_speedup, 6),
            "rollout_parallel_efficiency": round(current_rollout_metrics.parallel_efficiency, 6),
            "rollout_active_slot_utilization": round(current_rollout_metrics.active_slot_utilization, 6),
            "rollout_merge_sec": round(current_rollout_metrics.merge_sec, 6),
            "mc_update_sec": round(update_sec, 6),
            "gpu_memory_allocated_mib": round(gpu_allocated_mib, 6),
            "gpu_memory_reserved_mib": round(gpu_reserved_mib, 6),
            "gpu_peak_allocated_mib": round(gpu_peak_allocated_mib, 6),
            "gpu_peak_reserved_mib": round(gpu_peak_reserved_mib, 6),
            "validation_wall_sec": round(validation_metrics.wall_sec, 6),
            "validation_wave_count": validation_metrics.wave_count,
            "validation_performed": validation_performed,
            "train_eval_performed": validation_performed and combined_checkpoint_evaluation,
            "train_eval_products_avg": (
                last_train_eval if validation_performed and combined_checkpoint_evaluation else ""
            ),
            "train_eval_products_std": (
                last_train_eval_std if validation_performed and combined_checkpoint_evaluation else ""
            ),
            "train_eval_products_ci95_low": (
                last_train_eval
                - 1.96 * last_train_eval_std / math.sqrt(max(1, train_eval_sample_count))
                if validation_performed and combined_checkpoint_evaluation
                else ""
            ),
            "train_eval_products_ci95_high": (
                last_train_eval
                + 1.96 * last_train_eval_std / math.sqrt(max(1, train_eval_sample_count))
                if validation_performed and combined_checkpoint_evaluation
                else ""
            ),
            "validation_products_avg": last_validation if validation_performed else "",
            "validation_products_std": last_validation_std if validation_performed else "",
            "validation_products_ci95_low": (
                last_validation
                - 1.96 * last_validation_std / math.sqrt(max(1, validation_sample_count))
                if validation_performed else ""
            ),
            "validation_products_ci95_high": (
                last_validation
                + 1.96 * last_validation_std / math.sqrt(max(1, validation_sample_count))
                if validation_performed else ""
            ),
            "validation_stage": (
                "combined_checkpoint_diagnostic"
                if validation_performed and combined_checkpoint_evaluation
                else "screening" if validation_performed else ""
            ),
        }
        iteration_row.update(
            {
                f"validation_products_avg_workers_{worker_count}": (
                    last_validation_by_worker[worker_count] if validation_performed else ""
                )
                for worker_count in worker_counts
            }
        )
        iteration_row.update(
            {
                f"train_eval_products_avg_workers_{worker_count}": (
                    last_train_eval_by_worker[worker_count]
                    if validation_performed and combined_checkpoint_evaluation
                    else ""
                )
                for worker_count in worker_counts
            }
        )
        iteration_rows.append(iteration_row)
        _write_csv(output_dir / "iteration_metrics.csv", iteration_rows)
        manifest = {
            **(fingerprint or {}),
            "mansim_version": mansim_version,
            "reward_mode": "completed_product_mc",
            "loss_type": "mse",
            "random_policy": random_policy_name,
            "wait_action_enabled": allow_wait_action,
            "potential_shaping": False,
            "seed_partitions": seed_partition_meta,
            "fixed_action_probe": {
                "enabled": fixed_probe_enabled,
                "contract": "fixed_seed_empirical_counterfactual_mc",
                "global_optimum_claimed": False,
                "state_count": int(fixed_probe_metrics["probe_state_count"]),
                "candidate_count": int(fixed_probe_metrics["probe_candidate_count"]),
                "informative_state_count": int(
                    fixed_probe_metrics["probe_informative_state_count"]
                ),
                "metrics": dict(fixed_probe_metrics),
            },
            "ood_support_diagnostics": {
                "enabled": ood_enabled,
                "contract": "previous_on_policy_batch_q95_support",
                "reference_samples": ood_reference_samples,
                "calibration_samples": ood_calibration_samples,
                "evaluation_samples": ood_evaluation_samples,
                "quantile": ood_quantile,
                "greedy_selections_only": True,
            },
            "checkpoint_id": f"ADP-{output_dir.name}-I{iteration:02d}",
            "worker_count_range": [min(worker_counts), max(worker_counts)],
            "horizon_days": days,
            "model": {
                "embedding_dim": int(model_cfg["embedding_dim"]),
                "heads": int(model_cfg["heads"]),
                "layers": int(model_cfg["layers"]),
                "beam_width": int(model_cfg["beam_width"]),
            },
            "training": {
                "mc_return_only": True,
                "n_step": False,
                "td_bootstrap": False,
                "initial_policy": random_policy_name,
                "reward_mode": "completed_product_mc",
                "potential_shaping": False,
                "replay_scope": replay_scope,
                "compact_episode_tensors": compact_enabled,
                "release_samples_after_update": release_after_update,
                "policy_iterations": policy_iterations,
                "episodes_per_iteration": episodes_per_iteration,
                "screening_interval_iterations": screening_interval,
                "screening_seed_count": screening_seed_count,
                "combined_checkpoint_evaluation": combined_checkpoint_evaluation,
                "train_eval_seed_count": (
                    train_eval_seed_count if combined_checkpoint_evaluation else 0
                ),
                "final_candidate_count": final_candidate_count,
                "final_selection_seed_count": final_selection_seed_count,
                "training_episode_count": initial_random + policy_iterations * episodes_per_iteration,
                "loss_type": "mse",
                "learning_rate": float(training["learning_rate"]),
                "max_epochs_per_iteration": int(training["max_epochs_per_iteration"]),
                "epsilon_start": float(training["epsilon_start"]),
                "epsilon_end": float(training["epsilon_end"]),
                "max_review_interval_min": float(adp_runtime_cfg["max_review_interval_min"]),
                "wait_action_enabled": allow_wait_action,
                "peak_compact_batch_mib": round(peak_compact_batch_mib, 6),
                "observed_peak_process_rss_mib": round(peak_process_rss_mib, 6),
                "rollout_parallel": True,
                "rollout_process_count": rollout_process_count,
                "rollout_wave_size": rollout_wave_size,
                "rollout_start_method": rollout_start_method,
                "rollout_device": rollout_device,
                "rollout_torch_threads_per_process": rollout_torch_threads,
                "peak_child_rss_mib": round(peak_child_rss_mib, 6),
                "require_cuda_training": require_cuda_training,
                "training_device": training_device_meta,
                "runtime_environment": runtime_environment_meta,
            },
            "validation_completed_products_avg": last_validation,
            "validation_completed_products_std": last_validation_std,
            "train_evaluation_completed_products_avg": (
                last_train_eval if combined_checkpoint_evaluation else None
            ),
            "train_evaluation_completed_products_std": (
                last_train_eval_std if combined_checkpoint_evaluation else None
            ),
            "validation_completed_products_avg_by_worker_count": {
                str(worker_count): last_validation_by_worker[worker_count]
                for worker_count in worker_counts
            },
        }
        checkpoint_manifests[iteration] = manifest
        iteration_checkpoint = checkpoints_dir / f"iteration_{iteration:03d}.pt"
        save_checkpoint(iteration_checkpoint, model=model, optimizer=None, manifest=manifest)
        save_checkpoint(output_dir / "last.pt", model=model, optimizer=optimizer, manifest=manifest)
        if validation_performed:
            candidate_checkpoint = checkpoints_dir / f"selection_{iteration:03d}.pt"
            save_checkpoint(candidate_checkpoint, model=model, optimizer=optimizer, manifest=manifest)
            screening_rows.append(
                {
                    "iteration": iteration,
                    "train_eval_mean_products": (
                        round(last_train_eval, 6) if combined_checkpoint_evaluation else ""
                    ),
                    "train_eval_std_products": (
                        round(last_train_eval_std, 6) if combined_checkpoint_evaluation else ""
                    ),
                    "screening_mean_products": round(last_validation, 6),
                    "screening_std_products": round(last_validation_std, 6),
                    "screening_seed_count": len(screening_seeds),
                    "iteration_checkpoint": str(iteration_checkpoint.resolve()),
                    "selection_checkpoint": str(candidate_checkpoint.resolve()),
                }
            )
        ood_support_bank = next_ood_support_bank
        del current_batch
        gc.collect()
        peak_process_rss_mib = max(peak_process_rss_mib, _process_rss_mib(peak=True))
        if iteration < policy_iterations:
            epsilon = _linear(
                float(training["epsilon_start"]),
                float(training["epsilon_end"]),
                iteration,
                policy_iterations,
            )
            current_batch, current_rollout_metrics = collect_batch(
                episodes_per_iteration,
                iteration=iteration + 1,
                phase=f"policy_iteration_{iteration + 1}",
                force_random=False,
                epsilon=epsilon,
                active_model=model,
            )

    ranked_screening = sorted(
        screening_rows,
        key=lambda row: (
            -float(row["screening_mean_products"]),
            float(row["screening_std_products"]),
            int(row["iteration"]),
        ),
    )
    selected_screening = ranked_screening[: min(final_candidate_count, len(ranked_screening))]
    selection_rows: list[dict[str, Any]] = []
    selection_payloads: dict[int, dict[str, Any]] = {}
    for screening_rank, row in enumerate(selected_screening, start=1):
        candidate_iteration = int(row["iteration"])
        candidate_path = Path(str(row["selection_checkpoint"]))
        payload = torch.load(candidate_path, map_location=device, weights_only=False)
        candidate_model = build_value_network(
            embedding_dim=int(model_cfg["embedding_dim"]),
            heads=int(model_cfg["heads"]),
            layers=int(model_cfg["layers"]),
        ).to(device)
        candidate_model.load_state_dict(payload["model_state_dict"])
        candidate_model.eval()
        final_mean, final_std, _worker_means, final_metrics = validate_policy(
            candidate_iteration,
            active_model=candidate_model,
            seeds=final_selection_seeds,
            phase="final_selection_validation",
        )
        selection_payloads[candidate_iteration] = payload
        selection_rows.append(
            {
                **row,
                "screening_rank": screening_rank,
                "final_mean_products": round(final_mean, 6),
                "final_std_products": round(final_std, 6),
                "final_seed_count": len(final_selection_seeds),
                "final_ci95_low": round(
                    final_mean - 1.96 * final_std / math.sqrt(len(final_selection_seeds)), 6
                ),
                "final_ci95_high": round(
                    final_mean + 1.96 * final_std / math.sqrt(len(final_selection_seeds)), 6
                ),
                "final_validation_wall_sec": round(final_metrics.wall_sec, 6),
            }
        )
        del candidate_model
        gc.collect()
    if not selection_rows:
        raise RuntimeError("ADP training produced no screened checkpoint candidates.")
    ranked_final = sorted(
        selection_rows,
        key=lambda row: (
            -float(row["final_mean_products"]),
            float(row["final_std_products"]),
            int(row["iteration"]),
        ),
    )
    winner = ranked_final[0]
    best_iteration = int(winner["iteration"])
    best_validation = float(winner["final_mean_products"])
    best_validation_std = float(winner["final_std_products"])
    best_payload = selection_payloads[best_iteration]
    best_manifest = dict(best_payload.get("manifest", checkpoint_manifests[best_iteration]))
    best_manifest["validation_completed_products_avg"] = best_validation
    best_manifest["validation_completed_products_std"] = best_validation_std
    best_manifest["checkpoint_selection"] = {
        "stage": "final_selection",
        "screening_rank": int(winner["screening_rank"]),
        "best_iteration": best_iteration,
        "candidate_count": len(selection_rows),
        "selection_rule": "max_mean_then_min_std_then_earliest_iteration",
    }
    best_manifest["seed_partitions"] = seed_partition_meta
    torch.save(
        {
            "model_state_dict": best_payload["model_state_dict"],
            "optimizer_state_dict": best_payload.get("optimizer_state_dict"),
            "manifest": best_manifest,
        },
        output_dir / "best.pt",
    )
    for row in selection_rows:
        row["selected_best"] = int(row["iteration"]) == best_iteration
        row.pop("selection_checkpoint", None)
        row["temporary_optimizer_checkpoint_removed"] = True
    _write_csv(output_dir / "checkpoint_selection.csv", selection_rows)
    for path in checkpoints_dir.glob("selection_*.pt"):
        path.unlink(missing_ok=True)
    _write_csv(output_dir / "episode_metrics.csv", episode_rows)
    _write_csv(output_dir / "wave_metrics.csv", wave_rows)
    _write_csv(output_dir / "iteration_metrics.csv", iteration_rows)
    completed_wave_rows = [row for row in wave_rows if row.get("status") == "completed"]
    phase_wave_counts = {
        phase: sum(1 for row in completed_wave_rows if str(row.get("phase", "")).startswith(phase))
        for phase in (
            "fixed_probe_capture",
            "fixed_probe_counterfactual",
            "initial_random",
            "policy_iteration",
            "checkpoint_diagnostic",
            "screening_validation",
            "final_selection_validation",
        )
    }
    total_episode_elapsed_sec = sum(float(row.get("elapsed_sec", 0.0)) for row in episode_rows)
    total_wave_wall_sec = sum(float(row.get("wall_sec", 0.0)) for row in completed_wave_rows)
    total_slot_capacity_sec = sum(
        float(row.get("wall_sec", 0.0)) * max(1, int(row.get("active_process_count", 1)))
        for row in completed_wave_rows
    )
    overall_speedup = total_episode_elapsed_sec / max(1e-9, total_wave_wall_sec)
    overall_slot_utilization = total_episode_elapsed_sec / max(1e-9, total_slot_capacity_sec)
    overall_efficiency = overall_slot_utilization
    final_training_meta = dict(best_manifest.get("training", {}))
    final_training_meta.update(
        {
            "initial_random_episodes": initial_random,
            "policy_iterations": policy_iterations,
            "episodes_per_iteration": episodes_per_iteration,
            "training_episode_count": episode_index,
            "loss_type": "mse",
            "reward_mode": "completed_product_mc",
            "potential_shaping": False,
            "random_policy": random_policy_name,
            "wait_action_enabled": allow_wait_action,
            "learning_rate": float(training["learning_rate"]),
            "max_epochs_per_iteration": int(training["max_epochs_per_iteration"]),
            "epsilon_start": float(training["epsilon_start"]),
            "epsilon_end": float(training["epsilon_end"]),
            "screening_interval_iterations": screening_interval,
            "screening_include_initial": screening_include_initial,
            "screening_seed_count": screening_seed_count,
            "combined_checkpoint_evaluation": combined_checkpoint_evaluation,
            "train_eval_seed_count": (
                train_eval_seed_count if combined_checkpoint_evaluation else 0
            ),
            "final_candidate_count": final_candidate_count,
            "final_selection_seed_count": final_selection_seed_count,
            "screening_iterations": [
                int(row["iteration"]) for row in iteration_rows if bool(row["validation_performed"])
            ],
            "best_iteration": best_iteration,
            "replay_scope": replay_scope,
            "compact_episode_tensors": compact_enabled,
            "release_samples_after_update": release_after_update,
            "peak_compact_batch_mib": round(peak_compact_batch_mib, 6),
            "observed_peak_process_rss_mib": round(peak_process_rss_mib, 6),
            "peak_child_rss_mib": round(peak_child_rss_mib, 6),
            "rollout_parallel": True,
            "rollout_process_count": rollout_process_count,
            "rollout_wave_size": rollout_wave_size,
            "rollout_start_method": rollout_start_method,
            "rollout_device": rollout_device,
            "rollout_torch_threads_per_process": rollout_torch_threads,
            "total_wave_count": len(completed_wave_rows),
            "phase_wave_counts": phase_wave_counts,
            "effective_speedup": round(overall_speedup, 6),
            "parallel_efficiency": round(overall_efficiency, 6),
            "active_slot_utilization": round(overall_slot_utilization, 6),
            "require_cuda_training": require_cuda_training,
            "training_device": training_device_meta,
            "runtime_environment": runtime_environment_meta,
            "fixed_action_probe": dict(best_manifest.get("fixed_action_probe", {})),
            "ood_support_diagnostics": dict(
                best_manifest.get("ood_support_diagnostics", {})
            ),
        }
    )
    best_manifest["training"] = final_training_meta
    best_manifest["seed_partitions"] = seed_partition_meta
    torch.save(
        {
            "model_state_dict": best_payload["model_state_dict"],
            "optimizer_state_dict": best_payload.get("optimizer_state_dict"),
            "manifest": best_manifest,
        },
        output_dir / "best.pt",
    )
    (output_dir / "checkpoint_manifest.json").write_text(json.dumps(best_manifest, indent=2), encoding="utf-8")
    summary = {
        "mansim_version": mansim_version,
        "training_episode_count": episode_index,
        "validation_episode_count": validation_episode_count,
        "train_evaluation_episode_count": train_evaluation_episode_count,
        "evaluation_episode_count": validation_episode_count + train_evaluation_episode_count,
        "worker_counts": worker_counts,
        "horizon_days": days,
        "device": str(device),
        "policy_iterations": policy_iterations,
        "episodes_per_iteration": episodes_per_iteration,
        "loss_type": "mse",
        "reward_mode": "completed_product_mc",
        "potential_shaping": False,
        "random_policy": random_policy_name,
        "wait_action_enabled": allow_wait_action,
        "learning_rate": float(training["learning_rate"]),
        "max_epochs_per_iteration": int(training["max_epochs_per_iteration"]),
        "epsilon_start": float(training["epsilon_start"]),
        "epsilon_end": float(training["epsilon_end"]),
        "screening_interval_iterations": screening_interval,
        "screening_include_initial": screening_include_initial,
        "combined_checkpoint_evaluation": combined_checkpoint_evaluation,
        "train_eval_seed_count": (
            train_eval_seed_count if combined_checkpoint_evaluation else 0
        ),
        "screening_iterations": final_training_meta["screening_iterations"],
        "train_evaluation_checkpoint_episode_count": train_evaluation_episode_count,
        "screening_validation_episode_count": (
            len(screening_seeds)
            * len(worker_counts)
            * len(final_training_meta["screening_iterations"])
        ),
        "final_selection_validation_episode_count": (
            len(final_selection_seeds) * len(worker_counts) * len(selection_rows)
        ),
        "final_candidate_count": len(selection_rows),
        "best_iteration": best_iteration,
        "best_validation_completed_products_avg": best_validation,
        "best_validation_completed_products_std": best_validation_std,
        "best_checkpoint": str((output_dir / "best.pt").resolve()),
        "last_checkpoint": str((output_dir / "last.pt").resolve()),
        "replay_scope": replay_scope,
        "compact_episode_tensors": compact_enabled,
        "release_samples_after_update": release_after_update,
        "max_review_interval_min": float(adp_runtime_cfg["max_review_interval_min"]),
        "seed_partitions": seed_partition_meta,
        "fixed_action_probe": dict(best_manifest.get("fixed_action_probe", {})),
        "fixed_action_probe_file": str((output_dir / "fixed_action_probe.json").resolve()),
        "fixed_action_probe_wall_sec": round(total_fixed_probe_sec, 6),
        "ood_support_diagnostics": dict(
            best_manifest.get("ood_support_diagnostics", {})
        ),
        "ood_diagnostic_sec": round(
            sum(float(row.get("ood_diagnostic_sec", 0.0) or 0.0) for row in iteration_rows),
            6,
        ),
        "checkpoint_selection_file": str((output_dir / "checkpoint_selection.csv").resolve()),
        "peak_compact_batch_mib": round(peak_compact_batch_mib, 6),
        "observed_peak_process_rss_mib": round(peak_process_rss_mib, 6),
        "peak_child_rss_mib": round(peak_child_rss_mib, 6),
        "rollout_parallel": True,
        "configured_process_count": rollout_process_count,
        "actual_process_count_max": max(
            (int(row.get("actual_process_count", 0)) for row in completed_wave_rows), default=0
        ),
        "wave_size": rollout_wave_size,
        "multiprocessing_start_method": rollout_start_method,
        "rollout_device": rollout_device,
        "training_device": str(device),
        "training_device_requested": requested_device,
        "require_cuda_training": require_cuda_training,
        "training_device_metadata": training_device_meta,
        "runtime_environment": runtime_environment_meta,
        "gpu_peak_allocated_mib": round(
            float(torch.cuda.max_memory_allocated(device)) / (1024.0**2)
            if device.type == "cuda"
            else 0.0,
            6,
        ),
        "gpu_peak_reserved_mib": round(
            float(torch.cuda.max_memory_reserved(device)) / (1024.0**2)
            if device.type == "cuda"
            else 0.0,
            6,
        ),
        "torch_threads_per_process": rollout_torch_threads,
        "total_wave_count": len(completed_wave_rows),
        "phase_wave_counts": phase_wave_counts,
        "training_rollout_sec": round(total_training_rollout_sec, 6),
        "validation_wall_sec": round(total_validation_sec, 6),
        "compact_merge_sec": round(total_merge_sec, 6),
        "mc_update_sec": round(total_update_sec, 6),
        "total_episode_elapsed_sec": round(total_episode_elapsed_sec, 6),
        "total_wave_wall_sec": round(total_wave_wall_sec, 6),
        "effective_speedup": round(overall_speedup, 6),
        "parallel_efficiency": round(overall_efficiency, 6),
        "active_slot_utilization": round(overall_slot_utilization, 6),
        "episodes_per_hour": round(
            len(episode_rows) * 3600.0 / max(1e-9, total_wave_wall_sec), 6
        ),
        "ipc_payload_mib": round(
            sum(float(row.get("ipc_payload_mib", 0.0)) for row in completed_wave_rows), 6
        ),
        "coordination_ipc_overhead_sec": round(
            sum(float(row.get("coordination_ipc_overhead_sec", 0.0)) for row in completed_wave_rows), 6
        ),
        "failed_wave_count": sum(1 for row in wave_rows if row.get("status") == "failed"),
        "cancelled_episode_count": 0,
        "retried_episode_count": 0,
        "fit_diagnostics_available": True,
        "update_timing_available": True,
        "gpu_memory_diagnostics_available": True,
        "process_memory_diagnostics_available": True,
        "wall_clock_complete": True,
        "mc_return_only": True,
        "n_step_used": False,
        "td_bootstrap_used": False,
    }
    summary["total_wall_clock_sec"] = round(time.perf_counter() - training_started, 6)
    (output_dir / "training_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    artifact_started = time.perf_counter()
    dashboard = render_training_dashboard(output_dir, episode_rows, iteration_rows, wave_rows, summary)
    summary["checkpoint_dashboard_write_sec"] = round(time.perf_counter() - artifact_started, 6)
    summary["total_wall_clock_sec"] = round(time.perf_counter() - training_started, 6)
    (output_dir / "training_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    dashboard = render_training_dashboard(output_dir, episode_rows, iteration_rows, wave_rows, summary)
    if not args.no_open_dashboard and bool(runtime.get("auto_open_dashboard", True)):
        webbrowser.open(dashboard.resolve().as_uri())
    return dashboard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train the mfg_flow_shop simulation-based ADP policy.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--worker-counts", nargs="*", type=int, default=None)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--initial-random-episodes", type=int, default=None)
    parser.add_argument("--policy-iterations", type=int, default=None)
    parser.add_argument("--episodes-per-iteration", type=int, default=None)
    parser.add_argument("--validation-episodes-per-worker", type=int, default=None)
    parser.add_argument("--rollout-processes", type=int, default=None)
    parser.add_argument("--no-open-dashboard", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    dashboard = train(args.config.resolve(), args)
    print(str(dashboard.resolve()))


if __name__ == "__main__":
    main()
