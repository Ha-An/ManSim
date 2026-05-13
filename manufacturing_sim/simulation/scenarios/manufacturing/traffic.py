from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from manufacturing_sim.simulation.scenarios.manufacturing.grid_map import Tile


def _tile_payload(tile: Tile | None) -> dict[str, int] | None:
    if tile is None:
        return None
    return {"x": int(tile[0]), "y": int(tile[1])}


def _edge_key(a: Tile, b: Tile) -> tuple[Tile, Tile]:
    return (a, b) if a <= b else (b, a)


TIME_EPS = 1e-6


def _intervals_overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return max(a_start, b_start) + TIME_EPS < min(a_end, b_end)


def _interval_gap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    if _intervals_overlap(a_start, a_end, b_start, b_end):
        return 0.0
    if a_end <= b_start:
        return b_start - a_end
    return max(0.0, a_start - b_end)


@dataclass(frozen=True)
class TrafficPlan:
    move_id: str
    worker_id: str
    path_tiles: tuple[Tile, ...]
    started_at: float
    ended_at: float

    @property
    def tile_set(self) -> set[Tile]:
        return set(self.path_tiles)

    @property
    def edges(self) -> set[tuple[Tile, Tile]]:
        return {_edge_key(src, dst) for src, dst in zip(self.path_tiles, self.path_tiles[1:])}


@dataclass(frozen=True)
class TrafficSegment:
    move_id: str
    worker_id: str
    segment_index: int
    from_tile: Tile
    to_tile: Tile
    started_at: float
    ended_at: float

    @property
    def edge(self) -> tuple[Tile, Tile]:
        return (self.from_tile, self.to_tile)

    @property
    def undirected_edge(self) -> tuple[Tile, Tile]:
        return _edge_key(self.from_tile, self.to_tile)


@dataclass(frozen=True)
class TrafficConflict:
    conflict_id: str
    conflict_type: str
    severity: str
    primary_worker_id: str
    other_worker_id: str
    move_id: str
    other_move_id: str
    started_at: float
    ended_at: float
    collision: bool = False
    tile: Tile | None = None
    edge_from: Tile | None = None
    edge_to: Tile | None = None
    other_edge_from: Tile | None = None
    other_edge_to: Tile | None = None
    gap_min: float | None = None
    overlap_tiles: tuple[Tile, ...] = ()
    overlap_edges: tuple[tuple[Tile, Tile], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "conflict_id": self.conflict_id,
            "conflict_type": self.conflict_type,
            "severity": self.severity,
            "collision": bool(self.collision),
            "primary_worker_id": self.primary_worker_id,
            "other_worker_id": self.other_worker_id,
            "worker_ids": sorted([self.primary_worker_id, self.other_worker_id]),
            "move_id": self.move_id,
            "other_move_id": self.other_move_id,
            "time_window": {
                "started_at": round(float(self.started_at), 3),
                "ended_at": round(float(self.ended_at), 3),
            },
        }
        if self.tile is not None:
            payload["tile"] = _tile_payload(self.tile)
        if self.edge_from is not None and self.edge_to is not None:
            payload["edge"] = {"from": _tile_payload(self.edge_from), "to": _tile_payload(self.edge_to)}
        if self.other_edge_from is not None and self.other_edge_to is not None:
            payload["other_edge"] = {"from": _tile_payload(self.other_edge_from), "to": _tile_payload(self.other_edge_to)}
        if self.gap_min is not None:
            payload["gap_min"] = round(float(self.gap_min), 6)
        if self.overlap_tiles:
            payload["overlap_tiles"] = [_tile_payload(tile) for tile in self.overlap_tiles]
        if self.overlap_edges:
            payload["overlap_edges"] = [
                {"from": _tile_payload(src), "to": _tile_payload(dst)}
                for src, dst in self.overlap_edges
            ]
        return payload


class TrafficMonitor:
    def __init__(self, *, near_miss_headway_min: float = 0.05, recent_retention_min: float = 2.0) -> None:
        self.near_miss_headway_min = max(0.0, float(near_miss_headway_min))
        self.recent_retention_min = max(self.near_miss_headway_min, float(recent_retention_min))
        self.active_plans: dict[str, TrafficPlan] = {}
        self.active_segments: dict[tuple[str, str, int], TrafficSegment] = {}
        self.recent_segments: list[TrafficSegment] = []
        self._counter = 0
        self._emitted_keys: set[tuple[Any, ...]] = set()

    def register_plan(self, plan: TrafficPlan) -> list[TrafficConflict]:
        conflicts: list[TrafficConflict] = []
        for other in list(self.active_plans.values()):
            if other.worker_id == plan.worker_id:
                continue
            overlap_tiles = tuple(sorted(plan.tile_set & other.tile_set))
            overlap_edges = tuple(sorted(plan.edges & other.edges))
            if not overlap_tiles and not overlap_edges:
                continue
            key = (
                "PATH_OVERLAP",
                tuple(sorted([plan.worker_id, other.worker_id])),
                tuple(overlap_tiles),
                tuple(overlap_edges),
                round(min(plan.started_at, other.started_at), 3),
            )
            if key in self._emitted_keys:
                continue
            self._emitted_keys.add(key)
            intervals_overlap = _intervals_overlap(plan.started_at, plan.ended_at, other.started_at, other.ended_at)
            conflicts.append(
                self._conflict(
                    conflict_type="PATH_OVERLAP",
                    severity="info",
                    primary=plan.worker_id,
                    other=other.worker_id,
                    move_id=plan.move_id,
                    other_move_id=other.move_id,
                    started_at=max(plan.started_at, other.started_at) if intervals_overlap else min(plan.started_at, other.started_at),
                    ended_at=min(plan.ended_at, other.ended_at) if intervals_overlap else max(plan.ended_at, other.ended_at),
                    overlap_tiles=overlap_tiles,
                    overlap_edges=overlap_edges,
                )
            )
        self.active_plans[plan.move_id] = plan
        return conflicts

    def complete_plan(self, move_id: str) -> None:
        self.active_plans.pop(move_id, None)

    def begin_segment(self, segment: TrafficSegment) -> list[TrafficConflict]:
        self._expire_active_segments(segment.started_at)
        self._prune(segment.started_at)
        conflicts: list[TrafficConflict] = []
        for other in list(self.active_segments.values()):
            if other.worker_id == segment.worker_id:
                continue
            conflicts.extend(self._conflicts_between(segment, other, active=True))
        for other in list(self.recent_segments):
            if other.worker_id == segment.worker_id:
                continue
            conflicts.extend(self._conflicts_between(segment, other, active=False))
        self.active_segments[(segment.worker_id, segment.move_id, segment.segment_index)] = segment
        return conflicts

    def end_segment(self, worker_id: str, move_id: str, segment_index: int, ended_at: float | None = None) -> None:
        key = (worker_id, move_id, segment_index)
        segment = self.active_segments.pop(key, None)
        if segment is None:
            return
        if ended_at is not None and abs(float(ended_at) - segment.ended_at) > 1e-9:
            segment = TrafficSegment(
                move_id=segment.move_id,
                worker_id=segment.worker_id,
                segment_index=segment.segment_index,
                from_tile=segment.from_tile,
                to_tile=segment.to_tile,
                started_at=segment.started_at,
                ended_at=float(ended_at),
            )
        self.recent_segments.append(segment)
        self._prune(segment.ended_at)

    def _conflicts_between(self, segment: TrafficSegment, other: TrafficSegment, *, active: bool) -> list[TrafficConflict]:
        gap = _interval_gap(segment.started_at, segment.ended_at, other.started_at, other.ended_at)
        overlap = gap == 0.0
        if not overlap and gap > self.near_miss_headway_min:
            return []

        conflict_type = ""
        severity = "warning"
        collision = False
        tile: Tile | None = None

        if overlap and segment.to_tile == other.to_tile:
            conflict_type = "TILE_CONFLICT"
            severity = "error"
            collision = True
            tile = segment.to_tile
        elif overlap and segment.from_tile == other.to_tile and segment.to_tile == other.from_tile:
            conflict_type = "EDGE_CONFLICT"
            severity = "error"
            collision = True
        elif segment.undirected_edge == other.undirected_edge or segment.to_tile in {other.from_tile, other.to_tile}:
            conflict_type = "NEAR_MISS"
            severity = "warning"
            tile = segment.to_tile if segment.to_tile in {other.from_tile, other.to_tile} else None

        if not conflict_type:
            return []

        key = (
            conflict_type,
            tuple(sorted([segment.worker_id, other.worker_id])),
            segment.undirected_edge,
            other.undirected_edge,
            round(min(segment.started_at, other.started_at), 3),
            round(max(segment.ended_at, other.ended_at), 3),
        )
        if key in self._emitted_keys:
            return []
        self._emitted_keys.add(key)
        return [
            self._conflict(
                conflict_type=conflict_type,
                severity=severity,
                primary=segment.worker_id,
                other=other.worker_id,
                move_id=segment.move_id,
                other_move_id=other.move_id,
                started_at=max(segment.started_at, other.started_at) if active else min(segment.started_at, other.started_at),
                ended_at=min(segment.ended_at, other.ended_at) if active else max(segment.ended_at, other.ended_at),
                collision=collision,
                tile=tile,
                edge_from=segment.from_tile,
                edge_to=segment.to_tile,
                other_edge_from=other.from_tile,
                other_edge_to=other.to_tile,
                gap_min=gap,
            )
        ]

    def _conflict(self, **kwargs: Any) -> TrafficConflict:
        self._counter += 1
        if "primary" in kwargs:
            kwargs["primary_worker_id"] = kwargs.pop("primary")
        if "other" in kwargs:
            kwargs["other_worker_id"] = kwargs.pop("other")
        return TrafficConflict(conflict_id=f"traffic-{self._counter:06d}", **kwargs)

    def _prune(self, now: float) -> None:
        cutoff = float(now) - self.recent_retention_min
        self.recent_segments = [segment for segment in self.recent_segments if segment.ended_at >= cutoff]

    def _expire_active_segments(self, now: float) -> None:
        for key, segment in list(self.active_segments.items()):
            if segment.ended_at <= float(now) + TIME_EPS:
                self.active_segments.pop(key, None)
                self.recent_segments.append(segment)


__all__ = ["TrafficConflict", "TrafficMonitor", "TrafficPlan", "TrafficSegment"]
