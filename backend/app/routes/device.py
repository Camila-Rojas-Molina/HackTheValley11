"""Device endpoints (BUILD_PLAN §5.8): static per-device bearer key.

POST /v1/device/heartbeat  headers: Authorization: Bearer <key>, X-Device-Id, X-Boot-Id, X-Seq
POST /v1/device/events     body: {event_id, type, payload, device_ms}; event_id = boot_id:seq.
                           A replayed event_id returns the cached reply byte for byte.
Replies only ever carry the public state; a duress alert reads ARMED.
"""
from __future__ import annotations

import json
import math
from typing import Literal, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, field_validator

from .. import engine, rules, states
from ..deps import canonical_json, get_ctx, get_db, require_device

router = APIRouter(tags=["device"])

OFF_CMD = {"led": "off", "blink_n": 0, "challenge_id": None}


class HeartbeatIn(BaseModel):
    wear_mode: Optional[Literal["wrist", "hair"]] = None
    enmo60_mg: Optional[float] = Field(default=None, ge=0, le=20000)
    offbody: Optional[bool] = None
    feats: Optional[list] = None


def _finite(value) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_finite(v) for v in value)
    return True


class EventIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=80)
    type: Literal["squeeze", "head_position_change", "offbody", "prompt_shown", "fall_like"]
    payload: dict = Field(default_factory=dict)
    device_ms: Optional[int] = Field(default=None, ge=0, le=2 ** 53 - 1)

    @field_validator("payload")
    @classmethod
    def _payload_is_storable(cls, v: dict) -> dict:
        # 1e999 parses as infinity, which JSON (and jsonb) cannot hold: reject it here (422).
        if not _finite(v):
            raise ValueError("payload numbers must be finite")
        return v


def _int_header(request: Request, name: str) -> Optional[int]:
    v = request.headers.get(name)
    try:
        return int(v) if v is not None else None
    except ValueError:
        return None


@router.post("/v1/device/heartbeat")
async def heartbeat(request: Request, body: Optional[HeartbeatIn] = None):
    body = body or HeartbeatIn()
    db, ctx = get_db(request), get_ctx(request)
    boot_id = request.headers.get("x-boot-id") or ""
    seq = _int_header(request, "x-seq")
    async with db.connection() as conn:
        async with conn.transaction():
            dev = await require_device(request, conn)
            sid = await engine.active_session_id(conn, dev["user_id"])
            profile = None
            if sid is not None:
                profile = (await (await conn.execute("SELECT profile FROM sessions WHERE id = %s",
                                                     (sid,))).fetchone())["profile"]
            lease = ctx.settings.timings(profile).lease_s
            # Replay protection: a heartbeat with an old seq for the same boot does not extend the lease.
            fresh = (dev["boot_id"] != boot_id or dev["last_seq"] is None or seq is None
                     or seq > dev["last_seq"])
            await conn.execute(
                """UPDATE devices SET
                      boot_id = %(boot)s,
                      last_seq = CASE WHEN boot_id IS DISTINCT FROM %(boot)s THEN %(seq)s
                                      ELSE greatest(coalesce(last_seq, -1), coalesce(%(seq)s, -1)) END,
                      lease_expires_at = CASE WHEN %(fresh)s THEN now() + make_interval(secs => %(lease)s)
                                              ELSE lease_expires_at END,
                      last_heartbeat_at = CASE WHEN %(fresh)s THEN now() ELSE last_heartbeat_at END,
                      wear_mode = coalesce(%(wear)s, wear_mode),
                      last_enmo60_mg = coalesce(%(enmo)s, last_enmo60_mg),
                      last_vigorous_at = CASE WHEN %(enmo)s::real >= %(vig)s THEN now() ELSE last_vigorous_at END,
                      offbody = coalesce(%(off)s, offbody)
                    WHERE id = %(id)s""",
                {"boot": boot_id, "seq": seq, "fresh": fresh, "lease": float(lease), "wear": body.wear_mode,
                 "enmo": body.enmo60_mg, "vig": rules.VIGOROUS_ENMO_MG, "off": body.offbody, "id": dev["id"]})
            if sid is None:
                now = await engine.db_now(conn)
                return canonical_json({"state_public": await _idle_public_state(conn, dev["user_id"]),
                                       "server_time": now.isoformat(), "cmd": OFF_CMD})
            await conn.execute("UPDATE sessions SET device_id = %s WHERE id = %s AND device_id IS NULL",
                               (dev["id"], sid))
            if fresh:
                # Every running session of this wearer hears the link is back (a session that only
                # looks ended, with its masked alert open, runs next to a fresh one).
                lost = await (await conn.execute(
                    """SELECT id FROM sessions WHERE user_id = %s AND state <> 'ENDED' AND link_state = %s
                        ORDER BY created_at FOR UPDATE""", (dev["user_id"], states.LINK_LOST))).fetchall()
                for r in lost:
                    await engine.apply(conn, r["id"], states.Event(states.LINK_RESTORED), actor="device",
                                       event_key=f"restore:{dev['id']}:{boot_id}:{seq}:{r['id']}", ctx=ctx)
            sess = await (await conn.execute("SELECT * FROM sessions WHERE id = %s", (sid,))).fetchone()
            now = await engine.db_now(conn)
            view = engine.public_view(sess)
    return canonical_json({"state_public": view["state_public"], "server_time": now.isoformat(), "cmd": view["cmd"]})


@router.post("/v1/device/events")
async def device_event(request: Request, body: EventIn):
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        async with conn.transaction():
            dev = await require_device(request, conn)
            ins = await (await conn.execute(
                """INSERT INTO device_events (device_id, event_id, type, payload, device_ms)
                   VALUES (%s, %s, %s, %s::jsonb, %s) ON CONFLICT DO NOTHING RETURNING event_id""",
                (dev["id"], body.event_id, body.type, json.dumps(body.payload), body.device_ms))).fetchone()
            if ins is None:
                cached = await (await conn.execute(
                    "SELECT result FROM device_events WHERE device_id = %s AND event_id = %s",
                    (dev["id"], body.event_id))).fetchone()
                return canonical_json(cached["result"] or {"ok": True, "result": "duplicate"})
            result = await _process(conn, ctx, dev, body)
            await conn.execute(
                "UPDATE device_events SET result = %s::jsonb WHERE device_id = %s AND event_id = %s",
                (json.dumps(result), dev["id"], body.event_id))
    return canonical_json(result)


async def _idle_public_state(conn, user_id) -> str:
    """Public state when no session is running: ENDED after one, DISARMED before any."""
    row = await (await conn.execute(
        "SELECT public_state FROM sessions WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
        (user_id,))).fetchone()
    return states.ENDED if row is not None else "DISARMED"


def _no_session(public_state: str) -> dict:
    return {"ok": True, "result": "no_session", "state_public": public_state, "cmd": OFF_CMD}


async def _process(conn, ctx, dev: dict, body: EventIn) -> dict:
    sid = await engine.active_session_id(conn, dev["user_id"])
    if sid is None:
        return _no_session(await _idle_public_state(conn, dev["user_id"]))
    key = f"dev:{dev['id']}:{body.event_id}"
    actor = "device"
    p = body.payload or {}
    reply = "ignored"
    if body.type == "squeeze":
        sess = await (await conn.execute("SELECT link_state FROM sessions WHERE id = %s FOR UPDATE",
                                         (sid,))).fetchone()
        pattern = p.get("pattern") or "plain"
        if pattern not in states.SQUEEZE_PATTERNS:
            pattern = "unrecognized"
        try:
            k = int(p["k"]) if p.get("k") is not None else None
        except (TypeError, ValueError):
            k = None
        late = bool(p.get("buffered")) or sess["link_state"] == states.LINK_LOST
        applied = await engine.apply(conn, sid, states.Event(states.SQUEEZE, pattern=pattern, k=k, late=late),
                                     actor=actor, event_key=key, ctx=ctx)
        if applied.public_state == states.ENDED:
            # Masked session that "ended" with the duress PIN: answer like a real ended session.
            return _no_session(states.ENDED)
        return {"ok": True, "result": applied.decision.reply, "state_public": applied.public_state,
                "cmd": applied.cmd}
    if body.type in ("head_position_change", "fall_like", "offbody"):
        pf = {"head_position_change": rules.head_position_rule, "fall_like": rules.fall_like_rule,
              "offbody": rules.offbody_rule}[body.type](p)
        applied = await engine.maybe_pull_forward(conn, sid, pf, actor=actor, event_key=key, ctx=ctx)
        reply = applied.decision.reply if applied is not None else "cooldown"
    elif body.type == "prompt_shown":
        n = await engine.lower_prompt_deadline(conn, sid)
        reply = "deadline_set" if n else "ignored"
    sess = await (await conn.execute("SELECT * FROM sessions WHERE id = %s", (sid,))).fetchone()
    view = engine.public_view(sess)
    if view["state_public"] == states.ENDED:
        return _no_session(states.ENDED)
    return {"ok": True, "result": reply, "state_public": view["state_public"], "cmd": view["cmd"]}
