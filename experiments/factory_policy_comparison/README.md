# Manufacturing Policy Comparison Experiment Suite

The default profile compares the trained ADP policy with its two primary baselines.

- Scenario/objective: `mfg_flow_shop / maximize_throughput`
- Modes: `simulation_based_adp`, `immediate_shared`, `random_feasible_dispatch`
- Worker count: `3`
- Held-out seeds: `50001, 50002, 50003, 50004, 50005`
- This seed set is the locked longitudinal benchmark. Default policy comparisons must not substitute another seed set.
- Horizon: 5 days, 8 hours per day
- Total: `3 x 1 x 5 = 15` runs

The ADP checkpoint is required. All three policies use the same scenario, worker count, timing profile,
finite buffers, stochastic seed, no-WAIT contract, and horizon.

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

Inspect all 15 commands without creating results:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py `
  --adp-checkpoint C:\path\to\best.pt --dry-run
```

Run a three-policy smoke comparison on one held-out seed:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py `
  --adp-checkpoint C:\path\to\best.pt --limit 3
```

Run all 15 combinations:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py `
  --adp-checkpoint C:\path\to\best.pt --jobs 5
```

The runner audits and summarizes the results, creates `comparison_dashboard.html`, and opens it in the
default browser. Use `--no-open-dashboard` in a headless environment.

## Preserved Rule-Based Profile

The former 32-run experiment remains available as
`config_mfg_flow_shop_4policy_objectives.yaml`. It compares two objectives, worker counts 3 through 6,
one seed, and the four immediate/rolling shared/dedicated rule policies.

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py `
  --config experiments\factory_policy_comparison\config_mfg_flow_shop_4policy_objectives.yaml
```

## Default Held-Out Comparison

Training, checkpoint diagnostics, final checkpoint selection, and held-out test seeds are disjoint.

| Mode | Dispatch contract | Comparison role |
| --- | --- | --- |
| `random_feasible_dispatch` | Uniform choice among conflict-free feasible tasks; no explicit WAIT | Untrained stochastic baseline |
| `immediate_shared` | Fixed-priority choice whenever a worker becomes idle | Primary reactive rule baseline |
| `simulation_based_adp` | Attention post-decision value and cyclic-order beam-search joint matching | Learned policy |

The primary test is the paired completed-product difference between ADP and Immediate
Shared. A positive mean alone is not reported as superiority; the paired bootstrap 95% interval must
also have a lower bound above zero. Results therefore describe this experiment contract rather than a
universal ordering of dispatch policies.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-adp.txt
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py `
  --adp-checkpoint outputs/adp_training/<timestamp>/best.pt --jobs 5
```

The default trainer uses 10 CPU rollout processes, updates after every 10-episode wave, performs one
initial plus 50 policy updates, and keeps the existing checkpoint graph at iterations
`0, 1, 5, 10, ..., 50`.

This profile runs 15 simulations. An absent checkpoint, held-out seed overlap, or a scenario,
objective, timing, feature-schema, review-interval, cyclic worker-order, exact worker support, or worker-specific environment
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

The dashboard links each run's Hub, KPI dashboard, and Gantt. Policy experiments do not persist the large `events.jsonl` or heavy Replay JSON/HTML artifacts by default. KPI and Gantt artifacts are still generated from in-memory events, while fairness checks use the compact stochastic signature stored in `run_meta.json`. With one seed, the displayed mean equals the observed run value and sample standard deviation is not estimable.

All fleet-size line charts show the mean with a shaded +/-1 sample-standard-deviation band and error bars.
This describes variation across seeds, not a confidence interval for the mean. Nonnegative metrics clip the
displayed band at zero; ratios clip it to [0, 1]. Point tooltips retain the original mean, SD, variance and
sample count. Single-seed or missing-SD points have no band. Marginal throughput gains and makespan reductions
use the mean and SD of differences between matching valid seeds, divided by the number of added workers.
The first worker count has no marginal value. With incomplete seed sets, only common seeds contribute.

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
