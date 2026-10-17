"""The real API process (scripts/run.py) on its own database: what the M2 DONE CHECK asks of it."""
from __future__ import annotations

import pytest

from tests.conftest import TEST_DB
from tests.livesrv import LiveServer


@pytest.mark.wiring
def test_the_real_process_migrates_seeds_and_answers(pg_base_uri, tmp_path):
    srv = LiveServer(pg_base_uri, f"{TEST_DB}_live", tmp_path).start()
    try:
        with srv.client() as c:
            h = c.get("/api/v1/health").json()
            assert (h["status"], h["db"], h["ai_chain"]) == ("ok", "ok", ["template"])
            info = c.get("/api/v1/tiger/info").json()
            assert info["db_size_bytes"] > 0 and [x["name"] for x in info["hypertables"]] == \
                ["scan_metrics", "fe_element_results", "ai_generation"]
            s = c.get("/api/v1/scans/myel018-L1-d273/summary").json()
            assert s["source"] == "template" and s["verified"] is True
    finally:
        srv.stop()
    assert "applied migrations: rb_001_core.sql, rb_002_timeseries.sql, rb_003_ai.sql" in srv.log_text()
