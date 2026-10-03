# 2026-09-26 정책 비교·학습 결과 정밀 감사

## 결론

대상은 `experiments/mfg_flow_shop_paper/results/20260919_212305`의
6개 정책 × worker 2~6대 × 공통 seed 20개, 총 600개 평가 결과와
worker별 ADP 학습 5회입니다.

**실제 시뮬레이션 동작 오류가 발견되었습니다. 기존 정책 성능의 최종 결론은 보류해야 합니다.**
설비 루프가 예방정비 중인 설비를 WAIT_INPUT으로 덮어쓰거나 가공을 시작했습니다.
원본 간트에서 124개 run의 PM·가공 구간 중첩을 확인했고, 동일 seed의 상세 이벤트
재실행에서도 재현했습니다. 수정 이후 원본과 생산량이 달라졌습니다.

원본 run, 학습 로그와 체크포인트는 삭제하지 않았습니다. 집계 수정 전 CSV/HTML은
`deep_audit_20260926/before_statistics_fix/`에 보존했습니다.
학습·비교 대시보드에는 재실험 필요 경고를 표시했습니다.

## 범위와 한계

- 600개 run의 KPI, 일별 생산량, 간트 구간, 실행 로그, run 식별자와 config를 확인했습니다.
- worker·seed별 100개 그룹의 공정성 및 난수열 기록을 다시 확인했습니다.
- ADP 학습은 총 4,000 training episode와 400 validation episode입니다.
  episode/wave/iteration 수, seed 분리, target 기록, checkpoint 선정과 best 모델의 유한값을 검사했습니다.
- 정책 비교 그래프 12개, 평균점 354개, 표준편차 밴드 72개를 원본 KPI로 검산했습니다.
- worker별 요약 420개 metric 그룹을 독립적으로 재집계하고, 브라우저의 로컬 링크를 확인했습니다.
- 원본 600개는 상세 events/Replay를 저장하지 않았습니다. 따라서 원본 전체의
  tile별 순간이동·item 이동을 사후에 완전히 복원하여 검증할 수는 없습니다.
- 상세 이벤트를 켠 수정 전 10회와 수정 후 10회의 5일 실행,
  Factory·Shipyard 각각 1일 회귀 실행으로 부족한 검증을 보완했습니다.
- 이번 수정 후 ADP 재실행은 기존 모델을 사용한 실행 경로 진단입니다.
  수정된 환경에서 새로 학습한 ADP의 성능 입증 실험이 아닙니다.

## 발견 및 수정

### 1. 예방정비와 설비 가공 동시 실행

`machine_lifecycle()`이 `UNDER_PM`과 PM owner를 확인하지 않았습니다.
정비 중에도 WAIT_INPUT을 다시 설정하여 load/setup 후보가 노출되고,
입력이 준비되면 PM 완료 전 가공할 수 있었습니다.

직접 재현: Immediate Shared, worker 3, seed 910007, S2M1.

- PM 시작: 1285.807분
- 가공 시작: 1305.311분
- PM 종료: 1313.427분

약 8.116분 동안 동일 설비에서 PM과 가공이 겹쳤습니다.
수정 전 생산량 54개, 수정 후 53개였습니다.

수정 내용:

- 예약 또는 실행 중 PM은 설비 실행 루프가 덮어쓰지 않도록 보호합니다.
- PM 중단 시 소요된 시간을 기록하고 owner·상태를 정리합니다.
- 완료되지 않은 PM에는 보호 효과나 완료 횟수를 부여하지 않습니다.
- PM·고장 구간이 horizon에서 열려 있어도 간트에 종료시각까지 표시합니다.
- 이벤트 저장을 끈 실행에서도 간트의 설비 구간 중첩을 검사합니다.
- `exclusive_pm_v1` 계약을 run metadata와 ADP environment fingerprint에 넣었습니다.
  이전 환경의 checkpoint는 새 실행에서 거부하고, 이전 평가 결과도 자동 skip하지 않습니다.

중첩이 직접 관측된 원본 run 수:

| 정책 | run 수 |
| --- | ---: |
| immediate_dedicated_roles | 59 |
| immediate_shared | 55 |
| simulation_based_adp | 9 |
| rolling_horizon_shared | 1 |
| 나머지 두 정책 | 0 |

0인 정책이 영향을 받지 않았다는 뜻은 아닙니다. PM 중 상태 덮어쓰기는 실제 가공
중첩이 발생하지 않아도 후보 생성과 의사결정 시각을 바꿀 수 있습니다.
따라서 124개만 바꿔 끼우지 않고 전체 실험 블록을 재실행해야 합니다.

### 2. 통계·공정성 검사

- 전체 worker 평균 bootstrap에서 worker마다 seed를 독립 추출하던 오류를 수정했습니다.
  같은 seed의 worker별 결과를 하나의 block으로 재추출합니다.
- 관측된 효과를 중심으로 한 bootstrap tail 비율을 p-value로 쓰던 계산을
  null-centered bootstrap으로 변경하고 finite-resample plus-one 보정을 적용했습니다.
  이는 근사 검정이며 worker별 다중 비교에는 Holm 보정을 유지합니다.
- ADP 학습 반복이 1회일 때 반복 간 표준편차를 0으로 표시하지 않고 공란으로 둡니다.
- 난수열 검사가 가장 짧은 prefix 이후의 불일치를 놓치지 않도록 수정했습니다.
- run ID/path/실험 조합 중복과 누락, run_meta·KPI의 seed/mode/worker 식별자를 검사합니다.
- paper CSV와 대시보드가 서로 다른 bootstrap으로 생산량 CI를 계산하지 않도록 통일했습니다.
- 누적 실행시간은 이후 실행부터 별도 timing 기록을 사용합니다.
  status 파일을 다시 쓰거나 이어 실행해도 과거 wall time이 사라지지 않으며 실제 jobs 수를 표시합니다.

기존 원본의 생산량 평균·표준편차에는 산술 오류가 없었습니다.
전체 `ADP - Immediate Shared` 평균은 여전히 +0.64개이며,
seed block 방식의 95% CI는 [-0.52, 1.82025]입니다.
**이는 오류가 있는 과거 시뮬레이션의 기술통계일 뿐, 수정 환경의 성능 결론이 아닙니다.**

### 3. 화면·설명

- 학습·비교 대시보드 상단에 시뮬레이션 유효성 경고를 넣었습니다.
- 정책 우위 판정 카드는 재실험 전 판단 보류로 표시합니다.
- worker 수를 합친 상단 표의 표준편차에는 fleet 차이도 포함된다고 명시했습니다.
  worker 수를 고정한 seed 변동성은 아래 worker별 표/그래프로 확인합니다.
- 실행비율에는 충전 태스크 수행시간도 포함된다는 설명을 추가했습니다.
- NaN/Infinity는 그래프 좌표로 렌더링하지 않습니다.
- paper 생산 기준선은 현재 YAML이 아닌 해당 run의 보존된 resolved config로 계산합니다.
- Chrome의 실제 1600px/390px viewport에서 비교·학습 화면을 확인했습니다.
  가로 넘침, 빈 SVG 그래프, JavaScript 예외 및 끊어진 로컬 링크가 없었습니다.

## 수정 후 재실행

| 정책 | worker | seed | 수정 전 제품 | 수정 후 제품 |
| --- | ---: | ---: | ---: | ---: |
| Random Feasible | 2 | 910001 | 22 | 21 |
| Immediate Shared | 3 | 910001 | 61 | 61 |
| Random Feasible | 3 | 910001 | 41 | 39 |
| ADP | 3 | 910001 | 62 | 63 |
| ADP | 5 | 910017 | 37 | 37 |
| ADP | 6 | 910015 | 38 | 38 |
| Immediate Dedicated | 3 | 910001 | 43 | 43 |
| Rolling Shared | 3 | 910001 | 45 | 46 |
| Rolling Dedicated | 3 | 910001 | 39 | 39 |
| Immediate Shared, PM 재현 조건 | 3 | 910007 | 54 | 53 |

위 10개 실행 모두 기존 artifact/KPI 감사와 독립 KPI·간트 검사를 통과했습니다.
공간 연속성 감사, buffer overflow/reservation leak 검사에서도 오류가 없었습니다.
Factory·Shipyard 1일 실행도 artifact/KPI 감사를 통과했습니다.

전체 테스트를 수정 전·중·후 반복 실행했습니다. 최종 결과는 **341개 테스트 및 45개 subtest 통과,
실패 0개**입니다. 최종 테스트 결과와 상세 감사 JSON은
같은 실험의 `deep_audit_20260926/`에 보관합니다. 확인한 범위에서 남은 수정 후 실패는
없지만 모든 가능한 seed/상태의 무결함을 보장하는 것은 아닙니다.

## 다음 정식 실험

1. 수정된 환경에서 worker 2~6의 ADP를 새 timestamp 디렉터리에 재학습합니다.
2. 기존 test seed 910001~910020을 유지하여 6개 정책 전체를 재평가합니다.
3. 새 실험은 이전 결과와 섞지 않고 별도 대시보드를 생성합니다.
4. 재학습 5회와 정식 600회 평가는 이번 버그 수정 검증에서 수행하지 않았습니다.
   이미 관찰한 test seed를 이용한 수정 후 반복 평가라는 사실도 논문에 명시해야 합니다.

증거 파일:

- `deep_audit_20260926/source_artifact_audit.json`
- `deep_audit_20260926/pm_overlap_event_evidence.json`
- `deep_audit_20260926/training_audit.json`
- `deep_audit_20260926/after_fix/reproduction_report.json`
- `deep_audit_20260926/after_fix/source_artifact_audit.json`
- `deep_audit_20260926/scenario_regression_audit.json`
- `deep_audit_20260926/summary_link_check.json`
- `deep_audit_20260926/browser_report.json`
- `deep_audit_20260926/final_verification.json`
