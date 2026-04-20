# manufacturing_sim

`manufacturing_sim` is the simulator-core package.

It is intentionally limited to simulation responsibilities:
- world state transitions
- entities and processes
- event logging
- opportunity enumeration
- action application
- manufacturing scenario dynamics

Higher-level layers now live at the repository root:
- `runtime/`
- `agents/`
- `knowledge/`
- `dashboards/`
- `configs/`
- `openclaw/`

If you are changing LLM orchestration, prompt construction, ontology persistence, or artifact export, you should start at the repository root, not inside `manufacturing_sim/`.
