import { useMemo, useState } from "react";
import type { BaseEntityState } from "../../core/types/entity";
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
}

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

function workerBatteryProgress(entity: BaseEntityState, currentTime: number): number | undefined {
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

function workerTaskProgress(entity: BaseEntityState, currentTime: number): number | undefined {
  const taskWindowProgress = progressFromWindow(entity.attributes.task_window, currentTime);
  if (taskWindowProgress !== undefined) return taskWindowProgress;
  const motionProgress = progressFromWindow(entity.attributes.motion, currentTime);
  if (entity.state === "moving" && motionProgress !== undefined) return motionProgress;
  return undefined;
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
  if (typeof entity.attributes.worker_state === "string") return entity.attributes.worker_state;
  if (typeof entity.attributes.machine_state === "string") return entity.attributes.machine_state;
  if (typeof entity.attributes.item_state === "string") return entity.attributes.item_state;
  return entity.state.toUpperCase();
}

function workerTaskLabel(entity: BaseEntityState): string {
  if (typeof entity.attributes.task_label === "string" && entity.attributes.task_label.trim()) return entity.attributes.task_label;
  if (typeof entity.attributes.current_task_type === "string" && entity.attributes.current_task_type.trim()) return entity.attributes.current_task_type;
  return "No active task";
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
  const kind = itemType(entity).toLowerCase();
  const state = itemState(entity);

  if (!kind || !state) return false;

  if (kind.includes("product")) {
    return state !== "SCRAPPED";
  }

  if (kind.includes("battery")) {
    return ["CREATED", "IN_STORAGE", "IN_QUEUE", "CARRIED_BY_WORKER"].includes(state);
  }

  if (kind.includes("material") || kind.includes("intermediate")) {
    return ["CREATED", "IN_STORAGE", "IN_QUEUE", "CARRIED_BY_WORKER"].includes(state);
  }

  return !["COMPLETED", "SCRAPPED"].includes(state);
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

export function EntityMonitorPanel({ workers, machines, items, regions, currentTime }: EntityMonitorPanelProps) {
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
            const batteryProgress = workerBatteryProgress(worker, currentTime);
            const taskProgress = workerTaskProgress(worker, currentTime);
            const carryingItemType = cargoItemType(worker) || "-";
            const carryingIcon = itemIconSrc(carryingItemType);
            const lastSwapAt = Number(worker.attributes.last_swap_at);
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
                  <div className={`worker-monitor-state state-${worker.state}`}>{visualState.panelText}</div>
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
                    <span className="worker-monitor-key">Task</span>
                    <span className="worker-monitor-value">{workerTaskLabel(worker)}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Carry</span>
                    <span className="worker-monitor-value carry-value">
                      {carryingIcon ? (
                        <span className="worker-monitor-carry-chip">
                          <img className="worker-monitor-carry-icon" src={carryingIcon} alt={carryingItemType} />
                        </span>
                      ) : null}
                      <span>{carryingItemType}</span>
                    </span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">State</span>
                    <span className="worker-monitor-value">{statusLabel(worker)}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Last Swap</span>
                    <span className="worker-monitor-value">{Number.isFinite(lastSwapAt) ? `${lastSwapAt.toFixed(1)} min` : "-"}</span>
                  </div>
                  <div>
                    <span className="worker-monitor-key">Updated</span>
                    <span className="worker-monitor-value">{worker.updated_at.toFixed(2)}</span>
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
                  <div>
                    <span className="worker-monitor-key">Updated</span>
                    <span className="worker-monitor-value">{machine.updated_at.toFixed(2)}</span>
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
                            <div>
                              <span className="worker-monitor-key">Updated</span>
                              <span className="worker-monitor-value">{item.updated_at.toFixed(2)}</span>
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
