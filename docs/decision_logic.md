# Decision Logic

이 문서는 ManSim의 decision mode와 task dispatch 기준을 정리합니다.

## 목표

ManSim의 최종 목표는 설정된 horizon 동안 `completed product`를 최대화하는 것입니다. completed product는 inspection을 통과한 뒤 최종 CompletedProducts zone에 dropoff된 제품입니다.

보조 지표는 원인 분석과 정책 비교에 사용합니다.

- inspection backlog
- machine broken / repair / PM time
- humanoid availability state
- traffic / incident count
- scrap rate
- queue wait time
- rolling horizon pool / dispatch / skipped task

## Decision Modes

현재 root config의 기본 mode는 `rolling_horizon_dedicated_roles`입니다. 비교 실험에서는 `decision=adaptive_priority`, `decision=rolling_horizon_aging_priority`처럼 Hydra override로 변경합니다.

### `adaptive_priority`

로컬 scripted baseline입니다. 현재 공장 상태를 보고 task family priority를 조정해 즉시 dispatch합니다.

### `fixed_priority`

고정 priority baseline입니다. priority 자체를 거의 조정하지 않고 deterministic rule로 dispatch합니다.

### `rolling_horizon_aging_priority`

Rolling window 동안 task opportunity를 pool에 모은 뒤 window boundary에서 dispatch하는 deterministic mode입니다.

- 설정 파일: `configs/decision/rolling_horizon_aging_priority.yaml`
- 기본 window: `rolling_horizon.window_min: 5.0`
- priority 기준: HumanoidSim `task_code`
- priority 설정: `rolling_horizon.task_code_priority_order`
- dispatch policy: `aging_priority`
- battery task도 다른 task와 동일하게 pool에 들어갑니다.

기본 동작:

1. task 후보가 발생하면 즉시 worker에게 할당하지 않고 rolling pool에 저장합니다.
2. 같은 item, material slot, machine resource를 사용하는 중복 opportunity는 pool에 동시에 들어가지 못합니다.
3. window가 끝나면 미해결 pool task를 정렬해 feasible task를 가능한 한 모두 worker dispatch queue에 배정합니다.
4. dispatch 직전 resource/item feasibility를 다시 확인합니다.
5. stale task는 `ROLLING_HORIZON_TASK_SKIPPED`로 기록하고 제거합니다.

한 worker는 여러 task를 queue로 받을 수 있습니다. Worker는 queue의 첫 task부터 FIFO로 수행합니다. 다음 window가 열리면 아직 `AGENT_TASK_START`가 발생하지 않은 queued task는 pool로 되돌아가 새로 수집된 task와 함께 다시 ranking됩니다. 이미 실행 중인 task는 중단하거나 다른 worker에게 재배정하지 않습니다.

Rolling task는 pool에 처음 들어올 때 stable task id를 받습니다.

- 예: `MAT-000001`, `TR-000002`, `SET-000003`, `RM-000004`
- task code별 prefix는 simulator runtime에서 고정합니다.
- 이 id는 requeue, re-dispatch, `AGENT_TASK_START/END`, Replay panel까지 유지됩니다.

### Aging Priority

`rolling_horizon_aging_priority`는 숫자 weight를 쓰지 않고 task code 순서와 기다린 window 수만 사용합니다. 예상 processing time, bottleneck bonus, deadline bonus는 사용하지 않습니다.

```text
effective_rank = base_rank - waited_window_count * rank_boost_per_window
```

낮은 숫자가 더 높은 priority입니다. 예를 들어 `PREVENTIVE_MAINTENANCE`의 base rank가 낮더라도 pool에서 여러 window를 기다리면 effective rank가 올라가므로 영구 starvation을 피할 수 있습니다.

정렬 기준:

1. `effective_rank` 낮은 순
2. `first_seen_min` 오래된 순
3. `task_code` 순
4. `opportunity_id` 순

worker 선택은 feasible worker 중 `(현재 dispatch queue 길이, 예상 수행 시간, 이동 시간, worker id)` 순으로 고릅니다. task ordering 자체에는 예상 처리시간을 사용하지 않습니다.

### `rolling_horizon_dedicated_roles`

`rolling_horizon_aging_priority`와 같은 rolling window/pool/aging 구조를 쓰지만, worker별 HumanoidSim task code allowlist를 강제합니다.

- 설정 파일: `configs/decision/rolling_horizon_dedicated_roles.yaml`
- 기본 window: `rolling_horizon.window_min: 5.0`
- priority 기준: worker별 `rolling_horizon.worker_task_priority` 순서
- dispatch policy: `dedicated_role_aging_priority`
- A1: `REPLENISH_MATERIAL`
- A2: `REPAIR_MACHINE`, `SETUP_MACHINE`, `UNLOAD_MACHINE`
- A3: `MANAGE_ROBOT_POWER`, `TRANSFER`, `INSPECT_PRODUCT`, `COLLECT_WASTE_OR_SCRAP`, `PREVENTIVE_MAINTENANCE`

`HANDOVER_ITEM`은 product 공동 운반 합류 task이므로 이 모드에서 pool에 수집하지 않습니다. `REPAIR_MACHINE`도 A2 단독 task로 처리되어 repair helper join이 발생하지 않습니다.

A1과 A2는 battery station으로 직접 이동해 self swap을 수행하지 않습니다. A1/A2가 설정된 low threshold 이하로 내려가면 A3가 `transfer_kind=battery_delivery`인 battery delivery task를 pool에서 받아 수행합니다. threshold와 provider/receiver 목록은 `decision.battery` 설정에서 조정합니다.

### `fixed_task_assignment`

worker별 허용 task family를 강제하는 scripted mode입니다.

### `openclaw_adaptive_priority`

OpenClaw manager mode입니다. Strategist, Reviewer, Curator가 high-level intent와 operational knowledge를 만들고, deterministic compiler가 이를 executable policy로 변환합니다. ManSim worker는 deterministic simulator runtime으로 실행됩니다.

## Removed Paths

`urgent_discuss` 기반 즉시 정책 변경 경로와 shared `norms` 기반 runtime 조정 경로는 현재 ManSim runtime path에서 제거했습니다. Incident와 recovery는 HumanoidSim incident taxonomy와 recovery protocol을 기준으로 처리하고, ManSim은 시뮬레이션 상황을 발생/관찰하는 역할에 집중합니다.

## Rolling Horizon Events

- `ROLLING_HORIZON_WINDOW_START`
- `ROLLING_HORIZON_CANDIDATE_COLLECTED`
- `ROLLING_HORIZON_DISPATCH`
- `ROLLING_HORIZON_TASK_REQUEUED`
- `ROLLING_HORIZON_TASK_SKIPPED`

3D Replay Studio는 이 event들을 읽어 Task Pool 패널에 window, stable task id, rank, target, assigned worker, status를 표시합니다. `POOL`은 아직 dispatch되지 않은 후보, `DISPATCHED`는 worker queue에 들어간 task, `REQUEUED`는 window boundary에서 아직 시작하지 않아 pool로 되돌아간 task, `SKIPPED`는 dispatch 직전 stale/resource conflict로 제거된 task입니다.

## Preventive Maintenance

`PREVENTIVE_MAINTENANCE`는 idle machine을 대상으로 하는 HumanoidSim composite task입니다. Rolling horizon에서는 낮은 base rank를 갖지만, aging priority 덕분에 pool에서 오래 기다릴수록 effective rank가 개선됩니다.

5일 검증 run 기준으로 `PREVENTIVE_MAINTENANCE`는 pool에 수집되고, dispatch되며, 실제 task timeline과 KPI `humanoid_task_minutes`에도 반영되는 것을 확인했습니다. 보이지 않는 경우는 대개 해당 window에서 PM 조건이 없거나 더 높은 rank의 feasible task가 먼저 처리된 상황입니다.
