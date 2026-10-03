"""Same-input ADP compute benchmark; never trains or replaces a checkpoint."""
from __future__ import annotations

import argparse
import ast
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import torch

from manufacturing_sim.adp import model as model_module
from manufacturing_sim.adp.compact import merge_compact_batches, select_compact_samples
from manufacturing_sim.adp.schema import FEATURE_SCHEMA_VERSION
from manufacturing_sim.adp.td import build_target_plan, sample_replay_episodes
from manufacturing_sim.adp.train import run_training_episode
from manufacturing_sim.simulation.scenarios.manufacturing.humanoid_runtime import HumanoidTaskRuntime

BASELINE = "baseline-adp-efficiency-20260926"


def reference_collate():
    source = subprocess.check_output(
        ["git", "show", f"{BASELINE}:manufacturing_sim/adp/model.py"], cwd=ROOT, encoding="utf-8",
    )
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "collate_states")
    namespace = dict(vars(model_module))
    exec(compile(ast.Module(body=[function], type_ignores=[]), "baseline_collate", "exec"), namespace)
    return namespace["collate_states"]


def timed(call, repeats=3, device="cpu"):
    elapsed = []
    result = None
    for _ in range(repeats):
        if device.startswith("cuda"):
            torch.cuda.synchronize()
        started = time.perf_counter()
        result = call()
        if device.startswith("cuda"):
            torch.cuda.synchronize()
        elapsed.append(time.perf_counter() - started)
    return result, {"seconds": elapsed, "median_sec": statistics.median(elapsed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--seed", type=int, default=72026)
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite an existing benchmark report")
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    torch.manual_seed(args.seed)
    report = {"workers": args.workers, "seed": args.seed, "days": args.days,
              "schema": FEATURE_SCHEMA_VERSION, "baseline": BASELINE,
              "scope": "Random rollout + fresh-weight same-input compute, not learned productivity",
              "torch": torch.__version__}

    load_runtime = HumanoidTaskRuntime._load_humanoidsim

    def uncached_runtime(runtime, cfg):
        load_runtime(runtime, cfg)
        for name in ("validate_state_snapshot", "transition_humanoid_state"):
            original = runtime._imports[name]

            def reload(*pos, _original=original, **kw):
                kw.pop("schema", None)
                return _original(*pos, **kw)

            runtime._imports[name] = reload

    def rollout():
        return run_training_episode(
            episode=1, phase="efficiency_benchmark", worker_count=args.workers,
            seed=args.seed, days=args.days, model=None, device="cpu", force_random=True, epsilon=1.0,
            adp_cfg={"_collect_td_replay": True, "allow_wait_action": False,
                     "worker_order_strategy": "cyclic", "beam_width": 64, "max_review_interval_min": 1.0},
        )

    batches = []
    for name, context in [("uncached_schema", patch.object(HumanoidTaskRuntime, "_load_humanoidsim", uncached_runtime)),
                          ("cached_schema", nullcontext())]:
        with context:
            (result, batch), timing = timed(rollout, repeats=1)
        report[name] = {**timing, "products": result.products, "decisions": result.decisions, "samples": len(batch)}
        batches.append(batch)
        print(name, report[name], flush=True)
    before, batch = batches
    same_rollout = all(torch.equal(getattr(before, name), getattr(batch, name)) for name in
        ("global_features", "worker_features", "task_features", "pair_features", "selected_assignment_mask", "targets"))
    same_rollout &= torch.equal(before.td_data.rewards, batch.td_data.rewards)
    same_rollout &= before.td_data.descriptors == batch.td_data.descriptors
    report["rollout_tensors_rewards_actions_equal"] = bool(same_rollout)
    if not same_rollout:
        raise AssertionError("Schema cache changed rollout")
    del before, batches
    reference = reference_collate()
    network = model_module.build_value_network().eval()
    devices = ["cpu"] + (["cuda:0"] if torch.cuda.is_available() else [])
    for device in devices:
        model = copy.deepcopy(network).to(device).eval()
        model_module.predict_values(model, [batch.td_data.state(0)], device)
        options = dict(n=30, beam_width=64, worker_order_strategy="cyclic")
        with patch("manufacturing_sim.adp.model.collate_states", reference), patch(
            "manufacturing_sim.adp.td.unique_feasible_assignment", return_value=None,
        ):
            old, old_time = timed(lambda: build_target_plan(batch, model, device, **options), args.repeats, device)
        new, new_time = timed(lambda: build_target_plan(batch, model, device, **options), args.repeats, device)
        equal = (torch.equal(old.endpoints, new.endpoints) and old.steps == new.steps and old.reasons == new.reasons
                 and torch.equal(old.greedy_posts.selected_assignment_mask, new.greedy_posts.selected_assignment_mask)
                 and torch.equal(old.targets(list(range(len(batch))), model, device), new.targets(list(range(len(batch))), model, device)))
        report[f"target_{device}"] = {"before": old_time, "after": new_time,
            "speedup": old_time["median_sec"] / new_time["median_sec"], "actions_endpoints_targets_equal": equal}
        if not equal:
            raise AssertionError(f"Target mismatch on {device}")
        print(device, report[f"target_{device}"], flush=True)

    history = []
    for eid in range(1, 101):
        chunk = copy.copy(batch)
        chunk.episode_ids = torch.full_like(batch.episode_ids, eid)
        history.append(chunk)

    def legacy_sample():
        replay = merge_compact_batches(history)
        ids = {100, *random.Random(2026).sample(list(range(1, 100)), 29)}
        return select_compact_samples(replay, [i for i, eid in enumerate(replay.episode_ids.tolist()) if eid in ids])

    old, old_time = timed(legacy_sample, args.repeats)
    new, new_time = timed(lambda: sample_replay_episodes(history, required_episode_ids={100}, episode_count=30, rng=random.Random(2026)), args.repeats)
    assert torch.equal(old.episode_ids, new.episode_ids)
    assert torch.equal(old.pair_features, new.pair_features)
    report["replay_merge"] = {"before": old_time, "after": new_time, "selected_episode_ids_equal": True,
                              "speedup": old_time["median_sec"] / new_time["median_sec"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(args.output, flush=True)


if __name__ == "__main__":
    main()
