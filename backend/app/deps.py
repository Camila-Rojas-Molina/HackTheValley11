"""Request helpers: auth for device / wearer / operator, canonical JSON replies."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import Response

from .engine import Ctx
from .security import same, sha256_hex


def canonical_json(data: dict, status_code: int = 200) -> Response:
    """Deterministic bytes (sorted keys, no spaces) so equal replies are byte-identical."""
    body = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return Response(content=body.encode("utf-8"), status_code=status_code, media_type="application/json")


def get_db(request: Request):
    return request.app.state.db


def get_ctx(request: Request) -> Ctx:
    ctx: Ctx = request.app.state.ctx
    if not ctx.secret:
        raise HTTPException(503, "database unavailable")
    return ctx


def _bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


async def require_device(request: Request, conn) -> dict:
    key = _bearer(request)
    device_id = request.headers.get("x-device-id")
    if not key or not device_id:
        raise HTTPException(401, "device key and X-Device-Id required")
    row = await (await conn.execute("SELECT * FROM devices WHERE id = %s", (device_id,))).fetchone()
    if row is None or not same(row["device_key_hash"], sha256_hex(key)):
        raise HTTPException(401, "unknown device or wrong key")
    return row


async def require_wearer(request: Request, conn) -> dict:
    key = _bearer(request)
    if not key:
        raise HTTPException(401, "wearer key required")
    row = await (await conn.execute("SELECT * FROM users WHERE api_key_hash = %s", (sha256_hex(key),))).fetchone()
    if row is None:
        raise HTTPException(401, "wrong wearer key")
    return row


def require_admin(request: Request) -> None:
    ctx: Ctx = request.app.state.ctx
    expected = ctx.settings.admin_key
    if not expected:
        raise HTTPException(503, "operator endpoints are disabled: ADMIN_KEY is not set")
    if not same(request.headers.get("x-admin-key"), expected):
        raise HTTPException(401, "wrong X-Admin-Key")
