"""Heart-rate ingest. Pluggable: any source can post here (HeartCast via the
Chrome BLE bridge, a streaming-service API, an Apple Health Shortcut, the
simulator). Rules read hr_samples; they can only bring a check-in forward.

POST /v1/hr  {user_id?, source, simulated, samples:[{t, bpm, rr_ms[], rr_synthetic}]}
             or a single sample {ts, bpm, rr_ms, rr_synthetic, source, simulated}
Auth: the device key (Authorization + X-Device-Id) or the wearer key.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..deps import get_db, require_device, require_wearer

router = APIRouter(tags=["heart rate"])

MAX_SKEW = timedelta(minutes=5)


class HrSample(BaseModel):
    t: Optional[Union[float, str]] = None
    ts: Optional[Union[float, str]] = None
    bpm: int = Field(ge=20, le=250)
    rr_ms: Optional[list[float]] = None
    rr_synthetic: Optional[bool] = None


class HrIn(BaseModel):
    user_id: Optional[str] = None
    source: str = Field(min_length=1, max_length=64)
    simulated: bool = False
    samples: Optional[list[HrSample]] = Field(default=None, max_length=600)
    ts: Optional[Union[float, str]] = None
    t: Optional[Union[float, str]] = None
    bpm: Optional[int] = Field(default=None, ge=20, le=250)
    rr_ms: Optional[list[float]] = None
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


@router.post("/v1/hr/calibrate")
async def calibrate(request: Request, body: Optional[CalibrateIn] = None):
    """Seated baseline before going out: median bpm over the last window_s seconds."""
    body = body or CalibrateIn()
    db = get_db(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user = await require_wearer(request, conn)
            cur = await conn.execute(
                """SELECT bpm, simulated FROM hr_samples
                    WHERE user_id = %s AND time > now() - make_interval(secs => %s)
                      AND (%s OR NOT simulated)""",
                (user["id"], float(body.window_s), body.allow_simulated))
            rows = await cur.fetchall()
            if len(rows) < 5:
                raise HTTPException(409, f"need at least 5 samples in the last {body.window_s} s, have {len(rows)}")
            baseline = float(statistics.median(r["bpm"] for r in rows))
            sim = any(r["simulated"] for r in rows)
            await conn.execute(
                """INSERT INTO user_baselines (user_id, kind, value, simulated, computed_at)
                   VALUES (%s, 'hr_rest', %s, %s, now())
                   ON CONFLICT (user_id, kind) DO UPDATE
                   SET value = EXCLUDED.value, simulated = EXCLUDED.simulated, computed_at = now()""",
                (user["id"], baseline, sim))
    return {"ok": True, "baseline_bpm": baseline, "samples": len(rows), "simulated": sim}
