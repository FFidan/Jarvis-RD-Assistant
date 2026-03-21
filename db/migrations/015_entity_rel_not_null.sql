-- Migration 015: Add NOT NULL constraints to entity_relationships FK columns
-- Orphan rows (NULL source/target) violate graph integrity
DELETE FROM entity_relationships WHERE source_entity_id IS NULL OR target_entity_id IS NULL;
ALTER TABLE entity_relationships ALTER COLUMN source_entity_id SET NOT NULL;
ALTER TABLE entity_relationships ALTER COLUMN target_entity_id SET NOT NULL;
