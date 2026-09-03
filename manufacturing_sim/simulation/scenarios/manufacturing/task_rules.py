from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Any, Iterable


MFG_FLOW_SHOP_POLICY_MODES = {
    "immediate_shared",
    "immediate_dedicated_roles",
    "rolling_horizon_shared",
    "rolling_horizon_dedicated_roles",
    "simulation_based_adp",
    "random_feasible_dispatch",
}

DEDICATED_POLICY_MODES = {
    "immediate_dedicated_roles",
    "rolling_horizon_dedicated_roles",
}

ROLLING_POLICY_MODES = {
    "rolling_horizon_shared",
    "rolling_horizon_dedicated_roles",
}

RULE_KINDS = {"exclusive", "self_service", "collaborative"}


class TaskRuleConfigError(ValueError):
    """Raised when an mfg_flow_shop policy rule set is ambiguous or incomplete."""


def _normalized_scalar(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if value is None:
        return None
    return str(value).strip().lower()


@dataclass(frozen=True)
class TaskRule:
    role_number: int
    rule_id: str
    display_name: str
    task_code: str
    match: dict[str, Any]
    kind: str
    owner: str

    def matches(self, task_code: str, context: dict[str, Any]) -> bool:
        if self.task_code != str(task_code).strip().upper():
            return False
        return all(
            _normalized_scalar(context.get(key)) == _normalized_scalar(expected)
            for key, expected in self.match.items()
        )


DEFAULT_CANDIDATE_CONTEXTS: tuple[dict[str, Any], ...] = (
    {
        "task_code": "MANAGE_ROBOT_POWER",
        "task_type": "BATTERY_CHARGE",
        "priority_key": "battery_charge",
    },
    {
        "task_code": "REPAIR_MACHINE",
        "task_type": "REPAIR_MACHINE",
        "priority_key": "repair_machine",
        "station": 1,
        "machine_id": "S1M1",
    },
    {
        "task_code": "REPAIR_MACHINE",
        "task_type": "REPAIR_MACHINE",
        "priority_key": "repair_machine",
        "station": 2,
        "machine_id": "S2M1",
    },
    {
        "task_code": "REPLENISH_MATERIAL",
        "task_type": "TRANSFER",
        "priority_key": "material_supply",
        "transfer_kind": "material_supply",
        "station": 1,
        "source": "Warehouse",
        "destination": "material_queue_1",
    },
    {
        "task_code": "REPLENISH_MATERIAL",
        "task_type": "TRANSFER",
        "priority_key": "material_supply",
        "transfer_kind": "material_supply",
        "station": 2,
        "source": "Warehouse",
        "destination": "material_queue_2",
    },
    {
        "task_code": "LOAD_MACHINE",
        "task_type": "LOAD_MACHINE",
        "priority_key": "load_machine",
        "station": 1,
        "machine_id": "S1M1",
        "load_slot": "material",
        "item_type": "material",
        "source": "material_queue_1",
    },
    {
        "task_code": "SETUP_MACHINE",
        "task_type": "SETUP_MACHINE",
        "priority_key": "setup_machine",
        "station": 1,
        "machine_id": "S1M1",
    },
    {
        "task_code": "UNLOAD_MACHINE",
        "task_type": "UNLOAD_MACHINE",
        "priority_key": "unload_machine",
        "station": 1,
        "machine_id": "S1M1",
    },
    {
        "task_code": "TRANSFER",
        "task_type": "TRANSFER",
        "priority_key": "inter_station_transfer",
        "transfer_kind": "inter_station",
        "from_station": 1,
    },
    {
        "task_code": "LOAD_MACHINE",
        "task_type": "LOAD_MACHINE",
        "priority_key": "load_machine",
        "station": 2,
        "machine_id": "S2M1",
        "load_slot": "material",
        "item_type": "material",
        "source": "material_queue_2",
    },
    {
        "task_code": "LOAD_MACHINE",
        "task_type": "LOAD_MACHINE",
        "priority_key": "load_machine",
        "station": 2,
        "machine_id": "S2M1",
        "load_slot": "intermediate",
        "item_type": "intermediate",
        "source": "intermediate_queue_2",
    },
    {
        "task_code": "SETUP_MACHINE",
        "task_type": "SETUP_MACHINE",
        "priority_key": "setup_machine",
        "station": 2,
        "machine_id": "S2M1",
    },
    {
        "task_code": "UNLOAD_MACHINE",
        "task_type": "UNLOAD_MACHINE",
        "priority_key": "unload_machine",
        "station": 2,
        "machine_id": "S2M1",
    },
    {
        "task_code": "TRANSFER",
        "task_type": "TRANSFER",
        "priority_key": "inter_station_transfer",
        "transfer_kind": "inter_station",
        "from_station": 2,
    },
    {
        "task_code": "LOAD_UNLOAD_TRANSFER_INTERFACE",
        "task_type": "LOAD_UNLOAD_TRANSFER_INTERFACE",
        "priority_key": "load_inspection_desk",
        "interface_action": "load",
        "interface": "inspection_desk",
    },
    {
        "task_code": "INSPECT_PRODUCT",
        "task_type": "INSPECT_PRODUCT",
        "priority_key": "inspect_product",
    },
    {
        "task_code": "LOAD_UNLOAD_TRANSFER_INTERFACE",
        "task_type": "LOAD_UNLOAD_TRANSFER_INTERFACE",
        "priority_key": "unload_inspection_desk",
        "interface_action": "unload",
        "interface": "inspection_desk",
    },
    {
        "task_code": "TRANSFER",
        "task_type": "TRANSFER",
        "priority_key": "inter_station_transfer",
        "transfer_kind": "inter_station",
        "from_station": 4,
    },
    {
        "task_code": "COLLECT_WASTE_OR_SCRAP",
        "task_type": "COLLECT_WASTE_OR_SCRAP",
        "priority_key": "scrap_disposal",
        "source": "inspection_scrap_queue",
        "destination": "scrap_disposal_bin",
    },
)


class MfgFlowShopTaskPolicy:
    """Fixed-priority task rules and deterministic pre-run role generation."""

    def __init__(
        self,
        *,
        world: Any,
        decision_mode: str,
        cfg: dict[str, Any],
        worker_ids: Iterable[str],
    ) -> None:
        self.world = world
        self.decision_mode = str(decision_mode).strip().lower()
        self.worker_ids = sorted({str(worker_id).strip() for worker_id in worker_ids if str(worker_id).strip()})
        self.dedicated = self.decision_mode in DEDICATED_POLICY_MODES
        self.cfg = cfg if isinstance(cfg, dict) else {}
        if self.decision_mode not in MFG_FLOW_SHOP_POLICY_MODES:
            raise TaskRuleConfigError(f"Unsupported mfg_flow_shop policy mode: {self.decision_mode}")
        if not self.worker_ids:
            raise TaskRuleConfigError("mfg_flow_shop policy requires at least one worker.")

        self.validation = str(self.cfg.get("validation", "error")).strip().lower() or "error"
        self.rules = self._parse_rules(self.cfg.get("task_rules"))
        self.rules_by_id = {rule.rule_id: rule for rule in self.rules}
        self.rules_by_number = {rule.role_number: rule for rule in self.rules}
        self._validate_role_contract()
        self.priority_order = self._parse_priority_order(self.cfg.get("priority_order"))
        self.priority_rank = {rule_id: index + 1 for index, rule_id in enumerate(self.priority_order)}
        self._validate_default_coverage()

        dedicated_cfg = (
            self.cfg.get("dedicated_roles", {})
            if isinstance(self.cfg.get("dedicated_roles", {}), dict)
            else {}
        )
        self.assignment_strategy = str(
            dedicated_cfg.get("assignment_strategy", "workload_balanced_lpt")
        ).strip().lower()
        self.generated_at = str(dedicated_cfg.get("generated_at", "pre_run_once")).strip().lower()
        self.overflow_policy = str(dedicated_cfg.get("overflow_policy", "idle_extra_workers")).strip().lower()
        if self.dedicated:
            if self.assignment_strategy != "workload_balanced_lpt":
                raise TaskRuleConfigError("dedicated_roles.assignment_strategy must be 'workload_balanced_lpt'.")
            if self.generated_at != "pre_run_once":
                raise TaskRuleConfigError("dedicated_roles.generated_at must be 'pre_run_once'.")
            if self.overflow_policy != "idle_extra_workers":
                raise TaskRuleConfigError("dedicated_roles.overflow_policy must be 'idle_extra_workers'.")

        count_overrides = dedicated_cfg.get("expected_count_overrides", {})
        duration_overrides = dedicated_cfg.get("expected_duration_overrides_min", {})
        self.expected_count_overrides = dict(count_overrides) if isinstance(count_overrides, dict) else {}
        self.expected_duration_overrides = dict(duration_overrides) if isinstance(duration_overrides, dict) else {}

        self.expected_product_count = self._expected_product_count()
        self.rule_metrics = self._build_rule_metrics()
        self.exclusive_owner_by_rule: dict[str, str] = {}
        self.worker_exclusive_rules: dict[str, list[str]] = {worker_id: [] for worker_id in self.worker_ids}
        self.worker_expected_busy_min: dict[str, float] = {worker_id: 0.0 for worker_id in self.worker_ids}
        if self.dedicated:
            self._assign_exclusive_rules()

    def _parse_rules(self, value: Any) -> list[TaskRule]:
        if not isinstance(value, list) or not value:
            raise TaskRuleConfigError("decision.mfg_flow_shop_policy.task_rules must be a non-empty list.")
        parsed: list[TaskRule] = []
        seen: set[str] = set()
        for index, row in enumerate(value):
            if not isinstance(row, dict):
                raise TaskRuleConfigError(f"task_rules[{index}] must be a mapping.")
            rule_id = str(row.get("id", "")).strip().lower()
            try:
                role_number = int(row.get("role_number"))
            except (TypeError, ValueError):
                raise TaskRuleConfigError(f"task_rules[{index}] must define an integer role_number.") from None
            display_name = str(row.get("display_name", "")).strip()
            task_code = str(row.get("task_code", "")).strip().upper()
            kind = str(row.get("kind", "exclusive")).strip().lower() or "exclusive"
            owner = str(row.get("owner", "auto")).strip() or "auto"
            match = row.get("match", {})
            if not rule_id or rule_id in seen:
                raise TaskRuleConfigError(f"task_rules[{index}] has a blank or duplicate id: {rule_id!r}.")
            if not task_code:
                raise TaskRuleConfigError(f"task_rules[{index}] must define task_code.")
            if not display_name:
                raise TaskRuleConfigError(f"task_rules[{index}] must define display_name.")
            if kind not in RULE_KINDS:
                raise TaskRuleConfigError(f"task_rules[{index}].kind must be one of {sorted(RULE_KINDS)}.")
            if not isinstance(match, dict):
                raise TaskRuleConfigError(f"task_rules[{index}].match must be a mapping.")
            if kind != "exclusive" and owner.lower() != "auto":
                raise TaskRuleConfigError(f"Only exclusive task rules may pin an owner: {rule_id}.")
            parsed.append(
                TaskRule(
                    role_number=role_number,
                    rule_id=rule_id,
                    display_name=display_name,
                    task_code=task_code,
                    match={str(key).strip(): expected for key, expected in match.items() if str(key).strip()},
                    kind=kind,
                    owner=owner,
                )
            )
            seen.add(rule_id)
        return parsed

    def _validate_role_contract(self) -> None:
        expected_numbers = set(range(1, 19))
        observed_numbers = [rule.role_number for rule in self.rules]
        missing = sorted(expected_numbers - set(observed_numbers))
        duplicates = sorted({number for number in observed_numbers if observed_numbers.count(number) > 1})
        extras = sorted(set(observed_numbers) - expected_numbers)
        if len(self.rules) != 18 or missing or duplicates or extras:
            raise TaskRuleConfigError(
                "mfg_flow_shop must define role numbers 1..18 exactly once; "
                f"count={len(self.rules)}, missing={missing}, duplicates={duplicates}, extras={extras}."
            )
        invalid_exclusive = [
            rule.role_number for rule in self.rules if rule.role_number <= 16 and rule.kind != "exclusive"
        ]
        role_17 = self.rules_by_number[17]
        role_18 = self.rules_by_number[18]
        if invalid_exclusive:
            raise TaskRuleConfigError(f"mfg_flow_shop roles 1..16 must be exclusive: {invalid_exclusive}.")
        if role_17.task_code != "MANAGE_ROBOT_POWER" or role_17.kind != "self_service":
            raise TaskRuleConfigError("mfg_flow_shop role 17 must be MANAGE_ROBOT_POWER/self_service.")
        if role_18.task_code != "REPAIR_MACHINE" or role_18.kind != "collaborative":
            raise TaskRuleConfigError("mfg_flow_shop role 18 must be REPAIR_MACHINE/collaborative.")
        if len(self.worker_ids) < 2:
            raise TaskRuleConfigError("mfg_flow_shop requires at least two workers for collaborative repair.")
        if int(getattr(self.world, "max_repair_agents", 3) or 3) < 2:
            raise TaskRuleConfigError("mfg_flow_shop machine_failure.max_repair_agents must be at least 2.")

    def _parse_priority_order(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise TaskRuleConfigError("decision.mfg_flow_shop_policy.priority_order must be a list.")
        order = [str(item).strip().lower() for item in value if str(item).strip()]
        if len(order) != len(set(order)):
            raise TaskRuleConfigError("priority_order contains duplicate rule ids.")
        missing = sorted(set(self.rules_by_id) - set(order))
        extra = sorted(set(order) - set(self.rules_by_id))
        if missing or extra:
            raise TaskRuleConfigError(f"priority_order must contain every task rule exactly once; missing={missing}, extra={extra}.")
        return order

    def _validate_default_coverage(self) -> None:
        matched_rule_ids: set[str] = set()
        issues: list[str] = []
        for context in DEFAULT_CANDIDATE_CONTEXTS:
            matches = self._matching_rules(str(context["task_code"]), context)
            label = f"{context.get('task_code')}:{context.get('priority_key')}:{context.get('station', context.get('from_station', ''))}"
            if len(matches) != 1:
                issues.append(f"{label} matched {[rule.rule_id for rule in matches]}; expected exactly one rule")
            else:
                matched_rule_ids.add(matches[0].rule_id)
        unused = sorted(set(self.rules_by_id) - matched_rule_ids)
        if unused:
            issues.append(f"rules do not match any supported mfg_flow_shop candidate shape: {unused}")
        if issues:
            raise TaskRuleConfigError("Invalid mfg_flow_shop task rules:\n- " + "\n- ".join(issues))

    def _matching_rules(self, task_code: str, context: dict[str, Any]) -> list[TaskRule]:
        return [rule for rule in self.rules if rule.matches(task_code, context)]

    @staticmethod
    def task_context(task: Any, task_code: str) -> dict[str, Any]:
        payload = task.payload if isinstance(getattr(task, "payload", None), dict) else {}
        return {
            **payload,
            "task_code": str(task_code).strip().upper(),
            "task_type": str(getattr(task, "task_type", "")).strip().upper(),
            "priority_key": str(getattr(task, "priority_key", "")).strip().lower(),
            "location": str(getattr(task, "location", "")).strip(),
        }

    def rule_for_task(self, task: Any, task_code: str) -> TaskRule:
        context = self.task_context(task, task_code)
        matches = self._matching_rules(task_code, context)
        if len(matches) != 1:
            raise TaskRuleConfigError(
                f"Runtime task {getattr(task, 'task_id', '')}:{task_code} matched "
                f"{[rule.rule_id for rule in matches]}; expected exactly one rule. context={context}"
            )
        return matches[0]

    def rank_for_task(self, task: Any, task_code: str) -> int:
        return int(self.priority_rank[self.rule_for_task(task, task_code).rule_id])

    def allowed_worker_ids(self, task: Any, task_code: str) -> list[str]:
        rule = self.rule_for_task(task, task_code)
        if rule.kind == "self_service":
            target_id = str((getattr(task, "payload", {}) or {}).get("target_agent_id", "")).strip()
            return [target_id] if target_id in self.worker_ids else []
        if not self.dedicated or rule.kind == "collaborative":
            return list(self.worker_ids)
        owner = self.exclusive_owner_by_rule.get(rule.rule_id, "")
        return [owner] if owner else []

    def _expected_product_count(self) -> float:
        material_count = float(getattr(self.world, "material_shelf_initial_fill", 0) or 0)
        if str(getattr(self.world, "objective_mode", "")) == "maximize_throughput":
            days = max(1, int(getattr(self.world, "configured_throughput_days", 1) or 1))
            interval = max(1, int(getattr(self.world, "throughput_restock_interval_days", 1) or 1))
            restock_events = max(0, (days - 1) // interval)
            material_count += float(restock_events * int(getattr(self.world, "throughput_restock_target_fill", 0) or 0))
            horizon_min = float(days * int(getattr(self.world, "minutes_per_day", 240) or 240))
            station_caps = [
                horizon_min / max(0.1, float(value))
                for value in getattr(self.world, "processing_time_min", {}).values()
            ]
            process_capacity = min(station_caps) if station_caps else material_count / 2.0
            inspection_service = max(
                0.1,
                float(self.world.timing.expected_task_duration("LOAD_UNLOAD_TRANSFER_INTERFACE")) * 2.0
                + float(self.world.timing.expected_task_duration("INSPECT_PRODUCT")),
            )
            inspection_capacity = horizon_min / inspection_service
            return max(0.0, min(material_count / 2.0, process_capacity, inspection_capacity))
        return max(0.0, material_count / 2.0)

    def _expected_count(self, rule: TaskRule) -> float:
        if rule.rule_id in self.expected_count_overrides:
            return max(0.0, float(self.expected_count_overrides[rule.rule_id]))
        if rule.kind != "exclusive":
            return 0.0
        products = float(self.expected_product_count)
        defect_prob = max(0.0, min(1.0, float(getattr(self.world, "quality_cfg", {}).get("defect_prob", 0.0) or 0.0)))
        if rule.task_code == "TRANSFER" and int(rule.match.get("from_station", 0) or 0) == 4:
            return products * (1.0 - defect_prob)
        if rule.task_code == "COLLECT_WASTE_OR_SCRAP":
            failed = products * defect_prob
            carry_count = max(1, int(getattr(self.world, "scrap_transport_max_carry_count", 1) or 1))
            return float(math.ceil(failed / carry_count)) if failed > 0.0 else 0.0
        return products

    def _task_route(self, rule: TaskRule) -> tuple[str, list[tuple[str, str, str]]]:
        match = rule.match
        code = rule.task_code
        station = int(match.get("station", 0) or 0)
        if code == "REPLENISH_MATERIAL":
            return "Warehouse", [("Warehouse", f"material_queue_{station}", "material")]
        if code == "LOAD_MACHINE":
            slot = str(match.get("load_slot", "material")).strip().lower()
            item_type = "intermediate" if slot == "intermediate" else "material"
            source = str(match.get("source") or f"{item_type}_queue_{station}")
            return source, [(source, f"S{station}M1", item_type)]
        if code == "SETUP_MACHINE":
            return f"S{station}M1", []
        if code == "UNLOAD_MACHINE":
            item_type = "intermediate" if station == 1 else "product"
            machine_id = f"S{station}M1"
            return machine_id, [(machine_id, f"output_buffer_station_{station}", item_type)]
        if code == "TRANSFER":
            source_station = int(match.get("from_station", 0) or 0)
            if source_station == 1:
                return "output_buffer_station_1", [("output_buffer_station_1", "intermediate_queue_2", "intermediate")]
            if source_station == 2:
                return "output_buffer_station_2", [("output_buffer_station_2", "intermediate_queue_4", "product")]
            if source_station == 4:
                return "output_buffer_station_4", [("output_buffer_station_4", "warehouse_buffer", "product")]
        if code == "INSPECT_PRODUCT":
            workstation = str(getattr(self.world, "inspection_workstation_id", "inspection_desk"))
            return workstation, []
        if code == "LOAD_UNLOAD_TRANSFER_INTERFACE":
            workstation = str(getattr(self.world, "inspection_workstation_id", "inspection_desk"))
            action = str(match.get("interface_action") or match.get("action") or "").strip().lower()
            if action == "load":
                return "intermediate_queue_4", [("intermediate_queue_4", workstation, "product")]
            return workstation, [(workstation, "inspection_output_queue", "product")]
        if code == "COLLECT_WASTE_OR_SCRAP":
            return "inspection_scrap_queue", [("inspection_scrap_queue", "scrap_disposal_bin", "product")]
        return "BatteryStation", []

    def _expected_duration(self, rule: TaskRule) -> float:
        if rule.rule_id in self.expected_duration_overrides:
            return max(0.0, float(self.expected_duration_overrides[rule.rule_id]))
        service_min = float(self.world.timing.expected_task_duration(rule.task_code))
        start, segments = self._task_route(rule)
        dock_ids = [f"charging_dock_{worker_id}" for worker_id in self.worker_ids]
        access_values = [float(self.world.travel_time(dock_id, start)) for dock_id in dock_ids]
        access_min = mean(access_values) if access_values else 0.0
        route_min = 0.0
        for source, destination, item_type in segments:
            multiplier = float(self.world.timing.multiplier(item_type, 1.0))
            route_min += float(self.world.travel_time(source, destination)) * multiplier
        if rule.task_code == "LOAD_UNLOAD_TRANSFER_INTERFACE" and str(
            rule.match.get("interface_action") or rule.match.get("action") or ""
        ).strip().lower() == "unload":
            defect_prob = max(
                0.0,
                min(1.0, float(getattr(self.world, "quality_cfg", {}).get("defect_prob", 0.0) or 0.0)),
            )
            workstation = str(getattr(self.world, "inspection_workstation_id", "inspection_desk"))
            output_travel = float(self.world.travel_time(workstation, "inspection_output_queue"))
            scrap_travel = float(self.world.travel_time(workstation, "inspection_scrap_queue"))
            route_min = ((1.0 - defect_prob) * output_travel + defect_prob * scrap_travel) * float(
                self.world.timing.multiplier("product", 1.0)
            )
        return max(0.0, service_min + access_min + route_min)

    def _build_rule_metrics(self) -> dict[str, dict[str, Any]]:
        metrics: dict[str, dict[str, Any]] = {}
        for rule in self.rules:
            expected_count = self._expected_count(rule)
            expected_duration = self._expected_duration(rule) if rule.kind == "exclusive" else 0.0
            metrics[rule.rule_id] = {
                "role_number": int(rule.role_number),
                "rule_id": rule.rule_id,
                "display_name": rule.display_name,
                "task_code": rule.task_code,
                "match": dict(rule.match),
                "kind": rule.kind,
                "configured_owner": rule.owner,
                "assignment_source": (
                    "fixed"
                    if rule.kind == "exclusive" and rule.owner.lower() != "auto"
                    else "auto_lpt"
                    if rule.kind == "exclusive"
                    else "self_service"
                    if rule.kind == "self_service"
                    else "collaborative"
                ),
                "expected_count": round(expected_count, 6),
                "expected_duration_min": round(expected_duration, 6),
                "expected_busy_min": round(expected_count * expected_duration, 6),
            }
        return metrics

    def _assign_exclusive_rules(self) -> None:
        exclusive_rules = [rule for rule in self.rules if rule.kind == "exclusive"]
        for rule in exclusive_rules:
            configured_owner = rule.owner
            if configured_owner.lower() == "auto":
                continue
            if configured_owner not in self.worker_ids:
                raise TaskRuleConfigError(
                    f"Task rule {rule.rule_id} pins unknown worker {configured_owner}; workers={self.worker_ids}."
                )
            self._assign_rule(rule, configured_owner)

        auto_rules = [rule for rule in exclusive_rules if rule.owner.lower() == "auto"]
        auto_rules.sort(
            key=lambda rule: (
                -float(self.rule_metrics[rule.rule_id]["expected_busy_min"]),
                rule.rule_id,
            )
        )
        for rule in auto_rules:
            worker_id = min(
                self.worker_ids,
                key=lambda candidate: (self.worker_expected_busy_min[candidate], candidate),
            )
            self._assign_rule(rule, worker_id)

        if len(self.exclusive_owner_by_rule) != len(exclusive_rules):
            missing = sorted(rule.rule_id for rule in exclusive_rules if rule.rule_id not in self.exclusive_owner_by_rule)
            raise TaskRuleConfigError(f"Exclusive task rules without an owner: {missing}")

    def _assign_rule(self, rule: TaskRule, worker_id: str) -> None:
        if rule.rule_id in self.exclusive_owner_by_rule:
            raise TaskRuleConfigError(f"Exclusive task rule has multiple owners: {rule.rule_id}")
        self.exclusive_owner_by_rule[rule.rule_id] = worker_id
        self.worker_exclusive_rules[worker_id].append(rule.rule_id)
        self.worker_expected_busy_min[worker_id] += float(self.rule_metrics[rule.rule_id]["expected_busy_min"])

    def summary(self) -> dict[str, Any]:
        ordered_rules = sorted(self.rules, key=lambda rule: rule.role_number)
        exclusive_rule_ids = [rule.rule_id for rule in ordered_rules if rule.kind == "exclusive"]
        common_rule_ids = [rule.rule_id for rule in ordered_rules if rule.kind in {"self_service", "collaborative"}]
        all_rule_ids = [rule.rule_id for rule in ordered_rules]
        shared_busy_min = sum(
            float(self.rule_metrics[rule_id]["expected_busy_min"])
            for rule_id in exclusive_rule_ids
        ) / max(1, len(self.worker_ids))
        reported_worker_loads = {
            worker_id: (
                float(self.worker_expected_busy_min[worker_id])
                if self.dedicated
                else float(shared_busy_min)
            )
            for worker_id in self.worker_ids
        }
        loads = list(reported_worker_loads.values())
        average = mean(loads) if loads else 0.0
        std = pstdev(loads) if len(loads) > 1 else 0.0
        owner_by_rule: dict[str, Any] = {}
        for rule in ordered_rules:
            if rule.kind == "exclusive":
                owner_by_rule[rule.rule_id] = self.exclusive_owner_by_rule.get(rule.rule_id, "") if self.dedicated else "shared"
            elif rule.kind == "self_service":
                owner_by_rule[rule.rule_id] = "self"
            else:
                owner_by_rule[rule.rule_id] = list(self.worker_ids)
        return {
            "schema_version": "1.1",
            "decision_mode": self.decision_mode,
            "dedicated_roles": self.dedicated,
            "assignment_strategy": self.assignment_strategy if self.dedicated else "shared",
            "generated_at": self.generated_at if self.dedicated else "not_applicable",
            "overflow_policy": self.overflow_policy if self.dedicated else "not_applicable",
            "load_basis": "expected_busy_time",
            "objective_mode": str(getattr(self.world, "objective_mode", "")),
            "expected_product_count": round(float(self.expected_product_count), 6),
            "priority_order": list(self.priority_order),
            "owner_by_rule": owner_by_rule,
            "rules": [dict(self.rule_metrics[rule.rule_id]) for rule in ordered_rules],
            "workers": {
                worker_id: {
                    "exclusive_rule_ids": list(self.worker_exclusive_rules[worker_id]),
                    "shared_rule_ids": [] if self.dedicated else list(exclusive_rule_ids),
                    "common_rule_ids": list(common_rule_ids),
                    "assigned_rule_ids": (
                        list(self.worker_exclusive_rules[worker_id]) + list(common_rule_ids)
                        if self.dedicated
                        else list(all_rule_ids)
                    ),
                    "role_numbers": sorted(
                        self.rules_by_id[rule_id].role_number
                        for rule_id in (
                            list(self.worker_exclusive_rules[worker_id]) + list(common_rule_ids)
                            if self.dedicated
                            else list(all_rule_ids)
                        )
                    ),
                    "expected_busy_min": round(reported_worker_loads[worker_id], 6),
                    "task_codes": sorted(
                        {
                            self.rules_by_id[rule_id].task_code
                            for rule_id in (
                                list(self.worker_exclusive_rules[worker_id]) + list(common_rule_ids)
                                if self.dedicated
                                else all_rule_ids
                            )
                        }
                    ),
                }
                for worker_id in self.worker_ids
            },
            "balance": {
                "mean_expected_busy_min": round(average, 6),
                "max_expected_busy_min": round(max(loads, default=0.0), 6),
                "min_expected_busy_min": round(min(loads, default=0.0), 6),
                "coefficient_of_variation": round(std / average, 6) if average > 0.0 else 0.0,
                "max_to_mean_ratio": round(max(loads, default=0.0) / average, 6) if average > 0.0 else 0.0,
            },
            "validation": {
                "role_count": len(self.rules),
                "role_numbers_complete": sorted(rule.role_number for rule in self.rules) == list(range(1, 19)),
                "exclusive_rule_count": sum(1 for rule in self.rules if rule.kind == "exclusive"),
                "exclusive_owned_rule_count": len(self.exclusive_owner_by_rule) if self.dedicated else 0,
                "duplicate_exclusive_owner_count": 0,
                "unmatched_candidate_shape_count": 0,
                "ambiguous_candidate_shape_count": 0,
            },
        }
