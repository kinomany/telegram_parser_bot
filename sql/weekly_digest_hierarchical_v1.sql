-- weekly_digest_hierarchical_v1.sql
-- История иерархических дайджестов: сводки по каналам + итоговая общая сводка.
-- Безопасно запускать повторно.

CREATE TABLE IF NOT EXISTS weekly_digest_jobs (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',
    period_from TIMESTAMP,
    period_to TIMESTAMP,
    selected_user_channel_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    channels_count INTEGER NOT NULL DEFAULT 0,
    messages_count INTEGER NOT NULL DEFAULT 0,
    channel_digests_count INTEGER NOT NULL DEFAULT 0,
    final_summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    final_text TEXT,
    model TEXT,
    error_text TEXT,
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS weekly_channel_digest_parts (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES weekly_digest_jobs(id) ON DELETE CASCADE,
    user_channel_id BIGINT REFERENCES user_channels(id) ON DELETE SET NULL,
    username TEXT NOT NULL,
    title TEXT,
    messages_count INTEGER NOT NULL DEFAULT 0,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    model TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    UNIQUE (job_id, user_channel_id)
);

CREATE INDEX IF NOT EXISTS idx_weekly_digest_jobs_user_id
ON weekly_digest_jobs (user_id);

CREATE INDEX IF NOT EXISTS idx_weekly_digest_jobs_status
ON weekly_digest_jobs (status);

CREATE INDEX IF NOT EXISTS idx_weekly_digest_jobs_period
ON weekly_digest_jobs (period_from, period_to);

CREATE INDEX IF NOT EXISTS idx_weekly_channel_digest_parts_job_id
ON weekly_channel_digest_parts (job_id);

CREATE INDEX IF NOT EXISTS idx_weekly_channel_digest_parts_user_channel_id
ON weekly_channel_digest_parts (user_channel_id);
