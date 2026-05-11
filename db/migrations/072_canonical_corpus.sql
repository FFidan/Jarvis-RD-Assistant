-- 072_canonical_corpus.sql — Sprint B (canonical corpus refactor)
--
-- Spec: docs/plans/2026-05-10-multiuser-followup-sprints.md §"Sprint B"
--
-- Today `papers.user_id` does double duty: "creator" when a user manually saved,
-- "system-shared" when NULL. Feed queries muddle the two with
-- `user_id IS NULL OR user_id = $N`. This migration adopts the standard
-- canonical-corpus model used by Zotero / Mendeley / Semantic Scholar:
--
--   * `papers` is a global, canonical corpus (no owner).
--   * `user_library` is the per-user library — engagement implies membership.
--   * The legacy `papers.user_id` is renamed to `papers.discovered_by` and is
--     henceforth audit-only ("which user (or NULL for system) first discovered
--     this paper") with no functional role.
--
-- Backfill is the bridge between the two models. Two sources:
--   (a) every (paper_id, user_id) pair where `papers.user_id IS NOT NULL` —
--       these were manual saves under the old model.
--   (b) every (user_id, paper_id) pair from `paper_user_state` whose state
--       implies engagement: 'to_read', 'reading', 'done'. State 'inbox' is
--       explicitly excluded (untriaged) and 'trash' is explicitly excluded
--       (rejection ≠ library membership).
--
-- The migration is idempotent: every INSERT uses ON CONFLICT DO NOTHING. The
-- column rename is wrapped in DO/EXCEPTION so re-running on a prior partial
-- apply succeeds cleanly.
--
-- (Transaction wrapper added by the migrations runner; no BEGIN/COMMIT here.)


-- ---------------------------------------------------------------------------
-- 1. user_library table
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS user_library (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    paper_id  INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    added_via TEXT NOT NULL CHECK (added_via IN (
        'manual_save',
        'batch_save',
        'zotero_pull',
        'pulse_acceptance',
        'auto_fetch_topic_match',
        'backfill_engagement',
        'backfill_legacy_user_id',
        'topic_discovery',
        'citation_graph'
    )),
    PRIMARY KEY (user_id, paper_id)
);

CREATE INDEX IF NOT EXISTS idx_user_library_paper
    ON user_library(paper_id);

CREATE INDEX IF NOT EXISTS idx_user_library_user_added
    ON user_library(user_id, added_at DESC);

COMMENT ON TABLE user_library IS
    'Per-user library entries (Sprint B canonical-corpus refactor). Each row '
    'represents "user U has paper P in their library". Replaces the muddled '
    '`papers.user_id IS NULL OR papers.user_id = $N` predicate.';


-- ---------------------------------------------------------------------------
-- 2. Pre-count for verification gate
-- ---------------------------------------------------------------------------
-- Capture pre-migration distinct (user_id, paper_id) pairs implied by the
-- legacy model so the verification step at the end can fail loudly if the
-- backfill loses rows.

DO $$
DECLARE
    pre_distinct_legacy   BIGINT;
    pre_distinct_state    BIGINT;
    pre_distinct_union    BIGINT;
BEGIN
    -- (a) legacy ownership
    SELECT COUNT(*) INTO pre_distinct_legacy
        FROM (SELECT DISTINCT user_id, id FROM papers WHERE user_id IS NOT NULL) AS t;

    -- (b) engagement (state in to_read/reading/done)
    SELECT COUNT(*) INTO pre_distinct_state
        FROM (
            SELECT DISTINCT user_id, paper_id
              FROM paper_user_state
             WHERE user_id IS NOT NULL
               AND state IN ('to_read', 'reading', 'done')
        ) AS t;

    -- Approximate union (max of the two; true union requires a temp table —
    -- we capture both sides + sum as upper bound).
    pre_distinct_union := pre_distinct_legacy + pre_distinct_state;

    RAISE NOTICE 'migration 072: pre-backfill counts: legacy_owned=%, engagement=%, upper_bound_union=%',
        pre_distinct_legacy, pre_distinct_state, pre_distinct_union;

    -- Stash counters in a temp table so the post-backfill block can read them.
    CREATE TEMP TABLE IF NOT EXISTS _migration_072_pre_counts (
        legacy_owned    BIGINT,
        engagement      BIGINT,
        upper_bound     BIGINT
    ) ON COMMIT DROP;
    DELETE FROM _migration_072_pre_counts;
    INSERT INTO _migration_072_pre_counts VALUES
        (pre_distinct_legacy, pre_distinct_state, pre_distinct_union);
END $$;


-- ---------------------------------------------------------------------------
-- 3. Backfill (a) — legacy manual saves from `papers.user_id IS NOT NULL`
-- ---------------------------------------------------------------------------

INSERT INTO user_library (user_id, paper_id, added_at, added_via)
SELECT
    p.user_id,
    p.id,
    COALESCE(p.created_at, NOW()),
    'backfill_legacy_user_id'
  FROM papers p
 WHERE p.user_id IS NOT NULL
   -- Defensive: only backfill where the user still exists. Orphan rows from
   -- an old hard-deleted user become canonical-corpus-only entries.
   AND EXISTS (SELECT 1 FROM users u WHERE u.id = p.user_id)
ON CONFLICT (user_id, paper_id) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 4. Backfill (b) — engagement implies library membership
--     state IN ('to_read', 'reading', 'done')   →  in library
--     state IN ('inbox', 'trash')               →  NOT in library
-- ---------------------------------------------------------------------------

INSERT INTO user_library (user_id, paper_id, added_at, added_via)
SELECT
    pus.user_id,
    pus.paper_id,
    COALESCE(MIN(pus.created_at), NOW()) AS added_at,
    'backfill_engagement'
  FROM paper_user_state pus
 WHERE pus.user_id IS NOT NULL
   AND pus.state IN ('to_read', 'reading', 'done')
   AND EXISTS (SELECT 1 FROM users u WHERE u.id = pus.user_id)
   AND EXISTS (SELECT 1 FROM papers p WHERE p.id = pus.paper_id)
GROUP BY pus.user_id, pus.paper_id
ON CONFLICT (user_id, paper_id) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 5. Verification gate
-- ---------------------------------------------------------------------------
-- Post-backfill row count must be at least the larger of the two pre-counts
-- (sanity: every legacy-owned paper should map to a library row, since the
-- user_id-NOT-NULL set is a strict subset of "should be in library").

DO $$
DECLARE
    pre_legacy      BIGINT;
    pre_engagement  BIGINT;
    post_count      BIGINT;
BEGIN
    SELECT legacy_owned, engagement INTO pre_legacy, pre_engagement
      FROM _migration_072_pre_counts;

    SELECT COUNT(*) INTO post_count FROM user_library;

    RAISE NOTICE 'migration 072: post-backfill user_library rows=%, pre_legacy=%, pre_engagement=%',
        post_count, pre_legacy, pre_engagement;

    -- Hard guard: every legacy-owned paper MUST have produced a library row.
    -- Engagement may overlap with legacy ownership, so we only assert against
    -- the legacy slice (post_count >= pre_legacy is a necessary condition).
    IF post_count < pre_legacy THEN
        RAISE EXCEPTION
            'migration 072 verification failed: post-backfill rows (%) < legacy_owned (%); rolling back',
            post_count, pre_legacy;
    END IF;
END $$;


-- ---------------------------------------------------------------------------
-- 6. Rename `papers.user_id` → `papers.discovered_by`
-- ---------------------------------------------------------------------------
-- Idempotent: skip if column already renamed (re-run safety).

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'papers' AND column_name = 'user_id'
    ) THEN
        ALTER TABLE papers RENAME COLUMN user_id TO discovered_by;
        RAISE NOTICE 'migration 072: renamed papers.user_id -> papers.discovered_by';
    ELSE
        RAISE NOTICE 'migration 072: papers.user_id already renamed; skipping';
    END IF;
END $$;

COMMENT ON COLUMN papers.discovered_by IS
    'Audit only: which user (or NULL for system) first discovered this paper. '
    'Library membership lives in user_library, not here. Sprint B (migration 072).';

-- Drop the legacy index (was: idx_papers_user on papers(user_id)).
DROP INDEX IF EXISTS idx_papers_user;

-- Recreate the index under the new column name for audit queries.
CREATE INDEX IF NOT EXISTS idx_papers_discovered_by
    ON papers(discovered_by) WHERE discovered_by IS NOT NULL;
