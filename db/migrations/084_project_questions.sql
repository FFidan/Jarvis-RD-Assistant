-- 084: per-project open research questions (Projects IA redesign — §4a)
--
-- A separate table (not a JSONB column on projects) keeps transient research
-- artefacts out of project metadata. Scoped by both project_id (FK, cascade on
-- project delete) and user_id (ownership; checked at the project row, mirroring
-- list_project_papers). Idempotent: safe to re-run (runner applies once, but the
-- IF NOT EXISTS guards make a manual re-apply harmless).

CREATE TABLE IF NOT EXISTS project_questions (
    id          SERIAL PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_project_questions_project ON project_questions(project_id);
CREATE INDEX IF NOT EXISTS idx_project_questions_user    ON project_questions(user_id)
    WHERE user_id IS NOT NULL;

COMMENT ON TABLE project_questions IS
    'Per-project open research questions (Projects document-pane § OPEN QUESTIONS).';
