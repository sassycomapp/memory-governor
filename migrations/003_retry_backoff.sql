ALTER TABLE memory_events ADD COLUMN permanently_failed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE memory_events ADD COLUMN next_retry_after TIMESTAMPTZ;
