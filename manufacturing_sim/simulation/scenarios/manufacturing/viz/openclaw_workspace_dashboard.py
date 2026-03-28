from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from .artifact_meta import format_run_mode_line, load_artifact_meta


WORKSPACE_FILES = [
    'AGENTS.md',
    'USER.md',
    'MEMORY.md',
    'SOUL.md',
    'TOOLS.md',
    'HEARTBEAT.md',
    'BOOTSTRAP.md',
    'facts/current_request.json',
    'facts/current_response_template.json',
    'facts/current_native_turn.json',
]

FILE_DESCRIPTIONS = {
    'AGENTS.md': 'OpenClaw 에이전트 규칙 파일입니다. 이 워크스페이스에서 어떤 원칙으로 응답해야 하는지 기본 규범이 들어갑니다.',
    'USER.md': '이번 턴의 활성 프롬프트입니다. 호출 직전에 system/user 지시와 Request JSON, Response Template JSON으로 다시 써집니다.',
    'MEMORY.md': '현재 run 동안만 유지되는 요약 메모리입니다. 장기 규칙보다 실행 중 축적된 요약을 담습니다.',
    'SOUL.md': '정체성/성향 슬롯입니다. 현재 ManSim에서는 대부분 비워 두고 필요할 때만 씁니다.',
    'TOOLS.md': '도구 및 행동 규칙 슬롯입니다. 현재 native-local 경로에서는 대부분 비어 있거나 고정 기본값 상태입니다.',
    'HEARTBEAT.md': '헬스 체크/keepalive 성격의 슬롯입니다. 런타임 초기화 때 비워집니다.',
    'BOOTSTRAP.md': '워크스페이스 초기화 슬롯입니다. 런타임 복사본 생성 시 비워집니다.',
    'facts/current_request.json': '이번 호출에 실제로 전달된 구조화 입력 원본입니다.',
    'facts/current_response_template.json': '이번 호출에서 모델이 따라야 하는 JSON 출력 계약입니다.',
    'facts/current_native_turn.json': 'native turn 실행 메타데이터 스냅샷입니다.',
}

INIT_WRITER = 'OpenClawClient.prepare_run_runtime / _ensure_workspace_minimum'
REQUEST_WRITER = 'OpenClawOrchestratedDecisionModule._native_turn_prompts'
TURN_WRITER = 'OpenClawClient.native_agent_turn'


def _safe_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def _runtime_info(run_meta: dict[str, Any]) -> dict[str, Any]:
    llm = run_meta.get('llm', {}) if isinstance(run_meta, dict) else {}
    openclaw = llm.get('openclaw', {}) if isinstance(llm.get('openclaw', {}), dict) else {}
    runtime = openclaw.get('runtime', {}) if isinstance(openclaw.get('runtime', {}), dict) else {}
    return runtime if isinstance(runtime, dict) else {}


def _fmt_ts(value: Any) -> str:
    if value is None:
        return '-'
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).strftime('%Y-%m-%d %H:%M:%S')
        except (OverflowError, OSError, ValueError):
            return str(value)
    text = str(value).strip()
    return text or '-'


def _phase_reason(phase: str) -> str:
    normalized = str(phase or '').strip()
    if normalized == 'manager_bottleneck_detector':
        return '현재 공장 상태와 병목을 요약·진단하기 위해 갱신되었습니다.'
    if normalized == 'manager_daily_planner':
        return '다음 실행 계획(diff)을 만들기 위해 갱신되었습니다.'
    return '현재 native 호출을 준비하거나 기록하기 위해 갱신되었습니다.'


def _initial_reason(file_name: str) -> str:
    if file_name in {'SOUL.md', 'TOOLS.md', 'HEARTBEAT.md', 'BOOTSTRAP.md', 'USER.md'}:
        return '런타임 워크스페이스를 초기화하면서 빈 파일로 리셋되었습니다.'
    if file_name == 'AGENTS.md':
        return '에이전트 기본 규칙을 제공하기 위해 런타임 초기화 시 준비되었습니다.'
    if file_name == 'MEMORY.md':
        return 'run-local 메모리 슬롯을 준비하기 위해 런타임 초기화 시 준비되었습니다.'
    if file_name in {'facts/current_request.json', 'facts/current_response_template.json'}:
        return '호출 전 구조화 입력/출력 계약 슬롯을 준비하기 위해 초기화되었습니다.'
    if file_name == 'facts/current_native_turn.json':
        return 'native turn 메타데이터를 기록하기 위한 슬롯입니다. 실제 호출 시점에 써집니다.'
    return '런타임 초기화 과정에서 준비되었습니다.'


def _timeline_events_for_workspace(alias: str, workspace: Path | None, records: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    events: dict[str, list[dict[str, str]]] = {name: [] for name in WORKSPACE_FILES}

    if workspace is not None:
        for rel in WORKSPACE_FILES:
            path = workspace / rel
            if path.exists():
                events[rel].append(
                    {
                        'who': INIT_WRITER,
                        'when': 'run bootstrap',
                        'why': _initial_reason(rel),
                    }
                )

    related = []
    for rec in records:
        request = rec.get('request', {}) if isinstance(rec.get('request', {}), dict) else {}
        headers = request.get('headers', {}) if isinstance(request.get('headers', {}), dict) else {}
        if str(headers.get('workspace_alias', '')).strip() != alias:
            continue
        related.append(rec)

    for index, rec in enumerate(related, start=1):
        request = rec.get('request', {}) if isinstance(rec.get('request', {}), dict) else {}
        headers = request.get('headers', {}) if isinstance(request.get('headers', {}), dict) else {}
        context = rec.get('context', {}) if isinstance(rec.get('context', {}), dict) else {}
        phase = str(context.get('phase', rec.get('call_name', ''))).strip()
        day = context.get('day', '')
        who = str(headers.get('agent_id', '')).strip() or '-'
        when_text = f"day {day} / call {index}"
        why = _phase_reason(phase)

        for rel in ('facts/current_request.json', 'facts/current_response_template.json'):
            events[rel].append(
                {
                    'who': REQUEST_WRITER,
                    'when': when_text,
                    'why': why,
                }
            )
        for rel in ('facts/current_native_turn.json', 'USER.md'):
            events[rel].append(
                {
                    'who': f'{TURN_WRITER} -> {who}',
                    'when': when_text,
                    'why': why,
                }
            )

    return events


def _collect_workspace_panels(runtime: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    workspace_root_raw = str(runtime.get('workspace_root', '')).strip()
    workspace_root = Path(workspace_root_raw) if workspace_root_raw else None
    alias_map = runtime.get('workspace_aliases', {}) if isinstance(runtime.get('workspace_aliases', {}), dict) else {}
    aliases: list[str] = []
    seen: set[str] = set()
    for value in alias_map.values():
        alias = str(value).strip()
        if alias and alias not in seen:
            seen.add(alias)
            aliases.append(alias)

    panels: list[dict[str, Any]] = []
    for alias in aliases:
        workspace = workspace_root / alias if workspace_root is not None else None
        timeline_map = _timeline_events_for_workspace(alias, workspace, records)
        file_cards: list[dict[str, Any]] = []
        if workspace is not None:
            for rel in WORKSPACE_FILES:
                path = workspace / rel
                text = _safe_text(path)
                file_cards.append(
                    {
                        'name': rel,
                        'exists': path.exists(),
                        'size': path.stat().st_size if path.exists() else 0,
                        'last_modified': _fmt_ts(path.stat().st_mtime) if path.exists() else '-',
                        'content': text,
                        'timeline': timeline_map.get(rel, []),
                    }
                )

        related_calls: list[dict[str, Any]] = []
        for rec in records:
            req = rec.get('request', {}) if isinstance(rec.get('request', {}), dict) else {}
            headers = req.get('headers', {}) if isinstance(req.get('headers', {}), dict) else {}
            payload = req.get('payload', {}) if isinstance(req.get('payload', {}), dict) else {}
            if str(headers.get('workspace_alias', '')).strip() != alias:
                continue
            context = rec.get('context', {}) if isinstance(rec.get('context', {}), dict) else {}
            related_calls.append(
                {
                    'call_name': rec.get('call_name', ''),
                    'day': context.get('day', ''),
                    'agent_id': headers.get('agent_id', ''),
                    'session_id': payload.get('session_id', ''),
                    'message': payload.get('message', ''),
                    'response_text': rec.get('response_text', ''),
                }
            )
        panels.append({'alias': alias, 'files': file_cards, 'calls': related_calls})
    return panels


def export_openclaw_workspace_dashboard(*, output_dir: Path, run_meta: dict[str, Any], records: list[dict[str, Any]]) -> Path | None:
    runtime = _runtime_info(run_meta)
    if not runtime:
        return None
    panels = _collect_workspace_panels(runtime, records)
    if not panels:
        return None

    meta = load_artifact_meta(output_dir)
    run_mode_line = escape(format_run_mode_line(meta))
    events_path = escape(str(meta.get('events_path', '-') or '-'))
    runtime_root = escape(str(runtime.get('runtime_root', '-') or '-'))
    workspace_root = escape(str(runtime.get('workspace_root', '-') or '-'))
    alias_map = runtime.get('workspace_aliases', {}) if isinstance(runtime.get('workspace_aliases', {}), dict) else {}
    reflect_agent = escape(', '.join(sorted(str(k) for k, v in alias_map.items() if str(v).strip() == 'MANAGER_BOTTLENECK_DETECTOR')) or '-')
    plan_agent = escape(', '.join(sorted(str(k) for k, v in alias_map.items() if str(v).strip() == 'MANAGER_DAILY_PLANNER')) or '-')

    overview_rows: list[str] = []
    for rel in WORKSPACE_FILES:
        overview_rows.append(
            '<tr>'
            f'<td style="padding:8px; border-bottom:1px solid #e2e8f0;"><code>{escape(rel)}</code></td>'
            f'<td style="padding:8px; border-bottom:1px solid #e2e8f0;">{escape(FILE_DESCRIPTIONS.get(rel, ""))}</td>'
            '</tr>'
        )

    section_html: list[str] = []
    for panel in panels:
        alias = escape(panel['alias'])
        call_rows: list[str] = []
        for call in panel['calls']:
            call_rows.append(
                '<div class="call-card">'
                f"<div><strong>{escape(str(call['call_name']))}</strong> | day {escape(str(call['day']))} | {escape(str(call['agent_id']))}</div>"
                f"<div class='muted'>session: {escape(str(call['session_id']))}</div>"
                f"<details><summary>CLI message</summary><pre>{escape(str(call['message']))}</pre></details>"
                f"<details><summary>response_text</summary><pre>{escape(str(call['response_text']))}</pre></details>"
                '</div>'
            )
        if not call_rows:
            call_rows.append('<div class="muted">이 workspace alias에 매칭된 호출이 없습니다.</div>')

        file_rows: list[str] = []
        for file_card in panel['files']:
            badge = 'present' if file_card['exists'] else 'missing'
            summary = f"{file_card['name']} | {badge} | {file_card['size']} bytes | last_modified={file_card['last_modified']}"
            content = file_card['content'] if file_card['content'] else '(empty)'
            description = FILE_DESCRIPTIONS.get(file_card['name'], '')
            timeline_rows = []
            for item in file_card['timeline']:
                timeline_rows.append(
                    '<div class="timeline-item">'
                    f"<div><strong>누가</strong>: {escape(item['who'])}</div>"
                    f"<div><strong>언제</strong>: {escape(item['when'])}</div>"
                    f"<div><strong>왜</strong>: {escape(item['why'])}</div>"
                    '</div>'
                )
            if not timeline_rows:
                timeline_rows.append('<div class="muted">이 파일에 대해 추정 가능한 갱신 이력이 없습니다.</div>')
            file_rows.append(
                f"<details class='file-card'><summary>{escape(summary)}</summary>"
                f"<div class='muted'>{escape(description)}</div>"
                "<details><summary>갱신 타임라인</summary>" + ''.join(timeline_rows) + "</details>"
                f"<pre>{escape(content)}</pre></details>"
            )

        section_html.append(
            '<section class="panel">'
            f'<h2>{alias}</h2>'
            '<div class="grid two">'
            '<div class="card"><h3>Workspace Files</h3>' + ''.join(file_rows) + '</div>'
            '<div class="card"><h3>Calls Using This Workspace</h3>' + ''.join(call_rows) + '</div>'
            '</div>'
            '</section>'
        )

    html = f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <title>OpenClaw Workspace Dashboard</title>
  <style>
    body {{ font-family: Segoe UI, Arial, sans-serif; margin: 0; background: #f4f7fb; color: #0f172a; }}
    .wrap {{ max-width: 1800px; margin: 0 auto; padding: 20px; }}
    .meta {{ color: #475569; margin-bottom: 18px; }}
    .grid.two {{ display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 16px; }}
    .panel {{ margin-top: 20px; }}
    .card {{ background: #fff; border: 1px solid #dbe4f0; border-radius: 18px; padding: 18px; box-shadow: 0 2px 12px rgba(15,23,42,0.04); }}
    .file-card, .call-card, .timeline-item {{ margin-bottom: 12px; border: 1px solid #e2e8f0; border-radius: 12px; padding: 10px 12px; background: #f8fafc; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 12px; overflow-x: auto; }}
    summary {{ cursor: pointer; font-weight: 600; }}
    .muted {{ color: #64748b; font-size: 13px; margin-top: 4px; }}
    h1, h2, h3 {{ margin: 0 0 12px 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>OpenClaw Workspace Dashboard</h1>
    <div class="meta">{run_mode_line}<br/>events.jsonl: {events_path}</div>
    <div class="meta">runtime_root: {runtime_root}<br/>workspace_root: {workspace_root}<br/>reflect runtime_agent_id: {reflect_agent}<br/>plan runtime_agent_id: {plan_agent}</div>
    <section class="panel">
      <div class="card">
        <h3>파일 역할</h3>
        <div class="muted">아래 파일들은 repo 템플릿에서 runtime workspace로 복사된 뒤, 일부는 호출 직전에 실제 내용으로 다시 써집니다.</div>
        <table style="width:100%; border-collapse:collapse; margin-top:12px;">
          <thead>
            <tr>
              <th style="text-align:left; border-bottom:1px solid #cbd5e1; padding:8px;">파일</th>
              <th style="text-align:left; border-bottom:1px solid #cbd5e1; padding:8px;">역할</th>
            </tr>
          </thead>
          <tbody>
            {''.join(overview_rows)}
          </tbody>
        </table>
      </div>
    </section>
    {''.join(section_html)}
  </div>
</body>
</html>
"""
    path = output_dir / 'openclaw_workspace_dashboard.html'
    path.write_text(html, encoding='utf-8')
    return path
