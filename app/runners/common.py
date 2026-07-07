from __future__ import annotations

from app.bot.runtime import bot, telegram_client
from app.db.database import close_db, init_db
from app.telegram.access import ensure_telegram_client_started


async def start_database() -> None:
    print("Подключаюсь к PostgreSQL...")
    await init_db()
    print("PostgreSQL подключен")


async def start_telethon_or_stop(role_label: str = "процесс") -> bool:
    print("Запускаю Telethon...")
    is_authorized = await ensure_telegram_client_started()

    if not is_authorized:
        print(f"Telethon не авторизован. {role_label} не запущен.")
        return False

    me = await telegram_client.get_me()

    if getattr(me, "bot", False):
        print("Ошибка: эта Telethon-сессия авторизована как бот.")
        print("Удалите sessions/user_session.session и войдите через номер телефона.")
        return False

    print(f"Telethon запущен как пользователь: {me.first_name} / id={me.id}")
    return True


async def shutdown_services(close_bot_session: bool = True) -> None:
    try:
        await close_db()
    finally:
        try:
            await telegram_client.disconnect()
        finally:
            if close_bot_session:
                try:
                    await bot.session.close()
                except Exception:
                    pass
