from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from manufacturing_sim.adp.background import launch, render_monitor, supervise
from manufacturing_sim.adp.live import TrainingProgress, atomic_json, read_json


def _launch_tracked(**kwargs):
    original = subprocess.Popen
    children = []

    def spawn(*args, **options):
        process = original(*args, **options)
        children.append(process)
        return process

    with patch("manufacturing_sim.adp.background.subprocess.Popen", side_effect=spawn):
        monitor = launch(**kwargs)
    return monitor, children[0]


class LiveProgressTests(unittest.TestCase):
    def test_atomic_progress_preserves_run_contract_and_updates_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            progress = TrainingProgress(output)
            progress.update(status="running", expected_episode_count=12, training_device="cuda:0")
            progress.rollout({"phase": "policy_iteration", "iteration": 1,
                              "wave_completed": 2, "completed_episode_count": 7})
            row = read_json(progress.path)
            self.assertEqual(row["expected_episode_count"], 12)
            self.assertEqual(row["completed_episode_count"], 7)
            self.assertEqual(row["training_device"], "cuda:0")
            self.assertEqual(row["pid"], os.getpid())
            self.assertFalse(list(output.glob("*.tmp")))

    def test_monitor_refresh_failure_escape_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "training run"
            output.mkdir()
            (output / "training_dashboard.html").write_text("<h1>curve</h1>", encoding="utf-8")
            job = output / "background_job"
            job.mkdir()
            runs = [{"label": "Worker 3", "output": str(output)}]
            TrainingProgress(output).update(status="running", phase="value_update", iteration=2,
                expected_episode_count=20, completed_episode_count=10, training_device="cuda:0")
            render_monitor(job, {"status": "running", "heartbeat_epoch": time.time()}, runs)
            page = (job / "live_training.html").read_text(encoding="utf-8")
            self.assertIn('http-equiv="refresh" content="5"', page)
            self.assertIn("../training_dashboard.html", page)
            self.assertIn("가치망 업데이트", page)
            self.assertIn("10 / 20", page)
            self.assertIn('value="10"', page)
            render_monitor(job, {"status": "failed", "error": "<script>bad</script>"}, runs)
            page = (job / "live_training.html").read_text(encoding="utf-8")
            self.assertNotIn('http-equiv="refresh"', page)
            self.assertIn("&lt;script&gt;bad&lt;/script&gt;", page)

    def test_launch_is_detached_no_console_and_prevents_same_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = [{"label": "test", "output": str(root / "output")}]
            with patch("manufacturing_sim.adp.background.subprocess.Popen") as popen:
                launch(command=[sys.executable, "-c", "pass"], job_dir=root / "first",
                       runs=runs, open_browser=False)
                kwargs = popen.call_args.kwargs
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertTrue(kwargs["close_fds"])
                if os.name == "nt":
                    self.assertTrue(kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
                    self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)
                else:
                    self.assertTrue(kwargs["start_new_session"])
                with self.assertRaises(FileExistsError):
                    launch(command=[sys.executable, "-c", "pass"], job_dir=root / "second",
                           runs=runs, open_browser=False)
                self.assertEqual(popen.call_count, 1)


class BackgroundProcessTests(unittest.TestCase):
    def test_failure_return_code_and_log_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            atomic_json(job / "job.json", {
                "command": [sys.executable, "-u", "-c", "print('failure fixture'); raise SystemExit(7)"],
                "cwd": str(job), "runs": [{"label": "failure", "output": str(job / "run")}],
            })
            self.assertEqual(supervise(job), 1)
            status = read_json(job / "status.json")
            self.assertEqual(status["return_code"], 7)
            self.assertEqual(status["status"], "failed")
            self.assertIn("failure fixture", (job / "console.log").read_text())

    def test_real_detached_job_finishes_and_releases_output_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / "job"
            monitor, supervisor = _launch_tracked(command=[sys.executable, "-u", "-c", "print('detached complete')"],
                             job_dir=job, runs=[{"label": "test", "output": str(root / "output")}],
                             open_browser=False)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                status = read_json(job / "status.json")
                if status.get("status") in {"completed", "failed"} and not (root / ".output.adp_background.lock").exists():
                    break
                time.sleep(.1)
            self.assertEqual(status.get("status"), "completed", status)
            self.assertTrue(monitor.is_file())
            self.assertFalse((root / ".output.adp_background.lock").exists())
            supervisor.wait(timeout=30)

    def test_stop_terminates_child_tree_without_deleting_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = root / "job"
            output = root / "run"
            output.mkdir()
            checkpoint = output / "last.pt"
            checkpoint.write_bytes(b"preserve")
            # A grandchild writes repeatedly; after stop, writes must cease as well.
            ticker = root / "ticks.txt"
            child_code = f"import time; f=open({str(ticker)!r}, 'a', buffering=1); "
            child_code += "exec(\"while True:\\n f.write('tick\\\\n'); time.sleep(.1)\")"
            code = (f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-u','-c',{child_code!r}]); "
                    "time.sleep(60)")
            _, supervisor = _launch_tracked(command=[sys.executable, "-u", "-c", code], job_dir=job,
                   runs=[{"label": "test", "output": str(output)}], open_browser=False)
            deadline = time.monotonic() + 30
            while not ticker.exists() and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertTrue(ticker.exists(), (job / "console.log").read_text())
            (job / "stop.request").write_text("stop")
            while time.monotonic() < deadline:
                status = read_json(job / "status.json")
                if status.get("status") == "stopped" and not (root / ".run.adp_background.lock").exists():
                    break
                time.sleep(.1)
            self.assertEqual(status.get("status"), "stopped")
            size = ticker.stat().st_size
            time.sleep(.3)
            self.assertEqual(ticker.stat().st_size, size)
            self.assertEqual(checkpoint.read_bytes(), b"preserve")
            supervisor.wait(timeout=30)


if __name__ == "__main__":
    unittest.main()
