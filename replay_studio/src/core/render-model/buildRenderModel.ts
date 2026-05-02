import type { BaseEntityState, DomainState } from "../types/entity";
import type { ReplayEvent } from "../types/event";
import type { LayoutConfig } from "../types/layout";
import type { ReplayRenderModel, RenderFlow, RenderNode, RenderRegion } from "../types/replay";
import { resolveLayout } from "../layout/layoutResolver";
import { getActiveInteractions, getFocusedEntityIds } from "../replay/selectors";

interface MotionMeta {
  from?: { x: number; y: number };
  to?: { x: number; y: number };
  started_at?: number;
  ended_at?: number;
}

export interface BuildRenderModelOptions {
  logLayout?: LayoutConfig;
  externalLayout?: LayoutConfig;
  currentEvent?: ReplayEvent;
  selectedEntityId?: string;
  followSelected?: boolean;
  visibleEntityTypes?: string[];
  entityIdFilter?: string;
  searchQuery?: string;
}

function createVirtualOutputQueues(domainState: DomainState): BaseEntityState[] {
  const virtualQueues: BaseEntityState[] = [];
  const candidates = [
    {
      entity_id: "station_1_output_queue",
      label: "S1 Output Queue",
      region_id: "station_1_region",
      derived_from_queue: "output_buffer_station_1",
    },
    {
      entity_id: "station_2_output_queue",
      label: "S2 Output Queue",
      region_id: "station_2_region",
      derived_from_queue: "output_buffer_station_2",
    },
    {
      entity_id: "inspection_output_queue",
      label: "Inspection Output",
      region_id: "inspection_region",
      derived_from_queue: "output_buffer_station_4",
    },
  ];

  for (const candidate of candidates) {
    if (domainState.entities[candidate.entity_id]) continue;
    virtualQueues.push({
      entity_id: candidate.entity_id,
      entity_type: "buffer",
      state: "waiting",
      label: candidate.label,
      attributes: {
        queue_size: 0,
        virtual: true,
        queue_kind: "output",
        region_id: candidate.region_id,
        derived_from_queue: candidate.derived_from_queue,
      },
      relations: {},
      updated_at: domainState.current_time,
    });
  }

  return virtualQueues;
}

function workerMachineTaskPosition(
  entity: BaseEntityState,
  basePosition: { x: number; y: number },
  entities: Record<string, BaseEntityState>,
  resolvedPositions: Record<string, { x: number; y: number }>,
): { x: number; y: number } {
  if (entity.entity_type !== "worker" && entity.entity_type !== "robot" && entity.entity_type !== "transporter") {
    return basePosition;
  }

  if (entity.state === "moving") return basePosition;

  const workerState = typeof entity.attributes.worker_state === "string" ? entity.attributes.worker_state.toUpperCase() : "";
  const taskKind = typeof entity.attributes.current_task_type === "string" ? entity.attributes.current_task_type.toUpperCase() : "";
  const effectiveTaskKind =
    workerState === "SETTING_UP_MACHINE"
      ? "SETUP_MACHINE"
      : workerState === "UNLOADING_MACHINE"
        ? "UNLOAD_MACHINE"
        : workerState === "REPAIRING_MACHINE"
          ? "REPAIR_MACHINE"
          : workerState === "PREVENTIVE_MAINTENANCE"
            ? "PREVENTIVE_MAINTENANCE"
            : taskKind;
  if (!["SETUP_MACHINE", "UNLOAD_MACHINE", "REPAIR_MACHINE", "PREVENTIVE_MAINTENANCE"].includes(effectiveTaskKind)) {
    return basePosition;
  }

  const targetId = typeof entity.attributes.active_target_id === "string" ? entity.attributes.active_target_id : "";
  if (!targetId) return basePosition;

  const targetPosition = resolvedPositions[targetId];
  if (!targetPosition) return basePosition;

  const cargo = entity.attributes.cargo;
  const cargoType =
    cargo && typeof cargo === "object" && typeof (cargo as Record<string, unknown>).item_type === "string"
      ? String((cargo as Record<string, unknown>).item_type)
      : typeof entity.attributes.carrying_item_type === "string"
        ? entity.attributes.carrying_item_type
        : "";
  const carried = cargoType.trim().length > 0;
  let offset = { x: -54, y: 6 };
  if (effectiveTaskKind === "SETUP_MACHINE") {
    offset = carried ? { x: -46, y: 8 } : { x: -58, y: 4 };
  } else if (effectiveTaskKind === "UNLOAD_MACHINE") {
    offset = { x: -34, y: 2 };
  } else if (effectiveTaskKind === "REPAIR_MACHINE" || effectiveTaskKind === "PREVENTIVE_MAINTENANCE") {
    const targetEntity = entities[targetId];
    const repairTeam = Array.isArray(targetEntity?.attributes?.repair_team)
      ? targetEntity.attributes.repair_team.map((member) => String(member))
      : [];
    const repairOffsets = [
      { x: -56, y: -12 },
      { x: -58, y: 18 },
      { x: -10, y: 26 },
    ];
    const teamIndex = Math.max(0, repairTeam.indexOf(entity.entity_id));
    if (effectiveTaskKind === "REPAIR_MACHINE" && repairTeam.length > 0) {
      offset = repairOffsets[teamIndex % repairOffsets.length];
    } else {
      offset = { x: -44, y: 12 };
    }
  }
  return {
    x: targetPosition.x + offset.x,
    y: targetPosition.y + offset.y,
  };
}

function interpolatePosition(entity: BaseEntityState, basePosition: { x: number; y: number }, currentTime: number) {
  const motion = entity.attributes.motion as MotionMeta | undefined;
  if (!motion?.from || !motion?.to || motion.started_at === undefined || motion.ended_at === undefined) {
    return basePosition;
  }
  if (currentTime <= motion.started_at) return motion.from;
  if (currentTime >= motion.ended_at || entity.state !== "moving") return basePosition;
  const span = Math.max(0.0001, motion.ended_at - motion.started_at);
  const ratio = (currentTime - motion.started_at) / span;
  return {
    x: motion.from.x + (motion.to.x - motion.from.x) * ratio,
    y: motion.from.y + (motion.to.y - motion.from.y) * ratio,
  };
}

function matchesSearch(entity: BaseEntityState, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [entity.entity_id, entity.label, entity.entity_type, entity.state].some((value) => value.toLowerCase().includes(normalized));
}

function currentEventSeverity(event: ReplayEvent): "info" | "warning" | "error" {
  if (event.event_type === "error_raised" || event.event_type === "deadlock_detected") return "error";
  if (
    event.event_type === "warning_raised" ||
    event.event_type === "bottleneck_detected" ||
    event.event_type === "battery_low" ||
    event.event_type === "maintenance_started"
  ) {
    return "warning";
  }
  return "info";
}

function currentEventLabel(event: ReplayEvent): string {
  const taskLabel = typeof event.payload.task_label === "string" ? event.payload.task_label : undefined;
  const taskId = typeof event.payload.task_id === "string" ? event.payload.task_id : undefined;
  const payloadLabel = typeof event.payload.label === "string" ? event.payload.label : undefined;
  const message = typeof event.payload.message === "string" ? event.payload.message : undefined;
  return taskLabel || payloadLabel || message || taskId || event.event_type;
}

function buildCurrentEventFlow(event?: ReplayEvent): RenderFlow | undefined {
  if (!event) return undefined;
  const flowableTypes = new Set<ReplayEvent["event_type"]>([
    "message_sent",
    "task_assigned",
    "task_started",
    "task_finished",
    "queue_entered",
    "queue_exited",
    "charging_started",
    "charging_finished",
    "maintenance_started",
    "maintenance_finished",
    "collaboration_started",
    "battery_low",
    "warning_raised",
    "error_raised",
    "deadlock_detected",
    "livelock_detected",
    "bottleneck_detected",
  ]);
  if (!flowableTypes.has(event.event_type)) return undefined;
  return {
    id: `current:${event.event_id}`,
    source_id: event.entity_refs.source ?? event.entity_refs.primary,
    target_id: event.entity_refs.target ?? event.entity_refs.related?.[0],
    label: currentEventLabel(event),
    kind:
      event.event_type === "message_sent"
        ? "message"
        : event.event_type === "task_assigned"
          ? "task_handoff"
          : event.event_type === "collaboration_started"
            ? "collaboration"
            : event.event_type === "warning_raised" ||
                event.event_type === "error_raised" ||
                event.event_type === "deadlock_detected" ||
                event.event_type === "livelock_detected" ||
                event.event_type === "bottleneck_detected" ||
                event.event_type === "battery_low" ||
                event.event_type === "maintenance_started"
              ? "warning"
              : undefined,
    severity: currentEventSeverity(event),
    active: true,
  };
}

export function buildRenderModel(
  domainState: DomainState,
  currentTime: number,
  options: BuildRenderModelOptions = {},
): ReplayRenderModel {
  // The render model is deliberately isolated from replay/state reconstruction.
  const entities = [...Object.values(domainState.entities), ...createVirtualOutputQueues(domainState)];
  const resolvedLayout = resolveLayout(entities, options.logLayout, options.externalLayout);
  const focusedEntityIds = getFocusedEntityIds(domainState, options.selectedEntityId);

  const nodes: RenderNode[] = entities
    .filter((entity) => {
      if (typeof entity.attributes.item_state === "string") return false;
      if (options.visibleEntityTypes?.length && !options.visibleEntityTypes.includes(entity.entity_type)) return false;
      if (options.entityIdFilter && entity.entity_id !== options.entityIdFilter) return false;
      if (!matchesSearch(entity, options.searchQuery ?? "")) return false;
      return true;
    })
    .map((entity) => {
      const isMobileEntity =
        entity.entity_type === "worker" || entity.entity_type === "robot" || entity.entity_type === "transporter";
      const basePosition =
        (isMobileEntity && entity.position ? { ...entity.position } : undefined) ??
        resolvedLayout.positions[entity.entity_id] ??
        { x: 0, y: 0 };
      if (entity.entity_type === "queue" || entity.entity_type === "buffer") {
        const queueSize = domainState.queues[entity.entity_id]?.item_ids.length;
        if (queueSize !== undefined) {
          entity.attributes.queue_size = queueSize;
        }
      }
      const derivedFromQueue =
        typeof entity.attributes.derived_from_queue === "string" ? entity.attributes.derived_from_queue : undefined;
      if (entity.attributes.virtual && derivedFromQueue) {
        entity.attributes.queue_size = domainState.queues[derivedFromQueue]?.item_ids.length ?? 0;
      }
      const visualPosition = workerMachineTaskPosition(entity, basePosition, domainState.entities, resolvedLayout.positions);
      return {
        entity,
        position: interpolatePosition(entity, visualPosition, currentTime),
        selected: entity.entity_id === options.selectedEntityId,
        focused: !options.followSelected || focusedEntityIds.size === 0 || focusedEntityIds.has(entity.entity_id),
      };
    });

  const regions: RenderRegion[] = Object.values(resolvedLayout.regions).map((region) => ({
    region_id: region.region_id,
    label: region.label,
    kind: region.kind,
    position: region.position,
    size: region.size,
    accent: region.accent,
    background: region.background,
  }));

  const activeInteractions = getActiveInteractions(domainState, currentTime);
  const flows: RenderFlow[] = activeInteractions.map((interaction) => ({
    id: interaction.interaction_id,
    source_id: interaction.source_id,
    target_id: interaction.target_id,
    label: interaction.label,
    kind: interaction.type,
    severity: interaction.severity,
    active: true,
  }));

  const currentFlow = buildCurrentEventFlow(options.currentEvent);
  if (currentFlow) flows.push(currentFlow);

  return {
    regions,
    nodes,
    flows,
    selectedEntity: options.selectedEntityId ? domainState.entities[options.selectedEntityId] : undefined,
    activeInteractions,
  };
}
