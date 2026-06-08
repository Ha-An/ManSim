# ManSim

ManSim은 휴머노이드 작업자가 포함된 공정 시뮬레이션을 실행하고, 결과를 Hub, KPI dashboard, Gantt chart, 3D Replay Studio로 확인하는 discrete-event simulation 워크스페이스입니다.

Worker의 State, Task, Primitive, Incident 정의는 `HumanoidSim`이 소유합니다. ManSim은 시나리오에서 어떤 작업 후보와 사건이 발생했는지 판단하고, HumanoidSim runtime을 통해 state transition과 task execution trace를 기록합니다.

![Replay Studio factory replay](docs/assets/replay-studio-worker-replay.png)

## Current Focus

- 기본 decision mode는 `rolling_horizon_dedicated_roles`입니다.
- 기본 scenario는 `factory_mfg_basic`입니다.
- Results Hub는 모든 scenario가 공유하는 공통 UI입니다.
- 시각 replay의 기본 진입점은 `Replay Studio 3D`입니다. 2D Replay artifact는 호환용으로 남아 있지만 Hub 메뉴에는 노출하지 않습니다.
- OpenClaw/LLM manager, Knowledge Graph, LLM Wiki 관련 화면은 해당 기능이 활성화된 run에서만 Hub에 표시됩니다.

## Scenarios

ManSim은 `scenario.type`으로 scenario plugin을 선택합니다.

| Scenario | 설명 | 주요 KPI |
| --- | --- | --- |
| `factory_mfg_basic` | Warehouse, Station 1, Station 2, Inspection을 거치는 제조 공정입니다. | accepted products, scrap rate, machine utilization, worker state/time |
| `shipyard_basic` | 중앙 선박 외관 surface tile을 `WELD_SEAM -> PREPARE_SURFACE -> PAINT_SURFACE -> VERIFY_SHIP_SECTION` 순서로 처리하는 조선소 공정입니다. | `makespan_min`, completed surface tiles, quality pass rate, cart logistics |

## Quick Start

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\HumanoidSim
```

기본 5일 run:

```powershell
.\.venv\Scripts\python.exe main.py
```

Factory 5일 run:

```powershell
.\.venv\Scripts\python.exe main.py scenario=factory_mfg_basic scenario.horizon.num_days=5 runtime.ui.auto_open_results=false
```

Shipyard 5일 run:

```powershell
.\.venv\Scripts\python.exe main.py scenario=shipyard_basic scenario.horizon.num_days=5 runtime.ui.auto_open_results=false
```

최근 run Hub 열기:

```powershell
.\.venv\Scripts\python.exe -m dashboards.manifest --latest
```

## Decision Modes

| Mode | 설명 |
| --- | --- |
| `rolling_horizon_dedicated_roles` | Window 동안 모은 task를 worker별 전담 task allowlist와 aging rank로 dispatch합니다. 기본 mode입니다. |
| `rolling_horizon_aging_priority` | Scenario별 task code priority와 기다린 window 수를 기준으로 dispatch합니다. |
| `adaptive_priority` | 공정 상태에 따라 scripted priority를 조정하는 baseline입니다. |
| `fixed_priority` | 고정 priority baseline입니다. |
| `openclaw_adaptive_priority` | OpenClaw manager loop를 사용합니다. manager/knowledge 화면은 이 계열 run에서만 표시됩니다. |

Rolling horizon 계열은 scenario별 priority 설정을 사용합니다. 설정 위치:

- `configs/decision/rolling_horizon_aging_priority.yaml`
- `configs/decision/rolling_horizon_dedicated_roles.yaml`

Factory와 Shipyard는 task set이 다르므로 `scenario_task_code_priority_order.<scenario>`와 `scenario_worker_task_priority.<scenario>` 아래에서 따로 설정합니다.

## Runtime Notes

- Worker 이동은 tile path와 strict reservation 기반입니다.
- Worker battery drain은 scenario config의 `worker.battery_drain`에서 조정합니다.
- 기본 drain multiplier는 `AVAILABLE=0.5`, non-available state는 `1.0`입니다. 따라서 작업/이동/대기 중인 worker는 idle available 상태보다 2배 빠르게 배터리를 사용합니다.
- 긴급 task trigger는 rolling horizon window를 기다리지 않고 worker queue에 먼저 반영할 수 있습니다. 기본 정책은 현재 task를 중단하지 않고, 완료 직후 우선 실행하는 `next_after_current`입니다.
- Machine repair KPI는 `BROKEN`과 `UNDER_REPAIR` 시간을 분리합니다. 수리가 취소되어 repair team이 비면 다시 `BROKEN` 시간으로 집계됩니다.

## Outputs

Run artifact는 `outputs/YYYY-MM-DD/HH-MM-SS/` 아래에 생성됩니다.

| 파일 | 용도 |
| --- | --- |
| `results_dashboard.html` | 공통 Results Hub |
| `kpi.json`, `kpi_dashboard.html` | KPI source와 dashboard |
| `gantt.html`, `gantt_segments.csv` | Gantt chart와 source data |
| `events.jsonl` | simulation event log |
| `minute_snapshots.json` | minute-level state snapshot |
| `replay_studio_log.json` | 3D Replay Studio 입력 |
| `replay_studio_layout.json` | replay layout 입력 |
| `dashboard_manifest.json` | Hub가 참조하는 artifact manifest |

## Verification

주요 회귀 테스트:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_runtime_compat tests.test_rolling_horizon_decision tests.test_replay_export tests.test_gantt
```

전체 테스트:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Run artifact 감사:

```powershell
.\.venv\Scripts\python.exe scripts\audit_run_artifacts.py outputs\YYYY-MM-DD\HH-MM-SS
.\.venv\Scripts\python.exe scripts\audit_kpi.py outputs\YYYY-MM-DD\HH-MM-SS
```

3D Replay Studio:

```powershell
cd replay_studio_3d
npm run test
npm run build
```

## Documents

- [docs/README.md](docs/README.md): 문서 index
- [docs/simulator_core_guide.md](docs/simulator_core_guide.md): simulation core, scenario, task generation, KPI/export
- [docs/humanoid_worker_model.md](docs/humanoid_worker_model.md): HumanoidSim 기반 worker state/task/incident runtime
- [docs/humanoid_movement_model.md](docs/humanoid_movement_model.md): tile movement, reservation, traffic model
- [docs/decision_logic.md](docs/decision_logic.md): decision mode와 rolling horizon dispatch
- [docs/replay_dashboards.md](docs/replay_dashboards.md): Hub, KPI, Gantt, 3D Replay Studio
- [docs/llm_wiki_curator.md](docs/llm_wiki_curator.md): optional LLM Wiki, Curator, Graphify path
