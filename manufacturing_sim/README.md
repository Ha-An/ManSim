# manufacturing_sim

`manufacturing_sim`은 ManSim의 simulator-core package입니다. Factory state, SimPy process loop, humanoid task execution, event logging, KPI aggregation을 담당하며 LLM manager UI나 dashboard rendering 자체는 포함하지 않습니다.

## 책임 범위

- world state transition
- worker, machine, item entity model
- feasible task candidate 생성
- `HumanoidSim` task hierarchy execution bridge
- queue, machine, inspection, battery, repair side effect
- tile 기반 pathfinding, reservation, traffic observation
- warehouse material shelf, completed product zone, scrap disposal flow
- event logging과 KPI source 생성

## 주요 모듈

- `simulation/scenarios/manufacturing/world.py`: factory world state, task enumeration, execution, KPI aggregation
- `simulation/scenarios/manufacturing/humanoid_runtime.py`: `HumanoidSim` catalog/profile validation, step flattening, primitive execution bridge
- `simulation/scenarios/manufacturing/grid_map.py`: tile map, pathfinding, occupancy
- `simulation/scenarios/manufacturing/traffic.py`: path overlap, tile/edge conflict, near miss detection
- `simulation/scenarios/manufacturing/entities.py`: `Worker`, `Machine`, `Task`, `Item` dataclass와 domain state
- `simulation/scenarios/manufacturing/processes.py`: SimPy process orchestration
- `simulation/scenarios/manufacturing/logging.py`: `events.jsonl` event writer
- `simulation/scenarios/manufacturing/run.py`: manufacturing scenario entrypoint

## Humanoid Runtime

Worker는 `HumanoidSim`의 `HumanoidStateSnapshot`과 `TaskSpec -> child Task -> Primitive` 정의를 사용합니다.

- State, Task, Primitive, Incident 의미는 `HumanoidSim`이 소유합니다.
- ManSim은 factory scenario에서 발생한 event와 side effect를 기록합니다.
- Task 후보는 기존 priority family도 보존하지만 실행과 Replay/KPI 기준은 `task_code`입니다.
- Primitive step 중 domain action은 ManSim queue/machine/inspection/battery side effect를 호출합니다.
- `LOAD_MACHINE`, unload, inspection은 queue와 machine/table/output buffer 사이의 carry 이동까지 tile path로 수행합니다. `SETUP_MACHINE`은 input이 이미 적재된 machine에서 fixture, recipe, program setup만 수행합니다.
- 비도메인 primitive는 `configs/humanoidsim/default.yaml`의 `primitive_timing.default_min`만큼 최소 시간을 소비합니다.

## Decision Modes

`world.py`는 여러 decision mode가 같은 task execution runtime을 공유하도록 구성되어 있습니다.

- `adaptive_priority`: 즉시 dispatch scripted baseline
- `rolling_horizon_aging_priority`: rolling window pool + task-code aging rank
- `rolling_horizon_dedicated_roles`: rolling window pool + worker별 HumanoidSim task allowlist
- `openclaw_adaptive_priority`: OpenClaw manager loop가 priority를 조정하는 optional mode

Dedicated roles mode에서는 `HANDOVER_ITEM`을 수집하지 않고, `REPAIR_MACHINE`은 A2 단독 task로 제한합니다. A1/A2 low battery는 A3의 battery delivery task로 처리합니다.

## Factory Flow Extensions

- Warehouse material은 `warehouse_material_shelf`의 개별 slot에 보관합니다.
- Worker는 material slot service tile까지 이동해야 pickup할 수 있습니다.
- Inspection pass product는 `CompletedProducts` zone의 `completed_product_buffer`에 dropoff되어야 최종 count에 반영됩니다.
- Inspection fail product는 `inspection_scrap_queue`에 쌓인 뒤 `COLLECT_WASTE_OR_SCRAP` task로 `scrap_disposal_bin`까지 batch 운반됩니다.
- Product 운반은 weight multiplier가 적용되며, 일반 mode에서는 `HANDOVER_ITEM`으로 최대 2명의 공동 운반을 표현할 수 있습니다.

## Boundary

아래 기능은 repository root의 상위 layer가 담당합니다.

- Hydra config composition: `configs/`, `runtime/`
- OpenClaw manager orchestration: `agents/`, `openclaw/`
- LLM Wiki, Graphify, run-series knowledge: `knowledge/`
- dashboard/replay rendering: `dashboards/`, `replay_studio/`, `replay_studio_3d/`

Simulator core를 수정할 때는 [docs/simulator_core_guide.md](../docs/simulator_core_guide.md)와 [docs/humanoid_worker_model.md](../docs/humanoid_worker_model.md)의 runtime boundary를 함께 확인합니다.
