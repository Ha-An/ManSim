# Humanoid Worker Model

이 문서는 ManSim에서 worker를 `HumanoidSim` 기반 휴머노이드 로봇 runtime instance로 사용하는 방식을 정리합니다. ManSim은 Task, Primitive, State, Incident의 기본 의미를 직접 정의하지 않고 `HumanoidSim` catalog와 transition API를 import해 사용합니다.

관련 문서:

- [simulator_core_guide.md](simulator_core_guide.md): simulation core와 artifact 흐름
- [humanoid_movement_model.md](humanoid_movement_model.md): tile pathfinding, reservation, traffic model
- [decision_logic.md](decision_logic.md): decision mode와 task dispatch 정책
- [replay_dashboards.md](replay_dashboards.md): Hub, KPI, Gantt, Replay Studio
- `C:\Github\HumanoidSim\docs\tasks_reference.md`
- `C:\Github\HumanoidSim\docs\primitives_reference.md`
- `C:\Github\HumanoidSim\docs\state_reference.md`
- `C:\Github\HumanoidSim\docs\incident_reference.md`

## Ownership Boundary

| 영역 | 소유 주체 | 설명 |
| --- | --- | --- |
| Task taxonomy | HumanoidSim | `PRIMITIVE_SKILL`, `ATOMIC_TASK`, `COMPOSITE_TASK`와 task catalog |
| Primitive definition | HumanoidSim | primitive code, 설명, state relation, transition effect |
| State model | HumanoidSim | Availability, Mobility, Power, Manipulation 축과 transition graph |
| Incident taxonomy | HumanoidSim | incident code, category, default availability, recovery protocol |
| Factory object | ManSim | station, machine, queue, shelf, inspection table, scrap zone |
| Runtime side effect | ManSim | item 이동, machine 상태 변경, inspection 결과, repair progress |
| Metrics and replay | ManSim | event log, KPI, Gantt, 2D/3D Replay Studio artifact |

예를 들어 `REPLENISH_MATERIAL`이 어떤 child task와 primitive로 구성되는지는 HumanoidSim이 결정합니다. 반면 어떤 warehouse shelf slot에서 material을 집어 어느 station queue에 넣는지는 ManSim scenario runtime이 결정합니다.

## State Model

Worker state는 `HumanoidStateSnapshot` 하나로 기록합니다. worker용 legacy enum이나 replay bucket state를 별도 의미 상태로 사용하지 않습니다.

| Axis | States |
| --- | --- |
| Availability | `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED` |
| Mobility | `STATIONARY`, `NAVIGATING`, `DOCKING` |
| Power | `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING` |
| Manipulation | `FREE`, `REACHING`, `HOLDING`, `PLACING` |

`WAITING`은 같은 task를 계속 이어갈 수 있다는 전제가 남아 있는 짧은 대기입니다. `BLOCKED`는 현재 task를 그대로 속행할 수 없어 incident, recovery, replan이 필요한 상태입니다.

정상 실행 중인 모든 primitive는 HumanoidSim 정의에 따라 `availability=EXECUTING`입니다. Incident recovery step은 일반 생산 task가 아니므로 recovery가 진행되는 동안 availability는 `BLOCKED`로 유지되고, Task/Primitive 표시에는 `(RECOVERY)` suffix를 붙입니다.

## Task, Child Task, Primitive

Task는 목표 작업이고 primitive는 task를 이루는 실행 단계입니다. State는 task 이름이 아니라 로봇의 현재 운용 상태입니다.

`COMPOSITE_TASK`는 child task call을 포함하는 workflow입니다.

| 개념 | 예시 | Replay 표시 |
| --- | --- | --- |
| Parent task | `REPLENISH_MATERIAL` | `Task` |
| Child task | `TRANSFER` | `Child Task` |
| Primitive | `NAVIGATE_TO`, `GRASP`, `PLACE` | `Primitive` |
| Recovery step | `GRASP (RECOVERY)` | 기존 Task/Primitive 위치 |

## Generic Request and Concrete Binding

ManSim task 후보가 항상 concrete item id를 미리 고정하는 것은 아닙니다. `REPLENISH_MATERIAL`은 “Station N에 material을 보충하라”는 generic material request로 생성되며, rolling horizon pool에서도 특정 `MAT-WH-*` 대신 station과 source/destination만 표시합니다.

Concrete material instance는 task 실행 중 `PRIMITIVE_IDENTIFY_ITEM` 단계에서 warehouse shelf를 스캔해 선택합니다. 이 시점에 ManSim runtime이 `source_slot_id`, `transfer_item_id`, `material_item_id`를 task payload에 채우고, 이후 child `TRANSFER`가 해당 material을 운반합니다.

반면 `SETUP_MACHINE`, `INSPECT_PRODUCT`, 일반 `TRANSFER`는 현재 scenario에서 queue item 자체가 task 대상이므로 후보 단계에서 concrete item id를 유지합니다.

## ManSim Task Subset

ManSim v0.4.3에서 factory flow에 연결된 HumanoidSim task subset은 다음과 같습니다.

| Task code | 역할 |
| --- | --- |
| `REPLENISH_MATERIAL` | warehouse shelf material을 station material queue로 보충 |
| `TRANSFER` | item, battery, completed product 등 위치 간 운반 |
| `MANAGE_ROBOT_POWER` | battery swap, charging, power 처리 |
| `SETUP_MACHINE` | input queue item을 machine setup/load에 연결 |
| `LOAD_MACHINE` | machine에 item 적재 |
| `UNLOAD_MACHINE` | machine output을 station output buffer로 이동 |
| `INSPECT_PRODUCT` | inspection table에서 product 검사 후 output/scrap queue로 이동 |
| `REPAIR_MACHINE` | breakdown machine 수리 |
| `PREVENTIVE_MAINTENANCE` | idle machine preventive maintenance |
| `INSPECT_MACHINE` | repair/maintenance 중 machine 상태 진단 child task |
| `HANDOVER_ITEM` | product 공동 운반 helper 합류 |
| `COLLECT_WASTE_OR_SCRAP` | inspection scrap queue의 불량품 batch를 ScrapDisposal로 운반 |
| `UPDATE_INVENTORY_RECORD` | inventory 기록 step |

HumanoidSim catalog의 모든 task가 ManSim에서 실행되는 것은 아닙니다. ManSim은 현재 factory scenario에 필요한 subset만 candidate 생성과 primitive side effect에 연결합니다.

## Dedicated Roles Mode

`rolling_horizon_dedicated_roles`는 worker별 task code를 명확히 나누는 rolling horizon mode입니다. 현재 root config의 기본 decision mode입니다.

| Worker | Task priority order |
| --- | --- |
| A1 | `REPLENISH_MATERIAL` |
| A2 | `REPAIR_MACHINE`, `SETUP_MACHINE`, `UNLOAD_MACHINE` |
| A3 | `MANAGE_ROBOT_POWER`, `TRANSFER`, `INSPECT_PRODUCT`, `COLLECT_WASTE_OR_SCRAP`, `PREVENTIVE_MAINTENANCE` |

이 mode에서 `HANDOVER_ITEM`은 협업 task이므로 pool에 수집하지 않습니다. `REPAIR_MACHINE`은 A2 단독 작업으로 제한되어 repair helper join이 발생하지 않습니다. A1/A2는 battery station으로 직접 self swap을 가지 않고, 20% 이하일 때 A3의 battery delivery 대상이 됩니다.

## Runtime Flow

1. ManSim이 factory state에서 feasible task candidate를 생성합니다.
2. `HumanoidTaskRuntime`이 candidate를 HumanoidSim task code와 step plan으로 bind합니다.
3. Decision mode가 candidate를 선택하거나 rolling pool에 저장합니다.
4. 선택된 task는 parent task, child task, primitive event를 남기며 실행됩니다.
5. State 전이는 HumanoidSim transition API를 통해 `HumanoidStateSnapshot`으로 기록됩니다.
6. Replay/KPI/Gantt는 snapshot과 event를 그대로 읽어 관찰합니다.

## Observability

- `events.jsonl`: task, child task, primitive, movement, incident, recovery event
- `minute_snapshots.json`: worker별 `humanoid_state`
- `kpi.json`: task minutes, primitive minutes, state time, incident, rolling horizon metrics
- 2D/3D Replay Studio: worker panel, motion path, traffic/incident, rolling task pool
- Gantt chart: worker lane을 Availability State 기준으로 표시
