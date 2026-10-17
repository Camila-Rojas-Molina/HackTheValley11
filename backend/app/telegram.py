"""Telegram long-poll bot (the "Reminders" bot).

* Singleton: the poller holds pg_try_advisory_lock on a dedicated connection
  outside the pool; a second process stays idle until the lock frees.
* getUpdates(timeout=25, allowed_updates=[message, callback_query]); the offset
  is stored in bot_state in the same transaction that processes the update.
* /start <token>: binds a contact invite (pending until the wearer approves)
  or the wearer's own chat (for "Reminder" nudges the Apple Watch mirrors).
* Inline buttons: ack / pass / safe / cant -> CONTACT_* events.
With no TELEGRAM_BOT_TOKEN the poller does nothing and the outbox logs instead.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Optional

import httpx
import psycopg

from . import audit, engine, messages, states
from .engine import Ctx
from .outbox import TelegramAPI
from .security import sha256_hex

log = logging.getLogger("stillhere.telegram")

POLLER_LOCK_ID = 7_202_612
OFFSET_KEY = "tg_offset"
ACTIONS = {
    "ack": (states.CONTACT_ACK, "ack"),
    "pass": (states.CONTACT_PASS, "pass"),
    "safe": (states.CONTACT_SAFE, "safe"),
    "cant": (states.CONTACT_CANT_REACH, "cant_reach"),
}


async def get_offset(conn) -> int:
    row = await (await conn.execute("SELECT value FROM bot_state WHERE key = %s", (OFFSET_KEY,))).fetchone()
    return int(row["value"]["offset"]) if row else 0


async def _set_offset(conn, offset: int) -> None:
    await conn.execute(
        """INSERT INTO bot_state (key, value, updated_at) VALUES (%s, jsonb_build_object('offset', %s::bigint), now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""", (OFFSET_KEY, offset))


async def _say(conn, chat_id: int, template: str, params: dict, key: str) -> None:
    await conn.execute(
        """INSERT INTO outbox (dedupe_key, recipient, chat_id, purpose, template, text)
           VALUES (%s, 'chat', %s, %s, %s, %s) ON CONFLICT (dedupe_key) DO NOTHING""",
        (key, chat_id, template, template, messages.render(template, params)))


async def handle_update(db, ctx: Ctx, update: dict) -> Optional[dict]:
    """Process one update exactly once. Returns an answerCallbackQuery payload, if any."""
    uid = int(update["update_id"])
    answer: Optional[dict] = None
    async with db.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (POLLER_LOCK_ID + 1,))
            if uid < await get_offset(conn):
                return None  # already processed
            try:
                async with conn.transaction():  # savepoint: a bad update must not block the queue
                    if "callback_query" in update:
                        answer = await _handle_callback(conn, ctx, update)
                    elif "message" in update:
                        await _handle_message(conn, ctx, update)
            except Exception:
                log.exception("telegram update %s failed; skipping it", uid)
                answer = None
            await _set_offset(conn, uid + 1)
    return answer


async def _handle_message(conn, ctx: Ctx, update: dict) -> None:
    msg = update["message"]
    text = (msg.get("text") or "").strip()
    chat_id = msg["chat"]["id"]
    if not text.startswith("/start"):
        return
    parts = text.split(maxsplit=1)
    token = parts[1].strip() if len(parts) > 1 else ""
    key = f"tg:{update['update_id']}"
    if not token:
        return
    if (msg["chat"].get("type") or "private") != "private":
        # A group would receive every alert, and its buttons would never match one person.
        # The token is not used, so it still works in a private chat with the bot.
        await _say(conn, chat_id, "private_chat_only", {}, key)
        return
    th = sha256_hex(token)
    contact = await (await conn.execute(
        """SELECT c.id, c.user_id, c.name, c.web_token_nonce, u.display_name AS wearer
             FROM contacts c JOIN users u ON u.id = c.user_id
            WHERE c.invite_token_hash = %s AND c.invite_used_at IS NULL AND c.revoked_at IS NULL
              AND c.invite_expires_at > now()
            FOR UPDATE OF c""", (th,))).fetchone()
    if contact is not None:
        first = ((msg.get("from") or {}).get("first_name") or "").strip()[:60]
        nonce = contact["web_token_nonce"] or secrets.token_urlsafe(8)
        web_hash = sha256_hex(engine.contact_web_token(ctx, contact["id"], nonce))
        await conn.execute(
            """UPDATE contacts SET telegram_chat_id = %s, channel = 'telegram', joined_at = now(),
                      invite_used_at = now(), name = CASE WHEN name = '' THEN %s ELSE name END,
                      web_token_nonce = %s, web_token_hash = %s
                WHERE id = %s""", (chat_id, first or "Contact", nonce, web_hash, contact["id"]))
        await audit.append(conn, chain_key=audit.user_chain(contact["user_id"]), actor=f"contact:{contact['id']}",
                           kind="contact_joined", user_id=contact["user_id"], detail={"contact_id": contact["id"],
                                                                                       "channel": "telegram"})
        await _say(conn, chat_id, "contact_pending", {"name": contact["wearer"]}, key)
        return
    user = await (await conn.execute(
        """SELECT id FROM users WHERE tg_link_token_hash = %s AND tg_link_expires_at > now() FOR UPDATE""",
        (th,))).fetchone()
    if user is not None:
        await conn.execute(
            """UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s AND id <> %s""",
            (chat_id, user["id"]))
        await conn.execute(
            """UPDATE users SET telegram_chat_id = %s, tg_link_token_hash = NULL, tg_link_expires_at = NULL
                WHERE id = %s""", (chat_id, user["id"]))
        await audit.append(conn, chain_key=audit.user_chain(user["id"]), actor="wearer", kind="wearer_chat_linked",
                           user_id=user["id"], detail={})
        await _say(conn, chat_id, "wearer_linked", {}, key)
        return
    await _say(conn, chat_id, "invite_invalid", {}, key)


async def _handle_callback(conn, ctx: Ctx, update: dict) -> dict:
    cq = update["callback_query"]
    answer = {"callback_query_id": cq["id"], "text": messages.CALLBACK_ANSWERS["ignored"]}
    try:
        action, a, c = (cq.get("data") or "").split(":")
        alert_id, contact_id = int(a), int(c)
        event_kind, ack_action = ACTIONS[action]
    except (ValueError, KeyError):
        return answer
    from_id = (cq.get("from") or {}).get("id")
    contact = await (await conn.execute(
        """SELECT id FROM contacts WHERE id = %s AND telegram_chat_id = %s
              AND approved_at IS NOT NULL AND revoked_at IS NULL""", (contact_id, from_id))).fetchone()
    alert = await (await conn.execute(
        "SELECT id, session_id FROM alerts WHERE id = %s AND status IN ('OPEN', 'ACKED')",
        (alert_id,))).fetchone()
    if contact is None or alert is None:
        return answer
    ins = await (await conn.execute(
        """INSERT INTO acks (alert_id, contact_id, action, source, telegram_update_id)
           VALUES (%s, %s, %s, 'telegram', %s) ON CONFLICT (telegram_update_id) DO NOTHING RETURNING id""",
        (alert_id, contact_id, ack_action, update["update_id"]))).fetchone()
    if ins is None:
        return answer
    message = cq.get("message") or {}
    edit = {"message_id": message.get("message_id"), "prefix": message.get("text")}
    applied = await engine.apply(conn, alert["session_id"], states.Event(event_kind, contact_id=contact_id),
                                 actor=f"contact:{contact_id}", event_key=f"tg:{update['update_id']}", ctx=ctx,
                                 edit=edit)
    await conn.execute("UPDATE acks SET result = %s WHERE id = %s", (applied.decision.reply, ins["id"]))
    answer["text"] = messages.CALLBACK_ANSWERS.get(applied.decision.reply, messages.CALLBACK_ANSWERS["ignored"])
    return answer


async def telegram_poller(db, ctx: Ctx, stop: asyncio.Event, client: Optional[httpx.AsyncClient] = None) -> None:
    if not ctx.settings.telegram_enabled:
        log.info("telegram poller idle: TELEGRAM_BOT_TOKEN is not set (outbox uses the log channel)")
        await stop.wait()
        return
    own = client is None
    client = client or httpx.AsyncClient()
    api = TelegramAPI(ctx.settings.telegram_bot_token, client, ctx.settings.telegram_api_base)
    webhook_cleared = False
    backoff = 1.0
    try:
        while not stop.is_set():
            lock_conn = None
            try:
                lock_conn = await psycopg.AsyncConnection.connect(db.conninfo, autocommit=True)
                got = (await (await lock_conn.execute("SELECT pg_try_advisory_lock(%s)",
                                                      (POLLER_LOCK_ID,))).fetchone())[0]
                if not got:
                    log.info("another process holds the Telegram poller lock; waiting")
                    await _wait(stop, 15)
                    continue
                if not webhook_cleared:
                    await api.call("deleteWebhook", {})
                    webhook_cleared = True
                while not stop.is_set():
                    async with db.connection() as conn:
                        offset = await get_offset(conn)
                    res = await api.call("getUpdates", {"offset": offset, "timeout": 25,
                                                        "allowed_updates": ["message", "callback_query"]},
                                         timeout=35)
                    if not res.get("ok"):
                        if res.get("status") == 429:
                            await _wait(stop, float((res.get("parameters") or {}).get("retry_after") or 5))
                        else:
                            log.warning("getUpdates failed: %s", api.scrub(str(res.get("description"))))
                            await _wait(stop, backoff)
                            backoff = min(60.0, backoff * 2)
                        continue
                    backoff = 1.0
                    for upd in res.get("result") or []:
                        ans = await handle_update(db, ctx, upd)
                        if ans:
                            await api.call("answerCallbackQuery", ans)
                    await lock_conn.execute("SELECT 1")  # the lock connection is still alive
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("telegram poller error: %s", api.scrub(repr(exc)))
                await _wait(stop, backoff)
                backoff = min(60.0, backoff * 2)
            finally:
                if lock_conn is not None:
                    await lock_conn.close()
    finally:
        if own:
            await client.aclose()


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
