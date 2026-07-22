from __future__ import annotations

import argparse
import html
from pathlib import Path

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.factory_policy_comparison.common import (
    AUDIT_CSV,
    DASHBOARD_HTML,
    DEFAULT_CONFIG_PATH,
    FAIRNESS_CSV,
    MODE_SUMMARY_CSV,
    MODE_WORKER_SUMMARY_CSV,
    RUN_SUMMARY_CSV,
    STATUS_CSV,
    ExperimentConfig,
    load_experiment_config,
    read_csv,
    read_json,
    relative_link,
)


RANKING_METRICS = [
    ("Throughput", "throughput_per_sim_hour.mean", True),
    ("Lead Time", "completed_product_lead_time_avg_min.mean", False),
    ("Incidents", "humanoid_incident_total.mean", False),
    ("Blocked Ratio", "humanoid_blocked_ratio_avg.mean", False),
]

CHART_COLORS = [
    "#2563eb",
    "#16a34a",
    "#dc2626",
    "#9333ea",
    "#ea580c",
    "#0891b2",
    "#4f46e5",
    "#65a30d",
]


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _table(rows: list[dict[str, str]], columns: list[str], *, limit: int | None = None) -> str:
    shown = rows[:limit] if limit is not None else rows
    head = "".join(f"<th>{_esc(col)}</th>" for col in columns)
    body = []
    for row in shown:
        body.append("<tr>" + "".join(f"<td>{_esc(row.get(col, ''))}</td>" for col in columns) + "</tr>")
    if not body:
        body.append(f"<tr><td colspan='{len(columns)}'>No rows</td></tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _float(row: dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, ""))
    except ValueError:
        return 0.0


def _float_or_none(row: dict[str, str], key: str) -> float | None:
    try:
        return float(row.get(key, ""))
    except (TypeError, ValueError):
        return None


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    total_seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _runtime_summary_rows(status_rows: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    rows_with_elapsed = [
        row
        for row in status_rows
        if _float_or_none(row, "elapsed_sec") is not None
    ]
    if not rows_with_elapsed:
        return "-", [{"metric": "Run command time", "value": "No elapsed_sec rows found"}]

    elapsed_values = [float(_float_or_none(row, "elapsed_sec") or 0.0) for row in rows_with_elapsed]
    total_elapsed = sum(elapsed_values)
    average_elapsed = total_elapsed / len(elapsed_values) if elapsed_values else 0.0
    slowest = max(rows_with_elapsed, key=lambda row: float(_float_or_none(row, "elapsed_sec") or 0.0))
    slowest_label = (
        f"{_format_duration(_float_or_none(slowest, 'elapsed_sec'))} "
        f"({slowest.get('mode', '')}, workers={slowest.get('worker_count', '')}, seed={slowest.get('seed', '')})"
    )
    completed_elapsed = sum(
        float(_float_or_none(row, "elapsed_sec") or 0.0)
        for row in rows_with_elapsed
        if row.get("status") == "completed"
    )
    failed_elapsed = total_elapsed - completed_elapsed
    return _format_duration(total_elapsed), [
        {"metric": "Total run command time", "value": _format_duration(total_elapsed)},
        {"metric": "Completed-run command time", "value": _format_duration(completed_elapsed)},
        {"metric": "Failed/skipped command time", "value": _format_duration(failed_elapsed)},
        {"metric": "Average run command time", "value": _format_duration(average_elapsed)},
        {"metric": "Slowest run", "value": slowest_label},
        {"metric": "Runs with elapsed_sec", "value": str(len(rows_with_elapsed))},
    ]


def _line_chart(rows: list[dict[str, str]], metric: str, title: str) -> str:
    points_by_mode: dict[str, list[tuple[int, float]]] = {}
    for row in rows:
        mode = str(row.get("mode", ""))
        worker_count = _float_or_none(row, "worker_count")
        value = _float_or_none(row, metric)
        if not mode or worker_count is None or value is None:
            continue
        points_by_mode.setdefault(mode, []).append((int(worker_count), value))
    for points in points_by_mode.values():
        points.sort(key=lambda item: item[0])
    all_points = [point for points in points_by_mode.values() for point in points]
    if not all_points:
        return f"<section class='panel'><h2>{_esc(title)}</h2><div class='empty'>No chart data</div></section>"

    width, height = 760, 310
    left, right, top, bottom = 58, 22, 28, 54
    xs = sorted({x for x, _value in all_points})
    y_values = [value for _x, value in all_points]
    y_min = min(0.0, min(y_values))
    y_max = max(y_values)
    if y_max == y_min:
        y_max = y_min + 1.0

    def sx(worker_count: int) -> float:
        if len(xs) == 1:
            return left + (width - left - right) / 2
        return left + ((worker_count - min(xs)) / (max(xs) - min(xs))) * (width - left - right)

    def sy(value: float) -> float:
        return height - bottom - ((value - y_min) / (y_max - y_min)) * (height - top - bottom)

    grid = []
    for index in range(5):
        ratio = index / 4
        value = y_min + (y_max - y_min) * ratio
        y = sy(value)
        grid.append(f"<line x1='{left}' y1='{y:.1f}' x2='{width-right}' y2='{y:.1f}' class='gridline' />")
        grid.append(f"<text x='{left-8}' y='{y+4:.1f}' class='axis-label' text-anchor='end'>{value:.2g}</text>")
    for worker_count in xs:
        x = sx(worker_count)
        grid.append(f"<text x='{x:.1f}' y='{height-22}' class='axis-label' text-anchor='middle'>{worker_count}</text>")

    series = []
    legend = []
    for index, (mode, points) in enumerate(sorted(points_by_mode.items())):
        color = CHART_COLORS[index % len(CHART_COLORS)]
        coords = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in points)
        series.append(f"<polyline points='{coords}' fill='none' stroke='{color}' stroke-width='2.5' />")
        for x, y in points:
            series.append(f"<circle cx='{sx(x):.1f}' cy='{sy(y):.1f}' r='3.5' fill='{color}'><title>{_esc(mode)} workers={x}: {y:.4g}</title></circle>")
        legend_y = 20 + index * 18
        legend.append(f"<rect x='{width-235}' y='{legend_y-9}' width='10' height='10' fill='{color}' />")
        legend.append(f"<text x='{width-220}' y='{legend_y}' class='legend'>{_esc(mode)}</text>")

    svg = (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{_esc(title)}'>"
        f"<text x='{left}' y='18' class='chart-title'>{_esc(title)}</text>"
        + "".join(grid)
        + f"<line x1='{left}' y1='{height-bottom}' x2='{width-right}' y2='{height-bottom}' class='axis' />"
        + f"<line x1='{left}' y1='{top}' x2='{left}' y2='{height-bottom}' class='axis' />"
        + "".join(series)
        + "".join(legend)
        + "</svg>"
    )
    return f"<section class='panel chart-panel'>{svg}</section>"


def _ranking_tables(mode_rows: list[dict[str, str]]) -> str:
    panels = []
    for label, metric, higher_is_better in RANKING_METRICS:
        ranked = sorted(mode_rows, key=lambda row: _float(row, metric), reverse=higher_is_better)
        rows = [
            {
                "rank": index + 1,
                "mode": row.get("mode", ""),
                metric: row.get(metric, ""),
            }
            for index, row in enumerate(ranked)
        ]
        panels.append(f"<section class='panel'><h2>{_esc(label)} Ranking</h2>{_table(rows, ['rank', 'mode', metric])}</section>")
    return "<div class='grid'>" + "".join(panels) + "</div>"


def _run_links(output_root: Path, run_rows: list[dict[str, str]]) -> str:
    rows = []
    for row in run_rows:
        run_dir = Path(row.get("run_dir", ""))
        links = []
        for name, label in [
            ("results_dashboard.html", "Hub"),
            ("kpi_dashboard.html", "KPI"),
            ("gantt.html", "Gantt"),
            ("dashboard_manifest.json", "Manifest"),
        ]:
            target = run_dir / name
            if target.exists():
                links.append(f"<a href='{_esc(relative_link(output_root / DASHBOARD_HTML, target))}'>{label}</a>")
        rows.append((row.get("mode", ""), row.get("worker_count", ""), row.get("seed", ""), row.get("status", ""), " | ".join(links)))
    body = "".join(
        f"<tr><td>{_esc(mode)}</td><td>{_esc(worker_count)}</td><td>{_esc(seed)}</td><td>{_esc(status)}</td><td>{links}</td></tr>"
        for mode, worker_count, seed, status, links in rows
    )
    if not body:
        body = "<tr><td colspan='5'>No rows</td></tr>"
    return (
        "<table><thead><tr><th>mode</th><th>worker_count</th><th>seed</th><th>status</th><th>links</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def render_dashboard(output_root: Path, cfg: ExperimentConfig) -> Path:
    output_root = output_root.resolve()
    run_rows = read_csv(output_root / RUN_SUMMARY_CSV)
    mode_rows = read_csv(output_root / MODE_SUMMARY_CSV)
    mode_worker_rows = read_csv(output_root / MODE_WORKER_SUMMARY_CSV)
    fairness_rows = read_csv(output_root / FAIRNESS_CSV)
    audit_rows = read_csv(output_root / AUDIT_CSV)
    status_rows = read_csv(output_root / STATUS_CSV)
    experiment_plan = read_json(output_root / "experiment_plan.json")
    plan_scenario = str(experiment_plan.get("scenario", cfg.scenario) or cfg.scenario)
    total_runtime_label, runtime_rows = _runtime_summary_rows(status_rows)
    completed_count = sum(1 for row in run_rows if row.get("status") == "completed")
    failed_fairness = sum(1 for row in fairness_rows if str(row.get("fairness_pass", "")).lower() != "true")
    failed_audits = sum(
        1
        for row in audit_rows
        if row.get("artifact_audit_status") != "pass" or row.get("kpi_audit_status") != "pass"
    )
    fairness_cols = [
        "mode",
        "worker_count",
        "seed",
        "scenario_ok",
        "worker_count_ok",
        "horizon_ok",
        "seed_ok",
        "pre_run_matches_reference",
        "artifact_errors_empty",
        "fairness_pass",
    ]
    summary_cols = [
        "mode",
        "completed_run_count",
        "total_products.mean",
        "throughput_per_sim_hour.mean",
        "completed_product_lead_time_avg_min.mean",
        "humanoid_incident_total.mean",
        "humanoid_blocked_ratio_avg.mean",
        "otc.mean",
    ]
    mode_worker_cols = [
        "mode",
        "worker_count",
        "completed_run_count",
        "total_products.mean",
        "total_products.std",
        "throughput_per_sim_hour.mean",
        "throughput_per_sim_hour.std",
        "throughput_per_sim_hour.marginal_gain",
        "completed_product_lead_time_avg_min.mean",
        "humanoid_incident_total.mean",
        "humanoid_blocked_ratio_avg.mean",
        "otc.mean",
    ]
    chart_rows = mode_worker_rows if mode_worker_rows else mode_rows
    plan_modes = experiment_plan.get("modes") if isinstance(experiment_plan.get("modes"), list) else cfg.modes
    plan_workers = (
        experiment_plan.get("worker_counts")
        if isinstance(experiment_plan.get("worker_counts"), list)
        else cfg.worker_counts
    )
    plan_seeds = experiment_plan.get("seeds") if isinstance(experiment_plan.get("seeds"), list) else cfg.seeds
    expected_runs = int(
        experiment_plan.get("run_count", len(plan_modes) * len(plan_workers) * len(plan_seeds))
        or 0
    )
    worker_counts = sorted({str(count) for count in plan_workers}, key=lambda item: int(item))
    horizon_days = int(experiment_plan.get("horizon_days", cfg.horizon_days) or cfg.horizon_days)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Factory Policy Comparison</title>
  <style>
    body {{ margin: 0; font-family: Segoe UI, Arial, sans-serif; background: #f4f7fb; color: #14213d; }}
    header {{ padding: 28px 36px; background: #0f1b2d; color: #f8fbff; }}
    main {{ padding: 28px 36px 48px; }}
    h1, h2 {{ margin: 0 0 14px; }}
    .sub {{ color: #b9c7da; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 22px; }}
    .card, .panel {{ background: #fff; border: 1px solid #d9e2ef; border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(15, 27, 45, 0.06); }}
    .card .label {{ color: #59708f; font-size: 0.82rem; text-transform: uppercase; letter-spacing: .04em; }}
    .card .value {{ font-size: 1.55rem; font-weight: 700; margin-top: 8px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 18px; margin-bottom: 22px; }}
    .chart-grid {{ display: grid; grid-template-columns: repeat(2, minmax(360px, 1fr)); gap: 18px; margin-bottom: 22px; }}
    .chart-panel {{ overflow: hidden; }}
    svg {{ width: 100%; height: auto; }}
    .axis {{ stroke: #6b7f99; stroke-width: 1; }}
    .gridline {{ stroke: #dbe5f2; stroke-width: 1; }}
    .axis-label {{ fill: #5f728c; font-size: 12px; }}
    .legend {{ fill: #243b5a; font-size: 11px; }}
    .chart-title {{ fill: #14213d; font-weight: 700; font-size: 15px; }}
    .empty {{ color: #71839b; padding: 28px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
    th, td {{ border-bottom: 1px solid #e4ebf5; padding: 8px 9px; text-align: left; vertical-align: top; }}
    th {{ color: #29466d; background: #eef4fb; }}
    a {{ color: #1d66c2; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .section {{ margin-bottom: 22px; }}
  </style>
</head>
<body>
<header>
  <h1>Factory Policy Comparison</h1>
  <div class="sub">Scenario: {_esc(plan_scenario)} | horizon: {_esc(horizon_days)} days | workers: {_esc(', '.join(worker_counts))} | seeds: {_esc(plan_seeds)}</div>
</header>
<main>
  <section class="cards">
    <div class="card"><div class="label">Modes</div><div class="value">{len(plan_modes)}</div></div>
    <div class="card"><div class="label">Expected Runs</div><div class="value">{expected_runs}</div></div>
    <div class="card"><div class="label">Completed</div><div class="value">{completed_count}</div></div>
    <div class="card"><div class="label">Run Time</div><div class="value">{_esc(total_runtime_label)}</div></div>
    <div class="card"><div class="label">Audit/Fairness Issues</div><div class="value">{failed_audits + failed_fairness}</div></div>
  </section>
  <section class="panel section">
    <h2>Experiment Runtime</h2>
    <p>Elapsed time is summed from <code>run_status.csv</code> command durations and excludes dashboard/audit post-processing.</p>
    {_table(runtime_rows, ["metric", "value"])}
  </section>
  <section class="chart-grid">
    {_line_chart(chart_rows, "throughput_per_sim_hour.mean", "Throughput vs Worker Count")}
    {_line_chart(chart_rows, "total_products.mean", "Total Products vs Worker Count")}
    {_line_chart(chart_rows, "throughput_per_sim_hour.marginal_gain", "Marginal Throughput Gain")}
    {_line_chart(chart_rows, "humanoid_incident_total.mean", "Incidents vs Worker Count")}
    {_line_chart(chart_rows, "humanoid_blocked_ratio_avg.mean", "Blocked Ratio vs Worker Count")}
    {_line_chart(chart_rows, "otc.mean", "OTC vs Worker Count")}
  </section>
  <section class="panel section">
    <h2>KPI Summary by Mode and Worker Count</h2>
    {_table(mode_worker_rows, mode_worker_cols)}
  </section>
  <section class="panel section">
    <h2>Overall KPI Summary by Mode</h2>
    {_table(mode_rows, summary_cols)}
  </section>
  {_ranking_tables(mode_rows)}
  <section class="panel section">
    <h2>Fairness Check</h2>
    <p>Pre-run diagnostics are compared within the same worker-count group; different worker counts are expected to have different environment fingerprints.</p>
    {_table(fairness_rows, fairness_cols)}
  </section>
  <section class="panel section">
    <h2>Run Links</h2>
    {_run_links(output_root, run_rows)}
  </section>
</main>
</body>
</html>
"""
    output_path = output_root / DASHBOARD_HTML
    output_path.write_text(html_text, encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render factory policy comparison dashboard.")
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_experiment_config(args.config)
    path = render_dashboard(args.output_root, cfg)
    print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
