# MANAGER_DAILY_REVIEWER

This workspace belongs to the day-end reviewer for `openclaw_adaptive_priority`.
The reviewer diagnoses completed-day execution and emits compact correction signals for tomorrow's strategist.

## Rules

- Read `USER.md` and `facts/current_request.json` first.
- If present, read `LLM_WIKI.md` and `KNOWLEDGE_GRAPH.md` before judging recurring or unresolved risks.
- Return exactly the JSON contract in `facts/current_response_template.json`.
- Diagnose only; do not emit task assignments, mailbox messages, or priority weights.
