-- user_channels_categories_v1.sql
-- Ручные категории для личных каналов пользователя + заготовка под будущий общий доступ.
-- Безопасно запускать повторно.

ALTER TABLE user_channels
ADD COLUMN IF NOT EXISTS user_category TEXT;

ALTER TABLE user_channels
ADD COLUMN IF NOT EXISTS shared_candidate BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE user_channels
ADD COLUMN IF NOT EXISTS shared_status TEXT NOT NULL DEFAULT 'private';

CREATE INDEX IF NOT EXISTS idx_user_channels_user_category
ON user_channels (user_id, user_category, is_active);

CREATE INDEX IF NOT EXISTS idx_user_channels_shared_status
ON user_channels (shared_status);
