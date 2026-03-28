# OpenClaw Native Loop 검토 메모

## 현재 경로
- ManSim은 OpenClaw native-local 경로를 통해 manager agent를 호출합니다.
- 백엔드는 Ollama를 사용하고, OpenClaw gateway가 그 위에서 워크스페이스 기반 turn을 처리합니다.

## 현재 manager 구조
- `MANAGER_BOTTLENECK_DETECTOR_<RUN>_D<day>`
- `MANAGER_DAILY_PLANNER_<RUN>_D<day>`
- day 단위 runtime agent id를 사용해 세션 오염을 줄입니다.
- workspace alias는 각각 `MANAGER_BOTTLENECK_DETECTOR`, `MANAGER_DAILY_PLANNER`로 분리합니다.

## 안정화 포인트
- health probe 실패만으로 즉시 종료하지 않습니다.
- readiness 확인과 gateway 재기동 복구 절차를 거친 뒤 실제 turn 실패로 판단합니다.
- detector와 planner의 memory를 분리해 역할 오염을 줄입니다.

## 현재 남은 과제
- detector의 day-to-day 재랭킹 안정성 향상
- planner의 detector override 사례 확보
- planner queue의 실제 실행 영향력 추가 검증
