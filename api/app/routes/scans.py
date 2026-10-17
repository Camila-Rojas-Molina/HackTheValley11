"""Scans (C1): upload, the manifest, asset files and analysis runs.

- The manifest is built from the database (app.views.manifest).
- An asset is served from storage (app.storage) when the manifest lists it as ready; a pending asset answers
  501 with the milestone it lands in; any other name, `../` included, is a 404.
- Upload and runs are admin routes: 503 without RB_ADMIN_KEY, 401 without the right X-Admin-Key, 413 over
  RB_MAX_UPLOAD_MB; ingest and the analysis jobs land in M5 (501 until then; no scan is stored before G1).
"""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from .. import storage, views
from ..contract_fixtures import not_available
from ..deps import ready_db, require_admin
from ..schemas import API_PREFIX, ID_PATTERN, RunRequest, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.post("/scans", dependencies=[Depends(require_admin)], **route_kwargs("upload_scan"))
async def upload_scan(request: Request, patient_id: Annotated[str, Query(pattern=ID_PATTERN)],
                      day: Annotated[int, Query(ge=0)], label: Optional[str] = None):
    cap = request.app.state.settings.max_upload_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > cap:
        raise storage.TooLarge()
    await storage.read_capped(request.stream(), cap)
    return not_available("upload_scan")


@router.get("/scans/{scan_id}", **route_kwargs("get_scan"))
async def get_scan(scan_id: str, db=Depends(ready_db)):
    async with db.connection() as conn:
        out = await views.manifest(conn, scan_id)
    if out is None:
        raise HTTPException(404, "unknown scan_id")
    return out


@router.get("/scans/{scan_id}/assets/{name}", **route_kwargs("get_scan_asset"))
async def get_scan_asset(request: Request, scan_id: str, name: str, db=Depends(ready_db)):
    if not (storage.valid_scan_id(scan_id) and storage.valid_name(name)):
        raise HTTPException(404, "unknown asset")
    async with db.connection() as conn:
        scan = await views.scan_row(conn, scan_id)
    entry = ((scan or {}).get("files") or {}).get(name)
    if scan is None or entry is None or views.describe_asset(name)[0] is None:
        raise HTTPException(404, "unknown asset")
    if entry.get("status") != "ready":
        milestone = entry.get("available_in") or "M5"
        return JSONResponse({"detail": f"lands in {milestone}", "available_in": milestone}, status_code=501)
    s = request.app.state.settings
    path = storage.asset_path([s.storage_dir, s.demo_dir], scan_id, name)
    if path is None:
        raise HTTPException(404, "asset file not found")
    return FileResponse(path, media_type=storage.media_type(name))


@router.post("/scans/{scan_id}/runs", dependencies=[Depends(require_admin)], **route_kwargs("start_scan_run"))
async def start_scan_run(scan_id: str, body: RunRequest):
    return not_available("start_scan_run")
