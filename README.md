# Manufacturing Simulation (Hydra + SimPy)

## Run

```powershell
C:\Github\mansim\.venv\Scripts\python.exe -m pip install -r C:\Github\mansim\requirements.txt
C:\Github\mansim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main
```

## Policy Mode

- Default mode is `heuristic` (rule-based).
- Switch to LLM mode:

```powershell
C:\Github\mansim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm
```

Current LLM mode is a stub and will raise an explicit error until `decision/llm_optional.py` is connected to your LLM server.

## Heuristic Rules

All heuristic policy knobs are centralized in:

- `manufacturing_sim/simulation/conf/heuristic_rules/default.yaml`

You can override individual heuristic values with Hydra, for example:

```powershell
C:\Github\mansim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main heuristic_rules.world.battery.mandatory_swap_threshold_min=20
```

## Outputs

- `events.jsonl`: event log for replay
- `daily_summary.json`: day-level metrics
- `kpi.json`: final KPI summary
- `minute_snapshots.json`: minute-level system snapshots
- `gantt_segments.csv`: intervals for machine/agent gantt
- `gantt.html`: gantt chart
- `kpi_dashboard.html`: KPI dashboard chart bundle

By default, simulation run auto-opens:
- `kpi_dashboard.html`
- `gantt.html`
- Streamlit replay dashboard (`events.jsonl` of the latest run is passed via URL query)

## Replay UI

```powershell
C:\Github\mansim\.venv\Scripts\streamlit.exe run C:\Github\mansim\manufacturing_sim\simulation\scenarios\manufacturing\viz\replay_app.py
```
