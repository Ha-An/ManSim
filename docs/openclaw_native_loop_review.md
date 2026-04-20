# OpenClaw Native Loop 검토

## 범위
이 문서는 ManSim이 현재 사용하는 로컬 OpenClaw native-local 스택을 요약합니다.

대상 경로
- `llm_planner`
- `openclaw_adaptive_priority`

현재 유지되는 production 경로
- `openclaw_adaptive_priority`
- local OpenClaw gateway + local vLLM backend
- strategist + compiler + reviewer closed loop

## 기본 스택
- OpenClaw gateway
- local vLLM backend
- default model alias: `vllm/mansim-gemma4-e4b`
- workspace templates: `openclaw/workspaces/`

## 런타임 동작
- runtime이 temp runtime 디렉터리 아래 run별 OpenClaw workspace root를 준비합니다.
- strategist와 reviewer는 day-scoped runtime alias를 사용합니다.
- 요청/응답 contract 파일은 turn마다 다시 기록됩니다.
- run-local prompt-facing memory는 실행 중 갱신됩니다.

## 현재 manager set
### `openclaw_adaptive_priority`
- shift strategist
- daily reviewer
- run reflector는 multi-run일 때만 사용

### `llm_planner`
- detector
- evaluator(optional)
- planner
- reflector

## 운영상 장점
- workspace 상태를 run 이후 직접 점검할 수 있습니다.
- native-local 요청은 per-turn artifact를 그대로 남깁니다.
- strategist reasoning은 유지하면서도 execution policy는 deterministic compiler가 고정합니다.
- day boundary 중심의 closed loop를 유지합니다.

## 주요 실패 지점
- gateway는 정상인데 backend가 비정상인 경우
- strategist/reviewer output schema drift
- compiler mapping이 너무 약하거나 너무 강한 경우
- strategist role drift가 run마다 커지는 경우
- daily reviewer diagnosis가 지나치게 일반적인 경우
- 여전히 남아 있는 late-horizon variance

## 문제 발생 시 확인할 것
1. `run_meta.json`
2. `kpi.json`
3. `daily_summary.json`
4. `day_summary_memory.json`
5. `day_review_memory.json`
6. `shift_policy_history.json`
7. temp runtime root 아래 OpenClaw workspace 파일
8. `reasoning_dashboard.html`
