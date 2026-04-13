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

### `queue_add_contract`
- Top-level contract for worker-specific queue assignments.
- Meaning: planner must follow this nested shape when producing `queue_add`.

### `reason_trace_contract`
- Top-level contract for explanation trace entries.
- Meaning: planner must follow this shape when producing `reason_trace`.

### `decision_contract`
- Planning decision rules.
- Meaning: these define when `maintain`, `adjust`, and `queue_add` are valid.

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
- `current_weights`: active shared task-priority weights.
- `current_personal_queues`: existing worker-specific queued assignments.
- `current_agent_multipliers`: current per-worker overlays on top of shared task weights.
- Interpretation: this is the current operating plan, not binding truth.

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
- `review_status`: whether the diagnosis was approved normally or passed after revision budget exhaustion.
- `review_rounds`: number of evaluator review rounds used.
- Interpretation: planner should treat this as the reviewed diagnosis packet for the day, not as binding truth.

### `guardrails`
- `allowed_task_priority_keys`: only these task families may appear in planner outputs.
- `allowed_agent_ids`: only these worker ids may receive queue assignments or agent-specific adjustments.
- `task_priority_weight_range`: allowed min/max range for shared task weights.
- `agent_priority_multiplier_range`: allowed min/max range for per-agent multipliers.
- `allowed_quota_keys`: only these quota keys may appear if quotas are emitted.
- `quota_range`: allowed quota bounds, including `max_by_key`.
- `allowed_target_stations`: only these station ids may appear in queued work orders.
- `allowed_target_types`: only these target object types may appear in queued work orders.
- `queue_add_entry_contract`: required shape of each queued work-order object.
- `dispatch_expectation`: indicates whether the current state requires at least one concrete queued work order.
- `norm_targets`: current norm or rule targets the planner should avoid violating.
- Interpretation: this section defines the planner output boundary. Never invent ids, target types, task families, or quota keys outside guardrails.

## Workspace Memory

- `MEMORY.md`
- `memory/rolling_summary.md`

Meaning: planner may consult these compact run-local summaries when deciding whether a recurring issue or carry-over focus still deserves action today.
