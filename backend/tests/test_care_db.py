"""Senior mode over HTTP and the database: the seed endpoint, the two reads, the synthetic-only
constraints, and that none of it touches a KAZE table."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from app import care, vultr
from tests.conftest import ADMIN_KEY
from tests.test_care_wording import hits

ADMIN = {"X-Admin-Key": ADMIN_KEY}
END = date(2026, 10, 16)
KAZE_TABLES = ("users", "devices", "contacts", "sessions", "alerts", "timers", "outbox", "audit_log",
               "location_snapshots", "hr_samples")


@pytest.fixture(autouse=True)
def _no_vultr_key(monkeypatch):
    """These tests check the rule template; a VULTR_API_KEY in the shell must not reach the network."""
    for name in ("VULTR_API_KEY", "VULTR_INFERENCE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    vultr.reset_state()
    yield
    vultr.reset_state()


async def kaze_counts(db) -> dict:
    async with db.connection() as conn:
        out = {}
        for t in KAZE_TABLES:
            out[t] = (await (await conn.execute(f"SELECT count(*) AS n FROM {t}")).fetchone())["n"]
        return out


async def care_counts(db) -> dict:
    async with db.connection() as conn:
        return dict(await (await conn.execute(
            """SELECT (SELECT count(*) FROM care_residents) AS residents,
                      (SELECT count(*) FROM care_daily_metrics) AS metrics,
                      (SELECT count(*) FROM care_alerts) AS alerts""")).fetchone())


async def seed(client, **body):
    """A fresh cohort for each test (reset: no push history or feedback from an earlier test)."""
    body = body or {"seed": care.DEFAULT_SEED, "end_day": END.isoformat(), "reset": True}
    r = await client.post("/v1/demo/care-seed", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


async def test_reads_before_any_seed_are_empty_with_a_hint(client, db):
    async with db.connection() as conn:
        await conn.execute("DELETE FROM care_residents")
    r = await client.get("/v1/care/alerts")
    assert r.status_code == 200
    assert r.json()["alerts"] == [] and "care-seed" in r.json()["hint"] and r.json()["synthetic"] is True
    r = await client.get("/v1/care/residents")
    assert r.json()["residents"] == [] and "care-seed" in r.json()["hint"]


async def test_seed_needs_the_admin_key(client, db):
    before = await care_counts(db)
    assert (await client.post("/v1/demo/care-seed")).status_code == 401
    assert (await client.post("/v1/demo/care-seed", headers={"X-Admin-Key": "wrong"})).status_code == 401
    assert await care_counts(db) == before


async def test_seed_scores_the_cohort_and_touches_no_kaze_table(client, db):
    before = await kaze_counts(db)
    body = await seed(client)
    assert body["synthetic"] is True and body["residents"] == 20 and body["days"] == 28 and body["rows"] == 560
    assert body["as_of"] == END.isoformat()
    assert body["scripted_change"] == {"residents": 5, "flagged": 5}
    assert body["no_change_scripts"] == {"residents": 15, "flagged": 0}
    assert sum(body["levels"].values()) == 20 and body["levels"]["low"] == 15
    assert await care_counts(db) == {"residents": 20, "metrics": 560, "alerts": 20}
    await client.get("/v1/care/residents")
    await client.get("/v1/care/alerts?include_low=true")
    assert await kaze_counts(db) == before


async def test_every_stored_row_is_synthetic_and_the_constraint_holds(client, db):
    await seed(client)
    async with db.connection() as conn:
        for t in ("care_residents", "care_daily_metrics", "care_alerts"):
            row = await (await conn.execute(f"SELECT bool_and(synthetic) AS s, count(*) AS n FROM {t}")).fetchone()
            assert row["s"] is True and row["n"] > 0, t
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                """INSERT INTO care_residents (id, display_name, synthetic, script, expect_flag)
                   VALUES ('REAL-1', 'A real person', false, 'none', false)""")
        with pytest.raises(psycopg.errors.CheckViolation):
            await conn.execute(
                """INSERT INTO care_daily_metrics (resident_id, day, synthetic, wear_hours)
                   VALUES ('R-01', '2020-01-01', false, 10)""")


async def test_alerts_are_the_flagged_cards_check_today_first_then_by_score(client):
    await seed(client)
    body = (await client.get("/v1/care/alerts")).json()
    assert body["synthetic"] is True and body["as_of"] == END.isoformat()
    assert body["thresholds_status"] == "untested assumption"
    cards = body["alerts"]
    assert len(cards) == 5 and all(c["level"] in care.FLAGGED for c in cards)
    keys = [(c["level"] != care.LEVEL_REVIEW, -c["score"]) for c in cards]
    assert keys == sorted(keys)
    for c in cards:
        assert {"resident_id", "display_name", "synthetic", "score", "level", "rules", "suggested_check",
                "briefing"} <= set(c)
        for r in c["rules"]:
            assert {"rule", "observed", "baseline", "unit", "contribution", "explanation"} <= set(r)
            assert r["threshold_status"] == "untested assumption"
    everyone = (await client.get("/v1/care/alerts?include_low=true")).json()["alerts"]
    assert len(everyone) == 20


async def test_stored_cards_match_the_pure_engine(client):
    await seed(client)
    pure = care.score_cohort(care.generate_cohort(care.DEFAULT_SEED, END), END)
    expected = {c["resident_id"]: json.loads(json.dumps(c)) for c in pure}
    served = {c["resident_id"]: c for c in (await client.get("/v1/care/alerts?include_low=true")).json()["alerts"]}
    # Without a Vultr key every card keeps the rule template, so served == stored == pure, apart
    # from the delivery decision, which needs the push history and is made when it is stored.
    assert {c.pop("briefing_source") for c in served.values()} == {"template"}
    for c in served.values():
        assert c.pop("delivery")["channel"] in ("push", "digest", None)
        c.pop("query_ms"), c.pop("series")                     # read-time fields (research 4.1)
    for c in expected.values():
        assert c.pop("delivery") is None
    assert served == expected


async def test_residents_list_shows_level_and_the_script_separately(client):
    await seed(client)
    body = (await client.get("/v1/care/residents")).json()
    assert body["synthetic"] is True and body["as_of"] == END.isoformat()
    rows = body["residents"]
    assert [r["resident_id"] for r in rows] == [f"R-{i:02d}" for i in range(1, 21)]
    for r in rows:
        assert r["synthetic"] is True and r["days_of_data"] == 28 and r["as_of"] == END.isoformat()
        assert r["script"]["read_by_rules"] is False
        assert (r["level"] != "low") == (r["script"]["expected"] == "flag"), r


async def test_reseed_replaces_the_cohort(client, db):
    await seed(client)
    await seed(client)
    assert await care_counts(db) == {"residents": 20, "metrics": 560, "alerts": 20}
    later = END + timedelta(days=3)
    body = await seed(client, seed=4, end_day=later.isoformat())
    assert body["as_of"] == later.isoformat()
    assert await care_counts(db) == {"residents": 20, "metrics": 560, "alerts": 20}
    assert (await client.get("/v1/care/alerts")).json()["as_of"] == later.isoformat()


async def test_concurrent_seeds_serialise(client, db):
    results = await asyncio.gather(*(client.post("/v1/demo/care-seed", json={"seed": s, "end_day": END.isoformat()},
                                                 headers=ADMIN) for s in (1, 2, 3)))
    assert [r.status_code for r in results] == [200, 200, 200]
    assert await care_counts(db) == {"residents": 20, "metrics": 560, "alerts": 20}


async def test_seed_defaults_to_yesterday(client):
    r = await client.post("/v1/demo/care-seed", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["as_of"] == (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    assert r.json()["seed"] == care.DEFAULT_SEED


async def test_live_responses_pass_the_wording_check(client):
    texts = [json.dumps(await seed(client), ensure_ascii=False)]
    for path in ("/v1/care/residents", "/v1/care/alerts?include_low=true"):
        texts.append((await client.get(path)).text)
    for t in texts:
        assert not hits(t), hits(t)


async def test_a_resident_whose_data_stopped_stays_on_the_alert_list(client, db):
    from app import care
    await seed(client)
    async with db.connection() as conn:
        await conn.execute("DELETE FROM care_daily_metrics WHERE resident_id = 'R-05' AND day >= %s",
                           (END - timedelta(days=1),))
        await care.evaluate(conn, END)
    alerts = {c["resident_id"]: c for c in (await client.get("/v1/care/alerts")).json()["alerts"]}
    assert alerts["R-05"]["level"] == care.LEVEL_NO_DATA
    assert alerts["R-05"]["briefing_source"] == "template"


# --- research 1.7: feedback buttons ---------------------------------------------------------------
async def feedback_rows(db, resident_id):
    async with db.connection() as conn:
        return list(await (await conn.execute(
            "SELECT * FROM care_feedback WHERE resident_id = %s ORDER BY id", (resident_id,))).fetchall())


@pytest.mark.parametrize("action", ["useful", "not_useful", "already_known", "snooze_3d"])
async def test_each_feedback_button_is_stored_against_the_latest_card(client, db, action):
    await seed(client)
    body = {"action": action}
    if action == "already_known":
        body["reason"] = "family mentioned it at visiting"
    r = await client.post("/v1/care/alerts/R-08/feedback", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["synthetic"] is True and out["action"] == action
    assert out["as_of"] == END.isoformat()
    rows = await feedback_rows(db, "R-08")
    assert rows[-1]["action"] == action and rows[-1]["as_of"] == END and rows[-1]["synthetic"] is True
    if action == "snooze_3d":
        until = datetime.fromisoformat(out["snooze_until"])
        assert timedelta(days=2, hours=23) < until - datetime.now(timezone.utc) <= timedelta(days=3)
    else:
        assert out["snooze_until"] is None
    if action == "already_known":
        assert rows[-1]["reason"] == "family mentioned it at visiting"


async def test_feedback_rejects_unknown_residents_actions_and_long_reasons(client):
    await seed(client)
    assert (await client.post("/v1/care/alerts/R-99/feedback", json={"action": "useful"})).status_code == 404
    assert (await client.post("/v1/care/alerts/R-08/feedback", json={"action": "delete"})).status_code == 422
    r = await client.post("/v1/care/alerts/R-08/feedback", json={"action": "already_known", "reason": "x" * 201})
    assert r.status_code == 422


async def test_feedback_summary_counts_each_button(client, db):
    await seed(client)
    for action in ("useful", "useful", "not_useful"):
        assert (await client.post("/v1/care/alerts/R-10/feedback", json={"action": action})).status_code == 200
    summary = (await client.get("/v1/care/feedback")).json()
    assert summary["synthetic"] is True
    assert summary["counts"]["useful"] >= 2 and summary["counts"]["not_useful"] >= 1
    assert set(summary["counts"]) == {"useful", "not_useful", "already_known", "snooze_3d"}


async def test_feedback_touches_no_kaze_table(client, db):
    await seed(client)
    before = await kaze_counts(db)
    await client.post("/v1/care/alerts/R-08/feedback", json={"action": "snooze_3d"})
    assert await kaze_counts(db) == before


# --- research 1.6: alert budget and the shift-start digest ----------------------------------------
async def test_fresh_seed_pushes_every_flagged_card_once(client, db):
    await seed(client)
    cards = (await client.get("/v1/care/alerts")).json()["alerts"]
    assert cards and all(c["delivery"]["channel"] == "push" for c in cards if c["level"] in care.FLAGGED)
    async with db.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM care_pushes")).fetchone())["n"]
        assert n == len([c for c in cards if c["level"] in care.FLAGGED])
        await care.evaluate(conn, END)                                  # re-scoring the same day
        assert (await (await conn.execute("SELECT count(*) AS n FROM care_pushes")).fetchone())["n"] == n


async def test_a_second_push_in_24_hours_goes_to_the_digest(client, db):
    await seed(client)
    async with db.connection() as conn:
        await conn.execute("DELETE FROM care_pushes WHERE resident_id = 'R-08'")
        await conn.execute(
            """INSERT INTO care_pushes (resident_id, as_of, pushed_at, level, score, signature)
               VALUES ('R-08', %s, now() - interval '2 hours', 'worth a look this shift', 30, 'other')""",
            (END - timedelta(days=1),))
        await care.evaluate(conn, END)
    cards = {c["resident_id"]: c for c in (await client.get("/v1/care/alerts")).json()["alerts"]}
    assert cards["R-08"]["delivery"]["channel"] == "digest"
    digest = (await client.get("/v1/care/digest")).json()
    assert digest["synthetic"] is True and digest["as_of"] == END.isoformat()
    item = next(i for i in digest["items"] if i["resident_id"] == "R-08")
    assert "1 push per resident per 24 h" in item["reason"] and item["level"] == cards["R-08"]["level"]
    assert all(i["resident_id"] != "R-10" for i in digest["items"])   # pushed cards are not repeated


async def test_snooze_button_moves_the_next_card_to_the_digest(client, db):
    await seed(client)
    assert (await client.post("/v1/care/alerts/R-10/feedback", json={"action": "snooze_3d"})).status_code == 200
    async with db.connection() as conn:
        await conn.execute("DELETE FROM care_pushes WHERE resident_id = 'R-10'")
        await care.evaluate(conn, END)
    cards = {c["resident_id"]: c for c in (await client.get("/v1/care/alerts")).json()["alerts"]}
    assert cards["R-10"]["delivery"]["channel"] == "digest"
    assert cards["R-10"]["delivery"]["reason"].startswith("snoozed until ")


# --- research 4.1 (Tiger Data): query time in ms next to each card -----------------------------------
async def test_each_card_shows_its_own_query_time_and_series(client):
    await seed(client)
    body = (await client.get("/v1/care/alerts?include_low=true")).json()
    assert body["timing"]["unit"] == "ms" and body["timing"]["list_query_ms"] >= 0
    assert "server-side" in body["timing"]["note"]
    for c in body["alerts"]:
        assert isinstance(c["query_ms"], float) and 0 <= c["query_ms"] < 5000, c["query_ms"]
        series = c["series"]
        assert len(series["days"]) == 28 and series["days"][-1] == END.isoformat()
        assert len(series["night_restless_min"]) == 28 and len(series["stand_hr_delta_bpm"]) == 28
    # The timings are read-time fields: the stored card does not change.
    async with client._transport.app.state.db.connection() as conn:
        stored = (await (await conn.execute(
            "SELECT card FROM care_alerts WHERE resident_id = 'R-08' ORDER BY as_of DESC LIMIT 1")).fetchone())["card"]
    assert "query_ms" not in stored and "series" not in stored


# --- review round 2: snooze, the push budget and the digest work through the API alone ------------------
async def test_snooze_the_push_budget_and_the_digest_take_effect_through_the_api(client):
    from datetime import timedelta
    day1, day2 = END, END + timedelta(days=1)
    await seed(client, seed=care.DEFAULT_SEED, end_day=day1.isoformat(), reset=True)
    first = {a["resident_id"]: a for a in (await client.get("/v1/care/alerts")).json()["alerts"]}
    flagged1 = {i for i, a in first.items() if a["level"] in care.FLAGGED}
    assert flagged1 and all(first[i]["delivery"]["channel"] == "push" for i in flagged1)
    before = (await client.get("/v1/care/feedback")).json()["counts"]["snooze_3d"]
    r = await client.post("/v1/care/alerts/R-08/feedback", json={"action": "snooze_3d"})
    assert r.status_code == 200 and r.json()["snooze_until"]
    await seed(client, seed=care.DEFAULT_SEED, end_day=day2.isoformat())          # the next day
    assert (await client.get("/v1/care/feedback")).json()["counts"]["snooze_3d"] == before + 1
    second = {a["resident_id"]: a for a in (await client.get("/v1/care/alerts")).json()["alerts"]}
    r08 = second["R-08"]
    assert r08["level"] in care.FLAGGED
    assert r08["delivery"]["channel"] == "digest" and r08["delivery"]["reason"].startswith("snoozed until")
    budgeted = [i for i in flagged1 if i != "R-08" and second.get(i, {}).get("level") in care.FLAGGED]
    assert budgeted
    for i in budgeted:
        assert second[i]["delivery"]["channel"] == "digest"
        assert second[i]["delivery"]["reason"].startswith("1 push per resident per 24 h; the last one was at")
    digest = {d["resident_id"]: d for d in (await client.get("/v1/care/digest")).json()["items"]}
    assert "R-08" in digest and set(budgeted) <= set(digest)
    # reset starts the cohort's history over.
    await seed(client, seed=care.DEFAULT_SEED, end_day=day2.isoformat(), reset=True)
    third = (await client.get("/v1/care/alerts")).json()["alerts"]
    assert all(a["delivery"]["channel"] == "push" for a in third if a["level"] in care.FLAGGED)
    assert (await client.get("/v1/care/feedback")).json()["counts"]["snooze_3d"] == 0
