# Docs

이 디렉터리는 현재 ManSim v0.4 구조를 설명합니다.

## 읽기 순서
1. `decision_logic.md`
2. `simulator_core_guide.md`
3. `openclaw_adaptive_priority_call_flow.md`
4. `openclaw_native_loop_review.md`
5. `llm_planner_call_flow.md`
6. `llm_prompt_design.md`

## 범위
현재 문서는 다음 구현을 기준으로 합니다.
- simulator core: `manufacturing_sim/`
- decision/orchestration: repository root의 `agents/`, `runtime/`, `dashboards/`
- canonical decision presets: `configs/decision/`
- primary production LLM path: `openclaw_adaptive_priority`

## `openclaw_adaptive_priority`
현재 문서가 설명하는 구조는 다음 네 계층으로 읽으면 됩니다.
- strategist day-start owner
- deterministic policy compiler
- deterministic worker execution
- daily reviewer 1회/day

핵심은 strategist intent와 reviewer diagnosis를 deterministic compiler가 실행 가능한 정책으로 연결하는 점입니다.
