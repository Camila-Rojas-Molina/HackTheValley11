"""The audit chain (C3 `audit_log`): the hash formula, verification, tamper detection, and the append-only
trigger rb_audit_guard() refusing UPDATE, DELETE and TRUNCATE."""
from __future__ import annotations

import json
from datetime import datetime

import psycopg
import pytest
from psycopg.rows import dict_row

from app import audit
from app.contract_fixtures import contracts_dir


def test_the_hash_formula_reproduces_the_contract_fixture():
    trail = json.loads((contracts_dir() / "fixtures" / "audit" / "myel018-L1-d273.json").read_text())
    e = trail["entries"][0]
    payload = audit.entry_payload(chain_key=trail["chain_key"], at=datetime.fromisoformat(e["at"].replace("Z", "+00:00")),
                                  actor=e["actor"], action=e["action"], patient_id=e["patient_id"],
                                  scan_id=e["scan_id"], run_id=e["run_id"], detail=e["detail"])
    assert audit.compute_hash(e["prev_hash"], payload) == e["hash"] == trail["head"]


async def test_chain_appends_verify_and_detect_tampering(app):
    async with app.state.db.connection() as conn:
        async with conn.transaction():
            h1 = await audit.append(conn, scan_id="myel018-L1-d0", patient_id="demo-a", actor="test",
                                    action="register", detail={"n": 1})
            h2 = await audit.append(conn, scan_id="myel018-L1-d0", patient_id="demo-a", actor="test",
                                    action="store", detail={"n": 2, "unicode": "µε cm³"})
        rows = await audit.rows(conn, "myel018-L1-d0")
    assert [r["hash"] for r in rows][-2:] == [h1, h2]
    assert rows[-1]["prev_hash"] == h1
    assert audit.verify_rows(rows)["ok"] and audit.verify_rows(rows)["head"] == h2
    forged = [dict(r) for r in rows]
    forged[-1]["detail"] = {"n": 3, "unicode": "µε cm³"}
    assert audit.verify_rows(forged) == {"ok": False, "head": h1, "count": len(rows), "bad_id": rows[-1]["id"]}


@pytest.mark.parametrize("sql", [
    "UPDATE audit_log SET actor = 'mallory'",
    "DELETE FROM audit_log",
    "TRUNCATE audit_log",
])
def test_update_delete_and_truncate_are_refused(app, db_url, sql):
    with psycopg.connect(db_url, row_factory=dict_row) as c:
        n = c.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"]
        assert n > 0   # the seed wrote entries
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
            c.execute(sql)
        c.rollback()
        assert c.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"] == n
