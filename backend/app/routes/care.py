"""PointClickCare senior mode endpoints. Synthetic residents only; separate from KAZE.

GET  /v1/care/residents   every synthetic resident with their latest score and level, plus the
                          generator's script (ground truth for checking the rules; never read by them)
GET  /v1/care/alerts      latest alert cards at level "worth a look this shift" or "review this shift":
                          "review this shift" first, then by score
                          (?include_low=true returns every resident's card)
                          Each flagged card's briefing is worded by Vultr Serverless
                          Inference when VULTR_API_KEY is set (app/vultr.py); briefing_source says
                          which text is shown: "vultr", or "template" (care.local_briefing).
POST /v1/care/alerts/{resident_id}/feedback   {action: useful | not_useful | already_known |
                          snooze_3d, reason?}: a button on a flagged card, stored against the
                          resident's latest card (research 1.7). snooze_3d mutes pushes for 3 days.
GET  /v1/care/feedback    counts per button.
POST /v1/demo/care-seed   X-Admin-Key. Body optional: {seed, end_day}. Replaces the synthetic cohort
                          (20 residents x 28 days, ending end_day, default yesterday UTC) and scores it.

The reads need no key because every row is synthetic (a CHECK constraint enforces it); put
them behind auth before any real resident data goes in. Card shape:
{resident_id, display_name, synthetic, score 0-100, level, rules: [{rule, observed, baseline,
unit, contribution, explanation, ...}], suggested_check, briefing, ...}. See app/care.py.

This router imports only app.care, app.security and app.vultr, so nothing here can reach the
check-in state machine, timers, outbox or Telegram. app.care decides every score and level
without a model; app.vultr only rewords a finished card's briefing.
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .. import care, vultr
from ..security import same

router = APIRouter(tags=["senior mode (synthetic)"])


class FeedbackIn(BaseModel):
    action: Literal["useful", "not_useful", "already_known", "snooze_3d"]
    reason: Optional[str] = Field(default=None, max_length=200)


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
        out = await care.list_alerts(conn, include_low=include_low)
    await word_briefings(out["alerts"])    # after the connection is back in the pool
    return out


async def word_briefings(cards: list[dict], **vultr_options) -> list[dict]:
    """Replace the briefing of each flagged card (care.FLAGGED) with Vultr's wording, in place.

    Runs on finished cards: score, level, rules and suggested check are already set and are
    not touched. vultr.abriefing never raises and never blocks the event loop the KAZE timer
    worker shares. Without a key, on any error or timeout, or when the text breaks the wording
    rules, the card keeps care.local_briefing and briefing_source is "template". "low" and
    "not enough data" cards are not sent. vultr_options (env, transport) are for tests.
    """
    worded = [c for c in cards if c.get("level") in care.FLAGGED]
    results = await asyncio.gather(*(vultr.abriefing(c, **vultr_options) for c in worded))
    for card in cards:
        card["briefing_source"] = "template"
    for card, result in zip(worded, results):
        if result["source"] == "vultr":
            card["briefing"] = result["text"]
            card["briefing_source"] = "vultr"
    return cards


@router.post("/v1/care/alerts/{resident_id}/feedback")
async def care_feedback(request: Request, resident_id: str, body: FeedbackIn):
    async with request.app.state.db.connection() as conn:
        out = await care.record_feedback(conn, resident_id, body.action, body.reason)
    if out is None:
        raise HTTPException(404, "no synthetic resident with a card under that id")
    return out


@router.get("/v1/care/feedback")
async def care_feedback_summary(request: Request):
    async with request.app.state.db.connection() as conn:
        return await care.feedback_summary(conn)


@router.post("/v1/demo/care-seed")
async def care_seed(request: Request, body: Optional[CareSeedIn] = None):
    _require_admin(request)
    body = body or CareSeedIn()
    async with request.app.state.db.connection() as conn:
        return await care.seed_synthetic(conn, seed=body.seed, end_day=body.end_day)
