"""PointClickCare senior mode endpoints. Synthetic residents only; separate from KAZE.

GET  /v1/care/residents   every synthetic resident with their latest score and level, plus the
                          generator's script (ground truth for checking the rules; never read by them)
GET  /v1/care/alerts      latest alert cards at level "watch" or "check today": "check today" first,
                          then by score
                          (?include_low=true returns every resident's card)
POST /v1/demo/care-seed   X-Admin-Key. Body optional: {seed, end_day}. Replaces the synthetic cohort
                          (20 residents x 14 days, ending end_day, default yesterday UTC) and scores it.

The reads need no key because every row is synthetic (a CHECK constraint enforces it); put
them behind auth before any real resident data goes in. Card shape:
{resident_id, display_name, synthetic, score 0-100, level, rules: [{rule, observed, baseline,
unit, contribution, explanation, ...}], suggested_check, briefing, ...}. See app/care.py.

This router imports only app.care and app.security, so nothing here can reach the
check-in state machine, timers, outbox or Telegram.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import care
from ..security import same

router = APIRouter(tags=["senior mode (synthetic)"])


class CareSeedIn(BaseModel):
    seed: int = Field(default=care.DEFAULT_SEED, ge=0, le=2 ** 31 - 1)
    end_day: Optional[date] = None


def _require_admin(request: Request) -> None:
    expected = request.app.state.ctx.settings.admin_key
    if not expected:
        raise HTTPException(503, "operator endpoints are disabled: ADMIN_KEY is not set")
    if not same(request.headers.get("x-admin-key"), expected):
        raise HTTPException(401, "wrong X-Admin-Key")


@router.get("/v1/care/residents")
async def care_residents(request: Request):
    async with request.app.state.db.connection() as conn:
        return await care.list_residents(conn)


@router.get("/v1/care/alerts")
async def care_alerts(request: Request, include_low: bool = False):
    async with request.app.state.db.connection() as conn:
        return await care.list_alerts(conn, include_low=include_low)


@router.post("/v1/demo/care-seed")
async def care_seed(request: Request, body: Optional[CareSeedIn] = None):
    _require_admin(request)
    body = body or CareSeedIn()
    async with request.app.state.db.connection() as conn:
        return await care.seed_synthetic(conn, seed=body.seed, end_day=body.end_day)
