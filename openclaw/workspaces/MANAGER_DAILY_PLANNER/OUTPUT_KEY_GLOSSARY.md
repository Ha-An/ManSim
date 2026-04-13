# Output Key Glossary

This glossary defines the meaning of the planner output keys.

## LLM Raw Output

### `plan_mode`
- Allowed values: `maintain`, `adjust`
- Meaning: High-level statement of whether current request evidence and relevant run-local memory support keeping the active plan or changing it.
- Interpretation:
  - `maintain`: Use only when no materially stronger intervention is justified by current request evidence and relevant run-local memory.
  - `adjust`: Use when current request evidence supports a concrete change in priorities or assignments.

### `weight_updates`
- Type: `dict[str, float]`
- Meaning: Temporary task-family weight adjustments proposed for today.
- Interpretation:
  - Keys must come from `guardrails.allowed_task_priority_keys`.
  - Higher values increase the shared priority of that task family.
  - Use this when planner wants to bias dispatcher behavior broadly, not just assign one specific action.

### `queue_add`
- Type: `dict[str, list]`
- Meaning: Worker-specific concrete assignments to append to each worker's personal queue.
- Interpretation:
  - Prefer this when current request evidence supports a specific next action for a specific worker.
  - Each work-order object should identify the task family, target type, target id, target station when relevant, and a short reason.
  - This is more concrete than `weight_updates`.

### `reason_trace`
- Type: `list[dict]`
- Meaning: Structured explanation of why the planner chose the proposed intervention.
- Required fields per item:
  - `decision`
  - `reason`
  - `evidence`
  - `affected_agents`
  - `task_families`
  - `detector_relation`
- Interpretation:
  - Use `follow`, `reject`, or `deprioritize` in `detector_relation` to explain how the planner treated the detector hypothesis.
  - Keep evidence tied to the current request payload and any directly relevant run-local memory references.

### `detector_alignment`
- Allowed values: `follow`, `partial_override`, `override`
- Meaning: Planner-level summary of how closely the final day plan matches the reviewed diagnosis packet.
- Interpretation:
  - `follow`: Detector diagnosis remains the main driver of the day plan.
  - `partial_override`: Detector is partly accepted, but planner changed the operational emphasis.
  - `override`: Planner concluded a different intervention is more important than the detector's main diagnosis.

## Simulator-Normalized Plan

These fields are what the simulator actually executes after sanitizing and normalizing the raw planner output.

### `task_priority_weights`
- Meaning: Final shared task-family weights after planner updates are applied.
- Source: Mostly derived from `weight_updates`, then clamped to allowed ranges.

### `personal_queues`
- Meaning: Final worker-specific work-order queues used by the runtime.
- Source: Sanitized and normalized form of `queue_add`.

### `mailbox`
- Meaning: Final worker handover/dependency messages used during execution.
- Source: Built by the simulator, not currently a direct planner raw-output field.

### `parallel_groups`
- Meaning: Optional groups of work intended to run in parallel when dependencies allow it.
- Source: Optional planner output, then sanitized by the simulator.

### `agent_priority_multipliers`
- Meaning: Final per-worker overlays on top of shared task weights.
- Source: Built or blended by the simulator from planner-visible updates and runtime defaults.

### `manager_summary`
- Meaning: Short planner-facing natural-language summary of the final day plan.
- Source: Derived from planner response or synthesized by the simulator when missing.

### `reason_trace`
- Meaning: Final preserved explanation trace attached to the executable plan.
- Source: Sanitized form of raw `reason_trace`.

### `detector_alignment`
- Meaning: Final preserved detector/planner relationship label attached to the executable plan.
- Source: Sanitized form of raw `detector_alignment`.

## Practical Rule

- Use `queue_add` when you can justify a specific worker-specific next action from current request evidence.
- Use `weight_updates` when the right intervention is broader than one immediate queue item.
- Use both together when the day needs an immediate concrete action plus a broader operating bias.


