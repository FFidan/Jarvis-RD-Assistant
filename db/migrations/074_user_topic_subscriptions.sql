-- 074: per-user topic subscriptions
-- Drives library.list_users_with_topic() so auto-fetched topic-matched papers
-- fan out into subscribers' libraries (added_via='auto_fetch_topic_match').

CREATE TABLE IF NOT EXISTS user_topic_subscriptions (
    user_id    INTEGER NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    topic_id   INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, topic_id)
);

CREATE INDEX IF NOT EXISTS idx_uts_topic ON user_topic_subscriptions(topic_id);
CREATE INDEX IF NOT EXISTS idx_uts_user  ON user_topic_subscriptions(user_id);
