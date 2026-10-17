"""Shared fixtures.

Integration tests need a Postgres server. By default they use the `pgserver`
pip package (a private local server; no system install). Set
STILLHERE_TEST_PG_URI to use another server instead, e.g. a TimescaleDB
container. Without either, DB tests are skipped and the unit tests still run.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import psycopg
import pytest
import pytest_asyncio
from psycopg.conninfo import make_conninfo

from app import engine, states, timers
from app.config import Settings
from app.outbox import Sender
from app.security import hash_pin, new_token, sha256_hex

PGDATA = Path(os.environ.get("STILLHERE_PGDATA") or Path(__file__).resolve().parents[3] / ".pgdata-stillhere")
TEST_DB = "stillhere_test"

ADMIN_KEY = "test-admin-key"
PIN, DURESS_PIN = "2468", "1357"


def server_uri() -> Optional[str]:
    uri = os.environ.get("STILLHERE_TEST_PG_URI")
    if uri:
        return uri
    try:
        import pgserver
    except ImportError:
        return None
    srv = pgserver.get_server(str(PGDATA), cleanup_mode=None)
    return srv.get_uri()


def fresh_database(base_uri: str, name: str) -> str:
    with psycopg.connect(base_uri, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{name}"')
    return make_conninfo(base_uri, dbname=name)


@pytest.fixture(scope="session")
def pg_base_uri() -> str:
    try:
        uri = server_uri()
    except Exception as exc:  # pragma: no cover - environment specific
        pytest.skip(f"no Postgres for integration tests: {exc}")
    if not uri:
        pytest.skip("no Postgres for integration tests (install pgserver or set STILLHERE_TEST_PG_URI)")
    return uri


@pytest.fixture(scope="session")
def db_url(pg_base_uri) -> str:
    return fresh_database(pg_base_uri, TEST_DB)


@pytest.fixture(scope="session")
def settings(db_url) -> Settings:
    return Settings(
        database_url=db_url, admin_key=ADMIN_KEY, device_key="demo-device-key", wearer_key="demo-wearer-key",
        demo_pin=PIN, demo_duress_pin=DURESS_PIN, app_secret="test-app-secret",
        public_base_url="https://stillhere.test", run_workers=False, auto_migrate=True, demo_seed=True,
    )


@pytest_asyncio.fixture(scope="session")
async def app(settings):
    from app.main import create_app
    application = create_app(settings, start_workers=False)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture(scope="session")
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(scope="session")
def db(app):
    return app.state.db


@pytest.fixture(scope="session")
def ctx(app):
    return app.state.ctx


@dataclass
class World:
    user_id: str
    wearer_key: str
    device_id: str
    device_key: str
    session_id: str
    contacts: list = field(default_factory=list)  # [{id, name, token}]
    pending: list = field(default_factory=list)
    boot_id: str = "b1"
    seq: int = 0

    @property
    def wearer(self) -> dict:
        return {"Authorization": f"Bearer {self.wearer_key}"}

    def device(self, seq: Optional[int] = None) -> dict:
        if seq is None:
            self.seq += 1
            seq = self.seq
        return {"Authorization": f"Bearer {self.device_key}", "X-Device-Id": self.device_id,
                "X-Boot-Id": self.boot_id, "X-Seq": str(seq)}


async def make_world(client, db, *, contacts: int = 2, approve: bool = True, pending: int = 0,
                     profile: str = "demo", name: str = "Maya", arm: bool = True, location: bool = True) -> World:
    ctx = client._transport.app.state.ctx
    wearer_key, device_key = new_token(), new_token()
    device_id = f"dev-{uuid.uuid4().hex[:10]}"
    async with db.connection() as conn:
        async with conn.transaction():
            u = await (await conn.execute(
                """INSERT INTO users (display_name, api_key_hash, pin_hash, duress_pin_hash)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (name, sha256_hex(wearer_key), hash_pin(PIN), hash_pin(DURESS_PIN)))).fetchone()
            await conn.execute("INSERT INTO devices (id, user_id, device_key_hash) VALUES (%s, %s, %s)",
                               (device_id, u["id"], sha256_hex(device_key)))
            if location:
                await engine.record_location(conn, ctx, user_id=u["id"], session_id=None, lat=43.7, lon=-79.4,
                                             accuracy_m=20.0, source="browser", simulated=False)
    w = World(user_id=str(u["id"]), wearer_key=wearer_key, device_id=device_id, device_key=device_key, session_id="")
    for i in range(contacts + pending):
        r = await client.post("/v1/contacts", json={"name": f"C{i + 1}"}, headers=w.wearer)
        assert r.status_code == 200, r.text
        inv = r.json()
        invite_token = inv["web_link"].rsplit("/", 1)[1]
        j = await client.post(f"/v1/contact/{invite_token}/join", json={})
        assert j.status_code == 200, j.text
        entry = {"id": inv["contact_id"], "name": f"C{i + 1}", "token": j.json()["contact_token"]}
        if i < contacts and approve:
            a = await client.post(f"/v1/contacts/{inv['contact_id']}/approve", headers=w.wearer)
            assert a.status_code == 200, a.text
            w.contacts.append(entry)
        else:
            w.pending.append(entry)
    if arm:
        r = await client.post("/v1/sessions", json={"profile": profile}, headers=w.wearer)
        assert r.status_code == 200, r.text
        w.session_id = r.json()["session_id"]
    return w


async def session_row(db, sid) -> dict:
    async with db.connection() as conn:
        return await (await conn.execute("SELECT * FROM sessions WHERE id = %s", (sid,))).fetchone()


async def open_alert(db, sid) -> Optional[dict]:
    async with db.connection() as conn:
        return await (await conn.execute(
            "SELECT * FROM alerts WHERE session_id = %s AND status IN ('OPEN', 'ACKED')", (sid,))).fetchone()


async def pending_timers(db, sid) -> list[dict]:
    async with db.connection() as conn:
        return list(await (await conn.execute(
            """SELECT * FROM timers WHERE session_id = %s AND fired_at IS NULL AND cancelled_at IS NULL
                ORDER BY fire_at""", (sid,))).fetchall())


async def fire(db, ctx, sid, kind: str) -> str:
    """Bring this session's pending `kind` timer forward to now and fire it."""
    async with db.connection() as conn:
        row = await (await conn.execute(
            """UPDATE timers SET fire_at = now()
                WHERE id = (SELECT id FROM timers WHERE session_id = %s AND kind = %s
                              AND fired_at IS NULL AND cancelled_at IS NULL ORDER BY fire_at LIMIT 1)
            RETURNING id""", (sid, kind))).fetchone()
        assert row is not None, f"no pending {kind} timer for {sid}"
        fired = await timers.fire_one(conn, ctx, session_id=sid)
        assert fired == row["id"], (fired, row["id"])
        res = await (await conn.execute("SELECT result FROM timers WHERE id = %s", (row["id"],))).fetchone()
    return res["result"]


async def outbox(db, sid=None, *, contact_id=None, recipient=None) -> list[dict]:
    q = "SELECT * FROM outbox WHERE true"
    args: list = []
    if sid is not None:
        q += " AND session_id = %s"
        args.append(sid)
    if contact_id is not None:
        q += " AND contact_id = %s"
        args.append(contact_id)
    if recipient is not None:
        q += " AND recipient = %s"
        args.append(recipient)
    async with db.connection() as conn:
        return list(await (await conn.execute(q + " ORDER BY id", args)).fetchall())


async def deliver_all(db, ctx, api=None) -> None:
    sender = Sender(db, ctx, api)
    for _ in range(50):
        if await sender.run_once() == 0:
            return


async def to_state(client, db, ctx, w: World, target: str) -> None:
    """Drive a fresh ARMED session to PROMPTED / REMINDED / ESCALATED_L1."""
    order = [states.PROMPTED, states.REMINDED, "ESCALATED_L1"]
    kinds = [states.CHECKIN_DUE, states.PROMPT_TIMEOUT, states.REMINDER_TIMEOUT]
    for st, kind in zip(order, kinds):
        await fire(db, ctx, w.session_id, kind)
        assert (await session_row(db, w.session_id))["state"] == st
        if st == target:
            return
    raise AssertionError(target)
