# Output Key Glossary

This glossary defines the meaning of the planner output keys.

## Planner Raw Output

### `plan_mode`
- Allowed values: `maintain`, `adjust`
- Meaning: planner-level decision about whether the active plan can continue as-is or needs a concrete change.

### `commitments`
- Type: `dict[str, list]`
- Meaning: worker-specific executable commitments.
- Required fields per commitment:
  - `opportunity_id`
  - `commitment_id`
  - `alternate_workers`
  - `dependencies`
  - `expiry_min`
  - `success_criteria`
  - `rationale`
- Interpretation:
  - `opportunity_id` must come directly from `input.opportunities`.
  - This is the authoritative execution output for `llm_planner`.
  - Do not invent target guesses outside the current opportunity board.

### `mailbox`
- Type: `dict[str, list]`
- Meaning: worker-specific coordination messages.
- Required fields per entry:
  - `to_agent`
  - `message_type`
  - `body`
- Interpretation:
  - Use for handover, dependency, coordination, watchout, or assist-request notices.
  - Mailbox is supplementary; it does not replace commitments.

### `incident_strategy`
- Type: `dict[str, Any]`
- Required fields:
  - `mode`
  - `focus_opportunity_ids`
  - `watchouts`
- Interpretation:
  - `mode` is typically `delta_replan` or `maintain`.
  - `focus_opportunity_ids` should identify the opportunities that matter most under the current incident context.
  - `watchouts` should remain compact and execution-relevant.

### `reason_trace`
- Type: `list[dict]`
- Meaning: structured explanation of why the planner chose the final intervention.
- Required fields per item:
  - `decision`
  - `reason`
  - `evidence`
  - `affected_agents`
  - `task_families`
  - `detector_relation`
- Interpretation:
  - Tie evidence to the current request payload.
  - `detector_relation` must explain whether the planner followed, rejected, or deprioritized the detector hypothesis.

### `detector_alignment`
- Allowed values: `follow`, `partial_override`, `override`
- Meaning: planner-level summary of how closely the final executable plan matches the reviewed diagnosis packet.

## Simulator-Normalized Plan

These fields are what the simulator actually executes after sanitizing and normalizing the raw planner output.

### `commitments`
- Meaning: final worker-specific executable commitments used by the runtime.
- Source: sanitized form of raw `commitments`, or synthesized fallback commitments when the planner output is inert or invalid.

### `mailbox`
- Meaning: final worker coordination messages used during execution.
- Source: sanitized form of raw `mailbox`.

### `incident_strategy`
- Meaning: final incident replanning guidance attached to the executable plan.
- Source: sanitized form of raw `incident_strategy`.

### `manager_summary`
- Meaning: short natural-language summary of the final plan.
- Source: derived from planner response or synthesized by the simulator when missing.

### `reason_trace`
- Meaning: final preserved explanation trace attached to the executable plan.
- Source: sanitized form of raw `reason_trace`.

### `detector_alignment`
- Meaning: final preserved detector/planner relationship label attached to the executable plan.
- Source: sanitized form of raw `detector_alignment`.

## Practical Rule

- For `llm_planner`, commitments are the authoritative execution contract.
- Mailbox supports coordination, not task dispatch on its own.
- If the planner emits an inert or invalid response, the simulator may synthesize fallback commitments from the canonical opportunity board.
