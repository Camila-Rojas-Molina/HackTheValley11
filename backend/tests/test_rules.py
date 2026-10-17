"""Unit tests for the sensor / heart-rate rules (output is PULL_FORWARD only)."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app import rules
from app.rules import PullForward

NOW = datetime(2026, 10, 17, 21, 0, tzinfo=timezone.utc)


def hr(**kw):
    base = dict(now=NOW, median_60s=118.0, baseline=72.0, deviating_since=NOW - timedelta(seconds=20),
                sustain_s=15, enmo60_mg=10.0, last_vigorous_at=None)
    base.update(kw)
    return rules.hr_rule(**base)


def test_threshold_is_max_of_30bpm_and_35pct():
    assert rules.hr_threshold(72) == 30
    assert rules.hr_threshold(100) == 35


def test_hr_rule_fires_only_when_sustained_and_still():
    assert hr().pull_forward == PullForward(rules.REASON_HR, rules.MED)
    assert hr(deviating_since=None).pull_forward is None  # just started deviating
    assert hr(deviating_since=None).deviating_since == NOW
    assert hr(enmo60_mg=60.0).pull_forward is None  # moving
    assert hr(enmo60_mg=None).pull_forward is None  # movement unknown -> never fires
    assert hr(last_vigorous_at=NOW - timedelta(minutes=5)).pull_forward is None  # recovering after dancing
    assert hr(last_vigorous_at=NOW - timedelta(minutes=11)).pull_forward is not None
    assert hr(median_60s=95.0).pull_forward is None and hr(median_60s=95.0).deviating_since is None
    assert hr(median_60s=40.0).pull_forward is not None  # lower than usual counts too
    assert hr(baseline=None).pull_forward is None


def test_hr_lost_fires_once_per_gap():
    last = NOW - timedelta(seconds=130)
    pf = rules.hr_lost_rule(now=NOW, armed=True, last_sample_at=last, lost_after_s=120, already_fired_for=None)
    assert pf == PullForward(rules.REASON_HR_LOST, rules.LOW)
    assert rules.hr_lost_rule(now=NOW, armed=True, last_sample_at=last, lost_after_s=120, already_fired_for=last) is None
    assert rules.hr_lost_rule(now=NOW, armed=False, last_sample_at=last, lost_after_s=120, already_fired_for=None) is None
    assert rules.hr_lost_rule(now=NOW, armed=True, last_sample_at=NOW, lost_after_s=120, already_fired_for=None) is None


def test_every_rule_returns_only_pull_forward_or_none():
    outputs = [rules.head_position_rule(), rules.fall_like_rule(), rules.offbody_rule(), hr().pull_forward,
               rules.hr_lost_rule(now=NOW, armed=True, last_sample_at=NOW - timedelta(hours=1), lost_after_s=120,
                                  already_fired_for=None)]
    assert all(isinstance(o, PullForward) for o in outputs)
    # The rule module has no way to name an escalation state or event.
    src = inspect.getsource(rules)
    for forbidden in ("ESCALATED", "ACK_TIMEOUT", "REMINDER_TIMEOUT", "states."):
        assert forbidden not in src


def test_pull_forward_rejects_unknown_reasons_and_levels():
    with pytest.raises(ValueError):
        PullForward("escalate", rules.HIGH)
    with pytest.raises(ValueError):
        PullForward(rules.REASON_HR, "CRITICAL")


def test_cooldown():
    assert rules.cooldown_ok(now=NOW, last_at=None, cooldown_s=900)
    assert not rules.cooldown_ok(now=NOW, last_at=NOW - timedelta(minutes=5), cooldown_s=900)
    assert rules.cooldown_ok(now=NOW, last_at=NOW - timedelta(minutes=16), cooldown_s=900)
