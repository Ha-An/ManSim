# ManSim v0.3

ManSim은 제조 공장의 자재 흐름, 설비 상태, 작업자 행동, 매니저 의사결정을 함께 시뮬레이션하는 연구용 프레임워크입니다.

v0.3의 중심은 `llm_planner` 경로를 GitHub 공개 기준으로 정리한 것입니다. 현재 구조는 worker 에이전트, manager 그룹, OpenClaw native-local LLM runtime, run-level reflection과 cross-run knowledge loop를 한 체계로 묶습니다.

## v0.3 핵심 변화
- `MANAGER_BOTTLENECK_DETECTOR`, `MANAGER_DIAGNOSIS_EVALUATOR`, `MANAGER_DAILY_PLANNER`, `MANAGER_RUN_REFLECTOR` 기반 manager 구조 정리
- run 종료 후 reflection을 남기고 다음 run에 `knowledge.md`를 주입하는 cross-run knowledge loop 추가
- `run_series_summary.json`, `series_analysis.json`, `series_dashboard.html` 기반 run-series 분석 추가
- manager workspace memory와 prompt-facing memory 구조 정리
- OpenClaw + Gemma 4 Docker runtime 기준 실행 경로 문서화

## 지원 모드
- `fixed_priority`
  - 고정 규칙 기반 우선순위
- `adaptive_priority`
  - 상태 반응형 규칙 기반 우선순위
- `llm_planner`
  - worker + manager + OpenClaw LLM runtime을 함께 쓰는 orchestration 모드

## 현재 아키텍처

### Worker Agents
- `A1`, `A2`, `A3`
- 개인 큐, mailbox, 로컬 priority score를 바탕으로 실제 작업을 수행합니다.

### Manager Group
- `MANAGER_BOTTLENECK_DETECTOR`
  - 현재 상태와 run-local memory를 함께 보고 남은 horizon 동안 생산성을 가장 제한하는 병목을 진단합니다.
- `MANAGER_DIAGNOSIS_EVALUATOR`
  - detector draft가 planner에 전달될 만큼 grounded 되어 있는지 검토합니다.
  - 기본 preset에서는 `off`입니다.
- `MANAGER_DAILY_PLANNER`
  - reviewed diagnosis와 실행 상태를 결합해 weight, queue, action-level 계획을 만듭니다.
- `MANAGER_RUN_REFLECTOR`
  - run 종료 후 detector/planner가 더 잘했어야 할 판단과 다음 run에 carry-forward할 지식을 압축합니다.

### Run 내부 체인
- `detector -> evaluator(optional) -> planner`

### Run 간 체인
- run 종료 후 `reflector -> knowledge.md update`
- 다음 run 시작 시 manager workspace에 `KNOWLEDGE.md` 주입

## 주요 개념

### `knowledge.md`
- run-series 루트에 저장되는 cross-run prior입니다.
- 구조:
  - `Persistent Lessons`
  - `Latest Lessons`
  - `Detector Guidance`
  - `Planner Guidance`
  - `Open Watchouts`

### Prompt-facing memory
- 각 workspace는 `MEMORY.md`, `memory/rolling_summary.md`를 유지합니다.
- raw day artifacts는 보존하되, prompt가 직접 읽는 memory는 압축된 형태로 유지합니다.

### Run-series 분석
- `run_count > 1`이면 parent output root에 시리즈 요약 산출물이 생성됩니다.
- 대표 산출물:
  - `knowledge.md`
  - `run_series_summary.json`
  - `series_analysis.json`
  - `series_dashboard.html`

## 디렉터리 구조
- `manufacturing_sim/simulation`
  - 시뮬레이션 엔진, 시나리오, 결정 로직, 시각화 코드
- `manufacturing_sim/simulation/conf`
  - experiment / decision preset
- `manufacturing_sim/simulation/scenarios/manufacturing`
  - 제조 시나리오 전용 world / decision / viz
- `openclaw`
  - OpenClaw profile과 workspace 템플릿
- `docs`
  - 구조 설명, 호출 흐름, 프롬프트 설계, 운영 메모

## 빠른 시작

### 규칙 기반 실행
```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=fixed_priority
```

또는

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=adaptive_priority
```

### LLM 기반 실행
현재 기본 preset은 `OpenClaw gateway + gemma4-cu130 Docker runtime + Gemma 4 E4B IT` 조합입니다.

기본 순서:
```powershell
.\install_openclaw_cli.ps1
.\start_vllm_gemma4_docker.ps1
.\openclaw\start_gateway.ps1
.\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main
```

종료:
```powershell
.\stop_vllm_gemma4_docker.ps1
```

기본 preset 파일:
- `manufacturing_sim/simulation/conf/decision/llm_planner.yaml`
- `manufacturing_sim/simulation/conf/experiment/mfg_basic.yaml`

### Legacy Qwen 경로
기존 Qwen runtime은 legacy preset으로 유지됩니다.

```powershell
.\start_vllm_wsl.ps1
.\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm_planner_qwen_legacy
```

## 설정 포인트
- `manufacturing_sim/simulation/conf/decision/llm_planner.yaml`
  - `decision.llm.orchestration.run_count`
  - `decision.llm.orchestration.evaluator.enabled`
  - `decision.llm.orchestration.evaluator.max_revision_requests`
  - `decision.llm.orchestration.detector.max_top_bottlenecks`
  - `decision.llm.memory.history_window_days`
- `manufacturing_sim/simulation/conf/experiment/mfg_basic.yaml`
  - `seed`
  - `horizon.num_days`
  - `horizon.minutes_per_day`
  - machine failure / PM / inventory / movement parameters

## 주요 산출물
- `events.jsonl`
  - 전체 이벤트 로그
- `daily_summary.json`
  - 일별 요약
- `kpi.json`
  - 최종 KPI
- `llm_exchange.json`
  - LLM 요청/응답 기록
- `run_reflection.json`, `run_reflection.md`
  - child run 단위 reflection
- `knowledge.md`
  - run-series 기준 누적 지식
- `run_series_summary.json`
  - serial multi-run 요약
- `series_analysis.json`
  - deterministic knowledge impact 분석
- `series_dashboard.html`
  - run-series 대시보드

### 주요 HTML 대시보드
- KPI dashboard
- gantt
- replay
- LLM trace
- OpenClaw workspace dashboard
- orchestration intelligence dashboard
- series dashboard

## Manager 계약 문서
- Detector input glossary:
  - [openclaw/workspaces/MANAGER_BOTTLENECK_DETECTOR/INPUT_KEY_GLOSSARY.md](openclaw/workspaces/MANAGER_BOTTLENECK_DETECTOR/INPUT_KEY_GLOSSARY.md)
- Evaluator input glossary:
  - [openclaw/workspaces/MANAGER_DIAGNOSIS_EVALUATOR/INPUT_KEY_GLOSSARY.md](openclaw/workspaces/MANAGER_DIAGNOSIS_EVALUATOR/INPUT_KEY_GLOSSARY.md)
- Planner input glossary:
  - [openclaw/workspaces/MANAGER_DAILY_PLANNER/INPUT_KEY_GLOSSARY.md](openclaw/workspaces/MANAGER_DAILY_PLANNER/INPUT_KEY_GLOSSARY.md)
- Reflector input glossary:
  - [openclaw/workspaces/MANAGER_RUN_REFLECTOR/INPUT_KEY_GLOSSARY.md](openclaw/workspaces/MANAGER_RUN_REFLECTOR/INPUT_KEY_GLOSSARY.md)
- Planner output glossary:
  - [openclaw/workspaces/MANAGER_DAILY_PLANNER/OUTPUT_KEY_GLOSSARY.md](openclaw/workspaces/MANAGER_DAILY_PLANNER/OUTPUT_KEY_GLOSSARY.md)
- Evaluator output glossary:
  - [openclaw/workspaces/MANAGER_DIAGNOSIS_EVALUATOR/OUTPUT_KEY_GLOSSARY.md](openclaw/workspaces/MANAGER_DIAGNOSIS_EVALUATOR/OUTPUT_KEY_GLOSSARY.md)
- Reflector output glossary:
  - [openclaw/workspaces/MANAGER_RUN_REFLECTOR/OUTPUT_KEY_GLOSSARY.md](openclaw/workspaces/MANAGER_RUN_REFLECTOR/OUTPUT_KEY_GLOSSARY.md)

## 관련 문서
- [문서 개요](docs/README.md)
- [의사결정 로직](docs/decision_logic.md)
- [LLM Planner 호출 흐름](docs/llm_planner_call_flow.md)
- [LLM 프롬프트 설계 원칙](docs/llm_prompt_design.md)
- [OpenClaw Native Loop 검토 메모](docs/openclaw_native_loop_review.md)

## 주의
- `outputs` 아래 파일은 실행 산출물입니다.
- `openclaw/workspaces` 아래 Markdown 파일은 런타임 템플릿입니다.
- `llm_planner`를 쓸 때는 OpenClaw gateway와 preset이 기대하는 vLLM backend가 모두 살아 있어야 합니다.
- multi-run series는 코드가 아니라 `knowledge.md`를 통해 run 간 prior를 전달합니다.
