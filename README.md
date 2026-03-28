# ManSim v0.2

ManSim은 제조 공장의 자재 흐름, 설비 상태, 작업자 행동, 매니저 의사결정을 함께 시뮬레이션하는 연구용 프레임워크입니다.  
현재 `v0.2`는 OpenClaw 기반 `manager_bottleneck_detector` / `manager_daily_planner` 구조를 포함한 최신 실험 버전입니다.

## 버전 구분
- `v0.1`: 기존 GitHub에 올라가 있는 기준 버전
- `v0.2`: 현재 작업 중인 최신 구조 정리 및 manager-agent 기반 버전

## 핵심 특징
- 제조 시나리오 전용 discrete-event 시뮬레이션
- 작업자 에이전트 `A1`, `A2`, `A3` 기반 현장 실행
- `MANAGER_BOTTLENECK_DETECTOR`와 `MANAGER_DAILY_PLANNER`로 분리된 매니저 구조
- `fixed_priority`, `adaptive_priority`, `llm_planner` 실행 모드 지원
- OpenClaw + Ollama 기반 native-local LLM 연동
- KPI, replay, gantt, LLM trace, workspace dashboard 산출물 생성

## 현재 의사결정 구조
### Worker Agents
- `A1`, `A2`, `A3`
- 개인 큐, mailbox, 로컬 우선순위를 바탕으로 실제 작업을 수행합니다.

### Manager Agents
- `MANAGER_BOTTLENECK_DETECTOR`
  - 현재 상태에서 완제품 마감을 가장 제한하는 병목을 진단합니다.
- `MANAGER_DAILY_PLANNER`
  - 병목 진단과 실행 상태를 함께 보고 하루 계획을 수립합니다.
  - 필요 시 detector 판단을 검토하고 계획을 보정할 수 있습니다.

## 실행 모드
- `fixed_priority`
  - 고정 규칙 기반 우선순위
- `adaptive_priority`
  - 상태 반응형 규칙 기반 우선순위
- `llm_planner`
  - manager agent 기반 LLM 의사결정 모드

## 디렉터리 구조
- `manufacturing_sim/simulation`
  - 시뮬레이션 엔진과 시나리오 코드
- `manufacturing_sim/simulation/scenarios/manufacturing`
  - 제조 시나리오 전용 월드, 의사결정, 시각화 코드
- `manufacturing_sim/simulation/conf`
  - 실험, 결정 모드, 규칙 설정 파일
- `openclaw`
  - OpenClaw 프로파일과 워크스페이스 템플릿
- `docs`
  - 구조 설명, 호출 흐름, 프롬프트 설계 문서

## 주요 산출물
- `events.jsonl`: 전체 이벤트 로그
- `daily_summary.json`: 일별 요약
- `kpi.json`: 최종 KPI
- `llm_exchange.json`: LLM 요청/응답 기록
- HTML 대시보드
  - KPI dashboard
  - gantt
  - replay
  - LLM trace
  - OpenClaw workspace dashboard
  - orchestration intelligence dashboard

## 빠른 시작
### 규칙 기반 실행
- `fixed_priority` 또는 `adaptive_priority` 설정으로 바로 실행할 수 있습니다.

### LLM 기반 실행
- OpenClaw gateway와 Ollama backend가 준비되어 있어야 합니다.
- 기본 설정 파일은 아래를 사용합니다.
  - `manufacturing_sim/simulation/conf/decision/llm_planner.yaml`
  - `manufacturing_sim/simulation/conf/experiment/mfg_basic.yaml`

## 관련 문서
- [문서 개요](docs/README.md)
- [의사결정 로직](docs/decision_logic.md)
- [LLM Planner 호출 흐름](docs/llm_planner_call_flow.md)
- [LLM 프롬프트 설계 원칙](docs/llm_prompt_design.md)
- [OpenClaw Native Loop 검토 메모](docs/openclaw_native_loop_review.md)

## 주의
- `outputs` 아래 파일은 실행 산출물입니다.
- `openclaw/workspaces` 아래 md 파일은 런타임 워크스페이스 템플릿입니다.
- `llm_planner` 모드를 사용할 때는 OpenClaw와 Ollama가 모두 살아 있어야 합니다.
