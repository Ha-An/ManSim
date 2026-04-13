# LLM Planner 호출 흐름

이 문서는 v0.3 기준 `llm_planner` 경로를 run 내부 체인과 run 간 knowledge loop로 나눠 설명합니다.

## 1. Run 시작
- `llm_planner` orchestration 경로는 `decision.llm.orchestration.run_count`만큼 full run을 직렬 실행할 수 있습니다.
- 각 child run 시작 전에 parent output root의 `knowledge.md`가 manager workspace에 `KNOWLEDGE.md`로 주입됩니다.
- detector, planner, evaluator(optional), reflector는 이 파일을 강한 prior로 참고하되, 현재 request facts와 충돌하면 current facts를 우선합니다.

## 2. 하루 시작 관측 생성
- world가 현재 공장 상태를 observation packet으로 요약합니다.
- 이 packet에는 생산 흐름, 제약 신호, 기계 상태, 작업자 상태가 포함됩니다.

## 3. 병목 탐지기 호출
- `MANAGER_BOTTLENECK_DETECTOR`가 `current_request.json`을 읽습니다.
- detector는 `KNOWLEDGE.md`, `MEMORY.md`, `memory/rolling_summary.md`, current request를 함께 참고합니다.
- detector는 남은 horizon 기준으로 생산성을 가장 제한하는 병목을 설정된 개수만큼 순위화합니다.
- detector 출력은 `summary`와 `top_bottlenecks`입니다.

## 4. 진단 품질 검토
- `MANAGER_DIAGNOSIS_EVALUATOR`는 기본 preset에서 `off`입니다.
- 켜져 있을 때만 detector draft를 읽고 `accept` 또는 `request_revision`을 반환합니다.
- `request_revision`이면 detector가 수정본을 다시 제출합니다.
- 이 루프는 `decision.llm.orchestration.evaluator.max_revision_requests`까지 반복됩니다.

## 5. 일일 계획기 호출
- `MANAGER_DAILY_PLANNER`는 다음을 함께 읽습니다.
  - 실행 상태
  - 마감 신호
  - 제약 신호
  - reviewed detector hypothesis
  - `KNOWLEDGE.md`
  - planner workspace memory
- planner request에는 구조화된 계약이 포함됩니다.
  - `queue_add_contract`
  - `reason_trace_contract`
  - `decision_contract`
  - `guardrails`
  - `dispatch_expectation`
- planner는 reviewed diagnosis를 실행 가능한 day plan으로 변환합니다.

## 6. 계획 적용
- planner는 `weight_updates`, `queue_add`, `reason_trace`, `detector_alignment`를 반환합니다.
- worker-specific queue가 있으면 작업자는 이를 먼저 실행합니다.
- 큐가 없거나 실행 불가능하면 로컬 `priority_score` 선택으로 넘어갑니다.

## 7. 하루 종료
- `daily_summary.json`에 일별 요약이 기록됩니다.
- manager review 이벤트와 workspace memory가 갱신됩니다.
- 이 정보는 다음 날 observation과 run-local memory에 반영됩니다.

## 8. Run 종료 Reflector 호출
- `MANAGER_RUN_REFLECTOR`가 완료된 run을 compact packet으로 리뷰합니다.
- 입력은 raw artifact 전체가 아니라 다음을 압축한 형태입니다.
  - `run_context`
  - `prior_knowledge`
  - `performance_summary`
  - `manager_behavior_summary`
  - `notable_failures`
- Reflector는 detector와 planner가 더 잘했어야 할 판단, 그리고 다음 run에 carry-forward할 지식을 정리합니다.

## 9. Series artifact 갱신
- child run output에는 `run_reflection.json`, `run_reflection.md`가 저장됩니다.
- parent output root에는 다음이 갱신됩니다.
  - `knowledge.md`
  - `knowledge_history/run_XX_reflection.md`
  - `run_series_summary.json`
  - `series_analysis.json`
  - `series_dashboard.html`

## 10. 다음 run 시작
- 다음 child run은 갱신된 `knowledge.md`를 다시 `KNOWLEDGE.md`로 주입받고 시작합니다.
- 따라서 run 간 학습 효과는 코드 상태가 아니라 cross-run knowledge를 통해 전달됩니다.
