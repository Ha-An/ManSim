import { Canvas, useThree } from "@react-three/fiber";
import { Billboard, Html, Line, OrbitControls, OrthographicCamera, Text } from "@react-three/drei";
import { useEffect, useMemo } from "react";
import type { ThreeEvent } from "@react-three/fiber";
import type { ReplayEvent } from "../replay-core/types/event";
import type { LayoutGridConfig } from "../replay-core/types/layout";
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
} from "./coordinates";
import {
  cargoItemId,
  humanoidStateValue,
  itemColor,
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

type SelectHandler = (entityId: string | undefined) => void;

function stopSelect(event: ThreeEvent<MouseEvent>, entityId: string, onSelect?: SelectHandler): void {
  event.stopPropagation();
  onSelect?.(entityId);
}

function Block({
  position,
  size,
  color,
  opacity = 1,
  wireframe = false,
}: {
  position: [number, number, number];
  size: [number, number, number];
  color: string;
  opacity?: number;
  wireframe?: boolean;
}) {
  return (
    <mesh position={position} scale={size} castShadow receiveShadow>
      <boxGeometry args={[1, 1, 1]} />
      <meshStandardMaterial color={color} transparent={opacity < 1} opacity={opacity} wireframe={wireframe} roughness={0.78} metalness={0.05} />
    </mesh>
  );
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

function GridShell({ renderModel, mapper }: { renderModel: ReplayRenderModel; mapper: CoordinateMapper }) {
  const grid = renderModel.grid;
  return (
    <group>
      <Block position={[0, -0.04, 0]} size={[mapper.gridWidth, 0.08, mapper.gridHeight]} color="#e8f1fb" />
      <gridHelper args={[Math.max(mapper.gridWidth, mapper.gridHeight), Math.max(mapper.gridWidth, mapper.gridHeight), "#9fb8d9", "#d2deee"]} position={[0, 0.03, 0]} />
      <RegionPlates regions={renderModel.regions} mapper={mapper} />
      {(grid?.walls ?? []).map((tile, index) => {
        const center = mapper.tileCenterToWorld({ x: tile.x + 0.5, y: tile.y + 0.5 }, 0.7);
        return <Block key={`wall:${index}`} position={[center.x, center.y, center.z]} size={[1, 1.4, 1]} color={wallColor(tile, renderModel.regions, mapper)} />;
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

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function workerBatteryProgress(entity: RenderNode["entity"]): number | undefined {
  const batteryPct = Number(entity.attributes.battery_pct);
  if (!Number.isFinite(batteryPct)) return undefined;
  return clamp(batteryPct / 100, 0, 1);
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
  return task.replace(/_/g, "").slice(0, 4);
}

function WorkerBatteryHud({ entity }: { entity: RenderNode["entity"] }) {
  const battery = workerBatteryProgress(entity);
  return (
    <Html position={[0.72, 1.1, 0]} center transform={false} className="worker-world-battery-hud">
      <div className="worker-world-battery" aria-label="Battery">
        <span style={{ height: `${Math.round((battery ?? 0) * 100)}%` }} />
      </div>
    </Html>
  );
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

function WorkerModel({
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
  return (
    <group position={[position.x, 0, position.z]} rotation={[0, rotationY, 0]} onClick={(event) => stopSelect(event, entity.entity_id, onSelect)}>
      <SelectionRing selected={selected} width={1.4} depth={1.4} />
      <Block position={[0, 0.35, -0.18]} size={[0.25, 0.7, 0.25]} color="#263b54" />
      <Block position={[0, 0.35, 0.18]} size={[0.25, 0.7, 0.25]} color="#263b54" />
      <Block position={[0, 1.05, 0]} size={[0.72, 0.85, 0.46]} color={color} />
      <Block position={[0, 1.73, 0]} size={[0.58, 0.48, 0.52]} color="#eff7ff" />
      <Block position={[0, 1.76, 0.28]} size={[0.4, 0.12, 0.04]} color="#29a8ff" />
      <Block position={[-0.5, 1.08, 0.04]} size={[0.18, 0.72, 0.18]} color="#8ea6c0" />
      <Block position={[0.5, 1.08, 0.04]} size={[0.18, 0.72, 0.18]} color="#8ea6c0" />
      {cargoId && <Block position={[0.76, 1.02, 0.22]} size={[0.35, 0.35, 0.35]} color="#ffd166" />}
      <Label text={entity.label || entity.entity_id} position={{ x: 0, y: 2.35, z: 0 }} />
      <WorkerBatteryHud entity={entity} />
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
      {progress !== undefined && (
        <group position={[0, 1.21, rect.depth * 0.15]}>
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
  const footprint = footprintForEntity(grid, entity.entity_id);
  const rect = footprint ? mapper.footprintToWorldRect(footprint) : { center: mapper.pointToWorld(node.position), width: 4, depth: 2 };
  const queueSize = Number(entity.attributes.queue_size ?? entity.attributes.item_count ?? 0);
  const isMaterialSlot = entity.entity_type === "material_slot";
  const isShelf = entity.entity_type === "shelf";
  const occupied = Boolean(entity.attributes.occupied || entity.attributes.material_item_id);
  const baseColor = entity.entity_type === "charger" ? "#4fcf8b" : entity.entity_type === "buffer" ? "#75a7ff" : "#f4b642";
  return (
    <group position={[rect.center.x, 0, rect.center.z]} onClick={(event) => stopSelect(event, entity.entity_id, onSelect)}>
      <SelectionRing selected={selected} width={rect.width} depth={rect.depth} />
      <Block position={[0, isMaterialSlot ? 0.04 : 0.18, 0]} size={[rect.width, isMaterialSlot ? 0.08 : 0.36, rect.depth]} color={isMaterialSlot ? "#30435c" : "#2c3f58"} />
      {!isMaterialSlot && <Block position={[0, 0.42, 0]} size={[rect.width * 0.88, 0.1, rect.depth * 0.72]} color={isShelf ? "#52657d" : baseColor} />}
      {isMaterialSlot && occupied && (
        <Block position={[0, 0.32, 0]} size={[Math.max(0.24, rect.width * 0.78), 0.42, Math.max(0.24, rect.depth * 0.78)]} color={itemColor("material")} />
      )}
      {!isMaterialSlot && Array.from({ length: Math.min(8, Math.max(0, queueSize)) }).map((_, index) => {
        const x = -rect.width * 0.36 + (index % 4) * 0.55;
        const z = -rect.depth * 0.18 + Math.floor(index / 4) * 0.55;
        return <Block key={index} position={[x, 0.75, z]} size={[0.36, 0.36, 0.36]} color={itemColor(String(entity.attributes.item_type ?? entity.entity_type))} />;
      })}
      {!isMaterialSlot && !isShelf && <Label text={entity.label || entity.entity_id} position={{ x: 0, y: 1.05, z: 0 }} />}
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
  const color = itemColor(String(entity.attributes.item_type ?? entity.label));
  return (
    <group position={[position.x, 0, position.z]} onClick={(event) => stopSelect(event, entity.entity_id, onSelect)}>
      <SelectionRing selected={selected} width={0.8} depth={0.8} />
      <Block position={[0, 0.34, 0]} size={[0.58, 0.58, 0.58]} color={color} />
      <Label text={entity.label || entity.entity_id} position={{ x: 0, y: 0.92, z: 0 }} />
    </group>
  );
}

function EntityModels({
  renderModel,
  mapper,
  currentTime,
  selectedEntityId,
  onSelectEntity,
}: {
  renderModel: ReplayRenderModel;
  mapper: CoordinateMapper;
  currentTime: number;
  selectedEntityId?: string;
  onSelectEntity?: SelectHandler;
}) {
  const grid = renderModel.grid;
  const nodes = renderModel.nodes;

  return (
    <group>
      {nodes.map((node) => {
        const selected = node.entity.entity_id === selectedEntityId;
        if (node.entity.entity_type === "worker" || node.entity.entity_type === "robot" || node.entity.entity_type === "transporter") {
          return <WorkerModel key={node.entity.entity_id} node={node} mapper={mapper} currentTime={currentTime} selected={selected} onSelect={onSelectEntity} />;
        }
        if (node.entity.entity_type === "machine" || node.entity.entity_type === "workstation") {
          return <MachineModel key={node.entity.entity_id} node={node} grid={grid} mapper={mapper} currentTime={currentTime} selected={selected} onSelect={onSelectEntity} />;
        }
        if (
          node.entity.entity_type === "queue" ||
          node.entity.entity_type === "buffer" ||
          node.entity.entity_type === "storage" ||
          node.entity.entity_type === "charger" ||
          node.entity.entity_type === "shelf" ||
          node.entity.entity_type === "material_slot"
        ) {
          return <PlatformModel key={node.entity.entity_id} node={node} grid={grid} mapper={mapper} selected={selected} onSelect={onSelectEntity} />;
        }
        return <ItemModel key={node.entity.entity_id} node={node} mapper={mapper} selected={selected} onSelect={onSelectEntity} />;
      })}
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
      <GridShell renderModel={renderModel} mapper={mapper} />
      <MotionPathOverlay renderModel={renderModel} mapper={mapper} currentTime={currentTime} />
      <TrafficConflictOverlay currentEvent={currentEvent} mapper={mapper} />
      <EntityModels renderModel={renderModel} mapper={mapper} currentTime={currentTime} selectedEntityId={selectedEntityId} onSelectEntity={onSelectEntity} />
    </Canvas>
  );
}
