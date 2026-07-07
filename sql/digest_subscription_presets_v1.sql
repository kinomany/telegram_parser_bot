-- v21: пресеты качества автосводки.
-- Поле digest_preset уже было в новых версиях, но миграция нужна для старых БД
-- и для нормального DEFAULT = 'normal'.

ALTER TABLE digest_subscriptions
ADD COLUMN IF NOT EXISTS digest_preset TEXT NOT NULL DEFAULT 'normal';

ALTER TABLE digest_subscriptions
ALTER COLUMN digest_preset SET DEFAULT 'normal';

UPDATE digest_subscriptions
SET digest_preset = 'normal'
WHERE digest_preset IS NULL
   OR digest_preset NOT IN ('brief', 'normal', 'detailed');

CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_preset
ON digest_subscriptions (digest_preset);
