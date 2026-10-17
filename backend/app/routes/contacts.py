"""Trusted contacts.

Wearer side (bearer wearer key):
  POST   /v1/contacts               create a single-use, 10-minute invite -> {t_me_link, web_link, qr_png}
  GET    /v1/contacts               list, including pending joins to approve
  POST   /v1/contacts/{id}/approve  contacts cannot add themselves
  PUT    /v1/contacts/{id}/duress   {eligible}: choose who may receive a duress alert. With none
                                    chosen, every approved contact can (research 1.15)
  DELETE /v1/contacts/{id}          silent: the contact is not told
  DELETE /v1/contacts               remove every contact in one action, silently (research 1.15;
                                    Apple Safety Check's emergency reset). An open alert keeps
                                    escalating: with nobody left it reaches the simulated FINAL.
Contact side (capability token; cannot arm, see session status or ask for location):
  GET  /c/{token}                       consent / live page
  GET  /v1/contact/{token}/invite       consent screen: wearer name and contact number (410 if not valid)
  POST /v1/contact/{token}/join         pending until the wearer approves
  GET  /v1/contact/{token}/status       {"alert": null} before an alert (no session data)
  POST /v1/contact/{token}/ack
  POST /v1/contact/{token}/followup     {action: safe | cant_reach | pass}
  GET  /l/{share_token}                 last-known location; 410 Gone after expiry
"""
from __future__ import annotations

import base64
import io
import secrets
import uuid
from pathlib import Path
from typing import Literal, Optional

import qrcode
import qrcode.image.pure
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import audit, engine, messages, states
from ..deps import get_ctx, get_db, require_wearer
from ..security import decrypt_coords, new_token, sha256_hex

router = APIRouter(tags=["contacts"])

INVITE_TTL_S = 600
PAGES = Path(__file__).resolve().parent.parent / "pages"


class ContactIn(BaseModel):
    name: str = Field(default="", max_length=60)
    priority: Optional[int] = Field(default=None, ge=1, le=1000)


class JoinIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=60)


class DuressIn(BaseModel):
    eligible: bool


class FollowupIn(BaseModel):
    action: Literal["safe", "cant_reach", "pass"]


def qr_png_b64(data: str) -> str:
    img = qrcode.make(data, image_factory=qrcode.image.pure.PyPNGImage)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


async def create_invite(conn, ctx, user_id, name: str = "", priority: Optional[int] = None,
                        actor: str = "wearer") -> dict:
    token = new_token()
    if priority is None:
        row = await (await conn.execute(
            "SELECT coalesce(max(priority), 0) + 1 AS p FROM contacts WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,))).fetchone()
        priority = row["p"]
    row = await (await conn.execute(
        """INSERT INTO contacts (user_id, name, priority, invite_token_hash, invite_expires_at, web_token_nonce)
           VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), %s) RETURNING id, invite_expires_at""",
        (user_id, name.strip(), priority, sha256_hex(token), INVITE_TTL_S, secrets.token_urlsafe(8)))).fetchone()
    await audit.append(conn, chain_key=audit.user_chain(user_id), actor=actor, kind="contact_invited",
                       user_id=user_id, detail={"contact_id": row["id"], "priority": priority})
    base = ctx.settings.public_base_url
    bot = ctx.settings.telegram_bot_username
    web_link = f"{base}/c/{token}"
    return {"contact_id": row["id"], "priority": priority, "web_link": web_link,
            "t_me_link": f"https://t.me/{bot}?start={token}" if bot else None,
            "qr_png": qr_png_b64(web_link), "expires_at": row["invite_expires_at"].isoformat()}


@router.post("/v1/contacts")
async def invite(request: Request, body: Optional[ContactIn] = None):
    body = body or ContactIn()
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            return await create_invite(conn, ctx, user["id"], body.name, body.priority)


@router.get("/v1/contacts")
async def list_contacts(request: Request):
    db = get_db(request)
    async with db.connection() as conn:
        user = await require_wearer(request, conn)
        rows = await (await conn.execute(
            """SELECT id, name, priority, channel, joined_at, approved_at, duress_eligible FROM contacts
                WHERE user_id = %s AND revoked_at IS NULL ORDER BY priority, id""", (user["id"],))).fetchall()
    approved = [r for r in rows if r["approved_at"]]
    order = {r["id"]: i + 1 for i, r in enumerate(sorted(approved, key=lambda r: (r["priority"], r["approved_at"], r["id"])))}
    chosen = [r["id"] for r in sorted(approved, key=lambda r: order[r["id"]]) if r["duress_eligible"]]
    return {"contacts": [{
        "contact_id": r["id"], "name": r["name"], "priority": r["priority"], "channel": r["channel"],
        "status": "approved" if r["approved_at"] else ("pending_approval" if r["joined_at"] else "invited"),
        "n": order.get(r["id"]), "duress_eligible": bool(r["duress_eligible"]),
    } for r in rows],
        "duress_recipients": ({"mode": "chosen", "contact_ids": chosen} if chosen else
                              {"mode": "all approved contacts (none chosen)",
                               "contact_ids": [r["id"] for r in sorted(approved, key=lambda r: order[r["id"]])]})}


@router.post("/v1/contacts/{contact_id}/approve")
async def approve(request: Request, contact_id: int):
    db = get_db(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            row = await (await conn.execute(
                """UPDATE contacts SET approved_at = coalesce(approved_at, now())
                    WHERE id = %s AND user_id = %s AND joined_at IS NOT NULL AND revoked_at IS NULL
                RETURNING id, name, channel""", (contact_id, user["id"]))).fetchone()
            if row is None:
                raise HTTPException(404, "no joined contact with that id")
            approved = await engine.approved_contacts(conn, user["id"])
            n = next(i + 1 for i, c in enumerate(approved) if c["id"] == contact_id)
            await audit.append(conn, chain_key=audit.user_chain(user["id"]), actor="wearer", kind="contact_approved",
                               user_id=user["id"], detail={"contact_id": contact_id, "n": n})
            await conn.execute(
                """INSERT INTO outbox (dedupe_key, user_id, contact_id, recipient, purpose, template, text)
                   VALUES (%s, %s, %s, 'contact', 'contact_approved', 'contact_approved', %s)
                   ON CONFLICT (dedupe_key) DO NOTHING""",
                (f"approve:{contact_id}", user["id"], contact_id,
                 messages.render("contact_approved", {"name": user["display_name"], "n": n})))
    return {"ok": True, "contact_id": contact_id, "n": n, "name": row["name"]}


@router.put("/v1/contacts/{contact_id}/duress")
async def set_duress_recipient(request: Request, contact_id: int, body: DuressIn):
    """Wearer only. A contact kept out of duress alerts is never told about one."""
    db = get_db(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            row = await (await conn.execute(
                """UPDATE contacts SET duress_eligible = %s
                    WHERE id = %s AND user_id = %s AND revoked_at IS NULL RETURNING id""",
                (body.eligible, contact_id, user["id"]))).fetchone()
            if row is None:
                raise HTTPException(404, "no such contact")
            await audit.append(conn, chain_key=audit.user_chain(user["id"]), actor="wearer",
                               kind="contact_duress_recipient", user_id=user["id"],
                               detail={"contact_id": contact_id, "eligible": body.eligible})
    return {"ok": True, "contact_id": contact_id, "duress_eligible": body.eligible}


@router.delete("/v1/contacts")
async def revoke_all(request: Request):
    db = get_db(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            rows = await (await conn.execute(
                """UPDATE contacts SET revoked_at = now(), web_token_hash = NULL
                    WHERE user_id = %s AND revoked_at IS NULL RETURNING id""", (user["id"],))).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                # Silent: nothing is sent to anyone, and nothing still queued for them goes out.
                await conn.execute(
                    """UPDATE outbox SET status = 'failed', last_error = 'contact removed'
                        WHERE contact_id = ANY(%s) AND status = 'pending'""", (ids,))
            await audit.append(conn, chain_key=audit.user_chain(user["id"]), actor="wearer",
                               kind="contacts_removed_all", user_id=user["id"], detail={"removed": len(ids)})
    return {"ok": True, "removed": len(ids)}


@router.delete("/v1/contacts/{contact_id}")
async def revoke(request: Request, contact_id: int):
    db = get_db(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            cur = await conn.execute(
                """UPDATE contacts SET revoked_at = now(), web_token_hash = NULL
                    WHERE id = %s AND user_id = %s AND revoked_at IS NULL""", (contact_id, user["id"]))
            if cur.rowcount == 0:
                raise HTTPException(404, "no such contact")
            # Silent: nothing is sent to the contact.
            await conn.execute(
                """UPDATE outbox SET status = 'failed', last_error = 'contact removed'
                    WHERE contact_id = %s AND status = 'pending'""", (contact_id,))
            await audit.append(conn, chain_key=audit.user_chain(user["id"]), actor="wearer", kind="contact_removed",
                               user_id=user["id"], detail={"contact_id": contact_id})
    return {"ok": True}


# --- contact side -------------------------------------------------------------
async def _contact_by_token(conn, token: str) -> Optional[dict]:
    return await (await conn.execute(
        """SELECT c.*, u.display_name AS wearer FROM contacts c JOIN users u ON u.id = c.user_id
            WHERE c.web_token_hash = %s AND c.revoked_at IS NULL""", (sha256_hex(token),))).fetchone()


async def _invite_by_token(conn, token: str, lock: bool = False) -> Optional[dict]:
    return await (await conn.execute(
        """SELECT c.*, u.display_name AS wearer FROM contacts c JOIN users u ON u.id = c.user_id
            WHERE c.invite_token_hash = %s AND c.invite_used_at IS NULL AND c.revoked_at IS NULL
              AND c.invite_expires_at > now()""" + (" FOR UPDATE OF c" if lock else ""),
        (sha256_hex(token),))).fetchone()


@router.get("/c/{token}", response_class=HTMLResponse)
async def contact_page(request: Request, token: str):
    page = PAGES / "c.html"
    return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store",
                                                                   "Referrer-Policy": "no-referrer"})


@router.get("/v1/contact/{token}/invite")
async def invite_info(request: Request, token: str):
    """What the consent screen needs: whose invite it is and which contact number joining would ask for.

    Nothing about the wearer's session. Unknown, used and expired invites all answer 410.
    """
    db = get_db(request)
    async with db.connection() as conn:
        inv = await _invite_by_token(conn, token)
        if inv is None:
            return JSONResponse({"valid": False}, status_code=410, headers={"Cache-Control": "no-store"})
        row = await (await conn.execute(
            """SELECT count(*) AS n, extract(epoch FROM %s::timestamptz - now()) AS left_s FROM contacts
                WHERE user_id = %s AND approved_at IS NOT NULL AND revoked_at IS NULL""",
            (inv["invite_expires_at"], inv["user_id"]))).fetchone()
    return JSONResponse({"valid": True, "wearer": inv["wearer"], "n": int(row["n"]) + 1,
                         "expires_in_s": max(0, int(row["left_s"]))}, headers={"Cache-Control": "no-store"})


@router.post("/v1/contact/{token}/join")
async def join(request: Request, token: str, body: Optional[JoinIn] = None):
    body = body or JoinIn()
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        async with conn.transaction():
            inv = await _invite_by_token(conn, token, lock=True)
            if inv is None:
                raise HTTPException(410, "invite expired or already used")
            web_token = engine.contact_web_token(ctx, inv["id"], inv["web_token_nonce"])
            name = (body.name or "").strip()[:60]
            await conn.execute(
                """UPDATE contacts SET joined_at = now(), invite_used_at = now(), channel = 'web',
                          web_token_hash = %s, name = CASE WHEN name = '' THEN %s ELSE name END
                    WHERE id = %s""", (sha256_hex(web_token), name or "Contact", inv["id"]))
            await audit.append(conn, chain_key=audit.user_chain(inv["user_id"]), actor=f"contact:{inv['id']}",
                               kind="contact_joined", user_id=inv["user_id"],
                               detail={"contact_id": inv["id"], "channel": "web"})
    return {"ok": True, "status": "pending_approval", "contact_token": web_token,
            "message": messages.render("contact_pending", {"name": inv["wearer"]})}


async def _alert_for_contact(conn, contact: dict) -> Optional[dict]:
    """The open alert this contact has been told about, if any. A duress alert does not exist for a
    contact the wearer keeps out of duress alerts, even one told about it before it turned into
    duress: they see the twin's alert instead (states.py, "the twin"), the one they would see had she
    taken the OK action, and their buttons act on it. Duress contacts never see the twin's alert."""
    if contact["approved_at"] is None:
        return None
    rows = await (await conn.execute(
        """SELECT a.*, s.state, s.user_id FROM alerts a
             JOIN sessions s ON s.id = a.session_id
             JOIN alert_recipients r ON r.alert_id = a.id AND r.contact_id = %s
            WHERE s.user_id = %s AND a.status IN ('OPEN', 'ACKED')
            ORDER BY a.id DESC""", (contact["id"], contact["user_id"]))).fetchall()
    if not rows:
        return None
    on_duress_ladder = contact["id"] in await engine.duress_ladder_ids(conn, contact["user_id"])
    for alert in rows:
        if engine.contact_sees(alert, on_duress_ladder):
            return alert
    return None


@router.get("/v1/contact/{token}/status")
async def contact_status(request: Request, token: str):
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        contact = await _contact_by_token(conn, token)
        if contact is None:
            raise HTTPException(404, "unknown contact link")
        alert = await _alert_for_contact(conn, contact)
        if alert is None:
            # Nothing about the wearer's session before an alert.
            return JSONResponse({"alert": None, "approved": contact["approved_at"] is not None},
                                headers={"Cache-Control": "no-store"})
        now = await engine.db_now(conn)
        loc = await engine.share_for_alert(conn, ctx, alert["id"], now)
        handler = None
        if alert["acked_by"]:
            r = await (await conn.execute("SELECT name FROM contacts WHERE id = %s", (alert["acked_by"],))).fetchone()
            handler = r["name"] if r else None
        you_acked = alert["acked_by"] == contact["id"]
        if alert["kind"] == states.LOST_CONTACT:
            actions: list[str] = []
        elif alert["status"] == "ACKED":
            actions = ["safe", "cant_reach"] if you_acked else []
        else:
            # Only the contact at the current level can pass it on (states._on_contact_pass);
            # earlier contacts who are re-pinged can still take it on.
            ladder = ([c for c in await engine.duress_ladder_ids(conn, contact["user_id"])]
                      if alert["kind"] == states.DURESS else
                      [c["id"] for c in await engine.approved_contacts(conn, contact["user_id"])])
            current = (not alert["is_final"] and 0 < alert["level"] <= len(ladder)
                       and ladder[alert["level"] - 1] == contact["id"])
            actions = ["ack"] + (["pass"] if current else [])
        body = {
            "alert": {
                "id": alert["id"],
                "kind": alert["kind"],
                "title": messages.CONTACT_PAGE_TITLES[alert["kind"]].format(name=contact["wearer"]),
                "wearer": contact["wearer"],
                "level": alert["level"],
                "final": bool(alert["is_final"]),
                "banner": states.FINAL_BANNER if alert["is_final"] else None,
                "status": alert["status"],
                "handled_by": handler,
                "you_acked": you_acked,
                "location": loc,
                "actions": actions,
                "opened_at": alert["opened_at"].isoformat(),
            },
            "approved": True,
        }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


async def _contact_event(request: Request, token: str, event_kind: str, action: str) -> dict:
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        async with conn.transaction():
            contact = await _contact_by_token(conn, token)
            if contact is None or contact["approved_at"] is None:
                raise HTTPException(404, "unknown or unapproved contact link")
            alert = await _alert_for_contact(conn, contact)
            if alert is None:
                raise HTTPException(409, "no active alert")
            # Session lock before the acks row (same order as the timer worker; see telegram.py).
            await conn.execute("SELECT 1 FROM sessions WHERE id = %s FOR UPDATE", (alert["session_id"],))
            ack = await (await conn.execute(
                """INSERT INTO acks (alert_id, contact_id, action, source) VALUES (%s, %s, %s, 'web')
                   RETURNING id""", (alert["id"], contact["id"], action))).fetchone()
            ev = states.Event(event_kind, contact_id=contact["id"], twin=bool(alert["cover"]))
            applied = await engine.apply(conn, alert["session_id"], ev, actor=f"contact:{contact['id']}",
                                         event_key=f"web:{uuid.uuid4()}", ctx=ctx)
            await conn.execute("UPDATE acks SET result = %s WHERE id = %s", (applied.decision.reply, ack["id"]))
    return {"ok": applied.decision.reply not in ("ignored",), "result": applied.decision.reply}


@router.post("/v1/contact/{token}/ack")
async def contact_ack(request: Request, token: str):
    return await _contact_event(request, token, states.CONTACT_ACK, "ack")


@router.post("/v1/contact/{token}/followup")
async def contact_followup(request: Request, token: str, body: FollowupIn):
    kind = {"safe": states.CONTACT_SAFE, "cant_reach": states.CONTACT_CANT_REACH,
            "pass": states.CONTACT_PASS}[body.action]
    return await _contact_event(request, token, kind, body.action)


@router.get("/l/{share_token}")
async def location_share(request: Request, share_token: str):
    db, ctx = get_db(request), get_ctx(request)
    async with db.connection() as conn:
        row = await (await conn.execute(
            """SELECT ls.expires_at, ls.alert_id, s.coords_enc, s.captured_at, s.simulated, s.accuracy_m,
                      now() AS now, (ls.expires_at IS NOT NULL AND ls.expires_at <= now()) AS expired
                 FROM location_shares ls JOIN location_snapshots s ON s.id = ls.snapshot_id
                WHERE ls.token_hash = %s""", (sha256_hex(share_token),))).fetchone()
    if row is None:
        raise HTTPException(404, "unknown link")
    if row["expired"]:
        return JSONResponse({"error": "gone", "detail": "This location link has expired."}, status_code=410,
                            headers={"Cache-Control": "no-store"})
    try:
        coords = decrypt_coords(ctx.secret, row["coords_enc"])
    except InvalidTag:  # sealed under an earlier APP_SECRET: it cannot be read any more
        return JSONResponse({"error": "gone", "detail": "This location is no longer available."}, status_code=410,
                            headers={"Cache-Control": "no-store"})
    age = max(0, int((row["now"] - row["captured_at"]).total_seconds() // 60))
    lat, lon = coords["lat"], coords["lon"]
    body = {
        "lat": lat, "lon": lon, "accuracy_m": row["accuracy_m"], "simulated": bool(row["simulated"]),
        "as_of": engine.hm(row["captured_at"], ctx.settings.display_tz), "age_min": age,
        "map": f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=17/{lat:.5f}/{lon:.5f}",
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
