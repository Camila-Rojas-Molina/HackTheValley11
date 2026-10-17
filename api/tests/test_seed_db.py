"""The demo seed: idempotent, provenance strings present, and the seeded database inside the C3 budget."""
from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row

from app import audit
from app.tiger import BUDGET_BYTES
from scripts import seed_demo


def _counts(url):
    with psycopg.connect(url, row_factory=dict_row) as c:
        return {t: c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
                for t in ("patients", "scans", "scan_metrics", "audit_log")}


def test_a_second_seed_changes_nothing(app, db_url):
    before = _counts(db_url)
    out = seed_demo.seed(db_url)
    assert out["audit_appended"] == []
    assert _counts(db_url) == before
    assert before["patients"] == 1 and before["scans"] == 3 and before["scan_metrics"] == 15


def test_seeded_rows_carry_their_provenance(app, db_url):
    with psycopg.connect(db_url, row_factory=dict_row) as c:
        rows = c.execute("SELECT provenance, solver_version FROM scan_metrics").fetchall()
        scans = {r["scan_id"]: r for r in c.execute("SELECT scan_id, provenance, generator_version FROM scans")}
        seeds = c.execute("SELECT scan_id, detail FROM audit_log WHERE action = 'seed_demo'").fetchall()
    assert {(r["provenance"], r["solver_version"]) for r in rows} == {("real_scan", seed_demo.SEED_VERSION)}
    assert scans["phantom"]["provenance"] == "synthetic_insilico" and scans["phantom"]["generator_version"]
    by_scan = {r["scan_id"]: r["detail"] for r in seeds}
    assert "10.7937/k4qv-hh78" in by_scan["myel018-L1-d273"]["measured_from"]
    assert seed_demo.PER_VERTEBRA_SHA256 in by_scan["myel018-L1-d0"]["measured_from"]
    assert "mock" in by_scan["phantom"]["measured_from"]


async def test_seed_chains_verify(app):
    async with app.state.db.connection() as conn:
        for sid in ("myel018-L1-d0", "myel018-L1-d273", "phantom"):
            assert (await audit.verify_chain(conn, sid))["ok"]


def test_the_seeded_database_is_inside_the_storage_budget(app, db_url):
    with psycopg.connect(db_url) as c:
        size = c.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    assert 0 < size < BUDGET_BYTES


def test_a_restart_never_undoes_what_the_pipeline_wrote(pg_base_uri, monkeypatch):
    """The API seeds at every start: an unchanged seed writes nothing, and a changed one keeps ready assets
    and a recorded calibration."""
    from scripts.migrate import apply_migrations
    from tests.conftest import TEST_DB, fresh_database
    url = fresh_database(pg_base_uri, f"{TEST_DB}_seed")
    apply_migrations(url)
    assert seed_demo.seed(url)["audit_appended"] == ["myel018-L1-d0", "myel018-L1-d273", "phantom"]
    ready = {"status": "ready", "bytes": 10, "sha256": "a" * 64, "basis": "measured", "available_in": None}
    with psycopg.connect(url, row_factory=dict_row) as c:
        c.execute("""UPDATE scans SET files = jsonb_set(files, '{mask_body.expert.nii.gz}', %s::jsonb),
                                      calibration = '{"method": "phantomless_fat_air"}'::jsonb
                      WHERE scan_id = 'myel018-L1-d273'""", (json.dumps(ready),))
        c.commit()
    before = _counts(url)
    assert seed_demo.seed(url)["unchanged"] is True and _counts(url) == before
    monkeypatch.setattr(seed_demo, "SEED_VERSION", "seed_demo/test-change")
    out = seed_demo.seed(url)
    assert out["unchanged"] is False and len(out["audit_appended"]) == 3
    with psycopg.connect(url, row_factory=dict_row) as c:
        row = c.execute("SELECT files, calibration FROM scans WHERE scan_id = 'myel018-L1-d273'").fetchone()
        n = c.execute("SELECT count(*) AS n FROM scan_metrics").fetchone()["n"]
    assert row["files"]["mask_body.expert.nii.gz"] == ready
    assert row["calibration"] == {"method": "phantomless_fat_air"}
    assert n == 15   # the seeded rows were replaced, not duplicated
