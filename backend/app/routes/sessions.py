"""Wearer endpoints (the /me page). Bearer wearer key.

Real-PIN and duress-PIN replies are byte-identical (same JSON, same status);
the wearer's own views (status, timeline) only show the public state, and
duress entries stay hidden until the alert is resolved.
"""
from __future__ import annotations

import math
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from .. import audit, engine, messages, states, timeseries
from ..config import PRESETS
from ..deps import canonical_json, get_ctx, get_db, require_wearer
from ..security import classify_pin, new_token, sha256_hex

router = APIRouter(tags=["wearer"])


class ArmIn(BaseModel):
    preset: Optional[Literal["night_out", "walking_home", "demo"]] = None
    mode: Literal["wrist", "hair"] = "wrist"
    profile: Optional[Literal["demo", "real"]] = None
    interval_s: Optional[int] = Field(default=None, ge=10, le=24 * 3600)
    duration_s: Optional[int] = Field(default=None, ge=30, le=24 * 3600)
    window_multiplier: float = Field(default=1.0, ge=1.0, le=10.0)
    device_id: Optional[str] = None


class CheckinIn(BaseModel):
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    accuracy_m: Optional[float] = None

    @field_validator("accuracy_m", mode="before")
    @classmethod
    def _plausible_accuracy(cls, v):
        # An unusable accuracy is dropped; it must never cost her the "I'm OK".
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) and 0 <= f <= 1_000_000 else None


class PinIn(BaseModel):
    pin: str = Field(min_length=1, max_length=32)


async def who_can_be_alerted(conn, user_id) -> list[dict]:
    return [{"n": i + 1, "name": c["name"], "contact_id": c["id"]}
            for i, c in enumerate(await engine.approved_contacts(conn, user_id))]


async def arm_session(conn, ctx, user: dict, body: ArmIn, actor: str = "wearer") -> dict:
    profile = body.profile or ctx.settings.default_profile
    t = ctx.settings.timings(profile)
    interval = body.interval_s or int(t.interval_s)
    duration = body.duration_s
    if duration is None and body.preset:
        duration = PRESETS[body.preset]["duration_s"]
    device_id = body.device_id
    if device_id is None:
        row = await (await conn.execute(
            "SELECT id FROM devices WHERE user_id = %s ORDER BY created_at LIMIT 1", (user["id"],))).fetchone()
        device_id = row["id"] if row else None
    existing = await engine.active_session_id(conn, user["id"])
    if existing is not None:
        prev = await (await conn.execute("SELECT public_state FROM sessions WHERE id = %s",
                                         (existing,))).fetchone()
        # A session "ended" with the duress PIN only looks ended: its masked alert keeps
        # escalating on its own row, and she gets a fresh session exactly as after a real end.
        if prev["public_state"] != states.ENDED:
            raise HTTPException(409, {"error": "already_armed", "session_id": str(existing)})
    sess = await (await conn.execute(
        """INSERT INTO sessions (user_id, device_id, profile, mode, preset, interval_s, window_multiplier, end_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s,
                   CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(secs => %s::int) END)
           RETURNING *""",
        (user["id"], device_id, profile, body.mode, body.preset, interval, body.window_multiplier,
         duration, duration))).fetchone()
    await conn.execute(
        """INSERT INTO timers (session_id, kind, fire_at, expected_version)
           VALUES (%s, 'CHECKIN_DUE', now() + make_interval(secs => %s), 0)""", (sess["id"], float(interval)))
    if duration:
        await conn.execute(
            """INSERT INTO timers (session_id, kind, fire_at) VALUES (%s, 'SESSION_END_DUE', %s)""",
            (sess["id"], sess["end_at"]))
    await audit.append(conn, chain_key=audit.session_chain(sess["id"]), actor=actor, kind="armed",
                       user_id=user["id"], session_id=sess["id"], to_state=states.ARMED,
                       detail={"profile": profile, "mode": body.mode, "preset": body.preset,
                               "interval_s": interval, "window_multiplier": body.window_multiplier})
    return {"session_id": str(sess["id"]), "state_public": sess["public_state"], "profile": profile,
            "interval_s": interval, "end_at": sess["end_at"].isoformat() if sess["end_at"] else None,
            "who_can_be_alerted": await who_can_be_alerted(conn, user["id"])}


@router.post("/v1/sessions")
async def arm(request: Request, body: Optional[ArmIn] = None):
    body = body or ArmIn()
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            return await arm_session(conn, ctx, user, body)


async def _own_session(conn, user: dict, session_id: str) -> dict:
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(404, "no such session")
    row = await (await conn.execute("SELECT * FROM sessions WHERE id = %s AND user_id = %s",
                                    (session_id, user["id"]))).fetchone()
    if row is None:
        raise HTTPException(404, "no such session")
    return row


@router.get("/v1/sessions/{session_id}")
async def session_status(request: Request, session_id: str):
    db = get_db(request)
    async with db.connection() as conn:
        user = await require_wearer(request, conn)
        sess = await _own_session(conn, user, session_id)
        view = engine.public_view(sess)
        # The link state she sees: a session that only looks ended keeps the one it ended with.
        return {"session_id": str(sess["id"]), "state_public": view["state_public"], "cmd": view["cmd"],
                "link_state": sess["public_link_state"], "profile": sess["profile"], "mode": sess["mode"],
                "who_can_be_alerted": await who_can_be_alerted(conn, user["id"])}


@router.post("/v1/sessions/{session_id}/checkin")
async def checkin(request: Request, session_id: str, body: Optional[CheckinIn] = None):
    """/me "I'm OK". Also refreshes the last-known location when the browser shares it."""
    body = body or CheckinIn()
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            sess = await _own_session(conn, user, session_id)
            if body.lat is not None and body.lon is not None:
                await engine.record_location(conn, ctx, user_id=user["id"], session_id=sess["id"], lat=body.lat,
                                             lon=body.lon, accuracy_m=body.accuracy_m, source="browser",
                                             simulated=False)
            applied = await engine.apply(conn, sess["id"], states.Event(states.WEARER_OK), actor="wearer",
                                         event_key=f"web:{uuid.uuid4()}", ctx=ctx)
    return canonical_json({"ok": True, "message": messages.NOTED, "state_public": applied.public_state})


async def _pin_action(request: Request, session_id: str, body: PinIn, end: bool):
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            sess = await _own_session(conn, user, session_id)
            kind = classify_pin(body.pin, user["pin_hash"], user["duress_pin_hash"])
            if kind is None:
                return canonical_json({"ok": False, "error": "wrong_pin"}, status_code=403)
            if end:
                ev = states.END_DURESS if kind == "duress" else states.END_REAL
            else:
                ev = states.PIN_DURESS if kind == "duress" else states.PIN_REAL
            applied = await engine.apply(conn, sess["id"], states.Event(ev), actor="wearer",
                                         event_key=f"web:{uuid.uuid4()}", ctx=ctx)
    message = messages.ENDED if end else messages.CANCELLED
    return canonical_json({"ok": True, "message": message, "state_public": applied.public_state})


@router.post("/v1/sessions/{session_id}/cancel")
async def cancel(request: Request, session_id: str, body: PinIn):
    return await _pin_action(request, session_id, body, end=False)


@router.post("/v1/sessions/{session_id}/end")
async def end(request: Request, session_id: str, body: PinIn):
    return await _pin_action(request, session_id, body, end=True)


def _entry(r: dict) -> dict:
    return {"at": r["at"].isoformat(), "actor": r["actor"], "kind": r["kind"], "from_state": r["from_state"],
            "to_state": r["to_state"], "detail": r["detail"]}


async def visible_timeline(conn, session_id) -> list[dict]:
    return [_entry(r) for r in await audit.visible_rows(conn, session_id)]


@router.get("/v1/sessions/{session_id}/timeline")
async def timeline(request: Request, session_id: str):
    db = get_db(request)
    async with db.connection() as conn:
        user = await require_wearer(request, conn)
        sess = await _own_session(conn, user, session_id)
        return {"session_id": str(sess["id"]), "entries": await visible_timeline(conn, sess["id"])}


@router.get("/v1/me/export")
async def export(request: Request):
    """Personal timeline export with chain verification (server-side recompute)."""
    db = get_db(request)
    async with db.connection() as conn:
        user = await require_wearer(request, conn)
        sessions = await (await conn.execute(
            "SELECT id, created_at, ended_at FROM sessions WHERE user_id = %s ORDER BY created_at",
            (user["id"],))).fetchall()
        # After "Delete my data" a chain may start after erased rows (entries kept for an open alert).
        erased = await (await conn.execute(
            "SELECT 1 FROM audit_log WHERE chain_key = %s AND kind = 'timeline_erased' LIMIT 1",
            (audit.user_chain(user["id"]),))).fetchone() is not None
        out = []
        for s in sessions:
            chain = await audit.verify_chain(conn, audit.session_chain(s["id"]), after_erase=erased)
            rows = await audit.visible_rows(conn, s["id"])
            # head_hash covers the listed entries (each with its masked flag), so anyone can recompute it.
            out.append({"session_id": str(s["id"]), "created_at": s["created_at"].isoformat(),
                        "ended_at": s["ended_at"].isoformat() if s["ended_at"] else None,
                        "chain_ok": chain["ok"], "head_hash": audit.public_head(rows),
                        "entries": [{**_entry(r), "masked": r["masked"]} for r in rows]})
        user_chain = await audit.verify_chain(conn, audit.user_chain(user["id"]), after_erase=erased)
        return {"user": user["display_name"], "sessions": out,
                "account_chain_ok": user_chain["ok"], "account_head_hash": user_chain["head"],
                "note": "Hash-chained: any edit after export breaks the chain when it is recomputed."}


@router.get("/v1/me/alerts")
async def my_alerts(request: Request):
    """The wearer's record of who was alerted and who acknowledged, per alert (research 1.15). An open
    masked (duress) alert is left out until it closes, exactly like its timeline entries; in its place
    she sees the twin's alert (states.py, "the twin"): the alert she could already see, as the OK
    action left it, or the one the twin opened itself. So the record after a duress PIN or a duress
    squeeze reads exactly as after the real PIN or a plain squeeze."""
    db = get_db(request)
    async with db.connection() as conn:
        user = await require_wearer(request, conn)
        alerts = await (await conn.execute(
            """SELECT a.id, a.session_id, a.status, a.level, a.is_final, a.resolved_by, a.opened_at, a.closed_at,
                      a.kind
                 FROM alerts a JOIN sessions s ON s.id = a.session_id
                WHERE s.user_id = %s AND (a.cover OR NOT (a.masked AND a.status IN ('OPEN', 'ACKED')))
                ORDER BY a.opened_at DESC, a.id DESC""", (user["id"],))).fetchall()
        ids = [a["id"] for a in alerts]
        told = await (await conn.execute(
            """SELECT r.alert_id, r.contact_id, c.name, r.first_notified_at FROM alert_recipients r
                 JOIN contacts c ON c.id = r.contact_id
                WHERE r.alert_id = ANY(%s) ORDER BY r.first_notified_at, r.contact_id""", (ids,))).fetchall()
        acks = await (await conn.execute(
            """SELECT k.alert_id, k.contact_id, c.name, k.action, k.source, k.result, k.at FROM acks k
                 JOIN contacts c ON c.id = k.contact_id
                WHERE k.alert_id = ANY(%s) ORDER BY k.at, k.id""", (ids,))).fetchall()
    out = []
    for a in alerts:
        out.append({
            "alert_id": a["id"], "session_id": str(a["session_id"]), "kind": a["kind"], "status": a["status"],
            "level": a["level"], "final": bool(a["is_final"]), "resolved_by": a["resolved_by"],
            "opened_at": a["opened_at"].isoformat(), "closed_at": a["closed_at"].isoformat() if a["closed_at"] else None,
            "alerted": [{"contact_id": r["contact_id"], "name": r["name"],
                         "first_notified_at": r["first_notified_at"].isoformat()}
                        for r in told if r["alert_id"] == a["id"]],
            "acknowledged": [{"contact_id": k["contact_id"], "name": k["name"], "action": k["action"],
                              "source": k["source"], "result": k["result"], "at": k["at"].isoformat()}
                             for k in acks if k["alert_id"] == a["id"]],
        })
    return canonical_json({"alerts": out})


@router.delete("/v1/me/data")
async def erase(request: Request):
    """Delete my data: timeline, locations, raw heart rate and raw scrunchie telemetry. Leaves a tombstone."""
    db = get_db(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            await conn.execute("SET LOCAL stillhere.erase = 'on'")
            # Everything she can see goes. While an alert is open, its hidden (duress) entries and
            # the location released to her contacts stay until it closes; erase again after that.
            n_audit = (await conn.execute(
                f"DELETE FROM audit_log a WHERE a.user_id = %s AND {audit.VISIBLE_SQL}", (user["id"],))).rowcount
            await conn.execute(
                """DELETE FROM location_snapshots s WHERE s.user_id = %s AND NOT EXISTS (
                     SELECT 1 FROM location_shares ls JOIN alerts al ON al.id = ls.alert_id
                      WHERE ls.snapshot_id = s.id AND al.status IN ('OPEN', 'ACKED'))""", (user["id"],))
            n_hr = (await conn.execute("DELETE FROM hr_samples WHERE user_id = %s", (user["id"],))).rowcount
            n_tel = await timeseries.erase_user_raw(conn, user["id"])
            await conn.execute("SET LOCAL stillhere.erase = 'off'")
            await audit.append(conn, chain_key=audit.user_chain(user["id"]), actor="wearer", kind="timeline_erased",
                               user_id=user["id"], detail={"audit_rows": n_audit, "hr_rows": n_hr,
                                                           "telemetry_rows": n_tel})
    return {"ok": True, "erased": {"timeline_entries": n_audit, "hr_samples": n_hr, "telemetry_rows": n_tel}}


@router.post("/v1/me/telegram-link")
async def telegram_link(request: Request):
    """One-time /start link that binds the wearer's own chat for Reminder nudges."""
    db, ctx = get_db(request), get_ctx(request)
    token = new_token()
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            await conn.execute(
                """UPDATE users SET tg_link_token_hash = %s, tg_link_expires_at = now() + interval '10 minutes'
                    WHERE id = %s""", (sha256_hex(token), user["id"]))
    bot = ctx.settings.telegram_bot_username
    return {"t_me_link": f"https://t.me/{bot}?start={token}" if bot else None, "start_token": token,
            "expires_in_s": 600}
