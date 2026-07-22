# Replay Studio 3D

Replay Studio 3D는 ManSim v0.5 run이 생성한 `replay_studio_log.json`, `replay_studio_layout.json`, `dashboard_manifest.json`을 읽는 React + Three.js viewer입니다. Simulation core와 분리되어 있으며 event stream을 재생해 factory와 shipyard 상태를 복원합니다.

## Supported Views

- Factory: worker, machine, queue, warehouse shelf, inspection table, item lineage와 machine input/output 상태
- Shipyard: surface work tile, cart route, 2-tile cart, parking tile, cart inventory와 작업 진행 상태
- Worker: 이동/작업 animation, cargo, battery/state/task monitor, 선택 worker의 first-person PiP
- Rolling horizon: task pool, dispatch/start/complete/skip/requeue 상태와 stable task id
- Incident and traffic: active incident, recovery context, movement path와 tile/edge conflict

오른쪽 monitor는 scenario에 따라 항목이 달라집니다.

- Factory: `WORKER`, `MACHINE`, `ITEM`
- Shipyard: `WORKER`, `TILE`, `CART`, `ITEM`

## Run Locally

Node.js 20.19 이상과 npm이 필요하며 Node.js 24를 권장합니다. 저장소의 `package-lock.json`과 동일한 dependency를 설치하려면 `npm ci`를 사용합니다.

```powershell
cd C:\Github\ManSim\replay_studio_3d
npm ci
npm run dev
```

기본 URL은 `http://127.0.0.1:5174`입니다.

특정 replay log를 직접 열 수 있습니다.

```text
http://127.0.0.1:5174/?log=C:\Github\ManSim\outputs\YYYY-MM-DD\HH-MM-SS\replay_studio_log.json
```

Hub와 같은 manifest를 사용하려면 다음 query를 사용합니다.

```text
http://127.0.0.1:5174/?manifest=C:\Github\ManSim\outputs\YYYY-MM-DD\HH-MM-SS\dashboard_manifest.json&run=run_01
```

## Replay Contract

Viewer는 simulation artifact를 관찰하는 계층입니다. Worker 위치, battery, item state, task 상태를 renderer에서 임의로 보정하지 않습니다.

- event는 `timestamp`, `sequence_index`, `event_id` 순서로 재생합니다.
- worker 이동은 motion payload의 tile path와 durative window를 보간합니다.
- item은 material, intermediate, product, battery만 monitor 대상으로 사용하고 lineage를 함께 표시합니다.
- worker cargo와 cart inventory는 replay event에 기록된 state만 표시합니다.
- ship work tile과 cart route/parking은 layout의 grid tile 목록을 사용합니다.
- rolling task table은 event에 기록된 stable task id와 status transition을 사용합니다.

## Coordinates And Rendering

`layout.grid.width_tiles`, `layout.grid.height_tiles`, `layout.viewport`를 사용해 replay 좌표를 Three.js world 좌표로 변환합니다.

```text
worldX = point.x / tileWidth - grid.width_tiles / 2
worldZ = point.y / tileHeight - grid.height_tiles / 2
```

`layout.grid.object_footprints`가 있는 entity는 footprint 중심과 크기를 사용합니다. Worker와 cart는 현재 path heading을 기준으로 방향을 정하고, first-person camera는 선택 worker의 eye-height pose와 마지막 유효 heading을 사용합니다.

## Verification

```powershell
npm run test
npm run build
npm run test:visual
```

`test:visual`은 Playwright로 desktop/mobile viewport, nonblank canvas, 3D scene framing, first-person PiP와 주요 artifact rendering을 확인합니다.
