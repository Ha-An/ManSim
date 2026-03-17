# `llm_planner` 호출 흐름

## 하루 단위 기본 흐름

`llm_planner`는 하루 기준으로 아래 순서로 동작한다.

1. `reflect`
2. `propose_jobs`
3. day runtime 진행
4. `townhall_round_plan`
5. `townhall_round` 반복
6. `townhall_synthesis`
7. norm 및 agent overlay 갱신

설정에 따라 `urgent_discuss`가 runtime 중간에 추가로 호출될 수 있다.

## 1. reflect

목적:
- plant-level 진단
- 현재 상태를 risk 중심으로 해석

출력:
- `summary`
- `flow_risks`
- `maintenance_risks`
- `inspection_risks`
- `battery_risks`
- `evidence`

중요:
- direct dispatch를 하지 않는다
- 고정 bottleneck을 가정하지 않는다

## 2. propose_jobs

목적:
- shared baseline task-family priority와 daily quota를 제안

입력:
- observation
- reflect 결과의 compact diagnosis
- current norms
- non-neutral agent priority profiles

출력:
- `task_priority_weights`
- `quotas`
- `rationale`

현재 구조에서 `rationale`은 communication language가 `KOR`이면 한국어로 반환된다.

## 3. runtime dispatch

`llm_planner`는 day-level planning만 담당한다.
실제 next-task dispatch는 엔진이 수행한다.

다만 엔진이 참조하는 priority는 아래를 함께 반영한다.
- shared baseline
- agent priority overlay

## 4. townhall_round_plan

목적:
- 하루 종료 후 townhall에 몇 라운드를 쓸지 결정
- 어떤 stage를 사용할지 결정

중요:
- moderator가 day summary를 보고 스스로 라운드 수를 정한다
- 최대 라운드 안에서 필요한 stage만 사용한다
- 항상 5단계를 다 거칠 필요는 없다

현재 stage 집합:
- `diagnose`
- `critique`
- `alternatives`
- `tradeoff`
- `synthesis`

원칙:
- `diagnose`로 시작
- `synthesis`로 종료
- 필요 없는 중간 단계는 생략 가능

## 5. townhall_round

각 agent가 round plan에 맞춰 발화한다.

발화 규칙:
- 반드시 아래 중 하나 이상 포함
  - 새로운 증거
  - 이전 제안의 약점
  - 다른 task family를 활용한 대안
  - short-term vs long-term trade-off
- 단순 동의나 반복 재진술은 금지

입력:
- compact day summary
- shared memory
- speaker 자신의 memory와 experience
- speaker 자신의 priority profile
- 다른 agent들의 lightweight specialization summary
- recent highlights

## 6. townhall_synthesis

목적:
- discussion을 shared norm과 agent별 specialization update로 정리

출력:
- `updated_norms`
- `agent_priority_updates`
- `summary`

원칙:
- one-off noise는 reject
- 기존 profile과 norm을 근거 없이 과도하게 ratchet하지 않음
- repeated experience와 discussion이 같이 specialization을 지지할 때만 agent profile을 더 분화

## 7. 경험 집계와 overlay 갱신

하루 종료 후 agent experience가 집계된다.

대표 신호:
- task family별 완료 횟수
- task family별 완료 시간
- interruption / skip
- contribution signal
- recent task events

그 뒤 아래 순서로 overlay가 갱신된다.

1. 경험 기반 업데이트
2. townhall synthesis의 `agent_priority_updates` blend

즉, 개인 overlay는 말만으로 바뀌지 않고 경험과 토론이 함께 반영된다.

## urgent_discuss

`urgent_discuss`는 별도 토글을 가진 runtime 이벤트 대응 경로다.

```yaml
urgent_discuss:
  enabled: true
```

발동 예시:
- machine breakdown
- agent discharged

역할:
- local correction 성격의 priority update
- day-level policy 전체를 다시 쓰는 용도가 아님
