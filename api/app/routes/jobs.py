"""Job status, seconds and cost (C1, C5). The queue exists from M2 (app/jobs.py); this route goes live with
the worker and POST /scans/{id}/runs in M5, and answers 501 until then."""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import not_available
from ..schemas import API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/jobs/{job_id}", **route_kwargs("get_job"))
async def get_job(job_id: str):
    return not_available("get_job")
