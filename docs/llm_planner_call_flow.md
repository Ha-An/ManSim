# LLM Planner 호출 흐름

## 1. 하루 시작 관측 생성
- 월드가 현재 상태를 요약한 observation을 생성합니다.
- 이 observation은 생산 흐름, 제약 신호, 기계와 작업자 상태를 포함합니다.

## 2. 병목 탐지기 호출
- `MANAGER_BOTTLENECK_DETECTOR`가 observation을 읽습니다.
- 현재 완제품 마감을 가장 제한하는 병목을 상위 몇 개로 진단합니다.
- 결과는 strategy state와 OpenClaw diagnosis 메모리로 저장됩니다.

## 3. 일일 계획기 호출
- `MANAGER_DAILY_PLANNER`가 다음을 함께 읽습니다.
  - 실행 상태
  - 마감 신호
  - 제약 신호
  - candidate orders
  - detector hypothesis
- planner는 detector를 그대로 따를 수도 있고, 오늘 증거가 더 강하면 일부 또는 전체를 override할 수도 있습니다.

## 4. 계획 적용
- planner는 `weight_updates`, `queue_add`, `reason_trace`, `detector_alignment`를 반환합니다.
- 작업자 큐가 있으면 작업자는 이를 먼저 실행합니다.
- 큐가 없거나 불가능하면 로컬 `priority_score` 기반 선택으로 fallback합니다.

## 5. 하루 종료 리뷰
- day summary가 생성됩니다.
- manager review 이벤트가 기록됩니다.
- 다음 날 observation과 memory에 반영됩니다.
