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

function cargoItemTypeOf(entity: Pick<BaseEntityState, "attributes">): string {
  const attributes = entity.attributes ?? {};
  const cargo = attributes.cargo;
  if (cargo && typeof cargo === "object") {
    const itemType = (cargo as Record<string, unknown>).item_type;
    if (typeof itemType === "string") return itemType.trim();
  }
  return typeof attributes.carrying_item_type === "string" ? attributes.carrying_item_type.trim() : "";
}

function workerVisualModeFromState(entity: Pick<BaseEntityState, "state" | "attributes">): WorkerVisualMode {
  const workerState =
    typeof entity.attributes.worker_state === "string" ? entity.attributes.worker_state.trim().toUpperCase() : "";

  switch (workerState) {
    case "DISCHARGED":
      return "discharged";
    case "SETTING_UP_MACHINE":
      return "setup";
    case "UNLOADING_MACHINE":
      return "unload";
    case "REPAIRING_MACHINE":
    case "PREVENTIVE_MAINTENANCE":
      return "fix";
    case "INSPECTING_PRODUCT":
      return "inspect";
    case "MOVING":
      return cargoItemTypeOf(entity) ? "carry" : "moving";
    case "SUPPLYING_MATERIAL":
    case "TRANSFERRING_INTERMEDIATE":
    case "BATTERY_DELIVERING":
      return cargoItemTypeOf(entity) ? "carry" : "working";
    case "BATTERY_SWAPPING":
      return "working";
    case "WAITING":
      return "idle";
    case "IDLE":
      return "idle";
    default:
      break;
  }

  // Legacy log fallback only. New v1.1 logs should provide attributes.worker_state.
  if (entity.state === "moving") return cargoItemTypeOf(entity) ? "carry" : "moving";
  if (entity.state === "maintenance") return "fix";
  if (entity.state === "error") return "error";
  if (entity.state === "working") return "working";
  return "idle";
}

export function getWorkerVisualState(entity: Pick<BaseEntityState, "state" | "attributes">): WorkerVisualState {
  const mode = workerVisualModeFromState(entity);

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
