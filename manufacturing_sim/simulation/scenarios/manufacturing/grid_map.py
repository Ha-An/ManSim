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
        "output_buffer_station_1": "station_1_output_queue",
        "output_buffer_station_2": "station_2_output_queue",
        "output_buffer_station_4": "inspection_output_queue",
        "product_queue_4": "intermediate_queue_4",
        "warehouse_buffer": "warehouse_buffer",
        "battery_rack": "battery_rack",
    }

    REGION_ID = {
        "Warehouse": "warehouse_region",
        "Station1": "station_1_region",
        "Station2": "station_2_region",
        "Inspection": "inspection_region",
        "BatteryStation": "battery_station_region",
    }

    REGION_LABEL = {
        "Warehouse": "Warehouse",
        "Station1": "Station 1",
        "Station2": "Station 2",
        "Inspection": "Inspection",
        "BatteryStation": "Battery Station",
    }

    REGION_KIND = {
        "Warehouse": "storage",
        "Station1": "station",
        "Station2": "station",
        "Inspection": "inspection",
        "BatteryStation": "battery",
    }

    REGION_ACCENT = {
        "Warehouse": "#86b6ff",
        "Station1": "#8fb6ff",
        "Station2": "#8fb6ff",
        "Inspection": "#adc0eb",
        "BatteryStation": "#96c3f7",
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
            for tile in obj.tiles:
                self.object_tiles[tile] = obj.object_id
        self.blocked_static = set(self.walls) | set(self.object_tiles.keys())
        self.service_tiles = {key: list(value) for key, value in service_tiles.items()}
        self.zone_service_tiles = {key: list(value) for key, value in zone_service_tiles.items()}
        self.worker_tiles: dict[str, Tile] = {}
        self.occupied_tiles: dict[Tile, str] = {}
        self.reserved_tiles: dict[Tile, str] = {}

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
        objects = cls._default_objects(zones, stations=stations, machines_per_station=machines_per_station)
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
        add_horizontal(warehouse, warehouse.y1)
        add_horizontal(station1, station1.y)
        add_vertical(station1, station1.x1)
        add_horizontal(station2, station2.y)
        add_vertical(station2, station2.x)
        add_vertical(station2, station2.x1)
        add_horizontal(station2, station2.y1)
        add_horizontal(inspection, inspection.y)
        add_vertical(inspection, inspection.x)
        add_horizontal(battery, battery.y)
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

    @classmethod
    def _default_objects(
        cls,
        zones: dict[str, ZoneRect],
        *,
        stations: Iterable[int],
        machines_per_station: int,
    ) -> dict[str, ObjectFootprint]:
        objects: dict[str, ObjectFootprint] = {}

        def add(object_id: str, object_type: str, zone_name: str, rel_x: int, rel_y: int, width: int, height: int) -> None:
            zone = zones[zone_name]
            objects[object_id] = ObjectFootprint(
                object_id=object_id,
                object_type=object_type,
                zone=zone_name,
                x=zone.x + rel_x,
                y=zone.y + rel_y,
                width=width,
                height=height,
            )

        queue_width, queue_height = cls.QUEUE_FOOTPRINT
        machine_width, machine_height = cls.MACHINE_FOOTPRINT

        add("warehouse_buffer", "buffer", "Warehouse", 15, 5, queue_width, queue_height)
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
        add("inspection_output_queue", "buffer", "Inspection", 15, 6, queue_width, queue_height)
        return objects

    @classmethod
    def _build_object_service_tiles(
        cls,
        width: int,
        height: int,
        walls: set[Tile],
        objects: dict[str, ObjectFootprint],
    ) -> dict[str, list[Tile]]:
        object_tiles = {tile for obj in objects.values() for tile in obj.tiles}
        blocked = walls | object_tiles
        out: dict[str, list[Tile]] = {}
        for obj in objects.values():
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
        object_tiles = {tile for obj in objects.values() for tile in obj.tiles}
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
        slots = self.zone_service_tiles.get("Warehouse") or [self.zones["Warehouse"].center()]
        index = 0
        try:
            index = max(0, int(str(worker_id).lstrip("A")) - 1)
        except ValueError:
            index = 0
        return slots[index % len(slots)]

    def register_worker(self, worker_id: str, tile: Tile) -> Tile:
        chosen = self.nearest_free_tile(tile, worker_id=worker_id) or tile
        self.worker_tiles[worker_id] = chosen
        self.occupied_tiles[chosen] = worker_id
        return chosen

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

    def is_enterable(self, tile: Tile, *, worker_id: str, allow_reserved_by_self: bool = True) -> bool:
        if not self.is_passable_static(tile):
            return False
        occupant = self.occupied_tiles.get(tile)
        if occupant is not None and occupant != worker_id:
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

    def destination_tiles(self, location: str, *, worker_id: str, from_tile: Tile | None = None) -> list[Tile]:
        normalized = self.normalize_location(location)
        if normalized in self.worker_tiles:
            target = self.worker_tiles[normalized]
            tiles = [tile for tile in self.neighbors(target) if self.is_enterable(tile, worker_id=worker_id)]
            return sorted(tiles, key=lambda tile: (self.manhattan(from_tile or target, tile), tile[1], tile[0]))
        if normalized in self.service_tiles:
            tiles = self.service_tiles[normalized]
        elif normalized in self.zone_service_tiles:
            tiles = self.zone_service_tiles[normalized]
        else:
            tiles = self.zone_service_tiles.get("Warehouse", [])
        return sorted(tiles, key=lambda tile: (self.manhattan(from_tile or tile, tile), tile[1], tile[0]))

    def select_destination_tile(self, location: str, *, worker_id: str, from_tile: Tile) -> Tile | None:
        candidates = self.destination_tiles(location, worker_id=worker_id, from_tile=from_tile)
        for tile in candidates:
            if self.is_enterable(tile, worker_id=worker_id):
                return tile
        return candidates[0] if candidates else None

    def find_path(self, start: Tile, goals: Tile | Iterable[Tile], *, worker_id: str) -> list[Tile] | None:
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
                if neighbor != start and not self.is_enterable(neighbor, worker_id=worker_id):
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
        old = self.worker_tiles.get(worker_id)
        if old is not None and self.occupied_tiles.get(old) == worker_id:
            self.occupied_tiles.pop(old, None)
        self.release_reservation(worker_id, tile)
        self.worker_tiles[worker_id] = tile
        self.occupied_tiles[tile] = worker_id

    def travel_time(self, src: str, dst: str) -> float:
        src_norm = self.normalize_location(src)
        dst_norm = self.normalize_location(dst)
        if src_norm == dst_norm:
            return 0.0
        src_tiles = self.destination_tiles(src_norm, worker_id="__travel__", from_tile=None)
        dst_tiles = self.destination_tiles(dst_norm, worker_id="__travel__", from_tile=src_tiles[0] if src_tiles else None)
        if not src_tiles or not dst_tiles:
            return self.tile_time_min
        best_edges: int | None = None
        for src_tile in src_tiles[:3]:
            path = self.find_path(src_tile, dst_tiles[:4], worker_id="__travel__")
            if path:
                edges = max(0, len(path) - 1)
                best_edges = edges if best_edges is None else min(best_edges, edges)
        if best_edges is None:
            return self.tile_time_min * float(self.width_tiles + self.height_tiles)
        return best_edges * self.tile_time_min

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
        }
        for obj in self.objects.values():
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
                    }
                    for obj in self.objects.values()
                ],
                "service_tiles": {
                    key: self.path_payload(value)
                    for key, value in {**self.zone_service_tiles, **self.service_tiles}.items()
                },
            },
        }
