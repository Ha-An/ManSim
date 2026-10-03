# mfg_flow_shop Confirmatory Policy Experiment

This suite prepares the pre-registered paper experiment separately from development and pilot results.

## Confirmatory Design

- Worker counts: 2, 3, 4, 5 and 6.
- Policies: Immediate Shared, Random Feasible and Simulation-Based ADP. The other heuristic policies can be
  appended later without retraining the saved ADP checkpoints.
- Objective: completed products over five 480-minute days.
- Rolling-horizon window: fixed at 5 minutes.
- Final environment seeds: 20 new common-random-number seeds, 910001 through 910020.
- ADP: one reusable fleet-specific model per worker count and one training run.
- WAIT is disabled for every policy. The scenario, timing, stochastic streams and physical feasibility
  contract are otherwise identical.

The compact design contains five ADP training runs and 300 final simulation runs. Training-seed variability
is not part of this design; uncertainty intervals describe the 20 common environment seeds for each fixed
worker-specific checkpoint.

## Prepare

```powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/prepare_experiment.py
```

This validates all seed partitions and writes the following under `prepared/`:

- `training_configs/workers_<N>/replicate_<R>.yaml`
- `training_plan.csv`
- `training_commands.ps1`
- `evaluation_plan.csv`
- `evaluation_commands.ps1`
- `experiment_plan.json`

Each generated training profile uses the same 800-episode one-run TD schedule. Screening is limited to
iterations 0, 15, 30, 45, 60 and 75 with ten seeds, followed by two final candidates with ten fresh seeds.
Only worker count, training seed and worker-specific validation seeds differ. Training should remain sequential by default
because every run already uses ten CPU rollout processes and `cuda:0` for value updates.

Run all five fleet-specific training jobs sequentially. Existing completed checkpoints are skipped. Each
successfully audited training dashboard opens in Chrome immediately after that worker count finishes. Because compact
replay is intentionally not persisted, an interrupted job cannot resume exactly: its partial directory is archived
and that one training replicate restarts from the beginning.

```powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_training.py --background
```

The command returns immediately. A local live monitor opens in Chrome and refreshes every five seconds,
showing the active worker count, phase, iteration, wave/episode progress, device and log tail. Each completed
training dashboard remains available through its existing link. The monitor is stored under
`background_training/<timestamp>/live_training.html`. Omit `--background` to keep the original blocking behavior.
See [background training](../../docs/adp_background_training.md) for stop/status commands. PC reboot or sleep
does not preserve an in-memory replay buffer; this is detached execution, not exact resume support.

After all five `best.pt` files exist, run the 300 final evaluations with five concurrent simulations.
Existing runs are skipped only after their KPI completion contract and both audits pass. Incomplete directories
are archived before that individual run restarts. Ctrl+C cancels queued evaluations after currently running jobs exit.

```powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_evaluation.py --jobs 5
```

Each completed evaluation is audited immediately. On Windows its directory is then compressed with NTFS
`compact.exe`; use `--skip-audit` or `--no-compress` only for diagnostics, not the final study.
After every expected run and audit passes, the runner creates `evaluation_raw.csv`, `fairness_report.csv`,
`policy_worker_summary.csv`, `paired_contrasts.csv`, `analysis_summary.json` and the standard
`policy_comparison_dashboard/comparison_dashboard.html`, then opens the dashboard. The dashboard reuses the
established `factory_policy_comparison` interface while leaving the confirmatory source tables unchanged.
Add `--no-open-dashboard` on a headless machine.
The dashboard also reports the configuration-derived theoretical maximum and realistic expected production
reference for every worker count, with the material, process-time and worker bounds used in the calculation.

The aggregation treats the 20 environment seeds as paired experimental units. ADP uses one fixed checkpoint
per worker count. Confidence intervals therefore quantify environment-seed uncertainty only; they do not
estimate variation caused by neural-network initialization or training seeds.

## Analysis Contract

### Extend A Completed Three-Policy Comparison

The optional extension adds Immediate Dedicated Roles, Rolling Horizon Shared and Rolling Horizon Dedicated
Roles on the archived worker/seed grid. It checks current non-policy Hydra settings against archived runs,
backs up the original plan, tables and dashboard, and leaves all trained checkpoints and completed runs intact.
The three-policy default configuration is unchanged.

```powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/extend_evaluation.py --prepared <result_directory>
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_evaluation.py --prepared <result_directory> --jobs 5 --modes immediate_dedicated_roles rolling_horizon_shared rolling_horizon_dedicated_roles
```

The filtered runner preserves the other policies' status and timing rows. Aggregation still requires the full
six-policy plan to pass. The updated standard dashboard retains mean-plus-standard-deviation bands and links
all 600 runs. Its elapsed time includes the original evaluation session and the extension, not the idle gap
between them. Additional policy comparisons are supplemental, not newly pre-registered hypotheses.

The primary response is five-day `total_products`. The primary contrast is ADP minus Immediate Shared.
Report policy-by-worker interaction, worker marginal gains, paired seed differences, paired-bootstrap
95% intervals over common test seeds, and Holm-corrected worker-level comparisons.

The confirmatory hypotheses are fixed before the final runs:

- H1: the equal-weight worker-2-through-6 mean of `ADP - Immediate Shared` is greater than zero.
- H2: policy performance has a worker-count interaction, reported as the change in each policy contrast as
  the fleet grows from two to six workers.
- Worker-level ADP contrasts against Immediate Shared and Random Feasible are secondary comparisons.

For each worker count, hold the trained ADP checkpoint fixed and pair all three policies by the same environment
seed. Use a paired bootstrap over those 20 seeds. Report means, standard deviations, 95% intervals, paired
effect sizes and raw seed-level values. Explicitly state that training-seed uncertainty was not estimated.

Secondary operational responses are lead time, worker execution and unavailable ratios, buffer blocking,
battery depletion/recovery, failures, repair response and preventive maintenance. These explain mechanisms;
they do not replace `total_products` as the pre-registered primary endpoint.

Do not use the repeatedly inspected development seeds 50001 through 50005 for confirmatory claims.
Any behavior-changing fix after final runs begin invalidates the complete affected experiment block.

## Expected Runtime And Storage

On the current workstation, the reduced checkpoint-validation schedule is expected to take roughly 9.5--16.5
hours per worker-specific training job. Five sequential jobs should take about 47--82 hours. The 300 final
evaluations should take about 6.5--9 hours with `--jobs 5`; audits and final aggregation add roughly 1--2 hours.
The practical total is approximately 55--93 hours, assuming no thermal throttling or competing workload.

Default NTFS compression is expected to keep the complete compact study near 4--6 GiB, including the five ADP
training outputs and 300 evaluations. Check free disk space before starting.

## Acceptance Checks

### Deep Audit

```powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/audit_study.py --prepared <result_directory>
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/audit_study.py --prepared <result_directory> --reproduce
```

The first command independently checks all run KPI values, daily totals, console errors,
worker Gantt coverage and utilization, and incompatible machine intervals. The optional
second command writes nine five-day qualification runs with detailed events into a separate
audit directory (the registered seeds include two low-product ADP cases). It never overwrites
the study's original runs. Detailed events can consume substantial disk space.

The September 26 audit found that the machine lifecycle could overwrite `UNDER_PM` and
process material during preventive maintenance. This is a simulation behavior bug, not a
chart-only issue. Historical results in `20260919_212305` are marked `requires_rerun`;
their descriptive statistics remain available but cannot establish corrected-policy superiority.
Retraining and a complete evaluation block in the repaired environment are required.
New run metadata and ADP environment fingerprints include `exclusive_pm_v1`. Resume
does not skip historical mfg-flow-shop results from the previous lifecycle contract,
and old checkpoints are rejected for new production runs. Qualification comparisons
recorded during this audit explicitly reused old models before enabling that guard;
they are diagnostic only, not corrected training or confirmatory evaluation.

Overall paired intervals resample a common seed block across all worker counts. Production
intervals in the standard dashboard use the paper CSV's registered bootstrap results, not a
second independently seeded calculation. P values use an approximate null-centered bootstrap
with a plus-one finite-resample correction; worker-level multiplicity uses Holm adjustment.
One training replicate has no estimable between-training standard deviation (blank, not zero).
Production reference calculations use the archived resolved configuration for paper results.
Aggregation `pass` and simulation validity are separate statuses.

- All runs must end with `objective_status=complete` at exactly 2,400 simulated minutes.
- Scenario, timing, reliability, buffer and policy-independent fingerprints must match within each
  worker-count/seed block.
- Buffer overflow, reservation leak, duplicated item/task, role violation and unexplained relocation counts
  must be zero in detailed-event qualification runs. Every final compact run must pass its available KPI and
  layout invariants; event-only continuity checks are not falsely reported as executed when detailed events are off.
- ADP checkpoints must declare exactly one supported worker count and all test seeds as held out.
- Failed runs are rerun with the same seed; they are never silently dropped or replaced.
