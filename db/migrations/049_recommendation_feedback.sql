-- Migration 049 — recommendation_feedback table + drop pulse_ratings.
-- Spec: docs/specs/2026-04-29-paper-lifecycle-redesign.md §3.3
--
-- Creates the single source of truth for recommendation-quality user signals
-- (👍 / 👎 / "trash & reject" combined). Migrates non-lifecycle rows from the
-- legacy pulse_ratings table, then drops the legacy table — pulse_ratings was
-- partly lifecycle (save/dismiss writing into paper_user_state) and partly
-- feedback (up/down). Phase A separates the two cleanly: lifecycle is in
-- paper_user_state.state (mig 047), feedback is in this table.

CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id              BIGSERIAL PRIMARY KEY,
    paper_id        BIGINT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    user_id         BIGINT,                                       -- NULL = single-tenant
    signal          TEXT NOT NULL CHECK (signal IN ('positive', 'negative')),
    source          TEXT NOT NULL CHECK (source IN (
        'pulse_thumbs',          -- 👍/👎 on Pulse Deck card
        'feed_thumbs',           -- 👍/👎 on Inbox/Library row (Pulse-origin only)
        'paper_detail_thumbs',   -- 👍/👎 on Paper Detail page
        'dismiss_combined'       -- 🗑+👎 combined button
    )),
    topic_id        BIGINT REFERENCES topics(id) ON DELETE SET NULL,
    reason          TEXT,                                         -- optional free-text (Paper Detail only)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT recommendation_feedback_paper_user_source_uniq
        UNIQUE NULLS NOT DISTINCT (paper_id, user_id, source)     -- replace via upsert
);

CREATE INDEX IF NOT EXISTS recommendation_feedback_paper_idx
    ON recommendation_feedback (paper_id);
CREATE INDEX IF NOT EXISTS recommendation_feedback_signal_recent_idx
    ON recommendation_feedback (signal, created_at DESC);
CREATE INDEX IF NOT EXISTS recommendation_feedback_topic_idx
    ON recommendation_feedback (topic_id) WHERE topic_id IS NOT NULL;

-- Migrate existing pulse_ratings into recommendation_feedback BEFORE dropping
-- the source table. Legacy mapping per spec §3.3:
--   pulse 'save'    → lifecycle-only in new model, NOT feedback. Skip.
--   pulse 'open'    → no signal in new model. Skip.
--   pulse 'up'      → positive thumbs.
--   pulse 'down'    → negative thumbs.
--   pulse 'dismiss' → was lifecycle+feedback combined → maps to 'dismiss_combined'.
--
-- Wrapped in IF EXISTS so re-running this migration after the DROP below is
-- a no-op (the migration runner should not re-apply, but defence in depth).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = 'public' AND table_name = 'pulse_ratings') THEN
        INSERT INTO recommendation_feedback (paper_id, user_id, signal, source, created_at)
        SELECT
            pr.paper_id,
            pr.user_id,
            CASE WHEN pr.rating = 'up' THEN 'positive'
                 WHEN pr.rating IN ('down', 'dismiss') THEN 'negative'
            END AS signal,
            CASE WHEN pr.rating IN ('up', 'down') THEN 'pulse_thumbs'
                 WHEN pr.rating = 'dismiss' THEN 'dismiss_combined'
            END AS source,
            pr.created_at
        FROM pulse_ratings pr
        WHERE pr.rating IN ('up', 'down', 'dismiss')
        ON CONFLICT (paper_id, user_id, source) DO NOTHING;
    END IF;
END $$;

-- Drop the legacy table — single source of truth going forward. Pulse-internal
-- analytics (deck composition, rating distribution) is reconstructable from
-- pulse_decks ⨝ pulse_cards ⨝ recommendation_feedback by (paper_id,
-- created_at::date). No data lost, just deduplicated.
DROP TABLE IF EXISTS pulse_ratings;

COMMENT ON TABLE recommendation_feedback IS
    'Single source of truth for recommendation-quality user signals. Pulse stage-2 reranker (L1), pulse stage-1 cosine penalty (L2), and recommender hard exclusion + topic dampening (L3) all read from this table. Decoupled from paper_user_state.state lifecycle on purpose: 👎 does not trash a paper, Trash does not write 👎.';
COMMENT ON COLUMN recommendation_feedback.source IS
    'Where the signal originated: pulse_thumbs (Pulse Deck card 👍/👎), feed_thumbs (Inbox/Library row, Pulse-origin only), paper_detail_thumbs (Paper Detail page), dismiss_combined (🗑+👎 single-click combo button).';
