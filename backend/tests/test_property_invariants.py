"""Property-based tests (Hypothesis) of the KAZE invariants on the pure code: no database, no clock.

Random event sequences are folded through states.decide() the way the engine applies them, starting from
random contact lists and duress-recipient choices. After every event the invariants are checked:

* sensors and heart rate (PULL_FORWARD) only ever move ARMED -> PROMPTED; nothing they send reaches
  ESCALATED_* or touches an alert;
* only a contact's "she's safe" (from the contact who took it on) or the wearer's real PIN closes an alert;
  a real PIN never closes a duress alert;
* a device squeeze during an alert, late or not, never stands it down;
* from every reachable state, the real PIN and the duress PIN (cancel and end) give the wearer the same
  reply, public state, device command, messages and visible timeline; so do a plain and a duress squeeze;
* a masked duress never shows the wearer an escalation it did not cause herself;
* FINAL is simulated: the emergency-services step is logged as not placed and no message goes anywhere
  but the wearer and her approved contacts;
* contacts hear nothing before an alert and only ever the approved ones;
* LOST_CONTACT keeps every deadline running and has its own wording.

The rules, parsers and the senior-mode engine get the same treatment: whatever the input, a sensor or
heart-rate rule can only return a PullForward, the bridge's line parser never raises, the telemetry row
parser only raises BatchError, and a care card stays labelled, bounded and free of banned wording.
"""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app import care, messages, rules, states, timeseries
from app.config import PROFILES
from app.states import (ACKED, ARMED, DURESS, ENDED, FINAL, PROMPTED, AlertView, Event, Snapshot, decide,
                        is_alerting, is_escalated)
from scripts import bridge as br
from tests.test_wording import PATTERN as BANNED_WORDING

T = PROFILES["demo"]
OUTSIDER = 999  # a contact id that is never approved
FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# Event kinds that may move a session that is not alerting into an escalation. A missed check-in
# (REMINDER_TIMEOUT) or a duress signal; nothing else, and never a sensor.
ESCALATION_ENTRY = {states.REMINDER_TIMEOUT, states.PIN_DURESS, states.END_DURESS}


def first(options):
    return list(options)[0]


# --- strategies ---------------------------------------------------------------------------------------
contact_lists = st.lists(st.integers(201, 206), min_size=0, max_size=4, unique=True).map(tuple)

TIMER_EVENTS = (states.CHECKIN_DUE, states.PROMPT_TIMEOUT, states.REMINDER_TIMEOUT, states.ACK_TIMEOUT,
                states.RENOTIFY, states.FOLLOWUP_TIMEOUT, states.SESSION_END_DUE, states.LOST_CONTACT_NOTIFY)
PLAIN_EVENTS = (states.WEARER_OK, states.PIN_REAL, states.PIN_DURESS, states.END_REAL, states.END_DURESS,
                states.LEASE_EXPIRED, states.LINK_RESTORED, states.ARRIVED) + states.COVER_TIMER_KINDS


def events_for(contacts: tuple) -> st.SearchStrategy:
    who = st.sampled_from(contacts + (OUTSIDER,))
    squeeze = st.builds(Event, kind=st.just(states.SQUEEZE), pattern=st.sampled_from(states.SQUEEZE_PATTERNS),
                        k=st.one_of(st.none(), st.integers(1, 4)), late=st.booleans(), stale=st.booleans())
    sensor = st.builds(Event, kind=st.just(states.PULL_FORWARD), reason=st.sampled_from(rules.REASONS),
                       level=st.sampled_from((rules.LOW, rules.MED, rules.HIGH)), simulated=st.booleans())
    contact = st.builds(Event, kind=st.sampled_from((states.CONTACT_ACK, states.CONTACT_PASS, states.CONTACT_SAFE,
                                                     states.CONTACT_CANT_REACH)),
                        contact_id=who, twin=st.booleans())
    plain = st.sampled_from(TIMER_EVENTS + PLAIN_EVENTS).map(Event)
    # Timers and squeezes dominate a real night; weight them so walks reach every escalation level.
    return st.one_of(plain, plain, squeeze, sensor, contact, st.sampled_from(TIMER_EVENTS).map(Event))


def guided(s: Snapshot) -> st.SearchStrategy:
    """Events that make sense in state `s` (the next deadline, the right squeeze, the contact who can act),
    so walks reach every level, ACKED, FINAL and the masked twin's own prompts and alerts."""
    a, opts = s.alert, []
    ladder = states.ladder(s, a.kind) if a is not None else ()
    if s.state == ARMED:
        opts += [Event(states.CHECKIN_DUE), Event(states.PULL_FORWARD, reason=rules.REASON_HR, level=rules.MED)]
    if s.state == PROMPTED:
        opts += [Event(states.PROMPT_TIMEOUT), Event(states.SQUEEZE, pattern="plain", k=s.challenge_n)]
    if s.state == states.REMINDED:
        opts += [Event(states.REMINDER_TIMEOUT), Event(states.REMINDER_TIMEOUT)]
    if is_escalated(s.state) and a is not None:
        opts += [Event(states.ACK_TIMEOUT), Event(states.RENOTIFY)]
        opts += [Event(states.CONTACT_ACK, contact_id=c) for c in a.recipients]
        if 0 < a.level <= len(ladder):
            opts.append(Event(states.CONTACT_PASS, contact_id=ladder[a.level - 1]))
    if s.state == ACKED and a is not None:
        opts += [Event(states.CONTACT_SAFE, contact_id=a.acked_by), Event(states.CONTACT_CANT_REACH, contact_id=a.acked_by),
                 Event(states.FOLLOWUP_TIMEOUT), Event(states.CONTACT_ACK, contact_id=a.acked_by)]
    if states.is_masked(s):
        twin = s.cover_alert
        opts += [Event(k) for k in states.COVER_TIMER_KINDS]
        if s.public_state in states.PROMPT_STATES and s.challenge_n:
            opts.append(Event(states.SQUEEZE, pattern="plain", k=s.challenge_n))
        if twin is not None:
            opts += [Event(kind, contact_id=c, twin=True) for c in twin.recipients
                     for kind in (states.CONTACT_ACK, states.CONTACT_SAFE, states.CONTACT_CANT_REACH)]
    opts += [Event(states.PIN_DURESS), Event(states.SQUEEZE, pattern="duress", k=3), Event(states.LEASE_EXPIRED),
             Event(states.LINK_RESTORED)]
    return st.sampled_from(opts)


def next_event(data, s: Snapshot) -> Event:
    if data.draw(st.integers(0, 9), label="guided?") < 6:
        return data.draw(guided(s), label="event")
    return data.draw(events_for(s.contacts), label="event")


def start_snapshot(data) -> Snapshot:
    contacts = data.draw(contact_lists, label="contacts")
    duress = tuple(c for c in contacts if data.draw(st.booleans(), label="duress recipient"))
    return Snapshot(state=ARMED, public_state=ARMED, version=1, timings=T, contacts=contacts,
                    duress_contacts=duress)


def walk(data, max_events: int = 50, on_state=None, on_decision=None) -> Snapshot:
    """Fold `max_events` drawn events from a fresh session; returns the last state reached."""
    s = start_snapshot(data)
    if on_state:
        on_state(s)
    for _ in range(data.draw(st.integers(1, max_events), label="length")):
        ev = next_event(data, s)
        d = decide(s, ev, first)  # _finish() re-checks the core rules and raises InvariantViolation
        if on_decision:
            on_decision(s, ev, d)
        s = step(s, d)
        if s.state == ENDED:
            break
        if on_state:
            on_state(s)
    return s


# --- folding a decision back into a snapshot (what engine.apply stores) --------------------------------
def _fold_alert(a, ops, notify, twin: bool):
    recipients = list(a.recipients) if a else []
    for op in ops:
        if op.op == "open":
            a, recipients = AlertView(kind=op.kind, level=op.level or 1, masked=bool(op.masked)), []
        elif op.op == "level":
            a = replace(a, level=op.level, is_final=op.is_final, status="OPEN", acked_by=None,
                        renotify_count=a.renotify_count if op.keep_count else 0)
        elif op.op == "ack":
            ackers = a.ackers if op.contact_id in a.ackers else a.ackers + (op.contact_id,)
            a = replace(a, status="ACKED", acked_by=op.contact_id, ackers=ackers)
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
    cover_base = s.alert if d.fork else s.cover_alert
    cover = _fold_alert(cover_base, d.cover_alert_ops, d.notify, twin=True)
    alert = _fold_alert(s.alert, d.alert_ops, d.notify, twin=False)
    if alert is None or not alert.masked:
        cover = None  # the twin ends with the mask
    return replace(s, state=d.to_state, public_state=d.public_state, version=s.version + (1 if d.changed else 0),
                   alert=alert, cover_alert=cover, challenge_n=d.challenge_n, prompt_cause=d.prompt_cause,
                   retry_used=d.retry_used, link_state=d.link_state, lost_contact_told=d.lost_contact_told,
                   public_link_state=d.public_link_state, cover_told=d.cover_told)


def wearer_view(d) -> tuple:
    """Everything the wearer can observe from one decision: reply, public state, LED command, her
    messages (rendered) and her visible timeline entries."""
    cmd = states.device_cmd(d.public_state, d.challenge_n, "cid", d.prompt_cause)
    texts = [messages.render(n.template, {"name": "Maya", **n.params}) for n in d.notify if n.to == "wearer"]
    visible = [(e.kind, e.detail, e.from_state or d.from_state, e.to_state or d.to_state)
               for e in d.audit if not e.masked]
    timers = sorted(t.kind.removeprefix("COVER_") for t in d.timers
                    if t.kind.startswith("COVER_") or (not d.cover and t.kind in states.STATE_TIMER_KINDS))
    return d.reply, d.public_state, d.public_link_state, cmd, texts, visible, d.challenge_n, timers


def _closes(d) -> list:
    return [op for op in d.alert_ops if op.op == "close"]


def check_decision(s: Snapshot, ev: Event, d) -> None:
    # 1. Sensors and heart rate only ever bring a check-in forward.
    if ev.kind == states.PULL_FORWARD:
        assert d.to_state in (s.state, PROMPTED), (s.state, d.to_state)
        if s.state != ARMED:
            assert d.to_state == s.state
        assert not d.alert_ops and not d.cover_alert_ops
        assert not (is_alerting(d.to_state) and not is_alerting(s.state))
        assert not (is_alerting(d.public_state) and not is_alerting(s.public_state))
        assert all(n.to == "wearer" or n.silent for n in d.notify)
    # 2. Into an escalation only through a missed check-in or a duress signal.
    if is_alerting(d.to_state) and not is_alerting(s.state):
        assert ev.kind in ESCALATION_ENTRY or (ev.kind == states.SQUEEZE and ev.pattern == "duress"), ev
    # ...and the wearer sees an escalation only after her own (or her twin's) missed check-in.
    if is_alerting(d.public_state) and not is_alerting(s.public_state):
        assert ev.kind in (states.REMINDER_TIMEOUT, states.COVER_REMINDER_TIMEOUT), ev
    # 3. Only "she's safe" from the contact who took it on, or the real PIN, closes an alert.
    for op in _closes(d):
        if op.status in (states.RESOLVED, states.FALSE_ALARM):
            assert ev.kind in states.RESOLVING_EVENTS, ev
            if ev.kind == states.CONTACT_SAFE:
                assert s.alert is not None and ev.contact_id == s.alert.acked_by and s.state == ACKED
            else:
                assert s.alert is not None and s.alert.kind != DURESS, "a real PIN closed a duress alert"
        else:
            assert op.status == states.ALERT_RESTORED and ev.kind == states.LINK_RESTORED
            assert s.alert is not None and s.alert.kind == states.LOST_CONTACT
    if is_alerting(s.state) and not is_alerting(d.to_state):
        assert ev.kind in states.RESOLVING_EVENTS, f"{ev.kind} stood down {s.state}"
        if s.alert is not None and s.alert.kind == DURESS:
            assert ev.kind == states.CONTACT_SAFE, "only she's-safe closes a duress alert"
    # 4. A squeeze during an alert never stands it down, late or not.
    if ev.kind == states.SQUEEZE and is_alerting(s.state):
        assert d.to_state == s.state and not _closes(d) and not d.cancel_state_timers
    # 5. FINAL is simulated; nothing is ever dialled and nobody else is messaged.
    if d.to_state == FINAL and s.state != FINAL:
        sim = [e for e in d.audit if e.kind == "emergency_services_simulated"]
        assert sim and all(e.detail["placed"] is False for e in sim)
        assert states.FINAL_BANNER.startswith("Emergency services: SIMULATED")
    assert all(n.to in ("wearer", "contact") for n in d.notify)
    # 6. Contacts: only approved ones, and only about an alert.
    told = [n for n in d.notify if n.to == "contact"]
    assert {n.contact_id for n in told} <= set(s.contacts), "a message to a contact who is not approved"
    if told:
        assert s.alert is not None or d.alert_open_after or s.cover_alert is not None
    # 7. Duress is masked: while a masked alert is open nothing duress-shaped reaches the wearer.
    if d.alert_open_after and d.alert_masked_after:
        assert not any(n.template in ("alert_duress", "duress_pin_note") for n in d.notify if n.to == "wearer")
    # 8. LOST_CONTACT keeps the deadlines running and speaks only for itself.
    if ev.kind == states.LEASE_EXPIRED:
        assert d.to_state == s.state and not d.cancel_state_timers and not d.alert_ops
        assert {n.template for n in d.notify if n.to == "contact"} <= {"lost_contact"}


def check_twins(s: Snapshot) -> None:
    """From state `s`: real vs duress PIN (cancel and end) and plain vs duress squeeze look the same."""
    pairs = [(Event(states.PIN_REAL), Event(states.PIN_DURESS)), (Event(states.END_REAL), Event(states.END_DURESS))]
    if s.public_state in states.PROMPT_STATES and s.challenge_n:
        pairs.append((Event(states.SQUEEZE, pattern="plain", k=s.challenge_n),
                      Event(states.SQUEEZE, pattern="duress", k=3)))
    elif s.public_state == ARMED or is_alerting(s.public_state):
        pairs.append((Event(states.SQUEEZE, pattern="plain", k=2), Event(states.SQUEEZE, pattern="duress", k=3)))
    for ok_ev, duress_ev in pairs:
        ok, du = decide(s, ok_ev, first), decide(s, duress_ev, first)
        assert wearer_view(ok) == wearer_view(du), (s.state, s.public_state, ok_ev.kind)


@FAST
@given(st.data())
def test_random_event_sequences_hold_every_kaze_invariant(data):
    walk(data, on_decision=check_decision)


@FAST
@given(st.data())
def test_real_and_duress_pin_look_the_same_from_every_reachable_state(data):
    walk(data, max_events=40, on_state=check_twins)


@FAST
@given(st.data())
def test_late_squeezes_never_stand_an_escalation_down(data):
    s = walk(data, max_events=40)
    assume(is_alerting(s.state))
    was_open = s.alert is not None
    squeezes = data.draw(st.lists(st.tuples(st.sampled_from(states.SQUEEZE_PATTERNS),
                                            st.one_of(st.none(), st.integers(1, 4)), st.booleans()),
                                  min_size=1, max_size=8), label="squeezes")
    for pattern, k, late in squeezes:
        d = decide(s, Event(states.SQUEEZE, pattern=pattern, k=k, late=late), first)
        assert d.to_state == s.state and not _closes(d)
        s = step(s, d)
        assert is_alerting(s.state) and (s.alert is not None) == was_open


@FAST
@given(st.data())
def test_sensor_bursts_from_any_state_never_reach_an_escalation(data):
    s = walk(data, max_events=30)
    assume(s.state != ENDED)
    for reason in data.draw(st.lists(st.sampled_from(rules.REASONS), min_size=1, max_size=10), label="sensors"):
        ev = Event(states.PULL_FORWARD, reason=reason, level=rules.MED)
        d = decide(s, ev, first)
        check_decision(s, ev, d)
        assert not (is_escalated(d.to_state) and not is_escalated(s.state))
        s = step(s, d)


# --- rules: every output is a PullForward ----------------------------------------------------------------
NOW = datetime(2026, 10, 17, 21, 0, tzinfo=timezone.utc)
opt_float = st.one_of(st.none(), st.floats(min_value=0, max_value=400, allow_nan=False))
opt_ago = st.one_of(st.none(), st.integers(0, 4 * 3600).map(lambda s: NOW - timedelta(seconds=s)))


@FAST
@given(median=opt_float, baseline=opt_float, since=opt_ago, sustain=st.floats(0, 600), enmo=opt_float,
       vigorous=opt_ago, simulated=st.booleans())
def test_the_heart_rate_rule_can_only_return_a_pull_forward(median, baseline, since, sustain, enmo, vigorous,
                                                            simulated):
    res = rules.hr_rule(now=NOW, median_60s=median, baseline=baseline, deviating_since=since, sustain_s=sustain,
                        enmo60_mg=enmo, last_vigorous_at=vigorous, simulated=simulated)
    assert isinstance(res, rules.HrRuleResult)
    if res.pull_forward is not None:
        pf = res.pull_forward
        assert type(pf) is rules.PullForward and pf.reason == rules.REASON_HR and pf.simulated == simulated
        # It fires only while still, after the sustain time, away from a vigorous spell.
        assert enmo is not None and enmo < rules.STILL_ENMO_MG
        assert baseline is not None and median is not None
        assert abs(median - baseline) >= rules.hr_threshold(baseline)
    lost = rules.hr_lost_rule(now=NOW, armed=True, last_sample_at=since, lost_after_s=sustain,
                              already_fired_for=vigorous)
    assert lost is None or (type(lost) is rules.PullForward and lost.reason == rules.REASON_HR_LOST)


@FAST
@given(payload=st.one_of(st.none(), st.dictionaries(st.text(max_size=8), st.one_of(st.integers(), st.text(max_size=8),
                                                                                   st.booleans()), max_size=4)))
def test_device_sensor_rules_only_return_pull_forwards(payload):
    for rule in (rules.head_position_rule, rules.fall_like_rule, rules.offbody_rule):
        assert type(rule(payload)) is rules.PullForward


@FAST
@given(st.text(max_size=120))
def test_the_bridge_line_parser_never_raises(line):
    kind, item = br.parse_line(line, 1.0)
    assert kind in (None, "H", "E", "R")
    if kind is None:
        assert isinstance(item, str)
    if kind == "E":
        assert item.type in br.EVENT_TYPES and br._printable(item.event_id)


@FAST
@given(boot=st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126, exclude_characters=",:"),
                    min_size=1, max_size=6),
       seq=st.integers(0, 10 ** 9), etype=st.sampled_from(sorted(br.EVENT_TYPES)),
       k=st.integers(1, 4), pattern=st.sampled_from(states.SQUEEZE_PATTERNS))
def test_well_formed_event_lines_round_trip(boot, seq, etype, k, pattern):
    kind, ev = br.parse_line(f'E,{boot}:{seq},{etype},{{"pattern":"{pattern}","k":{k},"device_ms":5}}', 2.0)
    assert kind == "E" and ev.event_id == f"{boot}:{seq}" and ev.type == etype
    assert ev.payload == {"pattern": pattern, "k": k} and ev.device_ms == 5


num = st.one_of(st.integers(-40000, 40000), st.floats(allow_nan=True, allow_infinity=True), st.none(), st.text(max_size=3))


@FAST
@given(st.lists(st.lists(num, min_size=0, max_size=7), min_size=0, max_size=6), st.sampled_from(("device_ms", "epoch")))
def test_the_telemetry_row_parser_accepts_or_raises_batch_error_only(raw, clock):
    try:
        prep = timeseries.prepare_rows(raw, clock=clock, now=NOW, user_id="u", device_id="d", profile="night_out",
                                       simulated=False, device_ms_now=None)
    except timeseries.BatchError:
        return
    for row in prep.rows:
        assert all(v is None or isinstance(v, (int, float, str, datetime, bool)) for v in row)


# --- senior mode: any seed gives labelled, bounded, clean cards --------------------------------------------
_WORD = re.compile(r"\b(will|likely to)\s+(fall|develop)\b", re.I)


@settings(max_examples=6, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(0, 2 ** 31 - 1))
def test_any_synthetic_cohort_scores_into_labelled_bounded_cards(seed):
    end = date(2026, 10, 16)
    cards = care.score_cohort(care.generate_cohort(seed, end), end)
    assert len(cards) == 20
    for card in cards:
        assert card["synthetic"] is True and 0 <= card["score"] <= 100
        assert card["level"] in care.LEVELS
        text = " ".join(str(v) for v in (card["suggested_check"], card.get("pattern_note"),
                                          care.local_briefing(card))) + " ".join(r["explanation"] for r in card["rules"])
        assert not BANNED_WORDING.search(text), text
        assert not _WORD.search(text)


def test_the_property_modules_import_nothing_that_talks_to_a_model():
    import app.states
    import app.rules
    src = (app.states.__file__, app.rules.__file__)
    for path in src:
        body = open(path, encoding="utf-8").read()
        assert "vultr" not in body and "openai" not in body.lower() and "anthropic" not in body.lower()


@pytest.mark.parametrize("ev", [states.PIN_REAL, states.END_REAL])
def test_walk_helper_matches_the_engine_for_a_simple_false_alarm(ev):
    """The fold above agrees with the stored result of the simplest alert lifecycle."""
    s = Snapshot(state=ARMED, public_state=ARMED, version=1, timings=T, contacts=(201,))
    for kind in (states.CHECKIN_DUE, states.PROMPT_TIMEOUT, states.REMINDER_TIMEOUT):
        s = step(s, decide(s, Event(kind), first))
    assert s.state == "ESCALATED_L1" and s.alert is not None and s.alert.recipients == (201,)
    d = decide(s, Event(ev), first)
    s = step(s, d)
    assert d.through == states.FALSE_ALARM and s.alert is None
