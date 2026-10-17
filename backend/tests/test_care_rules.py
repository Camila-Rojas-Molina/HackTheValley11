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
    base = dict(day=d, wear_hours=14.0, sway_rms=0.06, stand_ups=12, settle_s=None, settle_sway_s=16.0,
                settle_hr_s=None, night_restless_min=30.0, night_wear_min=410.0, stand_hr_n=6,
                stand_hr_delta_bpm=14.0)
    base.update(kw)
    return care.DayMetrics(**base)


def flat_days(**recent_kw) -> list[care.DayMetrics]:
    """RECORD_DAYS identical days; the last two take recent_kw."""
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
    assert names.count("stable") >= 8
    for counter in ("one_off_bad_night", "one_off_bad_night_last", "high_usual", "hr_rule_off_pacemaker"):
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
            assert card["level"] in (care.LEVEL_LOOK, care.LEVEL_REVIEW), (res.id, res.script.name, card["score"])
            assert set(res.script.moves) <= fired(card), (res.id, res.script.name, fired(card))
        else:
            assert card["score"] == 0 and card["level"] == care.LEVEL_LOW, (res.id, res.script.name, card["rules"])
            assert not fired(card)


@pytest.mark.parametrize("seed", list(range(200)) + [666])   # 666: a stable resident short of heart rate
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
    top_stable_hr = max(d.stand_hr_delta_bpm for r in stable for d in r.days if d.stand_hr_delta_bpm is not None)
    top_stable_night = max(d.night_restless_min for r in stable for d in r.days)
    for res, card in high:
        # Every raw value is above any stable resident's, so a population cut-off would flag them...
        assert min(d.sway_rms for d in res.days) > top_stable_sway
        assert min(d.stand_hr_delta_bpm for d in res.days if d.stand_hr_delta_bpm is not None) > top_stable_hr
        assert min(d.night_restless_min for d in res.days) > top_stable_night
        # ...but against their own baseline nothing changed.
        assert card["score"] == 0 and card["level"] == care.LEVEL_LOW


def test_engine_never_sees_the_script():
    params = list(inspect.signature(care.score_resident).parameters)
    assert params == ["resident_id", "display_name", "days", "as_of", "briefing", "record"]
    for fn in (care.score_resident, care.experimental_sway, care.experimental_balance, care.rule_stand_hr,
               care.rule_night, care.level_for,
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
        assert card["level"] in ("low", "worth a look this shift", "review this shift")
        assert card["score"] == sum(r["contribution"] for r in card["rules"])
        assert [r["rule"] for r in card["rules"]] == list(care.RULES)
        for r in card["rules"]:
            assert RULE_KEYS <= set(r)
            if r["status"] == "scored":
                assert isinstance(r["observed"], (int, float)) and isinstance(r["baseline"], (int, float))
            else:
                assert r["status"] == "off" and r["contribution"] == 0
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
            if r["status"] == "off":
                continue
            assert len(r["recent_values"]) == 2 and 7 <= len(r["baseline_values"]) <= care.BASELINE_DAYS
            assert r["observed"] == pytest.approx(sum(r["recent_values"]) / 2, abs=0.6)
            n = len(r["baseline_values"])
            assert r["baseline"] == pytest.approx(sum(r["baseline_values"]) / n, abs=1.0)  # whole minutes
        hr = rule(card, care.STAND_HR)
        if hr["status"] == "scored":
            assert min(hr["stands_with_hr"]["recent"]) >= care.MIN_STANDS_WITH_HR


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
    card = care.score_resident("X", "X", flat_days(sway_rms=0.03, settle_sway_s=5.0, night_restless_min=5.0), END)
    assert card["score"] == 0 and card["level"] == care.LEVEL_LOW


@pytest.mark.parametrize("today,span,level", [
    (set(), set(), "low"),
    (set(), {"night_restlessness"}, "low"),                                   # nothing fires today
    ({"night_restlessness"}, {"night_restlessness"}, "worth a look this shift"),
    ({"stand_hr_response"}, {"stand_hr_response", "night_restlessness"}, "review this shift"),
    ({"stand_hr_response", "night_restlessness"}, {"stand_hr_response", "night_restlessness"},
     "review this shift"),
])
def test_levels(today, span, level):
    assert care.level_for(today, span) == level


def test_missing_data_scores_nothing_and_says_so():
    days = flat_days(sway_rms=0.2, stand_hr_delta_bpm=40.0, night_restless_min=200.0)
    sparse = [d for d in days if d.day != END]                        # the last day never arrived
    card = care.score_resident("X", "Synthetic resident X", sparse, END)
    assert card["score"] == 0 and not card["data_quality"]["all_rules_scored"]
    for r in card["rules"]:
        assert not r["scored"] and r["contribution"] == 0 and r["explanation"].startswith("Not enough data")
    assert "Not enough data for" in card["briefing"]
    low_wear = [replace(d, wear_hours=2.0, night_wear_min=60.0, stand_ups=2, stand_hr_n=2) for d in days]
    card = care.score_resident("X", "X", low_wear, END)
    assert card["score"] == 0 and not any(r["scored"] for r in card["rules"])


def test_suggested_check_is_for_the_aide_and_points_to_the_nurse():
    by_level = {}
    for _, card in scored():
        by_level.setdefault(card["level"], []).append(card)
    assert by_level["low"][0]["suggested_check"] == "No extra check suggested; continue routine care."
    for card in by_level.get(care.LEVEL_LOOK, []) + by_level[care.LEVEL_REVIEW]:
        assert card["suggested_check"].endswith("Tell the nurse what you see.")
    for card in by_level[care.LEVEL_REVIEW]:
        assert card["suggested_check"].startswith("Ask the nurse to review this resident this shift.")
    for card in by_level[care.LEVEL_LOOK]:
        assert card["suggested_check"].startswith("Worth a look this shift:")


def test_missing_device_data_is_never_reported_as_routine_care():
    """A scripted decline whose last two days lost their data must not read 'continue routine care'."""
    cohort = care.generate_cohort(care.DEFAULT_SEED, END)
    r02 = next(r for r in cohort if r.id == "R-05")  # a scripted heart-rate-after-standing change
    assert care.score_resident(r02.id, r02.display_name, r02.days, END)["level"] != care.LEVEL_LOW
    not_worn = [replace(d, wear_hours=5.0, stand_ups=0, stand_hr_n=0) if d.day >= END - timedelta(days=1) else d
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
    days[-1] = replace(days[-1], stand_ups=0, stand_hr_n=0)
    card = care.score_resident("X", "X", days, END)
    assert card["level"] == care.LEVEL_LOOK and not card["data_quality"]["all_rules_scored"]


def test_stand_hr_not_enough_data_text_states_the_rule_it_applies():
    days = flat_days()
    days[-1] = replace(days[-1], stand_hr_n=2)
    r = rule(care.score_resident("X", "X", days, END), care.STAND_HR)
    assert not r["scored"] and r["status"] == "not enough data"
    assert "3+ stands with heart rate on each of the last 2 days" in r["explanation"]
    assert "3+ stands with heart rate" in care.THRESHOLDS[care.STAND_HR]


# --- research 1.3: rolling 14-day baseline, 7 valid days of warm-up, 28-day record -----------------
def test_baseline_is_the_14_days_just_before_the_recent_window():
    recent, baseline = care.windows(END)
    assert recent == [END - timedelta(days=1), END]
    assert baseline == [END - timedelta(days=15 - i) for i in range(14)]
    assert care.BASELINE_DAYS == 14 and care.WARMUP_DAYS == 7


def test_days_before_the_rolling_baseline_do_not_count():
    days = flat_days()
    early = [replace(d, stand_hr_delta_bpm=60.0, night_restless_min=300.0, sway_rms=0.5) if d.day < END - timedelta(days=15)
             else d for d in days]
    a = care.score_resident("X", "X", days, END)
    b = care.score_resident("X", "X", early, END)
    assert a == b


@pytest.mark.parametrize("valid,scores", [(6, False), (7, True), (14, True)])
def test_no_rule_can_fire_before_7_valid_baseline_days(valid, scores):
    """Warm-up (Rispens 2015: one week gives stable gait measures): fewer than 7 valid days in the
    baseline window means the rule does not score, however large the change."""
    days = flat_days(stand_hr_delta_bpm=40.0, night_restless_min=200.0)
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
        for onset, ramp in ((script.onset, script.sway_per_day), (script.night_onset, script.night_per_day),
                            (script.hr_onset, script.hr_shift_bpm)):
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


# --- research 1.2: heart-rate response to standing replaces "rise to settle" --------------------------
def hr_days(baseline: list[float], recent: tuple[float, float], **kw) -> list[care.DayMetrics]:
    days = flat_days()
    _, window = care.windows(END)
    by_day = dict(zip(window, baseline))
    out = []
    for d in days:
        if d.day in by_day:
            d = replace(d, stand_hr_delta_bpm=by_day[d.day])
        elif d.day == END - timedelta(days=1):
            d = replace(d, stand_hr_delta_bpm=recent[0], **kw)
        elif d.day == END:
            d = replace(d, stand_hr_delta_bpm=recent[1], **kw)
        out.append(d)
    return out


BASE_14 = [12.0, 16.0] * 7           # usual rise +14 bpm, sample SD 2.08 bpm


def test_settle_time_is_gone_and_its_balance_half_is_experimental():
    assert care.RULES == (care.STAND_HR, care.NIGHT)
    assert "rise_to_settle_time" not in care.RULES and "rise_to_settle_time" not in care.MAX_POINTS
    assert care.SETTLE_BALANCE in care.EXPERIMENTAL and care.SETTLE_BALANCE in care.EXPERIMENTAL_NOTES
    card = care.score_resident("X", "X", flat_days(settle_sway_s=40.0), END)
    exp = {e["signal"]: e for e in card["experimental"]}[care.SETTLE_BALANCE]
    assert exp["status"] == "experimental, not scored" and exp["observed"] == 40.0 and card["score"] == 0


@pytest.mark.parametrize("recent,points", [
    ((9.5, 9.5), 0),                 # z -2.2 both days: under 2.5 SD
    ((8.5, 8.0), 30),                # z -2.6 and -2.9: smaller rise than usual on both days
    ((3.0, 4.0), 55),                # z -5.3 and -4.8: top band
    ((20.0, 20.0), 30),              # z +2.9 both days: bigger rise than usual also counts
    ((8.0, 20.0), 0),                # opposite directions: no points
    ((8.0, 13.0), 0),                # one day only
])
def test_stand_hr_rule_is_z_on_both_days_in_one_direction(recent, points):
    r = rule(care.score_resident("X", "X", hr_days(BASE_14, recent), END), care.STAND_HR)
    assert r["scored"] and r["contribution"] == points, r
    assert r["baseline"] == pytest.approx(14.0, abs=0.05) and r["unit"] == "bpm"
    assert len(r["z"]) == 2


def test_stand_hr_explanation_is_plain_and_names_no_condition():
    r = rule(care.score_resident("X", "X", hr_days(BASE_14, (3.0, 4.0)), END), care.STAND_HR)
    assert r["explanation"].startswith("Heart rate rose less than usual after standing")
    assert "+14 bpm" in r["explanation"] and "Many things can cause this" in r["explanation"]
    assert "medications or watch fit" in r["explanation"]


def test_stand_hr_needs_3_stands_with_heart_rate_on_each_recent_day():
    days = hr_days(BASE_14, (3.0, 4.0), stand_hr_n=2)
    r = rule(care.score_resident("X", "X", days, END), care.STAND_HR)
    assert not r["scored"] and r["contribution"] == 0 and care.MIN_STANDS_WITH_HR == 3


def test_stand_hr_sd_floor_is_2_bpm():
    r = rule(care.score_resident("X", "X", hr_days([14.0] * 14, (10.0, 10.0)), END), care.STAND_HR)
    assert r["baseline_sd"] == 2.0 and r["z"] == [-2.0, -2.0] and r["contribution"] == 0


@pytest.mark.parametrize("reason", ["pacemaker", "non-sinus rhythm"])
def test_stand_hr_rule_is_off_for_a_pacemaker_or_non_sinus_rhythm(reason):
    days = hr_days(BASE_14, (3.0, 4.0))
    card = care.score_resident("X", "Synthetic resident X", days, END, record={"hr_rule_off": reason})
    r = rule(card, care.STAND_HR)
    assert r["status"] == "off" and not r["scored"] and r["contribution"] == 0
    assert reason in r["explanation"] and "not compared" in r["explanation"]
    assert card["score"] == 0 and card["level"] == care.LEVEL_LOW        # off is not "data missing"


def test_pacemaker_counter_example_in_the_cohort_scores_nothing():
    for res, card in scored():
        if res.script.name == "hr_rule_off_pacemaker":
            assert res.record["hr_rule_off"] == "pacemaker"
            assert rule(card, care.STAND_HR)["status"] == "off" and card["level"] == care.LEVEL_LOW


def test_stand_hr_aide_check_points_to_vitals_per_protocol():
    text = care.AIDE_CHECKS[care.STAND_HR]
    assert text.startswith("if they seem unsteady or dizzy, consider lying and standing vitals per your protocol")
    assert "lying 5 min" in text and "1 and 3 min standing" in text
    assert "SOURCES" in dir(care) and "CDC STEADI" in care.SOURCES[care.STAND_HR]
    assert "Norcliffe-Kaufmann 2018" in care.SOURCES[care.STAND_HR]


# --- research 1.5: a single rule is "worth a look"; "review" needs 2 different rules in 3 days -------
def shifted(days, dates, **kw):
    return [replace(d, **kw) if d.day in dates else d for d in days]


def test_a_single_rule_at_its_top_band_is_only_worth_a_look():
    card = care.score_resident("X", "X", flat_days(night_restless_min=200.0), END)
    night = rule(card, care.NIGHT)
    assert night["contribution"] == night["max_contribution"] and night["top_band"]
    assert card["level"] == care.LEVEL_LOOK


def test_two_rules_today_is_review_this_shift():
    card = care.score_resident("X", "X", flat_days(night_restless_min=200.0, stand_hr_delta_bpm=2.0), END)
    assert card["level"] == care.LEVEL_REVIEW
    assert card["fired_last_3_days"] == {care.NIGHT: [END.isoformat()], care.STAND_HR: [END.isoformat()]}


def test_a_different_rule_two_days_ago_makes_it_review():
    days = flat_days(night_restless_min=200.0)                     # night fires today
    two_ago = END - timedelta(days=2)
    days = shifted(days, {two_ago - timedelta(days=1), two_ago}, stand_hr_delta_bpm=2.0)  # heart rate fired then
    card = care.score_resident("X", "X", days, END)
    assert rule(card, care.STAND_HR)["contribution"] == 0          # not today...
    assert two_ago.isoformat() in card["fired_last_3_days"][care.STAND_HR]
    assert card["level"] == care.LEVEL_REVIEW                       # ...but within the last 3 days


def test_a_rule_three_days_ago_is_outside_the_span():
    days = flat_days(night_restless_min=200.0)
    three_ago = END - timedelta(days=3)
    days = shifted(days, {three_ago - timedelta(days=1), three_ago}, stand_hr_delta_bpm=2.0)
    card = care.score_resident("X", "X", days, END)
    assert care.STAND_HR not in card["fired_last_3_days"]
    assert card["level"] == care.LEVEL_LOOK


def test_score_no_longer_sets_the_level():
    assert not hasattr(care, "CHECK_TODAY_FROM") and not hasattr(care, "WATCH_FROM")
    assert care.LEVELS == ("low", "worth a look this shift", "review this shift", "not enough data")
    assert care.FLAGGED == ("worth a look this shift", "review this shift")
    assert "2 different rules" in care.THRESHOLDS["levels"] and "3 days" in care.THRESHOLDS["levels"]


# --- research 1.7: data quality, medication context and the pattern-change line on every card -------
def test_every_card_shows_hours_worn_gaps_and_heart_rate_availability():
    for _, card in scored():
        dq = card["data_quality"]
        assert dq["days_expected"] == 16 and dq["gaps"] == [] and dq["days_present"] == 16
        assert 0 < dq["hours_worn_last_48h"] <= 48
        assert dq["heart_rate"]["of_days"] == 16 and 0 <= dq["heart_rate"]["available_days"] <= 16
        assert dq["heart_rate"]["label"].startswith("available on ")


def test_gaps_and_missing_heart_rate_are_listed():
    days = flat_days()
    _, baseline = care.windows(END)
    gone = {baseline[3], baseline[4]}
    days = [replace(d, stand_hr_n=0) for d in days if d.day not in gone]
    dq = care.score_resident("X", "X", days, END)["data_quality"]
    assert dq["gaps"] == sorted(x.isoformat() for x in gone) and dq["days_present"] == 14
    assert dq["heart_rate"] == {"available_days": 0, "of_days": 16, "label": "not available"}


MED_9 = {"class": "antihypertensive", "change": "started", "on": (END - timedelta(days=9)).isoformat()}


def test_medication_context_raises_a_look_to_review():
    card = care.score_resident("X", "X", flat_days(night_restless_min=200.0), END, record={"med_changes": [MED_9]})
    assert card["level"] == care.LEVEL_REVIEW and card["priority_raised_by"] == "medication context"
    ctx = card["record_context"]
    assert ctx["synthetic"] is True and ctx["raises_priority"] is True
    assert ctx["medication_changes_30d"] == [{**MED_9, "days_ago": 9}]
    assert "started an antihypertensive 9 days ago" in card["briefing"]


def test_medication_context_never_alerts_on_its_own():
    card = care.score_resident("X", "X", flat_days(), END, record={"med_changes": [MED_9]})
    assert card["level"] == care.LEVEL_LOW and card["priority_raised_by"] is None
    assert card["record_context"]["raises_priority"] is False      # listed, but it starts nothing


@pytest.mark.parametrize("med", [
    {"class": "antihypertensive", "change": "started", "on": (END - timedelta(days=31)).isoformat()},  # too old
    {"class": "vitamin D", "change": "started", "on": (END - timedelta(days=3)).isoformat()},         # not listed
])
def test_medication_context_outside_30_days_or_the_listed_classes_does_not_raise(med):
    card = care.score_resident("X", "X", flat_days(night_restless_min=200.0), END, record={"med_changes": [med]})
    assert card["level"] == care.LEVEL_LOOK and card["priority_raised_by"] is None


def test_listed_medication_classes_are_the_research_ones():
    assert "antihypertensive" in care.MED_CLASSES_RAISE
    for c in ("anticholinergic", "antipsychotic", "benzodiazepine", "corticosteroid", "H2 blocker", "Z-drug",
              "opioid", "SSRI", "SNRI", "TCA", "antiepileptic"):
        assert c in care.MED_CLASSES_RAISE, c
    assert care.MED_WINDOW_DAYS == 30
    assert "Beers 2023" in care.SOURCES["medication"] and "Dave 2024" in care.SOURCES["medication"]


def test_cohort_has_a_raised_card_and_a_medication_change_that_alerts_nothing():
    cards = {c["resident_id"]: c for _, c in scored()}
    raised = [c for c in cards.values() if c["priority_raised_by"] == "medication context"]
    assert raised and all(c["level"] == care.LEVEL_REVIEW for c in raised)
    quiet = [c for c in cards.values() if c["record_context"]["medication_changes_30d"] and not c["rules"][1]["contribution"]
             and not c["rules"][0]["contribution"]]
    assert quiet and all(c["level"] == care.LEVEL_LOW for c in quiet)


def test_every_card_says_pattern_change_only():
    assert care.PATTERN_NOTE == "Pattern change only - not a medical assessment."
    for _, card in scored():
        assert card["pattern_note"] == care.PATTERN_NOTE
        assert card["briefing"].endswith(care.PATTERN_NOTE)


# --- research 1.8: the 0-100 number is a change score, never a risk score ---------------------------
def test_score_is_labelled_a_change_score():
    assert care.SCORE_LABEL == "change score (0-100)"
    for _, card in scored():
        assert card["score_label"] == care.SCORE_LABEL and card["score_note"] == care.SCORE_NOTE
        assert f"change score {card['score']}/100" in card["briefing"]
    assert "risk" not in (care.SCORE_LABEL + care.SCORE_NOTE).lower()
    assert "own usual" in care.SCORE_NOTE


def test_template_and_prompt_call_it_a_change_score():
    from app import vultr
    card = scored()[7][1]
    assert f"(change score {card['score']}/100)" in vultr.template_briefing(card)
    assert "change score out of 100" in vultr.SYSTEM_PROMPT and "never call it a risk" in vultr.SYSTEM_PROMPT


# --- research 1.7: Useful / Not useful / Already known / Snooze buttons on flagged cards --------------
def test_flagged_cards_offer_the_four_feedback_buttons():
    for _, card in scored():
        if card["level"] in care.FLAGGED:
            assert [a["id"] for a in card["actions"]] == ["useful", "not_useful", "already_known", "snooze_3d"]
            assert [a["label"] for a in card["actions"]] == ["Useful", "Not useful", "Already known", "Snooze 3 days"]
            assert next(a for a in card["actions"] if a["id"] == "already_known")["asks_reason"] is True
            assert card["feedback_url"] == f"/v1/care/alerts/{card['resident_id']}/feedback"
        else:
            assert card["actions"] == [] and card["feedback_url"] is None
    assert care.SNOOZE_DAYS == 3


# --- research 1.6: at most 1 push per resident per 24 h; identical alert muted 3 days unless worse -----
from datetime import datetime, timezone  # noqa: E402

NOW = datetime(2026, 10, 17, 7, 0, tzinfo=timezone.utc)


def look_card(**kw):
    card = care.score_resident("X", "X", flat_days(night_restless_min=200.0), END)
    card.update(kw)
    return card


def push(as_of, hours_ago, card):
    return care.Push(as_of=as_of, at=NOW - timedelta(hours=hours_ago), signature=care.signature(card),
                     level=card["level"], score=card["score"])


def test_a_first_flagged_card_is_pushed():
    d = care.delivery_for(look_card(), [], None, NOW)
    assert d["channel"] == "push" and d["reason"] == "first alert for this resident in 24 h"


def test_low_cards_are_not_delivered_and_no_data_goes_to_the_digest():
    low = care.score_resident("X", "X", flat_days(), END)
    assert care.delivery_for(low, [], None, NOW)["channel"] is None
    nodata = care.score_resident("X", "X", [d for d in flat_days() if d.day != END], END)
    assert care.delivery_for(nodata, [], None, NOW)["channel"] == "digest"


def test_at_most_one_push_per_resident_per_24_hours():
    card = look_card()
    other = look_card(level=care.LEVEL_REVIEW, fired_last_3_days={care.NIGHT: [], care.STAND_HR: []})
    d = care.delivery_for(card, [push(END - timedelta(days=1), 5, other)], None, NOW)
    assert d["channel"] == "digest" and "1 push per resident per 24 h" in d["reason"]
    d = care.delivery_for(card, [push(END - timedelta(days=1), 25, other)], None, NOW)
    assert d["channel"] == "push"


def test_re_scoring_the_same_day_keeps_its_push():
    card = look_card()
    d = care.delivery_for(card, [push(END, 1, card)], None, NOW)
    assert d["channel"] == "push" and d["reason"] == "already pushed for this day"


def test_an_identical_alert_is_muted_for_3_days_unless_it_gets_worse():
    card = look_card()
    muted = care.delivery_for(card, [push(END - timedelta(days=2), 30, card)], None, NOW)
    assert muted["channel"] == "digest" and "muted 3 days unless it gets worse" in muted["reason"]
    old = care.delivery_for(card, [push(END - timedelta(days=3), 30, card)], None, NOW)
    assert old["channel"] == "push"
    lower = dict(card, score=card["score"] - 20)
    worse = care.delivery_for(card, [push(END - timedelta(days=2), 30, lower)], None, NOW)
    assert worse["channel"] == "push" and worse["reason"] == "worse than the alert pushed on " + \
        (END - timedelta(days=2)).isoformat()


def test_a_snoozed_resident_goes_to_the_digest():
    d = care.delivery_for(look_card(), [], NOW + timedelta(hours=10), NOW)
    assert d["channel"] == "digest" and d["reason"].startswith("snoozed until ")
    assert care.delivery_for(look_card(), [], NOW - timedelta(minutes=1), NOW)["channel"] == "push"


def test_budget_constants_and_sources():
    assert care.PUSH_BUDGET_HOURS == 24 and care.MUTE_DAYS == 3
    assert "Ancker 2017" in care.SOURCES["budget"] and "Joint Commission" in care.SOURCES["budget"]


# --- review round 2: heart rate is "when available"; the medication raise is a stated threshold ----------
def test_heart_rate_that_is_not_available_never_makes_a_worn_device_read_as_missing_data():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    few = flat_days(stand_hr_n=2)                                                  # worn, 2 stands with HR
    none_ever = [replace(d, stand_hr_n=0, stand_hr_delta_bpm=None) for d in flat_days()]   # never any HR
    for days in (few, none_ever):
        card = care.score_resident("X", "Synthetic resident X", days, END)
        assert rule(card, care.STAND_HR)["status"] == "not enough data"
        assert card["level"] == care.LEVEL_LOW, card["level"]
        assert "worn and charged" not in card["suggested_check"] and "worn and charged" not in card["briefing"]
        assert care.delivery_for(card, [], None, now)["channel"] is None
    # Motion data really missing (no stand-ups counted) still reads as missing device data.
    gone = flat_days(stand_ups=0, stand_hr_n=0)
    assert care.score_resident("X", "X", gone, END)["level"] == care.LEVEL_NO_DATA


def test_seed_666_keeps_every_no_change_resident_out_of_the_digest():
    from datetime import datetime, timezone
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    for res, card in scored(666):
        if not res.script.expect_flag:
            assert card["level"] == care.LEVEL_LOW, (res.id, res.script.name, card["level"])
            assert care.delivery_for(card, [], None, now)["channel"] is None


def test_the_medication_raise_is_a_stated_threshold_on_every_card_it_raises():
    raised = [card for seed in range(40) for _, card in scored(seed) if card["priority_raised_by"]]
    assert raised
    for card in raised:
        t = card["thresholds"]
        assert card["level"] == care.LEVEL_REVIEW and len(card["fired_last_3_days"]) < 2
        assert f"{care.MED_WINDOW_DAYS} days" in t["medication"] and "antihypertensive" in t["medication"]
        assert all(cls in t["medication"] for cls in care.MED_CLASSES_RAISE)
        assert care.ASSUMPTION in t["medication"]
        assert "medication" in t["levels"]                    # the levels text allows what the card shows
        assert card["record_context"]["threshold_status"] == care.ASSUMPTION
        assert card["record_context"]["window_days"] == care.MED_WINDOW_DAYS
