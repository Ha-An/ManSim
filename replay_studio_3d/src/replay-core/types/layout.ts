import type { EntityType, XY } from "./entity";

export type LayoutSource = "log" | "config" | "auto";

export type LayoutRegionKind = "station" | "inspection" | "storage" | "battery" | "home" | "generic" | "dock" | "materials" | "paint" | "scrap";

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
  tile?: { x: number; y: number };
  footprint?: { x: number; y: number; width: number; height: number };
}

export interface LayoutGridObjectFootprint {
  object_id: string;
  object_type?: string;
  zone?: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LayoutGridConfig {
  width_tiles: number;
  height_tiles: number;
  tile_time_min?: number;
  walls?: Array<{ x: number; y: number }>;
  doors?: Array<{ x: number; y: number }>;
  cart_route_tiles?: Array<{ x: number; y: number }>;
  cart_parking_tiles?: Array<{ x: number; y: number }>;
  object_footprints?: LayoutGridObjectFootprint[];
  service_tiles?: Record<string, Array<{ x: number; y: number }>>;
}

export interface LayoutConfig {
  scenario_type?: string;
  source_priority?: LayoutSource[];
  regions?: LayoutRegionConfig[];
  nodes?: LayoutNodeConfig[];
  lanes?: Record<string, XY>;
  viewport?: { width: number; height: number };
  grid?: LayoutGridConfig;
}
