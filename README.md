# ManSim v0.4.2

ManSim은 제조 라인의 discrete-event simulation, 작업자/설비/큐 동역학, LLM 기반 운영 의사결정, 리플레이 대시보드를 함께 실험하는 연구용 프레임워크입니다.

![Replay Studio factory replay 화면](docs/assets/replay-studio-worker-replay.png)

## v0.4.2 주요 업데이트

- 시뮬레이션 환경을 연속 좌표 기반 배치에서 타일 기반 grid map으로 전환했습니다. 공장 내 설비, 큐, 작업자, 이동 경로를 tile 단위로 표현해 replay와 simulation state의 위치 해석을 더 일관되게 맞췄습니다.
- 타일 기반 환경에 맞춰 Replay Studio를 수정했습니다. 작업자 이동, 설비/큐 배치, inspection/replay 화면이 tile layout을 기준으로 렌더링되도록 정리했습니다.
- LLM Wiki와 `MANAGER_CURATOR`를 추가했습니다. 매일 run 결과에서 raw data를 분리 저장하고, manager가 재사용할 수 있는 운영 지식 중심의 Obsidian-compatible wiki를 생성합니다.
- Graphify 기반 knowledge graph pipeline을 추가했습니다. run 단위로 wiki를 graph로 변환/업데이트하고, Strategist와 Reviewer가 과거 운영 지식을 참고할 수 있도록 compact digest를 제공합니다.

현재 주력 경로는 `openclaw_adaptive_priority`입니다. 이 모드의 최종 목적은 horizon 동안 `completed products`, 즉 inspection을 통과해 warehouse까지 도착한 accepted product 수를 최대화하는 것입니다. Closure ratio, backlog, battery/reliability 안정성은 이 목표를 설명하거나 보조하는 secondary signal입니다.

## 핵심 구조

- `manufacturing_sim/` - 시뮬레이터 코어, factory world, scenario runtime.
- `agents/` - scripted mode, OpenClaw manager loop, policy compiler.
- `configs/` - scenario, decision, worker, runtime 설정.
- `runtime/` - Hydra entrypoint, artifact export, dashboard 생성.
- `dashboards/` - results hub, replay, knowledge, series dashboard.
- `replay_studio/` - React 기반 factory/manager replay viewer.
- `knowledge/` - run-series knowledge, LLM Wiki, Graphify graph artifact.
- `openclaw/` - OpenClaw profile, gateway script, workspace template.
- `docs/` - simulator, decision, dashboard, LLM Wiki 문서.

## OpenClaw Adaptive Priority

하루 운영 루프는 아래 순서로 진행됩니다.

1. `MANAGER_SHIFT_STRATEGIST`가 현재 공장 상태, 전날 reviewer memory, LLM Wiki/graph digest를 읽고 하루 운영 의도를 작성합니다.
2. deterministic compiler가 의도를 실행 가능한 worker role, task weight, safety floor, support rule로 변환합니다.
3. worker들은 simulator 안에서 deterministic dispatch를 수행합니다.
4. `MANAGER_DAILY_REVIEWER`가 하루 결과를 진단하고 다음 날 correction signal을 남깁니다.
5. `MANAGER_CURATOR`가 운영 지식을 Obsidian-compatible LLM Wiki와 Graphify knowledge graph source로 정리합니다.

Strategist와 Reviewer는 직접 task instance를 배정하지 않습니다. 실행 가능한 저수준 정책은 compiler가 생성합니다.

## 빠른 시작

의존성 설치:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

1일 smoke run:

```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority scenario.horizon.num_days=1 runtime.ui.auto_open_results=false
```

5일 run:

```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority scenario.horizon.num_days=5
```

5일 run을 3회 반복해 run-to-run knowledge 효과를 비교:

```powershell
.\.venv\Scripts\python.exe main.py decision=openclaw_adaptive_priority scenario.horizon.num_days=5 decision.llm.orchestration.run_count=3
```

OpenClaw local stack:

```powershell
.\install_openclaw_cli.ps1
.\start_vllm_gemma4_docker.ps1
.\openclaw\start_gateway.ps1
```

Replay Studio 개발 서버:

```powershell
cd replay_studio
npm install
npm run dev
```

## 주요 산출물

시뮬레이션 결과는 `outputs/<date>/<run-id>/` 아래에 생성됩니다.

- `results_dashboard.html` - run hub.
- `series_dashboard.html` - multi-run 비교. `completed products`를 1순위로 해석합니다.
- `replay_studio_log.json`, `replay_studio_layout.json` - factory replay 입력.
- `manager_replay.json` - strategist/compiler/reviewer/curator 흐름 replay 입력.
- `dashboard_manifest.json` - hub navigation manifest.
- `kpi.json`, `daily_summary.json`, `shift_policy_history.json` - 운영 분석 artifact.
- `day_review_memory.json`, `day_summary_memory.json` - day-boundary manager memory.
- `llm_wiki_dashboard.html` - LLM Wiki 진입점. Obsidian 앱과 browser preview를 함께 제공합니다.
- `knowledge_graph_dashboard.html` - Graphify 기반 knowledge graph viewer.

LLM Wiki와 graph의 원본 저장소는 기본적으로 `knowledge/llm_knowledge/experiments/<experiment-id>/`입니다. 반복 실험은 experiment별로 분리되어 누적됩니다.

## 문서

- [docs/README.md](docs/README.md) - 문서 인덱스.
- [docs/simulator_core_guide.md](docs/simulator_core_guide.md) - simulator core guide.
- [docs/decision_logic.md](docs/decision_logic.md) - decision mode와 성공 기준.
- [docs/openclaw_adaptive_priority_call_flow.md](docs/openclaw_adaptive_priority_call_flow.md) - OpenClaw manager loop.
- [docs/llm_wiki_curator.md](docs/llm_wiki_curator.md) - LLM Wiki, Curator, Graphify pipeline.
- [docs/replay_dashboards.md](docs/replay_dashboards.md) - dashboard와 replay artifact.


## 검증

Python compile check:

```powershell
.\.venv\Scripts\python.exe -m py_compile agents\openclaw_adaptive_priority.py knowledge\llm_wiki.py runtime\entrypoint.py dashboards\series_dashboard.py dashboards\llm_graph.py
```

Replay Studio build:

```powershell
cd replay_studio
npm run build
```

## 운영 메모

- `outputs/`, `.venv/`, `node_modules/`, `dist/`, `.tooling/`은 git에서 제외합니다.
- 현재 production LLM path는 `openclaw_adaptive_priority`입니다.
- LLM Wiki와 knowledge graph는 현재 사실을 대체하지 않습니다. manager는 항상 최신 simulation state를 우선하고, 과거 지식은 판단 보조 자료로 사용해야 합니다.
