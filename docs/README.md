# 문서 개요

이 디렉터리는 제조 시나리오의 현재 의사결정 구조와 OpenClaw 연동 방식을 설명합니다.

## 문서 목록
- `decision_logic.md`: 전체 의사결정 구조와 모드 비교
- `llm_planner_call_flow.md`: LLM 기반 매니저 호출 흐름
- `llm_prompt_design.md`: 병목 탐지기와 일일 계획기 프롬프트 설계 원칙
- `openclaw_native_loop_review.md`: OpenClaw native-local 경로 검토 및 운영 메모

## 현재 기준 용어
- `MANAGER_BOTTLENECK_DETECTOR`: 하루 시작 시 병목을 진단하는 매니저 에이전트
- `MANAGER_DAILY_PLANNER`: 병목 진단을 실행 계획으로 바꾸는 매니저 에이전트
- `worker`: 현장에서 실제 작업을 수행하는 `A1`, `A2`, `A3`
