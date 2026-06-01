from __future__ import annotations

import copy
import json
import os
import re
import socket
import subprocess
import sys
import time
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
    export_llm_graph_dashboard,
    export_manager_replay,
    export_operations_replay,
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
from manufacturing_sim.simulation.scenarios.registry import run_scenario as run
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


def _runtime_ui_cfg(cfg: DictConfig) -> dict[str, Any] | DictConfig:
    runtime_cfg = cfg.get("runtime", {}) if isinstance(cfg.get("runtime", {}), DictConfig) else cfg.get("runtime", {})
    ui_cfg = runtime_cfg.get("ui", {}) if isinstance(runtime_cfg, (dict, DictConfig)) else {}
    return ui_cfg if isinstance(ui_cfg, (dict, DictConfig)) else {}


def _tcp_port_open(host: str, port: int, timeout_sec: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_sec):
            return True
    except OSError:
        return False


def _ensure_replay_studio_server(cfg: DictConfig) -> None:
    ui_cfg = _runtime_ui_cfg(cfg)
    if not bool(ui_cfg.get("auto_start_replay_studio", True)):
        return
    if not bool(ui_cfg.get("auto_open_results", False)):
        return

    port = int(ui_cfg.get("replay_studio_preferred_port", 5173) or 5173)
    if _tcp_port_open("127.0.0.1", port):
        return

    repo_root = Path(__file__).resolve().parents[1]
    app_dir = repo_root / "replay_studio"
    package_json = app_dir / "package.json"
    if not package_json.exists():
        return

    npm = "npm.cmd" if os.name == "nt" else "npm"
    log_dir = repo_root / ".tooling" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if not (app_dir / "node_modules").exists() and bool(ui_cfg.get("replay_studio_auto_install", True)):
        try:
            with (log_dir / "replay_studio_install.stdout.log").open("a", encoding="utf-8") as stdout, (
                log_dir / "replay_studio_install.stderr.log"
            ).open("a", encoding="utf-8") as stderr:
                subprocess.run(
                    [npm, "install"],
                    cwd=str(app_dir),
                    check=True,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=180,
                )
        except Exception:
            return

    stdout = (log_dir / "replay_studio.stdout.log").open("a", encoding="utf-8")
    stderr = (log_dir / "replay_studio.stderr.log").open("a", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.Popen(
            [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(app_dir),
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception:
        return

    for _ in range(20):
        if _tcp_port_open("127.0.0.1", port):
            return
        time.sleep(0.25)


def _start_python_dist_server(*, repo_root: Path, app_dir: Path, port: int, log_dir: Path) -> None:
    dist_index = app_dir / "dist" / "index.html"
    server_script = repo_root / "scripts" / "serve_replay_studio_3d_dist.py"
    if not dist_index.exists() or not server_script.exists():
        return
    stdout = (log_dir / "replay_studio_3d_dist.stdout.log").open("a", encoding="utf-8")
    stderr = (log_dir / "replay_studio_3d_dist.stderr.log").open("a", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(server_script),
                "--repo-root",
                str(repo_root),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=str(repo_root),
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception:
        return


def _ensure_replay_studio_3d_server(cfg: DictConfig) -> None:
    ui_cfg = _runtime_ui_cfg(cfg)
    if not bool(ui_cfg.get("auto_start_replay_studio_3d", bool(ui_cfg.get("auto_start_replay_studio", True)))):
        return
    if not bool(ui_cfg.get("auto_open_results", False)):
        return

    port = int(ui_cfg.get("replay_studio_3d_preferred_port", 5174) or 5174)
    if _tcp_port_open("127.0.0.1", port):
        return

    repo_root = Path(__file__).resolve().parents[1]
    app_dir = repo_root / "replay_studio_3d"
    package_json = app_dir / "package.json"
    log_dir = repo_root / ".tooling" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    npm = "npm.cmd" if os.name == "nt" else "npm"
    if package_json.exists():
        stdout = (log_dir / "replay_studio_3d.stdout.log").open("a", encoding="utf-8")
        stderr = (log_dir / "replay_studio_3d.stderr.log").open("a", encoding="utf-8")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            subprocess.Popen(
                [npm, "run", "dev", "--", "--host", "127.0.0.1", "--port", str(port)],
                cwd=str(app_dir),
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            for _ in range(20):
                if _tcp_port_open("127.0.0.1", port):
                    return
                time.sleep(0.25)
        except Exception:
            pass

    _start_python_dist_server(repo_root=repo_root, app_dir=app_dir, port=port, log_dir=log_dir)
    for _ in range(20):
        if _tcp_port_open("127.0.0.1", port):
            return
        time.sleep(0.25)


def _open_selected_artifacts(output_root: Path, child_output_dir: Path, cfg: DictConfig) -> None:
    ui_cfg = _runtime_ui_cfg(cfg)
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
            for name in ("series_dashboard.html", "series_analysis.json", "knowledge/KNOWLEDGE.md", "knowledge/knowledge_graph.json", "llm_wiki_dashboard.html"):
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


def _export_replay_studio_assets(output_dir: Path) -> tuple[Path, Path]:
    script_path = Path(__file__).resolve().parents[1] / "replay_studio" / "examples" / "export_mansim_run.py"
    output_dir = Path(output_dir).resolve()
    output_log = (output_dir / "replay_studio_log.json").resolve()
    output_layout = (output_dir / "replay_studio_layout.json").resolve()
    subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--run-dir",
            str(output_dir),
            "--output-log",
            str(output_log),
            "--output-layout",
            str(output_layout),
        ],
        check=True,
        cwd=str(script_path.parent),
    )
    return output_log, output_layout


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
    operations_replay_path = export_operations_replay(output_dir=output_dir, events=events)
    manager_replay_path = export_manager_replay(output_dir=output_dir)
    manager_replay_json_path = output_dir / "manager_replay.json"
    replay_studio_log_path, replay_studio_layout_path = _export_replay_studio_assets(output_dir)
    llm_wiki_dashboard_raw = str(run_meta.get("llm_wiki_dashboard_path", "")).strip()
    llm_wiki_dashboard_path = Path(llm_wiki_dashboard_raw) if llm_wiki_dashboard_raw else output_dir / "llm_wiki_dashboard.html"
    if not llm_wiki_dashboard_path.exists():
        llm_wiki_dashboard_path = output_dir / "llm_wiki_dashboard.html"
    llm_graph_raw = str(run_meta.get("llm_graph_path", "")).strip()
    llm_graph_path = Path(llm_graph_raw) if llm_graph_raw else None
    graphify_tree_path = (llm_graph_path / "GRAPH_TREE.html") if llm_graph_path is not None else None
    graphify_graph_html_path = (llm_graph_path / "graph.html") if llm_graph_path is not None else None
    graphify_graph_raw_path = graphify_tree_path if graphify_tree_path is not None and graphify_tree_path.exists() else graphify_graph_html_path
    graphify_graph_json_path = (llm_graph_path / "graph.json") if llm_graph_path is not None else None
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
    graph_dashboard_path = export_llm_graph_dashboard(
        output_dir=output_dir,
        graph_html_path=graphify_graph_raw_path if graphify_graph_raw_path is not None and graphify_graph_raw_path.exists() else None,
        graph_json_path=graphify_graph_json_path if graphify_graph_json_path is not None and graphify_graph_json_path.exists() else None,
        manifest=manifest,
        manifest_path=manifest_path,
        current_run_id=current_run_id,
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
        "operations_replay_dashboard_path": str(operations_replay_path.resolve()),
        "manager_replay_dashboard_path": str(manager_replay_path.resolve()) if manager_replay_path is not None else "",
        "manager_replay_json_path": str(manager_replay_json_path.resolve()) if manager_replay_json_path.exists() else "",
        "replay_studio_log_path": str(replay_studio_log_path.resolve()),
        "replay_studio_layout_path": str(replay_studio_layout_path.resolve()),
        "knowledge_dashboard_path": str(knowledge_path.resolve()),
        "llm_wiki_dashboard_path": str(llm_wiki_dashboard_path.resolve()) if llm_wiki_dashboard_path.exists() else "",
        "graphify_graph_path": str(graph_dashboard_path.resolve()) if graph_dashboard_path.exists() else "",
        "graphify_graph_raw_path": str(graphify_graph_raw_path.resolve()) if graphify_graph_raw_path is not None and graphify_graph_raw_path.exists() else "",
        "knowledge_graph_path": str(graphify_graph_json_path.resolve()) if graphify_graph_json_path is not None and graphify_graph_json_path.exists() else "",
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


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _bool_cfg(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _apply_series_run_seed(
    *,
    child_cfg: dict[str, Any],
    orchestration_cfg: dict[str, Any],
    run_index: int,
) -> dict[str, int | bool]:
    """Assign a deterministic but distinct seed to each run in a series."""

    base_seed = _coerce_int(child_cfg.get("seed", 7), 7)
    vary_seed = _bool_cfg(orchestration_cfg.get("vary_seed_by_run", False), False)
    seed_stride = max(1, _coerce_int(orchestration_cfg.get("seed_stride", 1), 1))
    run_seed = base_seed + ((max(1, int(run_index)) - 1) * seed_stride) if vary_seed else base_seed
    child_cfg["seed"] = int(run_seed)

    decision_cfg = child_cfg.get("decision", {}) if isinstance(child_cfg.get("decision", {}), dict) else {}
    llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
    sync_llm_seed = _bool_cfg(orchestration_cfg.get("sync_llm_seed_with_run_seed", True), True)
    if sync_llm_seed and isinstance(llm_cfg, dict) and "seed" in llm_cfg:
        llm_cfg["seed"] = int(run_seed)

    return {
        "base_seed": int(base_seed),
        "run_seed": int(run_seed),
        "vary_seed_by_run": bool(vary_seed),
        "seed_stride": int(seed_stride),
        "sync_llm_seed_with_run_seed": bool(sync_llm_seed),
    }


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
    ui_cfg = _runtime_ui_cfg(cfg)
    manifest = build_dashboard_manifest(
        root_output_dir=runtime_output_dir,
        summary_payload=summary_payload,
        analysis_payload=analysis,
        streamlit_port=int(ui_cfg.get("streamlit_preferred_port", 8505) or 8505),
        replay_studio_port=int(ui_cfg.get("replay_studio_preferred_port", 5173) or 5173),
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
            manifest_path=root_manifest_path,
            current_run_id=current_run_id,
            analysis=analysis,
        )
        run_entry["results_dashboard_path"] = exported["results_dashboard_path"]
        run_entry["replay_dashboard_path"] = exported["replay_dashboard_path"]
        run_entry["operations_replay_dashboard_path"] = exported["operations_replay_dashboard_path"]
        run_entry["manager_replay_dashboard_path"] = exported.get("manager_replay_dashboard_path", "")
        run_entry["manager_replay_json_path"] = exported.get("manager_replay_json_path", "")
        run_entry["knowledge_dashboard_path"] = exported["knowledge_dashboard_path"]
        run_entry["llm_wiki_dashboard_path"] = exported.get("llm_wiki_dashboard_path", "")
        run_entry["graphify_graph_path"] = exported.get("graphify_graph_path", "")
        run_entry["graphify_graph_raw_path"] = exported.get("graphify_graph_raw_path", "")
        run_entry["knowledge_graph_path"] = exported.get("knowledge_graph_path", "")
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
        replay_studio_port=int(manifest.get("replay_studio_preferred_port", 5173) or 5173),
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


def _safe_experiment_id(runtime_output_dir: Path) -> str:
    resolved = runtime_output_dir.resolve()
    parts = resolved.parts
    raw = f"{parts[-2]}_{parts[-1]}" if len(parts) >= 2 else resolved.name
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")
    return safe or "experiment"


@hydra.main(config_path="../configs", config_name="config", version_base="1.3")
def main(cfg: DictConfig) -> None:
    experiment_cfg = build_legacy_experiment_cfg(cfg)
    runtime_output_dir = Path(HydraConfig.get().runtime.output_dir)
    runtime_output_dir.mkdir(parents=True, exist_ok=True)

    decision_cfg = experiment_cfg.get("decision", {}) if isinstance(experiment_cfg.get("decision", {}), dict) else {}
    decision_mode = normalize_decision_mode(str(decision_cfg.get("mode", "adaptive_priority")))
    llm_cfg = decision_cfg.get("llm", {}) if isinstance(decision_cfg.get("llm", {}), dict) else {}
    if isinstance(llm_cfg, dict):
        knowledge_cfg = llm_cfg.get("knowledge", {}) if isinstance(llm_cfg.get("knowledge", {}), dict) else {}
        if bool(knowledge_cfg.get("enabled", False)):
            raw_root = str(knowledge_cfg.get("root", "knowledge/llm_knowledge") or "knowledge/llm_knowledge").strip()
            base_root = Path(raw_root)
            if not base_root.is_absolute():
                base_root = Path(str(HydraConfig.get().runtime.cwd)) / base_root
            base_root = base_root.resolve()
            scope = str(knowledge_cfg.get("experiment_scope", "auto") or "auto").strip().lower()
            experiment_id = _safe_experiment_id(runtime_output_dir)
            root_path = base_root if scope in {"shared", "global"} else base_root / "experiments" / experiment_id
            knowledge_cfg["base_root"] = str(base_root)
            knowledge_cfg["experiment_id"] = experiment_id if scope not in {"shared", "global"} else "shared"
            knowledge_cfg["root"] = str(root_path.resolve())
            llm_cfg["knowledge"] = knowledge_cfg
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
        seed_info = _apply_series_run_seed(
            child_cfg=child_cfg,
            orchestration_cfg=orchestration_cfg,
            run_index=run_index,
        )
        if orchestration_active:
            child_cfg["_run_series"] = {
                "run_index": run_index,
                "total_runs": run_count,
                "parent_output_dir": str(runtime_output_dir.resolve()),
                "knowledge_path": str(knowledge_store.markdown_path.resolve()),
                "knowledge_history_dir": str(knowledge_history_dir.resolve()),
                "base_seed": int(seed_info["base_seed"]),
                "run_seed": int(seed_info["run_seed"]),
                "vary_seed_by_run": bool(seed_info["vary_seed_by_run"]),
                "seed_stride": int(seed_info["seed_stride"]),
                "sync_llm_seed_with_run_seed": bool(seed_info["sync_llm_seed_with_run_seed"]),
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
            "llm_knowledge_base_root": str(result.get("llm_knowledge_base_root", "")),
            "llm_knowledge_experiment_id": str(result.get("llm_knowledge_experiment_id", "")),
            "llm_knowledge_root": str(result.get("llm_knowledge_root", "")),
            "llm_wiki_path": str(result.get("llm_wiki_path", "")),
            "llm_graph_path": str(result.get("llm_graph_path", "")),
            "llm_wiki_dashboard_path": exported.get("llm_wiki_dashboard_path", str(result.get("llm_wiki_dashboard_path", ""))),
            "graphify_graph_path": exported.get("graphify_graph_path", ""),
            "knowledge_graph_path": exported.get("knowledge_graph_path", ""),
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
            "operations_replay_dashboard_path": exported["operations_replay_dashboard_path"],
            "manager_replay_dashboard_path": exported.get("manager_replay_dashboard_path", ""),
            "manager_replay_json_path": exported.get("manager_replay_json_path", ""),
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
    _ensure_replay_studio_server(cfg)
    _ensure_replay_studio_3d_server(cfg)
    _open_selected_artifacts(runtime_output_dir, child_output_dir, cfg)
    print(json.dumps(last_result["kpi"], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
