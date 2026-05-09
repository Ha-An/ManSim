from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _slug(text: Any) -> str:
    raw = str(text if text is not None else "").strip()
    if not raw:
        return "item"
    allowed: list[str] = []
    for ch in raw:
        if ch.isalnum():
            allowed.append(ch)
        elif ch in {" ", "-", "_", "."}:
            allowed.append("-")
    slug = "".join(allowed).strip("-").replace("--", "-")
    return slug or "item"


def _json_dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)


def _yaml_scalar(value: Any) -> str:
    text = str(value if value is not None else "").replace('"', '\\"')
    return f'"{text}"'


def _frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        elif isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_yaml_scalar(item)}")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _coerce_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _coerce_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_text(value: Any, *, max_len: int = 220) -> str:
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_len:
        return text[: max(0, max_len - 3)].rstrip() + "..."
    return text


def _string_items(value: Any, *, limit: int = 5, max_len: int = 220) -> list[str]:
    rows: list[str] = []
    values = value if isinstance(value, list) else []
    for item in values:
        text = _clean_text(item, max_len=max_len)
        if text:
            rows.append(text)
        if len(rows) >= max(1, limit):
            break
    return rows


def _bullet_list(items: list[str], *, empty: str = "No curated note yet.") -> str:
    rows = [f"- {item}" for item in items if str(item).strip()]
    return "\n".join(rows) if rows else f"- {empty}"


def _group_bullets(groups: dict[str, Any], *, limit: int = 4) -> str:
    rows: list[str] = []
    for key, value in groups.items():
        items = _string_items(value, limit=limit)
        if not items:
            continue
        rows.append(f"### {key.replace('_', ' ').title()}")
        rows.append(_bullet_list(items))
    return "\n\n".join(rows) if rows else "- No curated grouped notes yet."


def _compact_mapping(mapping: Any, *, limit: int = 8) -> list[str]:
    src = mapping if isinstance(mapping, dict) else {}
    rows: list[str] = []
    for key, value in src.items():
        if isinstance(value, (dict, list)):
            text = _clean_text(_json_dump(value), max_len=120)
        else:
            text = _clean_text(value, max_len=120)
        rows.append(f"{key}: {text}")
        if len(rows) >= max(1, limit):
            break
    return rows


class LLMWikiStore:
    """Obsidian-compatible LLM wiki store for ManSim run knowledge."""

    def __init__(
        self,
        root_dir: Path | str,
        *,
        graphify_command: str | None = None,
        graphify_timeout_sec: int = 300,
        graphify_backend: str | None = None,
        graphify_model: str | None = None,
        graphify_base_url: str | None = None,
        graphify_api_key: str | None = None,
        graphify_max_output_tokens: int | None = None,
        graphify_no_cluster: bool = False,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.raw_dir = self.root_dir / "raw"
        self.wiki_dir = self.root_dir / "wiki"
        self.graph_dir = self.root_dir / "graph"
        self.trace_dir = self.root_dir / "curator_trace"
        self.graphify_command_parts = self._normalize_graphify_command(graphify_command)
        self.graphify_timeout_sec = max(30, int(graphify_timeout_sec or 300))
        self.graphify_backend = str(graphify_backend or "").strip()
        self.graphify_model = str(graphify_model or "").strip()
        self.graphify_base_url = str(graphify_base_url or "").strip()
        self.graphify_api_key = str(graphify_api_key or "").strip()
        self.graphify_max_output_tokens = int(graphify_max_output_tokens or 0)
        self.graphify_no_cluster = bool(graphify_no_cluster)
        self.ensure_layout()

    @staticmethod
    def _normalize_graphify_command(raw_command: str | None) -> list[str]:
        raw = str(raw_command or "").strip()
        if not raw:
            return [sys.executable, "-m", "graphify"]
        try:
            parts = shlex.split(raw, posix=(os.name != "nt"))
        except ValueError:
            parts = [raw]
        return parts or [sys.executable, "-m", "graphify"]

    def ensure_layout(self) -> None:
        for path in (self.raw_dir, self.wiki_dir, self.graph_dir, self.trace_dir):
            path.mkdir(parents=True, exist_ok=True)
        obsidian_dir = self.wiki_dir / ".obsidian"
        obsidian_dir.mkdir(parents=True, exist_ok=True)
        app_config = obsidian_dir / "app.json"
        if not app_config.exists():
            app_config.write_text(
                json.dumps(
                    {
                        "alwaysUpdateLinks": True,
                        "newLinkFormat": "shortest",
                        "useMarkdownLinks": False,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        self.register_obsidian_vault()
        self._write_index_if_missing()

    def register_obsidian_vault(self) -> None:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return
        config_path = Path(appdata) / "obsidian" / "obsidian.json"
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        vaults = payload.get("vaults", {})
        if not isinstance(vaults, dict):
            vaults = {}
        vault_path = str(self.wiki_dir.resolve())
        vault_id = hashlib.md5(vault_path.casefold().encode("utf-8")).hexdigest()[:16]
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        existing_id = ""
        for key, value in vaults.items():
            if isinstance(value, dict) and str(value.get("path", "")).casefold() == vault_path.casefold():
                existing_id = str(key)
                break
        vaults[existing_id or vault_id] = {"path": vault_path, "ts": now_ms, "open": True}
        payload["vaults"] = vaults
        try:
            config_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        except OSError:
            return

    def _write_index_if_missing(self) -> None:
        index = self.wiki_dir / "00_Index.md"
        if index.exists():
            return
        self._write_markdown(
            index,
            _frontmatter(
                {
                    "kind": "index",
                    "title": "ManSim LLM Wiki",
                    "updated_at": _utc_now_iso(),
                }
            )
            + "\n\n# ManSim LLM Wiki\n\n"
            + "## Runs\n\n"
            + "- No runs recorded yet.\n\n"
            + "## Manager Pages\n\n"
            + "- [[Managers/Strategist|Strategist]]\n"
            + "- [[Managers/Reviewer|Reviewer]]\n"
            + "- [[Managers/Curator|Curator]]\n\n"
            + "## Managed Objects\n\n"
            + "- [[Managed/Queues/Inspection Output|Inspection Output Queue]]\n"
            + "- [[Managed/Equipment/Inspection Workbench|Inspection Workbench]]\n",
        )

    def _write_markdown(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json_dump(payload) + "\n", encoding="utf-8")

    @staticmethod
    def run_label(run_index: int) -> str:
        return f"Run-{max(1, int(run_index)):04d}"

    @staticmethod
    def day_label(day: int) -> str:
        return f"Day-{max(1, int(day)):04d}"

    def _run_raw_dir(self, run_index: int) -> Path:
        return self.raw_dir / self.run_label(run_index)

    def _day_raw_dir(self, run_index: int, day: int) -> Path:
        return self._run_raw_dir(run_index) / self.day_label(day)

    def _run_page_path(self, run_index: int) -> Path:
        return self.wiki_dir / "Runs" / f"{self.run_label(run_index)}.md"

    def _day_page_path(self, run_index: int, day: int) -> Path:
        return self.wiki_dir / "Days" / self.run_label(run_index) / f"{self.day_label(day)}.md"

    def _manager_page_path(self, manager_name: str) -> Path:
        return self.wiki_dir / "Managers" / f"{manager_name}.md"

    def write_run_raw(
        self,
        *,
        run_index: int,
        run_output_dir: Path | str,
        run_meta: dict[str, Any],
        kpi: dict[str, Any],
        daily_summaries: list[dict[str, Any]],
        reflection: dict[str, Any] | None = None,
    ) -> None:
        run_dir = self._run_raw_dir(run_index)
        self._write_json(run_dir / "run_meta.json", run_meta)
        self._write_json(run_dir / "kpi.json", kpi)
        self._write_json(run_dir / "daily_summary.json", {"days": daily_summaries})
        if isinstance(reflection, dict) and reflection:
            self._write_json(run_dir / "run_reflection.json", reflection)
        llm_exchange = Path(run_output_dir) / "llm_exchange.json"
        if llm_exchange.exists():
            shutil.copy2(llm_exchange, run_dir / "llm_exchange.json")
        self.render_run_page(
            run_index=run_index,
            run_meta=run_meta,
            kpi=kpi,
            daily_summaries=daily_summaries,
            reflection=reflection if isinstance(reflection, dict) else None,
        )
        self.render_index()

    def write_daily_update(
        self,
        *,
        run_index: int,
        day: int,
        day_summary: dict[str, Any],
        shift_policy: dict[str, Any],
        reviewer_report: dict[str, Any],
        curator_report: dict[str, Any],
        source_output_dir: Path | str | None = None,
    ) -> None:
        day_dir = self._day_raw_dir(run_index, day)
        bundle = {
            "run_index": int(run_index),
            "day": int(day),
            "updated_at": _utc_now_iso(),
            "day_summary": day_summary,
            "shift_policy": shift_policy,
            "reviewer_report": reviewer_report,
            "curator_report": curator_report,
        }
        self._write_json(day_dir / "day_bundle.json", bundle)
        self._write_json(day_dir / "day_summary.json", day_summary)
        self._write_json(day_dir / "shift_policy.json", shift_policy)
        self._write_json(day_dir / "daily_review.json", reviewer_report)
        self._write_json(day_dir / "curator_report.json", curator_report)
        self._write_json(self.trace_dir / f"{self.run_label(run_index)}_{self.day_label(day)}.json", bundle)
        if source_output_dir is not None:
            self._copy_day_events(source_output_dir=Path(source_output_dir), run_index=run_index, day=day)
        self.render_day_page(
            run_index=run_index,
            day=day,
            day_summary=day_summary,
            shift_policy=shift_policy,
            reviewer_report=reviewer_report,
            curator_report=curator_report,
        )
        self.render_manager_pages(run_index=run_index, day=day, shift_policy=shift_policy, reviewer_report=reviewer_report, curator_report=curator_report)
        self.render_managed_object_pages(
            run_index=run_index,
            day=day,
            day_summary=day_summary,
            shift_policy=shift_policy,
            reviewer_report=reviewer_report,
            curator_report=curator_report,
        )
        self.render_index()

    def _copy_day_events(self, *, source_output_dir: Path, run_index: int, day: int) -> None:
        events_path = source_output_dir / "events.jsonl"
        if not events_path.exists():
            return
        out_path = self._day_raw_dir(run_index, day) / "events_slice.jsonl"
        copied = 0
        with events_path.open("r", encoding="utf-8", errors="replace") as src, out_path.open("w", encoding="utf-8") as dst:
            for line in src:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if int(payload.get("day", 0) or 0) != int(day):
                    continue
                dst.write(json.dumps(payload, ensure_ascii=False) + "\n")
                copied += 1
                if copied >= 4000:
                    break

    def render_index(self) -> None:
        run_links: list[str] = []
        runs_dir = self.wiki_dir / "Runs"
        if runs_dir.exists():
            for path in sorted(runs_dir.glob("Run-*.md")):
                label = path.stem
                run_links.append(f"- [[Runs/{label}|{label}]]")
        if not run_links:
            run_links = ["- No runs recorded yet."]
        text = (
            _frontmatter({"kind": "index", "title": "ManSim LLM Wiki", "updated_at": _utc_now_iso()})
            + "\n\n# ManSim LLM Wiki\n\n"
            + "## Runs\n\n"
            + "\n".join(run_links)
            + "\n\n## Manager Pages\n\n"
            + "- [[Managers/Strategist|Strategist]]\n"
            + "- [[Managers/Reviewer|Reviewer]]\n"
            + "- [[Managers/Curator|Curator]]\n\n"
            + "## Managed Objects\n\n"
            + "- [[Managed/Queues/Inspection Output|Inspection Output Queue]]\n"
            + "- [[Managed/Equipment/Inspection Workbench|Inspection Workbench]]\n"
            + "- [[Managed/Equipment/Machines|Machines]]\n"
            + "- [[Managed/Workers/Workers|Workers]]\n"
        )
        self._write_markdown(self.wiki_dir / "00_Index.md", text)

    def render_run_page(
        self,
        *,
        run_index: int,
        run_meta: dict[str, Any],
        kpi: dict[str, Any],
        daily_summaries: list[dict[str, Any]],
        reflection: dict[str, Any] | None = None,
    ) -> None:
        run = self.run_label(run_index)
        day_links = [
            f"- [[Days/{run}/{self.day_label(_safe_int(row.get('day'), idx))}|{self.day_label(_safe_int(row.get('day'), idx))}]]: products={_safe_int(row.get('products'))}, inspection_passes={_safe_int(row.get('inspection_passes'))}"
            for idx, row in enumerate(daily_summaries, start=1)
            if isinstance(row, dict)
        ]
        if not day_links:
            day_links = ["- No day summaries recorded yet."]
        reflection = reflection if isinstance(reflection, dict) else {}
        run_lessons = _string_items(reflection.get("carry_forward_lessons", []), limit=5)
        detector_guidance = _string_items(reflection.get("detector_guidance", []), limit=4)
        planner_guidance = _string_items(reflection.get("planner_guidance", []), limit=4)
        open_watchouts = _string_items(reflection.get("open_watchouts", []), limit=4)
        total_products = _safe_int(kpi.get("total_products"))
        closure_ratio = _safe_float(kpi.get("downstream_closure_ratio"))
        inspection_passes = _safe_int(kpi.get("inspection_passes"))
        inspection_backlog = _safe_int(kpi.get("inspection_backlog_end"))
        daily_products = [
            str(_safe_int(row.get("products")))
            for row in daily_summaries
            if isinstance(row, dict)
        ]
        text = (
            _frontmatter({"kind": "run", "run": run, "updated_at": _utc_now_iso(), "sources": [f"raw/{run}/kpi.json"]})
            + f"\n\n# {run}\n\n"
            + "## Operating Outcome\n\n"
            + _bullet_list(
                [
                    f"Accepted products: {total_products}.",
                    f"Daily accepted products: {', '.join(daily_products) if daily_products else 'not recorded'}.",
                    f"Downstream closure ratio: {closure_ratio:.6f}.",
                    f"Inspection passes: {inspection_passes}; inspection backlog ended at {inspection_backlog}.",
                    f"Decision mode: {_clean_text(run_meta.get('decision_mode', ''), max_len=80)}.",
                ]
            )
            + "\n\n## Run-Level Lessons\n\n"
            + _bullet_list(run_lessons, empty="No run reflection lesson recorded yet.")
            + "\n\n## Planner Guidance\n\n"
            + _bullet_list(planner_guidance, empty="No planner guidance recorded yet.")
            + "\n\n## Detector Guidance\n\n"
            + _bullet_list(detector_guidance, empty="No detector guidance recorded yet.")
            + "\n\n## Open Watchouts\n\n"
            + _bullet_list(open_watchouts, empty="No open watchout recorded yet.")
            + "\n\n## Raw Sources\n\n"
            + f"- KPI: `raw/{run}/kpi.json`\n"
            + f"- Daily summary: `raw/{run}/daily_summary.json`\n"
            + f"- Run reflection: `raw/{run}/run_reflection.json`\n"
            + "\n## Days\n\n"
            + "\n".join(day_links)
            + "\n"
        )
        self._write_markdown(self._run_page_path(run_index), text)

    def render_day_page(
        self,
        *,
        run_index: int,
        day: int,
        day_summary: dict[str, Any],
        shift_policy: dict[str, Any],
        reviewer_report: dict[str, Any],
        curator_report: dict[str, Any],
    ) -> None:
        run = self.run_label(run_index)
        day_name = self.day_label(day)
        managed_links = "\n".join(
            [
                "- [[Managed/Queues/Inspection Output|Inspection Output Queue]]",
                "- [[Managed/Equipment/Inspection Workbench|Inspection Workbench]]",
                "- [[Managed/Workers/Workers|Workers]]",
            ]
        )
        manager_links = "\n".join(
            [
                "- [[Managers/Strategist|Strategist]]",
                "- [[Managers/Reviewer|Reviewer]]",
                "- [[Managers/Curator|Curator]]",
            ]
        )
        products = _safe_int(day_summary.get("products"))
        inspection_passes = _safe_int(day_summary.get("inspection_passes"))
        closeout_gap = max(0, inspection_passes - products)
        policy_focus = _clean_text(shift_policy.get("operating_focus", ""), max_len=80)
        worker_roles = _compact_mapping(shift_policy.get("worker_roles", {}), limit=5)
        daily_targets = _compact_mapping(shift_policy.get("daily_targets", {}), limit=5)
        prevention_targets = _string_items(shift_policy.get("prevention_targets", []), limit=4)
        reviewer_signals = (
            _string_items(reviewer_report.get("top_failure_modes", []), limit=4)
            + _string_items(reviewer_report.get("target_misses", []), limit=4)
            + _string_items(reviewer_report.get("carry_forward_risks", []), limit=4)
        )
        curator_updates = _string_items(curator_report.get("daily_updates", []), limit=5)
        manager_lessons = _coerce_dict(curator_report.get("manager_lessons"))
        managed_updates = _coerce_dict(curator_report.get("managed_object_updates"))
        graph_hints = _string_items(curator_report.get("graph_hints", []), limit=5)
        text = (
            _frontmatter(
                {
                    "kind": "day",
                    "run": run,
                    "day": day_name,
                    "updated_at": _utc_now_iso(),
                    "sources": [f"raw/{run}/{day_name}/day_bundle.json"],
                }
            )
            + f"\n\n# {run} {day_name}\n\n"
            + "## Operating Outcome\n\n"
            + _bullet_list(
                [
                    f"Accepted products: {products}.",
                    f"Inspection passes: {inspection_passes}; closeout gap: {closeout_gap}.",
                    f"Inspection backlog end: {_safe_int(day_summary.get('inspection_backlog_end'))}.",
                    f"Station 2 output buffer end: {_safe_int(day_summary.get('station2_output_buffer_end'))}.",
                    f"Machine breakdowns: {_safe_int(day_summary.get('machine_breakdowns'))}; discharged workers: {_safe_int(day_summary.get('agent_discharged_count'))}.",
                ]
            )
            + "\n\n## Curated Operational Knowledge\n\n"
            + _bullet_list(curator_updates, empty="No curated daily operational note yet.")
            + "\n\n## Policy Used\n\n"
            + _bullet_list(
                [
                    f"Operating focus: {policy_focus or 'not recorded'}.",
                    "Worker roles: " + ("; ".join(worker_roles) if worker_roles else "not recorded") + ".",
                    "Prevention targets: " + (", ".join(prevention_targets) if prevention_targets else "not recorded") + ".",
                    "Daily targets: " + ("; ".join(daily_targets) if daily_targets else "not recorded") + ".",
                ]
            )
            + "\n\n## Reviewer Signals\n\n"
            + _bullet_list(list(dict.fromkeys(reviewer_signals))[:8], empty="No reviewer signal recorded yet.")
            + "\n\n## Manager Lessons\n\n"
            + _group_bullets(manager_lessons)
            + "\n\n## Managed Object Updates\n\n"
            + _group_bullets(managed_updates)
            + "\n\n## Graph Hints\n\n"
            + _bullet_list(graph_hints, empty="No graph hint recorded yet.")
            + "\n\n## Manager Perspective\n\n"
            + manager_links
            + "\n\n## Managed Object Perspective\n\n"
            + managed_links
            + "\n\n## Raw Sources\n\n"
            + f"- Day bundle: `raw/{run}/{day_name}/day_bundle.json`\n"
            + f"- Day summary: `raw/{run}/{day_name}/day_summary.json`\n"
            + f"- Shift policy: `raw/{run}/{day_name}/shift_policy.json`\n"
            + f"- Reviewer report: `raw/{run}/{day_name}/daily_review.json`\n"
            + f"- Curator report: `raw/{run}/{day_name}/curator_report.json`\n"
            + "\n"
        )
        self._write_markdown(self._day_page_path(run_index, day), text)

    def render_manager_pages(
        self,
        *,
        run_index: int,
        day: int,
        shift_policy: dict[str, Any],
        reviewer_report: dict[str, Any],
        curator_report: dict[str, Any],
    ) -> None:
        run = self.run_label(run_index)
        day_name = self.day_label(day)
        managers = {
            "Strategist": {
                "latest_role": "Builds day-start operating intent.",
                "notes": [
                    f"Operating focus: {_clean_text(shift_policy.get('operating_focus', ''), max_len=80) or 'not recorded'}.",
                    "Worker roles: " + ("; ".join(_compact_mapping(shift_policy.get("worker_roles", {}), limit=5)) or "not recorded") + ".",
                    "Prevention targets: " + (", ".join(_string_items(shift_policy.get("prevention_targets", []), limit=4)) or "not recorded") + ".",
                    "Daily targets: " + ("; ".join(_compact_mapping(shift_policy.get("daily_targets", {}), limit=5)) or "not recorded") + ".",
                ],
                "lessons": _string_items(_coerce_dict(curator_report.get("manager_lessons")).get("strategist", []), limit=5),
            },
            "Reviewer": {
                "latest_role": "Diagnoses completed-day execution and correction signals.",
                "notes": [
                    "Failure modes: " + (", ".join(_string_items(reviewer_report.get("top_failure_modes", []), limit=4)) or "not recorded") + ".",
                    "Recommended prevention targets: " + (", ".join(_string_items(reviewer_report.get("recommended_prevention_targets", []), limit=4)) or "not recorded") + ".",
                    "Support pair: " + (_clean_text(reviewer_report.get("recommended_support_pair", ""), max_len=80) or "not recorded") + ".",
                ],
                "lessons": _string_items(_coerce_dict(curator_report.get("manager_lessons")).get("reviewer", []), limit=5),
            },
            "Curator": {
                "latest_role": "Maintains the LLM wiki and knowledge graph source material.",
                "notes": [
                    _clean_text(curator_report.get("summary", ""), max_len=220) or "Curator update recorded.",
                    "Graph hints: " + (", ".join(_string_items(curator_report.get("graph_hints", []), limit=5)) or "not recorded") + ".",
                ],
                "lessons": _string_items(_coerce_dict(curator_report.get("manager_lessons")).get("curator", []), limit=5),
            },
        }
        for name, payload in managers.items():
            text = (
                _frontmatter({"kind": "manager", "manager": name, "updated_at": _utc_now_iso()})
                + f"\n\n# {name}\n\n"
                + f"{payload['latest_role']}\n\n"
                + f"## Latest Update\n\n- Source: [[Days/{run}/{day_name}|{run} {day_name}]]\n\n"
                + "## Current Operating Notes\n\n"
                + _bullet_list(list(payload.get("notes", [])))
                + "\n\n## Reusable Lessons\n\n"
                + _bullet_list(list(payload.get("lessons", [])), empty="No reusable lesson recorded yet.")
                + "\n"
            )
            self._write_markdown(self._manager_page_path(name), text)

    def render_managed_object_pages(
        self,
        *,
        run_index: int,
        day: int,
        day_summary: dict[str, Any],
        shift_policy: dict[str, Any],
        reviewer_report: dict[str, Any],
        curator_report: dict[str, Any],
    ) -> None:
        run = self.run_label(run_index)
        day_name = self.day_label(day)
        managed_updates = _coerce_dict(curator_report.get("managed_object_updates"))
        reviewer_modes = _string_items(reviewer_report.get("top_failure_modes", []), limit=4)
        object_payloads = {
            self.wiki_dir / "Managed" / "Queues" / "Inspection Output.md": {
                "title": "Inspection Output Queue",
                "kind": "queue",
                "observations": [
                    f"Accepted products: {_safe_int(day_summary.get('products'))}.",
                    f"Inspection passes: {_safe_int(day_summary.get('inspection_passes'))}.",
                    f"Closeout gap: {max(0, _safe_int(day_summary.get('inspection_passes')) - _safe_int(day_summary.get('products')))}.",
                    f"Station 2 output buffer end: {_safe_int(day_summary.get('station2_output_buffer_end'))}.",
                ],
                "lessons": _string_items(managed_updates.get("queues", []), limit=5),
            },
            self.wiki_dir / "Managed" / "Equipment" / "Inspection Workbench.md": {
                "title": "Inspection Workbench",
                "kind": "equipment",
                "observations": [
                    f"Inspection passes: {_safe_int(day_summary.get('inspection_passes'))}.",
                    f"Inspection backlog end: {_safe_int(day_summary.get('inspection_backlog_end'))}.",
                    f"Inspect-product tasks completed: {_safe_int(day_summary.get('inspect_product_task_count'))}.",
                ],
                "lessons": _string_items(managed_updates.get("equipment", []), limit=5),
            },
            self.wiki_dir / "Managed" / "Equipment" / "Machines.md": {
                "title": "Machines",
                "kind": "equipment_group",
                "observations": [
                    f"Machine breakdowns: {_safe_int(day_summary.get('machine_breakdowns'))}.",
                    f"Machine broken ratio: {_safe_float(day_summary.get('machine_broken_ratio')):.6f}.",
                    f"Machine PM ratio: {_safe_float(day_summary.get('machine_pm_ratio')):.6f}.",
                    "Reviewer failure modes: " + (", ".join(reviewer_modes) if reviewer_modes else "not recorded") + ".",
                ],
                "lessons": _string_items(managed_updates.get("equipment", []), limit=5),
            },
            self.wiki_dir / "Managed" / "Workers" / "Workers.md": {
                "title": "Workers",
                "kind": "worker_group",
                "observations": [
                    "Worker roles: " + ("; ".join(_compact_mapping(shift_policy.get("worker_roles", {}), limit=5)) or "not recorded") + ".",
                    f"Discharged worker count: {_safe_int(day_summary.get('agent_discharged_count'))}.",
                    "Prevention targets: " + (", ".join(_string_items(shift_policy.get("prevention_targets", []), limit=4)) or "not recorded") + ".",
                ],
                "lessons": _string_items(managed_updates.get("workers", []), limit=5),
            },
        }
        for path, payload in object_payloads.items():
            text = (
                _frontmatter({"kind": payload["kind"], "title": payload["title"], "updated_at": _utc_now_iso()})
                + f"\n\n# {payload['title']}\n\n"
                + f"## Latest Observation\n\n- Source: [[Days/{run}/{day_name}|{run} {day_name}]]\n\n"
                + _bullet_list(list(payload.get("observations", [])))
                + "\n\n## Reusable Operating Knowledge\n\n"
                + _bullet_list(list(payload.get("lessons", [])), empty="No reusable object lesson recorded yet.")
                + "\n"
            )
            self._write_markdown(path, text)

    def compact_prompt_digest(self, *, max_len: int = 3000) -> str:
        chunks: list[str] = []
        paths = [
            self.wiki_dir / "00_Index.md",
            self._manager_page_path("Strategist"),
            self._manager_page_path("Reviewer"),
            self._manager_page_path("Curator"),
            self.wiki_dir / "Managed" / "Queues" / "Inspection Output.md",
            self.wiki_dir / "Managed" / "Equipment" / "Inspection Workbench.md",
            self.wiki_dir / "Managed" / "Workers" / "Workers.md",
        ]
        runs_dir = self.wiki_dir / "Runs"
        if runs_dir.exists():
            paths.extend(sorted(runs_dir.glob("Run-*.md"))[-2:])
        days_dir = self.wiki_dir / "Days"
        if days_dir.exists():
            paths.extend(sorted(days_dir.glob("Run-*/Day-*.md"))[-3:])
        for path in paths:
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if text:
                chunks.append(f"## {path.relative_to(self.wiki_dir).as_posix()}\n{text[:900]}")
        graph_report = self.graph_dir / "GRAPH_REPORT.md"
        if graph_report.exists():
            try:
                chunks.append("## Graphify Report\n" + graph_report.read_text(encoding="utf-8", errors="replace")[:900])
            except OSError:
                pass
        digest = "\n\n".join(chunks).strip()
        if not digest:
            return "No LLM wiki knowledge has been accumulated yet."
        return digest[: max(100, int(max_len))]

    def graph_digest(self, *, max_len: int = 1500) -> str:
        graph_path = self.graph_dir / "graph.json"
        if not graph_path.exists():
            graph_path = self.graph_dir / "graphify-out" / "graph.json"
        if not graph_path.exists():
            return "No knowledge graph has been built yet."
        try:
            payload = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception:
            return "Knowledge graph exists but could not be parsed."
        raw_nodes = payload.get("nodes", {})
        edges = payload.get("edges", []) if isinstance(payload.get("edges", []), list) else []
        if isinstance(raw_nodes, dict):
            node_sample = list(raw_nodes.keys())[:12]
            node_count = len(raw_nodes)
        elif isinstance(raw_nodes, list):
            node_sample = [str(row.get("id", row.get("label", ""))) for row in raw_nodes if isinstance(row, dict)][:12]
            node_count = len(raw_nodes)
        else:
            node_sample = []
            node_count = 0
        text = _json_dump({"node_count": node_count, "edge_count": len(edges), "node_sample": node_sample})
        return text[: max(100, int(max_len))]

    def _graph_counts(self, graph_path: Path | str | None = None) -> dict[str, Any]:
        path = Path(graph_path) if graph_path is not None else self.graph_dir / "graph.json"
        if not path.exists():
            return {"exists": False, "valid": False, "node_count": 0, "edge_count": 0}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"exists": True, "valid": False, "node_count": 0, "edge_count": 0}
        raw_nodes = payload.get("nodes", {})
        raw_edges = payload.get("edges", [])
        node_count = len(raw_nodes) if isinstance(raw_nodes, (dict, list)) else 0
        edge_count = len(raw_edges) if isinstance(raw_edges, list) else 0
        return {"exists": True, "valid": True, "node_count": node_count, "edge_count": edge_count}

    @staticmethod
    def _meaningful_graph_counts(counts: dict[str, Any]) -> bool:
        return bool(counts.get("valid")) and int(counts.get("node_count", 0) or 0) > 0

    def run_graphify(self, *, run_index: int | None = None, day: int | None = None, reason: str = "run") -> dict[str, Any]:
        self.graph_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.graph_dir / "graphify.log"
        history_dir = self.graph_dir / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        previous_graph_path = self.graph_dir / "graph.json"
        previous_counts = self._graph_counts(previous_graph_path)
        previous_graph_bytes = previous_graph_path.read_bytes() if previous_graph_path.exists() else None
        cmd = [
            *self.graphify_command_parts,
            "extract",
            str(self.wiki_dir.resolve()),
            "--out",
            str(self.graph_dir.resolve()),
        ]
        if self.graphify_backend:
            cmd.extend(["--backend", self.graphify_backend])
        if self.graphify_model:
            cmd.extend(["--model", self.graphify_model])
        if self.graphify_no_cluster:
            cmd.append("--no-cluster")
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        if self.graphify_max_output_tokens > 0:
            env["GRAPHIFY_MAX_OUTPUT_TOKENS"] = str(self.graphify_max_output_tokens)
        backend = self.graphify_backend.lower()
        if backend == "ollama":
            if self.graphify_base_url:
                env["OLLAMA_BASE_URL"] = self.graphify_base_url
            if self.graphify_model:
                env["OLLAMA_MODEL"] = self.graphify_model
            env["OLLAMA_API_KEY"] = self.graphify_api_key or env.get("OLLAMA_API_KEY", "") or "ollama"
        elif backend == "openai":
            if self.graphify_model:
                env["GRAPHIFY_OPENAI_MODEL"] = self.graphify_model
            if self.graphify_api_key:
                env["OPENAI_API_KEY"] = self.graphify_api_key
        elif backend == "gemini":
            if self.graphify_model:
                env["GRAPHIFY_GEMINI_MODEL"] = self.graphify_model
            if self.graphify_api_key and not (env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")):
                env["GEMINI_API_KEY"] = self.graphify_api_key
        elif backend == "kimi" and self.graphify_api_key:
            env["MOONSHOT_API_KEY"] = self.graphify_api_key
        elif backend == "claude" and self.graphify_api_key:
            env["ANTHROPIC_API_KEY"] = self.graphify_api_key
        started = _utc_now_iso()
        status = "ok"
        error = ""
        stdout = ""
        stderr = ""
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.root_dir.resolve()),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.graphify_timeout_sec,
                env=env,
                check=False,
            )
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            if result.returncode != 0:
                status = "error"
                error = f"graphify exited with code {result.returncode}"
        except Exception as exc:
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
        self._promote_graphify_outputs()
        promoted_counts = self._graph_counts(self.graph_dir / "graph.json")
        empty_graph_protected = False
        if (
            previous_graph_bytes is not None
            and self._meaningful_graph_counts(previous_counts)
            and promoted_counts.get("valid")
            and int(promoted_counts.get("node_count", 0) or 0) == 0
        ):
            previous_graph_path.write_bytes(previous_graph_bytes)
            promoted_counts = self._graph_counts(previous_graph_path)
            empty_graph_protected = True
            if status == "ok":
                status = "warning"
            error = (error + "; " if error else "") + "graphify produced an empty graph; preserved previous non-empty graph"
        if not self._meaningful_graph_counts(promoted_counts):
            self._write_fallback_graph()
            promoted_counts = self._graph_counts(self.graph_dir / "graph.json")
        self._write_graph_json_html()
        self._write_graphify_tree_html()
        final_counts = self._graph_counts(self.graph_dir / "graph.json")
        payload = {
            "status": status,
            "run_index": run_index,
            "day": day,
            "reason": str(reason or "run"),
            "started_at": started,
            "finished_at": _utc_now_iso(),
            "command": cmd,
            "error": error,
            "previous_graph": previous_counts,
            "promoted_graph": promoted_counts,
            "final_graph": final_counts,
            "empty_graph_protected": empty_graph_protected,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }
        log_path.write_text(_json_dump(payload) + "\n", encoding="utf-8")
        with (self.graph_dir / "graphify_history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        label = self.run_label(run_index) if run_index is not None else "Run-unknown"
        if day is not None:
            label = f"{label}_{self.day_label(day)}"
        self._write_json(history_dir / f"{label}_{_slug(reason)}_graphify.json", payload)
        if (self.graph_dir / "graph.json").exists():
            shutil.copy2(self.graph_dir / "graph.json", history_dir / f"{label}_{_slug(reason)}_graph.json")
        return payload

    def _write_graphify_tree_html(self) -> None:
        graph_path = self.graph_dir / "graph.json"
        if not graph_path.exists():
            return
        output_path = self.graph_dir / "GRAPH_TREE.html"
        cmd = [
            sys.executable,
            "-m",
            "graphify",
            "tree",
            "--graph",
            str(graph_path.resolve()),
            "--output",
            str(output_path.resolve()),
            "--label",
            "ManSim LLM Wiki",
        ]
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            subprocess.run(
                cmd,
                cwd=str(self.root_dir.resolve()),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=120,
                env=env,
                check=False,
            )
        except Exception:
            return

    def _promote_graphify_outputs(self) -> None:
        out_dir = self.graph_dir / "graphify-out"
        if not out_dir.exists():
            return
        mapping = {
            out_dir / "graph.json": self.graph_dir / "graph.json",
            out_dir / "graph.html": self.graph_dir / "graph.html",
            out_dir / "GRAPH_REPORT.md": self.graph_dir / "GRAPH_REPORT.md",
        }
        for src, dst in mapping.items():
            if src.exists():
                shutil.copy2(src, dst)

    def _load_graph_json(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        graph_path = self.graph_dir / "graph.json"
        if not graph_path.exists():
            return ([], [])
        try:
            payload = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception:
            return ([], [])
        raw_nodes = payload.get("nodes", [])
        raw_edges = payload.get("edges", [])
        nodes: list[dict[str, Any]] = []
        if isinstance(raw_nodes, dict):
            for node_id, row in raw_nodes.items():
                data = dict(row) if isinstance(row, dict) else {}
                data.setdefault("id", str(node_id))
                data.setdefault("label", str(data.get("id", node_id)))
                nodes.append(data)
        elif isinstance(raw_nodes, list):
            for row in raw_nodes:
                if isinstance(row, dict):
                    data = dict(row)
                    data.setdefault("id", str(data.get("label", f"node_{len(nodes) + 1}")))
                    data.setdefault("label", str(data.get("id", "")))
                    nodes.append(data)
        edges = [dict(row) for row in raw_edges if isinstance(row, dict)] if isinstance(raw_edges, list) else []
        return (nodes, edges)

    def _write_graph_json_html(self) -> None:
        import html

        nodes, edges = self._load_graph_json()
        if not nodes:
            return
        node_ids = {str(node.get("id", "")) for node in nodes}
        clean_edges = [
            edge
            for edge in edges
            if str(edge.get("source", "")) in node_ids and str(edge.get("target", "")) in node_ids
        ]
        node_data = [
            {
                "id": str(node.get("id", "")),
                "label": str(node.get("label", node.get("id", ""))),
                "type": str(node.get("file_type", node.get("type", "concept"))),
                "source_file": str(node.get("source_file", "")),
            }
            for node in nodes
        ]
        edge_data = [
            {
                "source": str(edge.get("source", "")),
                "target": str(edge.get("target", "")),
                "relation": str(edge.get("relation", "related_to")),
                "confidence": str(edge.get("confidence", "")),
            }
            for edge in clean_edges
        ]
        graph_json = json.dumps({"nodes": node_data, "edges": edge_data}, ensure_ascii=False)
        report = (
            "# Graphify Knowledge Graph\n\n"
            + f"- Nodes: {len(node_data)}\n"
            + f"- Edges: {len(edge_data)}\n"
            + f"- Source: `{self.graph_dir / 'graph.json'}`\n"
        )
        self._write_markdown(self.graph_dir / "GRAPH_REPORT.md", report)
        html_text = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>ManSim Graphify Knowledge Graph</title>
  <style>
    body {{ font-family: Inter, Segoe UI, Arial, sans-serif; margin: 0; color: #172033; background: #f6f8fb; }}
    header {{ padding: 20px 24px; background: #fff; border-bottom: 1px solid #d8e1ef; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    .meta {{ color: #5b687a; font-size: 13px; }}
    #wrap {{ display: grid; grid-template-columns: minmax(0, 1fr) 320px; min-height: calc(100vh - 76px); }}
    svg {{ width: 100%; height: calc(100vh - 76px); background: #fbfdff; }}
    aside {{ border-left: 1px solid #d8e1ef; background: #fff; padding: 16px; overflow: auto; }}
    .node {{ cursor: pointer; }}
    .node circle {{ fill: #2563eb; stroke: #0f2f68; stroke-width: 1.5; }}
    .node text {{ font-size: 11px; fill: #172033; paint-order: stroke; stroke: #fff; stroke-width: 3px; }}
    .edge {{ stroke: #9aa9bd; stroke-width: 1.2; opacity: .75; }}
    .edge-label {{ font-size: 9px; fill: #6b7788; }}
    .item {{ border-bottom: 1px solid #edf2f7; padding: 8px 0; }}
    .item strong {{ display: block; font-size: 13px; }}
    .item span {{ display: block; color: #677386; font-size: 12px; margin-top: 2px; }}
  </style>
</head>
<body>
  <header>
    <h1>ManSim Graphify Knowledge Graph</h1>
    <div class="meta">{len(node_data)} nodes / {len(edge_data)} edges from semantic Graphify extraction</div>
  </header>
  <div id="wrap">
    <svg id="graph" viewBox="0 0 1100 720" role="img" aria-label="Graphify knowledge graph"></svg>
    <aside>
      <h2>Nodes</h2>
      <div id="details"></div>
    </aside>
  </div>
  <script>
    const graph = {graph_json};
    const svg = document.getElementById('graph');
    const details = document.getElementById('details');
    const width = 1100, height = 720, cx = width / 2, cy = height / 2;
    const radius = Math.min(width, height) * 0.36;
    const byId = new Map(graph.nodes.map((node, i) => {{
      const angle = (Math.PI * 2 * i) / Math.max(1, graph.nodes.length);
      node.x = cx + Math.cos(angle) * radius;
      node.y = cy + Math.sin(angle) * radius;
      return [node.id, node];
    }}));
    function add(tag, attrs, parent = svg) {{
      const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
      for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
      parent.appendChild(el);
      return el;
    }}
    graph.edges.forEach(edge => {{
      const source = byId.get(edge.source), target = byId.get(edge.target);
      if (!source || !target) return;
      add('line', {{ class: 'edge', x1: source.x, y1: source.y, x2: target.x, y2: target.y }});
      add('text', {{
        class: 'edge-label',
        x: (source.x + target.x) / 2,
        y: (source.y + target.y) / 2,
        'text-anchor': 'middle'
      }}).textContent = edge.relation;
    }});
    graph.nodes.forEach(node => {{
      const g = add('g', {{ class: 'node', transform: `translate(${{node.x}},${{node.y}})` }});
      add('circle', {{ r: 11 }}, g);
      const label = add('text', {{ x: 15, y: 4 }}, g);
      label.textContent = node.label.length > 34 ? node.label.slice(0, 31) + '...' : node.label;
      g.addEventListener('click', () => renderDetails(node.id));
    }});
    function renderDetails(selectedId = '') {{
      const rows = graph.nodes.map(node => {{
        const connected = graph.edges.filter(edge => edge.source === node.id || edge.target === node.id);
        const selected = node.id === selectedId ? 'background:#eef4ff;padding:8px;border-radius:8px;' : '';
        return `<div class="item" style="${{selected}}"><strong>${{escapeHtml(node.label)}}</strong><span>${{escapeHtml(node.type)}} | ${{escapeHtml(node.source_file)}}</span><span>${{connected.length}} links</span></div>`;
      }});
      details.innerHTML = rows.join('');
    }}
    function escapeHtml(text) {{
      return String(text).replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
    }}
    renderDetails();
  </script>
</body>
</html>"""
        (self.graph_dir / "graph.html").write_text(html_text, encoding="utf-8")

    def _write_fallback_graph(self) -> None:
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        title_to_id: dict[str, str] = {}
        for path in sorted(self.wiki_dir.rglob("*.md")):
            rel = path.relative_to(self.wiki_dir).as_posix()
            node_id = f"page:{_slug(rel)}"
            label = path.stem
            title_to_id[path.stem.casefold()] = node_id
            title_to_id[rel.removesuffix(".md").casefold()] = node_id
            nodes[node_id] = {
                "id": node_id,
                "type": "WikiPage",
                "label": label,
                "properties": {"path": rel, "generator": "wikilink_fallback"},
            }
        for path in sorted(self.wiki_dir.rglob("*.md")):
            rel = path.relative_to(self.wiki_dir).as_posix()
            source = f"page:{_slug(rel)}"
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in re.findall(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", text):
                key = str(match).strip().removesuffix(".md").casefold()
                target = title_to_id.get(key)
                if not target:
                    target = title_to_id.get(key.replace("\\", "/"))
                if not target or target == source:
                    continue
                edge = {"source": source, "target": target, "relation": "wikilink", "properties": {"generator": "wikilink_fallback"}}
                if edge not in edges:
                    edges.append(edge)
        payload = {
            "meta": {
                "version": "llm_wiki_fallback_v1",
                "source": "Obsidian wikilinks",
                "created_at": _utc_now_iso(),
            },
            "nodes": nodes,
            "edges": edges,
        }
        self._write_json(self.graph_dir / "graph.json", payload)
        self._write_markdown(
            self.graph_dir / "GRAPH_REPORT.md",
            "# Fallback Knowledge Graph\n\n"
            + f"- Nodes: {len(nodes)}\n"
            + f"- Edges: {len(edges)}\n"
            + "- Source: Obsidian wikilinks generated from the ManSim LLM wiki.\n",
        )
        self._write_fallback_graph_html(nodes=nodes, edges=edges)

    def _write_fallback_graph_html(self, *, nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> None:
        import html

        rows = [
            "<!doctype html><html><head><meta charset='utf-8'><title>ManSim Knowledge Graph</title>",
            "<style>body{font-family:Inter,Segoe UI,Arial,sans-serif;margin:32px;color:#172033}"
            ".node{padding:8px 10px;margin:6px;border:1px solid #d8e1ef;border-radius:8px;display:inline-block;background:#fbfdff}"
            ".edge{margin:4px 0;color:#475569}</style></head><body>",
            "<h1>ManSim Knowledge Graph</h1>",
            "<p>Fallback graph generated from Obsidian wikilinks because Graphify did not produce graph.json.</p>",
            "<h2>Nodes</h2>",
        ]
        for node in nodes.values():
            rows.append(f"<span class='node'>{html.escape(str(node.get('label', '')))}</span>")
        rows.append("<h2>Edges</h2>")
        label_by_id = {node_id: str(node.get("label", node_id)) for node_id, node in nodes.items()}
        for edge in edges:
            rows.append(
                "<div class='edge'>"
                + html.escape(label_by_id.get(str(edge.get("source", "")), str(edge.get("source", ""))))
                + " -> "
                + html.escape(label_by_id.get(str(edge.get("target", "")), str(edge.get("target", ""))))
                + "</div>"
            )
        rows.append("</body></html>")
        (self.graph_dir / "graph.html").write_text("\n".join(rows), encoding="utf-8")

    def export_wiki_dashboard(self, output_dir: Path | str) -> Path:
        output_path = Path(output_dir) / "llm_wiki_dashboard.html"
        index = self.wiki_dir / "00_Index.md"
        try:
            index_text = index.read_text(encoding="utf-8", errors="replace")
        except OSError:
            index_text = "# ManSim LLM Wiki\n\nNo wiki has been generated yet."
        html = self._simple_markdown_html(index_text, self.wiki_dir)
        index_uri = self._obsidian_uri(index if index.exists() else self.wiki_dir)
        vault_uri = self._obsidian_uri(self.wiki_dir)
        graph_html = self.graph_dir / "GRAPH_TREE.html"
        if not graph_html.exists():
            graph_html = self.graph_dir / "graph.html"
        graph_link = graph_html.resolve().as_uri() if graph_html.exists() else ""
        page_links = self._wiki_page_links_html()
        graph_anchor = f"<a class='button secondary' href='{graph_link}'>Open Graph</a>" if graph_link else ""
        output_path.write_text(
            "<!doctype html><html><head><meta charset='utf-8'><title>ManSim LLM Wiki</title>"
            "<style>body{font-family:Inter,Segoe UI,Arial,sans-serif;margin:32px;line-height:1.5;color:#172033}"
            "a{color:#0f5cc0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}"
            ".panel{border:1px solid #d8e1ef;border-radius:8px;padding:16px;background:#fbfdff}"
            ".actions{display:flex;gap:10px;flex-wrap:wrap;margin:16px 0 22px}"
            ".button{display:inline-block;padding:9px 12px;border-radius:8px;background:#123c69;color:white;text-decoration:none;font-weight:600}"
            ".button.secondary{background:#eef4fb;color:#123c69;border:1px solid #c8d7ea}"
            ".page-list{columns:2;column-gap:28px}.page-list a{display:block;margin:4px 0}"
            "pre{background:#0f172a;color:#e2e8f0;padding:12px;border-radius:8px;overflow:auto}</style></head><body>"
            "<h1>ManSim LLM Wiki</h1>"
            f"<div class='actions'><a class='button' href='{index_uri}'>Open Wiki in Obsidian</a>"
            f"<a class='button secondary' href='{vault_uri}'>Open Vault</a>{graph_anchor}</div>"
            f"<p>Vault: <code>{self.wiki_dir.resolve()}</code></p>"
            f"<p>Graph: <code>{self.graph_dir.resolve()}</code></p>"
            f"<div class='panel'><h2>Pages</h2><div class='page-list'>{page_links}</div></div>"
            "<h2>Index Preview</h2>"
            f"<div class='panel'>{html}</div>"
            "</body></html>",
            encoding="utf-8",
        )
        return output_path

    @staticmethod
    def _obsidian_uri(path: Path) -> str:
        return "obsidian://open?path=" + quote(path.resolve().as_posix(), safe="")

    def _wiki_page_links_html(self) -> str:
        import html

        rows: list[str] = []
        for path in sorted(self.wiki_dir.rglob("*.md")):
            rel = path.relative_to(self.wiki_dir).as_posix()
            rows.append(f"<a href='{self._obsidian_uri(path)}'>{html.escape(rel)}</a>")
        return "\n".join(rows) if rows else "<span>No wiki pages yet.</span>"

    @classmethod
    def _simple_markdown_html(cls, markdown: str, wiki_dir: Path) -> str:
        import html
        import re

        def render_inline(text: str) -> str:
            escaped = html.escape(text)

            def repl(match: re.Match[str]) -> str:
                target = str(match.group(1) or "").strip()
                label = str(match.group(2) or target).strip()
                file_target = target.split("#", 1)[0].removesuffix(".md")
                path = wiki_dir / f"{file_target}.md"
                return f"<a href='{cls._obsidian_uri(path)}'>{html.escape(label)}</a>"

            return re.sub(r"\[\[([^|\]#]+)(?:#[^|\]]+)?(?:\|([^\]]+))?\]\]", repl, escaped)

        rows: list[str] = []
        in_code = False
        in_frontmatter = False
        seen_content = False
        for raw in markdown.splitlines():
            line = raw.rstrip()
            if not seen_content and line.strip() == "---":
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter:
                continue
            if line.strip():
                seen_content = True
            if line.startswith("```"):
                rows.append("<pre>" if not in_code else "</pre>")
                in_code = not in_code
                continue
            if in_code:
                rows.append(html.escape(line))
                continue
            if line.startswith("# "):
                rows.append(f"<h2>{html.escape(line[2:].strip())}</h2>")
            elif line.startswith("## "):
                rows.append(f"<h3>{html.escape(line[3:].strip())}</h3>")
            elif line.startswith("- "):
                text = render_inline(line[2:].strip())
                rows.append(f"<p>- {text}</p>")
            elif line.strip():
                rows.append(f"<p>{render_inline(line)}</p>")
        return "\n".join(rows)
