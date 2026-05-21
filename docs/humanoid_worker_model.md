# Humanoid Worker Model

이 문서는 ManSim에서 worker를 `HumanoidSim` 기반 휴머노이드 로봇으로 다루는 방식을 설명합니다. ManSim은 휴머노이드의 task, primitive, state, incident 정의를 직접 소유하지 않고, `HumanoidSim`에서 정의된 모델을 import해서 factory scenario 안에서 실행하고 관찰합니다.

관련 문서:

- [simulator_core_guide.md](simulator_core_guide.md): simulation core와 artifact 흐름
- [humanoid_movement_model.md](humanoid_movement_model.md): tile pathfinding, reservation, traffic
- [../README.md](../README.md): 실행 방법과 v0.4.3 요약
- `C:\Github\HumanoidSim\docs\tasks_reference.md`: task catalog reference
- `C:\Github\HumanoidSim\docs\primitives_reference.md`: primitive reference
- `C:\Github\HumanoidSim\docs\state_reference.md`: state transition reference
- `C:\Github\HumanoidSim\docs\incident_reference.md`: incident taxonomy reference

## Responsibility Boundary

| 영역 | 소유 주체 | 설명 |
| --- | --- | --- |
| Task taxonomy | HumanoidSim | `PRIMITIVE_SKILL`, `ATOMIC_TASK`, `COMPOSITE_TASK`와 82개 task catalog |
| Primitive definition | HumanoidSim | primitive code, 설명, state relation, transition effect |
| State model | HumanoidSim | Availability, Mobility, Power, Manipulation 네 축과 transition graph |
| Incident taxonomy | HumanoidSim | 범용 incident code, category, default availability, recovery protocol |
| Factory object | ManSim | station, machine, queue, shelf, inspection table, scrap zone |
| Runtime side effect | ManSim | item 이동, machine 상태 변화, inspection 결과, repair progress |
| Metrics/replay | ManSim | event, KPI, Gantt, 2D/3D Replay Studio artifact |

즉 `REPLENISH_MATERIAL`이 어떤 child task와 primitive로 구성되는지는 HumanoidSim catalog가 결정합니다. 반면 warehouse shelf의 어떤 slot에서 material을 꺼내고 station material queue에 넣는지는 ManSim scenario runtime이 결정합니다.

## State Model

Worker의 상태는 `HumanoidStateSnapshot` 하나로 기록합니다. 예전의 단일 worker state enum이나 Replay 전용 bucket state는 worker의 의미 상태로 사용하지 않습니다.

### Availability State

| State | 의미 | ManSim에서의 예 |
| --- | --- | --- |
| `AVAILABLE` | 새 task 수락 가능 | active task가 없고 task context가 비어 있음 |
| `ASSIGNED` | task를 받았지만 아직 본격 실행 전 | scheduler가 task를 선택했고 실행 시작 직전 |
| `EXECUTING` | task 또는 primitive 실행 중 | 이동, 집기, 검사, 수리, 기록 primitive 실행 |
| `WAITING` | 예상 가능한 조건 대기 | traffic reservation 대기, operator 준비 대기 |
| `BLOCKED` | 현재 task를 그대로 속행 불가 | 목표 slot이 비었거나 자원 선점 실패, grip 실패, item drop |
| `OFFLINE` | 운용 제외 | 현재 기본 scenario에서는 거의 사용하지 않음 |
| `DISABLED` | 로봇 자체가 작업 불가 | 방전, 심각한 hardware/power incident |

`WAITING`은 같은 task를 계속 이어갈 수 있다는 전제가 남아 있는 짧은 대기입니다. `BLOCKED`는 현재 task의 전제가 깨져 재계획, 재할당, 복구 task가 필요한 상태입니다.

### Mobility State

| State | 의미 | ManSim에서의 예 |
| --- | --- | --- |
| `STATIONARY` | 멈춰 있음 | 이동 primitive가 끝났거나 작업 위치에 정지 |
| `NAVIGATING` | 목적지로 이동 중 | `NAVIGATE_TO`, `move_agent()` 실행 중 |
| `DOCKING` | charger, workbench, equipment에 정렬 중 | schema에는 있으나 현재 기본 flow에서는 별도 docking primitive로 세분화하지 않음 |

### Power State

| State | 의미 | ManSim에서의 예 |
| --- | --- | --- |
| `POWER_NORMAL` | 작업 가능한 정상 전원 | 기본 상태 |
| `POWER_LOW` | 낮은 전원 | 향후 policy threshold에서 사용 가능 |
| `POWER_CRITICAL` | 위험 수준 전원 | 향후 policy threshold에서 사용 가능 |
| `DEPLETED` | 방전 | worker가 `DISABLED`로 전환 |
| `CHARGING` | 충전 중 | `MANAGE_ROBOT_POWER` 또는 충전 처리 |

### Manipulation State

| State | 의미 | ManSim에서의 예 |
| --- | --- | --- |
| `FREE` | 손이나 gripper가 비어 있음 | cargo 없음 |
| `REACHING` | 대상에 접근 중 | `REACH_TO` |
| `HOLDING` | item/tool을 들고 있음 | material, intermediate, product, scrap cargo 보유 |
| `PLACING` | 내려놓는 중 | queue, machine, inspection table, disposal bin에 배치 |

Cargo 변화는 manipulation state와 함께 기록됩니다. item을 집으면 `HOLDING`, 내려놓으면 `FREE`로 돌아갑니다.

## Task, Child Task, Primitive

Task는 목표 작업이고, primitive는 task를 이루는 실행 단계입니다. State는 작업 이름이 아니라 로봇의 현재 운용 상태입니다.

`COMPOSITE_TASK`는 하위 task를 직접 포함하는 workflow입니다. 예를 들어 `REPLENISH_MATERIAL`은 parent task이고, 내부에 child task `TRANSFER`를 포함합니다. ManSim은 parent task, active child task, primitive를 모두 event와 Replay panel에 남깁니다.

### 현재 ManSim에서 사용하는 Task

| Task Code | Level | 주요 역할 | 주요 child task |
| --- | --- | --- | --- |
| `REPLENISH_MATERIAL` | `COMPOSITE_TASK` | warehouse shelf에서 material을 가져와 station material queue 보충 | `TRANSFER` |
| `TRANSFER` | `ATOMIC_TASK` | item을 source에서 destination으로 운반 | 없음 |
| `MANAGE_ROBOT_POWER` | `ATOMIC_TASK` | battery 교체 또는 충전 관련 처리 | 없음 |
| `SETUP_MACHINE` | `COMPOSITE_TASK` | queue에서 item을 가져와 machine setup/load | `LOAD_MACHINE` |
| `LOAD_MACHINE` | `ATOMIC_TASK` | machine에 item 적재 | 없음 |
| `UNLOAD_MACHINE` | `ATOMIC_TASK` | machine output을 output queue로 unload | 없음 |
| `INSPECT_PRODUCT` | `ATOMIC_TASK` | product 검사, pass/fail 판정 | 없음 |
| `REPAIR_MACHINE` | `COMPOSITE_TASK` | 고장 machine 진단 및 수리 | `INSPECT_MACHINE` |
| `PREVENTIVE_MAINTENANCE` | `COMPOSITE_TASK` | 예방 정비와 점검 | `INSPECT_MACHINE` |
| `INSPECT_MACHINE` | `ATOMIC_TASK` | machine 상태 점검 | 없음 |
| `HANDOVER_ITEM` | `ATOMIC_TASK` | product 공동 운반 합류 또는 handover 동기화 | 없음 |
| `COLLECT_WASTE_OR_SCRAP` | `COMPOSITE_TASK` | inspection scrap queue의 불량품 batch 폐기 운반 | `TRANSFER`, `UPDATE_INVENTORY_RECORD` |
| `UPDATE_INVENTORY_RECORD` | `ATOMIC_TASK` | 재고/처리 기록 갱신 | 없음 |

Decision layer의 기존 priority family 이름은 호환을 위해 남아 있지만, 실제 실행과 KPI/Replay는 `task_code`, child task path, `primitive_call_code`를 기준으로 봅니다.

### Priority Key Mapping

| Priority key | HumanoidSim task code |
| --- | --- |
| `material_supply` | `REPLENISH_MATERIAL` |
| `inter_station_transfer` | `TRANSFER` |
| `battery_swap` | `MANAGE_ROBOT_POWER` |
| `battery_delivery_low_battery` | `TRANSFER` |
| `battery_delivery_discharged` | `TRANSFER` |
| `setup_machine` | `SETUP_MACHINE` |
| `unload_machine` | `UNLOAD_MACHINE` |
| `inspect_product` | `INSPECT_PRODUCT` |
| `repair_machine` | `REPAIR_MACHINE` |
| `preventive_maintenance` | `PREVENTIVE_MAINTENANCE` |
| `handover_item` | `HANDOVER_ITEM` |
| `scrap_disposal` | `COLLECT_WASTE_OR_SCRAP` |

## Runtime Flow

1. ManSim이 현재 factory state를 보고 task candidate를 만듭니다.
2. `HumanoidTaskRuntime`이 priority key를 HumanoidSim task code로 변환합니다.
3. `TaskInstance.args`를 채웁니다. 예: source, destination, item type, machine id, shelf slot id.
4. HumanoidSim의 catalog/profile validation을 통과한 candidate만 실행 후보가 됩니다.
5. 선택된 task는 `TaskSpec -> child task -> primitive` plan으로 expand됩니다.
6. 각 task/child task/primitive start/end가 event로 기록됩니다.
7. 상태 변경은 ManSim이 직접 축 값을 쓰지 않고 `HumanoidSim.transition_humanoid_state()`에 event를 전달해 계산합니다.
8. Domain side effect는 ManSim helper가 수행합니다. 예: item pickup/drop, machine repair progress, inspection result.

전이가 잘못되면 strict fail로 처리하여 simulation 중에 worker id, event, 이전 snapshot, 실패 reason을 명확히 드러냅니다.

## Main Domain Flows

### Material Replenishment

- station material queue가 target보다 낮으면 `REPLENISH_MATERIAL` 후보가 생깁니다.
- worker는 warehouse material shelf slot의 service tile로 이동합니다.
- material이 있는 slot에서만 pickup할 수 있습니다.
- material을 들고 target station material queue까지 이동해 dropoff합니다.
- shelf가 비어 있으면 supply 후보를 만들지 않고 `MATERIAL_SHELF_EMPTY`와 KPI에 기록합니다.
- day boundary마다 빈 shelf slot만 capacity까지 restock합니다.

### Machine Setup and Load

- `SETUP_MACHINE`은 `LOAD_MACHINE` child task를 포함합니다.
- worker는 input queue에서 material/intermediate를 집고 machine service tile로 이동합니다.
- machine에 item을 load한 뒤 setup 시간이 진행됩니다.
- queue와 machine 사이의 carry 이동도 실제 tile path로 기록됩니다.

### Machine Unload

- processing이 끝난 machine에서 output item을 꺼냅니다.
- worker는 machine service tile에서 item을 집고 output queue service tile까지 이동합니다.
- output queue에 내려놓으면 downstream transfer 후보가 생깁니다.

### Product Inspection

- worker는 inspection input queue에서 product를 집습니다.
- inspection table service tile 중앙까지 이동한 뒤에만 `EXECUTE_QUALITY_ACTION`이 시작됩니다.
- pass product는 inspection output queue로 이동합니다.
- fail product는 inspection scrap queue로 이동하고 `scrap_count`가 증가합니다.
- inspection table은 한 번에 worker 한 명만 점유할 수 있습니다.

### Completed Product Transfer

- accepted product는 inspection output queue에서 `CompletedProducts` zone의 `completed_product_buffer`로 이동해야 최종 completed product로 집계됩니다.
- 단순히 inspection output queue에 쌓인 상태는 completed count가 아닙니다.

### Scrap Disposal

- inspection fail item은 `inspection_scrap_queue`에 쌓입니다.
- `COLLECT_WASTE_OR_SCRAP` worker는 최대 `quality.scrap_transport.max_carry_count`개를 batch cargo로 집습니다.
- `scrap_disposal_bin`에 dropoff하면 item state가 `SCRAPPED`가 되고 `disposed_scrap_count`가 증가합니다.

### Repair and Preventive Maintenance

- `REPAIR_MACHINE`은 `INSPECT_MACHINE` child task 후 `EXECUTE_MAINTENANCE_ACTION`을 수행합니다.
- 여러 worker가 repair team에 합류할 수 있고, KPI의 collaboration 지표에 반영됩니다.
- 이동 중인 worker에게는 repair bubble을 바로 표시하지 않고, 실제 maintenance primitive 구간에서만 작업 progress를 표시합니다.

### Handover and Shared Product Carry

- product는 material보다 무겁기 때문에 기본 이동 시간이 더 깁니다.
- product 운반 중 carrier가 1명이고 남은 경로가 충분하면 `HANDOVER_ITEM` 후보가 생길 수 있습니다.
- helper가 합류하면 같은 item id를 공유 cargo로 들고, 다음 tile segment부터 product 이동 시간이 carrier 수로 나뉩니다.
- helper task는 실제 합류 또는 종료 시점에 task context가 정리되어야 합니다.

## Movement and Traffic

ManSim의 기본 이동은 tile path 기반입니다.

- pathfinding: `TileGridMap`
- dynamic reservation: `movement.traffic.mode=strict_reservation`
- 이동 event: `AGENT_MOVE_START`, `AGENT_MOVE_TILE_START`, `AGENT_MOVE_TILE_END`, `AGENT_MOVE_END`
- Replay Studio는 `motion.path`를 따라 worker 위치를 보간합니다.

Traffic conflict는 policy 실패를 숨기지 않기 위해 event와 KPI에 남깁니다. 같은 worker끼리의 conflict는 core traffic bug로 봐야 합니다.

## Item Weight and Transport

이동 시간 multiplier는 [../configs/scenario/mfg_basic.yaml](../configs/scenario/mfg_basic.yaml)에 있습니다.

| Item type | 기본 multiplier | 설명 |
| --- | ---: | --- |
| material | 1.0 | 기준 이동 시간 |
| intermediate | 1.5 | material보다 무거움 |
| product | 2.0 | 가장 무거우며 공동 운반 가능 |

Product는 최대 2명까지 공동 운반할 수 있습니다.

## Incident Handling

Incident taxonomy는 HumanoidSim이 소유합니다. ManSim은 scenario-specific 발생 조건과 확률만 갖습니다.

현재 ManSim에서 낮은 확률로 발생시킬 수 있는 random incident:

- `OBJECT_RECOGNITION_FAILED`
- `GRIP_FAILED`
- `ITEM_DROPPED`
- `UNKNOWN`

자연 발생 incident:

- `RESOURCE_PREEMPTED`: 예상 resource가 다른 worker에 의해 선점됨
- `TRAFFIC_WAIT`: strict reservation 대기
- `NEAR_MISS`: traffic monitor near miss
- `COLLISION`: tile/edge 충돌

Incident는 state enum이 아닙니다. Availability가 보통 `BLOCKED`, `WAITING`, `DISABLED` 중 하나로 바뀌고, 원인은 `humanoid_state.reason.code`에 기록됩니다. Recovery protocol은 HumanoidSim incident schema에 정의된 task 또는 primitive code만 참조합니다.

ManSim은 incident가 발생하면 HumanoidSim에서 받은 recovery protocol step을 실제 `HUMANOID_TASK_*` 또는 `HUMANOID_STEP_*` timeline event로 실행합니다. Recovery protocol이 진행 중인 동안 worker availability는 `BLOCKED`를 유지합니다. 현재 recovery step은 `task_context`에 기록되며, Replay Studio에는 recovery 전용 새 panel을 만들지 않고 기존 Task 또는 Primitive 필드에 `CODE (RECOVERY)` 형식으로 표시합니다. 각 recovery step의 최소 관측 시간은 `configs/humanoidsim/default.yaml`의 `recovery_protocol` 섹션에서 조정합니다.

ManSim 내부에서 관찰되는 세부 실패 reason은 HumanoidSim의 incident alias로 resolve합니다. 예를 들어 `material_shelf_slot_empty`은 ManSim이 새 incident로 정의하는 것이 아니라 HumanoidSim schema의 alias를 통해 `RESOURCE_PREEMPTED`로 기록됩니다.

Replay Studio의 worker bubble은 incident code를 모두 보여주지 않고 availability 축을 짧게 표시합니다. 예: `BLK`, `WAIT`, `DIS`.

## KPI and Replay

Worker 관련 KPI는 HumanoidSim 기준을 사용합니다.

- `humanoid_state_time_by_worker`
- `humanoid_state_time_by_axis`
- `humanoid_state_ratio_by_worker`
- `humanoid_execution_ratio_by_worker`
- `humanoid_unavailable_ratio_by_worker`
- `humanoid_task_minutes`
- `humanoid_primitive_minutes`
- incident category/code/worker 집계
- collaboration sessions, shared product carry, shared repair 지표

Replay Studio worker panel은 다음 값을 `humanoid_state`에서 읽습니다.

- Availability
- Mobility
- Manipulation
- Task
- Child Task
- Primitive
- Reason / Incident
- Cargo item id 또는 batch item ids
- Motion path와 현재 tile

Power 값은 상세 panel에서 숨겨져 있지만, snapshot과 artifact에는 남습니다.

### Replay 표시 계약

Replay Studio는 simulation artifact를 보정하지 않고 표시합니다. 그래서 아래 규칙을 명시적으로 지킵니다.

- Worker 위치는 `entity_moved.payload.motion.path`와 `started_at/ended_at`을 기준으로 보간합니다.
- Worker task bubble은 이동 중에는 숨기고, 정지해서 task를 수행하는 구간에만 표시합니다.
- Worker progress bar는 primitive가 아니라 parent task의 `task_window` 기준으로 채웁니다.
- Traffic conflict는 worker끼리 직선을 연결하지 않고 tile/edge overlay와 오른쪽 panel text로만 표시합니다.
- Machine 위 item overlay는 `DONE_WAIT_UNLOAD`일 때만 finished output을 뜻합니다.
- `WAIT_INPUT`은 machine이 input을 기다리는 상태이므로 machine 위에 material/intermediate/product 이미지를 그리지 않습니다.
- `warehouse_buffer`는 내부 호환 alias로 남을 수 있지만, visible completed product target은 `completed_product_buffer`입니다.

## Debugging Checklist

Replay Studio에서 이상한 표시가 보이면 먼저 core artifact를 확인합니다.

- `events.jsonl`의 `details.humanoid_state`가 원본입니다.
- `task_context`가 task 종료 뒤에도 남아 있으면 core/runtime 정리 문제입니다.
- core event는 정상인데 Replay만 다르면 exporter/reducer/UI 문제입니다.
- worker 이동이 순간이동처럼 보이면 `entity_moved.payload.motion.path`와 start/end time을 확인합니다.
- traffic conflict에서 `primary_worker_id == other_worker_id`이면 traffic monitor bug입니다.
- resource가 사라져 현재 task를 이어갈 수 없으면 `WAITING`이 아니라 `BLOCKED`여야 합니다.

## Main Config

| 설정 | 파일 | 의미 |
| --- | --- | --- |
| `defaults` | `configs/config.yaml` | 기본 decision mode, scenario, humanoidsim config |
| `scenario.horizon.num_days` | `configs/scenario/mfg_basic.yaml` | 기본 5일 run |
| `quality.inspection.defect_prob` | `configs/scenario/mfg_basic.yaml` | inspection 불량률 |
| `quality.scrap_transport.max_carry_count` | `configs/scenario/mfg_basic.yaml` | scrap batch 최대 운반 개수 |
| `warehouse.material_shelf.capacity` | `configs/scenario/mfg_basic.yaml` | warehouse shelf slot 개수 |
| `movement.traffic.mode` | `configs/scenario/mfg_basic.yaml` | `strict_reservation` 등 traffic mode |
| `humanoid_incidents` | `configs/scenario/mfg_basic.yaml` | random incident 발생 확률과 trigger primitive |
| `primitive_timing.default_min` | `configs/humanoidsim/default.yaml` | primitive 최소 표시 시간 |
| `recovery_protocol.default_step_min` | `configs/humanoidsim/default.yaml` | recovery step 최소 표시 시간 |
