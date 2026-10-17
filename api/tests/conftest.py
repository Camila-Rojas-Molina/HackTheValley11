"""Shared fixtures for api/tests. New in Push 1: the StillHere conftest imported modules Push 1 deleted.

Database tests use a private Postgres from the `pgserver` pip package (no system install), or the server
named by RB_TEST_PG_URI (a CI service container, a TimescaleDB container). Every name is per worktree, so
runs in other worktrees, or a second runner in this repo, never drop each other's database (PLAN 3.4):
- database: rb_test_<first 8 hex characters of sha256(worktree path)>
- PGDATA:   <worktree>/.pgdata/   (gitignored; never a shared parent directory)
With neither pgserver nor RB_TEST_PG_URI the database tests skip and the unit tests still run.

Markers (pytest.ini) are applied here: a test that uses `pg_base_uri` (directly or through `client`) is
`db`; FILE_MARKS adds the file's own markers; a test with neither `db` nor a live marker is `unit`.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg.conninfo import make_conninfo

from app.config import Settings

WORKTREE = Path(__file__).resolve().parents[2]
ADMIN_KEY = "test-admin-key"


def db_name_for(worktree: Path) -> str:
    """The test database name for a checkout at `worktree`."""
    digest = hashlib.sha256(str(Path(worktree).resolve()).encode("utf-8")).hexdigest()
    return f"rb_test_{digest[:8]}"


def pgdata_for(worktree: Path) -> Path:
    """The pgserver data directory for a checkout at `worktree`: inside it, never shared."""
    return Path(worktree).resolve() / ".pgdata"


TEST_DB = db_name_for(WORKTREE)
PGDATA = pgdata_for(WORKTREE)


def server_uri() -> Optional[str]:
    uri = os.environ.get("RB_TEST_PG_URI")
    if uri:
        return uri
    try:
        import pgserver
    except ImportError:
        return None
    return pgserver.get_server(str(PGDATA)).get_uri()


def fresh_database(base_uri: str, name: str) -> str:
    """Drops and recreates database `name` on the server at `base_uri`; returns its URI."""
    with psycopg.connect(base_uri, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')
    return make_conninfo(base_uri, dbname=name)


# --- markers ------------------------------------------------------------------------------------
FILE_MARKS = {
    "test_contract.py": ("contract",),
}
LIVE = {"ops", "chaos", "docker", "browser", "wiring", "e2e"}


def pytest_collection_modifyitems(config, items):
    for item in items:
        names = set(FILE_MARKS.get(item.path.name, ()))
        names |= {m.name for m in item.iter_markers()} & LIVE
        if "pg_base_uri" in getattr(item, "fixturenames", ()):
            names.add("db")
        if names & LIVE:
            names.add("slow")
        elif "db" not in names:
            names.add("unit")
        for name in sorted(names):
            item.add_marker(getattr(pytest.mark, name))


# --- fixtures -----------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def pg_base_uri() -> str:
    try:
        uri = server_uri()
    except Exception as exc:  # pragma: no cover - environment specific
        pytest.skip(f"no Postgres for database tests: {exc}")
    if not uri:
        pytest.skip("no Postgres for database tests (install pgserver or set RB_TEST_PG_URI)")
    return uri


@pytest.fixture(scope="session")
def db_url(pg_base_uri) -> str:
    return fresh_database(pg_base_uri, TEST_DB)


@pytest.fixture(scope="session")
def settings(db_url) -> Settings:
    return Settings(database_url=db_url, admin_key=ADMIN_KEY, run_workers=False, auto_migrate=True,
                    demo_seed=False)


@pytest_asyncio.fixture(scope="session")
async def app(settings):
    from app.main import create_app
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture(scope="session")
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
