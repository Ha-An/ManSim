# Replay Studio

Replay Studio는 ManSim replay artifact를 보여주는 React + TypeScript viewer입니다. Simulator 내부 로직과 분리되어 있으며, export된 JSON artifact를 읽고 event-sourced 방식으로 상태를 복원합니다.

## 지원 View

- Factory replay: worker, machine, queue, battery, inspection, movement, traffic conflict, incident, shared repair animation.
- Manager replay: `openclaw_adaptive_priority`의 day-centered decision pipeline.

## 로컬 실행

```powershell
npm install
npm run dev
```

정적 bundle build:

```powershell
npm run build
npm run preview
```

ManSim repo에 포함된 local node tooling을 사용할 때:

```powershell
$env:PATH = "C:\Github\ManSim\.tooling\node;$env:PATH"
npm.cmd run build
```

## Hub 연동

Results hub는 `dashboard_manifest.json`을 기준으로 Replay Studio를 아래 query parameter와 함께 엽니다.

```text
?manifest=<path-to-dashboard_manifest.json>&run=<run-id>
?manifest=<path-to-dashboard_manifest.json>&run=<run-id>&view=manager
```

Factory replay는 선택한 run의 `replay_studio_log.json`, `replay_studio_layout.json`을 읽습니다. Manager replay는 `manager_replay.json`을 읽습니다.

## Strict Replay

Factory replay export는 strict mode를 목표로 합니다.

- Worker 위치는 simulator가 기록한 tile 또는 motion payload만 사용합니다.
- Inspection 중 worker를 workbench 위치로 강제 이동시키지 않습니다.
- Output queue 같은 entity를 renderer에서 새로 만들지 않습니다.
- Battery gauge는 log에 기록된 `battery_pct`를 사용합니다.
- Primitive는 `HUMANOID_STEP_START`/`HUMANOID_STEP_END`와 worker state event에 기록된 값을 표시합니다.
- Worker movement는 `entity_moved.payload.path`와 `durative` window로 보간합니다.
- Movement overlay는 실제 tile path를 polyline으로 표시합니다.
- Traffic conflict는 `traffic_conflict_detected` event의 tile/edge payload를 overlay로 표시합니다.
- Item monitor는 scrapped/completed item을 숨기지 않고 log에 존재하는 item state를 모두 보여줍니다.

`replay_studio_log.json.metadata`에는 `replay_mode: strict`, `position_policy: simulation_tile_or_motion_only`, `visual_corrections: false`가 기록됩니다.

## Worker Monitor

오른쪽 Worker panel은 반복 관찰용 compact monitor입니다.

- `Task / Code`: task label과 Humanoid task code.
- `Primitive`: 현재 primitive call code.
- `Motion Path`: 현재 motion payload의 path point 수.
- `Traffic`: 최근 traffic conflict type과 상대 worker.
- `Carry`: 현재 들고 있는 item ID와 type.
- `Updated`: replay entity update time.

Task step ID는 event/debug 정보로 남기고 panel에는 표시하지 않습니다.

## Architecture

1. `core/parser` - replay log validation과 normalization.
2. `core/replay` - deterministic event-sourced reconstruction과 checkpoint 기반 seek.
3. `core/render-model` - reconstructed domain state를 renderer-friendly scene model로 변환.
4. `renderer`, `ui` - Canvas/SVG 시각화와 operator control.

## Event Requirements

각 replay event는 아래 필드를 포함해야 합니다.

- `event_id`
- `sequence_index`
- `timestamp`
- `event_type`
- `entity_refs`
- `payload`

정렬 규칙은 deterministic합니다.

1. `timestamp ASC`
2. `sequence_index ASC`
3. `event_id ASC`

`entity_refs.primary`, `source`, `target`, `related[]`는 존재할 경우 string이어야 합니다. 빈 reference는 `null`로 유지하지 말고 필드 자체를 생략해야 합니다.

## Export Helper

기존 ManSim run을 Replay Studio 입력으로 다시 변환하려면 아래 명령을 사용합니다.

```powershell
..\.venv\Scripts\python.exe examples\export_mansim_run.py `
  --run-dir ..\outputs\YYYY-MM-DD\HH-MM-SS `
  --output-log ..\outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_log.json `
  --output-layout ..\outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_layout.json
```

참고 파일:

- `examples/python_event_builder.py`
- `examples/python_exporter.py`
- `examples/export_mansim_run.py`
