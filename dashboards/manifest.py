from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


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


def _normalize_label(text: Any) -> str:
    return str(text if text is not None else "").strip()


_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)


def parse_markdown_bullet_sections(markdown: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    matches = list(_SECTION_RE.finditer(markdown or ""))
    for idx, match in enumerate(matches):
        title = _normalize_label(match.group("title"))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        chunk = (markdown[start:end] if markdown else "").strip()
        items: list[str] = []
        for line in chunk.splitlines():
            clean = line.strip()
            if clean.startswith("- "):
                item = clean[2:].strip()
                if item:
                    items.append(item)
        sections[title] = items
    return sections


def load_knowledge_sections(root_output_dir: Path) -> dict[str, list[str]]:
    knowledge_path = Path(root_output_dir) / "knowledge.md"
    try:
        markdown = knowledge_path.read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    sections = parse_markdown_bullet_sections(markdown)
    return {
        "persistent_lessons": sections.get("Persistent Lessons", []),
        "latest_lessons": sections.get("Latest Lessons", []),
        "detector_guidance": sections.get("Detector Guidance", []),
        "planner_guidance": sections.get("Planner Guidance", []),
        "open_watchouts": sections.get("Open Watchouts", []),
    }


def _artifact_map(output_dir: Path, row: dict[str, Any]) -> dict[str, str]:
    def _pick(path_value: Any, fallback_name: str) -> str:
        text = _normalize_label(path_value)
        if text:
            return str(Path(text).resolve())
        return str((output_dir / fallback_name).resolve())

    return {
        "results_dashboard.html": _pick(row.get("results_dashboard_path", ""), "results_dashboard.html"),
        "kpi_dashboard.html": _pick(row.get("kpi_dashboard_path", ""), "kpi_dashboard.html"),
        "gantt.html": _pick(row.get("gantt_path", ""), "gantt.html"),
        "task_priority_dashboard.html": _pick(row.get("task_priority_dashboard_path", ""), "task_priority_dashboard.html"),
        "knowledge_dashboard.html": _pick(row.get("knowledge_dashboard_path", ""), "knowledge_dashboard.html"),
        "reasoning_dashboard.html": _pick(row.get("reasoning_dashboard_path", ""), "reasoning_dashboard.html"),
        "replay_dashboard.html": _pick(row.get("replay_dashboard_path", ""), "replay_dashboard.html"),
        "events.jsonl": _pick(row.get("events_path", ""), "events.jsonl"),
        "daily_summary.json": _pick(row.get("daily_summary_path", ""), "daily_summary.json"),
        "run_reflection.json": _pick(row.get("run_reflection_path", ""), "run_reflection.json"),
        "run_reflection.md": _pick(row.get("run_reflection_markdown_path", ""), "run_reflection.md"),
        "run_meta.json": _pick(row.get("run_meta_path", ""), "run_meta.json"),
        "kpi.json": _pick(row.get("kpi_path", ""), "kpi.json"),
        "orchestration_intelligence_dashboard.html": _pick(
            row.get("orchestration_intelligence_dashboard_path", ""),
            "orchestration_intelligence_dashboard.html",
        ),
        "llm_trace.html": _pick(row.get("llm_trace_path", ""), "llm_trace.html"),
    }


def build_dashboard_manifest(
    *,
    root_output_dir: Path,
    summary_payload: dict[str, Any] | None = None,
    analysis_payload: dict[str, Any] | None = None,
    streamlit_port: int = 8505,
) -> dict[str, Any]:
    root_output_dir = Path(root_output_dir)
    summary = summary_payload if isinstance(summary_payload, dict) else (_safe_json(root_output_dir / "run_series_summary.json") or {})
    analysis = analysis_payload if isinstance(analysis_payload, dict) else (_safe_json(root_output_dir / "series_analysis.json") or {})
    runs_raw = summary.get("runs", []) if isinstance(summary.get("runs", []), list) else []
    knowledge_sections = analysis.get("knowledge_sections", {}) if isinstance(analysis.get("knowledge_sections", {}), dict) else load_knowledge_sections(root_output_dir)

    runs: list[dict[str, Any]] = []
    for idx, row in enumerate(runs_raw, start=1):
        if not isinstance(row, dict):
            continue
        run_index = _safe_int(row.get("run_index"), idx)
        run_id = f"run_{run_index:02d}"
        output_dir = Path(_normalize_label(row.get("output_dir")) or (root_output_dir / run_id))
        kpi = _safe_json(Path(_normalize_label(row.get("kpi_path")) or (output_dir / "kpi.json"))) or {}
        run_meta = _safe_json(Path(_normalize_label(row.get("run_meta_path")) or (output_dir / "run_meta.json"))) or {}
        daily_payload = _safe_json(Path(_normalize_label(row.get("daily_summary_path")) or (output_dir / "daily_summary.json"))) or {}
        reflection = _safe_json(Path(_normalize_label(row.get("run_reflection_path")) or (output_dir / "run_reflection.json"))) or {}
        daily_rows = daily_payload.get("days", []) if isinstance(daily_payload.get("days", []), list) else []
        last_day = daily_rows[-1] if daily_rows else {}
        llm_meta = run_meta.get("llm", {}) if isinstance(run_meta.get("llm", {}), dict) else {}
        transport = llm_meta.get("transport_metrics", {}) if isinstance(llm_meta.get("transport_metrics", {}), dict) else {}
        runs.append(
            {
                "id": run_id,
                "label": run_id,
                "run_index": run_index,
                "output_dir": str(output_dir.resolve()),
                "artifacts": _artifact_map(output_dir, row),
                "kpi": {
                    "total_products": _safe_int(kpi.get("total_products"), _safe_int(row.get("total_products"))),
                    "downstream_closure_ratio": _safe_float(kpi.get("downstream_closure_ratio"), _safe_float(row.get("downstream_closure_ratio"))),
                    "machine_broken_ratio": _safe_float(kpi.get("machine_broken_ratio")),
                    "machine_pm_ratio": _safe_float(kpi.get("machine_pm_ratio")),
                    "physical_incident_total": _safe_int(kpi.get("physical_incident_total")),
                    "coordination_incident_total": _safe_int(kpi.get("coordination_incident_total")),
                    "unique_replan_blocker_total": _safe_int(kpi.get("unique_replan_blocker_total")),
                    "planner_escalation_total": _safe_int(kpi.get("planner_escalation_total")),
                    "commitment_dispatch_total": _safe_int(kpi.get("commitment_dispatch_total")),
                    "completed_product_lead_time_avg_min": _safe_float(kpi.get("completed_product_lead_time_avg_min")),
                    "product_input_wait_avg_min": _safe_float((kpi.get("buffer_wait_avg_min_including_open", {}) or {}).get("product_input")),
                    "wall_clock_sec": _safe_float(kpi.get("wall_clock_sec"), _safe_float(row.get("wall_clock_sec"))),
                },
                "reflection_summary": _normalize_label(reflection.get("summary")),
                "reflection": reflection,
                "run_meta": run_meta,
                "transport_metrics": transport,
                "daily": {
                    "count": len(daily_rows),
                    "rows": daily_rows,
                    "last_day": last_day if isinstance(last_day, dict) else {},
                },
                "knowledge_in_path": _normalize_label(row.get("knowledge_in_path")),
                "knowledge_out_path": _normalize_label(row.get("knowledge_out_path")),
                "evaluator_enabled": bool(row.get("evaluator_enabled", False)),
            }
        )

    manifest: dict[str, Any] = {
        "version": "v0.4",
        "series_root": str(root_output_dir.resolve()),
        "single_run": len(runs) <= 1,
        "streamlit_preferred_port": int(streamlit_port),
        "requested_run_count": _safe_int(summary.get("requested_run_count"), len(runs)),
        "completed_run_count": _safe_int(summary.get("completed_run_count"), len(runs)),
        "current_run": runs[-1]["id"] if runs else "",
        "runs": runs,
        "analysis": {
            "knowledge_effect_classification": _normalize_label(analysis.get("knowledge_effect_classification")) or "mixed",
            "performance_pattern": _normalize_label(analysis.get("performance_pattern")) or "insufficient_data",
            "analysis_summary": _normalize_label(analysis.get("analysis_summary")),
            "peak_run": analysis.get("peak_run", {}) if isinstance(analysis.get("peak_run", {}), dict) else {},
            "worst_run": analysis.get("worst_run", {}) if isinstance(analysis.get("worst_run", {}), dict) else {},
            "baseline_vs_best": analysis.get("baseline_vs_best", {}) if isinstance(analysis.get("baseline_vs_best", {}), dict) else {},
            "baseline_vs_final": analysis.get("baseline_vs_final", {}) if isinstance(analysis.get("baseline_vs_final", {}), dict) else {},
            "lesson_stability": analysis.get("lesson_stability", {}) if isinstance(analysis.get("lesson_stability", {}), dict) else {},
        },
        "knowledge_sections": knowledge_sections,
    }
    return manifest


def write_dashboard_manifests(*, root_output_dir: Path, manifest: dict[str, Any]) -> dict[str, str]:
    root_output_dir = Path(root_output_dir)
    root_output_dir.mkdir(parents=True, exist_ok=True)
    root_manifest_path = root_output_dir / "dashboard_manifest.json"
    root_manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    run_manifest_paths: dict[str, str] = {}
    runs = manifest.get("runs", []) if isinstance(manifest.get("runs", []), list) else []
    for run in runs:
        if not isinstance(run, dict):
            continue
        output_dir = Path(_normalize_label(run.get("output_dir")))
        output_dir.mkdir(parents=True, exist_ok=True)
        run_manifest = copy.deepcopy(manifest)
        run_manifest["current_run"] = _normalize_label(run.get("id"))
        run_manifest_path = output_dir / "dashboard_manifest.json"
        run_manifest_path.write_text(json.dumps(run_manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        run_manifest_paths[_normalize_label(run.get("id"))] = str(run_manifest_path.resolve())
    return {
        "root": str(root_manifest_path.resolve()),
        "runs": run_manifest_paths,
    }
