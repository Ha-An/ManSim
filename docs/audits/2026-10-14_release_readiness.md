# 2026-10-14 코드 및 업로드 준비 점검

## 범위

Python 코드, ADP 학습 및 비교 산출물, 감사 도구, 공개 문서와 Replay frontend를 점검했습니다.
기존 작업 트리의 변경사항과 실행 중인 비교 실험은 보존했습니다.
이번 점검에서는 학습 설정, 정책 선택 규칙과 시뮬레이션 상태 전이를 변경하지 않았습니다.
창고의 의도적인 교착 조건도 유지했습니다.
Commit과 push는 수행하지 않았습니다.

## 수정 사항

- Background launcher의 초기 monitor/status 생성 실패 시 output lock이 남는 문제를 수정하고 회귀 테스트를 추가했습니다.
- 비교 대시보드에서 과거 timing metadata의 병렬 수 누락과 고정된 병렬 수 표시를 수정했습니다.
- Artifact 감사가 Replay 비활성 실행을 누락 오류로 처리하던 문제를 수정했습니다. 저장된 설정과 event metadata를 함께 확인하며, 잘못된 설정은 조용히 생략하지 않습니다.
- Runtime의 Replay 생성에는 저장된 events가 필요하다는 계약을 감사와 문서에 반영했습니다.
- 미정의 타입 annotation, 중복 dictionary key와 일부 미사용 import/지역 변수를 정리했습니다.
- 40-slot warehouse geometry에 맞게 생산량 기준 계산의 회귀 테스트 기대값을 갱신했습니다. 계산식 자체는 변경하지 않았습니다.
- README와 실험 안내를 현재 기본값 기준으로 정리하고 개발 검증 절차를 분리했습니다.
- Checkpoint, 임시 디렉터리와 테스트 캐시를 Git 제외 대상에 추가했습니다.

## 검증 결과

| 검사 | 결과 |
| --- | --- |
| 전체 Python 테스트 | 526 passed, 45 subtests passed |
| 위험 항목 정적 검사 | `E9,F63,F7,F82,F601` 통과 |
| Python 의존성 | `pip check` 통과 |
| HumanoidSim catalog | 87 tasks, 61 primitives 검증 통과 |
| Replay Studio 3D | Build, unit test 20개, desktop/mobile visual test 2개 통과 |
| Replay Studio 2D | Build 통과 |
| 학습 및 실시간 대시보드 | Desktop/mobile에서 JavaScript 오류와 가로 overflow 없음 |
| 완료된 worker 2~6 학습 5건 | 학습 산출물 감사 오류 0, 경고 0 |
| Manufacturing/Shipyard 1일 smoke | 3개 scenario KPI 및 artifact 감사 통과 |
| Event/Replay export 조합 | 회귀 테스트 및 실제 실행 산출물 검사 통과 |

실행 결과와 화면 검증 자료는 로컬 `outputs/release_review_20261014/`에 저장했습니다.
전체 테스트는 `pytest tests -q`로 실행했으며 `unittest`에서 발견하지 못하는 함수형 테스트도 포함했습니다.
정적 검사 통과는 전체 스타일 규칙 적용을 의미하지 않습니다.

## 남은 경고와 한계

- Shipyard smoke의 artifact 경고 3건은 고정 horizon 끝에서 진행 중이던 task, primitive와 이동 기록에 관한 것입니다. KPI 및 artifact 오류는 없었습니다.
- 3D build의 큰 JavaScript chunk 경고는 남아 있습니다. 기능 오류는 아니며 bundle 분할은 별도 개선 항목입니다.
- Events를 저장하지 않은 결과에 대해 이벤트 단위 이동 연속성을 검증했다고 주장하지 않습니다.
- 실행 중인 전체 비교 실험의 완료 후 감사는 이번 완료 학습 및 smoke 검증과 구분합니다.
- 테스트 통과는 모든 seed에서 오류가 없다는 보장이 아니며, 이번 점검에서 ADP를 재학습하지 않았습니다.
- 기존 모듈 경로는 실행 명령과 저장된 계획의 호환성을 위해 유지했습니다.
- 로컬 전용 자료와 실험 결과는 업로드 대상에서 제외하며, 실제 업로드 직전에 staged 목록을 다시 확인해야 합니다.
