# Simulator Core Guide

이 문서는 `manufacturing_sim/` 아래의 제조 simulator core를 설명합니다. Humanoid State/Task/Primitive 상세는 [humanoid_worker_model.md](humanoid_worker_model.md), 이동 경로계획과 traffic 상세는 [humanoid_movement_model.md](humanoid_movement_model.md)에 분리되어 있습니다.

## Core Responsibility

Simulator core가 담당하는 것:

- factory state 보관과 transition
- SimPy 기반 discrete-event time progression
- worker, machine, item entity 관리
- 현재 상태에서 실행 가능한 task 후보 생성
- 선택된 task 실행
- tile map 기반 worker 이동
- traffic reservation, conflict 관찰, event 기록
- battery, setup, breakdown, repair, inspection 처리
- event log와 KPI source 생성

Simulator core가 담당하지 않는 것:

- LLM manager orchestration
- OpenClaw request/response
- dashboard UI rendering
- run-series knowledge synthesis
- LLM Wiki/Graphify update

## Key Files

- `manufacturing_sim/simulation/scenarios/manufacturing/world.py`
  - Factory world state, task enumeration, execution, KPI aggregation.
- `manufacturing_sim/simulation/scenarios/manufacturing/humanoid_runtime.py`
  - `HumanoidSim` catalog/profile validation, TaskSpec step flattening, primitive execution bridge.
- `manufacturing_sim/simulation/scenarios/manufacturing/grid_map.py`
  - Tile map, pathfinding, worker tile occupancy.
- `manufacturing_sim/simulation/scenarios/manufacturing/traffic.py`
  - Path overlap, tile conflict, edge conflict, near miss detection.
- `manufacturing_sim/simulation/scenarios/manufacturing/entities.py`
  - `Worker`, `Machine`, `Task`, `Item` dataclasses and machine/item domain states.
- `manufacturing_sim/simulation/scenarios/manufacturing/processes.py`
  - SimPy process orchestration.
- `manufacturing_sim/simulation/scenarios/manufacturing/logging.py`
  - `events.jsonl` event writer.
- `manufacturing_sim/simulation/scenarios/manufacturing/run.py`
  - Scenario execution entrypoint.

## HumanoidSim State Boundary

ManSim은 Humanoid의 기본 state 의미를 소유하지 않습니다. Humanoid의 Availability, Mobility, Power, Manipulation 축과 primitive별 state effect는 `HumanoidSim`에서 정의합니다.

ManSim이 담당하는 것은 scenario fact입니다.

- task가 선택되었는지
- task 또는 child task가 시작/종료되었는지
- primitive가 시작/종료되었는지
- cargo를 집거나 내려놓았는지
- battery가 방전되었거나 충전 중인지
- resource가 사라져 blocked가 되었는지
- traffic wait 또는 conflict가 발생했는지

이 사실들은 `HumanoidTaskRuntime.transition_state()`를 통해 HumanoidSim transition event로 전달됩니다. ManSim은 `availability`, `mobility`, `power`, `manipulation` 값을 직접 계산하거나 대입하지 않고, HumanoidSim이 반환한 `HumanoidStateSnapshot`을 event, minute snapshot, KPI, Replay Studio에 기록합니다.

정상적으로 실행 중인 모든 primitive는 `availability=EXECUTING`입니다. Mobility와 Manipulation은 [HumanoidSim Primitive Reference](../../HumanoidSim/docs/primitives_reference.md)의 primitive별 state relation을 따릅니다. 단, incident recovery protocol 안에서 실행되는 task/primitive는 정상 작업이 아니라 blocked 상태의 복구 절차이므로 availability를 `BLOCKED`로 유지하고, 현재 step만 Task 또는 Primitive context에 `CODE (RECOVERY)`로 기록합니다.

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
  -> CompletedProducts accepted product
  -> ScrapDisposal failed product
```

주요 queue/buffer:

- warehouse material shelf - Warehouse 내부 공유 material slot pool.
- `material_queues` - station별 raw material 대기.
- `intermediate_queues` - station 사이 item 대기.
- `output_buffers` - stage 처리 후 다음 이동 전 대기.
- inspection input queue - inspection 대상 item 대기.
- inspection output queue - inspection 통과 후 completed product transfer 대기.
- inspection scrap queue - inspection fail 후 scrap disposal transfer 대기.
- completed product buffer - 최종 accepted product count source.
- scrap disposal bin - 폐기 완료 count source.

`completed products`는 inspection output에 쌓인 item이 아닙니다. Inspection을 통과한 accepted product가 `CompletedProducts` zone의 `completed_product_buffer`까지 운반되어야 최종 count가 증가합니다.

## Entity Model

### Worker

Worker는 ManSim 내부 entity이지만 상태와 task 의미는 `HumanoidSim`에서 가져온 정의를 사용합니다. `Worker.humanoid_state`는 `HumanoidStateSnapshot` dictionary이며, `availability`, `mobility`, `power`, `manipulation`, `task_context`, `reason`을 담습니다.

자세한 상태 축과 task 목록은 [humanoid_worker_model.md](humanoid_worker_model.md)를 보세요.

### Machine

Machine은 ManSim domain state를 그대로 사용합니다.

- `WAIT_INPUT`
- `SETUP`
- `IDLE`
- `PROCESSING`
- `DONE_WAIT_UNLOAD`
- `BROKEN`
- `UNDER_REPAIR`
- `UNDER_PM`

Machine state는 Humanoid state와 별개입니다. Machine dashboard와 KPI는 machine state를 기준으로 집계합니다.

### Item

Item은 material, intermediate, product, battery 등으로 구분됩니다. 주요 state:

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
- `COMPLETED`
- `SCRAPPED`

Item state도 Humanoid state와 별개입니다. Worker가 item을 들면 worker의 `manipulation`은 보통 `HOLDING`이 되고, item state는 `CARRIED_BY_WORKER`가 됩니다.

## Task Runtime Boundary

Simulator는 현재 factory state에서 실행 가능한 task 후보를 만듭니다. Decision mode는 후보 중 하나를 선택합니다. 선택된 task는 `HumanoidTaskRuntime`을 통해 `HumanoidSim`의 task catalog와 worker profile validation을 거친 뒤 실행됩니다.

`task_type`과 priority key는 기존 decision layer 호환용 label입니다. 실제 실행 단위는 `task_code`입니다.

현재 ManSim에서 사용하는 task code:

- `REPLENISH_MATERIAL`
- `TRANSFER`
- `MANAGE_ROBOT_POWER`
- `SETUP_MACHINE`
- `UNLOAD_MACHINE`
- `INSPECT_PRODUCT`
- `REPAIR_MACHINE`
- `PREVENTIVE_MAINTENANCE`
- `HANDOVER_ITEM`
- `COLLECT_WASTE_OR_SCRAP`

Task별 primitive sequence, state 축 변화, ManSim domain side effect는 [humanoid_worker_model.md](humanoid_worker_model.md)에 정리되어 있습니다.

## Domain Rules

### Inspection

Inspection workbench는 한 번에 하나의 worker만 점유할 수 있습니다.

- Worker는 inspection input queue에서 product를 집습니다.
- Worker는 `inspection_table` service tile까지 이동합니다.
- Worker가 table 위치에 도착한 뒤에만 `EXECUTE_QUALITY_ACTION`이 진행됩니다.
- Pass item은 inspection output queue까지 worker가 직접 운반합니다.
- Fail item은 inspection scrap queue에 들어가며 이때 `scrap_count`가 증가합니다.
- `COLLECT_WASTE_OR_SCRAP`가 scrap batch를 `scrap_disposal_bin`까지 운반하면 `disposed_scrap_count`가 증가합니다.
- CompletedProducts transfer가 끝나야 `completed products`가 증가합니다.

### Warehouse Material Shelf

Warehouse의 material은 공유 shelf slot에 놓입니다.

- 기본 capacity는 `scenario.warehouse.material_shelf.capacity: 10`입니다.
- 시작 시 `initial_fill`만큼 채우고, `restock_policy: day_boundary`이면 매일 시작 시 빈 slot만 capacity까지 보충합니다.
- Worker는 `warehouse_material_slot_XX`의 service tile까지 이동해야 material을 집을 수 있습니다.
- shelf가 비어 있으면 material supply 후보를 만들지 않고 `MATERIAL_SHELF_EMPTY` event와 KPI로 남깁니다.

### Scrap Disposal

Inspection fail product는 즉시 `SCRAPPED`가 되지 않습니다.

- fail 판정 시 item state는 `WAITING_SCRAP_DISPOSAL`이 되고 `inspection_scrap_queue`에 들어갑니다.
- `quality.scrap_transport.max_carry_count`만큼 batch pickup할 수 있습니다. 기본값은 3입니다.
- `scrap_disposal_bin` dropoff 후 item state가 `SCRAPPED`가 되고 `disposed_scrap_count`가 증가합니다.

### Setup / Unload

Setup과 unload는 queue와 machine 사이의 실제 carry 이동을 포함합니다.

- `SETUP_MACHINE`: 필요한 input queue로 이동해 item을 집고 machine service tile로 돌아와 load/setup을 수행합니다.
- `UNLOAD_MACHINE`: machine output을 집고 station output buffer까지 운반합니다.

### Repair / Preventive Maintenance

Repair는 여러 worker가 한 machine에 합류할 수 있습니다. 동시 repair worker 수는 `scenario.machine_failure.max_repair_agents`가 제한합니다. Preventive maintenance는 idle machine에 대해 수행되며, breakdown probability를 낮추는 효과를 갖습니다.

### Battery

Battery swap은 `MANAGE_ROBOT_POWER`로 표현합니다. Battery delivery는 `TRANSFER` task code를 사용하며, payload args에 target worker와 battery rack 정보가 들어갑니다.

### Product Handover

Product transport session이 active이고 carrier가 1명일 때, 다른 available worker는 `HANDOVER_ITEM` 후보를 받을 수 있습니다. Helper가 source carrier를 따라잡을 수 있을 만큼 남은 경로가 있을 때만 후보가 생성됩니다. 합류 후 다음 tile segment부터 product 이동 multiplier가 carrier 수로 나뉩니다.

## Movement And Traffic

Worker 이동은 tile map 기반입니다. Core는 `move_agent(agent, dst)`를 통해 logical destination을 service tile 후보로 바꾸고, A* path를 따라 한 tile씩 이동합니다.

기본 traffic mode는 `strict_reservation`입니다. Worker가 다음 tile을 예약하지 못하면 이동하지 않고 `AGENT_TRAFFIC_CONFLICT`와 `TRAFFIC_WAIT` HumanoidSim incident를 기록한 뒤, HumanoidSim에 정의된 recovery protocol을 실행합니다. 장시간 path를 찾지 못해 `AGENT_TILE_BLOCKED`가 발생하면 `PATH_BLOCKED` incident와 recovery protocol로 연결됩니다. `observe_conflicts` 모드는 충돌 가능 상황을 막지 않고 관찰하기 위한 실험 모드입니다.

자세한 pathfinding, reservation, traffic conflict 정의는 [humanoid_movement_model.md](humanoid_movement_model.md)를 보세요.

## Event Logging

Core는 event-sourced replay를 위해 주요 상태 변화를 기록합니다.

Worker 관련 event details에는 `humanoid_state` snapshot 원본이 들어갑니다.

주요 event:

- `WORKER_STATE_CHANGED`
- `WORKER_CARGO_CHANGED`
- `HUMANOID_TASK_START`, `HUMANOID_TASK_END`
- `HUMANOID_STEP_START`, `HUMANOID_STEP_END`
- `AGENT_MOVE_START`, `AGENT_MOVE_TILE_START`, `AGENT_MOVE_TILE_END`, `AGENT_MOVE_END`
- `AGENT_TRAFFIC_CONFLICT`
- `PRODUCT_CARRY_STARTED`, `PRODUCT_CARRY_JOINED`, `PRODUCT_CARRY_COMPLETED`
- `ITEM_MOVED`
- `MACHINE_STATE_CHANGED`
- `MACHINE_REPAIR_*`

`HUMANOID_STEP_START`와 `HUMANOID_STEP_END`에는 `task_code`, `instance_id`, `step_id`, `primitive_call_code`, `humanoid_state`가 포함됩니다.

## Replay Export

`events.jsonl`은 `replay_studio_log.json`으로 변환됩니다. Replay Studio는 worker 위치를 임의 보정하지 않고, `AGENT_MOVE_START`에서 export된 `entity_moved.payload.path`와 `durative` window를 기준으로 이동을 보간합니다.

Worker panel은 `humanoid_state` 원본에서 네 state 축, task context, primitive, cargo item id, traffic reason을 읽습니다. Task가 종료되어 `task_context=null`이면 stale task label을 표시하지 않습니다.

Nested task가 끝나도 parent task가 계속 실행 중이면 replay export는 parent `task_window`를 유지합니다. 따라서 `REPAIR_MACHINE -> INSPECT_MACHINE -> EXECUTE_MAINTENANCE_ACTION`처럼 child task가 먼저 끝나는 flow에서도 worker task 게이지는 parent task 기준으로 계속 진행됩니다.

Domain-internal primitive 변경은 `WORKER_STATE_CHANGED`에도 즉시 기록됩니다. 예를 들어 `INSPECT_PRODUCT`가 table에 도착해 실제 검사 시간에 들어가면 `NAVIGATE_TO`가 아니라 `EXECUTE_QUALITY_ACTION`으로 표시됩니다. Product 공동 운반 helper의 `HANDOVER_ITEM`은 primary worker가 inspection table에 도착하면 종료되고, 이후 inspection은 primary worker의 `INSPECT_PRODUCT` context로만 표시됩니다.

## KPI Source

`kpi.json`과 dashboard KPI는 simulator core artifact에서 집계합니다.

Humanoid/worker KPI:

- `humanoid_state_time_by_worker`
- `humanoid_state_time_by_axis`
- `humanoid_state_ratio_by_worker`
- `humanoid_execution_ratio_by_worker`
- `humanoid_unavailable_ratio_by_worker`
- `humanoid_task_minutes`
- `humanoid_primitive_minutes`
- `humanoid_task_taxonomy`

Traffic KPI:

- `traffic_conflicts_by_type`
- `traffic_conflicts_by_worker_pair`
- `collision_count`
- `near_miss_count`
- `edge_conflict_count`
- `path_overlap_count`

Transport/handover KPI:

- `handover_item_count`
- `shared_product_carry_completed_count`
- `product_carry_time_min`
- `shared_product_carry_time_min`
- `item_transport_time_by_type`

Production and flow KPI:

- `total_products`
- `downstream_closure_ratio`
- `completed_product_lead_time_avg_min`
- buffer wait time
- machine utilization and breakdown time
- inspection throughput

가장 중요한 해석 기준은 `total_products`입니다.

## Runtime Boundary

`main.py`와 `runtime/entrypoint.py`는 scenario를 실행하고 artifact를 내보내는 상위 layer입니다. Simulator core가 생성한 state/log를 바탕으로 아래 산출물이 만들어집니다.

- `kpi.json`
- `daily_summary.json`
- `events.jsonl`
- `minute_snapshots.json`
- `replay_studio_log.json`
- `replay_studio_layout.json`
- `results_dashboard.html`

LLM Wiki, Graphify graph, manager replay는 core 바깥의 orchestration/dashboard layer에서 생성됩니다.

## Debugging Order

Factory behavior가 이상하면 아래 순서로 확인합니다.

1. `events.jsonl`
2. `minute_snapshots.json`
3. `kpi.json`
4. `daily_summary.json`
5. `replay_studio_log.json`

Replay Studio에서 이상해 보이면 먼저 `events.jsonl`의 core event가 같은 내용을 말하는지 확인합니다. Core event가 정상이고 Replay만 다르면 exporter/reducer/UI 문제일 가능성이 큽니다.


### HumanoidSim transition API

ManSim does not define the meaning of humanoid state axes itself. It sends task, primitive, cargo, power, waiting, blocked, and disabled events to `HumanoidSim.transition_humanoid_state()`, then records the returned `HumanoidStateSnapshot` in events, replay, and KPI artifacts.

- Every running primitive is represented with `availability=EXECUTING` according to HumanoidSim.
- Mobility and Manipulation are updated from the primitive profile `allowed` and `effects` definitions.
- ManSim still decides scenario facts such as battery depletion, cargo changes, missing resources, and traffic waits.
- Invalid transitions use strict fail so incorrect runtime/state definitions are caught during simulation.

