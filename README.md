# ManSim v0.4.3

ManSim은 제조 환경의 discrete-event simulation, KPI dashboard, Gantt chart, 2D/3D Replay Studio를 포함한 공장 운영 시뮬레이션 워크스페이스입니다. v0.4.3에서는 worker를 단순 agent가 아니라 `HumanoidSim`에서 정의한 task, primitive, state, incident 모델을 사용하는 휴머노이드 로봇으로 재구성했습니다.

![Replay Studio factory replay](docs/assets/replay-studio-worker-replay.png)

## v0.4.3 주요 업데이트

v0.4.3은 v0.4.2의 tile 기반 공장 layout과 Replay Studio 개편 위에 Humanoid runtime을 크게 확장한 버전입니다.

- Worker/task 실행 단위를 `HumanoidSim`의 `TaskSpec -> nested child Task -> Primitive` hierarchy 기반으로 전환했습니다.
- `COMPOSITE_TASK`는 하위 task call을 포함하는 workflow로 해석합니다. 예: `REPLENISH_MATERIAL -> TRANSFER`, `SETUP_MACHINE -> LOAD_MACHINE`
- Worker state는 `HumanoidSim`의 `HumanoidStateSnapshot`을 기준으로 기록합니다.
  - `availability`
  - `mobility`
  - `power`
  - `manipulation`
- 현재 ManSim에서 사용하는 task subset은 `REPLENISH_MATERIAL`, `TRANSFER`, `MANAGE_ROBOT_POWER`, `SETUP_MACHINE`, `UNLOAD_MACHINE`, `INSPECT_PRODUCT`, `REPAIR_MACHINE`, `PREVENTIVE_MAINTENANCE`, `HANDOVER_ITEM`, `COLLECT_WASTE_OR_SCRAP`입니다.
- Primitive별 최소 duration 기본값을 `0.1`분으로 두어 Replay Studio에서 primitive 전환을 관찰할 수 있게 했습니다.
- Setup, unload, inspection에서 queue와 machine/table/output buffer 사이의 실제 carry 이동을 추가했습니다.
- Product 운반은 material보다 오래 걸리며, `HANDOVER_ITEM`으로 최대 2명의 공동 운반을 표현합니다.
- 기본 traffic mode는 `strict_reservation`입니다. 다음 tile 예약에 실패하면 worker는 이동하지 않고 `TRAFFIC_WAIT`으로 대기합니다.
- `observe_conflicts` 모드에서는 path overlap, near miss, collision 가능 상황을 차단하지 않고 event/KPI/Replay overlay로 관찰할 수 있습니다.
- Replay Studio는 worker의 실제 tile path를 따라 이동을 보간하고, worker panel에 Task, Child Task, Primitive, Motion Path, Traffic, Incident, Carry 정보를 표시합니다.
- Gantt chart는 worker lane을 `HumanoidSim` Availability State 기준으로 표시합니다.
- KPI dashboard는 humanoid state, task, primitive, task taxonomy, worker collaboration, incident 통계를 포함합니다.
- 독립 실험 앱인 `replay_studio_3d/`를 추가했습니다. 기존 2D Replay Studio와 직접 import 관계가 없습니다.
- Warehouse material shelf, CompletedProducts zone, ScrapDisposal zone, inspection scrap queue를 추가했습니다.
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
- `replay_studio_3d/`: 독립형 3D Replay Studio 실험 앱
- `agents/`, `openclaw/`: optional OpenClaw manager loop
- `knowledge/`: run-series knowledge, LLM Wiki, Graphify artifact
- `docs/`: simulator, humanoid runtime, movement, dashboard, LLM Wiki 문서
- `tests/`: humanoid runtime, traffic, replay export, zone/scrap contract tests

## Humanoid Worker Model

ManSim의 worker는 `HumanoidSim`에서 정의한 휴머노이드 모델을 import해 사용하는 runtime instance입니다. State, Task, Primitive, Incident의 기본 정의는 ManSim이 아니라 `HumanoidSim`이 소유합니다. ManSim은 factory scenario에서 어떤 task가 할당되고, 어떤 event가 발생했는지 판단해 HumanoidSim transition API에 전달합니다.

Worker state는 다음 네 축으로 표현됩니다.

- `availability`: `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED`
- `mobility`: `STATIONARY`, `NAVIGATING`, `DOCKING`
- `power`: `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING`
- `manipulation`: `FREE`, `REACHING`, `HOLDING`, `PLACING`

Task는 state가 아닙니다. 예를 들어 `REPLENISH_MATERIAL` 수행 중인 worker는 `availability=EXECUTING`이고, task 정보는 `humanoid_state.task_context.task_code=REPLENISH_MATERIAL`에 기록됩니다.

자세한 설명은 [docs/humanoid_worker_model.md](docs/humanoid_worker_model.md)를 참고하세요.

## HumanoidSim Transition API

ManSim은 state 축을 직접 임의 계산하지 않습니다. 다음과 같은 scenario fact를 event로 전달합니다.

- task assigned/start/end
- primitive start/end
- cargo pickup/drop
- battery/charging
- waiting/blocked/disabled/offline
- traffic wait/resource race/incident

HumanoidSim은 이 event를 바탕으로 `HumanoidStateSnapshot`을 반환합니다. 잘못된 transition은 strict fail로 처리되어 simulation이 중단되고 worker id, event, previous snapshot, reason을 출력합니다.

## Movement

Worker 이동은 tile map 기반입니다. `TileGridMap.find_path()`가 A* search로 4방향 path를 찾고, worker는 `map.tile_time_min` 단위로 한 tile씩 이동합니다. Replay Studio는 simulation artifact에 기록된 `motion.path`를 사용해 출발지에서 목적지까지 부드럽게 보간합니다.

기본 traffic mode는 `strict_reservation`입니다. Worker가 다음 tile을 예약하지 못하면 이동하지 않고 `TRAFFIC_WAIT` reason을 남깁니다. `observe_conflicts` 모드에서는 path overlap, near miss, collision을 차단하지 않고 관찰 event로 기록합니다.

자세한 설명은 [docs/humanoid_movement_model.md](docs/humanoid_movement_model.md)를 참고하세요.

## Humanoid Incident Model

휴머노이드 돌발상황의 taxonomy와 recovery protocol은 `HumanoidSim`이 정의합니다. ManSim은 factory scenario에서 어떤 확률과 조건으로 incident가 발생하는지만 판단하고, 발생한 사건을 `HUMANOID_INCIDENT` event와 `StateReason`으로 기록합니다.

- 확률 기반 incident는 `configs/scenario/mfg_basic.yaml`의 `humanoid_incidents.random`에서 조정합니다.
- 기본 random incident는 `OBJECT_RECOGNITION_FAILED`, `GRIP_FAILED`, `ITEM_DROPPED`, `UNKNOWN`입니다.
- `RESOURCE_PREEMPTED`, `TRAFFIC_WAIT`, `NEAR_MISS`, `COLLISION`은 ManSim의 resource race나 traffic model에서 자연 발생한 상황을 HumanoidSim incident code로 기록합니다.
- `material_shelf_slot_empty` 같은 ManSim 내부 실패 reason은 ManSim에서 별도 taxonomy로 정의하지 않고, HumanoidSim의 incident alias를 통해 canonical incident code로 해석합니다.
- Incident는 state가 아닙니다. 예를 들어 grip 실패나 item drop은 `availability=BLOCKED`가 되고, reason code가 `GRIP_FAILED` 또는 `ITEM_DROPPED`로 남습니다.
- Recovery protocol이 진행 중일 때도 availability는 `BLOCKED`를 유지합니다. 현재 복구 step은 기존 Task 또는 Primitive 필드에 `CODE (RECOVERY)`로 표시되며, 정상 task 실행 상태인 `EXECUTING`과 구분합니다.
- Replay Studio 말풍선은 incident code 전체를 쓰지 않고 Availability badge를 우선 표시합니다. 예: `BLK`, `WAIT`, `DIS`, `OFF`
- Replay Studio worker panel에는 incident category/code를 표시합니다. Recovery protocol 전체 목록은 별도 패널로 만들지 않고, 현재 실행 중인 recovery step만 기존 Task 또는 Primitive 칸에 `CODE (RECOVERY)`로 표시합니다.
- KPI에는 `humanoid_incident_total`, `humanoid_incidents_by_code`, `humanoid_incidents_by_category`, `humanoid_incidents_by_worker`, `humanoid_incident_recovery_protocol_by_code`가 추가됩니다.

HumanoidSim 기준 문서는 `C:\Github\HumanoidSim\docs\incident_reference.md`를 참고하세요.

## Quick Start

의존성을 설치합니다.

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

Run artifact는 `outputs/` 아래에 생성됩니다. 주요 파일은 다음과 같습니다.

- `events.jsonl`: simulation event log
- `minute_snapshots.json`: minute-level factory snapshot
- `daily_summary.json`: day별 생산/queue/incident summary
- `kpi.json`: KPI dashboard source
- `replay_studio_log.json`: 2D/3D Replay Studio 입력
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

이 감사 스크립트는 KPI 필수 필드, Gantt lane/status, Replay log/layout, stale machine overlay,
self traffic conflict, worker state payload를 한 번에 점검합니다. Replay Studio에서 이상한 장면이 보이면
먼저 이 스크립트로 core artifact와 UI artifact 중 어디가 어긋났는지 확인합니다.

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
- [docs/dashboard_guide.md](docs/dashboard_guide.md): results hub와 KPI dashboard
- [docs/llm_wiki_design.md](docs/llm_wiki_design.md): LLM Wiki / Curator / Graphify 설계

## Notes

- 기본 decision mode는 `adaptive_priority`입니다.
- 기본 horizon은 5일 run입니다.
- OpenClaw mode는 optional입니다.
- `HumanoidSim`은 ManSim과 같은 상위 폴더인 `C:\Github` 아래에 있다고 가정합니다.
