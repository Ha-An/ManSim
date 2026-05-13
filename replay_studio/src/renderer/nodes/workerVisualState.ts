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

function humanoidStateValue(entity: Pick<BaseEntityState, "attributes">, nestedKey: string): string {
  const attributes = entity.attributes ?? {};
  const humanoidState = attributes.humanoid_state;
  if (humanoidState && typeof humanoidState === "object") {
    const nested = (humanoidState as Record<string, unknown>)[nestedKey];
    if (typeof nested === "string" && nested.trim()) return nested.trim().toUpperCase();
  }
  return "";
}

function humanoidTaskCode(entity: Pick<BaseEntityState, "attributes">): string {
  const humanoidState = entity.attributes.humanoid_state;
  if (humanoidState && typeof humanoidState === "object") {
    const taskContext = (humanoidState as Record<string, unknown>).task_context;
    if (taskContext && typeof taskContext === "object") {
      const taskCode = (taskContext as Record<string, unknown>).task_code;
      if (typeof taskCode === "string" && taskCode.trim()) return taskCode.trim().toUpperCase();
    }
    const availability = (humanoidState as Record<string, unknown>).availability;
    if (typeof availability === "string" && availability.trim().toUpperCase() === "AVAILABLE") return "";
  }
  const direct = entity.attributes.current_task_code;
  if (typeof direct === "string" && direct.trim()) return direct.trim().toUpperCase();
  return "";
}

function humanoidPrimitiveCode(entity: Pick<BaseEntityState, "attributes">): string {
  const humanoidState = entity.attributes.humanoid_state;
  if (humanoidState && typeof humanoidState === "object") {
    const taskContext = (humanoidState as Record<string, unknown>).task_context;
    if (taskContext && typeof taskContext === "object") {
      const primitive = (taskContext as Record<string, unknown>).primitive_call_code;
      if (typeof primitive === "string" && primitive.trim()) return primitive.trim().toUpperCase();
    }
  }
  return "";
}

function workerVisualModeFromState(entity: Pick<BaseEntityState, "attributes">): WorkerVisualMode {
  const availability = humanoidStateValue(entity, "availability");
  const mobility = humanoidStateValue(entity, "mobility");
  const power = humanoidStateValue(entity, "power");
  const manipulation = humanoidStateValue(entity, "manipulation");
  const taskCode = humanoidTaskCode(entity);
  const primitiveCode = humanoidPrimitiveCode(entity);
  const hasCargo = Boolean(cargoItemTypeOf(entity));

  if (availability === "DISABLED" || power === "DEPLETED") return "discharged";
  if (availability === "BLOCKED") return "error";
  if (mobility === "NAVIGATING" || mobility === "DOCKING") return hasCargo ? "carry" : "moving";
  if ((manipulation === "HOLDING" || manipulation === "PLACING") && hasCargo) return "carry";
  if (taskCode === "SETUP_MACHINE" && primitiveCode === "EXECUTE_MACHINE_ACTION") return "setup";
  if (taskCode === "UNLOAD_MACHINE" && primitiveCode === "EXECUTE_MACHINE_ACTION") return "unload";
  if ((taskCode === "REPAIR_MACHINE" || taskCode === "PREVENTIVE_MAINTENANCE") && primitiveCode === "EXECUTE_MAINTENANCE_ACTION") return "fix";
  if (taskCode === "INSPECT_PRODUCT" && primitiveCode === "EXECUTE_QUALITY_ACTION") return "inspect";
  if (availability === "EXECUTING") return "working";
  if (availability === "ASSIGNED" || availability === "WAITING" || availability === "AVAILABLE") return "idle";
  return "idle";
}

export function getWorkerVisualState(entity: Pick<BaseEntityState, "attributes">): WorkerVisualState {
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
        spriteVariant: "setup",
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
