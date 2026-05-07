-- Migration 057: Seed default model and FSRS config for existing installations.
-- Fresh installs already have these from db/init.sql (ON CONFLICT makes this idempotent).
INSERT INTO user_config (key, value) VALUES
    ('llm.smart_model',        '"smart"'),
    ('llm.fast_model',         '"fast"'),
    ('llm.embed_model',        '"embed"'),
    ('fsrs.desired_retention', '0.9'),
    ('fsrs.learning_steps',    '[1, 10]'),
    ('user.timezone',          '"UTC"'::jsonb)
ON CONFLICT (key) DO NOTHING;
