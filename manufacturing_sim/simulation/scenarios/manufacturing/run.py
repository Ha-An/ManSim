from __future__ import annotations

from pathlib import Path
from typing import Any

import simpy

from manufacturing_sim.simulation.scenarios.manufacturing.decision.llm_optional import OptionalLLMDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.decision.modes import normalize_decision_mode
from manufacturing_sim.simulation.scenarios.manufacturing.decision.scripted import ScriptedDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.viz.dashboard import export_kpi_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.gantt import export_gantt
from manufacturing_sim.simulation.scenarios.manufacturing.viz.llm_trace import export_llm_trace_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.task_priority_dashboard import (
    export_task_priority_dashboard,
)
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


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

    run_meta: dict[str, Any] = {
        "decision_mode": decision_mode,
    }

    if decision_modules is not None:
        decision_module = decision_modules
    else:
        if decision_mode in {"adaptive_priority", "fixed_priority"}:
            decision_module = ScriptedDecisionModule(experiment_cfg)
        elif decision_mode == "llm":
            try:
                decision_module = OptionalLLMDecisionModule(
                    cfg=experiment_cfg,
                    llm_cfg=decision_cfg.get("llm", {}),
                )
            except NotImplementedError as exc:
                raise RuntimeError(
                    "decision.mode=llm is selected, but the configured LLM backend is unavailable. "
                    "Use decision=adaptive_priority for a local rule-based run or configure the LLM server."
                ) from exc
        else:
            raise ValueError(f"Unsupported decision mode: {decision_mode}")

    if decision_mode == "llm":
        llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
        comm_cfg = llm_cfg.get("communication", {}) if isinstance(llm_cfg.get("communication", {}), dict) else {}
        run_meta["llm"] = {
            "provider": str(llm_cfg.get("provider", "")),
            "server_url": str(llm_cfg.get("server_url", "")),
            "model": str(llm_cfg.get("model", "")),
            "communication_enabled": bool(comm_cfg.get("enabled", True)),
            "communication_rounds": int(comm_cfg.get("rounds", 0)),
        }

    env = simpy.Environment()
    world = ManufacturingWorld(env=env, cfg=experiment_cfg, logger=event_logger, decision_module=decision_module)
    world.bootstrap()

    last_summary: dict[str, Any] | None = None
    for day in range(1, int(experiment_cfg["horizon"]["num_days"]) + 1):
        if world.terminated:
            break
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
            townhall_details: dict[str, Any] = {"day_summary": day_summary, "updated_norms": world.norms}
            if comm_enabled is not None:
                townhall_details["communication_enabled"] = comm_enabled
            if discussion_trace:
                townhall_details["discussion_trace"] = discussion_trace
            event_logger.log(
                t=env.now,
                day=day,
                event_type="CHAT_TOWNHALL",
                entity_id="system",
                location="TownHall",
                details=townhall_details,
            )
        last_summary = day_summary
        if world.terminated:
            break

    kpi = world.finalize_kpis()
    kpi["run_meta"] = run_meta

    event_logger.write_json("daily_summary.json", {"days": world.daily_summaries})
    event_logger.write_json("kpi.json", kpi)
    event_logger.write_json("run_meta.json", run_meta)
    event_logger.write_json("minute_snapshots.json", {"snapshots": world.minute_snapshots})

    llm_trace_path: str = ""
    if decision_mode == "llm" and hasattr(decision_module, "get_llm_exchange_records"):
        get_logs = getattr(decision_module, "get_llm_exchange_records")
        if callable(get_logs):
            try:
                llm_records = get_logs()
            except Exception:
                llm_records = []
            if isinstance(llm_records, list) and llm_records:
                event_logger.write_json("llm_exchange.json", {"run_meta": run_meta, "records": llm_records})
                trace_dashboard_path = export_llm_trace_dashboard(records=llm_records, output_dir=output_root)
                if trace_dashboard_path is not None:
                    llm_trace_path = str(trace_dashboard_path)

    export_gantt(events=event_logger.events, output_dir=output_root)
    dashboard_path = export_kpi_dashboard(
        kpi=kpi,
        daily_summary=world.daily_summaries,
        output_dir=output_root,
    )
    task_priority_dashboard_path = export_task_priority_dashboard(
        output_dir=output_root,
        events=event_logger.events,
        heuristic_rules=experiment_cfg.get("heuristic_rules", {}),
    )
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
