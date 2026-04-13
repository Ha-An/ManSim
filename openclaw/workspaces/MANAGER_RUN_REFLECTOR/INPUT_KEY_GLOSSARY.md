# Input Key Glossary

## Request Envelope
- `phase`: current native-local phase id. For this workspace it is `manager_run_reflector`.
- `language`: natural-language output language for text fields.
- `role`: concise role summary for this run-level reflection turn.
- `input`: structured run-review packet.
- `required_keys`: JSON keys that must appear in the response.
- `instructions`: phase-specific operating instructions.
- `response_rule`: exact JSON-output rule.
- `language_rule`: language rule for natural-language fields.
- `knowledge_contract`: compact description of the expected reflection output shape.
- `decision_contract`: additional reflector-specific decision rules.

## Input Packet
- `run_context`: current run index, total runs, decision mode, and evaluator-enabled state.
- `prior_knowledge`: compressed `knowledge.md` content carried over from previous runs.
- `performance_summary`: compact KPI and day-level outcome summary for the finished run.
- `manager_behavior_summary`: detector, planner, and optional evaluator behavior trend over the finished run.
- `notable_failures`: compact summary of startup failures, recurring issues, repair-vs-PM skew, and manager execution gaps.
