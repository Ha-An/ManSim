from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


ScenarioRun = Callable[..., dict[str, Any]]


_SUPPORTED_SCENARIOS = {
    "factory_mfg_basic": "factory_mfg_basic",
    "mfg_flow_shop": "mfg_flow_shop",
    "shipyard_basic": "shipyard_basic",
}


def scenario_type(experiment_cfg: dict[str, Any]) -> str:
    raw = str(
        experiment_cfg.get("type")
        or experiment_cfg.get("scenario_type")
        or experiment_cfg.get("name")
        or "factory_mfg_basic"
    ).strip().lower()
    return _SUPPORTED_SCENARIOS.get(raw, raw)


def _runner(kind: str) -> ScenarioRun:
    if kind in {"factory_mfg_basic", "mfg_flow_shop"}:
        from manufacturing_sim.simulation.scenarios.manufacturing.run import run

        return run
    if kind == "shipyard_basic":
        from manufacturing_sim.simulation.scenarios.shipyard.run import run

        return run
    supported = ", ".join(sorted(_SUPPORTED_SCENARIOS))
    raise ValueError(f"Unsupported scenario.type={kind!r}. Supported scenarios: {supported}")


def run_scenario(
    experiment_cfg: dict[str, Any],
    logger: Any | None = None,
    decision_modules: Any | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    kind = scenario_type(experiment_cfg)
    experiment_cfg.setdefault("scenario_type", kind)
    return _runner(kind)(
        experiment_cfg=experiment_cfg,
        logger=logger,
        decision_modules=decision_modules,
        output_dir=output_dir,
    )
