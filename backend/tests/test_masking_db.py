"""Duress masking end to end: a session cancelled or ended with the duress PIN must look, to the
wearer and her device, exactly like a twin session that used the real PIN.

Each test builds two worlds (real PIN and duress PIN), drives both through the same inputs and
compares what the wearer can see: HTTP bodies, device replies, wearer messages and the visible
timeline.
"""
from __future__ import annotations

import pytest

from app import states, timers
from tests.conftest import DURESS_PIN, PIN, fire, make_world, outbox, pending_timers, session_row, to_state


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
    if isinstance(value, list):
        return [_norm(v, w) for v in value]
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


def _norm_reply(r):
    body = r.json()
    if body.get("cmd"):
        body["cmd"] = {**body["cmd"], "challenge_id": bool(body["cmd"]["challenge_id"])}
    return r.status_code, body


@pytest.mark.parametrize("kind", ["head_position_change", "offbody", "fall_like"])
async def test_sensor_events_during_masked_duress_prompt_like_armed(client, db, ctx, first_choice, kind):
    real, duress = await twin_worlds(client, db)
    await cancel_both(client, real, duress)
    replies = []
    for w in (real, duress):
        r = await client.post("/v1/device/events", headers=w.device(),
                              json={"event_id": "b1:500", "type": kind, "payload": {}})
        replies.append(_norm_reply(r))
    assert replies[0][1]["result"] == "prompted" and replies[0][1]["state_public"] == states.PROMPTED
    assert replies[0] == replies[1]
    assert await wearer_texts(db, real) == await wearer_texts(db, duress)
    assert await device_view(client, real) == await device_view(client, duress)
    assert await visible(client, real) == await visible(client, duress)
    # The second event is in cooldown for both.
    again = [_norm_reply(await client.post("/v1/device/events", headers=w.device(),
                                           json={"event_id": "b1:501", "type": kind, "payload": {}}))
             for w in (real, duress)]
    assert again[0] == again[1]


async def test_heart_rate_rule_during_masked_duress_prompts_like_armed(client, db, ctx, first_choice):
    from app import sensors
    real, duress = await twin_worlds(client, db)
    await cancel_both(client, real, duress)
    async with db.connection() as conn:
        for w in (real, duress):
            await conn.execute("INSERT INTO user_baselines (user_id, kind, value) VALUES (%s, 'hr_rest', 60)",
                               (w.user_id,))
            await conn.execute(
                """INSERT INTO hr_samples (time, user_id, source, bpm)
                   SELECT now() - make_interval(secs => s), %s, 'test', 120 FROM generate_series(0, 50, 5) s""",
                (w.user_id,))
    for w in (real, duress):
        await client.post("/v1/device/heartbeat", headers=w.device(), json={"enmo60_mg": 5})
    await sensors.evaluate_hr(db, ctx, force=True)
    async with db.connection() as conn:
        for w in (real, duress):
            await conn.execute("UPDATE sessions SET hr_deviating_since = now() - interval '600 seconds' WHERE id = %s",
                               (w.session_id,))
    await sensors.evaluate_hr(db, ctx, force=True)
    assert (await device_view(client, real))[0] == states.PROMPTED
    assert await device_view(client, real) == await device_view(client, duress)
    assert await wearer_texts(db, real) == await wearer_texts(db, duress)
    assert (await session_row(db, duress.session_id))["state"].startswith("ESCALATED_")


async def _squeeze(client, w, event_id, pattern, k):
    return await client.post("/v1/device/events", headers=w.device(),
                             json={"event_id": event_id, "type": "squeeze", "payload": {"pattern": pattern, "k": k}})


async def test_wearer_input_during_masked_duress_is_answered_like_armed(client, db, ctx, first_choice):
    real, duress = await twin_worlds(client, db)
    await cancel_both(client, real, duress)
    inputs = [("b1:600", "unrecognized", 5), ("b1:601", "plain", 2), ("b1:602", "duress", 3)]
    for event_id, pattern, k in inputs:
        rr, rd = await _squeeze(client, real, event_id, pattern, k), await _squeeze(client, duress, event_id, pattern, k)
        assert rr.content == rd.content, (pattern, rr.content, rd.content)
    cr = await client.post(f"/v1/sessions/{real.session_id}/checkin", json={}, headers=real.wearer)
    cd = await client.post(f"/v1/sessions/{duress.session_id}/checkin", json={}, headers=duress.wearer)
    assert cr.content == cd.content
    assert await wearer_texts(db, real) == await wearer_texts(db, duress)
    assert await visible(client, real) == await visible(client, duress)
    # A cover prompt answered with the right count goes back to ARMED, like the twin.
    for w in (real, duress):
        await advance(db, ctx, w.session_id, 61)
    rr, rd = await _squeeze(client, real, "b1:603", "plain", 2), await _squeeze(client, duress, "b1:603", "plain", 2)
    assert rr.content == rd.content and rr.json()["state_public"] == states.ARMED
    assert await wearer_texts(db, real) == await wearer_texts(db, duress)
    assert await visible(client, real) == await visible(client, duress)
    # The contacts of the duress world still hear that she responded, and the alert stays open.
    c1 = [o["template"] for o in await outbox(db, duress.session_id, contact_id=duress.contacts[0]["id"])]
    assert "late_device" in c1 and "late_phone" in c1
    assert (await session_row(db, duress.session_id))["state"].startswith("ESCALATED_")


async def test_after_a_duress_end_wearer_input_gets_no_wearer_message(client, db, ctx):
    real, duress = await twin_worlds(client, db)
    await cancel_both(client, real, duress, action="end")
    rr, rd = await _squeeze(client, real, "b1:700", "plain", 2), await _squeeze(client, duress, "b1:700", "plain", 2)
    assert rr.content == rd.content
    cr = await client.post(f"/v1/sessions/{real.session_id}/checkin", json={}, headers=real.wearer)
    cd = await client.post(f"/v1/sessions/{duress.session_id}/checkin", json={}, headers=duress.wearer)
    assert cr.content == cd.content
    assert [t.split(" Timeline")[0] for t in await wearer_texts(db, real)] == \
           [t.split(" Timeline")[0] for t in await wearer_texts(db, duress)] == ["Ended ✓"]
    assert await visible(client, real) == await visible(client, duress)


@pytest.mark.parametrize("action", ["cancel", "end"])
@pytest.mark.parametrize("pin", [PIN, DURESS_PIN])
async def test_a_pin_after_a_duress_end_answers_like_an_ended_session(client, db, ctx, action, pin):
    real, duress = await twin_worlds(client, db)
    await cancel_both(client, real, duress, action="end")
    rr = await client.post(f"/v1/sessions/{real.session_id}/{action}", json={"pin": pin}, headers=real.wearer)
    rd = await client.post(f"/v1/sessions/{duress.session_id}/{action}", json={"pin": pin}, headers=duress.wearer)
    assert rr.status_code == rd.status_code == 200 and rr.content == rd.content
    assert rr.json()["state_public"] == states.ENDED
    for w in (real, duress):
        st = (await client.get(f"/v1/sessions/{w.session_id}", headers=w.wearer)).json()
        assert st["state_public"] == states.ENDED
    assert [t.split(" Timeline")[0] for t in await wearer_texts(db, real)] == \
           [t.split(" Timeline")[0] for t in await wearer_texts(db, duress)]
    assert await visible(client, real) == await visible(client, duress)
    assert (await session_row(db, duress.session_id))["state"].startswith("ESCALATED_")


@pytest.mark.parametrize("action", ["end", "cancel"])
async def test_device_unplugged_after_a_duress_pin_messages_the_wearer_like_the_twin(client, db, ctx, action):
    real, duress = await twin_worlds(client, db)
    for w in (real, duress):
        await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    await cancel_both(client, real, duress, action=action)
    async with db.connection() as conn:
        for w in (real, duress):
            await conn.execute("UPDATE devices SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                               (w.device_id,))
    await timers.check_leases(db, ctx)
    for w in (real, duress):
        await client.post("/v1/device/heartbeat", headers=w.device(), json={})  # plugged back in

    def heads_removed(texts):
        return [t.split(" Timeline")[0] for t in texts]
    assert heads_removed(await wearer_texts(db, real)) == heads_removed(await wearer_texts(db, duress))
    assert await visible(client, real) == await visible(client, duress)
    # The duress world's contacts were told the link dropped and came back.
    c1 = [o["template"] for o in await outbox(db, duress.session_id, contact_id=duress.contacts[0]["id"])]
    assert "lost_contact" in c1 and "restored" in c1


async def test_arming_again_after_a_duress_end_starts_a_fresh_session(client, db, ctx):
    real, duress = await twin_worlds(client, db)
    old = {real.user_id: real.session_id, duress.user_id: duress.session_id}
    await cancel_both(client, real, duress, action="end")
    arms = {}
    for w in (real, duress):
        r = await client.post("/v1/sessions", json={"preset": "night_out"}, headers=w.wearer)
        assert r.status_code == 200, r.text
        arms[w.user_id] = r.json()
    a_real, a_duress = arms[real.user_id], arms[duress.user_id]
    assert a_duress["session_id"] != old[duress.user_id] and a_real["session_id"] != old[real.user_id]
    assert a_real["end_at"] and a_duress["end_at"]
    assert _norm({k: v for k, v in a_real.items() if k not in ("session_id", "end_at")}, real) == \
           _norm({k: v for k, v in a_duress.items() if k not in ("session_id", "end_at")}, duress)
    for w in (real, duress):
        new = arms[w.user_id]["session_id"]
        kinds = sorted(t["kind"] for t in await pending_timers(db, new))
        assert kinds == [states.CHECKIN_DUE, states.SESSION_END_DUE]
        st = (await client.get(f"/v1/sessions/{old[w.user_id]}", headers=w.wearer)).json()
        assert st["state_public"] == states.ENDED
    assert await visible(client, real, a_real["session_id"]) == await visible(client, duress, a_duress["session_id"])
    assert await visible(client, real, old[real.user_id]) == await visible(client, duress, old[duress.user_id])
    ex_r = (await client.get("/v1/me/export", headers=real.wearer)).json()["sessions"]
    ex_d = (await client.get("/v1/me/export", headers=duress.wearer)).json()["sessions"]
    assert len(ex_r) == len(ex_d) == 2
    assert [len(s["entries"]) for s in ex_r] == [len(s["entries"]) for s in ex_d]
    # Underneath, the masked alert of the first session keeps escalating.
    assert (await session_row(db, old[duress.user_id]))["state"].startswith("ESCALATED_")
    # The device now talks to the new session in both worlds.
    assert await device_view(client, real) == await device_view(client, duress) == \
        (states.ARMED, {"led": "off", "blink_n": 0, "challenge_id": False})


def recompute(session_id, entries) -> str:
    """What anyone holding the export can compute from the entries it lists (app/audit.py is public)."""
    from datetime import datetime

    from app import audit
    prev = audit.GENESIS
    for e in entries:
        payload = audit.entry_payload(chain_key=audit.session_chain(session_id), at=datetime.fromisoformat(e["at"]),
                                      actor=e["actor"], kind=e["kind"], from_state=e["from_state"],
                                      to_state=e["to_state"], detail=e["detail"], masked=e.get("masked", False))
        prev = audit.compute_hash(prev, payload)
    return prev


@pytest.mark.parametrize("action", ["cancel", "end"])
async def test_export_head_hash_recomputes_from_the_visible_entries_for_both_pins(client, db, ctx, action):
    real, duress = await twin_worlds(client, db)
    await cancel_both(client, real, duress, action=action)
    for w in (real, duress):
        ex = (await client.get("/v1/me/export", headers=w.wearer)).json()
        s = ex["sessions"][-1]
        assert s["chain_ok"] is True
        assert recompute(s["session_id"], s["entries"]) == s["head_hash"], w is duress
        if action == "end":
            msg = [t for t in await wearer_texts(db, w) if t.startswith("Ended ✓")][-1]
            assert msg == f"Ended ✓ Timeline head: {s['head_hash'][:16]}", w is duress


async def test_export_shows_ended_at_after_either_pin(client, db, ctx):
    real, duress = await twin_worlds(client, db)
    await cancel_both(client, real, duress, action="end")
    ex_r = (await client.get("/v1/me/export", headers=real.wearer)).json()["sessions"]
    ex_d = (await client.get("/v1/me/export", headers=duress.wearer)).json()["sessions"]
    assert ex_r[-1]["ended_at"] is not None and ex_d[-1]["ended_at"] is not None
    assert [sorted(s) for s in ex_r] == [sorted(s) for s in ex_d]


async def test_operator_entries_stay_hidden_while_a_masked_alert_is_open(client, db, ctx):
    from tests.conftest import ADMIN_KEY
    h = {"X-Admin-Key": ADMIN_KEY}
    wearer = {"Authorization": "Bearer demo-wearer-key"}
    sid = (await client.post("/v1/demo/reset", headers=h, json={})).json()["armed"]["session_id"]
    r = await client.post(f"/v1/sessions/{sid}/cancel", json={"pin": DURESS_PIN}, headers=wearer)
    assert r.json()["state_public"] == states.ARMED
    before = (await client.get(f"/v1/sessions/{sid}/timeline", headers=wearer)).json()["entries"]
    assert (await client.post("/v1/demo/fast-forward", headers=h)).json()["ok"] is True
    assert (await client.post("/v1/demo/profile", headers=h, json={"profile": "demo"})).json()["ok"] is True
    after = (await client.get(f"/v1/sessions/{sid}/timeline", headers=wearer)).json()["entries"]
    assert after == before
    async with db.connection() as conn:
        rows = await (await conn.execute(
            """SELECT kind, masked, alert_id FROM audit_log WHERE session_id = %s
                AND kind IN ('demo_fast_forward', 'profile_set')""", (sid,))).fetchall()
    assert len(rows) == 2 and all(r["masked"] and r["alert_id"] is not None for r in rows)


async def test_public_health_does_not_change_with_a_duress_pin(client, db, ctx):
    from tests.conftest import ADMIN_KEY
    w = await make_world(client, db)

    def public(r):
        assert r.status_code == 200
        return {k: v for k, v in r.json().items() if k != "server_time"}
    before = public(await client.get("/v1/health"))
    await client.post(f"/v1/sessions/{w.session_id}/cancel", json={"pin": DURESS_PIN}, headers=w.wearer)
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"
    assert public(await client.get("/v1/health")) == before
    assert "pending_timers" not in before and "pending_outbox" not in before
    # The operator still gets the queue sizes.
    op = (await client.get("/v1/health", headers={"X-Admin-Key": ADMIN_KEY})).json()
    assert isinstance(op["pending_timers"], int) and isinstance(op["pending_outbox"], int)
    assert "pending_timers" not in (await client.get("/v1/health", headers={"X-Admin-Key": "wrong"})).json()
