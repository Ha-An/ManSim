# Humanoid Worker Model

이 문서는 ManSim에서 worker를 `HumanoidSim` 기반 휴머노이드 로봇으로 다루는 방식을 설명합니다. ManSim은 휴머노이드의 Task, Primitive, State, Incident 의미를 직접 정의하지 않고, `HumanoidSim` catalog와 transition API를 import해 사용합니다.

관련 문서:

- [simulator_core_guide.md](simulator_core_guide.md): simulator core와 artifact 흐름
- [humanoid_movement_model.md](humanoid_movement_model.md): tile pathfinding, reservation, traffic model
- [replay_dashboards.md](replay_dashboards.md): Hub, KPI, Gantt, Replay Studio
- [../README.md](../README.md): 실행 방법과 v0.4.3 요약
- `C:\Github\HumanoidSim\docs\tasks_reference.md`: Task catalog reference
- `C:\Github\HumanoidSim\docs\primitives_reference.md`: Primitive reference
- `C:\Github\HumanoidSim\docs\state_reference.md`: State transition reference
- `C:\Github\HumanoidSim\docs\incident_reference.md`: Incident taxonomy reference

## Ownership Boundary

| 영역 | 소유 주체 | 설명 |
| --- | --- | --- |
| Task taxonomy | HumanoidSim | `PRIMITIVE_SKILL`, `ATOMIC_TASK`, `COMPOSITE_TASK`와 전체 task catalog |
| Primitive definition | HumanoidSim | primitive code, 설명, state relation, transition effect |
| State model | HumanoidSim | Availability, Mobility, Power, Manipulation 네 축과 transition graph |
| Incident taxonomy | HumanoidSim | 범용 incident code, category, default availability, recovery protocol |
| Factory object | ManSim | station, machine, queue, shelf, inspection table, scrap zone |
| Runtime side effect | ManSim | item 이동, machine 상태 변화, inspection 결과, repair progress |
| Metrics and replay | ManSim | event log, KPI, Gantt, 2D/3D Replay Studio artifact |

예를 들어 `REPLENISH_MATERIAL`이 어떤 child task와 primitive로 이루어지는지는 HumanoidSim이 결정합니다. 반면 어떤 warehouse shelf slot에서 material을 꺼내 어떤 station material queue에 넣는지는 ManSim scenario runtime이 결정합니다.

## State Model

Worker 상태는 `HumanoidStateSnapshot` 하나로 기록합니다. 예전의 단일 worker state enum이나 Replay 전용 요약 상태는 worker의 의미 상태로 사용하지 않습니다.

### Availability State

| State | 의미 | ManSim 적용 예 |
| --- | --- | --- |
| `AVAILABLE` | 새 task 수락 가능 | active task가 없고 task context가 비어 있음 |
| `ASSIGNED` | task를 받았지만 아직 본격 실행 전 | scheduler가 task를 선택한 뒤 최소 할당 체류 시간 동안 표시 |
| `EXECUTING` | task 또는 primitive 실행 중 | 이동, 집기, 놓기, 검사, 수리, 기록 primitive 수행 |
| `WAITING` | 예상 가능한 짧은 조건 대기 | operator 대기, 짧은 retry window |
| `BLOCKED` | 현재 task를 그대로 속행 불가 | resource 선점, grip 실패, item drop, path blocked, traffic wait recovery |
| `OFFLINE` | 운용 제외 | 기본 scenario에서는 거의 사용하지 않음 |
| `DISABLED` | 로봇 자체가 작업 불가 | 방전, 심각한 hardware/power incident |

`WAITING`은 같은 task를 계속 이어갈 수 있다는 전제가 남아 있는 대기입니다. `BLOCKED`는 현재 task의 전제가 깨져 recovery protocol이나 재할당이 필요한 상태입니다.

### Mobility State

| State | 의미 | ManSim 적용 예 |
| --- | --- | --- |
| `STATIONARY` | 멈춰 있음 | 이동 primitive가 끝났거나 작업 위치에 정지 |
| `NAVIGATING` | 목적지로 이동 중 | `NAVIGATE_TO`, `move_agent()` 실행 |
| `DOCKING` | charger, workbench, equipment에 정렬 중 | 작업대, 설비, 충전기 service tile에 맞추는 `ALIGN` 계열 step |

`STATIONARY`는 단순히 움직이지 않는 상태입니다. `DOCKING`은 멈춰 있는 것처럼 보여도 특정 설비나 작업대에 정렬하는 목적이 있는 mobility 상태입니다.

### Power State

| State | 의미 | ManSim 적용 예 |
| --- | --- | --- |
| `POWER_NORMAL` | 정상 전원 | 기본 상태 |
| `POWER_LOW` | 낮은 전원 | low threshold 이하 |
| `POWER_CRITICAL` | 위험 수준 전원 | mandatory/critical threshold 이하 |
| `DEPLETED` | 방전 | worker가 `DISABLED`로 전환 |
| `CHARGING` | 충전 중 | `MANAGE_ROBOT_POWER` 또는 charger 처리 |

### Manipulation State

| State | 의미 | ManSim 적용 예 |
| --- | --- | --- |
| `FREE` | 손이나 gripper가 비어 있음 | cargo 없음 |
| `REACHING` | 대상에 접근 중 | `REACH_TO` |
| `HOLDING` | item/tool을 들고 있음 | material, intermediate, product, scrap cargo 보유 |
| `PLACING` | 내려놓는 중 | queue, machine, inspection table, disposal bin에 배치 |

Cargo 변화는 manipulation state와 같이 기록됩니다. item을 집으면 보통 `HOLDING`, 내려놓으면 `FREE`로 돌아갑니다.

## Task, Child Task, Primitive

Task는 목표 작업이고 primitive는 task를 이루는 실행 단계입니다. State는 task 이름이 아니라 로봇의 현재 운용 상태입니다.

`COMPOSITE_TASK`는 하위 task를 직접 포함하는 workflow입니다. ManSim은 parent task, active child task, active primitive를 모두 event와 Replay panel에 남깁니다.

| 개념 | 예 | ManSim 표시 |
| --- | --- | --- |
| Parent task | `REPLENISH_MATERIAL` | Worker panel의 `Task` |
| Child task | `TRANSFER` | Worker panel의 `Child Task` |
| Primitive | `NAVIGATE_TO`, `GRASP`, `PLACE` | Worker panel의 `Primitive` |
| Recovery step | `GRASP (RECOVERY)` | 기존 Task/Primitive 칸에 `(RECOVERY)` suffix |

정상적으로 실행 중인 모든 primitive는 HumanoidSim 정의에 따라 `availability=EXECUTING`입니다. Incident recovery protocol 안에서 실행되는 step은 정상 생산 task가 아니므로 availability를 `BLOCKED`로 유지하고, 현재 step만 `CODE (RECOVERY)`로 표시합니다.

## ManSim Task Subset

ManSim v0.4.3에서 factory flow에 실제 연결된 HumanoidSim task는 다음과 같습니다.

| Task code | Level | ManSim 역할 |
| --- | --- | --- |
| `REPLENISH_MATERIAL` | Composite | warehouse shelf material을 station material queue로 보충 |
| `TRANSFER` | Atomic | item, battery, completed product 등 위치 간 운반 |
| `MANAGE_ROBOT_POWER` | Composite/Atomic | battery swap, charging, power 관련 처리 |
| `SETUP_MACHINE` | Composite | input queue에서 item을 가져와 machine setup/load로 연결 |
| `LOAD_MACHINE` | Atomic | machine에 item 적재 |
| `UNLOAD_MACHINE` | Atomic | machine output을 station output buffer로 운반 |
| `INSPECT_PRODUCT` | Atomic | inspection table에서 product 검사 후 output/scrap queue로 이동 |
| `REPAIR_MACHINE` | Composite | breakdown machine 수리, helper 합류 포함 |
| `PREVENTIVE_MAINTENANCE` | Composite | idle machine preventive maintenance |
| `INSPECT_MACHINE` | Atomic | repair/maintenance 중 machine 상태 점검 child task |
| `HANDOVER_ITEM` | Composite/Atomic | product 공동 운반 helper 합류 |
| `COLLECT_WASTE_OR_SCRAP` | Composite/Atomic | inspection scrap queue의 불량품 batch를 폐기 zone으로 운반 |
| `UPDATE_INVENTORY_RECORD` | Atomic | inventory 관련 기록 step |

HumanoidSim catalog에 있는 모든 task가 ManSim에서 실행되는 것은 아닙니다. ManSim은 현재 factory scenario에 필요한 subset만 후보 생성과 primitive side effect에 연결합니다.

## Runtime Flow

ManSim의 한 task 실행은 아래 순서로 진행됩니다.

1. Factory state에서 가능한 task 후보를 생성합니다.
2. Candidate에 `task_code`, `args`, `instance_id`, target 정보를 채웁니다.
3. HumanoidSim profile과 task catalog로 validation을 수행합니다.
4. Scheduler나 manager가 task를 선택합니다.
5. Worker state를 HumanoidSim transition API로 `ASSIGNED`로 전환합니다.
6. Parent task와 child task boundary event를 기록합니다.
7. Primitive start/end마다 HumanoidSim transition API로 state snapshot을 갱신합니다.
8. ManSim primitive executor가 item, machine, queue, shelf, inspection table 같은 factory side effect를 적용합니다.
9. Task 종료 시 task context를 비우고 worker를 `AVAILABLE`로 되돌립니다.

State 축을 직접 대입하는 로직은 ManSim에 두지 않는 것이 원칙입니다. ManSim은 "어떤 일이 발생했는지"를 event로 전달하고, state transition은 HumanoidSim이 판단합니다.

## Domain Flows

### Warehouse Material Shelf

Warehouse material은 shelf slot에 놓인 item입니다. Worker는 slot의 service tile까지 이동해야 pickup할 수 있습니다.

- Shelf capacity, initial fill, restock rule은 `configs/scenario/mfg_basic.yaml`에서 설정합니다.
- Shelf와 shelf 뒤 wall은 pathfinding blocking object입니다.
- Material이 없거나 다른 worker가 먼저 가져가면 HumanoidSim incident alias를 통해 `RESOURCE_PREEMPTED` 또는 `RESOURCE_MISSING`으로 기록됩니다.
- 매일 day boundary에서 빈 slot만 capacity까지 보충합니다.

### Setup / Load / Unload

Setup과 unload는 실제 carry 이동을 포함합니다.

- `SETUP_MACHINE`: input queue로 이동, item pickup, machine service tile로 이동, setup/load 수행.
- `LOAD_MACHINE`: machine에 item을 올려 machine process가 시작될 수 있게 함.
- `UNLOAD_MACHINE`: machine output item을 집고 output buffer까지 운반.

Machine 위에 item이 보이는 경우는 실제 `machine.current_item`이나 output item state를 반영한 것입니다. stale overlay를 피하기 위해 exporter는 machine state와 item state를 함께 확인합니다.

### Inspection And Scrap

Inspection은 inspection table에서만 수행됩니다.

- Worker는 inspection input queue에서 product를 집습니다.
- Inspection table service tile에 도착해야 `EXECUTE_QUALITY_ACTION`이 시작됩니다.
- Pass product는 inspection output queue로 운반됩니다.
- Fail product는 inspection scrap queue에 들어가고 `scrap_count`가 증가합니다.
- `COLLECT_WASTE_OR_SCRAP`는 scrap batch를 `scrap_disposal_bin`으로 운반하고 `disposed_scrap_count`를 증가시킵니다.

2D/3D Replay Studio는 `inspection_table` footprint를 검사 테이블로 렌더링합니다.

### Completed Products

Completed product KPI는 inspection output queue에 쌓인 개수가 아닙니다. Accepted product가 `CompletedProducts` zone의 `completed_product_buffer`까지 운반되어야 `total_products`에 반영됩니다.

### Repair And Preventive Maintenance

Repair는 여러 worker가 같은 machine에 합류할 수 있습니다. ManSim은 repair team size와 progress를 machine event로 기록하고, KPI에서는 collaboration 통계에 반영합니다.

### Product Handover

Product는 material보다 무겁기 때문에 이동 시간이 더 깁니다. Product transport session이 active이고 carrier가 1명이며 남은 경로가 충분하면 다른 worker가 `HANDOVER_ITEM`으로 합류할 수 있습니다. 합류 후 다음 tile segment부터 product 이동 multiplier가 carrier 수로 나뉩니다.

## Movement And Traffic

Worker 이동은 tile path 기반입니다. A* path는 `TileGridMap.find_path()`가 만들고, worker는 `movement.traffic.mode`에 따라 다음 tile을 예약하거나 conflict를 관찰합니다.

| Mode | 의미 |
| --- | --- |
| `strict_reservation` | 다음 tile 예약 실패 시 worker는 이동하지 않고 `TRAFFIC_WAIT` incident와 recovery protocol을 실행 |
| `observe_conflicts` | 충돌 가능 상황을 차단하지 않고 `PATH_OVERLAP`, `NEAR_MISS`, `COLLISION` event/KPI/Replay overlay로 관찰 |

예약 실패, 장기 path block, near miss, collision은 ManSim 자체 incident taxonomy가 아니라 HumanoidSim incident code로 기록합니다.

## Incident And Recovery

Incident는 state가 아닙니다. Incident는 `StateReason`과 recovery protocol로 표현됩니다.

| 상황 | Canonical incident | 기본 availability |
| --- | --- | --- |
| item 인식 실패 | `OBJECT_RECOGNITION_FAILED` | `BLOCKED` |
| grip 실패 | `GRIP_FAILED` | `BLOCKED` |
| item drop | `ITEM_DROPPED` | `BLOCKED` |
| target resource를 다른 worker가 먼저 사용 | `RESOURCE_PREEMPTED` | `BLOCKED` |
| 필요한 resource가 없음 | `RESOURCE_MISSING` | `BLOCKED` |
| tile reservation 실패 | `TRAFFIC_WAIT` | `BLOCKED` 또는 짧은 retry window의 `WAITING` |
| path가 막힘 | `PATH_BLOCKED` | `BLOCKED` |
| 원인 불명 | `UNKNOWN` | `BLOCKED` |

Recovery protocol은 HumanoidSim incident profile에 정의된 task/primitive sequence입니다. ManSim은 이 sequence를 실제 timeline으로 실행하고, 현재 step을 Task 또는 Primitive context에 `(RECOVERY)`로 표시합니다.

Item drop이 발생하면 item은 현재 tile에 `DROPPED` state로 남습니다. Recovery protocol은 떨어진 item을 다시 localize, reach, grasp, lift한 뒤 기존 운반 flow로 복귀하도록 연결됩니다.

## Replay Rules

Replay Studio는 simulation artifact를 왜곡하지 않고 관찰하는 도구입니다.

- Worker label은 `A1`, `A2`처럼 id만 표시합니다.
- Worker 말풍선은 이동 중에는 숨기고, 멈춰서 작업하거나 blocked/waiting일 때만 표시합니다.
- Incident 중에는 긴 incident code 대신 availability badge를 우선 사용합니다. 예: `BLK`, `WAIT`, `DIS`.
- Worker task progress bar는 primitive가 아니라 parent task window 기준으로 채웁니다.
- Worker 이동은 `motion.path` polyline을 따라 보간합니다. 예약 실패처럼 멈춘 motion은 현재 tile에 고정하고 `display_path`만 점선으로 보여줍니다.
- 3D Replay는 worker가 cargo를 들고 있으면 item shape를 몸 앞에 유지합니다. 작업 중 팔 동작이 있어도 cargo visual은 사라지지 않아야 합니다.
- Queue item은 item type별 shape/color를 사용합니다. Input queue는 노랑, output/completed queue는 파랑, scrap queue는 빨강 계열로 표시합니다.

## KPI And Gantt

Worker KPI와 Gantt는 HumanoidSim state schema를 기준으로 합니다.

- Availability, Mobility, Power, Manipulation 축별 time ratio를 집계합니다.
- 발생하지 않은 state도 schema에 있으면 0으로 표시합니다.
- Gantt worker lane은 `A1`, `A2`, `A3` 순서로 정렬하고 Availability State 기준 segment를 표시합니다.
- Task/primitive 시간은 exact code와 HumanoidSim category/level 기준으로 집계합니다.
- Incident KPI는 code, category, worker별 count를 포함합니다.
- Collaboration KPI는 product shared carry와 repair team participation을 포함합니다.

## Config Reference

| 설정 | 파일 | 의미 |
| --- | --- | --- |
| `humanoidsim` | `configs/config.yaml` | HumanoidSim runtime config default |
| `task_lifecycle.assignment_min_duration` | `configs/humanoidsim/default.yaml` | `ASSIGNED` 상태 최소 관찰 시간 |
| `primitive_timing.default_min` | `configs/humanoidsim/default.yaml` | primitive 최소 표시 시간 |
| `recovery_protocol.default_step_min` | `configs/humanoidsim/default.yaml` | recovery step 최소 표시 시간 |
| `movement.traffic.mode` | `configs/scenario/mfg_basic.yaml` | `strict_reservation` 또는 `observe_conflicts` |
| `humanoid_incidents.random` | `configs/scenario/mfg_basic.yaml` | random incident 확률과 trigger primitive |
| `warehouse.material_shelf` | `configs/scenario/mfg_basic.yaml` | shelf capacity, initial fill, restock policy |
| `quality.inspection.defect_prob` | `configs/scenario/mfg_basic.yaml` | inspection fail probability |
| `quality.scrap_transport.max_carry_count` | `configs/scenario/mfg_basic.yaml` | scrap batch 최대 운반 개수 |

## Debug Checklist

화면에서 이상한 장면이 보이면 아래 순서로 확인합니다.

1. `events.jsonl`에서 해당 worker의 task/step/move/incident event를 확인합니다.
2. `minute_snapshots.json`에서 worker tile, cargo, `humanoid_state`가 같은 시점에 맞는지 봅니다.
3. `replay_studio_log.json`에서 exporter가 stale task, stale motion, stale cargo를 남기지 않았는지 확인합니다.
4. 2D와 3D Replay가 같은 artifact를 다르게 표현한다면 renderer 문제로 봅니다.
5. KPI나 Gantt가 화면과 다르면 `kpi.json`, `gantt_segments.csv` source를 먼저 확인합니다.

Core event가 정상이고 Replay만 다르면 exporter/reducer/UI 문제일 가능성이 큽니다. Core event 자체가 틀리면 `world.py`, `humanoid_runtime.py`, `grid_map.py`, `traffic.py`를 우선 확인합니다.
