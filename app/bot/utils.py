from aiogram.types import Message

from app.bot import runtime
from app.db.database import get_or_create_user
from config import MAX_CHANNELS_PER_PARSE
from app.utils.parsing import normalize_channel_input as _normalize_channel_input
from app.utils.parsing import parse_channel_numbers as _parse_channel_numbers

async def register_user(message: Message) -> dict:
    user = message.from_user

    db_user = await get_or_create_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    if user.id not in runtime.user_states:
        runtime.user_states[user.id] = "menu"

    return db_user


def normalize_channel_input(text: str) -> str | None:
    return _normalize_channel_input(text)


def parse_channel_numbers(text: str, max_number: int) -> list[int] | None:
    """Парсит выбор каналов: 1,2,3 или 1 2 3. Возвращает индексы с нуля."""
    return _parse_channel_numbers(text, max_number, max_selected=MAX_CHANNELS_PER_PARSE)

async def send_long_text(message: Message, text: str, reply_markup=None) -> None:
    """Telegram не принимает слишком длинные сообщения, поэтому режем текст на части."""
    limit = 3500
    chunks = []
    current = ""

    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate

    if current:
        chunks.append(current)

    if not chunks:
        chunks = ["Нет текста для вывода."]

    for index, chunk in enumerate(chunks):
        markup = reply_markup if index == len(chunks) - 1 else None
        await message.answer(chunk, reply_markup=markup)
