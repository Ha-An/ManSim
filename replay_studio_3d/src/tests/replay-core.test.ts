import { describe, expect, it } from "vitest";
import { buildRenderModel } from "../replay-core/render-model/buildRenderModel";
import { applyEvent } from "../replay-core/replay/reducers";
import type { DomainState } from "../replay-core/types/entity";
import { parseReplayLog } from "../replay-core/parser/parseReplayLog";
import { buildRollingTaskPoolModel, isRollingHorizonReplay } from "../ui/RollingTaskPoolPanel";

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

  it("keeps separate S2 machine input slots when legacy logs replace input_item_id", () => {
    const domainState: DomainState = {
      entities: {
        S2M2: {
          entity_id: "S2M2",
          entity_type: "machine",
          state: "waiting",
          label: "S2M2",
          attributes: {},
          relations: {},
          updated_at: 140,
        },
      },
      resources: {},
      queues: {},
      interactions: {},
      current_event_index: 0,
      current_time: 140,
    };

    const withMaterial = applyEvent(domainState, {
      event_id: "s2-material",
      sequence_index: 1,
      timestamp: 141,
      event_type: "state_changed",
      entity_refs: { primary: "S2M2" },
      payload: {
        state: "waiting",
        attributes: {
          machine_state: "WAIT_INPUT",
          input_item_id: "MAT-WH-4",
        },
      },
    });
    const withIntermediate = applyEvent(withMaterial, {
      event_id: "s2-intermediate",
      sequence_index: 2,
      timestamp: 145,
      event_type: "state_changed",
      entity_refs: { primary: "S2M2" },
      payload: {
        state: "waiting",
        attributes: {
          machine_state: "WAIT_INPUT",
          input_item_id: "INT-S1-32",
        },
      },
    });

    expect(withIntermediate.entities.S2M2.attributes.input_material_id).toBe("MAT-WH-4");
    expect(withIntermediate.entities.S2M2.attributes.input_intermediate_id).toBe("INT-S1-32");
  });

  it("stores the last worker heading from movement paths for first-person replay", () => {
    const domainState: DomainState = {
      entities: {
        A1: {
          entity_id: "A1",
          entity_type: "worker",
          state: "idle",
          label: "A1",
          position: { x: 0, y: 0 },
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
      event_id: "move-east",
      sequence_index: 1,
      timestamp: 0,
      event_type: "entity_moved",
      entity_refs: { primary: "A1" },
      durative: { started_at: 0, ended_at: 1 },
      payload: {
        from: { x: 0, y: 0 },
        to: { x: 2, y: 0 },
        path: [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 2, y: 0 }],
      },
    });

    expect(next.entities.A1.attributes.last_heading_angle).toBeCloseTo(0);
  });

  it("preserves paused motion so first-person replay does not interpolate path-wait movement", () => {
    const domainState: DomainState = {
      entities: {
        A2: {
          entity_id: "A2",
          entity_type: "worker",
          state: "idle",
          label: "A2",
          position: { x: 5, y: 5 },
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
      event_id: "paused-move",
      sequence_index: 1,
      timestamp: 4,
      event_type: "entity_moved",
      entity_refs: { primary: "A2" },
      durative: { started_at: 4, ended_at: 9 },
      payload: {
        from: { x: 5, y: 5 },
        to: { x: 25, y: 5 },
        path: [{ x: 5, y: 5 }, { x: 25, y: 5 }],
        display_path: [{ x: 5, y: 5 }, { x: 25, y: 5 }],
        paused: true,
      },
    });

    expect((next.entities.A2.attributes.motion as Record<string, unknown>).paused).toBe(true);
  });

  it("recognizes rolling horizon logs and aggregates task pool opportunities", () => {
    const parsed = parseReplayLog({
      schema_version: "1.0",
      metadata: {
        run_id: "rolling",
        domain: "manufacturing",
        decision_mode: "rolling_horizon_aging_priority",
        total_duration: 10,
        time_unit: "minutes",
      },
      events: [
        {
          event_id: "w0",
          sequence_index: 1,
          timestamp: 0,
          event_type: "rolling_horizon_window_started",
          entity_refs: { primary: "RH-0" },
          payload: { window_index: 0, window_start_min: 0, window_end_min: 5 },
        },
        {
          event_id: "c1",
          sequence_index: 2,
          timestamp: 0,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-1", related: ["A1"] },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-1",
            worker_id: "A1",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station1", transfer_item_id: "MAT-WH-1" },
          },
        },
        {
          event_id: "c2",
          sequence_index: 3,
          timestamp: 0.1,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-1", related: ["A2"] },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-1",
            worker_id: "A2",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station1", transfer_item_id: "MAT-WH-1" },
          },
        },
        {
          event_id: "d1",
          sequence_index: 4,
          timestamp: 5,
          event_type: "rolling_horizon_dispatched",
          entity_refs: { primary: "RHOPP-1", target: "A2" },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-1",
            assigned_worker_id: "A2",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station1", transfer_item_id: "MAT-WH-1" },
          },
        },
      ],
    });

    expect(isRollingHorizonReplay(parsed)).toBe(true);
    const model = buildRollingTaskPoolModel(parsed.events, 5);
    expect(model.entries).toHaveLength(1);
    expect(model.entries[0].workerIds).toEqual(["A1", "A2"]);
    expect(model.entries[0].assignedWorkerId).toBe("A2");
    expect(model.entries[0].status).toBe("dispatched");
  });

  it("keeps unresolved rolling horizon tasks visible and updates repeated opportunity windows in place", () => {
    const parsed = parseReplayLog({
      schema_version: "1.0",
      metadata: {
        run_id: "rolling-carryover",
        domain: "manufacturing",
        decision_mode: "rolling_horizon_aging_priority",
        total_duration: 15,
        time_unit: "minutes",
      },
      events: [
        {
          event_id: "w0",
          sequence_index: 1,
          timestamp: 0,
          event_type: "rolling_horizon_window_started",
          entity_refs: { primary: "RH-0" },
          payload: { window_index: 0, window_start_min: 0, window_end_min: 5 },
        },
        {
          event_id: "old",
          sequence_index: 2,
          timestamp: 0,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-OLD", related: ["A1"] },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-OLD",
            worker_id: "A1",
            task_code: "SETUP_MACHINE",
            fixed_priority: 80,
            rolling_task_signature: { machine_id: "S1M1" },
          },
        },
        {
          event_id: "repeat-0",
          sequence_index: 3,
          timestamp: 0.1,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-REPEAT", related: ["A2"] },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-REPEAT",
            worker_id: "A2",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station1", source_slot_id: "warehouse_material_slot_01" },
          },
        },
        {
          event_id: "w1",
          sequence_index: 4,
          timestamp: 5,
          event_type: "rolling_horizon_window_started",
          entity_refs: { primary: "RH-1" },
          payload: { window_index: 1, window_start_min: 5, window_end_min: 10 },
        },
        {
          event_id: "w2",
          sequence_index: 5,
          timestamp: 10,
          event_type: "rolling_horizon_window_started",
          entity_refs: { primary: "RH-2" },
          payload: { window_index: 2, window_start_min: 10, window_end_min: 15 },
        },
        {
          event_id: "repeat-2",
          sequence_index: 6,
          timestamp: 10.1,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-REPEAT", related: ["A3"] },
          payload: {
            window_index: 2,
            opportunity_id: "RHOPP-REPEAT",
            worker_id: "A3",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station1", source_slot_id: "warehouse_material_slot_01" },
          },
        },
      ],
    });

    const model = buildRollingTaskPoolModel(parsed.events, 10.2);
    const repeated = model.entries.filter((entry) => entry.opportunityId === "RHOPP-REPEAT");
    expect(repeated).toHaveLength(1);
    expect(repeated[0].windowIndex).toBe(2);
    expect(repeated[0].collectedAt).toBe(0.1);
    expect(repeated[0].updatedAt).toBe(0.1);
    expect(repeated[0].workerIds).toEqual(["A2", "A3"]);

    const oldCarryover = model.entries.find((entry) => entry.opportunityId === "RHOPP-OLD");
    expect(oldCarryover?.windowIndex).toBe(0);
    expect(oldCarryover?.status).toBe("pool");
  });

  it("does not reopen a dispatched rolling horizon opportunity when a stale candidate event appears later", () => {
    const parsed = parseReplayLog({
      schema_version: "1.0",
      metadata: {
        run_id: "rolling-stale-candidate",
        domain: "manufacturing",
        decision_mode: "rolling_horizon_aging_priority",
        total_duration: 10,
        time_unit: "minutes",
      },
      events: [
        {
          event_id: "c0",
          sequence_index: 1,
          timestamp: 0,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-MAT2", related: ["A1"] },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-MAT2",
            worker_id: "A1",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station2", transfer_item_id: "MAT-WH-2" },
          },
        },
        {
          event_id: "d0",
          sequence_index: 2,
          timestamp: 5,
          event_type: "rolling_horizon_dispatched",
          entity_refs: { primary: "RHOPP-MAT2", target: "A2" },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-MAT2",
            assigned_worker_id: "A2",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station2", transfer_item_id: "MAT-WH-2" },
          },
        },
        {
          event_id: "c1",
          sequence_index: 3,
          timestamp: 5,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-MAT2", related: ["A3"] },
          payload: {
            window_index: 1,
            opportunity_id: "RHOPP-MAT2",
            worker_id: "A3",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station2", transfer_item_id: "MAT-WH-2" },
          },
        },
      ],
    });

    const model = buildRollingTaskPoolModel(parsed.events, 11.6);
    expect(model.entries).toHaveLength(1);
    expect(model.entries[0].status).toBe("dispatched");
    expect(model.entries[0].windowIndex).toBe(0);
    expect(model.entries[0].assignedWorkerId).toBe("A2");
  });

  it("keeps dispatched rolling horizon tasks visible after later windows become active", () => {
    const parsed = parseReplayLog({
      schema_version: "1.0",
      metadata: {
        run_id: "rolling-dispatched-visible",
        domain: "manufacturing",
        decision_mode: "rolling_horizon_dedicated_roles",
        total_duration: 30,
        time_unit: "minutes",
      },
      events: [
        {
          event_id: "w0",
          sequence_index: 1,
          timestamp: 0,
          event_type: "rolling_horizon_window_started",
          entity_refs: { primary: "RH-00000" },
          payload: { window_index: 0, window_start_min: 0, window_end_min: 5 },
        },
        {
          event_id: "c0",
          sequence_index: 2,
          timestamp: 1,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-OLD", related: ["A3"] },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-OLD",
            worker_id: "A3",
            task_code: "MANAGE_ROBOT_POWER",
            effective_priority_rank: 1,
            rolling_task_signature: { target_id: "A3" },
          },
        },
        {
          event_id: "d0",
          sequence_index: 3,
          timestamp: 5,
          event_type: "rolling_horizon_dispatched",
          entity_refs: { primary: "RHOPP-OLD", target: "A3" },
          payload: {
            window_index: 0,
            opportunity_id: "RHOPP-OLD",
            assigned_worker_id: "A3",
            task_code: "MANAGE_ROBOT_POWER",
            effective_priority_rank: 1,
            rolling_task_signature: { target_id: "A3" },
          },
        },
        {
          event_id: "w3",
          sequence_index: 4,
          timestamp: 15,
          event_type: "rolling_horizon_window_started",
          entity_refs: { primary: "RH-00003" },
          payload: { window_index: 3, window_start_min: 15, window_end_min: 20 },
        },
        {
          event_id: "c3",
          sequence_index: 5,
          timestamp: 16,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-NEW", related: ["A1"] },
          payload: {
            window_index: 3,
            opportunity_id: "RHOPP-NEW",
            worker_id: "A1",
            task_code: "REPLENISH_MATERIAL",
            effective_priority_rank: 2,
            rolling_task_signature: { target_id: "station1", source_slot_id: "warehouse_material_slot_01" },
          },
        },
      ],
    });

    const model = buildRollingTaskPoolModel(parsed.events, 16);
    const oldDispatched = model.entries.find((entry) => entry.opportunityId === "RHOPP-OLD");
    const newPool = model.entries.find((entry) => entry.opportunityId === "RHOPP-NEW");

    expect(oldDispatched?.status).toBe("dispatched");
    expect(oldDispatched?.assignedWorkerId).toBe("A3");
    expect(newPool?.status).toBe("pool");
  });

  it("ignores rolling horizon window summary dispatch events in task pool rows", () => {
    const parsed = parseReplayLog({
      schema_version: "1.0",
      metadata: {
        run_id: "rolling-summary",
        domain: "manufacturing",
        decision_mode: "rolling_horizon_aging_priority",
        total_duration: 20,
        time_unit: "minutes",
      },
      events: [
        {
          event_id: "w1-dispatch-summary",
          sequence_index: 1,
          timestamp: 10,
          event_type: "rolling_horizon_dispatched",
          entity_refs: { primary: "RH-00001" },
          payload: {
            window_index: 1,
            candidate_count: 0,
            dispatch_count: 0,
            dispatch_policy: "aging_priority",
          },
        },
        {
          event_id: "w2",
          sequence_index: 2,
          timestamp: 10,
          event_type: "rolling_horizon_window_started",
          entity_refs: { primary: "RH-00002" },
          payload: { window_index: 2, window_start_min: 10, window_end_min: 15 },
        },
        {
          event_id: "c2",
          sequence_index: 3,
          timestamp: 14,
          event_type: "rolling_horizon_candidate_collected",
          entity_refs: { primary: "RHOPP-MAT3", related: ["A3"] },
          payload: {
            window_index: 2,
            opportunity_id: "RHOPP-MAT3",
            worker_id: "A3",
            task_code: "REPLENISH_MATERIAL",
            fixed_priority: 86,
            rolling_task_signature: { target_id: "station2", source_slot_id: "warehouse_material_slot_03", transfer_item_id: "MAT-WH-3" },
          },
        },
      ],
    });

    const model = buildRollingTaskPoolModel(parsed.events, 14);
    expect(model.entries).toHaveLength(1);
    expect(model.entries[0].opportunityId).toBe("RHOPP-MAT3");
    expect(model.entries[0].taskCode).toBe("REPLENISH_MATERIAL");
  });
});
