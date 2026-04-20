# OpenClaw Assets

This directory contains the OpenClaw profile and workspace templates used by ManSim v0.4.

## Purpose
`openclaw/` is not the simulator core. It is the workspace and profile layer used by the root-level runtime and agent stack.

## Main Contents
- `profiles/mansim_repo/`
  - Default Gemma 4 E4B OpenClaw profile for the primary GPU 1 path
- `profiles/mansim_repo_parallel/`
  - Optional Gemma 4 E4B OpenClaw profile for the parallel GPU 0 path
- `workspaces/A1`, `A2`, `A3`
  - worker templates
- `workspaces/MANAGER`
  - shared manager template
- `workspaces/MANAGER_BOTTLENECK_DETECTOR`
  - detector template
- `workspaces/MANAGER_DIAGNOSIS_EVALUATOR`
  - evaluator template
- `workspaces/MANAGER_DAILY_PLANNER`
  - planner template
- `workspaces/MANAGER_RUN_REFLECTOR`
  - reflector template

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

## Optional Parallel Local Stack
```powershell
.\start_vllm_gemma4_parallel_docker.ps1
.\openclaw\start_gateway.ps1 -ProfileName mansim_repo_parallel -BackendModelsUrl http://127.0.0.1:8001/v1/models -ExpectedModelId mansim-gemma4-e4b-parallel -Port 18790
```

Note
- `mansim_repo` + E4B on GPU 1 is the current validated healthy baseline.
- `mansim_repo_parallel` + E4B on GPU 0 is an optional sidecar path for parallel experiments only.
- current pinned operating profile for the primary path is the `experiment 3` configuration.
  - stronger strategist authority
  - stronger strategist role guidance
  - `llm.max_tokens = 600`
  - `backend.max_output_tokens = 2048`
- representative primary-path run: `outputs/2026-04-18/20-18-45`
  - `23 products / 7m 34s / terminated=false`
- 3-run summary for the pinned profile: `outputs/2026-04-18/experiment_3_summary.json`
  - average `21.67 products / 8m 47s`
  - `10분 이내 3/3`
- `outputs/2026-04-18/15-47-38` (`26 products / 4m 48s`) remains a best-observed outlier, not the current operating baseline.

## Workspace Runtime Files
Each runtime workspace typically receives:
- `USER.md`
- `MEMORY.md`
- `KNOWLEDGE.md` for manager roles
- `facts/current_request.json`
- `facts/current_response_template.json`
- `facts/current_native_turn.json`
- `reports/*`
- `trace/*`

## Notes
- Canonical config now lives under `configs/`, not under `manufacturing_sim/simulation/conf/`.
- Cross-run knowledge is injected into manager workspaces as `KNOWLEDGE.md`.
- Root-level documentation lives in `README.md` and `docs/`.
