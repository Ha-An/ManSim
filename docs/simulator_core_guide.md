# 시뮬레이터 코어 가이드

이 문서는 `manufacturing_sim/` 아래에 있는 제조 시뮬레이터 자체를 설명합니다.  
대상은 LLM orchestration이 아니라, 공장 상태 전이와 작업 실행을 담당하는 simulator core입니다.

## 1. 범위
시뮬레이터 코어의 책임은 다음으로 제한됩니다.
- 공장 상태 보유
- 시간 진행
- 기계/작업자/아이템 상태 전이
- 가능한 작업 후보 생성
- 선택된 작업 실행
- 이벤트 로깅
- 일별/최종 KPI 집계

다음 계층은 시뮬레이터 바깥 책임입니다.
- manager orchestration
- prompt 구성
- OpenClaw runtime
- dashboard export wiring
- 실험 preset 조합

즉 `manufacturing_sim/`은 "세상이 어떻게 움직이는가"를 담당하고,  
repository root의 `agents/`, `runtime/`, `dashboards/`, `configs/`는 "무엇을 시킬 것인가"를 담당합니다.

## 2. 핵심 파일
- `manufacturing_sim/simulation/scenarios/manufacturing/world.py`
  - 공장 상태, 작업 후보 생성, 작업 실행, 집계 로직
- `manufacturing_sim/simulation/scenarios/manufacturing/processes.py`
  - SimPy process loop
- `manufacturing_sim/simulation/scenarios/manufacturing/entities.py`
  - `Machine`, `Agent`, `Task`, `Item`, `MachineState`
- `manufacturing_sim/simulation/scenarios/manufacturing/logging.py`
  - `events.jsonl` 기록
- `manufacturing_sim/simulation/scenarios/manufacturing/run.py`
  - run 진입점, artifact export, dashboard 호출

## 3. 시간 모델
시뮬레이터는 SimPy 기반의 discrete-event simulation입니다.

- 전역 시간 단위: `분`
- 하루 길이: `horizon.minutes_per_day`
- 총 일수: `horizon.num_days`

현재 시뮬레이션 시간 `t`가 있으면, day는 대략 다음처럼 계산됩니다.
- `day = floor(t / minutes_per_day) + 1`

즉 하루가 240분이면,
- `0 ~ 239.999...`는 day 1
- `240 ~ 479.999...`는 day 2
입니다.

이 구조 덕분에
- 실제 작업은 continuous simulation time 위에서 진행되고
- manager loop나 summary는 day boundary에서 끊어 읽을 수 있습니다.

## 4. 공장 토폴로지
현재 기본 시나리오는 2단계 가공 + inspection 구조입니다.

대략적인 흐름
1. Warehouse material 공급
2. Station1 처리
3. Station2 처리
4. Inspection
5. 완제품 accepted 또는 scrap

핵심 queue / buffer
- `material_queues`
  - station별 raw material 입력 대기열
- `intermediate_queues`
  - station2 및 inspection 입력 대기열
- `output_buffers`
  - 각 stage에서 처리 후 다음 이동 전까지 머무는 출력 버퍼

핵심 설비 수는 시나리오 config에서 결정됩니다.
- `factory.num_agents`
- `factory.machines_per_station`
- `factory.processing_time_min`

## 5. 엔티티 모델

### Machine
기계는 다음 상태를 가집니다.
- `WAIT_INPUT`
- `SETUP`
- `PROCESSING`
- `DONE_WAIT_UNLOAD`
- `BROKEN`
- `UNDER_REPAIR`
- `UNDER_PM`
- `IDLE`

Machine은 현재 입력물, 출력물, PM 시점, 고장 이력, 누적 처리시간 등을 함께 보유합니다.

### Agent
작업자는 이동/운반/수리/PM/검사 지원 등 실제 현장 작업을 수행합니다.

주요 상태
- 현재 위치
- 방전 여부
- 현재 작업
- 배터리 교체 시점
- 들고 있는 item
- suspended task

현재 구조에서 worker는 high-level planner가 직접 task instance를 지정받기보다,  
현재 가능한 후보 중 하나를 선택하는 deterministic executor입니다.

### Item
item은 raw material, intermediate, finished product를 모두 포함하는 일반 구조입니다.

주요 속성
- `item_id`
- `item_type`
- `created_at`
- `current_station`

`created_at`은 lead time 계산에 중요합니다.

### Task
worker가 선택할 수 있는 실행 단위입니다.

주요 속성
- `task_type`
- `priority_key`
- `priority`
- `location`
- `payload`
- `selection_meta`

task는 "실행 가능한 조치"를 표현합니다.  
예:
- setup
- unload
- transfer
- repair
- PM
- inspect
- battery swap

## 6. SimPy process 구조
시뮬레이터는 여러 loop를 동시에 돌립니다.

### machine_lifecycle
각 machine마다 하나씩 존재합니다.

역할
- 기계가 broken인지 확인
- output이 남아 있으면 unload 대기 상태 유지
- input이 부족하면 대기
- input이 준비되면 processing 시작

### machine_failure_monitor
각 machine마다 하나씩 존재합니다.

역할
- exponential 고장 도착 모델에 따라 stochastic failure 발생
- PM 상태면 failure rate를 낮춤

### agent_work_loop
각 agent마다 하나씩 존재합니다.

역할
- 현재 가능한 task 선택
- task 실행
- interruption 처리
- skipped / interrupted / completed 기록

### agent_battery_monitor
각 agent마다 하나씩 존재합니다.

역할
- 배터리 잔량 감시
- low-battery alert
- 방전 시 agent를 discharged 상태로 전환

### snapshot_loop
주기적으로 minute snapshot을 남깁니다.

이 snapshot은 이후 dashboard와 KPI 계산에 사용됩니다.

## 7. 작업 선택 구조
worker는 아무 task나 실행하지 않습니다.  
`world.select_task_for_agent()`가 선택 순서를 정합니다.

현재 구조의 개념적 순서는 다음과 같습니다.
1. hard constraint / safety guard
2. local response
3. compiled mailbox / focus
4. deterministic priority dispatch

즉 worker는
- 현재 feasible candidates를 모으고
- safety/local-response를 먼저 반영한 뒤
- compiled policy를 bias로 사용해
- 최종 작업을 고릅니다.

중요한 점
- simulator는 manager reasoning을 직접 구현하지 않습니다.
- simulator는 "가능한 행동 집합"과 "그 행동을 실행했을 때의 상태 전이"를 제공합니다.

## 8. 기계 처리 흐름
가공 station에서는 일반적으로 다음 순서를 탑니다.

1. 입력 준비
2. setup
3. processing
4. output 생성
5. unload 대기
6. 다음 stage로 transfer

processing 완료 후 output item은 새로 생성되며,
가능하면 source item의 생성 시점을 이어받아 downstream lead time 계산이 왜곡되지 않게 합니다.

## 9. 고장 / 수리 / PM
기계는 stochastic failure를 겪을 수 있습니다.

관련 설정
- `machine_failure.mean_time_to_fail_min`
- `repair_time_min`
- `pm_time_min`
- `pm_effect_duration_min`
- `pm_lambda_multiplier`
- `pm_interval_target_min`

핵심 동작
- failure 발생 시 machine은 `BROKEN`
- repair task가 시작되면 `UNDER_REPAIR`
- PM task가 시작되면 `UNDER_PM`
- PM 효과 동안은 failure rate가 줄어듭니다

즉 PM은 단순 비용이 아니라 미래 고장 확률을 낮추는 예방 조치입니다.

## 10. 배터리 모델
agent는 유한한 배터리 주기를 가집니다.

관련 설정
- `agent.battery_swap_period_min`
- `battery_pickup_time_min`
- `battery_delivery_extra_min`

핵심 동작
- 일정 시간 이상 작업하면 배터리가 줄어듭니다
- low-battery alert가 발생할 수 있습니다
- 임계치를 넘으면 battery swap 또는 battery delivery task가 필요합니다
- 완전히 소진되면 worker는 `discharged` 상태가 됩니다

이 배터리 모델은 생산성뿐 아니라 variance에도 영향을 줍니다.  
특히 inspection closer나 support worker가 늦은 시점에 방전되면 closeout이 크게 흔들릴 수 있습니다.

## 11. 이벤트 로깅
모든 run은 `events.jsonl`을 남깁니다.

logger는 각 이벤트에 대해 다음을 기록합니다.
- `t`
- `day`
- `type`
- `entity_id`
- `location`
- `details`

이 로그는 replay, reasoning dashboard, 사후 분석의 기준 데이터입니다.

대표 이벤트
- machine start / end
- failure / repair / PM
- battery 관련 이벤트
- move / transfer / inspect
- coordination review 관련 이벤트

## 12. 일별 집계
각 day가 끝나면 `world.finalize_day(day)`가 일별 요약을 만듭니다.

여기서 집계되는 값 예시
- products
- scrap
- inspection backlog
- incident count
- coordination incident count
- local response task count
- commitment dispatch count
- stage별 처리시간
- queue/buffer 압력
- role / priority snapshot

이 요약은
- `daily_summary.json`
- `day_summary_memory.json`
- reviewer 입력
으로 이어집니다.

## 13. 최종 KPI 집계
run 종료 후 `world.finalize_kpis()`가 최종 KPI를 만듭니다.

대표 KPI
- `total_products`
- `scrap_count`
- `avg_daily_products`
- `throughput_per_sim_hour`
- `downstream_closure_ratio`
- `machine_utilization`
- `machine_broken_ratio`
- `agent_discharged_time_min_total`
- `buffer_wait_avg_min`
- `completed_product_lead_time_avg_min`

즉 최종 KPI는 단순 생산량뿐 아니라
- 설비 건강
- worker availability
- buffer 대기
- closeout 품질
까지 함께 보여줍니다.

## 14. run 진입점 역할
`run.py`는 simulator와 decision module을 연결하는 진입점입니다.

하루 단위 실행 순서
1. observation 생성
2. decision module reflect
3. decision module propose_jobs
4. `world.start_day(...)`
5. 하루 종료까지 SimPy 실행
6. `world.finalize_day(...)`
7. 필요 시 `decision_module.discuss(...)`

run 종료 후
- `daily_summary.json`
- `kpi.json`
- `run_meta.json`
- `minute_snapshots.json`
을 기록하고
- dashboard export를 호출합니다.

즉 `run.py`는 simulator core를 외부 orchestration과 접속시키는 얇은 어댑터입니다.

## 15. 설정 파일과 simulator의 관계
simulator는 주로 scenario config를 읽습니다.

예:
- `configs/scenario/mfg_basic.yaml`

주요 입력
- horizon
- factory topology
- movement time
- inspection time
- failure / PM / battery
- inventory targets
- dispatcher timing

반면 decision config는 simulator 바깥 계층과 더 강하게 연결됩니다.

예:
- `configs/decision/openclaw_adaptive_priority.yaml`

즉
- scenario config = 세상 물리
- decision config = manager/worker 정책
으로 보면 됩니다.

## 16. simulator를 수정할 때 주의할 점

### simulator 안에서 해결해야 하는 문제
- 상태 전이 버그
- 고장/PM/배터리 모델 버그
- 작업 후보 생성 오류
- KPI 집계 오류
- event logging 누락

### simulator 바깥에서 해결해야 하는 문제
- manager prompt
- reviewer schema
- strategist output contract
- compiler policy translation
- OpenClaw runtime / gateway / backend

이 경계를 흐리면,  
단기적으로는 성능이 오를 수 있어도 장기적으로는 실험 해석이 어려워집니다.

## 17. 디버깅 체크리스트
시뮬레이터 문제를 볼 때는 보통 이 순서가 좋습니다.

1. `run_meta.json`
2. `kpi.json`
3. `daily_summary.json`
4. `minute_snapshots.json`
5. `events.jsonl`
6. `shift_policy_history.json`
7. `day_review_memory.json`

이 순서가 좋은 이유
- 먼저 결과와 상태를 보고
- 그다음 timeline과 raw event를 확인하고
- 마지막에 manager layer가 그 상태를 어떻게 해석했는지 보는 흐름이 되기 때문입니다.

## 18. 한 줄 요약
ManSim의 simulator core는  
**제조 공장의 상태 전이와 작업 실행을 담당하는 deterministic + discrete-event 세계 모델**입니다.

manager가 아무리 복잡해도, 결국 실험의 기반은 이 simulator가 제공하는
- feasible task
- 물리 제약
- 실패/복구 dynamics
- KPI 집계
에 의해 결정됩니다.
