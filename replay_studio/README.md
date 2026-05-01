# Replay Studio

Replay Studio는 ManSim replay artifact를 보여주는 React + TypeScript viewer입니다. 현재 두 가지 production view를 지원합니다.

- Factory replay: worker, machine, queue, battery, inspection, movement, incident, shared repair animation.
- Manager replay: `openclaw_adaptive_priority`용 day-centered decision pipeline.

앱은 simulator internals와 분리되어 있습니다. Export된 JSON artifact를 읽고 deterministic하게 상태를 복원합니다.

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

## Hub 연동

Results hub는 `dashboard_manifest.json`을 기준으로 Replay Studio를 아래 query parameter와 함께 엽니다.

```text
?manifest=<path-to-dashboard_manifest.json>&run=<run-id>
?manifest=<path-to-dashboard_manifest.json>&run=<run-id>&view=manager
```

Factory replay는 선택된 run의 `replay_studio_log.json`, `replay_studio_layout.json`을 읽습니다. Manager replay는 `manager_replay.json`을 읽습니다.

## 직접 로딩

Factory replay는 log를 직접 지정해서 열 수도 있습니다.

```text
?log=/demo/manufacturing_demo_log.json
```

개발 중에는 호환 JSON replay log를 앱에 drag-and-drop할 수 있습니다.

## 아키텍처

앱은 네 계층으로 분리되어 있습니다.

1. `core/schema`, `core/parser` - replay log validation과 normalization.
2. `core/replay` - deterministic event-sourced reconstruction과 checkpoint 기반 seek.
3. `core/render-model` - domain state를 renderer-friendly scene model로 변환.
4. `renderer`, `ui` - canvas/SVG 시각화와 operator control.

## Event Log 요구사항

각 event는 아래 필드를 포함해야 합니다.

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

`entity_refs.primary`, `source`, `target`, `related[]`는 존재할 경우 string이어야 합니다. 빈 reference는 `null`로 쓰지 말고 생략해야 합니다.

## Simulator Export Helper

아래 파일을 참고합니다.

- `examples/python_event_builder.py`
- `examples/python_exporter.py`
- `examples/export_mansim_run.py`

기존 ManSim run을 변환하는 예시:

```powershell
..\.venv\Scripts\python.exe examples\export_mansim_run.py `
  --run-dir ..\outputs\YYYY-MM-DD\HH-MM-SS `
  --output-log ..\outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_log.json `
  --output-layout ..\outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_layout.json
```

## Rendering Notes

메인 entity scene은 빈번한 animation update를 안정적으로 처리하기 위해 Canvas를 사용합니다. Flow overlay, selection ring, event highlight처럼 스타일 유지보수가 중요한 부분은 SVG를 사용합니다.

시각 스타일은 밝은 pixel-grid factory floor, pixel-art worker/machine/item, HUD-style label, compact worker monitor panel을 기반으로 합니다.
