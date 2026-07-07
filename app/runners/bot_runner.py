from __future__ import annotations

import asyncio

from app.bot import handlers                                                         
from app.bot.runtime import bot, dp, setup_middlewares
from app.runners.common import shutdown_services, start_database, start_telethon_or_stop


async def run_bot_polling() -> None:
    """
    Запуск только Telegram-бота без scheduler-а автосводок.

    Автосводки теперь запускаются отдельным процессом:
        python digest_worker.py
    """
    setup_middlewares()

    await start_database()
    is_telethon_ready = await start_telethon_or_stop("Бот")
    if not is_telethon_ready:
        await shutdown_services(close_bot_session=True)
        return

    print("Бот запущен")
    print("Автосводки в этом процессе НЕ запускаются. Для них нужен отдельный worker: python digest_worker.py")

    try:
        await dp.start_polling(bot)
    finally:
        await shutdown_services(close_bot_session=True)


def main() -> None:
    asyncio.run(run_bot_polling())
