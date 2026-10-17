-- StillHere time-series layer (Tiger Data / TimescaleDB, with a plain PostgreSQL fallback).
--
-- Objects (same names and columns on both paths, so the app has one code path):
--   scrunchie_telemetry   raw accelerometer x/y/z (mg) + two FSR channels (mV), up to 50 Hz per device
--   hr_samples            heart rate from any source (created in 001; gains a profile column here)
--   hr_1m -> hr_1h -> hr_1d, telemetry_1m -> telemetry_1h -> telemetry_1d
--                         rollups: n, sum, sum of squares, min, max. Sums roll up exactly, so the
--                         hourly and daily layers give the same mean and SD as a raw rescan.
--   user_zscore_5m        per user and metric: the last 5 minutes against that user's own
--                         7-day baseline of 1-minute means (Toolkit stats_agg when installed)
--   timeseries_setup      one row: which path this database took, and why
--
-- TimescaleDB path (extension installed, or installable, in this database):
--   * both raw tables are hypertables with 1-day chunks;
--   * the rollups are hierarchical continuous aggregates (1 min on raw, 1 h on 1 min,
--     1 d on 1 h), real-time (materialized_only = false) with refresh policies;
--   * columnstore (compression) on raw chunks older than 1 day, segmented by user and profile;
--   * retention. Policies are per hypertable, so per-profile retention is two parts:
--       - a retention policy drops whole raw chunks after 30 days (the longest profile, 'care');
--       - a scheduled job (add_job -> stillhere_retention_job) deletes 'night_out' raw rows
--         older than 48 hours every hour, from both raw tables, compressed chunks included.
--     The 1-minute rollups keep 8 days (the z-score needs 7), hourly 90 days, daily are kept.
--     The 1-minute refresh window (2 h) is shorter than the 48 h raw window, so deleting raw
--     rows never rewrites summaries the policies have already materialized.
-- Plain PostgreSQL path: plain tables and ordinary views with the same columns. Nothing runs
-- on a schedule there; the app calls stillhere_prune_raw() itself (see app/timeseries.py).
-- Every TimescaleDB statement is inside a DO block behind a pg_extension check, and the whole
-- TimescaleDB setup runs in one sub-transaction: if any part of it fails, it is rolled back,
-- the plain objects are created instead and the reason is stored in timeseries_setup.note.
--
-- Nothing here can change a session, alert or timer: these objects are read by the dashboard.

-- 0. Extensions (best effort; absent on plain PostgreSQL).
DO $ext$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb') THEN
    BEGIN
      CREATE EXTENSION IF NOT EXISTS timescaledb;
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'timescaledb not enabled (%); using plain PostgreSQL objects', SQLERRM;
    END;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb_toolkit') THEN
    BEGIN
      CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit;
    EXCEPTION WHEN OTHERS THEN
      RAISE NOTICE 'timescaledb_toolkit not enabled (%)', SQLERRM;
    END;
  END IF;
END
$ext$;

-- 1. Raw tables. No unique keys: a hypertable's unique keys must include the time column.
ALTER TABLE hr_samples
  ADD COLUMN profile text NOT NULL DEFAULT 'night_out' CHECK (profile IN ('night_out', 'care'));

CREATE TABLE scrunchie_telemetry (
  time      timestamptz NOT NULL,
  user_id   uuid NOT NULL,
  device_id text,
  profile   text NOT NULL DEFAULT 'night_out' CHECK (profile IN ('night_out', 'care')),
  simulated boolean NOT NULL DEFAULT false,
  ax_mg     real NOT NULL,
  ay_mg     real NOT NULL,
  az_mg     real NOT NULL,
  fsr_a_mv  real,
  fsr_b_mv  real
);
CREATE INDEX scrunchie_telemetry_user_time ON scrunchie_telemetry (user_id, time DESC);

-- Ingest dedupe: a retried batch (same batch_id) is acknowledged without inserting twice.
CREATE TABLE telemetry_batches (
  user_id     uuid NOT NULL,
  batch_id    text NOT NULL,
  device_id   text,
  inserted    int NOT NULL,
  dropped     int NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, batch_id)
);
CREATE INDEX telemetry_batches_received ON telemetry_batches (received_at);

-- ENMO (Euclidean norm minus one g, clipped at 0), in mg.
CREATE FUNCTION stillhere_enmo_mg(ax real, ay real, az real) RETURNS double precision
  LANGUAGE sql IMMUTABLE PARALLEL SAFE
  AS $fn$ SELECT greatest(sqrt(ax::float8 * ax + ay::float8 * ay + az::float8 * az) - 1000.0, 0.0) $fn$;

CREATE TABLE timeseries_setup (
  id            boolean PRIMARY KEY DEFAULT true CHECK (id),
  mode          text NOT NULL CHECK (mode IN ('timescaledb', 'plain')),
  zscore_method text NOT NULL DEFAULT 'plain_avg_stddev',
  note          text,
  set_up_at     timestamptz NOT NULL DEFAULT now()
);

-- 2. Per-profile raw retention. On TimescaleDB the scheduled job calls this with
--    care_after => NULL (the chunk retention policy covers 30 days); on plain PostgreSQL the
--    app calls it with both bounds.
CREATE FUNCTION stillhere_prune_raw(night_out_after interval DEFAULT '48 hours',
                                    care_after interval DEFAULT '30 days')
RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
  DELETE FROM scrunchie_telemetry WHERE profile = 'night_out' AND time < now() - night_out_after;
  DELETE FROM hr_samples WHERE profile = 'night_out' AND time < now() - night_out_after;
  IF care_after IS NOT NULL THEN
    DELETE FROM scrunchie_telemetry WHERE time < now() - care_after;
    DELETE FROM hr_samples WHERE time < now() - care_after;
  END IF;
  DELETE FROM telemetry_batches WHERE received_at < now() - night_out_after;
END
$fn$;

-- Job entry point for TimescaleDB's scheduler (add_job needs a (job_id, config) signature).
CREATE PROCEDURE stillhere_retention_job(job_id int, config jsonb) LANGUAGE plpgsql AS $pr$
BEGIN
  PERFORM stillhere_prune_raw(coalesce((config ->> 'night_out_after')::interval, INTERVAL '48 hours'),
                              (config ->> 'care_after')::interval);
END
$pr$;

-- 3. Hypertables, continuous aggregates and policies, or the plain views.
DO $tsdb$
DECLARE
  use_ts boolean := EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb');
  why text;
BEGIN
  IF use_ts THEN
    BEGIN
      PERFORM create_hypertable('hr_samples', by_range('time', INTERVAL '1 day'), migrate_data => true);
      PERFORM create_hypertable('scrunchie_telemetry', by_range('time', INTERVAL '1 day'));

      ALTER TABLE hr_samples SET (timescaledb.enable_columnstore = true,
                                  timescaledb.segmentby = 'user_id, profile',
                                  timescaledb.orderby = 'time DESC');
      ALTER TABLE scrunchie_telemetry SET (timescaledb.enable_columnstore = true,
                                           timescaledb.segmentby = 'user_id, profile',
                                           timescaledb.orderby = 'time DESC');
      CALL add_columnstore_policy('hr_samples', after => INTERVAL '1 day', if_not_exists => true);
      CALL add_columnstore_policy('scrunchie_telemetry', after => INTERVAL '1 day', if_not_exists => true);

      -- GROUP BY repeats the time_bucket expression: in the upper layers "bucket" alone would
      -- name the input column (the finer bucket), not the output.
      CREATE MATERIALIZED VIEW hr_1m
        WITH (timescaledb.continuous, timescaledb.materialized_only = false,
              timescaledb.chunk_interval = '1 day') AS
        SELECT time_bucket(INTERVAL '1 minute', time) AS bucket, user_id, profile, simulated,
               count(*) AS n,
               sum(bpm::float8) AS bpm_sum,
               sum(bpm::float8 * bpm) AS bpm_sumsq,
               min(bpm::float8) AS bpm_min,
               max(bpm::float8) AS bpm_max
          FROM hr_samples
         GROUP BY time_bucket(INTERVAL '1 minute', time), user_id, profile, simulated
        WITH NO DATA;
      CREATE MATERIALIZED VIEW hr_1h
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket(INTERVAL '1 hour', bucket) AS bucket, user_id, profile, simulated,
               sum(n)::bigint AS n, sum(bpm_sum) AS bpm_sum, sum(bpm_sumsq) AS bpm_sumsq,
               min(bpm_min) AS bpm_min, max(bpm_max) AS bpm_max
          FROM hr_1m
         GROUP BY time_bucket(INTERVAL '1 hour', bucket), user_id, profile, simulated
        WITH NO DATA;
      CREATE MATERIALIZED VIEW hr_1d
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket(INTERVAL '1 day', bucket) AS bucket, user_id, profile, simulated,
               sum(n)::bigint AS n, sum(bpm_sum) AS bpm_sum, sum(bpm_sumsq) AS bpm_sumsq,
               min(bpm_min) AS bpm_min, max(bpm_max) AS bpm_max
          FROM hr_1h
         GROUP BY time_bucket(INTERVAL '1 day', bucket), user_id, profile, simulated
        WITH NO DATA;

      CREATE MATERIALIZED VIEW telemetry_1m
        WITH (timescaledb.continuous, timescaledb.materialized_only = false,
              timescaledb.chunk_interval = '1 day') AS
        SELECT time_bucket(INTERVAL '1 minute', time) AS bucket, user_id, device_id, profile, simulated,
               count(*) AS n,
               sum(ax_mg::float8) AS ax_sum, sum(ay_mg::float8) AS ay_sum, sum(az_mg::float8) AS az_sum,
               sum(stillhere_enmo_mg(ax_mg, ay_mg, az_mg)) AS enmo_sum,
               sum(stillhere_enmo_mg(ax_mg, ay_mg, az_mg) ^ 2) AS enmo_sumsq,
               max(stillhere_enmo_mg(ax_mg, ay_mg, az_mg)) AS enmo_max,
               max(fsr_a_mv::float8) AS fsr_a_max,
               max(fsr_b_mv::float8) AS fsr_b_max
          FROM scrunchie_telemetry
         GROUP BY time_bucket(INTERVAL '1 minute', time), user_id, device_id, profile, simulated
        WITH NO DATA;
      CREATE MATERIALIZED VIEW telemetry_1h
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket(INTERVAL '1 hour', bucket) AS bucket, user_id, device_id, profile, simulated,
               sum(n)::bigint AS n, sum(ax_sum) AS ax_sum, sum(ay_sum) AS ay_sum, sum(az_sum) AS az_sum,
               sum(enmo_sum) AS enmo_sum, sum(enmo_sumsq) AS enmo_sumsq, max(enmo_max) AS enmo_max,
               max(fsr_a_max) AS fsr_a_max, max(fsr_b_max) AS fsr_b_max
          FROM telemetry_1m
         GROUP BY time_bucket(INTERVAL '1 hour', bucket), user_id, device_id, profile, simulated
        WITH NO DATA;
      CREATE MATERIALIZED VIEW telemetry_1d
        WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
        SELECT time_bucket(INTERVAL '1 day', bucket) AS bucket, user_id, device_id, profile, simulated,
               sum(n)::bigint AS n, sum(ax_sum) AS ax_sum, sum(ay_sum) AS ay_sum, sum(az_sum) AS az_sum,
               sum(enmo_sum) AS enmo_sum, sum(enmo_sumsq) AS enmo_sumsq, max(enmo_max) AS enmo_max,
               max(fsr_a_max) AS fsr_a_max, max(fsr_b_max) AS fsr_b_max
          FROM telemetry_1h
         GROUP BY time_bucket(INTERVAL '1 day', bucket), user_id, device_id, profile, simulated
        WITH NO DATA;

      -- Refresh windows cover at least two buckets; each layer's window stays inside the
      -- retention of the layer it reads (raw 48 h / 30 d, 1 min 8 d, 1 h 90 d).
      PERFORM add_continuous_aggregate_policy('hr_1m', start_offset => INTERVAL '2 hours',
              end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute');
      PERFORM add_continuous_aggregate_policy('hr_1h', start_offset => INTERVAL '1 day',
              end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '30 minutes');
      PERFORM add_continuous_aggregate_policy('hr_1d', start_offset => INTERVAL '3 days',
              end_offset => INTERVAL '1 day', schedule_interval => INTERVAL '1 hour');
      PERFORM add_continuous_aggregate_policy('telemetry_1m', start_offset => INTERVAL '2 hours',
              end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute');
      PERFORM add_continuous_aggregate_policy('telemetry_1h', start_offset => INTERVAL '1 day',
              end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '30 minutes');
      PERFORM add_continuous_aggregate_policy('telemetry_1d', start_offset => INTERVAL '3 days',
              end_offset => INTERVAL '1 day', schedule_interval => INTERVAL '1 hour');

      PERFORM add_retention_policy('hr_samples', drop_after => INTERVAL '30 days');
      PERFORM add_retention_policy('scrunchie_telemetry', drop_after => INTERVAL '30 days');
      PERFORM add_retention_policy('hr_1m', drop_after => INTERVAL '8 days');
      PERFORM add_retention_policy('telemetry_1m', drop_after => INTERVAL '8 days');
      PERFORM add_retention_policy('hr_1h', drop_after => INTERVAL '90 days');
      PERFORM add_retention_policy('telemetry_1h', drop_after => INTERVAL '90 days');

      PERFORM add_job('stillhere_retention_job', INTERVAL '1 hour',
                      config => jsonb_build_object('night_out_after', '48 hours'));
    EXCEPTION WHEN OTHERS THEN
      why := SQLERRM;
      RAISE WARNING 'TimescaleDB setup failed (%); using plain PostgreSQL objects instead', why;
      use_ts := false;
    END;
  END IF;

  IF NOT use_ts THEN
    CREATE VIEW hr_1m AS
      SELECT date_trunc('minute', time, 'UTC') AS bucket, user_id, profile, simulated,
             count(*) AS n,
             sum(bpm::float8) AS bpm_sum,
             sum(bpm::float8 * bpm) AS bpm_sumsq,
             min(bpm::float8) AS bpm_min,
             max(bpm::float8) AS bpm_max
        FROM hr_samples
       GROUP BY date_trunc('minute', time, 'UTC'), user_id, profile, simulated;
    CREATE VIEW hr_1h AS
      SELECT date_trunc('hour', bucket, 'UTC') AS bucket, user_id, profile, simulated,
             sum(n)::bigint AS n, sum(bpm_sum) AS bpm_sum, sum(bpm_sumsq) AS bpm_sumsq,
             min(bpm_min) AS bpm_min, max(bpm_max) AS bpm_max
        FROM hr_1m
       GROUP BY date_trunc('hour', bucket, 'UTC'), user_id, profile, simulated;
    CREATE VIEW hr_1d AS
      SELECT date_trunc('day', bucket, 'UTC') AS bucket, user_id, profile, simulated,
             sum(n)::bigint AS n, sum(bpm_sum) AS bpm_sum, sum(bpm_sumsq) AS bpm_sumsq,
             min(bpm_min) AS bpm_min, max(bpm_max) AS bpm_max
        FROM hr_1h
       GROUP BY date_trunc('day', bucket, 'UTC'), user_id, profile, simulated;

    CREATE VIEW telemetry_1m AS
      SELECT date_trunc('minute', time, 'UTC') AS bucket, user_id, device_id, profile, simulated,
             count(*) AS n,
             sum(ax_mg::float8) AS ax_sum, sum(ay_mg::float8) AS ay_sum, sum(az_mg::float8) AS az_sum,
             sum(stillhere_enmo_mg(ax_mg, ay_mg, az_mg)) AS enmo_sum,
             sum(stillhere_enmo_mg(ax_mg, ay_mg, az_mg) ^ 2) AS enmo_sumsq,
             max(stillhere_enmo_mg(ax_mg, ay_mg, az_mg)) AS enmo_max,
             max(fsr_a_mv::float8) AS fsr_a_max,
             max(fsr_b_mv::float8) AS fsr_b_max
        FROM scrunchie_telemetry
       GROUP BY date_trunc('minute', time, 'UTC'), user_id, device_id, profile, simulated;
    CREATE VIEW telemetry_1h AS
      SELECT date_trunc('hour', bucket, 'UTC') AS bucket, user_id, device_id, profile, simulated,
             sum(n)::bigint AS n, sum(ax_sum) AS ax_sum, sum(ay_sum) AS ay_sum, sum(az_sum) AS az_sum,
             sum(enmo_sum) AS enmo_sum, sum(enmo_sumsq) AS enmo_sumsq, max(enmo_max) AS enmo_max,
             max(fsr_a_max) AS fsr_a_max, max(fsr_b_max) AS fsr_b_max
        FROM telemetry_1m
       GROUP BY date_trunc('hour', bucket, 'UTC'), user_id, device_id, profile, simulated;
    CREATE VIEW telemetry_1d AS
      SELECT date_trunc('day', bucket, 'UTC') AS bucket, user_id, device_id, profile, simulated,
             sum(n)::bigint AS n, sum(ax_sum) AS ax_sum, sum(ay_sum) AS ay_sum, sum(az_sum) AS az_sum,
             sum(enmo_sum) AS enmo_sum, sum(enmo_sumsq) AS enmo_sumsq, max(enmo_max) AS enmo_max,
             max(fsr_a_max) AS fsr_a_max, max(fsr_b_max) AS fsr_b_max
        FROM telemetry_1h
       GROUP BY date_trunc('day', bucket, 'UTC'), user_id, device_id, profile, simulated;
  END IF;

  INSERT INTO timeseries_setup (mode, note)
  VALUES (CASE WHEN use_ts THEN 'timescaledb' ELSE 'plain' END,
          CASE WHEN why IS NOT NULL THEN 'TimescaleDB setup failed: ' || why END);
END
$tsdb$;

-- 4. Z-score of the last 5 minutes against the user's own 7-day baseline, per metric:
--      hr_bpm          heart rate
--      motion_enmo_mg  movement (ENMO) from the scrunchie accelerometer
--    recent_mean   = mean of every sample in the last 5 minutes
--    baseline_*    = mean / sample SD / count of the 1-minute means in the 7 days before that
--    z             = (recent_mean - baseline_mean) / baseline_sd, NULL under 30 baseline minutes
--    Informational: it is shown on the dashboard and never changes a session or alerts anyone.
CREATE VIEW user_zscore_5m AS
WITH per_min AS (
  SELECT user_id, 'hr_bpm'::text AS metric, bucket,
         sum(bpm_sum) AS s, sum(n)::bigint AS n, bool_or(simulated) AS simulated
    FROM hr_1m
   WHERE bucket >= now() - INTERVAL '7 days 5 minutes'
   GROUP BY user_id, bucket
  UNION ALL
  SELECT user_id, 'motion_enmo_mg'::text, bucket,
         sum(enmo_sum), sum(n)::bigint, bool_or(simulated)
    FROM telemetry_1m
   WHERE bucket >= now() - INTERVAL '7 days 5 minutes'
   GROUP BY user_id, bucket
), recent AS (
  SELECT user_id, metric, sum(s) / sum(n) AS recent_mean, count(*) AS recent_minutes,
         sum(n)::bigint AS recent_samples, max(bucket) AS last_bucket, bool_or(simulated) AS simulated
    FROM per_min
   WHERE bucket >= now() - INTERVAL '5 minutes'
   GROUP BY user_id, metric
), baseline AS (
  SELECT user_id, metric, avg(s / n) AS baseline_mean, stddev_samp(s / n) AS baseline_sd,
         count(*) AS baseline_minutes, bool_or(simulated) AS simulated
    FROM per_min
   WHERE bucket < now() - INTERVAL '5 minutes'
   GROUP BY user_id, metric
)
SELECT r.user_id, r.metric, r.last_bucket, r.recent_mean, r.recent_minutes, r.recent_samples,
       b.baseline_mean, b.baseline_sd, coalesce(b.baseline_minutes, 0) AS baseline_minutes,
       CASE WHEN b.baseline_minutes >= 30 AND b.baseline_sd > 0
            THEN (r.recent_mean - b.baseline_mean) / b.baseline_sd END AS z,
       r.simulated OR coalesce(b.simulated, false) AS simulated,
       'plain_avg_stddev'::text AS method
  FROM recent r
  LEFT JOIN baseline b USING (user_id, metric);

-- Same view through Toolkit's stats_agg summary when timescaledb_toolkit is installed.
DO $tk$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb_toolkit') THEN
    BEGIN
      CREATE OR REPLACE VIEW user_zscore_5m AS
      WITH per_min AS (
        SELECT user_id, 'hr_bpm'::text AS metric, bucket,
               sum(bpm_sum) AS s, sum(n)::bigint AS n, bool_or(simulated) AS simulated
          FROM hr_1m
         WHERE bucket >= now() - INTERVAL '7 days 5 minutes'
         GROUP BY user_id, bucket
        UNION ALL
        SELECT user_id, 'motion_enmo_mg'::text, bucket,
               sum(enmo_sum), sum(n)::bigint, bool_or(simulated)
          FROM telemetry_1m
         WHERE bucket >= now() - INTERVAL '7 days 5 minutes'
         GROUP BY user_id, bucket
      ), recent AS (
        SELECT user_id, metric, sum(s) / sum(n) AS recent_mean, count(*) AS recent_minutes,
               sum(n)::bigint AS recent_samples, max(bucket) AS last_bucket, bool_or(simulated) AS simulated
          FROM per_min
         WHERE bucket >= now() - INTERVAL '5 minutes'
         GROUP BY user_id, metric
      ), baseline AS (
        SELECT user_id, metric, stats_agg(s / n) AS stats, bool_or(simulated) AS simulated
          FROM per_min
         WHERE bucket < now() - INTERVAL '5 minutes'
         GROUP BY user_id, metric
      )
      SELECT r.user_id, r.metric, r.last_bucket, r.recent_mean, r.recent_minutes, r.recent_samples,
             average(b.stats) AS baseline_mean, stddev(b.stats) AS baseline_sd,
             coalesce(num_vals(b.stats), 0) AS baseline_minutes,
             CASE WHEN num_vals(b.stats) >= 30 AND stddev(b.stats) > 0
                  THEN (r.recent_mean - average(b.stats)) / stddev(b.stats) END AS z,
             r.simulated OR coalesce(b.simulated, false) AS simulated,
             'toolkit_stats_agg'::text AS method
        FROM recent r
        LEFT JOIN baseline b USING (user_id, metric);
      UPDATE timeseries_setup SET zscore_method = 'toolkit_stats_agg';
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'Toolkit z-score view not created (%); keeping avg/stddev', SQLERRM;
    END;
  END IF;
END
$tk$;
