import asyncio
import os
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.types import Message, ReplyKeyboardRemove, TelegramObject
from telethon import TelegramClient

from config import (
    AI_CONCURRENT_REQUESTS,
    API_HASH,
    API_ID,
    BOT_TOKEN,
    SESSIONS_DIR,
    RUNTIME_TELETHON_SESSION_NAME,
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

os.makedirs(SESSIONS_DIR, exist_ok=True)

telegram_client = TelegramClient(
    RUNTIME_TELETHON_SESSION_NAME,
    API_ID,
    API_HASH,
)

                                                                      
telegram_client.flood_sleep_threshold = 0

                                                                        
telegram_api_lock = asyncio.Lock()

                                                                                           
                                                                              
ai_api_semaphore = asyncio.Semaphore(max(1, int(AI_CONCURRENT_REQUESTS or 1)))

                          
TELEGRAM_REQUEST_DELAY_SECONDS = 1.5
TELEGRAM_REQUEST_DELAY_JITTER_SECONDS = 2.5
TELEGRAM_HISTORY_WAIT_SECONDS = 2
TELEGRAM_PROGRESS_PAUSE_EVERY = 25
TELEGRAM_FLOOD_WAIT_SLEEP_CAP_SECONDS = 60
TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS = 3
AI_REPORT_TIMEOUT_SECONDS = 120

telegram_flood_wait_until = None

                                                
                                                                             
user_states: dict[int, str] = {}
user_parse_context: dict[int, dict] = {}

                                                    
busy_users: set[int] = set()


def set_user_busy(user_id: int) -> None:
    busy_users.add(user_id)


def clear_user_busy(user_id: int) -> None:
    busy_users.discard(user_id)


def is_user_busy(user_id: int | None) -> bool:
    return bool(user_id is not None and user_id in busy_users)


async def answer_progress(message: Message, text: str) -> None:
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


class BusyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
            text = (event.text or "").strip()

                                                                          
                                                                                   
            safe_busy_commands = {"/stop", "/status", "/reset", "/restart", "/start"}
            if is_user_busy(user_id) and text not in safe_busy_commands:
                await event.answer(
                    "⏳ Уже выполняю предыдущую команду. Дождись полного ответа — потом кнопки вернутся.\n\n"
                    "Если кажется, что бот завис, напиши /status или /reset.",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return None

        return await handler(event, data)


class ErrorLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as error:
            telegram_id = None
            text = None
            state = None

            if isinstance(event, Message) and event.from_user:
                telegram_id = event.from_user.id
                text = event.text
                state = user_states.get(telegram_id)
                clear_user_busy(telegram_id)

            from app.utils.error_logging import log_exception

            error_id = await log_exception(
                "message_handler",
                error,
                telegram_id=telegram_id,
                context={
                    "text": text,
                    "state": state,
                    "handler_data_keys": list(data.keys()),
                },
            )

            if isinstance(event, Message):
                try:
                    from app.bot.keyboards import main_keyboard

                    message_text = (
                        "⚠️ Команда не выполнена из-за ошибки. "
                        + "Я записал её в журнал"
                        + (f" #{error_id}" if error_id else "")
                        + ".\n\nМожно нажать /restart и попробовать ещё раз."
                    )
                    await event.answer(
                        message_text,
                        reply_markup=main_keyboard,
                    )
                except Exception:
                    pass

            return None


def setup_middlewares() -> None:
                                                    
    dp.message.middleware(ErrorLoggingMiddleware())
    dp.message.middleware(BusyMiddleware())
