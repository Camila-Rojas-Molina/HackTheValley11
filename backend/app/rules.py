"""Sensor and heart-rate rules. Pure functions; no DB, no clock, no model.

Every rule returns either None or a PullForward. PullForward is the only output
type a rule has: it asks the wearer sooner (ARMED -> PROMPTED with a counted
challenge) and cannot escalate anything. Escalation only ever follows missed
check-in deadlines.

All thresholds are untested engineering assumptions (BUILD_PLAN §6.6, §9).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

LOW, MED, HIGH = "LOW", "MED", "HIGH"

REASON_HR = "hr_different_from_usual"
REASON_HR_LOST = "hr_signal_lost"
REASON_HEAD = "head_position_change"
REASON_FALL_LIKE = "fall_like_motion"
REASON_OFFBODY = "offbody"

REASONS = (REASON_HR, REASON_HR_LOST, REASON_HEAD, REASON_FALL_LIKE, REASON_OFFBODY)

# Wording shown to people for each reason ("... -> early check-in").
REASON_LABELS = {
    REASON_HR: "heart rate different from your usual → early check-in",
    REASON_HR_LOST: "heart rate signal lost → early check-in",
    REASON_HEAD: "head position change → early check-in",
    REASON_FALL_LIKE: "fall-like motion → early check-in",
    REASON_OFFBODY: "scrunchie off → early check-in",
}

HR_ABS_DELTA_BPM = 30.0  # assumption
HR_REL_DELTA = 0.35  # assumption
STILL_ENMO_MG = 45.0  # assumption: "still" below this 60 s mean ENMO
VIGOROUS_ENMO_MG = 100.0  # assumption: suppress after this (e.g. dancing)
VIGOROUS_SUPPRESS = timedelta(minutes=10)


@dataclass(frozen=True)
class PullForward:
    """The only thing a sensor rule can output."""
    reason: str
    level: str
    simulated: bool = False

    def __post_init__(self) -> None:
        if self.reason not in REASONS:
            raise ValueError(f"unknown reason {self.reason!r}")
        if self.level not in (LOW, MED, HIGH):
            raise ValueError(f"unknown level {self.level!r}")


@dataclass(frozen=True)
class HrRuleResult:
    deviating_since: Optional[datetime]
    pull_forward: Optional[PullForward]
    median_bpm: Optional[float] = None
    threshold_bpm: Optional[float] = None


def median_bpm(samples: Iterable[float]) -> Optional[float]:
    values = [float(s) for s in samples]
    return statistics.median(values) if values else None


def hr_threshold(baseline: float) -> float:
    return max(HR_ABS_DELTA_BPM, HR_REL_DELTA * baseline)


def hr_rule(*, now: datetime, median_60s: Optional[float], baseline: Optional[float],
            deviating_since: Optional[datetime], sustain_s: float, enmo60_mg: Optional[float],
            last_vigorous_at: Optional[datetime], simulated: bool = False) -> HrRuleResult:
    """Heart rate different from the wearer's own baseline for long enough, while still.

    Fires when |median - baseline| >= max(30 bpm, 35%) has held for `sustain_s`
    and the 60 s mean ENMO is below 45 mg; suppressed for 10 min after ENMO
    >= 100 mg. Unknown movement (no fresh heartbeat) never fires.
    """
    if baseline is None or median_60s is None:
        return HrRuleResult(None, None, median_60s)
    threshold = hr_threshold(baseline)
    if abs(median_60s - baseline) < threshold:
        return HrRuleResult(None, None, median_60s, threshold)
    since = deviating_since or now
    held = (now - since).total_seconds() >= sustain_s
    still = enmo60_mg is not None and enmo60_mg < STILL_ENMO_MG
    suppressed = last_vigorous_at is not None and now - last_vigorous_at < VIGOROUS_SUPPRESS
    pf = PullForward(REASON_HR, MED, simulated) if (held and still and not suppressed) else None
    return HrRuleResult(since, pf, median_60s, threshold)


def hr_lost_rule(*, now: datetime, armed: bool, last_sample_at: Optional[datetime],
                 lost_after_s: float, already_fired_for: Optional[datetime]) -> Optional[PullForward]:
    """One LOW pull-forward when heart rate stops arriving for > lost_after_s while armed.

    `already_fired_for` is the last_sample_at the rule last fired for, so it fires
    once per gap.
    """
    if not armed or last_sample_at is None:
        return None
    if (now - last_sample_at).total_seconds() <= lost_after_s:
        return None
    if already_fired_for is not None and already_fired_for >= last_sample_at:
        return None
    return PullForward(REASON_HR_LOST, LOW)


def head_position_rule(payload: Optional[dict] = None) -> PullForward:
    """The device reports a held head-position change (hair mode)."""
    return PullForward(REASON_HEAD, MED)


def fall_like_rule(payload: Optional[dict] = None) -> PullForward:
    return PullForward(REASON_FALL_LIKE, HIGH)


def offbody_rule(payload: Optional[dict] = None) -> PullForward:
    return PullForward(REASON_OFFBODY, MED)


def cooldown_ok(*, now: datetime, last_at: Optional[datetime], cooldown_s: float) -> bool:
    return last_at is None or (now - last_at).total_seconds() >= cooldown_s
