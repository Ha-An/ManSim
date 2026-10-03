# ADP 구조·입력 의미·학습 효율 검토

## 결론

현재 우선순위는 **feature 대량 삭제나 episode 축소가 아니라, 입력 의미 수정과 반복 추론·계산 제거**다.

- 신경망 SGD는 최근 전체 학습 시간의 0.6~1.0%다. Rollout과 TD target 구성이 대부분이다.
- 창고 재고, 가공 잔여시간, 재발한 후보의 대기시간에서 입력 의미 오류를 재현했다.
- 작은 GPU 추론을 상태마다 호출하고, 선택지가 하나뿐인 상태에도 beam 가치 평가를 수행한다.
- Feature의 수학적 중복은 있지만, 삭제하면 학습 성능이 유지된다는 실험 근거는 아직 없다.
- 이번 검토에서는 운영 코드, 학습 설정, checkpoint, 기존 결과를 변경하지 않았다.

## 조사 범위와 한계

1. `schema.py`, `encoding.py`, `model.py`, `policy.py`, `coordinator.py`, `compact.py`, `td.py`, `td_train.py`, `train.py`를 확인했다.
2. `20260919_212305` 연구의 worker 2~6대 학습 5회 CSV/JSON을 재집계했다.
3. 현재 코드로 worker 3·6대, seed 72026, 1일 Random Feasible episode를 각각 실행했다.
4. 이때 수집된 실제 상태에서 기본 128차원·4 head·3 layer·beam 64 네트워크의 추론 비용을 측정했다.
5. 별도 seed 946003의 worker 3, 1일 episode를 cProfile로 확인했다. 생산 0개, 의사결정 1회인 퇴화 실행이므로 정상 실행의 대표 비율로 사용하지 않는다.
6. 신경망 속도 시험은 **새로 초기화한 가중치**다. 학습된 정책의 생산성, pruning 효과, 최종 성능 우위를 검증한 시험이 아니다.

이전 연구에는 PM와 가공 중첩 오류가 있었으므로 그 결과로 현재 정책의 우열을 판단할 수 없다.
과거 wall-clock 기록은 병목을 찾는 참고 자료로만 사용했다. 현재의 정상 episode 두 개 역시
성능을 대표하는 표본이 아니라 구조와 비용을 확인하는 소형 진단이다.

산출물: `outputs/adp_efficiency_review_20260926/`.

## 현재 학습 구조

```text
CPU 10 process: simulation rollout / epsilon-greedy joint assignment
  -> CPU compact pre-state / post-state / reward / constraint descriptors
  -> 최근 100 episode replay
  -> 초기 50 episode 또는 현재 10 + 과거 20 episode 선택
  -> online network로 replay의 각 상태에서 greedy action 재탐색
  -> n <= 30: greedy mismatch 직전 / terminal에서 조기 중단
  -> target network bootstrap + MSE, GPU minibatch 512
  -> SGD마다 target tau=0.03
  -> 고정 seed greedy screening / 최종 checkpoint selection
```

- 기본 학습: 초기 50 + 75회 x 10 = 800 training episodes.
- 기본 profile validation: screening 160 + final 60 = 220 episodes.
- 논문 실험 profile은 screening 60 + final 20 = 80 episodes로 덮어쓴다.
- Policy iteration의 update는 현재 10개뿐 아니라 과거 replay 20개도 사용한다.
- 따라서 800회 simulation과 별개로, 초기 50 + 75 x 30 = 2,300 episode 분량의 target 재구성 방문이 있다.
- WAIT, shaping, pairwise MC loss, conservative gate는 현재 TD 기본 경로에서 비활성이다.

## 실제 시간 분해

과거 논문 실험의 단일 fleet 학습 800 + validation 80 episode 기준이다.
각 비율은 해당 run의 전체 wall-clock에 대한 비율이며 process 시간을 합산한 값이 아니다.

| Worker | 전체 시간 | Training rollout | Validation | TD target 구성 | 신경망 update |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 8.32 h | 67.3% | 6.8% | 25.1% | 0.6% |
| 3 | 16.66 h | 61.7% | 6.1% | 31.5% | 0.6% |
| 4 | 18.93 h | 62.1% | 6.0% | 30.9% | 0.8% |
| 5 | 27.60 h | 53.0% | 4.8% | 41.2% | 0.9% |
| 6 | 29.96 h | 51.9% | 5.1% | 41.8% | 1.0% |

남은 작은 비중은 저장·진단·정리 등이다. Epoch 수를 절반으로 줄여도 이 기록에서는
SGD 시간만으로 전체를 크게 단축할 수 없다. GPU upgrade도 첫 번째 해결책이 아니다.

10개 rollout 슬롯의 가중 활용률은 89.8~95.3%였다. 이는 CPU 사용률이 아니다.
Wave 시작/IPC overhead 합은 88 wave당 약 148~163초였다. Pool 유지 최적화는 가능하지만,
이 기록상 target 재탐색이나 simulator 자체보다 우선순위가 낮다.

## 입력 의미 오류와 한계

### 1. 창고 material feature가 실제 재고가 아니다

`encoding.py:271`은 `len(world.warehouse_material_shelf_slots) / 30`을 사용한다.
이 dict는 material 집합이 아니라 고정된 선반 slot 집합이다.

격리 fixture에서 실제 재고를 30 -> 25로 바꿔도 encoded 값은 1.0 -> 1.0이었다.
문서의 "warehouse material 수"와 일치하지 않는다. 기존 `_material_shelf_count()`를 사용해야 한다.
현재 관측에서 상수였다고 이 feature를 삭제하면 재고 정보 자체를 잃는다. 삭제보다 수정이 우선이다.

### 2. 가공 중 잔여시간이 실시간으로 감소하지 않는다

`encoding.py:96,334`는 `cycle_remaining_process_min`을 그대로 읽는다.
이 값은 cycle 시작/재개 당시 잔여량이며 simulator는 중단·완료 시 갱신한다.
가공 중 snapshot에서 현재 경과시간을 차감하지 않는다.

Fixture: cycle sample 7.489860분, 3분 경과 후에도 encoded remaining은 시작 시와 같았다.
계속 가공했다면 실제 잔여는 4.489860분이다. 이 값은 station 집계와 terminal-closure 예상시간에 함께 영향을 준다.

개선: 현재 processing 구간 시작시각과 그 시점의 잔여량으로 조회 시점의 남은 시간을 계산한다.
고장·재개·PM 경계까지 테스트해야 하며 simulation의 원래 cycle bookkeeping 값을 단순히 매 조회마다 차감하면 안 된다.

### 3. 새 수리 후보가 이전 고장의 대기시간을 물려받는다

`coordinator.py:229`의 `first_seen_by_opportunity.setdefault()`는 episode 동안 삭제되지 않는다.
Repair, setup, self-charge 등 반복 가능한 resource 기반 opportunity에 발생 회차가 들어가지 않으면
예전 first-seen이 다음 발생에 재사용된다.

직접 재현: t=3에 수리 후보 생성, 조건 해소, t=20에 새 고장 생성.
새 후보의 first-seen은 3이어서 생성 직후 대기시간이 17분이 된다.

개선: candidate의 연속 존재 기간 또는 실제 failure/service 발생 ID를 기준으로 수명을 관리한다.
단순히 후보 목록에 안 보이면 지우는 방식은 실행 중 resource 때문에 잠깐 가려진 후보의 aging을
잘못 초기화할 수 있으므로 lifecycle 구분이 필요하다.

### 4. Worker 예상 잔여시간은 매우 거친 근사다

`encoding.py:379-385`는 task primitive 평균시간에서 elapsed를 뺀다.
거리 기반 실제 이동시간과 남은 수행 단계는 반영하지 않는다. 시작시각 0은 `or env.now` 때문에
없는 값처럼 취급하는 문제도 있다.

소형 수집에서 busy worker 중 이 feature가 0인 비율은 3대 68.8%, 6대 62.1%였다.
이를 "worker가 곧 끝난다"는 정보로 해석하기 어렵다. 다만 이 비율만으로 모델이 해당 feature를
잘못 활용했다고 단정할 수는 없다.

이 입력 오류들이 생산성 정체의 주원인이라는 결론은 아직 아니다. 수정 전후 동일 조건 학습과
held-out 평가가 필요하다. Feature 의미를 수정하면 기존 checkpoint의 입력 계약도 구분해야 한다.

## Feature 수와 중복

현재 schema는 `mfg_flow_shop_adp_v9`이며 다음 80개 feature 종류를 사용한다.

| 그룹 | 개수 | 주요 내용 |
| --- | ---: | --- |
| Global | 35 | 시간, 생산/재고, fleet, station buffer·설비 상태 |
| Worker | worker당 16 | 위치, 배터리, 상태, 운반, 할당, 예상 잔여시간 |
| Task | candidate당 21 | 역할·종류·station, priority, age, urgency, 완료까지 예상시간 |
| Pair | worker-task당 8 | 이동, 수행시간, battery margin, downstream·buffer 정보 |

실제 float 입력 개수는 `35 + 16M + 21N + 8MN`이며 80개 고정 scalar가 아니다.
별도로 feasibility, selected-assignment, padding mask를 사용한다.
모델은 474,369 parameters, 네 input embedding은 합계 10,752 parameters(2.27%)다.
Parameter 비중은 latency 비중과 같지 않지만, feature 일부를 줄여도 128차원 attention 구조는 그대로 남는다.

### 확정적 또는 구조적인 중복

- Global elapsed fraction과 remaining fraction의 합은 1이다.
- 유한 buffer의 occupancy + reserved + free 비율은 정상 용량 계약에서 1이다. Station별 input/output 4개 묶음에 각 1개 유도 가능한 값이 있다.
- 고정 fleet별 checkpoint에서 fleet-size feature는 상수다. 통합 fleet 모델에서는 의미가 있으므로 무조건 삭제할 대상은 아니다.
- Pair 8개 중 repair flag, downstream progress, blockage release, destination finite/free 5개는 task 측 정보다. 수집된 3·6대 상태에서도 동일 opportunity의 feasible worker 사이에 같았다.
- Task의 role number, type flags, load/interface selector, 고정 priority는 분류 정보가 중첩된다. 다만 숫자 role만 남기면 categorical inductive bias가 달라져 성능 보존을 보장할 수 없다.

### 삭제하면 안 되는 것 / 아직 삭제 근거가 없는 것

- Worker의 assignment flag/role, task의 assigned count는 **pre-state에서 0이어도 afterstate에서 바뀐다**. 상수 판정만으로 지우면 행동 정보가 사라진다.
- Selected assignment mask, SOC, battery margin, 위치·이동시간, 실행 가능성, finite-buffer reservation 정보는 유지한다.
- Rare failure, critical urgency, suspended 상태가 1일 표본에서 없었다고 제거하지 않는다.
- Terminal-closure 예상시간은 계산을 캐시할 수 있지만, 실제로 중복/무효한 feature인지 판정하려면 별도 ablation이 필요하다.
- PM protection/maintenance 진행도는 현재 압축 상태에 직접 들어가지 않는다. 이를 추가해야 성능이 오른다는 증거는 없지만, 현재 표현을 완전한 물리 상태라고 볼 수도 없다.

**권고: 우선 차원은 유지하고 값의 의미와 계산법을 고친다.** Feature 삭제·embedding 축소는 그 다음 독립 실험으로 분리한다.

## 비용을 줄일 수 있는 구현 지점

### A. 선택 결과가 하나인 상태의 target 탐색 생략

`td.py:207-219`는 모든 replay state에서 `greedy_beam_matching()`을 호출한다.
`policy.py:214-215`는 complete matching이 하나뿐이어도 가치망을 실행한다.
Target plan은 여기서 반환된 predicted value를 사용하지 않고 selected afterstate만 보관한다.

| 소형 진단 | 수집 decision 수 | 후보 task가 0개 | 계측 상태 수 | complete matching이 1개 |
| --- | ---: | ---: | ---: | ---: |
| Worker 3 | 184 | 44 | 184 | 61 (33.2%) |
| Worker 6 | 796 | 570 | 300 | 268 (89.3%) |

Beam width는 64인데 두 계측 집합의 최대 matching 수는 24였다. 이 표본에서는 beam pruning 자체보다
작은 묶음의 반복 network 호출이 문제였다. 다른 seed·정책·장기 실행에서 64보다 큰 경우가 없다는 뜻은 아니다.

추천 구현: constraint를 적용한 결과가 유일함을 확인하면 그 assignment를 바로 plan에 넣는다.
TD reward/시간/episode 순서는 그대로 두고, 이후 필요한 target-network 평가는 원래 minibatch에서 수행한다.
**유휴 transition 자체를 삭제하면 n-step 의미와 시간 범위가 달라지므로 이번 최적화와 구분해야 한다.**

### B. TD greedy 탐색과 GPU 전송을 batch화

현재 `build_target_plan()`은 상태마다 Python loop -> afterstate 객체 생성 -> CPU 배열별 CUDA 전송 ->
작은 network 호출 -> CPU 결과 반환을 반복한다. GPU를 사용한다는 이유로 이 경로가 빠르지는 않다.

동일 실제 상태·동일 초기 가중치로 3회 측정한 beam-search median:

| 상태 집합 | CPU 1 thread | CUDA | CPU/GPU action 일치율 |
| --- | ---: | ---: | ---: |
| Worker 3, 184개 | 0.264초 | 0.666초 | 98.37% |
| Worker 6, 300개 | 0.361초 | 0.957초 | 99.67% |

가치 차이는 최대 약 1.94e-7이었지만 near-tie에서 선택이 바뀌었다.
전체 episode target plan에서도 selected mask가 달라졌고, 3대 사례는 endpoint까지 달라졌다.
따라서 CPU로 바꾸는 것만을 "완전히 동일한 최적화"라고 주장할 수 없다.
현재 CPU rollout과 GPU target-plan 행동 비교에도 같은 종류의 수치 민감성이 존재할 수 있다.
이번 측정이 기존 학습에서 그 문제가 얼마나 자주 발생했는지를 보여주는 것은 아니다.

추천 순서:

1. 유일한 행동의 불필요한 탐색을 제거한다.
2. 같은 단계의 여러 state 후보를 함께 평가한다. Beam/resource/cyclic-order 계약은 유지한다.
3. 같은 tensor 크기끼리 묶고, CPU에서 한 번에 collate한 뒤 field당 한 번 전송한다.
4. 기존 결과와 selected masks, off-policy mismatch, TD endpoints를 비교한다.
5. 수치 차이로 tie가 바뀌면 이를 숨기지 말고 rollout/target의 일관된 tie 정책을 별도 검토한다.

### C. 사용하지 않는 attention weight 계산

`model.py:75-76`는 반환 weight를 버리지만 `need_weights=False`를 지정하지 않는다.
진단 process 안에서만 이 옵션을 바꿔 측정했으며 원래 구현은 변경하지 않았다.

- 3대 표본: CPU beam 0.264 -> 0.241초.
- 6대 표본: CPU beam 0.361 -> 0.334초.
- 두 표본에서는 action이 모두 같았지만 output은 약 1e-7 차이가 있었다.

이는 beam 부분의 약 7~8% 차이이며 전체 학습의 7~8% 절감이라는 뜻이 아니다.
학습 backward, near-tie와 full trajectory도 확인한 뒤 적용해야 한다.
`predict_values()`가 호출마다 전체 module에 `eval()`을 재귀 적용하는 비용도 중복된다.

### D. Encoder의 동일 snapshot 반복 계산

- `_task_estimated_duration()`을 계산하고 이어 `_task_battery_risk_metadata()` 안에서 다시 계산한다.
- 후보 filtering에서도 같은 risk metadata를 이미 계산한다.
- Role별 expected duration을 task마다 rule table에서 반복 검색한다.
- Station queue/release delay와 inspection delay를 여러 task에서 다시 계산한다.
- Pair의 task-only 5개 값을 feasible worker 수만큼 반복 계산한다.

정적인 role/primitive 기대시간은 run 단위, machine/queue 값은 encode 호출 단위,
worker-task 추정치는 동일 decision snapshot 단위로 캐시하는 것이 맞다.
시간·위치·reservation이 바뀐 다음 decision까지 동적 값을 무조건 재사용하면 안 된다.

### E. Simulator의 schema 재읽기와 경로 검색

ManSim의 `HumanoidRuntime._normalize_state_payload()`는 schema를 넘기지 않고
HumanoidSim `validate_state_snapshot()`을 호출한다. 그 기본 경로는 JSON 파일을 매번 읽고
StateSchema를 다시 만든다.

같은 정상 snapshot 200회 검증, 3회 median:

- 매번 schema load: 0.15238초.
- 이미 읽은 schema 재사용, snapshot validation은 그대로 실행: 0.000218초.

이는 고립된 validation 함수의 비용이다. 전체 simulation이 같은 비율로 빨라진다는 뜻이 아니다.
Runtime 초기화 때 schema를 한 번 읽고 검증 객체를 재사용하는 변경은 우선 시험할 가치가 높다.
스냅샷 검증 자체를 끄자는 제안이 아니다.

별도의 생산 0개 episode profile에서는 `find_path()`가 6,088회, cumulative 267.7초로
profiled 총 291.0초의 92.0%였다. 동적 경로는 이동 tile/재시도마다 다시 탐색한다.
이 퇴화 사례에서의 비용은 일반적인 실행 전체 비율로 외삽하지 않는다.
Start/goal, 점유, reservation, 통행 가능 상태가 모두 같은 경우의 실패 탐색 재사용이나
정적 이웃 정보 캐시는 검토할 수 있다. 동적 장애물 변경을 무시하거나 이동·대기 event를
생략하면 시뮬레이션을 바꾸므로 금지해야 한다.

### F. Replay의 복사와 pre/post 중복

`td_train.py:491-506`은 보관 100 episode를 먼저 하나로 합친 뒤 사용할 30 episode를 복사한다.
Episode ID를 먼저 뽑아 선택된 episode만 합치면 동일 sampling 규칙을 유지하며 메모리 복사를 줄일 수 있다.

또한 pre/post가 global/pair/feasibility 등 대부분 같은 tensor를 각각 보유한다.
WAIT 비활성 상태에서는 afterstate가 worker assignment/task assigned-count/selected mask 중심으로 바뀐다.
장기적으로 공통 tensor와 delta를 분리할 수 있지만, compact schema/IPC 수정이 필요하므로 첫 변경으로 권하지 않는다.

## 권장 실행 순서

| 순서 | 변경 | 성능 영향 검증 |
| --- | --- | --- |
| 1 | 창고 수량·실시간 잔여시간·candidate age 의미 수정 | 새로운 feature 계약으로 구분, 재학습 필요 |
| 2 | schema 1회 로드, 동일 decision의 정적/동적 계산 캐시 | action·event·reward·RNG sample 동일성 검사 |
| 3 | target에서 유일한 action 재탐색 생략 | selected mask·n-step endpoints·targets 동일성 검사 |
| 4 | 선택 replay만 merge, 불필요 tensor 복사 축소 | sample ID/순서·값·seed 재현성 검사 |
| 5 | batched greedy evaluation, attention weight 미계산 | 값 허용오차뿐 아니라 실제 action/trajectory 비교 |
| 6 | 필요할 때만 feature pruning 또는 model 폭/깊이 축소 | 별도 동일 budget ablation 및 held-out 생산량 비교 |

800 episode, seed, epsilon, n=30, tau=0.03, replay 크기, beam=64를 동시에 바꾸지 않는 것이 좋다.
알고리즘을 약화해서 빨라진 것과 구현 비용을 줄인 것을 구별해야 한다.

성능 검증에서는 기존 MSE만 보지 말고 동일 held-out seed에서 completed products의 paired difference와
95% CI를 보고, 방전·복귀·buffer/PM/역할 감사도 같이 확인한다. 성능 하락이 유의하지 않다는 것과
성능 보존이 입증됐다는 것은 다르므로 허용 손실 폭을 사전에 정해야 한다.

## 시간 단축 기대를 표현하는 방법

현재 전체 재학습을 수행하지 않았으므로 보장된 새 완료시간은 없다.
과거 worker 3의 16.66시간 구성을 참고한 조건부 계산은 다음과 같다.

- Target 구성만 2배 빨라지면: 약 14.03시간.
- Training rollout 시간이 25% 줄고 target 구성이 2배 빨라지면: 약 11.46시간.
- 계산: `Tnew = Told - 0.25 * Ttraining_rollout - 0.50 * Ttarget_build`.

이는 최적화가 그만큼 성공한다는 가정 아래의 Amdahl식 예시다.
현재 환경의 실측 예측이나 생산성 보장으로 읽으면 안 된다.
효과를 실제로 측정한 후에만 전체 fleet 실험 일정을 다시 추정해야 한다.

## 재현 파일

- `outputs/adp_efficiency_review_20260926/audit_efficiency.py`
- `outputs/adp_efficiency_review_20260926/check_feature_semantics.py`
- `outputs/adp_efficiency_review_20260926/feature_semantics.json`
- `outputs/adp_efficiency_review_20260926/workers_3_seed_72026.json`
- `outputs/adp_efficiency_review_20260926/workers_6_seed_72026.json`
- `outputs/adp_efficiency_review_20260926/workers_3.json`: seed 946003의 퇴화 cProfile 사례
- `outputs/adp_efficiency_review_20260926/schema_cache_benchmark.json`
- `outputs/adp_efficiency_review_20260926/historical_time_breakdown.json`

학습·평가 결과 디렉터리에는 덮어쓰지 않았고 장시간 학습도 실행하지 않았다.

기존 `tests/test_adp_td.py`, `tests/test_simulation_based_adp.py` 77개는 통과했다.
그러나 위 입력 의미 3건은 기존 테스트가 잡지 못한 별도 재현 결과이며, 테스트 통과가
이 문제들의 해소를 의미하지 않는다. 운영 코드는 이 검토에서 수정하지 않았으므로
다음 구현 단계에서 재현 fixture를 회귀 테스트로 옮기고 수정해야 한다.
