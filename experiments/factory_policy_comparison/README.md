# Manufacturing Policy Comparison Experiment Suite

This folder runs the default `mfg_flow_shop` policy comparison across two objectives, four fleet sizes, and four decision modes.

- Objectives: `maximize_throughput`, `minimize_makespan`
- Modes: `immediate_shared`, `immediate_dedicated_roles`, `rolling_horizon_shared`, `rolling_horizon_dedicated_roles`
- Worker counts: `3, 4, 5, 6`
- Seed: `2026`
- Throughput horizon: 5 days
- Shift length: 8 hours (`480` simulation minutes per day)
- Makespan safety limit: 30 days
- Total: `2 x 4 x 4 x 1 = 32` runs

The single-seed result is a deterministic descriptive comparison. It does not estimate statistical uncertainty.

## Installation

Run from the ManSim repository root after cloning ManSim and HumanoidSim as sibling folders. LLM, OpenClaw, Knowledge Graph, Streamlit, and Node.js packages are not required.

```powershell
cd C:\Github\ManSim
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\HumanoidSim
.\.venv\Scripts\python.exe -m humanoidsim validate-catalog
.\.venv\Scripts\python.exe -m pip check
```

The complete setup procedure is in the [root README](../../README.md#installation) and [Installation Guide](../../docs/installation.md).

## Run The Experiment

Inspect all 32 commands without creating results:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --dry-run
```

Run an 8-run smoke comparison using three workers:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --worker-counts 3
```

Run all 32 combinations:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py
```

Run only one objective or override its limit:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --objectives maximize_throughput --days 5
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --objectives minimize_makespan --makespan-max-days 30
```

After all runs finish, the runner audits and summarizes the results, creates `comparison_dashboard.html`, and opens it in the default browser. Use `--no-open-dashboard` in a headless environment.

## Worker-3 ADP Held-Out Comparison

The four rule-based modes remain available without PyTorch. The ADP research profile compares a
worker-3 checkpoint against a random feasible baseline and the four rule-based modes on five held-out
seeds. Training, screening, final checkpoint selection, and held-out test seeds are disjoint.

| Mode | Dispatch contract | Comparison role |
| --- | --- | --- |
| `random_feasible_dispatch` | Uniform choice among WAIT and conflict-free feasible tasks | Untrained stochastic baseline |
| `immediate_shared` | Fixed-priority choice whenever a worker becomes idle | Primary reactive rule baseline |
| `immediate_dedicated_roles` | Immediate choice constrained by exclusive task-rule ownership | Reactive specialization baseline |
| `rolling_horizon_shared` | Strict-periodic global reallocation with shared roles | Periodic planning baseline |
| `rolling_horizon_dedicated_roles` | Strict-periodic reallocation under fixed LPT roles | Periodic specialization baseline |
| `simulation_based_adp` | Attention post-decision value and beam-search joint matching | Learned policy |

All six modes use the same scenario, worker count, timing profile, finite buffers, stochastic seed, and
five-day horizon. The primary test is the paired completed-product difference between ADP and Immediate
Shared. A positive mean alone is not reported as superiority; the paired bootstrap 95% interval must
also have a lower bound above zero. Results therefore describe this experiment contract rather than a
universal ordering of dispatch policies.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-adp.txt
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train `
  --config configs/adp/mfg_flow_shop_throughput_10x100.yaml
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py `
  --config experiments/factory_policy_comparison/config_mfg_flow_shop_worker3_adp.yaml `
  --adp-checkpoint outputs/adp_training/<timestamp>/best.pt
```

This profile runs 30 simulations: six modes, five seeds, worker count 3, and the same 5-day
`maximize_throughput` objective. An absent checkpoint, held-out seed overlap, or a scenario,
objective, timing, feature-schema, review-interval, or worker-range
mismatch stops the ADP run; it never falls back to a rule-based policy. Tiny smoke checkpoints only
validate the execution contract and are not meaningful performance baselines.

The generated `paired_comparison.csv` and dashboard report seed-paired differences, deterministic
10,000-resample bootstrap 95% intervals, win/tie/loss counts, relative improvement, and the primary
`simulation_based_adp - immediate_shared` superiority verdict. Superiority is reported only when the
mean product difference is positive and the paired interval lower bound is above zero.

## Result Layout

Results are written to:

```text
experiments/factory_policy_comparison/results/<timestamp>/
```

Each objective-aware run is stored as:

```text
runs/<objective>/<decision_mode>/workers_<worker_count>/seed_2026/
```

Key outputs:

- `run_status.csv`: command status, failure reason, and elapsed time
- `fairness_report.csv`: objective/config/seed/timing/stochastic consistency checks
- `audit_summary.csv`: artifact and KPI audit results
- `comparison_summary.csv`: run-level objective and KPI values
- `mode_worker_summary.csv`: objective x mode x worker-count summary and marginal change
- `mode_summary.csv`: objective x mode summary
- `paired_comparison.csv`: seed-paired ADP differences, bootstrap intervals, and win/tie/loss
- `comparison_summary.json`: machine-readable combined result
- `theoretical_capacity.json`: worker-count-specific ideal upper bound, realistic expected production reference, and calculation components
- `comparison_dashboard.html`: integrated Throughput and Makespan tabs with run links

The dashboard links each run's Hub, KPI dashboard, and Gantt. Policy experiments do not persist the large `events.jsonl` or heavy Replay JSON/HTML artifacts by default. KPI and Gantt artifacts are still generated from in-memory events, while fairness checks use the compact stochastic signature stored in `run_meta.json`. With one seed, the displayed mean equals the observed run value and standard deviation is zero.

For `mfg_flow_shop`, the dashboard also reports a theoretical maximum production count and a theoretical minimum initial-batch makespan. This ideal deterministic bound uses triangular minimum times, all mandatory production tasks, shortest-path item movement with load multipliers, two parallel machines at each processing station, configured finite-buffer capacities, the single Inspection resource, pooled worker workload, charging duty cycle, startup lead time, and material availability. Failures, defects, incidents, dynamic traffic waits, and avoidable deadhead travel are relaxed, so the value is an optimistic capacity ceiling rather than an expected result.

The dashboard also reports a policy-independent realistic expected production reference. It uses triangular expected times, four-machine availability and repair labor, direct-charge duty cycle, inspection yield, and first-order incident recovery burden. Its standard-work cadence conservatively serializes Station 2 release with downstream transport, inspection, and terminal delivery, so it is neither a hard upper bound nor a fitted policy result. Finite-buffer blocking, dynamic traffic, queue starvation, dispatch delay, role imbalance, and policy-specific overlap remain visible only in the observed experiment results. The report stores both expected process completions before quality loss and expected accepted products after the configured defect probability.

Enable both event persistence and replay export only when per-run visual replay or event-level forensic auditing is required:

```powershell
runtime.artifacts.export_events=true runtime.ui.export_replay_artifacts=true
```

Event and Replay retention changes artifact generation and disk use, not simulation decisions or KPI values.

## Fairness And Failure Rules

Fairness is checked within each `objective x worker_count x seed` group. The suite requires:

- identical scenario, seed, worker count, timing fingerprint, and policy-independent diagnostics
- matching quality and machine-repair random-stream prefixes across policies
- exactly 2400 simulation minutes and 480-minute daily restocking for Throughput
- one initial fill, no later restocking, and terminal material lineage `30/30` for Makespan
- empty artifact errors and passing artifact/KPI audits
- no role violation, duplicate task ownership, invalid charging dock use, collision, or discontinuous worker movement

An incomplete Makespan run or a missing combination causes the experiment command to return a non-zero exit code. A diagnostic dashboard is still generated when possible.

## Legacy Six-Policy Factory Profile

The previous 180-run `factory_mfg_basic` experiment remains available:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --config experiments\factory_policy_comparison\config_factory_mfg_basic.yaml
```

Legacy outputs without an objective directory remain discoverable by the audit, summary, and dashboard tools.
