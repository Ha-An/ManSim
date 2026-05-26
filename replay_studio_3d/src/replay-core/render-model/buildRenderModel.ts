import type { BaseEntityState, DomainState, XY } from "../types/entity";
import type { ReplayEvent } from "../types/event";
import type { LayoutConfig } from "../types/layout";
import type { ReplayRenderModel, RenderFlow, RenderNode, RenderRegion } from "../types/replay";
import { resolveLayout } from "../layout/layoutResolver";
import { getActiveInteractions, getFocusedEntityIds } from "../replay/selectors";

interface MotionMeta {
  from?: { x: number; y: number };
  to?: { x: number; y: number };
  path?: Array<{ x: number; y: number }>;
  started_at?: number;
  ended_at?: number;
  paused?: boolean;
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

function interpolatePosition(entity: BaseEntityState, basePosition: { x: number; y: number }, currentTime: number) {
  const motion = entity.attributes.motion as MotionMeta | undefined;
  if (!motion?.from || !motion?.to || motion.started_at === undefined || motion.ended_at === undefined) {
    return basePosition;
  }
  if (motion.paused) return motion.from;
  if (currentTime <= motion.started_at) return motion.from;
  if (currentTime >= motion.ended_at) return motion.to;
  const span = Math.max(0.0001, motion.ended_at - motion.started_at);
  const ratio = (currentTime - motion.started_at) / span;
  if (Array.isArray(motion.path) && motion.path.length >= 2) {
    const clampedRatio = Math.max(0, Math.min(1, ratio));
    const scaled = clampedRatio * (motion.path.length - 1);
    const index = Math.min(motion.path.length - 2, Math.floor(scaled));
    const local = scaled - index;
    const source = motion.path[index];
    const target = motion.path[index + 1];
    return {
      x: source.x + (target.x - source.x) * local,
      y: source.y + (target.y - source.y) * local,
    };
  }
  return {
    x: motion.from.x + (motion.to.x - motion.from.x) * ratio,
    y: motion.from.y + (motion.to.y - motion.from.y) * ratio,
  };
}

function isMobileEntity(entity: BaseEntityState): boolean {
  return entity.entity_type === "worker" || entity.entity_type === "robot" || entity.entity_type === "transporter";
}

function resolveEntityPosition(
  entity: BaseEntityState,
  basePosition: XY,
  currentTime: number,
): XY {
  return interpolatePosition(entity, basePosition, currentTime);
}

function matchesSearch(entity: BaseEntityState, query: string): boolean {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return true;
  return [entity.entity_id, entity.label, entity.entity_type, entity.state].some((value) => value.toLowerCase().includes(normalized));
}

function currentEventSeverity(event: ReplayEvent): "info" | "warning" | "error" {
  if (event.event_type === "error_raised" || event.event_type === "deadlock_detected") return "error";
  if (event.event_type === "traffic_conflict_detected") {
    return event.payload.severity === "error" ? "error" : event.payload.severity === "info" ? "info" : "warning";
  }
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
  // The render model reads reconstructed replay state without inventing entities or positions.
  const entities = Object.values(domainState.entities);
  // `warehouse_buffer` is still emitted as a compatibility alias in some
  // artifacts. 3D rendering should keep the canonical completed-product buffer
  // only, otherwise the alias appears as a stray object in Station 1.
  const hasCanonicalCompletedBuffer = entities.some((entity) => entity.entity_id === "completed_product_buffer");
  const resolvedLayout = resolveLayout(entities, options.logLayout, options.externalLayout);
  const focusedEntityIds = getFocusedEntityIds(domainState, options.selectedEntityId);

  const nodes: RenderNode[] = entities
    .filter((entity) => {
      if (entity.entity_id === "warehouse_buffer" && hasCanonicalCompletedBuffer) return false;
      const itemState = typeof entity.attributes.item_state === "string" ? entity.attributes.item_state.trim().toUpperCase() : "";
      if (itemState && itemState !== "DROPPED") return false;
      if (options.visibleEntityTypes?.length && !options.visibleEntityTypes.includes(entity.entity_type)) return false;
      if (options.entityIdFilter && entity.entity_id !== options.entityIdFilter) return false;
      if (!matchesSearch(entity, options.searchQuery ?? "")) return false;
      return true;
    })
    .map((entity) => {
      const basePosition =
        (isMobileEntity(entity) && entity.position ? { ...entity.position } : undefined) ??
        resolvedLayout.positions[entity.entity_id] ??
        { x: 0, y: 0 };
      return {
        entity,
        position: resolveEntityPosition(entity, basePosition, currentTime),
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
    grid: options.externalLayout?.grid ?? options.logLayout?.grid,
    nodes,
    flows,
    selectedEntity: options.selectedEntityId ? domainState.entities[options.selectedEntityId] : undefined,
    activeInteractions,
  };
}
