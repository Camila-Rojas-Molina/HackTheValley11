-- Personal timeline retention (BUILD_PLAN §6.3): a session's timeline is deleted 30 days after the session
-- ended unless the wearer pins it (POST /v1/sessions/{id}/pin). The job (app/engine.py expire_timelines,
-- run from the timer worker) uses the same erase flag as "Delete my data" and leaves a tombstone.
ALTER TABLE sessions ADD COLUMN pinned boolean NOT NULL DEFAULT false;
CREATE INDEX sessions_timeline_expiry ON sessions (ended_at) WHERE NOT pinned AND ended_at IS NOT NULL;
