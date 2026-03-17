# `llm_task_selector` 호출 흐름

## 개요

`llm_task_selector`는 `llm_planner`의 planning 구조를 유지하면서, runtime next-task 선택도 LLM이 담당하는 모드다.

즉, 하루 단위로는 planner처럼 동작하고, 실행 중에는 selector가 candidate 중 하나를 선택한다.

## planner 단계

planner 단계는 `llm_planner`와 거의 같다.

- `reflect`
- `propose_jobs`
- `townhall_round_plan`
- `townhall_round`
- `townhall_synthesis`
- `urgent_discuss`(설정 시)

이 단계에서 shared baseline과 agent overlay가 갱신된다.

## runtime next-task 선택 흐름

runtime에서 각 agent는 아래 순서로 next task를 정한다.

1. world가 feasible candidate task를 생성
2. 하드 제약 검사
3. single candidate면 바로 선택
4. 복수 candidate면 LLM selector 호출
5. LLM 실패 시 fallback 사용

## selector 입력

selector는 현재 agent의 로컬 의사결정에 필요한 정보만 받는다.

대표 블록:
- `agent`
- `plant_state`
- `current_policy`
- `candidate_tasks`
- `selection_rules`

### current_policy
- shared baseline priority
- 현재 agent의 priority profile
- 현재 agent의 effective priority
- compact strategy diagnosis

### agent experience
selector는 현재 agent의 개인 경험 요약도 받는다.

예시:
- 최근 많이 완료한 task family
- contribution signals
- recent task events
- 최근 personal memory

이 구조 덕분에 같은 candidate set이 주어져도 agent마다 선택이 달라질 수 있다.

## agent별 priority 사용 방식

selector는 shared baseline만 보지 않는다.

- shared baseline은 팀 공통 방향
- agent overlay는 개인 성향
- selector는 둘이 결합된 effective priority와 current local state를 함께 본다

즉, 시작은 같은 baseline이라도 시간이 지나면 A1, A2, A3의 선택 성향이 달라질 수 있다.

## fallback

LLM selector가 실패하면 fallback이 동작한다.

fallback 상황 예시:
- malformed JSON
- candidate ID 불일치
- 허용되지 않은 task 반환

fallback은 engine 쪽 deterministic scoring으로 candidate 중 하나를 고른다.

## 로그와 시각화

관련 로그:
- `PHASE_JOB_ASSIGNMENT`
- `AGENT_PRIORITY_PROFILE_UPDATE`
- `llm_exchange.json`

관련 대시보드:
- `task_priority_dashboard.html`
  - shared baseline line chart
  - agent별 effective priority line chart
- `llm_trace.html`
  - planner / selector 호출과 응답 확인

## townhall과 selector의 역할 차이

- townhall
  - 하루 종료 후 전략, norm, specialization을 논의
- selector
  - 지금 이 순간 어떤 task를 고를지 결정

즉, townhall은 day-level coordination이고 selector는 runtime dispatch다.
