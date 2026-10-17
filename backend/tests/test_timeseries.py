"""Tiger Data layer on whatever server the suite uses (pgserver = plain PostgreSQL by default;
STILLHERE_TEST_PG_URI may point at TimescaleDB). TimescaleDB-only checks (policies, columnstore,
the retention job) are in test_timeseries_tsdb.py.
"""
from __future__ import annotations

import ast
import math
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import timeseries
from tests.conftest import ADMIN_KEY, make_world, session_row
from tests.test_wording import PATTERN

ADMIN = {"X-Admin-Key": ADMIN_KEY}
NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)


# --- pure functions ------------------------------------------------------------------------
def _prep(rows, **kw):
    args = dict(clock="device_ms", now=NOW, user_id="u", device_id="d", profile="night_out", simulated=False)
    args.update(kw)
    return timeseries.prepare_rows(rows, **args)


def test_device_ms_rows_are_anchored_to_server_time():
    rows = [[1000, 0, 0, 1000, 300, 310], [1020, 1, 2, 999], [1040, -5, 3, 1001.5, None, 12]]
    p = _prep(rows)
    assert p.dropped == 0 and len(p.rows) == 3
    assert [r[0] for r in p.rows] == [NOW - timedelta(milliseconds=40), NOW - timedelta(milliseconds=20), NOW]
    assert p.rows[0][1:] == ("u", "d", "night_out", False, 0.0, 0.0, 1000.0, 300.0, 310.0)
    assert p.rows[1][-2:] == (None, None) and p.rows[2][-2:] == (None, 12.0)
    # With the device's send-time counter, the newest row sits that far before now.
    p = _prep(rows, device_ms_now=2040)
    assert p.rows[-1][0] == NOW - timedelta(seconds=1)


def test_rows_outside_the_window_are_dropped_not_stored():
    old_ms = timedelta(hours=49).total_seconds() * 1000
    p = _prep([[0, 0, 0, 1000], [old_ms, 0, 0, 1000]])           # first row 49 h before the second
    assert p.dropped == 1 and len(p.rows) == 1
    p = _prep([[1000, 0, 0, 1000]], device_ms_now=0)              # one second in the future: kept
    assert p.dropped == 0
    p = _prep([[100_000, 0, 0, 1000]], device_ms_now=0)           # 100 s in the future: dropped
    assert p.dropped == 1 and not p.rows
    epoch_ms = NOW.timestamp() * 1000
    p = _prep([[epoch_ms - 500, 0, 0, 1000], [NOW.timestamp(), 0, 0, 1000], [epoch_ms - 3 * 86400e3, 0, 0, 1000]],
              clock="epoch")
    assert [r[0] for r in p.rows] == [NOW - timedelta(milliseconds=500), NOW] and p.dropped == 1


@pytest.mark.parametrize("rows, msg", [
    ([[0, 0, 0]], "expected"),
    ([[0, 0, 0, 1000, 1, 2, 3]], "expected"),
    ([[0, 40000, 0, 1000]], "ax_mg out of range"),
    ([[0, 0, float("nan"), 1000]], "ay_mg out of range"),
    ([[0, 0, 0, float("inf")]], "az_mg out of range"),
    ([[0, 0, 0, 1000, -1]], "negative"),
    ([[None, 0, 0, 1000]], "t is required"),
    ([], "no rows"),
])
def test_bad_rows_are_rejected(rows, msg):
    with pytest.raises(timeseries.BatchError, match=msg):
        _prep(rows)


def test_enmo_and_floor():
    assert timeseries.enmo_mg(0, 0, 1000) == 0.0
    assert timeseries.enmo_mg(0, 0, 500) == 0.0                                 # clipped at 0
    assert math.isclose(timeseries.enmo_mg(300, 400, 1200), math.sqrt(300**2 + 400**2 + 1200**2) - 1000)
    t = datetime(2026, 10, 17, 12, 34, 56, 789, tzinfo=timezone.utc)
    assert timeseries._floor(t, timedelta(minutes=1)) == datetime(2026, 10, 17, 12, 34, tzinfo=timezone.utc)
    assert timeseries._floor(t, timedelta(days=1)) == datetime(2026, 10, 17, tzinfo=timezone.utc)


def test_layer_imports_nothing_that_can_change_a_session():
    """PULL_FORWARD-only stays intact: this layer has no path to a transition, alert or timer."""
    backend = Path(__file__).resolve().parent.parent
    forbidden = {"engine", "states", "timers", "outbox", "rules", "sensors", "telegram"}
    for rel in ("app/timeseries.py", "app/routes/telemetry.py"):
        tree = ast.parse((backend / rel).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names.add((node.module or "").split(".")[-1])
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.Import):
                names.update(a.name.split(".")[-1] for a in node.names)
        assert not names & forbidden, (rel, names & forbidden)


def test_user_facing_strings_are_clean():
    for s in (timeseries.NOTE, "last 5 min vs the 7 days before, as 1-minute means"):
        assert not PATTERN.search(s)


# --- helpers for the DB tests --------------------------------------------------------------------
async def _now(db) -> datetime:
    async with db.connection() as conn:
        return (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]


def _accel_rows(n: int, amp: float, start_ms: int = 0, step_ms: int = 1000, seed: int = 1):
    import random
    rnd = random.Random(seed)
    return [[start_ms + i * step_ms, rnd.uniform(-amp, amp), rnd.uniform(-amp, amp), 1000 + rnd.uniform(-amp, amp),
             300 + rnd.uniform(0, 40), 300 + rnd.uniform(0, 40)] for i in range(n)]


async def _seed(db, user_id, *, days: float, profile: str = "care", every_s: float = 30.0):
    now = await _now(db)
    end = now - timedelta(minutes=5)
    start = end - timedelta(days=days)
    async with db.connection() as conn:
        async with conn.transaction():
            counts = await timeseries.seed_synthetic(conn, user_id, start=start, end=end, profile=profile,
                                                     hr_every_s=every_s, motion_every_s=every_s)
        refreshed = await timeseries.refresh(conn, start, now)
    return counts, refreshed


async def _refresh_recent(db):
    """What the 1-minute policy does every minute on TimescaleDB (no-op on plain PostgreSQL).

    Rollups are shared by every user: once another user's newer minutes are materialized, rows
    that arrive behind them are picked up by the next policy run, not by the real-time part.
    """
    now = await _now(db)
    async with db.connection() as conn:
        await timeseries.refresh(conn, now - timedelta(minutes=15), now)


def expected_rows(hr_raw, motion_raw, now):
    """Python recomputation of user_zscore_5m from raw rows: [(metric, time, value)]."""
    out = {}
    for metric, raw in (("hr_bpm", hr_raw), ("motion_enmo_mg", motion_raw)):
        per_min = defaultdict(lambda: [0.0, 0])
        for t, v in raw:
            b = t.astimezone(timezone.utc).replace(second=0, microsecond=0)
            if b >= now - timedelta(days=7, minutes=5):
                per_min[b][0] += v
                per_min[b][1] += 1
        recent = [(b, s, n) for b, (s, n) in per_min.items() if b >= now - timedelta(minutes=5)]
        base = [s / n for b, (s, n) in per_min.items() if b < now - timedelta(minutes=5)]
        if not recent:
            continue
        rmean = sum(s for _, s, _ in recent) / sum(n for _, _, n in recent)
        bmean = sum(base) / len(base) if base else None
        bsd = math.sqrt(sum((x - bmean) ** 2 for x in base) / (len(base) - 1)) if len(base) > 1 else None
        z = (rmean - bmean) / bsd if len(base) >= 30 and bsd else None
        out[metric] = {"recent_mean": rmean, "recent_minutes": len(recent), "baseline_mean": bmean,
                       "baseline_sd": bsd, "baseline_minutes": len(base), "z": z}
    return out


async def view_and_expected(db, user_id):
    """Read the view and the raw rows in one transaction, so both see the same now()."""
    async with db.connection() as conn:
        async with conn.transaction():
            now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
            view = {r["metric"]: r for r in await (await conn.execute(
                "SELECT * FROM user_zscore_5m WHERE user_id = %s", (user_id,))).fetchall()}
            hr = [(r["time"], float(r["bpm"])) for r in await (await conn.execute(
                "SELECT time, bpm FROM hr_samples WHERE user_id = %s", (user_id,))).fetchall()]
            mo = [(r["time"], timeseries.enmo_mg(r["ax_mg"], r["ay_mg"], r["az_mg"])) for r in await (await conn.execute(
                "SELECT time, ax_mg, ay_mg, az_mg FROM scrunchie_telemetry WHERE user_id = %s", (user_id,))).fetchall()]
    return view, expected_rows(hr, mo, now)


def assert_view_matches(view, expected):
    assert set(view) == set(expected), (set(view), set(expected))
    for metric, e in expected.items():
        v = view[metric]
        assert v["recent_minutes"] == e["recent_minutes"] and v["baseline_minutes"] == e["baseline_minutes"], metric
        for k in ("recent_mean", "baseline_mean", "baseline_sd", "z"):
            if e[k] is None:
                assert v[k] is None, (metric, k)
            else:
                assert math.isclose(float(v[k]), e[k], rel_tol=1e-7, abs_tol=1e-6), (metric, k, v[k], e[k])


# --- DB tests ------------------------------------------------------------------------------------
async def test_migration_recorded_its_path(db):
    async with db.connection() as conn:
        s = await timeseries.setup(conn)
        ts = await (await conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).fetchone()
    assert s["mode"] == ("timescaledb" if ts else "plain") and s["note"] is None
    assert s["zscore_method"] in ("plain_avg_stddev", "toolkit_stats_agg")


async def test_batch_ingest_with_copy_and_retry_key(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    rows = _accel_rows(250, 30, start_ms=5000, step_ms=20)           # 5 s at 50 Hz
    body = {"rows": rows, "batch_id": "b-1"}
    r = await client.post("/v1/telemetry/batch", json=body, headers=w.device())
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "duplicate": False, "inserted": 250, "dropped": 0}
    again = await client.post("/v1/telemetry/batch", json=body, headers=w.device())
    assert again.json() == {"ok": True, "duplicate": True, "inserted": 250, "dropped": 0}
    async with db.connection() as conn:
        got = await (await conn.execute(
            """SELECT count(*) AS n, min(time) AS lo, max(time) AS hi, bool_and(device_id = %s) AS dev,
                      bool_and(profile = 'night_out') AS prof, bool_and(NOT simulated) AS real
                 FROM scrunchie_telemetry WHERE user_id = %s""", (w.device_id, w.user_id))).fetchone()
    assert got["n"] == 250 and got["dev"] and got["prof"] and got["real"]
    assert abs((got["hi"] - got["lo"]).total_seconds() - 4.98) < 1e-3       # 249 steps of 20 ms

    # Wearer key, epoch clock, care profile, simulated flag; another wearer's device is refused.
    now = await _now(db)
    epoch_rows = [[(now - timedelta(seconds=10 - i)).timestamp() * 1000, 0, 0, 1000, 0, 0] for i in range(10)]
    r = await client.post("/v1/telemetry/batch", headers=w.wearer, json={
        "rows": epoch_rows, "clock": "epoch", "profile": "care", "simulated": True, "device_id": w.device_id})
    assert r.status_code == 200 and r.json()["inserted"] == 10, r.text
    other = await make_world(client, db, contacts=0, arm=False)
    r = await client.post("/v1/telemetry/batch", headers=w.wearer, json={"rows": epoch_rows, "device_id": other.device_id})
    assert r.status_code == 403
    r = await client.post("/v1/telemetry/batch", headers=w.wearer, json={"rows": epoch_rows, "user_id": other.user_id})
    assert r.status_code == 403
    assert (await client.post("/v1/telemetry/batch", json={"rows": epoch_rows})).status_code == 401
    bad = await client.post("/v1/telemetry/batch", headers=w.wearer, json={"rows": [[0, 99999, 0, 0]]})
    assert bad.status_code == 422 and "ax_mg" in bad.text


async def test_zscore_view_is_sane_and_matches_a_raw_recomputation(client, db):
    calm = await make_world(client, db, contacts=0, arm=False)
    busy = await make_world(client, db, contacts=0, arm=False)
    for w in (calm, busy):
        counts, refreshed = await _seed(db, w.user_id, days=7)
        assert counts["hr_rows"] > 20000 and counts["motion_rows"] > 20000
    # The last few minutes, from the live paths: heart rate via /v1/hr, motion via the batch API.
    now = await _now(db)
    for w, bpm, amp in ((calm, 66, 20), (busy, 118, 450)):
        samples = [{"t": (now - timedelta(seconds=200 - 5 * i)).isoformat(), "bpm": bpm + (i % 3) - 1}
                   for i in range(40)]
        r = await client.post("/v1/hr", json={"source": "test", "samples": samples}, headers=w.wearer)
        assert r.status_code == 200, r.text
        r = await client.post("/v1/telemetry/batch", headers=w.device(),
                              json={"rows": _accel_rows(200, amp, step_ms=1000, seed=7)})
        assert r.status_code == 200 and r.json()["inserted"] == 200, r.text
    await _refresh_recent(db)

    for w in (calm, busy):
        view, expected = await view_and_expected(db, w.user_id)
        assert_view_matches(view, expected)
        for metric, row in view.items():
            assert 7 * 1440 - 10 <= row["baseline_minutes"] <= 7 * 1440 + 5, row
            assert row["simulated"] is True                    # the baseline is synthetic
        hr = view["hr_bpm"]
        assert 60 < hr["baseline_mean"] < 72 and 4 < hr["baseline_sd"] < 12, hr
    calm_v, _ = await view_and_expected(db, calm.user_id)
    busy_v, _ = await view_and_expected(db, busy.user_id)
    assert abs(calm_v["hr_bpm"]["z"]) < 2
    assert busy_v["hr_bpm"]["z"] > 5 and busy_v["motion_enmo_mg"]["z"] > 3

    # The endpoint: the wearer sees only their own rows; the operator sees everyone.
    r = await client.get("/v1/telemetry/zscore", headers=busy.wearer)
    assert r.status_code == 200
    body = r.json()
    assert {x["user_id"] for x in body["rows"]} == {busy.user_id} and len(body["rows"]) == 2
    assert body["note"] == timeseries.NOTE and not PATTERN.search(r.text)
    hr_row = next(x for x in body["rows"] if x["metric"] == "hr_bpm")
    assert hr_row["z"] > 5 and hr_row["method"] == body["method"]
    r = await client.get("/v1/telemetry/zscore", headers=ADMIN)
    assert {calm.user_id, busy.user_id} <= {x["user_id"] for x in r.json()["rows"]}
    assert (await client.get(f"/v1/telemetry/zscore?user_id={calm.user_id}", headers=busy.wearer)).status_code == 403


async def test_rollups_agree_across_layers(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    await _seed(db, w.user_id, days=2, every_s=20)
    r = await client.post("/v1/telemetry/batch", headers=w.device(), json={"rows": _accel_rows(120, 60)})
    assert r.status_code == 200
    await _refresh_recent(db)
    async with db.connection() as conn:
        raw = await (await conn.execute(
            """SELECT count(*) AS n, avg(bpm::float8) AS mean, min(bpm) AS lo, max(bpm) AS hi
                 FROM hr_samples WHERE user_id = %s""", (w.user_id,))).fetchone()
        raw_m = await (await conn.execute(
            """SELECT count(*) AS n, avg(stillhere_enmo_mg(ax_mg, ay_mg, az_mg)) AS enmo,
                      max(fsr_a_mv::float8) AS fa FROM scrunchie_telemetry WHERE user_id = %s""",
            (w.user_id,))).fetchone()
    for resolution, hours in (("1h", 24 * 3), ("1d", 24 * 4)):
        r = await client.get(f"/v1/telemetry/aggregates?series=hr&resolution={resolution}&hours={hours}",
                             headers=w.wearer)
        assert r.status_code == 200, r.text
        b = r.json()["buckets"]
        n = sum(x["n"] for x in b)
        assert n == raw["n"], (resolution, n, raw["n"])
        assert math.isclose(sum(x["mean"] * x["n"] for x in b) / n, raw["mean"], rel_tol=1e-4)
        assert min(x["min"] for x in b) == raw["lo"] and max(x["max"] for x in b) == raw["hi"]
        r = await client.get(f"/v1/telemetry/aggregates?series=motion&resolution={resolution}&hours={hours}",
                             headers=w.wearer)
        mb = r.json()["buckets"]
        assert sum(x["n"] for x in mb) == raw_m["n"]
        assert math.isclose(sum(x["enmo_mean"] * x["n"] for x in mb) / raw_m["n"], raw_m["enmo"], rel_tol=1e-4)
        assert math.isclose(max(x["fsr_a_max"] for x in mb), raw_m["fa"], rel_tol=1e-5)
    # 1-minute buckets for the last hour match a direct GROUP BY on the raw rows.
    r = await client.get("/v1/telemetry/aggregates?series=motion&resolution=1m&hours=1", headers=w.wearer)
    async with db.connection() as conn:
        direct = await (await conn.execute(
            """SELECT date_trunc('minute', time, 'UTC') AS b, count(*) AS n FROM scrunchie_telemetry
                WHERE user_id = %s AND time >= now() - interval '70 minutes'
                GROUP BY 1 ORDER BY 1""", (w.user_id,))).fetchall()
    got = {x["bucket"]: x["n"] for x in r.json()["buckets"]}
    want = {d["b"].astimezone(timezone.utc).isoformat(): d["n"] for d in direct}
    common = set(got) & set(want)
    assert len(common) >= 55 and all(got[b] == want[b] for b in common), (got, want)
    # Limits.
    bad = await client.get("/v1/telemetry/aggregates?series=hr&resolution=1m&hours=49", headers=w.wearer)
    assert bad.status_code == 422


async def test_reads_need_the_wearer_or_operator(client, db):
    w = await make_world(client, db, contacts=1)
    contact_token = w.contacts[0]["token"]
    for path in ("/v1/telemetry/zscore", "/v1/telemetry/aggregates"):
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers={"Authorization": f"Bearer {contact_token}"})).status_code == 401
    assert (await client.get("/v1/telemetry/info", headers=w.wearer)).status_code == 401
    r = await client.get(f"/v1/telemetry/aggregates?user_id={w.user_id}", headers=ADMIN)
    assert r.status_code == 200 and r.json()["user_id"] == w.user_id


async def test_per_profile_retention_prune(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    u = w.user_id
    ages = {"fresh": timedelta(hours=1), "3d": timedelta(days=3), "31d": timedelta(days=31)}
    async with db.connection() as conn:
        async with conn.transaction():
            for profile in ("night_out", "care"):
                for label, age in ages.items():
                    await conn.execute(
                        """INSERT INTO scrunchie_telemetry (time, user_id, profile, ax_mg, ay_mg, az_mg, device_id)
                           VALUES (now() - %s, %s, %s, 0, 0, 1000, %s)""", (age, u, profile, label))
                    await conn.execute(
                        """INSERT INTO hr_samples (time, user_id, source, bpm, profile)
                           VALUES (now() - %s, %s, %s, 70, %s)""", (age, u, label, profile))
            await conn.execute("""INSERT INTO telemetry_batches (user_id, batch_id, inserted, dropped, received_at)
                                  VALUES (%s, 'old', 1, 0, now() - interval '3 days'),
                                         (%s, 'new', 1, 0, now())""", (u, u))
        async with conn.transaction():
            await timeseries.prune(conn)
        left_t = {(r["profile"], r["device_id"]) for r in await (await conn.execute(
            "SELECT profile, device_id FROM scrunchie_telemetry WHERE user_id = %s", (u,))).fetchall()}
        left_h = {(r["profile"], r["source"]) for r in await (await conn.execute(
            "SELECT profile, source FROM hr_samples WHERE user_id = %s", (u,))).fetchall()}
        batches = {r["batch_id"] for r in await (await conn.execute(
            "SELECT batch_id FROM telemetry_batches WHERE user_id = %s", (u,))).fetchall()}
    want = {("night_out", "fresh"), ("care", "fresh"), ("care", "3d")}
    assert left_t == want and left_h == want
    assert batches == {"new"}


async def test_plain_path_prunes_from_the_app_at_most_every_ten_minutes(db, monkeypatch):
    monkeypatch.setattr(timeseries, "_last_prune", {})
    async with db.connection() as conn:
        mode = (await timeseries.setup(conn))["mode"]
    first = await timeseries.maybe_prune(db)
    second = await timeseries.maybe_prune(db)
    assert first is (mode == "plain") and second is False


async def test_telemetry_never_changes_a_session(client, db):
    """Wild motion and a huge z-score leave the armed session, alerts, timers and outbox alone."""
    w = await make_world(client, db, contacts=2)
    before = await session_row(db, w.session_id)

    async def snapshot():
        async with db.connection() as conn:
            return [await (await conn.execute(q, (w.session_id,))).fetchone() for q in (
                "SELECT count(*) AS n FROM alerts WHERE session_id = %s",
                "SELECT count(*) AS n, max(fire_at) AS f FROM timers WHERE session_id = %s",
                "SELECT count(*) AS n FROM outbox WHERE session_id = %s",
                "SELECT count(*) AS n FROM audit_log WHERE session_id = %s")]
    snap = await snapshot()
    await _seed(db, w.user_id, days=1, every_s=60)
    r = await client.post("/v1/telemetry/batch", headers=w.device(),
                          json={"rows": _accel_rows(300, 15000, step_ms=20), "simulated": True})
    assert r.status_code == 200
    assert (await client.get("/v1/telemetry/zscore", headers=w.wearer)).status_code == 200
    after = await session_row(db, w.session_id)
    assert after["state"] == before["state"] == "ARMED"
    assert after["version"] == before["version"] and after["prompted_at"] is None
    assert await snapshot() == snap


async def test_erase_user_raw(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    r = await client.post("/v1/telemetry/batch", headers=w.device(), json={"rows": _accel_rows(10, 5), "batch_id": "x"})
    assert r.json()["inserted"] == 10
    async with db.connection() as conn:
        async with conn.transaction():
            assert await timeseries.erase_user_raw(conn, w.user_id) == 10
        left = await (await conn.execute(
            """SELECT (SELECT count(*) FROM scrunchie_telemetry WHERE user_id = %s)
                    + (SELECT count(*) FROM telemetry_batches WHERE user_id = %s) AS n""",
            (w.user_id, w.user_id))).fetchone()
    assert left["n"] == 0


async def test_delete_my_data_erases_raw_telemetry(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    r = await client.post("/v1/telemetry/batch", headers=w.device(), json={"rows": _accel_rows(12, 5), "batch_id": "e"})
    assert r.json()["inserted"] == 12
    r = await client.delete("/v1/me/data", headers=w.wearer)
    assert r.status_code == 200 and r.json()["erased"]["telemetry_rows"] == 12
    async with db.connection() as conn:
        left = await (await conn.execute(
            """SELECT (SELECT count(*) FROM scrunchie_telemetry WHERE user_id = %s)
                    + (SELECT count(*) FROM telemetry_batches WHERE user_id = %s) AS n""",
            (w.user_id, w.user_id))).fetchone()
    assert left["n"] == 0


async def test_operator_endpoints(client, db):
    w = await make_world(client, db, contacts=0, arm=False)
    r = await client.post("/v1/telemetry/synthetic", headers=ADMIN, json={
        "user_id": w.user_id, "days": 0.25, "hr_every_s": 30, "motion_every_s": 30, "profile": "care"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["simulated"] is True and body["hr_rows"] == 720 and body["motion_rows"] == 720
    info = (await client.get("/v1/telemetry/info", headers=ADMIN)).json()
    assert info["mode"] in ("plain", "timescaledb")
    # A 6-hour range holds no complete daily bucket, so the daily layers are left to real-time.
    assert body["refreshed"] == ([] if info["mode"] == "plain" else
                                 [v for v, res in timeseries.REFRESH_ORDER if res != "1d"])
    r = await client.post("/v1/telemetry/synthetic", headers=ADMIN, json={
        "user_id": w.user_id, "days": 7, "until_minutes_ago": 1440, "hr_every_s": 600, "motion_every_s": 600})
    assert r.status_code == 200, r.text                         # an older range refreshes only itself
    r = await client.post("/v1/telemetry/refresh", headers=ADMIN, json={"hours": 2})
    assert r.status_code == 200 and r.json()["mode"] == info["mode"]
    assert (await client.post("/v1/telemetry/refresh", headers=ADMIN, json={"hours": 48})).status_code == 422
    assert (await client.post("/v1/telemetry/synthetic", headers=w.wearer, json={})).status_code == 401
    r = await client.post("/v1/telemetry/synthetic", headers=ADMIN, json={"user_id": str(uuid.uuid4())})
    assert r.status_code == 404
    async with db.connection() as conn:
        tagged = await (await conn.execute(
            """SELECT bool_and(simulated AND source = 'synthetic') AS ok FROM hr_samples WHERE user_id = %s""",
            (w.user_id,))).fetchone()
    assert tagged["ok"] is True


async def test_plain_path_prunes_night_out_heart_rate_without_any_telemetry_batch(client, db, ctx):
    """POST /v1/hr is the KAZE heart-rate path; pruning must not wait for a telemetry batch."""
    import asyncio
    import uuid as _uuid

    from app import timeseries, timers
    async with db.connection() as conn:
        if (await timeseries.setup(conn))["mode"] != "plain":
            pytest.skip("plain PostgreSQL path only")
        uid = str(_uuid.uuid4())
        await conn.execute(
            """INSERT INTO hr_samples (time, user_id, source, bpm, profile)
               VALUES (now() - interval '72 hours', %s, 'test', 70, 'night_out')""", (uid,))
    timeseries._last_prune.clear()
    stop = asyncio.Event()
    worker = asyncio.create_task(timers.timer_worker(db, ctx, stop, tick_s=0.1))
    await asyncio.sleep(0.5)
    stop.set()
    await worker
    async with db.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM hr_samples WHERE user_id = %s", (uid,))).fetchone())["n"]
    assert n == 0


async def test_zscore_rows_never_carry_nan_toolkit_one_minute_baseline(client, db, ctx):
    """Toolkit's stddev(stats_agg(x)) is NaN for a single value; the reply must stay valid JSON."""
    import json as _json

    import psycopg.sql
    w = await make_world(client, db, contacts=0, arm=False)
    async with db.connection() as conn:
        async with conn.transaction():
            # Emulate the Toolkit view's output for a wearer with one baseline minute.
            await conn.execute(psycopg.sql.SQL(
                """CREATE TEMP VIEW user_zscore_5m AS
                   SELECT {}::uuid AS user_id, 'hr_bpm'::text AS metric, now() AS last_bucket,
                          80.0::float8 AS recent_mean, 5::bigint AS recent_minutes, 50::bigint AS recent_samples,
                          70.0::float8 AS baseline_mean, 'NaN'::float8 AS baseline_sd, 1::bigint AS baseline_minutes,
                          NULL::float8 AS z, false AS simulated, 'toolkit_stats_agg'::text AS method"""
            ).format(psycopg.sql.Literal(w.user_id)))
            rows = await timeseries.zscores(conn, w.user_id)
            await conn.execute("DROP VIEW pg_temp.user_zscore_5m")
    assert rows[0]["baseline_sd"] is None
    _json.dumps(rows, allow_nan=False)  # what Starlette's JSONResponse does


async def test_same_batch_id_from_two_senders_of_one_wearer_is_stored_twice(client, db, ctx):
    from app.security import new_token, sha256_hex
    w = await make_world(client, db, contacts=0, arm=False)
    key_b = new_token()
    dev_b = f"dev-{uuid.uuid4().hex[:10]}"
    async with db.connection() as conn:
        await conn.execute("INSERT INTO devices (id, user_id, device_key_hash) VALUES (%s, %s, %s)",
                           (dev_b, w.user_id, sha256_hex(key_b)))
    dev_a_h = {"Authorization": f"Bearer {w.device_key}", "X-Device-Id": w.device_id}
    dev_b_h = {"Authorization": f"Bearer {key_b}", "X-Device-Id": dev_b}

    def batch(n):
        return {"rows": [[1000 + i * 20, 0, 0, 1000] for i in range(n)], "batch_id": "b1-000001"}
    ra = await client.post("/v1/telemetry/batch", headers=dev_a_h, json=batch(10))
    rb = await client.post("/v1/telemetry/batch", headers=dev_b_h, json=batch(25))
    rw = await client.post("/v1/telemetry/batch", headers=w.wearer, json=batch(7))
    assert [r.json()["duplicate"] for r in (ra, rb, rw)] == [False, False, False]
    assert [r.json()["inserted"] for r in (ra, rb, rw)] == [10, 25, 7]
    again = await client.post("/v1/telemetry/batch", headers=dev_b_h, json=batch(25))  # a real retry
    assert again.json() == {"ok": True, "duplicate": True, "inserted": 25, "dropped": 0}
    async with db.connection() as conn:
        n = (await (await conn.execute("SELECT count(*) AS n FROM scrunchie_telemetry WHERE user_id = %s",
                                       (w.user_id,))).fetchone())["n"]
    assert n == 42
