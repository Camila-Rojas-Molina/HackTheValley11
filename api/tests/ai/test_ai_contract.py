"""Checker, template and reading grade (C4 "Output and the checker", "Patient explanation")."""
from __future__ import annotations

import random

import pytest

from app.ai import contract
from app.ai.contract import check, numbers_in, reading_grade
from app.ai.facts import build_facts
from app.ai.template import TemplateMissing, template
from app.schemas import GeneratedText
from tests.ai.conftest import EXPLANATION_FIXTURE, load_fixture

TASKS = ("care_team_summary", "patient_explanation")


def gt(headline, detail="Lesion 3.24 cm³."):
    return GeneratedText(headline=headline, detail=detail, numbers_used=[])


# ----------------------------------------------------------------------------------------- template
@pytest.mark.parametrize("task", TASKS)
def test_template_of_facts_fixture_passes_check_and_has_no_blocked_wording(task, facts):
    text = template(task, facts)
    verdict = check(text, facts)
    assert verdict.ok, verdict.reason
    assert verdict.numbers_matched == verdict.numbers_total > 0
    assert contract.blocked_wording(f"{text.headline} {text.detail}") == []


def _random_facts(rng: random.Random):
    day_a = rng.randint(0, 400)
    vol_a = rng.choice([0.0, rng.uniform(0, 80)])
    return build_facts(
        scan_id=f"case{rng.randint(0, 999)}-L{rng.randint(1, 5)}-d{day_a}", level=rng.choice(
            ["T11", "T12", "L1", "L2", "L3", "L4", "L5"]),
        day_a=day_a, day_b=day_a + rng.randint(0, 1200), lesion_volume_a_cm3=vol_a,
        lesion_volume_b_cm3=rng.choice([0.0, rng.uniform(0, 80), rng.uniform(0, 0.004)]),
        lesion_grid=rng.choice(["original", "crop_1mm"]), vertebra_volume_b_cm3=rng.uniform(5, 150),
        mask_source=rng.choice(["expert", "ai"]),
        provenance=rng.choice(["real_scan", "synthetic_insilico", "synthetic_metric_interp"]))


@pytest.mark.parametrize("task", TASKS)
def test_template_passes_check_for_any_facts(task):
    """Property: the template is correct by construction (seeded, 500 cases per task)."""
    rng = random.Random(20261017)
    for _ in range(500):
        f = _random_facts(rng)
        verdict = check(template(task, f), f)
        assert verdict.ok, (verdict.reason, f.model_dump())


@pytest.mark.parametrize("task", TASKS)
def test_facts_with_stiffness_never_get_the_pending_template(task, facts):
    """Once the physics fills a stiffness number, the lesion-only text ("pending") must not be used."""
    with_x = facts.model_copy(update={"stiffness_change_axial_pct": -21.4})
    if f"{task}_full" in contract.ai_labels()["templates"]:
        assert check(template(task, with_x), with_x).ok
    else:
        with pytest.raises(TemplateMissing):
            template(task, with_x)


# ----------------------------------------------------------------------------------------- checker
@pytest.mark.parametrize("headline,reason", [
    ("L1 changed over four months.", "spelled-out quantity"),
    ("L1 lost about 60% of its strength.", "number not in facts: 60"),
    ("Surgical fixation should be considered.", "blocked wording"),
    ("This indicates a positive response to therapy.", "blocked wording"),
    ("Fracture Risk Index rose.", "blocked wording"),
    ("The lesion volume doubled.", "spelled-out quantity"),
    ("The lesion is 3.24 cm³ on 2026-10-17.", "number not in facts"),
    ("Volume went from −3.24 cm³.", "number not in facts: −3.24"),
    ("L1 lesion went from 0 to 3.24 cm³. It grew 15mm.", "number not in facts: 15"),
    ("L1 lesion is 3.24 cm³ and the vertebra is 2x weaker.", "number not in facts: 2"),
    ("L1 height fell 4cm.", "number not in facts: 4"),
    ("L1 lost 30pct.", "number not in facts: 30"),
    ("L1 lesion is 273.5x larger.", "number not in facts: 273.5"),
])
def test_checker_rejects(facts, headline, reason):
    verdict = check(gt(headline), facts)
    assert not verdict.ok and reason in verdict.reason


def test_checker_rejects_over_length_and_length_finish(facts):
    long = gt("L1 lesion. " * 60)
    assert "over 600 characters" in check(long, facts).reason
    assert "cut off" in check(gt("L1 lesion 3.24 cm³."), facts, finish_reason="length").reason


def test_checker_order_length_before_numbers(facts):
    verdict = check(gt("Over four months, 999 risk. " * 30), facts)
    assert "over 600" in verdict.reason


def test_identifiers_and_units_are_not_numbers(facts):
    text = gt("T11–L5 ladder; L1 lesion 3.24 cm³ (3.24 cm3) on Day 273, M5 pending.",
              "That is 4.73% of the vertebra.")
    verdict = check(text, facts)
    assert verdict.ok, verdict.reason
    assert (verdict.numbers_matched, verdict.numbers_total) == (3, 3)


def test_fall_and_fell_are_allowed(facts):
    assert check(gt("L1 lesion volume rose to 3.24 cm³.", "Nothing fell outside the computed values."),
                 facts).ok


def test_numbers_in():
    assert numbers_in("L1 0.00 cm³, Day 0, 3.24 cm3, 4.73%, T11, −2.5 and 273.") == [0.0, 3.24, 4.73, -2.5, 273.0]


def test_word_lists_are_read_from_labels_not_copied():
    ai = contract.ai_labels()
    assert len(contract._blocked()) == len(ai["blocked_output_patterns"])
    assert contract._number_words().search("Twice")
    assert contract.labels() is contract.labels()  # loaded once


# ----------------------------------------------------------------------------------------- reading grade
def test_reading_grade_reproduces_the_fixture():
    golden = load_fixture(EXPLANATION_FIXTURE)
    text = f"{golden['text']['headline']} {golden['text']['detail']}"
    assert reading_grade(text) == golden["reading_grade"] == 2.0


def test_reading_grade_rules():
    assert reading_grade("") == 0.0
    # 1 sentence, 4 words, 4 syllables ("the" 1, "cat" 1, "sat" 1, "here" 1: final e removed only above 1)
    assert reading_grade("The cat sat here.") == round(0.39 * 4 + 11.8 * 1 - 15.59, 1)
    # digit tokens are not words; "3.24." does not end a sentence early
    assert reading_grade("It is 3.24 cm. It is L1.") == reading_grade("It is cm. It is.")
