-- Senior mode, research 1.5: the level no longer follows the score. A single rule is
-- 'worth a look this shift'; 'review this shift' needs 2 different rules in the last 3 days.
UPDATE care_alerts SET level = CASE level WHEN 'watch' THEN 'worth a look this shift'
                                          WHEN 'check today' THEN 'review this shift'
                                          ELSE level END;
ALTER TABLE care_alerts DROP CONSTRAINT IF EXISTS care_alerts_level_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_level_check
  CHECK (level IN ('low', 'worth a look this shift', 'review this shift', 'not enough data'));
