# ManSim Docs

이 디렉터리는 ManSim의 simulation core, Humanoid runtime, movement model, decision mode, Replay Studio, KPI dashboard, LLM Wiki, OpenClaw manager path 문서를 담고 있습니다.

## 현재 기준

- 기본 simulation path: `rolling_horizon_dedicated_roles`
- 기본 horizon: 5일
- worker runtime: `HumanoidSim`의 `TaskSpec -> child Task -> Primitive` hierarchy
- worker state: `HumanoidSim`의 `HumanoidStateSnapshot`
- movement reservation: `movement.traffic.mode=strict_reservation`
- worker initial placement: Warehouse와 Station 2 사이 복도
- primary objective: completed products 최대화
- completed product 기준: accepted product가 `CompletedProducts` zone의 `completed_product_buffer`에 dropoff될 때 증가
- scrap 기준: inspection fail은 `scrap_count`, ScrapDisposal 운반 완료는 `disposed_scrap_count`
- optional LLM manager path: `openclaw_adaptive_priority`
- deterministic rolling path: `rolling_horizon_aging_priority`
- dedicated no-collaboration rolling path: `rolling_horizon_dedicated_roles`

`task_type`은 기존 decision layer 호환용 priority family label입니다. 실제 hierarchy 실행, Replay 표시, KPI 집계는 `task_code`, child task path, `step_id`, `primitive_call_code`, `humanoid_state`를 기준으로 봅니다.

## 추천 읽기 순서

1. [simulator_core_guide.md](simulator_core_guide.md) - factory simulator core 구조, task 후보 발생 조건, runtime boundary, artifact 흐름
2. [humanoid_worker_model.md](humanoid_worker_model.md) - Humanoid state, task, child task, primitive, incident 적용 방식
3. [humanoid_movement_model.md](humanoid_movement_model.md) - tile pathfinding, strict reservation, movement events, Replay 표시 방식
4. [decision_logic.md](decision_logic.md) - decision mode, rolling horizon aging priority, manager boundary
5. [replay_dashboards.md](replay_dashboards.md) - results hub, KPI dashboard, Gantt, 2D/3D Replay Studio
6. [llm_wiki_curator.md](llm_wiki_curator.md) - Curator, Obsidian vault, Graphify graph
7. [openclaw_adaptive_priority_call_flow.md](openclaw_adaptive_priority_call_flow.md) - OpenClaw manager loop

## Humanoid 기준

ManSim은 worker의 State/Task 정의를 자체 enum으로 새로 만들지 않습니다. Worker는 Humanoid robot이고 상태는 네 축으로 기록합니다.

- `availability`: `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED`
- `mobility`: `STATIONARY`, `NAVIGATING`, `DOCKING`
- `power`: `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING`
- `manipulation`: `FREE`, `REACHING`, `HOLDING`, `PLACING`

Task는 목표 작업이고 Primitive는 Task를 구성하는 실행 단계입니다. `COMPOSITE_TASK`는 최소 하나 이상의 child task call을 포함하는 workflow입니다.

## Incident 기준

Humanoid incident taxonomy는 `HumanoidSim`이 소유합니다. ManSim은 factory scenario에서 incident 발생 조건과 확률만 정의합니다.

- Random incident: `OBJECT_RECOGNITION_FAILED`, `GRIP_FAILED`, `ITEM_DROPPED`, `UNKNOWN`
- Natural incident: `RESOURCE_PREEMPTED`, `RESOURCE_MISSING`, `TRAFFIC_WAIT`, `PATH_BLOCKED`, `NEAR_MISS`, `COLLISION`
- Incident code는 uppercase canonical code를 사용합니다.
- Recovery protocol 진행 중인 worker는 availability를 `BLOCKED`로 유지합니다.
- 현재 recovery step은 Task 또는 Primitive 칸에 `CODE (RECOVERY)`로 표시합니다.
- KPI는 code/category/worker별 incident count와 recovery protocol metadata를 집계합니다.

HumanoidSim 기준 문서는 `C:\Github\HumanoidSim\docs\incident_reference.md`를 참고합니다.

## Zone / Inventory 기준

현재 factory map에는 기존 Warehouse, Station, Inspection, BatteryStation 외에 다음 zone이 있습니다.

- `CompletedProducts`: accepted product 최종 dropoff zone
- `ScrapDisposal`: inspection fail product 최종 disposal zone

Warehouse material은 `warehouse_material_shelf`의 개별 slot 재고로 관리합니다. Day boundary마다 빈 slot만 capacity까지 채웁니다. Inspection fail product는 `inspection_scrap_queue`를 거쳐 `COLLECT_WASTE_OR_SCRAP` task로 batch disposal됩니다.

## Replay / Dashboard 기준

- 2D Replay Studio는 simulation artifact를 보정 없이 표시하는 것을 원칙으로 합니다.
- 3D Replay Studio는 `replay_studio_3d/`에 분리된 실험 앱입니다.
- Gantt worker lane은 `HumanoidSim` Availability State를 그대로 사용합니다.
- KPI dashboard는 state, task, primitive, task taxonomy, traffic, collaboration, incident, shelf/scrap을 표시합니다.
- `rolling_horizon_aging_priority`와 `rolling_horizon_dedicated_roles` run에서는 3D Replay map 위쪽에 Task Pool 패널이 표시됩니다.
- Task Pool 패널은 stable task id, window, rank, worker queue 배정 상태, requeue/skipped 상태를 같은 행으로 추적합니다.

## Artifact 감사

Run을 검증할 때는 아래 명령을 기본 체크로 사용합니다.

```powershell
.\.venv\Scripts\python.exe scripts\audit_run_artifacts.py outputs\YYYY-MM-DD\HH-MM-SS
```

주요 확인 항목은 hub/KPI/Gantt/Replay artifact 존재, humanoid state와 incident 집계, Gantt lane 구성, Replay worker state 보존, traffic conflict 자기참조 여부, rolling horizon pool 중복 여부입니다.
