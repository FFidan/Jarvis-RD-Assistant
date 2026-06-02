-- 0092: Re-own NULL-user product rows to the single admin (single-tenant backfill).
--
-- WHY: the legacy bot/system write path inserted product rows with user_id = NULL
-- (a "belongs to nobody / the whole deployment" sentinel). The canonical-ownership
-- refactor wants every product row owned by a real user_id. On a true single-tenant
-- deployment "the whole deployment" == "the one admin", so we re-own those NULL rows
-- to that admin.
--
-- SAFETY:
--   * Runs ONLY when EXACTLY ONE non-deleted admin exists (a real single-tenant box).
--     Multi-admin / zero-admin deployments are NOT single-tenant, so we skip — there
--     is no unambiguous owner to assign to, and guessing would mis-attribute data.
--   * Idempotent: a second run finds no user_id IS NULL product rows and is a no-op.
--   * Collision-safe on UNIQUE NULLS NOT DISTINCT (..., user_id) constraints: a NULL
--     row re-owned to the admin could duplicate an existing admin row on the key.
--     Two disposition classes handle that (see below).
--
-- DISPOSITION CLASSES:
--   A. Plain re-own — tables with NO (..., user_id) unique that the admin row could
--      collide on. A bare UPDATE ... WHERE user_id IS NULL is safe.
--   B. Collision-bearing tables (UNIQUE NULLS NOT DISTINCT including user_id):
--      B1. daily_log — count-bearing. The NULL row and the admin row for the same
--          log_date are BOTH real activity. We MERGE (SUM) the counts into the admin
--          row, then delete the NULL duplicate. Deleting the orphan instead would
--          silently lose the legacy day's tallies (streaks/analytics regress).
--      B2. paper_summaries / paper_user_state / pulse_decks / daily_intent — NOT
--          count-bearing; the NULL row and the admin row for the same key are
--          alternative copies of the same per-user fact. We keep the admin's
--          (newer / authoritative) row and DELETE the NULL orphan, then re-own the
--          rest. Merging arbitrary text/jsonb columns has no well-defined semantics.
--
-- Real NULL writers (legacy system path) are only: projects, cards, decks, daily_log,
-- paper_summaries. The other Class-A/B tables are re-owned defensively (cheap no-op
-- if they hold no NULL rows) so the deployment ends fully canonical.
DO $$
DECLARE
    _admin_count integer;
    _admin_id    integer;
BEGIN
    SELECT COUNT(*) INTO _admin_count
    FROM users
    WHERE role = 'admin' AND deleted_at IS NULL;

    IF _admin_count <> 1 THEN
        -- Not a single-tenant deployment (or no admin yet): no unambiguous owner.
        RAISE NOTICE '0092: skip (admin count %)', _admin_count;
        RETURN;
    END IF;

    SELECT id INTO _admin_id
    FROM users
    WHERE role = 'admin' AND deleted_at IS NULL
    ORDER BY id ASC
    LIMIT 1;

    -- -----------------------------------------------------------------------
    -- Class A — plain re-own (no (..., user_id) unique to collide on).
    -- Verified against db/init.sql: each of these has only a single-column PK
    -- (or a unique that excludes user_id, e.g. pulse_cards' (deck_id, paper_id)).
    -- -----------------------------------------------------------------------
    UPDATE projects     SET user_id = _admin_id WHERE user_id IS NULL;
    UPDATE tasks        SET user_id = _admin_id WHERE user_id IS NULL;
    UPDATE milestones   SET user_id = _admin_id WHERE user_id IS NULL;
    UPDATE cards        SET user_id = _admin_id WHERE user_id IS NULL;
    UPDATE decks        SET user_id = _admin_id WHERE user_id IS NULL;
    UPDATE review_logs  SET user_id = _admin_id WHERE user_id IS NULL;
    UPDATE thread       SET user_id = _admin_id WHERE user_id IS NULL;
    UPDATE pulse_cards  SET user_id = _admin_id WHERE user_id IS NULL;

    -- papers.discovered_by is an audit-only column (which user first discovered the
    -- paper); NULL means "system". Re-own to the admin for a coherent single-tenant
    -- audit trail. papers' only unique is external_id, so no collision risk.
    UPDATE papers       SET discovered_by = _admin_id WHERE discovered_by IS NULL;

    -- -----------------------------------------------------------------------
    -- Class B1 — daily_log: count-bearing, UNIQUE NULLS NOT DISTINCT (user_id, log_date).
    -- SUM-merge the NULL row's counts into the admin row for the same log_date, then
    -- delete the NULL duplicate, then re-own any remaining (non-colliding) NULL rows.
    --
    -- CRITICAL: the count columns are NULLABLE (DEFAULT 0, but a row may carry NULL).
    -- Wrap BOTH operands of EVERY count column in COALESCE(col, 0): in SQL
    -- `admin_value + NULL = NULL`, which would silently null the admin's counters
    -- (data loss). COALESCE makes the merge total-preserving.
    -- -----------------------------------------------------------------------
    UPDATE daily_log a SET
        tasks_completed = COALESCE(a.tasks_completed, 0) + COALESCE(n.tasks_completed, 0),
        cards_reviewed  = COALESCE(a.cards_reviewed, 0)  + COALESCE(n.cards_reviewed, 0),
        papers_read     = COALESCE(a.papers_read, 0)     + COALESCE(n.papers_read, 0),
        focus_hours     = COALESCE(a.focus_hours, 0)     + COALESCE(n.focus_hours, 0)
    FROM daily_log n
    WHERE n.user_id IS NULL
      AND a.user_id = _admin_id
      AND a.log_date = n.log_date;

    DELETE FROM daily_log
    WHERE user_id IS NULL
      AND log_date IN (SELECT log_date FROM daily_log WHERE user_id = _admin_id);

    UPDATE daily_log SET user_id = _admin_id WHERE user_id IS NULL;

    -- -----------------------------------------------------------------------
    -- Class B2 — delete-NULL-orphan-on-collision, then re-own the rest.
    -- For each, the key is UNIQUE NULLS NOT DISTINCT including user_id; when a NULL
    -- row would collide with an existing admin row on that key, drop the NULL copy
    -- (the admin row is authoritative) and re-own the rest.
    -- -----------------------------------------------------------------------

    -- paper_summaries: key (paper_id, user_id)
    DELETE FROM paper_summaries n
    WHERE n.user_id IS NULL
      AND EXISTS (
          SELECT 1 FROM paper_summaries o
          WHERE o.paper_id = n.paper_id AND o.user_id = _admin_id);
    UPDATE paper_summaries SET user_id = _admin_id WHERE user_id IS NULL;

    -- paper_user_state: key (paper_id, user_id)
    DELETE FROM paper_user_state n
    WHERE n.user_id IS NULL
      AND EXISTS (
          SELECT 1 FROM paper_user_state o
          WHERE o.paper_id = n.paper_id AND o.user_id = _admin_id);
    UPDATE paper_user_state SET user_id = _admin_id WHERE user_id IS NULL;

    -- pulse_decks: key (deck_date, user_id)
    DELETE FROM pulse_decks n
    WHERE n.user_id IS NULL
      AND EXISTS (
          SELECT 1 FROM pulse_decks o
          WHERE o.deck_date = n.deck_date AND o.user_id = _admin_id);
    UPDATE pulse_decks SET user_id = _admin_id WHERE user_id IS NULL;

    -- daily_intent: key (user_id, intent_date)
    DELETE FROM daily_intent n
    WHERE n.user_id IS NULL
      AND EXISTS (
          SELECT 1 FROM daily_intent o
          WHERE o.intent_date = n.intent_date AND o.user_id = _admin_id);
    UPDATE daily_intent SET user_id = _admin_id WHERE user_id IS NULL;

    -- -----------------------------------------------------------------------
    -- GUARDRAIL — intentionally NOT touched (amendment S7):
    -- These tables ALSO carry a UNIQUE NULLS NOT DISTINCT(..., user_id) constraint
    -- but have NO legacy NULL writer, so they hold no NULL-user rows to re-own:
    --     journal_entries, tracked_authors, author_alert_log,
    --     paper_notes, source_health
    -- (daily_intent has the same constraint shape but IS a legacy-writable surface,
    -- so it is handled as a Class-B2 delete-orphan above.) A blind plain-Class-A
    -- UPDATE on these could surface a stray NULL row and raise unique_violation, so
    -- they are deliberately left alone. Confirmed real NULL writers are only
    -- projects / cards / decks / daily_log / paper_summaries.
    --
    -- paper_chunks.user_id is a vestigial column never used for ownership filtering
    -- (no query filters on it), so it is intentionally NOT touched.
    -- -----------------------------------------------------------------------
END $$;
