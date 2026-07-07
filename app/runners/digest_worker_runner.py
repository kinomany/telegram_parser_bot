from __future__ import annotations

import asyncio

from app.bot.runtime import bot
from app.runners.common import shutdown_services, start_database, start_telethon_or_stop
from app.workers.digest_scheduler import digest_scheduler_loop
from config import DIGEST_WORKER_TELETHON_SESSION_NAME, TELETHON_SESSION_NAME


async def run_digest_worker() -> None:
    """
    Отдельный worker автосводок без polling-а Telegram-бота.

    Он:
    - подключается к БД;
    - подключает Telethon user-session;
    - раз в N секунд проверяет due-автосводки;
    - отправляет готовые дайджесты через Bot API.
    """
    await start_database()

    if str(DIGEST_WORKER_TELETHON_SESSION_NAME) == str(TELETHON_SESSION_NAME):
        print(
            "ВНИМАНИЕ: worker использует тот же Telethon session-file, что и bot-процесс. "
            "Для постоянной параллельной работы лучше задать в .env: "
            "DIGEST_WORKER_TELETHON_SESSION_NAME=sessions/digest_worker_session"
        )

    is_telethon_ready = await start_telethon_or_stop("Worker автосводок")
    if not is_telethon_ready:
        await shutdown_services(close_bot_session=True)
        return

    print("Worker автосводок запущен")
    print("Polling-бот в этом процессе НЕ запускается. Для бота нужен отдельный процесс: python main.py")

    try:
        await digest_scheduler_loop(bot)
    finally:
        await shutdown_services(close_bot_session=True)


def main() -> None:
    asyncio.run(run_digest_worker())
