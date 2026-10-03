from __future__ import annotations

import json
import csv
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from manufacturing_sim.adp.background import _performance_cards, launch, render_monitor, supervise, watch_monitor
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

    def test_early_completion_shows_actual_iteration_not_budget_or_best(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            TrainingProgress(root).update(status="completed", phase="completed", policy_iterations=75, iteration=45)
            atomic_json(root / "training_summary.json", {"policy_iterations": 75,
                "completed_policy_iterations": 45, "best_iteration": 30, "stop_reason": "production_plateau"})
            render_monitor(root, {"status": "completed"}, [{"label": "test", "output": str(root)}])
            page = (root / "live_training.html").read_text(encoding="utf-8")
            self.assertIn("45 / 75", page)
            self.assertIn("production_plateau", page)

    def test_wave_failure_is_not_reported_as_running(self):
        with tempfile.TemporaryDirectory() as directory:
            progress = TrainingProgress(Path(directory))
            progress.rollout({"event": "wave_failed", "error": "child failed"})
            self.assertEqual(read_json(progress.path)["status"], "failed")

    def test_failed_td_rollout_preserves_contract_and_previous_curves(self):
        from manufacturing_sim.adp.train import _record_rollout_failure, _write_csv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / "training_summary.json", {
                "return_estimator": "n_step_td", "training_device": "cuda:0", "n_step": 30})
            _write_csv(root / "iteration_metrics.csv", [{"iteration": 0, "td_train_mse": 0.125}])
            episodes = [{"phase": phase, "iteration": 0, "products": 5} for phase in
                        ("initial_random", "screening_validation", "final_selection_validation")]
            _record_rollout_failure(root, episodes, [{"status": "failed", "cancelled_episode_count": 2}], "test failure")
            summary = read_json(root / "training_summary.json")
            self.assertEqual(summary["return_estimator"], "n_step_td")
            self.assertEqual(summary["training_device"], "cuda:0")
            self.assertEqual(summary["training_episode_count"], 1)
            self.assertEqual(summary["validation_episode_count"], 2)
            self.assertEqual(summary["status"], "failed")
            page = (root / "training_dashboard.html").read_text(encoding="utf-8")
            self.assertIn("n-step TD", page)
            self.assertIn("0.125", page)
            self.assertIn("test failure", page)

    def test_live_csv_replacement_is_atomic_and_preserves_utf8(self):
        import csv
        from manufacturing_sim.adp.train import _write_csv
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics.csv"
            _write_csv(path, [{"value": "previous"}])
            original = os.replace
            old = path.read_bytes()
            seen = []

            def replace(source, destination):
                seen.append(path.read_bytes())
                return original(source, destination)

            with patch("manufacturing_sim.adp.live.os.replace", side_effect=replace):
                _write_csv(path, [{"value": "한글, quoted\nline"}])
            self.assertEqual(seen, [old])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream))[0]["value"], "한글, quoted\nline")

    def test_failure_keeps_legacy_mc_policy_phase_in_training_count(self):
        from manufacturing_sim.adp.train import _record_rollout_failure, _write_csv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_csv(root / "iteration_metrics.csv", [{"iteration": 1, "mc_loss": .5}])
            episodes = [{"phase": "policy_iteration_1", "iteration": 1},
                        {"phase": "screening_validation", "iteration": 1}]
            with patch("manufacturing_sim.adp.train.render_training_dashboard") as render:
                _record_rollout_failure(root, episodes, [], "fixture")
            self.assertEqual(read_json(root / "training_summary.json")["training_episode_count"], 1)
            self.assertEqual(render.call_args.args[2][0]["iteration"], "1")

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


class MonitorPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.summary = {
            "status": "running", "worker_counts": [3], "horizon_days": 5,
            "screening_iterations": [0, 5, 10, 15],
            "seed_partitions": {"screening_validation": list(range(10)),
                                "final_selection_validation": list(range(20))},
        }

    def write_rows(self, rows):
        with (self.output / "iteration_metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=sorted({key for row in rows for key in row}))
            writer.writeheader()
            writer.writerows(rows)

    def cards(self, progress=None):
        return {label: (value, detail) for label, value, detail in
                _performance_cards(self.output, progress or {}, self.summary)}

    def test_before_validation_is_missing_not_zero(self):
        self.write_rows([{"iteration": 0, "validation_products_mean": ""}])
        cards = self.cards({"iteration": 0})
        self.assertEqual(cards["Best iteration (잠정)"][0], "평가 전")
        self.assertEqual(cards["Best validation 평균 생산량"][0], "평가 전")
        self.assertEqual(cards["다음 validation"][0], "Iteration 0")

    def test_zero_products_and_iteration_zero_are_valid(self):
        self.write_rows([{"iteration": 0, "validation_products_mean": 0,
                          "validation_products_std": 0, "validation_episode_count": 10}])
        cards = self.cards()
        self.assertEqual(cards["Best iteration (잠정)"][0], "0")
        self.assertEqual(cards["Best validation 평균 생산량"][0], "0.00개")
        self.assertIn("10 episodes", cards["Best validation 평균 생산량"][1])

    def test_best_latest_and_partial_evaluation_are_distinct(self):
        self.write_rows([
            {"iteration": 0, "validation_products_mean": 40, "validation_products_std": 3},
            {"iteration": 5, "validation_products_mean": 39, "validation_products_std": 2},
            {"iteration": 6, "validation_products_mean": "", "rollout_products_mean": 70},
            {"iteration": 10, "validation_products_mean": "nan"},
        ])
        # Published CSV has a newer validation than this deliberately stale summary.
        self.summary.update(best_iteration=99, best_screening_mean=80)
        cards = self.cards({"iteration": 10, "phase": "screening_validation"})
        self.assertEqual(cards["Best iteration (잠정)"][0], "0")
        self.assertEqual(cards["최근 screening 생산량"][0], "39.00개 (I5)")
        self.assertIn("-1.00개", cards["최근 screening 생산량"][1])
        self.assertEqual(cards["다음 validation"][0], "Iteration 10")

    def test_screening_tie_break_matches_checkpoint_selection(self):
        self.write_rows([
            {"iteration": 0, "validation_products_mean": 40, "validation_products_std": 3},
            {"iteration": 10, "validation_products_mean": 40, "validation_products_std": 2},
            {"iteration": 5, "validation_products_mean": 40, "validation_products_std": 2},
        ])
        self.assertEqual(self.cards()["Best iteration (잠정)"][0], "5")

    def test_final_best_uses_final_mean_std_and_sample_count(self):
        self.write_rows([
            {"iteration": 0, "validation_products_mean": 42, "validation_products_std": 2},
            {"iteration": 5, "validation_products_mean": 40, "validation_products_std": 3},
        ])
        self.summary.update(status="completed", best_iteration=5,
                            best_validation_completed_products_avg=41.25,
                            best_validation_completed_products_std=1.75)
        cards = self.cards({"status": "completed"})
        self.assertEqual(cards["Best iteration (최종)"][0], "5")
        self.assertEqual(cards["Best validation 평균 생산량"][0], "41.25개")
        self.assertIn("1.75개", cards["Best validation 평균 생산량"][1])
        self.assertIn("20 episodes", cards["Best validation 평균 생산량"][1])
        self.assertIn("-2.00개", cards["최근 screening 생산량"][1])
        self.assertEqual(cards["다음 validation"][0], "종료")

    def test_final_selection_does_not_claim_final_best_early(self):
        self.write_rows([{"iteration": 0, "validation_products_mean": 40,
                          "final_validation_products_mean": 30}])
        self.summary["stop_reason"] = "production_plateau"
        cards = self.cards({"phase": "final_selection_validation", "iteration": 0})
        self.assertEqual(cards["Best iteration (잠정)"][0], "0")
        self.assertEqual(cards["Best validation 평균 생산량"][0], "40.00개")
        self.assertEqual(cards["다음 validation"][0], "최종 선정 평가")

    def test_patience_uses_last_completed_check_not_current_iteration(self):
        self.summary.update(early_stopping={"enabled": True, "min_iterations": 45,
                            "patience_iterations": 20, "consecutive_checks": 2},
                            early_stopping_history=[{"iteration": 45, "early_stop_stagnant_iterations": 15,
                            "early_stop_low_gain": True, "early_stop_triggered": False}])
        value, detail = self.cards({"iteration": 49})["조기 종료 점검"]
        self.assertIn("15/20", value)
        self.assertIn("I45", detail)
        self.assertIn("1/2", detail)

    def test_watcher_refreshes_separate_file_and_never_writes_training_state(self):
        job = self.output / "background_job"
        job.mkdir()
        atomic_json(job / "job.json", {"runs": [{"label": "test", "output": str(self.output)}]})
        atomic_json(job / "status.json", {"status": "running", "heartbeat_epoch": time.time()})
        TrainingProgress(self.output).update(status="running", iteration=5)
        original_progress = (self.output / "training_progress.json").read_bytes()
        (job / "live_training.html").write_text("original supervisor", encoding="utf-8")

        def finish(_):
            atomic_json(job / "status.json", {"status": "completed"})

        with patch("manufacturing_sim.adp.background.time.sleep", side_effect=finish), \
                patch("manufacturing_sim.adp.background.open_monitor") as opened:
            path = watch_monitor(job)
        opened.assert_called_once_with(path)
        self.assertEqual(path.name, "live_training_latest.html")
        self.assertNotIn('http-equiv="refresh"', path.read_text(encoding="utf-8"))
        self.assertEqual((job / "live_training.html").read_text(encoding="utf-8"), "original supervisor")
        self.assertEqual((self.output / "training_progress.json").read_bytes(), original_progress)

    def test_watcher_rebuilds_td_display_only_when_artifacts_change(self):
        job = self.output / "background_job"
        job.mkdir()
        atomic_json(job / "job.json", {"runs": [{"label": "test", "output": str(self.output)}]})
        atomic_json(job / "status.json", {"status": "running", "heartbeat_epoch": time.time()})
        atomic_json(self.output / "training_summary.json", {"return_estimator": "n_step_td"})

        def finish(_):
            atomic_json(job / "status.json", {"status": "completed"})

        with patch("manufacturing_sim.adp.background.time.sleep", side_effect=finish), \
                patch("manufacturing_sim.adp.td_dashboard.render_from_files") as render:
            watch_monitor(job, open_browser=False)
        render.assert_called_once_with(self.output, filename="training_dashboard_latest.html")


class BackgroundProcessTests(unittest.TestCase):
    def test_final_monitor_failure_still_releases_owned_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            claim = job / "output.lock"
            atomic_json(claim, {"job_dir": str(job)})
            atomic_json(job / "job.json", {
                "command": [sys.executable, "-c", "pass"], "cwd": str(job),
                "runs": [{"label": "test", "output": str(job / "output")}], "claims": [str(claim)]})
            with patch("manufacturing_sim.adp.background.render_monitor", side_effect=OSError("write fixture")):
                with self.assertRaises(OSError):
                    supervise(job)
            self.assertFalse(claim.exists())
            self.assertEqual(read_json(job / "status.json")["status"], "failed")

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
