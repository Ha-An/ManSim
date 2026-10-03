# ADP Efficiency Baseline

## Baseline: 2026-09-26

Before performance changes, the current code, configuration and documentation are
committed to main and tagged `baseline-adp-efficiency-20260926` on origin.
Experiment results, checkpoints and virtual environments stay local and are not
included in the Git snapshot.

The baseline uses 800 training episodes, truncated n-step TD (maximum n=30),
target tau=0.03, a 100-episode replay, 10 CPU rollout processes and CUDA updates.
The default comparison remains ADP, Immediate Shared and Random Feasible, with
unchanged held-out seeds. Background training and live monitoring are supported.

This is a code recovery point, not a new validated scientific result. Results
predating the PM/processing exclusivity fix are not valid for the corrected
environment. Known input-semantics and efficiency issues are documented in the
[efficiency review](audits/2026-09-26_adp_efficiency_review.md).

## Inspect the Baseline Without Overwriting Work

```powershell
git fetch origin --tags
git worktree add ../ManSim-before-efficiency baseline-adp-efficiency-20260926
```

Virtual environments and experiment artifacts are managed separately. Do not
change the source or configuration used by an active training process.

## Optimization Contract

- Preserve feasibility, constraints, RNG, rewards, TD targets, seeds and episode counts.
- Remove algebraically redundant inputs, not unproven low-importance features.
- Test numerical and action equivalence for computation-only optimizations.
- Document schema/input-semantics changes and checkpoint incompatibilities.
- Distinguish focused regression tests from full-training productivity evidence.
