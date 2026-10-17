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
