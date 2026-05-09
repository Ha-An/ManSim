# LLM Wiki / Curator Pipeline

`openclaw_adaptive_priority`는 run 결과를 `knowledge/llm_knowledge/` 아래에 LLM Wiki와 knowledge graph로 누적할 수 있습니다. 목적은 raw data를 다시 저장하는 것이 아니라, 다음 manager가 더 좋은 판단을 하도록 reusable operational knowledge를 만드는 것입니다.

## 저장 위치

기본 설정은 experiment별 격리입니다.

```text
knowledge/llm_knowledge/
  experiments/
    <experiment-id>/
      raw/
      wiki/
      graph/
      curator_trace/
```

`experiment_scope: auto`이면 Hydra run마다 새 experiment directory가 만들어집니다. 코드 수정 중 여러 번 실험해도 서로 덮어쓰지 않습니다.

## Raw와 Wiki의 역할

Raw는 증거 보관소입니다.

- `raw/Run-0001/Day-0001/day_bundle.json`
- `raw/Run-0001/run_kpi.json`
- `raw/Run-0001/run_metadata.json`
- manager request/response trace

Wiki는 운영 지식 저장소입니다.

- 어떤 조치가 어떤 결과와 연결되었는지.
- 어떤 bottleneck이 반복되었는지.
- 어떤 worker/equipment/item 관점의 risk가 있었는지.
- 어떤 manager guidance가 다음 run에서 재사용할 만한지.
- 어떤 lesson은 아직 검증되지 않았거나 tradeoff가 있는지.

Wiki에는 raw JSON 구조를 그대로 붙이지 않습니다.

## Wiki Schema

Obsidian-compatible vault 구조:

```text
wiki/
  00_Index.md
  Runs/
    Run-0001.md
  Days/
    Run-0001/
      Day-0001.md
  Managers/
    Strategist.md
    Reviewer.md
    Curator.md
  Managed/
    Queues/
      Inspection Output.md
    Equipment/
      Inspection Workbench.md
      Machines.md
    Workers/
      Workers.md
```

관점은 크게 두 축입니다.

- Managed-object view: item, worker, equipment, queue 중심.
- Manager view: strategist/reviewer/curator의 판단, 조치, correction 중심.

## Daily Update

하루가 끝나면 Curator가 wiki update intent를 만듭니다. Deterministic writer는 이 intent와 day summary/reviewer report를 합쳐 Markdown과 raw trace를 갱신합니다.

기본 설정:

```yaml
llm:
  knowledge:
    curator:
      enabled: true
      update_frequency: daily
```

Daily update는 가볍게 유지해야 합니다. Prompt 입력도 compact digest만 사용하고, 전체 raw history를 manager prompt에 밀어 넣지 않습니다.

## Graphify Update

Graphify는 wiki vault를 knowledge graph로 변환합니다. 기본 cadence는 run-end입니다.

```yaml
llm:
  knowledge:
    graph:
      enabled: true
      provider: graphifyy
      backend: ollama
      model: mansim-gemma4-e4b
      base_url: http://127.0.0.1:8000/v1
      update_frequency: run
```

동작 원칙:

- Run 1: wiki를 기반으로 graph를 처음 생성합니다.
- Run 2 이후: 같은 experiment wiki를 기반으로 graph를 업데이트합니다.
- `graphify_history.jsonl`에 각 graph update 결과를 기록합니다.
- Graphify가 빈 graph를 내면 기존 non-empty graph를 보존합니다.
- Graphify가 실패하면 Obsidian wikilink fallback graph를 생성합니다.

주요 artifact:

- `graph/graph.json`
- `graph/graph.html`
- `graph/GRAPH_REPORT.md`
- `graph/graphify.log`
- `graph/graphify_history.jsonl`
- `graph/history/*.json`

## Manager 활용

Strategist와 Reviewer는 설정이 켜져 있을 때만 knowledge digest를 받습니다.

```yaml
llm:
  knowledge:
    enabled: true
    manager_usage:
      strategist: true
      reviewer: true
      curator: true
```

Prompt에는 아래 필드가 들어갑니다.

- `llm_wiki_digest`
- `knowledge_graph_digest`

Runtime workspace에는 아래 파일도 갱신됩니다.

- `LLM_WIKI.md`
- `KNOWLEDGE_GRAPH.md`

Manager는 지식을 활용하되, 현재 state와 충돌하면 현재 state를 우선해야 합니다.

## Dashboard

Results Hub에서 아래 메뉴로 접근합니다.

- `LLM Wiki` - Obsidian 앱으로 vault/index를 여는 링크와 browser preview.
- `Wiki Preview` - 브라우저에서 볼 수 있는 lightweight Markdown view.
- `Knowledge Graph` - Graphify graph를 앱 내부 dashboard로 표시. Network view가 기본이며 Tree, Communities, Edges, Raw JSON tab을 제공합니다.

Obsidian을 처음 쓰는 환경에서는 vault를 한 번 등록해야 `obsidian://open?...` 링크가 정상 동작합니다.

## 좋은 Wiki Entry 기준

좋은 entry:

- “Day 4에 closeout support를 강화하자 inspection output backlog가 7에서 4로 줄었다.”
- “Completed products는 늘지 않았으므로 closure 개선은 throughput gain이 아니라 downstream cleanup일 가능성이 있다.”
- “Battery prevention은 incident를 줄였지만 A2의 upstream feed time을 빼앗았다.”

나쁜 entry:

- raw JSON 전체 복사.
- `kpi.json` 값 나열만 있는 문서.
- 현재 run에서 한 번 관측된 내용을 확정적인 causal law처럼 쓰는 문장.

## 해석 주의

지식이 쌓인다고 자동으로 completed products가 증가하는 것은 아닙니다. Series 분석에서 completed products가 감소하고 closure만 좋아졌다면, manager가 downstream cleanup은 배웠지만 throughput objective를 충분히 반영하지 못한 것으로 봐야 합니다.
