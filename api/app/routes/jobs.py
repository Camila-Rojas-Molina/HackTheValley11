"""Job status, seconds and cost (C1, C5). M1: 501 until the job queue lands."""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import serve
from ..schemas import API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/jobs/{job_id}", **route_kwargs("get_job"))
async def get_job(job_id: str):
    return serve("get_job", f"/jobs/{job_id}", "job_id")
