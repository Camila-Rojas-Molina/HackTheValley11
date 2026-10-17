"""Heart-rate ingest over HTTP: calibration counts independent readings, not notifications.

HeartCast notifies about once a second while the Watch produces a new value about every 5 s
(Apple Support 120277), so most notifications repeat the last value (research 1.13). Any
minimum-sample check counts at most one reading per 5-second slot.
"""
from __future__ import annotations

import time

from tests.conftest import make_world


def samples(bpm: int, n: int, every_s: float, start_ago_s: float) -> list[dict]:
    now = time.time()
    return [{"bpm": bpm, "t": now - start_ago_s + i * every_s} for i in range(n)]


async def test_repeated_notifications_inside_one_watch_update_are_one_reading(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    r = await client.post("/v1/hr", headers=w.wearer,
                          json={"source": "heartcast-ble", "samples": samples(72, 10, 0.3, 30.0)})
    assert r.status_code == 200 and r.json()["inserted"] == 10
    r = await client.post("/v1/hr/calibrate", headers=w.wearer, json={"window_s": 60})
    assert r.status_code == 409, r.text
    assert "independent readings" in r.json()["detail"] and "one per 5 s" in r.json()["detail"]


async def test_one_hertz_notifications_over_a_minute_count_as_about_twelve_readings(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    body = {"source": "heartcast-ble", "samples": samples(70, 30, 1.0, 59.0) + samples(80, 30, 1.0, 29.0)}
    assert (await client.post("/v1/hr", headers=w.wearer, json=body)).status_code == 200
    r = await client.post("/v1/hr/calibrate", headers=w.wearer, json={"window_s": 120})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["samples"] == 60 and 11 <= out["readings"] <= 13
    assert out["baseline_bpm"] in (70.0, 75.0, 80.0)


async def test_five_readings_five_seconds_apart_are_enough(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    body = {"source": "pulsoid-ws", "samples": samples(64, 6, 5.0, 40.0)}
    assert (await client.post("/v1/hr", headers=w.wearer, json=body)).status_code == 200
    r = await client.post("/v1/hr/calibrate", headers=w.wearer, json={"window_s": 60})
    assert r.status_code == 200 and r.json()["readings"] >= 5 and r.json()["baseline_bpm"] == 64.0


# --- research 1.12: "HR stale" after about 15 s without a post; display only --------------------------
from datetime import timedelta  # noqa: E402

from app import sensors, states  # noqa: E402
from tests.conftest import pending_timers, session_row  # noqa: E402


async def insert_hr(db, user_id, seconds_ago: float, source: str = "heartcast-ble", bpm: int = 72):
    async with db.connection() as conn:
        await conn.execute(
            """INSERT INTO hr_samples (time, user_id, source, simulated, bpm)
               VALUES (now() - make_interval(secs => %s), %s, %s, false, %s)""",
            (seconds_ago, user_id, source, bpm))


async def test_status_before_any_heart_rate(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    body = (await client.get("/v1/hr/status", headers=w.wearer)).json()
    assert body["last_at"] is None and body["stale"] is True and body["label"] == "no heart rate yet"
    assert body["stale_after_s"] == 15 and body["display_only"] is True and body["sources"] == []


async def test_status_is_live_then_stale_after_15_seconds(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    await insert_hr(db, w.user_id, 3)
    body = (await client.get("/v1/hr/status", headers=w.wearer)).json()
    assert body["stale"] is False and body["label"] == "HR live" and 2 <= body["age_s"] < 10
    w2 = await make_world(client, db, contacts=0, arm=False)
    await insert_hr(db, w2.user_id, 20)
    body = (await client.get("/v1/hr/status", headers=w2.wearer)).json()
    assert body["stale"] is True and body["label"] == "HR stale" and body["age_s"] >= 19
    assert "never delays a check-in" in body["note"]


async def test_status_lists_each_source_posting_in_the_last_minute(client, db):
    """Two bridges at once (e.g. the bleak bridge and /hr-bridge) post duplicates: the status shows both."""
    w = await make_world(client, db, contacts=0, arm=False)
    for ago in (2, 4, 6):
        await insert_hr(db, w.user_id, ago, "heartcast-ble")
    await insert_hr(db, w.user_id, 3, "heartcast-webbt")
    body = (await client.get("/v1/hr/status", headers=w.wearer)).json()
    assert {s["source"]: s["samples_60s"] for s in body["sources"]} == {"heartcast-ble": 3, "heartcast-webbt": 1}
    assert body["duplicate_sources"] is True


async def test_status_needs_the_wearer_or_device_key(client, db):
    assert (await client.get("/v1/hr/status")).status_code == 401


async def test_stale_heart_rate_never_moves_a_check_in_later(client, db, ctx):
    w = await make_world(client, db, contacts=1)
    await insert_hr(db, w.user_id, 30)                 # stale, not yet "lost" (120 s)
    before = {t["kind"]: t["fire_at"] for t in await pending_timers(db, w.session_id)}
    await sensors.evaluate_hr(db, ctx, force=True)
    after = {t["kind"]: t["fire_at"] for t in await pending_timers(db, w.session_id)}
    assert after.get(states.CHECKIN_DUE) == before[states.CHECKIN_DUE]
    assert (await session_row(db, w.session_id))["state"] == states.ARMED
