"""Tiger Data layer (contracts/db.md C3): what /tiger/info reports, and the range refresh.

Schema in api/migrations/rb_002_timeseries.sql and rb_003_ai.sql. On TimescaleDB scan_metrics,
fe_element_results and ai_generation are hypertables and scan_metrics_monthly / fe_level_daily are real-time
continuous aggregates grouped by provenance; on plain PostgreSQL the same names are tables and views.

Unlike StillHere there is no retention policy, no ingest-age window and no refresh-window cap: a scan dated
months back is accepted, and after writing one the caller refreshes that scan's bucket range
(refresh_range), so the aggregates cover the whole history.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("rainbowbone.tiger")

BUDGET_BYTES = 314_572_800   # contracts/db.md "Total budget" (300 MiB)

HYPERTABLES = ("scan_metrics", "fe_element_results", "ai_generation")
# name -> (hypertable, bucket, group_by), as created in rb_002_timeseries.sql.
CAGGS: dict[str, tuple[str, str, list[str]]] = {
    "scan_metrics_monthly": ("scan_metrics", "1 month",
                             ["bucket", "patient_id", "site", "provenance", "mask_source", "lesion_model",
                              "lesion_grid", "statistic"]),
    "fe_level_daily": ("fe_element_results", "1 day",
                       ["bucket", "patient_id", "site", "provenance", "mask_source", "lesion_model", "run_id"]),
}


async def is_timescale(conn) -> bool:
    """True when scan_metrics really is a hypertable (the migration falls back to plain tables otherwise)."""
    row = await (await conn.execute(
        """SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') AS ext""")).fetchone()
    if not row["ext"]:
        return False
    row = await (await conn.execute(
        """SELECT EXISTS (SELECT 1 FROM timescaledb_information.hypertables
                           WHERE hypertable_name = 'scan_metrics') AS ht""")).fetchone()
    return bool(row["ht"])


def _pretty(n: Optional[int]) -> Optional[str]:
    if n is None:
        return None
    for unit in ("bytes", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n = n / 1024
    return None


async def timed(conn, sql: str, params=()) -> tuple[list[dict], float]:
    t0 = time.perf_counter()
    rows = await (await conn.execute(sql, params)).fetchall()
    return [dict(r) for r in rows], round((time.perf_counter() - t0) * 1000.0, 3)


# The same per-level question asked of the raw hypertable and of the monthly aggregate: real lesion and
# vertebra volumes per site and month, expert masks. Used for the raw vs aggregate query_ms pair.
RAW_LEVEL_SQL = """
SELECT date_trunc('month', time, 'UTC') AS bucket, site, avg(lesion_volume_mm3) AS lesion_mm3,
       avg(vertebra_volume_mm3) AS vertebra_mm3, count(*) AS n
  FROM scan_metrics
 WHERE patient_id = %s AND provenance = 'real_scan' AND mask_source = 'expert'
 GROUP BY 1, 2 ORDER BY 1, 2"""
AGG_LEVEL_SQL = """
SELECT bucket, site, sum(lesion_volume_mm3_sum) / nullif(sum(lesion_volume_mm3_n), 0) AS lesion_mm3,
       sum(vertebra_volume_mm3_sum) / nullif(sum(vertebra_volume_mm3_n), 0) AS vertebra_mm3, sum(n) AS n
  FROM scan_metrics_monthly
 WHERE patient_id = %s AND provenance = 'real_scan' AND mask_source = 'expert'
 GROUP BY 1, 2 ORDER BY 1, 2"""


async def query_timing(conn, patient_id: Optional[str]) -> dict:
    """{raw_ms, aggregate_ms} for the same per-level question on scan_metrics and on scan_metrics_monthly."""
    if patient_id is None:
        row = await (await conn.execute(
            "SELECT patient_id FROM patients WHERE cohort = 'demo' ORDER BY patient_id LIMIT 1")).fetchone()
        patient_id = row["patient_id"] if row else ""
    _, raw_ms = await timed(conn, RAW_LEVEL_SQL, (patient_id,))
    _, agg_ms = await timed(conn, AGG_LEVEL_SQL, (patient_id,))
    return {"raw_ms": raw_ms, "aggregate_ms": agg_ms}


async def _hypertable_rows(conn, ts: bool) -> list[dict]:
    out = []
    for name in HYPERTABLES:
        info = {"name": name, "chunk_interval": None, "chunks": None, "compressed_chunks": None,
                "rows_estimate": None, "before_bytes": None, "after_bytes": None}
        if ts:
            r = await (await conn.execute(
                """SELECT d.time_interval::text AS chunk_interval, h.num_chunks
                     FROM timescaledb_information.hypertables h
                     JOIN timescaledb_information.dimensions d USING (hypertable_schema, hypertable_name)
                    WHERE h.hypertable_name = %s AND d.dimension_number = 1""", (name,))).fetchone()
            if r:
                info["chunk_interval"] = r["chunk_interval"]
                info["chunks"] = r["num_chunks"]
                info["rows_estimate"] = (await (await conn.execute(
                    "SELECT approximate_row_count(%s::regclass) AS n", (name,))).fetchone())["n"]
                s = await (await conn.execute(
                    """SELECT number_compressed_chunks, before_compression_total_bytes, after_compression_total_bytes
                         FROM hypertable_columnstore_stats(%s::regclass)""", (name,))).fetchone()
                if s:
                    info["compressed_chunks"] = s["number_compressed_chunks"]
                    info["before_bytes"] = s["before_compression_total_bytes"]
                    info["after_bytes"] = s["after_compression_total_bytes"]
        else:
            r = await (await conn.execute(
                "SELECT greatest(reltuples, 0)::bigint AS n FROM pg_class WHERE oid = to_regclass(%s)",
                (name,))).fetchone()
            info["rows_estimate"] = r["n"] if r else None
        out.append(info)
    return out


async def _cagg_rows(conn, ts: bool) -> list[dict]:
    mat: dict[str, bool] = {}
    if ts:
        mat = {r["view_name"]: r["materialized_only"] for r in await (await conn.execute(
            "SELECT view_name, materialized_only FROM timescaledb_information.continuous_aggregates")).fetchall()}
    return [{"name": name, "hypertable": ht, "bucket": bucket, "group_by": list(group_by),
             "materialized_only": mat.get(name)} for name, (ht, bucket, group_by) in CAGGS.items()]


async def info(conn, patient_id: Optional[str] = None) -> dict:
    """The TigerInfo payload (C1), measured now."""
    ts = await is_timescale(conn)
    version = None
    if ts:
        row = await (await conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")).fetchone()
        version = row["extversion"] if row else None
    size = (await (await conn.execute(
        "SELECT pg_database_size(current_database()) AS b")).fetchone())["b"]
    return {
        "backend": "timescaledb" if ts else "postgres",
        "timescaledb_version": version,
        "hypertables": await _hypertable_rows(conn, ts),
        "continuous_aggregates": await _cagg_rows(conn, ts),
        "db_size_bytes": int(size),
        "db_size_pretty": _pretty(int(size)),
        "budget_bytes": BUDGET_BYTES,
        "query_ms": await query_timing(conn, patient_id),
        "source": "live",
        "measured_at": datetime.now(timezone.utc),
    }


def _month_floor(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    return datetime(t.year, t.month, 1, tzinfo=timezone.utc)


def _next_month(t: datetime) -> datetime:
    return datetime(t.year + (t.month == 12), t.month % 12 + 1, 1, tzinfo=timezone.utc)


def bucket_range(cagg: str, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Whole buckets covering [start, end]: the window refresh_continuous_aggregate needs."""
    if CAGGS[cagg][1] == "1 month":
        return _month_floor(start), _next_month(_month_floor(end))
    lo = start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    hi = end.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return lo, hi


async def refresh_range(conn, start: datetime, end: datetime, caggs: Optional[list[str]] = None) -> list[str]:
    """Materialize every continuous aggregate over the buckets covering [start, end]. TimescaleDB only.

    Needs an autocommit connection outside a transaction (the refresh procedure commits). There is no cap on
    how far back the range may reach: nothing in this schema is ever dropped by a policy.
    """
    if not await is_timescale(conn):
        return []
    done = []
    for name in caggs or list(CAGGS):
        lo, hi = bucket_range(name, start, end)
        await conn.execute("CALL refresh_continuous_aggregate(%s::regclass, %s::timestamptz, %s::timestamptz)",
                           (name, lo, hi))
        done.append(name)
    return done
