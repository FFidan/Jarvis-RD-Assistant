-- 0094: make paper_extractions / paper_entities / paper_notes(zotero) per-user.
-- Backfill NULL user_id -> the single admin (single-tenant only; mirror 0092),
-- then swap each global unique/PK to a per-user UNIQUE NULLS NOT DISTINCT key.
-- Idempotent: re-runs are no-ops (single-admin gate + IF EXISTS + DO-block guards).
DO $$
DECLARE
    _admin_count integer;
    _admin_id    integer;
BEGIN
    SELECT COUNT(*), MIN(id) INTO _admin_count, _admin_id
    FROM users WHERE role = 'admin' AND deleted_at IS NULL;
    IF _admin_count = 1 THEN
        UPDATE paper_extractions SET user_id = _admin_id WHERE user_id IS NULL;
        UPDATE paper_entities    SET user_id = _admin_id WHERE user_id IS NULL;
        UPDATE paper_notes       SET user_id = _admin_id
            WHERE user_id IS NULL AND source = 'zotero';
    END IF;
END $$;

ALTER TABLE public.paper_extractions
    DROP CONSTRAINT IF EXISTS paper_extractions_paper_id_template_id_key;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.paper_extractions'::regclass
          AND conname  = 'paper_extractions_paper_template_user_key'
    ) THEN
        ALTER TABLE public.paper_extractions
            ADD CONSTRAINT paper_extractions_paper_template_user_key
            UNIQUE NULLS NOT DISTINCT (paper_id, template_id, user_id);
    END IF;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE public.paper_entities
    DROP CONSTRAINT IF EXISTS paper_entities_pkey;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_constraint
        WHERE conrelid = 'public.paper_entities'::regclass
          AND conname  = 'paper_entities_paper_entity_user_key'
    ) THEN
        ALTER TABLE public.paper_entities
            ADD CONSTRAINT paper_entities_paper_entity_user_key
            UNIQUE NULLS NOT DISTINCT (paper_id, entity_id, user_id);
    END IF;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DROP INDEX IF EXISTS uq_paper_notes_zotero_annotation;
CREATE UNIQUE INDEX uq_paper_notes_zotero_annotation
    ON public.paper_notes USING btree (paper_id, user_id, zotero_annotation_key)
    WHERE (zotero_annotation_key IS NOT NULL);
