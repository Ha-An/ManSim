# ManSim Docs

이 디렉터리는 ManSim의 simulator, decision loop, LLM Wiki, dashboard 문서를 담고 있습니다.

## 추천 읽기 순서

1. [simulator_core_guide.md](simulator_core_guide.md) - entity, event flow, runtime artifact.
2. [decision_logic.md](decision_logic.md) - decision mode, success metric, manager boundary.
3. [openclaw_adaptive_priority_call_flow.md](openclaw_adaptive_priority_call_flow.md) - production OpenClaw manager loop.
4. [llm_wiki_curator.md](llm_wiki_curator.md) - Curator, Obsidian vault, Graphify graph.
5. [replay_dashboards.md](replay_dashboards.md) - results hub, Replay Studio, graph/wiki dashboard.
6. [llm_prompt_design.md](llm_prompt_design.md) - prompt responsibility split.
7. [llm_planner_call_flow.md](llm_planner_call_flow.md) - legacy LLM planner flow.
8. [openclaw_native_loop_review.md](openclaw_native_loop_review.md) - local OpenClaw stack notes.

## 현재 주력 경로

`openclaw_adaptive_priority`가 현재 production LLM path입니다.

- Primary goal: completed products 최대화.
- Strategist: day-start 운영 의도 작성.
- Compiler: deterministic executable policy 생성.
- Workers: simulator 안에서 deterministic execution.
- Reviewer: day-end 진단과 다음 날 correction signal 작성.
- Curator: 운영 지식을 LLM Wiki와 knowledge graph로 정리.

`Humanoid_Tasks`는 별도 워크스페이스에서 관리되는 휴머노이드 task library입니다. 현재 ManSim 문서와 runtime은 기존 worker model을 기준으로 설명하며, Humanoid adapter는 다음 통합 단계의 범위입니다.

## 주요 Artifact

Run directory:

- `results_dashboard.html`
- `replay_studio_log.json`
- `replay_studio_layout.json`
- `manager_replay.json`
- `dashboard_manifest.json`
- `kpi.json`
- `daily_summary.json`
- `shift_policy_history.json`
- `day_review_memory.json`
- `day_summary_memory.json`
- `llm_wiki_dashboard.html`
- `knowledge_graph_dashboard.html`

Run-series directory:

- `run_series_summary.json`
- `series_analysis.json`
- `series_dashboard.html`
- `run_01/`, `run_02/`, ...

Knowledge directory:

- `knowledge/llm_knowledge/experiments/<experiment-id>/raw/`
- `knowledge/llm_knowledge/experiments/<experiment-id>/wiki/`
- `knowledge/llm_knowledge/experiments/<experiment-id>/graph/`
- `knowledge/llm_knowledge/experiments/<experiment-id>/curator_trace/`

## 해석 기준

Series dashboard와 문서에서 “개선”은 completed products를 1순위로 판단합니다. Closure ratio, inspection backlog, incident count는 보조 지표입니다. 예를 들어 closure가 좋아졌더라도 completed products가 감소했다면 knowledge impact는 `positive`가 아니라 `mixed` 또는 `negative`로 봐야 합니다.
