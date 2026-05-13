import Ajv2020, { type ErrorObject } from "ajv/dist/2020";
import replayLogSchema from "./replay-log.schema.json";
import type { ReplayLog } from "../types/replay";

const ajv = new Ajv2020({ allErrors: true, strict: false });
const validate = ajv.compile<ReplayLog>(replayLogSchema);

function formatErrors(errors: ErrorObject[] | null | undefined): string {
  if (!errors?.length) return "Unknown validation error.";
  return errors
    .map((error) => `${error.instancePath || "/"} ${error.message || "is invalid"}`.trim())
    .join("\n");
}

// Validation is intentionally split from parsing so replay engine code can assume a sane log.
export function validateReplayLog(payload: unknown): ReplayLog {
  if (!validate(payload)) {
    throw new Error(formatErrors(validate.errors));
  }

  const log = payload as ReplayLog;
  const sequenceIndexes = new Set<number>();
  for (const event of log.events) {
    if (sequenceIndexes.has(event.sequence_index)) {
      throw new Error(`Duplicate sequence_index: ${event.sequence_index}`);
    }
    sequenceIndexes.add(event.sequence_index);

    const durative = event.durative;
    if (durative?.started_at !== undefined && durative?.ended_at !== undefined && durative.ended_at < durative.started_at) {
      throw new Error(`Durative event ${event.event_id} has ended_at earlier than started_at.`);
    }
  }

  for (const checkpoint of log.checkpoints ?? []) {
    if (checkpoint.event_cursor > log.events.length) {
      throw new Error(`Checkpoint ${checkpoint.checkpoint_id} references invalid event_cursor ${checkpoint.event_cursor}.`);
    }
  }

  return log;
}

