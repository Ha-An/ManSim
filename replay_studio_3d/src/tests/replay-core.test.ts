import { describe, expect, it } from "vitest";
import { buildRenderModel } from "../replay-core/render-model/buildRenderModel";
import { applyEvent } from "../replay-core/replay/reducers";
import type { DomainState } from "../replay-core/types/entity";
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

  it("hides the legacy warehouse buffer when the canonical completed buffer exists", () => {
    const domainState: DomainState = {
      entities: {
        completed_product_buffer: {
          entity_id: "completed_product_buffer",
          entity_type: "buffer",
          state: "waiting",
          label: "Completed Products",
          position: { x: 100, y: 20 },
          attributes: {},
          relations: {},
          updated_at: 0,
        },
        warehouse_buffer: {
          entity_id: "warehouse_buffer",
          entity_type: "buffer",
          state: "waiting",
          label: "Completed Buffer",
          attributes: {},
          relations: {},
          updated_at: 0,
        },
      },
      resources: {},
      queues: {},
      interactions: {},
      current_event_index: 0,
      current_time: 0,
    };

    const renderModel = buildRenderModel(domainState, 0);
    expect(renderModel.nodes.map((node) => node.entity.entity_id)).toEqual(["completed_product_buffer"]);
  });

  it("clears stale machine wait item overlays when the machine reports a new state without overlay fields", () => {
    const domainState: DomainState = {
      entities: {
        S1M1: {
          entity_id: "S1M1",
          entity_type: "machine",
          state: "waiting",
          label: "S1M1",
          attributes: {
            machine_state: "DONE_WAIT_UNLOAD",
            wait_visual: "completed_output",
            wait_item_kind: "intermediate",
          },
          relations: {},
          updated_at: 81,
        },
      },
      resources: {},
      queues: {},
      interactions: {},
      current_event_index: 0,
      current_time: 81,
    };

    const next = applyEvent(domainState, {
      event_id: "state-245",
      sequence_index: 1,
      timestamp: 245,
      event_type: "state_changed",
      entity_refs: { primary: "S1M1" },
      payload: {
        state: "waiting",
        attributes: {
          machine_state: "WAIT_INPUT",
          input_item_id: null,
          output_item_id: null,
        },
      },
    });

    expect(next.entities.S1M1.attributes.wait_visual).toBeUndefined();
    expect(next.entities.S1M1.attributes.wait_item_kind).toBeUndefined();
  });

  it("clears explicit prep-wait machine overlays because WAIT_INPUT should not draw an item on the machine", () => {
    const domainState: DomainState = {
      entities: {
        S1M2: {
          entity_id: "S1M2",
          entity_type: "machine",
          state: "waiting",
          label: "S1M2",
          attributes: {},
          relations: {},
          updated_at: 0,
        },
      },
      resources: {},
      queues: {},
      interactions: {},
      current_event_index: 0,
      current_time: 0,
    };

    const next = applyEvent(domainState, {
      event_id: "state-prep-wait",
      sequence_index: 1,
      timestamp: 16.4,
      event_type: "state_changed",
      entity_refs: { primary: "S1M2" },
      payload: {
        state: "waiting",
        attributes: {
          machine_state: "WAIT_INPUT",
          wait_visual: "prep_wait",
          wait_item_kind: "material",
        },
      },
    });

    expect(next.entities.S1M2.attributes.wait_visual).toBeUndefined();
    expect(next.entities.S1M2.attributes.wait_item_kind).toBeUndefined();
  });
});
