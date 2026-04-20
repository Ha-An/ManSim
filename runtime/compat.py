from __future__ import annotations

from typing import Any

from omegaconf import DictConfig, OmegaConf


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, DictConfig):
        payload = OmegaConf.to_container(value, resolve=True)
        return payload if isinstance(payload, dict) else {}
    if isinstance(value, dict):
        return value
    return {}


def build_legacy_experiment_cfg(cfg: DictConfig) -> dict[str, Any]:
    scenario_cfg = _to_dict(cfg.get("scenario", {}))
    decision_cfg = _to_dict(cfg.get("decision", {}))
    heuristic_cfg = _to_dict(cfg.get("heuristic_rules", {}))
    worker_cfg = _to_dict(cfg.get("worker", {}))

    experiment_cfg = dict(scenario_cfg)
    global_seed = cfg.get("seed", None)
    if global_seed is not None:
        experiment_cfg["seed"] = int(global_seed)

    if str(decision_cfg.get("mode", "")).strip() == "llm_planner":
        llm_cfg = decision_cfg.setdefault("llm", {})
        orchestration_cfg = llm_cfg.setdefault("orchestration", {}) if isinstance(llm_cfg, dict) else {}
        if isinstance(orchestration_cfg, dict):
            incident_cfg = orchestration_cfg.setdefault("incident", {})
            if isinstance(incident_cfg, dict):
                incident_cfg.setdefault("enabled", True)
                incident_cfg.setdefault("prefer_worker_local_response", True)
                detector_recheck = incident_cfg.setdefault("detector_recheck", {})
                if isinstance(detector_recheck, dict):
                    detector_recheck.setdefault("capacity_loss_ratio", 0.5)
                    detector_recheck.setdefault("recurring_incident_count", 2)
                    detector_recheck.setdefault("backlog_delta", 3)

    experiment_cfg["decision"] = decision_cfg
    experiment_cfg["heuristic_rules"] = heuristic_cfg
    experiment_cfg["worker"] = worker_cfg
    return experiment_cfg
