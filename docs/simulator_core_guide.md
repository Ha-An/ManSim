# Simulator Core Guide

이 문서는 `manufacturing_sim/` 아래의 제조 시뮬레이터 코어를 설명합니다. LLM manager, dashboard, OpenClaw runtime은 코어 바깥 layer입니다.

## Core Responsibility

Simulator core가 담당하는 것:

- factory state 보관
- SimPy 기반 discrete-event time progression
- worker, machine, item state transition
- feasible task 후보 생성
- 선택된 task 실행
- battery, setup, breakdown, repair, inspection 처리
- event log와 KPI source 생성

Simulator core가 담당하지 않는 것:

- LLM manager orchestration
- OpenClaw request/response
- dashboard UI rendering
- run-series knowledge synthesis
- LLM Wiki/Graphify update

## 주요 파일

- `manufacturing_sim/simulation/scenarios/manufacturing/world.py`
  - Factory world state, task enumeration, task execution, KPI aggregation.
- `manufacturing_sim/simulation/scenarios/manufacturing/processes.py`
  - SimPy process loop.
- `manufacturing_sim/simulation/scenarios/manufacturing/entities.py`
  - `Machine`, `Worker`, `Task`, `Item`과 state enum.
- `manufacturing_sim/simulation/scenarios/manufacturing/logging.py`
  - `events.jsonl` event logging.
- `manufacturing_sim/simulation/scenarios/manufacturing/run.py`
  - Scenario execution entry와 artifact export.

## Time Model

ManSim은 SimPy 기반 discrete-event simulation입니다.

- 기본 시간 단위: minute.
- 하루 길이: `scenario.horizon.minutes_per_day`.
- 총 일수: `scenario.horizon.num_days`.

현재 simulation time `t`의 day 계산:

```text
day = floor(t / minutes_per_day) + 1
```

예를 들어 하루가 240분이면 `0 <= t < 240`은 Day 1, `240 <= t < 480`은 Day 2입니다.

## Factory Flow

기본 제조 흐름:

```text
Warehouse material
  -> Station 1 processing
  -> Station 2 processing
  -> Inspection
  -> Warehouse completed product or scrap
```

주요 buffer/queue:

- `material_queues` - station별 raw material 대기.
- `intermediate_queues` - station 사이의 item 대기.
- `output_buffers` - stage 처리 후 다음 이동 전 대기.
- `inspection_queue` - inspection 대상 item 대기.
- `inspection_output` - inspection 통과 후 warehouse transfer 대기.
- completed warehouse buffer - 최종 accepted product count source.

`completed products`는 inspection output에 쌓인 item이 아니라 warehouse까지 도착한 accepted product입니다.

## Entity Model

### Worker

Worker는 현재 위치, battery, task 상태, 이동/작업 여부를 가집니다. Decision layer가 task를 선택하면 simulator가 이동과 작업 시간을 반영합니다.

대표 상태:

- idle
- moving
- processing
- recharging
- repairing
- blocked

### Machine

Machine은 processing station의 상태를 나타냅니다.

대표 상태:

- idle
- processing
- broken
- repair_in_progress
- setup_required

### Item

Item은 factory flow를 따라 이동합니다.

대표 상태:

- raw material
- in process
- waiting transfer
- waiting inspection
- inspected accepted
- inspected rejected
- completed

## Task Model

Simulator는 현재 상태에서 실행 가능한 task 후보를 생성합니다. Decision mode는 이 후보 중 하나를 선택합니다.

대표 task family:

- `load_material`
- `process_stage_1`
- `process_stage_2`
- `transfer_to_next_stage`
- `inspect`
- `transfer_inspection_output`
- `recharge`
- `repair_machine`
- `setup_machine`

Task 후보에는 보통 target entity, expected duration, route, priority 관련 context가 포함됩니다.

## Inspection Constraint

Inspection workbench는 한 번에 하나의 worker만 점유할 수 있어야 합니다. Inspection queue 위의 item이 많더라도 동시에 여러 worker가 같은 inspection 작업을 수행하면 안 됩니다.

정상 동작:

- 한 worker가 inspection task를 시작하면 workbench가 busy가 됩니다.
- 다른 worker는 inspection이 끝날 때까지 같은 workbench inspection을 시작하지 않습니다.
- Inspection 완료 후 accepted item은 inspection output으로 이동하고, 이후 warehouse transfer가 완료되어야 completed count가 증가합니다.

## Repair Model

Machine repair는 협동 수리를 지원합니다.

- `scenario.machine_failure.max_repair_agents`가 동시 repair 참여 worker 수를 제한합니다.
- 한 worker가 수리를 시작하고 helper가 합류할 수 있습니다.
- Repair progress는 active repair worker 수에 따라 빨라집니다.
- 모든 repair worker가 이탈하면 남은 repair work는 보존됩니다.

Replay event에는 helper join/leave, team size, remaining work가 기록됩니다.

## Battery Model

Worker는 이동과 작업 중 battery를 소모합니다. Battery가 낮으면 recharge task가 feasible candidate로 올라오며, safety guard가 low-battery worker를 보호합니다.

Manager가 battery prevention target을 선택하면 compiler는 recharge 관련 floor와 role multiplier를 강화할 수 있습니다.

## Event Logging

Core는 event-sourced replay를 위해 주요 상태 변화를 기록합니다.

대표 event:

- worker movement start/end
- task start/end
- item transfer
- processing complete
- inspection start/end
- machine breakdown/repaired
- repair helper join/leave
- battery recharge
- incident/blocker

Export된 event는 `replay_studio_log.json`으로 변환되어 Replay Studio가 소비합니다.

## KPI Source

`kpi.json`과 dashboard KPI는 simulator core artifact에서 집계됩니다.

핵심 KPI:

- `total_products`
- `downstream_closure_ratio`
- `completed_product_lead_time_avg_min`
- buffer wait time
- physical incident total
- coordination incident total
- machine breakdown count
- inspection backlog end

해석상 가장 중요한 지표는 `total_products`입니다.

## Runtime Boundary

`main.py`와 `runtime/entrypoint.py`는 scenario를 실행하고 artifact를 내보내는 상위 layer입니다. Simulator core가 생성한 state/log를 바탕으로 아래 산출물이 만들어집니다.

- `kpi.json`
- `daily_summary.json`
- `events.jsonl`
- `minute_snapshots.json`
- `replay_studio_log.json`
- `replay_studio_layout.json`
- `results_dashboard.html`

LLM Wiki, Graphify graph, manager replay는 core 밖의 orchestration/dashboard layer에서 생성됩니다.

## 디버깅 순서

Factory behavior가 이상하면 먼저 아래를 봅니다.

1. `daily_summary.json`
2. `kpi.json`
3. `events.jsonl`
4. `minute_snapshots.json`
5. `replay_studio_log.json`
6. factory Replay Studio

Manager 판단이 이상하면 core보다 `manager_replay.json`, `shift_policy_history.json`, `day_review_memory.json`, OpenClaw workspace trace를 먼저 봅니다.
