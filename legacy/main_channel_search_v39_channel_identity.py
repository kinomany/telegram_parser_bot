import asyncio
import json
import os
import random
import re
from typing import Any, Awaitable, Callable
from datetime import date, datetime, time, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    TelegramObject,
)

from telethon import TelegramClient
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from config import (
    API_HASH,
    API_ID,
    BOT_TOKEN,
    PHONE_NUMBER,
    SESSIONS_DIR,
    TELETHON_SESSION_NAME,
    MAX_CHANNELS_PER_PARSE,
    MAX_CUSTOM_LOOKBACK_DAYS,
    MAX_CUSTOM_RANGE_DAYS,
    MAX_MESSAGES_PER_CHANNEL,
    MAX_TELEGRAM_CHECK_LIMIT_PER_CHANNEL,
    TOP_RELEVANT_MESSAGES_LIMIT,
    MIN_RELEVANT_MESSAGE_SCORE,
)

from database_channels_v3 import (
    init_db,
    close_db,
    get_or_create_telegram_channel,
    get_or_create_user,
    add_user_channel,
    get_user_channels,
    user_has_channel,
    verified_channel_exists,
    remove_user_channel,
    create_parse_job,
    finish_parse_job,
    save_message,
    get_cached_useful_messages,
    get_newest_cached_message_date,
    link_message_to_user_result,
    save_rejected_message,
    mark_channel_as_ad_heavy,
)

from query_channel_selector import (
    analyze_user_query,
    create_db_pool as create_channel_search_db_pool,
    load_marked_channels,
    rank_channels,
)

from ai_report_generator_v6_report_presets import (
    create_ai_report,
    format_ai_report_for_user,
)


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

os.makedirs(SESSIONS_DIR, exist_ok=True)

telegram_client = TelegramClient(
    TELETHON_SESSION_NAME,
    API_ID,
    API_HASH,
)

                                                                      
telegram_client.flood_sleep_threshold = 0

                                                                        
                                                                            
telegram_api_lock = asyncio.Lock()

                          
TELEGRAM_REQUEST_DELAY_SECONDS = 1.5
TELEGRAM_REQUEST_DELAY_JITTER_SECONDS = 2.5
TELEGRAM_HISTORY_WAIT_SECONDS = 2
TELEGRAM_PROGRESS_PAUSE_EVERY = 25
TELEGRAM_FLOOD_WAIT_SLEEP_CAP_SECONDS = 60
TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS = 3

                                                            
                                                                        
AI_REPORT_TIMEOUT_SECONDS = 120

telegram_flood_wait_until: datetime | None = None


                                                
                                                                             
user_states = {}
user_parse_context = {}

                                                    
                                                                             
                                                                          
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

                                                                                               
            if is_user_busy(user_id) and text != "/stop":
                await event.answer(
                    "⏳ Уже выполняю предыдущую команду. Дождись полного ответа — потом кнопки вернутся.",
                    reply_markup=ReplyKeyboardRemove(),
                )
                return None

        return await handler(event, data)


dp.message.middleware(BusyMiddleware())


SEARCH_DEBUG_ENV_NAME = "SEARCH_DEBUG"


def is_search_debug_enabled() -> bool:
    value = os.getenv(SEARCH_DEBUG_ENV_NAME, "").strip().lower()
    return value in {"1", "true", "yes", "y", "on", "debug"}


def debug_print_block(title: str, data=None) -> None:
    if not is_search_debug_enabled():
        return

    print("\n" + "=" * 90)
    print(f"SEARCH DEBUG | {title}")
    print("=" * 90)

    if data is None:
        return

    if isinstance(data, str):
        print(data)
        return

    try:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    except TypeError:
        print(data)


BOT_INTRO_TEXT = (
    "Привет. Это Telegram-парсер, а не обычный поисковик.\n\n"
    "Как он работает:\n"
    "1. Подбирает каналы из уже размеченной базы.\n"
    "2. Читает сообщения из выбранных каналов за период.\n"
    "3. Отбирает самые похожие сообщения.\n"
    "4. Делает ИИ-отчёт только по найденным сообщениям.\n\n"
    "Лучше писать не вопрос, а задание для подбора источников и постов."
)

QUERY_GUIDE_TEXT = (
    "Напиши тему для подбора каналов и сбора сообщений.\n\n"
    "Важно: это не Google и не Яндекс. Бот не ищет по всему интернету. "
    "Он выбирает подходящие Telegram-каналы из размеченной базы, потом парсит их сообщения за выбранный период.\n\n"
    "Чем больше полезных уточнений, тем точнее подбор. Но не надо писать набор слов через запятую. "
    "Лучше сформулировать цельно: главная тема → связанные подтемы → уточняющие ключи или исключения.\n\n"
    "Примеры нормальных запросов:\n"
    "• Рынок труда в IT: вакансии, зарплаты и релокация; без курсов программирования.\n"
    "• Игровые новинки из мира видеоигр: релизы на ПК, демоверсии в Steam и крупные игровые анонсы; не авторские каналы.\n"
    "• Оппозиционное СМИ о недавних событиях в России: задержания, суды, протесты и реакция властей; без личных блогов.\n"
    "• Новости нейросетей для работы: новые модели, сервисы для текста и изображений, обновления OpenAI и Google; без вакансий и рекламы курсов.\n"
    "• Экономика России: ключевая ставка, банки, курс рубля и санкции; без криптовалют и трейдинга.\n"
    "• События в Тбилиси: аварии, полиция, транспорт и решения мэрии; без туристических подборок.\n"
    "• Здоровье и клещи: укусы, симптомы, профилактика и обращения к врачам; без рекламы клиник и БАДов.\n"
    "• Новые правила для автомобилистов: штрафы, ОСАГО, права и изменения ПДД; без обзоров машин.\n\n"
    "Слишком широко:\n"
    "• новости\n"
    "• игры\n"
    "• политика\n"
    "• нейросети\n\n"
    "Главная мысль: чем точнее ты описал тему и лишнее, тем меньше случайных каналов и постов попадёт в отчёт."
)

SHORT_QUERY_HINT_TEXT = (
    "Запрос слишком короткий. Боту нужны уточнения: главная тема, подтемы и что исключить.\n\n"
    "Например: “Рынок труда в IT: вакансии, зарплаты и релокация; без курсов программирования.”"
)


CHANNEL_PICK_EXPLANATION = (
    "Я не делаю веб-поиск. Сейчас подбираю каналы из твоей размеченной базы, "
    "поэтому результат зависит от того, какие каналы уже добавлены и размечены."
)

REPORT_PRESET_BY_BUTTON = {
    "📰 Краткая сводка": "brief",
    "🧩 Общий сюжет": "story",
    "⚖️ Сравнение источников": "compare_sources",
    "✅ Практическая польза": "practical",
    "🚨 Срочное / происшествие": "urgent",
    "🧾 Только факты": "facts_only",
}

REPORT_PRESET_TITLE_BY_ID = {
    "brief": "Краткая сводка",
    "story": "Общий сюжет",
    "compare_sources": "Сравнение источников",
    "practical": "Практическая польза",
    "urgent": "Срочное / происшествие",
    "facts_only": "Только факты",
}

REPORT_PRESET_HELP_TEXT = (
    "Выбери, каким должен быть ИИ-отчёт. Это не меняет сбор Telegram — "
    "меняется только инструкция для ИИ, как обработать найденные сообщения.\n\n"
    "📰 Краткая сводка — что произошло и главные события, без глубокой аналитики.\n"
    "🧩 Общий сюжет — попытка собрать события в общую картину, если связь есть.\n"
    "⚖️ Сравнение источников — кто что пишет, где сходятся и расходятся.\n"
    "✅ Практическая польза — что можно вынести для действий, риски и ограничения.\n"
    "🚨 Срочное / происшествие — что известно, что подтверждено, что пока неясно.\n"
    "🧾 Только факты — список фактов без анализа и интерпретаций."
)


def get_report_preset_from_text(text: str | None) -> str | None:
    return REPORT_PRESET_BY_BUTTON.get((text or "").strip())


def get_report_preset_title(report_preset: str | None) -> str:
    return REPORT_PRESET_TITLE_BY_ID.get(report_preset or "brief", "Краткая сводка")



def get_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def get_env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def get_env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


AD_HEAVY_MIN_AD_MESSAGES = get_env_int("AD_HEAVY_MIN_AD_MESSAGES", 3)
AD_HEAVY_RATIO = get_env_float("AD_HEAVY_RATIO", 0.35)
AUTO_DISABLE_AD_HEAVY_CHANNELS = get_env_bool("AUTO_DISABLE_AD_HEAVY_CHANNELS", False)


def compact_debug_message(item: dict, max_text_chars: int = 500) -> dict:
    text = item.get("cleaned_text") or item.get("message_text") or ""
    text = re.sub(r"\s{2,}", " ", str(text)).strip()

    return {
        "username": item.get("username"),
        "title": item.get("title"),
        "date_text": item.get("date_text"),
        "score": item.get("score"),
        "matched": item.get("matched"),
        "text_preview": text[:max_text_chars],
    }


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
    if telegram_flood_wait_until is None:
        return 0

    now = datetime.now(timezone.utc)
    seconds_left = int((telegram_flood_wait_until - now).total_seconds())

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
    delay = TELEGRAM_REQUEST_DELAY_SECONDS + random.uniform(0, TELEGRAM_REQUEST_DELAY_JITTER_SECONDS)

    if delay <= 0:
        return

    print(f"Telegram-пауза ({reason}): {delay:.1f} сек.")
    await asyncio.sleep(delay)


async def handle_telegram_flood_wait(error: FloodWaitError, context: str) -> str:
    """
    Единая обработка FloodWait.
    Важно: FloodWait считаем ограничением всей Telethon-сессии, а не одного канала.
    """
    global telegram_flood_wait_until

    seconds = get_flood_wait_seconds(error)
    sleep_seconds = max(2, seconds) + TELEGRAM_FLOOD_WAIT_EXTRA_SECONDS
    telegram_flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=sleep_seconds)

    message = (
        f"Telegram FloodWait при операции: {context}. "
        f"Нужно подождать {seconds} сек. "
        f"Telethon-запросы временно остановлены."
    )

    print(message)

    if sleep_seconds <= TELEGRAM_FLOOD_WAIT_SLEEP_CAP_SECONDS:
        print(f"Малый FloodWait. Сплю {sleep_seconds} сек.")
        await asyncio.sleep(sleep_seconds)
        telegram_flood_wait_until = None

    return message


main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎯 Подобрать каналы по теме")],
        [KeyboardButton(text="📥 Собрать из моих каналов")],
        [
            KeyboardButton(text="📋 Мои каналы"),
            KeyboardButton(text="ℹ️ Помощь"),
        ],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие",
)


channels_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="📋 Показать"),
            KeyboardButton(text="➕ Добавить"),
        ],
        [
            KeyboardButton(text="🗑 Удалить"),
        ],
        [
            KeyboardButton(text="⬅️ Назад"),
        ],
    ],
    resize_keyboard=True,
    input_field_placeholder="Управление каналами",
)


parse_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📥 Собрать выбранные каналы")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Сбор из своих каналов",
)


search_results_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📥 Читать посты из подобранных")],
        [KeyboardButton(text="🔁 Другой запрос")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Собрать сообщения или уточнить запрос",
)


report_preset_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📰 Краткая сводка")],
        [KeyboardButton(text="🧩 Общий сюжет")],
        [KeyboardButton(text="⚖️ Сравнение источников")],
        [KeyboardButton(text="✅ Практическая польза")],
        [KeyboardButton(text="🚨 Срочное / происшествие")],
        [KeyboardButton(text="🧾 Только факты")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите тип ИИ-отчёта",
)


period_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 За день")],
        [KeyboardButton(text="📅 За неделю")],
        [KeyboardButton(text="📅 За месяц")],
        [KeyboardButton(text="📆 Свой период")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите период",
)


settings_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📖 Команды")],
        [KeyboardButton(text="🎯 Как писать запрос")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Помощь",
)


async def register_user(message: Message) -> dict:
    user = message.from_user

    db_user = await get_or_create_user(
        telegram_id=user.id,
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
    )

    if user.id not in user_states:
        user_states[user.id] = "menu"

    return db_user


def normalize_channel_input(text: str) -> str | None:
    text = text.strip()

    if text.startswith("https://t.me/"):
        username = text.replace("https://t.me/", "", 1)
    elif text.startswith("http://t.me/"):
        username = text.replace("http://t.me/", "", 1)
    elif text.startswith("t.me/"):
        username = text.replace("t.me/", "", 1)
    elif text.startswith("@"):
        username = text[1:]
    else:
        return None

    username = username.strip().split("/")[0]
    username = username.split("?")[0]

    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return None

    return f"@{username}"


async def ensure_telegram_client_started() -> bool:
    """
    Telethon должен быть авторизован как обычный пользователь, а не как бот.
    Именно пользовательская сессия имеет право читать историю каналов.
    """
    if not telegram_client.is_connected():
        await telegram_client.connect()

    if await telegram_client.is_user_authorized():
        return True

    print("Telethon-сессия не авторизована.")
    print("Сейчас нужно войти как обычный Telegram-пользователь.")

    if PHONE_NUMBER:
        await telegram_client.start(phone=PHONE_NUMBER)
    else:
        await telegram_client.start()

    return await telegram_client.is_user_authorized()


async def check_channel_access(channel_username: str) -> dict:
    """
    Проверяем канал сразу при добавлении.
    Закрытые и недоступные каналы не добавляем в список.
    Автоподписку не делаем.

    Важно: все Telegram-вызовы идут под telegram_api_lock,
    чтобы несколько пользователей не создавали пачку запросов одновременно.
    """
    try:
        async with telegram_api_lock:
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
            entity = await telegram_client.get_entity(channel_username)

                                                                                              
                                                                                         
            await telegram_request_pause(f"перед проверкой истории {channel_username}")
            async for _ in telegram_client.iter_messages(
                entity,
                limit=1,
                wait_time=TELEGRAM_HISTORY_WAIT_SECONDS,
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

        async with telegram_api_lock:
            block_message = get_telegram_global_block_message()
            if block_message:
                return block_message

                                                      
                                                                             
            await telegram_request_pause(f"перед чтением последних сообщений {channel_username}")
            async for msg in telegram_client.iter_messages(
                channel_username,
                limit=10,
                wait_time=TELEGRAM_HISTORY_WAIT_SECONDS,
            ):
                if msg.text:
                    return msg.text

        return "Канал доступен, но в последних сообщениях не найден текст."

    except FloodWaitError as error:
        return await handle_telegram_flood_wait(error, f"чтение последнего сообщения {channel_username}")

    except Exception as error:
        return f"Ошибка при чтении канала: {error}"



def remove_links_completely(text: str) -> str:
    """Удаляет ссылки, включая YouTube/youtu.be, даже если ссылка без https://."""
    if not text:
        return ""

                                               
    text = re.sub(
        r"\[([^\]]+)\]\((?:https?://|www\.|t\.me/|youtu\.be/|(?:m\.)?youtube\.com/|youtube-nocookie\.com/)[^\s)]+[^)]*\)",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )

                                                                    
    url_pattern = re.compile(
        r"""(?ix)
        (?:https?://|www\.)[^\s<>()"'«»]+
        |\b(?:youtu\.be|(?:m\.)?youtube\.com|youtube-nocookie\.com)/[^\s<>()"'«»]+
        |\bt\.me/[^\s<>()"'«»]+
        |\bbit\.ly/[^\s<>()"'«»]+
        |\bgoo\.gl/[^\s<>()"'«»]+
        |\btinyurl\.com/[^\s<>()"'«»]+
        """
    )

    return url_pattern.sub(" ", text)


AD_MESSAGE_RE = re.compile(
    r"(?iu)"
    r"("
    r"#\s*реклама\b|"
    r"о\s+рекламодателе\b|"
    r"на\s+правах\s+рекламы|"
    r"рекламн(?:ый|ая|ое|ые)\s+материал|"
    r"партн[её]рск(?:ий|ая|ое|ие)\s+материал|"
    r"спонсорск(?:ий|ая|ое|ие)\s+материал|"
    r"узнать\s+больше\s+#?\s*реклама|"
    r"финансовые\s+услуги\s+оказывает|"
    r"пассивн(?:ый|ого)\s+доход|"
    r"\b\d{1,2}[,.]?\d?\s*%\s+годовых|"
    r"инвестируйте|"
    r"рентн(?:ые|ых)\s+выплат|"
    r"купи\s+.*\s+чек|"
    r"загрузи\s+чек|"
    r"выиграй\s+мечт|"
    r"promo\.[a-z0-9.-]+|"
    r"clients\.site|"
    r"alfacapital|"
    r"morpheusbed|"
    r"chernogolovka-promo"
    r")"
)

CROSS_PROMO_RE = re.compile(
    r"(?iu)"
    r"("
    r"рекомендуем\s+подписаться|"
    r"подписаться,\s*чтобы\s+не\s+пропустить|"
    r"подпишитесь,\s*чтобы\s+не\s+пропустить|"
    r"канал(?:у)?\s+за\s+вдохновение|"
    r"необычные\s+лайфхаки.*полезные\s+советы|"
    r"мы\s+в\s+мах\b"
    r")"
)


def clean_message_text(text: str | None) -> str:
    """Грубая очистка текста перед сохранением: удаляем ссылки, markdown-разметку и мусор."""
    if not text:
        return ""

    text = remove_links_completely(text)

                         
    text = re.sub(r"@\w+", " ", text)

                         
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("`", "")

                         
    text = text.replace("\xa0", " ")

                               
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()

def get_filter_reason(cleaned_text: str, original_text: str | None) -> str | None:
    """
    Фильтр перед сохранением/score.

    Рекламу режем до попадания в structured_messages, иначе она забивает топ-10
    словами вроде "советы", "доход", "узнать больше", "лайфхаки".
    """
    if not original_text or not original_text.strip():
        return "empty_text"

    if AD_MESSAGE_RE.search(original_text):
        return "ad_marker"

    if CROSS_PROMO_RE.search(original_text):
        return "cross_promo"

    if not cleaned_text:
        return "only_links_or_mentions"

    return None


def format_filter_stats(filter_stats: dict) -> str:
    """Готовит текстовую статистику по причинам отбраковки сообщений."""
    if not filter_stats:
        return ""

    reason_titles = {
        "empty_text": "пустой текст",
        "only_links_or_mentions": "только ссылки/упоминания",
        "too_short": "слишком короткие",
        "ad_marker": "явная реклама",
        "cross_promo": "перекрёстная реклама / промо канала",
        "too_many_links": "много ссылок",
        "trash_words": "рекламные слова",
    }

    filter_text = "\n\nОтфильтровано:\n"

    for reason, count in sorted(filter_stats.items()):
        title = reason_titles.get(reason, reason)
        filter_text += f"{title}: {count}\n"

    return filter_text.rstrip()

def parse_channel_numbers(text: str, max_number: int) -> list[int] | None:
    """Парсит выбор каналов: 1,2,3 или 1 2 3. Возвращает индексы с нуля."""
    parts = re.split(r"[\s,;]+", text.strip())
    parts = [part for part in parts if part]

    if not parts or len(parts) > MAX_CHANNELS_PER_PARSE:
        return None

    indexes = []
    for part in parts:
        if not part.isdigit():
            return None

        number = int(part)
        if number < 1 or number > max_number:
            return None

        index = number - 1
        if index not in indexes:
            indexes.append(index)

    return indexes



def get_period_range(period_text: str) -> tuple[datetime, datetime] | None:
    now = datetime.now(timezone.utc)

    if period_text == "📅 За день":
        return now - timedelta(days=1), now
    if period_text == "📅 За неделю":
        return now - timedelta(days=7), now
    if period_text == "📅 За месяц":
        return now - timedelta(days=30), now

    return None


def parse_date_value(value: str) -> date | None:
    value = value.strip()

    for date_format in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue

    return None


def parse_custom_period(text: str) -> dict:
    """
    Принимает период в формате:
    01.05.2026 20.05.2026
    01.05.2026-20.05.2026
    2026-05-01 2026-05-20
    """
    parts = re.findall(r"\d{1,2}\.\d{1,2}\.\d{2,4}|\d{4}-\d{1,2}-\d{1,2}", text)

    if len(parts) != 2:
        return {
            "ok": False,
            "error": (
                "Нужно ввести две даты. Например:\n"
                "01.05.2026 20.05.2026\n"
                "или 2026-05-01 2026-05-20"
            ),
        }

    start_date = parse_date_value(parts[0])
    end_date = parse_date_value(parts[1])

    if start_date is None or end_date is None:
        return {
            "ok": False,
            "error": "Не смог распознать даты. Используй формат ДД.ММ.ГГГГ, например 01.05.2026 20.05.2026.",
        }

    if start_date > end_date:
        return {
            "ok": False,
            "error": "Начальная дата не может быть позже конечной.",
        }

    today = datetime.now(timezone.utc).date()
    oldest_allowed = today - timedelta(days=MAX_CUSTOM_LOOKBACK_DAYS)

    if start_date < oldest_allowed:
        return {
            "ok": False,
            "error": f"Нельзя выбирать период старше {MAX_CUSTOM_LOOKBACK_DAYS} дней. Самая ранняя дата: {oldest_allowed.strftime('%d.%m.%Y')}.",
        }

    if end_date > today:
        return {
            "ok": False,
            "error": "Конечная дата не может быть в будущем.",
        }

    days_count = (end_date - start_date).days + 1
    if days_count > MAX_CUSTOM_RANGE_DAYS:
        return {
            "ok": False,
            "error": f"Свой период не может быть больше {MAX_CUSTOM_RANGE_DAYS} дня. Сейчас выбрано: {days_count} дней.",
        }

    date_from = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    date_to = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)

    return {
        "ok": True,
        "date_from": date_from,
        "date_to": date_to,
        "label": f"с {start_date.strftime('%d.%m.%Y')} по {end_date.strftime('%d.%m.%Y')}",
    }


def ensure_aware_utc(value: datetime | None) -> datetime | None:
    """Telethon обычно даёт UTC-aware datetime, но на всякий случай приводим к UTC-aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



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




def normalize_found_channel_username(username: str | None) -> str:
    username = (username or "").strip().lstrip("@")
    return f"@{username}" if username else ""


def convert_found_channels_for_collect(found_channels: list[dict], limit: int = 5) -> list[dict]:
    """Приводит найденные каналы из общей базы к формату collect_messages_from_channels()."""
    selected_channels = []

    for item in found_channels[:limit]:
        username = normalize_found_channel_username(item.get("username"))

        if not username:
            continue

        selected_channels.append(
            {
                "id": item.get("channel_id"),
                "username": username,
                "title": item.get("title") or username,
            }
        )

    return selected_channels


def format_query_markup_short(query_markup: dict) -> str:
    parts = []

    category = query_markup.get("category")
    if category and category != "неясно":
        parts.append(f"категория: {category}")

    region = query_markup.get("region") or query_markup.get("country") or query_markup.get("city")
    if region:
        parts.append(f"регион: {region}")

    position = query_markup.get("position")
    if position:
        parts.append(f"позиция: {position}")

    topics = query_markup.get("topics") or []
    if topics:
        parts.append("темы: " + ", ".join(str(topic) for topic in topics[:4]))

    keywords = query_markup.get("keywords") or []
    if keywords:
        parts.append("ключи: " + ", ".join(keywords[:5]))

    return "; ".join(parts) if parts else "не уверен, ищу по ключевым словам"



POSITION_UNKNOWN_VALUES = {
    "",
    "неясно",
    "не указано",
    "не указана",
    "unknown",
    "none",
    "null",
    "смешанная",
    "смешанное",
    "нейтральная",
    "нейтрально",
}


def normalize_position_value(value: str | None) -> str:
    value = normalize_match_text(value)
    value = re.sub(r"\s{2,}", " ", value).strip(" .,:;!?-—\"'«»")

    if not value or value in POSITION_UNKNOWN_VALUES:
        return ""

    if any(marker in value for marker in ("оппози", "антивоен", "анти-воен", "критич")):
        return "оппозиционная"

    if any(marker in value for marker in ("независим", "independent")):
        return "независимая"

    if any(marker in value for marker in ("пророссий", "провласт", "государствен", "официальн", "кремл", "z-пози", "z пози")):
        return "пророссийская"

    if any(marker in value for marker in ("проукраин", "украинск")):
        return "проукраинская"

    return value


def get_requested_position(query_markup: dict, user_query: str | None = None) -> str:
    """
    Достаём позицию из ИИ-разбора и подстраховываемся исходным запросом.
    Для таких запросов позиция — не мягкий keyword, а обязательное условие.
    """
    candidates = [
        query_markup.get("position"),
        query_markup.get("position_label"),
        query_markup.get("позиция"),
        query_markup.get("позиция подачи"),
    ]

    for candidate in candidates:
        normalized = normalize_position_value(str(candidate) if candidate is not None else None)
        if normalized:
            return normalized

    query_text = normalize_match_text(user_query)

    if any(word in query_text for word in ("оппозицион", "оппозиция", "антивоен", "против войны", "против сво")):
        return "оппозиционная"

    if any(word in query_text for word in ("пророссий", "провласт", "официальн", "кремл", "госканал", "гос канал")):
        return "пророссийская"

    if any(word in query_text for word in ("проукраин", "украинск")):
        return "проукраинская"

    return ""


def deep_get_position_from_classification(classification: dict | None) -> str:
    if not isinstance(classification, dict):
        return ""

    fields = (
        "позиция",
        "позиция подачи",
        "position",
        "position_label",
        "political_position",
        "tone_position",
    )

    for field in fields:
        if field in classification:
            normalized = normalize_position_value(str(classification.get(field)))
            if normalized:
                return normalized

    return ""


def extract_channel_position(item: dict) -> str:
    """
    Берём позицию из результата rank_channels().
    Поддерживаем разные названия полей, чтобы не зависеть от одной версии query_channel_selector.py.
    """
    direct_fields = (
        "position",
        "position_label",
        "позиция",
        "позиция подачи",
    )

    for field in direct_fields:
        normalized = normalize_position_value(str(item.get(field)) if item.get(field) is not None else None)
        if normalized:
            return normalized

    for nested_field in ("classification", "ai_classification", "markup", "ai_markup"):
        position = deep_get_position_from_classification(item.get(nested_field))
        if position:
            return position

                                                                   
    description = normalize_match_text(item.get("description") or item.get("ai_description") or "")
    title = normalize_match_text(item.get("title") or "")
    text = f"{title}\n{description}"

    if any(word in text for word in ("оппозицион", "антивоен", "анти-воен", "критик власти", "критика власти")):
        return "оппозиционная"

    if any(word in text for word in ("пророссий", "провласт", "государствен", "кремл", "с пророссийской позиции")):
        return "пророссийская"

    if any(word in text for word in ("проукраин", "украинск")):
        return "проукраинская"

    return ""


def position_matches(requested_position: str, channel_position: str) -> bool:
    requested_position = normalize_position_value(requested_position)
    channel_position = normalize_position_value(channel_position)

    if not requested_position:
        return True

    if not channel_position:
        return False

    if requested_position == channel_position:
        return True

                                                                                  
                                                    
    if requested_position == "оппозиционная":
        return channel_position in {"оппозиционная", "независимая"}

    return False



MEDIA_REQUEST_WORDS = (
    "сми",
    "медиа",
    "издание",
    "издания",
    "редакция",
    "редакционное",
    "редакционный",
    "новостное издание",
    "новостной сайт",
    "газета",
    "журнал",
    "телеканал",
)

MEDIA_POSITIVE_WORDS = (
    "сми",
    "медиа",
    "издание",
    "редакция",
    "редакционный",
    "новостное издание",
    "новостной сайт",
    "газета",
    "журнал",
    "телеканал",
    "радио",
    "информационное агентство",
    "агентство",
)

MEDIA_NEGATIVE_WORDS = (
    "авторский канал",
    "личный канал",
    "блог",
    "блогер",
    "политик",
    "депутат",
    "военкор",
    "военный корреспондент",
    "канал ильи",
    "канал ольги",
    "канал александра",
    "канал автора",
)


def get_requested_media_filter(query_markup: dict, user_query: str | None = None) -> bool:
    """
    Если пользователь написал "СМИ", "медиа", "издание" и т.п.,
    это обязательный фильтр по типу канала.
    """
    query_text = normalize_match_text(user_query)

    if any(word in query_text for word in MEDIA_REQUEST_WORDS):
        return True

    fields = [
        query_markup.get("format"),
        query_markup.get("content_format"),
        query_markup.get("channel_type"),
        query_markup.get("тип канала"),
        query_markup.get("тип охвата"),
        query_markup.get("формат"),
    ]

    for value in fields:
        value_text = normalize_match_text(str(value) if value is not None else "")
        if any(word in value_text for word in MEDIA_REQUEST_WORDS):
            return True

    return False



def raw_query_has_word(user_query: str | None, words: tuple[str, ...]) -> bool:
    query_text = normalize_match_text(user_query)
    return any(word in query_text for word in words)


def normalize_query_markup_for_channel_search(
    query_markup: dict,
    user_query: str | None = None,
) -> dict:
    """
    Страховка от плохого разбора дешёвой ИИ.

    Пример ошибки:
    "СМИ оппозиция о войне" -> ИИ вернула category="политика".
    Для выбора каналов это плохо: СМИ размечены как "новости и СМИ",
    а не как "политика", поэтому они не попадают в первые raw_results.

    Поэтому явные слова из сырого запроса имеют право поправить query_markup.
    """
    normalized = dict(query_markup or {})
    media_required = get_requested_media_filter(normalized, user_query=user_query)

    if media_required:
        old_category = normalized.get("category")
        normalized["category"] = "новости и СМИ"

        topics = list(normalized.get("topics") or [])
        keywords = list(normalized.get("keywords") or [])
        objects = list(normalized.get("objects") or [])

        if old_category and old_category not in ("неясно", "новости и СМИ"):
            topics.append(str(old_category))

        if raw_query_has_word(user_query, ("войн", "сво", "фронт", "украин")):
            topics.append("война и конфликты")
            keywords.append("война")

        if raw_query_has_word(user_query, ("полит", "оппози", "власть", "репресс")):
            topics.append("политика")

        keywords.extend(["СМИ", "медиа", "издание"])

        def unique_strings(values: list) -> list[str]:
            result = []
            seen = set()

            for value in values:
                if not isinstance(value, str):
                    continue

                value = value.strip()
                key = normalize_match_text(value)

                if not value or key in seen:
                    continue

                seen.add(key)
                result.append(value)

            return result

        normalized["topics"] = unique_strings(topics)
        normalized["keywords"] = unique_strings(keywords)
        normalized["objects"] = unique_strings(objects)

    requested_position = get_requested_position(normalized, user_query=user_query)
    if requested_position:
        normalized["position"] = requested_position

    return normalized


def deep_get_media_text_from_classification(classification: dict | None) -> str:
    if not isinstance(classification, dict):
        return ""

    fields = (
        "категория",
        "тип СМИ",
        "новостная тема",
        "формат",
        "тип канала",
        "тип охвата",
        "channel_type",
        "content_format",
        "format",
        "описание",
    )

    parts = []

    for field in fields:
        value = classification.get(field)

        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value is not None:
            parts.append(str(value))

    return normalize_match_text(" ".join(parts))


def extract_channel_media_status(item: dict) -> tuple[bool, str]:
    """
    Возвращает:
    - True/False: похоже ли на СМИ
    - человекочитаемую причину/тип
    """
    direct_fields = (
        "category",
        "категория",
        "media_type",
        "тип СМИ",
        "news_topics",
        "новостная тема",
        "format",
        "content_format",
        "channel_type",
        "type",
        "тип канала",
        "тип охвата",
        "формат",
    )

    parts = []

    for field in direct_fields:
        value = item.get(field)
        if value is not None:
            parts.append(str(value))

    for nested_field in ("classification", "ai_classification", "markup", "ai_markup"):
        nested_text = deep_get_media_text_from_classification(item.get(nested_field))
        if nested_text:
            parts.append(nested_text)

    parts.append(str(item.get("title") or ""))
    parts.append(str(item.get("description") or item.get("ai_description") or ""))

    text = normalize_match_text(" ".join(parts))

    if any(word in text for word in MEDIA_NEGATIVE_WORDS):
                                                                                             
                                                                             
        return False, "не СМИ / авторский канал"

    if any(word in text for word in MEDIA_POSITIVE_WORDS):
        return True, "СМИ/медиа"

    return False, "тип не указан"


def apply_position_filter_to_channel_results(
    query_markup: dict,
    results: list[dict],
    user_query: str | None = None,
) -> list[dict]:
    requested_position = get_requested_position(query_markup, user_query=user_query)
    media_required = get_requested_media_filter(query_markup, user_query=user_query)

    prepared_results = []
    excluded_results = []

    for item in results:
        channel_position = extract_channel_position(item)
        is_media, media_label = extract_channel_media_status(item)

        item = dict(item)
        item["display_position"] = channel_position or "не указана"
        item["display_media_type"] = media_label

        exclude_reasons = []

        if requested_position and not position_matches(requested_position, channel_position):
            exclude_reasons.append(f"позиция={item['display_position']}")

        if media_required and not is_media:
            exclude_reasons.append(f"тип={media_label}")

        if exclude_reasons:
            item["exclude_reason"] = "; ".join(exclude_reasons)
            excluded_results.append(item)
            continue

        matched = list(item.get("matched") or [])

                                                                        
                                                                                  
                                                                     
        if requested_position:
            matched.insert(0, f"фильтр позиции: {channel_position}")

        if media_required:
            matched.insert(0, "фильтр типа: СМИ/медиа")

        item["matched"] = matched
        prepared_results.append(item)

    if requested_position or media_required:
        filters = []
        if requested_position:
            filters.append(f"позиция='{requested_position}'")
        if media_required:
            filters.append("тип='СМИ/медиа'")

        print(
            f"Фильтры каналов: {', '.join(filters)}. "
            f"Оставлено: {len(prepared_results)}. Отсеяно: {len(excluded_results)}."
        )

        for item in excluded_results[:30]:
            print(
                f"Отсеян: @{str(item.get('username') or '').lstrip('@')} "
                f"позиция={item.get('display_position')} "
                f"тип={item.get('display_media_type')} "
                f"score={item.get('score')} "
                f"причина={item.get('exclude_reason')}"
            )

    prepared_results.sort(key=lambda item: item.get("score", 0), reverse=True)
    return prepared_results



def format_channel_search_results(user_query: str, query_markup: dict, results: list[dict]) -> str:
    text = "🎯 Подбор каналов для парсинга\n\n"
    text += f"Запрос: {user_query}\n"
    text += f"ИИ понял: {format_query_markup_short(query_markup)}\n"
    text += "\nЭто не веб-поиск. Каналы берутся из уже размеченной базы. Дальше бот сможет прочитать их посты за выбранный период.\n"

    if not results:
        requested_position = get_requested_position(query_markup, user_query=user_query)
        text += "\nПодходящие каналы не найдены.\n\n"

        media_required = get_requested_media_filter(query_markup, user_query=user_query)

        if requested_position:
            text += (
                f"Фильтр позиции: нужна позиция «{requested_position}».\n"
                "Каналы с другой или неуказанной позицией отсечены.\n"
            )

        if media_required:
            text += (
                "Фильтр типа: нужны именно СМИ/медиа/издания.\n"
                "Авторские каналы, политики, блогеры и военкоры отсечены.\n"
            )

        text += "\nПопробуй сделать запрос предметнее: тема + тип источников + что исключить. Например: “игровые новинки для ПК, не авторские каналы”."
        return text

    text += "\nПодходящие каналы из базы:\n"

    for index, item in enumerate(results, start=1):
        username = normalize_found_channel_username(item.get("username"))
        title = item.get("title") or username
        score = item.get("score", 0)
        description = item.get("description")
        matched = item.get("matched") or []

        text += f"\n{index}. {username} — {title}\n"
        text += f"score: {score}\n"

        position = item.get("display_position") or extract_channel_position(item)
        if position:
            text += f"позиция: {position}\n"

        media_type = item.get("display_media_type")
        if media_type:
            text += f"тип: {media_type}\n"

        if description:
            text += f"описание: {description}\n"

        if matched:
            text += "почему: " + "; ".join(matched[:5]) + "\n"

    text += "\nСледующий шаг — прочитать посты из этих каналов за выбранный период."
    return text



def normalize_username_key_for_lookup(username: str | None) -> str:
    return (username or "").strip().lstrip("@").lower()


async def enrich_ranked_results_with_current_markup(pool, results: list[dict]) -> list[dict]:
    """
    rank_channels() может вернуть укороченный result без position_label.
    Тогда main видит "позиция=не указана", хотя в БД channel_ai_markup она есть.

    Добираем текущую разметку из channel_ai_markup и подмешиваем обратно в result
    перед жёсткими фильтрами позиции/СМИ.
    """
    if not results:
        return results

    usernames = [
        normalize_username_key_for_lookup(item.get("username"))
        for item in results
        if normalize_username_key_for_lookup(item.get("username"))
    ]

    if not usernames:
        return results

    async with pool.acquire() as connection:
        rows = await connection.fetch(
            """
            SELECT
                LOWER(TRIM(LEADING '@' FROM c.username)) AS username_key,
                c.id AS channel_id,
                c.username,
                COALESCE(c.tg_title, c.username) AS title,
                m.category,
                m.region,
                m.content_format,
                m.position_label,
                m.ai_keywords,
                m.ai_description,
                m.ai_classification
            FROM channels c
            JOIN channel_ai_markup m
              ON m.channel_id = c.id
             AND m.is_current = TRUE
            WHERE LOWER(TRIM(LEADING '@' FROM c.username)) = ANY($1::text[])
            """,
            usernames,
        )

    markup_by_username = {}

    for row in rows:
        data = dict(row)
        key = data.pop("username_key")

        data["position"] = data.get("position_label")
        data["format"] = data.get("content_format")
        data["classification"] = data.get("ai_classification") or {}

        if data.get("ai_description"):
            data["description"] = data.get("ai_description")

        markup_by_username[key] = data

    enriched = []

    for item in results:
        key = normalize_username_key_for_lookup(item.get("username"))
        extra = markup_by_username.get(key)

        if not extra:
            enriched.append(item)
            continue

        merged = dict(item)

        for field, value in extra.items():
            if value in (None, "", [], {}):
                continue

            if field not in merged or merged.get(field) in (None, "", [], {}, "не указана"):
                merged[field] = value

        if extra.get("position_label") and not merged.get("position"):
            merged["position"] = extra["position_label"]

        if extra.get("ai_classification") and not merged.get("classification"):
            merged["classification"] = extra["ai_classification"]

        enriched.append(merged)

    return enriched


async def find_channels_by_user_query(user_query: str, limit: int = 5) -> dict:
    """Разбирает запрос через ИИ и возвращает топ каналов из общей базы."""
    debug_print_block("ИИ-разбор запроса | input", {"user_query": user_query})

    query_markup = await analyze_user_query(user_query)
    debug_print_block("ИИ-разбор запроса | output от analyze_user_query", query_markup)

    query_markup = normalize_query_markup_for_channel_search(
        query_markup=query_markup,
        user_query=user_query,
    )
    debug_print_block("ИИ-разбор запроса | normalized query_markup для поиска", query_markup)

    pool = await create_channel_search_db_pool()
    try:
        marked_channels = await load_marked_channels(pool)

                                                                                                
                                                                                             
                                                                              
                                                                                       
                                                                                  
        if get_requested_media_filter(query_markup, user_query=user_query) or get_requested_position(query_markup, user_query=user_query):
            raw_limit = max(len(marked_channels), limit * 10, 50)
        else:
            raw_limit = max(limit * 10, 50)

        raw_results = rank_channels(query_markup, marked_channels, limit=raw_limit)
        debug_print_block(
            "Каналы после rank_channels до DB-enrich",
            [
                {
                    "username": item.get("username"),
                    "title": item.get("title"),
                    "score": item.get("score"),
                    "matched": item.get("matched"),
                    "position": item.get("position") or item.get("position_label"),
                    "category": item.get("category"),
                }
                for item in raw_results[:30]
            ],
        )

        raw_results = await enrich_ranked_results_with_current_markup(pool, raw_results)
        debug_print_block(
            "Каналы после DB-enrich перед фильтрами",
            [
                {
                    "username": item.get("username"),
                    "title": item.get("title"),
                    "score": item.get("score"),
                    "matched": item.get("matched"),
                    "position": item.get("position") or item.get("position_label"),
                    "category": item.get("category"),
                    "content_format": item.get("content_format"),
                    "media_status": extract_channel_media_status(item),
                }
                for item in raw_results[:30]
            ],
        )

        filtered_results = apply_position_filter_to_channel_results(
            query_markup=query_markup,
            results=raw_results,
            user_query=user_query,
        )
        results = filtered_results[:limit]
        debug_print_block(
            "Каналы после фильтров | финальный top",
            [
                {
                    "username": item.get("username"),
                    "title": item.get("title"),
                    "score": item.get("score"),
                    "matched": item.get("matched"),
                    "display_position": item.get("display_position"),
                    "display_media_type": item.get("display_media_type"),
                }
                for item in results
            ],
        )
    finally:
        await pool.close()

    return {
        "query_markup": query_markup,
        "marked_channels_count": len(marked_channels),
        "results": results,
    }


def normalize_match_text(value: str | None) -> str:
    return (value or "").lower().replace("ё", "е")


MESSAGE_STOP_WORDS = {
    "новости", "новость", "канал", "пост", "посты", "информация", "события",
    "материалы", "telegram", "телеграм", "ссылка", "подробнее", "сегодня",
    "вчера", "завтра", "который", "которая", "которые", "этого", "этот", "эта",
    "что", "как", "где", "про", "для", "или", "если", "мне", "надо", "найди",
    "напиши", "расскажи", "сделай", "обзор", "дай", "есть", "все", "самое",
}


def normalize_token(token: str) -> str:
    token = normalize_match_text(token)
    token = re.sub(r"[^a-zа-я0-9-]", "", token)
    return token.strip("-_")


def token_stem(token: str) -> str:
    """
    Очень простой русский/английский стемминг без внешних библиотек.
    Нужен не для идеальной лингвистики, а чтобы 'Украина/Украине/Украины'
    и 'нейросеть/нейросети' чаще совпадали.
    """
    token = normalize_token(token)
    if len(token) <= 5:
        return token

    endings = (
        "иями", "ями", "ами", "ого", "ему", "ыми", "ими", "ее", "ие", "ые", "ое",
        "ей", "ий", "ый", "ой", "ая", "яя", "ую", "юю", "ам", "ям", "ах", "ях",
        "ов", "ев", "ом", "ем", "ою", "ею", "ия", "иям", "иях", "ии", "ию", "ия",
        "а", "я", "ы", "и", "е", "у", "ю", "о",
    )

    for ending in endings:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[:-len(ending)]

    return token


def extract_search_tokens(text: str | None, limit: int = 20) -> list[str]:
    text = normalize_match_text(text)
    raw_tokens = re.findall(r"[a-zа-я0-9-]{3,}", text)

    result: list[str] = []
    seen: set[str] = set()

    for raw in raw_tokens:
        token = normalize_token(raw)
        if not token or token in MESSAGE_STOP_WORDS:
            continue

        stem = token_stem(token)
        if len(stem) < 3 or stem in MESSAGE_STOP_WORDS or stem in seen:
            continue

        seen.add(stem)
        result.append(stem)

        if len(result) >= limit:
            break

    return result



def extract_user_query_phrases(user_query: str | None, limit: int = 5) -> list[str]:
    """
    Достаём короткие фразы прямо из запроса пользователя.
    Это нужно для запросов вроде:
    "Советы по здоровью. Защита от клещей"

    ИИ может дать общие keywords: здоровье, профилактика.
    А слово/фраза пользователя "клещей" должны сильно помогать отбору сообщений.
    """
    text = normalize_match_text(user_query)

    if not text:
        return []

    chunks = re.split(r"[.!?;,\n]+", text)
    result = []
    seen = set()

    for chunk in chunks:
        tokens = extract_search_tokens(chunk, limit=6)

        if len(tokens) < 2:
            continue

        phrase = " ".join(tokens)
        if phrase in seen:
            continue

        seen.add(phrase)
        result.append(phrase)

        if len(result) >= limit:
            break

    return result


def build_message_query_terms(query_markup: dict, user_query: str | None = None) -> list[dict]:
    """
    Собирает поисковые термины для постов.
    Важно: тут используются не только keywords от ИИ, но и слова из исходного запроса.
    Иначе один кривой JSON от ИИ убивает весь отбор сообщений.
    """
    weighted_sources = [
        (query_markup.get("objects") or [], 12, "объект"),
        (query_markup.get("keywords") or [], 10, "keyword"),
        (query_markup.get("topics") or [], 7, "тема"),
    ]

    terms: list[dict] = []
    seen: set[str] = set()

    for values, weight, label in weighted_sources:
        for value in values:
            if not isinstance(value, str):
                continue
            phrase = normalize_match_text(value).strip()
            if not phrase or phrase in MESSAGE_STOP_WORDS:
                continue

            key = f"phrase:{phrase}"
            if key not in seen:
                seen.add(key)
                terms.append({"value": phrase, "tokens": extract_search_tokens(phrase), "weight": weight, "label": label})

                                                                          
                                                             
    for phrase in extract_user_query_phrases(user_query, limit=5):
        key = f"user_phrase:{phrase}"
        if key not in seen:
            seen.add(key)
            terms.append({
                "value": phrase,
                "tokens": extract_search_tokens(phrase, limit=8),
                "weight": 14,
                "label": "фраза пользователя",
            })

    for token in extract_search_tokens(user_query, limit=12):
        key = f"user_token:{token}"
        if key not in seen:
            seen.add(key)
            terms.append({"value": token, "tokens": [token], "weight": 12, "label": "слово пользователя"})

    category = normalize_match_text(query_markup.get("category"))
    if category and category != "неясно":
        for token in extract_search_tokens(category, limit=3):
            key = f"category:{token}"
            if key not in seen:
                seen.add(key)
                terms.append({"value": token, "tokens": [token], "weight": 2, "label": "категория"})

    return terms


def token_matches_text(token: str, text_tokens: set[str]) -> bool:
    token = token_stem(token)
    if not token:
        return False

    for text_token in text_tokens:
        if token == text_token:
            return True
                                                                              
        if len(token) >= 5 and len(text_token) >= 5:
            if token.startswith(text_token) or text_token.startswith(token):
                return True

    return False


def term_score(term: dict, text: str, text_tokens: set[str]) -> int:
    value = normalize_match_text(term.get("value"))
    tokens = term.get("tokens") or []
    weight = int(term.get("weight") or 0)

    if not value or not tokens:
        return 0

                                            
    if value in text:
        return weight

    matched_count = sum(1 for token in tokens if token_matches_text(token, text_tokens))
    if matched_count <= 0:
        return 0

    if len(tokens) == 1:
        return max(1, int(weight * 0.75))

                                                                                
    ratio = matched_count / len(tokens)
    if ratio >= 0.66:
        return max(1, int(weight * ratio))

    return 0


def score_message_for_query(query_markup: dict, message_item: dict, user_query: str | None = None) -> tuple[int, list[str]]:
    text = normalize_match_text(message_item.get("cleaned_text") or "")
    title = normalize_match_text(message_item.get("title") or "")
    full_text = f"{title}\n{text}"
    text_tokens = set(extract_search_tokens(full_text, limit=300))

    score = 0
    reasons: list[str] = []
    debug_terms: list[dict] = []

    terms = build_message_query_terms(query_markup, user_query=user_query)

    for term in terms:
        points = term_score(term, full_text, text_tokens)
        debug_terms.append({
            "label": term.get("label"),
            "value": term.get("value"),
            "tokens": term.get("tokens"),
            "weight": term.get("weight"),
            "points": points,
        })

        if points > 0:
            score += points
            reasons.append(f"{term['label']}: {term['value']} +{points}")

    negative_debug = []

    for negative in query_markup.get("negative_keywords") or []:
        negative_tokens = extract_search_tokens(str(negative), limit=8)
        hit = bool(negative_tokens and any(token_matches_text(token, text_tokens) for token in negative_tokens))

        negative_debug.append({
            "value": negative,
            "tokens": negative_tokens,
            "hit": hit,
            "points": -20 if hit else 0,
        })

        if hit:
            score -= 20
            reasons.append(f"минус: {negative} -20")

    length = len(text)
    length_points = 0

    if 250 <= length <= 1800:
        length_points = 3
        score += length_points
        reasons.append("длина сообщения: нормальная +3")
    elif 100 <= length < 250:
        length_points = 1
        score += length_points
        reasons.append("длина сообщения: короткая, но ок +1")
    elif length > 2500:
        length_points = -2
        score += length_points
        reasons.append("длина сообщения: слишком длинное -2")

    if is_search_debug_enabled():
        debug_print_block(
            "Расчёт score сообщения",
            {
                "username": message_item.get("username"),
                "title": message_item.get("title"),
                "date_text": message_item.get("date_text"),
                "final_score": score,
                "text_length": length,
                "text_preview": (message_item.get("cleaned_text") or "")[:700],
                "query_markup": query_markup,
                "terms_all": debug_terms,
                "terms_matched": [item for item in debug_terms if item["points"] > 0],
                "negative_terms": negative_debug,
                "length_points": length_points,
                "final_reasons": reasons[:12],
            },
        )

    return score, reasons[:8]


def rank_messages_for_query(
    query_markup: dict,
    messages: list[dict],
    limit: int = TOP_RELEVANT_MESSAGES_LIMIT,
    user_query: str | None = None,
) -> list[dict]:
    ranked = []

    for item in messages:
        score, reasons = score_message_for_query(query_markup, item, user_query=user_query)

        if score < MIN_RELEVANT_MESSAGE_SCORE:
            continue

        ranked.append({
            **item,
            "score": score,
            "matched": reasons,
        })

    ranked.sort(key=lambda item: item["score"], reverse=True)
    result = ranked[:limit]

    debug_print_block(
        f"Топ-{limit} сообщений после score",
        [compact_debug_message(item) for item in result],
    )

    return result


def format_ranked_messages_for_user(ranked_messages: list[dict]) -> str:
    if not ranked_messages:
        return ""

    output_text = f"Топ-{len(ranked_messages)} сообщений по счёту:\n\n"

    for index, item in enumerate(ranked_messages, start=1):
        username = item.get("username") or ""
        title = item.get("title") or username
        date_text = item.get("date_text") or "без даты"
        score = item.get("score", 0)
        cleaned_text = item.get("cleaned_text") or ""
        matched = item.get("matched") or []

        output_text += f"{index}. 📌 {title} ({username})\n"
        output_text += f"score: {score}\n"
        output_text += f"🕒 {date_text}\n"

        if matched:
            output_text += "почему: " + "; ".join(matched) + "\n"

        output_text += f"{cleaned_text}\n\n"

    return output_text.strip()


def build_found_channel_messages_output(user_context: dict, result: dict) -> str | None:
    query_markup = user_context.get("query_markup") or {}
    structured_messages = result.get("structured_messages") or []
    ranked_messages = rank_messages_for_query(
        query_markup,
        structured_messages,
        limit=TOP_RELEVANT_MESSAGES_LIMIT,
        user_query=user_context.get("search_query"),
    )

    if ranked_messages:
        return format_ranked_messages_for_user(ranked_messages)

    if structured_messages:
        return (
            "Сообщения собраны, но ни одно не набрало положительный score по запросу.\n\n"
            "Большую простыню не показываю: сейчас включён тестовый режим топа по счёту."
        )

    return None


async def build_found_channel_report_output(user_context: dict, result: dict) -> str | None:
    """Берёт топ сообщений по score и просит ИИ сделать отчёт. Если сообщений нет — возвращает None."""
    user_query = user_context.get("search_query") or ""
    query_markup = user_context.get("query_markup") or {}
    structured_messages = result.get("structured_messages") or []

    ranked_messages = rank_messages_for_query(
        query_markup,
        structured_messages,
        limit=TOP_RELEVANT_MESSAGES_LIMIT,
        user_query=user_context.get("search_query"),
    )

    if not ranked_messages:
        if structured_messages:
            return (
                "⚠️ Отчёт не создан\n\n"
                "Причина: сообщения собраны, но ни одно не набрало положительный score по запросу."
            )

        return None

    debug_print_block(
        "Данные, которые отправляются в ИИ-отчёт",
        {
            "user_query": user_query,
            "query_markup": query_markup,
            "report_preset": user_context.get("report_preset") or "brief",
            "ranked_messages": [compact_debug_message(item, max_text_chars=1200) for item in ranked_messages],
        },
    )

    try:
        report = await asyncio.wait_for(
            create_ai_report(
                user_query=user_query,
                query_markup=query_markup,
                ranked_messages=ranked_messages,
                report_preset=user_context.get("report_preset") or "brief",
            ),
            timeout=AI_REPORT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return (
            "⚠️ Отчёт не создан\n\n"
            f"Причина: ИИ-отчёт не успел подготовиться за {AI_REPORT_TIMEOUT_SECONDS} сек. "
            "Бот не завис — я специально остановил ожидание."
        )
    except Exception as error:
        return (
            "⚠️ Отчёт не создан\n\n"
            f"Причина: ошибка при подготовке ИИ-отчёта: {type(error).__name__}: {error}"
        )

    return format_ai_report_for_user(report)


def format_collect_result_stats(result: dict) -> str:
    return (
        f"Получено для пользователя: {result['messages_for_user']}\n"
        f"Из них взято из кэша БД: {result['messages_from_cache']}\n"
        f"Проверено новых через Telegram: {result['messages_found']}\n"
        f"Сохранено новых в БД: {result['messages_saved']}"
    )


def format_channel_collect_debug(result: dict) -> str:
    channel_stats = result.get("channel_stats") or []

    if not channel_stats:
        return ""

    lines = ["", "Диагностика по каналам:"]

    for item in channel_stats[:10]:
        line = (
            f"{item.get('username')}: "
            f"получено от Telegram={item.get('telegram_seen', 0)}, "
            f"в периоде={item.get('telegram_in_period', 0)}, "
            f"полезных={item.get('useful_saved', 0)}, "
            f"из кэша={item.get('from_cache', 0)}, "
            f"отфильтровано={item.get('filtered', 0)}, "
            f"реклама={item.get('ads_rejected', 0)}, "
            f"ad_ratio={item.get('ad_ratio', 0)}"
        )

        if item.get("ad_heavy"):
            line += ", ПОМЕЧЕН КАК РЕКЛАМНЫЙ"

        if item.get("error"):
            line += f", ошибка={item['error']}"

        lines.append(line)

    if len(channel_stats) > 10:
        lines.append(f"...ещё каналов: {len(channel_stats) - 10}")

    return "\n".join(lines)


def format_no_messages_explanation(result: dict, period_label: str) -> str:
    details = [
        "⚠️ Отчёт не создан",
        "",
        "Каналы подобраны, но за выбранный период бот не получил полезные текстовые сообщения.",
        "",
        f"Период: {period_label}",
        format_collect_result_stats(result),
    ]

    filter_text = format_filter_stats(result.get("filter_stats") or {})
    if filter_text:
        details.append(filter_text)

    debug_text = format_channel_collect_debug(result)
    if debug_text:
        details.append(debug_text)

    details.append("")
    details.append(
        "Что попробовать: выбрать период «За неделю», увеличить MAX_TELEGRAM_CHECK_LIMIT_PER_CHANNEL "
        "или проверить в терминале диагностику по каналам."
    )

    return "\n".join(details)


async def send_found_channel_report_or_explanation(
    message: Message,
    user_context: dict,
    result: dict,
    period_label: str,
) -> None:
    structured_messages = result.get("structured_messages") or []
    query_markup = user_context.get("query_markup") or {}

    if not structured_messages:
        await send_long_text(
            message,
            format_no_messages_explanation(result, period_label),
        )
        return

    ranked_messages = rank_messages_for_query(
        query_markup,
        structured_messages,
        limit=TOP_RELEVANT_MESSAGES_LIMIT,
        user_query=user_context.get("search_query"),
    )

    if not ranked_messages:
        await send_long_text(
            message,
            (
                "⚠️ Отчёт не создан\n\n"
                "Сообщения собраны, но ни одно не набрало положительный score по запросу.\n\n"
                + format_collect_result_stats(result)
                + format_channel_collect_debug(result)
            ),
        )
        return

    await message.answer(f"Нашёл топ-{len(ranked_messages)} сообщений по счёту. Готовлю ИИ-отчёт...")

    output_text = await build_found_channel_report_output(
        user_context=user_context,
        result=result,
    )

    if output_text:
        await send_long_text(message, output_text)
    else:
        await message.answer("⚠️ Отчёт не создан: неизвестная причина.")



def is_ad_filter_reason(reason: str | None) -> bool:
    return reason in {"ad_marker", "cross_promo"}


def should_mark_channel_ad_heavy(channel_stats: dict) -> tuple[bool, float]:
    checked_count = int(channel_stats.get("telegram_in_period") or 0)
    ad_count = int(channel_stats.get("ads_rejected") or 0)

    if checked_count <= 0:
        return False, 0.0

    ratio = ad_count / checked_count

    if ad_count >= AD_HEAVY_MIN_AD_MESSAGES and ratio >= AD_HEAVY_RATIO:
        return True, ratio

    return False, ratio


async def maybe_mark_ad_heavy_channel(channel: dict, channel_stats: dict) -> None:
    should_mark, ratio = should_mark_channel_ad_heavy(channel_stats)

    channel_stats["ad_ratio"] = round(ratio, 4)
    channel_stats["ad_heavy"] = bool(should_mark)

    if not should_mark:
        return

    username = channel.get("username") or ""
    ad_count = int(channel_stats.get("ads_rejected") or 0)
    checked_count = int(channel_stats.get("telegram_in_period") or 0)

    message = (
        f"Канал помечен как рекламный/замусоренный: {username}. "
        f"Рекламы: {ad_count}/{checked_count} ({ratio:.1%}). "
        f"auto_disable={AUTO_DISABLE_AD_HEAVY_CHANNELS}"
    )

    print(message)
    debug_print_block(
        "AD HEAVY CHANNEL",
        {
            "username": username,
            "ad_count": ad_count,
            "checked_count": checked_count,
            "ad_ratio": ratio,
            "auto_disable": AUTO_DISABLE_AD_HEAVY_CHANNELS,
        },
    )

    await mark_channel_as_ad_heavy(
        username=username,
        ad_count=ad_count,
        checked_count=checked_count,
        ad_ratio=ratio,
        auto_disable=AUTO_DISABLE_AD_HEAVY_CHANNELS,
    )


async def collect_messages_from_channels(
    db_user_id: int,
    selected_channels: list[dict],
    date_from: datetime,
    date_to: datetime | None = None,
) -> dict:
    """
    Собирает сообщения с учётом общего кэша.

    Исправленная логика кэша:
    1. Кэш берётся свежими сообщениями вперёд, а не старыми.
    2. Даже если кэш уже заполнил лимит, бот всё равно проверяет Telegram на новые посты.
    3. После чтения Telegram кэш и новые сообщения объединяются, сортируются по свежести,
       и только потом выбирается финальный MAX_MESSAGES_PER_CHANNEL для пользователя.
    """
    if date_to is None:
        date_to = datetime.now(timezone.utc)

    selected_channels_text = ", ".join(channel["username"] for channel in selected_channels)
    job_id = await create_parse_job(
        user_id=db_user_id,
        source_type="user",
        selected_channels=selected_channels_text,
    )

    messages_found = 0
    messages_saved = 0
    messages_from_cache = 0
    messages_for_user = 0
    useful_messages = []
    structured_messages = []
    filter_stats = {}
    channel_stats = []

    def make_message_item(
        *,
        username: str,
        title: str,
        telegram_message_id: int,
        cleaned_text: str,
        msg_date: datetime | None,
        from_cache: bool,
        db_message_id: int | None = None,
    ) -> dict:
        msg_date = ensure_aware_utc(msg_date)
        date_text = msg_date.strftime("%d.%m.%Y %H:%M") if msg_date else "без даты"
        return {
            "db_message_id": db_message_id,
            "telegram_message_id": telegram_message_id,
            "username": username,
            "title": title,
            "date_obj": msg_date,
            "date_text": date_text,
            "cleaned_text": cleaned_text,
            "from_cache": from_cache,
        }

    def sort_message_item(item: dict) -> tuple:
        date_obj = item.get("date_obj")
        if date_obj is None:
            date_obj = datetime.min.replace(tzinfo=timezone.utc)
        return (date_obj, int(item.get("telegram_message_id") or 0))

    try:
        block_message = get_telegram_global_block_message()
        if block_message:
            raise RuntimeError(block_message)

        for channel in selected_channels:
            title = channel.get("title") or channel["username"]
            username = channel["username"]
            current_channel_stats = {
                "username": username,
                "from_cache": 0,
                "telegram_seen": 0,
                "telegram_in_period": 0,
                "useful_saved": 0,
                "filtered": 0,
                "ads_rejected": 0,
                "ad_ratio": 0.0,
                "ad_heavy": False,
                "error": None,
            }
            channel_stats.append(current_channel_stats)

            print(f"Сбор: канал {username}, период {date_from} — {date_to}")

            canonical_channel = await get_or_create_telegram_channel(
                username=username,
                title=title,
            )
            telegram_channel_id = int(canonical_channel["id"])

            per_channel_messages: dict[int, dict] = {}
            cached_message_ids: set[int] = set()

            cached_messages = await get_cached_useful_messages(
                source_type="user",
                username=username,
                date_from=date_from,
                date_to=date_to,
                limit=MAX_MESSAGES_PER_CHANNEL,
            )

            for cached in cached_messages:
                telegram_message_id = int(cached["telegram_message_id"])
                cached_message_ids.add(telegram_message_id)
                per_channel_messages[telegram_message_id] = make_message_item(
                    username=username,
                    title=title,
                    telegram_message_id=telegram_message_id,
                    cleaned_text=cached["cleaned_text"] or "",
                    msg_date=cached.get("message_date"),
                    from_cache=True,
                    db_message_id=int(cached["id"]),
                )

            block_message = get_telegram_global_block_message()
            if block_message:
                raise RuntimeError(block_message)

            async with telegram_api_lock:
                block_message = get_telegram_global_block_message()
                if block_message:
                    raise RuntimeError(block_message)

                await telegram_request_pause(f"перед чтением {username}")

                async for msg in telegram_client.iter_messages(
                    username,
                    offset_date=date_to,
                    limit=MAX_TELEGRAM_CHECK_LIMIT_PER_CHANNEL,
                    wait_time=TELEGRAM_HISTORY_WAIT_SECONDS,
                ):
                    current_channel_stats["telegram_seen"] += 1
                    msg_date = ensure_aware_utc(msg.date)

                    if msg_date and msg_date < date_from:
                        break

                    if msg_date and msg_date >= date_to:
                        continue

                    current_channel_stats["telegram_in_period"] += 1
                    messages_found += 1

                    if (
                        TELEGRAM_PROGRESS_PAUSE_EVERY > 0
                        and messages_found % TELEGRAM_PROGRESS_PAUSE_EVERY == 0
                    ):
                        await telegram_request_pause(f"после проверки {messages_found} сообщений")

                    if msg.id in cached_message_ids:
                                                                                                   
                                                                                                 
                        continue

                    original_text = msg.text or ""
                    cleaned_text = clean_message_text(original_text)
                    filter_reason = get_filter_reason(cleaned_text, original_text)
                    is_useful = filter_reason is None

                    if not is_useful:
                        await save_rejected_message(
                            source_type="user",
                            source_channel_id=channel["id"],
                            username=username,
                            title=channel.get("title"),
                            telegram_message_id=msg.id,
                            original_text=original_text,
                            cleaned_text=cleaned_text,
                            reject_reason=filter_reason,
                            message_date=msg_date,
                            has_text=bool(original_text.strip()),
                            has_media=bool(msg.media),
                            views_count=getattr(msg, "views", None),
                            forwards_count=getattr(msg, "forwards", None),
                            replies_count=getattr(msg.replies, "replies", None) if msg.replies else None,
                        )

                        filter_stats[filter_reason] = filter_stats.get(filter_reason, 0) + 1
                        current_channel_stats["filtered"] += 1

                        if is_ad_filter_reason(filter_reason):
                            current_channel_stats["ads_rejected"] += 1

                        continue

                    save_result = await save_message(
                        source_type="user",
                        source_channel_id=channel["id"],                                                                    
                        telegram_channel_id=telegram_channel_id,
                        username=username,
                        title=channel.get("title"),
                        telegram_message_id=msg.id,
                        message_text=cleaned_text,
                        cleaned_text=cleaned_text,
                        message_date=msg_date,
                        has_text=bool(cleaned_text.strip()),
                        has_media=bool(msg.media),
                        is_useful=True,
                        filter_reason=None,
                        views_count=getattr(msg, "views", None),
                        forwards_count=getattr(msg, "forwards", None),
                        replies_count=getattr(msg.replies, "replies", None) if msg.replies else None,
                    )

                    if save_result["inserted"]:
                        messages_saved += 1

                    per_channel_messages[msg.id] = make_message_item(
                        username=username,
                        title=title,
                        telegram_message_id=msg.id,
                        cleaned_text=cleaned_text,
                        msg_date=msg_date,
                        from_cache=False,
                        db_message_id=save_result["id"],
                    )

            await maybe_mark_ad_heavy_channel(channel, current_channel_stats)

            final_channel_messages = sorted(
                per_channel_messages.values(),
                key=sort_message_item,
                reverse=True,
            )[:MAX_MESSAGES_PER_CHANNEL]

            if final_channel_messages:
                useful_messages.append(f"📌 {title} ({username})")

            for item in final_channel_messages:
                if item.get("db_message_id"):
                    await link_message_to_user_result(
                        user_id=db_user_id,
                        parse_job_id=job_id,
                        message_id=item["db_message_id"],
                    )

                if item.get("from_cache"):
                    messages_from_cache += 1
                    current_channel_stats["from_cache"] += 1

                messages_for_user += 1
                current_channel_stats["useful_saved"] += 1

                useful_messages.append(f"🕒 {item['date_text']}\n{item['cleaned_text']}")
                structured_messages.append({
                    "username": item["username"],
                    "title": item["title"],
                    "date_text": item["date_text"],
                    "cleaned_text": item["cleaned_text"],
                    "from_cache": item["from_cache"],
                })

        await finish_parse_job(
            job_id=job_id,
            status="finished",
            messages_found=messages_found,
            messages_saved=messages_saved,
        )

        return {
            "ok": True,
            "messages_found": messages_found,
            "messages_saved": messages_saved,
            "messages_from_cache": messages_from_cache,
            "messages_for_user": messages_for_user,
            "filter_stats": filter_stats,
            "channel_stats": channel_stats,
            "useful_messages": useful_messages,
            "structured_messages": structured_messages,
            "error": None,
        }

    except FloodWaitError as error:
        error_text = await handle_telegram_flood_wait(error, "сбор сообщений")

        await finish_parse_job(
            job_id=job_id,
            status="failed",
            messages_found=messages_found,
            messages_saved=messages_saved,
            error_text=error_text,
        )

        return {
            "ok": False,
            "messages_found": messages_found,
            "messages_saved": messages_saved,
            "messages_from_cache": messages_from_cache,
            "messages_for_user": messages_for_user,
            "filter_stats": filter_stats,
            "channel_stats": channel_stats,
            "useful_messages": useful_messages,
            "structured_messages": structured_messages,
            "error": error_text,
        }

    except Exception as error:
        await finish_parse_job(
            job_id=job_id,
            status="failed",
            messages_found=messages_found,
            messages_saved=messages_saved,
            error_text=str(error),
        )

        return {
            "ok": False,
            "messages_found": messages_found,
            "messages_saved": messages_saved,
            "messages_from_cache": messages_from_cache,
            "messages_for_user": messages_for_user,
            "filter_stats": filter_stats,
            "channel_stats": channel_stats,
            "useful_messages": useful_messages,
            "structured_messages": structured_messages,
            "error": str(error),
        }

@dp.message(CommandStart())
async def start_handler(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "menu"

    await message.answer(
        BOT_INTRO_TEXT,
        reply_markup=main_keyboard,
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    await register_user(message)

    await message.answer(
        BOT_INTRO_TEXT + "\n\n" + QUERY_GUIDE_TEXT
    )


@dp.message(Command("commands"))
async def commands_handler(message: Message):
    await register_user(message)

    await message.answer(
        "Список команд:\n\n"
        "/start — открыть главное меню\n"
        "/help — как работает бот и как писать запрос\n"
        "/commands — список команд\n"
        "/stop — убрать кнопки\n"
        "/restart — перезапустить меню"
    )


@dp.message(Command("stop"))
async def stop_handler(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "stopped"

    await message.answer(
        "Бот остановлен для вас. Кнопки убраны.\n\n"
        "Чтобы снова открыть меню, напишите /start или /restart.",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(Command("restart"))
async def restart_handler(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "menu"

    await message.answer(
        "Меню открыто. Выбери: подобрать каналы по теме или собрать из своих каналов.",
        reply_markup=main_keyboard,
    )


@dp.message(F.text.in_({"📋 Мои каналы", "📋 Каналы"}))
async def channels_menu(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "channels_menu"

    await message.answer(
        "Здесь твой личный список каналов для прямого сбора. Это отдельный режим: бот читает только те каналы, которые ты добавил сам.",
        reply_markup=channels_keyboard,
    )


@dp.message(F.text == "➕ Добавить")
async def add_channel_start(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "waiting_channel"

    await message.answer(
        "Отправь username или ссылку на канал.\n\n"
        "Например:\n"
        "@channel\n"
        "https://t.me/channel\n\n"
        "Можно отправлять несколько каналов подряд.\n"
        "Когда закончишь, нажми «⬅️ Назад».",
        reply_markup=channels_keyboard,
    )


@dp.message(F.text == "📋 Показать")
async def show_channels(message: Message):
    db_user = await register_user(message)

    channels = await get_user_channels(db_user["id"])

    if not channels:
        await message.answer(
            "У тебя пока нет добавленных каналов.",
            reply_markup=channels_keyboard,
        )
        return

    text = "Твои каналы:\n\n"

    for index, channel in enumerate(channels, start=1):
        title = channel.get("title")
        if title:
            text += f"{index}. {channel['username']} — {title}\n"
        else:
            text += f"{index}. {channel['username']}\n"

    await message.answer(text, reply_markup=channels_keyboard)


@dp.message(F.text == "🗑 Удалить")
async def delete_channel_start(message: Message):
    db_user = await register_user(message)

    user_id = message.from_user.id
    channels = await get_user_channels(db_user["id"])

    if not channels:
        await message.answer(
            "У тебя пока нет добавленных каналов, удалять нечего.",
            reply_markup=channels_keyboard,
        )
        return

    user_states[user_id] = "waiting_delete_channel_number"

    text = "Выбери канал, который нужно удалить из твоего списка.\n\n"
    text += "Напиши номер канала:\n\n"

    for index, channel in enumerate(channels, start=1):
        title = channel.get("title")
        if title:
            text += f"{index}. {channel['username']} — {title}\n"
        else:
            text += f"{index}. {channel['username']}\n"

    await message.answer(text, reply_markup=channels_keyboard)


@dp.message(F.text.in_({"📥 Собрать из моих каналов", "🔎 Собрать"}))
async def parse_menu(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "parse_menu"

    await message.answer(
        "Сбор из твоих каналов. Выбери каналы из личного списка, затем период.\n\nЕсли хочешь сначала подобрать каналы по теме из общей базы — нажми «🎯 Подобрать каналы по теме».",
        reply_markup=parse_keyboard,
    )


@dp.message(F.text.in_({"📥 Собрать выбранные каналы", "📥 Собрать по каналам"}))
async def parse_from_channel_start(message: Message):
    db_user = await register_user(message)

    user_id = message.from_user.id
    channels = await get_user_channels(db_user["id"])

    if not channels:
        await message.answer(
            "У тебя пока нет добавленных каналов.\n\n"
            "Сначала добавь канал в разделе «📋 Мои каналы».\n\nИли используй «🎯 Подобрать каналы по теме», если хочешь выбрать каналы из размеченной базы.",
            reply_markup=parse_keyboard,
        )
        return

    user_states[user_id] = "waiting_parse_channel_numbers"
    user_parse_context[user_id] = {}

    text = "Выбери от 1 до 3 каналов из личного списка.\n\n"
    text += "Напиши номера через пробел или запятую. Например: 1 2 3\n\n"

    for index, channel in enumerate(channels, start=1):
        title = channel.get("title")
        if title:
            text += f"{index}. {channel['username']} — {title}\n"
        else:
            text += f"{index}. {channel['username']}\n"

    await message.answer(
        text,
        reply_markup=parse_keyboard,
    )


@dp.message(F.text.in_({"🎯 Подобрать каналы по теме", "🔍 Найти каналы"}))
async def search_channels_start(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    user_states[user_id] = "waiting_channel_search_query"
    user_parse_context[user_id] = {}

    await message.answer(
        QUERY_GUIDE_TEXT,
        reply_markup=main_keyboard,
    )


@dp.message(F.text.in_({"🔁 Другой запрос", "🔁 Новый запрос"}))
async def search_channels_again(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    user_states[user_id] = "waiting_channel_search_query"
    user_parse_context[user_id] = {}

    await message.answer(
        QUERY_GUIDE_TEXT,
        reply_markup=main_keyboard,
    )


@dp.message(F.text.in_({"📥 Читать посты из подобранных", "📥 Читать посты из найденных", "📥 Собрать из найденных"}))
async def collect_from_found_channels_start(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    found_channels = user_parse_context.get(user_id, {}).get("found_channels") or []

    if not found_channels:
        user_states[user_id] = "waiting_channel_search_query"
        await message.answer(
            "Список подобранных каналов потерялся. Напиши запрос заново.",
            reply_markup=main_keyboard,
        )
        return

    selected_channels = convert_found_channels_for_collect(found_channels, limit=5)

    if not selected_channels:
        user_states[user_id] = "waiting_channel_search_query"
        await message.answer(
            "Не получилось подготовить найденные каналы для сбора. Попробуй другой запрос.",
            reply_markup=main_keyboard,
        )
        return

    user_parse_context[user_id]["selected_channels"] = selected_channels
    user_states[user_id] = "waiting_found_report_preset"

    selected_text = "Буду читать сообщения из этих каналов:\n\n"
    for index, channel in enumerate(selected_channels, start=1):
        selected_text += f"{index}. {channel['username']} — {channel.get('title') or channel['username']}\n"

    selected_text += "\n" + REPORT_PRESET_HELP_TEXT

    await message.answer(
        selected_text,
        reply_markup=report_preset_keyboard,
    )


@dp.message(F.text.in_({"ℹ️ Помощь", "⚙️ Настройки"}))
async def settings_stub(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "settings"

    await message.answer(
        BOT_INTRO_TEXT + "\n\nНажми «🎯 Как писать запрос», чтобы посмотреть примеры.",
        reply_markup=settings_keyboard,
    )


@dp.message(F.text == "📖 Команды")
async def commands_button(message: Message):
    await register_user(message)

    await message.answer(
        "Список команд:\n\n"
        "/start — открыть главное меню\n"
        "/help — как работает бот и как писать запрос\n"
        "/commands — список команд\n"
        "/stop — убрать кнопки\n"
        "/restart — перезапустить меню"
    )


@dp.message(F.text == "🎯 Как писать запрос")
async def query_guide_button(message: Message):
    await register_user(message)

    await message.answer(
        QUERY_GUIDE_TEXT,
        reply_markup=settings_keyboard,
    )


@dp.message(F.text == "⬅️ Назад")
async def back_to_main_menu(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "menu"
    user_parse_context.pop(message.from_user.id, None)

    await message.answer(
        "Главное меню. Бот может подобрать каналы по теме или собрать сообщения из твоих каналов.",
        reply_markup=main_keyboard,
    )


@dp.message()
async def text_handler(message: Message):
    db_user = await register_user(message)

    user_id = message.from_user.id
    state = user_states.get(user_id, "menu")
    text = message.text.strip() if message.text else ""

    if state == "stopped":
        await message.answer(
            "Бот сейчас остановлен для вас.\n\n"
            "Чтобы снова открыть меню, напишите /start или /restart."
        )
        return

    if state == "waiting_channel_search_query":
        if len(text) < 3:
            await message.answer(
                SHORT_QUERY_HINT_TEXT,
                reply_markup=main_keyboard,
            )
            return

        set_user_busy(user_id)
        try:
            await answer_progress(
                message,
                "Разбираю запрос. " + CHANNEL_PICK_EXPLANATION + "\n\n"
                "Потом ты сможешь выбрать период и собрать сообщения."
            )

            try:
                search_result = await find_channels_by_user_query(text, limit=5)
            except Exception as error:
                await message.answer(
                    "Не получилось подобрать каналы.\n\n"
                    f"Ошибка: {error}",
                    reply_markup=main_keyboard,
                )
                user_states[user_id] = "menu"
                user_parse_context.pop(user_id, None)
                return

            found_channels = search_result["results"]
            query_markup = search_result["query_markup"]

            user_parse_context[user_id] = {
                "search_query": text,
                "query_markup": query_markup,
                "found_channels": found_channels,
            }

            output_text = format_channel_search_results(
                user_query=text,
                query_markup=query_markup,
                results=found_channels,
            )

            if found_channels:
                user_states[user_id] = "waiting_found_channels_action"
                await send_long_text(message, output_text, reply_markup=search_results_keyboard)
            else:
                user_states[user_id] = "waiting_channel_search_query"
                await send_long_text(message, output_text, reply_markup=main_keyboard)
        finally:
            clear_user_busy(user_id)

        return

    if state == "waiting_channel":
        channel = normalize_channel_input(text)

        if not channel:
            await message.answer(
                "Это не похоже на Telegram-канал.\n\n"
                "Отправь в формате:\n"
                "@channel\n"
                "https://t.me/channel\n\n"
                "Или нажми «⬅️ Назад».",
                reply_markup=channels_keyboard,
            )
            return

        channel_exists = await user_has_channel(db_user["id"], channel)

        if channel_exists:
            await message.answer(
                f"Этот канал уже есть в твоём списке:\n{channel}\n\n"
                "Можешь отправить ещё один канал или нажать «⬅️ Назад».",
                reply_markup=channels_keyboard,
            )
            user_states[user_id] = "waiting_channel"
            return

                                                   
                                  
                                                                     
                                                                 
                                                                   
                                                
              
                                                     
                   

        set_user_busy(user_id)
        try:
            await answer_progress(message, f"Проверяю доступ к каналу:\n{channel}")

            channel_check = await check_channel_access(channel)

            if not channel_check["ok"]:
                await message.answer(
                    f"Канал не добавлен:\n{channel}\n\n"
                    f"Причина: {channel_check['error']}\n\n"
                    "Можешь отправить другой открытый канал или нажать «⬅️ Назад».",
                    reply_markup=channels_keyboard,
                )
                user_states[user_id] = "waiting_channel"
                return

            await add_user_channel(
                user_id=db_user["id"],
                username=channel,
                title=channel_check["title"],
            )

            title_text = f"\nНазвание: {channel_check['title']}" if channel_check["title"] else ""

            await message.answer(
                f"Канал проверен и добавлен:\n{channel}{title_text}\n\n"
                "Можешь отправить ещё один канал или нажать «⬅️ Назад».",
                reply_markup=channels_keyboard,
            )

            user_states[user_id] = "waiting_channel"
        finally:
            clear_user_busy(user_id)

        return

    if state == "waiting_delete_channel_number":
        channels = await get_user_channels(db_user["id"])

        if not text.isdigit():
            await message.answer(
                "Нужно написать номер канала из списка.\n\n"
                "Например: 1",
                reply_markup=channels_keyboard,
            )
            return

        channel_index = int(text) - 1

        if channel_index < 0 or channel_index >= len(channels):
            await message.answer(
                "Такого номера нет в списке каналов.",
                reply_markup=channels_keyboard,
            )
            return

        channel = channels[channel_index]
        removed = await remove_user_channel(
            user_id=db_user["id"],
            user_channel_id=channel["id"],
        )

        if removed:
            await message.answer(
                f"Канал удалён из твоего списка:\n{channel['username']}",
                reply_markup=channels_keyboard,
            )
        else:
            await message.answer(
                "Канал не найден в твоём списке или уже был удалён.",
                reply_markup=channels_keyboard,
            )

        user_states[user_id] = "channels_menu"
        return

    if state == "waiting_parse_channel_numbers":
        channels = await get_user_channels(db_user["id"])
        selected_indexes = parse_channel_numbers(text, len(channels))

        if selected_indexes is None:
            await message.answer(
                "Нужно выбрать от 1 до 3 каналов из списка.\n\n"
                "Например: 1 или 1 2 3",
                reply_markup=parse_keyboard,
            )
            return

        selected_channels = [channels[index] for index in selected_indexes]
        user_parse_context[user_id] = {
            "selected_channels": selected_channels,
        }
        user_states[user_id] = "waiting_parse_period"

        selected_text = "Выбраны каналы:\n\n"
        for index, channel in enumerate(selected_channels, start=1):
            title = channel.get("title") or channel["username"]
            selected_text += f"{index}. {channel['username']} — {title}\n"

        selected_text += "\nТеперь выбери период. Бот будет читать сообщения только за этот промежуток."

        await message.answer(
            selected_text,
            reply_markup=period_keyboard,
        )
        return

    if state == "waiting_parse_period":
        if text == "📆 Свой период":
            user_states[user_id] = "waiting_custom_period"
            await message.answer(
                "Введи период двумя датами.\n\n"
                "Формат: ДД.ММ.ГГГГ ДД.ММ.ГГГГ\n"
                "Например: 01.05.2026 20.05.2026\n\n"
                f"Ограничения: не больше {MAX_CUSTOM_RANGE_DAYS} дня и не старше {MAX_CUSTOM_LOOKBACK_DAYS} дней.",
                reply_markup=period_keyboard,
            )
            return

        period_range = get_period_range(text)

        if period_range is None:
            await message.answer(
                "Выбери период кнопкой: за день, за неделю, за месяц или свой период.",
                reply_markup=period_keyboard,
            )
            return

        date_from, date_to = period_range
        period_label = text

        selected_channels = user_parse_context.get(user_id, {}).get("selected_channels", [])

        if not selected_channels:
            user_states[user_id] = "parse_menu"
            await message.answer(
                "Выбор каналов потерялся. Начни сбор заново.",
                reply_markup=parse_keyboard,
            )
            return

        channels_text = ", ".join(channel["username"] for channel in selected_channels)
        set_user_busy(user_id)
        try:
            await answer_progress(
                message,
                f"Начинаю сбор.\n\nКаналы: {channels_text}\nПериод: {period_label}"
            )

            result = await collect_messages_from_channels(
                db_user_id=db_user["id"],
                selected_channels=selected_channels,
                date_from=date_from,
                date_to=date_to,
            )

            if not result["ok"]:
                await message.answer(
                    "Сбор завершился с ошибкой.\n\n"
                    f"Получено для пользователя до ошибки: {result['messages_for_user']}\n"
                    f"Проверено новых через Telegram до ошибки: {result['messages_found']}\n"
                    f"Сохранено новых до ошибки: {result['messages_saved']}\n"
                    f"Взято из кэша до ошибки: {result['messages_from_cache']}\n"
                    f"Ошибка: {result['error']}",
                    reply_markup=parse_keyboard,
                )
                user_states[user_id] = "parse_menu"
                user_parse_context.pop(user_id, None)
                return

            if result["useful_messages"]:
                output_text = "Сохранённые и очищенные сообщения:\n\n"
                output_text += "\n\n".join(result["useful_messages"])
                await send_long_text(message, output_text)
            else:
                await message.answer("Полезных сообщений после грубого фильтра не найдено.")

            await message.answer(
                "Готово.\n\n"
                f"Получено для пользователя: {result['messages_for_user']}\n"
                f"Из них взято из кэша БД: {result['messages_from_cache']}\n"
                f"Проверено новых через Telegram: {result['messages_found']}\n"
                f"Сохранено новых в БД: {result['messages_saved']}",
                reply_markup=parse_keyboard,
            )

            user_states[user_id] = "parse_menu"
            user_parse_context.pop(user_id, None)
        finally:
            clear_user_busy(user_id)

        return

    if state == "waiting_found_report_preset":
        report_preset = get_report_preset_from_text(text)

        if not report_preset:
            await message.answer(
                "Выбери тип ИИ-отчёта кнопкой.\n\n" + REPORT_PRESET_HELP_TEXT,
                reply_markup=report_preset_keyboard,
            )
            return

        user_parse_context.setdefault(user_id, {})["report_preset"] = report_preset
        user_states[user_id] = "waiting_found_channels_period"

        await message.answer(
            f"Тип отчёта: {get_report_preset_title(report_preset)}\n\n"
            "Теперь выбери период. Бот будет читать сообщения только за этот промежуток.",
            reply_markup=period_keyboard,
        )
        return

    if state == "waiting_found_channels_period":
        if text == "📆 Свой период":
            user_states[user_id] = "waiting_found_channels_custom_period"
            await message.answer(
                "Введи период двумя датами.\n\n"
                "Формат: ДД.ММ.ГГГГ ДД.ММ.ГГГГ\n"
                "Например: 01.05.2026 20.05.2026\n\n"
                f"Ограничения: не больше {MAX_CUSTOM_RANGE_DAYS} дня и не старше {MAX_CUSTOM_LOOKBACK_DAYS} дней.",
                reply_markup=period_keyboard,
            )
            return

        period_range = get_period_range(text)

        if period_range is None:
            await message.answer(
                "Выбери период кнопкой: за день, за неделю, за месяц или свой период.",
                reply_markup=period_keyboard,
            )
            return

        date_from, date_to = period_range
        period_label = text
        selected_channels = user_parse_context.get(user_id, {}).get("selected_channels", [])

        if not selected_channels:
            user_states[user_id] = "waiting_channel_search_query"
            await message.answer(
                "Список подобранных каналов потерялся. Напиши запрос заново.",
                reply_markup=main_keyboard,
            )
            return

        channels_text = ", ".join(channel["username"] for channel in selected_channels)
        set_user_busy(user_id)
        try:
            await answer_progress(
                message,
                f"Начинаю чтение сообщений из подобранных каналов.\n\nКаналы: {channels_text}\nПериод: {period_label}"
            )

            result = await collect_messages_from_channels(
                db_user_id=db_user["id"],
                selected_channels=selected_channels,
                date_from=date_from,
                date_to=date_to,
            )

            if not result["ok"]:
                await message.answer(
                    "Сбор завершился с ошибкой.\n\n"
                    f"Получено для пользователя до ошибки: {result['messages_for_user']}\n"
                    f"Проверено новых через Telegram до ошибки: {result['messages_found']}\n"
                    f"Сохранено новых до ошибки: {result['messages_saved']}\n"
                    f"Взято из кэша до ошибки: {result['messages_from_cache']}\n"
                    f"Ошибка: {result['error']}",
                    reply_markup=main_keyboard,
                )
                user_states[user_id] = "menu"
                user_parse_context.pop(user_id, None)
                return

            await send_found_channel_report_or_explanation(
                message=message,
                user_context=user_parse_context.get(user_id, {}),
                result=result,
                period_label=period_label,
            )

            await message.answer(
                "Сбор завершён.\n\n" + format_collect_result_stats(result),
                reply_markup=main_keyboard,
            )

            user_states[user_id] = "menu"
            user_parse_context.pop(user_id, None)
        finally:
            clear_user_busy(user_id)

        return

    if state == "waiting_found_channels_custom_period":
        custom_period = parse_custom_period(text)

        if not custom_period["ok"]:
            await message.answer(
                f"{custom_period['error']}\n\n"
                "Попробуй ещё раз или нажми «⬅️ Назад».",
                reply_markup=period_keyboard,
            )
            return

        selected_channels = user_parse_context.get(user_id, {}).get("selected_channels", [])

        if not selected_channels:
            user_states[user_id] = "waiting_channel_search_query"
            await message.answer(
                "Список подобранных каналов потерялся. Напиши запрос заново.",
                reply_markup=main_keyboard,
            )
            return

        date_from = custom_period["date_from"]
        date_to = custom_period["date_to"]
        period_label = custom_period["label"]

        channels_text = ", ".join(channel["username"] for channel in selected_channels)
        set_user_busy(user_id)
        try:
            await answer_progress(
                message,
                f"Начинаю чтение сообщений из подобранных каналов.\n\nКаналы: {channels_text}\nПериод: {period_label}"
            )

            result = await collect_messages_from_channels(
                db_user_id=db_user["id"],
                selected_channels=selected_channels,
                date_from=date_from,
                date_to=date_to,
            )

            if not result["ok"]:
                await message.answer(
                    "Сбор завершился с ошибкой.\n\n"
                    f"Получено для пользователя до ошибки: {result['messages_for_user']}\n"
                    f"Проверено новых через Telegram до ошибки: {result['messages_found']}\n"
                    f"Сохранено новых до ошибки: {result['messages_saved']}\n"
                    f"Взято из кэша до ошибки: {result['messages_from_cache']}\n"
                    f"Ошибка: {result['error']}",
                    reply_markup=main_keyboard,
                )
                user_states[user_id] = "menu"
                user_parse_context.pop(user_id, None)
                return

            await send_found_channel_report_or_explanation(
                message=message,
                user_context=user_parse_context.get(user_id, {}),
                result=result,
                period_label=period_label,
            )

            await message.answer(
                "Сбор завершён.\n\n" + format_collect_result_stats(result),
                reply_markup=main_keyboard,
            )

            user_states[user_id] = "menu"
            user_parse_context.pop(user_id, None)
        finally:
            clear_user_busy(user_id)

        return

    if state == "waiting_custom_period":
        custom_period = parse_custom_period(text)

        if not custom_period["ok"]:
            await message.answer(
                f"{custom_period['error']}\n\n"
                "Попробуй ещё раз или нажми «⬅️ Назад».",
                reply_markup=period_keyboard,
            )
            return

        selected_channels = user_parse_context.get(user_id, {}).get("selected_channels", [])

        if not selected_channels:
            user_states[user_id] = "parse_menu"
            await message.answer(
                "Выбор каналов потерялся. Начни сбор заново.",
                reply_markup=parse_keyboard,
            )
            return

        date_from = custom_period["date_from"]
        date_to = custom_period["date_to"]
        period_label = custom_period["label"]

        channels_text = ", ".join(channel["username"] for channel in selected_channels)
        set_user_busy(user_id)
        try:
            await answer_progress(
                message,
                f"Начинаю сбор.\n\nКаналы: {channels_text}\nПериод: {period_label}"
            )

            result = await collect_messages_from_channels(
                db_user_id=db_user["id"],
                selected_channels=selected_channels,
                date_from=date_from,
                date_to=date_to,
            )

            if not result["ok"]:
                await message.answer(
                    "Сбор завершился с ошибкой.\n\n"
                    f"Получено для пользователя до ошибки: {result['messages_for_user']}\n"
                    f"Проверено новых через Telegram до ошибки: {result['messages_found']}\n"
                    f"Сохранено новых до ошибки: {result['messages_saved']}\n"
                    f"Взято из кэша до ошибки: {result['messages_from_cache']}\n"
                    f"Ошибка: {result['error']}",
                    reply_markup=parse_keyboard,
                )
                user_states[user_id] = "parse_menu"
                user_parse_context.pop(user_id, None)
                return

            if result["useful_messages"]:
                output_text = "Сохранённые и очищенные сообщения:\n\n"
                output_text += "\n\n".join(result["useful_messages"])
                await send_long_text(message, output_text)
            else:
                await message.answer("Полезных сообщений после грубого фильтра не найдено.")

            await message.answer(
                "Готово.\n\n"
                f"Получено для пользователя: {result['messages_for_user']}\n"
                f"Из них взято из кэша БД: {result['messages_from_cache']}\n"
                f"Проверено новых через Telegram: {result['messages_found']}\n"
                f"Сохранено новых в БД: {result['messages_saved']}",
                reply_markup=parse_keyboard,
            )

            user_states[user_id] = "parse_menu"
            user_parse_context.pop(user_id, None)
        finally:
            clear_user_busy(user_id)

        return

    await message.answer(
        "Сейчас я не принимаю обычный текст.\n\n"
        "Выбери действие в меню.",
        reply_markup=main_keyboard,
    )


async def main():
    print("Подключаюсь к PostgreSQL...")
    await init_db()
    print("PostgreSQL подключен")

    print("Запускаю Telethon...")

    is_authorized = await ensure_telegram_client_started()

    if not is_authorized:
        print("Telethon не авторизован. Бот не запущен.")
        await close_db()
        return

    me = await telegram_client.get_me()

    if getattr(me, "bot", False):
        print("Ошибка: эта Telethon-сессия авторизована как бот.")
        print("Удалите sessions/user_session.session и войдите через номер телефона.")
        await close_db()
        return

    print(f"Telethon запущен как пользователь: {me.first_name} / id={me.id}")
    print("Бот запущен")

    try:
        await dp.start_polling(bot)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())