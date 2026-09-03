from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from experiments.factory_policy_comparison.audit_experiment import (
    _observed_prefixes_consistent,
    _pre_run_fingerprints,
    audit_experiment,
)
from experiments.factory_policy_comparison.common import (
    ExperimentConfig,
    build_run_command,
    build_run_specs,
    dedicated_role_template,
    discover_run_dirs,
    load_experiment_config,
    read_csv,
    write_csv,
    write_json,
)
from experiments.factory_policy_comparison.render_dashboard import (
    _format_chart_tick,
    _line_chart,
    _worker_ranking_table,
    render_dashboard,
)
from experiments.factory_policy_comparison.forensic_audit import audit_run
from experiments.factory_policy_comparison.run_experiment import (
    experiment_audit_failed,
    open_experiment_dashboard,
    validate_adp_held_out_seeds,
)
from experiments.factory_policy_comparison.summarize_results import (
    _paired_adp_comparisons,
    _paired_bootstrap_interval,
    summarize_results,
)
from experiments.factory_policy_comparison.theoretical_capacity import (
    calculate_theoretical_capacity,
)


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_DIR = ROOT / "experiments" / "factory_policy_comparison"


class FactoryPolicyComparisonTests(unittest.TestCase):

    def test_mfg_flow_shop_theoretical_capacity_includes_process_movement_and_resources(self) -> None:
        report = calculate_theoretical_capacity(
            scenario="mfg_flow_shop",
            worker_counts=[3, 4, 5, 6],
            horizon_days=5,
            minutes_per_day=480,
        )
        self.assertTrue(report["available"])
        self.assertEqual(3, report["schema_version"])
        self.assertEqual("triangular_minimum", report["timing_basis"])
        self.assertEqual(
            "triangular_expected_value",
            report["expected_reference"]["timing_basis"],
        )
        rows = report["rows"]
        self.assertEqual([3, 4, 5, 6], [row["worker_count"] for row in rows])
        self.assertEqual([75, 75, 75, 75], [row["theoretical_max_products"] for row in rows])
        self.assertAlmostEqual(40.87, rows[0]["realistic_expected_products"], places=2)
        self.assertAlmostEqual(45.41, rows[0]["expected_flow_attempts_before_quality"], places=2)
        self.assertLess(
            rows[0]["realistic_expected_products"],
            rows[0]["theoretical_max_products"],
        )
        self.assertEqual(0.9, rows[0]["quality_yield"])
        self.assertEqual(0.9375, rows[0]["machine_availability"])
        self.assertEqual(2, rows[0]["machines_per_station"])
        self.assertEqual(18.36, rows[0]["station2_cycle_min"])
        self.assertEqual(9.18, rows[0]["station2_parallel_cycle_min"])
        self.assertEqual(2, rows[0]["finite_buffer_capacities"]["s1_output"])
        self.assertGreater(rows[0]["first_product_min"], 60.0)
        self.assertGreater(rows[0]["worker_busy_min_per_product"], 50.0)
        self.assertGreater(rows[0]["battery_charge_cycle_min"], 0.0)
        self.assertEqual(15, rows[0]["initial_batch_product_count"])
        self.assertGreater(rows[0]["theoretical_min_makespan_min"], 300.0)

    def test_worker3_adp_profile_has_30_held_out_runs(self) -> None:
        cfg = load_experiment_config(EXPERIMENT_DIR / "config_mfg_flow_shop_worker3_adp.yaml")
        with tempfile.TemporaryDirectory() as tmp:
            specs = build_run_specs(cfg, Path(tmp))
        self.assertEqual(len(specs), 30)
        self.assertEqual(cfg.worker_counts, [3])
        self.assertEqual(len(cfg.seeds), 5)
        self.assertIn("random_feasible_dispatch", cfg.modes)
        self.assertIn("simulation_based_adp", cfg.modes)

    def test_paired_bootstrap_interval_is_deterministic(self) -> None:
        first = _paired_bootstrap_interval([1.0, 2.0, 3.0], repetitions=1000)
        second = _paired_bootstrap_interval([1.0, 2.0, 3.0], repetitions=1000)
        self.assertEqual(first, second)
        self.assertGreater(first[0], 0.0)

    def test_single_pair_never_claims_superiority(self) -> None:
        rows = [
            {
                "objective_mode": "maximize_throughput",
                "worker_count": 3,
                "mode": mode,
                "seed": 50001,
                "total_products": products,
                "throughput_per_sim_hour": products / 20.0,
                "comparison_eligible": True,
            }
            for mode, products in (("immediate_shared", 10), ("simulation_based_adp", 11))
        ]
        primary = next(
            row for row in _paired_adp_comparisons(rows) if bool(row["primary_comparison"])
        )
        self.assertFalse(primary["superiority_demonstrated"])

    def test_paired_adp_comparison_uses_matching_seeds_and_primary_verdict(self) -> None:
        rows: list[dict[str, object]] = []
        for seed, baseline, adp in ((50001, 10, 12), (50002, 11, 13), (50003, 9, 11)):
            for mode, products in (("immediate_shared", baseline), ("simulation_based_adp", adp)):
                rows.append(
                    {
                        "objective_mode": "maximize_throughput",
                        "worker_count": 3,
                        "mode": mode,
                        "seed": seed,
                        "total_products": products,
                        "throughput_per_sim_hour": products / 20.0,
                        "comparison_eligible": True,
                    }
                )
        paired = _paired_adp_comparisons(rows)
        primary = next(row for row in paired if bool(row["primary_comparison"]))
        self.assertEqual(primary["paired_seed_count"], 3)
        self.assertEqual(primary["mean_difference"], 2.0)
        self.assertEqual(primary["win_count"], 3)
        self.assertTrue(primary["superiority_demonstrated"])

    def test_adp_preflight_rejects_seed_overlap_and_non_worker3_checkpoint(self) -> None:
        import torch

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checkpoint.pt"
            torch.save(
                {
                    "manifest": {
                        "checkpoint_id": "fixture",
                        "worker_count_range": [3, 3],
                        "seed_partitions": {
                            "training": {"values": [2026, 2027]},
                            "screening_validation": {"values": [102626]},
                            "final_selection_validation": {"values": [102636]},
                            "held_out_test": {"values": [50001, 50002]},
                            "disjoint": True,
                        },
                    }
                },
                path,
            )
            result = validate_adp_held_out_seeds(path, [50001, 50002])
            self.assertEqual(result["held_out_seed_count"], 2)
            with self.assertRaisesRegex(RuntimeError, "overlap"):
                validate_adp_held_out_seeds(path, [2026])
            with self.assertRaisesRegex(RuntimeError, "not declared"):
                validate_adp_held_out_seeds(path, [59999])

            torch.save(
                {
                    "manifest": {
                        "worker_count_range": [3, 6],
                        "seed_partitions": {
                            "training": {"values": [2026]},
                            "screening_validation": {"values": [102626]},
                            "final_selection_validation": {"values": [102636]},
                            "held_out_test": {"values": [50001]},
                            "disjoint": True,
                        },
                    }
                },
                path,
            )
            with self.assertRaisesRegex(RuntimeError, "worker_count_range"):
                validate_adp_held_out_seeds(path, [50001])

    def _cfg(
        self,
        *,
        objectives: list[str] | None = None,
        modes: list[str] | None = None,
        worker_counts: list[int] | None = None,
    ) -> ExperimentConfig:
        return ExperimentConfig(
            scenario="mfg_flow_shop",
            horizon_days=5,
            makespan_max_sim_days=30,
            minutes_per_day=480.0,
            objective_modes=objectives or ["maximize_throughput", "minimize_makespan"],
            seeds=[2026],
            worker_counts=worker_counts or [3],
            modes=modes or ["immediate_shared", "rolling_horizon_shared"],
            common_overrides=[
                "runtime.ui.auto_open_results=false",
                "runtime.ui.auto_start_replay_studio_3d=false",
            ],
            metrics={
                "objective": [
                    "total_products",
                    "throughput_per_sim_hour",
                    "makespan_min",
                    "initial_batch_progress_ratio",
                    "initial_batch_yield_ratio",
                ],
                "robot": ["humanoid_blocked_ratio_avg", "humanoid_incident_total", "otc"],
            },
        )

    def _write_run(
        self,
        root: Path,
        *,
        objective_mode: str,
        mode: str,
        worker_count: int,
        products: int,
        throughput: float = 0.0,
        makespan: float | None = None,
    ) -> Path:
        run_dir = (
            root
            / "runs"
            / objective_mode
            / mode
            / f"workers_{worker_count}"
            / "seed_2026"
        )
        run_dir.mkdir(parents=True)
        is_makespan = objective_mode == "minimize_makespan"
        total_days = 30 if is_makespan else 5
        write_json(
            run_dir / "run_meta.json",
            {
                "scenario_type": "mfg_flow_shop",
                "decision_mode": mode,
                "objective_mode": objective_mode,
                "seed": 2026,
                "total_days": total_days,
                "minutes_per_day": 480.0,
                "configured_throughput_days": 5,
                "configured_max_sim_days": 30,
                "task_primitive_timing": {"profile_fingerprint": "flow-timing-v1"},
                "stochastic_streams": {"scheme": "isolated_v1", "base_seed": 2026},
            },
        )
        write_json(
            run_dir / "kpi.json",
            {
                "scenario_type": "mfg_flow_shop",
                "objective_mode": objective_mode,
                "objective_status": "complete",
                "termination_reason": (
                    "initial_material_batch_terminal_complete" if is_makespan else "completed_horizon"
                ),
                "sim_elapsed_min": makespan + 0.2 if is_makespan and makespan is not None else 2400.0,
                "total_products": products,
                "throughput_per_sim_hour": throughput,
                "completed_product_lead_time_avg_min": 420.0,
                "makespan_min": makespan,
                "makespan_status": "complete" if is_makespan else "not_applicable",
                "initial_batch_material_count": 30 if is_makespan else 0,
                "initial_batch_terminal_material_count": 30 if is_makespan else 0,
                "initial_batch_progress_ratio": 1.0 if is_makespan else 0.0,
                "initial_batch_yield_ratio": 0.8 if is_makespan else 0.0,
                "humanoid_blocked_ratio_avg": 0.1,
                "humanoid_execution_ratio_avg": 0.75,
                "humanoid_incident_total": 2,
                "otc": 4.0,
                # Immediate policies retain the generic default window in KPI
                # metadata, but the comparison dashboard must ignore it.
                "rolling_horizon": {
                    "enabled": mode.startswith("rolling_horizon_"),
                    "window_min": 3.0 if mode.startswith("rolling_horizon_") else 5.0,
                },
            },
        )
        write_json(
            run_dir / "pre_run_diagnostics.json",
            {
                "scenario_type": "mfg_flow_shop",
                "supported": True,
                "metric_order": ["traffic_contention_index"],
                "inputs": {
                    "worker_ids": [f"A{index}" for index in range(1, worker_count + 1)],
                    "machine_ids": ["S1M1", "S2M1"],
                    "estimated_task_counts": {"objective_mode": objective_mode},
                    "mfg_flow_shop_task_policy": {"decision_mode": mode},
                },
            },
        )
        write_json(run_dir / "artifact_status.json", {"errors": {}})
        restock_times = [0.0] if is_makespan else [0.0, 480.0, 960.0, 1440.0, 1920.0]
        events = [
            {
                "t": event_time,
                "type": "WAREHOUSE_MATERIAL_RESTOCK",
                "entity_id": "warehouse_material_shelf",
                "details": {"reason": "initial_fill" if event_time == 0 else "day_boundary"},
            }
            for event_time in restock_times
        ]
        events.extend(
            [
                {"t": 100.0, "type": "INSPECT_PASS", "entity_id": "PRODUCT-1", "details": {}},
                {"t": 200.0, "type": "INSPECT_FAIL", "entity_id": "PRODUCT-2", "details": {}},
                {
                    "t": 300.0,
                    "type": "MACHINE_REPAIR_START",
                    "entity_id": "S1M1",
                    "details": {"repair_total_min": 20.0},
                },
            ]
        )
        (run_dir / "events.jsonl").write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        for name, body in [
            ("results_dashboard.html", "<html>hub</html>"),
            ("kpi_dashboard.html", "<html>kpi</html>"),
            ("gantt.html", "<html>gantt</html>"),
            ("dashboard_manifest.json", "{}"),
        ]:
            (run_dir / name).write_text(body, encoding="utf-8")
        return run_dir

    def test_summary_always_collects_metrics_required_by_dashboard(self) -> None:
        cfg = self._cfg(
            objectives=["maximize_throughput"],
            modes=["immediate_shared"],
            worker_counts=[3],
        )
        cfg = replace(
            cfg,
            metrics={"productivity": ["total_products", "throughput_per_sim_hour"]},
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(
                root,
                objective_mode="maximize_throughput",
                mode="immediate_shared",
                worker_count=3,
                products=10,
                throughput=0.5,
            )
            payload = summarize_results(root, cfg)

            self.assertIn("completed_product_lead_time_avg_min", payload["metrics"])
            self.assertIn("humanoid_incident_total", payload["metrics"])
            self.assertIn("humanoid_execution_ratio_avg", payload["metrics"])
            self.assertIn("otc", payload["metrics"])
            mode_worker = payload["mode_worker"][0]
            self.assertEqual(420.0, mode_worker["completed_product_lead_time_avg_min.mean"])
            self.assertEqual(2.0, mode_worker["humanoid_incident_total.mean"])
            self.assertEqual(0.75, mode_worker["humanoid_execution_ratio_avg.mean"])
            self.assertEqual(4.0, mode_worker["otc.mean"])

    def test_forensic_audit_accepts_intentionally_disabled_event_export(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._write_run(
                Path(tmp),
                objective_mode="maximize_throughput",
                mode="immediate_shared",
                worker_count=3,
                products=10,
                throughput=0.5,
            )
            (run_dir / "events.jsonl").unlink()

            result = audit_run(run_dir)

            self.assertTrue(result.passed, result.errors)
            self.assertFalse(result.events_available)
            self.assertTrue(any("event-level" in warning for warning in result.warnings))

    def test_default_config_is_32_run_flow_shop_experiment(self) -> None:
        cfg = load_experiment_config()
        self.assertEqual("mfg_flow_shop", cfg.scenario)
        self.assertEqual(["maximize_throughput", "minimize_makespan"], cfg.objective_modes)
        self.assertEqual([2026], cfg.seeds)
        self.assertEqual([3, 4, 5, 6], cfg.worker_counts)
        self.assertEqual(
            [
                "immediate_shared",
                "immediate_dedicated_roles",
                "rolling_horizon_shared",
                "rolling_horizon_dedicated_roles",
            ],
            cfg.modes,
        )
        self.assertEqual(32, len(build_run_specs(cfg, Path("result"))))

    def test_legacy_factory_profile_is_preserved(self) -> None:
        cfg = load_experiment_config(EXPERIMENT_DIR / "config_factory_mfg_basic.yaml")
        self.assertEqual("factory_mfg_basic", cfg.scenario)
        self.assertEqual(["scenario_default"], cfg.objective_modes)
        self.assertEqual(180, len(build_run_specs(cfg, Path("result"))))

    def test_default_requirements_are_experiment_only(self) -> None:
        default_requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        optional_requirements = (ROOT / "requirements-optional.txt").read_text(encoding="utf-8").lower()
        for package in ("hydra-core", "omegaconf", "simpy", "ortools", "plotly", "pandas"):
            self.assertIn(package, default_requirements)
        for package in ("streamlit", "graphifyy", "openai"):
            self.assertNotIn(package, default_requirements)
            self.assertIn(package, optional_requirements)

    def test_run_specs_and_commands_include_objective_axis(self) -> None:
        cfg = load_experiment_config()
        with tempfile.TemporaryDirectory() as tmp:
            specs = build_run_specs(cfg, Path(tmp))
            self.assertEqual(32, len({spec.run_id for spec in specs}))
            throughput = specs[0]
            makespan = next(spec for spec in specs if spec.objective_mode == "minimize_makespan")
            self.assertIn("maximize_throughput", throughput.run_dir.parts)
            self.assertIn("minimize_makespan", makespan.run_dir.parts)
            throughput_command = " ".join(build_run_command(throughput, cfg.common_overrides))
            makespan_command = " ".join(build_run_command(makespan, cfg.common_overrides))
            self.assertIn("scenario=mfg_flow_shop", throughput_command)
            self.assertIn("scenario.objective.mode=maximize_throughput", throughput_command)
            self.assertIn("scenario.factory.num_workers=3", throughput_command)
            self.assertIn("runtime.ui.export_replay_artifacts=false", throughput_command)
            self.assertIn("runtime.artifacts.export_events=false", throughput_command)
            self.assertIn("scenario.objective.mode=minimize_makespan", makespan_command)
            self.assertIn("scenario.objective.makespan.max_sim_days=30", makespan_command)
            self.assertNotIn("scenario_worker_task_priority.factory_mfg_basic", makespan_command)

    def test_rolling_window_override_applies_only_to_rolling_modes(self) -> None:
        cfg = load_experiment_config()
        with tempfile.TemporaryDirectory() as tmp:
            specs = build_run_specs(cfg, Path(tmp))
            immediate = next(spec for spec in specs if spec.mode == "immediate_shared")
            rolling = next(spec for spec in specs if spec.mode == "rolling_horizon_shared")

            immediate_command = " ".join(
                build_run_command(immediate, cfg.common_overrides, rolling_window_min=3.0)
            )
            rolling_command = " ".join(
                build_run_command(rolling, cfg.common_overrides, rolling_window_min=3.0)
            )

            self.assertNotIn("decision.rolling_horizon.window_min", immediate_command)
            self.assertIn("decision.rolling_horizon.window_min=3", rolling_command)

    def test_replay_export_can_be_explicitly_enabled_for_an_experiment(self) -> None:
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_run_specs(cfg, Path(tmp), limit=1)[0]
            command = build_run_command(
                spec,
                [*cfg.common_overrides, "runtime.ui.export_replay_artifacts=true"],
            )
            self.assertIn("runtime.ui.export_replay_artifacts=true", command)
            self.assertNotIn("runtime.ui.export_replay_artifacts=false", command)

    def test_event_export_can_be_explicitly_enabled_for_an_experiment(self) -> None:
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_run_specs(cfg, Path(tmp), limit=1)[0]
            command = build_run_command(
                spec,
                [*cfg.common_overrides, "runtime.artifacts.export_events=true"],
            )
            self.assertIn("runtime.artifacts.export_events=true", command)
            self.assertNotIn("runtime.artifacts.export_events=false", command)

    def test_dedicated_role_template_still_supports_legacy_factory_profile(self) -> None:
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
            self.assertTrue(expected.issubset({code for codes in role_map.values() for code in codes}))

    def test_discovery_supports_objective_and_legacy_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            new_run = root / "runs" / "maximize_throughput" / "mode" / "workers_3" / "seed_2026"
            legacy_run = root / "runs" / "fixed_priority" / "workers_3" / "seed_2026"
            for run in (new_run, legacy_run):
                run.mkdir(parents=True)
                write_json(run / "run_meta.json", {"decision_mode": "mode"})
            self.assertEqual({new_run.resolve(), legacy_run.resolve()}, set(discover_run_dirs(root)))

    def test_audit_groups_fairness_by_objective_worker_and_seed(self) -> None:
        cfg = self._cfg()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for objective in cfg.objective_modes:
                for mode in cfg.modes:
                    self._write_run(
                        root,
                        objective_mode=objective,
                        mode=mode,
                        worker_count=3,
                        products=10,
                        throughput=0.5,
                        makespan=900.0 if objective == "minimize_makespan" else None,
                    )
            with patch(
                "experiments.factory_policy_comparison.audit_experiment._run_audit_script",
                return_value=("pass", 0, "PASS"),
            ):
                summary = audit_experiment(root, cfg)
            self.assertEqual(4, summary["expected_run_count"])
            self.assertEqual(4, summary["fairness_pass_count"])
            rows = read_csv(root / "fairness_report.csv")
            self.assertTrue(all(row["objective_contract_ok"] == "True" for row in rows))
            self.assertTrue(all(row["quality_stream_prefix_matches"] == "True" for row in rows))

    def test_fairness_ignores_policy_scheduler_and_empty_quality_observations(self) -> None:
        self.assertTrue(_observed_prefixes_consistent([[], ["P", "F"], ["P"]]))
        self.assertFalse(_observed_prefixes_consistent([["P"], ["F"]]))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            immediate_dir = root / "immediate"
            rolling_dir = root / "rolling"
            immediate_dir.mkdir()
            rolling_dir.mkdir()
            common_inputs = {
                "worker_ids": ["A1", "A2", "A3"],
                "machine_ids": ["S1M1", "S2M1"],
            }
            write_json(
                immediate_dir / "pre_run_diagnostics.json",
                {
                    "scenario_type": "mfg_flow_shop",
                    "supported": True,
                    "metric_order": [],
                    "inputs": {
                        **common_inputs,
                        "rolling_horizon_scheduler": {"enabled": False},
                    },
                },
            )
            write_json(
                rolling_dir / "pre_run_diagnostics.json",
                {
                    "scenario_type": "mfg_flow_shop",
                    "supported": True,
                    "metric_order": [],
                    "inputs": common_inputs,
                    "policy_runtime": {
                        "rolling_horizon_scheduler": {
                            "enabled": True,
                            "scheduler_mode": "strict_periodic",
                        }
                    },
                },
            )
            immediate_environment, immediate_policy = _pre_run_fingerprints(immediate_dir)
            rolling_environment, rolling_policy = _pre_run_fingerprints(rolling_dir)
            self.assertEqual(immediate_environment, rolling_environment)
            self.assertNotEqual(immediate_policy, rolling_policy)

    def test_summary_is_separate_by_objective_and_computes_both_marginals(self) -> None:
        cfg = self._cfg(modes=["immediate_shared"], worker_counts=[3, 4])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, objective_mode="maximize_throughput", mode="immediate_shared", worker_count=3, products=10, throughput=0.5)
            self._write_run(root, objective_mode="maximize_throughput", mode="immediate_shared", worker_count=4, products=14, throughput=0.7)
            self._write_run(root, objective_mode="minimize_makespan", mode="immediate_shared", worker_count=3, products=12, makespan=1000.0)
            self._write_run(root, objective_mode="minimize_makespan", mode="immediate_shared", worker_count=4, products=12, makespan=850.0)
            payload = summarize_results(root, cfg)
            self.assertEqual(4, payload["completed_run_count"])
            rows = read_csv(root / "mode_worker_summary.csv")
            throughput4 = next(row for row in rows if row["objective_mode"] == "maximize_throughput" and row["worker_count"] == "4")
            makespan4 = next(row for row in rows if row["objective_mode"] == "minimize_makespan" and row["worker_count"] == "4")
            self.assertAlmostEqual(0.2, float(throughput4["throughput_per_sim_hour.marginal_gain"]))
            self.assertAlmostEqual(150.0, float(makespan4["makespan_min.marginal_reduction"]))

    def test_summary_excludes_audit_failures_and_does_not_fake_single_run_std(self) -> None:
        cfg = self._cfg(
            objectives=["maximize_throughput"],
            modes=["immediate_shared", "rolling_horizon_shared"],
            worker_counts=[3],
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid_dir = self._write_run(
                root,
                objective_mode="maximize_throughput",
                mode="immediate_shared",
                worker_count=3,
                products=10,
                throughput=0.5,
            )
            invalid_dir = self._write_run(
                root,
                objective_mode="maximize_throughput",
                mode="rolling_horizon_shared",
                worker_count=3,
                products=999,
                throughput=49.95,
            )
            identity_rows = [
                {
                    "run_dir": str(run_dir),
                    "objective_mode": "maximize_throughput",
                    "mode": mode,
                    "worker_count": 3,
                    "seed": 2026,
                }
                for run_dir, mode in [
                    (valid_dir, "immediate_shared"),
                    (invalid_dir, "rolling_horizon_shared"),
                ]
            ]
            write_csv(
                root / "run_status.csv",
                [{**row, "status": "completed"} for row in identity_rows],
            )
            write_csv(
                root / "audit_summary.csv",
                [
                    {
                        **row,
                        "artifact_audit_status": "pass" if row["mode"] == "immediate_shared" else "fail",
                        "kpi_audit_status": "pass",
                    }
                    for row in identity_rows
                ],
            )
            write_csv(
                root / "fairness_report.csv",
                [{**row, "fairness_pass": True} for row in identity_rows],
            )

            payload = summarize_results(root, cfg)
            self.assertEqual(2, payload["completed_run_count"])
            self.assertEqual(1, payload["comparison_run_count"])

            run_rows = read_csv(root / "comparison_summary.csv")
            valid_run = next(row for row in run_rows if row["mode"] == "immediate_shared")
            invalid_run = next(row for row in run_rows if row["mode"] == "rolling_horizon_shared")
            self.assertEqual("True", valid_run["comparison_eligible"])
            self.assertEqual("False", invalid_run["comparison_eligible"])

            mode_rows = read_csv(root / "mode_summary.csv")
            valid_mode = next(row for row in mode_rows if row["mode"] == "immediate_shared")
            invalid_mode = next(row for row in mode_rows if row["mode"] == "rolling_horizon_shared")
            self.assertEqual("1", valid_mode["comparison_run_count"])
            self.assertEqual("", valid_mode["throughput_per_sim_hour.std"])
            self.assertEqual("0", invalid_mode["comparison_run_count"])
            self.assertEqual("", invalid_mode["throughput_per_sim_hour.mean"])

    def test_marginals_are_normalized_per_added_worker(self) -> None:
        cfg = self._cfg(modes=["immediate_shared"], worker_counts=[3, 5])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, objective_mode="maximize_throughput", mode="immediate_shared", worker_count=3, products=10, throughput=0.5)
            self._write_run(root, objective_mode="maximize_throughput", mode="immediate_shared", worker_count=5, products=14, throughput=0.7)
            self._write_run(root, objective_mode="minimize_makespan", mode="immediate_shared", worker_count=3, products=12, makespan=1000.0)
            self._write_run(root, objective_mode="minimize_makespan", mode="immediate_shared", worker_count=5, products=12, makespan=850.0)
            summarize_results(root, cfg)
            rows = read_csv(root / "mode_worker_summary.csv")
            throughput5 = next(row for row in rows if row["objective_mode"] == "maximize_throughput" and row["worker_count"] == "5")
            makespan5 = next(row for row in rows if row["objective_mode"] == "minimize_makespan" and row["worker_count"] == "5")
            self.assertAlmostEqual(0.1, float(throughput5["throughput_per_sim_hour.marginal_gain"]))
            self.assertAlmostEqual(75.0, float(makespan5["makespan_min.marginal_reduction"]))

    def test_dashboard_renders_integrated_objective_tabs_and_links(self) -> None:
        cfg = self._cfg(modes=["immediate_shared"], worker_counts=[3])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_run(root, objective_mode="maximize_throughput", mode="immediate_shared", worker_count=3, products=10, throughput=0.5)
            self._write_run(root, objective_mode="minimize_makespan", mode="immediate_shared", worker_count=3, products=12, makespan=900.0)
            summarize_results(root, cfg)
            (root / "fairness_report.csv").write_text(
                "objective_mode,mode,worker_count,seed,fairness_pass\nmaximize_throughput,immediate_shared,3,2026,True\n",
                encoding="utf-8",
            )
            (root / "audit_summary.csv").write_text(
                "objective_mode,mode,worker_count,seed,artifact_audit_status,kpi_audit_status\nmaximize_throughput,immediate_shared,3,2026,pass,pass\n",
                encoding="utf-8",
            )
            (root / "run_status.csv").write_text(
                "run_id,objective_mode,mode,worker_count,seed,scenario,horizon_days,makespan_max_sim_days,run_dir,status,return_code,elapsed_sec,reason,command\n"
                f"run,maximize_throughput,immediate_shared,3,2026,mfg_flow_shop,5,30,{root},completed,0,65,,cmd\n",
                encoding="utf-8",
            )
            write_json(
                root / "experiment_plan.json",
                {
                    "scenario": "mfg_flow_shop",
                    "horizon_days": 5,
                    "makespan_max_sim_days": 30,
                    "objective_modes": ["maximize_throughput", "minimize_makespan"],
                    # Old plan files could contain one mode entry per run.
                    "modes": ["immediate_shared", "immediate_shared"],
                    "worker_counts": [3],
                    "seeds": [2026],
                    "run_count": 2,
                },
            )
            path = render_dashboard(root, cfg)
            html = path.read_text(encoding="utf-8")
            self.assertIn("mfg_flow_shop Policy Comparison", html)
            self.assertIn('data-tab="throughput"', html)
            self.assertIn('data-tab="makespan"', html)
            self.assertIn("정책별 평균 Makespan", html)
            self.assertIn("single-run comparisons", html)
            self.assertIn("1m 05s", html)
            self.assertIn('<div class="label">Modes</div><div class="value">1</div>', html)
            self.assertIn("results_dashboard.html", html)
            self.assertIn("정책별 핵심 성능 요약", html)
            self.assertIn("평균 제품 수", html)
            self.assertIn("75.00%", html)
            self.assertIn("Individual Run Artifacts", html)
            self.assertIn("Run Gantt", html)
            self.assertIn("정책별 집계 간트가 아닙니다", html)
            self.assertIn("throughput_per_sim_hour.min", html)
            self.assertIn("throughput_per_sim_hour.max", html)
            self.assertIn("makespan_min.std", html)
            self.assertIn("makespan_min.min", html)
            self.assertIn("makespan_min.max", html)
            self.assertIn("per additional worker", html)
            self.assertIn("이론적 최대 생산량 산정", html)
            self.assertIn("Worker 수별 생산능력 기준", html)
            self.assertIn("현실적 기대 생산량 산정", html)
            self.assertIn("realistic_expected_products", html)
            self.assertIn("Theoretical Minimum Batch Makespan by Worker Count", html)
            self.assertIn("parallel_resource_finite_buffer_shortest_path_bound_v2", html)
            self.assertTrue((root / "theoretical_capacity.json").exists())
            self.assertIn("With one seed, standard deviation is left blank", html)
            self.assertNotIn('<div class="label">Rolling Window</div>', html)

            plan = json.loads((root / "experiment_plan.json").read_text(encoding="utf-8"))
            plan["seeds"] = [2026, 2027, 2028]
            write_json(root / "experiment_plan.json", plan)
            html = render_dashboard(root, cfg).read_text(encoding="utf-8")
            self.assertIn(
                "Across 3 seeds, standard deviation is the sample standard deviation",
                html,
            )
            self.assertNotIn("With one seed, standard deviation is left blank", html)

    def test_line_chart_includes_zero_and_reserves_legend_space(self) -> None:
        chart = _line_chart(
            [
                {"mode": "mode_a", "worker_count": "3", "metric": "-2.0"},
                {"mode": "mode_a", "worker_count": "5", "metric": "-1.0"},
            ],
            "metric",
            "Negative Metric",
        )
        self.assertIn("data-y-min='-2'", chart)
        self.assertIn("data-y-max='0'", chart)
        self.assertIn("data-plot-right='570'", chart)
        self.assertIn("data-legend-left='588'", chart)
        self.assertIn(">휴머노이드 수</text>", chart)
        self.assertEqual("112", _format_chart_tick(112.0))
        self.assertEqual("0.025", _format_chart_tick(0.025))

    def test_dashboard_uses_competition_rank_for_equal_policy_values(self) -> None:
        metric = "throughput_per_sim_hour.mean"
        table = _worker_ranking_table(
            [
                {"worker_count": "4", "mode": "mode_a", metric: "0.6"},
                {"worker_count": "4", "mode": "mode_b", metric: "0.6"},
                {"worker_count": "4", "mode": "mode_c", metric: "0.5"},
            ],
            metric,
            higher_is_better=True,
        )
        self.assertIn("<tr><td>4</td><td>1</td><td>mode_a</td><td>0.6</td></tr>", table)
        self.assertIn("<tr><td>4</td><td>1</td><td>mode_b</td><td>0.6</td></tr>", table)
        self.assertIn("<tr><td>4</td><td>3</td><td>mode_c</td><td>0.5</td></tr>", table)

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

    def test_experiment_audit_failure_includes_missing_runs(self) -> None:
        passing = {
            "expected_run_count": 32,
            "run_count": 32,
            "fairness_pass_count": 32,
            "artifact_audit_fail_count": 0,
            "kpi_audit_fail_count": 0,
            "unexpected_run_count": 0,
        }
        self.assertFalse(experiment_audit_failed(passing))
        self.assertTrue(experiment_audit_failed({**passing, "run_count": 31, "fairness_pass_count": 31}))
        self.assertTrue(experiment_audit_failed({**passing, "fairness_pass_count": 31}))
        self.assertTrue(experiment_audit_failed({**passing, "artifact_audit_fail_count": 1}))


if __name__ == "__main__":
    unittest.main()
