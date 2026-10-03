from __future__ import annotations

import argparse
import csv
import html
import json
import statistics
from pathlib import Path
from typing import Any

from manufacturing_sim.adp.train import _svg_chart


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _float(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = str(row.get(key, "")).strip()
    return float(value) if value else float(default)


def _bool(row: dict[str, Any], key: str) -> bool:
    return str(row.get(key, "")).strip().lower() in {"true", "1", "yes"}


def _load_run(path: Path, label: str) -> dict[str, Any]:
    rows = _read_csv(path / "iteration_metrics.csv")
    summary = json.loads((path / "training_summary.json").read_text(encoding="utf-8"))
    ood_rows = [row for row in rows if str(row.get("ood_selection_rate", "")).strip()]
    excess_rows = [
        row for row in rows if str(row.get("ood_overestimation_excess", "")).strip()
    ]
    effective = [
        _float(row, "effective_incumbent_validation_products_avg") for row in rows
    ]
    return {
        "label": label,
        "path": path,
        "rows": rows,
        "summary": summary,
        "iterations": [_float(row, "iteration") for row in rows],
        "candidate": [_float(row, "candidate_validation_products_avg") for row in rows],
        "effective": effective,
        "ood_x": [_float(row, "iteration") - 1.0 for row in ood_rows],
        "ood_rate": [100.0 * _float(row, "ood_selection_rate") for row in ood_rows],
        "excess_x": [_float(row, "iteration") - 1.0 for row in excess_rows],
        "ood_excess": [_float(row, "ood_overestimation_excess") for row in excess_rows],
        "ood_rate_mean": statistics.fmean(
            _float(row, "ood_selection_rate") for row in ood_rows
        ) if ood_rows else 0.0,
        "ood_excess_mean": statistics.fmean(
            _float(row, "ood_overestimation_excess") for row in excess_rows
        ) if excess_rows else 0.0,
        "accepted": sum(_bool(row, "policy_update_accepted") for row in rows),
        "rejected": sum(not _bool(row, "policy_update_accepted") for row in rows),
        "nondecreasing": all(
            right + 1e-9 >= left for left, right in zip(effective, effective[1:])
        ),
    }


def _panel(title: str, chart: str, description: str) -> str:
    return (
        "<section class='panel'><h2>" + html.escape(title) + "</h2>" + chart
        + "<p>" + html.escape(description) + "</p></section>"
    )


def render(control_dir: Path, treatment_dir: Path, output: Path) -> Path:
    control = _load_run(control_dir.resolve(), "Control")
    treatment = _load_run(treatment_dir.resolve(), "Treatment")
    ood_improved = (
        treatment["ood_rate_mean"] < control["ood_rate_mean"]
        and treatment["ood_excess_mean"] < control["ood_excess_mean"]
    )
    stable = bool(treatment["nondecreasing"])
    if ood_improved and stable:
        verdict = "해결"
        verdict_detail = "OOD 선택과 추가 과대평가가 모두 감소했고 incumbent 생산량도 하락하지 않았습니다."
    elif stable and treatment["rejected"]:
        verdict = "억제"
        verdict_detail = "OOD 문제는 남아 있지만 성능이 낮은 candidate를 거부해 incumbent 붕괴를 차단했습니다."
    else:
        verdict = "미해결"
        verdict_detail = "OOD 지표 또는 incumbent 안정성 기준을 충족하지 못했습니다."

    colors = {"Control": "#43a5ff", "Treatment": "#72d69b"}
    validation_chart = _svg_chart(
        [
            ("Control candidate", control["candidate"], colors["Control"]),
            ("Treatment candidate", treatment["candidate"], "#ff8b72"),
            ("Treatment incumbent", treatment["effective"], colors["Treatment"]),
        ],
        x_values=control["iterations"],
        x_label="가치함수 업데이트",
        y_label="2일 episode 완료 제품 수",
        include_zero=True,
    )
    ood_chart = _svg_chart(
        [
            ("Control", control["ood_rate"], colors["Control"]),
            ("Treatment", treatment["ood_rate"], colors["Treatment"]),
        ],
        x_values=control["ood_x"],
        x_label="Rollout 생성 checkpoint",
        y_label="Greedy OOD 선택률(%)",
        include_zero=True,
    )
    excess_chart = _svg_chart(
        [
            ("Control", control["ood_excess"], colors["Control"]),
            ("Treatment", treatment["ood_excess"], colors["Treatment"]),
        ],
        x_values=control["excess_x"],
        x_label="Rollout 생성 checkpoint",
        y_label="OOD 추가 예측오차(제품 수)",
        include_zero=True,
    )

    treatment_rows = "".join(
        "<tr>"
        f"<td>{int(_float(row, 'iteration'))}</td>"
        f"<td>{_float(row, 'incumbent_before_validation_products_avg'):.3f}</td>"
        f"<td>{_float(row, 'candidate_validation_products_avg'):.3f}</td>"
        f"<td>{_float(row, 'effective_incumbent_validation_products_avg'):.3f}</td>"
        f"<td>{html.escape(str(row.get('policy_update_accepted', '')))}</td>"
        f"<td>{html.escape(str(row.get('rollback_model_hash_match', '')))}</td>"
        f"<td>{html.escape(str(row.get('rollback_optimizer_hash_match', '')))}</td>"
        "</tr>"
        for row in treatment["rows"]
    )
    cards = "".join(
        f"<div class='card'><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>"
        for label, value in (
            ("판정", verdict),
            ("Control 평균 OOD 선택률", f"{100.0 * control['ood_rate_mean']:.2f}%"),
            ("Treatment 평균 OOD 선택률", f"{100.0 * treatment['ood_rate_mean']:.2f}%"),
            ("Control 평균 OOD 과대평가", f"{control['ood_excess_mean']:.3f}"),
            ("Treatment 평균 OOD 과대평가", f"{treatment['ood_excess_mean']:.3f}"),
            ("Treatment 채택/거부", f"{treatment['accepted']} / {treatment['rejected']}"),
            ("Incumbent 비감소", "PASS" if stable else "FAIL"),
        )
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ADP OOD Quick Validation</title><style>"
        "body{margin:0;background:#08111f;color:#e8f1ff;font:15px Segoe UI,Arial}"
        "main{max-width:1450px;margin:auto;padding:26px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}"
        ".card,.panel{border:1px solid #294567;background:#101d31;border-radius:6px;padding:16px}.card span{display:block;color:#91add0}.card strong{font-size:21px}"
        ".grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:14px}.panel p{color:#b8cae0;line-height:1.5}"
        ".metric-chart{width:100%;height:auto;background:#0b1728}.grid-line{stroke:#223752}.axis-line{stroke:#86a5c9}.tick{fill:#9eb5d1;font-size:11px}.x-tick{text-anchor:middle}.y-tick{text-anchor:end}.axis-label{fill:#d8e8fa;font-weight:600}.x-axis-label,.y-axis-label{text-anchor:middle}.point-label{fill:#fff;text-anchor:middle}.chart-legend{display:flex;gap:14px;flex-wrap:wrap}.chart-legend i{display:inline-block;width:12px;height:3px;margin-right:6px}"
        "table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid #294567;text-align:right}th:first-child,td:first-child{text-align:left}a{color:#72d69b}@media(max-width:900px){.grid{grid-template-columns:1fr}}"
        "</style></head><body><main><h1>Worker 3 ADP OOD 과대평가 신속 검증</h1>"
        f"<p>{html.escape(verdict_detail)}</p><div class='cards'>{cards}</div><div class='grid'>"
        + _panel(
            "Validation 생산량과 Conservative Gate",
            validation_chart,
            "Candidate 성능과 실제 다음 rollout에 유지된 incumbent 성능을 비교합니다.",
        )
        + _panel(
            "미관측 행동 선택률",
            ood_chart,
            "낮을수록 가치망이 학습 support 안에서 행동을 선택한 비율이 높습니다.",
        )
        + _panel(
            "미관측 행동 추가 과대평가량",
            excess_chart,
            "양수이면 OOD 행동이 관측 범위 내 행동보다 더 낙관적으로 예측됐다는 뜻입니다.",
        )
        + "<section class='panel'><h2>Treatment Gate 기록</h2><table><thead><tr>"
        "<th>Update</th><th>Before</th><th>Candidate</th><th>Effective</th><th>Accepted</th><th>Model rollback</th><th>Optimizer rollback</th>"
        f"</tr></thead><tbody>{treatment_rows}</tbody></table></section></div>"
        f"<p><a href='{control_dir.resolve().as_uri()}'>Control 결과 폴더</a> · "
        f"<a href='{treatment_dir.resolve().as_uri()}'>Treatment 결과 폴더</a></p>"
        "</main></body></html>",
        encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the worker-3 ADP OOD quick A/B dashboard.")
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--treatment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(render(args.control, args.treatment, args.output).resolve())


if __name__ == "__main__":
    main()
