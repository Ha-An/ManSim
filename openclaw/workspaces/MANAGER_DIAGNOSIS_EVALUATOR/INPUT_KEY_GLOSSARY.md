# Input Key Glossary

This glossary documents the evaluator request exactly as it appears in `facts/current_request.json`.

## Request Envelope

### `phase`
- Fixed phase id for this turn.
- Meaning: tells the workspace that the current turn is an evaluator turn.

### `role`
- Short runtime role summary.
- Meaning: defines evaluator scope as diagnosis-quality review, not planning.

### `input`
- The actual evaluator packet.
- Meaning: this is the evidence packet and detector draft being reviewed.

### `required_keys`
- List of required response keys.
- Meaning: evaluator output must include these keys and match the response template exactly.

### `response_rule`
- Output-format instruction.
- Meaning: return one JSON object that matches `facts/current_response_template.json`.

### `language_rule`
- Language rule for natural-language values.
- Meaning: prose values must follow the configured language rule while JSON keys remain English.

### `instructions`
- Turn-specific runtime instructions.
- Meaning: these define the active review contract for the current turn.

### `review_contract`
- Structural contract for evaluator review output.
- Meaning: evaluator must follow this shape when returning `revision_requests`.

### `decision_contract`
- Review decision rules.
- Meaning: these define when `accept` or `request_revision` is valid.

### `examples`
- Runtime examples for the current contract.
- Meaning: examples illustrate the expected JSON shape, not the only acceptable review content.

## Input Packet

### `objective`
- `global_goal`: shared plant-level objective.
- Interpretation: evaluate whether the detector draft is good enough to support that objective.

### `time_context`
- `day`: current simulation day.
- `days_remaining`: remaining day count.
- `horizon_remaining_min`: remaining simulated minutes.
- Interpretation: use this as time context when judging whether the diagnosis is well ranked for the remaining horizon.

### `throughput_closure_state`
- Same operational meaning as the detector packet.
- Interpretation: use this as current throughput evidence when judging the detector draft.

### `constraint_state`
- Same operational meaning as the detector packet.
- Interpretation: use this to verify whether machine and worker constraints are ranked and explained appropriately.

### `supporting_detail`
- Same operational meaning as the detector packet.
- Interpretation: use this to ground review comments and compare close bottleneck candidates.

### `detector_draft`
- `summary`: detector's current diagnosis summary.
- `top_bottlenecks`: detector's current ranked bottleneck list.
- Interpretation: this is the object being reviewed.

### `review_context`
- `round_index`: current evaluator review round.
- `max_revision_requests`: configured revision budget.
- Interpretation: this tells evaluator how many review loops remain.

## Workspace Memory

### `MEMORY.md`
- Compressed prompt-facing summary of recent diagnosis reviews.

### `memory/rolling_summary.md`
- Compact rolling review summary for the current run.

### `machine_recurrence_summary`
- Recurring machine issue summary stored inside evaluator memory artifacts.
- Meaning: use it to check whether repeated machine problems are being ignored or weakly explained.
