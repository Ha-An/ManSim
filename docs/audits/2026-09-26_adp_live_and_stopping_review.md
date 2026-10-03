# ADP 실시간 모니터 검토와 학습 종료 시점 분석

## 범위와 데이터

- ADP TD driver, episode replay/target/update 흐름, 병렬 rollout 실패 처리,
  백그라운드 supervisor, 진행 JSON, CSV 저장, TD 그래프 및 집계를 확인했다.
- 이전 효율화 변경과 기존 작업 트리를 유지했다. 학습 파라미터 기본값은 이번 검토에서 바꾸지 않았다.
- 최근 paper 실험의 worker 2~6 학습 5개와 worker 3 n=30 본학습/파인튜닝 2개를 읽었다.
  논문용 5개 학습은 각 800 training + 80 validation episode이며 screening은 I0/15/30/45/60/75이다.
- `scripts/analyze_adp_training_plateau.py`는 원본을 수정하지 않고 평균, paired difference,
  검증 최고 checkpoint, 후반 비용과 기존 artifact audit 결과를 JSON으로 저장한다.
- 결과: `outputs/adp_live_audit_20260926/historical_plateau.json`.

**중요한 한계:** paper 결과에는 이미 `result_validity.json`의 PM/processing 중첩 오류 경고가 있다.
이번 집계 검산 통과가 과거 시뮬레이션 오류를 무효화하지 않는다. 과거 곡선은 학습 경향과 비용
분석용이지 수정된 환경의 성능 증명이 아니다. 현재 v10 입력 의미도 달라져 새 학습에서 확인해야 한다.

## 확인하고 수정한 오류

1. **Rollout 실패 시 잘못된 집계/화면 전환:** screening/final phase를 training으로 세고,
   기존 summary 계약을 덮어쓰며 완료 iteration 곡선도 버리는 실패 경로가 있었다.
   TD/MC 계약과 기존 곡선을 유지하고 phase별 개수를 정확히 기록하도록 수정했다.
   실패 wave에서 이미 반환된 episode 결과도 보존하되 부분 value update는 하지 않는다.
2. **진행 중 시간 오분류:** wave 완료 시 HTML은 다시 생성하지만 해당 rollout 전체가 끝나야
   소요시간이 반영되어 현재 phase 시간이 0/이전 값이고 기타 시간이 커지는 현상이 있었다.
   실행 중 phase 시간을 snapshot에 반영하고 종료/실패 시 한 번만 확정한다.
3. **업데이트 완료 수 지연:** SGD 완료 후에도 validation을 모두 마쳐야 업데이트 수가 증가했다.
   이제 SGD가 끝나는 즉시 진행 숫자를 갱신한다.
4. **CSV 부분 읽기:** CSV를 직접 덮어써 실시간 다운로드/재생성 중 불완전한 파일을 읽을 수 있었다.
   UTF-8 BOM과 개행을 유지하면서 atomic 교체로 변경했다.
5. **종료 시 lock 잔존:** 마지막 모니터 저장이 실패하면 output lock 해제가 건너뛰어졌다.
   종료 처리의 `finally`에서 자신이 소유한 lock을 해제한다.

실행 중인 코드 변경이나 강제 종료 뒤의 정확한 TD replay 재개는 지원하지 않는다.
기존 배경 실행 문서의 제한은 그대로다. 모든 가능한 시뮬레이션 버그가 없다는 증명은 아니다.

## 실제 학습 곡선

평균 제품 수는 동일 screening seed 10개를 사용하는 5일 greedy 평가값이다.
`최고`는 screening 최고 평균이며 별도 final-selection seed에서 고른 best.pt iteration과 다를 수 있다.

| Worker | I0 | Screening 최고 | I75 | I45 이후 관측 비용 |
|---|---:|---:|---:|---:|
| 2 | 25.5 | I30: 41.5 | 37.6 | 3.17시간 |
| 3 | 43.3 | I30: 59.0 | 56.9 | 4.75시간 |
| 4 | 49.3 | I75: 66.4 | 66.4 | 6.71시간 |
| 5 | 64.8 | I45: 66.1 | 65.2 | 7.62시간 |
| 6 | 62.8 | I60: 66.9 | 65.3 | 8.92시간 |

후반 비용은 I45 이후 완료 training/screening wave wall time과 TD target/update 시간의 합이다.
Final selection, pool 초기화 등 일부 overhead는 제외했다. 새 코드 예상시간이 아니고,
모든 worker를 I45에 끝내도 된다는 뜻도 아니다. 그러면 worker 4의 1.4개와 worker 6의 3.8개
추가 screening 개선을 놓쳤을 것이다. 단일 학습 seed이므로 그 증가도 일반화 증거는 아니다.

- Worker 2: I75-I30 = -3.9개, paired 근사 CI [-4.58, -3.22], 10 seed 모두 하락.
- Worker 3: I75-I30 = -2.1개, paired 근사 CI [-3.04, -1.16], 1승/0무/9패.
- Worker 5와 6의 최고 대비 최종 차이 CI는 0을 포함한다. 단순 최고점만 보고 후반 저하를 확정하면 안 된다.
- 다른 worker 3 n=30 실험에서도 I25 59.0개에서 I50 56.4개로 하락했다.
  반면 파인튜닝 결과는 I0 57.4개에서 I20 59.4개로 개선됐다. 후반 학습이 항상 불필요한 것은 아니다.

Paired CI는 동일 seed 차이의 평균 ± 1.96×표본 표준편차/√10이다.
최고 checkpoint는 이 screening 자료에서 고른 것이므로 선택 편향과 반복 비교가 있다.
최종 독립 test의 유의성 검정으로 해석하지 않는다.

## 생산성 정체와 가치함수 수렴은 다르다

Worker 3의 I30→I75에서 제품 수는 59.0→56.9였지만 greedy MC RMSE는 13.27→1.89,
bias는 -4.92→-0.74로 개선됐다. I45에서도 RMSE 5.28이었다.
즉 후반 학습은 절대 미래 생산량 예측에는 기여했지만 더 좋은 행동을 선택하게 만들지는 못했다.
현재 정책이 방문하는 상태에 대한 오차이므로 동일 상태의 오차 감소를 증명하는 것도 아니다.

I0의 TD holdout MSE는 worker별 약 0.04~0.09로 처음부터 작지만 실제 MC RMSE는 약 16~38이었다.
Target도 초기 network 예측을 사용하므로 낮은 초기 TD loss는 수렴 증거가 아니다.
따라서 TD MSE의 정체를 조기 종료의 단독 조건으로 쓰지 않는다.

## 권장 설정: 변경 전 제안

네트워크, n=30, target_tau=0.03, initial 50, wave 10, 현재 10+과거 20 update,
replay 100, batch 512, epoch 2, CPU 10 process는 우선 유지한다.
최근 효율화로 target/rollout 비용도 달라졌으므로 여러 파라미터를 동시에 줄이지 않는다.

고정 75회 강제 실행 대신 다음 **생산성 기반 조기 종료 후보 규칙**을 권장한다.

1. 최대 75회는 유지하고, 최소 45회까지 학습한다.
2. 같은 screening seed 10개로 5 iteration마다 평가한다. 이미 기본 profile은 이 간격이다.
   Paper의 15회 간격은 종료 시점을 판별하기에 거칠다.
3. 최근 20 iteration 동안 best screening 평균을 갱신하지 못했을 때 종료 검토를 시작한다.
   작은 개선도 best 저장에는 반영한다. 이것은 candidate를 롤백하는 conservative gate가 아니다.
4. 최근 두 번의 검증에서 현재-best의 paired 근사 CI 상한이 +0.5개 미만이면
   추가 학습의 생산성 이득이 작다는 운영상 신호로 보고 종료한다.
   +0.5는 허용 가능한 실용적 차이의 예시이며 임계값은 실험 전에 고정한다.
   CI가 넓으면 개선 여지를 배제하기 어려우므로 최대 횟수까지 계속한다.
5. 종료 시 last를 채택하지 않고 screening 상위 2개를 별도 final-selection seed 20개로 평가한다.
   Held-out 정책 비교 seed는 종료 판단이나 checkpoint 선정에 사용하지 않는다.

위 규칙은 통계적 수렴 증명이나 늦은 개선이 없다는 보장이 아니다. 반복 screening의 선택 편향을
포함하는 계산비용/성능 절충 규칙이다. 실제 작동 여부는 v10 환경에서 확인해야 한다.
예측 정확도 자체도 목표라면 MC RMSE가 계속 크게 감소 중인 경우 종료를 보류할 수 있으나,
그 선택은 생산량 향상 없이 계산시간을 더 쓰게 할 수 있다.

Epsilon/LR schedule은 우선 현행 유지가 안전하다. 45회 종료면 미래의 75회 schedule까지 실행되지
않는다. 고정 45회 profile을 별도 실험할 때에만 낮은 epsilon/LR 구간을 35~45회로 당기는 ablation을
검토한다. 조기 종료와 schedule 변경을 한 번에 하면 단축 효과와 정책 변화 효과가 섞인다.

## 검증 산출물

- 배경 CUDA smoke: `outputs/adp_live_audit_20260926/smoke`.
  6 training + 4 validation episode, 3회 업데이트, 정상 종료. Training artifact audit 오류/경고 0.
- 회귀 테스트는 실패 시 계약 보존, phase별 수, atomic CSV, lock 해제,
  validation 전 update 수 및 wave 진행 중 시간 반영을 포함한다.
- 전체 pytest: **375 passed, 45 subtests passed**. `git diff --check` 통과.
- 실제 Chrome에서 로컬 HTML을 열어 1440px/390px 폭의 모니터와 학습 화면을 검사했다.
  8개 패널/10개 SVG, 상세 표 펼치기, 한글, 완료 상태와 진행률을 확인했고
  page 가로 overflow, NaN/Infinity 및 JavaScript exception은 발견하지 못했다.
  `outputs/adp_live_audit_20260926/browser_checks.json`과 같은 폴더의 PNG에 기록했다.
- 장시간 재학습과 정책 비교는 이번 검토에서 실행하지 않았다. 조기 종료 기능도 아직 켜지 않았다.
