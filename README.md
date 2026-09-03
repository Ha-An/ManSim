# ManSim

ManSim은 휴머노이드 작업자가 포함된 제조 및 조선 공정을 실행하는 discrete-event simulation
워크스페이스입니다. 결과는 공통 Results Hub, KPI dashboard, Gantt chart와 Replay Studio 3D에서
확인할 수 있습니다.

현재 릴리스 기준은 **ManSim v0.6.0**입니다. Worker의 State, Task, Primitive와 Incident 정의는
HumanoidSim이 소유하고, ManSim은 시나리오 상태에서 task 후보를 만들고 실행 결과를 기록합니다.

![Replay Studio factory replay](docs/assets/replay-studio-worker-replay.png)

## 주요 기능

- scenario registry 기반의 <code>factory_mfg_basic</code>, <code>mfg_flow_shop</code>, <code>shipyard_basic</code>
- HumanoidSim task/primitive catalog와 시나리오별 확률 timing profile
- Immediate, Strict Periodic Rolling Horizon, dedicated-role 정책
- OR-Tools throughput optimizer와 선택형 Simulation-Based ADP
- KPI, OTC, Gantt, 공통 Hub와 Replay Studio 3D
- artifact, KPI, finite-buffer, task lifecycle와 공간 이동 감사
- seed, worker 수, 정책과 목적함수를 통제하는 policy comparison suite

세부 변경 사항은 [v0.6 Release Notes](docs/v0.6_release_notes.md)를 참고합니다.

## 시나리오

| Scenario | 설명 | 주요 결과 |
| --- | --- | --- |
| <code>factory_mfg_basic</code> | Station 1, Station 2, Inspection으로 이어지는 기본 제조 시나리오 | accepted products, throughput, lead time |
| <code>mfg_flow_shop</code> | 병렬 설비, 유한 버퍼, inspection desk와 worker별 charging dock를 사용하는 flow shop | throughput 또는 initial-batch makespan |
| <code>shipyard_basic</code> | 선박 외관 tile의 용접, 표면처리, 도장, 검사와 cart logistics | completed surface tiles, makespan |

기본 scenario는 <code>factory_mfg_basic</code>이며 명시적으로 변경할 수 있습니다.

~~~powershell
.\.venv\Scripts\python.exe main.py scenario=mfg_flow_shop
.\.venv\Scripts\python.exe main.py scenario=shipyard_basic
~~~

## 설치

Python 3.12 이상을 권장합니다. 기본 rule-based policy 실험과 simulation에는 외부 LLM,
ROS2, Gazebo, Omniverse가 필요하지 않습니다.

~~~powershell
cd C:\Github
git clone https://github.com/Ha-An/ManSim.git
git clone https://github.com/Ha-An/HumanoidSim.git
cd ManSim
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e ..\HumanoidSim
python -m humanoidsim validate-catalog
python -m pip check
~~~

ADP 학습과 추론을 사용할 때만 CUDA PyTorch가 포함된 선택 의존성을 설치합니다.

~~~powershell
python -m pip install -r requirements-adp.txt
~~~

설치 옵션과 GPU 확인 방법은 [Installation Guide](docs/installation.md)에 정리되어 있습니다.

## 빠른 실행

~~~powershell
.\.venv\Scripts\python.exe main.py scenario=factory_mfg_basic decision=rolling_horizon_dedicated_roles scenario.horizon.num_days=5
~~~

브라우저 자동 실행을 끄려면 다음 override를 사용합니다.

~~~powershell
.\.venv\Scripts\python.exe main.py runtime.ui.auto_open_results=false runtime.ui.auto_start_replay_studio_3d=false
~~~

Hydra 출력 경로를 고정할 수 있습니다.

~~~powershell
.\.venv\Scripts\python.exe main.py scenario=mfg_flow_shop decision=immediate_shared hydra.run.dir=outputs/my_run
~~~

## 기본 정책 비교 실험

기본 profile은 `mfg_flow_shop`의 두 목적함수, 4개 rule-based 정책, worker 3~6명과 seed 2026을
조합한 32개 run입니다. 완료 후 audit과 집계를 수행하고 `comparison_dashboard.html`을 자동으로
엽니다.

~~~powershell
.\.venv\Scripts\python.exe experiments/factory_policy_comparison/run_experiment.py --dry-run
.\.venv\Scripts\python.exe experiments/factory_policy_comparison/run_experiment.py
~~~

브라우저를 열 수 없는 환경에서는 `--no-open-dashboard`를 추가합니다. 자세한 실행 옵션과 결과
구조는 [Factory Policy Comparison](experiments/factory_policy_comparison/README.md)을 참고합니다.

## Legacy Factory 6-Policy 비교 실험

기본 factory 실험은 다음 정책을 같은 scenario, horizon과 seed에서 비교합니다.

- <code>fixed_priority</code>
- <code>adaptive_priority</code>
- <code>rolling_horizon_aging_priority</code>
- <code>rolling_horizon_dedicated_roles</code>
- <code>bottleneck_aware_dispatch</code>
- <code>rolling_horizon_throughput_optimizer</code>

~~~powershell
.\.venv\Scripts\python.exe experiments/factory_policy_comparison/run_experiment.py --config experiments/factory_policy_comparison/config_factory_mfg_basic.yaml
~~~

Runner는 run별 KPI와 config fingerprint를 확인하고 결과를 CSV, JSON과
<code>comparison_dashboard.html</code>로 집계합니다. OR-Tools 정책은 OR-Tools가 없거나 feasible
solution을 만들지 못하면 fallback 없이 실패합니다.

## mfg_flow_shop

<code>mfg_flow_shop</code>은 Station 1과 Station 2에 각각 병렬 설비 2대를 둡니다.

- Station 1 가공시간: triangular <code>7 / 8.5 / 11분</code>
- Station 2 가공시간: triangular <code>10 / 12.5 / 16분</code>
- Inspection desk capacity: 1
- Station 1 material input/output capacity: <code>4 / 2</code>
- Station 2 material/intermediate/output capacity: <code>4 / 4 / 2</code>
- Inspection input/pass/scrap queue capacity: 각각 <code>3</code>
- CompletedProducts와 ScrapDisposal: 무제한 terminal store

Destination buffer는 task가 worker queue에 확정될 때 inbound slot을 예약합니다. Queue 점유량과
inbound reservation의 합은 capacity를 넘을 수 없습니다. 가공 완료 후 output buffer가 가득 차면
item은 machine 안에 남고 해당 machine은 공간이 확보될 때까지 다음 cycle을 시작하지 않습니다.
Material 보충량은 고정 목표 재고가 아니라 미충족 machine 수요에서 queue와 inbound 재고를 뺀
값으로 계산합니다.

Inspection은 세 개의 독립 task로 실행됩니다.

~~~text
LOAD_UNLOAD_TRANSFER_INTERFACE (desk load)
-> INSPECT_PRODUCT
-> LOAD_UNLOAD_TRANSFER_INTERFACE (desk unload)
~~~

Battery item 교환이나 전달은 사용하지 않습니다. Worker는 자신의 charging dock로 이동해 직접
충전하며, dock tile에 도착한 뒤에만 SOC가 증가합니다.

### 목적함수

Throughput 모드는 매일 warehouse를 설정량까지 보충하고 지정한 일수까지 실행합니다.

~~~powershell
.\.venv\Scripts\python.exe main.py scenario=mfg_flow_shop scenario.objective.mode=maximize_throughput scenario.horizon.num_days=5
~~~

Makespan 모드는 최초 warehouse material만 처리하며 보충하지 않습니다. 초기 material lineage가
accepted product 또는 ScrapDisposal에 도착한 scrap으로 모두 종결되면 조기 종료합니다.

~~~powershell
.\.venv\Scripts\python.exe main.py scenario=mfg_flow_shop scenario.objective.mode=minimize_makespan
~~~

### 대표 정책

| Decision mode | Dispatch 시점 | 역할 |
| --- | --- | --- |
| <code>immediate_shared</code> | worker가 idle이면 즉시 | 모든 생산 역할 공유 |
| <code>immediate_dedicated_roles</code> | worker가 idle이면 즉시 | 실행 전 LPT 역할 고정 |
| <code>rolling_horizon_shared</code> | strict periodic window 경계 | 모든 생산 역할 공유 |
| <code>rolling_horizon_dedicated_roles</code> | strict periodic window 경계 | 실행 전 LPT 역할 고정 |

Rolling Horizon은 worker polling이 아니라 독립 SimPy coordinator가 정확한
<code>t = window, 2*window, ...</code> 시각에 미시작 queue를 회수하고 전체 재할당합니다. 실행 중
task는 선점하지 않으며 low-battery self-charge만 현재 task 다음에 긴급 삽입됩니다.

### Worker 3 정책 비교

ADP 실험에서는 동일한 `mfg_flow_shop`, 5일 throughput 목적, worker 3명과 held-out seed를 사용해
다음 여섯 정책을 비교합니다.

| Decision mode | 선택 방식 | 비교 역할 |
| --- | --- | --- |
| <code>random_feasible_dispatch</code> | WAIT와 충돌 없는 feasible task를 균등 무작위 선택 | 학습 없는 무작위 기준선 |
| <code>immediate_shared</code> | idle event마다 고정 priority로 즉시 선택 | 반응형 shared rule 기준선 |
| <code>immediate_dedicated_roles</code> | idle event마다 전담 역할 안에서 즉시 선택 | 반응형 역할 분리 기준선 |
| <code>rolling_horizon_shared</code> | strict periodic window마다 전체 재할당 | 주기형 shared 기준선 |
| <code>rolling_horizon_dedicated_roles</code> | strict periodic window마다 역할 제약 하에 재할당 | 주기형 역할 분리 기준선 |
| <code>simulation_based_adp</code> | 가치망과 beam search로 joint assignment 선택 | 학습 기반 정책 |

정책 비교 dashboard는 평균 제품 수, 표준편차, 95% CI, worker 실행비율과 seed별 paired
difference를 함께 표시합니다. ADP의 주 비교 대상은 `immediate_shared`이며, 같은 seed에서의 평균
차이가 양수이고 paired bootstrap 95% CI 하한도 0보다 클 때만 우위를 입증한 것으로 판정합니다.
이 결과는 해당 환경·worker 수·horizon에 대한 실험 근거이며 모든 제조 환경에 대한 일반적 우위를
뜻하지 않습니다.

## Task와 Primitive Timing

HumanoidSim은 task와 primitive의 구조를 소유하고, ManSim은 시나리오별 시간분포를 소유합니다.

~~~text
configs/task_primitive_timing/factory_mfg_basic.yaml
configs/task_primitive_timing/mfg_flow_shop.yaml
configs/task_primitive_timing/shipyard_basic.yaml
~~~

각 task의 primitive occurrence는 exact <code>step_path</code>로 정의합니다. Primitive와 machine
processing time은 triangular distribution에서 전용 RNG stream으로 sample합니다.
<code>NAVIGATE_TO</code>는 이동을 시작할 때 tile당 시간을 한 번 sample하고 실제 edge 수와 적재
multiplier를 곱합니다. Timing profile 누락, 중복, call code 불일치와 잘못된 분포는 실행 전에
오류로 처리됩니다.

## Artifact와 Dashboard

주요 출력은 Hydra run directory에 생성됩니다.

| 파일 | 용도 |
| --- | --- |
| <code>results_dashboard.html</code> | 공통 Results Hub |
| <code>kpi.json</code>, <code>kpi_dashboard.html</code> | KPI 원본과 상세 화면 |
| <code>gantt.html</code>, <code>gantt_segments.csv</code> | task 및 상태 timeline |
| <code>daily_summary.json</code> | 일별 생산 집계 |
| <code>run_meta.json</code> | scenario, policy, seed와 fingerprint |
| <code>pre_run_diagnostics.json/html</code> | policy-independent 사전 지표 |
| <code>replay_studio_layout.json</code> | compact map layout |

정책 비교 실험은 디스크 사용량을 줄이기 위해 <code>events.jsonl</code>과 대용량 Replay artifact를
기본 저장하지 않습니다. KPI와 Gantt는 simulation 중 memory event로 생성되므로 정책 결정과
성능 수치에는 영향이 없습니다. 이벤트 단위 감사와 visual replay가 필요한 run만 다음을
명시합니다.

~~~powershell
runtime.artifacts.export_events=true runtime.ui.export_replay_artifacts=true
~~~

## 검증

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe scripts/audit_kpi.py outputs/my_run
.\.venv\Scripts\python.exe scripts/audit_run_artifacts.py outputs/my_run
~~~

<code>mfg_flow_shop</code> 감사에서는 buffer overflow, reservation failure/leak,
<code>occupancy + inbound reservations <= capacity</code>, blocked-after-service, 역할 위반,
inspection lifecycle와 이동 연속성을 확인합니다.

## 문서

- [Documentation Index](docs/README.md)
- [Installation Guide](docs/installation.md)
- [Simulator Core Guide](docs/simulator_core_guide.md)
- [Decision Logic](docs/decision_logic.md)
- [Scenario Timing](docs/scenario_timing.md)
- [Humanoid Worker Model](docs/humanoid_worker_model.md)
- [Movement Model](docs/humanoid_movement_model.md)
- [Replay and Dashboards](docs/replay_dashboards.md)
- [Factory Policy Comparison](experiments/factory_policy_comparison/README.md)

## Simulation-Based ADP (Optional)

<code>simulation_based_adp</code>는 <code>mfg_flow_shop / maximize_throughput / worker 3</code> 전용
event-driven joint assignment 정책입니다.

표준 profile은 초기 Random Feasible 20 episode와 10회의 policy update별 100 episode를 사용해
총 1,020 training episode를 생성합니다. WAIT는 일반 action으로 포함되며, mandatory charging을
먼저 처리한 뒤 각 worker가 WAIT 또는 conflict-free feasible task를 선택합니다. 전원 WAIT
afterstate에는 다음 1분 재검토까지의 시간 경과가 반영됩니다.

학습 target은 다음 decision epoch까지 증가한 completed product 수의 complete Monte Carlo
return입니다. Potential shaping, TD, bootstrapping과 n-step target은 사용하지 않고
post-decision value network를 MSE로 학습합니다. Raw transition은 episode 종료 직후 CPU compact
tensor로 바뀌고 해당 100-episode update가 끝나면 폐기됩니다.

Rollout은 CPU process 20개가 wave당 20 episode를 생성합니다. Main process만 <code>cuda:0</code>에서
network를 업데이트합니다. 초기 random 1 wave, policy rollout 50 wave, checkpoint 0~10
diagnostic 11 wave와 final selection 3 wave를 실행합니다.

~~~powershell
.\.venv\Scripts\python.exe -m manufacturing_sim.adp.train --config configs/adp/mfg_flow_shop_throughput_10x100.yaml
~~~

결과는 <code>outputs/adp_training/&lt;timestamp&gt;/</code>에 저장됩니다.

- <code>best.pt</code>, <code>last.pt</code>, <code>checkpoints/iteration_*.pt</code>
- <code>episode_metrics.csv</code>, <code>wave_metrics.csv</code>, <code>iteration_metrics.csv</code>
- <code>checkpoint_selection.csv</code>, <code>training_summary.json</code>
- <code>fixed_action_probe.json</code>
- <code>training_dashboard.html</code>

학습 dashboard는 checkpoint별 training/validation 생산량, post-decision value MSE/MAE/RMSE,
WAIT 행동, 병렬 rollout 시간과 메모리를 표시합니다. 행동 선택 품질은 고정 반사실 MC probe의
행동가치 순위 상관계수, 후보집합 Top-1 일치율과 선택 행동 regret으로 진단합니다. OOD 선택률과
OOD 과대평가 초과값은 현재 greedy 행동이 직전 on-policy batch의 지지영역을 벗어나는지 보여줍니다.
Probe와 OOD 값은 경험적 진단이며 전역 최적해를 뜻하지 않습니다. 모든 그래프 하단에는 한국어
해석을 함께 표시합니다.

Checkpoint fingerprint에는 scenario, objective, worker 범위, timing profile, feature schema,
설비 ID와 수, processing distribution, finite-buffer capacity, inspection capacity와 지도 구조가
포함됩니다. 하나라도 현재 환경과 다르면 fallback 없이 오류로 종료합니다.

~~~powershell
.\.venv\Scripts\python.exe main.py scenario=mfg_flow_shop decision=simulation_based_adp decision.adp.checkpoint_path=C:/path/to/best.pt
~~~

최종 비교는 학습과 checkpoint 선택에 사용하지 않은 seed <code>50001~50005</code>에서 Random
Feasible, 네 rule-based 정책과 ADP를 총 30회 실행합니다.

~~~powershell
.\.venv\Scripts\python.exe experiments/factory_policy_comparison/run_experiment.py --config experiments/factory_policy_comparison/config_mfg_flow_shop_worker3_adp.yaml --adp-checkpoint C:/path/to/best.pt
~~~

비교 dashboard는 평균, 표준편차, 95% CI, paired bootstrap CI, win/tie/loss와
<code>ADP - Immediate Shared</code> 판정을 표시합니다.
