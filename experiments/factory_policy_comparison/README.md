# Factory Policy Comparison Experiment Suite

This folder runs a fair factory-scenario comparison across six ManSim decision modes:

- `fixed_priority`
- `adaptive_priority`
- `rolling_horizon_aging_priority`
- `rolling_horizon_dedicated_roles`
- `bottleneck_aware_dispatch`
- `rolling_horizon_throughput_optimizer`

The default experiment uses `factory_mfg_basic`, worker counts `3..8`, five seeds, and a five-day horizon.

This suite is part of ManSim v0.5. It compares policy outcomes; the Pre-Run Diagnostics values are intentionally policy-independent within the same worker-count group.

## Quick Start

Run these commands from the ManSim repository root after cloning ManSim and HumanoidSim as sibling folders. The complete fresh-install procedure is in the [root README](../../README.md#installation) and [Installation Guide](../../docs/installation.md). LLM, OpenClaw, Knowledge Graph, Streamlit, and Node.js packages are not required.

```powershell
cd C:\Github\ManSim
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\HumanoidSim
.\.venv\Scripts\python.exe -m humanoidsim validate-catalog
.\.venv\Scripts\python.exe -c "from ortools.sat.python import cp_model; print('OR-Tools OK')"
.\.venv\Scripts\python.exe -m pip check
```

Dry-run the 180 commands without creating outputs:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --dry-run
```

Run a small smoke test:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --worker-counts 3 --seeds 2026 --days 1
```

The smoke command runs all six policies once and verifies audit, aggregation, and automatic dashboard opening before the 180-run experiment.

Run the full comparison:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py
```

실험이 끝나면 run audit와 KPI audit, 결과 집계가 수행되고 `comparison_dashboard.html`이 기본 브라우저에서 자동으로 열립니다. Headless 환경에서는 다음처럼 자동 열기만 끌 수 있습니다.

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --no-open-dashboard
```

Artifact audit에는 worker 이동 path와 tile segment의 인접성, map 경계, wall/blocking footprint 침범, Replay `entity_moved` 경로 검사, item pickup/carry/drop과 destination state 연결 검사가 포함됩니다. 공정성, artifact 또는 KPI audit가 실패하면 dashboard는 조사용으로 생성되지만 runner는 non-zero exit code로 종료됩니다.

The full 180-run experiment exports each run's Hub, KPI, Gantt, logs, and replay payloads and can require well over 100 GB. Check free disk space before starting, or use `--worker-counts`, `--seeds`, and `--limit` for a smaller experiment.

Results are written to:

```text
experiments/factory_policy_comparison/results/<timestamp>/
```

Each run is stored as:

```text
runs/<decision_mode>/workers_<worker_count>/seed_<seed>/
```

## Outputs

- `run_status.csv`: command, status, elapsed time, and failure reason per run
- `fairness_report.csv`: seed/config/pre-run-diagnostics consistency checks
- `audit_summary.csv`: existing artifact/KPI audit results per run
- `comparison_summary.csv`: run-level KPI table
- `mode_worker_summary.csv`: mode × worker-count mean/std/min/max KPI table with marginal throughput gain
- `mode_summary.csv`: mode-level mean/std/min/max KPI table
- `comparison_summary.json`: machine-readable combined summary
- `comparison_dashboard.html`: browser dashboard linking the original ManSim Hub/KPI/Gantt artifacts

## Dedicated Roles Scaling

For `rolling_horizon_dedicated_roles`, the runner expands a fixed role template so all worker counts remain feasible:

- `G1 Supply`: `REPLENISH_MATERIAL`
- `G2 Machine`: `REPAIR_MACHINE`, `LOAD_MACHINE`, `SETUP_MACHINE`, `UNLOAD_MACHINE`
- `G3 Flow/QA/Power`: `MANAGE_ROBOT_POWER`, `TRANSFER`, `INSPECT_PRODUCT`, `COLLECT_WASTE_OR_SCRAP`, `PREVENTIVE_MAINTENANCE`

`A1..A3` receive `G1..G3`; `A4+` repeat `G1`, `G2`, `G3`. Battery delivery providers are workers with the `G3` role, and receivers are the remaining workers. This is a fixed template baseline, not role-map tuning.

## Fairness Checks

The suite checks that completed runs share:

- `scenario=factory_mfg_basic`
- the expected worker count for each mode/worker-count/seed run
- the same horizon and minutes per day
- the expected seed for each mode/worker-count/seed run
- matching policy-independent pre-run diagnostics fingerprints within each worker-count group
- empty `artifact_status.errors`
- no worker tile movement after battery is already depleted

Pre-run diagnostics are intentionally scenario-level indicators. They should match across policy modes within the same worker-count group, while different worker counts are expected to have different environment fingerprints.
