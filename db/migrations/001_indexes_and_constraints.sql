-- Migration 001: Add performance indexes and source_type uniqueness constraint
-- Run this on existing databases. Safe to run multiple times (uses IF NOT EXISTS).

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_papers_source_type ON papers(source_type);
CREATE INDEX IF NOT EXISTS idx_paper_user_state_status ON paper_user_state(status);

-- Ensure no duplicate source types exist before adding constraint:
-- SELECT source_type, COUNT(*) FROM paper_sources GROUP BY source_type HAVING COUNT(*) > 1;
-- If the above returns rows, deduplicate first.

-- Add color CHECK constraint to projects table
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
        WHERE t.relname = 'projects'
          AND a.attname = 'color'
          AND c.contype = 'c'
    ) THEN
        ALTER TABLE projects
            ADD CONSTRAINT chk_projects_color
            CHECK (color IS NULL OR color ~ '^#[0-9A-Fa-f]{6}$');
    END IF;
END $$;
