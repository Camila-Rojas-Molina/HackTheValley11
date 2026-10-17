"""Migration tests, on plain PostgreSQL (always) and on TimescaleDB (STILLHERE_TSDB_URIS, e.g. the Docker
container that scripts/test_all.sh starts):

* every file in migrations/ applies once, in name order, recorded with its sha256; a second run applies
  nothing (the app runs the migrations at every start);
* every Timescale-specific statement is guarded: on plain PostgreSQL the same tables and views exist
  without the extension; on TimescaleDB the raw tables are hypertables and the rollups continuous aggregates;
* the KAZE core runs on the migrated schema: a no-response check-in escalates to L1 through the timer rows
  and the outbox, and the z-score view answers.
"""
from __future__ import annotations

import hashlib
import os

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from app import states
from app.config import Settings
from scripts.migrate import MIGRATIONS_DIR, apply_migrations, migration_files
from tests.conftest import ADMIN_KEY, DURESS_PIN, PIN, fire, fresh_database, make_world, outbox, session_row

TSDB_URIS = [u.strip() for u in os.environ.get("STILLHERE_TSDB_URIS", "").split(",") if u.strip()]
CORE_TABLES = ("users", "devices", "sessions", "timers", "alerts", "outbox", "audit_log", "contacts",
               "device_events", "hr_samples", "scrunchie_telemetry", "care_residents")
ROLLUPS = ("hr_1m", "hr_1h", "hr_1d", "telemetry_1m", "telemetry_1h", "telemetry_1d")


def _check_runner(url: str) -> None:
    files = [p.name for p in migration_files()]
    assert files == sorted(files) and files[0].startswith("001_")
    assert apply_migrations(url) == files
    assert apply_migrations(url) == []                                   # a restart applies nothing
    with psycopg.connect(url, row_factory=dict_row) as c:
        rows = c.execute("SELECT name, sha256 FROM schema_migrations ORDER BY name").fetchall()
        assert [r["name"] for r in rows] == files
        for r in rows:
            sql = (MIGRATIONS_DIR / r["name"]).read_text(encoding="utf-8")
            assert r["sha256"] == hashlib.sha256(sql.encode("utf-8")).hexdigest(), r["name"]
        have = {r["t"] for r in c.execute(
            "SELECT table_name AS t FROM information_schema.tables WHERE table_schema = 'public'").fetchall()}
        assert set(CORE_TABLES) <= have, set(CORE_TABLES) - have
        views = {r["v"] for r in c.execute(
            """SELECT viewname AS v FROM pg_views WHERE schemaname = 'public'
               UNION SELECT matviewname FROM pg_matviews WHERE schemaname = 'public'""").fetchall()}
        assert set(ROLLUPS) | {"user_zscore_5m"} <= views, set(ROLLUPS) - views
        assert c.execute("SELECT count(*) AS n FROM user_zscore_5m").fetchone()["n"] == 0


async def _kaze_runs_on(url: str) -> None:
    from app.main import create_app
    settings = Settings(database_url=url, admin_key=ADMIN_KEY, device_key="mig-device", wearer_key="mig-wearer",
                        demo_pin=PIN, demo_duress_pin=DURESS_PIN, app_secret="mig-secret",
                        public_base_url="https://stillhere.test", run_workers=False, auto_migrate=True,
                        demo_seed=True)
    app = create_app(settings, start_workers=False)
    async with app.router.lifespan_context(app):
        db, ctx = app.state.db, app.state.ctx
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mig") as client:
            assert (await client.get("/v1/health")).json()["db"] == "up"
            w = await make_world(client, db, contacts=1)
            for kind in (states.CHECKIN_DUE, states.PROMPT_TIMEOUT, states.REMINDER_TIMEOUT):
                await fire(db, ctx, w.session_id, kind)
            assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"
            assert [o["template"] for o in await outbox(db, w.session_id, recipient="contact")] == ["alert_no_response"]
            r = await client.post("/v1/hr", headers=w.wearer, json={"bpm": 70, "source": "migration"})
            assert r.json() == {"ok": True, "inserted": 1}
            z = await client.get("/v1/telemetry/zscore", headers=w.wearer)
            assert z.status_code == 200 and z.json()["rows"][0]["metric"] == "hr_bpm"
            return (await client.get("/v1/telemetry/info", headers={"X-Admin-Key": ADMIN_KEY})).json()


async def test_migrations_apply_once_in_order_on_plain_postgres_and_the_core_runs(pg_base_uri):
    url = fresh_database(pg_base_uri, "stillhere_migrations")
    _check_runner(url)
    with psycopg.connect(url) as c:
        if c.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone():
            pytest.skip("this server has TimescaleDB; see the tsdb variant")
    info = await _kaze_runs_on(url)
    assert info["mode"] == "plain"


@pytest.mark.skipif(not TSDB_URIS, reason="set STILLHERE_TSDB_URIS to TimescaleDB server URIs")
@pytest.mark.parametrize("server", TSDB_URIS or ["unset"])
async def test_migrations_on_timescaledb_build_hypertables_and_rollups_and_the_core_runs(server):
    url = fresh_database(server, "stillhere_migrations_tsdb")
    _check_runner(url)
    with psycopg.connect(url, row_factory=dict_row) as c:
        assert c.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
        hyper = {r["h"] for r in c.execute(
            "SELECT hypertable_name AS h FROM timescaledb_information.hypertables").fetchall()}
        assert {"hr_samples", "scrunchie_telemetry"} <= hyper
        caggs = {r["v"]: r["m"] for r in c.execute(
            "SELECT view_name AS v, materialized_only AS m FROM timescaledb_information.continuous_aggregates").fetchall()}
        assert set(ROLLUPS) <= set(caggs) and not any(caggs[v] for v in ROLLUPS)    # real-time aggregation on
        jobs = {r["p"] for r in c.execute("SELECT proc_name AS p FROM timescaledb_information.jobs").fetchall()}
        assert {"policy_refresh_continuous_aggregate", "policy_retention"} <= jobs or len(jobs) >= 3, jobs
    info = await _kaze_runs_on(url)
    assert info["mode"] == "timescaledb" and info["timescaledb"]
