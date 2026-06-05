-- 0095: paper_entities + pulse_models user_id FK SET NULL -> CASCADE.
-- Restores the data_purge contract (jobs/data_purge.py): a multi-row DELETE FROM users
-- must cascade-collapse per-user rows, not SET NULL them into a
-- UNIQUE NULLS NOT DISTINCT / COALESCE(user_id,0) collision (DB-01, DB-02).
DO $$ BEGIN
  ALTER TABLE public.paper_entities DROP CONSTRAINT IF EXISTS paper_entities_user_id_fkey;
  ALTER TABLE public.paper_entities
    ADD CONSTRAINT paper_entities_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  ALTER TABLE public.pulse_models DROP CONSTRAINT IF EXISTS pulse_models_user_id_fkey;
  ALTER TABLE public.pulse_models
    ADD CONSTRAINT pulse_models_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
