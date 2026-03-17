# ManSim

ManSim은 소형 제조 라인의 운영 의사결정을 비교하기 위한 Hydra + SimPy 기반 시뮬레이터다. 규칙 기반 모드와 LLM 기반 모드를 모두 지원하며, 생산량, WIP, 고장 대응, 검사 흐름, 배터리 운영을 함께 관찰할 수 있다.

## 주요 모드

- `adaptive_priority`
  - 규칙 기반 모드.
  - 실행 중 상태를 보고 task-family priority를 적응적으로 조정한다.

- `fixed_priority`
  - 규칙 기반 baseline 모드.
  - task-family priority weight는 고정하고, quota와 norm은 제한적으로 반영한다.

- `llm_planner`
  - LLM이 매일 `reflect`, `propose_jobs`, townhall, norm 갱신을 수행한다.
  - 팀 공통 baseline priority와 agent별 priority overlay를 함께 사용한다.

- `llm_task_selector`
  - `llm_planner`의 planning 구조를 유지하면서, 실행 시 next-task 선택도 LLM이 담당한다.

## 현재 LLM 모드의 우선순위 구조

LLM 모드에서는 팀 공통 baseline과 agent별 overlay를 함께 사용한다.

- `shared_task_priority_weights`
  - 팀 전체가 공유하는 baseline priority다.
  - `propose_jobs`가 매일 갱신한다.

- `agent_priority_multipliers`
  - 각 agent가 task family별로 가지는 개인 overlay다.
  - 시작값은 모든 agent가 `1.0`이다.
  - day 종료 경험 집계와 townhall synthesis를 통해 서서히 달라질 수 있다.

- runtime effective priority
  - 개별 agent가 실제로 사용하는 우선순위는 아래 개념으로 계산된다.
  - `task.priority * shared_task_priority_weight * agent_priority_multiplier`

## 실행 예시

기본 실행:

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main
```

모드 지정:

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=adaptive_priority
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=fixed_priority
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm_planner
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm_task_selector
```

자주 쓰는 override:

```powershell
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm_planner decision.urgent_discuss.enabled=false
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm_planner decision.norms.enabled=false
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm_planner decision.llm.communication.language=KOR
C:\Github\ManSim\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main experiment.horizon.num_days=3
```

## 결과물

기본 output 폴더에는 아래 파일들이 생성된다.

- `kpi.json`
- `daily_summary.json`
- `events.jsonl`
- `minute_snapshots.json`
- `run_meta.json`
- `artifact_status.json`
- `llm_exchange.json` (LLM 모드)
- `kpi_dashboard.html`
- `gantt.html`
- `task_priority_dashboard.html`
- `llm_trace.html` (LLM 모드)

## 문서

- `docs/README.md`
- `docs/decision_logic.md`
- `docs/llm_prompt_design.md`
- `docs/llm_planner_call_flow.md`
- `docs/llm_task_selector_call_flow.md`

## 문서 인코딩 메모

이 저장소의 한글 문서는 `UTF-8`로 저장해야 한다. PowerShell here-string이나 기본 `Set-Content` 경로를 사용할 때는 UTF-8을 명시하지 않으면 저장 시점에 한글이 `?`로 치환될 수 있다.
