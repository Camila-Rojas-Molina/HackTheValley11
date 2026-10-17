"""PointClickCare senior mode: synthetic residents, deterministic rules, explainable cards.

Separate from the KAZE check-in path: this module imports nothing from it, writes only
the care_* tables and sends no messages. Plain code decides every score and level; no
model takes part. The briefing text is written from the finished card afterwards.

Data. generate_cohort() makes 20 synthetic residents x 28 days. Every resident and every
row is labelled synthetic. Seven residents follow a scripted gradual change; the rest are
no-change scripts: stable residents, a one-off bad night, and residents whose usual values
are simply high. The script is ground truth for checking the rules; the rules never read it.

Rules. Each compares a resident with their own baseline, never with other residents:
  stand_hr_response     heart-rate change after standing (when available): mean heart rate
                        60-180 s after standing minus the 60 s before, daily median, at
                        z <= -2.5 or >= +2.5 on both of the last 2 days (research 1.2). Off for a
                        resident whose record notes non-sinus rhythm or a pacemaker. Heart rate
                        alone cannot show blood pressure, so the suggested check is lying and
                        standing vitals per the home's protocol, never a named condition.
  night_restlessness    minutes moving at night; both of the last 2 nights must be at least
                        2.5 SD (z) and 15% above their 14-day usual, and 15 min more, so one
                        bad night scores nothing (research 1.4)
Postural sway is shown on the card greyed out, "experimental, not scored" (research 1.1):
traditional sway measures were not associated with the outcome studied in 738 older adults
(Zhou 2017, PMC5462759), and sway measured at the wrist or in the hair is not validated. The
balance half of the old "rise to settle" rule is derived from sway, so it is greyed out too.
Baseline (research 1.3): rolling, the 14 days just before the recent 48 h (Skubic 2015,
PMC4848095: "two weeks immediately before the day examined"), so the recent window never
compares with itself. A rule needs 7 valid days in that baseline before it can score at all
(warm-up; Rispens 2015: one week of daily-life data gave stable gait measures, ICC > 0.75).
The generator's day-to-day variation is a chosen demo value and the scripted changes are
large next to it; real data will be noisier, and how early a change shows up here comes
from the script, not from any study.

Card. score is a 0-100 change score (research 1.8), the sum of today's rule contributions (55 + 45 at most). The level does
not follow the score (research 1.5): "worth a look this shift" when one rule fires today;
"review this shift" only when 2 different rules fired in the last 3 days, one of them today
(Skubic 2015: a multi-feature rule matched an SVM at about 86% accuracy, while single-feature
alerts were rated good only 39% of the time); otherwise "low", or "not enough data" when a rule
could not score (the data stopped). Every threshold here is an untested assumption (a demo
parameter), not a clinical cut-off, and the API says so on every card.
"""
from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional, Sequence

NOTICE = ("Hackathon prototype. Synthetic data only; no real residents. Rule-based change flags, "
          "not a medical device and not clinically validated. Thresholds are untested assumptions.")
ASSUMPTION = "untested assumption"
METHOD = "deterministic rules against the resident's own baseline; no model"

SWAY, SETTLE_BALANCE = "postural_sway_drift", "settle_balance"
STAND_HR, NIGHT = "stand_hr_response", "night_restlessness"
RULES = (STAND_HR, NIGHT)               # scored
EXPERIMENTAL = (SWAY, SETTLE_BALANCE)   # shown greyed out, never scored (research 1.1, 1.2)
LEVEL_LOW, LEVEL_LOOK, LEVEL_REVIEW = "low", "worth a look this shift", "review this shift"
LEVEL_NO_DATA = "not enough data"  # nothing fired and a rule was unable to score: the data stopped
LEVELS = (LEVEL_LOW, LEVEL_LOOK, LEVEL_REVIEW, LEVEL_NO_DATA)
FLAGGED = (LEVEL_LOOK, LEVEL_REVIEW)
# "Review this shift" needs 2 different rules in this many days, today included (research 1.5:
# "at least 2 different rules" in a 3-day span; Skubic 2015 multi-feature rules).
LEVEL_SPAN_DAYS = 3

DEFAULT_SEED = 11
RESIDENT_COUNT = 20
RECORD_DAYS = 28                # research 1.3: at least 21 days; 28 gives a full baseline + room
SEED_LOCK_ID = 7_202_612  # advisory lock: one cohort seed at a time

# --- thresholds: every value below is an untested assumption -------------------------
WINDOW_DAYS = 2                 # the recent window: the last 48 h
BASELINE_DAYS = 14              # rolling baseline: the 14 days before the recent window (Skubic 2015)
WARMUP_DAYS = 7                 # valid baseline days before any rule can score (Rispens 2015)
MIN_WEAR_HOURS = 6.0            # a day counts for sway only if worn this long
MIN_NIGHT_WEAR_MIN = 240.0      # a night counts only if worn this long (of 420 min)
# Heart-rate response to standing (research 1.2). Per stand-up: mean heart rate 60-180 s after
# standing minus the mean in the 60 s before (CDC STEADI 2017 takes BP and pulse at 1 and 3 min
# standing). A day counts with 3+ stand-ups that had heart rate: HeartCast only broadcasts during
# a Watch workout, so heart rate is "when available". The daily median of both of the last 2
# days at z <= -2.5 or >= +2.5 against the 14-day baseline, same direction (assumption, the same
# 2.5 SD as the night rule). The SD is never below 2 bpm: single readings are noisy (Falter 2019,
# PMC6444219: Apple Watch at seated rest, limits of agreement -20.7 to +28.0 bpm).
STAND_HR_Z = 2.5
STAND_HR_BANDS = ((4.0, 55), (2.5, 30))   # (smaller |z| of the 2 days, points)
MIN_STANDS_WITH_HR = 3
STAND_HR_SD_FLOOR_BPM = 2.0
# Night (research 1.4): each of the last 2 nights at z >= +2.5 against the 14-day baseline
# (Skubic 2015, PMC4848095: alerts at 2.5-4 SD; bed restlessness was the best-rated feature)
# AND at least +15% (Leung 2023, PMC10431933: >15% change vs own baseline) AND 15 min more.
NIGHT_Z = 2.5
NIGHT_MIN_REL = 0.15
NIGHT_MIN_CHANGE_MIN = 15.0     # kept from the first version: minutes, not only proportions
NIGHT_BANDS = ((4.0, 45), (2.5, 25))  # (smaller of the 2 nights' z, points): Skubic's 2.5-4 SD span
# A baseline SD is never taken below 5% of the baseline mean, so a very steady fortnight cannot
# turn a few minutes into a large z (assumption).
SD_FLOOR_FRAC = 0.05

THRESHOLDS = {
    STAND_HR: ("daily median heart-rate change after standing (mean 60-180 s after minus 60 s before) at "
               "z <= -2.5 or >= +2.5 against the 14-day baseline on both of the last 2 days, same direction: "
               "smaller |z| >= 2.5 -> 30, >= 4 -> 55 points; 3+ stands with heart rate per day; baseline SD at "
               "least 2 bpm; 7+ valid days in the 14-day baseline; off for non-sinus rhythm or a pacemaker"),
    NIGHT: ("both of the last 2 nights (2 nights in a row) at z >= +2.5 against the 14-day baseline and +15% "
            "and 15 min above it: smaller z >= 2.5 -> 25, >= 4 -> 45 points; baseline SD at least 5% of its mean; "
            "nights worn 4 h+; 7+ valid nights in the 14-day baseline"),
    "levels": ("worth a look this shift when one rule fires today; review this shift only when 2 different "
               "rules fired in the last 3 days, one of them today, or when one rule fires today and the record "
               "shows a medication start or change that raises the priority (see medication); the score never "
               "sets the level; not enough data when nothing fired and a rule could not score (heart rate missing "
               "on a worn device does not count: that rule is 'when available')"),
}
MAX_POINTS = {STAND_HR: STAND_HR_BANDS[0][1], NIGHT: NIGHT_BANDS[0][1]}
assert sum(MAX_POINTS.values()) == 100

LABELS = {
    SWAY: "Standing sway (experimental, not scored)",
    SETTLE_BALANCE: "Balance settling after standing up (experimental, not scored)",
    STAND_HR: "Heart-rate change after standing (when available)",
    NIGHT: "Movement at night",
}

# Suggested check for the care aide, per rule that scored.
AIDE_CHECKS = {
    STAND_HR: ("if they seem unsteady or dizzy, consider lying and standing vitals per your protocol "
               "(lying 5 min; blood pressure and pulse at 1 and 3 min standing)"),
    NIGHT: "note how they slept and any night-time waking on rounds tonight",
}


# Medication context (research 1.7): a start or change in the last 30 days of an antihypertensive
# or a medication class the AGS Beers Criteria 2023 lists raises the priority one level
# ("worth a look" -> "review"); it never starts an alert on its own. Synthetic record field.
MED_WINDOW_DAYS = 30
MED_CLASSES_RAISE = ("antihypertensive", "anticholinergic", "antipsychotic", "benzodiazepine", "corticosteroid",
                     "H2 blocker", "Z-drug", "opioid", "SSRI", "SNRI", "TCA", "antiepileptic")
MED_NOTE = "Medication context raises the priority one level; it never starts an alert on its own."
THRESHOLDS["medication"] = (
    f"a start or change in the last {MED_WINDOW_DAYS} days of a medication in one of these classes raises "
    f"'worth a look this shift' to 'review this shift' (never starts an alert on its own): "
    f"{', '.join(MED_CLASSES_RAISE)}; {ASSUMPTION}")
# The line every card carries (research 1.7). The research wording ends in a word the project's
# wording rules ban (BUILD_PLAN 7.3), so the card says "not a medical assessment" instead.
PATTERN_NOTE = "Pattern change only - not a medical assessment."

# The 0-100 number is a change score (research 1.8): how far this resident's patterns moved from
# their own usual. It ranks change only; nothing on the card looks ahead.
SCORE_LABEL = "change score (0-100)"
SCORE_NOTE = ("How far today's patterns moved from this resident's own usual: the sum of the rule points. "
              "It ranks change only; the level comes from which rules fired, not from this number.")

# Alert budget (research 1.6): at most 1 push per resident per 24 h; an identical alert (same level
# and rules) is muted for 3 days unless it gets worse (higher level or score); everything else goes
# into the shift-start digest. A snoozed resident (feedback button) goes to the digest too.
PUSH_BUDGET_HOURS = 24
MUTE_DAYS = 3
LEVEL_RANK = {LEVEL_LOW: 0, LEVEL_NO_DATA: 0, LEVEL_LOOK: 1, LEVEL_REVIEW: 2}

# Feedback buttons on every flagged card (research 1.7; Henry 2022 TREWS: confirm, dismiss or pause;
# TigerPlace staff rated each alert). "Snooze" mutes pushes for this resident for 3 days.
SNOOZE_DAYS = 3
FEEDBACK_ACTIONS = (
    {"id": "useful", "label": "Useful"},
    {"id": "not_useful", "label": "Not useful"},
    {"id": "already_known", "label": "Already known", "asks_reason": True},
    {"id": "snooze_3d", "label": "Snooze 3 days"},
)
FEEDBACK_IDS = tuple(a["id"] for a in FEEDBACK_ACTIONS)

EXPERIMENTAL_STATUS = "experimental, not scored"
# Why each experimental signal is on the card but outside the score. Source: research 1.1,
# Zhou 2017 (MOBILIZE Boston, PMC5462759); no study validates sway from a wrist or hair tie.
EXPERIMENTAL_NOTES = {
    SWAY: ("Shown for the care team only. Traditional sway measures were not linked to the outcome "
           "studied in 738 older adults (Zhou 2017), and sway from a wrist or hair tie is not validated."),
    SETTLE_BALANCE: ("Shown for the care team only. It is derived from sway, which is not validated from "
                     "a wrist or hair tie."),
}


@dataclass(frozen=True)
class DayMetrics:
    day: date
    wear_hours: float
    sway_rms: Optional[float]
    stand_ups: int
    settle_s: Optional[float]
    settle_sway_s: Optional[float]
    settle_hr_s: Optional[float]
    night_restless_min: Optional[float]
    night_wear_min: Optional[float]
    stand_hr_n: int = 0                          # stand-ups that had heart rate around them
    stand_hr_delta_bpm: Optional[float] = None   # median heart-rate change after standing that day
    synthetic: bool = True


# --- the rule engine (pure) -----------------------------------------------------------
def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _band(change: float, bands: Sequence[tuple[float, int]]) -> tuple[int, Optional[float]]:
    for cut, points in bands:
        if change >= cut:
            return points, cut
    return 0, None


def _sd(values: Sequence[float], mean: float) -> float:
    """Sample SD, floored at SD_FLOOR_FRAC of the mean."""
    raw = statistics.stdev(values) if len(values) > 1 else 0.0
    return max(raw, SD_FLOOR_FRAC * abs(mean))


SOURCES = {
    "budget": ("Ancker 2017 (PMC5387195: acceptance dropped about 30% with each repeat reminder); Joint Commission "
               "SEA 50 (2013, recommendation 4); Rantz 2017 (PMC5679074: a morning summary of the prior 24 h)"),
    "medication": ("Shimbo 2016 (15 days after starting an antihypertensive); Dave 2024 (JAMA Intern Med, "
                   "nursing-home residents, 30 days after starting an antihypertensive); AGS Beers 2023"),
    STAND_HR: ("CDC STEADI 2017 (BP and pulse at 1 and 3 min standing); Norcliffe-Kaufmann 2018 "
               "(PMC5867255: the blood-pressure criterion needs blood pressure); Falter 2019 (PMC6444219: "
               "watch heart-rate error at rest)"),
    NIGHT: "Skubic 2015 (PMC4848095: 14-day baseline, 2.5-4 SD); Leung 2023 (PMC10431933: >15%)",
}


def _pct(change: float) -> int:
    return int(round(change * 100))


def _rule(rule: str, unit: str, digits: int, recent: Sequence[float], baseline: Sequence[float]) -> dict:
    return {
        "rule": rule,
        "label": LABELS[rule],
        "observed": None,
        "baseline": None,
        "unit": unit,
        "contribution": 0,
        "max_contribution": MAX_POINTS[rule],
        "change_pct": None,
        "explanation": "",
        "status": "not enough data",
        "scored": False,
        "top_band": False,
        "recent_values": [round(v, digits) if digits else round(v) for v in recent],
        "baseline_values": [round(v, digits) if digits else round(v) for v in baseline],
        "baseline_days": len(baseline),
        "threshold": THRESHOLDS[rule],
        "threshold_status": ASSUMPTION,
    }


def _not_enough(out: dict, need: str) -> dict:
    out["explanation"] = f"Not enough data to compare ({need}); this rule did not score."
    return out


def _warming_up(out: dict, valid: int, what: str) -> dict:
    out["explanation"] = (f"Not enough data to compare (warm-up: {valid} of {WARMUP_DAYS} valid {what} in the "
                          f"14-day baseline); this rule did not score.")
    return out


def experimental_sway(recent: Sequence[Optional[DayMetrics]],
                      baseline: Sequence[Optional[DayMetrics]]) -> dict:
    """Standing sway, recent vs baseline, for display only: it never adds points (research 1.1)."""
    def worn(days):
        return [d.sway_rms for d in days if d is not None and d.sway_rms is not None
                and d.wear_hours >= MIN_WEAR_HOURS]

    r, b = worn(recent), worn(baseline)
    out = {"signal": SWAY, "label": LABELS[SWAY], "status": EXPERIMENTAL_STATUS, "unit": "m/s²",
           "observed": round(_mean(r), 3) if r else None, "baseline": round(_mean(b), 3) if b else None,
           "change_pct": None, "contribution": 0, "note": EXPERIMENTAL_NOTES[SWAY]}
    if r and b and _mean(b) > 0:
        out["change_pct"] = _pct(_mean(r) / _mean(b) - 1.0)
    return out


def experimental_balance(recent: Sequence[Optional[DayMetrics]],
                         baseline: Sequence[Optional[DayMetrics]]) -> dict:
    """Balance settling time after standing, recent vs baseline, for display only (research 1.2)."""
    def usable(days):
        return [d.settle_sway_s for d in days if d is not None and d.settle_sway_s is not None]

    r, b = usable(recent), usable(baseline)
    out = {"signal": SETTLE_BALANCE, "label": LABELS[SETTLE_BALANCE], "status": EXPERIMENTAL_STATUS, "unit": "s",
           "observed": round(_mean(r), 1) if r else None, "baseline": round(_mean(b), 1) if b else None,
           "change_pct": None, "contribution": 0, "note": EXPERIMENTAL_NOTES[SETTLE_BALANCE]}
    if r and b and _mean(b) > 0:
        out["change_pct"] = _pct(_mean(r) / _mean(b) - 1.0)
    return out


def rule_stand_hr(recent: Sequence[Optional[DayMetrics]], baseline: Sequence[Optional[DayMetrics]],
                  off_reason: Optional[str] = None) -> dict:
    """Heart-rate change after standing vs the resident's own 14-day usual (research 1.2)."""
    def valid(days):
        return [d for d in days if d is not None and d.stand_hr_delta_bpm is not None
                and d.stand_hr_n >= MIN_STANDS_WITH_HR]

    r, b = valid(recent), valid(baseline)
    out = _rule(STAND_HR, "bpm", 1, [d.stand_hr_delta_bpm for d in r], [d.stand_hr_delta_bpm for d in b])
    out.update(source=SOURCES[STAND_HR], baseline_sd=None, z=None,
               stands_with_hr={"recent": [d.stand_hr_n if d is not None else 0 for d in recent],
                               "baseline_days": len(b)})
    # Heart rate is "when available" (HeartCast streams it only during a Watch workout): with stand-ups
    # counted on the last 2 days, a rule short of heart rate means no heart rate, not a device left off.
    out["heart_rate_unavailable"] = all(d is not None and d.stand_ups >= MIN_STANDS_WITH_HR for d in recent)
    if off_reason:
        out.update(status="off", recent_values=[], baseline_values=[], baseline_days=0,
                   explanation=(f"Off for this resident: the synthetic record notes {_article(off_reason)} "
                                f"{off_reason}, so heart rate after standing is not compared."))
        return out
    if len(b) < WARMUP_DAYS:
        return _warming_up(out, len(b), "days")
    if len(r) < WINDOW_DAYS:
        return _not_enough(out, f"needs {MIN_STANDS_WITH_HR}+ stands with heart rate on each of the last "
                                f"{WINDOW_DAYS} days")
    vals = [d.stand_hr_delta_bpm for d in r]
    base = _mean([d.stand_hr_delta_bpm for d in b])
    raw_sd = statistics.stdev([d.stand_hr_delta_bpm for d in b])
    sd = max(raw_sd, STAND_HR_SD_FLOOR_BPM)
    zs = [(x - base) / sd for x in vals]
    lower = all(z <= -STAND_HR_Z for z in zs)
    higher = all(z >= STAND_HR_Z for z in zs)
    points, _ = _band(min(abs(z) for z in zs), STAND_HR_BANDS) if (lower or higher) else (0, None)
    obs = _mean(vals)
    out.update(observed=round(obs, 1), baseline=round(base, 1), baseline_sd=round(sd, 1),
               z=[round(z, 1) for z in zs], change_pct=_pct(obs / base - 1.0) if base else None,
               scored=True, status="scored", contribution=points, top_band=points == STAND_HR_BANDS[0][1])
    stands = " and ".join(str(d.stand_hr_n) for d in r)
    shown_z = " and ".join(f"{z:+.1f}" for z in zs)
    usual = f"their usual {base:+.0f} bpm over the 14-day baseline, SD {sd:.1f}"
    if points:
        way = "less" if lower else "more"
        out["explanation"] = (f"Heart rate rose {way} than usual after standing (avg {obs:+.1f} vs {usual}; "
                              f"z {shown_z}; {stands} stands with heart rate). Many things can cause this, "
                              f"including medications or watch fit; {points} points.")
    else:
        out["explanation"] = (f"Heart-rate change after standing over the last 2 days: "
                              f"{' and '.join(f'{x:+.1f}' for x in vals)} bpm vs {usual} (z {shown_z}); not "
                              f"{STAND_HR_Z:g} SD away on both days in one direction, no points.")
    return out


def rule_night(recent: Sequence[Optional[DayMetrics]], baseline: Sequence[Optional[DayMetrics]]) -> dict:
    def nights(days):
        return [d.night_restless_min for d in days if d is not None and d.night_restless_min is not None
                and (d.night_wear_min or 0) >= MIN_NIGHT_WEAR_MIN]

    r, b = nights(recent), nights(baseline)
    out = _rule(NIGHT, "min", 0, r, b)
    out.update(source=SOURCES[NIGHT], baseline_sd=None, z=None)
    if len(b) < WARMUP_DAYS:
        return _warming_up(out, len(b), "nights")
    if len(r) < WINDOW_DAYS or _mean(b) <= 0:
        return _not_enough(out, f"needs both of the last {WINDOW_DAYS} nights worn {MIN_NIGHT_WEAR_MIN / 60:g} h+")
    base = _mean(b)
    sd = _sd(b, base)
    obs = _mean(r)
    zs = [(x - base) / sd for x in r]

    def above(x: float, z: float) -> bool:
        return z >= NIGHT_Z and x / base - 1.0 >= NIGHT_MIN_REL and x - base >= NIGHT_MIN_CHANGE_MIN

    n_above = sum(1 for x, z in zip(r, zs) if above(x, z))
    points, cut = _band(min(zs), NIGHT_BANDS) if n_above == len(r) else (0, None)
    out.update(observed=round(obs), baseline=round(base), baseline_sd=round(sd, 1), z=[round(z, 1) for z in zs],
               change_pct=_pct(obs / base - 1.0), scored=True, status="scored", contribution=points,
               top_band=points == NIGHT_BANDS[0][1])
    shown = " and ".join(f"{x:.0f}" for x in r)
    shown_z = " and ".join(f"{z:+.1f}" for z in zs)
    usual = f"{base:.0f} min in their own 14-day baseline, SD {sd:.1f} min"
    if points:
        out["explanation"] = (f"More movement at night than usual on both of the last 2 nights ({shown} min vs "
                              f"{usual}; z {shown_z}); each night at least {NIGHT_Z:g} SD, "
                              f"+{_pct(NIGHT_MIN_REL)}% and {NIGHT_MIN_CHANGE_MIN:g} min above, {points} points.")
    elif n_above == 1:
        out["explanation"] = (f"One of the last 2 nights had more movement than usual ({shown} min vs {usual}). "
                              f"This rule needs 2 nights in a row, so no points.")
    else:
        out["explanation"] = (f"Movement at night over the last 2 nights: {shown} min vs {usual} (z {shown_z}); "
                              f"under {NIGHT_Z:g} SD, +{_pct(NIGHT_MIN_REL)}% and {NIGHT_MIN_CHANGE_MIN:g} min "
                              f"above on both nights, no points.")
    return out


def level_for(fired_today: Iterable[str], fired_span: Iterable[str]) -> str:
    """Level from which rules fired, never from the score: a single rule (even at its top band)
    is worth a look; review needs 2 different rules in the last LEVEL_SPAN_DAYS days."""
    today = set(fired_today)
    if not today:
        return LEVEL_LOW
    if len(today | set(fired_span)) >= 2:
        return LEVEL_REVIEW
    return LEVEL_LOOK


def suggested_check(level: str, scored_rules: Sequence[str], missing: Sequence[str] = ()) -> str:
    if level == LEVEL_NO_DATA:
        labels = ", ".join(LABELS[r].lower() for r in RULES if r in missing)
        return (f"Device data is missing for: {labels}. Check the device is being worn and charged; "
                "tell the nurse if the data stays missing.")
    if level == LEVEL_LOW or not scored_rules:
        return "No extra check suggested; continue routine care."
    steps = "; ".join(AIDE_CHECKS[r] for r in RULES if r in scored_rules)
    if level == LEVEL_REVIEW:
        return "Ask the nurse to review this resident this shift. " + steps[0].upper() + steps[1:] + \
            ". Tell the nurse what you see."
    return "Worth a look this shift: " + steps + ". Tell the nurse what you see."


def local_briefing(card: dict) -> str:
    """Care-team briefing from the finished card. Plain template; it never changes the card."""
    fired = [r for r in card["rules"] if r["contribution"] > 0]
    parts = [f"{card['display_name']}: {card['level']}, change score {card['score']}/100 as of {card['as_of']}."]
    if fired:
        parts += [r["explanation"] for r in fired]
    else:
        parts.append("No rule reached a scoring band against their own baseline.")
    missing = [r["label"].lower() for r in card["rules"] if r["status"] == "not enough data"]
    if missing:
        parts.append("Not enough data for: " + ", ".join(missing) + ".")
    parts += [r["explanation"] for r in card["rules"] if r["status"] == "off"]
    parts += context_sentences(card)
    parts.append("Suggested check: " + card["suggested_check"])
    parts.append("Synthetic data. " + PATTERN_NOTE)
    return " ".join(parts)


def context_sentences(card: dict) -> list[str]:
    """The data-quality line and the record context (and whether it raised the priority): part of every
    briefing, the template's or a model's wording of it (routes/care.py word_briefings)."""
    dq = card["data_quality"]
    gaps = f"{len(dq['gaps'])} day(s) without data" if dq["gaps"] else "no gaps"
    out = [f"Data: worn {dq['hours_worn_last_48h']:g} h in the last 48 h; heart rate "
           f"{dq['heart_rate']['label']}; {gaps}."]
    meds = card["record_context"]["medication_changes_30d"]
    if meds:
        text = "; ".join(_med_phrase(m) for m in meds)
        raised = " This raised the priority one level." if card["priority_raised_by"] else ""
        out.append(f"Record context (synthetic): {text}.{raised}")
    return out


def _article(word: str) -> str:
    initialism = len(word) > 1 and word[:2].isupper()
    return "an" if word[:1].lower() in "aeiou" or (initialism and word[0] in "FHLMNRSX") else "a"


def _med_phrase(m: dict) -> str:
    cls, change, ago = m.get("class", "medication"), m.get("change", "changed"), m.get("days_ago")
    when = f"{ago} days ago" if ago != 1 else "1 day ago"
    if change == "started":
        return f"started {_article(cls)} {cls} {when}"
    return f"{cls} {change} {when}"


def record_context(record: dict, as_of: date) -> dict:
    """Synthetic record context on the card: medication starts or changes in the last 30 days."""
    meds = []
    for m in record.get("med_changes") or []:
        try:
            ago = (as_of - date.fromisoformat(str(m["on"]))).days
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= ago <= MED_WINDOW_DAYS:
            meds.append({**m, "days_ago": ago})
    return {"synthetic": True, "medication_changes_30d": meds, "raises_priority": False, "note": MED_NOTE,
            "window_days": MED_WINDOW_DAYS, "threshold_status": ASSUMPTION, "source": SOURCES["medication"]}


def windows(as_of: date) -> tuple[list[date], list[date]]:
    """(recent, baseline) days: the last 48 h, and the 14 days just before them (rolling)."""
    recent = [as_of - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    first = recent[0] - timedelta(days=BASELINE_DAYS)
    baseline = [first + timedelta(days=i) for i in range(BASELINE_DAYS)]
    return recent, baseline


def _hr_availability(present: Sequence[DayMetrics], of_days: int) -> dict:
    n = sum(1 for d in present if d.stand_hr_n >= MIN_STANDS_WITH_HR and d.stand_hr_delta_bpm is not None)
    return {"available_days": n, "of_days": of_days,
            "label": f"available on {n} of {of_days} days" if n else "not available"}


@dataclass(frozen=True)
class Push:
    """A card that was pushed to the care team (care_pushes)."""
    as_of: date
    at: datetime
    signature: str
    level: str
    score: int


def signature(card: dict) -> str:
    """What makes two alerts "identical": the same level and the same rules in the 3-day span."""
    return f"{card['level']}|{','.join(sorted(card.get('fired_last_3_days') or {}))}"


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def delivery_for(card: dict, pushes: Sequence[Push], snoozed_until: Optional[datetime], now: datetime) -> dict:
    """push, digest or nothing for one card, given the resident's push history (research 1.6)."""
    level = card["level"]
    if level not in FLAGGED:
        if level == LEVEL_NO_DATA:
            return {"channel": "digest", "reason": "device data is missing; listed in the shift-start digest"}
        return {"channel": None, "reason": "not flagged"}
    as_of = date.fromisoformat(card["as_of"])
    if any(p.as_of == as_of for p in pushes):
        return {"channel": "push", "reason": "already pushed for this day"}
    if snoozed_until is not None and snoozed_until > now:
        return {"channel": "digest", "reason": f"snoozed until {_utc(snoozed_until)}"}
    recent = [p for p in pushes if p.at > now - timedelta(hours=PUSH_BUDGET_HOURS)]
    if recent:
        last = max(recent, key=lambda p: p.at)
        return {"channel": "digest",
                "reason": f"1 push per resident per 24 h; the last one was at {_utc(last.at)}"}
    sig = signature(card)
    same = [p for p in pushes if p.signature == sig and 0 < (as_of - p.as_of).days < MUTE_DAYS]
    if same:
        last = max(same, key=lambda p: p.as_of)
        if LEVEL_RANK[level] > LEVEL_RANK.get(last.level, 0) or card["score"] > last.score:
            return {"channel": "push", "reason": f"worse than the alert pushed on {last.as_of.isoformat()}"}
        return {"channel": "digest",
                "reason": f"same alert pushed on {last.as_of.isoformat()}; muted 3 days unless it gets worse"}
    return {"channel": "push", "reason": "first alert for this resident in 24 h"}


def _rules_on(by_day: dict, as_of: date, record: dict) -> list[dict]:
    """Every scored rule for one day, against that day's own recent and baseline windows."""
    recent_days, baseline_days = windows(as_of)
    recent = [by_day.get(d) for d in recent_days]
    baseline = [by_day.get(d) for d in baseline_days]
    return [rule_stand_hr(recent, baseline, record.get("hr_rule_off")), rule_night(recent, baseline)]


def score_resident(resident_id: str, display_name: str, days: Iterable[DayMetrics], as_of: date,
                   briefing: Optional[Callable[[dict], str]] = None, record: Optional[dict] = None) -> dict:
    """The alert card for one resident. Reads only that resident's metrics and record context
    (e.g. hr_rule_off), never the generator's script."""
    record = record or {}
    by_day = {d.day: d for d in days}
    recent_days, baseline_days = windows(as_of)
    recent = [by_day.get(d) for d in recent_days]
    baseline = [by_day.get(d) for d in baseline_days]
    rules = _rules_on(by_day, as_of, record)
    score = sum(r["contribution"] for r in rules)
    fired_today = [r["rule"] for r in rules if r["contribution"] > 0]
    fired_span: dict[str, list[str]] = {}
    for back in range(LEVEL_SPAN_DAYS - 1, -1, -1):
        day = as_of - timedelta(days=back)
        for r in (rules if back == 0 else _rules_on(by_day, day, record)):
            if r["contribution"] > 0:
                fired_span.setdefault(r["rule"], []).append(day.isoformat())
    level = level_for(fired_today, fired_span)
    context = record_context(record, as_of)
    raised_by = None
    if level == LEVEL_LOOK and any(m.get("class") in MED_CLASSES_RAISE for m in context["medication_changes_30d"]):
        level, raised_by = LEVEL_REVIEW, "medication context"
    context["raises_priority"] = raised_by is not None
    missing = [r["rule"] for r in rules if r["status"] == "not enough data" and not r.get("heart_rate_unavailable")]
    if level == LEVEL_LOW and missing:
        # No data is not "nothing changed": keep the resident on the list.
        level = LEVEL_NO_DATA
    present = [d for d in recent + baseline if d is not None]
    card = {
        "resident_id": resident_id,
        "display_name": display_name,
        "synthetic": True,
        "score": score,
        "score_label": SCORE_LABEL,
        "score_note": SCORE_NOTE,
        "level": level,
        "rules": rules,
        "experimental": [experimental_sway(recent, baseline), experimental_balance(recent, baseline)],
        "suggested_check": suggested_check(level, list(fired_span) if level == LEVEL_REVIEW else fired_today,
                                           missing),
        "fired_last_3_days": fired_span,
        "briefing": "",
        "as_of": as_of.isoformat(),
        "recent_window": {"from": recent_days[0].isoformat(), "to": recent_days[-1].isoformat(), "hours": 48},
        "baseline_window": {"from": baseline_days[0].isoformat(), "to": baseline_days[-1].isoformat(),
                            "days": BASELINE_DAYS,
                            "note": (f"rolling: their own {BASELINE_DAYS} days just before the last 48 h; "
                                     f"{WARMUP_DAYS} valid days needed before a rule can score (warm-up)")},
        "data_quality": {
            "days_present": len(present),
            "days_expected": WINDOW_DAYS + BASELINE_DAYS,
            "gaps": sorted(d.isoformat() for d in baseline_days + recent_days if d not in by_day),
            "hours_worn_last_48h": round(sum(d.wear_hours for d in recent if d is not None), 1),
            "wear_hours": round(sum(d.wear_hours for d in present), 1),
            "heart_rate": _hr_availability(present, WINDOW_DAYS + BASELINE_DAYS),
            "all_rules_scored": all(r["status"] != "not enough data" for r in rules),
        },
        "record_context": context,
        "actions": [dict(a) for a in FEEDBACK_ACTIONS] if level in FLAGGED else [],
        "feedback_url": f"/v1/care/alerts/{resident_id}/feedback" if level in FLAGGED else None,
        "delivery": None,                 # push / digest: decided when stored, from the push history
        "priority_raised_by": raised_by,
        "pattern_note": PATTERN_NOTE,
        "limits": ["Heart rate alone cannot show blood pressure.",
                   "A change from their usual is a reason to look, not a finding."],
        "method": METHOD,
        "thresholds": THRESHOLDS,
        "thresholds_status": ASSUMPTION,
        "notice": NOTICE,
    }
    card["briefing"] = (briefing or local_briefing)(card)
    return card


# --- the synthetic cohort ---------------------------------------------------------------
@dataclass(frozen=True)
class Script:
    name: str
    note: str
    expect_flag: bool
    moves: tuple[str, ...] = ()          # rules this script is written to move
    onset: int = 0                       # first changed day (0-based index); linear ramp from there
    sway_per_day: float = 0.0            # per day, as a fraction of the resident's usual
    night_onset: int = 0                 # first changed night (index of the day it ends on)
    night_per_day: float = 0.0
    hr_onset: int = 0                    # first day with the heart-rate-after-standing shift
    hr_shift_bpm: float = 0.0            # added to the usual heart-rate change after standing
    bad_night_day: Optional[int] = None  # one night at 3x usual movement, the next day a little tired
    high_usual: bool = False             # usual values far above the other residents', all steady
    hr_rule_off: Optional[str] = None    # synthetic record note that switches the heart-rate rule off


SCRIPTS: dict[str, Script] = {s.name: s for s in (
    Script("stable", "No scripted change; day-to-day variation only.", False),
    # Sway is experimental (research 1.1): these drifts are shown on the card and score nothing.
    Script("sway_drift", "Standing sway rises 5% of usual a day from day 22 (shown, not scored).", False,
           onset=21, sway_per_day=0.05),
    Script("sway_drift_steep", "Standing sway rises 7% of usual a day from day 21 (shown, not scored).", False,
           onset=20, sway_per_day=0.07),
    # Changes start after the warm-up, on the last 2 days: a rolling baseline (and its SD) absorbs a
    # change that has been going on for much of its 14 days (research 1.3).
    Script("hr_response_flatter", "Heart rate rises about 12 bpm less than usual after standing on the last "
           "2 days.", True, (STAND_HR,), hr_onset=26, hr_shift_bpm=-12.0),
    Script("hr_response_bigger", "Heart rate rises about 14 bpm more than usual after standing on the last "
           "2 days.", True, (STAND_HR,), hr_onset=26, hr_shift_bpm=14.0),
    Script("night_restless", "Night-time movement up on the last 2 nights (1.9x, then 2.8x usual).", True,
           (NIGHT,), night_onset=26, night_per_day=0.90),
    Script("clustered", "Sway rises gradually from day 22; on the last 2 days heart rate rises about 12 bpm "
           "less after standing and night-time movement is up.", True, (STAND_HR, NIGHT), onset=21,
           sway_per_day=0.05, hr_onset=26, hr_shift_bpm=-12.0, night_onset=26, night_per_day=0.85),
    Script("clustered_mild", "Sway rises gradually from day 22; night-time movement up on the last 2 nights.",
           True, (NIGHT,), onset=21, sway_per_day=0.045, night_onset=26, night_per_day=0.80),
    Script("hr_rule_off_pacemaker", "Synthetic record notes a pacemaker; heart rate after standing changes on "
           "the last 2 days, and the rule is off for them.", False, hr_onset=26, hr_shift_bpm=-12.0,
           hr_rule_off="pacemaker"),
    Script("one_off_bad_night", "One bad night before day 27 (3x usual movement), then back to usual.", False,
           bad_night_day=26),
    Script("one_off_bad_night_last", "One bad night before day 28 (3x usual movement).", False,
           bad_night_day=27),
    Script("high_usual", "Naturally high sway, slow balance settling, a big heart-rate rise on standing and "
           "restless nights; all steady.", False,
           high_usual=True),
)}

# Fixed assignment, mixed so the flagged residents are not grouped together.
COHORT: tuple[tuple[str, str], ...] = (
    ("R-01", "stable"), ("R-02", "sway_drift"), ("R-03", "stable"), ("R-04", "one_off_bad_night"),
    ("R-05", "hr_response_flatter"), ("R-06", "stable"), ("R-07", "high_usual"), ("R-08", "clustered"),
    ("R-09", "stable"), ("R-10", "night_restless"), ("R-11", "stable"), ("R-12", "sway_drift_steep"),
    ("R-13", "one_off_bad_night_last"), ("R-14", "stable"), ("R-15", "hr_response_bigger"),
    ("R-16", "high_usual"), ("R-17", "hr_rule_off_pacemaker"), ("R-18", "clustered_mild"), ("R-19", "stable"),
    ("R-20", "stable"),
)
assert len(COHORT) == RESIDENT_COUNT

# Synthetic record context (research 1.7): (class, change, days before the end of the record).
# R-10's night change is raised to "review this shift"; R-01's change alone alerts nothing;
# R-03's is older than 30 days and is not listed.
MED_CHANGES: dict[str, tuple[tuple[str, str, int], ...]] = {
    "R-10": (("antihypertensive", "started", 9),),
    "R-01": (("opioid", "dose changed", 12),),
    "R-03": (("antipsychotic", "started", 40),),
}


@dataclass(frozen=True)
class SyntheticResident:
    id: str
    display_name: str
    script: Script
    days: tuple[DayMetrics, ...]
    record: dict = None                  # synthetic record context the rules may read (not the script)


def _bounded(rng: random.Random, sd: float) -> float:
    """Gaussian noise bounded at 2.5 standard deviations."""
    return max(-2.5 * sd, min(2.5 * sd, rng.gauss(0.0, sd)))


def _jitter(rng: random.Random, sd: float) -> float:
    """Multiplicative day-to-day variation, bounded at 2.5 standard deviations."""
    return 1.0 + _bounded(rng, sd)


def default_end_day() -> date:
    """Yesterday (UTC): the last complete day."""
    return datetime.now(timezone.utc).date() - timedelta(days=1)


def generate_resident(resident_id: str, script: Script, seed: int, end_day: date,
                      n_days: int = RECORD_DAYS) -> SyntheticResident:
    rng = random.Random(f"stillhere-care:{seed}:{resident_id}")
    if script.high_usual:
        sway0, sway_settle0, hr_rise0 = rng.uniform(0.13, 0.15), rng.uniform(34, 40), rng.uniform(36, 42)
        night0 = rng.uniform(100, 120)
    else:
        sway0, sway_settle0, hr_rise0 = rng.uniform(0.05, 0.09), rng.uniform(14, 22), rng.uniform(8, 16)
        night0 = rng.uniform(40, 60)
    stand0, wear0 = rng.uniform(12, 18), rng.uniform(13.5, 15.5)
    hr_share = rng.uniform(0.75, 0.95)     # share of stand-ups with heart rate (watch workout running)
    night_sd = 0.05 if script.high_usual else 0.08   # "all steady": high-usual residents vary less
    start = end_day - timedelta(days=n_days - 1)
    rows = []
    for i in range(n_days):
        ramp = max(0, i - script.onset + 1)
        sway_m = 1.0 + script.sway_per_day * ramp
        night_m = 1.0 + script.night_per_day * max(0, i - script.night_onset + 1)
        hr_shift = script.hr_shift_bpm if (script.hr_shift_bpm and i >= script.hr_onset) else 0.0
        if script.bad_night_day == i:
            night_m *= 3.0
            sway_m *= 1.05
        n = max(0, round(stand0 * _jitter(rng, 0.12)))
        balance = [sway_settle0 * (1.0 + (sway_m - 1.0) / 2) * rng.lognormvariate(0.0, 0.12) for _ in range(n)]
        # Heart-rate change after each stand-up that had heart rate: the day's level plus per-stand noise.
        day_rise = hr_rise0 + hr_shift + _bounded(rng, 1.0)
        rises = [day_rise + _bounded(rng, 3.0) for _ in range(n) if rng.random() < hr_share]
        rows.append(DayMetrics(
            day=start + timedelta(days=i),
            wear_hours=round(min(24.0, max(0.0, wear0 + rng.gauss(0.0, 0.8))), 1),
            sway_rms=round(sway0 * sway_m * _jitter(rng, 0.035), 4),
            stand_ups=n,
            settle_s=None,
            settle_sway_s=round(statistics.median(balance), 1) if n else None,
            settle_hr_s=None,
            night_restless_min=round(night0 * night_m * _jitter(rng, night_sd), 1),
            night_wear_min=round(min(420.0, rng.gauss(405.0, 8.0)), 1),
            stand_hr_n=len(rises),
            stand_hr_delta_bpm=round(statistics.median(rises), 1) if rises else None,
        ))
    record = {"hr_rule_off": script.hr_rule_off,
              "med_changes": [{"class": c, "change": ch, "on": (end_day - timedelta(days=k)).isoformat()}
                              for c, ch, k in MED_CHANGES.get(resident_id, ())]}
    return SyntheticResident(resident_id, f"Synthetic resident {resident_id}", script, tuple(rows), record)


def generate_cohort(seed: int = DEFAULT_SEED, end_day: Optional[date] = None) -> list[SyntheticResident]:
    end_day = end_day or default_end_day()
    return [generate_resident(rid, SCRIPTS[name], seed, end_day) for rid, name in COHORT]


def score_cohort(cohort: Sequence[SyntheticResident], as_of: date) -> list[dict]:
    return [score_resident(r.id, r.display_name, r.days, as_of, record=r.record) for r in cohort]


# --- database ---------------------------------------------------------------------------
METRIC_COLUMNS = ("resident_id", "day", "synthetic", "wear_hours", "sway_rms", "stand_ups", "settle_s",
                  "settle_sway_s", "settle_hr_s", "night_restless_min", "night_wear_min", "stand_hr_n",
                  "stand_hr_delta_bpm")


def _day_from_row(row: dict) -> DayMetrics:
    return DayMetrics(day=row["day"], wear_hours=row["wear_hours"], sway_rms=row["sway_rms"],
                      stand_ups=row["stand_ups"], settle_s=row["settle_s"], settle_sway_s=row["settle_sway_s"],
                      settle_hr_s=row["settle_hr_s"], night_restless_min=row["night_restless_min"],
                      night_wear_min=row["night_wear_min"], stand_hr_n=row["stand_hr_n"],
                      stand_hr_delta_bpm=row["stand_hr_delta_bpm"], synthetic=row["synthetic"])


async def evaluate(conn, as_of: Optional[date] = None) -> list[dict]:
    """Score every resident from care_daily_metrics and upsert care_alerts. Never reads the script."""
    from psycopg.types.json import Jsonb

    if as_of is None:
        row = await (await conn.execute("SELECT max(day) AS d FROM care_daily_metrics")).fetchone()
        as_of = row["d"]
        if as_of is None:
            return []
    residents = await (await conn.execute(
        "SELECT id, display_name, hr_rule_off, med_changes FROM care_residents ORDER BY id")).fetchall()
    rows = await (await conn.execute(
        """SELECT * FROM care_daily_metrics WHERE day BETWEEN %s AND %s ORDER BY resident_id, day""",
        (as_of - timedelta(days=RECORD_DAYS - 1), as_of))).fetchall()
    by_resident: dict[str, list[DayMetrics]] = {}
    for row in rows:
        by_resident.setdefault(row["resident_id"], []).append(_day_from_row(row))
    cards = [score_resident(r["id"], r["display_name"], by_resident.get(r["id"], []), as_of,
                            record={"hr_rule_off": r["hr_rule_off"], "med_changes": r["med_changes"]})
             for r in residents]
    async with conn.transaction():
        now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
        history: dict[str, list[Push]] = {}
        for p in await (await conn.execute(
                "SELECT resident_id, as_of, pushed_at, level, score, signature FROM care_pushes")).fetchall():
            history.setdefault(p["resident_id"], []).append(
                Push(as_of=p["as_of"], at=p["pushed_at"], signature=p["signature"], level=p["level"], score=p["score"]))
        snoozed = {r["resident_id"]: r["until"] for r in await (await conn.execute(
            "SELECT resident_id, max(snooze_until) AS until FROM care_feedback GROUP BY resident_id")).fetchall()}
        for card in cards:
            rid = card["resident_id"]
            card["delivery"] = delivery_for(card, history.get(rid, []), snoozed.get(rid), now)
            await conn.execute(
                """INSERT INTO care_alerts (resident_id, as_of, score, level, card) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (resident_id, as_of) DO UPDATE
                   SET score = EXCLUDED.score, level = EXCLUDED.level, card = EXCLUDED.card, created_at = now()""",
                (rid, as_of, card["score"], card["level"], Jsonb(card)))
            if card["delivery"]["channel"] == "push":
                await conn.execute(
                    """INSERT INTO care_pushes (resident_id, as_of, level, score, signature)
                       VALUES (%s, %s, %s, %s, %s) ON CONFLICT (resident_id, as_of) DO NOTHING""",
                    (rid, as_of, card["level"], card["score"], signature(card)))
    return cards


async def seed_synthetic(conn, *, seed: int = DEFAULT_SEED, end_day: Optional[date] = None,
                         reset: bool = False) -> dict:
    """Replace the synthetic cohort's data (20 residents x 28 days), then score it. One transaction.

    The residents' push history and feedback (Snooze 3 days, Useful, ...) are kept, so seeding the next
    day carries the 24 h push budget, the 3-day mute and a snooze over as they would on a ward;
    reset=True starts the cohort over without them."""
    from psycopg.types.json import Jsonb

    end_day = end_day or default_end_day()
    cohort = generate_cohort(seed, end_day)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SEED_LOCK_ID,))
        if reset:
            await conn.execute("DELETE FROM care_residents")  # pushes and feedback go with them
        else:
            await conn.execute("DELETE FROM care_residents WHERE NOT id = ANY(%s)", ([r.id for r in cohort],))
            await conn.execute("DELETE FROM care_daily_metrics")
            await conn.execute("DELETE FROM care_alerts")
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO care_residents (id, display_name, synthetic, script, script_note, expect_flag,
                                              hr_rule_off, med_changes)
                   VALUES (%s, %s, true, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET display_name = EXCLUDED.display_name, script = EXCLUDED.script,
                          script_note = EXCLUDED.script_note, expect_flag = EXCLUDED.expect_flag,
                          hr_rule_off = EXCLUDED.hr_rule_off, med_changes = EXCLUDED.med_changes""",
                [(r.id, r.display_name, r.script.name, r.script.note, r.script.expect_flag,
                  r.record.get("hr_rule_off"), Jsonb(r.record.get("med_changes") or [])) for r in cohort])
            async with cur.copy(f"COPY care_daily_metrics ({', '.join(METRIC_COLUMNS)}) FROM STDIN") as copy:
                for r in cohort:
                    for d in r.days:
                        await copy.write_row((r.id, d.day, True, d.wear_hours, d.sway_rms, d.stand_ups, d.settle_s,
                                              d.settle_sway_s, d.settle_hr_s, d.night_restless_min,
                                              d.night_wear_min, d.stand_hr_n, d.stand_hr_delta_bpm))
        cards = await evaluate(conn, end_day)
    flagged = {c["resident_id"] for c in cards if c["level"] in FLAGGED}
    changes = [r.id for r in cohort if r.script.expect_flag]
    steady = [r.id for r in cohort if not r.script.expect_flag]
    return {
        "ok": True,
        "synthetic": True,
        "seed": seed,
        "as_of": end_day.isoformat(),
        "residents": len(cohort),
        "days": RECORD_DAYS,
        "rows": sum(len(r.days) for r in cohort),
        "levels": {lvl: sum(1 for c in cards if c["level"] == lvl) for lvl in LEVELS},
        "scripted_change": {"residents": len(changes), "flagged": sum(1 for i in changes if i in flagged)},
        "no_change_scripts": {"residents": len(steady), "flagged": sum(1 for i in steady if i in flagged)},
        "notice": NOTICE,
    }


async def list_residents(conn) -> dict:
    rows = await (await conn.execute(
        """SELECT r.id, r.display_name, r.synthetic, r.script, r.script_note, r.expect_flag,
                  a.as_of, a.score, a.level,
                  (SELECT count(*) FROM care_daily_metrics m WHERE m.resident_id = r.id) AS days
             FROM care_residents r
             LEFT JOIN LATERAL (SELECT as_of, score, level FROM care_alerts
                                 WHERE resident_id = r.id ORDER BY as_of DESC LIMIT 1) a ON true
            ORDER BY r.id""")).fetchall()
    out = {"synthetic": True, "notice": NOTICE, "as_of": None, "residents": []}
    for row in rows:
        out["residents"].append({
            "resident_id": row["id"],
            "display_name": row["display_name"],
            "synthetic": row["synthetic"],
            "days_of_data": row["days"],
            "as_of": row["as_of"].isoformat() if row["as_of"] else None,
            "score": row["score"],
            "level": row["level"],
            "script": {"name": row["script"], "note": row["script_note"],
                       "expected": "flag" if row["expect_flag"] else "no flag",
                       "read_by_rules": False},
        })
    dates = [r["as_of"] for r in out["residents"] if r["as_of"]]
    out["as_of"] = max(dates) if dates else None
    if not rows:
        out["hint"] = "POST /v1/demo/care-seed (X-Admin-Key) generates the synthetic cohort."
    return out


async def list_alerts(conn, include_low: bool = False) -> dict:
    q = """SELECT card FROM care_alerts WHERE as_of = (SELECT max(as_of) FROM care_alerts)"""
    if not include_low:
        q += " AND level <> 'low'"
    order = (" ORDER BY CASE level WHEN 'review this shift' THEN 0 WHEN 'worth a look this shift' THEN 1"
             " WHEN 'not enough data' THEN 2 ELSE 3 END, score DESC, resident_id")
    t0 = time.perf_counter()
    rows = await (await conn.execute(q + order)).fetchall()
    list_ms = (time.perf_counter() - t0) * 1000.0
    cards = [r["card"] for r in rows]
    for card in cards:
        await _with_series(conn, card)
    out = {"synthetic": True, "notice": NOTICE, "thresholds_status": ASSUMPTION,
           "as_of": cards[0]["as_of"] if cards else None, "alerts": cards,
           "timing": {"unit": "ms", "list_query_ms": round(list_ms, 2),
                      "note": ("server-side time of each database query: list_query_ms for the card list, each "
                               "card's query_ms for its own 28-day series")}}
    if not cards:
        latest = await (await conn.execute("SELECT max(as_of) AS d FROM care_alerts")).fetchone()
        if latest["d"] is None:
            out["hint"] = "POST /v1/demo/care-seed (X-Admin-Key) generates the synthetic cohort."
        else:
            out["as_of"] = latest["d"].isoformat()
    return out


SERIES_SQL = """SELECT day, night_restless_min, stand_hr_delta_bpm, wear_hours FROM care_daily_metrics
                 WHERE resident_id = %s AND day > %s::date - %s AND day <= %s::date ORDER BY day"""


async def _with_series(conn, card: dict) -> None:
    """Add the card's 28-day series (for the usual-vs-now sparkline) and the time its query took
    (research 4.1: query time in ms next to each card). Read-time fields; the stored card is unchanged."""
    as_of = date.fromisoformat(card["as_of"])
    t0 = time.perf_counter()
    rows = await (await conn.execute(SERIES_SQL, (card["resident_id"], as_of, RECORD_DAYS, as_of))).fetchall()
    card["query_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    card["series"] = {"days": [r["day"].isoformat() for r in rows],
                      "night_restless_min": [r["night_restless_min"] for r in rows],
                      "stand_hr_delta_bpm": [r["stand_hr_delta_bpm"] for r in rows],
                      "wear_hours": [r["wear_hours"] for r in rows]}


async def record_feedback(conn, resident_id: str, action: str, reason: Optional[str] = None) -> Optional[dict]:
    """Store one button press against the resident's latest card. None if there is no such resident."""
    if action not in FEEDBACK_IDS:
        raise ValueError(action)
    row = await (await conn.execute(
        """SELECT r.id, (SELECT max(as_of) FROM care_alerts a WHERE a.resident_id = r.id) AS as_of
             FROM care_residents r WHERE r.id = %s""", (resident_id,))).fetchone()
    if row is None or row["as_of"] is None:
        return None
    saved = await (await conn.execute(
        """INSERT INTO care_feedback (resident_id, as_of, action, reason, snooze_until)
           VALUES (%s, %s, %s, %s, CASE WHEN %s THEN now() + make_interval(days => %s) END)
           RETURNING as_of, action, snooze_until""",
        (resident_id, row["as_of"], action, (reason or None) if action == "already_known" else None,
         action == "snooze_3d", SNOOZE_DAYS))).fetchone()
    return {"ok": True, "synthetic": True, "resident_id": resident_id, "action": saved["action"],
            "as_of": saved["as_of"].isoformat(),
            "snooze_until": saved["snooze_until"].isoformat() if saved["snooze_until"] else None}


async def feedback_summary(conn) -> dict:
    rows = await (await conn.execute(
        "SELECT action, count(*) AS n FROM care_feedback GROUP BY action")).fetchall()
    counts = {a: 0 for a in FEEDBACK_IDS}
    counts.update({r["action"]: r["n"] for r in rows})
    return {"synthetic": True, "notice": NOTICE, "counts": counts}


async def list_digest(conn) -> dict:
    """The shift-start digest: flagged (and data-missing) cards of the latest day that were not pushed."""
    rows = await (await conn.execute(
        """SELECT card FROM care_alerts
            WHERE as_of = (SELECT max(as_of) FROM care_alerts) AND card -> 'delivery' ->> 'channel' = 'digest'
            ORDER BY CASE level WHEN 'review this shift' THEN 0 WHEN 'worth a look this shift' THEN 1 ELSE 2 END,
                     score DESC, resident_id""")).fetchall()
    latest = await (await conn.execute("SELECT max(as_of) AS d FROM care_alerts")).fetchone()
    items = [{"resident_id": c["resident_id"], "display_name": c["display_name"], "level": c["level"],
              "score": c["score"], "reason": c["delivery"]["reason"], "suggested_check": c["suggested_check"],
              "briefing": c["briefing"]} for c in (r["card"] for r in rows)]
    return {"synthetic": True, "notice": NOTICE, "as_of": latest["d"].isoformat() if latest["d"] else None,
            "budget": {"push_per_resident_hours": PUSH_BUDGET_HOURS, "identical_muted_days": MUTE_DAYS,
                       "source": SOURCES["budget"], "status": ASSUMPTION},
            "items": items}
