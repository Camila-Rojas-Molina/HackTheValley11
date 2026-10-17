"""Operator endpoints (X-Admin-Key). Every injected change is logged with actor='demo'.

GET  /v1/admin/timers        pending timers with seconds left (?recent=N: last N fired, with lag)
GET  /v1/admin/overview      true states (operator view, includes masked alerts)
GET  /v1/admin/outbox        outbox rows (?session_id=&contact_id=&recipient=&limit=)
POST /v1/demo/checkin-now    routine check-in now (labelled demo)
POST /v1/demo/inject         {kind: head_position | fall_like | offbody} -> PULL_FORWARD only
POST /v1/demo/hr-source      {mode: live | sim:steady | sim:climb_still | sim:lost}
POST /v1/demo/profile        {profile: demo | real}
POST /v1/demo/fast-forward   fire the next pending deadline now
POST /v1/demo/judge-invite   fresh single-use invite + QR for the judge
POST /v1/demo/reset          back to the starting state (fresh armed demo session)
"""
from __future__ import annotations

import uuid
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from .. import audit, engine, rules, sensors, states
from ..deps import get_ctx, get_db, require_admin
from ..security import classify_pin, hash_pin, sha256_hex
from .contacts import create_invite
from .sessions import ArmIn, arm_session

router = APIRouter(tags=["operator"])


class InjectIn(BaseModel):
    kind: Literal["head_position", "fall_like", "offbody"]


class HrSourceIn(BaseModel):
    mode: Literal["live", "sim:steady", "sim:climb_still", "sim:lost"]


class ProfileIn(BaseModel):
    profile: Literal["demo", "real"]


class ResetIn(BaseModel):
    mode: Literal["wrist", "hair"] = "hair"
    hr_source: Literal["live", "sim:steady", "sim:climb_still", "sim:lost"] = "live"
    arm: bool = True


async def ensure_demo_world(conn, ctx) -> dict:
    """Demo wearer, device, PINs, simulated HR baseline and simulated location. Idempotent."""
    s = ctx.settings
    user = await (await conn.execute(
        "SELECT * FROM users WHERE is_demo ORDER BY created_at LIMIT 1")).fetchone()
    if user is None:
        user = await (await conn.execute(
            "INSERT INTO users (display_name, is_demo) VALUES (%s, true) RETURNING *",
            (s.demo_wearer_name,))).fetchone()
    if s.wearer_key and user["api_key_hash"] != sha256_hex(s.wearer_key):
        await conn.execute("UPDATE users SET api_key_hash = %s WHERE id = %s", (sha256_hex(s.wearer_key), user["id"]))
    if s.demo_pin and classify_pin(s.demo_pin, user["pin_hash"], None) != "real":
        await conn.execute("UPDATE users SET pin_hash = %s WHERE id = %s", (hash_pin(s.demo_pin), user["id"]))
    if s.demo_duress_pin and classify_pin(s.demo_duress_pin, None, user["duress_pin_hash"]) != "duress":
        await conn.execute("UPDATE users SET duress_pin_hash = %s WHERE id = %s",
                           (hash_pin(s.demo_duress_pin), user["id"]))
    if s.device_key:
        await conn.execute(
            """INSERT INTO devices (id, user_id, device_key_hash) VALUES (%s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET device_key_hash = EXCLUDED.device_key_hash, user_id = EXCLUDED.user_id""",
            (s.demo_device_id, user["id"], sha256_hex(s.device_key)))
    await conn.execute(
        """INSERT INTO user_baselines (user_id, kind, value, simulated) VALUES (%s, 'hr_rest', %s, true)
           ON CONFLICT (user_id, kind) DO NOTHING""", (user["id"], s.demo_hr_baseline))
    has_loc = await (await conn.execute("SELECT 1 FROM location_snapshots WHERE user_id = %s LIMIT 1",
                                        (user["id"],))).fetchone()
    if not has_loc:
        await engine.record_location(conn, ctx, user_id=user["id"], session_id=None, lat=s.demo_lat, lon=s.demo_lon,
                                     accuracy_m=50.0, source="simulated", simulated=True)
    return await (await conn.execute("SELECT * FROM users WHERE id = %s", (user["id"],))).fetchone()


async def _demo_user(conn, ctx) -> dict:
    user = await (await conn.execute("SELECT * FROM users WHERE is_demo ORDER BY created_at LIMIT 1")).fetchone()
    return user if user is not None else await ensure_demo_world(conn, ctx)


async def _demo_session(conn, user) -> uuid.UUID:
    sid = await engine.active_session_id(conn, user["id"])
    if sid is None:
        raise HTTPException(409, "no armed demo session; POST /v1/demo/reset first")
    return sid


async def _masked_alert_id(conn, sid) -> Optional[int]:
    """The session's open masked alert. Operator entries are tied to it so they stay hidden
    from the wearer's timeline until it closes, like every other entry it covers."""
    row = await (await conn.execute(
        "SELECT id FROM alerts WHERE session_id = %s AND masked AND status IN ('OPEN', 'ACKED')", (sid,))).fetchone()
    return row["id"] if row is not None else None


@router.get("/v1/admin/timers")
async def admin_timers(request: Request, recent: int = Query(default=0, ge=0, le=200)):
    """Pending timers; with ?recent=N also the N most recently fired ones and how late each fired."""
    require_admin(request)
    async with get_db(request).connection() as conn:
        rows = await (await conn.execute(
            """SELECT id, session_id, kind, fire_at, expected_version,
                      extract(epoch FROM fire_at - now()) AS in_s
                 FROM timers WHERE fired_at IS NULL AND cancelled_at IS NULL ORDER BY fire_at""")).fetchall()
        fired = []
        if recent:
            fired = await (await conn.execute(
                """SELECT id, session_id, kind, fire_at, fired_at, result, attempts,
                          extract(epoch FROM fired_at - fire_at) AS lag_s
                     FROM timers WHERE fired_at IS NOT NULL ORDER BY fired_at DESC, id DESC LIMIT %s""",
                (recent,))).fetchall()
    out = {"timers": [{"id": r["id"], "session_id": str(r["session_id"]), "kind": r["kind"],
                       "fire_at": r["fire_at"].isoformat(), "in_s": round(float(r["in_s"]), 1),
                       "expected_version": r["expected_version"]} for r in rows]}
    if recent:
        out["recent_fired"] = [{"id": r["id"], "session_id": str(r["session_id"]), "kind": r["kind"],
                                "fire_at": r["fire_at"].isoformat(), "fired_at": r["fired_at"].isoformat(),
                                "lag_s": round(float(r["lag_s"]), 3), "result": r["result"],
                                "attempts": r["attempts"]} for r in fired]
    return out


@router.get("/v1/admin/outbox")
async def admin_outbox(request: Request, session_id: Optional[uuid.UUID] = None, contact_id: Optional[int] = None,
                       recipient: Optional[Literal["wearer", "contact", "chat"]] = None,
                       limit: int = Query(default=100, ge=1, le=1000)):
    """Outbox rows (operator view), newest last. Filters: session_id, contact_id, recipient."""
    require_admin(request)
    q = """SELECT id, session_id, alert_id, contact_id, recipient, template, text, status, channel, attempts,
                  dedupe_key, created_at, sent_at
             FROM outbox WHERE true"""
    args: list = []
    if session_id is not None:
        q += " AND session_id = %s"
        args.append(session_id)
    if contact_id is not None:
        q += " AND contact_id = %s"
        args.append(contact_id)
    if recipient is not None:
        q += " AND recipient = %s"
        args.append(recipient)
    q += " ORDER BY id DESC LIMIT %s"
    args.append(limit)
    async with get_db(request).connection() as conn:
        rows = await (await conn.execute(q, args)).fetchall()
    return {"outbox": [{**r, "session_id": str(r["session_id"]) if r["session_id"] else None,
                        "created_at": r["created_at"].isoformat(),
                        "sent_at": r["sent_at"].isoformat() if r["sent_at"] else None}
                       for r in reversed(rows)]}


@router.get("/v1/admin/overview")
async def admin_overview(request: Request):
    require_admin(request)
    async with get_db(request).connection() as conn:
        sessions = await (await conn.execute(
            """SELECT s.id, s.state, s.public_state, s.link_state, s.version, s.profile, s.challenge_n,
                      s.prompt_cause, u.display_name
                 FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.state <> 'ENDED' ORDER BY s.created_at""")).fetchall()
        alerts = await (await conn.execute(
            """SELECT id, session_id, kind, level, is_final, masked, status, acked_by, renotify_count
                 FROM alerts WHERE status IN ('OPEN', 'ACKED') ORDER BY id""")).fetchall()
        outbox = await (await conn.execute(
            """SELECT id, recipient, contact_id, template, status, channel, created_at
                 FROM outbox ORDER BY id DESC LIMIT 30""")).fetchall()
    return {
        "sessions": [{**{k: v for k, v in r.items() if k != "id"}, "id": str(r["id"])} for r in sessions],
        "alerts": [{**{k: v for k, v in r.items() if k != "session_id"}, "session_id": str(r["session_id"]),
                    "final_banner": states.FINAL_BANNER if r["is_final"] else None} for r in alerts],
        "outbox": [{**r, "created_at": r["created_at"].isoformat()} for r in outbox],
    }


@router.post("/v1/demo/checkin-now")
async def checkin_now(request: Request):
    require_admin(request)
    ctx = get_ctx(request)
    async with get_db(request).connection() as conn:
        async with conn.transaction():
            user = await _demo_user(conn, ctx)
            sid = await _demo_session(conn, user)
            applied = await engine.apply(conn, sid, states.Event(states.CHECKIN_DUE), actor="demo",
                                         event_key=f"demo:{uuid.uuid4()}", ctx=ctx)
    return {"ok": True, "result": applied.decision.reply, "state": applied.decision.to_state}


@router.post("/v1/demo/inject")
async def inject(request: Request, body: InjectIn):
    require_admin(request)
    ctx = get_ctx(request)
    pf = {"head_position": rules.head_position_rule, "fall_like": rules.fall_like_rule,
          "offbody": rules.offbody_rule}[body.kind]()
    async with get_db(request).connection() as conn:
        async with conn.transaction():
            user = await _demo_user(conn, ctx)
            sid = await _demo_session(conn, user)
            applied = await engine.maybe_pull_forward(conn, sid, pf, actor="demo", event_key=f"demo:{uuid.uuid4()}",
                                                      ctx=ctx, bypass_cooldown=True)
    return {"ok": True, "reason": pf.reason, "label": rules.REASON_LABELS[pf.reason],
            "result": applied.decision.reply if applied else "cooldown"}


@router.post("/v1/demo/hr-source")
async def hr_source(request: Request, body: HrSourceIn):
    require_admin(request)
    async with get_db(request).connection() as conn:
        value = await sensors.set_hr_source(conn, body.mode)
    return {"ok": True, "hr_source": value, "simulated": body.mode.startswith("sim:")}


@router.post("/v1/demo/profile")
async def set_profile(request: Request, body: ProfileIn):
    require_admin(request)
    ctx = get_ctx(request)
    async with get_db(request).connection() as conn:
        async with conn.transaction():
            user = await _demo_user(conn, ctx)
            sid = await _demo_session(conn, user)
            await conn.execute("UPDATE sessions SET profile = %s WHERE id = %s", (body.profile, sid))
            masked_alert = await _masked_alert_id(conn, sid)
            await audit.append(conn, chain_key=audit.session_chain(sid), actor="demo", kind="profile_set",
                               user_id=user["id"], session_id=sid, detail={"profile": body.profile},
                               alert_id=masked_alert, masked=masked_alert is not None)
    return {"ok": True, "profile": body.profile}


@router.post("/v1/demo/fast-forward")
async def fast_forward(request: Request):
    require_admin(request)
    ctx = get_ctx(request)
    async with get_db(request).connection() as conn:
        async with conn.transaction():
            user = await _demo_user(conn, ctx)
            sid = await _demo_session(conn, user)
            row = await (await conn.execute(
                """UPDATE timers SET fire_at = now()
                    WHERE id = (SELECT id FROM timers
                                 WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL
                                   AND kind = ANY(%s)
                                 ORDER BY fire_at LIMIT 1)
                RETURNING kind""", (sid, list(states.STATE_TIMER_KINDS)))).fetchone()
            if row is None:
                return {"ok": False, "result": "no pending deadline"}
            masked_alert = await _masked_alert_id(conn, sid)
            await audit.append(conn, chain_key=audit.session_chain(sid), actor="demo", kind="demo_fast_forward",
                               user_id=user["id"], session_id=sid, detail={"timer": row["kind"]},
                               alert_id=masked_alert, masked=masked_alert is not None)
    return {"ok": True, "fired_next": row["kind"], "label": "fast-forward (demo)"}


@router.post("/v1/demo/judge-invite")
async def judge_invite(request: Request):
    require_admin(request)
    ctx = get_ctx(request)
    async with get_db(request).connection() as conn:
        async with conn.transaction():
            user = await _demo_user(conn, ctx)
            return await create_invite(conn, ctx, user["id"], name="", actor="demo")


@router.post("/v1/demo/reset")
async def reset(request: Request, body: Optional[ResetIn] = None):
    """Close the demo wearer's session (alerts marked RESET by the operator, not resolved) and re-arm."""
    require_admin(request)
    body = body or ResetIn()
    ctx = get_ctx(request)
    async with get_db(request).connection() as conn:
        async with conn.transaction():
            user = await ensure_demo_world(conn, ctx)
            sids = [r["id"] for r in await (await conn.execute(
                "SELECT id FROM sessions WHERE user_id = %s AND state <> 'ENDED' FOR UPDATE", (user["id"],))).fetchall()]
            for sid in sids:
                await conn.execute(
                    """UPDATE location_shares SET expires_at = now()
                        WHERE alert_id IN (SELECT id FROM alerts WHERE session_id = %s)
                          AND (expires_at IS NULL OR expires_at > now())""", (sid,))
                await conn.execute(
                    """UPDATE alerts SET status = 'RESET', closed_at = now(), resolved_by = 'demo_reset'
                        WHERE session_id = %s AND status IN ('OPEN', 'ACKED')""", (sid,))
                await conn.execute(
                    """UPDATE timers SET cancelled_at = now()
                        WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL""", (sid,))
                await conn.execute(
                    """UPDATE sessions SET state = 'ENDED', public_state = 'ENDED', version = version + 1,
                              ended_at = now(), updated_at = now() WHERE id = %s""", (sid,))
                await audit.append(conn, chain_key=audit.session_chain(sid), actor="demo", kind="demo_reset",
                                   user_id=user["id"], session_id=sid, to_state=states.ENDED, detail={})
            await conn.execute(
                """UPDATE outbox SET status = 'failed', last_error = 'demo reset'
                    WHERE user_id = %s AND status IN ('pending', 'sending')""", (user["id"],))
            await conn.execute("UPDATE devices SET lease_expires_at = NULL WHERE user_id = %s", (user["id"],))
            await engine.record_location(conn, ctx, user_id=user["id"], session_id=None, lat=ctx.settings.demo_lat,
                                         lon=ctx.settings.demo_lon, accuracy_m=50.0, source="simulated",
                                         simulated=True)
            await sensors.set_hr_source(conn, body.hr_source)
            armed = None
            if body.arm:
                armed = await arm_session(conn, ctx, user, ArmIn(mode=body.mode, profile="demo", preset="demo"),
                                          actor="demo")
    return {"ok": True, "closed_sessions": len(sids), "armed": armed}
