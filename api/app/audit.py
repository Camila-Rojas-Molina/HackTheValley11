"""Append-only, hash-chained audit trail, one chain per scan (contracts/db.md C3, `audit_log`).

hash = sha256(prev_hash || canonical_json({chain_key, at, actor, action, patient_id, scan_id, run_id, detail}))
with canonical = sort_keys, separators (",", ":"), ensure_ascii False; `at` is UTC ISO 8601 with microseconds;
the genesis prev_hash is "". The table trigger rb_audit_guard() refuses UPDATE, DELETE and TRUNCATE, so an
edit made after an export breaks the chain when it is recomputed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from .security import canonical_json

GENESIS = ""


def chain_key(scan_id: str) -> str:
    return f"scan:{scan_id}"


def _norm_time(at: datetime) -> str:
    return at.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _jsonable(detail: Any) -> Any:
    return json.loads(json.dumps(detail or {}, default=str))


def entry_payload(*, chain_key: str, at: datetime, actor: str, action: str, patient_id: Optional[str],
                  scan_id: Optional[str], run_id: Any, detail: Any) -> dict:
    return {
        "chain_key": chain_key,
        "at": _norm_time(at),
        "actor": actor,
        "action": action,
        "patient_id": patient_id,
        "scan_id": scan_id,
        "run_id": str(run_id) if run_id is not None else None,
        "detail": _jsonable(detail),
    }


def compute_hash(prev_hash: str, payload: dict) -> str:
    return hashlib.sha256((prev_hash + canonical_json(payload)).encode("utf-8")).hexdigest()


async def head(conn, key: str) -> str:
    row = await (await conn.execute(
        "SELECT hash FROM audit_log WHERE chain_key = %s ORDER BY id DESC LIMIT 1", (key,))).fetchone()
    return row["hash"] if row else GENESIS


async def append(conn, *, scan_id: str, actor: str, action: str, patient_id: Optional[str] = None,
                 run_id: Any = None, detail: Any = None) -> str:
    """Append one entry to scan_id's chain inside the caller's transaction. Returns its hash."""
    key = chain_key(scan_id)
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
    prev = await head(conn, key)
    at = (await (await conn.execute("SELECT clock_timestamp() AS at")).fetchone())["at"]
    payload = entry_payload(chain_key=key, at=at, actor=actor, action=action, patient_id=patient_id,
                            scan_id=scan_id, run_id=run_id, detail=detail)
    h = compute_hash(prev, payload)
    await conn.execute(
        """INSERT INTO audit_log (chain_key, patient_id, scan_id, run_id, at, actor, action, detail, prev_hash, hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)""",
        (key, patient_id, scan_id, payload["run_id"], at, actor, action, json.dumps(payload["detail"]), prev, h))
    return h


def verify_rows(rows: list[dict]) -> dict:
    """Recompute a chain from its rows (ordered by id). Returns {ok, head, count, bad_id}."""
    prev = GENESIS
    for r in rows:
        if r["prev_hash"] != prev:
            return {"ok": False, "head": prev, "count": len(rows), "bad_id": r["id"]}
        payload = entry_payload(chain_key=r["chain_key"], at=r["at"], actor=r["actor"], action=r["action"],
                                patient_id=r["patient_id"], scan_id=r["scan_id"], run_id=r["run_id"],
                                detail=r["detail"])
        if compute_hash(prev, payload) != r["hash"]:
            return {"ok": False, "head": prev, "count": len(rows), "bad_id": r["id"]}
        prev = r["hash"]
    return {"ok": True, "head": prev, "count": len(rows), "bad_id": None}


async def rows(conn, scan_id: str) -> list[dict]:
    cur = await conn.execute(
        """SELECT id, chain_key, patient_id, scan_id, run_id, at, actor, action, detail, prev_hash, hash
             FROM audit_log WHERE chain_key = %s ORDER BY id""", (chain_key(scan_id),))
    return [dict(r) for r in await cur.fetchall()]


async def verify_chain(conn, scan_id: str) -> dict:
    return verify_rows(await rows(conn, scan_id))
