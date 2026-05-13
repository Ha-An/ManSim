# Decision Logic

이 문서는 ManSim의 decision mode와 성공 기준을 정리합니다.

## Primary Goal

최종 목적은 configured horizon 동안 `completed products`를 최대화하는 것입니다. 여기서 completed product는 inspection을 통과하고 warehouse까지 도착한 accepted product입니다.

Secondary signal은 중요하지만 목표 자체는 아닙니다.

- downstream closure ratio
- inspection output backlog
- product input wait
- battery instability
- machine reliability instability
- coordination incident
- physical incident
- replan blocker

Series dashboard와 run-to-run knowledge 평가는 completed products를 1순위로 보고, secondary signal은 throughput 결과를 설명하는 보조 지표로 사용합니다.

## Control Layers

### Simulator Core

`manufacturing_sim/`이 담당합니다.

- physical state transition
- time progression
- movement and processing
- setup, machine breakdown, preventive maintenance
- battery consumption and recharge
- feasible opportunity enumeration
- event logging and replay export source

### Repository Orchestration

저장소 루트의 runtime/agents layer가 담당합니다.

- decision mode 선택
- OpenClaw request/response assembly
- deterministic policy compilation
- run artifact export
- dashboard generation
- run-series knowledge handoff
- LLM Wiki and graph update

## Decision Modes

### `fixed_priority`

Deterministic scripted baseline입니다. LLM 없이 task priority만으로 dispatch합니다.

### `adaptive_priority`

Scripted baseline입니다. 공장 상태에 따라 priority를 조정합니다.

### `fixed_task_assignment`

Worker별 canonical task family allowlist를 강제하는 scripted mode입니다.

### `llm_planner`

Legacy LLM mode입니다. Planner가 commitment를 생성하고 worker가 commitment를 우선합니다. 현재 주력 path는 아닙니다.

### `openclaw_adaptive_priority`

현재 주력 LLM manager mode입니다. 기본 simulation path는 여전히 OpenClaw 없는 `adaptive_priority`이며, 이 mode는 LLM manager loop를 명시적으로 사용할 때 선택합니다.

- Strategist가 day-start intent를 생성합니다.
- Deterministic compiler가 intent를 executable policy로 변환합니다.
- Worker는 deterministic dispatch를 수행합니다.
- Reviewer가 day-end diagnosis와 next-day correction signal을 생성합니다.
- Curator가 reusable operational knowledge를 LLM Wiki와 graph로 정리합니다.

## Manager Boundary

`openclaw_adaptive_priority`에서 manager는 task instance를 직접 지정하지 않습니다.

- Strategist: “오늘 무엇을 더 밀어야 하는가”를 의도 수준으로 결정합니다.
- Compiler: 의도를 task weight, worker role, support rule, safety floor로 변환합니다.
- Reviewer: 결과를 진단하고 다음 날 반복해야 할 correction signal을 남깁니다.
- Curator: raw JSON을 복붙하지 않고 운영관리 지식을 요약합니다.

이 경계는 prompt size를 줄이고, LLM schema drift가 물리 실행을 망가뜨리지 않게 하기 위한 것입니다.

## Knowledge Use

Knowledge는 현재 사실보다 우선하지 않습니다. Strategist와 Reviewer는 config에서 활성화된 경우에만 compact digest를 입력으로 받습니다.

관련 설정:

```yaml
llm:
  knowledge:
    enabled: true
    manager_usage:
      strategist: true
      reviewer: true
      curator: true
```

입력되는 지식은 두 종류입니다.

- `llm_wiki_digest`: Obsidian-compatible wiki에서 뽑은 compact operational lesson.
- `knowledge_graph_digest`: Graphify graph에서 뽑은 concept/relation 요약.

## Success Criteria

우선순위는 아래 순서입니다.

1. Completed products가 baseline 또는 이전 run보다 증가하는가.
2. 같은 completed products라면 closure ratio와 ending backlog가 좋아지는가.
3. Incident, blocker, escalation이 줄어드는가.
4. Lead time과 wait time이 악화되지 않는가.
5. Manager decision이 반복 가능한 lesson을 실제 policy로 반영하는가.

Completed products가 떨어졌는데 closure만 올랐다면 “좋아졌다”고 보지 않습니다. 그런 경우는 throughput tradeoff로 분류하고 원인을 따로 봐야 합니다.
