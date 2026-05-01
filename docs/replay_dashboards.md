# Replay와 Dashboard Artifact

ManSim은 정적 HTML dashboard와 Replay Studio용 구조화 JSON payload를 함께 export합니다.

## Results Hub

`results_dashboard.html`은 run별 메인 진입점입니다. 내부적으로 `dashboard_manifest.json`을 사용하며, 이 manifest에는 run metadata와 artifact path가 들어 있습니다.

Hub는 아래 view로 연결됩니다.

- KPI와 results summary
- task-priority dashboard
- OpenClaw workspace dashboard
- factory Replay Studio view
- manager Replay Studio view

## Factory Replay Studio

Factory replay는 아래 파일을 사용합니다.

- `replay_studio_log.json`
- `replay_studio_layout.json`

로그는 event-sourced 방식이며 deterministic replay를 목표로 합니다. Replay Studio는 아래 순서로 상태를 복원합니다.

1. initial state
2. `timestamp`, `sequence_index`, `event_id` 기준으로 stable sort된 event stream
3. 선택적 checkpoint

Renderer는 worker, machine, queue, battery station, inspection, material flow, movement, incident, shared repair를 시각화합니다.

## Manager Replay

Manager replay는 아래 파일을 사용합니다.

- `manager_replay.json`

이 view는 `openclaw_adaptive_priority`를 대상으로 하며, 하루를 하나의 sequential pipeline으로 보여줍니다.

1. Input Bundle
2. Strategist Decision
3. Compiled Policy
4. Factory Response
5. Reviewer Assessment
6. Next-Day Carry Forward

Compiler는 agent가 아니라 deterministic system stage로 표시합니다. Strategist와 Reviewer는 manager decision phase로 유지합니다.

## Shared Repair 시각화

협동 수리 event는 아래 형태로 export됩니다.

- `MACHINE_REPAIR_START`
- `MACHINE_REPAIR_HELPER_JOIN`
- `MACHINE_REPAIR_HELPER_LEAVE`
- `MACHINE_REPAIRED`

Replay Studio는 repair team size, repair progress, machine 주변에 배치된 참여 worker를 표시합니다.

## Replay Studio Asset 재생성

기존 run directory에서 Replay Studio 입력을 다시 만들려면 아래 명령을 사용합니다.

```powershell
.\.venv\Scripts\python.exe replay_studio\examples\export_mansim_run.py `
  --run-dir outputs\YYYY-MM-DD\HH-MM-SS `
  --output-log outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_log.json `
  --output-layout outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_layout.json
```

## 개발 검증

```powershell
cd replay_studio
npm run build
```

Replay log validator는 잘못된 entity reference를 거부합니다. Exporter는 빈 ref를 `null`로 쓰지 말고 필드 자체를 생략해야 합니다.
