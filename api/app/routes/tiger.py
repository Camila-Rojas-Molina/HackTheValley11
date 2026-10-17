"""Tiger Data info (C1): hypertables, continuous aggregates, columnstore bytes and the database size.

M1: the golden fixture (app.contract_fixtures). Push 2 reads them from the database.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import serve
from ..schemas import API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/tiger/info", **route_kwargs("get_tiger_info"))
async def get_tiger_info():
    return serve("get_tiger_info", "/tiger/info", "tiger info")
