# Output Key Glossary

## `summary`
Short explanation of the day-start operating intent.

## `worker_roles`
Canonical role per worker.

## `operating_focus`
One of `flow`, `reliability`, `closeout`, `battery`.

## `late_horizon_mode`
One of `normal`, `closeout_drive`, `reliability_guarded_closeout`, `battery_guarded_closeout`.

## `role_plan`
Sparse rationale for why each worker role was chosen.

## `support_plan`
Contains exactly one `primary_support_pair` and one `support_intent`.

## `prevention_targets`
Up to 2 canonical failure patterns to prevent today.

## `daily_targets`
Compact numeric targets for the day.

## `plan_revision`
Monotonic revision id for the compiled daily policy.

## Practical Rule
Emit intent only. The deterministic compiler will derive low-level execution policy.
