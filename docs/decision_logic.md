# 의사결정 로직

## 목적
ManSim의 현재 목적은 남은 horizon 동안 accepted finished-product completion을 최대화하는 것입니다. v0.3에서는 이 목적을 worker 실행, manager planning, run-level reflection까지 일관되게 연결하는 데 초점을 둡니다.

## 모드별 개요

### `fixed_priority`
- 고정 우선순위 규칙만 사용합니다.
- 가장 빠르고 재현성이 높습니다.
- 상황 변화에 대한 적응력은 낮습니다.

### `adaptive_priority`
- 상태 요약을 바탕으로 규칙 기반 priority를 조정합니다.
- LLM 없이도 일부 병목 전환에 반응할 수 있습니다.
- 구조적 병목 해석과 장기적 조정은 제한적입니다.

### `llm_planner`
- worker 실행 위에 manager chain과 run 간 knowledge loop를 얹는 모드입니다.
- run 내부 체인:
  - `MANAGER_BOTTLENECK_DETECTOR`
  - `MANAGER_DIAGNOSIS_EVALUATOR` (`optional`, current preset default `off`)
  - `MANAGER_DAILY_PLANNER`
- run 간 지식 루프:
  - `MANAGER_RUN_REFLECTOR`가 run 종료 후 `knowledge.md`를 갱신
  - 다음 run 시작 시 manager group이 `KNOWLEDGE.md`를 강한 prior로 읽음

## 현재 LLM 매니저 구조

### 병목 탐지기
- 입력
  - 현재 관측
  - 마감 신호
  - 제약 신호
  - 보조 상세
  - `KNOWLEDGE.md`
  - detector workspace memory
- 출력
  - `summary`
  - `top_bottlenecks`
- 역할
  - 현재 상태와 run-local memory를 함께 보고, 남은 horizon 동안 생산성을 가장 제한하는 병목을 랭킹합니다.

### 진단 evaluator
- 입력
  - detector가 본 관측 packet
  - detector draft
  - review round 정보
  - `KNOWLEDGE.md`
  - evaluator workspace memory
- 출력
  - `verdict`
  - `summary`
  - `revision_requests`
- 역할
  - detector draft가 planner에 전달될 만큼 planning-ready한지 검토합니다.
  - 기본 preset에서는 `off`입니다.

### 일일 계획기
- 입력
  - 실행 상태
  - 마감 신호
  - 제약 신호
  - reviewed diagnosis
  - `KNOWLEDGE.md`
  - planner workspace memory
  - queue dispatch contract
- 출력
  - weight diff
  - worker-specific queue
  - detector alignment
  - reason trace
- 역할
  - reviewed diagnosis와 현재 실행 상태를 결합해 실제 day plan으로 변환합니다.

### Run Reflector
- 입력
  - prior knowledge
  - KPI / day summary compact summary
  - detector / planner / evaluator behavior summary
  - recurring issue trend
  - repair-vs-PM summary
  - manager execution gap
- 출력
  - `run_reflection.json`
  - `run_reflection.md`
  - next-run용 `knowledge.md`
- 역할
  - detector와 planner가 다음 run에서 더 잘하기 위해 필요한 cross-run knowledge를 압축 갱신합니다.

## 현재 실행 원리
- worker는 실제 작업을 수행합니다.
- manager는 병목 진단, 검토, 계획 수립을 담당합니다.
- reflector는 한 run이 끝난 뒤 다음 run을 위한 prior를 정리합니다.
- run을 여러 번 직렬 실행할 때, run 간 carry-over는 코드 상태가 아니라 `knowledge.md`를 통해 전달됩니다.

## v0.3 기준 평가 포인트
- 병목 진단이 실제 생산 제약과 얼마나 잘 맞는가
- evaluator가 detector 오판을 실제로 걸러내는가
- planner가 concrete queue와 weight를 통해 실행을 바꾸는가
- reflector가 재사용 가능한 지식을 남기는가
- `manager_queue` 사용 비율이 의미 있게 존재하는가
- run이 반복될수록 `knowledge.md`가 같은 실패 패턴 감소에 기여하는가
