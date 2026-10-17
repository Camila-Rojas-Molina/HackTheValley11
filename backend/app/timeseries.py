"""Tiger Data layer: raw scrunchie telemetry, heart-rate rollups and the 5-minute z-score.

Schema in migrations/002_timeseries.sql. On TimescaleDB the raw tables are hypertables, the
1 min -> 1 h -> 1 d rollups are real-time continuous aggregates and retention runs on the
database's own scheduler. On plain PostgreSQL the same names are plain tables and views, and
the app prunes raw rows itself (maybe_prune).

Retention per profile (raw rows):
  night_out   48 hours (KAZE: a night out leaves no raw motion or heart-rate trail)
  care        30 days  (senior mode: the care team's trends need history)
Rollups keep 8 days at 1 min (the z-score baseline needs 7), 90 days at 1 h; daily rows stay.

Real-time rollups on TimescaleDB: buckets newer than the last materialized one are aggregated
from raw rows at query time. Materialization is shared by all users, so a row that arrives
behind an already materialized bucket appears after the next 1-minute policy run (every minute,
covering the last 2 hours); a batch with older rows (a backlog) has its own range refreshed by the
ingest call itself (refresh_backlog), so every accepted row reaches the 1 min, 1 h and 1 d rollups.

This module only stores and reads numbers. It imports nothing from the state machine, so no
value computed here can change a session, open an alert or move a timer.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Sequence

log = logging.getLogger("stillhere.timeseries")

PROFILES = ("night_out", "care")
NIGHT_OUT_RAW = timedelta(hours=48)
CARE_RAW = timedelta(days=30)

# Ingest limits.
MAX_ROWS = 6000                       # 2 minutes at 50 Hz
# Older rows are dropped. One hour inside the 48 h night_out window, so every accepted row can still be
# refreshed into the rollups (a refresh further back could erase other wearers' night_out minutes).
MAX_AGE = NIGHT_OUT_RAW - timedelta(hours=1)
# The 1-minute refresh policies cover the last 2 hours; a row older than this (less a margin for the
# next policy run) would never reach the rollups by itself, so ingest refreshes its range.
POLICY_WINDOW = timedelta(hours=2) - timedelta(minutes=10)
MAX_AHEAD = timedelta(seconds=60)     # rows from the future beyond this are dropped
ACCEL_LIMIT_MG = 32000.0              # +-16 g sensors report up to 16000 mg; allow headroom
FSR_LIMIT_MV = 10000.0

COPY_SQL = ("COPY scrunchie_telemetry (time, user_id, device_id, profile, simulated, "
            "ax_mg, ay_mg, az_mg, fsr_a_mv, fsr_b_mv) FROM STDIN")

SERIES_VIEWS = {
    "hr": {"1m": "hr_1m", "1h": "hr_1h", "1d": "hr_1d"},
    "motion": {"1m": "telemetry_1m", "1h": "telemetry_1h", "1d": "telemetry_1d"},
}
MAX_HOURS = {"1m": 48.0, "1h": 24.0 * 31, "1d": 24.0 * 400}
BUCKETS = {"1m": timedelta(minutes=1), "1h": timedelta(hours=1), "1d": timedelta(days=1)}
# Bottom-up: each layer reads the one below it.
REFRESH_ORDER = (("hr_1m", "1m"), ("telemetry_1m", "1m"), ("hr_1h", "1h"), ("telemetry_1h", "1h"),
                 ("hr_1d", "1d"), ("telemetry_1d", "1d"))
# A refresh recomputes 1-minute rows from raw rows. It must stay inside the 1-minute rollups'
# 8-day retention, and over 'night_out' minutes older than 48 h (raw rows already pruned) it
# would erase their summaries, so only a range just written should be refreshed that far back.
MAX_REFRESH = timedelta(days=7, hours=1)

PLAIN_PRUNE_EVERY_S = 600.0
_last_prune: dict[str, float] = {}

NOTE = "Informational only. It never changes a check-in session and never alerts anyone."


class BatchError(ValueError):
    """A telemetry row that cannot be stored (bad shape or value). Maps to HTTP 422."""


def enmo_mg(ax: float, ay: float, az: float) -> float:
    """Euclidean norm minus one g, clipped at 0 (mg). Mirrors stillhere_enmo_mg() in SQL."""
    return max(math.sqrt(ax * ax + ay * ay + az * az) - 1000.0, 0.0)


# --- ingest ------------------------------------------------------------------------------
@dataclass
class Prepared:
    rows: list[tuple]      # ready for COPY
    dropped: int           # outside [now - 47 h, now + 60 s]


def _num(v, limit: float, what: str, i: int, *, optional: bool = False) -> Optional[float]:
    if v is None:
        if optional:
            return None
        raise BatchError(f"row {i}: {what} is required")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise BatchError(f"row {i}: {what} is not a number") from None
    if not math.isfinite(f) or abs(f) > limit:
        raise BatchError(f"row {i}: {what} out of range")
    return f


def prepare_rows(raw_rows: Sequence[Sequence], *, clock: str, now: datetime, user_id: str,
                 device_id: Optional[str], profile: str, simulated: bool,
                 device_ms_now: Optional[float] = None) -> Prepared:
    """Validate rows [t, ax_mg, ay_mg, az_mg, fsr_a_mv?, fsr_b_mv?] and give each a server time.

    clock='device_ms': t is the device's millisecond counter (the R line of the serial protocol).
      The row with t == device_ms_now (default: the largest t in the batch) is placed at server
      `now`; earlier rows go back by the difference. The server clock is the only clock.
    clock='epoch': t is Unix time in seconds or milliseconds (values above 1e11 are ms).
    Rows that land outside [now - 47 h, now + 60 s] are counted in `dropped`, not stored.
    """
    if profile not in PROFILES:
        raise BatchError(f"profile must be one of {PROFILES}")
    if clock not in ("device_ms", "epoch"):
        raise BatchError("clock must be 'device_ms' or 'epoch'")
    if not raw_rows:
        raise BatchError("no rows")
    if len(raw_rows) > MAX_ROWS:
        raise BatchError(f"at most {MAX_ROWS} rows per batch")
    parsed = []
    for i, r in enumerate(raw_rows):
        if not isinstance(r, (list, tuple)) or not 4 <= len(r) <= 6:
            raise BatchError(f"row {i}: expected [t, ax_mg, ay_mg, az_mg, fsr_a_mv?, fsr_b_mv?]")
        t = _num(r[0], 1e15, "t", i)
        ax = _num(r[1], ACCEL_LIMIT_MG, "ax_mg", i)
        ay = _num(r[2], ACCEL_LIMIT_MG, "ay_mg", i)
        az = _num(r[3], ACCEL_LIMIT_MG, "az_mg", i)
        fa = _num(r[4] if len(r) > 4 else None, FSR_LIMIT_MV, "fsr_a_mv", i, optional=True)
        fb = _num(r[5] if len(r) > 5 else None, FSR_LIMIT_MV, "fsr_b_mv", i, optional=True)
        if (fa is not None and fa < 0) or (fb is not None and fb < 0):
            raise BatchError(f"row {i}: FSR values are millivolts and cannot be negative")
        parsed.append((t, ax, ay, az, fa, fb))

    if clock == "device_ms":
        ref = float(device_ms_now) if device_ms_now is not None else max(p[0] for p in parsed)
        if not math.isfinite(ref):
            raise BatchError("device_ms_now is not a number")

        def to_time(t: float) -> datetime:
            return now - timedelta(milliseconds=ref - t)
    else:
        def to_time(t: float) -> datetime:
            secs = t / 1000.0 if abs(t) > 1e11 else t
            return datetime.fromtimestamp(secs, tz=timezone.utc)

    lo, hi = now - MAX_AGE, now + MAX_AHEAD
    rows: list[tuple] = []
    dropped = 0
    for t, ax, ay, az, fa, fb in parsed:
        try:
            ts = to_time(t)
        except (OverflowError, OSError, ValueError):
            dropped += 1
            continue
        if not lo <= ts <= hi:
            dropped += 1
            continue
        rows.append((ts, user_id, device_id, profile, simulated, ax, ay, az, fa, fb))
    return Prepared(rows=rows, dropped=dropped)


async def copy_rows(conn, rows: Iterable[tuple]) -> int:
    """Bulk insert with COPY FROM STDIN (one round trip per batch)."""
    n = 0
    async with conn.cursor() as cur:
        async with cur.copy(COPY_SQL) as cp:
            for r in rows:
                await cp.write_row(r)
                n += 1
    return n


async def ingest(conn, prepared: Prepared, *, user_id: str, device_id: Optional[str],
                 batch_id: Optional[str]) -> dict:
    """Store one batch. Call inside a transaction. A batch_id repeated by the same sender (device,
    or the wearer key without one) stores nothing again; batch_id is a per-sender retry key."""
    if batch_id:
        key = f"{device_id or '-'}:{batch_id}"
        claimed = await (await conn.execute(
            """INSERT INTO telemetry_batches (user_id, batch_id, device_id, inserted, dropped)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, batch_id) DO NOTHING RETURNING 1""",
            (user_id, key, device_id, len(prepared.rows), prepared.dropped))).fetchone()
        if claimed is None:
            prev = await (await conn.execute(
                "SELECT inserted, dropped FROM telemetry_batches WHERE user_id = %s AND batch_id = %s",
                (user_id, key))).fetchone()
            return {"ok": True, "duplicate": True, "inserted": prev["inserted"] if prev else 0,
                    "dropped": prev["dropped"] if prev else 0}
    inserted = await copy_rows(conn, prepared.rows) if prepared.rows else 0
    return {"ok": True, "duplicate": False, "inserted": inserted, "dropped": prepared.dropped}


# --- setup / mode ----------------------------------------------------------------------------
async def setup(conn) -> dict:
    row = await (await conn.execute(
        "SELECT mode, zscore_method, gaps_method, note FROM timeseries_setup LIMIT 1")).fetchone()
    if row is None:
        return {"mode": "unknown", "zscore_method": None, "gaps_method": None, "note": "002_timeseries.sql not applied"}
    return dict(row)


async def info(conn) -> dict:
    """What this database runs: path, versions, hypertables, rollups, scheduled jobs."""
    s = await setup(conn)
    exts = {r["extname"]: r["extversion"] for r in await (await conn.execute(
        "SELECT extname, extversion FROM pg_extension WHERE extname IN ('timescaledb', 'timescaledb_toolkit')"
    )).fetchall()}
    out = {**s, "server_version": (await (await conn.execute("SHOW server_version")).fetchone())["server_version"],
           "timescaledb": exts.get("timescaledb"), "timescaledb_toolkit": exts.get("timescaledb_toolkit"),
           "retention": {"night_out_raw_hours": NIGHT_OUT_RAW.total_seconds() / 3600,
                         "care_raw_days": CARE_RAW.days, "rollup_1m_days": 8, "rollup_1h_days": 90}}
    if s["mode"] != "timescaledb":
        out["schedule"] = "none on plain PostgreSQL: raw rows are pruned by the app"
        return out
    out["hypertables"] = [dict(r) for r in await (await conn.execute(
        """SELECT hypertable_name, num_chunks, compression_enabled
             FROM timescaledb_information.hypertables
            WHERE hypertable_name IN ('hr_samples', 'scrunchie_telemetry') ORDER BY 1""")).fetchall()]
    out["continuous_aggregates"] = [dict(r) for r in await (await conn.execute(
        """SELECT view_name, materialized_only, hypertable_name AS reads_from
             FROM timescaledb_information.continuous_aggregates ORDER BY 1""")).fetchall()]
    # Job timestamps are '-infinity' until a job has run, which Python datetimes cannot hold.
    jobs = await (await conn.execute(
        """SELECT j.job_id, j.proc_name, j.hypertable_name, j.schedule_interval::text AS schedule_interval,
                  j.scheduled, j.config, s.last_run_status,
                  CASE WHEN isfinite(s.last_successful_finish) THEN s.last_successful_finish::text END
                    AS last_successful_finish,
                  CASE WHEN isfinite(s.next_start) THEN s.next_start::text END AS next_start
             FROM timescaledb_information.jobs j
             LEFT JOIN timescaledb_information.job_stats s USING (job_id)
            WHERE j.proc_name IN ('policy_compression', 'policy_retention',
                                  'policy_refresh_continuous_aggregate', 'stillhere_retention_job')
            ORDER BY j.job_id""")).fetchall()
    out["jobs"] = [dict(r) for r in jobs]
    stats = []
    for table in ("hr_samples", "scrunchie_telemetry"):
        r = await (await conn.execute(
            """SELECT total_chunks, number_compressed_chunks, before_compression_total_bytes,
                      after_compression_total_bytes
                 FROM hypertable_columnstore_stats(%s::regclass)""", (table,))).fetchone()
        if r:
            stats.append({"hypertable": table, **dict(r)})
    out["columnstore"] = stats
    return out


# --- reads ---------------------------------------------------------------------------------
def _f(v, nd: int = 3) -> Optional[float]:
    # Not-a-number reads as missing: Toolkit's stddev() of a single value is NaN, and JSON has no NaN.
    if v is None or not math.isfinite(float(v)):
        return None
    return round(float(v), nd)


async def zscores(conn, user_id: Optional[str] = None) -> list[dict]:
    """Rows of user_zscore_5m (one per user and metric with data in the last 5 minutes)."""
    q = "SELECT * FROM user_zscore_5m"
    args: list = []
    if user_id is not None:
        q += " WHERE user_id = %s"
        args.append(user_id)
    rows = await (await conn.execute(q + " ORDER BY user_id, metric", args)).fetchall()
    return [{"user_id": str(r["user_id"]), "metric": r["metric"],
             "last_bucket": r["last_bucket"].isoformat() if r["last_bucket"] else None,
             "recent_mean": _f(r["recent_mean"]), "recent_minutes": r["recent_minutes"],
             "recent_samples": r["recent_samples"], "baseline_mean": _f(r["baseline_mean"]),
             "baseline_sd": _f(r["baseline_sd"]), "baseline_minutes": r["baseline_minutes"],
             "z": _f(r["z"], 2), "simulated": bool(r["simulated"]), "method": r["method"]} for r in rows]


_SD = ("CASE WHEN sum(n) > 1 THEN sqrt(greatest((sum({ss}) - sum({s}) ^ 2 / sum(n)::float8)"
       " / (sum(n)::float8 - 1), 0)) END")

_AGG_SQL = {
    "hr": ("SELECT bucket, sum(n)::bigint AS n, sum(bpm_sum) / sum(n)::float8 AS mean, "
           + _SD.format(s="bpm_sum", ss="bpm_sumsq") + " AS sd, "
           "min(bpm_min) AS min, max(bpm_max) AS max, bool_or(simulated) AS simulated "
           "FROM {view} WHERE user_id = %s AND bucket >= %s AND bucket < %s "
           "GROUP BY bucket ORDER BY bucket"),
    "motion": ("SELECT bucket, sum(n)::bigint AS n, sum(enmo_sum) / sum(n)::float8 AS enmo_mean, "
               + _SD.format(s="enmo_sum", ss="enmo_sumsq") + " AS enmo_sd, max(enmo_max) AS enmo_max, "
               "sum(ax_sum) / sum(n)::float8 AS ax_mean, sum(ay_sum) / sum(n)::float8 AS ay_mean, "
               "sum(az_sum) / sum(n)::float8 AS az_mean, max(fsr_a_max) AS fsr_a_max, "
               "max(fsr_b_max) AS fsr_b_max, bool_or(simulated) AS simulated "
               "FROM {view} WHERE user_id = %s AND bucket >= %s AND bucket < %s "
               "GROUP BY bucket ORDER BY bucket"),
}


async def aggregates(conn, *, series: str, resolution: str, user_id: str, hours: float,
                     now: Optional[datetime] = None) -> list[dict]:
    """Buckets for one user, merged across devices and profiles, oldest first."""
    if series not in SERIES_VIEWS or resolution not in BUCKETS:
        raise ValueError("unknown series or resolution")
    if not 0 < hours <= MAX_HOURS[resolution]:
        raise ValueError(f"hours must be in (0, {MAX_HOURS[resolution]:g}] at {resolution}")
    if now is None:
        now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    sql = _AGG_SQL[series].format(view=SERIES_VIEWS[series][resolution])
    rows = await (await conn.execute(sql, (user_id, now - timedelta(hours=hours), now + BUCKETS[resolution]))).fetchall()
    out = []
    for r in rows:
        d = {"bucket": r["bucket"].isoformat()}
        for k, v in r.items():
            if k == "bucket":
                continue
            d[k] = v if isinstance(v, (bool, int)) or v is None else round(float(v), 3)
        out.append(d)
    return out


# --- maintenance ---------------------------------------------------------------------------
async def refresh(conn, start: datetime, end: datetime) -> list[str]:
    """Materialize the rollups for [start, end], bottom layer first. TimescaleDB only.

    Needs an autocommit connection outside a transaction (refresh_continuous_aggregate is a
    procedure that commits). Refresh only the range you just wrote: a refresh recomputes rows
    from the layer below, and 'night_out' raw rows older than 48 h no longer exist.
    """
    if (await setup(conn))["mode"] != "timescaledb":
        return []
    if end - start > MAX_REFRESH:
        raise ValueError("refresh window is limited to 7 days + 1 hour")
    now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    done = []
    for view, res in REFRESH_ORDER:
        lo = _floor(start, BUCKETS[res])
        # Stop where the policies stop (end_offset = one bucket) so the watermark never runs
        # ahead of them; newer buckets come from the real-time part of the query.
        hi = min(end, now - BUCKETS[res])
        if hi - lo < BUCKETS[res]:
            continue        # no complete bucket in range; TimescaleDB rejects such a window
        await conn.execute("CALL refresh_continuous_aggregate(%s::regclass, %s::timestamptz, %s::timestamptz)",
                           (view, lo, hi))
        done.append(view)
    return done


async def refresh_backlog(conn, rows: Sequence[tuple]) -> list[str]:
    """After a batch is committed: materialize its range when it has rows older than the refresh
    policies' window, which would otherwise never reach the rollups. TimescaleDB only; autocommit
    connection outside a transaction. Complete buckets only, and never ahead of the policies."""
    if not rows:
        return []
    now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    oldest, newest = min(r[0] for r in rows), max(r[0] for r in rows)
    if oldest >= now - POLICY_WINDOW or (await setup(conn))["mode"] != "timescaledb":
        return []
    done = []
    for view, res in REFRESH_ORDER:
        step = BUCKETS[res]
        lo = max(_floor(oldest, step), _floor(now - MAX_AGE, step) + step)
        hi = min(_floor(newest, step) + step, now - step)
        if hi - lo < step:
            continue  # recent enough for the real-time part of the query (or no complete bucket)
        await conn.execute("CALL refresh_continuous_aggregate(%s::regclass, %s::timestamptz, %s::timestamptz)",
                           (view, lo, hi))
        done.append(view)
    return done


def _floor(t: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    secs = (t - epoch) // step
    return epoch + secs * step


async def prune(conn, *, care: bool = True) -> None:
    """Delete raw rows past their profile's retention (night_out 48 h; with care=True, any
    profile past 30 days, which TimescaleDB's chunk retention policy covers on its own)."""
    await conn.execute("SELECT stillhere_prune_raw(%s, %s)", (NIGHT_OUT_RAW, CARE_RAW if care else None))


async def maybe_prune(db) -> bool:
    """Plain PostgreSQL has no scheduler: prune at most every 10 minutes per database (called by
    the timer worker and after a telemetry batch).

    On TimescaleDB the stillhere_retention_job and the chunk retention policies do this, so
    nothing runs here.
    """
    key = db.conninfo
    now = time.monotonic()
    if now - _last_prune.get(key, -1e9) < PLAIN_PRUNE_EVERY_S:
        return False
    _last_prune[key] = now
    try:
        async with db.connection() as conn:
            if (await setup(conn))["mode"] == "timescaledb":
                return False
            async with conn.transaction():
                await prune(conn)
        return True
    except Exception as exc:  # best effort; the next batch tries again in 10 minutes
        log.warning("raw-row prune failed: %s", type(exc).__name__)
        return False


async def erase_user_raw(conn, user_id) -> int:
    """Delete one user's raw scrunchie telemetry and batch receipts (for DELETE /v1/me/data)."""
    n = (await conn.execute("DELETE FROM scrunchie_telemetry WHERE user_id = %s", (user_id,))).rowcount
    await conn.execute("DELETE FROM telemetry_batches WHERE user_id = %s", (user_id,))
    return n


# --- synthetic baseline (labelled) ------------------------------------------------------------
# Deterministic pseudo-noise in [0, 1) from a timestamp, so seeded data is reproducible.
_NOISE = "(abs(sin(extract(epoch FROM t) * {k}) * 43758.5453) - floor(abs(sin(extract(epoch FROM t) * {k}) * 43758.5453)))"


async def seed_synthetic(conn, user_id, *, start: datetime, end: datetime, profile: str = "night_out",
                         hr_every_s: float = 10.0, motion_every_s: float = 1.0) -> dict:
    """Write a SYNTHETIC baseline for one user over [start, end): heart rate with a daily rhythm
    and resting-to-active motion. Every row is simulated = true (heart rate source 'synthetic',
    telemetry device_id 'synthetic'); earlier synthetic rows in the range are replaced.
    Call inside a transaction, then refresh() the same range.
    """
    if profile not in PROFILES:
        raise ValueError("profile")
    if end <= start:
        raise ValueError("empty range")
    if (await setup(conn))["mode"] == "timescaledb":
        # Earlier synthetic rows may sit in compressed chunks (care rows stay 30 days); replacing them
        # decompresses whole batches, more than TimescaleDB's default per-transaction limit of 100000
        # rows for a 7-day baseline. Lift the limit for this transaction only.
        await conn.execute("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
    await conn.execute("""DELETE FROM hr_samples WHERE user_id = %s AND source = 'synthetic'
                           AND time >= %s AND time < %s""", (user_id, start, end))
    await conn.execute("""DELETE FROM scrunchie_telemetry WHERE user_id = %s AND simulated
                           AND device_id = 'synthetic' AND time >= %s AND time < %s""", (user_id, start, end))
    hod = "extract(hour FROM t AT TIME ZONE 'UTC') + extract(minute FROM t AT TIME ZONE 'UTC') / 60.0"
    hr = await conn.execute(
        f"""INSERT INTO hr_samples (time, user_id, source, simulated, bpm, profile)
            SELECT t, %s, 'synthetic', true,
                   round(66 + 10 * sin(2 * pi() * (({hod}) - 10) / 24) + 8 * ({_NOISE.format(k='12.9898')} - 0.5)),
                   %s
              FROM generate_series(%s::timestamptz, %s::timestamptz - interval '1 microsecond',
                                   make_interval(secs => %s)) AS t""",
        (user_id, profile, start, end, hr_every_s))
    amp = f"(CASE WHEN ({hod}) < 7 THEN 15.0 ELSE 80.0 END)"
    mo = await conn.execute(
        f"""INSERT INTO scrunchie_telemetry (time, user_id, device_id, profile, simulated,
                                             ax_mg, ay_mg, az_mg, fsr_a_mv, fsr_b_mv)
            SELECT t, %s, 'synthetic', %s, true,
                   {amp} * ({_NOISE.format(k='78.233')} - 0.5) * 2,
                   {amp} * ({_NOISE.format(k='37.719')} - 0.5) * 2,
                   1000 + {amp} * ({_NOISE.format(k='11.131')} - 0.5) * 2,
                   300 + 40 * {_NOISE.format(k='93.989')},
                   300 + 40 * {_NOISE.format(k='51.537')}
              FROM generate_series(%s::timestamptz, %s::timestamptz - interval '1 microsecond',
                                   make_interval(secs => %s)) AS t""",
        (user_id, profile, start, end, motion_every_s))
    return {"hr_rows": hr.rowcount, "motion_rows": mo.rowcount, "simulated": True}


# --- columnstore proof (research 4.2) ----------------------------------------------------------------
# The columnstore policy converts a chunk only once it is older than the 1-day chunk interval, on a
# daily schedule, so a weekend demo compresses nothing on its own, and tiny tables can even grow.
# compression_demo() writes several days of SYNTHETIC care-profile telemetry that end more than a day
# ago (care raw rows are kept 30 days; night_out ones only 48 h), converts every chunk older than a day
# with convert_to_columnstore, and reads the real before/after bytes from hypertable_columnstore_stats.
COMPRESSION_PROFILE = "care"
COMPRESSED_TABLES = ("scrunchie_telemetry", "hr_samples")


def compression_window(now: datetime, days: float) -> tuple[datetime, datetime]:
    """[start, end): `days` of data ending 25 h ago, so every chunk is past the 1-day policy age."""
    end = now - timedelta(days=1, hours=1)
    return end - timedelta(days=days), end


async def convert_old_chunks(conn, older_than: timedelta = timedelta(days=1)) -> dict:
    """CALL convert_to_columnstore on every chunk older than `older_than`. Autocommit, no transaction."""
    done = {}
    for table in COMPRESSED_TABLES:
        chunks = [r["c"] for r in await (await conn.execute(
            "SELECT show_chunks(%s::regclass, older_than => %s::interval)::text AS c", (table, older_than))).fetchall()]
        for chunk in chunks:
            await conn.execute("CALL convert_to_columnstore(%s::regclass, if_not_columnstore => true)", (chunk,))
        done[table] = len(chunks)
    return done


async def columnstore_stats(conn) -> list[dict]:
    out = []
    for table in COMPRESSED_TABLES:
        r = await (await conn.execute(
            """SELECT total_chunks, number_compressed_chunks, before_compression_total_bytes,
                      after_compression_total_bytes
                 FROM hypertable_columnstore_stats(%s::regclass)""", (table,))).fetchone()
        row = {"hypertable": table, **(dict(r) if r else {})}
        before, after = row.get("before_compression_total_bytes"), row.get("after_compression_total_bytes")
        row["ratio"] = round(before / after, 1) if before and after else None
        out.append(row)
    return out


async def compression_demo(conn, user_id, *, days: float = 3.0, motion_every_s: float = 1.0,
                           hr_every_s: float = 5.0) -> dict:
    """Backdated SYNTHETIC care-profile telemetry, converted to the columnstore: a measured ratio.

    TimescaleDB only; on plain PostgreSQL it writes nothing. Call on an autocommit connection
    outside a transaction. The rollups are not refreshed over this backdated range (a refresh there
    would recompute other wearers' 'night_out' minutes whose raw rows are already pruned).
    """
    if (await setup(conn))["mode"] != "timescaledb":
        return {"mode": "plain", "available": False, "synthetic": True,
                "note": "The columnstore needs TimescaleDB; nothing was written on plain PostgreSQL."}
    now = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    start, end = compression_window(now, days)
    async with conn.transaction():
        # A re-run replaces this wearer's earlier synthetic rows in the window, which may sit in
        # compressed chunks: let this one transaction decompress as many as that takes.
        await conn.execute("SET LOCAL timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
        rows = await seed_synthetic(conn, user_id, start=start, end=end, profile=COMPRESSION_PROFILE,
                                    hr_every_s=hr_every_s, motion_every_s=motion_every_s)
    converted = await convert_old_chunks(conn)
    tables = await columnstore_stats(conn)
    for t in tables:
        t["chunks_converted"] = converted.get(t["hypertable"], 0)
    return {"mode": "timescaledb", "available": True, "synthetic": True, "profile": COMPRESSION_PROFILE,
            "user_id": str(user_id), "from": start.isoformat(), "to": end.isoformat(), "rows": rows,
            "tables": tables,
            "note": ("Measured by hypertable_columnstore_stats over every compressed chunk of each hypertable; "
                     "the data is SYNTHETIC and labelled simulated.")}


# --- "device sent no data" timeline (research 4.3) -----------------------------------------------------
GAP_LIVENESS = timedelta(seconds=60)     # a sample keeps the device "live" this long (heartbeat_agg)
GAP_MAX_HOURS = 168
GAPS_LABEL = "device sent no data"
GAPS_NOTE = ("Read-only, for the care team (care profile). The server's check-in countdown is the authority; "
             "this view never changes a session.")


def _clip(ranges, lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    out = []
    for a, b in ranges:
        a, b = max(a, lo), min(b, hi)
        if b > a:
            out.append((a, b))
    return out


async def _gaps_plain(conn, user_id, lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    rows = await (await conn.execute(
        """SELECT t, nxt FROM (
             SELECT time AS t, lead(time) OVER (ORDER BY time) AS nxt FROM scrunchie_telemetry
              WHERE user_id = %s AND profile = 'care' AND time >= %s AND time <= %s) s
            WHERE nxt IS NULL OR nxt - t > %s ORDER BY t""",
        (user_id, lo - GAP_LIVENESS, hi, GAP_LIVENESS))).fetchall()
    if not rows:
        return [(lo, hi)]
    first = (await (await conn.execute(
        """SELECT min(time) AS t FROM scrunchie_telemetry
            WHERE user_id = %s AND profile = 'care' AND time >= %s AND time <= %s""",
        (user_id, lo - GAP_LIVENESS, hi))).fetchone())["t"]
    ranges = [(lo, first)]
    for r in rows:
        ranges.append((r["t"] + GAP_LIVENESS, r["nxt"] if r["nxt"] is not None else hi))
    return _clip(ranges, lo, hi)


def _dead_between(live: list[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    """[lo, hi] minus the live ranges (sorted)."""
    dead, cur = [], lo
    for a, b in live:
        if a > cur:
            dead.append((cur, a))
        cur = max(cur, b)
    if cur < hi:
        dead.append((cur, hi))
    return dead


async def _gaps_toolkit(conn, user_id, lo: datetime, hi: datetime) -> list[tuple[datetime, datetime]]:
    """The live ranges of the rolled-up hourly heartbeat_agg, and everything else in [lo, hi] is dead.

    The rollup only spans the hours that have samples, so its own dead_ranges() would miss the silence
    before the first of them and after the last; and it ends a sample's liveness at the end of the last
    hour with data, so the tail is extended to the last sample + 60 s, as the plain computation does."""
    row = await (await conn.execute(
        """SELECT rollup(hb) IS NULL AS empty FROM telemetry_heartbeat_1h
            WHERE user_id = %s AND bucket >= time_bucket(INTERVAL '1 hour', %s::timestamptz) AND bucket <= %s""",
        (user_id, lo, hi))).fetchone()
    if row is None or row["empty"]:
        return [(lo, hi)]
    rows = await (await conn.execute(
        """SELECT l.start AS a, l."end" AS b
             FROM (SELECT rollup(hb) AS hb FROM telemetry_heartbeat_1h
                    WHERE user_id = %s AND bucket >= time_bucket(INTERVAL '1 hour', %s::timestamptz)
                      AND bucket <= %s) x, live_ranges(x.hb) l
            ORDER BY l.start""", (user_id, lo, hi))).fetchall()
    live = [(r["a"], r["b"]) for r in rows]
    last = (await (await conn.execute(
        """SELECT max(time) AS t FROM scrunchie_telemetry
            WHERE user_id = %s AND profile = 'care' AND time >= %s AND time <= %s""",
        (user_id, lo - GAP_LIVENESS, hi))).fetchone())["t"]
    if live and last is not None and last + GAP_LIVENESS > live[-1][1]:
        live[-1] = (live[-1][0], last + GAP_LIVENESS)
    return _dead_between(_clip(live, lo, hi), lo, hi)


async def device_gaps(conn, user_id, *, hours: float = 24.0, method: Optional[str] = None) -> dict:
    """Where the device sent no data in the last `hours` (care profile): a sample counts as data for 60 s."""
    if not 0 < hours <= GAP_MAX_HOURS:
        raise ValueError(f"hours must be in (0, {GAP_MAX_HOURS}]")
    s = await setup(conn)
    method = method or s.get("gaps_method") or "plain_lag"
    hi = (await (await conn.execute("SELECT now() AS now")).fetchone())["now"]
    lo = hi - timedelta(hours=hours)
    ranges = await (_gaps_toolkit if method == "toolkit_heartbeat_agg" else _gaps_plain)(conn, user_id, lo, hi)
    dead = [{"start": a.isoformat(), "end": b.isoformat(), "seconds": round((b - a).total_seconds(), 1),
             "start_s_ago": round((hi - a).total_seconds(), 1), "end_s_ago": round((hi - b).total_seconds(), 1)}
            for a, b in ranges]
    return {"label": GAPS_LABEL, "method": method, "liveness_s": GAP_LIVENESS.total_seconds(),
            "profile": "care", "from": lo.isoformat(), "to": hi.isoformat(), "dead_ranges": dead,
            "num_gaps": len(dead), "downtime_s": round(sum(d["seconds"] for d in dead), 1), "note": GAPS_NOTE}
