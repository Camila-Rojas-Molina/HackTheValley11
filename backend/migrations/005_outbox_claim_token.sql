-- Which sender process claimed an outbox row. A restarted sender returns rows claimed by an
-- earlier process (killed mid-send) at once instead of waiting out their 30 s lease, and a sender
-- can only mark sent or release rows it still holds.
ALTER TABLE outbox ADD COLUMN claimed_by text;
