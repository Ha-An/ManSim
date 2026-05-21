import type { BaseEntityState, DomainState, EntityRelations, InteractionState, QueueState, ResourceState, XY } from "../types/entity";
import type { ReplayEvent } from "../types/event";
import type { ReplaySnapshot } from "../types/snapshot";

function cloneRelations(relations: EntityRelations): EntityRelations {
  return {
    ...relations,
    linked_ids: [...(relations.linked_ids ?? [])],
    holding_ids: [...(relations.holding_ids ?? [])],
  };
}

function cloneEntity(entity: BaseEntityState): BaseEntityState {
  return {
    ...entity,
    position: entity.position ? { ...entity.position } : undefined,
    attributes: { ...entity.attributes },
    relations: cloneRelations(entity.relations),
  };
}

function cloneQueue(queue: QueueState): QueueState {
  return { ...queue, item_ids: [...queue.item_ids] };
}

function cloneResource(resource: ResourceState): ResourceState {
  return { ...resource, holders: [...(resource.holders ?? [])] };
}

function cloneInteraction(interaction: InteractionState): InteractionState {
  return { ...interaction, related_ids: [...(interaction.related_ids ?? [])] };
}

function asXY(value: unknown): XY | undefined {
  if (!value || typeof value !== "object") return undefined;
  const candidate = value as Record<string, unknown>;
  if (typeof candidate.x !== "number" || typeof candidate.y !== "number") return undefined;
  return { x: candidate.x, y: candidate.y };
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function createDomainShell(time = 0): DomainState {
  return {
    entities: {},
    resources: {},
    queues: {},
    interactions: {},
    current_event_index: 0,
    current_time: time,
  };
}

export function snapshotToDomain(snapshot?: ReplaySnapshot): DomainState {
  if (!snapshot) return createDomainShell(0);

  const domain = createDomainShell(snapshot.timestamp);
  for (const [entityId, entity] of Object.entries(snapshot.entities)) domain.entities[entityId] = cloneEntity(entity);
  for (const [resourceId, resource] of Object.entries(snapshot.resources)) domain.resources[resourceId] = cloneResource(resource);
  for (const [queueId, queue] of Object.entries(snapshot.queues)) domain.queues[queueId] = cloneQueue(queue);
  return domain;
}

export function domainToSnapshot(domain: DomainState): ReplaySnapshot {
  return {
    timestamp: domain.current_time,
    entities: Object.fromEntries(Object.entries(domain.entities).map(([key, value]) => [key, cloneEntity(value)])),
    resources: Object.fromEntries(Object.entries(domain.resources).map(([key, value]) => [key, cloneResource(value)])),
    queues: Object.fromEntries(Object.entries(domain.queues).map(([key, value]) => [key, cloneQueue(value)])),
    annotations: [],
  };
}

function upsertEntity(next: DomainState, entityId: string, fallback: Partial<BaseEntityState> = {}): BaseEntityState {
  const existing = next.entities[entityId];
  if (existing) {
    const clone = cloneEntity(existing);
    next.entities[entityId] = clone;
    return clone;
  }

  const created: BaseEntityState = {
    entity_id: entityId,
    entity_type: (fallback.entity_type ?? "task") as BaseEntityState["entity_type"],
    state: (fallback.state ?? "idle") as BaseEntityState["state"],
    label: fallback.label ?? entityId,
    position: fallback.position ? { ...fallback.position } : undefined,
    attributes: { ...(fallback.attributes ?? {}) },
    relations: cloneRelations(fallback.relations ?? {}),
    updated_at: fallback.updated_at ?? next.current_time,
  };
  next.entities[entityId] = created;
  return created;
}

function upsertQueue(next: DomainState, queueId: string): QueueState {
  const existing = next.queues[queueId];
  if (existing) {
    const clone = cloneQueue(existing);
    next.queues[queueId] = clone;
    return clone;
  }
  const created: QueueState = { queue_id: queueId, item_ids: [], updated_at: next.current_time };
  next.queues[queueId] = created;
  return created;
}

function mergePayloadAttributes(entity: BaseEntityState, payload: Record<string, unknown>): void {
  const raw = payload.attributes;
  if (!raw || typeof raw !== "object") return;
  Object.assign(entity.attributes, raw as Record<string, unknown>);
}

function clearStaleMachineWaitAttributes(entity: BaseEntityState, rawAttributes: unknown): void {
  if (!rawAttributes || typeof rawAttributes !== "object") return;
  if (entity.entity_type !== "machine" && entity.entity_type !== "workstation") return;
  const raw = rawAttributes as Record<string, unknown>;
  const machineState = typeof raw.machine_state === "string" ? raw.machine_state.toUpperCase() : "";
  if (!machineState) return;
  // Machine item overlays mean "finished output waiting for unload" only.
  // WAIT_INPUT must not draw a pseudo item on the machine, because that looks
  // like a loaded or completed item in Replay Studio.
  if (machineState === "DONE_WAIT_UNLOAD") return;
  delete entity.attributes.wait_visual;
  delete entity.attributes.wait_item_kind;
}

function clearHumanoidTaskAttributes(entity: BaseEntityState): void {
  delete entity.attributes.active_task;
  delete entity.attributes.active_target_id;
  delete entity.attributes.task_label;
  delete entity.attributes.task_kind;
  delete entity.attributes.current_task_type;
  delete entity.attributes.current_task_code;
  delete entity.attributes.current_task_name;
  delete entity.attributes.current_task_instance_id;
  delete entity.attributes.current_parent_task_code;
  delete entity.attributes.current_parent_task_instance_id;
  delete entity.attributes.current_child_task_code;
  delete entity.attributes.current_child_task_name;
  delete entity.attributes.current_child_task_instance_id;
  delete entity.attributes.current_task_path;
  delete entity.attributes.current_task_depth;
  delete entity.attributes.current_step_id;
  delete entity.attributes.current_primitive_call_code;
  delete entity.attributes.current_execution_status;
}

function upsertResource(next: DomainState, resourceId: string): ResourceState {
  const existing = next.resources[resourceId];
  if (existing) {
    const clone = cloneResource(existing);
    next.resources[resourceId] = clone;
    return clone;
  }
  const created: ResourceState = { resource_id: resourceId, holders: [], updated_at: next.current_time };
  next.resources[resourceId] = created;
  return created;
}

function addInteraction(next: DomainState, interaction: InteractionState): void {
  next.interactions[interaction.interaction_id] = cloneInteraction(interaction);
}

function resolveInteractionId(event: ReplayEvent, prefix: string): string {
  const payloadId = typeof event.payload.interaction_id === "string" ? event.payload.interaction_id : undefined;
  return payloadId || `${prefix}:${event.event_id}`;
}

export function applyEvent(domain: DomainState, event: ReplayEvent): DomainState {
  const next: DomainState = {
    ...domain,
    entities: { ...domain.entities },
    resources: { ...domain.resources },
    queues: { ...domain.queues },
    interactions: { ...domain.interactions },
    current_event_id: event.event_id,
    current_event_index: event.sequence_index,
    current_time: event.timestamp,
  };

  const primaryId = event.entity_refs.primary;
  const sourceId = event.entity_refs.source;
  const targetId = event.entity_refs.target;
  const relatedIds = event.entity_refs.related ?? [];

  switch (event.event_type) {
    case "entity_created": {
      const entityId = primaryId || (typeof event.payload.entity_id === "string" ? event.payload.entity_id : undefined);
      if (!entityId) return next;
      next.entities[entityId] = {
        entity_id: entityId,
        entity_type: ((event.payload.entity_type as BaseEntityState["entity_type"]) ?? "task"),
        state: ((event.payload.state as BaseEntityState["state"]) ?? "idle"),
        label: typeof event.payload.label === "string" ? event.payload.label : entityId,
        position: asXY(event.payload.position),
        attributes: { ...(typeof event.payload.attributes === "object" && event.payload.attributes ? (event.payload.attributes as Record<string, unknown>) : {}) },
        relations: {
          parent_id: typeof event.payload.parent_id === "string" ? event.payload.parent_id : undefined,
          assigned_to: typeof event.payload.assigned_to === "string" ? event.payload.assigned_to : undefined,
          linked_ids: asStringArray(event.payload.linked_ids),
          queue_id: typeof event.payload.queue_id === "string" ? event.payload.queue_id : undefined,
          holding_ids: asStringArray(event.payload.holding_ids),
        },
        updated_at: event.timestamp,
      };
      return next;
    }
    case "entity_removed": {
      if (primaryId) delete next.entities[primaryId];
      return next;
    }
    case "entity_moved": {
      if (!primaryId) return next;
      const entity = upsertEntity(next, primaryId);
      const from = asXY(event.payload.from) ?? entity.position;
      const to = asXY(event.payload.to) ?? asXY(event.payload.position) ?? entity.position;
      if (from) entity.position = { ...from };
      entity.state = "moving";
      entity.updated_at = event.timestamp;
      entity.attributes.motion = {
        from,
        to,
        path: Array.isArray(event.payload.path) ? event.payload.path : undefined,
        display_path: Array.isArray(event.payload.display_path) ? event.payload.display_path : undefined,
        started_at: event.durative?.started_at ?? event.timestamp,
        ended_at: event.durative?.ended_at ?? event.timestamp,
      };
      return next;
    }
    case "state_changed": {
      if (!primaryId) return next;
      const entity = upsertEntity(next, primaryId);
      if (typeof event.payload.state === "string") entity.state = event.payload.state as BaseEntityState["state"];
      entity.position = asXY(event.payload.position) ?? entity.position;
      mergePayloadAttributes(entity, event.payload);
      const rawAttributes = event.payload.attributes;
      if (rawAttributes && typeof rawAttributes === "object" && (rawAttributes as Record<string, unknown>).motion === null) {
        delete entity.attributes.motion;
      }
      if (rawAttributes && typeof rawAttributes === "object" && (rawAttributes as Record<string, unknown>).task_window === null) {
        delete entity.attributes.task_window;
      }
      clearStaleMachineWaitAttributes(entity, rawAttributes);
      const humanoidState = rawAttributes && typeof rawAttributes === "object" ? (rawAttributes as Record<string, unknown>).humanoid_state : undefined;
      if (
        humanoidState &&
        typeof humanoidState === "object" &&
        (humanoidState as Record<string, unknown>).task_context === null
      ) {
        clearHumanoidTaskAttributes(entity);
      }
      entity.updated_at = event.timestamp;
      return next;
    }
    case "traffic_conflict_detected": {
      const relatedIdsForConflict = [primaryId, ...relatedIds].filter((id): id is string => Boolean(id));
      const primaryWorkerId =
        typeof event.payload.primary_worker_id === "string" ? event.payload.primary_worker_id : primaryId;
      const originalOtherWorkerId =
        typeof event.payload.other_worker_id === "string" ? event.payload.other_worker_id : undefined;
      const baseConflict = {
        conflict_id: event.payload.conflict_id,
        conflict_type: event.payload.conflict_type,
        severity: event.payload.severity,
        collision: event.payload.collision,
        primary_worker_id: primaryWorkerId,
        worker_ids: event.payload.worker_ids,
        tile: event.payload.tile,
        edge: event.payload.edge,
        other_edge: event.payload.other_edge,
        gap_min: event.payload.gap_min,
        time_window: event.payload.time_window,
      };
      for (const id of relatedIdsForConflict) {
        const entity = upsertEntity(next, id);
        const otherForEntity =
          id === primaryWorkerId
            ? originalOtherWorkerId
            : id === originalOtherWorkerId
              ? primaryWorkerId
              : originalOtherWorkerId;
        entity.attributes.last_traffic_conflict = {
          ...baseConflict,
          other_worker_id: otherForEntity,
        };
        entity.updated_at = event.timestamp;
      }
      return next;
    }
    case "task_assigned": {
      if (!targetId) return next;
      const entity = upsertEntity(next, targetId);
      entity.attributes.assigned_task = event.payload.task_id;
      entity.attributes.task_label = event.payload.task_label;
      entity.updated_at = event.timestamp;
      addInteraction(next, {
        interaction_id: resolveInteractionId(event, "assign"),
        type: "task_handoff",
        source_id: sourceId,
        target_id: targetId,
        related_ids: relatedIds,
        started_at: event.timestamp,
        ended_at: event.timestamp + 1,
        label: typeof event.payload.task_label === "string" ? event.payload.task_label : "Task assigned",
        severity: "info",
      });
      return next;
    }
    case "task_started": {
      if (!primaryId) return next;
      const entity = upsertEntity(next, primaryId);
      entity.state = "working";
      entity.attributes.active_task = event.payload.task_id;
      entity.attributes.active_target_id = targetId;
      entity.attributes.task_label = event.payload.task_label;
      delete entity.attributes.motion;
      entity.attributes.task_window = {
        started_at: event.durative?.started_at ?? event.timestamp,
        ended_at: (event.durative?.started_at ?? event.timestamp) + (event.durative?.expected_duration ?? 1),
      };
      mergePayloadAttributes(entity, event.payload);
      entity.updated_at = event.timestamp;
      return next;
    }
    case "task_finished": {
      if (!primaryId) return next;
      const entity = upsertEntity(next, primaryId);
      entity.state = ((event.payload.next_state as BaseEntityState["state"]) ?? "idle");
      delete entity.attributes.active_task;
      delete entity.attributes.active_target_id;
      delete entity.attributes.motion;
      delete entity.attributes.task_window;
      delete entity.attributes.task_label;
      delete entity.attributes.task_kind;
      delete entity.attributes.current_task_type;
      delete entity.attributes.current_task_code;
      delete entity.attributes.current_task_name;
      delete entity.attributes.current_task_instance_id;
      delete entity.attributes.current_step_id;
      delete entity.attributes.current_primitive_call_code;
      delete entity.attributes.task_role;
      mergePayloadAttributes(entity, event.payload);
      entity.updated_at = event.timestamp;
      return next;
    }
    case "queue_entered": {
      const queueId = (typeof event.payload.queue_id === "string" ? event.payload.queue_id : targetId) || primaryId;
      if (!queueId) return next;
      const queue = upsertQueue(next, queueId);
      const itemId = typeof event.payload.item_id === "string" ? event.payload.item_id : primaryId;
      if (itemId && !queue.item_ids.includes(itemId)) queue.item_ids.push(itemId);
      queue.updated_at = event.timestamp;
      const queueEntity = next.entities[queueId];
      if (queueEntity) {
        queueEntity.attributes.queue_size = queue.item_ids.length;
        queueEntity.updated_at = event.timestamp;
      }
      if (itemId && next.entities[itemId]) {
        const item = upsertEntity(next, itemId);
        item.relations.queue_id = queueId;
        item.state = "waiting";
        item.updated_at = event.timestamp;
      }
      return next;
    }
    case "queue_exited": {
      const queueId = (typeof event.payload.queue_id === "string" ? event.payload.queue_id : sourceId) || primaryId;
      if (!queueId) return next;
      const queue = upsertQueue(next, queueId);
      const itemId = typeof event.payload.item_id === "string" ? event.payload.item_id : targetId;
      if (itemId) {
        queue.item_ids = queue.item_ids.filter((candidate) => candidate !== itemId);
        if (next.entities[itemId]) {
          const item = upsertEntity(next, itemId);
          item.relations.queue_id = undefined;
          item.updated_at = event.timestamp;
        }
      }
      queue.updated_at = event.timestamp;
      const queueEntity = next.entities[queueId];
      if (queueEntity) {
        queueEntity.attributes.queue_size = queue.item_ids.length;
        queueEntity.updated_at = event.timestamp;
      }
      return next;
    }
    case "resource_seized": {
      if (!primaryId) return next;
      const resource = upsertResource(next, primaryId);
      resource.owner_id = sourceId || targetId;
      resource.holders = [...new Set([...(resource.holders ?? []), ...(sourceId ? [sourceId] : []), ...(targetId ? [targetId] : [])])];
      resource.updated_at = event.timestamp;
      return next;
    }
    case "resource_released": {
      if (!primaryId) return next;
      const resource = upsertResource(next, primaryId);
      resource.owner_id = undefined;
      resource.holders = [];
      resource.updated_at = event.timestamp;
      return next;
    }
    case "message_sent": {
      addInteraction(next, {
        interaction_id: resolveInteractionId(event, "message"),
        type: "message",
        source_id: sourceId,
        target_id: targetId,
        related_ids: relatedIds,
        started_at: event.timestamp,
        ended_at: event.timestamp + 1.2,
        label: typeof event.payload.message === "string" ? event.payload.message : "Message sent",
        severity: "info",
      });
      return next;
    }
    case "collaboration_started": {
      addInteraction(next, {
        interaction_id: resolveInteractionId(event, "collaboration"),
        type: "collaboration",
        source_id: sourceId,
        target_id: targetId,
        related_ids: relatedIds,
        started_at: event.timestamp,
        label: typeof event.payload.label === "string" ? event.payload.label : "Collaboration",
        severity: "info",
      });
      return next;
    }
    case "collaboration_finished": {
      const interactionId = resolveInteractionId(event, "collaboration");
      const interaction = next.interactions[interactionId];
      if (interaction) interaction.ended_at = event.timestamp;
      return next;
    }
    case "battery_low": {
      if (primaryId) {
        const entity = upsertEntity(next, primaryId);
        entity.attributes.battery_pct = event.payload.battery_pct;
        entity.updated_at = event.timestamp;
      }
      addInteraction(next, {
        interaction_id: resolveInteractionId(event, "battery-low"),
        type: "warning",
        source_id: primaryId,
        started_at: event.timestamp,
        ended_at: event.timestamp + 2,
        label: typeof event.payload.label === "string" ? event.payload.label : "Battery low",
        severity: "warning",
      });
      return next;
    }
    case "charging_started": {
      if (!primaryId) return next;
      const entity = upsertEntity(next, primaryId);
      entity.state = "charging";
      delete entity.attributes.motion;
      entity.attributes.task_label = "Charging";
      entity.attributes.task_window = {
        started_at: event.durative?.started_at ?? event.timestamp,
        ended_at: (event.durative?.started_at ?? event.timestamp) + (event.durative?.expected_duration ?? 1),
      };
      mergePayloadAttributes(entity, event.payload);
      entity.updated_at = event.timestamp;
      return next;
    }
    case "charging_finished": {
      if (!primaryId) return next;
      const entity = upsertEntity(next, primaryId);
      entity.state = "idle";
      entity.attributes.battery_pct = event.payload.battery_pct ?? 100;
      delete entity.attributes.active_target_id;
      delete entity.attributes.motion;
      delete entity.attributes.task_window;
      delete entity.attributes.task_label;
      mergePayloadAttributes(entity, event.payload);
      entity.updated_at = event.timestamp;
      return next;
    }
    case "maintenance_started": {
      if (!primaryId) return next;
      const entity = upsertEntity(next, primaryId);
      entity.state = "maintenance";
      delete entity.attributes.motion;
      entity.attributes.task_label = event.payload.label ?? "Maintenance";
      entity.attributes.task_window = {
        started_at: event.durative?.started_at ?? event.timestamp,
        ended_at: (event.durative?.started_at ?? event.timestamp) + (event.durative?.expected_duration ?? 1),
      };
      mergePayloadAttributes(entity, event.payload);
      entity.updated_at = event.timestamp;
      return next;
    }
    case "maintenance_finished": {
      if (!primaryId) return next;
      const entity = upsertEntity(next, primaryId);
      entity.state = "idle";
      delete entity.attributes.active_target_id;
      delete entity.attributes.motion;
      delete entity.attributes.task_window;
      delete entity.attributes.task_label;
      mergePayloadAttributes(entity, event.payload);
      entity.updated_at = event.timestamp;
      return next;
    }
    case "deadlock_detected":
    case "livelock_detected":
    case "bottleneck_detected":
    case "warning_raised":
    case "error_raised": {
      addInteraction(next, {
        interaction_id: resolveInteractionId(event, "warning"),
        type: "warning",
        source_id: primaryId,
        target_id: targetId,
        related_ids: relatedIds,
        started_at: event.timestamp,
        ended_at: event.timestamp + 4,
        label: typeof event.payload.label === "string" ? event.payload.label : event.event_type,
        severity: event.event_type === "error_raised" || event.event_type === "deadlock_detected" ? "error" : "warning",
      });
      return next;
    }
    default:
      return next;
  }
}
