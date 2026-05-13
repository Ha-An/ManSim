# Humanoid Movement Model

이 문서는 ManSim에서 휴머노이드 worker가 목적지까지 이동하는 방식과 traffic reservation 동작을 설명합니다. State/Task 정의는 `Humanoid_Tasks`에서 가져오고, ManSim은 실제 factory scenario에서 이동 경로를 계산하고 이벤트로 기록합니다.

Humanoid State/Task/Primitive 전체 설명은 [humanoid_worker_model.md](humanoid_worker_model.md)를 보세요.

## 기본 구조

현재 기본 이동 모델은 tile map 기반입니다.

- map 구현: `manufacturing_sim/simulation/scenarios/manufacturing/grid_map.py`
- 이동 실행: `manufacturing_sim/simulation/scenarios/manufacturing/world.py`
- traffic 감지/예약: `manufacturing_sim/simulation/scenarios/manufacturing/traffic.py`
- 기본 설정: `configs/scenario/mfg_basic.yaml`

`movement.warehouse_to_station_min` 같은 zone 기반 이동 시간은 tile map이 꺼졌을 때 쓰는 fallback 값입니다. 기본값은 `map.enabled=true`이므로 실제 worker 이동은 tile path를 따릅니다.

## Destination Tile

이동 요청은 `move_agent(agent, dst)`로 시작합니다. `dst`는 `"S1M1"`, `"material_queue_1"`, `"inspection_table"`, `"Warehouse"` 같은 logical destination입니다.

`TileGridMap.destination_tiles()`는 destination별 후보 tile을 만듭니다.

- machine, queue, buffer, charger: object 주변 service tile.
- `inspection_table`: table 중앙 service tile. Inspection은 worker가 이 tile에 도착한 뒤에만 수행됩니다.
- zone: zone 중앙 근처 passable tile.
- 다른 worker ID: 해당 worker 주변 인접 tile.

후보 tile은 현재 위치에서 가까운 순서로 정렬됩니다.

## Pathfinding

경로계획은 `TileGridMap.find_path()`가 수행합니다.

- 알고리즘: A* search.
- 이동 방향: 상, 하, 좌, 우 4방향.
- diagonal 이동 없음.
- tile cost: 1.
- heuristic: Manhattan distance.
- wall과 blocking object는 통과 불가.
- 목적지 후보 중 도달 가능한 첫 goal까지의 path를 반환합니다.

반환 path는 현재 tile을 포함합니다.

```text
current tile -> next tile -> ... -> destination service tile
```

이 path는 `AGENT_MOVE_START.details.path_tiles`에 기록되고 Replay Studio의 movement overlay와 smooth interpolation에 사용됩니다.

## Tile Step Movement

Worker는 path 전체를 한 번에 순간이동하지 않습니다. `_move_agent_grid()`가 path의 다음 tile인 `path[1]`로 한 칸씩 이동합니다.

- 한 tile 이동 시간: `map.tile_time_min`
- 현재 기본값: `0.1`분
- item을 들고 있으면 tile segment 시간이 item weight multiplier만큼 길어집니다.
- 각 tile segment마다 `AGENT_MOVE_TILE_START`, `AGENT_MOVE_TILE_END`를 기록합니다.
- 전체 이동 시작/종료는 `AGENT_MOVE_START`, `AGENT_MOVE_END`로 기록합니다.

Replay Studio는 `AGENT_MOVE_START`에서 export된 `entity_moved.payload.path`와 `durative.started_at/ended_at`을 읽어 worker 위치를 보간합니다. 화면에서 보이는 부드러운 이동은 simulator가 기록한 tile path와 duration을 시각화한 것입니다.

## Item Weight And Shared Carry

빈손 이동이 기준 속도입니다. Worker가 item을 들고 이동하면 item type별 multiplier가 적용됩니다.

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

예를 들어 `map.tile_time_min=0.1`일 때 product를 혼자 들고 한 tile을 이동하면 `0.1 * 2.0 = 0.2`분이 걸립니다. 같은 product transport session에 helper가 `HANDOVER_ITEM`으로 합류하면 다음 tile segment부터 `0.1 * (2.0 / 2) = 0.1`분이 됩니다.

공동 운반은 현재 product에만 적용됩니다. Intermediate는 material보다 무겁지만 기본 구현에서는 공동 운반 대상이 아닙니다.

## Strict Reservation

기본 traffic mode는 `strict_reservation`입니다.

```yaml
movement:
  traffic:
    enabled: true
    mode: strict_reservation
    fidelity: tile_edge
    collision_effect: log_only
    near_miss_headway_min: 0.05
    emit_tile_step_events: true
```

`strict_reservation`에서는 worker가 다음 tile로 들어가기 전에 `grid.try_reserve(worker_id, next_tile)`을 호출합니다.

- 예약 성공: 한 tile 이동 시간만큼 진행한 뒤 worker occupancy를 next tile로 옮깁니다.
- 예약 실패: 이동하지 않고 `map.tile_time_min`만큼 대기합니다.
- 예약 실패 event: `AGENT_TRAFFIC_CONFLICT` with `conflict_type=TRAFFIC_WAIT`.
- 대기 중에도 worker state는 `HumanoidStateSnapshot`에 기록됩니다.

이 모드는 동적 충돌을 줄이는 보수적인 실행 방식입니다. 더 정교한 교차로 정책이나 우선순위 정책은 별도 traffic policy layer에서 확장할 수 있습니다.

## Observe Conflicts

`movement.traffic.mode=observe_conflicts`로 바꾸면 pathfinding과 tile 이동은 다른 worker의 동적 점유를 막지 않습니다.

- A*는 `ignore_dynamic=true`로 실행합니다.
- 다음 tile 예약 실패 때문에 이동을 막지 않습니다.
- `TrafficMonitor`가 동선 겹침과 실제 segment conflict를 event/KPI/Replay overlay로 기록합니다.

이 모드는 충돌 회피가 아니라 상황 재현과 관찰에 적합합니다.

기록되는 conflict type:

- `PATH_OVERLAP`: planned path가 같은 tile 또는 edge를 공유.
- `TILE_CONFLICT`: 같은 시간 구간에 같은 tile에 진입하거나 점유.
- `EDGE_CONFLICT`: 서로 반대 방향으로 같은 edge를 동시 통과.
- `NEAR_MISS`: 설정된 headway보다 짧은 간격으로 근접 통과.
- `COLLISION`: tile/edge conflict가 실제 이동 구간에서 겹침.
- `TRAFFIC_WAIT`: strict reservation에서 다음 tile 예약 실패로 대기.

## Replay 표시

Replay Studio는 시뮬레이션 위치를 임의로 보정하지 않습니다.

- Worker 위치: `entity_moved.payload.path`와 duration 기준 보간.
- Worker path overlay: 실제 tile path polyline.
- Traffic overlay: 현재 conflict tile 또는 edge.
- Worker panel의 `Motion Path`: 현재 active motion의 path tile 수.

Traffic conflict는 worker 사이 직선 연결로 표시하지 않고 tile/edge overlay로만 표시합니다. 그래서 실제 이동 경로와 충돌 지점이 섞여 보이지 않습니다.

## 설정 변경 예시

충돌 회피에 가까운 기본 실행:

```yaml
movement:
  traffic:
    mode: strict_reservation
```

충돌 가능 상황을 일부러 관찰:

```yaml
movement:
  traffic:
    mode: observe_conflicts
```

tile 이동 속도 조정:

```yaml
map:
  tile_time_min: 0.1
```

동적 점유 때문에 path가 오래 막힐 때 blocked event를 더 빨리 보고 싶다면:

```yaml
map:
  blocked_replan_threshold_min: 2.0
```
