"""Shared fixtures for api/tests. New in Push 1: the StillHere conftest imported modules Push 1 deleted.

Database tests use a private Postgres from the `pgserver` pip package (no system install), or the server
named by RB_TEST_PG_URI (a CI service container, a TimescaleDB container). Every name is per worktree, so
runs in other worktrees, or a second runner in this repo, never drop each other's database (PLAN 3.4):
- database: rb_test_<first 8 hex characters of sha256(worktree path)>
- PGDATA:   <worktree>/.pgdata/   (gitignored; never a shared parent directory)
With neither pgserver nor RB_TEST_PG_URI the database tests skip and the unit tests still run.

TimescaleDB tests (`tsdb_base_uri`) use the server in RB_TEST_TSDB_URI (CI: a timescale/timescaledb:latest-pg16
service container), or start this worktree's own throwaway container rb-tsdb-<hash> on a free 127.0.0.1 port
with Docker ("${DOCKER:-$HOME/.docker/bin/docker}") and remove it at the end (RB_TSDB_KEEP=1 keeps it). Never
Tiger Cloud: the schema reaches it only after G1.

Markers (pytest.ini) are applied here: a test that uses `pg_base_uri` (directly or through `client`) is
`db`; one that uses `tsdb_base_uri` is `tsdb`; FILE_MARKS adds the file's own markers; a test with none of
`db`, `tsdb` or a live marker is `unit`.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import subprocess
import time
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
        if "tsdb_base_uri" in getattr(item, "fixturenames", ()):
            names.add("tsdb")
        if names & LIVE:
            names.add("slow")
        elif not names & {"db", "tsdb"}:
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
def storage_dirs(tmp_path_factory) -> tuple[Path, Path]:
    """(storage_dir, demo_dir) for the session app: empty temporary folders, so asset tests are hermetic."""
    root = tmp_path_factory.mktemp("rb-assets")
    (root / "storage").mkdir()
    (root / "demo").mkdir()
    return root / "storage", root / "demo"


@pytest.fixture(scope="session")
def settings(db_url, storage_dirs) -> Settings:
    return Settings(database_url=db_url, admin_key=ADMIN_KEY, auto_migrate=True, demo_seed=True,
                    storage_dir=storage_dirs[0], demo_dir=storage_dirs[1], max_upload_mb=1)


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


# --- TimescaleDB (throwaway, per worktree) -------------------------------------------------------
TSDB_IMAGE = "timescale/timescaledb:latest-pg16"
TSDB_CONTAINER = "rb-tsdb-" + hashlib.sha256(str(WORKTREE.resolve()).encode("utf-8")).hexdigest()[:8]


def _docker() -> Optional[str]:
    d = os.environ.get("DOCKER") or str(Path.home() / ".docker" / "bin" / "docker")
    return d if (Path(d).exists() or shutil.which(d)) else None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(uri: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            with psycopg.connect(uri, connect_timeout=2) as c:
                c.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(scope="session")
def tsdb_base_uri():
    uri = os.environ.get("RB_TEST_TSDB_URI")
    if uri:
        yield uri
        return
    docker = _docker()
    if docker is None or subprocess.run([docker, "info"], capture_output=True).returncode != 0:
        pytest.skip("no TimescaleDB for tsdb tests (set RB_TEST_TSDB_URI or start Docker)")
    port_out = subprocess.run([docker, "port", TSDB_CONTAINER, "5432/tcp"], capture_output=True, text=True)
    started = False
    if port_out.returncode == 0 and port_out.stdout.strip():
        port = int(port_out.stdout.strip().splitlines()[0].rsplit(":", 1)[1])   # reuse this worktree's container
    else:
        subprocess.run([docker, "rm", "-f", TSDB_CONTAINER], capture_output=True)
        port = _free_port()
        subprocess.run([docker, "run", "-d", "--name", TSDB_CONTAINER, "-p", f"127.0.0.1:{port}:5432",
                        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", TSDB_IMAGE], check=True, capture_output=True)
        started = True
    uri = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    _wait_ready(uri)
    try:
        yield uri
    finally:
        if started and os.environ.get("RB_TSDB_KEEP") != "1":
            subprocess.run([docker, "rm", "-f", TSDB_CONTAINER], capture_output=True)
