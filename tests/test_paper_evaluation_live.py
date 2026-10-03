from __future__ import annotations

from argparse import Namespace
import csv
import json
from pathlib import Path

import pytest

from experiments.mfg_flow_shop_paper.evaluation_live import EvaluationMonitor, render_monitor
from experiments.mfg_flow_shop_paper.extend_seeds import build_seed_extension, mark_pending
from experiments.mfg_flow_shop_paper.extend_evaluation import build_extension
from experiments.mfg_flow_shop_paper.prepare_experiment import evaluation_plan_rows, load_config
from experiments.mfg_flow_shop_paper import run_evaluation as runner


def test_extend_20_to_100_seeds_preserves_original_rows_and_checkpoints(tmp_path):
    cfg = load_config()
    original = evaluation_plan_rows(cfg, tmp_path)
    original.extend(build_extension(original, 5))
    before = json.dumps(original, sort_keys=True)
    added = build_seed_extension(original, list(range(910001, 910101)))
    assert len(original) == 600 and len(added) == 2400
    assert json.dumps(original, sort_keys=True) == before
    all_rows = original + added
    assert len({r['run_id'] for r in all_rows}) == 3000
    assert len({r['run_dir'] for r in all_rows}) == 3000
    for row in added:
        assert 910021 <= int(row['seed']) <= 910100
        cmd = json.loads(row['command_json'])
        assert f"seed={row['seed']}" in cmd
        assert f"hydra.run.dir={Path(row['run_dir']).as_posix()}" in cmd
        source = next(r for r in original if r['mode'] == row['mode'] and r['worker_count'] == row['worker_count'])
        assert row['checkpoint_path'] == source['checkpoint_path']
    assert not build_seed_extension(all_rows, list(range(910001, 910101)))
    with pytest.raises(ValueError, match='Duplicate'):
        build_seed_extension(original, [910001, 910001])


def test_expanded_comparison_does_not_present_old_summary_as_final(tmp_path):
    backup = tmp_path / 'backup'
    backup.mkdir()
    original = '{"status":"pass","eligible_run_count":600}'
    (backup / 'analysis_summary.json').write_text(original)
    mark_pending(tmp_path, {'reused_run_count': 600, 'added_run_count': 2400, 'backup_path': str(backup)})
    summary = json.loads((tmp_path / 'analysis_summary.json').read_text())
    assert summary['status'] == 'pending' and summary['expected_run_count'] == 3000
    assert (backup / 'analysis_summary.json').read_text() == original
    assert 'url=../live_evaluation.html' in (tmp_path / 'policy_comparison_dashboard/comparison_dashboard.html').read_text()


def _fixture(tmp_path):
    rows = [{'run_id': f'run-{s}', 'mode': 'immediate_shared', 'worker_count': '3', 'seed': str(s),
             'training_replicate': '', 'checkpoint_path': '', 'run_dir': str(tmp_path / f'run-{s}'),
             'command_json': '["python", "main.py"]'} for s in (1, 2, 3)]
    plan = {'worker_counts': [3], 'policies': ['immediate_shared'], 'test_seeds': [1, 2, 3], 'horizon_days': 5}
    (tmp_path / 'experiment_plan.json').write_text(json.dumps(plan))
    done = {**rows[0], 'status': 'completed', 'return_code': 0, 'elapsed_sec': 60,
            'artifact_audit': 'pass', 'kpi_audit': 'pass', 'reason': ''}
    return rows, done


def test_monitor_counts_reuse_running_audit_failures_and_terminal_state(tmp_path):
    rows, done = _fixture(tmp_path)
    live = EvaluationMonitor(tmp_path, rows, [done], 2)
    live.set_phase('running')
    live.execute(lambda row: None, rows[1])
    snap = live.snapshot()
    assert (snap['completed'], snap['running'], snap['pending']) == (1, 1, 1)
    assert snap['reused'] == 1 and snap['new_completed'] == 0
    assert snap['eta_sec'] == 60
    live.finish_run({**rows[1], 'status': 'audit_failed', 'reason': '<bad & audit>',
                     'artifact_audit': 'fail', 'kpi_audit': 'pass'})
    snap = live.snapshot()
    assert (snap['completed'], snap['failed'], snap['running'], snap['pending']) == (1, 1, 0, 1)
    doc = render_monitor(tmp_path, snap)
    assert '&lt;bad &amp; audit&gt;' in doc
    assert 'http-equiv="refresh"' in doc
    assert 'Open final policy comparison' not in doc
    live.set_phase('failed', 'One run failed')
    assert 'http-equiv="refresh"' not in (tmp_path / 'live_evaluation.html').read_text()


def test_monitor_never_counts_simulation_done_before_audits(tmp_path):
    rows, done = _fixture(tmp_path)
    live = EvaluationMonitor(tmp_path, rows, [done], 2)
    live.execute(lambda row: None, rows[1])
    run_dir = Path(rows[1]['run_dir'])
    run_dir.mkdir()
    (run_dir / 'progress.json').write_text(json.dumps({'status': 'completed', 'progress_percent': 100}))
    snap = live.snapshot()
    assert snap['completed'] == 1
    assert 'Artifacts / audit' in render_monitor(tmp_path, snap)


def test_pending_only_does_not_rerun_audited_reused_results(tmp_path, monkeypatch):
    rows, done = _fixture(tmp_path)
    monkeypatch.setattr(runner, '_completed_kpi_is_valid', lambda *args: True)
    assert runner._pending_rows(rows, {done['run_id']: done}) == rows[1:]
    invalid = {**done, 'artifact_audit': 'fail'}
    assert runner._pending_rows(rows, {done['run_id']: invalid}) == rows


def test_evaluation_lock_prevents_duplicate_jobs_and_releases(tmp_path):
    with runner._evaluation_lock(tmp_path):
        with pytest.raises(RuntimeError, match='already owns'):
            with runner._evaluation_lock(tmp_path):
                pass
    with runner._evaluation_lock(tmp_path):
        pass


@pytest.mark.parametrize('fail', [False, True])
def test_runner_updates_live_monitor_and_only_publishes_verified_final(tmp_path, monkeypatch, fail):
    rows, _ = _fixture(tmp_path)
    with (tmp_path / 'evaluation_plan.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    args = Namespace(prepared=tmp_path, jobs=2, force=False, pending_only=True,
                     modes=None, limit=None, dry_run=False, no_live=False,
                     no_open_dashboard=True, skip_audit=False, no_compress=True,
                     allow_undeclared_held_out_seeds=True)
    monkeypatch.setattr(runner, 'parse_args', lambda: args)
    monkeypatch.setattr(runner, '_preflight_adp_checkpoints', lambda *a, **kw: [])
    def run(row, *_):
        if fail and row['seed'] == '2':
            raise RuntimeError('fixture failure')
        return {**{k: v for k, v in row.items() if k != 'command_json'},
                'status': 'completed', 'return_code': 0, 'elapsed_sec': 1,
                'artifact_audit': 'pass', 'kpi_audit': 'pass', 'ntfs_compressed': False, 'reason': ''}
    monkeypatch.setattr(runner, '_run', run)
    monkeypatch.setattr(runner, 'summarize', lambda _: {'status': 'pass'})
    rendered = []
    monkeypatch.setattr(runner, 'render_dashboard', lambda root: rendered.append(root) or root / 'comparison_dashboard.html')
    assert runner.main() == (1 if fail else 0)
    snap = json.loads((tmp_path / 'evaluation_progress.json').read_text())
    assert snap['phase'] == ('failed' if fail else 'completed')
    assert snap['running'] == 0
    assert snap['completed'] == (2 if fail else 3)
    assert len(rendered) == (0 if fail else 1)
    assert ('Open final policy comparison' in (tmp_path / 'live_evaluation.html').read_text()) is not fail


def test_live_final_fairness_failure_is_visible(tmp_path, monkeypatch):
    rows, done = _fixture(tmp_path)
    live = EvaluationMonitor(tmp_path, rows, [done], 1)
    live.set_phase('postprocessing')
    assert 'Open final policy comparison' not in render_monitor(tmp_path, live.snapshot())
    live.set_phase('failed', 'Final fairness/statistical audit failed')
    assert 'Final fairness/statistical audit failed' in (tmp_path / 'live_evaluation.html').read_text()
