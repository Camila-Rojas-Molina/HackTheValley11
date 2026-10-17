"""One metric over time for one patient and site (C1); points carry `provenance` and `filled`.

Read from scan_metrics: one point per viewable scan at that site (a value, or `pending` with the milestone it
lands in), or, for a level without viewable scans, one point per measured timepoint. Gap-filling (M7) adds
`filled` points at query time.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import views
from ..deps import ready_db
from ..schemas import API_PREFIX, SeriesMetric, Site, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/patients/{patient_id}/series", **route_kwargs("get_patient_series"))
async def get_patient_series(patient_id: str, site: Site, metric: SeriesMetric, db=Depends(ready_db)):
    async with db.connection() as conn:
        out = None
        if await views.patient(conn, patient_id) is not None:
            out = await views.series(conn, patient_id, site, metric)
    if out is None:
        raise HTTPException(404, "unknown patient_id, site or metric")
    return out
