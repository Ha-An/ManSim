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

## Implemented Changes

1. Global features: 35 -> 30. Remove elapsed/horizon (remaining/horizon is
   retained), plus input/output free-capacity ratios for both stations.
   For each finite buffer, free = max(0, 1 - occupancy - reservations).
   No worker/task/pair features, safety constraints or transitions are removed.
2. Correct warehouse count (occupied slots, not shelf capacity), live machine
   remaining time (nonmutating read, including interrupted/resumed cycles), and
   worker task-start time zero (zero is a valid timestamp).
3. Load the HumanoidSim state schema once per runtime. Every transition and
   snapshot is still strictly validated; schema changes require a new runtime.
4. Collate complete CPU arrays before device transfer, instead of copying every
   state/field separately. Preserve float32, masks, sentinel and padding rules.
5. TD target construction skips online inference only for a provably unique
   feasible joint action. Bootstrap evaluation still uses the target network.
   States with a real choice still use the same beam, device and tie-breaking.
6. Sample complete replay episodes before merging tensors. Preserve RNG state,
   sample ordering and full-history padding widths. No replay samples expire early.
7. Reuse the already computed task duration in battery-risk metadata; avoid
   reconstructing the idle-worker ID set for every worker.

Feature schema is now `mfg_flow_shop_adp_v10`. No automatic old-checkpoint weight
conversion is attempted because corrected input semantics are not equivalent to
v9. Baseline checkpoints remain untouched and can be used with their original
code/environment, not the new schema.

No smaller network, attention-kernel switch, GPU-to-CPU target search switch,
episode reduction or seed change is included. Numerically small score differences
can change near-tied actions, so those options need separate trajectory ablations.
Candidate-age lifecycle changes and pathfinder redesign are also deferred.

## Input Layout

Global indices 0..11: remaining horizon, completed products, scrap, actual warehouse
stock, aggregate material/intermediate/output queues, broken/processing fractions,
decision-worker fraction, opportunity count, fleet size.
Station 1 indices 12..20 and Station 2 indices 21..29: input occupancy/reservation,
output occupancy/reservation, idle/processing/blocked/broken fractions, mean live
remaining processing time. Worker/task/pair widths remain 16/21/8.

Cumulative products, scrap, role priority and fleet size remain available. They may
correlate with other inputs but are not algebraically redundant in the supported
profiles. Removing them without a performance ablation is outside this change.

## Verification Commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_adp_efficiency.py tests/test_adp_td.py tests/test_simulation_based_adp.py -q
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train --config configs/adp/mfg_flow_shop_n_step_td_smoke.yaml --no-open-dashboard
.\.venv\Scripts\python.exe scripts/benchmark_adp_efficiency.py --workers 3 --output outputs/efficiency_worker3.json
```

The benchmark compares cached/uncached schema rollout and old/new compute paths
on identical encoded states and network parameters, including CUDA target equality.
Its random policy and fresh weights measure implementation overhead, not final ADP
production. It does not start a full training run or overwrite existing reports.

## 2026-09-26 실측

동일 seed 72026, 1일, Random Feasible, CPU thread 1개 조건입니다.
값의 의미를 수정한 v10 입력은 양쪽에서 동일하게 사용하고 계산 경로만 비교했습니다.
Rollout은 조건당 1회, target/merge는 3회 중앙값이므로 전체 학습시간 예측이 아닙니다.

| 항목 | Worker 3: 이전 / 개선 | Worker 6: 이전 / 개선 |
| --- | --- | --- |
| Schema 반복 로딩 / 재사용 rollout | 46.45 / 15.68초 | 76.44 / 24.72초 |
| CPU target 구성 | 0.257 / 0.175초 | 0.876 / 0.146초 |
| CUDA target 구성 | 0.651 / 0.384초 | 2.381 / 0.295초 |
| 생산량 / decision 수 | 양쪽 4개 / 184회 | 양쪽 12개 / 796회 |

두 조건 모두 행동, 보상, rollout tensor와 TD endpoints/targets가 정확히 일치했습니다.
유일한 행동이 많은 worker 6 샘플에서 target 최적화 효과가 더 큽니다. 이는 state/episode를
삭제한 효과가 아니라 불필요한 online ranking 호출을 생략한 효과입니다.

원본 측정 파일은 로컬 `outputs/adp_efficiency_implementation_20260926/workers_3.json`과
`workers_6.json`에 있습니다. 재현 스크립트는 Git으로 관리합니다.
CUDA smoke 학습은 6 training + 4 validation episode를 완료했고 학습 artifact 감사가 통과했습니다.
최종 전체 테스트는 370개와 45개 subtest가 통과했습니다. 저장된 `best.pt`의 1일 추론도
완료했으며 KPI 및 core artifact/공간 연속성 감사는 오류·경고 0건입니다.
Replay export를 끈 실행이므로 artifact 감사에는 `--skip-replay-log`를 명시했습니다.

```powershell
.\.venv\Scripts\python.exe scripts/audit_adp_training.py outputs/adp_training_worker3_n_step_td_smoke/20260926_161112_996977
.\.venv\Scripts\python.exe scripts/audit_kpi.py outputs/adp_efficiency_implementation_20260926/inference
.\.venv\Scripts\python.exe scripts/audit_run_artifacts.py outputs/adp_efficiency_implementation_20260926/inference --skip-replay-log
```

입력 차원과 의미가 달라져 새로 학습한 정책의 최종 생산성이 같다는 보장은 아직 없습니다.
장시간 학습·held-out 정책 비교는 이 변경 검증에서 수행하지 않았습니다. 우선 동일 학습 예산을
유지하며, 이후 생산성의 paired 비교로 허용 가능한 성능 저하가 없는지 확인해야 합니다.
