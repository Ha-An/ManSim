# Decision Logic

## Goal
configured horizon 동안 accepted finished-product completion을 최대화하는 것이 1차 목표입니다.

Secondary signals는 이 목표에 영향을 줄 때만 중요합니다.
- closeout gap
- battery instability
- reliability instability
- flow blockage
- stage-2 underfeed

## Control Layers
### Simulator core
`manufacturing_sim/`이 담당합니다.
- physical state transitions
- time progression
- movement / processing
- machine breakdown / PM / setup dynamics
- battery consumption and recovery
- event logging
- feasible opportunity enumeration

### Repository-root orchestration
repository root가 담당합니다.
- decision modules
- OpenClaw request/response assembly
- deterministic policy compilation
- runtime artifact export
- dashboards

## Decision Modes
### `fixed_priority`
정적 scripted baseline.

### `adaptive_priority`
상황에 따라 priority를 조정하는 scripted baseline.

### `fixed_task_assignment`
priority-driven scripted mode이지만 worker별 canonical task family allowlist를 강제합니다.

### `llm_planner`
legacy commitment-driven LLM mode.
- planner가 `commitments`를 authoritative output으로 생성
- worker는 commitment를 우선 소비

### `openclaw_adaptive_priority`
current manager-only LLM mode.
- strategist가 day-start intent를 생성
- deterministic compiler가 low-level execution policy로 변환
- worker는 deterministic priority dispatch 수행
- daily reviewer가 day-end diagnosis를 만들고 다음날 strategist input으로 전달

## Worker Execution Model
workers는 shop-floor deterministic executor입니다.

입력
- local observation
- feasible candidates
- current compiled policy
- mailbox / focus windows / assists
- safety/local-response context

특징
- `openclaw_adaptive_priority`는 commitment-driven mode가 아닙니다.
- planner/manager가 exact task instance를 직접 할당하지 않습니다.
- strategist는 intent를 내고, compiler가 executable bias로 번역합니다.

## Manager Roles
### `openclaw_adaptive_priority`
- `MANAGER_SHIFT_STRATEGIST`
  - roles, focus, support intent, prevention targets, daily targets 생성
- `MANAGER_DAILY_REVIEWER`
  - target miss / failure mode / next-day correction signal 생성

### `llm_planner`
- detector
- evaluator(optional)
- planner
- run reflector

## Closed Loop Structure
### `openclaw_adaptive_priority`
1. strategist
2. compiler
3. deterministic execution
4. reviewer
5. next-day strategist receives `previous_day_review`

이 구조의 목적은 다음입니다.
- strategist reasoning 보존
- deterministic translation을 통한 실행 안정화
- day-boundary closed loop 형성
- run-to-run variance 감소

## Success Criteria
실무적인 기준은 다음입니다.
- accepted output이 baseline보다 개선되는가
- wall time이 악화되지 않는가
- closeout이 더 smooth해지는가
- variance가 줄어드는가
