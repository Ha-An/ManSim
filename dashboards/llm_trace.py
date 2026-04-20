from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from .artifact_meta import format_run_mode_line, load_artifact_meta


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


def _safe_iso_text(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(sep=" ", timespec="seconds")
        except ValueError:
            return value
    return str(value)

def _runtime_workspace_diagnostics(output_dir: Path) -> dict[str, bool]:
    run_meta_path = output_dir / 'run_meta.json'
    if not run_meta_path.exists():
        return {}
    try:
        run_meta = json.loads(run_meta_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    runtime = (((run_meta.get('llm') or {}).get('openclaw') or {}).get('runtime') or {}) if isinstance(run_meta, dict) else {}
    workspace_root = Path(str(runtime.get('workspace_root', '')).strip()) if str(runtime.get('workspace_root', '')).strip() else None
    if workspace_root is None:
        return {}

    def _is_empty(alias: str) -> bool:
        request_path = workspace_root / alias / 'facts' / 'current_request.json'
        template_path = workspace_root / alias / 'facts' / 'current_response_template.json'
        try:
            request_text = request_path.read_text(encoding='utf-8', errors='replace').strip()
            template_text = template_path.read_text(encoding='utf-8', errors='replace').strip()
        except OSError:
            return False
        return request_text in {'', '{}'} or template_text in {'', '{}'}

    return {
        'reflect_input_empty': _is_empty('MANAGER_BOTTLENECK_DETECTOR'),
        'plan_input_empty': _is_empty('MANAGER_DAILY_PLANNER'),
    }


def _sort_agent_ids(values: set[str]) -> list[str]:
    def _key(v: str) -> tuple[int, str]:
        a = str(v).upper()
        if a == "MANAGER":
            return (0, a)
        if a.startswith("A") and a[1:].isdigit():
            return (1, f"{int(a[1:]):04d}")
        return (2, a)

    return sorted({str(v).upper() for v in values if str(v).strip()}, key=_key)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    q = max(0.0, min(1.0, q))
    vals = sorted(values)
    idx = (len(vals) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(vals) - 1)
    if lo == hi:
        return float(vals[lo])
    frac = idx - lo
    return float(vals[lo] + (vals[hi] - vals[lo]) * frac)


def _load_events(output_dir: Path) -> list[dict[str, Any]]:
    path = output_dir / "events.jsonl"
    if not path.exists():
        return []

    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fp:
            for raw in fp:
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows


def _build_task_lookup(events: list[dict[str, Any]]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    out: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for ev in events:
        if str(ev.get("type", "")).strip() != "AGENT_TASK_START":
            continue
        agent = str(ev.get("entity_id", "")).strip().upper()
        if not agent:
            continue
        day = _safe_int(ev.get("day"), 0)
        if day <= 0:
            continue
        out[(agent, day)].append(ev)
    return out


def _extract_agent(context: Any) -> str:
    if not isinstance(context, dict):
        return "SYSTEM"
    candidate = str(context.get("agent_id", context.get("agent", context.get("agent_name", "")))).strip()
    return candidate.upper() if candidate else "SYSTEM"


def _normalize_call_name(value: Any) -> str:
    return str(value or "").strip()


def _extract_prompt_text(record: dict[str, Any], max_len: int = 200) -> str:
    req = record.get("request", {}) if isinstance(record.get("request", {}), dict) else {}
    payload = req.get("payload", {}) if isinstance(req.get("payload", {}), dict) else {}

    if isinstance(payload, dict):
        if isinstance(payload.get("message"), str) and payload.get("message", "").strip():
            return _safe_text(payload.get("message", ""), max_len)
        if isinstance(payload.get("user_message"), str) and payload.get("user_message", "").strip():
            return _safe_text(payload.get("user_message", ""), max_len)
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for item in reversed(msgs):
                if isinstance(item, dict) and str(item.get("role", "")).strip().lower() == "user":
                    return _safe_text(item.get("content", ""), max_len)

    if isinstance(req.get("response_schema"), (dict, list)):
        return _safe_text(req.get("response_schema"), max_len)

    return "-"


def _extract_phase(record: dict[str, Any], context: dict[str, Any]) -> str:
    return _safe_text(context.get("phase", record.get("call_name", "")), 110)


def _extract_observed_task(event: dict[str, Any] | None) -> str:
    if not isinstance(event, dict):
        return ""

    details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
    if not isinstance(details, dict):
        details = {}

    task_type = str(details.get("task_type", "TASK")).strip()
    task_id = str(details.get("task_id", "")).strip()
    priority = str(details.get("priority_key", "")).strip()
    payload = details.get("payload", {}) if isinstance(details.get("payload", {}), dict) else {}
    station = ""
    if isinstance(payload, dict):
        station = str(payload.get("station", "")).strip()

    bits: list[str] = [task_type]
    if task_id:
        bits.append(task_id)
    if priority:
        bits.append(f"priority={priority}")
    if station:
        bits.append(f"station={station}")
    return " ".join(bits)


def _link_to_task(
    row: dict[str, Any],
    task_lookup: dict[tuple[str, int], list[dict[str, Any]]],
    task_state: dict[tuple[str, int], int],
) -> dict[str, Any] | None:
    raw = row.get("raw", {}) if isinstance(row.get("raw", {}), dict) else {}
    context = raw.get("context", {}) if isinstance(raw.get("context", {}), dict) else {}
    agent = str(row.get("agent", "")).strip().upper() or _extract_agent(context)
    day = _safe_int(row.get("day"), _safe_int(context.get("day"), 0))
    for candidate_day in (day, max(1, day - 1)):
        queue = task_lookup.get((agent, candidate_day), [])
        if not queue:
            continue
        idx = task_state.get((agent, candidate_day), 0)
        if idx >= len(queue):
            continue
        task_state[(agent, candidate_day)] = idx + 1
        return queue[idx]
    return None


def _extract_reason(parsed: Any, max_len: int = 140) -> str:
    if not isinstance(parsed, dict):
        return "-"

    if parsed.get("reason_trace") is not None and isinstance(parsed.get("reason_trace"), list):
        items = [str(item) for item in parsed.get("reason_trace", []) if str(item).strip()]
        if items:
            return "reasons: " + _safe_text(", ".join(items), max_len)

    for key in ("manager_summary", "watchouts", "decision_rule", "decision_source"):
        value = parsed.get(key)
        if value is not None and str(value).strip():
            return f"{key}={_safe_text(value, max_len)}"

    if isinstance(parsed.get("task_priority_weights"), dict):
        weights = parsed.get("task_priority_weights") or {}
        top = sorted(weights.items(), key=lambda kv: -_safe_float(kv[1], 0.0))[:2]
        if top:
            return "priority=" + ", ".join(f"{k}:{_safe_float(v):.1f}" for k, v in top)

    if parsed.get("completed") is not None or parsed.get("blocked") is not None:
        return f"completed={_safe_int(len(parsed.get('completed', [])), 0)} blocked={_safe_int(len(parsed.get('blocked', [])), 0)}"

    return _safe_text(parsed, max_len)


def _extract_action(call_name: str, parsed: Any) -> str:
    name = str(call_name or "").lower()
    if "manager_daily_planner" in name:
        return "publish daily plan"
    if "manager_bottleneck_detector" in name:
        return "update reflection"

    if isinstance(parsed, dict):
        if parsed.get("selected_task"):
            return f"select task: {_safe_text(parsed.get('selected_task'), 80)}"
        if parsed.get("decision_rule"):
            return f"apply rule: {_safe_text(parsed.get('decision_rule'), 80)}"
        if parsed.get("decision_source"):
            return f"source={_safe_text(parsed.get('decision_source'), 80)}"
    return f"execute {name or 'handler'}"


def _extract_thought(call_name: str, parsed: Any, context: dict[str, Any]) -> str:
    if not isinstance(parsed, dict):
        return f"context phase={_safe_text(context.get('phase', call_name), 80)}"

    return _extract_reason(parsed)


def _alignment_signal(thought: str, action: str, observed: str) -> bool | None:
    obs = (observed or "").strip()
    if not obs:
        return None

    observed_tokens = {t for t in obs.replace("_", " ").replace("-", " ").replace("=", " ").split() if t}
    expected = f"{thought} {action}".strip()
    expected_tokens = {t for t in expected.replace("_", " ").replace("-", " ").replace("=", " ").split() if t}

    if not expected_tokens:
        return None
    if observed_tokens & expected_tokens:
        return True

    keywords = {"transfer", "setup", "inspect", "repair", "maintenance", "delivery", "material", "unload", "battery"}
    return bool((observed_tokens & keywords) and (expected_tokens & keywords))


_NON_ACTION_CALL_MARKERS = (
    "manager_bottleneck_detector",
    "manager_daily_planner",
)


def _alignment_scope(call_name: str) -> str:
    normalized = (call_name or "").strip().lower()
    if any(marker in normalized for marker in _NON_ACTION_CALL_MARKERS):
        return "non_action"
    return "action_expected"


def _normalize_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            continue

        context = rec.get("context", {}) if isinstance(rec.get("context", {}), dict) else {}
        call_name = _normalize_call_name(rec.get("call_name"))
        parsed = rec.get("parsed", {}) if isinstance(rec.get("parsed", {}), dict) else {}
        latency = _safe_float(rec.get("latency_ms"), _safe_float(rec.get("latency_sec"), 0.0) * 1000.0)
        backend_health = rec.get("backend_health", {}) if isinstance(rec.get("backend_health", {}), dict) else {}

        out.append(
            {
                "idx": i,
                "call_id": _safe_int(rec.get("call_id"), i),
                "raw": rec,
                "agent": _extract_agent(context),
                "call_name": call_name,
                "phase": _extract_phase(rec, context),
                "status": str(rec.get("status", "")).upper() or "UNKNOWN",
                "day": _safe_int(context.get("day"), 0),
                "round": _safe_int(context.get("round"), 0),
                "latency_ms": latency,
                "transport_requested": _safe_text(rec.get("transport_requested", "-"), 64),
                "transport_used": _safe_text(rec.get("transport_used", "-"), 64),
                "backend_ok": bool(backend_health.get("ok", False)) if isinstance(backend_health, dict) else False,
                "backend_reason": _safe_text(backend_health.get("reason", ""), 180) if isinstance(backend_health, dict) else "-",
                "fallback": bool(rec.get("native_fallback_used", False)),
                "default_contract": bool(rec.get("native_default_contract_used", False)),
                "attempt_count": _safe_int(rec.get("attempt_count"), 0),
                "attempt_durations": rec.get("attempt_durations_ms") if isinstance(rec.get("attempt_durations_ms"), list) else [],
                "thought": _extract_thought(call_name, parsed, context),
                "action": _extract_action(call_name, parsed),
                "reason": _safe_text(parsed.get("decision_rationale", _extract_reason(parsed, 240)), 240),
                "prompt": _extract_prompt_text(rec, 220),
                "response": _safe_text(rec.get("response_text", ""), 420),
                "error": _safe_text(rec.get("error", ""), 240),
                "alignment_scope": _alignment_scope(call_name),
                "alignment_reason": "pending",
            }
        )

    return out


def _build_summary_cards(rows: list[dict[str, Any]], diagnostics: dict[str, bool] | None = None) -> str:
    total = len(rows)
    if total == 0:
        return "<span class='badge'>No LLM calls.</span>"

    ok_count = sum(1 for r in rows if r["status"] == "OK")
    errs = total - ok_count
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] >= 0]
    p50 = _quantile(latencies, 0.5)
    p95 = _quantile(latencies, 0.95)
    avg = sum(latencies) / len(latencies) if latencies else 0.0

    by_agent = Counter(r["agent"] for r in rows)
    top_agents = " / ".join(f"{k}({v})" for k, v in by_agent.most_common(4))

    native_requested = sum(1 for r in rows if r["transport_requested"] == "native_local")
    native_used = sum(1 for r in rows if r["transport_used"] == "native_local")
    fallback = sum(1 for r in rows if r["fallback"])
    default_contract = sum(1 for r in rows if r["default_contract"])
    backend_ok = sum(1 for r in rows if r["backend_ok"])
    attempts = [r["attempt_count"] for r in rows if r["attempt_count"] > 0]
    avg_attempts = (sum(attempts) / len(attempts)) if attempts else 0.0

    action_rows = [r for r in rows if r.get("alignment_scope") == "action_expected"]
    non_action = sum(1 for r in rows if r.get("alignment_scope") == "non_action")
    mismatch = sum(1 for r in action_rows if r.get("alignment") is False)
    align_unknown = sum(1 for r in action_rows if r.get("alignment") is None)
    align_true = sum(1 for r in action_rows if r.get("alignment") is True)

    diagnostics = diagnostics or {}
    reflect_empty = bool(diagnostics.get("reflect_input_empty", False))
    plan_empty = bool(diagnostics.get("plan_input_empty", False))

    return (
        f"<span class='badge'>LLM calls: {total}</span>"
        f"<span class='badge'>success={ok_count}/{total} ({ok_count/total:.1%})</span>"
        f"<span class='badge'>errors={errs}</span>"
        f"<span class='badge'>latency mean={avg:.1f} p50={p50:.1f} p95={p95:.1f} ms</span>"
        f"<span class='badge'>native req/used: {native_requested}/{native_used}</span>"
        f"<span class='badge'>fallback {fallback}/{native_requested if native_requested else 0} ({fallback / native_requested if native_requested else 0:.1%})</span>"
        f"<span class='badge'>default contract {default_contract}/{native_requested if native_requested else 0} ({default_contract / native_requested if native_requested else 0:.1%})</span>"
        f"<span class='badge'>backend ok={backend_ok}/{len(rows)} ({backend_ok/len(rows):.1%})</span>"
        f"<span class='badge'>avg retries={avg_attempts:.2f}</span>"
        f"<span class='badge'>alignment ok={align_true} mismatch={mismatch} unknown={align_unknown}</span>"
        f"<span class='badge'>non-action phases={non_action}</span>"
        f"<span class='badge'>top agents: {top_agents or '-'}</span>"
        f"<span class='badge'>{'reflect_input_empty=1' if reflect_empty else 'reflect_input_empty=0'}</span>"
        f"<span class='badge'>{'plan_input_empty=1' if plan_empty else 'plan_input_empty=0'}</span>"
    )


def _chart_timeline(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No LLM data.</p>"

    try:
        import plotly.graph_objects as go
    except Exception:
        return "<p>Plotly unavailable.</p>"

    agents = _sort_agent_ids({r["agent"] for r in rows})
    idx = {a: i for i, a in enumerate(agents)}
    fig = go.Figure()

    for status, color in (("OK", "#16a34a"), ("ERROR", "#dc2626"), ("UNKNOWN", "#64748b")):
        subset = [r for r in rows if r["status"] == status]
        if not subset:
            continue
        fig.add_trace(
            go.Scatter(
                x=[r["idx"] for r in subset],
                y=[idx[r["agent"]] for r in subset],
                mode="markers",
                name=status,
                marker=dict(size=11, color=color),
                text=[
                    f"{r['agent']} | day={r['day']} | {r['call_name']} | latency={r['latency_ms']:.1f}ms | {r.get('alignment')}"
                    for r in subset
                ],
                hovertemplate="%{text}<extra></extra>",
            )
        )

    fig.update_layout(
        title="LLM Call Timeline by Agent",
        xaxis_title="global call index",
        yaxis_title="agent",
        yaxis=dict(tickvals=list(idx.values()), ticktext=list(idx.keys())),
        height=300,
        margin=dict(l=60, r=16, t=44, b=38),
    )
    return fig.to_html(full_html=False, include_plotlyjs="inline")


def _chart_phase_by_agent(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No phase data.</p>"

    by_agent: dict[str, Counter[str]] = {}
    for r in rows:
        by_agent.setdefault(r["agent"], Counter())
        by_agent[r["agent"]][r["call_name"]] += 1

    phases = sorted({p for row in rows for p in [row["call_name"]] if p})
    if not phases:
        return "<p>No phase data.</p>"

    try:
        import plotly.graph_objects as go
    except Exception:
        return "<p>Plotly unavailable.</p>"

    fig = go.Figure()
    for agent in _sort_agent_ids(set(by_agent.keys())):
        fig.add_trace(go.Bar(name=agent, x=phases, y=[by_agent[agent][p] for p in phases]))

    fig.update_layout(
        barmode="stack",
        title="Call Phases per Agent",
        xaxis_title="phase",
        yaxis_title="count",
        height=300,
        margin=dict(l=50, r=16, t=42, b=36),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _chart_daily_alignment(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No alignment data.</p>"

    summary: dict[int, Counter[str]] = defaultdict(Counter)
    for r in rows:
        if r.get("alignment_scope") != "action_expected":
            continue
        day = _safe_int(r.get("day"), 0)
        if day <= 0:
            continue
        status = r.get("alignment")
        if status is True:
            key = "match"
        elif status is False:
            key = "mismatch"
        else:
            key = "unknown"
        summary[day][key] += 1

    days = sorted(summary)
    if not days:
        return "<p>No day alignment data.</p>"

    try:
        import plotly.graph_objects as go
    except Exception:
        return "<p>Plotly unavailable.</p>"

    fig = go.Figure(
        data=[
            go.Bar(name="match", x=[f"D{d}" for d in days], y=[summary[d]["match"] for d in days], marker_color="#16a34a"),
            go.Bar(name="mismatch", x=[f"D{d}" for d in days], y=[summary[d]["mismatch"] for d in days], marker_color="#dc2626"),
            go.Bar(name="unknown", x=[f"D{d}" for d in days], y=[summary[d]["unknown"] for d in days], marker_color="#64748b"),
        ]
    )
    fig.update_layout(barmode="stack", title="Thought-Action Alignment by Day", xaxis_title="day", yaxis_title="count", height=250, margin=dict(l=46, r=16, t=46, b=34))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _chart_latency_days(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No latency data.</p>"

    by_day: defaultdict[int, list[float]] = defaultdict(list)
    for r in rows:
        if r["latency_ms"] < 0:
            continue
        day = _safe_int(r.get("day"), 0)
        if day <= 0:
            continue
        by_day[day].append(r["latency_ms"])

    if not by_day:
        return "<p>No latency data.</p>"

    days = sorted(by_day)
    mean = [sum(by_day[d]) / len(by_day[d]) for d in days]
    p95 = [_quantile(by_day[d], 0.95) for d in days]

    try:
        import plotly.graph_objects as go
    except Exception:
        return "<p>Plotly unavailable.</p>"

    fig = go.Figure()
    fig.add_trace(
        go.Bar(name="calls", x=[f"D{d}" for d in days], y=[len(by_day[d]) for d in days], marker_color="#0ea5e9", yaxis="y1")
    )
    fig.add_trace(
        go.Scatter(
            x=[f"D{d}" for d in days],
            y=mean,
            name="avg ms",
            mode="lines+markers",
            marker=dict(color="#334155"),
            yaxis="y2",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=[f"D{d}" for d in days],
            y=p95,
            name="p95 ms",
            mode="lines+markers",
            marker=dict(color="#7c2d12"),
            yaxis="y2",
        )
    )
    fig.update_layout(
        title="Daily Latency",
        xaxis_title="day",
        yaxis=dict(title="calls", side="left"),
        yaxis2=dict(title="latency (ms)", overlaying="y", side="right"),
        height=250,
        margin=dict(l=54, r=32, t=46, b=34),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _build_transport_distribution(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No transport data.</p>"

    stat = Counter()
    for r in rows:
        req = str(r.get("transport_requested", "") or "-")
        used = str(r.get("transport_used", "") or "-")
        if req:
            stat[f"requested:{req}"] += 1
        if used and used != req:
            stat[f"used:{used}"] += 1

    if not stat:
        return "<p>No transport data.</p>"

    try:
        import plotly.graph_objects as go
    except Exception:
        return "<p>Plotly unavailable.</p>"

    labels = list(stat.keys())
    values = [stat[k] for k in labels]
    fig = go.Figure(go.Pie(values=values, labels=labels, textinfo="label+percent", hole=0.4))
    fig.update_layout(title="Transport request / usage", height=250, margin=dict(l=20, r=20, t=40, b=20))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _build_mismatch_list(rows: list[dict[str, Any]]) -> str:
    mismatches = [r for r in rows if r.get("alignment") is False]
    if not mismatches:
        return "<p>No mismatch found.</p>"

    rows_html: list[str] = []
    for r in mismatches:
        rows_html.extend(
            [
                f"<details class='ledger'><summary>[{r['call_id']}] {r['agent']} Day {r['day']} {r['phase']}</summary>",
                f"<div class='kv'><span>Observed</span><p>{escape(_safe_text(r.get('observed_task', ''), 180))}</p></div>",
                f"<div class='kv'><span>Thought</span><p>{escape(r['thought'])}</p></div>",
                f"<div class='kv'><span>Action</span><p>{escape(r['action'])}</p></div>",
                f"<div class='kv'><span>Reason</span><p>{escape(r['reason'])}</p></div>",
                f"<div class='kv'><span>Transport</span><p>{escape(r['transport_requested'])} -> {escape(r['transport_used'])}</p></div>",
                "</details>",
            ]
        )
    return "<div class='mismatch-list'>" + "\n".join(rows_html) + "</div>"


def _build_reasoning_lane(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No calls.</p>"

    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_agent[r["agent"]].append(r)

    lanes: list[str] = []
    for agent in _sort_agent_ids(set(by_agent.keys())):
        entries: list[str] = []
        for r in by_agent[agent]:
            align = r.get("alignment")
            if r.get("alignment_scope") == "non_action":
                align_text = "NON_ACTION"
                cls = "unknown"
            else:
                align_text = "MATCH" if align is True else "MISMATCH" if align is False else "UNKNOWN"
                cls = "match" if align is True else "mismatch" if align is False else "unknown"
            status = r["status"]
            status_cls = "ok" if status == "OK" else "err" if status == "ERROR" else "warn"
            pill = f"<span class='pill {cls}'>{align_text}</span><span class='pill {status_cls}'>{status}</span>"
            entries.append(
                "\n".join(
                    [
                        f"<details class='lane-step'>",
                        f"<summary>{pill} {r['call_id']} -> Day {r['day']} R{r['round']} -> {r['phase']} -> latency={r['latency_ms']:.1f}ms</summary>",
                        "<div class='lane-body'>",
                        f"<div class='kv'><span>Thought</span><p>{escape(r['thought'])}</p></div>",
                        f"<div class='kv'><span>Action</span><p>{escape(r['action'])}</p></div>",
                        f"<div class='kv'><span>Observed task</span><p>{escape(_safe_text(r.get('observed_task', ''), 180))}</p></div>",
                        f"<div class='kv'><span>Alignment reason</span><p>{escape(_safe_text(r.get('alignment_reason', ''), 180))}</p></div>",
                        f"<div class='kv'><span>Prompt snippet</span><p>{escape(r['prompt'])}</p></div>",
                        f"<div class='kv'><span>Reason</span><p>{escape(r['reason'])}</p></div>",
                        f"<div class='kv'><span>Transport</span><p>{escape(r['transport_requested'])} -> {escape(r['transport_used'])}</p></div>",
                        f"<div class='kv'><span>Response</span><p>{escape(r['response'])}</p></div>",
                        f"<div class='kv'><span>Error</span><p>{escape(r['error'])}</p></div>",
                        "</div>",
                        "</details>",
                    ]
                )
            )

        lanes.append(
            f"<section class='agent-lane'>\n"
            f"<h3>{agent}</h3>\n"
            f"<p class='lane-sub'>calls={len(by_agent[agent])} | attempts={sum(int(r.get('attempt_count',0)) for r in by_agent[agent])}</p>\n"
            f"{''.join(entries)}\n"
            "</section>"
        )

    return "\n".join(lanes)


def export_llm_trace_dashboard(*, records: list[dict[str, Any]], output_dir: Path) -> Path | None:
    if not isinstance(records, list) or not records:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "llm_trace.html"

    meta = load_artifact_meta(output_dir)
    events = _load_events(output_dir)
    task_lookup = _build_task_lookup(events)

    normalized = _normalize_records(records)
    sorted_rows = sorted(
        normalized,
        key=lambda r: (
            r["idx"],
            _safe_int(r.get("day", 0), 0),
            _safe_int(r.get("round", 0), 0),
        ),
    )

    state: dict[tuple[str, int], int] = {}
    for row in sorted_rows:
        if row.get("alignment_scope") != "action_expected":
            row["observed_task"] = ""
            row["alignment"] = None
            row["alignment_reason"] = "non_action_phase"
            continue
        linked = _link_to_task(row, task_lookup=task_lookup, task_state=state)
        row["observed_task"] = _extract_observed_task(linked)
        if not row["observed_task"]:
            row["alignment"] = None
            row["alignment_reason"] = "link_failed"
            continue
        row["alignment"] = _alignment_signal(row["thought"], row["action"], row.get("observed_task"))
        row["alignment_reason"] = "token_match" if row["alignment"] is True else "token_mismatch" if row["alignment"] is False else "unverified"

    html = "\n".join(
        [
            "<!doctype html>",
            "<html lang='en'>",
            "<head>",
            "<meta charset='utf-8' />",
            "<meta name='viewport' content='width=device-width, initial-scale=1' />",
            f"<title>LLM Exchange Trace</title>",
            "<style>",
            "body{margin:18px;font-family:Inter,'Noto Sans KR',Arial,sans-serif;background:#f6f8fc;color:#0f172a;line-height:1.35;}",
            ".summary{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0;}",
            ".badge{background:#e2e8f0;border:1px solid #cbd5e1;padding:6px 10px;border-radius:999px;font-size:12px;display:inline-block;white-space:nowrap;}",
            ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px;margin-bottom:12px;}",
            ".card{background:#fff;border:1px solid #dbe2ec;border-radius:12px;padding:10px;box-shadow:0 8px 22px rgba(15,23,42,.05);}",
            ".lane{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;}",
            ".agent-lane{background:#fff;border:1px solid #d5e2ee;border-left:4px solid #1d4ed8;border-radius:10px;padding:8px;}",
            ".lane-step{margin:7px 0;border:1px solid #e2e8f0;border-radius:8px;padding:5px;background:#fff;}",
            ".lane-step summary{cursor:pointer;font-weight:700;display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px;}",
            ".lane-sub{color:#334155;margin:0 0 6px; font-size:12px;}",
            ".pill{display:inline-block;padding:2px 7px;border-radius:999px;font-size:11px;}",
            ".match{background:#dcfce7;color:#14532d;}",
            ".mismatch{background:#fee2e2;color:#7f1d1d;}",
            ".unknown{background:#f1f5f9;color:#334155;}",
            ".ok{background:#dcfce7;color:#14532d;}",
            ".err{background:#fecaca;color:#991b1b;}",
            ".warn{background:#f1f5f9;color:#334155;}",
            ".kv{margin:5px 0;}",
            ".kv span{font-size:12px;font-weight:700;display:block;color:#334155;margin-bottom:2px;}",
            ".kv p{margin:0;white-space:pre-wrap;word-break:break-word;color:#334155;}",
            "h1,h2,h3{margin:6px 0 10px;}",
            ".muted{color:#64748b;font-size:12px;}",
            "</style>",
            "</head>",
            "<body>",
            "<h1>LLM Exchange Trace</h1>",
            f"<p><strong>{escape(format_run_mode_line(meta))}</strong></p>",
            f"<p class='muted'>events.jsonl: {escape(str(meta.get('events_path', '-')))}</p>",
            f"<div class='summary'>{_build_summary_cards(sorted_rows)}</div>",
            "<div class='grid'>",
            "<section class='card'><h2>Call Timeline</h2>" + _chart_timeline(sorted_rows) + "</section>",
            "<section class='card'><h2>Thought/Action Alignment by Day</h2>" + _chart_daily_alignment(sorted_rows) + "</section>",
            "<section class='card'><h2>Phase Mix</h2>" + _chart_phase_by_agent(sorted_rows) + "</section>",
            "<section class='card'><h2>Latency by Day</h2>" + _chart_latency_days(sorted_rows) + "</section>",
            "</div>",
            "<div class='grid'>",
            "<section class='card'><h2>Transport Distribution</h2>" + _build_transport_distribution(sorted_rows) + "</section>",
            "<section class='card'><h2>Alignment Mismatch</h2>" + _build_mismatch_list(sorted_rows) + "</section>",
            "</div>",
            "<h2>Agent Behavior Lanes</h2>",
            "<div class='lane'>" + _build_reasoning_lane(sorted_rows) + "</div>",
            f"<p class='muted'>generated: {_safe_iso_text(meta.get('wall_clock_human'))}</p>",
            "</body>",
            "</html>",
        ]
    )

    html_path.write_text(html, encoding="utf-8")
    return html_path






