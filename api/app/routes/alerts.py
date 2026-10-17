"""Care-team alert feedback (C1, PLAN 6.6): Useful / Not useful / Snooze, stored in alert_feedback and appended
to the scan's audit chain. An alert id is `al-<scan_id>` of a patient's scan."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from .. import audit, views
from ..deps import ready_db
from ..schemas import AlertFeedbackRequest, API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.post("/alerts/{alert_id}/feedback", **route_kwargs("post_alert_feedback"))
async def post_alert_feedback(alert_id: str, body: AlertFeedbackRequest, db=Depends(ready_db)):
    if body.feedback != "snooze" and body.snooze_hours is not None:
        return JSONResponse({"detail": "snooze_hours is for snooze only"}, status_code=422)
    scan_id = alert_id[3:] if alert_id.startswith("al-") else ""
    async with db.connection() as conn:
        scan = await views.scan_row(conn, scan_id) if scan_id else None
        if scan is None or not scan["patient_id"]:
            raise HTTPException(404, "unknown alert_id")
        hours = body.snooze_hours or views.labels()["flag"]["snooze_default_hours"]
        async with conn.transaction():
            row = await (await conn.execute(
                """INSERT INTO alert_feedback (alert_id, patient_id, scan_id, feedback, snooze_until, note, actor)
                   VALUES (%s, %s, %s, %s,
                           CASE WHEN %s THEN now() + make_interval(hours => %s) END, %s, 'care_team')
                RETURNING feedback_id, alert_id, feedback, snooze_until, created_at""",
                (alert_id, scan["patient_id"], scan_id, body.feedback, body.feedback == "snooze", int(hours),
                 body.note))).fetchone()
            await audit.append(conn, scan_id=scan_id, patient_id=scan["patient_id"], actor="care_team",
                               action="feedback",
                               detail={"alert_id": alert_id, "feedback": body.feedback,
                                       "feedback_id": row["feedback_id"],
                                       "snooze_until": row["snooze_until"], "note": body.note})
    return {"feedback_id": row["feedback_id"], "alert_id": row["alert_id"], "feedback": row["feedback"],
            "snooze_until": row["snooze_until"], "recorded_at": row["created_at"]}
