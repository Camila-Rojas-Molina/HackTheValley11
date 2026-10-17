"""Settings from the environment: the public .env.example placeholders are never live credentials."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from app.config import Settings
from tests.conftest import fresh_database

EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def test_env_example_placeholders_are_treated_as_unset():
    env = dict(dotenv_values(EXAMPLE))
    assert env["ADMIN_KEY"].startswith("change-me")  # the public file ships placeholders
    s = Settings.from_env(env)
    assert s.admin_key is None and s.device_key is None and s.wearer_key is None
    s2 = Settings.from_env({"ADMIN_KEY": "CHANGE_ME", "APP_SECRET": "change-me-secret", "WEARER_KEY": "my-own-key"})
    assert s2.admin_key is None and s2.app_secret is None and s2.wearer_key == "my-own-key"


async def test_a_server_started_from_env_example_rejects_the_placeholder_keys(pg_base_uri):
    from app.main import create_app
    env = dict(dotenv_values(EXAMPLE))
    env.update(DATABASE_URL=fresh_database(pg_base_uri, "stillhere_test_placeholders"), RUN_WORKERS="0")
    application = create_app(Settings.from_env(env), start_workers=False)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://t") as c:
            r = await c.get("/v1/admin/overview", headers={"X-Admin-Key": env["ADMIN_KEY"]})
            assert r.status_code == 503
            r = await c.get("/v1/contacts", headers={"Authorization": f"Bearer {env['WEARER_KEY']}"})
            assert r.status_code == 401
            r = await c.post("/v1/hr", json={"source": "x", "bpm": 70},
                             headers={"Authorization": f"Bearer {env['DEVICE_KEY']}", "X-Device-Id": env["DEMO_DEVICE_ID"]})
            assert r.status_code == 401


def test_access_log_lines_never_carry_capability_tokens(caplog, settings):
    import logging

    from app.main import create_app
    create_app(settings, start_workers=False)  # installs the filter, as scripts/run.py's app does
    access = logging.getLogger("uvicorn.access")
    secrets_ = ["InviteTok123", "ContactTok456", "ShareTok789", "PageTok000"]
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        for path in (f"/v1/contact/{secrets_[0]}/join", f"/v1/contact/{secrets_[1]}/status?x=1",
                     f"/l/{secrets_[2]}", f"/c/{secrets_[3]}"):
            access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", path, "1.1", 200)
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5555", "GET", "/v1/health", "1.1", 200)
    text = caplog.text
    assert not any(s in text for s in secrets_), text
    assert "/v1/contact/***/status" in text and "/l/***" in text and "/v1/health" in text


async def test_location_link_sealed_under_an_older_secret_answers_410_not_500(client, db, ctx):
    from app.security import encrypt_coords, new_token, sha256_hex
    w = await make_world_for_secret(client, db)
    token = new_token()
    async with db.connection() as conn:
        snap = await (await conn.execute(
            """INSERT INTO location_snapshots (user_id, coords_enc, source) VALUES (%s, %s, 'browser') RETURNING id""",
            (w.user_id, encrypt_coords("an-older-app-secret", 43.7, -79.4)))).fetchone()
        await conn.execute("INSERT INTO location_shares (token_hash, token_nonce, snapshot_id) VALUES (%s, 'n', %s)",
                           (sha256_hex(token), snap["id"]))
    r = await client.get(f"/l/{token}")
    assert r.status_code == 410 and "no longer available" in r.json()["detail"]


async def make_world_for_secret(client, db):
    from tests.conftest import make_world
    return await make_world(client, db, contacts=0, arm=False)


async def test_unset_app_secret_with_a_remote_database_logs_a_warning(caplog, db):
    import logging

    from app import engine
    remote = Settings(database_url="postgresql://u@db.example.tsdb.cloud.timescale.com:31234/tsdb?sslmode=require")
    local = Settings(database_url="postgresql://postgres@localhost:5432/stillhere")
    async with db.connection() as conn:
        with caplog.at_level(logging.WARNING, logger="stillhere.engine"):
            await engine.load_secret(conn, local)
            assert "APP_SECRET" not in caplog.text
            await engine.load_secret(conn, remote)
    assert "APP_SECRET is not set" in caplog.text


# --- research 1.16: a small database pool, so the API stays well under Tiger's 25 connections ---------
@pytest.mark.parametrize("raw,expected", [(None, 8), ("6", 6), ("abc", 8), ("0", 1), ("100", 20), ("", 8)])
def test_db_pool_max_comes_from_the_environment_and_is_bounded(raw, expected):
    env = {} if raw is None else {"DB_POOL_MAX": raw}
    assert Settings.from_env(env).db_pool_max == expected


def test_connection_budget_stays_well_under_a_free_tiger_service():
    from app.db import connection_budget
    budget = connection_budget(Settings.from_env({}))
    assert budget == {"pool_max": 8, "telegram_lock": 0, "migrations_at_start": 1, "total_max": 9,
                      "scripts": 0, "free_tier_max_connections": 25}
    with_bot = connection_budget(Settings.from_env({"TELEGRAM_BOT_TOKEN": "123:abc", "DB_POOL_MAX": "20"}))
    assert with_bot["total_max"] == 22 and with_bot["telegram_lock"] == 1


async def test_the_app_pool_uses_the_setting(app, settings):
    assert app.state.db.pool.max_size == settings.db_pool_max == 8


async def test_telemetry_info_reports_the_connection_budget(client):
    from tests.conftest import ADMIN_KEY
    body = (await client.get("/v1/telemetry/info", headers={"X-Admin-Key": ADMIN_KEY})).json()
    conn = body["connections"]
    assert conn["pool_max"] == 8 and conn["server_max_connections"] >= 25 and conn["total_max"] <= 12
