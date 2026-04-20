# Output Key Glossary

This glossary defines the meaning of the detector output keys.

## Detector Raw Output

### `summary`
- Type: `str`
- Meaning: one compact diagnosis summary for the current day.
- Interpretation: summarize the strongest remaining-horizon bottleneck picture, not a task plan.

### `top_bottlenecks`
- Type: `list[dict]`
- Meaning: ranked bottleneck entries for the current detector turn.
- Required fields per item:
  - `name`
  - `rank`
  - `severity`
  - `evidence`
  - `why_it_limits_output`
- Interpretation:
  - `name`: bottleneck label for the constraint being ranked.
  - `rank`: relative order, where `1` is the strongest limiting bottleneck.
  - `severity`: `low | medium | high`.
  - `evidence`: compact supporting signals from the current request payload.
  - `why_it_limits_output`: short explanation of how the bottleneck restricts accepted finished-product completion.

## Practical Rule

- Detector output is diagnosis only.
- Do not emit commitments, mailbox messages, plans, or planner actions from this workspace.
- Keep bottleneck naming stable when the same recurring issue remains active across days or runs.
