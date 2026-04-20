# ManSim v0.4

ManSim은 제조 공정 시뮬레이터 위에 manager 계층과 deterministic worker 실행 계층을 얹어, 생산성·분산·운영정책을 실험하는 프레임워크입니다.

현재 기준 주요 경로는 다음과 같습니다.
- simulator core: `manufacturing_sim/`
- decision/orchestration: `agents/`
- runtime/artifact export: `runtime/`
- dashboards: `dashboards/`
- configs: `configs/`
- OpenClaw workspace templates: `openclaw/workspaces/`

## 핵심 모드
- `fixed_priority`
  - 정적 scripted baseline
- `adaptive_priority`
  - scripted priority adaptation baseline
- `fixed_task_assignment`
  - worker별 canonical task family allowlist를 강제하는 scripted mode
- `llm_planner`
  - legacy commitment-driven LLM mode
- `openclaw_adaptive_priority`
  - 현재 production LLM mode
  - strategist / deterministic policy compiler / daily reviewer closed loop

## `openclaw_adaptive_priority` 요약
실행 루프는 다음 순서로 고정됩니다.
1. `MANAGER_SHIFT_STRATEGIST`
2. deterministic policy compiler
3. deterministic worker execution
4. `MANAGER_DAILY_REVIEWER`

strategist는 저수준 priority/mailbox 생성자가 아니라 하루 운영 의도를 정하는 manager입니다.
- `worker_roles`
- `operating_focus`
- `late_horizon_mode`
- `role_plan`
- `support_plan`
- `prevention_targets`
- `daily_targets`
- `plan_revision`

deterministic policy compiler는 strategist 의도를 실행 가능한 정책으로 번역합니다.
- canonical task priority bundle 생성
- agent multiplier 생성
- mailbox / focus_window / assist_request 생성
- closeout / battery / reliability safety floor 적용

daily reviewer는 raw summary를 반복하지 않고, 다음날 교정을 위한 구조화된 진단만 남깁니다.
- `target_misses`
- `top_failure_modes`
- `recommended_prevention_targets`
- `recommended_support_pair`
- `role_change_advice`
- `carry_forward_risks`

## 주요 산출물
- `kpi.json`
- `daily_summary.json`
- `day_summary_memory.json`
- `day_review_memory.json`
- `shift_policy_history.json`
- `results_dashboard.html`
- `reasoning_dashboard.html`
- `openclaw_workspace_dashboard.html`

## 디렉터리 구조
- `manufacturing_sim/`
  - 상태 전이, 이동, 가공, 고장, PM, 배터리, inspection, event logging
- `agents/`
  - scripted controller, OpenClaw orchestration, decision modules, compiler/fallback logic
- `runtime/`
  - 실행 진입점, artifact export, dashboard wiring
- `dashboards/`
  - KPI, results, reasoning, workspace, replay, task-priority 뷰
- `configs/`
  - scenario, decision, worker, runtime, heuristic rules
- `openclaw/`
  - profile, gateway launcher, workspace template
- `docs/`
  - 아키텍처와 호출 흐름 문서
- `outputs/`
  - 실행 결과 artifact 루트

## 빠른 시작
현재 root 기본 설정이 `openclaw_adaptive_priority`가 아니면 명시적으로 지정해서 실행하면 됩니다.

### 기본 5일 run
```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority
```

### 1일 smoke run
```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority scenario.horizon.num_days=1 runtime.ui.auto_open_results=false
```

### 로컬 LLM 경로 기동
```powershell
.\install_openclaw_cli.ps1
.\start_vllm_gemma4_docker.ps1
.\openclaw\start_gateway.ps1
```

## 해석 가이드
- worker가 `decision_source=simulator_fallback`를 많이 보여도 manager failure를 뜻하지는 않습니다.
- 이 모드는 manager가 executable task를 직접 주는 구조가 아니라, intent와 compiled policy를 만들고 worker는 deterministic dispatcher가 실제 task를 고르는 구조입니다.
- reviewer는 summary repetition agent가 아니라 diagnosis agent입니다.
- `avg_attempts`는 total call count가 아니라 repair/retry count 평균으로 읽어야 합니다.
