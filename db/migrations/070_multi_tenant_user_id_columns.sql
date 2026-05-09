-- 070_multi_tenant_user_id_columns.sql — Phase 2 WS-2D
--
-- Multi-tenant readiness: add nullable user_id columns to tables that the
-- Wave-2 audit (docs/plans/2026-05-09-wave2-audit-results.md) identified as
-- still missing per-user scoping after WS-2A landed.
--
-- All columns are nullable to preserve existing rows as "system-shared"
-- (NULL user_id = visible to all users, by the documented project convention).
-- Sparse indexes (`WHERE user_id IS NOT NULL`) keep storage small for the
-- transition period when most rows are still system-shared.
--
-- Tables touched:
--   * cards               — flashcards (LE IDOR fix prerequisite)
--   * decks               — flashcard decks (batch-generation ownership check)
--   * review_logs         — flashcard review history (per-user stats)
--   * tracked_authors     — author alerts (per-user authors)
--   * author_alert_log    — author alert dedupe (per-user log)
--
-- (Transaction wrapper added by the migrations runner; do not include
-- BEGIN/COMMIT here.)

ALTER TABLE cards ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_cards_user
    ON cards(user_id) WHERE user_id IS NOT NULL;

ALTER TABLE decks ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_decks_user
    ON decks(user_id) WHERE user_id IS NOT NULL;

ALTER TABLE review_logs ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_review_logs_user
    ON review_logs(user_id) WHERE user_id IS NOT NULL;

ALTER TABLE tracked_authors ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_tracked_authors_user
    ON tracked_authors(user_id) WHERE user_id IS NOT NULL;

ALTER TABLE author_alert_log ADD COLUMN IF NOT EXISTS user_id INTEGER NULL;
CREATE INDEX IF NOT EXISTS idx_author_alert_log_user
    ON author_alert_log(user_id) WHERE user_id IS NOT NULL;
