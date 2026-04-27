-- Wave 6 multi-tenant groundwork.
-- Single-user mode keeps user_id NULL (system-owned, accessible to all).
-- Multi-user mode (future) writes integer user_id; rows with NULL stay public.
-- (migrations_runner.py wraps every migration in a savepoint — no BEGIN/COMMIT here.)

ALTER TABLE papers              ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
ALTER TABLE paper_notes         ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
ALTER TABLE paper_summaries     ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
ALTER TABLE paper_chunks        ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
ALTER TABLE paper_user_state    ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
ALTER TABLE pulse_cards         ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
ALTER TABLE paper_contradictions ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
ALTER TABLE paper_extractions   ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;

CREATE INDEX IF NOT EXISTS idx_papers_user
    ON papers(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_notes_user
    ON paper_notes(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_summaries_user
    ON paper_summaries(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_chunks_user
    ON paper_chunks(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_user_state_user
    ON paper_user_state(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pulse_cards_user
    ON pulse_cards(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_contradictions_user
    ON paper_contradictions(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_paper_extractions_user
    ON paper_extractions(user_id) WHERE user_id IS NOT NULL;

-- DB-004: shared updated_at trigger function (idempotent).
-- Attaches to tables that have an updated_at column; keeps it current on UPDATE.
-- NOTE: BEGIN/END kept inline to avoid migrations_runner.py's standalone
-- BEGIN-line stripper from chewing the PL/pgSQL function body.
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

-- user_config
DROP TRIGGER IF EXISTS trg_user_config_updated_at ON user_config;
CREATE TRIGGER trg_user_config_updated_at
    BEFORE UPDATE ON user_config
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- paper_contradictions
DROP TRIGGER IF EXISTS trg_paper_contradictions_updated_at ON paper_contradictions;
CREATE TRIGGER trg_paper_contradictions_updated_at
    BEFORE UPDATE ON paper_contradictions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- cards
DROP TRIGGER IF EXISTS trg_cards_updated_at ON cards;
CREATE TRIGGER trg_cards_updated_at
    BEFORE UPDATE ON cards
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- projects
DROP TRIGGER IF EXISTS trg_projects_updated_at ON projects;
CREATE TRIGGER trg_projects_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- tasks
DROP TRIGGER IF EXISTS trg_tasks_updated_at ON tasks;
CREATE TRIGGER trg_tasks_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- extraction_templates
DROP TRIGGER IF EXISTS trg_extraction_templates_updated_at ON extraction_templates;
CREATE TRIGGER trg_extraction_templates_updated_at
    BEFORE UPDATE ON extraction_templates
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
