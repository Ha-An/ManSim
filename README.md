# ManSim

ManSim is a manufacturing-floor multi-agent simulation built with Hydra and SimPy. It supports rule-based scheduling, fixed-priority baselines, and an LLM-driven mode with townhall-style inter-agent discussion.

## Setup

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m pip install -r C:\Github\ManSim\requirements.txt
```

## Run

Current default config in `manufacturing_sim/simulation/conf/config.yaml` uses `decision=llm`.

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main
```

Recommended first run without an LLM server:

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=adaptive_priority
```

Fixed-priority baseline:

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=fixed_priority
```

LLM mode:

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm
```

If you use Ollama through WSL, the helper script below restarts the service and runs the simulation:

```powershell
.\run_llm.ps1
```

## Decision Modes

- `adaptive_priority`: rule-based controller that adjusts task/category priorities from daily outcomes and urgent events.
- `fixed_priority`: rule-based baseline that keeps task/category priorities fixed during the run.
- `llm`: LLM-backed controller with optional townhall communication.

Backward-compatible aliases are still supported:

- `heuristic` -> `adaptive_priority`
- `heuristic_fixed` -> `fixed_priority`

## Main Config Files

- `manufacturing_sim/simulation/conf/config.yaml`: root Hydra config, default decision mode, UI auto-open options.
- `manufacturing_sim/simulation/conf/experiment/mfg_basic.yaml`: factory layout, timings, battery policy, failure/quality parameters.
- `manufacturing_sim/simulation/conf/heuristic_rules/default.yaml`: task priorities, norms, urgent-response weights.
- `manufacturing_sim/simulation/conf/decision/*.yaml`: per-mode configuration presets.

Example override:

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=adaptive_priority heuristic_rules.world.battery.mandatory_swap_threshold_min=20
```

## Outputs

Each run writes to `outputs/YYYY-MM-DD/HH-MM-SS/`.

- `events.jsonl`: event log used by the replay UI.
- `daily_summary.json`: day-level summary metrics.
- `kpi.json`: final KPI summary.
- `run_meta.json`: run mode and LLM metadata.
- `minute_snapshots.json`: minute-level snapshots for replay/debugging.
- `gantt.html`: machine/agent timeline dashboard.
- `kpi_dashboard.html`: KPI dashboard.
- `task_priority_dashboard.html`: task-priority trend dashboard.
- `llm_trace.html`: LLM request/response trace dashboard for LLM runs.

By default, the simulation opens selected HTML artifacts plus the Streamlit replay UI after the run finishes.

## Replay UI

```powershell
C:\Github\ManSim\.venv\Scripts\streamlit.exe run C:\Github\ManSim\manufacturing_sim\simulation\scenarios\manufacturing\viz\replay_app.py
```
