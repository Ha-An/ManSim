# ManSim 문서

이 디렉터리는 ManSim v0.4의 아키텍처 노트와 모드별 참고 문서를 담고 있습니다.

## 추천 읽기 순서

1. `simulator_core_guide.md` - 시뮬레이터 entity, event flow, runtime 개념.
2. `decision_logic.md` - decision mode 개요와 policy 동작.
3. `openclaw_adaptive_priority_call_flow.md` - production OpenClaw adaptive-priority manager loop.
4. `openclaw_native_loop_review.md` - OpenClaw native-loop 검토 노트.
5. `llm_planner_call_flow.md` - legacy LLM planner 흐름.
6. `llm_prompt_design.md` - prompt와 structured output 설계 노트.
7. `replay_dashboards.md` - Replay Studio, manager replay, dashboard artifact 설명.

## 현재 주력 경로

현재 주력 LLM 경로는 `openclaw_adaptive_priority`입니다.

- Strategist: 하루 운영 의도와 support plan 작성.
- Deterministic compiler: 실행 가능한 task weight, worker role, safety floor 생성.
- Deterministic worker execution: 시뮬레이터 기반 task dispatch와 local response.
- Reviewer: 일일 진단과 다음 날 carry-forward memory 생성.

## 주요 Runtime Artifact

- `kpi.json`
- `daily_summary.json`
- `day_summary_memory.json`
- `day_review_memory.json`
- `shift_policy_history.json`
- `events.jsonl`
- `minute_snapshots.json`
- `replay_studio_log.json`
- `replay_studio_layout.json`
- `manager_replay.json`
- `dashboard_manifest.json`
- `results_dashboard.html`

## Dashboard 범위

Dashboard layer는 아래 artifact로 분리되어 있습니다.

- Results Hub: run 단위 navigation과 artifact link.
- Replay Studio factory view: event-sourced worker/factory replay.
- Replay Studio manager view: day 단위 sequential manager decision flow.
- Static fallback dashboards: file 직접 접근을 위한 generated HTML artifact.

Replay Studio는 `replay_studio/`에 구현되어 있으며 simulator internals와 분리되어 있습니다. 입력은 export된 JSON artifact입니다.
