-- Marks the moment a paper's full chunk set was successfully embedded + stored.
-- NULL = never completed (or only partially embedded after a mid-paper failure):
-- such papers must be re-processed rather than treated as done.
ALTER TABLE public.papers ADD COLUMN IF NOT EXISTS chunked_at timestamp with time zone;

-- Backfill: every paper that ALREADY has chunk rows was processed before this
-- column existed, so stamp it as complete. WITHOUT this, every pre-existing paper
-- starts with chunked_at = NULL — auto_fetch's 3b query (chunked_at IS NULL) would
-- re-pick the entire existing corpus and run_process_pdf's Phase-1 guard would no
-- longer short-circuit them, triggering a corpus-wide re-embedding storm on the
-- first deploy. Stamping now() for already-chunked papers makes them short-circuit.
UPDATE public.papers p
   SET chunked_at = now()
 WHERE chunked_at IS NULL
   AND EXISTS (SELECT 1 FROM paper_chunks pc WHERE pc.paper_id = p.id);
