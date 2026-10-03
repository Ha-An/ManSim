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
from typing import Any, Callable

import simpy

from manufacturing_sim import __version__ as mansim_version
from hydra import compose, initialize_config_dir
from omegaconf import OmegaConf

from agents.factory import build_decision_module
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld
from runtime.compat import build_legacy_experiment_cfg

from .checkpoint import checkpoint_fingerprint, save_checkpoint
from .compact import (
    CompactMCBatch,
    CompactPairwiseMCBatch,
    compact_episode_transitions,
    merge_compact_batches,
    merge_pairwise_mc_batches,
    select_compact_samples,
    stratified_episode_split,
)
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
    beam_value_entropy_avg: float = 0.0
    beam_value_entropy_decision_count: int = 0
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
    probe_target_post_state: dict[str, Any] | None = None
    value_validation_samples: list[dict[str, Any]] | None = None
    simulation_end_min: float = 0.0
    termination_reason: str = ""
    greedy_mc_sample_count: int = 0
    greedy_mc_squared_error_sum: float = 0.0
    greedy_mc_error_sum: float = 0.0
    greedy_mc_prediction_sum: float = 0.0
    greedy_mc_target_sum: float = 0.0


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


def _payload_hash(value: Any) -> str:
    """Return a stable digest for nested optimizer/checkpoint state."""

    torch = require_torch()
    digest = hashlib.sha256()

    def update(current: Any) -> None:
        if torch.is_tensor(current):
            tensor = current.detach().cpu().contiguous()
            digest.update(b"tensor")
            digest.update(str(tensor.dtype).encode("ascii"))
            digest.update(str(tuple(tensor.shape)).encode("ascii"))
            buffer = io.BytesIO()
            torch.save(tensor, buffer)
            digest.update(buffer.getvalue())
            return
        if isinstance(current, dict):
            digest.update(b"dict")
            for key in sorted(current, key=lambda item: repr(item)):
                update(key)
                update(current[key])
            return
        if isinstance(current, (list, tuple)):
            digest.update(type(current).__name__.encode("ascii"))
            for item in current:
                update(item)
            return
        digest.update(type(current).__name__.encode("ascii", errors="replace"))
        digest.update(repr(current).encode("utf-8"))

    update(value)
    return digest.hexdigest()[:16]


def _should_accept_candidate(
    *,
    enabled: bool,
    iteration: int,
    candidate_mean: float,
    incumbent_mean: float | None,
    min_improvement: float,
) -> bool:
    if not enabled or int(iteration) == 0 or incumbent_mean is None:
        return True
    return float(candidate_mean) > float(incumbent_mean) + float(min_improvement)


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
    probe_index = int(adp_cfg.get("_probe_target_decision_number", 0)) - 1
    target_post_state = None
    if probe_index >= 0:
        if probe_index >= len(transitions):
            raise RuntimeError("Counterfactual episode did not reach the target decision.")
        from .schema import serialize_state

        target_post_state = serialize_state(transitions[probe_index]["post_state"])
    compact_transitions = transitions
    if collect_compact_samples and adp_cfg.get("_probe_collect_target_only", False):
        if probe_index < 0 or float(gamma) != 1.0:
            raise ValueError("Counterfactual target-only sampling requires a target decision and gamma=1.")
        compact_transitions = [
            {
                **transitions[probe_index],
                "raw_reward": sum(float(row["raw_reward"]) for row in transitions[probe_index:]),
            }
        ]
    compact_batch = (
        compact_episode_transitions(
            compact_transitions,
            episode_id=episode,
            worker_count=worker_count,
            gamma=gamma,
        )
        if collect_compact_samples
        else None
    )
    if compact_batch is not None and adp_cfg.get("_collect_td_replay", False):
        from .td import CompactTDData

        compact_batch.td_data = CompactTDData.from_transitions(
            transitions, episode, worker_count, repair_capacity=int(world.max_repair_agents)
        )
    greedy_mc = (0, 0.0, 0.0, 0.0, 0.0)
    if not collect_compact_samples and adp_cfg.get("_collect_td_replay", False):
        from .td import greedy_mc_errors

        greedy_mc = greedy_mc_errors(model, transitions, device)
    value_validation_samples = None
    if adp_cfg.get("_value_validation_predictions", False):
        from .value_validation import score_episode

        value_validation_samples = score_episode(model, transitions, device)
    result = EpisodeResult(
        episode=episode,
        phase=phase,
        worker_count=worker_count,
        seed=seed,
        products=int(world.product_count),
        scrap=int(world.scrap_count),
        raw_return=raw_return,
        decisions=int(world.adp_coordinator.metrics["decision_count"]),
        fingerprint=checkpoint_fingerprint(
            world, return_estimator="n_step_td" if adp_cfg.get("_collect_td_replay", False) else "monte_carlo"
        ),
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
        beam_value_entropy_avg=(
            float(world.adp_coordinator.metrics["beam_value_entropy_sum"])
            / max(
                1,
                int(
                    world.adp_coordinator.metrics[
                        "beam_value_entropy_decision_count"
                    ]
                ),
            )
        ),
        beam_value_entropy_decision_count=int(
            world.adp_coordinator.metrics["beam_value_entropy_decision_count"]
        ),
        compact_sample_count=len(compact_batch) if compact_batch is not None else 0,
        compact_memory_bytes=compact_batch.memory_bytes if compact_batch is not None else 0,
        probe_records=list(world.adp_coordinator.probe_records),
        probe_target_product_count=world.adp_coordinator.probe_target_product_count,
        probe_id=str(adp_cfg.get("_probe_id", "") or ""),
        probe_candidate_id=int(adp_cfg.get("_probe_candidate_id", -1)),
        probe_target_post_state=target_post_state,
        value_validation_samples=value_validation_samples,
        simulation_end_min=float(env.now),
        termination_reason=str(world.termination_reason),
        greedy_mc_sample_count=greedy_mc[0],
        greedy_mc_squared_error_sum=greedy_mc[1],
        greedy_mc_error_sum=greedy_mc[2],
        greedy_mc_prediction_sum=greedy_mc[3],
        greedy_mc_target_sum=greedy_mc[4],
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
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[EpisodeResult], CompactMCBatch, RolloutBatchMetrics]:
    if not jobs:
        return [], merge_compact_batches([]), RolloutBatchMetrics()
    max_processes = max(1, int(process_count))
    chunks = _wave_chunks(jobs, min(max_processes, max(1, int(wave_size))))
    all_results: list[EpisodeResult] = []
    compact_episodes: list[CompactMCBatch] = []
    batch_started = time.perf_counter()
    from .live import TrainingProgress
    report_progress = progress_callback or TrainingProgress(output_dir).rollout
    context = multiprocessing.get_context(start_method)
    with ProcessPoolExecutor(
        max_workers=max_processes,
        mp_context=context,
        initializer=_initialize_rollout_process,
        initargs=(model_state, model_cfg, torch_threads),
    ) as executor:
        for wave_jobs in chunks:
            wave_started = time.perf_counter()
            progress_event = {
                "phase": wave_jobs[0].phase, "iteration": wave_jobs[0].iteration,
                "wave_id": wave_jobs[0].wave_id, "wave_episode_count": len(wave_jobs),
                "process_count": max_processes,
            }
            report_progress({**progress_event, "event": "wave_started", "wave_completed": 0,
                             "completed_episode_count": len(episode_rows)})
            futures = {executor.submit(_execute_rollout_job, job): job for job in wave_jobs}
            wave_results: list[EpisodeResult] = []
            wave_compact: list[CompactMCBatch] = []
            try:
                for future in as_completed(futures):
                    result, compact = future.result()
                    wave_results.append(result)
                    if compact is not None:
                        wave_compact.append(compact)
                    report_progress({**progress_event, "event": "episode_completed",
                                     "wave_completed": len(wave_results),
                                     "completed_episode_count": len(episode_rows) + len(wave_results)})
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
                    f"ADP rollout wave {wave_jobs[0].wave_id} failed; no partial value update was applied."
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
            report_progress({**progress_event, "event": "wave_completed",
                             "wave_completed": len(wave_results),
                             "completed_episode_count": len(episode_rows)})
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


def _weighted_mc_regression_loss(
    predictions: Any,
    targets: Any,
    sample_weights: Any | None = None,
) -> Any:
    if sample_weights is None:
        return _mc_regression_loss(predictions, targets)
    torch = require_torch()
    weights = sample_weights.to(device=predictions.device, dtype=predictions.dtype)
    if weights.ndim != 1 or weights.shape != predictions.shape:
        raise ValueError("MC sample weights must be a vector matching predictions.")
    if not bool(torch.isfinite(weights).all()) or bool((weights <= 0).any()):
        raise ValueError("MC sample weights must be finite and strictly positive.")
    squared_error = (predictions - targets) ** 2
    return (squared_error * weights).sum() / weights.sum()


def build_short_replay_batch(
    history: list[tuple[int, CompactMCBatch]],
    *,
    window_iterations: int,
    latest_iteration_weight: float,
) -> tuple[CompactMCBatch, Any, dict[str, Any]]:
    """Merge a bounded compact MC window and up-weight only its newest wave."""
    torch = require_torch()
    if not history:
        raise ValueError("Short replay requires at least one compact batch.")
    if int(window_iterations) < 1:
        raise ValueError("replay_window_iterations must be at least one.")
    if not math.isfinite(float(latest_iteration_weight)) or float(latest_iteration_weight) < 1.0:
        raise ValueError("latest_iteration_weight must be finite and at least one.")
    selected = history[-int(window_iterations) :]
    iterations = [int(iteration) for iteration, _ in selected]
    if iterations != sorted(set(iterations)):
        raise ValueError("Replay history iterations must be unique and increasing.")
    batch = merge_compact_batches([item for _, item in selected])
    weights = torch.ones((len(batch),), dtype=torch.float32)
    latest_iteration = iterations[-1]
    offset = 0
    current_sample_count = 0
    for iteration, item in selected:
        end = offset + len(item)
        if int(iteration) == latest_iteration:
            weights[offset:end] = float(latest_iteration_weight)
            current_sample_count = len(item)
        offset = end
    effective_sample_count = float(weights.sum().item())
    return batch, weights, {
        "replay_iteration_count": len(selected),
        "replay_oldest_iteration": iterations[0],
        "replay_newest_iteration": iterations[-1],
        "replay_episode_count": batch.episode_count,
        "replay_sample_count": len(batch),
        "replay_current_sample_count": current_sample_count,
        "replay_effective_sample_count": effective_sample_count,
        "replay_compact_mib": batch.memory_bytes / (1024.0**2),
        "latest_iteration_weight": float(latest_iteration_weight),
    }


def build_short_pairwise_replay_batch(
    history: list[tuple[int, CompactPairwiseMCBatch]],
    *,
    window_iterations: int,
    latest_iteration_weight: float,
) -> tuple[CompactPairwiseMCBatch, Any]:
    """Merge the bounded same-state action-pair window with replay weights."""

    torch = require_torch()
    selected = [
        (int(iteration), batch)
        for iteration, batch in history[-max(1, int(window_iterations)) :]
        if len(batch)
    ]
    if not selected:
        return merge_pairwise_mc_batches([]), torch.zeros((0,), dtype=torch.float32)
    iterations = [iteration for iteration, _ in selected]
    if iterations != sorted(set(iterations)):
        raise ValueError("Pairwise replay history iterations must be unique and increasing.")
    batch = merge_pairwise_mc_batches([item for _, item in selected])
    weights = torch.ones((len(batch),), dtype=torch.float32)
    latest_iteration = iterations[-1]
    offset = 0
    for iteration, item in selected:
        end = offset + len(item)
        if iteration == latest_iteration:
            weights[offset:end] = float(latest_iteration_weight)
        offset = end
    return batch, weights


def _pairwise_mc_advantage_loss(
    model: Any,
    samples: CompactPairwiseMCBatch,
    indices: list[int],
    device: Any,
    sample_weights: Any | None = None,
) -> Any:
    torch = require_torch()
    selected_inputs, alternative_inputs, target_advantage = samples.model_batch(
        indices, device
    )
    predicted_advantage = model(**selected_inputs) - model(**alternative_inputs)
    if sample_weights is None:
        return torch.nn.functional.mse_loss(predicted_advantage, target_advantage)
    index = torch.as_tensor(indices, dtype=torch.long)
    weights = sample_weights.index_select(0, index).to(
        device=predicted_advantage.device,
        dtype=predicted_advantage.dtype,
    )
    squared_error = (predicted_advantage - target_advantage) ** 2
    return (squared_error * weights).sum() / weights.sum()


def pairwise_mc_advantage_diagnostics(
    model: Any,
    samples: CompactPairwiseMCBatch | None,
    *,
    device: Any,
    batch_size: int,
) -> dict[str, float | int]:
    if samples is None or not len(samples):
        return {
            "pair_count": 0,
            "mse": 0.0,
            "mae": 0.0,
            "sign_accuracy": 0.0,
            "informative_pair_count": 0,
        }
    torch = require_torch()
    squared_error_sum = 0.0
    absolute_error_sum = 0.0
    sign_match_count = 0
    informative_count = 0
    model.eval()
    with torch.no_grad():
        for start in range(0, len(samples), max(1, int(batch_size))):
            indices = list(range(start, min(start + max(1, int(batch_size)), len(samples))))
            selected_inputs, alternative_inputs, targets = samples.model_batch(
                indices, device
            )
            predictions = model(**selected_inputs) - model(**alternative_inputs)
            errors = predictions - targets
            squared_error_sum += float((errors**2).sum().item())
            absolute_error_sum += float(errors.abs().sum().item())
            informative = targets != 0
            informative_count += int(informative.sum().item())
            sign_match_count += int(
                ((torch.sign(predictions) == torch.sign(targets)) & informative).sum().item()
            )
    return {
        "pair_count": len(samples),
        "mse": squared_error_sum / len(samples),
        "mae": absolute_error_sum / len(samples),
        "sign_accuracy": sign_match_count / informative_count if informative_count else 0.0,
        "informative_pair_count": informative_count,
    }


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
    counterfactual_samples: CompactMCBatch | None = None,
    counterfactual_fraction: float = 0.1,
    sample_weights: Any | None = None,
    pairwise_samples: CompactPairwiseMCBatch | None = None,
    pairwise_loss_weight: float = 0.0,
    pairwise_sample_weights: Any | None = None,
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
    if sample_weights is not None:
        sample_weights = sample_weights.detach().cpu().to(dtype=torch.float32)
        if sample_weights.ndim != 1 or len(sample_weights) != len(samples):
            raise ValueError("sample_weights must match the compact MC batch.")
        if not bool(torch.isfinite(sample_weights).all()) or bool((sample_weights <= 0).any()):
            raise ValueError("sample_weights must be finite and strictly positive.")
    if not math.isfinite(float(pairwise_loss_weight)) or float(pairwise_loss_weight) < 0.0:
        raise ValueError("pairwise_loss_weight must be finite and non-negative.")
    pairwise_train_indices: list[int] = []
    if pairwise_samples is not None and len(pairwise_samples):
        if float(pairwise_loss_weight) <= 0.0:
            raise ValueError("Pairwise MC samples require a positive pairwise_loss_weight.")
        train_episode_ids = {int(samples.episode_ids[index]) for index in train_indices}
        pairwise_train_indices = [
            index
            for index, episode_id in enumerate(
                pairwise_samples.selected.episode_ids.tolist()
            )
            if int(episode_id) in train_episode_ids
        ]
        if not pairwise_train_indices:
            raise ValueError("No pairwise MC samples belong to training episodes.")
        if pairwise_sample_weights is not None:
            pairwise_sample_weights = pairwise_sample_weights.detach().cpu().to(
                dtype=torch.float32
            )
            if (
                pairwise_sample_weights.ndim != 1
                or len(pairwise_sample_weights) != len(pairwise_samples)
            ):
                raise ValueError("pairwise_sample_weights must match the pair batch.")
            if not bool(torch.isfinite(pairwise_sample_weights).all()) or bool(
                (pairwise_sample_weights <= 0).any()
            ):
                raise ValueError(
                    "pairwise_sample_weights must be finite and strictly positive."
                )
    counterfactual_indices: list[int] = []
    counterfactual_rng = random.Random()
    counterfactual_rng.setstate(rng.getstate())
    if counterfactual_samples is not None and len(counterfactual_samples):
        if not 0.0 < counterfactual_fraction < 1.0:
            raise ValueError("counterfactual_fraction must be strictly between zero and one.")
        train_episode_ids = {int(samples.episode_ids[index]) for index in train_indices}
        counterfactual_indices = [
            len(samples) + index
            for index, episode_id in enumerate(counterfactual_samples.episode_ids.tolist())
            if int(episode_id) in train_episode_ids
        ]
        if not counterfactual_indices:
            raise ValueError("No counterfactual samples belong to training episodes.")
        samples = merge_compact_batches([samples, counterfactual_samples])
        if sample_weights is not None:
            sample_weights = torch.cat(
                [sample_weights, torch.ones((len(counterfactual_samples),), dtype=torch.float32)]
            )
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
            if counterfactual_indices and len(batch_indices) >= 2:
                count = min(len(batch_indices) - 1, max(1, round(len(batch_indices) * counterfactual_fraction)))
                # Keep the original number of SGD steps and holdout episodes in both arms.
                batch_indices = batch_indices[:-count] + counterfactual_rng.choices(
                    counterfactual_indices, k=count
                )
            batch, targets = samples.model_batch(batch_indices, device)
            predictions = model(**batch)
            batch_weights = (
                sample_weights.index_select(
                    0, torch.as_tensor(batch_indices, dtype=torch.long)
                ).to(device)
                if sample_weights is not None
                else None
            )
            loss = _weighted_mc_regression_loss(predictions, targets, batch_weights)
            if pairwise_train_indices:
                pair_count = min(max(1, int(batch_size)), len(pairwise_train_indices))
                pair_indices = (
                    list(pairwise_train_indices)
                    if pair_count == len(pairwise_train_indices)
                    else rng.sample(pairwise_train_indices, pair_count)
                )
                pair_loss = _pairwise_mc_advantage_loss(
                    model,
                    pairwise_samples,
                    pair_indices,
                    device,
                    pairwise_sample_weights,
                )
                loss = loss + float(pairwise_loss_weight) * pair_loss
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
                evaluation_weights = (
                    sample_weights.index_select(
                        0, torch.as_tensor(batch_indices, dtype=torch.long)
                    ).to(device)
                    if sample_weights is not None
                    else None
                )
                batch_weight = (
                    float(evaluation_weights.sum().item())
                    if evaluation_weights is not None
                    else float(len(batch_indices))
                )
                batch_loss = float(
                    _weighted_mc_regression_loss(
                        model(**batch), targets, evaluation_weights
                    ).item()
                )
                weighted_loss_sum += batch_loss * batch_weight
                evaluated_sample_count += batch_weight
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
    series_x_values: dict[str, list[float]] | None = None,
    x_label: str,
    y_label: str,
    include_zero: bool = False,
    error_ranges: dict[str, tuple[list[float | None], list[float | None]]] | None = None,
    dashed_series: set[str] | None = None,
    width: int = 720,
    height: int = 250,
) -> str:
    non_empty = [(name, values, color) for name, values, color in series if values]
    if not non_empty:
        return "<div class='empty'>No data</div>"

    point_count = max(len(values) for _, values, _ in non_empty)
    default_xs = list(x_values or range(point_count))
    if len(default_xs) < point_count:
        default_xs.extend(float(index) for index in range(len(default_xs), point_count))
    default_xs = [float(value) for value in default_xs]
    xs_by_series: dict[str, list[float]] = {}
    for name, values, _ in non_empty:
        configured_xs = (series_x_values or {}).get(name)
        current_xs = list(configured_xs if configured_xs is not None else default_xs)
        if len(current_xs) < len(values):
            current_xs.extend(
                float(index) for index in range(len(current_xs), len(values))
            )
        xs_by_series[name] = [float(value) for value in current_xs[: len(values)]]
    all_x = [value for values in xs_by_series.values() for value in values]
    all_y = [float(value) for _, values, _ in non_empty for value in values]
    for name, _, _ in non_empty:
        if error_ranges and name in error_ranges:
            lows, highs = error_ranges[name]
            all_y.extend(float(value) for value in lows if value is not None)
            all_y.extend(float(value) for value in highs if value is not None)
    low = min(all_y)
    high = max(all_y)
    if include_zero:
        low = min(0.0, low)
        high = max(0.0, high)
    if math.isclose(low, high, abs_tol=1e-12):
        padding = max(1.0, abs(low) * 0.1)
        low -= padding
        high += padding

    x_low = min(all_x)
    x_high = max(all_x)
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
    unique_xs = sorted(set(all_x))
    x_tick_count = min(6, len(unique_xs))
    x_tick_indexes = sorted(
        {
            int(round(index * (len(unique_xs) - 1) / max(1, x_tick_count - 1)))
            for index in range(x_tick_count)
        }
    )
    for index in x_tick_indexes:
        value = unique_xs[index]
        x = map_x(value)
        x_grid.append(
            f"<line x1='{x:.1f}' y1='{margin_top:.1f}' x2='{x:.1f}' y2='{height - margin_bottom:.1f}' class='grid-line'/>"
            f"<text x='{x:.1f}' y='{height - margin_bottom + 20:.1f}' class='tick x-tick'>{html_lib.escape(_format_axis_tick(value))}</text>"
        )

    lines: list[str] = []
    legend: list[str] = []
    for name, values, color in non_empty:
        series_xs = xs_by_series[name]
        coordinates = [
            (map_x(series_xs[index]), map_y(float(value)), float(value))
            for index, value in enumerate(values)
        ]
        if error_ranges and name in error_ranges:
            lows, highs = error_ranges[name]
            for index in range(min(len(coordinates), len(lows), len(highs))):
                if lows[index] is None or highs[index] is None:
                    continue
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
    if summary.get("return_estimator") == "n_step_td":
        from .td_dashboard import render_td_dashboard

        return render_td_dashboard(output_dir, episode_rows, iteration_rows, wave_rows, summary)
    from .value_validation import dashboard_panels

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
        if str(row.get("phase", ""))
        in {"checkpoint_validation", "screening_validation", "candidate_validation"}
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

    def weighted_beam_entropy(rows: list[dict[str, Any]]) -> float | None:
        weighted_sum = 0.0
        sample_count = 0
        for row in rows:
            count = int(float(row.get("beam_value_entropy_decision_count", 0) or 0))
            if count <= 0:
                continue
            weighted_sum += float(row.get("beam_value_entropy_avg", 0.0) or 0.0) * count
            sample_count += count
        return weighted_sum / sample_count if sample_count else None

    training_entropy_x: list[float] = []
    training_entropy_values: list[float] = []
    for iteration in training_iterations:
        rows = [
            row
            for row in training_episode_rows
            if int(row.get("iteration", 0)) == iteration
        ]
        value = weighted_beam_entropy(rows)
        if value is not None:
            training_entropy_x.append(float(iteration))
            training_entropy_values.append(float(value))
    validation_entropy_x: list[float] = []
    validation_entropy_values: list[float] = []
    for iteration in checkpoint_wait_iterations:
        rows = [
            row
            for row in checkpoint_wait_rows
            if int(row.get("iteration", 0)) == iteration
        ]
        value = weighted_beam_entropy(rows)
        if value is not None:
            validation_entropy_x.append(float(iteration))
            validation_entropy_values.append(float(value))
    exploratory_checkpoint_mean: list[float] = []
    exploratory_checkpoint_std: list[float] = []
    exploratory_checkpoint_x: list[float] = []
    for rollout_iteration in training_iterations:
        rows = [
            row
            for row in training_episode_rows
            if int(row.get("iteration", 0)) == rollout_iteration
        ]
        mean, std = _mean_and_std([float(row.get("products", 0)) for row in rows])
        exploratory_checkpoint_x.append(float(rollout_iteration))
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
    replay_scope_value = str(summary.get("replay_scope", "current_iteration"))
    recent_replay_enabled = replay_scope_value == "recent_window"
    replay_intro = (
        "현재 rollout과 제한된 최근 Monte Carlo replay를 함께 학습합니다. "
        "Replay window를 벗어난 compact episode tensor는 즉시 폐기합니다."
        if recent_replay_enabled
        else "현재 rollout의 Monte Carlo batch만 학습하며, 업데이트 후 compact episode "
        "tensor를 폐기합니다."
    )
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
    pairwise_enabled = bool(summary.get("pairwise_mc_advantage_enabled", False))
    pairwise_mse_before = [
        float(row.get("pairwise_mc_advantage_mse_before", 0.0))
        for row in iteration_rows
    ]
    pairwise_mse_after = [
        float(row.get("pairwise_mc_advantage_mse_after", 0.0))
        for row in iteration_rows
    ]
    pairwise_sign_accuracy = [
        100.0 * float(row.get("pairwise_mc_advantage_sign_accuracy", 0.0))
        for row in iteration_rows
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
    validation_rows = [
        row
        for row in iteration_rows
        if _as_bool(row.get("validation_performed", False))
    ]
    validation = [float(row.get("validation_products_avg", 0)) for row in validation_rows]
    validation_x = [float(row.get("iteration", index)) for index, row in enumerate(validation_rows)]
    validation_ci_low = [float(row.get("validation_products_ci95_low", 0)) for row in validation_rows]
    validation_ci_high = [float(row.get("validation_products_ci95_high", 0)) for row in validation_rows]
    conservative_update_enabled = _as_bool(
        summary.get("conservative_policy_update", False)
    )
    gate_rows = [
        row
        for row in validation_rows
        if str(row.get("candidate_validation_products_avg", "")).strip()
    ]
    gate_x = [float(row.get("iteration", index)) for index, row in enumerate(gate_rows)]
    gate_candidate = [
        float(row.get("candidate_validation_products_avg", 0.0)) for row in gate_rows
    ]
    gate_effective = [
        float(row.get("effective_incumbent_validation_products_avg", 0.0))
        for row in gate_rows
    ]
    compact_memory = [float(row.get("compact_batch_mib", 0)) for row in iteration_rows]
    replay_memory = [float(row.get("replay_buffer_mib", 0)) for row in iteration_rows]
    replay_episode_counts = [
        float(row.get("replay_episode_count", 0)) for row in iteration_rows
    ]
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
        phase_waves.get("checkpoint_diagnostic", 0)
        or phase_waves.get("candidate_validation", 0)
        or phase_waves.get("screening_validation", 0)
        or phase_waves.get("validation", 0)
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
            ("Best Validation Products", f"{float(summary.get('best_validation_completed_products_avg', 0)):.3f}"),
            ("Best Iteration", summary.get("best_iteration", "-")),
            (
                "Value Updates (incl. initial)",
                summary.get(
                    "value_update_count",
                    int(summary.get("policy_iterations", 0)) + 1,
                ),
            ),
            ("Policy Iterations", summary.get("policy_iterations", "-")),
            ("Episodes / Update", summary.get("episodes_per_iteration", "-")),
            ("MC Loss", loss_label),
            (
                "Pairwise MC Advantage",
                "Enabled" if pairwise_enabled else "Disabled",
            ),
            (
                "Pairwise Loss Weight",
                f"{float(summary.get('pairwise_mc_advantage_loss_weight', 0.0)):.2f}",
            ),
            (
                "Pairwise Samples / Branch Episodes",
                f"{int(summary.get('pairwise_mc_advantage_pair_count', 0))} / "
                f"{int(summary.get('pairwise_mc_advantage_branch_episode_count', 0))}",
            ),
            ("Reward", "Completed-product MC"),
            ("WAIT Action", "Enabled" if wait_action_enabled else "Disabled"),
            (
                "Supported Worker Counts",
                ", ".join(str(value) for value in summary.get("worker_counts", [])),
            ),
            ("Beam Worker Order", summary.get("worker_order_strategy", "-")),
            ("Policy Update", summary.get("policy_update_mode", "always_accept")),
            ("Accepted Updates", summary.get("accepted_update_count", 0)),
            ("Rejected Updates", summary.get("rejected_update_count", 0)),
            ("Learning Rate", f"{float(summary.get('learning_rate', 0)):.1e}"),
            ("Max Epochs / Update", summary.get("max_epochs_per_iteration", "-")),
            ("Replay Scope", replay_scope_value),
            ("Replay Window", f"{int(summary.get('replay_window_iterations', 1))} updates"),
            (
                "Latest Wave Weight",
                f"{float(summary.get('latest_iteration_weight', 1.0)):.1f}x",
            ),
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
                "Peak Replay Buffer",
                f"{float(summary.get('peak_replay_buffer_mib', 0)):.2f} MiB",
            ),
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
            (
                "Checkpoint",
                f"{Path(str(summary.get('best_checkpoint', 'best.pt'))).name} "
                f"(iteration {summary.get('best_iteration', '-')})",
            ),
        ]
    )
    def screening_cell(row: dict[str, Any]) -> str:
        if not _as_bool(row.get("validation_performed", False)):
            return "-"
        return (
            f"{float(row.get('validation_products_avg', 0)):.3f} "
            f"[{float(row.get('validation_products_ci95_low', 0)):.3f}, "
            f"{float(row.get('validation_products_ci95_high', 0)):.3f}]"
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
        f"<td>{int(row.get('replay_oldest_iteration', row.get('iteration', 0)))}-"
        f"{int(row.get('replay_newest_iteration', row.get('iteration', 0)))}</td>"
        f"<td>{int(row.get('replay_episode_count', row.get('batch_episode_count', 0)))}</td>"
        f"<td>{int(row.get('replay_sample_count', row.get('sample_count', 0)))} "
        f"({float(row.get('replay_effective_sample_count', row.get('sample_count', 0))):.0f})</td>"
        f"<td>{float(row.get('replay_buffer_mib', row.get('compact_batch_mib', 0))):.3f}</td>"
        f"<td>{float(row.get('rollout_wall_sec', 0)):.2f}s</td>"
        f"<td>{update_time_cell(row)}</td>"
        f"<td>{fit_cell(row, 'mc_mae')}</td>"
        f"<td>{fit_cell(row, 'mc_rmse')}</td>"
        f"<td>{int(row.get('pairwise_mc_advantage_pair_count', 0))}</td>"
        f"<td>{float(row.get('pairwise_mc_advantage_mse_after', 0)):.3f}</td>"
        f"<td>{100.0 * float(row.get('pairwise_mc_advantage_sign_accuracy', 0)):.1f}%</td>"
        f"<td>{float(row.get('epsilon', 0)):.3f}</td>"
        f"<td>{ood_cell(row, 'ood_selection_rate', percent=True)}</td>"
        f"<td>{ood_cell(row, 'ood_overestimation_excess')}</td>"
        f"<td>{ood_cell(row, 'candidate_validation_products_avg')}</td>"
        f"<td>{ood_cell(row, 'effective_incumbent_validation_products_avg')}</td>"
        f"<td>{html_lib.escape(str(row.get('policy_update_accepted', '-')))}</td>"
        f"<td>{screening_cell(row)}</td>"
        "</tr>"
        for row in iteration_rows
    )
    iteration_table = (
        "<table><thead><tr><th>Update</th><th>Current episodes</th><th>Current MC samples</th>"
        "<th>Current MiB</th><th>Replay span</th><th>Replay episodes</th>"
        "<th>Replay samples (weighted)</th><th>Replay MiB</th>"
        "<th>Rollout</th><th>GPU update</th><th>MAE</th><th>RMSE</th>"
        "<th>Pair count</th><th>Pair MSE</th><th>Pair sign accuracy</th><th>Epsilon</th>"
        "<th>OOD 선택률</th><th>OOD 추가 과대평가</th>"
        "<th>Candidate validation</th><th>Effective incumbent</th><th>Accepted</th>"
        "<th>Validation mean [95% CI]</th></tr></thead><tbody>"
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
        f"<tr><th>Training seed / deterministic</th><td>{html_lib.escape(str(device_meta.get('determinism', {}).get('seed', '-')))} / {('yes' if device_meta.get('determinism', {}).get('torch_deterministic_algorithms', False) else 'no')}</td></tr>"
        f"<tr><th>GPU count / compute capability</th><td>{int(device_meta.get('gpu_count', 0))} / {html_lib.escape(str(device_meta.get('gpu_compute_capability', '-')))}</td></tr>"
        f"<tr><th>CPU / logical processors</th><td>{html_lib.escape(str(environment_meta.get('cpu_model', '-')))} / {int(environment_meta.get('logical_cpu_count', 0))}</td></tr>"
        f"<tr><th>Host / OS</th><td>{html_lib.escape(str(environment_meta.get('host_name', '-')))} / {html_lib.escape(str(environment_meta.get('operating_system', '-')))}</td></tr>"
        f"<tr><th>Python</th><td>{html_lib.escape(str(environment_meta.get('python_version', '-')))}</td></tr>"
        f"<tr><th>Torch threads per process</th><td>{int(summary.get('torch_threads_per_process', 0))}</td></tr>"
        f"<tr><th>Initial / policy / checkpoint diagnostic / final waves</th><td>{int(phase_waves.get('initial_random', 0))} / {int(phase_waves.get('policy_iteration', 0))} / {checkpoint_diagnostic_waves} / {int(phase_waves.get('final_selection_validation', 0))}</td></tr>"
        f"<tr><th>Pairwise counterfactual waves</th><td>{int(phase_waves.get('pairwise_mc_advantage', 0))}</td></tr>"
        f"<tr><th>Total waves</th><td>{int(summary.get('total_wave_count', 0))}</td></tr>"
        f"<tr><th>Active-slot utilization</th><td>{100.0 * float(summary.get('active_slot_utilization', 0)):.1f}%</td></tr>"
        f"<tr><th>IPC payload</th><td>{float(summary.get('ipc_payload_mib', 0)):.2f} MiB</td></tr>"
        f"<tr><th>Failed / cancelled / retried</th><td>{int(summary.get('failed_wave_count', 0))} / {int(summary.get('cancelled_episode_count', 0))} / {int(summary.get('retried_episode_count', 0))}</td></tr>"
        "</tbody></table>"
    )
    time_parts = [
        ("고정 행동 probe", float(summary.get("fixed_action_probe_wall_sec", 0)), "#4dc6c6"),
        ("OOD 진단", float(summary.get("ood_diagnostic_sec", 0)), "#ff8b72"),
        (
            "Training rollout",
            float(summary.get("ordinary_training_rollout_sec", summary.get("training_rollout_sec", 0))),
            "#43a5ff",
        ),
        (
            "Pairwise counterfactual rollout",
            float(summary.get("pairwise_mc_advantage_rollout_sec", 0)),
            "#4dc6c6",
        ),
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
            "<table><thead><tr><th>Iteration</th><th>Screen rank</th><th>Validation mean +/- std</th>"
            "<th>Final mean +/- std</th><th>Final 95% CI</th><th>Selected</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td>{int(float(row.get('iteration', 0)))}</td>"
                f"<td>{int(float(row.get('screening_rank', 0)))}</td>"
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
    checkpoint_x = list(range(int(summary.get("policy_iterations", 0)) + 1))
    checkpoint_series: list[tuple[str, list[float], str]] = []
    checkpoint_error_ranges: dict[str, tuple[list[float], list[float]]] = {}
    if validation:
        checkpoint_series.append(("Validation (epsilon=0)", validation, "#d78cff"))
        checkpoint_error_ranges["Validation (epsilon=0)"] = (
            validation_ci_low,
            validation_ci_high,
        )
    if exploratory_checkpoint_mean:
        checkpoint_series.append(
            ("업데이트용 탐색 rollout", exploratory_checkpoint_mean, "#91a3b8")
        )
    checkpoint_series_x = {
        "Validation (epsilon=0)": validation_x,
        "업데이트용 탐색 rollout": exploratory_checkpoint_x,
    }
    fleet_validation_colors = ["#43a5ff", "#72d69b", "#d78cff", "#ffb84d"]
    fleet_validation_series = [
        (
            f"Worker {worker_count}",
            [
                float(row.get(f"validation_products_avg_workers_{worker_count}", 0.0))
                for row in validation_rows
            ],
            fleet_validation_colors[index % len(fleet_validation_colors)],
        )
        for index, worker_count in enumerate(worker_counts)
    ]
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
        "보라색은 지정된 iteration의 checkpoint를 고정 validation seed와 epsilon=0으로 평가한 결과이며 "
        "오차 막대는 95% 신뢰구간입니다. 회색 점선의 iteration 0은 초기 Random Feasible sample, "
        "iteration n은 n번째 가치함수 업데이트에 사용된 탐색 rollout의 평균입니다. "
        f"초기 무작위 정책 기준은 {random_baseline_mean:.3f} +/- {random_baseline_std:.3f}개입니다."
    )
    conservative_gate_panel = (
        _chart_panel(
            "Conservative Update 후보와 채택 정책 생산량",
            _svg_chart(
                [
                    ("Candidate", gate_candidate, "#ff8b72"),
                    ("Effective incumbent", gate_effective, "#72d69b"),
                ],
                x_values=gate_x,
                x_label="가치함수 업데이트",
                y_label=f"{int(summary.get('horizon_days', 5))}일 episode 완료 제품 수",
                include_zero=True,
            ),
            "Candidate가 기존 incumbent보다 높은 validation 생산량을 기록할 때만 다음 rollout 정책으로 채택됩니다. 초록색 선이 하락하지 않으면 gate가 정책 붕괴를 차단한 것입니다.",
            "같은 고정 validation seed와 epsilon=0으로 비교합니다. Candidate 곡선의 하락은 허용되지만, 거부된 candidate는 model과 optimizer 모두 복원되어 다음 rollout에 영향을 주지 않습니다.",
        )
        if conservative_update_enabled and gate_rows
        else ""
    )
    pairwise_advantage_panel = (
        _chart_panel(
            "동일 상태 행동쌍의 Pairwise MC Advantage 오차",
            _svg_chart(
                [
                    ("업데이트 전 Pair MSE", pairwise_mse_before, "#ff8b72"),
                    ("업데이트 후 Pair MSE", pairwise_mse_after, "#72d69b"),
                ],
                x_values=iteration_x,
                x_label="가치함수 업데이트 iteration",
                y_label="MC advantage 차이의 MSE",
                include_zero=True,
            ),
            "같은 pre-decision state에서 실제 선택 행동과 대안 행동을 각각 끝까지 실행한 뒤, "
            "두 Monte Carlo return의 차이를 가치망이 얼마나 정확히 재현하는지 보여줍니다. "
            "초록선이 주황선보다 낮아지면 해당 업데이트가 후보 행동의 상대 순위를 직접 학습했다는 뜻입니다.",
            "이 지표는 절대 미래 생산량 MSE와 별개입니다. MC advantage가 0인 행동쌍은 부호 정확도에서 제외하며, "
            "Pair MSE 감소만으로 held-out 생산성 향상이 보장되지는 않으므로 validation 생산량과 함께 봐야 합니다.",
        )
        if pairwise_enabled
        else ""
    )
    chart_grid = "".join(
        [
            "<div class='chart-section-title'><h2>1. 정책 성능과 미관측 행동 진단</h2>"
            "<p>업데이트 후 생산 성능과, 직전 on-policy 학습 범위를 벗어난 greedy 행동 선택을 먼저 확인합니다.</p></div>",
            _chart_panel(
                "Iteration별 탐색 Rollout 및 Validation 생산량",
                _svg_chart(
                    checkpoint_series,
                    x_values=checkpoint_x,
                    series_x_values=checkpoint_series_x,
                    x_label="Iteration",
                    y_label=f"{int(summary.get('horizon_days', 5))}일 episode 완료 제품 수",
                    include_zero=True,
                    error_ranges=checkpoint_error_ranges,
                    dashed_series={"업데이트용 탐색 rollout"},
                ),
                "회색 탐색 rollout과 이후 checkpoint의 보라색 greedy validation을 같은 iteration 축에서 비교합니다. 보라색이 지속적으로 상승하면 정책 개선, 회색만 높고 보라색이 낮으면 탐색 성능이 greedy 정책으로 이어지지 않은 것입니다.",
                checkpoint_detail,
            ),
            _chart_panel(
                "Beam 후보 가치 엔트로피",
                _svg_chart(
                    [
                        ("탐색 rollout의 greedy 결정", training_entropy_values, "#43a5ff"),
                        ("Validation (epsilon=0)", validation_entropy_values, "#d78cff"),
                    ],
                    x_values=checkpoint_x,
                    series_x_values={
                        "탐색 rollout의 greedy 결정": training_entropy_x,
                        "Validation (epsilon=0)": validation_entropy_x,
                    },
                    x_label="가치함수 업데이트 후 checkpoint",
                    y_label="정규화 엔트로피 (0~1)",
                    include_zero=True,
                ),
                "각 의사결정에서 beam에 남은 공동 할당들의 예측가치를 표준화한 뒤 계산한 Shannon entropy입니다. 1에 가까우면 후보 가치를 비슷하게 보며, 0에 가까우면 한 후보에 강하게 집중합니다.",
                "낮은 엔트로피와 생산량 상승은 유효한 수렴 신호입니다. 낮은 엔트로피와 생산량 하락이 함께 나타나면 잘못된 행동에 대한 과신을 의심해야 합니다. 후보가 하나뿐이거나 random/mandatory 결정인 경우는 집계에서 제외합니다.",
            ),
            conservative_gate_panel,
            _chart_panel(
                "Worker 수별 미사용 Validation 생산량",
                _svg_chart(
                    fleet_validation_series,
                    x_values=validation_x,
                    x_label="가치함수 업데이트 후 checkpoint",
                    y_label=f"{int(summary.get('horizon_days', 5))}일 episode 완료 제품 수",
                    include_zero=True,
                ),
                "동일한 통합 checkpoint를 3대와 5대 환경에서 각각 epsilon=0으로 평가한 평균 생산량입니다. 두 곡선이 함께 개선되는지 확인해 통합학습이 한 fleet에만 치우치지 않았는지 판단합니다.",
                "각 점은 worker 수별 고정 validation seed의 평균입니다. worker 수 사이의 절대 생산량보다 같은 worker 수 곡선의 checkpoint 간 변화를 우선 해석해야 합니다.",
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
            dashboard_panels(output_dir),
            wait_panel,
            "<div class='chart-section-title'><h2>3. 가치함수 학습 상태</h2>"
            + (
                "<p>현재 rollout과 짧은 recent-policy MC replay의 적합도 및 return 분산을 확인합니다. 낮은 오차만으로 좋은 행동 순위를 보장하지는 않습니다.</p></div>"
                if recent_replay_enabled
                else "<p>현재 rollout MC batch의 적합도와 return 분산을 확인합니다. 낮은 오차만으로 좋은 행동 순위를 보장하지는 않습니다.</p></div>"
            ),
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
                    (
                        "최근 replay window를 episode 단위로 나눈 holdout에서 early stopping용 적합 오차를 측정합니다. 현재 wave에는 설정된 추가 가중치를 적용합니다. 아래 MAE/RMSE는 현재 rollout만 평가하므로 표본 범위가 다르며, 낮은 loss만으로 dispatch 정책 개선을 증명할 수는 없습니다."
                        if recent_replay_enabled
                        else "현재 rollout batch의 episode 단위 holdout에서 early stopping용 적합 오차를 측정합니다. 아래 MAE/RMSE와 표본 범위가 달라 제곱값이 정확히 일치하지 않으며, 낮은 loss만으로 dispatch 정책 개선을 증명할 수는 없습니다."
                    )
                    if fit_diagnostics_available
                    else "중단된 실행에서 업데이트별 loss가 보존되지 않았습니다. 누락값을 0으로 해석하면 안 됩니다."
                ),
            ),
            pairwise_advantage_panel,
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
                    "업데이트 후 현재 rollout batch 전체(학습 표본 포함)의 MAE/RMSE입니다. 독립 greedy validation 오차가 아닙니다. iteration마다 표본과 후속 정책이 바뀌므로 단순 감소만으로 수렴을 판단하지 마세요."
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
                    "현재 batch 전체의 예측·목표 표준편차입니다. 상수 예측이나 규모 차이를 감지하지만 산포가 같아도 개별 예측과 행동 순위는 틀릴 수 있습니다."
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
                "해당 iteration에서 새로 수집한 current rollout만의 compact tensor 크기입니다.",
            ),
            _chart_panel(
                "Short Replay 보관 규모",
                _svg_chart(
                    [("Replay episodes", replay_episode_counts, "#72d69b")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="보관 episode 수",
                    include_zero=True,
                ),
                "현재 wave와 직전 wave들을 합쳐 실제 가치망 업데이트에 사용한 bounded replay 규모입니다. 설정된 window에 도달한 뒤에는 일정하게 유지되어야 합니다.",
                "과거 표본은 수집 당시 정책의 complete MC return을 유지합니다. 이는 target network나 TD replay가 아니며, window 밖으로 밀려난 compact batch는 즉시 해제합니다.",
            ),
            _chart_panel(
                "Short Replay 메모리",
                _svg_chart(
                    [("Replay buffer", replay_memory, "#4dc6c6")],
                    x_values=iteration_x,
                    x_label="정책 업데이트",
                    y_label="보관 메모리(MiB)",
                    include_zero=True,
                ),
                "최근 replay window가 parent process에서 차지하는 compact tensor 메모리입니다. Window가 찬 뒤 지속적으로 증가하면 FIFO 해제 누락을 의심해야 합니다.",
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
<style>body{{margin:0;background:#08111f;color:#e8f1ff;font:14px Segoe UI,Arial;overflow-x:hidden}}main{{max-width:1480px;min-width:0;margin:auto;padding:28px}}h1{{letter-spacing:0;overflow-wrap:anywhere}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.card,.panel{{min-width:0;border:1px solid #294567;background:#101d31;border-radius:6px;padding:16px}}.wide-table{{overflow-x:auto}}.warning{{margin:12px 0;padding:14px;border:1px solid #b88932;background:#2b2312;color:#ffe4a3;border-radius:6px;overflow-wrap:anywhere}}.card span{{display:block;color:#8fb2de}}.card strong{{font-size:20px;overflow-wrap:anywhere}}.grid{{min-width:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:14px}}.chart-section-title{{grid-column:1/-1;border-top:1px solid #294567;padding:18px 4px 0;margin-top:4px}}.chart-section-title h2{{margin:0 0 6px}}.chart-section-title p{{margin:0;color:#91add0}}.metric-chart{{width:100%;height:auto;aspect-ratio:720/250;background:#0b1728}}.grid-line{{stroke:#223752;stroke-width:1}}.axis-line{{stroke:#86a5c9;stroke-width:1.4}}.tick{{fill:#9eb5d1;font-size:11px}}.x-tick{{text-anchor:middle}}.y-tick{{text-anchor:end}}.axis-label{{fill:#d8e8fa;font-size:13px;font-weight:600}}.x-axis-label,.y-axis-label{{text-anchor:middle}}.point-label{{fill:#ffffff;font-size:12px;font-weight:700;text-anchor:middle}}.chart-legend{{display:flex;gap:14px;flex-wrap:wrap;margin-top:8px;color:#b8cae0}}.chart-legend i{{display:inline-block;width:12px;height:3px;margin:0 6px 3px 0}}.chart-diagnosis{{color:#d5e3f4;line-height:1.5;margin:12px 0 0}}.chart-detail{{color:#91add0;line-height:1.45;margin:6px 0 0}}code{{color:#77d3a8;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #294567;text-align:right}}th:first-child,td:first-child{{text-align:left}}.stack{{height:28px;display:flex;background:#0b1728;overflow:hidden;border-radius:4px}}.stack div{{min-width:2px}}.legend{{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px;color:#a9bfdb}}.legend i{{display:inline-block;width:10px;height:10px;margin-right:5px}}@media(max-width:900px){{main{{padding:18px}}.grid{{grid-template-columns:1fr}}.chart-section-title{{grid-column:1}}.panel{{overflow-x:auto}}.chart-panel{{overflow-x:hidden}}table{{min-width:620px}}}}@media(max-width:600px){{main{{padding:14px}}.cards{{grid-template-columns:1fr}}.card strong{{font-size:18px}}}}</style></head><body><main>
<h1>Simulation-Based ADP 학습 대시보드</h1><p>{replay_intro}</p>
{recovery_banner}<div class='cards'>{cards}</div><div class='grid'>
<section class='panel'><h2>병렬 실행 설정</h2>{parallel_config_table}</section>
<section class='panel'><h2>전체 시간 구성</h2>{time_bar}<p>Compact 병합: {float(summary.get('compact_merge_sec', 0)):.2f}s · IPC/조정 overhead: {float(summary.get('coordination_ipc_overhead_sec', 0)):.2f}s</p>{"<p class='chart-detail'>복구된 실행은 원래 parent wall clock과 GPU update 시간이 없어 저장된 rollout·validation wave 시간만 표시합니다.</p>" if not wall_clock_complete else ""}</section>
{chart_grid}
</div><section class='panel' style='margin-top:14px'><h2>Checkpoint 선정 결과</h2>{checkpoint_table}</section><section class='panel' style='margin-top:14px'><h2>Seed 분할</h2>{seed_table}</section><section class='panel wide-table' style='margin-top:14px'><h2>MC 업데이트 Batch와 Short Replay</h2>{iteration_table}</section><section class='panel' style='margin-top:14px'><h2>Worker 수별 성능</h2>{worker_table}</section><div class='grid'><section class='panel'><h2>가장 느린 Episode</h2>{slow_episode_table}</section><section class='panel'><h2>가장 느린 Wave</h2>{slow_wave_table}</section></div><section class='panel' style='margin-top:14px'><h2>실패한 Wave</h2>{failed_wave_table}</section><p>Feature schema: <code>{FEATURE_SCHEMA_VERSION}</code></p></main></body></html>"""
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


def _as_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n"}:
            return False
    return bool(value)


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
        "simulation_end_min": result.simulation_end_min,
        "termination_reason": result.termination_reason,
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
        "beam_value_entropy_avg": round(result.beam_value_entropy_avg, 6),
        "beam_value_entropy_decision_count": result.beam_value_entropy_decision_count,
        "compact_sample_count": result.compact_sample_count,
        "compact_memory_mib": round(result.compact_memory_bytes / (1024.0**2), 6),
        "elapsed_sec": round(result.elapsed_sec, 6),
        "child_peak_rss_mib": round(result.child_peak_rss_mib, 6),
        "snapshot_hash": result.snapshot_hash,
        "greedy_mc_sample_count": result.greedy_mc_sample_count,
        "greedy_mc_squared_error_sum": result.greedy_mc_squared_error_sum,
        "greedy_mc_error_sum": result.greedy_mc_error_sum,
        "greedy_mc_prediction_sum": result.greedy_mc_prediction_sum,
        "greedy_mc_target_sum": result.greedy_mc_target_sum,
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


def _resolve_screening_iterations(
    *,
    policy_iterations: int,
    interval: int,
    include_initial: bool,
    configured: list[int] | tuple[int, ...] | None = None,
) -> list[int]:
    if configured is None:
        return [
            iteration
            for iteration in range(int(policy_iterations) + 1)
            if _should_validate(
                iteration,
                policy_iterations,
                interval,
                include_initial=include_initial,
            )
        ]
    values = [int(value) for value in configured]
    if len(values) != len(set(values)):
        raise ValueError("validation.screening_iterations must not contain duplicates.")
    if any(value < 0 or value > int(policy_iterations) for value in values):
        raise ValueError(
            "validation.screening_iterations must be within "
            f"0..{int(policy_iterations)}."
        )
    values = sorted(values)
    if bool(include_initial) and 0 not in values:
        raise ValueError(
            "validation.screening_iterations must include 0 when "
            "screening_include_initial=true."
        )
    if int(policy_iterations) not in values:
        raise ValueError(
            "validation.screening_iterations must include the final policy iteration."
        )
    return values


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


def _configure_training_determinism(torch: Any, seed: int) -> dict[str, Any]:
    """Fix parent-process model initialization and CUDA update randomness."""

    resolved_seed = int(seed)
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    random.seed(resolved_seed)
    torch.manual_seed(resolved_seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(resolved_seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.use_deterministic_algorithms(True)
    return {
        "seed": resolved_seed,
        "torch_deterministic_algorithms": True,
        "cudnn_deterministic": True,
        "cudnn_benchmark": False,
        "cublas_workspace_config": os.environ["CUBLAS_WORKSPACE_CONFIG"],
    }


def train(config_path: Path, args: argparse.Namespace) -> Path:
    training_started = time.perf_counter()
    torch = require_torch()
    cfg = OmegaConf.to_container(OmegaConf.load(config_path), resolve=True)
    if not isinstance(cfg, dict):
        raise ValueError("ADP config must be a YAML mapping.")
    if cfg.get("algorithm", {}).get("return_estimator") == "n_step_td":
        from .td_train import train_td

        return train_td(cfg, args)
    determinism_meta = _configure_training_determinism(torch, int(cfg["base_seed"]))
    algorithm = cfg.get("algorithm", {})
    if (
        str(algorithm.get("return_estimator", "")).lower() != "monte_carlo"
        or bool(algorithm.get("n_step_enabled", False))
        or bool(algorithm.get("td_bootstrap_enabled", False))
    ):
        raise ValueError("Simulation-Based ADP supports complete Monte Carlo returns only; n-step and TD bootstrap must be disabled.")
    allow_wait_action = bool(algorithm.get("allow_wait_action", False))
    worker_order_strategy = str(
        algorithm.get("worker_order_strategy", "cyclic") or "cyclic"
    ).strip().lower()
    if worker_order_strategy not in {"cyclic", "fixed"}:
        raise ValueError("algorithm.worker_order_strategy must be 'cyclic' or 'fixed'.")
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
    policy_update_cfg = (
        cfg.get("policy_update", {})
        if isinstance(cfg.get("policy_update", {}), dict)
        else {}
    )
    conservative_update_enabled = bool(policy_update_cfg.get("enabled", False))
    policy_update_mode = str(
        policy_update_cfg.get(
            "mode",
            "conservative_validation_gate" if conservative_update_enabled else "always_accept",
        )
        or ""
    ).strip().lower()
    expected_update_mode = (
        "conservative_validation_gate" if conservative_update_enabled else "always_accept"
    )
    if policy_update_mode != expected_update_mode:
        raise ValueError(
            "policy_update.mode must be "
            f"{expected_update_mode!r} when policy_update.enabled="
            f"{conservative_update_enabled}."
        )
    acceptance_metric = str(
        policy_update_cfg.get("acceptance_metric", "completed_products_mean")
        or "completed_products_mean"
    ).strip().lower()
    if acceptance_metric != "completed_products_mean":
        raise ValueError(
            "policy_update.acceptance_metric must be 'completed_products_mean'."
        )
    min_policy_improvement = float(policy_update_cfg.get("min_improvement", 0.0) or 0.0)
    if min_policy_improvement < 0.0:
        raise ValueError("policy_update.min_improvement must be non-negative.")
    rollback_model = bool(policy_update_cfg.get("rollback_model", True))
    rollback_optimizer = bool(policy_update_cfg.get("rollback_optimizer", True))
    if conservative_update_enabled and (not rollback_model or not rollback_optimizer):
        raise ValueError(
            "Conservative policy update requires rollback_model=true and "
            "rollback_optimizer=true."
        )
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
    if not worker_counts or len(set(worker_counts)) != len(worker_counts):
        raise ValueError("worker_counts must contain unique worker counts.")
    worker_counts = sorted(worker_counts)
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
    pairwise_cfg = (
        training.get("pairwise_mc_advantage", {})
        if isinstance(training.get("pairwise_mc_advantage", {}), dict)
        else {}
    )
    pairwise_enabled = bool(pairwise_cfg.get("enabled", False))
    pairwise_loss_weight = float(pairwise_cfg.get("loss_weight", 1.0) or 0.0)
    pairwise_capture_fraction = float(
        pairwise_cfg.get("capture_episode_fraction", 0.20) or 0.0
    )
    pairwise_min_pairs_per_batch = max(
        1, int(pairwise_cfg.get("minimum_pairs_per_batch", 1) or 1)
    )
    pairwise_candidate_limit = max(
        2, int(pairwise_cfg.get("candidates_per_state", 3) or 3)
    )
    pairwise_decision_thresholds = [
        max(1, int(value))
        for value in pairwise_cfg.get("decision_thresholds", [20, 100, 300])
    ]
    if pairwise_enabled:
        if not math.isfinite(pairwise_loss_weight) or pairwise_loss_weight <= 0.0:
            raise ValueError(
                "training.pairwise_mc_advantage.loss_weight must be finite and positive."
            )
        if not 0.0 < pairwise_capture_fraction <= 1.0:
            raise ValueError(
                "training.pairwise_mc_advantage.capture_episode_fraction must be in (0, 1]."
            )
        if not pairwise_decision_thresholds:
            raise ValueError(
                "training.pairwise_mc_advantage.decision_thresholds must not be empty."
            )
    days = int(args.days if args.days is not None else cfg["horizon_days"])
    screening_interval = max(
        1,
        int(validation_cfg.get("screening_interval_iterations", training.get("validation_interval_iterations", 5))),
    )
    screening_include_initial = bool(validation_cfg.get("screening_include_initial", True))
    combined_checkpoint_evaluation = bool(
        validation_cfg.get("combined_checkpoint_evaluation", False)
    )
    configured_screening_iterations = validation_cfg.get("screening_iterations")
    screening_iterations = _resolve_screening_iterations(
        policy_iterations=policy_iterations,
        interval=screening_interval,
        include_initial=screening_include_initial,
        configured=(
            [int(value) for value in configured_screening_iterations]
            if configured_screening_iterations is not None
            else None
        ),
    )
    screening_iteration_set = set(screening_iterations)
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
        if not screening_include_initial:
            raise ValueError(
                "Combined checkpoint evaluation requires screening_include_initial=true."
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
    if conservative_update_enabled and screening_iterations != list(
        range(policy_iterations + 1)
    ):
        raise ValueError(
            "Conservative policy update requires validation at every iteration so every "
            "candidate is gated."
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
    independent_cfg = diagnostics_cfg.get("value_validation", {})
    if independent_cfg.get("enabled", False):
        from .value_validation import validate_config

        independent_cfg = validate_config(
            independent_cfg,
            set(training_seeds) | set(screening_seeds) | set(final_selection_seeds)
            | set(held_out_seeds) | (set(fixed_probe_seeds) if fixed_probe_enabled else set()),
        )
    requested_device = str(args.device or runtime.get("device", "cuda:0")).lower()
    require_cuda_training = bool(runtime.get("require_cuda_training", True))
    device = _resolve_training_device(
        torch,
        requested_device,
        require_cuda=require_cuda_training,
    )
    training_device_meta = _training_device_metadata(torch, device)
    training_device_meta["determinism"] = dict(determinism_meta)
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
        "worker_order_strategy": worker_order_strategy,
    }
    replay_scope = str(training.get("replay_scope", "current_iteration")).strip().lower()
    replay_window_iterations = int(training.get("replay_window_iterations", 1))
    latest_iteration_weight = float(training.get("latest_iteration_weight", 1.0))
    compact_enabled = bool(training.get("compact_episode_tensors", True))
    release_after_update = bool(training.get("release_samples_after_update", True))
    if replay_scope not in {"current_iteration", "recent_window"}:
        raise ValueError("training.replay_scope must be current_iteration or recent_window.")
    if replay_scope == "current_iteration":
        replay_window_iterations = 1
        latest_iteration_weight = 1.0
    if replay_window_iterations < 1 or not math.isfinite(latest_iteration_weight) or latest_iteration_weight < 1.0:
        raise ValueError(
            "Short replay requires replay_window_iterations >= 1 and "
            "latest_iteration_weight >= 1.0."
        )
    if not compact_enabled or not release_after_update:
        raise ValueError(
            "Simulation-Based ADP training requires compact_episode_tensors=true and "
            "release_samples_after_update=true. Expired recent-window samples are released."
        )
    validation_episode_fraction = min(0.5, max(0.0, float(training.get("validation_episode_fraction", 0.20))))
    gamma = float(training.get("gamma", 1.0))
    if pairwise_enabled and not math.isclose(gamma, 1.0, abs_tol=1e-12):
        raise ValueError(
            "Pairwise MC advantage replay currently requires training.gamma=1.0."
        )
    episode_rows: list[dict[str, Any]] = []
    iteration_rows: list[dict[str, Any]] = []
    wave_rows: list[dict[str, Any]] = []
    episode_index = 0
    fingerprint: dict[str, Any] | None = None
    environment_fingerprints_by_worker_count: dict[int, str] = {}
    peak_compact_batch_mib = 0.0
    peak_replay_buffer_mib = 0.0
    peak_replay_training_batch_mib = 0.0
    peak_process_rss_mib = _process_rss_mib(peak=True)
    peak_child_rss_mib = 0.0
    total_training_rollout_sec = 0.0
    total_validation_sec = 0.0
    total_merge_sec = 0.0
    total_update_sec = 0.0
    total_fixed_probe_sec = 0.0
    total_pairwise_rollout_sec = 0.0
    pairwise_branch_episode_count = 0
    pairwise_pair_count = 0
    peak_pairwise_replay_mib = 0.0
    ood_support_bank = None
    replay_history: list[tuple[int, CompactMCBatch]] = []
    pairwise_replay_history: list[tuple[int, CompactPairwiseMCBatch]] = []
    pairwise_rows: list[dict[str, Any]] = []

    def register_fingerprint(result: RolloutResult) -> None:
        nonlocal fingerprint
        current = dict(result.fingerprint)
        worker_count = int(result.worker_count)
        environment = str(current.get("environment_fingerprint", ""))
        previous_environment = environment_fingerprints_by_worker_count.get(worker_count)
        if previous_environment is not None and previous_environment != environment:
            raise RuntimeError(
                "ADP environment fingerprint changed within one worker-count group: "
                f"worker_count={worker_count}."
            )
        environment_fingerprints_by_worker_count[worker_count] = environment
        if fingerprint is None:
            fingerprint = current
            return
        for key in (
            "scenario_type",
            "objective_mode",
            "feature_schema_version",
            "timing_fingerprint",
            "reward_mode",
            "loss_type",
            "random_policy",
            "wait_action_enabled",
            "worker_order_strategy",
            "potential_shaping",
        ):
            if str(current.get(key, "")) != str(fingerprint.get(key, "")):
                raise RuntimeError(
                    "ADP rollout contract changed across worker counts: "
                    f"key={key}, expected={fingerprint.get(key)!r}, "
                    f"actual={current.get(key)!r}."
                )

    def collect_batch(
        count: int,
        *,
        iteration: int,
        phase: str,
        force_random: bool,
        epsilon: float,
        active_model: Any | None,
    ) -> tuple[
        CompactMCBatch,
        RolloutBatchMetrics,
        CompactPairwiseMCBatch,
        RolloutBatchMetrics,
    ]:
        nonlocal episode_index, peak_compact_batch_mib, peak_process_rss_mib
        nonlocal peak_child_rss_mib, total_training_rollout_sec, total_merge_sec
        nonlocal total_pairwise_rollout_sec, pairwise_branch_episode_count
        nonlocal pairwise_pair_count
        model_state, snapshot_hash = _snapshot_state_dict(None if force_random else active_model)
        schedule = _balanced_worker_schedule(worker_counts, count)
        capture_count = 0
        capture_indices: set[int] = set()
        if pairwise_enabled and count > 0:
            capture_count = min(
                count,
                max(
                    pairwise_min_pairs_per_batch,
                    int(math.ceil(count * pairwise_capture_fraction)),
                ),
            )
            if capture_count == 1:
                capture_indices = {0}
            else:
                capture_indices = {
                    round(index * (count - 1) / (capture_count - 1))
                    for index in range(capture_count)
                }
        jobs: list[RolloutJob] = []
        capture_ordinal = 0
        for local_index, worker_count in enumerate(schedule):
            absolute_episode = episode_index + local_index + 1
            wave_number = local_index // rollout_wave_size + 1
            episode_adp_cfg = dict(adp_runtime_cfg)
            if local_index in capture_indices:
                threshold = pairwise_decision_thresholds[
                    (iteration * max(1, capture_count) + capture_ordinal)
                    % len(pairwise_decision_thresholds)
                ]
                episode_adp_cfg.update(
                    {
                        "_probe_capture_decision_thresholds": [threshold],
                        "_probe_candidate_limit": pairwise_candidate_limit,
                    }
                )
                capture_ordinal += 1
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
                    adp_cfg=episode_adp_cfg,
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
            register_fingerprint(result)
        episode_index += len(results)
        total_training_rollout_sec += batch_metrics.wall_sec
        total_merge_sec += batch_metrics.merge_sec
        peak_child_rss_mib = max(peak_child_rss_mib, batch_metrics.child_peak_rss_max_mib)
        compact_mib = compact_batch.memory_bytes / (1024.0**2)
        peak_compact_batch_mib = max(peak_compact_batch_mib, compact_mib)
        peak_process_rss_mib = max(peak_process_rss_mib, _process_rss_mib(peak=True))
        empty_pairwise = merge_pairwise_mc_batches([])
        pairwise_metrics = RolloutBatchMetrics()
        if not pairwise_enabled:
            return compact_batch, batch_metrics, empty_pairwise, pairwise_metrics

        captured: list[tuple[EpisodeResult, dict[str, Any], dict[str, Any]]] = []
        for result in results:
            records = result.probe_records or []
            if not records:
                continue
            if len(records) != 1:
                raise RuntimeError(
                    "Pairwise MC capture expected at most one decision per episode: "
                    f"episode={result.episode}, captured={len(records)}."
                )
            record = records[0]
            alternatives = [
                candidate
                for candidate in record["candidates"]
                if candidate["assignment"] != record["selected_assignment"]
            ]
            if not alternatives:
                continue
            alternative_rng = random.Random(
                int(cfg["base_seed"])
                ^ (int(result.episode) << 9)
                ^ (int(record["decision_number"]) << 3)
                ^ 0xA6D2026
            )
            captured.append((result, record, alternative_rng.choice(alternatives)))
        if len(captured) < pairwise_min_pairs_per_batch:
            raise RuntimeError(
                "Pairwise MC advantage sampling did not capture enough multi-action states: "
                f"required={pairwise_min_pairs_per_batch}, captured={len(captured)}, "
                f"iteration={iteration}."
            )

        branch_jobs: list[RolloutJob] = []
        branch_context: dict[int, tuple[EpisodeResult, dict[str, Any], dict[str, Any]]] = {}
        for local_index, (origin, record, alternative) in enumerate(captured):
            branch_episode = 3_000_000 + pairwise_branch_episode_count + local_index + 1
            branch_cfg = {
                **adp_runtime_cfg,
                "_probe_forced_action_script": [
                    *[dict(row) for row in record["prefix_actions"]],
                    dict(alternative["assignment"]),
                ],
                "_probe_target_decision_number": int(record["decision_number"]),
                "_probe_expected_pre_state": dict(record["pre_state"]),
                "_probe_expected_post_state": dict(alternative["post_state"]),
                "_probe_policy_rng_state_after_selection": record[
                    "policy_rng_state_after_selection"
                ],
                "_probe_collect_target_only": True,
                "_probe_id": (
                    f"I{iteration:02d}-E{origin.episode}-"
                    f"D{int(record['decision_number']):05d}"
                ),
                "_probe_candidate_id": int(alternative.get("candidate_id", -1)),
            }
            branch_jobs.append(
                RolloutJob(
                    episode=branch_episode,
                    phase="pairwise_mc_advantage",
                    iteration=iteration,
                    worker_count=origin.worker_count,
                    seed=origin.seed,
                    days=days,
                    epsilon=epsilon,
                    force_random=force_random,
                    adp_cfg=branch_cfg,
                    collect_compact_samples=True,
                    gamma=gamma,
                    wave_id=f"pairwise_mc_advantage-I{iteration:02d}-W01",
                    snapshot_hash=snapshot_hash,
                )
            )
            branch_context[branch_episode] = (origin, record, alternative)
        branch_results, alternative_batch, pairwise_metrics = _run_rollout_jobs_parallel(
            jobs=branch_jobs,
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
        branch_result_by_episode = {result.episode: result for result in branch_results}
        anchor_indices: list[int] = []
        alternative_indices: list[int] = []
        parent_episode_ids: list[int] = []
        for branch_job in branch_jobs:
            branch_result = branch_result_by_episode[branch_job.episode]
            origin, record, alternative = branch_context[branch_job.episode]
            register_fingerprint(branch_result)
            if branch_result.probe_target_post_state != alternative["post_state"]:
                raise RuntimeError(
                    "Pairwise MC alternative afterstate does not match the captured candidate."
                )
            origin_positions = (
                compact_batch.episode_ids == int(origin.episode)
            ).nonzero().flatten().tolist()
            decision_index = int(record["decision_number"]) - 1
            if decision_index < 0 or decision_index >= len(origin_positions):
                raise RuntimeError(
                    "Pairwise MC anchor decision is missing from the compact origin episode."
                )
            alternative_positions = (
                alternative_batch.episode_ids == int(branch_job.episode)
            ).nonzero().flatten().tolist()
            if len(alternative_positions) != 1:
                raise RuntimeError(
                    "Pairwise MC branch must retain exactly one target-decision sample."
                )
            anchor_indices.append(origin_positions[decision_index])
            alternative_indices.append(alternative_positions[0])
            parent_episode_ids.append(int(origin.episode))
        selected_batch = select_compact_samples(compact_batch, anchor_indices)
        alternative_batch = select_compact_samples(
            alternative_batch, alternative_indices
        )
        alternative_batch.episode_ids = selected_batch.episode_ids.clone()
        pairwise_batch = CompactPairwiseMCBatch(
            selected=selected_batch,
            alternative=alternative_batch,
        )
        for pair_index, branch_job in enumerate(branch_jobs):
            origin, record, alternative = branch_context[branch_job.episode]
            selected_target = float(pairwise_batch.selected.targets[pair_index].item())
            alternative_target = float(
                pairwise_batch.alternative.targets[pair_index].item()
            )
            expected_selected = float(origin.products - int(record["products_before"]))
            expected_alternative = float(
                branch_result_by_episode[branch_job.episode].products
                - int(record["products_before"])
            )
            if not math.isclose(selected_target, expected_selected, abs_tol=1e-6):
                raise RuntimeError("Pairwise MC selected-action return mismatch.")
            if not math.isclose(alternative_target, expected_alternative, abs_tol=1e-6):
                raise RuntimeError("Pairwise MC alternative-action return mismatch.")
            pairwise_rows.append(
                {
                    "iteration": iteration,
                    "parent_episode": parent_episode_ids[pair_index],
                    "seed": origin.seed,
                    "worker_count": origin.worker_count,
                    "decision_number": int(record["decision_number"]),
                    "decision_time_min": float(record["time_min"]),
                    "selected_candidate_id": next(
                        (
                            int(candidate.get("candidate_id", -1))
                            for candidate in record["candidates"]
                            if candidate["assignment"] == record["selected_assignment"]
                        ),
                        -1,
                    ),
                    "alternative_candidate_id": int(
                        alternative.get("candidate_id", -1)
                    ),
                    "selected_mc_return": selected_target,
                    "alternative_mc_return": alternative_target,
                    "mc_advantage": selected_target - alternative_target,
                }
            )
        _write_csv(output_dir / "pairwise_mc_advantage_samples.csv", pairwise_rows)
        pairwise_branch_episode_count += len(branch_results)
        pairwise_pair_count += len(pairwise_batch)
        total_pairwise_rollout_sec += pairwise_metrics.wall_sec
        total_training_rollout_sec += pairwise_metrics.wall_sec
        total_merge_sec += pairwise_metrics.merge_sec
        peak_child_rss_mib = max(
            peak_child_rss_mib, pairwise_metrics.child_peak_rss_max_mib
        )
        peak_compact_batch_mib = max(
            peak_compact_batch_mib,
            (compact_batch.memory_bytes + pairwise_batch.memory_bytes) / (1024.0**2),
        )
        peak_process_rss_mib = max(
            peak_process_rss_mib, _process_rss_mib(peak=True)
        )
        return compact_batch, batch_metrics, pairwise_batch, pairwise_metrics

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
            register_fingerprint(result)
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
        for result in results:
            register_fingerprint(result)
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

    (
        current_batch,
        current_rollout_metrics,
        current_pairwise_batch,
        current_pairwise_metrics,
    ) = collect_batch(
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
    incumbent_validation: float | None = None
    incumbent_validation_std = 0.0
    incumbent_validation_by_worker = {
        worker_count: 0.0 for worker_count in worker_counts
    }
    incumbent_train_eval: float | None = None
    incumbent_train_eval_std = 0.0
    incumbent_train_eval_by_worker = {
        worker_count: 0.0 for worker_count in worker_counts
    }
    incumbent_iteration = -1
    accepted_update_count = 0
    rejected_update_count = 0
    for iteration in range(policy_iterations + 1):
        batch_episode_count = current_batch.episode_count
        batch_sample_count = len(current_batch)
        compact_batch_mib = current_batch.memory_bytes / (1024.0**2)
        replay_history.append((iteration, current_batch))
        while len(replay_history) > replay_window_iterations:
            replay_history.pop(0)
        if pairwise_enabled:
            pairwise_replay_history.append((iteration, current_pairwise_batch))
            while len(pairwise_replay_history) > replay_window_iterations:
                pairwise_replay_history.pop(0)
        replay_batch, replay_sample_weights, replay_metrics = build_short_replay_batch(
            replay_history,
            window_iterations=replay_window_iterations,
            latest_iteration_weight=latest_iteration_weight,
        )
        replay_pairwise_batch, replay_pairwise_weights = (
            build_short_pairwise_replay_batch(
                pairwise_replay_history,
                window_iterations=replay_window_iterations,
                latest_iteration_weight=latest_iteration_weight,
            )
            if pairwise_enabled
            else (merge_pairwise_mc_batches([]), torch.zeros((0,), dtype=torch.float32))
        )
        pairwise_replay_mib = replay_pairwise_batch.memory_bytes / (1024.0**2)
        peak_pairwise_replay_mib = max(
            peak_pairwise_replay_mib, pairwise_replay_mib
        )
        replay_buffer_mib = sum(
            batch.memory_bytes for _, batch in replay_history
        ) / (1024.0**2)
        peak_replay_buffer_mib = max(peak_replay_buffer_mib, replay_buffer_mib)
        peak_replay_training_batch_mib = max(
            peak_replay_training_batch_mib,
            float(replay_metrics["replay_compact_mib"]),
        )
        model_before_state, model_before_hash = _snapshot_state_dict(model)
        optimizer_before_state = copy.deepcopy(optimizer.state_dict())
        optimizer_before_hash = _payload_hash(optimizer_before_state)
        incumbent_before_iteration = incumbent_iteration
        incumbent_before_validation = incumbent_validation
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
        pairwise_before = pairwise_mc_advantage_diagnostics(
            model,
            current_pairwise_batch if pairwise_enabled else None,
            device=device,
            batch_size=int(training["batch_size"]),
        )
        update_started = time.perf_counter()
        loss, epochs = fit_mc_value(
            model=model,
            optimizer=optimizer,
            samples=replay_batch,
            device=device,
            batch_size=int(training["batch_size"]),
            max_epochs=int(training["max_epochs_per_iteration"]),
            gradient_clip=float(training["gradient_clip"]),
            patience=int(training["early_stopping_patience"]),
            validation_episode_fraction=validation_episode_fraction,
            rng=rng,
            sample_weights=replay_sample_weights,
            pairwise_samples=(
                replay_pairwise_batch if pairwise_enabled else None
            ),
            pairwise_loss_weight=(
                pairwise_loss_weight if pairwise_enabled else 0.0
            ),
            pairwise_sample_weights=(
                replay_pairwise_weights if pairwise_enabled else None
            ),
        )
        pairwise_after = pairwise_mc_advantage_diagnostics(
            model,
            current_pairwise_batch if pairwise_enabled else None,
            device=device,
            batch_size=int(training["batch_size"]),
        )
        fit_diagnostics = _mc_fit_diagnostics(
            model,
            current_batch,
            device,
            int(training["batch_size"]),
        )
        _candidate_model_state, candidate_model_hash = _snapshot_state_dict(model)
        candidate_optimizer_hash = _payload_hash(optimizer.state_dict())
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
        validation_performed = iteration in screening_iteration_set
        validation_metrics = RolloutBatchMetrics()
        candidate_validation: float | None = None
        candidate_validation_std = 0.0
        candidate_validation_by_worker = {
            worker_count: 0.0 for worker_count in worker_counts
        }
        candidate_train_eval: float | None = None
        candidate_train_eval_std = 0.0
        candidate_train_eval_by_worker = {
            worker_count: 0.0 for worker_count in worker_counts
        }
        if validation_performed:
            if combined_checkpoint_evaluation:
                checkpoint_evaluation, validation_metrics = evaluate_checkpoint(
                    iteration,
                    active_model=model,
                )
                train_eval_summary = checkpoint_evaluation["checkpoint_train_eval"]
                validation_summary = checkpoint_evaluation["checkpoint_validation"]
                candidate_train_eval = float(train_eval_summary["mean"])
                candidate_train_eval_std = float(train_eval_summary["std"])
                candidate_train_eval_by_worker = dict(train_eval_summary["by_worker"])
                candidate_validation = float(validation_summary["mean"])
                candidate_validation_std = float(validation_summary["std"])
                candidate_validation_by_worker = dict(validation_summary["by_worker"])
            else:
                (
                    candidate_validation,
                    candidate_validation_std,
                    candidate_validation_by_worker,
                    validation_metrics,
                ) = validate_policy(
                    iteration,
                    active_model=model,
                    seeds=screening_seeds,
                    phase=(
                        "candidate_validation"
                        if conservative_update_enabled
                        else "screening_validation"
                    ),
                )
        policy_update_accepted = _should_accept_candidate(
            enabled=conservative_update_enabled,
            iteration=iteration,
            candidate_mean=float(candidate_validation or 0.0),
            incumbent_mean=incumbent_validation,
            min_improvement=min_policy_improvement,
        )
        rollback_model_hash_match: bool | str = ""
        rollback_optimizer_hash_match: bool | str = ""
        if conservative_update_enabled:
            if candidate_validation is None:
                raise RuntimeError(
                    "Conservative policy update requires candidate validation at every iteration."
                )
            if policy_update_accepted:
                incumbent_iteration = iteration
                incumbent_validation = float(candidate_validation)
                incumbent_validation_std = float(candidate_validation_std)
                incumbent_validation_by_worker = dict(candidate_validation_by_worker)
                if candidate_train_eval is not None:
                    incumbent_train_eval = float(candidate_train_eval)
                    incumbent_train_eval_std = float(candidate_train_eval_std)
                    incumbent_train_eval_by_worker = dict(candidate_train_eval_by_worker)
                accepted_update_count += 1
            else:
                if model_before_state is None:
                    raise RuntimeError("Incumbent model state is unavailable for rollback.")
                model.load_state_dict(model_before_state)
                optimizer.load_state_dict(copy.deepcopy(optimizer_before_state))
                _, restored_model_hash = _snapshot_state_dict(model)
                restored_optimizer_hash = _payload_hash(optimizer.state_dict())
                rollback_model_hash_match = restored_model_hash == model_before_hash
                rollback_optimizer_hash_match = (
                    restored_optimizer_hash == optimizer_before_hash
                )
                if not rollback_model_hash_match or not rollback_optimizer_hash_match:
                    raise RuntimeError(
                        "Conservative policy update rollback hash mismatch: "
                        f"model={rollback_model_hash_match}, "
                        f"optimizer={rollback_optimizer_hash_match}."
                    )
                rejected_update_count += 1
            last_validation = float(incumbent_validation or 0.0)
            last_validation_std = float(incumbent_validation_std)
            last_validation_by_worker = dict(incumbent_validation_by_worker)
            if incumbent_train_eval is not None:
                last_train_eval = float(incumbent_train_eval)
                last_train_eval_std = float(incumbent_train_eval_std)
                last_train_eval_by_worker = dict(incumbent_train_eval_by_worker)
        else:
            incumbent_iteration = iteration
            accepted_update_count += 1
            if validation_performed and candidate_validation is not None:
                last_validation = float(candidate_validation)
                last_validation_std = float(candidate_validation_std)
                last_validation_by_worker = dict(candidate_validation_by_worker)
                if candidate_train_eval is not None:
                    last_train_eval = float(candidate_train_eval)
                    last_train_eval_std = float(candidate_train_eval_std)
                    last_train_eval_by_worker = dict(candidate_train_eval_by_worker)
        _effective_model_state, effective_model_hash = _snapshot_state_dict(model)
        effective_optimizer_hash = _payload_hash(optimizer.state_dict())
        candidate_incumbent_difference = (
            float(candidate_validation) - float(incumbent_before_validation)
            if candidate_validation is not None and incumbent_before_validation is not None
            else None
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
            "pairwise_mc_advantage_enabled": pairwise_enabled,
            "pairwise_mc_advantage_loss_weight": (
                pairwise_loss_weight if pairwise_enabled else 0.0
            ),
            "pairwise_mc_advantage_pair_count": int(
                pairwise_after["pair_count"]
            ),
            "pairwise_mc_advantage_informative_pair_count": int(
                pairwise_after["informative_pair_count"]
            ),
            "pairwise_mc_advantage_mse_before": round(
                float(pairwise_before["mse"]), 6
            ),
            "pairwise_mc_advantage_mse_after": round(
                float(pairwise_after["mse"]), 6
            ),
            "pairwise_mc_advantage_mae_after": round(
                float(pairwise_after["mae"]), 6
            ),
            "pairwise_mc_advantage_sign_accuracy": round(
                float(pairwise_after["sign_accuracy"]), 6
            ),
            "pairwise_mc_advantage_replay_pair_count": len(
                replay_pairwise_batch
            ),
            "pairwise_mc_advantage_replay_mib": round(
                pairwise_replay_mib, 6
            ),
            "pairwise_mc_advantage_rollout_wall_sec": round(
                current_pairwise_metrics.wall_sec, 6
            ),
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
            "replay_scope": replay_scope,
            "replay_iteration_count": int(replay_metrics["replay_iteration_count"]),
            "replay_oldest_iteration": int(replay_metrics["replay_oldest_iteration"]),
            "replay_newest_iteration": int(replay_metrics["replay_newest_iteration"]),
            "replay_episode_count": int(replay_metrics["replay_episode_count"]),
            "replay_sample_count": int(replay_metrics["replay_sample_count"]),
            "replay_effective_sample_count": round(
                float(replay_metrics["replay_effective_sample_count"]), 3
            ),
            "replay_compact_mib": round(float(replay_metrics["replay_compact_mib"]), 6),
            "replay_buffer_mib": round(replay_buffer_mib, 6),
            "latest_iteration_weight": float(replay_metrics["latest_iteration_weight"]),
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
            "policy_update_mode": policy_update_mode,
            "policy_update_accepted": policy_update_accepted,
            "candidate_validation_products_avg": (
                float(candidate_validation) if candidate_validation is not None else ""
            ),
            "incumbent_before_validation_products_avg": (
                float(incumbent_before_validation)
                if incumbent_before_validation is not None
                else ""
            ),
            "effective_incumbent_validation_products_avg": (
                float(last_validation) if validation_performed else ""
            ),
            "candidate_incumbent_paired_difference": (
                float(candidate_incumbent_difference)
                if candidate_incumbent_difference is not None
                else ""
            ),
            "incumbent_before_iteration": incumbent_before_iteration,
            "effective_incumbent_iteration": incumbent_iteration,
            "model_hash_before_update": model_before_hash,
            "candidate_model_hash": candidate_model_hash,
            "effective_model_hash": effective_model_hash,
            "optimizer_hash_before_update": optimizer_before_hash,
            "candidate_optimizer_hash": candidate_optimizer_hash,
            "effective_optimizer_hash": effective_optimizer_hash,
            "rollback_model_hash_match": rollback_model_hash_match,
            "rollback_optimizer_hash_match": rollback_optimizer_hash_match,
            "next_rollout_incumbent_checkpoint_id": (
                f"ADP-{output_dir.name}-I{incumbent_iteration:02d}"
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
            "worker_order_strategy": worker_order_strategy,
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
            "policy_update": {
                "mode": policy_update_mode,
                "enabled": conservative_update_enabled,
                "acceptance_metric": acceptance_metric,
                "min_improvement": min_policy_improvement,
                "candidate_accepted": policy_update_accepted,
                "candidate_validation_products_avg": candidate_validation,
                "incumbent_before_validation_products_avg": (
                    incumbent_before_validation
                ),
                "effective_incumbent_validation_products_avg": last_validation,
                "candidate_incumbent_paired_difference": (
                    candidate_incumbent_difference
                ),
                "incumbent_before_iteration": incumbent_before_iteration,
                "effective_incumbent_iteration": incumbent_iteration,
                "rollback_model_hash_match": rollback_model_hash_match,
                "rollback_optimizer_hash_match": rollback_optimizer_hash_match,
                "effective_model_hash": effective_model_hash,
                "effective_optimizer_hash": effective_optimizer_hash,
            },
            "checkpoint_id": (
                f"ADP-{output_dir.name}-I{iteration:02d}"
                if not conservative_update_enabled
                else (
                    f"ADP-{output_dir.name}-I{iteration:02d}"
                    f"-INC{incumbent_iteration:02d}"
                )
            ),
            "worker_count_range": [min(worker_counts), max(worker_counts)],
            "supported_worker_counts": list(worker_counts),
            "environment_fingerprints_by_worker_count": {
                str(worker_count): environment_fingerprints_by_worker_count[worker_count]
                for worker_count in worker_counts
            },
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
                "replay_window_iterations": replay_window_iterations,
                "latest_iteration_weight": latest_iteration_weight,
                "replay_target_contract": "complete_mc_return_under_collection_policy",
                "target_network": False,
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
                "pairwise_mc_advantage": {
                    "enabled": pairwise_enabled,
                    "loss_weight": pairwise_loss_weight,
                    "capture_episode_fraction": pairwise_capture_fraction,
                    "minimum_pairs_per_batch": pairwise_min_pairs_per_batch,
                    "decision_thresholds": list(pairwise_decision_thresholds),
                    "candidates_per_state": pairwise_candidate_limit,
                    "target": "selected_mc_return_minus_alternative_mc_return",
                    "common_random_seed_replay": True,
                },
                "learning_rate": float(training["learning_rate"]),
                "max_epochs_per_iteration": int(training["max_epochs_per_iteration"]),
                "epsilon_start": float(training["epsilon_start"]),
                "epsilon_end": float(training["epsilon_end"]),
                "max_review_interval_min": float(adp_runtime_cfg["max_review_interval_min"]),
                "wait_action_enabled": allow_wait_action,
                "worker_order_strategy": worker_order_strategy,
                "policy_update_mode": policy_update_mode,
                "conservative_policy_update": conservative_update_enabled,
                "policy_update_min_improvement": min_policy_improvement,
                "peak_compact_batch_mib": round(peak_compact_batch_mib, 6),
                "peak_replay_buffer_mib": round(peak_replay_buffer_mib, 6),
                "peak_replay_training_batch_mib": round(
                    peak_replay_training_batch_mib, 6
                ),
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
                    "candidate_validation_products_avg": (
                        round(float(candidate_validation), 6)
                        if candidate_validation is not None
                        else ""
                    ),
                    "candidate_incumbent_paired_difference": (
                        round(float(candidate_incumbent_difference), 6)
                        if candidate_incumbent_difference is not None
                        else ""
                    ),
                    "policy_update_accepted": policy_update_accepted,
                    "effective_incumbent_iteration": incumbent_iteration,
                    "iteration_checkpoint": str(iteration_checkpoint.resolve()),
                    "selection_checkpoint": str(candidate_checkpoint.resolve()),
                }
            )
        ood_support_bank = next_ood_support_bank
        del model_before_state
        del optimizer_before_state
        del _candidate_model_state
        del _effective_model_state
        del replay_batch
        del replay_sample_weights
        del replay_pairwise_batch
        del replay_pairwise_weights
        del current_batch
        del current_pairwise_batch
        gc.collect()
        peak_process_rss_mib = max(peak_process_rss_mib, _process_rss_mib(peak=True))
        if iteration < policy_iterations:
            epsilon = _linear(
                float(training["epsilon_start"]),
                float(training["epsilon_end"]),
                iteration,
                policy_iterations,
            )
            (
                current_batch,
                current_rollout_metrics,
                current_pairwise_batch,
                current_pairwise_metrics,
            ) = collect_batch(
                episodes_per_iteration,
                iteration=iteration + 1,
                phase=f"policy_iteration_{iteration + 1}",
                force_random=False,
                epsilon=epsilon,
                active_model=model,
            )

    replay_history.clear()
    pairwise_replay_history.clear()
    gc.collect()

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
        final_mean, final_std, worker_means, final_metrics = validate_policy(
            candidate_iteration,
            active_model=candidate_model,
            seeds=final_selection_seeds,
            phase="final_selection_validation",
        )
        selection_payloads[candidate_iteration] = payload
        final_sample_count = len(final_selection_seeds) * len(worker_counts)
        selection_rows.append(
            {
                **row,
                "screening_rank": screening_rank,
                "final_mean_products": round(final_mean, 6),
                "final_std_products": round(final_std, 6),
                "final_seed_count": len(final_selection_seeds),
                "final_sample_count": final_sample_count,
                **{
                    f"final_mean_products_workers_{worker_count}": round(
                        float(worker_means[worker_count]), 6
                    )
                    for worker_count in worker_counts
                },
                "final_ci95_low": round(
                    final_mean - 1.96 * final_std / math.sqrt(final_sample_count), 6
                ),
                "final_ci95_high": round(
                    final_mean + 1.96 * final_std / math.sqrt(final_sample_count), 6
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
    best_manifest["validation_completed_products_avg_by_worker_count"] = {
        str(worker_count): float(
            winner[f"final_mean_products_workers_{worker_count}"]
        )
        for worker_count in worker_counts
    }
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
            "pairwise_mc_advantage",
            "checkpoint_diagnostic",
            "screening_validation",
            "candidate_validation",
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
            "value_update_count": policy_iterations + 1,
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
            "screening_schedule_mode": (
                "explicit" if configured_screening_iterations is not None else "interval"
            ),
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
            "replay_window_iterations": replay_window_iterations,
            "latest_iteration_weight": latest_iteration_weight,
            "replay_target_contract": "complete_mc_return_under_collection_policy",
            "target_network": False,
            "compact_episode_tensors": compact_enabled,
            "release_samples_after_update": release_after_update,
            "peak_compact_batch_mib": round(peak_compact_batch_mib, 6),
            "peak_replay_buffer_mib": round(peak_replay_buffer_mib, 6),
            "peak_replay_training_batch_mib": round(
                peak_replay_training_batch_mib, 6
            ),
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
            "policy_update_mode": policy_update_mode,
            "conservative_policy_update": conservative_update_enabled,
            "policy_update_min_improvement": min_policy_improvement,
            "accepted_update_count": accepted_update_count,
            "rejected_update_count": rejected_update_count,
            "pairwise_mc_advantage": {
                "enabled": pairwise_enabled,
                "loss_weight": pairwise_loss_weight,
                "capture_episode_fraction": pairwise_capture_fraction,
                "minimum_pairs_per_batch": pairwise_min_pairs_per_batch,
                "decision_thresholds": list(pairwise_decision_thresholds),
                "candidates_per_state": pairwise_candidate_limit,
                "target": "selected_mc_return_minus_alternative_mc_return",
                "common_random_seed_replay": True,
                "branch_episode_count": pairwise_branch_episode_count,
                "pair_count": pairwise_pair_count,
                "peak_replay_mib": round(peak_pairwise_replay_mib, 6),
            },
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
        "value_update_count": policy_iterations + 1,
        "episodes_per_iteration": episodes_per_iteration,
        "loss_type": "mse",
        "reward_mode": "completed_product_mc",
        "potential_shaping": False,
        "random_policy": random_policy_name,
        "wait_action_enabled": allow_wait_action,
        "worker_order_strategy": worker_order_strategy,
        "policy_update_mode": policy_update_mode,
        "conservative_policy_update": conservative_update_enabled,
        "policy_update_min_improvement": min_policy_improvement,
        "accepted_update_count": accepted_update_count,
        "rejected_update_count": rejected_update_count,
        "pairwise_mc_advantage_enabled": pairwise_enabled,
        "pairwise_mc_advantage_loss_weight": pairwise_loss_weight,
        "pairwise_mc_advantage_capture_episode_fraction": pairwise_capture_fraction,
        "pairwise_mc_advantage_minimum_pairs_per_batch": pairwise_min_pairs_per_batch,
        "pairwise_mc_advantage_decision_thresholds": list(
            pairwise_decision_thresholds
        ),
        "pairwise_mc_advantage_candidates_per_state": pairwise_candidate_limit,
        "pairwise_mc_advantage_branch_episode_count": pairwise_branch_episode_count,
        "pairwise_mc_advantage_pair_count": pairwise_pair_count,
        "pairwise_mc_advantage_peak_replay_mib": round(
            peak_pairwise_replay_mib, 6
        ),
        "supported_worker_counts": list(worker_counts),
        "environment_fingerprints_by_worker_count": {
            str(worker_count): environment_fingerprints_by_worker_count.get(worker_count, "")
            for worker_count in worker_counts
        },
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
        "replay_window_iterations": replay_window_iterations,
        "latest_iteration_weight": latest_iteration_weight,
        "replay_target_contract": "complete_mc_return_under_collection_policy",
        "target_network": False,
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
        "peak_replay_buffer_mib": round(peak_replay_buffer_mib, 6),
        "peak_replay_training_batch_mib": round(
            peak_replay_training_batch_mib, 6
        ),
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
        "ordinary_training_rollout_sec": round(
            total_training_rollout_sec - total_pairwise_rollout_sec, 6
        ),
        "pairwise_mc_advantage_rollout_sec": round(
            total_pairwise_rollout_sec, 6
        ),
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
        "independent_value_validation_config": independent_cfg,
        "total_simulation_episode_count": len(episode_rows),
    }
    summary["total_wall_clock_sec"] = round(time.perf_counter() - training_started, 6)
    (output_dir / "training_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    artifact_started = time.perf_counter()
    dashboard = render_training_dashboard(output_dir, episode_rows, iteration_rows, wave_rows, summary)
    summary["checkpoint_dashboard_write_sec"] = round(time.perf_counter() - artifact_started, 6)
    summary["total_wall_clock_sec"] = round(time.perf_counter() - training_started, 6)
    (output_dir / "training_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    dashboard = render_training_dashboard(output_dir, episode_rows, iteration_rows, wave_rows, summary)
    if independent_cfg.get("enabled", False):
        from .value_validation import refresh_dashboard, run_validation

        run_validation(output_dir, independent_cfg)
        dashboard = refresh_dashboard(output_dir)
    if not args.no_open_dashboard and bool(runtime.get("auto_open_dashboard", True)):
        webbrowser.open(dashboard.resolve().as_uri())
    return dashboard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train the mfg_flow_shop simulation-based ADP policy.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "configs"
        / "adp"
        / "mfg_flow_shop_throughput.yaml",
        help="Training profile. Defaults to fleet-specific n-step TD with n=30.",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--device", default=None)
    parser.add_argument("--worker-counts", nargs="*", type=int, default=None)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--initial-random-episodes", type=int, default=None)
    parser.add_argument("--policy-iterations", type=int, default=None)
    parser.add_argument("--episodes-per-iteration", type=int, default=None)
    parser.add_argument("--validation-episodes-per-worker", type=int, default=None)
    parser.add_argument("--rollout-processes", type=int, default=None)
    parser.add_argument(
        "--warm-start-checkpoint",
        type=Path,
        default=None,
        help="Override the n-step TD warm-start checkpoint configured in YAML.",
    )
    parser.add_argument("--no-open-dashboard", action="store_true")
    parser.add_argument("--background", action="store_true",
                        help="Launch an independent process and return immediately with a live monitor path.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    from .live import TrainingProgress
    if args.background:
        from .background import launch
        cfg = OmegaConf.load(args.config)
        output = (args.output or Path(str(cfg.runtime.output_root)) / datetime.now().strftime("%Y%m%d_%H%M%S_%f")).resolve()
        if any((output / name).exists() for name in ("training_summary.json", "episode_metrics.csv")):
            raise ValueError("Use a new output directory for background training; existing results are preserved.")
        forwarded = [arg for arg in sys.argv[1:] if arg != "--background"]
        # The explicit trailing output wins over any earlier --output argument.
        command = [sys.executable, "-u", "-m", "manufacturing_sim.adp.train", *forwarded,
                   "--output", str(output), "--no-open-dashboard"]
        monitor = launch(command=command, job_dir=output / "background_job",
                         runs=[{"label": output.name, "output": str(output)}],
                         cwd=Path.cwd(), open_browser=not args.no_open_dashboard)
        print(f"Background training launched.\nMonitor: {monitor}\nJob: {monitor.parent}", flush=True)
        return
    if args.output is None:
        cfg = OmegaConf.load(args.config)
        args.output = Path(str(cfg.runtime.output_root)) / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    progress = TrainingProgress(args.output.resolve())
    # Check before publishing progress so a mistaken command cannot modify an old result.
    if any((args.output / name).exists() for name in ("training_summary.json", "episode_metrics.csv")):
        raise ValueError("Refusing to overwrite an existing training run.")
    progress.update(status="running", phase="starting")
    try:
        dashboard = train(args.config.resolve(), args)
    except BaseException as exc:
        progress.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    progress.update(status="completed", phase="completed")
    print(str(dashboard.resolve()))


if __name__ == "__main__":
    main()
