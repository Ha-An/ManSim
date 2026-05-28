import { useMemo, useState, type CSSProperties } from "react";
import { Canvas } from "@react-three/fiber";
import { OrthographicCamera } from "@react-three/drei";
import type { BaseEntityState, XY } from "../replay-core/types/entity";
import type { LayoutGridConfig } from "../replay-core/types/layout";
import type { RenderRegion } from "../replay-core/types/replay";
import { HumanoidBlockModel } from "../scene/blockModels";
import {
  cargoItemId,
  childTaskCode,
  childTaskLabel,
  humanoidStateValue,
  primitiveCode,
  taskCode,
  taskLabel,
  taskWindowProgress,
  workerColor,
} from "../scene/entityVisuals";
import { DEFAULT_GRID, DEFAULT_VIEWPORT, isMotionActive, motionDisplayPathPoints, motionPathPoints, samplePath } from "../scene/coordinates";

type MonitorMode = "worker" | "machine" | "item";

interface EntityMonitorPanelProps {
  workers: BaseEntityState[];
  machines: BaseEntityState[];
  items: BaseEntityState[];
  regions: RenderRegion[];
  currentTime: number;
  selectedEntity?: BaseEntityState;
  grid?: LayoutGridConfig;
  viewport?: { width: number; height: number };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function valueOrDash(value: unknown): string {
  if (value === undefined || value === null || value === "") return "-";
  return String(value);
}

function progressFromWindow(windowValue: unknown, currentTime: number): number | undefined {
  if (!windowValue || typeof windowValue !== "object") return undefined;
  const startedAt = Number((windowValue as Record<string, unknown>).started_at);
  const endedAt = Number((windowValue as Record<string, unknown>).ended_at);
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt) || endedAt <= startedAt) return undefined;
  return clamp((currentTime - startedAt) / (endedAt - startedAt), 0, 1);
}

function progressPercent(value: number | undefined): string {
  if (value === undefined) return "-";
  return `${Math.round(value * 100)}%`;
}

function batteryProgress(entity: BaseEntityState): number | undefined {
  const batteryPct = Number(entity.attributes.battery_pct);
  if (!Number.isFinite(batteryPct)) return undefined;
  return clamp(batteryPct / 100, 0, 1);
}

function progressClass(value: number | undefined, kind: "battery" | "task" | "machine"): string {
  if (value === undefined) return `${kind} muted`;
  if (kind === "battery") {
    if (value <= 0.2) return "battery critical";
    if (value <= 0.45) return "battery warning";
    return "battery healthy";
  }
  if (kind === "machine") {
    if (value <= 0.2) return "machine low";
    if (value <= 0.6) return "machine medium";
    return "machine high";
  }
  if (value <= 0.2) return "task low";
  if (value <= 0.6) return "task medium";
  return "task high";
}

function regionLabel(entity: BaseEntityState, regions: RenderRegion[]): string {
  const position = entity.position;
  if (!position) return "Unknown";
  const hit = regions.find(
    (region) =>
      position.x >= region.position.x &&
      position.x <= region.position.x + region.size.width &&
      position.y >= region.position.y &&
      position.y <= region.position.y + region.size.height,
  );
  return hit?.label ?? "Transit";
}

function humanoidTaskContext(entity: BaseEntityState): Record<string, unknown> {
  const value = entity.attributes.humanoid_state;
  if (!value || typeof value !== "object") return {};
  const context = (value as Record<string, unknown>).task_context;
  return context && typeof context === "object" ? (context as Record<string, unknown>) : {};
}

function workerIncident(entity: BaseEntityState): string {
  const incident = entity.attributes.last_humanoid_incident ?? entity.attributes.incident_bubble;
  const recovery = entity.attributes.current_recovery_context;
  const recoveryCode =
    recovery && typeof recovery === "object" && typeof (recovery as Record<string, unknown>).incident_code === "string"
      ? String((recovery as Record<string, unknown>).incident_code).trim()
      : "";
  const reason = (entity.attributes.humanoid_state as Record<string, unknown> | undefined)?.reason;
  const metadata = reason && typeof reason === "object" ? (reason as Record<string, unknown>).metadata : undefined;
  const reasonCode =
    metadata && typeof metadata === "object" && typeof (metadata as Record<string, unknown>).incident_code === "string"
      ? String((metadata as Record<string, unknown>).incident_code)
      : "";
  if (!incident || typeof incident !== "object") return recoveryCode || reasonCode || "-";
  const code = (incident as Record<string, unknown>).code;
  return typeof code === "string" && code.trim() ? code.trim() : recoveryCode || reasonCode || "-";
}

function cargoItemType(entity: BaseEntityState): string {
  const cargo = entity.attributes.cargo;
  if (cargo && typeof cargo === "object") {
    const itemType = (cargo as Record<string, unknown>).item_type;
    if (typeof itemType === "string" && itemType.trim()) return itemType.trim();
  }
  return typeof entity.attributes.carrying_item_type === "string" ? entity.attributes.carrying_item_type.trim() : "";
}

function sharedCarry(entity: BaseEntityState): string {
  const cargo = entity.attributes.cargo;
  if (!cargo || typeof cargo !== "object") return "-";
  const row = cargo as Record<string, unknown>;
  const carrierIds = Array.isArray(row.carrier_ids) ? row.carrier_ids.map(String).filter(Boolean) : [];
  if (!row.shared_carry && carrierIds.length <= 1) return "-";
  const multiplier = Number(row.effective_time_multiplier);
  const multiplierLabel = Number.isFinite(multiplier) ? `, x${multiplier.toFixed(2)}` : "";
  return `${carrierIds.length ? carrierIds.join(" + ") : valueOrDash(row.carrier_count)}${multiplierLabel}`;
}

function asXY(value: unknown): XY | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Record<string, unknown>;
  const x = Number(candidate.x);
  const y = Number(candidate.y);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : undefined;
}

function pointToTile(
  point: XY | undefined,
  grid: LayoutGridConfig | undefined,
  viewport: { width: number; height: number } | undefined,
): XY | undefined {
  if (!point) return undefined;
  const gridWidth = grid?.width_tiles ?? DEFAULT_GRID.width_tiles;
  const gridHeight = grid?.height_tiles ?? DEFAULT_GRID.height_tiles;
  const view = viewport ?? DEFAULT_VIEWPORT;
  if (gridWidth <= 0 || gridHeight <= 0 || view.width <= 0 || view.height <= 0) return undefined;
  const tileWidth = view.width / gridWidth;
  const tileHeight = view.height / gridHeight;
  return {
    x: clamp(Math.round(point.x / tileWidth - 0.5), 0, gridWidth - 1),
    y: clamp(Math.round(point.y / tileHeight - 0.5), 0, gridHeight - 1),
  };
}

function formatTileCoord(point: XY | undefined): string {
  return point ? `(${point.x}, ${point.y})` : "(-, -)";
}

function currentMotionPoint(entity: BaseEntityState, currentTime: number): XY | undefined {
  const motion = entity.attributes.motion;
  if (!isMotionActive(motion, currentTime)) return entity.position;
  const payload = motion as Record<string, unknown>;
  const startedAt = Number(payload.started_at);
  const endedAt = Number(payload.ended_at);
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt) || endedAt <= startedAt) return entity.position;
  return samplePath(motionPathPoints(motion), (currentTime - startedAt) / (endedAt - startedAt))?.point ?? entity.position;
}

function motionPathLabel(
  entity: BaseEntityState,
  currentTime: number,
  grid: LayoutGridConfig | undefined,
  viewport: { width: number; height: number } | undefined,
): string {
  const coord = formatTileCoord(pointToTile(currentMotionPoint(entity, currentTime), grid, viewport));
  const motion = entity.attributes.motion;
  if (!isMotionActive(motion, currentTime)) return `0 tiles ${coord}`;
  return `${Math.max(0, motionDisplayPathPoints(motion).length)} tiles ${coord}`;
}

function trafficConflictIsActive(conflict: Record<string, unknown>, currentTime: number): boolean {
  const timeWindow = conflict.time_window;
  if (!timeWindow || typeof timeWindow !== "object") return true;
  const payload = timeWindow as Record<string, unknown>;
  const startedAt = Number(payload.started_at);
  const endedAt = Number(payload.ended_at);
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt)) return true;
  return currentTime >= startedAt && currentTime <= endedAt;
}

function trafficConflict(entity: BaseEntityState, currentTime: number): string {
  const conflict = entity.attributes.last_traffic_conflict;
  if (!conflict || typeof conflict !== "object") return "-";
  const payload = conflict as Record<string, unknown>;
  if (!trafficConflictIsActive(payload, currentTime)) return "-";
  const type = valueOrDash(payload.conflict_type);
  const primary = typeof payload.primary_worker_id === "string" ? payload.primary_worker_id : "";
  let other = typeof payload.other_worker_id === "string" ? payload.other_worker_id : "";
  if (other === entity.entity_id) other = primary && primary !== entity.entity_id ? primary : "";
  return `${type}${other ? ` with ${other}` : ""}`;
}

function machineProgress(entity: BaseEntityState, currentTime: number): number | undefined {
  const machineState = typeof entity.attributes.machine_state === "string" ? entity.attributes.machine_state.trim().toUpperCase() : "";
  if (!machineState.includes("PROCESS")) return undefined;
  return progressFromWindow(entity.attributes.process_window, currentTime);
}

function statusLabel(entity: BaseEntityState): string {
  if (typeof entity.attributes.machine_state === "string") return entity.attributes.machine_state;
  if (typeof entity.attributes.item_state === "string") return entity.attributes.item_state;
  return entity.state.toUpperCase();
}

function machineActiveWorkers(entity: BaseEntityState): string {
  const activeWorkerIds = Array.isArray(entity.attributes.active_worker_ids)
    ? entity.attributes.active_worker_ids.filter((value): value is string => typeof value === "string" && value.trim().length > 0)
    : [];
  return activeWorkerIds.length ? activeWorkerIds.join(", ") : "-";
}

function itemType(entity: BaseEntityState): string {
  const value = typeof entity.attributes.item_type === "string" ? entity.attributes.item_type.trim() : "";
  return value.toLowerCase().startsWith("battery") ? "battery" : value;
}

function itemState(entity: BaseEntityState): string {
  return typeof entity.attributes.item_state === "string" ? entity.attributes.item_state.trim().toUpperCase() : "";
}

function itemLineage(entity: BaseEntityState, key: "source_material_ids" | "source_intermediate_ids" | "transformed_from_item_ids"): string {
  const value = entity.attributes[key];
  if (!Array.isArray(value)) return "-";
  const ids = value.map(String).map((item) => item.trim()).filter(Boolean);
  return ids.length ? ids.join(", ") : "-";
}

function itemStageGroup(entity: BaseEntityState): "queue" | "carried" | "loaded" | "completed" {
  const state = itemState(entity);
  if (state === "CARRIED_BY_WORKER") return "carried";
  if (state === "COMPLETED") return "completed";
  if (["LOADED_ON_MACHINE", "PROCESSING", "WAITING_MACHINE_UNLOAD", "WAITING_INSPECTION", "INSPECTING", "WAITING_INSPECTION_OUTPUT"].includes(state)) {
    return "loaded";
  }
  return "queue";
}

function itemStageTitle(stage: "queue" | "carried" | "loaded" | "completed"): string {
  switch (stage) {
    case "queue":
      return "Queue / Storage";
    case "carried":
      return "Carried";
    case "loaded":
      return "Loaded / Processing";
    case "completed":
      return "Completed Product";
  }
}

function workerPortraitAccent(entity: BaseEntityState): string {
  const palette = ["#7fd4ff", "#8e7dff", "#57d49b", "#ffcf63", "#ff9bc8", "#64e5d0"];
  const suffix = entity.entity_id.match(/\d+$/)?.[0];
  if (suffix) return palette[(Number(suffix) - 1) % palette.length];
  const hash = Array.from(entity.entity_id).reduce((sum, char) => sum + char.charCodeAt(0), 0);
  return palette[hash % palette.length];
}

function workerPortraitTone(entity: BaseEntityState): string {
  const availability = humanoidStateValue(entity, "availability");
  const power = humanoidStateValue(entity, "power");
  if (availability === "DISABLED" || power === "DEPLETED") return "disabled";
  if (availability === "BLOCKED") return "blocked";
  if (availability === "WAITING") return "waiting";
  if (availability === "EXECUTING") return "executing";
  if (availability === "ASSIGNED") return "assigned";
  return "available";
}

function WorkerPortraitModel({ worker, currentTime }: { worker: BaseEntityState; currentTime: number }) {
  const moving = isMotionActive(worker.attributes.motion, currentTime);
  const cargoId = cargoItemId(worker);
  const cargoType = cargoItemType(worker) || cargoId;
  const availability = humanoidStateValue(worker, "availability");
  const walkSwing = moving ? Math.sin(currentTime * 9.5) * 0.42 : 0;
  const workSwing = !moving && availability === "EXECUTING" ? Math.sin(currentTime * 8.5) * 0.34 : 0;
  const color = workerColor(worker);

  return (
    <group position={[0, -0.08, 0]} rotation={[0, 0, 0]}>
      <HumanoidBlockModel color={color} cargoId={cargoId} cargoType={cargoType} walkSwing={walkSwing} workSwing={workSwing} />
    </group>
  );
}

function WorkerPortrait({ worker, currentTime }: { worker: BaseEntityState; currentTime: number }) {
  const style = { "--portrait-accent": workerPortraitAccent(worker) } as CSSProperties;
  return (
    <div className={`worker-monitor-portrait ${workerPortraitTone(worker)}`} style={style} aria-label={`${worker.label} portrait`}>
      <Canvas className="worker-monitor-portrait-canvas" gl={{ antialias: true, alpha: true }} dpr={[1, 2]}>
        <ambientLight intensity={0.84} />
        <directionalLight position={[2.5, 4, 5]} intensity={1.2} />
        <OrthographicCamera makeDefault position={[0, 0.95, 5]} zoom={26} near={0.1} far={40} />
        <WorkerPortraitModel worker={worker} currentTime={currentTime} />
      </Canvas>
      <span className="worker-portrait-id">{worker.entity_id}</span>
    </div>
  );
}

function MonitorTabs({ mode, setMode, counts }: { mode: MonitorMode; setMode: (mode: MonitorMode) => void; counts: Record<MonitorMode, number> }) {
  return (
    <div className="entity-monitor-tabs">
      {(["worker", "machine", "item"] as MonitorMode[]).map((tab) => (
        <button key={tab} type="button" className={`entity-monitor-tab ${mode === tab ? "active" : ""}`} onClick={() => setMode(tab)}>
          <span>{tab === "worker" ? "Worker" : tab === "machine" ? "Machine" : "Item"}</span>
          <span className="entity-monitor-tab-count">{counts[tab]}</span>
        </button>
      ))}
    </div>
  );
}

function Meter({ label, value, kind }: { label: string; value: number | undefined; kind: "battery" | "task" | "machine" }) {
  return (
    <div className="worker-monitor-meter">
      <div className="worker-monitor-label-row">
        <span>{label}</span>
        <span>{progressPercent(value)}</span>
      </div>
      <div className="worker-monitor-bar">
        <div className={`worker-monitor-fill ${progressClass(value, kind)}`} style={{ width: value !== undefined ? `${Math.round(value * 100)}%` : "0%" }} />
      </div>
    </div>
  );
}

export function EntityMonitorPanel({ workers, machines, items, regions, currentTime, selectedEntity, grid, viewport }: EntityMonitorPanelProps) {
  const [mode, setMode] = useState<MonitorMode>("worker");
  const groupedItems = useMemo(() => {
    const groups: Record<"queue" | "carried" | "loaded" | "completed", BaseEntityState[]> = {
      queue: [],
      carried: [],
      loaded: [],
      completed: [],
    };
    for (const item of items) groups[itemStageGroup(item)].push(item);
    for (const key of Object.keys(groups) as Array<keyof typeof groups>) {
      groups[key].sort((left, right) => left.label.localeCompare(right.label));
    }
    return groups;
  }, [items]);

  const counts = { worker: workers.length, machine: machines.length, item: items.length } satisfies Record<MonitorMode, number>;
  const title = mode === "worker" ? "Worker Monitor" : mode === "machine" ? "Machine Monitor" : "Item Monitor";

  return (
    <section className="panel-card worker-monitor-panel">
      <div className="entity-monitor-header">
        <h3>{title}</h3>
        <MonitorTabs mode={mode} setMode={setMode} counts={counts} />
      </div>

      {selectedEntity && (
        <div className="selected-entity-strip">
          <span>Selected</span>
          <strong>{selectedEntity.label || selectedEntity.entity_id}</strong>
          <em>{selectedEntity.entity_type}</em>
        </div>
      )}

      {mode === "worker" && (
        <div className="worker-monitor-list">
          {workers.map((worker) => {
            const availability = humanoidStateValue(worker, "availability") || "-";
            const mobility = humanoidStateValue(worker, "mobility") || "-";
            const manipulation = humanoidStateValue(worker, "manipulation") || "-";
            const context = humanoidTaskContext(worker);
            return (
              <article className={`worker-monitor-card ${selectedEntity?.entity_id === worker.entity_id ? "selected" : ""}`} key={worker.entity_id}>
                <div className="worker-monitor-header">
                  <div className="worker-monitor-identity">
                    <WorkerPortrait worker={worker} currentTime={currentTime} />
                    <div>
                      <div className="worker-monitor-name">{worker.label}</div>
                      <div className="worker-monitor-location">{regionLabel(worker, regions)}</div>
                    </div>
                  </div>
                  <div className="worker-monitor-state">{availability}</div>
                </div>
                <Meter label="BATTERY" value={batteryProgress(worker)} kind="battery" />
                <Meter label="TASK" value={taskWindowProgress(worker, currentTime)} kind="task" />
                <div className="worker-monitor-grid">
                  <div><span className="worker-monitor-key">Availability</span><span className="worker-monitor-value">{availability}</span></div>
                  <div><span className="worker-monitor-key">Mobility</span><span className="worker-monitor-value">{mobility}</span></div>
                  <div><span className="worker-monitor-key">Manipulation</span><span className="worker-monitor-value">{manipulation}</span></div>
                  <div><span className="worker-monitor-key">Task</span><span className="worker-monitor-value">{valueOrDash(taskLabel(worker) || taskCode(worker) || context.task_code)}</span></div>
                  <div><span className="worker-monitor-key">Child Task</span><span className="worker-monitor-value">{valueOrDash(childTaskLabel(worker) || childTaskCode(worker))}</span></div>
                  <div><span className="worker-monitor-key">Primitive</span><span className="worker-monitor-value">{valueOrDash(primitiveCode(worker))}</span></div>
                  <div><span className="worker-monitor-key">Motion Path</span><span className="worker-monitor-value">{motionPathLabel(worker, currentTime, grid, viewport)}</span></div>
                  <div><span className="worker-monitor-key">Traffic</span><span className="worker-monitor-value">{trafficConflict(worker, currentTime)}</span></div>
                  <div><span className="worker-monitor-key">Incident</span><span className="worker-monitor-value">{workerIncident(worker)}</span></div>
                  <div><span className="worker-monitor-key">Carry</span><span className="worker-monitor-value">{cargoItemId(worker) || "-"} {cargoItemType(worker) ? `(${cargoItemType(worker)})` : ""}</span></div>
                  <div><span className="worker-monitor-key">Shared Carry</span><span className="worker-monitor-value">{sharedCarry(worker)}</span></div>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {mode === "machine" && (
        <div className="worker-monitor-list">
          {machines.map((machine) => {
            const progress = machineProgress(machine, currentTime);
            return (
              <article className={`worker-monitor-card ${selectedEntity?.entity_id === machine.entity_id ? "selected" : ""}`} key={machine.entity_id}>
                <div className="worker-monitor-header">
                  <div className="worker-monitor-identity">
                    <div className="worker-monitor-thumb machine-thumb">MC</div>
                    <div>
                      <div className="worker-monitor-name">{machine.label}</div>
                      <div className="worker-monitor-location">{regionLabel(machine, regions)}</div>
                    </div>
                  </div>
                  <div className="worker-monitor-state">{statusLabel(machine)}</div>
                </div>
                {progress !== undefined && <Meter label="PROCESS" value={progress} kind="machine" />}
                <div className="worker-monitor-grid">
                  <div><span className="worker-monitor-key">Machine State</span><span className="worker-monitor-value">{statusLabel(machine)}</span></div>
                  <div><span className="worker-monitor-key">Active Workers</span><span className="worker-monitor-value">{machineActiveWorkers(machine)}</span></div>
                  <div><span className="worker-monitor-key">Input Item</span><span className="worker-monitor-value">{String(machine.attributes.input_item_id ?? "-")}</span></div>
                  <div><span className="worker-monitor-key">Output Item</span><span className="worker-monitor-value">{String(machine.attributes.output_item_id ?? "-")}</span></div>
                </div>
              </article>
            );
          })}
        </div>
      )}

      {mode === "item" && (
        <div className="worker-monitor-list">
          {Object.values(groupedItems).some((entries) => entries.length > 0) ? (
            (["queue", "carried", "loaded", "completed"] as const).map((stage) =>
              groupedItems[stage].length ? (
                <section className="entity-item-group" key={stage}>
                  <div className="entity-item-group-title">
                    <span>{itemStageTitle(stage)}</span>
                    <span className="entity-item-group-count">{groupedItems[stage].length}</span>
                  </div>
                  <div className="worker-monitor-list">
                    {groupedItems[stage].map((item) => (
                      <article className={`worker-monitor-card ${selectedEntity?.entity_id === item.entity_id ? "selected" : ""}`} key={item.entity_id}>
                        <div className="worker-monitor-header">
                          <div className="worker-monitor-identity">
                            <div className="worker-monitor-thumb item-thumb">IT</div>
                            <div>
                              <div className="worker-monitor-name">{item.label}</div>
                              <div className="worker-monitor-location">{itemType(item) || "Unknown item"}</div>
                            </div>
                          </div>
                          <div className="worker-monitor-state">{statusLabel(item)}</div>
                        </div>
                        <div className="worker-monitor-grid">
                          <div><span className="worker-monitor-key">Item Type</span><span className="worker-monitor-value">{itemType(item) || "-"}</span></div>
                          <div><span className="worker-monitor-key">Stage</span><span className="worker-monitor-value">{itemStageTitle(stage)}</span></div>
                          <div><span className="worker-monitor-key">Reference</span><span className="worker-monitor-value">{String(item.attributes.ref ?? "-")}</span></div>
                          <div><span className="worker-monitor-key">From Material</span><span className="worker-monitor-value">{itemLineage(item, "source_material_ids")}</span></div>
                          <div><span className="worker-monitor-key">From Intermediate</span><span className="worker-monitor-value">{itemLineage(item, "source_intermediate_ids")}</span></div>
                          <div><span className="worker-monitor-key">Transformed From</span><span className="worker-monitor-value">{itemLineage(item, "transformed_from_item_ids")}</span></div>
                        </div>
                      </article>
                    ))}
                  </div>
                </section>
              ) : null,
            )
          ) : (
            <div className="entity-monitor-empty">No item state is active at the current replay time.</div>
          )}
        </div>
      )}
    </section>
  );
}
