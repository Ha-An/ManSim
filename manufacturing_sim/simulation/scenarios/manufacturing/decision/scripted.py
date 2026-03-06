from __future__ import annotations

from typing import Any

from .base import DecisionModule, JobPlan, StrategyState


class ScriptedDecisionModule(DecisionModule):
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        rules_root = cfg.get("heuristic_rules", {}) if isinstance(cfg.get("heuristic_rules", {}), dict) else {}
        self.rules = rules_root.get("decision", {}) if isinstance(rules_root.get("decision", {}), dict) else {}

    def _rule(self, dotted_path: str, default: Any) -> Any:
        node: Any = self.rules
        for key in dotted_path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        queue_lengths = observation.get("component_queue_lengths", {})
        default_bottleneck = int(self._rule("reflect.default_bottleneck_station", 2))
        if queue_lengths:
            bottleneck_station = max(queue_lengths, key=queue_lengths.get)
        else:
            bottleneck_station = default_bottleneck

        notes: list[str] = []
        break_notes_threshold = int(self._rule("reflect.break_notes_threshold", 2))
        scrap_notes_threshold = float(self._rule("reflect.scrap_notes_threshold", 0.08))
        if observation.get("last_day_machine_breaks", 0) > break_notes_threshold:
            notes.append("Increase maintenance focus due to frequent breakdowns.")
        if observation.get("last_day_scrap_rate", 0.0) > scrap_notes_threshold:
            notes.append("Increase inspection focus due to high scrap rate.")

        flow_bias_stations = {int(v) for v in self._rule("reflect.flow_bias_stations", [2, 3])}
        flow_bias_multiplier = float(self._rule("reflect.flow_bias_multiplier", 1.2))
        maintenance_bias_multiplier = float(self._rule("reflect.maintenance_bias_multiplier", 1.2))
        quality_bias_multiplier = float(self._rule("reflect.quality_bias_multiplier", 1.2))

        return StrategyState(
            bottleneck_station=int(bottleneck_station),
            notes=notes,
            priority_bias={
                "flow": flow_bias_multiplier if int(bottleneck_station) in flow_bias_stations else 1.0,
                "maintenance": maintenance_bias_multiplier
                if observation.get("last_day_machine_breaks", 0) > break_notes_threshold
                else 1.0,
                "quality": quality_bias_multiplier if observation.get("last_day_scrap_rate", 0.0) > scrap_notes_threshold else 1.0,
            },
        )

    def propose_jobs(
        self,
        observation: dict[str, Any],
        strategy: StrategyState,
        norms: dict[str, Any],
    ) -> JobPlan:
        base_task_weights = self._rule(
            "propose_jobs.base_task_weights",
            {
                "safety": 1.0,
                "blocking": 1.0,
                "flow": 1.0,
                "supply": 1.0,
                "quality": 1.0,
                "maintenance": 1.0,
                "support": 1.0,
            },
        )
        weights = {
            "safety": 1.0,
            "blocking": 1.0,
            "flow": 1.0,
            "supply": 1.0,
            "quality": 1.0,
            "maintenance": 1.0,
            "support": 1.0,
        }
        if isinstance(base_task_weights, dict):
            for key, value in base_task_weights.items():
                weights[str(key)] = float(value)
        weights.update(strategy.priority_bias)

        min_pm = int(norms.get("min_pm_per_machine_per_day", 1))
        base_quotas = self._rule(
            "propose_jobs.base_quotas",
            {
                "warehouse_material_runs": 20,
                "setup_runs": 40,
                "transfer_runs": 40,
                "inspection_runs": 35,
            },
        )
        quotas: dict[str, int] = {}
        if isinstance(base_quotas, dict):
            for key, value in base_quotas.items():
                quotas[str(key)] = int(value)
        pm_multiplier = int(self._rule("propose_jobs.pm_runs_per_machine_multiplier", 1))
        quotas["pm_runs"] = min_pm * pm_multiplier * 6

        bottleneck_stations_extra = {int(v) for v in self._rule("propose_jobs.bottleneck_stations_extra", [2, 3])}
        bottleneck_quota_bonus = self._rule(
            "propose_jobs.bottleneck_quota_bonus",
            {"setup_runs": 8, "transfer_runs": 8},
        )
        if strategy.bottleneck_station in bottleneck_stations_extra and isinstance(bottleneck_quota_bonus, dict):
            for key, bonus in bottleneck_quota_bonus.items():
                quotas[str(key)] = int(quotas.get(str(key), 0)) + int(bonus)

        backlog_threshold = int(self._rule("propose_jobs.inspection_backlog_threshold", 10))
        backlog_bonus = int(self._rule("propose_jobs.inspection_quota_bonus", 10))
        quality_boost = float(self._rule("propose_jobs.inspection_quality_weight_multiplier", 1.2))
        if observation.get("inspection_backlog", 0) > backlog_threshold:
            quotas["inspection_runs"] = int(quotas.get("inspection_runs", 0)) + backlog_bonus
            weights["quality"] *= quality_boost

        rationale = (
            f"Bottleneck station={strategy.bottleneck_station}, "
            f"notes={'; '.join(strategy.notes) if strategy.notes else 'none'}"
        )
        return JobPlan(task_weights=weights, quotas=quotas, rationale=rationale)

    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        updated = dict(norms)
        machine_breaks = day_summary.get("machine_breakdowns", 0)
        scrap_rate = day_summary.get("scrap_rate", 0.0)

        min_pm = int(updated.get("min_pm_per_machine_per_day", 1))
        min_pm_cap = int(self._rule("discuss.min_pm_cap", 3))
        raise_threshold = int(self._rule("discuss.machine_breakdown_raise_threshold", 3))
        raise_step = int(self._rule("discuss.machine_breakdown_raise_step", 1))
        lower_when_zero = bool(self._rule("discuss.machine_breakdown_lower_when_zero", True))
        if machine_breaks >= raise_threshold:
            min_pm = min(min_pm_cap, min_pm + raise_step)
        elif lower_when_zero and machine_breaks == 0 and min_pm > 1:
            min_pm -= 1

        inspect_weight = float(updated.get("quality_weight", 1.0))
        raise_scrap_threshold = float(self._rule("discuss.quality_weight_raise_scrap_threshold", 0.08))
        raise_step_weight = float(self._rule("discuss.quality_weight_raise_step", 0.1))
        raise_cap = float(self._rule("discuss.quality_weight_raise_cap", 1.8))
        lower_scrap_threshold = float(self._rule("discuss.quality_weight_lower_scrap_threshold", 0.03))
        lower_step_weight = float(self._rule("discuss.quality_weight_lower_step", 0.05))
        lower_floor = float(self._rule("discuss.quality_weight_floor", 1.0))
        if scrap_rate > raise_scrap_threshold:
            inspect_weight = min(raise_cap, inspect_weight + raise_step_weight)
        elif scrap_rate < lower_scrap_threshold:
            inspect_weight = max(lower_floor, inspect_weight - lower_step_weight)

        updated["min_pm_per_machine_per_day"] = min_pm
        updated["quality_weight"] = round(inspect_weight, 3)
        return updated

    def urgent_discuss(self, event: dict[str, Any], local_state: dict[str, Any]) -> dict[str, Any]:
        event_type = event.get("event_type")
        weight_updates: dict[str, float] = {}

        machine_breakdown_updates = self._rule(
            "urgent.machine_breakdown_weight_updates",
            {"blocking": 1.4, "maintenance": 1.5},
        )
        agent_discharged_updates = self._rule(
            "urgent.agent_discharged_weight_updates",
            {"support": 1.4, "blocking": 1.2},
        )
        battery_risk_updates = self._rule(
            "urgent.battery_risk_weight_updates",
            {"safety": 1.5},
        )

        if event_type == "machine_breakdown":
            if isinstance(machine_breakdown_updates, dict):
                for key, value in machine_breakdown_updates.items():
                    weight_updates[str(key)] = float(value)
        elif event_type in {"agent_failure", "agent_discharged"}:
            if isinstance(agent_discharged_updates, dict):
                for key, value in agent_discharged_updates.items():
                    weight_updates[str(key)] = float(value)
        elif event_type == "battery_risk":
            if isinstance(battery_risk_updates, dict):
                for key, value in battery_risk_updates.items():
                    weight_updates[str(key)] = float(value)

        inspection_backlog = int(local_state.get("inspection_backlog", 0))
        backlog_threshold = int(self._rule("urgent.inspection_backlog_threshold", 15))
        quality_min_weight = float(self._rule("urgent.inspection_quality_min_weight", 1.3))
        if inspection_backlog > backlog_threshold:
            weight_updates["quality"] = max(weight_updates.get("quality", 1.0), quality_min_weight)

        return {"weight_updates": weight_updates}
