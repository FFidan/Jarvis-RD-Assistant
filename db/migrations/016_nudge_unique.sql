-- Add UNIQUE constraint to scheduled_nudges.nudge_type (idempotent)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'scheduled_nudges_nudge_type_key'
    ) THEN
        ALTER TABLE scheduled_nudges
            ADD CONSTRAINT scheduled_nudges_nudge_type_key UNIQUE (nudge_type);
    END IF;
END $$;
