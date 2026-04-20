# Input Key Glossary

This glossary describes the strategist request packet.

### `objective`
Global objective for the remaining horizon.

### `time_context`
Current day, days remaining, and remaining horizon minutes.

### `operating_state`
Compact operational state for backlog, buffers, battery health, reliability pressure, and closeout pressure.

### `opportunities`
Compact list of high-leverage opportunities visible today.

### `current_policy`
Snapshot of the currently active compiled policy.

### `current_policy_focus_summary`
Short textual summary of the current compiled policy focus.

### `previous_day_review`
Diagnosis-only reviewer output from the prior day.
Use this as correction guidance, not as a raw fact dump.

### `norm_targets`
Shared norm targets available to the strategist.

### `refresh_context`
Optional day-start refresh context when strategist is explicitly re-entered.
