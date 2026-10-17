"""The Care-Team Copilot (C1, C4). M1: 501 until the copilot lands."""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import serve
from ..schemas import API_PREFIX, CopilotRequest, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.post("/copilot", **route_kwargs("post_copilot"))
async def post_copilot(body: CopilotRequest):
    return serve("post_copilot", "/copilot", "patient_id")
