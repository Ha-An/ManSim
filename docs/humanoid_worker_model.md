# Humanoid Worker Model

이 문서는 ManSim에서 worker를 `HumanoidSim` 기반 휴머노이드 로봇으로 사용하는 방식을 정리합니다. State, Task, Primitive의 정의 주체는 `HumanoidSim`입니다. ManSim은 이 정의를 import해서 factory scenario 안에서 실행하고, 그 결과를 event, KPI, Replay Studio로 관찰합니다.

## Ownership

`HumanoidSim`가 소유하는 것:

- State schema
- Task schema
- Task catalog
- Primitive template
- Humanoid profile validation
- Task, Primitive, State의 의미 관계

ManSim이 소유하는 것:

- factory layout, tile map, queue, machine, inspection table
- task 후보 생성 조건
- priority decision flow
- domain side effect
- primitive별 environment-specific duration
- event log, KPI, Replay export

즉 `REPLENISH_MATERIAL`이 어떤 nested task sequence를 갖는지는 `HumanoidSim`의 task JSON에 있고, 그 child task와 primitive가 ManSim factory에서 어떤 queue와 item을 바꾸는지는 ManSim runtime에 있습니다.

## State Snapshot

Worker 상태는 `HumanoidStateSnapshot` 하나로 표현합니다. Legacy worker enum이나 Replay bucket state를 worker 의미 상태로 사용하지 않습니다.

예시:

```json
{
  "humanoid_id": "A1",
  "availability": "EXECUTING",
  "mobility": "NAVIGATING",
  "power": "POWER_NORMAL",
  "manipulation": "HOLDING",
  "task_context": {
    "task_code": "TRANSFER",
    "task_instance_id": "TR-42:TRANSFER",
    "step_id": "s06_navigate_to",
    "primitive_call_code": "NAVIGATE_TO",
    "execution_status": "RUNNING"
  },
  "reason": null,
  "timestamp_s": 128.4,
  "metadata": {
    "source": "mansim",
    "battery_remaining_min": 132.1
  }
}
```

## Availability State

로봇이 일을 받을 수 있는지와 task lifecycle상 어디에 있는지를 나타냅니다.

| State | 의미 | ManSim 적용 |
| --- | --- | --- |
| `AVAILABLE` | 새 task 수락 가능 | active task가 없고 task context가 비어 있음 |
| `ASSIGNED` | task는 받았지만 step 실행 전 | candidate 선택 직후, `HUMANOID_TASK_START` 전 |
| `EXECUTING` | task 실행 중 | task 또는 primitive 실행 중 |
| `WAITING` | 조건 대기 중 | resource, input, traffic wait, battery delivery 등 외부 조건 대기 |
| `BLOCKED` | 진행 불가 | task/primitive가 계속될 수 없고 reason이 필요 |
| `OFFLINE` | 운용 제외 | 현재 기본 scenario에서는 직접 쓰지 않지만 schema에 포함 |
| `DISABLED` | 방전/고장 등 작업 불가 | battery depleted 등으로 task 수행 불가 |

`WAITING`은 정상적인 대기 상태이고, `BLOCKED`는 현재 task를 계속하기 어려운 예외 상태입니다. `DISABLED`는 robot 자체가 작업 불가능한 상태입니다.

## Mobility State

로봇의 이동 관점 상태입니다.

| State | 의미 | ManSim 적용 |
| --- | --- | --- |
| `STATIONARY` | 멈춰 있음 | 이동 primitive가 실행 중이 아니거나 이동 종료 |
| `NAVIGATING` | 목적지로 이동 중 | `NAVIGATE_TO` 또는 `move_agent()` 실행 중 |
| `DOCKING` | 충전기/작업대/설비에 정렬 중 | schema에 포함되어 있으나 현재 ManSim 기본 flow에서는 별도 docking primitive로 세분화하지 않음 |

Replay Studio에서 worker가 움직이는 동안에는 `mobility=NAVIGATING`이고, motion path가 active입니다.

## Power State

배터리와 전원 관점 상태입니다.

| State | 의미 | ManSim 적용 |
| --- | --- | --- |
| `POWER_NORMAL` | 정상 전원 상태 | 기본 상태 |
| `POWER_LOW` | 전원 낮음 | 향후 policy/threshold에서 사용 가능 |
| `POWER_CRITICAL` | 전원 위험 수준 | 향후 policy/threshold에서 사용 가능 |
| `DEPLETED` | 방전됨 | 보통 `availability=DISABLED`와 함께 사용 |
| `CHARGING` | 충전 중 | `MANAGE_ROBOT_POWER` 또는 battery swap 중 |

ManSim의 battery runtime은 low/critical threshold를 task 후보 생성에 쓰지만, 현재 snapshot 축은 주로 `POWER_NORMAL`, `CHARGING`, `DEPLETED`로 관찰됩니다.

## Manipulation State

팔, 그리퍼, 적재 상태입니다.

| State | 의미 | ManSim 적용 |
| --- | --- | --- |
| `FREE` | 손이 비어 있음 | cargo 없음 |
| `REACHING` | 대상에 접근 중 | `REACH_TO` primitive |
| `HOLDING` | item/tool을 들고 있음 | `GRASP`, `LIFT`, cargo pickup 이후 |
| `PLACING` | 내려놓는 중 | `PLACE`, `RELEASE` primitive |

Cargo 변화는 item state와 동기화됩니다. Worker가 item을 집으면 worker는 `HOLDING`, item은 `CARRIED_BY_WORKER`가 됩니다. 내려놓으면 worker는 `FREE`로 돌아갑니다.

## Task, Primitive, State Relationship

Task는 목표 작업입니다. Primitive는 Task를 이루는 실행 단계입니다. State는 로봇의 현재 운용 상태입니다.

Task와 Primitive는 state가 아닙니다. 현재 task/primitive 정보는 `humanoid_state.task_context`에 들어갑니다.

예시:

```text
Task: INSPECT_PRODUCT
Primitive: EXECUTE_QUALITY_ACTION
State: availability=EXECUTING, mobility=STATIONARY, manipulation=HOLDING
```

Primitive는 state hint를 줄 수 있습니다.

| Primitive | State hint |
| --- | --- |
| `NAVIGATE_TO` | `mobility=NAVIGATING` |
| `REACH_TO` | `manipulation=REACHING` |
| `GRASP` | `manipulation=HOLDING` |
| `LIFT` | `manipulation=HOLDING` |
| `PLACE` | `manipulation=PLACING` |
| `RELEASE` | `manipulation=PLACING`, 종료 후 `FREE` |
| `EXECUTE_SYSTEM_ACTION` + `MANAGE_ROBOT_POWER` | `power=CHARGING` |

기록/검증 계열 primitive는 물리 state를 직접 바꾸지 않고 `task_context`만 갱신합니다.

## ManSim Task Subset

ManSim은 `HumanoidSim`의 82개 제조 task 중 현재 scenario에 맞는 subset만 실행합니다.

| Task Code | Level | Category | Template | ManSim 역할 |
| --- | --- | --- | --- | --- |
| `MANAGE_ROBOT_POWER` | `ATOMIC_TASK` | Robot Readiness & Self-Operation | `PT-SYSTEM` | battery swap |
| `TRANSFER` | `ATOMIC_TASK` | Mobility, Intralogistics & Material Flow | `PT-TRANSFER` | station 간 item 이동, inspection output warehouse 이동, battery delivery |
| `REPLENISH_MATERIAL` | `COMPOSITE_TASK` | Mobility, Intralogistics & Material Flow | `PT-REPLENISH` | warehouse material을 station material queue에 보충 |
| `SETUP_MACHINE` | `COMPOSITE_TASK` | Machine Tending & Equipment Operation | `PT-MACHINE` | machine input material/intermediate load 및 setup |
| `UNLOAD_MACHINE` | `ATOMIC_TASK` | Machine Tending & Equipment Operation | `PT-MACHINE` | machine output을 output buffer로 unload |
| `INSPECT_PRODUCT` | `ATOMIC_TASK` | Quality Inspection, Measurement & Testing | `PT-QUALITY` | inspection table에서 product 검사 |
| `PREVENTIVE_MAINTENANCE` | `COMPOSITE_TASK` | Maintenance, Repair & Calibration | `PT-MAINTENANCE` | idle machine 예방 정비 |
| `REPAIR_MACHINE` | `COMPOSITE_TASK` | Maintenance, Repair & Calibration | `PT-MAINTENANCE` | broken machine repair |
| `HANDOVER_ITEM` | `ATOMIC_TASK` | Human Collaboration & Operator Assistance | `PT-HUMAN` | product 공동 운반 합류 |

## Priority Key Mapping

Decision layer는 여전히 priority family 이름을 사용합니다. Humanoid runtime은 이를 task code로 변환합니다.

| Priority key / legacy task family | Humanoid task code |
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

## Task / Nested Sequences

아래 sequence는 `HumanoidSim/data/tasks/*.json`의 `steps`를 기준으로 합니다. `[...]` 표기는 해당 step이 primitive가 아니라 child task call임을 뜻합니다.

### `MANAGE_ROBOT_POWER`

```text
CHECK_CONTEXT
-> EXECUTE_SYSTEM_ACTION
-> VERIFY_ROBOT_STATE
-> LOG_RESULT
```

ManSim 적용:

- battery rack으로 이동합니다.
- fresh battery를 받고 spent battery를 반환합니다.
- `power=CHARGING` 또는 `POWER_NORMAL`으로 전환됩니다.

### `TRANSFER`

```text
NAVIGATE_TO
-> LOCALIZE_OBJECT
-> REACH_TO
-> GRASP
-> LIFT
-> NAVIGATE_TO
-> PLACE
-> RELEASE
-> VERIFY_PLACEMENT
```

ManSim 적용:

- station output buffer에서 downstream queue로 item을 옮깁니다.
- inspection output queue에서 warehouse로 accepted product를 옮깁니다.
- battery delivery도 `TRANSFER`로 표현합니다.
- Product를 warehouse까지 운반해야 `completed products`가 증가합니다.

### `REPLENISH_MATERIAL`

```text
CHECK_REQUEST
-> PRIMITIVE_IDENTIFY_ITEM
-> TRANSFER [ATOMIC_TASK]
-> VERIFY_LEVEL_OR_QUANTITY
-> UPDATE_RECORD
```

ManSim 적용:

- station material queue가 목표보다 낮을 때 후보가 됩니다.
- warehouse에서 material을 생성/집고 target material queue로 이동해 보충합니다.

### `SETUP_MACHINE`

```text
CHECK_SAFETY_ZONE
-> READ_MACHINE_STATE
-> LOAD_MACHINE [ATOMIC_TASK]
-> VERIFY_MACHINE_STATE
-> LOG_RESULT
```

ManSim 적용:

- machine service tile로 이동해 상태를 확인합니다.
- 필요한 material/intermediate queue로 이동해 item을 집습니다.
- machine service tile로 돌아와 item을 load하고 setup을 완료합니다.
- input queue와 machine 사이의 carry 이동이 실제 path로 기록됩니다.

### `UNLOAD_MACHINE`

```text
CHECK_SAFETY_ZONE
-> NAVIGATE_TO
-> READ_MACHINE_STATE
-> EXECUTE_MACHINE_ACTION
-> VERIFY_MACHINE_STATE
-> LOG_RESULT
```

ManSim 적용:

- `DONE_WAIT_UNLOAD` machine의 output을 집습니다.
- station output buffer까지 이동해 item을 내려놓습니다.

### `INSPECT_PRODUCT`

```text
PRIMITIVE_IDENTIFY_ITEM
-> LOCALIZE_OBJECT
-> EXECUTE_QUALITY_ACTION
-> CLASSIFY_RESULT
-> RECORD_RESULT
```

ManSim 적용:

- inspection input queue에서 product를 집습니다.
- `inspection_table` service tile 중앙까지 이동합니다.
- table에 도착한 뒤에만 inspection 시간이 소모됩니다.
- pass면 inspection output queue로 직접 운반합니다.
- fail이면 scrap 처리합니다.
- inspection workbench는 한 번에 하나의 worker만 점유합니다.

### `PREVENTIVE_MAINTENANCE`

```text
CHECK_SAFETY_ZONE
-> INSPECT_MACHINE [ATOMIC_TASK]
-> LOG_RESULT
```

ManSim 적용:

- idle machine에 대해 예방 정비를 수행합니다.
- maintenance 중 machine은 `UNDER_PM`으로 표시됩니다.
- 완료 후 failure probability modifier가 갱신됩니다.

### `REPAIR_MACHINE`

```text
CHECK_SAFETY_ZONE
-> INSPECT_MACHINE [ATOMIC_TASK]
-> EXECUTE_MAINTENANCE_ACTION
-> VERIFY_MACHINE_STATE
-> LOG_RESULT
```

ManSim 적용:

- broken machine으로 이동합니다.
- repair worker는 `max_repair_agents`까지 합류할 수 있습니다.
- repair progress는 active repair team size에 따라 빨라집니다.
- 멀리 이동 중인 worker에게 Replay bubble이 바로 뜨지 않고, 실제 `EXECUTE_MAINTENANCE_ACTION` 단계에서만 작업 표시가 뜹니다.

### `HANDOVER_ITEM`

```text
NAVIGATE_TO
-> ANNOUNCE_INTENT
-> EXECUTE_HUMAN_COLLABORATION_ACTION
-> CONFIRM_OPERATOR_STATE
-> LOG_RESULT
```

ManSim 적용:

- product transport session이 active이고 carrier가 1명일 때 후보가 됩니다.
- helper가 source carrier를 따라잡을 수 있을 만큼 남은 경로가 있어야 합니다.
- helper가 합류하면 같은 product id를 shared carry로 들고, 다음 tile segment부터 product 이동 시간이 carrier 수로 나뉩니다.
- 현재 기본 구현은 product 공동 운반 합류이며, 단순 소유권 인계 모드는 별도 확장 대상입니다.

## Supported Primitive Calls

ManSim runtime이 인식하는 primitive call code:

```text
ANNOUNCE_INTENT
CHECK_CONTEXT
CHECK_REQUEST
CHECK_SAFETY_ZONE
CLASSIFY_RESULT
CONFIRM_OPERATOR_STATE
CREATE_OR_UPDATE_RECORD
EXECUTE_HUMAN_COLLABORATION_ACTION
EXECUTE_MACHINE_ACTION
EXECUTE_MAINTENANCE_ACTION
EXECUTE_QUALITY_ACTION
EXECUTE_REPLENISHMENT_ACTION
EXECUTE_SYSTEM_ACTION
GRASP
INSPECT_OR_DIAGNOSE
LIFT
LOCALIZE_OBJECT
LOG_RESULT
NAVIGATE_TO
PLACE
PRIMITIVE_IDENTIFY_ITEM
READ_MACHINE_STATE
REACH_TO
RECORD_RESULT
RELEASE
UPDATE_RECORD
VERIFY_LEVEL_OR_QUANTITY
VERIFY_LOCKOUT_IF_REQUIRED
VERIFY_MACHINE_STATE
VERIFY_PLACEMENT
VERIFY_ROBOT_STATE
```

위 목록 중 현재 ManSim에서 적용하는 9개 task의 expanded primitive leaf에 실제로 들어가는 것은 29개입니다. `CREATE_OR_UPDATE_RECORD`는 ManSim runtime이 지원하지만 현재 적용 중인 9개 task에는 들어가지 않습니다. 다만 `HumanoidSim` 전체 catalog에는 `RECORD_QUALITY_RESULT`, `REPORT_HAZARD`, `UPDATE_INVENTORY_RECORD`, work order/traceability 계열 task에서 사용됩니다. 즉 문서에만 있는 값은 아니고, 향후 해당 task를 ManSim에 연결할 때 바로 쓸 수 있도록 runtime 지원 목록에 포함되어 있습니다.

Domain side effect가 있는 핵심 trigger:

| Task | Domain action trigger |
| --- | --- |
| `TRANSFER` | `GRASP` |
| `REPLENISH_MATERIAL` | child task `TRANSFER` |
| `MANAGE_ROBOT_POWER` | `EXECUTE_SYSTEM_ACTION` |
| `SETUP_MACHINE` | child task `LOAD_MACHINE` |
| `UNLOAD_MACHINE` | `EXECUTE_MACHINE_ACTION` |
| `INSPECT_PRODUCT` | `EXECUTE_QUALITY_ACTION` |
| `REPAIR_MACHINE` | `EXECUTE_MAINTENANCE_ACTION` |
| `PREVENTIVE_MAINTENANCE` | child task `INSPECT_MACHINE` |
| `HANDOVER_ITEM` | `EXECUTE_HUMAN_COLLABORATION_ACTION` |

## Primitive Timing

ManSim의 simulation clock 단위는 minute입니다. Primitive별 최소 duration은 [../configs/humanoidsim/default.yaml](../configs/humanoidsim/default.yaml)에 있습니다.

```yaml
primitive_timing:
  unit: min
  default_min: 0.1
  by_call_code: {}
```

이 값은 확인/기록 primitive가 0분으로 사라지지 않도록 하는 최소 표시/실행 시간입니다. Domain action primitive가 실제 이동, setup, unload, inspection, repair 등으로 더 오래 걸리면 실제 소요 시간이 우선합니다.

## Humanoid Profiles

기본 profile은 [../configs/humanoidsim/default.yaml](../configs/humanoidsim/default.yaml)에 정의되어 있습니다. `A1`, `A2`, `A3`는 같은 capability set을 갖습니다.

주요 capability:

- `navigation`
- `object_localization`
- `manipulation`
- `payload_handling`
- `inventory_interaction`
- `machine_interface`
- `safety_zone_check`
- `maintenance`
- `diagnostics`
- `inspection`
- `measurement`
- `digital_recording`
- `digital_context`
- `digital_transaction`
- `traceability`
- `system_operation`
- `tool_use`
- `vehicle_operation`
- `equipment_interaction`
- `human_collaboration`
- `handover`
- `safe_interaction`
- `high_risk_task`

Candidate task는 `validate_task_sequence()`를 통해 profile capability/resource/input 검증을 통과해야 실행 후보로 남습니다.

## Lifecycle

일반 task lifecycle:

```text
AVAILABLE
-> ASSIGNED
-> EXECUTING
-> AVAILABLE
```

조건이 맞지 않으면:

```text
ASSIGNED or EXECUTING
-> WAITING
-> EXECUTING or AVAILABLE
```

진행 불가능하면:

```text
ASSIGNED or EXECUTING or WAITING
-> BLOCKED
-> WAITING / EXECUTING / AVAILABLE / DISABLED
```

방전 등 robot 자체 문제:

```text
EXECUTING or WAITING
-> DISABLED + power=DEPLETED
```

Task 종료 후 `task_context`는 `null`이 됩니다. Replay exporter와 UI는 `humanoid_state.task_context`를 우선하며, stale `current_task_code`를 표시하지 않습니다.

## Item Transport And Handover

Item을 들고 이동할 때는 [../configs/scenario/mfg_basic.yaml](../configs/scenario/mfg_basic.yaml)의 weight multiplier가 tile segment duration에 적용됩니다.

```yaml
movement:
  item_transport:
    weight_time_multiplier:
      material: 1.0
      intermediate: 1.5
      product: 2.0
      battery: 1.0
    product_collaboration:
      enabled: true
      max_carriers: 2
      divide_time_by_carrier_count: true
```

Material은 기준 속도입니다. Intermediate는 1.5배, product는 2배 시간이 걸립니다. Product는 공동 운반 대상입니다.

Product transport session event:

- `PRODUCT_CARRY_STARTED`
- `PRODUCT_CARRY_JOINED`
- `PRODUCT_CARRY_COMPLETED`

KPI:

- `handover_item_count`
- `shared_product_carry_completed_count`
- `product_carry_time_min`
- `shared_product_carry_time_min`
- `item_transport_time_by_type`

## Event And Replay

Humanoid 관련 core event:

- `WORKER_STATE_CHANGED`
- `WORKER_CARGO_CHANGED`
- `HUMANOID_TASK_START`
- `HUMANOID_TASK_END`
- `HUMANOID_STEP_START`
- `HUMANOID_STEP_END`
- `AGENT_DISCHARGED`
- `PRODUCT_CARRY_STARTED`
- `PRODUCT_CARRY_JOINED`
- `PRODUCT_CARRY_COMPLETED`

Replay Studio worker panel은 다음 값을 `humanoid_state`에서 읽습니다.

- Availability
- Mobility
- Power
- Manipulation
- Task / Code
- Primitive
- Motion Path
- Traffic
- Carry item id
- Shared carry

Worker panel은 parent task, active child task, primitive를 분리해 표시합니다. Worker 말풍선과 progress bar는 이동 중에는 표시하지 않습니다. 정지 상태이고 실제 물리/domain primitive를 수행할 때만 task 기준 progress를 표시합니다.

## KPI

Worker/Humanoid KPI:

- `humanoid_state_time_by_worker`
- `humanoid_state_time_by_axis`
- `humanoid_state_ratio_by_worker`
- `humanoid_execution_ratio_by_worker`
- `humanoid_unavailable_ratio_by_worker`
- `humanoid_task_minutes`
- `humanoid_primitive_minutes`
- `humanoid_task_taxonomy`

Task grouping은 임의 grouping을 쓰지 않고 `HumanoidSim` catalog 기준만 사용합니다.

- `TaskSpec.level`
- `metadata.catalog.category_id`
- `metadata.catalog.category`

## Debugging Tips

Replay Studio에서 이상한 표시가 보이면 먼저 `events.jsonl`을 확인합니다.

- Core event도 같은 내용을 말하면 simulation core 문제입니다.
- Core event는 정상인데 Replay만 이상하면 exporter/reducer/UI 문제입니다.
- Worker 상태는 `details.humanoid_state`가 원본입니다.
- Task 종료 후에는 `task_context=null`이어야 합니다.
- Traffic conflict의 `primary_worker_id`와 `other_worker_id`가 같은 값이면 core traffic bug입니다.
