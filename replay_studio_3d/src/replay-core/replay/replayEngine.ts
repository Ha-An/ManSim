import { buildRenderModel } from "../render-model/buildRenderModel";
import type { DomainState } from "../types/entity";
import type { ReplayEvent } from "../types/event";
import type { EventPredicate, ReplayFrameState, ReplayLog } from "../types/replay";
import { createCheckpointStore, DEFAULT_CHECKPOINT_INTERVAL, type ReplayCheckpointStore } from "./checkpointStore";
import { buildEventIndex, findNextIndex, type ReplayEventIndex } from "./eventIndex";
import { snapshotToDomain } from "./reducers";
import { getCurrentEvent } from "./selectors";

export interface ReplayEngine {
  load(log: ReplayLog): void;
  play(): void;
  pause(): void;
  resume(): void;
  seek(timestamp: number): void;
  stepForward(): void;
  stepBackward(): void;
  jumpToNextEvent(predicate: EventPredicate): void;
  jumpToNextWarning(): void;
  setSpeed(speed: 0.25 | 0.5 | 1 | 2 | 4 | 8): void;
  getCurrentState(): ReplayFrameState;
  subscribe(listener: () => void): () => void;
}

const SPEEDS = new Set([0.25, 0.5, 1, 2, 4, 8]);

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function upperBound(events: ReplayEvent[], timestamp: number): number {
  let low = 0;
  let high = events.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (events[middle].timestamp <= timestamp) low = middle + 1;
    else high = middle;
  }
  return low;
}

export function createReplayEngine(checkpointInterval = DEFAULT_CHECKPOINT_INTERVAL): ReplayEngine {
  let parsedLog: ReplayLog | null = null;
  let eventIndex: ReplayEventIndex | null = null;
  let checkpointStore: ReplayCheckpointStore | null = null;
  let domainState: DomainState = snapshotToDomain();
  let currentTime = 0;
  let currentCursor = 0;
  let speed: 0.25 | 0.5 | 1 | 2 | 4 | 8 = 1;
  let playing = false;
  let rafId = 0;
  let lastTickAt = 0;
  const listeners = new Set<() => void>();

  function notify(): void {
    for (const listener of listeners) listener();
  }

  function ensureLoaded(): void {
    if (!parsedLog || !eventIndex || !checkpointStore) throw new Error("Replay engine is not loaded.");
  }

  function restoreCursor(cursor: number): void {
    ensureLoaded();
    // All backward movement restores from the nearest checkpoint and replays deterministically.
    currentCursor = clamp(cursor, 0, parsedLog!.events.length);
    domainState = checkpointStore!.restoreAtCursor(currentCursor);
    currentTime = currentCursor > 0 ? parsedLog!.events[currentCursor - 1].timestamp : parsedLog!.initial_state?.timestamp ?? 0;
    domainState.current_time = currentTime;
  }

  function restoreTime(timestamp: number): void {
    ensureLoaded();
    // Timestamp seeks are converted into a stable event cursor, then replayed from a checkpoint.
    const boundedTime = clamp(timestamp, parsedLog!.initial_state?.timestamp ?? 0, parsedLog!.metadata.total_duration);
    const targetCursor = upperBound(parsedLog!.events, boundedTime);
    restoreCursor(targetCursor);
    currentTime = boundedTime;
    domainState.current_time = boundedTime;
  }

  function stopLoop(): void {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = 0;
    lastTickAt = 0;
  }

  function tick(now: number): void {
    if (!playing || !parsedLog) return;
    if (!lastTickAt) lastTickAt = now;
    const deltaSeconds = (now - lastTickAt) / 1000;
    lastTickAt = now;
    // Runtime playback advances replay time without touching the canonical log order.
    const nextTime = currentTime + deltaSeconds * speed;
    restoreTime(nextTime);
    if (currentTime >= parsedLog.metadata.total_duration || currentCursor >= parsedLog.events.length) {
      playing = false;
      stopLoop();
      notify();
      return;
    }
    notify();
    rafId = requestAnimationFrame(tick);
  }

  return {
    load(log: ReplayLog): void {
      parsedLog = log;
      eventIndex = buildEventIndex(parsedLog.events);
      checkpointStore = createCheckpointStore(parsedLog, checkpointInterval);
      domainState = snapshotToDomain(parsedLog.initial_state);
      currentTime = parsedLog.initial_state?.timestamp ?? 0;
      currentCursor = 0;
      playing = false;
      stopLoop();
      notify();
    },
    play(): void {
      ensureLoaded();
      if (playing) return;
      playing = true;
      lastTickAt = 0;
      rafId = requestAnimationFrame(tick);
      notify();
    },
    pause(): void {
      playing = false;
      stopLoop();
      notify();
    },
    resume(): void {
      this.play();
    },
    seek(timestamp: number): void {
      ensureLoaded();
      restoreTime(timestamp);
      notify();
    },
    stepForward(): void {
      ensureLoaded();
      if (currentCursor >= parsedLog!.events.length) return;
      const nextEvent = parsedLog!.events[currentCursor];
      restoreCursor(currentCursor + 1);
      currentTime = nextEvent.timestamp;
      domainState.current_time = currentTime;
      notify();
    },
    stepBackward(): void {
      ensureLoaded();
      restoreCursor(Math.max(0, currentCursor - 1));
      notify();
    },
    jumpToNextEvent(predicate: EventPredicate): void {
      ensureLoaded();
      const matchIndex = parsedLog!.events.findIndex((event, index) => index >= currentCursor && predicate(event));
      if (matchIndex >= 0) {
        restoreCursor(matchIndex + 1);
        notify();
      }
    },
    jumpToNextWarning(): void {
      ensureLoaded();
      const nextIndex = findNextIndex(eventIndex!.warningIndexes, currentCursor);
      if (nextIndex !== undefined) {
        restoreCursor(nextIndex + 1);
        notify();
      }
    },
    setSpeed(nextSpeed: 0.25 | 0.5 | 1 | 2 | 4 | 8): void {
      if (!SPEEDS.has(nextSpeed)) return;
      speed = nextSpeed;
      notify();
    },
    getCurrentState(): ReplayFrameState {
      const currentEvent = parsedLog ? getCurrentEvent(parsedLog.events, currentCursor) : undefined;
      return {
        time: currentTime,
        cursor: currentCursor,
        currentEvent,
        domainState,
        renderModel: buildRenderModel(domainState, currentTime, {
          logLayout: parsedLog?.layout,
          currentEvent,
        }),
        matchingEventIndexes: parsedLog ? parsedLog.events.map((_, index) => index) : [],
      };
    },
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}
