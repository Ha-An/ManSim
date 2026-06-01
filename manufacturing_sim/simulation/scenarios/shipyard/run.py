from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

import simpy

from agents import build_decision_module
from agents.modes import normalize_decision_mode
from dashboards.dashboard import export_kpi_dashboard
from dashboards.gantt import export_gantt
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.shipyard.world import ShipyardWorld


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _format_duration(seconds: float) -> str:
    raw = max(0.0, float(seconds))
    if raw < 1.0:
        return f"{raw:.3f}s"
    if raw < 10.0:
        return f"{raw:.2f}s"
    total = int(round(raw))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def run(
    experiment_cfg: dict[str, Any],
    logger: EventLogger | None = None,
    decision_modules: Any | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the standalone shipyard exterior surface-tile scenario."""

    output_root = Path(output_dir or Path.cwd() / "outputs")
    output_root.mkdir(parents=True, exist_ok=True)
    event_logger = logger or EventLogger(output_root)
    decision_cfg = experiment_cfg.get("decision", {}) if isinstance(experiment_cfg.get("decision", {}), dict) else {}
    decision_mode = normalize_decision_mode(str(decision_cfg.get("mode", "rolling_horizon_dedicated_roles")))
    decision_module = decision_modules or build_decision_module(experiment_cfg=experiment_cfg, decision_mode=decision_mode)

    total_days = int(experiment_cfg.get("horizon", {}).get("num_days", 1) or 1)
    minutes_per_day = float(experiment_cfg.get("horizon", {}).get("minutes_per_day", 240) or 240)
    sim_total_min = total_days * minutes_per_day
    started_at = _utc_now_iso()
    wall_started = perf_counter()
    run_meta: dict[str, Any] = {
        "scenario_type": "shipyard_basic",
        "decision_mode": decision_mode,
        "seed": int(experiment_cfg.get("seed", 2026)),
        "total_days": total_days,
        "minutes_per_day": minutes_per_day,
        "sim_total_min": round(sim_total_min, 3),
        "started_at_utc": started_at,
        "finished_at_utc": "",
        "wall_clock_sec": 0.0,
        "wall_clock_human": "0s",
    }
    try:
        env = simpy.Environment()
        world = ShipyardWorld(env=env, cfg=experiment_cfg, logger=event_logger, decision_module=decision_module)
        world.start()
        for day in range(1, total_days + 1):
            until = min(sim_total_min, day * minutes_per_day)
            env.run(until=until)
            world.close_day(day)
            if world.terminated:
                break

        elapsed = perf_counter() - wall_started
        run_meta["finished_at_utc"] = _utc_now_iso()
        run_meta["wall_clock_sec"] = round(elapsed, 3)
        run_meta["wall_clock_human"] = _format_duration(elapsed)
        kpi = world.finalize_kpis()
        kpi["wall_clock_sec"] = run_meta["wall_clock_sec"]
        kpi["wall_clock_human"] = run_meta["wall_clock_human"]
        kpi["run_meta"] = run_meta

        event_logger.write_json("daily_summary.json", {"days": world.daily_summaries})
        event_logger.write_json("kpi.json", kpi)
        event_logger.write_json("run_meta.json", run_meta)
        event_logger.write_json("minute_snapshots.json", {"snapshots": world.minute_snapshots})
        artifact_status = {"generated": {}, "errors": {}}
        try:
            export_gantt(events=event_logger.events, output_dir=output_root)
            artifact_status["generated"]["gantt"] = str((output_root / "gantt.html").resolve())
        except BaseException as exc:
            artifact_status["errors"]["gantt"] = f"{type(exc).__name__}: {exc}"
        dashboard_path: Path | None = None
        try:
            dashboard_path = export_kpi_dashboard(kpi=kpi, daily_summary=world.daily_summaries, output_dir=output_root)
            artifact_status["generated"]["kpi_dashboard"] = str(Path(dashboard_path).resolve()) if dashboard_path else ""
        except BaseException as exc:
            artifact_status["errors"]["kpi_dashboard"] = f"{type(exc).__name__}: {exc}"
        event_logger.write_json("artifact_status.json", artifact_status)
    finally:
        event_logger.close()

    return {
        "kpi": kpi,
        "daily_summary": world.daily_summaries,
        "output_dir": str(output_root),
        "events_path": str(output_root / "events.jsonl"),
        "gantt_path": str(output_root / "gantt.html"),
        "kpi_dashboard_path": str(dashboard_path) if dashboard_path else "",
        "task_priority_dashboard_path": "",
        "orchestration_intelligence_dashboard_path": "",
        "llm_trace_path": "",
        "terminated": world.terminated,
        "termination_reason": world.termination_reason,
        "decision_mode": decision_mode,
        "run_reflection_path": "",
        "run_reflection_markdown_path": "",
        "knowledge_in_path": "",
        "knowledge_out_path": "",
        "llm_knowledge_root": "",
        "llm_knowledge_base_root": "",
        "llm_knowledge_experiment_id": "",
        "llm_wiki_path": "",
        "llm_graph_path": "",
        "llm_wiki_dashboard_path": "",
    }
