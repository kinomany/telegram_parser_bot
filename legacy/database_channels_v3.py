import asyncpg
import json
from datetime import timezone


from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD, DB_POOL_MIN_SIZE, DB_POOL_MAX_SIZE


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
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
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
            RETURNING id, telegram_id, username, first_name, last_name;
            """,
            telegram_id,
            username,
            first_name,
            last_name,
        )

    return dict(user)


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


async def add_user_channel(user_id: int, username: str, title: str | None) -> dict:
    """
    Добавляет канал только в личный список пользователя.
    С таблицей channels не связывает.
    """
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        channel = await connection.fetchrow(
            """
            INSERT INTO user_channels (
                user_id,
                username,
                title,
                is_public,
                is_available,
                is_active,
                last_checked_at
            )
            VALUES ($1, $2, $3, TRUE, TRUE, TRUE, NOW())
            ON CONFLICT (user_id, username)
            DO UPDATE SET
                title = EXCLUDED.title,
                is_public = TRUE,
                is_available = TRUE,
                is_active = TRUE,
                last_checked_at = NOW(),
                updated_at = NOW()
            RETURNING id, user_id, username, title;
            """,
            user_id,
            username,
            title,
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


async def get_user_channels(user_id: int) -> list[dict]:
    """Возвращает активные личные каналы пользователя."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                id,
                username,
                title
            FROM user_channels
            WHERE user_id = $1
              AND is_active = TRUE
            ORDER BY created_at, id;
            """,
            user_id,
        )

    return [dict(row) for row in rows]


async def remove_user_channel(user_id: int, user_channel_id: int) -> bool:
    """Убирает канал из личного списка пользователя через is_active = FALSE."""
    db_pool = _get_pool()

    async with db_pool.acquire() as connection:
        result = await connection.execute(
            """
            UPDATE user_channels
            SET
                is_active = FALSE,
                updated_at = NOW()
            WHERE user_id = $1
              AND id = $2
              AND is_active = TRUE;
            """,
            user_id,
            user_channel_id,
        )

    return result == "UPDATE 1"


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
