# ManSim Docs

이 디렉터리는 ManSim의 simulation core, Humanoid runtime, movement model, decision mode, Replay Studio, KPI dashboard, LLM Wiki, OpenClaw manager path 문서를 담고 있습니다.

## 현재 기준

- 기본 simulation path: `adaptive_priority`
- 기본 horizon: 5일
- worker runtime: `HumanoidSim`의 `TaskSpec -> nested child Task -> Primitive` hierarchy
- worker state: `HumanoidSim`의 `HumanoidStateSnapshot`
- movement reservation: `movement.traffic.mode=strict_reservation`
- primary objective: `completed products` 최대화
- completed product 기준: accepted product가 `CompletedProducts` zone의 `completed_product_buffer`에 dropoff될 때 증가
- scrap 기준: inspection fail은 `scrap_count`, `ScrapDisposal` 운반 완료는 `disposed_scrap_count`
- optional LLM manager path: `openclaw_adaptive_priority`

`task_type`은 기존 decision layer 호환용 priority family label입니다. 실제 hierarchy 실행, Replay 표시, 신규 KPI 집계는 `task_code`, child task path, `step_id`, `primitive_call_code`, `humanoid_state`를 기준으로 봅니다.

## 추천 읽기 순서

1. [simulator_core_guide.md](simulator_core_guide.md) - factory simulator core 구조, runtime boundary, artifact 흐름
2. [humanoid_worker_model.md](humanoid_worker_model.md) - Humanoid state, task, child task, primitive, incident 적용 방식
3. [humanoid_movement_model.md](humanoid_movement_model.md) - tile pathfinding, strict reservation, movement events, Replay 표시 방식
4. [decision_logic.md](decision_logic.md) - decision mode, 성공 기준, manager boundary
5. [replay_dashboards.md](replay_dashboards.md) - results hub, Replay Studio, dashboard artifact
6. [llm_wiki_curator.md](llm_wiki_curator.md) - Curator, Obsidian vault, Graphify graph
7. [openclaw_adaptive_priority_call_flow.md](openclaw_adaptive_priority_call_flow.md) - OpenClaw manager loop

## Humanoid 기준

ManSim은 worker의 State/Task 정의를 자체 enum으로 새로 만들지 않습니다. Worker는 Humanoid robot이고, 상태는 네 축으로 기록합니다.

- `availability`: `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED`
- `mobility`: `STATIONARY`, `NAVIGATING`, `DOCKING`
- `power`: `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING`
- `manipulation`: `FREE`, `REACHING`, `HOLDING`, `PLACING`

Task는 목표 작업이고 Primitive는 Task를 구성하는 실행 단계입니다. `COMPOSITE_TASK`는 최소 하나 이상의 child task call을 포함하는 workflow입니다. 예를 들어 `REPLENISH_MATERIAL` 안에는 `TRANSFER [ATOMIC_TASK]`가 들어가고, `SETUP_MACHINE` 안에는 `LOAD_MACHINE [ATOMIC_TASK]`가 들어갑니다.

ManSim에서 적용되는 Humanoid task/state/incident 관계는 [humanoid_worker_model.md](humanoid_worker_model.md)에 정리되어 있습니다.

## Incident 기준

Humanoid incident taxonomy는 `HumanoidSim`이 소유합니다. ManSim은 factory scenario에서 incident 발생 조건과 확률만 정의합니다.

- Random incident: `OBJECT_RECOGNITION_FAILED`, `GRIP_FAILED`, `ITEM_DROPPED`, `UNKNOWN`
- Natural incident: `RESOURCE_PREEMPTED`, `RESOURCE_MISSING`, `TRAFFIC_WAIT`, `NEAR_MISS`, `COLLISION`
- Incident code는 uppercase canonical code를 사용합니다.
- Recovery protocol이 진행 중인 worker는 availability를 `BLOCKED`로 유지합니다. 현재 recovery step은 Task 또는 Primitive 칸에 `CODE (RECOVERY)`로 표시합니다.
- Replay Studio 말풍선은 incident code 전체가 아니라 Availability badge를 우선 표시합니다. 예: `BLK`, `WAIT`, `DIS`, `OFF`
- Worker panel에는 incident category/code를 표시합니다. Recovery protocol 전체 목록은 표시하지 않고, 현재 실행 중인 recovery step만 기존 Task 또는 Primitive 칸에 `CODE (RECOVERY)`로 표시합니다.
- KPI는 code/category/worker별 incident count와 recovery protocol metadata를 집계합니다.

HumanoidSim 쪽 기준 문서는 `C:\Github\HumanoidSim\docs\incident_reference.md`를 참고합니다.

## Zone / Inventory 확장

현재 factory map에는 기존 Warehouse, Station, Inspection, BatteryStation 외에 다음 zone이 있습니다.

- `CompletedProducts`: Warehouse 오른쪽, Inspection 위쪽. `completed_product_buffer`가 있으며 accepted product 최종 count가 여기서 증가합니다.
- `ScrapDisposal`: Inspection 아래, BatteryStation 오른쪽. `scrap_disposal_bin`이 있으며 scrap disposal count가 여기서 증가합니다.

Warehouse material은 `warehouse_material_shelf`의 개별 slot 재고로 관리됩니다. 기본 capacity는 10이고 day boundary마다 빈 slot만 채워집니다. Inspection fail product는 `inspection_scrap_queue`를 거쳐 `COLLECT_WASTE_OR_SCRAP` task로 batch disposal됩니다.

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

KPI dashboard의 Worker Collaboration 섹션은 product 공동 운반, handover, repair helper 합류, repair team size를 명시적 event 기준으로 집계합니다.

Gantt worker status는 `HumanoidSim` Availability State를 그대로 사용합니다. `MOVING`, `WORKING`, `DISCHARGED` 같은 legacy bucket은 worker Gantt status로 쓰지 않습니다.

## Artifact 감사 기준

새 run을 검증할 때는 아래 명령을 기본 체크로 사용합니다.

```powershell
.\.venv\Scripts\python.exe scripts\audit_run_artifacts.py outputs\YYYY-MM-DD\HH-MM-SS
```

감사 대상은 다음과 같습니다.

- Results Hub, KPI, Gantt, Replay 2D/3D 입력 artifact 존재 여부
- `kpi.json`의 humanoid state, incident, collaboration, traffic, shelf/scrap 필수 집계
- `gantt_segments.csv`의 Worker/Machine lane 구분과 Availability State status
- product/item lane이 Gantt에 잘못 올라오지 않는지
- Replay worker event의 `humanoid_state` 보존 여부
- machine `WAIT_INPUT` 상태에서 완료품/준비품 overlay가 stale하게 남지 않는지
- traffic conflict가 같은 worker 자신과 매칭되지 않는지
- `completed_product_buffer`가 canonical completed-product object로 export되는지

이 스크립트는 UI를 열기 전 산출물 구조를 먼저 확인하기 위한 안전망입니다. 화면에서 이상한 장면이 보이면,
동일 run directory에 대해 감사 스크립트를 먼저 실행한 뒤 core event 문제인지 Replay reducer/export 문제인지 나눠 봅니다.

## 해석 기준

Series dashboard에서 가장 중요한 지표는 `completed products`입니다. Closure ratio, inspection backlog, incident count, lead time은 보조 지표입니다. Closure가 좋아도 completed products가 줄면 운영 관점의 개선으로 보지 않습니다.

기본 traffic mode는 `strict_reservation`입니다. Worker가 다음 tile 예약에 실패하면 이동하지 않고 `TRAFFIC_WAIT`을 기록하며 대기합니다. `observe_conflicts` 모드에서는 traffic conflict를 이동 차단 없이 관찰하며, `collision_count`가 증가해도 설정에 따라 worker가 자동으로 `BLOCKED`나 `DISABLED`가 되지는 않습니다.
