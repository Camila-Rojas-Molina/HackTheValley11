"""Heart-rate ingest. Pluggable: any source can post here (HeartCast via the
Chrome BLE bridge, a streaming-service API, an Apple Health Shortcut, the
simulator). Rules read hr_samples; they can only bring a check-in forward.

POST /v1/hr  {user_id?, source, simulated, samples:[{t, bpm, rr_ms[], rr_synthetic}]}
             or a single sample {ts, bpm, rr_ms, rr_synthetic, source, simulated}
GET  /v1/hr/status   "HR live" / "HR stale" (no sample for 15 s) and the sources posting in the
             last minute. Display only (research 1.12): stale or missing heart rate never delays
             a check-in; the heart-rate-lost rule (hr_lost_s) only brings one forward.
Auth: the device key (Authorization + X-Device-Id) or the wearer key.
GET /hr-bridge serves the Web Bluetooth bridge page (pages/hr_bridge.html).
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Optional, Union

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..deps import get_db, require_device, require_wearer

router = APIRouter(tags=["heart rate"])

MAX_SKEW = timedelta(minutes=5)
# Independent readings (research 1.13): the Apple Watch gives a new heart-rate value about every
# 5 s (Apple Support 120277) while HeartCast notifies about once a second (Bluetooth Heart Rate
# Service: "approximately 1 time per second"), so most notifications repeat the last value. A
# minimum-sample check counts at most one reading per 5-second slot, never notifications.
READING_SLOT_S = 5
MIN_READINGS = 5
# Display only (research 1.12): "HR stale" after this long with no sample. The check-in schedule
# never waits on heart rate; after hr_lost_s without it the check-in is brought forward instead.
HR_STALE_S = 15
PAGES = Path(__file__).resolve().parent.parent / "pages"


@router.get("/hr-bridge", response_class=HTMLResponse)
async def hr_bridge_page():
    """Chrome Web Bluetooth bridge: Apple Watch (HeartCast) heart rate -> POST /v1/hr.

    Open it at http://localhost:8000/hr-bridge (localhost counts as a secure context).
    """
    return HTMLResponse((PAGES / "hr_bridge.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


# BLE Heart Rate Service RR intervals are uint16 in 1/1024 s: at most 64 s.
RrMs = Annotated[float, Field(ge=0, le=65535)]


class HrSample(BaseModel):
    t: Optional[Union[float, str]] = None
    ts: Optional[Union[float, str]] = None
    bpm: int = Field(ge=20, le=250)
    rr_ms: Optional[list[RrMs]] = None
    rr_synthetic: Optional[bool] = None


class HrIn(BaseModel):
    user_id: Optional[str] = None
    source: str = Field(min_length=1, max_length=64)
    simulated: bool = False
    samples: Optional[list[HrSample]] = Field(default=None, max_length=600)
    ts: Optional[Union[float, str]] = None
    t: Optional[Union[float, str]] = None
    bpm: Optional[int] = Field(default=None, ge=20, le=250)
    rr_ms: Optional[list[RrMs]] = None
    rr_synthetic: Optional[bool] = None


class CalibrateIn(BaseModel):
    window_s: int = Field(default=300, ge=10, le=3600)
    allow_simulated: bool = False


def _parse_time(value, now: datetime) -> datetime:
    """Client time if plausible, else server time (server time is the only clock)."""
    t: Optional[datetime] = None
    try:
        if isinstance(value, (int, float)):
            secs = value / 1000.0 if value > 1e11 else float(value)
            t = datetime.fromtimestamp(secs, tz=timezone.utc)
        elif isinstance(value, str) and value:
            t = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        t = None
    if t is None or abs(t - now) > MAX_SKEW:
        return now
    return t


async def _hr_user(request: Request, conn, body_user: Optional[str]) -> str:
    if request.headers.get("x-device-id"):
        dev = await require_device(request, conn)
        user_id = str(dev["user_id"])
    else:
        user_id = str((await require_wearer(request, conn))["id"])
    if body_user and body_user != user_id:
        raise HTTPException(403, "user_id does not match the credential")
    return user_id


@router.post("/v1/hr")
async def ingest_hr(request: Request, body: HrIn):
    db = get_db(request)
    samples = list(body.samples or [])
    if not samples and body.bpm is not None:
        samples = [HrSample(t=body.t, ts=body.ts, bpm=body.bpm, rr_ms=body.rr_ms, rr_synthetic=body.rr_synthetic)]
    if not samples:
        raise HTTPException(422, "no samples")
    async with db.connection() as conn:
        async with conn.transaction():
            user_id = await _hr_user(request, conn, body.user_id)
            now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
            rows = [(_parse_time(s.t if s.t is not None else s.ts, now), user_id, body.source, body.simulated,
                     s.bpm, s.rr_ms, s.rr_synthetic) for s in samples]
            async with conn.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO hr_samples (time, user_id, source, simulated, bpm, rr_ms, rr_synthetic)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""", rows)
    return {"ok": True, "inserted": len(rows)}


@router.get("/v1/hr/status")
async def hr_status(request: Request):
    db = get_db(request)
    async with db.connection() as conn:
        user_id = await _hr_user(request, conn, None)
        row = await (await conn.execute(
            """SELECT now() AS now, max(time) AS last FROM hr_samples WHERE user_id = %s""", (user_id,))).fetchone()
        sources = await (await conn.execute(
            """SELECT source, count(*) AS n, max(time) AS last, bool_or(simulated) AS simulated FROM hr_samples
                WHERE user_id = %s AND time > now() - interval '60 seconds'
                GROUP BY source ORDER BY max(time) DESC""", (user_id,))).fetchall()
    last = row["last"]
    age = (row["now"] - last).total_seconds() if last else None
    stale = age is None or age > HR_STALE_S
    label = "no heart rate yet" if last is None else ("HR stale" if stale else "HR live")
    return {"label": label, "stale": stale, "stale_after_s": HR_STALE_S, "display_only": True,
            "last_at": last.isoformat() if last else None, "age_s": round(age, 1) if age is not None else None,
            "sources": [{"source": r["source"], "samples_60s": r["n"], "last_at": r["last"].isoformat(),
                         "simulated": bool(r["simulated"])} for r in sources],
            "duplicate_sources": len(sources) > 1,
            "note": ("Display only. Stale or missing heart rate never delays a check-in; without heart rate "
                     "for the profile's hr_lost_s the next check-in is brought forward.")}


@router.post("/v1/hr/calibrate")
async def calibrate(request: Request, body: Optional[CalibrateIn] = None):
    """Seated baseline before going out: median bpm over the last window_s seconds."""
    body = body or CalibrateIn()
    db = get_db(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            cur = await conn.execute(
                """SELECT bpm, simulated, floor(extract(epoch FROM time) / %s)::bigint AS slot FROM hr_samples
                    WHERE user_id = %s AND time > now() - make_interval(secs => %s)
                      AND (%s OR NOT simulated)""",
                (READING_SLOT_S, user["id"], float(body.window_s), body.allow_simulated))
            rows = await cur.fetchall()
            by_slot: dict[int, list[int]] = {}
            for r in rows:
                by_slot.setdefault(r["slot"], []).append(r["bpm"])
            if len(by_slot) < MIN_READINGS:
                raise HTTPException(409, f"need at least {MIN_READINGS} independent readings (one per "
                                         f"{READING_SLOT_S} s) in the last {body.window_s} s, have {len(by_slot)} "
                                         f"from {len(rows)} samples")
            # One value per slot, so a burst of repeated notifications weighs like one reading.
            baseline = float(statistics.median(statistics.median(v) for v in by_slot.values()))
            sim = any(r["simulated"] for r in rows)
            await conn.execute(
                """INSERT INTO user_baselines (user_id, kind, value, simulated, computed_at)
                   VALUES (%s, 'hr_rest', %s, %s, now())
                   ON CONFLICT (user_id, kind) DO UPDATE
                   SET value = EXCLUDED.value, simulated = EXCLUDED.simulated, computed_at = now()""",
                (user["id"], baseline, sim))
    return {"ok": True, "baseline_bpm": baseline, "samples": len(rows), "readings": len(by_slot), "simulated": sim}
