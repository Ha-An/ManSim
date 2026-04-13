# Input Key Glossary

This glossary documents the detector request exactly as it appears in `facts/current_request.json`.

## Request Envelope

### `phase`
- Fixed phase id for this turn.
- Meaning: tells the workspace that the current turn is a detector turn.

### `role`
- Short role summary injected by runtime.
- Meaning: defines the detector's local responsibility for this turn.

### `input`
- The actual detector packet.
- Meaning: this is the primary evidence payload to rank bottlenecks from.

### `required_keys`
- List of JSON keys that must appear in the response.
- Meaning: detector output must include these keys and no required key may be omitted.

### `response_rule`
- Output-format instruction.
- Meaning: return one JSON object that matches `facts/current_response_template.json`.

### `language_rule`
- Language rule for natural-language values.
- Meaning: prose values must follow the configured language rule while JSON keys remain English.

### `instructions`
- Turn-specific runtime instructions.
- Meaning: these override older assumptions and should be treated as the active behavioral contract.

### `bottleneck_contract`
- Structural contract for each `top_bottlenecks` entry.
- Meaning: detector must follow this exact nested shape when returning ranked bottlenecks.

### `count_rule`
- Required bottleneck count rule.
- Meaning: detector must return the configured number of ranked bottlenecks.

### `examples`
- Runtime examples for the current contract.
- Meaning: examples illustrate the expected JSON shape, not the only valid bottleneck content.

## Input Packet

### `objective`
- `global_goal`: shared plant-level objective.
- Interpretation: rank bottlenecks by how much they constrain accepted finished-product completion over the remaining horizon.

### `time_context`
- `day`: current simulation day.
- `days_remaining`: remaining day count in the horizon.
- `horizon_remaining_min`: remaining simulated minutes in the horizon.
- Interpretation: use this as time context for ranking remaining-horizon impact.

### `throughput_closure_state`
- `inspection_backlog`: finished products waiting for inspection.
- `station1_output_buffer`: output blockage after station 1.
- `station2_output_buffer`: output blockage after station 2.
- `completed_products_total`: accepted completed products so far.
- `completed_products_last_window`: recent accepted completions.
- `inspection_passes_last_window`: recent inspection completions.
- `active_inspection_agents`: workers currently assigned to inspection.
- `inspection_input_queue`: queue length in front of inspection.
- Interpretation: these are downstream throughput and closure signals.

### `constraint_state`
#### `machine_constraints`
- `wait_input_total`: machines blocked by missing input.
- `finished_wait_unload_total`: finished machines still blocked until unloading.
- `missing_material`: machines waiting for raw material.
- `missing_intermediate_input`: machines waiting for upstream intermediates.
- `waiting_unload`: machines waiting for unload action.
- `ready_for_setup`: machines that could start after setup.
- `broken`: broken machines.

#### `worker_constraints`
- `low_battery_agents`: workers near battery depletion.
- `discharged_agents`: workers already discharged.
- `idle_agents`: workers with no current work.

- Interpretation: these explain why throughput is weak or why a bottleneck persists.

### `supporting_detail`
- `material_queues`: station-level material queue lengths.
- `intermediate_queues`: station-level intermediate queue lengths.
- `machines_waiting_unload`: concrete machine ids waiting for unload.
- `broken_machine_count`: total broken machine count.
- `last_day_products`: accepted products completed on the previous day.
- `queue_delta`: recent queue movement by queue or stage.
- `machine_focus`: compact machine-level evidence snapshots.
- `agent_focus`: compact worker-level evidence snapshots.
- Interpretation: use this section to ground or disambiguate ranked bottlenecks.

### `prior_detector_draft`
- Optional previous detector draft during a revision turn.
- Meaning: compare the earlier draft against current evidence and evaluator feedback.

### `evaluator_feedback`
- Optional evaluator feedback during a revision turn.
- Meaning: revision requests here are mandatory quality corrections to address.

### `review_context`
- Optional revision metadata.
- `revision_index`: current detector revision count.
- `max_revision_requests`: configured evaluator revision budget.
- Interpretation: relevant only during detector revision turns.

## Workspace Memory

- `MEMORY.md`
- `memory/rolling_summary.md`

Meaning: detector should re-read these prompt-facing summaries to check recurring or chronic issues, but current request facts override stale memory when they conflict.
