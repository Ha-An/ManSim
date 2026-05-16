# ManSim v0.4.3

ManSim은 제조 라인의 discrete-event simulation을 실행하고, 그 결과를 KPI dashboard와 Replay Studio로 관찰하는 실험 프레임워크입니다. v0.4.3의 중심 변화는 worker를 단순 작업자가 아니라 `HumanoidSim`에서 정의한 휴머노이드 로봇으로 다루고, 그 state/task/primitive를 Replay와 KPI에서 그대로 관찰할 수 있게 한 것입니다.

![Replay Studio factory replay 화면](docs/assets/replay-studio-worker-replay.png)

## v0.4.3 업데이트

v0.4.3은 v0.4.2의 tile 기반 factory map과 Replay Studio 개편 위에 Humanoid runtime을 본격 통합한 버전입니다.

- Worker/task 실행 엔진을 `HumanoidSim`의 `TaskSpec -> nested child Task -> Primitive` hierarchy 기반으로 전환했습니다.
- `COMPOSITE_TASK`는 최소 하나 이상의 child task call을 포함하는 workflow로 해석합니다. 예를 들어 `REPLENISH_MATERIAL -> TRANSFER`, `SETUP_MACHINE -> LOAD_MACHINE` 구조가 이벤트와 Replay에 그대로 남습니다.
- Worker 상태를 `HumanoidSim`의 `HumanoidStateSnapshot` 하나로 통일했습니다. State 축은 `availability`, `mobility`, `power`, `manipulation`입니다.
- ManSim에서 사용하는 Humanoid task subset을 명확히 연결했습니다: `REPLENISH_MATERIAL`, `TRANSFER`, `MANAGE_ROBOT_POWER`, `SETUP_MACHINE`, `UNLOAD_MACHINE`, `INSPECT_PRODUCT`, `REPAIR_MACHINE`, `PREVENTIVE_MAINTENANCE`, `HANDOVER_ITEM`, `COLLECT_WASTE_OR_SCRAP`.
- Primitive별 최소 duration을 설정해 Replay Studio에서 task 내부 primitive sequence가 사라지지 않도록 했습니다. 기본값은 `configs/humanoidsim/default.yaml`의 `primitive_timing.default_min: 0.1`분입니다.
- Setup, unload, inspection은 queue와 machine/table/output buffer 사이의 실제 carry 이동을 포함합니다.
- Inspection은 worker가 `inspection_table` service tile에 도착한 뒤에만 수행되며, inspection output queue까지 직접 운반합니다.
- Item weight 기반 이동 시간을 추가했습니다. 기본값은 material `1.0`, intermediate `1.5`, product `2.0`입니다.
- `HANDOVER_ITEM`을 product 공동 운반 합류 task로 지원합니다. Product는 최대 2명의 휴머노이드가 함께 운반할 수 있고, 합류 이후 남은 tile 이동 시간이 carrier 수로 나뉩니다.
- 이동 모델에 traffic layer를 추가했습니다. 기본값은 `strict_reservation`이며, 다음 tile 예약에 실패하면 worker가 대기하고 `TRAFFIC_WAIT`을 기록합니다.
- `observe_conflicts` 모드에서는 동선 겹침, near miss, collision 가능 상황을 막지 않고 event/KPI/Replay overlay로 관찰할 수 있습니다.
- Replay Studio는 worker 이동을 출발지와 목적지의 직선이 아니라 simulator가 기록한 tile path 기준으로 보간합니다.
- Replay Studio worker panel은 `Availability`, `Mobility`, `Manipulation`, `Task`, `Child Task`, `Primitive`, `Motion Path`, `Traffic`, carry item ID, shared carry 정보를 표시합니다.
- Replay Studio 표시 버그를 정리했습니다. 종료된 task의 stale label, 자기 자신과의 traffic 표시, 이동 중 부적절한 task 말풍선이 나오지 않도록 했습니다.
- Repair, inspection, handover replay context를 정리했습니다. Nested child task 종료 뒤에도 parent task progress가 유지되고, domain-internal primitive 변경은 즉시 `WORKER_STATE_CHANGED`로 남으며, product 공동 운반 helper의 `HANDOVER_ITEM`은 inspection table 도착 시점에 종료됩니다.
- 독립 실험 앱인 `replay_studio_3d/`를 추가했습니다. 기존 2D Replay Studio와 직접 import 관계가 없어서 분리해서 삭제할 수 있으며, Results Hub에서 “Replay Studio 3D” 메뉴로 열 수 있습니다.
- Humanoid 관련 문서를 [docs/humanoid_worker_model.md](docs/humanoid_worker_model.md)와 [docs/humanoid_movement_model.md](docs/humanoid_movement_model.md)로 분리했습니다.
- Warehouse의 공유 material shelf를 실제 slot 재고로 바꿨습니다. Worker는 개별 slot service tile까지 가서 material을 집고, day boundary마다 빈 slot만 capacity까지 보충합니다.
- `CompletedProducts` zone과 `ScrapDisposal` zone을 추가했습니다. Accepted product는 `completed_product_buffer`에 도착해야 최종 count가 증가하고, inspection fail product는 `inspection_scrap_queue`를 거쳐 `scrap_disposal_bin`으로 batch 운반됩니다.

## v0.4.2 주요 업데이트

- 시뮬레이션 공간을 좌표 기반 배치에서 tile 기반 factory map으로 전환했습니다.
- Replay Studio가 tile map, queue, machine, inspection 구역을 tile layout 기준으로 표시하도록 바뀌었습니다.
- LLM Wiki, Curator, Graphify 기반 knowledge graph pipeline을 추가했습니다.

기본 실행 경로는 OpenClaw를 쓰지 않는 scripted `adaptive_priority`입니다. 기본 horizon은 5일이며, primary objective는 inspection을 통과한 accepted product를 `CompletedProducts` zone까지 운반해 `completed products`를 최대화하는 것입니다.

## Architecture

- `manufacturing_sim/` - factory simulator core, tile map, task execution, traffic observation, KPI source.
- `configs/` - scenario, decision mode, humanoid profile, runtime 설정.
- `runtime/` - Hydra entrypoint, artifact export, dashboard 생성.
- `dashboards/` - results hub, KPI, knowledge, series dashboard.
- `replay_studio/` - 기존 2D React Replay Studio.
- `replay_studio_3d/` - 독립 3D Replay Studio 실험 앱. Results Hub에서 “Replay Studio 3D”로 접근합니다.
- `agents/`, `openclaw/` - optional OpenClaw manager loop.
- `knowledge/` - run-series knowledge, LLM Wiki, Graphify artifact.
- `docs/` - simulator, humanoid runtime, movement, dashboard, LLM Wiki 문서.
- `tests/` - Humanoid runtime and traffic contract tests.

## Humanoid Worker Model

ManSim에서 worker는 `HumanoidSim` 라이브러리의 정의를 import해서 사용하는 휴머노이드 로봇입니다. State, Task, Primitive의 정의 주체는 ManSim이 아니라 `HumanoidSim`입니다. ManSim은 특정 factory scenario에서 이 정의가 어떻게 실행되고 관찰되는지를 기록합니다.

현재 ManSim에서 쓰는 Humanoid task subset:

- `REPLENISH_MATERIAL`
- `TRANSFER`
- `MANAGE_ROBOT_POWER`
- `SETUP_MACHINE`
- `UNLOAD_MACHINE`
- `INSPECT_PRODUCT`
- `REPAIR_MACHINE`
- `PREVENTIVE_MAINTENANCE`
- `HANDOVER_ITEM`
- `COLLECT_WASTE_OR_SCRAP`

Worker state는 다음 네 축으로만 표현합니다.

- `availability`: `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED`
- `mobility`: `STATIONARY`, `NAVIGATING`, `DOCKING`
- `power`: `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING`
- `manipulation`: `FREE`, `REACHING`, `HOLDING`, `PLACING`

Task 자체는 state가 아닙니다. 예를 들어 `REPLENISH_MATERIAL` 수행 중인 worker는 `availability=EXECUTING`이고, task 정보는 `humanoid_state.task_context.task_code=REPLENISH_MATERIAL`로 기록됩니다.

자세한 내용은 [docs/humanoid_worker_model.md](docs/humanoid_worker_model.md)를 보세요.

## Movement

Worker 이동은 tile map 위에서 이루어집니다. `TileGridMap.find_path()`가 A* search로 목적지 service tile까지의 4방향 path를 만들고, worker는 `map.tile_time_min` 단위로 한 tile씩 이동합니다.

기본 traffic mode는 `strict_reservation`입니다. Worker가 다음 tile을 예약하지 못하면 이동하지 않고 대기하며, `AGENT_TRAFFIC_CONFLICT` / `TRAFFIC_WAIT`이 event와 KPI에 기록됩니다. `observe_conflicts`로 바꾸면 동선 겹침과 near miss를 막지 않고 관찰할 수 있습니다.

자세한 내용은 [docs/humanoid_movement_model.md](docs/humanoid_movement_model.md)를 보세요.

## Quick Start

가상환경과 의존성 설치:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\HumanoidSim
```

`HumanoidSim`는 ManSim과 같은 `C:\Github` 아래에 있는 독립 라이브러리입니다. 패키지가 설치되어 있지 않으면 Humanoid hierarchy runtime은 시작 시 명확한 에러를 냅니다.

1일 smoke run:

```powershell
.\.venv\Scripts\python.exe main.py scenario.horizon.num_days=1 runtime.ui.auto_open_results=false
```

기본 5일 run:

```powershell
.\.venv\Scripts\python.exe main.py
```

3일 run:

```powershell
.\.venv\Scripts\python.exe main.py scenario.horizon.num_days=3
```

OpenClaw/LLM manager path:

```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority
```

Replay Studio 개발 서버:

```powershell
cd replay_studio
npm install
npm run dev
```

3D Replay Studio 개발 서버:

```powershell
cd replay_studio_3d
npm install
npm run dev
```

## Outputs

Run 결과는 `outputs/<date>/<run-id>/` 아래에 생성됩니다.

- `results_dashboard.html` - run hub.
- `kpi.json`, `daily_summary.json` - 운영 분석 artifact.
- `events.jsonl` - simulator event stream.
- `minute_snapshots.json` - queue, machine, worker `humanoid_states` snapshot.
- `replay_studio_log.json`, `replay_studio_layout.json` - Replay Studio 입력.
- `dashboard_manifest.json` - hub navigation manifest.
- `run_series_summary.json`, `series_analysis.json` - multi-run 비교 데이터.
- `llm_wiki_dashboard.html` - LLM Wiki 진입점.
- `knowledge_graph_dashboard.html` - Graphify knowledge graph viewer.

Worker 관련 event에는 `humanoid_state` 원본 snapshot이 들어갑니다. 주요 event는 `HUMANOID_TASK_*`, `HUMANOID_STEP_*`, `WORKER_STATE_CHANGED`, `WORKER_CARGO_CHANGED`, `AGENT_MOVE_TILE_*`, `AGENT_TRAFFIC_CONFLICT`입니다.

## Verification

Python tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Replay Studio build:

```powershell
$env:PATH = "C:\Github\ManSim\.tooling\node;$env:PATH"
cd replay_studio
npm.cmd run build
```

3D Replay Studio build:

```powershell
$env:PATH = "C:\Github\ManSim\.tooling\node;$env:PATH"
cd replay_studio_3d
npm.cmd run build
```

## Documents

- [docs/README.md](docs/README.md) - 문서 index.
- [docs/simulator_core_guide.md](docs/simulator_core_guide.md) - simulator core 구조와 runtime boundary.
- [docs/humanoid_worker_model.md](docs/humanoid_worker_model.md) - Humanoid State, Task, Primitive, ManSim 적용 방식.
- [docs/humanoid_movement_model.md](docs/humanoid_movement_model.md) - tile pathfinding, traffic reservation, movement events.
- [docs/decision_logic.md](docs/decision_logic.md) - decision mode와 성공 기준.
- [docs/replay_dashboards.md](docs/replay_dashboards.md) - dashboard와 replay artifact.
- [docs/openclaw_adaptive_priority_call_flow.md](docs/openclaw_adaptive_priority_call_flow.md) - OpenClaw manager loop.
- [docs/llm_wiki_curator.md](docs/llm_wiki_curator.md) - LLM Wiki, Curator, Graphify pipeline.

## Notes

- 기본 config는 `decision=adaptive_priority`, `scenario.horizon.num_days=5`입니다.
- `task_type`과 priority key는 decision layer 호환용 label입니다. 실제 실행과 분석 기준은 `task_code`, child task path, `step_id`, `primitive_call_code`, `humanoid_state`입니다.
- State/Task/Primitive의 정의와 관계는 `HumanoidSim`가 소유합니다. ManSim은 이를 import해서 실행하고 관찰합니다.
- LLM Wiki와 knowledge graph는 optional manager knowledge layer이며 simulator state를 대체하지 않습니다.
