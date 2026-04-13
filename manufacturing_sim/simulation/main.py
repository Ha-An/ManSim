from __future__ import annotations

import copy
import json
import os
import socket
import subprocess
import sys
import time
from urllib.parse import quote
import webbrowser
from pathlib import Path

import hydra
from hydra.core.hydra_config import HydraConfig
from omegaconf import DictConfig, OmegaConf

from manufacturing_sim.simulation.scenarios.manufacturing.run import run
from manufacturing_sim.simulation.scenarios.manufacturing.decision.modes import normalize_decision_mode
from manufacturing_sim.simulation.scenarios.manufacturing.viz.orchestration_intelligence_dashboard import export_orchestration_intelligence_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.dashboard import export_kpi_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.gantt import export_gantt
from manufacturing_sim.simulation.scenarios.manufacturing.viz.llm_trace import export_llm_trace_dashboard
from manufacturing_sim.simulation.scenarios.manufacturing.viz.series_dashboard import (
    build_series_analysis,
    export_series_dashboard,
)
from manufacturing_sim.simulation.scenarios.manufacturing.viz.task_priority_dashboard import export_task_priority_dashboard


def _open_artifact(path: Path) -> None:
    if not path.exists():
        return
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            webbrowser.open_new_tab(path.resolve().as_uri())
    except Exception:
        try:
            webbrowser.open_new_tab(path.resolve().as_uri())
        except Exception:
            pass


def _iter_output_artifacts(output_dir: Path) -> list[Path]:
    artifacts: list[Path] = []
    for path in sorted(output_dir.iterdir()):
        if path.is_file() and not path.name.startswith("."):
            artifacts.append(path)
    return artifacts


def _open_url(url: str) -> None:
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            webbrowser.open_new_tab(url)
    except Exception:
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            pass


def _load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _recover_artifacts_if_possible(output_dir: Path, experiment_cfg: dict[str, object]) -> dict[str, object] | None:
    progress = _load_json(output_dir / "progress.json")
    progress_status = str(progress.get("status", "")) if isinstance(progress, dict) else ""
    if progress_status not in {"exporting_artifacts", "completed"}:
        return None

    kpi = _load_json(output_dir / "kpi.json")
    daily_summary_blob = _load_json(output_dir / "daily_summary.json")
    run_meta = _load_json(output_dir / "run_meta.json")
    if not isinstance(kpi, dict) or not isinstance(daily_summary_blob, dict):
        return None
    daily_summary = daily_summary_blob.get("days", []) if isinstance(daily_summary_blob.get("days", []), list) else []
    events_path = output_dir / "events.jsonl"
    if not events_path.exists():
        return None

    try:
        events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except Exception:
        return None

    artifact_status: dict[str, object] = {"generated": {}, "errors": {}, "recovered": True}

    gantt_path = output_dir / "gantt.html"
    try:
        export_gantt(events=events, output_dir=output_dir)
        if gantt_path.exists():
            artifact_status["generated"]["gantt"] = str(gantt_path)
        else:
            artifact_status["errors"]["gantt"] = "gantt export completed without creating gantt.html"
    except BaseException as exc:
        artifact_status["errors"]["gantt"] = f"{type(exc).__name__}: {exc}"

    dashboard_path = None
    try:
        dashboard_path = export_kpi_dashboard(kpi=kpi, daily_summary=daily_summary, output_dir=output_dir)
        if dashboard_path is not None and Path(dashboard_path).exists():
            artifact_status["generated"]["kpi_dashboard"] = str(dashboard_path)
        else:
            artifact_status["errors"]["kpi_dashboard"] = "kpi dashboard export returned no HTML path"
    except BaseException as exc:
        artifact_status["errors"]["kpi_dashboard"] = f"{type(exc).__name__}: {exc}"

    task_priority_dashboard_path = None
    try:
        task_priority_dashboard_path = export_task_priority_dashboard(
            output_dir=output_dir,
            events=events,
            heuristic_rules=experiment_cfg.get("heuristic_rules", {}),
        )
        if task_priority_dashboard_path is not None and Path(task_priority_dashboard_path).exists():
            artifact_status["generated"]["task_priority_dashboard"] = str(task_priority_dashboard_path)
        else:
            artifact_status["errors"]["task_priority_dashboard"] = "task priority dashboard export returned no HTML path"
    except BaseException as exc:
        artifact_status["errors"]["task_priority_dashboard"] = f"{type(exc).__name__}: {exc}"

    orchestration_intelligence_dashboard_path = None
    llm_exchange_blob = _load_json(output_dir / "llm_exchange.json")
    records = llm_exchange_blob.get("records", []) if isinstance(llm_exchange_blob, dict) and isinstance(llm_exchange_blob.get("records", []), list) else []
    try:
        orchestration_intelligence_dashboard_path = export_orchestration_intelligence_dashboard(
            output_dir=output_dir,
            daily_summary=daily_summary,
            llm_records=records,
        )
        if orchestration_intelligence_dashboard_path is not None and Path(orchestration_intelligence_dashboard_path).exists():
            artifact_status["generated"]["orchestration_intelligence_dashboard"] = str(orchestration_intelligence_dashboard_path)
        else:
            artifact_status["errors"]["orchestration_intelligence_dashboard"] = "orchestration intelligence dashboard export returned no HTML path"
    except BaseException as exc:
        artifact_status["errors"]["orchestration_intelligence_dashboard"] = f"{type(exc).__name__}: {exc}"

    llm_trace_path = ""
    if records:
        try:
            trace_path = export_llm_trace_dashboard(records=records, output_dir=output_dir)
            if trace_path is not None and Path(trace_path).exists():
                llm_trace_path = str(trace_path)
                artifact_status["generated"]["llm_trace"] = llm_trace_path
            else:
                artifact_status["errors"]["llm_trace"] = "llm trace export returned no HTML path"
        except BaseException as exc:
            artifact_status["errors"]["llm_trace"] = f"{type(exc).__name__}: {exc}"

    try:
        (output_dir / "artifact_status.json").write_text(json.dumps(artifact_status, indent=2, ensure_ascii=True), encoding="utf-8")
    except Exception:
        pass

    if isinstance(progress, dict):
        progress["status"] = "completed"
        progress["message"] = "simulation finished"
        try:
            (output_dir / "progress.json").write_text(json.dumps(progress, indent=2, ensure_ascii=True), encoding="utf-8")
        except Exception:
            pass

    return {
        "kpi": kpi,
        "daily_summary": daily_summary,
        "output_dir": str(output_dir),
        "events_path": str(events_path),
        "gantt_path": str(gantt_path),
        "kpi_dashboard_path": str(dashboard_path) if dashboard_path else "",
        "task_priority_dashboard_path": str(task_priority_dashboard_path) if task_priority_dashboard_path else "",
        "orchestration_intelligence_dashboard_path": str(orchestration_intelligence_dashboard_path) if orchestration_intelligence_dashboard_path else "",
        "llm_trace_path": llm_trace_path,
        "terminated": bool(kpi.get("terminated", False)),
        "termination_reason": str(kpi.get("termination_reason", "")),
        "decision_mode": str((run_meta or {}).get("decision_mode", "")) if isinstance(run_meta, dict) else "",
    }


def _is_port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _pick_streamlit_port(preferred_port: int, range_start: int, range_end: int) -> int | None:
    if not _is_port_open(preferred_port):
        return preferred_port
    for port in range(range_start, range_end + 1):
        if not _is_port_open(port):
            return port
    return None


def _launch_streamlit_dashboard(
    *,
    app_path: Path,
    events_path: Path,
    preferred_port: int,
    range_start: int,
    range_end: int,
) -> str | None:
    port = _pick_streamlit_port(preferred_port, range_start, range_end)
    if port is None:
        return None

    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.port",
        str(port),
        "--server.headless",
        "true",
    ]
    try:
        subprocess.Popen(
            cmd,
            cwd=str(app_path.parents[5]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None

    time.sleep(1.2)
    url = f"http://localhost:{port}?events_path={quote(str(events_path))}"
    _open_url(url)
    return url


def _ensure_knowledge_stub(path: Path) -> None:
    if path.exists():
        return
    stub = "\n".join(
        [
            "# Run-Series Knowledge",
            "",
            "## Run-Series Scope",
            "No prior cross-run knowledge has been accumulated yet.",
            "",
            "## Carry-Forward Lessons",
            "- No carry-forward lessons recorded yet.",
            "",
            "## Detector Guidance",
            "- No detector guidance recorded yet.",
            "",
            "## Planner Guidance",
            "- No planner guidance recorded yet.",
            "",
            "## Open Watchouts",
            "- No open watchouts recorded yet.",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stub, encoding="utf-8")


def _export_series_artifacts_if_needed(runtime_output_dir: Path, run_count: int, summary_payload: dict[str, object]) -> tuple[str, str]:
    if run_count <= 1:
        return ("", "")
    analysis = build_series_analysis(parent_output_dir=runtime_output_dir, summary_payload=summary_payload)
    analysis_path = runtime_output_dir / "series_analysis.json"
    _write_json(analysis_path, analysis)
    dashboard_path = export_series_dashboard(parent_output_dir=runtime_output_dir, analysis=analysis)
    return (str(dashboard_path.resolve()) if dashboard_path is not None else "", str(analysis_path.resolve()))


def _run_once(experiment_cfg: dict[str, object], output_dir: Path) -> dict[str, object]:
    result: dict[str, object] | None = None
    try:
        try:
            result = run(experiment_cfg=experiment_cfg, output_dir=output_dir)
        except BaseException:
            recovered = _recover_artifacts_if_possible(output_dir, experiment_cfg)
            if recovered is None:
                raise
            print("[run] artifact recovery completed after post-simulation failure", flush=True)
            result = recovered
        else:
            recovered = _recover_artifacts_if_possible(output_dir, experiment_cfg)
            if recovered is not None:
                result.update({k: v for k, v in recovered.items() if k.endswith("_path") or k in {"output_dir", "events_path"}})
        return result
    except BaseException:
        recovered = _recover_artifacts_if_possible(output_dir, experiment_cfg)
        if recovered is None:
            raise
        if result is None:
            print("[run] artifact recovery completed after post-simulation failure", flush=True)
            print(json.dumps(recovered["kpi"], indent=2))
        return recovered


@hydra.main(config_path="conf", config_name="config", version_base="1.3")
def main(cfg: DictConfig) -> None:
    # Scenario config + policy-mode config are flattened into one payload
    # so scenario.run(...) and world/decision modules can read everything.
    experiment_cfg = OmegaConf.to_container(cfg.experiment, resolve=True)
    global_seed = cfg.get("seed", None)
    if global_seed is not None:
        experiment_cfg["seed"] = int(global_seed)
    experiment_cfg["decision"] = OmegaConf.to_container(cfg.get("decision", {}), resolve=True)
    experiment_cfg["heuristic_rules"] = OmegaConf.to_container(cfg.get("heuristic_rules", {}), resolve=True)
    runtime_output_dir = Path(HydraConfig.get().runtime.output_dir)
    print(f"[run] output_dir={runtime_output_dir}", flush=True)
    print(f"[run] progress_path={runtime_output_dir / 'progress.json'}", flush=True)
    decision_cfg = experiment_cfg.get("decision", {}) if isinstance(experiment_cfg.get("decision", {}), dict) else {}
    decision_mode = normalize_decision_mode(str(decision_cfg.get("mode", "adaptive_priority")))
    llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
    orchestration_cfg = llm_cfg.get("orchestration", {}) if isinstance(llm_cfg.get("orchestration", {}), dict) else {}
    orchestration_active = decision_mode == "llm_planner" and bool(orchestration_cfg.get("enabled", True))
    requested_run_count = int(orchestration_cfg.get("run_count", 3) or 3)
    run_count = max(1, requested_run_count) if orchestration_active else 1

    knowledge_path = runtime_output_dir / "knowledge.md"
    knowledge_history_dir = runtime_output_dir / "knowledge_history"
    if orchestration_active:
        _ensure_knowledge_stub(knowledge_path)

    series_results: list[dict[str, object]] = []
    last_result: dict[str, object] | None = None
    try:
        for run_index in range(1, run_count + 1):
            child_output_dir = runtime_output_dir if run_count == 1 else (runtime_output_dir / f"run_{run_index:02d}")
            child_cfg = copy.deepcopy(experiment_cfg)
            if orchestration_active:
                child_cfg["_run_series"] = {
                    "run_index": run_index,
                    "total_runs": run_count,
                    "parent_output_dir": str(runtime_output_dir.resolve()),
                    "knowledge_path": str(knowledge_path.resolve()),
                    "knowledge_history_dir": str(knowledge_history_dir.resolve()),
                }
            print(f"[run] series_run={run_index}/{run_count} child_output_dir={child_output_dir}", flush=True)
            result = _run_once(child_cfg, child_output_dir)
            last_result = result
            kpi_blob = result.get("kpi", {}) if isinstance(result.get("kpi", {}), dict) else {}
            run_meta_blob = _load_json(child_output_dir / "run_meta.json")
            series_entry = {
                "run_index": run_index,
                "output_dir": str(child_output_dir.resolve()),
                "kpi_path": str((child_output_dir / "kpi.json").resolve()),
                "run_meta_path": str((child_output_dir / "run_meta.json").resolve()),
                "knowledge_in_path": str(result.get("knowledge_in_path", "")),
                "knowledge_out_path": str(result.get("knowledge_out_path", "")),
                "run_reflection_path": str(result.get("run_reflection_path", "")),
                "total_products": int(kpi_blob.get("total_products", 0) or 0),
                "downstream_closure_ratio": float(kpi_blob.get("downstream_closure_ratio", 0.0) or 0.0),
                "wall_clock_sec": float(kpi_blob.get("wall_clock_sec", 0.0) or 0.0),
                "kpi_dashboard_path": str(result.get("kpi_dashboard_path", "")),
                "orchestration_intelligence_dashboard_path": str(result.get("orchestration_intelligence_dashboard_path", "")),
                "run_reflection_markdown_path": str(result.get("run_reflection_markdown_path", "")),
                "evaluator_enabled": bool(
                    (
                        ((run_meta_blob or {}).get("llm", {}))
                        if isinstance((run_meta_blob or {}).get("llm", {}), dict)
                        else {}
                    ).get("evaluator_enabled", False)
                ),
            }
            series_results.append(series_entry)
            if orchestration_active:
                summary_payload: dict[str, object] = {
                    "requested_run_count": requested_run_count,
                    "completed_run_count": len(series_results),
                    "knowledge_path": str(knowledge_path.resolve()),
                    "series_dashboard_path": "",
                    "series_analysis_path": "",
                    "runs": series_results,
                }
                _write_json(runtime_output_dir / "run_series_summary.json", summary_payload)
                series_dashboard_path, series_analysis_path = _export_series_artifacts_if_needed(runtime_output_dir, run_count, summary_payload)
                summary_payload["series_dashboard_path"] = series_dashboard_path
                summary_payload["series_analysis_path"] = series_analysis_path
                _write_json(runtime_output_dir / "run_series_summary.json", summary_payload)

        if last_result is None:
            raise RuntimeError("No run result was produced.")

        result = last_result
        print(json.dumps(result["kpi"], indent=2))

        ui_cfg = cfg.get("ui", {})
        artifact_root = Path(result["output_dir"])

        auto_open = bool(ui_cfg.get("auto_open_results", False))
        if auto_open:
            open_all_artifacts = bool(ui_cfg.get("open_all_artifacts", False))
            artifact_names = [str(item) for item in list(ui_cfg.get("open_artifacts", []))]
            opened_paths: set[Path] = set()

            def _open_once(path: Path) -> None:
                resolved = path.resolve()
                if resolved in opened_paths:
                    return
                opened_paths.add(resolved)
                _open_artifact(path)

            if open_all_artifacts:
                for artifact_path in _iter_output_artifacts(artifact_root):
                    _open_once(artifact_path)
            else:
                for artifact_name in artifact_names:
                    _open_once(artifact_root / str(artifact_name))
                    if run_count > 1 and artifact_name in {"series_dashboard.html", "series_analysis.json"}:
                        _open_once(runtime_output_dir / str(artifact_name))

            llm_trace_path = str(result.get("llm_trace_path", "")).strip()
            if llm_trace_path and (open_all_artifacts or "llm_trace.html" in artifact_names):
                _open_once(Path(llm_trace_path))
            if run_count > 1 and open_all_artifacts:
                for series_artifact in ("series_dashboard.html", "series_analysis.json", "knowledge.md", "run_series_summary.json"):
                    _open_once(runtime_output_dir / series_artifact)

        auto_open_streamlit = bool(ui_cfg.get("auto_open_streamlit", False))
        if auto_open_streamlit:
            app_path = Path(__file__).resolve().parent / "scenarios" / "manufacturing" / "viz" / "replay_app.py"
            events_path = Path(result["events_path"])
            port_cfg = ui_cfg.get("streamlit_port_range", {})
            range_start = int(port_cfg.get("start", 8505))
            range_end = int(port_cfg.get("end", 8555))
            preferred_port = int(ui_cfg.get("streamlit_preferred_port", 8505))
            _launch_streamlit_dashboard(
                app_path=app_path,
                events_path=events_path,
                preferred_port=preferred_port,
                range_start=range_start,
                range_end=range_end,
            )
    except BaseException:
        if last_result is not None:
            raise
        recovered = _recover_artifacts_if_possible(runtime_output_dir, experiment_cfg)
        if recovered is None:
            raise
        print("[run] artifact recovery completed after post-simulation failure", flush=True)
        print(json.dumps(recovered["kpi"], indent=2))
        return


if __name__ == "__main__":
    main()


