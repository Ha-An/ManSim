import type { BaseEntityState } from "../replay-core/types/entity";

export function humanoidStateValue(entity: BaseEntityState, key: string): string {
  const humanoidState = entity.attributes.humanoid_state;
  if (!humanoidState || typeof humanoidState !== "object") return "";
  const value = (humanoidState as Record<string, unknown>)[key];
  return typeof value === "string" ? value.toUpperCase() : "";
}

export function taskCode(entity: BaseEntityState): string {
  const recoveryTask = recoveryStepCode(entity, "task");
  if (recoveryTask) return `${recoveryTask} (RECOVERY)`;
  const active = humanoidStateValue(entity, "availability") !== "AVAILABLE";
  const parent = entity.attributes.current_parent_task_code;
  if (active && typeof parent === "string" && parent.trim()) return parent.trim().toUpperCase();
  const context = taskContext(entity);
  const value = context.task_code;
  if (typeof value === "string" && value.trim()) return value.trim().toUpperCase();
  if (!active) return "";
  const direct = entity.attributes.current_task_code;
  return typeof direct === "string" ? direct.trim().toUpperCase() : "";
}

export function childTaskCode(entity: BaseEntityState): string {
  if (activeRecoveryContext(entity)) return "";
  if (humanoidStateValue(entity, "availability") === "AVAILABLE") return "";
  const child = entity.attributes.current_child_task_code;
  if (typeof child === "string" && child.trim()) return child.trim().toUpperCase();
  return "";
}

export function primitiveCode(entity: BaseEntityState): string {
  const recoveryPrimitive = recoveryStepCode(entity, "primitive");
  if (recoveryPrimitive) return `${recoveryPrimitive} (RECOVERY)`;
  const context = taskContext(entity);
  const value = context.primitive_call_code;
  if (typeof value === "string" && value.trim()) return value.trim().toUpperCase();
  if (humanoidStateValue(entity, "availability") === "AVAILABLE") return "";
  const direct = entity.attributes.current_primitive_call_code;
  return typeof direct === "string" ? direct.trim().toUpperCase() : "";
}

export function taskContext(entity: BaseEntityState): Record<string, unknown> {
  const humanoidState = entity.attributes.humanoid_state;
  if (!humanoidState || typeof humanoidState !== "object") return {};
  const context = (humanoidState as Record<string, unknown>).task_context;
  return context && typeof context === "object" ? (context as Record<string, unknown>) : {};
}

function activeRecoveryContext(entity: BaseEntityState): Record<string, unknown> | null {
  const value = entity.attributes.current_recovery_context;
  if (!value || typeof value !== "object") return null;
  const context = value as Record<string, unknown>;
  return context.active === true ? context : null;
}

function recoveryStepCode(entity: BaseEntityState, kind: "task" | "primitive"): string {
  const context = activeRecoveryContext(entity);
  if (!context || String(context.step_kind || "").toLowerCase() !== kind) return "";
  return String(context.step_code || "").trim().toUpperCase();
}

export function cargoItemId(entity: BaseEntityState): string {
  const cargo = entity.attributes.cargo;
  if (cargo && typeof cargo === "object") {
    const row = cargo as Record<string, unknown>;
    const itemId = row.item_id;
    if (typeof itemId === "string" && itemId.trim()) return itemId.trim();
    const itemIds = row.item_ids;
    if (Array.isArray(itemIds)) {
      const firstId = itemIds.map(String).find((candidate) => candidate.trim());
      if (firstId) return firstId.trim();
    }
    const itemCount = Number(row.item_count);
    if (Number.isFinite(itemCount) && itemCount > 0) return `CARGO-${Math.round(itemCount)}`;
  }
  if (typeof entity.attributes.carrying_item_id === "string" && entity.attributes.carrying_item_id.trim()) {
    return entity.attributes.carrying_item_id.trim();
  }
  const carryingItemIds = entity.attributes.carrying_item_ids;
  if (Array.isArray(carryingItemIds)) {
    const firstId = carryingItemIds.map(String).find((candidate) => candidate.trim());
    if (firstId) return firstId.trim();
  }
  return "";
}

export function cargoItemType(entity: BaseEntityState): string {
  const cargo = entity.attributes.cargo;
  if (cargo && typeof cargo === "object") {
    const itemType = (cargo as Record<string, unknown>).item_type;
    if (typeof itemType === "string") return itemType.trim();
  }
  return typeof entity.attributes.carrying_item_type === "string" ? entity.attributes.carrying_item_type.trim() : "";
}

export function taskWindowProgress(entity: BaseEntityState, currentTime: number): number | undefined {
  const windowValue = entity.attributes.task_window;
  if (!windowValue || typeof windowValue !== "object") return undefined;
  const startedAt = Number((windowValue as Record<string, unknown>).started_at);
  const endedAt = Number((windowValue as Record<string, unknown>).ended_at);
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt) || endedAt <= startedAt) return undefined;
  return Math.max(0, Math.min(1, (currentTime - startedAt) / (endedAt - startedAt)));
}

export function machineProcessProgress(entity: BaseEntityState, currentTime: number): number | undefined {
  const machineState = typeof entity.attributes.machine_state === "string" ? entity.attributes.machine_state.toUpperCase() : "";
  if (!machineState.includes("PROCESS")) return undefined;
  const windowValue = entity.attributes.process_window;
  if (!windowValue || typeof windowValue !== "object") return undefined;
  const startedAt = Number((windowValue as Record<string, unknown>).started_at);
  const endedAt = Number((windowValue as Record<string, unknown>).ended_at);
  if (!Number.isFinite(startedAt) || !Number.isFinite(endedAt) || endedAt <= startedAt) return undefined;
  return Math.max(0, Math.min(1, (currentTime - startedAt) / (endedAt - startedAt)));
}

export function itemColor(itemType: string): string {
  const normalized = itemType.toLowerCase();
  if (normalized.includes("battery")) return "#ffd166";
  if (normalized.includes("product")) return "#63d471";
  if (normalized.includes("intermediate") || normalized.includes("transfer")) return "#69d2e7";
  if (normalized.includes("material")) return "#75a7ff";
  return "#cdd7e6";
}

export function workerColor(entity: BaseEntityState): string {
  const power = humanoidStateValue(entity, "power");
  const availability = humanoidStateValue(entity, "availability");
  const task = taskCode(entity);
  const primitive = primitiveCode(entity);
  if (availability === "DISABLED" || power === "DEPLETED") return "#ff6b7a";
  if (availability === "BLOCKED") return "#ff315a";
  if (task === "INSPECT_PRODUCT" && primitive === "EXECUTE_QUALITY_ACTION") return "#47b9ff";
  if ((task === "REPAIR_MACHINE" || task === "PREVENTIVE_MAINTENANCE") && primitive === "EXECUTE_MAINTENANCE_ACTION") return "#4fcf8b";
  if (task === "SETUP_MACHINE" && primitive === "EXECUTE_MACHINE_ACTION") return "#8e7dff";
  if (task === "UNLOAD_MACHINE" && primitive === "EXECUTE_MACHINE_ACTION") return "#ffb85e";
  return "#d9e6f7";
}

export function machineColor(entity: BaseEntityState): string {
  const state = typeof entity.attributes.machine_state === "string" ? entity.attributes.machine_state.toUpperCase() : "";
  if (state.includes("BROKEN") || state.includes("REPAIR")) return "#ff6b7a";
  if (state.includes("PROCESS")) return "#55cc8a";
  if (state.includes("SETUP")) return "#8e7dff";
  return "#536b8d";
}
