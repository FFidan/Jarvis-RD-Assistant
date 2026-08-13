CREATE TABLE focus_sessions (
    id bigserial PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    state text NOT NULL CHECK (state IN ('active', 'paused', 'completed')),
    source text NOT NULL CHECK (source IN ('web', 'telegram')),
    duration_seconds integer NOT NULL CHECK (duration_seconds BETWEEN 60 AND 28800),
    started_at timestamp with time zone NOT NULL DEFAULT now(),
    paused_at timestamp with time zone,
    paused_seconds double precision NOT NULL DEFAULT 0 CHECK (paused_seconds >= 0),
    completed_at timestamp with time zone,
    telegram_notified_at timestamp with time zone,
    recorded_seconds double precision NOT NULL DEFAULT 0 CHECK (recorded_seconds >= 0),
    task_id integer REFERENCES tasks(id) ON DELETE SET NULL,
    paper_id integer REFERENCES papers(id) ON DELETE SET NULL,
    created_at timestamp with time zone NOT NULL DEFAULT now(),
    updated_at timestamp with time zone NOT NULL DEFAULT now(),
    CHECK ((state = 'paused') = (paused_at IS NOT NULL)),
    CHECK ((state = 'completed') = (completed_at IS NOT NULL))
);

CREATE UNIQUE INDEX focus_sessions_one_open_per_user
    ON focus_sessions (user_id)
    WHERE state IN ('active', 'paused');

CREATE INDEX focus_sessions_user_recent
    ON focus_sessions (user_id, created_at DESC);
