"""Append-only, hash-chained personal timeline.

hash = sha256(prev_hash || canonical_json(entry)), chained per chain_key
("session:<id>" or "user:<id>"). The table trigger rejects UPDATE and DELETE
(except the user's own erase, flagged with SET LOCAL stillhere.erase = 'on').
Any edit made after an export breaks the chain when it is recomputed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

GENESIS = ""


def _norm_time(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _jsonable(detail: Any) -> Any:
    return json.loads(json.dumps(detail or {}, default=str))


def canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def entry_payload(*, chain_key: str, at: datetime, actor: str, kind: str, from_state: Optional[str],
                  to_state: Optional[str], detail: Any, masked: bool) -> dict:
    return {
        "chain_key": chain_key,
        "at": _norm_time(at),
        "actor": actor,
        "kind": kind,
        "from_state": from_state,
        "to_state": to_state,
        "detail": _jsonable(detail),
        "masked": bool(masked),
    }


def compute_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


def session_chain(session_id) -> str:
    return f"session:{session_id}"


def user_chain(user_id) -> str:
    return f"user:{user_id}"


async def append(conn, *, chain_key: str, actor: str, kind: str, user_id=None, session_id=None,
                 alert_id: Optional[int] = None, from_state: Optional[str] = None,
                 to_state: Optional[str] = None, detail: Any = None, masked: bool = False) -> str:
    """Append one entry inside the caller's transaction. Returns its hash."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (chain_key,))
    row = await (await conn.execute(
        "SELECT hash FROM audit_log WHERE chain_key = %s ORDER BY id DESC LIMIT 1", (chain_key,)
    )).fetchone()
    prev = _get(row, "hash", GENESIS)
    at = _get(await (await conn.execute("SELECT clock_timestamp() AS at")).fetchone(), "at")
    payload = entry_payload(chain_key=chain_key, at=at, actor=actor, kind=kind, from_state=from_state,
                            to_state=to_state, detail=detail, masked=masked)
    h = compute_hash(prev, payload)
    await conn.execute(
        """INSERT INTO audit_log (chain_key, user_id, session_id, alert_id, at, actor, kind,
                                  from_state, to_state, detail, masked, prev_hash, hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)""",
        (chain_key, user_id, session_id, alert_id, at, actor, kind, from_state, to_state,
         json.dumps(payload["detail"]), bool(masked), prev, h),
    )
    return h


def _get(row, key, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    return row[0]


async def head(conn, chain_key: str) -> str:
    row = await (await conn.execute(
        "SELECT hash FROM audit_log WHERE chain_key = %s ORDER BY id DESC LIMIT 1", (chain_key,)
    )).fetchone()
    return _get(row, "hash", GENESIS)


def verify_rows(rows: list[dict]) -> dict:
    """Recompute a chain from its rows (ordered by id). Returns {ok, head, count, bad_id}."""
    prev = GENESIS
    for r in rows:
        if r["prev_hash"] != prev:
            return {"ok": False, "head": prev, "count": len(rows), "bad_id": r["id"]}
        payload = entry_payload(chain_key=r["chain_key"], at=r["at"], actor=r["actor"], kind=r["kind"],
                                from_state=r["from_state"], to_state=r["to_state"], detail=r["detail"],
                                masked=r["masked"])
        if compute_hash(prev, payload) != r["hash"]:
            return {"ok": False, "head": prev, "count": len(rows), "bad_id": r["id"]}
        prev = r["hash"]
    return {"ok": True, "head": prev, "count": len(rows), "bad_id": None}


# An entry the wearer can see: not masked, or its masked alert is closed. While a masked
# (duress) alert is open its entries stay hidden from her timeline and export.
VISIBLE_SQL = """(NOT a.masked OR a.alert_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM alerts al WHERE al.id = a.alert_id AND al.status IN ('OPEN', 'ACKED')))"""


async def visible_rows(conn, session_id) -> list[dict]:
    cur = await conn.execute(
        f"""SELECT a.id, a.chain_key, a.at, a.actor, a.kind, a.from_state, a.to_state, a.detail, a.masked
              FROM audit_log a WHERE a.session_id = %s AND {VISIBLE_SQL} ORDER BY a.id""", (session_id,))
    return [dict(r) for r in await cur.fetchall()]


def public_head(rows: list[dict]) -> str:
    """Head of the chain over the entries the wearer can see, so she can recompute it from her
    export. Hidden entries join it once their alert is closed; with none, it is the stored head."""
    prev = GENESIS
    for r in rows:
        prev = compute_hash(prev, entry_payload(chain_key=r["chain_key"], at=r["at"], actor=r["actor"],
                                                kind=r["kind"], from_state=r["from_state"],
                                                to_state=r["to_state"], detail=r["detail"], masked=r["masked"]))
    return prev


async def verify_chain(conn, chain_key: str) -> dict:
    cur = await conn.execute(
        """SELECT id, chain_key, at, actor, kind, from_state, to_state, detail, masked, prev_hash, hash
             FROM audit_log WHERE chain_key = %s ORDER BY id""", (chain_key,))
    rows = await cur.fetchall()
    return verify_rows([dict(r) for r in rows])
