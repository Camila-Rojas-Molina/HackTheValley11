"""Scrunchie telemetry ingest and the dashboard's time-series reads (Tiger Data layer).

POST /v1/telemetry/batch        raw accelerometer + FSR rows, stored with COPY
     auth: device key (Authorization + X-Device-Id) or wearer key
     body: {rows: [[t, ax_mg, ay_mg, az_mg, fsr_a_mv?, fsr_b_mv?], ...]   (1..6000 rows)
            clock: "device_ms" (default; t = the R line's ms counter) | "epoch" (Unix s or ms),
            device_ms_now?: device counter at send time (default: the last row's t),
            profile: "night_out" (default; raw kept 48 h) | "care" (raw kept 30 days),
            simulated: false, batch_id?: retry key (repeated by the same sender, stored once),
            device_id?: with the wearer key, one of the wearer's own devices}
     reply: {ok, duplicate, inserted, dropped}   (dropped = rows outside now - 48 h .. now + 60 s)
GET  /v1/telemetry/zscore       last 5 min vs the user's own 7-day baseline, per metric
GET  /v1/telemetry/aggregates   ?series=hr|motion&resolution=1m|1h|1d&hours=N
     reads: wearer key (own data) or X-Admin-Key (?user_id=; zscore without it lists every user,
     aggregates without it use the demo wearer). Contact tokens have no access.
GET  /v1/telemetry/gaps         ?hours=N (<= 168): "device sent no data" ranges for the care profile (Toolkit
                                heartbeat_agg -> dead_ranges, or the same computed with lead()); read-only
GET  /v1/telemetry/info         (X-Admin-Key) TimescaleDB or plain path, versions, rollups, jobs
POST /v1/telemetry/refresh      (X-Admin-Key) {hours <= 47}: materialize the rollups for a backfill
POST /v1/telemetry/compression-demo  (X-Admin-Key) {user_id?, days, motion_every_s, hr_every_s}: backdated
                                SYNTHETIC care-profile telemetry converted with convert_to_columnstore; returns
                                the measured before/after bytes and ratio (TimescaleDB only; research 4.2)
POST /v1/telemetry/synthetic    (X-Admin-Key) {user_id?, days, profile, ...}: labelled SYNTHETIC
                                baseline so the z-score has 7 days to compare against. Demo
                                databases only: it refreshes its whole range for every user, which
                                erases 'night_out' summaries there whose raw rows are already pruned.

Nothing here can change a check-in session: these routes store and read numbers only.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import timeseries
from ..db import connection_budget
from ..deps import get_db, require_admin, require_device, require_wearer

router = APIRouter(tags=["telemetry"])


class BatchIn(BaseModel):
    rows: list[list[Optional[float]]] = Field(min_length=1, max_length=timeseries.MAX_ROWS)
    clock: Literal["device_ms", "epoch"] = "device_ms"
    device_ms_now: Optional[float] = None
    profile: Literal["night_out", "care"] = "night_out"
    simulated: bool = False
    batch_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    user_id: Optional[str] = None
    device_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class RefreshIn(BaseModel):
    # Under 48 h: 'night_out' raw rows exist for that long, so this can never erase summaries
    # whose raw rows are already pruned. /v1/telemetry/synthetic refreshes its own older range.
    hours: float = Field(default=46.0, gt=0, le=47)


class CompressionDemoIn(BaseModel):
    user_id: Optional[uuid.UUID] = None
    days: float = Field(default=3.0, ge=1.0, le=6.0)
    motion_every_s: float = Field(default=1.0, ge=0.5, le=10.0)
    hr_every_s: float = Field(default=5.0, ge=1.0, le=60.0)


class SyntheticIn(BaseModel):
    user_id: Optional[uuid.UUID] = None
    days: float = Field(default=7.0, gt=0, le=7)
    until_minutes_ago: float = Field(default=5.0, ge=0, le=24 * 60)
    profile: Literal["night_out", "care"] = "night_out"
    hr_every_s: float = Field(default=10.0, ge=1, le=600)
    motion_every_s: float = Field(default=1.0, ge=0.5, le=600)


async def _writer(request: Request, conn, body: BatchIn) -> tuple[str, Optional[str]]:
    """(user_id, device_id) for an ingest call."""
    if request.headers.get("x-device-id"):
        dev = await require_device(request, conn)
        user_id, device_id = str(dev["user_id"]), dev["id"]
        if body.device_id and body.device_id != device_id:
            raise HTTPException(403, "device_id does not match the credential")
    else:
        user_id = str((await require_wearer(request, conn))["id"])
        device_id = None
        if body.device_id:
            row = await (await conn.execute("SELECT user_id FROM devices WHERE id = %s",
                                            (body.device_id,))).fetchone()
            if row is None or str(row["user_id"]) != user_id:
                raise HTTPException(403, "device_id is not one of this wearer's devices")
            device_id = body.device_id
    if body.user_id and body.user_id != user_id:
        raise HTTPException(403, "user_id does not match the credential")
    return user_id, device_id


async def _reader(request: Request, conn, user_id: Optional[uuid.UUID]) -> Optional[str]:
    """Wearer: their own id. Operator (X-Admin-Key): the requested user, or None for all."""
    if request.headers.get("x-admin-key") is not None:
        require_admin(request)
        return str(user_id) if user_id else None
    me = str((await require_wearer(request, conn))["id"])
    if user_id is not None and str(user_id) != me:
        raise HTTPException(403, "a wearer can read only their own data")
    return me


async def _demo_user_id(conn) -> str:
    row = await (await conn.execute("SELECT id FROM users WHERE is_demo ORDER BY created_at LIMIT 1")).fetchone()
    if row is None:
        raise HTTPException(422, "user_id is required (no demo wearer)")
    return str(row["id"])


@router.post("/v1/telemetry/batch")
async def ingest_batch(request: Request, body: BatchIn, background: BackgroundTasks):
    db = get_db(request)
    async with db.connection() as conn:
        async with conn.transaction():
            user_id, device_id = await _writer(request, conn, body)
            now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
            try:
                prepared = timeseries.prepare_rows(
                    body.rows, clock=body.clock, now=now, user_id=user_id, device_id=device_id,
                    profile=body.profile, simulated=body.simulated, device_ms_now=body.device_ms_now)
            except timeseries.BatchError as exc:
                raise HTTPException(422, str(exc)) from None
            result = await timeseries.ingest(conn, prepared, user_id=user_id, device_id=device_id,
                                             batch_id=body.batch_id)
    background.add_task(timeseries.maybe_prune, db)
    return result


@router.get("/v1/telemetry/zscore")
async def zscore(request: Request, user_id: Optional[uuid.UUID] = None):
    async with get_db(request).connection() as conn:
        who = await _reader(request, conn, user_id)
        setup = await timeseries.setup(conn)
        rows = await timeseries.zscores(conn, who)
    return {"mode": setup["mode"], "method": setup["zscore_method"],
            "window": "last 5 min vs the 7 days before, as 1-minute means",
            "note": timeseries.NOTE, "rows": rows}


@router.get("/v1/telemetry/aggregates")
async def aggregates(request: Request, series: Literal["hr", "motion"] = "hr",
                     resolution: Literal["1m", "1h", "1d"] = "1m",
                     hours: float = Query(default=6.0, gt=0), user_id: Optional[uuid.UUID] = None):
    if hours > timeseries.MAX_HOURS[resolution]:
        raise HTTPException(422, f"hours must be at most {timeseries.MAX_HOURS[resolution]:g} at {resolution}")
    async with get_db(request).connection() as conn:
        who = await _reader(request, conn, user_id)
        if who is None:
            who = await _demo_user_id(conn)
        setup = await timeseries.setup(conn)
        rows = await timeseries.aggregates(conn, series=series, resolution=resolution, user_id=who, hours=hours)
    return {"mode": setup["mode"], "series": series, "resolution": resolution, "user_id": who,
            "buckets": rows}


@router.get("/v1/telemetry/gaps")
async def gaps(request: Request, hours: float = Query(default=24.0, gt=0, le=timeseries.GAP_MAX_HOURS),
               user_id: Optional[uuid.UUID] = None):
    async with get_db(request).connection() as conn:
        who = await _reader(request, conn, user_id)
        if who is None:
            who = await _demo_user_id(conn)
        out = await timeseries.device_gaps(conn, who, hours=hours)
    return {"user_id": who, **out}


@router.get("/v1/telemetry/info")
async def telemetry_info(request: Request):
    require_admin(request)
    async with get_db(request).connection() as conn:
        out = await timeseries.info(conn)
        server_max = (await (await conn.execute("SHOW max_connections")).fetchone())["max_connections"]
    # Tiger free services have 25 connections and no pooler (research 1.16): show the budget.
    out["connections"] = {**connection_budget(request.app.state.ctx.settings),
                          "server_max_connections": int(server_max)}
    return out


@router.post("/v1/telemetry/refresh")
async def telemetry_refresh(request: Request, body: Optional[RefreshIn] = None):
    """Materialize the rollups for the last `hours` (TimescaleDB). Policies do this on their own
    for new data; this is for a backfill that landed behind the refresh window."""
    require_admin(request)
    body = body or RefreshIn()
    async with get_db(request).connection() as conn:   # autocommit, no transaction: refresh commits
        now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
        start = now - timedelta(hours=body.hours)
        done = await timeseries.refresh(conn, start, now)
        mode = (await timeseries.setup(conn))["mode"]
    return {"ok": True, "mode": mode, "refreshed": done, "from": start.isoformat(), "to": now.isoformat()}


@router.post("/v1/telemetry/compression-demo")
async def telemetry_compression_demo(request: Request, body: Optional[CompressionDemoIn] = None):
    """research 4.2: a real compression ratio. Autocommit connection: convert_to_columnstore is a procedure."""
    require_admin(request)
    body = body or CompressionDemoIn()
    async with get_db(request).connection() as conn:
        user_id = str(body.user_id) if body.user_id else await _demo_user_id(conn)
        if body.user_id and not await (await conn.execute("SELECT 1 FROM users WHERE id = %s",
                                                          (user_id,))).fetchone():
            raise HTTPException(404, "no such user")
        return await timeseries.compression_demo(conn, user_id, days=body.days, motion_every_s=body.motion_every_s,
                                                 hr_every_s=body.hr_every_s)


@router.post("/v1/telemetry/synthetic")
async def telemetry_synthetic(request: Request, body: Optional[SyntheticIn] = None):
    """Seed a SYNTHETIC (simulated = true) baseline for one user, then refresh that range."""
    require_admin(request)
    body = body or SyntheticIn()
    async with get_db(request).connection() as conn:
        user_id = str(body.user_id) if body.user_id else await _demo_user_id(conn)
        if body.user_id and not await (await conn.execute("SELECT 1 FROM users WHERE id = %s",
                                                          (user_id,))).fetchone():
            raise HTTPException(404, "no such user")
        now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
        end = now - timedelta(minutes=body.until_minutes_ago)
        start = end - timedelta(days=body.days)
        async with conn.transaction():
            counts = await timeseries.seed_synthetic(conn, user_id, start=start, end=end, profile=body.profile,
                                                     hr_every_s=body.hr_every_s,
                                                     motion_every_s=body.motion_every_s)
        done = await timeseries.refresh(conn, start, end)
    return {"ok": True, "user_id": user_id, "from": start.isoformat(), "to": end.isoformat(),
            "profile": body.profile, **counts, "refreshed": done}
