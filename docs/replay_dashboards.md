# Replay와 Dashboard Artifact

ManSim은 정적 HTML dashboard와 Replay Studio용 구조화 JSON payload를 함께 export합니다.

## Results Hub

`results_dashboard.html`은 run별 메인 진입점입니다. 내부적으로 `dashboard_manifest.json`을 사용하며, 이 manifest에는 run metadata와 artifact path가 들어 있습니다.

Hub는 아래 view로 연결됩니다.

- KPI와 results summary
- task-priority dashboard
- OpenClaw workspace dashboard
- factory Replay Studio view
- factory Replay Studio 3D view
- manager Replay Studio view
- LLM Wiki dashboard
- Graphify knowledge graph dashboard
- Series dashboard, multi-run일 때

## KPI Dashboard

`kpi_dashboard.html`은 생산, 설비, worker, worker collaboration, movement/traffic safety를 분리해서 보여줍니다.

Worker collaboration 섹션은 worker가 같은 작업을 실제로 함께 수행했는지 확인하기 위한 영역입니다. 단순히 같은 장소에 있거나 경로가 가까운 것만으로 협업으로 추정하지 않고, simulator가 남긴 명시적 event만 사용합니다.

Worker Metrics 섹션은 `HumanoidSim` Availability State를 기준으로 `EXECUTING`, `BLOCKED`, `DISABLED/OFFLINE` 비율을 분리해서 보여줍니다. `BLOCKED`는 예상치 못한 외부 요인이나 stale precondition 때문에 현재 task를 속행하기 어려운 시간입니다.

State 축별 차트와 `kpi.json`의 `humanoid_state_time_by_worker`, `humanoid_state_time_by_axis`, `humanoid_state_ratio_by_worker`는 `HumanoidSim` state schema에 정의된 모든 state를 포함합니다. 현재 run에서 발생하지 않은 state도 0으로 남겨, “상태가 없었던 것”과 “집계가 누락된 것”을 구분합니다.

- `HANDOVER_ITEM` / `PRODUCT_CARRY_JOINED`: product 공동 운반에 helper가 합류한 횟수입니다.
- `PRODUCT_CARRY_COMPLETED`: product 운반 완료 시점에 전체 product 운반 시간, 공동 운반 시간, worker/pair별 공동 운반 시간을 집계합니다.
- `MACHINE_REPAIR_START`, `MACHINE_REPAIR_HELPER_JOIN`, `MACHINE_REPAIR_HELPER_LEAVE`, `MACHINE_REPAIRED`: repair team size를 시간 구간으로 적분해서 solo repair와 collaboration repair를 나눕니다.

주요 collaboration KPI는 다음과 같습니다.

- `handover_item_count`: product 운반 세션에 helper가 합류한 횟수.
- `shared_product_carry_time_min`: 두 명이 함께 product를 운반한 시간.
- `shared_product_carry_ratio`: product 운반 시간 중 공동 운반 비율.
- `repair_helper_join_count`: 고장 수리에 helper가 합류한 횟수.
- `repair_collaboration_time_min`: 두 명 이상이 함께 수리한 시간.
- `repair_collaboration_ratio`: active repair time 중 team size가 2 이상인 비율.
- `repair_team_size_avg`: active repair 중 시간 가중 평균 팀 크기.

## Factory Replay Studio

Factory replay는 아래 파일을 사용합니다.

- `replay_studio_log.json`
- `replay_studio_layout.json`

로그는 event-sourced 방식이며 deterministic replay를 목표로 합니다. Factory replay는 기본적으로 strict mode로 export됩니다. 이 모드에서는 Replay Studio가 worker 위치를 임의 보정하거나 inspection workbench로 강제 이동시키지 않고, simulator가 남긴 tile/motion/event payload를 기준으로만 상태를 복원합니다.

1. initial state
2. `timestamp`, `sequence_index`, `event_id` 기준으로 stable sort된 event stream
3. 선택적 checkpoint

Renderer는 worker, machine, queue, battery station, inspection, material flow, movement, traffic conflict, incident, shared repair를 시각화합니다. 부드러운 이동 animation은 `AGENT_MOVE_START`에서 export된 `entity_moved.payload.path`와 `durative` window를 따른 표현이며, 위치 fallback이나 inspection 전용 강제 배치는 사용하지 않습니다.

`path_wait`처럼 대기 관찰을 위해 반복 export되는 motion은 `motion.paused=true`로 표시합니다. 이때 Replay Studio는 worker 위치를 현재 tile에 고정하고, 실제 이동 경로 보간에는 짧은 고정 path만 사용합니다. 계획된 전체 route는 `display_path`에 남겨 점선 overlay로만 표시합니다.

Traffic conflict는 simulator의 `AGENT_TRAFFIC_CONFLICT` event에서 생성됩니다. Replay Studio에서는 `traffic_conflict_detected` event로 변환되며, 현재 conflict tile 또는 edge를 overlay로 표시합니다. 이 표시는 정책 보정이 아니라 simulator가 기록한 이동 사건을 그대로 관찰하기 위한 것입니다.

Worker monitor는 compact 운영 패널입니다.

- `Task`: 현재 Humanoid task code를 표시합니다.
- `Child Task`: 현재 nested child task code를 표시합니다.
- `Primitive`: 현재 `HUMANOID_STEP_START`/`HUMANOID_STEP_END` 또는 `WORKER_STATE_CHANGED`에 기록된 primitive를 표시합니다. Simulation-side presentation hint는 더 이상 생성하지 않습니다.
- `Motion Path`: 이동 중이면 현재 motion payload의 path 길이를 tile 단위로 표시하고, 괄호 안에 현재 tile 좌표를 표시합니다. 정지 중이면 `0 tiles`로 표시합니다.
- `Traffic`: 최근 traffic conflict type과 상대 worker를 표시합니다.
- `Carry`: item image 대신 현재 들고 있는 item ID와 type을 표시합니다.
- `Shared Carry`: product 공동 운반 세션의 carrier 수와 역할을 표시합니다.

`Power`와 `Updated`는 worker monitor에서 제거했습니다. Worker의 배터리 상태는 상단 battery meter로 확인하고, 의미 상태는 `HumanoidStateSnapshot`의 네 축 중 현재 UI에 필요한 축만 표시합니다.

## Gantt

`gantt.html`은 worker lane과 machine lane을 함께 보여줍니다. Worker lane의 status와 색상은 `HumanoidSim`의 Availability State를 그대로 사용합니다.

- `AVAILABLE`
- `ASSIGNED`
- `EXECUTING`
- `WAITING`
- `BLOCKED`
- `OFFLINE`
- `DISABLED`

이전의 `MOVING`, `WORKING`, `DISCHARGED` bucket은 worker Gantt status로 사용하지 않습니다. 이동 여부는 Gantt hover의 `mobility`, `primitive_call_code`, 그리고 Replay Studio의 motion path에서 확인합니다. Machine lane은 기존처럼 `RUNNING`, `DOWN`, `FINISHED-WAIT-UNLOAD`를 표시합니다.

Gantt는 worker id가 `A1`, `A2`처럼 worker 형식인 lane만 Worker로 취급합니다. Product/item id는 Gantt resource lane에 올리지 않습니다. `ASSIGNED`는 `configs/humanoidsim/default.yaml`의 `task_lifecycle.assignment_min_duration`만큼 최소 체류 시간을 갖기 때문에 worker lane과 KPI에서 관측됩니다. Hover 창은 resource, status, task/primitive 또는 machine cycle, time range 정도만 표시합니다.

## Replay Studio 3D

`replay_studio_3d/`는 기존 2D 앱과 직접 import 관계가 없는 독립 Vite + React + Three.js 앱입니다. Results Hub의 “Replay Studio 3D” 메뉴는 기본 포트 `5174`의 3D 앱으로 현재 run의 `replay_studio_log.json`을 전달합니다.

3D 앱은 기존 replay schema v1.0을 읽고, factory layout과 object footprint를 block model로 표현합니다. Worker 위치는 2D 앱과 동일하게 simulator가 기록한 motion path와 durative window를 기준으로 보간합니다. 실패하거나 방향을 바꾸고 싶으면 `replay_studio_3d/` 폴더만 분리해서 제거할 수 있도록 기존 2D 앱과 코드를 공유하지 않습니다.

## Manager Replay

Manager replay는 아래 파일을 사용합니다.

- `manager_replay.json`

이 view는 `openclaw_adaptive_priority`를 대상으로 하며, 하루를 하나의 sequential pipeline으로 보여줍니다.

1. Input Bundle
2. Strategist Decision
3. Compiled Policy
4. Factory Response
5. Reviewer Assessment
6. Next-Day Carry Forward

Compiler는 agent가 아니라 deterministic system stage로 표시합니다. Strategist와 Reviewer는 manager decision phase로 유지합니다.

## LLM Wiki Dashboard

`llm_wiki_dashboard.html`은 Curator가 만든 Obsidian-compatible vault의 진입점입니다.

- Obsidian 앱이 설치되어 있고 vault가 등록되어 있으면 `obsidian://open?...` 링크로 앱을 엽니다.
- 앱 연결이 실패해도 browser preview로 `00_Index.md`와 주요 page link를 확인할 수 있습니다.
- Wiki 원본은 `knowledge/llm_knowledge/experiments/<id>/wiki/`에 있습니다.

Wiki는 raw JSON viewer가 아닙니다. Raw artifact는 `raw/`에 보관하고, wiki에는 반복 사용 가능한 운영관리 지식만 정리합니다.

## Knowledge Graph Dashboard

`knowledge_graph_dashboard.html`은 Graphify 결과를 hub 내부 dashboard로 보여줍니다.

기본 tab은 Network입니다.

- Network: node-link graph.
- Tree: hierarchy-oriented view.
- Communities: cluster/community view.
- Edges: relation table.
- Raw JSON: `graph.json` 원본 확인.

Graph 원본은 `knowledge/llm_knowledge/experiments/<id>/graph/` 아래에 저장됩니다.

## Shared Repair 시각화

협동 수리 event는 아래 형태로 export됩니다.

- `MACHINE_REPAIR_START`
- `MACHINE_REPAIR_HELPER_JOIN`
- `MACHINE_REPAIR_HELPER_LEAVE`
- `MACHINE_REPAIRED`

Replay Studio는 repair team size, repair progress, machine 주변에 배치된 참여 worker를 표시합니다.

## Series Dashboard

Multi-run 실행이면 parent output directory에 `series_dashboard.html`이 생성됩니다. 이 dashboard는 completed products를 primary metric으로 사용합니다. Closure ratio와 backlog가 개선되어도 completed products가 감소하면 knowledge impact를 positive로 보지 않습니다.

## Replay Studio Asset 재생성

기존 run directory에서 Replay Studio 입력을 다시 만들려면 아래 명령을 사용합니다.

```powershell
.\.venv\Scripts\python.exe replay_studio\examples\export_mansim_run.py `
  --run-dir outputs\YYYY-MM-DD\HH-MM-SS `
  --output-log outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_log.json `
  --output-layout outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_layout.json
```

## Artifact Audit

Replay Studio, KPI, Gantt를 함께 확인할 때는 run directory 단위 감사 스크립트를 사용합니다.

```powershell
.\.venv\Scripts\python.exe scripts\audit_run_artifacts.py outputs\YYYY-MM-DD\HH-MM-SS
```

감사 스크립트는 다음 항목을 자동으로 확인합니다.

- 필수 dashboard/replay/KPI artifact가 모두 생성됐는지
- `kpi.json`에 humanoid state, incident, collaboration, traffic, warehouse shelf, scrap metric이 들어 있는지
- Gantt가 Worker/Machine lane만 사용하고 worker status를 Availability State로만 표시하는지
- Gantt hover가 핵심 정보만 담고 payload 전체를 노출하지 않는지
- Replay log에서 worker state event가 `humanoid_state`를 보존하는지
- `WAIT_INPUT` machine 위에 stale item overlay가 남지 않는지
- traffic conflict가 같은 worker 자신과 연결되지 않는지
- 3D Replay가 legacy `warehouse_buffer` alias 대신 canonical `completed_product_buffer`를 쓰는지

화면에서 보이는 현상이 버그인지 애매할 때는 이 감사 결과를 먼저 봅니다. 감사가 통과하면 대부분은
렌더링/표현 문제이고, 감사가 실패하면 exporter 또는 simulation event 쪽을 먼저 확인합니다.

## 개발 검증

```powershell
cd replay_studio
npm run build
```

Replay log validator는 잘못된 entity reference를 거부합니다. Exporter는 빈 ref를 `null`로 쓰지 말고 필드 자체를 생략해야 합니다.

`replay_studio_log.json.metadata`에는 `replay_mode: strict`, `position_policy: simulation_tile_or_motion_only`, `visual_corrections: false`가 기록됩니다.
