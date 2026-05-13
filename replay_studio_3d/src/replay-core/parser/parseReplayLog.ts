import { validateReplayLog } from "../schema/validateReplayLog";
import type { ReplayEvent } from "../types/event";
import type { ReplayLog } from "../types/replay";

function stableSortEvents(events: ReplayEvent[]): ReplayEvent[] {
  return [...events].sort((left, right) => {
    if (left.timestamp !== right.timestamp) return left.timestamp - right.timestamp;
    if (left.sequence_index !== right.sequence_index) return left.sequence_index - right.sequence_index;
    return left.event_id.localeCompare(right.event_id);
  });
}

// Parsing normalizes ordering before the engine sees the event stream.
export function parseReplayLog(raw: unknown): ReplayLog {
  const validated = validateReplayLog(raw);
  return {
    ...validated,
    events: stableSortEvents(validated.events),
  };
}
