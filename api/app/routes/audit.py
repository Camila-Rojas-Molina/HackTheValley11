"""The hash-chained audit trail of one scan (C1), read from audit_log and recomputed on every request."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import audit as chain
from .. import views
from ..deps import ready_db
from ..schemas import API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/audit/{scan_id}", **route_kwargs("get_audit_trail"))
async def get_audit_trail(scan_id: str, db=Depends(ready_db)):
    async with db.connection() as conn:
        if await views.scan_row(conn, scan_id) is None:
            raise HTTPException(404, "unknown scan_id")
        rows = await chain.rows(conn, scan_id)
    check = chain.verify_rows(rows)
    entries = [{"seq": i, "at": r["at"], "actor": r["actor"], "action": r["action"], "patient_id": r["patient_id"],
                "scan_id": r["scan_id"], "run_id": str(r["run_id"]) if r["run_id"] is not None else None,
                "detail": r["detail"], "prev_hash": r["prev_hash"], "hash": r["hash"]}
               for i, r in enumerate(rows, start=1)]
    return {"scan_id": scan_id, "chain_key": chain.chain_key(scan_id), "chain_ok": check["ok"],
            "head": check["head"], "entries": entries}
