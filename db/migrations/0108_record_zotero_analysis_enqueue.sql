-- 0108: Record when a Zotero import's analysis scheduling was resolved, and
-- how many attempts it has spent trying.
--
-- The enqueue happens after the ingest transaction commits, so a failure used
-- to leave a committed paper whose analysis was never scheduled and never
-- could be: the retry path re-runs the upsert, sees an existing row, and the
-- brand-new-paper gate never fires again. Recording the successful enqueue
-- lets a retry tell "already scheduled" from "never scheduled" without
-- re-scheduling every previously imported item on each re-poll.
ALTER TABLE paper_user_zotero_links
    ADD COLUMN IF NOT EXISTS analysis_enqueued_at TIMESTAMPTZ;

-- Treat every link row that predates this column as already resolved. Without
-- this they all read as "decision outstanding", so the first poll after the
-- upgrade re-evaluates the whole imported library. Marking them preserves the
-- behaviour they already had: the previous gate fired only for a brand-new
-- paper, so an existing link could never be re-scheduled regardless.
UPDATE paper_user_zotero_links
   SET analysis_enqueued_at = updated_at
 WHERE analysis_enqueued_at IS NULL;

COMMENT ON COLUMN paper_user_zotero_links.analysis_enqueued_at IS
    'When this import''s analysis scheduling was resolved, by any of the three ways it can resolve: the paper.analyze job was deferred, the import carried no PDF to analyse, or the import spent every attempt allowed and was given up on. It records that a decision was reached, not which one; analysis_enqueue_attempts is what distinguishes an import that was given up on. NULL means the decision is still outstanding and the next poll must make it.';

-- Bound that retrying per item. An unresolved decision pins the library
-- version cursor so the next poll retries it, which is what an enqueue that
-- failed transiently needs — but an enqueue that can never succeed would
-- otherwise stop every other item in the library from syncing forever.
-- Counting attempts on the link row keeps the bound on the item that earned
-- it, so an item that failed once is never given up on because a different
-- item is stuck. Zero is correct for every pre-existing row: the backfill
-- above resolves them all, so they are never scheduled again and can spend
-- no attempt.
ALTER TABLE paper_user_zotero_links
    ADD COLUMN IF NOT EXISTS analysis_enqueue_attempts INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN paper_user_zotero_links.analysis_enqueue_attempts IS
    'How many times this import has tried to schedule its analysis. Incremented inside the ingest transaction, before the paper.analyze deferral runs, so an attempt whose deferral then fails is still counted. Once the limit is reached the poll resolves analysis_enqueued_at and stops retrying that item.';
