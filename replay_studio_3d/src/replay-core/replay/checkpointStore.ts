import type { ReplayCheckpoint } from "../types/snapshot";
import type { ReplayLog } from "../types/replay";
import type { DomainState } from "../types/entity";
import { applyEvent, domainToSnapshot, snapshotToDomain } from "./reducers";

export interface ReplayCheckpointStore {
  checkpoints: ReplayCheckpoint[];
  restoreAtCursor(cursor: number): DomainState;
}

export const DEFAULT_CHECKPOINT_INTERVAL = 500;

function buildRuntimeCheckpoints(log: ReplayLog, interval: number): ReplayCheckpoint[] {
  // If the simulator did not emit checkpoints, build deterministic in-memory ones for seek.
  const checkpoints: ReplayCheckpoint[] = [
    {
      checkpoint_id: "runtime-0",
      event_cursor: 0,
      timestamp: log.initial_state?.timestamp ?? 0,
      snapshot: domainToSnapshot(snapshotToDomain(log.initial_state)),
    },
  ];

  let domain = snapshotToDomain(log.initial_state);
  log.events.forEach((event, index) => {
    domain = applyEvent(domain, event);
    if ((index + 1) % interval === 0) {
      checkpoints.push({
        checkpoint_id: `runtime-${index + 1}`,
        event_cursor: index + 1,
        timestamp: event.timestamp,
        snapshot: domainToSnapshot(domain),
      });
    }
  });

  return checkpoints;
}

export function createCheckpointStore(log: ReplayLog, interval = DEFAULT_CHECKPOINT_INTERVAL): ReplayCheckpointStore {
  const checkpoints = (log.checkpoints?.length ? log.checkpoints : buildRuntimeCheckpoints(log, interval))
    .slice()
    .sort((left, right) => left.event_cursor - right.event_cursor);

  return {
    checkpoints,
    restoreAtCursor(cursor: number): DomainState {
      const boundedCursor = Math.max(0, Math.min(cursor, log.events.length));
      // Restore from the nearest checkpoint and replay only the missing suffix.
      const checkpoint =
        [...checkpoints]
          .reverse()
          .find((candidate) => candidate.event_cursor <= boundedCursor) ?? checkpoints[0];

      let domain = snapshotToDomain(checkpoint.snapshot);
      for (let index = checkpoint.event_cursor; index < boundedCursor; index += 1) {
        domain = applyEvent(domain, log.events[index]);
      }
      domain.current_time = boundedCursor > 0 ? log.events[boundedCursor - 1].timestamp : log.initial_state?.timestamp ?? 0;
      return domain;
    },
  };
}
