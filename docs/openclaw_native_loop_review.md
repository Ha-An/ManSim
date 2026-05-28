# OpenClaw Native Loop Review

이 문서는 ManSim이 사용하는 local OpenClaw stack과 runtime workspace 구조를 요약합니다.

## 범위

대상 decision path:

- `openclaw_adaptive_priority`
- `llm_planner`, legacy 참고용

현재 root 기본 simulation path는 `rolling_horizon_dedicated_roles`입니다. OpenClaw manager를 명시적으로 켠 path는 `openclaw_adaptive_priority`이며, OpenClaw 없이 비교할 수 있는 scripted baseline으로 `adaptive_priority`도 유지합니다.

## Local Stack

기본 구성:

- local vLLM backend
- OpenClaw gateway
- ManSim runtime
- OpenClaw workspace templates

기본 실행 순서:

```powershell
.\install_openclaw_cli.ps1
.\start_vllm_gemma4_docker.ps1
.\openclaw\start_gateway.ps1
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority
```

기본 profile:

- `openclaw/profiles/mansim_repo/openclaw.json`
- model alias: `vllm/mansim-gemma4-e4b`
- backend model: `mansim-gemma4-e4b`
- gateway: `http://localhost:18789/v1`
- backend: `http://127.0.0.1:8000/v1`

## Runtime Workspace

Runtime은 run마다 임시 OpenClaw workspace를 준비합니다. 각 manager workspace에는 보통 아래 파일이 들어갑니다.

- `USER.md`
- `MEMORY.md`
- `KNOWLEDGE.md`
- `LLM_WIKI.md`
- `KNOWLEDGE_GRAPH.md`
- `facts/current_request.json`
- `facts/current_response_template.json`
- `facts/current_native_turn.json`
- `reports/*`
- `trace/*`

`LLM_WIKI.md`와 `KNOWLEDGE_GRAPH.md`는 config에서 manager knowledge usage가 켜져 있을 때 compact digest로 갱신됩니다.

## Manager Set

`openclaw_adaptive_priority`:

- `MANAGER_SHIFT_STRATEGIST`
- `MANAGER_DAILY_REVIEWER`
- `MANAGER_CURATOR`

Multi-run에서는 run-level reflection과 knowledge handoff도 함께 사용됩니다.

Legacy `llm_planner`:

- detector
- evaluator
- planner
- run reflector

## 운영 장점

- Strategist reasoning을 보존하면서 execution은 deterministic compiler가 안정화합니다.
- Manager request/response artifact를 turn 단위로 남깁니다.
- Workspace 파일을 통해 OpenClaw 입력을 직접 검사할 수 있습니다.
- LLM Wiki와 knowledge graph를 manager input으로 주입할 수 있습니다.

## 자주 보는 실패 지점

- OpenClaw gateway는 떠 있지만 backend model이 응답하지 않는 경우.
- Strategist/reviewer output schema가 contract에서 벗어나는 경우.
- Compiler mapping이 너무 약해 manager intent가 실행에 반영되지 않는 경우.
- Compiler mapping이 너무 강해 current state보다 과거 lesson을 과적용하는 경우.
- Reviewer가 raw metric을 반복하고 actionable correction을 남기지 않는 경우.
- Curator가 raw JSON을 wiki로 복사해 reusable knowledge가 되지 않는 경우.

## 디버깅 체크리스트

먼저 아래 artifact를 봅니다.

- `run_meta.json`
- `kpi.json`
- `daily_summary.json`
- `day_summary_memory.json`
- `day_review_memory.json`
- `shift_policy_history.json`
- `manager_replay.json`
- `reasoning_dashboard.html`
- runtime OpenClaw workspace의 `facts/current_request.json`
- runtime OpenClaw workspace의 `reports/*`

Knowledge 관련 문제는 아래를 추가로 봅니다.

- `llm_wiki_dashboard.html`
- `knowledge_graph_dashboard.html`
- `knowledge/llm_knowledge/experiments/<id>/wiki/00_Index.md`
- `knowledge/llm_knowledge/experiments/<id>/graph/graphify_history.jsonl`
