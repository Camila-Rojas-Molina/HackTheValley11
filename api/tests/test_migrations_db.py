"""Migrations on plain PostgreSQL (pgserver or RB_TEST_PG_URI): they apply, a second run changes nothing,
and the C3 rules hold in the schema itself (contracts/db.md)."""
from __future__ import annotations

import psycopg
import pytest

from scripts.migrate import LOCK_ID, apply_migrations, migration_files
from tests.conftest import TEST_DB, fresh_database

EXPECTED = ["rb_001_core.sql", "rb_002_timeseries.sql", "rb_003_ai.sql"]
TABLES = {"patients", "scans", "jobs", "audit_log", "alert_feedback", "slice_rigidity", "scan_metrics",
          "fe_element_results", "ai_generation"}
CATALOG_SQL = """
SELECT c.relname, c.relkind, a.attname, format_type(a.atttypid, a.atttypmod) AS type, a.attnotnull
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
 WHERE n.nspname = 'public' ORDER BY 1, 3"""


@pytest.fixture(scope="module")
def mig_url(pg_base_uri):
    return fresh_database(pg_base_uri, f"{TEST_DB}_mig")


def _catalog(url):
    with psycopg.connect(url) as c:
        schema = c.execute(CATALOG_SQL).fetchall()
        cons = c.execute("""SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid)
                              FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2""").fetchall()
        trig = c.execute("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1").fetchall()
    return schema, cons, trig


def test_migrations_apply_once_and_a_second_run_changes_nothing(mig_url):
    assert [p.name for p in migration_files()] == EXPECTED
    assert apply_migrations(mig_url) == EXPECTED
    before = _catalog(mig_url)
    assert apply_migrations(mig_url) == []
    assert _catalog(mig_url) == before
    with psycopg.connect(mig_url) as c:
        names = {r[0] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        views = {r[0] for r in c.execute("SELECT viewname FROM pg_views WHERE schemaname = 'public'")}
    assert TABLES <= names
    assert {"scan_metrics_monthly", "fe_level_daily"} <= views   # the plain-PostgreSQL fallback


def test_the_lock_id_is_rainbowbones_own():
    assert LOCK_ID != 7_202_611   # StillHere's runner used 7202611


def test_no_column_is_called_risk(mig_url):
    apply_migrations(mig_url)
    with psycopg.connect(mig_url) as c:
        cols = [r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public'")]
    assert cols and not [n for n in cols if "risk" in n.lower()]


@pytest.mark.parametrize("table,columns,values", [
    ("scans", "(scan_id, acquisition_id, label, provenance)", "('s1', 'a', 'x', 'projection')"),
    ("scans", "(scan_id, acquisition_id, label, provenance)", "('s2', 'a', 'x', 'synthetic_insilico')"),
    ("scan_metrics", "(time, patient_id, site, provenance, mask_source)",
     "(now(), 'p', 'L1', 'synthetic_metric_interp', 'expert')"),
    ("scan_metrics", "(time, patient_id, site, provenance, mask_source)", "(now(), 'p', 'L1', 'forecast', 'expert')"),
    ("patients", "(patient_id, label, dataset)", "('Bad Id', 'x', 'y')"),
    ("jobs", "(kind)", "('predict')"),
])
def test_provenance_and_enum_checks_refuse_bad_rows(mig_url, table, columns, values):
    apply_migrations(mig_url)
    with psycopg.connect(mig_url) as c:
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute(f"INSERT INTO {table} {columns} VALUES {values}")


def test_a_synthetic_row_with_its_generator_version_is_accepted(mig_url):
    apply_migrations(mig_url)
    with psycopg.connect(mig_url) as c:
        c.execute("""INSERT INTO scan_metrics (time, patient_id, site, provenance, generator_version, mask_source)
                     VALUES (now(), 'p', 'L1', 'synthetic_metric_interp', 'growth/1', 'expert')""")
        c.rollback()
