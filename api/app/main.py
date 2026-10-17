"""RainbowBone API (FastAPI).

The lifespan builds the AI chain (a bad AI_CHAIN stops startup), opens the database pool, applies migrations
(RB_AUTO_MIGRATE) and seeds the demo patient (RB_DEMO_SEED). Until that succeeds the database routes answer
503, and a background task retries. No job ever runs in this process: the worker is a separate process (C5).
The routes are mounted under /api/v1 (contracts/openapi.json). Run with:

    uvicorn app.main:app --host 0.0.0.0 --port 8000      (from api/)
"""
from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import DataError, OperationalError
from psycopg_pool import PoolTimeout

from .ai.chain import Chain
from .config import Settings, get_settings
from .db import Database
from .deps import AdminDisabled, DatabaseNotReady
from .routes import alerts, audit, copilot, health, jobs, patients, scans, series, summary, tiger
from .schemas import API_TITLE, CONTRACT_VERSION
from .storage import TooLarge

log = logging.getLogger("rainbowbone")

UNAVAILABLE = {"ok": False, "error": "database unavailable"}


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _apply(settings: Settings) -> list[str]:
    """Migrations, then the demo seed (blocking; run in a thread)."""
    names: list[str] = []
    if (settings.auto_migrate or settings.demo_seed) and not settings.schema_writes_allowed:
        log.error("refusing to migrate or seed a database that is not on this machine: the schema reaches "
                  "Tiger Cloud only after G1 (RB_ALLOW_REMOTE_SCHEMA=1). To serve an already migrated "
                  "database set RB_AUTO_MIGRATE=0 and RB_DEMO_SEED=0. The API answers 503 meanwhile.")
        raise PermissionError("remote schema writes are off")
    if settings.auto_migrate:
        from scripts.migrate import apply_migrations
        names = apply_migrations(settings.database_url)
    if settings.demo_seed:
        from scripts.seed_demo import seed
        seed(settings.database_url)
    return names


async def _db_init(app: FastAPI, settings: Settings) -> bool:
    """Migrations and seed. False if the database is not reachable yet."""
    db: Database = app.state.db
    try:
        names = await asyncio.to_thread(_apply, settings)
        if names:
            log.info("applied migrations: %s", ", ".join(names))
        async with db.connection() as conn:
            await conn.execute("SELECT 1")
        app.state.db_ready = True
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
    """`start_workers` is accepted for older callers and ignored: the API process runs no workers."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # First: an unknown AI_CHAIN entry raises AIConfigError and stops startup (C4).
        app.state.ai_chain = Chain.from_env()
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        app.state.db = Database(settings.database_url, max_size=settings.db_pool_max)
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
    app.state.settings = settings
    app.state.db_ready = False
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=False,
                       allow_methods=["GET", "POST"], allow_headers=["content-type", "x-admin-key"])

    @app.exception_handler(PoolTimeout)
    async def _pool_timeout(request: Request, exc: PoolTimeout):
        return JSONResponse(UNAVAILABLE, status_code=503)

    @app.exception_handler(OperationalError)
    async def _db_down(request: Request, exc: OperationalError):
        return JSONResponse(UNAVAILABLE, status_code=503)

    @app.exception_handler(DatabaseNotReady)
    async def _db_not_ready(request: Request, exc: DatabaseNotReady):
        return JSONResponse(UNAVAILABLE, status_code=503)

    @app.exception_handler(DataError)
    async def _unstorable(request: Request, exc: DataError):
        # Input PostgreSQL cannot store (a NUL character in text, a number out of range): a client error,
        # not a 500. The transaction is rolled back.
        return JSONResponse({"detail": "a value in the request cannot be stored (for example a NUL character "
                                       "or a number out of range)"}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        # FastAPI's own 422 echoes the input, and an input like 1e999 (infinity) cannot be written as JSON.
        return JSONResponse({"detail": _json_safe(jsonable_encoder(exc.errors()))}, status_code=422)

    @app.exception_handler(AdminDisabled)
    async def _admin_disabled(request: Request, exc: AdminDisabled):
        return JSONResponse({"ok": False, "error": "admin routes are disabled: RB_ADMIN_KEY is not set"},
                            status_code=503)

    @app.exception_handler(TooLarge)
    async def _too_large(request: Request, exc: TooLarge):
        return JSONResponse({"detail": "upload over RB_MAX_UPLOAD_MB"}, status_code=413)

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
