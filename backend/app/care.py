"""PointClickCare senior mode: synthetic residents, deterministic rules, explainable cards.

Separate from the KAZE check-in path: this module imports nothing from it, writes only
the care_* tables and sends no messages. Plain code decides every score and level; no
model takes part. The briefing text is written from the finished card afterwards.

Data. generate_cohort() makes 20 synthetic residents x 14 days. Every resident and every
row is labelled synthetic. Seven residents follow a scripted gradual change; the rest are
no-change scripts: stable residents, a one-off bad night, and residents whose usual values
are simply high. The script is ground truth for checking the rules; the rules never read it.

Rules. Each compares a resident with their own baseline, never with other residents:
  rise_to_settle_time   after standing up, the time until balance (accelerometer) and
                        heart rate have both settled; same two windows
  night_restlessness    minutes moving at night; both of the last 2 nights must be above
                        their usual, so one bad night scores nothing
Postural sway is shown on the card greyed out, "experimental, not scored" (research 1.1):
traditional sway measures were not associated with the outcome studied in 738 older adults
(Zhou 2017, PMC5462759), and sway measured at the wrist or in the hair is not validated.
The baseline is the first 48 h of the 14-day record. 48 h is short: day-to-day variation
in older adults is large, so a longer baseline would be steadier once more data exists.
The generator's day-to-day variation is a chosen demo value and the scripted changes are
large next to it; real data will be noisier, and how early a change shows up here comes
from the script, not from any study.

Card. score 0-100 is the sum of the rule contributions (55 + 45 at most). Level:
"check today" from 45 points or when any rule reaches its top band, "watch" from 15,
otherwise "low", or "not enough data" when a rule could not score (the data stopped). Every threshold here is an untested assumption (a demo parameter), not a
clinical cut-off, and the API says so on every card.
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional, Sequence

NOTICE = ("Hackathon prototype. Synthetic data only; no real residents. Rule-based change flags, "
          "not a medical device and not clinically validated. Thresholds are untested assumptions.")
ASSUMPTION = "untested assumption"
METHOD = "deterministic rules against the resident's own baseline; no model"

SWAY, SETTLE, NIGHT = "postural_sway_drift", "rise_to_settle_time", "night_restlessness"
RULES = (SETTLE, NIGHT)          # scored
EXPERIMENTAL = (SWAY,)           # shown greyed out, never scored (research 1.1)
LEVEL_LOW, LEVEL_WATCH, LEVEL_CHECK = "low", "watch", "check today"
LEVEL_NO_DATA = "not enough data"  # below "watch" with a rule unable to score: the data stopped
LEVELS = (LEVEL_LOW, LEVEL_WATCH, LEVEL_CHECK, LEVEL_NO_DATA)
FLAGGED = (LEVEL_WATCH, LEVEL_CHECK)

DEFAULT_SEED = 11
RESIDENT_COUNT = 20
RECORD_DAYS = 14
SEED_LOCK_ID = 7_202_612  # advisory lock: one cohort seed at a time

# --- thresholds: every value below is an untested assumption -------------------------
WINDOW_DAYS = 2                 # "last 48 h" and "own 48 h baseline"
MIN_WEAR_HOURS = 6.0            # a day counts for sway only if worn this long
MIN_STAND_UPS = 8               # stand-ups needed in each 48 h window
MIN_NIGHT_WEAR_MIN = 240.0      # a night counts only if worn this long (of 420 min)
# (change vs baseline, points), highest band first. Sway's 30 points went to the scored rules
# in proportion (40:30 -> 55:45) when sway left the score (research 1.1).
SETTLE_BANDS = ((0.60, 55), (0.40, 40), (0.25, 25))
SETTLE_MIN_CHANGE_S = 5.0       # and at least this many seconds slower
NIGHT_BANDS = ((1.00, 45), (0.50, 25))  # each of the last 2 nights at least this far above
NIGHT_MIN_CHANGE_MIN = 15.0     # and at least this many minutes above, each night
WATCH_FROM = 15
CHECK_TODAY_FROM = 45

THRESHOLDS = {
    SETTLE: "+25% -> 25, +40% -> 40, +60% -> 55 points, and at least 5 s slower; 8+ stand-ups per 48 h",
    NIGHT: "both of the last 2 nights +50% -> 25, +100% -> 45 points, and at least 15 min above; nights worn 4 h+",
    "levels": ("watch from 15 points; check today from 45 points or any rule in its top band; "
               "not enough data below 15 points when a rule could not score"),
}
MAX_POINTS = {SETTLE: SETTLE_BANDS[0][1], NIGHT: NIGHT_BANDS[0][1]}
assert sum(MAX_POINTS.values()) == 100

LABELS = {
    SWAY: "Standing sway (experimental, not scored)",
    SETTLE: "Settling after standing up",
    NIGHT: "Movement at night",
}

# Suggested check for the care aide, per rule that scored.
AIDE_CHECKS = {
    SETTLE: ("stay with them the first time they stand up after resting, give them time to steady, "
             "and note it if they say they feel dizzy"),
    NIGHT: "note how they slept and any night-time waking on rounds tonight",
}


EXPERIMENTAL_STATUS = "experimental, not scored"
# Why each experimental signal is on the card but outside the score. Source: research 1.1,
# Zhou 2017 (MOBILIZE Boston, PMC5462759); no study validates sway from a wrist or hair tie.
EXPERIMENTAL_NOTES = {
    SWAY: ("Shown for the care team only. Traditional sway measures were not linked to the outcome "
           "studied in 738 older adults (Zhou 2017), and sway from a wrist or hair tie is not validated."),
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
    synthetic: bool = True


# --- the rule engine (pure) -----------------------------------------------------------
def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _band(change: float, bands: Sequence[tuple[float, int]]) -> tuple[int, Optional[float]]:
    for cut, points in bands:
        if change >= cut:
            return points, cut
    return 0, None


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
        "scored": False,
        "top_band": False,
        "recent_values": [round(v, digits) if digits else round(v) for v in recent],
        "baseline_values": [round(v, digits) if digits else round(v) for v in baseline],
        "threshold": THRESHOLDS[rule],
        "threshold_status": ASSUMPTION,
    }


def _not_enough(out: dict, need: str) -> dict:
    out["explanation"] = f"Not enough data to compare ({need}); this rule did not score."
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


def rule_settle(recent: Sequence[Optional[DayMetrics]], baseline: Sequence[Optional[DayMetrics]]) -> dict:
    def usable(days):
        return [d for d in days if d is not None and d.settle_s is not None and d.stand_ups > 0]

    r, b = usable(recent), usable(baseline)
    out = _rule(SETTLE, "s", 1, [d.settle_s for d in r], [d.settle_s for d in b])
    n_r, n_b = sum(d.stand_ups for d in r), sum(d.stand_ups for d in b)
    out["stand_ups"] = {"recent": n_r, "baseline": n_b}
    if len(r) < WINDOW_DAYS or len(b) < WINDOW_DAYS or n_r < MIN_STAND_UPS or n_b < MIN_STAND_UPS:
        return _not_enough(out, f"needs stand-ups on both days and {MIN_STAND_UPS}+ stand-ups per 48 h window")
    obs, base = _mean([d.settle_s for d in r]), _mean([d.settle_s for d in b])
    if base <= 0:
        return _not_enough(out, "baseline is zero")
    parts = {}
    for key, attr in (("balance_s", "settle_sway_s"), ("heart_rate_s", "settle_hr_s")):
        rv = [getattr(d, attr) for d in r if getattr(d, attr) is not None]
        bv = [getattr(d, attr) for d in b if getattr(d, attr) is not None]
        parts[key] = {"observed": round(_mean(rv), 1) if rv else None,
                      "baseline": round(_mean(bv), 1) if bv else None}
    out["components"] = parts
    change, delta = obs / base - 1.0, obs - base
    points, cut = _band(change, SETTLE_BANDS)
    small = bool(points) and delta < SETTLE_MIN_CHANGE_S
    if small:
        points = 0
    out.update(observed=round(obs, 1), baseline=round(base, 1), change_pct=_pct(change), scored=True,
               contribution=points, top_band=points == SETTLE_BANDS[0][1])
    head = (f"After standing up, balance and heart rate took {obs:.0f} s to settle vs {base:.0f} s in their "
            f"own 48 h baseline ({_pct(change):+d}%; {n_r} and {n_b} stand-ups)")
    if points:
        out["explanation"] = f"{head}; at or above the +{_pct(cut)}% band, {points} points."
    elif small:
        out["explanation"] = f"{head}; under the {SETTLE_MIN_CHANGE_S:g} s minimum change, no points."
    else:
        out["explanation"] = f"{head}; below the +{_pct(SETTLE_BANDS[-1][0])}% band, no points."
    return out


def rule_night(recent: Sequence[Optional[DayMetrics]], baseline: Sequence[Optional[DayMetrics]]) -> dict:
    def nights(days):
        return [d.night_restless_min for d in days if d is not None and d.night_restless_min is not None
                and (d.night_wear_min or 0) >= MIN_NIGHT_WEAR_MIN]

    r, b = nights(recent), nights(baseline)
    out = _rule(NIGHT, "min", 0, r, b)
    if len(r) < WINDOW_DAYS or len(b) < WINDOW_DAYS or _mean(b) <= 0:
        return _not_enough(out, f"needs {WINDOW_DAYS} nights worn {MIN_NIGHT_WEAR_MIN / 60:g} h+ in each window")
    base = _mean(b)
    obs = _mean(r)

    def above(x: float) -> bool:
        return x / base - 1.0 >= NIGHT_BANDS[-1][0] and x - base >= NIGHT_MIN_CHANGE_MIN

    low = min(r)
    points, cut = _band(low / base - 1.0, NIGHT_BANDS) if low - base >= NIGHT_MIN_CHANGE_MIN else (0, None)
    out.update(observed=round(obs), baseline=round(base), change_pct=_pct(obs / base - 1.0), scored=True,
               contribution=points, top_band=points == NIGHT_BANDS[0][1])
    shown = " and ".join(f"{x:.0f}" for x in r)
    n_above = sum(1 for x in r if above(x))
    if points:
        out["explanation"] = (f"More movement at night than usual on both of the last 2 nights ({shown} min vs "
                              f"{base:.0f} min in their own baseline); each night at or above the "
                              f"+{_pct(cut)}% band, {points} points.")
    elif n_above == 1:
        out["explanation"] = (f"One of the last 2 nights had more movement than usual ({shown} min vs "
                              f"{base:.0f} min in their own baseline). This rule needs 2 nights in a row, "
                              f"so no points.")
    else:
        out["explanation"] = (f"Movement at night over the last 2 nights: {shown} min vs {base:.0f} min in "
                              f"their own baseline; no points.")
    return out


def level_for(score: int, top_band: bool) -> str:
    if score >= CHECK_TODAY_FROM or top_band:
        return LEVEL_CHECK
    if score >= WATCH_FROM:
        return LEVEL_WATCH
    return LEVEL_LOW


def suggested_check(level: str, scored_rules: Sequence[str], missing: Sequence[str] = ()) -> str:
    if level == LEVEL_NO_DATA:
        labels = ", ".join(LABELS[r].lower() for r in RULES if r in missing)
        return (f"Device data is missing for: {labels}. Check the device is being worn and charged; "
                "tell the nurse if the data stays missing.")
    if level == LEVEL_LOW or not scored_rules:
        return "No extra check suggested; continue routine care."
    steps = "; ".join(AIDE_CHECKS[r] for r in RULES if r in scored_rules)
    text = steps[0].upper() + steps[1:] + ". Tell the nurse what you see."
    if level == LEVEL_CHECK:
        return "Ask the nurse to review this resident today. " + text
    return text


def local_briefing(card: dict) -> str:
    """Care-team briefing from the finished card. Plain template; it never changes the card."""
    fired = [r for r in card["rules"] if r["contribution"] > 0]
    parts = [f"{card['display_name']}: {card['level']}, score {card['score']}/100 as of {card['as_of']}."]
    if fired:
        parts += [r["explanation"] for r in fired]
    else:
        parts.append("No rule reached a scoring band against their own baseline.")
    missing = [r["label"].lower() for r in card["rules"] if not r["scored"]]
    if missing:
        parts.append("Not enough data for: " + ", ".join(missing) + ".")
    parts.append("Suggested check: " + card["suggested_check"])
    parts.append("Rule-based flag on synthetic data; not a medical assessment.")
    return " ".join(parts)


def windows(as_of: date) -> tuple[list[date], list[date]]:
    """(recent, baseline) days: the last 48 h, and the first 48 h of the 14-day record."""
    recent = [as_of - timedelta(days=i) for i in range(WINDOW_DAYS - 1, -1, -1)]
    first = as_of - timedelta(days=RECORD_DAYS - 1)
    baseline = [first + timedelta(days=i) for i in range(WINDOW_DAYS)]
    return recent, baseline


def score_resident(resident_id: str, display_name: str, days: Iterable[DayMetrics], as_of: date,
                   briefing: Optional[Callable[[dict], str]] = None) -> dict:
    """The alert card for one resident. Reads only that resident's metrics: never the script."""
    by_day = {d.day: d for d in days}
    recent_days, baseline_days = windows(as_of)
    recent = [by_day.get(d) for d in recent_days]
    baseline = [by_day.get(d) for d in baseline_days]
    rules = [rule_settle(recent, baseline), rule_night(recent, baseline)]
    score = sum(r["contribution"] for r in rules)
    level = level_for(score, any(r["top_band"] for r in rules))
    missing = [r["rule"] for r in rules if not r["scored"]]
    if level == LEVEL_LOW and missing:
        # No data is not "nothing changed": keep the resident on the list.
        level = LEVEL_NO_DATA
    present = [d for d in recent + baseline if d is not None]
    card = {
        "resident_id": resident_id,
        "display_name": display_name,
        "synthetic": True,
        "score": score,
        "level": level,
        "rules": rules,
        "experimental": [experimental_sway(recent, baseline)],
        "suggested_check": suggested_check(level, [r["rule"] for r in rules if r["contribution"] > 0], missing),
        "briefing": "",
        "as_of": as_of.isoformat(),
        "recent_window": {"from": recent_days[0].isoformat(), "to": recent_days[-1].isoformat(), "hours": 48},
        "baseline_window": {"from": baseline_days[0].isoformat(), "to": baseline_days[-1].isoformat(),
                            "hours": 48, "note": "their own first 48 h in this 14-day record"},
        "data_quality": {
            "days_present": len(present),
            "days_expected": 2 * WINDOW_DAYS,
            "wear_hours": round(sum(d.wear_hours for d in present), 1),
            "all_rules_scored": all(r["scored"] for r in rules),
        },
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
    settle_per_day: float = 0.0
    night_onset: int = 0                 # first changed night (index of the day it ends on)
    night_per_day: float = 0.0
    bad_night_day: Optional[int] = None  # one night at 3x usual movement, the next day a little tired
    high_usual: bool = False             # usual values far above the other residents', all steady


SCRIPTS: dict[str, Script] = {s.name: s for s in (
    Script("stable", "No scripted change; day-to-day variation only.", False),
    # Sway is experimental (research 1.1): these drifts are shown on the card and score nothing.
    Script("sway_drift", "Standing sway rises 5% of usual a day from day 5 (shown, not scored).", False,
           onset=4, sway_per_day=0.05),
    Script("sway_drift_steep", "Standing sway rises 7% of usual a day from day 4 (shown, not scored).", False,
           onset=3, sway_per_day=0.07),
    Script("slower_settle", "Settling after standing up slows 6% of usual a day from day 6.", True, (SETTLE,),
           onset=5, settle_per_day=0.06),
    Script("slower_settle_steep", "Settling after standing up slows 8.5% of usual a day from day 5.", True,
           (SETTLE,), onset=4, settle_per_day=0.085),
    Script("night_restless", "Night-time movement rises 20% of usual a night from night 6.", True, (NIGHT,),
           night_onset=5, night_per_day=0.20),
    Script("clustered", "Sway and settling time rise gradually from day 5, night-time movement from night 6.",
           True, (SETTLE, NIGHT), onset=4, sway_per_day=0.05, settle_per_day=0.06,
           night_onset=5, night_per_day=0.18),
    Script("clustered_mild", "Sway rises gradually from day 5, night-time movement from night 6.", True,
           (NIGHT,), onset=4, sway_per_day=0.045, night_onset=5, night_per_day=0.15),
    Script("one_off_bad_night", "One bad night before day 13 (3x usual movement), then back to usual.", False,
           bad_night_day=12),
    Script("one_off_bad_night_last", "One bad night before day 14 (3x usual movement).", False,
           bad_night_day=13),
    Script("high_usual", "Naturally high sway, slow settling and restless nights; all steady.", False,
           high_usual=True),
)}

# Fixed assignment, mixed so the flagged residents are not grouped together.
COHORT: tuple[tuple[str, str], ...] = (
    ("R-01", "stable"), ("R-02", "sway_drift"), ("R-03", "stable"), ("R-04", "one_off_bad_night"),
    ("R-05", "slower_settle"), ("R-06", "stable"), ("R-07", "high_usual"), ("R-08", "clustered"),
    ("R-09", "stable"), ("R-10", "night_restless"), ("R-11", "stable"), ("R-12", "sway_drift_steep"),
    ("R-13", "one_off_bad_night_last"), ("R-14", "stable"), ("R-15", "slower_settle_steep"),
    ("R-16", "high_usual"), ("R-17", "stable"), ("R-18", "clustered_mild"), ("R-19", "stable"),
    ("R-20", "stable"),
)
assert len(COHORT) == RESIDENT_COUNT


@dataclass(frozen=True)
class SyntheticResident:
    id: str
    display_name: str
    script: Script
    days: tuple[DayMetrics, ...]


def _jitter(rng: random.Random, sd: float) -> float:
    """Multiplicative day-to-day variation, bounded at 2.5 standard deviations."""
    return 1.0 + max(-2.5 * sd, min(2.5 * sd, rng.gauss(0.0, sd)))


def default_end_day() -> date:
    """Yesterday (UTC): the last complete day."""
    return datetime.now(timezone.utc).date() - timedelta(days=1)


def generate_resident(resident_id: str, script: Script, seed: int, end_day: date,
                      n_days: int = RECORD_DAYS) -> SyntheticResident:
    rng = random.Random(f"stillhere-care:{seed}:{resident_id}")
    if script.high_usual:
        sway0, sway_settle0, hr_settle0 = rng.uniform(0.13, 0.15), rng.uniform(34, 40), rng.uniform(38, 46)
        night0 = rng.uniform(80, 100)
    else:
        sway0, sway_settle0, hr_settle0 = rng.uniform(0.05, 0.09), rng.uniform(14, 22), rng.uniform(16, 26)
        night0 = rng.uniform(25, 45)
    stand0, wear0 = rng.uniform(10, 18), rng.uniform(13.5, 15.5)
    start = end_day - timedelta(days=n_days - 1)
    rows = []
    for i in range(n_days):
        ramp = max(0, i - script.onset + 1)
        sway_m = 1.0 + script.sway_per_day * ramp
        settle_m = 1.0 + script.settle_per_day * ramp
        night_m = 1.0 + script.night_per_day * max(0, i - script.night_onset + 1)
        if script.bad_night_day == i:
            night_m *= 3.0
            sway_m *= 1.05
            settle_m *= 1.10
        n = max(0, round(stand0 * _jitter(rng, 0.12)))
        balance = [sway_settle0 * settle_m * rng.lognormvariate(0.0, 0.12) for _ in range(n)]
        heart = [hr_settle0 * settle_m * rng.lognormvariate(0.0, 0.15) for _ in range(n)]
        both = [max(a, h) for a, h in zip(balance, heart)]
        rows.append(DayMetrics(
            day=start + timedelta(days=i),
            wear_hours=round(min(24.0, max(0.0, wear0 + rng.gauss(0.0, 0.8))), 1),
            sway_rms=round(sway0 * sway_m * _jitter(rng, 0.035), 4),
            stand_ups=n,
            settle_s=round(statistics.median(both), 1) if n else None,
            settle_sway_s=round(statistics.median(balance), 1) if n else None,
            settle_hr_s=round(statistics.median(heart), 1) if n else None,
            night_restless_min=round(night0 * night_m * _jitter(rng, 0.08), 1),
            night_wear_min=round(min(420.0, rng.gauss(405.0, 8.0)), 1),
        ))
    return SyntheticResident(resident_id, f"Synthetic resident {resident_id}", script, tuple(rows))


def generate_cohort(seed: int = DEFAULT_SEED, end_day: Optional[date] = None) -> list[SyntheticResident]:
    end_day = end_day or default_end_day()
    return [generate_resident(rid, SCRIPTS[name], seed, end_day) for rid, name in COHORT]


def score_cohort(cohort: Sequence[SyntheticResident], as_of: date) -> list[dict]:
    return [score_resident(r.id, r.display_name, r.days, as_of) for r in cohort]


# --- database ---------------------------------------------------------------------------
METRIC_COLUMNS = ("resident_id", "day", "synthetic", "wear_hours", "sway_rms", "stand_ups", "settle_s",
                  "settle_sway_s", "settle_hr_s", "night_restless_min", "night_wear_min")


def _day_from_row(row: dict) -> DayMetrics:
    return DayMetrics(day=row["day"], wear_hours=row["wear_hours"], sway_rms=row["sway_rms"],
                      stand_ups=row["stand_ups"], settle_s=row["settle_s"], settle_sway_s=row["settle_sway_s"],
                      settle_hr_s=row["settle_hr_s"], night_restless_min=row["night_restless_min"],
                      night_wear_min=row["night_wear_min"], synthetic=row["synthetic"])


async def evaluate(conn, as_of: Optional[date] = None) -> list[dict]:
    """Score every resident from care_daily_metrics and upsert care_alerts. Never reads the script."""
    from psycopg.types.json import Jsonb

    if as_of is None:
        row = await (await conn.execute("SELECT max(day) AS d FROM care_daily_metrics")).fetchone()
        as_of = row["d"]
        if as_of is None:
            return []
    residents = await (await conn.execute(
        "SELECT id, display_name FROM care_residents ORDER BY id")).fetchall()
    rows = await (await conn.execute(
        """SELECT * FROM care_daily_metrics WHERE day BETWEEN %s AND %s ORDER BY resident_id, day""",
        (as_of - timedelta(days=RECORD_DAYS - 1), as_of))).fetchall()
    by_resident: dict[str, list[DayMetrics]] = {}
    for row in rows:
        by_resident.setdefault(row["resident_id"], []).append(_day_from_row(row))
    cards = [score_resident(r["id"], r["display_name"], by_resident.get(r["id"], []), as_of) for r in residents]
    async with conn.transaction():
        for card in cards:
            await conn.execute(
                """INSERT INTO care_alerts (resident_id, as_of, score, level, card) VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (resident_id, as_of) DO UPDATE
                   SET score = EXCLUDED.score, level = EXCLUDED.level, card = EXCLUDED.card, created_at = now()""",
                (card["resident_id"], as_of, card["score"], card["level"], Jsonb(card)))
    return cards


async def seed_synthetic(conn, *, seed: int = DEFAULT_SEED, end_day: Optional[date] = None) -> dict:
    """Replace the synthetic cohort (20 residents x 14 days), then score it. One transaction."""
    end_day = end_day or default_end_day()
    cohort = generate_cohort(seed, end_day)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (SEED_LOCK_ID,))
        await conn.execute("DELETE FROM care_residents")
        async with conn.cursor() as cur:
            await cur.executemany(
                """INSERT INTO care_residents (id, display_name, synthetic, script, script_note, expect_flag)
                   VALUES (%s, %s, true, %s, %s, %s)""",
                [(r.id, r.display_name, r.script.name, r.script.note, r.script.expect_flag) for r in cohort])
            async with cur.copy(f"COPY care_daily_metrics ({', '.join(METRIC_COLUMNS)}) FROM STDIN") as copy:
                for r in cohort:
                    for d in r.days:
                        await copy.write_row((r.id, d.day, True, d.wear_hours, d.sway_rms, d.stand_ups, d.settle_s,
                                              d.settle_sway_s, d.settle_hr_s, d.night_restless_min,
                                              d.night_wear_min))
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
    order = (" ORDER BY CASE level WHEN 'check today' THEN 0 WHEN 'watch' THEN 1 WHEN 'not enough data' THEN 2"
             " ELSE 3 END, score DESC, resident_id")
    rows = await (await conn.execute(q + order)).fetchall()
    cards = [r["card"] for r in rows]
    out = {"synthetic": True, "notice": NOTICE, "thresholds_status": ASSUMPTION,
           "as_of": cards[0]["as_of"] if cards else None, "alerts": cards}
    if not cards:
        latest = await (await conn.execute("SELECT max(as_of) AS d FROM care_alerts")).fetchone()
        if latest["d"] is None:
            out["hint"] = "POST /v1/demo/care-seed (X-Admin-Key) generates the synthetic cohort."
        else:
            out["as_of"] = latest["d"].isoformat()
    return out
