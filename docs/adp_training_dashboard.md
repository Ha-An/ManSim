# ADP 실시간 학습 진단 화면

현재 n-step TD 학습과 실시간 모니터는 같은 renderer를 사용합니다. 17개 개별 그래프를
기본 표시 4개와 상세 진단 4개 묶음으로 정리했습니다. 상세 묶음 중 TD 구간과 OOD는 단위가
다른 두 subplot을 사용하므로 전체 SVG는 10개이며, 기본 화면에는 4개만 표시합니다.
모든 그래프 아래에 해석·산출식·유의사항이 있습니다. 추가 simulation이나 재학습은 필요하지 않습니다.

## 기본 표시

| 번호 | 그래프 | 주요 질문 |
|---|---|---|
| 1 | 정책 생산성: 학습 수집과 Greedy 검증 | 학습한 정책이 실제 제품을 더 만드는가? |
| 2 | 초기 Checkpoint 대비 생산량 변화 | 동일 worker·seed에서 I0보다 개선했는가? |
| 3 | n-step TD 적합 오차 | 현재 bootstrap target에 잘 맞추는가? |
| 4 | 실제 미래 생산량 예측 오차 | 독립 greedy trajectory의 잔여 생산량을 정확히 예측하는가? |

1번의 회색 iteration k는 업데이트 k **이전** 정책의 epsilon-greedy 학습 자료입니다.
보라색 k는 업데이트 k **이후** checkpoint를 epsilon=0으로 평가한 값입니다.
회색 0은 초기 Random Feasible 수집이고, 보라색 0은 그 자료로 초기 학습한 모델입니다.
Warm-start는 source 모델로 초기 replay를 채우며, 설정에 따라 초기 update를 생략합니다.
두 선은 정책과 seed가 달라 그 간격만으로 과적합을 단정하지 않습니다.

생산량 평균과 오차막대는 다음과 같습니다.

\[
\bar P_k=\frac{1}{N_k}\sum_e P_{k,e},\quad
s_k^2=\frac{\sum_e(P_{k,e}-\bar P_k)^2}{N_k-1},\quad
CI_k\approx\bar P_k\pm1.96\frac{s_k}{\sqrt{N_k}}.
\]

오차막대는 평균의 근사 95% 신뢰구간이지 episode 95%의 분포 구간이 아닙니다.
표준편차와 seed 수도 표로 보여줍니다. N=1은 CI를 표시하지 않고 표본 표준편차도 N/A입니다.
Screening은 gradient 학습에는 사용하지 않지만 checkpoint 선택에 사용되므로 최종 held-out test는 아닙니다.

### 2. 초기 대비 Paired 변화

`episode_metrics.csv`의 `screening_validation` 행을 `(worker_count, seed)`로 짝지어 계산합니다.

\[
d_{k,e}=P_{k,e}-P_{0,e},\qquad
\Delta_k=\frac{1}{N}\sum_e d_{k,e},\qquad
CI_{\Delta_k}\approx\Delta_k\pm1.96\frac{s(d_{k,e})}{\sqrt N}.
\]

- 양수는 초기 모델보다 개선, 음수는 저하입니다. Win/tie/loss는 차이의 부호별 seed 수입니다.
- 두 평균의 독립 CI를 단순히 빼지 않습니다. 동일 seed 차이의 표준편차를 사용합니다.
- Seed/worker 집합, 완료 수, 원본 평균이 맞지 않으면 해당 점을 제외하고 경고합니다.
- 아직 완료하지 않은 validation, final selection, policy test, training rollout은 포함하지 않습니다.
- I0의 0은 자기 자신과의 차이입니다. 여러 worker 수가 혼합된 결과의 CI는 임의로 합산하지 않습니다.
- 고정 screening seed와 I0를 여러 번 재사용하므로 checkpoint 간 점들이 독립적이지 않습니다.
  작은 N의 근사 CI이기도 하므로 최종 논문 유의성 검정이나 전체 곡선 동시 CI로 쓰지 않습니다.

### 3. TD MSE

실제 구현은 gamma=1에서 다음 target을 사용합니다.

\[
Y_j=\sum_{\ell=0}^{h_j-1}R_{j+\ell}
+\mathbb{1}_{\text{nonterminal}}\bar V(S^{x,\pi}_{j+h_j}),\qquad
MSE=\frac1J\sum_j(V_\theta(S_j^x)-Y_j)^2.
\]

Greedy 행동 map은 업데이트 시작 online network로 고정합니다. Endpoint value는 SGD 중
soft-update되는 target network로 계산합니다. 그래프는 업데이트 종료 후 최종 online/target
쌍으로 재계산한 train/holdout MSE이며, mini-batch loss의 단순 평균이 아닙니다.
Holdout은 episode ID가 10의 배수인 전체 episode로 고정되고 gradient에 쓰이지 않습니다.
Holdout이 없으면 N/A입니다. Replay와 target이 바뀌므로 iteration 간 같은 시험지의 loss는 아닙니다.
낮은 TD MSE는 bootstrap 자기일관성을 뜻할 수 있지만 실제 미래 생산량 정확성을 보장하지 않습니다.

### 4. Greedy MC RMSE와 편향

\[
G_j=\sum_{u=j}^{terminal}R_u,\quad e_j=V_\theta(S_j^x)-G_j,\quad
RMSE=\sqrt{\frac1J\sum_j e_j^2},\quad Bias=\frac1J\sum_j e_j.
\]

학습에 사용하지 않은 greedy validation의 모든 decision을 합쳐 계산합니다.
Decision이 많은 episode가 더 큰 가중치를 가지며 episode별 RMSE 평균과 다릅니다.
편향은 평균 예측에서 실제 MC 평균을 뺀 값이므로 기존 평균 예측/MC 평균 그래프는 같은
패널의 가치 보정 표로 통합했습니다. 평균 편향의 상쇄만으로 개별 오차가 작다고 볼 수 없습니다.
이 값은 각 decision의 잔여 생산량으로, episode 시작 때의 총 제품 수와 다릅니다.
MC는 진단용이고 TD target에 혼합하지 않습니다. 정확한 절대값도 행동 간 순위를 보장하지 않습니다.

## 상세 진단

### 5. 실제 TD 관측 범위

`effective_n_mean = mean(h_j)`와 `td_span_min_mean = mean(t_endpoint - t_j)`를 각각
decision 개수와 simulation 분의 독립 축으로 표시합니다. 이벤트 간 간격이 달라서 둘은 비례하지 않습니다.
`h_j`는 n 상한, 중간 행동 불일치 직전, terminal 중 코드상 먼저 만족한 조건까지의 reward 수입니다.
중단 사유 비율은 각 사유의 표본 수/J이고 세 비율의 합은 1입니다. 최소·최대 n과 중단 사유는
표에 통합했습니다. 이 구간을 미래를 완전히 예측하는 lookahead 길이로 해석하지 않습니다.

### 6. Replay

보유 episode, 현재 수집 episode, 과거에서 선택한 episode를 표시합니다.
업데이트 episode 합계는 현재+과거의 합이므로 중복 곡선 대신 표로 표시합니다.
선택된 episode에는 holdout도 포함되어 모든 표본이 gradient에 쓰이지는 않습니다.
Replay MiB = tensor와 직렬화 기준 제약 metadata bytes/2^20이며 전체 RAM과 다릅니다.
단독 메모리 그래프는 제거하고 replay/update MiB와 GPU peak를 통계 표로 옮겼습니다.

### 7. OOD

직전 batch의 reference/calibration으로 정규화한 afterstate 요약벡터를 사용합니다.
`d = min ||z-z_ref|| / sqrt(dimension)`, `q = calibration 거리의 설정 quantile`로 두고
`OOD rate = count(d>q)/evaluated selections`를 계산합니다.
추가 편향은 `mean(V-G | OOD) - mean(V-G | in-support)`입니다.
비율과 제품 수는 다른 단위이므로 한 패널의 두 subplot으로 구분했습니다.
업데이트 전 수집 정책의 greedy 선택에 대한 지표이고 이후 epsilon-greedy MC return이 기준입니다.
직전 batch 밖이라는 뜻이지 학습 전체에서 미관측이라는 뜻은 아닙니다. Reference나 어느 한
집단이 없으면 N/A입니다. 반사실 행동을 비교하지 않으므로 과대평가의 인과적 증거는 아닙니다.

### 8. 병렬 수집

Wave wall time을 초기/policy/screening/final phase 색으로 구분합니다.

\[
episodes/hour=3600\frac{\sum_w N_w}{\sum_w T_w},\qquad
utilization=\frac{\sum_w\sum_e T_e}{\sum_w p_wT_w}.
\]

Phase별 처리량과 활용률은 위 합계식으로 계산하며 wave별 비율을 단순 평균하지 않습니다.
`p_w`는 partial wave에서 실제 배정한 슬롯 수입니다. CPU 사용률이나 단일 process 대비 실측 speedup은 아닙니다.
완료 wave 표에는 wave 사이 처리나 pool 초기화, 가치망 학습이 빠지므로 end-to-end 시간과 다릅니다.
전체 Time Breakdown과 target/update 시간은 상세 표에 남아 있습니다. Process 수로 나누지 않습니다.

## 표로 옮긴 진단

Epsilon, learning rate, 후보 가치 엔트로피, 자발적 WAIT, 메모리, GPU update 시간은 iteration 표에서
확인합니다. WAIT 비활성 계약인데 양수가 기록되면 경고합니다. 불필요한 0 곡선을 만들지 않습니다.
후보 가치 엔트로피는 표준화 가치의 softmax에 대한 정규화 Shannon entropy입니다. 정책 확률의
엔트로피가 아니고 절대 가치 차이를 제거하므로 수렴이나 생산성의 직접 척도가 아닙니다.

원본 CSV/JSON, checkpoint, 학습 로직은 변경하지 않습니다. 파생 paired 지표는 HTML의
`adp-derived-metrics` JSON에 포함되어 원본과 독립 검산할 수 있습니다.
실시간 갱신 중 상세 열기 상태와 스크롤은 동일 탭의 sessionStorage에 보존합니다.
임베드 화면은 바깥 모니터와 겹치는 상단 실행 카드만 숨깁니다. 기존 결과의 재실험 경고는 숨기지 않습니다.
