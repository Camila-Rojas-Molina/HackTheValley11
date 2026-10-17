-- Senior mode: a card whose rules could not score because the device data stopped is
-- 'not enough data', listed with the alerts, instead of 'low' ("continue routine care").
ALTER TABLE care_alerts DROP CONSTRAINT IF EXISTS care_alerts_level_check;
ALTER TABLE care_alerts ADD CONSTRAINT care_alerts_level_check
  CHECK (level IN ('low', 'watch', 'check today', 'not enough data'));
