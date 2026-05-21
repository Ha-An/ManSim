import { useMemo, useState } from "react";
import type { BaseEntityState, XY } from "../../core/types/entity";
import type { LayoutGridConfig } from "../../core/types/layout";
import type { RenderRegion } from "../../core/types/replay";
import { getWorkerSpriteThumbUrl } from "../../renderer/nodes/workerSpriteSheet";
import { getWorkerVisualState } from "../../renderer/nodes/workerVisualState";

type MonitorMode = "worker" | "machine" | "item";

interface EntityMonitorPanelProps {
  workers: BaseEntityState[];
  machines: BaseEntityState[];
  items: BaseEntityState[];
  regions: RenderRegion[];
  currentTime: number;
  grid?: LayoutGridConfig;
  viewport?: { width: number; height: number };
}

const DEFAULT_GRID = { width_tiles: 100, height_tiles: 70 };
const DEFAULT_VIEWPORT = { width: 1600, height: 960 };

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function progressFromWindow(windowValue: unknown, currentTime: number): number | undefined {
  if (!windowValue || typeof windowValue !== "object") return undefined;
  const startedAt = Number((windowValue as Record<string, unknown>).started_at);
  const endedAt = Number((windowValue as Record<string, unknown>).ended_at);
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt) || endedAt <= startedAt) return undefined;
  return clamp((currentTime - startedAt) / (endedAt - startedAt), 0, 1);
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

function progressPercent(value: number | undefined): string {
  if (value === undefined) return "-";
  return `${Math.round(value * 100)}%`;
}

function workerBatteryProgress(entity: BaseEntityState): number | undefined {
  const batteryPct = Number(entity.attributes.battery_pct);
  if (!Number.isFinite(batteryPct)) return undefined;
  return clamp(batteryPct / 100, 0, 1);
}

function workerTaskProgress(entity: BaseEntityState, currentTime: number): number | undefined {
  return progressFromWindow(entity.attributes.task_window, currentTime);
}

function machineProcessProgress(entity: BaseEntityState, currentTime: number): number | undefined {
  const machineState =
    typeof entity.attributes.machine_state === "string" ? entity.attributes.machine_state.trim().toUpperCase() : "";
  if (machineState !== "PROCESSING") return undefined;
  return progressFromWindow(entity.attributes.process_window, currentTime);
}

function cargoItemType(entity: BaseEntityState): string {
  const cargo = entity.attributes.cargo;
  if (cargo && typeof cargo === "object") {
    const itemType = (cargo as Record<string, unknown>).item_type;
    if (typeof itemType === "string" && itemType.trim()) return itemType.trim();
  }
  return typeof entity.attributes.carrying_item_type === "string" ? entity.attributes.carrying_item_type.trim() : "";
}

function cargoItemId(entity: BaseEntityState): string {
  const cargo = entity.attributes.cargo;
  if (cargo && typeof cargo === "object") {
    const itemId = (cargo as Record<string, unknown>).item_id;
    if (typeof itemId === "string" && itemId.trim()) return itemId.trim();
  }
  return typeof entity.attributes.carrying_item_id === "string" ? entity.attributes.carrying_item_id.trim() : "";
}

function cargoSharedCarry(entity: BaseEntityState): string {
  const cargo = entity.attributes.cargo;
  if (!cargo || typeof cargo !== "object") return "-";
  const row = cargo as Record<string, unknown>;
  const carrierCount = Number(row.carrier_count);
  const carrierIds = Array.isArray(row.carrier_ids) ? row.carrier_ids.map(String).filter(Boolean) : [];
  const multiplier = Number(row.effective_time_multiplier);
  if (!row.shared_carry && carrierIds.length <= 1) return "-";
  const carrierLabel = carrierIds.length ? carrierIds.join(" + ") : `${carrierCount || 1} carriers`;
  const multiplierLabel = Number.isFinite(multiplier) ? `, x${multiplier.toFixed(2)}` : "";
  return `${carrierLabel}${multiplierLabel}`;
}

function humanoidState(entity: BaseEntityState): Record<string, unknown> {
  const value = entity.attributes.humanoid_state;
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function humanoidTaskContext(entity: BaseEntityState): Record<string, unknown> {
  const context = humanoidState(entity).task_context;
  return context && typeof context === "object" ? (context as Record<string, unknown>) : {};
}

function humanoidStateValue(entity: BaseEntityState, nestedKey: string): string {
  const nested = humanoidState(entity)[nestedKey];
  return typeof nested === "string" && nested.trim() ? nested.trim() : "-";
}

function hasActiveHumanoidTask(entity: BaseEntityState): boolean {
  const availability = humanoidStateValue(entity, "availability").toUpperCase();
  const taskCode = humanoidTaskContext(entity).task_code;
  return availability !== "AVAILABLE" && typeof taskCode === "string" && taskCode.trim().length > 0;
}

function activeRecoveryContext(entity: BaseEntityState): Record<string, unknown> | null {
  const value = entity.attributes.current_recovery_context;
  if (!value || typeof value !== "object") return null;
  const context = value as Record<string, unknown>;
  return context.active === true ? context : null;
}

function recoveryStepLabel(entity: BaseEntityState, kind: "task" | "primitive"): string {
  const context = activeRecoveryContext(entity);
  if (!context || String(context.step_kind || "").toLowerCase() !== kind) return "";
  const code = String(context.step_code || "").trim();
  return code ? `${code} (RECOVERY)` : "";
}

function itemType(entity: BaseEntityState): string {
  return typeof entity.attributes.item_type === "string" ? entity.attributes.item_type.trim() : "";
}

function itemState(entity: BaseEntityState): string {
  return typeof entity.attributes.item_state === "string" ? entity.attributes.item_state.trim().toUpperCase() : "";
}

function itemIconSrc(itemTypeValue: string): string | null {
  const normalized = itemTypeValue.trim().toLowerCase();
  if (!normalized) return null;
  if (normalized.includes("battery")) return "/assets/battery.png";
  if (normalized.includes("product")) return "/assets/product.png";
  if (normalized.includes("intermediate") || normalized.includes("transfer")) return "/assets/intermediate.png";
  if (normalized.includes("material")) return "/assets/material.png";
  return null;
}

function machineThumbSrc(entity: BaseEntityState): string {
  const machineState =
    typeof entity.attributes.machine_state === "string" ? entity.attributes.machine_state.trim().toUpperCase() : "";
  if (machineState.includes("BROKEN") || machineState.includes("REPAIR") || machineState.includes("PM")) {
    return "/assets/facility_processed/Down.png";
  }
  if (machineState.includes("PROCESS")) {
    return "/assets/facility_processed/Processing.png";
  }
  return "/assets/facility_processed/Waiting.png";
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

function statusLabel(entity: BaseEntityState): string {
  if (typeof entity.attributes.machine_state === "string") return entity.attributes.machine_state;
  if (typeof entity.attributes.item_state === "string") return entity.attributes.item_state;
  return entity.state.toUpperCase();
}

function workerTaskCode(entity: BaseEntityState): string {
  const recoveryTask = recoveryStepLabel(entity, "task");
  if (recoveryTask) return recoveryTask;
  const activeTask = hasActiveHumanoidTask(entity);
  if (activeTask && typeof entity.attributes.current_parent_task_code === "string" && entity.attributes.current_parent_task_code.trim()) {
    return entity.attributes.current_parent_task_code.trim();
  }
  const taskCode = humanoidTaskContext(entity).task_code;
  if (typeof taskCode === "string" && taskCode.trim()) return taskCode.trim();
  if (activeTask && typeof entity.attributes.current_task_code === "string" && entity.attributes.current_task_code.trim()) {
    return entity.attributes.current_task_code.trim();
  }
  return "-";
}

function workerChildTask(entity: BaseEntityState): string {
  if (activeRecoveryContext(entity)) return "-";
  if (!hasActiveHumanoidTask(entity)) return "-";
  const childCode =
    typeof entity.attributes.current_child_task_code === "string" ? entity.attributes.current_child_task_code.trim() : "";
  if (childCode) return childCode;
  return "-";
}

function workerPrimitive(entity: BaseEntityState): string {
  const recoveryPrimitive = recoveryStepLabel(entity, "primitive");
  if (recoveryPrimitive) return recoveryPrimitive;
  const primitive = humanoidTaskContext(entity).primitive_call_code;
  if (typeof primitive === "string" && primitive.trim()) return primitive.trim();
  if (hasActiveHumanoidTask(entity) && typeof entity.attributes.current_primitive_call_code === "string" && entity.attributes.current_primitive_call_code.trim()) {
    return entity.attributes.current_primitive_call_code.trim();
  }
  return "-";
}

function asXY(value: unknown): XY | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Record<string, unknown>;
  const x = Number(candidate.x);
  const y = Number(candidate.y);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : undefined;
}

function motionPathPoints(motion: unknown): XY[] {
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

function motionDisplayPathPoints(motion: unknown): XY[] {
  if (!motion || typeof motion !== "object") return [];
  const displayPath = (motion as Record<string, unknown>).display_path;
  if (Array.isArray(displayPath)) {
    const path = displayPath.map(asXY).filter((point): point is XY => Boolean(point));
    if (path.length >= 2) return path;
  }
  return motionPathPoints(motion);
}

function samplePathPoint(points: XY[], progress: number): XY | undefined {
  if (points.length < 2) return undefined;
  const distances: number[] = [];
  let total = 0;
  for (let index = 1; index < points.length; index += 1) {
    const distance = Math.hypot(points[index].x - points[index - 1].x, points[index].y - points[index - 1].y);
    distances.push(distance);
    total += distance;
  }
  if (total <= 0) return undefined;
  const targetDistance = clamp(progress, 0, 1) * total;
  let walked = 0;
  for (let index = 1; index < points.length; index += 1) {
    const segment = distances[index - 1];
    if (segment <= 0) continue;
    if (walked + segment >= targetDistance) {
      const source = points[index - 1];
      const target = points[index];
      const local = (targetDistance - walked) / segment;
      return {
        x: source.x + (target.x - source.x) * local,
        y: source.y + (target.y - source.y) * local,
      };
    }
    walked += segment;
  }
  return points[points.length - 1];
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

function currentWorkerPoint(entity: BaseEntityState, currentTime: number): XY | undefined {
  const motion = entity.attributes.motion;
  const fallback = entity.position;
  if (!motion || typeof motion !== "object") return fallback;
  const payload = motion as Record<string, unknown>;
  const startedAt = Number(payload.started_at);
  const endedAt = Number(payload.ended_at);
  const isMoving =
    Number.isFinite(startedAt) &&
    Number.isFinite(endedAt) &&
    endedAt > startedAt &&
    currentTime >= startedAt &&
    currentTime < endedAt;
  if (!isMoving) return fallback;
  const path = motionPathPoints(payload);
  const progress = clamp((currentTime - startedAt) / (endedAt - startedAt), 0, 1);
  return samplePathPoint(path, progress) ?? fallback;
}

function workerMotionPath(
  entity: BaseEntityState,
  currentTime: number,
  grid: LayoutGridConfig | undefined,
  viewport: { width: number; height: number } | undefined,
): string {
  const tile = pointToTile(currentWorkerPoint(entity, currentTime), grid, viewport);
  const coord = formatTileCoord(tile);
  const motion = entity.attributes.motion;
  if (!motion || typeof motion !== "object") return `0 tiles ${coord}`;
  const payload = motion as Record<string, unknown>;
  const startedAt = Number(payload.started_at);
  const endedAt = Number(payload.ended_at);
  const isMoving =
    Number.isFinite(startedAt) &&
    Number.isFinite(endedAt) &&
    endedAt > startedAt &&
    currentTime >= startedAt &&
    currentTime < endedAt;
  if (!isMoving) return `0 tiles ${coord}`;
  const pathLength = motionDisplayPathPoints(payload).length;
  return `${pathLength >= 2 ? pathLength : 0} tiles ${coord}`;
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

function workerTrafficConflict(entity: BaseEntityState, currentTime: number): string {
  const conflict = entity.attributes.last_traffic_conflict;
  if (!conflict || typeof conflict !== "object") return "-";
  const payload = conflict as Record<string, unknown>;
  if (!trafficConflictIsActive(payload, currentTime)) return "-";
  const type = typeof payload.conflict_type === "string" ? payload.conflict_type : "TRAFFIC";
  const primary = typeof payload.primary_worker_id === "string" ? payload.primary_worker_id : "";
  let other = typeof payload.other_worker_id === "string" ? payload.other_worker_id : "";
  if (other === entity.entity_id) other = primary && primary !== entity.entity_id ? primary : "";
  const suffix = other ? ` with ${other}` : "";
  return `${type}${suffix}`;
}

function workerHumanoidIncident(entity: BaseEntityState): string {
  const incident = entity.attributes.last_humanoid_incident ?? entity.attributes.incident_bubble;
  const recoveryContext = activeRecoveryContext(entity);
  const recoveryIncidentCode =
    recoveryContext && typeof recoveryContext.incident_code === "string" && recoveryContext.incident_code.trim()
      ? recoveryContext.incident_code.trim()
      : "";
  const reason = humanoidState(entity).reason;
  const reasonMetadata = reason && typeof reason === "object" ? (reason as Record<string, unknown>).metadata : undefined;
  const reasonIncidentCode =
    reasonMetadata && typeof reasonMetadata === "object" && typeof (reasonMetadata as Record<string, unknown>).incident_code === "string"
      ? String((reasonMetadata as Record<string, unknown>).incident_code)
      : "";
  if (!incident || typeof incident !== "object") {
    return recoveryIncidentCode || reasonIncidentCode || "-";
  }
  const row = incident as Record<string, unknown>;
  const code = typeof row.code === "string" && row.code.trim() ? row.code.trim() : recoveryIncidentCode || reasonIncidentCode;
  return code || "-";
}

function machineActiveWorkers(entity: BaseEntityState): string {
  const activeWorkerIds = Array.isArray(entity.attributes.active_worker_ids)
    ? entity.attributes.active_worker_ids.filter((value): value is string => typeof value === "string" && value.trim().length > 0)
    : [];
  return activeWorkerIds.length ? activeWorkerIds.join(", ") : "-";
}

function itemRefLabel(entity: BaseEntityState): string {
  return typeof entity.attributes.ref === "string" && entity.attributes.ref.trim() ? entity.attributes.ref.trim() : "-";
}

function isCompletedProduct(entity: BaseEntityState): boolean {
  return itemType(entity).toLowerCase().includes("product") && itemState(entity) === "COMPLETED";
}

function shouldDisplayItem(entity: BaseEntityState): boolean {
  return Boolean(itemType(entity) || itemState(entity));
}

function itemStageGroup(entity: BaseEntityState): "queue" | "carried" | "loaded" | "completed" {
  const state = itemState(entity);
  if (state === "CARRIED_BY_WORKER") return "carried";
  if (state === "COMPLETED") return "completed";
  if (
    [
      "LOADED_ON_MACHINE",
      "PROCESSING",
      "WAITING_MACHINE_UNLOAD",
      "WAITING_INSPECTION",
      "INSPECTING",
      "WAITING_INSPECTION_OUTPUT",
    ].includes(state)
  ) {
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

function MonitorTabs({
  mode,
  setMode,
  counts,
}: {
  mode: MonitorMode;
  setMode: (mode: MonitorMode) => void;
  counts: Record<MonitorMode, number>;
}) {
  return (
    <div className="entity-monitor-tabs">
      {(["worker", "machine", "item"] as MonitorMode[]).map((tab) => (
        <button
          key={tab}
          type="button"
          className={`entity-monitor-tab ${mode === tab ? "active" : ""}`}
          onClick={() => setMode(tab)}
        >
          <span>{tab === "worker" ? "Worker" : tab === "machine" ? "Machine" : "Item"}</span>
          <span className="entity-monitor-tab-count">{counts[tab]}</span>
        </button>
      ))}
    </div>
  );
}

export function EntityMonitorPanel({ workers, machines, items, regions, currentTime, grid, viewport }: EntityMonitorPanelProps) {
  const [mode, setMode] = useState<MonitorMode>("worker");

  const groupedItems = useMemo(
    () => {
      const groups: Record<"queue" | "carried" | "loaded" | "completed", BaseEntityState[]> = {
        queue: [],
        carried: [],
        loaded: [],
        completed: [],
      };

      for (const item of items) {
        if (!shouldDisplayItem(item)) continue;
        groups[itemStageGroup(item)].push(item);
      }

      for (const key of Object.keys(groups) as Array<keyof typeof groups>) {
        groups[key].sort((left, right) => {
          const leftType = itemType(left);
          const rightType = itemType(right);
          if (leftType !== rightType) return leftType.localeCompare(rightType);
          return left.label.localeCompare(right.label);
        });
      }

      return groups;
    },
    [items],
  );

  const counts = {
    worker: workers.length,
    machine: machines.length,
    item: items.length,
  } satisfies Record<MonitorMode, number>;

  const title = mode === "worker" ? "Worker Monitor" : mode === "machine" ? "Machine Monitor" : "Item Monitor";

  return (
    <section className="panel-card worker-monitor-panel">
      <div className="entity-monitor-header">
        <h3>{title}</h3>
        <MonitorTabs mode={mode} setMode={setMode} counts={counts} />
      </div>

      {mode === "worker" ? (
        <div className="worker-monitor-list">
          {workers.map((worker) => {
            const visualState = getWorkerVisualState(worker);
            const batteryProgress = workerBatteryProgress(worker);
            const taskProgress = workerTaskProgress(worker, currentTime);
            const carryingItemType = cargoItemType(worker) || "-";
            const carryingId = cargoItemId(worker) || "-";
            const availability = humanoidStateValue(worker, "availability");
            const mobility = humanoidStateValue(worker, "mobility");
            const manipulation = humanoidStateValue(worker, "manipulation");
            const motionPath = workerMotionPath(worker, currentTime, grid, viewport);
            const trafficConflict = workerTrafficConflict(worker, currentTime);
            const sharedCarry = cargoSharedCarry(worker);
            const incident = workerHumanoidIncident(worker);
            return (
              <article className="worker-monitor-card" key={worker.entity_id}>
                <div className="worker-monitor-header">
                  <div className="worker-monitor-identity">
                    <div className="worker-monitor-thumb">
                      <img src={getWorkerSpriteThumbUrl(worker)} alt={worker.label} />
                    </div>
                    <div>
                      <div className="worker-monitor-name">{worker.label}</div>
                      <div className="worker-monitor-location">{regionLabel(worker, regions)}</div>
                    </div>
                  </div>
                  <div className={`worker-monitor-state state-${visualState.mode}`}>{visualState.panelText}</div>
                </div>

                <div className="worker-monitor-meter">
                  <div className="worker-monitor-label-row">
                    <span>BATTERY</span>
                    <span>{progressPercent(batteryProgress)}</span>
                  </div>
                  <div className="worker-monitor-bar">
                    <div
                      className={`worker-monitor-fill ${progressClass(batteryProgress, "battery")}`}
                      style={{ width: batteryProgress !== undefined ? `${Math.round(batteryProgress * 100)}%` : "0%" }}
                    />
                  </div>
                </div>

                <div className="worker-monitor-meter">
                  <div className="worker-monitor-label-row">
                    <span>TASK</span>
                    <span>{progressPercent(taskProgress)}</span>
                  </div>
                  <div className="worker-monitor-bar">
                    <div
                      className={`worker-monitor-fill ${progressClass(taskProgress, "task")}`}
                      style={{ width: taskProgress !== undefined ? `${Math.round(taskProgress * 100)}%` : "0%" }}
                    />
                  </div>
                </div>

                <div className="worker-monitor-grid">
                  <div>
                    <span className="worker-monitor-key">Availability</span>
                    <span className="worker-monitor-value">{availability}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Mobility</span>
                    <span className="worker-monitor-value">{mobility}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Manipulation</span>
                    <span className="worker-monitor-value">{manipulation}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Task</span>
                    <span className="worker-monitor-value">{workerTaskCode(worker)}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Child Task</span>
                    <span className="worker-monitor-value">{workerChildTask(worker)}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Primitive</span>
                    <span className="worker-monitor-value">{workerPrimitive(worker)}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Motion Path</span>
                    <span className="worker-monitor-value">{motionPath}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Traffic</span>
                    <span className="worker-monitor-value">{trafficConflict}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Incident</span>
                    <span className="worker-monitor-value">{incident}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Carry</span>
                    <span className="worker-monitor-value carry-value">
                      <span>{carryingId}</span>
                      <span>{carryingItemType !== "-" ? `(${carryingItemType})` : ""}</span>
                    </span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Shared Carry</span>
                    <span className="worker-monitor-value">{sharedCarry}</span>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      ) : null}

      {mode === "machine" ? (
        <div className="worker-monitor-list">
          {machines.map((machine) => {
            const processProgress = machineProcessProgress(machine, currentTime);
            return (
              <article className="worker-monitor-card" key={machine.entity_id}>
                <div className="worker-monitor-header">
                  <div className="worker-monitor-identity">
                    <div className="worker-monitor-thumb machine-thumb">
                      <img src={machineThumbSrc(machine)} alt={machine.label} />
                    </div>
                    <div>
                      <div className="worker-monitor-name">{machine.label}</div>
                      <div className="worker-monitor-location">{regionLabel(machine, regions)}</div>
                    </div>
                  </div>
                  <div className={`worker-monitor-state state-${machine.state}`}>{statusLabel(machine)}</div>
                </div>

                {processProgress !== undefined ? (
                  <div className="worker-monitor-meter">
                    <div className="worker-monitor-label-row">
                      <span>PROCESS</span>
                      <span>{progressPercent(processProgress)}</span>
                    </div>
                    <div className="worker-monitor-bar">
                      <div
                        className={`worker-monitor-fill ${progressClass(processProgress, "machine")}`}
                        style={{ width: `${Math.round(processProgress * 100)}%` }}
                      />
                    </div>
                  </div>
                ) : null}

                <div className="worker-monitor-grid">
                  <div>
                    <span className="worker-monitor-key">Machine State</span>
                    <span className="worker-monitor-value">{statusLabel(machine)}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Active Workers</span>
                    <span className="worker-monitor-value">{machineActiveWorkers(machine)}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Input Item</span>
                    <span className="worker-monitor-value">{String(machine.attributes.input_item_id ?? "-")}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Output Item</span>
                    <span className="worker-monitor-value">{String(machine.attributes.output_item_id ?? "-")}</span>
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      ) : null}

      {mode === "item" ? (
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
                    {groupedItems[stage].map((item) => {
                      const kind = itemType(item);
                      const icon = itemIconSrc(kind);
                      return (
                        <article className="worker-monitor-card" key={item.entity_id}>
                          <div className="worker-monitor-header">
                            <div className="worker-monitor-identity">
                              <div className="worker-monitor-thumb item-thumb">
                                {icon ? <img src={icon} alt={kind || item.label} /> : <span className="entity-monitor-fallback">IT</span>}
                              </div>
                              <div>
                                <div className="worker-monitor-name">{item.label}</div>
                                <div className="worker-monitor-location">{kind || "Unknown item"}</div>
                              </div>
                            </div>
                            <div className={`worker-monitor-state state-${item.state}`}>
                              {isCompletedProduct(item) ? "COMPLETED" : statusLabel(item)}
                            </div>
                          </div>

                          <div className="worker-monitor-grid">
                            <div>
                              <span className="worker-monitor-key">Item Type</span>
                              <span className="worker-monitor-value">{kind || "-"}</span>
                            </div>
                            <div>
                              <span className="worker-monitor-key">Stage</span>
                              <span className="worker-monitor-value">{itemStageTitle(stage)}</span>
                            </div>
                            <div>
                              <span className="worker-monitor-key">Reference</span>
                              <span className="worker-monitor-value">{itemRefLabel(item)}</span>
                            </div>
                          </div>
                        </article>
                      );
                    })}
                  </div>
                </section>
              ) : null,
            )
          ) : (
            <div className="entity-monitor-empty">No item state is active at the current replay time.</div>
          )}
        </div>
      ) : null}
    </section>
  );
}
