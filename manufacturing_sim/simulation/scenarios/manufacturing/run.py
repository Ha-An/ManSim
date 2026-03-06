from __future__ import annotations

from pathlib import Path
from typing import Any

import simpy

from manufacturing_sim.simulation.scenarios.manufacturing.decision.llm_optional import OptionalLLMDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.decision.scripted import ScriptedDecisionModule
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.manufacturing.viz.dashboard import export_kpi_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.gantt import export_gantt
from manufacturing_sim.simulation.scenarios.manufacturing.world import ManufacturingWorld


def run(
    experiment_cfg: dict[str, Any],
    logger: EventLogger | None = None,
    decision_modules: Any | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    output_root = Path(output_dir or Path.cwd() / "outputs")
    output_root.mkdir(parents=True, exist_ok=True)

    event_logger = logger or EventLogger(output_root)
    if decision_modules is not None:
        decision_module = decision_modules
    else:
        decision_cfg = experiment_cfg.get("decision", {})
        decision_mode = str(decision_cfg.get("mode", "heuristic")).strip().lower()
        if decision_mode == "heuristic":
            decision_module = ScriptedDecisionModule(experiment_cfg)
        elif decision_mode == "llm":
            try:
                decision_module = OptionalLLMDecisionModule(
                    cfg=experiment_cfg,
                    llm_cfg=decision_cfg.get("llm", {}),
                )
            except NotImplementedError as exc:
                raise RuntimeError(
                    "decision.mode=llm is selected, but LLM adapter is not implemented yet. "
                    "Use decision=heuristic for now or implement decision/llm_optional.py."
                ) from exc
        else:
            raise ValueError(f"Unsupported decision mode: {decision_mode}")

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
    event_logger.write_json("daily_summary.json", {"days": world.daily_summaries})
    event_logger.write_json("kpi.json", kpi)
    event_logger.write_json("minute_snapshots.json", {"snapshots": world.minute_snapshots})
    export_gantt(events=event_logger.events, output_dir=output_root)
    dashboard_path = export_kpi_dashboard(
        kpi=kpi,
        daily_summary=world.daily_summaries,
        output_dir=output_root,
    )
    event_logger.close()

    return {
        "kpi": kpi,
        "daily_summary": world.daily_summaries,
        "output_dir": str(output_root),
        "events_path": str(output_root / "events.jsonl"),
        "gantt_path": str(output_root / "gantt.html"),
        "kpi_dashboard_path": str(dashboard_path) if dashboard_path else "",
        "terminated": world.terminated,
        "termination_reason": world.termination_reason,
    }
