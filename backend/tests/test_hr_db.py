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
