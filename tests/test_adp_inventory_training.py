from pathlib import Path
from dataclasses import replace
import json
import yaml

import pytest
import simpy

from experiments.mfg_flow_shop_paper.inventory_study import inventory_overrides
from manufacturing_sim.adp.checkpoint import checkpoint_fingerprint, load_checkpoint, save_checkpoint
from manufacturing_sim.adp.model import build_value_network, require_torch
from manufacturing_sim.adp.train import (
    _compose_episode_cfg, _scenario_override_arguments, InMemoryEventLogger,
    ManufacturingWorld, build_decision_module,
)


def world_for(fill, workers=2):
    cfg = _compose_episode_cfg(worker_count=workers, seed=2026, days=5,
        adp_cfg={"_scenario_overrides": inventory_overrides(fill)})
    return ManufacturingWorld(simpy.Environment(), cfg, InMemoryEventLogger(),
        build_decision_module(experiment_cfg=cfg, decision_mode="simulation_based_adp"))


@pytest.mark.parametrize("workers", [2, 3, 4, 5, 6])
def test_inventory_override_reaches_world_without_changing_defaults(workers):
    world = world_for(20, workers)
    assert world.num_workers == workers
    assert world.material_shelf_initial_fill == 20
    assert world.throughput_restock_target_fill == 20
    assert world.material_shelf_capacity == 40
    assert world.cfg["map"]["warehouse_height_tiles"] == 15
    assert world.num_days == 5 and world.minutes_per_day == 480
    assert "_scenario_overrides" not in world.cfg["decision"]["adp"]
    default = _compose_episode_cfg(worker_count=workers, seed=2026, days=5, adp_cfg={})
    assert default["warehouse"]["material_shelf"]["initial_fill"] == 30


@pytest.mark.parametrize("overrides", [[], {"seed": 1}, {"scenario.factory.num_workers": 6},
    {"scenario.objective.mode": "minimize_makespan"}, {"scenario.horizon.num_days": 1},
    {"scenario.map.foo": 1, "+scenario.map.foo": 2}])
def test_invalid_scenario_override_is_rejected(overrides):
    with pytest.raises(ValueError):
        _scenario_override_arguments(overrides)


def test_checkpoint_supply_contract_rejects_other_fill(tmp_path: Path):
    torch = require_torch()
    torch.set_num_threads(1)
    world = world_for(20)
    model = build_value_network(embedding_dim=16, heads=2, layers=1)
    manifest = {**checkpoint_fingerprint(world, return_estimator="n_step_td"),
                "return_estimator": "n_step_td",
                "supported_worker_counts": [2], "model": {"embedding_dim": 16, "heads": 2, "layers": 1}}
    path = tmp_path / "best.pt"
    save_checkpoint(path, model=model, optimizer=None, manifest=manifest)
    load_checkpoint(path, world=world, device="cpu")
    with pytest.raises(RuntimeError, match="material_supply_contract"):
        load_checkpoint(path, world=world_for(40), device="cpu")


def test_prepared_study_preserves_budget_and_reuses_baseline_paths(tmp_path, monkeypatch):
    from experiments.mfg_flow_shop_paper import train_inventory as runner
    from experiments.mfg_flow_shop_paper.prepare_experiment import load_config, evaluation_plan_rows
    from manufacturing_sim.adp.td_train import validate_td_config
    cfg = replace(load_config(), test_seeds=list(range(910001, 910101)), training_replicates=0)
    source = tmp_path / "source"
    baselines = evaluation_plan_rows(cfg, source)
    source_plan = {"test_seeds": cfg.test_seeds, "condition_label": "Materials 20"}
    monkeypatch.setattr(runner, "qualify_source", lambda *args: (source_plan, baselines, []))
    monkeypatch.setattr(runner, "source_digest", lambda rows: "test-hash")
    output = tmp_path / "prepared"
    plan = runner.prepare(source, output, 20)
    assert plan["counts"]["reused_baseline_runs"] == 1000
    assert plan["counts"]["new_evaluation_runs"] == 500
    assert plan["counts"]["evaluation_runs"] == 1500
    for row in plan["training_plan"]:
        payload = yaml.safe_load(Path(row["config_path"]).read_text())
        validate_td_config(payload)
        assert payload["worker_counts"] == [row["worker_count"]]
        assert payload["training"]["initial_random_episodes"] == 50
        assert payload["training"]["policy_iterations"] == 75
        assert payload["training"]["replay_sample_episodes_per_update"] == 30
        assert payload["training"]["target_state_batch_size"] == 64
        assert payload["rollout"]["process_count"] == 10
        assert payload["rollout"]["reuse_processes"] is False
        assert payload["scenario_overrides"] == inventory_overrides(20)
        assert payload["seed_partitions"]["held_out_test"] == cfg.test_seeds
    rows = runner._read_rows(output / "evaluation_plan.csv")
    assert [row["run_dir"] for row in rows[:1000]] == [row["run_dir"] for row in baselines]
    for row in rows[1000:]:
        command = json.loads(row["command_json"])
        assert "scenario.warehouse.material_shelf.initial_fill=20" in command
        assert "++scenario.map.warehouse_height_tiles=15" in command


def test_fresh_30_study_trains_five_fleets_and_compares_all_six_policies(tmp_path):
    from experiments.mfg_flow_shop_paper import train_inventory as runner
    from manufacturing_sim.adp.td_train import validate_td_config
    output = tmp_path / "fresh30"
    plan = runner.prepare(None, output, 30, all_policies=True)
    assert plan["baseline_source"] is None
    assert plan["baseline_source_digest"] is None
    assert plan["counts"]["reused_baseline_runs"] == 0
    assert plan["counts"]["new_evaluation_runs"] == 3000
    assert plan["counts"]["evaluation_runs"] == 3000
    assert plan["counts"]["adp_training_runs"] == 5
    assert plan["counts"]["baseline_evaluation_runs"] == 2500
    assert plan["evaluation_min_free_gib"] == 45.0
    assert plan["test_seeds"] == list(range(910001, 910101))
    assert set(plan["policies"]) == set(runner.ALL_POLICIES)
    assert not (output / "evaluation_status.csv").exists()
    for row in plan["training_plan"]:
        payload = yaml.safe_load(Path(row["config_path"]).read_text())
        validate_td_config(payload)
        assert payload["scenario_overrides"] == inventory_overrides(30)
        assert payload["worker_counts"] == [row["worker_count"]]
        assert payload["seed_partitions"]["held_out_test"] == plan["test_seeds"]
        assert payload["validation"]["screening_interval_iterations"] == 5
        assert payload["validation"]["screening_iterations"] == list(range(0, 76, 5))
        assert payload["rollout"]["process_count"] == 10
        assert payload["runtime"]["require_cuda_training"] is True
    rows = runner._read_rows(output / "evaluation_plan.csv")
    assert len(rows) == len({row["run_id"] for row in rows}) == 3000
    assert len({row["run_dir"] for row in rows}) == 3000
    for row in rows:
        assert Path(row["run_dir"]).is_relative_to(output)
        command = json.loads(row["command_json"])
        for key, value in inventory_overrides(30).items():
            assert f"{key}={value}" in command
        assert "runtime.artifacts.export_events=false" in command
        assert "runtime.ui.export_replay_artifacts=false" in command
        if row["mode"].startswith("rolling_horizon"):
            assert "decision.rolling_horizon.window_min=5" in command
    with pytest.raises(FileExistsError):
        runner.prepare(None, output, 30, all_policies=True)


@pytest.mark.parametrize("workers", [2, 3, 4, 5, 6])
def test_default_30_world_uses_40_slots_with_ten_empty(workers):
    cfg = _compose_episode_cfg(worker_count=workers, seed=2026, days=5, adp_cfg={})
    world = ManufacturingWorld(simpy.Environment(), cfg, InMemoryEventLogger(),
        build_decision_module(experiment_cfg=cfg, decision_mode="immediate_shared"))
    world.bootstrap()
    assert world.material_shelf_capacity == 40
    assert world.material_shelf_initial_fill == world.throughput_restock_target_fill == 30
    assert world._material_shelf_count() == 30
    assert world.cfg["map"]["warehouse_height_tiles"] == 15


def test_disk_guard_preserves_training_and_resumes_when_space_available(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from experiments.mfg_flow_shop_paper import train_inventory as runner
    free = iter([11 * 2**30, 46 * 2**30])
    sleeps = []
    monkeypatch.setattr(runner.shutil, "disk_usage", lambda path: SimpleNamespace(free=next(free)))
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    runner.wait_for_evaluation_space(tmp_path, 45.0)
    status = json.loads((tmp_path / "pipeline_status.json").read_text())
    assert status["phase"] == "waiting_for_disk_space"
    assert status["required_free_gib"] == 45.0
    assert sleeps == [60]
