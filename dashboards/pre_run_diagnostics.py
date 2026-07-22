from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .shell import render_page_shell


def export_pre_run_diagnostics_dashboard(
    *,
    diagnostics: dict[str, Any],
    output_dir: Path,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    current_run_id: str | None = None,
) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    page_path = output_dir / "pre_run_diagnostics.html"
    supported = bool(diagnostics.get("supported", False))
    if not supported:
        body = (
            "<section class='section panel'>"
            "<h2>Not Available</h2>"
            f"<p class='muted'>{html.escape(str(diagnostics.get('reason', 'No pre-run diagnostics are available for this scenario.')))}</p>"
            "</section>"
        )
    else:
        body = _render_supported_body(diagnostics)

    page = render_page_shell(
        title="Pre-Run Diagnostics",
        current_page_path=page_path,
        manifest=manifest,
        manifest_path=manifest_path,
        current_artifact="pre_run_diagnostics.html",
        current_run_id=current_run_id,
        page_title="Pre-Run Diagnostics",
        page_subtitle="Factory scenario indicators computed before simulation from task complexity, role policy, map topology, service tiles, shared resources, and battery settings.",
        body_html=body,
    )
    page_path.write_text(page, encoding="utf-8")
    return page_path


def _render_supported_body(diagnostics: dict[str, Any]) -> str:
    scenario_type = str(diagnostics.get("scenario_type", "") or "-")
    metrics = diagnostics.get("metrics", {}) if isinstance(diagnostics.get("metrics", {}), dict) else {}
    order = diagnostics.get("metric_order", []) if isinstance(diagnostics.get("metric_order", []), list) else list(metrics)
    cards = "".join(_metric_card(code, metrics.get(code, {})) for code in order if isinstance(metrics.get(code, {}), dict))
    details = "".join(_metric_detail(code, metrics.get(code, {})) for code in order if isinstance(metrics.get(code, {}), dict))
    inputs_html = _json_block(diagnostics.get("inputs", {}))
    return f"""
<section class="section panel">
  <h2>Scenario</h2>
  <p><code class="inline">{html.escape(scenario_type)}</code></p>
</section>
<section class="section grid cards-3">
  {cards}
</section>
<section class="section grid cards-2">
  {details}
</section>
<section class="section panel">
  <h2>Input Snapshot</h2>
  <p class="muted">These inputs are read before the run from scenario config, decision config, map topology, service targets, battery settings, and HumanoidSim task complexity.</p>
  {inputs_html}
</section>
"""


def _metric_card(code: str, metric: dict[str, Any]) -> str:
    label = str(metric.get("label", "Metric"))
    value = _format_number(metric.get("value", 0.0))
    unit = str(metric.get("unit", ""))
    interpretation = str(metric.get("interpretation", ""))
    assessment = _assessment_for_metric(code, metric)
    badge = _assessment_badge(assessment)
    range_text = _metric_range_text(code, metric)
    return (
        "<div class='card'>"
        f"<div class='label'>{html.escape(label)}</div>"
        f"{badge}"
        f"<div class='value'>{html.escape(value)}</div>"
        f"<div class='sub'>{html.escape(unit)}</div>"
        f"<div class='sub'>{html.escape(range_text)}</div>"
        f"<div class='sub'>{html.escape(interpretation)}</div>"
        f"<div class='sub'><strong>{html.escape(str(assessment.get('label', '')))}:</strong> {html.escape(str(assessment.get('message', '')))}</div>"
        "</div>"
    )


def _metric_detail(code: str, metric: dict[str, Any]) -> str:
    label = str(metric.get("label", code))
    calculation = metric.get("calculation", {}) if isinstance(metric.get("calculation", {}), dict) else {}
    inputs = metric.get("inputs", {}) if isinstance(metric.get("inputs", {}), dict) else {}
    formula = str(calculation.get("formula", ""))
    calculation_without_formula = {key: value for key, value in calculation.items() if key != "formula"}
    assessment = _assessment_for_metric(code, metric)
    return (
        "<section class='panel'>"
        f"<h2>{html.escape(label)}</h2>"
        f"<p class='muted'><code class='inline'>{html.escape(code)}</code></p>"
        f"<h3>Reading</h3>{_render_assessment(assessment)}"
        f"<h3>Formula</h3>{_formula_block(code, formula)}"
        "<h3>Inputs Used</h3>"
        f"{_json_block(inputs)}"
        "<h3>Calculation</h3>"
        f"{_json_block(calculation_without_formula)}"
        "</section>"
    )


def _assessment_for_metric(code: str, metric: dict[str, Any]) -> dict[str, Any]:
    assessment = metric.get("assessment", {}) if isinstance(metric.get("assessment", {}), dict) else {}
    if assessment:
        return assessment
    try:
        value = float(metric.get("value", 0.0) or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    return _fallback_assessment(code, value)


def _fallback_assessment(code: str, value: float) -> dict[str, str]:
    rules = {
        "worker_otc_imbalance": [(1.2, "low", "Good"), (1.6, "moderate", "Watch"), (float("inf"), "high", "High")],
        "resource_conflict_potential": [(0.2, "low", "Low"), (0.45, "moderate", "Watch"), (float("inf"), "high", "High")],
        "traffic_contention_index": [(0.15, "low", "Low"), (0.35, "moderate", "Watch"), (float("inf"), "high", "High")],
        "service_tile_scarcity": [(0.2, "low", "Good"), (0.5, "moderate", "Watch"), (float("inf"), "high", "High")],
        "robot_interaction_load": [(0.1, "low", "Low"), (0.3, "moderate", "Watch"), (float("inf"), "high", "High")],
        "power_coordination_risk": [(0.8, "low", "Low"), (1.2, "moderate", "Watch"), (float("inf"), "high", "High")],
    }
    messages = {
        "low": "현재 값은 낮은 부담 구간입니다.",
        "moderate": "현재 값은 주의해서 볼 구간입니다.",
        "high": "현재 값은 높은 부담 구간입니다.",
    }
    bands = {
        "worker_otc_imbalance": "Good <= 1.2, Watch <= 1.6, High > 1.6",
        "resource_conflict_potential": "Low <= 0.20, Watch <= 0.45, High > 0.45",
        "traffic_contention_index": "Low <= 0.15, Watch <= 0.35, High > 0.35",
        "service_tile_scarcity": "Good <= 0.20, Watch <= 0.50, High > 0.50",
        "robot_interaction_load": "Low <= 0.10, Watch <= 0.30, High > 0.30",
        "power_coordination_risk": "Low <= 0.80, Watch <= 1.20, High > 1.20",
    }
    for upper, severity, label in rules.get(code, []):
        if value <= upper:
            return {"severity": severity, "label": label, "message": messages[severity], "bands": bands.get(code, "")}
    return {"severity": "unknown", "label": "Unknown", "message": "", "bands": ""}


def _assessment_badge(assessment: dict[str, Any]) -> str:
    severity = str(assessment.get("severity", "unknown")).lower()
    label = str(assessment.get("label", "Unknown"))
    colors = {
        "low": ("#e8f7ef", "#0f8c5b"),
        "moderate": ("#fff4df", "#b9770e"),
        "high": ("#fdeceb", "#c0392b"),
    }
    bg, fg = colors.get(severity, ("#edf3fb", "#617088"))
    return (
        f"<span style='display:inline-flex;margin-top:10px;padding:5px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;letter-spacing:.03em;'>"
        f"{html.escape(label)}</span>"
    )


def _render_assessment(assessment: dict[str, Any]) -> str:
    return (
        "<div style='border:1px solid var(--line);background:#f8fbff;border-radius:14px;padding:12px;margin-bottom:12px;'>"
        f"{_assessment_badge(assessment)}"
        f"<p style='margin:10px 0 4px;line-height:1.55;'>{html.escape(str(assessment.get('message', '')))}</p>"
        f"<p class='muted' style='margin:0;'>Band: {html.escape(str(assessment.get('bands', '')))}</p>"
        "</div>"
    )


def _formula_block(code: str, raw_formula: str) -> str:
    model = _formula_model(code)
    equation = str(model.get("equation", "") or raw_formula)
    definitions = model.get("definitions", []) if isinstance(model.get("definitions", []), list) else []
    range_text = model.get("range", "") or _metric_range_text(code, {})
    rows = "".join(
        "<div style='display:grid;grid-template-columns:150px minmax(0,1fr);gap:10px;"
        "padding:7px 0;border-bottom:1px solid #e7edf6;'>"
        f"<strong>{html.escape(str(label))}</strong><span>{html.escape(str(text))}</span></div>"
        for label, text in definitions
    )
    return (
        "<div style='background:#f8fafc;border:1px solid var(--line);border-radius:14px;padding:12px;margin-bottom:12px;'>"
        "<div style='font-family:Cambria Math, Times New Roman, serif;font-size:22px;line-height:1.45;"
        "background:white;border:1px solid #e7edf6;border-radius:12px;padding:12px;margin-bottom:10px;overflow:auto;'>"
        f"{equation}</div>"
        f"<div class='muted' style='margin-bottom:8px;'>Range: {html.escape(str(range_text))}</div>"
        f"{rows}"
        "</div>"
    )


def _formula_model(code: str) -> dict[str, Any]:
    formulas = {
        "worker_otc_imbalance": {
            "equation": "I<sub>OTC</sub> = max<sub>w∈W</sub> L<sub>w</sub> / ((1 / |W|) ∑<sub>w∈W</sub> L<sub>w</sub>), &nbsp; L<sub>w</sub> = ∑<sub>t∈T<sub>w</sub></sub> N<sub>t</sub>C<sub>task</sub>(t)",
            "range": "Min 1.0, Max |W|",
            "definitions": [
                ("W", "worker 집합"),
                ("T_w", "worker w에게 허용된 task code 집합"),
                ("N_t", "사전 추정한 task t 발생 횟수"),
                ("C_task(t)", "HumanoidSim primitive difficulty 기반 task complexity"),
            ],
        },
        "resource_conflict_potential": {
            "equation": "I<sub>res</sub> = (∑<sub>i&lt;j</sub> n<sub>i</sub>n<sub>j</sub>𝟙[R<sub>i</sub>∩R<sub>j</sub>≠∅] + ∑<sub>i</sub> n<sub>i</sub>(n<sub>i</sub>-1)/2) / (∑<sub>i&lt;j</sub> n<sub>i</sub>n<sub>j</sub> + ∑<sub>i</sub> n<sub>i</sub>(n<sub>i</sub>-1)/2)",
            "range": "Min 0, Max 1",
            "definitions": [
                ("n_i", "task template i의 예상 발생 횟수"),
                ("R_i", "task template i가 점유할 수 있는 공유자원 key 집합"),
                ("𝟙[...]", "조건이 참이면 1, 아니면 0"),
            ],
        },
        "traffic_contention_index": {
            "equation": "I<sub>traffic</sub> = min(1, (B / P) · max(1, |W| / 3))",
            "range": "Min 0, Max 1",
            "definitions": [
                ("B", "passable neighbor가 2개 이하인 bottleneck tile 수"),
                ("P", "전체 passable tile 수"),
                ("|W|", "worker 수"),
            ],
        },
        "service_tile_scarcity": {
            "equation": "I<sub>service</sub> = (1 / |R|) ∑<sub>r∈R</sub> 1 / max(1, s<sub>r</sub>)",
            "range": "Min 0에 근접, Max 1",
            "definitions": [
                ("R", "machine, queue, shelf, zone 등 service target 집합"),
                ("s_r", "target r에 접근 가능한 service tile 수"),
            ],
        },
        "robot_interaction_load": {
            "equation": "I<sub>interact</sub> = (∑<sub>t∈T<sub>int</sub></sub> N<sub>t</sub>C<sub>task</sub>(t)) / (∑<sub>t∈T</sub> N<sub>t</sub>C<sub>task</sub>(t))",
            "range": "Min 0, Max 1",
            "definitions": [
                ("T_int", "handover, battery delivery 등 robot-to-robot timing이 필요한 task 집합"),
                ("T", "전체 task 집합"),
            ],
        },
        "power_coordination_risk": {
            "equation": "I<sub>power</sub> = max<sub>w∈W</sub> ((m<sup>active</sup><sub>w</sub>r<sub>active</sub> + m<sup>idle</sup><sub>w</sub>r<sub>idle</sub>) / B<sub>period</sub>)",
            "range": "Min 0, Max 고정 없음",
            "definitions": [
                ("m_active", "worker w의 예상 active minutes proxy"),
                ("m_idle", "worker w의 예상 idle minutes proxy"),
                ("r_active", "non-available battery drain multiplier"),
                ("r_idle", "available battery drain multiplier"),
                ("B_period", "battery swap period minutes"),
            ],
        },
    }
    return formulas.get(code, {})


def _metric_range_text(code: str, metric: dict[str, Any]) -> str:
    if code == "worker_otc_imbalance":
        inputs = metric.get("inputs", {}) if isinstance(metric.get("inputs", {}), dict) else {}
        loads = inputs.get("task_complexity_load_by_worker", {}) if isinstance(inputs.get("task_complexity_load_by_worker", {}), dict) else {}
        worker_count = len(loads)
        max_text = str(worker_count) if worker_count else "|W|"
        return f"Min 1.0, Max {max_text}"
    if code in {"resource_conflict_potential", "traffic_contention_index", "robot_interaction_load"}:
        return "Min 0, Max 1"
    if code == "service_tile_scarcity":
        return "Min 0에 근접, Max 1"
    if code == "power_coordination_risk":
        return "Min 0, Max 고정 없음"
    return "Min/Max not defined"


def _json_block(payload: Any) -> str:
    try:
        text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(payload)
    return (
        "<pre style='white-space:pre-wrap;overflow:auto;background:#f8fafc;border:1px solid var(--line);"
        "border-radius:14px;padding:12px;line-height:1.45;font-size:13px;'>"
        f"{html.escape(text)}"
        "</pre>"
    )


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(number) >= 100:
        return f"{number:.1f}"
    return f"{number:.3f}"
