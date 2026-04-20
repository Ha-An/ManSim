# LLM Prompt Design

## OpenClaw Adaptive Priority
Current `openclaw_adaptive_priority` prompt stack is split by responsibility.

### Strategist
- intent-only
- chooses roles, focus, support intent, prevention targets, daily targets
- does not emit low-level policy maps

### Daily Reviewer
- diagnosis-only
- does not replay raw facts
- emits next-day correction signals only

### Compiler Boundary
Low-level policy is not an LLM responsibility in this mode.
A deterministic compiler converts strategist intent plus reviewer feedback into executable policy.
