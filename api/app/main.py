"""RainbowBone API (FastAPI).

The lifespan opens the database pool and applies migrations (AUTO_MIGRATE). The routes are mounted under
/api/v1 (contracts/openapi.json). Run with:

    uvicorn app.main:app --host 0.0.0.0 --port 8000      (from api/)
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from typing import Optional

import math

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import DataError, OperationalError
from psycopg_pool import PoolTimeout

from .ai.chain import Chain
from .config import Settings, get_settings
from .db import Database
from .deps import Ctx
from .routes import alerts, audit, copilot, health, jobs, patients, scans, series, summary, tiger
from .schemas import API_TITLE, CONTRACT_VERSION

log = logging.getLogger("stillhere")


def _quiet_http_logs() -> None:
    # httpx logs request URLs at INFO, and Bot API URLs contain the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


# Capability tokens travel in URL paths (/c/{token}, /v1/contact/{token}/..., /l/{token}).
_TOKEN_PATH = re.compile(r"(/(?:c|l|v1/contact)/)[^/?#\s\"]+")


class RedactTokens(logging.Filter):
    """Replaces those path tokens with *** in log records (uvicorn's access log in particular)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_TOKEN_PATH.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = _TOKEN_PATH.sub(r"\1***", record.msg)
        return True


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _redact_access_log() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactTokens) for f in access.filters):
        access.addFilter(RedactTokens())


async def _db_init(app: FastAPI, settings: Settings) -> bool:
    """Migrations. False if the database is not reachable yet."""
    db: Database = app.state.db
    try:
        if settings.auto_migrate:
            from scripts.migrate import apply_migrations
            names = await asyncio.to_thread(apply_migrations, settings.database_url)
            if names:
                log.info("applied migrations: %s", ", ".join(names))
        async with db.connection() as conn:
            await conn.execute("SELECT 1")
        return True
    except Exception as exc:
        log.error("database init failed (%s); the API answers 503 until it succeeds", type(exc).__name__)
        return False


async def _db_init_retry(app: FastAPI, settings: Settings, stop: asyncio.Event) -> None:
    delay = 2.0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
            return
        except asyncio.TimeoutError:
            pass
        if await _db_init(app, settings):
            return
        delay = min(30.0, delay * 2)


def create_app(settings: Optional[Settings] = None, *, start_workers: Optional[bool] = None) -> FastAPI:
    settings = settings or get_settings()
    _redact_access_log()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # First: an unknown AI_CHAIN entry raises AIConfigError and stops startup (C4).
        app.state.ai_chain = Chain.from_env()
        _quiet_http_logs()
        app.state.db = Database(settings.database_url, max_size=settings.db_pool_max)
        app.state.ctx = Ctx(settings=settings, secret=settings.app_secret or "")
        await app.state.db.open(wait=False)
        stop = asyncio.Event()
        tasks: list[asyncio.Task] = []
        if not await _db_init(app, settings):
            tasks.append(asyncio.create_task(_db_init_retry(app, settings, stop), name="db_init_retry"))
        app.state.tasks = tasks
        try:
            yield
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.db.close()

    app = FastAPI(title=API_TITLE, version=CONTRACT_VERSION, lifespan=lifespan)

    @app.exception_handler(PoolTimeout)
    async def _pool_timeout(request: Request, exc: PoolTimeout):
        return JSONResponse({"ok": False, "error": "database unavailable"}, status_code=503)

    @app.exception_handler(OperationalError)
    async def _db_down(request: Request, exc: OperationalError):
        return JSONResponse({"ok": False, "error": "database unavailable"}, status_code=503)

    @app.exception_handler(DataError)
    async def _unstorable(request: Request, exc: DataError):
        # Input PostgreSQL cannot store (a NUL character in text, a number out of range): a client
        # error, not a 500 that a device would retry forever. The transaction is rolled back.
        return JSONResponse({"detail": "a value in the request cannot be stored (for example a NUL character "
                                       "or a number out of range)"}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        # FastAPI's own 422 echoes the input, and an input like 1e999 (infinity) cannot be
        # written as JSON: that turned a bad request into a 500.
        return JSONResponse({"detail": _json_safe(jsonable_encoder(exc.errors()))}, status_code=422)

    for module in (health, patients, series, scans, jobs, summary, alerts, copilot, tiger, audit):
        app.include_router(module.router)
    return app


def _default_app() -> FastAPI:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app(Settings.from_env())


app = _default_app()
