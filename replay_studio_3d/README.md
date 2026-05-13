# Replay Studio 3D

Replay Studio 3D는 기존 `replay_studio/`를 건드리지 않는 독립 실험 앱입니다. 실패하거나 방향을 바꾸고 싶으면 `replay_studio_3d/` 폴더만 삭제하면 됩니다.

## 목표

- 기존 `replay_studio_log.json` v1.0을 그대로 읽습니다.
- 기존 factory layout, tile grid, object footprint, worker motion path를 3D로 표현합니다.
- 2D sprite를 쓰지 않고 Three.js procedural block model로 worker, machine, queue, buffer, charger, item을 그립니다.
- 기존 hub와 2D Replay Studio에는 연결하지 않습니다.

## 실행

```powershell
cd C:\Github\ManSim\replay_studio_3d
npm install
npm run dev
```

기본 URL:

```text
http://127.0.0.1:5174
```

특정 run log 열기:

```text
http://127.0.0.1:5174/?log=C:\Github\ManSim\outputs\2026-05-11\23-49-32\replay_studio_log.json
```

dashboard manifest에서 run 선택:

```text
http://127.0.0.1:5174/?manifest=C:\Github\ManSim\outputs\dashboard_manifest.json&run=23-49-32
```

## 좌표 변환

- `layout.grid.width_tiles`, `layout.grid.height_tiles`를 3D world 크기로 씁니다.
- `layout.viewport`의 2D 좌표는 tile 좌표로 변환됩니다.
- 변환식:
  - `worldX = point.x / tileWidth - grid.width_tiles / 2`
  - `worldZ = point.y / tileHeight - grid.height_tiles / 2`
  - `Y`축은 높이입니다.
- `layout.grid.object_footprints`가 있는 object는 footprint 중심과 tile 크기를 그대로 씁니다.

## 표현 규칙

- Worker: blocky humanoid robot
- Machine: block machine + status panel + process progress
- Queue/buffer: conveyor/platform + item cubes
- Charger: charging rack block
- Item: material/intermediate/product/battery 색상 cube
- Wall: voxel block
- Door: amber floor plate
- Traffic conflict: worker 사이 직선 연결 없이 tile/edge highlight로 표시합니다.

## 검증

```powershell
npm run test
npm run build
npm run test:visual
```

`test:visual`은 Playwright로 desktop/mobile viewport에서 3D canvas가 비어 있지 않은지, replay object들이 렌더링되는지 확인합니다.

