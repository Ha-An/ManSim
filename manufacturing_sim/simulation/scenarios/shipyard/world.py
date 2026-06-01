from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

import simpy

from humanoidsim import default_humanoid_state, expand_task_steps, transition_humanoid_state

from manufacturing_sim.simulation.scenarios.manufacturing.entities import Task, Worker
from manufacturing_sim.simulation.scenarios.manufacturing.logging import EventLogger
from manufacturing_sim.simulation.scenarios.shipyard.grid_map import ShipyardTileGridMap


SURFACE_TILE_STATES = {
    "WAIT_WELD",
    "WELDED",
    "SURFACE_PREPARED",
    "PAINTED",
    "COMPLETE",
    "REWORK_REQUIRED",
}

COMPLETION_STATES = {"COMPLETE"}
WELDED_OR_LATER = {"WELDED", "SURFACE_PREPARED", "PAINTED", "COMPLETE"}
PAINTED_OR_LATER = {"PAINTED", "COMPLETE"}

TASK_ID_PREFIX = {
    "WELD_SEAM": "WELD",
    "PREPARE_SURFACE": "PREP",
    "PAINT_SURFACE": "PAINT",
    "APPLY_SEALANT": "SEAL",
    "VERIFY_SHIP_SECTION": "VER",
    "OPERATE_VEHICLE_TRANSPORT": "CART",
    "TRANSFER": "TR",
    "CLEAN_AREA": "CLN",
    "COLLECT_WASTE_OR_SCRAP": "SCRAP",
    "MANAGE_ROBOT_POWER": "BAT",
}

_SCENARIO_ALIASES = {
    "shipyard": "shipyard_basic",
    "shipyard_basic": "shipyard_basic",
    "mfg_basic": "factory_mfg_basic",
    "factory_mfg_basic": "factory_mfg_basic",
}


def _scenario_key(cfg: dict[str, Any]) -> str:
    raw = str(cfg.get("scenario_type") or cfg.get("type") or cfg.get("name") or "shipyard_basic").strip().lower()
    return _SCENARIO_ALIASES.get(raw, raw)


def _scenario_entry(mapping: Any, scenario_key: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    candidates = [scenario_key, "shipyard" if scenario_key == "shipyard_basic" else ""]
    for key in candidates:
        if key and key in mapping:
            return mapping[key]
    return None


@dataclass
class ShipWorkTile:
    work_tile_id: str
    entity_id: str
    tile: tuple[int, int]
    state: str = "WAIT_WELD"
    weld_supply_ready: bool = False
    paint_supply_ready: bool = False
    owner: str | None = None
    rework_target: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ShipyardCart:
    cart_id: str
    tile: tuple[int, int]
    heading: tuple[int, int] = (0, 1)
    parking_spot_id: str = ""
    status: str = "parked"
    inventory_kind: str = ""
    inventory_count: int = 0
    reserved_count: int = 0
    owner: str | None = None
    assigned_task_id: str | None = None
    reserved_parking_spot_id: str = ""
    trip_count: int = 0
    items_moved: int = 0
    busy_started_at: float | None = None
    busy_total_min: float = 0.0

    @property
    def available_count(self) -> int:
        return max(0, int(self.inventory_count) - int(self.reserved_count))


class ShipyardWorld:
    def __init__(
        self,
        *,
        env: simpy.Environment,
        cfg: dict[str, Any],
        logger: EventLogger,
        decision_module: Any | None = None,
    ) -> None:
        self.env = env
        self.cfg = cfg
        self.logger = logger
        self.decision_module = decision_module
        self.map = ShipyardTileGridMap.from_world_config(cfg)
        self.minutes_per_day = float(cfg.get("horizon", {}).get("minutes_per_day", 240))
        factory_cfg = cfg.get("factory", {}) if isinstance(cfg.get("factory", {}), dict) else {}
        worker_cfg = cfg.get("worker", {}) if isinstance(cfg.get("worker", {}), dict) else {}
        decision_cfg = cfg.get("decision", {}) if isinstance(cfg.get("decision", {}), dict) else {}
        self.decision_mode = str(decision_cfg.get("mode", "")).strip()
        rolling_cfg = decision_cfg.get("rolling_horizon", {}) if isinstance(decision_cfg.get("rolling_horizon", {}), dict) else {}
        self.rolling_horizon_enabled = self.decision_mode in {"rolling_horizon_aging_priority", "rolling_horizon_dedicated_roles"}
        self.rolling_horizon_window_min = max(0.1, float(rolling_cfg.get("window_min", 5.0) or 5.0))
        self.rolling_horizon_window_index = -1
        self.rolling_horizon_window_start = 0.0
        self.rolling_horizon_window_end = 0.0
        aging_cfg = rolling_cfg.get("aging", {}) if isinstance(rolling_cfg.get("aging", {}), dict) else {}
        self.rolling_horizon_rank_boost_per_window = max(0, int(aging_cfg.get("rank_boost_per_window", 1) or 1))
        self.rolling_horizon_pending: dict[str, dict[str, Any]] = {}
        self.rolling_horizon_dispatch_queues: dict[str, list[dict[str, Any]]] = {}
        self.rolling_horizon_opportunity_counter = 0
        self.rolling_horizon_metrics = {
            "started_window_count": 0,
            "candidate_collected_count": 0,
            "dispatched_task_count": 0,
            "stale_skipped_task_count": 0,
            "requeued_task_count": 0,
            "max_worker_queue_length": 0,
        }
        battery_cfg = decision_cfg.get("battery", {}) if isinstance(decision_cfg.get("battery", {}), dict) else {}
        self.battery_period_min = float(worker_cfg.get("battery_swap_period_min", 240) or 240)
        self.battery_pickup_time_min = float(worker_cfg.get("battery_pickup_time_min", 4.0) or 4.0)
        self.battery_delivery_extra_min = float(worker_cfg.get("battery_delivery_extra_min", 3.0) or 3.0)
        self.battery_low_threshold_ratio = float(battery_cfg.get("low_threshold_ratio", 0.20) or 0.20)
        self.battery_critical_threshold_ratio = float(
            battery_cfg.get("critical_threshold_ratio", max(0.01, self.battery_low_threshold_ratio * 0.5)) or 0.10
        )
        self.battery_delivery_provider_agent_ids = [str(item) for item in battery_cfg.get("delivery_provider_agent_ids", []) or []]
        self.battery_delivery_receiver_agent_ids = [str(item) for item in battery_cfg.get("delivery_receiver_agent_ids", []) or []]
        shipyard_cfg = cfg.get("shipyard", {}) if isinstance(cfg.get("shipyard", {}), dict) else {}
        logistics_cfg = shipyard_cfg.get("logistics", {}) if isinstance(shipyard_cfg.get("logistics", {}), dict) else {}
        self.cart_capacity = max(1, int(logistics_cfg.get("cart_capacity", 20) or 20))
        self.cart_count = max(0, int(logistics_cfg.get("cart_count", getattr(self.map, "cart_count", 2)) or 0))
        self.cart_footprint_tiles = max(1, int(logistics_cfg.get("cart_footprint_tiles", 2) or 2))
        self.cart_load_model = str(logistics_cfg.get("cart_load_model", "single_item_type") or "single_item_type")
        self.worker_ids = [f"A{i}" for i in range(1, int(factory_cfg.get("num_workers", 3) or 3) + 1)]
        self.workers: dict[str, Worker] = {}
        for worker_id in self.worker_ids:
            worker = Worker(worker_id=worker_id, location="ShipDock", tile=self.map.initial_worker_tile(worker_id))
            worker.humanoid_state = default_humanoid_state(worker_id).to_dict()
            self.workers[worker_id] = worker
            self.rolling_horizon_dispatch_queues[worker_id] = []
        self.carts: dict[str, ShipyardCart] = {}
        for index in range(1, self.cart_count + 1):
            cart_id = f"CART-{index:02d}"
            tile = self.map.initial_cart_tile(cart_id)
            parking_spot_id = ""
            for spot_id, spot in self.map.cart_parking_spots.items():
                if spot.tile == tile:
                    parking_spot_id = spot_id
                    break
            self.carts[cart_id] = ShipyardCart(
                cart_id=cart_id,
                tile=tile,
                heading=self._cart_heading_at_tile(tile),
                parking_spot_id=parking_spot_id,
            )

        self.work_tiles = {
            work_tile_id: ShipWorkTile(
                work_tile_id=work_tile_id,
                entity_id=self.map.work_tile_entity_id(work_tile_id),
                tile=layout.tile,
            )
            for work_tile_id, layout in self.map.work_tiles.items()
        }
        # Backward-compatible alias for older utility code. Values are tile objects,
        # not legacy named sections.
        self.sections = self.work_tiles

        self.rng = random.Random(int(cfg.get("seed", 2026)))
        self.daily_summaries: list[dict[str, Any]] = []
        self.minute_snapshots: list[dict[str, Any]] = []
        self.worker_busy_min = {worker_id: 0.0 for worker_id in self.worker_ids}
        self.worker_task_minutes: dict[str, dict[str, float]] = {worker_id: {} for worker_id in self.worker_ids}
        self.task_counter = 0
        self.rework_count = 0
        self.verify_count = 0
        self.verify_pass_count = 0
        self.incident_count_by_code: dict[str, int] = {}
        self.cart_wait_time_min = 0.0
        self.cart_collision_wait_count = 0
        self.terminated = False
        self.termination_reason = ""
        self._last_snapshot_at = -1.0

    def day_index(self) -> int:
        return int(self.env.now // max(1.0, self.minutes_per_day)) + 1

    def start(self) -> None:
        for worker in self.workers.values():
            self._emit_worker_state(worker, "WORKER_STATE_CHANGED")
            self.env.process(self._worker_loop(worker))
        for cart in self.carts.values():
            self._emit_cart_state(cart)
        self.env.process(self._snapshot_loop())

    def _next_task_id(self, task_code: str) -> str:
        self.task_counter += 1
        prefix = TASK_ID_PREFIX.get(task_code, "TASK")
        return f"{prefix}-{self.task_counter:06d}"

    def _emit_worker_state(self, worker: Worker, event_type: str = "WORKER_STATE_CHANGED") -> None:
        self._sync_worker_power_state(worker)
        battery_remaining = self._worker_battery_remaining_min(worker.worker_id)
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type=event_type,
            entity_id=worker.worker_id,
            location=worker.location,
            details={
                "humanoid_state": worker.humanoid_state,
                "tile": self.map.tile_payload(worker.tile),
                "battery_remaining_min": battery_remaining,
                "battery_period_min": self.battery_period_min,
                "task_id": worker.current_task_id or "",
                "task_code": worker.current_task_code or "",
                "instance_id": worker.current_task_instance_id or "",
                "payload": dict(worker.current_task_payload or {}),
            },
        )

    def _emit_cart_state(self, cart: ShipyardCart, event_type: str = "CART_STATE_CHANGED") -> None:
        footprint_tiles = self._cart_footprint_tiles(cart)
        cargo_tile = footprint_tiles[1] if len(footprint_tiles) > 1 else None
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type=event_type,
            entity_id=cart.cart_id,
            location=cart.parking_spot_id or "cart_route",
            details={
                "cart_id": cart.cart_id,
                "vehicle_id": cart.cart_id,
                "tile": self.map.tile_payload(cart.tile),
                "cockpit_tile": self.map.tile_payload(cart.tile),
                "cargo_tile": self.map.tile_payload(cargo_tile),
                "heading": {"x": int(cart.heading[0]), "y": int(cart.heading[1])},
                "footprint_tiles": self.map.path_payload(footprint_tiles),
                "footprint_length_tiles": int(self.cart_footprint_tiles),
                "status": cart.status,
                "inventory_kind": cart.inventory_kind,
                "inventory_count": int(cart.inventory_count),
                "reserved_count": int(cart.reserved_count),
                "available_count": int(cart.available_count),
                "capacity": int(self.cart_capacity),
                "owner": cart.owner or "",
                "assigned_worker_id": cart.owner or "",
                "assigned_task_id": cart.assigned_task_id or "",
                "parking_spot_id": cart.parking_spot_id or "",
                "reserved_parking_spot_id": cart.reserved_parking_spot_id or "",
                "cart_load_model": self.cart_load_model,
            },
        )

    def _cart_allowed_tiles(self) -> set[tuple[int, int]]:
        return set(self.map.cart_route_tiles) | {spot.tile for spot in self.map.cart_parking_spots.values()} | set(self.map.cart_source_tiles.values())

    @staticmethod
    def _cart_step_heading(from_tile: tuple[int, int], to_tile: tuple[int, int], fallback: tuple[int, int] = (0, 1)) -> tuple[int, int]:
        dx = max(-1, min(1, to_tile[0] - from_tile[0]))
        dy = max(-1, min(1, to_tile[1] - from_tile[1]))
        return (dx, dy) if abs(dx) + abs(dy) == 1 else fallback

    def _cart_heading_at_tile(self, tile: tuple[int, int], preferred: tuple[int, int] | None = None) -> tuple[int, int]:
        allowed = self._cart_allowed_tiles()
        candidates = []
        if preferred is not None:
            candidates.append(preferred)
        candidates.extend([(0, 1), (1, 0), (0, -1), (-1, 0)])
        for heading in candidates:
            if self.cart_footprint_tiles <= 1:
                return heading
            front = (tile[0] + heading[0], tile[1] + heading[1])
            if front in allowed:
                return heading
        return preferred or (0, 1)

    def _cart_footprint_tiles(
        self,
        cart: ShipyardCart,
        *,
        tile: tuple[int, int] | None = None,
        heading: tuple[int, int] | None = None,
    ) -> list[tuple[int, int]]:
        anchor = tile or cart.tile
        direction = heading or cart.heading
        tiles = [anchor]
        for offset in range(1, int(self.cart_footprint_tiles)):
            tiles.append((anchor[0] + direction[0] * offset, anchor[1] + direction[1] * offset))
        return tiles

    def _cart_footprint_allowed(self, cart: ShipyardCart, *, tile: tuple[int, int], heading: tuple[int, int]) -> bool:
        allowed = self._cart_allowed_tiles()
        return all(candidate in allowed for candidate in self._cart_footprint_tiles(cart, tile=tile, heading=heading))

    def _worker_battery_remaining_min(self, worker_id: str | None = None) -> float:
        if self.battery_period_min <= 0:
            return 0.0
        last_swap = 0.0
        if worker_id and worker_id in self.workers:
            last_swap = float(getattr(self.workers[worker_id], "last_battery_swap", 0.0) or 0.0)
        elapsed = max(0.0, float(self.env.now) - last_swap)
        return max(0.0, self.battery_period_min - elapsed)

    def _battery_low_threshold_min(self) -> float:
        return max(0.0, self.battery_period_min * self.battery_low_threshold_ratio)

    def _battery_critical_threshold_min(self) -> float:
        return max(0.0, self.battery_period_min * self.battery_critical_threshold_ratio)

    def _battery_is_low(self, worker_id: str) -> bool:
        return self._worker_battery_remaining_min(worker_id) <= self._battery_low_threshold_min()

    def _sync_worker_power_state(self, worker: Worker) -> None:
        if self.battery_period_min <= 0:
            return
        current_power = str((worker.humanoid_state or {}).get("power", "POWER_NORMAL"))
        if worker.current_task_code == "MANAGE_ROBOT_POWER" and current_power == "CHARGING":
            return
        remaining = self._worker_battery_remaining_min(worker.worker_id)
        if remaining <= self._battery_critical_threshold_min():
            event_type = "power_critical"
            desired_power = "POWER_CRITICAL"
        elif remaining <= self._battery_low_threshold_min():
            event_type = "power_low"
            desired_power = "POWER_LOW"
        else:
            event_type = "power_normal"
            desired_power = "POWER_NORMAL"
        if current_power == desired_power:
            return
        worker.humanoid_state = transition_humanoid_state(
            worker.humanoid_state,
            {
                "event_type": event_type,
                "timestamp_s": float(self.env.now) * 60.0,
                "metadata": {
                    "source": "shipyard_battery_policy",
                    "battery_remaining_min": remaining,
                    "battery_period_min": self.battery_period_min,
                },
            },
        ).to_dict()

    def _transition(self, worker: Worker, event: dict[str, Any]) -> None:
        event.setdefault("timestamp_s", float(self.env.now) * 60.0)
        worker.humanoid_state = transition_humanoid_state(worker.humanoid_state, event).to_dict()
        self._emit_worker_state(worker)

    def _emit_work_tile(self, work_tile: ShipWorkTile, *, reason: str, worker_id: str = "", task_id: str = "") -> None:
        work_tile.history.append(
            {
                "t": round(float(self.env.now), 3),
                "state": work_tile.state,
                "reason": reason,
                "worker_id": worker_id,
                "task_id": task_id,
            }
        )
        completed = sum(1 for tile in self.work_tiles.values() if tile.state == "COMPLETE")
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="SHIP_TILE_STATE_CHANGED",
            entity_id=work_tile.entity_id,
            location="ShipDock",
            details={
                "work_tile_id": work_tile.work_tile_id,
                "ship_tile_id": work_tile.entity_id,
                "state": work_tile.state,
                "surface_tile_state": work_tile.state,
                "ship_surface_state": work_tile.state,
                "tile": self.map.tile_payload(work_tile.tile),
                "weld_supply_ready": work_tile.weld_supply_ready,
                "paint_supply_ready": work_tile.paint_supply_ready,
                "rework_target": work_tile.rework_target or "",
                "worker_id": worker_id,
                "task_id": task_id,
                "reason": reason,
                "completed_surface_tile_count": completed,
                "surface_tile_completion_ratio": round(completed / max(1, len(self.work_tiles)), 6),
            },
        )

    def _worker_loop(self, worker: Worker) -> Any:
        while not self.terminated:
            task = self.candidate_task(worker)
            if task is None:
                yield self.env.timeout(1.0)
                continue
            yield from self.execute_task(worker, task)
            if self._all_surface_tiles_complete():
                self.terminated = True
                self.termination_reason = "all_ship_surface_tiles_complete"
                break

    def candidate_task(self, worker: Worker) -> Task | None:
        if self.rolling_horizon_enabled:
            self._rolling_horizon_update()
            if self._worker_battery_remaining_min(worker.worker_id) <= 0.0 and worker.awaiting_battery_from:
                return None
            queued_battery = self._pop_queued_self_battery_task(worker)
            if queued_battery is not None:
                self._reserve_battery_task(worker, queued_battery)
                return queued_battery
            guard_battery = self._battery_guard_task(worker)
            if guard_battery is not None:
                self._reserve_battery_task(worker, guard_battery)
                return guard_battery
            while self.rolling_horizon_dispatch_queues.get(worker.worker_id):
                entry = self.rolling_horizon_dispatch_queues[worker.worker_id].pop(0)
                task = entry["task"]
                if not self._task_still_feasible(task):
                    self._rolling_horizon_log_skip(entry, "stale_or_infeasible_before_start")
                    continue
                self._reserve_battery_task(worker, task)
                self._reserve_cart_task(worker, task)
                work_tile_id = str(task.payload.get("work_tile_id", ""))
                if work_tile_id in self.work_tiles:
                    self.work_tiles[work_tile_id].owner = worker.worker_id
                return task
            return None
        candidates = self._candidate_tasks_for_worker(worker)
        if not candidates:
            return None
        current_tile = worker.tile or self.map.initial_worker_tile(worker.worker_id)
        def local_distance(task: Task) -> int:
            target_tile = task.payload.get("target_tile")
            if isinstance(target_tile, dict):
                tile = (int(target_tile.get("x", current_tile[0])), int(target_tile.get("y", current_tile[1])))
                return abs(tile[0] - current_tile[0]) + abs(tile[1] - current_tile[1])
            service = self.map.work_tile_service_tile(str(task.payload.get("work_tile_id", "")), current_tile)
            return abs(service[0] - current_tile[0]) + abs(service[1] - current_tile[1])

        candidates.sort(
            key=lambda task: (
                0 if task.task_code == "MANAGE_ROBOT_POWER" else self._task_rank(worker.worker_id, task.task_code),
                local_distance(task),
                str(task.payload.get("work_tile_id", "")),
            )
        )
        task = candidates[0]
        self._reserve_battery_task(worker, task)
        self._reserve_cart_task(worker, task)
        work_tile_id = str(task.payload.get("work_tile_id", ""))
        if work_tile_id in self.work_tiles:
            self.work_tiles[work_tile_id].owner = worker.worker_id
        return task

    def _rolling_horizon_update(self) -> None:
        now = float(self.env.now)
        if self.rolling_horizon_window_index < 0:
            self._rolling_horizon_start_window(now)
            self._rolling_horizon_collect_candidates()
            return
        if now + 1e-9 < self.rolling_horizon_window_end:
            return
        self._rolling_horizon_requeue_unstarted()
        self._rolling_horizon_dispatch_pending()
        self._rolling_horizon_start_window(self.rolling_horizon_window_end)
        self._rolling_horizon_collect_candidates()

    def _rolling_horizon_start_window(self, start_time: float) -> None:
        self.rolling_horizon_window_index += 1
        self.rolling_horizon_window_start = float(start_time)
        self.rolling_horizon_window_end = self.rolling_horizon_window_start + self.rolling_horizon_window_min
        self.rolling_horizon_metrics["started_window_count"] += 1
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="ROLLING_HORIZON_WINDOW_START",
            entity_id=f"RHW-{self.rolling_horizon_window_index:04d}",
            location="ShipDock",
            details={
                "window_index": self.rolling_horizon_window_index,
                "window_start_min": round(self.rolling_horizon_window_start, 3),
                "window_end_min": round(self.rolling_horizon_window_end, 3),
                "window_min": round(self.rolling_horizon_window_min, 3),
                "dispatch_policy": "dedicated_role_aging_priority" if self.decision_mode == "rolling_horizon_dedicated_roles" else "aging_priority",
                "pending_candidate_count": len(self.rolling_horizon_pending),
            },
        )

    def _rolling_horizon_collect_candidates(self) -> None:
        existing_keys = {
            str(entry.get("resource_key", ""))
            for entry in self.rolling_horizon_pending.values()
            if str(entry.get("resource_key", ""))
        }
        for queue in self.rolling_horizon_dispatch_queues.values():
            for entry in queue:
                key = str(entry.get("resource_key", ""))
                if key:
                    existing_keys.add(key)
        for work_tile in self.work_tiles.values():
            if work_tile.owner:
                task_code, extra_payload = self._task_for_work_tile(work_tile)
                if task_code:
                    existing_keys.add(self._resource_key_for_payload(work_tile.work_tile_id, task_code, extra_payload))
        for worker in self.workers.values():
            for task in self._candidate_tasks_for_worker(worker):
                resource_key = self._resource_key(task)
                if resource_key in existing_keys:
                    continue
                allowed_workers = self._allowed_workers_for_task(task)
                if not allowed_workers:
                    continue
                opportunity_id = self._next_rolling_horizon_opportunity_id()
                entry = {
                    "opportunity_id": opportunity_id,
                    "task": task,
                    "task_id": task.task_id,
                    "task_code": task.task_code,
                    "resource_key": resource_key,
                    "allowed_worker_ids": allowed_workers,
                    "first_seen": float(self.env.now),
                    "first_window_index": self.rolling_horizon_window_index,
                    "last_updated": float(self.env.now),
                    "status": "pool",
                }
                self.rolling_horizon_pending[opportunity_id] = entry
                existing_keys.add(resource_key)
                self.rolling_horizon_metrics["candidate_collected_count"] += 1
                self._rolling_horizon_log_entry("ROLLING_HORIZON_CANDIDATE_COLLECTED", entry)

    def _rolling_horizon_dispatch_pending(self) -> None:
        reserved: set[str] = set()
        ordered_entries = sorted(
            list(self.rolling_horizon_pending.items()),
            key=lambda item: (
                min(
                    (
                        self._rolling_horizon_effective_rank(item[1], worker_id)
                        for worker_id in item[1].get("allowed_worker_ids", []) or self.worker_ids
                    ),
                    default=999,
                ),
                float(item[1].get("first_seen", self.env.now)),
                str(item[1].get("resource_key", "")),
            ),
        )
        for opportunity_id, entry in ordered_entries:
            task = entry["task"]
            if not self._task_still_feasible(task):
                self.rolling_horizon_pending.pop(opportunity_id, None)
                self._rolling_horizon_log_skip(entry, "stale_or_infeasible_at_dispatch")
                continue
            key = str(entry.get("resource_key", ""))
            if key in reserved:
                continue
            worker_id = self._rolling_horizon_choose_worker(entry)
            if not worker_id:
                continue
            entry["assigned_worker_id"] = worker_id
            entry["status"] = "dispatched"
            entry["last_updated"] = float(self.env.now)
            self._reserve_cart_task(self.workers[worker_id], task)
            self.rolling_horizon_dispatch_queues.setdefault(worker_id, []).append(entry)
            self.rolling_horizon_pending.pop(opportunity_id, None)
            reserved.add(key)
            self.rolling_horizon_metrics["dispatched_task_count"] += 1
            queue_len = len(self.rolling_horizon_dispatch_queues[worker_id])
            self.rolling_horizon_metrics["max_worker_queue_length"] = max(
                int(self.rolling_horizon_metrics.get("max_worker_queue_length", 0)),
                queue_len,
            )
            self._rolling_horizon_log_entry("ROLLING_HORIZON_DISPATCH", entry)

    def _rolling_horizon_requeue_unstarted(self) -> None:
        for worker_id, queue in self.rolling_horizon_dispatch_queues.items():
            if not queue:
                continue
            self.rolling_horizon_dispatch_queues[worker_id] = []
            for entry in queue:
                self._release_cart_task(entry["task"])
                entry.pop("assigned_worker_id", None)
                entry["status"] = "pool"
                entry["last_updated"] = float(self.env.now)
                self.rolling_horizon_pending[str(entry["opportunity_id"])] = entry
                self.rolling_horizon_metrics["requeued_task_count"] += 1
                self._rolling_horizon_log_entry("ROLLING_HORIZON_TASK_REQUEUED", entry, worker_id=worker_id)

    def _rolling_horizon_choose_worker(self, entry: dict[str, Any], *, excluded_workers: set[str] | None = None) -> str | None:
        task = entry["task"]
        excluded_workers = excluded_workers or set()
        current_candidates = []
        for worker_id in entry.get("allowed_worker_ids", []):
            if str(worker_id) in excluded_workers:
                continue
            worker = self.workers.get(str(worker_id))
            if not worker:
                continue
            # Active work must count as load; otherwise a busy worker can keep
            # receiving queued tasks while another role-compatible worker sits idle.
            queue_len = len(self.rolling_horizon_dispatch_queues.get(worker.worker_id, [])) + (
                1 if worker.current_task_id else 0
            )
            distance = self._distance_to_task(worker, task)
            rank = self._rolling_horizon_effective_rank(entry, worker.worker_id)
            current_candidates.append((queue_len, rank, distance, worker.worker_id))
        return min(current_candidates)[3] if current_candidates else None

    def _allowed_workers_for_task(self, task: Task) -> list[str]:
        if task.task_code == "MANAGE_ROBOT_POWER" and task.assigned_robot_id:
            return [str(task.assigned_robot_id)]
        allowed: list[str] = []
        for worker_id in self.worker_ids:
            codes = self._allowed_task_codes(worker_id)
            if not codes or task.task_code in codes:
                allowed.append(worker_id)
        return allowed

    def _distance_to_task(self, worker: Worker, task: Task) -> int:
        current_tile = worker.tile or self.map.initial_worker_tile(worker.worker_id)
        target_tile = task.payload.get("target_tile")
        if isinstance(target_tile, dict):
            tile = (int(target_tile.get("x", current_tile[0])), int(target_tile.get("y", current_tile[1])))
        else:
            work_tile_id = str(task.payload.get("work_tile_id", ""))
            tile = self.map.work_tile_service_tile(work_tile_id, current_tile) if work_tile_id in self.work_tiles else current_tile
        return abs(tile[0] - current_tile[0]) + abs(tile[1] - current_tile[1])

    def _task_still_feasible(self, task: Task) -> bool:
        if task.task_code == "MANAGE_ROBOT_POWER":
            action = str(task.payload.get("power_action", "self_swap"))
            receiver_id = str(task.payload.get("receiver_id", ""))
            target_id = receiver_id if action == "battery_delivery" and receiver_id else task.assigned_robot_id or ""
            provider_id = str(task.assigned_robot_id or "")
            if action == "battery_delivery" and provider_id and self._battery_is_low(provider_id):
                return False
            return bool(target_id and target_id in self.workers and self._battery_is_low(target_id))
        if task.task_code == "OPERATE_VEHICLE_TRANSPORT":
            cart_id = str(task.payload.get("vehicle_id", ""))
            item_type = str(task.payload.get("item_type", ""))
            cart = self.carts.get(cart_id)
            if cart is None:
                return False
            if cart.assigned_task_id and cart.assigned_task_id != task.task_id:
                return False
            if cart.inventory_count > 0 and cart.assigned_task_id != task.task_id:
                return False
            return self._unserved_supply_demand_count(item_type) > 0
        work_tile_id = str(task.payload.get("work_tile_id", ""))
        if work_tile_id not in self.work_tiles:
            return False
        work_tile = self.work_tiles[work_tile_id]
        if work_tile.owner:
            return False
        current_kind = task.payload.get("transfer_kind")
        if task.task_code == "TRANSFER" and current_kind == "cart_supply":
            item_type = str(task.payload.get("item_type", ""))
            if not self._work_tile_needs_cart_supply(work_tile, item_type):
                return False
            cart = self.carts.get(str(task.payload.get("source_cart_id", "")))
            if cart is None:
                return False
            if cart.inventory_kind != item_type:
                return False
            if cart.status != "parked" or not cart.parking_spot_id:
                return False
            task.payload["parking_spot_id"] = cart.parking_spot_id
            task.payload["source"] = cart.cart_id
            if task.payload.get("_cart_reserved"):
                return cart.inventory_count > 0
            return cart.available_count > 0
        expected_code, expected_payload = self._task_for_work_tile(work_tile)
        if expected_code != task.task_code:
            return False
        expected_kind = expected_payload.get("transfer_kind")
        return expected_kind == current_kind

    def _resource_key(self, task: Task) -> str:
        if task.task_code == "MANAGE_ROBOT_POWER":
            receiver = str(task.payload.get("receiver_id", "")) or str(task.assigned_robot_id or "")
            # Battery service is exclusive by serviced worker, regardless of
            # whether the final plan is self-swap or delivery by another worker.
            return f"battery_service:{receiver}"
        if task.task_code == "OPERATE_VEHICLE_TRANSPORT":
            return (
                f"cart_batch:{task.payload.get('vehicle_id', '')}:"
                f"{task.payload.get('item_type', '')}:{task.payload.get('parking_spot_id', '')}"
            )
        if task.task_code == "TRANSFER" and task.payload.get("transfer_kind") == "cart_supply":
            return f"cart_supply:{task.payload.get('work_tile_id', '')}:{task.payload.get('item_type', '')}"
        return self._resource_key_for_payload(
            str(task.payload.get("work_tile_id", "")),
            task.task_code,
            task.payload,
        )

    def _resource_key_for_payload(self, work_tile_id: str, task_code: str, payload: dict[str, Any]) -> str:
        return f"{work_tile_id}:{task_code}:{payload.get('transfer_kind', '')}"

    def _next_rolling_horizon_opportunity_id(self) -> str:
        self.rolling_horizon_opportunity_counter += 1
        return f"RHOPP-{self.rolling_horizon_opportunity_counter:06d}"

    def _rolling_horizon_log_skip(self, entry: dict[str, Any], reason: str) -> None:
        self._release_cart_task(entry["task"])
        self.rolling_horizon_metrics["stale_skipped_task_count"] += 1
        entry["status"] = "skipped"
        entry["skip_reason"] = reason
        entry["last_updated"] = float(self.env.now)
        self._rolling_horizon_log_entry("ROLLING_HORIZON_TASK_SKIPPED", entry)

    def _rolling_horizon_waited_window_count(self, entry: dict[str, Any]) -> int:
        return max(0, self.rolling_horizon_window_index - int(entry.get("first_window_index", self.rolling_horizon_window_index)))

    def _rolling_horizon_effective_rank(self, entry: dict[str, Any], worker_id: str | None = None) -> int:
        task: Task = entry["task"]
        if worker_id:
            base_rank = self._task_rank(worker_id, task.task_code)
        else:
            base_rank = min((self._task_rank(candidate, task.task_code) for candidate in entry.get("allowed_worker_ids", []) or self.worker_ids), default=999)
        waited_windows = self._rolling_horizon_waited_window_count(entry)
        floor = 0 if base_rank <= 0 else 1
        return max(floor, base_rank - waited_windows * self.rolling_horizon_rank_boost_per_window)

    def _rolling_horizon_log_entry(self, event_type: str, entry: dict[str, Any], *, worker_id: str | None = None) -> None:
        task: Task = entry["task"]
        assigned_worker = worker_id or entry.get("assigned_worker_id", "")
        payload = dict(task.payload)
        waited_windows = self._rolling_horizon_waited_window_count(entry)
        base_rank = min((self._task_rank(worker_id, task.task_code) for worker_id in entry.get("allowed_worker_ids", []) or self.worker_ids), default=999)
        details = {
            "opportunity_id": entry["opportunity_id"],
            "task_id": task.task_id,
            "task_code": task.task_code,
            "task_type": task.task_type,
            "instance_id": task.instance_id,
            "target": payload.get("target", task.location),
            "window_index": self.rolling_horizon_window_index,
            "first_window_index": entry.get("first_window_index", self.rolling_horizon_window_index),
            "window_start_min": round(self.rolling_horizon_window_start, 3),
            "window_end_min": round(self.rolling_horizon_window_end, 3),
            "first_seen_min": round(float(entry.get("first_seen", self.env.now)), 3),
            "status_time_min": round(float(entry.get("last_updated", self.env.now)), 3),
            "status": str(entry.get("status", "") or "").strip() or (
                "pool" if event_type == "ROLLING_HORIZON_CANDIDATE_COLLECTED" else ""
            ),
            "base_priority_rank": base_rank,
            "effective_priority_rank": self._rolling_horizon_effective_rank(entry),
            "waited_window_count": waited_windows,
            "worker_id": assigned_worker,
            "assigned_worker_id": assigned_worker,
            "allowed_worker_ids": list(entry.get("allowed_worker_ids", [])),
            "role_policy": "dedicated_roles" if self.decision_mode == "rolling_horizon_dedicated_roles" else "aging_priority",
            "rolling_task_signature": payload,
            "reason": entry.get("skip_reason", ""),
        }
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type=event_type,
            entity_id=str(entry["opportunity_id"]),
            location="ShipDock",
            details=details,
        )

    def _candidate_tasks_for_worker(self, worker: Worker) -> list[Task]:
        allowed = self._allowed_task_codes(worker.worker_id)
        candidates: list[Task] = self._battery_candidates_for_worker(worker, allowed)
        candidates.extend(self._cart_batch_candidates_for_worker(worker, allowed))
        for work_tile in self.work_tiles.values():
            if work_tile.owner and work_tile.owner != worker.worker_id:
                continue
            task_code, extra_payload = self._task_for_work_tile(work_tile)
            if not task_code:
                continue
            if allowed and task_code not in allowed:
                continue
            task_id = self._next_task_id(task_code)
            target = work_tile.entity_id
            candidates.append(
                Task(
                    task_id=task_id,
                    task_type=task_code,
                    priority_key=task_code.lower(),
                    priority=float(self._task_rank(worker.worker_id, task_code)),
                    location=target,
                    task_code=task_code,
                    instance_id=task_id,
                    payload={
                        "work_tile_id": work_tile.work_tile_id,
                        "ship_tile_id": work_tile.entity_id,
                        "target": target,
                        "tile": self.map.tile_payload(work_tile.tile),
                        "surface_tile_state": work_tile.state,
                        **extra_payload,
                    },
                    args={
                        "ship_surface_tile": work_tile.work_tile_id,
                        "work_spec": {"scenario": "shipyard_basic", "task_code": task_code},
                    },
                )
            )
        return candidates

    def _cart_batch_candidates_for_worker(self, worker: Worker, allowed: list[str]) -> list[Task]:
        if allowed and "OPERATE_VEHICLE_TRANSPORT" not in allowed:
            return []
        candidates: list[Task] = []
        for item_type, source in (("weld_wire", "MaterialYard"), ("paint_can", "PaintSupply")):
            demand_count = self._unserved_supply_demand_count(item_type)
            if demand_count <= 0:
                continue
            remaining_demand = demand_count - self._cart_inventory_available(item_type)
            if remaining_demand <= 0:
                continue
            source_tile = self.map.cart_source_tile(source)
            if source_tile is None:
                continue
            reserved_spot_tiles: set[tuple[int, int]] = set()
            for cart in self._available_empty_carts():
                parking_spot = self._best_parking_spot_for_item(item_type, excluded_tiles=reserved_spot_tiles)
                if parking_spot is None:
                    break
                batch_count = min(self.cart_capacity, remaining_demand)
                if batch_count <= 0:
                    break
                task_id = self._next_task_id("OPERATE_VEHICLE_TRANSPORT")
                payload = {
                    "vehicle_id": cart.cart_id,
                    "vehicle_type": "cart",
                    "item_type": item_type,
                    "batch_count": batch_count,
                    "source": source,
                    "source_tile": self.map.tile_payload(source_tile),
                    "parking_spot_id": parking_spot.parking_spot_id,
                    "target": parking_spot.parking_spot_id,
                    "target_tile": self.map.tile_payload(parking_spot.tile),
                    "route_id": "shipyard_cart_route",
                }
                candidates.append(
                    Task(
                        task_id=task_id,
                        task_type="OPERATE_VEHICLE_TRANSPORT",
                        priority_key="operate_vehicle_transport",
                        priority=float(self._task_rank(worker.worker_id, "OPERATE_VEHICLE_TRANSPORT")),
                        location=parking_spot.parking_spot_id,
                        task_code="OPERATE_VEHICLE_TRANSPORT",
                        instance_id=task_id,
                        payload=payload,
                        args={
                            "vehicle": cart.cart_id,
                            "item": {"item_type": item_type, "quantity": batch_count},
                            "source": source,
                            "destination": parking_spot.parking_spot_id,
                            "route": "shipyard_cart_route",
                        },
                    )
                )
                reserved_spot_tiles.add(parking_spot.tile)
                remaining_demand -= batch_count
        return candidates

    def _unserved_supply_demand_count(self, item_type: str) -> int:
        count = 0
        for work_tile in self.work_tiles.values():
            if work_tile.owner:
                continue
            if item_type == "weld_wire" and work_tile.state in {"WAIT_WELD", "REWORK_REQUIRED"} and not work_tile.weld_supply_ready:
                if work_tile.state != "REWORK_REQUIRED" or work_tile.rework_target == "weld":
                    count += 1
            elif item_type == "paint_can" and work_tile.state in {"SURFACE_PREPARED", "REWORK_REQUIRED"} and not work_tile.paint_supply_ready:
                if work_tile.state != "REWORK_REQUIRED" or work_tile.rework_target != "weld":
                    count += 1
        return count

    def _cart_inventory_available(self, item_type: str) -> int:
        return sum(cart.available_count for cart in self.carts.values() if cart.inventory_kind == item_type)

    def _available_empty_carts(self) -> list[ShipyardCart]:
        return sorted(
            [
                cart
                for cart in self.carts.values()
                if not cart.assigned_task_id and cart.inventory_count <= 0 and cart.status in {"parked", "idle", ""}
            ],
            key=lambda cart: cart.cart_id,
        )

    def _available_empty_cart(self) -> ShipyardCart | None:
        candidates = self._available_empty_carts()
        return candidates[0] if candidates else None

    def _best_parking_spot_for_item(
        self,
        item_type: str,
        *,
        excluded_tiles: set[tuple[int, int]] | None = None,
    ) -> Any | None:
        target_tiles = [
            tile.tile
            for tile in self.work_tiles.values()
            if (
                item_type == "weld_wire"
                and tile.state in {"WAIT_WELD", "REWORK_REQUIRED"}
                and not tile.weld_supply_ready
                and (tile.state != "REWORK_REQUIRED" or tile.rework_target == "weld")
            )
            or (
                item_type == "paint_can"
                and tile.state in {"SURFACE_PREPARED", "REWORK_REQUIRED"}
                and not tile.paint_supply_ready
                and (tile.state != "REWORK_REQUIRED" or tile.rework_target != "weld")
            )
        ]
        if not target_tiles:
            return None
        excluded_tiles = excluded_tiles or set()
        occupied_tiles = {
            cart.tile for cart in self.carts.values() if cart.status in {"parked", "reserved", "moving", "loading"}
        }
        occupied_tiles.update(excluded_tiles)
        for cart in self.carts.values():
            if cart.reserved_parking_spot_id in self.map.cart_parking_spots:
                occupied_tiles.add(self.map.cart_parking_spots[cart.reserved_parking_spot_id].tile)
        candidate_spots = [spot for spot in self.map.cart_parking_spots.values() if spot.tile not in occupied_tiles]
        if not candidate_spots:
            candidate_spots = [
                spot for spot in self.map.cart_parking_spots.values() if spot.tile not in excluded_tiles
            ] or list(self.map.cart_parking_spots.values())
        best_spot = None
        best_score = 10**9
        for spot in candidate_spots:
            score = sum(abs(spot.tile[0] - tile[0]) + abs(spot.tile[1] - tile[1]) for tile in target_tiles)
            if score < best_score:
                best_score = score
                best_spot = spot
        return best_spot

    def _cart_with_supply_for_tile(self, work_tile: ShipWorkTile, item_type: str) -> ShipyardCart | None:
        carts = [
            cart
            for cart in self.carts.values()
            if cart.inventory_kind == item_type
            and cart.available_count > 0
            and cart.status == "parked"
            and bool(cart.parking_spot_id)
        ]
        if not carts:
            return None
        service = self.map.work_tile_service_tile(work_tile.work_tile_id, work_tile.tile)
        return min(carts, key=lambda cart: abs(cart.tile[0] - service[0]) + abs(cart.tile[1] - service[1]))

    def _other_cart_tiles(self, cart: ShipyardCart) -> set[tuple[int, int]]:
        occupied: set[tuple[int, int]] = set()
        for other in self.carts.values():
            if other.cart_id == cart.cart_id:
                continue
            occupied.update(self._cart_footprint_tiles(other))
        return occupied

    def _battery_rack_tile(self) -> tuple[int, int]:
        return self.map.objects["battery_rack"].center()

    def _battery_delivery_destination(self, receiver: Worker) -> tuple[int, int]:
        # Delivery meets the receiver at its active movement destination. This
        # keeps the provider from chasing a moving worker tile-by-tile.
        return (
            receiver.movement_target_tile
            or receiver.tile
            or self.map.initial_worker_tile(receiver.worker_id)
        )

    def _battery_task(self, worker_id: str, *, action: str, receiver_id: str = "") -> Task:
        task_id = self._next_task_id("MANAGE_ROBOT_POWER")
        target_tile = self._battery_rack_tile()
        target = "battery_rack"
        if action == "battery_delivery" and receiver_id in self.workers:
            target_tile = self._battery_delivery_destination(self.workers[receiver_id])
            target = f"battery_delivery:{receiver_id}"
        payload = {
            "power_action": action,
            "receiver_id": receiver_id,
            "source": "battery_rack",
            "target": target,
            "target_tile": self.map.tile_payload(target_tile),
            "battery_low_threshold_min": self._battery_low_threshold_min(),
        }
        return Task(
            task_id=task_id,
            task_type="MANAGE_ROBOT_POWER",
            priority_key="battery",
            priority=0.0,
            location=target,
            task_code="MANAGE_ROBOT_POWER",
            instance_id=task_id,
            assigned_robot_id=worker_id,
            payload=payload,
            args={
                "robot": receiver_id if action == "battery_delivery" else worker_id,
                "action": action,
                "station": "battery_rack",
                "target_soc": 1.0,
            },
        )

    def _battery_candidates_for_worker(self, worker: Worker, allowed: list[str]) -> list[Task]:
        if allowed and "MANAGE_ROBOT_POWER" not in allowed:
            return []
        candidates: list[Task] = []
        if self._battery_is_low(worker.worker_id) and not worker.battery_service_owner and not worker.awaiting_battery_from:
            candidates.append(self._battery_task(worker.worker_id, action="self_swap"))
            return candidates
        if worker.worker_id in self.battery_delivery_provider_agent_ids:
            for receiver_id in self.battery_delivery_receiver_agent_ids:
                if receiver_id == worker.worker_id or receiver_id not in self.workers:
                    continue
                receiver = self.workers[receiver_id]
                if not self._battery_is_low(receiver_id):
                    continue
                if receiver.battery_service_owner and receiver.battery_service_owner != worker.worker_id:
                    continue
                candidates.append(self._battery_task(worker.worker_id, action="battery_delivery", receiver_id=receiver_id))
        return candidates

    def _pop_queued_self_battery_task(self, worker: Worker) -> Task | None:
        if not self._battery_is_low(worker.worker_id) or worker.battery_service_owner or worker.awaiting_battery_from:
            return None
        queue = self.rolling_horizon_dispatch_queues.get(worker.worker_id, [])
        for index, entry in enumerate(queue):
            task = entry["task"]
            if task.task_code != "MANAGE_ROBOT_POWER":
                continue
            if str(task.payload.get("power_action", "self_swap")) != "self_swap":
                continue
            queue.pop(index)
            entry["status"] = "started"
            entry["last_updated"] = float(self.env.now)
            self._rolling_horizon_log_entry("ROLLING_HORIZON_TASK_STARTED", entry, worker_id=worker.worker_id)
            return task
        return None

    def _battery_guard_task(self, worker: Worker) -> Task | None:
        allowed = self._allowed_task_codes(worker.worker_id)
        if allowed and "MANAGE_ROBOT_POWER" not in allowed:
            return None
        if not self._battery_is_low(worker.worker_id):
            return None
        if worker.battery_service_owner or worker.awaiting_battery_from:
            return None
        task = self._battery_task(worker.worker_id, action="self_swap")
        entry = {
            "opportunity_id": f"battery-guard:{task.task_id}",
            "task": task,
            "task_id": task.task_id,
            "task_code": task.task_code,
            "resource_key": self._resource_key(task),
            "allowed_worker_ids": [worker.worker_id],
            "assigned_worker_id": worker.worker_id,
            "first_seen": float(self.env.now),
            "first_window_index": self.rolling_horizon_window_index,
            "last_updated": float(self.env.now),
            "status": "dispatched",
        }
        self._rolling_horizon_log_entry("ROLLING_HORIZON_BATTERY_GUARD_DISPATCH", entry, worker_id=worker.worker_id)
        return task

    def _reserve_battery_task(self, worker: Worker, task: Task) -> None:
        if task.task_code != "MANAGE_ROBOT_POWER":
            return
        action = str(task.payload.get("power_action", "self_swap"))
        if action == "battery_delivery":
            receiver_id = str(task.payload.get("receiver_id", ""))
            if receiver_id in self.workers:
                self.workers[receiver_id].battery_service_owner = worker.worker_id
                self.workers[receiver_id].awaiting_battery_from = worker.worker_id
        worker.battery_service_owner = worker.worker_id

    def _reserve_cart_task(self, worker: Worker, task: Task) -> None:
        if task.payload.get("_cart_reserved"):
            return
        if task.task_code == "OPERATE_VEHICLE_TRANSPORT":
            cart = self.carts.get(str(task.payload.get("vehicle_id", "")))
            if cart is None:
                return
            cart.assigned_task_id = task.task_id
            cart.owner = worker.worker_id
            cart.status = "reserved"
            cart.reserved_parking_spot_id = str(task.payload.get("parking_spot_id", ""))
            task.payload["_cart_reserved"] = True
            self._emit_cart_state(cart)
            return
        if task.task_code == "TRANSFER" and task.payload.get("transfer_kind") == "cart_supply":
            cart = self.carts.get(str(task.payload.get("source_cart_id", "")))
            if cart is None:
                return
            cart.reserved_count = min(cart.inventory_count, cart.reserved_count + 1)
            task.payload["_cart_reserved"] = True
            self._emit_cart_state(cart)

    def _release_cart_task(self, task: Task) -> None:
        if not task.payload.get("_cart_reserved"):
            return
        if task.task_code == "OPERATE_VEHICLE_TRANSPORT":
            cart = self.carts.get(str(task.payload.get("vehicle_id", "")))
            if cart is not None and cart.assigned_task_id == task.task_id:
                cart.assigned_task_id = None
                cart.owner = None
                cart.status = "parked"
                cart.reserved_parking_spot_id = ""
                self._emit_cart_state(cart)
        elif task.task_code == "TRANSFER" and task.payload.get("transfer_kind") == "cart_supply":
            cart = self.carts.get(str(task.payload.get("source_cart_id", "")))
            if cart is not None:
                cart.reserved_count = max(0, cart.reserved_count - 1)
                self._emit_cart_state(cart)
        task.payload.pop("_cart_reserved", None)

    def _task_for_work_tile(self, work_tile: ShipWorkTile) -> tuple[str | None, dict[str, Any]]:
        if work_tile.state == "WAIT_WELD":
            if not work_tile.weld_supply_ready:
                payload = self._cart_supply_payload(work_tile, "weld_wire")
                return ("TRANSFER", payload) if payload else (None, {})
            return "WELD_SEAM", {}
        if work_tile.state == "WELDED":
            return "PREPARE_SURFACE", {}
        if work_tile.state == "SURFACE_PREPARED":
            if not work_tile.paint_supply_ready:
                payload = self._cart_supply_payload(work_tile, "paint_can")
                return ("TRANSFER", payload) if payload else (None, {})
            return "PAINT_SURFACE", {}
        if work_tile.state == "PAINTED":
            return "VERIFY_SHIP_SECTION", {}
        if work_tile.state == "REWORK_REQUIRED":
            if work_tile.rework_target == "weld":
                if not work_tile.weld_supply_ready:
                    payload = self._cart_supply_payload(work_tile, "weld_wire")
                    return ("TRANSFER", payload) if payload else (None, {})
                return "WELD_SEAM", {}
            if not work_tile.paint_supply_ready:
                payload = self._cart_supply_payload(work_tile, "paint_can")
                return ("TRANSFER", payload) if payload else (None, {})
            return "PAINT_SURFACE", {}
        return None, {}

    @staticmethod
    def _work_tile_needs_cart_supply(work_tile: ShipWorkTile, item_type: str) -> bool:
        if item_type == "weld_wire":
            return (
                work_tile.state == "WAIT_WELD"
                or (work_tile.state == "REWORK_REQUIRED" and work_tile.rework_target == "weld")
            ) and not work_tile.weld_supply_ready
        if item_type == "paint_can":
            return (
                work_tile.state == "SURFACE_PREPARED"
                or (work_tile.state == "REWORK_REQUIRED" and work_tile.rework_target != "weld")
            ) and not work_tile.paint_supply_ready
        return False

    def _cart_supply_payload(self, work_tile: ShipWorkTile, item_type: str) -> dict[str, Any]:
        cart = self._cart_with_supply_for_tile(work_tile, item_type)
        if cart is None:
            return {}
        return {
            "transfer_kind": "cart_supply",
            "item_type": item_type,
            "source": cart.cart_id,
            "source_cart_id": cart.cart_id,
            "parking_spot_id": cart.parking_spot_id,
            "destination": work_tile.entity_id,
        }

    def _allowed_task_codes(self, worker_id: str) -> list[str]:
        decision_cfg = self.cfg.get("decision", {}) if isinstance(self.cfg.get("decision", {}), dict) else {}
        rolling = decision_cfg.get("rolling_horizon", {}) if isinstance(decision_cfg.get("rolling_horizon", {}), dict) else {}
        scenario_roles = _scenario_entry(rolling.get("scenario_worker_task_priority", {}), _scenario_key(self.cfg))
        role_map = (
            scenario_roles
            if isinstance(scenario_roles, dict)
            else rolling.get("worker_task_priority", {})
            if isinstance(rolling.get("worker_task_priority", {}), dict)
            else {}
        )
        if role_map:
            return [str(code).upper() for code in role_map.get(worker_id, []) if str(code).strip()]
        return []

    def _task_rank(self, worker_id: str, task_code: str) -> int:
        if task_code == "MANAGE_ROBOT_POWER":
            return 0
        allowed = self._allowed_task_codes(worker_id)
        if allowed and task_code in allowed:
            return allowed.index(task_code) + 1
        decision_cfg = self.cfg.get("decision", {}) if isinstance(self.cfg.get("decision", {}), dict) else {}
        rolling = decision_cfg.get("rolling_horizon", {}) if isinstance(decision_cfg.get("rolling_horizon", {}), dict) else {}
        scenario_order = _scenario_entry(rolling.get("scenario_task_code_priority_order", {}), _scenario_key(self.cfg))
        raw_order = scenario_order if isinstance(scenario_order, list) else rolling.get("task_code_priority_order", [])
        order = [str(code).upper() for code in raw_order if str(code).strip()]
        return order.index(task_code) + 1 if task_code in order else 999

    def execute_task(self, worker: Worker, task: Task) -> Any:
        if task.task_code == "MANAGE_ROBOT_POWER":
            yield from self._execute_battery_task(worker, task)
            return
        if task.task_code == "OPERATE_VEHICLE_TRANSPORT":
            yield from self._execute_cart_transport_task(worker, task)
            return
        work_tile_id = str(task.payload["work_tile_id"])
        work_tile = self.work_tiles[work_tile_id]
        task_code = task.task_code
        worker.current_task_id = task.task_id
        worker.current_task_code = task_code
        worker.current_task_instance_id = task.instance_id
        worker.current_task_payload = dict(task.payload)

        self._transition(
            worker,
            {
                "event_type": "task_assigned",
                "task_code": task_code,
                "task_instance_id": task.instance_id,
                "metadata": {"task_id": task.task_id, "source": "shipyard_world"},
            },
        )
        self._transition(
            worker,
            {
                "event_type": "task_started",
                "task_code": task_code,
                "task_instance_id": task.instance_id,
                "metadata": {"task_id": task.task_id, "source": "shipyard_world"},
            },
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_TASK_START",
            entity_id=worker.worker_id,
            location=worker.location,
            details={
                "task_id": task.task_id,
                "task_type": task_code,
                "task_code": task_code,
                "instance_id": task.instance_id,
                "payload": task.payload,
                "priority_key": task.priority_key,
            },
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="HUMANOID_TASK_START",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "depth": 0},
        )

        if task_code == "TRANSFER":
            source = self._transfer_source_tile(task)
            if source is not None:
                source_name, source_tile = source
                yield from self._move_worker(worker, source_tile, task, target_location=source_name)
                self._log_resource_pickup(worker, task, source_name)

        destination = self.map.work_tile_service_tile(work_tile_id, worker.tile or self.map.initial_worker_tile(worker.worker_id))
        yield from self._move_worker(worker, destination, task)
        started = float(self.env.now)
        work_tile.started_at = work_tile.started_at if work_tile.started_at is not None else started

        step_rows = [row for row in expand_task_steps(task_code, task.args) if str(row.get("call_level")) == "PRIMITIVE_SKILL"]
        duration = self._operation_duration(task_code, work_tile_id)
        step_duration = duration / max(1, len(step_rows))
        for row in step_rows:
            call_code = str(row["call_code"])
            step_id = str(row["step_id"])
            worker.current_step_id = step_id
            worker.current_primitive_call_code = call_code
            self._transition(
                worker,
                {
                    "event_type": "primitive_started",
                    "task_code": task_code,
                    "task_instance_id": task.instance_id,
                    "step_id": step_id,
                    "primitive_call_code": call_code,
                    "metadata": {"task_id": task.task_id, "source": "shipyard_world"},
                },
            )
            yield self.env.timeout(step_duration)
            self._transition(
                worker,
                {
                    "event_type": "primitive_finished",
                    "task_code": task_code,
                    "task_instance_id": task.instance_id,
                    "step_id": step_id,
                    "primitive_call_code": call_code,
                    "metadata": {"task_id": task.task_id, "source": "shipyard_world"},
                },
            )

        self._apply_task_result(work_tile, task_code, worker.worker_id, task.task_id)
        if task_code == "TRANSFER":
            self._log_resource_dropoff(worker, task, work_tile)
        elapsed = float(self.env.now) - started
        self.worker_busy_min[worker.worker_id] += max(0.0, elapsed)
        self.worker_task_minutes[worker.worker_id][task_code] = self.worker_task_minutes[worker.worker_id].get(task_code, 0.0) + max(0.0, elapsed)
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="HUMANOID_TASK_END",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "status": "completed", "depth": 0},
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_TASK_END",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_type": task_code, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "status": "completed"},
        )
        self._transition(
            worker,
            {
                "event_type": "task_completed",
                "task_code": task_code,
                "task_instance_id": task.instance_id,
                "metadata": {"task_id": task.task_id, "source": "shipyard_world"},
            },
        )
        worker.current_task_id = None
        worker.current_task_code = None
        worker.current_task_instance_id = None
        worker.current_step_id = None
        worker.current_primitive_call_code = None
        worker.current_task_payload = {}
        if work_tile.owner == worker.worker_id:
            work_tile.owner = None

    def _execute_cart_transport_task(self, worker: Worker, task: Task) -> Any:
        cart_id = str(task.payload.get("vehicle_id", ""))
        cart = self.carts.get(cart_id)
        if cart is None:
            return
        task_code = task.task_code
        started = float(self.env.now)
        worker.current_task_id = task.task_id
        worker.current_task_code = task_code
        worker.current_task_instance_id = task.instance_id
        worker.current_task_payload = dict(task.payload)

        for event_type in ("task_assigned", "task_started"):
            self._transition(
                worker,
                {
                    "event_type": event_type,
                    "task_code": task_code,
                    "task_instance_id": task.instance_id,
                    "metadata": {"task_id": task.task_id, "source": "shipyard_world", "vehicle_id": cart_id},
                },
            )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_TASK_START",
            entity_id=worker.worker_id,
            location=worker.location,
            details={
                "task_id": task.task_id,
                "task_type": task_code,
                "task_code": task_code,
                "instance_id": task.instance_id,
                "payload": task.payload,
                "priority_key": task.priority_key,
            },
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="HUMANOID_TASK_START",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "depth": 0},
        )

        cart.status = "reserved"
        cart.owner = worker.worker_id
        cart.assigned_task_id = task.task_id
        if cart.busy_started_at is None:
            cart.busy_started_at = float(self.env.now)
        self._emit_cart_state(cart)

        yield from self._move_worker(worker, cart.tile, task, target_location=cart.cart_id)

        source = str(task.payload.get("source", ""))
        source_tile = self.map.cart_source_tile(source) or cart.tile
        yield from self._move_worker(
            worker,
            source_tile,
            task,
            target_location=source,
            cart=cart,
            path_override=self.map.find_cart_route_path(
                cart.tile,
                source_tile,
                blocked_tiles=self._other_cart_tiles(cart),
                footprint_tiles=self.cart_footprint_tiles,
            ),
        )

        item_type = str(task.payload.get("item_type", ""))
        batch_count = min(self.cart_capacity, max(1, int(task.payload.get("batch_count", self.cart_capacity) or self.cart_capacity)))
        cart.status = "loading"
        self._emit_cart_state(cart)
        yield self.env.timeout(max(0.1, self._operation_duration(task_code, "") * 0.25))
        cart.inventory_kind = item_type
        cart.inventory_count = batch_count
        cart.reserved_count = 0
        cart.trip_count += 1
        cart.items_moved += batch_count
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="CART_BATCH_LOADED",
            entity_id=cart.cart_id,
            location=source,
            details={
                "cart_id": cart.cart_id,
                "task_id": task.task_id,
                "worker_id": worker.worker_id,
                "item_type": item_type,
                "batch_count": batch_count,
                "source": source,
                "capacity": self.cart_capacity,
            },
        )
        self._emit_cart_state(cart)

        parking_id = str(task.payload.get("parking_spot_id", ""))
        parking = self.map.cart_parking_spots.get(parking_id)
        parking_tile = parking.tile if parking is not None else cart.tile
        yield from self._move_worker(
            worker,
            parking_tile,
            task,
            target_location=parking_id or "cart_parking",
            cart=cart,
            path_override=self.map.find_cart_route_path(
                cart.tile,
                parking_tile,
                blocked_tiles=self._other_cart_tiles(cart),
                footprint_tiles=self.cart_footprint_tiles,
            ),
        )

        step_rows = [row for row in expand_task_steps(task_code, task.args) if str(row.get("call_level")) == "PRIMITIVE_SKILL"]
        duration = max(0.1, self._operation_duration(task_code, "") * 0.75)
        step_duration = duration / max(1, len(step_rows))
        for row in step_rows:
            call_code = str(row["call_code"])
            step_id = str(row["step_id"])
            worker.current_step_id = step_id
            worker.current_primitive_call_code = call_code
            self._transition(
                worker,
                {
                    "event_type": "primitive_started",
                    "task_code": task_code,
                    "task_instance_id": task.instance_id,
                    "step_id": step_id,
                    "primitive_call_code": call_code,
                    "metadata": {"task_id": task.task_id, "source": "shipyard_world", "vehicle_id": cart_id},
                },
            )
            yield self.env.timeout(step_duration)
            self._transition(
                worker,
                {
                    "event_type": "primitive_finished",
                    "task_code": task_code,
                    "task_instance_id": task.instance_id,
                    "step_id": step_id,
                    "primitive_call_code": call_code,
                    "metadata": {"task_id": task.task_id, "source": "shipyard_world", "vehicle_id": cart_id},
                },
            )

        cart.status = "parked"
        cart.parking_spot_id = parking_id
        cart.heading = self._cart_heading_at_tile(cart.tile, cart.heading)
        cart.reserved_parking_spot_id = ""
        cart.owner = None
        cart.assigned_task_id = None
        if cart.busy_started_at is not None:
            cart.busy_total_min += max(0.0, float(self.env.now) - cart.busy_started_at)
            cart.busy_started_at = None
        task.payload.pop("_cart_reserved", None)
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="CART_PARKED",
            entity_id=cart.cart_id,
            location=parking_id or "cart_parking",
            details={
                "cart_id": cart.cart_id,
                "task_id": task.task_id,
                "worker_id": worker.worker_id,
                "parking_spot_id": parking_id,
                "tile": self.map.tile_payload(cart.tile),
                "cockpit_tile": self.map.tile_payload(cart.tile),
                "cargo_tile": self.map.tile_payload(self._cart_footprint_tiles(cart)[1] if self.cart_footprint_tiles > 1 else None),
                "heading": {"x": int(cart.heading[0]), "y": int(cart.heading[1])},
                "footprint_tiles": self.map.path_payload(self._cart_footprint_tiles(cart)),
                "inventory_kind": cart.inventory_kind,
                "inventory_count": cart.inventory_count,
            },
        )
        self._emit_cart_state(cart)

        elapsed = max(0.0, float(self.env.now) - started)
        self.worker_busy_min[worker.worker_id] += elapsed
        self.worker_task_minutes[worker.worker_id][task_code] = self.worker_task_minutes[worker.worker_id].get(task_code, 0.0) + elapsed
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="HUMANOID_TASK_END",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "status": "completed", "depth": 0},
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_TASK_END",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_type": task_code, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "status": "completed"},
        )
        self._transition(
            worker,
            {
                "event_type": "task_completed",
                "task_code": task_code,
                "task_instance_id": task.instance_id,
                "metadata": {"task_id": task.task_id, "source": "shipyard_world", "vehicle_id": cart_id},
            },
        )
        worker.current_task_id = None
        worker.current_task_code = None
        worker.current_task_instance_id = None
        worker.current_step_id = None
        worker.current_primitive_call_code = None
        worker.current_task_payload = {}

    def _execute_battery_task(self, worker: Worker, task: Task) -> Any:
        task_code = task.task_code
        action = str(task.payload.get("power_action", "self_swap"))
        receiver_id = str(task.payload.get("receiver_id", ""))
        started = float(self.env.now)
        worker.current_task_id = task.task_id
        worker.current_task_code = task_code
        worker.current_task_instance_id = task.instance_id
        worker.current_task_payload = dict(task.payload)

        for event_type in ("task_assigned", "task_started"):
            self._transition(
                worker,
                {
                    "event_type": event_type,
                    "task_code": task_code,
                    "task_instance_id": task.instance_id,
                    "metadata": {"task_id": task.task_id, "source": "shipyard_world", "power_action": action},
                },
            )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_TASK_START",
            entity_id=worker.worker_id,
            location=worker.location,
            details={
                "task_id": task.task_id,
                "task_type": task_code,
                "task_code": task_code,
                "instance_id": task.instance_id,
                "payload": task.payload,
                "priority_key": task.priority_key,
            },
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="HUMANOID_TASK_START",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "depth": 0},
        )

        task.payload["target"] = "battery_rack"
        task.payload["target_tile"] = self.map.tile_payload(self._battery_rack_tile())
        worker.current_task_payload = dict(task.payload)
        yield from self._move_worker(worker, self._battery_rack_tile(), task)

        if action == "battery_delivery" and receiver_id in self.workers:
            receiver = self.workers[receiver_id]
            destination = self._battery_delivery_destination(receiver)
            task.payload["target"] = f"battery_delivery:{receiver_id}"
            task.payload["target_tile"] = self.map.tile_payload(destination)
            worker.current_task_payload = dict(task.payload)
            yield from self._move_worker(worker, destination, task)
            while receiver.tile != destination:
                if self._worker_battery_remaining_min(receiver_id) <= 0.0:
                    destination = receiver.tile or destination
                    task.payload["target_tile"] = self.map.tile_payload(destination)
                    worker.current_task_payload = dict(task.payload)
                    if worker.tile != destination:
                        self.logger.log(
                            t=self.env.now,
                            day=self.day_index(),
                            event_type="BATTERY_DELIVERY_RETARGET",
                            entity_id=worker.worker_id,
                            location=worker.location,
                            details={
                                "task_id": task.task_id,
                                "receiver_id": receiver_id,
                                "retarget_tile": self.map.tile_payload(destination),
                                "reason": "receiver_depleted_before_meet_point",
                            },
                        )
                        yield from self._move_worker(worker, destination, task)
                    break
                self.logger.log(
                    t=self.env.now,
                    day=self.day_index(),
                    event_type="BATTERY_DELIVERY_WAIT",
                    entity_id=worker.worker_id,
                    location=worker.location,
                    details={
                        "task_id": task.task_id,
                        "receiver_id": receiver_id,
                        "wait_tile": self.map.tile_payload(destination),
                        "receiver_tile": self.map.tile_payload(receiver.tile),
                        "duration": self.map.tile_time_min,
                    },
                )
                yield self.env.timeout(self.map.tile_time_min)

        step_rows = [row for row in expand_task_steps(task_code, task.args) if str(row.get("call_level")) == "PRIMITIVE_SKILL"]
        duration = self.battery_delivery_extra_min if action == "battery_delivery" else self.battery_pickup_time_min
        step_duration = duration / max(1, len(step_rows))
        for row in step_rows:
            call_code = str(row["call_code"])
            step_id = str(row["step_id"])
            worker.current_step_id = step_id
            worker.current_primitive_call_code = call_code
            self._transition(
                worker,
                {
                    "event_type": "primitive_started",
                    "task_code": task_code,
                    "task_instance_id": task.instance_id,
                    "step_id": step_id,
                    "primitive_call_code": call_code,
                    "metadata": {"task_id": task.task_id, "source": "shipyard_world", "power_action": action},
                },
            )
            yield self.env.timeout(step_duration)
            self._transition(
                worker,
                {
                    "event_type": "primitive_finished",
                    "task_code": task_code,
                    "task_instance_id": task.instance_id,
                    "step_id": step_id,
                    "primitive_call_code": call_code,
                    "metadata": {"task_id": task.task_id, "source": "shipyard_world", "power_action": action},
                },
            )

        serviced_worker_id = receiver_id if action == "battery_delivery" and receiver_id in self.workers else worker.worker_id
        if serviced_worker_id in self.workers:
            serviced_worker = self.workers[serviced_worker_id]
            serviced_worker.last_battery_swap = float(self.env.now)
            serviced_worker.battery_service_owner = None
            serviced_worker.awaiting_battery_from = None
            self._emit_worker_state(serviced_worker)
        worker.battery_service_owner = None

        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="BATTERY_SERVICE_COMPLETED",
            entity_id=worker.worker_id,
            location=worker.location,
            details={
                "task_id": task.task_id,
                "task_code": task_code,
                "power_action": action,
                "receiver_id": serviced_worker_id,
                "battery_remaining_min": self._worker_battery_remaining_min(serviced_worker_id),
                "battery_period_min": self.battery_period_min,
            },
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="HUMANOID_TASK_END",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "status": "completed", "depth": 0},
        )
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_TASK_END",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"task_id": task.task_id, "task_type": task_code, "task_code": task_code, "instance_id": task.instance_id, "payload": task.payload, "status": "completed"},
        )
        self._transition(
            worker,
            {
                "event_type": "task_completed",
                "task_code": task_code,
                "task_instance_id": task.instance_id,
                "metadata": {"task_id": task.task_id, "source": "shipyard_world", "power_action": action},
            },
        )
        elapsed = max(0.0, float(self.env.now) - started)
        self.worker_busy_min[worker.worker_id] += elapsed
        self.worker_task_minutes[worker.worker_id][task_code] = self.worker_task_minutes[worker.worker_id].get(task_code, 0.0) + elapsed
        worker.current_task_id = None
        worker.current_task_code = None
        worker.current_task_instance_id = None
        worker.current_step_id = None
        worker.current_primitive_call_code = None
        worker.current_task_payload = {}

    def _move_worker(
        self,
        worker: Worker,
        destination: tuple[int, int],
        task: Task,
        *,
        target_location: str | None = None,
        cart: ShipyardCart | None = None,
        path_override: list[tuple[int, int]] | None = None,
    ) -> Any:
        start = worker.tile or self.map.initial_worker_tile(worker.worker_id)
        path = path_override if path_override is not None else self.map.find_path(start, destination)
        duration = max(0.0, (len(path) - 1) * self.map.tile_time_min)
        if duration <= 0.0:
            return
        move_id = f"{worker.worker_id}-{task.task_id}-{int(self.env.now * 1000)}"
        target_location = str(target_location or task.payload.get("target", "ShipDock"))
        worker.movement_path = list(path)
        worker.movement_target_tile = destination
        worker.current_move_id = move_id
        worker.current_move_started_at = float(self.env.now)
        worker.current_move_logical_destination = target_location
        self._transition(
            worker,
            {
                "event_type": "primitive_started",
                "task_code": task.task_code,
                "task_instance_id": task.instance_id,
                "step_id": "navigate_to_work_tile",
                "primitive_call_code": "NAVIGATE_TO",
                "metadata": {"task_id": task.task_id, "source": "shipyard_world"},
            },
        )
        # The full move event preserves the overview path, while the per-tile
        # events below update the worker position one adjacent tile at a time.
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_MOVE_START",
            entity_id=worker.worker_id,
            location=worker.location,
            details={
                "from": worker.location,
                "to": target_location,
                "from_tile": self.map.tile_payload(start),
                "to_tile": self.map.tile_payload(destination),
                "path_tiles": self.map.path_payload(path),
                "duration": duration,
                "move_id": move_id,
            },
        )
        if cart is not None:
            cart.status = "moving"
            cart.owner = worker.worker_id
            self.logger.log(
                t=self.env.now,
                day=self.day_index(),
                event_type="CART_MOVE_START",
                entity_id=cart.cart_id,
                location=cart.parking_spot_id or "cart_route",
                details={
                    "cart_id": cart.cart_id,
                    "worker_id": worker.worker_id,
                    "task_id": task.task_id,
                    "from_tile": self.map.tile_payload(start),
                    "to_tile": self.map.tile_payload(destination),
                    "heading": {"x": int(cart.heading[0]), "y": int(cart.heading[1])},
                    "footprint_tiles": self.map.path_payload(self._cart_footprint_tiles(cart)),
                    "path_tiles": self.map.path_payload(path),
                    "duration": duration,
                    "move_id": move_id,
                },
            )
            self._emit_cart_state(cart)
        segment_duration = max(0.0, self.map.tile_time_min)
        segment_count = max(0, len(path) - 1)
        for segment_index, (from_tile, to_tile) in enumerate(zip(path, path[1:]), start=1):
            yield from self._wait_for_battery_delivery_if_depleted(worker, task, move_id)
            next_heading = self._cart_step_heading(from_tile, to_tile, cart.heading if cart is not None else (0, 1))
            next_footprint = self._cart_footprint_tiles(cart, tile=to_tile, heading=next_heading) if cart is not None else []
            if cart is not None:
                while any(
                    other.cart_id != cart.cart_id and set(next_footprint).intersection(self._cart_footprint_tiles(other))
                    for other in self.carts.values()
                ):
                    self.cart_collision_wait_count += 1
                    self.cart_wait_time_min += segment_duration
                    self.logger.log(
                        t=self.env.now,
                        day=self.day_index(),
                        event_type="CART_TRAFFIC_WAIT",
                        entity_id=cart.cart_id,
                        location=cart.parking_spot_id or "cart_route",
                        details={
                            "cart_id": cart.cart_id,
                            "worker_id": worker.worker_id,
                            "task_id": task.task_id,
                            "blocked_tile": self.map.tile_payload(to_tile),
                            "blocked_footprint_tiles": self.map.path_payload(next_footprint),
                            "duration": segment_duration,
                            "move_id": move_id,
                        },
                    )
                    yield self.env.timeout(segment_duration)
            segment_started_at = float(self.env.now)
            battery_remaining = self._worker_battery_remaining_min(worker.worker_id)
            self.logger.log(
                t=self.env.now,
                day=self.day_index(),
                event_type="AGENT_MOVE_TILE_START",
                entity_id=worker.worker_id,
                location=worker.location,
                details={
                    "from": worker.location,
                    "to": target_location,
                    "from_tile": self.map.tile_payload(from_tile),
                    "to_tile": self.map.tile_payload(to_tile),
                    "started_at": segment_started_at,
                    "ended_at": segment_started_at + segment_duration,
                    "duration": segment_duration,
                    "move_id": move_id,
                    "segment_index": segment_index,
                    "segment_count": segment_count,
                    "humanoid_state": worker.humanoid_state,
                    "battery_remaining_min": battery_remaining,
                    "battery_period_min": self.battery_period_min,
                },
            )
            if cart is not None:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_index(),
                    event_type="CART_MOVE_TILE_START",
                    entity_id=cart.cart_id,
                    location=cart.parking_spot_id or "cart_route",
                    details={
                        "cart_id": cart.cart_id,
                        "worker_id": worker.worker_id,
                        "task_id": task.task_id,
                        "from_tile": self.map.tile_payload(from_tile),
                        "to_tile": self.map.tile_payload(to_tile),
                        "cockpit_tile": self.map.tile_payload(to_tile),
                        "cargo_tile": self.map.tile_payload(next_footprint[1] if len(next_footprint) > 1 else None),
                        "heading": {"x": int(next_heading[0]), "y": int(next_heading[1])},
                        "footprint_tiles": self.map.path_payload(next_footprint),
                        "started_at": segment_started_at,
                        "ended_at": segment_started_at + segment_duration,
                        "duration": segment_duration,
                        "move_id": move_id,
                        "segment_index": segment_index,
                        "segment_count": segment_count,
                    },
                )
            yield self.env.timeout(segment_duration)
            worker.tile = to_tile
            if cart is not None:
                cart.tile = to_tile
                cart.heading = next_heading
                cart.parking_spot_id = ""
            if segment_index == segment_count:
                worker.location = target_location
            battery_remaining = self._worker_battery_remaining_min(worker.worker_id)
            self.logger.log(
                t=self.env.now,
                day=self.day_index(),
                event_type="AGENT_MOVE_TILE_END",
                entity_id=worker.worker_id,
                location=worker.location,
                details={
                    "from": worker.location if segment_index == segment_count else "in_transit",
                    "to": target_location,
                    "from_tile": self.map.tile_payload(from_tile),
                    "to_tile": self.map.tile_payload(to_tile),
                    "tile": self.map.tile_payload(to_tile),
                    "move_id": move_id,
                    "segment_index": segment_index,
                    "segment_count": segment_count,
                    "humanoid_state": worker.humanoid_state,
                    "battery_remaining_min": battery_remaining,
                    "battery_period_min": self.battery_period_min,
                },
            )
            if cart is not None:
                self.logger.log(
                    t=self.env.now,
                    day=self.day_index(),
                    event_type="CART_MOVE_TILE_END",
                    entity_id=cart.cart_id,
                    location=cart.parking_spot_id or "cart_route",
                    details={
                        "cart_id": cart.cart_id,
                        "worker_id": worker.worker_id,
                        "task_id": task.task_id,
                        "from_tile": self.map.tile_payload(from_tile),
                        "to_tile": self.map.tile_payload(to_tile),
                        "tile": self.map.tile_payload(to_tile),
                        "cockpit_tile": self.map.tile_payload(to_tile),
                        "cargo_tile": self.map.tile_payload(next_footprint[1] if len(next_footprint) > 1 else None),
                        "heading": {"x": int(cart.heading[0]), "y": int(cart.heading[1])},
                        "footprint_tiles": self.map.path_payload(self._cart_footprint_tiles(cart)),
                        "move_id": move_id,
                        "segment_index": segment_index,
                        "segment_count": segment_count,
                    },
                )
                self._emit_cart_state(cart)
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_MOVE_END",
            entity_id=worker.worker_id,
            location=worker.location,
            details={"tile": self.map.tile_payload(destination), "move_id": move_id, "to": target_location},
        )
        if cart is not None:
            self.logger.log(
                t=self.env.now,
                day=self.day_index(),
                event_type="CART_MOVE_END",
                entity_id=cart.cart_id,
                location=cart.parking_spot_id or "cart_route",
                details={
                    "cart_id": cart.cart_id,
                    "worker_id": worker.worker_id,
                    "task_id": task.task_id,
                    "tile": self.map.tile_payload(destination),
                    "cockpit_tile": self.map.tile_payload(cart.tile),
                    "cargo_tile": self.map.tile_payload(self._cart_footprint_tiles(cart)[1] if self.cart_footprint_tiles > 1 else None),
                    "heading": {"x": int(cart.heading[0]), "y": int(cart.heading[1])},
                    "footprint_tiles": self.map.path_payload(self._cart_footprint_tiles(cart)),
                    "move_id": move_id,
                    "to": target_location,
                },
            )
        self._transition(
            worker,
            {
                "event_type": "primitive_finished",
                "task_code": task.task_code,
                "task_instance_id": task.instance_id,
                "step_id": "navigate_to_work_tile",
                "primitive_call_code": "NAVIGATE_TO",
                "metadata": {"task_id": task.task_id, "source": "shipyard_world"},
            },
        )
        worker.movement_path = []
        worker.movement_target_tile = None
        worker.current_move_id = None
        worker.current_move_segment_index = 0
        worker.current_move_segment_from_tile = None
        worker.current_move_segment_to_tile = None
        worker.current_move_logical_destination = None
        worker.current_move_started_at = None

    def _wait_for_battery_delivery_if_depleted(self, worker: Worker, task: Task, move_id: str) -> Any:
        minimum_move_budget = max(0.0, self.map.tile_time_min)
        entered_wait = False
        while self._worker_battery_remaining_min(worker.worker_id) <= minimum_move_budget and worker.awaiting_battery_from:
            if not entered_wait:
                self._transition(
                    worker,
                    {
                        "event_type": "waiting",
                        "task_code": task.task_code,
                        "task_instance_id": task.instance_id,
                        "step_id": "navigate_to_work_tile",
                        "primitive_call_code": "NAVIGATE_TO",
                        "metadata": {
                            "task_id": task.task_id,
                            "source": "shipyard_battery_policy",
                            "reason_code": "BATTERY_DELIVERY_WAIT",
                            "provider_id": worker.awaiting_battery_from,
                        },
                    },
                )
                entered_wait = True
            self._emit_worker_state(worker)
            self.logger.log(
                t=self.env.now,
                day=self.day_index(),
                event_type="BATTERY_DEPLETED_WAIT",
                entity_id=worker.worker_id,
                location=worker.location,
                details={
                    "task_id": task.task_id,
                    "task_code": task.task_code,
                    "provider_id": worker.awaiting_battery_from,
                    "tile": self.map.tile_payload(worker.tile),
                    "move_id": move_id,
                    "duration": self.map.tile_time_min,
                    "battery_remaining_min": self._worker_battery_remaining_min(worker.worker_id),
                    "battery_period_min": self.battery_period_min,
                },
            )
            yield self.env.timeout(self.map.tile_time_min)
        if entered_wait:
            self._transition(
                worker,
                {
                    "event_type": "primitive_started",
                    "task_code": task.task_code,
                    "task_instance_id": task.instance_id,
                    "step_id": "navigate_to_work_tile",
                    "primitive_call_code": "NAVIGATE_TO",
                    "metadata": {"task_id": task.task_id, "source": "shipyard_battery_policy", "resumed_from": "BATTERY_DELIVERY_WAIT"},
                },
            )

    def _transfer_source_tile(self, task: Task) -> tuple[str, tuple[int, int]] | None:
        if task.payload.get("transfer_kind") == "cart_supply":
            cart_id = str(task.payload.get("source_cart_id", ""))
            cart = self.carts.get(cart_id)
            return (cart_id, cart.tile) if cart is not None else None
        source = str(task.payload.get("source", "") or "").strip()
        object_by_source = {
            "MaterialYard": "material_yard",
            "PaintSupply": "paint_supply",
            "ScrapArea": "scrap_bin",
        }
        object_id = object_by_source.get(source)
        obj = self.map.objects.get(object_id) if object_id else None
        if obj is None:
            return None
        center = obj.center()
        if self.map.passable(center):
            return source, center
        candidates = [tile for tile in obj.tiles if self.map.passable(tile)]
        if not candidates:
            for radius in range(1, 6):
                for dx in range(-radius, radius + 1):
                    for dy in range(-radius, radius + 1):
                        if abs(dx) + abs(dy) != radius:
                            continue
                        tile = (center[0] + dx, center[1] + dy)
                        if self.map.passable(tile):
                            candidates.append(tile)
                if candidates:
                    break
        if not candidates:
            return None
        return source, min(candidates, key=lambda tile: abs(tile[0] - center[0]) + abs(tile[1] - center[1]))

    def _resource_item_type(self, task: Task) -> str:
        if task.payload.get("transfer_kind") == "cart_supply":
            return str(task.payload.get("item_type", "resource") or "resource")
        kind = str(task.payload.get("transfer_kind", "resource") or "resource").strip()
        if kind == "paint_can":
            return "paint"
        if kind == "weld_wire":
            return "weld_wire"
        return kind or "resource"

    def _log_resource_pickup(self, worker: Worker, task: Task, source: str) -> None:
        item_type = self._resource_item_type(task)
        item_id = f"{item_type.upper()}-{task.task_id}"
        if task.payload.get("transfer_kind") == "cart_supply":
            cart = self.carts.get(str(task.payload.get("source_cart_id", "")))
            if cart is not None:
                cart.inventory_count = max(0, cart.inventory_count - 1)
                if task.payload.get("_cart_reserved"):
                    cart.reserved_count = max(0, cart.reserved_count - 1)
                    task.payload.pop("_cart_reserved", None)
                if cart.inventory_count <= 0:
                    cart.inventory_kind = ""
                    cart.reserved_count = 0
                self._emit_cart_state(cart)
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_PICK_ITEM",
            entity_id=worker.worker_id,
            location=source,
            details={
                "task_id": task.task_id,
                "task_code": task.task_code,
                "item_id": item_id,
                "item_type": item_type,
                "source": source,
                "transfer_kind": task.payload.get("transfer_kind", ""),
            },
        )

    def _log_resource_dropoff(self, worker: Worker, task: Task, work_tile: ShipWorkTile) -> None:
        item_type = self._resource_item_type(task)
        item_id = f"{item_type.upper()}-{task.task_id}"
        self.logger.log(
            t=self.env.now,
            day=self.day_index(),
            event_type="AGENT_DROP_ITEM",
            entity_id=worker.worker_id,
            location=work_tile.entity_id,
            details={
                "task_id": task.task_id,
                "task_code": task.task_code,
                "item_id": item_id,
                "item_type": item_type,
                "destination": work_tile.entity_id,
                "transfer_kind": task.payload.get("transfer_kind", ""),
            },
        )

    def _operation_duration(self, task_code: str, work_tile_id: str) -> float:
        shipyard_cfg = self.cfg.get("shipyard", {}) if isinstance(self.cfg.get("shipyard", {}), dict) else {}
        ops = shipyard_cfg.get("operations", {}) if isinstance(shipyard_cfg.get("operations", {}), dict) else {}
        task_cfg = ops.get(task_code, {}) if isinstance(ops.get(task_code, {}), dict) else {}
        tile_min = task_cfg.get("tile_min", {}) if isinstance(task_cfg.get("tile_min", {}), dict) else {}
        return float(tile_min.get(work_tile_id, task_cfg.get("default_min", 1.0)) or 1.0)

    def _apply_task_result(self, work_tile: ShipWorkTile, task_code: str, worker_id: str, task_id: str) -> None:
        payload: dict[str, Any] = {}
        for worker in self.workers.values():
            if worker.current_task_id == task_id:
                payload = worker.current_task_payload
                break
        previous_state = work_tile.state
        if task_code == "TRANSFER":
            kind = str(payload.get("transfer_kind", "resource"))
            item_type = str(payload.get("item_type", ""))
            if kind == "weld_wire" or item_type == "weld_wire":
                work_tile.weld_supply_ready = True
            elif kind == "paint_can" or item_type == "paint_can":
                work_tile.paint_supply_ready = True
        elif task_code == "WELD_SEAM":
            work_tile.state = "WELDED"
            work_tile.weld_supply_ready = False
            work_tile.paint_supply_ready = False
            work_tile.rework_target = None
        elif task_code == "PREPARE_SURFACE":
            work_tile.state = "SURFACE_PREPARED"
        elif task_code == "PAINT_SURFACE":
            work_tile.state = "PAINTED"
            work_tile.paint_supply_ready = False
        elif task_code == "VERIFY_SHIP_SECTION":
            self.verify_count += 1
            if self.rng.random() < float(self.cfg.get("quality", {}).get("defect_prob", 0.0) or 0.0):
                self.rework_count += 1
                work_tile.state = "REWORK_REQUIRED"
                work_tile.rework_target = "paint" if self.rng.random() < 0.7 else "weld"
                work_tile.weld_supply_ready = False
                work_tile.paint_supply_ready = False
            else:
                self.verify_pass_count += 1
                work_tile.state = "COMPLETE"
                work_tile.completed_at = float(self.env.now)
        if work_tile.state not in SURFACE_TILE_STATES:
            raise ValueError(f"Invalid ship surface tile state: {work_tile.state}")
        reason = f"{task_code}_completed"
        if previous_state != work_tile.state:
            self._emit_work_tile(work_tile, reason=reason, worker_id=worker_id, task_id=task_id)

    def _snapshot_loop(self) -> Any:
        interval = float(self.cfg.get("dispatcher", {}).get("snapshot_interval_min", 1.0) or 1.0)
        while not self.terminated:
            self._capture_snapshot()
            for worker in self.workers.values():
                self._emit_worker_state(worker, "WORKER_STATE_CHANGED")
            yield self.env.timeout(interval)
        self._capture_snapshot()
        for worker in self.workers.values():
            self._emit_worker_state(worker, "WORKER_STATE_CHANGED")

    def _surface_state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {state: 0 for state in sorted(SURFACE_TILE_STATES)}
        for work_tile in self.work_tiles.values():
            counts[work_tile.state] = counts.get(work_tile.state, 0) + 1
        return counts

    def _capture_snapshot(self) -> None:
        if float(self.env.now) == self._last_snapshot_at:
            return
        self._last_snapshot_at = float(self.env.now)
        counts = self._surface_state_counts()
        completed = counts.get("COMPLETE", 0)
        snapshot = {
            "t": round(float(self.env.now), 3),
            "day": self.day_index(),
            "surface_tile_state_counts": counts,
            "completed_surface_tile_count": completed,
            "surface_tile_completion_ratio": completed / max(1, len(self.work_tiles)),
            "worker_states": {wid: worker.humanoid_state for wid, worker in self.workers.items()},
            "cart_states": {
                cart_id: {
                    "status": cart.status,
                    "inventory_kind": cart.inventory_kind,
                    "inventory_count": cart.inventory_count,
                    "reserved_count": cart.reserved_count,
                    "tile": self.map.tile_payload(cart.tile),
                    "parking_spot_id": cart.parking_spot_id,
                }
                for cart_id, cart in self.carts.items()
            },
            # Compatibility aliases for older viewers.
            "section_state_counts": counts,
            "completed_section_count": completed,
        }
        self.minute_snapshots.append(snapshot)

    def close_day(self, day: int) -> dict[str, Any]:
        counts = self._surface_state_counts()
        completed = counts.get("COMPLETE", 0)
        summary = {
            "day": int(day),
            "completed_surface_tile_count": completed,
            "surface_tile_completion_ratio": completed / max(1, len(self.work_tiles)),
            "welded_surface_tile_count": sum(1 for tile in self.work_tiles.values() if tile.state in WELDED_OR_LATER),
            "painted_surface_tile_count": sum(1 for tile in self.work_tiles.values() if tile.state in PAINTED_OR_LATER),
            "rework_count": self.rework_count,
            "cart_trip_count": sum(cart.trip_count for cart in self.carts.values()),
            "cart_items_moved": sum(cart.items_moved for cart in self.carts.values()),
            "cart_wait_time_min": round(float(self.cart_wait_time_min), 3),
            "cart_collision_wait_count": int(self.cart_collision_wait_count),
            # Compatibility aliases.
            "completed_section_count": completed,
            "section_completion_ratio": completed / max(1, len(self.work_tiles)),
            "welded_section_count": sum(1 for tile in self.work_tiles.values() if tile.state in WELDED_OR_LATER),
            "painted_section_count": sum(1 for tile in self.work_tiles.values() if tile.state in PAINTED_OR_LATER),
        }
        self.daily_summaries.append(summary)
        return summary

    def _all_surface_tiles_complete(self) -> bool:
        return bool(self.work_tiles) and all(tile.state == "COMPLETE" for tile in self.work_tiles.values())

    def finalize_kpis(self) -> dict[str, Any]:
        completed = [tile for tile in self.work_tiles.values() if tile.state == "COMPLETE"]
        completed_count = len(completed)
        raw_makespan = max((tile.completed_at or 0.0 for tile in completed), default=0.0)
        all_tiles_complete = self._all_surface_tiles_complete()
        makespan = raw_makespan if all_tiles_complete else None
        sim_time = max((makespan if self.terminated and makespan is not None and makespan > 0 else float(self.env.now)), 1.0)
        worker_util = {
            worker_id: round(float(minutes) / sim_time, 6)
            for worker_id, minutes in self.worker_busy_min.items()
        }
        quality_pass_rate = float(self.verify_pass_count) / float(self.verify_count) if self.verify_count else 0.0
        surface_ratio = round(completed_count / max(1, len(self.work_tiles)), 6)
        state_metrics = self._humanoid_state_metrics(sim_time)
        cart_busy_total = sum(cart.busy_total_min + (max(0.0, float(self.env.now) - cart.busy_started_at) if cart.busy_started_at is not None else 0.0) for cart in self.carts.values())
        cart_util = round(cart_busy_total / max(1e-9, sim_time * max(1, len(self.carts))), 6) if self.carts else 0.0
        kpi = {
            "scenario_type": "shipyard_basic",
            "makespan_min": round(makespan, 3) if makespan is not None else None,
            "makespan_status": "complete" if all_tiles_complete else "pending",
            "surface_tile_count": len(self.work_tiles),
            "completed_surface_tile_count": completed_count,
            "surface_tile_completion_ratio": surface_ratio,
            "welded_surface_tile_count": sum(1 for tile in self.work_tiles.values() if tile.state in WELDED_OR_LATER),
            "painted_surface_tile_count": sum(1 for tile in self.work_tiles.values() if tile.state in PAINTED_OR_LATER),
            "surface_tile_state_counts": self._surface_state_counts(),
            "rework_count": self.rework_count,
            "quality_pass_rate": round(quality_pass_rate, 6),
            "cart_trip_count": sum(cart.trip_count for cart in self.carts.values()),
            "cart_items_moved": sum(cart.items_moved for cart in self.carts.values()),
            "cart_wait_time_min": round(float(self.cart_wait_time_min), 3),
            "cart_utilization": cart_util,
            "cart_collision_wait_count": int(self.cart_collision_wait_count),
            "cart_inventory_by_cart": {
                cart_id: {
                    "inventory_kind": cart.inventory_kind,
                    "inventory_count": cart.inventory_count,
                    "reserved_count": cart.reserved_count,
                    "trip_count": cart.trip_count,
                    "items_moved": cart.items_moved,
                }
                for cart_id, cart in self.carts.items()
            },
            "worker_utilization_by_worker": worker_util,
            "worker_task_minutes": self.worker_task_minutes,
            "incident_count_by_code": dict(self.incident_count_by_code),
            "total_products": completed_count,
            "downstream_closure_ratio": surface_ratio,
            "throughput_per_sim_hour": round(completed_count / max(1e-9, sim_time / 60.0), 6),
            "completed_product_lead_time_avg_min": round(makespan, 3) if makespan is not None else None,
            "humanoid_incident_total": sum(self.incident_count_by_code.values()),
            "humanoid_incidents_by_code": dict(self.incident_count_by_code),
            "humanoid_incidents_by_category": {},
            "humanoid_incidents_by_worker": {},
            "humanoid_incident_recovery_protocol_by_code": {},
            "repair_collaboration_time_min": 0.0,
            "repair_collaboration_episodes": [],
            "shared_product_carry_time_by_worker": {},
            "traffic_conflicts_by_type": {},
            "traffic_conflicts_by_worker_pair": {},
            "warehouse_material_shelf_count": 0,
            "warehouse_material_shelf_capacity": 0,
            "inspection_scrap_queue_length": 0,
            "disposed_scrap_count": 0,
            "machine_utilization": 0.0,
            "machine_broken_ratio": 0.0,
            "machine_pm_ratio": 0.0,
            "rolling_horizon_window_count": int(self.rolling_horizon_metrics.get("started_window_count", 0)),
            "rolling_horizon_candidate_collected_count": int(self.rolling_horizon_metrics.get("candidate_collected_count", 0)),
            "rolling_horizon_dispatched_task_count": int(self.rolling_horizon_metrics.get("dispatched_task_count", 0)),
            "rolling_horizon_requeued_task_count": int(self.rolling_horizon_metrics.get("requeued_task_count", 0)),
            "rolling_horizon_max_worker_queue_length": int(self.rolling_horizon_metrics.get("max_worker_queue_length", 0)),
            "rolling_horizon_stale_skipped_task_count": int(self.rolling_horizon_metrics.get("stale_skipped_task_count", 0)),
            "rolling_horizon": {
                "enabled": bool(self.rolling_horizon_enabled),
                "dedicated_roles": self.decision_mode == "rolling_horizon_dedicated_roles",
                "window_min": round(float(self.rolling_horizon_window_min), 3),
                "window_count": int(self.rolling_horizon_metrics.get("started_window_count", 0)),
                "candidate_collected_count": int(self.rolling_horizon_metrics.get("candidate_collected_count", 0)),
                "dispatched_task_count": int(self.rolling_horizon_metrics.get("dispatched_task_count", 0)),
                "stale_skipped_task_count": int(self.rolling_horizon_metrics.get("stale_skipped_task_count", 0)),
                "requeued_task_count": int(self.rolling_horizon_metrics.get("requeued_task_count", 0)),
                "pending_candidate_count": int(len(self.rolling_horizon_pending)),
                "queued_dispatch_count": int(sum(len(queue) for queue in self.rolling_horizon_dispatch_queues.values())),
            },
            "terminated": self.terminated,
            "termination_reason": self.termination_reason or ("completed_horizon" if not self.terminated else "all_ship_surface_tiles_complete"),
            # Compatibility aliases for existing hub/audit code.
            "completed_section_count": completed_count,
            "section_completion_ratio": surface_ratio,
            "welded_section_count": sum(1 for tile in self.work_tiles.values() if tile.state in WELDED_OR_LATER),
            "painted_section_count": sum(1 for tile in self.work_tiles.values() if tile.state in PAINTED_OR_LATER),
        }
        kpi.update(state_metrics)
        return kpi

    def _humanoid_state_metrics(self, sim_time: float) -> dict[str, Any]:
        axis_states = {
            "availability": ["AVAILABLE", "ASSIGNED", "EXECUTING", "WAITING", "BLOCKED", "OFFLINE", "DISABLED"],
            "mobility": ["STATIONARY", "NAVIGATING", "DOCKING"],
            "power": ["POWER_NORMAL", "POWER_LOW", "POWER_CRITICAL", "DEPLETED", "CHARGING"],
            "manipulation": ["FREE", "REACHING", "HOLDING", "PLACING"],
        }
        by_worker = {
            worker_id: {axis: {state: 0.0 for state in states} for axis, states in axis_states.items()}
            for worker_id in self.worker_ids
        }
        current = {worker_id: self.workers[worker_id].humanoid_state for worker_id in self.worker_ids}
        last_t = {worker_id: 0.0 for worker_id in self.worker_ids}
        for event in sorted(self.logger.events, key=lambda row: float(row.get("t", 0.0) or 0.0)):
            worker_id = str(event.get("entity_id", ""))
            if worker_id not in by_worker:
                continue
            details = event.get("details", {}) if isinstance(event.get("details", {}), dict) else {}
            state = details.get("humanoid_state")
            if not isinstance(state, dict):
                continue
            t = float(event.get("t", 0.0) or 0.0)
            self._accumulate_state_duration(by_worker[worker_id], current[worker_id], max(0.0, t - last_t[worker_id]), axis_states)
            current[worker_id] = state
            last_t[worker_id] = t
        for worker_id in self.worker_ids:
            self._accumulate_state_duration(by_worker[worker_id], current[worker_id], max(0.0, sim_time - last_t[worker_id]), axis_states)

        by_axis = {axis: {state: 0.0 for state in states} for axis, states in axis_states.items()}
        ratios_by_worker: dict[str, dict[str, dict[str, float]]] = {}
        for worker_id, axes in by_worker.items():
            ratios_by_worker[worker_id] = {}
            for axis, states in axes.items():
                for state, minutes in states.items():
                    by_axis[axis][state] += round(minutes, 3)
                denom = max(1e-9, sum(states.values()))
                ratios_by_worker[worker_id][axis] = {state: round(minutes / denom, 6) for state, minutes in states.items()}
        execution_ratio = {
            worker_id: ratios_by_worker[worker_id]["availability"].get("EXECUTING", 0.0)
            for worker_id in self.worker_ids
        }
        unavailable_ratio = {
            worker_id: ratios_by_worker[worker_id]["availability"].get("DISABLED", 0.0) + ratios_by_worker[worker_id]["availability"].get("OFFLINE", 0.0)
            for worker_id in self.worker_ids
        }
        blocked_ratio = {
            worker_id: ratios_by_worker[worker_id]["availability"].get("BLOCKED", 0.0)
            for worker_id in self.worker_ids
        }
        return {
            "humanoid_state_time_by_worker": by_worker,
            "humanoid_state_time_by_axis": by_axis,
            "humanoid_state_ratio_by_worker": ratios_by_worker,
            "humanoid_execution_ratio_by_worker": execution_ratio,
            "humanoid_execution_ratio_avg": round(sum(execution_ratio.values()) / max(1, len(execution_ratio)), 6),
            "humanoid_blocked_ratio_by_worker": blocked_ratio,
            "humanoid_blocked_ratio_avg": round(sum(blocked_ratio.values()) / max(1, len(blocked_ratio)), 6),
            "humanoid_unavailable_ratio_by_worker": unavailable_ratio,
            "humanoid_unavailable_ratio_avg": round(sum(unavailable_ratio.values()) / max(1, len(unavailable_ratio)), 6),
        }

    @staticmethod
    def _accumulate_state_duration(target: dict[str, dict[str, float]], state: dict[str, Any], duration: float, axis_states: dict[str, list[str]]) -> None:
        if duration <= 0.0:
            return
        for axis, states in axis_states.items():
            value = str(state.get(axis) or states[0])
            if value not in target[axis]:
                target[axis][value] = 0.0
            target[axis][value] += round(duration, 3)
