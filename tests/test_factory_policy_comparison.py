from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from experiments.factory_policy_comparison.audit_experiment import audit_experiment
from experiments.factory_policy_comparison.common import (
    ExperimentConfig,
    build_run_command,
    build_run_specs,
    dedicated_role_template,
    load_experiment_config,
    read_csv,
    write_json,
)
from experiments.factory_policy_comparison.render_dashboard import render_dashboard
from experiments.factory_policy_comparison.run_experiment import experiment_audit_failed, open_experiment_dashboard
from experiments.factory_policy_comparison.summarize_results import summarize_results


class FactoryPolicyComparisonTests(unittest.TestCase):
    def _cfg(self) -> ExperimentConfig:
        return ExperimentConfig(
            scenario="factory_mfg_basic",
            horizon_days=5,
            minutes_per_day=240.0,
            seeds=[2026, 2027],
            worker_counts=[3, 4],
            modes=["fixed_priority", "rolling_horizon_throughput_optimizer"],
            common_overrides=[
                "runtime.ui.auto_open_results=false",
                "runtime.ui.auto_start_replay_studio_3d=false",
            ],
            metrics={
                "productivity": ["total_products", "throughput_per_sim_hour"],
                "robot": ["humanoid_blocked_ratio_avg"],
            },
        )

    def _write_run(
        self,
        root: Path,
        *,
        mode: str,
        seed: int,
        worker_count: int = 3,
        products: int = 1,
        throughput: float = 0.25,
    ) -> Path:
        run_dir = root / "runs" / mode / f"workers_{worker_count}" / f"seed_{seed}"
        run_dir.mkdir(parents=True)
        write_json(
            run_dir / "run_meta.json",
            {
                "scenario_type": "factory_mfg_basic",
                "decision_mode": mode,
                "seed": seed,
                "total_days": 5,
                "minutes_per_day": 240.0,
            },
        )
        write_json(
            run_dir / "kpi.json",
            {
                "scenario_type": "factory_mfg_basic",
                "total_products": products,
                "throughput_per_sim_hour": throughput,
                "humanoid_blocked_ratio_avg": 0.1,
            },
        )
        write_json(
            run_dir / "pre_run_diagnostics.json",
            {
                "scenario_type": "factory_mfg_basic",
                "supported": True,
                "metric_order": ["traffic_contention_index"],
                "inputs": {
                    "worker_ids": [f"A{index}" for index in range(1, worker_count + 1)],
                    "machine_ids": ["S1M1"],
                },
            },
        )
        write_json(run_dir / "artifact_status.json", {"errors": {}})
        (run_dir / "results_dashboard.html").write_text("<html>hub</html>", encoding="utf-8")
        (run_dir / "kpi_dashboard.html").write_text("<html>kpi</html>", encoding="utf-8")
        (run_dir / "gantt.html").write_text("<html>gantt</html>", encoding="utf-8")
        (run_dir / "dashboard_manifest.json").write_text("{}", encoding="utf-8")
        return run_dir

    def test_config_loads_default_modes_and_seeds(self) -> None:
        cfg = load_experiment_config()
        self.assertEqual("factory_mfg_basic", cfg.scenario)
        self.assertEqual(
            [
                "fixed_priority",
                "adaptive_priority",
                "rolling_horizon_aging_priority",
                "rolling_horizon_dedicated_roles",
                "bottleneck_aware_dispatch",
                "rolling_horizon_throughput_optimizer",
            ],
            cfg.modes,
        )
        self.assertEqual([2026, 2027, 2028, 2029, 2030], cfg.seeds)
        self.assertEqual([3, 4, 5, 6, 7, 8], cfg.worker_counts)
        self.assertIn("runtime.ui.auto_start_replay_studio_3d=false", cfg.common_overrides)

    def test_default_requirements_are_experiment_only(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        default_requirements = (repo_root / "requirements.txt").read_text(encoding="utf-8").lower()
        optional_requirements = (repo_root / "requirements-optional.txt").read_text(encoding="utf-8").lower()
        for package in ("hydra-core", "omegaconf", "simpy", "ortools", "plotly", "pandas"):
            self.assertIn(package, default_requirements)
        for package in ("streamlit", "graphifyy", "openai"):
            self.assertNotIn(package, default_requirements)
            self.assertIn(package, optional_requirements)

    def test_build_run_specs_and_command_include_fairness_overrides(self) -> None:
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            specs = build_run_specs(cfg, Path(tmp), limit=1)
            self.assertEqual(1, len(specs))
            command = build_run_command(specs[0], cfg.common_overrides)
            joined = " ".join(command)
            self.assertIn("scenario=factory_mfg_basic", joined)
            self.assertIn("decision=fixed_priority", joined)
            self.assertIn("scenario.factory.num_workers=3", joined)
            self.assertIn("seed=2026", joined)
            self.assertIn("scenario.horizon.num_days=5", joined)
            self.assertIn("runtime.ui.auto_open_results=false", joined)
            self.assertIn("runtime.ui.auto_start_replay_studio_3d=false", joined)
            self.assertIn("hydra.run.dir=", joined)
            self.assertIn("workers_3", specs[0].run_dir.as_posix())

    def test_dedicated_role_template_covers_all_factory_tasks_for_scaling_range(self) -> None:
        expected = {
            "REPLENISH_MATERIAL",
            "REPAIR_MACHINE",
            "LOAD_MACHINE",
            "SETUP_MACHINE",
            "UNLOAD_MACHINE",
            "MANAGE_ROBOT_POWER",
            "TRANSFER",
            "INSPECT_PRODUCT",
            "COLLECT_WASTE_OR_SCRAP",
            "PREVENTIVE_MAINTENANCE",
        }
        for worker_count in range(3, 9):
            role_map = dedicated_role_template(worker_count)
            covered = {code for codes in role_map.values() for code in codes}
            self.assertTrue(expected.issubset(covered), worker_count)

    def test_audit_experiment_reports_fairness_pass(self) -> None:
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, mode="fixed_priority", seed=2026, worker_count=3)
            self._write_run(root, mode="rolling_horizon_throughput_optimizer", seed=2026, worker_count=3)
            self._write_run(root, mode="fixed_priority", seed=2026, worker_count=4)
            self._write_run(root, mode="rolling_horizon_throughput_optimizer", seed=2026, worker_count=4)
            with patch(
                "experiments.factory_policy_comparison.audit_experiment._run_audit_script",
                return_value=("pass", 0, "PASS"),
            ):
                summary = audit_experiment(root, cfg)
            self.assertEqual(4, summary["fairness_pass_count"])
            rows = read_csv(root / "fairness_report.csv")
            self.assertTrue(all(row["pre_run_matches_reference"] == "True" for row in rows))

    def test_summarize_results_creates_mode_mean_std(self) -> None:
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, mode="fixed_priority", seed=2026, worker_count=3, products=1, throughput=0.2)
            self._write_run(root, mode="fixed_priority", seed=2027, worker_count=3, products=3, throughput=0.6)
            self._write_run(root, mode="fixed_priority", seed=2026, worker_count=4, products=4, throughput=0.8)
            payload = summarize_results(root, cfg)
            self.assertEqual(3, payload["completed_run_count"])
            rows = read_csv(root / "mode_summary.csv")
            row = next(item for item in rows if item["mode"] == "fixed_priority")
            self.assertEqual("3", row["completed_run_count"])
            self.assertAlmostEqual(8 / 3, float(row["total_products.mean"]), places=5)
            self.assertGreater(float(row["total_products.std"]), 0.0)
            worker_rows = read_csv(root / "mode_worker_summary.csv")
            worker3 = next(item for item in worker_rows if item["mode"] == "fixed_priority" and item["worker_count"] == "3")
            worker4 = next(item for item in worker_rows if item["mode"] == "fixed_priority" and item["worker_count"] == "4")
            self.assertEqual(2.0, float(worker3["total_products.mean"]))
            self.assertEqual(4.0, float(worker4["total_products.mean"]))
            self.assertEqual(0.4, float(worker3["throughput_per_sim_hour.mean"]))
            self.assertEqual(0.4, float(worker4["throughput_per_sim_hour.marginal_gain"]))

    def test_dashboard_renders_with_failed_run_rows(self) -> None:
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, mode="fixed_priority", seed=2026, worker_count=3)
            summarize_results(root, cfg)
            (root / "fairness_report.csv").write_text(
                "mode,worker_count,seed,scenario_ok,worker_count_ok,horizon_ok,seed_ok,pre_run_matches_reference,artifact_errors_empty,fairness_pass\n"
                "fixed_priority,3,2026,True,True,True,True,True,True,True\n",
                encoding="utf-8",
            )
            (root / "audit_summary.csv").write_text(
                "mode,worker_count,seed,artifact_audit_status,kpi_audit_status\nfixed_priority,3,2026,pass,pass\n",
                encoding="utf-8",
            )
            (root / "run_status.csv").write_text(
                "run_id,mode,worker_count,seed,scenario,horizon_days,run_dir,status,return_code,elapsed_sec,reason,command\n"
                f"fixed_priority__workers_3__seed_2026,fixed_priority,3,2026,factory_mfg_basic,5,{root},completed,0,65,,cmd\n",
                encoding="utf-8",
            )
            path = render_dashboard(root, cfg)
            html = path.read_text(encoding="utf-8")
            self.assertIn("Factory Policy Comparison", html)
            self.assertIn("fixed_priority", html)
            self.assertIn("Experiment Runtime", html)
            self.assertIn("1m 05s", html)
            self.assertIn("Throughput vs Worker Count", html)
            self.assertIn("results_dashboard.html", html)

    def test_dashboard_uses_selected_experiment_plan_for_expected_runs(self) -> None:
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, mode="fixed_priority", seed=2026, worker_count=3)
            write_json(
                root / "experiment_plan.json",
                {
                    "scenario": "factory_mfg_basic",
                    "horizon_days": 5,
                    "modes": ["fixed_priority"],
                    "worker_counts": [3],
                    "seeds": [2026],
                    "run_count": 1,
                },
            )
            summarize_results(root, cfg)
            path = render_dashboard(root, cfg)
            html = path.read_text(encoding="utf-8")
            self.assertIn('<div class="label">Expected Runs</div><div class="value">1</div>', html)
            self.assertIn("workers: 3 | seeds: [2026]", html)

    def test_experiment_dashboard_opens_in_default_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comparison_dashboard.html"
            path.write_text("<html></html>", encoding="utf-8")
            with patch(
                "experiments.factory_policy_comparison.run_experiment.webbrowser.open",
                return_value=True,
            ) as browser_open:
                self.assertTrue(open_experiment_dashboard(path))
            browser_open.assert_called_once_with(path.resolve().as_uri(), new=2)

    def test_experiment_audit_failure_includes_fairness_and_empty_runs(self) -> None:
        passing = {
            "run_count": 6,
            "fairness_pass_count": 6,
            "artifact_audit_fail_count": 0,
            "kpi_audit_fail_count": 0,
        }
        self.assertFalse(experiment_audit_failed(passing))
        self.assertTrue(experiment_audit_failed({**passing, "fairness_pass_count": 5}))
        self.assertTrue(experiment_audit_failed({**passing, "artifact_audit_fail_count": 1}))
        self.assertTrue(experiment_audit_failed({**passing, "run_count": 0, "fairness_pass_count": 0}))


if __name__ == "__main__":
    unittest.main()
