"""Bounded same-input benchmark, not a full training/productivity experiment."""
from __future__ import annotations

import argparse
import copy
import dataclasses
import gc
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import torch

from manufacturing_sim.adp.live import atomic_json
from manufacturing_sim.adp.model import build_value_network, predict_values
from manufacturing_sim.adp.td import build_target_plan, fit_td_value, sample_replay_episodes
from manufacturing_sim.adp.train import (RolloutJob, RolloutPool, _configure_training_determinism,
    _process_rss_mib, _run_rollout_jobs_parallel, _snapshot_state_dict)


def batch_digest(batch):
    digest = hashlib.sha256()
    for field in dataclasses.fields(batch):
        value = getattr(batch, field.name)
        if torch.is_tensor(value):
            digest.update(field.name.encode())
            digest.update(str(tuple(value.shape)).encode())
            digest.update(value.numpy().tobytes())
    if batch.td_data is not None:
        digest.update(batch_digest(batch.td_data.pre).encode())
        digest.update(batch.td_data.rewards.numpy().tobytes())
        digest.update(batch.td_data.dones.numpy().tobytes())
        digest.update(json.dumps(batch.td_data.descriptors, sort_keys=True).encode())
    return digest.hexdigest()


def compare_plans(a, b, target, device):
    assert torch.equal(a.endpoints, b.endpoints), "TD endpoints changed"
    assert a.steps == b.steps and a.reasons == b.reasons, "TD truncation changed"
    assert batch_digest(a.greedy_posts) == batch_digest(b.greedy_posts), "Greedy afterstates changed"
    max_error = 0.0
    for start in range(0, len(a.greedy_posts), 512):
        ids = list(range(start, min(start + 512, len(a.greedy_posts))))
        x, y = a.targets(ids, target, device), b.targets(ids, target, device)
        max_error = max(max_error, float((x - y).abs().max()))
        assert torch.equal(x, y), "TD target changed"
    return max_error


def independent_optimizer(model, state):
    optimizer = torch.optim.Adam(model.parameters(), lr=2e-5)
    # Adam may retain CPU step tensors from load_state_dict, even for CUDA models.
    optimizer.load_state_dict(copy.deepcopy(state))
    return optimizer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--second-checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite an existing report")
    args.output.mkdir(parents=True)
    torch.set_num_threads(1)
    _configure_training_determinism(torch, 72026)
    device = torch.device("cuda:0")
    payloads = [torch.load(path, map_location="cpu", weights_only=False)
                for path in (args.checkpoint, args.second_checkpoint)]
    manifests = [payload["manifest"] for payload in payloads]
    model_cfg = manifests[0]["model"]
    for manifest in manifests:
        assert manifest["supported_worker_counts"] == [3]
        assert manifest["model"] == model_cfg
        assert manifest["environment_fingerprint"] == manifests[0]["environment_fingerprint"]
        assert manifest["horizon_days"] == 5
        assert manifest.get("wait_action_enabled") is False
        assert manifest.get("worker_order_strategy") == "cyclic"
    network_args = {k: model_cfg[k] for k in ("embedding_dim", "heads", "layers")}
    models = [build_value_network(**network_args).eval() for _ in payloads]
    for model, payload in zip(models, payloads):
        model.load_state_dict(payload["model_state_dict"])
    snapshots = [_snapshot_state_dict(model) for model in models]
    assert snapshots[0][1] != snapshots[1][1], "Need different models to test snapshot refresh"
    started = time.perf_counter()
    report = dict(status="running", scope="compute equivalence and speed; not long-run noninferiority",
                  checkpoint_paths=[str(args.checkpoint), str(args.second_checkpoint)],
                  snapshot_hashes=[s[1] for s in snapshots], workers=3, days=5,
                  processes=10, wave_size=10, budget_sec=args.minutes * 60,
                  phases=[], rollout=[], target=[], replay_budget=[])

    def write(phase):
        report.update(phase=phase, elapsed_sec=time.perf_counter() - started,
                      parent_peak_rss_mib=_process_rss_mib(peak=True))
        if torch.cuda.is_initialized():
            report["cuda_peak_allocated_mib"] = torch.cuda.max_memory_allocated(device) / 1024 ** 2
            report["cuda_peak_reserved_mib"] = torch.cuda.max_memory_reserved(device) / 1024 ** 2
        atomic_json(args.output / "report.json", report)
        print(phase, f"elapsed={report['elapsed_sec']:.1f}s", flush=True)

    def room(seconds):
        return time.perf_counter() - started + seconds < args.minutes * 60

    adp = dict(_collect_td_replay=True, beam_width=model_cfg["beam_width"],
               max_review_interval_min=1., allow_wait_action=False, worker_order_strategy="cyclic")

    def collect(label, wave, pool=None, force_random=False):
        out = args.output / label / str(wave)
        out.mkdir(parents=True)
        state, snapshot = (None, "RANDOM") if force_random else snapshots[wave - 1]
        jobs = [RolloutJob(episode=wave * 10 + i + 1, phase="speedup_benchmark", iteration=wave,
                          worker_count=3, seed=71901 + wave * 100 + i, days=5, epsilon=.2,
                          force_random=force_random, adp_cfg=adp, collect_compact_samples=True,
                          gamma=1., wave_id=f"W{wave}", snapshot_hash=snapshot) for i in range(10)]
        before = time.perf_counter()
        results, batch, metrics = _run_rollout_jobs_parallel(
            jobs=jobs, model_state=state, model_cfg=model_cfg, process_count=10, wave_size=10,
            start_method="spawn", torch_threads=1, output_dir=out, episode_rows=[], wave_rows=[],
            rollout_pool=pool, progress_callback=lambda e: None)
        assert all(r.termination_reason == "completed_horizon" for r in results)
        assert all(r.fingerprint["environment_fingerprint"] == manifests[0]["environment_fingerprint"] for r in results)
        record = dict(label=label, wave=wave, wall_sec=time.perf_counter() - before,
                      metrics=dataclasses.asdict(metrics), products=[r.products for r in results],
                      decisions=[r.decisions for r in results], batch_hash=batch_digest(batch),
                      process_ids=sorted({r.process_slot for r in results}),
                      observed_snapshot_hashes=sorted({r.snapshot_hash for r in results}))
        report["rollout"].append(record)
        write(f"{label} wave {wave} complete")
        return batch, record

    write("starting")
    try:
        initial, _ = collect("initial_fixture", 0, force_random=True)
        reference, records = [], []
        for wave in [1, 2]:
            batch, record = collect("reference_pool", wave)
            reference.append(batch)
            records.append(record)
        with RolloutPool(process_count=10, model_cfg=model_cfg, torch_threads=1) as pool:
            for wave in [1, 2]:
                batch, record = collect("persistent_pool", wave, pool)
                baseline = records[wave - 1]
                for key in ("products", "decisions", "batch_hash", "observed_snapshot_hashes"):
                    assert record[key] == baseline[key], f"Rollout mismatch: {key}"
                del batch
                gc.collect()
            report["persistent_pool_start_count"] = pool.start_count
            closing = time.perf_counter()
            pool.close()
            report["persistent_pool_shutdown_sec"] = time.perf_counter() - closing
        report["rollout_exact_equivalence"] = True
        model = models[0].to(device)
        target = copy.deepcopy(model).eval()
        target.load_state_dict(payloads[0]["target_model_state_dict"])
        options = dict(n=30, beam_width=model_cfg["beam_width"], worker_order_strategy="cyclic")
        predict_values(model, [reference[0].td_data.state(0)], device)
        plans, elapsed = {}, {1: [], 64: []}
        for repeat in range(args.repeats):
            if repeat and not room(240):
                break
            for size in ([1, 64] if repeat % 2 == 0 else [64, 1]):
                stats = {}
                torch.cuda.synchronize()
                before = time.perf_counter()
                plan = build_target_plan(reference[0], model, device, **options, state_batch_size=size, stats=stats)
                torch.cuda.synchronize()
                seconds = time.perf_counter() - before
                elapsed[size].append(seconds)
                plans[size] = plan
                report["target"].append(dict(repeat=repeat, state_batch_size=size, wall_sec=seconds,
                                             sample_count=len(reference[0]), **stats))
                write(f"target batch={size} repeat={repeat}")
            report["target_max_absolute_error"] = compare_plans(plans[1], plans[64], target, device)
        report["target_exact_equivalence"] = True
        report["target_speedup"] = statistics.median(elapsed[1]) / statistics.median(elapsed[64])
        # Same optimizer/target and shuffle before either path, not a fresh optimizer.
        updated = []
        for size in [1, 64]:
            online, frozen = copy.deepcopy(model), copy.deepcopy(target)
            optimizer = independent_optimizer(online, payloads[0]["optimizer_state_dict"])
            metrics = fit_td_value(model=online, target_model=frozen, optimizer=optimizer,
                samples=reference[0], plan=plans[size], device=device, batch_size=512,
                epochs=2, gradient_clip=.5, target_tau=.03, rng=random.Random(123))
            updated.append(_snapshot_state_dict(online)[1])
        assert updated[0] == updated[1], "Training update changed"
        report["updated_model_hash_equal"] = True
        del plans
        for count in [30, 20]:
            if not room(150):
                report.setdefault("skipped", []).append(f"replay_{count}: time budget")
                continue
            sampled = sample_replay_episodes([initial, *reference], required_episode_ids=set(range(21, 31)),
                                             episode_count=count, rng=random.Random(123))
            online, frozen = copy.deepcopy(model), copy.deepcopy(target)
            optimizer = independent_optimizer(online, payloads[0]["optimizer_state_dict"])
            before = time.perf_counter()
            plan = build_target_plan(sampled, online, device, **options, state_batch_size=64)
            torch.cuda.synchronize()
            build_seconds = time.perf_counter() - before
            before = time.perf_counter()
            metrics = fit_td_value(model=online, target_model=frozen, optimizer=optimizer, samples=sampled,
                plan=plan, device=device, batch_size=512, epochs=2, gradient_clip=.5, target_tau=.03,
                rng=random.Random(123))
            torch.cuda.synchronize()
            report["replay_budget"].append(dict(episodes=count, target_build_sec=build_seconds,
                update_sec=time.perf_counter() - before, samples=len(sampled), **metrics))
            del sampled, plan, online, frozen, optimizer
            write(f"replay budget={count}")
        report["status"] = "completed"
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        write("failed")
        raise
    write("completed")


if __name__ == "__main__":
    main()
