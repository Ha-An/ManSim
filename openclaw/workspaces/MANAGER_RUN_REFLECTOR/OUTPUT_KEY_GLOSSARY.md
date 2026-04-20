# Output Key Glossary

This glossary defines the meaning of the reflector output keys.

## Reflector Raw Output

### `summary`
- Type: `str`
- Meaning: one compact run-level summary of what went wrong and what should carry forward.
- Interpretation: summarize the dominant operational lesson from the finished run, not a day-level diagnosis.

### `run_problems`
- Type: `list[dict]`
- Meaning: core problems that materially weakened the run.
- Common fields per item:
  - `issue`
  - `impact`
- Interpretation:
  - `issue`: concise label for the problem that mattered at run scope.
  - `impact`: short explanation of how it hurt throughput, closure, or execution stability.

### `detector_should_have_done`
- Type: `list[str]`
- Meaning: detector-specific corrections that should improve future bottleneck ranking.
- Interpretation: use concise, actionable guidance tied to ranking, recurrence handling, or evidence use.

### `planner_should_have_done`
- Type: `list[str]`
- Meaning: planner-specific corrections that should improve future execution plans.
- Interpretation: use concise, actionable guidance tied to commitment quality, opportunity selection, or incident handling.

### `carry_forward_lessons`
- Type: `list[str]`
- Meaning: the strongest cross-run lessons that should persist into the next run.
- Interpretation: these become the highest-priority carry-over knowledge items.

### `detector_guidance`
- Type: `list[str]`
- Meaning: next-run guidance aimed specifically at the detector workspace.
- Interpretation: use when the detector should rank, explain, or stabilize a recurring issue differently.

### `planner_guidance`
- Type: `list[str]`
- Meaning: next-run guidance aimed specifically at the planner workspace.
- Interpretation: use when the planner should choose different commitments, escalation patterns, or intervention priorities.

### `open_watchouts`
- Type: `list[str]`
- Meaning: unresolved risks that the next run should keep visible even if they are not yet the top lesson.
- Interpretation: these are not firm lessons yet; they are live concerns that still deserve monitoring.

## Knowledge Merge Meaning

- `carry_forward_lessons`, `detector_guidance`, `planner_guidance`, and `open_watchouts` are merged into the run-level ontology and then rendered into `KNOWLEDGE.md`.
- `run_problems` are used to update recurring issue memory for future runs.
- Reflector output should remain compact, stable, and cross-run reusable.

## Practical Rule

- Reflector output is run-level learning, not day planning.
- Do not emit commitments, mailbox messages, or day bottleneck ranks from this workspace.
- Prefer stable wording for recurring lessons so cross-run accumulation stays interpretable.
