"""Detached training supervisor and file-based live monitor (no web server)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from typing import Any

from .live import atomic_json, atomic_text, read_json

REPO_ROOT = Path(__file__).resolve().parents[2]
TERMINAL = {"completed", "failed", "stopped"}
PHASES = {
    "starting": "시작 준비", "initial_random": "초기 Random rollout",
    "warm_start_replay": "Warm-start replay", "policy_iteration": "학습 rollout",
    "screening_validation": "Checkpoint validation", "final_selection_validation": "최종 validation",
    "target_build": "TD target 구성", "value_update": "가치망 업데이트",
    "diagnostics": "학습 진단", "checkpoint": "Checkpoint 저장", "completed": "완료",
}


def open_monitor(path: Path) -> bool:
    for candidate in (shutil.which("chrome.exe"), shutil.which("chrome"),
                      r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if candidate and Path(candidate).is_file():
            subprocess.Popen([str(candidate), path.resolve().as_uri()],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return bool(webbrowser.open(path.resolve().as_uri()))


def _tail(path: Path, limit: int = 16_384) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _link(base: Path, path: Path) -> str:
    try:
        from urllib.parse import quote
        return quote(Path(os.path.relpath(path, base)).as_posix(), safe="/:.")
    except ValueError:
        return path.resolve().as_uri()


def render_monitor(job_dir: Path, state: dict[str, Any], runs: list[dict[str, str]]) -> None:
    esc = lambda value: html.escape(str(value), quote=True)
    records = []
    for run in runs:
        output = Path(run["output"])
        progress = read_json(output / "training_progress.json")
        summary = read_json(output / "training_summary.json")
        records.append((run, progress, summary))
    active = next((row for row in records if row[1].get("status") == "running"), None)
    if active is None:
        active = next((row for row in reversed(records) if row[1] or row[2]), records[0] if records else None)
    run, progress, summary = active or ({"label": "-"}, {}, {})
    status = str(state.get("status", "starting"))
    status_label = {"starting": "시작 준비", "running": "실행 중", "completed": "완료",
                    "failed": "실패", "stopped": "중단됨", "stopping": "중단 처리 중"}.get(status, status)
    elapsed = float(state.get("elapsed_sec", 0))
    duration = f"{int(elapsed // 3600):02d}:{int(elapsed % 3600 // 60):02d}:{int(elapsed % 60):02d}"
    stage = PHASES.get(progress.get("phase", "starting"), progress.get("phase", "시작 준비"))
    completed = progress.get("completed_episode_count", 0)
    total = progress.get("expected_episode_count", 0)
    wave_done, wave_total = progress.get("wave_completed", 0), progress.get("wave_episode_count", 0)
    current_iteration = (summary.get("policy_iterations", progress.get("iteration", "-"))
                         if progress.get("status") == "completed" else progress.get("iteration", "-"))
    cards = [("현재 학습", run["label"]), ("현재 단계", stage),
             ("Iteration", f"{current_iteration} / {progress.get('policy_iterations', '-') }"),
             ("완료된 가치망 업데이트", progress.get("completed_update_count", summary.get("value_update_count", 0))),
             ("전체 episode (학습 + 평가)", f"{completed} / {total or '-'}"),
             ("현재 wave 완료 episode", f"{wave_done} / {wave_total or '-'}"),
             ("Wave", progress.get("wave_id", "-")),
             ("Wall-clock 경과", duration),
             ("CPU rollout process", progress.get("process_count", "-")),
             ("가치망 학습 장치", progress.get("training_device", summary.get("training_device", "-"))),
             ("마지막 학습 진행 갱신", progress.get("updated_at", "-"))]
    cards_html = "".join(f"<div class='metric'><dt>{esc(k)}</dt><dd>{esc(v)}</dd></div>" for k, v in cards)
    table = []
    dashboard_html = ""
    for item, pr, su in records:
        path = Path(item["output"]) / "training_dashboard.html"
        link = f"<a href='{esc(_link(job_dir, path))}' target='_blank'>학습 그래프</a>" if path.exists() else "-"
        displayed_iteration = (su.get("policy_iterations", pr.get("iteration", "-"))
                               if pr.get("status") == "completed" else pr.get("iteration", "-"))
        table.append(f"<tr><td>{esc(item['label'])}</td><td>{esc(pr.get('status', su.get('status', 'pending')))}</td>"
                     f"<td>{esc(displayed_iteration)}</td><td>{link}</td></tr>")
        if item == run and path.exists():
            dashboard_html = (f"<section><h2>학습 곡선</h2><iframe title='현재 학습 대시보드' "
                              f"src='{esc(_link(job_dir, path))}?embedded=1'></iframe></section>")
    log = _tail(job_dir / "console.log")
    if run.get("output"):
        child_log = Path(run["output"]) / "training_console.log"
        if child_log.exists():
            log += "\n" + _tail(child_log)
    reason = state.get("error", "")
    if status == "failed" and not reason:
        reason = f"프로세스 종료 코드: {state.get('return_code')}"
    notice = f"<p role='alert'>{esc(reason)}</p>" if reason else ""
    # Reload local HTML, not fetch(file://); this also works without a server/CORS permissions.
    refresh = '<meta http-equiv="refresh" content="5">' if status not in TERMINAL else ""
    heartbeat = float(state.get("heartbeat_epoch", time.time()))
    page = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>ADP Live Training</title>
<style>body{{margin:0;background:#f5f7f9;color:#182329;font:14px/1.5 system-ui,sans-serif;letter-spacing:0}}
main{{max-width:1500px;margin:auto;padding:20px}}header{{display:flex;gap:20px;align-items:baseline;flex-wrap:wrap}}
h1{{font-size:24px;margin:0}}h2{{font-size:18px;margin:20px 0 10px}}.status{{color:#087759;font-weight:700}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));margin:16px 0;border-top:1px solid #ccd4dc}}
.metric{{min-width:0;padding:12px 14px 12px 0;border-bottom:1px solid #ccd4dc}}dt{{color:#52616d}}dd{{margin:4px 0 0;overflow-wrap:anywhere;font-weight:600}}
progress{{width:100%;height:12px;accent-color:#168873}}table{{width:100%;table-layout:fixed;border-collapse:collapse}}td,th{{text-align:left;padding:8px;border-bottom:1px solid #ccd4dc;overflow-wrap:anywhere}}
a{{color:#086aba}}iframe{{width:100%;height:1000px;border:0;background:white}}pre{{background:#172229;color:#e5edf1;padding:12px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto}}
[role=alert]{{color:#af302e}}.muted{{color:#586773}}@media(max-width:500px){{main{{padding:12px}}.metrics{{grid-template-columns:1fr 1fr}}td,th{{padding:5px}}}}
</style></head><body><main><header><h1>ADP 실시간 학습</h1><span id="status" class="status">{esc(status_label)}</span></header>
{notice}<p class="muted">PID {esc(state.get('child_pid', '-'))} · 모니터 갱신 {esc(state.get('updated_at', '-'))}</p>
<dl class="metrics">{cards_html}</dl><progress max="{max(1, int(total or 0))}" value="{int(completed)}"></progress>
<section><h2>학습 실행 목록</h2><table><thead><tr><th>학습</th><th>상태</th><th>Iteration</th><th>결과</th></tr></thead><tbody>{''.join(table)}</tbody></table></section>
{dashboard_html}<section><h2>최근 로그</h2><pre>{esc(log) or '아직 기록된 로그가 없습니다.'}</pre></section></main>
<script>if({json.dumps(status not in TERMINAL)} && Date.now()/1000-{heartbeat}>30){{
document.getElementById('status').textContent='모니터 응답 없음: 프로세스 상태 확인 필요';}}
try{{window.scrollTo(0,Number(sessionStorage.getItem(location.pathname)||0));
window.addEventListener('beforeunload',()=>sessionStorage.setItem(location.pathname,String(window.scrollY)));}}catch(e){{}}</script>
</body></html>"""
    atomic_text(job_dir / "live_training.html", page)


def launch(*, command: list[str], job_dir: Path, runs: list[dict[str, str]],
           cwd: Path = REPO_ROOT, open_browser: bool = True) -> Path:
    job_dir = job_dir.resolve()
    job_dir.mkdir(parents=True, exist_ok=True)
    if not runs:
        raise ValueError("At least one training output is required.")
    # Never overwrite a previous job's command, logs or PID records.
    with (job_dir / "job.json").open("x", encoding="utf-8") as stream:
        json.dump({"command": command, "cwd": str(cwd.resolve()), "runs": runs}, stream, indent=2)
    claims: list[Path] = []
    try:
        for run in runs:
            output = Path(run["output"]).resolve()
            claim = output.with_name(f".{output.name}.adp_background.lock")
            claim.parent.mkdir(parents=True, exist_ok=True)
            if claim.exists():
                owner = read_json(claim).get("job_dir", "")
                if owner and read_json(Path(owner) / "status.json").get("status") in TERMINAL:
                    claim.unlink()
            with claim.open("x", encoding="utf-8") as stream:
                json.dump({"job_dir": str(job_dir)}, stream)
            claims.append(claim)
    except BaseException:
        for claim in claims:
            claim.unlink(missing_ok=True)
        atomic_json(job_dir / "status.json", {"status": "failed", "error": "Output is locked by another background job."})
        raise
    manifest = read_json(job_dir / "job.json")
    manifest["claims"] = [str(path) for path in claims]
    atomic_json(job_dir / "job.json", manifest)
    state = {"status": "starting", "heartbeat_epoch": time.time()}
    atomic_json(job_dir / "status.json", state)
    render_monitor(job_dir, state, runs)
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
    else:
        options["start_new_session"] = True
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    try:
        with (job_dir / "supervisor.log").open("ab") as log:
            supervisor = subprocess.Popen([sys.executable, "-u", "-m", "manufacturing_sim.adp.background",
                                           "_supervise", "--job", str(job_dir)], cwd=cwd,
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                          close_fds=True, env=env, **options)
        # Reap the handle if the launcher remains alive; never wait on application exit.
        threading.Thread(target=supervisor.wait, daemon=True).start()
    except OSError as exc:
        state.update(status="failed", error=str(exc))
        atomic_json(job_dir / "status.json", state)
        render_monitor(job_dir, state, runs)
        for claim in claims:
            claim.unlink(missing_ok=True)
        raise
    monitor = job_dir / "live_training.html"
    if open_browser:
        open_monitor(monitor)
    return monitor


def _stop_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=15)


def supervise(job_dir: Path) -> int:
    manifest = read_json(job_dir / "job.json")
    runs = manifest["runs"]
    state: dict[str, Any] = {"status": "starting", "supervisor_pid": os.getpid()}
    started = time.monotonic()
    child = None

    def persist() -> None:
        state.update(elapsed_sec=time.monotonic() - started, heartbeat_epoch=time.time(),
                     updated_at=datetime.now(timezone.utc).isoformat())
        atomic_json(job_dir / "status.json", state)
        render_monitor(job_dir, state, runs)

    try:
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with (job_dir / "console.log").open("ab") as log:
            child = subprocess.Popen(manifest["command"], cwd=manifest["cwd"],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                     env={**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}, **options)
            state.update(status="running", child_pid=child.pid)
            while child.poll() is None:
                if (job_dir / "stop.request").exists():
                    state["status"] = "stopping"
                    persist()
                    _stop_tree(child)
                    state["status"] = "stopped"
                    break
                persist()
                time.sleep(2)
            state["return_code"] = child.wait()
            if state["status"] != "stopped":
                state["status"] = "completed" if child.returncode == 0 else "failed"
    except BaseException as exc:
        if child is not None:
            _stop_tree(child)
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        if state["status"] in {"failed", "stopped"}:
            from .live import TrainingProgress
            for run in runs:
                progress = read_json(Path(run["output"]) / "training_progress.json")
                if progress.get("status") == "running":
                    TrainingProgress(Path(run["output"])).update(status=state["status"])
        persist()
        for path in manifest.get("claims", []):
            claim = Path(path)
            if read_json(claim).get("job_dir") == str(job_dir):
                claim.unlink(missing_ok=True)
    return 0 if state["status"] == "completed" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or stop a detached ADP training job.")
    parser.add_argument("action", choices=["status", "stop", "open", "_supervise"])
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args()
    directory = args.job.resolve()
    if not (directory / "job.json").is_file():
        parser.error(f"Unknown background job: {directory}")
    if args.action == "_supervise":
        raise SystemExit(supervise(directory))
    if args.action == "stop":
        state = read_json(directory / "status.json")
        if state.get("status") not in TERMINAL:
            atomic_text(directory / "stop.request", datetime.now(timezone.utc).isoformat())
        print("Stop requested; completed checkpoints are retained. This is not a resumable pause.")
    elif args.action == "open":
        open_monitor(directory / "live_training.html")
    else:
        state = read_json(directory / "status.json")
        state["heartbeat_stale"] = (state.get("status") not in TERMINAL
                                     and time.time() - state.get("heartbeat_epoch", 0) > 30)
        print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
