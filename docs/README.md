# ManSim 문서 안내

`docs` 폴더는 ManSim의 의사결정 구조, LLM 프롬프트 구조, 모드별 호출 흐름을 정리한 문서 모음이다.

## 문서 목록

### `decision_logic.md`
- 4개 decision mode 개요
- shared baseline, agent overlay, norm, urgent discuss 관계
- runtime에서 어떤 단계로 의사결정이 진행되는지 정리

### `llm_prompt_design.md`
- LLM 시스템 프롬프트와 입력 JSON 구조
- observation, diagnosis, memory, townhall prompt 구성
- 프롬프트를 줄이면서도 추론 재료를 유지하는 원칙

### `llm_planner_call_flow.md`
- `llm_planner`의 일일 호출 순서
- `reflect`, `propose_jobs`, townhall, `urgent_discuss` 역할
- shared baseline과 agent별 priority overlay가 언제 갱신되는지 설명

### `llm_task_selector_call_flow.md`
- `llm_task_selector`의 runtime next-task 선택 흐름
- candidate generation, selector payload, fallback 경로 설명
- agent experience와 개인 priority profile이 selector에 어떻게 들어가는지 정리

## 추천 읽기 순서

1. `decision_logic.md`
2. `llm_prompt_design.md`
3. `llm_planner_call_flow.md`
4. `llm_task_selector_call_flow.md`

## 참고 설정 파일

- `manufacturing_sim/simulation/conf/config.yaml`
- `manufacturing_sim/simulation/conf/decision/adaptive_priority.yaml`
- `manufacturing_sim/simulation/conf/decision/fixed_priority.yaml`
- `manufacturing_sim/simulation/conf/decision/llm_planner.yaml`
- `manufacturing_sim/simulation/conf/decision/llm_task_selector.yaml`
- `manufacturing_sim/simulation/conf/experiment/mfg_basic.yaml`
- `manufacturing_sim/simulation/conf/heuristic_rules/default.yaml`
