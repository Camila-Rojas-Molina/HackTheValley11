"""StillHere backend (FastAPI).

The lifespan opens the database pool, applies migrations (AUTO_MIGRATE), seeds
the demo wearer (DEMO_SEED) and starts three asyncio tasks: timer_worker,
outbox_sender and telegram_poller. Run with:

    uvicorn app.main:app --host 0.0.0.0 --port 8000      (from backend/)
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg import OperationalError
from psycopg_pool import PoolTimeout

from . import engine
from .config import Settings, get_settings
from .db import Database
from .engine import Ctx
from .outbox import outbox_sender
from .routes import contacts, demo, device, health, hr, sessions
from .routes import care as care_routes
from .telegram import telegram_poller
from .timers import timer_worker
from .routes import telemetry

log = logging.getLogger("stillhere")


def _quiet_http_logs() -> None:
    # httpx logs request URLs at INFO, and Bot API URLs contain the token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _db_init(app: FastAPI, settings: Settings) -> bool:
    """Migrations, the app secret and the demo seed. False if the database is not reachable yet."""
    db: Database = app.state.db
    ctx: Ctx = app.state.ctx
    try:
        if settings.auto_migrate:
            from scripts.migrate import apply_migrations
            names = await asyncio.to_thread(apply_migrations, settings.database_url)
            if names:
                log.info("applied migrations: %s", ", ".join(names))
        async with db.connection() as conn:
            ctx.secret = await engine.load_secret(conn, settings)
            if settings.demo_seed:
                async with conn.transaction():
                    await demo.ensure_demo_world(conn, ctx)
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
    run_workers = settings.run_workers if start_workers is None else start_workers

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _quiet_http_logs()
        app.state.db = Database(settings.database_url)
        app.state.ctx = Ctx(settings=settings, secret=settings.app_secret or "")
        await app.state.db.open(wait=False)
        stop = asyncio.Event()
        tasks: list[asyncio.Task] = []
        if not await _db_init(app, settings):
            tasks.append(asyncio.create_task(_db_init_retry(app, settings, stop), name="db_init_retry"))
        if run_workers:
            ctx = app.state.ctx
            tasks += [
                asyncio.create_task(timer_worker(app.state.db, ctx, stop), name="timer_worker"),
                asyncio.create_task(outbox_sender(app.state.db, ctx, stop), name="outbox_sender"),
                asyncio.create_task(telegram_poller(app.state.db, ctx, stop), name="telegram_poller"),
            ]
        app.state.tasks = tasks
        try:
            yield
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.db.close()

    app = FastAPI(title="StillHere", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(PoolTimeout)
    async def _pool_timeout(request: Request, exc: PoolTimeout):
        return JSONResponse({"ok": False, "error": "database unavailable"}, status_code=503)

    @app.exception_handler(OperationalError)
    async def _db_down(request: Request, exc: OperationalError):
        return JSONResponse({"ok": False, "error": "database unavailable"}, status_code=503)

    app.include_router(telemetry.router)
    for r in (health.router, device.router, hr.router, sessions.router, contacts.router, demo.router):
        app.include_router(r)
    app.include_router(care_routes.router)
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
