"""Care-team alert feedback (C1): Useful / Not useful / Snooze. M1: 501 until Push 2 stores it."""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import serve
from ..schemas import AlertFeedbackRequest, API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.post("/alerts/{alert_id}/feedback", **route_kwargs("post_alert_feedback"))
async def post_alert_feedback(alert_id: str, body: AlertFeedbackRequest):
    return serve("post_alert_feedback", f"/alerts/{alert_id}/feedback", "alert_id")
