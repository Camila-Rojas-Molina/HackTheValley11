-- Rollups follow the privacy rules of the raw rows they summarise.
--
-- TimescaleDB: continuous aggregates have no per-profile retention policy (a policy drops whole chunks),
-- so a night out's per-minute, hourly and daily summaries would outlive its 48 h raw limit (hourly 90 days,
-- daily for good) and "Delete my data" would leave them behind. Two functions close that:
--   stillhere_prune_rollups(after)        deletes 'night_out' rows older than `after` (8 days: the
--                                          z-score baseline needs 7) from every rollup's materialized
--                                          hypertable; the scheduled stillhere_retention_job now calls it
--                                          too. 'care' rollups keep their layer's own retention.
--   stillhere_erase_user_rollups(user)    deletes one wearer's rows from every rollup (DELETE /v1/me/data).
-- Plain PostgreSQL: the rollups are views over the raw rows, so they already follow the raw retention and
-- the erase; both functions return 0 there.

CREATE FUNCTION stillhere_rollup_tables()
RETURNS TABLE (view_name text, mat text, has_profile boolean) LANGUAGE plpgsql STABLE AS $fn$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    RETURN;
  END IF;
  RETURN QUERY EXECUTE $q$
    SELECT ca.view_name::text,
           format('%I.%I', ca.materialization_hypertable_schema, ca.materialization_hypertable_name),
           EXISTS (SELECT 1 FROM information_schema.columns c
                    WHERE c.table_schema = ca.materialization_hypertable_schema
                      AND c.table_name = ca.materialization_hypertable_name AND c.column_name = 'profile')
      FROM timescaledb_information.continuous_aggregates ca
     WHERE ca.view_schema = current_schema()
       AND EXISTS (SELECT 1 FROM information_schema.columns c
                    WHERE c.table_schema = ca.materialization_hypertable_schema
                      AND c.table_name = ca.materialization_hypertable_name AND c.column_name = 'user_id')
     ORDER BY ca.view_name$q$;
END
$fn$;

CREATE FUNCTION stillhere_prune_rollups(night_out_after interval DEFAULT '8 days')
RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
  r record;
  n bigint;
  total bigint := 0;
BEGIN
  FOR r IN SELECT * FROM stillhere_rollup_tables() WHERE has_profile LOOP
    EXECUTE format('WITH d AS (DELETE FROM %s WHERE profile = %L AND bucket < now() - $1 RETURNING 1)
                    SELECT count(*) FROM d', r.mat, 'night_out')
      INTO n USING night_out_after;
    total := total + n;
  END LOOP;
  RETURN total;
END
$fn$;

CREATE FUNCTION stillhere_erase_user_rollups(target uuid)
RETURNS bigint LANGUAGE plpgsql AS $fn$
DECLARE
  r record;
  n bigint;
  total bigint := 0;
BEGIN
  FOR r IN SELECT * FROM stillhere_rollup_tables() LOOP
    EXECUTE format('WITH d AS (DELETE FROM %s WHERE user_id = $1 RETURNING 1) SELECT count(*) FROM d', r.mat)
      INTO n USING target;
    total := total + n;
  END LOOP;
  RETURN total;
END
$fn$;

CREATE OR REPLACE PROCEDURE stillhere_retention_job(job_id int, config jsonb) LANGUAGE plpgsql AS $pr$
BEGIN
  PERFORM stillhere_prune_raw(coalesce((config ->> 'night_out_after')::interval, INTERVAL '48 hours'),
                              (config ->> 'care_after')::interval);
  PERFORM stillhere_prune_rollups(coalesce((config ->> 'night_out_rollups_after')::interval, INTERVAL '8 days'));
END
$pr$;

-- The job's config shows both limits (GET /v1/telemetry/info lists it).
DO $job$
DECLARE
  j int;
BEGIN
  IF EXISTS (SELECT 1 FROM timeseries_setup WHERE mode = 'timescaledb') THEN
    FOR j IN EXECUTE $q$SELECT job_id FROM timescaledb_information.jobs
                        WHERE proc_name = 'stillhere_retention_job'$q$ LOOP
      EXECUTE 'SELECT alter_job($1, config => $2)'
        USING j, jsonb_build_object('night_out_after', '48 hours', 'night_out_rollups_after', '8 days');
    END LOOP;
  END IF;
END
$job$;
