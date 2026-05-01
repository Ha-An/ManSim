from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any


MANAGER_REPLAY_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Manager Replay</title>
  <style>
    :root {
      --bg: #12172a;
      --bg-2: #18203a;
      --panel: #1d2746;
      --panel-2: #111a31;
      --ink: #ebf3ff;
      --muted: #94a8cc;
      --accent: #77a8ff;
      --accent-2: #68e1ff;
      --line: rgba(129, 170, 255, 0.28);
      --pixel: rgba(128, 180, 255, 0.16);
      --strategist: #73d7ff;
      --compiler: #a492ff;
      --reviewer: #ffbd6e;
      --good: #56d392;
      --warn: #ffb347;
      --bad: #ff707f;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      font-family: Consolas, "Courier New", monospace;
      color: var(--ink);
      background:
        radial-gradient(circle at 1px 1px, rgba(114, 166, 255, 0.18) 1.2px, transparent 0) 0 0 / 24px 24px,
        linear-gradient(180deg, #12172a 0%, #101427 100%);
      min-height: 100vh;
    }

    .shell {
      max-width: 1680px;
      margin: 0 auto;
      padding: 20px 20px 28px;
    }

    .topbar {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 16px;
    }

    .title {
      margin: 0;
      font-size: 38px;
      letter-spacing: 0.04em;
    }

    .subtitle {
      margin-top: 8px;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.4;
    }

    .meta-strip {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }

    .meta-chip,
    .select-wrap,
    .phase-button,
    .timeline-phase {
      border: 1px solid rgba(122, 169, 255, 0.35);
      background: rgba(21, 31, 60, 0.88);
      color: var(--ink);
      border-radius: 14px;
      box-shadow: 0 0 0 1px rgba(111, 162, 255, 0.08) inset;
    }

    .meta-chip {
      padding: 10px 14px;
      font-size: 13px;
      color: var(--muted);
    }

    .meta-chip strong {
      display: block;
      color: var(--ink);
      font-size: 16px;
      margin-top: 4px;
    }

    .controls {
      display: grid;
      grid-template-columns: 220px 1fr;
      gap: 14px;
      align-items: start;
      margin-bottom: 18px;
    }

    .control-block,
    .main-panel,
    .timeline-panel {
      background: rgba(17, 26, 49, 0.92);
      border: 1px solid rgba(122, 169, 255, 0.25);
      border-radius: 20px;
      box-shadow: 0 18px 44px rgba(0, 0, 0, 0.18);
    }

    .control-block {
      padding: 14px 16px 16px;
    }

    .control-label {
      font-size: 12px;
      letter-spacing: 0.14em;
      color: var(--muted);
      margin-bottom: 10px;
      text-transform: uppercase;
    }

    .select-wrap {
      display: inline-flex;
      align-items: center;
      padding: 0 10px;
      min-width: 100%;
      height: 48px;
    }

    .select-wrap select {
      width: 100%;
      background: transparent;
      border: none;
      color: var(--ink);
      font-family: inherit;
      font-size: 16px;
      outline: none;
      appearance: none;
    }

    .select-wrap::after {
      content: "▾";
      color: var(--muted);
      margin-left: 8px;
    }

    .phase-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      padding: 14px;
      align-items: center;
    }

    .phase-button {
      padding: 12px 16px;
      font-family: inherit;
      font-size: 15px;
      cursor: pointer;
      transition: transform 120ms ease, border-color 120ms ease, background 120ms ease;
    }

    .phase-button:hover {
      transform: translateY(-1px);
      border-color: rgba(147, 192, 255, 0.55);
    }

    .phase-button[data-active="true"] {
      background: linear-gradient(180deg, rgba(62, 103, 195, 0.95), rgba(44, 86, 181, 0.95));
      border-color: rgba(135, 185, 255, 0.65);
    }

    .main-grid {
      display: grid;
      grid-template-columns: minmax(360px, 0.95fr) minmax(420px, 1.15fr) minmax(340px, 0.78fr);
      gap: 18px;
      padding: 18px;
    }

    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-bottom: 14px;
    }

    .panel-title {
      margin: 0;
      font-size: 24px;
      letter-spacing: 0.04em;
    }

    .panel-subtitle {
      margin: 0;
      color: var(--muted);
      font-size: 13px;
    }

    .lane-panel,
    .scene-panel,
    .detail-panel {
      padding: 18px;
      min-height: 620px;
    }

    .lane-stack {
      position: relative;
      display: grid;
      gap: 20px;
      padding: 10px 10px 10px 14px;
      min-height: 520px;
    }

    .lane-stack::before {
      content: "";
      position: absolute;
      left: 58px;
      top: 28px;
      bottom: 28px;
      width: 2px;
      background: linear-gradient(180deg, rgba(115, 215, 255, 0.35), rgba(164, 146, 255, 0.35) 48%, rgba(255, 189, 110, 0.35));
    }

    .phase-card {
      position: relative;
      display: grid;
      grid-template-columns: 84px 1fr;
      gap: 16px;
      padding: 18px 18px 18px 18px;
      border-radius: 18px;
      background: rgba(31, 42, 75, 0.76);
      border: 1px solid rgba(132, 181, 255, 0.18);
      box-shadow: 0 0 0 1px rgba(120, 174, 255, 0.05) inset;
      cursor: pointer;
      transition: border-color 140ms ease, transform 140ms ease, box-shadow 140ms ease;
    }

    .phase-card:hover {
      transform: translateY(-1px);
      border-color: rgba(156, 197, 255, 0.45);
    }

    .phase-card[data-active="true"] {
      border-color: rgba(144, 193, 255, 0.66);
      box-shadow: 0 0 22px rgba(95, 151, 255, 0.18), 0 0 0 1px rgba(130, 184, 255, 0.1) inset;
    }

    .phase-node {
      position: relative;
      width: 72px;
      height: 72px;
      border-radius: 18px;
      display: grid;
      place-items: center;
      border: 1px solid rgba(146, 194, 255, 0.28);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.03), transparent),
        radial-gradient(circle at 2px 2px, rgba(155, 196, 255, 0.22) 1px, transparent 0) 0 0 / 14px 14px,
        rgba(14, 22, 42, 0.92);
      overflow: hidden;
    }

    .phase-node img {
      width: 54px;
      height: 54px;
      image-rendering: pixelated;
      image-rendering: crisp-edges;
      object-fit: contain;
      filter: drop-shadow(0 3px 8px rgba(0, 0, 0, 0.35));
    }

    .phase-badge {
      position: absolute;
      top: 6px;
      right: 6px;
      min-width: 22px;
      height: 22px;
      padding: 0 6px;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 11px;
      font-weight: 700;
      color: #081220;
      background: var(--accent);
    }

    .phase-card[data-role="strategist"] .phase-badge { background: var(--strategist); }
    .phase-card[data-role="compiler"] .phase-badge { background: var(--compiler); color: #160c26; }
    .phase-card[data-role="reviewer"] .phase-badge { background: var(--reviewer); color: #2a1400; }

    .packet {
      position: absolute;
      left: 53px;
      width: 12px;
      height: 12px;
      border-radius: 4px;
      background: var(--accent-2);
      box-shadow: 0 0 10px rgba(104, 225, 255, 0.55);
      animation: laneFloat 1.6s linear infinite;
    }

    .packet.packet-1 { top: 118px; animation-delay: 0s; }
    .packet.packet-2 { top: 298px; animation-delay: 0.8s; }

    @keyframes laneFloat {
      0% { transform: translateY(0); opacity: 0.2; }
      20% { opacity: 0.95; }
      100% { transform: translateY(168px); opacity: 0; }
    }

    .phase-title {
      display: flex;
      gap: 8px;
      align-items: baseline;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }

    .phase-title strong {
      font-size: 20px;
    }

    .phase-title span {
      color: var(--muted);
      font-size: 13px;
    }

    .phase-summary {
      font-size: 14px;
      line-height: 1.45;
      color: var(--ink);
      margin-bottom: 10px;
    }

    .phase-excerpt {
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }

    .metric-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .mini-chip {
      padding: 6px 8px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(138, 182, 255, 0.22);
      color: var(--muted);
      font-size: 12px;
    }

    .scene-frame {
      background:
        radial-gradient(circle at 2px 2px, rgba(126, 177, 255, 0.22) 1px, transparent 0) 0 0 / 18px 18px,
        #eef5ff;
      border-radius: 22px;
      border: 2px solid rgba(128, 171, 255, 0.44);
      min-height: 520px;
      padding: 18px;
      position: relative;
      overflow: hidden;
    }

    .scene-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(180px, 1fr));
      gap: 16px;
    }

    .scene-region {
      min-height: 180px;
      background:
        radial-gradient(circle at 1.5px 1.5px, rgba(121, 171, 255, 0.18) 1px, transparent 0) 0 0 / 15px 15px,
        rgba(255, 255, 255, 0.92);
      border: 2px solid rgba(117, 164, 255, 0.62);
      border-radius: 18px;
      padding: 14px;
      color: #24446d;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.5);
    }

    .scene-region[data-highlighted="true"] {
      box-shadow: 0 0 0 2px rgba(114, 220, 255, 0.3) inset, 0 0 18px rgba(116, 210, 255, 0.18);
      border-color: rgba(113, 219, 255, 0.78);
    }

    .scene-region.warehouse {
      background:
        linear-gradient(180deg, rgba(191, 215, 255, 0.22), rgba(191, 215, 255, 0.08)),
        radial-gradient(circle at 1.5px 1.5px, rgba(124, 171, 255, 0.16) 1px, transparent 0) 0 0 / 16px 16px,
        rgba(255, 255, 255, 0.96);
    }

    .scene-region.battery {
      background:
        linear-gradient(90deg, rgba(115, 237, 170, 0.09) 0 16%, transparent 16% 33%, rgba(115, 237, 170, 0.09) 33% 49%, transparent 49% 66%, rgba(115, 237, 170, 0.09) 66% 82%, transparent 82% 100%),
        rgba(255, 255, 255, 0.96);
    }

    .scene-region header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 8px;
      margin-bottom: 10px;
    }

    .scene-region h3 {
      margin: 0;
      font-size: 18px;
      color: #213b67;
    }

    .region-kpi {
      font-size: 12px;
      color: #4a6798;
    }

    .region-metrics {
      display: grid;
      gap: 8px;
      margin-bottom: 12px;
    }

    .metric-line {
      padding: 8px 10px;
      border-radius: 12px;
      background: rgba(120, 174, 255, 0.08);
      border: 1px dashed rgba(106, 163, 255, 0.28);
    }

    .metric-line strong {
      display: block;
      font-size: 13px;
      color: #25416d;
      margin-bottom: 4px;
    }

    .metric-line small {
      color: #5c78a8;
      font-size: 11px;
    }

    .delta {
      display: inline-flex;
      gap: 6px;
      align-items: center;
      font-size: 12px;
      color: #213b67;
      margin-top: 4px;
    }

    .delta.bad { color: #af3244; }
    .delta.good { color: #157b54; }
    .delta.neutral { color: #50698f; }

    .machines,
    .roles {
      display: grid;
      gap: 8px;
    }

    .machine-row,
    .role-row {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      padding: 6px 8px;
      border-radius: 12px;
      background: rgba(20, 35, 65, 0.06);
    }

    .machine-row strong,
    .role-row strong {
      color: #294570;
      font-size: 13px;
    }

    .machine-row span,
    .role-row span {
      color: #5974a2;
      font-size: 12px;
      text-align: right;
    }

    .detail-panel {
      display: grid;
      gap: 14px;
      align-content: start;
    }

    .detail-card {
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(30, 41, 73, 0.84);
      border: 1px solid rgba(128, 178, 255, 0.22);
    }

    .detail-card h3 {
      margin: 0 0 10px;
      font-size: 16px;
      letter-spacing: 0.05em;
      color: var(--muted);
      text-transform: uppercase;
    }

    .detail-card p,
    .detail-card li {
      margin: 0;
      font-size: 14px;
      line-height: 1.55;
      color: var(--ink);
    }

    .detail-card ul {
      margin: 0;
      padding-left: 18px;
      display: grid;
      gap: 6px;
    }

    .structured-grid {
      display: grid;
      gap: 8px;
    }

    .structured-row {
      padding: 10px 11px;
      border-radius: 12px;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(137, 185, 255, 0.12);
    }

    .structured-row strong {
      display: block;
      font-size: 12px;
      color: var(--muted);
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 5px;
    }

    .timeline-panel {
      margin-top: 18px;
      padding: 16px;
    }

    .timeline-grid {
      display: grid;
      gap: 12px;
    }

    .timeline-day {
      display: grid;
      grid-template-columns: 68px 1fr;
      gap: 12px;
      align-items: center;
    }

    .timeline-day-label {
      font-size: 18px;
      color: var(--ink);
    }

    .timeline-phases {
      display: grid;
      grid-template-columns: repeat(4, minmax(88px, 1fr));
      gap: 8px;
    }

    .timeline-phase {
      min-height: 58px;
      padding: 10px 12px;
      display: grid;
      gap: 3px;
      cursor: pointer;
      transition: border-color 120ms ease, transform 120ms ease;
    }

    .timeline-phase:hover {
      border-color: rgba(151, 193, 255, 0.58);
      transform: translateY(-1px);
    }

    .timeline-phase[data-active="true"] {
      background: linear-gradient(180deg, rgba(58, 100, 188, 0.95), rgba(40, 79, 169, 0.95));
      border-color: rgba(151, 193, 255, 0.68);
    }

    .timeline-phase small {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
    }

    .timeline-phase strong {
      font-size: 14px;
      color: var(--ink);
    }

    .empty {
      padding: 30px;
      border-radius: 20px;
      background: rgba(17, 26, 49, 0.92);
      border: 1px solid rgba(122, 169, 255, 0.25);
      color: var(--muted);
      text-align: center;
      font-size: 15px;
    }

    @media (max-width: 1320px) {
      .main-grid { grid-template-columns: 1fr; }
      .lane-panel, .scene-panel, .detail-panel { min-height: auto; }
      .timeline-phases { grid-template-columns: repeat(2, minmax(120px, 1fr)); }
      .controls { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div id="app" class="shell"></div>
  <script>
    const EMBEDDED_PAYLOAD = __PAYLOAD_JSON__;
    const PAYLOAD_URL = new URL("./manager_replay.json", window.location.href).toString();

    function clamp(value, min, max) {
      return Math.max(min, Math.min(max, value));
    }

    function fmtNumber(value) {
      const num = Number(value || 0);
      return Number.isFinite(num) ? num.toLocaleString() : "-";
    }

    function fmtDelta(delta) {
      const num = Number(delta || 0);
      if (!Number.isFinite(num) || num === 0) return { text: "0", cls: "neutral" };
      return { text: `${num > 0 ? "+" : ""}${num}`, cls: num > 0 ? "good" : "bad" };
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/\"/g, "&quot;")
        .replace(/'/g, "&#039;");
    }

    async function loadPayload() {
      if (EMBEDDED_PAYLOAD && typeof EMBEDDED_PAYLOAD === "object") {
        return EMBEDDED_PAYLOAD;
      }
      const response = await fetch(PAYLOAD_URL, { cache: "no-store" });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return response.json();
    }

    function buildMeta(payload) {
      const meta = payload.meta || {};
      return `
        <div class="topbar">
          <div>
            <h1 class="title">Manager Replay</h1>
            <div class="subtitle">${escapeHtml(meta.mode || "-")} / ${escapeHtml(meta.model || "-")} / ${escapeHtml(meta.total_days || "-")} days / ${escapeHtml(meta.wall_clock_human || "-")}</div>
          </div>
          <div class="meta-strip">
            <div class="meta-chip">Run<strong>${escapeHtml(meta.run_id || "-")}</strong></div>
            <div class="meta-chip">Products<strong>${fmtNumber(meta.total_products)}</strong></div>
            <div class="meta-chip">Closure<strong>${Number(meta.closure_ratio || 0).toFixed(3)}</strong></div>
          </div>
        </div>
      `;
    }

    function buildPhaseButtons(day, selectedPhaseId) {
      const buttons = (day.phases || []).map((phase) => `
        <button class="phase-button" data-phase-id="${escapeHtml(phase.id)}" data-active="${String(phase.id === selectedPhaseId)}">
          ${escapeHtml(String(phase.phase_type || "").toUpperCase())}
        </button>
      `).join("");
      return `<div class="control-block"><div class="control-label">Phase</div><div class="phase-row">${buttons}</div></div>`;
    }

    function buildLane(day, selectedPhaseId, assets) {
      const phases = day.phases || [];
      return `
        <div class="lane-panel main-panel">
          <div class="panel-head">
            <div>
              <h2 class="panel-title">Manager Orchestration</h2>
              <p class="panel-subtitle">Strategist -> Compiler -> Reviewer with explicit handoff.</p>
            </div>
          </div>
          <div class="lane-stack">
            <div class="packet packet-1"></div>
            <div class="packet packet-2"></div>
            ${phases.map((phase) => {
              const role = String(phase.phase_type || "");
              const active = phase.id === selectedPhaseId;
              const sprite = assets.manager_sprite || "";
              const badges = { strategist: "ST", compiler: "CP", reviewer: "RV" };
              const meta = phase.meta || {};
              const chips = [];
              if (meta.latency_ms != null) chips.push(`${fmtNumber(Math.round(Number(meta.latency_ms)))} ms`);
              if (meta.plan_revision != null) chips.push(`rev ${escapeHtml(meta.plan_revision)}`);
              if (meta.target_count != null) chips.push(`${escapeHtml(meta.target_count)} targets`);
              return `
                <div class="phase-card" data-phase-id="${escapeHtml(phase.id)}" data-role="${escapeHtml(role)}" data-active="${String(active)}">
                  <div class="phase-node">
                    ${sprite ? `<img src="${sprite}" alt="" />` : ""}
                    <span class="phase-badge">${escapeHtml(badges[role] || "MG")}</span>
                  </div>
                  <div>
                    <div class="phase-title">
                      <strong>${escapeHtml(phase.actor_label || role)}</strong>
                      <span>${escapeHtml(phase.time_label || "")}</span>
                    </div>
                    <div class="phase-summary">${escapeHtml(phase.decision_summary || "-")}</div>
                    <div class="phase-excerpt">${escapeHtml(phase.excerpt || "-")}</div>
                    <div class="metric-row">${chips.map((chip) => `<span class="mini-chip">${escapeHtml(chip)}</span>`).join("")}</div>
                  </div>
                </div>
              `;
            }).join("")}
          </div>
        </div>
      `;
    }

    function buildScene(day, selectedPhaseId) {
      const scene = day.scene || {};
      const selected = (day.phases || []).find((phase) => phase.id === selectedPhaseId) || (day.phases || [])[0] || {};
      const highlightSet = new Set((selected.factory_effect || {}).highlights || []);
      return `
        <div class="scene-panel main-panel">
          <div class="panel-head">
            <div>
              <h2 class="panel-title">Factory Effect</h2>
              <p class="panel-subtitle">Phase-aligned view of floor state and end-of-day deltas.</p>
            </div>
          </div>
          <div class="scene-frame">
            <div class="scene-grid">
              ${(scene.regions || []).map((region) => {
                const regionClass = String(region.id || "");
                return `
                  <section class="scene-region ${escapeHtml(regionClass)}" data-highlighted="${String(highlightSet.has(region.id))}">
                    <header>
                      <h3>${escapeHtml(region.label || region.id || "-")}</h3>
                      <span class="region-kpi">${escapeHtml(region.kpi || "")}</span>
                    </header>
                    <div class="region-metrics">
                      ${(region.metrics || []).map((metric) => {
                        const delta = fmtDelta(metric.delta);
                        return `
                          <div class="metric-line">
                            <strong>${escapeHtml(metric.label || "-")}</strong>
                            <small>${escapeHtml(metric.start_label || "Start")} ${fmtNumber(metric.start)} -> ${escapeHtml(metric.end_label || "End")} ${fmtNumber(metric.end)}</small>
                            <div class="delta ${delta.cls}">${escapeHtml(delta.text)}</div>
                          </div>
                        `;
                      }).join("")}
                    </div>
                    ${(region.machines || []).length ? `
                      <div class="machines">
                        ${(region.machines || []).map((machine) => `
                          <div class="machine-row">
                            <strong>${escapeHtml(machine.id || "-")}</strong>
                            <span>${escapeHtml(machine.start_state || "-")} -> ${escapeHtml(machine.end_state || "-")}</span>
                          </div>
                        `).join("")}
                      </div>
                    ` : ""}
                    ${(region.roles || []).length ? `
                      <div class="roles">
                        ${(region.roles || []).map((role) => `
                          <div class="role-row">
                            <strong>${escapeHtml(role.worker || "-")}</strong>
                            <span>${escapeHtml(role.role || "-")}${role.changed ? " *" : ""}</span>
                          </div>
                        `).join("")}
                      </div>
                    ` : ""}
                  </section>
                `;
              }).join("")}
            </div>
          </div>
        </div>
      `;
    }

    function renderStructuredRows(structured) {
      const entries = Object.entries(structured || {}).filter(([, value]) => value != null && String(JSON.stringify(value)).trim() !== "");
      if (!entries.length) return "<div class=\"structured-row\"><strong>Data</strong><div>-</div></div>";
      return entries.map(([key, value]) => `
        <div class="structured-row">
          <strong>${escapeHtml(key.replace(/_/g, " "))}</strong>
          <div>${escapeHtml(Array.isArray(value) || typeof value === "object" ? JSON.stringify(value) : String(value))}</div>
        </div>
      `).join("");
    }

    function renderList(values, emptyText = "-") {
      const rows = (values || []).filter(Boolean);
      if (!rows.length) return `<p>${escapeHtml(emptyText)}</p>`;
      return `<ul>${rows.map((value) => `<li>${escapeHtml(value)}</li>`).join("")}</ul>`;
    }

    function buildDetail(day, selectedPhaseId) {
      const phase = (day.phases || []).find((row) => row.id === selectedPhaseId) || (day.phases || [])[0] || {};
      const effect = phase.factory_effect || {};
      return `
        <div class="detail-panel main-panel">
          <div class="detail-card">
            <h3>Current Decision</h3>
            <p>${escapeHtml(phase.decision_summary || "-")}</p>
            <div class="metric-row">
              <span class="mini-chip">${escapeHtml(phase.actor_label || "-")}</span>
              <span class="mini-chip">${escapeHtml(phase.time_label || "-")}</span>
            </div>
          </div>
          <div class="detail-card">
            <h3>Why It Changed</h3>
            ${renderList(phase.why_changed || [], "No explicit rationale captured.")}
          </div>
          <div class="detail-card">
            <h3>Factory Effect</h3>
            ${renderList(effect.summary_lines || [], "No factory effect summary.")}
          </div>
          <div class="detail-card">
            <h3>Carry-Forward Risks</h3>
            ${renderList(phase.carry_forward_risks || [], "No carry-forward risks.")}
          </div>
          <div class="detail-card">
            <h3>Structured Decision</h3>
            <div class="structured-grid">${renderStructuredRows(phase.decision_structured || {})}</div>
          </div>
          <div class="detail-card">
            <h3>Input Summary</h3>
            ${renderList(phase.inputs_summary || [], "No input summary.")}
          </div>
        </div>
      `;
    }

    function buildTimeline(payload, selectedDay, selectedPhaseId) {
      return `
        <div class="timeline-panel">
          <div class="panel-head">
            <div>
              <h2 class="panel-title">Day-Phase Timeline</h2>
              <p class="panel-subtitle">Read each day as strategist -> compiler -> reviewer.</p>
            </div>
          </div>
          <div class="timeline-grid">
            ${(payload.days || []).map((day) => `
              <div class="timeline-day">
                <div class="timeline-day-label">D${escapeHtml(day.day)}</div>
                <div class="timeline-phases">
                  ${(day.phases || []).map((phase) => `
                    <button class="timeline-phase" data-day="${escapeHtml(day.day)}" data-phase-id="${escapeHtml(phase.id)}" data-active="${String(day.day === selectedDay && phase.id === selectedPhaseId)}">
                      <small>${escapeHtml(phase.phase_type || "-")}</small>
                      <strong>${escapeHtml(phase.actor_label || "-")}</strong>
                    </button>
                  `).join("")}
                  <div class="timeline-phase" data-active="false">
                    <small>impact</small>
                    <strong>${fmtNumber((day.day_kpis || {}).products || 0)} products</strong>
                  </div>
                </div>
              </div>
            `).join("")}
          </div>
        </div>
      `;
    }

    function renderApp(payload, state) {
      const selectedDayValue = Number((state.selectedDay ?? (((payload.days || [])[0] || {}).day)) || 1);
      const day = (payload.days || []).find((row) => Number(row.day) === selectedDayValue) || (payload.days || [])[0] || null;
      if (!day) {
        return `<div class="empty">No manager replay payload was generated for this run.</div>`;
      }
      const selectedPhaseId = state.selectedPhaseId || ((day.phases || [])[0] || {}).id || "";
      return `
        ${buildMeta(payload)}
        <section class="controls">
          <div class="control-block">
            <div class="control-label">Day</div>
            <label class="select-wrap">
              <select id="day-select">
                ${(payload.days || []).map((entry) => `<option value="${escapeHtml(entry.day)}" ${Number(entry.day) === Number(selectedDayValue) ? "selected" : ""}>Day ${escapeHtml(entry.day)}</option>`).join("")}
              </select>
            </label>
          </div>
          ${buildPhaseButtons(day, selectedPhaseId)}
        </section>
        <section class="main-grid">
          ${buildLane(day, selectedPhaseId, payload.assets || {})}
          ${buildScene(day, selectedPhaseId)}
          ${buildDetail(day, selectedPhaseId)}
        </section>
        ${buildTimeline(payload, Number(day.day), selectedPhaseId)}
      `;
    }

    function attachHandlers(root, payload, state) {
      const daySelect = root.querySelector("#day-select");
      if (daySelect) {
        daySelect.addEventListener("change", (event) => {
          const nextDay = Number(event.target.value);
          const day = (payload.days || []).find((row) => Number(row.day) === nextDay);
          state.selectedDay = nextDay;
          state.selectedPhaseId = ((day?.phases || [])[0] || {}).id || "";
          redraw();
        });
      }

      root.querySelectorAll("[data-phase-id]").forEach((node) => {
        node.addEventListener("click", () => {
          const phaseId = node.getAttribute("data-phase-id") || "";
          const dayAttr = node.getAttribute("data-day");
          if (dayAttr) state.selectedDay = Number(dayAttr);
          state.selectedPhaseId = phaseId;
          redraw();
        });
      });
    }

    let appState = { selectedDay: null, selectedPhaseId: null };
    let payloadRef = null;

    function redraw() {
      const root = document.getElementById("app");
      root.innerHTML = renderApp(payloadRef, appState);
      attachHandlers(root, payloadRef, appState);
    }

    loadPayload()
      .then((payload) => {
        payloadRef = payload || {};
        const lastDay = (payloadRef.days || [])[Math.max(0, (payloadRef.days || []).length - 1)] || null;
        appState.selectedDay = lastDay ? Number(lastDay.day) : null;
        appState.selectedPhaseId = lastDay && (lastDay.phases || []).length ? lastDay.phases[lastDay.phases.length - 1].id : null;
        redraw();
      })
      .catch((error) => {
        const root = document.getElementById("app");
        root.innerHTML = `<div class="empty">Failed to load manager_replay.json: ${escapeHtml(String(error))}</div>`;
      });
  </script>
</body>
</html>
"""


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


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _load_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _load_snapshots(path: Path) -> list[dict[str, Any]]:
    payload = _load_json(path)
    rows = payload.get("snapshots", [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _to_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_fence(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _excerpt(text: Any, max_len: int = 180) -> str:
    value = _strip_fence(str(text or "")).replace("\n", " ").strip()
    if not value:
        return "-"
    if len(value) <= max_len:
        return value
    return value[: max_len - 3].rstrip() + "..."


def _asset_data_uri(path: Path) -> str:
    if not path.exists():
        return ""
    suffix = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(suffix)
    if mime is None:
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _nearest_day_snapshots(day: int, snapshots: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    day_rows = [row for row in snapshots if _safe_int(row.get("day")) == day]
    if day_rows:
        return day_rows[0], day_rows[-1]
    if not snapshots:
        return {}, {}
    before = [row for row in snapshots if _safe_int(row.get("day")) <= day]
    after = [row for row in snapshots if _safe_int(row.get("day")) >= day]
    start = before[-1] if before else snapshots[0]
    end = after[0] if after else snapshots[-1]
    return start, end


def _build_snapshot_blob(snapshot: dict[str, Any]) -> dict[str, Any]:
    material = snapshot.get("material_queue_lengths", {}) if isinstance(snapshot.get("material_queue_lengths", {}), dict) else {}
    intermediate = snapshot.get("intermediate_queue_lengths", {}) if isinstance(snapshot.get("intermediate_queue_lengths", {}), dict) else {}
    output = snapshot.get("output_buffer_lengths", {}) if isinstance(snapshot.get("output_buffer_lengths", {}), dict) else {}
    machines = snapshot.get("machine_states", {}) if isinstance(snapshot.get("machine_states", {}), dict) else {}
    return {
        "t": _safe_float(snapshot.get("t")),
        "day": _safe_int(snapshot.get("day")),
        "queues": {
            "station1_material": _safe_int(material.get("1")),
            "station2_material": _safe_int(material.get("2")),
            "station2_intermediate": _safe_int(intermediate.get("2")),
            "inspection_queue": _safe_int(intermediate.get("4")),
            "station1_output": _safe_int(output.get("1")),
            "station2_output": _safe_int(output.get("2")),
            "inspection_output": _safe_int(output.get("4")),
        },
        "machines": {key: str(value) for key, value in machines.items()},
        "inspection_active_agents": _safe_int(snapshot.get("inspection_active_agents")),
        "incident_count": _safe_int(snapshot.get("incident_count")),
        "commitment_count": _safe_int(snapshot.get("commitment_count")),
    }


def _queue_metric(label: str, start: int, end: int) -> dict[str, Any]:
    return {
        "label": label,
        "start_label": "Start",
        "end_label": "End",
        "start": start,
        "end": end,
        "delta": end - start,
    }


def _top_machine_hotspots(daily_row: dict[str, Any], start_snapshot: dict[str, Any], end_snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    broken = daily_row.get("machine_broken_min", {}) if isinstance(daily_row.get("machine_broken_min", {}), dict) else {}
    ranked = sorted(((str(key), _safe_float(value)) for key, value in broken.items()), key=lambda item: item[1], reverse=True)
    start_states = start_snapshot.get("machines", {}) if isinstance(start_snapshot.get("machines", {}), dict) else {}
    end_states = end_snapshot.get("machines", {}) if isinstance(end_snapshot.get("machines", {}), dict) else {}
    rows: list[dict[str, Any]] = []
    for machine_id, broken_min in ranked[:4]:
        rows.append(
            {
                "id": machine_id,
                "start_state": str(start_states.get(machine_id, "-")),
                "end_state": str(end_states.get(machine_id, "-")),
                "broken_min": round(broken_min, 1),
            }
        )
    return rows


def _role_rows(current_policy: dict[str, Any], previous_policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    current_roles = current_policy.get("worker_roles", {}) if isinstance(current_policy.get("worker_roles", {}), dict) else {}
    previous_roles = previous_policy.get("worker_roles", {}) if isinstance(previous_policy, dict) and isinstance(previous_policy.get("worker_roles", {}), dict) else {}
    rows: list[dict[str, Any]] = []
    for worker_id in sorted(current_roles.keys()):
        role = str(current_roles.get(worker_id, "")).strip() or "-"
        rows.append(
            {
                "worker": str(worker_id),
                "role": role,
                "changed": str(previous_roles.get(worker_id, "")).strip() != role,
            }
        )
    return rows


def _highlight_regions(phase_type: str, structured: dict[str, Any]) -> list[str]:
    targets: set[str] = set()
    if phase_type == "strategist":
        focus = str(structured.get("operating_focus", "")).strip().lower()
        prevention = structured.get("prevention_targets", [])
        if "flow" in focus:
            targets.update({"station1", "station2"})
        if "closeout" in json.dumps(prevention, ensure_ascii=False):
            targets.update({"inspection", "warehouse"})
        if "reliability" in json.dumps(prevention, ensure_ascii=False):
            targets.update({"station1", "station2"})
    elif phase_type == "compiler":
        roles = structured.get("worker_roles", {}) if isinstance(structured.get("worker_roles", {}), dict) else {}
        if any("inspection" in str(role).lower() for role in roles.values()):
            targets.add("inspection")
        if any("intake" in str(role).lower() for role in roles.values()):
            targets.update({"station1", "station2"})
        if any("reliability" in str(role).lower() for role in roles.values()):
            targets.update({"station1", "station2"})
    elif phase_type == "reviewer":
        misses = json.dumps(structured.get("target_misses", []), ensure_ascii=False)
        failure_modes = json.dumps(structured.get("top_failure_modes", []), ensure_ascii=False)
        if "closeout" in misses or "closeout" in failure_modes:
            targets.update({"inspection", "warehouse"})
        if "reliability" in failure_modes:
            targets.update({"station1", "station2"})
        if "flow" in failure_modes:
            targets.update({"station1", "station2"})
    return sorted(targets)


def _day_event_counts(day: int, events: list[dict[str, Any]]) -> dict[str, int]:
    rows = [row for row in events if _safe_int(row.get("day")) == day]
    return {
        "breakdowns": sum(1 for row in rows if str(row.get("type", "")).strip() == "MACHINE_BROKEN"),
        "repairs": sum(1 for row in rows if str(row.get("type", "")).strip() == "MACHINE_REPAIRED"),
        "discharges": sum(1 for row in rows if str(row.get("type", "")).strip() == "AGENT_DISCHARGED"),
        "completed_products": sum(1 for row in rows if str(row.get("type", "")).strip() == "COMPLETED_PRODUCT"),
    }


def _build_scene(
    *,
    day: int,
    daily_row: dict[str, Any],
    policy_row: dict[str, Any],
    previous_policy_row: dict[str, Any] | None,
    start_snapshot: dict[str, Any],
    end_snapshot: dict[str, Any],
) -> dict[str, Any]:
    start_queues = start_snapshot.get("queues", {}) if isinstance(start_snapshot.get("queues", {}), dict) else {}
    end_queues = end_snapshot.get("queues", {}) if isinstance(end_snapshot.get("queues", {}), dict) else {}
    hotspots = _top_machine_hotspots(daily_row, start_snapshot, end_snapshot)
    machine_by_station = {
        "station1": [row for row in hotspots if str(row.get("id", "")).startswith("S1")],
        "station2": [row for row in hotspots if str(row.get("id", "")).startswith("S2")],
    }
    cumulative_products = _safe_int(policy_row.get("__cumulative_products"))
    regions = [
        {
            "id": "warehouse",
            "label": "Warehouse",
            "kpi": f"Completed {cumulative_products}",
            "metrics": [
                _queue_metric("Completed Today", 0, _safe_int(daily_row.get("products"))),
                _queue_metric("Close-out Gap", 0, _safe_int(daily_row.get("inspection_backlog_end"))),
            ],
            "machines": [],
            "roles": [],
        },
        {
            "id": "station1",
            "label": "Station 1",
            "kpi": f"Completions {_safe_int(daily_row.get('station1_completions'))}",
            "metrics": [
                _queue_metric("Material Queue", _safe_int(start_queues.get("station1_material")), _safe_int(end_queues.get("station1_material"))),
                _queue_metric("Output Queue", _safe_int(start_queues.get("station1_output")), _safe_int(end_queues.get("station1_output"))),
            ],
            "machines": machine_by_station["station1"],
            "roles": [row for row in _role_rows(policy_row, previous_policy_row) if row["worker"] in {"A1", "A2", "A3"}],
        },
        {
            "id": "station2",
            "label": "Station 2",
            "kpi": f"Completions {_safe_int(daily_row.get('station2_completions'))}",
            "metrics": [
                _queue_metric("Material Queue", _safe_int(start_queues.get("station2_material")), _safe_int(end_queues.get("station2_material"))),
                _queue_metric("Intermediate Queue", _safe_int(start_queues.get("station2_intermediate")), _safe_int(end_queues.get("station2_intermediate"))),
                _queue_metric("Output Queue", _safe_int(start_queues.get("station2_output")), _safe_int(end_queues.get("station2_output"))),
            ],
            "machines": machine_by_station["station2"],
            "roles": [],
        },
        {
            "id": "inspection",
            "label": "Inspection",
            "kpi": f"Passes {_safe_int(daily_row.get('inspection_passes'))}",
            "metrics": [
                _queue_metric("Inspection Queue", _safe_int(start_queues.get("inspection_queue")), _safe_int(end_queues.get("inspection_queue"))),
                _queue_metric("Inspection Output", _safe_int(start_queues.get("inspection_output")), _safe_int(end_queues.get("inspection_output"))),
            ],
            "machines": [],
            "roles": [],
        },
        {
            "id": "battery",
            "label": "Battery Station",
            "kpi": f"Discharged {_safe_int(daily_row.get('agent_discharged_count'))}",
            "metrics": [
                _queue_metric("Battery Deliveries", 0, _safe_int(daily_row.get("battery_delivery_count"))),
                _queue_metric("Discharged Workers", 0, _safe_int(daily_row.get("agent_discharged_count"))),
            ],
            "machines": [],
            "roles": [],
        },
    ]
    return {"regions": regions}


def _strategist_inputs(policy_row: dict[str, Any], start_snapshot: dict[str, Any]) -> list[str]:
    previous_review = policy_row.get("previous_day_review", {}) if isinstance(policy_row.get("previous_day_review", {}), dict) else {}
    queues = start_snapshot.get("queues", {}) if isinstance(start_snapshot.get("queues", {}), dict) else {}
    rows: list[str] = []
    risks = previous_review.get("carry_forward_risks", [])
    misses = previous_review.get("target_misses", [])
    failures = previous_review.get("top_failure_modes", [])
    if isinstance(risks, list):
        rows.extend(str(item) for item in risks[:2] if str(item).strip())
    if isinstance(misses, list) and misses:
        rows.append("Previous misses: " + ", ".join(str(item) for item in misses[:3]))
    if isinstance(failures, list) and failures:
        rows.append("Recent failure modes: " + ", ".join(str(item) for item in failures[:3]))
    rows.append(
        "Day start queues: "
        f"S1 material={_safe_int(queues.get('station1_material'))}, "
        f"S2 material={_safe_int(queues.get('station2_material'))}, "
        f"inspection={_safe_int(queues.get('inspection_queue'))}"
    )
    return rows[:5]


def _compiler_inputs(policy_row: dict[str, Any]) -> list[str]:
    prevention = policy_row.get("prevention_targets", [])
    support = policy_row.get("support_plan", {}) if isinstance(policy_row.get("support_plan", {}), dict) else {}
    roles = policy_row.get("worker_roles", {}) if isinstance(policy_row.get("worker_roles", {}), dict) else {}
    rows = [
        f"Operating focus: {policy_row.get('operating_focus', '-')}",
        "Worker roles requested: " + ", ".join(f"{worker}={role}" for worker, role in sorted(roles.items())),
    ]
    if isinstance(prevention, list) and prevention:
        rows.append("Prevention targets: " + ", ".join(str(item) for item in prevention[:3]))
    if support:
        rows.append("Support pair: " + str(support.get("primary_support_pair", "-")))
    return [row for row in rows if row.strip()][:5]


def _reviewer_inputs(daily_row: dict[str, Any], day_summary_row: dict[str, Any] | None) -> list[str]:
    rows = [
        f"Products today: {_safe_int(daily_row.get('products'))}",
        f"Inspection passes: {_safe_int(daily_row.get('inspection_passes'))}",
        f"Breakdowns: {_safe_int(daily_row.get('machine_breakdowns'))}",
        f"Close-out gap end: {_safe_int((day_summary_row or {}).get('open_closeout_gap', daily_row.get('inspection_backlog_end')))}",
    ]
    risks = (day_summary_row or {}).get("carry_forward_risks", [])
    if isinstance(risks, list) and risks:
        rows.extend(str(item) for item in risks[:2] if str(item).strip())
    return rows[:6]


def _compiler_structured(policy_row: dict[str, Any]) -> dict[str, Any]:
    return {
        "worker_roles": policy_row.get("worker_roles", {}),
        "task_priority_weights": policy_row.get("task_priority_weights", {}),
        "agent_priority_multipliers": policy_row.get("agent_priority_multipliers", {}),
        "mailbox_seed": policy_row.get("mailbox_seed", {}),
        "plan_revision": policy_row.get("plan_revision"),
    }


def _review_summary(parsed: dict[str, Any]) -> str:
    misses = parsed.get("target_misses", []) if isinstance(parsed.get("target_misses", []), list) else []
    failures = parsed.get("top_failure_modes", []) if isinstance(parsed.get("top_failure_modes", []), list) else []
    risks = parsed.get("carry_forward_risks", []) if isinstance(parsed.get("carry_forward_risks", []), list) else []
    parts: list[str] = []
    if misses:
        parts.append("Misses: " + ", ".join(str(item) for item in misses[:2]))
    if failures:
        parts.append("Failures: " + ", ".join(str(item) for item in failures[:2]))
    if risks:
        parts.append(str(risks[0]))
    return " | ".join(parts) or "No reviewer summary."


def _compiler_summary(policy_row: dict[str, Any]) -> str:
    roles = policy_row.get("worker_roles", {}) if isinstance(policy_row.get("worker_roles", {}), dict) else {}
    focus = str(policy_row.get("operating_focus", "")).strip() or "-"
    role_text = ", ".join(f"{worker}:{role}" for worker, role in sorted(roles.items()))
    return f"Compiled deterministic execution plan for focus={focus}. Roles={role_text or '-'}."


def _event_counts_for_factory_effect(day: int, events: list[dict[str, Any]], daily_row: dict[str, Any]) -> dict[str, Any]:
    event_counts = _day_event_counts(day, events)
    return {
        "breakdowns": event_counts["breakdowns"],
        "repairs": event_counts["repairs"],
        "discharges": event_counts["discharges"],
        "completed_products": _safe_int(daily_row.get("products")),
        "coordination_incidents": _safe_int(daily_row.get("coordination_incident_count")),
        "dispatches": _safe_int(daily_row.get("commitment_dispatch_task_count")),
        "local_responses": _safe_int(daily_row.get("local_response_task_count")),
    }


def _build_phase(
    *,
    phase_type: str,
    actor_id: str,
    actor_label: str,
    record: dict[str, Any] | None,
    structured: dict[str, Any],
    inputs_summary: list[str],
    decision_summary: str,
    excerpt_text: str,
    why_changed: list[str],
    carry_forward_risks: list[str],
    factory_effect: dict[str, Any],
    meta: dict[str, Any],
    phase_id: str,
) -> dict[str, Any]:
    started = _to_datetime(record.get("started_at_utc")) if isinstance(record, dict) else None
    latency_ms = _safe_float((record or {}).get("latency_ms")) if isinstance(record, dict) else 0.0
    ended = started + timedelta(milliseconds=latency_ms) if started is not None and latency_ms > 0 else started
    time_label = started.astimezone(timezone.utc).strftime("%H:%M:%S UTC") if started else meta.get("time_label", "")
    return {
        "id": phase_id,
        "phase_type": phase_type,
        "actor_id": actor_id,
        "actor_label": actor_label,
        "started_at": started.isoformat() if started else "",
        "ended_at": ended.isoformat() if ended else "",
        "time_label": time_label,
        "inputs_summary": inputs_summary,
        "decision_summary": decision_summary,
        "decision_structured": structured,
        "excerpt": excerpt_text,
        "why_changed": why_changed,
        "carry_forward_risks": carry_forward_risks,
        "factory_effect": factory_effect,
        "meta": meta,
    }


def _build_manager_payload(output_dir: Path) -> dict[str, Any] | None:
    run_meta = _load_json(output_dir / "run_meta.json")
    if str(run_meta.get("decision_mode", "")).strip() != "openclaw_adaptive_priority":
        return None

    llm_exchange = _load_json(output_dir / "llm_exchange.json")
    records = llm_exchange.get("records", []) if isinstance(llm_exchange.get("records", []), list) else []
    llm_records = [row for row in records if isinstance(row, dict)]
    shift_rows = _load_json_list(output_dir / "shift_policy_history.json")
    review_memory_rows = _load_json_list(output_dir / "day_review_memory.json")
    day_summary_memory_rows = _load_json_list(output_dir / "day_summary_memory.json")
    daily_payload = _load_json(output_dir / "daily_summary.json")
    daily_rows = daily_payload.get("days", []) if isinstance(daily_payload.get("days", []), list) else []
    daily_rows = [row for row in daily_rows if isinstance(row, dict)]
    minute_snapshots = _load_snapshots(output_dir / "minute_snapshots.json")
    events = _load_events(output_dir / "events.jsonl")
    kpi = _load_json(output_dir / "kpi.json")

    if not shift_rows or not llm_records or not daily_rows:
        return None

    strategist_by_day = {
        _safe_int(row.get("context", {}).get("day")): row
        for row in llm_records
        if str(row.get("call_name", "")).strip() == "manager_shift_strategist" and isinstance(row.get("context", {}), dict)
    }
    reviewer_by_day = {
        _safe_int(row.get("context", {}).get("day")): row
        for row in llm_records
        if str(row.get("call_name", "")).strip() == "manager_daily_reviewer" and isinstance(row.get("context", {}), dict)
    }
    review_memory_by_day = {
        _safe_int(row.get("day")): row
        for row in review_memory_rows
        if _safe_int(row.get("day")) > 0
    }
    day_summary_by_day = {
        _safe_int(row.get("day")): row
        for row in day_summary_memory_rows
        if _safe_int(row.get("day")) > 0
    }
    daily_by_day = {
        _safe_int(row.get("day")): row
        for row in daily_rows
        if _safe_int(row.get("day")) > 0
    }

    cumulative_products = 0
    for row in shift_rows:
        day = _safe_int(row.get("day"))
        cumulative_products += _safe_int((daily_by_day.get(day) or {}).get("products"))
        row["__cumulative_products"] = cumulative_products

    manager_sprite = _asset_data_uri(Path(__file__).resolve().parents[1] / "replay_studio" / "public" / "assets" / "worker_processed" / "Idle1.png")

    days: list[dict[str, Any]] = []
    snapshots_blob: dict[str, Any] = {}
    for index, shift_row in enumerate(shift_rows):
        day = _safe_int(shift_row.get("day"))
        if day <= 0:
            continue
        daily_row = daily_by_day.get(day, {})
        if not daily_row:
            continue
        previous_policy_row = shift_rows[index - 1] if index > 0 else None
        strategist_record = strategist_by_day.get(day)
        reviewer_record = reviewer_by_day.get(day)
        start_snapshot_raw, end_snapshot_raw = _nearest_day_snapshots(day, minute_snapshots)
        start_snapshot = _build_snapshot_blob(start_snapshot_raw)
        end_snapshot = _build_snapshot_blob(end_snapshot_raw)
        snapshots_blob[f"day_{day}"] = {"start": start_snapshot, "end": end_snapshot}
        day_summary_row = day_summary_by_day.get(day)
        reviewer_structured = dict(reviewer_record.get("parsed", {})) if isinstance(reviewer_record, dict) and isinstance(reviewer_record.get("parsed", {}), dict) else review_memory_by_day.get(day, {})
        strategist_structured = {
            "operating_focus": shift_row.get("operating_focus"),
            "role_plan": shift_row.get("role_plan", {}),
            "support_plan": shift_row.get("support_plan", {}),
            "prevention_targets": shift_row.get("prevention_targets", []),
            "daily_targets": shift_row.get("daily_targets", {}),
        }
        compiler_structured = _compiler_structured(shift_row)
        event_effect = _event_counts_for_factory_effect(day, events, daily_row)
        scene = _build_scene(
            day=day,
            daily_row=daily_row,
            policy_row=shift_row,
            previous_policy_row=previous_policy_row,
            start_snapshot=start_snapshot,
            end_snapshot=end_snapshot,
        )
        scene_summaries = [
            f"Products={_safe_int(daily_row.get('products'))}, inspection passes={_safe_int(daily_row.get('inspection_passes'))}.",
            f"Breakdowns={event_effect['breakdowns']}, repairs={event_effect['repairs']}, coordination incidents={event_effect['coordination_incidents']}.",
            f"Queues ended with S1 output={_safe_int(end_snapshot.get('queues', {}).get('station1_output'))}, S2 output={_safe_int(end_snapshot.get('queues', {}).get('station2_output'))}, inspection output={_safe_int(end_snapshot.get('queues', {}).get('inspection_output'))}.",
        ]
        strategist_phase = _build_phase(
            phase_type="strategist",
            actor_id="MANAGER_SHIFT_STRATEGIST",
            actor_label="Strategist",
            record=strategist_record,
            structured=strategist_structured,
            inputs_summary=_strategist_inputs(shift_row, start_snapshot),
            decision_summary=str(shift_row.get("summary", "")).strip() or "No strategist summary.",
            excerpt_text=_excerpt((strategist_record or {}).get("response_text", "")),
            why_changed=_strategist_inputs(shift_row, start_snapshot)[:3],
            carry_forward_risks=[
                str(item)
                for item in (
                    (shift_row.get("previous_day_review", {}) if isinstance(shift_row.get("previous_day_review", {}), dict) else {}).get("carry_forward_risks", [])
                    if isinstance((shift_row.get("previous_day_review", {}) if isinstance(shift_row.get("previous_day_review", {}), dict) else {}).get("carry_forward_risks", []), list)
                    else []
                )[:4]
            ],
            factory_effect={
                "summary_lines": scene_summaries,
                "highlights": _highlight_regions("strategist", strategist_structured),
                "incidents": event_effect,
            },
            meta={"latency_ms": _safe_float((strategist_record or {}).get("latency_ms"))},
            phase_id=f"day-{day}-strategist",
        )
        compiler_reasons: list[str] = []
        role_plan = shift_row.get("role_plan", {}) if isinstance(shift_row.get("role_plan", {}), dict) else {}
        for worker_id in sorted(role_plan.keys()):
            row = role_plan.get(worker_id, {})
            if isinstance(row, dict):
                reason = str(row.get("reason", "")).strip()
                if reason:
                    compiler_reasons.append(f"{worker_id}: {reason}")
        support_plan = shift_row.get("support_plan", {}) if isinstance(shift_row.get("support_plan", {}), dict) else {}
        support_reason = str(support_plan.get("reason", "")).strip()
        if support_reason:
            compiler_reasons.insert(0, support_reason)
        compiler_phase = _build_phase(
            phase_type="compiler",
            actor_id="DETERMINISTIC_COMPILER",
            actor_label="Compiler",
            record=None,
            structured=compiler_structured,
            inputs_summary=_compiler_inputs(shift_row),
            decision_summary=_compiler_summary(shift_row),
            excerpt_text=_excerpt(shift_row.get("summary", "")),
            why_changed=compiler_reasons[:4] or _compiler_inputs(shift_row)[:3],
            carry_forward_risks=strategist_phase["carry_forward_risks"],
            factory_effect={
                "summary_lines": scene_summaries,
                "highlights": _highlight_regions("compiler", compiler_structured),
                "incidents": event_effect,
            },
            meta={
                "plan_revision": shift_row.get("plan_revision"),
                "target_count": len(shift_row.get("prevention_targets", [])) if isinstance(shift_row.get("prevention_targets", []), list) else 0,
                "time_label": strategist_phase["time_label"],
            },
            phase_id=f"day-{day}-compiler",
        )
        reviewer_why = []
        if day_summary_row and isinstance(day_summary_row.get("policy_critique_hints", []), list):
            reviewer_why.extend(str(item) for item in day_summary_row.get("policy_critique_hints", [])[:3] if str(item).strip())
        if not reviewer_why:
            reviewer_why.extend(str(item) for item in (reviewer_structured.get("top_failure_modes", []) if isinstance(reviewer_structured.get("top_failure_modes", []), list) else [])[:3])
        reviewer_phase = _build_phase(
            phase_type="reviewer",
            actor_id="MANAGER_DAILY_REVIEWER",
            actor_label="Reviewer",
            record=reviewer_record,
            structured={
                "target_misses": reviewer_structured.get("target_misses", []),
                "top_failure_modes": reviewer_structured.get("top_failure_modes", []),
                "recommended_prevention_targets": reviewer_structured.get("recommended_prevention_targets", []),
                "recommended_support_pair": reviewer_structured.get("recommended_support_pair", ""),
                "carry_forward_risks": reviewer_structured.get("carry_forward_risks", []),
            },
            inputs_summary=_reviewer_inputs(daily_row, day_summary_row),
            decision_summary=_review_summary(reviewer_structured if isinstance(reviewer_structured, dict) else {}),
            excerpt_text=_excerpt((reviewer_record or {}).get("response_text", "")),
            why_changed=reviewer_why[:4],
            carry_forward_risks=[str(item) for item in (reviewer_structured.get("carry_forward_risks", []) if isinstance(reviewer_structured.get("carry_forward_risks", []), list) else [])[:5]],
            factory_effect={
                "summary_lines": scene_summaries
                + [
                    f"Open close-out gap={_safe_int((day_summary_row or {}).get('open_closeout_gap', daily_row.get('inspection_backlog_end')))}.",
                ],
                "highlights": _highlight_regions("reviewer", reviewer_structured if isinstance(reviewer_structured, dict) else {}),
                "incidents": event_effect,
            },
            meta={"latency_ms": _safe_float((reviewer_record or {}).get("latency_ms"))},
            phase_id=f"day-{day}-reviewer",
        )
        days.append(
            {
                "day": day,
                "phases": [strategist_phase, compiler_phase, reviewer_phase],
                "day_kpis": {
                    "products": _safe_int(daily_row.get("products")),
                    "inspection_passes": _safe_int(daily_row.get("inspection_passes")),
                    "machine_breakdowns": _safe_int(daily_row.get("machine_breakdowns")),
                    "coordination_incident_count": _safe_int(daily_row.get("coordination_incident_count")),
                    "plan_revision": _safe_int(daily_row.get("plan_revision")),
                },
                "day_summary": day_summary_row or {},
                "scene": scene,
            }
        )

    if not days:
        return None

    return {
        "meta": {
            "run_id": output_dir.name,
            "mode": str(run_meta.get("decision_mode", "")),
            "model": (((run_meta.get("llm", {}) if isinstance(run_meta.get("llm", {}), dict) else {}).get("model", "")) or ""),
            "total_days": _safe_int(run_meta.get("total_days"), len(days)),
            "minutes_per_day": _safe_float(run_meta.get("minutes_per_day")),
            "total_products": _safe_int(kpi.get("total_products")),
            "closure_ratio": _safe_float(kpi.get("downstream_closure_ratio")),
            "wall_clock_human": str(run_meta.get("wall_clock_human", "")).strip(),
        },
        "assets": {
            "manager_sprite": manager_sprite,
        },
        "days": days,
        "factory_snapshots": snapshots_blob,
    }


def export_manager_replay(*, output_dir: Path) -> Path | None:
    payload = _build_manager_payload(output_dir)
    if not isinstance(payload, dict):
        return None
    json_path = output_dir / "manager_replay.json"
    html_path = output_dir / "manager_replay_dashboard.html"
    payload_json = json.dumps(payload, ensure_ascii=False)
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    html_path.write_text(MANAGER_REPLAY_HTML.replace("__PAYLOAD_JSON__", payload_json), encoding="utf-8")
    return html_path
