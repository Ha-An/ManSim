export interface XY {
  x: number;
  y: number;
}

export type EntityType =
  | "worker"
  | "robot"
  | "machine"
  | "workstation"
  | "queue"
  | "transporter"
  | "order"
  | "task"
  | "charger"
  | "maintenance_station"
  | "storage"
  | "shelf"
  | "material_slot"
  | "buffer";

export type EntityStatus =
  | "idle"
  | "working"
  | "blocked"
  | "waiting"
  | "moving"
  | "charging"
  | "maintenance"
  | "error";

export interface EntityRelations {
  parent_id?: string;
  assigned_to?: string;
  linked_ids?: string[];
  queue_id?: string;
  holding_ids?: string[];
}

export interface BaseEntityState {
  entity_id: string;
  entity_type: EntityType;
  state: EntityStatus;
  label: string;
  position?: XY;
  attributes: Record<string, unknown>;
  relations: EntityRelations;
  updated_at: number;
}

export interface QueueState {
  queue_id: string;
  item_ids: string[];
  capacity?: number;
  updated_at: number;
}

export interface ResourceState {
  resource_id: string;
  owner_id?: string;
  holders?: string[];
  updated_at: number;
}

export interface InteractionState {
  interaction_id: string;
  type: "message" | "task_handoff" | "collaboration" | "warning" | "movement";
  source_id?: string;
  target_id?: string;
  related_ids?: string[];
  started_at: number;
  ended_at?: number;
  label?: string;
  severity?: "info" | "warning" | "error";
}

export interface DomainState {
  entities: Record<string, BaseEntityState>;
  resources: Record<string, ResourceState>;
  queues: Record<string, QueueState>;
  interactions: Record<string, InteractionState>;
  current_event_id?: string;
  current_event_index: number;
  current_time: number;
}
