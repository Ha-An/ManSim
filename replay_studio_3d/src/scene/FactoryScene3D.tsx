import { Canvas, useThree } from "@react-three/fiber";
import { Billboard, Html, Line, OrbitControls, OrthographicCamera, Text } from "@react-three/drei";
import { useEffect, useMemo } from "react";
import type { ThreeEvent } from "@react-three/fiber";
import type { ReplayEvent } from "../replay-core/types/event";
import type { LayoutGridConfig, LayoutGridObjectFootprint } from "../replay-core/types/layout";
import type { RenderNode, RenderRegion, ReplayRenderModel } from "../replay-core/types/replay";
import {
  createCoordinateMapper,
  footprintForEntity,
  isMotionActive,
  motionDisplayPathPoints,
  motionPathPoints,
  samplePath,
  type CoordinateMapper,
  type WorldPoint,
  type WorldRect,
} from "./coordinates";
import { Block, HumanoidBlockModel, ItemShape } from "./blockModels";
import {
  cargoItemId,
  cargoItemType,
  humanoidStateValue,
  machineColor,
  machineProcessProgress,
  primitiveCode,
  taskCode,
  taskWindowProgress,
  workerColor,
} from "./entityVisuals";

interface FactoryScene3DProps {
  renderModel: ReplayRenderModel;
  currentEvent?: ReplayEvent;
  currentTime: number;
  viewport: { width: number; height: number };
  selectedEntityId?: string;
  onSelectEntity?: SelectHandler;
}

export type SelectHandler = (entityId: string | undefined) => void;

function stopSelect(event: ThreeEvent<MouseEvent>, entityId: string, onSelect?: SelectHandler): void {
  event.stopPropagation();
  onSelect?.(entityId);
}

function CameraRig({ gridWidth, gridHeight }: { gridWidth: number; gridHeight: number }) {
  const { camera, size } = useThree();
  useEffect(() => {
    const largest = Math.max(gridWidth, gridHeight);
    const zoom = Math.max(4.2, Math.min(size.width / (gridWidth + 18), size.height / (gridHeight + 18)));
    camera.position.set(largest * 0.64, largest * 0.68, largest * 0.64);
    camera.lookAt(0, 0, 0);
    if ("zoom" in camera) {
      camera.zoom = zoom;
      camera.updateProjectionMatrix();
    }
  }, [camera, gridHeight, gridWidth, size.height, size.width]);

  return <OrbitControls makeDefault enableDamping dampingFactor={0.08} target={[0, 0, 0]} maxPolarAngle={Math.PI * 0.47} minDistance={20} maxDistance={180} />;
}

function regionColor(region: RenderRegion): string {
  if (region.kind === "inspection") return "#dceaff";
  if (region.kind === "storage") return "#dfeeff";
  if (region.kind === "battery") return "#dff7ec";
  if (region.kind === "station") return "#eaf3ff";
  return "#e7eef8";
}

function RegionPlates({ regions, mapper }: { regions: RenderRegion[]; mapper: CoordinateMapper }) {
  return (
    <group>
      {regions.map((region) => {
        const center = mapper.pointToWorld(
          { x: region.position.x + region.size.width / 2, y: region.position.y + region.size.height / 2 },
          0.03,
        );
        const size = mapper.viewportSizeToWorld(region.size);
        return (
          <group key={region.region_id}>
            <Block position={[center.x, 0.02, center.z]} size={[size.width, 0.04, size.depth]} color={regionColor(region)} opacity={0.78} />
            <Billboard position={[center.x - size.width / 2 + 1.2, 0.34, center.z - size.depth / 2 + 1.2]}>
              <Text fontSize={0.75} color="#183353" anchorX="left" anchorY="middle" outlineWidth={0.01} outlineColor="#ffffff">
                {region.label}
              </Text>
            </Billboard>
          </group>
        );
      })}
    </group>
  );
}

function regionForTile(tile: { x: number; y: number }, regions: RenderRegion[], mapper: CoordinateMapper): RenderRegion | undefined {
  return regions.find((region) => {
    const startX = Math.floor(region.position.x / mapper.tileWidth);
    const endX = Math.ceil((region.position.x + region.size.width) / mapper.tileWidth);
    const startY = Math.floor(region.position.y / mapper.tileHeight);
    const endY = Math.ceil((region.position.y + region.size.height) / mapper.tileHeight);
    return tile.x >= startX && tile.x < endX && tile.y >= startY && tile.y < endY;
  });
}

function wallColor(tile: { x: number; y: number }, regions: RenderRegion[], mapper: CoordinateMapper): string {
  const region = regionForTile(tile, regions, mapper);
  if (region?.region_id === "station_1_region") return "#175775";
  if (region?.region_id === "station_2_region") return "#266753";
  if (region?.region_id === "inspection_region") return "#815323";
  return "#10223a";
}

type WallRenderKind = "high" | "low";

function wallLikeObjectTiles(grid?: LayoutGridConfig): Array<{ x: number; y: number; kind: WallRenderKind }> {
  const out: Array<{ x: number; y: number; kind: WallRenderKind }> = [];
  for (const footprint of grid?.object_footprints ?? []) {
    if (
      footprint.object_type !== "shelf_wall" &&
      footprint.object_type !== "shelf_low_wall" &&
      footprint.object_type !== "shelf_blocker"
    ) {
      continue;
    }
    const kind: WallRenderKind = footprint.object_type === "shelf_wall" ? "high" : "low";
    for (let x = footprint.x; x < footprint.x + footprint.width; x += 1) {
      for (let y = footprint.y; y < footprint.y + footprint.height; y += 1) {
        out.push({ x, y, kind });
      }
    }
  }
  return out;
}

function GridShell({ renderModel, mapper }: { renderModel: ReplayRenderModel; mapper: CoordinateMapper }) {
  const grid = renderModel.grid;
  const wallTiles = useMemo(() => {
    const byKey = new Map<string, { x: number; y: number; kind: WallRenderKind }>();
    const objectTiles = wallLikeObjectTiles(grid);
    const objectKinds = new Map<string, WallRenderKind>();
    for (const tile of objectTiles) objectKinds.set(`${tile.x},${tile.y}`, tile.kind);
    for (const tile of grid?.walls ?? []) {
      const key = `${tile.x},${tile.y}`;
      byKey.set(key, { x: tile.x, y: tile.y, kind: objectKinds.get(key) ?? "high" });
    }
    for (const tile of objectTiles) {
      byKey.set(`${tile.x},${tile.y}`, tile);
    }
    return Array.from(byKey.values());
  }, [grid]);
  return (
    <group>
      <Block position={[0, -0.04, 0]} size={[mapper.gridWidth, 0.08, mapper.gridHeight]} color="#e8f1fb" />
      <gridHelper args={[Math.max(mapper.gridWidth, mapper.gridHeight), Math.max(mapper.gridWidth, mapper.gridHeight), "#9fb8d9", "#d2deee"]} position={[0, 0.03, 0]} />
      <RegionPlates regions={renderModel.regions} mapper={mapper} />
      {(grid?.cart_route_tiles ?? []).map((tile, index) => {
        const center = mapper.tileCenterToWorld({ x: tile.x + 0.5, y: tile.y + 0.5 }, 0.07);
        return <Block key={`cart-route:${index}`} position={[center.x, center.y, center.z]} size={[1, 0.1, 1]} color="#38bdf8" opacity={0.82} />;
      })}
      {(grid?.cart_parking_tiles ?? []).map((tile, index) => {
        const center = mapper.tileCenterToWorld({ x: tile.x + 0.5, y: tile.y + 0.5 }, 0.1);
        return <Block key={`cart-parking:${index}`} position={[center.x, center.y, center.z]} size={[0.92, 0.14, 0.92]} color="#facc15" opacity={0.9} />;
      })}
      {wallTiles.map((tile, index) => {
        const height = tile.kind === "low" ? 0.7 : 1.4;
        const center = mapper.tileCenterToWorld({ x: tile.x + 0.5, y: tile.y + 0.5 }, height / 2);
        const color = tile.kind === "low" ? "#8f98a3" : wallColor(tile, renderModel.regions, mapper);
        return <Block key={`wall:${index}`} position={[center.x, center.y, center.z]} size={[1, height, 1]} color={color} />;
      })}
      {(grid?.doors ?? []).map((tile, index) => {
        const center = mapper.tileCenterToWorld({ x: tile.x + 0.5, y: tile.y + 0.5 }, 0.08);
        return <Block key={`door:${index}`} position={[center.x, center.y, center.z]} size={[1, 0.16, 1]} color="#f4b642" />;
      })}
    </group>
  );
}

function Label({ text, position }: { text: string; position: WorldPoint }) {
  return (
    <Billboard position={[position.x, position.y, position.z]}>
      <Text fontSize={0.62} color="#173252" anchorX="center" anchorY="middle" outlineWidth={0.025} outlineColor="#f8fbff">
        {text}
      </Text>
    </Billboard>
  );
}

function taskBubbleText(entity: RenderNode["entity"]): string {
  const availability = humanoidStateValue(entity, "availability");
  if (availability === "BLOCKED") return "BLK";
  if (availability === "WAITING") return "WAIT";
  if (availability === "DISABLED") return "DIS";
  const task = taskCode(entity);
  if (!task) return "";
  if (task === "REPLENISH_MATERIAL") return "MAT";
  if (task === "TRANSFER") return "MOVE";
  if (task === "SETUP_MACHINE") return "SET";
  if (task === "LOAD_MACHINE") return "LOAD";
  if (task === "UNLOAD_MACHINE") return "UNLD";
  if (task === "INSPECT_PRODUCT") return "INSP";
  if (task === "REPAIR_MACHINE") return "FIX";
  if (task === "PREVENTIVE_MAINTENANCE") return "PM";
  if (task === "HANDOVER_ITEM") return "HAND";
  if (task === "COLLECT_WASTE_OR_SCRAP") return "SCRP";
  if (task === "OPERATE_VEHICLE_TRANSPORT") return "CART";
  return task.replace(/_/g, "").slice(0, 4);
}

function WorkerHud({ entity, currentTime, moving }: { entity: RenderNode["entity"]; currentTime: number; moving: boolean }) {
  const progress = taskWindowProgress(entity, currentTime);
  const bubble = taskBubbleText(entity);
  const showTaskHud = !moving && Boolean(bubble) && humanoidStateValue(entity, "availability") !== "AVAILABLE";
  if (!showTaskHud) return null;
  return (
    <Html position={[0, 2.72, 0]} center transform={false} className="worker-world-hud">
      <div className="worker-world-hud-inner">
        <div className="worker-world-task">
          <div className="worker-world-bubble">{bubble}</div>
          {progress !== undefined && (
            <div className="worker-world-progress">
              <span style={{ width: `${Math.round(progress * 100)}%` }} />
            </div>
          )}
        </div>
      </div>
    </Html>
  );
}

function SelectionRing({ selected, width, depth }: { selected: boolean; width: number; depth: number }) {
  if (!selected) return null;
  return <Block position={[0, 0.08, 0]} size={[width + 0.35, 0.06, depth + 0.35]} color="#22d3ee" opacity={0.24} />;
}

const SHIP_TASK_CODES = new Set(["WELD_SEAM", "PREPARE_SURFACE", "PAINT_SURFACE", "VERIFY_SHIP_SECTION", "APPLY_SEALANT"]);

function viewportPointToTile(point: { x: number; y: number }, mapper: CoordinateMapper): { x: number; y: number } {
  return {
    x: Math.floor(point.x / mapper.tileWidth),
    y: Math.floor(point.y / mapper.tileHeight),
  };
}

function adjacentShipTileOffset(
  entity: RenderNode["entity"],
  mapper: CoordinateMapper,
  shipBlockedTileKeys?: Set<string>,
): { x: number; z: number } {
  if (!shipBlockedTileKeys?.size || !entity.position) return { x: 0, z: 0 };
  if (!SHIP_TASK_CODES.has(taskCode(entity) ?? "")) return { x: 0, z: 0 };

  const tile = viewportPointToTile(entity.position, mapper);
  const neighbors = [
    { x: tile.x + 1, y: tile.y },
    { x: tile.x - 1, y: tile.y },
    { x: tile.x, y: tile.y + 1 },
    { x: tile.x, y: tile.y - 1 },
  ];

  let awayX = 0;
  let awayZ = 0;
  for (const neighbor of neighbors) {
    if (!shipBlockedTileKeys.has(`${neighbor.x},${neighbor.y}`)) continue;
    awayX += tile.x - neighbor.x;
    awayZ += tile.y - neighbor.y;
  }

  const length = Math.hypot(awayX, awayZ);
  if (length <= 0) return { x: 0, z: 0 };
  // The worker stands on a legal adjacent service tile.  This visual standoff
  // keeps the block-model body from overlapping the ship plate next to it.
  const standoff = 0.92;
  return { x: (awayX / length) * standoff, z: (awayZ / length) * standoff };
}

function WorkerModel({
  node,
  mapper,
  currentTime,
  selected,
  onSelect,
  shipBlockedTileKeys,
}: {
  node: RenderNode;
  mapper: CoordinateMapper;
  currentTime: number;
  selected: boolean;
  onSelect?: SelectHandler;
  shipBlockedTileKeys?: Set<string>;
}) {
  const entity = node.entity;
  const position = mapper.pointToWorld(node.position);
  const motion = entity.attributes.motion;
  const path = motionPathPoints(motion);
  const moving = isMotionActive(motion, currentTime);
  const rotationY = useMemo(() => {
    if (!moving || path.length < 2 || !motion || typeof motion !== "object") return 0;
    const startedAt = Number((motion as Record<string, unknown>).started_at);
    const endedAt = Number((motion as Record<string, unknown>).ended_at);
    const progress = (currentTime - startedAt) / Math.max(0.0001, endedAt - startedAt);
    const sample = samplePath(path, progress);
    if (!sample) return 0;
    return Math.atan2(Math.cos(sample.angle), Math.sin(sample.angle));
  }, [currentTime, motion, moving, path]);
  const color = workerColor(entity);
  const cargoId = cargoItemId(entity);
  const cargoType = cargoItemType(entity) || cargoId;
  const availability = humanoidStateValue(entity, "availability");
  const walkSwing = moving ? Math.sin(currentTime * 9.5) * 0.42 : 0;
  const workSwing = !moving && availability === "EXECUTING" ? Math.sin(currentTime * 8.5) * 0.34 : 0;
  const shipOffset = !moving ? adjacentShipTileOffset(entity, mapper, shipBlockedTileKeys) : { x: 0, z: 0 };
  return (
    <group position={[position.x + shipOffset.x, 0, position.z + shipOffset.z]} rotation={[0, rotationY, 0]} onClick={(event) => stopSelect(event, entity.entity_id, onSelect)}>
      <SelectionRing selected={selected} width={1.4} depth={1.4} />
      <HumanoidBlockModel color={color} cargoId={cargoId} cargoType={cargoType} walkSwing={walkSwing} workSwing={workSwing} />
      <Label text={entity.entity_id} position={{ x: 0, y: 2.35, z: 0 }} />
      <WorkerHud entity={entity} currentTime={currentTime} moving={moving} />
    </group>
  );
}

function machineStateText(entity: RenderNode["entity"]): string {
  const raw = typeof entity.attributes.machine_state === "string" ? entity.attributes.machine_state.toUpperCase() : entity.state.toUpperCase();
  if (raw.includes("BROKEN")) return "BROK";
  if (raw.includes("REPAIR")) return "FIX";
  if (raw.includes("PROCESS")) return "RUN";
  if (raw.includes("SETUP")) return "SET";
  if (raw.includes("BLOCK")) return "BLK";
  if (raw.includes("WAIT")) return "WAIT";
  if (raw.includes("IDLE") || raw.includes("READY")) return "IDLE";
  return raw.replace(/[^A-Z0-9]/g, "").slice(0, 4) || "STAT";
}

function MachineStatusBubble({ entity, height }: { entity: RenderNode["entity"]; height: number }) {
  const label = machineStateText(entity);
  return (
    <Html position={[0, height, 0]} center transform={false} className="machine-world-hud">
      <div className="machine-world-bubble">{label}</div>
    </Html>
  );
}

function InspectionTableVisual({
  entityId,
  label,
  rect,
  selected,
  onSelect,
}: {
  entityId: string;
  label: string;
  rect: WorldRect;
  selected: boolean;
  onSelect?: SelectHandler;
}) {
  const topWidth = rect.width * 0.92;
  const topDepth = rect.depth * 0.78;
  return (
    <group position={[rect.center.x, 0, rect.center.z]} onClick={(event) => stopSelect(event, entityId, onSelect)}>
      <SelectionRing selected={selected} width={rect.width} depth={rect.depth} />
      <Block position={[0, 0.48, 0]} size={[topWidth, 0.18, topDepth]} color="#31445e" />
      <Block position={[0, 0.62, 0]} size={[topWidth * 0.92, 0.08, topDepth * 0.84]} color="#dce8f5" />
      <Block position={[-topWidth * 0.38, 0.22, -topDepth * 0.32]} size={[0.18, 0.44, 0.18]} color="#26364b" />
      <Block position={[topWidth * 0.38, 0.22, -topDepth * 0.32]} size={[0.18, 0.44, 0.18]} color="#26364b" />
      <Block position={[-topWidth * 0.38, 0.22, topDepth * 0.32]} size={[0.18, 0.44, 0.18]} color="#26364b" />
      <Block position={[topWidth * 0.38, 0.22, topDepth * 0.32]} size={[0.18, 0.44, 0.18]} color="#26364b" />
      <Block position={[0, 0.9, -topDepth * 0.22]} size={[topWidth * 0.58, 0.12, 0.12]} color="#2c5b83" />
      <Block position={[0, 1.1, -topDepth * 0.22]} size={[topWidth * 0.58, 0.08, 0.08]} color="#5ee08e" />
      <Block position={[topWidth * 0.34, 0.98, topDepth * 0.16]} size={[0.18, 0.62, 0.18]} color="#22324a" />
      <Block position={[topWidth * 0.2, 1.27, topDepth * 0.16]} rotation={[0, 0, 0.35]} size={[0.44, 0.1, 0.1]} color="#22324a" />
      <Block position={[topWidth * 0.02, 1.2, topDepth * 0.16]} size={[0.18, 0.12, 0.18]} color="#ffcf63" />
      <Block position={[0, 0.72, topDepth * 0.22]} size={[topWidth * 0.46, 0.05, 0.18]} color="#29a8ff" opacity={0.72} />
      <Label text={label} position={{ x: 0, y: 1.65, z: 0 }} />
    </group>
  );
}

function InspectionTableModel({
  node,
  grid,
  mapper,
  selected,
  onSelect,
}: {
  node: RenderNode;
  grid?: LayoutGridConfig;
  mapper: CoordinateMapper;
  selected: boolean;
  onSelect?: SelectHandler;
}) {
  const entity = node.entity;
  const footprint = footprintForEntity(grid, entity.entity_id);
  const rect = footprint ? mapper.footprintToWorldRect(footprint) : { center: mapper.pointToWorld(node.position), width: 5, depth: 3 };
  return (
    <InspectionTableVisual
      entityId={entity.entity_id}
      label={entity.label || "Inspection Table"}
      rect={rect}
      selected={selected}
      onSelect={onSelect}
    />
  );
}

function StaticInspectionTables({
  grid,
  regions,
  mapper,
  renderedIds,
  selectedEntityId,
  onSelect,
}: {
  grid?: LayoutGridConfig;
  regions: RenderRegion[];
  mapper: CoordinateMapper;
  renderedIds: Set<string>;
  selectedEntityId?: string;
  onSelect?: SelectHandler;
}) {
  const footprints = (grid?.object_footprints ?? []).filter(
    (footprint): footprint is LayoutGridObjectFootprint =>
      footprint.object_type === "inspection_table" && !renderedIds.has(footprint.object_id),
  );
  const fallbackRegion = footprints.length === 0 && !renderedIds.has("inspection_table")
    ? regions.find((region) => region.kind === "inspection" || region.region_id.includes("inspection"))
    : undefined;
  return (
    <>
      {footprints.map((footprint) => (
        <InspectionTableVisual
          key={`static:${footprint.object_id}`}
          entityId={footprint.object_id}
          label="Inspection Table"
          rect={mapper.footprintToWorldRect(footprint)}
          selected={selectedEntityId === footprint.object_id}
          onSelect={onSelect}
        />
      ))}
      {fallbackRegion && (
        <InspectionTableVisual
          entityId="inspection_table"
          label="Inspection Table"
          rect={{
            center: mapper.pointToWorld(
              {
                x: fallbackRegion.position.x + fallbackRegion.size.width * 0.5,
                y: fallbackRegion.position.y + fallbackRegion.size.height * 0.68,
              },
              0,
            ),
            width: 6,
            depth: 4,
          }}
          selected={selectedEntityId === "inspection_table"}
          onSelect={onSelect}
        />
      )}
    </>
  );
}

function queueItemCount(entity: RenderNode["entity"]): number {
  const attrs = entity.attributes;
  const explicitCount = attrs.queue_size ?? attrs.item_count ?? attrs.count ?? attrs.completed_count;
  const numericCount = Number(explicitCount);
  if (Number.isFinite(numericCount)) return Math.max(0, Math.round(numericCount));

  for (const key of ["item_ids", "items"]) {
    const value = attrs[key];
    if (Array.isArray(value)) return value.length;
  }

  return 0;
}

function queueItemType(entity: RenderNode["entity"]): string {
  const explicitType = entity.attributes.item_type;
  if (typeof explicitType === "string" && explicitType.trim()) return explicitType;

  const id = entity.entity_id.toLowerCase();
  if (id.includes("scrap")) return "scrap";
  if (id.includes("material_queue")) return "material";
  if (id.includes("intermediate_queue_4")) return "product";
  if (id.includes("intermediate_queue")) return "intermediate";
  if (id.includes("station_1_output")) return "intermediate";
  if (id.includes("station_2_output") || id.includes("inspection_output") || id.includes("completed")) return "product";
  return entity.entity_type;
}

function platformSurfaceColor(entity: RenderNode["entity"]): string {
  const id = entity.entity_id.toLowerCase();
  if (entity.entity_type === "charger") return "#4fcf8b";
  if (entity.entity_type === "shelf") return "#52657d";
  if (entity.entity_type === "material_slot") return "#30435c";
  if (entity.entity_type === "ship_hull" || entity.entity_type === "ship_hull_segment") return "#56697f";
  if (entity.entity_type === "cart_parking_spot") return "#facc15";
  if (entity.entity_type === "ship_section" || entity.entity_type === "ship_work_tile") {
    const sectionState = String(
      entity.attributes.ship_surface_state ??
        entity.attributes.surface_tile_state ??
        entity.attributes.ship_section_state ??
        entity.attributes.state ??
        entity.state ??
        "",
    ).toUpperCase();
    if (sectionState === "COMPLETE") return "#16a085";
    if (sectionState === "PAINTED") return "#3498db";
    if (sectionState === "SURFACE_PREPARED") return "#95a5a6";
    if (sectionState === "WELDED") return "#f39c12";
    if (sectionState === "REWORK_REQUIRED") return "#e74c3c";
    return "#2f3a45";
  }
  if (entity.entity_type === "tool_rack") return "#8e44ad";
  if (entity.entity_type === "material_rack") return "#d68910";
  if (entity.entity_type === "paint_rack") return "#d2529f";
  if (entity.entity_type === "scrap_bin") return "#c0392b";
  if (id.includes("scrap")) return "#e56b6f";
  if (id.includes("material_queue") || id.includes("intermediate_queue")) return "#f4b642";
  if (id.includes("output_queue") || id.includes("completed_product_buffer") || id.includes("warehouse_buffer")) return "#75a7ff";
  return entity.entity_type === "buffer" ? "#75a7ff" : "#f4b642";
}

function attrString(entity: RenderNode["entity"], key: string): string {
  const value = entity.attributes[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function itemTypeFromId(itemId: string, fallback: string): string {
  const normalized = itemId.trim().toUpperCase();
  if (normalized.startsWith("MAT-")) return "material";
  if (normalized.startsWith("INT-")) return "intermediate";
  if (normalized.startsWith("PRODUCT-")) return "product";
  if (normalized.includes("BATTERY")) return "battery";
  return fallback;
}

function attributeTile(entity: RenderNode["entity"]): { x: number; y: number } | undefined {
  const value = entity.attributes.tile;
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const candidate = value as Record<string, unknown>;
  const x = Number(candidate.x);
  const y = Number(candidate.y);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : undefined;
}

function shipBlockedTileKeys(grid: LayoutGridConfig | undefined, nodes: RenderNode[]): Set<string> {
  const keys = new Set<string>();
  for (const footprint of grid?.object_footprints ?? []) {
    if (footprint.object_type !== "ship_hull" && footprint.object_type !== "ship_hull_segment") continue;
    for (let x = footprint.x; x < footprint.x + footprint.width; x += 1) {
      for (let y = footprint.y; y < footprint.y + footprint.height; y += 1) {
        keys.add(`${x},${y}`);
      }
    }
  }
  for (const node of nodes) {
    if (node.entity.entity_type !== "ship_work_tile") continue;
    const tile = attributeTile(node.entity);
    if (tile) keys.add(`${tile.x},${tile.y}`);
  }
  return keys;
}

function machineStationId(entity: RenderNode["entity"]): number | undefined {
  const match = entity.entity_id.match(/^S(\d+)M/i);
  if (match?.[1]) return Number.parseInt(match[1], 10);
  const station = entity.attributes.station;
  if (typeof station === "number") return station;
  if (typeof station === "string") {
    const parsed = Number.parseInt(station.replace(/\D/g, ""), 10);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

type MachineSlot = {
  key: string;
  label: string;
  itemId: string;
  itemType: string;
  color: string;
  lane: "input" | "output";
};

function machineSlotItems(entity: RenderNode["entity"]): MachineSlot[] {
  const station = machineStationId(entity);
  let materialId = attrString(entity, "input_material_id");
  let intermediateId = attrString(entity, "input_intermediate_id");
  const genericInputId = attrString(entity, "input_item_id");
  if (!materialId && !intermediateId && genericInputId) {
    const genericType = itemTypeFromId(genericInputId, "material");
    if (genericType === "intermediate") intermediateId = genericInputId;
    else materialId = genericInputId;
  }
  const outputId = attrString(entity, "output_item_id");
  const slots: MachineSlot[] = [
    { key: "material", label: "MAT", itemId: materialId, itemType: "material", color: "#3f5f8f", lane: "input" as const },
  ];
  if (station === 2) {
    slots.push({ key: "intermediate", label: "INT", itemId: intermediateId, itemType: "intermediate", color: "#2f6f7f", lane: "input" as const });
  }
  slots.push({
    key: "output",
    label: "OUT",
    itemId: outputId,
    itemType: itemTypeFromId(outputId, station === 1 ? "intermediate" : "product"),
    color: "#3f7d55",
    lane: "output" as const,
  });
  return slots;
}

function MachineItemSlots({ entity, width, depth }: { entity: RenderNode["entity"]; width: number; depth: number }) {
  const slots = machineSlotItems(entity);
  const slotWidth = Math.max(0.44, Math.min(0.82, width * 0.18));
  const slotDepth = Math.max(0.38, Math.min(0.72, depth * 0.26));
  const inputSlots = slots.filter((slot) => slot.lane === "input");
  const outputSlots = slots.filter((slot) => slot.lane === "output");
  const inputZ = -depth * 0.33;
  const outputZ = depth * 0.33;
  const laneLabelZOffset = slotDepth * 0.72;
  const inputSpacing = Math.min(width * 0.2, slotWidth + 0.18);
  const slotPosition = (slot: (typeof slots)[number], index: number, laneCount: number): [number, number, number] => {
    if (slot.lane === "output") return [width * 0.24, 0, outputZ];
    if (laneCount === 1) return [-width * 0.24, 0, inputZ];
    return [(index - (laneCount - 1) / 2) * inputSpacing - width * 0.18, 0, inputZ];
  };
  return (
    <group>
      {inputSlots.length > 0 && (
        <Billboard position={[-width * 0.25, 1.58, inputZ - laneLabelZOffset]}>
          <Text fontSize={0.18} color="#93c5fd" anchorX="center" anchorY="middle" outlineWidth={0.016} outlineColor="#0f172a">
            INPUT
          </Text>
        </Billboard>
      )}
      {outputSlots.length > 0 && (
        <Billboard position={[width * 0.24, 1.58, outputZ + laneLabelZOffset]}>
          <Text fontSize={0.18} color="#86efac" anchorX="center" anchorY="middle" outlineWidth={0.016} outlineColor="#0f172a">
            OUTPUT
          </Text>
        </Billboard>
      )}
      {slots.map((slot) => {
        const laneSlots = slot.lane === "input" ? inputSlots : outputSlots;
        const laneIndex = laneSlots.findIndex((candidate) => candidate.key === slot.key);
        const [x, y, z] = slotPosition(slot, laneIndex, laneSlots.length);
        return (
          <group key={slot.key} position={[x, y, z]}>
            <Block position={[0, 1.17, 0]} size={[slotWidth, 0.08, slotDepth]} color={slot.itemId ? slot.color : slot.lane === "input" ? "#172033" : "#14251a"} />
            <Block position={[0, 1.225, 0]} size={[slotWidth * 0.82, 0.03, slotDepth * 0.72]} color={slot.itemId ? "#c8d7e8" : "#31445f"} opacity={slot.itemId ? 0.88 : 0.72} />
            {slot.itemId && <ItemShape itemType={slot.itemType || slot.itemId} position={[0, 1.44, 0]} scale={0.74} />}
            <Billboard position={[0, 1.62, slot.lane === "input" ? -slotDepth * 0.6 : slotDepth * 0.6]}>
              <Text fontSize={0.22} color="#dbeafe" anchorX="center" anchorY="middle" outlineWidth={0.018} outlineColor="#0f172a">
                {slot.label}
              </Text>
            </Billboard>
          </group>
        );
      })}
    </group>
  );
}

function MachineModel({
  node,
  grid,
  mapper,
  currentTime,
  selected,
  onSelect,
}: {
  node: RenderNode;
  grid?: LayoutGridConfig;
  mapper: CoordinateMapper;
  currentTime: number;
  selected: boolean;
  onSelect?: SelectHandler;
}) {
  const entity = node.entity;
  const footprint = footprintForEntity(grid, entity.entity_id);
  const rect = footprint ? mapper.footprintToWorldRect(footprint) : { center: mapper.pointToWorld(node.position), width: 4, depth: 3 };
  const color = machineColor(entity);
  const progress = machineProcessProgress(entity, currentTime);
  return (
    <group position={[rect.center.x, 0, rect.center.z]} onClick={(event) => stopSelect(event, entity.entity_id, onSelect)}>
      <SelectionRing selected={selected} width={rect.width} depth={rect.depth} />
      <Block position={[0, 0.35, 0]} size={[rect.width, 0.7, rect.depth]} color="#25364b" />
      <Block position={[0, 0.86, 0]} size={[rect.width * 0.72, 0.28, rect.depth * 0.72]} color={color} />
      <Block position={[rect.width * 0.22, 1.12, -rect.depth * 0.32]} size={[rect.width * 0.22, 0.24, 0.08]} color="#101827" />
      <Block position={[rect.width * 0.22, 1.14, -rect.depth * 0.37]} size={[rect.width * 0.16, 0.1, 0.04]} color={progress === undefined ? "#86efac" : "#facc15"} />
      <MachineItemSlots entity={entity} width={rect.width} depth={rect.depth} />
      {progress !== undefined && (
        <group position={[0, 1.21, -rect.depth * 0.02]}>
          <Block position={[0, 0, 0]} size={[rect.width * 0.62, 0.07, 0.08]} color="#172033" />
          <Block position={[-rect.width * 0.31 + (rect.width * 0.31) * progress, 0.04, 0]} size={[Math.max(0.02, rect.width * 0.62 * progress), 0.08, 0.1]} color="#5ee08e" />
        </group>
      )}
      <Label text={entity.label || entity.entity_id} position={{ x: 0, y: 1.65, z: 0 }} />
      <MachineStatusBubble entity={entity} height={1.92} />
    </group>
  );
}

function PlatformModel({
  node,
  grid,
  mapper,
  selected,
  onSelect,
}: {
  node: RenderNode;
  grid?: LayoutGridConfig;
  mapper: CoordinateMapper;
  selected: boolean;
  onSelect?: SelectHandler;
}) {
  const entity = node.entity;
  const workTile = entity.entity_type === "ship_work_tile" ? attributeTile(entity) : undefined;
  const footprint = footprintForEntity(grid, entity.entity_id);
  const rect = workTile
    ? {
        center: mapper.tileCenterToWorld({ x: workTile.x + 0.5, y: workTile.y + 0.5 }),
        width: 1,
        depth: 1,
      }
    : footprint
      ? mapper.footprintToWorldRect(footprint)
      : { center: mapper.pointToWorld(node.position), width: 4, depth: 2 };
  const queueSize = queueItemCount(entity);
  const displayItemType = queueItemType(entity);
  const isMaterialSlot = entity.entity_type === "material_slot";
  const isShelf = entity.entity_type === "shelf";
  const isShipWorkTile = entity.entity_type === "ship_work_tile";
  const isShipHullSegment = entity.entity_type === "ship_hull" || entity.entity_type === "ship_hull_segment";
  const showQueueCount = entity.entity_type === "queue" || entity.entity_type === "buffer";
  const occupied = Boolean(entity.attributes.occupied || entity.attributes.material_item_id);
  const baseColor = platformSurfaceColor(entity);
  if (isShelf) return null;
  return (
    <group position={[rect.center.x, 0, rect.center.z]} onClick={(event) => stopSelect(event, entity.entity_id, onSelect)}>
      <SelectionRing selected={selected} width={rect.width} depth={rect.depth} />
      {isMaterialSlot && <Block position={[0, 0.35, 0]} size={[rect.width, 0.7, rect.depth]} color="#8f98a3" />}
      {!isMaterialSlot && !isShipWorkTile && (
        <Block
          position={[0, isShipHullSegment ? 0.35 : 0.18, 0]}
          size={[rect.width, isShipHullSegment ? 0.7 : 0.36, rect.depth]}
          color={isShipHullSegment ? baseColor : "#2c3f58"}
          opacity={isShipHullSegment ? 0.96 : 1}
        />
      )}
      {!isMaterialSlot && !isShipHullSegment && !isShipWorkTile && <Block position={[0, 0.42, 0]} size={[rect.width * 0.88, 0.1, rect.depth * 0.72]} color={baseColor} />}
      {isShipWorkTile && (
        <Block
          position={[0, 0.7, 0]}
          size={[rect.width * 0.96, 1.4, rect.depth * 0.96]}
          color={baseColor}
          opacity={0.92}
        />
      )}
      {isMaterialSlot && occupied && (
        <ItemShape
          itemType="material"
          // Keep shelf material visible even when old replay logs still have a
          // half-height blocker on the same tile.
          position={[0, 0.86, 0]}
          scale={Math.max(0.62, Math.min(rect.width, rect.depth))}
        />
      )}
      {!isMaterialSlot && Array.from({ length: Math.min(8, Math.max(0, queueSize)) }).map((_, index) => {
        const x = -rect.width * 0.36 + (index % 4) * 0.55;
        const z = -rect.depth * 0.18 + Math.floor(index / 4) * 0.55;
        return <ItemShape key={index} itemType={displayItemType} position={[x, 0.75, z]} scale={0.68} />;
      })}
      {!isMaterialSlot && !isShelf && !isShipWorkTile && !isShipHullSegment && <Label text={entity.label || entity.entity_id} position={{ x: 0, y: 1.05, z: 0 }} />}
      {showQueueCount && (
        <Billboard position={[rect.width * 0.43, 1.18, -rect.depth * 0.34]}>
          <Text fontSize={0.56} color="#10233f" anchorX="center" anchorY="middle" outlineWidth={0.035} outlineColor="#ffffff">
            {`x${queueSize}`}
          </Text>
        </Billboard>
      )}
    </group>
  );
}

function ItemModel({
  node,
  mapper,
  selected,
  onSelect,
}: {
  node: RenderNode;
  mapper: CoordinateMapper;
  selected: boolean;
  onSelect?: SelectHandler;
}) {
  const entity = node.entity;
  const position = mapper.pointToWorld(node.position);
  return (
    <group position={[position.x, 0, position.z]} onClick={(event) => stopSelect(event, entity.entity_id, onSelect)}>
      <SelectionRing selected={selected} width={0.8} depth={0.8} />
      <ItemShape itemType={entity.attributes.item_type ?? entity.label} position={[0, 0.36, 0]} />
      <Label text={entity.label || entity.entity_id} position={{ x: 0, y: 0.92, z: 0 }} />
    </group>
  );
}

function CartModel({
  node,
  mapper,
  currentTime,
  selected,
  onSelect,
}: {
  node: RenderNode;
  mapper: CoordinateMapper;
  currentTime: number;
  selected: boolean;
  onSelect?: SelectHandler;
}) {
  const entity = node.entity;
  const anchorPosition = mapper.pointToWorld(node.position);
  const motion = entity.attributes.motion;
  const path = motionPathPoints(motion);
  const moving = isMotionActive(motion, currentTime);
  const motionHeading = moving && motion && typeof motion === "object" && path.length >= 2
    ? (() => {
        const startedAt = Number((motion as Record<string, unknown>).started_at);
        const endedAt = Number((motion as Record<string, unknown>).ended_at);
        const sample = samplePath(path, (currentTime - startedAt) / Math.max(0.0001, endedAt - startedAt));
        return sample ? { x: Math.cos(sample.angle), z: Math.sin(sample.angle) } : undefined;
      })()
    : undefined;
  const headingAttr = entity.attributes.heading;
  const headingX =
    motionHeading?.x ??
    (headingAttr && typeof headingAttr === "object" ? Number((headingAttr as Record<string, unknown>).x) : undefined) ??
    0;
  const headingZ =
    motionHeading?.z ??
    (headingAttr && typeof headingAttr === "object" ? Number((headingAttr as Record<string, unknown>).y) : undefined) ??
    1;
  const headingLength = Math.hypot(headingX, headingZ) || 1;
  const heading = { x: headingX / headingLength, z: headingZ / headingLength };
  const position = { x: anchorPosition.x + heading.x * 0.5, y: anchorPosition.y, z: anchorPosition.z + heading.z * 0.5 };
  const rotationY = Math.atan2(heading.x, heading.z);
  const inventoryKind = typeof entity.attributes.inventory_kind === "string" ? entity.attributes.inventory_kind : "";
  const inventoryCount = Number(entity.attributes.inventory_count ?? 0);
  const status = String(entity.attributes.status ?? entity.state ?? "parked").toUpperCase();
  const isActivelyOperated = status === "MOVING" || status === "LOADING";
  const reservedOrDriverId =
    typeof entity.attributes.assigned_worker_id === "string" && entity.attributes.assigned_worker_id.trim()
      ? entity.attributes.assigned_worker_id.trim()
      : typeof entity.attributes.owner === "string" && entity.attributes.owner.trim()
        ? entity.attributes.owner.trim()
        : "";
  const driverId = isActivelyOperated ? reservedOrDriverId : "";
  return (
    <group position={[position.x, 0, position.z]} rotation={[0, rotationY, 0]} onClick={(event) => stopSelect(event, entity.entity_id, onSelect)}>
      <SelectionRing selected={selected} width={1.35} depth={2.15} />
      <Block position={[0, 0.2, 0]} size={[1.08, 0.18, 2.02]} color="#1f2937" />
      <Block position={[0, 0.54, -0.52]} size={[0.94, 0.5, 0.82]} color="#2563eb" />
      <Block position={[0, 0.46, 0.55]} size={[1.02, 0.16, 0.92]} color={inventoryCount > 0 ? "#334155" : "#475569"} />
      <Block position={[-0.42, 0.68, 0.55]} size={[0.08, 0.34, 0.82]} color="#0f172a" />
      <Block position={[0.42, 0.68, 0.55]} size={[0.08, 0.34, 0.82]} color="#0f172a" />
      <Block position={[0, 0.68, 0.16]} size={[0.88, 0.08, 0.08]} color="#0f172a" />
      <Block position={[-0.24, 0.24, 1.18]} size={[0.16, 0.1, 0.74]} color="#111827" />
      <Block position={[0.24, 0.24, 1.18]} size={[0.16, 0.1, 0.74]} color="#111827" />
      {driverId && (
        <group position={[0, 1.02, -0.58]} scale={[0.46, 0.46, 0.46]}>
          <HumanoidBlockModel color="#dbeafe" />
          <Billboard position={[0, 2.38, 0]}>
            <Text fontSize={0.34} color="#0f172a" anchorX="center" anchorY="middle" outlineWidth={0.035} outlineColor="#ffffff">
              {driverId}
            </Text>
          </Billboard>
        </group>
      )}
      {[
        [-0.46, 0.16, -0.92],
        [0.46, 0.16, -0.92],
        [-0.46, 0.16, 0.92],
        [0.46, 0.16, 0.92],
      ].map(([x, y, z], index) => (
        <Block key={index} position={[x, y, z]} size={[0.2, 0.26, 0.2]} color="#0f172a" />
      ))}
      {inventoryKind && inventoryCount > 0 ? (
        <group position={[0, 0.86, 0.55]}>
          <ItemShape itemType={inventoryKind} position={[-0.22, 0, -0.1]} scale={0.58} />
          <ItemShape itemType={inventoryKind} position={[0.2, 0, 0.08]} scale={0.5} />
        </group>
      ) : (
        <Block position={[0, 0.74, 0.55]} size={[0.7, 0.04, 0.56]} color="#94a3b8" opacity={0.38} wireframe />
      )}
      <Label text={entity.entity_id} position={{ x: 0, y: 1.4, z: -0.52 }} />
      <Billboard position={[0, 1.62, 0.58]}>
        <Text fontSize={0.34} color="#e0f2fe" anchorX="center" anchorY="middle" outlineWidth={0.025} outlineColor="#0f172a">
          {`x${Number.isFinite(inventoryCount) ? Math.max(0, Math.round(inventoryCount)) : 0}`}
        </Text>
      </Billboard>
      <Billboard position={[0, 1.9, 0]}>
        <Text fontSize={0.3} color="#dbeafe" anchorX="center" anchorY="middle" outlineWidth={0.02} outlineColor="#0f172a">
          {status}
        </Text>
      </Billboard>
    </group>
  );
}

function EntityModels({
  renderModel,
  mapper,
  currentTime,
  selectedEntityId,
  onSelectEntity,
  hiddenEntityIds,
}: {
  renderModel: ReplayRenderModel;
  mapper: CoordinateMapper;
  currentTime: number;
  selectedEntityId?: string;
  onSelectEntity?: SelectHandler;
  hiddenEntityIds?: Set<string>;
}) {
  const grid = renderModel.grid;
  const nodes = renderModel.nodes;
  const renderedIds = useMemo(() => new Set(nodes.map((node) => node.entity.entity_id)), [nodes]);
  const shipBlockedKeys = useMemo(() => shipBlockedTileKeys(grid, nodes), [grid, nodes]);

  return (
    <group>
      {nodes.map((node) => {
        if (hiddenEntityIds?.has(node.entity.entity_id)) return null;
        const selected = node.entity.entity_id === selectedEntityId;
        if (node.entity.entity_type === "worker" || node.entity.entity_type === "robot" || node.entity.entity_type === "transporter") {
          return (
            <WorkerModel
              key={node.entity.entity_id}
              node={node}
              mapper={mapper}
              currentTime={currentTime}
              selected={selected}
              onSelect={onSelectEntity}
              shipBlockedTileKeys={shipBlockedKeys}
            />
          );
        }
        if (node.entity.entity_type === "machine" || node.entity.entity_type === "workstation") {
          return <MachineModel key={node.entity.entity_id} node={node} grid={grid} mapper={mapper} currentTime={currentTime} selected={selected} onSelect={onSelectEntity} />;
        }
        if (node.entity.entity_type === "inspection_table") {
          return <InspectionTableModel key={node.entity.entity_id} node={node} grid={grid} mapper={mapper} selected={selected} onSelect={onSelectEntity} />;
        }
        if (node.entity.entity_type === "cart") {
          return <CartModel key={node.entity.entity_id} node={node} mapper={mapper} currentTime={currentTime} selected={selected} onSelect={onSelectEntity} />;
        }
        if (
          node.entity.entity_type === "queue" ||
          node.entity.entity_type === "buffer" ||
          node.entity.entity_type === "storage" ||
          node.entity.entity_type === "charger" ||
          node.entity.entity_type === "shelf" ||
          node.entity.entity_type === "material_slot" ||
          node.entity.entity_type === "ship_hull" ||
          node.entity.entity_type === "ship_hull_segment" ||
          node.entity.entity_type === "ship_section" ||
          node.entity.entity_type === "ship_work_tile" ||
          node.entity.entity_type === "cart_parking_spot" ||
          node.entity.entity_type === "tool_rack" ||
          node.entity.entity_type === "material_rack" ||
          node.entity.entity_type === "paint_rack" ||
          node.entity.entity_type === "scrap_bin"
        ) {
          return <PlatformModel key={node.entity.entity_id} node={node} grid={grid} mapper={mapper} selected={selected} onSelect={onSelectEntity} />;
        }
        return <ItemModel key={node.entity.entity_id} node={node} mapper={mapper} selected={selected} onSelect={onSelectEntity} />;
      })}
      <StaticInspectionTables
        grid={grid}
        regions={renderModel.regions}
        mapper={mapper}
        renderedIds={renderedIds}
        selectedEntityId={selectedEntityId}
        onSelect={onSelectEntity}
      />
    </group>
  );
}

function MotionPathOverlay({ renderModel, mapper, currentTime }: { renderModel: ReplayRenderModel; mapper: CoordinateMapper; currentTime: number }) {
  return (
    <group>
      {renderModel.nodes.map((node) => {
        const motion = node.entity.attributes.motion;
        if (!isMotionActive(motion, currentTime)) return null;
        const points = motionDisplayPathPoints(motion);
        if (points.length < 2) return null;
        const worldPoints = points.map((point) => {
          const world = mapper.pointToWorld(point, 0.16);
          return [world.x, world.y, world.z] as [number, number, number];
        });
        return <Line key={`motion:${node.entity.entity_id}`} points={worldPoints} color="#0b6cff" lineWidth={2} dashed dashSize={0.45} gapSize={0.28} transparent opacity={0.82} />;
      })}
    </group>
  );
}

function asXY(value: unknown): { x: number; y: number } | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Record<string, unknown>;
  const x = Number(candidate.x);
  const y = Number(candidate.y);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : undefined;
}

function conflictColor(event?: ReplayEvent): string {
  const severity = event?.payload.severity;
  if (severity === "error") return "#ff315a";
  if (severity === "warning") return "#ffb703";
  return "#4d8dff";
}

function TrafficConflictOverlay({ currentEvent, mapper }: { currentEvent?: ReplayEvent; mapper: CoordinateMapper }) {
  if (currentEvent?.event_type !== "traffic_conflict_detected") return null;
  const tile = asXY(currentEvent.payload.tile) ?? asXY(currentEvent.payload.tile_position);
  const edge = currentEvent.payload.edge && typeof currentEvent.payload.edge === "object" ? (currentEvent.payload.edge as Record<string, unknown>) : undefined;
  const edgeFrom = asXY(edge?.from) ?? asXY(currentEvent.payload.edge_from_position);
  const edgeTo = asXY(edge?.to) ?? asXY(currentEvent.payload.edge_to_position);
  const color = conflictColor(currentEvent);
  return (
    <group>
      {tile && (() => {
        const center = mapper.tileCenterToWorld({ x: tile.x + 0.5, y: tile.y + 0.5 }, 0.18);
        return <Block position={[center.x, center.y, center.z]} size={[1.12, 0.18, 1.12]} color={color} opacity={0.46} />;
      })()}
      {edgeFrom && edgeTo && (() => {
        const from = mapper.tileCenterToWorld({ x: edgeFrom.x + 0.5, y: edgeFrom.y + 0.5 }, 0.28);
        const to = mapper.tileCenterToWorld({ x: edgeTo.x + 0.5, y: edgeTo.y + 0.5 }, 0.28);
        return <Line points={[[from.x, from.y, from.z], [to.x, to.y, to.z]]} color={color} lineWidth={5} transparent opacity={0.9} />;
      })()}
      <Html position={[-mapper.gridWidth / 2 + 1, 3.2, mapper.gridHeight / 2 - 2]} transform={false}>
        <div className={`traffic-card ${currentEvent.payload.severity === "error" ? "error" : currentEvent.payload.severity === "warning" ? "warning" : "info"}`}>
          <strong>{String(currentEvent.payload.conflict_type ?? "TRAFFIC")}</strong>
          <span>{String(currentEvent.payload.label ?? "")}</span>
        </div>
      </Html>
    </group>
  );
}

export function FactoryScene3D({
  renderModel,
  currentEvent,
  currentTime,
  viewport,
  selectedEntityId,
  onSelectEntity,
}: FactoryScene3DProps) {
  const mapper = useMemo(() => createCoordinateMapper(renderModel.grid, viewport), [renderModel.grid, viewport]);
  return (
    <Canvas shadows gl={{ antialias: true, preserveDrawingBuffer: true }} onPointerMissed={() => onSelectEntity?.(undefined)}>
      <color attach="background" args={["#dfeaf5"]} />
      <ambientLight intensity={0.72} />
      <directionalLight position={[24, 44, 18]} intensity={1.25} castShadow shadow-mapSize-width={2048} shadow-mapSize-height={2048} />
      <OrthographicCamera makeDefault near={0.1} far={500} />
      <CameraRig gridWidth={mapper.gridWidth} gridHeight={mapper.gridHeight} />
      <FactoryWorldContents
        renderModel={renderModel}
        mapper={mapper}
        currentTime={currentTime}
        currentEvent={currentEvent}
        selectedEntityId={selectedEntityId}
        onSelectEntity={onSelectEntity}
      />
    </Canvas>
  );
}

export function FactoryWorldContents({
  renderModel,
  mapper,
  currentTime,
  currentEvent,
  selectedEntityId,
  onSelectEntity,
  hiddenEntityIds,
  showMotionPaths = true,
}: {
  renderModel: ReplayRenderModel;
  mapper: CoordinateMapper;
  currentTime: number;
  currentEvent?: ReplayEvent;
  selectedEntityId?: string;
  onSelectEntity?: SelectHandler;
  hiddenEntityIds?: Set<string>;
  showMotionPaths?: boolean;
}) {
  return (
    <>
      <GridShell renderModel={renderModel} mapper={mapper} />
      {showMotionPaths && <MotionPathOverlay renderModel={renderModel} mapper={mapper} currentTime={currentTime} />}
      <TrafficConflictOverlay currentEvent={currentEvent} mapper={mapper} />
      <EntityModels
        renderModel={renderModel}
        mapper={mapper}
        currentTime={currentTime}
        selectedEntityId={selectedEntityId}
        onSelectEntity={onSelectEntity}
        hiddenEntityIds={hiddenEntityIds}
      />
    </>
  );
}
