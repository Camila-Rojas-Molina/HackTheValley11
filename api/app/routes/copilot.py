"""The Care-Team Copilot (C1, C4). 501 until the copilot lands (M8)."""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import not_available
from ..schemas import API_PREFIX, CopilotRequest, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.post("/copilot", **route_kwargs("post_copilot"))
async def post_copilot(body: CopilotRequest):
    return not_available("post_copilot")
