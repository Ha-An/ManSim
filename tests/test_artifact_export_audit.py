import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from scripts import audit_run_artifacts as module


@pytest.mark.parametrize("events", [False, True])
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("retained_replay", [False, True])
def test_replay_audit_matches_export_contract(tmp_path, events, replay, retained_replay):
    (tmp_path / ".hydra").mkdir()
    (tmp_path / ".hydra/config.yaml").write_text(
        json.dumps({"runtime": {"ui": {"export_replay_artifacts": replay}}}), encoding="utf-8")
    (tmp_path / "run_meta.json").write_text(json.dumps({"event_log": {"enabled": events}}), encoding="utf-8")
    if retained_replay:
        (tmp_path / "replay_studio_log.json").write_text("{}", encoding="utf-8")
    expected_replay = replay and (events or retained_replay)
    with ExitStack() as stack:
        checked = {}
        for name in ("check_required_files", "check_replay_log", "check_kpi", "check_layout", "check_gantt",
                     "check_event_log_consistency", "check_spatial_continuity", "check_item_transport_continuity",
                     "check_inspection_task_lifecycle", "check_strict_periodic_rolling_horizon",
                     "check_machine_breakdown_contract"):
            checked[name] = stack.enter_context(patch.object(module, name))
        stack.enter_context(patch.object(module, "_iter_events", return_value=[]))
        module.audit_run(tmp_path)
        assert checked["check_required_files"].call_args.kwargs == {
            "require_replay_log": expected_replay, "require_event_log": events,
        }
        assert checked["check_replay_log"].call_count == int(expected_replay)
        assert checked["check_spatial_continuity"].call_count == int(events)


def test_replay_disabled_must_be_explicit_in_config(tmp_path):
    audit = module.Audit()
    assert module._replay_log_enabled(tmp_path, audit)
    config = tmp_path / ".hydra/config.yaml"
    config.parent.mkdir()
    config.write_text("runtime: [invalid", encoding="utf-8")
    assert module._replay_log_enabled(tmp_path, audit)
    assert audit.errors


def test_cli_skip_replay_remains_an_explicit_override(tmp_path):
    with patch.object(module, "audit_run", return_value=module.Audit()) as audit:
        assert module.main([str(tmp_path), "--skip-replay-log"]) == 0
        assert audit.call_args.kwargs == {"require_replay_log": False}
        assert module.main([str(tmp_path)]) == 0
        assert audit.call_args.kwargs == {"require_replay_log": None}
