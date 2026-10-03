from __future__ import annotations

import argparse
import csv
from html import escape
import json
from pathlib import Path
import sys
import webbrowser


HERE = Path(__file__).resolve().parent


MODE_LABELS = {
    "simulation_based_adp": "Simulation-Based ADP",
    "immediate_shared": "Immediate Shared",
    "immediate_dedicated_roles": "Immediate Dedicated",
    "rolling_horizon_shared": "Rolling Horizon Shared",
    "rolling_horizon_dedicated_roles": "Rolling Horizon Dedicated",
    "random_feasible_dispatch": "Random Feasible",
}
COLORS = {
    "simulation_based_adp": "#2f80ed",
    "immediate_shared": "#17a673",
    "immediate_dedicated_roles": "#f2b134",
    "rolling_horizon_shared": "#db5a42",
    "rolling_horizon_dedicated_roles": "#8c62d8",
    "random_feasible_dispatch": "#6d7b8d",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _fmt(value: object, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _line_chart(rows: list[dict[str, str]]) -> str:
    width, height = 980, 430
    left, right, top, bottom = 72, 24, 28, 64
    plot_w, plot_h = width - left - right, height - top - bottom
    workers = sorted({int(row["worker_count"]) for row in rows})
    values = [float(row["mean"]) for row in rows]
    lows = [float(row["ci95_low"]) for row in rows]
    highs = [float(row["ci95_high"]) for row in rows]
    if not workers or not values:
        return "<p class='empty'>No completed results.</p>"
    y_min = min(0.0, min(lows))
    y_max = max(highs)
    span = max(1.0, y_max - y_min)
    y_max += span * 0.08

    def x(worker: int) -> float:
        return left + (worker - min(workers)) / max(1, max(workers) - min(workers)) * plot_w

    def y(value: float) -> float:
        return top + (y_max - value) / max(1e-9, y_max - y_min) * plot_h

    parts = [f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='Completed products by policy and worker count'>"]
    for index in range(6):
        value = y_min + (y_max - y_min) * index / 5
        yy = y(value)
        parts.append(f"<line x1='{left}' y1='{yy:.2f}' x2='{width-right}' y2='{yy:.2f}' class='grid'/>")
        parts.append(f"<text x='{left-10}' y='{yy+4:.2f}' text-anchor='end' class='axis'>{value:.1f}</text>")
    for worker in workers:
        xx = x(worker)
        parts.append(f"<line x1='{xx:.2f}' y1='{top}' x2='{xx:.2f}' y2='{height-bottom}' class='grid vertical'/>")
        parts.append(f"<text x='{xx:.2f}' y='{height-bottom+28}' text-anchor='middle' class='axis'>{worker}</text>")
    by_mode: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_mode.setdefault(row["mode"], []).append(row)
    for mode, mode_rows in sorted(by_mode.items()):
        mode_rows.sort(key=lambda row: int(row["worker_count"]))
        color = COLORS.get(mode, "#ffffff")
        points = " ".join(f"{x(int(row['worker_count'])):.2f},{y(float(row['mean'])):.2f}" for row in mode_rows)
        parts.append(f"<polyline points='{points}' fill='none' stroke='{color}' stroke-width='3'/>")
        for row in mode_rows:
            xx = x(int(row["worker_count"]))
            yy = y(float(row["mean"]))
            low = y(float(row["ci95_low"]))
            high = y(float(row["ci95_high"]))
            parts.append(f"<line x1='{xx:.2f}' y1='{high:.2f}' x2='{xx:.2f}' y2='{low:.2f}' stroke='{color}' opacity='.65'/>")
            parts.append(f"<circle cx='{xx:.2f}' cy='{yy:.2f}' r='4' fill='{color}'/>")
    parts.append(f"<text x='{left+plot_w/2:.2f}' y='{height-10}' text-anchor='middle' class='label'>Worker count</text>")
    parts.append(f"<text x='18' y='{top+plot_h/2:.2f}' text-anchor='middle' transform='rotate(-90 18 {top+plot_h/2:.2f})' class='label'>Completed products / 5 days</text>")
    parts.append("</svg>")
    return "".join(parts)


def render(prepared: Path) -> Path:
    analysis = json.loads((prepared / "analysis_summary.json").read_text(encoding="utf-8"))
    experiment = json.loads((prepared / "experiment_plan.json").read_text(encoding="utf-8"))
    seed_count = len(experiment.get("test_seeds", []))
    training_replicates = int(experiment.get("training_replicates", 1))
    summaries = [
        row for row in _read_csv(prepared / "policy_worker_summary.csv")
        if row.get("metric") == "total_products"
    ]
    contrasts = _read_csv(prepared / "paired_contrasts.csv")
    fairness = _read_csv(prepared / "fairness_report.csv")
    capacity = json.loads((prepared / "theoretical_capacity.json").read_text(encoding="utf-8"))
    fairness_fail = sum(str(row.get("fairness_pass", "")).lower() != "true" for row in fairness)

    legend = "".join(
        f"<span><i style='background:{COLORS.get(mode, '#fff')}'></i>{escape(MODE_LABELS.get(mode, mode))}</span>"
        for mode in MODE_LABELS if any(row.get("mode") == mode for row in summaries)
    )
    table_rows = "".join(
        "<tr>"
        f"<td>{escape(MODE_LABELS.get(row['mode'], row['mode']))}</td>"
        f"<td>{row['worker_count']}</td>"
        f"<td>{_fmt(row['mean'])}</td>"
        f"<td>{_fmt(row['std_across_seed_units'])}</td>"
        f"<td>[{_fmt(row['ci95_low'])}, {_fmt(row['ci95_high'])}]</td>"
        f"<td>{row['environment_seed_count']}</td>"
        f"<td>{row['training_replicate_count'] or '-'}</td>"
        f"<td>{_fmt(row['between_training_replicate_sd']) if row['mode'] == 'simulation_based_adp' and training_replicates > 1 else '-'}</td>"
        "</tr>"
        for row in sorted(summaries, key=lambda item: (int(item["worker_count"]), item["mode"]))
    )
    contrast_rows = "".join(
        "<tr>"
        f"<td>{escape(str(row['worker_count']))}</td>"
        f"<td>{escape(MODE_LABELS.get(row['baseline_mode'], row['baseline_mode']))}</td>"
        f"<td>{_fmt(row['adp_minus_baseline_mean'])}</td>"
        f"<td>[{_fmt(row['ci95_low'])}, {_fmt(row['ci95_high'])}]</td>"
        f"<td>{_fmt(row['relative_improvement_pct']) if row['relative_improvement_pct'] else '-'}</td>"
        f"<td>{escape(str(row['win_count'] or '-'))}/{escape(str(row['tie_count'] or '-'))}/{escape(str(row['loss_count'] or '-'))}</td>"
        f"<td>{_fmt(row['holm_adjusted_p'], 4) if row['holm_adjusted_p'] else '-'}</td>"
        "</tr>"
        for row in contrasts
    )
    capacity_rows = "".join(
        "<tr>"
        f"<td>{row['worker_count']}</td>"
        f"<td>{row['theoretical_max_products']}</td>"
        f"<td>{_fmt(row['realistic_expected_products'])}</td>"
        f"<td>{row['material_capacity_bound']}</td>"
        f"<td>{row['time_capacity_bound']}</td>"
        f"<td>{row['worker_capacity_bound']}</td>"
        f"<td>{_fmt(100.0 * row['quality_yield'], 1)}%</td>"
        "</tr>"
        for row in capacity.get("rows", [])
    )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>mfg_flow_shop Confirmatory Policy Comparison</title>
<style>
:root {{ color-scheme: dark; --bg:#0b1220; --band:#111d2f; --line:#2c415d; --text:#e9f0f7; --muted:#9fb0c3; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.5 Inter,Segoe UI,sans-serif; letter-spacing:0; }}
header,section {{ padding:22px max(24px,calc((100vw - 1260px)/2)); }} header {{ background:#15243a; border-bottom:1px solid var(--line); }}
h1 {{ margin:0 0 5px; font-size:28px; }} h2 {{ margin:0 0 14px; font-size:19px; }} p {{ color:var(--muted); margin:6px 0; }}
.cards {{ display:grid; grid-template-columns:repeat(4,minmax(150px,1fr)); gap:10px; margin-top:18px; }}
.card {{ background:#0d1727; border:1px solid var(--line); border-radius:6px; padding:13px; }} .card b {{ display:block; font-size:22px; }}
.chart {{ background:#0d1727; border:1px solid var(--line); padding:14px; overflow:auto; }} svg {{ width:100%; min-width:720px; }}
.grid {{ stroke:#263b55; stroke-width:1; }} .vertical {{ opacity:.55; }} .axis {{ fill:#9fb0c3; font-size:12px; }} .label {{ fill:#d8e3ef; font-size:13px; }}
.legend {{ display:flex; flex-wrap:wrap; gap:14px; margin:12px 0; color:var(--muted); }} .legend i {{ width:12px; height:12px; display:inline-block; margin-right:6px; }}
.table-wrap {{ overflow:auto; border:1px solid var(--line); }} table {{ width:100%; border-collapse:collapse; white-space:nowrap; }} th,td {{ padding:9px 11px; border-bottom:1px solid #20334b; text-align:right; }} th:first-child,td:first-child {{ text-align:left; }} th {{ position:sticky; top:0; background:#15243a; }}
.links a {{ color:#77b9ff; margin-right:16px; }} .ok {{ color:#72d6a0; }} .fail {{ color:#ff8274; }}
@media(max-width:760px) {{ .cards {{ grid-template-columns:repeat(2,minmax(130px,1fr)); }} h1 {{ font-size:22px; }} }}
</style></head><body>
<header><h1>Confirmatory Policy Comparison</h1><p>mfg_flow_shop · worker 2–6 · {seed_count} common environment seeds · ADP {training_replicates} training run per worker count</p>
<div class="cards"><div class="card"><span>Analysis</span><b class="{'ok' if analysis['status']=='pass' else 'fail'}">{escape(str(analysis['status']).upper())}</b></div>
<div class="card"><span>Eligible runs</span><b>{analysis['eligible_run_count']} / {analysis['expected_run_count']}</b></div>
<div class="card"><span>Fairness groups</span><b>{analysis['fairness_pass_count']} / {analysis['fairness_group_count']}</b></div>
<div class="card"><span>Fairness failures</span><b>{fairness_fail}</b></div></div></header>
<section><h2>Five-day production by fleet size</h2><div class="legend">{legend}</div><div class="chart">{_line_chart(summaries)}</div>
<p>Points are means over {seed_count} common environment-seed units. Error bars are paired-bootstrap 95% intervals. ADP uses one fixed worker-specific checkpoint, so these intervals measure environment-seed uncertainty and do not estimate training-seed variability.</p></section>
<section><h2>Production summary</h2><div class="table-wrap"><table><thead><tr><th>Policy</th><th>Workers</th><th>Mean</th><th>Seed SD</th><th>95% CI</th><th>Seeds</th><th>ADP reps</th><th>Between-rep SD</th></tr></thead><tbody>{table_rows}</tbody></table></div></section>
<section><h2>Capacity references</h2><div class="table-wrap"><table><thead><tr><th>Workers</th><th>Theoretical maximum</th><th>Realistic expected</th><th>Material bound</th><th>Time bound</th><th>Worker bound</th><th>Quality yield</th></tr></thead><tbody>{capacity_rows}</tbody></table></div>
<p>The theoretical maximum includes all required station processing, handling and shortest-path loaded travel, parallel machines, finite buffers, inspection and charging, while relaxing failures, incidents and dynamic queueing. The realistic reference uses triangular means, active-processing failures, preventive maintenance, charging, quality yield and expected incident burden. It is a planning reference, not a hard upper bound.</p></section>
<section><h2>ADP paired contrasts</h2><div class="table-wrap"><table><thead><tr><th>Workers</th><th>Baseline</th><th>Mean difference</th><th>95% CI</th><th>Improvement %</th><th>Win/Tie/Loss</th><th>Holm p</th></tr></thead><tbody>{contrast_rows}</tbody></table></div>
<p>Worker-level intervals use the registered paired bootstrap over common environment seeds. Overall rows weight worker counts equally. Holm adjustment applies to worker-level secondary contrasts.</p></section>
<section class="links"><h2>Audit and data</h2><a href="analysis_summary.json">Analysis summary</a><a href="fairness_report.csv">Fairness</a><a href="evaluation_raw.csv">Raw runs</a><a href="policy_worker_summary.csv">Policy summary</a><a href="paired_contrasts.csv">Paired contrasts</a><a href="theoretical_capacity.json">Capacity model</a></section>
</body></html>"""
    output = prepared / "comparison_dashboard.html"
    output.write_text(html, encoding="utf-8")
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the confirmatory experiment dashboard.")
    parser.add_argument("--prepared", type=Path, default=HERE / "prepared")
    parser.add_argument("--open", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = render(args.prepared.resolve())
    print(output)
    if args.open:
        webbrowser.open(output.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
