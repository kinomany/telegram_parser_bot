-- user_channel_digest_state_v1.sql
-- Состояние режима «🕓 С прошлого дайджеста» для личных каналов.
-- Безопасно запускать повторно.

CREATE TABLE IF NOT EXISTS user_channel_digest_state (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_channel_id BIGINT NOT NULL REFERENCES user_channels(id) ON DELETE CASCADE,
    last_digest_at TIMESTAMP,
    last_message_date TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, user_channel_id)
);

CREATE INDEX IF NOT EXISTS idx_user_channel_digest_state_user_id
ON user_channel_digest_state (user_id);

CREATE INDEX IF NOT EXISTS idx_user_channel_digest_state_channel_id
ON user_channel_digest_state (user_channel_id);
