"""The hash-chained audit trail of one scan (C1).

M1: the golden fixture (app.contract_fixtures). Push 2 reads audit_log.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import serve
from ..schemas import API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/audit/{scan_id}", **route_kwargs("get_audit_trail"))
async def get_audit_trail(scan_id: str):
    return serve("get_audit_trail", f"/audit/{scan_id}", "scan_id")
