"""TimescaleDB-only checks: hypertables, hierarchical real-time continuous aggregates, refresh,
columnstore, retention policies and the per-profile retention job.

Needs one or more TimescaleDB servers; skipped otherwise. Example (local Docker):

  docker run -d --name stillhere-tsdb -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=<pw> \\
      timescale/timescaledb:latest-pg16                    # TimescaleDB, no Toolkit
  docker run -d --name stillhere-tsdb-ha -p 127.0.0.1:55433:5432 -e POSTGRES_PASSWORD=<pw> \\
      timescale/timescaledb-ha:pg18                        # TimescaleDB + Toolkit (as on Tiger Cloud)
  STILLHERE_TSDB_URIS=postgresql://postgres:<pw>@127.0.0.1:55432/postgres,postgresql://postgres:<pw>@127.0.0.1:55433/postgres \\
      ../.venv/bin/python -m pytest -q tests/test_timeseries_tsdb.py

Each URI gets a fresh database. Background jobs are switched off in it so the tests run them
on purpose with run_job and nothing changes underneath them.
"""
from __future__ import annotations

import os
import uuid
from datetime import timedelta

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row

from app import timeseries
from scripts.migrate import apply_migrations
from tests.conftest import fresh_database
from tests.test_timeseries import assert_view_matches, expected_rows

URIS = [u.strip() for u in os.environ.get("STILLHERE_TSDB_URIS", "").split(",") if u.strip()]
pytestmark = pytest.mark.skipif(not URIS, reason="set STILLHERE_TSDB_URIS to TimescaleDB server URIs")

CAGGS = {"hr_1m": "hr_samples", "hr_1h": "hr_1m", "hr_1d": "hr_1h",
         "telemetry_1m": "scrunchie_telemetry", "telemetry_1h": "telemetry_1m", "telemetry_1d": "telemetry_1h"}


@pytest.fixture(scope="module", params=URIS or ["unset"])
def ts_url(request):
    url = fresh_database(request.param, "stillhere_tsdb_test")
    applied = apply_migrations(url)
    assert applied[:2] == ["001_core.sql", "002_timeseries.sql"], applied
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("""SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs
                      WHERE job_id >= 1000""")
    return url


@pytest_asyncio.fixture
async def conn(ts_url):
    c = await psycopg.AsyncConnection.connect(ts_url, autocommit=True, row_factory=dict_row)
    await c.execute("SET TIME ZONE 'UTC'")
    try:
        yield c
    finally:
        await c.close()


async def _user(conn) -> str:
    row = await (await conn.execute("INSERT INTO users (display_name) VALUES ('ts') RETURNING id")).fetchone()
    return str(row["id"])


async def _jobs(conn) -> dict:
    rows = await (await conn.execute(
        """SELECT job_id, proc_name, hypertable_name, config FROM timescaledb_information.jobs
            WHERE job_id >= 1000""")).fetchall()
    return {(r["proc_name"], r["hypertable_name"]): r for r in rows}


async def _run(conn, job_id: int) -> None:
    await conn.execute(f"CALL run_job({int(job_id)})")


async def _materialized_rows(conn, view: str) -> int:
    m = await (await conn.execute(
        """SELECT materialization_hypertable_schema AS s, materialization_hypertable_name AS n
             FROM timescaledb_information.continuous_aggregates WHERE view_name = %s""", (view,))).fetchone()
    return (await (await conn.execute(f'SELECT count(*) AS n FROM "{m["s"]}"."{m["n"]}"')).fetchone())["n"]


async def test_schema_policies_and_versions(conn):
    s = await timeseries.setup(conn)
    assert s["mode"] == "timescaledb" and s["note"] is None
    versions = {r["extname"]: r["extversion"] for r in await (await conn.execute(
        "SELECT extname, extversion FROM pg_extension WHERE extname LIKE 'timescale%'")).fetchall()}
    server = (await (await conn.execute("SHOW server_version")).fetchone())["server_version"]
    print(f"\nPostgreSQL {server}, extensions {versions}, z-score method {s['zscore_method']}")
    assert s["zscore_method"] == ("toolkit_stats_agg" if "timescaledb_toolkit" in versions else "plain_avg_stddev")

    hts = {r["hypertable_name"]: r for r in await (await conn.execute(
        "SELECT hypertable_name, compression_enabled FROM timescaledb_information.hypertables")).fetchall()}
    assert {"hr_samples", "scrunchie_telemetry"} <= set(hts)
    assert hts["hr_samples"]["compression_enabled"] and hts["scrunchie_telemetry"]["compression_enabled"]
    dims = {r["hypertable_name"]: r for r in await (await conn.execute(
        """SELECT hypertable_name, column_name, time_interval FROM timescaledb_information.dimensions
            WHERE hypertable_name IN ('hr_samples', 'scrunchie_telemetry')""")).fetchall()}
    assert all(d["column_name"] == "time" and d["time_interval"] == timedelta(days=1) for d in dims.values())
    seg = {(r["hypertable_name"], r["attname"]): r for r in await (await conn.execute(
        """SELECT hypertable_name, attname, segmentby_column_index, orderby_column_index, orderby_asc
             FROM timescaledb_information.compression_settings""")).fetchall()}
    for t in ("hr_samples", "scrunchie_telemetry"):
        assert seg[(t, "user_id")]["segmentby_column_index"] == 1 and seg[(t, "profile")]["segmentby_column_index"] == 2
        assert seg[(t, "time")]["orderby_column_index"] == 1 and seg[(t, "time")]["orderby_asc"] is False

    caggs = {r["view_name"]: r for r in await (await conn.execute(
        """SELECT view_name, hypertable_name, materialization_hypertable_name, materialized_only
             FROM timescaledb_information.continuous_aggregates""")).fetchall()}
    assert set(caggs) == set(CAGGS)
    for view, source in CAGGS.items():
        assert caggs[view]["materialized_only"] is False, view          # real-time on every layer
        expected_source = caggs[source]["materialization_hypertable_name"] if source in caggs else source
        assert caggs[view]["hypertable_name"] == expected_source, (view, caggs[view]["hypertable_name"])

    jobs = await _jobs(conn)
    for t in ("hr_samples", "scrunchie_telemetry"):
        assert jobs[("policy_compression", t)]["config"]["compress_after"] == "1 day"
        assert jobs[("policy_retention", t)]["config"]["drop_after"] == "30 days"
    for view in CAGGS:
        assert ("policy_refresh_continuous_aggregate", view) in jobs, view
    assert jobs[("policy_refresh_continuous_aggregate", "hr_1m")]["config"]["start_offset"] == "02:00:00"
    for view, keep in (("hr_1m", "8 days"), ("telemetry_1m", "8 days"), ("hr_1h", "90 days"),
                       ("telemetry_1h", "90 days")):
        assert jobs[("policy_retention", view)]["config"]["drop_after"] == keep
    assert jobs[("stillhere_retention_job", None)]["config"] == {"night_out_after": "48 hours"}

    info = await timeseries.info(conn)
    assert info["timescaledb"] == versions["timescaledb"] and len(info["continuous_aggregates"]) == 6
    assert {j["proc_name"] for j in info["jobs"]} == {"policy_compression", "policy_retention",
                                                      "policy_refresh_continuous_aggregate", "stillhere_retention_job"}


async def test_rollups_refresh_and_stay_real_time(conn):
    u = await _user(conn)
    now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    start, end = now - timedelta(days=2), now - timedelta(minutes=5)
    async with conn.transaction():
        await timeseries.seed_synthetic(conn, u, start=start, end=end, profile="care", hr_every_s=60,
                                        motion_every_s=60)
    q = """SELECT sum(n)::bigint AS n, sum(bpm_sum) AS s FROM {v} WHERE user_id = %s"""
    real_time = {v: await (await conn.execute(q.format(v=v), (u,))).fetchone() for v in ("hr_1m", "hr_1h", "hr_1d")}
    raw = (await (await conn.execute("SELECT count(*) AS n, sum(bpm) AS s FROM hr_samples WHERE user_id = %s",
                                     (u,))).fetchone())
    for v, r in real_time.items():
        assert r["n"] == raw["n"] and float(r["s"]) == float(raw["s"]), v     # nothing materialized yet

    done = await timeseries.refresh(conn, start, now)
    assert done == [v for v, _ in timeseries.REFRESH_ORDER]
    for v in CAGGS:
        assert await _materialized_rows(conn, v) > 0, v
    for v in ("hr_1m", "hr_1h", "hr_1d"):
        r = await (await conn.execute(q.format(v=v), (u,))).fetchone()
        assert r["n"] == raw["n"] and float(r["s"]) == float(raw["s"]), v     # same totals once materialized

    # A sample written after the refresh shows up at every layer at once (real-time aggregation).
    await conn.execute("INSERT INTO hr_samples (time, user_id, source, bpm, profile) VALUES (now(), %s, 't', 99, 'care')",
                       (u,))
    for v in ("hr_1m", "hr_1h", "hr_1d"):
        r = await (await conn.execute(q.format(v=v), (u,))).fetchone()
        assert r["n"] == raw["n"] + 1, v

    # The refresh policies run cleanly.
    jobs = await _jobs(conn)
    for view in ("hr_1m", "telemetry_1m", "hr_1h", "telemetry_1h", "hr_1d", "telemetry_1d"):
        await _run(conn, jobs[("policy_refresh_continuous_aggregate", view)]["job_id"])


async def test_zscore_view_on_timescale(conn):
    users = [await _user(conn), await _user(conn)]
    now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    start, end = now - timedelta(days=7, minutes=5), now - timedelta(minutes=5)
    async with conn.transaction():
        for u in users:
            await timeseries.seed_synthetic(conn, u, start=start, end=end, profile="care", hr_every_s=30,
                                            motion_every_s=30)
    await timeseries.refresh(conn, start, now)
    # Recent minutes after the refresh: read through the real-time part of the aggregates.
    for u, bpm, amp in ((users[0], 66, 20.0), (users[1], 120, 400.0)):
        await conn.execute(
            """INSERT INTO hr_samples (time, user_id, source, bpm)
               SELECT now() - make_interval(secs => s), %s, 'test', %s + (s %% 3) - 1
                 FROM generate_series(5, 200, 5) AS s""", (u, bpm))
        rows = [(now - timedelta(seconds=200 - i), u, "dev", "night_out", False,
                 amp * ((i * 37) % 11 - 5) / 5, amp * ((i * 53) % 7 - 3) / 3, 1000 + amp * ((i * 29) % 13 - 6) / 6,
                 300.0, 310.0) for i in range(200)]
        async with conn.transaction():
            assert await timeseries.copy_rows(conn, rows) == 200
    await timeseries.refresh(conn, now - timedelta(minutes=15), now + timedelta(seconds=1))

    zs = {}
    for u in users:
        async with conn.transaction():      # one now() for the view and the recomputation
            qnow = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
            view = {r["metric"]: r for r in await (await conn.execute(
                "SELECT * FROM user_zscore_5m WHERE user_id = %s", (u,))).fetchall()}
            hr = [(r["time"], float(r["bpm"])) for r in await (await conn.execute(
                "SELECT time, bpm FROM hr_samples WHERE user_id = %s", (u,))).fetchall()]
            mo = [(r["time"], timeseries.enmo_mg(r["ax_mg"], r["ay_mg"], r["az_mg"]))
                  for r in await (await conn.execute(
                      "SELECT time, ax_mg, ay_mg, az_mg FROM scrunchie_telemetry WHERE user_id = %s",
                      (u,))).fetchall()]
        assert_view_matches(view, expected_rows(hr, mo, qnow))
        method = (await timeseries.setup(conn))["zscore_method"]
        assert all(r["method"] == method for r in view.values())
        zs[u] = view
    calm, busy = zs[users[0]], zs[users[1]]
    assert 7 * 1440 - 10 <= calm["hr_bpm"]["baseline_minutes"] <= 7 * 1440 + 5
    assert 60 < calm["hr_bpm"]["baseline_mean"] < 72 and 4 < calm["hr_bpm"]["baseline_sd"] < 12
    assert abs(calm["hr_bpm"]["z"]) < 2 and busy["hr_bpm"]["z"] > 5 and busy["motion_enmo_mg"]["z"] > 3


async def test_columnstore_and_per_profile_retention(conn):
    u = await _user(conn)
    jobs = await _jobs(conn)
    ages = {"fresh": "1 hour", "3d": "3 days", "32d": "32 days"}
    for profile in ("night_out", "care"):
        for label, age in ages.items():
            await conn.execute(
                """INSERT INTO scrunchie_telemetry (time, user_id, device_id, profile, ax_mg, ay_mg, az_mg)
                   SELECT now() - %s::interval + make_interval(secs => s), %s, %s, %s, 0, 0, 1000 + s
                     FROM generate_series(0, 59) AS s""", (age, u, label, profile))
            await conn.execute(
                """INSERT INTO hr_samples (time, user_id, source, bpm, profile)
                   SELECT now() - %s::interval + make_interval(secs => s), %s, %s, 70, %s
                     FROM generate_series(0, 59) AS s""", (age, u, label, profile))
    now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    # Summaries for the 3-day-old minutes exist before the raw rows go.
    await conn.execute("CALL refresh_continuous_aggregate('hr_1m', %s::timestamptz, %s::timestamptz)",
                       (now - timedelta(days=3, minutes=5), now - timedelta(days=3) + timedelta(minutes=5)))
    agg_q = """SELECT profile, sum(n)::bigint AS n FROM hr_1m
                WHERE user_id = %s AND bucket < now() - interval '2 days' AND bucket > now() - interval '4 days'
                GROUP BY profile ORDER BY profile"""
    before_aggs = await (await conn.execute(agg_q, (u,))).fetchall()
    assert [(r["profile"], r["n"]) for r in before_aggs] == [("care", 60), ("night_out", 60)]

    # Columnstore: the policy converts every chunk older than 1 day.
    totals_q = "SELECT profile, device_id, count(*) AS n, sum(az_mg) AS s FROM scrunchie_telemetry WHERE user_id = %s GROUP BY 1, 2 ORDER BY 1, 2"
    before = await (await conn.execute(totals_q, (u,))).fetchall()
    for t in ("hr_samples", "scrunchie_telemetry"):
        await _run(conn, jobs[("policy_compression", t)]["job_id"])
        st = await (await conn.execute(
            "SELECT number_compressed_chunks FROM hypertable_columnstore_stats(%s::regclass)", (t,))).fetchone()
        assert st["number_compressed_chunks"] >= 2, t                    # the 3-day and the 32-day chunks
    assert await (await conn.execute(totals_q, (u,))).fetchall() == before   # same rows, read from the columnstore

    # Per-profile job: night_out raw rows older than 48 h go, also from compressed chunks.
    await _run(conn, jobs[("stillhere_retention_job", None)]["job_id"])
    left_t = {(r["profile"], r["device_id"]) for r in await (await conn.execute(
        "SELECT DISTINCT profile, device_id FROM scrunchie_telemetry WHERE user_id = %s", (u,))).fetchall()}
    left_h = {(r["profile"], r["source"]) for r in await (await conn.execute(
        "SELECT DISTINCT profile, source FROM hr_samples WHERE user_id = %s", (u,))).fetchall()}
    want = {("night_out", "fresh"), ("care", "fresh"), ("care", "3d"), ("care", "32d")}
    assert left_t == want and left_h == want
    # ...and the 1-minute summaries already made from them stay, also after the 1-minute policy runs.
    await _run(conn, jobs[("policy_refresh_continuous_aggregate", "hr_1m")]["job_id"])
    assert await (await conn.execute(agg_q, (u,))).fetchall() == before_aggs

    # Chunk retention: anything past 30 days is dropped whole, whatever the profile.
    for t in ("hr_samples", "scrunchie_telemetry"):
        await _run(conn, jobs[("policy_retention", t)]["job_id"])
    left_t = {(r["profile"], r["device_id"]) for r in await (await conn.execute(
        "SELECT DISTINCT profile, device_id FROM scrunchie_telemetry WHERE user_id = %s", (u,))).fetchall()}
    assert left_t == {("night_out", "fresh"), ("care", "fresh"), ("care", "3d")}
    for view in ("hr_1m", "telemetry_1m", "hr_1h", "telemetry_1h"):
        await _run(conn, jobs[("policy_retention", view)]["job_id"])


async def test_copy_ingest_lands_in_the_hypertable(conn):
    u = await _user(conn)
    now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    prepared = timeseries.prepare_rows([[i * 20, 1, 2, 1000, 5, 6] for i in range(500)], clock="device_ms",
                                       now=now, user_id=u, device_id=None, profile="care", simulated=True)
    async with conn.transaction():
        res = await timeseries.ingest(conn, prepared, user_id=u, device_id=None, batch_id=str(uuid.uuid4()))
    assert res["inserted"] == 500 and res["dropped"] == 0
    chunks = (await (await conn.execute(
        """SELECT count(DISTINCT tableoid) AS n FROM scrunchie_telemetry WHERE user_id = %s""", (u,))).fetchone())["n"]
    assert chunks >= 1
    r = await (await conn.execute(
        "SELECT sum(n)::bigint AS n FROM telemetry_1m WHERE user_id = %s", (u,))).fetchone()
    assert r["n"] == 500
