from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event as ThreadEvent, Lock, Thread
from time import perf_counter
from typing import Any

import simpy

from manufacturing_sim.simulation.scenarios.manufacturing.decision.llm_common import OptionalLLMDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.decision.openclaw_orchestrated import OpenClawOrchestratedDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.decision.modes import is_llm_mode, normalize_decision_mode
from manufacturing_sim.simulation.scenarios.manufacturing.decision.scripted import ScriptedDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.viz.orchestration_intelligence_dashboard import export_orchestration_intelligence_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.dashboard import export_kpi_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.gantt import export_gantt
from manufacturing_sim.simulation.scenarios.manufacturing.viz.llm_trace import export_llm_trace_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.openclaw_workspace_dashboard import export_openclaw_workspace_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.task_priority_dashboard import (
    export_task_priority_dashboard,
)
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _write_progress(output_root: Path, payload: dict[str, Any]) -> None:
    progress_path = output_root / "progress.json"
    progress_path.write_text(__import__("json").dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")



def _safe_float_val(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if q <= 0:
        return values[0]
    if q >= 1:
        return values[-1]
    ordered = sorted(values)
    idx = (len(ordered) - 1) * q
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return float(ordered[lo])
    frac = idx - lo
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def _latency_stats_ms(rows: list[dict[str, Any]]) -> dict[str, float]:
    latencies = [_safe_float_val(rec.get("latency_ms"), 0.0) for rec in rows if _safe_float_val(rec.get("latency_ms"), 0.0) >= 0]
    if not latencies:
        return {
            "count": 0,
            "p50_ms": 0.0,
            "p90_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "mean_ms": 0.0,
            "min_ms": 0.0,
            "max_ms": 0.0,
        }

    ordered = sorted(latencies)
    count = len(ordered)
    return {
        "count": count,
        "p50_ms": round(_quantile(ordered, 0.50), 3),
        "p90_ms": round(_quantile(ordered, 0.90), 3),
        "p95_ms": round(_quantile(ordered, 0.95), 3),
        "p99_ms": round(_quantile(ordered, 0.99), 3),
        "mean_ms": round(sum(ordered) / float(count), 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
    }


def _summarize_llm_transport_metrics(records: list[dict[str, Any]] | None) -> dict[str, Any]:
    rows = records if isinstance(records, list) else []
    total_calls = len(rows)
    requested_native = sum(1 for rec in rows if str(rec.get("transport_requested", "")).strip() == "native_local")
    used_native = sum(1 for rec in rows if str(rec.get("transport_used", "")).strip() == "native_local")
    used_chat = sum(1 for rec in rows if str(rec.get("transport_used", "")).strip() == "chat_compat")
    fallback_count = sum(1 for rec in rows if bool(rec.get("native_fallback_used", False)))
    default_contract_count = sum(1 for rec in rows if bool(rec.get("native_default_contract_used", False)) or "native_default_contract_used" in str(rec.get("error", "")))

    backend_checked = 0
    backend_healthy = 0
    backend_failed = 0
    by_phase: dict[str, dict[str, Any]] = {}

    for rec in rows:
        ctx = rec.get("context", {}) if isinstance(rec.get("context", {}), dict) else {}
        phase = str(ctx.get("phase", rec.get("call_name", "llm_call"))).strip() or str(rec.get("call_name", "llm_call")).strip() or "llm_call"

        phase_entry = by_phase.setdefault(
            phase,
            {
                "calls": 0,
                "requested_native": 0,
                "used_native": 0,
                "used_chat": 0,
                "native_fallback_count": 0,
                "native_default_contract_count": 0,
                "latencies_ms": [],
                "attempt_counts": [],
                "backend_health_ok": 0,
                "backend_health_checked": 0,
            },
        )
        phase_entry["calls"] += 1
        if str(rec.get("transport_requested", "")).strip() == "native_local":
            phase_entry["requested_native"] += 1
        if str(rec.get("transport_used", "")).strip() == "native_local":
            phase_entry["used_native"] += 1
        if str(rec.get("transport_used", "")).strip() == "chat_compat":
            phase_entry["used_chat"] += 1
        if bool(rec.get("native_fallback_used", False)):
            phase_entry["native_fallback_count"] += 1
        if bool(rec.get("native_default_contract_used", False)) or "native_default_contract_used" in str(rec.get("error", "")):
            phase_entry["native_default_contract_count"] += 1

        latency_ms = _safe_float_val(rec.get("latency_ms"), -1.0)
        if latency_ms >= 0:
            phase_entry["latencies_ms"].append(latency_ms)

        attempt_count = _safe_float_val(rec.get("attempt_count", 0), 0.0)
        phase_entry["attempt_counts"].append(attempt_count)

        backend_raw = rec.get("backend_health", None)
        backend = backend_raw if isinstance(backend_raw, dict) else None
        backend_checked_flag = isinstance(backend, dict) and (
            "ok" in backend or "checked_at" in backend or "gateway" in backend or "backend" in backend
        )
        if backend_checked_flag:
            phase_entry["backend_health_checked"] += 1
            backend_checked += 1
            if bool(backend.get("ok", False)):
                phase_entry["backend_health_ok"] += 1
                backend_healthy += 1
            else:
                backend_failed += 1

    for phase_entry in by_phase.values():
        denom = int(phase_entry.get("requested_native", 0) or 0)
        phase_entry["native_fallback_ratio"] = round((float(phase_entry.get("native_fallback_count", 0) or 0) / denom), 6) if denom > 0 else 0.0
        phase_entry["native_default_contract_ratio"] = round((float(phase_entry.get("native_default_contract_count", 0) or 0) / denom), 6) if denom > 0 else 0.0

        lat_rows = [{"latency_ms": x} for x in phase_entry.pop("latencies_ms", [])]
        phase_entry["latency_stats_ms"] = _latency_stats_ms(lat_rows)
        attempts = [float(a) for a in phase_entry.pop("attempt_counts", []) if float(a) >= 0]
        phase_entry["avg_attempts"] = round((sum(attempts) / float(len(attempts))), 6) if attempts else 0.0
        checked = phase_entry.get("backend_health_checked", 0) or 0
        phase_entry["backend_health_ok_ratio"] = round((float(phase_entry.get("backend_health_ok", 0) or 0) / checked), 6) if checked > 0 else 0.0

    all_lat_rows = [{"latency_ms": _safe_float_val(rec.get("latency_ms"), -1.0)} for rec in rows if _safe_float_val(rec.get("latency_ms"), -1.0) >= 0]
    latency_stats = _latency_stats_ms(all_lat_rows)
    attempts = [float(_safe_float_val(rec.get("attempt_count", 0), 0.0)) for rec in rows]
    attempts = [x for x in attempts if x >= 0]
    avg_attempts = round((sum(attempts) / float(len(attempts))), 6) if attempts else 0.0

    return {
        "total_calls": total_calls,
        "requested_native_local": requested_native,
        "used_native_local": used_native,
        "used_chat_compat": used_chat,
        "native_fallback_count": fallback_count,
        "native_fallback_ratio": round((float(fallback_count) / float(requested_native)), 6) if requested_native > 0 else 0.0,
        "native_default_contract_count": default_contract_count,
        "native_default_contract_ratio": round((float(default_contract_count) / float(requested_native)), 6) if requested_native > 0 else 0.0,
        "latency_stats_ms": latency_stats,
        "avg_attempts": avg_attempts,
        "backend_health_checked": backend_checked,
        "backend_health_ok": backend_healthy,
        "backend_health_failed": backend_failed,
        "backend_health_ok_ratio": round((float(backend_healthy) / float(backend_checked)), 6) if backend_checked > 0 else 0.0,
        "by_phase": by_phase,
    }

def _build_progress_payload(
    *,
    status: str,
    decision_mode: str,
    started_at_utc: str,
    total_days: int,
    current_day: int,
    sim_time_min: float,
    sim_total_min: float,
    elapsed_wall_sec: float,
    output_root: Path,
    message: str,
    finished_at_utc: str | None = None,
) -> dict[str, Any]:
    progress_ratio = 0.0 if sim_total_min <= 0 else min(1.0, max(0.0, float(sim_time_min) / float(sim_total_min)))
    return {
        "status": status,
        "decision_mode": decision_mode,
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc or "",
        "elapsed_wall_sec": round(float(elapsed_wall_sec), 3),
        "elapsed_wall_human": _format_duration(elapsed_wall_sec),
        "current_day": int(current_day),
        "total_days": int(total_days),
        "sim_time_min": round(float(sim_time_min), 3),
        "sim_total_min": round(float(sim_total_min), 3),
        "progress_ratio": round(progress_ratio, 6),
        "progress_percent": round(progress_ratio * 100.0, 2),
        "events_path": str((output_root / "events.jsonl").resolve()),
        "message": message,
    }


def _emit_progress(output_root: Path, payload: dict[str, Any]) -> None:
    _write_progress(output_root, payload)
    print(
        "[progress] "
        f"status={payload['status']} | "
        f"day={payload['current_day']}/{payload['total_days']} | "
        f"sim={payload['sim_time_min']:.0f}/{payload['sim_total_min']:.0f} min | "
        f"{payload['progress_percent']:.1f}% | "
        f"elapsed={payload['elapsed_wall_human']} | "
        f"{payload['message']}",
        flush=True,
    )


def run(
    experiment_cfg: dict[str, Any],
    logger: EventLogger | None = None,
    decision_modules: Any | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """제조 시뮬레이션 한 번을 실행하고 결과 아티팩트를 내보낸다."""

    output_root = Path(output_dir or Path.cwd() / "outputs")
    output_root.mkdir(parents=True, exist_ok=True)

    event_logger = logger or EventLogger(output_root)

    decision_cfg = experiment_cfg.get("decision", {}) if isinstance(experiment_cfg.get("decision", {}), dict) else {}
    decision_mode = normalize_decision_mode(str(decision_cfg.get("mode", "adaptive_priority")))

    total_days = int(experiment_cfg["horizon"]["num_days"])
    sim_total_min = float(total_days) * float(experiment_cfg["horizon"].get("minutes_per_day", 0))
    wall_clock_started = perf_counter()
    started_at_utc = _utc_now_iso()
    urgent_cfg = decision_cfg.get("urgent_discuss", {}) if isinstance(decision_cfg.get("urgent_discuss", {}), dict) else {}
    series_cfg = experiment_cfg.get("_run_series", {}) if isinstance(experiment_cfg.get("_run_series", {}), dict) else {}
    run_index = max(1, int(series_cfg.get("run_index", 1) or 1))
    total_runs = max(run_index, int(series_cfg.get("total_runs", 1) or 1))
    knowledge_in_path = str(series_cfg.get("knowledge_path", str((output_root / "knowledge.md").resolve()))).strip()

    run_meta: dict[str, Any] = {
        "decision_mode": decision_mode,
        "run_index": run_index,
        "total_runs": total_runs,
        "knowledge_in_path": knowledge_in_path,
        "knowledge_out_path": "",
        "started_at_utc": started_at_utc,
        "finished_at_utc": "",
        "wall_clock_sec": 0.0,
        "wall_clock_human": "0s",
        "progress_path": str((output_root / "progress.json").resolve()),
        "urgent_discuss_enabled": bool(urgent_cfg.get("enabled", True)),
    }

    if decision_modules is not None:
        decision_module = decision_modules
    else:
        if decision_mode in {"adaptive_priority", "fixed_priority"}:
            decision_module = ScriptedDecisionModule(experiment_cfg)
        elif decision_mode == "llm_planner":
            # ?? LLM ??? OpenClaw ??? ??? ????.
            # OpenClaw? ?? ?? ????? OptionalLLMDecisionModule? fallback?? ????.
            try:
                llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
                provider = str(llm_cfg.get("provider", "")).strip().lower()
                orchestration_cfg = llm_cfg.get("orchestration", {}) if isinstance(llm_cfg.get("orchestration", {}), dict) else {}
                if provider == "openclaw" and bool(orchestration_cfg.get("enabled", True)):
                    decision_module = OpenClawOrchestratedDecisionModule(
                        cfg=experiment_cfg,
                        llm_cfg=llm_cfg,
                    )
                else:
                    decision_module = OptionalLLMDecisionModule(
                        cfg=experiment_cfg,
                        llm_cfg=llm_cfg,
                    )
            except RuntimeError as exc:
                raise RuntimeError(
                    "decision.mode=llm_planner is selected, but the configured LLM backend is unavailable. "
                    "Use decision=adaptive_priority for a local rule-based run or configure the LLM server."
                ) from exc
        else:
            raise ValueError(f"Unsupported decision mode: {decision_mode}")

    if is_llm_mode(decision_mode):
        llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
        provider = str(llm_cfg.get("provider", "")).strip().lower()
        comm_cfg = llm_cfg.get("communication", {}) if isinstance(llm_cfg.get("communication", {}), dict) else {}
        openclaw_cfg = llm_cfg.get("openclaw", {}) if isinstance(llm_cfg.get("openclaw", {}), dict) else {}
        orchestration_cfg = llm_cfg.get("orchestration", {}) if isinstance(llm_cfg.get("orchestration", {}), dict) else {}
        evaluator_cfg = orchestration_cfg.get("evaluator", {}) if isinstance(orchestration_cfg.get("evaluator", {}), dict) else {}
        llm_language = str(llm_cfg.get("language", comm_cfg.get("language", "ENG"))).strip().upper() or "ENG"
        run_meta["llm"] = {
            "mode_variant": decision_mode,
            "provider": str(llm_cfg.get("provider", "")),
            "server_url": str(llm_cfg.get("server_url", "")),
            "model": str(llm_cfg.get("model", "")),
            "language": llm_language,
            "communication_enabled": bool(comm_cfg.get("enabled", True)),
            "coordination_review_enabled": bool(orchestration_cfg.get("daily_review_enabled", True)) if provider == "openclaw" else bool(comm_cfg.get("enabled", True)),
            "communication_rounds": int(comm_cfg.get("rounds", 0)),
            "communication_language": llm_language,
            "urgent_discuss_enabled": bool(urgent_cfg.get("enabled", True)),
            "evaluator_enabled": bool(evaluator_cfg.get("enabled", False)) if provider == "openclaw" else False,
            "evaluator_max_revision_requests": int(evaluator_cfg.get("max_revision_requests", 2) or 2) if provider == "openclaw" else 0,
        }
        if str(llm_cfg.get("provider", "")).strip().lower() == "openclaw":
            backend_cfg = openclaw_cfg.get("backend", {}) if isinstance(openclaw_cfg.get("backend", {}), dict) else {}
            run_meta["llm"]["openclaw"] = {
                "gateway_url": str(openclaw_cfg.get("gateway_url", llm_cfg.get("gateway_url", llm_cfg.get("server_url", "")))),
                "profile_name": str(openclaw_cfg.get("profile_name", "mansim_repo")),
                "profile_config_path": str(openclaw_cfg.get("profile_config_path", "")),
                "session_namespace": str(openclaw_cfg.get("session_namespace", "mansim")),
                "manager_agent_id": str(openclaw_cfg.get("manager_agent_id", "MANAGER")),
                "worker_agent_ids": list(openclaw_cfg.get("worker_agent_ids", [])) if isinstance(openclaw_cfg.get("worker_agent_ids", []), list) else [],
                "workspace_root": str(openclaw_cfg.get("workspace_root", "openclaw/workspaces")),
                "backend": {
                    "provider": str(backend_cfg.get("provider", "")),
                    "model": str(backend_cfg.get("model", "")),
                    "model_name": str(backend_cfg.get("model_name", "")),
                    "base_url": str(backend_cfg.get("base_url", "")),
                    "api": str(backend_cfg.get("api", "")),
                    "context_window": int(backend_cfg.get("context_window", 0) or 0),
                    "max_output_tokens": int(backend_cfg.get("max_output_tokens", 0) or 0),
                    "reasoning": bool(backend_cfg.get("reasoning", False)),
                    "api_key_configured": bool(str(backend_cfg.get("api_key", "")).strip()),
                },
            }

    if hasattr(decision_module, "prepare_run_context"):
        prepare_fn = getattr(decision_module, "prepare_run_context")
        if callable(prepare_fn):
            runtime_info = prepare_fn(output_root)
            if isinstance(runtime_info, dict) and is_llm_mode(decision_mode):
                run_meta.setdefault("llm", {})
                if str(run_meta["llm"].get("provider", "")).strip().lower() == "openclaw":
                    run_meta["llm"].setdefault("openclaw", {})
                    run_meta["llm"]["openclaw"]["runtime"] = runtime_info
                    client = getattr(decision_module, "openclaw_client", None)
                    client_backend = getattr(client, "backend", None)
                    if isinstance(client_backend, dict):
                        backend_meta = run_meta["llm"]["openclaw"].setdefault("backend", {})
                        backend_meta["effective_base_url"] = str(client_backend.get("base_url", backend_meta.get("base_url", "")))
                        if bool(client_backend.get("resolved_via_local_proxy", False)):
                            backend_meta["resolved_via_local_proxy"] = True
                        if str(client_backend.get("resolved_wsl_distro", "")).strip():
                            backend_meta["resolved_wsl_distro"] = str(client_backend.get("resolved_wsl_distro", "")).strip()
                        if str(client_backend.get("resolved_wsl_ipv4", "")).strip():
                            backend_meta["resolved_wsl_ipv4"] = str(client_backend.get("resolved_wsl_ipv4", "")).strip()

    env = simpy.Environment()
    world = ManufacturingWorld(env=env, cfg=experiment_cfg, logger=event_logger, decision_module=decision_module)
    world.bootstrap()

    progress_lock = Lock()
    progress_stop = ThreadEvent()
    progress_state: dict[str, Any] = {
        "status": "starting",
        "message": "bootstrapped world",
        "current_day": 0,
        "finished_at_utc": "",
    }

    def _set_progress_state(*, status: str, message: str, current_day: int, finished_at_utc: str = "") -> None:
        with progress_lock:
            progress_state["status"] = status
            progress_state["message"] = message
            progress_state["current_day"] = int(current_day)
            progress_state["finished_at_utc"] = finished_at_utc

    def _emit_run_progress(
        *,
        status: str,
        message: str,
        current_day: int,
        finished_at_utc: str = "",
        log_line: bool = True,
    ) -> None:
        _set_progress_state(
            status=status,
            message=message,
            current_day=current_day,
            finished_at_utc=finished_at_utc,
        )
        payload = _build_progress_payload(
            status=status,
            decision_mode=decision_mode,
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc or None,
            total_days=total_days,
            current_day=current_day,
            sim_time_min=env.now,
            sim_total_min=sim_total_min,
            elapsed_wall_sec=perf_counter() - wall_clock_started,
            output_root=output_root,
            message=message,
        )
        if log_line:
            _emit_progress(output_root, payload)
        else:
            _write_progress(output_root, payload)

    def _progress_monitor() -> None:
        while not progress_stop.wait(2.0):
            with progress_lock:
                status = str(progress_state.get("status", "running"))
                message = str(progress_state.get("message", "running"))
                current_day = int(progress_state.get("current_day", 0))
                finished_at_utc = str(progress_state.get("finished_at_utc", ""))
            _write_progress(
                output_root,
                _build_progress_payload(
                    status=status,
                    decision_mode=decision_mode,
                    started_at_utc=started_at_utc,
                    finished_at_utc=finished_at_utc or None,
                    total_days=total_days,
                    current_day=current_day,
                    sim_time_min=env.now,
                    sim_total_min=sim_total_min,
                    elapsed_wall_sec=perf_counter() - wall_clock_started,
                    output_root=output_root,
                    message=message,
                ),
            )

    _emit_run_progress(status="starting", message="bootstrapped world", current_day=0)
    progress_thread = Thread(target=_progress_monitor, name="mansim-progress-monitor", daemon=True)
    progress_thread.start()

    last_summary: dict[str, Any] | None = None
    llm_trace_path: str = ""
    dashboard_path: Path | None = None
    task_priority_dashboard_path: Path | None = None
    orchestration_intelligence_dashboard_path: Path | None = None
    gantt_path = output_root / "gantt.html"
    artifact_status: dict[str, Any] = {"generated": {}, "errors": {}}
    run_reflection_info: dict[str, Any] = {}

    try:
        for day in range(1, total_days + 1):
            if world.terminated:
                break

            _emit_run_progress(status="running", message=f"starting day {day}", current_day=day)

            observation = world.build_observation(last_summary)
            strategy = decision_module.reflect(observation)
            job_plan = decision_module.propose_jobs(observation, strategy, world.norms)
            world.start_day(day, strategy, job_plan)

            day_end = day * world.minutes_per_day
            if env.now < day_end and not world.terminated:
                stop_event = env.any_of([world.termination_event, env.timeout(day_end - env.now)])
                env.run(until=stop_event)

            day_summary = world.finalize_day(day)
            if world.terminated:
                event_logger.log(
                    t=env.now,
                    day=day,
                    event_type="PHASE_TERMINATED",
                    entity_id="system",
                    location="Factory",
                    details={"reason": world.termination_reason},
                )
            else:
                world.norms = decision_module.discuss(day_summary, world.norms)
                discussion_trace: list[dict[str, Any]] = []
                if hasattr(decision_module, "consume_last_discussion_trace"):
                    consume_fn = getattr(decision_module, "consume_last_discussion_trace")
                    if callable(consume_fn):
                        maybe_trace = consume_fn()
                        if isinstance(maybe_trace, list):
                            discussion_trace = maybe_trace
                comm_enabled: bool | None = None
                if hasattr(decision_module, "is_communication_enabled"):
                    comm_fn = getattr(decision_module, "is_communication_enabled")
                    if callable(comm_fn):
                        try:
                            comm_enabled = bool(comm_fn())
                        except Exception:
                            comm_enabled = None
                agent_priority_update_trace: dict[str, Any] = {}
                if hasattr(decision_module, "consume_last_agent_priority_update_trace"):
                    consume_profile_fn = getattr(decision_module, "consume_last_agent_priority_update_trace")
                    if callable(consume_profile_fn):
                        maybe_trace = consume_profile_fn()
                        if isinstance(maybe_trace, dict):
                            agent_priority_update_trace = maybe_trace
                coordination_review_details: dict[str, Any] = {"day_summary": day_summary, "updated_norms": world.norms}
                if comm_enabled is not None:
                    coordination_review_details["coordination_review_enabled"] = comm_enabled
                    coordination_review_details["communication_enabled"] = comm_enabled
                if discussion_trace:
                    coordination_review_details["review_trace"] = discussion_trace
                    coordination_review_details["discussion_trace"] = discussion_trace
                if agent_priority_update_trace:
                    coordination_review_details["agent_priority_update_trace"] = agent_priority_update_trace
                    event_logger.log(
                        t=env.now,
                        day=day,
                        event_type="AGENT_PRIORITY_PROFILE_UPDATE",
                        entity_id="system",
                        location="OperationsReview",
                        details=agent_priority_update_trace,
                    )
                # ?? ?? ???? ?? ?? ???.
                # replay? ? ???? ????? ????? ????.
                event_logger.log(
                    t=env.now,
                    day=day,
                    event_type="CHAT_DAILY_REVIEW",
                    entity_id="system",
                    location="CoordinationReview",
                    details=coordination_review_details,
                )
            last_summary = day_summary

            _emit_run_progress(status="running", message=f"completed day {day}", current_day=day)
            if world.terminated:
                break

        elapsed_wall_sec = perf_counter() - wall_clock_started
        finished_at_utc = _utc_now_iso()
        run_meta["finished_at_utc"] = finished_at_utc
        run_meta["wall_clock_sec"] = round(elapsed_wall_sec, 3)
        run_meta["wall_clock_human"] = _format_duration(elapsed_wall_sec)

        kpi = world.finalize_kpis()
        kpi["wall_clock_sec"] = round(elapsed_wall_sec, 3)
        kpi["wall_clock_human"] = _format_duration(elapsed_wall_sec)
        kpi["run_meta"] = run_meta

        _emit_run_progress(
            status="exporting_artifacts",
            message="exporting artifacts",
            current_day=min(total_days, len(world.daily_summaries)),
            finished_at_utc=finished_at_utc,
        )

        event_logger.write_json("daily_summary.json", {"days": world.daily_summaries})
        event_logger.write_json("kpi.json", kpi)
        event_logger.write_json("run_meta.json", run_meta)
        event_logger.write_json("minute_snapshots.json", {"snapshots": world.minute_snapshots})

        if decision_mode == "llm_planner" and hasattr(decision_module, "reflect_run"):
            reflect_run_fn = getattr(decision_module, "reflect_run")
            if callable(reflect_run_fn):
                try:
                    maybe_reflection = reflect_run_fn(
                        output_root=output_root,
                        kpi=kpi,
                        daily_summaries=world.daily_summaries,
                        run_meta=run_meta,
                    )
                    if isinstance(maybe_reflection, dict):
                        run_reflection_info = maybe_reflection
                        run_meta["knowledge_out_path"] = str(maybe_reflection.get("knowledge_out_path", "")).strip()
                        if str(maybe_reflection.get("run_reflection_path", "")).strip():
                            artifact_status["generated"]["run_reflection"] = str(maybe_reflection.get("run_reflection_path", "")).strip()
                        if str(maybe_reflection.get("run_reflection_markdown_path", "")).strip():
                            artifact_status["generated"]["run_reflection_markdown"] = str(maybe_reflection.get("run_reflection_markdown_path", "")).strip()
                        if str(maybe_reflection.get("knowledge_archive_path", "")).strip():
                            artifact_status["generated"]["knowledge_archive"] = str(maybe_reflection.get("knowledge_archive_path", "")).strip()
                except BaseException as exc:
                    artifact_status["errors"]["run_reflection"] = f"{type(exc).__name__}: {exc}"
                    raise

        llm_records: list[dict[str, Any]] = []
        transport_metrics: dict[str, Any] = {}
        try:
            if is_llm_mode(decision_mode) and hasattr(decision_module, "get_llm_exchange_records"):
                get_logs = getattr(decision_module, "get_llm_exchange_records")
                if callable(get_logs):
                    try:
                        llm_records = get_logs()
                    except BaseException:
                        llm_records = []
                    if isinstance(llm_records, list):
                        transport_metrics = _summarize_llm_transport_metrics(llm_records)
                        if transport_metrics and is_llm_mode(decision_mode):
                            run_meta.setdefault("llm", {})
                            run_meta["llm"]["transport_metrics"] = transport_metrics
                            kpi["llm_transport_metrics"] = transport_metrics
                    if isinstance(llm_records, list) and llm_records:
                        try:
                            event_logger.write_json("llm_exchange.json", {"run_meta": run_meta, "records": llm_records})
                        except BaseException as exc:
                            artifact_status["errors"]["llm_exchange"] = f"{type(exc).__name__}: {exc}"
                        try:
                            trace_dashboard_path = export_llm_trace_dashboard(records=llm_records, output_dir=output_root)
                            if trace_dashboard_path is not None and Path(trace_dashboard_path).exists():
                                llm_trace_path = str(trace_dashboard_path)
                                artifact_status["generated"]["llm_trace"] = llm_trace_path
                            else:
                                artifact_status["errors"]["llm_trace"] = "llm trace export returned no HTML path"
                        except BaseException as exc:
                            artifact_status["errors"]["llm_trace"] = f"{type(exc).__name__}: {exc}"
                        try:
                            workspace_dashboard_path = export_openclaw_workspace_dashboard(output_dir=output_root, run_meta=run_meta, records=llm_records)
                            if workspace_dashboard_path is not None and Path(workspace_dashboard_path).exists():
                                artifact_status["generated"]["openclaw_workspace_dashboard"] = str(workspace_dashboard_path)
                            else:
                                artifact_status["errors"]["openclaw_workspace_dashboard"] = "openclaw workspace dashboard export returned no HTML path"
                        except BaseException as exc:
                            artifact_status["errors"]["openclaw_workspace_dashboard"] = f"{type(exc).__name__}: {exc}"

            try:
                export_gantt(events=event_logger.events, output_dir=output_root)
                if gantt_path.exists():
                    artifact_status["generated"]["gantt"] = str(gantt_path)
                else:
                    artifact_status["errors"]["gantt"] = "gantt export completed without creating gantt.html"
            except BaseException as exc:
                artifact_status["errors"]["gantt"] = f"{type(exc).__name__}: {exc}"

            try:
                dashboard_path = export_kpi_dashboard(
                    kpi=kpi,
                    daily_summary=world.daily_summaries,
                    output_dir=output_root,
                )
                if dashboard_path is not None and Path(dashboard_path).exists():
                    artifact_status["generated"]["kpi_dashboard"] = str(dashboard_path)
                else:
                    artifact_status["errors"]["kpi_dashboard"] = "kpi dashboard export returned no HTML path"
            except BaseException as exc:
                artifact_status["errors"]["kpi_dashboard"] = f"{type(exc).__name__}: {exc}"

            try:
                task_priority_dashboard_path = export_task_priority_dashboard(
                    output_dir=output_root,
                    events=event_logger.events,
                    heuristic_rules=experiment_cfg.get("heuristic_rules", {}),
                )
                if task_priority_dashboard_path is not None and Path(task_priority_dashboard_path).exists():
                    artifact_status["generated"]["task_priority_dashboard"] = str(task_priority_dashboard_path)
                else:
                    artifact_status["errors"]["task_priority_dashboard"] = "task priority dashboard export returned no HTML path"
            except BaseException as exc:
                artifact_status["errors"]["task_priority_dashboard"] = f"{type(exc).__name__}: {exc}"

            try:
                orchestration_intelligence_dashboard_path = export_orchestration_intelligence_dashboard(
                    output_dir=output_root,
                    daily_summary=world.daily_summaries,
                    llm_records=llm_records,
                )
                if orchestration_intelligence_dashboard_path is not None and Path(orchestration_intelligence_dashboard_path).exists():
                    artifact_status["generated"]["orchestration_intelligence_dashboard"] = str(orchestration_intelligence_dashboard_path)
                else:
                    artifact_status["errors"]["orchestration_intelligence_dashboard"] = "orchestration intelligence dashboard export returned no HTML path"
            except BaseException as exc:
                artifact_status["errors"]["orchestration_intelligence_dashboard"] = f"{type(exc).__name__}: {exc}"
        except BaseException as exc:
            artifact_status["errors"]["artifact_export_fatal"] = f"{type(exc).__name__}: {exc}"

        kpi["run_meta"] = run_meta
        if run_reflection_info:
            kpi["run_reflection"] = run_reflection_info.get("run_reflection", {})
        event_logger.write_json("kpi.json", kpi)
        event_logger.write_json("run_meta.json", run_meta)

        try:
            event_logger.write_json("artifact_status.json", artifact_status)
        except BaseException as exc:
            artifact_status["errors"]["artifact_status"] = f"{type(exc).__name__}: {exc}"

        progress_stop.set()
        progress_thread.join(timeout=3.0)
        _emit_run_progress(
            status="completed",
            message="simulation finished",
            current_day=min(total_days, len(world.daily_summaries)),
            finished_at_utc=finished_at_utc,
        )
    except Exception:
        elapsed_wall_sec = perf_counter() - wall_clock_started
        finished_at_utc = _utc_now_iso()
        run_meta["finished_at_utc"] = finished_at_utc
        run_meta["wall_clock_sec"] = round(elapsed_wall_sec, 3)
        run_meta["wall_clock_human"] = _format_duration(elapsed_wall_sec)
        progress_stop.set()
        progress_thread.join(timeout=3.0)
        _emit_run_progress(
            status="failed",
            message="simulation failed",
            current_day=min(total_days, len(world.daily_summaries)),
            finished_at_utc=finished_at_utc,
        )
        raise
    finally:
        progress_stop.set()
        progress_thread.join(timeout=3.0)
        event_logger.close()

    return {
        "kpi": kpi,
        "daily_summary": world.daily_summaries,
        "output_dir": str(output_root),
        "events_path": str(output_root / "events.jsonl"),
        "gantt_path": str(output_root / "gantt.html"),
        "kpi_dashboard_path": str(dashboard_path) if dashboard_path else "",
        "task_priority_dashboard_path": str(task_priority_dashboard_path) if task_priority_dashboard_path else "",
        "orchestration_intelligence_dashboard_path": str(orchestration_intelligence_dashboard_path) if orchestration_intelligence_dashboard_path else "",
        "llm_trace_path": llm_trace_path,
        "terminated": world.terminated,
        "termination_reason": world.termination_reason,
        "decision_mode": decision_mode,
        "run_reflection_path": str(run_reflection_info.get("run_reflection_path", "")).strip(),
        "run_reflection_markdown_path": str(run_reflection_info.get("run_reflection_markdown_path", "")).strip(),
        "knowledge_in_path": str(run_meta.get("knowledge_in_path", "")).strip(),
        "knowledge_out_path": str(run_meta.get("knowledge_out_path", "")).strip(),
    }





