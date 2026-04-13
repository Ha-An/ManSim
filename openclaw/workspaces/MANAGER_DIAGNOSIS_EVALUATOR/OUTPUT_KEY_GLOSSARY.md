# Output Key Glossary

This glossary defines the meaning of the evaluator output keys.

## Evaluator Raw Output

### `verdict`
- Allowed values: `accept`, `request_revision`
- Meaning: Whether the current detector draft is sufficiently grounded for planning.
- Interpretation:
  - `accept`: The diagnosis is good enough to pass to the planner as today's reviewed diagnosis.
  - `request_revision`: The diagnosis has one or more concrete quality issues that should be corrected before planning.

### `summary`
- Type: `str`
- Meaning: Short evaluator-level explanation of the verdict.
- Interpretation:
  - For `accept`, summarize why the diagnosis is sufficiently grounded.
  - For `request_revision`, summarize the main deficiency or deficiencies.

### `revision_requests`
- Type: `list[dict]`
- Meaning: Actionable correction requests for the detector.
- Required fields per item:
  - `target_rank`
  - `issue_type`
  - `issue`
  - `requested_change`
  - `evidence`
- Interpretation:
  - `target_rank`: Which ranked bottleneck the issue primarily concerns.
  - `issue_type`: Short category label for the defect.
  - `issue`: What is wrong with the current detector draft.
  - `requested_change`: Concrete correction the detector should make.
  - `evidence`: Supporting signals from today's request payload that justify the correction.
- Rule:
  - If `verdict=accept`, this list must be empty.
  - If `verdict=request_revision`, this list must contain at least one actionable item.

## Practical Rule

- Accept only when the detector draft is already good enough for planning.
- Request revision only when there is a specific diagnosis-quality problem that can be corrected.
- Never output day plans, queue assignments, or task weights from this workspace.
