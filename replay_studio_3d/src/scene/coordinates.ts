import type { XY } from "../replay-core/types/entity";
import type { LayoutGridConfig, LayoutGridObjectFootprint } from "../replay-core/types/layout";

export interface WorldPoint {
  x: number;
  y: number;
  z: number;
}

export interface WorldRect {
  center: WorldPoint;
  width: number;
  depth: number;
}

export interface CoordinateMapper {
  gridWidth: number;
  gridHeight: number;
  tileWidth: number;
  tileHeight: number;
  pointToWorld(point: XY, y?: number): WorldPoint;
  tileCenterToWorld(tile: XY, y?: number): WorldPoint;
  footprintToWorldRect(footprint: LayoutGridObjectFootprint, y?: number): WorldRect;
  viewportSizeToWorld(size: { width: number; height: number }): { width: number; depth: number };
}

export const DEFAULT_VIEWPORT = { width: 1600, height: 960 };
export const DEFAULT_GRID = { width_tiles: 100, height_tiles: 70 } satisfies Pick<LayoutGridConfig, "width_tiles" | "height_tiles">;

export function createCoordinateMapper(
  grid: LayoutGridConfig | undefined,
  viewport: { width: number; height: number } = DEFAULT_VIEWPORT,
): CoordinateMapper {
  const gridWidth = grid?.width_tiles ?? DEFAULT_GRID.width_tiles;
  const gridHeight = grid?.height_tiles ?? DEFAULT_GRID.height_tiles;
  const tileWidth = viewport.width / gridWidth;
  const tileHeight = viewport.height / gridHeight;

  function pointToWorld(point: XY, y = 0): WorldPoint {
    return {
      x: point.x / tileWidth - gridWidth / 2,
      y,
      z: point.y / tileHeight - gridHeight / 2,
    };
  }

  function tileCenterToWorld(tile: XY, y = 0): WorldPoint {
    return {
      x: tile.x - gridWidth / 2,
      y,
      z: tile.y - gridHeight / 2,
    };
  }

  function footprintToWorldRect(footprint: LayoutGridObjectFootprint, y = 0): WorldRect {
    return {
      center: tileCenterToWorld({ x: footprint.x + footprint.width / 2, y: footprint.y + footprint.height / 2 }, y),
      width: footprint.width,
      depth: footprint.height,
    };
  }

  function viewportSizeToWorld(size: { width: number; height: number }): { width: number; depth: number } {
    return {
      width: size.width / tileWidth,
      depth: size.height / tileHeight,
    };
  }

  return { gridWidth, gridHeight, tileWidth, tileHeight, pointToWorld, tileCenterToWorld, footprintToWorldRect, viewportSizeToWorld };
}

function asXY(value: unknown): XY | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Record<string, unknown>;
  const x = Number(candidate.x);
  const y = Number(candidate.y);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : undefined;
}

export function motionPathPoints(motion: unknown): XY[] {
  if (!motion || typeof motion !== "object") return [];
  const payload = motion as Record<string, unknown>;
  if (Array.isArray(payload.path)) {
    const path = payload.path.map(asXY).filter((point): point is XY => Boolean(point));
    if (path.length >= 2) return path;
  }
  const from = asXY(payload.from);
  const to = asXY(payload.to);
  return from && to ? [from, to] : [];
}

export function motionDisplayPathPoints(motion: unknown): XY[] {
  if (!motion || typeof motion !== "object") return [];
  const displayPath = (motion as Record<string, unknown>).display_path;
  if (Array.isArray(displayPath)) {
    const path = displayPath.map(asXY).filter((point): point is XY => Boolean(point));
    if (path.length >= 2) return path;
  }
  return motionPathPoints(motion);
}

export function isMotionActive(motion: unknown, currentTime: number): boolean {
  if (!motion || typeof motion !== "object") return false;
  const payload = motion as Record<string, unknown>;
  if (payload.paused === true) return false;
  const startedAt = Number(payload.started_at);
  const endedAt = Number(payload.ended_at);
  return Number.isFinite(startedAt) && Number.isFinite(endedAt) && endedAt > startedAt && currentTime >= startedAt && currentTime <= endedAt;
}

export function samplePath(points: XY[], progress: number): { point: XY; angle: number } | undefined {
  if (points.length < 2) return undefined;
  const distances: number[] = [];
  let total = 0;
  for (let index = 1; index < points.length; index += 1) {
    const distance = Math.hypot(points[index].x - points[index - 1].x, points[index].y - points[index - 1].y);
    distances.push(distance);
    total += distance;
  }
  if (total <= 0) return undefined;
  const targetDistance = Math.max(0, Math.min(1, progress)) * total;
  let walked = 0;
  for (let index = 1; index < points.length; index += 1) {
    const segment = distances[index - 1];
    if (segment <= 0) continue;
    if (walked + segment >= targetDistance) {
      const source = points[index - 1];
      const target = points[index];
      const local = (targetDistance - walked) / segment;
      return {
        point: {
          x: source.x + (target.x - source.x) * local,
          y: source.y + (target.y - source.y) * local,
        },
        angle: Math.atan2(target.y - source.y, target.x - source.x),
      };
    }
    walked += segment;
  }
  const previous = points[points.length - 2];
  const last = points[points.length - 1];
  return { point: last, angle: Math.atan2(last.y - previous.y, last.x - previous.x) };
}

export function footprintForEntity(grid: LayoutGridConfig | undefined, entityId: string): LayoutGridObjectFootprint | undefined {
  return grid?.object_footprints?.find((footprint) => footprint.object_id === entityId);
}
