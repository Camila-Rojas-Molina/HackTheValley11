"""TimescaleDB-only checks: hypertables, hierarchical real-time continuous aggregates, refresh,
columnstore, retention policies and the per-profile retention job.

Needs one or more TimescaleDB servers; skipped otherwise. Example (local Docker):

  docker run -d --name stillhere-tsdb -p 127.0.0.1:55432:5432 -e POSTGRES_PASSWORD=<pw> \\
      timescale/timescaledb:latest-pg16                    # TimescaleDB, no Toolkit
  docker run -d --name stillhere-tsdb-ha -p 127.0.0.1:55433:5432 -e POSTGRES_PASSWORD=<pw> \\
      timescale/timescaledb-ha:pg18                        # TimescaleDB + Toolkit (as on Tiger Cloud)
  STILLHERE_TSDB_URIS=postgresql://postgres:<pw>@127.0.0.1:55432/postgres,postgresql://postgres:<pw>@127.0.0.1:55433/postgres \\
      ../.venv/bin/python -m pytest -q tests/test_timeseries_tsdb.py

Without Docker: build PostgreSQL 17 and TimescaleDB 2.30 from source (cmake from cmake.org), add
shared_preload_libraries = 'timescaledb' to postgresql.conf, start it and point STILLHERE_TSDB_URIS at it.
A second server on the same build without the preload line serves STILLHERE_TSDB_NOPRELOAD_URI.

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
    # With Toolkit 1.15+ the device-gap timeline adds its own hourly heartbeat_agg view (research 4.3).
    gaps_view = {"telemetry_heartbeat_1h"} if "timescaledb_toolkit" in versions else set()
    assert set(caggs) == set(CAGGS) | gaps_view
    assert s["gaps_method"] == ("toolkit_heartbeat_agg" if gaps_view else "plain_lag")
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
    assert jobs[("stillhere_retention_job", None)]["config"] == {"night_out_after": "48 hours",
                                                                 "night_out_rollups_after": "8 days"}

    info = await timeseries.info(conn)
    assert info["timescaledb"] == versions["timescaledb"] and len(info["continuous_aggregates"]) == 6 + len(gaps_view)
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


async def test_compression_demo_gives_a_real_ratio(conn):
    """research 4.2: several backdated days of SYNTHETIC care-profile telemetry at 1 Hz, converted to
    the columnstore chunk by chunk, read back from hypertable_columnstore_stats."""
    user = await _user(conn)
    out = await timeseries.compression_demo(conn, user, days=2, motion_every_s=1.0, hr_every_s=5.0)
    assert out["mode"] == "timescaledb" and out["available"] is True and out["synthetic"] is True
    assert out["profile"] == "care" and out["rows"]["motion_rows"] >= 2 * 86400 - 10
    tables = {t["hypertable"]: t for t in out["tables"]}
    tel = tables["scrunchie_telemetry"]
    assert tel["chunks_converted"] >= 2 and tel["number_compressed_chunks"] >= 2
    assert tel["before_compression_total_bytes"] > tel["after_compression_total_bytes"] > 0
    assert tel["ratio"] == round(tel["before_compression_total_bytes"] / tel["after_compression_total_bytes"], 1)
    assert tel["ratio"] > 2
    again = await timeseries.compression_demo(conn, user, days=2, motion_every_s=1.0, hr_every_s=5.0)
    assert {t["hypertable"]: t for t in again["tables"]}["scrunchie_telemetry"]["number_compressed_chunks"] >= 2


async def test_device_gaps_use_toolkit_heartbeat_agg_and_match_the_plain_computation(conn):
    """research 4.3: Toolkit 1.15+ heartbeat_agg -> dead_ranges, read through the hourly continuous
    aggregate with rollup(); the plain LAG computation over the same raw rows gives the same ranges."""
    tk = (await (await conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb_toolkit'")).fetchone())
    setup = await timeseries.setup(conn)
    if tk is None:
        assert setup["gaps_method"] == "plain_lag"
        pytest.skip("this server has no Toolkit")
    assert setup["gaps_method"] == "toolkit_heartbeat_agg"
    user = await _user(conn)
    await conn.execute(
        """INSERT INTO scrunchie_telemetry (time, user_id, profile, ax_mg, ay_mg, az_mg)
           SELECT t, %s, 'care', 0, 0, 1000 FROM (
             SELECT generate_series(now() - interval '40 min', now() - interval '30 min', interval '1 s') t
             UNION ALL SELECT generate_series(now() - interval '25 min', now() - interval '15 min', interval '1 s')
             UNION ALL SELECT generate_series(now() - interval '14 min 30 s', now() - interval '10 min', interval '1 s')) x""",
        (user,))
    tk_out = await timeseries.device_gaps(conn, user, hours=1)
    plain = await timeseries.device_gaps(conn, user, hours=1, method="plain_lag")
    assert tk_out["method"] == "toolkit_heartbeat_agg" and plain["method"] == "plain_lag"
    assert tk_out["num_gaps"] == plain["num_gaps"] == 3
    for a, b in zip(tk_out["dead_ranges"], plain["dead_ranges"]):
        assert abs(a["start_s_ago"] - b["start_s_ago"]) <= 1 and abs(a["end_s_ago"] - b["end_s_ago"]) <= 1
    cagg = await (await conn.execute(
        "SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = 'telemetry_heartbeat_1h'"
    )).fetchone()
    assert cagg is not None


# --- review round 2: device gaps at the window edges, re-seeding over compressed chunks, backlog ------
async def _gaps_both(conn, user, hours):
    tk_out = await timeseries.device_gaps(conn, user, hours=hours)
    plain = await timeseries.device_gaps(conn, user, hours=hours, method="plain_lag")
    return tk_out, plain


def _same_ranges(a, b, tol=1.0):
    assert a["num_gaps"] == b["num_gaps"], (a["dead_ranges"], b["dead_ranges"])
    for x, y in zip(a["dead_ranges"], b["dead_ranges"]):
        assert abs(x["start_s_ago"] - y["start_s_ago"]) <= tol and abs(x["end_s_ago"] - y["end_s_ago"]) <= tol, (x, y)
    assert abs(a["downtime_s"] - b["downtime_s"]) <= 2 * tol * max(1, a["num_gaps"])


async def test_toolkit_gaps_include_the_silence_before_and_after_the_hours_with_data(conn):
    if (await timeseries.setup(conn))["gaps_method"] != "toolkit_heartbeat_agg":
        pytest.skip("this server has no Toolkit")
    user = await _user(conn)
    await conn.execute(
        """INSERT INTO scrunchie_telemetry (time, user_id, profile, ax_mg, ay_mg, az_mg)
           SELECT t, %s, 'care', 0, 0, 1000
             FROM generate_series(now() - interval '3 hours', now() - interval '2 hours', interval '1 s') t""", (user,))
    tk_out, plain = await _gaps_both(conn, user, 4)
    assert tk_out["method"] == "toolkit_heartbeat_agg"
    _same_ranges(tk_out, plain)
    first, last = tk_out["dead_ranges"][0], tk_out["dead_ranges"][-1]
    assert abs(first["start_s_ago"] - 14400) <= 1 and abs(last["end_s_ago"]) <= 1      # silent until now
    assert abs(last["start_s_ago"] - (7200 - 60)) <= 1


async def test_toolkit_gaps_keep_a_samples_liveness_past_an_hour_boundary(conn):
    if (await timeseries.setup(conn))["gaps_method"] != "toolkit_heartbeat_agg":
        pytest.skip("this server has no Toolkit")
    user = await _user(conn)
    # The last sample is 20 s before the top of an hour: it keeps the device live 40 s into the next.
    await conn.execute(
        """INSERT INTO scrunchie_telemetry (time, user_id, profile, ax_mg, ay_mg, az_mg)
           SELECT t, %s, 'care', 0, 0, 1000
             FROM generate_series(date_trunc('hour', now()) - interval '1 hour 20 s',
                                  date_trunc('hour', now()) - interval '20 s', interval '1 s') t""", (user,))
    tk_out, plain = await _gaps_both(conn, user, 3)
    _same_ranges(tk_out, plain)


@pytest.fixture(scope="module", params=URIS or ["unset"])
def ts_app_url(request):
    """A database of its own for the tests that drive the API (and seed a lot of rows)."""
    url = fresh_database(request.param, "stillhere_tsdb_api")
    apply_migrations(url)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("""SELECT alter_job(job_id, scheduled => false) FROM timescaledb_information.jobs
                      WHERE job_id >= 1000""")
    return url


@pytest_asyncio.fixture
async def api(ts_app_url):
    import httpx

    from app.config import Settings
    from app.main import create_app
    s = Settings(database_url=ts_app_url, admin_key="tsdb-admin", device_key="tsdb-device", wearer_key="tsdb-wearer",
                 demo_pin="2468", demo_duress_pin="1357", app_secret="tsdb-secret",
                 public_base_url="https://stillhere.test", run_workers=False, auto_migrate=False, demo_seed=True)
    application = create_app(s, start_workers=False)
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://t",
                                     timeout=600) as c:
            yield c


ADMIN_H = {"X-Admin-Key": "tsdb-admin"}
WEARER_H = {"Authorization": "Bearer tsdb-wearer"}


async def _run_jobs(url, proc, tables=None):
    async with await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row) as c:
        for j in await (await c.execute(
                """SELECT job_id, hypertable_name FROM timescaledb_information.jobs
                    WHERE proc_name = %s AND job_id >= 1000""", (proc,))).fetchall():
            if tables is None or j["hypertable_name"] in tables:
                await c.execute(f"CALL run_job({int(j['job_id'])})")


@pytest.mark.parametrize("profile", ["care", "night_out"])
async def test_reseeding_the_synthetic_baseline_works_once_its_chunks_are_compressed(api, ts_app_url, profile):
    first = await api.post("/v1/telemetry/synthetic", headers=ADMIN_H, json={"profile": profile})
    assert first.status_code == 200, first.text
    assert first.json()["motion_rows"] == 604800
    await _run_jobs(ts_app_url, "policy_compression")
    async with await psycopg.AsyncConnection.connect(ts_app_url, autocommit=True, row_factory=dict_row) as c:
        st = await (await c.execute(
            "SELECT number_compressed_chunks AS n FROM hypertable_columnstore_stats('scrunchie_telemetry'::regclass)")).fetchone()
    assert st["n"] >= 5
    again = await api.post("/v1/telemetry/synthetic", headers=ADMIN_H, json={"profile": profile})
    assert again.status_code == 200, again.text
    assert again.json()["motion_rows"] == 604800


async def test_a_backlog_older_than_the_refresh_window_reaches_every_rollup(api, ts_app_url):
    from datetime import datetime

    from app.security import sha256_hex
    async with await psycopg.AsyncConnection.connect(ts_app_url, autocommit=True, row_factory=dict_row) as c:
        uid = (await (await c.execute(
            "INSERT INTO users (display_name, api_key_hash) VALUES ('backlog', %s) RETURNING id",
            (sha256_hex("backlog-wearer"),))).fetchone())["id"]
    wearer_h = {"Authorization": "Bearer backlog-wearer"}
    now = (await api.get("/v1/health")).json()["server_time"]
    t_now = datetime.fromisoformat(now).timestamp()
    live = [[t_now - 600 + i * 0.1, 0, 0, 1000] for i in range(100)]          # 10 min ago
    r = await api.post("/v1/telemetry/batch", headers=wearer_h, json={"rows": live, "clock": "epoch",
                                                                       "batch_id": "live-1"})
    assert r.status_code == 200 and r.json()["inserted"] == 100, r.text
    await _run_jobs(ts_app_url, "policy_refresh_continuous_aggregate")     # the live minutes are materialized
    old = [[t_now - 5 * 3600 + i * 0.1, 0, 0, 1000] for i in range(600)]     # a backlog from 5 h ago
    r = await api.post("/v1/telemetry/batch", headers=wearer_h, json={"rows": old, "clock": "epoch",
                                                                      "batch_id": "old-1"})
    assert r.status_code == 200 and r.json()["inserted"] == 600, r.text
    for _ in range(3):
        await _run_jobs(ts_app_url, "policy_refresh_continuous_aggregate")
    async with await psycopg.AsyncConnection.connect(ts_app_url, autocommit=True, row_factory=dict_row) as c:
        counts = {}
        for v in ("scrunchie_telemetry", "telemetry_1m", "telemetry_1h", "telemetry_1d"):
            col = "count(*)" if v == "scrunchie_telemetry" else "sum(n)"
            counts[v] = (await (await c.execute(f"SELECT {col}::bigint AS n FROM {v} WHERE user_id = %s",
                                                (uid,))).fetchone())["n"]
    assert counts == {"scrunchie_telemetry": 700, "telemetry_1m": 700, "telemetry_1h": 700, "telemetry_1d": 700}
    agg = (await api.get("/v1/telemetry/aggregates", headers=wearer_h,
                         params={"series": "motion", "resolution": "1h", "hours": 8, "user_id": str(uid)})).json()
    assert sum(b["n"] for b in agg["buckets"]) == 700, agg


NOPRELOAD = os.environ.get("STILLHERE_TSDB_NOPRELOAD_URI")


@pytest.mark.skipif(not NOPRELOAD, reason="set STILLHERE_TSDB_NOPRELOAD_URI to a TimescaleDB server whose "
                                          "shared_preload_libraries leaves timescaledb out")
def test_migrations_fall_back_to_plain_postgres_when_timescaledb_is_installed_but_not_preloaded():
    with psycopg.connect(NOPRELOAD, autocommit=True) as c:
        avail = c.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'").fetchone()
        assert avail, "this server does not have the timescaledb package"
        c.execute('DROP DATABASE IF EXISTS "stillhere_nopreload" WITH (FORCE)')
        c.execute('CREATE DATABASE "stillhere_nopreload" TEMPLATE template0')
    from psycopg.conninfo import make_conninfo
    url = make_conninfo(NOPRELOAD, dbname="stillhere_nopreload")
    applied = apply_migrations(url)
    from scripts.migrate import migration_files
    assert applied == [p.name for p in migration_files()]
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as c:
        s = c.execute("SELECT mode, note FROM timeseries_setup").fetchone()
        assert s["mode"] == "plain" and "shared_preload_libraries" in s["note"], s
        assert c.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone() is None


# --- coverage r3: night-out rollups follow the privacy rules of their raw rows -------------------------
ROLLUPS = ("hr_1m", "hr_1h", "hr_1d", "telemetry_1m", "telemetry_1h", "telemetry_1d")


async def _rollup_counts(conn, user, where: str = "true", args: tuple = ()) -> dict:
    out = {}
    for v in ROLLUPS:
        r = await (await conn.execute(
            f"SELECT profile, sum(n)::bigint AS n FROM {v} WHERE user_id = %s AND {where} GROUP BY profile",
            (user, *args))).fetchall()
        out[v] = {x["profile"]: x["n"] for x in r}
    return out


async def test_night_out_rollups_age_out_after_8_days_at_every_layer_and_care_rollups_stay(conn):
    """Without this a night out's hourly summaries lived 90 days and its daily ones for good."""
    u = await _user(conn)
    for profile in ("night_out", "care"):
        for age in ("10 days", "3 days"):
            await conn.execute(
                """INSERT INTO hr_samples (time, user_id, source, bpm, profile)
                   SELECT date_trunc('day', now() - %s::interval) + interval '6 hours' + make_interval(secs => s),
                          %s, 'r3', 70, %s FROM generate_series(0, 59) AS s""", (age, u, profile))
            await conn.execute(
                """INSERT INTO scrunchie_telemetry (time, user_id, device_id, profile, ax_mg, ay_mg, az_mg)
                   SELECT date_trunc('day', now() - %s::interval) + interval '6 hours' + make_interval(secs => s),
                          %s, 'r3', %s, 0, 0, 1000 FROM generate_series(0, 59) AS s""", (age, u, profile))
    now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    start = (now - timedelta(days=11)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    for v in ROLLUPS:   # bottom layer first
        await conn.execute("CALL refresh_continuous_aggregate(%s::regclass, %s::timestamptz, %s::timestamptz)",
                           (v, start, end))
    for v in ROLLUPS:
        assert await _materialized_rows(conn, v) > 0, v
    old = "bucket < now() - interval '8 days'"
    before = await _rollup_counts(conn, u, old)
    assert all(before[v] == {"night_out": 60, "care": 60} for v in ROLLUPS), before

    jobs = await _jobs(conn)
    await _run(conn, jobs[("stillhere_retention_job", None)]["job_id"])

    after_old = await _rollup_counts(conn, u, old)
    assert all(after_old[v] == {"care": 60} for v in ROLLUPS), after_old          # night_out gone everywhere
    # The 3-day-old night_out raw rows are gone (48 h), but their summaries stay until 8 days.
    raw = (await (await conn.execute(
        "SELECT count(*) AS n FROM hr_samples WHERE user_id = %s AND profile = 'night_out'", (u,))).fetchone())["n"]
    assert raw == 0
    recent = await _rollup_counts(conn, u, "bucket >= now() - interval '8 days'")
    assert all(recent[v] == {"night_out": 60, "care": 60} for v in ROLLUPS), recent
    # The refresh policies do not bring the old ones back.
    for view in ROLLUPS:
        await _run(conn, jobs[("policy_refresh_continuous_aggregate", view)]["job_id"])
    assert await _rollup_counts(conn, u, old) == after_old


async def test_delete_my_data_erases_rollups_and_compressed_raw_rows(api, ts_app_url):
    """DELETE /v1/me/data on TimescaleDB: raw rows in compressed chunks (more than the default
    100000-row decompression limit) and every rollup row of the wearer go at once."""
    from app.security import sha256_hex
    async with await psycopg.AsyncConnection.connect(ts_app_url, autocommit=True, row_factory=dict_row) as c:
        uid = str((await (await c.execute(
            "INSERT INTO users (display_name, api_key_hash) VALUES ('erase-r3', %s) RETURNING id",
            (sha256_hex("erase-r3-wearer"),))).fetchone())["id"])
        other = str((await (await c.execute(
            "INSERT INTO users (display_name) VALUES ('keep-r3') RETURNING id")).fetchone())["id"])
        for who in (uid, other):
            out = await timeseries.compression_demo(c, who, days=2, motion_every_s=1.0, hr_every_s=5.0)
            assert out["available"] is True
        now = (await (await c.execute("SELECT now() AS now")).fetchone())["now"]
        await timeseries.refresh(c, now - timedelta(days=4), now)
        for who in (uid, other):
            async with c.transaction():
                await timeseries.seed_synthetic(c, who, start=now - timedelta(hours=1), end=now, profile="night_out",
                                                hr_every_s=10, motion_every_s=10)
        await timeseries.refresh(c, now - timedelta(hours=2), now)
        before = await _rollup_counts(c, uid)
        assert all(before[v] for v in ROLLUPS), before
        before_other = await _rollup_counts(c, other)
        compressed = (await (await c.execute(
            """SELECT count(*) AS n FROM scrunchie_telemetry WHERE user_id = %s
                 AND time < now() - interval '25 hours'""", (uid,))).fetchone())["n"]
        assert compressed > 100000
    r = await api.delete("/v1/me/data", headers={"Authorization": "Bearer erase-r3-wearer"})
    assert r.status_code == 200, r.text
    erased = r.json()["erased"]
    assert erased["telemetry_rows"] > 100000 and erased["rollup_rows"] > 0
    async with await psycopg.AsyncConnection.connect(ts_app_url, autocommit=True, row_factory=dict_row) as c:
        assert await _rollup_counts(c, uid) == {v: {} for v in ROLLUPS}
        for t in ("scrunchie_telemetry", "hr_samples"):
            n = (await (await c.execute(f"SELECT count(*) AS n FROM {t} WHERE user_id = %s", (uid,))).fetchone())["n"]
            assert n == 0, t
        for view in ROLLUPS:
            mat = (await (await c.execute(
                """SELECT format('%%I.%%I', materialization_hypertable_schema, materialization_hypertable_name) AS m
                     FROM timescaledb_information.continuous_aggregates WHERE view_name = %s""", (view,))).fetchone())["m"]
            n = (await (await c.execute(f"SELECT count(*) AS n FROM {mat} WHERE user_id = %s", (uid,))).fetchone())["n"]
            assert n == 0, view
        # Another wearer's rows are untouched.
        assert await _rollup_counts(c, other) == before_other
        assert all(before_other[v] for v in ROLLUPS)
