# LLM 프롬프트 설계 원칙

이 문서는 v0.3 기준 manager prompt를 설계할 때 유지하는 원칙을 정리합니다. 목표는 instruction debt를 늘리지 않으면서도 detector, evaluator, planner, reflector가 서로 다른 책임을 안정적으로 수행하게 만드는 것입니다.

## 공통 원칙
- 프롬프트를 계속 덧붙여 길게 만들지 않습니다.
- 약한 문장을 지우고 더 짧고 강한 문장으로 교체합니다.
- 자연어 장문보다 구조화된 입력 순서와 의미를 먼저 정리합니다.
- raw history 전체를 직접 주입하지 않고, prompt-facing memory를 압축 유지합니다.

## Detector 설계 원칙
- detector는 현재 상태와 run-local memory를 함께 봅니다.
- 문제를 단순 anomaly 나열이 아니라, 남은 horizon 동안 생산성을 제한하는 bottleneck ranking 문제로 정의합니다.
- 입력은 생산 흐름, 제약 신호, 보조 상세, memory prior 순으로 정리합니다.
- 출력은 `summary + top_bottlenecks` 중심으로 짧고 구조화된 형태를 유지합니다.

## Evaluator 설계 원칙
- evaluator는 detector를 대체하지 않습니다.
- detector draft가 planning-ready한지 검토하는 reviewer로 둡니다.
- detector와 같은 사실을 보더라도, ranking, evidence quality, severity calibration, explanation quality를 점검하도록 만듭니다.
- revision schema는 구조화하되, 불필요한 enum 확장은 피합니다.

## Planner 설계 원칙
- planner는 병목을 새로 진단하는 역할이 아니라 reviewed diagnosis를 실행 계획으로 바꾸는 역할입니다.
- 일반적인 weight 변경보다 concrete worker queue를 더 가치 있는 행동으로 취급합니다.
- queue가 필요할 때는 실제 contract를 따라 구조화된 `queue_add`를 내도록 유도합니다.
- `maintain`은 강한 근거가 있을 때만 허용합니다.

## Reflector 설계 원칙
- Reflector는 run이 끝난 뒤 한 번만 호출합니다.
- raw artifact 전체를 넣지 않고 compact packet으로 요약합니다.
- 출력은 detector/planner가 다음 run에서 실제로 반영할 수 있는 lesson과 guidance로 제한합니다.
- `knowledge.md`는 append-only 로그가 아니라 압축된 cross-run prior로 유지합니다.

## Prompt-facing memory 원칙
- `MEMORY.md`, `memory/rolling_summary.md`는 raw archive가 아니라 압축 요약입니다.
- 최근 추세, 반복 이슈, carry-over watchout만 유지합니다.
- raw day file, reports, trace는 보존하되 prompt에 직접 붙이지 않습니다.
- 지식이 길어질수록 더 많은 로그를 넣는 대신 더 좋은 압축을 우선합니다.

## 피해야 할 것
- 규칙을 계속 추가해 instruction debt를 쌓는 것
- detector와 planner에 같은 결정을 반복 요구하는 것
- detector가 worker-specific queue까지 직접 짜게 만드는 것
- summary와 lesson을 문장 중간에서 잘라 의미를 훼손하는 것
- cross-run knowledge를 너무 추상적으로 만들어 실제 action space와 연결되지 않게 두는 것
