-- Migration 015: Add NOT NULL constraints to entity_relationships FK columns
-- Orphan rows (NULL source/target) violate graph integrity
DO $$
BEGIN
    -- Only act if columns still allow NULL (idempotent guard)
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'entity_relationships'
          AND column_name IN ('source_entity_id', 'target_entity_id')
          AND is_nullable = 'YES'
    ) THEN
        DELETE FROM entity_relationships
        WHERE source_entity_id IS NULL OR target_entity_id IS NULL;

        ALTER TABLE entity_relationships ALTER COLUMN source_entity_id SET NOT NULL;
        ALTER TABLE entity_relationships ALTER COLUMN target_entity_id SET NOT NULL;
    END IF;
END $$;
