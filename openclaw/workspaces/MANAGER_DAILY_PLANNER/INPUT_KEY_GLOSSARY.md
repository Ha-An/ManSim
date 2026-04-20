# Input Key Glossary

This glossary documents the planner request exactly as it appears in `facts/current_request.json`.

## Request Envelope

### `phase`
- Fixed phase id for this turn.
- Meaning: tells the workspace that the current turn is a planner turn.

### `role`
- Short runtime role summary.
- Meaning: defines planner scope as execution planning, not diagnosis generation.

### `input`
- The actual planner packet.
- Meaning: this is the current planning evidence packet.

### `required_keys`
- List of required response keys.
- Meaning: planner output must include these keys and match the response template exactly.

### `response_rule`
- Output-format instruction.
- Meaning: return one JSON object that matches `facts/current_response_template.json`.

### `language_rule`
- Language rule for natural-language values.
- Meaning: prose values must follow the configured language rule while JSON keys remain English.

### `instructions`
- Turn-specific runtime instructions.
- Meaning: these define the active planning contract for the current turn.

### `reason_trace_contract`
- Structural contract for each `reason_trace` entry.
- Meaning: planner must follow this shape when explaining why it changed or maintained the plan.

### `commitment_contract`
- Structural contract for worker-specific executable commitments.
- Meaning: planner must reference canonical `opportunities.opportunity_id` values directly.

### `mailbox_contract`
- Structural contract for worker mailbox entries.
- Meaning: planner may send handover, dependency, coordination, or watchout messages to specific workers.

### `decision_contract`
- Planning decision rules.
- Meaning: these define when `maintain` or `adjust` is valid and how commitments must be grounded.

## Input Packet

### `objective`
- `global_goal`: shared plant-level objective.
- Interpretation: choose the plan that most improves accepted finished-product completion over the remaining horizon.

### `time_context`
- `day`: current simulation day.
- `days_remaining`: remaining day count.
- `horizon_remaining_min`: remaining simulated minutes in the full horizon.
- Interpretation: use this as time context for planning urgency and tradeoffs.

### `execution_state`
- `days_remaining`: same horizon reminder for the planner.
- `current_plan_revision`: active plan revision id.
- Interpretation: this is the current execution-control state, not a free-form planning template.

### `closure_signals`
- `inspection_backlog`: finished products waiting for inspection.
- `station1_output_buffer`: output blockage after station 1.
- `station2_output_buffer`: output blockage after station 2.
- `completed_products_last_window`: recent accepted completions.
- `inspection_passes_last_window`: recent inspection completions.
- `active_inspection_agents`: workers currently assigned to inspection.
- Interpretation: these are the strongest throughput-closing signals for the planner.

### `constraint_signals`
- `missing_material`: material starvation count.
- `missing_intermediate_input`: upstream intermediate starvation count.
- `waiting_unload`: post-process unload blockage count.
- `broken_machines`: broken machine count.
- `low_battery_agents`: worker battery-risk count.
- Interpretation: these justify concrete interventions that relieve closure pressure.

### `detector_hypothesis`
- `summary`: reviewed diagnosis summary.
- `top_bottlenecks`: reviewed ranked bottleneck list.
- `review_status`: whether the diagnosis was accepted normally or passed after revision budget exhaustion.
- `review_rounds`: number of evaluator review rounds used.
- `top_bottlenecks[*].related_opportunity_ids`: planner-facing opportunity ids associated with each detected bottleneck.
- Interpretation: planner should treat this as the reviewed diagnosis packet for the day, not as binding truth.

### `opportunities`
- Canonical opportunity board emitted by the simulator.
- Required fields per item include:
  - `opportunity_id`
  - `task_family`
  - `target_type`
  - `target_id`
  - `target_station`
  - `shareable`
  - `capacity`
  - `owners`
  - `why_available`
  - `expected_output_impact`
- Interpretation: planner must build commitments by pointing directly to these opportunities. Do not invent targets outside this board.

### `active_commitments`
- Current executable commitments already active in the runtime.
- Interpretation: use this to avoid duplicate assignment, conflicting ownership, or stale rework.

### `incident_context`
- Mid-run or carry-over coordination state relevant to replanning.
- Common fields include:
  - `active_blockers`
  - `recent_incidents`
  - `escalation_reason`
- Interpretation: use this when a delta replan is needed after local worker recovery was insufficient.

### `guardrails`
- `allowed_task_priority_keys`: only these task families may appear in commitments and reason traces.
- `allowed_agent_ids`: only these worker ids may receive commitments or mailbox messages.
- `allowed_target_stations`: only these station ids may appear when a station target is referenced.
- `allowed_target_types`: only executable target object types may appear.
- `commitment_entry_contract`: required shape of each commitment object.
- `mailbox_entry_contract`: required shape of each mailbox message.
- `incident_strategy_contract`: required shape of `incident_strategy`.
- `norm_targets`: current rule or norm targets that the planner should avoid violating.
- Interpretation: this section defines the planner output boundary. Never invent ids, target types, task families, or mailbox message types outside guardrails.

## Workspace Memory

- `KNOWLEDGE.md`
- `MEMORY.md`
- `memory/rolling_summary.md`

Meaning: planner may consult cross-run knowledge and run-local summaries when deciding whether a recurring issue or carry-over focus still deserves action today.
