"""Applies state-machine decisions to the database.

`apply()` runs inside the caller's transaction and does, atomically: the
guarded session UPDATE, timer cancellation and insertion, alert changes,
location release/expiry, timeline entries and outbox rows. A crash before
COMMIT rolls all of it back; the stored deadline is fired again later.
"""
from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import audit, messages, states
from .config import Settings
from .rules import PullForward, cooldown_ok
from .security import derive_token, encrypt_coords, new_token, sha256_hex
from .states import Decision, Event, Snapshot

log = logging.getLogger("stillhere.engine")

LOCATION_TEMPLATES = {"alert_no_response", "alert_duress", "alert_final"}


class SessionNotFound(LookupError):
    pass


class StaleTransition(RuntimeError):
    pass


@dataclass
class Ctx:
    settings: Settings
    secret: str
    choose: Optional[states.Chooser] = None


@dataclass
class Applied:
    decision: Decision
    session_id: str
    version: int
    public_state: str
    cmd: dict
    alert_id: Optional[int]


def hm(ts: Optional[datetime], tz: str) -> str:
    if ts is None:
        return ""
    return ts.astimezone(ZoneInfo(tz)).strftime("%H:%M")


async def db_now(conn) -> datetime:
    return (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]


async def load_secret(conn, settings: Settings) -> str:
    """APP_SECRET from the environment, else a random one persisted in bot_state."""
    if settings.app_secret:
        return settings.app_secret
    await conn.execute(
        """INSERT INTO bot_state (key, value) VALUES ('app_secret', to_jsonb(%s::text))
           ON CONFLICT (key) DO NOTHING""", (secrets.token_urlsafe(32),))
    row = await (await conn.execute("SELECT value FROM bot_state WHERE key = 'app_secret'")).fetchone()
    return row["value"]


# --- loading ----------------------------------------------------------------
async def approved_contacts(conn, user_id) -> list[dict]:
    cur = await conn.execute(
        """SELECT id, name, priority, channel, telegram_chat_id, web_token_nonce
             FROM contacts
            WHERE user_id = %s AND approved_at IS NOT NULL AND revoked_at IS NULL
            ORDER BY priority, approved_at, id""", (user_id,))
    return list(await cur.fetchall())


async def _load(conn, session_id):
    sess = await (await conn.execute(
        """SELECT s.*, u.display_name AS wearer_name
             FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.id = %s FOR UPDATE OF s""", (session_id,))).fetchone()
    if sess is None:
        raise SessionNotFound(str(session_id))
    alert = await (await conn.execute(
        "SELECT * FROM alerts WHERE session_id = %s AND status IN ('OPEN', 'ACKED') FOR UPDATE",
        (session_id,))).fetchone()
    contacts = await approved_contacts(conn, sess["user_id"])
    recipients: list[int] = []
    if alert is not None:
        cur = await conn.execute(
            "SELECT contact_id FROM alert_recipients WHERE alert_id = %s ORDER BY first_notified_at, contact_id",
            (alert["id"],))
        recipients = [r["contact_id"] for r in await cur.fetchall()]
    return sess, alert, contacts, recipients


def build_snapshot(sess: dict, alert: Optional[dict], contacts: list[dict], recipients: list[int],
                   settings: Settings) -> Snapshot:
    view = None
    if alert is not None:
        view = states.AlertView(
            id=alert["id"], kind=alert["kind"], level=alert["level"], is_final=alert["is_final"],
            masked=alert["masked"], status=alert["status"], acked_by=alert["acked_by"],
            renotify_count=alert["renotify_count"], recipients=tuple(recipients))
    return Snapshot(
        state=sess["state"], public_state=sess["public_state"], version=sess["version"],
        timings=settings.timings(sess["profile"]), contacts=tuple(c["id"] for c in contacts),
        alert=view, challenge_n=sess["challenge_n"], prompt_cause=sess["prompt_cause"],
        retry_used=sess["retry_used"], link_state=sess["link_state"],
        lost_contact_told=sess["lost_contact_told"], window_multiplier=float(sess["window_multiplier"]))


# --- location -----------------------------------------------------------------
async def record_location(conn, ctx: Ctx, *, user_id, session_id, lat: float, lon: float,
                          accuracy_m: Optional[float], source: str, simulated: bool) -> int:
    row = await (await conn.execute(
        """INSERT INTO location_snapshots (user_id, session_id, coords_enc, accuracy_m, source, simulated)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (user_id, session_id, encrypt_coords(ctx.secret, lat, lon), accuracy_m, source, simulated))).fetchone()
    return row["id"]


def location_token(ctx: Ctx, alert_id: int, nonce: str) -> str:
    return derive_token(ctx.secret, "loc", str(alert_id), nonce)


def contact_web_token(ctx: Ctx, contact_id: int, nonce: str) -> str:
    return derive_token(ctx.secret, "contact", str(contact_id), nonce)


async def _release_location(conn, ctx: Ctx, user_id, alert_id: int) -> None:
    exists = await (await conn.execute(
        "SELECT 1 FROM location_shares WHERE alert_id = %s", (alert_id,))).fetchone()
    if exists:
        return
    snap = await (await conn.execute(
        "SELECT id FROM location_snapshots WHERE user_id = %s ORDER BY captured_at DESC LIMIT 1",
        (user_id,))).fetchone()
    if snap is None:
        return
    nonce = new_token()
    token = location_token(ctx, alert_id, nonce)
    await conn.execute(
        """INSERT INTO location_shares (token_hash, token_nonce, snapshot_id, alert_id)
           VALUES (%s, %s, %s, %s)""", (sha256_hex(token), nonce, snap["id"], alert_id))


async def share_for_alert(conn, ctx: Ctx, alert_id: Optional[int], now: datetime) -> Optional[dict]:
    if alert_id is None:
        return None
    row = await (await conn.execute(
        """SELECT ls.token_nonce, ls.expires_at, s.captured_at, s.simulated
             FROM location_shares ls JOIN location_snapshots s ON s.id = ls.snapshot_id
            WHERE ls.alert_id = %s ORDER BY ls.id DESC LIMIT 1""", (alert_id,))).fetchone()
    if row is None:
        return None
    token = location_token(ctx, alert_id, row["token_nonce"])
    age = max(0, int((now - row["captured_at"]).total_seconds() // 60))
    return {
        "as_of": hm(row["captured_at"], ctx.settings.display_tz),
        "age_min": age,
        "link": f"{ctx.settings.public_base_url}/l/{token}",
        "simulated": bool(row["simulated"]),
    }


# --- buttons ------------------------------------------------------------------
def _markup(kind: Optional[str], alert_id: Optional[int], contact: Optional[dict], ctx: Ctx) -> Optional[dict]:
    if kind is None or alert_id is None or contact is None:
        return None
    cid = contact["id"]
    if kind == "alert":
        rows = [
            [{"text": messages.BTN_ON_IT, "callback_data": f"ack:{alert_id}:{cid}"}],
            [{"text": messages.BTN_PASS, "callback_data": f"pass:{alert_id}:{cid}"}],
        ]
        base = ctx.settings.public_base_url
        if base.startswith("https://") and contact.get("web_token_nonce"):
            token = contact_web_token(ctx, cid, contact["web_token_nonce"])
            rows.append([{"text": messages.BTN_LIVE, "url": f"{base}/c/{token}"}])
        return {"inline_keyboard": rows}
    if kind == "followup":
        return {"inline_keyboard": [
            [{"text": messages.BTN_SAFE, "callback_data": f"safe:{alert_id}:{cid}"}],
            [{"text": messages.BTN_CANT_REACH, "callback_data": f"cant:{alert_id}:{cid}"}],
        ]}
    return None


# --- apply ------------------------------------------------------------------
async def apply(conn, session_id, event: Event, *, actor: str, event_key: str, ctx: Ctx,
                edit: Optional[dict] = None) -> Applied:
    """Decide and apply one event for one session inside the caller's transaction."""
    sess, alert, contacts, recipients = await _load(conn, session_id)
    snap = build_snapshot(sess, alert, contacts, recipients, ctx.settings)
    d = states.decide(snap, event, ctx.choose)
    timings = snap.timings
    new_version = sess["version"] + (1 if d.changed else 0)

    if d.challenge_n is None:
        challenge_id = None
    elif d.public_changed or d.challenge_n != sess["challenge_n"]:
        challenge_id = secrets.token_hex(3)
    else:
        challenge_id = sess["challenge_id"]

    cur = await conn.execute(
        """UPDATE sessions SET
              state = %(to)s, public_state = %(pub)s, version = %(nv)s,
              challenge_n = %(n)s, challenge_id = %(chid)s, prompt_cause = %(cause)s,
              retry_used = %(retry)s, link_state = %(link)s, lost_contact_told = %(told)s,
              prompted_at = CASE WHEN %(stamp)s::text = 'prompted_at' THEN now() ELSE prompted_at END,
              reminded_at = CASE WHEN %(stamp)s::text = 'reminded_at' THEN now()
                                 WHEN %(stamp)s::text = 'prompted_at' THEN NULL ELSE reminded_at END,
              lost_contact_at = CASE WHEN %(lost)s::boolean IS TRUE THEN now()
                                     WHEN %(lost)s::boolean IS FALSE THEN NULL ELSE lost_contact_at END,
              ended_at = CASE WHEN %(to)s::text = 'ENDED' THEN now() ELSE ended_at END,
              updated_at = now()
            WHERE id = %(id)s AND version = %(v)s AND state = %(from)s
        RETURNING prompted_at, reminded_at""",
        {"to": d.to_state, "pub": d.public_state, "nv": new_version, "n": d.challenge_n,
         "chid": challenge_id, "cause": d.prompt_cause, "retry": d.retry_used, "link": d.link_state,
         "told": d.lost_contact_told, "stamp": d.stamp, "lost": d.stamp_lost, "id": session_id,
         "v": sess["version"], "from": sess["state"]})
    updated = await cur.fetchone()
    if updated is None:
        raise StaleTransition(f"session {session_id} moved under us")

    # Timers
    if d.rekind_cover:
        # A masked duress alert opens: the check-in schedule the wearer sees keeps running
        # as cover timers, which the escalation's transitions do not cancel.
        await conn.execute(
            """UPDATE timers SET kind = 'COVER_' || kind, expected_version = NULL
                WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL AND kind = ANY(%s)""",
            (session_id, list(states.COVER_OF)))
    if d.cancel_all_timers:
        await conn.execute(
            """UPDATE timers SET cancelled_at = now()
                WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL""", (session_id,))
    else:
        kinds = list(states.STATE_TIMER_KINDS) if d.cancel_state_timers else []
        kinds += d.cancel_kinds
        if kinds:
            await conn.execute(
                """UPDATE timers SET cancelled_at = now()
                    WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL
                      AND kind = ANY(%s)""", (session_id, kinds))
    timer_ids: dict[str, int] = {}
    timer_windows: dict[str, float] = {}
    for t in d.timers:
        expected = new_version if t.kind in states.STATE_TIMER_KINDS else None
        payload = {"window_s": t.prompt_window_s} if t.prompt_window_s is not None else {}
        row = await (await conn.execute(
            """INSERT INTO timers (session_id, kind, fire_at, expected_version, payload)
               VALUES (%s, %s, now() + make_interval(secs => %s), %s, %s::jsonb) RETURNING id""",
            (session_id, t.kind, float(t.delay_s), expected, json.dumps(payload)))).fetchone()
        timer_ids[t.kind] = row["id"]
        if t.prompt_window_s is not None:
            timer_windows[t.kind] = float(t.prompt_window_s)

    # Alert
    alert_id = alert["id"] if alert else None
    for op in d.alert_ops:
        if op.op == "open":
            row = await (await conn.execute(
                """INSERT INTO alerts (session_id, kind, level, masked) VALUES (%s, %s, %s, %s) RETURNING id""",
                (session_id, op.kind, op.level or 1, bool(op.masked)))).fetchone()
            alert_id = row["id"]
        elif op.op == "level":
            await conn.execute(
                """UPDATE alerts SET level = %s, is_final = %s, status = 'OPEN', acked_by = NULL,
                          acked_at = NULL, renotify_count = 0 WHERE id = %s""",
                (op.level, op.is_final, alert_id))
        elif op.op == "ack":
            await conn.execute(
                "UPDATE alerts SET status = 'ACKED', acked_by = %s, acked_at = now() WHERE id = %s",
                (op.contact_id, alert_id))
        elif op.op == "close":
            await conn.execute(
                "UPDATE alerts SET status = %s, resolved_by = %s, closed_at = now() WHERE id = %s",
                (op.status, op.by, alert_id))
        elif op.op == "upgrade":
            await conn.execute(
                "UPDATE alerts SET kind = COALESCE(%s, kind), masked = COALESCE(%s, masked) WHERE id = %s",
                (op.kind, op.masked, alert_id))
        elif op.op == "renotify":
            await conn.execute("UPDATE alerts SET renotify_count = renotify_count + 1 WHERE id = %s", (alert_id,))

    # Location
    if d.location == "release" and alert_id is not None:
        await _release_location(conn, ctx, sess["user_id"], alert_id)
    elif d.location == "expire_now" and alert_id is not None:
        await conn.execute(
            """UPDATE location_shares SET expires_at = now()
                WHERE alert_id = %s AND (expires_at IS NULL OR expires_at > now())""", (alert_id,))
    elif d.location == "expire_later" and alert_id is not None:
        await conn.execute(
            """UPDATE location_shares SET expires_at = now() + make_interval(secs => %s)
                WHERE alert_id = %s AND (expires_at IS NULL OR expires_at > now())""",
            (float(timings.link_expiry_s), alert_id))

    # Timeline
    chain = audit.session_chain(session_id)
    head_hash = None
    for e in d.audit:
        detail = dict(e.detail)
        if d.through and e.kind in ("resolved", "false_alarm"):
            detail.setdefault("through", d.through)
        head_hash = await audit.append(
            conn, chain_key=chain, actor=actor, kind=e.kind, user_id=sess["user_id"], session_id=session_id,
            alert_id=alert_id,
            from_state=e.from_state if e.from_state is not None else d.from_state,
            to_state=e.to_state if e.to_state is not None else d.to_state,
            detail=detail, masked=bool(e.masked))

    # Outbox
    now = await db_now(conn)
    tz = ctx.settings.display_tz
    by_id = {c["id"]: c for c in contacts}
    base = {"name": sess["wearer_name"], "time": hm(now, tz),
            "t1": hm(updated["prompted_at"], tz), "t2": hm(updated["reminded_at"], tz)}
    loc_cache: dict = {}
    for i, n in enumerate(d.notify):
        p = dict(base)
        p.update(n.params)
        if "contact_id" in n.params:
            p["contact"] = await _contact_name(conn, by_id, n.params["contact_id"])
        if n.template in LOCATION_TEMPLATES:
            if "loc" not in loc_cache:
                loc_cache["loc"] = await share_for_alert(conn, ctx, alert_id, now)
            p["location"] = loc_cache["loc"]
        if n.template == "ended":
            p["head"] = head_hash or await audit.head(conn, chain)
        text = messages.render(n.template, p)
        contact = by_id.get(n.contact_id) if n.to == "contact" else None
        if n.to == "contact" and contact is None:
            continue  # revoked or never approved: silent
        markup = _markup(n.buttons, alert_id, contact, ctx)
        method, edit_id = "sendMessage", None
        if (edit and n.template == "ack_thanks" and n.contact_id == event.contact_id
                and edit.get("message_id")):
            method, edit_id = "editMessageText", edit["message_id"]
            if edit.get("prefix"):
                text = f"{edit['prefix']}\n\n{text}"
        dedupe = f"{event_key}|{i}|{n.template}|{n.to}:{n.contact_id or ''}"
        await conn.execute(
            """INSERT INTO outbox (dedupe_key, session_id, alert_id, user_id, contact_id, recipient,
                                   method, edit_message_id, purpose, template, text, reply_markup,
                                   protect_content, timer_id, prompt_window_s)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
               ON CONFLICT (dedupe_key) DO NOTHING""",
            (dedupe, session_id, alert_id, sess["user_id"], n.contact_id, n.to, method, edit_id,
             "prompt" if n.deadline_timer else n.template, n.template, text,
             json.dumps(markup) if markup else None, n.to == "contact",
             timer_ids.get(n.deadline_timer) if n.deadline_timer else None,
             timer_windows.get(n.deadline_timer) if n.deadline_timer else None))
        if n.to == "contact" and alert_id is not None:
            await conn.execute(
                """INSERT INTO alert_recipients (alert_id, contact_id) VALUES (%s, %s)
                   ON CONFLICT DO NOTHING""", (alert_id, n.contact_id))

    cmd = states.device_cmd(d.public_state, d.challenge_n, challenge_id, d.prompt_cause)
    return Applied(decision=d, session_id=str(session_id), version=new_version,
                   public_state=d.public_state, cmd=cmd, alert_id=alert_id)


async def _contact_name(conn, by_id: dict, contact_id) -> str:
    if contact_id in by_id:
        return by_id[contact_id]["name"] or "A contact"
    row = await (await conn.execute("SELECT name FROM contacts WHERE id = %s", (contact_id,))).fetchone()
    return (row["name"] if row else None) or "A contact"


# --- helpers used by routes and workers ---------------------------------------
async def maybe_pull_forward(conn, session_id, pf: PullForward, *, actor: str, event_key: str, ctx: Ctx,
                             bypass_cooldown: bool = False) -> Optional[Applied]:
    """Apply a rule's PullForward unless that reason is cooling down."""
    row = await (await conn.execute(
        "SELECT profile, pull_forward_last, now() AS now FROM sessions WHERE id = %s FOR UPDATE",
        (session_id,))).fetchone()
    if row is None:
        raise SessionNotFound(str(session_id))
    last_raw = (row["pull_forward_last"] or {}).get(pf.reason)
    last = datetime.fromisoformat(last_raw) if last_raw else None
    cooldown = ctx.settings.timings(row["profile"]).sensor_cooldown_s
    if not bypass_cooldown and not cooldown_ok(now=row["now"], last_at=last, cooldown_s=cooldown):
        return None
    applied = await apply(conn, session_id,
                          Event(states.PULL_FORWARD, reason=pf.reason, level=pf.level, simulated=pf.simulated),
                          actor=actor, event_key=event_key, ctx=ctx)
    if applied.decision.changed:
        await conn.execute(
            """UPDATE sessions SET pull_forward_last = pull_forward_last || jsonb_build_object(%s::text, now())
                WHERE id = %s""", (pf.reason, session_id))
    return applied


async def lower_prompt_deadline(conn, session_id, delivered_at_sql: str = "now()") -> int:
    """Prompt deadline = delivery + window (never later than the current deadline)."""
    cur = await conn.execute(
        f"""UPDATE timers
               SET fire_at = LEAST(fire_at, {delivered_at_sql} + make_interval(secs => (payload->>'window_s')::float8))
             WHERE session_id = %s
               AND kind IN ('PROMPT_TIMEOUT', 'REMINDER_TIMEOUT', 'COVER_PROMPT_TIMEOUT', 'COVER_REMINDER_TIMEOUT')
               AND fired_at IS NULL AND cancelled_at IS NULL AND payload ? 'window_s'""", (session_id,))
    return cur.rowcount


async def active_session_id(conn, user_id) -> Optional[str]:
    row = await (await conn.execute(
        "SELECT id FROM sessions WHERE user_id = %s AND state <> 'ENDED' ORDER BY created_at DESC LIMIT 1",
        (user_id,))).fetchone()
    return row["id"] if row else None


def public_view(sess: dict) -> dict:
    """What the device and the wearer may see. Never the masked internal state."""
    return {
        "state_public": sess["public_state"],
        "cmd": states.device_cmd(sess["public_state"], sess["challenge_n"], sess["challenge_id"],
                                 sess["prompt_cause"]),
    }
