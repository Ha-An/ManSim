# ManSim Docs

이 디렉터리는 ManSim의 simulator core, Humanoid runtime, movement model, decision mode, Replay Studio, LLM Wiki, OpenClaw manager path 문서를 담고 있습니다.

## 현재 기준

- 기본 simulation path: `adaptive_priority`
- 기본 horizon: 5일
- worker runtime: `HumanoidSim`의 `TaskSpec -> nested child Task -> Primitive` hierarchy
- worker state: `HumanoidSim`의 `HumanoidStateSnapshot`
- movement reservation: `movement.traffic.mode=strict_reservation`
- primary objective: `completed products` 최대화
- optional LLM manager path: `openclaw_adaptive_priority`

`task_type`은 기존 decision layer 호환용 priority family label입니다. 실제 hierarchy 실행, Replay 표시, 신규 KPI 집계는 `task_code`, child task path, `step_id`, `primitive_call_code`, `humanoid_state`를 기준으로 봅니다.

## 추천 읽기 순서

1. [simulator_core_guide.md](simulator_core_guide.md) - factory simulator core 구조, runtime boundary, artifact 흐름.
2. [humanoid_worker_model.md](humanoid_worker_model.md) - Humanoid State, Task, child task, Primitive, ManSim 적용 방식.
3. [humanoid_movement_model.md](humanoid_movement_model.md) - tile pathfinding, strict reservation, movement events, Replay 표시 방식.
4. [decision_logic.md](decision_logic.md) - decision mode, 성공 기준, manager boundary.
5. [replay_dashboards.md](replay_dashboards.md) - results hub, Replay Studio, dashboard artifact.
6. [llm_wiki_curator.md](llm_wiki_curator.md) - Curator, Obsidian vault, Graphify graph.
7. [openclaw_adaptive_priority_call_flow.md](openclaw_adaptive_priority_call_flow.md) - OpenClaw manager loop.

## Humanoid 문서 기준

ManSim은 worker의 State/Task 정의를 자체 enum으로 새로 만들지 않습니다. Worker는 Humanoid 로봇이고, 상태는 네 축으로 기록합니다.

- `availability`: `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED`
- `mobility`: `STATIONARY`, `NAVIGATING`, `DOCKING`
- `power`: `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING`
- `manipulation`: `FREE`, `REACHING`, `HOLDING`, `PLACING`

Task는 목표 작업이고 Primitive는 Task를 구성하는 실행 단계입니다. `COMPOSITE_TASK`는 최소 하나 이상의 child task call을 포함하는 workflow입니다. 예를 들어 `REPLENISH_MATERIAL` 안에는 `TRANSFER [ATOMIC_TASK]`가 들어가고, `SETUP_MACHINE` 안에는 `LOAD_MACHINE [ATOMIC_TASK]`가 들어갑니다.

현재 ManSim에서 적용되는 모든 Humanoid task와 state 관계는 [humanoid_worker_model.md](humanoid_worker_model.md)에 정리되어 있습니다.

## 주요 Artifact

Run directory:

- `results_dashboard.html`
- `dashboard_manifest.json`
- `kpi.json`
- `daily_summary.json`
- `events.jsonl`
- `minute_snapshots.json`
- `replay_studio_log.json`
- `replay_studio_layout.json`
- `manager_replay.json`
- `llm_wiki_dashboard.html`
- `knowledge_graph_dashboard.html`

Run-series directory:

- `run_series_summary.json`
- `series_analysis.json`
- `series_dashboard.html`
- `run_01/`, `run_02/`, ...

Knowledge directory:

- `knowledge/llm_knowledge/experiments/<experiment-id>/raw/`
- `knowledge/llm_knowledge/experiments/<experiment-id>/wiki/`
- `knowledge/llm_knowledge/experiments/<experiment-id>/graph/`
- `knowledge/llm_knowledge/experiments/<experiment-id>/curator_trace/`

## 해석 기준

Series dashboard와 KPI에서 가장 중요한 지표는 `completed products`입니다. Closure ratio, inspection backlog, incident count, lead time은 보조 지표입니다. Closure가 좋아져도 completed products가 줄면 운영 관점의 개선으로 보지 않습니다.

기본 traffic mode는 `strict_reservation`입니다. Worker가 다음 tile 예약에 실패하면 이동하지 않고 `TRAFFIC_WAIT`을 기록하며 대기합니다. `observe_conflicts` 모드에서는 traffic conflict를 이동 차단 없이 관찰하며, `collision_count`가 증가해도 worker가 자동으로 `BLOCKED`나 `DISABLED`가 되지는 않습니다.
