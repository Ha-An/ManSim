from __future__ import annotations

import copy
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import hydra
from hydra.core.hydra_config import HydraConfig
from omegaconf import DictConfig

from dashboards import (
    build_series_analysis,
    export_knowledge_dashboard,
    export_reasoning_dashboard,
    export_replay_dashboard,
    export_results_dashboard,
    export_series_dashboard,
)
from dashboards.dashboard import export_kpi_dashboard
from dashboards.gantt import export_gantt
from dashboards.manifest import build_dashboard_manifest, write_dashboard_manifests
from agents.modes import normalize_decision_mode
from knowledge import KnowledgeStore
from manufacturing_sim.simulation.scenarios.manufacturing.run import run
from runtime.compat import build_legacy_experiment_cfg


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _open_artifact(path: Path) -> None:
    if not path.exists():
        return
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open_new_tab(path.resolve().as_uri())
    except Exception:
        pass


def _open_selected_artifacts(output_root: Path, child_output_dir: Path, cfg: DictConfig) -> None:
    ui_cfg = cfg.get("runtime", {}).get("ui", {}) if isinstance(cfg.get("runtime", {}).get("ui", {}), DictConfig) else cfg.get("runtime", {}).get("ui", {})
    if not isinstance(ui_cfg, (dict, DictConfig)):
        return
    auto_open = bool(ui_cfg.get("auto_open_results", False))
    if not auto_open:
        return
    open_all = bool(ui_cfg.get("open_all_artifacts", False))
    artifact_names = [str(item) for item in list(ui_cfg.get("open_artifacts", []))]
    if open_all:
        for path in sorted(child_output_dir.iterdir()):
            if path.is_file() and not path.name.startswith("."):
                _open_artifact(path)
        if output_root != child_output_dir:
            for name in ("series_dashboard.html", "series_analysis.json", "knowledge/KNOWLEDGE.md", "knowledge/knowledge_graph.json"):
                _open_artifact(output_root / name)
        return
    for name in artifact_names:
        candidate = child_output_dir / name
        if candidate.exists():
            _open_artifact(candidate)
            continue
        shared = output_root / name
        if shared.exists():
            _open_artifact(shared)


def _build_replay_streamlit_url(
    *,
    cfg: DictConfig,
    events_path: Path | None = None,
    manifest_path: Path | None = None,
    run_id: str | None = None,
    series_root: Path | None = None,
) -> str:
    runtime_cfg = cfg.get("runtime", {}) if isinstance(cfg.get("runtime", {}), DictConfig) else cfg.get("runtime", {})
    ui_cfg = runtime_cfg.get("ui", {}) if isinstance(runtime_cfg.get("ui", {}), DictConfig) else runtime_cfg.get("ui", {})
    port = int(ui_cfg.get("streamlit_preferred_port", 8505) or 8505)
    params: list[str] = []
    if manifest_path is not None:
        params.append(f"manifest_path={quote(manifest_path.resolve().as_posix(), safe='')}")
    if series_root is not None:
        params.append(f"series_root={quote(series_root.resolve().as_posix(), safe='')}")
    if run_id:
        params.append(f"run={quote(str(run_id), safe='')}")
    if events_path is not None:
        params.append(f"events_path={quote(events_path.resolve().as_posix(), safe='')}")
    return f"http://localhost:{port}/?{'&'.join(params)}" if params else f"http://localhost:{port}"


def _export_run_dashboards(
    *,
    output_dir: Path,
    result: dict[str, Any],
    knowledge_store: KnowledgeStore,
    cfg: DictConfig,
    manifest: dict[str, Any] | None = None,
    manifest_path: Path | None = None,
    current_run_id: str | None = None,
    analysis: dict[str, Any] | None = None,
) -> dict[str, str]:
    kpi = result.get("kpi", {}) if isinstance(result.get("kpi", {}), dict) else {}
    if not kpi:
        kpi = _load_json(output_dir / "kpi.json") or {}
    events_path = Path(str(result.get("events_path", output_dir / "events.jsonl")))
    events = _load_events(events_path)
    daily_payload = _load_json(output_dir / "daily_summary.json") or {}
    daily_rows = daily_payload.get("days", []) if isinstance(daily_payload.get("days", []), list) else []
    run_meta = _load_json(output_dir / "run_meta.json") or {}
    reflection = _load_json(Path(str(result.get("run_reflection_path", output_dir / "run_reflection.json")))) or {}

    export_kpi_dashboard(
        kpi=kpi,
        daily_summary=daily_rows,
        output_dir=output_dir,
        manifest=manifest,
        manifest_path=manifest_path,
        current_run_id=current_run_id,
    )
    export_gantt(
        events,
        output_dir,
        manifest=manifest,
        manifest_path=manifest_path,
        current_run_id=current_run_id,
    )
    replay_path = export_replay_dashboard(output_dir=output_dir, events=events)
    knowledge_path = export_knowledge_dashboard(
        output_dir=output_dir,
        graph=knowledge_store.graph,
        manifest=manifest,
        manifest_path=manifest_path,
        current_run_id=current_run_id,
        analysis=analysis,
    )
    reasoning_path = export_reasoning_dashboard(
        output_dir=output_dir,
        manifest=manifest,
        manifest_path=manifest_path,
        current_run_id=current_run_id,
        kpi=kpi,
        daily_summary=daily_rows,
        reflection=reflection,
        run_meta=run_meta,
    )
    results_path = export_results_dashboard(
        output_dir=output_dir,
        kpi=kpi,
        manifest=manifest,
        manifest_path=manifest_path,
        current_run_id=current_run_id,
        analysis=analysis,
        reflection=reflection,
        run_meta=run_meta,
    )
    return {
        "results_dashboard_path": str(results_path.resolve()),
        "replay_dashboard_path": str(replay_path.resolve()),
        "knowledge_dashboard_path": str(knowledge_path.resolve()),
        "reasoning_dashboard_path": str(reasoning_path.resolve()),
    }


def _export_series_artifacts_if_needed(runtime_output_dir: Path, run_count: int, summary_payload: dict[str, object]) -> tuple[str, str]:
    if run_count <= 1:
        return ("", "")
    analysis = build_series_analysis(parent_output_dir=runtime_output_dir, summary_payload=summary_payload)
    analysis_path = runtime_output_dir / "series_analysis.json"
    _write_json(analysis_path, analysis)
    dashboard_path = export_series_dashboard(parent_output_dir=runtime_output_dir, analysis=analysis)
    return (
        str(dashboard_path.resolve()) if dashboard_path is not None else "",
        str(analysis_path.resolve()),
    )


def _refresh_dashboard_suite(
    *,
    runtime_output_dir: Path,
    summary_payload: dict[str, object],
    knowledge_store: KnowledgeStore,
    cfg: DictConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    analysis = build_series_analysis(parent_output_dir=runtime_output_dir, summary_payload=summary_payload)
    analysis_path = runtime_output_dir / "series_analysis.json"
    _write_json(analysis_path, analysis)
    manifest = build_dashboard_manifest(
        root_output_dir=runtime_output_dir,
        summary_payload=summary_payload,
        analysis_payload=analysis,
        streamlit_port=int(
            (
                (
                    cfg.get("runtime", {}).get("ui", {})
                    if isinstance(cfg.get("runtime", {}).get("ui", {}), DictConfig)
                    else cfg.get("runtime", {}).get("ui", {})
                )
                or {}
            ).get("streamlit_preferred_port", 8505)
            or 8505
        ),
    )
    manifest_paths = write_dashboard_manifests(root_output_dir=runtime_output_dir, manifest=manifest)
    root_manifest_path = Path(str(manifest_paths.get("root", runtime_output_dir / "dashboard_manifest.json")))
    run_manifest_paths = manifest_paths.get("runs", {}) if isinstance(manifest_paths.get("runs", {}), dict) else {}

    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    for run_entry in runs:
        if not isinstance(run_entry, dict):
            continue
        current_run_id = str(run_entry.get("id", "")).strip()
        output_dir = Path(str(run_entry.get("output_dir", "")).strip())
        if not output_dir.exists():
            continue
        result_like = {
            "output_dir": str(output_dir.resolve()),
            "events_path": str(((run_entry.get("artifacts", {}) if isinstance(run_entry.get("artifacts", {}), dict) else {}).get("events.jsonl", output_dir / "events.jsonl"))),
            "run_reflection_path": str(((run_entry.get("artifacts", {}) if isinstance(run_entry.get("artifacts", {}), dict) else {}).get("run_reflection.json", output_dir / "run_reflection.json"))),
        }
        exported = _export_run_dashboards(
            output_dir=output_dir,
            result=result_like,
            knowledge_store=knowledge_store,
            cfg=cfg,
            manifest=manifest,
            manifest_path=Path(str(run_manifest_paths.get(current_run_id, root_manifest_path))),
            current_run_id=current_run_id,
            analysis=analysis,
        )
        run_entry["results_dashboard_path"] = exported["results_dashboard_path"]
        run_entry["replay_dashboard_path"] = exported["replay_dashboard_path"]
        run_entry["knowledge_dashboard_path"] = exported["knowledge_dashboard_path"]
        run_entry["reasoning_dashboard_path"] = exported["reasoning_dashboard_path"]

    if not bool(manifest.get("single_run", True)):
        dashboard_path = export_series_dashboard(
            parent_output_dir=runtime_output_dir,
            analysis=analysis,
            manifest=manifest,
            manifest_path=root_manifest_path,
        )
        summary_payload["series_dashboard_path"] = str(dashboard_path.resolve()) if dashboard_path is not None else ""
    summary_payload["series_analysis_path"] = str(analysis_path.resolve())
    summary_payload["dashboard_manifest_path"] = str(root_manifest_path.resolve())
    summary_payload["runs"] = runs
    _write_json(runtime_output_dir / "run_series_summary.json", summary_payload)
    manifest = build_dashboard_manifest(
        root_output_dir=runtime_output_dir,
        summary_payload=summary_payload,
        analysis_payload=analysis,
        streamlit_port=int(manifest.get("streamlit_preferred_port", 8505) or 8505),
    )
    manifest_paths = write_dashboard_manifests(root_output_dir=runtime_output_dir, manifest=manifest)
    return analysis, manifest


def _run_once(experiment_cfg: dict[str, object], output_dir: Path) -> dict[str, object]:
    return run(experiment_cfg=experiment_cfg, output_dir=output_dir)


def _sync_root_knowledge_artifacts(runtime_output_dir: Path, knowledge_store: KnowledgeStore) -> None:
    runtime_output_dir.mkdir(parents=True, exist_ok=True)
    runtime_output_dir.joinpath("knowledge.md").write_text(
        knowledge_store.markdown_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    runtime_output_dir.joinpath("knowledge_graph.json").write_text(
        knowledge_store.graph_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )


@hydra.main(config_path="../configs", config_name="config", version_base="1.3")
def main(cfg: DictConfig) -> None:
    experiment_cfg = build_legacy_experiment_cfg(cfg)
    runtime_output_dir = Path(HydraConfig.get().runtime.output_dir)
    runtime_output_dir.mkdir(parents=True, exist_ok=True)

    decision_cfg = experiment_cfg.get("decision", {}) if isinstance(experiment_cfg.get("decision", {}), dict) else {}
    decision_mode = normalize_decision_mode(str(decision_cfg.get("mode", "adaptive_priority")))
    llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
    orchestration_cfg = llm_cfg.get("orchestration", {}) if isinstance(llm_cfg.get("orchestration", {}), dict) else {}
    orchestration_active = decision_mode in {"llm_planner", "openclaw_adaptive_priority"} and bool(orchestration_cfg.get("enabled", True))
    requested_run_count = int(orchestration_cfg.get("run_count", 1 if decision_mode == "openclaw_adaptive_priority" else 3) or (1 if decision_mode == "openclaw_adaptive_priority" else 3))
    run_count = max(1, requested_run_count) if orchestration_active else 1
    knowledge_ingest_enabled = not (decision_mode == "openclaw_adaptive_priority" and run_count <= 1)

    knowledge_root = runtime_output_dir / "knowledge"
    knowledge_history_dir = knowledge_root / "history"
    knowledge_store = KnowledgeStore(knowledge_root)
    knowledge_store.save()
    knowledge_store.render_markdown()
    _sync_root_knowledge_artifacts(runtime_output_dir, knowledge_store)
    if knowledge_ingest_enabled:
        export_knowledge_dashboard(output_dir=runtime_output_dir, graph=knowledge_store.graph)

    series_results: list[dict[str, object]] = []
    last_result: dict[str, object] | None = None
    for run_index in range(1, run_count + 1):
        child_output_dir = runtime_output_dir if run_count == 1 else (runtime_output_dir / f"run_{run_index:02d}")
        child_output_dir.mkdir(parents=True, exist_ok=True)
        child_cfg = copy.deepcopy(experiment_cfg)
        if orchestration_active:
            child_cfg["_run_series"] = {
                "run_index": run_index,
                "total_runs": run_count,
                "parent_output_dir": str(runtime_output_dir.resolve()),
                "knowledge_path": str(knowledge_store.markdown_path.resolve()),
                "knowledge_history_dir": str(knowledge_history_dir.resolve()),
            }
        result = _run_once(child_cfg, child_output_dir)
        last_result = result

        kpi = result.get("kpi", {}) if isinstance(result.get("kpi", {}), dict) else {}
        daily_summary = result.get("daily_summary", []) if isinstance(result.get("daily_summary", []), list) else []
        reflection_path = Path(str(result.get("run_reflection_path", ""))) if str(result.get("run_reflection_path", "")).strip() else None
        reflection = _load_json(reflection_path) if reflection_path is not None else None
        if knowledge_ingest_enabled:
            knowledge_store.ingest_run(run_index=run_index, kpi=kpi, daily_summary=daily_summary, reflection=reflection)
            knowledge_store.save()
            knowledge_store.render_markdown()
            _sync_root_knowledge_artifacts(runtime_output_dir, knowledge_store)
            export_knowledge_dashboard(output_dir=runtime_output_dir, graph=knowledge_store.graph)

        exported = _export_run_dashboards(output_dir=child_output_dir, result=result, knowledge_store=knowledge_store, cfg=cfg)
        run_meta_blob = _load_json(child_output_dir / "run_meta.json") or {}
        series_entry = {
            "run_index": run_index,
            "output_dir": str(child_output_dir.resolve()),
            "kpi_path": str((child_output_dir / "kpi.json").resolve()),
            "run_meta_path": str((child_output_dir / "run_meta.json").resolve()),
            "daily_summary_path": str((child_output_dir / "daily_summary.json").resolve()),
            "events_path": str((child_output_dir / "events.jsonl").resolve()),
            "knowledge_in_path": str(result.get("knowledge_in_path", "")),
            "knowledge_out_path": str(knowledge_store.markdown_path.resolve()),
            "run_reflection_path": str(result.get("run_reflection_path", "")),
            "run_reflection_markdown_path": str(result.get("run_reflection_markdown_path", "")),
            "total_products": int(kpi.get("total_products", 0) or 0),
            "downstream_closure_ratio": float(kpi.get("downstream_closure_ratio", 0.0) or 0.0),
            "wall_clock_sec": float(kpi.get("wall_clock_sec", 0.0) or 0.0),
            "physical_incident_total": int(kpi.get("physical_incident_total", 0) or 0),
            "coordination_incident_total": int(kpi.get("coordination_incident_total", 0) or 0),
            "unique_replan_blocker_total": int(kpi.get("unique_replan_blocker_total", 0) or 0),
            "planner_escalation_total": int(kpi.get("planner_escalation_total", 0) or 0),
            "commitment_dispatch_total": int(kpi.get("commitment_dispatch_total", 0) or 0),
            "completed_product_lead_time_avg_min": float(kpi.get("completed_product_lead_time_avg_min", 0.0) or 0.0),
            "kpi_dashboard_path": str(result.get("kpi_dashboard_path", "")),
            "gantt_path": str(result.get("gantt_path", "")),
            "task_priority_dashboard_path": str(result.get("task_priority_dashboard_path", "")),
            "orchestration_intelligence_dashboard_path": str(result.get("orchestration_intelligence_dashboard_path", "")),
            "llm_trace_path": str(result.get("llm_trace_path", "")),
            "replay_dashboard_path": exported["replay_dashboard_path"],
            "knowledge_dashboard_path": exported["knowledge_dashboard_path"],
            "reasoning_dashboard_path": exported["reasoning_dashboard_path"],
            "results_dashboard_path": exported["results_dashboard_path"],
            "evaluator_enabled": bool(
                (
                    (run_meta_blob.get("llm", {}))
                    if isinstance(run_meta_blob.get("llm", {}), dict)
                    else {}
                ).get("evaluator_enabled", False)
            ),
        }
        series_results.append(series_entry)

        summary_payload: dict[str, object] = {
            "requested_run_count": run_count,
            "completed_run_count": len(series_results),
            "knowledge_path": str((runtime_output_dir / "knowledge.md").resolve()),
            "knowledge_graph_path": str((runtime_output_dir / "knowledge_graph.json").resolve()),
            "series_dashboard_path": "",
            "series_analysis_path": "",
            "runs": series_results,
        }
        _write_json(runtime_output_dir / "run_series_summary.json", summary_payload)
        if run_count > 1:
            dashboard_path, analysis_path = _export_series_artifacts_if_needed(runtime_output_dir, run_count, summary_payload)
            summary_payload["series_dashboard_path"] = dashboard_path
            summary_payload["series_analysis_path"] = analysis_path
            _write_json(runtime_output_dir / "run_series_summary.json", summary_payload)

    if last_result is None:
        raise RuntimeError("No run result was produced.")

    final_summary_payload = _load_json(runtime_output_dir / "run_series_summary.json") or {
        "requested_run_count": run_count,
        "completed_run_count": len(series_results),
        "knowledge_path": str((runtime_output_dir / "knowledge.md").resolve()),
        "knowledge_graph_path": str((runtime_output_dir / "knowledge_graph.json").resolve()),
        "runs": series_results,
    }
    _refresh_dashboard_suite(
        runtime_output_dir=runtime_output_dir,
        summary_payload=final_summary_payload,
        knowledge_store=knowledge_store,
        cfg=cfg,
    )

    child_output_dir = Path(str(last_result.get("output_dir", runtime_output_dir)))
    _open_selected_artifacts(runtime_output_dir, child_output_dir, cfg)
    print(json.dumps(last_result["kpi"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
