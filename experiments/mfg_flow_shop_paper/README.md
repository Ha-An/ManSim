# mfg_flow_shop Confirmatory Policy Experiment

This suite prepares the pre-registered paper experiment separately from development and pilot results.

## Material Supply Sensitivity (20 vs 40)

`inventory_study.py` reuses the existing evaluator, live monitor and comparison dashboard.
It compares Immediate Shared and Random Feasible at workers 2--6, seeds 910001--910100,
over five 480-minute days: 1,000 runs per condition, 2,000 total. No ADP training is performed.
Both conditions use 40 physical shelf slots in a 26-by-15-tile Warehouse, with four rows of
ten slots and the bottom doorway at y=18. Only initial fill and daily top-up target differ.
Daily top-up fills the shelf to 20 or 40; it does not add that many materials unconditionally.
The ordinary 30-slot scenario is unchanged. Compare these two new conditions to each other;
old results used a different Warehouse doorway and are not a layout-controlled comparison.

```powershell
# Qualification: eight five-day runs with events for movement/item audits.
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/inventory_study.py `
  --output <new_smoke_directory> --smoke --run --no-open-dashboard
# Full study: ten processes total; the two conditions run sequentially.
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/inventory_study.py `
  --output <new_study_directory> --run
# After interruption, skip only completed runs whose settings and audits match.
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/inventory_study.py `
  --output <existing_study_directory> --resume --run
```

Each `materials_20/` or `materials_40/` directory contains `live_evaluation.html` and,
after all runs and fairness checks pass, `policy_comparison_dashboard/comparison_dashboard.html`.
Final dashboards open automatically. Detailed events and Replay exports are disabled in the
2,000-run study; event-level movement audits are limited to the separate qualification runs.
For detached Windows execution, launch the same command with `Start-Process -WindowStyle Hidden`
and redirected stdout/stderr. This does not change the simulation or statistical protocol.

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

### Evaluate Existing Fleet-Specific Checkpoints

Completed external training directories can be evaluated without retraining or modifying their checkpoints:

```powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/prepare_saved_checkpoints.py `
  --training-roots <worker2_training> <worker3_training> <worker4_training> <worker5_training> <worker6_training> `
  --output <new_result_directory> --previous-result <old_result_directory> `
  --allow-undeclared-held-out-seeds
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_evaluation.py `
  --prepared <new_result_directory> --jobs 10 --allow-undeclared-held-out-seeds
```

The preparation audits all five training outputs, validates checkpoint compatibility against the current
runtime, and records source paths, SHA-256 digests, actual training budgets and validation configurations.
The default evaluates three policies (300 runs); add `--six-policies` during preparation for all six (600).
It does not launch training or overwrite existing result directories.

The explicit seed-extension flag is needed only when the old checkpoint declared development seeds rather
than the established paper seeds 910001--910020. Overlap with training or validation remains an error.
The original checkpoint is unchanged and the declaration extension is recorded in preflight JSON; this is
not a claim that the new seed set was embedded in that checkpoint before training.

Previous results marked `requires_rerun` are never imported into the new comparison. In particular, the
September 19 results used the pre-fix PM lifecycle and cannot serve as baselines for `exclusive_pm_v1` models.
The new evaluation directory preserves that reuse assessment independently of the old results.

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

### Extend To 100 Seeds With Live Progress

Keep the completed, audited 20-seed runs and append the same 80 new seeds to every policy and fleet size:

```powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/extend_seeds.py `
  --prepared <result_directory> --first-seed 910001 --seed-count 100 `
  --allow-undeclared-held-out-seeds
```

This preserves the original checkpoints and run directories, checks all resolved policy/scenario settings,
verifies checkpoint hashes and seed separation, and backs up the 20-seed CSVs and dashboard.
For six policies and worker counts 2--6, the fixed plan contains 3,000 runs: 600 reused and 2,400 new.
This is a documented follow-up extension of an already inspected study, not a new pre-registration.
Do not stop early when a comparison becomes significant.

Run the extension in a hidden background process (replace the absolute result path):

```powershell
$prepared = 'C:\Github\ManSim\experiments\mfg_flow_shop_paper\results\<result_directory>'
$env:OMP_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
Start-Process -FilePath "$PWD\.venv\Scripts\python.exe" `
  -ArgumentList @('-u', 'experiments/mfg_flow_shop_paper/run_evaluation.py',
    '--prepared', $prepared, '--jobs', '10', '--pending-only',
    '--allow-undeclared-held-out-seeds') `
  -WorkingDirectory $PWD -WindowStyle Hidden `
  -RedirectStandardOutput "$prepared\evaluation_100seed_console.log" `
  -RedirectStandardError "$prepared\evaluation_100seed_stderr.log"
```

`live_evaluation.html` opens in Chrome (default-browser fallback) and refreshes every five seconds,
without a web server. It shows audited completion, reused/new counts, pending/running/failed runs,
per-policy/per-worker progress, active run day and simulation progress, elapsed time and approximate ETA.
An expired heartbeat is visibly flagged rather than presented as a healthy running process.
The previous 20-seed dashboard is explicitly labelled and preserved separately.
Simulation progress of 100% is not counted as complete until artifact and KPI audits pass.
Only the fully audited 100-seed block produces the final statistical dashboard; partial results are not ranked.
After aggregation and fairness checks pass, the existing standard comparison dashboard opens automatically.

The runner defaults to ten parallel jobs. `--no-open-dashboard` suppresses browser opening;
`--no-live` disables the monitor. After interruption, repeat the same `--pending-only` command to retain
verified results and resume missing/failed runs. An OS file lock prevents duplicate evaluators from writing
the same experiment concurrently. CSV/JSON monitor updates are atomic.

For this extension, the preceding 600-run evaluation took about 2.16 hours with ten concurrent jobs;
2,400 additional runs are therefore initially estimated at about 8.6 hours. This estimate is machine- and
workload-dependent and is separate from the older pilot estimates below. Replay and detailed events remain off.

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

The ideal process/movement capacity and operational planning references are approximations,
not certified scheduling bounds or empirical expectations. Average machine routes, a steady-state
cycle and threshold-based charging are planning assumptions. Do not use proximity to these
references as proof of saturation or optimality. The separately labelled material-only bound
relaxes all processing and transport constraints; legacy `theoretical_*` JSON keys are retained.

### Inventory Stress Audit

Warehouse deadlocks in the 20/40 inventory study are intentional extreme-scenario behavior.
Do not remove them or change warehouse geometry, route selection or yielding as an audit fix.
The earlier 30-material study has a different warehouse layout, so comparing 20/30/40 does
not isolate inventory alone. The 20 and 40 conditions share the expanded 40-slot layout.

```powershell
.\.venv\Scripts\python.exe -m experiments.mfg_flow_shop_paper.audit_inventory_results `
  --prepared <materials_20> <materials_40> <previous_materials_30> --output <audit_dir> --jobs 10
.\.venv\Scripts\python.exe -m experiments.mfg_flow_shop_paper.audit_inventory_results `
  --prepared <materials_20> <materials_40> <previous_materials_30> --output <audit_dir> --statistics-only
```

These checks independently reconcile daily outputs, worker state integrals, snapshot capacity,
Gantt coverage, KPI ratios, per-seed statistics and paired fleet marginal gains. Minute snapshots
cannot establish per-edge movement continuity or item custody. Detailed-event qualification
is still needed for those claims. Gantt `CHARGING` is a power overlay that can also cover blocked
availability; do not simply add every charging segment to execution time. New Gantt CSVs retain
the underlying `availability` and a `charging` flag. Legacy CSVs allow only overlap-aware checks.

Restock fairness compares the configured refill schedule and target, not identical positive-quantity event
times: a warehouse already at its target legitimately skips a refill. A missing event is accepted only when
the exact boundary inventory snapshot proves the shelf is at target and within capacity. Missing evidence,
underfilled shelves, duplicate events and off-schedule additions still fail. `evaluation_raw.csv` retains the
actual event times, verified no-op boundaries and audit errors; `fairness_report.csv` reports verified no-ops.

- All runs must end with `objective_status=complete` at exactly 2,400 simulated minutes.
- Scenario, timing, reliability, buffer and policy-independent fingerprints must match within each
  worker-count/seed block.
- Buffer overflow, reservation leak, duplicated item/task, role violation and unexplained relocation counts
  must be zero in detailed-event qualification runs. Every final compact run must pass its available KPI and
  layout invariants; event-only continuity checks are not falsely reported as executed when detailed events are off.
- ADP checkpoints must declare exactly one supported worker count and all test seeds as held out.
- Failed runs are rerun with the same seed; they are never silently dropped or replaced.
