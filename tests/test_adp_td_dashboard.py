import copy
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

import pytest

from manufacturing_sim.adp.td_dashboard import paired_production_changes, render_td_dashboard, _interval, cell
from manufacturing_sim.adp.train import _svg_chart
from manufacturing_sim.adp.td_formulas import FORMULAS, render_formula


def fixture():
    episodes = []
    values = {0: [10, 20, 30], 5: [13, 19, 35]}
    for iteration, products in values.items():
        for seed, value in enumerate(products, 100):
            episodes.append({"phase": "screening_validation", "worker_count": 3, "seed": seed,
                             "iteration": iteration, "products": value, "termination_reason": "completed_horizon"})
    iterations = [{"iteration": 0, "validation_products_mean": 20, "validation_episode_count": 3, "validation_products_std": 10},
                  {"iteration": 5, "validation_products_mean": 67 / 3, "validation_episode_count": 3, "validation_products_std": 11.37248}]
    return episodes, iterations


def test_paired_change_uses_seed_matches_not_subtracted_independent_intervals():
    episodes, iterations = fixture()
    episodes.reverse()
    rows, warnings = paired_production_changes(episodes, iterations)
    last = rows[-1]
    assert not warnings
    assert last["paired_mean"] == pytest.approx(7 / 3)
    assert last["paired_std"] == pytest.approx(math.sqrt(28 / 3))
    assert last["ci_low"] == pytest.approx(7 / 3 - 1.96 * math.sqrt(28 / 9))
    assert [last[key] for key in ("win", "tie", "loss")] == [2, 0, 1]
    assert rows[0]["paired_mean"] == 0


@pytest.mark.parametrize("fault", ["seed", "worker", "duplicate", "missing", "mean", "early_end"])
def test_pairing_refuses_inconsistent_or_incomplete_checkpoint(fault):
    episodes, iterations = fixture()
    if fault == "seed":
        episodes[-1]["seed"] = 999
    elif fault == "worker":
        episodes[-1]["worker_count"] = 4
    elif fault == "duplicate":
        episodes.append(copy.deepcopy(episodes[-1]))
    elif fault == "missing":
        episodes.pop()
    elif fault == "mean":
        iterations[-1]["validation_products_mean"] += 1
    else:
        episodes[-1]["termination_reason"] = "incomplete"
    rows, warnings = paired_production_changes(episodes, iterations)
    assert [row["iteration"] for row in rows] == [0]
    assert warnings


def test_pairing_excludes_final_test_and_live_partial_validation():
    episodes, iterations = fixture()
    episodes.extend([{**episodes[0], "phase": phase, "products": 900} for phase in ("initial_random", "final_selection_validation", "held_out_test")])
    episodes.append({**episodes[0], "iteration": 10, "products": 900})
    rows, warnings = paired_production_changes(episodes, iterations)
    assert [row["iteration"] for row in rows] == [0, 5]
    assert not warnings


def test_single_seed_has_no_ci_and_missing_baseline_is_not_fabricated():
    episodes = [{"phase": "screening_validation", "iteration": 0, "seed": 9, "worker_count": 3, "products": 10}]
    rows, _ = paired_production_changes(episodes, [{"iteration": 0, "validation_products_mean": 10, "validation_episode_count": 1}])
    assert rows[0]["paired_std"] is None
    assert rows[0]["ci_low"] is None
    assert _interval(10, 0, 1) == (None, None)
    assert paired_production_changes(episodes, [{"iteration": 5, "validation_products_mean": 10}])[0] == []


def test_paired_ci_does_not_pretend_same_seed_across_fleets_is_independent():
    episodes = [{"phase": "screening_validation", "iteration": 0, "seed": 9, "worker_count": w, "products": 10}
                for w in (3, 5)]
    rows, warnings = paired_production_changes(episodes, [{"iteration": 0, "validation_products_mean": 10, "validation_episode_count": 2}])
    assert not rows and warnings


def test_optional_ci_skips_only_the_single_seed_point():
    graph = _svg_chart([("metric", [10, 12], "red")], x_values=[0, 5], x_label="I", y_label="P",
                       error_ranges={"metric": ([None, 11], [None, 13])})
    assert graph.count("stroke-width='1.4'") == 3
    assert "None" not in graph


def test_dashboard_has_four_core_panels_and_detailed_explanations(tmp_path: Path):
    episodes, iterations = fixture()
    iterations[-1]["td_span_min_mean"] = 12.0
    snapshot = copy.deepcopy((episodes, iterations))
    page = render_td_dashboard(tmp_path, episodes, iterations, [], {"n_step": 30, "wait_action_enabled": False}).read_text(encoding="utf-8")
    assert (episodes, iterations) == snapshot
    assert page.count("data-metric-id=") == 8
    assert page.count("<strong>해석</strong>") == 8
    assert page.count("<strong>산출식</strong>") == 8
    assert page.count("<strong>유의사항</strong>") == 8
    assert page.split("<details id='learning-details'>")[0].count("data-metric-id=") == 4
    assert "<details id='learning-details' open" not in page
    assert "sessionStorage" in page and "embedded" in page
    derived = json.loads(re.search(r"id='adp-derived-metrics' type='application/json'>(.*?)</script>", page).group(1))
    assert derived["paired_production_changes"][-1]["paired_n"] == 3
    assert "Simulation 분" in page
    for removed in ("학습률 Schedule</h3>", "후보 가치 엔트로피</h3>", "자발적 WAIT 선택 수</h3>", "Replay 크기</h3>"):
        assert removed not in page


def test_unknown_values_are_na_and_invalidity_warning_survives(tmp_path: Path):
    (tmp_path / "result_validity.json").write_text(json.dumps({"status": "requires_rerun", "message": "<unsafe>"}))
    row = {"iteration": 0, "td_train_mse": float("nan"), "learning_rate": float("inf"), "voluntary_wait_count": 2}
    page = render_td_dashboard(tmp_path, [], [row], [], {"wait_action_enabled": False}).read_text(encoding="utf-8")
    assert "N/A" in page and "NaN" not in page and "Infinity" not in page
    assert "&lt;unsafe&gt;" in page
    assert "WAIT 비활성 계약" in page


@pytest.mark.parametrize("value", [5e-5, 2e-5, 1e-5, -1e-7])
def test_small_nonzero_metrics_are_not_displayed_as_zero(value):
    assert float(cell(value)) == pytest.approx(value)
    assert cell(value) != "0.000"


def test_random_batch_entropy_is_unavailable_and_legacy_idle_is_flagged(tmp_path):
    episodes = [{"phase": "initial_random", "iteration": 0,
                 "beam_value_entropy_avg": 0., "beam_value_entropy_decision_count": 0}]
    iterations = [{"iteration": 0, "beam_entropy": 0.}]
    page = render_td_dashboard(tmp_path, episodes, iterations, [], {"wait_action_enabled": False}).read_text(encoding="utf-8")
    assert "원본 로그 없이 복원할 수 없습니다" in page
    assert "유효 decision이 없는 초기 random batch 등은 N/A" in page
    assert "<td>N/A</td>" in page
    assert iterations[0]["beam_entropy"] == 0.


@pytest.mark.parametrize("key", list(FORMULAS))
def test_offline_equations_have_well_formed_accessible_mathml(key):
    page = render_formula(key)
    root = ET.fromstring(page)
    math_ns = "{http://www.w3.org/1998/Math/MathML}"
    equations = root.findall(f".//{math_ns}math")
    assert len(equations) == len(FORMULAS[key][0])
    for equation in equations:
        assert equation.attrib["display"] == "block"
        assert equation.attrib["aria-label"]
        for node in equation.iter():
            if node.tag in {math_ns + tag for tag in ("mfrac", "msub", "msup", "mover", "munder")}:
                assert len(node) == 2
            if node.tag in {math_ns + tag for tag in ("msubsup", "munderover")}:
                assert len(node) == 3
    assert not root.findall(".//script")
    assert "&lt;mfrac" not in page


def test_production_formulas_use_subscripts_sample_sd_and_separate_ci():
    page = render_formula("production")
    ns = {"m": "http://www.w3.org/1998/Math/MathML"}
    equations = ET.fromstring(page).findall(".//m:math", ns)
    assert len(equations) == 3
    variance = equations[1].find(".//m:msqrt/m:mfrac", ns)
    assert "".join(variance[1].itertext()).strip() == "Nk−1"
    assert equations[1].find(".//m:msup/m:mn", ns).text == "2"
    assert "1.96" in "".join(equations[2].itertext())
    assert len(equations[0].findall(".//m:msub", ns)) >= 3


def test_display_only_refresh_does_not_overwrite_trainer_page(tmp_path):
    episodes, iterations = fixture()
    original = tmp_path / "training_dashboard.html"
    original.write_text("trainer-owned", encoding="utf-8")
    page = render_td_dashboard(tmp_path, episodes, iterations, [], {"n_step": 30},
                               filename="training_dashboard_latest.html")
    assert page.name == "training_dashboard_latest.html"
    assert original.read_text(encoding="utf-8") == "trainer-owned"
    content = page.read_text(encoding="utf-8")
    assert content.count("<math ") >= 20
    assert "MathJax" not in content and "cdn.jsdelivr" not in content
    assert "overflow-x:auto" in content
