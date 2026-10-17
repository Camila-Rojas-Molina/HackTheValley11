"""GET /v1/health: 200 when the database answers, 503 otherwise.

No auth (the Docker healthcheck uses it). Queue sizes are for the operator only
(X-Admin-Key): a duress PIN adds escalation timers and contact messages, so
public counters would tell it apart from the real PIN.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..deps import get_db
from ..security import same

router = APIRouter(tags=["health"])


@router.get("/v1/health")
async def health(request: Request):
    ctx = request.app.state.ctx
    info = {
        "telegram": "configured" if ctx.settings.telegram_enabled else "off (log channel)",
        "default_profile": ctx.settings.default_profile,
        "emergency_services": "SIMULATED (never placed)",
    }
    operator = bool(ctx.settings.admin_key) and same(request.headers.get("x-admin-key"), ctx.settings.admin_key)
    try:
        async with get_db(request).connection() as conn:
            row = await (await conn.execute(
                """SELECT now() AS now,
                          (SELECT count(*) FROM timers WHERE fired_at IS NULL AND cancelled_at IS NULL) AS pending_timers,
                          (SELECT count(*) FROM outbox WHERE status IN ('pending', 'sending')) AS pending_outbox""")).fetchone()
    except Exception as exc:  # pool timeout or connection error
        return JSONResponse({"ok": False, "db": "down", "error": type(exc).__name__, **info}, status_code=503)
    out = {"ok": True, "db": "up", "server_time": row["now"].isoformat(), **info}
    if operator:
        out.update(pending_timers=row["pending_timers"], pending_outbox=row["pending_outbox"])
    return out
