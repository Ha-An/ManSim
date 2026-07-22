from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def build_operational_task_complexity_metrics(
    task_counts: dict[str, int],
    *,
    num_days: float,
    catalog: Any | None = None,
) -> dict[str, Any]:
    """Compute run-level OTC from completed task instance counts.

    Pool-only or skipped opportunities are intentionally excluded. The metric
    represents executed humanoid workload, so callers should pass task
    instances that reached execution completion.
    """
    normalized_counts = {
        str(task_code).strip().upper(): int(count)
        for task_code, count in task_counts.items()
        if str(task_code).strip() and int(count or 0) > 0
    }
    complexity_index = _load_task_complexity_index(catalog)
    by_task: dict[str, dict[str, Any]] = {}
    primitive_counts: Counter[str] = Counter()
    primitive_contributions: defaultdict[str, float] = defaultdict(float)
    cumulative = 0.0
    missing_task_codes: list[str] = []

    for task_code, count in sorted(normalized_counts.items()):
        payload = complexity_index.get(task_code)
        if not isinstance(payload, dict):
            missing_task_codes.append(task_code)
            task_complexity = 0.0
            primitive_count = 0
            primitive_contribution = {}
            primitive_count_payload = {}
        else:
            task_complexity = float(payload.get("complexity", 0.0) or 0.0)
            primitive_count = int(payload.get("primitive_count", 0) or 0)
            primitive_contribution = payload.get("primitive_contributions", {})
            primitive_count_payload = payload.get("primitive_counts", {})
        cumulative_contribution = task_complexity * float(count)
        cumulative += cumulative_contribution
        by_task[task_code] = {
            "instance_count": count,
            "task_complexity": round(task_complexity, 3),
            "primitive_count": primitive_count,
            "cumulative_complexity": round(cumulative_contribution, 3),
        }
        if isinstance(primitive_count_payload, dict):
            for primitive_code, primitive_count_value in primitive_count_payload.items():
                primitive_counts[str(primitive_code)] += int(primitive_count_value or 0) * count
        if isinstance(primitive_contribution, dict):
            for primitive_code, contribution in primitive_contribution.items():
                primitive_contributions[str(primitive_code)] += float(contribution or 0.0) * float(count)

    period_days = max(1e-9, float(num_days or 0.0))
    otc = cumulative / period_days
    return {
        "operational_task_complexity": round(otc, 3),
        "otc": round(otc, 3),
        "cumulative_operational_complexity_over_n_days": round(cumulative, 3),
        "operational_complexity_period_days": round(period_days, 3),
        "operational_task_complexity_details": {
            "formula": "OTC=sum_t(N_t/n*C_task(t)); C_task(t)=sum_k(a_tk*d_k)",
            "task_instance_count": int(sum(normalized_counts.values())),
            "task_instance_count_by_code": dict(sorted(normalized_counts.items())),
            "by_task": by_task,
            "primitive_execution_count_by_code": dict(sorted(primitive_counts.items())),
            "primitive_complexity_contribution_by_code": {
                key: round(value, 3)
                for key, value in sorted(primitive_contributions.items(), key=lambda item: (-item[1], item[0]))
            },
            "missing_task_codes": missing_task_codes,
        },
    }


def _load_task_complexity_index(catalog: Any | None) -> dict[str, dict[str, Any]]:
    try:
        from humanoidsim import task_complexity_index

        return task_complexity_index(catalog=catalog)
    except Exception:
        return {}
