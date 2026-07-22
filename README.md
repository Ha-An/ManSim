# ManSim

ManSim은 휴머노이드 작업자가 포함된 공정 시뮬레이션을 실행하고, 결과를 Hub, KPI dashboard, Gantt chart, 3D Replay Studio로 확인하는 discrete-event simulation 워크스페이스입니다.

현재 릴리스 기준은 **ManSim v0.5.0**입니다.

Worker의 State, Task, Primitive, Incident 정의는 `HumanoidSim`이 소유합니다. ManSim은 시나리오에서 어떤 작업 후보와 사건이 발생했는지 판단하고, HumanoidSim runtime을 통해 state transition과 task execution trace를 기록합니다.

![Replay Studio factory replay](docs/assets/replay-studio-worker-replay.png)

## v0.5 Highlights

- `factory_mfg_basic`과 `shipyard_basic`을 scenario plugin으로 분리하고 공통 Hub/Replay artifact contract를 사용합니다.
- rolling horizon aging/dedicated-role 정책에 stable task id, multi-task worker queue, requeue, immediate trigger를 적용합니다.
- `bottleneck_aware_dispatch`와 OR-Tools 기반 `rolling_horizon_throughput_optimizer`를 추가했습니다.
- HumanoidSim primitive difficulty에서 task complexity, cumulative complexity, OTC를 계산합니다.
- Factory 실행 전에 6개 multi-humanoid 운영 지표를 계산하는 Pre-Run Diagnostics를 생성합니다.
- worker 수 3~8대, 6개 정책, 5개 seed를 비교하는 factory policy experiment suite를 제공합니다.
- 공통 Results Hub, KPI, Gantt, 3D Replay Studio와 artifact/KPI audit를 정리했습니다.

세부 변경 사항은 [v0.5 Release Notes](docs/v0.5_release_notes.md)를 참고합니다.

## Current Focus

- 기본 연구 workflow는 `factory_mfg_basic`의 6개 정책 비교 실험입니다.
- 단일 run의 기본 decision mode는 `rolling_horizon_dedicated_roles`이고 기본 scenario는 `factory_mfg_basic`입니다.
- Results Hub는 모든 scenario가 공유하는 공통 UI입니다.
- 시각 replay의 기본 진입점은 `Replay Studio 3D`입니다. 2D Replay artifact는 호환용으로 남아 있지만 Hub 메뉴에는 노출하지 않습니다.

## Scenarios

ManSim은 `scenario.type`으로 scenario plugin을 선택합니다.

| Scenario | 설명 | 주요 KPI |
| --- | --- | --- |
| `factory_mfg_basic` | Warehouse, Station 1, Station 2, Inspection을 거치는 제조 공정입니다. | accepted products, scrap rate, machine utilization, worker state/time |
| `shipyard_basic` | 중앙 선박 외관 surface tile을 `WELD_SEAM -> PREPARE_SURFACE -> PAINT_SURFACE -> VERIFY_SHIP_SECTION` 순서로 처리하는 조선소 공정입니다. | `makespan_min`, completed surface tiles, quality pass rate, cart logistics |

## Requirements

- **Python 3.12 이상**: HumanoidSim의 최소 지원 버전입니다.
- **Git**: ManSim과 HumanoidSim 저장소를 내려받는 데 사용합니다.
- **HumanoidSim 저장소**: 기본 설치 예시는 `ManSim`과 같은 상위 폴더에 둡니다.
- **Node.js 20.19 이상과 npm (선택)**: 개별 run의 3D Replay Studio를 볼 때만 필요합니다. Node.js 24를 권장합니다.

권장 폴더 구조:

```text
C:\Github\
  ManSim\
  HumanoidSim\
```

기본 설치 프로필은 `factory_mfg_basic`에서 6개 정책을 비교하는 실험 suite입니다. `requirements.txt`에는 simulation, OR-Tools optimizer, 비교 집계와 정적 dashboard에 필요한 라이브러리만 포함됩니다. LLM/OpenClaw/Knowledge Graph 및 legacy 2D Replay 라이브러리는 기본 설치에 포함하지 않습니다.

## Installation

처음 설치하는 Windows 사용자는 아래 블록을 순서대로 실행하면 됩니다. ManSim과 HumanoidSim은 같은 상위 폴더 아래에 clone합니다.

```powershell
cd C:\Github
git clone https://github.com/Ha-An/ManSim.git
git clone https://github.com/Ha-An/HumanoidSim.git
cd C:\Github\ManSim
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\HumanoidSim
.\.venv\Scripts\python.exe -m humanoidsim validate-catalog
.\.venv\Scripts\python.exe -c "from ortools.sat.python import cp_model; import humanoidsim; print('Experiment environment OK')"
.\.venv\Scripts\python.exe -m pip check
```

`py -3.12`가 인식되지 않으면 Python 3.12 설치 경로의 `python.exe -m venv .venv`를 사용합니다.

HumanoidSim을 다른 경로에 내려받았다면 마지막 install 명령의 경로를 해당 저장소 경로로 바꿉니다. `-e` editable install을 사용하므로 HumanoidSim의 task/primitive 정의를 수정하면 ManSim이 같은 working tree의 최신 정의를 읽습니다.

설치 확인과 실험 명령 확인:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --dry-run
```

이 명령은 기본 설정의 6개 정책, worker 3~8대, seed 5개 조합인 180개 run을 출력만 하고 simulation은 실행하지 않습니다. Linux/macOS 명령과 선택 기능, 설치 확인, 문제 해결은 [Installation Guide](docs/installation.md)를 참고합니다.

실제 실행 확인은 먼저 다음 smoke experiment로 수행합니다. 완료되면 audit, 결과 집계와 비교 dashboard 생성까지 확인할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --worker-counts 3 --seeds 2026 --days 1
```

이 smoke 명령이 통과하면 기본 6정책 실험 환경이 준비된 것입니다. 기본 full experiment는 180개 5일 run이므로 충분한 실행 시간과 저장 공간을 확보한 뒤 실행합니다. 6정책 비교 자체에는 Node.js, LLM API, OpenClaw, ROS2 또는 Knowledge Graph 라이브러리가 필요하지 않습니다.

## Run

### Six-Policy Experiment

기본 연구 환경은 다음 6개 정책을 동일한 factory scenario, worker count, seed, horizon에서 비교합니다.

- `fixed_priority`
- `adaptive_priority`
- `rolling_horizon_aging_priority`
- `rolling_horizon_dedicated_roles`
- `bottleneck_aware_dispatch`
- `rolling_horizon_throughput_optimizer`

짧은 smoke experiment:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --worker-counts 3 5 --limit 6 --days 1
```

기본 full experiment는 6개 정책, worker 3~8대, seed 5개, 5일 horizon의 180개 run입니다.

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py
```

결과는 `experiments/factory_policy_comparison/results/<timestamp>/comparison_dashboard.html`에서 비교할 수 있습니다. 정상 종료 후 비교 dashboard가 기본 브라우저에서 자동으로 열립니다. GUI가 없는 서버에서는 `--no-open-dashboard`를 추가합니다. 전체 실험은 큰 저장 공간과 긴 실행 시간이 필요하므로 먼저 dry run과 smoke experiment를 권장합니다.

### Single Run

단일 simulation의 기본 설정은 `factory_mfg_basic`, `rolling_horizon_dedicated_roles`, seed `2026`, 5일 horizon입니다. Results Hub는 자동으로 열리고, Node.js frontend가 준비된 경우 3D Replay Studio도 자동으로 시작합니다.

기본 5일 run:

```powershell
.\.venv\Scripts\python.exe main.py
```

Factory 5일 run:

```powershell
.\.venv\Scripts\python.exe main.py scenario=factory_mfg_basic scenario.horizon.num_days=5
```

Shipyard 5일 run:

```powershell
.\.venv\Scripts\python.exe main.py scenario=shipyard_basic scenario.horizon.num_days=5
```

최근 run Hub 열기:

```powershell
.\.venv\Scripts\python.exe -m dashboards.manifest --latest
```

브라우저를 자동으로 열지 않는 batch run은 다음 override를 추가합니다.

```powershell
.\.venv\Scripts\python.exe main.py runtime.ui.auto_open_results=false runtime.ui.auto_start_replay_studio_3d=false
```

## Experiment Decision Modes

| Mode | 설명 |
| --- | --- |
| `rolling_horizon_dedicated_roles` | Window 동안 모은 task를 worker별 전담 task allowlist와 aging rank로 dispatch합니다. 기본 mode입니다. |
| `rolling_horizon_aging_priority` | Scenario별 task code priority와 기다린 window 수를 기준으로 dispatch합니다. |
| `bottleneck_aware_dispatch` | Factory task 후보를 즉시 평가해 bottleneck relief, downstream progress, machine continuity가 큰 task를 우선 선택합니다. |
| `rolling_horizon_throughput_optimizer` | Factory rolling window pool을 OR-Tools CP-SAT로 최적화해 worker별 dispatch queue를 구성합니다. OR-Tools가 필수입니다. |
| `adaptive_priority` | 공정 상태에 따라 scripted priority를 조정하는 baseline입니다. |
| `fixed_priority` | 고정 priority baseline입니다. |

Rolling horizon 계열은 scenario별 priority 설정을 사용합니다. 설정 위치:

- `configs/decision/rolling_horizon_aging_priority.yaml`
- `configs/decision/rolling_horizon_dedicated_roles.yaml`
- `configs/decision/bottleneck_aware_dispatch.yaml`
- `configs/decision/rolling_horizon_throughput_optimizer.yaml`

Factory와 Shipyard는 task set이 다르므로 `scenario_task_code_priority_order.<scenario>`와 `scenario_worker_task_priority.<scenario>` 아래에서 따로 설정합니다.
`bottleneck_aware_dispatch`와 `rolling_horizon_throughput_optimizer`는 factory throughput 비교 실험용 mode입니다. `rolling_horizon_throughput_optimizer`는 fallback 없이 OR-Tools CP-SAT를 사용하므로, 실행 전 `requirements.txt`로 `ortools`가 설치되어 있어야 합니다.

`fixed_task_assignment`, `llm_planner`, `openclaw_adaptive_priority`는 비교 실험 대상이 아닌 optional/legacy mode입니다. 이를 사용할 때만 `requirements-optional.txt`와 각 integration 설정을 추가로 설치합니다.

## Runtime Notes

- Worker 이동은 tile path와 strict reservation 기반입니다.
- Worker battery drain은 scenario config의 `worker.battery_drain`에서 조정합니다.
- 기본 drain multiplier는 `AVAILABLE=0.5`, non-available state는 `1.0`입니다. 따라서 작업/이동/대기 중인 worker는 idle available 상태보다 2배 빠르게 배터리를 사용합니다.
- 긴급 task trigger는 rolling horizon window를 기다리지 않고 worker queue에 먼저 반영할 수 있습니다. 기본 정책은 현재 task를 중단하지 않고, 완료 직후 우선 실행하는 `next_after_current`입니다.
- Machine repair KPI는 `BROKEN`과 `UNDER_REPAIR` 시간을 분리합니다. 수리가 취소되어 repair team이 비면 다시 `BROKEN` 시간으로 집계됩니다.
- OTC(Operational Task Complexity)는 HumanoidSim primitive difficulty weight를 기준으로 계산합니다. ManSim은 완료된 top-level task instance 수를 집계해 Hub에는 `OTC`와 `Cumulative Complexity`를 표시하고, KPI dashboard에는 task/primitive별 기여도를 표시합니다.
- Bottleneck/throughput policy run은 Hub와 KPI dashboard에 `Bottleneck Score`, optimizer solve count, optimizer objective를 추가로 표시합니다.
- Factory run은 사전 운영 진단을 함께 export합니다. Hub의 `Pre-Run Diagnostics` 메뉴에서 worker 부담 불균형, 공유자원 경쟁 가능성, traffic contention, service tile 부족, robot interaction load, power coordination risk의 계산값과 입력값을 확인할 수 있습니다.

## Experiment Details

결과 파일 구조, dedicated-role 확장 규칙, 공정성 검사와 KPI 집계 기준은 [Factory Policy Comparison README](experiments/factory_policy_comparison/README.md)에 정리되어 있습니다.

## Outputs

Run artifact는 `outputs/YYYY-MM-DD/HH-MM-SS/` 아래에 생성됩니다.

| 파일 | 용도 |
| --- | --- |
| `results_dashboard.html` | 공통 Results Hub |
| `kpi.json`, `kpi_dashboard.html` | KPI source와 dashboard |
| `gantt.html`, `gantt_segments.csv` | Gantt chart와 source data |
| `pre_run_diagnostics.json`, `pre_run_diagnostics.html` | Factory 사전 운영 진단 source와 dashboard |
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
- [docs/installation.md](docs/installation.md): 요구사항, 전체 설치, 실행, 문제 해결
- [docs/v0.5_release_notes.md](docs/v0.5_release_notes.md): v0.5 변경 내용과 호환성 기준
- [docs/simulator_core_guide.md](docs/simulator_core_guide.md): simulation core, scenario, task generation, KPI/export
- [docs/humanoid_worker_model.md](docs/humanoid_worker_model.md): HumanoidSim 기반 worker state/task/incident runtime
- [docs/humanoid_movement_model.md](docs/humanoid_movement_model.md): tile movement, reservation, traffic model
- [docs/decision_logic.md](docs/decision_logic.md): decision mode와 rolling horizon dispatch
- [docs/replay_dashboards.md](docs/replay_dashboards.md): Hub, KPI, Gantt, 3D Replay Studio
- [docs/llm_wiki_curator.md](docs/llm_wiki_curator.md): optional LLM Wiki, Curator, Graphify path
