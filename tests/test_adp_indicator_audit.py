import math

import pytest
import simpy

from manufacturing_sim.adp.train import (
    _compose_episode_cfg, _episode_metric_row, EpisodeResult, InMemoryEventLogger,
    build_decision_module, ManufacturingWorld,
)
from scripts.audit_adp_training import audit_metric_aggregates, audit_training, _nonfinite_metric_errors


def test_finite_audit_does_not_parse_snapshot_digest_as_scientific_notation():
    assert _nonfinite_metric_errors(
        [{"snapshot_hash": "70e4813438655286", "products": "42"}], "episodes"
    ) == []


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e999"])
def test_finite_audit_still_rejects_invalid_numeric_metrics(value):
    errors = _nonfinite_metric_errors(
        [{"snapshot_hash": "70e4813438655286", "products": value}], "episodes"
    )
    assert errors == [f"episodes row 0: non-finite products={value}"]


@pytest.mark.parametrize("wait_enabled", [False, True])
def test_empty_candidates_are_counted_independently_of_wait(wait_enabled, monkeypatch):
    cfg = _compose_episode_cfg(worker_count=2, seed=2026, days=1,
                               adp_cfg={"force_random_policy": True, "allow_wait_action": wait_enabled})
    env, logger = simpy.Environment(), InMemoryEventLogger()
    world = ManufacturingWorld(env, cfg, logger, build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"))
    coordinator = world.adp_coordinator
    monkeypatch.setattr(coordinator, '_candidate_map', lambda workers: ({w.agent_id: {} for w in workers}, {}))
    coordinator._decide(['audit'])
    metrics = coordinator.summary()
    assert metrics['no_candidate_unassigned_count'] == 2
    assert metrics['joint_no_candidate_count'] == 1
    assert metrics['no_candidate_unassigned_ratio'] == 1.
    assert metrics['unassigned_count'] == 2
    assert metrics['wait_count'] == (2 if wait_enabled else 0)
    assert metrics['joint_all_wait_count'] == (1 if wait_enabled else 0)
    assert coordinator.previous_action == {'A1': None, 'A2': None}


def test_forced_idle_ratio_uses_all_worker_decisions_not_only_assigned_tasks():
    result = EpisodeResult(1, 'policy_iteration', 2, 2026, 1, 0, 1., 10, {},
                           assigned_task_count=2, unassigned_count=18,
                           no_candidate_unassigned_count=18, idle_metrics_version=2)
    row = _episode_metric_row(result)
    assert row['no_candidate_unassigned_ratio'] == .9
    assert row['worker_wait_ratio'] == 0.


def records():
    episodes = []
    for phase in ['policy_iteration', 'screening_validation', 'final_selection_validation']:
        for index, products in enumerate([10, 12]):
            episodes.append(dict(phase=phase, iteration=1, products=products, wave_id=phase,
                                 snapshot_hash='fixed', elapsed_sec=1.,
                                 greedy_mc_sample_count=2,
                                 greedy_mc_squared_error_sum=10., greedy_mc_error_sum=2.,
                                 greedy_mc_prediction_sum=8., greedy_mc_target_sum=6.))
    std = math.sqrt(2.)
    iterations = [dict(iteration=1, rollout_products_mean=11., rollout_products_std=std, batch_episode_count=2,
                       validation_products_mean=11., validation_products_std=std, validation_episode_count=2,
                       greedy_mc_sample_count=4, greedy_mc_rmse=math.sqrt(5.), greedy_mc_bias=1.,
                       greedy_mc_prediction_mean=4., greedy_mc_target_mean=3.,
                       final_validation_products_mean=11., final_validation_products_std=std)]
    waves = [dict(wave_id=phase, phase=phase, iteration=1, snapshot_hash='fixed', episode_count=2,
                  episode_elapsed_sum_sec=2.) for phase in ['policy_iteration', 'screening_validation', 'final_selection_validation']]
    summary = dict(best_iteration=1, best_validation_completed_products_avg=11., best_validation_completed_products_std=std)
    selection = [dict(iteration=1, screening_mean=11., screening_std=std, final_mean=11., final_std=std)]
    return episodes, iterations, waves, summary, selection


def test_aggregate_audit_passes_recomputed_statistics():
    assert audit_metric_aggregates(*records()) == []


@pytest.mark.parametrize('field', ['rollout_products_mean', 'rollout_products_std', 'validation_products_std',
                                  'greedy_mc_rmse', 'greedy_mc_bias', 'greedy_mc_sample_count',
                                  'final_validation_products_mean'])
def test_aggregate_audit_rejects_corrupted_indicators(field):
    values = records()
    values[1][0][field] += 1.
    assert any(field in error for error in audit_metric_aggregates(*values))


@pytest.mark.parametrize('fault', ['best', 'wave_hash', 'wave_duration', 'selection'])
def test_aggregate_audit_rejects_inconsistent_sources(fault):
    values = records()
    if fault == 'best':
        values[3]['best_iteration'] = 0
    elif fault == 'wave_hash':
        values[0][0]['snapshot_hash'] = 'different'
    elif fault == 'wave_duration':
        values[2][0]['episode_elapsed_sum_sec'] = 99.
    else:
        values[4][0]['final_mean'] = 99.
    assert audit_metric_aggregates(*values)


@pytest.mark.parametrize('csv_file', ['episode_metrics.csv', 'iteration_metrics.csv', 'wave_metrics.csv', 'checkpoint_selection.csv'])
def test_audit_rejects_nonfinite_csv_before_nan_can_bypass_comparisons(tmp_path, csv_file):
    for name in ['resolved_config.yaml', 'training_summary.json', 'checkpoint_manifest.json']:
        (tmp_path / name).write_text('{}', encoding='utf-8')
    for name in ['best.pt', 'last.pt', 'training_dashboard.html']:
        (tmp_path / name).write_text('fixture', encoding='utf-8')
    for name in ['episode_metrics.csv', 'iteration_metrics.csv', 'wave_metrics.csv', 'checkpoint_selection.csv']:
        (tmp_path / name).write_text('products\n' + ('nan\n' if name == csv_file else ''), encoding='utf-8')
    result = audit_training(tmp_path, load_checkpoints=False)
    assert result['status'] == 'fail'
    assert any('non-finite' in error for error in result['errors'])
