# Flow-Shop Fleet And Inventory Experiments

This suite trains fleet-specific ADP checkpoints and compares policies using common environment seeds.
Existing module paths are retained so prepared plans and background jobs remain compatible.

## Workflows

| Workflow | Training | Evaluation |
| --- | --- | --- |
| Inventory ADP pipeline | One model per fleet 2--6, current TD profile | 100 seeds, 3 policies by default or all 6 |
| Inventory baseline sweep | None | Materials 20 and 40, Immediate Shared and Random Feasible, 100 seeds |
| Compact fleet experiment | One model per fleet 2--6, reduced validation | 20 seeds, 3 policies |
| Saved-checkpoint evaluation | Reuse compatible completed training | 20 seeds initially; optional extension to 100 |

The default policy set is ADP, Immediate Shared and Random Feasible.
Additional modes are Immediate Dedicated Roles, Rolling Horizon Shared and Rolling Horizon Dedicated Roles.
Comparisons use five 480-minute days, no explicit WAIT, common physical constraints and fixed seeds.
Forced idle without a feasible task is still possible.

## Inventory ADP Pipeline

Materials 20, 30 and 40 share a Warehouse with 26 x 15 tiles, four shelf rows and 40 slots.
Door tiles are (49,18) and (50,18). Only initial fill and daily top-up target differ.
Top-up restores inventory to the target; it does not add the target unconditionally.
The default scenario uses this geometry with fill/target 30.
Old 30-slot results are not layout-controlled comparisons and must not be reused in this environment.
Warehouse congestion and deadlocks are intentional stress behavior; routing is not changed to remove them.

Run from the repository root, using a new output directory:

~~~powershell
.\.venv\Scripts\python.exe -m experiments.mfg_flow_shop_paper.train_inventory --fresh --all-policies --fill 30 --output experiments/mfg_flow_shop_paper/results/new_materials30 --background
~~~

Replace fill with 20 or 40 for another condition; omit <code>--all-policies</code> for three policies.
The pipeline sequentially trains workers 2--6, audits and preserves their checkpoints, validates
fleet/inventory compatibility, then evaluates on seeds 910001--910100 with ten parallel jobs.
Three policies produce 1,500 evaluation runs; six policies produce 3,000.

Training uses initial 50 episodes, at most 75 updates of 10 new episodes, replay samples of 30,
and production early stopping. Screening occurs every five updates with ten seeds; the top two
candidates each receive 20 separate final-selection episodes. CPU waves use ten processes; updates use CUDA.
The compact fleet workflow below uses a different validation schedule.

The existing live training monitor opens immediately and shows completed training hours per fleet.
Evaluation uses live_evaluation.html; final results use policy_comparison_dashboard/comparison_dashboard.html.
For 3,000 fresh evaluations the pipeline waits until at least 45 GiB is free, preserving checkpoints
and checking every minute. This startup guard is not a guarantee against later disk exhaustion.
It never deletes previous experiments automatically.

Without <code>--background</code>, preparation writes the plan but does not launch the pipeline.
Start a prepared pipeline explicitly with:

~~~powershell
.\.venv\Scripts\python.exe -m experiments.mfg_flow_shop_paper.train_inventory --output <prepared_directory> --run-prepared
~~~

### Qualified Baseline Reuse

~~~powershell
.\.venv\Scripts\python.exe -m experiments.mfg_flow_shop_paper.train_inventory --source <completed_baseline_directory> --fill 20 --output experiments/mfg_flow_shop_paper/results/new_materials20 --background
~~~

Reuse requires matching archived environments, timing, successful audits and four representative
deterministic reproduction cases. A content digest detects source changes during training.
Baseline files are referenced without copying or overwriting them. New wall time excludes historical costs.
Missing source artifacts cannot be recreated from aggregate CSVs alone; use <code>--fresh</code> instead.

## Baseline Inventory Sweep

The baseline sweep compares Immediate Shared and Random Feasible for materials 20 and 40.
Workers 2--6 and seeds 910001--910100 produce 1,000 runs per condition.

~~~powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/inventory_study.py --output <new_directory> --smoke --run --no-open-dashboard
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/inventory_study.py --output <new_directory> --run
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/inventory_study.py --output <existing_directory> --resume --run
~~~

Smoke runs eight five-day qualification cases with detailed events.
Full sweeps run inventory conditions sequentially using ten total jobs and omit detailed events and Replay.
Each condition has its own live monitor and standard comparison dashboard.

## Compact Fleet Experiment

The local config.yaml retains a smaller 20-seed workflow, separate from the inventory pipeline.
It uses one ADP model per fleet, screening at 0/15/30/45/60/75 (ten seeds each),
and two final candidates with ten fresh seeds each. Training is capped at 800 episodes with early stopping.
Final seeds 910001--910020 produce 300 runs across three policies and five fleets.

~~~powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/prepare_experiment.py
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_training.py --background
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_evaluation.py --jobs 10
~~~

Preparation writes training/evaluation CSV plans, PowerShell commands, generated YAMLs and experiment_plan.json.
The training-only background runner does not automatically start evaluation.

## Saved Checkpoints And Extensions

Evaluate compatible completed training without retraining:

~~~powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/prepare_saved_checkpoints.py --training-roots <worker2_training> <worker3_training> <worker4_training> <worker5_training> <worker6_training> --output <new_result_directory> --allow-undeclared-held-out-seeds
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_evaluation.py --prepared <new_result_directory> --jobs 10 --allow-undeclared-held-out-seeds
~~~

Use the seed-extension flag only when test seeds were not declared in the source checkpoints.
Overlap with training/validation remains an error. The declaration change is recorded without modifying checkpoints.
Preparation defaults to three policies; <code>--six-policies</code> includes all six.

Append three policies or extend the common seed set to 100:

~~~powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/extend_evaluation.py --prepared <result_directory>
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/extend_seeds.py --prepared <result_directory> --first-seed 910001 --seed-count 100 --allow-undeclared-held-out-seeds
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_evaluation.py --prepared <result_directory> --jobs 10 --pending-only --allow-undeclared-held-out-seeds
~~~

Extensions validate environments, back up original tables and retain qualified completed runs.
New runs belong to the destination study even when baselines reference another directory.
An OS lock prevents concurrent evaluators from writing the same study.
After interruption, <code>--pending-only</code> resumes missing/failed evaluations, not ADP mid-training.
Use <code>--no-open-dashboard</code> for headless operation.

## Metrics And Audit Scope

- Per-fleet means, sample standard deviations and seed-level confidence intervals.
- Paired ADP-minus-baseline differences, bootstrap intervals and win/tie/loss.
- Equal-fleet overall contrasts resample common seed blocks across fleets.
- Worker execution ratio is an availability-time integral, not a task count.
- Throughput, lead time, blocking, depletion/recovery, repair and PM indicators.
- Capacity references use each run's archived configuration.

One fixed checkpoint per fleet does not estimate training-seed variability.
Screening selects checkpoints; held-out tests must not select them again.
Process/movement and operational capacity references are approximations, not certified bounds or proof of saturation.
The separately labelled material-only bound relaxes processing and transport constraints.

~~~powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/audit_study.py --prepared <result_directory>
.\.venv\Scripts\python.exe -m experiments.mfg_flow_shop_paper.audit_inventory_results --prepared <materials_20> <materials_40> <materials_30> --output <audit_directory> --statistics-only
~~~

KPI/artifact audits and policy-independent fairness must pass before final aggregation.
Refill fairness allows verified no-op boundaries when inventory is already at target.
Failed runs are not silently omitted or replaced with different seeds.
Detailed events are needed for per-edge continuity and item custody; snapshots or aggregate KPI alone cannot prove them.

Results predating the exclusive_pm_v1 lifecycle require reruns in the corrected environment.
Passing statistics checks does not make an incompatible historical simulation valid.
Local archives may have been cleaned up: summary tables do not restore deleted checkpoints, events, snapshots or HTML.
Where available, artifact_cleanup.json / artifact_retention.json describe cleanup scope.

## Runtime And Storage

Measure comparable completed fleets on the current machine; costs vary with early stopping, candidates and traffic.
Evaluation elapsed time is wall-clock time, not a sum of parallel child runtimes.
NTFS compression is enabled on Windows, but free storage must still be checked before large runs.
Do not change simulation behavior or configuration during an active experiment.
