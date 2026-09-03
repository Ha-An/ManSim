# Decision Logic

이 문서는 ManSim의 decision mode와 task dispatch 기준을 정리합니다.

## 목표

ManSim의 최종 목표는 설정된 horizon 동안 `completed product`를 최대화하는 것입니다. completed product는 inspection을 통과한 뒤 최종 CompletedProducts zone에 dropoff된 제품입니다.

보조 지표는 원인 분석과 정책 비교에 사용합니다.

- inspection backlog
- machine broken / repair / PM time
- humanoid availability state
- traffic / incident count
- scrap rate
- queue wait time
- rolling horizon pool / dispatch / skipped task

## Decision Modes

현재 root config의 기본 mode는 `rolling_horizon_dedicated_roles`입니다. 비교 실험에서는 `decision=adaptive_priority`, `decision=rolling_horizon_aging_priority`처럼 Hydra override로 변경합니다.

### `mfg_flow_shop` 대표 정책 4개

`mfg_flow_shop`의 공식 정책 비교는 dispatch 시점과 역할 독점 여부만 조합한 다음 네 mode를 사용합니다.

| Mode | Dispatch | Role policy |
| --- | --- | --- |
| `immediate_shared` | 실행 가능해진 즉시 | 모든 worker가 모든 일반 업무 수행 |
| `immediate_dedicated_roles` | 실행 가능해진 즉시 | 세부 업무별 단일 owner |
| `rolling_horizon_shared` | 기본 5분 window | 모든 worker가 모든 일반 업무 수행 |
| `rolling_horizon_dedicated_roles` | 기본 5분 window | 세부 업무별 단일 owner |

네 mode는 adaptive weight와 aging boost를 사용하지 않고 동일한 `mfg_flow_shop_policy.priority_order`를 사용합니다. Immediate mode는 현재 실행 중인 task를 중단하지 않으며 다음 task 요청 시 고정 순서에서 가장 앞선 feasible rule을 선택합니다. Rolling mode는 worker 요청과 무관한 strict-periodic coordinator가 정확히 `t=5,10,15,...`에 신규 pool과 아직 시작하지 않은 queue task를 다시 배정하고 실행 중인 task는 유지합니다.

설정은 각 mode YAML의 `mfg_flow_shop_policy`에 있습니다. 역할 번호는 분류 식별자이며 `priority_order`의 실행 순서와 독립적입니다. 일반 task code만으로는 경로가 다른 `TRANSFER`와 slot이 다른 `LOAD_MACHINE`을 구별할 수 없으므로 `task_rules[].match`가 payload field를 함께 검사합니다.

```yaml
task_rules:
  - role_number: 3
    id: transfer_s1_to_s2
    display_name: TRANSFER (Station 1 to Station 2)
    task_code: TRANSFER
    kind: exclusive
    owner: auto
    match: {transfer_kind: inter_station, from_station: 1}
  - role_number: 9
    id: load_s2_intermediate
    display_name: LOAD_MACHINE (Station 2, intermediate)
    task_code: LOAD_MACHINE
    kind: exclusive
    owner: A2
    match: {station: 2, load_slot: intermediate}
```

표준 역할은 1) S1 보충, 2) S2 보충, 3) S1-to-S2 운반, 4) S2-to-Inspection 운반, 5) Inspection-to-CompletedProducts 운반, 6) `COLLECT_WASTE_OR_SCRAP`, 7~9) station/slot별 load, 10~11) station별 setup, 12~13) station별 unload, 14) inspection desk load, 15) inspection, 16) inspection desk unload, 17) power 관리, 18) 공동수리입니다. 역할 1~16은 dedicated mode에서 단일 owner를 가지며 역할 17과 18은 모든 worker가 공통으로 가집니다. 모든 runtime 후보는 정확히 한 rule과 일치해야 하며 누락·중복 selector는 오류로 중단됩니다.

Dedicated mode는 실행 전에 역할 1~16의 예상 부하 `expected occurrence count x expected busy minutes`를 계산합니다. Busy minutes는 scenario timing profile의 삼각분포 기댓값과 map의 예상 tile 이동시간을 합산합니다. 고정 `owner`를 먼저 배치한 뒤 나머지 `owner: auto` rule을 deterministic LPT로 배정하며 역할표는 run 중 바뀌지 않습니다. 역할 17 `MANAGE_ROBOT_POWER`와 역할 18 `REPAIR_MACHINE`은 LPT 대상이 아니며 모든 worker에게 공통 부여됩니다. 16명보다 worker가 많으면 남는 worker는 두 공통 역할만 수행합니다.

생성된 owner, rule별 예상 횟수·시간, worker별 expected busy minutes, 평균·최대 부하와 변동계수는 `run_meta.json`과 Pre-Run Diagnostics의 **Task Rule and Role Assignment**에 기록됩니다.

### `adaptive_priority`

로컬 scripted baseline입니다. 현재 공장 상태를 보고 task family priority를 조정해 즉시 dispatch합니다.

### `fixed_priority`

고정 priority baseline입니다. priority 자체를 거의 조정하지 않고 deterministic rule로 dispatch합니다.

### `rolling_horizon_aging_priority`

Rolling window 동안 task opportunity를 pool에 모은 뒤 window boundary에서 dispatch하는 deterministic mode입니다.

- 설정 파일: `configs/decision/rolling_horizon_aging_priority.yaml`
- 기본 window: `rolling_horizon.window_min: 5.0`
- priority 기준: HumanoidSim `task_code`
- priority 설정: `rolling_horizon.scenario_task_code_priority_order.<scenario>`
- dispatch policy: `aging_priority`
- 저전력 service만 `next_after_current` 긴급 queue로 즉시 들어가며 현재 task는 선점하지 않습니다.

Priority order는 scenario별로 분리합니다.

- `factory_mfg_basic`: 기존 Station1, Station2, Inspection 제조 task 순서
- `mfg_flow_shop`: direct charging, collaborative repair, parallel-machine finite-buffer flow-shop task 순서
- `shipyard_basic`: cart batch logistics, ship exterior surface tile의 용접, 표면처리, 도장, 검사 task 순서

기존 `rolling_horizon.task_code_priority_order`는 이전 설정 파일을 위한 fallback입니다.

기본 동작:

1. 시각 0에는 `[0,5]` window를 열고 후보만 수집하며 최초 일반 dispatch는 정확히 시각 5에 수행합니다.
2. task 후보가 발생하면 즉시 worker에게 할당하지 않고 event 기반 rolling pool에 저장합니다.
3. 같은 item, material slot, machine resource를 사용하는 중복 opportunity는 pool에 동시에 들어가지 못합니다.
4. 정확한 경계에서 미시작 queue를 회수하고 전체 후보를 다시 스캔해 누락과 stale 상태를 보정합니다.
5. 미해결 pool task를 정렬해 feasible task를 가능한 한 모두 worker dispatch queue에 배정합니다.
6. stale task는 `ROLLING_HORIZON_TASK_SKIPPED`로 기록하고 제거합니다.

Coordinator는 일반 domain event보다 낮은 SimPy priority로 경계를 처리합니다. 따라서 경계 시각과 정확히 같은 시각에 완료되거나 고장 난 machine 상태가 먼저 반영됩니다. Machine 고장은 event 시점에 repair 후보를 pool에 표시하지만 즉시 dispatch하지 않고 다음 정규 경계에서 배정합니다. 고정 horizon의 마지막 시각에는 실행할 시간이 없으므로 새 queue를 만들지 않습니다.

한 worker는 여러 task를 queue로 받을 수 있습니다. Worker는 queue의 첫 task부터 FIFO로 수행합니다. 다음 window가 열리면 아직 `AGENT_TASK_START`가 발생하지 않은 queued task는 pool로 되돌아가 새로 수집된 task와 함께 다시 ranking됩니다. 이미 실행 중인 task는 중단하거나 다른 worker에게 재배정하지 않습니다.

Rolling task는 pool에 처음 들어올 때 stable task id를 받습니다.

- 예: `MAT-000001`, `TR-000002`, `SET-000003`, `RM-000004`
- task code별 prefix는 simulator runtime에서 고정합니다.
- 이 id는 requeue, re-dispatch, `AGENT_TASK_START/END`, Replay panel까지 유지됩니다.

### Aging Priority

`rolling_horizon_aging_priority`는 숫자 weight를 쓰지 않고 task code 순서와 기다린 window 수만 사용합니다. 예상 processing time, bottleneck bonus, deadline bonus는 사용하지 않습니다.

```text
effective_rank = base_rank - waited_window_count * rank_boost_per_window
```

낮은 숫자가 더 높은 priority입니다. 예를 들어 `PREVENTIVE_MAINTENANCE`의 base rank가 낮더라도 pool에서 여러 window를 기다리면 effective rank가 올라가므로 영구 starvation을 피할 수 있습니다.

정렬 기준:

1. `effective_rank` 낮은 순
2. `first_seen_min` 오래된 순
3. `task_code` 순
4. `opportunity_id` 순

worker 선택은 feasible worker 중 `(현재 dispatch queue 길이, 예상 수행 시간, 이동 시간, worker id)` 순으로 고릅니다. task ordering 자체에는 예상 처리시간을 사용하지 않습니다.

### `rolling_horizon_dedicated_roles`

`rolling_horizon_aging_priority`와 같은 rolling window/pool/aging 구조를 쓰지만, worker별 HumanoidSim task code allowlist를 강제합니다.

- 설정 파일: `configs/decision/rolling_horizon_dedicated_roles.yaml`
- 기본 window: `rolling_horizon.window_min: 5.0`
- priority 기준: worker별 `rolling_horizon.scenario_worker_task_priority.<scenario>` 순서
- dispatch policy: `dedicated_role_aging_priority`

Factory dedicated roles 기본값:

- A1: `REPLENISH_MATERIAL`
- A2: `REPAIR_MACHINE`, `LOAD_MACHINE`, `SETUP_MACHINE`, `UNLOAD_MACHINE`
- A3: `MANAGE_ROBOT_POWER`, `TRANSFER`, `LOAD_UNLOAD_TRANSFER_INTERFACE`, `INSPECT_PRODUCT`, `COLLECT_WASTE_OR_SCRAP`, `PREVENTIVE_MAINTENANCE`

`mfg_flow_shop`은 위의 granular task-rule/LPT 역할표를 사용하며 task-code template 순환을 사용하지 않습니다.

Shipyard dedicated roles 기본값:

- A1: `OPERATE_VEHICLE_TRANSPORT`, `TRANSFER`, `WELD_SEAM`, `MANAGE_ROBOT_POWER`
- A2: `WELD_SEAM`, `PREPARE_SURFACE`
- A3: `OPERATE_VEHICLE_TRANSPORT`, `TRANSFER`, `PAINT_SURFACE`, `VERIFY_SHIP_SECTION`, `MANAGE_ROBOT_POWER`, `COLLECT_WASTE_OR_SCRAP`

기존 `rolling_horizon.worker_task_priority`는 이전 설정 파일을 위한 fallback입니다.

`factory_mfg_basic` dedicated-role mode에서 `HANDOVER_ITEM`은 product 공동 운반 합류 task이므로 pool에 수집하지 않고 `REPAIR_MACHINE`은 A2 단독으로 처리합니다. `mfg_flow_shop`은 모든 product 운반을 `TRANSFER`로 처리하고 handover를 생성하지 않지만, `REPAIR_MACHINE`만은 모든 worker가 참여할 수 있는 유일한 협업 task입니다.

A1과 A2는 battery station으로 직접 이동해 self swap을 수행하지 않습니다. A1/A2가 설정된 low threshold 이하로 내려가면 A3가 `transfer_kind=battery_delivery`인 battery delivery task를 pool에서 받아 수행합니다. threshold와 provider/receiver 목록은 `decision.battery` 설정에서 조정합니다.

`mfg_flow_shop`에서는 battery delivery와 swap을 사용하지 않습니다. 각 worker는 현재 task를 끝낸 뒤 `MANAGE_ROBOT_POWER(action=dock_charge)`를 받아 자기 전용 `charging_dock_<worker>`까지 이동하고, 그 tile을 점유한 동안만 SOC가 선형으로 증가합니다.

### `bottleneck_aware_dispatch`

`bottleneck_aware_dispatch`는 factory throughput 비교 실험용 즉시 dispatch mode입니다. Worker가 task를 요청할 때 현재 가능한 후보를 bottleneck relief, downstream progress, machine continuity, travel/execution time, resource risk, battery risk로 scoring하고 가장 높은 후보를 선택합니다. 선택된 task에는 `selection_meta.score_components`가 기록되어 왜 그 task가 선택되었는지 확인할 수 있습니다.

### `rolling_horizon_throughput_optimizer`

`rolling_horizon_throughput_optimizer`는 rolling horizon pool/window/requeue 구조를 유지하되 window dispatch를 OR-Tools CP-SAT로 결정합니다. Objective는 task별 throughput score와 urgent bonus를 최대화하고 worker queue load와 load imbalance를 penalty로 둡니다. 비교 실험 재현성을 위해 기본 `num_search_workers=1`이며 CP-SAT `random_seed`는 별도 값이 없으면 ManSim run seed를 사용합니다. 이 mode는 OR-Tools가 필수이며, solver가 `OPTIMAL` 또는 `FEASIBLE` status를 반환하지 않으면 fallback 없이 run을 실패시킵니다.

### `fixed_task_assignment`

worker별 허용 task family를 강제하는 scripted mode입니다.

### `openclaw_adaptive_priority`

OpenClaw manager mode입니다. Strategist, Reviewer, Curator가 high-level intent와 operational knowledge를 만들고, deterministic compiler가 이를 executable policy로 변환합니다. ManSim worker는 deterministic simulator runtime으로 실행됩니다.

## Removed Paths

`urgent_discuss` 기반 즉시 정책 변경 경로와 shared `norms` 기반 runtime 조정 경로는 현재 ManSim runtime path에서 제거했습니다. Incident와 recovery는 HumanoidSim incident taxonomy와 recovery protocol을 기준으로 처리하고, ManSim은 시뮬레이션 상황을 발생/관찰하는 역할에 집중합니다.

## Rolling Horizon Events

- `ROLLING_HORIZON_WINDOW_START`
- `ROLLING_HORIZON_CANDIDATE_COLLECTED`
- `ROLLING_HORIZON_DISPATCH`
- `ROLLING_HORIZON_TASK_REQUEUED`
- `ROLLING_HORIZON_TASK_SKIPPED`

3D Replay Studio는 이 event들을 읽어 Task Pool 패널에 window, stable task id, rank, target, assigned worker, status를 표시합니다. `POOL`은 아직 dispatch되지 않은 후보, `DISPATCHED`는 worker queue에 들어간 task, `REQUEUED`는 window boundary에서 아직 시작하지 않아 pool로 되돌아간 task, `SKIPPED`는 dispatch 직전 stale/resource conflict로 제거된 task입니다.

## Preventive Maintenance

`PREVENTIVE_MAINTENANCE`는 idle machine을 대상으로 하는 HumanoidSim composite task입니다. Rolling horizon에서는 낮은 base rank를 갖지만, aging priority 덕분에 pool에서 오래 기다릴수록 effective rank가 개선됩니다.

5일 검증 run 기준으로 `PREVENTIVE_MAINTENANCE`는 pool에 수집되고, dispatch되며, 실제 task timeline과 KPI `humanoid_task_minutes`에도 반영되는 것을 확인했습니다. 보이지 않는 경우는 대개 해당 window에서 PM 조건이 없거나 더 높은 rank의 feasible task가 먼저 처리된 상황입니다.
## Simulation-Based ADP

`simulation_based_adp`는 `mfg_flow_shop / maximize_throughput` 전용 event-driven SMDP
정책입니다. Worker polling과 분리된 coordinator가 같은 시각의 idle worker를 모아 feasible
worker-task bipartite graph를 구성하고, attention post-decision value network와 beam search로
joint matching을 선택합니다. 기본 action set에는 `WAIT`가 포함되며 일반 task는 한 worker만,
`REPAIR_MACHINE`은 repair capacity까지 공동 배정할 수 있습니다.

즉각 보상은 다음 decision epoch까지 실제로 발생한 `COMPLETED_PRODUCT` 수입니다. Potential-based
shaping은 사용하지 않으며 episode 종료 후 raw reward의 완전한 Monte Carlo return을 역산해
post-decision value를 MSE로 회귀합니다. TD, bootstrapping, target network와 n-step target은
사용하지 않습니다.

초기 random episode는 기존 fixed priority나 dedicated-role heuristic을 참조하지 않습니다.
Mandatory charging 같은 safety action만 먼저 고정한 뒤, 각 worker가 `WAIT`와 현재 남아 있는
conflict-free feasible task를 합친 선택지 중 하나를 균등 무작위 선택합니다. 후보 task가
`k`개라면 해당 worker의 WAIT 확률은 `1/(k+1)`입니다. 이후 policy iteration rollout은
epsilon-greedy와 value beam search를 사용합니다.

학습은 worker 3명 전용 순수 on-policy batch 방식입니다. 초기 20 episode로 한 번 학습한 뒤
데이터를 폐기하고, 이후에는 현재 정책을 고정해 생성한 100 episode만으로 업데이트합니다.
이 과정을 10회 반복하여 총 training episode 수를 1,020으로 유지합니다. 가치망은 설정된 learning
rate와 MSE loss로 업데이트합니다. Episode 종료 시 raw
transition은 complete MC target과 model input만 담은 CPU compact tensor로 바뀌며, policy update
후 해당 batch 전체를 폐기합니다. 과거 정책 sample은 다음 iteration에 섞지 않습니다.

Episode 생성은 `spawn` 방식의 CPU process 20개로 병렬 실행합니다. 각 policy iteration은
20-episode wave 다섯 번으로 구성하며, 다섯 wave가 모두 완료된 뒤에만 network를 업데이트합니다.
한 iteration의 child process는 동일한 frozen CPU model snapshot을 사용하고 결과는 episode ID
순서로 병합합니다. 초기 random rollout은 1 wave, policy rollout은 50 wave, checkpoint
diagnostic은 11 wave, final selection은 3 wave입니다. Child process는 Torch/BLAS thread를 하나만 사용하며 main process만 GPU update를
담당합니다. 표준 학습은 `cuda:0`을 필수로 사용하며 CUDA 지원 PyTorch가 확인되지 않으면
rollout 시작 전에 실패합니다. 병렬 실행시간, speedup, 효율, active-slot utilization,
process별 memory와 GPU/PyTorch/CUDA/cuDNN 정보는 `wave_metrics.csv`, checkpoint manifest 및
`training_dashboard.html`에 기록됩니다.

가치함수의 행동 선택 정확도는 학습 batch와 독립된 고정 반사실 MC probe로 진단합니다. 고정
seed의 동일 의사결정 상태까지 action prefix를 재현하고, WAIT를 포함한 제한된 후보 joint
matching을 각각 실행한 뒤 동일 Random Feasible continuation의 완료 제품 수를 경험적 목표로
사용합니다. Dashboard에는 행동가치 순위 상관계수, 후보집합 Top-1 일치율과 선택 행동 regret을
표시합니다. 고정 probe MSE와 미선택 행동 MSE는 행동별 MC 목표의 표본 수와 분산이 달라 신뢰하기
어려우므로 계산하거나 표시하지 않습니다. Probe 값은 제한된 후보집합 내 경험적 비교이며 수학적
전역 최적행동을 의미하지 않습니다.

미관측 행동 과대평가는 두 지표로 별도 진단합니다. OOD 선택률은 greedy 선택 중 직전 on-policy
batch의 95% 지지영역을 벗어난 비율이고, OOD 과대평가 초과값은 OOD 선택의 평균 가치 예측오차에서
지지영역 내 선택의 평균 가치 예측오차를 뺀 값입니다. 두 값이 함께 상승하면 학습 근거가 약한
행동을 낙관적으로 선택하는 현상의 직접적인 신호로 해석합니다. 초기 random batch에는 비교할
이전 support가 없으므로 OOD 값이 비어 있는 것이 정상입니다.

SGD `batch_size=512`는 episode 수가 아니라 현재 100-episode compact batch에서 뽑는 state-return
sample 수입니다. Iteration 0부터 10까지 모든 checkpoint를 training seed 10개와 validation seed
10개로 진단하며, 최종 후보 하나는 별도의 60개 seed로 final selection합니다. Best checkpoint는 raw completed products
평균, 표준편차, 이른 iteration 순서로 선택합니다. 학습, screening, final selection과 held-out
test seed는 실행 전에 상호 중복이 없는지 검증합니다.

수렴 진단용 `mfg_flow_shop_throughput_10x100.yaml` profile은 checkpoint 0부터 마지막
checkpoint까지 빠짐없이 평가합니다. 각 value update 직후 모델을 frozen snapshot으로 만들고,
같은 wave에서 training seed 10개와 held-out validation seed 10개를 모두 `epsilon=0`으로
평가합니다. Training dashboard의 첫 그래프는 두 평가 평균과 95% 신뢰구간을 같은 checkpoint
축에 표시하고, checkpoint가 다음 update용으로 생성한 exploratory rollout 평균은 점선으로
분리합니다. 따라서 두 실선의 격차는 과적합, 두 실선의 동반 하락은 정책 붕괴로 해석할 수
있으며 exploratory rollout과 validation policy의 차이를 혼동하지 않습니다.

최종 정책 비교는 worker 3명, 5일 throughput, held-out seed 5개에서 `random_feasible_dispatch`,
네 rule-based 정책과 ADP를 비교합니다. Seed별 paired difference의 bootstrap 95% CI 하한이
0보다 클 때만 ADP 우위를 입증한 것으로 보고합니다.

`random_feasible_dispatch`는 ADP 초기 rollout과 같은 hard constraint 및 event-driven joint
decision coordinator를 사용하지만 value network와 checkpoint는 사용하지 않습니다. Mandatory
charging을 먼저 고정하고 나머지는 conflict-free feasible task 중 하나를 균등 무작위
선택하므로, 학습 효과를 rule-based 정책뿐 아니라 비학습 random baseline과도 분리해 평가합니다.

Afterstate에는 선택된 worker-task edge를 Boolean mask로 표시합니다. Edge의 travel time, 예상
duration과 battery margin은 기존 pair feature를 그대로 재사용합니다. 표준 ADP 학습과 Random
Feasible 비교 모두 `allow_wait_action=true`를 사용합니다. 모든 worker가 WAIT를 선택한
afterstate만 review interval만큼 시간을 전진시키며, 일부 worker만 WAIT하면 다른 작업이 실제로
진행되므로 인위적으로 시간을 전진시키지 않습니다.

Checkpoint에는 scenario, objective, HumanoidSim timing fingerprint, ADP feature schema, 지원
worker 범위와 environment fingerprint를 기록합니다. Environment fingerprint에는 설비 ID와 수,
가공시간 분포, 유한 버퍼 용량, inspection capacity와 지도 구조가 포함됩니다. 하나라도 현재
run과 다르면 rule-based fallback 없이 오류로 종료합니다.
