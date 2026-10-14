from experiments.factory_policy_comparison.render_dashboard import _runtime_summary_rows
from experiments.mfg_flow_shop_paper.render_standard_dashboard import _compatibility_status


def test_legacy_timing_keeps_wall_time_and_uses_planned_parallel_jobs(tmp_path):
    import json
    from experiments.mfg_flow_shop_paper.render_standard_dashboard import _experiment_timing
    path = tmp_path / "evaluation_timing.json"
    path.write_text(json.dumps({"run_phase_wall_sec": 120.0}), encoding="utf-8")
    timing = _experiment_timing(tmp_path, parallel_jobs=10)
    assert timing["parallel_jobs"] == 10
    assert timing["run_phase_wall_sec"] == 120.0
    path.write_text(json.dumps({"run_phase_wall_sec": 120.0, "parallel_jobs": 2}), encoding="utf-8")
    assert _experiment_timing(tmp_path, parallel_jobs=10)["parallel_jobs"] == 2


def test_imported_baseline_time_is_not_counted_against_new_wall_time():
    sources = [
        {"status": "skipped_existing", "elapsed_sec": "500", "reason": "Reused from C:/source"},
        {"status": "completed", "elapsed_sec": "80"},
        {"status": "completed", "elapsed_sec": "80"},
    ]
    statuses = [_compatibility_status(row, objective_mode="maximize_throughput", scenario="mfg_flow_shop", horizon_days=5)
                for row in sources]
    assert statuses[0]["execution_scope"] == "reused_external"
    _, rows = _runtime_summary_rows(statuses, experiment_wall_sec=100, parallel_jobs=2)
    metrics = {row["metric"]: row["value"] for row in rows}
    assert metrics["Current run time / evaluation wall ratio"] == "1.60x"
    assert metrics["Current runs with elapsed_sec"] == "2"
    assert metrics["Reused baseline runs"] == "1"
    assert "Aggregate compute / wall ratio" not in metrics


def test_resume_from_same_experiment_keeps_original_run_times():
    sources = [{"status": "skipped_existing", "elapsed_sec": "80", "reason": ""},
               {"status": "completed", "elapsed_sec": "80"}]
    _, rows = _runtime_summary_rows(sources, experiment_wall_sec=100, parallel_jobs=2)
    assert {row["metric"]: row["value"] for row in rows}["Aggregate compute / wall ratio"] == "1.60x"


def test_reuse_without_new_execution_does_not_claim_speedup():
    _, rows = _runtime_summary_rows([{"elapsed_sec": "500", "execution_scope": "reused_external"}],
                                   experiment_wall_sec=5, parallel_jobs=10)
    assert not any("ratio" in row["metric"] for row in rows)
    metrics = {row["metric"]: row["value"] for row in rows}
    assert metrics["Average run command time"] == "-"
    assert metrics["Slowest run"] == "-"
