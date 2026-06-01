from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from typing import Any, Iterable

from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import ObjectFootprint, Tile, ZoneRect


@dataclass(frozen=True)
class ShipWorkTileLayout:
    work_tile_id: str
    tile: Tile
    service_tiles: tuple[Tile, ...]


@dataclass(frozen=True)
class CartParkingSpotLayout:
    parking_spot_id: str
    tile: Tile


class ShipyardTileGridMap:
    """Tile map for ship exterior surface repair.

    The central ship is a fixed blocking silhouette.  Only exterior surface
    tiles become work targets, and every target exposes passable adjacent
    service tiles where workers can stand while welding, preparing, painting,
    and verifying that tile.
    """

    REGION_ID = {
        "ShipDock": "ship_dock_region",
        "BatteryZone": "battery_zone_region",
        "MaterialYard": "material_yard_region",
        "PaintSupply": "paint_supply_region",
        "ScrapArea": "scrap_area_region",
    }
    REGION_LABEL = {
        "ShipDock": "Ship Dock",
        "BatteryZone": "Battery Zone",
        "MaterialYard": "Material Yard",
        "PaintSupply": "Paint Supply",
        "ScrapArea": "Scrap Area",
    }
    REGION_KIND = {
        "ShipDock": "dock",
        "BatteryZone": "battery",
        "MaterialYard": "materials",
        "PaintSupply": "paint",
        "ScrapArea": "scrap",
    }
    REGION_ACCENT = {
        "ShipDock": "#1f6feb",
        "BatteryZone": "#2ecc71",
        "MaterialYard": "#f39c12",
        "PaintSupply": "#e84393",
        "ScrapArea": "#e74c3c",
    }

    def __init__(
        self,
        *,
        width_tiles: int,
        height_tiles: int,
        tile_time_min: float,
        zones: dict[str, ZoneRect],
        objects: dict[str, ObjectFootprint],
        work_tiles: dict[str, ShipWorkTileLayout],
        ship_hull_tiles: set[Tile],
        cart_route_tiles: set[Tile],
        cart_parking_spots: dict[str, CartParkingSpotLayout],
        cart_source_tiles: dict[str, Tile],
        cart_count: int,
    ) -> None:
        self.width_tiles = int(width_tiles)
        self.height_tiles = int(height_tiles)
        self.tile_time_min = float(tile_time_min)
        self.zones = zones
        self.objects = objects
        self.work_tiles = work_tiles
        # Backward-compatible alias for older dashboard/export code paths.
        self.sections = work_tiles
        self.ship_hull_tiles = set(ship_hull_tiles)
        self.cart_route_tiles = set(cart_route_tiles)
        self.cart_parking_spots = cart_parking_spots
        self.cart_source_tiles = cart_source_tiles
        self.cart_count = max(0, int(cart_count))
        # Shipyard zones are semantic work areas, not physical rooms. Keep the
        # central ship surface blocking, but let workers cross zone boundaries.
        self.walls: set[Tile] = set()
        self.doors: set[Tile] = set()
        self.object_tiles = {
            tile: obj.object_id
            for obj in objects.values()
            if obj.blocking
            for tile in obj.tiles
        }
        self.blocked_static = set(self.walls) | set(self.object_tiles) | set(self.ship_hull_tiles)

    @classmethod
    def from_world_config(cls, cfg: dict[str, Any]) -> "ShipyardTileGridMap":
        map_cfg = cfg.get("map", {}) if isinstance(cfg.get("map", {}), dict) else {}
        shipyard_cfg = cfg.get("shipyard", {}) if isinstance(cfg.get("shipyard", {}), dict) else {}
        layout_cfg = shipyard_cfg.get("layout", {}) if isinstance(shipyard_cfg.get("layout", {}), dict) else {}
        surface_cfg = shipyard_cfg.get("surface_tiles", {}) if isinstance(shipyard_cfg.get("surface_tiles", {}), dict) else {}
        logistics_cfg = shipyard_cfg.get("logistics", {}) if isinstance(shipyard_cfg.get("logistics", {}), dict) else {}
        width = int(map_cfg.get("width_tiles", 100) or 100)
        height = int(map_cfg.get("height_tiles", 70) or 70)
        tile_time = float(map_cfg.get("tile_time_min", 0.12) or 0.12)

        zones: dict[str, ZoneRect] = {}
        for name, raw in (layout_cfg.get("zones", {}) if isinstance(layout_cfg.get("zones", {}), dict) else {}).items():
            if not isinstance(raw, dict):
                continue
            zones[str(name)] = ZoneRect(str(name), int(raw.get("x", 0)), int(raw.get("y", 0)), int(raw.get("w", 1)), int(raw.get("h", 1)))
        zones.setdefault("ShipDock", ZoneRect("ShipDock", 12, 9, 76, 46))
        zones.setdefault("BatteryZone", ZoneRect("BatteryZone", 4, 54, 16, 10))
        zones.setdefault("MaterialYard", ZoneRect("MaterialYard", 78, 5, 18, 10))
        zones.setdefault("PaintSupply", ZoneRect("PaintSupply", 4, 5, 18, 8))
        zones.setdefault("ScrapArea", ZoneRect("ScrapArea", 82, 54, 14, 10))
        zones.pop("ToolCrib", None)

        raw_center = surface_cfg.get("center", [50, 32])
        if isinstance(raw_center, dict):
            center_tile = (int(raw_center.get("x", 50) or 50), int(raw_center.get("y", 32) or 32))
        else:
            center = tuple(raw_center)
            center_tile = (int(center[0]), int(center[1])) if len(center) >= 2 else (50, 32)
        length = int(surface_cfg.get("length_tiles", surface_cfg.get("length", 62)) or 62)
        beam = int(surface_cfg.get("beam_tiles", surface_cfg.get("beam", 24)) or 24)
        target_count = int(surface_cfg.get("target_count", 120) or 120)
        hull_tiles = cls._build_ship_silhouette(center_tile, length, beam, width, height)
        surface_tiles = cls._surface_tiles(hull_tiles)
        selected_surface_tiles = cls._select_surface_tiles(surface_tiles, center_tile, target_count)
        lane_width = max(2, int(logistics_cfg.get("cart_lane_width_tiles", 2) or 2))
        parking_count = max(1, int(logistics_cfg.get("parking_spot_count", 6) or 6))
        cart_count = max(0, int(logistics_cfg.get("cart_count", 2) or 2))
        cart_route_tiles, cart_parking_spots, cart_source_tiles = cls._build_cart_route(
            hull_tiles=hull_tiles,
            zones=zones,
            width=width,
            height=height,
            lane_width=lane_width,
            parking_count=parking_count,
        )

        objects = {
            "material_yard": ObjectFootprint("material_yard", "material_rack", "MaterialYard", zones["MaterialYard"].x + 2, zones["MaterialYard"].y + 2, 12, 4, False),
            "paint_supply": ObjectFootprint("paint_supply", "paint_rack", "PaintSupply", zones["PaintSupply"].x + 2, zones["PaintSupply"].y + 2, 12, 3, False),
            "battery_rack": ObjectFootprint("battery_rack", "charger", "BatteryZone", zones["BatteryZone"].x + 3, zones["BatteryZone"].y + 3, 8, 3, False),
            "scrap_bin": ObjectFootprint("scrap_bin", "scrap_bin", "ScrapArea", zones["ScrapArea"].x + 3, zones["ScrapArea"].y + 3, 8, 4, False),
        }
        for index, segment in enumerate(cls._row_segments(hull_tiles), start=1):
            y, x0, width_run = segment
            objects[f"ship_hull_row_{index:03d}"] = ObjectFootprint(
                f"ship_hull_row_{index:03d}",
                "ship_hull_segment",
                "ShipDock",
                x0,
                y,
                width_run,
                1,
                True,
            )

        work_tiles: dict[str, ShipWorkTileLayout] = {}
        walls_for_service: set[Tile] = set()
        for index, tile in enumerate(selected_surface_tiles, start=1):
            service_tiles = tuple(
                service
                for service in cls._adjacent_tiles(tile)
                if 0 <= service[0] < width
                and 0 <= service[1] < height
                and service not in hull_tiles
                and service not in walls_for_service
            )
            if not service_tiles:
                raise ValueError(f"Ship work tile {tile} has no passable adjacent service tile")
            work_tile_id = f"SHIP-TILE-{index:04d}"
            work_tiles[work_tile_id] = ShipWorkTileLayout(work_tile_id=work_tile_id, tile=tile, service_tiles=service_tiles)

        return cls(
            width_tiles=width,
            height_tiles=height,
            tile_time_min=tile_time,
            zones=zones,
            objects=objects,
            work_tiles=work_tiles,
            ship_hull_tiles=hull_tiles,
            cart_route_tiles=cart_route_tiles,
            cart_parking_spots=cart_parking_spots,
            cart_source_tiles=cart_source_tiles,
            cart_count=cart_count,
        )

    @staticmethod
    def _build_ship_silhouette(center: Tile, length: int, beam: int, width: int, height: int) -> set[Tile]:
        cx, cy = center
        half_length = max(4.0, float(length) / 2.0)
        half_beam = max(3.0, float(beam) / 2.0)
        tiles: set[Tile] = set()
        for x in range(int(cx - half_length) - 1, int(cx + half_length) + 2):
            for y in range(int(cy - half_beam) - 1, int(cy + half_beam) + 2):
                if not (0 <= x < width and 0 <= y < height):
                    continue
                dx = abs((x + 0.5 - cx) / half_length)
                if dx > 1.0:
                    continue
                # A rounded, pointed outline: wide amidships and tapered bow/stern.
                local_half_beam = max(1.4, half_beam * (0.18 + 0.82 * (1.0 - dx**1.65)))
                dy = abs(y + 0.5 - cy)
                if dy <= local_half_beam:
                    tiles.add((x, y))
        return tiles

    @staticmethod
    def _adjacent_tiles(tile: Tile) -> tuple[Tile, ...]:
        x, y = tile
        return ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))

    @classmethod
    def _surface_tiles(cls, hull_tiles: set[Tile]) -> list[Tile]:
        return sorted(tile for tile in hull_tiles if any(neighbor not in hull_tiles for neighbor in cls._adjacent_tiles(tile)))

    @staticmethod
    def _select_surface_tiles(surface_tiles: list[Tile], center: Tile, target_count: int) -> list[Tile]:
        if target_count <= 0 or len(surface_tiles) <= target_count:
            return surface_tiles
        cx, cy = center
        ordered = sorted(surface_tiles, key=lambda tile: (math.atan2(tile[1] - cy, tile[0] - cx), tile[0], tile[1]))
        selected: list[Tile] = []
        used: set[Tile] = set()
        for index in range(target_count):
            candidate = ordered[min(len(ordered) - 1, round(index * (len(ordered) - 1) / max(1, target_count - 1)))]
            if candidate in used:
                continue
            selected.append(candidate)
            used.add(candidate)
        if len(selected) < target_count:
            for candidate in ordered:
                if candidate in used:
                    continue
                selected.append(candidate)
                used.add(candidate)
                if len(selected) >= target_count:
                    break
        return sorted(selected)

    @staticmethod
    def _row_segments(tiles: set[Tile]) -> list[tuple[int, int, int]]:
        rows: dict[int, list[int]] = {}
        for x, y in tiles:
            rows.setdefault(y, []).append(x)
        segments: list[tuple[int, int, int]] = []
        for y, xs in sorted(rows.items()):
            sorted_xs = sorted(xs)
            start = sorted_xs[0]
            previous = start
            for x in sorted_xs[1:]:
                if x == previous + 1:
                    previous = x
                    continue
                segments.append((y, start, previous - start + 1))
                start = previous = x
            segments.append((y, start, previous - start + 1))
        return segments

    @staticmethod
    def _add_lane_tile(out: set[Tile], tile: Tile, width: int, height: int) -> None:
        if 0 <= tile[0] < width and 0 <= tile[1] < height:
            out.add(tile)

    @classmethod
    def _add_lane_rect(cls, out: set[Tile], x0: int, x1: int, y0: int, y1: int, width: int, height: int) -> None:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                cls._add_lane_tile(out, (x, y), width, height)

    @classmethod
    def _add_manhattan_lane(cls, out: set[Tile], start: Tile, end: Tile, lane_width: int, width: int, height: int) -> None:
        half = max(0, lane_width - 1)
        sx, sy = start
        ex, ey = end
        for offset in range(0, half + 1):
            cls._add_lane_rect(out, sx, ex, sy + offset, sy + offset, width, height)
            cls._add_lane_rect(out, ex + offset, ex + offset, sy, ey, width, height)

    @classmethod
    def _build_cart_route(
        cls,
        *,
        hull_tiles: set[Tile],
        zones: dict[str, ZoneRect],
        width: int,
        height: int,
        lane_width: int,
        parking_count: int,
    ) -> tuple[set[Tile], dict[str, CartParkingSpotLayout], dict[str, Tile]]:
        if not hull_tiles:
            return set(), {}, {}
        xs = [tile[0] for tile in hull_tiles]
        ys = [tile[1] for tile in hull_tiles]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        margin = max(3, lane_width + 2)
        left = max(1, min_x - margin)
        right = min(width - 2, max_x + margin)
        top = max(1, min_y - margin)
        bottom = min(height - 2, max_y + margin)
        route: set[Tile] = set()
        cls._add_lane_rect(route, left, right, top, top + lane_width - 1, width, height)
        cls._add_lane_rect(route, left, right, bottom - lane_width + 1, bottom, width, height)
        cls._add_lane_rect(route, left, left + lane_width - 1, top, bottom, width, height)
        cls._add_lane_rect(route, right - lane_width + 1, right, top, bottom, width, height)

        cart_source_tiles: dict[str, Tile] = {}
        for source_name, zone in zones.items():
            if source_name not in {"MaterialYard", "PaintSupply"}:
                continue
            source_center = zone.center()
            target_x = right if source_center[0] >= (left + right) // 2 else left
            target_y = min(max(source_center[1], top), bottom)
            target = (target_x, target_y)
            cls._add_manhattan_lane(route, source_center, target, lane_width, width, height)
            # Loading must happen at the supply area itself. The route target is
            # only the junction where the source spur meets the hull ring.
            cart_source_tiles[source_name] = source_center

        candidate_spots = [
            (left, top + lane_width // 2),
            ((left + right) // 2, top + lane_width // 2),
            (right, top + lane_width // 2),
            (left, bottom - lane_width // 2),
            ((left + right) // 2, bottom - lane_width // 2),
            (right, bottom - lane_width // 2),
        ]
        spots: dict[str, CartParkingSpotLayout] = {}
        for index, candidate in enumerate(candidate_spots[:parking_count], start=1):
            if candidate not in route:
                route.add(candidate)
            parking_id = f"CART-PARK-{index:02d}"
            spots[parking_id] = CartParkingSpotLayout(parking_spot_id=parking_id, tile=candidate)
        return route - hull_tiles, spots, cart_source_tiles

    @staticmethod
    def _zone_door_tiles(zones: dict[str, ZoneRect], width: int, height: int) -> set[Tile]:
        doors: set[Tile] = set()
        dock = zones.get("ShipDock")
        if dock:
            for tile in [(dock.x + dock.width // 2 + dx, dock.y) for dx in (-1, 0, 1)]:
                doors.add(tile)
            for tile in [(dock.x + dock.width // 2 + dx, dock.y1) for dx in (-1, 0, 1)]:
                doors.add(tile)
            for tile in [(dock.x, dock.y + dock.height // 2 + dy) for dy in (-1, 0, 1)]:
                doors.add(tile)
            for tile in [(dock.x1, dock.y + dock.height // 2 + dy) for dy in (-1, 0, 1)]:
                doors.add(tile)
        for name, zone in zones.items():
            if name == "ShipDock":
                continue
            if dock:
                zone_cx, zone_cy = zone.center()
                dock_cx, dock_cy = dock.center()
                x_overlap0 = max(zone.x, dock.x)
                x_overlap1 = min(zone.x1, dock.x1)
                y_overlap0 = max(zone.y, dock.y)
                y_overlap1 = min(zone.y1, dock.y1)
                if zone_cx >= dock_cx and y_overlap0 <= y_overlap1:
                    y = (y_overlap0 + y_overlap1) // 2
                    candidates = [(zone.x + offset, y + dy) for offset in (0, 1) for dy in (-1, 0, 1)]
                    candidates += [(dock.x1, y + dy) for dy in (-1, 0, 1)]
                elif zone_cx < dock_cx and y_overlap0 <= y_overlap1:
                    y = (y_overlap0 + y_overlap1) // 2
                    candidates = [(zone.x1 - offset, y + dy) for offset in (0, 1) for dy in (-1, 0, 1)]
                    candidates += [(dock.x, y + dy) for dy in (-1, 0, 1)]
                elif zone_cy >= dock_cy and x_overlap0 <= x_overlap1:
                    x = (x_overlap0 + x_overlap1) // 2
                    candidates = [(x + dx, zone.y + offset) for offset in (0, 1) for dx in (-1, 0, 1)]
                    candidates += [(x + dx, dock.y1) for dx in (-1, 0, 1)]
                elif zone_cy < dock_cy and x_overlap0 <= x_overlap1:
                    x = (x_overlap0 + x_overlap1) // 2
                    candidates = [(x + dx, zone.y1 - offset) for offset in (0, 1) for dx in (-1, 0, 1)]
                    candidates += [(x + dx, dock.y) for dx in (-1, 0, 1)]
                else:
                    center_x, center_y = zone.center()
                    if center_y >= height // 2:
                        candidates = [(center_x + dx, zone.y) for dx in (-1, 0, 1)]
                    else:
                        candidates = [(center_x + dx, zone.y1) for dx in (-1, 0, 1)]
            else:
                center_x, center_y = zone.center()
                if center_y >= height // 2:
                    candidates = [(center_x + dx, zone.y) for dx in (-1, 0, 1)]
                else:
                    candidates = [(center_x + dx, zone.y1) for dx in (-1, 0, 1)]
            for tile in candidates:
                doors.add(tile)
        return {tile for tile in doors if 0 <= tile[0] < width and 0 <= tile[1] < height}

    @classmethod
    def _perimeter_walls_for_zones(cls, zones: dict[str, ZoneRect], width: int, height: int) -> set[Tile]:
        walls: set[Tile] = set()
        for zone in zones.values():
            for x in range(zone.x, zone.x + zone.width):
                walls.add((x, zone.y))
                walls.add((x, zone.y1))
            for y in range(zone.y, zone.y + zone.height):
                walls.add((zone.x, y))
                walls.add((zone.x1, y))
        for tile in cls._zone_door_tiles(zones, width, height):
            walls.discard(tile)
        return {tile for tile in walls if 0 <= tile[0] < width and 0 <= tile[1] < height}

    def _perimeter_walls(self) -> set[Tile]:
        return self._perimeter_walls_for_zones(self.zones, self.width_tiles, self.height_tiles)

    def in_bounds(self, tile: Tile) -> bool:
        x, y = tile
        return 0 <= x < self.width_tiles and 0 <= y < self.height_tiles

    def passable(self, tile: Tile) -> bool:
        return self.in_bounds(tile) and tile not in self.blocked_static

    def neighbors(self, tile: Tile) -> Iterable[Tile]:
        for nxt in self._adjacent_tiles(tile):
            if self.passable(nxt):
                yield nxt

    def find_path(self, start: Tile, goal: Tile) -> list[Tile]:
        if start == goal:
            return [start]
        frontier: list[tuple[int, int, Tile]] = []
        counter = 0
        heapq.heappush(frontier, (0, counter, start))
        came_from: dict[Tile, Tile | None] = {start: None}
        cost_so_far: dict[Tile, int] = {start: 0}
        while frontier:
            _, _, current = heapq.heappop(frontier)
            if current == goal:
                break
            for nxt in self.neighbors(current):
                new_cost = cost_so_far[current] + 1
                if nxt not in cost_so_far or new_cost < cost_so_far[nxt]:
                    cost_so_far[nxt] = new_cost
                    priority = new_cost + abs(goal[0] - nxt[0]) + abs(goal[1] - nxt[1])
                    counter += 1
                    heapq.heappush(frontier, (priority, counter, nxt))
                    came_from[nxt] = current
        if goal not in came_from:
            return [start, goal]
        path: list[Tile] = []
        cur: Tile | None = goal
        while cur is not None:
            path.append(cur)
            cur = came_from[cur]
        return list(reversed(path))

    def travel_time(self, start: Tile, goal: Tile) -> float:
        return max(0, len(self.find_path(start, goal)) - 1) * self.tile_time_min

    def work_tile_entity_id(self, work_tile_id: str) -> str:
        suffix = str(work_tile_id).replace("SHIP-TILE-", "").replace("_", "-")
        return f"ship_tile_{suffix}"

    def work_tile_service_tile(self, work_tile_id: str, current: Tile) -> Tile:
        layout = self.work_tiles[str(work_tile_id)]
        return min(layout.service_tiles, key=lambda tile: abs(tile[0] - current[0]) + abs(tile[1] - current[1]))

    def section_service_tile(self, work_tile_id: str, current: Tile) -> Tile:
        # Compatibility shim for older shipyard code paths that used section ids.
        return self.work_tile_service_tile(work_tile_id, current)

    def initial_worker_tile(self, worker_id: str) -> Tile:
        # Start workers in an open staging strip near the upper-left dock area.
        paint_zone = self.zones.get("PaintSupply")
        base = (paint_zone.x + 2, paint_zone.y1 + 3) if paint_zone else (8, 15)
        try:
            idx = max(0, int(str(worker_id)[1:]) - 1)
        except ValueError:
            idx = 0
        candidate = (base[0] + idx * 2, base[1])
        if self.passable(candidate):
            return candidate
        for radius in range(1, 8):
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if abs(dx) + abs(dy) != radius:
                        continue
                    fallback = (candidate[0] + dx, candidate[1] + dy)
                    if self.passable(fallback):
                        return fallback
        return candidate

    def initial_cart_tile(self, cart_id: str) -> Tile:
        try:
            idx = max(0, int(str(cart_id).split("-")[-1]) - 1)
        except ValueError:
            idx = 0
        preferred_ids = [
            "CART-PARK-01",
            "CART-PARK-04",
            "CART-PARK-03",
            "CART-PARK-06",
            "CART-PARK-02",
            "CART-PARK-05",
        ]
        ordered_spots = [self.cart_parking_spots[spot_id] for spot_id in preferred_ids if spot_id in self.cart_parking_spots]
        ordered_spots.extend(spot for spot_id, spot in self.cart_parking_spots.items() if spot_id not in preferred_ids)
        spots = ordered_spots
        if not spots:
            return self.initial_worker_tile("A1")
        return spots[idx % len(spots)].tile

    def nearest_cart_parking_spot(self, tile: Tile) -> CartParkingSpotLayout | None:
        if not self.cart_parking_spots:
            return None
        return min(self.cart_parking_spots.values(), key=lambda spot: abs(spot.tile[0] - tile[0]) + abs(spot.tile[1] - tile[1]))

    def cart_source_tile(self, source: str) -> Tile | None:
        return self.cart_source_tiles.get(str(source))

    def find_cart_route_path(self, start: Tile, goal: Tile, blocked_tiles: set[Tile] | None = None, footprint_tiles: int = 1) -> list[Tile]:
        allowed = set(self.cart_route_tiles) | {spot.tile for spot in self.cart_parking_spots.values()} | set(self.cart_source_tiles.values())
        blocked = set(blocked_tiles or set()) - {start}

        def step_heading(source: Tile, target: Tile) -> Tile:
            dx = max(-1, min(1, target[0] - source[0]))
            dy = max(-1, min(1, target[1] - source[1]))
            if abs(dx) + abs(dy) != 1:
                return (0, 1)
            return (dx, dy)

        def footprint_clear(anchor: Tile, heading: Tile) -> bool:
            if anchor not in allowed or anchor in blocked:
                return False
            if footprint_tiles <= 1:
                return True
            dx, dy = heading
            for offset in range(1, max(1, int(footprint_tiles))):
                tile = (anchor[0] + dx * offset, anchor[1] + dy * offset)
                if tile not in allowed or tile in blocked:
                    return False
            return True

        if start == goal:
            return [start]
        if start not in allowed or goal not in allowed:
            return [start, goal]

        def reconstruct(came_from: dict[Tile, Tile | None]) -> list[Tile] | None:
            if goal not in came_from:
                return None
            path: list[Tile] = []
            cur: Tile | None = goal
            while cur is not None:
                path.append(cur)
                cur = came_from[cur]
            return list(reversed(path))

        def search(*, enforce_footprint: bool) -> list[Tile] | None:
            frontier: list[Tile] = [start]
            came_from: dict[Tile, Tile | None] = {start: None}
            while frontier:
                current = frontier.pop(0)
                if current == goal:
                    break
                for nxt in self._adjacent_tiles(current):
                    if nxt in came_from or nxt in blocked or nxt not in allowed:
                        continue
                    if enforce_footprint and not footprint_clear(nxt, step_heading(current, nxt)):
                        continue
                    came_from[nxt] = current
                    frontier.append(nxt)
            return reconstruct(came_from)

        path = search(enforce_footprint=footprint_tiles > 1)
        if path is not None:
            return path
        # Tight source/parking endpoints can reject a two-tile footprint even
        # though the lane anchor is connected. Preserve visible lane movement
        # instead of exporting a start->goal jump.
        path = search(enforce_footprint=False)
        return path if path is not None else [start, goal]

    def tile_payload(self, tile: Tile | None) -> dict[str, int] | None:
        return None if tile is None else {"x": int(tile[0]), "y": int(tile[1])}

    def path_payload(self, path: Iterable[Tile]) -> list[dict[str, int]]:
        return [self.tile_payload(tile) for tile in path if tile is not None]  # type: ignore[list-item]

    def tile_to_position(self, tile: Tile) -> dict[str, float]:
        tile_w = 1600.0 / max(1, self.width_tiles)
        tile_h = 960.0 / max(1, self.height_tiles)
        return {"x": (tile[0] + 0.5) * tile_w, "y": (tile[1] + 0.5) * tile_h}

    def to_replay_layout(self, worker_ids: Iterable[str]) -> dict[str, Any]:
        tile_w = 1600.0 / max(1, self.width_tiles)
        tile_h = 960.0 / max(1, self.height_tiles)

        def pos(zone: ZoneRect) -> dict[str, float]:
            return {"x": zone.x * tile_w, "y": zone.y * tile_h}

        def size(zone: ZoneRect) -> dict[str, float]:
            return {"width": zone.width * tile_w, "height": zone.height * tile_h}

        regions = [
            {
                "region_id": self.REGION_ID[name],
                "label": self.REGION_LABEL[name],
                "kind": self.REGION_KIND[name],
                "position": pos(zone),
                "size": size(zone),
                "accent": self.REGION_ACCENT[name],
            }
            for name, zone in self.zones.items()
        ]
        nodes: list[dict[str, Any]] = []
        for obj in self.objects.values():
            nodes.append(
                {
                    "entity_id": obj.object_id,
                    "entity_type": obj.object_type,
                    "region_id": self.REGION_ID[obj.zone],
                    "position": self.tile_to_position(obj.center()),
                    "tile": self.tile_payload(obj.center()),
                    "footprint": {"x": obj.x, "y": obj.y, "width": obj.width, "height": obj.height},
                }
            )
        for work_tile_id, layout in self.work_tiles.items():
            entity_id = self.work_tile_entity_id(work_tile_id)
            nodes.append(
                {
                    "entity_id": entity_id,
                    "entity_type": "ship_work_tile",
                    "region_id": self.REGION_ID["ShipDock"],
                    "position": self.tile_to_position(layout.tile),
                    "tile": self.tile_payload(layout.tile),
                    "footprint": {"x": layout.tile[0], "y": layout.tile[1], "width": 1, "height": 1},
                    "attributes": {
                        "work_tile_id": work_tile_id,
                        "surface_tile_state": "WAIT_WELD",
                        "ship_surface_state": "WAIT_WELD",
                    },
                }
            )
        for worker_id in worker_ids:
            tile = self.initial_worker_tile(str(worker_id))
            nodes.append(
                {
                    "entity_id": str(worker_id),
                    "entity_type": "worker",
                    "region_id": self.REGION_ID["ShipDock"],
                    "position": self.tile_to_position(tile),
                    "tile": self.tile_payload(tile),
                }
            )
        for spot in self.cart_parking_spots.values():
            nodes.append(
                {
                    "entity_id": spot.parking_spot_id,
                    "entity_type": "cart_parking_spot",
                    "region_id": self.REGION_ID["ShipDock"],
                    "position": self.tile_to_position(spot.tile),
                    "tile": self.tile_payload(spot.tile),
                    "footprint": {"x": spot.tile[0], "y": spot.tile[1], "width": 1, "height": 1},
                    "attributes": {"parking_spot_id": spot.parking_spot_id, "kind": "cart_parking"},
                }
            )
        for index in range(1, self.cart_count + 1):
            cart_id = f"CART-{index:02d}"
            tile = self.initial_cart_tile(cart_id)
            nodes.append(
                {
                    "entity_id": cart_id,
                    "entity_type": "cart",
                    "region_id": self.REGION_ID["ShipDock"],
                    "position": self.tile_to_position(tile),
                    "tile": self.tile_payload(tile),
                    "attributes": {
                        "cart_id": cart_id,
                        "status": "parked",
                        "inventory_kind": "",
                        "inventory_count": 0,
                        "reserved_count": 0,
                    },
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
                "cart_route_tiles": self.path_payload(sorted(self.cart_route_tiles)),
                "cart_parking_tiles": self.path_payload(spot.tile for spot in self.cart_parking_spots.values()),
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
                "service_tiles": {self.work_tile_entity_id(tile_id): self.path_payload(layout.service_tiles) for tile_id, layout in self.work_tiles.items()},
            },
        }
