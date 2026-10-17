"""Outbox delivery (log channel, Telegram 429 backoff, reaper) and the Telegram bot handlers."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx

from app import engine, sensors, states, telegram
from app.engine import Ctx
from app.outbox import Sender, TelegramAPI, reap
from tests.conftest import deliver_all, make_world, open_alert, outbox, session_row, to_state


async def _only_pending(db, keep_contact_id):
    async with db.connection() as conn:
        await conn.execute(
            """UPDATE outbox SET status = 'sent', channel = 'test-skip'
                WHERE status IN ('pending', 'sending')
                  AND NOT (contact_id IS NOT DISTINCT FROM %s AND template = 'alert_no_response')""",
            (keep_contact_id,))


async def test_log_channel_without_token(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    await deliver_all(db, ctx)
    rows = await outbox(db, w.session_id)
    assert rows and all(r["status"] == "sent" for r in rows)
    assert {r["channel"] for r in rows if r["recipient"] == "wearer"} == {"log"}
    assert {r["channel"] for r in rows if r["recipient"] == "contact"} == {"web"}  # read on the live page


async def test_dedupe_key_blocks_duplicate_enqueue(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, states.PROMPTED)
    row = (await outbox(db, w.session_id, recipient="wearer"))[0]
    async with db.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO outbox (dedupe_key, recipient, purpose, text) VALUES (%s, 'wearer', 'x', 'dup')
               ON CONFLICT (dedupe_key) DO NOTHING""", (row["dedupe_key"],))
        assert cur.rowcount == 0


async def test_telegram_429_backoff_then_send(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    c1 = w.contacts[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = 5551 WHERE id = %s", (c1["id"],))
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    await _only_pending(db, c1["id"])

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path.rsplit("/", 1)[1], json.loads(request.content)))
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                                             "description": "Too Many Requests: retry after 3",
                                             "parameters": {"retry_after": 3}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sender = Sender(db, ctx, TelegramAPI("123:TEST", http))
        assert await sender.run_once() == 1
        row = (await outbox(db, w.session_id, contact_id=c1["id"]))[0]
        async with db.connection() as conn:
            left = (await (await conn.execute("SELECT %s::timestamptz - now() AS d", (row["next_attempt_at"],))).fetchone())["d"]
        assert row["status"] == "pending" and 2.0 < left.total_seconds() <= 3.0  # retry_after honoured
        assert await sender.run_once() == 0  # not due yet
        assert len(calls) == 1
        async with db.connection() as conn:
            await conn.execute("UPDATE outbox SET next_attempt_at = now() WHERE id = %s", (row["id"],))
        sender._last_by_chat.clear()
        assert await sender.run_once() == 1
    row = (await outbox(db, w.session_id, contact_id=c1["id"]))[0]
    assert row["status"] == "sent" and row["channel"] == "telegram" and row["provider_message_id"] == "42"
    method, payload = calls[-1]
    assert method == "sendMessage" and payload["chat_id"] == 5551 and payload["protect_content"] is True
    assert payload["reply_markup"]["inline_keyboard"][0][0]["text"] == "I'm on it"


async def test_reaper_returns_stale_sending_rows(client, db, ctx):
    async with db.connection() as conn:
        row = await (await conn.execute(
            """INSERT INTO outbox (dedupe_key, recipient, purpose, text, status, locked_until)
               VALUES ('reaper-test', 'chat', 'x', 'x', 'sending', now() - interval '1 second') RETURNING id""")).fetchone()
    assert await reap(db) >= 1
    async with db.connection() as conn:
        r = await (await conn.execute("SELECT status FROM outbox WHERE id = %s", (row["id"],))).fetchone()
    assert r["status"] == "pending"


async def test_telegram_start_approve_and_buttons(client, db, ctx):
    async with db.connection() as conn:
        base = await telegram.get_offset(conn)
    uid = iter(range(base + 1, base + 1000))
    w = await make_world(client, db, contacts=0)
    inv = (await client.post("/v1/contacts", json={}, headers=w.wearer)).json()
    token = inv["web_link"].rsplit("/", 1)[1]

    start = {"update_id": next(uid), "message": {"message_id": 1, "chat": {"id": 7001},
                                                 "from": {"id": 7001, "first_name": "Sam"}, "text": f"/start {token}"}}
    await telegram.handle_update(db, ctx, start)
    async with db.connection() as conn:
        c = await (await conn.execute("SELECT * FROM contacts WHERE id = %s", (inv["contact_id"],))).fetchone()
    assert c["telegram_chat_id"] == 7001 and c["channel"] == "telegram" and c["name"] == "Sam"
    assert c["approved_at"] is None  # pending until the wearer approves
    n_before = len(await outbox(db))
    await telegram.handle_update(db, ctx, start)  # redelivered update: no-op
    assert len(await outbox(db)) == n_before

    r = await client.post(f"/v1/contacts/{c['id']}/approve", headers=w.wearer)
    assert r.json()["n"] == 1
    approved_msg = (await outbox(db, None, contact_id=c["id"]))[-1]["text"]
    assert approved_msg == ("You're Maya's trusted contact #1. You'll see nothing from StillHere unless she "
                            "misses check-ins or asks for help.")
    live = engine.contact_web_token(ctx, c["id"], c["web_token_nonce"])
    assert (await client.get(f"/v1/contact/{live}/status")).json() == {"alert": None, "approved": True}

    await to_state(client, db, ctx, w, "ESCALATED_L1")
    alert_row = (await outbox(db, w.session_id, contact_id=c["id"]))[0]
    ack_data = alert_row["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert ack_data.startswith("ack:") and len(ack_data.encode()) <= 64

    def cb(data, from_id=7001):
        return {"update_id": next(uid), "callback_query": {
            "id": f"cq{from_id}", "from": {"id": from_id}, "data": data,
            "message": {"message_id": 55, "chat": {"id": from_id}, "text": "StillHere: original alert"}}}

    ans = await telegram.handle_update(db, ctx, cb(ack_data, from_id=9999))  # not this contact's chat
    assert ans["text"] == "This alert is no longer active."
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"

    ack = cb(ack_data)
    ans = await telegram.handle_update(db, ctx, ack)
    assert ans["text"] == "Thanks, you're on it."
    assert (await session_row(db, w.session_id))["state"] == states.ACKED
    edit = [o for o in await outbox(db, w.session_id, contact_id=c["id"]) if o["template"] == "ack_thanks"][0]
    assert edit["method"] == "editMessageText" and edit["edit_message_id"] == 55
    assert edit["text"].startswith("StillHere: original alert\n\n")
    assert await telegram.handle_update(db, ctx, ack) is None  # same update_id again: ignored

    safe = cb(f"safe:{alert_row['alert_id']}:{c['id']}")
    ans = await telegram.handle_update(db, ctx, safe)
    assert ans["text"] == "Thank you. Marked safe."
    assert (await session_row(db, w.session_id))["state"] == states.ARMED
    assert await open_alert(db, w.session_id) is None

    # The wearer binds her own chat for "Reminder" nudges.
    link = (await client.post("/v1/me/telegram-link", headers=w.wearer)).json()
    await telegram.handle_update(db, ctx, {"update_id": next(uid), "message": {
        "message_id": 2, "chat": {"id": 7002}, "from": {"id": 7002}, "text": f"/start {link['start_token']}"}})
    async with db.connection() as conn:
        u = await (await conn.execute("SELECT telegram_chat_id FROM users WHERE id = %s", (w.user_id,))).fetchone()
    assert u["telegram_chat_id"] == 7002
    await telegram.handle_update(db, ctx, {"update_id": next(uid), "message": {
        "message_id": 3, "chat": {"id": 7003}, "from": {"id": 7003}, "text": "/start not-a-token"}})
    last = (await outbox(db))[-1]
    assert last["chat_id"] == 7003 and last["text"] == "This invite link has expired or was already used."


async def test_poller_is_a_singleton(settings, db, ctx):
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        calls.append(method)
        await asyncio.sleep(0.05)
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, json={"ok": True, "result": True})

    tg_ctx = Ctx(settings=replace(settings, telegram_bot_token="123:TEST"), secret=ctx.secret)
    stop = asyncio.Event()
    clients = [httpx.AsyncClient(transport=httpx.MockTransport(handler)) for _ in range(2)]
    tasks = [asyncio.create_task(telegram.telegram_poller(db, tg_ctx, stop, client=c)) for c in clients]
    await asyncio.sleep(1.0)
    stop.set()
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=10)
    for c in clients:
        await c.aclose()
    assert calls.count("deleteWebhook") == 1  # only the lock holder polls
    assert calls.count("getUpdates") >= 1


async def test_hr_ingest_and_rule_only_pulls_forward(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    async with db.connection() as conn:
        await conn.execute("INSERT INTO user_baselines (user_id, kind, value) VALUES (%s, 'hr_rest', 70)",
                           (w.user_id,))
    await client.post("/v1/device/heartbeat", headers=w.device(), json={"enmo60_mg": 5})
    import time
    now_ms = int(time.time() * 1000)
    r = await client.post("/v1/hr", headers={"Authorization": f"Bearer {w.device_key}", "X-Device-Id": w.device_id},
                          json={"source": "apple_watch_heartcast", "simulated": False,
                                "samples": [{"t": now_ms - i * 1000, "bpm": 120, "rr_ms": [500.0],
                                             "rr_synthetic": True} for i in range(8)]})
    assert r.json() == {"ok": True, "inserted": 8}
    r = await client.post("/v1/hr", headers=w.wearer, json={"ts": now_ms, "bpm": 121, "source": "shortcut"})
    assert r.json()["inserted"] == 1
    bad = await client.post("/v1/hr", headers=w.wearer, json={"user_id": "00000000-0000-0000-0000-000000000000",
                                                              "bpm": 80, "source": "x"})
    assert bad.status_code == 403

    await sensors.evaluate_hr(db, ctx, force=True)
    s = await session_row(db, w.session_id)
    assert s["state"] == states.ARMED and s["hr_deviating_since"] is not None  # not sustained yet
    async with db.connection() as conn:
        await conn.execute("UPDATE sessions SET hr_deviating_since = now() - interval '20 seconds' WHERE id = %s",
                           (w.session_id,))
    await sensors.evaluate_hr(db, ctx, force=True)
    s = await session_row(db, w.session_id)
    assert s["state"] == states.PROMPTED and s["prompt_cause"] == "challenge" and s["challenge_n"] in (1, 3, 4)
    async with db.connection() as conn:
        pf = await (await conn.execute(
            "SELECT actor, detail FROM audit_log WHERE session_id = %s AND kind = 'pull_forward'",
            (w.session_id,))).fetchone()
    assert pf["actor"] == "rule:hr" and pf["detail"]["reason"] == "hr_different_from_usual"
    for _ in range(3):  # more deviation never escalates
        await sensors.evaluate_hr(db, ctx, force=True)
    assert (await session_row(db, w.session_id))["state"] == states.PROMPTED
    assert await open_alert(db, w.session_id) is None


async def test_export_and_erase(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, states.REMINDED)
    await client.post("/v1/hr", headers=w.wearer, json={"bpm": 75, "source": "shortcut"})
    exp = (await client.get("/v1/me/export", headers=w.wearer)).json()
    assert exp["sessions"][0]["chain_ok"] is True and len(exp["sessions"][0]["entries"]) >= 3
    assert exp["account_chain_ok"] is True
    r = (await client.delete("/v1/me/data", headers=w.wearer)).json()
    assert r["erased"]["timeline_entries"] >= 3 and r["erased"]["hr_samples"] == 1
    exp2 = (await client.get("/v1/me/export", headers=w.wearer)).json()
    assert exp2["sessions"][0]["entries"] == [] and exp2["account_chain_ok"] is True
    async with db.connection() as conn:
        tomb = await (await conn.execute(
            "SELECT kind FROM audit_log WHERE user_id = %s ORDER BY id", (w.user_id,))).fetchall()
    assert [t["kind"] for t in tomb] == ["timeline_erased"]


async def test_telegram_start_from_a_group_chat_binds_nothing(client, db, ctx):
    async with db.connection() as conn:
        base = await telegram.get_offset(conn)
    uid = iter(range(base + 1, base + 1000))
    w = await make_world(client, db, contacts=0)
    inv = (await client.post("/v1/contacts", json={}, headers=w.wearer)).json()
    token = inv["web_link"].rsplit("/", 1)[1]
    link = (await client.post("/v1/me/telegram-link", headers=w.wearer)).json()["start_token"]
    for chat_type, text, chat_id in (("supergroup", f"/start@StillHereBot {token}", -1001234567890),
                                     ("group", f"/start {link}", -100999)):
        upd = {"update_id": next(uid), "message": {"message_id": 1, "chat": {"id": chat_id, "type": chat_type},
                                                   "from": {"id": 7001, "first_name": "Sam"}, "text": text}}
        await telegram.handle_update(db, ctx, upd)
        said = [o for o in await outbox(db) if o["chat_id"] == chat_id]
        assert [o["template"] for o in said] == ["private_chat_only"], said
    async with db.connection() as conn:
        c = await (await conn.execute("SELECT * FROM contacts WHERE id = %s", (inv["contact_id"],))).fetchone()
        u = await (await conn.execute("SELECT * FROM users WHERE id = %s", (w.user_id,))).fetchone()
    assert c["telegram_chat_id"] is None and c["invite_used_at"] is None  # the invite still works in private
    assert u["telegram_chat_id"] is None and u["tg_link_token_hash"] is not None


async def test_outbox_never_gives_up_on_a_network_outage(client, db, ctx):
    from app import outbox as ob
    w = await make_world(client, db, contacts=1)
    c1 = w.contacts[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = 5552 WHERE id = %s", (c1["id"],))
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    await _only_pending(db, c1["id"])
    up = {"on": True}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if not up["on"]:
            raise httpx.ConnectError("network unreachable")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    async def cycle(sender):
        async with db.connection() as conn:
            await conn.execute("UPDATE outbox SET next_attempt_at = now() WHERE contact_id = %s AND status = 'pending'",
                               (c1["id"],))
        sender._last_by_chat.clear()
        await sender.run_once()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sender = Sender(db, ctx, TelegramAPI("123:TEST", http))
        await cycle(sender)  # the alert reaches the contact
        up["on"] = False  # then the laptop loses its route to Telegram...
        await client.post(f"/v1/sessions/{w.session_id}/cancel", json={"pin": "2468"}, headers=w.wearer)
        for _ in range(3 * ob.MAX_ATTEMPTS):  # ...for far longer than the attempt cap
            await cycle(sender)
        fa = [r for r in await outbox(db, w.session_id, contact_id=c1["id"]) if r["template"] == "false_alarm"][0]
        assert fa["status"] == "pending" and fa["attempts"] >= 3 * ob.MAX_ATTEMPTS
        assert fa["last_error"].startswith("0: ConnectError")
        up["on"] = True
        await cycle(sender)
    rows = await outbox(db, w.session_id, contact_id=c1["id"])
    assert {(r["template"], r["status"], r["channel"]) for r in rows} == {
        ("alert_no_response", "sent", "telegram"), ("false_alarm", "sent", "telegram")}


async def test_outbox_stops_retrying_a_permanent_rejection(client, db, ctx):
    from app import outbox as ob
    async with db.connection() as conn:
        row = await (await conn.execute(
            """INSERT INTO outbox (dedupe_key, recipient, chat_id, purpose, text)
               VALUES ('permanent-403', 'chat', 5553, 'x', 'x') RETURNING id""")).fetchone()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sender = Sender(db, ctx, TelegramAPI("123:TEST", http))
        for _ in range(ob.MAX_ATTEMPTS + 2):
            async with db.connection() as conn:
                await conn.execute("UPDATE outbox SET next_attempt_at = now() WHERE id = %s", (row["id"],))
            sender._last_by_chat.clear()
            await sender.run_once()
    async with db.connection() as conn:
        r = await (await conn.execute("SELECT status, attempts FROM outbox WHERE id = %s", (row["id"],))).fetchone()
    assert r["status"] == "failed" and r["attempts"] == ob.MAX_ATTEMPTS


async def _telegram_contact_at_l1(client, db, ctx, chat_id):
    w = await make_world(client, db, contacts=2)
    c1 = w.contacts[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = %s WHERE id = %s", (chat_id, c1["id"]))
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    alert = await open_alert(db, w.session_id)
    return w, c1, alert


async def test_contact_tap_racing_the_ack_timeout_is_not_lost(client, db, ctx, monkeypatch):
    """The tap and the timer lock the session row first, so they queue instead of deadlocking."""
    from app import messages, timers
    w, c1, alert = await _telegram_contact_at_l1(client, db, ctx, 7101)
    async with db.connection() as conn:
        await conn.execute("UPDATE timers SET fire_at = now() WHERE session_id = %s AND kind = 'ACK_TIMEOUT'",
                           (w.session_id,))
        uid = await telegram.get_offset(conn) + 1
    claimed, go = asyncio.Event(), asyncio.Event()
    real_apply = engine.apply

    async def paused_apply(conn, session_id, event, **kw):
        if kw.get("actor") == "timer":  # the timer worker has claimed its timer (and the session row)
            claimed.set()
            await go.wait()
        return await real_apply(conn, session_id, event, **kw)
    monkeypatch.setattr(engine, "apply", paused_apply)

    async def timer():
        async with db.connection() as conn:
            return await timers.fire_one(conn, ctx, session_id=w.session_id)
    t = asyncio.create_task(timer())
    await asyncio.wait_for(claimed.wait(), 5)
    tap = {"update_id": uid, "callback_query": {
        "id": "cq-race", "from": {"id": 7101}, "data": f"ack:{alert['id']}:{c1['id']}",
        "message": {"message_id": 9, "chat": {"id": 7101}, "text": "StillHere: alert"}}}
    h = asyncio.create_task(telegram.handle_update(db, ctx, tap))
    await asyncio.sleep(0.4)  # the tap is now waiting behind the timer's transaction
    go.set()
    answer = await asyncio.wait_for(h, 15)
    await asyncio.wait_for(t, 15)
    assert answer is not None and answer["text"] == messages.CALLBACK_ANSWERS["acked"]
    assert (await session_row(db, w.session_id))["state"] == states.ACKED
    async with db.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM acks WHERE telegram_update_id = %s",
                                       (uid,))).fetchone())["n"]
    assert n == 1


async def test_a_transient_database_error_leaves_the_update_for_a_retry(client, db, ctx, monkeypatch):
    import psycopg
    w, c1, alert = await _telegram_contact_at_l1(client, db, ctx, 7102)
    async with db.connection() as conn:
        uid = await telegram.get_offset(conn) + 1

    async def boom(conn, ctx_, update):
        raise psycopg.errors.SerializationFailure("simulated")
    monkeypatch.setattr(telegram, "_handle_callback", boom)
    tap = {"update_id": uid, "callback_query": {"id": "cq-x", "from": {"id": 7102},
                                                "data": f"ack:{alert['id']}:{c1['id']}", "message": {}}}
    try:
        await telegram.handle_update(db, ctx, tap)
        raise AssertionError("a transient error must not be swallowed")
    except psycopg.OperationalError:
        pass
    async with db.connection() as conn:
        assert await telegram.get_offset(conn) == uid - 1  # not advanced: getUpdates returns it again


async def test_live_page_tap_racing_the_ack_timeout_is_not_lost(client, db, ctx, monkeypatch):
    from app import timers
    w, c1, alert = await _telegram_contact_at_l1(client, db, ctx, 7103)
    async with db.connection() as conn:
        await conn.execute("UPDATE timers SET fire_at = now() WHERE session_id = %s AND kind = 'ACK_TIMEOUT'",
                           (w.session_id,))
    claimed, go = asyncio.Event(), asyncio.Event()
    real_apply = engine.apply

    async def paused_apply(conn, session_id, event, **kw):
        if kw.get("actor") == "timer":
            claimed.set()
            await go.wait()
        return await real_apply(conn, session_id, event, **kw)
    monkeypatch.setattr(engine, "apply", paused_apply)

    async def timer():
        async with db.connection() as conn:
            return await timers.fire_one(conn, ctx, session_id=w.session_id)
    t = asyncio.create_task(timer())
    await asyncio.wait_for(claimed.wait(), 5)
    h = asyncio.create_task(client.post(f"/v1/contact/{c1['token']}/ack"))
    await asyncio.sleep(0.4)
    go.set()
    r = await asyncio.wait_for(h, 15)
    await asyncio.wait_for(t, 15)
    assert r.status_code == 200 and r.json() == {"ok": True, "result": "acked"}
    assert (await session_row(db, w.session_id))["state"] == states.ACKED


async def test_workers_wait_for_the_app_secret(client, db, ctx, settings):
    """Before the database init loads APP_SECRET, nothing may sign links with an empty key."""
    from app import timers
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, states.REMINDED)
    async with db.connection() as conn:
        await conn.execute("UPDATE timers SET fire_at = now() WHERE session_id = %s AND kind = 'REMINDER_TIMEOUT'",
                           (w.session_id,))
    early = Ctx(settings=replace(settings, telegram_bot_token="123:TEST"), secret="")
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[1])
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"ok": True, "result": []})
    stop = asyncio.Event()
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tasks = [asyncio.create_task(timers.timer_worker(db, early, stop, tick_s=0.1)),
             asyncio.create_task(telegram.telegram_poller(db, early, stop, client=http))]
    await asyncio.sleep(0.6)
    assert (await session_row(db, w.session_id))["state"] == states.REMINDED  # nothing escalated yet
    assert calls == []
    early.secret = ctx.secret  # the init succeeded
    for _ in range(40):
        if (await session_row(db, w.session_id))["state"] == "ESCALATED_L1" and "getUpdates" in calls:
            break
        await asyncio.sleep(0.1)
    stop.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 10)
    await http.aclose()
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"
    assert "getUpdates" in calls
    # The location link in the alert is the one the server answers with the real secret.
    link = [o for o in await outbox(db, w.session_id, contact_id=w.contacts[0]["id"])][0]["text"].split(": ")[-1]
    assert (await client.get("/l/" + link.rsplit("/l/", 1)[1])).status_code == 200
