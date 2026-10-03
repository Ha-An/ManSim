# ManSim Docs

이 디렉터리는 ManSim의 scenario, decision mode, humanoid runtime, movement, dashboard, manager/knowledge pipeline 문서를 담고 있습니다.

## 현재 기준

- ManSim release: `v0.6.0`
- 기본 scenario: `factory_mfg_basic`
- 기본 decision mode: `rolling_horizon_dedicated_roles`
- 기본 horizon: 5일 run
- worker state/task/primitive/incident 정의: `HumanoidSim`
- movement model: tile pathfinding + `strict_reservation`
- 기본 visual replay: `Replay Studio 3D`
- Hub UI: 모든 scenario가 공유하는 공통 Results Hub

## 추천 읽기 순서

1. [installation.md](installation.md): Python/Node.js 요구사항, 전체 설치, 첫 실행, 문제 해결
2. [simulator_core_guide.md](simulator_core_guide.md): scenario registry, factory/shipyard world, task generation, artifact export
3. [scenario_timing.md](scenario_timing.md): scenario별 Task/Primitive 삼각분포, 이동시간, machine cycle timing
4. [decision_logic.md](decision_logic.md): decision mode, rolling horizon pool/dispatch, dedicated roles
5. [humanoid_worker_model.md](humanoid_worker_model.md): ManSim worker가 HumanoidSim state/task/incident를 사용하는 방식
6. [humanoid_movement_model.md](humanoid_movement_model.md): pathfinding, reservation, traffic wait, replay interpolation
7. [replay_dashboards.md](replay_dashboards.md): Results Hub, KPI, Gantt, 3D Replay Studio
8. [llm_wiki_curator.md](llm_wiki_curator.md): optional LLM Wiki, Curator, Graphify
9. [openclaw_adaptive_priority_call_flow.md](openclaw_adaptive_priority_call_flow.md): OpenClaw manager loop
10. [v0.6_release_notes.md](v0.6_release_notes.md): v0.6 변경 사항, ADP와 정책 비교, 검증 범위
11. [v0.5_release_notes.md](v0.5_release_notes.md): v0.5 이전 릴리스 이력

Simulation-Based ADP의 worker 3 기본학습, checkpoint validation, cyclic beam search와 held-out
정책 비교 계약은 [decision_logic.md](decision_logic.md#simulation-based-adp)에 정리되어 있습니다.
현재 기본 학습은 초기부터 상한 `n=30` TD와 target network를 사용합니다. 설정, replay와 그래프 해석은
[adp_n_step_td.md](adp_n_step_td.md)를 참고하세요. 기존 MC profile도 별도로 보존되어 있습니다.
학습 중에도 Codex를 사용할 수 있는 독립 실행과 실시간 진행 화면은
[adp_background_training.md](adp_background_training.md)에 정리되어 있습니다.
학습 그래프의 구성, 산출식과 해석상 주의점은 [adp_training_dashboard.md](adp_training_dashboard.md)를 참고하세요.
효율 개선 전 복구 기준과 변경 원칙은 [adp_efficiency.md](adp_efficiency.md)에 기록합니다.
학습 오차의 해석 범위와 독립 MC·반복 행동 검증은 [adp_value_validation.md](adp_value_validation.md)를 참고합니다.

기본 정책 비교는 `mfg_flow_shop`, 5일 throughput, worker 3과 held-out seed 5개에서
`simulation_based_adp`, `immediate_shared`, `random_feasible_dispatch`를 비교하는 15-run
실험입니다. 이전 4-policy·두 목적함수 32-run 실험은 별도 profile로 보존됩니다. 실행법과 통합
대시보드는 [Manufacturing Policy Comparison README](../experiments/factory_policy_comparison/README.md)를 참고합니다.

## Scenario 문서 기준

`factory_mfg_basic`은 Warehouse, Station 1, Station 2, Inspection을 거치는 제조 공정입니다. Accepted product, scrap, queue wait, machine state, humanoid state가 주요 관찰 대상입니다.

`mfg_flow_shop`은 Station 1/2에 설비를 두 대씩 두고 유한 input/output buffer, worker별 전용 charging dock와 단일 Inspection Desk를 사용하는 제조 공정입니다. 실제 가공 누적시간 기준 고장과 예방정비를 반영하고, handover와 battery delivery/swap은 제외하며 machine repair만 공동 작업을 허용합니다.

이 시나리오는 `objective.mode=minimize_makespan|maximize_throughput`을 지원합니다. Makespan은 time-zero warehouse material의 product/폐기 계보가 모두 종결될 때 조기 종료하고 중간 보충을 하지 않으며, Throughput은 매일 material을 보충하고 설정된 일수까지 실행합니다. 세부 계약과 실행 예시는 [simulator_core_guide.md](simulator_core_guide.md#flow-shop-objective-and-termination)를 참고합니다.

네 rule-based 대표 mode는 계속 사용할 수 있지만 기본 ADP 정책 비교에는 `immediate_shared`만
포함합니다. 세부 `TRANSFER` 경로, `LOAD_MACHINE` slot, inspection desk load/inspect/unload와
예방정비를 구분한 19개 역할을 공통으로 사용합니다. Dedicated mode는 역할 1~16과 19를 예상 busy
time 기반 deterministic LPT로 배정하고, 자기 충전 역할 17과 공동수리 역할 18은 모든 worker에게
부여합니다. 설정과 계산 과정은 [decision_logic.md](decision_logic.md#mfg_flow_shop-대표-정책-4개)를 참고합니다.

Rolling mode는 worker polling 기반이 아닙니다. 독립 strict-periodic coordinator가 기본 `5`분 간격의 정확한 경계에서 미시작 queue를 회수하고 전체 후보를 재검증한 뒤 재할당합니다. `mfg_flow_shop`에서는 critical repair만 경계를 기다리지 않고 idle worker queue를 갱신할 수 있으며, battery charge는 강제 삽입하지 않고 정책이 선택합니다.

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
- battery가 이미 방전된 worker의 추가 tile 이동 금지
