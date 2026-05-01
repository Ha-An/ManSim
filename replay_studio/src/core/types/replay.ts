import type { ReplayEvent } from "./event";
import type { DomainState, BaseEntityState, InteractionState, XY } from "./entity";
import type { LayoutConfig, LayoutRegionConfig } from "./layout";
import type { ReplayCheckpoint, ReplaySnapshot } from "./snapshot";

export interface ReplayMetadata {
  run_id: string;
  title?: string;
  domain: string;
  description?: string;
  created_at?: string;
  total_duration: number;
  time_unit: "seconds" | "minutes";
}

export interface ReplayLog {
  schema_version: "1.0";
  metadata: ReplayMetadata;
  layout?: LayoutConfig;
  initial_state?: ReplaySnapshot;
  events: ReplayEvent[];
  checkpoints?: ReplayCheckpoint[];
}

export interface RenderRegion {
  region_id: string;
  label: string;
  kind: LayoutRegionConfig["kind"];
  position: XY;
  size: { width: number; height: number };
  accent?: string;
  background?: string;
}

export interface RenderNode {
  entity: BaseEntityState;
  position: XY;
  selected: boolean;
  focused: boolean;
}

export interface RenderFlow {
  id: string;
  source_id?: string;
  target_id?: string;
  label?: string;
  kind?: InteractionState["type"];
  severity?: "info" | "warning" | "error";
  active: boolean;
}

export interface ReplayRenderModel {
  regions: RenderRegion[];
  nodes: RenderNode[];
  flows: RenderFlow[];
  selectedEntity?: BaseEntityState;
  activeInteractions: InteractionState[];
}

export interface ReplayFrameState {
  time: number;
  cursor: number;
  currentEvent?: ReplayEvent;
  domainState: DomainState;
  renderModel: ReplayRenderModel;
  matchingEventIndexes: number[];
}

export type EventPredicate = (event: ReplayEvent) => boolean;
