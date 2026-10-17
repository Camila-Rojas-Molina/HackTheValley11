-- One publicly active session per wearer. A session "ended" with the duress PIN looks
-- ended (public_state = 'ENDED') while its masked alert keeps escalating, so arming
-- again must start a fresh session next to it, exactly as after a real end.
DROP INDEX IF EXISTS sessions_one_active;
CREATE UNIQUE INDEX sessions_one_active ON sessions (user_id) WHERE state <> 'ENDED' AND public_state <> 'ENDED';
