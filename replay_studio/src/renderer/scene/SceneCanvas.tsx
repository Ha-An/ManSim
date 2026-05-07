import { useEffect, useMemo, useRef, useState } from "react";
import type { ReplayEvent } from "../../core/types/event";
import type { ReplayRenderModel, RenderRegion } from "../../core/types/replay";
import { getEntityNodeStyle, getNodeSize } from "../nodes/EntityNode";
import { getMachineSpriteFrame, loadMachineSpriteSet, type MachineSpriteSet } from "../nodes/machineSpriteSet";
import { loadSceneIconSet, type SceneIconFrame, type SceneIconSet } from "../nodes/sceneIconSet";
import { getWorkerSpriteFrame, loadWorkerSpriteSheet, type WorkerSpriteSheet } from "../nodes/workerSpriteSheet";
import { getWorkerVisualState } from "../nodes/workerVisualState";

interface SceneCanvasProps {
  width: number;
  height: number;
  viewport: { width: number; height: number };
  renderModel: ReplayRenderModel;
  currentEvent?: ReplayEvent;
  currentTime: number;
  onSelectEntity?: (entityId: string) => void;
}

interface NodeBounds {
  entityId: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

function drawPixelGrid(ctx: CanvasRenderingContext2D, width: number, height: number): void {
  const background = ctx.createLinearGradient(0, 0, width, height);
  background.addColorStop(0, "#edf4fb");
  background.addColorStop(0.55, "#e8f0fa");
  background.addColorStop(1, "#f2f6fb");
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, width, height);
}

function drawTileGridFloor(
  ctx: CanvasRenderingContext2D,
  renderModel: ReplayRenderModel,
  viewport: { width: number; height: number },
  width: number,
  height: number,
): void {
  const grid = renderModel.grid;
  if (!grid?.width_tiles || !grid?.height_tiles) return;
  const tileWidth = viewport.width / grid.width_tiles;
  const tileHeight = viewport.height / grid.height_tiles;

  ctx.save();
  for (let y = 0; y < grid.height_tiles; y += 1) {
    for (let x = 0; x < grid.width_tiles; x += 1) {
      const topLeft = project({ x: x * tileWidth, y: y * tileHeight }, viewport, width, height);
      const bottomRight = project({ x: (x + 1) * tileWidth, y: (y + 1) * tileHeight }, viewport, width, height);
      const checker = (x + y) % 2;
      const block = (Math.floor(x / 5) + Math.floor(y / 5)) % 2;
      ctx.fillStyle =
        checker === 0
          ? block === 0
            ? "rgba(217, 228, 244, 0.56)"
            : "rgba(224, 234, 248, 0.50)"
          : block === 0
            ? "rgba(232, 239, 250, 0.62)"
            : "rgba(222, 233, 247, 0.58)";
      ctx.fillRect(topLeft.x, topLeft.y, bottomRight.x - topLeft.x, bottomRight.y - topLeft.y);
    }
  }
  ctx.restore();
}

function drawTileGridOverlays(
  ctx: CanvasRenderingContext2D,
  renderModel: ReplayRenderModel,
  viewport: { width: number; height: number },
  width: number,
  height: number,
): void {
  const grid = renderModel.grid;
  if (!grid?.width_tiles || !grid?.height_tiles) return;
  const tileWidth = viewport.width / grid.width_tiles;
  const tileHeight = viewport.height / grid.height_tiles;

  const tileRect = (tile: { x: number; y: number }) => {
    const topLeft = project({ x: tile.x * tileWidth, y: tile.y * tileHeight }, viewport, width, height);
    const bottomRight = project({ x: (tile.x + 1) * tileWidth, y: (tile.y + 1) * tileHeight }, viewport, width, height);
    return {
      x: topLeft.x,
      y: topLeft.y,
      width: bottomRight.x - topLeft.x,
      height: bottomRight.y - topLeft.y,
    };
  };

  ctx.save();
  ctx.fillStyle = "rgba(13, 30, 52, 0.94)";
  ctx.strokeStyle = "rgba(0, 0, 0, 0.92)";
  ctx.lineWidth = 1;
  for (const wall of grid.walls ?? []) {
    const rect = tileRect(wall);
    const x = rect.x - 0.8;
    const y = rect.y - 0.8;
    const wallWidth = Math.max(2, rect.width + 1.6);
    const wallHeight = Math.max(2, rect.height + 1.6);
    ctx.fillRect(x, y, wallWidth, wallHeight);
    ctx.strokeRect(x + 0.5, y + 0.5, Math.max(1, wallWidth - 1), Math.max(1, wallHeight - 1));
  }

  ctx.fillStyle = "rgba(246, 185, 65, 0.94)";
  for (const door of grid.doors ?? []) {
    const rect = tileRect(door);
    ctx.fillRect(rect.x - 1, rect.y - 1, Math.max(4, rect.width + 2), Math.max(4, rect.height + 2));
  }

  ctx.fillStyle = "rgba(16, 38, 68, 0.075)";
  ctx.strokeStyle = "rgba(16, 38, 68, 0.28)";
  ctx.lineWidth = 1.2;
  for (const footprint of grid.object_footprints ?? []) {
    if (["machine", "queue", "buffer"].includes(String(footprint.object_type ?? ""))) {
      continue;
    }
    const topLeft = project({ x: footprint.x * tileWidth, y: footprint.y * tileHeight }, viewport, width, height);
    const bottomRight = project(
      { x: (footprint.x + footprint.width) * tileWidth, y: (footprint.y + footprint.height) * tileHeight },
      viewport,
      width,
      height,
    );
    const rectWidth = bottomRight.x - topLeft.x;
    const rectHeight = bottomRight.y - topLeft.y;
    ctx.fillRect(topLeft.x, topLeft.y, rectWidth, rectHeight);
    ctx.strokeRect(topLeft.x, topLeft.y, rectWidth, rectHeight);
  }
  ctx.restore();
}

function entityFootprintRect(
  grid: ReplayRenderModel["grid"],
  entityId: string,
  viewport: { width: number; height: number },
  width: number,
  height: number,
) {
  if (!grid?.width_tiles || !grid?.height_tiles) return undefined;
  const footprint = grid.object_footprints?.find((candidate) => candidate.object_id === entityId);
  if (!footprint) return undefined;
  const tileWidth = viewport.width / grid.width_tiles;
  const tileHeight = viewport.height / grid.height_tiles;
  const topLeft = project({ x: footprint.x * tileWidth, y: footprint.y * tileHeight }, viewport, width, height);
  const bottomRight = project(
    { x: (footprint.x + footprint.width) * tileWidth, y: (footprint.y + footprint.height) * tileHeight },
    viewport,
    width,
    height,
  );
  return {
    x: topLeft.x,
    y: topLeft.y,
    width: bottomRight.x - topLeft.x,
    height: bottomRight.y - topLeft.y,
  };
}

function project(position: { x: number; y: number }, viewport: { width: number; height: number }, width: number, height: number) {
  const scale = Math.min(width / viewport.width, height / viewport.height);
  const offsetX = (width - viewport.width * scale) / 2;
  const offsetY = (height - viewport.height * scale) / 2;
  return { x: offsetX + position.x * scale, y: offsetY + position.y * scale, scale };
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function roundedRectPath(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function pixelRect(ctx: CanvasRenderingContext2D, x: number, y: number, width: number, height: number, color: string): void {
  ctx.fillStyle = color;
  ctx.fillRect(Math.round(x), Math.round(y), Math.round(width), Math.round(height));
}

function drawPattern(
  ctx: CanvasRenderingContext2D,
  originX: number,
  originY: number,
  scale: number,
  pattern: number[][],
  color: string,
) {
  for (const [x, y] of pattern) {
    pixelRect(ctx, originX + x * scale, originY + y * scale, scale, scale, color);
  }
}

function abbreviatedState(state: string) {
  const mapping: Record<string, string> = {
    idle: "IDL",
    working: "WRK",
    blocked: "BLK",
    waiting: "WAI",
    moving: "MOV",
    charging: "CHG",
    maintenance: "MNT",
    error: "ERR",
  };
  return mapping[state] ?? state.slice(0, 3).toUpperCase();
}

function regionPalette(region: RenderRegion) {
  switch (region.kind) {
    case "station":
      return { fill: region.background ?? "rgba(255, 255, 255, 0.98)", stroke: region.accent ?? "rgba(143, 175, 230, 0.96)" };
    case "inspection":
      return { fill: region.background ?? "rgba(255, 255, 255, 0.98)", stroke: region.accent ?? "rgba(160, 184, 227, 0.96)" };
    case "storage":
      return { fill: region.background ?? "rgba(248, 250, 255, 0.98)", stroke: region.accent ?? "rgba(140, 171, 219, 0.98)" };
    case "battery":
      return { fill: region.background ?? "rgba(245, 251, 248, 0.98)", stroke: region.accent ?? "rgba(145, 184, 228, 0.98)" };
    default:
      return { fill: region.background ?? "rgba(255, 255, 255, 0.98)", stroke: region.accent ?? "rgba(155, 177, 220, 0.96)" };
  }
}

function regionTileColors(region: RenderRegion) {
  switch (region.kind) {
    case "storage":
      return ["rgba(231, 240, 254, 0.82)", "rgba(219, 232, 251, 0.82)", "rgba(211, 225, 246, 0.76)"];
    case "battery":
      return ["rgba(229, 247, 239, 0.82)", "rgba(216, 239, 229, 0.82)", "rgba(205, 229, 221, 0.76)"];
    case "inspection":
      return ["rgba(237, 244, 254, 0.82)", "rgba(226, 236, 252, 0.82)", "rgba(216, 228, 247, 0.76)"];
    case "station":
      return ["rgba(239, 246, 255, 0.82)", "rgba(228, 239, 254, 0.82)", "rgba(218, 232, 251, 0.76)"];
    default:
      return ["rgba(240, 246, 255, 0.80)", "rgba(230, 239, 252, 0.80)", "rgba(220, 232, 247, 0.74)"];
  }
}

function drawRegionTileColorField(
  ctx: CanvasRenderingContext2D,
  region: RenderRegion,
  grid: ReplayRenderModel["grid"],
  viewport: { width: number; height: number },
  width: number,
  height: number,
) {
  if (!grid?.width_tiles || !grid?.height_tiles) return;
  const tileWidth = viewport.width / grid.width_tiles;
  const tileHeight = viewport.height / grid.height_tiles;
  const startX = Math.max(0, Math.floor(region.position.x / tileWidth));
  const endX = Math.min(grid.width_tiles, Math.ceil((region.position.x + region.size.width) / tileWidth));
  const startY = Math.max(0, Math.floor(region.position.y / tileHeight));
  const endY = Math.min(grid.height_tiles, Math.ceil((region.position.y + region.size.height) / tileHeight));
  const colors = regionTileColors(region);

  for (let tileY = startY; tileY < endY; tileY += 1) {
    for (let tileX = startX; tileX < endX; tileX += 1) {
      const topLeft = project({ x: tileX * tileWidth, y: tileY * tileHeight }, viewport, width, height);
      const bottomRight = project({ x: (tileX + 1) * tileWidth, y: (tileY + 1) * tileHeight }, viewport, width, height);
      const colorIndex =
        (Math.floor(tileX / 5) + Math.floor(tileY / 5)) % 2 === 0 ? (tileX + tileY) % 2 : 2 - ((tileX + tileY) % 2);
      ctx.fillStyle = colors[colorIndex];
      ctx.fillRect(topLeft.x, topLeft.y, bottomRight.x - topLeft.x, bottomRight.y - topLeft.y);
    }
  }
}

function drawRegion(
  ctx: CanvasRenderingContext2D,
  region: RenderRegion,
  viewport: { width: number; height: number },
  width: number,
  height: number,
  grid?: ReplayRenderModel["grid"],
) {
  const topLeft = project(region.position, viewport, width, height);
  const bottomRight = project(
    { x: region.position.x + region.size.width, y: region.position.y + region.size.height },
    viewport,
    width,
    height,
  );
  const x = topLeft.x;
  const y = topLeft.y;
  const regionWidth = bottomRight.x - topLeft.x;
  const regionHeight = bottomRight.y - topLeft.y;
  const palette = regionPalette(region);

  ctx.save();
  roundedRectPath(ctx, x, y, regionWidth, regionHeight, 22);
  ctx.fillStyle = palette.fill;
  ctx.fill();

  ctx.save();
  roundedRectPath(ctx, x, y, regionWidth, regionHeight, 22);
  ctx.clip();
  drawRegionTileColorField(ctx, region, grid, viewport, width, height);

  ctx.restore();
  ctx.strokeStyle = palette.stroke;
  ctx.lineWidth = 1.5;
  ctx.stroke();

  ctx.fillStyle = "#183353";
  ctx.font = "700 14px Consolas";
  ctx.fillText(region.label, x + 16, y + 22);
  ctx.restore();
}

function drawStorageRegionDecoration(
  ctx: CanvasRenderingContext2D,
  region: RenderRegion,
  viewport: { width: number; height: number },
  width: number,
  height: number,
  completedCount = 0,
) {
  const topLeft = project(region.position, viewport, width, height);
  const bottomRight = project(
    { x: region.position.x + region.size.width, y: region.position.y + region.size.height },
    viewport,
    width,
    height,
  );
  const regionWidth = bottomRight.x - topLeft.x;
  const regionHeight = bottomRight.y - topLeft.y;
  const baseX = topLeft.x + regionWidth * 0.23;
  const baseY = topLeft.y + regionHeight * 0.63;
  const pixel = clamp(Math.round((regionWidth / 420) * 3), 2, 4);

  ctx.save();
  pixelRect(ctx, baseX, baseY + pixel * 5, pixel * 30, pixel * 2, "#40608a");
  const crateOffsets = [0, 9, 18];
  for (const offset of crateOffsets) {
    pixelRect(ctx, baseX + offset * pixel, baseY + pixel * 2, pixel * 6, pixel * 5, "#72a8ff");
    pixelRect(ctx, baseX + offset * pixel + pixel, baseY + pixel * 3, pixel * 4, pixel * 3, "#eef6ff");
    pixelRect(ctx, baseX + offset * pixel, baseY + pixel * 2, pixel * 6, pixel, "#274b74");
  }
  for (let i = 0; i < 7; i += 1) {
    pixelRect(ctx, baseX + pixel * (34 + i * 2), baseY + pixel * (2 + (i % 2)), pixel, pixel, "rgba(91, 154, 247, 0.5)");
  }
  ctx.fillStyle = "#183353";
  ctx.font = "700 11px Consolas";
  ctx.fillText(`COMPLETED ${completedCount}`, baseX + pixel * 2, baseY + pixel * 12);
  ctx.restore();
}

function drawBatteryRegionDecoration(
  ctx: CanvasRenderingContext2D,
  region: RenderRegion,
  viewport: { width: number; height: number },
  width: number,
  height: number,
) {
  const topLeft = project(region.position, viewport, width, height);
  const bottomRight = project(
    { x: region.position.x + region.size.width, y: region.position.y + region.size.height },
    viewport,
    width,
    height,
  );
  const regionWidth = bottomRight.x - topLeft.x;
  const regionHeight = bottomRight.y - topLeft.y;
  const pixel = clamp(Math.round((regionWidth / 420) * 3), 2, 4);
  const rackX = topLeft.x + regionWidth * 0.14;
  const rackY = topLeft.y + regionHeight * 0.54;

  ctx.save();
  pixelRect(ctx, rackX, rackY, pixel * 24, pixel * 2, "#35577f");
  for (let i = 0; i < 5; i += 1) {
    const cellX = rackX + pixel * (2 + i * 4);
    pixelRect(ctx, cellX, rackY - pixel * 6, pixel * 3, pixel * 6, "#4a6f9a");
    pixelRect(ctx, cellX + pixel, rackY - pixel * 5, pixel, pixel * 4, "#52d39c");
    pixelRect(ctx, cellX, rackY - pixel * 7, pixel * 3, pixel, "#183353");
  }
  for (let i = 0; i < 6; i += 1) {
    pixelRect(ctx, rackX + pixel * (28 + i * 2), rackY - pixel * (1 + (i % 2)), pixel, pixel, "rgba(84, 202, 151, 0.45)");
  }
  ctx.restore();
}

function drawMiniHud(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  label: string,
  state: string,
  accent: string,
) {
  roundedRectPath(ctx, x, y, width, 20, 7);
  ctx.fillStyle = "rgba(255,255,255,0.94)";
  ctx.fill();
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1;
  ctx.stroke();

  ctx.fillStyle = "#17314d";
  ctx.font = "700 9px Consolas";
  ctx.fillText(label.toUpperCase(), x + 6, y + 9.5);
  ctx.fillStyle = accent;
  ctx.font = "700 8px Consolas";
  ctx.fillText(state.toUpperCase(), x + 6, y + 17);
}

function drawTinyChip(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  text: string,
  accent: string,
  fill = "rgba(255,255,255,0.96)",
) {
  const width = Math.max(24, text.length * 6 + 12);
  roundedRectPath(ctx, x, y, width, 14, 5);
  ctx.fillStyle = fill;
  ctx.fill();
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.fillStyle = accent;
  ctx.font = "700 8px Consolas";
  ctx.fillText(text.toUpperCase(), x + 6, y + 9.5);
}

function drawSpeechBubble(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  text: string,
  accent: string,
  options?: {
    align?: "left" | "center";
    tailX?: number;
    tailSide?: "bottom" | "right" | "left";
    fill?: string;
    textColor?: string;
  },
) {
  const paddingX = 8;
  const bubbleHeight = 18;
  ctx.save();
  ctx.font = "700 8px Consolas";
  const textWidth = Math.ceil(ctx.measureText(text.toUpperCase()).width);
  const bubbleWidth = Math.max(28, textWidth + paddingX * 2);
  let left = x;
  if (options?.align === "center") left = x - bubbleWidth / 2;
  const top = y;

  roundedRectPath(ctx, left, top, bubbleWidth, bubbleHeight, 7);
  ctx.fillStyle = options?.fill ?? "rgba(255,255,255,0.97)";
  ctx.fill();
  ctx.strokeStyle = accent;
  ctx.lineWidth = 1;
  ctx.stroke();

  if (options?.tailSide === "right") {
    const tailY = top + bubbleHeight / 2;
    ctx.beginPath();
    ctx.moveTo(left + bubbleWidth, tailY - 3);
    ctx.lineTo(left + bubbleWidth + 5, tailY);
    ctx.lineTo(left + bubbleWidth, tailY + 3);
    ctx.closePath();
    ctx.fillStyle = options?.fill ?? "rgba(255,255,255,0.97)";
    ctx.fill();
    ctx.strokeStyle = accent;
    ctx.stroke();
  } else if (options?.tailSide === "left") {
    const tailY = top + bubbleHeight / 2;
    ctx.beginPath();
    ctx.moveTo(left, tailY - 3);
    ctx.lineTo(left - 5, tailY);
    ctx.lineTo(left, tailY + 3);
    ctx.closePath();
    ctx.fillStyle = options?.fill ?? "rgba(255,255,255,0.97)";
    ctx.fill();
    ctx.strokeStyle = accent;
    ctx.stroke();
  } else if (options?.tailX !== undefined) {
    const tailX = clamp(options.tailX, left + 8, left + bubbleWidth - 8);
    const tailY = top + bubbleHeight;
    ctx.beginPath();
    ctx.moveTo(tailX - 4, tailY);
    ctx.lineTo(tailX, tailY + 5);
    ctx.lineTo(tailX + 4, tailY);
    ctx.closePath();
    ctx.fillStyle = options?.fill ?? "rgba(255,255,255,0.97)";
    ctx.fill();
    ctx.strokeStyle = accent;
    ctx.stroke();
  }

  ctx.fillStyle = options?.textColor ?? "#17314d";
  ctx.fillText(text.toUpperCase(), left + paddingX, top + 11.5);
  ctx.restore();

  return { left, top, width: bubbleWidth, height: bubbleHeight };
}

function drawPlainLabel(
  ctx: CanvasRenderingContext2D,
  centerX: number,
  y: number,
  text: string,
  color: string,
) {
  ctx.save();
  ctx.font = "700 8px Consolas";
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = color;
  ctx.strokeStyle = "rgba(255,255,255,0.92)";
  ctx.lineWidth = 3;
  ctx.strokeText(text.toUpperCase(), centerX, y);
  ctx.fillText(text.toUpperCase(), centerX, y);
  ctx.restore();
}

function drawSegments(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  progress: number,
  activeColor: string,
  emptyColor = "rgba(40,68,112,0.14)",
) {
  const segments = 6;
  const gap = 2;
  const segmentWidth = (width - gap * (segments - 1)) / segments;
  const active = Math.round(clamp(progress, 0, 1) * segments);
  for (let index = 0; index < segments; index += 1) {
    pixelRect(ctx, x + index * (segmentWidth + gap), y, segmentWidth, 4, index < active ? activeColor : emptyColor);
  }
}

function progressFromWindow(windowValue: unknown, currentTime: number): number | undefined {
  if (!windowValue || typeof windowValue !== "object") return undefined;
  const windowRecord = windowValue as Record<string, unknown>;
  const startedAt = typeof windowRecord.started_at === "number" ? windowRecord.started_at : undefined;
  const endedAt = typeof windowRecord.ended_at === "number" ? windowRecord.ended_at : undefined;
  if (startedAt === undefined || endedAt === undefined) return undefined;
  if (endedAt <= startedAt) return 1;
  return clamp((currentTime - startedAt) / (endedAt - startedAt), 0, 1);
}

function drawGaugeBar(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  progress: number,
  accent: string,
  options?: {
    vertical?: boolean;
    track?: string;
    fill?: string;
    border?: string;
  },
) {
  const value = clamp(progress, 0, 1);
  const border = options?.border ?? "rgba(39,75,116,0.4)";
  const track = options?.track ?? "rgba(35, 64, 108, 0.14)";
  const fill = options?.fill ?? accent;
  const w = Math.max(4, Math.round(width));
  const h = Math.max(4, Math.round(height));
  const innerX = Math.round(x) + 1;
  const innerY = Math.round(y) + 1;
  const innerW = Math.max(1, w - 2);
  const innerH = Math.max(1, h - 2);

  pixelRect(ctx, x, y, w, h, border);
  pixelRect(ctx, innerX, innerY, innerW, innerH, track);

  if (options?.vertical) {
    const fillH = Math.max(1, Math.round(innerH * value));
    pixelRect(ctx, innerX, innerY + innerH - fillH, innerW, fillH, fill);
  } else {
    const fillW = Math.max(1, Math.round(innerW * value));
    pixelRect(ctx, innerX, innerY, fillW, innerH, fill);
  }
}

function workerBatteryProgress(entity: { attributes: Record<string, unknown> }, currentTime: number): number | undefined {
  const batteryPeriodMin = Number(entity.attributes.battery_period_min);
  const lastSwapAt = Number(entity.attributes.last_swap_at);
  const batteryPct = Number(entity.attributes.battery_pct);
  if (Number.isFinite(batteryPeriodMin) && batteryPeriodMin > 0 && Number.isFinite(lastSwapAt)) {
    if (Number.isFinite(batteryPct) && batteryPct <= 0) return 0;
    return clamp(1 - (currentTime - lastSwapAt) / batteryPeriodMin, 0, 1);
  }
  if (!Number.isFinite(batteryPct)) return undefined;
  return clamp(batteryPct / 100, 0, 1);
}

function workerTaskProgress(entity: { state: string; attributes: Record<string, unknown> }, currentTime: number): number | undefined {
  const taskWindowProgress = progressFromWindow(entity.attributes.task_window, currentTime);
  if (taskWindowProgress !== undefined) return taskWindowProgress;
  const motionProgress = progressFromWindow(entity.attributes.motion, currentTime);
  if (entity.state === "moving" && motionProgress !== undefined) return motionProgress;
  return undefined;
}

function machineProcessProgress(entity: { attributes: Record<string, unknown> }, currentTime: number): number | undefined {
  const machineState = typeof entity.attributes.machine_state === "string" ? entity.attributes.machine_state.toUpperCase() : "";

  if (machineState.includes("REPAIR")) {
    const repairWindowProgress = progressFromWindow(entity.attributes.repair_window, currentTime);
    if (repairWindowProgress !== undefined) return repairWindowProgress;
    const repairRemaining = Number(entity.attributes.repair_remaining_min);
    const repairTotal = Number(entity.attributes.repair_total_min);
    if (Number.isFinite(repairRemaining) && Number.isFinite(repairTotal) && repairTotal > 0) {
      return clamp(1 - repairRemaining / repairTotal, 0, 1);
    }
    return undefined;
  }

  if (machineState.includes("PROCESS")) {
    const processWindowProgress = progressFromWindow(entity.attributes.process_window, currentTime);
    if (processWindowProgress !== undefined) return processWindowProgress;
    const utilization = Number(entity.attributes.utilization);
    if (Number.isFinite(utilization)) return clamp(utilization, 0, 1);
    return undefined;
  }

  if (!machineState && entity.attributes.process_window) {
    return progressFromWindow(entity.attributes.process_window, currentTime);
  }

  return undefined;
}

function machineStatusBadge(entity: { state: string; attributes: Record<string, unknown> }) {
  const attrs = entity.attributes ?? {};
  const machineState = typeof attrs.machine_state === "string" ? attrs.machine_state.toUpperCase() : "";
  if (machineState.includes("REPAIR")) {
    return { text: "FIX", accent: "#ff9d62" };
  }
  if (machineState.includes("PM")) {
    return { text: "PM", accent: "#9d95ff" };
  }
  if (entity.state === "error" || machineState.includes("BROKEN")) {
    return { text: "DOWN", accent: "#ff7189" };
  }
  if (machineState.includes("SETUP")) {
    return { text: "SET", accent: "#5aa9ff" };
  }
  if (machineState.includes("PROCESS")) {
    return { text: "PROC", accent: "#4fcf8b" };
  }
  if (!machineState && entity.state === "working") {
    return { text: "PROC", accent: "#4fcf8b" };
  }
  return { text: "WAIT", accent: "#f0b45b" };
}

function machineRepairTeamSize(entity: { attributes: Record<string, unknown> }): number {
  const teamSize = Number(entity.attributes.repair_team_size);
  if (Number.isFinite(teamSize) && teamSize > 0) return Math.max(0, Math.round(teamSize));
  const repairTeam = entity.attributes.repair_team;
  if (Array.isArray(repairTeam)) return repairTeam.length;
  return 0;
}

function machineItemFrame(
  kind: unknown,
  sceneIcons: SceneIconSet | null,
): SceneIconFrame | null {
  if (!sceneIcons || typeof kind !== "string") return null;
  const normalized = kind.trim().toLowerCase();
  if (!normalized) return null;
  if (normalized.includes("material")) return sceneIcons.material;
  if (normalized.includes("intermediate") || normalized.includes("transfer")) return sceneIcons.intermediate;
  if (normalized.includes("product")) return sceneIcons.product;
  if (normalized.includes("battery")) return sceneIcons.battery;
  return null;
}

function machineWaitOverlay(
  entity: { attributes: Record<string, unknown> },
  sceneIcons: SceneIconSet | null,
): { frame: SceneIconFrame; placement: "top" | "center" } | null {
  const waitVisual = typeof entity.attributes.wait_visual === "string" ? entity.attributes.wait_visual : "";
  const frame = machineItemFrame(entity.attributes.wait_item_kind, sceneIcons);
  if (!frame) return null;
  if (waitVisual === "completed_output") {
    return { frame, placement: "center" };
  }
  if (waitVisual === "prep_wait") {
    return { frame, placement: "top" };
  }
  return null;
}

function queueKind(label: string, entityId: string) {
  const merged = `${label} ${entityId}`.toLowerCase();
  if (merged.includes("output")) return "output";
  if (merged.includes("inspect")) return "inspection";
  if (merged.includes("transfer") || merged.includes("intermediate")) return "transfer";
  if (merged.includes("material")) return "material";
  if (merged.includes("complete") || merged.includes("buffer")) return "completed";
  return "generic";
}

function queueDisplayLabel(label: string, entityId: string) {
  const merged = `${label} ${entityId}`.toLowerCase();
  if (merged.includes("material_queue")) return "Material Queue";
  if (merged.includes("transfer") || merged.includes("intermediate_queue_2")) return "Intermediate Queue";
  if (merged.includes("inspection queue") || merged.includes("intermediate_queue_4")) return "Inspection Queue";
  return labelText(label);
}

function queueItemFrame(kind: string, entityId: string, sceneIcons: SceneIconSet | null): SceneIconFrame | null {
  if (!sceneIcons) return null;
  if (kind === "material") return sceneIcons.material;
  if (kind === "transfer") return sceneIcons.intermediate;
  if (kind === "inspection" || kind === "completed") return sceneIcons.product;
  if (kind === "output") {
    if (entityId === "station_1_output_queue") return sceneIcons.intermediate;
    return sceneIcons.product;
  }
  return null;
}

function drawWorkerSpriteFallback(ctx: CanvasRenderingContext2D, x: number, y: number, scale: number, accent: string) {
  const outline = [
    [3, 0], [4, 0], [2, 1], [3, 1], [4, 1], [5, 1],
    [2, 2], [5, 2],
    [1, 3], [2, 3], [5, 3], [6, 3],
    [1, 4], [2, 4], [5, 4], [6, 4],
    [2, 5], [3, 5], [4, 5], [5, 5],
    [2, 6], [3, 6], [4, 6], [5, 6],
    [1, 7], [2, 7], [5, 7], [6, 7],
    [1, 8], [2, 8], [5, 8], [6, 8],
    [1, 9], [2, 9], [5, 9], [6, 9],
    [1, 10], [2, 10], [5, 10], [6, 10],
    [2, 11], [5, 11],
  ];
  const fill = [
    [3, 1], [4, 1], [2, 3], [5, 3], [2, 4], [5, 4],
    [3, 5], [4, 5], [3, 6], [4, 6], [2, 7], [5, 7], [2, 8], [5, 8], [2, 9], [5, 9],
  ];
  const visor = [[3, 2], [4, 2], [3, 3], [4, 3]];
  const backpack = [[0, 5], [1, 5], [0, 6], [1, 6], [0, 7], [1, 7]];
  drawPattern(ctx, x, y, scale, outline, "#17324f");
  drawPattern(ctx, x, y, scale, fill, "#6ca8ff");
  drawPattern(ctx, x, y, scale, visor, accent);
  drawPattern(ctx, x, y, scale, backpack, "#ffcf67");
}

function drawWorkerSpriteFrame(
  ctx: CanvasRenderingContext2D,
  frame: { canvas: HTMLCanvasElement; width: number; height: number },
  x: number,
  y: number,
  targetHeight: number,
) {
  const aspect = frame.width / Math.max(1, frame.height);
  const targetWidth = targetHeight * aspect;
  ctx.drawImage(frame.canvas, x, y, targetWidth, targetHeight);
}

function drawMachineSpriteFrame(
  ctx: CanvasRenderingContext2D,
  frame: { canvas: HTMLCanvasElement; width: number; height: number },
  x: number,
  y: number,
  targetWidth: number,
) {
  const aspect = frame.height / Math.max(1, frame.width);
  const targetHeight = targetWidth * aspect;
  ctx.drawImage(frame.canvas, x, y, targetWidth, targetHeight);
}

interface FrameCropBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

const frameCropBoundsCache = new WeakMap<HTMLCanvasElement, FrameCropBounds>();

function isVisibleSpritePixel(data: Uint8ClampedArray, index: number): boolean {
  const alpha = data[index + 3];
  if (alpha <= 12) return false;
  const red = data[index];
  const green = data[index + 1];
  const blue = data[index + 2];
  const nearWhite = red >= 244 && green >= 244 && blue >= 244;
  const lowChroma = Math.abs(red - green) <= 8 && Math.abs(green - blue) <= 8;
  return !(nearWhite && lowChroma);
}

function frameContentBounds(frame: { canvas: HTMLCanvasElement; width: number; height: number }): FrameCropBounds {
  const cached = frameCropBoundsCache.get(frame.canvas);
  if (cached) return cached;

  let bounds: FrameCropBounds = { x: 0, y: 0, width: frame.width, height: frame.height };
  const ctx = frame.canvas.getContext("2d");
  if (!ctx) return bounds;

  try {
    const imageData = ctx.getImageData(0, 0, frame.width, frame.height);
    let minX = frame.width;
    let minY = frame.height;
    let maxX = -1;
    let maxY = -1;
    for (let y = 0; y < frame.height; y += 1) {
      for (let x = 0; x < frame.width; x += 1) {
        const index = (y * frame.width + x) * 4;
        if (!isVisibleSpritePixel(imageData.data, index)) continue;
        minX = Math.min(minX, x);
        minY = Math.min(minY, y);
        maxX = Math.max(maxX, x);
        maxY = Math.max(maxY, y);
      }
    }
    if (maxX >= minX && maxY >= minY) {
      bounds = { x: minX, y: minY, width: maxX - minX + 1, height: maxY - minY + 1 };
    }
  } catch {
    bounds = { x: 0, y: 0, width: frame.width, height: frame.height };
  }

  frameCropBoundsCache.set(frame.canvas, bounds);
  return bounds;
}

function drawFrameCroppedToRect(
  ctx: CanvasRenderingContext2D,
  frame: { canvas: HTMLCanvasElement; width: number; height: number },
  x: number,
  y: number,
  targetWidth: number,
  targetHeight: number,
) {
  const crop = frameContentBounds(frame);
  ctx.drawImage(frame.canvas, crop.x, crop.y, crop.width, crop.height, x, y, targetWidth, targetHeight);
}

function drawMachineSpriteFrameRect(
  ctx: CanvasRenderingContext2D,
  frame: { canvas: HTMLCanvasElement; width: number; height: number },
  x: number,
  y: number,
  targetWidth: number,
  targetHeight: number,
) {
  drawFrameCroppedToRect(ctx, frame, x, y, targetWidth, targetHeight);
}

function drawSceneIconFrame(
  ctx: CanvasRenderingContext2D,
  frame: SceneIconFrame,
  x: number,
  y: number,
  targetHeight: number,
) {
  const aspect = frame.width / Math.max(1, frame.height);
  const targetWidth = targetHeight * aspect;
  ctx.drawImage(frame.canvas, x, y, targetWidth, targetHeight);
}

function drawSceneIconFrameRect(
  ctx: CanvasRenderingContext2D,
  frame: SceneIconFrame,
  x: number,
  y: number,
  targetWidth: number,
  targetHeight: number,
) {
  drawFrameCroppedToRect(ctx, frame, x, y, targetWidth, targetHeight);
}

function drawMachineSprite(ctx: CanvasRenderingContext2D, x: number, y: number, scale: number, accent: string) {
  const shell = [
    [0, 0], [1, 0], [2, 0], [3, 0], [4, 0], [5, 0], [6, 0], [7, 0],
    [0, 1], [7, 1], [0, 2], [7, 2], [0, 3], [7, 3], [0, 4], [7, 4], [0, 5], [7, 5],
    [0, 6], [1, 6], [2, 6], [3, 6], [4, 6], [5, 6], [6, 6], [7, 6],
  ];
  const chamber = [[2, 1], [3, 1], [4, 1], [5, 1], [2, 2], [5, 2], [2, 3], [5, 3], [2, 4], [3, 4], [4, 4], [5, 4]];
  const lights = [[1, 1], [1, 4], [6, 1], [6, 4]];
  const ports = [[1, 7], [2, 7], [5, 7], [6, 7], [1, 8], [2, 8], [5, 8], [6, 8]];
  drawPattern(ctx, x, y, scale, shell, "#203d60");
  drawPattern(ctx, x, y, scale, chamber, "#94d7ff");
  drawPattern(ctx, x, y, scale, lights, accent);
  drawPattern(ctx, x, y, scale, ports, "#7fd0a7");
}

function drawQueueSprite(ctx: CanvasRenderingContext2D, x: number, y: number, scale: number) {
  const crate = [
    [0, 0], [1, 0], [2, 0],
    [0, 1], [2, 1],
    [0, 2], [1, 2], [2, 2],
  ];
  drawPattern(ctx, x, y, scale, crate, "#6675c9");
  drawPattern(ctx, x + scale * 4, y + scale, scale, crate, "#5ad0bb");
  drawPattern(ctx, x + scale * 8, y, scale, crate, "#6675c9");
}

function drawQueueSpriteByKind(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  scale: number,
  kind: string,
  sceneIcons: SceneIconSet | null,
) {
  if (sceneIcons) {
    const baseHeight = clamp(scale * 9 * 1.7, 28, 48);
    drawSceneIconFrame(ctx, sceneIcons.queue, x, y + 2, baseHeight);
    return { width: baseHeight, height: baseHeight };
  }
  if (kind === "output") {
    const box = [[0, 0], [1, 0], [0, 1], [1, 1]];
    drawPattern(ctx, x, y, scale, box, "#f0b45b");
    drawPattern(ctx, x + scale * 4, y, scale, box, "#f0b45b");
    drawPattern(ctx, x + scale * 8, y, scale, box, "#f0b45b");
    return { width: scale * 11, height: scale * 3 };
  }
  if (kind === "inspection") {
    const diamond = [[1, 0], [0, 1], [2, 1], [1, 2]];
    drawPattern(ctx, x, y, scale, diamond, "#f3a53c");
    drawPattern(ctx, x + scale * 4, y, scale, diamond, "#f3a53c");
    drawPattern(ctx, x + scale * 8, y, scale, diamond, "#f3a53c");
    return { width: scale * 11, height: scale * 3 };
  }
  if (kind === "material") {
    const box = [[0, 0], [1, 0], [0, 1], [1, 1]];
    drawPattern(ctx, x, y, scale, box, "#62a9ff");
    drawPattern(ctx, x + scale * 4, y + scale, scale, box, "#62a9ff");
    drawPattern(ctx, x + scale * 8, y, scale, box, "#62a9ff");
    return { width: scale * 11, height: scale * 3 };
  }
  if (kind === "transfer") {
    const box = [[0, 0], [1, 0], [0, 1], [1, 1]];
    drawPattern(ctx, x, y, scale, box, "#7f75e6");
    drawPattern(ctx, x + scale * 4, y, scale, box, "#39c4ab");
    drawPattern(ctx, x + scale * 8, y, scale, box, "#7f75e6");
    return { width: scale * 11, height: scale * 3 };
  }
  if (kind === "completed") {
    const box = [[0, 0], [1, 0], [0, 1], [1, 1]];
    drawPattern(ctx, x, y, scale, box, "#2db36a");
    drawPattern(ctx, x + scale * 4, y, scale, box, "#2db36a");
    drawPattern(ctx, x + scale * 8, y, scale, box, "#2db36a");
    return { width: scale * 11, height: scale * 3 };
  }
  drawQueueSprite(ctx, x, y, scale);
  return { width: scale * 11, height: scale * 3 };
}

function drawQueueItemStack(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  maxWidth: number,
  count: number,
  kind: string,
  entityId: string,
  sceneIcons: SceneIconSet | null,
) {
  const visibleCount = Math.min(5, Math.max(0, count));
  const iconFrame = queueItemFrame(kind, entityId, sceneIcons);
  if (!iconFrame || visibleCount <= 0) return;
  const iconSize = 13.5;
  const aspect = iconFrame.width / Math.max(1, iconFrame.height);
  const iconWidth = iconSize * aspect;
  const usableWidth = Math.max(iconWidth, maxWidth);
  const stride = visibleCount > 1 ? Math.min(iconWidth + 0.5, (usableWidth - iconWidth) / (visibleCount - 1)) : 0;
  const stackWidth = iconWidth + stride * Math.max(0, visibleCount - 1);
  const startX = x + Math.max(0, (usableWidth - stackWidth) / 2);
  for (let index = 0; index < visibleCount; index += 1) {
    drawSceneIconFrame(ctx, iconFrame, startX + index * stride, y, iconSize);
  }
}

function drawQueueFootprintSprite(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  targetWidth: number,
  targetHeight: number,
  kind: string,
  sceneIcons: SceneIconSet | null,
) {
  ctx.save();
  if (sceneIcons) {
    drawSceneIconFrameRect(ctx, sceneIcons.queue, x, y, targetWidth, targetHeight);
  } else {
    const scale = Math.max(1, Math.floor(Math.min(targetWidth / 12, targetHeight / 4)));
    drawQueueSprite(ctx, x + (targetWidth - scale * 11) / 2, y + (targetHeight - scale * 3) / 2, scale);
  }
  ctx.restore();
  return { width: targetWidth, height: targetHeight };
}

function drawStorageSprite(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  scale: number,
  sceneIcons: SceneIconSet | null,
) {
  if (sceneIcons) {
    const baseHeight = clamp(scale * 10, 18, 30);
    drawSceneIconFrame(ctx, sceneIcons.product, x, y + 2, baseHeight);
    drawSceneIconFrame(ctx, sceneIcons.product, x + baseHeight * 0.7, y + 4, clamp(baseHeight * 0.92, 16, 28));
    return;
  }
  const shelf = [
    [0, 0], [1, 0], [2, 0], [3, 0], [4, 0],
    [0, 1], [4, 1], [0, 2], [4, 2], [0, 3], [4, 3],
    [0, 4], [1, 4], [2, 4], [3, 4], [4, 4],
  ];
  const boxes = [[1, 1], [2, 1], [3, 1], [1, 3], [3, 3]];
  drawPattern(ctx, x, y, scale, shelf, "#315684");
  drawPattern(ctx, x, y, scale, boxes, "#2db36a");
}

function drawChargerSprite(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  scale: number,
  accent: string,
  sceneIcons: SceneIconSet | null,
) {
  if (sceneIcons) {
    const baseHeight = clamp(scale * 10, 18, 30);
    drawSceneIconFrame(ctx, sceneIcons.battery, x, y + 2, baseHeight);
    return;
  }
  const stand = [[1, 0], [2, 0], [1, 1], [2, 1], [1, 2], [2, 2], [1, 3], [2, 3], [0, 4], [1, 4], [2, 4], [3, 4]];
  const bolt = [[4, 0], [3, 1], [4, 1], [3, 2], [4, 2], [3, 3]];
  drawPattern(ctx, x, y, scale, stand, "#284a72");
  drawPattern(ctx, x, y, scale, bolt, accent);
}

function labelText(label: string) {
  return label.replace(/^Worker\s+/i, "").replace(/^Machine\s+/i, "");
}

function drawPlainLeftLabel(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  text: string,
  color: string,
) {
  ctx.save();
  ctx.font = "700 8px Consolas";
  ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic";
  ctx.fillStyle = color;
  ctx.strokeStyle = "rgba(255,255,255,0.92)";
  ctx.lineWidth = 3;
  ctx.strokeText(text.toUpperCase(), x, y);
  ctx.fillText(text.toUpperCase(), x, y);
  ctx.restore();
}

function carryingIconFrame(entity: { attributes: Record<string, unknown> }, sceneIcons: SceneIconSet | null): SceneIconFrame | null {
  if (!sceneIcons) return null;
  const cargo = entity.attributes.cargo;
  const carryingItemType =
    cargo && typeof cargo === "object" && typeof (cargo as Record<string, unknown>).item_type === "string"
      ? String((cargo as Record<string, unknown>).item_type).trim().toLowerCase()
      : typeof entity.attributes.carrying_item_type === "string"
        ? entity.attributes.carrying_item_type.trim().toLowerCase()
        : "";
  if (!carryingItemType) return null;
  if (carryingItemType.includes("battery")) return sceneIcons.battery;
  if (carryingItemType.includes("product")) return sceneIcons.product;
  if (carryingItemType.includes("intermediate") || carryingItemType.includes("transfer")) return sceneIcons.intermediate;
  if (carryingItemType.includes("material")) return sceneIcons.material;
  return null;
}

function drawInspectionFacility(
  ctx: CanvasRenderingContext2D,
  region: RenderRegion,
  viewport: { width: number; height: number },
  width: number,
  height: number,
  sceneIcons: SceneIconSet | null,
) {
  if (!sceneIcons || region.kind !== "inspection") return;
  const topLeft = project(region.position, viewport, width, height);
  const bottomRight = project(
    { x: region.position.x + region.size.width, y: region.position.y + region.size.height },
    viewport,
    width,
    height,
  );
  const regionWidth = bottomRight.x - topLeft.x;
  const regionHeight = bottomRight.y - topLeft.y;
  const spriteHeight = clamp(regionHeight * 0.36 * 0.7, 32, 58);
  const spriteAspect = sceneIcons.inspectFacility.width / Math.max(1, sceneIcons.inspectFacility.height);
  const spriteWidth = spriteHeight * spriteAspect;
  const spriteX = topLeft.x + (regionWidth - spriteWidth) / 2;
  const spriteY = topLeft.y + (regionHeight - spriteHeight) / 2 + 12;
  drawSceneIconFrame(ctx, sceneIcons.inspectFacility, spriteX, spriteY, spriteHeight);
}

export function SceneCanvas({ width, height, viewport, renderModel, currentEvent, currentTime, onSelectEntity }: SceneCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const boundsRef = useRef<NodeBounds[]>([]);
  const [workerSpriteSheet, setWorkerSpriteSheet] = useState<WorkerSpriteSheet | null>(null);
  const [machineSpriteSet, setMachineSpriteSet] = useState<MachineSpriteSet | null>(null);
  const [sceneIconSet, setSceneIconSet] = useState<SceneIconSet | null>(null);

  const highlightedEntityIds = useMemo(() => {
    const ids = new Set<string>();
    if (currentEvent?.entity_refs.primary) ids.add(currentEvent.entity_refs.primary);
    if (currentEvent?.entity_refs.source) ids.add(currentEvent.entity_refs.source);
    if (currentEvent?.entity_refs.target) ids.add(currentEvent.entity_refs.target);
    for (const relatedId of currentEvent?.entity_refs.related ?? []) ids.add(relatedId);
    return ids;
  }, [currentEvent]);

  useEffect(() => {
    let disposed = false;
    void loadWorkerSpriteSheet()
      .then((sheet) => {
        if (!disposed) setWorkerSpriteSheet(sheet);
      })
      .catch(() => {
        if (!disposed) setWorkerSpriteSheet(null);
      });
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    void loadMachineSpriteSet()
      .then((set) => {
        if (!disposed) setMachineSpriteSet(set);
      })
      .catch(() => {
        if (!disposed) setMachineSpriteSet(null);
      });
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    let disposed = false;
    void loadSceneIconSet()
      .then((set) => {
        if (!disposed) setSceneIconSet(set);
      })
      .catch(() => {
        if (!disposed) setSceneIconSet(null);
      });
    return () => {
      disposed = true;
    };
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.imageSmoothingEnabled = false;
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    drawPixelGrid(ctx, width, height);
    drawTileGridFloor(ctx, renderModel, viewport, width, height);
    boundsRef.current = [];
    const warehouseBufferNode = renderModel.nodes.find((node) => node.entity.entity_id === "warehouse_buffer");
    const warehouseCompletedCount = Number(
      warehouseBufferNode?.entity.attributes.completed_count ?? warehouseBufferNode?.entity.attributes.queue_size ?? 0,
    );

    for (const region of renderModel.regions) {
      drawRegion(ctx, region, viewport, width, height, renderModel.grid);
    }
    for (const region of renderModel.regions) {
      if (region.kind === "storage") drawStorageRegionDecoration(ctx, region, viewport, width, height, warehouseCompletedCount);
      if (region.kind === "battery") drawBatteryRegionDecoration(ctx, region, viewport, width, height);
    }
    for (const region of renderModel.regions) {
      drawInspectionFacility(ctx, region, viewport, width, height, sceneIconSet);
    }
    drawTileGridOverlays(ctx, renderModel, viewport, width, height);

    for (const node of renderModel.nodes) {
      if (node.entity.entity_id === "battery_rack" || node.entity.entity_id === "warehouse_buffer") {
        continue;
      }
      const style = getEntityNodeStyle(node.entity);
      const transformed = project(node.position, viewport, width, height);
      const size = getNodeSize(node.entity);
      const footprintRect = entityFootprintRect(renderModel.grid, node.entity.entity_id, viewport, width, height);
      const useFootprintRect =
        !!footprintRect &&
        (node.entity.entity_type === "machine" ||
          node.entity.entity_type === "workstation" ||
          node.entity.entity_type === "queue" ||
          node.entity.entity_type === "buffer");
      const spriteScale = Math.max(2, Math.round(transformed.scale * 2.4));
      const nodeWidth = useFootprintRect ? footprintRect.width : size.width * transformed.scale * 0.9;
      const nodeHeight = useFootprintRect ? footprintRect.height : size.height * transformed.scale * 0.9;
      const x = useFootprintRect ? footprintRect.x : transformed.x - nodeWidth / 2;
      const y = useFootprintRect ? footprintRect.y : transformed.y - nodeHeight / 2;

      boundsRef.current.push({ entityId: node.entity.entity_id, x, y, width: nodeWidth, height: nodeHeight });

      ctx.save();
      ctx.globalAlpha = node.focused ? 1 : 0.34;
      if (node.selected || highlightedEntityIds.has(node.entity.entity_id)) {
        ctx.shadowBlur = 14;
        ctx.shadowColor = style.glow;
      }

      if (node.entity.entity_type === "worker" || node.entity.entity_type === "robot" || node.entity.entity_type === "transporter") {
        const workerVisualState = getWorkerVisualState(node.entity);
        const workerFrame = getWorkerSpriteFrame(workerSpriteSheet, node.entity, currentTime);
        const batteryProgress = workerBatteryProgress(node.entity, currentTime);
        const taskProgress = workerTaskProgress(node.entity, currentTime);
        const spriteHeight = clamp(nodeHeight - 28, 42, 72);
        const spriteWidth = workerFrame ? spriteHeight * (workerFrame.width / Math.max(1, workerFrame.height)) : 12 * spriteScale;
        const spriteX = transformed.x - spriteWidth / 2;
        const spriteY = transformed.y - spriteHeight / 2;
        const headCenterX = transformed.x;
        if (workerFrame) {
          drawWorkerSpriteFrame(ctx, workerFrame, spriteX, spriteY, spriteHeight);
        } else {
          drawWorkerSpriteFallback(ctx, x + 18, y + 26, spriteScale, style.accent);
        }
        const carriedIcon = workerVisualState.showCarryOverlay ? carryingIconFrame(node.entity, sceneIconSet) : null;
        if (carriedIcon) {
          const carriedSize = clamp(spriteHeight * 0.44, 20, 32);
          drawSceneIconFrame(
            ctx,
            carriedIcon,
            spriteX + spriteWidth * 0.18,
            spriteY + spriteHeight * 0.34,
            carriedSize,
          );
        }
        if (batteryProgress !== undefined) {
          drawGaugeBar(
            ctx,
            spriteX + 1,
            spriteY + Math.max(8, spriteHeight * 0.24),
            6,
            Math.max(18, spriteHeight * 0.38),
            batteryProgress,
            batteryProgress <= 0.2 ? "#ff7189" : "#39c4ab",
            { vertical: true },
          );
        }
        drawPlainLabel(ctx, headCenterX, Math.max(10, spriteY - 6), labelText(node.entity.label), "#274b74");
        const workerBubble = drawSpeechBubble(
          ctx,
          spriteX - 42,
          spriteY + 4,
          workerVisualState.badgeText,
          workerVisualState.badgeAccent,
          {
            tailSide: "right",
            fill: "rgba(255,255,255,0.94)",
          },
        );
        if (taskProgress !== undefined && node.entity.state !== "idle") {
          drawGaugeBar(
            ctx,
            workerBubble.left + 4,
            workerBubble.top + workerBubble.height + 4,
            Math.max(18, workerBubble.width - 8),
            6,
            taskProgress,
            workerVisualState.badgeAccent,
          );
        }
      } else if (node.entity.entity_type === "machine" || node.entity.entity_type === "workstation") {
        const machineBadge = machineStatusBadge(node.entity);
        const machineFrame = getMachineSpriteFrame(machineSpriteSet, node.entity);
        const machineWait = machineWaitOverlay(node.entity, sceneIconSet);
        const machineProgress = machineProcessProgress(node.entity, currentTime);
        const repairTeamSize = machineRepairTeamSize(node.entity);
        let machineX = useFootprintRect ? x : x + 10;
        let machineY = useFootprintRect ? y : y + 26;
        let machineWidth = useFootprintRect ? nodeWidth : Math.max(54, nodeWidth - 24) * 1.5;
        let machineHeight = useFootprintRect ? nodeHeight : machineWidth * 0.68;
        if (machineFrame) {
          if (useFootprintRect) {
            drawMachineSpriteFrameRect(ctx, machineFrame, machineX, machineY, machineWidth, machineHeight);
          } else {
            const baseWidth = Math.max(54, nodeWidth - 24);
            machineWidth = baseWidth * 1.5;
            machineX = x + (nodeWidth - machineWidth) / 2;
            machineHeight = machineWidth * (machineFrame.height / Math.max(1, machineFrame.width));
            drawMachineSpriteFrame(ctx, machineFrame, machineX, machineY, machineWidth);
          }
        } else {
          const fallbackScale = useFootprintRect
            ? Math.max(1, Math.floor(Math.min(machineWidth / 8, machineHeight / 9)))
            : spriteScale;
          const fallbackWidth = fallbackScale * 8;
          const fallbackHeight = fallbackScale * 9;
          drawMachineSprite(
            ctx,
            useFootprintRect ? machineX + (machineWidth - fallbackWidth) / 2 : x + 18,
            useFootprintRect ? machineY + (machineHeight - fallbackHeight) / 2 : y + 28,
            fallbackScale,
            style.accent,
          );
        }
        if (machineWait) {
          if (machineWait.placement === "center") {
            const iconHeight = clamp(machineWidth * 0.2, 16, 26);
            const iconWidth = iconHeight * (machineWait.frame.width / Math.max(1, machineWait.frame.height));
            drawSceneIconFrame(
              ctx,
              machineWait.frame,
              machineX + machineWidth * 0.56 - iconWidth / 2,
              machineY + machineHeight * 0.36,
              iconHeight,
            );
          } else {
            const iconHeight = clamp(machineWidth * 0.16, 14, 20);
            const iconWidth = iconHeight * (machineWait.frame.width / Math.max(1, machineWait.frame.height));
            drawSceneIconFrame(
              ctx,
              machineWait.frame,
              machineX + machineWidth * 0.5 - iconWidth / 2,
              machineY + machineHeight * 0.04,
              iconHeight,
            );
          }
        }
        drawPlainLabel(
          ctx,
          machineX + machineWidth / 2,
          useFootprintRect ? Math.max(10, y - 5) : y + 20,
          labelText(node.entity.label),
          "#274b74",
        );
        if (repairTeamSize > 0) {
          drawTinyChip(
            ctx,
            machineX + machineWidth - 8,
            machineY + 4,
            `x${repairTeamSize}`,
            machineBadge.accent,
            "rgba(255,255,255,0.98)",
          );
        }
        const machineBubble = drawSpeechBubble(ctx, machineX + machineWidth - 21, machineY + 2, machineBadge.text, machineBadge.accent, {
          tailSide: "left",
          fill: "rgba(255,255,255,0.94)",
        });
        if (machineProgress !== undefined) {
          drawGaugeBar(
            ctx,
            machineBubble.left + 4,
            machineBubble.top + machineBubble.height + 4,
            Math.max(22, machineBubble.width - 8),
            6,
            machineProgress,
            machineBadge.accent,
          );
        }
      } else if (node.entity.entity_type === "queue" || node.entity.entity_type === "buffer") {
        const kind = queueKind(node.entity.label, node.entity.entity_id);
        const queueLabel = queueDisplayLabel(node.entity.label, node.entity.entity_id);
        const queueX = useFootprintRect ? x : x + 8;
        const queueY = useFootprintRect ? y : y + 22;
        const rawCount = Number(node.entity.attributes.queue_size ?? node.entity.attributes.completed_count ?? 0);
        const count = Number.isFinite(rawCount) ? Math.max(0, Math.round(rawCount)) : 0;
        drawPlainLeftLabel(
          ctx,
          x + 2,
          useFootprintRect ? Math.max(10, y - 5) : y + 18,
          `${queueLabel} (${count})`,
          "#274b74",
        );
        const queueSprite = useFootprintRect
          ? drawQueueFootprintSprite(ctx, queueX, queueY, nodeWidth, nodeHeight, kind, sceneIconSet)
          : drawQueueSpriteByKind(ctx, queueX, queueY, spriteScale, kind, sceneIconSet);
        drawQueueItemStack(
          ctx,
          queueX + Math.max(2, queueSprite.width * 0.08),
          queueY + Math.max(5, queueSprite.height * 0.34),
          Math.max(28, queueSprite.width * 0.84),
          count,
          kind,
          node.entity.entity_id,
          sceneIconSet,
        );
      } else if (node.entity.entity_type === "storage") {
        drawMiniHud(ctx, x + 2, y, 70, labelText(node.entity.label), "BUF", style.accent);
        drawStorageSprite(ctx, x + 14, y + 30, spriteScale, sceneIconSet);
        const count = Number(node.entity.attributes.completed_count ?? node.entity.attributes.jobs_remaining ?? 0);
        ctx.fillStyle = "#17314d";
        ctx.font = "700 9px Consolas";
        ctx.fillText(String(count), x + 52, y + 52);
      } else if (node.entity.entity_type === "charger" || node.entity.entity_type === "maintenance_station") {
        if (node.entity.entity_id !== "battery_rack") {
          drawMiniHud(ctx, x + 2, y, 64, labelText(node.entity.label), "PWR", style.accent);
        }
        drawChargerSprite(ctx, x + 18, y + 30, spriteScale, style.accent, sceneIconSet);
      } else {
        drawMiniHud(ctx, x + 2, y, 56, labelText(node.entity.label), abbreviatedState(node.entity.state), style.accent);
      }

      ctx.restore();
    }
  }, [currentEvent, currentTime, height, highlightedEntityIds, machineSpriteSet, renderModel, sceneIconSet, viewport, width, workerSpriteSheet]);

  return (
    <canvas
      ref={canvasRef}
      className="scene-canvas"
      width={width}
      height={height}
      onClick={(event) => {
        if (!onSelectEntity) return;
        const rect = event.currentTarget.getBoundingClientRect();
        const localX = event.clientX - rect.left;
        const localY = event.clientY - rect.top;
        const hit = [...boundsRef.current].reverse().find(
          (bounds) =>
            localX >= bounds.x &&
            localX <= bounds.x + bounds.width &&
            localY >= bounds.y &&
            localY <= bounds.y + bounds.height,
        );
        if (hit) onSelectEntity(hit.entityId);
      }}
    />
  );
}
