# Simulator Core Guide

이 문서는 `manufacturing_sim/` 아래의 제조 simulator core를 설명합니다. Humanoid State/Task/Primitive 상세는 [humanoid_worker_model.md](humanoid_worker_model.md), 이동 경로계획과 traffic 상세는 [humanoid_movement_model.md](humanoid_movement_model.md), Replay/Dashboard 상세는 [replay_dashboards.md](replay_dashboards.md)에 분리되어 있습니다.

## Core Responsibility

Simulator core가 담당하는 것:

- factory state 보관과 transition
- SimPy 기반 discrete-event time progression
- worker, machine, item entity 관리
- 현재 상태에서 실행 가능한 task 후보 생성
- 선택된 task 실행
- tile map 기반 worker 이동
- traffic reservation, conflict 관찰, event 기록
- battery, setup, breakdown, repair, PM, inspection 처리
- event log와 KPI source 생성

Simulator core가 담당하지 않는 것:

- HumanoidSim의 state/task/incident taxonomy 정의
- dashboard UI rendering
- LLM manager orchestration의 prompt/transport 구현
- run-series knowledge synthesis
- LLM Wiki/Graphify update

## Key Files

- `manufacturing_sim/simulation/scenarios/manufacturing/world.py`: Factory world state, task enumeration, execution, KPI aggregation.
- `manufacturing_sim/simulation/scenarios/manufacturing/humanoid_runtime.py`: HumanoidSim catalog/profile validation, nested task flattening, primitive execution bridge.
- `manufacturing_sim/simulation/scenarios/manufacturing/grid_map.py`: Tile map, pathfinding, object footprints, worker tile occupancy.
- `manufacturing_sim/simulation/scenarios/manufacturing/traffic.py`: Path overlap, tile conflict, edge conflict, near miss detection.
- `manufacturing_sim/simulation/scenarios/manufacturing/entities.py`: `Worker`, `Machine`, `Task`, `Item` dataclasses and machine/item domain states.
- `manufacturing_sim/simulation/scenarios/manufacturing/processes.py`: SimPy process orchestration.
- `manufacturing_sim/simulation/scenarios/manufacturing/logging.py`: `events.jsonl` event writer.
- `manufacturing_sim/simulation/scenarios/manufacturing/run.py`: Scenario execution entrypoint and artifact export.

## HumanoidSim Boundary

ManSim은 Humanoid 자체의 state 의미를 소유하지 않습니다. Availability, Mobility, Power, Manipulation 축과 primitive별 state effect는 `HumanoidSim`에서 정의합니다.

ManSim이 판단하는 것은 scenario fact입니다.

- task가 선택되었는가
- task 또는 child task가 시작/종료되었는가
- primitive가 시작/종료되었는가
- cargo를 집거나 내려놓았는가
- battery가 방전되었거나 충전 중인가
- resource가 사라져 blocked가 되었는가
- traffic wait 또는 conflict가 발생했는가

이 사실들은 `HumanoidTaskRuntime.transition_state()`를 통해 HumanoidSim transition event로 전달됩니다. ManSim은 `availability`, `mobility`, `power`, `manipulation` 값을 직접 계산하거나 덮어쓰지 않고, HumanoidSim이 반환한 `HumanoidStateSnapshot`을 event, minute snapshot, KPI, Replay Studio에 기록합니다.

정상적으로 실행 중인 primitive는 `availability=EXECUTING`입니다. 단, incident recovery protocol 안에서 실행되는 task/primitive는 복구 절차임을 보존하기 위해 availability를 `BLOCKED`로 유지하고, 현재 step은 `CODE (RECOVERY)` 형태로 task 또는 primitive context에 기록합니다.

## Time Model

ManSim은 SimPy 기반 discrete-event simulation입니다.

- 기본 시간 단위: minute
- 하루 길이: `scenario.horizon.minutes_per_day`
- 총 일수: `scenario.horizon.num_days`

```text
day = floor(t / minutes_per_day) + 1
```

## Factory Flow

기본 제조 흐름:

```text
Warehouse material
  -> Station 1 processing
  -> Station 2 processing
  -> Inspection
  -> CompletedProducts accepted product
  -> ScrapDisposal failed product
```

주요 queue/buffer:

- warehouse material shelf: Warehouse 내부 공유 material slot pool
- `material_queues`: station별 raw material 대기
- `intermediate_queues`: station 사이 intermediate item 대기
- `output_buffers`: stage 처리 후 다음 이동 전 대기
- inspection input queue: inspection 대상 item 대기
- inspection output queue: inspection 통과 후 completed product transfer 대기
- inspection scrap queue: inspection fail 후 scrap disposal transfer 대기
- completed product buffer: 최종 accepted product count source
- scrap disposal bin: 폐기 완료 count source

`completed products`는 inspection output queue에 놓인 시점이 아니라, accepted product가 `completed_product_buffer`까지 운반된 시점에 증가합니다.

## Entity Model

### Worker

Worker는 ManSim 내부 entity이지만 상태와 task 의미는 `HumanoidSim` 정의를 사용합니다. `Worker.humanoid_state`는 `HumanoidStateSnapshot` dictionary이며, `availability`, `mobility`, `power`, `manipulation`, `task_context`, `reason`을 담습니다.

### Machine

Machine은 ManSim domain state를 사용합니다.

- `WAIT_INPUT`
- `SETUP`
- `IDLE`
- `PROCESSING`
- `DONE_WAIT_UNLOAD`
- `BROKEN`
- `UNDER_REPAIR`
- `UNDER_PM`

Machine state는 Humanoid state와 별개입니다.

### Item

Item은 material, intermediate, product, battery 등으로 구분합니다. 주요 state는 다음과 같습니다.

- `CREATED`
- `IN_STORAGE`
- `IN_QUEUE`
- `CARRIED_BY_WORKER`
- `LOADED_ON_MACHINE`
- `PROCESSING`
- `WAITING_MACHINE_UNLOAD`
- `WAITING_INSPECTION`
- `INSPECTING`
- `WAITING_INSPECTION_OUTPUT`
- `WAITING_SCRAP_DISPOSAL`
- `DROPPED`
- `COMPLETED`
- `SCRAPPED`

Item drop incident가 발생하면 item은 현재 tile에 `DROPPED` 상태로 남고, HumanoidSim recovery protocol을 통해 다시 localize/identify/transfer될 수 있습니다.

## Task Runtime Boundary

Simulator는 현재 factory state에서 실행 가능한 task 후보를 만듭니다. Decision mode는 후보 중 하나를 선택합니다. 선택된 task는 `HumanoidTaskRuntime`을 통해 HumanoidSim task catalog와 worker profile validation을 거친 뒤 실행됩니다.

`task_type`과 `priority_key`는 기존 decision layer 호환용 label입니다. 실제 실행 단위는 `task_code`입니다.

현재 ManSim에서 사용하는 task code:

- `REPLENISH_MATERIAL`
- `TRANSFER`
- `MANAGE_ROBOT_POWER`
- `SETUP_MACHINE`
- `LOAD_MACHINE`
- `UNLOAD_MACHINE`
- `INSPECT_PRODUCT`
- `REPAIR_MACHINE`
- `PREVENTIVE_MAINTENANCE`
- `INSPECT_MACHINE`
- `HANDOVER_ITEM`
- `COLLECT_WASTE_OR_SCRAP`
- `UPDATE_INVENTORY_RECORD`

## Task Candidate Generation Conditions

Task 정의와 hierarchy는 HumanoidSim이 소유하지만, ManSim에서 **언제 task opportunity가 생기는지**는 factory world state가 결정합니다. 핵심 구현 위치는 `manufacturing_sim/simulation/scenarios/manufacturing/world.py`의 `_candidate_tasks(agent)`이며, rolling horizon mode에서는 같은 후보를 window pool에 모았다가 dispatch합니다.

아래 표는 현재 ManSim world가 생성하는 주요 HumanoidSim task 후보와 발생 조건입니다.

| Task code | World trigger condition |
| --- | --- |
| `REPLENISH_MATERIAL` | Station material queue가 target보다 적고, 해당 station의 material supply owner가 없으며, warehouse shelf에 pickup 가능한 material이 있을 때 생성됩니다. 후보는 특정 `MAT-WH-*`를 미리 고정하지 않고 station, source, destination, target level만 담는 generic material request입니다. 실제 shelf slot과 material item id는 실행 중 `PRIMITIVE_IDENTIFY_ITEM` 단계에서 확정됩니다. |
| `TRANSFER` | Station output buffer에 다음 위치로 옮길 item이 있을 때 생성됩니다. Station 1/2 output은 다음 queue로, inspection output은 `completed_product_buffer`로 이동합니다. Battery delivery도 실행 task code는 `TRANSFER`이며 payload의 `transfer_kind=battery_delivery`로 구분합니다. |
| `MANAGE_ROBOT_POWER` | Worker의 battery remaining이 configured threshold 이하이고, 해당 worker가 battery service owner가 될 수 있을 때 self battery swap 후보로 생성됩니다. Rolling horizon mode에서도 일반 후보와 같은 pool/dispatch 흐름을 탑니다. |
| `SETUP_MACHINE` | Machine이 `WAIT_INPUT`이고 broken/processing 상태가 아니며 setup owner가 없고, 필요한 material 또는 intermediate가 machine에 없지만 source queue에는 있을 때 생성됩니다. 후보에는 실제로 집어야 할 queue item id가 포함됩니다. |
| `LOAD_MACHINE` | 독립 world 후보로 직접 생성되기보다는 `SETUP_MACHINE` composite task의 child task로 실행됩니다. Queue item을 machine에 적재하는 단계입니다. |
| `UNLOAD_MACHINE` | Machine에 `output_intermediate`가 존재하고 unload owner가 없을 때 생성됩니다. Worker는 machine output을 station output buffer로 옮깁니다. |
| `INSPECT_PRODUCT` | Inspection input queue에 product가 있고 inspection owner가 없을 때 생성됩니다. 후보에는 inspection 대상 product id가 포함됩니다. |
| `REPAIR_MACHINE` | Machine이 broken이고, 해당 worker가 repair team에 아직 없으며, repair team capacity가 남아 있을 때 생성됩니다. Dedicated roles mode에서는 collaboration 없이 단독 repair로 제한됩니다. |
| `PREVENTIVE_MAINTENANCE` | Machine의 마지막 PM 이후 시간이 `pm_interval_target_min` 이상이고, machine이 broken/processing 상태가 아니며, output이 비어 있고 pm owner가 없을 때 생성됩니다. |
| `HANDOVER_ITEM` | Product 공동 운반 session이 active이고 carrier가 max보다 적으며, 후보 worker가 아직 carrier가 아니고 source carrier와 남은 path가 유효할 때 생성됩니다. Dedicated roles mode에서는 협업을 배제하기 위해 pool에 넣지 않습니다. |
| `COLLECT_WASTE_OR_SCRAP` | Inspection scrap queue에 scrap item이 있고 scrap disposal owner가 없을 때 생성됩니다. Worker는 `quality.scrap_transport.max_carry_count` 이하의 batch를 `scrap_disposal_bin`으로 운반합니다. |

World는 같은 concrete item, material shelf slot, material supply station, machine resource가 동시에 여러 unresolved opportunity에 중복으로 잡히지 않도록 item/resource signature를 사용합니다. Rolling horizon mode에서는 이 signature가 `opportunity_id`와 exclusive resource key로 저장되어, 이미 pool 또는 dispatch queue에 있는 같은 자원을 다시 배정하지 않습니다.

## Domain Rules

### Inspection

Inspection workbench는 한 번에 하나의 worker만 점유할 수 있습니다.

- Worker는 inspection input queue에서 product를 집습니다.
- Worker는 `inspection_table` service tile까지 이동합니다.
- Worker가 table 위치에 도착한 뒤에만 `EXECUTE_QUALITY_ACTION`을 진행합니다.
- Pass item은 inspection output queue로 이동합니다.
- Fail item은 inspection scrap queue로 이동하며 `scrap_count`가 증가합니다.
- `COLLECT_WASTE_OR_SCRAP`가 scrap batch를 `scrap_disposal_bin`까지 운반하면 `disposed_scrap_count`가 증가합니다.
- Accepted product가 `completed_product_buffer`에 도착하면 `total_products`가 증가합니다.

### Warehouse Material Shelf

Warehouse material은 공유 shelf slot에 놓입니다.

- capacity: `warehouse.material_shelf.capacity`
- 초기 채움: `warehouse.material_shelf.initial_fill`
- restock: `warehouse.material_shelf.restock_policy: day_boundary`
- Worker는 material slot service tile까지 이동해야 pickup할 수 있습니다.
- 같은 material item, 같은 shelf slot, 또는 같은 station material replenishment를 대상으로 하는 unresolved task opportunity는 rolling pool에 중복으로 들어갈 수 없습니다.
- `REPLENISH_MATERIAL` 후보는 generic request로 생성됩니다. Rolling pool에서는 `station2 / any material from Warehouse`처럼 보이며, worker가 실행 중 warehouse shelf를 스캔해 concrete material id와 slot을 선택합니다.

### Setup / Load / Unload

Setup과 unload는 queue와 machine 사이의 실제 carry 이동을 포함합니다.

- `SETUP_MACHINE`: input queue에서 item을 집고 machine service tile로 이동한 뒤 load/setup을 수행합니다.
- `LOAD_MACHINE`: machine에 item을 적재합니다.
- `UNLOAD_MACHINE`: machine output을 집고 station output buffer로 운반합니다.

### Repair / Preventive Maintenance

Repair에는 여러 worker가 같은 machine에 합류할 수 있습니다. 동시 repair worker 수는 `machine_failure.max_repair_agents`가 제한합니다. `PREVENTIVE_MAINTENANCE`는 idle machine을 대상으로 수행하며, breakdown probability를 낮추는 효과를 가집니다.

### Battery

Battery swap은 `MANAGE_ROBOT_POWER`로 표현합니다. Rolling horizon mode에서는 battery task도 다른 task와 동일하게 pool에 들어가며 window boundary에서 dispatch됩니다.

### Product Handover

Product transport session이 active이고 carrier가 1명인 경우, 다른 available worker가 `HANDOVER_ITEM` 후보를 받을 수 있습니다. Helper가 합류하면 다음 tile segment부터 product 이동 multiplier가 carrier 수로 나뉩니다.

## Movement And Traffic

Worker 이동은 tile map 기반입니다. `move_agent(agent, dst)`는 logical destination을 service tile 후보로 바꾸고 A* path를 따라 한 tile씩 이동합니다.

기본 traffic mode는 `strict_reservation`입니다. Worker가 다음 tile을 예약하지 못하면 이동하지 않고 `AGENT_TRAFFIC_CONFLICT`와 `TRAFFIC_WAIT` HumanoidSim incident를 기록한 뒤 recovery protocol을 실행합니다. `observe_conflicts` 모드는 충돌 가능 상황을 막지 않고 event/KPI/Replay overlay로 관찰하기 위한 실험 모드입니다.

## Rolling Horizon aging priority

`rolling_horizon_aging_priority`는 일반 생산 task 후보를 즉시 dispatch하지 않고 rolling window 동안 pool에 모은 뒤 dispatch합니다.

- 설정 파일: `configs/decision/rolling_horizon_aging_priority.yaml`
- window 기본값: `rolling_horizon.window_min: 5.0`
- priority 기준: `rolling_horizon.task_code_priority_order`
- priority 단위: ManSim task family가 아니라 HumanoidSim `task_code`
- dispatch policy: `aging_priority`

정렬식:

```text
effective_rank = base_rank - waited_window_count * rank_boost_per_window
```

낮은 rank가 먼저 dispatch됩니다. `PREVENTIVE_MAINTENANCE`처럼 base rank가 낮은 task도 오래 기다리면 effective rank가 개선되어 영구 starvation을 피합니다.

Window boundary에서는 pool의 feasible task를 가능한 한 모두 worker dispatch queue에 배정합니다. 한 worker에게 여러 task가 queue될 수 있으며, worker는 queue의 앞에서부터 FIFO로 실행합니다. 새 window가 시작되면 아직 실행을 시작하지 않은 queued task는 pool로 돌아가고, 새로 수집된 task와 함께 다시 ranking됩니다. 실행 중인 task는 중단하지 않습니다.

Rolling task는 처음 pool에 들어올 때 stable task id를 받습니다. 예를 들어 `REPLENISH_MATERIAL`은 `MAT-000001`, `TRANSFER`는 `TR-000002`, `REPAIR_MACHINE`은 `RM-000003` 같은 형식입니다. 이 id는 requeue/re-dispatch 이후에도 유지되며 Replay panel의 `Task` 값에도 함께 표시됩니다.

## Event Logging

주요 event:

- `WORKER_STATE_CHANGED`
- `WORKER_CARGO_CHANGED`
- `HUMANOID_TASK_START`, `HUMANOID_TASK_END`
- `HUMANOID_STEP_START`, `HUMANOID_STEP_END`
- `AGENT_MOVE_START`, `AGENT_MOVE_TILE_START`, `AGENT_MOVE_TILE_END`, `AGENT_MOVE_END`
- `AGENT_TRAFFIC_CONFLICT`
- `HUMANOID_INCIDENT`
- `ROLLING_HORIZON_WINDOW_START`
- `ROLLING_HORIZON_CANDIDATE_COLLECTED`
- `ROLLING_HORIZON_DISPATCH`
- `ROLLING_HORIZON_TASK_REQUEUED`
- `ROLLING_HORIZON_TASK_SKIPPED`
- `ITEM_STATE_CHANGED`, `ITEM_MOVED`
- `MACHINE_STATE_CHANGED`
- `MACHINE_REPAIR_*`

Worker 관련 event details에는 `humanoid_state` snapshot 원본이 포함됩니다.

## KPI Source

Humanoid/worker KPI:

- `humanoid_state_time_by_worker`
- `humanoid_state_time_by_axis`
- `humanoid_state_ratio_by_worker`
- `humanoid_execution_ratio_by_worker`
- `humanoid_unavailable_ratio_by_worker`
- `humanoid_task_minutes`
- `humanoid_primitive_minutes`
- `humanoid_task_taxonomy`

Rolling horizon KPI:

- `rolling_horizon.window_count`
- `rolling_horizon.candidate_collected_count`
- `rolling_horizon.dispatched_task_count`
- `rolling_horizon.requeued_task_count`
- `rolling_horizon.stale_skipped_task_count`
- `rolling_horizon.pending_candidate_count`
- `rolling_horizon.max_worker_queue_length`
- `rolling_horizon.max_queue_length_by_worker`
- `rolling_horizon.task_code_priority_order`
- `rolling_horizon.rank_boost_per_window`

Traffic, transport, production, shelf/scrap KPI는 `kpi.json`에 함께 기록됩니다.

## Debugging Order

Factory behavior가 이상하면 아래 순서로 확인합니다.

1. `events.jsonl`
2. `minute_snapshots.json`
3. `kpi.json`
4. `daily_summary.json`
5. `replay_studio_log.json`

Replay Studio에서 이상해 보이면 먼저 `events.jsonl`의 core event가 같은 내용을 말하는지 확인합니다. Core event가 정상이고 Replay만 다르면 exporter/reducer/UI 문제일 가능성이 높습니다.
