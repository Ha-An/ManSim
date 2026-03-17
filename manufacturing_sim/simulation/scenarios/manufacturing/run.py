from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Event as ThreadEvent, Lock, Thread
from time import perf_counter
from typing import Any

import simpy

from manufacturing_sim.simulation.scenarios.manufacturing.decision.llm_optional import OptionalLLMDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.decision.llm_task_selector import LLMTaskSelectorDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.decision.modes import is_llm_mode, normalize_decision_mode
from manufacturing_sim.simulation.scenarios.manufacturing.decision.scripted import ScriptedDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.viz.dashboard import export_kpi_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.gantt import export_gantt
from manufacturing_sim.simulation.scenarios.manufacturing.viz.llm_trace import export_llm_trace_dashboard
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
    """Execute one manufacturing simulation run and export result artifacts."""

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

    run_meta: dict[str, Any] = {
        "decision_mode": decision_mode,
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
            try:
                decision_module = OptionalLLMDecisionModule(
                    cfg=experiment_cfg,
                    llm_cfg=decision_cfg.get("llm", {}),
                )
            except RuntimeError as exc:
                raise RuntimeError(
                    "decision.mode=llm_planner is selected, but the configured LLM backend is unavailable. "
                    "Use decision=adaptive_priority for a local rule-based run or configure the LLM server."
                ) from exc
        elif decision_mode == "llm_task_selector":
            try:
                decision_module = LLMTaskSelectorDecisionModule(
                    cfg=experiment_cfg,
                    llm_cfg=decision_cfg.get("llm", {}),
                )
            except RuntimeError as exc:
                raise RuntimeError(
                    "decision.mode=llm_task_selector is selected, but the configured LLM backend is unavailable. "
                    "Use decision=adaptive_priority for a local rule-based run or configure the LLM server."
                ) from exc
        else:
            raise ValueError(f"Unsupported decision mode: {decision_mode}")

    if is_llm_mode(decision_mode):
        llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
        comm_cfg = llm_cfg.get("communication", {}) if isinstance(llm_cfg.get("communication", {}), dict) else {}
        selector_cfg = llm_cfg.get("task_selector", {}) if isinstance(llm_cfg.get("task_selector", {}), dict) else {}
        run_meta["llm"] = {
            "mode_variant": decision_mode,
            "provider": str(llm_cfg.get("provider", "")),
            "server_url": str(llm_cfg.get("server_url", "")),
            "model": str(llm_cfg.get("model", "")),
            "communication_enabled": bool(comm_cfg.get("enabled", True)),
            "communication_rounds": int(comm_cfg.get("rounds", 0)),
            "communication_language": str(comm_cfg.get("language", "ENG")).strip().upper() or "ENG",
            "task_selector_max_candidates": int(selector_cfg.get("max_candidates", 0)),
            "task_selector_include_score_hints": bool(selector_cfg.get("include_score_hints", False)),
            "urgent_discuss_enabled": bool(urgent_cfg.get("enabled", True)),
        }

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
    gantt_path = output_root / "gantt.html"
    artifact_status: dict[str, Any] = {"generated": {}, "errors": {}}

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
                townhall_details: dict[str, Any] = {"day_summary": day_summary, "updated_norms": world.norms}
                if comm_enabled is not None:
                    townhall_details["communication_enabled"] = comm_enabled
                if discussion_trace:
                    townhall_details["discussion_trace"] = discussion_trace
                if agent_priority_update_trace:
                    townhall_details["agent_priority_update_trace"] = agent_priority_update_trace
                    event_logger.log(
                        t=env.now,
                        day=day,
                        event_type="AGENT_PRIORITY_PROFILE_UPDATE",
                        entity_id="system",
                        location="TownHall",
                        details=agent_priority_update_trace,
                    )
                event_logger.log(
                    t=env.now,
                    day=day,
                    event_type="CHAT_TOWNHALL",
                    entity_id="system",
                    location="TownHall",
                    details=townhall_details,
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

        try:
            if is_llm_mode(decision_mode) and hasattr(decision_module, "get_llm_exchange_records"):
                get_logs = getattr(decision_module, "get_llm_exchange_records")
                if callable(get_logs):
                    try:
                        llm_records = get_logs()
                    except BaseException:
                        llm_records = []
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
        except BaseException as exc:
            artifact_status["errors"]["artifact_export_fatal"] = f"{type(exc).__name__}: {exc}"

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
        "llm_trace_path": llm_trace_path,
        "terminated": world.terminated,
        "termination_reason": world.termination_reason,
        "decision_mode": decision_mode,
    }
