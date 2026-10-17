"""TimescaleDB only (PLAN M2 "Migrations" and "Tiger"), on a throwaway local container, never Tiger Cloud:
- the migrations apply and a second run changes nothing;
- scan_metrics, fe_element_results and ai_generation are hypertables with the C3 chunk intervals, and both
  continuous aggregates exist, real-time, grouped by provenance;
- no table has a retention policy, and nothing refreshes or drops history on a schedule;
- a scan dated 9 months back is accepted and reaches the aggregate after a range refresh;
- the API runs on it: /tiger/info says timescaledb, and the seeded manifest still equals its fixture.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest
from psycopg.rows import dict_row

from app import tiger
from app.config import Settings
from app.contract_fixtures import contracts_dir
from app.main import create_app
from app.schemas import API_PREFIX, TigerInfo
from scripts.migrate import apply_migrations
from tests.conftest import TEST_DB, fresh_database

EXPECTED = ["rb_001_core.sql", "rb_002_timeseries.sql", "rb_003_ai.sql"]


@pytest.fixture(scope="module")
def ts_url(tsdb_base_uri):
    url = fresh_database(tsdb_base_uri, f"{TEST_DB}_tsdb")
    assert apply_migrations(url) == EXPECTED
    return url


@pytest.fixture
async def conn(ts_url):
    c = await psycopg.AsyncConnection.connect(ts_url, autocommit=True, row_factory=dict_row)
    await c.execute("SET TIME ZONE 'UTC'")
    try:
        yield c
    finally:
        await c.close()


def test_a_second_run_changes_nothing(ts_url):
    with psycopg.connect(ts_url) as c:
        before = c.execute("SELECT relname, relkind FROM pg_class ORDER BY 1").fetchall()
    assert apply_migrations(ts_url) == []
    with psycopg.connect(ts_url) as c:
        assert c.execute("SELECT relname, relkind FROM pg_class ORDER BY 1").fetchall() == before


async def test_hypertables_and_chunk_intervals(conn):
    rows = await (await conn.execute(
        """SELECT hypertable_name, time_interval FROM timescaledb_information.dimensions
            WHERE hypertable_name IN ('scan_metrics', 'fe_element_results', 'ai_generation')""")).fetchall()
    assert {r["hypertable_name"]: r["time_interval"] for r in rows} == {
        "scan_metrics": timedelta(days=90), "fe_element_results": timedelta(days=30),
        "ai_generation": timedelta(days=7)}
    seg = await (await conn.execute(
        """SELECT hypertable_name, attname FROM timescaledb_information.compression_settings
            WHERE segmentby_column_index IS NOT NULL ORDER BY 1, segmentby_column_index""")).fetchall()
    assert [(r["hypertable_name"], r["attname"]) for r in seg] == [
        ("fe_element_results", "patient_id"), ("fe_element_results", "site"), ("fe_element_results", "run_id"),
        ("scan_metrics", "patient_id"), ("scan_metrics", "site")]


async def test_continuous_aggregates_exist_real_time_and_group_by_provenance(conn):
    rows = await (await conn.execute(
        """SELECT view_name, hypertable_name, materialized_only, view_definition
             FROM timescaledb_information.continuous_aggregates ORDER BY 1""")).fetchall()
    assert [(r["view_name"], r["hypertable_name"], r["materialized_only"]) for r in rows] == [
        ("fe_level_daily", "fe_element_results", False), ("scan_metrics_monthly", "scan_metrics", False)]
    for r in rows:
        group_by = r["view_definition"].split("GROUP BY", 1)[1]
        assert "provenance" in group_by, r["view_name"]


async def test_no_retention_or_any_scheduled_policy_touches_history(conn):
    jobs = await (await conn.execute(
        "SELECT proc_name, hypertable_name FROM timescaledb_information.jobs WHERE job_id >= 1000")).fetchall()
    assert jobs == []
    assert not await (await conn.execute(
        "SELECT 1 FROM timescaledb_information.jobs WHERE proc_name = 'policy_retention'")).fetchall()


async def _insert(conn, t, provenance, lesion_mm3, gen=None, patient="tsdb-p"):
    await conn.execute(
        """INSERT INTO scan_metrics (time, patient_id, site, provenance, generator_version, mask_source, lesion_grid,
                                     lesion_volume_mm3, vertebra_volume_mm3)
           VALUES (%s, %s, 'L1', %s, %s, 'expert', 'original', %s, 68000)""",
        (t, patient, provenance, gen, lesion_mm3))


async def _monthly(conn, patient):
    return await (await conn.execute(
        """SELECT bucket, provenance, n, lesion_volume_mm3_sum AS s FROM scan_metrics_monthly
            WHERE patient_id = %s ORDER BY bucket, provenance""", (patient,))).fetchall()


async def test_a_scan_nine_months_back_reaches_the_aggregate_after_a_range_refresh(conn):
    now = (await (await conn.execute("SELECT now() AS t")).fetchone())["t"]
    recent = now - timedelta(days=40)
    await _insert(conn, recent, "real_scan", 3240.0, patient="tsdb-old")
    await tiger.refresh_range(conn, recent, now)          # the watermark moves past every older bucket
    old = now - timedelta(days=273)
    await _insert(conn, old, "real_scan", 0.0, patient="tsdb-old")
    before = await _monthly(conn, "tsdb-old")
    assert all(r["bucket"] > old for r in before), "a backfilled row showed up without a refresh"
    assert await tiger.refresh_range(conn, old, old) == ["scan_metrics_monthly", "fe_level_daily"]
    after = await _monthly(conn, "tsdb-old")
    assert [r["n"] for r in after] == [1, 1] and after[0]["bucket"] <= old < after[0]["bucket"] + timedelta(days=31)


async def test_real_and_simulated_rows_never_share_an_aggregate_row(conn):
    t = datetime(2026, 5, 10, tzinfo=timezone.utc)
    await _insert(conn, t, "real_scan", 1000.0, patient="tsdb-prov")
    await _insert(conn, t + timedelta(days=2), "synthetic_metric_interp", 2000.0, gen="growth/1", patient="tsdb-prov")
    await tiger.refresh_range(conn, t, t + timedelta(days=2))
    rows = await _monthly(conn, "tsdb-prov")
    assert [(r["provenance"], r["n"], r["s"]) for r in rows] == [("real_scan", 1, 1000.0),
                                                                ("synthetic_metric_interp", 1, 2000.0)]


async def test_info_reports_timescaledb(conn):
    info = TigerInfo.model_validate(await tiger.info(conn))
    assert info.backend == "timescaledb" and info.timescaledb_version
    assert [(h.name, h.chunk_interval) for h in info.hypertables] == [
        ("scan_metrics", "90 days"), ("fe_element_results", "30 days"), ("ai_generation", "7 days")]
    assert [c.materialized_only for c in info.continuous_aggregates] == [False, False]
    assert info.db_size_bytes < info.budget_bytes


async def test_the_api_runs_on_timescaledb(tsdb_base_uri):
    url = fresh_database(tsdb_base_uri, f"{TEST_DB}_tsdb_api")
    app = create_app(Settings(database_url=url, admin_key="k"))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            health = (await c.get(f"{API_PREFIX}/health")).json()
            info = (await c.get(f"{API_PREFIX}/tiger/info")).json()
            manifest = (await c.get(f"{API_PREFIX}/scans/myel018-L1-d273")).json()
            levels = (await c.get(f"{API_PREFIX}/patients/demo-a/levels")).json()
    assert health["timescaledb"] is True and info["backend"] == "timescaledb"
    assert info["hypertables"][0]["rows_estimate"] is not None
    fixture = json.loads((contracts_dir() / "fixtures" / "scans" / "myel018-L1-d273.json").read_text())
    assert manifest == fixture
    assert [lv["level"] for lv in levels["levels"]] == ["T11", "T12", "L1", "L2", "L3", "L4", "L5"]


async def test_two_lesion_grids_in_one_month_stay_two_aggregate_rows_and_the_seed_keeps_day_0(tsdb_base_uri):
    from scripts import seed_demo
    url = fresh_database(tsdb_base_uri, f"{TEST_DB}_tsdb_seed")
    assert apply_migrations(url) == EXPECTED
    seed_demo.seed(url)
    c = await psycopg.AsyncConnection.connect(url, autocommit=True, row_factory=dict_row)
    try:
        await c.execute("SET TIME ZONE 'UTC'")
        later = datetime(2026, 6, 15, tzinfo=timezone.utc)   # a later writer refreshes only its own month
        await _insert(c, later, "synthetic_metric_interp", 100.0, gen="growth/1", patient="tsdb-later")
        assert await tiger.refresh_range(c, later, later) == ["scan_metrics_monthly", "fe_level_daily"]
        rows = await (await c.execute(
            """SELECT bucket, lesion_grid, n, lesion_volume_mm3_avg AS v FROM scan_metrics_monthly
                WHERE patient_id = 'demo-a' AND site = 'L1' ORDER BY bucket, lesion_grid""")).fetchall()
    finally:
        await c.close()
    assert [(r["bucket"].strftime("%Y-%m"), r["lesion_grid"], r["n"]) for r in rows] == [
        ("2026-01", "original", 1), ("2026-10", "crop_1mm", 1), ("2026-10", "original", 1)]
    assert rows[2]["v"] == 3240.0
