"""GET /api/v1/health: 200 when the database answers, 503 with the same `Health` body otherwise.

No auth (the Docker healthcheck uses it). M1 has no jobs table, so `queue` is null; Push 2 adds the job
counts for X-Admin-Key.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..contract_fixtures import labels_version
from ..deps import get_db
from ..schemas import API_PREFIX, CONTRACT_VERSION, Health, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/health", **route_kwargs("get_health"))
async def get_health(request: Request):
    # The chain the lifespan built from AI_CHAIN (template only until M6, contracts/ai-interface.md).
    ai_chain = list(request.app.state.ai_chain.order)
    try:
        async with get_db(request).connection() as conn:
            row = await (await conn.execute(
                """SELECT now() AS now,
                          EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') AS timescaledb"""
            )).fetchone()
    except Exception:  # pool timeout or connection error
        down = Health(status="down", db="down", version=CONTRACT_VERSION, labels_version=labels_version(),
                      server_time=None, timescaledb=None, ai_chain=ai_chain, queue=None)
        return JSONResponse(down.model_dump(mode="json"), status_code=503)
    return Health(status="ok", db="ok", version=CONTRACT_VERSION, labels_version=labels_version(),
                  server_time=row["now"], timescaledb=row["timescaledb"], ai_chain=ai_chain, queue=None)
