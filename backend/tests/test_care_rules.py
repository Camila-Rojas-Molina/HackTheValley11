"""Senior-mode rule engine and synthetic generator (pure; no database)."""
from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app import care

END = date(2026, 10, 16)
CONTRACT_KEYS = {"resident_id", "display_name", "synthetic", "score", "level", "rules", "suggested_check",
                 "briefing"}
RULE_KEYS = {"rule", "observed", "baseline", "unit", "contribution", "explanation"}


def scored(seed: int = care.DEFAULT_SEED):
    cohort = care.generate_cohort(seed, END)
    return list(zip(cohort, care.score_cohort(cohort, END)))


def fired(card) -> set[str]:
    return {r["rule"] for r in card["rules"] if r["contribution"] > 0}


def rule(card, name) -> dict:
    return next(r for r in card["rules"] if r["rule"] == name)


def day(d: date, **kw) -> care.DayMetrics:
    base = dict(day=d, wear_hours=14.0, sway_rms=0.06, stand_ups=12, settle_s=20.0, settle_sway_s=16.0,
                settle_hr_s=20.0, night_restless_min=30.0, night_wear_min=410.0)
    base.update(kw)
    return care.DayMetrics(**base)


def flat_days(**recent_kw) -> list[care.DayMetrics]:
    """14 identical days; the last two take recent_kw."""
    out = []
    for i in range(care.RECORD_DAYS):
        d = END - timedelta(days=care.RECORD_DAYS - 1 - i)
        out.append(day(d, **(recent_kw if i >= care.RECORD_DAYS - 2 else {})))
    return out


# --- the synthetic cohort ------------------------------------------------------------------------
def test_cohort_is_20_residents_by_14_days_all_synthetic():
    cohort = care.generate_cohort(care.DEFAULT_SEED, END)
    assert len(cohort) == 20 and len({r.id for r in cohort}) == 20
    for r in cohort:
        assert len(r.days) == 14 and r.days[-1].day == END and r.days[0].day == END - timedelta(days=13)
        assert all(d.synthetic for d in r.days)
        assert r.display_name.startswith("Synthetic resident ")
    names = [r.script.name for r in cohort]
    assert names.count("stable") >= 9
    for counter in ("one_off_bad_night", "one_off_bad_night_last", "high_usual"):
        assert counter in names
    assert sum(r.script.expect_flag for r in cohort) == 7


def test_generator_is_deterministic_per_seed():
    a, b = care.generate_cohort(5, END), care.generate_cohort(5, END)
    assert [r.days for r in a] == [r.days for r in b]
    c = care.generate_cohort(6, END)
    assert [r.days for r in a] != [r.days for r in c]


# --- rules fire on the scripted changes and not on the no-change scripts -------------------------
def test_default_seed_flags_every_scripted_change_and_nothing_else():
    for res, card in scored():
        if res.script.expect_flag:
            assert card["level"] in (care.LEVEL_WATCH, care.LEVEL_CHECK), (res.id, res.script.name, card["score"])
            assert set(res.script.moves) <= fired(card), (res.id, res.script.name, fired(card))
        else:
            assert card["score"] == 0 and card["level"] == care.LEVEL_LOW, (res.id, res.script.name, card["rules"])
            assert not fired(card)


@pytest.mark.parametrize("seed", range(200))
def test_seed_sweep_scripted_changes_fire_and_no_change_scripts_stay_low(seed):
    for res, card in scored(seed):
        if res.script.expect_flag:
            assert card["level"] != care.LEVEL_LOW and set(res.script.moves) <= fired(card), \
                (seed, res.id, res.script.name, fired(card))
        else:
            assert not fired(card) and card["level"] == care.LEVEL_LOW, (seed, res.id, res.script.name)


def test_one_off_bad_night_is_visible_but_scores_nothing():
    for res, card in scored():
        if res.script.bad_night_day is None:
            continue
        night = rule(card, care.NIGHT)
        assert max(night["recent_values"]) >= 2 * night["baseline"], night       # the bad night is real
        assert night["contribution"] == 0 and night["scored"]
        assert "needs 2 nights in a row" in night["explanation"]
        assert card["level"] == care.LEVEL_LOW


def test_high_usual_resident_is_judged_against_their_own_baseline():
    results = scored()
    high = [(r, c) for r, c in results if r.script.high_usual]
    stable = [r for r, _ in results if r.script.name == "stable"]
    assert len(high) == 2
    top_stable_sway = max(d.sway_rms for r in stable for d in r.days)
    top_stable_settle = max(d.settle_s for r in stable for d in r.days)
    top_stable_night = max(d.night_restless_min for r in stable for d in r.days)
    for res, card in high:
        # Every raw value is above any stable resident's, so a population cut-off would flag them...
        assert min(d.sway_rms for d in res.days) > top_stable_sway
        assert min(d.settle_s for d in res.days) > top_stable_settle
        assert min(d.night_restless_min for d in res.days) > top_stable_night
        # ...but against their own baseline nothing changed.
        assert card["score"] == 0 and card["level"] == care.LEVEL_LOW


def test_engine_never_sees_the_script():
    params = list(inspect.signature(care.score_resident).parameters)
    assert params == ["resident_id", "display_name", "days", "as_of", "briefing"]
    for fn in (care.score_resident, care.rule_sway, care.rule_settle, care.rule_night, care.level_for,
               care.suggested_check, care.local_briefing, care.evaluate):
        names = fn.__code__.co_names + fn.__code__.co_varnames
        assert not [n for n in names if "script" in n or "expect" in n], fn.__name__
    sql = [c for c in care.evaluate.__code__.co_consts if isinstance(c, str) and "SELECT" in c]
    assert any("FROM care_residents" in q for q in sql)
    assert not [q for q in sql if "script" in q or "expect_flag" in q]       # the rules' query never reads it
    # Same metrics under a different script give the same card.
    res = care.generate_cohort(care.DEFAULT_SEED, END)[1]
    swapped = replace(res, script=care.SCRIPTS["stable"])
    assert care.score_cohort([res], END) == care.score_cohort([swapped], END)


# --- the card contract ------------------------------------------------------------------------------
def test_every_card_meets_the_shared_contract():
    for _, card in scored():
        assert CONTRACT_KEYS <= set(card)
        assert card["synthetic"] is True
        assert isinstance(card["score"], int) and 0 <= card["score"] <= 100
        assert card["level"] in ("low", "watch", "check today")
        assert card["score"] == sum(r["contribution"] for r in card["rules"])
        assert [r["rule"] for r in card["rules"]] == list(care.RULES)
        for r in card["rules"]:
            assert RULE_KEYS <= set(r)
            assert isinstance(r["observed"], (int, float)) and isinstance(r["baseline"], (int, float))
            assert 0 <= r["contribution"] <= r["max_contribution"]
            assert r["threshold_status"] == "untested assumption" and r["threshold"]
            assert r["explanation"]
        assert card["thresholds_status"] == "untested assumption"
        assert "untested assumptions" in card["notice"] and "Synthetic data" in card["notice"]
        assert isinstance(card["suggested_check"], str) and card["suggested_check"]
        assert card["briefing"].startswith(card["display_name"])


def test_raw_numbers_behind_each_rule_are_on_the_card():
    for _, card in scored():
        for r in card["rules"]:
            assert len(r["recent_values"]) == 2 and len(r["baseline_values"]) == 2
            assert r["observed"] == pytest.approx(sum(r["recent_values"]) / 2, abs=0.6)
            assert r["baseline"] == pytest.approx(sum(r["baseline_values"]) / 2, abs=0.6)
        settle = rule(card, care.SETTLE)
        assert set(settle["components"]) == {"balance_s", "heart_rate_s"}       # accelerometer + heart rate
        assert settle["stand_ups"]["recent"] >= care.MIN_STAND_UPS


def test_briefing_is_written_after_the_decision_and_cannot_change_it():
    res = care.generate_cohort(care.DEFAULT_SEED, END)[7]
    a = care.score_resident(res.id, res.display_name, res.days, END)
    b = care.score_resident(res.id, res.display_name, res.days, END, briefing=lambda card: "x" * 10)
    assert b["briefing"] == "x" * 10
    assert {k: v for k, v in a.items() if k != "briefing"} == {k: v for k, v in b.items() if k != "briefing"}


# --- rule arithmetic on hand-made days ----------------------------------------------------------------
@pytest.mark.parametrize("sway,points", [(0.06, 0), (0.0715, 0), (0.0725, 15), (0.0815, 25), (0.0905, 30)])
def test_sway_bands(sway, points):
    card = care.score_resident("X", "Synthetic resident X", flat_days(sway_rms=sway), END)
    assert rule(card, care.SWAY)["contribution"] == points


@pytest.mark.parametrize("settle,points", [(24.0, 0), (25.5, 20), (28.5, 30), (32.5, 40)])
def test_settle_bands(settle, points):
    card = care.score_resident("X", "Synthetic resident X", flat_days(settle_s=settle), END)
    assert rule(card, care.SETTLE)["contribution"] == points


def test_settle_needs_a_minimum_change_in_seconds():
    days = [replace(d, settle_s=10.0) for d in flat_days()]
    days[-1] = replace(days[-1], settle_s=14.0)
    days[-2] = replace(days[-2], settle_s=14.0)                       # +40% but only 4 s
    r = rule(care.score_resident("X", "X", days, END), care.SETTLE)
    assert r["contribution"] == 0 and "minimum change" in r["explanation"]


def test_night_needs_both_nights():
    one = flat_days()
    one[-1] = replace(one[-1], night_restless_min=90.0)
    assert rule(care.score_resident("X", "X", one, END), care.NIGHT)["contribution"] == 0
    both = flat_days(night_restless_min=50.0)                         # +67% and +20 min each night
    assert rule(care.score_resident("X", "X", both, END), care.NIGHT)["contribution"] == 15
    double = flat_days(night_restless_min=61.0)
    assert rule(care.score_resident("X", "X", double, END), care.NIGHT)["contribution"] == 30


def test_lower_values_never_score():
    card = care.score_resident("X", "X", flat_days(sway_rms=0.03, settle_s=10.0, night_restless_min=5.0), END)
    assert card["score"] == 0 and card["level"] == care.LEVEL_LOW


@pytest.mark.parametrize("score,top,level", [
    (0, False, "low"), (14, False, "low"), (15, False, "watch"), (44, False, "watch"),
    (45, False, "check today"), (30, True, "check today"), (100, True, "check today"),
])
def test_levels(score, top, level):
    assert care.level_for(score, top) == level


def test_missing_data_scores_nothing_and_says_so():
    days = flat_days(sway_rms=0.2, settle_s=60.0, night_restless_min=200.0)
    sparse = [d for d in days if d.day != END]                        # the last day never arrived
    card = care.score_resident("X", "Synthetic resident X", sparse, END)
    assert card["score"] == 0 and not card["data_quality"]["all_rules_scored"]
    for r in card["rules"]:
        assert not r["scored"] and r["contribution"] == 0 and r["explanation"].startswith("Not enough data")
    assert "Not enough data for" in card["briefing"]
    low_wear = [replace(d, wear_hours=2.0, night_wear_min=60.0, stand_ups=2) for d in days]
    card = care.score_resident("X", "X", low_wear, END)
    assert card["score"] == 0 and not any(r["scored"] for r in card["rules"])


def test_suggested_check_is_for_the_aide_and_points_to_the_nurse():
    by_level = {}
    for _, card in scored():
        by_level.setdefault(card["level"], []).append(card)
    assert by_level["low"][0]["suggested_check"] == "No extra check suggested; continue routine care."
    for card in by_level.get("watch", []) + by_level["check today"]:
        assert card["suggested_check"].endswith("Tell the nurse what you see.")
    for card in by_level["check today"]:
        assert card["suggested_check"].startswith("Ask the nurse to review this resident today.")
