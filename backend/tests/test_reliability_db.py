"""Chaos and reliability rows of BUILD_PLAN §6.9 that had no committed test:

* Crash between send and mark: Telegram accepted the message, marking the row sent failed. At most one
  duplicate, and a mark that keeps failing never re-sends.
* DB unreachable: 503s, the bridge buffers, and no event is lost after recovery (a duress squeeze included).
* LOST_CONTACT in the real profile: a 5-minute grace before contacts hear, or at once when a check-in
  comes due; restored inside the grace, nobody was told.
* Invites expire after 10 minutes; the outbox sends about one message per second per chat.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from app import outbox as ob
from app import states, telegram
from app.config import Settings
from app.outbox import Sender
from scripts import bridge as br
from tests.conftest import (expire_leases, fire, make_world, open_alert, outbox, pending_timers, session_row,
                            to_state)


class FakeTelegram:
    """Stands in for TelegramAPI: every call is accepted (message_id 100, 101, ...)."""

    def __init__(self):
        self.calls: list[tuple[str, dict, float]] = []

    def scrub(self, text: str) -> str:
        return text

    async def call(self, method: str, payload: dict, timeout: float = 15.0) -> dict:
        self.calls.append((method, payload, ob.time.monotonic()))
        return {"ok": True, "status": 200, "result": {"message_id": 99 + len(self.calls)}}


async def _chat_rows(db, chat_id: int, n: int, tag: str) -> list[int]:
    """n pending rows to one Telegram chat; every other pending row in the shared database is set aside."""
    async with db.connection() as conn:
        await conn.execute("UPDATE outbox SET status = 'sent', channel = 'test-skip' WHERE status IN ('pending', 'sending')")
        ids = []
        for i in range(n):
            r = await (await conn.execute(
                """INSERT INTO outbox (dedupe_key, recipient, chat_id, purpose, text)
                   VALUES (%s, 'chat', %s, 'x', %s) RETURNING id""",
                (f"{tag}:{chat_id}:{i}:{time.time_ns()}", chat_id, f"{tag} {i}"))).fetchone()
            ids.append(r["id"])
    return ids


async def _row(db, row_id: int) -> dict:
    async with db.connection() as conn:
        return await (await conn.execute("SELECT * FROM outbox WHERE id = %s", (row_id,))).fetchone()


async def _due_now(db, row_id: int) -> None:
    async with db.connection() as conn:
        await conn.execute("UPDATE outbox SET next_attempt_at = now() WHERE id = %s", (row_id,))


# --- crash between send and mark (§6.9 row 2) ----------------------------------------------------------
def _failing_mark(monkeypatch, times: int):
    real = ob.mark_sent
    left = {"n": times}

    async def mark(db, row, channel, provider_id):
        if left["n"] > 0:
            left["n"] -= 1
            raise ConnectionError("database went away after Telegram said 200")
        return await real(db, row, channel, provider_id)
    monkeypatch.setattr(ob, "mark_sent", mark)
    return left


async def test_a_failed_mark_after_telegram_accepted_never_sends_again(db, ctx, monkeypatch):
    (rid,) = await _chat_rows(db, 5601, 1, "mark")
    api = FakeTelegram()
    sender = Sender(db, ctx, api)
    _failing_mark(monkeypatch, 3)                       # the mark fails three times in a row
    for attempt in range(4):
        await sender.run_once()
        r = await _row(db, rid)
        if r["status"] == "sent":
            break
        assert r["status"] == "pending", r["status"]    # released for a retry, not failed
        await _due_now(db, rid)
        sender._last_by_chat.clear()                    # the retry backoff (2 s and up) is past the throttle
    r = await _row(db, rid)
    assert r["status"] == "sent" and r["provider_message_id"] == "100" and r["channel"] == "telegram"
    assert len(api.calls) == 1                          # accepted once: never re-sent while the mark failed


async def test_a_crash_between_send_and_mark_sends_at_most_one_duplicate(db, ctx, monkeypatch):
    (rid,) = await _chat_rows(db, 5602, 1, "crash")
    api = FakeTelegram()
    dead = Sender(db, ctx, api)

    async def die(db_, row, channel, provider_id):     # the process dies right after Telegram's 200
        raise SystemExit("kill -9")
    monkeypatch.setattr(ob, "mark_sent", die)
    claimed = [r for r in await ob.claim(db, token=dead.token) if r["id"] == rid]
    with pytest.raises(SystemExit):
        await dead.deliver(claimed[0])
    assert (await _row(db, rid))["status"] == "sending" and len(api.calls) == 1
    monkeypatch.undo()
    restarted = Sender(db, ctx, api)                    # what it remembered died with it
    await restarted.run_once()
    r = await _row(db, rid)
    assert r["status"] == "sent" and len(api.calls) == 2            # exactly one duplicate
    assert api.calls[0][1] == api.calls[1][1]                        # the same message
    for _ in range(3):
        await restarted.run_once()
    assert len(api.calls) == 2


# --- throttle (§6.4) --------------------------------------------------------------------------------
async def test_about_one_message_per_second_per_chat(db, ctx, monkeypatch):
    a1, a2 = await _chat_rows(db, 5611, 2, "thr")
    async with db.connection() as conn:
        b1 = (await (await conn.execute(
            """INSERT INTO outbox (dedupe_key, recipient, chat_id, purpose, text)
               VALUES (%s, 'chat', 5612, 'x', 'other chat') RETURNING id""", (f"thr-b:{time.time_ns()}",))).fetchone())["id"]
    clock = {"t": 5000.0}
    # Only the outbox's clock (patching time.monotonic itself would freeze the event loop's clock too).
    monkeypatch.setattr(ob, "time", SimpleNamespace(monotonic=lambda: clock["t"]))
    api = FakeTelegram()
    sender = Sender(db, ctx, api)
    await sender.run_once()                             # one row per chat: a1 and b1 go together
    assert {(await _row(db, x))["status"] for x in (a1, b1)} == {"sent"}
    clock["t"] += 0.3
    await sender.run_once()                             # a2: same chat 0.3 s later -> deferred, not sent
    r = await _row(db, a2)
    assert r["status"] == "pending" and r["attempts"] == 0 and len(api.calls) == 2
    async with db.connection() as conn:
        wait = (await (await conn.execute("SELECT %s::timestamptz - now() AS d", (r["next_attempt_at"],))).fetchone())["d"]
    assert 0.5 < wait.total_seconds() <= 0.7            # the rest of the second
    clock["t"] += 0.8
    await _due_now(db, a2)
    await sender.run_once()
    assert (await _row(db, a2))["status"] == "sent" and len(api.calls) == 3
    t_a1 = next(t for m, p, t in api.calls if p["chat_id"] == 5611 and p["text"].endswith(" 0"))
    t_a2 = next(t for m, p, t in api.calls if p["chat_id"] == 5611 and p["text"].endswith(" 1"))
    assert t_a2 - t_a1 >= ob.PER_CHAT_MIN_INTERVAL_S == 1.0


# --- invites expire after 10 minutes (§6.7, §9) ----------------------------------------------------
async def test_an_invite_expires_after_ten_minutes_on_the_web_and_in_telegram(client, db, ctx):
    w = await make_world(client, db, contacts=0)
    async with db.connection() as conn:
        now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    web, tg = [(await client.post("/v1/contacts", json={"name": n}, headers=w.wearer)).json() for n in ("W", "T")]
    for inv in (web, tg):
        left = (datetime.fromisoformat(inv["expires_at"]) - now).total_seconds()
        assert 599 <= left <= 601
    web_tok, tg_tok = (inv["web_link"].rsplit("/", 1)[1] for inv in (web, tg))
    info = (await client.get(f"/v1/contact/{web_tok}/invite")).json()
    assert info["valid"] is True and 595 <= info["expires_in_s"] <= 600
    async with db.connection() as conn:          # ten minutes pass
        await conn.execute("UPDATE contacts SET invite_expires_at = now() - interval '1 second' WHERE id = ANY(%s)",
                           ([web["contact_id"], tg["contact_id"]],))
    assert (await client.get(f"/v1/contact/{web_tok}/invite")).status_code == 410
    assert (await client.post(f"/v1/contact/{web_tok}/join", json={})).status_code == 410
    async with db.connection() as conn:
        base = await telegram.get_offset(conn)
    await telegram.handle_update(db, ctx, {"update_id": base + 700001, "message": {
        "message_id": 1, "chat": {"id": 7301, "type": "private"}, "from": {"id": 7301, "first_name": "Tia"},
        "text": f"/start {tg_tok}"}})
    async with db.connection() as conn:
        rows = await (await conn.execute(
            "SELECT id, joined_at, telegram_chat_id FROM contacts WHERE id = ANY(%s)",
            ([web["contact_id"], tg["contact_id"]],))).fetchall()
    assert all(r["joined_at"] is None and r["telegram_chat_id"] is None for r in rows)
    said = await outbox(db, recipient="chat")
    assert [o["template"] for o in said if o["chat_id"] == 7301] == ["invite_invalid"]
    # The wearer can still approve nobody: there is no one pending.
    assert (await client.post(f"/v1/contacts/{web['contact_id']}/approve", headers=w.wearer)).status_code in (404, 409)


# --- LOST_CONTACT in the real profile (§6.2) ---------------------------------------------------------
async def _seconds_until(db, ts) -> float:
    async with db.connection() as conn:
        return (await (await conn.execute("SELECT %s::timestamptz - now() AS d", (ts,))).fetchone())["d"].total_seconds()


async def test_real_profile_waits_five_minutes_before_telling_contacts(client, db, ctx):
    w = await make_world(client, db, contacts=2, profile="real")
    sid = w.session_id
    assert (await client.post("/v1/device/heartbeat", headers=w.device(), json={})).status_code == 200
    await expire_leases(db, ctx, w)
    s = await session_row(db, sid)
    assert s["link_state"] == "LOST_CONTACT" and s["state"] == states.ARMED
    assert (await outbox(db, sid, recipient="wearer"))[-1]["template"] == "wearer_lost_contact"   # her first
    assert await outbox(db, sid, recipient="contact") == []
    notify = [t for t in await pending_timers(db, sid) if t["kind"] == states.LOST_CONTACT_NOTIFY]
    assert len(notify) == 1 and 295 <= await _seconds_until(db, notify[0]["fire_at"]) <= 300
    assert ctx.settings.timings("real").lost_contact_grace_s == 300
    await fire(db, ctx, sid, states.LOST_CONTACT_NOTIFY)              # five minutes, not restored
    told = await outbox(db, sid, recipient="contact")
    assert [o["template"] for o in told] == ["lost_contact"] and told[0]["contact_id"] == w.contacts[0]["id"]
    assert "Cause unknown." in told[0]["text"] and "Her check-in schedule is still running." in told[0]["text"]
    assert (await open_alert(db, sid))["kind"] == "LOST_CONTACT"
    assert (await session_row(db, sid))["state"] == states.ARMED      # a notice, never an escalation


async def test_real_profile_tells_contacts_when_a_check_in_comes_due_while_the_link_is_lost(client, db, ctx):
    w = await make_world(client, db, contacts=1, profile="real")
    sid = w.session_id
    await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    await expire_leases(db, ctx, w)
    assert await outbox(db, sid, recipient="contact") == []
    await fire(db, ctx, sid, states.CHECKIN_DUE)                      # inside the 5-minute grace
    s = await session_row(db, sid)
    assert s["state"] == states.PROMPTED and s["link_state"] == "LOST_CONTACT"
    told = await outbox(db, sid, recipient="contact")
    assert [o["template"] for o in told] == ["lost_contact"]
    assert states.LOST_CONTACT_NOTIFY not in [t["kind"] for t in await pending_timers(db, sid)]  # told once
    # The prompt's own deadline still runs and escalates as NO_RESPONSE.
    await fire(db, ctx, sid, states.PROMPT_TIMEOUT)
    await fire(db, ctx, sid, states.REMINDER_TIMEOUT)
    assert (await session_row(db, sid))["state"] == "ESCALATED_L1"
    assert (await open_alert(db, sid))["kind"] == "NO_RESPONSE"


async def test_real_profile_restored_inside_the_grace_tells_no_contact(client, db, ctx):
    w = await make_world(client, db, contacts=1, profile="real")
    sid = w.session_id
    await client.post("/v1/device/heartbeat", headers=w.device(), json={})
    await expire_leases(db, ctx, w)
    assert (await client.post("/v1/device/heartbeat", headers=w.device(), json={})).status_code == 200
    s = await session_row(db, sid)
    assert s["link_state"] == "OK"
    assert await outbox(db, sid, recipient="contact") == []
    assert states.LOST_CONTACT_NOTIFY not in [t["kind"] for t in await pending_timers(db, sid)]
    assert [o["template"] for o in await outbox(db, sid, recipient="wearer")][-2:] == ["wearer_lost_contact",
                                                                                       "wearer_restored"]


# --- DB unreachable (§6.9) ---------------------------------------------------------------------------
DOWN = Settings(database_url="postgresql://stillhere@127.0.0.1:1/unreachable", admin_key="down-admin",
                app_secret="down-secret", run_workers=False, auto_migrate=True, demo_seed=False)


@pytest.fixture
async def down_client():
    from app.main import create_app
    application = create_app(DOWN, start_workers=False)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://down",
                                     timeout=30) as c:
            yield c


async def test_database_unreachable_answers_503_never_a_code_the_bridge_drops(down_client):
    dev = {"Authorization": "Bearer k", "X-Device-Id": "d", "X-Boot-Id": "b", "X-Seq": "1"}
    t0 = time.monotonic()
    hb, ev, health = await asyncio.gather(
        down_client.post("/v1/device/heartbeat", headers=dev, json={}),
        down_client.post("/v1/device/events", headers=dev, json={"event_id": "b:1", "type": "squeeze",
                                                                 "payload": {"pattern": "duress", "k": 3}}),
        down_client.get("/v1/health"))
    assert time.monotonic() - t0 < 15
    for r in (hb, ev):
        assert r.status_code == 503 and r.json() == {"ok": False, "error": "database unavailable"}
    assert health.status_code == 503
    assert 503 not in br.PERMANENT_HTTP                  # the bridge keeps the event and retries it


async def test_a_duress_squeeze_made_during_a_db_outage_is_delivered_after_recovery(client, db, ctx, down_client):
    """The real bridge, the real API: a 503 from the DB-down server, then the same event to the healthy one."""
    w = await make_world(client, db, contacts=1)
    await to_state(client, db, ctx, w, states.PROMPTED)
    loop = asyncio.get_running_loop()
    state = {"down": False, "statuses": []}

    def handler(request: httpx.Request) -> httpx.Response:
        target = down_client if state["down"] else client
        headers = {k: v for k, v in request.headers.items()
                   if k in ("authorization", "x-device-id", "x-boot-id", "x-seq", "content-type")}
        fut = asyncio.run_coroutine_threadsafe(
            target.request(request.method, request.url.path, content=request.content, headers=headers), loop)
        r = fut.result(timeout=30)
        state["statuses"].append((request.url.path, r.status_code))
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    clock = {"t": 100.0}
    http = httpx.Client(base_url="http://bridge.test", transport=httpx.MockTransport(handler))
    b = br.Bridge(http=http, device_id=w.device_id, device_key=w.device_key, write_line=lambda _l: None,
                  clock=lambda: clock["t"])
    try:
        await asyncio.to_thread(b.handle_line, "H,b1,500,10,0,400,0")
        await asyncio.to_thread(b.tick)                                   # the device shows the prompt
        assert b.challenge_id is not None
        state["down"] = True                                              # the database goes away
        await asyncio.to_thread(b.handle_line, 'E,b1:501,squeeze,{"pattern":"duress","k":3}')
        assert state["statuses"][-1] == ("/v1/device/events", 503)
        assert len(b.events) == 1 and b.events[0].buffered and b.events[0].duress
        assert (await session_row(db, w.session_id))["state"] == states.PROMPTED   # nothing reached it
        state["down"] = False                                             # the database is back
        clock["t"] += 1.5
        await asyncio.to_thread(b.tick)
        assert state["statuses"][-1] == ("/v1/device/events", 200) and b.events == []
    finally:
        http.close()
    s = await session_row(db, w.session_id)
    assert s["state"] == "ESCALATED_L1" and s["public_state"] == states.ARMED      # masked like any duress
    a = await open_alert(db, w.session_id)
    assert a["kind"] == "DURESS" and a["masked"] is True
    assert [o["template"] for o in await outbox(db, w.session_id, recipient="contact")] == ["alert_duress"]
