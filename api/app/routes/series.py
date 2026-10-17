"""One metric over time for one patient and site (C1); points carry `provenance` and `filled`.

M1: golden fixtures (app.contract_fixtures). Push 2 reads the database; gap-filling arrives with M7.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import serve
from ..schemas import API_PREFIX, SeriesMetric, Site, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/patients/{patient_id}/series", **route_kwargs("get_patient_series"))
async def get_patient_series(patient_id: str, site: Site, metric: SeriesMetric):
    return serve("get_patient_series", f"/patients/{patient_id}/series?site={site}&metric={metric}",
                 "patient_id, site or metric")
