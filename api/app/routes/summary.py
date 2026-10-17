"""The care-team summary, the outage drill and the patient explanation (C1, C4).

GET summary builds the Facts from the database (app.views.facts_for) and runs the AI chain the lifespan built;
until M6 the chain is pinned to the template, so `source` is "template". The drill and the explanation answer
501 until the AI layer is live (M6); the drill is an admin route.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import views
from ..contract_fixtures import not_available
from ..deps import ready_db, require_admin
from ..schemas import API_PREFIX, RegenerateRequest, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/scans/{scan_id}/summary", **route_kwargs("get_scan_summary"))
async def get_scan_summary(request: Request, scan_id: str, db=Depends(ready_db)):
    async with db.connection() as conn:
        facts = await views.facts_for(conn, scan_id)
        scan = await views.scan_row(conn, scan_id) if facts is not None else None
    if facts is None:
        # No such scan, or nothing to summarise: a phantom, or a baseline with no earlier scan.
        raise HTTPException(404, "unknown scan_id")
    return await request.app.state.ai_chain.run("care_team_summary", facts, patient_id=scan["patient_id"])


@router.post("/scans/{scan_id}/summary/regenerate", dependencies=[Depends(require_admin)],
             **route_kwargs("regenerate_scan_summary"))
async def regenerate_scan_summary(scan_id: str, body: RegenerateRequest):
    return not_available("regenerate_scan_summary")


@router.get("/scans/{scan_id}/explanation", **route_kwargs("get_scan_explanation"))
async def get_scan_explanation(scan_id: str):
    return not_available("get_scan_explanation")
