# OpenClaw 구성

이 디렉터리는 ManSim v0.3이 사용하는 OpenClaw profile과 workspace 템플릿을 저장합니다.

현재 기본 연결 경로는 다음과 같습니다.
- OpenClaw gateway
- `gemma4-cu130` Docker runtime
- `Gemma 4 E4B IT`
- served model alias `mansim-gemma4-e4b`

## 기본 실행 흐름
```powershell
.\install_openclaw_cli.ps1
.\start_vllm_gemma4_docker.ps1
.\openclaw\start_gateway.ps1
.\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main
```

runtime 종료:
```powershell
.\stop_vllm_gemma4_docker.ps1
```

## Legacy 경로
기존 Qwen runtime은 제거하지 않았고 legacy preset으로 분리되어 있습니다.

```powershell
.\start_vllm_wsl.ps1
.\.venv\Scripts\python.exe -m manufacturing_sim.simulation.main decision=llm_planner_qwen_legacy
```

## 구성
- `profiles/mansim_repo`
  - OpenClaw profile 설정
- `workspaces/A1`, `A2`, `A3`
  - worker template
- `workspaces/MANAGER`
  - 공통 manager template
- `workspaces/MANAGER_BOTTLENECK_DETECTOR`
  - detector template
- `workspaces/MANAGER_DIAGNOSIS_EVALUATOR`
  - evaluator template
- `workspaces/MANAGER_DAILY_PLANNER`
  - planner template
- `workspaces/MANAGER_RUN_REFLECTOR`
  - run reflector template

## Workspace 파일 의미
- `SOUL.md`
  - 역할 성향과 판단 스타일
- `IDENTITY.md`
  - 책임 경계와 전문성
- `AGENTS.md`
  - workspace에서 지켜야 할 규칙
- `TOOLS.md`
  - 사용 가능한 도구와 원칙
- `BOOTSTRAP.md`
  - 시작 시 다시 읽을 최소 지침
- `HEARTBEAT.md`
  - 변하지 않는 운영 원칙
- `MEMORY.md`
  - prompt-facing 압축 memory
- `KNOWLEDGE.md`
  - run-series root의 `knowledge.md`가 manager group에 주입된 cross-run prior
- `USER.md`
  - 현재 turn 지시와 scratch context

## 현재 LLM orchestration 기준
- run 내부
  - `detector -> evaluator(optional) -> planner`
- run 종료 후
  - `reflector -> knowledge.md update`
- 다음 run 시작
  - manager workspace에 `KNOWLEDGE.md` 주입

## 런타임에 생성되는 대표 파일
- `facts/current_request.json`
  - 현재 turn 입력 packet
- `facts/current_response_template.json`
  - 해당 agent가 따라야 하는 출력 계약
- `memory/rolling_summary.md`
  - run-local 압축 요약
- `reports/*`
  - day별 reflect / evaluation / plan / reflection 산출물
- `trace/*`
  - manager call trace와 review history

## 참고
- OpenClaw 경로가 기대하는 상위 preset은 `manufacturing_sim/simulation/conf/decision/llm_planner.yaml`입니다.
- 전체 구조와 실행 방법은 [루트 README](../README.md)를 참고하면 됩니다.
