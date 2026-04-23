-- Cancel paper.process jobs that were enqueued without a paper_id
-- (created by the pre-PI-002 Zotero poll logic).  These jobs would have
-- failed silently because the handler does payload["paper_id"] and there
-- is no row in papers they can act on.
UPDATE jobs
   SET status      = 'cancelled',
       finished_at = NOW(),
       result      = '{"cancelled_reason": "orphan: no paper_id in payload (pre-PI-002)"}'::jsonb
 WHERE kind   = 'paper.process'
   AND status IN ('queued', 'running')
   AND (payload ? 'paper_id') = false;
