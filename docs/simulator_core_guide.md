# 시뮬레이터 코어 가이드

이 문서는 `manufacturing_sim/` 아래의 제조 시뮬레이터 코어를 설명합니다.

여기서 말하는 코어는 다음 책임만 가집니다.

- 공장 상태 보유
- SimPy 기반 시간 진행
- Worker, Machine, Item 상태 전이
- 실행 가능한 Task 후보 생성
- 선택된 Task 실행
- 이벤트 로그 기록
- KPI와 스냅샷 산출

반대로 다음은 코어 바깥 책임입니다.

- OpenClaw/LLM manager orchestration
- 프롬프트 구성
- 허브/대시보드 라우팅
- 결과물 표시 UI

즉 코어는 "공장이 실제로 어떻게 움직이는가"를 담당하고, 상위 계층은 "무슨 의사결정을 내릴 것인가"를 담당합니다.

## 1. 주요 파일

- `manufacturing_sim/simulation/scenarios/manufacturing/world.py`
  - 공장 런타임 상태, Task 후보 생성, Task 실행, 상태 전이, KPI 집계의 중심입니다.
- `manufacturing_sim/simulation/scenarios/manufacturing/processes.py`
  - SimPy process loop를 정의합니다.
- `manufacturing_sim/simulation/scenarios/manufacturing/entities.py`
  - `Machine`, `Worker`, `Task`, `Item`과 canonical state enum을 정의합니다.
- `manufacturing_sim/simulation/scenarios/manufacturing/logging.py`
  - `events.jsonl` 로그를 기록합니다.
- `manufacturing_sim/simulation/scenarios/manufacturing/run.py`
  - 시뮬레이션 실행 진입점과 artifact export를 담당합니다.

## 2. 시간 모델

이 시뮬레이터는 SimPy 기반 discrete-event simulation입니다.

- 전역 시간 단위: `분`
- 하루 길이: `horizon.minutes_per_day`
- 총 일수: `horizon.num_days`

현재 시간 `t`의 일차는 아래처럼 계산됩니다.

```text
day = floor(t / minutes_per_day) + 1
```

예를 들어 하루가 `240`분이면:

- `0 <= t < 240` 은 1일차
- `240 <= t < 480` 은 2일차

입니다.

## 3. 공장 구조

기본 시나리오는 2단계 가공 + inspection 구조입니다.

흐름은 다음과 같습니다.

1. Warehouse에서 material 공급
2. Station 1 가공
3. Station 2 가공
4. Inspection
5. Warehouse completed buffer 또는 scrap

주요 큐/버퍼는 다음과 같습니다.

- `material_queues`
  - Station별 raw material 입력 대기열
- `intermediate_queues`
  - Station 2 입력, Inspection 입력 대기열
- `output_buffers`
  - 각 stage 처리 후 다음 이송 전까지 머무는 출력 버퍼

관련 핵심 설정:

- `factory.num_workers`
- `factory.machines_per_station`
- `factory.processing_time_min`
- `movement.*`
- `machine_failure.*`
- `worker.*`

`factory.num_agents`, `agent.*`는 legacy alias로만 읽습니다. 새 설정의 표준은 `num_workers`, `worker.*`입니다.

## 4. Canonical 엔티티 모델

현재 코드는 Replay Studio용 별도 의미 추론을 최소화하기 위해, 시뮬레이션 내부에서 canonical 상태를 직접 정의하고 로그로 남깁니다.

핵심 원칙은 다음과 같습니다.

- Worker 상태는 `WorkerState`로 직접 기록합니다.
- Machine 상태는 `MachineState`로 직접 기록합니다.
- Item 상태는 `ItemState`로 직접 기록합니다.
- Replay Studio는 이 값을 1:1로 sprite/style에 매핑합니다.
- `carry`는 WorkerState가 아니라 `cargo` 축으로 기록합니다.

## 5. Machine 상태

`entities.py`의 `MachineState`가 canonical machine state입니다.

| 상태 | 의미 |
| --- | --- |
| `IDLE` | 입력은 준비되었고 다음 cycle 시작을 기다리는 상태 |
| `WAIT_INPUT` | material 또는 intermediate 입력이 부족한 상태 |
| `SETUP` | Worker가 설비에 입력물을 세팅 중인 상태 |
| `PROCESSING` | 설비가 실제 가공 중인 상태 |
| `DONE_WAIT_UNLOAD` | 가공은 끝났고 output이 설비에 남아 unload를 기다리는 상태 |
| `BROKEN` | 고장 상태 |
| `UNDER_REPAIR` | 수리 팀이 붙어서 repair 진행 중인 상태 |
| `UNDER_PM` | 예방정비 진행 중인 상태 |

Machine 주요 필드:

- `input_material`, `input_intermediate`
- `output_intermediate`
- `broken`, `failed_since`
- `repair_team`
- `repair_work_remaining_min`
- `setup_owner`, `unload_owner`, `pm_owner`

특히 repair는 단일 owner가 아니라 `repair_team` 중심 모델입니다. 남은 작업량은 `repair_work_remaining_min`으로 관리하고, 진행 속도는 팀 크기에 선형 비례합니다.

## 6. Worker 상태

`entities.py`의 `WorkerState`가 canonical worker state입니다.

| 상태 | 의미 |
| --- | --- |
| `IDLE` | 현재 수행 중인 작업이 없는 상태 |
| `MOVING` | 구역 간 이동 중 |
| `SUPPLYING_MATERIAL` | Warehouse에서 material을 가져와 Station으로 공급 중 |
| `TRANSFERRING_INTERMEDIATE` | intermediate 또는 product를 다음 구간으로 이송 중 |
| `SETTING_UP_MACHINE` | 설비 setup 작업 중 |
| `UNLOADING_MACHINE` | 설비 output unload 작업 중 |
| `INSPECTING_PRODUCT` | inspection 작업 중 |
| `REPAIRING_MACHINE` | 고장 설비 repair 작업 중 |
| `PREVENTIVE_MAINTENANCE` | 예방정비 작업 중 |
| `BATTERY_SWAPPING` | 본인 배터리 교체 중 |
| `BATTERY_DELIVERING` | 다른 Worker를 위한 배터리 전달 중 |
| `WAITING` | Task는 선택되었지만 이동 전/실행 전 대기 상태 |
| `DISCHARGED` | 배터리 방전으로 작업 불가 |

Worker 주요 필드:

- `worker_id`
- `location`
- `state`
- `current_task_id`, `current_task_type`
- `discharged`, `discharged_since`
- `in_transit_from`, `in_transit_to`, `in_transit_progress`, `in_transit_total_min`
- `carrying_item_id`, `carrying_item_type`
- `battery_service_owner`, `awaiting_battery_from`
- `suspended_task`

중요한 점:

- 코드 표준 명칭은 `Worker`입니다.
- `Agent = Worker` alias는 기존 코드와 대시보드 호환용입니다.
- `carry`는 WorkerState가 아닙니다.
- "빈손 이동"과 "물건을 든 이동"은 둘 다 `MOVING`일 수 있고, 차이는 `cargo`에 기록합니다.

## 7. Item 상태

`entities.py`의 `ItemState`가 canonical item state입니다.

| 상태 | 의미 |
| --- | --- |
| `CREATED` | 생성 직후 |
| `IN_STORAGE` | Warehouse 보관 중 |
| `IN_QUEUE` | 일반 queue 대기 중 |
| `CARRIED_BY_WORKER` | Worker가 운반 중 |
| `LOADED_ON_MACHINE` | 설비 입력으로 적재됨 |
| `PROCESSING` | 설비에서 가공 중 |
| `WAITING_MACHINE_UNLOAD` | 가공 완료 후 설비에 남아 unload 대기 중 |
| `WAITING_INSPECTION` | inspection queue 대기 중 |
| `INSPECTING` | inspection 진행 중 |
| `WAITING_INSPECTION_OUTPUT` | inspection pass 후 output 이동 대기 중 |
| `COMPLETED` | 완료품으로 확정됨 |
| `SCRAPPED` | 불량으로 폐기됨 |

Item 주요 필드:

- `item_id`
- `item_type`
- `created_at`
- `state`
- `current_station`
- `metadata`

## 8. Task 모델

Task는 "현재 공장 상태에서 실행 가능한 조치"를 뜻합니다.

핵심 필드:

- `task_id`
- `task_type`
- `priority_key`
- `priority`
- `location`
- `payload`
- `selection_meta`

대표 `task_type`:

- `BATTERY_SWAP`
- `TRANSFER`
- `SETUP_MACHINE`
- `UNLOAD_MACHINE`
- `REPAIR_MACHINE`
- `PREVENTIVE_MAINTENANCE`
- `INSPECT_PRODUCT`

`TRANSFER`는 하나의 task type이지만 `payload.transfer_kind`에 따라 실제 의미가 달라집니다.

예:

- `material_supply`
- `inter_station_transfer`
- `battery_delivery_low_battery`
- `battery_delivery_discharged`

따라서 분석 시에는 `task_type`만 보지 말고 `priority_key`와 `payload.transfer_kind`를 같이 봐야 합니다.

## 9. SimPy process 구조

### `machine_lifecycle`

Machine마다 하나씩 존재합니다.

역할:

- 고장 여부 확인
- output 대기 여부 확인
- 입력 부족 여부 확인
- processing cycle 시작
- cycle 완료 또는 중단 처리

### `machine_failure_monitor`

Machine마다 하나씩 존재합니다.

역할:

- stochastic failure 발생
- PM 상태일 때 failure rate 보정

### `worker_work_loop`

Worker마다 하나씩 존재합니다.

역할:

- 가능한 Task 후보 선택
- Task 실행
- interruption 처리
- `completed`, `skipped`, `interrupted` 결과 기록

`agent_work_loop`는 legacy wrapper이며 실제 구현은 `worker_work_loop`입니다.

### `worker_battery_monitor`

Worker마다 하나씩 존재합니다.

역할:

- 배터리 잔량 감시
- low-battery alert 발생
- 방전 시 `DISCHARGED` 전이

`agent_battery_monitor`는 legacy wrapper입니다.

### `snapshot_loop`

주기적으로 minute snapshot을 남깁니다.

이 snapshot은 KPI 계산과 후속 대시보드 export에 사용됩니다.

## 10. 상태 전이와 로그

상태 변경은 가능하면 직접 필드 대입 대신 setter를 통해 기록됩니다.

중심 setter:

- `_set_worker_state(...)`
- `_set_worker_motion(...)`
- `_set_worker_cargo(...)`
- `_set_machine_state(...)`
- `_set_item_state(...)`

이 경로를 통해 아래 canonical 이벤트가 기록됩니다.

- `WORKER_STATE_CHANGED`
- `WORKER_CARGO_CHANGED`
- `MACHINE_STATE_CHANGED`
- `ITEM_STATE_CHANGED`

새 Replay Studio exporter는 위 이벤트를 우선 사용합니다.

중요한 설계 방침:

- Replay Studio는 가능하면 의미를 추론하지 않습니다.
- 시뮬레이션 로그가 canonical state를 직접 제공해야 합니다.
- legacy `AGENT_*` 이벤트는 과거 산출물과 도구 호환용으로만 남아 있을 수 있습니다.

## 11. Replay Studio와의 관계

현재 방향은 "Replay Studio는 표시기이고, 의미 판단은 시뮬레이터가 한다"입니다.

즉:

- Worker sprite/badge는 `worker_state`를 그대로 매핑합니다.
- Machine sprite/status/progress는 `machine_state`를 그대로 매핑합니다.
- carry overlay는 `cargo.item_type`만 보고 표시합니다.
- queue와 item 표현은 `item_type + item_state`를 기준으로 구성합니다.

이 구조 덕분에 Scene과 오른쪽 패널이 서로 다른 의미 해석을 하는 문제를 줄일 수 있습니다.

## 12. 현재 코드에서 꼭 알아둘 점

- 제조 도메인에서 표준 용어는 `Worker`입니다.
- 다만 내부에는 아직 `agent`라는 변수명, 함수명, metric key가 일부 남아 있습니다.
- 이는 기존 artifact 호환 때문에 남겨둔 것이며, 새 canonical 모델의 기준은 `Worker`입니다.
- 새 설정 파일은 `factory.num_workers`, `worker.*`를 써야 합니다.
- 기존 `factory.num_agents`, `agent.*`는 deprecated alias입니다.

## 13. 읽는 순서

코드를 따라가며 이해하려면 아래 순서를 권장합니다.

1. `entities.py`
2. `processes.py`
3. `world.py`
4. `run.py`
5. `replay_studio/examples/export_mansim_run.py`

특히 canonical state 동기화를 보려면 아래 함수들을 먼저 보면 됩니다.

- `ManufacturingWorld._set_worker_state`
- `ManufacturingWorld._set_worker_motion`
- `ManufacturingWorld._set_worker_cargo`
- `ManufacturingWorld._set_machine_state`
- `ManufacturingWorld._set_item_state`
- `convert_events(...)` in `replay_studio/examples/export_mansim_run.py`

## 14. 요약

현재 시뮬레이터 코어의 기준은 다음 한 줄로 정리할 수 있습니다.

시뮬레이터가 Worker/Machine/Item의 canonical state를 직접 정의하고 로그로 남기며, Replay Studio는 그 값을 표시만 한다.
