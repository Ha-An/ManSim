# ManSim Docs

이 디렉터리는 ManSim의 scenario, decision mode, humanoid runtime, movement, dashboard, manager/knowledge pipeline 문서를 담고 있습니다.

## 현재 기준

- 기본 scenario: `factory_mfg_basic`
- 기본 decision mode: `rolling_horizon_dedicated_roles`
- 기본 horizon: 5일 run
- worker state/task/primitive/incident 정의: `HumanoidSim`
- movement model: tile pathfinding + `strict_reservation`
- 기본 visual replay: `Replay Studio 3D`
- Hub UI: 모든 scenario가 공유하는 공통 Results Hub

## 추천 읽기 순서

1. [simulator_core_guide.md](simulator_core_guide.md): scenario registry, factory/shipyard world, task generation, artifact export
2. [decision_logic.md](decision_logic.md): decision mode, rolling horizon pool/dispatch, dedicated roles
3. [humanoid_worker_model.md](humanoid_worker_model.md): ManSim worker가 HumanoidSim state/task/incident를 사용하는 방식
4. [humanoid_movement_model.md](humanoid_movement_model.md): pathfinding, reservation, traffic wait, replay interpolation
5. [replay_dashboards.md](replay_dashboards.md): Results Hub, KPI, Gantt, 3D Replay Studio
6. [llm_wiki_curator.md](llm_wiki_curator.md): optional LLM Wiki, Curator, Graphify
7. [openclaw_adaptive_priority_call_flow.md](openclaw_adaptive_priority_call_flow.md): OpenClaw manager loop

## Scenario 문서 기준

`factory_mfg_basic`은 Warehouse, Station 1, Station 2, Inspection을 거치는 제조 공정입니다. Accepted product, scrap, queue wait, machine state, humanoid state가 주요 관찰 대상입니다.

`shipyard_basic`은 선박 외관 surface tile을 대상으로 용접, 표면처리, 도장, 검사를 반복하는 공정입니다. 모든 surface tile이 완료되는 시점의 `makespan_min`이 핵심 KPI입니다. Cart logistics는 `OPERATE_VEHICLE_TRANSPORT`와 `TRANSFER(cart_supply)`로 표현합니다.

## Humanoid 기준

ManSim은 worker state enum을 자체적으로 새로 정의하지 않습니다. Worker state는 HumanoidSim의 네 축 snapshot으로 기록됩니다.

| Axis | States |
| --- | --- |
| Availability | `AVAILABLE`, `ASSIGNED`, `EXECUTING`, `WAITING`, `BLOCKED`, `OFFLINE`, `DISABLED` |
| Mobility | `STATIONARY`, `NAVIGATING`, `DOCKING` |
| Power | `POWER_NORMAL`, `POWER_LOW`, `POWER_CRITICAL`, `DEPLETED`, `CHARGING` |
| Manipulation | `FREE`, `REACHING`, `HOLDING`, `PLACING` |

Task는 state가 아닙니다. Task와 primitive는 `task_context`, event log, replay panel, KPI task minutes에 기록됩니다.

## Dashboard 기준

- Results Hub는 scenario/decision/run metadata와 주요 KPI로 구성됩니다.
- 3D Replay Studio는 현재 지원되는 visual replay entry point입니다.
- 2D Replay artifact는 backward compatibility 용도로 남아 있지만 Hub 메뉴에는 노출하지 않습니다.
- Knowledge Graph, LLM Wiki, manager replay, reasoning dashboard는 manager/knowledge 기능이 켜진 run에서만 표시됩니다.
- KPI dashboard는 상세 incident, traffic, humanoid state, machine state, cart/shipyard 지표를 확인하는 보조 화면입니다.

## Audit 명령

Run이 끝난 뒤 다음 두 audit를 기본으로 사용합니다.

```powershell
.\.venv\Scripts\python.exe scripts\audit_run_artifacts.py outputs\YYYY-MM-DD\HH-MM-SS
.\.venv\Scripts\python.exe scripts\audit_kpi.py outputs\YYYY-MM-DD\HH-MM-SS
```

주요 확인 항목:

- Hub/KPI/Gantt/Replay artifact 존재 여부
- scenario metadata 일관성
- humanoid state time과 event log 일관성
- machine `BROKEN`/`UNDER_REPAIR`/PM/setup/processing 시간 일관성
- rolling horizon task id, requeue, skip, dispatch 일관성
- replay worker position/state 보존
