-- Migration 009: Track embedding model per chunk

ALTER TABLE paper_chunks ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(100);
UPDATE paper_chunks SET embedding_model = 'qwen3-embedding:0.6b' WHERE embedding_model IS NULL;
