"""Contacts and wearer inputs that must not be able to do more than they should: removed contacts lose
the location link and anything still in flight, a contact acts only on its own wearer's alerts, a
wearer arms only with her own device, one contact cannot flood the others, and input the database
cannot store is a 4xx (never a 500 that a device would retry forever)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import states, telegram
from app.outbox import Sender, TelegramAPI
from tests.conftest import fire, make_world, open_alert, outbox, session_row, to_state


async def _link(client, c) -> str:
    loc = (await client.get(f"/v1/contact/{c['token']}/status")).json()["alert"]["location"]
    return "/l/" + loc["link"].rsplit("/l/", 1)[1]


# --- a removed contact's location link stops working ------------------------------------------------
@pytest.mark.parametrize("remove", ["one", "all"])
async def test_removing_a_contact_ends_their_location_link(client, db, ctx, remove):
    w = await make_world(client, db, contacts=2)
    c1, c2 = w.contacts
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    assert (await client.post(f"/v1/contact/{c1['token']}/followup", json={"action": "pass"})).json()["result"] == "passed"
    l1, l2 = await _link(client, c1), await _link(client, c2)
    assert (await client.get(l1)).status_code == (await client.get(l2)).status_code == 200
    # The link in the alert message is the contact's own link, the same one their page shows.
    msg = [o["text"] for o in await outbox(db, w.session_id, contact_id=c1["id"]) if o["template"] == "alert_no_response"]
    assert msg and all(l1 in t for t in msg)
    if remove == "one":
        assert (await client.delete(f"/v1/contacts/{c1['id']}", headers=w.wearer)).status_code == 200
        assert (await client.get(l1)).status_code == 410
        assert (await client.get(l2)).status_code == 200          # the others keep theirs
    else:
        assert (await client.delete("/v1/contacts", headers=w.wearer)).json()["removed"] == 2
        assert [(await client.get(x)).status_code for x in (l1, l2)] == [410, 410]
    assert (await open_alert(db, w.session_id))["status"] == "OPEN"   # removal never stands the alert down


# --- a removed contact gets nothing still in flight --------------------------------------------------
@pytest.mark.parametrize("remove", ["one", "all"])
async def test_a_message_in_flight_when_the_contact_is_removed_is_never_sent(client, db, ctx, remove):
    w = await make_world(client, db, contacts=1)
    c1 = w.contacts[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = 7301 WHERE id = %s", (c1["id"],))
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    async with db.connection() as conn:   # only C1's alert is left to send
        await conn.execute("""UPDATE outbox SET status = 'sent', channel = 'test-skip'
                               WHERE status IN ('pending', 'sending') AND NOT (contact_id = %s AND template = 'alert_no_response')""",
                           (c1["id"],))
    sent_to = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent_to.append(body.get("chat_id"))
        if len(sent_to) == 1:   # the wearer removes C1 while this send is in flight; Telegram then fails
            url = f"/v1/contacts/{c1['id']}" if remove == "one" else "/v1/contacts"
            assert (await client.delete(url, headers=w.wearer)).status_code == 200
            return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sender = Sender(db, ctx, TelegramAPI("123:TEST", http))
        assert await sender.run_once() == 1
        row = (await outbox(db, w.session_id, contact_id=c1["id"]))[0]
        assert row["status"] == "failed", row["status"]
        async with db.connection() as conn:
            await conn.execute("UPDATE outbox SET next_attempt_at = now() WHERE id = %s", (row["id"],))
        sender._last_by_chat.clear()
        await sender.run_once()
    assert sent_to == [7301]                                   # never retried after the removal


async def test_the_sender_never_sends_to_a_removed_contact(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    c1 = w.contacts[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = 7302 WHERE id = %s", (c1["id"],))
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    async with db.connection() as conn:   # removed by a path that left its row queued
        await conn.execute("UPDATE contacts SET revoked_at = now() WHERE id = %s", (c1["id"],))
        await conn.execute("""UPDATE outbox SET status = 'sent', channel = 'test-skip'
                               WHERE status IN ('pending', 'sending') AND NOT (contact_id = %s)""", (c1["id"],))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await Sender(db, ctx, TelegramAPI("123:TEST", http)).run_once()
    assert calls == []
    assert {r["status"] for r in await outbox(db, w.session_id, contact_id=c1["id"])} == {"failed"}


# --- a contact acts only on its own wearer's alerts (Telegram) ---------------------------------------
async def test_a_telegram_button_cannot_touch_another_wearers_alert(client, db, ctx):
    ana, bea = await make_world(client, db, contacts=1, name="Ana"), await make_world(client, db, contacts=2, name="Bea")
    mallory = ana.contacts[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = 8101 WHERE id = %s", (mallory["id"],))
        base = await telegram.get_offset(conn)
    await to_state(client, db, ctx, bea, "ESCALATED_L1")
    bea_alert = (await open_alert(db, bea.session_id))["id"]
    before = (await client.get("/v1/me/alerts", headers=bea.wearer)).content
    for i, action in enumerate(("ack", "pass", "safe", "cant")):
        ans = await telegram.handle_update(db, ctx, {"update_id": base + 10 + i, "callback_query": {
            "id": f"cq{i}", "from": {"id": 8101}, "data": f"{action}:{bea_alert}:{mallory['id']}",
            "message": {"message_id": 1, "chat": {"id": 8101}, "text": "x"}}})
        assert ans["text"] == "This alert is no longer active."
    async with db.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM acks WHERE alert_id = %s", (bea_alert,))).fetchone())["n"]
    assert n == 0
    assert (await client.get("/v1/me/alerts", headers=bea.wearer)).content == before
    assert (await session_row(db, bea.session_id))["state"] == "ESCALATED_L1"


async def test_a_telegram_button_works_only_for_a_contact_told_about_the_alert(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    c2 = w.contacts[1]
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = 8102 WHERE id = %s", (c2["id"],))
        base = await telegram.get_offset(conn)
    await to_state(client, db, ctx, w, "ESCALATED_L1")            # only C1 has been told
    aid = (await open_alert(db, w.session_id))["id"]
    ans = await telegram.handle_update(db, ctx, {"update_id": base + 50, "callback_query": {
        "id": "cqx", "from": {"id": 8102}, "data": f"ack:{aid}:{c2['id']}",
        "message": {"message_id": 1, "chat": {"id": 8102}, "text": "x"}}})
    assert ans["text"] == "This alert is no longer active."
    async with db.connection() as conn:
        assert (await (await conn.execute("SELECT count(*) AS n FROM acks WHERE alert_id = %s", (aid,))).fetchone())["n"] == 0


# --- a wearer arms only with her own device --------------------------------------------------------
async def test_arming_with_another_wearers_or_an_unknown_device_is_a_404(client, db):
    a, b = await make_world(client, db, contacts=0, arm=False), await make_world(client, db, contacts=0, arm=False)
    r = await client.post("/v1/sessions", json={"device_id": a.device_id}, headers=b.wearer)
    assert r.status_code == 404, r.text
    r = await client.post("/v1/sessions", json={"device_id": "no-such-device"}, headers=b.wearer)
    assert r.status_code == 404, r.text
    r = await client.post("/v1/sessions", json={"device_id": b.device_id}, headers=b.wearer)
    assert r.status_code == 200, r.text


async def test_two_arm_requests_at_once_give_one_session_and_a_409(client, db):
    for _ in range(5):
        w = await make_world(client, db, contacts=0, arm=False)
        replies = await asyncio.gather(*[client.post("/v1/sessions", json={}, headers=w.wearer) for _ in range(2)])
        assert sorted(r.status_code for r in replies) == [200, 409], [r.text for r in replies]
        conflict = next(r for r in replies if r.status_code == 409).json()["detail"]
        assert conflict["error"] == "already_armed"
        async with db.connection() as conn:
            n = (await (await conn.execute("SELECT count(*) AS n FROM sessions WHERE user_id = %s AND state <> 'ENDED'",
                                           (w.user_id,))).fetchone())["n"]
        assert n == 1


# --- one contact cannot flood the others -------------------------------------------------------------
async def test_one_contact_looping_ack_and_cant_reach_cannot_flood_the_others(client, db, ctx):
    w = await make_world(client, db, contacts=4)
    c1 = w.contacts[0]
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    for c in w.contacts:      # each passes it on: L1 -> L2 -> L3 -> L4 -> FINAL
        assert (await client.post(f"/v1/contact/{c['token']}/followup", json={"action": "pass"})).json()["result"] == "passed"
    assert (await session_row(db, w.session_id))["state"] == states.FINAL
    cap = ctx.settings.timings("demo").renotify_cap
    for _ in range(20):
        await client.post(f"/v1/contact/{c1['token']}/ack")
        await client.post(f"/v1/contact/{c1['token']}/followup", json={"action": "cant_reach"})
    for c in w.contacts[1:]:
        rows = [o for o in await outbox(db, w.session_id, contact_id=c["id"]) if o["template"] in ("alert_final", "handling")]
        assert len(rows) <= cap + 2, (c["name"], len(rows))
    a = await open_alert(db, w.session_id)
    assert a["is_final"] and a["renotify_count"] == cap            # the loop used up the renotify budget


async def test_a_second_acker_is_still_announced_to_the_others(client, db, ctx):
    w = await make_world(client, db, contacts=2)
    c1, c2 = w.contacts
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    await client.post(f"/v1/contact/{c1['token']}/ack")
    await client.post(f"/v1/contact/{c1['token']}/followup", json={"action": "cant_reach"})   # -> L2, C2 told
    assert (await client.post(f"/v1/contact/{c2['token']}/ack")).json()["result"] == "acked"
    handling = [o["text"] for o in await outbox(db, w.session_id, contact_id=c1["id"]) if o["template"] == "handling"]
    assert handling == ["StillHere: C2 is handling it."]


# --- input the database cannot store is a 4xx -----------------------------------------------------
@pytest.mark.parametrize("case", ["event_id", "payload_value", "payload_key", "hr_source", "contact_name",
                                  "join_name", "telemetry_batch", "heartbeat_seq", "event_seq"])
async def test_input_the_database_cannot_store_is_rejected_not_a_500(client, db, case):
    w = await make_world(client, db, contacts=0)
    nul = "a\u0000b"
    if case == "event_id":
        r = await client.post("/v1/device/events", headers=w.device(), json={"event_id": nul, "type": "squeeze", "payload": {}})
    elif case == "payload_value":
        r = await client.post("/v1/device/events", headers=w.device(),
                              json={"event_id": "b1:1", "type": "head_position_change", "payload": {"note": nul}})
    elif case == "payload_key":
        r = await client.post("/v1/device/events", headers=w.device(),
                              json={"event_id": "b1:2", "type": "head_position_change", "payload": {nul: 1}})
    elif case == "hr_source":
        r = await client.post("/v1/hr", headers=w.wearer, json={"source": nul, "bpm": 70})
    elif case == "contact_name":
        r = await client.post("/v1/contacts", headers=w.wearer, json={"name": nul})
    elif case == "join_name":
        inv = (await client.post("/v1/contacts", headers=w.wearer, json={})).json()
        r = await client.post(f"/v1/contact/{inv['web_link'].rsplit('/', 1)[1]}/join", json={"name": nul})
    elif case == "telemetry_batch":
        r = await client.post("/v1/telemetry/batch", headers=w.device(),
                              json={"batch_id": nul, "rows": [[0, 1, 2, 3, 4]]})
    elif case == "heartbeat_seq":
        r = await client.post("/v1/device/heartbeat", headers={**w.device(), "X-Seq": str(2 ** 63)}, json={})
    else:
        r = await client.post("/v1/device/events", headers={**w.device(), "X-Seq": str(10 ** 30)},
                              json={"event_id": "b1:3", "type": "prompt_shown", "payload": {}})
    assert 400 <= r.status_code < 500, (case, r.status_code, r.text)
    # Nothing was stored, and the device and wearer keep working.
    hb = await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    assert hb.status_code == 200
