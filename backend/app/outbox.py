"""Outbox sender: at-least-once delivery with a dedupe key.

Rows are claimed with UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)
and a 30 s lease (locked_until), in queue order per recipient (a row waits for every earlier
unsent row to the same recipient); a reaper returns stale 'sending' rows to
'pending', and a starting sender at once returns rows an earlier (killed)
sender had claimed (claimed_by). Network errors, timeouts and 5xx are retried until the outage ends
(backoff capped at 60 s); only a permanent 4xx rejection ends as 'failed'. A crash between the provider accepting a message and the row being
marked 'sent' can therefore send it twice: a deliberate safety trade-off. Within one sender process a mark
that fails is retried without sending again (Sender._accepted), so a failing mark never re-sends; only a
restart in that window can, once.

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
import uuid
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


async def reclaim_orphans(db, token: str) -> int:
    """At sender start: rows another (killed) sender left in 'sending' go back to pending now.

    With two live senders on one database this can resend a row the other is sending: at least
    once allows that duplicate."""
    async with db.connection() as conn:
        cur = await conn.execute(
            """UPDATE outbox SET status = 'pending', locked_until = NULL
                WHERE status = 'sending' AND claimed_by IS DISTINCT FROM %s""", (token,))
        return cur.rowcount


async def claim(db, limit: int = 10, token: Optional[str] = None) -> list[dict]:
    """Due rows, oldest first, at most one per recipient: a recipient's next row waits until every
    earlier one is sent or has failed, so a retried alert never lands after the false alarm that
    closed it."""
    async with db.connection() as conn:
        cur = await conn.execute(
            """UPDATE outbox SET status = 'sending', locked_until = now() + make_interval(secs => %s),
                      attempts = attempts + 1, claimed_by = %s
                WHERE id IN (SELECT o.id FROM outbox o
                              WHERE o.status = 'pending' AND o.next_attempt_at <= now()
                                AND NOT EXISTS (
                                    SELECT 1 FROM outbox e
                                     WHERE e.id < o.id AND e.status IN ('pending', 'sending')
                                       AND e.recipient = o.recipient
                                       AND CASE o.recipient WHEN 'contact' THEN e.contact_id = o.contact_id
                                                            WHEN 'wearer' THEN e.user_id = o.user_id
                                                            ELSE e.chat_id = o.chat_id END)
                              ORDER BY o.next_attempt_at, o.id
                              LIMIT %s
                              FOR UPDATE OF o SKIP LOCKED)
            RETURNING *""", (LEASE_S, token, limit))
        rows = list(await cur.fetchall())
    rows.sort(key=lambda r: r["id"])
    return rows


async def mark_sent(db, row: dict, channel: str, provider_id: Optional[str]) -> None:
    async with db.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                """UPDATE outbox SET status = 'sent', sent_at = now(), channel = %s,
                          provider_message_id = %s, locked_until = NULL
                    WHERE id = %s AND status = 'sending' AND claimed_by IS NOT DISTINCT FROM %s""",
                (channel, provider_id, row["id"], row.get("claimed_by")))
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
                WHERE id = %s AND status = 'sending' AND claimed_by IS NOT DISTINCT FROM %s""",
            (may_fail, count_attempt, MAX_ATTEMPTS, count_attempt, float(delay_s), error, row["id"],
             row.get("claimed_by")))


REMOVED = "removed"


async def _chat_id(db, row: dict) -> tuple[Optional[int], str]:
    """(chat_id, fallback channel) for a row; channel REMOVED for a contact the wearer removed."""
    if row["recipient"] == "chat":
        return row["chat_id"], "log"
    async with db.connection() as conn:
        if row["recipient"] == "wearer":
            r = await (await conn.execute("SELECT telegram_chat_id FROM users WHERE id = %s",
                                          (row["user_id"],))).fetchone()
            return (r["telegram_chat_id"] if r else None), "log"
        r = await (await conn.execute("SELECT telegram_chat_id, revoked_at FROM contacts WHERE id = %s",
                                      (row["contact_id"],))).fetchone()
        if r is None or r["revoked_at"] is not None:
            return None, REMOVED
        return r["telegram_chat_id"], "web"


async def drop(db, row: dict, reason: str) -> None:
    """Never send this row (its contact was removed)."""
    async with db.connection() as conn:
        await conn.execute(
            """UPDATE outbox SET status = 'failed', last_error = %s, locked_until = NULL
                WHERE id = %s AND status = 'sending' AND claimed_by IS NOT DISTINCT FROM %s""",
            (reason, row["id"], row.get("claimed_by")))


class Sender:
    def __init__(self, db, ctx: Ctx, api: Optional[TelegramAPI] = None):
        self.db = db
        self.ctx = ctx
        self.api = api
        self._last_by_chat: dict[int, float] = {}
        # Rows the provider accepted whose mark as 'sent' failed: retried as a mark only, never re-sent.
        self._accepted: dict[int, tuple[str, Optional[str]]] = {}
        self.token = uuid.uuid4().hex  # this sender's claims
        self._started = False

    async def _mark(self, row: dict, channel: str, provider_id: Optional[str]) -> None:
        self._accepted[row["id"]] = (channel, provider_id)
        await mark_sent(self.db, row, channel, provider_id)
        self._accepted.pop(row["id"], None)

    async def deliver(self, row: dict) -> str:
        if row["id"] in self._accepted:
            # Already accepted by the provider; only marking it failed. Mark it, do not send it again.
            channel, provider_id = self._accepted[row["id"]]
            await self._mark(row, channel, provider_id)
            return channel
        chat_id, fallback = await _chat_id(self.db, row)
        if fallback == REMOVED:
            await drop(self.db, row, "contact removed")
            return REMOVED
        if self.api is None or chat_id is None:
            channel = fallback
            if channel == "log":
                log.info("[outbox:log] %s %s: %s", row["recipient"], row.get("contact_id") or "", row["text"])
            await self._mark(row, channel, f"{channel}:{row['id']}")
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
            await self._mark(row, "telegram", str(mid) if mid is not None else None)
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
        if not self._started:
            await reclaim_orphans(self.db, self.token)
            self._started = True
        await reap(self.db)
        rows = await claim(self.db, token=self.token)
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
