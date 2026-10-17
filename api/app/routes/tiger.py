"""Tiger Data info (C1): hypertables, continuous aggregates, columnstore bytes, raw vs aggregate query time and
the database size, measured live (app/tiger.py)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import tiger
from ..deps import ready_db
from ..schemas import API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/tiger/info", **route_kwargs("get_tiger_info"))
async def get_tiger_info(db=Depends(ready_db)):
    async with db.connection() as conn:
        return await tiger.info(conn)
