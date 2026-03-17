# LLM 프롬프트 설계

## 목표

프롬프트 설계의 목적은 두 가지다.

- 공장 상태를 LLM이 충분히 이해할 수 있게 한다
- 같은 의미를 여러 번 반복하지 않아, 중요한 신호가 묻히지 않게 한다

현재 ManSim은 장황한 설명을 줄이는 대신, 공정 구조, task 의미, 상태 정보, 요약된 진단이라는 핵심 재료는 유지하는 방향을 사용한다.

## 시스템 프롬프트 구성

공통 시스템 프롬프트는 아래 내용을 담는다.

- global objective
- plant summary
- naming conventions
- core constraints
- phase별 지시문

필요한 phase에서만 추가로 넣는 내용:
- task family semantics
- shared norm semantics

### global objective
- 전체 horizon 안에서 accepted finished product 수를 최대화한다

### plant summary
- 공정 흐름: `Station1 -> Station2 -> Inspection`
- station별 input/output 의미
- cycle time, travel/setup/unload/repair/PM 시간에 대한 compact reference

### naming conventions
- agent ID: `A#`
- machine ID: `SXMY`
- location: `Warehouse`, `Station1..N`, `Inspection`, `BatteryStation`, `TownHall`

### core constraints
- feasible task만 선택 가능
- 존재하지 않는 task, machine, queue, process stage를 만들면 안 됨
- 고정 bottleneck을 가정하지 않고 현재 상태에서 추론해야 함

## 입력 JSON 구조

LLM-facing observation은 아래 블록으로 나뉜다.

- `time`
- `queues`
- `machines`
- `agents`
- `flow`
- `recent_history`
- `trends`

### time
현재 LLM prompt에는 아래 시간 정보만 남긴다.

- `day`
- `total_days`
- `days_remaining`
- `day_progress`
- `horizon_remaining_min`

절대시간 `sim_min`, `day_start_min`, `day_end_min`은 prompt에서 제외했다.

### queues
LLM-facing queue는 의미가 분명한 이름만 사용한다.

- `material.station1_input`
- `material.station2_input`
- `intermediate.station2_input`
- `inspection.inspection_input`
- `inspection.inspection_pass_output`
- `output_buffers.station1`
- `output_buffers.station2`

`component`라는 용어는 더 이상 사용하지 않고 `intermediate`로 통일했다.

### machines
planner용 observation은 전체 machine dump 대신 아래를 중심으로 본다.

- `summary`
- `wait_reason_summary`
- `focus_by_id`

`focus_by_id`에는 broken, waiting unload, ready for setup, owner lock 같은 이상 상태 machine만 남긴다.

### agents
planner용 observation은 아래만 유지한다.

- `summary`
- `focus_by_id`

`focus_by_id`에는 low battery, discharged, in transit, current task가 있는 agent만 남긴다.

## diagnosis 구조

`reflect`는 구조화된 진단을 반환한다.

- `summary`
- `flow_risks`
- `maintenance_risks`
- `inspection_risks`
- `battery_risks`
- `evidence`

다음 phase로 넘길 때는 중복을 줄이기 위해 `evidence`를 제외하고, 요약과 risk list만 전달한다.

## phase별 차이

### reflect
가장 얇은 prompt를 사용한다.

유지:
- objective
- plant summary
- core constraints
- observation

제거:
- task family semantics
- norm semantics
- memory

### propose_jobs
planning에 직접 필요한 재료를 유지한다.

유지:
- objective
- plant summary
- task family semantics
- compact norm semantics
- observation
- diagnosis summary / risks
- current norms
- non-neutral agent priority profiles

제거:
- diagnosis evidence
- neutral agent profiles
- 중복 output-buffer 표현

### townhall
대화 품질을 유지하되 중복을 줄인 구조를 사용한다.

- round plan은 task glossary 없이 day summary와 memory만 본다
- round prompt는 `peer_specialization_summary`만 사용하고, full team priority profile은 제거한다
- `recent_highlights`는 최근 핵심 포인트만 제한된 개수로 전달한다
- synthesis는 non-neutral profile과 compact highlights만 받는다

### selector
selector는 plant 전체 설명보다 현재 agent의 local decision에 필요한 재료를 우선한다.

유지:
- global objective
- compact task semantics
- current agent state
- local plant state
- candidate task list
- current agent priority profile
- experience summary

## 언어 설정

townhall 언어는 설정 파일에서 결정한다.

```yaml
llm:
  communication:
    language: KOR
```

- `KOR`: townhall 자연어 필드와 `rationale`을 한국어로 반환
- `ENG`: 자연어 필드를 영어로 반환

JSON key, enum 값, task key, norm key, agent/machine ID는 계속 영어를 사용한다.

## 프롬프트 축소 원칙

프롬프트를 줄일 때 기준은 단순한 삭제가 아니다.

유지해야 할 것:
- objective
- plant structure
- task meaning
- current state
- structured diagnosis

우선 줄일 수 있는 것:
- raw state를 반복하는 `diagnosis.evidence`
- neutral agent priority profile
- 중복 queue / output buffer 표현
- townhall에서 full team profile dump
- phase 목적과 맞지 않는 장문 glossary
