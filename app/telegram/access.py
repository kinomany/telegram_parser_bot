import asyncio
import random
from datetime import datetime, timedelta, timezone

from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from config import PHONE_NUMBER
from app.bot import runtime

def get_flood_wait_seconds(error: FloodWaitError) -> int:
    """Telethon v1 даёт .seconds, в новых вариантах может быть .value."""
    value = getattr(error, "seconds", None)

    if value is None:
        value = getattr(error, "value", None)

    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def get_telegram_global_block_seconds() -> int:
    """Сколько ещё нельзя трогать Telethon после большого FloodWait."""
    if runtime.telegram_flood_wait_until is None:
        return 0

    now = datetime.now(timezone.utc)
    seconds_left = int((runtime.telegram_flood_wait_until - now).total_seconds())

    return max(0, seconds_left)


def get_telegram_global_block_message() -> str | None:
    seconds_left = get_telegram_global_block_seconds()

    if seconds_left <= 0:
        return None

    return (
        f"Telegram временно ограничил Telethon-сессию. "
        f"Осталось подождать примерно {seconds_left} сек. "
        "Попробуй позже."
    )


async def telegram_request_pause(reason: str) -> None:
    """Небольшая пауза перед Telegram-вызовом, чтобы не слать запросы залпом."""
    delay = runtime.TELEGRAM_REQUEST_DELAY_SECONDS + random.uniform(0, runtime.TELEGRAM_REQUEST_DELAY_JITTER_SECONDS)

    if delay <= 0:
        return

    print(f"Telegram-пауза ({reason}): {delay:.1f} сек.")
    await asyncio.sleep(delay)


async def handle_telegram_flood_wait(error: FloodWaitError, context: str) -> str:
    """
    Единая обработка FloodWait.
    Важно: FloodWait считаем ограничением всей Telethon-сессии, а не одного канала.
    """
    seconds = get_flood_wait_seconds(error)
    sleep_seconds = max(2, seconds) + runtime.TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS
    runtime.telegram_flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)

    message = (
        f"Telegram FloodWait при операции: {context}. "
        f"Нужно подождать {seconds} сек. "
        f"Telethon-запросы временно остановлены."
    )

    print(message)

    if sleep_seconds <= runtime.TELEGRAM_FLOOD_WAIT_SLEEP_CAP_SECONDS:
        print(f"Малый FloodWait. Сплю {sleep_seconds} сек.")
        await asyncio.sleep(sleep_seconds)
        runtime.telegram_flood_wait_until = None

    return message

async def ensure_telegram_client_started() -> bool:
    """
    Telethon должен быть авторизован как обычный пользователь, а не как бот.
    Именно пользовательская сессия имеет право читать историю каналов.
    """
    if not runtime.telegram_client.is_connected():
        await runtime.telegram_client.connect()

    if await runtime.telegram_client.is_user_authorized():
        return True

    print("Telethon-сессия не авторизована.")
    print("Сейчас нужно войти как обычный Telegram-пользователь.")

    if PHONE_NUMBER:
        await runtime.telegram_client.start(phone=PHONE_NUMBER)
    else:
        await runtime.telegram_client.start()

    return await runtime.telegram_client.is_user_authorized()


async def check_channel_access(channel_username: str) -> dict:
    """
    Проверяем канал сразу при добавлении.
    Закрытые и недоступные каналы не добавляем в список.
    Автоподписку не делаем.

    Важно: все Telegram-вызовы идут под runtime.telegram_api_lock,
    чтобы несколько пользователей не создавали пачку запросов одновременно.
    """
    try:
        async with runtime.telegram_api_lock:
            block_message = get_telegram_global_block_message()
            if block_message:
                return {
                    "ok": False,
                    "title": None,
                    "error": block_message,
                }

            is_authorized = await ensure_telegram_client_started()

            if not is_authorized:
                return {
                    "ok": False,
                    "title": None,
                    "error": (
                        "Telethon не авторизован как пользователь.\n\n"
                        "Нужно войти через номер телефона, а не через BOT_TOKEN."
                    ),
                }

            await telegram_request_pause(f"перед get_entity {channel_username}")
            entity = await runtime.telegram_client.get_entity(channel_username)

                                                                                              
                                                                                         
            await telegram_request_pause(f"перед проверкой истории {channel_username}")
            async for _ in runtime.telegram_client.iter_messages(
                entity,
                limit=1,
                wait_time=runtime.TELEGRAM_HISTORY_WAIT_SECONDS,
            ):
                break

        return {
            "ok": True,
            "title": getattr(entity, "title", None),
            "error": None,
        }

    except FloodWaitError as error:
        error_text = await handle_telegram_flood_wait(error, f"проверка канала {channel_username}")
        return {
            "ok": False,
            "title": None,
            "error": error_text,
        }

    except (UsernameInvalidError, UsernameNotOccupiedError):
        return {
            "ok": False,
            "title": None,
            "error": "Канал не найден. Проверь username или ссылку.",
        }

    except ChannelPrivateError:
        return {
            "ok": False,
            "title": None,
            "error": (
                "Канал закрытый или недоступен.\n\n"
                "Закрытые каналы бот сейчас не добавляет. "
                "Добавляй только открытые публичные каналы."
            ),
        }

    except ChannelInvalidError:
        return {
            "ok": False,
            "title": None,
            "error": "Канал не найден или недоступен. Добавляй только открытые публичные каналы.",
        }

    except Exception as error:
        error_text = str(error)

        if "bot users" in error_text or "GetHistoryRequest" in error_text:
            return {
                "ok": False,
                "title": None,
                "error": (
                    "Telethon сейчас авторизован как бот, а не как пользователь.\n\n"
                    "Удалите файл sessions/user_session.session и запустите проект заново. "
                    "При запуске войдите через номер телефона Telegram-аккаунта. "
                    "BOT_TOKEN должен использоваться только для aiogram."
                ),
            }

        return {
            "ok": False,
            "title": None,
            "error": f"Ошибка при проверке канала: {error}",
        }


async def get_latest_channel_message(channel_username: str) -> str:
    try:
        channel_check = await check_channel_access(channel_username)

        if not channel_check["ok"]:
            return channel_check["error"]

        async with runtime.telegram_api_lock:
            block_message = get_telegram_global_block_message()
            if block_message:
                return block_message

                                                      
                                                                             
            await telegram_request_pause(f"перед чтением последних сообщений {channel_username}")
            async for msg in runtime.telegram_client.iter_messages(
                channel_username,
                limit=10,
                wait_time=runtime.TELEGRAM_HISTORY_WAIT_SECONDS,
            ):
                if msg.text:
                    return msg.text

        return "Канал доступен, но в последних сообщениях не найден текст."

    except FloodWaitError as error:
        return await handle_telegram_flood_wait(error, f"чтение последнего сообщения {channel_username}")

    except Exception as error:
        return f"Ошибка при чтении канала: {error}"
