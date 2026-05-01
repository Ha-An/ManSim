# ManSim v0.4.1

ManSim은 제조/생산 시스템 시뮬레이션 연구를 위한 프레임워크입니다. 생산 정책, 작업자 오케스트레이션, 설비 신뢰성, 큐 동역학, 배터리 제약, 이벤트 기반 리플레이 대시보드를 함께 실험할 수 있도록 구성되어 있습니다.

현재 주력 LLM 의사결정 모드는 `openclaw_adaptive_priority`입니다. 이 모드는 전략가가 하루 운영 의도를 만들고, deterministic compiler가 이를 실행 가능한 정책으로 변환하며, 작업자가 시뮬레이터 안에서 실행하고, 리뷰어가 다음 날을 위한 결과를 요약하는 폐루프 구조입니다.

## 저장소 구조

- `manufacturing_sim/` - discrete-event 제조 시뮬레이터 코어와 시나리오 런타임.
- `agents/` - scripted, OpenClaw, adaptive-priority, orchestration 의사결정 모드.
- `configs/` - scenario, decision, worker, runtime, heuristic 설정.
- `runtime/` - 실행 엔트리포인트, artifact export, dashboard 생성, manifest 연결.
- `dashboards/` - results hub, replay 연결, manager replay export, 정적 dashboard.
- `replay_studio/` - React + TypeScript 기반 factory/manager replay viewer.
- `docs/` - 아키텍처 노트와 모드별 call-flow 문서.
- `openclaw/` - OpenClaw 기반 LLM 실행용 profile과 workspace template.

## 의사결정 모드

- `fixed_priority` - deterministic scripted baseline.
- `adaptive_priority` - scripted priority adaptation baseline.
- `fixed_task_assignment` - worker별 canonical task family 제약을 강제하는 scripted mode.
- `llm_planner` - legacy commitment-driven LLM mode.
- `openclaw_adaptive_priority` - 현재 주력 production LLM mode. Strategist, deterministic compiler, deterministic worker execution, Reviewer loop로 구성됩니다.

## openclaw_adaptive_priority 루프

각 시뮬레이션 day는 아래 순서로 진행됩니다.

1. `MANAGER_SHIFT_STRATEGIST`가 최근 공장 상태와 reviewer carry-forward memory를 읽습니다.
2. deterministic policy compiler가 strategy를 worker role, task weight, agent multiplier, mailbox seed message, safety floor로 구체화합니다.
3. worker는 deterministic dispatch와 local response rule을 통해 시뮬레이터에서 작업을 수행합니다.
4. `MANAGER_DAILY_REVIEWER`가 하루 결과를 진단하고 다음 날을 위한 carry-forward recommendation을 기록합니다.

Strategist는 실행 task를 직접 배정하지 않고 운영 의도를 담당합니다. Compiler는 그 의도를 실행 가능한 정책으로 바꾸는 deterministic system stage입니다. Reviewer는 구조화된 진단과 다음 날 가이드를 담당합니다.

## 협동 수리

기계 수리는 공유 가능한 작업으로 모델링됩니다.

- `scenario.machine_failure.max_repair_agents`가 수리 참여 worker 상한을 제어합니다. 기본값은 `3`입니다.
- 수리는 worker 1명으로 시작할 수 있고, 중간에 helper가 합류할 수 있습니다.
- 수리 속도는 활성 수리 인원수에 선형 비례합니다.
- 모든 worker가 수리에서 빠지면 남은 수리 작업량이 보존되고 수리는 일시정지됩니다.
- replay log에는 repair team size, remaining work, helper join/leave event, machine repair progress가 포함됩니다.

## 대시보드와 리플레이

시뮬레이션이 완료되면 `outputs/<date>/<time>/` 아래에 results hub와 run별 artifact가 생성됩니다.

주요 artifact는 아래와 같습니다.

- `results_dashboard.html` - 메인 results hub.
- `replay_studio_log.json`, `replay_studio_layout.json` - event-sourced factory replay 입력.
- `manager_replay.json` - manager pipeline replay payload.
- `manager_replay_dashboard.html` - 정적 manager replay fallback artifact.
- `dashboard_manifest.json` - Replay Studio가 사용하는 hub manifest.
- `kpi.json`, `daily_summary.json`, `shift_policy_history.json`, `day_review_memory.json`, `day_summary_memory.json` - 분석 및 manager memory artifact.

Replay Studio는 두 가지 view를 지원합니다.

- factory replay: worker, machine, queue, battery, inspection, movement, repair, incident animation.
- manager replay: input bundle -> Strategist -> compiler -> factory response -> Reviewer -> next-day carry-forward 흐름을 day 단위로 표현합니다.

## 빠른 시작

Python 환경을 준비하고 dependency를 설치합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

대시보드를 자동으로 열지 않는 1일 smoke simulation:

```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority scenario.horizon.num_days=1 runtime.ui.auto_open_results=false
```

5일 simulation:

```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority scenario.horizon.num_days=5
```

현재 config에 정의된 기본 설정 그대로 실행:

```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority
```

OpenClaw 기반 모드를 사용할 때 로컬 LLM/OpenClaw 서비스를 시작합니다.

```powershell
.\install_openclaw_cli.ps1
.\start_vllm_gemma4_docker.ps1
.\openclaw\start_gateway.ps1
```

Replay Studio 개발 서버 실행:

```powershell
cd replay_studio
npm install
npm run dev
```

Replay Studio build:

```powershell
cd replay_studio
npm run build
```

## 검증 명령

```powershell
.\.venv\Scripts\python.exe -m py_compile agents\openclaw_adaptive_priority.py agents\openclaw_orchestrated.py manufacturing_sim\simulation\scenarios\manufacturing\world.py runtime\entrypoint.py dashboards\manager_replay.py
cd replay_studio
npm run build
```

## 참고

- `outputs/`, `.venv/`, `node_modules/`, `dist/`, `.tooling/`, local backup folder는 git에서 제외합니다.
- Manager Replay는 hub manifest를 통해 연결되지만, 기본 payload인 `manager_replay.json`은 독립적으로 재사용할 수 있습니다.
- Replay Studio는 정규화된 log를 소비하며, render layer가 simulator internals에 의존하지 않도록 설계되어 있습니다.
