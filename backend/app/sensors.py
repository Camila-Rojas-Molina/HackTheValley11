"""Heart-rate rule evaluation and the badged heart-rate simulator.

Runs from the timer worker. Rule output is a PullForward only, applied through
engine.maybe_pull_forward (ARMED -> PROMPTED with a counted challenge).
"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Optional

from . import engine, rules, states
from .engine import Ctx

log = logging.getLogger("stillhere.sensors")

SENSOR_TICK_S = 2.0
HR_SOURCE_KEY = "hr_source"
HR_SOURCE_MODES = ("live", "sim:steady", "sim:climb_still", "sim:lost")
SIM_STILL_ENMO_MG = 10.0  # simulated stillness for sim:climb_still when no device is streaming

_last_eval: dict[str, float] = {}


async def get_hr_source(conn) -> dict:
    row = await (await conn.execute("SELECT value FROM bot_state WHERE key = %s", (HR_SOURCE_KEY,))).fetchone()
    return dict(row["value"]) if row else {"mode": "live"}


async def set_hr_source(conn, mode: str) -> dict:
    if mode not in HR_SOURCE_MODES:
        raise ValueError(mode)
    row = await (await conn.execute(
        """INSERT INTO bot_state (key, value, updated_at)
           VALUES (%s, jsonb_build_object('mode', %s::text, 'since', now()), now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
           RETURNING value""", (HR_SOURCE_KEY, mode))).fetchone()
    return dict(row["value"])


async def simulate_hr(db, ctx: Ctx) -> int:
    """Insert one simulated sample per armed demo wearer (source='sim', simulated=true)."""
    async with db.connection() as conn:
        src = await get_hr_source(conn)
        mode = src.get("mode", "live")
        if mode in ("live", "sim:lost"):
            return 0
        rows = await (await conn.execute(
            """SELECT u.id AS user_id, coalesce(b.value, %s) AS baseline, now() AS now
                 FROM users u
                 JOIN sessions s ON s.user_id = u.id AND s.state <> 'ENDED'
                 LEFT JOIN user_baselines b ON b.user_id = u.id AND b.kind = 'hr_rest'
                WHERE u.is_demo""", (ctx.settings.demo_hr_baseline,))).fetchall()
        since = datetime.fromisoformat(src["since"]) if src.get("since") else None
        n = 0
        for r in rows:
            base = float(r["baseline"])
            if mode == "sim:climb_still" and since is not None:
                elapsed = max(0.0, (r["now"] - since).total_seconds())
                bpm = base + min(45.0, 3.0 * elapsed)
            else:
                bpm = base + random.uniform(-2.0, 2.0)
            await conn.execute(
                """INSERT INTO hr_samples (time, user_id, source, simulated, bpm)
                   VALUES (now(), %s, 'sim', true, %s)""", (r["user_id"], int(round(bpm))))
            n += 1
        return n


def _parse(ts) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)


async def evaluate_hr(db, ctx: Ctx, force: bool = False) -> int:
    """Evaluate the heart-rate rules for every active session. Returns pull-forwards applied."""
    applied_n = 0
    async with db.connection() as conn:
        src = await get_hr_source(conn)
        rows = await (await conn.execute(
            """SELECT s.id, s.user_id, s.state, s.profile, s.hr_deviating_since, s.pull_forward_last,
                      s.created_at, b.value AS baseline,
                      d.last_enmo60_mg, d.lease_expires_at, d.last_vigorous_at
                 FROM sessions s
                 LEFT JOIN user_baselines b ON b.user_id = s.user_id AND b.kind = 'hr_rest'
                 LEFT JOIN devices d ON d.id = s.device_id
                WHERE s.state <> 'ENDED'""")).fetchall()
        for r in rows:
            sid = str(r["id"])
            t = ctx.settings.timings(r["profile"])
            if not force and time.monotonic() - _last_eval.get(sid, 0.0) < t.hr_eval_s:
                continue
            _last_eval[sid] = time.monotonic()
            async with conn.transaction():
                stats = await (await conn.execute(
                    """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY bpm) AS med,
                              bool_or(simulated) AS sim, count(*) AS n, now() AS now
                         FROM hr_samples WHERE user_id = %s AND time > now() - interval '60 seconds'""",
                    (r["user_id"],))).fetchone()
                now = stats["now"]
                last = (await (await conn.execute(
                    "SELECT max(time) AS last FROM hr_samples WHERE user_id = %s AND time >= %s",
                    (r["user_id"], r["created_at"]))).fetchone())["last"]
                fresh = r["lease_expires_at"] is not None and r["lease_expires_at"] > now
                enmo = r["last_enmo60_mg"] if fresh else None
                if enmo is None and src.get("mode") == "sim:climb_still":
                    enmo = SIM_STILL_ENMO_MG
                res = rules.hr_rule(
                    now=now, median_60s=float(stats["med"]) if stats["med"] is not None else None,
                    baseline=float(r["baseline"]) if r["baseline"] is not None else None,
                    deviating_since=r["hr_deviating_since"], sustain_s=t.hr_sustain_s, enmo60_mg=enmo,
                    last_vigorous_at=r["last_vigorous_at"], simulated=bool(stats["sim"]))
                if res.deviating_since != r["hr_deviating_since"]:
                    await conn.execute("UPDATE sessions SET hr_deviating_since = %s WHERE id = %s",
                                       (res.deviating_since, r["id"]))
                if res.pull_forward is not None and r["state"] == states.ARMED:
                    a = await engine.maybe_pull_forward(conn, r["id"], res.pull_forward, actor="rule:hr",
                                                        event_key=f"hr:{sid}:{now.isoformat()}", ctx=ctx)
                    if a is not None and a.decision.changed:
                        applied_n += 1
                pfl = r["pull_forward_last"] or {}
                lost = rules.hr_lost_rule(now=now, armed=r["state"] == states.ARMED, last_sample_at=last,
                                          lost_after_s=t.hr_lost_s,
                                          already_fired_for=_parse(pfl.get("hr_signal_lost_for")))
                if lost is not None:
                    a = await engine.maybe_pull_forward(conn, r["id"], lost, actor="rule:hr",
                                                        event_key=f"hrlost:{sid}:{last.isoformat()}",
                                                        ctx=ctx, bypass_cooldown=True)
                    await conn.execute(
                        """UPDATE sessions SET pull_forward_last = pull_forward_last
                                  || jsonb_build_object('hr_signal_lost_for', %s::timestamptz)
                            WHERE id = %s""", (last, r["id"]))
                    if a is not None and a.decision.changed:
                        applied_n += 1
    return applied_n


async def tick(db, ctx: Ctx) -> None:
    await simulate_hr(db, ctx)
    await evaluate_hr(db, ctx)
