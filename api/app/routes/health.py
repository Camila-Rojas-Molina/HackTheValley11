"""GET /api/v1/health: 200 when the database answers, 503 with the same `Health` body otherwise.

No auth (the Docker healthcheck uses it). With a valid X-Admin-Key it also reports the job counts per status.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..contract_fixtures import labels_version
from ..deps import get_db, is_admin
from ..schemas import API_PREFIX, CONTRACT_VERSION, Health, route_kwargs

router = APIRouter(prefix=API_PREFIX)
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")


@router.get("/health", **route_kwargs("get_health"))
async def get_health(request: Request):
    # The chain the lifespan built from AI_CHAIN (template only until M6, contracts/ai-interface.md).
    ai_chain = list(request.app.state.ai_chain.order)
    queue = None
    try:
        if not request.app.state.db_ready:
            raise RuntimeError("database not initialised")
        async with get_db(request).connection() as conn:
            row = await (await conn.execute(
                """SELECT now() AS now,
                          EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') AS timescaledb"""
            )).fetchone()
            if is_admin(request):
                counts = {r["status"]: r["n"] for r in await (await conn.execute(
                    "SELECT status, count(*)::int AS n FROM jobs GROUP BY status")).fetchall()}
                queue = {s: counts.get(s, 0) for s in JOB_STATUSES}
    except Exception:  # pool timeout, connection error, or not initialised yet
        down = Health(status="down", db="down", version=CONTRACT_VERSION, labels_version=labels_version(),
                      server_time=None, timescaledb=None, ai_chain=ai_chain, queue=None)
        return JSONResponse(down.model_dump(mode="json"), status_code=503)
    return Health(status="ok", db="ok", version=CONTRACT_VERSION, labels_version=labels_version(),
                  server_time=row["now"], timescaledb=row["timescaledb"], ai_chain=ai_chain, queue=queue)
