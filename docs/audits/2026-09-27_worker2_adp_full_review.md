# Worker 2 ADP End-to-End Review (2026-09-27)

## Scope

Reviewed the completed run at
`outputs/adp_training_worker2_early_stop/20260926_195458`:
550 training episodes, 150 validation episodes, 51 value updates, 70 rollout waves.
The run stopped at policy iteration 50; final selection retained iteration 30.

The review covered TD return boundaries and greedy mismatch truncation, target-network
updates, complete-episode replay and holdout separation, coordinator reward/assignment
recording, early stopping, checkpoint selection, episode/iteration/wave aggregation,
the static training dashboard and the background live monitor.

No learning hyperparameters, reward, action selection, feature schema or checkpoints
were changed by this review. Pre-existing worktree changes were preserved.

## Findings and Fixes

1. **Nonzero learning rates displayed as zero.** The generic table formatter used
   three decimal places for every float: `0.00005` appeared as `0.000`. Values below
   `0.001` now use significant digits. This was a display error, not a zero Adam LR.
2. **Forced idle was not recorded when WAIT was disabled.** No-candidate counters
   were derived from the WAIT-only list, which was empty in the default profile.
   No-candidate worker/joint counters now operate independently of WAIT. Worker
   ratios use assigned plus unassigned decisions, avoiding an incorrect denominator.
   Episode/KPI records identify the new contract as `idle_metrics_version=2`.
3. **Undefined random-batch entropy appeared as zero.** Initial random rollouts have
   no eligible greedy beam decisions. TD summaries now exclude unobserved episodes
   and report `None` when no observations exist. The renderer also reconstructs
   entropy availability from older episode CSVs without rewriting those files.
4. **The artifact audit could miss corrupted aggregates.** It mainly checked counts,
   contracts and finite iteration fields, but did not independently recompute all
   production moments, greedy MC errors or final-selection values. It also allowed
   non-finite episode values to bypass `abs(actual-expected) > tolerance` checks.
   It now checks all CSVs for non-finite values; recomputes production/MC aggregates;
   checks per-checkpoint seed sets, worker counts, actual wave hashes/durations and
   saved best/last model equality with their iteration checkpoints.

## Original-Data Recalculation

- Episode production, sample SD, pooled greedy MC RMSE/bias and prediction/target
  means match the saved iteration statistics.
- Final selection remains I30: mean **40.45**, sample SD **2.41650029675366**, 20 seeds.
- I50 final mean remains **40.25**, SD **2.844662592146544**, 20 seeds.
- Early stopping at I50 is reproducible from the fixed screening seeds and the
  configured minimum iteration, patience and paired-CI rule.
- Best and last model tensors match I30 and I50 respectively. They were not retrained
  or replaced.
- The strengthened audit passes with one historical-data warning: old WAIT-disabled
  logs cannot establish that forced idle was zero. Missing event history cannot be
  reconstructed from the aggregate alone.

Artifacts: `audit_report.json` in the original run directory and
`outputs/adp_review_20260927/` for additional verification.

## Same-Seed Five-Day Reproductions

Three CPU episodes were rerun using the archived policies and original configuration.
Full events were inspected in memory; detailed replay/event files were not retained.

| Policy | Seed | Products | Scrap | Decisions | Assigned tasks | Newly recorded no-candidate worker decisions |
|---|---:|---:|---:|---:|---:|---:|
| I30 checkpoint | 102646 | 39 | 6 | 769 | 737 | 34 |
| I50 checkpoint | 102646 | 38 | 6 | 738 | 726 | 13 |
| Initial Random Feasible | 2026 | 25 | 3 | 571 | 548 | 31 |

Production, scrap, raw reward, decision count, assignment count and 2400-minute
termination exactly reproduce the originals. Greedy MC sample counts and both
error sums also match exactly for the checkpoint episodes.

305,379 events were inspected. Spatial continuity, carried-item transport, inspection
lifecycle and machine-breakdown/task consistency audits found no errors. No selected
assignment failed finalization in these reproductions. The two greedy runs each end
with work still in progress at the fixed horizon; open-task/primitive/movement
warnings are expected censoring at 2400 minutes, not duplicate tasks or teleportation.

See `reproduction.json` and `reproduce.py` in the review output directory.

## Dashboard Verification

- Rebuilt the original dashboard from unchanged CSV/JSON.
- Independently checked all 10 SVG charts (8 panels), totaling 622 plotted points,
  against the original series and paired production calculations. Differences stay
  within the SVG renderer's 0.05-unit rounding precision.
- Chrome headless checks passed at 1440x1100 and 390x844 for both static dashboard
  and live monitor. No JavaScript exception, non-finite displayed value or page-wide
  overflow was found. Narrow charts/tables have their own horizontal scroll areas.
- Live completion remains 700/700; learning-rate cells retain nonzero values.
- Browser screenshots, DOM checks and coordinate checks are stored in
  `browser_checks.json`, `chart_value_checks.json` and the accompanying PNGs.

## Tests and Limits

- Full suite: 410 tests and 45 subtests passed.
- Real worker-2 CUDA smoke: 6 training + 4 validation episodes, 3 updates, 7 waves;
  the strengthened training audit passed without errors or warnings.
- Regression tests cover WAIT-independent idle counting, ratio denominators, small
  number formatting, unavailable entropy and deliberately corrupted statistics.

These fixes do not require repeating the full learning run. They do not establish
absence of every possible simulation bug: detailed events for all original 700
episodes were disabled, and only three five-day trajectories were re-executed.
The observed late positive prediction bias is real in the saved predictions and
MC returns; it was not caused by dashboard aggregation. Accurate aggregation does
not demonstrate value-function convergence or policy optimality.
