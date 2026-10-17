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
def test_cohort_is_20_residents_by_28_days_all_synthetic():
    cohort = care.generate_cohort(care.DEFAULT_SEED, END)
    assert len(cohort) == 20 and len({r.id for r in cohort}) == 20
    assert care.RECORD_DAYS == 28
    for r in cohort:
        assert len(r.days) == 28 and r.days[-1].day == END and r.days[0].day == END - timedelta(days=27)
        assert all(d.synthetic for d in r.days)
        assert r.display_name.startswith("Synthetic resident ")
    names = [r.script.name for r in cohort]
    assert names.count("stable") >= 9
    for counter in ("one_off_bad_night", "one_off_bad_night_last", "high_usual"):
        assert counter in names
    assert sum(r.script.expect_flag for r in cohort) == 5
    # Sway-only scripts are counter-examples now: sway is shown, never scored (research 1.1).
    assert {r.script.name for r in cohort if r.script.moves == () and 'sway' in r.script.name} == \
        {'sway_drift', 'sway_drift_steep'}


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
    for fn in (care.score_resident, care.experimental_sway, care.rule_settle, care.rule_night, care.level_for,
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
            assert len(r["recent_values"]) == 2 and len(r["baseline_values"]) == care.BASELINE_DAYS
            assert r["observed"] == pytest.approx(sum(r["recent_values"]) / 2, abs=0.6)
            n = len(r["baseline_values"])
            assert r["baseline"] == pytest.approx(sum(r["baseline_values"]) / n, abs=0.6)
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
# --- research 1.1: postural sway is shown greyed out, never scored --------------------------------
@pytest.mark.parametrize("sway", [0.06, 0.0725, 0.0905, 0.2])
def test_sway_never_changes_the_score_or_level(sway):
    card = care.score_resident("X", "Synthetic resident X", flat_days(sway_rms=sway), END)
    assert care.SWAY not in [r["rule"] for r in card["rules"]]
    assert card["score"] == 0 and card["level"] == care.LEVEL_LOW
    exp = {e["signal"]: e for e in card["experimental"]}[care.SWAY]
    assert exp["status"] == "experimental, not scored" and exp["contribution"] == 0
    assert exp["observed"] == pytest.approx(sway, abs=1e-3) and exp["baseline"] == pytest.approx(0.06)


def test_sway_is_out_of_the_points_and_the_points_still_sum_to_100():
    assert care.SWAY not in care.RULES and care.SWAY not in care.MAX_POINTS
    assert care.SWAY in care.EXPERIMENTAL
    assert sum(care.MAX_POINTS.values()) == 100
    assert set(care.MAX_POINTS) == set(care.RULES)


def test_sway_only_scripts_stay_low_but_the_change_is_visible():
    for res, card in scored():
        if res.script.sway_per_day and not res.script.moves:
            exp = {e["signal"]: e for e in card["experimental"]}[care.SWAY]
            assert exp["observed"] > exp["baseline"], exp           # the drift is on the card...
            assert card["level"] == care.LEVEL_LOW and card["score"] == 0    # ...and scores nothing


@pytest.mark.parametrize("settle,points", [(24.0, 0), (25.5, 25), (28.5, 40), (32.5, 55)])
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
    assert rule(care.score_resident("X", "X", both, END), care.NIGHT)["contribution"] > 0


# --- research 1.4: night rule is z >= +2.5 against the 14-day baseline AND at least +15% -------------
def night_days(baseline: list[float], recent: tuple[float, float]) -> list[care.DayMetrics]:
    """A 28-day record whose 14-day baseline nights are `baseline` and last two nights `recent`."""
    days = flat_days()
    _, window = care.windows(END)
    by_day = dict(zip(window, baseline))
    out = []
    for d in days:
        if d.day in by_day:
            d = replace(d, night_restless_min=by_day[d.day])
        elif d.day == END - timedelta(days=1):
            d = replace(d, night_restless_min=recent[0])
        elif d.day == END:
            d = replace(d, night_restless_min=recent[1])
        out.append(d)
    return out


BASE_40 = [36.0, 44.0] * 7          # mean 40 min, sample SD 4.15 min


@pytest.mark.parametrize("recent,points", [
    ((49.0, 49.0), 0),               # z +2.2, +22.5%, +9 min: under 2.5 SD
    ((51.0, 51.0), 0),               # z +2.65, +27.5%, but +11 min: under the 15-minute minimum
    ((55.5, 55.5), 25),              # z +3.7, +39%, +15.5 min: fires
    ((57.0, 70.0), 45),              # z +4.1 and +7.2 on both nights: top band (Skubic: 2.5-4 SD)
    ((55.5, 40.0), 0),               # only one night: needs 2 nights in a row
])
def test_night_rule_uses_z_against_the_rolling_baseline(recent, points):
    r = rule(care.score_resident("X", "X", night_days(BASE_40, recent), END), care.NIGHT)
    assert r["scored"] and r["contribution"] == points, r
    assert r["baseline"] == 40 and r["baseline_sd"] == pytest.approx(4.2, abs=0.05)
    assert len(r["z"]) == 2


def test_night_rule_needs_at_least_15_percent_even_with_a_big_z():
    """A very steady fortnight (SD floored at 5 min on a 100-minute usual): +13 min is z +2.6 but only +13%."""
    steady = [98.0, 102.0] * 7
    r = rule(care.score_resident("X", "X", night_days(steady, (113.0, 113.0)), END), care.NIGHT)
    assert min(r["z"]) >= 2.5 and r["change_pct"] < 15 and r["contribution"] == 0, r


def test_night_sd_has_a_floor_so_a_flat_baseline_cannot_explode_z():
    flat = [40.0] * 14
    r = rule(care.score_resident("X", "X", night_days(flat, (46.0, 46.0)), END), care.NIGHT)
    assert r["baseline_sd"] == pytest.approx(2.0)                   # 5% of the usual, not 0
    assert r["z"] == [3.0, 3.0] and r["contribution"] == 0          # +6 min: under 15 min anyway


def test_night_threshold_is_labelled_with_its_sources():
    assert care.NIGHT_Z == 2.5 and care.NIGHT_MIN_REL == 0.15 and care.NIGHT_MIN_CHANGE_MIN == 15.0
    text = care.THRESHOLDS[care.NIGHT]
    assert "z >= +2.5" in text and "+15%" in text and "15 min" in text and "2 nights in a row" in text
    r = rule(care.score_resident("X", "X", night_days(BASE_40, (55.5, 55.5)), END), care.NIGHT)
    assert "Skubic 2015" in r["source"] and "Leung 2023" in r["source"]
    assert r["threshold_status"] == "untested assumption"


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


def test_missing_device_data_is_never_reported_as_routine_care():
    """A scripted decline whose last two days lost their data must not read 'continue routine care'."""
    cohort = care.generate_cohort(care.DEFAULT_SEED, END)
    r02 = next(r for r in cohort if r.id == "R-05")  # a scripted settling change
    assert care.score_resident(r02.id, r02.display_name, r02.days, END)["level"] != care.LEVEL_LOW
    not_worn = [replace(d, wear_hours=5.0, stand_ups=0) if d.day >= END - timedelta(days=1) else d
                for d in r02.days]
    gone = [d for d in r02.days if d.day < END - timedelta(days=1)]
    for days in (not_worn, gone):
        card = care.score_resident(r02.id, r02.display_name, days, END)
        assert card["level"] == care.LEVEL_NO_DATA, card["level"]
        assert "continue routine care" not in card["suggested_check"]
        assert card["suggested_check"].startswith("Device data is missing for:")
        assert not card["data_quality"]["all_rules_scored"]
    # Real scores still win: a rule that scored high with others unscored keeps its level.
    days = flat_days(night_restless_min=200.0)
    days[-1] = replace(days[-1], stand_ups=0)
    card = care.score_resident("X", "X", days, END)
    assert card["level"] == care.LEVEL_CHECK and not card["data_quality"]["all_rules_scored"]


def test_settle_not_enough_data_text_states_the_rule_it_applies():
    base = flat_days()
    twelve_then_none = flat_days()
    twelve_then_none[-2] = replace(twelve_then_none[-2], stand_ups=12)
    twelve_then_none[-1] = replace(twelve_then_none[-1], stand_ups=0)
    r = rule(care.score_resident("X", "X", twelve_then_none, END), care.SETTLE)
    assert not r["scored"]
    assert "8+ stand-ups per 48 h" in care.THRESHOLDS[care.SETTLE]
    assert "8+ stand-ups per 48 h" in r["explanation"] and "on both days" in r["explanation"]
    assert "8+ stand-ups on both days" not in r["explanation"]  # that is not the rule
    four_and_four = [replace(d, stand_ups=4) if d.day >= END - timedelta(days=1) else d for d in base]
    assert rule(care.score_resident("X", "X", four_and_four, END), care.SETTLE)["scored"] is True


# --- research 1.3: rolling 14-day baseline, 7 valid days of warm-up, 28-day record -----------------
def test_baseline_is_the_14_days_just_before_the_recent_window():
    recent, baseline = care.windows(END)
    assert recent == [END - timedelta(days=1), END]
    assert baseline == [END - timedelta(days=15 - i) for i in range(14)]
    assert care.BASELINE_DAYS == 14 and care.WARMUP_DAYS == 7


def test_days_before_the_rolling_baseline_do_not_count():
    days = flat_days()
    early = [replace(d, settle_s=90.0, night_restless_min=300.0, sway_rms=0.5) if d.day < END - timedelta(days=15)
             else d for d in days]
    a = care.score_resident("X", "X", days, END)
    b = care.score_resident("X", "X", early, END)
    assert a == b


@pytest.mark.parametrize("valid,scores", [(6, False), (7, True), (14, True)])
def test_no_rule_can_fire_before_7_valid_baseline_days(valid, scores):
    """Warm-up (Rispens 2015: one week gives stable gait measures): fewer than 7 valid days in the
    baseline window means the rule does not score, however large the change."""
    days = flat_days(settle_s=60.0, night_restless_min=200.0)
    _, baseline = care.windows(END)
    keep = set(baseline[len(baseline) - valid:])
    days = [d for d in days if d.day >= END - timedelta(days=1) or d.day in keep]
    card = care.score_resident("X", "Synthetic resident X", days, END)
    for r in card["rules"]:
        assert r["scored"] is scores, r
        assert r["baseline_days"] == valid
        if not scores:
            assert "warm-up" in r["explanation"] and f"{valid} of 7" in r["explanation"]
    assert (card["level"] == care.LEVEL_NO_DATA) is not scores
    assert (card["score"] > 0) is scores


def test_scripted_changes_start_after_the_warm_up():
    for script in care.SCRIPTS.values():
        for onset, ramp in ((script.onset, script.settle_per_day or script.sway_per_day),
                            (script.night_onset, script.night_per_day)):
            if ramp:
                assert onset >= care.WARMUP_DAYS + care.WINDOW_DAYS, (script.name, onset)
        if script.bad_night_day is not None:
            assert script.bad_night_day >= care.RECORD_DAYS - care.WINDOW_DAYS, script.name


def test_card_names_the_rolling_baseline():
    card = care.score_cohort(care.generate_cohort(care.DEFAULT_SEED, END), END)[0]
    bw = card["baseline_window"]
    assert bw["days"] == 14 and bw["from"] == (END - timedelta(days=15)).isoformat()
    assert bw["to"] == (END - timedelta(days=2)).isoformat()
    assert "rolling" in bw["note"] and "7 valid days" in bw["note"]
    assert card["data_quality"]["days_expected"] == 16
