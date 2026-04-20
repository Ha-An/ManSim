from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote


PRIMARY_ARTIFACTS = [
    "results_dashboard.html",
    "kpi_dashboard.html",
    "gantt.html",
]

ARTIFACT_LABELS = {
    "results_dashboard.html": "Results Hub",
    "kpi_dashboard.html": "KPI",
    "gantt.html": "Gantt",
    "task_priority_dashboard.html": "Task Priority",
    "knowledge_dashboard.html": "Knowledge",
    "reasoning_dashboard.html": "Reasoning",
    "series_dashboard.html": "Series",
}


def _decision_mode(run: dict[str, Any] | None) -> str:
    if not isinstance(run, dict):
        return ""
    run_meta = run.get("run_meta", {}) if isinstance(run.get("run_meta", {}), dict) else {}
    return str(run_meta.get("decision_mode", "")).strip().lower()


def _total_runs(run: dict[str, Any] | None) -> int:
    if not isinstance(run, dict):
        return 1
    run_meta = run.get("run_meta", {}) if isinstance(run.get("run_meta", {}), dict) else {}
    try:
        return max(1, int(run_meta.get("total_runs", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _show_task_priority(run: dict[str, Any] | None) -> bool:
    mode = _decision_mode(run)
    return mode in {"adaptive_priority", "fixed_priority", "fixed_task_assignment", "openclaw_adaptive_priority"}


def _show_reasoning(run: dict[str, Any] | None) -> bool:
    mode = _decision_mode(run)
    return mode in {"llm_planner", "openclaw_adaptive_priority"}


def _show_knowledge(run: dict[str, Any] | None) -> bool:
    mode = _decision_mode(run)
    if mode == "llm_planner":
        return True
    if mode == "openclaw_adaptive_priority":
        return _total_runs(run) > 1
    return False


def _find_run(manifest: dict[str, Any] | None, run_id: str | None) -> dict[str, Any] | None:
    if not isinstance(manifest, dict):
        return None
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    target = str(run_id or manifest.get("current_run", "")).strip()
    for row in runs:
        if isinstance(row, dict) and str(row.get("id", "")).strip() == target:
            return row
    return runs[-1] if runs and isinstance(runs[-1], dict) else None


def rel_href(current_page: Path, target: str | Path | None) -> str:
    raw = str(target or "").strip()
    if not raw:
        return "#"
    if raw.startswith(("http://", "https://")):
        return raw
    try:
        rel = os.path.relpath(str(Path(raw).resolve()), start=str(current_page.resolve().parent))
    except OSError:
        rel = raw
    return rel.replace("\\", "/")


def build_replay_app_url(*, port: int, manifest_path: Path | None = None, run_id: str | None = None, events_path: Path | None = None, series_root: Path | None = None) -> str:
    base = f"http://localhost:{int(port)}"
    params: list[str] = []
    if manifest_path is not None:
        params.append(f"manifest_path={quote(Path(manifest_path).resolve().as_posix(), safe='')}")
    if series_root is not None:
        params.append(f"series_root={quote(Path(series_root).resolve().as_posix(), safe='')}")
    if run_id:
        params.append(f"run={quote(str(run_id), safe='')}")
    if events_path is not None:
        params.append(f"events_path={quote(Path(events_path).resolve().as_posix(), safe='')}")
    return f"{base}/?{'&'.join(params)}" if params else base


def _run_selector_options(*, manifest: dict[str, Any] | None, current_page_path: Path, current_artifact: str, current_run_id: str | None) -> tuple[str, str]:
    if not isinstance(manifest, dict):
        return ("", "")
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    if len(runs) <= 1:
        return ("", "")
    option_rows: list[str] = []
    current_value = ""
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_id = str(run.get("id", "")).strip()
        artifacts = run.get("artifacts", {}) if isinstance(run.get("artifacts", {}), dict) else {}
        if current_artifact == "series_dashboard.html":
            target = artifacts.get("results_dashboard.html", "")
        else:
            target = artifacts.get(current_artifact, "") or artifacts.get("results_dashboard.html", "")
        href = rel_href(current_page_path, target)
        selected = " selected" if run_id == str(current_run_id or manifest.get("current_run", "")).strip() else ""
        if selected:
            current_value = href
        option_rows.append(f"<option value=\"{html.escape(href)}\"{selected}>{html.escape(str(run.get('label', run_id)))}</option>")
    if not option_rows:
        return ("", "")
    selector_html = (
        "<label class='selector-label'>Run"
        "<select id='run-selector' class='selector' onchange='if(this.value){window.location.href=this.value;}'>"
        + "".join(option_rows)
        + "</select></label>"
    )
    return selector_html, current_value


def _nav_links(*, manifest: dict[str, Any] | None, current_page_path: Path, current_artifact: str, current_run_id: str | None, manifest_path: Path | None) -> str:
    run = _find_run(manifest, current_run_id)
    if run is None:
        return ""
    artifacts = run.get("artifacts", {}) if isinstance(run.get("artifacts", {}), dict) else {}
    items: list[tuple[str, str, bool]] = []
    for artifact in PRIMARY_ARTIFACTS:
        target = artifacts.get(artifact, "")
        href = rel_href(current_page_path, target)
        items.append((ARTIFACT_LABELS.get(artifact, artifact), href, artifact == current_artifact))
    replay_href = build_replay_app_url(
        port=int(manifest.get("streamlit_preferred_port", 8505) or 8505) if isinstance(manifest, dict) else 8505,
        manifest_path=manifest_path,
        run_id=str(run.get("id", "")).strip(),
        events_path=Path(str(artifacts.get("events.jsonl", ""))) if str(artifacts.get("events.jsonl", "")).strip() else None,
        series_root=Path(str(manifest.get("series_root", ""))) if isinstance(manifest, dict) and str(manifest.get("series_root", "")).strip() else None,
    )
    items.insert(2, ("Replay", replay_href, current_artifact == "replay_app"))
    if _show_task_priority(run):
        target = artifacts.get("task_priority_dashboard.html", "")
        href = rel_href(current_page_path, target)
        items.append((ARTIFACT_LABELS.get("task_priority_dashboard.html", "Task Priority"), href, current_artifact == "task_priority_dashboard.html"))
    if _show_reasoning(run):
        target = artifacts.get("reasoning_dashboard.html", "")
        href = rel_href(current_page_path, target)
        items.append((ARTIFACT_LABELS.get("reasoning_dashboard.html", "Reasoning"), href, current_artifact == "reasoning_dashboard.html"))
    if _show_knowledge(run):
        for artifact in ("knowledge_dashboard.html",):
            target = artifacts.get(artifact, "")
            href = rel_href(current_page_path, target)
            items.append((ARTIFACT_LABELS.get(artifact, artifact), href, artifact == current_artifact))
    if isinstance(manifest, dict) and not bool(manifest.get("single_run", True)):
        series_href = rel_href(current_page_path, Path(str(manifest.get("series_root", ""))) / "series_dashboard.html")
        items.append((ARTIFACT_LABELS["series_dashboard.html"], series_href, current_artifact == "series_dashboard.html"))
    links: list[str] = []
    for label, href, active in items:
        cls = "nav-link active" if active else "nav-link"
        external = href.startswith(("http://", "https://"))
        target = ' target="_blank" rel="noopener noreferrer"' if external else ""
        links.append(f"<a class='{cls}' href='{html.escape(href)}'{target}>{html.escape(label)}</a>")
    return "".join(links)


def _series_strip(manifest: dict[str, Any] | None, current_run_id: str | None) -> str:
    if not isinstance(manifest, dict):
        return ""
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    if not runs:
        return ""
    analysis = manifest.get("analysis", {}) if isinstance(manifest.get("analysis", {}), dict) else {}
    run = _find_run(manifest, current_run_id)
    current_products = 0
    current_closure = 0.0
    if run:
        kpi = run.get("kpi", {}) if isinstance(run.get("kpi", {}), dict) else {}
        current_products = int(kpi.get("total_products", 0) or 0)
        current_closure = float(kpi.get("downstream_closure_ratio", 0.0) or 0.0)
    peak = analysis.get("peak_run", {}) if isinstance(analysis.get("peak_run", {}), dict) else {}
    worst = analysis.get("worst_run", {}) if isinstance(analysis.get("worst_run", {}), dict) else {}
    parts = [
        f"<span><strong>Run</strong> {html.escape(str(current_run_id or manifest.get('current_run', '-')))}</span>",
        f"<span><strong>Products</strong> {current_products}</span>",
        f"<span><strong>Closure</strong> {current_closure:.3f}</span>",
    ]
    if len(runs) > 1:
        parts.extend(
            [
                f"<span><strong>Classification</strong> {html.escape(str(analysis.get('knowledge_effect_classification', 'mixed')))}</span>",
                f"<span><strong>Pattern</strong> {html.escape(str(analysis.get('performance_pattern', '-')))}</span>",
                f"<span><strong>Peak</strong> run_{int(peak.get('run_index', 0) or 0):02d}</span>",
                f"<span><strong>Worst</strong> run_{int(worst.get('run_index', 0) or 0):02d}</span>",
            ]
        )
    return "<div class='series-strip'>" + "".join(f"<div class='strip-pill'>{item}</div>" for item in parts) + "</div>"


def render_page_shell(
    *,
    title: str,
    current_page_path: Path,
    manifest: dict[str, Any] | None,
    manifest_path: Path | None,
    current_artifact: str,
    current_run_id: str | None,
    page_title: str,
    page_subtitle: str = "",
    body_html: str,
) -> str:
    nav_html = _nav_links(
        manifest=manifest,
        current_page_path=current_page_path,
        current_artifact=current_artifact,
        current_run_id=current_run_id,
        manifest_path=manifest_path,
    )
    selector_html, _current = _run_selector_options(
        manifest=manifest,
        current_page_path=current_page_path,
        current_artifact=current_artifact,
        current_run_id=current_run_id,
    )
    series_strip = _series_strip(manifest, current_run_id)
    subtitle_html = f"<p class='page-subtitle'>{html.escape(page_subtitle)}</p>" if str(page_subtitle).strip() else ""
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f3f6fb;
      --panel: #ffffff;
      --panel-2: #f8fbff;
      --ink: #182338;
      --muted: #617088;
      --line: #d6deea;
      --brand: #0f5cc0;
      --brand-soft: #eaf2ff;
      --good: #0f8c5b;
      --warn: #b9770e;
      --bad: #c0392b;
      --shadow: 0 10px 28px rgba(17, 24, 39, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: linear-gradient(180deg, #eef3fb 0%, #f7f9fc 100%); color: var(--ink); font-family: Segoe UI, Arial, sans-serif; }}
    a {{ color: var(--brand); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .page {{ max-width: 1460px; margin: 0 auto; padding: 18px 22px 36px; }}
    .topbar {{ display: flex; gap: 16px; align-items: center; justify-content: space-between; padding: 14px 18px; background: rgba(255,255,255,0.92); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); position: sticky; top: 12px; z-index: 10; backdrop-filter: blur(8px); }}
    .brand {{ font-size: 18px; font-weight: 800; letter-spacing: 0.01em; }}
    .topbar-right {{ display: flex; gap: 14px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }}
    .nav {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .nav-link {{ display: inline-flex; align-items: center; min-height: 36px; padding: 0 12px; border-radius: 999px; color: var(--ink); background: transparent; border: 1px solid transparent; font-weight: 600; }}
    .nav-link.active {{ background: var(--brand-soft); color: var(--brand); border-color: #cfe0ff; }}
    .selector-label {{ display: inline-flex; gap: 8px; align-items: center; font-size: 13px; color: var(--muted); font-weight: 700; }}
    .selector {{ min-width: 170px; padding: 8px 10px; border-radius: 10px; border: 1px solid var(--line); background: white; color: var(--ink); }}
    .hero {{ margin-top: 16px; background: linear-gradient(135deg, #17324d, #244a6d); color: white; border-radius: 20px; padding: 22px 24px; box-shadow: var(--shadow); }}
    .hero h1 {{ margin: 0; font-size: 28px; }}
    .page-subtitle {{ margin: 10px 0 0; color: rgba(255,255,255,0.86); line-height: 1.55; max-width: 1080px; }}
    .series-strip {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 16px; }}
    .strip-pill {{ padding: 8px 12px; border-radius: 999px; background: rgba(255,255,255,0.14); border: 1px solid rgba(255,255,255,0.18); font-size: 13px; color: #f3f7ff; }}
    .section {{ margin-top: 18px; }}
    .panel {{ background: var(--panel); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); padding: 18px; }}
    .panel h2 {{ margin: 0 0 12px; font-size: 20px; }}
    .panel h3 {{ margin: 0 0 10px; font-size: 16px; }}
    .muted {{ color: var(--muted); }}
    .grid {{ display: grid; gap: 14px; }}
    .grid.cards-4 {{ grid-template-columns: repeat(4, minmax(0, 1fr)); }}
    .grid.cards-3 {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    .grid.cards-2 {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .card {{ background: var(--panel); border: 1px solid var(--line); border-radius: 18px; box-shadow: var(--shadow); padding: 16px; }}
    .card .label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 700; }}
    .card .value {{ margin-top: 8px; font-size: 28px; font-weight: 800; line-height: 1.1; }}
    .card .sub {{ margin-top: 6px; color: var(--muted); font-size: 13px; line-height: 1.5; }}
    .artifact-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px; }}
    .artifact-card {{ display: block; background: var(--panel-2); border: 1px solid var(--line); border-radius: 18px; padding: 16px; color: var(--ink); box-shadow: var(--shadow); }}
    .artifact-card:hover {{ border-color: #b8c9e6; text-decoration: none; }}
    .artifact-card strong {{ display: block; font-size: 16px; margin-bottom: 8px; }}
    .artifact-card span {{ display: block; color: var(--muted); font-size: 13px; line-height: 1.5; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 14px; overflow: hidden; border: 1px solid var(--line); }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #ecf1f8; text-align: left; vertical-align: top; font-size: 14px; }}
    th {{ background: #f8fafc; font-weight: 700; }}
    tr:last-child td {{ border-bottom: none; }}
    .good {{ color: var(--good); font-weight: 700; }}
    .bad {{ color: var(--bad); font-weight: 700; }}
    .warn {{ color: var(--warn); font-weight: 700; }}
    ul.clean {{ margin: 0; padding-left: 18px; line-height: 1.6; }}
    code.inline {{ background: #edf3fb; border-radius: 6px; padding: 1px 6px; }}
    @media (max-width: 1200px) {{
      .grid.cards-4, .grid.cards-3, .grid.cards-2, .artifact-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .topbar {{ position: static; }}
    }}
    @media (max-width: 760px) {{
      .grid.cards-4, .grid.cards-3, .grid.cards-2, .artifact-grid {{ grid-template-columns: 1fr; }}
      .page {{ padding: 14px; }}
      .topbar {{ flex-direction: column; align-items: stretch; }}
      .topbar-right {{ justify-content: flex-start; }}
    }}
  </style>
</head>
<body>
  <div class=\"page\">
    <div class=\"topbar\">
      <div class=\"brand\">ManSim Dashboard Suite</div>
      <div class=\"topbar-right\">
        <nav class=\"nav\">{nav_html}</nav>
        {selector_html}
      </div>
    </div>
    <section class=\"hero\">
      <h1>{html.escape(page_title)}</h1>
      {subtitle_html}
      {series_strip}
    </section>
    {body_html}
  </div>
</body>
</html>"""
