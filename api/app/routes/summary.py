"""The care-team summary, the outage drill and the patient explanation (C1, C4).

M1: the summary is a golden fixture (the template's text); the drill and the explanation answer 501 until
the AI layer is live.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import serve
from ..schemas import API_PREFIX, RegenerateRequest, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/scans/{scan_id}/summary", **route_kwargs("get_scan_summary"))
async def get_scan_summary(scan_id: str):
    return serve("get_scan_summary", f"/scans/{scan_id}/summary", "scan_id")


@router.post("/scans/{scan_id}/summary/regenerate", **route_kwargs("regenerate_scan_summary"))
async def regenerate_scan_summary(scan_id: str, body: RegenerateRequest):
    return serve("regenerate_scan_summary", f"/scans/{scan_id}/summary/regenerate", "scan_id")


@router.get("/scans/{scan_id}/explanation", **route_kwargs("get_scan_explanation"))
async def get_scan_explanation(scan_id: str):
    return serve("get_scan_explanation", f"/scans/{scan_id}/explanation", "scan_id")
