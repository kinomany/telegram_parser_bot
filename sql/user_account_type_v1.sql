-- v25: тип аккаунта пользователя для скрытия debug/admin-функций

ALTER TABLE users
ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'user';

UPDATE users
SET account_type = 'user'
WHERE account_type IS NULL
   OR account_type NOT IN ('user', 'admin');

CREATE INDEX IF NOT EXISTS idx_users_account_type
ON users (account_type);
