"""Duress masking end to end: a session cancelled or ended with the duress PIN must look, to the
wearer and her device, exactly like a twin session that used the real PIN.

Each test builds two worlds (real PIN and duress PIN), drives both through the same inputs and
compares what the wearer can see: HTTP bodies, device replies, wearer messages and the visible
timeline.
"""
from __future__ import annotations

import pytest

from app import states, timers
from tests.conftest import DURESS_PIN, PIN, fire, make_world, outbox, session_row, to_state


@pytest.fixture
def first_choice(ctx):
    """Deterministic challenge numbers, so two worlds pick the same N."""
    old = ctx.choose
    ctx.choose = lambda options: list(options)[0]
    yield
    ctx.choose = old


def _norm(value, w):
    ids = {c["id"]: f"C{i + 1}" for i, c in enumerate(w.contacts)}
    if isinstance(value, dict):
        return {k: (ids.get(v, v) if k == "contact_id" else _norm(v, w)) for k, v in value.items()}
    return value


async def visible(client, w, sid=None):
    """The wearer's own timeline, without timestamps and with contact ids made comparable."""
    sid = sid or w.session_id
    r = await client.get(f"/v1/sessions/{sid}/timeline", headers=w.wearer)
    assert r.status_code == 200, r.text
    return [(e["actor"], e["kind"], e["from_state"], e["to_state"], _norm(e["detail"], w)) for e in r.json()["entries"]]


async def wearer_texts(db, w):
    return [o["text"] for o in await outbox(db, None, recipient="wearer") if str(o["user_id"]) == w.user_id]


async def test_false_alarm_entry_is_identical_for_real_and_duress_pin(client, db, ctx, first_choice):
    # Escalated: the real PIN closes it as a false alarm; the duress PIN only shows the cover.
    real, duress = await make_world(client, db), await make_world(client, db)
    for w in (real, duress):
        await to_state(client, db, ctx, w, "ESCALATED_L1")
    rr = await client.post(f"/v1/sessions/{real.session_id}/cancel", json={"pin": PIN}, headers=real.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/cancel", json={"pin": DURESS_PIN}, headers=duress.wearer)
    assert rr.content == rd.content
    tl_real, tl_duress = await visible(client, real), await visible(client, duress)
    assert tl_real[-1][1] == "false_alarm" and tl_real[-1][4]["through"] == states.FALSE_ALARM
    assert tl_real == tl_duress

    # A PIN during a lost-contact notice (before any escalation): same rule.
    real, duress = await make_world(client, db), await make_world(client, db)
    for w in (real, duress):
        await client.post("/v1/device/heartbeat", headers=w.device(), json={})
        async with db.connection() as conn:
            await conn.execute("UPDATE devices SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                               (w.device_id,))
    await timers.check_leases(db, ctx)
    for w in (real, duress):
        assert (await session_row(db, w.session_id))["link_state"] == states.LINK_LOST
        await fire(db, ctx, w.session_id, states.LOST_CONTACT_NOTIFY)
    rr = await client.post(f"/v1/sessions/{real.session_id}/cancel", json={"pin": PIN}, headers=real.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/cancel", json={"pin": DURESS_PIN}, headers=duress.wearer)
    assert rr.content == rd.content
    assert await visible(client, real) == await visible(client, duress)


async def advance(db, ctx, sid, seconds: float) -> None:
    """Move this session's clock forward: pending timers come `seconds` closer, due ones fire."""
    async with db.connection() as conn:
        await conn.execute(
            """UPDATE timers SET fire_at = fire_at - make_interval(secs => %s)
                WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL""", (float(seconds), sid))
        while await timers.fire_one(conn, ctx, session_id=sid) is not None:
            pass


async def device_view(client, w):
    """Heartbeat as the device sees it; the challenge id is random per world."""
    hb = (await client.post("/v1/device/heartbeat", headers=w.device(), json={})).json()
    return hb["state_public"], {**hb["cmd"], "challenge_id": bool(hb["cmd"]["challenge_id"])}


async def twin_worlds(client, db, **arm):
    real, duress = await make_world(client, db, arm=False), await make_world(client, db, arm=False)
    for w in (real, duress):
        r = await client.post("/v1/sessions", json={"profile": "demo", **arm}, headers=w.wearer)
        w.session_id = r.json()["session_id"]
    return real, duress


async def cancel_both(client, real, duress, action="cancel"):
    rr = await client.post(f"/v1/sessions/{real.session_id}/{action}", json={"pin": PIN}, headers=real.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/{action}", json={"pin": DURESS_PIN},
                           headers=duress.wearer)
    assert rr.status_code == rd.status_code == 200 and rr.content == rd.content
    assert (await session_row(client._transport.app.state.db, duress.session_id))["state"] == "ESCALATED_L1"


async def test_masked_duress_keeps_the_check_in_schedule(client, db, ctx, first_choice):
    real, duress = await twin_worlds(client, db)
    await cancel_both(client, real, duress)

    async def same(step):
        assert await wearer_texts(db, real) == await wearer_texts(db, duress), step
        assert await device_view(client, real) == await device_view(client, duress), step
        st_r = (await client.get(f"/v1/sessions/{real.session_id}", headers=real.wearer)).json()
        st_d = (await client.get(f"/v1/sessions/{duress.session_id}", headers=duress.wearer)).json()
        assert (st_r["state_public"], st_r["cmd"]["led"]) == (st_d["state_public"], st_d["cmd"]["led"]), step

    for w in (real, duress):
        await advance(db, ctx, w.session_id, 61)  # one check-in interval
    assert (await wearer_texts(db, real))[-1] == "Reminder"
    assert (await device_view(client, real))[0] == states.PROMPTED
    await same("routine check-in")
    for w in (real, duress):
        await advance(db, ctx, w.session_id, 43)  # prompt window + delivery grace
    assert (await wearer_texts(db, real))[-1] == "Reminder 1"
    await same("reminder")
    # The escalation kept running underneath the whole time.
    assert (await session_row(db, duress.session_id))["state"] in ("ESCALATED_L2", states.FINAL)
    # Unanswered, the twin escalates; so does the cover (both devices show red).
    for w in (real, duress):
        await advance(db, ctx, w.session_id, 43)
    assert (await device_view(client, real))[1]["led"] == (await device_view(client, duress))[1]["led"] == "red"
    assert await wearer_texts(db, real) == await wearer_texts(db, duress)


async def test_masked_duress_still_asks_home_safe_at_the_preset_end(client, db, ctx, first_choice):
    real, duress = await twin_worlds(client, db, duration_s=30)
    await cancel_both(client, real, duress)
    for w in (real, duress):
        await advance(db, ctx, w.session_id, 31)
    assert (await wearer_texts(db, real))[-1] == "Home safe?"
    assert await wearer_texts(db, real) == await wearer_texts(db, duress)
    assert await device_view(client, real) == await device_view(client, duress)
