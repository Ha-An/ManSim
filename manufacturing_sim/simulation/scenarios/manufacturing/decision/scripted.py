from __future__ import annotations
from typing import Any
from .base import DecisionModule, JobPlan, StrategyState, default_task_priority_weights
from .modes import is_fixed_priority_mode, normalize_decision_mode

class ScriptedDecisionModule(DecisionModule):
    """Rule-based controller used by both non-LLM decision modes."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        rules_root = cfg.get("heuristic_rules", {}) if isinstance(cfg.get("heuristic_rules", {}), dict) else {}
        self.rules = rules_root.get("decision", {}) if isinstance(rules_root.get("decision", {}), dict) else {}
        decision_cfg = cfg.get("decision", {}) if isinstance(cfg.get("decision", {}), dict) else {}
        self.decision_mode = normalize_decision_mode(str(decision_cfg.get("mode", "adaptive_priority")))
        self.fixed_task_priority = is_fixed_priority_mode(self.decision_mode) or bool(decision_cfg.get("fixed_task_priority", False))
        norms_cfg = decision_cfg.get("norms", {}) if isinstance(decision_cfg.get("norms", {}), dict) else {}
        self.norms_enabled = bool(norms_cfg.get("enabled", True))

    def _rule(self, dotted_path: str, default: Any) -> Any:
        node: Any = self.rules
        for key in dotted_path.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def _apply_priority_updates(self, weights: dict[str, float], updates: dict[str, Any] | None) -> None:
        if not isinstance(updates, dict):
            return
        for key, value in updates.items():
            key_str = str(key)
            if key_str in weights:
                weights[key_str] = round(float(weights[key_str]) * float(value), 3)

    def reflect(self, observation: dict[str, Any]) -> StrategyState:
        notes: list[str] = []
        break_notes_threshold = int(self._rule("reflect.break_notes_threshold", 2))
        scrap_notes_threshold = float(self._rule("reflect.scrap_notes_threshold", 0.08))
        if observation.get("last_day_machine_breaks", 0) > break_notes_threshold:
            notes.append("Increase repair and PM priority due to frequent breakdowns.")
        if observation.get("last_day_scrap_rate", 0.0) > scrap_notes_threshold:
            notes.append("Increase inspection priority due to high scrap rate.")
        wait_input_count = sum(1 for state in observation.get("machine_states", {}).values() if state == "WAIT_INPUT")
        if wait_input_count >= int(self._rule("propose_jobs.wait_input_machine_threshold", 2)):
            notes.append("Several machines are waiting for input; increase setup and transfer focus.")
        if observation.get("inspection_backlog", 0) > int(self._rule("propose_jobs.inspection_backlog_threshold", 10)):
            notes.append("Inspection backlog is elevated; increase inspection priority.")
        return StrategyState(notes=notes)

    def propose_jobs(self, observation: dict[str, Any], strategy: StrategyState, norms: dict[str, Any]) -> JobPlan:
        weights = default_task_priority_weights()
        base_weights = self._rule("propose_jobs.base_task_priority_weights", default_task_priority_weights())
        if isinstance(base_weights, dict):
            for key, value in base_weights.items():
                key_str = str(key)
                if key_str in weights:
                    weights[key_str] = float(value)
        base_quotas = self._rule("propose_jobs.base_quotas", {"warehouse_material_runs": 20, "setup_runs": 40, "transfer_runs": 40, "inspection_runs": 35})
        quotas: dict[str, int] = {}
        if isinstance(base_quotas, dict):
            for key, value in base_quotas.items():
                quotas[str(key)] = int(value)
        effective_norms = norms if self.norms_enabled and isinstance(norms, dict) else {}
        min_pm = int(effective_norms.get("min_pm_per_machine_per_day", 1))
        inspection_backlog_target = max(1, int(effective_norms.get("inspection_backlog_target", 8)))
        output_buffer_target = max(1, int(effective_norms.get("max_output_buffer_target", 4)))
        battery_reserve_min = float(effective_norms.get("battery_reserve_min", 50.0))
        quotas["pm_runs"] = min_pm * int(self._rule("propose_jobs.pm_runs_per_machine_multiplier", 1)) * 6
        if not self.fixed_task_priority:
            if int(observation.get("last_day_machine_breaks", 0)) > int(self._rule("reflect.break_notes_threshold", 2)):
                self._apply_priority_updates(weights, self._rule("propose_jobs.machine_break_priority_multipliers", {"repair_machine": 1.2, "preventive_maintenance": 1.2}))
            if float(observation.get("last_day_scrap_rate", 0.0)) > float(self._rule("reflect.scrap_notes_threshold", 0.08)):
                self._apply_priority_updates(weights, self._rule("propose_jobs.scrap_priority_multipliers", {"inspect_product": 1.2}))
            wait_input_count = sum(1 for state in observation.get("machine_states", {}).values() if state == "WAIT_INPUT")
            if wait_input_count >= int(self._rule("propose_jobs.wait_input_machine_threshold", 2)):
                self._apply_priority_updates(weights, self._rule("propose_jobs.wait_input_priority_multipliers", {"setup_machine": 1.15, "inter_station_transfer": 1.15, "material_supply": 1.1}))
            if observation.get("inspection_backlog", 0) > int(self._rule("propose_jobs.inspection_backlog_threshold", 10)):
                quotas["inspection_runs"] = int(quotas.get("inspection_runs", 0)) + int(self._rule("propose_jobs.inspection_quota_bonus", 10))
                self._apply_priority_updates(weights, {"inspect_product": float(self._rule("propose_jobs.inspection_priority_multiplier", 1.2))})
        flow_obs = observation.get("flow", {}) if isinstance(observation.get("flow", {}), dict) else {}
        output_waiting = flow_obs.get("output_waiting_transfer", {}) if isinstance(flow_obs.get("output_waiting_transfer", {}), dict) else {}
        max_output_buffer = max((int(value) for value in output_waiting.values()), default=0)
        if not self.fixed_task_priority:
            weights["inspect_product"] = round(float(weights["inspect_product"]) * float(effective_norms.get("inspect_product_priority_weight", 1.0)), 3)
        if max_output_buffer > output_buffer_target:
            quotas["transfer_runs"] = int(quotas.get("transfer_runs", 0)) + int(self._rule("propose_jobs.inspection_quota_bonus", 10))
            if not self.fixed_task_priority:
                self._apply_priority_updates(weights, {"unload_machine": 1.2, "inter_station_transfer": 1.15})
        if int(observation.get("inspection_backlog", 0)) > inspection_backlog_target:
            quotas["inspection_runs"] = int(quotas.get("inspection_runs", 0)) + int(self._rule("propose_jobs.inspection_quota_bonus", 10))
            if not self.fixed_task_priority:
                self._apply_priority_updates(weights, {"inspect_product": 1.15})
        agents_obs = observation.get("agents", {}) if isinstance(observation.get("agents", {}), dict) else {}
        agents_by_id = agents_obs.get("by_id", {}) if isinstance(agents_obs.get("by_id", {}), dict) else {}
        active_batteries = [float(data.get("battery_remaining_min", 0.0)) for data in agents_by_id.values() if not bool(data.get("discharged", False))]
        min_battery = min(active_batteries) if active_batteries else 999.0
        if (not self.fixed_task_priority) and min_battery < battery_reserve_min:
            self._apply_priority_updates(weights, {"battery_swap": 1.15, "battery_delivery_low_battery": 1.15, "battery_delivery_discharged": 1.1})
        rationale_bits = ["direct task-priority planning"]
        if strategy.notes:
            rationale_bits.append("notes=" + "; ".join(strategy.notes))
        if self.fixed_task_priority:
            rationale_bits.append("fixed_task_priority=true")
        return JobPlan(task_priority_weights=weights, quotas=quotas, rationale=", ".join(rationale_bits))

    def discuss(self, day_summary: dict[str, Any], norms: dict[str, Any]) -> dict[str, Any]:
        if not self.norms_enabled:
            return {}
        updated = dict(norms)
        min_pm = int(updated.get("min_pm_per_machine_per_day", 1))
        machine_breaks = day_summary.get("machine_breakdowns", 0)
        if machine_breaks >= int(self._rule("discuss.machine_breakdown_raise_threshold", 3)):
            min_pm = min(int(self._rule("discuss.min_pm_cap", 3)), min_pm + int(self._rule("discuss.machine_breakdown_raise_step", 1)))
        elif bool(self._rule("discuss.machine_breakdown_lower_when_zero", True)) and machine_breaks == 0 and min_pm > 1:
            min_pm -= 1
        inspect_weight = float(updated.get("inspect_product_priority_weight", 1.0))
        if not self.fixed_task_priority:
            scrap_rate = float(day_summary.get("scrap_rate", 0.0))
            if scrap_rate > float(self._rule("discuss.inspect_product_priority_raise_scrap_threshold", 0.08)):
                inspect_weight = min(float(self._rule("discuss.inspect_product_priority_raise_cap", 1.8)), inspect_weight + float(self._rule("discuss.inspect_product_priority_raise_step", 0.1)))
            elif scrap_rate < float(self._rule("discuss.inspect_product_priority_lower_scrap_threshold", 0.03)):
                inspect_weight = max(float(self._rule("discuss.inspect_product_priority_floor", 1.0)), inspect_weight - float(self._rule("discuss.inspect_product_priority_lower_step", 0.05)))
        updated["min_pm_per_machine_per_day"] = min_pm
        updated["inspect_product_priority_weight"] = round(inspect_weight, 3)
        updated.setdefault("inspection_backlog_target", int(updated.get("inspection_backlog_target", 8)))
        updated.setdefault("max_output_buffer_target", int(updated.get("max_output_buffer_target", 4)))
        updated.setdefault("battery_reserve_min", float(updated.get("battery_reserve_min", 50.0)))
        return updated

    def urgent_discuss(self, event: dict[str, Any], local_state: dict[str, Any]) -> dict[str, Any]:
        if self.fixed_task_priority:
            return {"priority_updates": {}}
        event_type = event.get("event_type")
        priority_updates: dict[str, float] = {}
        if event_type == "machine_breakdown":
            src = self._rule("urgent.machine_breakdown_priority_updates", {"repair_machine": 1.4, "preventive_maintenance": 1.5})
        elif event_type in {"agent_failure", "agent_discharged"}:
            src = self._rule("urgent.agent_discharged_priority_updates", {"battery_delivery_discharged": 1.5, "battery_delivery_low_battery": 1.2})
        elif event_type == "battery_risk":
            src = self._rule("urgent.battery_risk_priority_updates", {"battery_swap": 1.5, "battery_delivery_low_battery": 1.4})
        else:
            src = {}
        if isinstance(src, dict):
            for key, value in src.items():
                priority_updates[str(key)] = float(value)
        if int(local_state.get("inspection_backlog", 0)) > int(self._rule("urgent.inspection_backlog_threshold", 15)):
            priority_updates["inspect_product"] = max(priority_updates.get("inspect_product", 1.0), float(self._rule("urgent.inspect_product_priority_min_weight", 1.3)))
        return {"priority_updates": priority_updates}
