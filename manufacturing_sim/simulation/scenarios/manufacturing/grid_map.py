from __future__ import annotations

import heapq
import builtins
from dataclasses import dataclass
from typing import Any, Iterable


Tile = tuple[int, int]


@dataclass(frozen=True)
class ZoneRect:
    name: str
    x: int
    y: int
    width: int
    height: int

    @property
    def x1(self) -> int:
        return self.x + self.width - 1

    @property
    def y1(self) -> int:
        return self.y + self.height - 1

    def center(self) -> Tile:
        return (self.x + self.width // 2, self.y + self.height // 2)

    def contains(self, tile: Tile) -> bool:
        x, y = tile
        return self.x <= x <= self.x1 and self.y <= y <= self.y1


@dataclass(frozen=True)
class ObjectFootprint:
    object_id: str
    object_type: str
    zone: str
    x: int
    y: int
    width: int
    height: int
    blocking: bool = True

    @property
    def tiles(self) -> tuple[Tile, ...]:
        return tuple((x, y) for x in range(self.x, self.x + self.width) for y in range(self.y, self.y + self.height))

    def center(self) -> Tile:
        return (self.x + self.width // 2, self.y + self.height // 2)


class TileGridMap:
    """Grid map with static walls/objects and dynamic worker tile occupancy."""

    QUEUE_FOOTPRINT = (8, 3)
    MACHINE_FOOTPRINT = (6, 5)

    LOCATION_ALIASES = {
        "Home": "Warehouse",
        "warehouse_region": "Warehouse",
        "station_1_region": "Station1",
        "station_2_region": "Station2",
        "inspection_region": "Inspection",
        "battery_station_region": "BatteryStation",
        "completed_products_region": "CompletedProducts",
        "scrap_disposal_region": "ScrapDisposal",
        "output_buffer_station_1": "station_1_output_queue",
        "output_buffer_station_2": "station_2_output_queue",
        "output_buffer_station_4": "inspection_output_queue",
        "product_queue_4": "intermediate_queue_4",
        "warehouse_buffer": "completed_product_buffer",
        "completed_product_buffer": "completed_product_buffer",
        "completed_products": "CompletedProducts",
        "scrap_disposal": "ScrapDisposal",
        "scrap_disposal_bin": "scrap_disposal_bin",
        "inspection_scrap_queue": "inspection_scrap_queue",
        "warehouse_material_shelf": "warehouse_material_shelf",
        "battery_rack": "battery_rack",
        "inspection_table": "inspection_table",
        "inspection_desk": "inspection_table",
        "inspection_workbench": "inspection_table",
    }

    REGION_ID = {
        "Warehouse": "warehouse_region",
        "Station1": "station_1_region",
        "Station2": "station_2_region",
        "Inspection": "inspection_region",
        "BatteryStation": "battery_station_region",
        "CompletedProducts": "completed_products_region",
        "ScrapDisposal": "scrap_disposal_region",
    }

    REGION_LABEL = {
        "Warehouse": "Warehouse",
        "Station1": "Station 1",
        "Station2": "Station 2",
        "Inspection": "Inspection",
        "BatteryStation": "Battery Station",
        "CompletedProducts": "Completed Products",
        "ScrapDisposal": "Scrap Disposal",
    }

    REGION_KIND = {
        "Warehouse": "storage",
        "Station1": "station",
        "Station2": "station",
        "Inspection": "inspection",
        "BatteryStation": "battery",
        "CompletedProducts": "completed_products",
        "ScrapDisposal": "scrap_disposal",
    }

    REGION_ACCENT = {
        "Warehouse": "#86b6ff",
        "Station1": "#8fb6ff",
        "Station2": "#8fb6ff",
        "Inspection": "#adc0eb",
        "BatteryStation": "#96c3f7",
        "CompletedProducts": "#9ad6b2",
        "ScrapDisposal": "#f0a7a7",
    }

    def __init__(
        self,
        *,
        width_tiles: int,
        height_tiles: int,
        tile_time_min: float,
        blocked_replan_threshold_min: float,
        zones: dict[str, ZoneRect],
        doors: set[Tile],
        walls: set[Tile],
        objects: dict[str, ObjectFootprint],
        service_tiles: dict[str, list[Tile]],
        zone_service_tiles: dict[str, list[Tile]],
    ) -> None:
        self.width_tiles = int(width_tiles)
        self.height_tiles = int(height_tiles)
        self.tile_time_min = float(tile_time_min)
        self.blocked_replan_threshold_min = float(blocked_replan_threshold_min)
        self.zones = zones
        self.doors = set(doors)
        self.walls = set(walls) - set(doors)
        self.objects = objects
        self.object_tiles: dict[Tile, str] = {}
        for obj in objects.values():
            if not obj.blocking:
                continue
            for tile in obj.tiles:
                self.object_tiles[tile] = obj.object_id
        self.blocked_static = set(self.walls) | set(self.object_tiles.keys())
        self.service_tiles = {key: list(value) for key, value in service_tiles.items()}
        self.zone_service_tiles = {key: list(value) for key, value in zone_service_tiles.items()}
        self.worker_tiles: dict[str, Tile] = {}
        self.tile_workers: dict[Tile, set[str]] = {}
        self.occupied_tiles: dict[Tile, str] = {}
        self.reserved_tiles: dict[Tile, str] = {}
        self._travel_time_cache: dict[tuple[str, str], float] = {}

    @classmethod
    def from_world_config(
        cls,
        cfg: dict[str, Any],
        *,
        stations: Iterable[int],
        machines_per_station: int,
    ) -> "TileGridMap":
        map_cfg = cfg.get("map", {}) if isinstance(cfg.get("map", {}), dict) else {}
        width = int(map_cfg.get("width_tiles", 100) or 100)
        height = int(map_cfg.get("height_tiles", 70) or 70)
        tile_time = float(map_cfg.get("tile_time_min", 0.1) or 0.1)
        blocked_threshold = float(map_cfg.get("blocked_replan_threshold_min", 5.0) or 5.0)

        zones = cls._default_zones(width, height)
        doors = cls._default_doors(zones)
        walls = cls._perimeter_walls(zones, doors)
        warehouse_cfg = cfg.get("warehouse", {}) if isinstance(cfg.get("warehouse", {}), dict) else {}
        shelf_cfg = warehouse_cfg.get("material_shelf", {}) if isinstance(warehouse_cfg.get("material_shelf", {}), dict) else {}
        material_shelf_capacity = max(0, int(shelf_cfg.get("capacity", 10) or 10))
        objects = cls._default_objects(
            zones,
            stations=stations,
            machines_per_station=machines_per_station,
            material_shelf_capacity=material_shelf_capacity,
        )
        walls |= cls._object_wall_tiles(objects)
        service_tiles = cls._build_object_service_tiles(width, height, walls, objects)
        zone_service_tiles = cls._build_zone_service_tiles(width, height, zones, walls, objects)
        return cls(
            width_tiles=width,
            height_tiles=height,
            tile_time_min=tile_time,
            blocked_replan_threshold_min=blocked_threshold,
            zones=zones,
            doors=doors,
            walls=walls,
            objects=objects,
            service_tiles=service_tiles,
            zone_service_tiles=zone_service_tiles,
        )

    @staticmethod
    def _default_zones(width: int, height: int) -> dict[str, ZoneRect]:
        # Keep the current Replay Studio composition: warehouse top-center,
        # stations/inspection middle row, battery station bottom-center.
        return {
            "Warehouse": ZoneRect("Warehouse", max(1, width // 2 - 13), 4, 26, 12),
            "Station1": ZoneRect("Station1", 4, 24, 26, 22),
            "Station2": ZoneRect("Station2", max(1, width // 2 - 13), 24, 26, 22),
            "Inspection": ZoneRect("Inspection", max(1, width - 30), 24, 26, 22),
            "BatteryStation": ZoneRect("BatteryStation", max(1, width // 2 - 13), max(1, height - 16), 26, 10),
            "CompletedProducts": ZoneRect("CompletedProducts", max(1, width - 30), 4, 26, 12),
            "ScrapDisposal": ZoneRect("ScrapDisposal", max(1, width - 30), max(1, height - 16), 26, 10),
        }

    @staticmethod
    def _default_doors(zones: dict[str, ZoneRect]) -> set[Tile]:
        doors: set[Tile] = set()

        def add_horizontal(zone: ZoneRect, y: int, center_x: int | None = None) -> None:
            cx = center_x if center_x is not None else zone.x + zone.width // 2
            doors.update({(cx, y), (cx - 1, y)})

        def add_vertical(zone: ZoneRect, x: int, center_y: int | None = None) -> None:
            cy = center_y if center_y is not None else zone.y + zone.height // 2
            doors.update({(x, cy), (x, cy - 1)})

        warehouse = zones["Warehouse"]
        station1 = zones["Station1"]
        station2 = zones["Station2"]
        inspection = zones["Inspection"]
        battery = zones["BatteryStation"]
        completed = zones["CompletedProducts"]
        scrap = zones["ScrapDisposal"]
        add_horizontal(warehouse, warehouse.y1)
        add_horizontal(station1, station1.y)
        add_vertical(station1, station1.x1)
        add_horizontal(station2, station2.y)
        add_vertical(station2, station2.x)
        add_vertical(station2, station2.x1)
        add_horizontal(station2, station2.y1)
        add_horizontal(inspection, inspection.y)
        add_horizontal(inspection, inspection.y1)
        add_vertical(inspection, inspection.x)
        add_horizontal(battery, battery.y)
        add_horizontal(completed, completed.y1)
        add_vertical(completed, completed.x)
        add_horizontal(scrap, scrap.y)
        add_vertical(scrap, scrap.x)
        return doors

    @staticmethod
    def _perimeter_walls(zones: dict[str, ZoneRect], doors: set[Tile]) -> set[Tile]:
        walls: set[Tile] = set()
        for zone in zones.values():
            for x in range(zone.x, zone.x + zone.width):
                walls.add((x, zone.y))
                walls.add((x, zone.y1))
            for y in range(zone.y, zone.y + zone.height):
                walls.add((zone.x, y))
                walls.add((zone.x1, y))
        return walls - doors

    @staticmethod
    def _object_wall_tiles(objects: dict[str, ObjectFootprint]) -> set[Tile]:
        """Promote wall-like blocking objects into the canonical wall layer."""
        return {
            tile
            for obj in objects.values()
            if obj.object_type in {"shelf_wall", "shelf_low_wall", "shelf_blocker"}
            for tile in obj.tiles
        }

    @classmethod
    def _default_objects(
        cls,
        zones: dict[str, ZoneRect],
        *,
        stations: Iterable[int],
        machines_per_station: int,
        material_shelf_capacity: int = 10,
    ) -> dict[str, ObjectFootprint]:
        objects: dict[str, ObjectFootprint] = {}

        def add(
            object_id: str,
            object_type: str,
            zone_name: str,
            rel_x: int,
            rel_y: int,
            width: int,
            height: int,
            *,
            blocking: bool = True,
        ) -> None:
            zone = zones[zone_name]
            objects[object_id] = ObjectFootprint(
                object_id=object_id,
                object_type=object_type,
                zone=zone_name,
                x=zone.x + rel_x,
                y=zone.y + rel_y,
                width=width,
                height=height,
                blocking=blocking,
            )

        queue_width, queue_height = cls.QUEUE_FOOTPRINT
        machine_width, machine_height = cls.MACHINE_FOOTPRINT

        shelf_capacity = max(0, int(material_shelf_capacity or 0))
        slots_per_row = max(1, min(10, shelf_capacity or 1))
        shelf_rows = max(1, (shelf_capacity + slots_per_row - 1) // slots_per_row)
        shelf_width = max(1, min(21, slots_per_row * 2 - 1))
        warehouse = zones["Warehouse"]
        shelf_rel_x = max(1, warehouse.width - 1 - shelf_width)
        shelf_slot_start_rel_y = 2
        shelf_row_pitch = 3
        # Shelf rows are right-aligned against the warehouse inner wall.
        # Each row is exactly wide enough for ten alternating material spots.
        shelf_wall_width = shelf_width
        shelf_height = max(1, (shelf_rows - 1) * shelf_row_pitch + 3)
        add("warehouse_material_shelf", "shelf", "Warehouse", shelf_rel_x, shelf_slot_start_rel_y - 1, shelf_wall_width, shelf_height, blocking=False)
        for row in range(shelf_rows):
            slot_rel_y = shelf_slot_start_rel_y + row * shelf_row_pitch
            row_slot_count = max(0, min(slots_per_row, shelf_capacity - row * slots_per_row))
            add(
                f"warehouse_material_shelf_wall_{row + 1:02d}",
                "shelf_wall",
                "Warehouse",
                shelf_rel_x,
                slot_rel_y - 1,
                shelf_wall_width,
                1,
                blocking=True,
            )
            # Material spots stay visually open so the item can be rendered on
            # the tile. Only the tiles between spots become low blockers.
            for col in range(max(0, row_slot_count - 1)):
                rel_x = shelf_rel_x + max(0, shelf_width - 2 - col * 2)
                add(
                    f"warehouse_material_shelf_spacer_{row + 1:02d}_{col + 1:02d}",
                    "shelf_low_wall",
                    "Warehouse",
                    rel_x,
                    slot_rel_y,
                    1,
                    1,
                    blocking=True,
                )
        for index in range(1, shelf_capacity + 1):
            row = (index - 1) // slots_per_row
            col = (index - 1) % slots_per_row
            rel_x = shelf_rel_x + max(0, shelf_width - 1 - col * 2)
            rel_y = shelf_slot_start_rel_y + row * shelf_row_pitch
            add(f"warehouse_material_slot_{index:02d}", "material_slot", "Warehouse", rel_x, rel_y, 1, 1, blocking=True)
        add("completed_product_buffer", "buffer", "CompletedProducts", 9, 5, queue_width, queue_height)
        add("scrap_disposal_bin", "scrap_bin", "ScrapDisposal", 9, 4, queue_width, queue_height)
        add("battery_rack", "charger", "BatteryStation", 4, 3, 5, 3)
        for station in sorted(int(s) for s in stations):
            if station == 1:
                zone_name = "Station1"
            elif station == 2:
                zone_name = "Station2"
            else:
                continue
            if station == 1:
                add(f"material_queue_{station}", "queue", zone_name, 3, 6, queue_width, queue_height)
                add(f"station_{station}_output_queue", "buffer", zone_name, 15, 6, queue_width, queue_height)
            else:
                add(f"material_queue_{station}", "queue", zone_name, 3, 4, queue_width, queue_height)
                add(f"intermediate_queue_{station}", "buffer", zone_name, 3, 8, queue_width, queue_height)
                add(f"station_{station}_output_queue", "buffer", zone_name, 15, 6, queue_width, queue_height)
            for idx in range(1, int(machines_per_station) + 1):
                rel_x = 4 if idx == 1 else 16
                add(f"S{station}M{idx}", "machine", zone_name, rel_x, 14, machine_width, machine_height)
        add("intermediate_queue_4", "queue", "Inspection", 3, 6, queue_width, queue_height)
        add("inspection_output_queue", "buffer", "Inspection", 15, 4, queue_width, queue_height)
        add("inspection_scrap_queue", "scrap_queue", "Inspection", 15, 8, queue_width, queue_height)
        add("inspection_table", "inspection_table", "Inspection", 10, 14, 6, 4, blocking=False)
        return objects

    @classmethod
    def _build_object_service_tiles(
        cls,
        width: int,
        height: int,
        walls: set[Tile],
        objects: dict[str, ObjectFootprint],
    ) -> dict[str, list[Tile]]:
        object_tiles = {tile for obj in objects.values() if obj.blocking for tile in obj.tiles}
        blocked = walls | object_tiles
        out: dict[str, list[Tile]] = {}
        for obj in objects.values():
            if obj.object_type == "inspection_table":
                center = obj.center()
                out[obj.object_id] = [center] if 0 <= center[0] < width and 0 <= center[1] < height and center not in walls else []
                continue
            if obj.object_type == "material_slot":
                # Pickup is allowed only from a tile directly adjacent to the
                # item on the open aisle side. The back side is a wall.
                candidates: list[Tile] = [(obj.x, obj.y + obj.height)]
                out[obj.object_id] = [
                    tile
                    for tile in candidates
                    if 0 <= tile[0] < width and 0 <= tile[1] < height and tile not in blocked
                ]
                continue
            candidates: set[Tile] = set()
            for x in range(obj.x, obj.x + obj.width):
                candidates.add((x, obj.y - 1))
                candidates.add((x, obj.y + obj.height))
            for y in range(obj.y, obj.y + obj.height):
                candidates.add((obj.x - 1, y))
                candidates.add((obj.x + obj.width, y))
            out[obj.object_id] = sorted(
                tile
                for tile in candidates
                if 0 <= tile[0] < width and 0 <= tile[1] < height and tile not in blocked
            )
        return out

    @classmethod
    def _build_zone_service_tiles(
        cls,
        width: int,
        height: int,
        zones: dict[str, ZoneRect],
        walls: set[Tile],
        objects: dict[str, ObjectFootprint],
    ) -> dict[str, list[Tile]]:
        object_tiles = {tile for obj in objects.values() if obj.blocking for tile in obj.tiles}
        blocked = walls | object_tiles
        out: dict[str, list[Tile]] = {}
        for zone_name, zone in zones.items():
            cx, cy = zone.center()
            candidates = [
                (cx, cy),
                (cx - 1, cy),
                (cx + 1, cy),
                (cx, cy - 1),
                (cx, cy + 1),
                (cx - 2, cy),
                (cx + 2, cy),
            ]
            out[zone_name] = [
                tile
                for tile in candidates
                if 0 <= tile[0] < width and 0 <= tile[1] < height and tile not in blocked and zone.contains(tile)
            ]
        return out

    def normalize_location(self, location: str) -> str:
        text = str(location or "").strip()
        if "->" in text:
            text = text.split("->", 1)[0].strip()
        if "(" in text:
            text = text.split("(", 1)[0].strip()
        return self.LOCATION_ALIASES.get(text, text)

    def logical_location(self, location: str) -> str:
        normalized = self.normalize_location(location)
        if normalized in self.objects:
            return self.objects[normalized].zone
        if normalized in self.worker_tiles:
            return normalized
        return normalized

    def initial_worker_tile(self, worker_id: str) -> Tile:
        index = 0
        try:
            index = max(0, int(str(worker_id).lstrip("A")) - 1)
        except ValueError:
            index = 0
        warehouse = self.zones.get("Warehouse")
        station2 = self.zones.get("Station2")
        if warehouse is not None and station2 is not None:
            # Start humanoids in the open corridor instead of inside Warehouse.
            # This keeps initial dispatch visible and avoids a first-frame
            # warehouse-to-corridor jump in Replay Studio.
            corridor_y = max(0, min(self.height_tiles - 1, (warehouse.y1 + station2.y) // 2))
            center_x = warehouse.x + warehouse.width // 2
            offsets = [0, -1, 1, -2, 2, -3, 3]
            corridor_slots = [
                (center_x + offset, corridor_y)
                for offset in offsets
                if 0 <= center_x + offset < self.width_tiles
                and self.is_passable_static((center_x + offset, corridor_y))
            ]
            if corridor_slots:
                return corridor_slots[index % len(corridor_slots)]
        slots = self.zone_service_tiles.get("Warehouse") or [self.zones["Warehouse"].center()]
        return slots[index % len(slots)]

    def register_worker(self, worker_id: str, tile: Tile) -> Tile:
        previous = self.worker_tiles.get(worker_id)
        if previous is not None:
            self._remove_worker_occupancy(worker_id, previous)
        chosen = self.nearest_free_tile(tile, worker_id=worker_id) or tile
        self.worker_tiles[worker_id] = chosen
        self._add_worker_occupancy(worker_id, chosen)
        return chosen

    def workers_at(self, tile: Tile) -> set[str]:
        return set(self.tile_workers.get(tile, set()))

    def _sync_occupied_tile(self, tile: Tile) -> None:
        workers = self.tile_workers.get(tile, set())
        if workers:
            self.occupied_tiles[tile] = sorted(workers)[0]
        else:
            self.tile_workers.pop(tile, None)
            self.occupied_tiles.pop(tile, None)

    def _add_worker_occupancy(self, worker_id: str, tile: Tile) -> None:
        self.tile_workers.setdefault(tile, set()).add(worker_id)
        self._sync_occupied_tile(tile)

    def _remove_worker_occupancy(self, worker_id: str, tile: Tile) -> None:
        workers = self.tile_workers.get(tile)
        if workers is None:
            if self.occupied_tiles.get(tile) == worker_id:
                self.occupied_tiles.pop(tile, None)
            return
        workers.discard(worker_id)
        self._sync_occupied_tile(tile)

    def nearest_free_tile(self, preferred: Tile, *, worker_id: str) -> Tile | None:
        if self.is_enterable(preferred, worker_id=worker_id):
            return preferred
        frontier = [(0, preferred)]
        seen = {preferred}
        while frontier:
            _dist, tile = heapq.heappop(frontier)
            for neighbor in self.neighbors(tile):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                if self.is_enterable(neighbor, worker_id=worker_id):
                    return neighbor
                heapq.heappush(frontier, (self.manhattan(preferred, neighbor), neighbor))
        return None

    def is_in_bounds(self, tile: Tile) -> bool:
        if not isinstance(tile, tuple) or len(tile) != 2:
            return False
        x, y = tile
        return 0 <= x < self.width_tiles and 0 <= y < self.height_tiles

    def is_passable_static(self, tile: Tile) -> bool:
        return self.is_in_bounds(tile) and tile not in self.blocked_static

    def is_enterable(
        self,
        tile: Tile,
        *,
        worker_id: str,
        allow_reserved_by_self: bool = True,
        ignore_dynamic: bool = False,
    ) -> bool:
        if not self.is_passable_static(tile):
            return False
        if not ignore_dynamic:
            occupants = self.tile_workers.get(tile)
            if occupants and any(occupant != worker_id for occupant in occupants):
                return False
            reserved_by = self.reserved_tiles.get(tile)
            if reserved_by is not None and (reserved_by != worker_id or not allow_reserved_by_self):
                return False
        return True

    def neighbors(self, tile: Tile) -> list[Tile]:
        if not self.is_in_bounds(tile):
            return []
        x, y = tile
        candidates = [(x, y - 1), (x - 1, y), (x + 1, y), (x, y + 1)]
        return [candidate for candidate in candidates if self.is_passable_static(candidate)]

    @staticmethod
    def manhattan(a: Tile, b: Tile) -> int:
        try:
            ax, ay = int(a[0]), int(a[1])
            bx, by = int(b[0]), int(b[1])
        except Exception:
            return 10**9
        return builtins.abs(ax - bx) + builtins.abs(ay - by)

    def destination_tiles(
        self,
        location: str,
        *,
        worker_id: str,
        from_tile: Tile | None = None,
        ignore_dynamic: bool = False,
    ) -> list[Tile]:
        normalized = self.normalize_location(location)
        if normalized in self.worker_tiles:
            target = self.worker_tiles[normalized]
            tiles = [
                tile
                for tile in self.neighbors(target)
                if self.is_enterable(tile, worker_id=worker_id, ignore_dynamic=ignore_dynamic)
            ]
            return sorted(tiles, key=lambda tile: (self.manhattan(from_tile or target, tile), tile[1], tile[0]))
        if normalized in self.service_tiles:
            tiles = self.service_tiles[normalized]
        elif normalized in self.zone_service_tiles:
            tiles = self.zone_service_tiles[normalized]
        else:
            tiles = self.zone_service_tiles.get("Warehouse", [])
        return sorted(tiles, key=lambda tile: (self.manhattan(from_tile or tile, tile), tile[1], tile[0]))

    def select_destination_tile(self, location: str, *, worker_id: str, from_tile: Tile, ignore_dynamic: bool = False) -> Tile | None:
        candidates = self.destination_tiles(location, worker_id=worker_id, from_tile=from_tile, ignore_dynamic=ignore_dynamic)
        for tile in candidates:
            if self.is_enterable(tile, worker_id=worker_id, ignore_dynamic=ignore_dynamic):
                return tile
        return candidates[0] if candidates else None

    def find_path(
        self,
        start: Tile,
        goals: Tile | Iterable[Tile],
        *,
        worker_id: str,
        ignore_dynamic: bool = False,
    ) -> list[Tile] | None:
        if not self.is_passable_static(start):
            return None
        raw_goals = [goals] if isinstance(goals, tuple) and len(goals) == 2 and isinstance(goals[0], int) else list(goals)
        goal_set = {
            goal
            for goal in raw_goals
            if isinstance(goal, tuple)
            and len(goal) == 2
            and isinstance(goal[0], int)
            and isinstance(goal[1], int)
            and self.is_passable_static(goal)
        }
        if not goal_set:
            return None
        if start in goal_set:
            return [start]

        def heuristic(tile: Tile) -> int:
            return min(self.manhattan(tile, goal) for goal in goal_set)

        frontier: list[tuple[int, int, Tile]] = []
        heapq.heappush(frontier, (heuristic(start), 0, start))
        came_from: dict[Tile, Tile | None] = {start: None}
        cost_so_far: dict[Tile, int] = {start: 0}
        while frontier:
            _priority, current_cost, current = heapq.heappop(frontier)
            if current in goal_set:
                path: list[Tile] = []
                node: Tile | None = current
                while node is not None:
                    path.append(node)
                    node = came_from[node]
                return list(reversed(path))
            for neighbor in self.neighbors(current):
                if neighbor != start and not self.is_enterable(neighbor, worker_id=worker_id, ignore_dynamic=ignore_dynamic):
                    continue
                new_cost = current_cost + 1
                if neighbor not in cost_so_far or new_cost < cost_so_far[neighbor]:
                    cost_so_far[neighbor] = new_cost
                    came_from[neighbor] = current
                    heapq.heappush(frontier, (new_cost + heuristic(neighbor), new_cost, neighbor))
        return None

    def try_reserve(self, worker_id: str, tile: Tile) -> bool:
        if not self.is_enterable(tile, worker_id=worker_id, allow_reserved_by_self=True):
            return False
        self.reserved_tiles[tile] = worker_id
        return True

    def release_reservation(self, worker_id: str, tile: Tile | None = None) -> None:
        for reserved_tile, reserved_by in list(self.reserved_tiles.items()):
            if reserved_by == worker_id and (tile is None or tile == reserved_tile):
                self.reserved_tiles.pop(reserved_tile, None)

    def move_worker_to_reserved(self, worker_id: str, tile: Tile) -> None:
        self.move_worker(worker_id, tile)
        self.release_reservation(worker_id, tile)

    def move_worker(self, worker_id: str, tile: Tile) -> None:
        old = self.worker_tiles.get(worker_id)
        if old is not None:
            self._remove_worker_occupancy(worker_id, old)
        self.worker_tiles[worker_id] = tile
        self._add_worker_occupancy(worker_id, tile)

    def travel_time(self, src: str, dst: str) -> float:
        src_norm = self.normalize_location(src)
        dst_norm = self.normalize_location(dst)
        if src_norm == dst_norm:
            return 0.0
        cache_key = (src_norm, dst_norm)
        cached = self._travel_time_cache.get(cache_key)
        if cached is not None:
            return cached
        src_tiles = self.destination_tiles(src_norm, worker_id="__travel__", from_tile=None)
        dst_tiles = self.destination_tiles(dst_norm, worker_id="__travel__", from_tile=src_tiles[0] if src_tiles else None)
        if not src_tiles or not dst_tiles:
            self._travel_time_cache[cache_key] = self.tile_time_min
            return self.tile_time_min
        best_edges: int | None = None
        for src_tile in src_tiles[:3]:
            # Travel-time estimates are used for ranking and battery reserve
            # checks. They should reflect the static factory layout, not the
            # momentary reservation/occupancy state of other workers.
            path = self.find_path(src_tile, dst_tiles[:4], worker_id="__travel__", ignore_dynamic=True)
            if path:
                edges = max(0, len(path) - 1)
                best_edges = edges if best_edges is None else min(best_edges, edges)
        if best_edges is None:
            value = self.tile_time_min * float(self.width_tiles + self.height_tiles)
        else:
            value = best_edges * self.tile_time_min
        self._travel_time_cache[cache_key] = value
        return value

    def tile_payload(self, tile: Tile | None) -> dict[str, int] | None:
        if tile is None:
            return None
        return {"x": int(tile[0]), "y": int(tile[1])}

    def path_payload(self, path: Iterable[Tile]) -> list[dict[str, int]]:
        return [self.tile_payload(tile) for tile in path if tile is not None]  # type: ignore[list-item]

    def tile_to_position(self, tile: Tile) -> dict[str, float]:
        tile_w = 1600.0 / max(1, self.width_tiles)
        tile_h = 960.0 / max(1, self.height_tiles)
        return {"x": (tile[0] + 0.5) * tile_w, "y": (tile[1] + 0.5) * tile_h}

    def to_replay_layout(self, worker_ids: Iterable[str]) -> dict[str, Any]:
        tile_w = 1600.0 / max(1, self.width_tiles)
        tile_h = 960.0 / max(1, self.height_tiles)

        def rect_position(zone: ZoneRect) -> dict[str, float]:
            return {"x": zone.x * tile_w, "y": zone.y * tile_h}

        def rect_size(zone: ZoneRect) -> dict[str, float]:
            return {"width": zone.width * tile_w, "height": zone.height * tile_h}

        regions = [
            {
                "region_id": self.REGION_ID[name],
                "label": self.REGION_LABEL[name],
                "kind": self.REGION_KIND[name],
                "position": rect_position(zone),
                "size": rect_size(zone),
                "accent": self.REGION_ACCENT[name],
            }
            for name, zone in self.zones.items()
        ]
        nodes: list[dict[str, Any]] = []
        type_map = {
            "queue": "queue",
            "buffer": "buffer",
            "machine": "machine",
            "charger": "charger",
            "shelf": "shelf",
            "material_slot": "material_slot",
            "scrap_queue": "queue",
            "scrap_bin": "buffer",
        }
        for obj in self.objects.values():
            if obj.object_type in {"shelf_blocker", "shelf_wall", "shelf_low_wall"}:
                continue
            nodes.append(
                {
                    "entity_id": obj.object_id,
                    "entity_type": type_map.get(obj.object_type, obj.object_type),
                    "region_id": self.REGION_ID[obj.zone],
                    "position": self.tile_to_position(obj.center()),
                    "tile": self.tile_payload(obj.center()),
                    "footprint": {
                        "x": obj.x,
                        "y": obj.y,
                        "width": obj.width,
                        "height": obj.height,
                    },
                }
            )
        for worker_id in worker_ids:
            tile = self.initial_worker_tile(str(worker_id))
            nodes.append(
                {
                    "entity_id": str(worker_id),
                    "entity_type": "worker",
                    "region_id": self.REGION_ID["Warehouse"],
                    "position": self.tile_to_position(tile),
                    "tile": self.tile_payload(tile),
                }
            )
        return {
            "source_priority": ["log", "config", "auto"],
            "viewport": {"width": 1600, "height": 960},
            "regions": regions,
            "nodes": nodes,
            "grid": {
                "width_tiles": self.width_tiles,
                "height_tiles": self.height_tiles,
                "tile_time_min": self.tile_time_min,
                "walls": self.path_payload(sorted(self.walls)),
                "doors": self.path_payload(sorted(self.doors)),
                "object_footprints": [
                    {
                        "object_id": obj.object_id,
                        "object_type": obj.object_type,
                        "zone": obj.zone,
                        "x": obj.x,
                        "y": obj.y,
                        "width": obj.width,
                        "height": obj.height,
                        "blocking": obj.blocking,
                    }
                    for obj in self.objects.values()
                ],
                "service_tiles": {
                    key: self.path_payload(value)
                    for key, value in {**self.zone_service_tiles, **self.service_tiles}.items()
                },
            },
        }
