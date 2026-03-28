from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from .artifact_meta import add_plotly_meta_header, format_run_mode_line, load_artifact_meta


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


def _safe_text(value: Any, max_len: int = 160) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return "-"
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _safe_iso(value: Any) -> str:
    if not value:
        return "-"
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(sep=" ", timespec="seconds")
        except ValueError:
            return value
    return str(value)


def _safe_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None

def _runtime_workspace_diagnostics(output_dir: Path) -> dict[str, bool]:
    run_meta = _safe_json(output_dir / 'run_meta.json')
    if not isinstance(run_meta, dict):
        return {}
    runtime = (((run_meta.get('llm') or {}).get('openclaw') or {}).get('runtime') or {})
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


def _safe_day_dict(payload: Any) -> dict[int, Any]:
    out: dict[int, Any] = {}
    if not isinstance(payload, dict):
        return out
    return {k: v for k, v in payload.items() if isinstance(k, int)}


def _sort_agent_ids(values: set[str]) -> list[str]:
    def _key(v: str) -> tuple[int, str]:
        a = str(v).upper()
        if a == "MANAGER":
            return (0, a)
        if a.startswith("A") and a[1:].isdigit():
            return (1, f"{int(a[1:]):04d}")
        return (2, a)

    return sorted({str(v).upper() for v in values if str(v).strip()}, key=_key)


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


def _collect_task_lookup(events: list[dict[str, Any]]) -> dict[tuple[str, int], list[dict[str, Any]]]:
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


def _link_task(
    record: dict[str, Any],
    task_lookup: dict[tuple[str, int], list[dict[str, Any]]],
    task_state: dict[tuple[str, int], int],
) -> dict[str, Any] | None:
    context = record.get("context", {}) if isinstance(record.get("context", {}), dict) else {}
    agent = _extract_agent(context)
    day = _safe_int(context.get("day"), 0)
    for d in (day, max(1, day - 1)):
        key = (agent, d)
        queue = task_lookup.get(key, [])
        if not queue:
            continue
        idx = task_state.get(key, 0)
        if idx < len(queue):
            task_state[key] = idx + 1
            return queue[idx]
    return None


def _extract_agent(context: Any) -> str:
    if not isinstance(context, dict):
        return "SYSTEM"
    candidate = str(context.get("agent_id", context.get("agent", context.get("agent_name", "")))).strip()
    return candidate.upper() if candidate else "SYSTEM"


def _extract_observed_task(event: dict[str, Any] | None) -> str:
    if not isinstance(event, dict):
        return ""
    details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
    if not isinstance(details, dict):
        details = {}

    task_type = str(details.get("task_type", "TASK")).strip()
    task_id = str(details.get("task_id", "")).strip()
    priority = str(details.get("priority_key", "")).strip()
    station = ""
    payload = details.get("payload", {}) if isinstance(details.get("payload", {}), dict) else {}
    if isinstance(payload, dict):
        station = str(payload.get("station", "")).strip()

    parts = [task_type]
    if task_id:
        parts.append(task_id)
    if priority:
        parts.append(f"priority={priority}")
    if station:
        parts.append(f"station={station}")
    return " ".join(parts).strip()


def _extract_phase(context: dict[str, Any], call_name: str) -> str:
    return _safe_text(context.get("phase", call_name), 120)


def _extract_thought(call_name: str, parsed: Any, context: dict[str, Any]) -> str:
    if not isinstance(parsed, dict):
        return "No structured reasoning"

    ordered_keys = [
        "manager_summary",
        "watchouts",
        "decision_rule",
        "decision_source",
        "selected_task",
        "personal_conclusion",
        "decision_rationale",
    ]
    for key in ordered_keys:
        if isinstance(parsed.get(key), (list, dict)):
            value = parsed.get(key)
            if value:
                return f"{key}: {_safe_text(value, 160)}"
        elif parsed.get(key) not in (None, ""):
            return f"{key}: {_safe_text(parsed.get(key), 160)}"

    if "reason_trace" in parsed and isinstance(parsed.get("reason_trace"), list):
        items = [str(item) for item in parsed.get("reason_trace") if str(item).strip()]
        if items:
            return f"reason_trace={_safe_text(items, 180)}"

    phase = str(context.get("phase", "")).strip()
    if phase:
        return f"phase={_safe_text(phase, 120)}"

    return f"call={call_name or 'unknown'}"


def _extract_action(call_name: str, parsed: Any) -> str:
    if not isinstance(parsed, dict):
        parsed = {}
    name = str(call_name or "").lower()
    if name == "manager_daily_planner":
        return "publish daily plan"
    if name == "manager_bottleneck_detector":
        return "publish reflection"

    if isinstance(parsed.get("selected_task"), str) and parsed.get("selected_task"):
        return f"select task={_safe_text(parsed.get('selected_task'), 100)}"
    if isinstance(parsed.get("decision_rule"), str) and parsed.get("decision_rule"):
        return f"apply rule={_safe_text(parsed.get('decision_rule'), 100)}"
    return f"execute {name or 'handler'}"


def _extract_reason(parsed: Any, max_len: int = 220) -> str:
    if not isinstance(parsed, dict):
        return "-"
    value = parsed.get("decision_rationale") if parsed.get("decision_rationale") else parsed.get("reason")
    if value not in (None, ""):
        return _safe_text(value, max_len)

    for key in ("watchouts", "manager_summary", "coordination_notes", "decision_rule", "local_risks"):
        if key not in parsed:
            continue
        if parsed.get(key) is not None:
            return f"{key}: {_safe_text(parsed.get(key), max_len)}"

    return _safe_text(parsed, max_len)


def _extract_prompt_text(record: dict[str, Any], max_len: int = 170) -> str:
    req = record.get("request", {}) if isinstance(record.get("request", {}), dict) else {}
    payload = req.get("payload", {}) if isinstance(req.get("payload", {}), dict) else {}
    if isinstance(payload, dict):
        if isinstance(payload.get("message"), str):
            return _safe_text(payload.get("message", ""), max_len)
        if isinstance(payload.get("user_message"), str):
            return _safe_text(payload.get("user_message", ""), max_len)
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for item in reversed(msgs):
                if isinstance(item, dict) and str(item.get("role", "")).strip().lower() == "user":
                    return _safe_text(item.get("content", ""), max_len)
    return "-"


def _alignment_signal(thought: str, action: str, observed_task: str) -> bool | None:
    obs = (observed_task or "").replace("_", " ").replace("-", " ").replace("=", " ").strip()
    if not obs:
        return None
    observed_tokens = {t for t in obs.split() if t}
    expected_tokens = {t for t in f"{thought} {action}".replace("_", " ").replace("-", " ").replace("=", " ").split() if t}
    if not expected_tokens:
        return None
    if observed_tokens & expected_tokens:
        return True

    keywords = {
        "transfer",
        "setup",
        "inspect",
        "repair",
        "maintenance",
        "delivery",
        "material",
        "unload",
        "battery",
        "deliver",
        "inspect",
    }
    return bool(observed_tokens & keywords and expected_tokens & keywords)


_NON_ACTION_CALL_MARKERS = (
    "manager_bottleneck_detector",
    "manager_daily_planner",
)


def _alignment_scope(call_name: str) -> str:
    normalized = (call_name or "").strip().lower()
    if any(marker in normalized for marker in _NON_ACTION_CALL_MARKERS):
        return "non_action"
    return "action_expected"


def _load_workspace_root(output_dir: Path) -> Path | None:
    meta = _safe_json(output_dir / "run_meta.json")
    if not isinstance(meta, dict):
        return None

    llm = meta.get("llm", {}) if isinstance(meta.get("llm", {}), dict) else {}
    openclaw = llm.get("openclaw", {}) if isinstance(llm.get("openclaw", {}), dict) else {}

    runtime_entry = str(openclaw.get("runtime", {}).get("workspace_root", "")).strip() if isinstance(openclaw.get("runtime", {}), dict) else ""
    if runtime_entry:
        runtime_root = Path(runtime_entry)
        if runtime_root.exists():
            return runtime_root

    configured_root = str(openclaw.get("workspace_root", "")).strip()
    if configured_root:
        candidate = Path(configured_root)
        if candidate.exists():
            return candidate
        candidate2 = output_dir / configured_root
        if candidate2.exists():
            return candidate2

    fallback = output_dir / "openclaw" / "workspaces"
    if fallback.exists():
        return fallback
    return None
def _safe_text_list(value: Any, max_len: int = 220) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        items = [str(i) for i in value if str(i).strip()]
        return _safe_text(", ".join(items), max_len)
    if isinstance(value, dict):
        return _safe_text(", ".join(f"{k}:{_safe_text(v, 40)}" for k, v in value.items()), max_len)
    return _safe_text(value, max_len)


def _load_day_history(path: Path) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    out: dict[int, dict[str, Any]] = {}
    for fp in path.glob("day_*.json"):
        m = re.match(r"^day_(\d+)", fp.name)
        if not m:
            continue
        try:
            day = int(m.group(1))
        except ValueError:
            continue
        payload = _safe_json(fp)
        if isinstance(payload, dict):
            out[day] = payload
    return out


def _safe_count_field(payload: Any, field: str) -> int:
    if not isinstance(payload, dict):
        return 0
    if field not in payload:
        return 0
    value = payload.get(field)
    if isinstance(value, list):
        return len([x for x in value if str(x).strip()])
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, str):
        return 1 if value.strip() else 0
    return 0


def _safe_markdown(path: Path, max_len: int = 280) -> str:
    if not path.exists():
        return "-"
    try:
        return _safe_text(path.read_text(encoding="utf-8"), max_len)
    except OSError:
        return "-"

def _collect_agent_ids(records: list[dict[str, Any]], events: list[dict[str, Any]], workspace_root: Path | None) -> list[str]:
    ids: set[str] = set()

    for rec in records:
        ids.add(_extract_agent(rec.get("context", {}) if isinstance(rec.get("context", {}), dict) else {}))

    for ev in events:
        ent = str(ev.get("entity_id", "")).strip().upper()
        if ent:
            ids.add(ent)

    if workspace_root and workspace_root.exists():
        for child in workspace_root.iterdir():
            if child.is_dir():
                ids.add(child.name.upper())

    if not ids:
        ids.add("SYSTEM")
    return _sort_agent_ids(ids)


def _safe_json_or_empty_dict(path: Path) -> dict[str, Any]:
    value = _safe_json(path)
    return value if isinstance(value, dict) else {}


def _load_history_payload(path: Path, current_file: str, suffix_filter: str | None = None) -> dict[str, Any]:
    current = _safe_json_or_empty_dict(path / current_file)
    history_path = path / "history"
    history = _load_day_history(history_path) if history_path.exists() else {}
    if suffix_filter:
        # Rebuild history by file prefix/suffix pattern if explicit suffix is requested.
        history = {}
        if path.exists():
            for fp in path.glob(f"day_*.json"):
                if not fp.name.endswith(suffix_filter):
                    continue
                m = re.match(r"^day_(\d+)", fp.name)
                if not m:
                    continue
                day = _safe_int(m.group(1), 0)
                if day <= 0:
                    continue
                payload = _safe_json(fp)
                if isinstance(payload, dict):
                    history[day] = payload

    return {
        "current": current,
        "history": history,
    }


def _load_cognition(agent_ids: list[str], workspace_root: Path | None) -> dict[str, dict[str, Any]]:
    if workspace_root is None or not workspace_root.exists():
        return {}

    out: dict[str, dict[str, Any]] = {}
    for agent in agent_ids:
        root = workspace_root / agent
        if not root.exists():
            continue

        beliefs = _load_history_payload(root / "beliefs", "current_beliefs.json")
        commitments = _load_history_payload(root / "commitments", "current_commitment.json")
        semantic = _load_history_payload(root / "memory" / "semantic", "current.json", suffix_filter=None)

        facts_root = root / "facts"
        facts_current = _safe_json_or_empty_dict(facts_root / "current_phase.txt")
        if isinstance(facts_current, str):
            facts_current = {"current_phase": facts_current.strip()}

        current_queue = _safe_json_or_empty_dict(facts_root / "current_personal_queue.json")
        current_mailbox = _safe_json_or_empty_dict(facts_root / "current_mailbox.json")
        memory_current = _safe_json_or_empty_dict(root / "memory" / "current.json")
        rolling = _safe_markdown(root / "memory" / "rolling_summary.md", 280)

        facts_reports = _load_day_history(facts_root / "report_history")
        facts_requests = _load_day_history(facts_root / "request_history")
        memory_daily = _load_day_history(root / "memory" / "daily")
        memory_episodic = _load_day_history(root / "memory" / "episodic")
        mailbox = _load_day_history(root / "mailboxes")
        plans = _load_day_history(root / "plans")
        reports = _load_day_history(root / "reports")

        trace_history: dict[int, Any] = {}
        trace_dir = root / "trace"
        if trace_dir.exists():
            for fp in trace_dir.glob("day_*.json"):
                m = re.match(r"^day_(\d+).+\.json$", fp.name)
                if not m:
                    continue
                day = _safe_int(m.group(1), 0)
                if day <= 0:
                    continue
                payload = _safe_json(fp)
                if payload is None:
                    continue
                trace_history[day] = payload

        out[agent] = {
            "beliefs": beliefs,
            "commitments": commitments,
            "semantic": semantic,
            "facts": {
                "current": facts_current,
                "current_queue": current_queue,
                "current_mailbox": current_mailbox,
                "history_report": facts_reports,
                "history_request": facts_requests,
            },
            "memory": {
                "current": memory_current,
                "daily": memory_daily,
                "episodic": memory_episodic,
            },
            "plans": plans,
            "reports": reports,
            "mailbox": mailbox,
            "trace": trace_history,
            "rolling_summary": rolling,
        }

    return out


def _normalize_llm_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for i, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            continue

        context = rec.get("context", {}) if isinstance(rec.get("context", {}), dict) else {}
        parsed = rec.get("parsed", {}) if isinstance(rec.get("parsed", {}), dict) else {}
        call_name = str(rec.get("call_name", "")).strip()
        thought = _extract_thought(call_name, parsed, context)
        action = _extract_action(call_name, parsed)

        row = {
            "idx": i,
            "call_id": _safe_int(rec.get("call_id"), i),
            "raw": rec,
            "agent": _extract_agent(context),
            "call_name": call_name,
            "status": str(rec.get("status", "")).upper() or "UNKNOWN",
            "day": _safe_int(context.get("day"), 0),
            "round": _safe_int(context.get("round"), 0),
            "phase": _extract_phase(context, call_name),
            "latency_ms": _safe_float(rec.get("latency_ms"), _safe_float(rec.get("latency_sec"), 0.0) * 1000.0),
            "attempt_count": _safe_int(rec.get("attempt_count"), 0),
            "transport_requested": _safe_text(rec.get("transport_requested", "-"), 80),
            "transport_used": _safe_text(rec.get("transport_used", "-"), 80),
            "thought": thought,
            "action": action,
            "reason": _extract_reason(parsed),
            "prompt": _extract_prompt_text(rec),
            "response": _safe_text(rec.get("response_text", rec.get("response", "")), 280),
            "error": _safe_text(rec.get("error", ""), 180),
            "alignment_scope": _alignment_scope(call_name),
            "alignment_reason": "pending",
        }

        normalized.append(row)

    return normalized


def _build_summary_cards(rows: list[dict[str, Any]], diagnostics: dict[str, bool] | None = None) -> list[str]:
    total = len(rows)
    if total == 0:
        return ["<span class='badge'>No LLM calls.</span>"]

    errors = sum(1 for r in rows if r["status"] != "OK")
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] >= 0]
    if latencies:
        avg_latency = sum(latencies) / len(latencies)
        p50 = sorted(latencies)[len(latencies) // 2]
        p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1 if len(latencies) > 1 else 0]
    else:
        avg_latency = 0.0
        p50 = 0.0
        p95 = 0.0

    native_requested = sum(1 for r in rows if r["transport_requested"] == "native_local")
    native_used = sum(1 for r in rows if r["transport_used"] == "native_local")
    fallback = sum(1 for r in rows if bool(r["raw"].get("native_fallback_used", False)))
    default_contract = sum(1 for r in rows if bool(r["raw"].get("native_default_contract_used", False)))
    backend_ok = sum(1 for r in rows if isinstance(r["raw"].get("backend_health"), dict) and r["raw"].get("backend_health", {}).get("ok") is True)
    total_checked = sum(1 for r in rows if isinstance(r["raw"].get("backend_health"), dict))
    action_rows = [r for r in rows if r.get("alignment_scope") == "action_expected"]
    non_action = sum(1 for r in rows if r.get("alignment_scope") == "non_action")
    alignment = Counter((r.get("alignment") for r in action_rows))

    diagnostics = diagnostics or {}
    return [
        f"<span class='badge'>LLM calls: {total}</span>",
        f"<span class='badge'>errors: {errors}</span>",
        f"<span class='badge'>latency avg={avg_latency:.1f} p50={p50:.1f} p95={p95:.1f} ms</span>",
        f"<span class='badge'>native requested/used: {native_requested}/{native_used}</span>",
        f"<span class='badge'>fallback ratio: {fallback}/{native_requested if native_requested else 0} ({(fallback / native_requested if native_requested else 0):.1%})</span>",
        f"<span class='badge'>default_contract ratio: {default_contract}/{native_requested if native_requested else 0} ({(default_contract / native_requested if native_requested else 0):.1%})</span>",
        f"<span class='badge'>backend ok: {backend_ok}/{total_checked} ({(backend_ok / total_checked if total_checked else 0):.1%})</span>",
        f"<span class='badge'>alignment match={alignment.get(True, 0)} mismatch={alignment.get(False, 0)} unknown={alignment.get(None, 0)}</span>",
        f"<span class='badge'>non-action phases={non_action}</span>",
        f"<span class='badge'>reflect_input_empty={1 if diagnostics.get('reflect_input_empty', False) else 0}</span>",
        f"<span class='badge'>plan_input_empty={1 if diagnostics.get('plan_input_empty', False) else 0}</span>",
    ]

def _link_observed_and_alignment(rows: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    task_lookup = _collect_task_lookup(events)
    task_state: dict[tuple[str, int], int] = {}

    for row in rows:
        if row.get("alignment_scope") != "action_expected":
            row["observed_task"] = ""
            row["alignment"] = None
            row["alignment_reason"] = "non_action_phase"
            continue
        linked = _link_task(row["raw"], task_lookup=task_lookup, task_state=task_state)
        observed = _extract_observed_task(linked)
        row["observed_task"] = observed
        if not observed:
            row["alignment"] = None
            row["alignment_reason"] = "link_failed"
            continue
        row["alignment"] = _alignment_signal(row["thought"], row["action"], observed)
        row["alignment_reason"] = "token_match" if row["alignment"] is True else "token_mismatch" if row["alignment"] is False else "unverified"

    return rows


def _build_cognition_cards(cognition: dict[str, dict[str, Any]], agent_ids: list[str]) -> str:
    if not cognition:
        return "<p>No cognition snapshot loaded.</p>"

    cards: list[str] = []
    for agent in agent_ids:
        if agent not in cognition:
            continue
        data = cognition[agent]
        beliefs = data.get("beliefs", {}).get("current", {}) if isinstance(data.get("beliefs", {}), dict) else {}
        commitments = data.get("commitments", {}).get("current", {}) if isinstance(data.get("commitments", {}), dict) else {}
        semantic = data.get("semantic", {}).get("current", {}) if isinstance(data.get("semantic", {}), dict) else {}
        facts = data.get("facts", {}) if isinstance(data.get("facts", {}), dict) else {}
        plans = data.get("plans", {}) if isinstance(data.get("plans", {}), dict) else {}
        mailbox = data.get("mailbox", {}) if isinstance(data.get("mailbox", {}), dict) else {}

        watchouts = beliefs.get("watchouts", []) if isinstance(beliefs.get("watchouts"), list) else []
        priority_weights = beliefs.get("priority_weights", beliefs.get("priority_candidates", {}))
        focus_tasks = commitments.get("focus_tasks", []) if isinstance(commitments.get("focus_tasks"), list) else []
        coord_notes = commitments.get("coordination_notes", []) if isinstance(commitments.get("coordination_notes"), list) else []
        heuristics = semantic.get("heuristics", []) if isinstance(semantic.get("heuristics"), list) else []
        anti_patterns = semantic.get("anti_patterns", []) if isinstance(semantic.get("anti_patterns"), list) else []
        specialization = semantic.get("specialization", []) if isinstance(semantic.get("specialization"), list) else []
        queue = facts.get("current_queue", []) if isinstance(facts.get("current_queue", []), list) else []
        mailbox_items = facts.get("current_mailbox", []) if isinstance(facts.get("current_mailbox", []), list) else []
        day_val = beliefs.get("day", beliefs.get("day_idx", 0))

        cards.append(
            f"<article class='agent-card'>"
            f"<h3>{agent}</h3>"
            f"<div class='chip'>day={_safe_int(day_val, 0)}</div>"
            f"<div class='chip'>watchouts={len(watchouts)} queue={len(queue)} mailbox={len(mailbox_items)}</div>"
            f"<div class='chip'>focus_tasks={len(focus_tasks)} heuristics={len(heuristics)} anti_patterns={len(anti_patterns)}</div>"
            f"<div class='chip'>plans={len(plans) if isinstance(plans, dict) else 0}</div>"
            "<div class='kv'><span>watchouts</span>" + f"<p>{_safe_text(_safe_text_list(watchouts), 280)}</p></div>"
            "<div class='kv'><span>priority weights</span>" + f"<p>{_safe_text(priority_weights, 280)}</p></div>"
            "<div class='kv'><span>focus tasks</span>" + f"<p>{_safe_text(_safe_text_list(focus_tasks), 280)}</p></div>"
            "<div class='kv'><span>coordination notes</span>" + f"<p>{_safe_text(_safe_text_list(coord_notes), 280)}</p></div>"
            "<div class='kv'><span>heuristics / anti patterns / specialization</span>"
            f"<p>{_safe_text(_safe_text_list(heuristics), 90)} / {_safe_text(_safe_text_list(anti_patterns), 90)} / {_safe_text(_safe_text_list(specialization), 90)}</p></div>"
            f"<div class='kv'><span>queue</span><p>{_safe_text(_safe_text_list(queue), 280)}</p></div>"
            f"<div class='kv'><span>mailbox</span><p>{_safe_text(_safe_text_list(mailbox_items), 280)}</p></div>"
            f"<div class='kv'><span>rolling summary</span><p>{_safe_text(data.get('rolling_summary', '-'), 380)}</p></div>"
            "</article>"
        )

    return "".join(cards)


def _build_trend_lines(cognition: dict[str, dict[str, Any]], output_dir: Path) -> list[str]:
    definitions = [
        ("beliefs", "watchouts", "Watchouts over days"),
        ("commitments", "focus_tasks", "Focus tasks over days"),
        ("commitments", "coordination_notes", "Coordination notes over days"),
        ("semantic", "heuristics", "Semantic heuristics over days"),
        ("semantic", "anti_patterns", "Semantic anti patterns over days"),
        ("semantic", "specialization", "Semantic specialization over days"),
    ]

    try:
        import plotly.graph_objects as go
    except Exception:
        return ["<p>Plotly is required for trend charts.</p>"]

    outputs: list[str] = []
    all_agents = set(cognition.keys())

    for path_key, field_key, title in definitions:
        traces: dict[str, dict[int, int]] = {}
        for agent, payload in cognition.items():
            section = payload.get(path_key, {}) if isinstance(payload.get(path_key, {}), dict) else {}
            history = section.get("history", {}) if isinstance(section.get("history", {}), dict) else {}
            for day in sorted(history):
                day_payload = history[day]
                traces.setdefault(agent, {})[day] = _safe_count_field(day_payload, field_key)

        if not traces:
            continue

        day_axis = sorted({day for series in traces.values() for day in series})
        if not day_axis:
            continue

        fig = go.Figure()
        for agent in _sort_agent_ids(all_agents & set(traces.keys())):
            values = [traces.get(agent, {}).get(day, 0) for day in day_axis]
            fig.add_trace(go.Scatter(x=[f"D{day}" for day in day_axis], y=values, mode="lines+markers", name=agent))

        fig.update_layout(
            title=title,
            xaxis_title="day",
            yaxis_title="count",
            height=230,
            margin=dict(l=46, r=16, t=46, b=34),
        )
        add_plotly_meta_header(fig, output_dir=output_dir, y_top=1.06)
        outputs.append(fig.to_html(full_html=False, include_plotlyjs=False))

    if not outputs:
        return ["<p>No cognitive trend data.</p>"]
    return outputs


def _build_alignment_chart(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No alignment data.</p>"

    by_day: dict[int, Counter[str]] = defaultdict(Counter)
    for row in rows:
        if row.get("alignment_scope") != "action_expected":
            continue
        day = _safe_int(row.get("day"), 0)
        if day <= 0:
            continue
        value = row.get("alignment")
        if value is True:
            key = "match"
        elif value is False:
            key = "mismatch"
        else:
            key = "unknown"
        by_day[day][key] += 1

    if not by_day:
        return "<p>No day alignment data.</p>"

    days = sorted(by_day)
    labels = [f"D{day}" for day in days]

    try:
        import plotly.graph_objects as go
    except Exception:
        return "<p>Plotly unavailable.</p>"

    fig = go.Figure(
        data=[
            go.Bar(name="match", x=labels, y=[by_day[d]["match"] for d in days], marker_color="#16a34a"),
            go.Bar(name="mismatch", x=labels, y=[by_day[d]["mismatch"] for d in days], marker_color="#dc2626"),
            go.Bar(name="unknown", x=labels, y=[by_day[d]["unknown"] for d in days], marker_color="#64748b"),
        ]
    )
    fig.update_layout(barmode="stack", title="Thought-Action Alignment by Day", xaxis_title="day", yaxis_title="count", height=260, margin=dict(l=48, r=20, t=48, b=36))
    return fig.to_html(full_html=False, include_plotlyjs="inline")


def _build_transport_chart(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No transport data.</p>"

    by_agent: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        agent = str(row.get("agent", "SYSTEM")).upper()
        by_agent[agent][row.get("transport_used", "-")] += 1

    if not by_agent:
        return "<p>No transport data.</p>"

    agents = _sort_agent_ids(set(by_agent.keys()))
    transport_types = sorted({key for c in by_agent.values() for key in c.keys()})

    try:
        import plotly.graph_objects as go
    except Exception:
        return "<p>Plotly unavailable.</p>"

    fig = go.Figure()
    for transport in transport_types:
        fig.add_trace(go.Bar(name=transport, x=agents, y=[by_agent[a][transport] for a in agents]))

    fig.update_layout(title="Transport usage by agent", barmode="stack", height=250, xaxis_title="agent", yaxis_title="calls", margin=dict(l=50, r=20, t=50, b=40))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _build_phase_chart(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No phase data.</p>"

    by_agent_phase: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_agent_phase[row["agent"]][row["phase"]] += 1

    if not by_agent_phase:
        return "<p>No phase data.</p>"

    phases = sorted({row["phase"] for row in rows})
    agents = _sort_agent_ids(set(by_agent_phase.keys()))

    try:
        import plotly.graph_objects as go
    except Exception:
        return "<p>Plotly unavailable.</p>"

    fig = go.Figure()
    for agent in agents:
        fig.add_trace(go.Bar(name=agent, x=phases, y=[by_agent_phase[agent][phase] for phase in phases]))

    fig.update_layout(barmode="stack", title="LLM Call Phases by Agent", xaxis_title="phase", yaxis_title="count", height=250, margin=dict(l=46, r=20, t=48, b=38))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def _build_reasoning_lanes(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "<p>No calls.</p>"

    by_agent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_agent[row["agent"]].append(row)

    lane_blocks: list[str] = []
    for agent in _sort_agent_ids(set(by_agent.keys())):
        steps: list[str] = []
        for row in by_agent[agent]:
            align = row.get("alignment")
            align_cls = "unknown"
            align_txt = "UNKNOWN"
            if row.get("alignment_scope") == "non_action":
                align_txt = "NON_ACTION"
            elif align is True:
                align_cls = "match"
                align_txt = "MATCH"
            elif align is False:
                align_cls = "mismatch"
                align_txt = "MISMATCH"

            status = row["status"]
            status_cls = "ok" if status == "OK" else "err" if status == "ERROR" else "warn"
            steps.append(
                "\n".join(
                    [
                        "<details class='lane-step'>",
                        f"<summary><span class='pill {align_cls}'>{align_txt}</span> "
                        f"<span class='pill {status_cls}'>{row['status']}</span>"
                        f" {row['call_id']} | Day {row['day']} R{row['round']} | {row['phase']} | {row['latency_ms']:.1f}ms</summary>",
                        "<div class='lane-body'>",
                        f"<div class='kv'><span>Thought</span><p>{escape(row['thought'])}</p></div>",
                        f"<div class='kv'><span>Action</span><p>{escape(row['action'])}</p></div>",
                        f"<div class='kv'><span>Observed task</span><p>{escape(_safe_text(row.get('observed_task', '-'), 180))}</p></div>",
                        f"<div class='kv'><span>Alignment reason</span><p>{escape(_safe_text(row.get('alignment_reason', '-'), 180))}</p></div>",
                        f"<div class='kv'><span>Reason</span><p>{escape(row['reason'])}</p></div>",
                        f"<div class='kv'><span>Prompt</span><p>{escape(row['prompt'])}</p></div>",
                        f"<div class='kv'><span>Transport</span><p>{escape(row['transport_requested'])} -> {escape(row['transport_used'])}</p></div>",
                        f"<div class='kv'><span>Response</span><p>{escape(row['response'])}</p></div>",
                        f"<div class='kv'><span>Error</span><p>{escape(row['error'])}</p></div>",
                        "</div>",
                        "</details>",
                    ]
                )
            )
        lane_blocks.append(f"<section class='agent-lane'><h3>{agent}</h3>" + ''.join(steps) + "</section>")

    return "\n".join(lane_blocks)


def _build_mismatch_list(rows: list[dict[str, Any]]) -> str:
    mismatches = [r for r in rows if r.get("alignment") is False]
    if not mismatches:
        return "<p>No mismatches.</p>"

    lines: list[str] = []
    for row in mismatches[:60]:
        lines.append(
            f"<details class='lane-step'><summary>[{row['call_id']}] {row['agent']} D{row['day']} {row['phase']}</summary>"
            f"<div class='kv'><span>Thought</span><p>{escape(row['thought'])}</p></div>"
            f"<div class='kv'><span>Action</span><p>{escape(row['action'])}</p></div>"
            f"<div class='kv'><span>Observed</span><p>{escape(_safe_text(row.get('observed_task', '-'), 160))}</p></div>"
            f"<div class='kv'><span>Reason</span><p>{escape(row['reason'])}</p></div>"
            "</details>"
        )
    return "<div class='mismatch'>{}</div>".format("\n".join(lines))


def _build_summary_by_day(daily_summary: list[dict[str, Any]]) -> str:
    if not daily_summary:
        return "<li>No daily summary.</li>"

    out = []
    for entry in daily_summary:
        if not isinstance(entry, dict):
            continue
        day = _safe_int(entry.get("day", 0), 0)
        products = _safe_int(entry.get("products", 0), 0)
        scraps = _safe_int(entry.get("scrap", 0), 0)
        completed = _safe_int(entry.get("station1_completions", 0), 0) + _safe_int(entry.get("station2_completions", 0), 0)
        out.append(f"<li>Day {day}: products={products}, scrap={scraps}, station1+2 completed={completed}</li>")
    if not out:
        out = ["<li>No daily summary.</li>"]
    return "".join(out)


def _collect_plan_snapshots(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for ev in events:
        if not isinstance(ev, dict) or str(ev.get("type", "")).strip() != "PHASE_JOB_ASSIGNMENT":
            continue
        day = _safe_int(ev.get("day"), 0)
        details = ev.get("details", {}) if isinstance(ev.get("details", {}), dict) else {}
        if day <= 0 or not isinstance(details, dict):
            continue
        snapshots.append({"day": day, "details": details})
    snapshots.sort(key=lambda item: item["day"])
    return snapshots


def _format_weight_delta_items(weights: dict[str, Any], limit: int = 5) -> list[str]:
    if not isinstance(weights, dict):
        return []
    ranked: list[tuple[str, float]] = []
    for key, value in weights.items():
        weight = _safe_float(value, 1.0)
        if abs(weight - 1.0) < 1e-6:
            continue
        ranked.append((str(key), weight))
    ranked.sort(key=lambda item: abs(item[1] - 1.0), reverse=True)
    return [f"{key}={weight:.2f}" for key, weight in ranked[:limit]]


def _format_agent_multiplier_summary(multipliers: dict[str, Any], limit: int = 3) -> str:
    if not isinstance(multipliers, dict):
        return "-"
    parts: list[str] = []
    for agent in _sort_agent_ids(set(multipliers.keys())):
        row = multipliers.get(agent, {}) if isinstance(multipliers.get(agent, {}), dict) else {}
        changed = _format_weight_delta_items(row, limit=limit)
        parts.append(f"{agent}: {', '.join(changed) if changed else 'neutral'}")
    return " | ".join(parts) if parts else "-"


def _format_queue_summary(queues: dict[str, Any]) -> str:
    if not isinstance(queues, dict):
        return "-"
    parts: list[str] = []
    for agent in _sort_agent_ids(set(queues.keys())):
        items = queues.get(agent, []) if isinstance(queues.get(agent, []), list) else []
        compact = []
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            task_family = str(item.get("task_family", "")).strip() or "-"
            target = str(item.get("target_id", item.get("target_station", ""))).strip()
            compact.append(task_family if not target else f"{task_family}->{target}")
        parts.append(f"{agent}({len(items)}): {', '.join(compact) if compact else '-'}")
    return " | ".join(parts) if parts else "-"


def _format_mailbox_summary(mailbox: dict[str, Any]) -> str:
    if not isinstance(mailbox, dict):
        return "-"
    parts: list[str] = []
    for agent in _sort_agent_ids(set(mailbox.keys())):
        items = mailbox.get(agent, []) if isinstance(mailbox.get(agent, []), list) else []
        compact = []
        for item in items[:3]:
            if not isinstance(item, dict):
                continue
            msg_type = str(item.get("message_type", "coordination")).strip() or "coordination"
            task_family = str(item.get("task_family", "")).strip()
            compact.append(msg_type if not task_family else f"{msg_type}:{task_family}")
        parts.append(f"{agent}({len(items)}): {', '.join(compact) if compact else '-'}")
    return " | ".join(parts) if parts else "-"


def _build_plan_ledger(snapshots: list[dict[str, Any]]) -> str:
    if not snapshots:
        return "<p>No manager plan snapshots.</p>"

    cards: list[str] = []
    for snap in snapshots:
        day = _safe_int(snap.get("day"), 0)
        details = snap.get("details", {}) if isinstance(snap.get("details", {}), dict) else {}
        weights = details.get("shared_task_priority_weights", details.get("task_priority_weights", {})) if isinstance(details.get("shared_task_priority_weights", details.get("task_priority_weights", {})), dict) else {}
        multipliers = details.get("agent_priority_multipliers", {}) if isinstance(details.get("agent_priority_multipliers", {}), dict) else {}
        queues = details.get("personal_queues", {}) if isinstance(details.get("personal_queues", {}), dict) else {}
        mailbox = details.get("mailbox", {}) if isinstance(details.get("mailbox", {}), dict) else {}
        reason_trace = details.get("reason_trace", []) if isinstance(details.get("reason_trace", []), list) else []
        summary = _safe_text(details.get("manager_summary", ""), 400)
        reason_lines = []
        for entry in reason_trace[:5]:
            if not isinstance(entry, dict):
                continue
            reason = _safe_text(entry.get("reason", ""), 220)
            evidence = ", ".join(str(item) for item in entry.get("evidence", [])[:4]) if isinstance(entry.get("evidence", []), list) else ""
            reason_lines.append(f"<li><strong>{escape(reason)}</strong><br><span class='muted'>{escape(evidence or '-')}</span></li>")
        cards.append(
            "".join(
                [
                    "<article class='agent-card'>",
                    f"<h3>Day {day} plan</h3>",
                    f"<div class='chip'>queue_agents={sum(1 for v in queues.values() if isinstance(v, list) and v)}</div>",
                    f"<div class='chip'>mailbox_agents={sum(1 for v in mailbox.values() if isinstance(v, list) and v)}</div>",
                    f"<div class='chip'>reason_items={len(reason_trace)}</div>",
                    f"<div class='kv'><span>manager summary</span><p>{escape(summary)}</p></div>",
                    f"<div class='kv'><span>task priority weights</span><p>{escape(', '.join(_format_weight_delta_items(weights, limit=8)) or 'all neutral')}</p></div>",
                    f"<div class='kv'><span>agent priority multipliers</span><p>{escape(_format_agent_multiplier_summary(multipliers))}</p></div>",
                    f"<div class='kv'><span>personal queues</span><p>{escape(_format_queue_summary(queues))}</p></div>",
                    f"<div class='kv'><span>mailbox</span><p>{escape(_format_mailbox_summary(mailbox))}</p></div>",
                    f"<div class='kv'><span>reason trace</span><ul>{''.join(reason_lines) if reason_lines else '<li>-</li>'}</ul></div>",
                    "</article>",
                ]
            )
        )
    return "".join(cards)


def export_orchestration_intelligence_dashboard(
    *,
    output_dir: Path,
    daily_summary: list[dict[str, Any]],
    llm_records: list[dict[str, Any]],
) -> Path | None:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "orchestration_intelligence_dashboard.html"

    events = _load_events(output_dir)
    meta = load_artifact_meta(output_dir)
    workspace_root = _load_workspace_root(output_dir)
    agent_ids = _collect_agent_ids(records=llm_records if isinstance(llm_records, list) else [], events=events, workspace_root=workspace_root)

    rows = _normalize_llm_records(records=llm_records if isinstance(llm_records, list) else [])
    rows = _link_observed_and_alignment(rows, events)

    cognition = _load_cognition(agent_ids=agent_ids, workspace_root=workspace_root)
    diagnostics = _runtime_workspace_diagnostics(output_dir)
    plan_ledger_html = _build_plan_ledger(_collect_plan_snapshots(events))
    cognition_cards_html = _build_cognition_cards(cognition, agent_ids)
    trend_html = _build_trend_lines(cognition, output_dir)
    summary_cards = _build_summary_cards(rows, diagnostics=diagnostics)

    lane_html = _build_reasoning_lanes(rows)
    mismatch_html = _build_mismatch_list(rows)
    alignment_html = _build_alignment_chart(rows)
    transport_html = _build_transport_chart(rows)
    phase_html = _build_phase_chart(rows)

    daily_html = _build_summary_by_day(daily_summary) if isinstance(daily_summary, list) else ["<li>No daily summary.</li>"]

    if not isinstance(daily_html, str):
        daily_html = "".join(daily_html)

    trends_block = "".join(f"<section class='trend'>{chart}</section>" for chart in trend_html)

    html = "\n".join(
        [
            "<!doctype html>",
            "<html lang='en'>",
            "<head>",
            "<meta charset='utf-8' />",
            "<meta name='viewport' content='width=device-width, initial-scale=1' />",
            "<title>Orchestration Intelligence Dashboard</title>",
            "<style>",
            "body{margin:18px;font-family:Inter,'Noto Sans KR',Arial,sans-serif;background:#eef2f7;color:#0f172a;line-height:1.35;}",
            ".summary{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0;}",
            ".badge{background:#e2e8f0;border:1px solid #cbd5e1;border-radius:999px;padding:5px 9px;font-size:12px;}",
            ".layout{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));}",
            ".card{background:#fff;border:1px solid #d5e2ee;border-radius:12px;padding:10px;box-shadow:0 8px 20px rgba(15,23,42,.05);}",
            ".agent-grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));}",
            ".agent-card{background:#f8fafc;border:1px solid #dbeafe;border-left:4px solid #1d4ed8;border-radius:9px;padding:8px;}",
            ".chip{display:inline-block;font-size:11px;margin:3px 4px 3px 0;padding:2px 7px;background:#eff6ff;color:#1e293b;border:1px solid #bfdbfe;border-radius:999px;}",
            ".kv{margin:8px 0;}",
            ".kv span{font-size:11px;font-weight:700;color:#334155;display:block;}",
            ".kv p{margin:4px 0 0;color:#334155;white-space:pre-wrap;word-break:break-word;}",
            ".lane{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px;}",
            ".agent-lane{background:#fff;border:1px solid #d1d5db;border-radius:10px;padding:8px;border-left:4px solid #1d4ed8;}",
            ".lane-step{margin:7px 0;padding:5px;border:1px solid #e2e8f0;border-radius:8px;background:#fff;}",
            ".lane-step summary{font-weight:700;cursor:pointer;font-size:13px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;}",
            ".lane-body{margin-top:6px;}",
            ".pill{display:inline-block;padding:2px 7px;border-radius:999px;font-size:11px}",
            ".match{background:#dcfce7;color:#14532d}",
            ".mismatch{background:#fee2e2;color:#7f1d1d}",
            ".unknown{background:#f1f5f9;color:#334155}",
            ".ok{background:#dcfce7;color:#14532d}",
            ".err{background:#fee2e2;color:#7f1d1d}",
            ".warn{background:#f1f5f9;color:#334155}",
            ".trend{margin-bottom:12px;}",
            "h1,h2,h3{margin:8px 0;}",
            ".muted{color:#64748b;font-size:12px;}",
            ".mismatch{margin-top:8px;max-height:260px;overflow:auto;}",
            "</style>",
            "</head>",
            "<body>",
            "<h1>Orchestration Intelligence Dashboard</h1>",
            f"<p><strong>{escape(format_run_mode_line(meta))}</strong></p>",
            f"<p class='muted'>events.jsonl: {escape(str(meta.get('events_path', '-')))}</p>",
            "<div class='summary'>" + "".join(summary_cards) + "</div>",
            "<div class='layout'>",
            f"<section class='card'><h2>Summary by day</h2><ul>{daily_html}</ul></section>",
            f"<section class='card'><h2>Alignment ratio</h2>{alignment_html}</section>",
            f"<section class='card'><h2>Phase mix</h2>{phase_html}</section>",
            f"<section class='card'><h2>Transport by agent</h2>{transport_html}</section>",
            "</div>",
            "<h2>Manager Plan Ledger</h2>",
            f"<section class='agent-grid'>" + plan_ledger_html + "</section>",
            "<h2>Agent cognition snapshot</h2>",
            f"<section class='agent-grid'>" + (cognition_cards_html if cognition_cards_html else "<p>No cognition.</p>") + "</section>",
            "<h2>Beliefs / Commitments / Semantic trends</h2>",
            f"<div class='layout'>{trends_block}</div>",
            "<h2>LLM thought -> action lanes (by agent)</h2>",
            f"<div class='lane'>{lane_html}</div>",
            "<h2>Alignment mismatches</h2>",
            mismatch_html,
            "<p class='muted'>generated: " + _safe_iso(meta.get("wall_clock_human")) + "</p>",
            "</body>",
            "</html>",
        ]
    )

    path.write_text(html, encoding="utf-8")
    return path






