# ManSim v0.4.3

ManSim은 제조 공장을 대상으로 하는 discrete-event simulation 워크스페이스입니다. 기본 시뮬레이션, KPI dashboard, Gantt chart, 2D Replay Studio, 독립형 3D Replay Studio, OpenClaw manager loop, LLM Wiki/Graphify 지식 파이프라인을 함께 제공합니다.

v0.4.3에서는 worker를 단순 이동 agent가 아니라 `HumanoidSim`에서 정의한 Task, Primitive, State, Incident 모델을 사용하는 휴머노이드 로봇 runtime instance로 재구성했습니다.

![Replay Studio factory replay](docs/assets/replay-studio-worker-replay.png)

## v0.4.3 주요 업데이트

v0.4.3은 v0.4.2의 tile 기반 공장 map과 Replay Studio 개편 위에 휴머노이드 실행 모델을 확장한 버전입니다.

- Worker/task 실행 단위를 `HumanoidSim`의 `TaskSpec -> nested child Task -> Primitive` hierarchy 기반으로 전환했습니다.
- `COMPOSITE_TASK`는 하위 task call을 포함하는 workflow로 해석합니다. 예: `REPLENISH_MATERIAL -> TRANSFER`, `SETUP_MACHINE -> LOAD_MACHINE`.
- Worker state는 `HumanoidSim`의 `HumanoidStateSnapshot`만 사용합니다. 축은 `availability`, `mobility`, `power`, `manipulation`입니다.
- 현재 ManSim에서 사용하는 task subset은 `REPLENISH_MATERIAL`, `TRANSFER`, `MANAGE_ROBOT_POWER`, `SETUP_MACHINE`, `UNLOAD_MACHINE`, `INSPECT_PRODUCT`, `REPAIR_MACHINE`, `PREVENTIVE_MAINTENANCE`, `HANDOVER_ITEM`, `COLLECT_WASTE_OR_SCRAP`입니다.
- Primitive별 최소 duration 기본값을 `0.1`분으로 두어 Replay Studio에서 primitive 전환을 관찰할 수 있게 했습니다.
- Setup, unload, inspection에서 queue, machine, inspection table, output buffer 사이의 실제 carry 이동을 추가했습니다.
- Product 운반은 material보다 오래 걸리며, `HANDOVER_ITEM`으로 최대 2명의 공동 운반을 표현합니다.
- Worker 시작 위치는 Warehouse 내부가 아니라 Warehouse와 Station 2 사이의 복도입니다.
- 기본 traffic mode는 `strict_reservation`입니다. 다음 tile 예약에 실패하면 worker는 이동하지 않고 `TRAFFIC_WAIT` HumanoidSim incident를 남긴 뒤 recovery protocol을 실행합니다.
- `observe_conflicts` 모드에서는 path overlap, near miss, collision 가능 상황을 차단하지 않고 event/KPI/Replay overlay로 관찰합니다.
- Warehouse material shelf, CompletedProducts zone, ScrapDisposal zone, inspection scrap queue를 추가했습니다.
- 2D/3D Replay Studio는 worker의 실제 tile path를 따라 이동을 보간하고, worker panel에 Task, Child Task, Primitive, Motion Path, Traffic, Incident, Carry 정보를 표시합니다.
- 3D Replay Studio는 procedural block model로 worker, machine, queue, shelf, inspection table을 표현합니다. Worker label은 `A1`처럼 id만 표시하고, 정지 작업 중에는 팔 동작을 보여줍니다.
- 3D inspection table은 replay entity가 없어도 layout footprint에서 직접 렌더링합니다. Input queue는 노랑, output/completed queue는 파랑으로 표시합니다.
- KPI dashboard는 humanoid state, task, primitive, task taxonomy, worker collaboration, incident, traffic, shelf/scrap 통계를 포함합니다.
- Gantt chart는 worker lane을 `HumanoidSim` Availability State 기준으로 표시합니다.
- LLM Wiki, Curator, Graphify 기반 knowledge pipeline은 기존 기능을 유지합니다.

## v0.4.2 주요 업데이트

- 시뮬레이션 환경을 좌표 기반이 아니라 tile 기반 factory map으로 전환했습니다.
- Replay Studio를 tile map, queue, machine, inspection layout 기준으로 수정했습니다.
- LLM Wiki, Curator, Graphify 기반 knowledge graph pipeline을 추가했습니다.

## Architecture

- `manufacturing_sim/`: factory simulation core, tile map, humanoid task runtime, traffic monitor, KPI source
- `configs/`: scenario, decision mode, humanoid profile, runtime 설정
- `runtime/`: Hydra entrypoint, artifact export, dashboard 실행
- `dashboards/`: results hub, KPI dashboard, Gantt chart, knowledge dashboard, series dashboard
- `replay_studio/`: 기존 2D React Replay Studio
- `replay_studio_3d/`: 독립형 3D React/Three.js Replay Studio
- `agents/`, `openclaw/`: optional OpenClaw manager loop
- `knowledge/`: run-series knowledge, LLM Wiki, Graphify artifact
- `docs/`: simulator, humanoid runtime, movement, dashboard, LLM Wiki 문서
- `tests/`: humanoid runtime, traffic, replay export, zone/scrap contract tests

## Humanoid Worker Model

ManSim의 worker는 `HumanoidSim`에서 정의한 휴머노이드 모델을 import해 사용하는 runtime instance입니다. State, Task, Primitive, Incident의 기본 정의는 ManSim이 아니라 `HumanoidSim`이 소유합니다. ManSim은 factory scenario에서 어떤 task가 할당되고 어떤 event가 발생했는지 판단해 HumanoidSim transition API에 전달합니다.

Worker state는 다음 네 축으로 표현됩니다.

- `availability`: `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED`
- `mobility`: `STATIONARY`, `NAVIGATING`, `DOCKING`
- `power`: `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING`
- `manipulation`: `FREE`, `REACHING`, `HOLDING`, `PLACING`

Task는 state가 아닙니다. 예를 들어 `REPLENISH_MATERIAL` 수행 중인 worker는 `availability=EXECUTING`이고, task 정보는 `humanoid_state.task_context.task_code=REPLENISH_MATERIAL`에 기록됩니다.

자세한 설명은 [docs/humanoid_worker_model.md](docs/humanoid_worker_model.md)를 참고하세요.

## Movement

Worker 이동은 tile map 기반입니다. `TileGridMap.find_path()`가 A* search로 4방향 path를 찾고, worker는 `map.tile_time_min` 단위로 한 tile씩 이동합니다. Replay Studio는 simulation artifact의 `motion.path`를 사용해 출발지에서 목적지까지 부드럽게 보간합니다.

`path_wait`처럼 실제로는 멈춰 있지만 계획 경로를 관찰해야 하는 경우, Replay artifact는 `motion.paused=true`와 `display_path`를 남깁니다. Replay Studio는 worker를 현재 tile에 고정하고 계획 경로는 점선 overlay로만 보여줍니다.

자세한 설명은 [docs/humanoid_movement_model.md](docs/humanoid_movement_model.md)를 참고하세요.

## Humanoid Incident Model

휴머노이드 돌발상황의 taxonomy와 recovery protocol은 `HumanoidSim`이 정의합니다. ManSim은 factory scenario에서 어떤 확률과 조건으로 incident가 발생하는지만 판단하고, 발생한 사건을 `HUMANOID_INCIDENT` event와 `StateReason`으로 기록합니다.

- 확률 기반 incident는 `configs/scenario/mfg_basic.yaml`의 `humanoid_incidents.random`에서 조정합니다.
- 기본 random incident는 `OBJECT_RECOGNITION_FAILED`, `GRIP_FAILED`, `ITEM_DROPPED`, `UNKNOWN`입니다.
- `RESOURCE_PREEMPTED`, `PATH_BLOCKED`, `TRAFFIC_WAIT`, `NEAR_MISS`, `COLLISION`은 resource race나 traffic model에서 자연 발생한 상황을 HumanoidSim incident code로 기록합니다.
- `material_shelf_slot_empty` 같은 ManSim 내부 실패 reason은 ManSim taxonomy가 아니라 HumanoidSim incident alias를 통해 canonical incident code로 해석합니다.
- Recovery protocol 진행 중에는 availability를 `BLOCKED`로 유지하고, 현재 recovery step은 Task 또는 Primitive 필드에 `CODE (RECOVERY)`로 표시합니다.

HumanoidSim 기준 문서는 `C:\Github\HumanoidSim\docs\incident_reference.md`를 참고하세요.

## Quick Start

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\HumanoidSim
```

1일 smoke run:

```powershell
.\.venv\Scripts\python.exe main.py scenario.horizon.num_days=1 runtime.ui.auto_open_results=false
```

기본 5일 run:

```powershell
.\.venv\Scripts\python.exe main.py
```

최근 run hub 열기:

```powershell
.\.venv\Scripts\python.exe -m dashboards.manifest --latest
```

## Outputs

Run artifact는 `outputs/` 아래에 생성됩니다.

- `events.jsonl`: simulation event log
- `minute_snapshots.json`: minute-level factory snapshot
- `daily_summary.json`: day별 생산/queue/incident summary
- `kpi.json`: KPI dashboard source
- `replay_studio_log.json`: 2D/3D Replay Studio 입력
- `replay_studio_layout.json`: Replay Studio layout 입력
- `gantt.html`, `gantt_segments.csv`: Gantt chart와 source data
- `dashboard_manifest.json`: hub가 참조하는 artifact manifest

## Verification

주요 테스트:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_humanoid_runtime
.\.venv\Scripts\python.exe -m unittest tests.test_traffic_monitor
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Run artifact 감사:

```powershell
.\.venv\Scripts\python.exe scripts\audit_run_artifacts.py outputs\YYYY-MM-DD\HH-MM-SS
```

Replay Studio build:

```powershell
cd replay_studio
npm run build
```

3D Replay Studio build:

```powershell
cd replay_studio_3d
npm run test
npm run build
```

## Documents

- [docs/README.md](docs/README.md): 문서 index
- [docs/simulator_core_guide.md](docs/simulator_core_guide.md): simulation core 개요
- [docs/humanoid_worker_model.md](docs/humanoid_worker_model.md): humanoid worker task/state/incident 모델
- [docs/humanoid_movement_model.md](docs/humanoid_movement_model.md): tile movement, reservation, traffic model
- [docs/replay_dashboards.md](docs/replay_dashboards.md): results hub, KPI, Gantt, Replay Studio
- [docs/llm_wiki_curator.md](docs/llm_wiki_curator.md): LLM Wiki / Curator / Graphify

## Notes

- 기본 decision mode는 `adaptive_priority`입니다.
- 기본 horizon은 5일 run입니다.
- OpenClaw mode는 optional입니다.
- `HumanoidSim`은 ManSim과 같은 상위 폴더인 `C:\Github` 아래에 있다고 가정합니다.
