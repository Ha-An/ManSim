import type { BaseEntityState } from "../../core/types/entity";
import type { RenderRegion } from "../../core/types/replay";
import { getWorkerSpriteThumbUrl } from "../../renderer/nodes/workerSpriteSheet";
import { getWorkerVisualState } from "../../renderer/nodes/workerVisualState";

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

function workerRegionLabel(entity: BaseEntityState, regions: RenderRegion[]): string {
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

function carryingIconSrc(entity: BaseEntityState): string | null {
  const carryingItemType = typeof entity.attributes.carrying_item_type === "string" ? entity.attributes.carrying_item_type.trim().toLowerCase() : "";
  if (!carryingItemType) return null;
  if (carryingItemType.includes("battery")) return "/assets/battery.png";
  if (carryingItemType.includes("product")) return "/assets/product.png";
  if (carryingItemType.includes("intermediate") || carryingItemType.includes("transfer")) return "/assets/intermediate.png";
  if (carryingItemType.includes("material")) return "/assets/material.png";
  return null;
}

function progressClass(value: number | undefined, kind: "battery" | "task"): string {
  if (value === undefined) return `${kind} muted`;
  if (kind === "battery") {
    if (value <= 0.2) return "battery critical";
    if (value <= 0.45) return "battery warning";
    return "battery healthy";
  }
  if (value <= 0.2) return "task low";
  if (value <= 0.6) return "task medium";
  return "task high";
}

interface WorkerMonitorPanelProps {
  workers: BaseEntityState[];
  regions: RenderRegion[];
  currentTime: number;
}

export function WorkerMonitorPanel({ workers, regions, currentTime }: WorkerMonitorPanelProps) {
  return (
    <section className="panel-card worker-monitor-panel">
      <h3>Worker Monitor</h3>
      <div className="worker-monitor-list">
        {workers.map((worker) => {
          const visualState = getWorkerVisualState(worker);
          const batteryProgress = workerBatteryProgress(worker, currentTime);
          const taskProgress = workerTaskProgress(worker, currentTime);
          const taskLabel = typeof worker.attributes.task_label === "string" ? worker.attributes.task_label : "No active task";
          const carryingItemType =
            typeof worker.attributes.carrying_item_type === "string" && worker.attributes.carrying_item_type
              ? String(worker.attributes.carrying_item_type)
              : "-";
          const carryingIcon = carryingIconSrc(worker);
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
                    <div className="worker-monitor-location">{workerRegionLabel(worker, regions)}</div>
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
                  <span className="worker-monitor-value">{taskLabel}</span>
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
    </section>
  );
}
