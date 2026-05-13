import { describe, expect, it } from "vitest";
import { parseReplayLog } from "../replay-core/parser/parseReplayLog";

describe("replay core copy", () => {
  it("parses and stable-sorts replay events", () => {
    const parsed = parseReplayLog({
      schema_version: "1.0",
      metadata: {
        run_id: "unit",
        domain: "manufacturing",
        total_duration: 10,
        time_unit: "minutes",
      },
      events: [
        { event_id: "b", sequence_index: 2, timestamp: 2, event_type: "state_changed", entity_refs: {}, payload: {} },
        { event_id: "a", sequence_index: 1, timestamp: 1, event_type: "state_changed", entity_refs: {}, payload: {} },
      ],
    });
    expect(parsed.events.map((event) => event.event_id)).toEqual(["a", "b"]);
  });
});

