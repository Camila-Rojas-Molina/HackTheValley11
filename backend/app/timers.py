"""Durable timer worker.

Timers are rows. Each due timer is claimed with FOR UPDATE SKIP LOCKED (with
its session row) and processed in its own transaction: the transition, the
next timer, outbox rows and the timeline entry commit together, then the timer
is marked fired. A kill -9 before COMMIT rolls everything back and the next
process fires the same row from its stored deadline. SKIP LOCKED lets several
workers run safely.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from . import engine, sensors, states, timeseries
from .engine import Ctx
from .states import Event

log = logging.getLogger("stillhere.timers")

MAX_ATTEMPTS = 5  # after this many errors in a row a timer is retried every RETRY_CAP_S, never dropped
RETRY_CAP_S = 30
BATCH = 20


async def fire_one(conn, ctx: Ctx, session_id=None) -> Optional[int]:
    """Claim and fire one due timer (optionally of one session). Returns its id, or None."""
    claimed: Optional[dict] = None
    try:
        async with conn.transaction():
            claimed = await (await conn.execute(
                """SELECT t.id, t.session_id, t.kind, t.expected_version, t.attempts, s.version AS session_version
                     FROM timers t JOIN sessions s ON s.id = t.session_id
                    WHERE t.fired_at IS NULL AND t.cancelled_at IS NULL AND t.fire_at <= now()
                      AND (%(sid)s::uuid IS NULL OR t.session_id = %(sid)s::uuid)
                    ORDER BY t.fire_at, t.id
                    LIMIT 1
                    FOR UPDATE OF t, s SKIP LOCKED""", {"sid": session_id})).fetchone()
            if claimed is None:
                return None
            if claimed["expected_version"] is not None and claimed["expected_version"] != claimed["session_version"]:
                result = "stale"
            else:
                applied = await engine.apply(conn, claimed["session_id"], Event(claimed["kind"]),
                                             actor="timer", event_key=f"timer:{claimed['id']}", ctx=ctx)
                result = applied.decision.reply
            await conn.execute("UPDATE timers SET fired_at = now(), result = %s WHERE id = %s",
                               (result, claimed["id"]))
            return claimed["id"]
    except Exception as exc:  # the transaction above is rolled back
        if claimed is None:
            raise
        log.exception("timer %s (%s) failed (attempt %s)", claimed["id"], claimed["kind"], claimed["attempts"] + 1)
        async with conn.transaction():
            # A deadline is never dropped: it is retried (backoff capped at RETRY_CAP_S) until it fires,
            # and the operator sees it in /v1/health (failing_timers) and /v1/admin/timers. Only a timer
            # still pending is touched: another worker may have fired it since this one let go of it.
            await conn.execute(
                """UPDATE timers
                      SET attempts = attempts + 1,
                          last_error = left(%s, 500),
                          fire_at = now() + make_interval(secs => least(%s, 2 ^ least(attempts + 1, 10)))
                    WHERE id = %s AND fired_at IS NULL AND cancelled_at IS NULL""",
                (repr(exc), RETRY_CAP_S, claimed["id"]))
        return claimed["id"]


async def run_due(db, ctx: Ctx, limit: int = BATCH) -> int:
    fired = 0
    async with db.connection() as conn:
        for _ in range(limit):
            if await fire_one(conn, ctx) is None:
                break
            fired += 1
    return fired


async def check_leases(db, ctx: Ctx) -> int:
    """link_state OK -> LOST_CONTACT for sessions whose device lease ran out."""
    n = 0
    async with db.connection() as conn:
        async with conn.transaction():
            rows = await (await conn.execute(
                """SELECT s.id, d.lease_expires_at
                     FROM sessions s JOIN devices d ON d.id = s.device_id
                    WHERE s.state <> 'ENDED' AND s.link_state = 'OK'
                      AND d.lease_expires_at IS NOT NULL AND d.lease_expires_at < now()
                    LIMIT 10
                    FOR UPDATE OF s SKIP LOCKED""")).fetchall()
            for r in rows:
                async with conn.transaction():  # savepoint per session
                    await engine.apply(conn, r["id"], Event(states.LEASE_EXPIRED), actor="system",
                                       event_key=f"lease:{r['id']}:{r['lease_expires_at'].isoformat()}", ctx=ctx)
                    n += 1
    return n


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def timer_worker(db, ctx: Ctx, stop: asyncio.Event, tick_s: float = 1.0) -> None:
    log.info("timer worker started")
    last_sensor = 0.0
    while not stop.is_set():
        if not ctx.secret:
            # APP_SECRET is loaded by the database init: until then no transition may run, since
            # alerts sign location and live-page links with it.
            await _sleep(stop, tick_s)
            continue
        fired = 0
        try:
            fired = await run_due(db, ctx)
            await check_leases(db, ctx)
            if time.monotonic() - last_sensor >= sensors.SENSOR_TICK_S:
                last_sensor = time.monotonic()
                await sensors.tick(db, ctx)
            # Plain PostgreSQL has no scheduler: raw-row retention (night_out 48 h) runs from here,
            # at most every 10 minutes, whatever the ingest path. A no-op on TimescaleDB.
            await timeseries.maybe_prune(db)
            # Real locations older than 48 h (research 1.15), at most every 10 minutes.
            await engine.maybe_purge_locations(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("timer worker iteration failed")
        if fired < BATCH:
            await _sleep(stop, tick_s)
    log.info("timer worker stopped")
