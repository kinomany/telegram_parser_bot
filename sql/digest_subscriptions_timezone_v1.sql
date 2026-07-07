-- v10: локальное время и timezone для автосводок

ALTER TABLE digest_subscriptions
ADD COLUMN IF NOT EXISTS send_time TIME NOT NULL DEFAULT '09:00';

ALTER TABLE digest_subscriptions
ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Asia/Tbilisi';

CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_timezone
ON digest_subscriptions (timezone);
