# Simulator Core Guide

이 문서는 ManSim `v0.6.0`의 `manufacturing_sim/` 아래 제조 simulator core를 설명합니다. Humanoid State/Task/Primitive 상세는 [humanoid_worker_model.md](humanoid_worker_model.md), 이동 경로계획과 traffic 상세는 [humanoid_movement_model.md](humanoid_movement_model.md), Replay/Dashboard 상세는 [replay_dashboards.md](replay_dashboards.md)에 분리되어 있습니다.

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

- `manufacturing_sim/simulation/scenarios/registry.py`: `scenario.type` lookup and plugin runner dispatch.
- `manufacturing_sim/simulation/timing.py`: scenario timing profile validation, deterministic triangular sampling, expected duration calculation, timing fingerprint.
- `manufacturing_sim/simulation/scenarios/manufacturing/world.py`: Factory world state, task enumeration, execution, KPI aggregation.
- `manufacturing_sim/simulation/scenarios/manufacturing/humanoid_runtime.py`: HumanoidSim catalog/profile validation, nested task flattening, primitive execution bridge.
- `manufacturing_sim/simulation/scenarios/manufacturing/grid_map.py`: Tile map, pathfinding, object footprints, worker tile occupancy.
- `manufacturing_sim/simulation/scenarios/manufacturing/traffic.py`: Path overlap, tile conflict, edge conflict, near miss detection.
- `manufacturing_sim/simulation/scenarios/manufacturing/entities.py`: `Worker`, `Machine`, `Task`, `Item` dataclasses and machine/item domain states.
- `manufacturing_sim/simulation/scenarios/manufacturing/processes.py`: SimPy process orchestration.
- `manufacturing_sim/simulation/scenarios/manufacturing/logging.py`: `events.jsonl` event writer.
- `manufacturing_sim/simulation/scenarios/manufacturing/run.py`: Scenario execution entrypoint and artifact export.
- `manufacturing_sim/simulation/scenarios/shipyard/world.py`: Shipyard surface tile state, task enumeration, execution, and makespan KPI aggregation.
- `manufacturing_sim/simulation/scenarios/shipyard/grid_map.py`: Shipyard 100x70 tile map, central fixed ship silhouette, work tile service tiles, and replay layout export.
- `manufacturing_sim/simulation/scenarios/shipyard/run.py`: Shipyard scenario execution entrypoint and artifact export.

## Scenario Plugins

ManSim은 `scenario.type` 값을 registry에서 해석해 scenario plugin을 실행합니다. 제조 시나리오는 `scenario=factory_mfg_basic`으로 직접 실행합니다.

| Scenario | Purpose |
| --- | --- |
| `factory_mfg_basic` | Warehouse -> Station 1 -> Station 2 -> Inspection 제조 공정입니다. 기존 ManSim factory flow와 artifact schema를 유지합니다. |
| `mfg_flow_shop` | Station 1/2 각각 병렬 설비 2대, 유한 buffer, worker별 전용 충전 도크와 단일 Inspection Desk를 사용하는 flow-shop 제조 공정입니다. Active-processing 기준 고장·예방정비와 공동수리를 사용하며 handover/battery delivery는 없습니다. |
| `shipyard_basic` | 중앙 고정 ship hull silhouette의 exterior surface tile별 용접, 표면처리, 도장, 검사를 수행합니다. 핵심 KPI는 `makespan_min`입니다. |

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

`mfg_flow_shop`의 유한 용량은 S1 material/output=`4/2`, S2 material/intermediate/output=`4/4/2`, Inspection input/pass/scrap=`3/3/3`입니다. 실제 점유 item과 dispatch된 inbound reservation의 합이 capacity를 넘을 수 없습니다. Machine 가공이 끝났지만 output buffer에 빈 slot이 없으면 결과물은 machine에 남고 상태는 `DONE_WAIT_UNLOAD`로 유지되며, unload가 끝날 때까지 해당 machine의 다음 cycle은 시작되지 않습니다.

`completed products`는 inspection output queue에 놓인 시점이 아니라, accepted product가 `completed_product_buffer`까지 운반된 시점에 증가합니다.

## Shipyard Flow

`shipyard_basic`은 assembly 공정 없이 선박 외관 수리 과정을 모델링합니다. 100x70 tile map 중앙에 배 모양의 blocking hull silhouette를 만들고, 그 hull 중 외부와 맞닿은 surface tile 약 120개만 작업 대상으로 둡니다. Worker는 ship tile 위에 올라가지 않고, 각 tile의 passable adjacent service tile에서 작업합니다.

Surface tile lifecycle:

```text
WAIT_WELD
  -> WELDED
  -> SURFACE_PREPARED
  -> PAINTED
  -> COMPLETE
```

검사 실패 시 surface tile은 `REWORK_REQUIRED`가 되고 rework target에 따라 `WAIT_WELD` 또는 `SURFACE_PREPARED` 계열 흐름으로 되돌아갑니다. v1에서는 `VERIFIED`를 stable 표시 state로 사용하지 않고, 검사 통과 즉시 `COMPLETE`로 전환합니다.

Shipyard에서 사용하는 주요 HumanoidSim task:

| Task code | World trigger |
| --- | --- |
| `OPERATE_VEHICLE_TRANSPORT` | `MaterialYard` 또는 `PaintSupply` 근처에서 cart에 `weld_wire` 또는 `paint_can`을 batch로 싣고 ship 주변 parking spot까지 운전해야 할 때 생성됩니다. |
| `TRANSFER` | Parking spot에 세워진 cart inventory에서 work tile까지 `weld_wire` 또는 `paint_can`을 1개 공급해야 할 때 생성됩니다. Target은 `ship_tile_0001` 같은 surface tile id입니다. |
| `WELD_SEAM` | Tile state가 `WAIT_WELD`이고 weld supply가 준비되었을 때 생성됩니다. 완료 후 `WELDED`가 됩니다. |
| `PREPARE_SURFACE` | Tile state가 `WELDED`일 때 생성됩니다. 완료 후 `SURFACE_PREPARED`가 됩니다. |
| `PAINT_SURFACE` | Tile state가 `SURFACE_PREPARED`이고 paint supply가 준비되었을 때 생성됩니다. 완료 후 `PAINTED`가 됩니다. |
| `VERIFY_SHIP_SECTION` | Tile state가 `PAINTED`일 때 생성됩니다. 통과하면 `COMPLETE`, 실패하면 `REWORK_REQUIRED`가 됩니다. |

Shipyard cart logistics:

- `ToolCrib` zone은 제거했고, `PaintSupply`는 기존 `ToolCrib` 위치인 좌상단으로 이동했습니다.
- `MaterialYard`는 `weld_wire`, `PaintSupply`는 `paint_can` source로 사용합니다.
- Cart는 기본 2대이며 `shipyard.logistics.cart_count`로 조정합니다.
- Cart capacity는 기본 20개이며 `shipyard.logistics.cart_capacity`로 조정합니다.
- Cart footprint는 기본 2 tile입니다. 뒤쪽 cockpit tile은 worker가 조종하는 자리이고, 앞쪽 cargo tile은 짐을 싣는 자리입니다.
- Cart는 `cart_route_tiles`로 표시되는 2-tile-wide lane과 6개 parking spot에서만 이동/정차합니다.
- Work tile까지의 최종 공급은 worker가 parking cart에서 1개를 꺼내는 짧은 `TRANSFER(cart_supply)`로 표현합니다.

Shipyard KPI:

- `makespan_min`: 모든 surface tile이 `COMPLETE`가 된 시점입니다. 완료 전 run에서는 `pending`으로 표시됩니다.
- `surface_tile_count`
- `completed_surface_tile_count`, `surface_tile_completion_ratio`
- `welded_surface_tile_count`, `painted_surface_tile_count`
- `surface_tile_state_counts`
- `rework_count`, `quality_pass_rate`
- `worker_utilization_by_worker`
- `cart_trip_count`, `cart_items_moved`, `cart_wait_time_min`, `cart_utilization`, `cart_collision_wait_count`

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

HumanoidSim은 Task/Primitive hierarchy를 소유하고 ManSim은 scenario별 실행시간을 소유합니다. Factory와 Shipyard는 각각 독립 timing profile을 자동 로드하며 다른 scenario의 값이나 공통 primitive 기본값으로 fallback하지 않습니다. Runtime은 exact `step_path`를 기준으로 primitive duration을 한 번만 샘플링하고, 이동 primitive는 실제 tile path 길이에 scenario별 sampled per-tile time을 적용합니다. 설정 형식과 검증 규칙은 [scenario_timing.md](scenario_timing.md)를 참고합니다.

현재 ManSim에서 사용하는 task code:

- `REPLENISH_MATERIAL`
- `TRANSFER`
- `MANAGE_ROBOT_POWER`
- `SETUP_MACHINE`
- `LOAD_MACHINE`
- `UNLOAD_MACHINE`
- `LOAD_UNLOAD_TRANSFER_INTERFACE`
- `INSPECT_PRODUCT`
- `REPAIR_MACHINE`
- `PREVENTIVE_MAINTENANCE`
- `INSPECT_MACHINE`
- `HANDOVER_ITEM`
- `COLLECT_WASTE_OR_SCRAP`
- `UPDATE_INVENTORY_RECORD`
- `OPERATE_VEHICLE_TRANSPORT`
- `WELD_SEAM`
- `PREPARE_SURFACE`
- `PAINT_SURFACE`
- `VERIFY_SHIP_SECTION`

## Task Candidate Generation Conditions

Task 정의와 hierarchy는 HumanoidSim이 소유하지만, ManSim에서 **언제 task opportunity가 생기는지**는 factory world state가 결정합니다. 핵심 구현 위치는 `manufacturing_sim/simulation/scenarios/manufacturing/world.py`의 `_candidate_tasks(agent)`이며, rolling horizon mode에서는 같은 후보를 window pool에 모았다가 dispatch합니다.

아래 표는 현재 ManSim world가 생성하는 주요 HumanoidSim task 후보와 발생 조건입니다.

| Task code | World trigger condition |
| --- | --- |
| `REPLENISH_MATERIAL` | `factory_mfg_basic`에서는 Station material queue가 configured target보다 적을 때 generic station request로 생성됩니다. `mfg_flow_shop`에는 목표재고가 없고 `material input이 비어 있는 설비 수 - queue item - inbound reservation`만큼 concrete 후보가 생성됩니다. 각 후보는 서로 다른 warehouse material instance, shelf slot과 destination buffer slot을 사용하므로 병렬 설비 수요를 중복 없이 채웁니다. |
| `TRANSFER` | Station output buffer에 다음 위치로 옮길 item이 있을 때 생성됩니다. Station 1/2 output은 다음 queue로, inspection output은 `completed_product_buffer`로 이동합니다. Battery delivery도 실행 task code는 `TRANSFER`이며 payload의 `transfer_kind=battery_delivery`로 구분합니다. |
| `MANAGE_ROBOT_POWER` | Worker의 battery remaining이 configured threshold 이하일 때 battery service 후보로 생성됩니다. `mfg_flow_shop`에서는 자신의 전용 dock로 이동해 충전하며, 추정 battery margin이 음수인 생산 task도 제거하지 않고 충전과 함께 정책 선택지로 노출합니다. |
| `LOAD_MACHINE` | Machine이 `WAIT_INPUT`이고 broken/processing 상태가 아니며 setup owner가 없고, 필요한 material 또는 intermediate input slot이 비어 있으며 해당 source queue에 item이 있을 때 생성됩니다. 후보에는 load slot과 concrete queue item id가 포함됩니다. |
| `SETUP_MACHINE` | Machine에 필요한 모든 input이 이미 적재되어 있고 `setup_ready=false`일 때 생성됩니다. Worker는 machine service tile에서 fixture, recipe, program 준비를 수행하며 item을 운반하지 않습니다. |
| `UNLOAD_MACHINE` | Machine에 `output_intermediate`가 존재하고 unload owner가 없으며 destination output buffer에 점유되지 않았거나 예약되지 않은 slot이 있을 때 생성됩니다. Worker는 해당 slot을 예약한 뒤 machine output을 옮깁니다. |
| `LOAD_UNLOAD_TRANSFER_INTERFACE (load)` | Inspection desk가 `EMPTY`이고 input queue에 예약되지 않은 product가 있을 때 생성됩니다. 특정 product를 queue에서 집어 단일 desk에 배치합니다. |
| `INSPECT_PRODUCT` | Desk가 `STAGED_FOR_INSPECTION`이고 아직 판정되지 않은 product가 있을 때만 생성됩니다. Item 운반 없이 desk에서 검사, 판정, 기록만 수행합니다. |
| `LOAD_UNLOAD_TRANSFER_INTERFACE (unload)` | Desk가 `INSPECTED_WAITING_UNLOAD`이고 PASS/FAIL 결과가 기록되었을 때 생성됩니다. PASS는 inspection output queue, FAIL은 inspection scrap queue로 이동합니다. |
| `REPAIR_MACHINE` | Machine이 broken이고, 해당 worker가 repair team에 아직 없으며, repair team capacity가 남아 있을 때 생성됩니다. `mfg_flow_shop`에서는 shared/dedicated 여부와 관계없이 모든 worker가 capacity 안에서 공동수리에 참여할 수 있습니다. 후보에는 station 가용 능력, 정지 비율, input/WIP 수요, machine 내 진행 WIP로 계산한 urgency가 포함됩니다. |
| `PREVENTIVE_MAINTENANCE` | `mfg_flow_shop`에서 마지막 PM 이후 실제 가공시간이 `due_processing_min` 이상이고, machine이 broken/repair/processing 상태가 아니며 output item이 없고 pm owner가 없을 때 생성됩니다. |
| `HANDOVER_ITEM` | Product 공동 운반 session이 active이고 carrier가 max보다 적으며, 후보 worker가 아직 carrier가 아니고 source carrier와 남은 path가 유효할 때 생성됩니다. ManSim의 task type은 `HANDOVER_ITEM`로 유지되지만 HumanoidSim 실행 task code는 robot-robot protocol인 `HANDOVER_ITEM_TO_ROBOT`에 바인딩됩니다. Dedicated roles mode에서는 협업을 배제하기 위해 pool에 넣지 않습니다. |
| `COLLECT_WASTE_OR_SCRAP` | Inspection scrap queue에 scrap item이 있고 scrap disposal owner가 없을 때 생성됩니다. Worker는 `quality.scrap_transport.max_carry_count` 이하의 batch를 `scrap_disposal_bin`으로 운반합니다. |

World는 같은 concrete item, material shelf slot, material supply station, machine resource가 동시에 여러 unresolved opportunity에 중복으로 잡히지 않도록 item/resource signature를 사용합니다. Rolling horizon mode에서는 이 signature가 `opportunity_id`와 exclusive resource key로 저장되어, 이미 pool 또는 dispatch queue에 있는 같은 자원을 다시 배정하지 않습니다.

## Domain Rules

### Inspection

Inspection workbench는 세 top-level task가 공유하는 capacity-1 exclusive resource입니다.

- Desk lifecycle은 `EMPTY -> STAGED_FOR_INSPECTION -> INSPECTING -> INSPECTED_WAITING_UNLOAD -> EMPTY`입니다.
- `LOAD_UNLOAD_TRANSFER_INTERFACE (load)`가 input queue의 concrete product를 예약하고 desk에 배치합니다.
- `INSPECT_PRODUCT`는 desk로 이동한 뒤 `PRIMITIVE_IDENTIFY_ITEM -> LOCALIZE_OBJECT -> EXECUTE_QUALITY_ACTION -> CLASSIFY_RESULT -> RECORD_RESULT`를 수행하며 item을 운반하지 않습니다.
- PASS/FAIL은 product metadata에 한 번만 기록되며 task 중단이나 재시도에서 다시 추첨하지 않습니다.
- `LOAD_UNLOAD_TRANSFER_INTERFACE (unload)`가 PASS item을 inspection output queue로, FAIL item을 inspection scrap queue로 옮긴 뒤 desk를 비웁니다.
- Load, inspect, unload는 서로 다른 worker에게 배정될 수 있지만 동시에 실행될 수는 없습니다.
- Load 또는 inspect 단계가 끝난 worker는 단일 desk service tile을 점유한 채 대기하지 않고, `inspection_staging`까지 실제 tile 경로로 이동해 다음 단계 worker의 접근 공간을 비웁니다. 이 이동은 `AGENT_MOVE_*`와 `INSPECTION_WORKSTATION_VACATED` event로 기록되어 순간이동과 교착을 audit합니다.
- `COLLECT_WASTE_OR_SCRAP`가 scrap batch를 `scrap_disposal_bin`까지 운반하면 `disposed_scrap_count`가 증가합니다.
- Accepted product가 `completed_product_buffer`에 도착하면 `total_products`가 증가합니다.

### Warehouse Material Shelf

Warehouse material은 공유 shelf slot에 놓입니다.

- capacity: `warehouse.material_shelf.capacity`
- 초기 채움: `warehouse.material_shelf.initial_fill`
- `factory_mfg_basic` restock: `warehouse.material_shelf.restock_policy: day_boundary`
- `mfg_flow_shop` restock: `objective.mode` controls whether replenishment is disabled or performed at configured day boundaries.
- Worker는 material slot service tile까지 이동해야 pickup할 수 있습니다.
- 같은 material item, 같은 shelf slot, 또는 같은 station material replenishment를 대상으로 하는 unresolved task opportunity는 rolling pool에 중복으로 들어갈 수 없습니다.
- `REPLENISH_MATERIAL` 후보는 generic request로 생성됩니다. Rolling pool에서는 `station2 / any material from Warehouse`처럼 보이며, worker가 실행 중 warehouse shelf를 스캔해 concrete material id와 slot을 선택합니다.

### Flow-shop Objective And Termination

`mfg_flow_shop`의 `objective.mode`는 run의 material 공급과 종료조건을 함께 결정합니다.

- `maximize_throughput`은 기본값입니다. 시각 0의 initial fill 이후 매 `objective.throughput.restock_interval_days` 경계에서 shelf를 `restock_target_fill`까지 보충하고 `horizon.num_days`가 끝나면 종료합니다.
- `minimize_makespan`은 시각 0에 생성된 warehouse material ID만 고정 batch로 등록하며 이후 material을 보충하지 않습니다. `horizon.num_days` 대신 `objective.makespan.max_sim_days`를 안전 상한으로 사용합니다.
- Makespan 완료 여부는 product 개수가 아니라 `source_material_ids` 계보로 판단합니다. 초기 material이 `CompletedProducts`에 도착한 양품 product 또는 `ScrapDisposal`에 실제 폐기된 product에 모두 포함되면 `initial_material_batch_terminal_complete`로 즉시 종료합니다.
- 검사 실패 후 scrap queue에 남아 있는 item은 아직 terminal이 아닙니다. `COLLECT_WASTE_OR_SCRAP`가 폐기를 마쳐야 완료에 포함됩니다.
- 현재 2-stage recipe는 최종 output 하나당 material 두 개를 사용하므로 initial fill은 2의 배수여야 하며, Makespan 모드에서 station initial inventory는 0이어야 합니다. 등록된 batch는 Station 1과 Station 2 몫으로 동일 분할되고, 각 `REPLENISH_MATERIAL`은 해당 station에 고정된 material만 선택합니다.
- 안전 상한까지 batch가 끝나지 않으면 `makespan_max_days_reached`와 incomplete 상태를 기록하고 `makespan_min`은 비워 둡니다.

```powershell
python main.py scenario=mfg_flow_shop scenario.objective.mode=minimize_makespan
python main.py scenario=mfg_flow_shop scenario.objective.mode=maximize_throughput scenario.horizon.num_days=10
```

### Flow-shop Representative Policies

공식 `mfg_flow_shop` 비교 mode는 `immediate_shared`, `immediate_dedicated_roles`, `rolling_horizon_shared`, `rolling_horizon_dedicated_roles` 네 개입니다. 네 mode는 동일한 fixed granular priority를 사용하고, rolling window 적용 여부와 세부 업무의 단일 owner 적용 여부만 다릅니다. Adaptive priority와 aging boost는 사용하지 않습니다.

각 decision YAML의 `mfg_flow_shop_policy.task_rules`는 번호가 부여된 19개 역할을 `task_code`와 payload selector로 구분합니다. 역할 1~16은 일반 생산 업무이고, 이 중 14~16은 inspection desk load, inspection, inspection desk unload입니다. 역할 17은 모든 worker의 자기 충전, 역할 18은 모든 worker가 참여 가능한 공동수리, 역할 19는 예방정비입니다. 예를 들어 `TRANSFER`는 `from_station`에 따라 S1-to-S2, S2-to-Inspection, Inspection-to-CompletedProducts로 나뉘고 `LOAD_MACHINE`은 station과 material/intermediate slot으로 나뉩니다. 역할 번호는 priority가 아니며 후보가 rule과 일치하지 않거나 여러 rule에 겹치면 실행 전에 설정 오류가 발생합니다.

Dedicated mode는 역할 1~16과 19의 objective별 예상 발생 횟수와 scenario timing/map 기반 예상 busy time을 계산한 뒤 deterministic LPT로 `owner: auto` rule을 한 worker에게만 배정합니다. `owner: A2`처럼 고정할 수도 있습니다. 역할 17 `MANAGE_ROBOT_POWER`와 역할 18 `REPAIR_MACHINE`은 LPT 대상이 아니며 모든 worker에게 공통 부여됩니다. 역할표와 예상 부하는 `run_meta.json`과 Pre-Run Diagnostics에 저장됩니다.

정책 비교에서 외생 불확실성이 dispatch 순서에 오염되지 않도록 품질 판정, machine별 고장, worker·incident code별 휴머노이드 incident는 서로 독립된 deterministic random stream을 사용합니다. stream namespace와 base seed는 `run_meta.json`의 `stochastic_streams`에 기록됩니다.

### Load / Setup / Unload

Machine input 준비는 item 적재와 setup을 분리해 표현합니다.

- `LOAD_MACHINE`: material 또는 intermediate queue에서 하나의 item을 집고 machine service tile로 이동해 해당 input slot에 적재합니다.
- `SETUP_MACHINE`: 모든 required input이 machine에 적재된 뒤 fixture, recipe, program 준비를 수행하고 `setup_ready=true`로 전환합니다.
- `UNLOAD_MACHINE`: machine output을 집고 station output buffer로 운반합니다.

Machine lifecycle은 required input이 모두 있고 `setup_ready=true`일 때만 processing을 시작합니다. 따라서 Station 2처럼 material과 intermediate가 모두 필요한 경우에도 두 input은 각각 별도의 `LOAD_MACHINE` task로 먼저 적재되고, 그 다음 `SETUP_MACHINE` task가 수행됩니다.

### Repair / Preventive Maintenance

Repair에는 여러 worker가 같은 machine에 합류할 수 있습니다. 동시 repair worker 수는 `machine_failure.max_repair_agents`가 제한합니다. `mfg_flow_shop`의 고장 clock은 달력시간이 아니라 machine이 실제로 가공한 시간만 누적하며, 평균 300 가공분의 지수분포 threshold를 machine별 독립 RNG로 샘플합니다. PM은 240 가공분마다 due가 되고 완료 후 다음 240 가공분의 hazard를 0.5배로 낮춥니다.

### Battery

Battery swap은 `MANAGE_ROBOT_POWER`로 표현합니다. 기존 factory/shipyard의 저전력 service는 strict-periodic 예외로 설정할 수 있지만, `mfg_flow_shop`은 `assignment_mode=policy_decides`를 사용해 충전을 강제 삽입하지 않습니다. 충전과 battery depletion risk가 있는 생산 task를 함께 후보로 노출하고 정책이 선택합니다.

`mfg_flow_shop`은 swap 대신 direct dock charging을 사용합니다. 내부 opportunity는 `BATTERY_CHARGE`, HumanoidSim binding은 `MANAGE_ROBOT_POWER`이며 args는 `action=dock_charge`, `station=charging_dock_<worker>`, `target_soc=1.0`입니다. Worker는 자기 도크로 tile 단위 이동하고 정확히 도크 tile에 도착한 뒤 충전합니다. 이동 또는 task 중 SOC가 0이 되면 현재 tile에 item을 내려놓고 resource/reservation을 해제한 뒤 `DISABLED`로 남습니다. 다음 day boundary에 외부 야간 복구를 나타내는 `WORKER_RETURNED_NEXT_DAY` event와 함께 자신의 dock에 SOC 100%로 복귀합니다.

Battery remaining은 worker별 budget으로 정산합니다. 기본 설정에서는 `availability=AVAILABLE`인 동안 `0.5`배 속도로 소모되고, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `DISABLED` 등 AVAILABLE이 아닌 상태에서는 `1.0`배 속도로 소모됩니다. 따라서 작업/이동/대기 중인 worker는 idle available 상태보다 2배 빠르게 배터리를 사용합니다. 배율은 scenario config의 `worker.battery_drain.available_rate_multiplier`와 `worker.battery_drain.non_available_rate_multiplier`에서 조정합니다.

### Product Handover

Product transport session이 active이고 carrier가 1명인 경우, 다른 available worker가 `HANDOVER_ITEM` 후보를 받을 수 있습니다. Helper가 합류하면 HumanoidSim의 `HANDOVER_ITEM_TO_ROBOT` sequence(`SYNC_WITH_ROBOT`, `EXECUTE_ROBOT_COLLABORATION_ACTION`)를 거쳐 다음 tile segment부터 product 이동 multiplier가 carrier 수로 나뉩니다.

## Movement And Traffic

Worker 이동은 tile map 기반입니다. `move_agent(agent, dst)`는 logical destination을 service tile 후보로 바꾸고 A* path를 따라 한 tile씩 이동합니다.

기본 traffic mode는 `strict_reservation`입니다. Worker가 다음 tile을 예약하지 못하면 이동하지 않고 `AGENT_TRAFFIC_CONFLICT`와 `TRAFFIC_WAIT` HumanoidSim incident를 기록한 뒤 recovery protocol을 실행합니다. `observe_conflicts` 모드는 충돌 가능 상황을 막지 않고 event/KPI/Replay overlay로 관찰하기 위한 실험 모드입니다.

## Rolling Horizon aging priority

`rolling_horizon_aging_priority`는 일반 생산 task 후보를 즉시 dispatch하지 않고 rolling window 동안 pool에 모은 뒤 dispatch합니다. 독립 SimPy coordinator가 worker polling과 무관하게 정확히 `t=5,10,15,...`에 동작하며, 시각 0에는 `[0,5]` window와 후보 pool만 생성합니다.

- 설정 파일: `configs/decision/rolling_horizon_aging_priority.yaml`
- window 기본값: `rolling_horizon.window_min: 5.0`
- priority 기준: `rolling_horizon.scenario_task_code_priority_order.<scenario>`
- priority 단위: ManSim task family가 아니라 HumanoidSim `task_code`
- dispatch policy: `aging_priority`

Task priority는 scenario별로 분리합니다. `factory_mfg_basic`은 제조 task 순서를, `shipyard_basic`은 조선소 surface tile 작업과 cart logistics task 순서를 사용합니다. 기존 `rolling_horizon.task_code_priority_order`는 이전 설정 파일을 위한 fallback입니다.

정렬식:

```text
effective_rank = base_rank - waited_window_count * rank_boost_per_window
```

낮은 rank가 먼저 dispatch됩니다. `PREVENTIVE_MAINTENANCE`처럼 base rank가 낮은 task도 오래 기다리면 effective rank가 개선되어 영구 starvation을 피합니다.

Window boundary에서는 먼저 아직 실행을 시작하지 않은 queued task를 pool로 회수하고, 전체 상태 스캔으로 event 수집 누락과 stale 후보를 보정한 다음 feasible task를 가능한 한 모두 worker dispatch queue에 배정합니다. 한 worker에게 여러 task가 queue될 수 있으며, worker는 queue의 앞에서부터 FIFO로 실행합니다. 실행 중 task는 회수·선점·재배정하지 않습니다. Machine failure는 즉시 pool에 보이며, `mfg_flow_shop`의 critical repair는 정규 경계 전에도 idle worker queue를 갱신할 수 있습니다. High/normal repair는 다음 경계에서 배정됩니다.

Rolling task는 처음 pool에 들어올 때 stable task id를 받습니다. 예를 들어 `REPLENISH_MATERIAL`은 `MAT-000001`, `TRANSFER`는 `TR-000002`, `REPAIR_MACHINE`은 `RM-000003` 같은 형식입니다. 이 id는 requeue/re-dispatch 이후에도 유지되며 Replay panel의 `Task` 값에도 함께 표시됩니다.

`rolling_horizon_dedicated_roles`는 같은 rolling window 구조를 씁니다. `factory_mfg_basic`과 `shipyard_basic`은 `rolling_horizon.scenario_worker_task_priority.<scenario>` task-code allowlist를 사용하고, `mfg_flow_shop`은 `mfg_flow_shop_policy.task_rules`의 granular LPT owner를 우선 적용합니다. 기존 `worker_task_priority`와 `task_code_priority_order`는 이전 설정 파일을 위한 fallback으로만 사용합니다.

## Event Logging

주요 event:

- `WORKER_STATE_CHANGED`
- `WORKER_CARGO_CHANGED`
- `HUMANOID_TASK_START`, `HUMANOID_TASK_END`
- `HUMANOID_STEP_START`, `HUMANOID_STEP_END`
- `AGENT_MOVE_START`, `AGENT_MOVE_TILE_START`, `AGENT_MOVE_TILE_END`, `AGENT_MOVE_TILE_CANCELLED`, `AGENT_MOVE_END`
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
- `SHIP_TILE_STATE_CHANGED`
- `CART_STATE_CHANGED`, `CART_ROUTE_MOVE`, `CART_SUPPLY_TRANSFER`

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
- `rolling_horizon.scheduler_mode`
- `rolling_horizon.candidate_collection_mode`
- `rolling_horizon.strict_boundary_count`
- `rolling_horizon.late_boundary_count`
- `rolling_horizon.max_boundary_lag_min`
- `throughput_optimizer_window_count`
- `throughput_optimizer_solved_count`
- `throughput_optimizer_failed_count`
- `throughput_optimizer_objective_avg`
- `bottleneck_score_avg`

Factory throughput policy modes:

- `bottleneck_aware_dispatch`는 rolling pool 없이 실행 가능한 task 후보를 즉시 scoring합니다. Score는 bottleneck relief, downstream progress, machine continuity에서 얻는 benefit에서 travel/execution time, resource risk, battery risk penalty를 뺀 값입니다. 선택된 task의 `selection_meta.score_components`에 계산 breakdown이 남습니다.
- `rolling_horizon_throughput_optimizer`는 기존 rolling horizon pool과 stable task id를 사용하지만 window dispatch를 OR-Tools CP-SAT로 풉니다. OR-Tools가 없거나 solver status가 설정된 `accept_statuses`에 없으면 fallback 없이 run을 실패시킵니다. Dispatch event에는 `optimizer_status`, `optimizer_objective`, `optimizer_score`, `sequence_position`, `score_components`가 포함됩니다.

Operational Task Complexity KPI:

- `operational_task_complexity` / `otc`
- `cumulative_operational_complexity_over_n_days`
- `operational_complexity_period_days`
- `operational_task_complexity_details`

Operational Task Complexity는 HumanoidSim primitive 정의의 `metadata.operational_complexity.difficulty_weight`를 사용합니다. HumanoidSim은 task를 primitive leaf step까지 전개해 `C_task(t)=sum_k a_tk*d_k`를 계산하고, ManSim은 run 중 완료된 top-level task instance 수 `N_t`를 곱해 `C_cum=sum_t N_t*C_task(t)`를 집계합니다. Hub의 `OTC`는 `C_cum / n_days`이며, pool에만 있었거나 dispatch 직전 skipped 된 task는 실행 부담으로 보지 않아 집계에서 제외합니다.

Factory Pre-Run Diagnostics:

- `pre_run_diagnostics.json`
- `pre_run_diagnostics.html`

이 진단은 simulation 결과를 사용하지 않고 factory scenario config, rolling-horizon role policy, HumanoidSim task complexity, tile map topology, service tile 수, battery 설정을 입력으로 사용합니다. 지표는 worker OTC imbalance, resource conflict potential, traffic contention index, service tile scarcity, robot interaction load, power coordination risk 여섯 개이며, Hub의 `Pre-Run Diagnostics` 메뉴에서 각 값과 계산 과정을 확인할 수 있습니다.

Traffic, transport, production, shelf/scrap KPI는 `kpi.json`에 함께 기록됩니다.

## Debugging Order

Factory behavior가 이상하면 아래 순서로 확인합니다.

1. `events.jsonl`
2. `minute_snapshots.json`
3. `kpi.json`
4. `daily_summary.json`
5. `replay_studio_log.json`

Replay Studio에서 이상해 보이면 먼저 `events.jsonl`의 core event가 같은 내용을 말하는지 확인합니다. Core event가 정상이고 Replay만 다르면 exporter/reducer/UI 문제일 가능성이 높습니다.
