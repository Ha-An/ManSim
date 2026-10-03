# ADP 백그라운드 학습과 실시간 모니터

## 단일 학습

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train --background
```

기존 `--config`, `--output`, `--worker-counts` 등의 옵션을 그대로 사용할 수 있습니다.
명령은 독립 supervisor를 생성한 직후 반환하므로 Codex에서 다른 작업을 이어갈 수 있습니다.
학습 parent는 신경망을 업데이트하고 기존 CPU rollout process pool을 그대로 사용합니다.
병렬 수, GPU 장치, seed, 보상, 학습 target 및 checkpoint 선정 방법은 바꾸지 않습니다.

출력 경로를 직접 지정하는 예:

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train --background `
  --config configs/adp/mfg_flow_shop_throughput.yaml `
  --output outputs/my_new_adp_run
```

기존 결과 경로는 덮어쓰지 않습니다. 동일 output을 사용하는 백그라운드 작업의 중복 실행도 차단합니다.
브라우저는 Chrome을 우선 사용하며, 없으면 시스템 기본 브라우저로 엽니다.
`--no-open-dashboard`는 자동 열기만 끄며 로그와 모니터 파일은 계속 생성됩니다.
터미널에서 완료까지 기다려야 하는 스크립트는 기존처럼 `--background` 없이 실행합니다.

## 실시간 화면

시작 시 출력되는 `background_job/live_training.html`을 엽니다. 별도 서버나 라이브러리 설치 없이
로컬 HTML이 5초마다 자동으로 갱신되며 완료 또는 실패하면 자동 갱신을 종료합니다.

- 현재 단계: random/policy rollout, TD target 구성, 가치망 업데이트, validation, checkpoint 저장
- 현재 iteration, wave ID, wave 안에서 완료된 episode 수
- 전체 예정 episode와 완료 수: 학습과 validation을 합한 **개수 기준 진행률**이며 남은 시간 예측은 아님
- wall-clock 경과시간, rollout process 수와 실제 training device
- 기존 학습 대시보드의 곡선과 최근 console log
- 학습 목록: worker별 연속 학습에서 현재 실행과 완료된 결과 링크

학습 곡선은 핵심 4개와 펼쳐보는 상세 4개 묶음으로 구성합니다. 중복된 평균/편향 그래프와
상수에 가까운 설정 그래프는 표로 통합했고, 같은 seed의 초기 checkpoint 대비 생산량 차이를
추가했습니다. 각 그래프의 해석·산출식·유의사항은 화면과 [그래프 안내](adp_training_dashboard.md)에 있습니다.

Episode 완료 즉시 진행 숫자를 갱신하고, wave 완료 및 iteration 완료 시 기존 TD 대시보드를 다시 씁니다.
아직 평가하지 않은 checkpoint 성능이나 업데이트하지 않은 MSE를 0으로 만들지 않습니다.
현재 episode의 내부 sim time이나 mini-batch 단위 GPU 진행률은 표시하지 않습니다.
오래 걸리는 wave/target 구성 동안 supervisor heartbeat와 elapsed time은 계속 갱신됩니다.
Heartbeat는 supervisor 생존 신호이며 학습 프로세스가 정상 진전 중이라는 증거는 아닙니다.
마지막 학습 진행 갱신 시각과 로그를 함께 확인하세요. 표시 시각은 UTC입니다.

Supervisor heartbeat는 약 2초마다 저장합니다. 비정상 종료 또는 재부팅으로 갱신이 30초 이상
끊기면 화면과 status 명령에서 응답 없음으로 표시합니다. 실행 중으로 오인하지 않도록 구분합니다.

## 상태 확인과 중단

아래 `<job_directory>`는 실행 명령에서 출력한 `Job:` 경로입니다.

```powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.background status --job <job_directory>
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.background open --job <job_directory>
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.background stop --job <job_directory>
```

`stop`은 요청 파일을 남기고, supervisor가 학습 및 rollout 자식 process를 함께 종료합니다.
이미 저장된 checkpoint/CSV는 삭제하지 않지만 진행 중인 wave와 저장 중이던 checkpoint는 완전하지 않을 수 있습니다.
이는 재개 가능한 pause가 아닙니다. 현재 TD driver는 replay를 디스크에 저장하지 않으므로
정확한 중간 재개는 지원하지 않습니다. Warm-start와 처음부터 재실행은 별도 선택입니다.

PC 전원이 켜져 있는 동안 터미널/Codex 세션과 독립적으로 실행됩니다. 절전·재부팅 이후의 자동
복구는 지원하지 않습니다. 다른 작업에서 학습 중인 코드/설정을 수정하거나 GPU 학습을 중복
시작하면 결과 재현성과 메모리에 영향을 줄 수 있으므로 피하세요.
강제 종료 뒤 남은 `.adp_background.lock`은 실제 학습 process가 없는지 확인한 뒤에만 정리해야 합니다.

## 논문용 worker별 연속 학습

새로 준비된 실험 디렉터리에 대해 다음과 같이 실행합니다.

```powershell
.\.venv\Scripts\python.exe experiments/mfg_flow_shop_paper/run_training.py `
  --prepared experiments/mfg_flow_shop_paper/prepared --background
```

5개 학습을 순차 실행하는 runner 자체를 백그라운드로 분리합니다. 각 학습이 끝나면 기존처럼
학습 audit를 수행하고 결과 대시보드를 엽니다. 통합 모니터는
`<prepared>/background_training/<timestamp>/live_training.html`에 있습니다.
정책 비교를 자동 추가하는 옵션은 아니며, 평가 실행은 기존 `run_evaluation.py`를 사용합니다.

## 산출물

| 파일 | 내용 |
|---|---|
| `training_progress.json` | 학습 parent가 기록한 최근 단계와 실제 완료 episode 수 |
| `background_job/job.json` | 실행 명령, cwd, 학습 output 목록 |
| `background_job/status.json` | supervisor/child PID, heartbeat, elapsed, 종료 코드 |
| `background_job/console.log` | 학습 stdout/stderr, 오류 traceback |
| `background_job/supervisor.log` | supervisor 자체 오류 |
| `background_job/live_training.html` | 자동 갱신 모니터 |

JSON과 모니터 HTML은 임시 파일을 쓴 뒤 교체하므로 갱신 중 잘린 내용을 읽지 않습니다.
완료된 학습 그래프와 원본 통계 파일의 계약은 그대로 유지합니다.
