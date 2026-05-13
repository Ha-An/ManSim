import { describe, expect, it } from "vitest";
import { createCoordinateMapper, motionPathPoints, samplePath } from "../scene/coordinates";

describe("3D coordinate mapper", () => {
  it("maps viewport center to world origin", () => {
    const mapper = createCoordinateMapper({ width_tiles: 100, height_tiles: 70 }, { width: 1600, height: 960 });
    expect(mapper.pointToWorld({ x: 800, y: 480 })).toEqual({ x: 0, y: 0, z: 0 });
  });

  it("preserves footprint size in tile units", () => {
    const mapper = createCoordinateMapper({ width_tiles: 100, height_tiles: 70 }, { width: 1600, height: 960 });
    const rect = mapper.footprintToWorldRect({ object_id: "machine", x: 12, y: 10, width: 6, height: 4 });
    expect(rect.width).toBe(6);
    expect(rect.depth).toBe(4);
    expect(rect.center.x).toBeCloseTo(-35);
    expect(rect.center.z).toBeCloseTo(-23);
  });

  it("extracts explicit motion path before falling back to endpoints", () => {
    expect(motionPathPoints({ path: [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 1, y: 1 }], from: { x: 9, y: 9 }, to: { x: 10, y: 10 } })).toHaveLength(3);
    expect(motionPathPoints({ from: { x: 9, y: 9 }, to: { x: 10, y: 10 } })).toEqual([
      { x: 9, y: 9 },
      { x: 10, y: 10 },
    ]);
  });

  it("samples along polyline distance rather than point index", () => {
    const sample = samplePath([{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }], 0.75);
    expect(sample?.point.x).toBeCloseTo(10);
    expect(sample?.point.y).toBeCloseTo(5);
  });
});

