-- v18: история дайджестов пользователя.
-- Отдельная таблица не нужна: финальный текст уже хранится в weekly_digest_jobs.final_text.
-- Добавляем индекс для быстрого списка последних сводок пользователя.

CREATE INDEX IF NOT EXISTS idx_weekly_digest_jobs_user_finished
ON weekly_digest_jobs (user_id, finished_at DESC NULLS LAST, created_at DESC);
