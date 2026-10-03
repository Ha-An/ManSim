# manufacturing_sim

`manufacturing_sim`은 ManSim의 simulator-core package입니다. Factory state, SimPy process loop, humanoid task execution, event logging, KPI aggregation을 담당하며 LLM manager UI나 dashboard rendering 자체는 포함하지 않습니다.

이 문서는 ManSim `v0.6.0` simulator core 기준입니다.

## 책임 범위

- world state transition
- worker, machine, item entity model
- feasible task candidate 생성
- `HumanoidSim` task hierarchy execution bridge
- queue, machine, inspection, battery, repair side effect
- tile 기반 pathfinding, reservation, traffic observation
- warehouse material shelf, completed product zone, scrap disposal flow
- event logging과 KPI source 생성

## 주요 모듈

- `simulation/scenarios/manufacturing/world.py`: factory world state, task enumeration, execution, KPI aggregation
- `simulation/scenarios/manufacturing/humanoid_runtime.py`: `HumanoidSim` catalog/profile validation, step flattening, primitive execution bridge
- `simulation/scenarios/manufacturing/grid_map.py`: tile map, pathfinding, occupancy
- `simulation/scenarios/manufacturing/traffic.py`: path overlap, tile/edge conflict, near miss detection
- `simulation/scenarios/manufacturing/entities.py`: `Worker`, `Machine`, `Task`, `Item` dataclass와 domain state
- `simulation/scenarios/manufacturing/processes.py`: SimPy process orchestration
- `simulation/scenarios/manufacturing/logging.py`: `events.jsonl` event writer
- `simulation/scenarios/manufacturing/run.py`: manufacturing scenario entrypoint
- `simulation/scenarios/manufacturing/throughput_policy.py`: bottleneck score와 OR-Tools optimizer 입력 계산
- `simulation/scenarios/manufacturing/task_rules.py`: `mfg_flow_shop`의 19개 역할 계약, selector 검증, 전담 역할 LPT 배정
- `simulation/operational_complexity.py`: HumanoidSim task complexity 기반 OTC 집계
- `simulation/pre_run_diagnostics.py`: factory 실행 전 multi-humanoid 운영 지표 계산

## Humanoid Runtime

Worker는 `HumanoidSim`의 `HumanoidStateSnapshot`과 `TaskSpec -> child Task -> Primitive` 정의를 사용합니다.

- State, Task, Primitive, Incident 의미는 `HumanoidSim`이 소유합니다.
- ManSim은 factory scenario에서 발생한 event와 side effect를 기록합니다.
- Task 후보는 기존 priority family도 보존하지만 실행과 Replay/KPI 기준은 `task_code`입니다.
- Primitive step 중 domain action은 ManSim queue/machine/inspection/battery side effect를 호출합니다.
- `LOAD_MACHINE`과 `LOAD_UNLOAD_TRANSFER_INTERFACE`는 queue와 machine/inspection desk 사이의 carry 이동을 tile path로 수행합니다. `INSPECT_PRODUCT`는 desk에 staged된 product를 검사하기만 하며, `SETUP_MACHINE`은 input이 이미 적재된 machine에서 fixture, recipe, program setup만 수행합니다.
- 정상 Task의 모든 primitive와 이동시간은 scenario별 `configs/task_primitive_timing/<scenario>.yaml`에서 exact step path 기준으로 정의합니다. HumanoidSim integration config의 공통 primitive 기본시간은 사용하지 않습니다.

## Decision Modes

`world.py`는 여러 decision mode가 같은 task execution runtime을 공유하도록 구성되어 있습니다.

- `adaptive_priority`: 즉시 dispatch scripted baseline
- `immediate_shared`: `mfg_flow_shop` granular fixed-priority immediate shared baseline
- `immediate_dedicated_roles`: `mfg_flow_shop` granular fixed-priority immediate LPT-owner baseline
- `bottleneck_aware_dispatch`: factory bottleneck/throughput score 기반 즉시 dispatch
- `rolling_horizon_aging_priority`: strict-periodic rolling pool + task-code aging rank
- `rolling_horizon_shared`: `mfg_flow_shop` strict 5-minute shared pool + fixed granular priority
- `rolling_horizon_dedicated_roles`: strict-periodic pool + scenario task-code allowlist 또는 `mfg_flow_shop` granular LPT owner
- `rolling_horizon_throughput_optimizer`: strict-periodic window를 OR-Tools CP-SAT로 푸는 throughput optimizer

모든 rolling mode는 worker polling과 독립적으로 `t=5, 10, 15, ...`에 dispatch합니다. 시각 0에는 `[0,5]` window와 후보 pool만 열리고 일반 task의 최초 할당은 시각 5입니다. `mfg_flow_shop`의 critical repair는 예외적으로 idle worker queue를 즉시 갱신할 수 있으며, 실행 중 task는 선점하지 않습니다.
- `openclaw_adaptive_priority`: OpenClaw manager loop가 priority를 조정하는 optional mode

`mfg_flow_shop`의 두 dedicated mode는 역할 1~16과 예방정비 역할 19를 예상 busy time 기준으로 한 worker에게만 배정합니다. 역할 14~16은 inspection desk load, stationary inspection, desk unload를 각각 나타냅니다. 역할 17 `MANAGE_ROBOT_POWER`와 역할 18 `REPAIR_MACHINE`은 모든 worker가 가지며, 수리만 설정된 capacity 안에서 공동 수행할 수 있습니다. 다른 manufacturing scenario의 기존 allowlist와 battery delivery 정책은 별도로 유지됩니다.

`mfg_flow_shop`의 Station 1·2는 각각 병렬 설비 2대를 사용합니다. 유한 buffer의 실제 점유량과 inbound reservation을 함께 제한하며, output slot이 없으면 완료 item이 machine에 남아 다음 cycle을 막습니다. Material 보충량은 목표재고가 아니라 아직 충족되지 않은 machine input 수요에서 queue와 inbound 수량을 뺀 값입니다.

Machine 고장은 실제 가공시간 기준 평균 300분 지수분포를 사용합니다. PM은 240 가공분마다 due가 되고 완료 후 240 가공분 동안 hazard를 0.5배로 낮춥니다. Battery-risk task도 후보에 남겨 정책이 충전과 생산 task 중 선택하고, 방전 worker는 다음 day boundary에 자신의 dock에 SOC 100%로 복귀합니다.

## Factory Flow Extensions

- Warehouse material은 `warehouse_material_shelf`의 개별 slot에 보관합니다.
- Worker는 material slot service tile까지 이동해야 pickup할 수 있습니다.
- Inspection은 interface load, stationary inspection, interface unload의 세 top-level task로 분리되며 하나의 capacity-1 desk를 공유합니다. 각 단계 완료 worker는 인접 `inspection_staging`으로 tile 단위 이동해 단일 desk service tile을 비웁니다. Pass product는 이후 `CompletedProducts` zone의 `completed_product_buffer`에 dropoff되어야 최종 count에 반영됩니다.
- Inspection fail product는 `inspection_scrap_queue`에 쌓인 뒤 `COLLECT_WASTE_OR_SCRAP` task로 `scrap_disposal_bin`까지 batch 운반됩니다.
- Product 운반은 weight multiplier가 적용되며, 일반 mode에서는 `HANDOVER_ITEM`으로 최대 2명의 공동 운반을 표현할 수 있습니다.

## Boundary

아래 기능은 repository root의 상위 layer가 담당합니다.

- Hydra config composition: `configs/`, `runtime/`
- OpenClaw manager orchestration: `agents/`, `openclaw/`
- LLM Wiki, Graphify, run-series knowledge: `knowledge/`
- dashboard/replay rendering: `dashboards/`, `replay_studio/`, `replay_studio_3d/`

Simulator core를 수정할 때는 [docs/simulator_core_guide.md](../docs/simulator_core_guide.md)와 [docs/humanoid_worker_model.md](../docs/humanoid_worker_model.md)의 runtime boundary를 함께 확인합니다.
## ADP package

`manufacturing_sim.adp`는 `mfg_flow_shop / maximize_throughput`용 선택적 simulation-based ADP 구현입니다.
`coordinator.py`가 event-driven joint dispatch, `encoding.py`가 variable-size worker/task state와
selected assignment afterstate, `model.py`가 bipartite attention value network를 담당합니다.
`train.py`는 기본 n-step TD와 legacy MC 실행 진입점입니다. `td_train.py`, `td.py`는 초기부터
동일한 n-step target, bounded episode replay와 target network를 사용합니다. `td_dashboard.py`는
TD 적합 오차와 독립 greedy MC 예측 오차를 분리합니다. Reward는 completed-product 증가량이고
value loss는 MSE로 고정됩니다. 표준 비교에서는
WAIT를 비활성화하고 Random rollout은 conflict-free feasible task를 균등하게 샘플링합니다. 고정
반사실 MC probe는 제한된 후보 행동의 순위, Top-1 일치와 선택 regret을 진단합니다. OOD support
진단은 greedy 선택이 직전 on-policy compact batch의 95% 지지영역을 벗어나는 비율과 그 선택의
상대적인 가치 과대평가 오차를 기록합니다. 신뢰하기 어려운 고정 probe MSE와 미선택 행동 MSE는
계산하지 않습니다.
Feature schema v10은 중복 global 입력 5개를 제거한 30차원이며, 실제 창고 재고와 실시간 설비 잔여시간을 사용합니다.
Station별 finite-buffer 및 machine 상태 집계, repair urgency, battery risk, terminal output까지의 예상 잔여시간과 선택 edge의 downstream progress,
blockage 해소 여부, 목적지 가용 용량을 포함합니다. Beam 후보 가치 엔트로피는 greedy 최종 후보
가치의 집중도를 기록하며 생산량과 함께 정책의 확신 또는 과신을 진단합니다.
기존 worker 3~6 profile은 `configs/adp/mfg_flow_shop_throughput_multifleet.yaml`에 보존되며,
PyTorch는 `requirements-adp.txt`로만 설치합니다.
