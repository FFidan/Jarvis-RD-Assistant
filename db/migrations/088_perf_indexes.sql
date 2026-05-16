-- 088_perf_indexes.sql — B7 hot-path indexes for the /my-day execution plan.
--
-- The /api/executive/my-day recommendations query filters by user_id, excludes
-- dismissed rows, and orders by score DESC. The only pre-existing index on
-- paper_recommendations is idx_paper_recommendations_score (score DESC, NOT
-- user-scoped) plus mig-063's partial index on user_id alone — neither serves
-- "WHERE user_id = ? AND NOT dismissed ORDER BY score DESC" without a scan/sort.
-- This composite partial index does, and stays small because dismissed rows are
-- pruned from it entirely.
--
-- Skipped as redundant (YAGNI — a redundant index only costs writes):
--   * user_library (user_id, paper_id): user_library already has
--     PRIMARY KEY (user_id, paper_id) (mig 072), whose implicit unique btree
--     IS this index. Adding it again is pure write overhead.
--   * paper_topics (paper_id): paper_topics has PRIMARY KEY (paper_id, topic_id)
--     (db/init.sql); its implicit btree's leading column is paper_id, so
--     "WHERE paper_id = ?" lookups already use it. A standalone (paper_id) index
--     is redundant.
--
-- Idempotent CREATE INDEX IF NOT EXISTS. No BEGIN/COMMIT (the runner wraps each
-- migration in a transaction).

CREATE INDEX IF NOT EXISTS idx_paper_recommendations_user_score_active
    ON paper_recommendations (user_id, score DESC)
    WHERE NOT dismissed;
