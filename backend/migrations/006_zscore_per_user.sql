-- user_zscore_5m reads its per-minute CTE twice, so PostgreSQL materialized it for every user
-- and could not push the caller's `WHERE user_id = ...` into it: each request scanned every
-- wearer's raw rows on the plain path. NOT MATERIALIZED lets the filter reach the raw tables.
-- Same columns and results; the Toolkit variant is kept where Toolkit is installed.
DO $zs$
BEGIN
  IF EXISTS (SELECT 1 FROM timeseries_setup WHERE zscore_method = 'toolkit_stats_agg')
     AND EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb_toolkit') THEN
    BEGIN
      CREATE OR REPLACE VIEW user_zscore_5m AS
      WITH per_min AS NOT MATERIALIZED (
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
    EXCEPTION WHEN OTHERS THEN
      RAISE WARNING 'Toolkit z-score view not replaced (%); keeping the current one', SQLERRM;
    END;
  ELSE
    CREATE OR REPLACE VIEW user_zscore_5m AS
    WITH per_min AS NOT MATERIALIZED (
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
  END IF;
END
$zs$;
