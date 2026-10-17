-- Telegram updates are deduplicated per bot by (bot, update_id), not by "update_id below the stored
-- offset": a different bot token, or Telegram restarting its update ids after a week without updates,
-- gives ids below an old offset, and those must still be processed (exactly once).
CREATE TABLE tg_updates (
  bot          text NOT NULL,
  update_id    bigint NOT NULL,
  processed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (bot, update_id)
);
CREATE INDEX tg_updates_processed ON tg_updates (processed_at);
-- An update id is unique only within one bot's epoch: tg_updates guarantees a tap is recorded once.
ALTER TABLE acks DROP CONSTRAINT IF EXISTS acks_telegram_update_id_key;
CREATE INDEX acks_telegram_update ON acks (telegram_update_id) WHERE telegram_update_id IS NOT NULL;
