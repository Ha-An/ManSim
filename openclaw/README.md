# OpenClaw Assets

`openclaw/`에는 ManSim이 local OpenClaw 실행에 사용하는 profile, gateway script, workspace template이 들어 있습니다. 이 디렉터리는 simulator core가 아니라 LLM manager runtime layer입니다.

## 주요 구성

- `profiles/mansim_repo/` - primary local profile.
- `profiles/mansim_repo_parallel/` - optional parallel experiment profile.
- `workspaces/A1`, `workspaces/A2`, `workspaces/A3` - worker workspace template.
- `workspaces/MANAGER_SHIFT_STRATEGIST` - day-start strategist template.
- `workspaces/MANAGER_DAILY_REVIEWER` - day-end reviewer template.
- `workspaces/MANAGER_CURATOR` - LLM Wiki curator template.
- `workspaces/MANAGER_*` - legacy planner/evaluator/reflector templates.

## Canonical Run Path

```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority
```

## Default Local Stack

```powershell
.\install_openclaw_cli.ps1
.\start_vllm_gemma4_docker.ps1
.\openclaw\start_gateway.ps1
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority
```

기본 연결:

- OpenClaw gateway: `http://localhost:18789/v1`
- vLLM backend: `http://127.0.0.1:8000/v1`
- model alias: `vllm/mansim-gemma4-e4b`
- backend model: `mansim-gemma4-e4b`

## Optional Parallel Stack

```powershell
.\start_vllm_gemma4_parallel_docker.ps1
.\openclaw\start_gateway.ps1 -ProfileName mansim_repo_parallel -BackendModelsUrl http://127.0.0.1:8001/v1/models -ExpectedModelId mansim-gemma4-e4b-parallel -Port 18790
```

Parallel profile은 실험용 sidecar입니다. Primary validation path는 `mansim_repo`입니다.

## Runtime Workspace Files

각 manager workspace에는 보통 아래 파일이 생성되거나 갱신됩니다.

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

`LLM_WIKI.md`와 `KNOWLEDGE_GRAPH.md`는 Curator-backed knowledge가 활성화된 경우에만 의미 있는 digest를 담습니다.

## Notes

- Canonical config는 `configs/`에 있습니다.
- Current production path는 `openclaw_adaptive_priority`입니다.
- Manager는 high-level intent와 diagnosis를 담당하고, deterministic compiler가 실행 policy를 만듭니다.
- LLM Wiki/graph는 current state를 대체하지 않습니다. Manager는 최신 simulator facts를 우선해야 합니다.
