"""DB integration tests: the KAZE flows end to end through the HTTP API and the timer worker."""
from __future__ import annotations

import json

from app import audit, states, timers
from tests.conftest import (ADMIN_KEY, DURESS_PIN, PIN, deliver_all, expire_leases, fire, make_world, open_alert,
                            outbox, pending_timers, session_row, to_state)


async def test_migrations_and_health(client):
    r = await client.get("/v1/health")
    assert r.status_code == 200 and r.json()["db"] == "up"
    assert r.json()["emergency_services"] == "SIMULATED (never placed)"


async def test_arm_no_response_l1_l2_final(client, db, ctx):
    w = await make_world(client, db)
    sid = w.session_id
    assert (await session_row(db, sid))["state"] == states.ARMED

    # CHECKIN_DUE -> PROMPTED; "Reminder" to the wearer; deadline starts at delivery.
    await fire(db, ctx, sid, states.CHECKIN_DUE)
    t = (await pending_timers(db, sid))[0]
    assert t["kind"] == states.PROMPT_TIMEOUT
    created_gap = (t["fire_at"] - t["created_at"]).total_seconds()
    assert 41 < created_gap < 43  # window 12 s + 30 s delivery grace
    await deliver_all(db, ctx)
    nudge = [o for o in await outbox(db, sid, recipient="wearer") if o["template"] == "reminder"][0]
    assert nudge["status"] == "sent" and nudge["channel"] == "log" and nudge["text"] == "Reminder"
    t = (await pending_timers(db, sid))[0]
    assert abs((t["fire_at"] - nudge["sent_at"]).total_seconds() - 12) < 0.5  # lowered to sent_at + window

    # PROMPT_TIMEOUT -> REMINDED ("Reminder N"); REMINDER_TIMEOUT -> L1.
    await fire(db, ctx, sid, states.PROMPT_TIMEOUT)
    s = await session_row(db, sid)
    assert s["state"] == states.REMINDED and s["challenge_n"] in (1, 3, 4)
    assert [o["text"] for o in await outbox(db, sid, recipient="wearer")][-1] == f"Reminder {s['challenge_n']}"

    c1, c2 = w.contacts
    st2 = await client.get(f"/v1/contact/{c2['token']}/status")
    assert st2.json() == {"alert": None, "approved": True}

    await fire(db, ctx, sid, states.REMINDER_TIMEOUT)
    assert (await session_row(db, sid))["state"] == "ESCALATED_L1"
    to_c1 = await outbox(db, sid, contact_id=c1["id"])
    assert [o["template"] for o in to_c1] == ["alert_no_response"]
    assert to_c1[0]["protect_content"] is True
    assert "hasn't responded to 2 check-ins" in to_c1[0]["text"]
    assert "This is not a confirmed emergency." in to_c1[0]["text"]
    assert "https://stillhere.test/l/" in to_c1[0]["text"]
    markup = to_c1[0]["reply_markup"]["inline_keyboard"]
    assert markup[0][0]["callback_data"].startswith("ack:") and markup[1][0]["callback_data"].startswith("pass:")
    assert markup[2][0]["url"].startswith("https://stillhere.test/c/")
    assert await outbox(db, sid, contact_id=c2["id"]) == []  # contact 2 not yet told
    assert (await client.get(f"/v1/contact/{c2['token']}/status")).json()["alert"] is None
    st1 = (await client.get(f"/v1/contact/{c1['token']}/status")).json()["alert"]
    assert st1["kind"] == "NO_RESPONSE" and st1["actions"] == ["ack", "pass"] and st1["location"]["simulated"] is False

    # ACK_TIMEOUT -> L2 (contact 2 told, contact 1 re-pinged) -> FINAL.
    await fire(db, ctx, sid, states.ACK_TIMEOUT)
    assert (await session_row(db, sid))["state"] == "ESCALATED_L2"
    assert [o["template"] for o in await outbox(db, sid, contact_id=c2["id"])] == ["alert_no_response"]
    assert len(await outbox(db, sid, contact_id=c1["id"])) == 2
    await fire(db, ctx, sid, states.ACK_TIMEOUT)
    s = await session_row(db, sid)
    assert s["state"] == states.FINAL
    finals = [o for o in await outbox(db, sid) if o["template"] == "alert_final"]
    assert sorted(o["contact_id"] for o in finals) == sorted([c1["id"], c2["id"]])
    assert "Emergency services: SIMULATED" in finals[0]["text"] and "No call is placed." in finals[0]["text"]
    st = (await client.get(f"/v1/contact/{c2['token']}/status")).json()["alert"]
    assert st["final"] is True and st["banner"].startswith("Emergency services: SIMULATED")
    async with db.connection() as conn:
        sim = await (await conn.execute(
            "SELECT detail FROM audit_log WHERE session_id = %s AND kind = 'emergency_services_simulated'",
            (sid,))).fetchone()
        assert sim["detail"]["placed"] is False
        chain = await audit.verify_chain(conn, audit.session_chain(sid))
    assert chain["ok"] and chain["count"] >= 6
    # Every outbound row is a message to the wearer or a contact; nothing else exists to call anyone.
    assert {o["recipient"] for o in await outbox(db, sid)} <= {"wearer", "contact"}


async def test_ack_followup_timeout_then_safe_resolves(client, db, ctx):
    w = await make_world(client, db)
    sid = w.session_id
    c1, c2 = w.contacts
    await to_state(client, db, ctx, w, "ESCALATED_L1")

    r = await client.post(f"/v1/contact/{c1['token']}/ack")
    assert r.json() == {"ok": True, "result": "acked"}
    assert (await session_row(db, sid))["state"] == states.ACKED
    a = await open_alert(db, sid)
    assert a["status"] == "ACKED" and a["acked_by"] == c1["id"]  # acknowledged, still open
    thanks = [o for o in await outbox(db, sid, contact_id=c1["id"]) if o["template"] == "ack_thanks"][0]
    assert [b[0]["callback_data"].split(":")[0] for b in thanks["reply_markup"]["inline_keyboard"]] == ["safe", "cant"]

    # Nobody confirms within the follow-up window: next contact, and the acker is re-pinged.
    await fire(db, ctx, sid, states.FOLLOWUP_TIMEOUT)
    assert (await session_row(db, sid))["state"] == "ESCALATED_L2"
    c1_templates = [o["template"] for o in await outbox(db, sid, contact_id=c1["id"])]
    assert c1_templates == ["alert_no_response", "ack_thanks", "alert_no_response"]
    assert [o["template"] for o in await outbox(db, sid, contact_id=c2["id"])] == ["alert_no_response"]

    # A contact who did not take it on cannot close it; the one who did, can.
    await client.post(f"/v1/contact/{c2['token']}/ack")
    assert (await client.post(f"/v1/contact/{c1['token']}/followup", json={"action": "safe"})).json()["result"] == "ignored"
    assert (await session_row(db, sid))["state"] == states.ACKED
    assert "C2 is handling it" in [o for o in await outbox(db, sid, contact_id=c1["id"])][-1]["text"]
    loc_link = [o for o in await outbox(db, sid, contact_id=c2["id"])][0]["text"].split(": ")[-1]
    share = loc_link.rsplit("/l/", 1)[1]
    assert (await client.get(f"/l/{share}")).status_code == 200

    r = await client.post(f"/v1/contact/{c2['token']}/followup", json={"action": "safe"})
    assert r.json()["result"] == "resolved"
    s = await session_row(db, sid)
    assert s["state"] == states.ARMED and s["public_state"] == states.ARMED
    async with db.connection() as conn:
        closed = await (await conn.execute(
            "SELECT status, resolved_by FROM alerts WHERE session_id = %s", (sid,))).fetchone()
        exp = await (await conn.execute(
            "SELECT expires_at - now() AS left FROM location_shares WHERE token_hash = encode(sha256(%s::bytea), 'hex')",
            (share,))).fetchone()
    assert closed["status"] == "RESOLVED" and closed["resolved_by"] == f"contact:{c2['id']}"
    assert 13 < exp["left"].total_seconds() <= 15  # link expires 15 s after resolution (demo)
    resolved = [o for o in await outbox(db, sid) if o["template"] == "resolved"]
    assert sorted(o["contact_id"] for o in resolved) == sorted([c1["id"], c2["id"]])
    assert (await client.get(f"/v1/contact/{c2['token']}/status")).json()["alert"] is None
    assert any(t["kind"] == states.CHECKIN_DUE for t in await pending_timers(db, sid))
    # After expiry the link is gone.
    async with db.connection() as conn:
        await conn.execute("UPDATE location_shares SET expires_at = now() - interval '1 second' "
                           "WHERE token_hash = encode(sha256(%s::bytea), 'hex')", (share,))
    assert (await client.get(f"/l/{share}")).status_code == 410


async def test_pass_it_on(client, db, ctx):
    w = await make_world(client, db)
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    r = await client.post(f"/v1/contact/{w.contacts[0]['token']}/followup", json={"action": "pass"})
    assert r.json()["result"] == "passed"
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L2"


async def _device_squeeze(client, w, event_id, pattern, k):
    return await client.post("/v1/device/events", headers=w.device(),
                             json={"event_id": event_id, "type": "squeeze", "payload": {"pattern": pattern, "k": k}})


async def test_duress_squeeze_is_masked_and_byte_identical(client, db, ctx):
    ok_w, du_w = await make_world(client, db), await make_world(client, db)
    for w in (ok_w, du_w):
        await to_state(client, db, ctx, w, states.PROMPTED)
        hb = await client.post("/v1/device/heartbeat", headers=w.device(), json={"enmo60_mg": 5})
        assert hb.json()["state_public"] == states.PROMPTED and hb.json()["cmd"]["led"] == "amber"

    r_ok = await _device_squeeze(client, ok_w, "b1:100", "plain", 2)
    r_du = await _device_squeeze(client, du_w, "b1:100", "duress", 3)
    assert r_ok.status_code == r_du.status_code == 200
    assert r_ok.content == r_du.content  # byte-identical device reply
    assert json.loads(r_ok.content) == {"cmd": {"blink_n": 0, "challenge_id": None, "led": "off"}, "ok": True,
                                        "result": "noted", "state_public": "ARMED"}
    hb_ok = (await client.post("/v1/device/heartbeat", headers=ok_w.device(), json={})).json()
    hb_du = (await client.post("/v1/device/heartbeat", headers=du_w.device(), json={})).json()
    assert (hb_ok["state_public"], hb_ok["cmd"]) == (hb_du["state_public"], hb_du["cmd"]) == ("ARMED", {
        "led": "off", "blink_n": 0, "challenge_id": None})

    # Inside: a masked DURESS alert at L1, contact 1 has the help request.
    s = await session_row(db, du_w.session_id)
    assert s["state"] == "ESCALATED_L1" and s["public_state"] == "ARMED"
    a = await open_alert(db, du_w.session_id)
    assert a["kind"] == "DURESS" and a["masked"] is True
    help_msg = (await outbox(db, du_w.session_id, contact_id=du_w.contacts[0]["id"]))[0]["text"]
    assert help_msg.startswith("StillHere: SILENT HELP REQUEST from Maya. She may not be able to talk. Don't call her phone.")

    # Wearer chat: identical text. Wearer views: identical.
    def wearer_texts(rows):
        return [o["text"] for o in rows if o["template"] in ("noted", "noted_alerting")]
    assert wearer_texts(await outbox(db, ok_w.session_id, recipient="wearer")) == ["Noted ✓"]
    assert wearer_texts(await outbox(db, du_w.session_id, recipient="wearer")) == ["Noted ✓"]
    st_ok = (await client.get(f"/v1/sessions/{ok_w.session_id}", headers=ok_w.wearer)).json()
    st_du = (await client.get(f"/v1/sessions/{du_w.session_id}", headers=du_w.wearer)).json()
    for k in ("state_public", "cmd", "link_state"):
        assert st_ok[k] == st_du[k]

    def strip(entries):
        return [(e["kind"], e["from_state"], e["to_state"], e["detail"]) for e in entries]
    tl_ok = (await client.get(f"/v1/sessions/{ok_w.session_id}/timeline", headers=ok_w.wearer)).json()["entries"]
    tl_du = (await client.get(f"/v1/sessions/{du_w.session_id}/timeline", headers=du_w.wearer)).json()["entries"]
    assert strip(tl_ok) == strip(tl_du)

    # The real PIN does not close a duress alert, and its reply matches a plain cancel.
    c_ok = await client.post(f"/v1/sessions/{ok_w.session_id}/cancel", json={"pin": PIN}, headers=ok_w.wearer)
    c_du = await client.post(f"/v1/sessions/{du_w.session_id}/cancel", json={"pin": PIN}, headers=du_w.wearer)
    assert c_ok.content == c_du.content
    assert (await open_alert(db, du_w.session_id))["status"] == "OPEN"
    assert any(o["template"] == "duress_pin_note" for o in await outbox(db, du_w.session_id))

    # Only a contact's "she's safe" closes it; then the hidden entries appear.
    c1 = du_w.contacts[0]
    await client.post(f"/v1/contact/{c1['token']}/ack")
    await client.post(f"/v1/contact/{c1['token']}/followup", json={"action": "safe"})
    assert await open_alert(db, du_w.session_id) is None
    tl_after = (await client.get(f"/v1/sessions/{du_w.session_id}/timeline", headers=du_w.wearer)).json()["entries"]
    assert "duress" in [e["kind"] for e in tl_after]


async def test_pin_cancel_real_and_duress_are_byte_identical(client, db, ctx):
    # Armed: real cancel vs duress cancel.
    a, b = await make_world(client, db), await make_world(client, db)
    ra = await client.post(f"/v1/sessions/{a.session_id}/cancel", json={"pin": PIN}, headers=a.wearer)
    rb = await client.post(f"/v1/sessions/{b.session_id}/cancel", json={"pin": DURESS_PIN}, headers=b.wearer)
    assert ra.status_code == rb.status_code == 200 and ra.content == rb.content
    assert ra.headers["content-length"] == rb.headers["content-length"]
    assert (await open_alert(db, a.session_id)) is None
    assert (await open_alert(db, b.session_id))["kind"] == "DURESS"
    assert [o["text"] for o in await outbox(db, a.session_id, recipient="wearer")] == \
           [o["text"] for o in await outbox(db, b.session_id, recipient="wearer")] == ["Cancelled ✓"]
    wrong = await client.post(f"/v1/sessions/{a.session_id}/cancel", json={"pin": "0000"}, headers=a.wearer)
    assert wrong.status_code == 403

    # During an escalation: real PIN = false alarm; duress PIN = masked, escalation continues.
    c, d = await make_world(client, db), await make_world(client, db)
    for w in (c, d):
        await to_state(client, db, ctx, w, "ESCALATED_L1")
    rc = await client.post(f"/v1/sessions/{c.session_id}/cancel", json={"pin": PIN}, headers=c.wearer)
    rd = await client.post(f"/v1/sessions/{d.session_id}/cancel", json={"pin": DURESS_PIN}, headers=d.wearer)
    assert rc.content == rd.content
    assert (await session_row(db, c.session_id))["state"] == states.ARMED
    sd = await session_row(db, d.session_id)
    assert sd["state"] == "ESCALATED_L1" and sd["public_state"] == "ARMED"
    assert any(o["template"] == "false_alarm" for o in await outbox(db, c.session_id))
    d_contact = [o["template"] for o in await outbox(db, d.session_id, recipient="contact")]
    assert "false_alarm" not in d_contact and "alert_duress" in d_contact

    # End with PIN: identical too.
    e, f = await make_world(client, db), await make_world(client, db)
    re_ = await client.post(f"/v1/sessions/{e.session_id}/end", json={"pin": PIN}, headers=e.wearer)
    rf = await client.post(f"/v1/sessions/{f.session_id}/end", json={"pin": DURESS_PIN}, headers=f.wearer)
    assert re_.content == rf.content
    assert (await session_row(db, e.session_id))["state"] == states.ENDED
    assert (await session_row(db, f.session_id))["state"] == "ESCALATED_L1"
    te = [o["text"] for o in await outbox(db, e.session_id, recipient="wearer")][0]
    tf = [o["text"] for o in await outbox(db, f.session_id, recipient="wearer")][0]
    assert te.startswith("Ended ✓ Timeline head: ") and tf.startswith("Ended ✓ Timeline head: ")
    assert len(te) == len(tf)
    # The device and the wearer's status see the same "ended" world.
    hb_e = (await client.post("/v1/device/heartbeat", headers=e.device(), json={})).json()
    hb_f = (await client.post("/v1/device/heartbeat", headers=f.device(), json={})).json()
    assert (hb_e["state_public"], hb_e["cmd"]) == (hb_f["state_public"], hb_f["cmd"]) == (
        "ENDED", {"led": "off", "blink_n": 0, "challenge_id": None})
    sq_e = await _device_squeeze(client, e, "b1:900", "plain", 2)
    sq_f = await _device_squeeze(client, f, "b1:900", "plain", 2)
    assert sq_e.content == sq_f.content
    st_e = (await client.get(f"/v1/sessions/{e.session_id}", headers=e.wearer)).json()
    st_f = (await client.get(f"/v1/sessions/{f.session_id}", headers=f.wearer)).json()
    assert (st_e["state_public"], st_e["cmd"]) == (st_f["state_public"], st_f["cmd"])
    # Arming again works for both; the masked alert keeps running underneath.
    ae = await client.post("/v1/sessions", json={}, headers=e.wearer)
    af = await client.post("/v1/sessions", json={}, headers=f.wearer)
    assert ae.status_code == af.status_code == 200
    assert ae.json()["state_public"] == af.json()["state_public"] == "ARMED"
    assert (await open_alert(db, f.session_id))["kind"] == "DURESS"


async def test_late_squeeze_never_stands_down(client, db, ctx):
    w = await make_world(client, db)
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    for i, (pattern, k) in enumerate((("plain", 2), ("plain", 3), ("duress", 3), ("unrecognized", 5))):
        r = await _device_squeeze(client, w, f"b1:{200 + i}", pattern, k)
        assert r.status_code == 200 and r.json()["state_public"] == "ESCALATED_L1"
        assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"
    r = await client.post(f"/v1/sessions/{w.session_id}/checkin", json={}, headers=w.wearer)
    assert r.status_code == 200
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"
    assert (await open_alert(db, w.session_id))["status"] == "OPEN"
    texts = [o["text"] for o in await outbox(db, w.session_id, contact_id=w.contacts[0]["id"])]
    assert any("responded on her device at" in t and "please still check on her" in t for t in texts)
    assert any("checked in on her phone" in t for t in texts)


async def test_event_replay_is_idempotent(client, db, ctx):
    w = await make_world(client, db)
    await to_state(client, db, ctx, w, states.PROMPTED)
    v0 = (await session_row(db, w.session_id))["version"]
    bodies = []
    for _ in range(5):
        r = await _device_squeeze(client, w, "b1:777", "plain", 2)
        bodies.append(r.content)
    assert len(set(bodies)) == 1
    s = await session_row(db, w.session_id)
    assert s["state"] == states.ARMED and s["version"] == v0 + 1  # one transition
    assert len([o for o in await outbox(db, w.session_id, recipient="wearer") if o["template"] == "noted"]) == 1
    # A replayed event after later changes still returns the original cached reply.
    await fire(db, ctx, w.session_id, states.CHECKIN_DUE)
    again = await _device_squeeze(client, w, "b1:777", "plain", 2)
    assert again.content == bodies[0]
    assert (await session_row(db, w.session_id))["state"] == states.PROMPTED


async def test_contacts_see_nothing_before_alert_and_must_be_approved(client, db, ctx):
    w = await make_world(client, db, contacts=1, pending=1)
    approved, pending = w.contacts[0], w.pending[0]
    assert (await client.get(f"/v1/contact/{approved['token']}/status")).json() == {"alert": None, "approved": True}
    assert (await client.get(f"/v1/contact/{pending['token']}/status")).json() == {"alert": None, "approved": False}
    listed = (await client.get("/v1/contacts", headers=w.wearer)).json()["contacts"]
    assert {c["status"] for c in listed} == {"approved", "pending_approval"}
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    await fire(db, ctx, w.session_id, states.ACK_TIMEOUT)  # past the only approved contact
    assert (await session_row(db, w.session_id))["state"] == states.FINAL
    assert await outbox(db, w.session_id, contact_id=pending["id"]) == []
    assert (await client.get(f"/v1/contact/{pending['token']}/status")).json()["alert"] is None
    assert (await client.post(f"/v1/contact/{pending['token']}/ack")).status_code == 404
    # Invites are single use.
    inv = (await client.post("/v1/contacts", json={"name": "X"}, headers=w.wearer)).json()
    tok = inv["web_link"].rsplit("/", 1)[1]
    assert (await client.post(f"/v1/contact/{tok}/join", json={})).status_code == 200
    assert (await client.post(f"/v1/contact/{tok}/join", json={})).status_code == 410
    assert inv["qr_png"].startswith("data:image/png;base64,")


async def test_removed_contact_is_silent_and_gets_nothing(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    c1, c2 = w.contacts
    r = await client.delete(f"/v1/contacts/{c1['id']}", headers=w.wearer)
    assert r.status_code == 200
    assert await outbox(db, None, contact_id=c1["id"]) == [] or all(
        o["template"] == "contact_approved" for o in await outbox(db, None, contact_id=c1["id"]))
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    assert [o["template"] for o in await outbox(db, w.session_id, contact_id=c2["id"])] == ["alert_no_response"]
    assert await outbox(db, w.session_id, contact_id=c1["id"]) == []
    assert (await client.get(f"/v1/contact/{c1['token']}/status")).status_code == 404


async def test_lost_contact_notice_and_restore(client, db, ctx):
    w = await make_world(client, db)
    sid = w.session_id
    hb = await client.post("/v1/device/heartbeat", headers=w.device(), json={"enmo60_mg": 3})
    assert hb.status_code == 200
    await expire_leases(db, ctx, w)
    s = await session_row(db, sid)
    assert s["link_state"] == "LOST_CONTACT" and s["state"] == states.ARMED
    kinds = [t["kind"] for t in await pending_timers(db, sid)]
    assert states.CHECKIN_DUE in kinds and states.LOST_CONTACT_NOTIFY in kinds  # deadlines keep running
    assert (await outbox(db, sid, recipient="wearer"))[-1]["template"] == "wearer_lost_contact"
    assert await outbox(db, sid, recipient="contact") == []  # grace first

    await fire(db, ctx, sid, states.LOST_CONTACT_NOTIFY)
    c1 = w.contacts[0]
    note = (await outbox(db, sid, contact_id=c1["id"]))[-1]
    assert note["template"] == "lost_contact"
    assert "We lost contact with Maya's device at" in note["text"] and "Cause unknown." in note["text"]
    assert "Her check-in schedule is still running." in note["text"]
    st = (await client.get(f"/v1/contact/{c1['token']}/status")).json()["alert"]
    assert st["kind"] == "LOST_CONTACT" and st["actions"] == []

    hb = await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    assert hb.status_code == 200
    s = await session_row(db, sid)
    assert s["link_state"] == "OK"
    assert (await outbox(db, sid, contact_id=c1["id"]))[-1]["template"] == "restored"
    async with db.connection() as conn:
        lc = await (await conn.execute("SELECT status FROM alerts WHERE session_id = %s AND kind = 'LOST_CONTACT'",
                                       (sid,))).fetchone()
    assert lc["status"] == "RESTORED"


async def test_lost_contact_during_alert_keeps_escalating(client, db, ctx):
    w = await make_world(client, db)
    sid = w.session_id
    await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    await expire_leases(db, ctx, w)
    c1 = w.contacts[0]
    assert (await outbox(db, sid, contact_id=c1["id"]))[-1]["template"] == "lost_contact"  # immediately
    await fire(db, ctx, sid, states.ACK_TIMEOUT)  # deadline still fires
    assert (await session_row(db, sid))["state"] == "ESCALATED_L2"
    await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    assert (await open_alert(db, sid))["kind"] == "NO_RESPONSE"  # restore never closes an escalation
    assert (await session_row(db, sid))["state"] == "ESCALATED_L2"


async def test_stale_timer_is_ignored(client, db, ctx):
    w = await make_world(client, db)
    async with db.connection() as conn:
        await conn.execute(
            """INSERT INTO timers (session_id, kind, fire_at, expected_version)
               VALUES (%s, 'REMINDER_TIMEOUT', now(), 999)""", (w.session_id,))
        await timers.fire_one(conn, ctx, session_id=w.session_id)
        r = await (await conn.execute(
            "SELECT result FROM timers WHERE session_id = %s AND expected_version = 999", (w.session_id,))).fetchone()
    assert r["result"] == "stale"
    assert (await session_row(db, w.session_id))["state"] == states.ARMED


async def test_skip_locked_lets_one_worker_take_a_timer(client, db, ctx):
    w = await make_world(client, db)
    async with db.connection() as conn:
        await conn.execute("UPDATE timers SET fire_at = now() WHERE session_id = %s AND kind = 'CHECKIN_DUE'",
                           (w.session_id,))
    # Another worker holds the row lock: we skip it instead of waiting.
    async with db.connection() as holder:
        async with holder.transaction():
            await holder.execute(
                """SELECT t.id FROM timers t JOIN sessions s ON s.id = t.session_id
                    WHERE t.session_id = %s AND t.kind = 'CHECKIN_DUE' AND t.fired_at IS NULL
                    FOR UPDATE OF t, s""", (w.session_id,))
            async with db.connection() as other:
                assert await timers.fire_one(other, ctx, session_id=w.session_id) is None
    # Two workers race: exactly one fires it.
    import asyncio

    async def worker():
        async with db.connection() as conn:
            return await timers.fire_one(conn, ctx, session_id=w.session_id)
    results = await asyncio.gather(worker(), worker())
    assert sum(r is not None for r in results) == 1
    assert (await session_row(db, w.session_id))["state"] == states.PROMPTED
    assert len([o for o in await outbox(db, w.session_id) if o["template"] == "reminder"]) == 1


async def test_audit_log_is_append_only_and_chain_catches_edits(client, db, ctx):
    w = await make_world(client, db)
    await to_state(client, db, ctx, w, states.REMINDED)
    chain = audit.session_chain(w.session_id)
    import psycopg
    async with db.connection() as conn:
        for sql in ("UPDATE audit_log SET kind = 'x' WHERE chain_key = %s",
                    "DELETE FROM audit_log WHERE chain_key = %s"):
            try:
                async with conn.transaction():
                    await conn.execute(sql, (chain,))
                raise AssertionError("audit_log accepted a change")
            except psycopg.errors.RaiseException as exc:
                assert "append-only" in str(exc)
        try:
            async with conn.transaction():
                await conn.execute("TRUNCATE audit_log")
            raise AssertionError("audit_log accepted TRUNCATE")
        except psycopg.errors.RaiseException:
            pass
        assert (await audit.verify_chain(conn, chain))["ok"]
        # Simulate an out-of-band edit (superuser disables the trigger) and recompute.
        async with conn.transaction():
            await conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_append_only")
            row = await (await conn.execute(
                "SELECT id FROM audit_log WHERE chain_key = %s ORDER BY id LIMIT 1 OFFSET 1", (chain,))).fetchone()
            await conn.execute("UPDATE audit_log SET detail = '{\"edited\": true}' WHERE id = %s", (row["id"],))
            await conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_append_only")
        res = await audit.verify_chain(conn, chain)
    assert res["ok"] is False and res["bad_id"] == row["id"]


async def test_auth_is_required(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    bad = {"Authorization": "Bearer nope", "X-Device-Id": w.device_id}
    assert (await client.post("/v1/device/heartbeat", headers=bad, json={})).status_code == 401
    assert (await client.post("/v1/device/events", headers=bad,
                              json={"event_id": "x:1", "type": "squeeze"})).status_code == 401
    assert (await client.post("/v1/sessions", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.post("/v1/demo/reset")).status_code == 401
    other = await make_world(client, db, contacts=0)
    assert (await client.get(f"/v1/sessions/{other.session_id}", headers=w.wearer)).status_code == 404


async def test_demo_reset_is_fast_and_rearms(client, db, ctx):
    import time
    h = {"X-Admin-Key": ADMIN_KEY}
    t0 = time.monotonic()
    r = await client.post("/v1/demo/reset", headers=h, json={})
    assert r.status_code == 200, r.text
    assert time.monotonic() - t0 < 5
    sid = r.json()["armed"]["session_id"]
    assert (await session_row(db, sid))["state"] == states.ARMED
    r = await client.post("/v1/demo/inject", headers=h, json={"kind": "head_position"})
    assert r.json()["result"] == "prompted" and r.json()["label"] == "head position change → early check-in"
    s = await session_row(db, sid)
    assert s["state"] == states.PROMPTED and s["challenge_n"] in (1, 3, 4)
    ff = await client.post("/v1/demo/fast-forward", headers=h)
    assert ff.json()["fired_next"] == states.PROMPT_TIMEOUT
    async with db.connection() as conn:
        await timers.fire_one(conn, ctx, session_id=sid)
    assert (await session_row(db, sid))["state"] == states.REMINDED
    timers_list = (await client.get("/v1/admin/timers", headers=h)).json()["timers"]
    assert any(t["session_id"] == sid for t in timers_list)
    inv = (await client.post("/v1/demo/judge-invite", headers=h)).json()
    assert inv["web_link"].startswith("https://stillhere.test/c/")
    # Reset again closes everything and re-arms.
    r2 = await client.post("/v1/demo/reset", headers=h, json={})
    assert (await session_row(db, sid))["state"] == states.ENDED
    assert (await session_row(db, r2.json()["armed"]["session_id"]))["state"] == states.ARMED


async def test_simulated_heart_rate_is_badged_and_only_pulls_forward(client, db, ctx):
    from app import sensors
    h = {"X-Admin-Key": ADMIN_KEY}
    sid = (await client.post("/v1/demo/reset", headers=h, json={})).json()["armed"]["session_id"]
    r = await client.post("/v1/demo/hr-source", headers=h, json={"mode": "sim:climb_still"})
    assert r.json()["simulated"] is True
    async with db.connection() as conn:
        await conn.execute("""UPDATE bot_state SET value = jsonb_set(value, '{since}', to_jsonb(now() - interval '30 seconds'))
                               WHERE key = 'hr_source'""")
    for _ in range(3):
        assert await sensors.simulate_hr(db, ctx) >= 1
    await sensors.evaluate_hr(db, ctx, force=True)
    async with db.connection() as conn:
        await conn.execute("UPDATE sessions SET hr_deviating_since = now() - interval '20 seconds' WHERE id = %s", (sid,))
    await sensors.evaluate_hr(db, ctx, force=True)
    s = await session_row(db, sid)
    assert s["state"] == states.PROMPTED and s["prompt_cause"] == "challenge"
    async with db.connection() as conn:
        pf = await (await conn.execute(
            "SELECT detail FROM audit_log WHERE session_id = %s AND kind = 'pull_forward'", (sid,))).fetchone()
        src = await (await conn.execute(
            "SELECT DISTINCT source, simulated FROM hr_samples WHERE time > now() - interval '1 minute' AND source = 'sim'")).fetchall()
    assert pf["detail"]["simulated"] is True
    assert [(r["source"], r["simulated"]) for r in src] == [("sim", True)]
    await client.post("/v1/demo/hr-source", headers=h, json={"mode": "live"})


async def test_prompt_shown_starts_the_deadline_and_stale_heartbeats_do_not_extend_the_lease(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, states.PROMPTED)
    before = (await pending_timers(db, w.session_id))[0]
    r = await client.post("/v1/device/events", headers=w.device(),
                          json={"event_id": "b1:50", "type": "prompt_shown", "payload": {}})
    assert r.json()["result"] == "deadline_set"
    after = (await pending_timers(db, w.session_id))[0]
    async with db.connection() as conn:
        left = (await (await conn.execute("SELECT %s::timestamptz - now() AS d", (after["fire_at"],))).fetchone())["d"]
    assert after["fire_at"] < before["fire_at"] and 11 < left.total_seconds() <= 12

    first = await client.post("/v1/device/heartbeat", headers=w.device(seq=100), json={})
    assert first.status_code == 200
    async with db.connection() as conn:
        lease1 = (await (await conn.execute("SELECT lease_expires_at FROM devices WHERE id = %s",
                                            (w.device_id,))).fetchone())["lease_expires_at"]
    replay = await client.post("/v1/device/heartbeat", headers=w.device(seq=100), json={})
    assert replay.status_code == 200
    async with db.connection() as conn:
        lease2 = (await (await conn.execute("SELECT lease_expires_at FROM devices WHERE id = %s",
                                            (w.device_id,))).fetchone())["lease_expires_at"]
    assert lease2 == lease1  # a replayed seq does not keep the link alive


async def test_session_end_during_an_alert_still_asks_home_safe_afterwards(client, db, ctx):
    w = await make_world(client, db, arm=False)
    r = await client.post("/v1/sessions", json={"profile": "demo", "duration_s": 30}, headers=w.wearer)
    w.session_id = r.json()["session_id"]
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    # The preset end time passes while the alert is open: it waits instead of being dropped.
    assert await fire(db, ctx, w.session_id, states.SESSION_END_DUE) == "deferred"
    assert states.SESSION_END_DUE in [t["kind"] for t in await pending_timers(db, w.session_id)]
    c1 = w.contacts[0]
    await client.post(f"/v1/contact/{c1['token']}/ack")
    assert (await client.post(f"/v1/contact/{c1['token']}/followup", json={"action": "safe"})).json()["result"] == "resolved"
    assert (await session_row(db, w.session_id))["state"] == states.ARMED
    assert await fire(db, ctx, w.session_id, states.SESSION_END_DUE) == "prompted"
    assert [o["text"] for o in await outbox(db, w.session_id, recipient="wearer")][-1] == "Home safe?"


async def test_out_of_range_numbers_get_4xx_never_500(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, states.PROMPTED)
    raw = '{"event_id":"b1:880","type":"squeeze","payload":{"pattern":"plain","k":1e999}}'
    for _ in range(3):  # the device retries the same event
        r = await client.post("/v1/device/events", content=raw,
                              headers={**w.device(), "Content-Type": "application/json"})
        assert r.status_code == 422, r.text
    r = await client.post("/v1/device/events", headers=w.device(),
                          json={"event_id": "b1:881", "type": "squeeze", "payload": {}, "device_ms": 2 ** 63})
    assert r.status_code == 422
    dev = {"Authorization": f"Bearer {w.device_key}", "X-Device-Id": w.device_id}
    r = await client.post("/v1/hr", headers=dev, json={"source": "x", "samples": [{"bpm": 70, "rr_ms": [1e39]}]})
    assert r.status_code == 422
    # "I'm OK" with a nonsense accuracy still counts: the accuracy is dropped, not the check-in.
    r = await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer,
                          json={"lat": 43.7, "lon": -79.4, "accuracy_m": 1e39})
    assert r.status_code == 200 and r.json()["state_public"] == states.ARMED
    assert (await session_row(db, w.session_id))["state"] == states.ARMED
