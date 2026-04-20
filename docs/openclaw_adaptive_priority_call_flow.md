# OpenClaw Adaptive Priority 호출 흐름

이 문서는 현재 production 경로인 `openclaw_adaptive_priority`의 호출 흐름을 설명합니다.

## 핵심 개념
- strategist가 하루 시작 시 운영 의도를 결정합니다.
- deterministic policy compiler가 그 의도를 실행 가능한 정책으로 번역합니다.
- worker는 compiled policy를 따라 deterministic하게 실행합니다.
- daily reviewer가 하루 종료 시 진단을 남기고 다음날 교정 루프를 닫습니다.

## 런타임 흐름
`shift strategist -> deterministic policy compiler -> worker execution -> daily reviewer`

## 1. Run 시작
- config tree 로드
- simulator state 초기화
- OpenClaw runtime workspace 생성
- gateway/backend health 확인

## 2. Shift Strategist turn
strategist 입력에는 다음이 들어갑니다.
- objective와 time context
- operating state
  - inspection backlog
  - buffers
  - completed products
  - broken machines
  - low-battery / discharged agents
  - closeout gap
  - inspection output open count
  - late-horizon closeout pressure
- top opportunities
- current compiled policy snapshot
- `previous_day_review`
- norm targets

strategist 출력 contract는 intent-only입니다.
- `summary`
- `worker_roles`
- `operating_focus`
- `late_horizon_mode`
- `role_plan`
- `support_plan`
- `prevention_targets`
- `daily_targets`
- `plan_revision`

strategist는 다음 항목을 직접 내지 않습니다.
- `task_priority_weights`
- `agent_priority_multipliers`
- `mailbox_seed`
- commitments

## 3. Deterministic Policy Compiler
compiler 입력
- strategist directive
- current operating state
- previous-day reviewer report
- scripted fallback baseline

compiler 출력
- canonical task priority bundle
- agent multiplier bundle
- mailbox / focus windows / assist requests
- closeout / battery / reliability safety floors

예시
- `support_plan = A1->A3 + closeout_support`
  - `A3`의 inspection/closeout lane 강화
  - `A1`의 unload/transfer support lane 생성
- `prevention_targets = [battery_instability]`
  - battery helper coverage와 battery task floor 강화
- `late_horizon_mode = closeout_drive`
  - generic flow보다 closeout bundle을 우선

## 4. Worker Execution
worker는 다음 입력을 받습니다.
- local observation
- feasible candidates
- current compiled policy
- mailbox / focus windows

선택 순서
1. hard constraint / safety guard
2. worker local response
3. compiled mailbox / focus
4. deterministic priority dispatch

## 5. Daily Reviewer turn
reviewer 입력
- raw completed-day summary
- final compiled policy snapshot
- daily target achievement state
- 주요 throughput / closeout / battery / reliability signals

reviewer 출력은 diagnosis-only입니다.
- `target_misses`
- `top_failure_modes`
- `recommended_prevention_targets`
- `recommended_support_pair`
- `role_change_advice`
- `carry_forward_risks`

reviewer는 다음을 하지 않습니다.
- raw metrics를 prose로 반복
- mailbox나 priority map 생성
- commitments 또는 task assignment 생성

## 6. End-of-day memory pipeline
artifact는 두 층으로 나뉩니다.

### Raw summary artifacts
- `daily_summary.json`
- `day_summary_memory.json`

### Review artifacts
- `day_review_memory.json`
- `MANAGER_DAILY_REVIEWER` 아래 reviewer workspace 파일

다음날 strategist는 raw prose summary가 아니라 reviewer output을 읽습니다.

## 7. End-of-run artifacts
주요 파일
- `kpi.json`
- `daily_summary.json`
- `day_summary_memory.json`
- `day_review_memory.json`
- `shift_policy_history.json`
- `results_dashboard.html`
- `reasoning_dashboard.html`

## 해석 포인트
이 모드는 strategist / compiler / reviewer closed loop로 읽으면 됩니다.
핵심은 manager가 고수준 의도를 만들고, deterministic compiler가 이를 실행 가능한 정책으로 안정화하는 구조라는 점입니다.
