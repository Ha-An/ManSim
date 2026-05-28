export type ReplayEventType =
  | "entity_created"
  | "entity_removed"
  | "entity_moved"
  | "state_changed"
  | "task_assigned"
  | "task_started"
  | "task_finished"
  | "queue_entered"
  | "queue_exited"
  | "resource_seized"
  | "resource_released"
  | "message_sent"
  | "collaboration_started"
  | "collaboration_finished"
  | "battery_low"
  | "charging_started"
  | "charging_finished"
  | "maintenance_started"
  | "maintenance_finished"
  | "traffic_conflict_detected"
  | "rolling_horizon_window_started"
  | "rolling_horizon_candidate_collected"
  | "rolling_horizon_dispatched"
  | "rolling_horizon_task_skipped"
  | "rolling_horizon_task_requeued"
  | "rolling_horizon_task_started"
  | "rolling_horizon_task_completed"
  | "deadlock_detected"
  | "livelock_detected"
  | "bottleneck_detected"
  | "warning_raised"
  | "error_raised";

export interface EventEntityRefs {
  primary?: string;
  related?: string[];
  source?: string;
  target?: string;
}

export interface DurativeEventMeta {
  started_at?: number;
  ended_at?: number;
  expected_duration?: number;
}

export interface ReplayEvent {
  event_id: string;
  sequence_index: number;
  timestamp: number;
  event_type: ReplayEventType;
  entity_refs: EventEntityRefs;
  durative?: DurativeEventMeta;
  payload: Record<string, unknown>;
}
