import { describe, expect, it } from "vitest";
import { createCoordinateMapper, motionPathPoints, samplePath, workerCameraPose } from "../scene/coordinates";
import type { BaseEntityState } from "../replay-core/types/entity";

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

  it("builds an eye-level first-person pose from active motion", () => {
    const mapper = createCoordinateMapper({ width_tiles: 100, height_tiles: 100 }, { width: 100, height: 100 });
    const worker: BaseEntityState = {
      entity_id: "A1",
      entity_type: "worker",
      state: "moving",
      label: "A1",
      position: { x: 0, y: 0 },
      attributes: {
        motion: {
          path: [{ x: 0, y: 0 }, { x: 10, y: 0 }],
          from: { x: 0, y: 0 },
          to: { x: 10, y: 0 },
          started_at: 0,
          ended_at: 10,
        },
      },
      relations: {},
      updated_at: 0,
    };
    const pose = workerCameraPose(worker, 5, mapper);
    expect(pose?.sourcePoint.x).toBeCloseTo(5);
    expect(pose?.sourcePoint.y).toBeCloseTo(0);
    expect(pose?.position.y).toBeCloseTo(1.75);
    expect((pose?.target.x ?? 0) > (pose?.position.x ?? 0)).toBe(true);
  });

  it("keeps stationary first-person heading from the last movement", () => {
    const mapper = createCoordinateMapper({ width_tiles: 100, height_tiles: 100 }, { width: 100, height: 100 });
    const worker: BaseEntityState = {
      entity_id: "A2",
      entity_type: "worker",
      state: "idle",
      label: "A2",
      position: { x: 10, y: 10 },
      attributes: { last_heading_angle: Math.PI / 2 },
      relations: {},
      updated_at: 0,
    };
    const pose = workerCameraPose(worker, 20, mapper);
    expect(pose?.sourcePoint).toEqual({ x: 10, y: 10 });
    expect((pose?.target.z ?? 0) > (pose?.position.z ?? 0)).toBe(true);
  });

  it("does not move the first-person camera along paused motion", () => {
    const mapper = createCoordinateMapper({ width_tiles: 100, height_tiles: 100 }, { width: 100, height: 100 });
    const worker: BaseEntityState = {
      entity_id: "A3",
      entity_type: "worker",
      state: "blocked",
      label: "A3",
      position: { x: 30, y: 30 },
      attributes: {
        motion: {
          paused: true,
          path: [{ x: 30, y: 30 }, { x: 60, y: 30 }],
          from: { x: 30, y: 30 },
          to: { x: 60, y: 30 },
          started_at: 10,
          ended_at: 20,
        },
      },
      relations: {},
      updated_at: 10,
    };
    const pose = workerCameraPose(worker, 15, mapper);
    expect(pose?.sourcePoint).toEqual({ x: 30, y: 30 });
  });
});
