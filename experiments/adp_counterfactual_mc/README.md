# Counterfactual MC Minimal A/B Experiment

This experiment compares one ordinary MSE update with one MSE update that also
samples paired original/alternative afterstates. It starts from an existing
checkpoint and does not perform a full ADP training run.

```powershell
.\.venv\Scripts\python.exe experiments/adp_counterfactual_mc/run_experiment.py --smoke
.\.venv\Scripts\python.exe experiments/adp_counterfactual_mc/run_experiment.py
```

## Sampling-Fraction Ablation

```powershell
.\.venv\Scripts\python.exe experiments/adp_counterfactual_mc/run_experiment.py --config experiments/adp_counterfactual_mc/config_sampling_fraction.yaml
```

This profile compares 0%, 1%, and 10% paired-sample sampling using the same
checkpoint, regenerated training episodes, episode holdout, and SGD budget.
Evaluation uses five new seeds (720101-720105). The standard training profile
is unchanged. There is no source-production or separate action-diagnostic
rollout in this experiment: 10 ordinary episodes, one identity replay,
10 alternative episodes, and 15 greedy evaluations total 36 simulations.
The report includes actual sampling fractions after minibatch rounding,
common holdout MSE, products, and paired exploratory bootstrap intervals.
No checkpoint is promoted based on these exploratory evaluation seeds.

The default config uses worker 3, five 480-minute days, ten training episodes,
five new evaluation seeds, ten CPU rollout processes, and CUDA updates. Both
arms start from iteration 5 of the reliability-v8 run with identical weights
and fresh Adam state. Both use epsilon 0.5, the same ordinary rollout batch,
the same episode holdout split, three epochs, and the same number and size of
optimizer steps. The source checkpoint is preserved.

For each training episode, one multi-action decision is captured. The original
return is reused, and one different feasible joint assignment is executed from
the same prefix. The captured policy RNG state is restored after the forced
decision. All subsequent decisions use the same frozen source policy and
epsilon as the ordinary rollout. The encoded pre-state and alternative
afterstate must match. An identical-action replay must reproduce products,
scrap, decision count, and return before the experiment proceeds.

The treatment reserves 10% of each SGD minibatch for the original/alternative
MC sample pairs. Samples retain their original episode ID, so holdout episodes
and their branches never enter training. Both arms select their best epoch
using the same original-episode holdout MSE. No ranking loss, shaping, TD,
validation gate, or historical transition replay is introduced.

Outputs are under `results/<timestamp>/`:

- `config.json`, `summary.json`, `report.md`, `diagnosis_dashboard.html`
- `control.pt`, `treatment.pt`
- `episode_metrics.csv`, `wave_metrics.csv`, `update_metrics.csv`
- `training_action_pairs.csv`, `diagnostic_actions.csv`

Greedy production is compared on the same five fresh seeds for source, control,
and treatment. A paired bootstrap interval summarizes treatment minus control;
five seeds are exploratory evidence, not a general performance claim.

Separate action diagnostics capture states on source-greedy evaluation runs.
The original and one alternative action each receive one continuation under
source greedy. All models directly score these two candidates without beam
pruning. The reported gap/regret are single-realization observations under that
fixed continuation policy, not expected optimal values or calibrated training
target errors. They are not used to choose model weights or hyperparameters.

The run writes no detailed event/replay artifacts or compact datasets. The
shared `fit_mc_value` API supports optional `counterfactual_samples` and
`counterfactual_fraction`; the standard training profile remains opt-in and
does not automatically run these extra simulations.
