# ADP 가치함수 독립 검증

## 기존 학습 오차와의 차이

- **Holdout MSE**: 기본 profile은 최근 5개 compact wave를 episode 단위로 나누어 early stopping에 사용하고, 현재 wave에 2배 가중치를 적용합니다.
- **Monte Carlo 가치 예측 오차(MAE/RMSE)**: 업데이트 후 현재 batch 전체에서 계산합니다. 학습 표본도 포함하며 독립 greedy validation 오차가 아닙니다.
- **예측·목표 표준편차**: 예측 규모와 상수 출력 붕괴를 확인하지만, 값이 비슷하다고 개별 예측이나 행동 순위가 정확한 것은 아닙니다.

위 지표는 유지하되, 단일 continuation으로 계산하던 기존 probe 순위·Top-1·regret 그래프는 아래 검증으로 대체합니다. 기존 결과 파일은 삭제하지 않습니다. 새 검증을 실행하지 않은 과거 결과는 `미실행`으로 표시하며 0으로 채우지 않습니다.

## 1. 독립 Greedy MC 가치 예측 오차

초기·best·마지막 checkpoint(중복 제외)를 각각 새로운 고정 seed 3개로 실행합니다. 행동 선택과 이후 행동 모두 평가하는 checkpoint의 greedy 정책이며, 진단 episode는 학습, early stopping, checkpoint 선택에 사용하지 않습니다.

각 decision의 post-decision 예측과 그 시점부터 종료까지 실제 completed product 증가량을 비교합니다.

\[
G_k=\sum_{j=k}^{K-1}R_j,\qquad
\mathrm{RMSE}=\sqrt{\frac1n\sum_k(\hat V(S_k^x)-G_k)^2},\qquad
\mathrm{Bias}=\frac1n\sum_k(\hat V(S_k^x)-G_k).
\]

같은 그래프에 시간만 사용하는 기준 RMSE를 표시합니다. 기준 예측은 remaining-horizon 비율의 2차 회귀이며, 평가 중인 seed 전체를 제외한 나머지 진단 episode에 적합합니다. 정책이나 행동 feature는 사용하지 않습니다. 각 점은 decision 표본 수로 가중된 오차이며, episode 수를 독립 표본 수로 해석해야 합니다.

이 검증은 **배포할 greedy 정책의 가치 보정 정확도**입니다. 학습 당시 이전 epsilon-greedy 정책의 MC target에 대한 오차와는 다릅니다. MC 확률 잡음도 포함하므로 RMSE가 0이어야 하는 것은 아닙니다. 평가 seed는 고정되어도 checkpoint별 방문 상태는 달라지며 고정 정답 시험이 아닙니다.

## 2. 동일 상태 반복 MC 행동 선택 손실

비용을 줄이기 위해 기본적으로 best checkpoint만 검사합니다. 별도 seed 1개에서 decision 20·100 이후의 첫 다중 행동 상태를 각각 포착합니다. 실제 beam 선택 행동을 반드시 포함하고 서로 다른 feasible 대안과 함께 최대 3개 후보를 평가합니다.

각 후보는 해당 상태까지 동일한 행동 prefix를 재현한 뒤 실행합니다. 분기 직전 pre-state와 선택 직후 post-state의 일치를 검사합니다. 후보당 미래 seed 5개를 사용하며 같은 상태의 모든 후보에 동일한 seed 집합을 적용합니다. 이후 정책은 평가 checkpoint의 greedy 정책으로 고정합니다.

\[
\widehat L(s)=\max_a\frac1R\sum_{r=1}^R G(s,a,r)
 -\frac1R\sum_{r=1}^R G(s,a_{\mathrm{beam}},r).
\]

앞서 추첨된 진행 중 작업시간, 잔여 고장 노출량, 예약된 event, 이동시간 cache는 유지합니다. 분기 이후 새로 발생하는 설비·primitive·이동시간, 품질, incident, 신규 고장 추첨만 바꿉니다. 따라서 **고정된 simulator 상태에 조건부인 미래 비교**이며 관측되지 않은 기존 잔여시간을 새로 추정하는 실험은 아닙니다.

구간은 미래 seed를 paired resampling한 centered bootstrap으로 후보 최대값 선택의 낙관 편향을 고려한 근사 구간입니다. 그래프는 상태별 손실과 구간 경계의 평균을 표시합니다. 이는 전체 공정 상태 모집단에 대한 CI가 아닙니다. 상태당 5회는 작은 표본이므로 근사 구간의 신뢰도도 제한적입니다. 0을 포함하면 우위를 확정하지 않으며, 후보를 완전 탐색한 전역 최적 손실로 해석하지 않습니다.

## 실행과 비용

기본 5.79시간 학습 profile에서는 `diagnostics.value_validation.enabled: false`입니다. 필요할 때 `true`로 켜면 학습과 best checkpoint 선정이 끝난 뒤 실행되며, 진단값으로 모델을 다시 고르지 않습니다. 기존 `1/50/12/1` 학습·선정 wave는 그대로이고 **진단 wave와 소요시간은 별도로** 표시합니다.

Worker 3 기본 설정은 MC 6~9 episode, 상태 포착 1 episode, 행동 continuation 최대 30 episode로 총 37~40 episode를 추가합니다. CPU는 최대 10 process이며 network update, 상세 events, Replay 저장은 하지 않습니다.

기존 checkpoint에 재학습 없이 적용:

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.value_validation `
  --training-dir outputs/<training-run>/<timestamp>
```

작은 검증(동일 horizon, 한 checkpoint, MC seed 2개, 상태 1개, 후보 2개, 미래 반복 3개; 총 9 episode):

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.value_validation `
  --training-dir outputs/<training-run>/<timestamp> --iterations 10 --smoke
```

화면만 재생성하려면 같은 명령에 `--render-only`를 사용합니다. 더 많은 checkpoint는 `--iterations 0 1 5 10`처럼 지정합니다. 이 옵션은 지정한 모든 checkpoint에서 MC와 행동 검증을 실행하므로 비용도 늘어납니다.

## 산출물

`<training-dir>/value_validation/<timestamp>/`에 다음 파일을 저장합니다.

- `config.json`, `summary.json`: seed 분리, 후속 정책, hash 불변 검사, 상태 수, 반복 수, 추가 실행시간
- `prediction_samples.csv`, `value_metrics.csv`: 스칼라 예측·raw MC target, RMSE·편향·시간 기준 오차
- `action_continuations.csv`, `action_metrics.csv`: 상태·후보·future seed별 결과, paired 손실과 구간
- `episode_metrics.csv`, `wave_metrics.csv`: 진단 실행 로그와 병렬 시간

큰 tensor, raw transition, 모델 가중치 복사본은 디스크에 저장하지 않습니다. 성공한 결과만 `value_validation_latest.json`으로 연결하며 기존 `training_dashboard.html`에 두 진단 그래프를 표시합니다. 실패한 진단은 실패 원인을 저장하고 성공값으로 대체하지 않습니다.
