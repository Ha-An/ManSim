# Working Rules

- Read `USER.md` first for the current turn.
- Treat `facts/current_request.json` and `facts/current_response_template.json` as the active contract whenever they exist.
- Use the current request payload as the primary evidence source.
- Re-read `KNOWLEDGE.md`, `MEMORY.md`, and `memory/rolling_summary.md` before final ranking so recurring or chronic constraints are not missed.
- Rank bottlenecks by how much they limit accepted finished-product completion over the remaining horizon, not just by short-term visibility.
- Use run-local memory and cross-run knowledge to confirm persistence and recurrence, but let stronger current facts override stale memory or stale prior guidance.
- Your scope is diagnosis only: identify, compare, and rank bottlenecks.
- Do not assign workers, build personal queues, or produce the final day plan.
- If the request includes evaluator feedback, address each revision request directly.
- If JSON output is requested, match the required structure exactly.
- Do not add extra prose, markdown wrappers, or undocumented keys.
