"""Personal timeline privacy (BUILD_PLAN §6.3, §6.5, §9):

* GET /v1/me/export?format=html: a printable page with the chain check shown for every session.
* Timelines are deleted 30 days after their session ended unless the wearer pins them (a job that uses
  the same erase flag and leaves a tombstone, like "Delete my data").
"""
from __future__ import annotations

import time

import psycopg
import pytest

from app import audit, engine, states
from tests.conftest import DURESS_PIN, PIN, make_world, session_row
from tests.test_wording import PATTERN


async def _end(client, w, pin=PIN):
    r = await client.post(f"/v1/sessions/{w.session_id}/end", json={"pin": pin}, headers=w.wearer)
    assert r.status_code == 200, r.text


# --- printable export -------------------------------------------------------------------------------
async def test_export_html_is_a_printable_page_with_the_chain_check(client, db, ctx):
    w = await make_world(client, db, contacts=1, name="Maya <script>alert(1)</script>")
    assert (await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer, json={})).status_code == 200
    r = await client.get("/v1/me/export", params={"format": "html"}, headers=w.wearer)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    page = r.text
    assert page.startswith("<!doctype html>")
    assert "<script>" not in page and "Maya &lt;script&gt;" in page          # her name is escaped
    assert page.count("Chain verified ✓") == 2                               # the session and the account
    data = (await client.get("/v1/me/export", headers=w.wearer)).json()
    sess = data["sessions"][0]
    assert sess["head_hash"] in page and data["account_head_hash"] in page
    for e in sess["entries"]:
        assert e["kind"] in page
    assert "@media print" in page
    assert "Personal timeline" in page and "any edit after export breaks the chain when it is recomputed" in page
    assert "deleted 30 days after it ended unless you pin it" in page
    assert not PATTERN.search(page), PATTERN.search(page)
    # JSON stays the default.
    assert (await client.get("/v1/me/export", headers=w.wearer)).headers["content-type"].startswith("application/json")


async def test_export_html_shows_a_broken_chain(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer, json={})
    chain = audit.session_chain(w.session_id)
    async with db.connection() as conn:
        async with conn.transaction():
            await conn.execute("ALTER TABLE audit_log DISABLE TRIGGER audit_append_only")
            row = await (await conn.execute(
                "SELECT id FROM audit_log WHERE chain_key = %s ORDER BY id LIMIT 1", (chain,))).fetchone()
            await conn.execute("UPDATE audit_log SET detail = '{\"edited\": true}' WHERE id = %s", (row["id"],))
            await conn.execute("ALTER TABLE audit_log ENABLE TRIGGER audit_append_only")
    page = (await client.get("/v1/me/export", params={"format": "html"}, headers=w.wearer)).text
    assert "Chain check failed ✗" in page and page.count("Chain verified ✓") == 1   # the account chain is fine


async def test_export_html_needs_the_wearer_key_and_hides_duress(client, db, ctx):
    assert (await client.get("/v1/me/export", params={"format": "html"})).status_code == 401
    real, duress = await make_world(client, db, contacts=1), await make_world(client, db, contacts=1)
    await _end(client, real, PIN)
    await _end(client, duress, DURESS_PIN)
    assert (await session_row(db, duress.session_id))["state"] == "ESCALATED_L1"
    pages = [(await client.get("/v1/me/export", params={"format": "html"}, headers=w.wearer)).text
             for w in (real, duress)]
    for p in pages:
        assert "duress" not in p.lower() and "ESCALATED" not in p
    kinds = [[e["kind"] for s in (await client.get("/v1/me/export", headers=w.wearer)).json()["sessions"]
              for e in s["entries"]] for w in (real, duress)]
    assert kinds[0] == kinds[1]
    assert (await client.get("/v1/me/export", params={"format": "xml"}, headers=real.wearer)).status_code == 422


# --- 30-day timeline retention ---------------------------------------------------------------------
async def _visible(db, sid) -> int:
    async with db.connection() as conn:
        return len(await audit.visible_rows(conn, sid))


async def test_timelines_are_deleted_30_days_after_the_session_ended_unless_pinned(client, db, ctx):
    w = await make_world(client, db, contacts=1, arm=False)
    sids = []
    for _ in range(3):
        r = await client.post("/v1/sessions", json={"profile": "demo"}, headers=w.wearer)
        w.session_id = r.json()["session_id"]
        await client.post(f"/v1/sessions/{w.session_id}/checkin", headers=w.wearer, json={})
        await _end(client, w)
        sids.append(w.session_id)
    old, pinned, recent = sids
    active = (await client.post("/v1/sessions", json={"profile": "demo"}, headers=w.wearer)).json()["session_id"]
    r = await client.post(f"/v1/sessions/{pinned}/pin", json={"pinned": True}, headers=w.wearer)
    assert r.status_code == 200 and r.json() == {"ok": True, "session_id": pinned, "pinned": True}
    async with db.connection() as conn:
        await conn.execute("UPDATE sessions SET ended_at = now() - interval '31 days' WHERE id = ANY(%s)",
                           ([old, pinned],))
        await conn.execute("UPDATE sessions SET ended_at = now() - interval '29 days' WHERE id = %s", (recent,))
    before = {s: await _visible(db, s) for s in (old, pinned, recent, active)}
    assert all(before.values())

    async with db.connection() as conn:
        out = await engine.expire_timelines(conn)
    assert out["sessions"] >= 1 and out["entries"] >= before[old]
    after = {s: await _visible(db, s) for s in (old, pinned, recent, active)}
    assert after[old] == 0 and after[pinned] == before[pinned] and after[recent] == before[recent]
    assert after[active] == before[active]
    async with db.connection() as conn:
        tomb = await (await conn.execute(
            """SELECT detail FROM audit_log WHERE chain_key = %s AND kind = 'timeline_erased'
                ORDER BY id""", (audit.user_chain(w.user_id),))).fetchall()
    assert [t["detail"]["by"] for t in tomb] == ["retention"] and tomb[0]["detail"]["sessions"] == 1
    # The export still verifies every chain that is left.
    ex = (await client.get("/v1/me/export", headers=w.wearer)).json()
    assert ex["account_chain_ok"] is True and all(s["chain_ok"] for s in ex["sessions"])
    # Run again: nothing more to delete, no second tombstone.
    async with db.connection() as conn:
        again = await engine.expire_timelines(conn)
        n = (await (await conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE chain_key = %s AND kind = 'timeline_erased'",
            (audit.user_chain(w.user_id),))).fetchone())["n"]
    assert n == 1 and w.user_id not in again["users"]
    # Unpinned later, the pinned one goes on the next run.
    await client.post(f"/v1/sessions/{pinned}/pin", json={"pinned": False}, headers=w.wearer)
    async with db.connection() as conn:
        await engine.expire_timelines(conn)
    assert await _visible(db, pinned) == 0


async def test_retention_keeps_hidden_entries_of_an_open_duress_alert(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await _end(client, w, DURESS_PIN)                    # looks ended; the masked alert keeps escalating
    async with db.connection() as conn:
        await conn.execute("UPDATE sessions SET ended_at = now() - interval '31 days' WHERE id = %s", (w.session_id,))
        hidden = (await (await conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE session_id = %s AND masked", (w.session_id,))).fetchone())["n"]
        assert hidden >= 1
        await engine.expire_timelines(conn)
        left = (await (await conn.execute(
            "SELECT count(*) AS n FROM audit_log WHERE session_id = %s AND masked", (w.session_id,))).fetchone())["n"]
    assert left == hidden and await _visible(db, w.session_id) == 0
    assert (await session_row(db, w.session_id))["state"] == "ESCALATED_L1"     # the escalation is untouched


async def test_pin_is_the_wearers_own_and_append_only_still_holds(client, db, ctx):
    a, b = await make_world(client, db, contacts=0), await make_world(client, db, contacts=0)
    assert (await client.post(f"/v1/sessions/{a.session_id}/pin", json={"pinned": True},
                              headers=b.wearer)).status_code == 404
    assert (await client.post(f"/v1/sessions/{a.session_id}/pin", json={"pinned": True})).status_code == 401
    async with db.connection() as conn:
        with pytest.raises(psycopg.errors.RaiseException):
            async with conn.transaction():
                await conn.execute("DELETE FROM audit_log WHERE session_id = %s", (a.session_id,))


async def test_the_retention_job_runs_at_most_hourly(db, monkeypatch):
    engine._last_timeline_expiry.clear()
    calls = []

    async def fake(conn):
        calls.append(1)
        return {"sessions": 0, "entries": 0, "users": []}
    monkeypatch.setattr(engine, "expire_timelines", fake)
    assert await engine.maybe_expire_timelines(db) is True
    assert await engine.maybe_expire_timelines(db) is False
    monkeypatch.setitem(engine._last_timeline_expiry, db.conninfo, time.monotonic() - engine.TIMELINE_EXPIRY_EVERY_S - 1)
    assert await engine.maybe_expire_timelines(db) is True
    assert len(calls) == 2


def test_the_timer_worker_runs_the_retention_job():
    import inspect

    from app import timers
    assert "engine.maybe_expire_timelines(db)" in inspect.getsource(timers.timer_worker)
    assert engine.TIMELINE_DAYS == 30 and states.ENDED == "ENDED"
