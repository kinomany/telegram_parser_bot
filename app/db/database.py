import asyncpg
import json
from datetime import datetime, timedelta, timezone


from config import (
    DB_HOST,
    DB_PORT,
    DB_NAME,
    DB_USER,
    DB_PASSWORD,
    DB_POOL_MIN_SIZE,
    DB_POOL_MAX_SIZE,
    DIGEST_SUBSCRIPTION_FAILED_BACKOFF_MINUTES,
)
from app.utils.timezones import (
    DEFAULT_SEND_TIME,
    DEFAULT_TIMEZONE,
    calculate_next_run_at,
    ensure_aware_utc,
    format_send_time,
    normalize_timezone_name,
    parse_send_time,
)


pool: asyncpg.Pool | None = None


async def init_db() -> None:
    """Создаёт пул подключений к PostgreSQL и проверяет минимальную схему."""
    global pool

    pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=DB_POOL_MIN_SIZE,
        max_size=DB_POOL_MAX_SIZE,
    )

    await ensure_db_schema()


async def close_db() -> None:
    """Закрывает пул подключений к PostgreSQL."""
    global pool

    if pool is not None:
        await pool.close()
        pool = None


def _get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("База данных не подключена. Сначала вызовите init_db().")
    return pool


def normalize_username_key(username: str | None) -> str:
    """Единый ключ Telegram-канала: без @, lowercase."""
    return (username or "").strip().lstrip("@").lower()


def normalize_username_display(username: str | None) -> str:
    key = normalize_username_key(username)
    return f"@{key}" if key else ""


async def _table_columns(connection: asyncpg.Connection, table_name: str) -> set[str]:
    rows = await connection.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = $1;
        """,
        table_name,
    )
    return {row["column_name"] for row in rows}


async def ensure_db_schema() -> None:
    """
    Создаёт актуальную схему БД под:
    - обычный сбор сообщений ботом;
    - личные каналы пользователя;
    - общую таблицу channels для ИИ-разметки;
    - новую разметку по tax_tree.py через ai_classification JSONB.

    Если база тестовая и схема уже старая, проще выполнить reset_schema_tax_tree.sql.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL UNIQUE,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                account_type TEXT NOT NULL DEFAULT 'user',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """
        )

        await connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'user';")
        await connection.execute(
            """
            UPDATE users
            SET account_type = 'user'
            WHERE account_type IS NULL OR account_type NOT IN ('user', 'admin');
            """
        )

        await connection.execute("CREATE INDEX IF NOT EXISTS idx_users_account_type ON users (account_type);")

        await connection.execute(
            """
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
            """
        )


        await connection.execute(
            """
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
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                source_url TEXT,

                title TEXT,
                description TEXT,
                tg_title TEXT,
                tg_description TEXT,

                category TEXT,
                ai_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
                ai_summary TEXT,
                ai_description TEXT,
                ai_classification JSONB NOT NULL DEFAULT '{}'::jsonb,
                ai_checked_at TIMESTAMP,

                status TEXT NOT NULL DEFAULT 'new',
                error TEXT,

                is_public BOOLEAN NOT NULL DEFAULT TRUE,
                is_available BOOLEAN NOT NULL DEFAULT TRUE,
                is_verified BOOLEAN NOT NULL DEFAULT TRUE,
                last_checked_at TIMESTAMP,

                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMP
            );
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_ai_markup (
                id BIGSERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                username TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'done',
                error TEXT,
                model TEXT,
                is_current BOOLEAN NOT NULL DEFAULT TRUE,

                category TEXT,
                region TEXT,
                content_format TEXT,
                position_label TEXT,
                confidence NUMERIC(5, 4),

                ai_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
                ai_description TEXT,
                ai_classification JSONB NOT NULL DEFAULT '{}'::jsonb,
                ai_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,

                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_channels (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                username TEXT NOT NULL,
                title TEXT,
                user_category TEXT,
                shared_candidate BOOLEAN NOT NULL DEFAULT FALSE,
                shared_status TEXT NOT NULL DEFAULT 'private',

                ai_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
                ai_summary TEXT,
                ai_description TEXT,
                ai_classification JSONB NOT NULL DEFAULT '{}'::jsonb,
                ai_checked_at TIMESTAMP,

                is_public BOOLEAN NOT NULL DEFAULT TRUE,
                is_available BOOLEAN NOT NULL DEFAULT TRUE,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                last_checked_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, username)
            );
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_channel_id BIGINT,
                username TEXT NOT NULL,
                title TEXT,
                telegram_message_id BIGINT NOT NULL,
                message_text TEXT,
                cleaned_text TEXT,
                message_date TIMESTAMP,
                has_text BOOLEAN NOT NULL DEFAULT FALSE,
                has_media BOOLEAN NOT NULL DEFAULT FALSE,
                views_count INTEGER,
                forwards_count INTEGER,
                replies_count INTEGER,
                is_useful BOOLEAN NOT NULL DEFAULT TRUE,
                filter_reason TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (source_type, username, telegram_message_id)
            );
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rejected_messages (
                id BIGSERIAL PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_channel_id BIGINT,
                username TEXT NOT NULL,
                title TEXT,
                telegram_message_id BIGINT NOT NULL,
                original_text TEXT,
                cleaned_text TEXT,
                reject_reason TEXT NOT NULL,
                message_date TIMESTAMP,
                has_text BOOLEAN NOT NULL DEFAULT FALSE,
                has_media BOOLEAN NOT NULL DEFAULT FALSE,
                views_count INTEGER,
                forwards_count INTEGER,
                replies_count INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (source_type, username, telegram_message_id, reject_reason)
            );
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS parse_jobs (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'created',
                source_type TEXT,
                selected_channels TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                messages_found INTEGER NOT NULL DEFAULT 0,
                messages_saved INTEGER NOT NULL DEFAULT 0,
                error_text TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_message_results (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                parse_job_id BIGINT NOT NULL REFERENCES parse_jobs(id) ON DELETE CASCADE,
                message_id BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, parse_job_id, message_id)
            );
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_channel_digest_state (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                user_channel_id BIGINT NOT NULL REFERENCES user_channels(id) ON DELETE CASCADE,
                last_digest_at TIMESTAMP,
                last_message_date TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (user_id, user_channel_id)
            );
            """
        )

        await connection.execute(
            """
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
            """
        )

        await connection.execute(
            """
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
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS digest_subscriptions (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title TEXT,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                period_days INTEGER NOT NULL DEFAULT 7,
                send_time TIME NOT NULL DEFAULT '09:00',
                timezone TEXT NOT NULL DEFAULT 'Asia/Tbilisi',
                digest_preset TEXT NOT NULL DEFAULT 'normal',
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
            """
        )

        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS digest_subscription_channels (
                id BIGSERIAL PRIMARY KEY,
                subscription_id BIGINT NOT NULL REFERENCES digest_subscriptions(id) ON DELETE CASCADE,
                user_channel_id BIGINT NOT NULL REFERENCES user_channels(id) ON DELETE CASCADE,
                position INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (subscription_id, user_channel_id)
            );
            """
        )

        await connection.execute(
            """
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
            """
        )


                                                                           
                                                                                                           
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS source_url TEXT;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS tg_title TEXT;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS tg_description TEXT;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_keywords JSONB NOT NULL DEFAULT '[]'::jsonb;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_summary TEXT;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_description TEXT;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_classification JSONB NOT NULL DEFAULT '{}'::jsonb;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_checked_at TIMESTAMP;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new';")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS error TEXT;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ad_heavy BOOLEAN NOT NULL DEFAULT FALSE;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ad_rejected_count INTEGER NOT NULL DEFAULT 0;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ad_checked_count INTEGER NOT NULL DEFAULT 0;")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ad_ratio NUMERIC(6, 4);")
        await connection.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ad_heavy_reason TEXT;")

        await connection.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS telegram_channel_id BIGINT;")
        await connection.execute(
            """
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
            """
        )

        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS user_category TEXT;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS shared_candidate BOOLEAN NOT NULL DEFAULT FALSE;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS shared_status TEXT NOT NULL DEFAULT 'private';")
        await connection.execute("ALTER TABLE digest_subscriptions ADD COLUMN IF NOT EXISTS send_time TIME NOT NULL DEFAULT '09:00';")
        await connection.execute("ALTER TABLE digest_subscriptions ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Asia/Tbilisi';")
        await connection.execute("ALTER TABLE digest_subscriptions ADD COLUMN IF NOT EXISTS digest_preset TEXT NOT NULL DEFAULT 'normal';")
        await connection.execute("ALTER TABLE digest_subscriptions ALTER COLUMN digest_preset SET DEFAULT 'normal';")
        await connection.execute("""
            UPDATE digest_subscriptions
            SET digest_preset = 'normal'
            WHERE digest_preset IS NULL
               OR digest_preset NOT IN ('brief', 'normal', 'detailed');
        """)

        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ai_keywords JSONB NOT NULL DEFAULT '[]'::jsonb;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ai_summary TEXT;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ai_description TEXT;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ai_classification JSONB NOT NULL DEFAULT '{}'::jsonb;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ai_checked_at TIMESTAMP;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ad_heavy BOOLEAN NOT NULL DEFAULT FALSE;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ad_rejected_count INTEGER NOT NULL DEFAULT 0;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ad_checked_count INTEGER NOT NULL DEFAULT 0;")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ad_ratio NUMERIC(6, 4);")
        await connection.execute("ALTER TABLE user_channels ADD COLUMN IF NOT EXISTS ad_heavy_reason TEXT;")

        user_channels_columns = await _table_columns(connection, "user_channels")
        if "channel_id" in user_channels_columns:
            raise RuntimeError(
                "В БД старая таблица user_channels с колонкой channel_id. "
                "Для текущей логики выполни reset_schema_tax_tree.sql."
            )

        messages_columns = await _table_columns(connection, "messages")
        if "channel_id" in messages_columns:
            raise RuntimeError(
                "В БД старая таблица messages с колонкой channel_id. "
                "Для текущей логики выполни reset_schema_tax_tree.sql."
            )

        parse_jobs_columns = await _table_columns(connection, "parse_jobs")
        if "channel_id" in parse_jobs_columns or "user_channel_id" in parse_jobs_columns:
            raise RuntimeError(
                "В БД старая таблица parse_jobs. Выполни reset_schema_tax_tree.sql."
            )

        await connection.execute("CREATE INDEX IF NOT EXISTS idx_telegram_channels_username_key ON telegram_channels (username_key);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_telegram_channels_username ON telegram_channels (username);")

        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channels_username ON channels (username);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channels_status ON channels (status);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channels_ai_checked_at ON channels (ai_checked_at);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channels_ai_keywords ON channels USING GIN (ai_keywords);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channels_ai_classification ON channels USING GIN (ai_classification);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channels_ad_heavy ON channels (ad_heavy);")

        await connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_channel_ai_markup_current
            ON channel_ai_markup (channel_id)
            WHERE is_current = TRUE;
            """
        )
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_channel_id ON channel_ai_markup (channel_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_username ON channel_ai_markup (username);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_category ON channel_ai_markup (category);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_region ON channel_ai_markup (region);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_content_format ON channel_ai_markup (content_format);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_position_label ON channel_ai_markup (position_label);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_ai_keywords ON channel_ai_markup USING GIN (ai_keywords);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_ai_classification ON channel_ai_markup USING GIN (ai_classification);")

        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_channels_user_id ON user_channels (user_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_channels_username ON user_channels (username);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_channels_user_category ON user_channels (user_id, user_category, is_active);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_channels_shared_status ON user_channels (shared_status);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_channels_ai_checked_at ON user_channels (ai_checked_at);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_channels_ad_heavy ON user_channels (ad_heavy);")

        await connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_username ON messages (username);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_message_date ON messages (message_date);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_source_type ON messages (source_type);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_source_channel_id ON messages (source_channel_id);")
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_source_username_date
            ON messages (source_type, username, message_date DESC NULLS LAST, telegram_message_id DESC);
            """
        )
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_telegram_channel_date
            ON messages (telegram_channel_id, message_date DESC NULLS LAST, telegram_message_id DESC);
            """
        )

        await connection.execute("CREATE INDEX IF NOT EXISTS idx_rejected_messages_username ON rejected_messages (username);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_rejected_messages_message_date ON rejected_messages (message_date);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_rejected_messages_reason ON rejected_messages (reject_reason);")
        await connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_rejected_source_username_date
            ON rejected_messages (source_type, username, message_date DESC NULLS LAST, telegram_message_id DESC);
            """
        )

        await connection.execute("CREATE INDEX IF NOT EXISTS idx_parse_jobs_user_id ON parse_jobs (user_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_parse_jobs_status ON parse_jobs (status);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_message_results_user_id ON user_message_results (user_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_message_results_parse_job_id ON user_message_results (parse_job_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_message_results_message_id ON user_message_results (message_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_channel_digest_state_user_id ON user_channel_digest_state (user_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_user_channel_digest_state_channel_id ON user_channel_digest_state (user_channel_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_weekly_digest_jobs_user_id ON weekly_digest_jobs (user_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_weekly_digest_jobs_status ON weekly_digest_jobs (status);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_weekly_digest_jobs_period ON weekly_digest_jobs (period_from, period_to);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_weekly_digest_jobs_user_finished ON weekly_digest_jobs (user_id, finished_at DESC NULLS LAST, created_at DESC);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_weekly_channel_digest_parts_job_id ON weekly_channel_digest_parts (job_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_weekly_channel_digest_parts_user_channel_id ON weekly_channel_digest_parts (user_channel_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_due ON digest_subscriptions (is_active, next_run_at);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_user_id ON digest_subscriptions (user_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscription_channels_subscription_id ON digest_subscription_channels (subscription_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscription_channels_user_channel_id ON digest_subscription_channels (user_channel_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscription_runs_subscription_id ON digest_subscription_runs (subscription_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscription_runs_status ON digest_subscription_runs (status);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscriptions_locked ON digest_subscriptions (locked_at) WHERE locked_at IS NOT NULL;")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscription_runs_running ON digest_subscription_runs (status, started_at) WHERE status = 'running';")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_digest_subscription_runs_user_created ON digest_subscription_runs (user_id, created_at DESC);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_created_at ON bot_errors (created_at DESC);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_place ON bot_errors (place);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_telegram_id ON bot_errors (telegram_id);")
        await connection.execute("CREATE INDEX IF NOT EXISTS idx_bot_errors_resolved ON bot_errors (is_resolved, created_at DESC);")



async def mark_channel_as_ad_heavy(
    username: str,
    ad_count: int,
    checked_count: int,
    ad_ratio: float,
    auto_disable: bool = False,
) -> None:
    """
    Безопасно помечает канал как рекламный/замусоренный.

    Важно: схема БД могла отличаться между версиями проекта.
    Поэтому функция не предполагает, что колонки is_available/is_active/ad_heavy уже есть.
    Она обновляет только те колонки, которые реально существуют.
    """
    db_pool = _get_pool()
    username_key = (username or "").strip().lstrip("@").lower()
    reason = (
        f"Слишком много рекламных сообщений: "
        f"{ad_count}/{checked_count} ({ad_ratio:.1%})"
    )

    async def update_table_if_exists(
        connection: asyncpg.Connection,
        table_name: str,
        disable_column: str | None = None,
    ) -> None:
        columns = await _table_columns(connection, table_name)

        if not columns:
            return

        assignments: list[str] = []
        args: list = [username_key]

        def add_value_assignment(sql_template: str, value) -> None:
            args.append(value)
            assignments.append(sql_template.format(param=f"${len(args)}"))

        if "ad_heavy" in columns:
            assignments.append("ad_heavy = TRUE")

        if "ad_rejected_count" in columns:
            add_value_assignment(
                "ad_rejected_count = GREATEST(COALESCE(ad_rejected_count, 0), {param})",
                int(ad_count),
            )

        if "ad_checked_count" in columns:
            add_value_assignment(
                "ad_checked_count = GREATEST(COALESCE(ad_checked_count, 0), {param})",
                int(checked_count),
            )

        if "ad_ratio" in columns:
            add_value_assignment("ad_ratio = {param}", float(ad_ratio))

        if "ad_heavy_reason" in columns:
            add_value_assignment("ad_heavy_reason = {param}", reason)

                                                                               
        if "error" in columns and "ad_heavy_reason" not in columns:
            add_value_assignment(
                """
                error = CASE
                    WHEN error IS NULL OR error = '' THEN {param}
                    ELSE error || '; ' || {param}
                END
                """,
                reason,
            )

        if auto_disable and disable_column and disable_column in columns:
            assignments.append(f"{disable_column} = FALSE")

        if "updated_at" in columns:
            assignments.append("updated_at = NOW()")

        if not assignments:
            print(
                f"Не удалось пометить рекламный канал в {table_name}: "
                f"нет подходящих колонок. username={username}"
            )
            return

        sql = f"""
            UPDATE {table_name}
            SET {", ".join(assignments)}
            WHERE LOWER(TRIM(LEADING '@' FROM username)) = $1;
        """

        await connection.execute(sql, *args)

    async with db_pool.acquire() as connection:
        await update_table_if_exists(
            connection=connection,
            table_name="channels",
            disable_column="is_available",
        )
        await update_table_if_exists(
            connection=connection,
            table_name="user_channels",
            disable_column="is_active",
        )


async def get_or_create_telegram_channel(
    username: str,
    title: str | None = None,
    tg_title: str | None = None,
    tg_description: str | None = None,
) -> dict:
    """
    Возвращает единый канонический id Telegram-канала.

    Этот id не зависит от того, откуда канал пришёл: из личного списка пользователя
    или из общей размеченной базы channels. Именно на него должны ссылаться messages.
    """
    db_pool = _get_pool()
    username_key = normalize_username_key(username)

    if not username_key:
        raise ValueError("Пустой username Telegram-канала")

    username_display = normalize_username_display(username)

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            INSERT INTO telegram_channels (
                username_key,
                username,
                title,
                tg_title,
                tg_description,
                updated_at
            )
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (username_key)
            DO UPDATE SET
                username = EXCLUDED.username,
                title = COALESCE(EXCLUDED.title, telegram_channels.title),
                tg_title = COALESCE(EXCLUDED.tg_title, telegram_channels.tg_title),
                tg_description = COALESCE(EXCLUDED.tg_description, telegram_channels.tg_description),
                updated_at = NOW()
            RETURNING id, username_key, username, title, tg_title, tg_description;
            """,
            username_key,
            username_display,
            title,
            tg_title,
            tg_description,
        )

    return dict(row)


async def get_or_create_user(
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> dict:
    """Создаёт пользователя или обновляет его данные, если он уже есть."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        user = await connection.fetchrow(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                updated_at = NOW()
            RETURNING id, telegram_id, username, first_name, last_name, account_type;
            """,
            telegram_id,
            username,
            first_name,
            last_name,
        )

    return dict(user)


def normalize_account_type(account_type: str | None) -> str:
    value = str(account_type or "user").strip().lower()
    if value in {"admin", "administrator", "админ"}:
        return "admin"
    return "user"


async def get_user_account_type_by_telegram_id(telegram_id: int) -> str:
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        value = await connection.fetchval(
            """
            SELECT account_type
            FROM users
            WHERE telegram_id = $1;
            """,
            int(telegram_id),
        )

    return normalize_account_type(value)


async def set_user_account_type_by_telegram_id(telegram_id: int, account_type: str) -> dict | None:
    db_pool = _get_pool()
    normalized = normalize_account_type(account_type)

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            UPDATE users
            SET account_type = $2,
                updated_at = NOW()
            WHERE telegram_id = $1
            RETURNING id, telegram_id, username, first_name, last_name, account_type, updated_at;
            """,
            int(telegram_id),
            normalized,
        )

    return dict(row) if row else None


async def set_user_account_type_by_id(user_id: int, account_type: str) -> dict | None:
    db_pool = _get_pool()
    normalized = normalize_account_type(account_type)

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            UPDATE users
            SET account_type = $2,
                updated_at = NOW()
            WHERE id = $1
            RETURNING id, telegram_id, username, first_name, last_name, account_type, updated_at;
            """,
            int(user_id),
            normalized,
        )

    return dict(row) if row else None


async def list_users_for_admin_console(limit: int = 50, query: str | None = None) -> list[dict]:
    db_pool = _get_pool()
    limit = max(1, min(int(limit or 50), 200))

    where_sql = ""
    params = []
    if query:
        params.append(f"%{query.strip()}%")
        where_sql = """
        WHERE username ILIKE $1
           OR first_name ILIKE $1
           OR last_name ILIKE $1
           OR telegram_id::text ILIKE $1
           OR id::text ILIKE $1
        """

    sql = f"""
        SELECT
            id, telegram_id, username, first_name, last_name, account_type,
            created_at, updated_at
        FROM users
        {where_sql}
        ORDER BY updated_at DESC NULLS LAST, id DESC
        LIMIT {limit};
    """

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(sql, *params)

    return [dict(row) for row in rows]



async def verified_channel_exists(username: str) -> bool:
    """Проверяет, есть ли канал в общей базе проверенных каналов channels."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        exists = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM channels
                WHERE username = $1
                  AND is_available = TRUE
            );
            """,
            username,
        )

    return bool(exists)


async def count_user_channels(user_id: int) -> int:
    """Считает активные личные каналы пользователя."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        count = await connection.fetchval(
            """
            SELECT COUNT(*)
            FROM user_channels
            WHERE user_id = $1
              AND is_active = TRUE;
            """,
            user_id,
        )

    return int(count or 0)


async def add_user_channel(
    user_id: int,
    username: str,
    title: str | None,
    user_category: str | None = None,
) -> dict:
    """
    Добавляет канал только в личный список пользователя.
    С таблицей channels не связывает.

    user_category — ручная категория пользователя. Потом разметчик может уточнить
    её через ai_classification, но для быстрого поиска достаточно ручной категории.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        channel = await connection.fetchrow(
            """
            INSERT INTO user_channels (
                user_id,
                username,
                title,
                user_category,
                shared_candidate,
                shared_status,
                is_public,
                is_available,
                is_active,
                last_checked_at
            )
            VALUES ($1, $2, $3, $4, TRUE, 'candidate', TRUE, TRUE, TRUE, NOW())
            ON CONFLICT (user_id, username)
            DO UPDATE SET
                title = EXCLUDED.title,
                user_category = COALESCE(EXCLUDED.user_category, user_channels.user_category),
                shared_candidate = TRUE,
                shared_status = CASE
                    WHEN user_channels.shared_status = 'approved' THEN user_channels.shared_status
                    ELSE 'candidate'
                END,
                is_public = TRUE,
                is_available = TRUE,
                is_active = TRUE,
                last_checked_at = NOW(),
                updated_at = NOW()
            RETURNING id, user_id, username, title, user_category, shared_candidate, shared_status;
            """,
            user_id,
            username,
            title,
            user_category,
        )

    return dict(channel)


async def user_has_channel(user_id: int, username: str) -> bool:
    """Проверяет, есть ли уже этот канал в личном списке пользователя."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        exists = await connection.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM user_channels
                WHERE user_id = $1
                  AND username = $2
                  AND is_active = TRUE
            );
            """,
            user_id,
            username,
        )

    return bool(exists)


async def get_user_channels(user_id: int, user_category: str | None = None) -> list[dict]:
    """Возвращает активные личные каналы пользователя. Можно отфильтровать по категории."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                id,
                username,
                title,
                COALESCE(user_category, 'другое') AS user_category,
                shared_candidate,
                shared_status
            FROM user_channels
            WHERE user_id = $1
              AND is_active = TRUE
              AND ($2::text IS NULL OR user_category = $2)
            ORDER BY COALESCE(user_category, 'другое'), created_at, id;
            """,
            user_id,
            user_category,
        )

    return [dict(row) for row in rows]


async def get_user_channel_categories(user_id: int) -> list[dict]:
    """Возвращает категории личных каналов пользователя и количество каналов в каждой."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                COALESCE(user_category, 'другое') AS user_category,
                COUNT(*)::int AS channels_count
            FROM user_channels
            WHERE user_id = $1
              AND is_active = TRUE
            GROUP BY COALESCE(user_category, 'другое')
            ORDER BY channels_count DESC, user_category ASC;
            """,
            user_id,
        )

    return [dict(row) for row in rows]


async def get_user_channels_for_query(
    user_id: int,
    query_category: str | None,
    additional_categories: list[str] | None = None,
    limit: int = 30,
) -> list[dict]:
    """
    Подбирает личные каналы пользователя по ручной категории для общего поиска.
    Пока это простой и предсказуемый матч по user_category.
    """
    categories: list[str] = []

    for category in [query_category, *(additional_categories or [])]:
        if not category or category == 'неясно':
            continue
        if category not in categories:
            categories.append(category)

    if not categories:
        return []

    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                id,
                username,
                title,
                COALESCE(user_category, 'другое') AS user_category,
                shared_candidate,
                shared_status
            FROM user_channels
            WHERE user_id = $1
              AND is_active = TRUE
              AND user_category = ANY($2::text[])
            ORDER BY created_at ASC, id ASC
            LIMIT $3;
            """,
            user_id,
            categories,
            limit,
        )

    return [dict(row) for row in rows]


async def get_user_channel_digest_period_start(
    user_id: int,
    user_channel_ids: list[int],
) -> dict:
    """
    Возвращает дату старта для режима «с прошлого дайджеста».

    Если хотя бы по одному выбранному каналу ещё не было успешной сводки,
    возвращает ok=False, чтобы пользователь явно выбрал обычный период.
    """
    ids = [int(item) for item in user_channel_ids if item is not None]
    if not ids:
        return {
            "ok": False,
            "date_from": None,
            "missing_channel_ids": [],
            "error": "Не выбраны каналы для дайджеста.",
        }

    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                user_channel_id,
                last_digest_at,
                last_message_date
            FROM user_channel_digest_state
            WHERE user_id = $1
              AND user_channel_id = ANY($2::bigint[]);
            """,
            user_id,
            ids,
        )

    by_id = {int(row["user_channel_id"]): dict(row) for row in rows}
    missing_ids = [channel_id for channel_id in ids if not by_id.get(channel_id) or not by_id[channel_id].get("last_digest_at")]

    if missing_ids:
        return {
            "ok": False,
            "date_from": None,
            "missing_channel_ids": missing_ids,
            "error": "Для части выбранных каналов ещё не было прошлой сводки.",
        }

                                                                        
                                                                                             
    date_from = min(row["last_digest_at"] for row in by_id.values())

    if date_from is not None and getattr(date_from, "tzinfo", None) is None:
        date_from = date_from.replace(tzinfo=timezone.utc)

    return {
        "ok": True,
        "date_from": date_from,
        "missing_channel_ids": [],
        "error": None,
    }


async def save_user_channel_digest_state(
    user_id: int,
    user_channel_ids: list[int],
    last_digest_at,
    last_message_date=None,
) -> None:
    """Запоминает, до какого момента пользователь получил дайджест по выбранным каналам."""
    ids = [int(item) for item in user_channel_ids if item is not None]
    if not ids:
        return

    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.executemany(
            """
            INSERT INTO user_channel_digest_state (
                user_id,
                user_channel_id,
                last_digest_at,
                last_message_date,
                updated_at
            )
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (user_id, user_channel_id)
            DO UPDATE SET
                last_digest_at = EXCLUDED.last_digest_at,
                last_message_date = COALESCE(EXCLUDED.last_message_date, user_channel_digest_state.last_message_date),
                updated_at = NOW();
            """,
            [
                (
                    user_id,
                    channel_id,
                    to_db_timestamp(last_digest_at),
                    to_db_timestamp(last_message_date),
                )
                for channel_id in ids
            ],
        )


async def create_weekly_digest_job(
    user_id: int,
    period_from,
    period_to,
    selected_user_channel_ids: list[int],
    channels_count: int,
    messages_count: int,
) -> int:
    """Создаёт задачу иерархического дайджеста: канал -> общая сводка."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        job_id = await connection.fetchval(
            """
            INSERT INTO weekly_digest_jobs (
                user_id,
                status,
                period_from,
                period_to,
                selected_user_channel_ids,
                channels_count,
                messages_count,
                started_at
            )
            VALUES ($1, 'running', $2, $3, $4::jsonb, $5, $6, NOW())
            RETURNING id;
            """,
            user_id,
            to_db_timestamp(period_from),
            to_db_timestamp(period_to),
            json.dumps([int(item) for item in selected_user_channel_ids if item is not None], ensure_ascii=False),
            int(channels_count or 0),
            int(messages_count or 0),
        )

    return int(job_id)


async def save_weekly_channel_digest_part(
    job_id: int,
    user_channel_id: int | None,
    username: str,
    title: str | None,
    messages_count: int,
    summary_json: dict,
    model: str | None = None,
) -> None:
    """Сохраняет промежуточную ИИ-сводку одного канала."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO weekly_channel_digest_parts (
                job_id,
                user_channel_id,
                username,
                title,
                messages_count,
                summary_json,
                model
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
            ON CONFLICT (job_id, user_channel_id)
            DO UPDATE SET
                username = EXCLUDED.username,
                title = EXCLUDED.title,
                messages_count = EXCLUDED.messages_count,
                summary_json = EXCLUDED.summary_json,
                model = EXCLUDED.model;
            """,
            int(job_id),
            int(user_channel_id) if user_channel_id is not None else None,
            username,
            title,
            int(messages_count or 0),
            json.dumps(summary_json or {}, ensure_ascii=False),
            model,
        )


async def finish_weekly_digest_job(
    job_id: int,
    status: str,
    channel_digests_count: int,
    final_summary_json: dict,
    final_text: str | None,
    model: str | None = None,
    error_text: str | None = None,
) -> None:
    """Завершает задачу и сохраняет итоговый общий дайджест."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE weekly_digest_jobs
            SET
                status = $1,
                channel_digests_count = $2,
                final_summary_json = $3::jsonb,
                final_text = $4,
                model = $5,
                error_text = $6,
                finished_at = NOW()
            WHERE id = $7;
            """,
            status,
            int(channel_digests_count or 0),
            json.dumps(final_summary_json or {}, ensure_ascii=False),
            final_text,
            model,
            error_text,
            int(job_id),
        )




async def list_user_digest_history(user_id: int, limit: int = 10) -> list[dict]:
    """Возвращает последние сохранённые дайджесты пользователя.

    История строится на weekly_digest_jobs: ручные дайджесты и автосводки
    используют один движок и сохраняют final_text здесь.
    """
    db_pool = _get_pool()
    safe_limit = max(1, min(int(limit or 10), 30))

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                id,
                status,
                period_from,
                period_to,
                selected_user_channel_ids,
                channels_count,
                messages_count,
                channel_digests_count,
                model,
                error_text,
                started_at,
                finished_at,
                created_at,
                COALESCE(LENGTH(final_text), 0)::int AS final_text_length
            FROM weekly_digest_jobs
            WHERE user_id = $1
              AND final_text IS NOT NULL
              AND final_text <> ''
            ORDER BY COALESCE(finished_at, created_at) DESC, id DESC
            LIMIT $2;
            """,
            int(user_id),
            safe_limit,
        )

    return [dict(row) for row in rows]


async def get_user_digest_history_item(user_id: int, digest_job_id: int) -> dict | None:
    """Возвращает одну старую сводку пользователя с финальным текстом."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                id,
                status,
                period_from,
                period_to,
                selected_user_channel_ids,
                channels_count,
                messages_count,
                channel_digests_count,
                final_summary_json,
                final_text,
                model,
                error_text,
                started_at,
                finished_at,
                created_at
            FROM weekly_digest_jobs
            WHERE user_id = $1
              AND id = $2
              AND final_text IS NOT NULL
              AND final_text <> '';
            """,
            int(user_id),
            int(digest_job_id),
        )

    return dict(row) if row else None


async def count_user_digest_history(user_id: int) -> int:
    """Считает сохранённые дайджесты пользователя."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        value = await connection.fetchval(
            """
            SELECT COUNT(*)::int
            FROM weekly_digest_jobs
            WHERE user_id = $1
              AND final_text IS NOT NULL
              AND final_text <> '';
            """,
            int(user_id),
        )

    return int(value or 0)


async def get_user_channel_subscription_usage(user_id: int, user_channel_id: int) -> list[dict]:
    """Показывает, в каких автосводках используется личный канал пользователя."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                s.id,
                s.title,
                s.is_active,
                s.period_days,
                s.send_time,
                s.timezone,
                s.next_run_at
            FROM digest_subscriptions s
            JOIN digest_subscription_channels sc ON sc.subscription_id = s.id
            JOIN user_channels uc ON uc.id = sc.user_channel_id
            WHERE s.user_id = $1
              AND uc.user_id = $1
              AND uc.id = $2
            ORDER BY s.is_active DESC, s.created_at DESC, s.id DESC;
            """,
            int(user_id),
            int(user_channel_id),
        )

    return [dict(row) for row in rows]


async def remove_user_channel_with_subscription_links(user_id: int, user_channel_id: int) -> dict:
    """
    Убирает канал из личного списка пользователя и из всех его автосводок.

    Канал удаляется мягко: user_channels.is_active = FALSE.
    Связи digest_subscription_channels удаляются физически, чтобы старые автосводки
    не держали ссылку на неактивный канал и не показывали неверный channels_count.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        async with connection.transaction():
            channel = await connection.fetchrow(
                """
                SELECT id, username, title, user_category
                FROM user_channels
                WHERE user_id = $1
                  AND id = $2
                  AND is_active = TRUE
                FOR UPDATE;
                """,
                int(user_id),
                int(user_channel_id),
            )

            if not channel:
                return {
                    "ok": False,
                    "removed": False,
                    "reason": "not_found",
                    "subscription_links_removed": 0,
                    "channel": None,
                }

            deleted_rows = await connection.fetch(
                """
                DELETE FROM digest_subscription_channels sc
                USING digest_subscriptions s
                WHERE sc.subscription_id = s.id
                  AND s.user_id = $1
                  AND sc.user_channel_id = $2
                RETURNING sc.subscription_id;
                """,
                int(user_id),
                int(user_channel_id),
            )

            await connection.execute(
                """
                UPDATE user_channels
                SET
                    is_active = FALSE,
                    updated_at = NOW()
                WHERE user_id = $1
                  AND id = $2;
                """,
                int(user_id),
                int(user_channel_id),
            )

    return {
        "ok": True,
        "removed": True,
        "reason": None,
        "subscription_links_removed": len(deleted_rows),
        "channel": dict(channel),
    }


async def remove_user_channel(user_id: int, user_channel_id: int) -> bool:
    """Совместимость со старым кодом: удаляет канал вместе со связями автосводок."""
    result = await remove_user_channel_with_subscription_links(user_id, user_channel_id)
    return bool(result.get("removed"))


async def create_parse_job(
    user_id: int,
    source_type: str | None = None,
    selected_channels: str | None = None,
) -> int:
    """Создаёт запись о запуске сбора."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        job_id = await connection.fetchval(
            """
            INSERT INTO parse_jobs (user_id, source_type, selected_channels, status, started_at)
            VALUES ($1, $2, $3, 'running', NOW())
            RETURNING id;
            """,
            user_id,
            source_type,
            selected_channels,
        )

    return int(job_id)


async def finish_parse_job(
    job_id: int,
    status: str,
    messages_found: int,
    messages_saved: int,
    error_text: str | None = None,
) -> None:
    """Завершает запись о сборе."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE parse_jobs
            SET
                status = $1,
                finished_at = NOW(),
                messages_found = $2,
                messages_saved = $3,
                error_text = $4
            WHERE id = $5;
            """,
            status,
            messages_found,
            messages_saved,
            error_text,
            job_id,
        )




def to_db_timestamp(value):
    """
    PostgreSQL column message_date has type TIMESTAMP WITHOUT TIME ZONE.
    Telethon returns timezone-aware UTC datetime.
    asyncpg cannot put aware datetime into a naive TIMESTAMP column,
    so we convert it to naive UTC before saving.
    """
    if value is None:
        return None

    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    return value


async def save_message(
    source_type: str,
    source_channel_id: int | None,
    telegram_channel_id: int | None,
    username: str,
    title: str | None,
    telegram_message_id: int,
    message_text: str | None,
    cleaned_text: str | None,
    message_date,
    has_text: bool,
    has_media: bool,
    is_useful: bool,
    filter_reason: str | None,
    views_count: int | None = None,
    forwards_count: int | None = None,
    replies_count: int | None = None,
) -> dict:
    """
    Сохраняет сообщение в общий кэш.
    Возвращает dict: {id: int, inserted: bool}.
    inserted=True означает, что это новое сообщение, которого раньше не было в БД.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            INSERT INTO messages (
                source_type,
                source_channel_id,
                telegram_channel_id,
                username,
                title,
                telegram_message_id,
                message_text,
                cleaned_text,
                message_date,
                has_text,
                has_media,
                is_useful,
                filter_reason,
                views_count,
                forwards_count,
                replies_count
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            ON CONFLICT (source_type, username, telegram_message_id)
            DO UPDATE SET
                source_channel_id = EXCLUDED.source_channel_id,
                telegram_channel_id = COALESCE(EXCLUDED.telegram_channel_id, messages.telegram_channel_id),
                title = EXCLUDED.title,
                message_text = EXCLUDED.message_text,
                cleaned_text = EXCLUDED.cleaned_text,
                message_date = EXCLUDED.message_date,
                has_text = EXCLUDED.has_text,
                has_media = EXCLUDED.has_media,
                is_useful = EXCLUDED.is_useful,
                filter_reason = EXCLUDED.filter_reason,
                views_count = EXCLUDED.views_count,
                forwards_count = EXCLUDED.forwards_count,
                replies_count = EXCLUDED.replies_count
            RETURNING id, xmax = 0 AS inserted;
            """,
            source_type,
            source_channel_id,
            telegram_channel_id,
            username,
            title,
            telegram_message_id,
            message_text,
            cleaned_text,
            to_db_timestamp(message_date),
            has_text,
            has_media,
            is_useful,
            filter_reason,
            views_count,
            forwards_count,
            replies_count,
        )

    return {
        "id": int(row["id"]),
        "inserted": bool(row["inserted"]),
    }


async def link_message_to_user_result(user_id: int, parse_job_id: int, message_id: int) -> None:
    """Фиксирует, что конкретный пользователь получил это сообщение в конкретном запуске сбора."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO user_message_results (user_id, parse_job_id, message_id)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, parse_job_id, message_id)
            DO NOTHING;
            """,
            user_id,
            parse_job_id,
            message_id,
        )


async def get_cached_useful_messages(
    source_type: str,
    username: str,
    date_from,
    date_to=None,
    limit: int | None = None,
) -> list[dict]:
    """
    Берёт уже сохранённые полезные сообщения из общего кэша по каналу и периоду.

    Важно: возвращаем сначала свежие сообщения.
    Старый вариант сортировал ASC и мог забивать лимит старыми постами,
    из-за чего бот не доходил до свежих сообщений.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                id,
                source_type,
                source_channel_id,
                username,
                title,
                telegram_message_id,
                message_text,
                cleaned_text,
                message_date,
                has_text,
                has_media,
                views_count,
                forwards_count,
                replies_count
            FROM messages
            WHERE source_type = $1
              AND username = $2
              AND message_date >= $3
              AND ($4::timestamp IS NULL OR message_date < $4)
              AND is_useful = TRUE
            ORDER BY message_date DESC NULLS LAST, telegram_message_id DESC
            LIMIT COALESCE($5::int, 2147483647);
            """,
            source_type,
            username,
            to_db_timestamp(date_from),
            to_db_timestamp(date_to),
            limit,
        )

    return [dict(row) for row in rows]

async def get_newest_cached_message_date(
    source_type: str,
    username: str,
    date_from,
    date_to=None,
):
    """Возвращает дату самого свежего сохранённого сообщения по каналу в выбранном периоде."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        newest_date = await connection.fetchval(
            """
            SELECT MAX(message_date)
            FROM messages
            WHERE source_type = $1
              AND username = $2
              AND message_date >= $3
              AND ($4::timestamp IS NULL OR message_date < $4);
            """,
            source_type,
            username,
            to_db_timestamp(date_from),
            to_db_timestamp(date_to),
        )

    return newest_date

async def save_rejected_message(
    source_type: str,
    source_channel_id: int | None,
    username: str,
    title: str | None,
    telegram_message_id: int,
    original_text: str | None,
    cleaned_text: str | None,
    reject_reason: str,
    message_date,
    has_text: bool,
    has_media: bool,
    views_count: int | None = None,
    forwards_count: int | None = None,
    replies_count: int | None = None,
) -> None:
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO rejected_messages (
                source_type,
                source_channel_id,
                username,
                title,
                telegram_message_id,
                original_text,
                cleaned_text,
                reject_reason,
                message_date,
                has_text,
                has_media,
                views_count,
                forwards_count,
                replies_count
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (source_type, username, telegram_message_id, reject_reason)
            DO UPDATE SET
                original_text = EXCLUDED.original_text,
                cleaned_text = EXCLUDED.cleaned_text,
                message_date = EXCLUDED.message_date,
                has_text = EXCLUDED.has_text,
                has_media = EXCLUDED.has_media,
                views_count = EXCLUDED.views_count,
                forwards_count = EXCLUDED.forwards_count,
                replies_count = EXCLUDED.replies_count;
            """,
            source_type,
            source_channel_id,
            username,
            title,
            telegram_message_id,
            original_text,
            cleaned_text,
            reject_reason,
            to_db_timestamp(message_date),
            has_text,
            has_media,
            views_count,
            forwards_count,
            replies_count,
        )

async def get_unmarked_channels(limit: int = 20) -> list[dict]:
    """
    Берёт каналы из общей таблицы channels, которые ещё не размечены ИИ.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                id,
                username,
                title,
                description,
                category
            FROM channels
            WHERE is_available = TRUE
              AND (
                    ai_checked_at IS NULL
                    OR ai_keywords = '[]'::jsonb
                  )
            ORDER BY created_at ASC, id ASC
            LIMIT $1;
            """,
            limit,
        )

    return [dict(row) for row in rows]


async def save_channel_ai_keywords(
    channel_id: int,
    ai_keywords: list[str],
    ai_summary: str | None,
) -> None:
    """
    Сохраняет простую ИИ-разметку для общей таблицы channels.
    Старое имя ai_summary оставлено для совместимости.
    Новое поле ai_description заполняется тем же текстом.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE channels
            SET
                ai_keywords = $1::jsonb,
                ai_summary = $2,
                ai_description = $2,
                ai_checked_at = NOW(),
                updated_at = NOW()
            WHERE id = $3;
            """,
            json.dumps(ai_keywords, ensure_ascii=False),
            ai_summary,
            channel_id,
        )


async def save_channel_ai_classification(
    channel_id: int,
    ai_keywords: list[str],
    ai_description: str | None,
    ai_classification: dict,
) -> None:
    """
    Сохраняет полную разметку канала по tax_tree.py.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE channels
            SET
                ai_keywords = $1::jsonb,
                ai_description = $2,
                ai_summary = $2,
                ai_classification = $3::jsonb,
                ai_checked_at = NOW(),
                processed_at = NOW(),
                status = 'done',
                error = NULL,
                updated_at = NOW()
            WHERE id = $4;
            """,
            json.dumps(ai_keywords, ensure_ascii=False),
            ai_description,
            json.dumps(ai_classification, ensure_ascii=False),
            channel_id,
        )


async def save_channel_ai_markup_to_table(
    channel_id: int,
    username: str,
    ai_keywords: list[str],
    ai_description: str | None,
    ai_classification: dict,
    ai_warnings: list[str] | None = None,
    model: str | None = None,
) -> None:
    """
    Сохраняет полную разметку канала в отдельную таблицу channel_ai_markup.
    Старую текущую разметку оставляет в истории через is_current = FALSE.
    """
    ai_warnings = ai_warnings or []

    category = ai_classification.get("категория")
    region = ai_classification.get("регион")
    content_format = ai_classification.get("формат")
    position_label = ai_classification.get("позиция") or ai_classification.get("позиция подачи")

    confidence_raw = ai_classification.get("уверенность")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else None
    except (TypeError, ValueError):
        confidence = None

    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE channel_ai_markup
                SET is_current = FALSE,
                    updated_at = NOW()
                WHERE channel_id = $1
                  AND is_current = TRUE;
                """,
                channel_id,
            )

            await connection.execute(
                """
                INSERT INTO channel_ai_markup (
                    channel_id,
                    username,
                    status,
                    model,
                    category,
                    region,
                    content_format,
                    position_label,
                    confidence,
                    ai_keywords,
                    ai_description,
                    ai_classification,
                    ai_warnings,
                    is_current,
                    processed_at
                )
                VALUES (
                    $1,
                    $2,
                    'done',
                    $3,
                    $4,
                    $5,
                    $6,
                    $7,
                    $8,
                    $9::jsonb,
                    $10,
                    $11::jsonb,
                    $12::jsonb,
                    TRUE,
                    NOW()
                );
                """,
                channel_id,
                username,
                model,
                category,
                region,
                content_format,
                position_label,
                confidence,
                json.dumps(ai_keywords, ensure_ascii=False),
                ai_description,
                json.dumps(ai_classification, ensure_ascii=False),
                json.dumps(ai_warnings, ensure_ascii=False),
            )

            await connection.execute(
                """
                UPDATE channels
                SET status = 'done',
                    error = NULL,
                    processed_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1;
                """,
                channel_id,
            )


async def get_unmarked_user_channels(limit: int = 20) -> list[dict]:
    """
    Берёт пользовательские каналы, которые ещё не размечены ИИ.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                id,
                user_id,
                username,
                title
            FROM user_channels
            WHERE is_available = TRUE
              AND is_active = TRUE
              AND (
                    ai_checked_at IS NULL
                    OR ai_keywords = '[]'::jsonb
                  )
            ORDER BY created_at ASC, id ASC
            LIMIT $1;
            """,
            limit,
        )

    return [dict(row) for row in rows]


async def save_user_channel_ai_keywords(
    user_channel_id: int,
    ai_keywords: list[str],
    ai_summary: str | None,
) -> None:
    """
    Сохраняет ИИ-разметку для пользовательского канала.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE user_channels
            SET
                ai_keywords = $1::jsonb,
                ai_summary = $2,
                ai_description = $2,
                ai_checked_at = NOW(),
                updated_at = NOW()
            WHERE id = $3;
            """,
            json.dumps(ai_keywords, ensure_ascii=False),
            ai_summary,
            user_channel_id,
        )


async def get_unmarked_channels_for_ai(limit: int = 10) -> list[dict]:
    """
    Берёт каналы из общей таблицы channels, которые ещё не размечены ИИ.
    user_channels не трогаем.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                id,
                username,
                title,
                description,
                category
            FROM channels
            WHERE is_available = TRUE
              AND (
                    ai_checked_at IS NULL
                    OR ai_keywords = '[]'::jsonb
                  )
            ORDER BY created_at ASC, id ASC
            LIMIT $1;
            """,
            limit,
        )

    return [dict(row) for row in rows]


async def get_last_cleaned_messages_for_channel(
    channel_id: int,
    limit: int = 10,
) -> list[str]:
    """
    Берёт последние полезные очищенные сообщения канала.
    Используется для ИИ-разметки keywords.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT cleaned_text
            FROM messages
            WHERE source_channel_id = $1
              AND is_useful = TRUE
              AND cleaned_text IS NOT NULL
              AND length(trim(cleaned_text)) >= 30
            ORDER BY message_date DESC NULLS LAST, id DESC
            LIMIT $2;
            """,
            channel_id,
            limit,
        )

    return [row["cleaned_text"] for row in rows]


async def save_channel_ai_markup(
    channel_id: int,
    ai_keywords: list[str],
    ai_summary: str | None,
) -> None:
    """
    Сохраняет простую ИИ-разметку канала.
    Старое имя оставлено для совместимости.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        await connection.execute(
            """
            UPDATE channels
            SET
                ai_keywords = $1::jsonb,
                ai_summary = $2,
                ai_description = $2,
                ai_checked_at = NOW(),
                updated_at = NOW()
            WHERE id = $3;
            """,
            json.dumps(ai_keywords, ensure_ascii=False),
            ai_summary,
            channel_id,
        )



                           
                        
                           

def _ensure_aware_utc(value):
    return ensure_aware_utc(value)


def _to_db_send_time(value):
    """PostgreSQL TIME через asyncpg должен получать datetime.time, а не строку HH:MM."""
    return parse_send_time(value or DEFAULT_SEND_TIME)


def calculate_next_digest_run_at(
    period_days: int,
    from_time=None,
    send_time: str | None = DEFAULT_SEND_TIME,
    timezone_name: str | None = DEFAULT_TIMEZONE,
):
    """Следующий запуск в локальное время пользователя, сохранённое в подписке."""
    return calculate_next_run_at(
        period_days=period_days,
        send_time=send_time or DEFAULT_SEND_TIME,
        timezone_name=timezone_name or DEFAULT_TIMEZONE,
        from_time=from_time,
    )



def normalize_digest_subscription_preset(value: str | None) -> str:
    preset = (value or "normal").strip().lower()
    return preset if preset in {"brief", "normal", "detailed"} else "normal"


async def create_digest_subscription(
    user_id: int,
    user_channel_ids: list[int],
    period_days: int,
    title: str | None = None,
    digest_preset: str = "normal",
    send_time: str | None = DEFAULT_SEND_TIME,
    timezone_name: str | None = DEFAULT_TIMEZONE,
) -> dict:
    """Создаёт активную подписку на автосводку по выбранным личным каналам."""
    raw_ids = [int(item) for item in user_channel_ids if item is not None]
    if not raw_ids:
        raise ValueError("Нельзя создать автосводку без каналов")

                                                                                         
                                                                                
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        owned_rows = await connection.fetch(
            """
            SELECT id
            FROM user_channels
            WHERE user_id = $1
              AND is_active = TRUE
              AND id = ANY($2::bigint[]);
            """,
            int(user_id),
            raw_ids,
        )
        owned_ids = {int(row["id"]) for row in owned_rows}

    ids = []
    for channel_id in raw_ids:
        if channel_id in owned_ids and channel_id not in ids:
            ids.append(channel_id)

    if not ids:
        raise ValueError("В выбранном списке нет активных личных каналов этого пользователя")

    if len(ids) != len(set(raw_ids)):
        raise ValueError("Часть выбранных каналов не принадлежит пользователю или уже удалена")

    period_days = max(1, int(period_days or 7))
    digest_preset = normalize_digest_subscription_preset(digest_preset)
    send_time_value = _to_db_send_time(send_time or DEFAULT_SEND_TIME)
    send_time_text = format_send_time(send_time_value)
    timezone_text = normalize_timezone_name(timezone_name or DEFAULT_TIMEZONE)
    next_run_at = calculate_next_digest_run_at(
        period_days,
        send_time=send_time_value,
        timezone_name=timezone_text,
    )

    async with db_pool.acquire() as connection:
        async with connection.transaction():
            sub = await connection.fetchrow(
                """
                INSERT INTO digest_subscriptions (
                    user_id,
                    title,
                    is_active,
                    period_days,
                    send_time,
                    timezone,
                    digest_preset,
                    next_run_at,
                    updated_at
                )
                VALUES ($1, $2, TRUE, $3, $4, $5, $6, $7, NOW())
                RETURNING id, user_id, title, is_active, period_days, send_time, timezone, digest_preset, next_run_at;
                """,
                int(user_id),
                title or f"Автосводка раз в {period_days} дн.",
                period_days,
                send_time_value,
                timezone_text,
                digest_preset,
                to_db_timestamp(next_run_at),
            )
            subscription_id = int(sub["id"])
            await connection.executemany(
                """
                INSERT INTO digest_subscription_channels (subscription_id, user_channel_id, position)
                VALUES ($1, $2, $3)
                ON CONFLICT (subscription_id, user_channel_id)
                DO UPDATE SET position = EXCLUDED.position;
                """,
                [(subscription_id, channel_id, index) for index, channel_id in enumerate(ids)],
            )
    return dict(sub)


async def list_digest_subscriptions(user_id: int, active_only: bool = True) -> list[dict]:
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                s.id,
                s.title,
                s.is_active,
                s.period_days,
                s.send_time,
                s.timezone,
                s.digest_preset,
                s.last_success_from,
                s.last_success_to,
                s.last_success_at,
                s.next_run_at,
                s.last_error_text,
                COUNT(uc.id)::int AS channels_count,
                COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'id', uc.id,
                            'username', uc.username,
                            'title', uc.title,
                            'user_category', COALESCE(uc.user_category, 'другое')
                        )
                        ORDER BY sc.position, sc.id
                    ) FILTER (WHERE uc.id IS NOT NULL),
                    '[]'::jsonb
                ) AS channels
            FROM digest_subscriptions s
            LEFT JOIN digest_subscription_channels sc ON sc.subscription_id = s.id
            LEFT JOIN user_channels uc ON uc.id = sc.user_channel_id AND uc.is_active = TRUE
            WHERE s.user_id = $1
              AND ($2::boolean = FALSE OR s.is_active = TRUE)
            GROUP BY s.id
            ORDER BY s.is_active DESC, s.created_at DESC, s.id DESC;
            """,
            int(user_id),
            bool(active_only),
        )
    return [dict(row) for row in rows]


async def disable_digest_subscription(user_id: int, subscription_id: int) -> bool:
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET is_active = FALSE,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = NOW()
            WHERE id = $1
              AND user_id = $2
              AND is_active = TRUE;
            """,
            int(subscription_id),
            int(user_id),
        )
    return result == "UPDATE 1"




async def enable_digest_subscription(user_id: int, subscription_id: int) -> bool:
    """Включает ранее отключённую автосводку и назначает новый ближайший запуск."""
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT period_days, send_time, timezone
            FROM digest_subscriptions
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
        )
        if not row:
            return False

        next_run_at = calculate_next_digest_run_at(
            int(row["period_days"] or 7),
            send_time=row["send_time"] or DEFAULT_SEND_TIME,
            timezone_name=row["timezone"] or DEFAULT_TIMEZONE,
        )
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET is_active = TRUE,
                next_run_at = $3,
                locked_at = NULL,
                locked_by = NULL,
                last_error_text = NULL,
                updated_at = NOW()
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
            to_db_timestamp(next_run_at),
        )
    return result == "UPDATE 1"


async def update_digest_subscription_period(user_id: int, subscription_id: int, period_days: int) -> bool:
    """Меняет частоту автосводки. История успешного парса сохраняется."""
    period_days = max(1, int(period_days or 7))
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT send_time, timezone
            FROM digest_subscriptions
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
        )
        if not row:
            return False

        next_run_at = calculate_next_digest_run_at(
            period_days,
            send_time=row["send_time"] or DEFAULT_SEND_TIME,
            timezone_name=row["timezone"] or DEFAULT_TIMEZONE,
        )
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET period_days = $3,
                next_run_at = $4,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = NOW()
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
            int(period_days),
            to_db_timestamp(next_run_at),
        )
    return result == "UPDATE 1"


async def update_digest_subscription_preset(user_id: int, subscription_id: int, digest_preset: str) -> bool:
    """Меняет формат автосводки: brief / normal / detailed. Расписание и last_success_to не трогаем."""
    preset = normalize_digest_subscription_preset(digest_preset)
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET digest_preset = $3,
                updated_at = NOW()
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
            preset,
        )
    return result == "UPDATE 1"


async def update_digest_subscription_time(user_id: int, subscription_id: int, send_time: str) -> bool:
    """Меняет локальное время отправки и пересчитывает ближайший запуск."""
    send_time_value = _to_db_send_time(send_time)
    send_time_text = format_send_time(send_time_value)
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT period_days, timezone
            FROM digest_subscriptions
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
        )
        if not row:
            return False

        next_run_at = calculate_next_digest_run_at(
            int(row["period_days"] or 7),
            send_time=send_time_value,
            timezone_name=row["timezone"] or DEFAULT_TIMEZONE,
        )
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET send_time = $3,
                next_run_at = $4,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = NOW()
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
            send_time_value,
            to_db_timestamp(next_run_at),
        )
    return result == "UPDATE 1"


async def update_digest_subscription_timezone(user_id: int, subscription_id: int, timezone_name: str) -> bool:
    """Меняет timezone и пересчитывает ближайший запуск для сохранённого локального времени."""
    timezone_text = normalize_timezone_name(timezone_name or DEFAULT_TIMEZONE)
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT period_days, send_time
            FROM digest_subscriptions
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
        )
        if not row:
            return False

        next_run_at = calculate_next_digest_run_at(
            int(row["period_days"] or 7),
            send_time=row["send_time"] or DEFAULT_SEND_TIME,
            timezone_name=timezone_text,
        )
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET timezone = $3,
                next_run_at = $4,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = NOW()
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
            timezone_text,
            to_db_timestamp(next_run_at),
        )
    return result == "UPDATE 1"


async def delete_digest_subscription(user_id: int, subscription_id: int) -> bool:
    """Удаляет автосводку пользователя. История запусков удалится каскадом."""
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            DELETE FROM digest_subscriptions
            WHERE id = $1
              AND user_id = $2;
            """,
            int(subscription_id),
            int(user_id),
        )
    return result == "DELETE 1"




async def add_user_channels_to_digest_subscription(
    user_id: int,
    subscription_id: int,
    user_channel_ids: list[int],
) -> dict:
    """Добавляет активные личные каналы пользователя в автосводку.

    Безопасность:
    - подписка должна принадлежать user_id;
    - каналы должны принадлежать user_id и быть активными;
    - дубли silently игнорируются;
    - возвращается подробный результат для UI.
    """
    raw_ids: list[int] = []
    for item in user_channel_ids or []:
        try:
            channel_id = int(item)
        except (TypeError, ValueError):
            continue
        if channel_id not in raw_ids:
            raw_ids.append(channel_id)

    if not raw_ids:
        return {"ok": False, "reason": "empty", "added_count": 0, "added_channels": [], "channels_count": 0}

    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        async with connection.transaction():
            subscription = await connection.fetchrow(
                """
                SELECT id
                FROM digest_subscriptions
                WHERE id = $1
                  AND user_id = $2
                FOR UPDATE;
                """,
                int(subscription_id),
                int(user_id),
            )
            if not subscription:
                return {"ok": False, "reason": "subscription_not_found", "added_count": 0, "added_channels": [], "channels_count": 0}

            current_rows = await connection.fetch(
                """
                SELECT sc.user_channel_id
                FROM digest_subscription_channels sc
                JOIN user_channels uc ON uc.id = sc.user_channel_id
                WHERE sc.subscription_id = $1
                  AND uc.user_id = $2
                  AND uc.is_active = TRUE;
                """,
                int(subscription_id),
                int(user_id),
            )
            current_ids = [int(row["user_channel_id"]) for row in current_rows]
            current_set = set(current_ids)

            owned_rows = await connection.fetch(
                """
                SELECT id, username, title, COALESCE(user_category, 'другое') AS user_category
                FROM user_channels
                WHERE user_id = $1
                  AND is_active = TRUE
                  AND id = ANY($2::bigint[])
                ORDER BY created_at ASC, id ASC;
                """,
                int(user_id),
                raw_ids,
            )
            owned_by_id = {int(row["id"]): dict(row) for row in owned_rows}
            ids_to_add = [channel_id for channel_id in raw_ids if channel_id in owned_by_id and channel_id not in current_set]

            if not ids_to_add:
                return {
                    "ok": True,
                    "reason": "nothing_to_add",
                    "added_count": 0,
                    "added_channels": [],
                    "channels_count": len(current_ids),
                }

            start_position = await connection.fetchval(
                """
                SELECT COALESCE(MAX(position), -1) + 1
                FROM digest_subscription_channels
                WHERE subscription_id = $1;
                """,
                int(subscription_id),
            )
            start_position = int(start_position or 0)

            await connection.executemany(
                """
                INSERT INTO digest_subscription_channels (subscription_id, user_channel_id, position)
                VALUES ($1, $2, $3)
                ON CONFLICT (subscription_id, user_channel_id)
                DO UPDATE SET position = EXCLUDED.position;
                """,
                [
                    (int(subscription_id), int(channel_id), start_position + index)
                    for index, channel_id in enumerate(ids_to_add)
                ],
            )

            await connection.execute(
                """
                UPDATE digest_subscriptions
                SET updated_at = NOW()
                WHERE id = $1
                  AND user_id = $2;
                """,
                int(subscription_id),
                int(user_id),
            )

    return {
        "ok": True,
        "reason": "added",
        "added_count": len(ids_to_add),
        "added_channels": [owned_by_id[channel_id] for channel_id in ids_to_add],
        "channels_count": len(current_ids) + len(ids_to_add),
    }


async def remove_user_channels_from_digest_subscription(
    user_id: int,
    subscription_id: int,
    user_channel_ids: list[int],
) -> dict:
    """Убирает каналы из автосводки, но не удаляет их из личного списка.

    Последний канал удалить нельзя: автосводка без каналов не имеет смысла и
    потом будет падать/давать пустые результаты.
    """
    raw_ids: list[int] = []
    for item in user_channel_ids or []:
        try:
            channel_id = int(item)
        except (TypeError, ValueError):
            continue
        if channel_id not in raw_ids:
            raw_ids.append(channel_id)

    if not raw_ids:
        return {"ok": False, "reason": "empty", "removed_count": 0, "removed_channels": [], "channels_count": 0}

    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        async with connection.transaction():
            subscription = await connection.fetchrow(
                """
                SELECT id
                FROM digest_subscriptions
                WHERE id = $1
                  AND user_id = $2
                FOR UPDATE;
                """,
                int(subscription_id),
                int(user_id),
            )
            if not subscription:
                return {"ok": False, "reason": "subscription_not_found", "removed_count": 0, "removed_channels": [], "channels_count": 0}

            current_rows = await connection.fetch(
                """
                SELECT
                    uc.id,
                    uc.username,
                    uc.title,
                    COALESCE(uc.user_category, 'другое') AS user_category
                FROM digest_subscription_channels sc
                JOIN user_channels uc ON uc.id = sc.user_channel_id
                WHERE sc.subscription_id = $1
                  AND uc.user_id = $2
                  AND uc.is_active = TRUE
                ORDER BY sc.position, sc.id;
                """,
                int(subscription_id),
                int(user_id),
            )
            current_channels = [dict(row) for row in current_rows]
            current_ids = [int(row["id"]) for row in current_channels]
            current_set = set(current_ids)
            ids_to_remove = [channel_id for channel_id in raw_ids if channel_id in current_set]

            if not ids_to_remove:
                return {
                    "ok": True,
                    "reason": "nothing_to_remove",
                    "removed_count": 0,
                    "removed_channels": [],
                    "channels_count": len(current_ids),
                }

            if len(current_ids) - len(ids_to_remove) <= 0:
                return {
                    "ok": False,
                    "reason": "would_be_empty",
                    "removed_count": 0,
                    "removed_channels": [],
                    "channels_count": len(current_ids),
                }

            removed_by_id = {int(row["id"]): row for row in current_channels if int(row["id"]) in ids_to_remove}

            await connection.execute(
                """
                DELETE FROM digest_subscription_channels sc
                USING digest_subscriptions s, user_channels uc
                WHERE sc.subscription_id = s.id
                  AND sc.user_channel_id = uc.id
                  AND s.id = $1
                  AND s.user_id = $2
                  AND uc.user_id = $2
                  AND sc.user_channel_id = ANY($3::bigint[]);
                """,
                int(subscription_id),
                int(user_id),
                ids_to_remove,
            )

            await connection.execute(
                """
                UPDATE digest_subscriptions
                SET updated_at = NOW()
                WHERE id = $1
                  AND user_id = $2;
                """,
                int(subscription_id),
                int(user_id),
            )

                                                                         
            remaining_rows = await connection.fetch(
                """
                SELECT sc.id
                FROM digest_subscription_channels sc
                JOIN user_channels uc ON uc.id = sc.user_channel_id
                WHERE sc.subscription_id = $1
                  AND uc.user_id = $2
                  AND uc.is_active = TRUE
                ORDER BY sc.position, sc.id;
                """,
                int(subscription_id),
                int(user_id),
            )
            await connection.executemany(
                """
                UPDATE digest_subscription_channels
                SET position = $2
                WHERE id = $1;
                """,
                [(int(row["id"]), index) for index, row in enumerate(remaining_rows)],
            )

    return {
        "ok": True,
        "reason": "removed",
        "removed_count": len(ids_to_remove),
        "removed_channels": [removed_by_id[channel_id] for channel_id in ids_to_remove],
        "channels_count": len(current_ids) - len(ids_to_remove),
    }


async def lock_digest_subscription_now(
    user_id: int,
    subscription_id: int,
    locked_by: str = "debug-button",
    lock_minutes: int = 60,
) -> bool:
    """
    Ручной/debug-запуск должен брать тот же DB-lock, что и scheduler.
    Важно для нескольких пользователей: пользователь может залочить только свою подписку.
    """
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET locked_at = NOW(),
                locked_by = $3,
                updated_at = NOW()
            WHERE id = $1
              AND user_id = $2
              AND is_active = TRUE
              AND (
                    locked_at IS NULL
                    OR locked_at < NOW() - ($4::int * INTERVAL '1 minute')
                  );
            """,
            int(subscription_id),
            int(user_id),
            str(locked_by),
            int(lock_minutes or 60),
        )
    return result == "UPDATE 1"


async def get_digest_subscription_for_run(subscription_id: int, locked_by: str | None = None) -> dict | None:
    """
    Возвращает подписку для запуска вместе с telegram_id и активными каналами.
    Если locked_by задан — дополнительно проверяем, что подписка действительно залочена этим worker-ом.
    """
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                s.*,
                u.telegram_id,
                COALESCE(
                    jsonb_agg(
                        jsonb_build_object(
                            'id', uc.id,
                            'username', uc.username,
                            'title', uc.title,
                            'user_category', COALESCE(uc.user_category, 'другое')
                        )
                        ORDER BY sc.position, sc.id
                    ) FILTER (WHERE uc.id IS NOT NULL),
                    '[]'::jsonb
                ) AS channels
            FROM digest_subscriptions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN digest_subscription_channels sc ON sc.subscription_id = s.id
            LEFT JOIN user_channels uc ON uc.id = sc.user_channel_id AND uc.is_active = TRUE
            WHERE s.id = $1
              AND s.is_active = TRUE
              AND ($2::text IS NULL OR s.locked_by = $2)
            GROUP BY s.id, u.telegram_id;
            """,
            int(subscription_id),
            locked_by,
        )
    return dict(row) if row else None


async def create_digest_subscription_run(
    subscription_id: int,
    user_id: int,
    period_from,
    period_to,
    channels_count: int = 0,
) -> int:
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            INSERT INTO digest_subscription_runs (
                subscription_id,
                user_id,
                status,
                period_from,
                period_to,
                channels_count,
                started_at,
                created_at
            )
            VALUES ($1, $2, 'running', $3, $4, $5, NOW(), NOW())
            RETURNING id;
            """,
            int(subscription_id),
            int(user_id),
            to_db_timestamp(period_from),
            to_db_timestamp(period_to),
            int(channels_count or 0),
        )
    return int(row["id"])


async def finish_digest_subscription_run(
    run_id: int,
    status: str,
    messages_count: int = 0,
    error_text: str | None = None,
    sent: bool = False,
) -> bool:
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE digest_subscription_runs
            SET status = $2,
                messages_count = $3,
                error_text = $4,
                finished_at = NOW(),
                sent_at = CASE WHEN $5::boolean THEN NOW() ELSE sent_at END
            WHERE id = $1;
            """,
            int(run_id),
            str(status),
            int(messages_count or 0),
            error_text,
            bool(sent),
        )
    return result == "UPDATE 1"


async def mark_digest_subscription_success(
    subscription_id: int,
    period_from,
    period_to,
) -> bool:
    """
    Успешный запуск двигает last_success_to и переносит next_run_at.
    next_run_at считаем от прошлого расписания, чтобы локальное время не плавало.
    """
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT period_days, send_time, timezone, next_run_at
            FROM digest_subscriptions
            WHERE id = $1;
            """,
            int(subscription_id),
        )
        if not row:
            return False

        period_days = int(row["period_days"] or 7)
        send_time_value = row["send_time"] or _to_db_send_time(DEFAULT_SEND_TIME)
        timezone_text = row["timezone"] or DEFAULT_TIMEZONE
        base_time = row["next_run_at"] or period_to
        next_run_at = calculate_next_digest_run_at(
            period_days=period_days,
            from_time=base_time,
            send_time=send_time_value,
            timezone_name=timezone_text,
        )

        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET last_success_from = $2,
                last_success_to = $3,
                last_success_at = NOW(),
                next_run_at = $4,
                locked_at = NULL,
                locked_by = NULL,
                last_error_text = NULL,
                updated_at = NOW()
            WHERE id = $1;
            """,
            int(subscription_id),
            to_db_timestamp(period_from),
            to_db_timestamp(period_to),
            to_db_timestamp(next_run_at),
        )
    return result == "UPDATE 1"


async def release_digest_subscription_lock(
    subscription_id: int,
    locked_by: str | None = None,
) -> bool:
    """
    Снимает lock после ручного/preview запуска без изменения last_success_to и next_run_at.
    Если locked_by передан, снимаем только свой lock, чтобы не трогать чужой worker.
    """
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        if locked_by:
            result = await connection.execute(
                """
                UPDATE digest_subscriptions
                SET locked_at = NULL,
                    locked_by = NULL,
                    updated_at = NOW()
                WHERE id = $1
                  AND locked_by = $2;
                """,
                int(subscription_id),
                str(locked_by),
            )
        else:
            result = await connection.execute(
                """
                UPDATE digest_subscriptions
                SET locked_at = NULL,
                    locked_by = NULL,
                    updated_at = NOW()
                WHERE id = $1;
                """,
                int(subscription_id),
            )
    return result == "UPDATE 1"


async def mark_digest_subscription_failed(
    subscription_id: int,
    error_text: str,
    backoff_minutes: int | None = None,
) -> bool:
    """
    Ошибка не двигает last_success_to, но переносит next_run_at вперёд,
    чтобы scheduler не запускал одну и ту же падающую подписку по кругу.
    """
    minutes = max(1, int(backoff_minutes or DIGEST_SUBSCRIPTION_FAILED_BACKOFF_MINUTES or 30))
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET next_run_at = NOW() + ($2::int * INTERVAL '1 minute'),
                locked_at = NULL,
                locked_by = NULL,
                last_error_text = $3,
                updated_at = NOW()
            WHERE id = $1;
            """,
            int(subscription_id),
            minutes,
            str(error_text)[:2000],
        )
    return result == "UPDATE 1"


async def recover_digest_scheduler_after_startup(
    worker_id: str = "scheduler-startup",
    release_locks: bool = True,
    stale_run_minutes: int = 60,
    lock_minutes: int = 60,
) -> dict:
    """
    Восстановление после перезапуска бота.

    Что делаем:
    - помечаем старые running-запуски как interrupted;
    - при необходимости снимаем lock-и прошлого процесса, чтобы due-подписки стартовали сразу после включения;
    - возвращаем короткую статистику для консоли/debug.

    Для локального polling-бота предполагается один активный процесс. Если когда-нибудь
    будет несколько worker-ов, release_locks лучше выключить в .env.
    """
    stale_minutes = max(1, int(stale_run_minutes or 60))
    lock_minutes = max(1, int(lock_minutes or 60))
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        interrupted_rows = await connection.fetch(
            """
            UPDATE digest_subscription_runs
            SET status = 'interrupted',
                finished_at = NOW(),
                error_text = COALESCE(error_text, 'Бот был остановлен или перезапущен во время автосводки.')
            WHERE status = 'running'
              AND finished_at IS NULL
              AND started_at < NOW() - ($1::int * INTERVAL '1 minute')
            RETURNING id;
            """,
            stale_minutes,
        )
        interrupted_count = len(interrupted_rows)

        released_locks = 0
        if release_locks:
            rows = await connection.fetch(
                """
                UPDATE digest_subscriptions
                SET locked_at = NULL,
                    locked_by = NULL,
                    updated_at = NOW()
                WHERE locked_at IS NOT NULL
                  AND is_active = TRUE
                  AND locked_at < NOW() - ($1::int * INTERVAL '1 minute')
                  AND (
                        locked_by IS NULL
                        OR locked_by LIKE 'digest-worker:%'
                        OR locked_by LIKE 'scheduler%'
                        OR locked_by LIKE 'debug%'
                  )
                RETURNING id;
                """,
                lock_minutes,
            )
            released_locks = len(rows)

        stats = await connection.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE is_active = TRUE)::int AS active_count,
                COUNT(*) FILTER (WHERE is_active = TRUE AND next_run_at <= NOW())::int AS due_count,
                COUNT(*) FILTER (WHERE is_active = TRUE AND locked_at IS NOT NULL)::int AS locked_count,
                MIN(next_run_at) FILTER (WHERE is_active = TRUE) AS nearest_next_run
            FROM digest_subscriptions;
            """
        )

    result = dict(stats) if stats else {}
    result["released_locks"] = released_locks
    result["interrupted_runs"] = int(interrupted_count or 0)
    result["worker_id"] = worker_id
    return result


async def refresh_digest_subscription_lock(subscription_id: int, locked_by: str) -> bool:
    """Продлевает lock во время долгой автосводки, чтобы второй worker не взял её повторно."""
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE digest_subscriptions
            SET locked_at = NOW(),
                updated_at = NOW()
            WHERE id = $1
              AND locked_by = $2
              AND is_active = TRUE;
            """,
            int(subscription_id),
            locked_by,
        )
    return result == "UPDATE 1"


async def claim_due_digest_subscriptions(limit: int = 2, locked_by: str = "scheduler", lock_minutes: int = 60) -> list[dict]:
    """Забирает просроченные подписки под lock, чтобы не запустить одну подписку дважды."""
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            WITH due AS (
                SELECT id
                FROM digest_subscriptions
                WHERE is_active = TRUE
                  AND next_run_at <= NOW()
                  AND (
                        locked_at IS NULL
                        OR locked_at < NOW() - ($2::int * INTERVAL '1 minute')
                  )
                ORDER BY next_run_at ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT $3
            )
            UPDATE digest_subscriptions s
            SET locked_at = NOW(),
                locked_by = $1,
                updated_at = NOW()
            FROM due
            WHERE s.id = due.id
            RETURNING s.*;
            """,
            locked_by,
            int(lock_minutes or 60),
            int(limit or 2),
        )
    return [dict(row) for row in rows]


async def get_digest_subscription_debug_stats(user_id: int) -> dict:
    """Короткая debug-информация по автосводкам пользователя."""
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE is_active = TRUE)::int AS active_count,
                COUNT(*) FILTER (WHERE is_active = TRUE AND next_run_at <= NOW())::int AS due_count,
                COUNT(*) FILTER (WHERE is_active = TRUE AND locked_at IS NOT NULL)::int AS locked_count,
                COUNT(*) FILTER (WHERE last_error_text IS NOT NULL)::int AS error_count,
                MIN(next_run_at) FILTER (WHERE is_active = TRUE) AS nearest_next_run
            FROM digest_subscriptions
            WHERE user_id = $1;
            """,
            int(user_id),
        )
        runs = await connection.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'running')::int AS running_runs,
                COUNT(*) FILTER (WHERE status IN ('failed', 'interrupted'))::int AS failed_runs,
                MAX(created_at) AS last_run_at
            FROM digest_subscription_runs
            WHERE user_id = $1;
            """,
            int(user_id),
        )
    result = dict(row) if row else {"active_count": 0, "due_count": 0, "nearest_next_run": None}
    if runs:
        result.update(dict(runs))
    return result


async def log_bot_error(
    place: str,
    error_text: str,
    error_type: str | None = None,
    traceback_text: str | None = None,
    user_id: int | None = None,
    telegram_id: int | None = None,
    context: dict | list | str | None = None,
) -> int | None:
    """Пишет ошибку бота в БД. Никогда не должен валить основной код."""
    try:
        db_pool = _get_pool()
    except Exception:
        return None

    if context is None:
        context_value = {}
    elif isinstance(context, (dict, list)):
        try:
            context_value = json.loads(json.dumps(context, ensure_ascii=False, default=str))
        except Exception:
            context_value = {"raw_context": str(context)}
    else:
        context_value = {"raw_context": str(context)}

    try:
        async with db_pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO bot_errors (
                    user_id,
                    telegram_id,
                    place,
                    error_type,
                    error_text,
                    traceback_text,
                    context_json
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                RETURNING id;
                """,
                int(user_id) if user_id is not None else None,
                int(telegram_id) if telegram_id is not None else None,
                place or "unknown",
                error_type,
                str(error_text or ""),
                traceback_text,
                json.dumps(context_value, ensure_ascii=False, default=str),
            )
        return int(row["id"]) if row else None
    except Exception as error:
        print(f"[bot_errors] failed to write error: {type(error).__name__}: {error}")
        return None


async def list_recent_bot_errors(limit: int = 10, include_resolved: bool = True) -> list[dict]:
    db_pool = _get_pool()
    where = "" if include_resolved else "WHERE is_resolved = FALSE"
    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            f"""
            SELECT
                e.id,
                e.user_id,
                e.telegram_id,
                u.username,
                u.first_name,
                e.place,
                e.error_type,
                e.error_text,
                e.traceback_text,
                e.context_json,
                e.is_resolved,
                e.created_at
            FROM bot_errors e
            LEFT JOIN users u ON u.id = e.user_id
            {where}
            ORDER BY e.created_at DESC
            LIMIT $1;
            """,
            int(limit or 10),
        )
    return [dict(row) for row in rows]


async def get_bot_error_stats() -> dict:
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                COUNT(*)::int AS total_errors,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours')::int AS errors_24h,
                COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '1 hour')::int AS errors_1h,
                COUNT(*) FILTER (WHERE is_resolved = FALSE)::int AS unresolved_errors,
                MAX(created_at) AS last_error_at
            FROM bot_errors;
            """
        )
        by_place = await connection.fetch(
            """
            SELECT place, COUNT(*)::int AS count
            FROM bot_errors
            WHERE created_at >= NOW() - INTERVAL '24 hours'
            GROUP BY place
            ORDER BY count DESC, place ASC
            LIMIT 8;
            """
        )
    result = dict(row) if row else {}
    result["by_place_24h"] = [dict(item) for item in by_place]
    return result


async def get_admin_system_stats() -> dict:
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                (SELECT COUNT(*)::int FROM users) AS users_count,
                (SELECT COUNT(*)::int FROM users WHERE account_type = 'admin') AS admin_users_count,
                (SELECT COUNT(*)::int FROM user_channels WHERE is_active = TRUE) AS active_user_channels,
                (SELECT COUNT(*)::int FROM digest_subscriptions) AS digest_subscriptions_total,
                (SELECT COUNT(*)::int FROM digest_subscriptions WHERE is_active = TRUE) AS digest_subscriptions_active,
                (SELECT COUNT(*)::int FROM digest_subscriptions WHERE is_active = TRUE AND next_run_at <= NOW()) AS digest_subscriptions_due,
                (SELECT COUNT(*)::int FROM digest_subscription_runs WHERE status = 'running') AS digest_runs_running,
                (SELECT COUNT(*)::int FROM digest_subscription_runs WHERE status IN ('failed', 'interrupted')) AS digest_runs_failed_or_interrupted,
                (SELECT COUNT(*)::int FROM messages) AS messages_count,
                (SELECT COUNT(*)::int FROM bot_errors WHERE created_at >= NOW() - INTERVAL '24 hours') AS errors_24h,
                (SELECT MAX(created_at) FROM bot_errors) AS last_error_at,
                NOW() AS db_now;
            """
        )
    return dict(row) if row else {}


async def get_admin_autodigest_stats() -> dict:
    db_pool = _get_pool()
    async with db_pool.acquire() as connection:
        row = await connection.fetchrow(
            """
            SELECT
                COUNT(*)::int AS total_count,
                COUNT(*) FILTER (WHERE is_active = TRUE)::int AS active_count,
                COUNT(*) FILTER (WHERE is_active = FALSE)::int AS inactive_count,
                COUNT(*) FILTER (WHERE is_active = TRUE AND next_run_at <= NOW())::int AS due_count,
                COUNT(*) FILTER (WHERE is_active = TRUE AND locked_at IS NOT NULL)::int AS locked_count,
                COUNT(*) FILTER (WHERE last_error_text IS NOT NULL)::int AS subscriptions_with_errors,
                MIN(next_run_at) FILTER (WHERE is_active = TRUE) AS nearest_next_run
            FROM digest_subscriptions;
            """
        )
        runs = await connection.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'running')::int AS running_runs,
                COUNT(*) FILTER (WHERE status = 'success')::int AS success_runs,
                COUNT(*) FILTER (WHERE status IN ('failed', 'interrupted'))::int AS failed_runs,
                MAX(created_at) AS last_run_at
            FROM digest_subscription_runs;
            """
        )
        recent_failed = await connection.fetch(
            """
            SELECT
                r.id,
                r.subscription_id,
                r.user_id,
                u.telegram_id,
                r.status,
                r.error_text,
                r.created_at
            FROM digest_subscription_runs r
            LEFT JOIN users u ON u.id = r.user_id
            WHERE r.status IN ('failed', 'interrupted')
            ORDER BY r.created_at DESC
            LIMIT 5;
            """
        )
    result = dict(row) if row else {}
    if runs:
        result.update(dict(runs))
    result["recent_failed_runs"] = [dict(item) for item in recent_failed]
    return result
