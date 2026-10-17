"""Scans (C1): upload, the manifest, asset files and analysis runs.

M1: the manifest is a golden fixture (app.contract_fixtures); upload, assets and runs answer 501 with the
milestone that makes them live.
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Query

from ..contract_fixtures import serve
from ..schemas import API_PREFIX, ID_PATTERN, RunRequest, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.post("/scans", **route_kwargs("upload_scan"))
async def upload_scan(patient_id: Annotated[str, Query(pattern=ID_PATTERN)], day: Annotated[int, Query(ge=0)],
                      label: Optional[str] = None):
    return serve("upload_scan", "/scans", "scan")


@router.get("/scans/{scan_id}", **route_kwargs("get_scan"))
async def get_scan(scan_id: str):
    return serve("get_scan", f"/scans/{scan_id}", "scan_id")


@router.get("/scans/{scan_id}/assets/{name}", **route_kwargs("get_scan_asset"))
async def get_scan_asset(scan_id: str, name: str):
    return serve("get_scan_asset", f"/scans/{scan_id}/assets/{name}", "asset")


@router.post("/scans/{scan_id}/runs", **route_kwargs("start_scan_run"))
async def start_scan_run(scan_id: str, body: RunRequest):
    return serve("start_scan_run", f"/scans/{scan_id}/runs", "scan_id")
