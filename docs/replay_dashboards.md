# Replay And Dashboard Artifacts

ManSim은 정적 HTML dashboard와 Replay Studio용 JSON payload를 함께 export합니다. Replay와 dashboard는 simulation core가 남긴 artifact를 읽어 표시하는 관찰 계층이며, worker 위치나 state를 임의로 보정하지 않는 것을 원칙으로 합니다.

## Results Hub

`results_dashboard.html`은 run별 메인 진입점입니다. 내부적으로 `dashboard_manifest.json`을 사용하며, manifest에는 run metadata와 artifact path가 들어 있습니다.

Hub는 다음 view로 연결합니다.

- KPI dashboard
- Gantt chart
- task-priority dashboard
- factory Replay Studio 2D
- factory Replay Studio 3D
- OpenClaw workspace dashboard
- manager Replay Studio view
- LLM Wiki dashboard
- Graphify knowledge graph dashboard
- Series dashboard

## KPI Dashboard

`kpi_dashboard.html`은 생산, 설비, worker, worker collaboration, movement/traffic safety, incident, shelf/scrap 지표를 보여줍니다.

Worker Metrics는 `HumanoidSim` Availability State를 기준으로 합니다.

- `EXECUTING`: 정상 task/primitive 실행
- `ASSIGNED`: task를 받았지만 본격 실행 전
- `AVAILABLE`: 새 task 수락 가능
- `WAITING`: 예상 가능한 짧은 대기
- `BLOCKED`: 현재 task를 그대로 속행할 수 없는 예외
- `DISABLED` / `OFFLINE`: 운용 불가 또는 제외

State 차트는 `HumanoidSim` state schema의 모든 state를 포함합니다. 해당 run에서 발생하지 않은 state도 0으로 표시해, 상태가 없는 것과 집계 누락을 구분합니다.

Incident KPI는 `HumanoidSim` incident taxonomy를 기준으로 집계합니다.

- `humanoid_incident_total`
- `humanoid_incidents_by_code`
- `humanoid_incidents_by_category`
- `humanoid_incidents_by_worker`
- `humanoid_incident_recovery_protocol_by_code`

Worker collaboration KPI는 명시적 collaboration event만 사용합니다. 단순히 같은 장소에 있거나 경로가 가까운 것은 collaboration으로 보지 않습니다.

- `handover_item_count`
- `shared_product_carry_time_min`
- `shared_product_carry_ratio`
- `repair_helper_join_count`
- `repair_collaboration_time_min`
- `repair_collaboration_ratio`
- `repair_team_size_avg`

## Decision / Dispatch KPI

`rolling_horizon_aging_priority`와 `rolling_horizon_dedicated_roles` run에서는 Decision / Dispatch 영역에 rolling window 기반 dispatch 결과가 표시됩니다.

- `rolling_horizon_window_count`: rolling window start 수
- `rolling_horizon_candidate_collected_count`: 수집된 unique task opportunity 수
- `rolling_horizon_dispatched_task_count`: worker dispatch queue에 배정된 task 수
- `rolling_horizon_requeued_task_count`: window boundary에서 아직 시작하지 않아 pool로 되돌린 task 수
- `rolling_horizon_stale_skipped_task_count`: dispatch 직전 stale/resource-preempted 상태로 skip된 task 수
- `rolling_horizon.pending_candidate_count`: run 종료 시 unresolved pool task 수
- `rolling_horizon.max_worker_queue_length`: run 중 worker dispatch queue 최대 길이

`rolling_horizon_aging_priority`는 HumanoidSim task code rank와 waited window count만 사용합니다. 예상 processing time, bottleneck bonus, deadline bonus는 사용하지 않습니다.

`rolling_horizon_dedicated_roles`는 같은 event를 사용하되 `role_owner_agent_id`, `allowed_worker_ids`, `role_policy=dedicated_roles`를 함께 기록합니다. 3D Replay Studio의 Task Pool 패널은 rolling horizon 계열 mode에서 pool, dispatched, requeued, skipped 상태를 같은 stable task id 기준으로 보여줍니다.

## Factory Replay Studio 2D

2D Replay Studio는 다음 파일을 사용합니다.

- `replay_studio_log.json`
- `replay_studio_layout.json`

Replay는 event-sourced 방식입니다.

1. initial state
2. `timestamp`, `sequence_index`, `event_id` 기준 stable sort event stream
3. 선택 checkpoint

Renderer는 worker, machine, queue, battery station, inspection, material flow, movement, traffic conflict, incident, shared repair를 표시합니다. 이동 animation은 `AGENT_MOVE_START`에서 export된 `entity_moved.payload.path`와 durative window를 따릅니다. worker 사이를 임의 직선으로 연결하지 않습니다.

Worker monitor는 다음 정보를 표시합니다.

- `Task`: 현재 Humanoid task code
- `Child Task`: 현재 nested child task code
- `Primitive`: 현재 primitive call code
- `Motion Path`: 이동 중이면 path 길이와 현재 tile, 정지 중이면 `0 tiles`
- `Traffic`: 최근 traffic conflict type과 관련 worker
- `Incident`: 최근 incident category/code
- `Carry`: 현재 들고 있는 item ID와 type
- `Shared Carry`: product 공동 운반 session 정보

`Power`와 `Updated`는 worker monitor에서 제거했습니다. 필요한 원본 값은 `humanoid_state`와 cargo/motion payload에 보존됩니다.

## Replay Studio 3D

`replay_studio_3d/`는 기존 2D 앱과 직접 import 관계가 없는 독립 Vite + React + Three.js 앱입니다. Results Hub의 “Replay Studio 3D” 메뉴는 기본 포트 `5174`의 3D 앱으로 현재 run의 `replay_studio_log.json`을 전달합니다.

3D 표시 규칙:

- Worker는 procedural block humanoid model로 표시합니다.
- Worker 머리 위 label은 `A1`처럼 id만 표시합니다.
- Worker가 이동 중이면 다리 animation을 표시합니다.
- Worker가 정지 상태에서 task를 수행 중이면 팔 animation으로 작업 중임을 표시합니다.
- Worker가 cargo를 들고 있으면 단일 item과 batch cargo 모두 몸 앞쪽에 표시합니다.
- 선택 worker의 1인칭 시점은 map 내부 왼쪽 하단 PiP로 표시합니다.
- Worker 옆 battery bar는 map에서 제거했고, panel meter로 확인합니다.
- Queue에는 현재 item count를 `xN` 형태로 표시합니다.
- Input queue는 노랑, output/completed queue는 파랑, scrap queue는 분홍 계열로 표시합니다.
- Inspection table은 layout footprint를 우선 사용해 block table로 표시합니다.
- Shelf wall과 aisle 구조는 `grid.walls`와 object footprint를 기준으로 표시합니다.
- `rolling_horizon_aging_priority`와 `rolling_horizon_dedicated_roles` run에서는 3D scene 위쪽에 Task Pool 패널이 표시됩니다.

## Rolling Horizon Task Pool Panel

3D Replay의 Task Pool 패널은 다음 event를 읽습니다.

- `ROLLING_HORIZON_WINDOW_START`
- `ROLLING_HORIZON_CANDIDATE_COLLECTED`
- `ROLLING_HORIZON_DISPATCH`
- `ROLLING_HORIZON_TASK_REQUEUED`
- `ROLLING_HORIZON_TASK_SKIPPED`

표시 column:

- `Win`: opportunity가 속한 최신 window
- `ID`: stable task id
- `First`: 처음 수집된 시각
- `Updated`: status가 마지막으로 바뀐 시각
- `Task`: HumanoidSim task code
- `Target`: station, machine, material slot, item 등 target 요약
- `Seq`: assigned worker queue에서 실행될 순서. 현재 실행 중인 task는 `0`, 아직 worker가 정해지지 않은 pool/requeued/skipped task는 `-`
- `Worker`: dispatched worker
- `Status`: `pool`, `dispatched`, `requeued`, `skipped`

Unresolved pool task와 아직 시작하지 않은 dispatched task는 window가 넘어가도 같은 stable task id를 유지합니다. 다음 window에서 queued task가 아직 시작되지 않았다면 `requeued` event를 거쳐 pool로 돌아오고, 새 ranking으로 다시 dispatch될 수 있습니다. 이미 실행 중인 task는 requeue하지 않습니다.

Worker panel의 `Task`는 top-level task만 `TASK_CODE (TASK_ID)` 형식으로 표시합니다. 예: `REPLENISH_MATERIAL (MAT-000133)`. `Child Task`는 nested workflow 단계이므로 별도 전역 id를 붙이지 않고 code만 표시합니다.

Item panel은 실제 item entity 중 `material`, `intermediate`, `product`, `battery`만 표시합니다. Queue, shelf slot, zone 같은 layout object는 Item 탭에 넣지 않습니다. `intermediate`와 `product`에는 `From Material`, `From Intermediate`, `Transformed From` 항목으로 어떤 item에서 변환되었는지 lineage를 표시합니다.

## Gantt

`gantt.html`은 worker lane과 machine lane을 보여줍니다. Worker lane의 status는 `HumanoidSim` Availability State를 그대로 사용합니다.

- `AVAILABLE`
- `ASSIGNED`
- `EXECUTING`
- `WAITING`
- `BLOCKED`
- `OFFLINE`
- `DISABLED`

이전의 `MOVING`, `WORKING`, `DISCHARGED` 같은 요약 상태는 worker Gantt status로 사용하지 않습니다. Product/item id는 Gantt resource lane에 올리지 않습니다.

## Manager Replay

Manager replay는 `openclaw_adaptive_priority`를 대상으로 하며, 하루 단위 manager pipeline을 보여줍니다.

1. Input Bundle
2. Strategist Decision
3. Compiled Policy
4. Factory Response
5. Reviewer Assessment
6. Next-Day Carry Forward

Compiler는 agent가 아니라 deterministic system stage입니다.

## LLM Wiki Dashboard

`llm_wiki_dashboard.html`은 Curator가 만든 Obsidian-compatible vault 진입점입니다. Wiki 원본은 `knowledge/llm_knowledge/experiments/<id>/wiki/`에 있습니다.
