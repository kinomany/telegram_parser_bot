-- digest_subscriptions_v1.sql
-- MVP автосводок: раз в 3 дня / раз в неделю + debug-запуск вручную.

BEGIN;

CREATE TABLE IF NOT EXISTS digest_subscriptions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    period_days INTEGER NOT NULL DEFAULT 7,
    send_time TIME NOT NULL DEFAULT '09:00',
    timezone TEXT NOT NULL DEFAULT 'Asia/Tbilisi',
    digest_preset TEXT NOT NULL DEFAULT 'brief',
    include_channel_digests BOOLEAN NOT NULL DEFAULT TRUE,
    include_final_summary BOOLEAN NOT NULL DEFAULT TRUE,
    last_success_from TIMESTAMP,
    last_success_to TIMESTAMP,
    last_success_at TIMESTAMP,
    next_run_at TIMESTAMP NOT NULL,
    locked_at TIMESTAMP,
    locked_by TEXT,
    last_error_text TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS digest_subscription_channels (
    id BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT NOT NULL REFERENCES digest_subscriptions(id) ON DELETE CASCADE,
    user_channel_id BIGINT NOT NULL REFERENCES user_channels(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (subscription_id, user_channel_id)
);

CREATE TABLE IF NOT EXISTS digest_subscription_runs (
    id BIGSERIAL PRIMARY KEY,
    subscription_id BIGINT NOT NULL REFERENCES digest_subscriptions(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'created',
    period_from TIMESTAMP NOT NULL,
    period_to TIMESTAMP NOT NULL,
    channels_count INTEGER NOT NULL DEFAULT 0,
    messages_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    sent_at TIMESTAMP,
    error_text TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_due
ON digest_subscriptions (is_active, next_run_at);

CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_user_id
ON digest_subscriptions (user_id);

CREATE INDEX IF NOT EXISTS idx_digest_subscription_channels_subscription_id
ON digest_subscription_channels (subscription_id);

CREATE INDEX IF NOT EXISTS idx_digest_subscription_channels_user_channel_id
ON digest_subscription_channels (user_channel_id);

CREATE INDEX IF NOT EXISTS idx_digest_subscription_runs_subscription_id
ON digest_subscription_runs (subscription_id);

CREATE INDEX IF NOT EXISTS idx_digest_subscription_runs_status
ON digest_subscription_runs (status);

ALTER TABLE digest_subscriptions
ADD COLUMN IF NOT EXISTS send_time TIME NOT NULL DEFAULT '09:00';

ALTER TABLE digest_subscriptions
ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Asia/Tbilisi';

CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_timezone
ON digest_subscriptions (timezone);

COMMIT;
