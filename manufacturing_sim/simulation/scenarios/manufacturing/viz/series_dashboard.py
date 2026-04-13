from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from typing import Any


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_text(value: Any, max_len: int = 220) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return "-"
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _slug(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _parse_markdown_json_array_section(markdown: str, section_title: str) -> list[str]:
    pattern = re.compile(
        rf"^##\s+{re.escape(section_title)}\s*$\n```json\s*\n(.*?)\n```",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown or "")
    if not match:
        return []
    payload = match.group(1).strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    items: list[str] = []
    for entry in parsed:
        text = str(entry).strip()
        if text:
            items.append(text)
    return items


def _load_knowledge_sections(parent_output_dir: Path) -> dict[str, list[str]]:
    knowledge_path = parent_output_dir / "knowledge.md"
    try:
        markdown = knowledge_path.read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    return {
        "persistent_lessons": _parse_markdown_json_array_section(markdown, "Persistent Lessons"),
        "latest_lessons": _parse_markdown_json_array_section(markdown, "Latest Lessons"),
        "detector_guidance": _parse_markdown_json_array_section(markdown, "Detector Guidance"),
        "planner_guidance": _parse_markdown_json_array_section(markdown, "Planner Guidance"),
        "open_watchouts": _parse_markdown_json_array_section(markdown, "Open Watchouts"),
    }


def _load_run_reflection(path_str: str) -> dict[str, Any]:
    path = Path(str(path_str).strip())
    payload = _safe_json(path)
    return payload if isinstance(payload, dict) else {}


def _shared_ratio(left: list[str], right: list[str]) -> float:
    left_norm = {_slug(item) for item in left if _slug(item)}
    right_norm = {_slug(item) for item in right if _slug(item)}
    if not left_norm or not right_norm:
        return 0.0
    union = left_norm | right_norm
    if not union:
        return 0.0
    return float(len(left_norm & right_norm)) / float(len(union))


def build_series_analysis(*, parent_output_dir: Path, summary_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    summary_blob: Any = summary_payload
    if not isinstance(summary_blob, dict):
        summary_path = parent_output_dir / "run_series_summary.json"
        summary_blob = _safe_json(summary_path)
    if not isinstance(summary_blob, dict):
        return {
            "knowledge_effect_classification": "mixed",
            "performance_pattern": "insufficient_data",
            "analysis_summary": "run_series_summary.json is missing or invalid.",
            "runs": [],
            "peak_run": {},
            "worst_run": {},
            "baseline_vs_best": {},
            "baseline_vs_final": {},
            "lesson_stability": {},
            "knowledge_sections": _load_knowledge_sections(parent_output_dir),
        }

    runs_raw = summary_blob.get("runs", [])
    runs = runs_raw if isinstance(runs_raw, list) else []
    enriched_runs: list[dict[str, Any]] = []
    for row in runs:
        if not isinstance(row, dict):
            continue
        reflection = _load_run_reflection(str(row.get("run_reflection_path", "")).strip())
        carry_forward = reflection.get("carry_forward_lessons", [])
        carry_forward = carry_forward if isinstance(carry_forward, list) else []
        enriched_runs.append(
            {
                "run_index": _safe_int(row.get("run_index"), 0),
                "output_dir": str(row.get("output_dir", "")).strip(),
                "total_products": _safe_int(row.get("total_products"), 0),
                "downstream_closure_ratio": _safe_float(row.get("downstream_closure_ratio"), 0.0),
                "wall_clock_sec": _safe_float(row.get("wall_clock_sec"), 0.0),
                "evaluator_enabled": bool(row.get("evaluator_enabled", False)),
                "kpi_path": str(row.get("kpi_path", "")).strip(),
                "run_meta_path": str(row.get("run_meta_path", "")).strip(),
                "run_reflection_path": str(row.get("run_reflection_path", "")).strip(),
                "knowledge_in_path": str(row.get("knowledge_in_path", "")).strip(),
                "knowledge_out_path": str(row.get("knowledge_out_path", "")).strip(),
                "kpi_dashboard_path": str(row.get("kpi_dashboard_path", "")).strip(),
                "orchestration_intelligence_dashboard_path": str(row.get("orchestration_intelligence_dashboard_path", "")).strip(),
                "run_reflection_markdown_path": str(row.get("run_reflection_markdown_path", "")).strip(),
                "reflection_summary": str(reflection.get("summary", "")).strip(),
                "carry_forward_lessons": [str(item).strip() for item in carry_forward if str(item).strip()],
            }
        )

    knowledge_sections = _load_knowledge_sections(parent_output_dir)

    if not enriched_runs:
        return {
            "knowledge_effect_classification": "mixed",
            "performance_pattern": "insufficient_data",
            "analysis_summary": "No completed child runs were recorded.",
            "runs": [],
            "peak_run": {},
            "worst_run": {},
            "baseline_vs_best": {},
            "baseline_vs_final": {},
            "lesson_stability": {},
            "knowledge_sections": knowledge_sections,
        }

    baseline = enriched_runs[0]
    final_run = enriched_runs[-1]
    peak_run = max(enriched_runs, key=lambda row: (float(row["downstream_closure_ratio"]), int(row["total_products"]), -int(row["run_index"])))
    worst_run = min(enriched_runs, key=lambda row: (float(row["downstream_closure_ratio"]), int(row["total_products"]), -int(row["run_index"])))

    positive_signal = any(
        (row["downstream_closure_ratio"] >= baseline["downstream_closure_ratio"] + 0.03)
        or (row["total_products"] >= baseline["total_products"] + 2)
        for row in enriched_runs[1:]
    )
    negative_signal = (
        final_run["downstream_closure_ratio"] <= peak_run["downstream_closure_ratio"] - 0.08
        or final_run["total_products"] <= peak_run["total_products"] - 3
        or final_run["downstream_closure_ratio"] <= baseline["downstream_closure_ratio"] - 0.08
        or final_run["total_products"] <= baseline["total_products"] - 3
    )
    if positive_signal and negative_signal:
        effect = "mixed"
    elif positive_signal:
        effect = "positive"
    else:
        effect = "negative"

    best_index = int(peak_run["run_index"])
    if effect == "positive":
        performance_pattern = "steady_improvement"
    elif positive_signal and negative_signal and best_index <= max(2, len(enriched_runs) - 2):
        performance_pattern = "early_improvement_then_regression"
    elif positive_signal and negative_signal:
        performance_pattern = "volatile"
    else:
        performance_pattern = "regression"

    consecutive_overlap: list[dict[str, Any]] = []
    for left, right in zip(enriched_runs, enriched_runs[1:]):
        shared_ratio = _shared_ratio(left["carry_forward_lessons"], right["carry_forward_lessons"])
        consecutive_overlap.append(
            {
                "from_run": int(left["run_index"]),
                "to_run": int(right["run_index"]),
                "shared_ratio": round(shared_ratio, 6),
            }
        )

    persistent = knowledge_sections.get("persistent_lessons", [])
    persistent_alignment: list[dict[str, Any]] = []
    for row in enriched_runs:
        shared_ratio = _shared_ratio(row["carry_forward_lessons"], persistent)
        persistent_alignment.append(
            {
                "run_index": int(row["run_index"]),
                "shared_ratio": round(shared_ratio, 6),
            }
        )

    analysis_summary = (
        f"Knowledge impact is {effect}: runs improved through run {int(peak_run['run_index'])}, "
        f"but the final run regressed to {int(final_run['total_products'])} products and "
        f"{float(final_run['downstream_closure_ratio']):.3f} closure."
        if effect == "mixed"
        else (
            f"Knowledge impact is positive: the final run retained better performance than baseline."
            if effect == "positive"
            else "Knowledge impact is negative: later runs did not preserve or improve baseline performance."
        )
    )

    return {
        "knowledge_effect_classification": effect,
        "performance_pattern": performance_pattern,
        "analysis_summary": analysis_summary,
        "requested_run_count": _safe_int(summary_blob.get("requested_run_count"), len(enriched_runs)),
        "completed_run_count": _safe_int(summary_blob.get("completed_run_count"), len(enriched_runs)),
        "runs": enriched_runs,
        "peak_run": {
            "run_index": int(peak_run["run_index"]),
            "total_products": int(peak_run["total_products"]),
            "downstream_closure_ratio": round(float(peak_run["downstream_closure_ratio"]), 6),
        },
        "worst_run": {
            "run_index": int(worst_run["run_index"]),
            "total_products": int(worst_run["total_products"]),
            "downstream_closure_ratio": round(float(worst_run["downstream_closure_ratio"]), 6),
        },
        "baseline_vs_best": {
            "from_run": int(baseline["run_index"]),
            "to_run": int(peak_run["run_index"]),
            "products_delta": int(peak_run["total_products"]) - int(baseline["total_products"]),
            "closure_delta": round(float(peak_run["downstream_closure_ratio"]) - float(baseline["downstream_closure_ratio"]), 6),
        },
        "baseline_vs_final": {
            "from_run": int(baseline["run_index"]),
            "to_run": int(final_run["run_index"]),
            "products_delta": int(final_run["total_products"]) - int(baseline["total_products"]),
            "closure_delta": round(float(final_run["downstream_closure_ratio"]) - float(baseline["downstream_closure_ratio"]), 6),
        },
        "lesson_stability": {
            "consecutive_overlap": consecutive_overlap,
            "persistent_alignment": persistent_alignment,
        },
        "knowledge_sections": knowledge_sections,
    }


def _metric_cards_html(analysis: dict[str, Any]) -> str:
    peak = analysis.get("peak_run", {}) if isinstance(analysis.get("peak_run", {}), dict) else {}
    worst = analysis.get("worst_run", {}) if isinstance(analysis.get("worst_run", {}), dict) else {}
    best_delta = analysis.get("baseline_vs_best", {}) if isinstance(analysis.get("baseline_vs_best", {}), dict) else {}
    final_delta = analysis.get("baseline_vs_final", {}) if isinstance(analysis.get("baseline_vs_final", {}), dict) else {}
    cards = [
        ("Classification", str(analysis.get("knowledge_effect_classification", "mixed")).strip() or "mixed"),
        ("Pattern", str(analysis.get("performance_pattern", "volatile")).strip() or "volatile"),
        ("Peak Run", f"run_{int(peak.get('run_index', 0) or 0):02d}"),
        ("Worst Run", f"run_{int(worst.get('run_index', 0) or 0):02d}"),
        ("Baseline -> Best", f"{int(best_delta.get('products_delta', 0) or 0):+d} products / {float(best_delta.get('closure_delta', 0.0) or 0.0):+.3f} closure"),
        ("Baseline -> Final", f"{int(final_delta.get('products_delta', 0) or 0):+d} products / {float(final_delta.get('closure_delta', 0.0) or 0.0):+.3f} closure"),
    ]
    return "".join(
        f"<div class='card'><div class='label'>{escape(label)}</div><div class='value'>{escape(value)}</div></div>"
        for label, value in cards
    )


def _lesson_list_html(title: str, items: list[str]) -> str:
    body = "".join(f"<li>{escape(_safe_text(item, 220))}</li>" for item in items) or "<li>-</li>"
    return f"<section class='lesson-box'><h3>{escape(title)}</h3><ul>{body}</ul></section>"


def _run_rows_html(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "<tr><td colspan='10'>No runs</td></tr>"
    baseline_products = int(runs[0].get("total_products", 0) or 0)
    baseline_closure = float(runs[0].get("downstream_closure_ratio", 0.0) or 0.0)
    rows_html: list[str] = []
    prev_products = baseline_products
    prev_closure = baseline_closure
    for row in runs:
        output_dir = Path(str(row.get("output_dir", "")).strip()) if str(row.get("output_dir", "")).strip() else None
        reflection_md_path = str(row.get("run_reflection_markdown_path", "")).strip() or (str((output_dir / "run_reflection.md").resolve()) if output_dir else "")
        kpi_dashboard_path = str(row.get("kpi_dashboard_path", "")).strip() or (str((output_dir / "kpi_dashboard.html").resolve()) if output_dir else "")
        orchestration_path = str(row.get("orchestration_intelligence_dashboard_path", "")).strip() or (str((output_dir / "orchestration_intelligence_dashboard.html").resolve()) if output_dir else "")
        products = int(row.get("total_products", 0) or 0)
        closure = float(row.get("downstream_closure_ratio", 0.0) or 0.0)
        rows_html.append(
            "<tr>"
            f"<td>run_{int(row.get('run_index', 0) or 0):02d}</td>"
            f"<td>{products}</td>"
            f"<td>{closure:.3f}</td>"
            f"<td>{float(row.get('wall_clock_sec', 0.0) or 0.0):.1f}s</td>"
            f"<td>{products - baseline_products:+d}</td>"
            f"<td>{closure - baseline_closure:+.3f}</td>"
            f"<td>{products - prev_products:+d}</td>"
            f"<td>{closure - prev_closure:+.3f}</td>"
            f"<td>{'on' if bool(row.get('evaluator_enabled', False)) else 'off'}</td>"
            f"<td><a href='{escape(kpi_dashboard_path)}'>KPI</a> | <a href='{escape(orchestration_path)}'>Orch</a> | <a href='{escape(reflection_md_path)}'>Reflection</a></td>"
            "</tr>"
        )
        prev_products = products
        prev_closure = closure
    return "".join(rows_html)


def _summary_preview(text: str, limit: int = 160) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "-"
    if len(raw) <= limit:
        return raw
    return raw[: limit - 3].rstrip() + "..."


def _reflection_summary_cell(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return "-"
    preview = _summary_preview(raw, limit=150)
    escaped_preview = escape(preview)
    escaped_full = escape(raw)
    return (
        "<details class='summary-toggle'>"
        f"<summary title='{escaped_full}'>{escaped_preview}</summary>"
        f"<div class='summary-body' title='{escaped_full}'>{escaped_full}</div>"
        "</details>"
    )


def export_series_dashboard(*, parent_output_dir: Path, analysis: dict[str, Any] | None = None) -> Path | None:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception:
        return None

    analysis_payload = analysis if isinstance(analysis, dict) else build_series_analysis(parent_output_dir=parent_output_dir)
    runs = analysis_payload.get("runs", [])
    runs = runs if isinstance(runs, list) else []
    if not runs:
        return None

    parent_output_dir.mkdir(parents=True, exist_ok=True)
    output_path = parent_output_dir / "series_dashboard.html"

    run_labels = [f"run_{int(row.get('run_index', 0) or 0):02d}" for row in runs]
    products = [int(row.get("total_products", 0) or 0) for row in runs]
    closures = [float(row.get("downstream_closure_ratio", 0.0) or 0.0) for row in runs]
    wall_clock = [float(row.get("wall_clock_sec", 0.0) or 0.0) for row in runs]
    baseline_products = products[0]
    baseline_closure = closures[0]
    delta_vs_baseline_products = [value - baseline_products for value in products]
    delta_vs_baseline_closure = [value - baseline_closure for value in closures]

    fig = make_subplots(
        rows=3,
        cols=1,
        subplot_titles=(
            "Run-Level Products and Closure",
            "Delta vs run_01",
            "Wall-Clock by Run",
        ),
        specs=[[{"secondary_y": True}], [{}], [{}]],
        vertical_spacing=0.12,
    )
    fig.add_trace(go.Bar(name="Products", x=run_labels, y=products, marker_color="#1d4e89"), row=1, col=1, secondary_y=False)
    fig.add_trace(
        go.Scatter(
            name="Closure",
            x=run_labels,
            y=closures,
            mode="lines+markers",
            line=dict(color="#e76f51", width=3),
            marker=dict(size=8),
        ),
        row=1,
        col=1,
        secondary_y=True,
    )
    fig.add_trace(go.Bar(name="Products delta", x=run_labels, y=delta_vs_baseline_products, marker_color="#2a9d8f"), row=2, col=1)
    fig.add_trace(
        go.Scatter(
            name="Closure delta",
            x=run_labels,
            y=delta_vs_baseline_closure,
            mode="lines+markers",
            line=dict(color="#bc4749", width=3),
            marker=dict(size=8),
        ),
        row=2,
        col=1,
    )
    fig.add_trace(go.Bar(name="Wall-clock", x=run_labels, y=wall_clock, marker_color="#6c757d"), row=3, col=1)

    fig.update_layout(
        title=dict(text="Run-Series Dashboard", x=0.02, xanchor="left"),
        height=1200,
        margin=dict(l=50, r=50, t=120, b=90),
        legend=dict(orientation="h", yanchor="top", y=1.08, xanchor="center", x=0.5),
        plot_bgcolor="#fbfbfd",
        paper_bgcolor="#ffffff",
    )
    fig.update_yaxes(title_text="Products", row=1, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Closure ratio", tickformat=".0%", row=1, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Delta", row=2, col=1)
    fig.update_yaxes(title_text="Wall-clock (sec)", row=3, col=1)
    fig.update_xaxes(title_text="Run", row=3, col=1)

    figure_html = fig.to_html(full_html=False, include_plotlyjs=True)

    knowledge_sections = analysis_payload.get("knowledge_sections", {})
    knowledge_sections = knowledge_sections if isinstance(knowledge_sections, dict) else {}
    persistent_lessons = knowledge_sections.get("persistent_lessons", [])
    latest_lessons = knowledge_sections.get("latest_lessons", [])
    detector_guidance = knowledge_sections.get("detector_guidance", [])
    planner_guidance = knowledge_sections.get("planner_guidance", [])

    summary_text = escape(_safe_text(analysis_payload.get("analysis_summary", ""), 400))
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Run-Series Dashboard</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 0; background: #f6f8fb; color: #14213d; }}
    .page {{ max-width: 1400px; margin: 0 auto; padding: 24px 24px 40px; }}
    .hero {{ background: linear-gradient(135deg, #17324d, #355c7d); color: white; padding: 20px 24px; border-radius: 16px; }}
    .hero h1 {{ margin: 0 0 8px; font-size: 28px; }}
    .hero p {{ margin: 0; font-size: 15px; line-height: 1.5; }}
    .grid {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin-top: 18px; }}
    .card {{ background: white; border: 1px solid #dbe2ea; border-radius: 14px; padding: 16px; box-shadow: 0 4px 14px rgba(15, 23, 42, 0.05); }}
    .card .label {{ font-size: 12px; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: #52616b; }}
    .card .value {{ margin-top: 8px; font-size: 20px; font-weight: 700; color: #14213d; }}
    .section {{ margin-top: 22px; }}
    .section h2 {{ margin: 0 0 12px; font-size: 20px; }}
    .lessons {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }}
    .lesson-box {{ background: white; border: 1px solid #dbe2ea; border-radius: 14px; padding: 16px; }}
    .lesson-box h3 {{ margin: 0 0 10px; font-size: 16px; }}
    .lesson-box ul {{ margin: 0; padding-left: 18px; line-height: 1.55; }}
    details.summary-toggle {{ cursor: pointer; }}
    details.summary-toggle summary {{ list-style: none; font-weight: 500; line-height: 1.5; }}
    details.summary-toggle summary::-webkit-details-marker {{ display: none; }}
    details.summary-toggle summary::after {{ content: " 펼치기"; color: #0a66c2; font-weight: 600; font-size: 12px; }}
    details.summary-toggle[open] summary::after {{ content: " 접기"; }}
    .summary-body {{ margin-top: 8px; white-space: normal; line-height: 1.6; color: #1f2937; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 14px; overflow: hidden; border: 1px solid #dbe2ea; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf2f7; text-align: left; font-size: 14px; vertical-align: top; }}
    th {{ background: #f8fafc; font-weight: 700; }}
    tr:last-child td {{ border-bottom: none; }}
    a {{ color: #0a66c2; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .muted {{ color: #52616b; font-size: 13px; }}
    @media (max-width: 1100px) {{
      .grid, .lessons {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <h1>Run-Series Dashboard</h1>
      <p>{summary_text}</p>
      <p class="muted">Parent output: {escape(str(parent_output_dir.resolve()))}</p>
    </section>

    <section class="grid">
      {_metric_cards_html(analysis_payload)}
    </section>

    <section class="section">
      {figure_html}
    </section>

    <section class="section">
      <h2>Run Comparison</h2>
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Products</th>
            <th>Closure</th>
            <th>Wall-clock</th>
            <th>Δ vs run_01 Products</th>
            <th>Δ vs run_01 Closure</th>
            <th>Δ vs Prev Products</th>
            <th>Δ vs Prev Closure</th>
            <th>Evaluator</th>
            <th>Artifacts</th>
          </tr>
        </thead>
        <tbody>
          {_run_rows_html(runs)}
        </tbody>
      </table>
    </section>

    <section class="section">
      <h2>Knowledge State</h2>
      <div class="lessons">
        {_lesson_list_html("Persistent Lessons", persistent_lessons if isinstance(persistent_lessons, list) else [])}
        {_lesson_list_html("Latest Lessons", latest_lessons if isinstance(latest_lessons, list) else [])}
        {_lesson_list_html("Detector Guidance", detector_guidance if isinstance(detector_guidance, list) else [])}
        {_lesson_list_html("Planner Guidance", planner_guidance if isinstance(planner_guidance, list) else [])}
      </div>
    </section>

    <section class="section">
      <h2>Carry-Forward Trend by Run</h2>
      <table>
        <thead>
          <tr>
            <th>Run</th>
            <th>Reflection Summary</th>
            <th>Carry-Forward Lessons</th>
          </tr>
        </thead>
        <tbody>
          {''.join(
              f"<tr><td>run_{int(row.get('run_index', 0) or 0):02d}</td>"
              f"<td>{_reflection_summary_cell(str(row.get('reflection_summary', '-')))}</td>"
              f"<td><ul>{''.join(f'<li>{escape(_safe_text(item, 180))}</li>' for item in (row.get('carry_forward_lessons', []) if isinstance(row.get('carry_forward_lessons', []), list) else [])) or '<li>-</li>'}</ul></td></tr>"
              for row in runs
          )}
        </tbody>
      </table>
    </section>
  </div>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    return output_path
