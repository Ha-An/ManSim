# ManSim Installation Guide

이 문서는 빈 개발 환경에서 ManSim v0.5를 설치하고 simulation, Results Hub, 3D Replay Studio를 실행하는 절차를 정리합니다.

## 1. System Requirements

| 구성요소 | 요구사항 | 용도 |
| --- | --- | --- |
| Python | 3.12 이상 | ManSim과 HumanoidSim runtime |
| Git | 최근 안정 버전 | 두 저장소 clone |
| Node.js | 20.19 이상, 24 권장, 선택 | 개별 run의 3D Replay Studio |
| npm | Node.js에 포함, 선택 | Replay frontend dependency/build |

ManSim의 기본 설치 대상은 `factory_mfg_basic` 6개 정책 비교 실험입니다. 이 실험과 정적 비교 dashboard에는 Node.js, ROS2, Gazebo, OpenClaw CLI, 외부 LLM API가 필요하지 않습니다. 이들은 개별 3D replay 또는 해당 integration을 명시적으로 사용할 때만 설치합니다.

## 2. Repository Layout

기본 명령은 두 저장소가 같은 상위 폴더에 있다고 가정합니다.

```text
C:\Github\
  ManSim\
  HumanoidSim\
```

```powershell
cd C:\Github
git clone https://github.com/Ha-An/ManSim.git
git clone https://github.com/Ha-An/HumanoidSim.git
```

이미 저장소가 있다면 clone 단계는 생략합니다. HumanoidSim이 다른 위치에 있어도 설치 명령에 절대 경로를 지정하면 됩니다.

## 3. Python Environment

### Windows PowerShell

```powershell
cd C:\Github\ManSim
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ..\HumanoidSim
```

`py -3.12`가 인식되지 않으면 먼저 `python --version`으로 Python 3.12 이상인지 확인한 뒤 `python -m venv .venv`를 사용합니다. 여러 Python이 설치된 환경에서는 Python 3.12 설치 경로의 `python.exe`를 직접 지정합니다.

### Linux/macOS

```bash
cd /path/to/ManSim
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m pip install -e ../HumanoidSim
```

HumanoidSim은 editable install합니다. 따라서 sibling working tree의 State, Task, Primitive, Incident 변경이 재설치 없이 ManSim import에 반영됩니다.

## 4. Python Libraries

`requirements.txt`는 6개 정책 비교 실험에 필요한 최소 Python package를 설치합니다.

| 라이브러리 | 역할 |
| --- | --- |
| `hydra-core`, `omegaconf` | scenario/decision/runtime 설정 조합과 CLI override |
| `simpy` | discrete-event simulation engine |
| `ortools` | `rolling_horizon_throughput_optimizer`의 CP-SAT solver |
| `pandas`, `plotly` | KPI 집계와 정적 비교 dashboard 생성 |

`ortools`는 throughput optimizer mode에서 필수이며 solver fallback은 없습니다. 다음 package는 기본 실험에 설치하지 않습니다.

| 선택 라이브러리 | 용도 |
| --- | --- |
| `streamlit` | legacy 2D Replay UI |
| `graphifyy` | Knowledge Graph 경로 |
| `openai` | optional LLM manager/curator 경로 |

선택 기능까지 필요한 경우에만 설치합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-optional.txt
```

## 5. Installation Check

Windows:

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m humanoidsim validate-catalog
.\.venv\Scripts\python.exe -c "import humanoidsim; print(humanoidsim.__file__)"
.\.venv\Scripts\python.exe -c "from ortools.sat.python import cp_model; print('OR-Tools OK')"
.\.venv\Scripts\python.exe -m pip check
```

마지막 출력이 의도한 HumanoidSim working tree를 가리키는지 확인합니다. 전체 HumanoidSim 검증이 필요하면 sibling 저장소에서 다음 명령을 실행합니다.

```powershell
cd C:\Github\HumanoidSim
..\ManSim\.venv\Scripts\python.exe -m humanoidsim validate-lab --all --out outputs\validation\latest
```

## 6. Run The Six-Policy Experiment

명령 생성과 공통 override를 먼저 확인합니다.

```powershell
cd C:\Github\ManSim
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --dry-run
```

짧은 smoke experiment:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py --worker-counts 3 --seeds 2026 --days 1
```

이 명령은 6개 정책을 worker 3대, seed 2026, 1일 horizon으로 각각 한 번 실행합니다. 6개 run과 audit, 집계, dashboard 자동 열기까지 통과하면 기본 실험 설치가 완료된 것입니다.

기본 full experiment:

```powershell
.\.venv\Scripts\python.exe experiments\factory_policy_comparison\run_experiment.py
```

Full experiment는 6개 정책, worker 3~8대, seed 5개, 5일 horizon의 180개 run입니다. 결과 비교 화면은 생성된 결과 폴더의 `comparison_dashboard.html`이며 audit와 집계가 끝나면 기본 브라우저에서 자동으로 열립니다. GUI가 없는 서버나 CI에서는 `--no-open-dashboard`를 사용합니다. Artifact audit은 worker 경로의 인접 타일 연속성, map 경계, wall/blocking object 통과, Replay 경로 endpoint, item pickup/carry/drop과 destination state 연결을 검사합니다. 공정성, artifact 또는 KPI audit가 실패하면 runner는 non-zero exit code로 종료됩니다.

## 7. Optional Replay Studio 3D

```powershell
cd C:\Github\ManSim\replay_studio_3d
npm ci
npm run build
```

개발 서버를 직접 실행할 때:

```powershell
npm run dev -- --port 5174
```

일반 ManSim run에서는 `runtime.ui.auto_start_replay_studio_3d=true`가 기본이며, Node.js/npm과 frontend dependency가 준비되어 있으면 Replay Studio를 자동으로 시작합니다. 자세한 frontend 계약은 [Replay Studio 3D README](../replay_studio_3d/README.md)를 참고합니다.

## 8. Run A Single Simulation

```powershell
cd C:\Github\ManSim
.\.venv\Scripts\python.exe main.py
```

Factory와 Shipyard를 명시적으로 실행할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe main.py scenario=factory_mfg_basic scenario.horizon.num_days=5
.\.venv\Scripts\python.exe main.py scenario=shipyard_basic scenario.horizon.num_days=5
```

결과 파일은 `outputs/YYYY-MM-DD/HH-MM-SS/`에 생성됩니다. batch나 CI에서는 브라우저와 frontend server를 끕니다.

```powershell
.\.venv\Scripts\python.exe main.py runtime.ui.auto_open_results=false runtime.ui.auto_start_replay_studio_3d=false
```

## 9. Optional Features

- `rolling_horizon_throughput_optimizer`: `requirements.txt`의 OR-Tools가 반드시 필요합니다.
- LLM/OpenClaw manager modes: `requirements-optional.txt`, provider credential과 manager별 설정이 추가로 필요합니다. 6개 정책 비교에는 필요하지 않습니다.
- Knowledge Graph: graph/curator 설정이 활성화된 run에서만 사용합니다.
- HumanoidSim ROS2/Gazebo validation: ManSim 실행과 독립된 HumanoidSim integration입니다. HumanoidSim의 ROS 문서를 따릅니다.

## 10. Troubleshooting

### `No module named humanoidsim`

ManSim 가상환경으로 HumanoidSim을 다시 editable install합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ..\HumanoidSim
```

### OR-Tools import 또는 solver 오류

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -c "from ortools.sat.python import cp_model; print('OR-Tools OK')"
```

Optimizer가 `INFEASIBLE`, `UNKNOWN`, `MODEL_INVALID`를 반환하면 해당 policy는 의도적으로 run을 중단하며 다른 정책으로 fallback하지 않습니다.

### Results Hub는 열리지만 3D Replay가 열리지 않음

Node.js/npm 설치와 frontend dependency를 확인합니다.

```powershell
node --version
npm --version
cd replay_studio_3d
npm ci
npm run build
```

### 최신 run Hub 다시 열기

```powershell
.\.venv\Scripts\python.exe -m dashboards.manifest --latest
```

### Run 결과 검사

```powershell
.\.venv\Scripts\python.exe scripts\audit_run_artifacts.py outputs\YYYY-MM-DD\HH-MM-SS
.\.venv\Scripts\python.exe scripts\audit_kpi.py outputs\YYYY-MM-DD\HH-MM-SS
```
