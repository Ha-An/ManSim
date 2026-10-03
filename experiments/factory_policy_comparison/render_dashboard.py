from __future__ import annotations

import argparse
import html
import json
import math
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
    PAIRED_COMPARISON_CSV,
    RUN_SUMMARY_CSV,
    STATUS_CSV,
    ExperimentConfig,
    load_experiment_config,
    read_csv,
    read_json,
    relative_link,
)
from experiments.factory_policy_comparison.theoretical_capacity import (
    CAPACITY_REPORT_JSON,
    calculate_theoretical_capacity,
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
    return _float_or_none(row, key) or 0.0


def _float_or_none(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
        return value if math.isfinite(value) else None
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


def _format_chart_tick(value: float) -> str:
    magnitude = abs(value)
    if magnitude == 0.0:
        return "0"
    if magnitude >= 100.0:
        return f"{value:,.0f}"
    if magnitude >= 10.0:
        return f"{value:.1f}".rstrip("0").rstrip(".")
    if magnitude >= 1.0:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if magnitude >= 0.001:
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{value:.2e}"


def _runtime_summary_rows(
    status_rows: list[dict[str, str]],
    *,
    experiment_wall_sec: float | None = None,
    parallel_jobs: int = 1,
) -> tuple[str, list[dict[str, str]]]:
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
    successful_elapsed = sum(
        float(_float_or_none(row, "elapsed_sec") or 0.0)
        for row in rows_with_elapsed
        if row.get("status") in {"completed", "skipped_existing"}
    )
    other_elapsed = total_elapsed - successful_elapsed
    wall_sec = experiment_wall_sec if experiment_wall_sec is not None else total_elapsed
    rows = [
        {"metric": "Experiment wall time", "value": _format_duration(wall_sec)},
        {"metric": "Configured parallel jobs", "value": str(max(1, int(parallel_jobs)))},
        {"metric": "Sum of run command times", "value": _format_duration(total_elapsed)},
        {"metric": "Successful-run command time", "value": _format_duration(successful_elapsed)},
        {"metric": "Failed/other command time", "value": _format_duration(other_elapsed)},
        {"metric": "Average run command time", "value": _format_duration(average_elapsed)},
        {"metric": "Slowest run", "value": slowest_label},
        {"metric": "Runs with elapsed_sec", "value": str(len(rows_with_elapsed))},
    ]
    if wall_sec > 0:
        rows.insert(
            3,
            {
                "metric": "Aggregate compute / wall ratio",
                "value": f"{total_elapsed / wall_sec:.2f}x",
            },
        )
    return _format_duration(wall_sec), rows


def _best_run_value(
    rows: list[dict[str, str]],
    *,
    objective_mode: str,
    worker_count: int,
    metric: str,
    higher_is_better: bool,
) -> float | None:
    values = [
        value
        for row in rows
        if str(row.get("objective_mode", "")) == objective_mode
        and int(_float_or_none(row, "worker_count") or -1) == int(worker_count)
        and str(row.get("comparison_eligible", "")).lower() == "true"
        and (value := _float_or_none(row, metric)) is not None
    ]
    if not values:
        return None
    return max(values) if higher_is_better else min(values)


def _capacity_dashboard_rows(
    capacity_report: dict[str, object],
    run_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    throughput_rows: list[dict[str, str]] = []
    makespan_rows: list[dict[str, str]] = []
    raw_rows = capacity_report.get("rows", [])
    for raw in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(raw, dict):
            continue
        worker_count = int(raw.get("worker_count", 0) or 0)
        upper_bound = float(raw.get("theoretical_max_products", 0.0) or 0.0)
        expected_products = float(raw.get("realistic_expected_products", 0.0) or 0.0)
        best_products = _best_run_value(
            run_rows,
            objective_mode="maximize_throughput",
            worker_count=worker_count,
            metric="total_products",
            higher_is_better=True,
        )
        bound_values = {
            "time": int(raw.get("time_capacity_bound", 0) or 0),
            "material": int(raw.get("material_capacity_bound", 0) or 0),
            "worker": int(raw.get("worker_capacity_bound", 0) or 0),
        }
        minimum_bound = min(bound_values.values()) if bound_values else 0
        binding = ", ".join(name for name, value in bound_values.items() if value == minimum_bound)
        throughput_rows.append(
            {
                "worker_count": str(worker_count),
                "horizon_min": f"{float(raw.get('horizon_min', 0.0) or 0.0):.1f}",
                "theoretical_max_products": str(int(upper_bound)),
                "ideal_capacity_reference_products": str(int(upper_bound)),
                "material_only_upper_bound": str(bound_values["material"]),
                "upper_bound_products_per_hour": f"{float(raw.get('theoretical_max_throughput_per_sim_hour', 0.0) or 0.0):.4f}",
                "realistic_expected_products": f"{expected_products:.2f}",
                "expected_products_per_hour": f"{float(raw.get('realistic_expected_throughput_per_sim_hour', 0.0) or 0.0):.4f}",
                "expected_attempts_before_quality": f"{float(raw.get('expected_flow_attempts_before_quality', 0.0) or 0.0):.2f}",
                "expected_operational_cycle_min": f"{float(raw.get('expected_operational_cycle_min', 0.0) or 0.0):.2f}",
                "machine_availability_pct": f"{100.0 * float(raw.get('machine_availability', 0.0) or 0.0):.2f}%",
                "effective_processing_mttf_min": f"{float(raw.get('effective_processing_mttf_min', 0.0) or 0.0):.2f}",
                "pm_due_processing_min": f"{float(raw.get('pm_due_processing_min', 0.0) or 0.0):.2f}",
                "pm_hazard_multiplier": f"{float(raw.get('pm_hazard_multiplier', 0.0) or 0.0):.3f}",
                "battery_duty_pct": f"{100.0 * float(raw.get('battery_duty_fraction', 0.0) or 0.0):.2f}%",
                "quality_yield_pct": f"{100.0 * float(raw.get('quality_yield', 0.0) or 0.0):.2f}%",
                "best_observed_products": "-" if best_products is None else f"{best_products:g}",
                "best_capacity_utilization_pct": (
                    "-" if best_products is None or upper_bound <= 0.0 else f"{100.0 * best_products / upper_bound:.2f}%"
                ),
                "best_vs_ideal_reference_pct": (
                    "-" if best_products is None or upper_bound <= 0.0 else f"{100.0 * best_products / upper_bound:.2f}%"
                ),
                "best_vs_expected_pct": (
                    "-"
                    if best_products is None or expected_products <= 0.0
                    else f"{100.0 * best_products / expected_products:.2f}%"
                ),
                "binding_bound": binding,
                "first_product_min": f"{float(raw.get('first_product_min', 0.0) or 0.0):.2f}",
                "bottleneck_cycle_min": f"{float(raw.get('bottleneck_cycle_min', 0.0) or 0.0):.2f}",
            }
        )

        theoretical_makespan = float(raw.get("theoretical_min_makespan_min", 0.0) or 0.0)
        best_makespan = _best_run_value(
            run_rows,
            objective_mode="minimize_makespan",
            worker_count=worker_count,
            metric="makespan_min",
            higher_is_better=False,
        )
        makespan_rows.append(
            {
                "worker_count": str(worker_count),
                "initial_batch_products": str(int(raw.get("initial_batch_product_count", 0) or 0)),
                "theoretical_min_makespan_min": f"{theoretical_makespan:.2f}",
                "ideal_makespan_reference_min": f"{theoretical_makespan:.2f}",
                "best_observed_makespan_min": "-" if best_makespan is None else f"{best_makespan:.3f}",
                "best_optimality_gap_pct": (
                    "-"
                    if best_makespan is None or theoretical_makespan <= 0.0
                    else f"{100.0 * (best_makespan / theoretical_makespan - 1.0):.2f}%"
                ),
                "best_vs_reference_difference_pct": (
                    "-" if best_makespan is None or theoretical_makespan <= 0.0
                    else f"{100.0 * (best_makespan / theoretical_makespan - 1.0):.2f}%"
                ),
                "bottleneck_cycle_min": f"{float(raw.get('bottleneck_cycle_min', 0.0) or 0.0):.2f}",
            }
        )
    return throughput_rows, makespan_rows


def _line_chart(
    rows: list[dict[str, str]],
    metric: str,
    title: str,
    *,
    y_label: str = "지표값",
    description: str = "",
) -> str:
    points_by_mode: dict[str, list[tuple[int, float, float | None, int | None]]] = {}
    stat_prefix = metric.removesuffix(".mean")
    marginal = metric.endswith((".marginal_gain", ".marginal_reduction"))

    def bounds(value: float, std: float | None) -> tuple[float, float]:
        low, high = value - (std or 0.0), value + (std or 0.0)
        if metric.endswith(".mean"):
            low = max(0.0, low)
            if "ratio" in stat_prefix:
                high = min(1.0, high)
        return low, high

    for row in rows:
        mode = str(row.get("mode", ""))
        worker_count = _float_or_none(row, "worker_count")
        value = _float_or_none(row, metric)
        if not mode or worker_count is None or value is None or not math.isfinite(value):
            continue
        count = _float_or_none(row, f"{stat_prefix}.count")
        if count is None and not marginal:
            count = _float_or_none(row, "comparison_run_count")
        sample_count = int(count) if count is not None and math.isfinite(count) else None
        std = _float_or_none(row, f"{stat_prefix}.std")
        if std is not None and (
            not math.isfinite(std) or std < 0.0 or (sample_count is not None and sample_count < 2)
        ):
            std = None
        points_by_mode.setdefault(mode, []).append((int(worker_count), value, std, sample_count))
    for points in points_by_mode.values():
        points.sort(key=lambda item: item[0])
    all_points = [point for points in points_by_mode.values() for point in points]
    if not all_points:
        return (
            f"<section class='panel'><h2>{_esc(title)}</h2><div class='empty'>표시할 데이터가 없습니다.</div>"
            f"<p class='chart-note'>{_esc(description)}</p></section>"
        )

    width, height = 840, 330
    left, right, top, bottom = 78, 270, 28, 62
    xs = sorted({point[0] for point in all_points})
    y_values = [limit for _x, value, std, _count in all_points for limit in bounds(value, std)]
    y_min = min(0.0, min(y_values))
    y_max = max(0.0, max(y_values))
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
        grid.append(
            f"<text x='{left-8}' y='{y+4:.1f}' class='axis-label' "
            f"text-anchor='end'>{_format_chart_tick(value)}</text>"
        )
    for worker_count in xs:
        x = sx(worker_count)
        grid.append(f"<text x='{x:.1f}' y='{height-22}' class='axis-label' text-anchor='middle'>{worker_count}</text>")

    bands = []
    series = []
    legend = []
    legend_x = width - right + 18
    for index, (mode, points) in enumerate(sorted(points_by_mode.items())):
        color = CHART_COLORS[index % len(CHART_COLORS)]
        # Split ribbons at missing SD values rather than interpolating uncertainty.
        segment: list[tuple[int, float, float]] = []
        segments: list[list[tuple[int, float, float]]] = []
        for x, y, std, _count in points:
            if std is None:
                if segment:
                    segments.append(segment)
                    segment = []
            else:
                low, high = bounds(y, std)
                segment.append((x, low, high))
        if segment:
            segments.append(segment)
        for segment in segments:
            if len(segment) < 2:
                continue
            upper = [f"{sx(x):.1f},{sy(high):.1f}" for x, _low, high in segment]
            lower = [f"{sx(x):.1f},{sy(low):.1f}" for x, low, _high in reversed(segment)]
            bands.append(
                f"<polygon class='sd-band' data-mode='{_esc(mode)}' "
                f"points='{' '.join(upper + lower)}' fill='{color}' fill-opacity='0.13' "
                "pointer-events='none' />"
            )
        coords = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y, _std, _count in points)
        series.append(f"<polyline points='{coords}' fill='none' stroke='{color}' stroke-width='2.5' />")
        for x, y, std, count in points:
            tooltip = f"{mode} | workers={x} | 평균={y:.6g}"
            if std is not None:
                low, high = bounds(y, std)
                px, lower_y, upper_y = sx(x), sy(low), sy(high)
                series.append(
                    f"<path class='sd-error-bar' stroke='{color}' stroke-width='1.2' opacity='0.7' "
                    f"d='M {px:.1f} {upper_y:.1f} V {lower_y:.1f} "
                    f"M {px-4:.1f} {upper_y:.1f} H {px+4:.1f} "
                    f"M {px-4:.1f} {lower_y:.1f} H {px+4:.1f}' />"
                )
                tooltip += f" | SD={std:.6g} | 분산={std * std:.6g}"
            else:
                tooltip += " | SD=추정 불가"
            tooltip += f" | n={count if count is not None else '미기록'}"
            series.append(
                f"<circle class='mean-point' cx='{sx(x):.1f}' cy='{sy(y):.1f}' r='4' "
                f"fill='{color}' tabindex='0' aria-label='{_esc(tooltip)}' "
                f"data-mode='{_esc(mode)}' data-workers='{x}' "
                f"data-mean='{y:.12g}' data-sd='{std if std is not None else ''}' "
                f"data-n='{count if count is not None else ''}'>"
                f"<title>{_esc(tooltip)}</title></circle>"
            )
        legend_y = 20 + index * 18
        legend.append(f"<rect x='{legend_x}' y='{legend_y-9}' width='10' height='10' fill='{color}' />")
        legend.append(f"<text x='{legend_x+15}' y='{legend_y}' class='legend'>{_esc(mode)}</text>")

    zero_line = ""
    if y_min < 0.0 < y_max:
        zero_y = sy(0.0)
        zero_line = (
            f"<line x1='{left}' y1='{zero_y:.1f}' x2='{width-right}' "
            f"y2='{zero_y:.1f}' class='zero-line' />"
        )

    svg = (
        f"<svg viewBox='0 0 {width} {height}' role='img' aria-label='{_esc(title)}' "
        f"data-metric='{_esc(metric)}' data-spread='sample-standard-deviation' "
        f"data-y-min='{y_min:.12g}' data-y-max='{y_max:.12g}' "
        f"data-plot-right='{width-right}' data-legend-left='{legend_x}'>"
        f"<text x='{left}' y='18' class='chart-title'>{_esc(title)}</text>"
        + "".join(grid)
        + zero_line
        + f"<line x1='{left}' y1='{height-bottom}' x2='{width-right}' y2='{height-bottom}' class='axis' />"
        + f"<line x1='{left}' y1='{top}' x2='{left}' y2='{height-bottom}' class='axis' />"
        + f"<text x='{(left + width - right) / 2:.1f}' y='{height-8}' class='axis-label' text-anchor='middle'>휴머노이드 수</text>"
        + f"<text x='16' y='{(top + height - bottom) / 2:.1f}' class='axis-label' text-anchor='middle' transform='rotate(-90 16 {(top + height - bottom) / 2:.1f})'>{_esc(y_label)}</text>"
        + "".join(bands)
        + "".join(series)
        + "".join(legend)
        + "</svg>"
    )
    spread_note = (
        "실선·점은 평균, 음영·오차막대는 seed 간 평균 ±1 표본 표준편차(SD)입니다. "
        "결과의 변동성을 나타내며 95% 신뢰구간이 아닙니다."
    )
    if marginal:
        spread_note += " 동일 seed의 인원 증가 전후 차이를 worker 1명당으로 환산해 집계합니다."
    elif metric.endswith(".mean"):
        spread_note += " 음영은 비음수 지표에서 0 이상, 비율에서는 0~1 범위에 표시합니다."
    if any(std is None for _x, _y, std, _count in all_points):
        spread_note += " 표본이 1개이거나 SD가 없는 지점은 변동 구간을 표시하지 않습니다."
    return (
        f"<section class='panel chart-panel'>{svg}"
        f"<p class='chart-note'><strong>해석:</strong> {_esc(description)}</p>"
        f"<p class='chart-note'>{_esc(spread_note)}</p></section>"
    )


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


def _worker_ranking_table(
    rows: list[dict[str, str]],
    metric: str,
    *,
    higher_is_better: bool,
) -> str:
    ranked_rows: list[dict[str, object]] = []
    worker_counts = sorted(
        {int(float(row.get("worker_count", 0) or 0)) for row in rows if row.get("worker_count")}
    )
    for worker_count in worker_counts:
        group = [
            row
            for row in rows
            if int(float(row.get("worker_count", 0) or 0)) == worker_count
            and _float_or_none(row, metric) is not None
        ]
        group.sort(key=lambda row: float(_float_or_none(row, metric) or 0.0), reverse=higher_is_better)
        previous_value: float | None = None
        competition_rank = 0
        for position, row in enumerate(group, 1):
            value = float(_float_or_none(row, metric) or 0.0)
            if previous_value is None or not math.isclose(value, previous_value, rel_tol=1e-12, abs_tol=1e-12):
                competition_rank = position
            ranked_rows.append(
                {
                    "worker_count": worker_count,
                    "rank": competition_rank,
                    "mode": row.get("mode", ""),
                    metric: row.get(metric, ""),
                }
            )
            previous_value = value
    return _table(ranked_rows, ["worker_count", "rank", "mode", metric])


def _policy_headline_summary(
    mode_rows: list[dict[str, str]],
    *,
    show_objective: bool,
) -> tuple[list[dict[str, str]], list[str]]:
    rows: list[dict[str, str]] = []
    ordered = sorted(
        mode_rows,
        key=lambda row: (
            str(row.get("objective_mode", "")),
            -float(_float_or_none(row, "total_products.mean") or 0.0),
            str(row.get("mode", "")),
        ),
    )
    for row in ordered:
        product_mean = _float_or_none(row, "total_products.mean")
        product_std = _float_or_none(row, "total_products.std")
        execution_ratio = _float_or_none(row, "humanoid_execution_ratio_avg.mean")
        summary_row = {
            "정책": str(row.get("mode", "")),
            "평균 제품 수": "-" if product_mean is None else f"{product_mean:.2f}",
            "제품 수 표준편차": "-" if product_std is None else f"{product_std:.2f}",
            "평균 실행비율": "-" if execution_ratio is None else f"{100.0 * execution_ratio:.2f}%",
        }
        if show_objective:
            summary_row = {
                "목적함수": str(row.get("objective_mode", "")),
                **summary_row,
            }
        rows.append(summary_row)
    columns = ["정책", "평균 제품 수", "제품 수 표준편차", "평균 실행비율"]
    if show_objective:
        columns.insert(0, "목적함수")
    return rows, columns


def _run_links(output_root: Path, run_rows: list[dict[str, str]]) -> str:
    rows = []
    for row in run_rows:
        run_dir = Path(row.get("run_dir", ""))
        links = []
        for name, label in [
            ("results_dashboard.html", "Hub"),
            ("kpi_dashboard.html", "KPI"),
            ("gantt.html", "Run Gantt"),
            ("dashboard_manifest.json", "Manifest"),
        ]:
            target = run_dir / name
            if target.exists():
                links.append(f"<a href='{_esc(relative_link(output_root / DASHBOARD_HTML, target))}'>{label}</a>")
        rows.append(
            (
                row.get("objective_mode", "scenario_default"),
                row.get("mode", ""),
                row.get("worker_count", ""),
                row.get("seed", ""),
                row.get("status", ""),
                row.get("comparison_eligible", ""),
                row.get("artifact_audit_status", ""),
                row.get("kpi_audit_status", ""),
                row.get("fairness_pass", ""),
                " | ".join(links),
            )
        )
    body = "".join(
        f"<tr><td>{_esc(objective)}</td><td>{_esc(mode)}</td><td>{_esc(worker_count)}</td><td>{_esc(seed)}</td>"
        f"<td>{_esc(status)}</td><td>{_esc(eligible)}</td><td>{_esc(artifact_audit)}</td>"
        f"<td>{_esc(kpi_audit)}</td><td>{_esc(fairness)}</td><td>{links}</td></tr>"
        for objective, mode, worker_count, seed, status, eligible, artifact_audit, kpi_audit, fairness, links in rows
    )
    if not body:
        body = "<tr><td colspan='10'>No rows</td></tr>"
    return (
        "<table><thead><tr><th>objective</th><th>mode</th><th>worker_count</th><th>seed</th>"
        "<th>status</th><th>comparison_eligible</th><th>artifact_audit</th><th>kpi_audit</th>"
        "<th>fairness</th><th>links</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def _merge_run_status_rows(
    status_rows: list[dict[str, str]],
    run_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Keep failed/missing runs visible while enriching completed rows with audit state."""

    if not status_rows:
        return run_rows
    summary_by_dir = {
        str(Path(row.get("run_dir", "")).resolve()): row
        for row in run_rows
        if row.get("run_dir")
    }
    def identity(row: dict[str, str]) -> tuple[str, str, str, str]:
        return (
            str(row.get("objective_mode", "scenario_default")),
            str(row.get("mode", "")),
            str(row.get("worker_count", "")),
            str(row.get("seed", "")),
        )

    summary_by_identity = {identity(row): row for row in run_rows}
    merged_rows: list[dict[str, str]] = []
    seen_dirs: set[str] = set()
    for status_row in status_rows:
        resolved_dir = str(Path(status_row.get("run_dir", "")).resolve())
        merged = dict(status_row)
        summary = summary_by_dir.get(resolved_dir) or summary_by_identity.get(identity(status_row), {})
        merged.update(summary)
        # The authoritative execution status comes from run_status.csv.
        merged["status"] = status_row.get("status", merged.get("status", ""))
        merged_rows.append(merged)
        if summary.get("run_dir"):
            seen_dirs.add(str(Path(summary["run_dir"]).resolve()))
        else:
            seen_dirs.add(resolved_dir)
    merged_rows.extend(
        row
        for row in run_rows
        if str(Path(row.get("run_dir", "")).resolve()) not in seen_dirs
    )
    return merged_rows


def render_dashboard(output_root: Path, cfg: ExperimentConfig) -> Path:
    output_root = output_root.resolve()
    run_rows = read_csv(output_root / RUN_SUMMARY_CSV)
    mode_rows = read_csv(output_root / MODE_SUMMARY_CSV)
    mode_worker_rows = read_csv(output_root / MODE_WORKER_SUMMARY_CSV)
    paired_rows = read_csv(output_root / PAIRED_COMPARISON_CSV)
    fairness_rows = read_csv(output_root / FAIRNESS_CSV)
    audit_rows = read_csv(output_root / AUDIT_CSV)
    status_rows = read_csv(output_root / STATUS_CSV)
    experiment_plan = read_json(output_root / "experiment_plan.json")
    validity = read_json(output_root / "result_validity.json")
    validity_banner = ""
    if validity.get("status") == "requires_rerun":
        validity_banner = (
            '<section role="alert" style="border:2px solid #c74343;padding:16px;margin:16px 0">'
            '<h2>재실험 필요: 시뮬레이션 동작 오류 발견</h2><p>'
            + _esc(str(validity.get("message", ""))) + '</p></section>'
        )
    experiment_timing = read_json(output_root / "experiment_timing.json")
    plan_scenario = str(experiment_plan.get("scenario", cfg.scenario) or cfg.scenario)
    experiment_wall_sec = _float_or_none(
        {"value": experiment_timing.get("experiment_wall_sec_through_summary", "")},
        "value",
    )
    if experiment_wall_sec is None:
        try:
            experiment_wall_sec = max(
                0.0,
                (output_root / STATUS_CSV).stat().st_mtime - output_root.stat().st_ctime,
            )
        except OSError:
            experiment_wall_sec = None
    parallel_jobs = int(
        experiment_timing.get(
            "parallel_jobs", experiment_plan.get("parallel_jobs", 1)
        )
        or 1
    )
    total_runtime_label, runtime_rows = _runtime_summary_rows(
        status_rows,
        experiment_wall_sec=experiment_wall_sec,
        parallel_jobs=parallel_jobs,
    )
    display_run_rows = _merge_run_status_rows(status_rows, run_rows)
    display_run_rows.sort(
        key=lambda row: (
            str(row.get("objective_mode", "")),
            str(row.get("mode", "")),
            int(_float_or_none(row, "worker_count") or 0),
            int(_float_or_none(row, "seed") or 0),
        )
    )
    successful_statuses = {"completed", "skipped_existing"}
    completed_count = sum(
        1 for row in display_run_rows if row.get("status") in successful_statuses
    )
    rolling_window_values: set[float] = set()
    for row in display_run_rows:
        raw_run_dir = str(row.get("run_dir", "")).strip()
        if not raw_run_dir:
            continue
        kpi = read_json(Path(raw_run_dir) / "kpi.json")
        rolling = kpi.get("rolling_horizon", {}) if isinstance(kpi.get("rolling_horizon", {}), dict) else {}
        if not bool(rolling.get("enabled", False)):
            continue
        try:
            window_min = float(rolling.get("window_min"))
        except (TypeError, ValueError):
            continue
        if window_min > 0:
            rolling_window_values.add(window_min)
    comparison_count = sum(
        1 for row in run_rows if str(row.get("comparison_eligible", "")).lower() == "true"
    )
    no_wait_modes = {"random_feasible_dispatch", "simulation_based_adp"}
    voluntary_wait_violation_count = sum(
        1
        for row in run_rows
        if str(row.get("mode", "")) in no_wait_modes
        and float(row.get("adp_candidate_available_wait_count", 0) or 0) > 0
    )
    adp_entropy_weighted_sum = 0.0
    adp_entropy_decision_count = 0.0
    for row in run_rows:
        if str(row.get("mode", "")) != "simulation_based_adp":
            continue
        entropy = _float_or_none(row, "adp_beam_value_entropy_avg")
        decision_count = _float_or_none(row, "adp_beam_value_entropy_decision_count")
        if entropy is None or decision_count is None or decision_count <= 0:
            continue
        adp_entropy_weighted_sum += entropy * decision_count
        adp_entropy_decision_count += decision_count
    adp_entropy_label = (
        f"{adp_entropy_weighted_sum / adp_entropy_decision_count:.3f}"
        if adp_entropy_decision_count > 0
        else "n/a"
    )
    excluded_count = max(0, completed_count - comparison_count)
    def validation_key(row: dict[str, str]) -> tuple[str, ...]:
        run_dir = str(row.get("run_dir", "")).strip()
        if run_dir:
            return (str(Path(run_dir).resolve()),)
        return (
            str(row.get("objective_mode", "scenario_default")),
            str(row.get("mode", "")),
            str(row.get("worker_count", "")),
            str(row.get("seed", "")),
        )

    validation_issue_keys = {
        validation_key(row)
        for row in audit_rows
        if row.get("artifact_audit_status") != "pass" or row.get("kpi_audit_status") != "pass"
    }
    validation_issue_keys.update(
        validation_key(row)
        for row in fairness_rows
        if str(row.get("fairness_pass", "")).lower() != "true"
    )
    fairness_cols = [
        "objective_mode",
        "mode",
        "worker_count",
        "seed",
        "scenario_ok",
        "worker_count_ok",
        "horizon_ok",
        "objective_contract_ok",
        "quality_stream_prefix_matches",
        "repair_stream_prefix_matches",
        "seed_ok",
        "timing_profile_fingerprint",
        "timing_profile_matches_reference",
        "pre_run_matches_reference",
        "artifact_errors_empty",
        "fairness_pass",
    ]
    def stat_cols(metric: str) -> list[str]:
        return [f"{metric}.{stat}" for stat in ("mean", "std", "min", "max")]

    throughput_summary_cols = [
        "objective_mode",
        "mode",
        "completed_run_count",
        "comparison_run_count",
        *stat_cols("total_products"),
        *stat_cols("throughput_per_sim_hour"),
        *stat_cols("completed_product_lead_time_avg_min"),
        *stat_cols("humanoid_incident_total"),
        *stat_cols("humanoid_blocked_ratio_avg"),
        *stat_cols("machine_failure_count"),
        *stat_cols("preventive_maintenance_count"),
        *stat_cols("battery_risk_assignment_count"),
        *stat_cols("worker_depleted_during_task_count"),
        *stat_cols("worker_returned_next_day_count"),
        *stat_cols("agent_discharged_time_min_total"),
        *stat_cols("otc"),
    ]
    makespan_summary_cols = [
        "objective_mode",
        "mode",
        "completed_run_count",
        "comparison_run_count",
        *stat_cols("makespan_min"),
        *stat_cols("initial_batch_yield_ratio"),
        *stat_cols("humanoid_incident_total"),
        *stat_cols("humanoid_blocked_ratio_avg"),
        *stat_cols("machine_failure_count"),
        *stat_cols("preventive_maintenance_count"),
        *stat_cols("battery_risk_assignment_count"),
        *stat_cols("worker_depleted_during_task_count"),
        *stat_cols("worker_returned_next_day_count"),
        *stat_cols("agent_discharged_time_min_total"),
        *stat_cols("otc"),
    ]
    mode_worker_cols = [
        "objective_mode",
        "mode",
        "worker_count",
        "completed_run_count",
        "comparison_run_count",
        *stat_cols("total_products"),
        *stat_cols("throughput_per_sim_hour"),
        "throughput_per_sim_hour.marginal_gain",
        *stat_cols("completed_product_lead_time_avg_min"),
        *stat_cols("humanoid_incident_total"),
        *stat_cols("humanoid_blocked_ratio_avg"),
        *stat_cols("machine_failure_count"),
        *stat_cols("preventive_maintenance_count"),
        *stat_cols("battery_risk_assignment_count"),
        *stat_cols("worker_depleted_during_task_count"),
        *stat_cols("worker_returned_next_day_count"),
        *stat_cols("agent_discharged_time_min_total"),
        *stat_cols("otc"),
    ]
    makespan_worker_cols = [
        "objective_mode",
        "mode",
        "worker_count",
        "completed_run_count",
        "comparison_run_count",
        *stat_cols("makespan_min"),
        "makespan_min.marginal_reduction",
        *stat_cols("initial_batch_progress_ratio"),
        *stat_cols("initial_batch_yield_ratio"),
        *stat_cols("humanoid_incident_total"),
        *stat_cols("humanoid_blocked_ratio_avg"),
        *stat_cols("machine_failure_count"),
        *stat_cols("preventive_maintenance_count"),
        *stat_cols("battery_risk_assignment_count"),
        *stat_cols("worker_depleted_during_task_count"),
        *stat_cols("worker_returned_next_day_count"),
        *stat_cols("agent_discharged_time_min_total"),
        *stat_cols("otc"),
    ]
    throughput_rows = [
        row
        for row in mode_worker_rows
        if row.get("objective_mode") in {"maximize_throughput", "scenario_default", ""}
    ]
    makespan_rows = [row for row in mode_worker_rows if row.get("objective_mode") == "minimize_makespan"]
    throughput_mode_rows = [
        row
        for row in mode_rows
        if row.get("objective_mode") in {"maximize_throughput", "scenario_default", ""}
    ]
    makespan_mode_rows = [row for row in mode_rows if row.get("objective_mode") == "minimize_makespan"]
    raw_plan_modes = experiment_plan.get("modes") if isinstance(experiment_plan.get("modes"), list) else cfg.modes
    raw_plan_workers = (
        experiment_plan.get("worker_counts")
        if isinstance(experiment_plan.get("worker_counts"), list)
        else cfg.worker_counts
    )
    raw_plan_seeds = experiment_plan.get("seeds") if isinstance(experiment_plan.get("seeds"), list) else cfg.seeds
    raw_plan_objectives = (
        experiment_plan.get("objective_modes")
        if isinstance(experiment_plan.get("objective_modes"), list)
        else cfg.objective_modes
    )
    policy_contract = (
        experiment_plan.get("policy_contract", {})
        if isinstance(experiment_plan.get("policy_contract", {}), dict)
        else {}
    )
    wait_contract_label = (
        "Disabled"
        if policy_contract.get("explicit_wait_action_enabled") is False
        else "Not declared"
    )
    beam_order_label = str(
        policy_contract.get("beam_worker_order_strategy", "Not declared")
    )
    # Older experiment plans stored one mode entry per run. Normalize every
    # dimension so overview counts and fallback expected-run counts stay true.
    plan_modes = list(dict.fromkeys(str(mode) for mode in raw_plan_modes))
    plan_workers = list(dict.fromkeys(int(count) for count in raw_plan_workers))
    plan_seeds = list(dict.fromkeys(int(seed) for seed in raw_plan_seeds))
    plan_objectives = list(dict.fromkeys(str(mode) for mode in raw_plan_objectives))
    expected_runs = int(
        experiment_plan.get(
            "run_count",
            len(plan_objectives) * len(plan_modes) * len(plan_workers) * len(plan_seeds),
        )
        or 0
    )
    incomplete_count = max(0, expected_runs - completed_count)
    worker_counts = sorted({str(count) for count in plan_workers}, key=lambda item: int(item))
    horizon_days = int(experiment_plan.get("horizon_days", cfg.horizon_days) or cfg.horizon_days)
    minutes_per_day = float(
        experiment_plan.get("minutes_per_day", cfg.minutes_per_day) or cfg.minutes_per_day
    )
    max_sim_days = int(
        experiment_plan.get("makespan_max_sim_days", cfg.makespan_max_sim_days)
        or cfg.makespan_max_sim_days
    )
    if len(plan_seeds) == 1:
        seed_summary = f"seed: {plan_seeds[0]}"
        seed_notice = (
            f"This experiment uses one deterministic seed ({plan_seeds[0]}). Values are descriptive "
            "single-run comparisons, not estimates of statistical uncertainty."
        )
    else:
        seed_summary = f"seeds: {plan_seeds}"
        seed_notice = (
            f"This experiment uses {len(plan_seeds)} seeds. Summary means and sample standard deviations "
            "describe the repeated runs."
        )
    if "minimize_makespan" in plan_objectives:
        duration_summary = (
            f"Throughput horizon: {horizon_days} days | Makespan limit: {max_sim_days} days"
        )
    else:
        duration_summary = f"horizon: {horizon_days} days"
    if len(rolling_window_values) == 1:
        rolling_window_summary = f"Rolling window: {next(iter(rolling_window_values)):g} min"
    elif rolling_window_values:
        rolling_window_summary = "Rolling windows: " + ", ".join(
            f"{value:g}" for value in sorted(rolling_window_values)
        ) + " min"
    else:
        rolling_window_summary = ""
    if len(plan_seeds) == 1:
        statistics_note = (
            "With one seed, standard deviation is left blank because it is not estimable."
        )
    else:
        statistics_note = (
            f"Across {len(plan_seeds)} seeds, standard deviation is the sample standard deviation."
        )
    capacity_report = read_json(output_root / CAPACITY_REPORT_JSON)
    if capacity_report.get("source") != "archived_run_config":
        capacity_report = calculate_theoretical_capacity(
            scenario=plan_scenario,
            worker_counts=plan_workers,
            horizon_days=horizon_days,
            minutes_per_day=minutes_per_day,
        )
    (output_root / CAPACITY_REPORT_JSON).write_text(
        json.dumps(capacity_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    capacity_available = bool(capacity_report.get("available", False))
    capacity_throughput_rows, capacity_makespan_rows = _capacity_dashboard_rows(
        capacity_report,
        run_rows,
    )
    upper_bound_values = sorted(
        {
            int(row.get("theoretical_max_products", "0") or 0)
            for row in capacity_throughput_rows
        }
    )
    if not upper_bound_values:
        upper_bound_label = "N/A"
    elif len(upper_bound_values) == 1:
        upper_bound_label = f"{upper_bound_values[0]}개"
    else:
        upper_bound_label = f"{upper_bound_values[0]}-{upper_bound_values[-1]}개"
    expected_reference_values = sorted(
        {
            round(float(row.get("realistic_expected_products", "0") or 0.0), 2)
            for row in capacity_throughput_rows
        }
    )
    if not expected_reference_values:
        expected_reference_label = "N/A"
    elif len(expected_reference_values) == 1:
        expected_reference_label = f"{expected_reference_values[0]:.2f}개"
    else:
        expected_reference_label = (
            f"{expected_reference_values[0]:.2f}-{expected_reference_values[-1]:.2f}개"
        )
    capacity_assumptions = capacity_report.get("assumptions", [])
    expected_reference = capacity_report.get("expected_reference", {})
    expected_reference = expected_reference if isinstance(expected_reference, dict) else {}
    expected_included = expected_reference.get("included_losses", [])
    expected_excluded = expected_reference.get("excluded_losses", [])
    capacity_method_html = (
        f"""
        <section class="panel section">
          <h2>이상적 생산능력 근사치 산정</h2>
          <p><strong>산정 방식:</strong> {_esc(capacity_report.get('method', ''))}. 평균 설비 경로·정상상태 cycle·충전 주기를 가정한 근사치이며, 수학적으로 보장된 상한이나 최적해가 아닙니다. 이 값만으로 정책이 포화되었다고 판단할 수 없습니다.</p>
          <p><code>{_esc(capacity_report.get('formula', ''))}</code></p>
          <p>삼각분포 최솟값, 모든 필수 생산 태스크, 적재물별 배수를 적용한 정적 최단경로 이동거리, Station 1·Station 2·Inspection의 독점 자원 cycle, 전체 worker 작업량, 직접 충전 duty cycle, 최초 제품 도달시간과 material 공급량을 사용합니다.</p>
          <ul>{''.join(f'<li>{_esc(item)}</li>' for item in capacity_assumptions if str(item).strip())}</ul>
        </section>
        """
        if capacity_available
        else f"<section class='panel section'><h2>이상적 생산능력 근사치 산정</h2><p>{_esc(capacity_report.get('reason', '계산할 수 없습니다.'))}</p></section>"
    )
    expected_reference_html = (
        f"""
        <section class="panel section">
          <h2>운영 계획 생산량 기준 산정</h2>
          <p><strong>산정 방식:</strong> {_esc(expected_reference.get('method', ''))}. {_esc(expected_reference.get('interpretation', ''))}</p>
          <p><code>{_esc(expected_reference.get('formula', ''))}</code></p>
          <p>먼저 품질 판정 전 기대 공정 완료량을 계산하고 설정된 검사 양품률을 적용합니다. 후속 작업의 완전한 중첩을 가정하지 않는 보수적인 표준 작업 cycle을 사용하므로, 조정 능력이 좋은 정책은 후속 작업을 중첩하여 이 기준을 초과할 수 있습니다.</p>
          <p><strong>산정에 포함되는 항목:</strong></p>
          <ul>{''.join(f'<li>{_esc(item)}</li>' for item in expected_included if str(item).strip())}</ul>
          <p><strong>정책 실행 전에는 확정할 수 없어 제외하는 항목:</strong></p>
          <ul>{''.join(f'<li>{_esc(item)}</li>' for item in expected_excluded if str(item).strip())}</ul>
        </section>
        """
        if capacity_available and expected_reference
        else ""
    )
    throughput_content = f"""
      <section class="panel section">
        <h2>Worker 수별 생산능력 기준</h2>
        <p>이상적 생산능력과 운영 계획 생산량은 근사 기준이며, 최적해나 실제 생산량의 통계적 기댓값이 아닙니다. 기준 대비 비율은 최적성 격차가 아닙니다. 별도 자재 상한은 floor((초기 자재 + 보충 횟수 × 보충 목표)/2)이며 시간·교통 제약을 완화한 상한입니다. 교착과 정책별 작업 중첩 효과는 실제 결과에서 확인해야 합니다.</p>
        {_table(
            capacity_throughput_rows,
            [
                "worker_count", "horizon_min", "ideal_capacity_reference_products", "material_only_upper_bound",
                "realistic_expected_products", "expected_attempts_before_quality",
                "expected_operational_cycle_min", "machine_availability_pct",
                "effective_processing_mttf_min", "pm_due_processing_min",
                "pm_hazard_multiplier",
                "battery_duty_pct", "quality_yield_pct",
                "best_observed_products", "best_vs_expected_pct",
                "best_vs_ideal_reference_pct", "binding_bound",
                "first_product_min", "bottleneck_cycle_min",
            ],
        )}
      </section>
      <section class="chart-grid">
        {_line_chart(throughput_rows, "total_products.mean", "정책별 평균 완료 제품 수", y_label="평균 완료 제품 수", description="동일한 worker 수에서 값이 클수록 5일 동안 더 많은 양품을 완료한 정책입니다. 반복 seed의 평균을 표시합니다.")}
        {_line_chart(throughput_rows, "throughput_per_sim_hour.mean", "정책별 시간당 Throughput", y_label="완료 제품/시뮬레이션 시간", description="고정 horizon의 시간당 생산량입니다. 동일한 horizon에서는 평균 완료 제품 수와 같은 방향으로 해석합니다.")}
        {_line_chart(throughput_rows, "throughput_per_sim_hour.marginal_gain", "Worker 추가에 따른 한계 Throughput", y_label="Worker 1명당 Throughput 증가", description="직전 worker 수 대비 휴머노이드 1명을 추가했을 때 증가한 시간당 생산량입니다. 값이 작아지면 인력 확장의 한계효과가 감소한 것입니다.")}
        {_line_chart(throughput_rows, "humanoid_blocked_ratio_avg.mean", "평균 Worker Blocked 비율", y_label="Blocked 비율", description="worker가 자원·공간·선행조건 때문에 진행하지 못한 시간 비율입니다. 일반적으로 낮을수록 운영 흐름이 원활합니다.")}
        {_line_chart(throughput_rows, "humanoid_incident_total.mean", "평균 Humanoid Incident 수", y_label="Incident 수", description="반복 run에서 발생한 humanoid incident의 평균입니다. 생산량과 함께 보며 낮을수록 안정적인 정책입니다.")}
        {_line_chart(throughput_rows, "otc.mean", "평균 운영 태스크 복잡도(OTC)", y_label="OTC", description="하루 평균 발생한 primitive 가중 복잡도입니다. 정책의 성능 점수가 아니라 정책이 실제로 처리한 운영 부담의 크기입니다.")}
        {_line_chart(throughput_rows, "machine_failure_count.mean", "평균 설비 고장 수", y_label="고장 수", description="실제 설비 가공 누적시간이 샘플링된 고장 노출 임계치에 도달한 횟수입니다. 정책별 설비 가동 패턴 차이도 함께 반영됩니다.")}
        {_line_chart(throughput_rows, "preventive_maintenance_count.mean", "평균 예방정비 수", y_label="예방정비 수", description="가공 누적시간 기준 PM 도래 후 실제로 완료된 예방정비 횟수입니다. 생산량과 함께 보아 예방정비의 기회비용과 고장 억제 효과를 판단합니다.")}
        {_line_chart(throughput_rows, "battery_risk_assignment_count.mean", "평균 배터리 위험 태스크 선택 수", y_label="위험 배정 수", description="예상 태스크 수행시간과 충전 도크 복귀시간을 합하면 잔여 배터리가 부족한데도 정책이 선택한 태스크 수입니다.")}
        {_line_chart(throughput_rows, "worker_depleted_during_task_count.mean", "평균 작업 중 방전 수", y_label="작업 중 방전 수", description="배터리 위험 선택이 실제 작업 중 SOC 0으로 이어진 횟수입니다. 다음날 복귀와 별개로 당일 생산능력 손실을 나타냅니다.")}
        {_line_chart(throughput_rows, "worker_returned_next_day_count.mean", "평균 다음날 복귀 수", y_label="복귀 수", description="방전 후 다음날 시작 시 전용 충전 도크에서 완충 상태로 외부 복구된 worker 수입니다.")}
        {_line_chart(throughput_rows, "agent_discharged_time_min_total.mean", "평균 방전 가동불가 시간", y_label="Worker-min", description="방전 시점부터 다음날 복귀 또는 horizon 종료까지 손실된 worker 시간입니다. 위험 행동의 실제 운영 비용을 직접 보여줍니다.")}
      </section>
      <section class="panel section">
        <h2>Throughput Results by Policy and Worker Count</h2>
        <p>Mean, sample standard deviation, minimum, and maximum use only execution-complete runs that passed artifact, KPI, and fairness validation. {_esc(statistics_note)} Marginal gain is reported per additional worker.</p>
        {_table(throughput_rows, mode_worker_cols)}
      </section>
      <section class="panel section">
        <h2>Throughput Ranking within Each Worker Count</h2>
        {_worker_ranking_table(throughput_rows, "throughput_per_sim_hour.mean", higher_is_better=True)}
      </section>
      <section class="panel section">
        <h2>Seed-Paired ADP Comparisons</h2>
        <p>Differences are ADP minus baseline on identical seeds. Registered paper production intervals use the experiment's paired-bootstrap contract and match its CSV. Other intervals use 10,000 deterministic paired resamples. Overall paper intervals resample common seed blocks across fleet sizes; uncertainty from a single trained checkpoint is not estimated.</p>
        {_table(
            [row for row in paired_rows if row.get("objective_mode") == "maximize_throughput"],
            [
                "worker_count", "baseline_mode", "metric", "paired_seed_count", "mean_difference",
                "ci95_low", "ci95_high", "relative_improvement_pct", "win_count",
                "tie_count", "loss_count", "superiority_demonstrated",
            ],
        )}
      </section>
      <section class="panel section">
        <h2>Run-Weighted Throughput Summary by Policy</h2>
        <p>This run-weighted pooled summary combines all valid configured worker counts and seeds. Use the worker-count table above for fleet-size-controlled comparisons.</p>
        {_table(throughput_mode_rows, throughput_summary_cols)}
      </section>
    """
    makespan_content = f"""
      <section class="panel section">
        <h2>Ideal Batch Makespan Reference by Worker Count</h2>
        <p>This steady-state process-and-movement approximation is not a certified scheduling lower bound. Differences from the best valid run are not optimality gaps.</p>
        {_table(
            capacity_makespan_rows,
            [
                "worker_count", "initial_batch_products", "ideal_makespan_reference_min",
                "best_observed_makespan_min", "best_vs_reference_difference_pct", "bottleneck_cycle_min",
            ],
        )}
      </section>
      <section class="chart-grid">
        {_line_chart(makespan_rows, "makespan_min.mean", "정책별 평균 Makespan", y_label="Makespan(분)", description="초기 material batch가 모두 terminal 상태에 도달할 때까지의 평균 시간입니다. 낮을수록 좋습니다.")}
        {_line_chart(makespan_rows, "makespan_min.marginal_reduction", "Worker 추가에 따른 Makespan 단축", y_label="Worker 1명당 단축시간(분)", description="직전 worker 수보다 휴머노이드 1명을 추가했을 때 줄어든 makespan입니다. 양수이고 클수록 증원의 효과가 큽니다.")}
        {_line_chart(makespan_rows, "initial_batch_yield_ratio.mean", "초기 Batch 평균 양품률", y_label="양품률", description="초기 material batch에서 양품 product로 종결된 비율입니다. Scrap으로 최종 종결된 material은 분모에 포함됩니다.")}
        {_line_chart(makespan_rows, "humanoid_blocked_ratio_avg.mean", "평균 Worker Blocked 비율", y_label="Blocked 비율", description="worker가 자원·공간·선행조건 때문에 진행하지 못한 시간 비율입니다. 일반적으로 낮을수록 좋습니다.")}
        {_line_chart(makespan_rows, "humanoid_incident_total.mean", "평균 Humanoid Incident 수", y_label="Incident 수", description="반복 run에서 발생한 humanoid incident 평균입니다. Makespan과 함께 안정성을 판단합니다.")}
        {_line_chart(makespan_rows, "otc.mean", "평균 운영 태스크 복잡도(OTC)", y_label="OTC", description="하루 평균 처리한 primitive 가중 복잡도입니다. Makespan과 별개의 운영 부담 지표입니다.")}
      </section>
      <section class="panel section">
        <h2>Makespan Results by Policy and Worker Count</h2>
        <p>Mean, sample standard deviation, minimum, and maximum use only execution-complete runs that passed artifact, KPI, and fairness validation. {_esc(statistics_note)} Marginal reduction is reported per additional worker.</p>
        {_table(makespan_rows, makespan_worker_cols)}
      </section>
      <section class="panel section">
        <h2>Makespan Ranking within Each Worker Count</h2>
        {_worker_ranking_table(makespan_rows, "makespan_min.mean", higher_is_better=False)}
      </section>
      <section class="panel section">
        <h2>Run-Weighted Makespan Summary by Policy</h2>
        <p>This run-weighted pooled summary combines all valid configured worker counts and seeds. Use the worker-count table above for fleet-size-controlled comparisons.</p>
        {_table(makespan_mode_rows, makespan_summary_cols)}
      </section>
    """
    objective_tabs: list[tuple[str, str, str]] = []
    if throughput_rows:
        objective_tabs.append(("throughput", "Maximize Throughput", throughput_content))
    if makespan_rows:
        objective_tabs.append(("makespan", "Minimize Makespan", makespan_content))
    tab_buttons = "".join(
        f'<button class="tab-button{" active" if index == 0 else ""}" type="button" data-tab="{tab_id}">{_esc(label)}</button>'
        for index, (tab_id, label, _content) in enumerate(objective_tabs)
    )
    tab_panels = "".join(
        f'<div id="{tab_id}" class="tab-panel{" active" if index == 0 else ""}">{content}</div>'
        for index, (tab_id, _label, content) in enumerate(objective_tabs)
    )
    primary_candidates = [
        row for row in paired_rows
        if str(row.get("primary_comparison", "")).lower() == "true"
    ]
    primary_row = next(
        (
            row for row in primary_candidates
            if str(row.get("worker_count", "")).lower() in {"overall", "2-6"}
        ),
        primary_candidates[0] if len(plan_workers) == 1 and primary_candidates else None,
    )
    primary_card = ""
    if primary_row is not None:
        demonstrated = str(primary_row.get("superiority_demonstrated", "")).lower() == "true"
        primary_card = (
            '<div class="card"><div class="label">ADP vs Immediate Shared</div>'
            f'<div class="value">{"Demonstrated" if demonstrated else "Not demonstrated"}</div>'
            f'<div>Delta {html.escape(str(primary_row.get("mean_difference", "")))}; '
            f'95% CI [{html.escape(str(primary_row.get("ci95_low", "")))}, '
            f'{html.escape(str(primary_row.get("ci95_high", "")))}]</div></div>'
        )
    policy_summary_rows, policy_summary_columns = _policy_headline_summary(
        mode_rows,
        show_objective=len(plan_objectives) > 1,
    )
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(plan_scenario)} Policy Comparison{_esc(' - ' + rolling_window_summary if rolling_window_summary else '')}</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ max-width: 100%; }}
    body {{ margin: 0; overflow-x: hidden; font-family: Segoe UI, Arial, sans-serif; background: #f4f7fb; color: #14213d; }}
    header, main {{ width: 100%; min-width: 0; max-width: 100vw; }}
    header {{ padding: 28px 36px; background: #0f1b2d; color: #f8fbff; }}
    main {{ padding: 28px 36px 48px; }}
    h1, h2 {{ margin: 0 0 14px; }}
    .sub {{ max-width: 100%; color: #b9c7da; overflow-wrap: anywhere; }}
    .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 16px; margin-bottom: 22px; }}
    .card, .panel {{ min-width: 0; background: #fff; border: 1px solid #d9e2ef; border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(15, 27, 45, 0.06); }}
    .panel {{ overflow-x: auto; }}
    .panel p, .panel li, .card .value {{ overflow-wrap: anywhere; }}
    .card .label {{ color: #59708f; font-size: 0.82rem; text-transform: uppercase; letter-spacing: .04em; }}
    .card .value {{ font-size: 1.55rem; font-weight: 700; margin-top: 8px; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 18px; margin-bottom: 22px; }}
    .chart-grid {{ display: grid; grid-template-columns: repeat(2, minmax(360px, 1fr)); gap: 18px; margin-bottom: 22px; }}
    .chart-panel {{ overflow: hidden; }}
    svg {{ width: 100%; height: auto; }}
    .axis {{ stroke: #6b7f99; stroke-width: 1; }}
    .gridline {{ stroke: #dbe5f2; stroke-width: 1; }}
    .zero-line {{ stroke: #8395ad; stroke-width: 1.5; stroke-dasharray: 4 3; }}
    .axis-label {{ fill: #5f728c; font-size: 12px; }}
    .legend {{ fill: #243b5a; font-size: 11px; }}
    .chart-title {{ fill: #14213d; font-weight: 700; font-size: 15px; }}
    .chart-note {{ color: #526985; line-height: 1.5; margin: 10px 4px 0; }}
    .empty {{ color: #71839b; padding: 28px 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.88rem; }}
    th, td {{ border-bottom: 1px solid #e4ebf5; padding: 8px 9px; text-align: left; vertical-align: top; }}
    th {{ color: #29466d; background: #eef4fb; }}
    a {{ color: #1d66c2; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .section {{ margin-bottom: 22px; }}
    .notice {{ padding: 12px 14px; border-left: 4px solid #d97706; background: #fff7ed; color: #7c3f00; margin-bottom: 22px; }}
    .tabs {{ display: flex; gap: 8px; margin: 0 0 18px; border-bottom: 1px solid #cbd8e8; }}
    .tab-button {{ border: 0; border-bottom: 3px solid transparent; background: transparent; color: #47617f; padding: 11px 16px; font: inherit; font-weight: 700; cursor: pointer; }}
    .tab-button.active {{ color: #174f91; border-bottom-color: #2563eb; }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    @media (max-width: 720px) {{
      header {{ padding: 24px 18px; }}
      main {{ padding: 20px 18px 36px; }}
      .sub {{ max-width: 100%; line-height: 1.5; }}
      .cards, .grid, .chart-grid {{ grid-template-columns: minmax(0, 1fr); }}
      .cards {{ gap: 12px; }}
      .card, .panel {{ padding: 16px; }}
      .tabs {{ overflow-x: auto; }}
      .tab-button {{ flex: 0 0 auto; padding-inline: 12px; }}
      .panel table {{ min-width: 560px; }}
    }}
  </style>
</head>
<body>
<header>
  <h1>{_esc(plan_scenario)} Policy Comparison</h1>
  {('<div class="sub">' + _esc(experiment_plan['condition_label']) + '</div>') if experiment_plan.get('condition_label') else ''}
  <div class="sub">{_esc(duration_summary)} | workers: {_esc(', '.join(worker_counts))} | {_esc(seed_summary)}{_esc(' | ' + rolling_window_summary if rolling_window_summary else '')}</div>
</header>
<main>
  {validity_banner}
  <section class="cards">
    <div class="card"><div class="label">Modes</div><div class="value">{len(plan_modes)}</div></div>
    <div class="card"><div class="label">Objectives</div><div class="value">{len(plan_objectives)}</div></div>
    <div class="card"><div class="label">Explicit WAIT</div><div class="value">{_esc(wait_contract_label)}</div></div>
    <div class="card"><div class="label">ADP Beam Order</div><div class="value">{_esc(beam_order_label)}</div></div>
    <div class="card"><div class="label">ADP Beam Value Entropy</div><div class="value">{_esc(adp_entropy_label)}</div><div>0은 가치 집중, 1은 후보 가치가 유사함을 뜻합니다.</div></div>
    <div class="card"><div class="label">Voluntary WAIT Violations</div><div class="value">{voluntary_wait_violation_count}</div></div>
    <div class="card"><div class="label">이상적 생산능력 근사치</div><div class="value">{_esc(upper_bound_label)}</div></div>
    <div class="card"><div class="label">운영 계획 양품 기준</div><div class="value">{_esc(expected_reference_label)}</div></div>
    <div class="card"><div class="label">Expected Runs</div><div class="value">{expected_runs}</div></div>
    <div class="card"><div class="label">Completed</div><div class="value">{completed_count}</div></div>
    <div class="card"><div class="label">{'Aggregation-Eligible Historical Runs' if validity_banner else 'Valid Comparison Runs'}</div><div class="value">{comparison_count}</div></div>
    <div class="card"><div class="label">Excluded Completed Runs</div><div class="value">{excluded_count}</div></div>
    <div class="card"><div class="label">Incomplete Runs</div><div class="value">{incomplete_count}</div></div>
    <div class="card"><div class="label">Experiment Wall Time</div><div class="value">{_esc(total_runtime_label)}</div></div>
    {f'<div class="card"><div class="label">Rolling Window</div><div class="value">{_esc(rolling_window_summary.removeprefix("Rolling window: ").removeprefix("Rolling windows: "))}</div></div>' if rolling_window_summary else ''}
    <div class="card"><div class="label">Validation-Failed Runs</div><div class="value">{len(validation_issue_keys)}</div></div>
    {primary_card if not validity_banner else '<div class="card"><div class="label">Policy Superiority</div><div class="value">재실험 전 판단 보류</div></div>'}
  </section>
  <section class="panel section">
    <h2>정책별 핵심 성능 요약</h2>
    <p>집계·공정성 검사를 통과한 run의 기술통계입니다. 시뮬레이션 유효성 경고가 있으면 정책 우위를 판단할 수 없습니다. {'이 표는 모든 worker 수의 run을 합칩니다. 표준편차에는 seed 변동뿐 아니라 worker 수에 따른 차이도 포함됩니다. 조건별 분산은 아래 worker별 그래프와 표를 확인하세요.' if len(plan_workers) > 1 else '제품 수 표준편차는 반복 seed 간 표본 표준편차입니다.'} 평균 실행비율은 각 run의 worker 실행비율 평균을 run에 대해 다시 평균한 값이며, 충전 태스크 수행시간도 포함합니다.</p>
    {_table(policy_summary_rows, policy_summary_columns)}
  </section>
  <section class="panel section">
    <h2>Experiment Runtime</h2>
    <p>실제 경과시간은 병렬 실행을 포함한 parent process wall clock입니다. 개별 run 시간의 합은 병렬 계산량을 나타내며 실제 경과시간과 구분합니다.</p>
    {_table(runtime_rows, ["metric", "value"])}
  </section>
  <div class="notice">{_esc(seed_notice)}</div>
  {capacity_method_html}
  {expected_reference_html}
  <nav class="tabs" aria-label="Objective results">
    {tab_buttons}
  </nav>
  {tab_panels}
  <section class="panel section">
    <h2>Fairness Check</h2>
    <p>Pre-run diagnostics and stochastic prefixes are compared within the same objective, worker-count, and seed group. The Task/Primitive timing fingerprint must match across every run. A passing aggregation audit does not override simulation-validity warnings above.</p>
    {_table(fairness_rows, fairness_cols)}
  </section>
  <section class="panel section">
    <h2>Individual Run Artifacts</h2>
    <p><code>Run Gantt</code>는 해당 정책과 seed의 실제 단일 실행 타임라인입니다. 여러 seed의 작업 구간을 평균하거나 합성한 정책별 집계 간트가 아닙니다.</p>
    {_run_links(output_root, display_run_rows)}
  </section>
</main>
<script>
  document.querySelectorAll('.tab-button').forEach((button) => {{
    button.addEventListener('click', () => {{
      document.querySelectorAll('.tab-button').forEach((item) => item.classList.remove('active'));
      document.querySelectorAll('.tab-panel').forEach((item) => item.classList.remove('active'));
      button.classList.add('active');
      document.getElementById(button.dataset.tab).classList.add('active');
    }});
  }});
</script>
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
