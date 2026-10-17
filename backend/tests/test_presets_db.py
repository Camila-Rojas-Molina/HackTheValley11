"""Session presets and the walking-home arrival tap (BUILD_PLAN §6.2 last row, §6.5, §7.1).

  night_out     every 30 min, "Home safe?" after 6 h
  date          every 45 min
  walking_home  one check-in, "Home safe?", 20 min after arming; ends on the PIN or an "I'm home" tap
Preset cadences apply to the real profile; the demo profile keeps its compressed timing.

The arrival tap is not a PIN: it ends a walking-home session only when no alert is open. During an alert it
is a late response from her phone (the alert stays open; only "she's safe" or the real PIN close it), and a
masked duress session answers it through its twin like any other wearer input.
"""
from __future__ import annotations

import pytest

from app import states
from app.config import PRESETS
from app.states import ARMED, ENDED, LOST_CONTACT, PROMPTED, REMINDED, AlertView, Event, decide
from tests.conftest import (DURESS_PIN, PIN, fire, make_world, open_alert, outbox, pending_timers, session_row,
                            to_state)
from tests.test_states import ALL_SNAPS, C1, snap


# --- state machine -----------------------------------------------------------------------------------
@pytest.mark.parametrize("s", [snap(ARMED), snap(PROMPTED, challenge_n=2, prompt_cause="routine"),
                               snap(REMINDED, challenge_n=3, prompt_cause="challenge")])
def test_arrival_ends_a_session_with_nothing_open(s):
    d = decide(s, Event(states.ARRIVED))
    assert d.to_state == ENDED and d.reply == "ended" and d.wearer_templates == ["ended"]
    assert d.cancel_all_timers and not d.alert_ops
    assert [(e.kind, e.detail, e.to_state) for e in d.audit] == [("arrived", {"via": "phone"}, ENDED)]
    assert not [n for n in d.notify if n.to == "contact"]


@pytest.mark.parametrize("s", [x for x in ALL_SNAPS if states.is_alerting(x.state)],
                         ids=lambda x: f"{x.state}-{x.alert.kind if x.alert else None}")
def test_arrival_during_an_alert_is_a_late_response_and_never_closes_it(s):
    d = decide(s, Event(states.ARRIVED))
    assert not any(op.op == "close" for op in d.alert_ops)
    if not states.is_masked(s):
        assert d.to_state == s.state and d.reply == "noted"
        assert {n.template for n in d.notify if n.to == "contact"} <= {"late_phone"}
    assert states.ARRIVED not in states.RESOLVING_EVENTS


def test_arrival_with_a_lost_contact_notice_out_does_not_end_the_session():
    s = snap(ARMED, link_state=states.LINK_LOST, lost_contact_told=True,
             alert=AlertView(kind=LOST_CONTACT, level=1, recipients=(C1,)))
    d = decide(s, Event(states.ARRIVED))
    assert d.to_state == ARMED and d.reply == "noted" and not d.alert_ops   # the notice closes on restore only


def test_arrival_on_a_masked_duress_session_answers_through_the_twin():
    s = snap("ESCALATED_L1", public_state=ARMED,
             alert=AlertView(kind=states.DURESS, level=1, masked=True, recipients=(C1,)))
    d = decide(s, Event(states.ARRIVED))
    assert d.to_state == "ESCALATED_L1" and d.public_state == ENDED and d.reply == "ended"
    assert d.alert_open_after and d.alert_masked_after
    assert d.wearer_templates == ["ended"]


# --- presets over HTTP -------------------------------------------------------------------------------
async def _arm(client, w, **body):
    r = await client.post("/v1/sessions", json=body, headers=w.wearer)
    assert r.status_code == 200, r.text
    w.session_id = r.json()["session_id"]
    return r.json()


async def _left(db, ts) -> float:
    async with db.connection() as conn:
        return (await (await conn.execute("SELECT %s::timestamptz - now() AS d", (ts,))).fetchone())["d"].total_seconds()


def test_preset_table():
    assert set(PRESETS) == {"night_out", "date", "walking_home", "demo"}
    assert PRESETS["night_out"] == {"interval_s": 1800, "duration_s": 6 * 3600}
    assert PRESETS["date"] == {"interval_s": 2700, "duration_s": None}
    assert PRESETS["walking_home"] == {"interval_s": 1200, "duration_s": None, "single_checkin": True}


@pytest.mark.parametrize("preset,interval,end_s,kinds", [
    ("night_out", 1800, 6 * 3600, {states.CHECKIN_DUE, states.SESSION_END_DUE}),
    ("date", 2700, None, {states.CHECKIN_DUE}),
    ("walking_home", 1200, 1200, {states.SESSION_END_DUE}),
])
async def test_real_profile_presets(client, db, ctx, preset, interval, end_s, kinds):
    w = await make_world(client, db, contacts=1, arm=False)
    out = await _arm(client, w, preset=preset, profile="real")
    assert out["interval_s"] == interval
    timers = await pending_timers(db, w.session_id)
    assert {t["kind"] for t in timers} == kinds
    for t in timers:
        want = interval if t["kind"] == states.CHECKIN_DUE else end_s
        assert want - 5 <= await _left(db, t["fire_at"]) <= want
    if end_s is None:
        assert out["end_at"] is None
    else:
        assert end_s - 5 <= await _left(db, out["end_at"]) <= end_s


async def test_demo_profile_keeps_its_compressed_timing_for_presets(client, db, ctx):
    w = await make_world(client, db, contacts=1, arm=False)
    out = await _arm(client, w, preset="walking_home", profile="demo")
    assert out["interval_s"] == 60
    (t,) = await pending_timers(db, w.session_id)
    assert t["kind"] == states.SESSION_END_DUE and 55 <= await _left(db, t["fire_at"]) <= 60
    w2 = await make_world(client, db, contacts=1, arm=False)
    assert (await _arm(client, w2, preset="date", profile="demo"))["interval_s"] == 60
    w3 = await make_world(client, db, contacts=1, arm=False)       # an explicit interval still wins
    assert (await _arm(client, w3, preset="date", profile="real", interval_s=600))["interval_s"] == 600


async def test_walking_home_check_in_is_home_safe_and_escalates_if_missed(client, db, ctx):
    w = await make_world(client, db, contacts=1, arm=False)
    await _arm(client, w, preset="walking_home", profile="demo")
    assert await fire(db, ctx, w.session_id, states.SESSION_END_DUE) == "prompted"
    assert [o["text"] for o in await outbox(db, w.session_id, recipient="wearer")][-1] == "Home safe?"
    await fire(db, ctx, w.session_id, states.PROMPT_TIMEOUT)
    await fire(db, ctx, w.session_id, states.REMINDER_TIMEOUT)
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"
    # Answered, the walk goes on with a check-in every interval until her PIN or the arrival tap.
    w2 = await make_world(client, db, contacts=1, arm=False)
    await _arm(client, w2, preset="walking_home", profile="demo")
    await fire(db, ctx, w2.session_id, states.SESSION_END_DUE)
    r = await client.post(f"/v1/sessions/{w2.session_id}/checkin", headers=w2.wearer, json={})
    assert r.json()["state_public"] == ARMED
    assert states.CHECKIN_DUE in [t["kind"] for t in await pending_timers(db, w2.session_id)]


async def test_arrival_tap_ends_a_walking_home_session(client, db, ctx):
    w = await make_world(client, db, contacts=1, arm=False)
    await _arm(client, w, preset="walking_home", profile="real")
    r = await client.post(f"/v1/sessions/{w.session_id}/arrived", headers=w.wearer)
    assert r.status_code == 200
    assert r.content == b'{"message":"Ended \xe2\x9c\x93","ok":true,"state_public":"ENDED"}'
    assert (await session_row(db, w.session_id))["state"] == ENDED
    assert await pending_timers(db, w.session_id) == []
    assert await outbox(db, w.session_id, recipient="contact") == []
    tl = (await client.get(f"/v1/sessions/{w.session_id}/timeline", headers=w.wearer)).json()["entries"]
    assert tl[-1]["kind"] == "arrived" and tl[-1]["to_state"] == ENDED


async def test_arrival_tap_is_only_for_walking_home_and_only_for_her(client, db, ctx):
    w = await make_world(client, db, contacts=1, arm=False)
    await _arm(client, w, preset="night_out", profile="real")
    r = await client.post(f"/v1/sessions/{w.session_id}/arrived", headers=w.wearer)
    assert r.status_code == 409
    assert (await session_row(db, w.session_id))["state"] == ARMED
    other = await make_world(client, db, contacts=0)
    assert (await client.post(f"/v1/sessions/{w.session_id}/arrived", headers=other.wearer)).status_code == 404
    assert (await client.post(f"/v1/sessions/{w.session_id}/arrived")).status_code == 401


async def test_arrival_tap_during_an_alert_never_stands_it_down(client, db, ctx):
    w = await make_world(client, db, contacts=1, arm=False)
    await _arm(client, w, preset="walking_home", profile="demo")
    await fire(db, ctx, w.session_id, states.SESSION_END_DUE)
    await fire(db, ctx, w.session_id, states.PROMPT_TIMEOUT)
    await fire(db, ctx, w.session_id, states.REMINDER_TIMEOUT)
    r = await client.post(f"/v1/sessions/{w.session_id}/arrived", headers=w.wearer)
    assert r.status_code == 200 and r.json()["state_public"] == "ESCALATED_L1" and r.json()["message"] == "Noted ✓"
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"
    assert (await open_alert(db, w.session_id))["kind"] == "NO_RESPONSE"
    assert (await outbox(db, w.session_id, recipient="contact"))[-1]["template"] == "late_phone"


async def test_arrival_tap_after_a_duress_pin_is_byte_identical_to_after_the_real_pin(client, db, ctx):
    real, duress = await make_world(client, db, contacts=1, arm=False), await make_world(client, db, contacts=1, arm=False)
    for w, pin in ((real, PIN), (duress, DURESS_PIN)):
        await _arm(client, w, preset="walking_home", profile="demo")
        await fire(db, ctx, w.session_id, states.SESSION_END_DUE)              # "Home safe?"
        r = await client.post(f"/v1/sessions/{w.session_id}/cancel", json={"pin": pin}, headers=w.wearer)
        assert r.status_code == 200
    taps = [await client.post(f"/v1/sessions/{w.session_id}/arrived", headers=w.wearer) for w in (real, duress)]
    assert taps[0].status_code == taps[1].status_code == 200
    assert taps[0].content == taps[1].content
    assert taps[0].json()["state_public"] == ENDED
    assert (await session_row(db, real.session_id))["state"] == ENDED
    sd = await session_row(db, duress.session_id)
    assert sd["state"] == "ESCALATED_L1" and sd["public_state"] == ENDED       # the help request keeps going
    assert (await open_alert(db, duress.session_id))["kind"] == "DURESS"
    views = [(await client.get(f"/v1/sessions/{w.session_id}", headers=w.wearer)).json() for w in (real, duress)]
    assert [(v["state_public"], v["cmd"]) for v in views][0] == [(v["state_public"], v["cmd"]) for v in views][1]
