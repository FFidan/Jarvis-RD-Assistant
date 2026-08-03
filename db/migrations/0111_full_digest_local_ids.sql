-- Local uploads are identified by the full content digest; the short 16-hex form
-- predates this and is derivable from the stored source URL (`local://<digest>`).
--
-- The NOT EXISTS clause protects the papers_external_id_key unique constraint
-- against a row that already carries the full-digest id for the same content.
-- A row it skips keeps its short id and stays reachable; a later re-upload of
-- those bytes then creates a second, full-id row. That is the accepted residual
-- for the pathological case where both forms of one digest already coexist.
UPDATE papers
SET external_id = 'local:' || substring(url from 9)
WHERE source_type = 'local'
  AND external_id LIKE 'local:%'
  AND length(external_id) = 22
  AND url LIKE 'local://%'
  AND length(url) = 72
  AND NOT EXISTS (
    SELECT 1 FROM papers p2 WHERE p2.external_id = 'local:' || substring(papers.url from 9)
  );
