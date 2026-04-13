# Bootstrap

- This workspace is scoped to the current simulation run only.
- `KNOWLEDGE.md` carries compact cross-run lessons from previous runs in the same experiment series.
- Runtime code refreshes the active facts and templates every turn.
- Static role files remain stable; only `USER.md` should drive the current turn.
- Interpret request-payload keys using `INPUT_KEY_GLOSSARY.md` before validating the reviewed diagnosis or planning actions.
- Interpret planner output keys using `OUTPUT_KEY_GLOSSARY.md` before filling the response JSON.
- Consult `KNOWLEDGE.md`, `MEMORY.md`, and `memory/rolling_summary.md` when deciding whether a carry-over focus still deserves action today.
- Plan with an execution, coordination, and tradeoff perspective rather than a fresh diagnosis perspective.
