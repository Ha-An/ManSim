# ManSim Docs

이 디렉터리는 ManSim의 simulation core, Humanoid runtime, movement model, decision mode, Replay Studio, KPI dashboard, LLM Wiki, OpenClaw manager path 문서를 담고 있습니다.

## 현재 기준

- 기본 simulation path: `adaptive_priority`
- 기본 horizon: 5일
- worker runtime: `HumanoidSim`의 `TaskSpec -> nested child Task -> Primitive` hierarchy
- worker state: `HumanoidSim`의 `HumanoidStateSnapshot`
- movement reservation: `movement.traffic.mode=strict_reservation`
- worker initial placement: Warehouse와 Station 2 사이 복도
- primary objective: completed products 최대화
- completed product 기준: accepted product가 `CompletedProducts` zone의 `completed_product_buffer`에 dropoff될 때 증가
- scrap 기준: inspection fail은 `scrap_count`, `ScrapDisposal` 운반 완료는 `disposed_scrap_count`
- optional LLM manager path: `openclaw_adaptive_priority`

`task_type`은 기존 decision layer 호환용 priority family label입니다. 실제 hierarchy 실행, Replay 표시, KPI 집계는 `task_code`, child task path, `step_id`, `primitive_call_code`, `humanoid_state`를 기준으로 봅니다.

## 추천 읽기 순서

1. [simulator_core_guide.md](simulator_core_guide.md) - factory simulator core 구조, runtime boundary, artifact 흐름
2. [humanoid_worker_model.md](humanoid_worker_model.md) - Humanoid state, task, child task, primitive, incident 적용 방식
3. [humanoid_movement_model.md](humanoid_movement_model.md) - tile pathfinding, strict reservation, movement events, Replay 표시 방식
4. [decision_logic.md](decision_logic.md) - decision mode, 성공 기준, manager boundary
5. [replay_dashboards.md](replay_dashboards.md) - results hub, KPI dashboard, Gantt, 2D/3D Replay Studio
6. [llm_wiki_curator.md](llm_wiki_curator.md) - Curator, Obsidian vault, Graphify graph
7. [openclaw_adaptive_priority_call_flow.md](openclaw_adaptive_priority_call_flow.md) - OpenClaw manager loop

## Humanoid 기준

ManSim은 worker의 State/Task 정의를 자체 enum으로 새로 만들지 않습니다. Worker는 Humanoid robot이고 상태는 네 축으로 기록합니다.

- `availability`: `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED`
- `mobility`: `STATIONARY`, `NAVIGATING`, `DOCKING`
- `power`: `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING`
- `manipulation`: `FREE`, `REACHING`, `HOLDING`, `PLACING`

Task는 목표 작업이고 Primitive는 Task를 구성하는 실행 단계입니다. `COMPOSITE_TASK`는 최소 하나 이상의 child task call을 포함하는 workflow입니다. 예를 들어 `REPLENISH_MATERIAL`에는 `TRANSFER [ATOMIC_TASK]`가 들어가고, `SETUP_MACHINE`에는 `LOAD_MACHINE [ATOMIC_TASK]`가 들어갑니다.

ManSim에서 적용되는 Humanoid task/state/incident 관계는 [humanoid_worker_model.md](humanoid_worker_model.md)에 정리되어 있습니다.

## Incident 기준

Humanoid incident taxonomy는 `HumanoidSim`이 소유합니다. ManSim은 factory scenario에서 incident 발생 조건과 확률만 정의합니다.

- Random incident: `OBJECT_RECOGNITION_FAILED`, `GRIP_FAILED`, `ITEM_DROPPED`, `UNKNOWN`
- Natural incident: `RESOURCE_PREEMPTED`, `RESOURCE_MISSING`, `TRAFFIC_WAIT`, `PATH_BLOCKED`, `NEAR_MISS`, `COLLISION`
- Incident code는 uppercase canonical code를 사용합니다.
- Recovery protocol 진행 중인 worker는 availability를 `BLOCKED`로 유지합니다.
- 현재 recovery step은 Task 또는 Primitive 칸에 `CODE (RECOVERY)`로 표시합니다.
- Replay Studio 말풍선은 incident code 전체가 아니라 Availability badge를 우선 표시합니다. 예: `BLK`, `WAIT`, `DIS`, `OFF`
- KPI는 code/category/worker별 incident count와 recovery protocol metadata를 집계합니다.
- Humanoid state KPI는 HumanoidSim state schema의 모든 state를 포함합니다. 현재 run에서 발생하지 않은 state도 0으로 남깁니다.

HumanoidSim 쪽 기준 문서는 `C:\Github\HumanoidSim\docs\incident_reference.md`를 참고합니다.

## Zone / Inventory 기준

현재 factory map에는 기존 Warehouse, Station, Inspection, BatteryStation 외에 다음 zone이 있습니다.

- `CompletedProducts`: Warehouse 오른쪽, Inspection 위쪽. `completed_product_buffer`가 있으며 accepted product 최종 count가 여기서 증가합니다.
- `ScrapDisposal`: Inspection 아래, BatteryStation 오른쪽. `scrap_disposal_bin`이 있으며 scrap disposal count가 여기서 증가합니다.

Warehouse material은 `warehouse_material_shelf`의 개별 slot 재고로 관리됩니다. Day boundary마다 빈 slot만 capacity까지 채웁니다. Inspection fail product는 `inspection_scrap_queue`를 거쳐 `COLLECT_WASTE_OR_SCRAP` task로 batch disposal됩니다.

## Replay / Dashboard 기준

- 2D Replay Studio는 simulation artifact를 보정 없이 표시합니다.
- 3D Replay Studio는 `replay_studio_3d/`에 분리된 실험 앱이며 기존 2D 앱과 직접 import 관계가 없습니다.
- 3D Replay는 worker, machine, queue, shelf, inspection table을 block model로 표시합니다.
- Worker 머리 위 label은 `A1`처럼 worker id만 표시합니다.
- 정지 상태에서 task를 수행하는 worker는 팔 움직임으로 작업 중임을 표현합니다.
- Worker가 cargo를 들고 있으면 단일 item과 batch cargo 모두 몸 앞쪽에 표시합니다.
- Input queue는 노랑, output/completed queue는 파랑, scrap queue는 붉은색 계열로 표시합니다.
- Gantt worker status는 `HumanoidSim` Availability State를 그대로 사용합니다. `MOVING`, `WORKING`, `DISCHARGED` 같은 예전 요약 상태는 worker Gantt status로 쓰지 않습니다.

## 주요 Artifact

Run directory:

- `results_dashboard.html` - run별 hub
- `dashboard_manifest.json` - dashboard artifact manifest
- `kpi.json` - KPI source data
- `kpi_dashboard.html` - KPI dashboard
- `gantt.html` - worker Availability State와 machine state timeline
- `gantt_segments.csv` - Gantt segment source data
- `daily_summary.json`
- `events.jsonl`
- `minute_snapshots.json`
- `replay_studio_log.json`
- `replay_studio_layout.json`
- `manager_replay.json`
- `llm_wiki_dashboard.html`
- `knowledge_graph_dashboard.html`

## Artifact 감사

Run을 검증할 때는 아래 명령을 기본 체크로 사용합니다.

```powershell
.\.venv\Scripts\python.exe scripts\audit_run_artifacts.py outputs\YYYY-MM-DD\HH-MM-SS
```

감사 대상은 다음과 같습니다.

- Results Hub, KPI, Gantt, Replay 2D/3D 입력 artifact 존재 여부
- `kpi.json`의 humanoid state, incident, collaboration, traffic, shelf/scrap 집계
- `gantt_segments.csv`의 Worker/Machine lane 구분과 Availability State status
- product/item lane이 Gantt에 잘못 올라오지 않는지
- Replay worker event가 `humanoid_state`를 보존하는지
- machine `WAIT_INPUT` 상태에서 stale item overlay가 남지 않는지
- traffic conflict가 같은 worker 자신과 연결되지 않는지
- `completed_product_buffer`가 canonical completed-product object로 export되는지

화면에서 보이는 현상이 버그인지 애매할 때는 이 감사 결과를 먼저 봅니다. 감사가 실패하면 exporter 또는 simulation event 쪽을 먼저 확인하고, 감사가 통과하면 renderer/UI 표현 문제를 우선 의심합니다.
