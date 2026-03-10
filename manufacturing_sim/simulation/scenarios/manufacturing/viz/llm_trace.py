from __future__ import annotations

import json
from html import escape
from pathlib import Path
from typing import Any

from manufacturing_sim.simulation.scenarios.manufacturing.viz.artifact_meta import (
    format_run_mode_line,
    load_artifact_meta,
)


def export_llm_trace_dashboard(*, records: list[dict[str, Any]], output_dir: Path) -> Path | None:
    if not records:
        return None

    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "llm_trace.html"

    try:
        import plotly.graph_objects as go
    except Exception:
        meta = load_artifact_meta(output_dir)
        rows = [
            "<html><head><meta charset='utf-8'><title>LLM Exchange Trace</title></head><body>",
            "<h2>LLM Exchange Trace</h2>",
            f"<p><b>events.jsonl</b>: {escape(str(meta.get('events_path','-')))}</p>",
            f"<p><b>{escape(format_run_mode_line(meta))}</b></p>",
            "<p>Plotly is unavailable, showing raw JSON only.</p>",
            f"<pre>{escape(json.dumps(records, ensure_ascii=False, indent=2))}</pre>",
            "</body></html>",
        ]
        html_path.write_text("\n".join(rows), encoding="utf-8")
        return html_path

    def _safe_int(val: Any, default: int = 0) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _safe_float(val: Any, default: float = 0.0) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    summary_rows: list[dict[str, Any]] = []
    for rec in records:
        ctx = rec.get("context", {}) if isinstance(rec.get("context", {}), dict) else {}
        req = rec.get("request", {}) if isinstance(rec.get("request", {}), dict) else {}
        req_payload = req.get("payload", {}) if isinstance(req.get("payload", {}), dict) else {}
        messages = req_payload.get("messages", []) if isinstance(req_payload.get("messages", []), list) else []

        prompt_chars = 0
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            prompt_chars += len(str(msg.get("content", "")))

        response_text = str(rec.get("response_text", ""))
        phase = str(ctx.get("phase", rec.get("call_name", "llm_call")))
        status = str(rec.get("status", ""))
        summary_rows.append(
            {
                "call_id": _safe_int(rec.get("call_id")),
                "phase": phase,
                "day": _safe_int(ctx.get("day"), 0),
                "round": _safe_int(ctx.get("round"), 0),
                "agent_id": str(ctx.get("agent_id", "")),
                "status": status,
                "latency_sec": _safe_float(rec.get("latency_sec"), 0.0),
                "prompt_chars": prompt_chars,
                "response_chars": len(response_text),
                "started_at_utc": str(rec.get("started_at_utc", "")),
                "call_name": str(rec.get("call_name", "")),
            }
        )

    summary_rows = sorted(summary_rows, key=lambda x: x["call_id"])

    y_labels = [f"#{r['call_id']} {r['phase']}" for r in summary_rows]
    x_latency = [float(r["latency_sec"]) for r in summary_rows]
    colors = ["#2a9d8f" if str(r["status"]).lower() == "ok" else "#e76f51" for r in summary_rows]
    hover = [
        "<br>".join(
            [
                f"call_id={r['call_id']}",
                f"phase={r['phase']}",
                f"day={r['day']}",
                f"round={r['round']}",
                f"agent={r['agent_id'] or '-'}",
                f"status={r['status']}",
                f"latency={r['latency_sec']}s",
                f"prompt_chars={r['prompt_chars']}",
                f"response_chars={r['response_chars']}",
                f"started_at_utc={r['started_at_utc']}",
            ]
        )
        for r in summary_rows
    ]

    latency_fig = go.Figure(
        data=[
            go.Bar(
                x=x_latency,
                y=y_labels,
                orientation="h",
                marker=dict(color=colors),
                hovertext=hover,
                hoverinfo="text",
                name="latency_sec",
            )
        ]
    )
    latency_fig.update_layout(
        title="LLM Call Latency by Step",
        xaxis_title="Latency (sec)",
        yaxis_title="Call",
        height=max(420, 34 * max(1, len(summary_rows)) + 140),
        margin=dict(l=120, r=40, t=60, b=40),
    )

    table_fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=[
                        "call_id",
                        "phase",
                        "day",
                        "round",
                        "agent_id",
                        "status",
                        "latency_sec",
                        "prompt_chars",
                        "response_chars",
                    ],
                    fill_color="#264653",
                    font=dict(color="white", size=12),
                    align="left",
                ),
                cells=dict(
                    values=[
                        [r["call_id"] for r in summary_rows],
                        [r["phase"] for r in summary_rows],
                        [r["day"] for r in summary_rows],
                        [r["round"] for r in summary_rows],
                        [r["agent_id"] or "-" for r in summary_rows],
                        [r["status"] for r in summary_rows],
                        [r["latency_sec"] for r in summary_rows],
                        [r["prompt_chars"] for r in summary_rows],
                        [r["response_chars"] for r in summary_rows],
                    ],
                    align="left",
                    fill_color="#f8f9fa",
                    font=dict(color="#1f2937", size=11),
                ),
            )
        ]
    )
    table_fig.update_layout(title="LLM Exchange Summary", height=max(380, 28 * max(1, len(summary_rows)) + 150))

    detail_blocks: list[str] = []
    for rec in records:
        call_id = _safe_int(rec.get("call_id"))
        call_name = str(rec.get("call_name", ""))
        status = str(rec.get("status", ""))
        latency = _safe_float(rec.get("latency_sec"), 0.0)
        ctx = rec.get("context", {}) if isinstance(rec.get("context", {}), dict) else {}
        req = rec.get("request", {}) if isinstance(rec.get("request", {}), dict) else {}
        resp = rec.get("response", {}) if isinstance(rec.get("response", {}), dict) else {}
        parsed = rec.get("parsed", {}) if isinstance(rec.get("parsed", {}), dict) else {}

        detail_blocks.append(
            "\n".join(
                [
                    "<details class='call'>",
                    f"<summary>#{call_id} | {escape(call_name)} | status={escape(status)} | latency={latency:.3f}s</summary>",
                    "<div class='meta'>",
                    f"<div><strong>started_at_utc</strong>: {escape(str(rec.get('started_at_utc', '')))}</div>",
                    f"<div><strong>context</strong><pre>{escape(json.dumps(ctx, ensure_ascii=False, indent=2))}</pre></div>",
                    "</div>",
                    "<div class='grid'>",
                    f"<div><h4>Request</h4><pre>{escape(json.dumps(req, ensure_ascii=False, indent=2))}</pre></div>",
                    f"<div><h4>Response</h4><pre>{escape(json.dumps(resp, ensure_ascii=False, indent=2))}</pre></div>",
                    "</div>",
                    f"<div><h4>Parsed JSON</h4><pre>{escape(json.dumps(parsed, ensure_ascii=False, indent=2))}</pre></div>",
                    f"<div><h4>Response Content</h4><pre>{escape(str(rec.get('response_text', '')))}</pre></div>",
                    f"<div><h4>Error</h4><pre>{escape(str(rec.get('error', '')))}</pre></div>",
                    "</details>",
                ]
            )
        )

    total_calls = len(summary_rows)
    error_calls = sum(1 for r in summary_rows if str(r["status"]).lower() != "ok")
    meta = load_artifact_meta(output_dir)
    html = "\n".join(
        [
            "<!doctype html>",
            "<html lang='en'>",
            "<head>",
            "<meta charset='utf-8' />",
            "<meta name='viewport' content='width=device-width, initial-scale=1' />",
            "<title>LLM Exchange Trace</title>",
            "<style>",
            "body { font-family: Segoe UI, Arial, sans-serif; margin: 20px; color: #1f2937; }",
            ".head { display:flex; gap:16px; flex-wrap:wrap; margin-bottom:14px; }",
            ".badge { background:#eef2ff; border:1px solid #c7d2fe; padding:8px 12px; border-radius:8px; font-size:13px; }",
            ".call { border:1px solid #d1d5db; border-radius:8px; padding:8px 10px; margin:10px 0; background:#fff; }",
            ".call summary { cursor:pointer; font-weight:600; }",
            ".meta { margin-top:10px; font-size:13px; }",
            ".grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:10px; }",
            "pre { background:#f8fafc; border:1px solid #e5e7eb; border-radius:6px; padding:10px; overflow:auto; white-space:pre-wrap; word-break:break-word; }",
            "h1, h2, h3, h4 { margin: 8px 0; }",
            "</style>",
            "</head>",
            "<body>",
            "<h1>LLM Exchange Trace</h1>",
            "<div class='head'>",
            f"<div class='badge'><strong>Total Calls</strong>: {total_calls}</div>",
            f"<div class='badge'><strong>Error Calls</strong>: {error_calls}</div>",
            f"<div class='badge'><strong>events.jsonl</strong>: {escape(str(meta.get('events_path', '-')))}</div>",
            f"<div class='badge'><strong>{escape(format_run_mode_line(meta))}</strong></div>",
            "<div class='badge'><strong>Source</strong>: simulation decision.llm calls</div>",
            "</div>",
            latency_fig.to_html(full_html=False, include_plotlyjs=True),
            table_fig.to_html(full_html=False, include_plotlyjs=False),
            "<h2>Request / Response Detail</h2>",
            "<p>Expand each row to inspect full request payload, raw server response, parsed JSON, and errors.</p>",
            *detail_blocks,
            "</body>",
            "</html>",
        ]
    )

    html_path.write_text(html, encoding="utf-8")
    return html_path

