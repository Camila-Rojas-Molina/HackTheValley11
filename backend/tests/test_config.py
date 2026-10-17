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
