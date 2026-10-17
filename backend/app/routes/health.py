"""GET /v1/health: 200 when the database answers, 503 otherwise."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..deps import get_db

router = APIRouter(tags=["health"])


@router.get("/v1/health")
async def health(request: Request):
    ctx = request.app.state.ctx
    info = {
        "telegram": "configured" if ctx.settings.telegram_enabled else "off (log channel)",
        "default_profile": ctx.settings.default_profile,
        "emergency_services": "SIMULATED (never placed)",
    }
    try:
        async with get_db(request).connection() as conn:
            row = await (await conn.execute(
                """SELECT now() AS now,
                          (SELECT count(*) FROM timers WHERE fired_at IS NULL AND cancelled_at IS NULL) AS pending_timers,
                          (SELECT count(*) FROM outbox WHERE status IN ('pending', 'sending')) AS pending_outbox""")).fetchone()
    except Exception as exc:  # pool timeout or connection error
        return JSONResponse({"ok": False, "db": "down", "error": type(exc).__name__, **info}, status_code=503)
    return {"ok": True, "db": "up", "server_time": row["now"].isoformat(),
            "pending_timers": row["pending_timers"], "pending_outbox": row["pending_outbox"], **info}
