import type { BaseEntityState, QueueState, ResourceState } from "./entity";

export interface ReplayAnnotation {
  annotation_id: string;
  label: string;
  severity?: "info" | "warning" | "error";
}

export interface ReplaySnapshot {
  timestamp: number;
  entities: Record<string, BaseEntityState>;
  resources: Record<string, ResourceState>;
  queues: Record<string, QueueState>;
  annotations?: ReplayAnnotation[];
}

export interface ReplayCheckpoint {
  checkpoint_id: string;
  event_cursor: number;
  timestamp: number;
  snapshot: ReplaySnapshot;
}
