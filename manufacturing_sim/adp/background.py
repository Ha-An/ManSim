"""Detached training supervisor and file-based live monitor (no web server)."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import html
import json
import math
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


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _performance_cards(output: Path, progress: dict[str, Any],
                       summary: dict[str, Any]) -> list[tuple[str, str, str]]:
    try:
        with (output / "iteration_metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except (OSError, csv.Error, UnicodeError):
        rows = []
    # Only published, completed screening evaluations may compete for best.
    evaluated = [row for row in rows if _number(row.get("iteration")) is not None
                 and _number(row.get("validation_products_mean")) is not None]
    evaluated.sort(key=lambda row: float(row["iteration"]))
    ranked = sorted(evaluated, key=lambda row: (
        -float(row["validation_products_mean"]),
        _number(row.get("validation_products_std")) if _number(row.get("validation_products_std")) is not None else math.inf,
        float(row["iteration"])))
    latest = evaluated[-1] if evaluated else {}
    screening_best = ranked[0] if ranked else {}
    final = summary.get("status") == "completed"
    best_iteration = _number(summary.get("best_iteration") if final else screening_best.get("iteration"))
    best_mean = _number(summary.get("best_validation_completed_products_avg") if final
                        else screening_best.get("validation_products_mean"))
    best_std = _number(summary.get("best_validation_completed_products_std") if final
                       else screening_best.get("validation_products_std"))
    partition = "final_selection_validation" if final else "screening_validation"
    workers = summary.get("worker_counts", [])
    seeds = summary.get("seed_partitions", {}).get(partition, [])
    sample_count = (len(seeds) * len(workers)) if seeds and workers else None
    if not final:
        sample_count = _number(screening_best.get("validation_episode_count")) or sample_count
    evaluation_kind = "최종 선정 validation" if final else "Screening validation · 잠정 best"
    spread = f"표준편차 {best_std:.2f}개" if best_std is not None else "표준편차 미기록"
    samples = f"평가 {int(sample_count)} episodes" if sample_count is not None else "평가 표본 수 미기록"
    cards = [
        ("Best iteration (최종)" if final else "Best iteration (잠정)",
         str(int(best_iteration)) if best_iteration is not None and best_iteration >= 0 else "평가 전", evaluation_kind),
        ("Best validation 평균 생산량", f"{best_mean:.2f}개" if best_mean is not None else "평가 전",
         f"{spread} · {samples} · greedy 평가" if best_mean is not None else "완료된 평가만 반영"),
    ]
    latest_mean = _number(latest.get("validation_products_mean"))
    if latest_mean is None:
        cards.append(("최근 screening 생산량", "평가 전", "최종 선정 validation과 별도"))
    else:
        delta = latest_mean - float(screening_best["validation_products_mean"])
        cards.append(("최근 screening 생산량", f"{latest_mean:.2f}개 (I{int(float(latest['iteration']))})",
                      f"Screening best 대비 {delta:+.2f}개 · 동일 seed 집합"))

    stopped = bool(progress.get("stop_reason") or summary.get("stop_reason"))
    terminal = progress.get("status") in TERMINAL or summary.get("status") in TERMINAL
    scheduled = [_number(value) for value in summary.get("screening_iterations", [])]
    completed_indices = {int(float(row["iteration"])) for row in evaluated}
    current = _number(progress.get("iteration")) or 0
    upcoming = sorted(int(value) for value in scheduled if value is not None
                      and value >= current and int(value) not in completed_indices)
    if final or terminal:
        next_value = "종료"
    elif stopped or progress.get("phase") == "final_selection_validation":
        next_value = "최종 선정 평가"
    else:
        next_value = f"Iteration {upcoming[0]}" if upcoming else "일정 미기록"
    cards.append(("다음 validation", next_value, "Checkpoint 업데이트 후 평가"))

    early = summary.get("early_stopping", {})
    history = summary.get("early_stopping_history", [])
    if not early.get("enabled"):
        cards.append(("조기 종료 점검", "비활성" if early else "미기록", "생산량 기준"))
    elif history:
        check = history[-1]
        stagnant = check.get("early_stop_stagnant_iterations", "-")
        checks = int(early.get("consecutive_checks", 1))
        consecutive = 0
        for row in reversed(history):
            if not row.get("early_stop_low_gain"):
                break
            consecutive += 1
        detail = (f"최근 I{check['iteration']} 평가 기준 · 최소 I{early.get('min_iterations', '-')}"
                  f" · 낮은 개선 연속 {min(consecutive, checks)}/{checks}회")
        value = ("종료 조건 충족" if check.get("early_stop_triggered")
                 else f"평균 개선 없음 {stagnant}/{early.get('patience_iterations', '-')} iterations")
        cards.append(("조기 종료 점검", value, detail))
    else:
        cards.append(("조기 종료 점검", "평가 전", f"최소 iteration {early.get('min_iterations', '-')} 이후 판단"))
    return cards


def render_monitor(job_dir: Path, state: dict[str, Any], runs: list[dict[str, str]],
                   *, filename: str = "live_training.html", dashboard_filename: str = "training_dashboard.html") -> None:
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
    training_finished = (progress.get("status") == "completed" or progress.get("stop_reason")
                         or summary.get("stop_reason"))
    current_iteration = (summary.get("completed_policy_iterations", summary.get("policy_iterations", progress.get("iteration", "-")))
                         if training_finished else progress.get("iteration", "-"))
    cards = [("현재 학습", run["label"]), ("현재 단계", stage),
             ("Iteration", f"{current_iteration} / {progress.get('policy_iterations', '-') }"),
             ("완료된 가치망 업데이트", progress.get("completed_update_count", summary.get("value_update_count", 0))),
             ("전체 episode (학습 + 평가)", f"{completed} / {total or '-'}"),
             ("현재 wave 완료 episode", f"{wave_done} / {wave_total or '-'}"),
             ("Wave", progress.get("wave_id", "-")),
             ("Wall-clock 경과", duration),
             ("학습 종료 사유", progress.get("stop_reason") or summary.get("stop_reason") or "-"),
             ("CPU rollout process", progress.get("process_count", "-")),
             ("가치망 학습 장치", progress.get("training_device", summary.get("training_device", "-"))),
             ("마지막 학습 진행 갱신", progress.get("updated_at", "-"))]
    detailed_cards = [(label, value, "") for label, value in cards]
    if run.get("output"):
        detailed_cards[2:2] = _performance_cards(Path(run["output"]), progress, summary)
    workers = summary.get("worker_counts", [])
    horizon = summary.get("horizon_days")
    detailed_cards[0] = ("현재 학습", run["label"],
                         f"Worker {', '.join(map(str, workers)) or '-'} · {horizon if horizon is not None else '-'}일 episode")
    detailed_cards = [(label, value,
                       f"완료 wave 기준: 학습 {summary.get('training_episode_count', '-')} / 평가 {summary.get('validation_episode_count', '-')}"
                       if label == "전체 episode (학습 + 평가)" else detail)
                      for label, value, detail in detailed_cards]
    cards_html = "".join(f"<div class='metric'><dt>{esc(k)}</dt><dd>{esc(v)}</dd>"
                         f"<div class='metric-detail'>{esc(detail)}</div></div>" for k, v, detail in detailed_cards)
    table = []
    dashboard_html = ""
    for item, pr, su in records:
        path = Path(item["output"]) / dashboard_filename
        if not path.exists():
            path = Path(item["output"]) / "training_dashboard.html"
        link = f"<a href='{esc(_link(job_dir, path))}' target='_blank'>학습 그래프</a>" if path.exists() else "-"
        displayed_iteration = (su.get("completed_policy_iterations", su.get("policy_iterations", pr.get("iteration", "-")))
                               if pr.get("status") == "completed" or pr.get("stop_reason") or su.get("stop_reason")
                               else pr.get("iteration", "-"))
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
.metric-detail{{color:#586773;font-size:12px;margin-top:4px;overflow-wrap:anywhere}}
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
    atomic_text(job_dir / filename, page)


def watch_monitor(job_dir: Path, *, open_browser: bool = True) -> Path:
    """Refresh the display of an older supervisor without restarting training."""
    runs = read_json(job_dir / "job.json")["runs"]
    path = job_dir / "live_training_latest.html"
    rendered: dict[str, tuple[Any, ...]] = {}
    dashboard_filename = "training_dashboard_latest.html"
    while True:
        state = read_json(job_dir / "status.json")
        # An already running trainer keeps its old renderer in memory. Publish a
        # separate view only when source artifacts change; never race its HTML.
        for run in runs:
            output = Path(run["output"])
            if read_json(output / "training_summary.json").get("return_estimator") != "n_step_td":
                continue
            sources = [output / name for name in ("episode_metrics.csv", "iteration_metrics.csv",
                                                   "wave_metrics.csv", "training_summary.json", "result_validity.json")]
            try:
                stamp = tuple((file.stat().st_mtime_ns, file.stat().st_size) if file.exists() else None for file in sources)
                if stamp != rendered.get(run["output"]):
                    from .td_dashboard import render_from_files
                    render_from_files(output, filename=dashboard_filename)
                    rendered[run["output"]] = stamp
            except (OSError, ValueError, csv.Error) as exc:
                print(f"Dashboard refresh deferred: {exc}", flush=True)
        render_monitor(job_dir, state, runs, filename=path.name, dashboard_filename=dashboard_filename)
        if open_browser:
            open_monitor(path)
            open_browser = False
        if state.get("status") in TERMINAL or time.time() - state.get("heartbeat_epoch", 0) > 30:
            return path
        time.sleep(5)


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
        try:
            persist()
        finally:
            # A final monitor write failure must not permanently lock the output.
            for path in manifest.get("claims", []):
                claim = Path(path)
                if read_json(claim).get("job_dir") == str(job_dir):
                    claim.unlink(missing_ok=True)
    return 0 if state["status"] == "completed" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or stop a detached ADP training job.")
    parser.add_argument("action", choices=["status", "stop", "open", "watch", "_supervise"])
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args()
    directory = args.job.resolve()
    if not (directory / "job.json").is_file():
        parser.error(f"Unknown background job: {directory}")
    if args.action == "_supervise":
        raise SystemExit(supervise(directory))
    if args.action == "watch":
        watch_monitor(directory)
        return
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
