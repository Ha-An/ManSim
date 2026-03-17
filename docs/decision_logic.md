# 의사결정 로직 개요

## 공통 전제

모든 모드는 동일한 공장 환경과 task family를 사용한다.

- 공정 흐름: `Station1 -> Station2 -> Inspection`
- 지원 작업: setup, unload, transfer, material supply, inspection, repair, PM, battery 관련 작업
- 하드 제약:
  - feasible task만 선택 가능
  - 존재하지 않는 machine, agent, stage를 만들 수 없음
  - runtime dispatch는 candidate task 집합 안에서만 이루어짐

## 4개 decision mode

### `adaptive_priority`
- 규칙 기반 모드
- observation과 rule set을 바탕으로 task-family priority를 적응적으로 조정한다
- planner나 selector용 LLM 호출은 없다

### `fixed_priority`
- 규칙 기반 baseline 모드
- task-family priority weight는 고정하고, quota와 norm만 제한적으로 반영한다
- planner나 selector용 LLM 호출은 없다

### `llm_planner`
- LLM이 하루 단위 planning을 담당한다
- 핵심 단계:
  - `reflect`
  - `propose_jobs`
  - townhall round plan
  - townhall rounds
  - townhall synthesis
  - `urgent_discuss`(설정 시)
- runtime dispatch는 엔진이 수행하지만, 엔진이 참조하는 shared baseline과 agent overlay는 LLM 결과로 갱신된다

### `llm_task_selector`
- `llm_planner` 구조를 유지한다
- 추가로 runtime next-task 선택도 LLM이 담당한다
- engine이 feasible candidates를 만들고, selector가 그 안에서 하나를 고른다

## LLM 모드의 우선순위 구조

LLM 모드는 공통 baseline과 개인 overlay를 함께 사용한다.

### shared baseline
- `shared_task_priority_weights`
- 팀 전체가 공유하는 task-family priority baseline
- `propose_jobs`가 매일 갱신한다

### agent overlay
- `agent_priority_multipliers`
- 각 agent의 task-family별 개인 overlay
- 초기값은 모든 agent가 `1.0`
- day 종료 경험 집계와 townhall synthesis를 통해 서서히 달라질 수 있다

### effective priority
- runtime 점수는 shared baseline과 개인 overlay를 함께 반영한다
- 개념적으로는 아래와 같다
- `task.priority * shared_task_priority_weight * agent_priority_multiplier`

## norms

norm은 shared team-level planning reference다.

현재 대표 norm 예시:
- `min_pm_per_machine_per_day`
- `inspect_product_priority_weight`
- `inspection_backlog_target`
- `max_output_buffer_target`
- `battery_reserve_min`

중요한 점:
- norm은 hard constraint가 아니다
- planner가 baseline을 만들 때 참고하는 지속형 기준값이다

## urgent discuss

`urgent_discuss`는 mode 공통 인터페이스이지만, 구현은 mode에 따라 다르다.

- heuristic 모드: 규칙 기반 업데이트
- LLM 모드: LLM이 이벤트 기반 임시 조정을 생성

각 preset 파일에는 아래 토글이 있다.

```yaml
urgent_discuss:
  enabled: true
```

## runtime dispatch 차이

### `llm_planner`
- LLM은 day-level planning을 만든다
- runtime next-task 선택은 엔진이 한다

### `llm_task_selector`
- engine이 candidate task를 만든다
- LLM selector가 현재 agent 상태, plant state, candidate task, 개인 priority profile을 보고 next task를 선택한다
- LLM이 실패하면 engine fallback이 동작한다

