# manufacturing_sim

`manufacturing_sim`은 ManSim의 simulator-core package입니다. 이 package는 factory state와 discrete-event 실행을 담당하고, LLM manager나 dashboard UI 자체는 담당하지 않습니다.

## 책임 범위

- world state transition
- worker, machine, item entity model
- SimPy process loop
- feasible task 후보 생성
- `HumanoidSim` task hierarchy 실행 bridge
- queue, machine, inspection, battery, repair side effect
- tile 기반 pathfinding과 worker occupancy 관리
- tile/edge traffic conflict observation
- warehouse material shelf, completed product zone, scrap disposal flow
- event logging과 KPI source 생성

## 주요 모듈

- `simulation/scenarios/manufacturing/world.py` - factory world state, task enumeration, execution, KPI aggregation.
- `simulation/scenarios/manufacturing/humanoid_runtime.py` - `HumanoidSim` catalog/profile validation, step flattening, primitive execution bridge.
- `simulation/scenarios/manufacturing/grid_map.py` - tile map, pathfinding, occupancy.
- `simulation/scenarios/manufacturing/traffic.py` - path overlap, tile conflict, edge conflict, near miss detection.
- `simulation/scenarios/manufacturing/entities.py` - `Worker`, `Machine`, `Task`, `Item` dataclass와 domain state.
- `simulation/scenarios/manufacturing/processes.py` - SimPy process orchestration.
- `simulation/scenarios/manufacturing/logging.py` - `events.jsonl` event writer.
- `simulation/scenarios/manufacturing/run.py` - manufacturing scenario entrypoint.

## Humanoid Runtime

Worker는 `HumanoidSim`의 `HumanoidStateSnapshot`과 `TaskSpec -> StepCall -> Primitive` 정의를 사용합니다.

- State 정의는 `HumanoidSim`가 소유합니다.
- ManSim은 state axes를 실행 중 관찰하고 event/KPI/Replay artifact로 기록합니다.
- Task 후보는 기존 priority family에서 `task_code`로 변환됩니다.
- Primitive step 중 domain action은 ManSim queue/machine/inspection/battery side effect를 호출합니다.
- Setup, unload, inspection은 queue와 machine/table/output buffer 사이의 carry 이동을 실제 tile path로 수행합니다.
- 비도메인 primitive는 `configs/humanoidsim/default.yaml`의 `primitive_timing.default_min`만큼 최소 시간을 소비합니다.

## 현재 제조 Flow 확장

- Warehouse material은 `warehouse_material_shelf`의 개별 slot에 보관됩니다.
- Worker는 material slot의 service tile까지 이동해야 material을 pickup할 수 있습니다.
- Inspection pass product는 output queue를 거쳐 `CompletedProducts` zone의 `completed_product_buffer`까지 운반되어야 최종 count에 반영됩니다.
- Inspection fail product는 `inspection_scrap_queue`에 쌓인 뒤 `COLLECT_WASTE_OR_SCRAP` task로 `scrap_disposal_bin`까지 batch 운반됩니다.
- Product 운반은 weight multiplier가 적용되며, `HANDOVER_ITEM`을 통해 최대 2명의 worker가 공동 운반할 수 있습니다.

## 경계

아래 작업은 repository root의 상위 layer에서 시작합니다.

- decision mode 선택과 Hydra config composition: `runtime/`, `configs/`
- OpenClaw manager orchestration: `agents/`, `openclaw/`
- LLM Wiki, Graphify, run-series knowledge: `knowledge/`
- dashboard rendering/export: `dashboards/`, `replay_studio/`

Simulator core를 수정할 때는 [docs/simulator_core_guide.md](../docs/simulator_core_guide.md)의 runtime boundary와 event/KPI 설명을 함께 확인합니다.
