import type { BaseEntityState } from "../../core/types/entity";

export type WorkerSpriteVariant = "idle" | "walk" | "carry" | "setup" | "unload" | "fix" | "discharged";

export type WorkerVisualMode =
  | "discharged"
  | "setup"
  | "unload"
  | "fix"
  | "inspect"
  | "carry"
  | "moving"
  | "working"
  | "error"
  | "idle";

export interface WorkerVisualState {
  mode: WorkerVisualMode;
  spriteVariant: WorkerSpriteVariant;
  badgeText: string;
  badgeAccent: string;
  panelText: string;
  showCarryOverlay: boolean;
}

function taskKindOf(entity: Pick<BaseEntityState, "attributes">): string {
  const attributes = entity.attributes ?? {};
  return typeof attributes.task_kind === "string" ? attributes.task_kind.toUpperCase() : "";
}

function taskLabelOf(entity: Pick<BaseEntityState, "attributes">): string {
  const attributes = entity.attributes ?? {};
  return typeof attributes.task_label === "string" ? attributes.task_label : "";
}

function carryingItemTypeOf(entity: Pick<BaseEntityState, "attributes">): string {
  const attributes = entity.attributes ?? {};
  return typeof attributes.carrying_item_type === "string" ? attributes.carrying_item_type.trim() : "";
}

function isDischarged(entity: Pick<BaseEntityState, "state" | "attributes">): boolean {
  const attributes = entity.attributes ?? {};
  const batteryPct = Number(attributes.battery_pct);
  if (Number.isFinite(batteryPct) && batteryPct <= 0) return true;
  return entity.state === "error" && /discharg/i.test(taskLabelOf(entity));
}

export function deriveWorkerVisualMode(entity: Pick<BaseEntityState, "state" | "attributes">): WorkerVisualMode {
  if (isDischarged(entity)) return "discharged";

  const taskKind = taskKindOf(entity);
  const carryingItemType = carryingItemTypeOf(entity);

  // Physical motion takes precedence over logical task assignment.
  // The simulator logs AGENT_TASK_START before AGENT_MOVE_START for many tasks,
  // so a worker can already own "Unload S1M2" while still walking toward Station 1.
  if (entity.state === "moving") {
    return carryingItemType ? "carry" : "moving";
  }

  if (taskKind === "SETUP_MACHINE") return "setup";
  if (taskKind === "UNLOAD_MACHINE") return "unload";
  if (taskKind === "REPAIR_MACHINE" || taskKind === "PREVENTIVE_MAINTENANCE" || entity.state === "maintenance") return "fix";
  if (taskKind === "INSPECT_PRODUCT") return "inspect";

  if (carryingItemType) return "carry";
  if (entity.state === "working") return "working";
  if (entity.state === "error") return "error";
  return "idle";
}

export function getWorkerVisualState(entity: Pick<BaseEntityState, "state" | "attributes">): WorkerVisualState {
  const mode = deriveWorkerVisualMode(entity);

  switch (mode) {
    case "discharged":
      return {
        mode,
        spriteVariant: "discharged",
        badgeText: "LOW",
        badgeAccent: "#ff7189",
        panelText: "Battery Low",
        showCarryOverlay: false,
      };
    case "setup":
      return {
        mode,
        spriteVariant: "setup",
        badgeText: "SET",
        badgeAccent: "#8e7dff",
        panelText: "Setup",
        showCarryOverlay: false,
      };
    case "unload":
      return {
        mode,
        spriteVariant: "unload",
        badgeText: "UNL",
        badgeAccent: "#ffb85e",
        panelText: "Unload",
        showCarryOverlay: false,
      };
    case "fix":
      return {
        mode,
        spriteVariant: "fix",
        badgeText: "FIX",
        badgeAccent: "#4fcf8b",
        panelText: "Fix",
        showCarryOverlay: false,
      };
    case "inspect":
      return {
        mode,
        spriteVariant: "idle",
        badgeText: "INSP",
        badgeAccent: "#44b7ff",
        panelText: "Inspection",
        showCarryOverlay: false,
      };
    case "carry":
      return {
        mode,
        spriteVariant: "carry",
        badgeText: "CAR",
        badgeAccent: "#ffb85e",
        panelText: "Carrying",
        showCarryOverlay: true,
      };
    case "moving":
      return {
        mode,
        spriteVariant: "walk",
        badgeText: "MOV",
        badgeAccent: "#5ba3ff",
        panelText: "Moving",
        showCarryOverlay: false,
      };
    case "working":
      return {
        mode,
        spriteVariant: "idle",
        badgeText: "WRK",
        badgeAccent: "#4fcf8b",
        panelText: "Working",
        showCarryOverlay: false,
      };
    case "error":
      return {
        mode,
        spriteVariant: "idle",
        badgeText: "ERR",
        badgeAccent: "#ff7189",
        panelText: "Error",
        showCarryOverlay: false,
      };
    default:
      return {
        mode: "idle",
        spriteVariant: "idle",
        badgeText: "IDL",
        badgeAccent: "#7a92b8",
        panelText: "Idle",
        showCarryOverlay: false,
      };
  }
}
