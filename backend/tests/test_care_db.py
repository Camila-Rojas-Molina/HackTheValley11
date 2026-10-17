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
    r = await client.post("/v1/demo/care-seed", json=body or {"seed": care.DEFAULT_SEED, "end_day": END.isoformat()},
                          headers=ADMIN)
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
    # Without a Vultr key every card keeps the rule template, so served == stored == pure.
    assert {c.pop("briefing_source") for c in served.values()} == {"template"}
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
