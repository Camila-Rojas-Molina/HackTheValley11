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
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from psycopg.conninfo import conninfo_to_dict

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


def _local_database(url: str) -> bool:
    try:
        host = conninfo_to_dict(url).get("host") or ""
    except Exception:
        return False
    return all(h in ("", "localhost", "127.0.0.1", "::1") or h.startswith("/") for h in host.split(","))


async def load_secret(conn, settings: Settings) -> str:
    """APP_SECRET from the environment, else a random one persisted in bot_state."""
    if settings.app_secret:
        return settings.app_secret
    if not _local_database(settings.database_url):
        # The key that seals coordinates and signs contact/location links would then sit in the
        # same remote database as what it protects: anyone with the database could read both.
        log.warning("APP_SECRET is not set: the coordinate key and link signing secret are kept in the "
                    "remote database itself. Set APP_SECRET for a database that is not on this machine.")
    await conn.execute(
        """INSERT INTO bot_state (key, value) VALUES ('app_secret', to_jsonb(%s::text))
           ON CONFLICT (key) DO NOTHING""", (secrets.token_urlsafe(32),))
    row = await (await conn.execute("SELECT value FROM bot_state WHERE key = 'app_secret'")).fetchone()
    return row["value"]


# --- loading ----------------------------------------------------------------
async def duress_ladder_ids(conn, user_id) -> list[int]:
    """Contacts a duress alert may reach: the chosen duress recipients, or every approved contact
    when the wearer chose none (states.duress_ladder)."""
    contacts = await approved_contacts(conn, user_id)
    chosen = [c["id"] for c in contacts if c["duress_eligible"]]
    return chosen or [c["id"] for c in contacts]


async def approved_contacts(conn, user_id) -> list[dict]:
    cur = await conn.execute(
        """SELECT id, name, priority, channel, telegram_chat_id, web_token_nonce, duress_eligible
             FROM contacts
            WHERE user_id = %s AND approved_at IS NOT NULL AND revoked_at IS NULL
            ORDER BY priority, approved_at, id""", (user_id,))
    return list(await cur.fetchall())


async def _recipients(conn, alert: Optional[dict]) -> list[int]:
    if alert is None:
        return []
    cur = await conn.execute(
        "SELECT contact_id FROM alert_recipients WHERE alert_id = %s ORDER BY first_notified_at, contact_id",
        (alert["id"],))
    return [r["contact_id"] for r in await cur.fetchall()]


async def _ackers(conn, alert: Optional[dict]) -> list[int]:
    if alert is None:
        return []
    cur = await conn.execute(
        "SELECT DISTINCT contact_id FROM acks WHERE alert_id = %s AND result = 'acked' ORDER BY contact_id",
        (alert["id"],))
    return [r["contact_id"] for r in await cur.fetchall()]


async def _load(conn, session_id):
    sess = await (await conn.execute(
        """SELECT s.*, u.display_name AS wearer_name
             FROM sessions s JOIN users u ON u.id = s.user_id
            WHERE s.id = %s FOR UPDATE OF s""", (session_id,))).fetchone()
    if sess is None:
        raise SessionNotFound(str(session_id))
    # The escalation's open alert, and the twin's (masked duress only: see states, "the twin").
    rows = await (await conn.execute(
        """SELECT * FROM alerts WHERE session_id = %s AND status IN ('OPEN', 'ACKED')
            ORDER BY cover, id FOR UPDATE""", (session_id,))).fetchall()
    alert = next((r for r in rows if not r["cover"]), None)
    cover = next((r for r in rows if r["cover"]), None)
    contacts = await approved_contacts(conn, sess["user_id"])
    for a in (alert, cover):
        if a is not None:
            a["ackers"] = await _ackers(conn, a)
    return sess, alert, contacts, await _recipients(conn, alert), cover, await _recipients(conn, cover)


def _alert_view(alert: Optional[dict], recipients: list[int]) -> Optional[states.AlertView]:
    if alert is None:
        return None
    return states.AlertView(
        id=alert["id"], kind=alert["kind"], level=alert["level"], is_final=alert["is_final"],
        masked=alert["masked"], status=alert["status"], acked_by=alert["acked_by"],
        renotify_count=alert["renotify_count"], recipients=tuple(recipients),
        ackers=tuple(alert.get("ackers") or ()))


def build_snapshot(sess: dict, alert: Optional[dict], contacts: list[dict], recipients: list[int],
                   settings: Settings, cover: Optional[dict] = None, cover_recipients: list[int] = ()) -> Snapshot:
    return Snapshot(
        state=sess["state"], public_state=sess["public_state"], version=sess["version"],
        timings=settings.timings(sess["profile"]), contacts=tuple(c["id"] for c in contacts),
        duress_contacts=tuple(c["id"] for c in contacts if c["duress_eligible"]),
        alert=_alert_view(alert, recipients), challenge_n=sess["challenge_n"], prompt_cause=sess["prompt_cause"],
        retry_used=sess["retry_used"], link_state=sess["link_state"],
        lost_contact_told=sess["lost_contact_told"], window_multiplier=float(sess["window_multiplier"]),
        cover_alert=_alert_view(cover, list(cover_recipients)), public_link_state=sess["public_link_state"],
        cover_told=sess["cover_told"])


# --- location -----------------------------------------------------------------
# Location privacy (research 1.15): only the last-known location is kept (no history), plus the
# copy each alert released; real locations are deleted 48 h after capture (the night-out raw-data
# window) unless an open alert still holds them. The simulated demo location is not personal data.
LOCATION_RETENTION_H = 48
LOCATION_PURGE_EVERY_S = 600.0
_last_location_purge: dict[str, float] = {}


async def record_location(conn, ctx: Ctx, *, user_id, session_id, lat: float, lon: float,
                          accuracy_m: Optional[float], source: str, simulated: bool) -> int:
    row = await (await conn.execute(
        """INSERT INTO location_snapshots (user_id, session_id, coords_enc, accuracy_m, source, simulated)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (user_id, session_id, encrypt_coords(ctx.secret, lat, lon), accuracy_m, source, simulated))).fetchone()
    # No location history: the new one replaces every earlier one no alert has released.
    await conn.execute(
        """DELETE FROM location_snapshots s WHERE s.user_id = %s AND s.id <> %s
              AND NOT EXISTS (SELECT 1 FROM location_shares ls WHERE ls.snapshot_id = s.id)""",
        (user_id, row["id"]))
    return row["id"]


async def purge_locations(conn) -> dict:
    """Delete real locations captured more than LOCATION_RETENTION_H ago (and their released links),
    unless an open alert still holds them, and links that expired that long ago."""
    snaps = await conn.execute(
        """DELETE FROM location_snapshots s
            WHERE NOT s.simulated AND s.captured_at < now() - make_interval(hours => %s)
              AND NOT EXISTS (SELECT 1 FROM location_shares ls JOIN alerts a ON a.id = ls.alert_id
                               WHERE ls.snapshot_id = s.id AND a.status IN ('OPEN', 'ACKED'))""",
        (LOCATION_RETENTION_H,))
    shares = await conn.execute(
        """DELETE FROM location_shares
            WHERE expires_at IS NOT NULL AND expires_at < now() - make_interval(hours => %s)""",
        (LOCATION_RETENTION_H,))
    return {"snapshots": snaps.rowcount, "shares": shares.rowcount}


async def maybe_purge_locations(db) -> bool:
    """From the timer worker: at most every 10 minutes per database. Best effort."""
    key = db.conninfo
    now = time.monotonic()
    if now - _last_location_purge.get(key, -1e9) < LOCATION_PURGE_EVERY_S:
        return False
    _last_location_purge[key] = now
    try:
        async with db.connection() as conn:
            async with conn.transaction():
                await purge_locations(conn)
        return True
    except Exception as exc:
        log.warning("location purge failed: %s", type(exc).__name__)
        return False


# --- personal timeline retention (BUILD_PLAN §6.3) ------------------------------------------------------
TIMELINE_DAYS = 30
TIMELINE_EXPIRY_EVERY_S = 3600.0
_last_timeline_expiry: dict[str, float] = {}


async def expire_timelines(conn, days: int = TIMELINE_DAYS) -> dict:
    """Delete the timeline of every session that ended more than `days` ago and is not pinned, with the
    erase flag "Delete my data" uses, and leave one tombstone per wearer on her account chain.

    Only entries she can see go (audit.VISIBLE_SQL): the hidden entries of a masked duress alert that is
    still open stay until it closes, exactly as with "Delete my data". `ended_at` is the end she saw, so a
    session that only looks ended (duress end) loses its visible entries on the same day as a real one."""
    async with conn.transaction():
        await conn.execute("SET LOCAL stillhere.erase = 'on'")
        rows = await (await conn.execute(
            f"""DELETE FROM audit_log a USING sessions s
                 WHERE a.session_id = s.id AND NOT s.pinned
                   AND s.ended_at < now() - make_interval(days => %s) AND {audit.VISIBLE_SQL}
             RETURNING s.user_id, s.id AS session_id""", (int(days),))).fetchall()
        await conn.execute("SET LOCAL stillhere.erase = 'off'")
        per_user: dict = {}
        for r in rows:
            u = per_user.setdefault(r["user_id"], {"sessions": set(), "entries": 0})
            u["sessions"].add(r["session_id"])
            u["entries"] += 1
        for user_id, u in per_user.items():
            await audit.append(conn, chain_key=audit.user_chain(user_id), actor="system", kind="timeline_erased",
                               user_id=user_id, detail={"by": "retention", "after_days": int(days),
                                                        "sessions": len(u["sessions"]), "audit_rows": u["entries"]})
    return {"sessions": sum(len(u["sessions"]) for u in per_user.values()), "entries": len(rows),
            "users": [str(u) for u in per_user]}


async def maybe_expire_timelines(db) -> bool:
    """From the timer worker: at most hourly per database. Best effort; the next run catches up."""
    key = db.conninfo
    now = time.monotonic()
    if now - _last_timeline_expiry.get(key, -1e9) < TIMELINE_EXPIRY_EVERY_S:
        return False
    _last_timeline_expiry[key] = now
    try:
        async with db.connection() as conn:
            out = await expire_timelines(conn)
        if out["entries"]:
            log.info("timeline retention: %d entries of %d sessions deleted", out["entries"], out["sessions"])
        return True
    except Exception as exc:
        log.warning("timeline retention failed: %s", type(exc).__name__)
        return False


def location_token(ctx: Ctx, alert_id: int, nonce: str) -> str:
    return derive_token(ctx.secret, "loc", str(alert_id), nonce)


def contact_web_token(ctx: Ctx, contact_id: int, nonce: str) -> str:
    return derive_token(ctx.secret, "contact", str(contact_id), nonce)


async def _release_location(conn, ctx: Ctx, user_id, alert_id: int, from_alert: Optional[int] = None) -> None:
    """Release the last-known location to an alert (once). `from_alert`: the same location another
    alert already released (a masked duress alert forked from the alert she could see)."""
    exists = await (await conn.execute(
        "SELECT 1 FROM location_shares WHERE alert_id = %s", (alert_id,))).fetchone()
    if exists:
        return
    snap = None
    if from_alert is not None:
        snap = await (await conn.execute(
            "SELECT snapshot_id AS id FROM location_shares WHERE alert_id = %s ORDER BY id LIMIT 1",
            (from_alert,))).fetchone()
    if snap is None:
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


async def share_for_alert(conn, ctx: Ctx, alert_id: Optional[int], now: datetime,
                          contact_id: Optional[int] = None) -> Optional[dict]:
    """The location this alert released, as a link of the contact's own (created on first use, and
    expired with the alert or when the contact is removed)."""
    if alert_id is None:
        return None
    q = """SELECT ls.token_nonce, ls.expires_at, ls.snapshot_id, s.captured_at, s.simulated
             FROM location_shares ls JOIN location_snapshots s ON s.id = ls.snapshot_id
            WHERE ls.alert_id = %s AND ls.contact_id IS NOT DISTINCT FROM %s ORDER BY ls.id DESC LIMIT 1"""
    row = await (await conn.execute(q, (alert_id, contact_id))).fetchone()
    if row is None and contact_id is not None:
        release = await (await conn.execute(q, (alert_id, None))).fetchone()
        if release is None:
            return None
        nonce = new_token()
        await conn.execute(
            """INSERT INTO location_shares (token_hash, token_nonce, snapshot_id, alert_id, contact_id, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (alert_id, contact_id) WHERE contact_id IS NOT NULL DO NOTHING""",
            (sha256_hex(location_token(ctx, alert_id, nonce)), nonce, release["snapshot_id"], alert_id, contact_id,
             release["expires_at"]))
        row = await (await conn.execute(q, (alert_id, contact_id))).fetchone()
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
    sess, alert, contacts, recipients, cover, cover_recipients = await _load(conn, session_id)
    snap = build_snapshot(sess, alert, contacts, recipients, ctx.settings, cover, cover_recipients)
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
              public_link_state = %(plink)s, cover_told = %(ctold)s,
              prompted_at = CASE WHEN %(stamp)s::text = 'prompted_at' THEN now() ELSE prompted_at END,
              reminded_at = CASE WHEN %(stamp)s::text = 'reminded_at' THEN now()
                                 WHEN %(stamp)s::text = 'prompted_at' THEN NULL ELSE reminded_at END,
              lost_contact_at = CASE WHEN %(lost)s::boolean IS TRUE THEN now()
                                     WHEN %(lost)s::boolean IS FALSE THEN NULL ELSE lost_contact_at END,
              ended_at = CASE WHEN %(to)s::text = 'ENDED' OR %(pub)s::text = 'ENDED'  -- ended, or looks it
                              THEN coalesce(ended_at, now()) ELSE ended_at END,
              updated_at = now()
            WHERE id = %(id)s AND version = %(v)s AND state = %(from)s
        RETURNING prompted_at, reminded_at""",
        {"to": d.to_state, "pub": d.public_state, "nv": new_version, "n": d.challenge_n,
         "chid": challenge_id, "cause": d.prompt_cause, "retry": d.retry_used, "link": d.link_state,
         "told": d.lost_contact_told, "plink": d.public_link_state, "ctold": d.cover_told, "stamp": d.stamp,
         "lost": d.stamp_lost, "id": session_id, "v": sess["version"], "from": sess["state"]})
    updated = await cur.fetchone()
    if updated is None:
        raise StaleTransition(f"session {session_id} moved under us")

    # Timers
    if d.rekind_cover:
        # A masked duress alert opens: the schedule the wearer sees keeps running as cover timers,
        # which the escalation's transitions do not cancel. Check-in timers move to the twin; the
        # timers of an alert she could see are copied (the escalation keeps its own).
        await conn.execute(
            """UPDATE timers SET kind = 'COVER_' || kind, expected_version = NULL
                WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL AND kind = ANY(%s)""",
            (session_id, list(states.COVER_MOVE_KINDS)))
        await conn.execute(
            """INSERT INTO timers (session_id, kind, fire_at, payload)
               SELECT session_id, 'COVER_' || kind, fire_at, payload FROM timers
                WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL AND kind = ANY(%s)""",
            (session_id, list(states.COVER_COPY_KINDS)))
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

    # Alerts: the escalation's, and the twin's (masked duress)
    alert_id = alert["id"] if alert else None
    cover_id = cover["id"] if cover else None
    if d.fork and alert_id is not None:
        # The alert she could see stays hers (the twin's) under its own id; the escalation goes on
        # as a copy of it, with the same contacts told and the same released location.
        cover_id = alert_id
        await conn.execute("UPDATE alerts SET cover = true WHERE id = %s", (cover_id,))
        alert_id = (await (await conn.execute(
            """INSERT INTO alerts (session_id, kind, level, is_final, masked, status, acked_by, acked_at,
                                   renotify_count, opened_at)
               SELECT session_id, kind, level, is_final, masked, status, acked_by, acked_at, renotify_count, opened_at
                 FROM alerts WHERE id = %s RETURNING id""", (cover_id,))).fetchone())["id"]
        await conn.execute(
            """INSERT INTO alert_recipients (alert_id, contact_id, first_notified_at)
               SELECT %s, contact_id, first_notified_at FROM alert_recipients WHERE alert_id = %s""",
            (alert_id, cover_id))
        await _release_location(conn, ctx, sess["user_id"], alert_id, from_alert=cover_id)
    alert_id = await _apply_alert_ops(conn, session_id, alert_id, d.alert_ops, cover=False)
    cover_id = await _apply_alert_ops(conn, session_id, cover_id, d.cover_alert_ops, cover=True)

    # Location
    await _apply_location(conn, ctx, sess["user_id"], alert_id, d.location, timings)
    await _apply_location(conn, ctx, sess["user_id"], cover_id, d.cover_location, timings)

    # Timeline
    chain = audit.session_chain(session_id)
    for e in d.audit:
        detail = dict(e.detail)
        if d.through and e.kind in ("resolved", "false_alarm"):
            detail.setdefault("through", d.through)
        await audit.append(
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
    loc_cache: dict = {}  # each contact gets a location link of their own
    for i, n in enumerate(d.notify):
        about = cover_id if n.twin else alert_id  # a twin message is about the twin's alert
        if n.to == "contact" and about is not None and n.contact_id in by_id:
            await conn.execute(
                """INSERT INTO alert_recipients (alert_id, contact_id) VALUES (%s, %s)
                   ON CONFLICT DO NOTHING""", (about, n.contact_id))
        if n.silent:
            continue  # the twin would have told them; they are on the escalation instead
        p = dict(base)
        p.update(n.params)
        if "contact_id" in n.params:
            p["contact"] = await _contact_name(conn, by_id, n.params["contact_id"])
        if n.template in LOCATION_TEMPLATES:
            if (about, n.contact_id) not in loc_cache:
                loc_cache[(about, n.contact_id)] = await share_for_alert(conn, ctx, about, now, n.contact_id)
            p["location"] = loc_cache[(about, n.contact_id)]
        if n.template == "ended":
            # The head of what she can see, so it recomputes from her export (masked duress too).
            p["head"] = audit.public_head(await audit.visible_rows(conn, session_id))
        text = messages.render(n.template, p)
        contact = by_id.get(n.contact_id) if n.to == "contact" else None
        if n.to == "contact" and contact is None:
            continue  # revoked or never approved: silent
        markup = _markup(n.buttons, about, contact, ctx)
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
            (dedupe, session_id, about, sess["user_id"], n.contact_id, n.to, method, edit_id,
             "prompt" if n.deadline_timer else n.template, n.template, text,
             json.dumps(markup) if markup else None, n.to == "contact",
             timer_ids.get(n.deadline_timer) if n.deadline_timer else None,
             timer_windows.get(n.deadline_timer) if n.deadline_timer else None))

    cmd = states.device_cmd(d.public_state, d.challenge_n, challenge_id, d.prompt_cause)
    return Applied(decision=d, session_id=str(session_id), version=new_version,
                   public_state=d.public_state, cmd=cmd, alert_id=alert_id)


async def _apply_alert_ops(conn, session_id, alert_id: Optional[int], ops: list, *, cover: bool) -> Optional[int]:
    for op in ops:
        if op.op == "open":
            row = await (await conn.execute(
                """INSERT INTO alerts (session_id, kind, level, masked, cover) VALUES (%s, %s, %s, %s, %s)
                   RETURNING id""", (session_id, op.kind, op.level or 1, bool(op.masked), cover))).fetchone()
            alert_id = row["id"]
        elif op.op == "level":
            await conn.execute(
                """UPDATE alerts SET level = %s, is_final = %s, status = 'OPEN', acked_by = NULL, acked_at = NULL,
                          renotify_count = CASE WHEN %s THEN renotify_count ELSE 0 END WHERE id = %s""",
                (op.level, op.is_final, op.keep_count, alert_id))
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
    return alert_id


async def _apply_location(conn, ctx: Ctx, user_id, alert_id: Optional[int], op: Optional[str], timings) -> None:
    if op is None or alert_id is None:
        return
    if op == "release":
        await _release_location(conn, ctx, user_id, alert_id)
    elif op == "expire_now":
        await conn.execute(
            """UPDATE location_shares SET expires_at = now()
                WHERE alert_id = %s AND (expires_at IS NULL OR expires_at > now())""", (alert_id,))
    elif op == "expire_later":
        await conn.execute(
            """UPDATE location_shares SET expires_at = now() + make_interval(secs => %s)
                WHERE alert_id = %s AND (expires_at IS NULL OR expires_at > now())""",
            (float(timings.link_expiry_s), alert_id))


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
    if applied.decision.public_changed:  # the prompt she sees moved (a masked session's cover too)
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
    """The wearer's running session: the publicly active one, else one that only looks ended
    (ended with the duress PIN while its masked alert is still open)."""
    row = await (await conn.execute(
        """SELECT id FROM sessions WHERE user_id = %s AND state <> 'ENDED'
            ORDER BY (public_state = 'ENDED'), created_at DESC LIMIT 1""",
        (user_id,))).fetchone()
    return row["id"] if row else None


def contact_sees(alert: dict, on_duress_ladder: bool) -> bool:
    """Whether a contact told about this open alert sees it: the twin's alert (masked duress) only
    for a contact kept out of duress alerts, a duress alert only for a duress contact."""
    if alert["cover"]:
        return not on_duress_ladder
    if alert["kind"] == states.DURESS:
        return on_duress_ladder
    return True


async def contact_alert(conn, contact: dict, alert_id: int) -> Optional[dict]:
    """The alert a contact's button acts on, from the alert id the button carries (Telegram): only an
    open alert of the contact's own wearer that they were told about. A duress contact pressing an
    old button of the alert she could see (now the twin's) acts on the escalation that continues it;
    a contact kept out of duress alerts acts on the twin's."""
    q = """SELECT a.id, a.session_id, a.kind, a.cover FROM alerts a
             JOIN sessions s ON s.id = a.session_id AND s.user_id = %(user)s
             JOIN alert_recipients r ON r.alert_id = a.id AND r.contact_id = %(contact)s
            WHERE a.status IN ('OPEN', 'ACKED') AND """
    args = {"user": contact["user_id"], "contact": contact["id"]}
    alert = await (await conn.execute(q + "a.id = %(alert)s", {**args, "alert": alert_id})).fetchone()
    if alert is None:
        return None
    on_ladder = contact["id"] in await duress_ladder_ids(conn, contact["user_id"])
    if alert["cover"] and on_ladder:
        alert = await (await conn.execute(q + "a.session_id = %(sid)s AND NOT a.cover",
                                          {**args, "sid": alert["session_id"]})).fetchone()
    return alert if alert is not None and contact_sees(alert, on_ladder) else None


def public_view(sess: dict) -> dict:
    """What the device and the wearer may see. Never the masked internal state."""
    return {
        "state_public": sess["public_state"],
        "cmd": states.device_cmd(sess["public_state"], sess["challenge_n"], sess["challenge_id"],
                                 sess["prompt_cause"]),
    }
