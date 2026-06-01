from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


ScenarioRun = Callable[..., dict[str, Any]]


_ALIASES = {
    "mfg_basic": "factory_mfg_basic",
    "manufacturing": "factory_mfg_basic",
    "factory": "factory_mfg_basic",
    "factory_mfg_basic": "factory_mfg_basic",
    "shipyard": "shipyard_basic",
    "shipyard_basic": "shipyard_basic",
}


def scenario_type(experiment_cfg: dict[str, Any]) -> str:
    raw = str(experiment_cfg.get("type") or experiment_cfg.get("scenario_type") or experiment_cfg.get("name") or "mfg_basic")
    return _ALIASES.get(raw.strip().lower(), raw.strip().lower())


def _runner(kind: str) -> ScenarioRun:
    if kind == "factory_mfg_basic":
        from manufacturing_sim.simulation.scenarios.manufacturing.run import run

        return run
    if kind == "shipyard_basic":
        from manufacturing_sim.simulation.scenarios.shipyard.run import run

        return run
    supported = ", ".join(sorted(_ALIASES))
    raise ValueError(f"Unsupported scenario.type={kind!r}. Supported scenario aliases: {supported}")


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
