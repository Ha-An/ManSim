from __future__ import annotations

import csv
import json
from pathlib import Path
import tempfile

import pytest
import yaml

from experiments.mfg_flow_shop_paper.prepare_experiment import (
    DEFAULT_CONFIG,
    expected_counts,
    final_selection_seeds,
    load_config,
    prepare,
    screening_seeds,
    training_seed,
)
from experiments.mfg_flow_shop_paper.run_evaluation import (
    _archive_existing as archive_evaluation,
    _audit_run,
    _completed_kpi_is_valid,
    _preflight_adp_checkpoints,
    _record_evaluation_timing,
)
from experiments.mfg_flow_shop_paper.run_training import (
    _archive_existing as archive_training,
    _recorded_wall_sec,
)
from experiments.mfg_flow_shop_paper.render_standard_dashboard import (
    _add_confirmatory_overall_contrasts,
    _compatibility_status,
    _dashboard_config,
)
from experiments.mfg_flow_shop_paper.summarize_results import (
    _centered_bootstrap_p,
    _identity_errors,
    _plan_errors,
    _normalize_legacy_repair_samples,
    _observed_prefixes_consistent,
    _restock_schedule_evidence,
    fairness_report,
    paired_contrasts,
    policy_worker_summary,
)
from manufacturing_sim.simulation.scenarios.manufacturing.run import _event_audit_signature
from scripts.audit_adp_training import _json_canonical
from experiments.mfg_flow_shop_paper.extend_evaluation import build_extension
from experiments.mfg_flow_shop_paper.render_standard_dashboard import _experiment_timing
from experiments.mfg_flow_shop_paper.prepare_saved_checkpoints import bind_checkpoints


def test_saved_checkpoint_binding_preserves_baselines_and_source_rows(tmp_path):
    checkpoint = tmp_path / "saved model" / "best.pt"
    source = {
        "mode": "simulation_based_adp", "worker_count": 3, "checkpoint_path": "old.pt",
        "command_json": json.dumps(["python", "main.py", "seed=910001",
                                    "decision.adp.checkpoint_path=old.pt"]),
        "command": "old command",
    }
    baseline = {"mode": "immediate_shared", "worker_count": 3, "command_json": "[]"}
    rebound = bind_checkpoints([source, baseline], {3: checkpoint})
    assert source["checkpoint_path"] == "old.pt"
    assert rebound[1] == baseline
    assert rebound[0]["checkpoint_path"] == str(checkpoint.resolve())
    command = json.loads(rebound[0]["command_json"])
    assert command[-1] == f"decision.adp.checkpoint_path={checkpoint.resolve().as_posix()}"
    assert command[2] == "seed=910001"


def test_saved_checkpoint_binding_requires_every_requested_fleet(tmp_path):
    with pytest.raises(KeyError):
        bind_checkpoints([{"mode": "simulation_based_adp", "worker_count": 6}], {3: tmp_path / "best.pt"})


def test_training_background_keeps_plan_and_does_not_run_inline(tmp_path: Path, monkeypatch) -> None:
    from experiments.mfg_flow_shop_paper import run_training
    from manufacturing_sim.adp import background
    import sys

    output = tmp_path / "training" / "workers_3"
    with (tmp_path / "training_plan.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["worker_count", "training_replicate", "training_output"])
        writer.writeheader()
        writer.writerow({"worker_count": 3, "training_replicate": 1, "training_output": str(output)})
    captured = {}

    def fake_launch(**kwargs):
        captured.update(kwargs)
        return kwargs["job_dir"] / "live_training.html"

    monkeypatch.setattr(background, "launch", fake_launch)
    monkeypatch.setattr(sys, "argv", ["run_training.py", "--prepared", str(tmp_path),
                                     "--background", "--no-open-dashboard"])
    assert run_training.main() == 0
    assert "--background" not in captured["command"]
    assert "--no-open-dashboard" in captured["command"]
    assert captured["runs"][0]["output"] == str(output)
    assert captured["open_browser"] is False
    assert not output.exists()


def test_extension_preserves_reference_and_adds_three_policy_commands(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/immediate_shared/workers_3/seed_910001"
    source = {
        "run_id": "immediate_shared__workers_3__seed_910001", "mode": "immediate_shared",
        "worker_count": "3", "seed": "910001", "run_dir": str(run_dir),
        "command_json": json.dumps(["python", "main.py", "decision=immediate_shared",
                                    "seed=910001", f"hydra.run.dir={run_dir.as_posix()}"]),
    }
    added = build_extension([source], 5.0)
    assert len(added) == 3
    assert source["mode"] == "immediate_shared"
    assert len({row["run_id"] for row in added}) == 3
    for row in added:
        command = json.loads(row["command_json"])
        assert f"decision={row['mode']}" in command
        assert "seed=910001" in command
        assert Path(row["run_dir"]).parent.parent.name == row["mode"]
        assert ("decision.rolling_horizon.window_min=5" in command) == row["mode"].startswith("rolling_")
    assert build_extension([source, *added], 5.0) == []


def test_extension_timing_adds_prior_active_session_without_idle_gap(tmp_path: Path) -> None:
    import os

    for name, timestamp in (("evaluation_preflight.json", 2000), ("evaluation_status.csv", 2100)):
        path = tmp_path / name
        path.touch()
        os.utime(path, (timestamp, timestamp))
    (tmp_path / "evaluation_extension.json").write_text(
        json.dumps({"original_evaluation_wall_sec": 300}), encoding="utf-8"
    )
    assert _experiment_timing(tmp_path, parallel_jobs=5)["run_phase_wall_sec"] == 400


def test_paper_config_has_pre_registered_factorial_design() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    assert cfg.worker_counts == [2, 3, 4, 5, 6]
    assert cfg.training_replicates == 1
    assert cfg.policies == ["immediate_shared", "random_feasible_dispatch", "simulation_based_adp"]
    assert len(cfg.test_seeds) == 20
    assert cfg.screening_iterations == [0, 15, 30, 45, 60, 75]
    assert cfg.screening_seed_count == 10
    assert cfg.final_candidate_count == 2
    assert cfg.final_selection_seed_count == 10
    assert expected_counts(cfg) == {
        "adp_training_runs": 5,
        "baseline_evaluation_runs": 200,
        "adp_evaluation_runs": 100,
        "evaluation_runs": 300,
    }


def test_checkpoint_manifest_comparison_normalizes_json_object_keys() -> None:
    checkpoint_manifest = {"training": {"epsilon_schedule": {"points": {0: 0.5, 75: 0.05}}}}
    json_manifest = {"training": {"epsilon_schedule": {"points": {"0": 0.5, "75": 0.05}}}}
    assert checkpoint_manifest != json_manifest
    assert _json_canonical(checkpoint_manifest) == _json_canonical(json_manifest)


def test_repair_fairness_uses_precision_shared_with_archived_signatures() -> None:
    archived = _normalize_legacy_repair_samples({"S1M1": [17.043, 22.577]})
    current = _normalize_legacy_repair_samples({"S1M1": [17.042644, 17.042644, 22.576842]})
    assert archived == current
    changed = _normalize_legacy_repair_samples({"S1M1": [17.042644, 23.576842]})
    assert changed != archived


def test_standard_dashboard_adapter_uses_confirmatory_design() -> None:
    cfg = _dashboard_config(
        {
            "scenario": "mfg_flow_shop",
            "objective_mode": "maximize_throughput",
            "horizon_days": 5,
            "minutes_per_day": 480,
            "worker_counts": [2, 3, 4, 5, 6],
            "policies": [
                "immediate_shared",
                "random_feasible_dispatch",
                "simulation_based_adp",
            ],
            "test_seeds": [910001, 910002],
        }
    )
    assert cfg.worker_counts == [2, 3, 4, 5, 6]
    assert cfg.seeds == [910001, 910002]
    assert cfg.objective_modes == ["maximize_throughput"]


def test_standard_dashboard_adapter_maps_evaluation_status() -> None:
    row = _compatibility_status(
        {
            "run_id": "run-1",
            "mode": "immediate_shared",
            "worker_count": "3",
            "seed": "910001",
            "run_dir": "C:/tmp/run-1",
            "status": "completed",
            "return_code": "0",
            "elapsed_sec": "12.5",
            "reason": "",
        },
        objective_mode="maximize_throughput",
        scenario="mfg_flow_shop",
        horizon_days=5,
    )
    assert row["objective_mode"] == "maximize_throughput"
    assert row["scenario"] == "mfg_flow_shop"
    assert row["elapsed_sec"] == "12.5"


def test_standard_dashboard_adapter_marks_only_overall_primary_contrast() -> None:
    rows = _add_confirmatory_overall_contrasts(
        [
            {
                "worker_count": 2,
                "baseline_mode": "immediate_shared",
                "primary_comparison": True,
            }
        ],
        [
            {
                "scope": "overall_equal_worker_weight",
                "worker_count": "2-6",
                "baseline_mode": "immediate_shared",
                "seed_count": "20",
                "adp_minus_baseline_mean": "0.64",
                "ci95_low": "-0.51",
                "ci95_high": "1.70",
            }
        ],
        objective_mode="maximize_throughput",
    )
    assert rows[0]["primary_comparison"] is False
    assert rows[1]["primary_comparison"] is True
    assert rows[1]["superiority_demonstrated"] is False


def test_existing_training_preserves_recorded_wall_time(tmp_path: Path) -> None:
    (tmp_path / "training_summary.json").write_text(
        json.dumps({"wall_sec": 123.4567}), encoding="utf-8"
    )
    assert _recorded_wall_sec(tmp_path) == 123.457


def test_paper_seed_partitions_are_disjoint() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    training = {
        seed
        for worker_count in cfg.worker_counts
        for replicate in range(1, cfg.training_replicates + 1)
        for seed in range(
            training_seed(cfg, worker_count, replicate),
            training_seed(cfg, worker_count, replicate) + 800,
        )
    }
    validation = {
        seed
        for worker_count in cfg.worker_counts
        for seed in [*screening_seeds(cfg, worker_count), *final_selection_seeds(cfg, worker_count)]
    }
    assert not training & validation
    assert not training & set(cfg.test_seeds)
    assert not validation & set(cfg.test_seeds)


def test_prepare_writes_five_fleet_specific_training_profiles() -> None:
    cfg = load_config(DEFAULT_CONFIG)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        plan = prepare(cfg, root)
        rows = list(csv.DictReader((root / "training_plan.csv").open(encoding="utf-8")))
        saved = json.loads((root / "experiment_plan.json").read_text(encoding="utf-8"))
        assert len(rows) == 5
        assert plan["counts"]["evaluation_runs"] == 300
        assert saved["test_seeds"] == cfg.test_seeds
        assert (root / "training_commands.ps1").is_file()
        assert (root / "evaluation_commands.ps1").is_file()
        evaluation_rows = list(csv.DictReader((root / "evaluation_plan.csv").open(encoding="utf-8")))
        assert len(evaluation_rows) == 300
        assert sum(row["mode"] == "simulation_based_adp" for row in evaluation_rows) == 100
        assert len({row["run_id"] for row in evaluation_rows}) == 300
        assert len({row["run_dir"] for row in evaluation_rows}) == 300
        adp_row = next(row for row in evaluation_rows if row["mode"] == "simulation_based_adp")
        command = json.loads(adp_row["command_json"])
        assert "runtime.ui.export_replay_artifacts=false" in command
        assert "runtime.artifacts.export_events=false" in command
        assert f"scenario.factory.num_workers={adp_row['worker_count']}" in command
        assert any(value.startswith("decision.adp.checkpoint_path=") for value in command)
        for row in rows:
            payload = yaml.safe_load(Path(row["config_path"]).read_text(encoding="utf-8"))
            assert payload["worker_counts"] == [int(row["worker_count"])]
            assert payload["training"]["policy_iterations"] == 75
            assert payload["training"]["initial_random_episodes"] == 50
            assert payload["training"]["episodes_per_iteration"] == 10
            assert payload["validation"]["screening_iterations"] == [0, 15, 30, 45, 60, 75]
            assert payload["validation"]["screening_seed_count"] == 10
            assert payload["validation"]["final_candidate_count"] == 2
            assert payload["validation"]["final_selection_seed_count"] == 10


def test_completion_check_rejects_partial_or_wrong_horizon_kpi() -> None:
    command = ["scenario.horizon.num_days=5", "scenario.horizon.minutes_per_day=480"]
    with tempfile.TemporaryDirectory() as directory:
        kpi = Path(directory) / "kpi.json"
        kpi.write_text(json.dumps({"objective_status": "complete", "sim_elapsed_min": 2400}), encoding="utf-8")
        assert _completed_kpi_is_valid(kpi, command)
        kpi.write_text(json.dumps({"objective_status": "complete", "sim_elapsed_min": 2399}), encoding="utf-8")
        assert not _completed_kpi_is_valid(kpi, command)
        kpi.write_text(json.dumps({"objective_status": "failed", "sim_elapsed_min": 2400}), encoding="utf-8")
        assert not _completed_kpi_is_valid(kpi, command)


def test_evaluation_timing_survives_status_rewrites_and_keeps_actual_jobs(tmp_path: Path) -> None:
    _record_evaluation_timing(tmp_path, {"run_phase_wall_sec": 100}, jobs=2,
                              started_at="2026-09-26T00:00:00+00:00", elapsed=20, status="finished")
    (tmp_path / "evaluation_status.csv").write_text("rewritten")
    first = _experiment_timing(tmp_path, parallel_jobs=5)
    assert first["run_phase_wall_sec"] == 120
    assert first["parallel_jobs"] == 2
    _record_evaluation_timing(tmp_path, first, jobs=3, started_at="2026-09-27T00:00:00+00:00",
                              elapsed=30, status="finished")
    second = _experiment_timing(tmp_path, parallel_jobs=5)
    assert second["run_phase_wall_sec"] == 150
    assert second["parallel_jobs"] == 3


def test_completion_check_rejects_wrong_run_identity_or_worker_count() -> None:
    command = [
        "scenario=mfg_flow_shop", "decision=immediate_shared", "seed=910001",
        "scenario.factory.num_workers=3", "scenario.objective.mode=maximize_throughput",
        "scenario.horizon.num_days=5", "scenario.horizon.minutes_per_day=480",
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        kpi = root / "kpi.json"
        payload = {
            "objective_status": "complete", "sim_elapsed_min": 2400,
            "run_meta": {
                "machine_lifecycle_contract": "exclusive_pm_v1",
                "scenario_type": "mfg_flow_shop", "decision_mode": "immediate_shared",
                "seed": 910001, "objective_mode": "maximize_throughput",
            },
        }
        kpi.write_text(json.dumps(payload), encoding="utf-8")
        (root / "pre_run_diagnostics.json").write_text(
            json.dumps({"inputs": {"worker_ids": ["A1", "A2", "A3"]}}), encoding="utf-8"
        )
        assert _completed_kpi_is_valid(kpi, command)
        del payload["run_meta"]["machine_lifecycle_contract"]
        kpi.write_text(json.dumps(payload), encoding="utf-8")
        assert not _completed_kpi_is_valid(kpi, command)
        payload["run_meta"]["machine_lifecycle_contract"] = "exclusive_pm_v1"
        payload["run_meta"]["seed"] = 910002
        kpi.write_text(json.dumps(payload), encoding="utf-8")
        assert not _completed_kpi_is_valid(kpi, command)
        payload["run_meta"]["seed"] = 910001
        kpi.write_text(json.dumps(payload), encoding="utf-8")
        (root / "pre_run_diagnostics.json").write_text(
            json.dumps({"inputs": {"worker_ids": ["A1", "A2"]}}), encoding="utf-8"
        )
        assert not _completed_kpi_is_valid(kpi, command)


def test_incomplete_outputs_are_archived_without_deletion() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        training = root / "replicate_1"
        training.mkdir()
        (training / "partial.txt").write_text("training", encoding="utf-8")
        archived_training = archive_training(training)
        assert archived_training is not None and (archived_training / "partial.txt").is_file()
        assert not training.exists()

        evaluation = root / "seed_1"
        evaluation.mkdir()
        (evaluation / "partial.txt").write_text("evaluation", encoding="utf-8")
        archived_evaluation = archive_evaluation(evaluation)
        assert archived_evaluation is not None and (archived_evaluation / "partial.txt").is_file()
        assert not evaluation.exists()


@pytest.mark.parametrize("allow_extension", [False, True])
def test_paper_evaluation_preflight_groups_adp_seeds_by_checkpoint_and_worker(allow_extension) -> None:
    calls: list[tuple[Path, list[int], list[int], bool]] = []

    def validator(path: Path, seeds: list[int], workers: list[int], *, allow_undeclared: bool):
        calls.append((path, seeds, workers, allow_undeclared))
        return {
            "checkpoint": str(path),
            "status": "pass",
            "return_estimator": "n_step_td",
            "horizon_days": 5,
            "n_step": 30,
            "target_tau": 0.03,
            "environment_fingerprints_by_worker_count": {"3": "same-environment"},
        }

    rows = [
        {
            "run_id": "adp-a",
            "mode": "simulation_based_adp",
            "worker_count": "3",
            "seed": "910002",
            "checkpoint_path": "checkpoint_a.pt",
        },
        {
            "run_id": "adp-b",
            "mode": "simulation_based_adp",
            "worker_count": "3",
            "seed": "910001",
            "checkpoint_path": "checkpoint_a.pt",
        },
        {
            "run_id": "baseline",
            "mode": "immediate_shared",
            "worker_count": "3",
            "seed": "910001",
            "checkpoint_path": "",
        },
    ]
    reports = _preflight_adp_checkpoints(rows, validator=validator, allow_undeclared=allow_extension)
    assert len(reports) == 1
    assert calls == [(
        Path("checkpoint_a.pt").resolve(), [910001, 910002], [3], allow_extension
    )]


def test_paper_summary_uses_seed_units_and_adp_training_replicates_hierarchically() -> None:
    rows: list[dict[str, object]] = []
    for seed, baseline in ((1, 10.0), (2, 20.0)):
        rows.append({
            "mode": "immediate_shared", "worker_count": 3, "seed": seed,
            "training_replicate": "", "total_products": baseline,
        })
        for replicate, offset in ((1, 1.0), (2, 2.0), (3, 3.0)):
            rows.append({
                "mode": "simulation_based_adp", "worker_count": 3, "seed": seed,
                "training_replicate": replicate, "total_products": baseline + offset,
            })

    summary = policy_worker_summary(rows, repetitions=100)
    adp = next(
        row for row in summary
        if row["metric"] == "total_products" and row["mode"] == "simulation_based_adp"
    )
    assert adp["environment_seed_count"] == 2
    assert adp["training_replicate_count"] == 3
    assert adp["mean"] == 17.0
    assert adp["between_training_replicate_sd"] == 1.0

    contrast = next(
        row for row in paired_contrasts(rows, repetitions=100)
        if row["scope"] == "worker" and row["baseline_mode"] == "immediate_shared"
    )
    assert contrast["seed_count"] == 2
    assert contrast["training_replicate_count"] == 3
    assert contrast["adp_minus_baseline_mean"] == 2.0
    assert contrast["win_count"] == 2


def test_paper_artifact_audit_skips_intentionally_disabled_replay(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    class Completed:
        returncode = 0

    def fake_run(command, **_kwargs):
        commands.append([str(value) for value in command])
        return Completed()

    monkeypatch.setattr("experiments.mfg_flow_shop_paper.run_evaluation.subprocess.run", fake_run)
    assert _audit_run(tmp_path) == ("pass", "pass")
    artifact_command = next(command for command in commands if "audit_run_artifacts.py" in command[1])
    assert "--skip-replay-log" in artifact_command


def test_paper_fairness_accepts_different_observation_lengths_but_rejects_rng_mismatch() -> None:
    assert _observed_prefixes_consistent([["P", "F", "P"], ["P", "F"], []])
    assert not _observed_prefixes_consistent([["P", "F"], ["P", "P"]])
    assert not _observed_prefixes_consistent([["P"], ["P", "F"], ["P", "P"]])


def test_paper_restock_fairness_accepts_evidenced_full_shelf_noop(tmp_path: Path) -> None:
    meta = {"throughput_restock_interval_days": 1, "minutes_per_day": 480,
            "throughput_restock_target_fill": 30}
    observed = [0, 480, 1440, 1920]
    (tmp_path / "minute_snapshots.json").write_text(json.dumps({"snapshots": [
        {"t": 960, "warehouse_material_shelf_count": 30, "warehouse_material_shelf_capacity": 30},
    ]}), encoding="utf-8")
    evidence = _restock_schedule_evidence(tmp_path, meta, observed, 2400)
    assert evidence["restock_schedule_valid"] is True
    assert json.loads(evidence["restock_noop_times_json"]) == [960]
    rows = []
    for mode, times in (("random_feasible_dispatch", observed),
                        ("immediate_shared", [0, 480, 960, 1440, 1920])):
        rows.append({
            "mode": mode, "worker_count": 2, "seed": 910034, "training_replicate": "",
            "environment_fingerprint": "env", "timing_profile_fingerprint": "timing",
            "stochastic_streams_fingerprint": "rng", "quality_rng_prefix_json": "[]",
            "repair_rng_prefix_json": "{}", "restock_times_json": json.dumps(times),
            **_restock_schedule_evidence(tmp_path, meta, times, 2400),
        })
    report, errors = fairness_report(rows, policies=[row["mode"] for row in rows], training_replicates=1)
    assert errors == []
    assert report[0]["fairness_pass"] is True
    assert report[0]["restock_positive_event_times_identical"] is False
    assert report[0]["restock_verified_noop_count"] == 1
    rows[0]["restock_schedule_valid"] = False
    assert fairness_report(rows, policies=[row["mode"] for row in rows], training_replicates=1)[1]


@pytest.mark.parametrize("count,timestamp", [(29, 960), (31, 960), (30, 959), (None, 960)])
def test_paper_restock_audit_requires_boundary_evidence(tmp_path: Path, count, timestamp) -> None:
    meta = {"throughput_restock_interval_days": 1, "minutes_per_day": 480,
            "throughput_restock_target_fill": 30}
    (tmp_path / "minute_snapshots.json").write_text(json.dumps({"snapshots": [
        {"t": timestamp, "warehouse_material_shelf_count": count, "warehouse_material_shelf_capacity": 30},
    ]}), encoding="utf-8")
    result = _restock_schedule_evidence(tmp_path, meta, [0, 480, 1440, 1920], 2400)
    assert result["restock_schedule_valid"] is False


@pytest.mark.parametrize("times", [[0, 480, 480], [0, 481], [480, 0], [0, 480, 960]])
def test_paper_restock_audit_rejects_invalid_events(tmp_path: Path, times) -> None:
    meta = {"throughput_restock_interval_days": 1, "minutes_per_day": 480,
            "throughput_restock_target_fill": 30}
    assert not _restock_schedule_evidence(tmp_path, meta, times, 960)["restock_schedule_valid"]


def test_paper_restock_audit_honors_non_daily_interval(tmp_path: Path) -> None:
    meta = {"throughput_restock_interval_days": 2, "minutes_per_day": 480,
            "throughput_restock_target_fill": 30}
    result = _restock_schedule_evidence(tmp_path, meta, [0, 960, 1920], 2400)
    assert result["restock_schedule_valid"] is True
    assert json.loads(result["restock_schedule_json"]) == [0, 960, 1920]


def test_common_seed_blocks_preserve_opposite_fleet_effects() -> None:
    rows = []
    for worker, sign in ((2, 1), (3, -1)):
        for seed, effect in ((1, -8), (2, 0), (3, 8)):
            for mode, value in (("immediate_shared", 20), ("simulation_based_adp", 20 + sign * effect)):
                rows.append({"mode": mode, "worker_count": worker, "seed": seed,
                             "training_replicate": 1 if mode == "simulation_based_adp" else "",
                             "total_products": value})
    overall = next(row for row in paired_contrasts(rows, repetitions=500) if row["scope"] == "overall_equal_worker_weight")
    assert overall["adp_minus_baseline_mean"] == 0
    assert overall["ci95_low"] == overall["ci95_high"] == 0
    assert overall["bootstrap_two_sided_p"] == 1
    adp = next(row for row in policy_worker_summary(rows, repetitions=100) if row["mode"] == "simulation_based_adp")
    assert adp["between_training_replicate_sd"] == ""


def test_centered_bootstrap_p_never_claims_zero_monte_carlo_probability() -> None:
    assert _centered_bootstrap_p([10.0] * 99, 10.0) == 0.01
    assert _centered_bootstrap_p([0.0] * 99, 0.0) == 1


def test_plan_audit_detects_missing_groups_and_duplicate_identities() -> None:
    plan = [{"mode": "immediate_shared", "worker_count": "3", "seed": "1",
             "run_id": "x", "run_dir": "x", "training_replicate": ""}]
    experiment = {"policies": ["immediate_shared"], "worker_counts": [3], "test_seeds": [1, 2], "training_replicates": 1}
    errors = _plan_errors(plan * 2, [{"run_id": "x"}] * 2, experiment)
    assert any("missing planned combination" in error for error in errors)
    assert any("duplicate planned combination" in error for error in errors)
    assert any("duplicate status ID" in error for error in errors)


def test_identity_audit_rejects_mislabeled_results() -> None:
    source = {"mode": "immediate_shared", "worker_count": "3", "seed": "1"}
    experiment = {"scenario": "mfg_flow_shop", "objective_mode": "maximize_throughput", "minutes_per_day": 480}
    meta = {"scenario_type": "mfg_flow_shop", "decision_mode": "immediate_shared", "seed": 1,
            "objective_mode": "maximize_throughput", "minutes_per_day": 480}
    kpi = {"run_meta": dict(meta), "termination_reason": "completed_horizon"}
    diagnostics = {"inputs": {"worker_ids": ["A1", "A2", "A3"]}}
    assert not _identity_errors(source, meta, kpi, diagnostics, experiment)
    kpi["run_meta"]["seed"] = 2
    assert _identity_errors(source, meta, kpi, diagnostics, experiment)


def test_paper_dashboard_uses_registered_worker_intervals() -> None:
    rows = _add_confirmatory_overall_contrasts(
        [{"metric": "total_products", "worker_count": 3, "baseline_mode": "immediate_shared", "ci95_low": -123}],
        [{"scope": "worker", "worker_count": "3", "baseline_mode": "immediate_shared",
          "adp_minus_baseline_mean": "2.95", "ci95_low": "2.3", "ci95_high": "3.7"}],
        objective_mode="maximize_throughput")
    assert rows[0]["ci95_low"] == "2.3"
    assert rows[0]["superiority_demonstrated"] is True


def test_repair_fairness_signature_records_each_failure_once() -> None:
    events = [
        {
            "type": "MACHINE_BROKEN",
            "entity_id": "S1M1",
            "details": {"sampled_repair_time_min": 20.25},
        },
        {
            "type": "MACHINE_REPAIR_START",
            "entity_id": "S1M1",
            "details": {"repair_total_min": 20.25},
        },
        {
            "type": "MACHINE_REPAIR_START",
            "entity_id": "S1M1",
            "details": {"repair_total_min": 20.25},
        },
    ]
    assert _event_audit_signature(events)["repair_samples"] == {"S1M1": [20.25]}


def test_legacy_repair_signature_collapses_collaborator_start_duplicates() -> None:
    assert _normalize_legacy_repair_samples(
        {"S1M2": [20.236, 20.236, 20.164], "S2M1": [17.285]}
    ) == {"S1M2": [20.236, 20.164], "S2M1": [17.285]}


def test_paper_preflight_rejects_training_replicates_from_different_environments() -> None:
    rows = [
        {
            "run_id": f"replicate-{replicate}",
            "mode": "simulation_based_adp",
            "worker_count": "3",
            "seed": "910001",
            "checkpoint_path": f"checkpoint_{replicate}.pt",
        }
        for replicate in (1, 2)
    ]

    def validator(path: Path, _seeds, _workers, *, allow_undeclared: bool):
        assert not allow_undeclared
        return {
            "checkpoint": str(path),
            "return_estimator": "n_step_td",
            "horizon_days": 5,
            "n_step": 30,
            "target_tau": 0.03,
            "environment_fingerprints_by_worker_count": {"3": path.stem},
        }

    with pytest.raises(RuntimeError, match="different environments"):
        _preflight_adp_checkpoints(rows, validator=validator)
