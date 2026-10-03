# Fleet-Specific n-step TD

## Default

`python -m manufacturing_sim.adp.train` reads `configs/adp/mfg_flow_shop_throughput.yaml`.
Feature schema v10 removes five algebraically redundant global inputs (35 to 30)
and corrects warehouse occupancy and live processing-time observations. Worker,
task and pair dimensions, attention architecture and cyclic beam rules are unchanged.
Previous schemas are rejected; train a new checkpoint for the corrected inputs.
See [efficiency changes and rollback](adp_efficiency.md).
Training and validation use worker 3, five days of 480 minutes, raw completed products, no explicit WAIT.
Worker 3 remains the default profile. The trainer also accepts one fleet-specific worker count from 2 through 6;
one checkpoint supports exactly the worker count on which it was trained. The confirmatory paper suite therefore
trains separate checkpoints instead of claiming cross-fleet generalization.

- Initial: 50 Random Feasible episodes collected in five waves, trained with TD, not MC pretraining.
- Policy: at most 75 updates, 10 new episodes per update.
- Collection: 10 CPU processes, 10 episodes per wave. The policy is frozen within each wave.
- Maximum: 800 collected training episodes; 76 value updates including initialization.
- Replay: retain the most recent 100 complete episodes. Each policy update includes all 10 current-wave
  episodes plus 20 complete episodes sampled uniformly from older retained history.
- Stable holdout: episode IDs divisible by 10 never enter SGD, even when replayed later.
- Update: CUDA, MSE, Adam, batch 512, one initial epoch, two policy epochs, gradient clip 0.5.
- Epsilon schedule: piecewise linear through `(0, .50)`, `(25, .15)`, `(50, .08)`, `(75, .05)`.
- Learning-rate schedule: piecewise linear through `(0, 5e-5)`, `(20, 5e-5)`, `(30, 2e-5)`,
  `(60, 2e-5)`, `(75, 1e-5)`.
- Target network: initialized as an identical copy, no gradients, soft update tau=0.03 per SGD step.
- Conservative validation gate and pairwise MC loss are disabled. There is no performance rollback.
- Screening: iterations 0, 5, 10, ..., 75 until stopping, ten fixed seeds each. The top two checkpoints are each
  evaluated on 20 separate final-selection seeds; final mean, lower standard deviation and earlier
  iteration break ties in that order.
- Final policy tests remain separate: ADP, Random Feasible and Immediate Shared.

## Production Early Stopping

The default `training.early_stopping.enabled: true` stops additional rollout collection,
not acceptance of a value-network update. There is no rollback or loss-based stopping.

- Minimum policy iteration: 45; maximum remains 75.
- At least 20 iterations without a strictly higher best screening mean. Any positive
  increase, including less than 0.5 products, resets this count.
- At each screening, pair the current checkpoint with the best preceding screening
  checkpoint using exactly the same seeds. Compute `mean(current-best) + 1.96 * sd(diff) / sqrt(N)`.
- Both of the last two screening upper bounds must be strictly below 0.5 products.
- Final selection still evaluates the two screening finalists on 20 separate seeds each.
  It never uses held-out policy-test seeds. `best.pt` need not be the last checkpoint.

This is a practical compute-budget rule, not proof of convergence or an absence of later gains;
repeated screening and selection affect its statistical interpretation. Epsilon/LR schedules
are not rescaled when stopping early. Use `enabled: false` in YAML for a fixed-budget ablation.
Completed runs report `completed_policy_iterations`, `early_stopped`, `stop_reason` and
actual counts. Original maximum budgets remain visible, and the live progress denominator
is adjusted when entering final selection. Best/last manifests distinguish consumed training
seeds from the planned seed budget in intermediate checkpoints.

Worker 2 with the default schedule, nonblocking execution and live monitor:

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train --worker-counts 2 --background `
  --output outputs/adp_worker2_new_run
```

## Target Contract

`n=30` counts decisions, not minutes or worker tasks. Time between decisions is irregular.
For each sampled afterstate, sum subsequent raw product rewards for at most n transitions.
At the endpoint, select an action with the online value network and evaluate its afterstate with the target network.
Gamma is 1. A true five-day terminal transition has bootstrap 0. Never join separate episodes.

The greedy action map is recomputed once per policy update, before SGD, using that update's starting online
parameters and the original decision's cyclic worker offset. This map stays fixed for that update's SGD epochs.
If an intermediate recorded action differs from this map, stop BEFORE taking that action and bootstrap there.
This is a truncated off-policy return, not an uncorrected n-step return under a historical epsilon-greedy policy.
Early random data and stale replay can therefore have effective n close to 1 even when configured n is 30.
The target network's endpoint values are recomputed for each mini-batch; old fixed MC targets are never optimized.

When a decision admits only one joint assignment under the same worker order and
constraints, target construction skips the unnecessary online ranking call.
It still retains the transition, off-policy comparison and target-network bootstrap.
Replay episode IDs are sampled before merging tensors. RNG draws, ordered full
episodes and full-history padding widths remain identical to merge-then-sample.
`replay_mib` reports the actual retained chunks, not a redundant fully padded copy;
`update_mib` reports the merged selected batch. Neither is total process RSS.

CPU replay retains pre/afterstate tensors, rewards, terminal flags, decision indices and small primitive
metadata for feasible task/resource contracts. It retains no simulator, worker or domain Task objects.
The constraint adapters reconstructed during target search use the same beam implementation as runtime.
Episode MC returns are retained only for OOD diagnostics and are not inputs to the TD regression loss.

## Dashboard Interpretation

All plot descriptions are in Korean, and each plot has explicit axes.

1. Production: grey k is the exploratory collection BEFORE update k; grey 0 is random initialization.
   Purple k is fixed-seed greedy evaluation AFTER update k. Purple error bars are approximate 95% mean CIs
   (mean +/- 1.96 * sample SD / sqrt(seed count)), not ranges containing 95% of episodes.
2. TD train/holdout MSE: consistency with bootstrapped targets, not independent future-product accuracy.
   Final-epoch errors use the current target network and the fixed greedy action map for that update.
3. Paired change from I0: match completed screening episodes by worker/seed and show the mean within-seed
   production difference with its approximate 95% CI. Different seed sets and partial evaluations are excluded.
4. Greedy validation MC RMSE and bias: actual remaining products versus predictions on unseen greedy episodes.
   All decision samples are pooled; episodes with more decisions carry more weight. Positive bias means overprediction.
   These validation trajectories are not replayed or trained on and require no additional simulation episodes.
5. Effective n plus simulation-minute span: distinguish decision count from actual time observed by each target.
   Cutoff ratios are in the table. Mean predicted/actual MC values are in the RMSE panel, not a duplicate chart.
6. Existing epsilon, beam entropy, WAIT, update timing and memory diagnostics move to detailed tables.
   Replay, OOD and phase-specific collection time remain in collapsible diagnostic panels, not proof of optimality.
   Beam entropy standardizes candidate values and does not measure absolute action-value differences.

Missing validation or holdout values show N/A, never fabricated zeros. OOD compares against the previous
collection batch and uses exploratory future MC returns, so it is not a counterfactual greedy action-ranking test.
Small nonzero numbers (including learning rates) use significant digits instead of rounding to `0.000`.
Beam entropy is averaged over episodes with at least one eligible greedy decision; a random-only batch
has undefined entropy (`N/A`), not zero entropy. The renderer can derive this from older episode CSVs
without changing saved training data.

Episode logs with `idle_metrics_version: 2` count no-candidate idle workers even when WAIT is disabled.
`unassigned_count` includes both voluntary and forced non-assignment; worker ratios use
`assigned_task_count + unassigned_count` as their denominator. `joint_no_candidate_count` counts
decisions where every decision worker lacks a candidate, independently of the WAIT action setting.
Older WAIT-disabled logs did not record these idle counts. Their zeros cannot be treated as observed
absence of idle time, or reconstructed without detailed events/re-execution. The dashboard and audit
explicitly warn about that limitation. These are event counts, not elapsed idle-time ratios.

`scripts/audit_adp_training.py <run>` recomputes episode-group production means/standard deviations,
decision-pooled greedy MC errors, wave durations/hashes, and final selection from episode records.
It rejects non-finite CSV data, missing/duplicate validation seeds and worker-count mismatches.
This statistical audit does not replace a spatial/event audit when detailed event logs were disabled.
The checkpoint-selection table ranks screening production, then lower standard deviation, then earlier iteration.
Best selection is not a gate: training always continues from the newly updated policy.

The default view now shows four primary charts; four collapsible diagnostic groups contain the rest.
Every panel documents interpretation, formula, weighting and caveats in Korean.
See the [full graph guide](adp_training_dashboard.md). The live embed preserves open sections and scroll
through refreshes, hides duplicate run cards, and keeps historical invalidity warnings visible.

## Verification

For nonblocking training and a live monitor, add `--background`:

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train --background
```

The launcher returns immediately. The independent process writes per-episode progress,
refreshes the existing TD dashboard after each wave/update, and updates a local HTML monitor every five seconds.
See [background training](adp_background_training.md) for status/stop commands and reboot limitations.
Monitoring changes neither the learning algorithm nor the simulation event schedule.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_adp_td.py tests/test_simulation_based_adp.py -q
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train --config configs/adp/mfg_flow_shop_n_step_td_smoke.yaml --no-open-dashboard
```

Rebuild the dashboard from unchanged CSV/JSON without training:

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.td_dashboard outputs/adp_training_worker3_n_step_td/<timestamp>
```

The smoke profile uses two CPU processes, a small network and one-day episodes. It checks execution,
not production superiority. To compare one-step and n-step, use a separate copy of the default profile with
`algorithm.n_step: 1`, matching all other settings and seeds. Match SGD budgets as well as collected episode counts.
Full episode collection is unchanged; target reconstruction adds work, so TD does not imply faster training.

Default output: `outputs/adp_training_worker3_n_step_td/<timestamp>/`.
Files include `resolved_config.yaml`, episode/wave/iteration CSV, `checkpoint_selection.csv`,
`training_summary.json`, `checkpoint_manifest.json`, `checkpoints/iteration_*.pt`, `best.pt`, `last.pt`
and `training_dashboard.html`. Best/last include target-network and optimizer states; replay is not persisted,
and automatic exact mid-run resume is not supported by this driver.
The legacy MC profile remains `mfg_flow_shop_throughput_worker3_wave_updates.yaml`.
