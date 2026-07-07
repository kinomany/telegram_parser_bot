-- v13: устойчивость автосводок после перезапуска/падения процесса.
-- Основная схема уже создаётся автоматически в app/db/database.py.
-- Этот файл добавляет только полезные индексы для recovery/debug.

CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_locked
ON digest_subscriptions (locked_at)
WHERE locked_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_digest_subscription_runs_running
ON digest_subscription_runs (status, started_at)
WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_digest_subscription_runs_user_created
ON digest_subscription_runs (user_id, created_at DESC);
