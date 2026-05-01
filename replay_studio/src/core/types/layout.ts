import type { EntityType, XY } from "./entity";

export type LayoutSource = "log" | "config" | "auto";

export type LayoutRegionKind = "station" | "inspection" | "storage" | "battery" | "home" | "generic";

export interface LayoutRegionConfig {
  region_id: string;
  label: string;
  kind?: LayoutRegionKind;
  position: XY;
  size: { width: number; height: number };
  accent?: string;
  background?: string;
}

export interface LayoutNodeConfig {
  entity_id: string;
  entity_type?: EntityType;
  position?: XY;
  size?: { width: number; height: number };
  lane?: string;
  region_id?: string;
  anchor?: XY;
}

export interface LayoutConfig {
  source_priority?: LayoutSource[];
  regions?: LayoutRegionConfig[];
  nodes?: LayoutNodeConfig[];
  lanes?: Record<string, XY>;
  viewport?: { width: number; height: number };
}
