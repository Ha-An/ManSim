# 문서 개요

이 디렉터리는 ManSim v0.3의 현재 구조를 설명하는 문서를 모아둡니다. 기준은 공개용 `llm_planner` 경로이며, worker 실행, manager chain, run-level reflection, cross-run knowledge loop까지 포함합니다.

## 문서 구성
- `decision_logic.md`
  - 전체 의사결정 구조와 모드별 차이
- `llm_planner_call_flow.md`
  - `llm_planner` 실행 시 run 내부 체인과 run 간 knowledge loop
- `llm_prompt_design.md`
  - manager prompt 설계 원칙과 memory 사용 방식
- `openclaw_native_loop_review.md`
  - OpenClaw native-local runtime 경로와 운영상 고려사항

## 권장 읽는 순서
1. `decision_logic.md`
2. `llm_planner_call_flow.md`
3. `llm_prompt_design.md`
4. `openclaw_native_loop_review.md`

## 현재 기준 용어
- `worker`
  - 현장에서 작업을 수행하는 `A1`, `A2`, `A3`
- `detector`
  - 병목 진단 담당 manager
- `evaluator`
  - detector draft 품질 검토 담당 manager
  - 기본 preset에서는 `off`
- `planner`
  - reviewed diagnosis를 실행 계획으로 변환하는 manager
- `reflector`
  - run 종료 후 `knowledge.md`를 갱신하는 manager

## v0.3에서 문서가 다루는 범위
- run 내부 manager 체인
  - `detector -> evaluator(optional) -> planner`
- run 종료 후 reflection
  - `reflector -> knowledge.md update`
- 다음 run 시작 전 prior 주입
  - manager workspace에 `KNOWLEDGE.md` 주입
- series artifacts
  - `run_series_summary.json`
  - `series_analysis.json`
  - `series_dashboard.html`

## 참고
- 저장소 전체 개요와 실행 방법은 [루트 README](../README.md)를 기준으로 봅니다.
