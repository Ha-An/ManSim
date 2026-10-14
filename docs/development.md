# 개발 검증과 업로드 준비

## 환경

저장소 루트에서 실행합니다. HumanoidSim editable 설치가 필요합니다.
전체 ADP 테스트에는 선택 의존성인 PyTorch도 필요하며, 실제 CUDA 경로 검증에는 CUDA 장치가 필요합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-adp.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m humanoidsim validate-catalog
```

기본 규칙 기반 시뮬레이션은 PyTorch 없이 계속 사용할 수 있습니다.
개발 검증 도구는 runtime requirements에 포함시키지 않습니다.

## Python 검사

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m ruff check agents manufacturing_sim runtime experiments scripts tests dashboards main.py --select E9,F63,F7,F82,F601 --exclude results
git diff --check
```

전체 테스트는 pytest로 실행합니다. `unittest discover`만 사용하면 함수형 pytest 테스트를 누락합니다.
정적 검사는 구문 오류와 미정의 이름 등 실행 위험 항목을 우선 검사하며, 전체 스타일 통일을 의미하지 않습니다.
테스트 통과는 모든 seed의 무결함이나 학습 성능 향상을 보장하지 않습니다.
장시간 실험이 실행 중이면 학습 파라미터·정책·시뮬레이션 상태 전이 코드를 변경하지 마세요.

## Frontend

각 디렉터리에서 설치 후 build를 확인합니다. 지원 Node.js 버전은 package.json을 따릅니다.

```powershell
npm --prefix replay_studio_3d ci
npm --prefix replay_studio_3d run build
npm --prefix replay_studio_3d test
npm --prefix replay_studio ci
npm --prefix replay_studio run build
```

3D visual 검증은 `npm --prefix replay_studio_3d run test:visual`로 별도 실행합니다.
Build/unit test만 실행한 경우 실제 3D 재생이나 브라우저 공간 연속성을 검증했다고 해석하지 않습니다.
실시간 학습과 정책 비교 HTML은 Python renderer가 생성하므로 별도로 화면과 수치 집계를 확인합니다.

## 산출물 감사

```powershell
.\.venv\Scripts\python.exe scripts/audit_kpi.py outputs/my_run
.\.venv\Scripts\python.exe scripts/audit_run_artifacts.py outputs/my_run
.\.venv\Scripts\python.exe scripts/audit_adp_training.py outputs/my_training
```

KPI·통계 검증과 상세 이벤트 검증은 범위가 다릅니다.
Artifact 감사는 저장된 Hydra 설정의 Replay 옵션과 run metadata의 event 옵션을 함께 확인합니다.
Runtime의 Replay 생성에는 저장된 events가 필요합니다. Events만 저장한 실행은 Replay 누락으로 실패하지 않습니다.
Events 저장이 비활성인 결과에 Replay 파일이 보존돼 있으면 Replay 자체를 검사하되 상세 event 감사는 생략합니다.
`--skip-replay-log`는 명시적으로 Replay 감사만 생략할 때 사용합니다.
원본 events를 저장하지 않은 실행에 대해 이벤트 단위 공간 연속성 검사를 통과했다고 보고하지 않습니다.
삭제된 산출물이 있으면 정리 기록과 남아 있는 파일을 기준으로 확인 범위를 제한합니다.

## Git 점검

```powershell
git status --short
git diff --stat
git diff --check
git diff --cached --name-only
```

코드, 설정, 테스트와 공개 사용 설명만 업로드합니다.
실험 결과, checkpoint, 로그, 가상환경, 캐시와 로컬 전용 자료는 제외합니다.
대형 파일을 force-add하지 말고 staged 목록을 확인하세요. `.gitignore`는 이미 추적 중인 파일을 자동 제거하지 않습니다.
검토 준비와 실제 commit/push는 별도 단계입니다.

최근 검증 결과는 [2026-10-14 점검 기록](audits/2026-10-14_release_readiness.md)을 참고하세요.
