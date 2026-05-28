# LLM Prompt Design

ManSim의 LLM manager prompt stack은 `openclaw_adaptive_priority`를 기준으로 설계되어 있습니다. 현재 root 기본 simulation path인 `rolling_horizon_dedicated_roles`와 scripted baseline인 `adaptive_priority`는 LLM prompt를 사용하지 않습니다.

## Design Principle

Prompt는 manager가 해야 할 판단만 담고, 실행 가능한 저수준 policy는 deterministic compiler가 만듭니다. 이렇게 해야 prompt가 커지지 않고, LLM output drift가 simulator execution을 직접 흔들지 않습니다.

## Strategist

Strategist는 intent-only manager입니다.

해야 할 일:

- 하루 운영 focus 결정.
- Worker role과 support intent 제안.
- Prevention target 선택.
- Daily target 작성.
- 전날 reviewer correction과 wiki/graph lesson을 참고.

하지 말아야 할 일:

- task instance 직접 배정.
- priority map 직접 작성.
- worker commitment 생성.
- raw metric을 길게 반복.

## Reviewer

Reviewer는 diagnosis-only manager입니다.

해야 할 일:

- target miss 식별.
- top failure mode 요약.
- 다음 날 prevention target과 support pair 추천.
- carry-forward risk 작성.

하지 말아야 할 일:

- 다음 날 executable policy 작성.
- Curator처럼 wiki page를 직접 편집.
- raw JSON을 prose로 반복.

## Curator

Curator는 wiki update intent manager입니다.

해야 할 일:

- 운영관리 lesson을 compact하게 정리.
- 조치와 결과의 관계를 증거와 함께 기록.
- positive/negative tradeoff를 같이 남김.
- Obsidian wikilink target을 제안.

하지 말아야 할 일:

- raw data를 wiki에 그대로 복사.
- 현재 run 하나만 보고 확정적인 causal law 선언.
- Markdown 파일을 직접 작성.

## Knowledge Injection

Strategist와 Reviewer는 config에서 활성화된 경우에만 compact knowledge를 받습니다.

- `llm_wiki_digest`
- `knowledge_graph_digest`

두 digest는 prompt를 비대하게 만들지 않는 선에서 잘립니다. 현재 simulation state가 wiki/graph와 충돌하면 현재 state를 우선합니다.

## Compiler Boundary

Compiler는 LLM agent가 아닙니다. Strategist intent와 reviewer feedback을 아래 실행 정보로 변환합니다.

- task priority weight
- worker role multiplier
- support/focus rule
- safety floor
- closeout/battery/reliability guard

이 boundary가 `openclaw_adaptive_priority`의 핵심 안정화 장치입니다.
