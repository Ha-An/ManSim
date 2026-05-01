import type { DomainState } from "../types/entity";
import type { ReplayEvent } from "../types/event";

export function getCurrentEvent(events: ReplayEvent[], cursor: number): ReplayEvent | undefined {
  return cursor > 0 ? events[cursor - 1] : undefined;
}

export function getActiveInteractions(domainState: DomainState, time: number) {
  return Object.values(domainState.interactions).filter(
    (interaction) => interaction.started_at <= time && (interaction.ended_at === undefined || interaction.ended_at >= time),
  );
}

export function getFocusedEntityIds(domainState: DomainState, selectedEntityId?: string): Set<string> {
  if (!selectedEntityId) return new Set();
  const selected = domainState.entities[selectedEntityId];
  const focused = new Set<string>([selectedEntityId]);
  for (const linkedId of selected?.relations.linked_ids ?? []) focused.add(linkedId);
  if (selected?.relations.assigned_to) focused.add(selected.relations.assigned_to);
  if (selected?.relations.parent_id) focused.add(selected.relations.parent_id);
  return focused;
}
