-- Senior mode, research 1.7: the buttons on a flagged card (Useful / Not useful / Already known /
-- Snooze 3 days), stored against the card they were pressed on. snooze_until mutes pushes for that
-- resident (research 1.6). Synthetic residents only, like every other care row.
CREATE TABLE care_feedback (
  id            bigserial PRIMARY KEY,
  resident_id   text NOT NULL REFERENCES care_residents(id) ON DELETE CASCADE,
  as_of         date NOT NULL,
  action        text NOT NULL CHECK (action IN ('useful', 'not_useful', 'already_known', 'snooze_3d')),
  reason        text CHECK (char_length(reason) <= 200),
  snooze_until  timestamptz,
  synthetic     boolean NOT NULL DEFAULT true CHECK (synthetic),
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX care_feedback_resident ON care_feedback (resident_id, created_at DESC);
