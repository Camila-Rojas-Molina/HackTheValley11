"""Timers, outbox and Telegram under failure: messages to one recipient keep their order, a timer is
never fired twice or dropped after errors, the Telegram poller follows a new bot or an update-id reset,
a squeeze held back by the bridge never answers a prompt she had not seen, and simulate.py never
starts a second poller on the team's bot."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app import engine, states, telegram, timers
from app.engine import Ctx
from app.outbox import Sender, TelegramAPI
from tests.conftest import ADMIN_KEY, PIN, fire, make_world, open_alert, pending_timers, session_row, to_state


# --- one recipient's messages arrive in the order they were queued ---------------------------------
async def test_a_retried_alert_is_never_delivered_after_the_false_alarm_that_closed_it(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    c1 = w.contacts[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = 7002 WHERE id = %s", (c1["id"],))
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    async with db.connection() as conn:
        await conn.execute("""UPDATE outbox SET status = 'sent', channel = 'test-skip'
                               WHERE status IN ('pending', 'sending')
                                 AND NOT (contact_id IS NOT DISTINCT FROM %s AND template = 'alert_no_response')""",
                           (c1["id"],))
    delivered, attempts = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        text = json.loads(request.content)["text"]
        attempts.append(text)
        if len(attempts) == 1:
            return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})
        delivered.append(text)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(attempts)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sender = Sender(db, ctx, TelegramAPI("123:TEST", http))
        assert await sender.run_once() == 1                    # the alert: 502, retried later
        r = await client.post(f"/v1/sessions/{w.session_id}/cancel", json={"pin": PIN}, headers=w.wearer)
        assert r.json()["state_public"] == states.ARMED      # false alarm queued for C1
        for _ in range(10):
            sender._last_by_chat.clear()
            await sender.run_once()
            if len(delivered) == 2:
                break
            async with db.connection() as conn:   # 5 s pass for C1's queue
                await conn.execute("""UPDATE outbox SET next_attempt_at = next_attempt_at - interval '5 seconds'
                                       WHERE contact_id = %s AND status = 'pending'""", (c1["id"],))
    assert len(delivered) == 2, (attempts, delivered)
    assert delivered[0].startswith("StillHere: Maya hasn't responded"), delivered
    assert delivered[1].startswith("StillHere: False alarm"), delivered


# --- a timer fires once, even when one worker fails while another takes it --------------------------
class _HookedConn:
    """A worker's connection whose second transaction first lets another worker run."""

    def __init__(self, conn, hook):
        self._c, self._hook, self._n = conn, hook, 0

    def transaction(self):
        self._n += 1
        tx = self._c.transaction()
        if self._n != 2:
            return tx
        hook = self._hook

        class _Tx:
            async def __aenter__(self):
                await hook()
                return await tx.__aenter__()

            async def __aexit__(self, *exc):
                return await tx.__aexit__(*exc)
        return _Tx()

    def __getattr__(self, name):
        return getattr(self._c, name)


async def test_a_failing_worker_never_unfires_a_timer_another_worker_fired(client, db, ctx, monkeypatch):
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    async with db.connection() as conn:
        t = await (await conn.execute(
            """UPDATE timers SET fire_at = now() WHERE session_id = %s AND kind = 'RENOTIFY'
                 AND fired_at IS NULL AND cancelled_at IS NULL RETURNING id""", (w.session_id,))).fetchone()
    real_apply = engine.apply
    other = {}

    async def other_worker():
        async with db.connection() as conn_b:
            other["fired"] = await timers.fire_one(conn_b, ctx, session_id=w.session_id)

    async with db.connection() as conn_a:
        hooked = _HookedConn(conn_a, other_worker)

        async def flaky_apply(conn, *a, **k):
            if conn is hooked:
                raise RuntimeError("transient failure in worker A")
            return await real_apply(conn, *a, **k)
        monkeypatch.setattr(engine, "apply", flaky_apply)
        assert await timers.fire_one(hooked, ctx, session_id=w.session_id) == t["id"]
    assert other["fired"] == t["id"]
    async with db.connection() as conn:
        row = await (await conn.execute("SELECT fired_at, result FROM timers WHERE id = %s", (t["id"],))).fetchone()
        alert = await (await conn.execute(
            "SELECT renotify_count FROM alerts WHERE session_id = %s AND NOT cover", (w.session_id,))).fetchone()
    assert row["fired_at"] is not None and row["result"] == "renotified"
    assert alert["renotify_count"] == 1
    assert [p["kind"] for p in await pending_timers(db, w.session_id)].count(states.RENOTIFY) == 1


async def test_a_deadline_that_keeps_failing_is_retried_until_it_fires(client, db, ctx, monkeypatch):
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, states.REMINDED)
    real_apply = engine.apply

    async def broken(*a, **k):
        raise RuntimeError("a bug in this state combination")
    monkeypatch.setattr(engine, "apply", broken)
    for _ in range(timers.MAX_ATTEMPTS + 2):
        async with db.connection() as conn:
            await conn.execute("""UPDATE timers SET fire_at = now() WHERE session_id = %s AND kind = 'REMINDER_TIMEOUT'
                                    AND fired_at IS NULL AND cancelled_at IS NULL""", (w.session_id,))
            assert await timers.fire_one(conn, ctx, session_id=w.session_id) is not None
    pending = [p for p in await pending_timers(db, w.session_id) if p["kind"] == states.REMINDER_TIMEOUT]
    assert len(pending) == 1 and pending[0]["attempts"] == timers.MAX_ATTEMPTS + 2
    health = (await client.get("/v1/health", headers={"X-Admin-Key": ADMIN_KEY})).json()
    assert health["failing_timers"] >= 1                     # the operator can see it
    assert "failing_timers" not in (await client.get("/v1/health")).json()
    monkeypatch.setattr(engine, "apply", real_apply)          # the fault is fixed: the deadline still fires
    assert await fire(db, ctx, w.session_id, states.REMINDER_TIMEOUT) == "escalated"
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"


# --- Telegram: a new bot, or an update-id reset after a week idle ----------------------------------
def _tg_ctx(ctx, token="123456:TESTTOKEN") -> Ctx:
    return Ctx(settings=replace(ctx.settings, telegram_bot_token=token), secret=ctx.secret, choose=ctx.choose)


async def _bind_and_alert(client, db, ctx, chat_id):
    w = await make_world(client, db, contacts=1)
    async with db.connection() as conn:
        await conn.execute("UPDATE contacts SET telegram_chat_id = %s WHERE id = %s", (chat_id, w.contacts[0]["id"]))
    await to_state(client, db, ctx, w, "ESCALATED_L1")
    return w, (await open_alert(db, w.session_id))["id"]


async def test_an_update_with_an_id_below_the_stored_offset_is_still_processed_once(client, db, ctx):
    tctx = _tg_ctx(ctx)
    w, aid = await _bind_and_alert(client, db, ctx, 8201)
    async with db.connection() as conn:
        await telegram._set_offset(conn, 900_000_001, tctx)   # left by an earlier epoch of the bot
    upd = {"update_id": 12345, "callback_query": {"id": "cq-low", "from": {"id": 8201},
                                                  "data": f"ack:{aid}:{w.contacts[0]['id']}",
                                                  "message": {"message_id": 3, "chat": {"id": 8201}, "text": "x"}}}
    ans = await telegram.handle_update(db, tctx, upd)
    assert ans["text"] == "Thanks, you're on it."
    assert (await session_row(db, w.session_id))["state"] == states.ACKED
    assert await telegram.handle_update(db, tctx, upd) is None   # the same update again: nothing


async def test_the_offset_belongs_to_one_bot(client, db, ctx):
    a, b = _tg_ctx(ctx, "111:AAA"), _tg_ctx(ctx, "222:BBB")
    async with db.connection() as conn:
        await telegram._set_offset(conn, 5000, a)
        assert await telegram.get_offset(conn, a) == 5000
        assert await telegram.get_offset(conn, b) == 0


async def test_the_poller_follows_update_ids_that_restart_lower(client, db, ctx):
    tctx = _tg_ctx(ctx, "333:CCC")
    w, aid = await _bind_and_alert(client, db, ctx, 8202)
    async with db.connection() as conn:
        await telegram._set_offset(conn, 900_000_001, tctx)
    stop = asyncio.Event()
    offsets = []
    low = {"update_id": 777, "callback_query": {"id": "cq-777", "from": {"id": 8202},
                                                "data": f"ack:{aid}:{w.contacts[0]['id']}",
                                                "message": {"message_id": 4, "chat": {"id": 8202}, "text": "x"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        if method != "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": True})
        offset = json.loads(request.content)["offset"]
        offsets.append(offset)
        if offset > 778:   # Telegram answers an offset "in the future" with what it still holds
            return httpx.Response(200, json={"ok": True, "result": [low]})
        stop.set()
        return httpx.Response(200, json={"ok": True, "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await asyncio.wait_for(telegram.telegram_poller(db, tctx, stop, client=http), timeout=20)
    assert offsets[:2] == [900_000_001, 778]
    assert (await session_row(db, w.session_id))["state"] == states.ACKED
    async with db.connection() as conn:
        assert await telegram.get_offset(conn, tctx) == 778


# --- a squeeze held back by the bridge never answers a prompt she had not seen ----------------------
async def _squeeze(client, w, event_id, **payload):
    return await client.post("/v1/device/events", headers=w.device(),
                             json={"event_id": event_id, "type": "squeeze", "payload": {"pattern": "plain", "k": 2, **payload}})


async def test_a_buffered_squeeze_from_before_the_prompt_does_not_answer_it(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await fire(db, ctx, w.session_id, states.CHECKIN_DUE)          # routine prompt, k = 2
    cid = (await client.post("/v1/device/heartbeat", headers=w.device(), json={})).json()["cmd"]["challenge_id"]
    assert cid
    for i, extra in enumerate(({"buffered": True}, {"buffered": True, "challenge_id": None},
                               {"buffered": True, "challenge_id": "000000"})):
        r = await _squeeze(client, w, f"b1:{40 + i}", **extra)
        assert r.status_code == 200 and r.json()["state_public"] == states.PROMPTED, (extra, r.text)
    assert (await session_row(db, w.session_id))["state"] == states.PROMPTED
    tl = (await client.get(f"/v1/sessions/{w.session_id}/timeline", headers=w.wearer)).json()["entries"]
    assert "checkin" not in [e["kind"] for e in tl]
    # Held back but made while she saw this prompt: it answers it.
    r = await _squeeze(client, w, "b1:50", buffered=True, challenge_id=cid)
    assert r.json()["result"] == "noted" and r.json()["state_public"] == states.ARMED


async def test_a_live_squeeze_without_a_challenge_id_still_answers(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await fire(db, ctx, w.session_id, states.CHECKIN_DUE)
    r = await _squeeze(client, w, "b1:60")
    assert r.json()["result"] == "noted" and r.json()["state_public"] == states.ARMED


# --- simulate.py never runs next to a live bot ----------------------------------------------------------
def test_simulate_refuses_a_server_with_a_telegram_bot(monkeypatch, capsys):
    from scripts import simulate

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"ok": True, "db": "up", "telegram": "configured",
                                             "emergency_services": "SIMULATED (never placed)"})
        raise AssertionError(f"simulate.py went on to {request.url.path}")

    real_client = httpx.Client
    monkeypatch.setattr(simulate.httpx, "Client",
                        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.MockTransport(handler)}))
    for k, v in {"ADMIN_KEY": "a", "DEVICE_KEY": "d", "WEARER_KEY": "w", "DEMO_PIN": "1", "DEMO_DURESS_PIN": "2"}.items():
        monkeypatch.setenv(k, v)
    assert simulate.main(["--url", "http://sim.test"]) == 2
    assert "TELEGRAM_BOT_TOKEN=" in capsys.readouterr().err


def test_the_readme_starts_the_simulate_server_without_the_bot_token():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    for doc in (repo / "README.md", repo / "backend" / "README.md"):
        lines = [ln for ln in doc.read_text(encoding="utf-8").splitlines() if "serve.sh" in ln and "8001" in ln]
        assert lines, doc
        assert all("TELEGRAM_BOT_TOKEN=" in ln for ln in lines), (doc, lines)
