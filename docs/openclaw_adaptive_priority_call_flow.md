# OpenClaw Adaptive Priority Call Flow

`openclaw_adaptive_priority`는 ManSim의 현재 production LLM path입니다. 핵심 아이디어는 manager가 운영 의도를 만들고, deterministic compiler가 그 의도를 안전하게 실행 가능한 정책으로 바꾸는 것입니다.

## 전체 흐름

```text
run start
  -> day start state snapshot
  -> MANAGER_SHIFT_STRATEGIST
  -> deterministic policy compiler
  -> simulator worker execution
  -> MANAGER_DAILY_REVIEWER
  -> MANAGER_CURATOR
  -> daily memory/wiki update
run end
  -> run raw artifact
  -> Graphify graph update
  -> dashboard export
```

## Run Start

Runtime은 아래를 준비합니다.

- Hydra config tree
- simulator initial state
- run output directory
- OpenClaw runtime workspace
- manager workspace files
- LLM Wiki experiment directory
- local OpenClaw gateway/backend health check

Multi-run이면 `run_01`, `run_02`처럼 child output directory를 만들고, 같은 experiment knowledge root를 공유합니다. 따라서 1번 run에서 만든 wiki/graph를 2번 run부터 strategist와 reviewer가 digest로 읽을 수 있습니다.

## Day Start Snapshot

Strategist 입력에는 현재 운영 상태가 들어갑니다.

- completed products
- inspection backlog
- inspection output open count
- product/input/output buffer 상태
- broken machine
- battery risk
- closeout gap
- remaining horizon
- previous day review
- existing compiled policy snapshot
- LLM Wiki digest
- knowledge graph digest

현재 simulation state가 항상 authoritative합니다. Wiki와 graph는 과거 lesson이며, 현재 상태와 충돌하면 현재 상태를 우선합니다.

## Strategist Turn

`MANAGER_SHIFT_STRATEGIST`는 intent-only manager입니다.

출력 contract:

- `summary`
- `worker_roles`
- `operating_focus`
- `late_horizon_mode`
- `role_plan`
- `support_plan`
- `prevention_targets`
- `daily_targets`
- `plan_revision`

Strategist가 직접 만들지 않는 것:

- task instance assignment
- low-level priority map
- mailbox command
- hard commitment

## Deterministic Policy Compiler

Compiler는 LLM agent가 아니라 system stage입니다. Strategist intent, previous reviewer signal, scripted fallback을 합쳐 executable policy를 만듭니다.

주요 출력:

- canonical task priority bundle
- agent priority multiplier
- role focus
- support assist rule
- mailbox/focus seed
- battery, closeout, reliability safety floor

예시:

- `late_horizon_mode = closeout_drive`이면 inspection output과 warehouse transfer를 더 강하게 밀어줍니다.
- `prevention_targets = battery_instability`이면 recharge/low-battery guard를 강화합니다.
- `support_plan = A1 -> A3 closeout_support`이면 A1이 downstream transfer를 보조하도록 multiplier를 조정합니다.

## Worker Execution

Worker는 shop-floor executor입니다.

선택 순서:

1. hard constraint and safety guard
2. local feasible candidate
3. compiled role/focus/mailbox signal
4. deterministic priority dispatch

Inspection은 현재 simulator logic에서 한 번에 하나의 worker만 작업할 수 있도록 제한되어 있습니다. 여러 worker가 동시에 inspection workbench를 점유하지 않아야 합니다.

## Daily Reviewer Turn

`MANAGER_DAILY_REVIEWER`는 diagnosis-only manager입니다.

입력:

- completed-day summary
- KPI deltas
- final compiled policy snapshot
- target achievement state
- throughput, closeout, battery, reliability signal
- LLM Wiki/graph digest

출력:

- `target_misses`
- `top_failure_modes`
- `recommended_prevention_targets`
- `recommended_support_pair`
- `role_change_advice`
- `carry_forward_risks`

Reviewer는 raw metrics를 길게 반복하지 않고, 다음 날 strategist가 쓸 correction signal을 짧게 남겨야 합니다.

## Curator Turn

`MANAGER_CURATOR`는 LLM Wiki update intent를 만듭니다. 실제 Markdown 작성과 파일 저장은 deterministic writer가 수행합니다.

Curator가 해야 하는 일:

- 어떤 조치가 어떤 결과와 연결되었는지 기록합니다.
- 좋아진 점과 나빠진 점을 모두 남깁니다.
- item, worker, equipment 관점과 manager 관점을 분리합니다.
- future manager가 바로 쓸 수 있는 operational lesson으로 압축합니다.

Curator가 하지 말아야 하는 일:

- raw JSON 구조를 wiki에 그대로 복사.
- current run의 metric dump를 지식으로 위장.
- 근거 없는 causal claim 작성.

## Daily Memory And Wiki Update

매일 종료 시 기본적으로 아래가 갱신됩니다.

- `daily_summary.json`
- `day_summary_memory.json`
- `day_review_memory.json`
- `knowledge/llm_knowledge/experiments/<id>/raw/Run-000x/Day-000x/`
- `knowledge/llm_knowledge/experiments/<id>/wiki/Days/Run-000x/Day-000x.md`
- `knowledge/llm_knowledge/experiments/<id>/curator_trace/`

Wiki는 raw data store가 아닙니다. Raw는 `raw/`에 저장하고, wiki에는 운영관리 지식만 정리합니다.

## Run-End Graphify Update

기본 설정은 Graphify update를 run마다 한 번 수행합니다.

```yaml
llm:
  knowledge:
    graph:
      enabled: true
      update_frequency: run
```

첫 run에서는 graph를 새로 만들고, 다음 run부터는 같은 wiki vault를 기반으로 기존 graph를 업데이트합니다. Graphify 결과는 아래에 저장됩니다.

- `graph/graph.json`
- `graph/graph.html`
- `graph/GRAPH_REPORT.md`
- `graph/graphify_history.jsonl`
- `graph/history/*.json`

Graphify가 빈 graph를 반환하면 기존 non-empty graph를 보존하고 fallback wikilink graph를 사용합니다.

## Dashboard Export

Run 종료 후 hub가 생성됩니다.

- Results Hub
- Factory Replay
- Manager Replay
- LLM Wiki dashboard
- Knowledge Graph dashboard
- Series dashboard, multi-run일 때

Series dashboard는 completed products를 primary metric으로 해석합니다. Closure 개선만으로 positive로 분류하지 않습니다.
