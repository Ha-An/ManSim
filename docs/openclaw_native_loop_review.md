# OpenClaw Native Loop 검토 메모

이 문서는 ManSim v0.3의 OpenClaw native-local 경로를 운영 관점에서 정리한 메모입니다. 목표는 현재 경로가 무엇을 전제로 하고, 어디까지 안정화됐고, 어떤 리스크가 남아 있는지 빠르게 파악하는 것입니다.

## 현재 경로
- ManSim은 OpenClaw native-local 경로를 통해 manager agent를 호출합니다.
- 현재 기본 backend는 `vllm/vllm-openai:gemma4-cu130` runtime 위의 `Gemma 4 E4B IT`입니다.
- OpenClaw gateway가 그 위에서 workspace 기반 turn을 처리합니다.

## 현재 manager runtime 구조
- run 내부 manager chain
  - `MANAGER_BOTTLENECK_DETECTOR`
  - `MANAGER_DIAGNOSIS_EVALUATOR` (`optional`)
  - `MANAGER_DAILY_PLANNER`
- run 종료 후
  - `MANAGER_RUN_REFLECTOR`
- runtime agent id는 run/day scoped 형식으로 파생되어 세션 오염을 줄입니다.
- workspace alias는 template 단위로 고정하고, runtime session만 분리합니다.

## 현재 안정화 포인트
- health probe 실패만으로 즉시 종료하지 않습니다.
- readiness 확인과 gateway 재기동 복구 절차를 거친 뒤 실제 turn 실패로 판단합니다.
- manager별 workspace memory를 분리해 역할 오염을 줄입니다.
- prompt-facing memory는 압축 유지하고 raw reports/trace는 별도 보존합니다.
- run-series root의 `knowledge.md`를 manager group에 `KNOWLEDGE.md`로 주입해 cross-run prior를 전달합니다.

## 현재 확인된 장점
- local OpenClaw workflow 안에서 manager chain을 구조적으로 분리할 수 있습니다.
- workspace artifact가 남기 때문에 detector/evaluator/planner/reflector 판단을 사후 검토하기 쉽습니다.
- run-level reflection과 series artifact를 결합해 cross-run learning loop를 실험할 수 있습니다.

## 현재 남은 과제
- detector bottleneck naming drift를 줄이는 것
- planner queue target grounding 품질을 높이는 것
- manager reasoning이 실제 `manager_queue` 소비로 얼마나 이어지는지 더 안정적으로 검증하는 것
- Reflector lesson이 다음 run 행동 변수에 더 직접 연결되도록 만드는 것

## 운영상 유의점
- OpenClaw gateway와 backend readiness는 별개로 봐야 합니다.
- prompt를 안정화하기 위해 출력 계약과 workspace template을 같이 관리해야 합니다.
- series 실험에서는 코드보다 `knowledge.md`가 run 간 prior를 전달하므로, artifact 품질이 실험 품질에 직접 영향을 줍니다.
