-- Migration 003: project_papers junction table for linking papers to R&D projects
CREATE TABLE IF NOT EXISTS project_papers (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    paper_id    INTEGER NOT NULL REFERENCES papers(id)   ON DELETE CASCADE,
    notes       TEXT,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, paper_id)
);
CREATE INDEX IF NOT EXISTS idx_project_papers_project ON project_papers(project_id);
CREATE INDEX IF NOT EXISTS idx_project_papers_paper   ON project_papers(paper_id);
