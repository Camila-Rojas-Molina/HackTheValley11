-- Masked duress keeps a whole twin session (states.py, "the twin"): what the wearer sees is the session
-- she would have had with the OK action (the real PIN, a plain squeeze), including its own alert, its
-- link state and its lost-contact flag. The escalation never reads the twin's alert.
--
-- alerts.cover: the twin's alert. When the mask opens on an alert she could already see, that row
-- becomes the twin's (same id) and the escalation continues on a copy of it.
ALTER TABLE alerts ADD COLUMN cover boolean NOT NULL DEFAULT false;
DROP INDEX IF EXISTS alerts_one_open;
CREATE UNIQUE INDEX alerts_one_open ON alerts (session_id) WHERE status IN ('OPEN', 'ACKED') AND NOT cover;
CREATE UNIQUE INDEX alerts_one_open_cover ON alerts (session_id) WHERE status IN ('OPEN', 'ACKED') AND cover;

-- The link state the twin shows (it stays as it was once the twin has ended), and whether the twin
-- has shared its lost-contact notice.
ALTER TABLE sessions ADD COLUMN public_link_state text;
UPDATE sessions SET public_link_state = link_state;
ALTER TABLE sessions ALTER COLUMN public_link_state SET DEFAULT 'OK';
ALTER TABLE sessions ALTER COLUMN public_link_state SET NOT NULL;
ALTER TABLE sessions ADD CONSTRAINT sessions_public_link_state_check
  CHECK (public_link_state IN ('OK', 'LOST_CONTACT'));
ALTER TABLE sessions ADD COLUMN cover_told boolean NOT NULL DEFAULT false;
