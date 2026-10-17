"""Outbox sender: at-least-once delivery with a dedupe key.

Rows are claimed with UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
and a 30 s lease (locked_until); a reaper returns stale 'sending' rows to
'pending'. Network errors, timeouts and 5xx are retried until the outage ends
(backoff capped at 60 s); only a permanent 4xx rejection ends as 'failed'. A crash between the provider accepting a message and the row being
marked 'sent' can therefore send it twice: a deliberate safety trade-off.

Channels:
  telegram  bot token set and the recipient has a chat id
  web       a contact with no Telegram chat (they read it on the live page)
  log       no bot token, or a wearer with no chat bound (logged, marked sent)
When a wearer prompt is marked sent, its deadline is lowered to sent_at + window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from .engine import Ctx

log = logging.getLogger("stillhere.outbox")

LEASE_S = 30
MAX_ATTEMPTS = 8
PER_CHAT_MIN_INTERVAL_S = 1.0  # about one message per second per chat


class TelegramAPI:
    """Minimal Bot API client. Never logs the token."""

    def __init__(self, token: str, client: httpx.AsyncClient, base: str = "https://api.telegram.org"):
        self._token = token
        self._client = client
        self._base = base.rstrip("/")

    def scrub(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def call(self, method: str, payload: dict, timeout: float = 15.0) -> dict:
        try:
            r = await self._client.post(f"{self._base}/bot{self._token}/{method}", json=payload, timeout=timeout)
        except httpx.HTTPError as exc:
            return {"ok": False, "status": 0, "description": self.scrub(f"{type(exc).__name__}: {exc}")}
        try:
            data = r.json()
        except ValueError:
            data = {"ok": False, "description": f"non-JSON reply ({r.status_code})"}
        data.setdefault("ok", r.is_success)
        data["status"] = r.status_code
        return data


async def reap(db) -> int:
    async with db.connection() as conn:
        cur = await conn.execute(
            """UPDATE outbox SET status = 'pending', locked_until = NULL
                WHERE status = 'sending' AND locked_until < now()""")
        return cur.rowcount


async def claim(db, limit: int = 10) -> list[dict]:
    async with db.connection() as conn:
        cur = await conn.execute(
            """UPDATE outbox SET status = 'sending', locked_until = now() + make_interval(secs => %s),
                      attempts = attempts + 1
                WHERE id IN (SELECT id FROM outbox
                              WHERE status = 'pending' AND next_attempt_at <= now()
                              ORDER BY next_attempt_at, id
                              LIMIT %s
                              FOR UPDATE SKIP LOCKED)
            RETURNING *""", (LEASE_S, limit))
        rows = list(await cur.fetchall())
    rows.sort(key=lambda r: r["id"])
    return rows


async def mark_sent(db, row: dict, channel: str, provider_id: Optional[str]) -> None:
    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """UPDATE outbox SET status = 'sent', sent_at = now(), channel = %s,
                          provider_message_id = %s, locked_until = NULL
                    WHERE id = %s AND status = 'sending'""", (channel, provider_id, row["id"]))
            if cur.rowcount and row.get("timer_id") and row.get("prompt_window_s"):
                # Prompt deadlines start at delivery.
                await conn.execute(
                    """UPDATE timers SET fire_at = LEAST(fire_at, now() + make_interval(secs => %s))
                        WHERE id = %s AND fired_at IS NULL AND cancelled_at IS NULL""",
                    (float(row["prompt_window_s"]), row["timer_id"]))


async def release(db, row: dict, delay_s: float, error: Optional[str] = None, count_attempt: bool = True,
                  may_fail: bool = True) -> None:
    """Back to pending, retried no earlier than now + delay_s. Only a permanent rejection
    (may_fail) ends as 'failed' after MAX_ATTEMPTS: an outage is retried for as long as it lasts."""
    async with db.connection() as conn:
        await conn.execute(
            """UPDATE outbox
                  SET status = CASE WHEN %s AND %s AND attempts >= %s THEN 'failed' ELSE 'pending' END,
                      attempts = CASE WHEN %s THEN attempts ELSE greatest(attempts - 1, 0) END,
                      next_attempt_at = now() + make_interval(secs => %s),
                      locked_until = NULL, last_error = coalesce(left(%s, 500), last_error)
                WHERE id = %s AND status = 'sending'""",
            (may_fail, count_attempt, MAX_ATTEMPTS, count_attempt, float(delay_s), error, row["id"]))


async def _chat_id(db, row: dict) -> tuple[Optional[int], str]:
    """(chat_id, fallback channel) for a row."""
    if row["recipient"] == "chat":
        return row["chat_id"], "log"
    async with db.connection() as conn:
        if row["recipient"] == "wearer":
            r = await (await conn.execute("SELECT telegram_chat_id FROM users WHERE id = %s",
                                          (row["user_id"],))).fetchone()
            return (r["telegram_chat_id"] if r else None), "log"
        r = await (await conn.execute("SELECT telegram_chat_id FROM contacts WHERE id = %s",
                                      (row["contact_id"],))).fetchone()
        return (r["telegram_chat_id"] if r else None), "web"


class Sender:
    def __init__(self, db, ctx: Ctx, api: Optional[TelegramAPI] = None):
        self.db = db
        self.ctx = ctx
        self.api = api
        self._last_by_chat: dict[int, float] = {}

    async def deliver(self, row: dict) -> str:
        chat_id, fallback = await _chat_id(self.db, row)
        if self.api is None or chat_id is None:
            channel = fallback
            if channel == "log":
                log.info("[outbox:log] %s %s: %s", row["recipient"], row.get("contact_id") or "", row["text"])
            await mark_sent(self.db, row, channel, f"{channel}:{row['id']}")
            return channel
        wait = PER_CHAT_MIN_INTERVAL_S - (time.monotonic() - self._last_by_chat.get(chat_id, -1e9))
        if wait > 0:
            await release(self.db, row, wait, count_attempt=False)
            return "deferred"
        payload: dict = {"chat_id": chat_id, "text": row["text"]}
        if row.get("reply_markup"):
            payload["reply_markup"] = row["reply_markup"]
        method = row["method"]
        if method == "editMessageText":
            payload["message_id"] = row["edit_message_id"]
        elif row.get("protect_content"):
            payload["protect_content"] = True
        self._last_by_chat[chat_id] = time.monotonic()
        res = await self.api.call(method, payload)
        if res.get("ok"):
            mid = (res.get("result") or {}).get("message_id") if isinstance(res.get("result"), dict) else None
            await mark_sent(self.db, row, "telegram", str(mid) if mid is not None else None)
            return "telegram"
        status = res.get("status") if res.get("status") is not None else res.get("error_code")
        desc = str(res.get("description", ""))
        if status == 429:
            retry_after = float((res.get("parameters") or {}).get("retry_after") or 1)
            await release(self.db, row, retry_after, f"429 retry_after={retry_after}", count_attempt=False)
            return "rate_limited"
        if method == "editMessageText" and status == 400:
            # The original message cannot be edited (too old, deleted): send instead.
            async with self.db.connection() as conn:
                await conn.execute(
                    """UPDATE outbox SET method = 'sendMessage', edit_message_id = NULL
                        WHERE id = %s""", (row["id"],))
            await release(self.db, row, 0, desc, count_attempt=False)
            return "edit_fallback"
        backoff = min(60.0, 2.0 ** max(1, row["attempts"]))
        # No answer, a timeout or a server error is an outage: retry (backoff capped at 60 s) until
        # it ends. Only a client error (bad request, bot blocked) can use up MAX_ATTEMPTS.
        permanent = bool(status) and 400 <= int(status) < 500 and int(status) != 408
        await release(self.db, row, backoff, f"{status}: {desc}", may_fail=permanent)
        return "retry"

    async def run_once(self) -> int:
        await reap(self.db)
        rows = await claim(self.db)
        for row in rows:
            try:
                await self.deliver(row)
            except Exception as exc:
                log.exception("outbox row %s failed", row["id"])
                msg = self.api.scrub(repr(exc)) if self.api else repr(exc)
                await release(self.db, row, min(60.0, 2.0 ** max(1, row["attempts"])), msg, may_fail=False)
        return len(rows)


async def outbox_sender(db, ctx: Ctx, stop: asyncio.Event, client: Optional[httpx.AsyncClient] = None,
                        idle_s: float = 0.5) -> None:
    own = client is None
    client = client or httpx.AsyncClient()
    api = TelegramAPI(ctx.settings.telegram_bot_token, client, ctx.settings.telegram_api_base) \
        if ctx.settings.telegram_enabled else None
    sender = Sender(db, ctx, api)
    log.info("outbox sender started (channel: %s)", "telegram" if api else "log")
    try:
        while not stop.is_set():
            n = 0
            try:
                n = await sender.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("outbox iteration failed")
            if n == 0:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=idle_s)
                except asyncio.TimeoutError:
                    pass
    finally:
        if own:
            await client.aclose()
