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
