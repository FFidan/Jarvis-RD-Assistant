-- 014: Performance indexes for common query patterns
-- NOTE: idx_paper_notes_paper and idx_paper_extractions_paper already exist in init.sql
CREATE INDEX IF NOT EXISTS idx_papers_published_date ON papers (published_date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_paper_chunks_paper_embedding ON paper_chunks (paper_id, embedding_id);
CREATE INDEX IF NOT EXISTS idx_cards_due_deck ON cards (due_at, deck_id);
