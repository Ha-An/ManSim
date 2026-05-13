import type { ReplayEvent } from "../types/event";

export interface ReplayEventIndex {
  all: number[];
  byType: Record<string, number[]>;
  byEntity: Record<string, number[]>;
  warningIndexes: number[];
}

function pushIndex(map: Record<string, number[]>, key: string | undefined, index: number): void {
  if (!key) return;
  map[key] ??= [];
  map[key].push(index);
}

export function buildEventIndex(events: ReplayEvent[]): ReplayEventIndex {
  const byType: Record<string, number[]> = {};
  const byEntity: Record<string, number[]> = {};
  const warningIndexes: number[] = [];

  events.forEach((event, index) => {
    pushIndex(byType, event.event_type, index);
    pushIndex(byEntity, event.entity_refs.primary, index);
    pushIndex(byEntity, event.entity_refs.source, index);
    pushIndex(byEntity, event.entity_refs.target, index);

    for (const relatedId of event.entity_refs.related ?? []) {
      pushIndex(byEntity, relatedId, index);
    }

    if (
      event.event_type === "warning_raised" ||
      event.event_type === "error_raised" ||
      event.event_type === "deadlock_detected" ||
      event.event_type === "livelock_detected" ||
      event.event_type === "bottleneck_detected" ||
      event.event_type === "traffic_conflict_detected"
    ) {
      warningIndexes.push(index);
    }
  });

  return {
    all: events.map((_, index) => index),
    byType,
    byEntity,
    warningIndexes,
  };
}

export function findNextIndex(indexes: number[], cursor: number): number | undefined {
  return indexes.find((index) => index >= cursor);
}
