-- Migration 048 — papers.discovery_origin column.
-- Spec: docs/specs/2026-04-29-paper-lifecycle-redesign.md §3.2
--
-- Adds a single immutable column tracking how each paper first entered the
-- system. Used by the frontend to conditionally render feedback (👍/👎) buttons
-- (only on machine-recommended papers, not on user-initiated ones — see spec §5.2).
--
-- Backfill order is significant:
--   1. Papers linked from pulse_cards → 'pulse'
--   2. Papers in paper_recommendations (and not already 'pulse') → 'recommender'
--   3. All others stay at default 'user_initiated'

ALTER TABLE papers
    ADD COLUMN IF NOT EXISTS discovery_origin TEXT NOT NULL DEFAULT 'user_initiated'
        CHECK (discovery_origin IN ('user_initiated', 'pulse', 'recommender', 'citation_batch'));

-- Backfill from pulse_cards (Pulse-discovered papers).
UPDATE papers SET discovery_origin = 'pulse'
WHERE id IN (SELECT DISTINCT paper_id FROM pulse_cards)
  AND discovery_origin = 'user_initiated';

-- Backfill from paper_recommendations (recommender-surfaced papers that were
-- not already tagged 'pulse'). Order: pulse wins over recommender if both apply.
UPDATE papers SET discovery_origin = 'recommender'
WHERE id IN (SELECT DISTINCT paper_id FROM paper_recommendations)
  AND discovery_origin = 'user_initiated';

-- Index for feedback-button visibility queries (frontend reads this on every
-- paper-list render to decide whether to mount FeedbackButtons).
CREATE INDEX IF NOT EXISTS idx_papers_discovery_origin ON papers(discovery_origin);

COMMENT ON COLUMN papers.discovery_origin IS
    'How the paper first entered the system. Immutable after insert (no business code may UPDATE this column). Values: user_initiated (manual search/upload/Zotero/citation graph), pulse (overnight discovery), recommender (paper_recommendations), citation_batch (citation graph batch save). Frontend uses this to conditionally render 👍/👎 feedback buttons only on machine-recommended papers (see redesign spec §5.2).';
