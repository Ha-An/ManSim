# LLM Planner Call Flow

이 문서는 legacy commitment-driven LLM mode인 `llm_planner` 경로를 설명합니다.

## Status
- `llm_planner`는 여전히 유지됩니다.
- 하지만 현재 repository에서 새 manager-only priority LLM path는 `openclaw_adaptive_priority`입니다.
- 따라서 이 문서는 현재 주력 경로가 아니라 commitment path reference입니다.

## Run-Level Structure
### Inside one run
`detector -> evaluator(optional) -> planner -> worker execution`

### Across runs
`run_reflector -> knowledge update -> next-run knowledge injection`

## 1. Run Start
- runtime loads `configs/`
- simulator initializes from `configs/scenario/*`
- current knowledge render is copied into manager workspaces as `KNOWLEDGE.md`

Injected prior is strong, but current facts win when they clearly contradict prior lessons.

## 2. Daily / Initial Observation
Typical planner-side content includes:
- machine states
- backlog and flow signals
- battery and worker state
- closure-oriented throughput signals
- current commitments and local incident context

## 3. Detector Turn
detector reads
- `current_request.json`
- `MEMORY.md`
- `memory/rolling_summary.md`
- `KNOWLEDGE.md`

returns
- `summary`
- ranked bottlenecks / issues
- supporting evidence

## 4. Optional Evaluator Turn
If enabled, evaluator reads:
- detector draft
- the same request packet
- evaluator memory
- `KNOWLEDGE.md`

returns
- `accept`
- or `request_revision`

## 5. Planner Turn
planner reads
- current execution state
- reviewed detector hypothesis
- planner memory
- `KNOWLEDGE.md`
- opportunity and commitment contracts

planner returns structured action outputs such as:
- `commitments`
- `mailbox`
- `incident_strategy`
- `reason_trace`
- `detector_alignment`

중요
- v0.4에서 planner의 authoritative 실행 출력은 `commitments`입니다.
- planner 응답이 inert/invalid이면 deterministic fallback commitment synthesizer가 대신 실행 plan을 만듭니다.

## 6. Worker Execution
worker는 다음을 사용합니다.
- local observation
- assigned commitments
- mailbox / handoff requests
- currently feasible opportunities
- local incident context

## 7. Mid-Day Incident Handling
기본 경로는 worker-local response first입니다.

incident 예시
- machine breakdown
- low battery or discharge
- blocked buffer
- material starvation
- inspection congestion
- blocked commitment

### Escalation order
1. worker local response
2. planner incident replan
3. detector re-evaluation only if the bottleneck hypothesis likely changed

planner incident mode는 full-day replanning이 아니라 delta replanning이어야 합니다.

## 8. End of Run
after child run finishes
- KPI and daily summaries are finalized
- run reasoning artifacts are written
- reflector receives a compact review packet

## 9. Reflector Turn
reflector writes
- `run_reflection.json`
- `run_reflection.md`
- updated carry-forward lessons through the ontology / knowledge render path

## 10. Series Artifacts
when `run_count > 1`, parent output root also receives
- `knowledge.md`
- `knowledge_graph.json`
- `run_series_summary.json`
- `series_analysis.json`
- `series_dashboard.html`

## Practical Debug Order
1. `run_meta.json`
2. `kpi.json`
3. `daily_summary.json`
4. `llm_exchange.json` or native-local trace
5. `run_reflection.json`
6. `knowledge.md`
7. `reasoning_dashboard.html`
8. replay / KPI dashboards
