"""Offline MathML explanations for the TD dashboard; no metric calculations."""
from __future__ import annotations

import html


# Authored MathML, not user input. Each equation is independently scrollable.
FORMULAS: dict[str, tuple[list[tuple[str, str]], str]] = {
    "production": ([
        ("평균 생산량", """
          <msub><mover><mi>P</mi><mo>¯</mo></mover><mi>k</mi></msub><mo>=</mo>
          <mfrac><mn>1</mn><msub><mi>N</mi><mi>k</mi></msub></mfrac>
          <munderover><mo>∑</mo><mrow><mi>e</mi><mo>=</mo><mn>1</mn></mrow><msub><mi>N</mi><mi>k</mi></msub></munderover>
          <msub><mi>P</mi><mrow><mi>k</mi><mo>,</mo><mi>e</mi></mrow></msub>"""),
        ("표본 표준편차", """
          <msub><mi>s</mi><mi>k</mi></msub><mo>=</mo><msqrt><mfrac>
            <mrow><munderover><mo>∑</mo><mrow><mi>e</mi><mo>=</mo><mn>1</mn></mrow><msub><mi>N</mi><mi>k</mi></msub></munderover>
              <msup><mrow><mo>(</mo><msub><mi>P</mi><mrow><mi>k</mi><mo>,</mo><mi>e</mi></mrow></msub><mo>−</mo>
                <msub><mover><mi>P</mi><mo>¯</mo></mover><mi>k</mi></msub><mo>)</mo></mrow><mn>2</mn></msup></mrow>
            <mrow><msub><mi>N</mi><mi>k</mi></msub><mo>−</mo><mn>1</mn></mrow>
          </mfrac></msqrt>"""),
        ("근사 95% 신뢰구간", """
          <msub><mi mathvariant="normal">CI</mi><mi>k</mi></msub><mo>≈</mo>
          <msub><mover><mi>P</mi><mo>¯</mo></mover><mi>k</mi></msub><mo>±</mo><mn>1.96</mn><mo>·</mo>
          <mfrac><msub><mi>s</mi><mi>k</mi></msub><msqrt><msub><mi>N</mi><mi>k</mi></msub></msqrt></mfrac>"""),
    ], "k: iteration, e: episode, P: 해당 episode의 완료 제품 수, N: 해당 iteration의 episode 수. 표준편차와 신뢰구간은 N ≥ 2에서 계산합니다."),
    "paired-gain": ([
        ("동일 seed 차이", """
          <msub><mi>d</mi><mrow><mi>k</mi><mo>,</mo><mi>e</mi></mrow></msub><mo>=</mo>
          <msub><mi>P</mi><mrow><mi>k</mi><mo>,</mo><mi>e</mi></mrow></msub><mo>−</mo>
          <msub><mi>P</mi><mrow><mn>0</mn><mo>,</mo><mi>e</mi></mrow></msub>"""),
        ("평균 개선량", """
          <msub><mi>Δ</mi><mi>k</mi></msub><mo>=</mo><mfrac><mn>1</mn><mi>N</mi></mfrac>
          <munderover><mo>∑</mo><mrow><mi>e</mi><mo>=</mo><mn>1</mn></mrow><mi>N</mi></munderover>
          <msub><mi>d</mi><mrow><mi>k</mi><mo>,</mo><mi>e</mi></mrow></msub>"""),
        ("근사 95% 신뢰구간", """
          <msub><mi mathvariant="normal">CI</mi><msub><mi>Δ</mi><mi>k</mi></msub></msub><mo>≈</mo>
          <msub><mi>Δ</mi><mi>k</mi></msub><mo>±</mo><mn>1.96</mn><mo>·</mo>
          <mfrac><msub><mi>s</mi><mrow><mi>d</mi><mo>,</mo><mi>k</mi></mrow></msub><msqrt><mi>N</mi></msqrt></mfrac>"""),
    ], "N: 동일 worker·seed의 짝 수. s(d,k): seed별 생산량 차이의 표본 표준편차. Win/tie/loss는 차이가 양수/0/음수인 seed 수입니다."),
    "td-fit": ([
        ("n-step TD target", """
          <msub><mi>Y</mi><mi>j</mi></msub><mo>=</mo>
          <munderover><mo>∑</mo><mrow><mi>ℓ</mi><mo>=</mo><mn>0</mn></mrow><mrow><msub><mi>h</mi><mi>j</mi></msub><mo>−</mo><mn>1</mn></mrow></munderover>
          <msub><mi>R</mi><mrow><mi>j</mi><mo>+</mo><mi>ℓ</mi></mrow></msub><mo>+</mo>
          <msub><mn>1</mn><mtext>비종료</mtext></msub><mo>·</mo><msub><mi>V</mi><mtext>target</mtext></msub>
          <mo>(</mo><msubsup><mi>S</mi><mrow><mi>j</mi><mo>+</mo><msub><mi>h</mi><mi>j</mi></msub></mrow><mrow><mi>x</mi><mo>,</mo><mtext>greedy</mtext></mrow></msubsup><mo>)</mo>"""),
        ("TD MSE", """
          <mi mathvariant="normal">MSE</mi><mo>=</mo><mfrac><mn>1</mn><mi>J</mi></mfrac>
          <munderover><mo>∑</mo><mrow><mi>j</mi><mo>=</mo><mn>1</mn></mrow><mi>J</mi></munderover>
          <msup><mrow><mo>[</mo><msub><mi>V</mi><mi>θ</mi></msub><mo>(</mo><msubsup><mi>S</mi><mi>j</mi><mi>x</mi></msubsup><mo>)</mo><mo>−</mo><msub><mi>Y</mi><mi>j</mi></msub><mo>]</mo></mrow><mn>2</mn></msup>"""),
        ("Target soft update", """
          <msub><mi>θ</mi><mtext>target</mtext></msub><mo>←</mo><mo>(</mo><mn>1</mn><mo>−</mo><mi>τ</mi><mo>)</mo>
          <msub><mi>θ</mi><mtext>target</mtext></msub><mo>+</mo><mi>τ</mi><mi>θ</mi>"""),
    ], "j: 학습 표본의 시작 decision, h: 실제 누적 step 수, J: 평가 표본 수, R: 완료 제품 보상. γ = 1이며 종료 시 bootstrap 항은 0입니다. Soft update는 SGD마다, 그래프 오차는 업데이트 종료 후 계산합니다."),
    "future-error": ([
        ("실제 잔여 생산량", """
          <msub><mi>G</mi><mi>j</mi></msub><mo>=</mo><munderover><mo>∑</mo><mrow><mi>u</mi><mo>=</mo><mi>j</mi></mrow><mrow><mi>T</mi><mo>−</mo><mn>1</mn></mrow></munderover><msub><mi>R</mi><mi>u</mi></msub>"""),
        ("예측 오차", """
          <msub><mi>e</mi><mi>j</mi></msub><mo>=</mo><msub><mi>V</mi><mi>θ</mi></msub><mo>(</mo><msubsup><mi>S</mi><mi>j</mi><mi>x</mi></msubsup><mo>)</mo><mo>−</mo><msub><mi>G</mi><mi>j</mi></msub>"""),
        ("RMSE", """
          <mi mathvariant="normal">RMSE</mi><mo>=</mo><msqrt><mfrac><mn>1</mn><mi>J</mi></mfrac>
          <munderover><mo>∑</mo><mrow><mi>j</mi><mo>=</mo><mn>1</mn></mrow><mi>J</mi></munderover><msubsup><mi>e</mi><mi>j</mi><mn>2</mn></msubsup></msqrt>"""),
        ("평균 편향", """
          <mi mathvariant="normal">Bias</mi><mo>=</mo><mfrac><mn>1</mn><mi>J</mi></mfrac>
          <munderover><mo>∑</mo><mrow><mi>j</mi><mo>=</mo><mn>1</mn></mrow><mi>J</mi></munderover><msub><mi>e</mi><mi>j</mi></msub>"""),
    ], "T: 해당 episode의 종료 decision. J: 모든 validation episode의 decision 표본 수. 평균 편향은 가치망 평균 예측에서 실제 MC 평균을 뺀 값입니다. 표의 두 평균도 같은 J개 표본으로 계산합니다."),
    "target-span": ([
        ("평균 누적 step", """
          <mover><mi>h</mi><mo>¯</mo></mover><mo>=</mo><mfrac><mn>1</mn><mi>J</mi></mfrac>
          <munderover><mo>∑</mo><mrow><mi>j</mi><mo>=</mo><mn>1</mn></mrow><mi>J</mi></munderover><msub><mi>h</mi><mi>j</mi></msub>"""),
        ("평균 시간 범위", """
          <mover><mrow><mi>Δ</mi><mi>t</mi></mrow><mo>¯</mo></mover><mo>=</mo><mfrac><mn>1</mn><mi>J</mi></mfrac>
          <munderover><mo>∑</mo><mrow><mi>j</mi><mo>=</mo><mn>1</mn></mrow><mi>J</mi></munderover>
          <mo>(</mo><msub><mi>t</mi><mrow><mi>j</mi><mo>+</mo><msub><mi>h</mi><mi>j</mi></msub></mrow></msub><mo>−</mo><msub><mi>t</mi><mi>j</mi></msub><mo>)</mo>"""),
        ("중단 사유 비율", """
          <msub><mi>r</mi><mi>c</mi></msub><mo>=</mo><mfrac><msub><mi>N</mi><mi>c</mi></msub><mi>J</mi></mfrac>"""),
    ], "h는 설정 n 상한까지 누적하되 행동 불일치 직전 또는 episode 종료에서 절단한 길이입니다. t: simulation 분, c: 중단 사유, N(c): 해당 사유 표본 수, J: 전체 표본 수."),
    "replay": ([
        ("업데이트 episode", """
          <msub><mi>N</mi><mtext>update</mtext></msub><mo>=</mo><msub><mi>N</mi><mtext>current</mtext></msub><mo>+</mo><msub><mi>N</mi><mtext>history</mtext></msub>"""),
        ("Replay 메모리", """
          <msub><mi>M</mi><mtext>MiB</mtext></msub><mo>=</mo><mfrac><msub><mi>B</mi><mtext>tensor + metadata</mtext></msub><msup><mn>2</mn><mn>20</mn></msup></mfrac>"""),
    ], "current: 현재 수집 episode, history: 선택된 과거 episode, B: tensor와 직렬화 기준 제약 metadata의 byte 수. 초기 학습은 fill 전체, 이후에는 현재 수집 전체와 과거 완전 episode를 사용합니다."),
    "ood": ([
        ("관측 표본까지 거리", """
          <mi>d</mi><mo>(</mo><mi>z</mi><mo>)</mo><mo>=</mo>
          <munder><mo>min</mo><mrow><mi>r</mi><mo>∈</mo><mi mathvariant="script">R</mi></mrow></munder>
          <mfrac><msub><mrow><mo>‖</mo><mi>z</mi><mo>−</mo><msub><mi>z</mi><mi>r</mi></msub><mo>‖</mo></mrow><mn>2</mn></msub><msqrt><mi>D</mi></msqrt></mfrac>"""),
        ("OOD 선택 비율", """
          <msub><mi>r</mi><mtext>OOD</mtext></msub><mo>=</mo><mfrac><msub><mi>N</mi><mrow><mi>d</mi><mo>&gt;</mo><mi>q</mi></mrow></msub><msub><mi>N</mi><mtext>evaluated</mtext></msub></mfrac>"""),
        ("관측 범위 대비 편향", """
          <mi>Δ</mi><mi mathvariant="normal">Bias</mi><mo>=</mo><msub><mover><mi>e</mi><mo>¯</mo></mover><mtext>OOD</mtext></msub><mo>−</mo><msub><mover><mi>e</mi><mo>¯</mo></mover><mtext>in-range</mtext></msub>"""),
    ], "z: 표준화 afterstate 요약벡터, D: 벡터 차원, R: 직전 batch의 reference 집합. q는 calibration 거리의 설정 분위수입니다. e = V − G, 각 평균은 OOD와 관측 범위 안의 표본을 구분해 계산합니다."),
    "runtime": ([
        ("Phase 처리량", """
          <mi mathvariant="normal">Episodes/hour</mi><mo>=</mo><mn>3600</mn><mo>·</mo>
          <mfrac><mrow><munder><mo>∑</mo><mi>w</mi></munder><msub><mi>N</mi><mi>w</mi></msub></mrow><mrow><munder><mo>∑</mo><mi>w</mi></munder><msub><mi>T</mi><mi>w</mi></msub></mrow></mfrac>"""),
        ("슬롯 활용률", """
          <mi>U</mi><mo>=</mo><mfrac>
            <mrow><munder><mo>∑</mo><mi>w</mi></munder><munder><mo>∑</mo><mrow><mi>e</mi><mo>∈</mo><mi>w</mi></mrow></munder><msub><mi>T</mi><mi>e</mi></msub></mrow>
            <mrow><munder><mo>∑</mo><mi>w</mi></munder><msub><mi>p</mi><mi>w</mi></msub><msub><mi>T</mi><mi>w</mi></msub></mrow></mfrac>"""),
    ], "w: 해당 phase의 완료 wave, N(w): episode 수, T(w): wave 시작부터 결과 수신까지의 실제 초, T(e): episode 실행 초, p(w): 실제 배정한 병렬 슬롯 수."),
    "entropy": ([
        ("가치 표준화", """
          <msub><mi>z</mi><mi>b</mi></msub><mo>=</mo><mfrac><mrow><msub><mi>v</mi><mi>b</mi></msub><mo>−</mo><mover><mi>v</mi><mo>¯</mo></mover></mrow><msub><mi>σ</mi><mi>v</mi></msub></mfrac>"""),
        ("정규화 후보 가중치", """
          <msub><mi>p</mi><mi>b</mi></msub><mo>=</mo><mfrac><mrow><mi mathvariant="normal">exp</mi><mo>(</mo><msub><mi>z</mi><mi>b</mi></msub><mo>)</mo></mrow>
          <mrow><munderover><mo>∑</mo><mrow><mi>c</mi><mo>=</mo><mn>1</mn></mrow><mi>B</mi></munderover><mi mathvariant="normal">exp</mi><mo>(</mo><msub><mi>z</mi><mi>c</mi></msub><mo>)</mo></mrow></mfrac>"""),
        ("정규화 엔트로피", """
          <mi>H</mi><mo>=</mo><mo>−</mo><mfrac>
          <mrow><munderover><mo>∑</mo><mrow><mi>b</mi><mo>=</mo><mn>1</mn></mrow><mi>B</mi></munderover><msub><mi>p</mi><mi>b</mi></msub><mi mathvariant="normal">log</mi><mo>(</mo><msub><mi>p</mi><mi>b</mi></msub><mo>)</mo></mrow>
          <mrow><mi mathvariant="normal">log</mi><mo>(</mo><mi>B</mi><mo>)</mo></mrow></mfrac>"""),
    ], "B: greedy beam 후보 수, v: 후보 가치. 후보가 하나면 N/A, 복수 후보의 가치가 모두 같으면 H = 1입니다. Episode별 유효 decision 평균을 구한 뒤 유효 episode만 평균합니다."),
    "early-stop": ([
        ("생산량 paired 차이", """
          <msub><mi>d</mi><mi>e</mi></msub><mo>=</mo><msub><mi>P</mi><mrow><mtext>current</mtext><mo>,</mo><mi>e</mi></mrow></msub><mo>−</mo><msub><mi>P</mi><mrow><mtext>best</mtext><mo>,</mo><mi>e</mi></mrow></msub>"""),
        ("근사 CI 상한", """
          <mi mathvariant="normal">Upper</mi><mo>=</mo><mover><mi>d</mi><mo>¯</mo></mover><mo>+</mo><mn>1.96</mn><mo>·</mo><mfrac><msub><mi>s</mi><mi>d</mi></msub><msqrt><mi>N</mi></msqrt></mfrac>"""),
    ], "동일 screening seed의 현재 − 기존 최고 생산량 차이를 사용합니다. N: seed 수, d의 윗줄: 차이 평균, s(d): 차이의 표본 표준편차. 종료에는 최소 iteration·미개선 기간·연속 CI 조건이 모두 필요합니다."),
}


def render_formula(key: str, fallback: str = "", *, heading: str = "산출식") -> str:
    if key not in FORMULAS:
        return f"<p class='formula'><strong>{html.escape(heading)}</strong> {html.escape(fallback)}</p>"
    equations, legend = FORMULAS[key]
    rows = "".join(
        f"<div class='equation-row'><span class='equation-label'>{html.escape(label)}</span>"
        f"<div class='equation-scroll' tabindex='0' role='group' aria-label='{html.escape(label, quote=True)}'>"
        f"<math xmlns='http://www.w3.org/1998/Math/MathML' display='block' aria-label='{html.escape(label, quote=True)}'>"
        f"<mrow>{expression}</mrow></math></div></div>" for label, expression in equations
    )
    return (f"<div class='formula' data-formula='{html.escape(key, quote=True)}'><strong>{html.escape(heading)}</strong>"
            f"{rows}<p class='formula-legend'>{html.escape(legend)}</p></div>")


FORMULA_CSS = """
.formula{background:#eaf0f4;padding:12px 14px;margin:12px 0;min-width:0}
.equation-row{display:grid;grid-template-columns:130px minmax(0,1fr);align-items:center;gap:8px;margin:12px 0}
.equation-label{font-size:13px;color:#435c6b;overflow-wrap:anywhere}
.equation-scroll{min-width:0;max-width:100%;overflow-x:auto;padding:8px 2px}
.equation-scroll:focus-visible{outline:2px solid #0969a2;outline-offset:2px}
.equation-scroll math{font-family:'Cambria Math','STIX Two Math',math;font-size:20px;margin:0;width:max-content;max-width:none;text-align:left}
.panel .formula-legend,.formula .formula-legend{font-size:13px;color:#435c6b;line-height:1.7;margin:12px 0 0}
@media(max-width:600px){.equation-row{grid-template-columns:minmax(0,1fr);gap:2px}.formula{padding:12px}}
"""
