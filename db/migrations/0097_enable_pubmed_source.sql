-- Enable PubMed as a default Discover source.
-- db/init.sql already seeds this row with enabled=TRUE for fresh deploys; this
-- migration flips it on in existing databases where the row was inserted with
-- enabled=FALSE by an earlier seed.
UPDATE public.paper_sources
   SET enabled = TRUE
 WHERE source_type = 'pubmed'
   AND enabled = FALSE;
