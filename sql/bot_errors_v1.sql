CREATE TABLE IF NOT EXISTS bot_errors (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    telegram_id BIGINT,
    place TEXT NOT NULL,
    error_type TEXT,
    error_text TEXT NOT NULL,
    traceback_text TEXT,
    context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_errors_created_at ON bot_errors (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bot_errors_place ON bot_errors (place);
CREATE INDEX IF NOT EXISTS idx_bot_errors_telegram_id ON bot_errors (telegram_id);
CREATE INDEX IF NOT EXISTS idx_bot_errors_resolved ON bot_errors (is_resolved, created_at DESC);
