-- Migration 013: Add missing index on task_paper_links(task_id)
-- Fixes ~1 minute cascade deletes due to full table scan
CREATE INDEX IF NOT EXISTS idx_task_paper_links_task ON task_paper_links(task_id);
