-- channel_identity_migration_v1.sql
-- Каноническая таблица Telegram-каналов + привязка messages.telegram_channel_id.
-- Безопасно запускать повторно.

BEGIN;

CREATE TABLE IF NOT EXISTS telegram_channels (
    id BIGSERIAL PRIMARY KEY,
    username_key TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    title TEXT,
    tg_title TEXT,
    tg_description TEXT,
    is_public BOOLEAN NOT NULL DEFAULT TRUE,
    is_available BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_telegram_channels_username_key
ON telegram_channels (username_key);

CREATE INDEX IF NOT EXISTS idx_telegram_channels_username
ON telegram_channels (username);

-- Наполняем каноническую таблицу из всех мест, где уже встречались username каналов.
WITH src AS (
    SELECT
        LOWER(TRIM(LEADING '@' FROM username)) AS username_key,
        username,
        title
    FROM messages
    WHERE username IS NOT NULL

    UNION ALL

    SELECT
        LOWER(TRIM(LEADING '@' FROM username)) AS username_key,
        username,
        title
    FROM user_channels
    WHERE username IS NOT NULL

    UNION ALL

    SELECT
        LOWER(TRIM(LEADING '@' FROM username)) AS username_key,
        username,
        COALESCE(tg_title, title) AS title
    FROM channels
    WHERE username IS NOT NULL
), prepared AS (
    SELECT
        username_key,
        '@' || username_key AS username,
        MAX(NULLIF(title, '')) AS title
    FROM src
    WHERE username_key IS NOT NULL
      AND username_key <> ''
    GROUP BY username_key
)
INSERT INTO telegram_channels (username_key, username, title, updated_at)
SELECT username_key, username, title, NOW()
FROM prepared
ON CONFLICT (username_key)
DO UPDATE SET
    username = EXCLUDED.username,
    title = COALESCE(EXCLUDED.title, telegram_channels.title),
    updated_at = NOW();

ALTER TABLE messages
ADD COLUMN IF NOT EXISTS telegram_channel_id BIGINT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_messages_telegram_channel_id'
    ) THEN
        ALTER TABLE messages
        ADD CONSTRAINT fk_messages_telegram_channel_id
        FOREIGN KEY (telegram_channel_id)
        REFERENCES telegram_channels(id)
        ON DELETE SET NULL;
    END IF;
END $$;

UPDATE messages m
SET telegram_channel_id = tc.id
FROM telegram_channels tc
WHERE LOWER(TRIM(LEADING '@' FROM m.username)) = tc.username_key
  AND m.telegram_channel_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_messages_telegram_channel_date
ON messages (telegram_channel_id, message_date DESC NULLS LAST, telegram_message_id DESC);

COMMIT;
