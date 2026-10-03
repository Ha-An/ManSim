"""File-based evaluation progress, independent of simulation and final statistics."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import statistics
import threading
import time
from urllib.parse import quote

from manufacturing_sim.adp.live import atomic_json, atomic_text, read_json

SUCCESS = {"completed", "skipped_existing"}
TERMINAL = {"completed", "failed", "interrupted", "partial"}
LABELS = {
    "simulation_based_adp": "ADP", "random_feasible_dispatch": "Random Feasible",
    "immediate_shared": "Immediate Shared", "immediate_dedicated_roles": "Immediate Dedicated",
    "rolling_horizon_shared": "Rolling Shared", "rolling_horizon_dedicated_roles": "Rolling Dedicated",
}


def _duration(seconds) -> str:
    if seconds is None:
        return "--"
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s"


def _href(base: Path, target: Path) -> str:
    return quote(Path(os.path.relpath(target, base)).as_posix(), safe="/.:_")


def render_monitor(prepared: Path, snapshot: dict) -> str:
    esc = html.escape
    total, completed = snapshot["total"], snapshot["completed"]
    terminal = snapshot["phase"] in TERMINAL
    refresh = "" if terminal else '<meta http-equiv="refresh" content="5">'
    workers = snapshot["worker_counts"]
    headers = "".join(f"<th>Worker {n}</th>" for n in workers)
    matrix = []
    for mode in snapshot["policies"]:
        cells = []
        for n in workers:
            group = snapshot["groups"][f"{mode}:{n}"]
            detail = f"{group['running']} running / {group['failed']} failed"
            cells.append(f'<td><strong>{group["completed"]} / {group["total"]}</strong>'
                         f'<small>{detail}</small><progress value="{group["completed"]}" max="{group["total"]}"></progress></td>')
        matrix.append(f'<tr><th scope="row">{esc(LABELS.get(mode, mode))}</th>{"".join(cells)}</tr>')
    active_rows = []
    for run in snapshot["active_runs"]:
        progress = run["progress"]
        day = f"{progress.get('current_day', '-')} / {progress.get('total_days', snapshot['horizon_days'])}"
        pct = float(progress.get("progress_percent", 0) or 0)
        # A simulation at 100% is not accepted until artifact/KPI audits finish.
        stage = "Artifacts / audit" if pct >= 100 else str(progress.get("status", "starting"))
        log = _href(prepared, Path(run["run_dir"]) / "experiment_console.log")
        active_rows.append(f'<tr><td>{esc(LABELS.get(run["mode"], run["mode"]))}</td>'
                           f'<td>{esc(str(run["worker_count"]))}</td><td>{esc(str(run["seed"]))}</td>'
                           f'<td>{esc(day)}</td><td>{pct:.1f}%</td><td>{esc(stage)}</td>'
                           f'<td>{_duration(run["wall_sec"])}</td><td><a href="{log}" target="_blank">Log</a></td></tr>')
    failures = "".join(f'<li><strong>{esc(r["run_id"])}</strong>: {esc(r.get("reason", ""))}</li>'
                       for r in snapshot["failures"])
    historical = ""
    if snapshot.get("previous_dashboard"):
        historical = (f'<a href="{esc(snapshot["previous_dashboard"], quote=True)}" target="_blank">'
                      f'Previous {snapshot["previous_seed_count"]}-seed results</a>')
    final = ('<a class="result" href="policy_comparison_dashboard/comparison_dashboard.html" target="_blank">'
             'Open final policy comparison</a>') if snapshot["phase"] == "completed" else ""
    cards = [
        ("Audited complete", f"{completed:,} / {total:,}"),
        ("Running", f"{snapshot['running']} / {snapshot['parallel_jobs']}"),
        ("Pending", f"{snapshot['pending']:,}"), ("Failed", str(snapshot["failed"])),
        ("Reused runs", str(snapshot["reused"])), ("Newly completed", str(snapshot["new_completed"])),
        ("Session wall time", _duration(snapshot["session_wall_sec"])),
        ("Remaining estimate", _duration(snapshot["eta_sec"])),
    ]
    cards_html = "".join(f"<div><dt>{label}</dt><dd>{esc(value)}</dd></div>" for label, value in cards)
    message = esc(snapshot.get("message", ""))
    phase = esc(snapshot["phase"].replace("_", " "))
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>ManSim | Policy Comparison Progress</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#f4f7f9;color:#17252b;font:14px/1.5 system-ui,sans-serif;letter-spacing:0}}
header,main,footer{{max-width:1500px;margin:auto;padding:20px 24px}}header{{border-bottom:1px solid #b8c9ce}}
h1{{font-size:24px;margin:0 0 8px}}h2{{font-size:18px;margin:0 0 12px}}p{{margin:8px 0;color:#425961}}
nav{{display:flex;gap:20px;flex-wrap:wrap;margin-top:12px}}a{{color:#006b70}}.result{{font-weight:700}}
.phase{{font-size:15px;font-weight:700;color:#005f54}}.metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:0;margin:0}}
.metrics>div{{padding:14px 12px;border-bottom:1px solid #ccd8dd}}dt{{color:#4c626b}}dd{{font-size:23px;font-weight:650;margin:4px 0;overflow-wrap:anywhere}}
section{{padding:24px 0;border-bottom:1px solid #ccd8dd}}progress{{width:100%;height:10px;accent-color:#00806f}}.overall{{height:18px;margin-top:18px}}
.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;min-width:820px}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #ccd8dd;vertical-align:top}}
thead th{{background:#e3ecef}}tbody th{{font-size:13px;max-width:180px}}small{{display:block;color:#536a73;font-size:12px;margin:4px 0}}
.alert{{color:#9e2222}}.note{{font-size:13px}}.stamp{{font-size:12px;color:#536a73}}li{{overflow-wrap:anywhere}}
@media(max-width:650px){{header,main,footer{{padding:16px}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}h1{{font-size:21px}}dd{{font-size:20px}}}}
</style></head><body>
<header><h1>Policy Comparison Progress</h1>
{('<p>' + esc(snapshot['condition_label']) + '</p>') if snapshot.get('condition_label') else ''}
<p>mfg_flow_shop | Workers {workers[0]}-{workers[-1]} | {len(snapshot['policies'])} policies | {snapshot['seed_count']} seeds | {snapshot['horizon_days']} days</p>
<div class="phase">{phase.upper()}</div><p class="alert">{message}</p>
<nav>{final}{historical}<a href="evaluation_progress.json" target="_blank">Progress JSON</a></nav></header>
<main><dl class="metrics">{cards_html}</dl>
<progress class="overall" value="{completed}" max="{max(1,total)}"></progress>
<p>{100*completed/max(1,total):.1f}% audited complete. Final statistics include all seeds only after the full comparison passes verification.</p>
<section><h2>Completion by Policy and Fleet</h2><div class="scroll"><table><thead><tr><th>Policy</th>{headers}</tr></thead><tbody>{''.join(matrix)}</tbody></table></div></section>
<section><h2>Active Runs</h2><div class="scroll"><table><thead><tr><th>Policy</th><th>Workers</th><th>Seed</th><th>Day</th><th>Simulation</th><th>Stage</th><th>Wall time</th><th>Log</th></tr></thead>
<tbody>{''.join(active_rows) or '<tr><td colspan="8">No active simulations</td></tr>'}</tbody></table></div></section>
<section><h2>Errors</h2>{'<ul>'+failures+'</ul>' if failures else '<p>No recorded run failures.</p>'}</section>
<p class="note">Progress counts accept only completed runs with artifact and KPI audits passed. Reused runs do not count toward session throughput. ETA uses historical timings for the same policy and fleet; final aggregation time is excluded.</p>
<p class="stamp">Last update: <span id="stamp">{esc(snapshot['updated_at'])}</span> | Runner PID: {snapshot['pid']}</p>
<p id="heartbeat" class="alert"></p></main>
<script>
const updated = {json.dumps(snapshot['updated_at'])};
document.getElementById('stamp').textContent = new Date(updated).toLocaleString();
if ({str(not terminal).lower()} && Date.now() - Date.parse(updated) > 45000)
 document.getElementById('heartbeat').textContent = 'No recent heartbeat. The runner may have stopped; do not assume progress is continuing.';
</script></body></html>'''


class EvaluationMonitor:
    def __init__(self, prepared: Path, rows: list[dict], statuses: list[dict], jobs: int):
        self.prepared, self.rows, self.jobs = prepared, rows, jobs
        self.plan = read_json(prepared / "experiment_plan.json")
        self.statuses = {str(r["run_id"]): dict(r) for r in statuses}
        self.reused_ids = {key for key, r in self.statuses.items() if self._accepted(r)}
        self.active = {}
        self.phase, self.message = "preflight", ""
        self.started = time.monotonic()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.errors = []
        prior = read_json(prepared / "evaluation_timing.json")
        prior_work = sum(float(r.get("elapsed_sec", 0) or 0) for r in statuses if self._accepted(r))
        self.timing_factor = max(1.0, float(prior.get("run_phase_wall_sec", 0))
                                 * int(prior.get("parallel_jobs", jobs)) / prior_work) if prior_work else 1.0

    @staticmethod
    def _accepted(row: dict) -> bool:
        return row.get("status") in SUCCESS and row.get("artifact_audit") == "pass" and row.get("kpi_audit") == "pass"

    def start(self):
        self.persist()
        self.thread = threading.Thread(target=self._loop, name="evaluation-monitor", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop_event.wait(5):
            try:
                self.persist()
            except Exception as exc:
                self.errors.append(str(exc))

    def set_phase(self, phase: str, message: str = ""):
        with self.lock:
            self.phase, self.message = phase, message
        self.persist()

    def execute(self, function, row, *args):
        with self.lock:
            self.statuses.pop(row["run_id"], None)
            self.active[row["run_id"]] = (dict(row), time.monotonic())
        return function(row, *args)

    def finish_run(self, result):
        with self.lock:
            self.active.pop(result["run_id"], None)
            self.statuses[result["run_id"]] = dict(result)

    def snapshot(self) -> dict:
        with self.lock:
            statuses, active = dict(self.statuses), dict(self.active)
            phase, message = self.phase, self.message
        groups = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0, "running": 0})
        samples = defaultdict(list)
        successful, failures = [], []
        for row in self.rows:
            key = f"{row['mode']}:{row['worker_count']}"
            groups[key]["total"] += 1
            result = statuses.get(row["run_id"])
            if result:
                if self._accepted(result):
                    groups[key]["completed"] += 1
                    successful.append(row["run_id"])
                    elapsed = float(result.get("elapsed_sec", 0) or 0)
                    if elapsed > 0:
                        samples[key].append(elapsed)
                else:
                    groups[key]["failed"] += 1
                    failures.append(result)
            if row["run_id"] in active:
                groups[key]["running"] += 1
        active_runs = []
        for row, started in active.values():
            active_runs.append({**{k: row[k] for k in ("run_id", "mode", "worker_count", "seed", "run_dir")},
                                "wall_sec": time.monotonic() - started,
                                "progress": read_json(Path(row["run_dir"]) / "progress.json")})
        remaining_cost = 0.0
        known = True
        all_samples = [v for values in samples.values() for v in values]
        for key, group in groups.items():
            remaining = group["total"] - group["completed"] - group["failed"]
            values = samples.get(key) or all_samples
            if remaining and not values:
                known = False
            elif remaining:
                remaining_cost += remaining * statistics.mean(values)
        extension = self.plan.get("seed_extension", {})
        backup = Path(extension.get("backup_path", self.prepared)) / "policy_comparison_dashboard/comparison_dashboard.html"
        final_message = message or ("Monitor write warnings: " + self.errors[-1] if self.errors else "")
        return {
            "phase": phase, "message": final_message, "total": len(self.rows), "completed": len(successful),
            "failed": len(failures), "running": len(active),
            "pending": len(self.rows) - len(successful) - len(failures) - len(active),
            "reused": len(set(successful) & self.reused_ids),
            "new_completed": len(set(successful) - self.reused_ids),
            "session_wall_sec": time.monotonic() - self.started,
            "eta_sec": (remaining_cost * self.timing_factor / self.jobs if known else None) if phase == "running" else None,
            "parallel_jobs": self.jobs, "groups": dict(groups), "active_runs": active_runs,
            "failures": failures, "policies": self.plan["policies"], "worker_counts": self.plan["worker_counts"],
            "seed_count": len(self.plan["test_seeds"]), "horizon_days": self.plan["horizon_days"],
            "condition_label": self.plan.get("condition_label", ""),
            "previous_dashboard": _href(self.prepared, backup) if extension and backup.is_file() else "",
            "previous_seed_count": len(extension.get("old_seeds", [])),
            "updated_at": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
        }

    def persist(self):
        # Serialize writers so the heartbeat cannot overwrite a newer terminal state.
        with self.lock:
            snapshot = self.snapshot()
            atomic_json(self.prepared / "evaluation_progress.json", snapshot)
            atomic_text(self.prepared / "live_evaluation.html", render_monitor(self.prepared, snapshot))

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=15)
        self.persist()
