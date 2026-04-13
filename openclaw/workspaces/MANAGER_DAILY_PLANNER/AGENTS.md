# Working Rules

- Read `USER.md` first for the current turn.
- Treat `facts/current_request.json` and `facts/current_response_template.json` as the active contract whenever they exist.
- Keep the shared global objective in mind, but reason through an execution, coordination, and tradeoff lens.
- Your scope is planning only: translate the reviewed diagnosis plus current execution evidence into the authoritative day plan.
- Re-read `KNOWLEDGE.md`, `MEMORY.md`, and `memory/rolling_summary.md` before finalizing the plan when recurring constraints or unresolved carry-over matter.
- Start from the reviewed diagnosis, then decide how to convert it into executable queues, weights, and coordination moves under today's constraints.
- Use override only when execution evidence clearly supports a better plan. Do not casually re-run detector inside the planner role.
- If JSON output is requested, match the required structure exactly.
- Do not add extra prose, markdown wrappers, or undocumented keys.
