from __future__ import annotations

import argparse
import copy
import gc
import html
import json
import math
import random
import sys
import time
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import simpy
from omegaconf import OmegaConf

from agents.factory import build_decision_module
from manufacturing_sim.adp.checkpoint import load_checkpoint, save_checkpoint
from manufacturing_sim.adp.compact import CompactMCBatch, merge_compact_batches, stratified_episode_split
from manufacturing_sim.adp.model import predict_values, require_torch
from manufacturing_sim.adp.schema import deserialize_state
from manufacturing_sim.adp.train import (
    InMemoryEventLogger,
    RolloutJob,
    _compose_episode_cfg,
    _configure_training_determinism,
    _run_rollout_jobs_parallel,
    _snapshot_state_dict,
    _svg_chart,
    _write_csv,
    fit_mc_value,
)
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


def select_samples(batch: CompactMCBatch, indices: list[int]) -> CompactMCBatch:
    torch = require_torch()
    index = torch.as_tensor(indices, dtype=torch.long)
    return CompactMCBatch(**{
        field.name: getattr(batch, field.name).index_select(0, index)
        for field in fields(CompactMCBatch)
    })


def make_branch(origin, record: dict, candidate: dict, *, episode: int, phase: str,
                runtime: dict, days: int, epsilon: float, snapshot_hash: str,
                collect: bool) -> RolloutJob:
    return RolloutJob(
        episode=episode, phase=phase, iteration=1, worker_count=origin.worker_count,
        seed=origin.seed, days=days, epsilon=epsilon, force_random=False,
        collect_compact_samples=collect, gamma=1.0, wave_id=phase,
        snapshot_hash=snapshot_hash,
        adp_cfg={
            **runtime,
            "_probe_forced_action_script": record["prefix_actions"] + [candidate["assignment"]],
            "_probe_target_decision_number": record["decision_number"],
            "_probe_expected_pre_state": record["pre_state"],
            "_probe_policy_rng_state_after_selection": record["policy_rng_state_after_selection"],
            "_probe_collect_target_only": True,
            "_probe_id": f"seed_{origin.seed}_decision_{record['decision_number']}",
            "_probe_candidate_id": int(candidate.get("candidate_id", -1)),
        },
    )


def alternative(record: dict, seed: int) -> dict:
    choices = [candidate for candidate in record["candidates"]
               if candidate["assignment"] != record["selected_assignment"]]
    if not choices:
        raise RuntimeError("No distinct alternative in captured decision.")
    return random.Random(seed).choice(choices)


def paired_summary(first: list[int], second: list[int]) -> dict:
    differences = np.asarray(first, dtype=float) - np.asarray(second, dtype=float)
    rng = np.random.default_rng(2026)
    means = rng.choice(differences, size=(10000, len(differences)), replace=True).mean(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return {
        "mean_difference": float(differences.mean()),
        "paired_bootstrap_ci95": [float(low), float(high)],
        "wins": int((differences > 0).sum()), "ties": int((differences == 0).sum()),
        "losses": int((differences < 0).sum()), "differences": differences.tolist(),
    }


def experiment_arms(training: dict) -> dict[str, float]:
    if "counterfactual_fractions" not in training:
        return {"control": 0.0, "treatment": float(training["counterfactual_fraction"])}
    fractions = [float(value) for value in training["counterfactual_fractions"]]
    if (len(fractions) < 2 or fractions[0] != 0.0 or len(set(fractions)) != len(fractions)
            or any(not math.isfinite(value) or not 0.0 <= value < 1.0 for value in fractions)):
        raise ValueError("Fractions must start with zero and contain distinct finite values in [0, 1).")
    return {"control" if value == 0 else f"cf_{value * 100:g}pct": value for value in fractions}


def sampling_counts(sample_count: int, batch_size: int, fraction: float) -> dict:
    sizes = [min(batch_size, sample_count - start) for start in range(0, sample_count, batch_size)]
    extra = sum(min(size - 1, max(1, round(size * fraction)))
                for size in sizes if size >= 2) if fraction else 0
    return {"sgd_steps_per_epoch": len(sizes), "samples_per_epoch": sample_count,
            "counterfactual_draws_per_epoch": extra,
            "actual_counterfactual_fraction": extra / sample_count if sample_count else 0.0}


def regression_mse(model, samples: CompactMCBatch, device) -> float:
    torch = require_torch()
    model.eval()
    squared = 0.0
    with torch.no_grad():
        for start in range(0, len(samples), 512):
            inputs, targets = samples.model_batch(list(range(start, min(start + 512, len(samples)))), device)
            squared += float(((model(**inputs) - targets) ** 2).sum().item())
    return squared / len(samples)


def write_report(output: Path, summary: dict, diagnostics: list[dict]) -> None:
    evaluation = summary["evaluation"]
    fractions = summary.get("counterfactual_fractions", {"control": 0.0, "treatment": summary.get("counterfactual_fraction", 0.1)})
    labels = {arm: f"추가 샘플 {fraction:.0%}" for arm, fraction in fractions.items()}
    labels["source"] = "업데이트 전"
    lines = [
        "# Counterfactual MC 최소 수정 대조 실험", "",
        "기존 checkpoint에서 한 번의 MSE 업데이트를 비교했습니다. 후보 행동 MC 데이터를 추가하는 효과를 확인하는 소형 실험입니다.", "",
        "## 실험 조건", "",
        f"- Source: `{summary['source_checkpoint']}`",
        f"- Worker {summary['worker_count']}, horizon {summary['horizon_days']}일, 하루 480분, WAIT 비활성",
        f"- 학습 {summary['training_episodes']} episode, epsilon {summary['epsilon']}, CPU {summary['process_count']} process, GPU {summary['gpu']}",
        f"- 동일 초기 가중치, 새 Adam optimizer, 동일 rollout/holdout/SGD seed, 각 1회 update. 같은 epoch 예산 안에서 공통 holdout MSE가 가장 낮은 epoch의 모델과 optimizer를 복원",
        f"- 대체 행동 {summary['alternative_training_samples']}개. 원래/대체 행동 샘플의 nominal 비중: {', '.join(f'{value:.0%}' for value in fractions.values())}",
        "- 후속 정책은 source checkpoint와 동일 epsilon. prefix 재현 후 정책 RNG 상태를 복원하고 pre-state 일치를 검사",
        "- holdout episode의 원래/대체 행동은 모두 학습에서 제외. TD, ranking loss, shaping, validation gate 없음",
        "", "## Greedy 생산량", "",
        "| 모델 | 평균 | 표준편차 | seed별 제품 수 |", "|---|---:|---:|---|",
    ]
    for name, row in evaluation.items():
        lines.append(f"| {labels.get(name, name)} | {row['mean']:.2f} | {row['std']:.2f} | {row['products']} |")
    lines += ["", "## MSE 업데이트", "",
              "| 모델 | 공통 holdout MSE | 공통 rollout 전체 MSE | 관측 행동 쌍 MSE | GPU 업데이트(초) |",
              "|---|---:|---:|---:|---:|"]
    for row in summary["updates"]:
        lines.append(f"| {labels.get(row['arm'], row['arm'])} | {row['holdout_mse']:.4f} | {row['common_batch_mse']:.4f} | {row['training_pair_mse']:.4f} | {row['gpu_update_sec']:.3f} |")
    for row in summary["updates"]:
        if "selected_epoch" in row:
            lines.append(f"\n{labels.get(row['arm'], row['arm'])}: {row['epochs']} epoch 학습 후 {row['selected_epoch']} epoch 모델 선택 (저장된 optimizer step {row['selected_sgd_steps']}).")
    lines += ["", "공통 rollout 전체 MSE에는 학습과 holdout episode가 모두 포함됩니다. 관측 행동 쌍 MSE는 학습용 rollout에서 포착한 모든 원래/대체 행동의 오차이며 독립적인 정책 성능 지표가 아닙니다."]
    if "sampling" in summary:
        sampling = summary["sampling"]
        lines += ["", f"일반 학습 샘플 {sampling['ordinary_training_samples']}개, 학습용 원래/대체 행동 샘플 {sampling['paired_training_samples']}개. 학습 parent episode {sampling['training_episode_ids']}, holdout parent episode {sampling['holdout_episode_ids']}.",
                  "", "| 조건 | 실제 샘플 비중 | epoch당 추가 샘플 추출 수 | epoch당 SGD step 수 |", "|---|---:|---:|---:|"]
        for row in summary["updates"]:
            lines.append(f"| {labels[row['arm']]} | {row['actual_counterfactual_fraction']:.3%} | {row['counterfactual_draws_per_epoch']} | {row['sgd_steps_per_epoch']} |")
    comparisons = summary.get("comparisons_vs_control")
    if comparisons is None:
        comparisons = {"treatment": summary["treatment_minus_control"]}
    for arm, comparison in comparisons.items():
        lines += ["", f"{labels.get(arm, arm)} - 0% 평균: {comparison['mean_difference']:+.2f}개. Paired bootstrap 95% CI: {comparison['paired_bootstrap_ci95']}. Win/tie/loss: {comparison['wins']}/{comparison['ties']}/{comparison['losses']}."]
    for name, comparison in summary.get("comparisons_between_treatments", {}).items():
        lines += ["", f"{name}: {comparison['mean_difference']:+.2f}개. Paired bootstrap 95% CI: {comparison['paired_bootstrap_ci95']}."]
    lines += ["", "95% 구간은 각 비교의 개별 paired bootstrap 구간이며 다중비교 보정은 적용하지 않았습니다. 평가 seed가 5개인 탐색적 실험이므로 통계적 성능 우위 확정에 사용하지 않습니다."]
    lines += ([
        "", "## 행동 진단", "",
        "평가 seed의 동일 상태에서 원래 행동과 대체 행동을 각각 실행하고, 이후에는 source greedy 정책으로 종료까지 진행했습니다. 모든 모델을 동일한 두 후보에 직접 적용하여 beam pruning을 제외했습니다.",
        "", "| 모델 | 평균 관측 행동 격차 | 평균 선택 regret | 진단 상태 수 |", "|---|---:|---:|---:|",
    ] if diagnostics else [])
    for row in diagnostics:
        lines.append(f"| {row['model']} | {row['observed_action_gap_mean']:.2f} | {row['observed_selection_regret_mean']:.2f} | {row['state_count']} |")
    if diagnostics:
        lines += ["", "행동 진단은 상태당 각 행동 1회 실행의 경험적 값입니다. 기대 가치의 정답이나 전역 최적값이 아닙니다. Source greedy 후속 정책 기준의 선택 결과이며, epsilon-greedy 학습 target의 정확도와 구분합니다."]
    else:
        lines += ["", "이번 실험에서는 별도 행동 진단 rollout을 실행하지 않았습니다. 기존 실험의 행동 진단값을 재사용하지 않습니다."]
    lines += [
        "", "## 검증과 해석", "",
        f"- 동일 행동 replay 재현: {summary['identity_replay_passed']}",
        f"- Source 가중치 불변: {summary['source_unchanged']}",
        f"- 모든 episode: 동일 환경 fingerprint, full horizon, raw return=products, 명시적 WAIT=0 검사 통과",
        f"- 소요시간: {summary['wall_sec'] / 60:.1f}분. 상세 event/replay 및 raw training tensor 파일을 저장하지 않음",
        "", summary["conclusion"], "",
        "한정된 행동 쌍에서 MSE나 선택 regret이 줄어도 전체 상태 분포의 행동 순위가 개선되었다는 뜻은 아닙니다. 이번 실험은 같은 초기 모델에서 추가 MC 샘플을 넣는 한 번의 업데이트 효과만 비교하며, 가치함수가 완전히 정확한지 또는 장기 성능 저하의 원인이 무엇인지를 확정하지 않습니다.", "",
        "소수 평가 seed와 한 번의 업데이트 결과이므로 장기 학습 수렴이나 일반적인 성능 우위를 입증하지 않습니다. 평가 seed를 보고 모델이나 설정을 재선정하지 않았습니다.",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    table = "<table><tr><th>모델</th><th>평균 제품 수</th><th>표준편차</th><th>Seed별 제품 수</th></tr>"
    for name, row in evaluation.items():
        table += f"<tr><td>{html.escape(labels.get(name, name))}</td><td>{row['mean']:.2f}</td><td>{row['std']:.2f}</td><td>{row['products']}</td></tr>"
    table += "</table>"
    chart = _svg_chart(
        [(labels.get(name, name), row["products"], ["#777777", "#247ac1", "#148456", "#b64c51"][index % 4])
         for index, (name, row) in enumerate(evaluation.items())],
        x_values=summary["evaluation_seeds"], x_label="평가 seed",
        y_label=f"{summary['horizon_days']}일 완료 제품 수", include_zero=True,
    )
    page = """<!doctype html><html lang="ko"><meta charset="utf-8"><title>Counterfactual MC A/B</title>
<style>body{font:16px/1.65 system-ui,sans-serif;margin:32px auto;padding:0 24px;max-width:1100px;color:#202628;background:#fff}h1{font-size:26px;overflow-wrap:anywhere}h2{font-size:20px}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}th,td{padding:8px;border-bottom:1px solid #ddd;text-align:left}svg{width:100%;height:auto}.grid-line{stroke:#ddd}.axis-line{stroke:#68777d}.tick,.axis-label{fill:#333;font-size:12px}.y-tick{text-anchor:end}.x-tick,.axis-label,.point-label{text-anchor:middle}.chart-legend{display:flex;gap:20px;flex-wrap:wrap}.chart-legend span{display:inline-flex;align-items:center;gap:8px}.chart-legend i{display:inline-block;width:18px;height:3px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 system-ui,sans-serif}</style><h1>Counterfactual MC 최소 수정 실험</h1>"""
    mse_chart = _svg_chart(
        [("공통 holdout MSE", [row["holdout_mse"] for row in summary["updates"]], "#247ac1")],
        x_values=[fractions[row["arm"]] * 100 for row in summary["updates"]],
        x_label="추가 행동 샘플 비중 (%)", y_label="MSE (제품 수 제곱)", include_zero=True,
    )
    page += "<div class='table-wrap'>" + table + "</div><h2>동일 평가 seed의 생산량</h2>" + chart
    page += "<p>같은 seed에서 세 조건의 5일 생산량을 비교합니다. 학습에 사용하지 않은 평가 seed이며, 각 점은 episode 한 번의 결과입니다.</p>"
    page += "<h2>공통 Holdout MSE</h2>" + mse_chart
    page += "<p>동일한 holdout episode에서 가치 예측 오차를 비교합니다. 낮을수록 오차가 작지만 생산량 개선을 보장하지는 않습니다. 가로축은 iteration이 아니라 샘플 비중입니다.</p>"
    page += "<pre>" + html.escape("\n".join(lines)) + "</pre></html>"
    (output / "diagnosis_dashboard.html").write_text(page, encoding="utf-8")


def run(config: dict, *, smoke: bool = False) -> Path:
    started = time.perf_counter()
    torch = require_torch()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for this controlled update experiment.")
    config = copy.deepcopy(config)
    if smoke:
        config.update(horizon_days=1, training_seeds=config["training_seeds"][:2],
                      evaluation_seeds=config["evaluation_seeds"][:2], capture_decisions=[20], process_count=2)
        config["training"].update(epochs=1, batch_size=128)
    training_seeds = list(config["training_seeds"])
    evaluation_seeds = list(config["evaluation_seeds"])
    if len(set(training_seeds)) != len(training_seeds) or len(set(evaluation_seeds)) != len(evaluation_seeds):
        raise ValueError("Episode seeds must be unique within each partition.")
    if set(training_seeds) & set(evaluation_seeds):
        raise ValueError("Training and evaluation seeds overlap.")
    device = torch.device("cuda:0")
    train_cfg = config["training"]
    arms = experiment_arms(train_cfg)
    include_source = bool(config.get("include_source_evaluation", True))
    action_diagnostics = bool(config.get("action_diagnostics", True))
    if action_diagnostics and not include_source:
        raise ValueError("Action diagnostics require source evaluation.")
    _configure_training_determinism(torch, int(train_cfg["seed"]))
    days, workers, processes = int(config["horizon_days"]), int(config["worker_count"]), int(config["process_count"])
    output = ROOT / config["output_root"] / (datetime.now().strftime("%Y%m%d_%H%M%S") + ("_smoke" if smoke else ""))
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    runtime = {"training": True, "force_random_policy": False, "allow_wait_action": False,
               "worker_order_strategy": "cyclic", "max_review_interval_min": 1.0, "beam_width": 64}
    world_cfg = _compose_episode_cfg(worker_count=workers, seed=training_seeds[0], days=days, adp_cfg=runtime)
    world = ManufacturingWorld(simpy.Environment(), world_cfg, InMemoryEventLogger(),
                               build_decision_module(experiment_cfg=world_cfg, decision_mode="simulation_based_adp"))
    source_path = (ROOT / config["checkpoint"]).resolve()
    source, manifest = load_checkpoint(source_path, world=world, device=device,
                                       wait_action_enabled=False, worker_order_strategy="cyclic")
    expected_fingerprint = manifest["environment_fingerprint"]
    del world
    source_state, source_hash = _snapshot_state_dict(source)
    model_cfg = manifest["model"]
    episode_rows, wave_rows = [], []
    phase_times = {}

    def collect(jobs, model_state):
        phase = jobs[0].phase
        jobs = [replace(job, wave_id=f"{phase}-W{index // processes + 1:02d}")
                for index, job in enumerate(jobs)]
        print(f"START {phase}: {len(jobs)} episodes", flush=True)
        results, batch, metrics = _run_rollout_jobs_parallel(
            jobs=jobs, model_state=model_state, model_cfg=model_cfg, process_count=processes,
            wave_size=processes, start_method="spawn", torch_threads=1, output_dir=output,
            episode_rows=episode_rows, wave_rows=wave_rows,
        )
        for result in results:
            if result.fingerprint["environment_fingerprint"] != expected_fingerprint:
                raise RuntimeError("Episode environment fingerprint mismatch.")
            if result.raw_return != result.products or result.wait_count:
                raise RuntimeError("Episode reward or WAIT contract violation.")
            if result.simulation_end_min != days * 480 or result.termination_reason != "completed_horizon":
                raise RuntimeError("Episode did not complete its configured horizon.")
        phase_times[phase] = metrics.wall_sec
        print(f"DONE {phase}: products={[r.products for r in results]}, wall={metrics.wall_sec:.1f}s", flush=True)
        return results, batch

    def jobs_for(seeds, phase, epsilon, *, capture, compact, offset, snapshot_hash):
        jobs = []
        for index, seed in enumerate(seeds):
            adp = dict(runtime)
            if capture:
                adp.update(_probe_capture_decision_thresholds=[config["capture_decisions"][index % len(config["capture_decisions"])]],
                           _probe_candidate_limit=int(config["candidate_limit"]))
            jobs.append(RolloutJob(offset + index, phase, 0, workers, seed, days, epsilon,
                                   False, adp, compact, 1.0, phase, snapshot_hash))
        return jobs

    origins, batch = collect(jobs_for(training_seeds, "training_rollout", config["epsilon"],
        capture=True, compact=True, offset=1000, snapshot_hash=source_hash), source_state)
    if any(len(origin.probe_records or []) != 1 for origin in origins):
        raise RuntimeError("Not every training episode captured a multi-action decision.")
    records = [origin.probe_records[0] for origin in origins]
    first = records[0]
    identity_job = make_branch(origins[0], first, {"assignment": first["selected_assignment"]},
        episode=1900, phase="identity_replay", runtime=runtime, days=days, epsilon=config["epsilon"],
        snapshot_hash=source_hash, collect=False)
    identities, _ = collect([identity_job], source_state)
    identity = identities[0]
    if (identity.products, identity.scrap, identity.decisions, identity.raw_return) != (
            origins[0].products, origins[0].scrap, origins[0].decisions, origins[0].raw_return):
        raise RuntimeError("Identical-action replay changed the episode trajectory.")
    if identity.probe_target_post_state != first["selected_post_state"]:
        raise RuntimeError("Identical-action post-state mismatch.")
    chosen = [alternative(record, origin.seed) for origin, record in zip(origins, records)]
    branch_jobs = [make_branch(origin, record, candidate, episode=2000 + index, phase="counterfactual_training",
        runtime=runtime, days=days, epsilon=config["epsilon"], snapshot_hash=source_hash, collect=True)
        for index, (origin, record, candidate) in enumerate(zip(origins, records, chosen))]
    branches, alternatives = collect(branch_jobs, source_state)
    pair_rows, anchor_indices = [], []
    for index, (origin, record, candidate, branch) in enumerate(zip(origins, records, chosen, branches)):
        if branch.probe_target_post_state != candidate["post_state"]:
            raise RuntimeError("Alternative afterstate did not match the encoded candidate.")
        positions = (batch.episode_ids == origin.episode).nonzero().flatten().tolist()
        anchor_indices.append(positions[int(record["decision_number"]) - 1])
        alternatives.episode_ids[index] = origin.episode
        if float(alternatives.targets[index]) != branch.products - record["products_before"]:
            raise RuntimeError("Alternative MC target is not the full remaining episode reward.")
        pair_rows.append({"parent_episode": origin.episode, "seed": origin.seed,
                         "decision": record["decision_number"], "time_min": record["time_min"],
                         "selected_target": origin.products - record["products_before"],
                         "alternative_target": branch.products - record["products_before"]})
    pairs = merge_compact_batches([select_samples(batch, anchor_indices), alternatives])
    _write_csv(output / "training_action_pairs.csv", pair_rows)
    del alternatives
    train_indices, holdout_indices = stratified_episode_split(
        batch, validation_fraction=float(train_cfg["holdout_fraction"]),
        rng=random.Random(int(train_cfg["seed"])),
    )
    training_ids = sorted({int(batch.episode_ids[index]) for index in train_indices})
    sampling = {"ordinary_training_samples": len(train_indices),
                "paired_training_samples": sum(int(value) in training_ids for value in pairs.episode_ids.tolist()),
                "training_episode_ids": training_ids,
                "holdout_episode_ids": sorted({int(batch.episode_ids[index]) for index in holdout_indices})}
    models = {"source": source} if include_source else {}
    fit_rows = []
    for arm, fraction in arms.items():
        _configure_training_determinism(torch, int(train_cfg["seed"]))
        model = copy.deepcopy(source)
        optimizer = torch.optim.Adam(model.parameters(), lr=float(train_cfg["learning_rate"]))
        torch.cuda.synchronize()
        update_start = time.perf_counter()
        loss, epochs = fit_mc_value(
            model=model, optimizer=optimizer, samples=batch, device=device,
            batch_size=int(train_cfg["batch_size"]), max_epochs=int(train_cfg["epochs"]),
            gradient_clip=float(train_cfg["gradient_clip"]), patience=int(train_cfg["epochs"]) + 1,
            validation_episode_fraction=float(train_cfg["holdout_fraction"]),
            rng=random.Random(int(train_cfg["seed"])),
            counterfactual_samples=pairs if fraction > 0 else None,
            counterfactual_fraction=fraction,
        )
        torch.cuda.synchronize()
        update_sec = time.perf_counter() - update_start
        counts = sampling_counts(len(train_indices), int(train_cfg["batch_size"]), fraction)
        selected_steps = max((int(value["step"]) for value in optimizer.state.values() if "step" in value), default=0)
        row = {"arm": arm, "counterfactual_fraction": fraction,
               **counts, "selected_sgd_steps": selected_steps,
               "selected_epoch": selected_steps // counts["sgd_steps_per_epoch"],
               "holdout_mse": loss, "epochs": epochs, "gpu_update_sec": update_sec,
               "common_batch_mse": regression_mse(model, batch, device),
               "training_pair_mse": regression_mse(model, pairs, device)}
        fit_rows.append(row)
        models[arm] = model
        arm_manifest = copy.deepcopy(manifest)
        arm_manifest.update(checkpoint_id=f"CFMC-{output.name}-{arm}",
                            counterfactual_experiment={"source_checkpoint": str(source_path), "arm": arm,
                            "additional_updates": 1, "config": config, "fit": row})
        save_checkpoint(output / f"{arm}.pt", model=model, optimizer=optimizer, manifest=arm_manifest)
        print(f"UPDATE {arm}: {row}", flush=True)
    _write_csv(output / "update_metrics.csv", fit_rows)
    compact_mib = (batch.memory_bytes + pairs.memory_bytes) / 1024**2
    del batch, pairs
    gc.collect()

    eval_results = {}
    for index, (arm, model) in enumerate(models.items()):
        state, digest = _snapshot_state_dict(model)
        results, _ = collect(jobs_for(evaluation_seeds, f"evaluation_{arm}", 0.0,
            capture=arm == "source" and action_diagnostics, compact=False, offset=3000 + index * 100, snapshot_hash=digest), state)
        eval_results[arm] = results
    diagnostic_jobs, diagnostic_records, diagnostic_choices = [], [], []
    for index, origin in enumerate(eval_results.get("source", []) if action_diagnostics else []):
        if len(origin.probe_records or []) != 1:
            raise RuntimeError("Evaluation source did not capture a diagnostic state.")
        record = origin.probe_records[0]
        candidate = alternative(record, origin.seed)
        diagnostic_records.append(record)
        diagnostic_choices.append(candidate)
        diagnostic_jobs.append(make_branch(origin, record, candidate, episode=4000 + index,
            phase="diagnostic_alternative", runtime=runtime, days=days, epsilon=0.0,
            snapshot_hash=source_hash, collect=False))
    diagnostic_branches = []
    if diagnostic_jobs:
        diagnostic_branches, _ = collect(diagnostic_jobs, source_state)
    diagnostic_rows = []
    # Score on the same backend as rollout inference, especially for near ties.
    torch.set_num_threads(1)
    diagnostic_models = {arm: copy.deepcopy(model).to("cpu").eval() for arm, model in models.items()} if diagnostic_jobs else {}
    for origin, record, candidate, branch in zip(eval_results.get("source", []), diagnostic_records,
                                               diagnostic_choices, diagnostic_branches):
        if branch.probe_target_post_state != candidate["post_state"]:
            raise RuntimeError("Diagnostic alternative post-state mismatch.")
        states = [deserialize_state(record["selected_post_state"]), deserialize_state(candidate["post_state"])]
        targets = [origin.products - record["products_before"], branch.products - record["products_before"]]
        for arm, model in diagnostic_models.items():
            predictions = predict_values(model, states, "cpu")
            selected = int(np.argmax(predictions))
            diagnostic_rows.append({"model": arm, "seed": origin.seed, "time_min": record["time_min"],
                "target_a": targets[0], "target_b": targets[1], "prediction_a": float(predictions[0]),
                "prediction_b": float(predictions[1]), "selected": selected,
                "observed_action_gap": abs(targets[0] - targets[1]),
                "observed_selection_regret": max(targets) - targets[selected]})
    if diagnostic_rows:
        _write_csv(output / "diagnostic_actions.csv", diagnostic_rows)
    diagnostics = []
    for arm in diagnostic_models:
        rows = [row for row in diagnostic_rows if row["model"] == arm]
        diagnostics.append({"model": arm, "state_count": len(rows),
            "observed_action_gap_mean": float(np.mean([row["observed_action_gap"] for row in rows])),
            "observed_selection_regret_mean": float(np.mean([row["observed_selection_regret"] for row in rows]))})
    evaluation = {}
    for arm, results in eval_results.items():
        products = [result.products for result in results]
        evaluation[arm] = {"mean": float(np.mean(products)), "std": float(np.std(products, ddof=1)), "products": products}
        if arm != "source":
            checkpoint = torch.load(output / f"{arm}.pt", map_location="cpu", weights_only=False)
            checkpoint["manifest"].update(
                validation_completed_products_avg=evaluation[arm]["mean"],
                validation_completed_products_std=evaluation[arm]["std"],
                validation_completed_products_avg_by_worker_count={str(workers): evaluation[arm]["mean"]},
                evaluation_context="small_counterfactual_mc_ablation_not_checkpoint_selection",
            )
            torch.save(checkpoint, output / f"{arm}.pt")
    comparisons = {arm: paired_summary(evaluation[arm]["products"], evaluation["control"]["products"])
                   for arm in arms if arm != "control"}
    positive = [arm for arm, value in comparisons.items() if value["paired_bootstrap_ci95"][0] > 0]
    negative = [arm for arm, value in comparisons.items() if value["paired_bootstrap_ci95"][1] < 0]
    between = {f"{left} - {right}": paired_summary(evaluation[left]["products"], evaluation[right]["products"])
               for index, left in enumerate(list(comparisons)) for right in list(comparisons)[index + 1:]}
    if positive:
        conclusion = "이번 소형 실험에서는 추가 MC 샘플이 기본 MSE 업데이트보다 높은 생산량을 보였습니다. 장기 수렴과 다른 학습 batch에서도 재현되는지는 별도 검증이 필요합니다."
    elif len(negative) == len(comparisons):
        conclusion = "이번 소형 실험에서는 추가 MC 샘플의 생산성이 대조군보다 낮았습니다. 이 설정을 기본 학습으로 채택할 근거가 없습니다. 다만 이 결과만으로 행동별 샘플 부족이라는 원인 가설 자체를 배제할 수는 없습니다."
    else:
        conclusion = "이번 소형 실험에서는 추가 MC 샘플의 생산성 개선이 입증되지 않았습니다. 평균 차이와 MSE 결과는 제한된 표본의 예비 결과로 해석해야 합니다."
    summary = {"source_checkpoint": str(source_path), "source_checkpoint_id": manifest["checkpoint_id"],
        "worker_count": workers, "horizon_days": days, "training_episodes": len(origins),
        "alternative_training_samples": len(branches), "epsilon": config["epsilon"],
        "counterfactual_fractions": arms, "sampling": sampling, "process_count": processes,
        "gpu": torch.cuda.get_device_name(0), "evaluation_seeds": evaluation_seeds, "evaluation": evaluation,
        "comparisons_vs_control": comparisons, "comparisons_between_treatments": between,
        "diagnostics": diagnostics, "updates": fit_rows,
        "identity_replay_passed": True, "source_unchanged": _snapshot_state_dict(source)[1] == source_hash,
        "compact_batch_mib": compact_mib, "phase_wall_sec": phase_times,
        "total_simulation_episodes": len(episode_rows), "wall_sec": time.perf_counter() - started,
        "smoke": smoke, "conclusion": conclusion}
    if not summary["source_unchanged"]:
        raise RuntimeError("The source checkpoint model was modified.")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(output, summary, diagnostics)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"REPORT {output / 'report.md'}", flush=True)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.yaml"))
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    run(OmegaConf.to_container(OmegaConf.load(args.config), resolve=True), smoke=args.smoke)
