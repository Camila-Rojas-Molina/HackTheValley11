"""Unit tests for the pure KAZE state machine (no database)."""
from __future__ import annotations

import itertools
import random
from dataclasses import replace

import pytest

from app import messages, states
from app.config import PROFILES
from app.states import (ACKED, ARMED, DURESS, ENDED, FINAL, LOST_CONTACT, NO_RESPONSE, PROMPTED, REMINDED,
                        AlertView, Event, Snapshot, decide, is_alerting, level_state)

T = PROFILES["demo"]
C1, C2 = 101, 102


def first(options):
    return list(options)[0]


def snap(state=ARMED, **kw) -> Snapshot:
    base = dict(state=state, public_state=kw.pop("public_state", state), version=3, timings=T, contacts=(C1, C2))
    base.update(kw)
    return Snapshot(**base)


def alert(kind=NO_RESPONSE, level=1, **kw) -> AlertView:
    return AlertView(kind=kind, level=level, recipients=kw.pop("recipients", tuple([C1, C2][:level])), **kw)


def alerting_snaps():
    """Every escalation-shaped snapshot, masked and unmasked."""
    out = []
    for kind, masked in ((NO_RESPONSE, False), (DURESS, True), (DURESS, False)):
        pub = ARMED if masked else None
        for st, lvl, final, acked in ((level_state(1), 1, False, None), (level_state(2), 2, False, None),
                                      (FINAL, 3, True, None), (ACKED, 1, False, C1), (ACKED, 3, True, C2)):
            a = AlertView(kind=kind, level=lvl, is_final=final, masked=masked,
                          status="ACKED" if acked else "OPEN", acked_by=acked, recipients=(C1, C2)[:max(1, min(lvl, 2))])
            out.append(snap(st, public_state=pub or st, alert=a))
    return out


def pre_alert_snaps():
    out = []
    for st, n, cause in ((ARMED, None, None), (PROMPTED, 2, "routine"), (PROMPTED, 3, "challenge"), (REMINDED, 4, "challenge")):
        out.append(snap(st, challenge_n=n, prompt_cause=cause))
        out.append(snap(st, challenge_n=n, prompt_cause=cause, link_state=states.LINK_LOST,
                        alert=AlertView(kind=LOST_CONTACT, level=1, recipients=(C1,)), lost_contact_told=True))
    return out


ALL_SNAPS = pre_alert_snaps() + alerting_snaps() + [snap(ENDED)]


def sample_events():
    evs = [Event(k) for k in states.ALL_EVENTS if k not in (states.SQUEEZE, states.PULL_FORWARD) and not k.startswith("CONTACT_")]
    for pattern, k in itertools.product(states.SQUEEZE_PATTERNS, (1, 2, 3, 4, None)):
        evs.append(Event(states.SQUEEZE, pattern=pattern, k=k))
    for kind in (states.CONTACT_ACK, states.CONTACT_PASS, states.CONTACT_SAFE, states.CONTACT_CANT_REACH):
        for c in (C1, C2, 999):
            evs.append(Event(kind, contact_id=c))
            evs.append(Event(kind, contact_id=c, twin=True))  # on the twin's alert (masked duress)
    for reason in ("hr_different_from_usual", "hr_signal_lost", "head_position_change", "fall_like_motion", "offbody"):
        evs.append(Event(states.PULL_FORWARD, reason=reason, level="MED"))
    return evs


# --- happy paths -------------------------------------------------------------
def test_routine_checkin_ok():
    d = decide(snap(ARMED), Event(states.CHECKIN_DUE), first)
    assert d.to_state == PROMPTED and d.challenge_n == 2 and d.prompt_cause == "routine"
    assert d.wearer_templates == ["reminder"]
    t = d.timers[0]
    assert t.kind == states.PROMPT_TIMEOUT and t.prompt_window_s == 12 and t.delay_s == 12 + 30
    d2 = decide(snap(PROMPTED, challenge_n=2, prompt_cause="routine"), Event(states.SQUEEZE, pattern="plain", k=2))
    assert d2.to_state == ARMED and d2.reply == "noted" and d2.wearer_templates == ["noted"]
    assert [t.kind for t in d2.timers] == [states.CHECKIN_DUE]


def test_no_response_ladder_to_final_is_simulated():
    s = snap(ARMED)
    path = []
    for ev in (states.CHECKIN_DUE, states.PROMPT_TIMEOUT, states.REMINDER_TIMEOUT, states.ACK_TIMEOUT, states.ACK_TIMEOUT):
        d = decide(s, Event(ev), first)
        s = step(s, d)
        path.append(d.to_state)
        last = d
    assert path == [PROMPTED, REMINDED, "ESCALATED_L1", "ESCALATED_L2", FINAL]
    assert any(e.kind == "emergency_services_simulated" and e.detail["placed"] is False for e in last.audit)
    assert {n.contact_id for n in last.notify} == {C1, C2}
    assert all(n.template == "alert_final" for n in last.notify)
    assert "SIMULATED" in messages.render("alert_final", {"name": "Maya"})
    # FINAL has no ack timeout; only capped renotify.
    assert [t.kind for t in last.timers] == [states.RENOTIFY]
    assert decide(s, Event(states.ACK_TIMEOUT)).reply == "ignored"


def test_l1_notifies_priority_one_only_and_releases_location():
    s = snap(REMINDED, challenge_n=3, prompt_cause="challenge")
    d = decide(s, Event(states.REMINDER_TIMEOUT))
    assert d.to_state == "ESCALATED_L1"
    assert [(n.to, n.contact_id, n.template) for n in d.notify] == [("contact", C1, "alert_no_response")]
    assert d.location == "release"
    assert any(t.kind == states.ACK_TIMEOUT and t.delay_s == T.ack_s for t in d.timers)


def test_ack_followup_timeout_moves_on_and_repings_acker():
    a = alert(level=1, recipients=(C1,), status="ACKED", acked_by=C1)
    d = decide(snap(ACKED, alert=a), Event(states.FOLLOWUP_TIMEOUT))
    assert d.to_state == "ESCALATED_L2"
    targets = [n.contact_id for n in d.notify]
    assert C2 in targets and C1 in targets


def test_ack_alone_never_closes_and_only_acker_can_confirm_safe():
    s = snap("ESCALATED_L1", alert=alert(level=1, recipients=(C1,)))
    d = decide(s, Event(states.CONTACT_ACK, contact_id=C1))
    assert d.to_state == ACKED and not any(op.op == "close" for op in d.alert_ops)
    s2 = step(s, d)
    assert decide(s2, Event(states.CONTACT_SAFE, contact_id=C2)).reply == "ignored"
    assert decide(s, Event(states.CONTACT_SAFE, contact_id=C1)).reply == "ignored"  # safe before ack
    d3 = decide(s2, Event(states.CONTACT_SAFE, contact_id=C1))
    assert d3.through == states.RESOLVED and d3.to_state == ARMED and d3.location == "expire_later"


def test_wrong_count_gets_one_retry_with_new_n_and_same_deadline():
    s = snap(PROMPTED, challenge_n=3, prompt_cause="challenge")
    d = decide(s, Event(states.SQUEEZE, pattern="plain", k=2), first)
    assert d.to_state == PROMPTED and d.reply == "retry"
    assert d.challenge_n != 3 and d.challenge_n in states.CHALLENGE_CHOICES
    assert not d.changed and not d.timers and not d.cancel_state_timers  # deadline unchanged
    s2 = step(s, d)
    d2 = decide(s2, Event(states.SQUEEZE, pattern="plain", k=9))
    assert d2.challenge_n == s2.challenge_n and not d2.notify  # no second retry


# --- KAZE invariants ------------------------------------------------------------
@pytest.mark.parametrize("s", ALL_SNAPS, ids=lambda s: f"{s.state}/{s.alert.kind if s.alert else '-'}")
def test_sensor_input_only_ever_pulls_forward(s):
    for reason, level in itertools.product(
            ("hr_different_from_usual", "hr_signal_lost", "head_position_change", "fall_like_motion", "offbody"),
            ("LOW", "MED", "HIGH")):
        d = decide(s, Event(states.PULL_FORWARD, reason=reason, level=level), first)
        assert not d.alert_ops
        assert not (is_alerting(d.to_state) and not is_alerting(s.state))
        assert d.to_state in (s.state, PROMPTED)
        if s.state == ARMED:
            assert d.to_state == PROMPTED and d.prompt_cause == "challenge"
            assert d.challenge_n in states.CHALLENGE_CHOICES
        else:
            assert d.to_state == s.state


@pytest.mark.parametrize("s", ALL_SNAPS, ids=lambda s: f"{s.state}/{s.alert.kind if s.alert else '-'}")
def test_only_safe_or_real_pin_resolves(s):
    for ev in sample_events():
        d = decide(s, ev, first)
        closes = [op for op in d.alert_ops if op.op == "close"]
        for op in closes:
            if op.status in (states.RESOLVED, states.FALSE_ALARM):
                assert ev.kind in (states.CONTACT_SAFE, states.PIN_REAL, states.END_REAL)
            else:  # the only other close: a lost-contact notice when the link comes back
                assert op.status == states.ALERT_RESTORED and s.alert.kind == LOST_CONTACT
                assert ev.kind == states.LINK_RESTORED
        if is_alerting(s.state) and not is_alerting(d.to_state):
            assert ev.kind in (states.CONTACT_SAFE, states.PIN_REAL, states.END_REAL)


@pytest.mark.parametrize("s", alerting_snaps(), ids=lambda s: f"{s.state}/{s.alert.kind}/{s.alert.masked}")
def test_late_squeeze_never_stands_down(s):
    for pattern, k, late in itertools.product(states.SQUEEZE_PATTERNS, (1, 2, 3, 4), (False, True)):
        d = decide(s, Event(states.SQUEEZE, pattern=pattern, k=k, late=late))
        assert d.to_state == s.state
        assert not any(op.op == "close" for op in d.alert_ops)
        assert not d.cancel_state_timers and not d.timers
        if pattern != "duress":
            assert all(n.template == "late_device" for n in d.notify if n.to == "contact")
    d = decide(s, Event(states.WEARER_OK))
    assert d.to_state == s.state and not any(op.op == "close" for op in d.alert_ops)


def _visible(d):
    return [(e.kind, e.detail, e.from_state or d.from_state, e.to_state or d.to_state)
            for e in d.audit if not e.masked]


def _wearer_view(d):
    cmd = states.device_cmd(d.public_state, d.challenge_n, "cid", d.prompt_cause)
    texts = [messages.render(n.template, {"name": "Maya", **n.params}) for n in d.notify if n.to == "wearer"]
    return d.reply, d.public_state, cmd, texts, _visible(d)


@pytest.mark.parametrize("s", ALL_SNAPS, ids=lambda s: f"{s.state}/{s.alert.kind if s.alert else '-'}")
def test_duress_is_indistinguishable_to_the_wearer(s):
    pairs = [(Event(states.PIN_REAL), Event(states.PIN_DURESS)), (Event(states.END_REAL), Event(states.END_DURESS))]
    if s.state in states.PROMPT_STATES:
        pairs.append((Event(states.SQUEEZE, pattern="plain", k=s.challenge_n), Event(states.SQUEEZE, pattern="duress", k=3)))
    elif s.state == ARMED or is_alerting(s.state):
        pairs.append((Event(states.SQUEEZE, pattern="plain", k=2), Event(states.SQUEEZE, pattern="duress", k=3)))
    for ok_ev, duress_ev in pairs:
        ok, du = decide(s, ok_ev, first), decide(s, duress_ev, first)
        assert _wearer_view(ok) == _wearer_view(du), (s.state, ok_ev.kind)


@pytest.mark.parametrize("s", pre_alert_snaps(), ids=lambda s: f"{s.state}/{s.alert.kind if s.alert else '-'}")
def test_duress_opens_masked_alert_at_l1(s):
    for ev in (Event(states.SQUEEZE, pattern="duress", k=3), Event(states.PIN_DURESS)):
        d = decide(s, ev, first)
        assert d.to_state == "ESCALATED_L1"
        assert d.public_state == ARMED
        assert d.alert_masked_after and d.alert_kind_after == DURESS
        assert [n.template for n in d.notify if n.to == "contact" and not n.silent] == ["alert_duress"]
        assert any(t.kind == states.ACK_TIMEOUT and t.delay_s == T.duress_ack_s for t in d.timers)
        assert all(e.masked for e in d.audit if e.kind in ("duress", "escalated"))


@pytest.mark.parametrize("st", [level_state(1), ACKED, FINAL])
def test_a_duress_squeeze_during_an_alert_masks_it_and_the_twin_keeps_the_alert_she_saw(st):
    a = alert(level=1, status="ACKED" if st == ACKED else "OPEN", acked_by=C1 if st == ACKED else None,
              is_final=st == FINAL)
    s = snap(st, alert=a)
    d = decide(s, Event(states.SQUEEZE, pattern="duress", k=2), first)
    assert [(op.op, op.kind, op.masked) for op in d.alert_ops] == [("upgrade", DURESS, True)]
    assert d.fork and d.alert_masked_after and d.public_state == st and d.to_state == st
    assert not d.cover_alert_ops  # the twin (a late plain squeeze) leaves the alert she saw open
    # A real PIN afterwards: the escalation goes on, the twin's alert closes as a false alarm.
    s2 = step(s, d)
    assert s2.cover_alert is not None and s2.cover_alert.kind == NO_RESPONSE and not s2.cover_alert.masked
    d2 = decide(s2, Event(states.PIN_REAL), first)
    assert d2.to_state == st and d2.public_state == ARMED and d2.reply == "cancelled"
    assert [(op.op, op.status) for op in d2.cover_alert_ops] == [("close", states.FALSE_ALARM)]
    assert states.device_cmd(d2.public_state, d2.challenge_n, None, d2.prompt_cause)["led"] == "off"


def test_real_pin_never_closes_duress_alert():
    a = alert(kind=DURESS, level=1, masked=True, recipients=(C1,))
    s = snap("ESCALATED_L1", public_state=ARMED, alert=a)
    d = decide(s, Event(states.PIN_REAL))
    assert d.to_state == "ESCALATED_L1" and not d.alert_ops
    assert [n.template for n in d.notify if n.to == "contact"] == ["duress_pin_note"]
    assert d.public_state == ARMED and d.reply == "cancelled"


def test_real_pin_on_no_response_is_false_alarm():
    s = snap("ESCALATED_L2", alert=alert(level=2))
    d = decide(s, Event(states.PIN_REAL))
    assert d.through == states.FALSE_ALARM and d.to_state == ARMED and d.location == "expire_now"
    assert sorted(n.contact_id for n in d.notify if n.template == "false_alarm") == [C1, C2]


def test_lost_contact_has_its_own_wording_and_keeps_deadlines():
    s = snap(REMINDED, challenge_n=3, prompt_cause="challenge")
    d = decide(s, Event(states.LEASE_EXPIRED))
    assert d.link_state == states.LINK_LOST and d.to_state == REMINDED
    assert not d.cancel_state_timers and not d.changed  # pending deadlines keep running
    assert [t.kind for t in d.timers] == [states.LOST_CONTACT_NOTIFY]
    assert d.wearer_templates == ["wearer_lost_contact"]
    s2 = step(s, d)
    d2 = decide(s2, Event(states.LOST_CONTACT_NOTIFY))
    assert d2.alert_kind_after == LOST_CONTACT and [n.template for n in d2.notify] == ["lost_contact"]
    text = messages.render("lost_contact", {"name": "Maya", "time": "21:14"})
    assert text == ("StillHere: We lost contact with Maya's device at 21:14. Cause unknown. "
                    "Her check-in schedule is still running.")
    assert text != messages.render("alert_no_response", {"name": "Maya"})
    # The deadline still escalates as NO_RESPONSE while the link is down.
    s3 = step(s2, d2)
    d3 = decide(s3, Event(states.REMINDER_TIMEOUT))
    assert d3.to_state == "ESCALATED_L1" and d3.alert_kind_after == NO_RESPONSE


def test_lost_contact_with_open_alert_tells_recipients_now():
    s = snap("ESCALATED_L1", alert=alert(level=1, recipients=(C1,)))
    d = decide(s, Event(states.LEASE_EXPIRED))
    assert [n.contact_id for n in d.notify if n.template == "lost_contact"] == [C1]
    assert d.to_state == "ESCALATED_L1"
    s2 = step(s, d)
    d2 = decide(s2, Event(states.LINK_RESTORED))
    assert [n.contact_id for n in d2.notify if n.template == "restored"] == [C1]
    assert not d2.alert_ops  # restore never closes an escalation


def test_invariant_guard_raises_on_a_bad_decision():
    s = snap(ARMED)
    bad = states.Decision(event=Event(states.PULL_FORWARD, reason="offbody", level="MED"), from_state=ARMED,
                          to_state="ESCALATED_L1", public_state="ESCALATED_L1")
    with pytest.raises(states.InvariantViolation):
        states._check_invariants(s, bad)
    bad2 = states.Decision(event=Event(states.SQUEEZE, pattern="plain", k=2), from_state="ESCALATED_L1",
                           to_state=ARMED, public_state=ARMED,
                           alert_ops=[states.AlertOp("close", status=states.RESOLVED)])
    with pytest.raises(states.InvariantViolation):
        states._check_invariants(snap("ESCALATED_L1", alert=alert()), bad2)


def test_no_contacts_goes_straight_to_simulated_final():
    s = snap(REMINDED, contacts=(), challenge_n=3, prompt_cause="challenge")
    d = decide(s, Event(states.REMINDER_TIMEOUT))
    assert d.to_state == FINAL and not d.notify


# --- randomized walk ------------------------------------------------------------
def _fold_alert(a, ops, notify, twin: bool):
    recipients = list(a.recipients) if a else []
    for op in ops:
        if op.op == "open":
            a, recipients = AlertView(kind=op.kind, level=op.level or 1, masked=bool(op.masked)), []
        elif op.op == "level":
            a = replace(a, level=op.level, is_final=op.is_final, status="OPEN", acked_by=None, renotify_count=0)
        elif op.op == "ack":
            a = replace(a, status="ACKED", acked_by=op.contact_id)
        elif op.op == "close":
            a = None
        elif op.op == "upgrade":
            a = replace(a, kind=op.kind or a.kind, masked=a.masked if op.masked is None else op.masked)
        elif op.op == "renotify":
            a = replace(a, renotify_count=a.renotify_count + 1)
    if a is not None:
        for n in notify:
            if n.to == "contact" and n.twin == twin and n.contact_id not in recipients:
                recipients.append(n.contact_id)
        a = replace(a, recipients=tuple(recipients))
    return a


def step(s: Snapshot, d) -> Snapshot:
    """Fold a decision back into a snapshot, the twin's alert included (a fork keeps the alert she
    could see as the twin's and continues the escalation on a copy)."""
    cover_base = s.alert if d.fork else s.cover_alert
    return replace(s, state=d.to_state, public_state=d.public_state, version=s.version + (1 if d.changed else 0),
                   alert=_fold_alert(s.alert, d.alert_ops, d.notify, twin=False),
                   cover_alert=_fold_alert(cover_base, d.cover_alert_ops, d.notify, twin=True),
                   challenge_n=d.challenge_n, prompt_cause=d.prompt_cause, retry_used=d.retry_used,
                   link_state=d.link_state, lost_contact_told=d.lost_contact_told,
                   public_link_state=d.public_link_state, cover_told=d.cover_told)


def test_random_walks_hold_every_invariant():
    rng = random.Random(1234)
    events = sample_events()
    for walk in range(300):
        s = snap(ARMED, contacts=tuple(range(201, 201 + rng.randint(0, 3))))
        for _ in range(60):
            ev = rng.choice(events)
            if ev.contact_id is not None and s.contacts:
                ev = replace(ev, contact_id=rng.choice(s.contacts + (999,)))
            d = decide(s, ev, lambda opts: rng.choice(list(opts)))
            # Sensors never escalate.
            if ev.kind == states.PULL_FORWARD:
                assert not is_alerting(d.to_state) or is_alerting(s.state)
            # Contacts only hear about an alert; nothing before one.
            contact_msgs = [n for n in d.notify if n.to == "contact"]
            if contact_msgs:
                assert set(n.contact_id for n in contact_msgs) <= set(s.contacts)
                if not d.alert_open_after:
                    assert s.alert is not None
                    assert set(n.contact_id for n in contact_msgs) <= set(s.alert.recipients)
            # Masked alerts keep the public state off the escalation ladder, unless the cover's
            # own check-in went unanswered (a twin that used the real PIN would escalate too).
            if d.alert_open_after and d.alert_masked_after:
                assert (not is_alerting(d.public_state) or is_alerting(s.public_state)
                        or ev.kind == states.COVER_REMINDER_TIMEOUT)
            # FINAL is only ever simulated.
            if d.to_state == FINAL and s.state != FINAL:
                assert any(e.kind == "emergency_services_simulated" and e.detail["placed"] is False for e in d.audit)
            s = step(s, d)
            if s.state == ENDED:
                break


# --- masked duress: the cover follows a twin -------------------------------------
def masked_duress(public=ARMED, **kw):
    a = AlertView(kind=DURESS, level=1, masked=True, recipients=(C1,))
    return snap("ESCALATED_L1", public_state=public, alert=a, **kw)


def test_masked_duress_keeps_the_twins_check_in_schedule():
    s = masked_duress()
    d = decide(s, Event(states.COVER_CHECKIN_DUE), first)
    twin = decide(snap(ARMED), Event(states.CHECKIN_DUE), first)
    assert _wearer_view(d) == _wearer_view(twin)
    assert d.to_state == "ESCALATED_L1" and not d.alert_ops and not d.cancel_state_timers
    assert [n for n in d.notify if n.to == "contact"] == []
    assert [t.kind for t in d.timers] == [states.COVER_PROMPT_TIMEOUT]
    # An escalation step underneath leaves the cover prompt exactly as it was.
    s2 = step(s, d)
    d2 = decide(s2, Event(states.ACK_TIMEOUT))
    assert d2.to_state == "ESCALATED_L2" and d2.public_state == PROMPTED
    assert (d2.challenge_n, d2.prompt_cause) == (s2.challenge_n, s2.prompt_cause) and not d2.public_changed
    # Without a mask, a cover timer does nothing.
    assert decide(snap(ARMED), Event(states.COVER_CHECKIN_DUE)).reply == "ignored"


def test_masked_duress_session_end_asks_home_safe():
    d = decide(masked_duress(), Event(states.SESSION_END_DUE), first)
    assert d.public_state == PROMPTED and d.wearer_templates == ["home_safe"]
    assert d.to_state == "ESCALATED_L1"


def test_masked_duress_sensor_input_prompts_the_cover_only():
    s = masked_duress()
    ev = Event(states.PULL_FORWARD, reason="head_position_change", level="MED")
    d, twin = decide(s, ev, first), decide(snap(ARMED), ev, first)
    assert _wearer_view(d) == _wearer_view(twin)
    assert d.to_state == s.state and not d.alert_ops and d.public_changed
    assert [t.kind for t in d.timers] == [states.COVER_PROMPT_TIMEOUT]


@pytest.mark.parametrize("public", [ARMED, ENDED])
def test_masked_duress_wearer_input_matches_the_twin_and_still_tells_contacts(public):
    s = masked_duress(public=public)
    twin_state = snap(public)
    for ev in (Event(states.SQUEEZE, pattern="unrecognized", k=5), Event(states.SQUEEZE, pattern="plain", k=2),
               Event(states.WEARER_OK)):
        d, twin = decide(s, ev, first), decide(twin_state, ev, first)
        assert _wearer_view(d) == _wearer_view(twin), ev
        assert d.to_state == s.state and not d.alert_ops
        assert [n.template for n in d.notify if n.to == "contact"] in (["late_device"], ["late_phone"])


@pytest.mark.parametrize("public", [ARMED, ENDED])
def test_masked_duress_link_notices_follow_the_twin(public):
    s = masked_duress(public=public)
    d = decide(s, Event(states.LEASE_EXPIRED))
    assert d.wearer_templates == ([] if public == ENDED else ["wearer_lost_contact"])
    assert [n.template for n in d.notify if n.to == "contact"] == ["lost_contact"]
    assert d.link_state == states.LINK_LOST
    d2 = decide(step(s, d), Event(states.LINK_RESTORED))
    assert d2.wearer_templates == ([] if public == ENDED else ["wearer_restored"])
    assert [n.template for n in d2.notify if n.to == "contact"] == ["restored"]


def test_back_to_final_spends_the_renotify_budget_and_a_repeat_ack_is_not_announced():
    held = alert(level=3, is_final=True, status="ACKED", acked_by=C1, recipients=(C1, C2), ackers=(C1,))
    s = snap(ACKED, alert=held)
    d = decide(s, Event(states.CONTACT_CANT_REACH, contact_id=C1), first)
    assert d.to_state == FINAL
    assert [(op.op, op.keep_count) for op in d.alert_ops] == [("level", True), ("renotify", False)]
    assert sorted(n.contact_id for n in d.notify if n.template == "alert_final") == [C1, C2]
    spent = replace(held, renotify_count=T.renotify_cap)
    d = decide(snap(ACKED, alert=spent), Event(states.CONTACT_CANT_REACH, contact_id=C1), first)
    assert d.to_state == FINAL and not [n for n in d.notify if n.template == "alert_final" and n.contact_id != C1]
    again = decide(snap(FINAL, alert=replace(held, status="OPEN", acked_by=None)),
                   Event(states.CONTACT_ACK, contact_id=C1), first)
    assert again.reply == "acked" and not [n for n in again.notify if n.template == "handling"]
    first_ack = decide(snap(FINAL, alert=replace(held, status="OPEN", acked_by=None, ackers=())),
                       Event(states.CONTACT_ACK, contact_id=C1), first)
    assert [n.contact_id for n in first_ack.notify if n.template == "handling"] == [C2]
