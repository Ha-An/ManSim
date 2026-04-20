from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def export_replay_dashboard(*, output_dir: Path, events: list[dict[str, Any]]) -> Path:
    safe_events = events[:5000]
    payload = json.dumps(safe_events, ensure_ascii=False)
    html_text = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>ManSim Replay Dashboard</title>
  <style>
    body {{ font-family: Segoe UI, sans-serif; margin: 24px; color: #1f2a3d; background: #f4f7fb; }}
    .controls {{ display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }}
    .panel {{ background: white; border-radius: 14px; padding: 16px; box-shadow: 0 8px 24px rgba(15, 27, 48, 0.08); }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: 10px; border-bottom: 1px solid #e8edf5; text-align: left; vertical-align: top; }}
    input, select {{ padding: 8px 10px; }}
    pre {{ margin: 0; white-space: pre-wrap; font-family: Consolas, monospace; font-size: 12px; }}
  </style>
</head>
<body>
  <h1>Replay Dashboard</h1>
  <div class="controls panel">
    <label>Day <select id="dayFilter"><option value="">All</option></select></label>
    <label>Entity <input id="entityFilter" placeholder="A1 / S1M1 / PRODUCT" /></label>
    <label>Event <input id="eventFilter" placeholder="MACHINE_BROKEN" /></label>
  </div>
  <div class="panel">
    <table>
      <thead><tr><th>t</th><th>day</th><th>event</th><th>entity</th><th>location</th><th>details</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <script>
    const events = {payload};
    const dayFilter = document.getElementById('dayFilter');
    const entityFilter = document.getElementById('entityFilter');
    const eventFilter = document.getElementById('eventFilter');
    const rows = document.getElementById('rows');
    const days = [...new Set(events.map(e => String(e.day)))].sort((a, b) => Number(a) - Number(b));
    for (const day of days) {{
      const opt = document.createElement('option');
      opt.value = day;
      opt.textContent = day;
      dayFilter.appendChild(opt);
    }}
    function render() {{
      const day = dayFilter.value.trim();
      const entity = entityFilter.value.trim().toLowerCase();
      const eventText = eventFilter.value.trim().toLowerCase();
      const filtered = events.filter(e => {{
        if (day && String(e.day) !== day) return false;
        if (entity && !String(e.entity_id || '').toLowerCase().includes(entity)) return false;
        if (eventText && !String(e.type || '').toLowerCase().includes(eventText)) return false;
        return true;
      }});
      rows.innerHTML = filtered.map(e => `
        <tr>
          <td>${{e.t}}</td>
          <td>${{e.day}}</td>
          <td>${{e.type}}</td>
          <td>${{e.entity_id || ''}}</td>
          <td>${{e.location || ''}}</td>
          <td><pre>${{JSON.stringify(e.details || {{}}, null, 2)}}</pre></td>
        </tr>`).join('');
    }}
    dayFilter.addEventListener('change', render);
    entityFilter.addEventListener('input', render);
    eventFilter.addEventListener('input', render);
    render();
  </script>
</body>
</html>"""
    output_path = Path(output_dir) / "replay_dashboard.html"
    output_path.write_text(html_text, encoding="utf-8")
    return output_path
