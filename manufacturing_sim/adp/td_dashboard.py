"""TD monitoring: policy outcomes, value accuracy, then optional diagnostics."""
from __future__ import annotations

import csv
import html
import json
import math
from pathlib import Path
import statistics
from typing import Any

from .td_formulas import FORMULA_CSS, render_formula


def number(row: dict[str, Any], key: str) -> float | None:
    try:
        value = float(row[key])
        return value if math.isfinite(value) else None
    except (KeyError, TypeError, ValueError):
        return None


def cell(value: Any) -> str:
    if value is None or isinstance(value, float) and not math.isfinite(value):
        return "N/A"
    if isinstance(value, float):
        return format(value, ".3g" if 0 < abs(value) < .001 else ".3f")
    return html.escape(str(value))


def _interval(mean: float, std: float | None, count: float | None) -> tuple[float | None, float | None]:
    if std is None or std < 0 or count is None or count < 2:
        return None, None
    half = 1.96 * std / math.sqrt(count)
    return mean - half, mean + half


def paired_production_changes(episodes: list[dict[str, Any]], iterations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Pair completed screening checkpoints against I0, never against final/test seeds."""
    checkpoints = {int(float(row["iteration"])): row for row in iterations
                   if number(row, "iteration") is not None and number(row, "validation_products_mean") is not None}
    if 0 not in checkpoints:
        return [], []
    grouped: dict[int, dict[tuple[int, int], float]] = {}
    bad: set[int] = set()
    warnings = []
    for row in episodes:
        if row.get("phase") != "screening_validation" or number(row, "iteration") is None:
            continue
        iteration = int(float(row["iteration"]))
        if iteration not in checkpoints:
            continue  # An unfinished validation wave is not a checkpoint estimate.
        if any(number(row, key) is None for key in ("worker_count", "seed", "products")):
            bad.add(iteration)
            continue
        key = (int(float(row["worker_count"])), int(float(row["seed"])))
        group = grouped.setdefault(iteration, {})
        if key in group or row.get("termination_reason", "completed_horizon") != "completed_horizon":
            bad.add(iteration)
        group[key] = float(row["products"])
    for iteration, row in checkpoints.items():
        group = grouped.get(iteration, {})
        count = number(row, "validation_episode_count")
        if (not group or count != len(group)
                or not math.isclose(statistics.fmean(group.values()), float(row["validation_products_mean"]), abs_tol=1e-6)):
            bad.add(iteration)
    if 0 in bad:
        return [], ["초기 checkpoint의 seed별 원본과 집계가 없거나 일치하지 않아 paired 개선량을 표시하지 않습니다."]
    baseline = grouped[0]
    if len({worker for worker, _seed in baseline}) != 1:
        return [], ["서로 다른 worker 수가 섞여 있어 paired CI를 합산하지 않습니다. Worker별 학습 결과에서 확인하세요."]
    result = []
    for iteration in sorted(checkpoints):
        current = grouped.get(iteration, {})
        if iteration in bad or set(current) != set(baseline):
            warnings.append(f"Iteration {iteration}: seed/worker 집합 또는 집계 불일치로 paired 개선량을 제외했습니다.")
            continue
        differences = [current[key] - baseline[key] for key in sorted(baseline)]
        mean = statistics.fmean(differences)
        std = statistics.stdev(differences) if len(differences) > 1 else None
        low, high = _interval(mean, std, len(differences))
        result.append({"iteration": iteration, "paired_mean": mean, "paired_std": std,
                       "paired_n": len(differences), "ci_low": low, "ci_high": high,
                       "win": sum(value > 0 for value in differences),
                       "tie": sum(value == 0 for value in differences),
                       "loss": sum(value < 0 for value in differences)})
    return result, warnings


def _table(rows: list[dict[str, Any]], fields: list[tuple[str, str]]) -> str:
    return ("<div class='table-scroll'><table><thead><tr>"
            + "".join(f"<th>{html.escape(label)}</th>" for label, _ in fields)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{cell(row.get(key))}</td>" for _, key in fields) + "</tr>" for row in rows)
            + "</tbody></table></div>")


def _panel(key: str, title: str, graph: str, interpretation: str, caveats: str, extra: str = "") -> str:
    return (f"<section class='panel' id='{key}' data-metric-id='{key}'><h3>{html.escape(title)}</h3>{graph}"
            f"<p><strong>해석</strong> {html.escape(interpretation)}</p>"
            + render_formula(key)
            + f"<p class='caveat'><strong>유의사항</strong> {html.escape(caveats)}</p>{extra}</section>")


def render_td_dashboard(output: Path, episodes: list[dict[str, Any]], iterations: list[dict[str, Any]],
                        waves: list[dict[str, Any]], summary: dict[str, Any],
                        *, filename: str = "training_dashboard.html") -> Path:
    from .train import _svg_chart
    from .live import atomic_text, read_json

    iterations = sorted((dict(row) for row in iterations), key=lambda row: float(row.get("iteration", 0)))
    # Rebuild optional entropy from its actual observations, including older CSVs
    # that encoded an initial random batch's undefined entropy as zero.
    for row in iterations:
        collected = [e for e in episodes if e.get("phase") in {"initial_random", "warm_start_replay", "policy_iteration"}
                     and number(e, "iteration") == number(row, "iteration")]
        if collected and all("beam_value_entropy_decision_count" in e for e in collected):
            observed = [number(e, "beam_value_entropy_avg") for e in collected
                        if (number(e, "beam_value_entropy_decision_count") or 0) > 0]
            observed = [value for value in observed if value is not None]
            row["beam_entropy"] = statistics.fmean(observed) if observed else None
    validity = read_json(output / "result_validity.json")
    banner = ("<aside role='alert'><strong>시뮬레이션 오류 수정 전 학습 결과</strong><p>"
              + html.escape(str(validity.get("message", ""))) + "</p></aside>"
              if validity.get("status") == "requires_rerun" else "")
    paired, warnings = paired_production_changes(episodes, iterations)
    if summary.get("wait_action_enabled") is False:
        violations = sum(number(row, "voluntary_wait_count") or 0 for row in iterations)
        if violations:
            warnings.append(f"WAIT 비활성 계약인데 자발적 WAIT가 {violations:g}회 기록되었습니다.")
        if any((number(row, "idle_metrics_version") or 1) < 2 for row in episodes):
            warnings.append("이전 로그는 WAIT 비활성 시 후보 없는 강제 유휴를 기록하지 않았습니다. 해당 유휴 지표의 0은 실제 유휴가 없었다는 뜻이 아니며 원본 로그 없이 복원할 수 없습니다. 생산량과 보상에는 영향이 없습니다.")
    warning_html = "".join(f"<p role='alert'>{html.escape(message)}</p>" for message in warnings)

    def chart(keys: list[tuple[str, str, str]], y_label: str, rows=None, *, x_key="iteration",
              x_label="Iteration (0 = 초기 학습)", ci=None, dashed=None) -> str:
        rows = iterations if rows is None else rows
        series, xs, intervals = [], {}, {}
        for label, key, color in keys:
            valid = [row for row in rows if number(row, key) is not None and number(row, x_key) is not None]
            series.append((label, [float(row[key]) for row in valid], color))
            xs[label] = [float(row[x_key]) for row in valid]
            if ci and key in ci:
                low, high = ci[key]
                intervals[label] = ([number(row, low) for row in valid], [number(row, high) for row in valid])
        graph = _svg_chart(series, x_values=None, series_x_values=xs, x_label=x_label, y_label=y_label,
                           include_zero=True, error_ranges=intervals, dashed_series=set(dashed or []))
        return graph.replace("No data", "집계 가능한 완료 데이터가 없습니다.")

    validation = []
    for row in iterations:
        mean = number(row, "validation_products_mean")
        if mean is not None:
            low, high = _interval(mean, number(row, "validation_products_std"), number(row, "validation_episode_count"))
            validation.append({**row, "ci_low": low, "ci_high": high,
                               "validation_products_std": number(row, "validation_products_std")
                               if (number(row, "validation_episode_count") or 0) >= 2 else None})
    production_rows = [{**row, **next((v for v in validation if v["iteration"] == row["iteration"]), {})} for row in iterations]
    warm = bool(summary.get("warm_start_enabled", False))
    initial = ("회색 0은 source 정책으로 replay를 채운 warm-start 수집이며 초기 업데이트를 생략할 수 있습니다. "
               if warm else "회색 0은 초기 Random Feasible 수집, 보라색 0은 그 자료로 초기 업데이트한 정책입니다. ")
    panels = []
    panels.append(_panel("production", "1. 정책 생산성: 학습 수집과 Greedy 검증",
        chart([("학습용 rollout (업데이트 전)", "rollout_products_mean", "#879aaa"),
               ("Greedy validation (업데이트 후)", "validation_products_mean", "#a955cf")],
              f"{summary.get('horizon_days', 5)}일 완료 제품 수", production_rows,
              ci={"validation_products_mean": ("ci_low", "ci_high")}, dashed=["학습용 rollout (업데이트 전)"]),
        "정책 성능은 보라색의 고정 validation seed 생산량으로 봅니다. 회색 k는 업데이트 k 이전 정책의 ε-greedy 수집이고, 보라색 k는 업데이트 후 ε=0 평가입니다. " + initial,
        "오차막대는 평균의 근사 95% CI이며 episode 산포 범위가 아닙니다. 작은 seed 수에서는 불확실성이 큽니다. N=1은 막대를 생략합니다. "
        "회색·보라색은 정책과 seed가 달라 간격만으로 과적합을 판정할 수 없습니다. 고정 screening seed는 학습 gradient에는 쓰지 않지만 checkpoint 선택에는 쓰므로 최종 독립 test와 다릅니다.",
        _table(validation[-1:], [("최근 검증 iteration", "iteration"), ("평균", "validation_products_mean"),
                                ("표준편차", "validation_products_std"), ("Seed 수", "validation_episode_count")])))
    panels.append(_panel("paired-gain", "2. 초기 Checkpoint 대비 생산량 변화",
        chart([("동일 seed의 생산량 차이", "paired_mean", "#138d75")], "완료 제품 차이 (현재 − I0)", paired,
              ci={"paired_mean": ("ci_low", "ci_high")}),
        "같은 worker·seed에서 초기 checkpoint보다 실제로 더 생산했는지를 비교합니다. 양수는 개선, 음수는 저하입니다. 초기 모델을 다시 평가하거나 추가 episode를 실행하지 않고 기존 screening 원본을 짝지었습니다.",
        "두 checkpoint의 worker·seed 집합과 완료 episode 수가 정확히 같을 때만 계산합니다. 독립 평균 CI 두 개를 빼는 계산이 아닙니다. "
        "I0의 0은 자기 자신과의 차이입니다. 반복 screening·선택과 같은 I0 재사용으로 점들이 의존하므로 CI를 최종 유의성 검정이나 전체 구간 동시 CI로 보지 마세요.",
        warning_html + _table(paired[-1:], [("Iteration", "iteration"), ("평균 차이", "paired_mean"), ("차이 표준편차", "paired_std"),
                                           ("짝 수", "paired_n"), ("Win", "win"), ("Tie", "tie"), ("Loss", "loss")])))
    panels.append(_panel("td-fit", "3. n-step TD 적합 오차",
        chart([("학습 표본", "td_train_mse", "#c6811d"), ("Episode holdout", "td_holdout_mse", "#138d75")], "TD MSE (제품 수²)"),
        "현재 bootstrap target에 가치망이 얼마나 잘 맞는지를 봅니다. 학습 오차만 줄고 holdout 오차가 커지면 replay 표본에 대한 과적합 가능성을 점검합니다.",
        "Greedy 행동 map은 해당 업데이트 시작 때 고정하고 endpoint 값은 target network로 계산합니다. Episode ID가 10의 배수인 표본은 holdout이며 gradient에 쓰지 않습니다. "
        "표본이 없는 holdout은 N/A입니다. 업데이트마다 replay와 target이 달라지므로 같은 시험지를 반복한 loss가 아닙니다. 낮은 TD MSE만으로 실제 미래 생산량이나 행동 순위가 정확하다고 결론낼 수 없습니다.",
        _table(iterations[-1:], [("TD 학습 표본", "td_train_sample_count"), ("TD holdout 표본", "td_holdout_sample_count")])))
    calibration = [row for row in iterations if number(row, "greedy_mc_rmse") is not None]
    panels.append(_panel("future-error", "4. 실제 미래 생산량 예측 오차",
        chart([("Greedy MC RMSE", "greedy_mc_rmse", "#c6811d"), ("평균 편향", "greedy_mc_bias", "#d75b48")], "잔여 완료 제품 수"),
        "학습에 쓰지 않은 greedy validation에서 실제 잔여 생산량을 얼마나 정확히 예측하는지 봅니다. RMSE는 작을수록 좋고, 편향은 0에 가까울수록 좋습니다. "
        "양의 편향은 과대예측, 음의 편향은 과소예측입니다. 기존 평균 예측·MC 평균 그래프는 아래 가치 보정 표로 합쳤습니다.",
        "J는 validation의 의사결정 표본 수입니다. Episode 평균 오차가 아니라 모든 decision을 합치므로 decision이 많은 episode의 가중치가 큽니다. "
        "MC return은 진단 전용이며 TD 학습 target에 섞지 않습니다. 작은 평균 편향만으로 개별 오차가 작다고 볼 수 없고, 정확한 절대값도 같은 상태의 행동 순위 정확성을 보장하지 않습니다. 표는 시작 시점의 총 생산량이 아닙니다.",
        "<h4>미래 생산량 가치 보정</h4>" + _table(calibration[-1:], [("Iteration", "iteration"), ("가치망 평균 예측", "greedy_mc_prediction_mean"),
                                      ("실제 MC 평균", "greedy_mc_target_mean"), ("Decision 수", "greedy_mc_sample_count")])))

    details = []
    target_graph = ("<h4>실제 관측 step 수</h4>" + chart([("평균 n", "effective_n_mean", "#227ab7")], "의사결정 step 수")
                    + "<h4>같은 TD 구간의 simulation 시간</h4>" + chart([("평균 시간 범위", "td_span_min_mean", "#138d75")], "Simulation 분"))
    details.append(_panel("target-span", "5. TD target의 관측 범위와 중단 사유", target_graph,
        "설정 n보다 실제 관측 길이가 짧아지는 이유를 봅니다. 이벤트 간 시간이 다르므로 step 수와 simulation 분을 별도 축으로 표시합니다. "
        "평균 step이 작으면 n을 올리기 전에 off-policy 중단 비율을 확인해야 합니다.",
        "중간 행동 비교는 업데이트 시작 online network의 고정 greedy map 기준입니다. 평균 구간은 미래를 완전히 예측하는 lookahead 길이가 아닙니다. "
        "n 도달·정책 불일치·terminal은 코드의 우선순위에 따라 배타적으로 기록되며 세 비율 합은 1입니다. n은 분이 아닌 decision 개수입니다.",
        _table([row for row in iterations if number(row, "effective_n_mean") is not None][-1:],
               [("n 최소", "effective_n_min"), ("n 최대", "effective_n_max"), ("정책 불일치 비율", "td_off_policy_cut_ratio"),
                ("n 도달 비율", "td_n_limit_ratio"), ("Terminal 비율", "td_terminal_ratio")])))
    details.append(_panel("replay", "6. Replay 보유량과 업데이트 사용량",
        chart([("보유 episode", "replay_episode_count", "#227ab7"),
               ("현재 수집", "update_current_episode_count", "#c6811d"), ("과거 replay", "update_history_episode_count", "#a955cf")], "Episode 수"),
        "Replay에 남아 있는 양과 한 번의 업데이트에 실제로 선택한 양을 구분합니다. 현재 수집과 과거 episode가 계획한 비율로 들어가는지 확인합니다.",
        "선택된 episode에도 holdout이 포함되므로 모든 표본이 gradient에 쓰이지는 않습니다. Warm-start I0의 epoch=0이면 fill만 하고 업데이트는 하지 않습니다. "
        "Replay MiB는 전체 RAM이 아니며 Python overhead·복사본·GPU 메모리는 별도입니다. 메모리와 학습률·epsilon은 아래 운영 표에서 확인합니다.",
        _table(iterations[-1:], [("업데이트 episode 합계", "update_episode_count"), ("Replay MiB", "replay_mib"),
                                ("학습용 복사본 MiB", "update_mib")])) )
    details.append(_panel("ood", "7. 관측 범위 밖 행동과 추가 예측 편향",
        "<h4>OOD 선택 비율</h4>" + chart([("OOD 선택률", "ood_selection_rate", "#d75b48")], "선택 비율 (0~1)")
        + "<h4>관측 범위 대비 추가 편향</h4>" + chart([("OOD − 관측 범위", "ood_overestimation_excess", "#a955cf")], "잔여 제품 수 차이"),
        "직전 수집 batch와 다른 상태·행동이 선택되는 정도와 그 선택의 추가 편향을 함께 봅니다. 둘 다 크면 분포 이탈과 과대예측 가능성을 추가 조사할 근거가 됩니다.",
        "이 진단은 이번 업데이트 이전 정책이 수집한 값이며 업데이트 후 validation과 시점이 다릅니다. G는 이후 ε-greedy trajectory의 MC return입니다. "
        "과거 전체 데이터에 한 번도 없었다는 뜻이 아니며, reference가 없거나 어느 한 집단이 비면 N/A입니다. 미선택 행동과 직접 비교하지 않아 인과적 행동 우열을 증명하지 않습니다."))
    completed_waves = [{**row, "wave": index + 1} for index, row in enumerate(waves) if row.get("status") == "completed"]
    phases = [("초기 / warm-start", {"initial_random", "warm_start_replay"}, "#879aaa"),
              ("Policy 수집", {"policy_iteration"}, "#227ab7"),
              ("Screening", {"screening_validation"}, "#a955cf"),
              ("Final", {"final_selection_validation"}, "#138d75")]
    phase_rows, phase_keys = [], []
    for index, (label, names, color) in enumerate(phases):
        key = f"phase_{index}_wall_sec"
        phase_keys.append((label, key, color))
        phase_rows.extend({**row, key: number(row, "wall_sec")} for row in completed_waves if row.get("phase") in names)
    wave_summary = []
    for label, names, _ in phases:
        rows = [row for row in completed_waves if row.get("phase") in names]
        if not rows:
            continue
        elapsed = sum(number(row, "wall_sec") or 0 for row in rows)
        count = sum(number(row, "episode_count") or 0 for row in rows)
        valid_slots = all(number(row, "active_process_count") is not None and number(row, "episode_elapsed_sum_sec") is not None for row in rows)
        slots = sum((number(row, "wall_sec") or 0) * (number(row, "active_process_count") or 0) for row in rows)
        busy = sum(number(row, "episode_elapsed_sum_sec") or 0 for row in rows)
        wave_summary.append({"phase": label, "waves": len(rows), "episodes": count, "wall_sec": elapsed,
                             "episodes_per_hour": count * 3600 / elapsed if elapsed else None,
                             "utilization": busy / slots if valid_slots and slots else None})
    details.append(_panel("runtime", "8. 병렬 수집 소요시간과 처리량",
        chart(phase_keys, "Wave wall-clock 초", phase_rows, x_key="wave", x_label="Wave 번호 (실행 순서)"),
        "학습과 validation wave를 색으로 구분해 어느 단계에서 수집 시간이 증가하는지 봅니다. GPU update·target 구성 시간은 별도 표로 내려 중복 시간 그래프를 줄였습니다.",
        "Elapsed는 process 시간 합이 아니므로 process 수로 나누지 않습니다. 슬롯 활용률은 CPU 사용률이나 단일 process 대비 실측 speedup이 아닙니다. "
        "Partial wave는 슬롯 수가 다릅니다. Phase 표는 완료 wave만 포함하며 pool 초기화·wave 사이 처리·가치망 학습을 제외하므로 전체 end-to-end 시간과 다릅니다. 같은 phase의 연결선도 중간에 다른 phase가 끼어 있을 수 있습니다.",
        _table(wave_summary, [("Phase", "phase"), ("Wave", "waves"), ("Episode", "episodes"), ("Wave 초 합", "wall_sec"),
                              ("Episode/hour", "episodes_per_hour"), ("슬롯 활용률", "utilization")])))

    def schedule_text(key: str) -> str:
        points = summary.get(key, [])
        if not isinstance(points, list):
            return str(points)
        return " -> ".join(f"I{int(p['iteration'])}:{float(p['value']):.2g}" for p in points
                           if isinstance(p, dict) and number(p, "iteration") is not None and number(p, "value") is not None)

    meta = summary.get("training_device_metadata", {})
    overview = [("상태", summary.get("status")), ("Training episode", summary.get("training_episode_count")),
                ("Validation episode", summary.get("validation_episode_count")), ("업데이트 수", summary.get("value_update_count")),
                ("Best iteration", summary.get("best_iteration")), ("최종 별도 seed 생산량", summary.get("best_validation_completed_products_avg"))]
    if summary.get("early_stopping", {}).get("enabled"):
        completed = summary.get("completed_policy_iterations")
        overview.extend([("완료 / 상한 iteration", f"{completed if completed is not None else '-'} / {summary.get('policy_iterations')}"),
                         ("학습 종료 사유", summary.get("stop_reason") or "학습 중")])
    settings = [("n 상한", summary.get("n_step")), ("CPU processes / wave", f"{summary.get('configured_process_count')} / {summary.get('wave_size')}"),
                ("학습 장치", f"{summary.get('training_device')} {meta.get('gpu_name', '')}"),
                ("Replay episode 상한", summary.get("replay_capacity_episodes")), ("Final 평가 후보 수", len(summary.get("final_candidate_results", []))),
                ("전체 경과시간(초)", summary.get("wall_sec")), ("Parent peak RSS(MiB)", summary.get("parent_peak_rss_mib")),
                ("Target τ (SGD step마다)", summary.get("target_tau")), ("Epsilon schedule", schedule_text("epsilon_schedule")),
                ("Learning-rate schedule", schedule_text("learning_rate_schedule"))]
    initial_phase = "warm_start_replay" if warm else "initial_random"
    settings.append((("Warm-start fill" if warm else "Initial") + " / policy / diagnostic / final waves", " / ".join(str(sum(
        row.get("phase") == phase for row in waves)) for phase in (initial_phase, "policy_iteration", "screening_validation", "final_selection_validation"))))
    initial_count = next((row.get("replay_episode_count") for row in iterations
                          if int(row.get("iteration", -1)) == 0 and row.get("replay_episode_count") is not None), summary.get("replay_capacity_episodes"))
    settings.append((("Warm-start fill" if warm else "Initial update") + " / policy update episode",
                     f"{initial_count} / {summary.get('replay_sample_episodes_per_update')}"))
    settings.append(("Initial / policy epoch", f"{summary.get('initial_update_epochs')} / {summary.get('policy_update_epochs')}"))
    if warm:
        settings.extend([("Warm-start checkpoint", summary.get("warm_start_checkpoint_id")), ("Source iteration", summary.get("warm_start_source_iteration"))])
    stopping = summary.get("early_stopping", {})
    if stopping.get("enabled"):
        settings.extend([("조기 종료 최소 iteration", stopping.get("min_iterations")),
                         ("최고 생산량 미개선 iteration", stopping.get("patience_iterations")),
                         ("연속 paired 검증 수", stopping.get("consecutive_checks")),
                         ("Paired CI 상한 기준 (제품)", stopping.get("paired_ci_upper_threshold"))])

    fields = [("Iteration", "iteration"), ("Epsilon", "epsilon"), ("Learning rate", "learning_rate"),
              ("Rollout 평균 제품", "rollout_products_mean"), ("Rollout 표준편차", "rollout_products_std"),
              ("Validation 평균 제품", "validation_products_mean"), ("Validation 표준편차", "validation_products_std"),
              ("Validation seed 수", "validation_episode_count"), ("TD MSE", "td_train_mse"), ("Holdout TD MSE", "td_holdout_mse"),
              ("Greedy MC RMSE", "greedy_mc_rmse"), ("Greedy 편향", "greedy_mc_bias"),
              ("가치망 평균 예측", "greedy_mc_prediction_mean"), ("실제 MC 평균", "greedy_mc_target_mean"),
              ("보유 replay", "replay_episode_count"), ("업데이트 episode", "update_episode_count"),
              ("현재 wave", "update_current_episode_count"), ("과거 표본", "update_history_episode_count"),
              ("Replay MiB", "replay_mib"), ("Update MiB", "update_mib"), ("GPU peak MiB", "gpu_peak_allocated_mib"),
              ("Epoch", "epochs"), ("SGD 횟수", "sgd_steps"), ("평균 n", "effective_n_mean"), ("TD 구간(분)", "td_span_min_mean"),
              ("Off-policy 중단 비율", "td_off_policy_cut_ratio"), ("n 도달 비율", "td_n_limit_ratio"), ("Terminal 비율", "td_terminal_ratio"),
              ("OOD 선택 비율", "ood_selection_rate"), ("OOD 추가 편향", "ood_overestimation_excess"),
              ("Beam entropy", "beam_entropy"), ("자발적 WAIT 수", "voluntary_wait_count"),
              ("Target 구성 초", "target_build_sec"), ("신경망 update 초", "update_sec"),
              ("Final 후보 순위", "final_candidate_rank"), ("Final 평균 제품", "final_validation_products_mean"),
              ("Final 표준편차", "final_validation_products_std")]
    if stopping.get("enabled"):
        fields.extend([("종료 판단 기준 checkpoint", "early_stop_reference_iteration"),
                       ("최고 생산량 미개선 iteration", "early_stop_stagnant_iterations"),
                       ("현재 - 최고 제품 차이", "early_stop_paired_mean"),
                       ("Paired 근사 CI 상한", "early_stop_paired_ci_upper"),
                       ("조기 종료 조건 충족", "early_stop_triggered")])
    contract_text = ("<p><strong>운영 지표 해석</strong> Epsilon은 수집 시 random 분기 확률이며 validation은 0입니다. "
                     "초기 Random 수집은 1이며 warm-start는 설정값을 사용합니다. Learning rate는 해당 iteration의 Adam 설정값입니다. "
                     "자발적 WAIT는 실행 가능한 후보가 있는데 WAIT를 선택한 worker 횟수이고, WAIT 비활성 계약에서는 0이어야 합니다. "
                     "강제 유휴는 포함하지 않으며 episode 수가 다르면 단순 횟수 비교가 공정하지 않습니다.</p>"
                     + render_formula("entropy", heading="엔트로피 산출식")
                     + "<p><strong>엔트로피 유의사항</strong> 후보가 하나면 계산하지 않고, 같은 가치의 복수 후보는 1입니다. "
                     "Episode의 유효 decision 평균을 구한 뒤 유효 episode만 평균합니다. 유효 decision이 없는 초기 random batch 등은 N/A입니다. "
                     "값의 절대 scale을 제거하므로 H가 비슷해도 생산량이나 행동 간 실제 가치 차이가 같다는 뜻이 아닙니다. 이것은 정책 확률의 엔트로피가 아닙니다.</p>"
                     "<p><strong>메모리·시간</strong> GPU peak는 update에서 PyTorch가 할당한 peak이며 전체 장치 사용량이 아닙니다. "
                     "Target 구성은 replay의 greedy 행동 재탐색, update 시간은 SGD와 진단 오차 계산입니다. GPU 실행 시 동기화된 wall-clock 시간이며 순수 kernel 시간은 아닙니다.</p>")
    times = [("Training rollout", summary.get("training_rollout_sec")), ("Validation", summary.get("validation_sec")),
             ("TD target 구성", summary.get("target_build_sec")), ("신경망 업데이트", summary.get("update_sec")),
             ("대시보드/통계 저장", summary.get("write_sec"))]
    if stopping.get("enabled"):
        contract_text += (render_formula("early-stop", heading="조기 종료 산출식")
                          + "<p><strong>조기 종료</strong> 최소 학습 횟수와 미개선 기간을 "
                          "충족하고 최근 연속 검증의 상한이 설정값 미만일 때 수집을 종료합니다. 작은 평균 개선도 "
                          "최고 checkpoint와 미개선 기간을 갱신합니다. MSE 기반 종료나 모델 롤백이 아닙니다. "
                          "반복 검증과 checkpoint 선택 편향이 있으므로 통계적 수렴 증명은 아닙니다. 종료 후 별도 "
                          "final-selection을 완료하며 진행률 분모는 실제 필요 episode 수로 조정됩니다.</p>")
    wall = number(summary, "wall_sec")
    known = [number({"value": value}, "value") for _, value in times]
    times.append(("기타 및 checkpoint 저장", max(0, wall - sum(known)) if wall is not None and all(v is not None for v in known) else None))
    time_table = _table([{"phase": label, "seconds": value} for label, value in times], [("Time Breakdown", "phase"), ("실제 경과시간(초)", "seconds")])
    cards = lambda values: "<dl class='cards'>" + "".join(f"<div><dt>{html.escape(label)}</dt><dd><strong>{cell(value)}</strong></dd></div>" for label, value in values) + "</dl>"
    css = """*{box-sizing:border-box}body{margin:0;background:#f5f7f9;color:#172a36;font:15px/1.65 system-ui,sans-serif;letter-spacing:0}
main{max-width:1560px;margin:auto;padding:24px}h1{font-size:26px;margin:0}h2{font-size:22px}h3{font-size:18px;margin:0 0 14px}h4{font-size:14px;color:#526773;margin:18px 0 8px}
header{border-bottom:1px solid #c8d4dc;padding-bottom:12px;margin-bottom:16px}p{overflow-wrap:anywhere}nav{display:flex;gap:20px;flex-wrap:wrap;margin:12px 0 22px}a{color:#0969a2}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));margin:16px 0;gap:14px}.cards>div{min-width:0;border-bottom:1px solid #c8d4dc;padding:8px 0}dt{font-size:13px;color:#526773}dd{margin:4px 0;overflow-wrap:anywhere}
.charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:32px}.panel{min-width:0;border-top:2px solid #bfd0db;padding:18px 0}
.panel p{font-size:14px;margin:10px 0}.caveat{color:#586470}
svg{display:block;width:100%;height:auto;background:white}.tick{fill:#4b6576;font-size:12px;text-anchor:end}.x-tick{text-anchor:middle}.axis-label{fill:#263e4d;font-size:13px;text-anchor:middle}
.grid-line{stroke:#d8e1e6;stroke-width:1}.axis-line{stroke:#8ba6b4}.point-label{fill:#172a36;font-size:12px}.chart-legend{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:13px}
.chart-legend span{display:inline-flex;align-items:center;gap:6px}.chart-legend i{display:inline-block;width:18px;height:3px}.empty{padding:30px 12px;color:#526773;background:#eaf0f4}
details{border-top:1px solid #b4c5d0;margin-top:24px;padding-top:12px}summary{cursor:pointer;font-size:18px;font-weight:650;margin-bottom:18px}details>summary:hover{color:#0969a2}
.table-scroll{overflow:auto;max-width:100%}table{border-collapse:collapse;width:100%;margin:14px 0;font-size:13px}th,td{padding:8px;border-bottom:1px solid #d1dce2;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
[role=alert]{border-left:4px solid #b52e3a;padding:12px;background:#fff0ef;color:#822630}.embedded .run-overview{display:none}.embedded main{padding:16px}
@media(max-width:850px){main{padding:14px}.charts{grid-template-columns:1fr}}@media(max-width:540px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.chart-wrap{overflow-x:auto}.metric-chart{min-width:500px}h1{font-size:22px}}
"""
    derived = json.dumps({"paired_production_changes": paired, "warnings": warnings}, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    content = ("<!doctype html><html lang='ko'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
               f"<title>ADP n-step TD 학습 진단</title><style>{css}{FORMULA_CSS}</style></head><body><main>{banner}"
               "<header class='run-overview'><h1>ADP n-step TD 학습 진단</h1>"
               "<p>n-step TD · MSE · 완료 제품 reward · target network · 최근 episode replay</p></header>"
               + (f"<p role='alert'>{html.escape(str(summary['failure']))}</p>" if summary.get("failure") else "")
               + "<div class='run-overview'>" + cards(overview) + "</div>"
               "<nav aria-label='학습 진단 영역'><a href='#core'>핵심 학습 성과</a><a href='#learning-details'>학습 구조·행동 진단</a>"
               "<a href='#operations'>설정·통계·시간</a></nav><section id='core'><h2>핵심 학습 성과</h2><div class='charts'>"
               + "".join(panels) + "</div></section>"
               "<details id='learning-details'><summary>학습 구조·행동·수집 비용 상세</summary><div class='charts'>"
               + "".join(details) + "</div></details><details id='operations'><summary>설정·Iteration 통계·시간 상세</summary>"
               + cards(settings) + contract_text + _table(production_rows, fields) + time_table + "</details>"
               "<footer><p><a href='iteration_metrics.csv'>Iteration CSV</a> · <a href='episode_metrics.csv'>Episode CSV</a> · "
               "<a href='wave_metrics.csv'>Wave CSV</a> · <a href='training_summary.json'>Summary JSON</a></p></footer>"
               f"<script id='adp-derived-metrics' type='application/json'>{derived}</script></main>"
               "<script>if(new URLSearchParams(location.search).has('embedded'))document.documentElement.classList.add('embedded');"
               "try{const key=location.pathname+':diagnostics';const saved=JSON.parse(sessionStorage.getItem(key)||'{}');"
               "document.querySelectorAll('details[id]').forEach(d=>{if(saved[d.id]!==undefined)d.open=saved[d.id];"
               "d.addEventListener('toggle',()=>{saved[d.id]=d.open;sessionStorage.setItem(key,JSON.stringify(saved));});});"
               "window.addEventListener('load',()=>window.scrollTo(0,Number(sessionStorage.getItem(key+':scroll')||0)));"
               "window.addEventListener('scroll',()=>sessionStorage.setItem(key+':scroll',String(window.scrollY)),{passive:true});"
               "document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',()=>{const d=document.querySelector(a.hash);if(d?.tagName==='DETAILS')d.open=true;}));"
               "}catch(e){}</script></body></html>")
    path = output / filename
    atomic_text(path, content)
    return path


def render_from_files(output: Path, *, filename: str = "training_dashboard.html") -> Path:
    def read_csv(name: str) -> list[dict[str, Any]]:
        def parse(value: str) -> Any:
            if value == "":
                return None
            try:
                number = float(value)
                return int(number) if number.is_integer() else number
            except ValueError:
                return value
        with (output / name).open(encoding="utf-8-sig", newline="") as stream:
            return [{key: parse(value) for key, value in row.items()} for row in csv.DictReader(stream)]
    summary = json.loads((output / "training_summary.json").read_text(encoding="utf-8"))
    if summary.get("return_estimator") != "n_step_td":
        raise ValueError("Use the legacy renderer for MC experiment results.")
    return render_td_dashboard(output, read_csv("episode_metrics.csv"), read_csv("iteration_metrics.csv"),
                               read_csv("wave_metrics.csv"), summary, filename=filename)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Rebuild an n-step TD dashboard without training.")
    parser.add_argument("output", type=Path)
    print(render_from_files(parser.parse_args().output.resolve()))
