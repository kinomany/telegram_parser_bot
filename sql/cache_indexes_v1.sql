-- cache_indexes_v1.sql
-- Индексы для быстрого чтения кэша сообщений по каналу и периоду.

CREATE INDEX IF NOT EXISTS idx_messages_source_username_date
ON messages (source_type, username, message_date DESC NULLS LAST, telegram_message_id DESC);

CREATE INDEX IF NOT EXISTS idx_rejected_source_username_date
ON rejected_messages (source_type, username, message_date DESC NULLS LAST, telegram_message_id DESC);
