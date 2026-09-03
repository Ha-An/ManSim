# Scenario Timing Profiles

ManSim은 HumanoidSim에서 Task와 Primitive의 구조 및 의미를 가져오지만, 실제 실행시간은 각 ManSim scenario가 독립적으로 결정합니다. 환경 파라미터와 휴머노이드 작업시간을 섞지 않기 위해 다음 네 파일을 사용합니다.

```text
configs/scenario/factory_mfg_basic.yaml
configs/scenario/mfg_flow_shop.yaml
configs/scenario/shipyard_basic.yaml
configs/task_primitive_timing/factory_mfg_basic.yaml
configs/task_primitive_timing/mfg_flow_shop.yaml
configs/task_primitive_timing/shipyard_basic.yaml
```

`configs/scenario/<scenario>.yaml`에는 horizon, worker 수, map, 설비와 재고, 품질, 배터리, traffic, failure, autonomous machine processing처럼 Task와 무관한 환경값을 둡니다. `configs/task_primitive_timing/<scenario>.yaml`에는 해당 scenario가 사용하는 HumanoidSim Task의 모든 primitive occurrence와 이동시간만 둡니다. `factory_mfg_basic`, `mfg_flow_shop`, `shipyard_basic`은 값을 공유하거나 상속하지 않습니다. `mfg_flow_shop` timing profile은 정확히 9개 enabled task만 포함하며 PM과 handover timing을 포함하지 않습니다.

## Primitive Duration

각 primitive occurrence는 HumanoidSim task expansion에서 얻은 정확한 `step_path`와 `call_code`로 식별합니다.

```yaml
tasks:
  SETUP_MACHINE:
    steps:
      SETUP_MACHINE/s04_execute_machine_action:
        call_code: EXECUTE_MACHINE_ACTION
        distribution:
          type: triangular
          min: 2.4
          mode: 3.0
          max: 3.6
```

Scenario 시작 시 사용하는 모든 Task를 primitive leaf까지 전개하고 timing profile을 검증합니다. 누락 또는 추가된 Task/step, 중복 path, `call_code` 불일치, 잘못된 삼각분포 순서는 실행 전 `TimingConfigError`를 발생시킵니다. Primitive duration은 runtime에서 한 번만 소비하며 domain helper가 같은 시간을 다시 더하지 않습니다.

## Movement Duration

`NAVIGATE_TO` 계열 step은 고정 service duration을 갖지 않습니다. 이동을 시작할 때 scenario별 타일시간을 한 번 샘플링합니다.

```yaml
movement:
  model: per_tile_triangular
  sample_scope: move
  per_tile_min:
    distribution:
      type: triangular
      min: 0.08
      mode: 0.10
      max: 0.12
```

실제 이동시간은 `sampled tile time x actual path edge count x load multiplier`입니다. 같은 top-level Task가 같은 목적지 이동을 재개하면 기존 sample을 유지하고, 새로운 목적지 이동에서 다시 샘플링합니다. Traffic wait는 이동시간에 포함하지 않고 별도 event와 KPI로 집계합니다. Shipyard cart는 Shipyard timing profile의 cart multiplier를 추가로 적용합니다.

## Machine Processing

Factory의 autonomous station processing은 일반 scenario 파일에서 별도 삼각분포로 정의합니다.

```yaml
factory:
  processing_time:
    station1:
      distribution: triangular
      min: 7.0
      mode: 8.5
      max: 11.0
```

`mfg_flow_shop`은 하루를 8시간(`480`분)으로 정의하며, Station 1·2에 각각 병렬 설비 2대를 둡니다. Station 1은 `7/8.5/11분`, Station 2는 `10/12.5/16분` 삼각분포를 사용합니다.

설비 cycle을 시작할 때 한 번 샘플링합니다. 가공 중 고장이 발생하면 loaded input, cycle id, sampled duration, remaining duration을 유지하고, 수리 후 같은 cycle의 잔여시간부터 재개합니다.

## Reproducibility And Audit

Timing sampler는 incident와 quality RNG에서 분리된 결정적 stream을 사용합니다. 같은 seed와 sample key는 같은 값을 만들며 primitive, movement, machine event에는 분포 파라미터와 sample을 기록합니다. KPI와 `run_meta.json`에는 timing profile fingerprint와 sample summary를 저장합니다. Factory policy comparison suite는 동일 worker count 비교군에서 모든 정책이 같은 Factory timing fingerprint를 사용했는지 fairness audit으로 확인합니다.
