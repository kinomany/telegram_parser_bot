import json
import re
from datetime import datetime, timezone

from aiogram import F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardRemove

from config import (
    MAX_CHANNELS_PER_PARSE,
    MAX_CUSTOM_RANGE_DAYS,
    MAX_CUSTOM_LOOKBACK_DAYS,
    TOP_RELEVANT_MESSAGES_LIMIT,
    DIGEST_MAX_CHANNELS,
    DIGEST_COLLECT_MESSAGES_PER_CHANNEL,
    DIGEST_SUBSCRIPTION_DEFAULT_TIMEZONE,
    DIGEST_SUBSCRIPTION_DEFAULT_HOUR,
    DIGEST_SUBSCRIPTION_DEFAULT_MINUTE,
    ADMIN_TELEGRAM_IDS,
    ADMIN_ERRORS_LIMIT,
    ADMIN_ERROR_TRACEBACK_CHARS,
)
from app.bot.runtime import (
    bot,
    dp,
    user_states,
    user_parse_context,
    set_user_busy,
    clear_user_busy,
    is_user_busy,
    answer_progress,
)
from app.bot.keyboards import (
    main_keyboard,
    channels_keyboard,
    channel_view_keyboard,
    parse_keyboard,
    found_channels_keyboard,
    found_channels_count_keyboard,
    report_preset_keyboard,
    period_keyboard,
    digest_channel_view_keyboard,
    digest_period_keyboard,
    digest_history_keyboard,
    autodigest_keyboard,
    subscription_manage_keyboard,
    subscription_delete_confirm_keyboard,
    subscription_run_update_keyboard,
    user_channel_delete_confirm_keyboard,
    subscription_period_keyboard,
    subscription_digest_preset_keyboard,
    subscription_timezone_keyboard,
    subscription_time_keyboard,
    settings_keyboard,
    admin_keyboard,
    build_autodigest_keyboard,
    build_subscription_manage_keyboard,
    build_user_channel_category_keyboard,
    build_user_channel_category_view_keyboard,
)
from app.bot.texts import (
    BOT_INTRO_TEXT,
    QUERY_GUIDE_TEXT,
    SHORT_QUERY_HINT_TEXT,
    CHANNEL_PICK_EXPLANATION,
    REPORT_PRESET_HELP_TEXT,
    get_report_preset_from_text,
    get_report_preset_title,
)
from app.bot.utils import register_user, normalize_channel_input, parse_channel_numbers, send_long_text
from app.db.database import (
    add_user_channel,
    count_user_channels,
    get_user_channel_categories,
    get_user_channels,
    get_user_channels_for_query,
    get_user_channel_digest_period_start,
    save_user_channel_digest_state,
    create_digest_subscription,
    delete_digest_subscription,
    disable_digest_subscription,
    enable_digest_subscription,
    get_digest_subscription_debug_stats,
    list_digest_subscriptions,
    lock_digest_subscription_now,
    update_digest_subscription_period,
    update_digest_subscription_preset,
    update_digest_subscription_time,
    update_digest_subscription_timezone,
    add_user_channels_to_digest_subscription,
    remove_user_channels_from_digest_subscription,
    get_admin_autodigest_stats,
    get_admin_system_stats,
    get_bot_error_stats,
    list_recent_bot_errors,
    list_user_digest_history,
    get_user_digest_history_item,
    count_user_digest_history,
    user_has_channel,
    get_user_channel_subscription_usage,
    remove_user_channel_with_subscription_links,
    remove_user_channel,
    get_user_account_type_by_telegram_id,
)
from app.telegram.access import check_channel_access
from app.telegram.collector import collect_messages_from_channels
from app.utils.dates import get_period_range, parse_custom_period
from app.search.channel_search import (
    find_channels_by_user_query,
    convert_found_channels_for_collect,
    format_channel_search_results,
)
from app.search.message_ranker import rank_messages_for_query, format_ranked_messages_for_user
from app.reports.service import (
    send_found_channel_report_or_explanation,
    build_found_channel_report_output,
    format_collect_result_stats,
    format_channel_collect_debug,
)
from app.reports.digest_service import send_digest_report_or_explanation
from app.reports.digest_subscription_service import (
    run_digest_subscription,
    preview_digest_subscription_messages,
)
from app.jobs.vacancy_parser import (
    DEFAULT_VACANCY_DAYS,
    find_vacancy_channels,
    format_vacancy_channels_preview,
    format_vacancy_parse_result,
    parse_vacancies_from_channels,
    parse_vacancy_keywords,
)
from app.utils.timezones import (
    DEFAULT_TIMEZONE,
    format_datetime_local,
    format_send_time,
    normalize_timezone_name,
    parse_send_time,
    validate_timezone_name,
)
from app.ai.tax_tree import ГЛАВНЫЕ_КАТЕГОРИИ

DEFAULT_FOUND_CHANNEL_COUNT = 5
MAX_FOUND_CHANNELS_FOR_USER_CHOICE = 10
MAX_USER_CHANNELS_PER_ACCOUNT = 30
MAX_DIGEST_CHANNELS = DIGEST_MAX_CHANNELS


def clamp_int(value, min_value: int, max_value: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default

    return max(min_value, min(number, max_value))


def is_env_admin_user(telegram_id: int | None) -> bool:
    """Bootstrap-доступ: старый ADMIN_TELEGRAM_IDS оставлен как запасной способ зайти в админку."""
    return bool(telegram_id is not None and int(telegram_id) in ADMIN_TELEGRAM_IDS)


async def is_admin_user(telegram_id: int | None) -> bool:
    if telegram_id is None:
        return False

    if is_env_admin_user(telegram_id):
        return True

    account_type = await get_user_account_type_by_telegram_id(int(telegram_id))
    return account_type == "admin"


async def is_admin_message(message: Message) -> bool:
    return await is_admin_user(message.from_user.id if message.from_user else None)


async def get_autodigest_keyboard_for_message(message: Message):
    return build_autodigest_keyboard(is_admin=await is_admin_message(message))


async def get_subscription_manage_keyboard_for_message(message: Message):
    return build_subscription_manage_keyboard(is_admin=await is_admin_message(message))


async def require_admin(message: Message) -> bool:
    if await is_admin_message(message):
        return True

    await message.answer(
        "Эта команда доступна только администратору бота.",
        reply_markup=main_keyboard,
    )
    return False


STATE_STATUS_MAP = {
    "menu": ("Главное меню", "ожидание выбора действия"),
    "settings": ("Помощь / настройки", "просмотр подсказок"),
    "admin_menu": ("Админка", "просмотр статуса и ошибок"),
    "waiting_vacancy_channel_query": ("Парс вакансий", "поиск каналов"),
    "waiting_vacancy_keywords": ("Парс вакансий", "ключевики"),
    "stopped": ("Бот остановлен для пользователя", "кнопки убраны"),
    "channels_menu": ("Мои каналы", "главное меню личных каналов"),
    "user_channels_view_menu": ("Мои каналы", "выбор способа просмотра"),
    "waiting_channel": ("Добавление канала", "ожидание @username или ссылки"),
    "waiting_user_channel_category": ("Добавление канала", "выбор категории"),
    "waiting_user_channel_category_full": ("Добавление канала", "выбор категории из полного списка"),
    "waiting_user_channel_category_view": ("Просмотр каналов", "выбор категории"),
    "waiting_delete_channel_number": ("Удаление канала", "выбор номера канала"),
    "waiting_delete_channel_confirm": ("Удаление канала", "подтверждение удаления"),
    "digest_channel_view_menu": ("Ручная сводка", "выбор способа показа каналов"),
    "waiting_digest_category_view": ("Ручная сводка", "выбор категории каналов"),
    "waiting_digest_channel_numbers": ("Ручная сводка", "выбор номеров каналов"),
    "waiting_digest_period": ("Ручная сводка", "выбор периода"),
    "waiting_digest_custom_period": ("Ручная сводка", "ввод своего периода"),
    "waiting_digest_history_number": ("История сводок", "выбор старой сводки по номеру"),
    "autodigest_menu": ("Автосводки", "главное меню автосводок"),
    "waiting_subscription_channel_numbers": ("Создание автосводки", "выбор каналов"),
    "waiting_subscription_period": ("Создание автосводки", "выбор частоты"),
    "waiting_subscription_preset": ("Создание автосводки", "выбор формата сводки"),
    "waiting_subscription_timezone": ("Создание автосводки", "выбор timezone"),
    "waiting_subscription_custom_timezone": ("Создание автосводки", "ввод timezone текстом"),
    "waiting_subscription_time": ("Создание автосводки", "выбор времени"),
    "waiting_subscription_custom_time": ("Создание автосводки", "ввод времени текстом"),
    "waiting_subscription_manage_number": ("Управление автосводками", "выбор автосводки"),
    "waiting_subscription_manage_action": ("Управление автосводкой", "выбор действия"),
    "waiting_subscription_disable_number": ("Управление автосводками", "выбор автосводки для отключения"),
    "waiting_subscription_run_number": ("Запуск автосводки", "выбор автосводки"),
    "waiting_subscription_run_update_choice": ("Запуск автосводки", "выбор режима обновления точки парса"),
    "waiting_subscription_preview_number": ("Предпросмотр автосводки", "выбор автосводки"),
    "waiting_subscription_add_channel_numbers": ("Управление автосводкой", "добавление каналов"),
    "waiting_subscription_remove_channel_numbers": ("Управление автосводкой", "удаление каналов"),
    "waiting_subscription_change_period": ("Управление автосводкой", "изменение периода"),
    "waiting_subscription_change_preset": ("Управление автосводкой", "изменение формата"),
    "waiting_subscription_change_time": ("Управление автосводкой", "изменение времени"),
    "waiting_subscription_change_custom_time": ("Управление автосводкой", "ввод времени текстом"),
    "waiting_subscription_change_timezone": ("Управление автосводкой", "изменение timezone"),
    "waiting_subscription_change_custom_timezone": ("Управление автосводкой", "ввод timezone текстом"),
    "waiting_subscription_delete_confirm": ("Управление автосводкой", "подтверждение удаления"),
    "waiting_channel_search_query": ("Поиск каналов", "ожидание запроса"),
    "waiting_found_channels_action": ("Поиск каналов", "настройка выбранных каналов"),
    "waiting_found_channel_count": ("Поиск каналов", "выбор количества каналов"),
    "waiting_found_channel_list": ("Поиск каналов", "выбор конкретных каналов"),
    "waiting_found_report_preset": ("Поиск каналов", "выбор типа отчёта"),
    "waiting_found_channels_period": ("Поиск каналов", "выбор периода"),
    "waiting_found_channels_custom_period": ("Поиск каналов", "ввод своего периода"),
    "parse_menu": ("Старый сбор", "устаревший режим"),
    "waiting_parse_channel_numbers": ("Старый сбор", "выбор каналов"),
    "waiting_parse_period": ("Старый сбор", "выбор периода"),
    "waiting_custom_period": ("Старый сбор", "ввод своего периода"),
}


def get_state_status_meta(state: str) -> tuple[str, str]:
    return STATE_STATUS_MAP.get(state or "menu", ("Неизвестный режим", "неизвестный шаг"))


def format_context_status(context: dict | None) -> str:
    context = context or {}
    parts: list[str] = []

    selected_channels = context.get("subscription_selected_user_channels") or context.get("digest_selected_channels") or []
    if selected_channels:
        parts.append(f"выбрано каналов: {len(selected_channels)}")

    selected_ids = context.get("subscription_selected_user_channel_ids") or []
    if selected_ids and not selected_channels:
        parts.append(f"выбрано каналов: {len(selected_ids)}")

    selected_subscription_id = context.get("selected_subscription_id")
    if selected_subscription_id:
        parts.append(f"выбранная автосводка: #{selected_subscription_id}")

    subscriptions = context.get("subscriptions_for_action") or []
    if subscriptions:
        parts.append(f"автосводок в списке: {len(subscriptions)}")

    history_items = context.get("digest_history_items") or []
    if history_items:
        parts.append(f"сводок в истории на экране: {len(history_items)}")

    found_channels = context.get("found_channels") or []
    if found_channels:
        parts.append(f"найдено каналов: {len(found_channels)}")

    candidate_channels = (
        context.get("subscription_candidate_channels")
        or context.get("digest_candidate_channels")
        or context.get("delete_candidate_channels")
        or []
    )
    if candidate_channels:
        parts.append(f"каналов в текущем списке: {len(candidate_channels)}")

    pending_channel = context.get("pending_user_channel") or {}
    if pending_channel:
        username = pending_channel.get("username") or pending_channel.get("input") or "канал проверен"
        parts.append(f"проверенный канал: {username}")

    return "; ".join(parts) if parts else "нет временных данных"


def build_user_status_text(user_id: int, state: str, context: dict | None, *, busy: bool) -> str:
    mode, step = get_state_status_meta(state)
    busy_text = "идёт операция, дождитесь ответа" if busy else "нет"
    context_text = format_context_status(context)

    return (
        "📍 Статус\n"
        f"Режим: {mode}\n"
        f"Шаг: {step}\n"
        f"Операция: {busy_text}\n"
        f"Контекст: {context_text}\n\n"
        "Застрял? Напиши /reset."
    )


def format_admin_datetime(value) -> str:
    return format_datetime_local(value, DIGEST_SUBSCRIPTION_DEFAULT_TIMEZONE or DEFAULT_TIMEZONE, include_timezone=True)


def trim_admin_text(value, limit: int = 900) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def format_admin_error_list(errors: list[dict]) -> str:
    if not errors:
        return "❌ Последние ошибки\n\nЖурнал ошибок пуст. Красота, аж подозрительно."

    text = "❌ Последние ошибки\n\n"
    for item in errors:
        error_id = item.get("id")
        created_at = format_admin_datetime(item.get("created_at"))
        place = item.get("place") or "unknown"
        error_type = item.get("error_type") or "Error"
        error_text = trim_admin_text(item.get("error_text"), 450)
        telegram_id = item.get("telegram_id")
        username = item.get("username") or item.get("first_name") or ""
        user_part = f"tg={telegram_id}" if telegram_id else "tg=?"
        if username:
            user_part += f" / {username}"

        text += (
            f"#{error_id} — {created_at}\n"
            f"Место: {place}\n"
            f"Пользователь: {user_part}\n"
            f"Ошибка: {error_type}: {error_text}\n\n"
        )

    text += "Полный traceback хранится в bot_errors.traceback_text."
    return text.strip()


def format_admin_system_status(stats: dict, error_stats: dict) -> str:
    by_place = error_stats.get("by_place_24h") or []
    place_text = ""
    if by_place:
        place_text = "\nОшибки за 24ч по местам:\n"
        for item in by_place:
            place_text += f"• {item.get('place')}: {item.get('count')}\n"

    return (
        "📊 Статус системы\n\n"
        f"Пользователей: {stats.get('users_count', 0)}\n"
        f"Админов: {stats.get('admin_users_count', 0)}\n"
        f"Активных личных каналов: {stats.get('active_user_channels', 0)}\n"
        f"Сообщений в БД: {stats.get('messages_count', 0)}\n\n"
        f"Автосводок всего: {stats.get('digest_subscriptions_total', 0)}\n"
        f"Активных автосводок: {stats.get('digest_subscriptions_active', 0)}\n"
        f"Готовы к запуску: {stats.get('digest_subscriptions_due', 0)}\n"
        f"Запусков running: {stats.get('digest_runs_running', 0)}\n"
        f"Failed/interrupted runs: {stats.get('digest_runs_failed_or_interrupted', 0)}\n\n"
        f"Ошибок за 1ч: {error_stats.get('errors_1h', 0)}\n"
        f"Ошибок за 24ч: {error_stats.get('errors_24h', 0)}\n"
        f"Нерешённых ошибок: {error_stats.get('unresolved_errors', 0)}\n"
        f"Последняя ошибка: {format_admin_datetime(error_stats.get('last_error_at'))}\n"
        f"Время БД: {format_admin_datetime(stats.get('db_now'))}"
        f"{place_text}"
    ).strip()


def format_admin_autodigest_status(stats: dict) -> str:
    text = (
        "🔔 Статус автосводок\n\n"
        f"Всего подписок: {stats.get('total_count', 0)}\n"
        f"Активных: {stats.get('active_count', 0)}\n"
        f"Отключённых: {stats.get('inactive_count', 0)}\n"
        f"Готовы к запуску сейчас: {stats.get('due_count', 0)}\n"
        f"Под lock: {stats.get('locked_count', 0)}\n"
        f"С последней ошибкой: {stats.get('subscriptions_with_errors', 0)}\n"
        f"Ближайший запуск: {format_admin_datetime(stats.get('nearest_next_run'))}\n\n"
        f"Runs running: {stats.get('running_runs', 0)}\n"
        f"Runs success: {stats.get('success_runs', 0)}\n"
        f"Runs failed/interrupted: {stats.get('failed_runs', 0)}\n"
        f"Последний run: {format_admin_datetime(stats.get('last_run_at'))}"
    )

    failed = stats.get("recent_failed_runs") or []
    if failed:
        text += "\n\nПоследние failed/interrupted:\n"
        for item in failed:
            text += (
                f"• run #{item.get('id')} / sub #{item.get('subscription_id')} / "
                f"tg={item.get('telegram_id')} / {item.get('status')} — "
                f"{trim_admin_text(item.get('error_text'), 180)}\n"
            )

    return text.strip()


def prepare_found_channel_context(context: dict) -> None:
    """Создаёт настройки выбора каналов после поиска, если их ещё нет."""
    found_channels = (context.get("found_channels") or [])[:MAX_FOUND_CHANNELS_FOR_USER_CHOICE]
    max_count = max(1, len(found_channels)) if found_channels else 1
    default_count = min(DEFAULT_FOUND_CHANNEL_COUNT, max_count)

    context["found_channels"] = found_channels
    context.setdefault("found_channel_count", default_count)
    context["found_channel_count"] = clamp_int(
        context.get("found_channel_count"),
        1,
        max_count,
        default_count,
    )
    context.setdefault("found_channel_autofill", True)

    if "selected_found_channel_indexes" not in context:
        context["selected_found_channel_indexes"] = list(range(context["found_channel_count"]))
        context["found_manual_selection"] = False


def parse_found_channel_numbers(text: str, max_number: int) -> list[int] | None:
    parts = [part for part in re.split(r"[\s,;]+", (text or "").strip()) if part]

    if not parts:
        return None

    indexes: list[int] = []

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


def parse_digest_channel_numbers(text: str, max_number: int, max_selected: int = MAX_DIGEST_CHANNELS) -> list[int] | None:
    parts = [part for part in re.split(r"[\s,;]+", (text or "").strip()) if part]

    if not parts or len(parts) > max_selected:
        return None

    indexes: list[int] = []

    for part in parts:
        if not part.isdigit():
            return None

        number = int(part)
        if number < 1 or number > max_number:
            return None

        index = number - 1
        if index not in indexes:
            indexes.append(index)

    if not indexes or len(indexes) > max_selected:
        return None

    return indexes


def format_digest_channels_for_pick(channels: list[dict], title: str = "Выбери каналы") -> str:
    if not channels:
        return "Каналов нет. Добавь канал."

    lines = [f"📍 {title}", "Шаг: напиши номера каналов.", ""]
    current_category = None
    for index, channel in enumerate(channels, start=1):
        category = channel.get("user_category") or "другое"
        if category != current_category:
            current_category = category
            lines.append(f"📁 {category}")
        username = normalize_username_for_display(channel.get("username"))
        title_value = channel.get("title") or username
        lines.append(f"{index}. {username} — {title_value}")

    lines.extend(["", f"Можно до {MAX_DIGEST_CHANNELS}. Пример: 1 2 5"])
    return "\n".join(lines).strip()

def normalize_digest_channels_for_collect(channels: list[dict]) -> list[dict]:
    return [normalize_user_channel_for_collect(channel) for channel in channels]


def format_digest_subscription_datetime(value, timezone_name: str | None = None) -> str:
    return format_datetime_local(
        value,
        timezone_name or DIGEST_SUBSCRIPTION_DEFAULT_TIMEZONE or DEFAULT_TIMEZONE,
        include_timezone=True,
    )




def format_digest_history_period(item: dict) -> str:
    period_from = format_digest_subscription_datetime(item.get("period_from"))
    period_to = format_digest_subscription_datetime(item.get("period_to"))
    return f"{period_from} → {period_to}"


def format_digest_history_list(items: list[dict], total_count: int | None = None) -> str:
    if not items:
        return "📍 История сводок\nСводок пока нет."

    lines = ["📍 История сводок", "Шаг: напиши номер сводки."]
    if total_count is not None:
        lines.append(f"Всего: {total_count}. Показано: {len(items)}.")
    lines.append("")
    for index, item in enumerate(items, start=1):
        finished = format_digest_subscription_datetime(item.get("finished_at") or item.get("created_at"))
        channels_count = int(item.get("channels_count") or 0)
        messages_count = int(item.get("messages_count") or 0)
        lines.append(f"{index}. #{item.get('id')} — {finished}")
        lines.append(f"   Каналов: {channels_count}; сообщений: {messages_count}")
    return "\n".join(lines).strip()

def format_digest_history_item_header(item: dict) -> str:
    finished = format_digest_subscription_datetime(item.get("finished_at") or item.get("created_at"))
    channels_count = int(item.get("channels_count") or 0)
    messages_count = int(item.get("messages_count") or 0)
    return (
        f"📍 Сводка #{item.get('id')}\n"
        f"Создана: {finished}\n"
        f"Каналов: {channels_count}; сообщений: {messages_count}\n"
    )

def parse_history_number(text: str | None, max_number: int) -> int | None:
    value = (text or "").strip()
    if not value.isdigit():
        return None
    number = int(value)
    if number < 1 or number > max_number:
        return None
    return number - 1


def default_subscription_send_time() -> str:
    return f"{int(DIGEST_SUBSCRIPTION_DEFAULT_HOUR):02d}:{int(DIGEST_SUBSCRIPTION_DEFAULT_MINUTE):02d}"


def parse_subscription_timezone_choice(text: str | None) -> str | None:
    raw = (text or "").strip()
    if raw == "✍️ Свой timezone":
        return "custom"
    return validate_timezone_name(raw)


def parse_subscription_time_choice(text: str | None) -> str | None:
    raw = (text or "").strip()
    if raw == "✍️ Своё время":
        return "custom"
    try:
        return format_send_time(raw)
    except Exception:
        return None


def format_timezone_examples() -> str:
    return "Пример: Asia/Tbilisi"


def format_time_examples() -> str:
    return "Формат: HH:MM. Пример: 09:00"


def format_digest_subscriptions_list(subscriptions: list[dict], title: str = "Твои автосводки") -> str:
    if not subscriptions:
        return "Автосводок пока нет."

    lines = [f"📍 {title}", "Шаг: напиши номер.", ""]
    for index, item in enumerate(subscriptions, start=1):
        status = "активна" if item.get("is_active") else "отключена"
        period_days = int(item.get("period_days") or 7)
        send_time = format_send_time(item.get("send_time") or default_subscription_send_time())
        channels_count = int(item.get("channels_count") or 0)
        preset_title = get_digest_preset_title(item.get("digest_preset"))
        title_value = item.get("title") or "Автосводка"
        lines.append(f"{index}. #{item['id']} — {title_value}")
        lines.append(f"   {status}; {channels_count} канал(ов); раз в {period_days} дн.; {send_time}")
        lines.append(f"   Формат: {preset_title}")
        if item.get("last_error_text"):
            lines.append(f"   Ошибка: {str(item['last_error_text'])[:120]}")

    return "\n".join(lines).strip()

def parse_subscription_period_days(text: str | None) -> int | None:
    text = (text or "").strip()
    if text == "🔁 Раз в 3 дня":
        return 3
    if text == "🔁 Раз в неделю":
        return 7
    return None


DIGEST_PRESET_TITLES = {
    "brief": "⚡ Только главное",
    "normal": "🧾 Обычная сводка",
    "detailed": "📚 Подробная сводка",
}

DIGEST_PRESET_DESCRIPTIONS = {
    "brief": "коротко: только самые важные события",
    "normal": "сбалансированно: главное + кратко по каналам",
    "detailed": "подробнее: больше контекста, второстепенные пункты и ограничения",
}


def normalize_digest_preset(value: str | None) -> str:
    preset = (value or "normal").strip().lower()
    return preset if preset in DIGEST_PRESET_TITLES else "normal"


def get_digest_preset_title(value: str | None) -> str:
    return DIGEST_PRESET_TITLES.get(normalize_digest_preset(value), DIGEST_PRESET_TITLES["normal"])


def get_digest_preset_description(value: str | None) -> str:
    return DIGEST_PRESET_DESCRIPTIONS.get(normalize_digest_preset(value), DIGEST_PRESET_DESCRIPTIONS["normal"])


def parse_digest_preset_choice(text: str | None) -> str | None:
    raw = (text or "").strip()
    mapping = {
        "⚡ Только главное": "brief",
        "🧾 Обычная сводка": "normal",
        "📚 Подробная сводка": "detailed",
        "brief": "brief",
        "normal": "normal",
        "detailed": "detailed",
    }
    return mapping.get(raw)


def format_digest_preset_help() -> str:
    return (
        "📍 Формат автосводки\n"
        "Шаг: выбери вариант.\n\n"
        "⚡ Только главное — коротко.\n"
        "🧾 Обычная — баланс.\n"
        "📚 Подробная — больше деталей."
    )

def parse_subscription_number(text: str | None, max_number: int) -> int | None:
    text = (text or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number < 1 or number > max_number:
        return None
    return number - 1


def normalize_subscription_channels_for_ui(raw_channels) -> list[dict]:
    """list_digest_subscriptions может вернуть jsonb как list или как JSON-строку.

    Для карточки и редактирования автосводки приводим всё к list[dict].
    """
    if raw_channels is None:
        return []

    if isinstance(raw_channels, str):
        text = raw_channels.strip()
        if not text:
            return []
        try:
            raw_channels = json.loads(text)
        except json.JSONDecodeError:
            return [
                {"id": None, "username": part.strip(), "title": part.strip(), "user_category": None}
                for part in text.replace("\n", ",").split(",")
                if part.strip()
            ]

    if isinstance(raw_channels, dict):
        raw_channels = [raw_channels]

    if not isinstance(raw_channels, list):
        return []

    result: list[dict] = []
    for item in raw_channels:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str) and item.strip():
            result.append({"id": None, "username": item.strip(), "title": item.strip(), "user_category": None})
    return result


def format_subscription_channels_edit_list(channels: list[dict], title: str) -> str:
    if not channels:
        return f"📍 {title}\nКаналов нет."

    lines = [f"📍 {title}", "Шаг: напиши номера.", ""]
    for index, channel in enumerate(channels, start=1):
        username = normalize_username_for_display(channel.get("username"))
        title_value = channel.get("title") or username
        category = channel.get("user_category") or "другое"
        lines.append(f"{index}. {username} — {title_value} | {category}")
    return "\n".join(lines).strip()

def format_digest_subscription_card(subscription: dict) -> str:
    if not subscription:
        return "Автосводка не найдена."

    status = "активна" if subscription.get("is_active") else "отключена"
    period_days = int(subscription.get("period_days") or 7)
    timezone_name = subscription.get("timezone") or DIGEST_SUBSCRIPTION_DEFAULT_TIMEZONE or DEFAULT_TIMEZONE
    send_time = format_send_time(subscription.get("send_time") or default_subscription_send_time())
    next_run = format_digest_subscription_datetime(subscription.get("next_run_at"), timezone_name)
    channels = normalize_subscription_channels_for_ui(subscription.get("channels"))
    preset_title = get_digest_preset_title(subscription.get("digest_preset"))

    lines = [
        f"📍 Автосводка #{subscription.get('id')}",
        f"Статус: {status}",
        f"Каналов: {subscription.get('channels_count', 0)}",
        f"Период: раз в {period_days} дн.",
        f"Формат: {preset_title}",
        f"Время: {send_time} ({timezone_name})",
        f"Следующий запуск: {next_run}",
    ]
    if subscription.get("last_error_text"):
        lines.append(f"Ошибка: {str(subscription['last_error_text'])[:160]}")
    lines.extend(["", "Что дальше: выбери кнопку ниже."])

    if channels:
        lines.extend(["", "Каналы:"])
        for index, channel in enumerate(channels[:8], start=1):
            username = normalize_username_for_display(channel.get("username"))
            title = channel.get("title") or username
            lines.append(f"{index}. {username} — {title}")
        if len(channels) > 8:
            lines.append(f"… ещё {len(channels) - 8}")

    return "\n".join(lines).strip()

async def finalize_digest_subscription_creation(message: Message, db_user: dict, send_time_text: str) -> None:
    user_id = message.from_user.id
    selected_ids = user_parse_context.get(user_id, {}).get("subscription_selected_user_channel_ids") or []
    selected_channels = user_parse_context.get(user_id, {}).get("subscription_selected_user_channels") or []
    period_days = user_parse_context.get(user_id, {}).get("subscription_period_days")
    timezone_name = user_parse_context.get(user_id, {}).get("subscription_timezone") or DIGEST_SUBSCRIPTION_DEFAULT_TIMEZONE or DEFAULT_TIMEZONE
    digest_preset = normalize_digest_preset(user_parse_context.get(user_id, {}).get("subscription_digest_preset"))

    if not selected_ids or not period_days:
        user_states[user_id] = "autodigest_menu"
        await message.answer(
            "Настройки автосводки потерялись. Создай автосводку заново.",
            reply_markup=await get_autodigest_keyboard_for_message(message),
        )
        return

    channels_preview = ", ".join(
        normalize_username_for_display(channel.get("username"))
        for channel in selected_channels[:3]
    )
    if len(selected_channels) > 3:
        channels_preview += f", ещё {len(selected_channels) - 3}"

    sub = await create_digest_subscription(
        user_id=db_user["id"],
        user_channel_ids=selected_ids,
        period_days=int(period_days),
        title=f"Автосводка: {channels_preview}" if channels_preview else None,
        digest_preset=digest_preset,
        send_time=send_time_text,
        timezone_name=timezone_name,
    )

    user_parse_context.pop(user_id, None)
    user_states[user_id] = "autodigest_menu"
    await message.answer(
        f"Автосводка создана: #{sub['id']}\n\n"
        f"Каналов: {len(selected_ids)}\n"
        f"Частота: раз в {int(period_days)} дн.\n"
        f"Формат: {get_digest_preset_title(sub.get('digest_preset') or digest_preset)}\n"
        f"Время: {format_send_time(sub.get('send_time'))}\n"
        f"Timezone: {sub.get('timezone') or timezone_name}\n"
        f"Следующий запуск: {format_digest_subscription_datetime(sub.get('next_run_at'), sub.get('timezone') or timezone_name)}\n\n"
        "Дальше: открой «⚙️ Управление автосводками».",
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


async def show_autodigest_management_list(message: Message, db_user: dict) -> None:
    user_id = message.from_user.id
    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=False)

    if not subscriptions:
        user_states[user_id] = "autodigest_menu"
        await message.answer(
            "Автосводок пока нет. Нажми «➕ Создать автосводку».",
            reply_markup=await get_autodigest_keyboard_for_message(message),
        )
        return

    user_parse_context[user_id] = {"subscriptions_for_manage": subscriptions}
    user_states[user_id] = "waiting_subscription_manage_number"
    await send_long_text(
        message,
        format_digest_subscriptions_list(subscriptions, title="Выбери автосводку для управления")
        + "\n\nНапиши номер.",
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


async def show_selected_subscription_card(message: Message, db_user: dict, subscription_id: int) -> None:
    user_id = message.from_user.id
    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=False)
    selected = next((item for item in subscriptions if int(item.get("id")) == int(subscription_id)), None)

    if not selected:
        user_states[user_id] = "autodigest_menu"
        await message.answer(
            "Автосводка не найдена.",
            reply_markup=await get_autodigest_keyboard_for_message(message),
        )
        return

    user_parse_context[user_id] = {
        "subscriptions_for_manage": subscriptions,
        "selected_subscription_id": int(subscription_id),
    }
    user_states[user_id] = "waiting_subscription_manage_action"
    await send_long_text(
        message,
        format_digest_subscription_card(selected),
        reply_markup=await get_subscription_manage_keyboard_for_message(message),
    )


def get_subscription_run_mode_from_text(text: str | None) -> str:
    raw = (text or "").strip()
    if raw.startswith("🧪"):
        return "debug"
    return "manual"


def format_subscription_run_mode_title(run_mode: str) -> str:
    return "Debug-проверка" if run_mode == "debug" else "Ручной запуск"


async def ask_subscription_run_progress_choice(
    message: Message,
    db_user: dict,
    subscription_id: int,
    run_mode: str,
) -> None:
    """Перед ручным/debug-запуском спрашиваем, двигать ли last_success_to."""
    user_id = message.from_user.id
    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=False)
    selected = next((item for item in subscriptions if int(item.get("id")) == int(subscription_id)), None)
    if not selected:
        user_states[user_id] = "autodigest_menu"
        await message.answer("Автосводка не найдена.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    user_parse_context[user_id] = {
        "subscriptions_for_manage": subscriptions,
        "selected_subscription_id": int(subscription_id),
        "pending_subscription_run_id": int(subscription_id),
        "pending_subscription_run_mode": run_mode,
    }
    user_states[user_id] = "waiting_subscription_run_update_choice"

    run_title = format_subscription_run_mode_title(run_mode)
    await message.answer(
        f"📍 {run_title} #{subscription_id}\n"
        "Шаг: обновить точку парса?\n\n"
        "✅ Да — настоящий запуск.\n"
        "👀 Нет — только посмотреть.",
        reply_markup=subscription_run_update_keyboard,
    )



async def run_subscription_ai_preview_for_user(message: Message, db_user: dict, subscription_id: int) -> None:
    """Показывает debug-предпросмотр сообщений, которые уйдут в ИИ.

    Это не вызывает ИИ и не двигает last_success_to/next_run_at. Lock нужен только чтобы
    preview не пересёкся с scheduler-ом или ручным запуском этой же подписки.
    """
    user_id = message.from_user.id
    locked_by = f"ai-preview:user:{db_user['id']}:subscription:{int(subscription_id)}"

    locked = await lock_digest_subscription_now(
        user_id=db_user["id"],
        subscription_id=int(subscription_id),
        locked_by=locked_by,
        lock_minutes=30,
    )
    if not locked:
        user_states[user_id] = "autodigest_menu"
        await message.answer(
            "Автосводка занята. Попробуй позже.",
            reply_markup=await get_autodigest_keyboard_for_message(message),
        )
        return

    set_user_busy(user_id)
    try:
        await message.answer(
            "Собираю предпросмотр. ИИ не вызываю.",
            reply_markup=await get_subscription_manage_keyboard_for_message(message),
        )
        result = await preview_digest_subscription_messages(
            subscription_id=int(subscription_id),
            locked_by=locked_by,
        )
    finally:
        clear_user_busy(user_id)

    user_parse_context.setdefault(user_id, {})["selected_subscription_id"] = int(subscription_id)
    user_states[user_id] = "waiting_subscription_manage_action"

    preview_text = result.get("preview_text")
    if preview_text:
        await send_long_text(message, preview_text, reply_markup=await get_subscription_manage_keyboard_for_message(message))
    else:
        await message.answer(
            "Предпросмотр не получился.\n\n"
            f"Статус: {result.get('status')}\n"
            f"Ошибка: {result.get('error')}",
            reply_markup=await get_subscription_manage_keyboard_for_message(message),
        )



async def show_digest_channels_for_pick(
    message: Message,
    user_id: int,
    channels: list[dict],
    title: str = "Выбери каналы для сводки",
) -> None:
    user_parse_context.setdefault(user_id, {})["digest_candidate_channels"] = channels
    user_states[user_id] = "waiting_digest_channel_numbers"

    await send_long_text(
        message,
        format_digest_channels_for_pick(channels, title=title),
        reply_markup=digest_channel_view_keyboard,
    )


async def run_digest_for_selected_channels(
    message: Message,
    db_user: dict,
    user_id: int,
    date_from: datetime,
    date_to: datetime,
    period_label: str,
) -> None:
    context = user_parse_context.get(user_id, {})
    selected_channels = context.get("selected_channels") or []
    digest_user_channel_ids = context.get("digest_user_channel_ids") or []

    if not selected_channels:
        user_states[user_id] = "channels_menu"
        await message.answer(
            "Выбор каналов потерялся. Начни заново.",
            reply_markup=channels_keyboard,
        )
        return

    channels_text = ", ".join(channel["username"] for channel in selected_channels)
    set_user_busy(user_id)
    try:
        await answer_progress(
            message,
            f"Собираю сводку.\n\nКаналы: {channels_text}\nПериод: {period_label}"
        )

        result = await collect_messages_from_channels(
            db_user_id=db_user["id"],
            selected_channels=selected_channels,
            date_from=date_from,
            date_to=date_to,
            max_messages_per_channel=DIGEST_COLLECT_MESSAGES_PER_CHANNEL,
        )

        if not result["ok"]:
            await message.answer(
                "Сбор завершился ошибкой.\n\n"
                f"Получено для пользователя до ошибки: {result['messages_for_user']}\n"
                f"Проверено новых через Telegram до ошибки: {result['messages_found']}\n"
                f"Сохранено новых до ошибки: {result['messages_saved']}\n"
                f"Взято из кэша до ошибки: {result['messages_from_cache']}\n"
                f"Ошибка: {result['error']}",
                reply_markup=channels_keyboard,
            )
            user_states[user_id] = "channels_menu"
            user_parse_context.pop(user_id, None)
            return

        await send_digest_report_or_explanation(
            message=message,
            selected_channels=selected_channels,
            result=result,
            period_label=period_label,
            db_user_id=db_user["id"],
            digest_user_channel_ids=digest_user_channel_ids,
            period_from=date_from,
            period_to=date_to,
        )

        await save_user_channel_digest_state(
            user_id=db_user["id"],
            user_channel_ids=digest_user_channel_ids,
            last_digest_at=date_to,
            last_message_date=None,
        )

        await message.answer(
            "Дайджест завершён.\n\n" + format_collect_result_stats(result),
            reply_markup=channels_keyboard,
        )

        user_states[user_id] = "channels_menu"
        user_parse_context.pop(user_id, None)
    finally:
        clear_user_busy(user_id)


def resolve_found_channel_indexes(context: dict) -> list[int]:
    """
    Возвращает финальные индексы каналов, которые будут прочитаны.

    Если добор включён и пользователь выбрал меньше нужного количества,
    недостающие каналы добираются сверху по рейтингу из топ-10.
    """
    prepare_found_channel_context(context)
    found_channels = context.get("found_channels") or []
    if not found_channels:
        return []

    max_count = len(found_channels)
    target_count = clamp_int(
        context.get("found_channel_count"),
        1,
        max_count,
        min(DEFAULT_FOUND_CHANNEL_COUNT, max_count),
    )
    autofill = bool(context.get("found_channel_autofill", True))

    raw_indexes = context.get("selected_found_channel_indexes") or []
    selected: list[int] = []

    for index in raw_indexes:
        try:
            index = int(index)
        except (TypeError, ValueError):
            continue

        if 0 <= index < max_count and index not in selected:
            selected.append(index)

    if not selected:
        selected = list(range(target_count))

    if autofill:
        for index in range(max_count):
            if len(selected) >= target_count:
                break
            if index not in selected:
                selected.append(index)

        return selected[:target_count]

    return selected[:target_count]


def get_selected_found_channels_for_collect(context: dict) -> list[dict]:
    found_channels = context.get("found_channels") or []
    indexes = resolve_found_channel_indexes(context)
    selected = [found_channels[index] for index in indexes if 0 <= index < len(found_channels)]
    selected_channels = convert_found_channels_for_collect(selected, limit=None)

    existing_usernames = {
        normalize_username_for_display(channel.get("username")).lower()
        for channel in selected_channels
    }

    for channel in get_extra_user_channels_from_context(context):
        key = normalize_username_for_display(channel.get("username")).lower()
        if key and key not in existing_usernames:
            selected_channels.append(channel)
            existing_usernames.add(key)

    return selected_channels[:MAX_CHANNELS_PER_PARSE]



def normalize_username_for_display(username: str | None) -> str:
    username = (username or "").strip()
    if username and not username.startswith("@"):
        username = f"@{username}"
    return username


def normalize_category_from_text(text: str | None) -> str | None:
    value = (text or "").strip()
    if value in ГЛАВНЫЕ_КАТЕГОРИИ:
        return value
    return None


def category_from_view_button(text: str | None) -> str | None:
    value = (text or "").strip()
    match = re.match(r"^📁\s+(.+?)\s+\(\d+\)$", value)
    if match:
        return match.group(1).strip()
    return None


def format_user_channels_list(channels: list[dict], title: str = "Твои каналы") -> str:
    if not channels:
        return "У тебя пока нет добавленных каналов."

    text = f"{title}:\n\n"
    current_category = None

    for index, channel in enumerate(channels, start=1):
        category = channel.get("user_category") or "другое"
        if category != current_category:
            current_category = category
            text += f"\n📁 {category}\n"

        username = normalize_username_for_display(channel.get("username"))
        title_value = channel.get("title") or username
        text += f"{index}. {username} — {title_value}\n"

    return text.strip()


def format_user_channels_delete_list(channels: list[dict], title: str = "Выбери канал для удаления") -> str:
    if not channels:
        return "У тебя пока нет добавленных каналов."

    text = f"{title}:\n\n"
    current_category = None

    for index, channel in enumerate(channels, start=1):
        category = channel.get("user_category") or "другое"
        if category != current_category:
            current_category = category
            text += f"\n📁 {category}\n"

        username = normalize_username_for_display(channel.get("username"))
        title_value = channel.get("title") or username
        text += f"{index}. {username} — {title_value}\n"

    text += "\nНапиши номер канала, который нужно удалить. Например: 1"
    return text.strip()


def format_user_channel_subscription_usage(usage: list[dict]) -> str:
    if not usage:
        return "Канал не используется в автосводках."

    lines = [f"Канал используется в автосводках: {len(usage)}"]
    for item in usage[:10]:
        status = "активна" if item.get("is_active") else "отключена"
        title = item.get("title") or f"Автосводка #{item.get('id')}"
        period_days = int(item.get("period_days") or 0)
        next_run = format_digest_subscription_datetime(item.get("next_run_at"), item.get("timezone"))
        lines.append(f"• #{item.get('id')} — {title} ({status}, раз в {period_days} дн., следующий запуск: {next_run})")

    if len(usage) > 10:
        lines.append(f"• ...и ещё {len(usage) - 10}")

    return "\n".join(lines)


def normalize_user_channel_for_collect(channel: dict) -> dict:
    username = normalize_username_for_display(channel.get("username"))
    return {
        "id": channel.get("id"),
        "username": username,
        "title": channel.get("title") or username,
        "source": "user_channel",
        "user_category": channel.get("user_category"),
    }


def get_extra_user_channels_from_context(context: dict) -> list[dict]:
    return [
        normalize_user_channel_for_collect(channel)
        for channel in (context.get("selected_user_channels") or [])
        if normalize_username_for_display(channel.get("username"))
    ]


def format_found_channel_selection_status(context: dict) -> str:
    prepare_found_channel_context(context)
    found_channels = context.get("found_channels") or []

    if not found_channels:
        return "Подходящие каналы не найдены."

    final_indexes = set(resolve_found_channel_indexes(context))
    manual_indexes = set(context.get("selected_found_channel_indexes") or [])
    target_count = context.get("found_channel_count") or min(DEFAULT_FOUND_CHANNEL_COUNT, len(found_channels))
    autofill = bool(context.get("found_channel_autofill", True))
    final_count = len(final_indexes)

    text = "\n\nТекущий выбор каналов:\n"
    text += f"Будет прочитано: {final_count} из топ-{len(found_channels)}. Целевое количество: {target_count}.\n"
    text += f"Добор каналов: {'включён' if autofill else 'выключен'}.\n"

    if autofill:
        manual_count = len([index for index in manual_indexes if 0 <= int(index) < len(found_channels)])
        if manual_count < int(target_count):
            text += "Если выбранных вручную меньше нужного количества, бот доберёт остальные сверху по рейтингу.\n"
    else:
        text += "Бот возьмёт только вручную выбранные каналы.\n"

    text += "\nТоп каналов:\n"

    for index, item in enumerate(found_channels, start=1):
        zero_index = index - 1
        marker = "✅" if zero_index in final_indexes else "▫️"
        pinned = " 📌" if zero_index in manual_indexes else ""
        username = str(item.get("username") or "").strip()
        if username and not username.startswith("@"):
            username = f"@{username}"
        title = item.get("title") or username
        score = item.get("score", 0)
        text += f"{marker} {index}. {username} — {title} | score: {score}{pinned}\n"

    text += "\nЧто дальше: выбери кнопку ниже."
    return text


def get_state_fallback(state: str, context: dict | None = None) -> tuple[str, object]:
    """
    Возвращает понятную подсказку и правильную клавиатуру для текущего состояния.

    Главная идея: при неправильном тексте не сбрасываем пользователя в главное меню
    и не оставляем его без кнопок. FSM-состояние остаётся прежним.
    """
    context = context or {}

    if state == "menu":
        return (
            "Не понял команду. Выбери действие кнопкой ниже.",
            main_keyboard,
        )

    if state == "settings":
        return (
            "Не понял команду. В разделе помощи доступны кнопки ниже.",
            settings_keyboard,
        )

    if state == "admin_menu":
        return (
            "Не понял команду. В админке доступны кнопки ниже.",
            admin_keyboard,
        )

    if state == "waiting_vacancy_channel_query":
        return (
            "📍 Парс вакансий\nШаг 1/2: напиши запрос для поиска каналов.",
            admin_keyboard,
        )

    if state == "waiting_vacancy_keywords":
        return (
            "📍 Парс вакансий\nШаг 2/2: напиши ключевики через запятую.",
            admin_keyboard,
        )

    if state == "channels_menu":
        return (
            "Не понял команду. В разделе «Мои каналы» можно просмотреть каналы или добавить новый.",
            channels_keyboard,
        )

    if state == "user_channels_view_menu":
        return (
            "Не понял команду. Выбери, как показать личные каналы: по категориям или все сразу.",
            channel_view_keyboard,
        )

    if state == "waiting_channel":
        return (
            "Жду ссылку или username Telegram-канала. Например: @channel или https://t.me/channel.",
            channels_keyboard,
        )

    if state == "waiting_user_channel_category":
        return (
            "Жду выбор категории для проверенного канала. Нажми категорию кнопкой или «📚 Ещё категории».",
            build_user_channel_category_keyboard(full=False),
        )

    if state == "waiting_user_channel_category_full":
        return (
            "Жду выбор категории из полного списка. Нажми одну из кнопок ниже.",
            build_user_channel_category_keyboard(full=True),
        )

    if state == "waiting_user_channel_category_view":
        categories = context.get("user_channel_categories") or []
        if categories:
            return (
                "Жду выбор категории из списка ниже. Также можно показать все каналы.",
                build_user_channel_category_view_keyboard(categories),
            )
        return (
            "Жду выбор категории или действие просмотра.",
            channel_view_keyboard,
        )


    if state == "autodigest_menu":
        return (
            "Не понял команду. В автосводках можно посмотреть подписки, создать новую, открыть управление или запустить debug-проверку.",
            autodigest_keyboard,
        )

    if state == "waiting_subscription_channel_numbers":
        return (
            f"Жду номера каналов для автосводки. Можно выбрать до {MAX_DIGEST_CHANNELS}. Например: 1 2 5.",
            autodigest_keyboard,
        )

    if state == "waiting_subscription_period":
        return (
            "Жду период автосводки: раз в 3 дня или раз в неделю.",
            subscription_period_keyboard,
        )

    if state == "waiting_subscription_preset":
        return (
            "Жду формат автосводки: «⚡ Только главное», «🧾 Обычная сводка» или «📚 Подробная сводка».",
            subscription_digest_preset_keyboard,
        )

    if state == "waiting_subscription_timezone":
        return (
            "Жду timezone автосводки. Выбери кнопку или нажми «✍️ Свой timezone».",
            subscription_timezone_keyboard,
        )

    if state == "waiting_subscription_custom_timezone":
        return (
            "Жду timezone текстом. " + format_timezone_examples(),
            subscription_timezone_keyboard,
        )

    if state == "waiting_subscription_time":
        return (
            "Жду время отправки автосводки. Выбери кнопку или нажми «✍️ Своё время».",
            subscription_time_keyboard,
        )

    if state == "waiting_subscription_custom_time":
        return (
            "Жду время отправки текстом. " + format_time_examples(),
            subscription_time_keyboard,
        )

    if state == "waiting_subscription_disable_number":
        return (
            "Жду номер автосводки, которую нужно отключить.",
            autodigest_keyboard,
        )


    if state == "waiting_subscription_preview_number":
        return (
            "Жду номер автосводки для предпросмотра сообщений, которые уйдут в ИИ.",
            autodigest_keyboard,
        )

    if state == "waiting_subscription_run_number":
        run_mode = context.get("pending_subscription_run_mode") or "manual"
        title = "debug-проверки" if run_mode == "debug" else "ручного запуска"
        return (
            f"Жду номер автосводки для {title} прямо сейчас.",
            autodigest_keyboard,
        )

    if state == "waiting_subscription_run_update_choice":
        return (
            "Жду выбор: обновить точку последнего парса или просто посмотреть сводку.",
            subscription_run_update_keyboard,
        )

    if state == "waiting_subscription_manage_number":
        return (
            "Жду номер автосводки для управления. Напиши номер из списка.",
            autodigest_keyboard,
        )

    if state == "waiting_subscription_manage_action":
        return (
            "Жду действие с выбранной автосводкой: включить, отключить, добавить/убрать канал, изменить период, запустить сейчас или удалить.",
            subscription_manage_keyboard,
        )

    if state == "waiting_subscription_add_channel_numbers":
        return (
            "Жду номера личных каналов, которые нужно добавить в выбранную автосводку.",
            subscription_manage_keyboard,
        )

    if state == "waiting_subscription_remove_channel_numbers":
        return (
            "Жду номера каналов, которые нужно убрать из выбранной автосводки.",
            subscription_manage_keyboard,
        )

    if state == "waiting_subscription_change_period":
        return (
            "Жду новый период автосводки: раз в 3 дня или раз в неделю.",
            subscription_period_keyboard,
        )

    if state == "waiting_subscription_change_preset":
        return (
            "Жду новый формат автосводки: кратко, обычно или подробно.",
            subscription_digest_preset_keyboard,
        )

    if state == "waiting_subscription_change_time":
        return (
            "Жду новое время отправки. Выбери кнопку или нажми «✍️ Своё время».",
            subscription_time_keyboard,
        )

    if state == "waiting_subscription_change_custom_time":
        return (
            "Жду время текстом. " + format_time_examples(),
            subscription_time_keyboard,
        )

    if state == "waiting_subscription_change_timezone":
        return (
            "Жду новый timezone. Выбери кнопку или нажми «✍️ Свой timezone».",
            subscription_timezone_keyboard,
        )

    if state == "waiting_subscription_change_custom_timezone":
        return (
            "Жду timezone текстом. " + format_timezone_examples(),
            subscription_timezone_keyboard,
        )

    if state == "waiting_subscription_delete_confirm":
        return (
            "Подтверди удаление автосводки или нажми «↩️ Нет, оставить».",
            subscription_delete_confirm_keyboard,
        )

    if state == "waiting_digest_history_number":
        return (
            "Жду номер старой сводки из истории. Можно нажать «🔄 Обновить историю» или «⬅️ Назад».",
            digest_history_keyboard,
        )

    if state == "digest_channel_view_menu":
        return (
            "Не понял команду. Выбери, как показать каналы для сводки: по категориям или все сразу.",
            digest_channel_view_keyboard,
        )

    if state == "waiting_digest_category_view":
        categories = context.get("user_channel_categories") or []
        if categories:
            return (
                "Жду выбор категории для сводки. Нажми категорию кнопкой или покажи все каналы.",
                build_user_channel_category_view_keyboard(categories),
            )
        return (
            "Жду выбор категории для сводки или действие просмотра.",
            digest_channel_view_keyboard,
        )

    if state == "waiting_digest_channel_numbers":
        return (
            f"Жду номера каналов для сводки. Можно выбрать до {MAX_DIGEST_CHANNELS}. Например: 1 2 5 8 10.",
            digest_channel_view_keyboard,
        )

    if state == "waiting_digest_period":
        return (
            "Жду выбор периода для дайджеста: день, неделя, месяц, свой период или с прошлого дайджеста.",
            digest_period_keyboard,
        )

    if state == "waiting_digest_custom_period":
        return (
            "Жду две даты для периода дайджеста. Например: 01.05.2026 20.05.2026.",
            digest_period_keyboard,
        )

    if state == "waiting_channel_search_query":
        return (
            SHORT_QUERY_HINT_TEXT,
            main_keyboard,
        )

    if state == "waiting_found_channels_action":
        status_text = format_found_channel_selection_status(context) if context.get("found_channels") else ""
        return (
            "Не понял команду. Настрой выбор каналов кнопками ниже." + status_text,
            found_channels_keyboard,
        )

    if state == "waiting_found_channel_count":
        return (
            "Жду число каналов от 1 до 10. Можно нажать кнопку с числом ниже.",
            found_channels_count_keyboard,
        )

    if state == "waiting_found_channel_list":
        status_text = format_found_channel_selection_status(context) if context.get("found_channels") else ""
        return (
            "Жду номера каналов из топа. Например: 1 3 7." + status_text,
            found_channels_keyboard,
        )

    if state == "waiting_found_report_preset":
        return (
            "Жду выбор типа ИИ-отчёта. Нажми один из пресетов ниже.",
            report_preset_keyboard,
        )

    if state == "waiting_found_channels_period":
        return (
            "Жду выбор периода кнопкой: за день, за неделю, за месяц или свой период.",
            period_keyboard,
        )

    if state == "waiting_found_channels_custom_period":
        return (
            "Жду две даты для своего периода. Например: 01.05.2026 20.05.2026.",
            period_keyboard,
        )

                                                                         
    if state == "parse_menu":
        return (
            "Старый отдельный сбор из личных каналов убран. Используй «📋 Мои каналы» или общий поиск.",
            channels_keyboard,
        )

    if state == "waiting_parse_channel_numbers":
        return (
            "Жду номера каналов из списка. Например: 1 или 1 2 3.",
            channels_keyboard,
        )

    if state == "waiting_parse_period":
        return (
            "Жду выбор периода кнопкой: за день, за неделю, за месяц или свой период.",
            period_keyboard,
        )

    if state == "waiting_custom_period":
        return (
            "Жду две даты для своего периода. Например: 01.05.2026 20.05.2026.",
            period_keyboard,
        )

    if state == "waiting_delete_channel_number":
        return (
            "Жду номер канала для удаления. Например: 1.",
            channels_keyboard,
        )

    if state == "waiting_delete_channel_confirm":
        return (
            "Подтверди удаление канала или нажми «↩️ Нет, оставить».",
            user_channel_delete_confirm_keyboard,
        )

    return (
        "Не понял команду. Выбери действие кнопкой ниже.",
        main_keyboard,
    )


async def answer_state_fallback(message: Message, state: str, context: dict | None = None) -> None:
    text, keyboard = get_state_fallback(state, context=context)
    await send_long_text(message, text, reply_markup=keyboard)


async def show_main_menu(message: Message, *, clear_context: bool = True) -> None:
    user_id = message.from_user.id
    user_states[user_id] = "menu"
    if clear_context:
        user_parse_context.pop(user_id, None)

    await message.answer(
        "Главное меню. Бот может подобрать каналы по теме или открыть личный каталог каналов.",
        reply_markup=main_keyboard,
    )


async def show_channels_root_menu(message: Message, db_user: dict, *, clear_context: bool = True) -> None:
    user_id = message.from_user.id
    user_states[user_id] = "channels_menu"
    if clear_context:
        user_parse_context.pop(user_id, None)

    channels_count = await count_user_channels(db_user["id"])
    await message.answer(
        "📍 Мои каналы\n"
        f"Добавлено: {channels_count}/{MAX_USER_CHANNELS_PER_ACCOUNT}.\n\n"
        "Что дальше: выбери кнопку.",
        reply_markup=channels_keyboard,
    )


async def show_digest_history_menu(message: Message, db_user: dict, *, refresh: bool = True) -> None:
    user_id = message.from_user.id
    user_states[user_id] = "waiting_digest_history_number"

    if refresh or not user_parse_context.get(user_id, {}).get("digest_history_items"):
        items = await list_user_digest_history(db_user["id"], limit=10)
        total_count = await count_user_digest_history(db_user["id"])
        user_parse_context[user_id] = {
            **user_parse_context.get(user_id, {}),
            "digest_history_items": items,
            "digest_history_total_count": total_count,
        }
    else:
        items = user_parse_context.get(user_id, {}).get("digest_history_items") or []
        total_count = user_parse_context.get(user_id, {}).get("digest_history_total_count")

    await send_long_text(
        message,
        format_digest_history_list(items, total_count=total_count),
        reply_markup=digest_history_keyboard,
    )


async def show_autodigest_root_menu(message: Message, db_user: dict, *, clear_context: bool = True) -> None:
    user_id = message.from_user.id
    user_states[user_id] = "autodigest_menu"
    if clear_context:
        user_parse_context.pop(user_id, None)

    stats = await get_digest_subscription_debug_stats(db_user["id"])
    nearest = format_digest_subscription_datetime(stats.get("nearest_next_run"))
    await message.answer(
        "📍 Автосводки\n"
        f"Активных: {stats.get('active_count', 0)}\n"
        f"Готовы сейчас: {stats.get('due_count', 0)}\n"
        f"Ошибок: {stats.get('error_count', 0)}\n"
        f"Ближайший запуск: {nearest}\n\n"
        "Что дальше: выбери кнопку.",
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


async def show_subscription_channel_pick_again(message: Message, user_id: int, *, title: str = "Выбери каналы для автосводки") -> None:
    channels = user_parse_context.get(user_id, {}).get("subscription_candidate_channels") or []
    if not channels:
        user_states[user_id] = "autodigest_menu"
        await message.answer(
            "Список каналов для автосводки потерялся. Начни создание заново.",
            reply_markup=await get_autodigest_keyboard_for_message(message),
        )
        return

    user_states[user_id] = "waiting_subscription_channel_numbers"
    await send_long_text(
        message,
        format_digest_channels_for_pick(channels, title=title)
        + "\n\nНапиши номера каналов через пробел или запятую.",
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


async def show_digest_channel_pick_again(message: Message, user_id: int, *, title: str = "Выбери каналы для сводки") -> None:
    channels = user_parse_context.get(user_id, {}).get("digest_candidate_channels") or []
    if not channels:
        user_states[user_id] = "digest_channel_view_menu"
        await message.answer(
            "Список каналов потерялся. Выбери заново.",
            reply_markup=digest_channel_view_keyboard,
        )
        return

    user_states[user_id] = "waiting_digest_channel_numbers"
    await send_long_text(
        message,
        format_digest_channels_for_pick(channels, title=title),
        reply_markup=digest_channel_view_keyboard,
    )


async def show_found_channels_action_again(message: Message, user_id: int, prefix: str = "") -> None:
    context = user_parse_context.get(user_id, {})
    if not context.get("found_channels"):
        user_states[user_id] = "waiting_channel_search_query"
        await message.answer(
            "Список потерялся. Напиши запрос заново.",
            reply_markup=main_keyboard,
        )
        return

    user_states[user_id] = "waiting_found_channels_action"
    await send_long_text(
        message,
        (prefix or "Вернулся к настройке выбранных каналов.")
        + format_found_channel_selection_status(context),
        reply_markup=found_channels_keyboard,
    )


async def handle_back_navigation(message: Message, db_user: dict) -> None:
    """Один шаг назад по текущему FSM-состоянию.

    Раньше «⬅️ Назад» всегда сбрасывал в главное меню. Это ломало вложенные меню:
    автосводки → управление → карточка → изменение времени. Теперь каждый state
    возвращается на свой предыдущий экран и по возможности сохраняет контекст.
    """
    user_id = message.from_user.id
    state = user_states.get(user_id, "menu")
    context = user_parse_context.get(user_id, {})

    if state in {"menu", "settings", "waiting_channel_search_query"}:
        await show_main_menu(message)
        return

    if state == "admin_menu":
        await show_main_menu(message)
        return

    if state in {"waiting_vacancy_channel_query", "waiting_vacancy_keywords"}:
        await show_admin_menu(message)
        return

    if state == "channels_menu":
        await show_main_menu(message)
        return

    if state in {"user_channels_view_menu", "waiting_channel", "waiting_delete_channel_number"}:
        await show_channels_root_menu(message, db_user)
        return

    if state == "waiting_delete_channel_confirm":
        channels = context.get("delete_candidate_channels") or await get_user_channels(db_user["id"])
        user_parse_context[user_id] = {"delete_candidate_channels": channels}
        user_states[user_id] = "waiting_delete_channel_number"
        await send_long_text(
            message,
            format_user_channels_delete_list(channels, title="Выбери канал для удаления"),
            reply_markup=channels_keyboard,
        )
        return

    if state == "waiting_user_channel_category_full":
        user_states[user_id] = "waiting_user_channel_category"
        await message.answer(
            "Вернулся к короткому списку категорий.",
            reply_markup=build_user_channel_category_keyboard(full=False),
        )
        return

    if state == "waiting_user_channel_category":
                                                                                           
        context.pop("pending_user_channel", None)
        user_parse_context[user_id] = context
        user_states[user_id] = "waiting_channel"
        await message.answer(
            "Вернулся к добавлению канала. Отправь @username или ссылку t.me/...",
            reply_markup=channels_keyboard,
        )
        return

    if state == "waiting_user_channel_category_view":
        user_states[user_id] = "user_channels_view_menu"
        await message.answer("Как показать личные каналы?", reply_markup=channel_view_keyboard)
        return

    if state == "digest_channel_view_menu":
        await show_channels_root_menu(message, db_user)
        return

    if state == "waiting_digest_history_number":
        await show_channels_root_menu(message, db_user)
        return

    if state == "waiting_digest_category_view":
        user_states[user_id] = "digest_channel_view_menu"
        await message.answer("Как показать каналы для сводки?", reply_markup=digest_channel_view_keyboard)
        return

    if state == "waiting_digest_channel_numbers":
        user_states[user_id] = "digest_channel_view_menu"
        await message.answer("Назад: выбор списка каналов.", reply_markup=digest_channel_view_keyboard)
        return

    if state == "waiting_digest_period":
        await show_digest_channel_pick_again(message, user_id, title="Выбери каналы для сводки заново")
        return

    if state == "waiting_digest_custom_period":
        user_states[user_id] = "waiting_digest_period"
        await message.answer("Назад: выбор периода.", reply_markup=digest_period_keyboard)
        return

    if state == "autodigest_menu":
        await show_channels_root_menu(message, db_user)
        return

    if state in {"waiting_subscription_disable_number", "waiting_subscription_run_number", "waiting_subscription_preview_number", "waiting_subscription_manage_number"}:
        await show_autodigest_root_menu(message, db_user)
        return

    if state == "waiting_subscription_run_update_choice":
        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if subscription_id:
            await show_selected_subscription_card(message, db_user, int(subscription_id))
        else:
            await show_autodigest_root_menu(message, db_user)
        return

    if state == "waiting_subscription_channel_numbers":
        await show_autodigest_root_menu(message, db_user)
        return

    if state == "waiting_subscription_period":
        await show_subscription_channel_pick_again(message, user_id, title="Выбери каналы для автосводки заново")
        return

    if state == "waiting_subscription_preset":
        user_states[user_id] = "waiting_subscription_period"
        await message.answer("Назад: выбор частоты.", reply_markup=subscription_period_keyboard)
        return

    if state == "waiting_subscription_timezone":
        user_states[user_id] = "waiting_subscription_preset"
        await message.answer("Назад: выбор формата.", reply_markup=subscription_digest_preset_keyboard)
        return

    if state == "waiting_subscription_custom_timezone":
        user_states[user_id] = "waiting_subscription_timezone"
        await message.answer("Назад: выбор timezone.", reply_markup=subscription_timezone_keyboard)
        return

    if state == "waiting_subscription_time":
        user_states[user_id] = "waiting_subscription_timezone"
        await message.answer("Назад: выбор timezone.", reply_markup=subscription_timezone_keyboard)
        return

    if state == "waiting_subscription_custom_time":
        user_states[user_id] = "waiting_subscription_time"
        await message.answer("Назад: выбор времени.", reply_markup=subscription_time_keyboard)
        return

    if state == "waiting_subscription_manage_action":
        await show_autodigest_management_list(message, db_user)
        return

    if state in {
        "waiting_subscription_change_period",
        "waiting_subscription_change_preset",
        "waiting_subscription_change_time",
        "waiting_subscription_change_timezone",
        "waiting_subscription_delete_confirm",
        "waiting_subscription_add_channel_numbers",
        "waiting_subscription_remove_channel_numbers",
    }:
        subscription_id = context.get("selected_subscription_id")
        if subscription_id:
            await show_selected_subscription_card(message, db_user, int(subscription_id))
        else:
            await show_autodigest_management_list(message, db_user)
        return

    if state == "waiting_subscription_change_custom_time":
        user_states[user_id] = "waiting_subscription_change_time"
        await message.answer("Назад: выбор времени.", reply_markup=subscription_time_keyboard)
        return

    if state == "waiting_subscription_change_custom_timezone":
        user_states[user_id] = "waiting_subscription_change_timezone"
        await message.answer("Назад: выбор timezone.", reply_markup=subscription_timezone_keyboard)
        return

    if state == "waiting_found_channels_action":
        user_states[user_id] = "waiting_channel_search_query"
        user_parse_context[user_id] = {}
        await message.answer(
            "Назад: напиши тему.",
            reply_markup=main_keyboard,
        )
        return

    if state in {"waiting_found_channel_count", "waiting_found_channel_list", "waiting_found_report_preset"}:
        await show_found_channels_action_again(message, user_id)
        return

    if state == "waiting_found_channels_period":
        user_states[user_id] = "waiting_found_report_preset"
        await message.answer(
            "Вернулся к выбору типа ИИ-отчёта.",
            reply_markup=report_preset_keyboard,
        )
        return

    if state == "waiting_found_channels_custom_period":
        user_states[user_id] = "waiting_found_channels_period"
        await message.answer("Назад: выбор периода.", reply_markup=period_keyboard)
        return

    if state in {"parse_menu", "waiting_parse_channel_numbers"}:
        await show_channels_root_menu(message, db_user)
        return

    if state == "waiting_parse_period":
        user_states[user_id] = "waiting_parse_channel_numbers"
        await message.answer("Назад: выбор каналов.", reply_markup=parse_keyboard)
        return

    if state == "waiting_custom_period":
        user_states[user_id] = "waiting_parse_period"
        await message.answer("Назад: выбор периода.", reply_markup=period_keyboard)
        return

    await show_main_menu(message)


@dp.message(CommandStart())
async def start_handler(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    user_states[user_id] = "menu"
    user_parse_context.pop(user_id, None)
    clear_user_busy(user_id)

    await message.answer(
        BOT_INTRO_TEXT,
        reply_markup=main_keyboard,
    )




async def show_admin_menu(message: Message) -> None:
    user_id = message.from_user.id
    user_states[user_id] = "admin_menu"
    user_parse_context.pop(user_id, None)
    await message.answer(
        "📍 Админка\nЧто дальше: выбери действие.",
        reply_markup=admin_keyboard,
    )


@dp.message(Command("admin"))
async def admin_command(message: Message):
    await register_user(message)
    if not await require_admin(message):
        return
    await show_admin_menu(message)


@dp.message(F.text == "🛠 Админка")
async def admin_menu_button(message: Message):
    await register_user(message)
    if not await require_admin(message):
        return
    await show_admin_menu(message)


@dp.message(F.text == "❌ Последние ошибки")
async def admin_recent_errors(message: Message):
    await register_user(message)
    if not await require_admin(message):
        return

    user_states[message.from_user.id] = "admin_menu"
    errors = await list_recent_bot_errors(limit=ADMIN_ERRORS_LIMIT, include_resolved=True)
    await send_long_text(
        message,
        format_admin_error_list(errors),
        reply_markup=admin_keyboard,
    )


@dp.message(F.text == "📊 Статус системы")
async def admin_system_status(message: Message):
    await register_user(message)
    if not await require_admin(message):
        return

    user_states[message.from_user.id] = "admin_menu"
    stats = await get_admin_system_stats()
    error_stats = await get_bot_error_stats()
    await send_long_text(
        message,
        format_admin_system_status(stats, error_stats),
        reply_markup=admin_keyboard,
    )


@dp.message(F.text == "🔔 Статус автосводок")
async def admin_autodigest_status(message: Message):
    await register_user(message)
    if not await require_admin(message):
        return

    user_states[message.from_user.id] = "admin_menu"
    stats = await get_admin_autodigest_stats()
    await send_long_text(
        message,
        format_admin_autodigest_status(stats),
        reply_markup=admin_keyboard,
    )


@dp.message(F.text == "💼 Парс вакансий")
async def admin_vacancy_parse_start(message: Message):
    await register_user(message)
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    user_states[user_id] = "waiting_vacancy_channel_query"
    user_parse_context[user_id] = {}
    await message.answer(
        "📍 Парс вакансий\n"
        "Шаг 1/2: найду каналы.\n\n"
        "Напиши запрос.\n"
        "Пример: вакансии python удалёнка",
        reply_markup=admin_keyboard,
    )


@dp.message(Command("help"))
async def help_handler(message: Message):
    await register_user(message)

    await message.answer(
        QUERY_GUIDE_TEXT,
        reply_markup=main_keyboard,
    )


@dp.message(Command("commands"))
async def commands_handler(message: Message):
    await register_user(message)

    await message.answer(
        "Команды:\n"
        "/status — где я сейчас\n"
        "/reset — выйти в меню\n"
        "/stop — убрать кнопки\n"
        "/restart — открыть меню",
        reply_markup=main_keyboard,
    )


@dp.message(Command("status"))
async def status_handler(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    state = user_states.get(user_id, "menu")
    context = user_parse_context.get(user_id, {})
    busy = is_user_busy(user_id)
    text = build_user_status_text(user_id, state, context, busy=busy)

    if state == "stopped":
        await message.answer(text, reply_markup=ReplyKeyboardRemove())
        return

    _, keyboard = get_state_fallback(state, context=context)
    await send_long_text(message, text, reply_markup=keyboard)


@dp.message(Command("reset"))
async def reset_handler(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    user_states[user_id] = "menu"
    user_parse_context.pop(user_id, None)
    clear_user_busy(user_id)

    await message.answer(
        "✅ Сброшено.\nТы в главном меню.",
        reply_markup=main_keyboard,
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

    user_id = message.from_user.id
    user_states[user_id] = "menu"
    user_parse_context.pop(user_id, None)
    clear_user_busy(user_id)

    await message.answer(
        "Главное меню. Выбери действие.",
        reply_markup=main_keyboard,
    )


@dp.message(F.text.in_({"📋 Мои каналы", "📋 Каналы"}))
async def channels_menu(message: Message):
    db_user = await register_user(message)

    user_id = message.from_user.id
    user_states[user_id] = "channels_menu"
    user_parse_context.pop(user_id, None)

    channels_count = await count_user_channels(db_user["id"])

    await message.answer(
        "📍 Мои каналы\n"
        f"Добавлено: {channels_count}/{MAX_USER_CHANNELS_PER_ACCOUNT}.\n\n"
        "Что дальше: выбери кнопку.",
        reply_markup=channels_keyboard,
    )


@dp.message(F.text == "👁 Просмотреть каналы")
async def view_user_channels_menu(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "user_channels_view_menu"

    await message.answer(
        "Как показать личные каналы?",
        reply_markup=channel_view_keyboard,
    )





@dp.message(F.text == "🗑 Удалить канал")
async def delete_user_channel_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    channels = await get_user_channels(db_user["id"])
    if not channels:
        user_states[user_id] = "channels_menu"
        await message.answer(
            "Удалять пока нечего: у тебя нет активных личных каналов.",
            reply_markup=channels_keyboard,
        )
        return

    user_states[user_id] = "waiting_delete_channel_number"
    user_parse_context[user_id] = {"delete_candidate_channels": channels}
    await send_long_text(
        message,
        format_user_channels_delete_list(channels, title="Выбери канал для удаления"),
        reply_markup=channels_keyboard,
    )


@dp.message(F.text.in_({"🕘 История сводок", "🔄 Обновить историю"}))
async def digest_history_start(message: Message):
    db_user = await register_user(message)
    await show_digest_history_menu(message, db_user, refresh=True)


@dp.message(F.text == "🔔 Автосводки")
async def autodigest_root(message: Message):
    db_user = await register_user(message)
    await show_autodigest_root_menu(message, db_user)


@dp.message(F.text == "📋 Мои автосводки")
async def autodigest_list(message: Message):
    db_user = await register_user(message)
    user_states[message.from_user.id] = "autodigest_menu"

    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=False)
    await send_long_text(
        message,
        format_digest_subscriptions_list(subscriptions, title="Мои автосводки"),
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


@dp.message(F.text == "🧪 Debug: статус scheduler")
async def autodigest_scheduler_debug_status(message: Message):
    db_user = await register_user(message)
    if not await require_admin(message):
        return
    user_states[message.from_user.id] = "autodigest_menu"

    stats = await get_digest_subscription_debug_stats(db_user["id"])
    nearest = format_digest_subscription_datetime(stats.get("nearest_next_run"))
    last_run = format_digest_subscription_datetime(stats.get("last_run_at"))
    await message.answer(
        "📍 Debug scheduler\n"
        f"Активных: {stats.get('active_count', 0)}\n"
        f"Готовы сейчас: {stats.get('due_count', 0)}\n"
        f"Под lock: {stats.get('locked_count', 0)}\n"
        f"Ошибок: {stats.get('error_count', 0)}\n"
        f"Ближайший запуск: {nearest}\n"
        f"Последний run: {last_run}",
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


@dp.message(F.text.in_({"⚙️ Управление автосводками", "📋 К списку автосводок"}))
async def autodigest_manage_start(message: Message):
    db_user = await register_user(message)
    await show_autodigest_management_list(message, db_user)


@dp.message(F.text == "➕ Создать автосводку")
async def autodigest_create_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id
    channels = await get_user_channels(db_user["id"])

    if not channels:
        user_states[user_id] = "autodigest_menu"
        await message.answer(
            "У тебя пока нет личных каналов. Сначала добавь каналы в разделе «📋 Мои каналы». Дальше по ним можно будет создать автосводку.",
            reply_markup=await get_autodigest_keyboard_for_message(message),
        )
        return

    user_parse_context[user_id] = {"subscription_candidate_channels": channels}
    user_states[user_id] = "waiting_subscription_channel_numbers"

    await send_long_text(
        message,
        format_digest_channels_for_pick(
            channels,
            title="Выбери каналы для автосводки"
        ) + "\n\nПосле выбора бот спросит частоту, формат сводки, timezone и время отправки.",
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


@dp.message(F.text == "⏸ Отключить автосводку")
async def autodigest_disable_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) == "waiting_subscription_manage_action":
        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        disabled = await disable_digest_subscription(db_user["id"], int(subscription_id))
        if disabled:
            await message.answer("Автосводка отключена. Она останется в списке, но больше не будет запускаться по расписанию.")
        else:
            await message.answer("Не получилось отключить автосводку. Возможно, она уже отключена или удалена.")
        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=True)

    if not subscriptions:
        user_states[user_id] = "autodigest_menu"
        await message.answer("Активных автосводок нет.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    user_parse_context[user_id] = {"subscriptions_for_action": subscriptions}
    user_states[user_id] = "waiting_subscription_disable_number"
    await send_long_text(
        message,
        format_digest_subscriptions_list(subscriptions, title="Какую автосводку отключить?")
        + "\n\nНапиши номер из списка.",
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


@dp.message(F.text == "▶️ Включить автосводку")
async def autodigest_enable_selected(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) != "waiting_subscription_manage_action":
        await message.answer("Сначала выбери автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if not subscription_id:
        await show_autodigest_management_list(message, db_user)
        return

    enabled = await enable_digest_subscription(db_user["id"], int(subscription_id))
    if enabled:
        await message.answer("Автосводка включена. Следующий запуск назначен заново.")
    else:
        await message.answer("Не получилось включить автосводку. Возможно, она удалена.")
    await show_selected_subscription_card(message, db_user, int(subscription_id))


@dp.message(F.text == "➕ Добавить канал в автосводку")
async def autodigest_add_channel_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) != "waiting_subscription_manage_action":
        await message.answer("Сначала выбери автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if not subscription_id:
        await show_autodigest_management_list(message, db_user)
        return

    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=False)
    selected = next((item for item in subscriptions if int(item.get("id")) == int(subscription_id)), None)
    if not selected:
        await show_autodigest_management_list(message, db_user)
        return

    current_channels = normalize_subscription_channels_for_ui(selected.get("channels"))
    current_ids = {
        int(channel.get("id"))
        for channel in current_channels
        if channel.get("id") is not None
    }
    current_count = len(current_ids)

    if current_count >= MAX_DIGEST_CHANNELS:
        await message.answer(
            f"В этой автосводке уже {current_count}/{MAX_DIGEST_CHANNELS} каналов. "
            "Сначала убери лишний канал, потом можно добавить новый.",
            reply_markup=await get_subscription_manage_keyboard_for_message(message),
        )
        return

    all_channels = await get_user_channels(db_user["id"])
    candidates = [
        channel
        for channel in all_channels
        if channel.get("id") is not None and int(channel["id"]) not in current_ids
    ]

    if not candidates:
        await message.answer(
            "Добавлять нечего: все активные личные каналы уже есть в этой автосводке.",
            reply_markup=await get_subscription_manage_keyboard_for_message(message),
        )
        return

    slots_left = MAX_DIGEST_CHANNELS - current_count
    user_parse_context[user_id] = {
        **user_parse_context.get(user_id, {}),
        "selected_subscription_id": int(subscription_id),
        "subscription_add_candidate_channels": candidates,
        "subscription_add_slots_left": slots_left,
    }
    user_states[user_id] = "waiting_subscription_add_channel_numbers"

    await send_long_text(
        message,
        format_digest_channels_for_pick(candidates, title="Какие каналы добавить в автосводку?")
        + f"\n\nСвободных мест в автосводке: {slots_left}. Напиши номера каналов, например: 1 3",
        reply_markup=await get_subscription_manage_keyboard_for_message(message),
    )


@dp.message(F.text == "➖ Убрать канал из автосводки")
async def autodigest_remove_channel_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) != "waiting_subscription_manage_action":
        await message.answer("Сначала выбери автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if not subscription_id:
        await show_autodigest_management_list(message, db_user)
        return

    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=False)
    selected = next((item for item in subscriptions if int(item.get("id")) == int(subscription_id)), None)
    if not selected:
        await show_autodigest_management_list(message, db_user)
        return

    current_channels = [
        channel
        for channel in normalize_subscription_channels_for_ui(selected.get("channels"))
        if channel.get("id") is not None
    ]

    if len(current_channels) <= 1:
        await message.answer(
            "В автосводке должен остаться хотя бы один канал.",
            reply_markup=await get_subscription_manage_keyboard_for_message(message),
        )
        return

    user_parse_context[user_id] = {
        **user_parse_context.get(user_id, {}),
        "selected_subscription_id": int(subscription_id),
        "subscription_remove_candidate_channels": current_channels,
    }
    user_states[user_id] = "waiting_subscription_remove_channel_numbers"

    await send_long_text(
        message,
        format_subscription_channels_edit_list(current_channels, title="Какие каналы убрать из автосводки?")
        + "\n\nНапиши номера каналов. Последний канал убрать нельзя, иначе автосводка станет пустой.",
        reply_markup=await get_subscription_manage_keyboard_for_message(message),
    )


@dp.message(F.text == "🔁 Изменить период")
async def autodigest_change_period_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) != "waiting_subscription_manage_action":
        await message.answer("Сначала выбери автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if not subscription_id:
        await show_autodigest_management_list(message, db_user)
        return

    user_states[user_id] = "waiting_subscription_change_period"
    await message.answer(
        "Выбери новый период автосводки. История прошлого успешного парса сохранится, поменяется только частота и следующий запуск.",
        reply_markup=subscription_period_keyboard,
    )


@dp.message(F.text == "🧾 Изменить пресет")
async def autodigest_change_preset_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) != "waiting_subscription_manage_action":
        await message.answer("Сначала выбери автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if not subscription_id:
        await show_autodigest_management_list(message, db_user)
        return

    user_states[user_id] = "waiting_subscription_change_preset"
    await message.answer(
        "Выбери новый формат автосводки. Он будет применяться со следующего запуска.\n\n"
        + format_digest_preset_help(),
        reply_markup=subscription_digest_preset_keyboard,
    )


@dp.message(F.text == "🕘 Изменить время")
async def autodigest_change_time_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) != "waiting_subscription_manage_action":
        await message.answer("Сначала выбери автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if not subscription_id:
        await show_autodigest_management_list(message, db_user)
        return

    user_states[user_id] = "waiting_subscription_change_time"
    await message.answer(
        "Выбери новое локальное время отправки или нажми «✍️ Своё время».\n\n"
        + format_time_examples(),
        reply_markup=subscription_time_keyboard,
    )


@dp.message(F.text == "🌍 Изменить timezone")
async def autodigest_change_timezone_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) != "waiting_subscription_manage_action":
        await message.answer("Сначала выбери автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if not subscription_id:
        await show_autodigest_management_list(message, db_user)
        return

    user_states[user_id] = "waiting_subscription_change_timezone"
    await message.answer(
        "Выбери новый timezone или нажми «✍️ Свой timezone».\n\n"
        + format_timezone_examples(),
        reply_markup=subscription_timezone_keyboard,
    )


@dp.message(F.text == "🗑 Удалить автосводку")
async def autodigest_delete_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id

    if user_states.get(user_id) != "waiting_subscription_manage_action":
        await message.answer("Сначала выбери автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if not subscription_id:
        await show_autodigest_management_list(message, db_user)
        return

    user_states[user_id] = "waiting_subscription_delete_confirm"
    await message.answer(
        f"Удалить автосводку #{subscription_id}?\n\n"
        "Это удалит саму подписку и историю её запусков. Если нужно просто поставить на паузу — лучше нажать «⏸ Отключить автосводку».",
        reply_markup=subscription_delete_confirm_keyboard,
    )


@dp.message(F.text == "↩️ Нет, оставить")
async def autodigest_delete_cancel(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id
    subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
    if subscription_id:
        await show_selected_subscription_card(message, db_user, int(subscription_id))
    else:
        await show_autodigest_management_list(message, db_user)


@dp.message(F.text.in_({"🧪 Debug: проверить сейчас", "🧪 Debug: запустить сейчас", "🧪 Запустить сейчас", "▶️ Запустить сейчас"}))
async def autodigest_run_now_start(message: Message):
    db_user = await register_user(message)
    user_id = message.from_user.id
    run_mode = get_subscription_run_mode_from_text(message.text)

    if run_mode == "debug" and not await require_admin(message):
        return

    if user_states.get(user_id) == "waiting_subscription_manage_action":
        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return
        await ask_subscription_run_progress_choice(message, db_user, int(subscription_id), run_mode)
        return

    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=True)

    if not subscriptions:
        user_states[user_id] = "autodigest_menu"
        await message.answer("Активных автосводок нет. Создай автосводку.", reply_markup=await get_autodigest_keyboard_for_message(message))
        return

    user_parse_context[user_id] = {
        "subscriptions_for_action": subscriptions,
        "pending_subscription_run_mode": run_mode,
    }
    user_states[user_id] = "waiting_subscription_run_number"
    title = (
        "Debug: какую автосводку проверить сейчас?"
        if run_mode == "debug"
        else "Какую автосводку запустить сейчас?"
    )
    await send_long_text(
        message,
        format_digest_subscriptions_list(subscriptions, title=title)
        + "\n\nНапиши номер из списка. После этого бот спросит, обновлять ли точку прошлого парса.",
        reply_markup=await get_autodigest_keyboard_for_message(message),
    )


@dp.message(F.text == "🧾 Сделать сводку")
async def digest_start(message: Message):
    db_user = await register_user(message)

    user_id = message.from_user.id
    channels = await get_user_channels(db_user["id"])

    if not channels:
        user_states[user_id] = "channels_menu"
        await message.answer(
            "Каналов пока нет. Нажми «➕ Добавить канал».",
            reply_markup=channels_keyboard,
        )
        return

    user_states[user_id] = "digest_channel_view_menu"
    user_parse_context[user_id] = {}

    await message.answer(
        f"🧾 Сводка по личным каналам\n\n"
        f"Можно выбрать до {MAX_DIGEST_CHANNELS} каналов. Как показать список?",
        reply_markup=digest_channel_view_keyboard,
    )


@dp.message(F.text == "📋 Все каналы")
async def digest_show_all_channels(message: Message):
    db_user = await register_user(message)

    user_id = message.from_user.id
    if user_states.get(user_id) not in {"digest_channel_view_menu", "waiting_digest_category_view", "waiting_digest_channel_numbers"}:
        await message.answer(
            "Эта кнопка относится к режиму сводки. Открой «📋 Мои каналы» → «🧾 Сделать сводку».",
            reply_markup=channels_keyboard,
        )
        return

    channels = await get_user_channels(db_user["id"])
    await show_digest_channels_for_pick(
        message=message,
        user_id=user_id,
        channels=channels,
        title="Все личные каналы для сводки",
    )


@dp.message(F.text == "📋 Показать все каналы")
async def show_all_user_channels(message: Message):
    db_user = await register_user(message)

    channels = await get_user_channels(db_user["id"])
    await send_long_text(
        message,
        format_user_channels_list(channels, title="Все личные каналы"),
        reply_markup=channel_view_keyboard,
    )


@dp.message(F.text == "🗂 По категориям")
async def show_user_channel_categories(message: Message):
    db_user = await register_user(message)

    categories = await get_user_channel_categories(db_user["id"])

    if not categories:
        await message.answer(
            "У тебя пока нет добавленных каналов.",
            reply_markup=channels_keyboard,
        )
        return

    user_id = message.from_user.id
    current_state = user_states.get(user_id)

    if current_state == "digest_channel_view_menu":
        user_states[user_id] = "waiting_digest_category_view"
    else:
        user_states[user_id] = "waiting_user_channel_category_view"

    user_parse_context.setdefault(user_id, {})["user_channel_categories"] = categories

    text = "Категории твоих каналов:\n\n"
    for item in categories:
        text += f"📁 {item['user_category']} — {item['channels_count']}\n"

    await message.answer(
        text + "\nВыбери категорию кнопкой.",
        reply_markup=build_user_channel_category_view_keyboard(categories),
    )


@dp.message(F.text.in_({"➕ Добавить канал", "➕ Добавить"}))
async def add_channel_start(message: Message):
    db_user = await register_user(message)

    user_id = message.from_user.id

    if user_states.get(user_id) == "waiting_found_channels_action":
        await add_user_channels_to_found_selection(message, db_user)
        return

    channels_count = await count_user_channels(db_user["id"])
    if channels_count >= MAX_USER_CHANNELS_PER_ACCOUNT:
        await message.answer(
            f"У тебя уже {MAX_USER_CHANNELS_PER_ACCOUNT} каналов. Это максимум для личного списка.\n\n"
            "Чтобы добавить новый канал, сначала удали лишний.",
            reply_markup=channels_keyboard,
        )
        return

    user_states[user_id] = "waiting_channel"
    user_parse_context[user_id] = {}

    await message.answer(
        "Отправь username или ссылку на канал.\n\n"
        "Например:\n"
        "@channel\n"
        "https://t.me/channel\n\n"
        "После проверки доступа я попрошу выбрать категорию.",
        reply_markup=channels_keyboard,
    )


async def add_user_channels_to_found_selection(message: Message, db_user: dict) -> None:
    """Добавляет личные каналы подходящей категории к текущей поисковой выдаче."""
    user_id = message.from_user.id
    context = user_parse_context.get(user_id, {})
    query_markup = context.get("query_markup") or {}

    query_category = query_markup.get("category")
    additional_categories = query_markup.get("additional_categories") or []

    candidates = await get_user_channels_for_query(
        user_id=db_user["id"],
        query_category=query_category,
        additional_categories=additional_categories,
        limit=MAX_USER_CHANNELS_PER_ACCOUNT,
    )

    if not candidates:
        category_text = query_category if query_category and query_category != "неясно" else "нужной категории"
        await message.answer(
            f"В личном списке нет каналов категории «{category_text}».\n\n"
            "Добавить новый личный канал можно через главное меню → 📋 Мои каналы → ➕ Добавить канал.",
            reply_markup=found_channels_keyboard,
        )
        return

    existing = {
        normalize_username_for_display(channel.get("username")).lower()
        for channel in (context.get("selected_user_channels") or [])
    }
    existing.update(
        normalize_username_for_display(channel.get("username")).lower()
        for channel in (context.get("found_channels") or [])
    )

    selected_user_channels = list(context.get("selected_user_channels") or [])
    added = []

    for channel in candidates:
        key = normalize_username_for_display(channel.get("username")).lower()
        if not key or key in existing:
            continue

        if len(resolve_found_channel_indexes(context)) + len(selected_user_channels) >= MAX_CHANNELS_PER_PARSE:
            break

        selected_user_channels.append(channel)
        added.append(channel)
        existing.add(key)

    context["selected_user_channels"] = selected_user_channels
    user_parse_context[user_id] = context

    if not added:
        await message.answer(
            "Подходящие личные каналы уже добавлены или достигнут лимит каналов на один сбор.",
            reply_markup=found_channels_keyboard,
        )
        return

    text = f"Добавил личные каналы к текущему сбору: {len(added)}\n\n"
    for index, channel in enumerate(added, start=1):
        username = normalize_username_for_display(channel.get("username"))
        title = channel.get("title") or username
        category = channel.get("user_category") or "другое"
        text += f"{index}. {username} — {title} | {category}\n"

    await send_long_text(
        message,
        text + format_found_channel_selection_status(context),
        reply_markup=found_channels_keyboard,
    )


                                                                             
@dp.message(F.text.in_({"📥 Собрать из моих каналов", "🔎 Собрать"}))
async def old_parse_menu_disabled(message: Message):
    await register_user(message)

    user_states[message.from_user.id] = "channels_menu"

    await message.answer(
        "Этот режим убран.\n"
        "Открой «📋 Мои каналы» → «🧾 Сделать сводку».",
        reply_markup=channels_keyboard,
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


@dp.message(F.text.in_({"📥 Читать выбранные каналы", "📥 Читать посты из подобранных", "📥 Читать посты из найденных", "📥 Собрать из найденных"}))
async def collect_from_found_channels_start(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    context = user_parse_context.get(user_id, {})
    found_channels = context.get("found_channels") or []

    if not found_channels:
        user_states[user_id] = "waiting_channel_search_query"
        await message.answer(
            "Список потерялся. Напиши запрос заново.",
            reply_markup=main_keyboard,
        )
        return

    selected_channels = get_selected_found_channels_for_collect(context)

    if not selected_channels:
        user_states[user_id] = "waiting_found_channels_action"
        await message.answer(
            "Не выбрано ни одного канала. Нажми «☑️ Изменить список» и выбери номера из топа.",
            reply_markup=found_channels_keyboard,
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


@dp.message(F.text == "🔢 Изменить количество")
async def change_found_channel_count_start(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    if user_states.get(user_id) != "waiting_found_channels_action":
        await message.answer("Сначала подбери каналы.", reply_markup=main_keyboard)
        return

    context = user_parse_context.get(user_id, {})
    prepare_found_channel_context(context)
    user_parse_context[user_id] = context
    user_states[user_id] = "waiting_found_channel_count"

    await message.answer(
        "Сколько каналов читать? Выбери число от 1 до 10. Если найдено меньше 10, максимум будет по числу найденных каналов.",
        reply_markup=found_channels_count_keyboard,
    )


@dp.message(F.text == "☑️ Изменить список")
async def change_found_channel_list_start(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    if user_states.get(user_id) != "waiting_found_channels_action":
        await message.answer("Сначала подбери каналы.", reply_markup=main_keyboard)
        return

    context = user_parse_context.get(user_id, {})
    prepare_found_channel_context(context)
    user_parse_context[user_id] = context
    user_states[user_id] = "waiting_found_channel_list"

    await send_long_text(
        message,
        format_found_channel_selection_status(context)
        + "\n\nНапиши номера нужных каналов из топа. Например: 1 3 7",
        reply_markup=found_channels_keyboard,
    )


@dp.message(F.text == "🔄 Добор каналов")
async def toggle_found_channel_autofill(message: Message):
    await register_user(message)

    user_id = message.from_user.id
    if user_states.get(user_id) != "waiting_found_channels_action":
        await message.answer("Сначала подбери каналы.", reply_markup=main_keyboard)
        return

    context = user_parse_context.get(user_id, {})
    prepare_found_channel_context(context)
    context["found_channel_autofill"] = not bool(context.get("found_channel_autofill", True))
    user_parse_context[user_id] = context

    await send_long_text(
        message,
        "Настройка добора изменена." + format_found_channel_selection_status(context),
        reply_markup=found_channels_keyboard,
    )


@dp.message(F.text == "➕ Добавить свой канал")
async def add_own_channel_legacy_button(message: Message):
    """Совместимость со старой кнопкой из v2."""
    db_user = await register_user(message)

    user_id = message.from_user.id
    if user_states.get(user_id) != "waiting_found_channels_action":
        await message.answer("Сначала подбери каналы.", reply_markup=main_keyboard)
        return

    await add_user_channels_to_found_selection(message, db_user)


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
        "Команды:\n"
        "/status — где я сейчас\n"
        "/reset — выйти в меню\n"
        "/stop — убрать кнопки\n"
        "/restart — открыть меню"
    )


@dp.message(F.text == "🎯 Как писать запрос")
async def query_guide_button(message: Message):
    await register_user(message)

    await message.answer(
        QUERY_GUIDE_TEXT,
        reply_markup=settings_keyboard,
    )


@dp.message(F.text == "⬅️ Назад")
async def back_one_step(message: Message):
    db_user = await register_user(message)
    await handle_back_navigation(message, db_user)


@dp.message(F.text == "🧪 Что уйдёт в ИИ")
async def autodigest_ai_preview_start(message: Message):
    db_user = await register_user(message)
    if not await require_admin(message):
        return
    user_id = message.from_user.id

    if user_states.get(user_id) == "waiting_subscription_manage_action":
        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if subscription_id:
            await run_subscription_ai_preview_for_user(message, db_user, int(subscription_id))
            return

    subscriptions = await list_digest_subscriptions(db_user["id"], active_only=False)
    if not subscriptions:
        user_states[user_id] = "autodigest_menu"
        await message.answer(
            "Автосводок пока нет. Сначала создай автосводку, потом можно будет посмотреть, какие сообщения уйдут в ИИ.",
            reply_markup=await get_autodigest_keyboard_for_message(message),
        )
        return

    user_parse_context[user_id] = {"subscriptions_for_action": subscriptions}
    user_states[user_id] = "waiting_subscription_preview_number"
    await send_long_text(
        message,
        format_digest_subscriptions_list(subscriptions, title="Для какой автосводки показать сообщения для ИИ?")
        + "\n\nНапиши номер из списка. ИИ не будет вызван, расписание не изменится.",
        reply_markup=await get_autodigest_keyboard_for_message(message),
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

    if state == "waiting_vacancy_channel_query":
        if not await require_admin(message):
            return

        query = text.strip()
        if len(query) < 3:
            await message.answer(
                "📍 Парс вакансий\nШаг 1/2: напиши запрос для поиска каналов.\n\nПример: вакансии python удалёнка",
                reply_markup=admin_keyboard,
            )
            return

        set_user_busy(user_id)
        try:
            await message.answer("Ищу каналы…", reply_markup=ReplyKeyboardRemove())
            search_data = await find_vacancy_channels(query, limit=10)
        finally:
            clear_user_busy(user_id)

        channels = search_data.get("channels") or []
        if not channels:
            user_states[user_id] = "admin_menu"
            user_parse_context.pop(user_id, None)
            await message.answer(
                "📍 Парс вакансий\nКаналы не найдены.\n\nЧто дальше: попробуй другой запрос.",
                reply_markup=admin_keyboard,
            )
            return

        user_parse_context[user_id] = {
            "vacancy_query": query,
            "vacancy_channels": channels,
        }
        user_states[user_id] = "waiting_vacancy_keywords"
        await send_long_text(
            message,
            format_vacancy_channels_preview(channels, query),
            reply_markup=admin_keyboard,
        )
        return

    if state == "waiting_vacancy_keywords":
        if not await require_admin(message):
            return

        keywords = parse_vacancy_keywords(text)
        if not keywords:
            await message.answer(
                "📍 Парс вакансий\nШаг 2/2: напиши ключевики через запятую.\n\nПример: python, backend, remote",
                reply_markup=admin_keyboard,
            )
            return

        channels = user_parse_context.get(user_id, {}).get("vacancy_channels") or []
        if not channels:
            user_states[user_id] = "waiting_vacancy_channel_query"
            await message.answer(
                "Каналы потерялись. Напиши запрос заново.",
                reply_markup=admin_keyboard,
            )
            return

        set_user_busy(user_id)
        try:
            await message.answer(
                f"Паршу вакансии за {DEFAULT_VACANCY_DAYS} дня…",
                reply_markup=ReplyKeyboardRemove(),
            )
            result = await parse_vacancies_from_channels(
                db_user_id=db_user["id"],
                channels=channels,
                keywords=keywords,
                days=DEFAULT_VACANCY_DAYS,
            )
        finally:
            clear_user_busy(user_id)

        user_states[user_id] = "admin_menu"
        user_parse_context.pop(user_id, None)
        await send_long_text(
            message,
            format_vacancy_parse_result(result),
            reply_markup=admin_keyboard,
        )
        return

    if state == "waiting_digest_history_number":
        items = user_parse_context.get(user_id, {}).get("digest_history_items") or []
        index = parse_history_number(text, len(items))

        if index is None:
            await send_long_text(
                message,
                format_digest_history_list(
                    items,
                    total_count=user_parse_context.get(user_id, {}).get("digest_history_total_count"),
                )
                + "\n\nНапиши номер сводки.",
                reply_markup=digest_history_keyboard,
            )
            return

        digest_job_id = int(items[index]["id"])
        item = await get_user_digest_history_item(db_user["id"], digest_job_id)
        if not item:
            await message.answer(
                "Эта сводка не найдена. Возможно, она была удалена или принадлежит другому пользователю.",
                reply_markup=digest_history_keyboard,
            )
            await show_digest_history_menu(message, db_user, refresh=True)
            return

        output_text = format_digest_history_item_header(item)
        if item.get("error_text"):
            output_text += f"\nОшибка/ограничение: {str(item.get('error_text'))[:700]}\n"
        output_text += "\n" + (item.get("final_text") or "Текст сводки пуст.")

        await send_long_text(message, output_text, reply_markup=digest_history_keyboard)
        return

    if state == "waiting_subscription_channel_numbers":
        channels = user_parse_context.get(user_id, {}).get("subscription_candidate_channels") or []

        if not channels:
            user_states[user_id] = "autodigest_menu"
            await message.answer(
                "Список каналов для автосводки потерялся. Начни создание заново.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        indexes = parse_digest_channel_numbers(text, max_number=len(channels), max_selected=MAX_DIGEST_CHANNELS)
        if indexes is None:
            await send_long_text(
                message,
                f"Нужно написать номера каналов из списка. Можно выбрать до {MAX_DIGEST_CHANNELS}. Например: 1 2 5.\n\n"
                + format_digest_channels_for_pick(channels, title="Каналы для автосводки"),
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        selected_user_channels = [channels[index] for index in indexes]
        user_parse_context[user_id] = {
            **user_parse_context.get(user_id, {}),
            "subscription_selected_user_channel_ids": [int(channel["id"]) for channel in selected_user_channels],
            "subscription_selected_user_channels": selected_user_channels,
        }
        user_states[user_id] = "waiting_subscription_period"

        selected_text = "Выбраны каналы для автосводки:\n\n"
        for index, channel in enumerate(selected_user_channels, start=1):
            username = normalize_username_for_display(channel.get("username"))
            title = channel.get("title") or username
            selected_text += f"{index}. {username} — {title}\n"

        selected_text += "\nТеперь выбери частоту автосводки."
        await message.answer(selected_text, reply_markup=subscription_period_keyboard)
        return

    if state == "waiting_subscription_period":
        period_days = parse_subscription_period_days(text)
        if period_days is None:
            await message.answer(
                "Выбери частоту кнопкой: «🔁 Раз в 3 дня» или «🔁 Раз в неделю».",
                reply_markup=subscription_period_keyboard,
            )
            return

        selected_ids = user_parse_context.get(user_id, {}).get("subscription_selected_user_channel_ids") or []
        if not selected_ids:
            user_states[user_id] = "autodigest_menu"
            await message.answer(
                "Выбор каналов потерялся. Создай автосводку заново.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        user_parse_context[user_id] = {
            **user_parse_context.get(user_id, {}),
            "subscription_period_days": int(period_days),
        }
        user_states[user_id] = "waiting_subscription_preset"
        await message.answer(
            "Частота выбрана.\n\n" + format_digest_preset_help(),
            reply_markup=subscription_digest_preset_keyboard,
        )
        return

    if state == "waiting_subscription_preset":
        digest_preset = parse_digest_preset_choice(text)
        if not digest_preset:
            await message.answer(format_digest_preset_help(), reply_markup=subscription_digest_preset_keyboard)
            return

        user_parse_context[user_id] = {
            **user_parse_context.get(user_id, {}),
            "subscription_digest_preset": digest_preset,
        }
        user_states[user_id] = "waiting_subscription_timezone"
        await message.answer(
            f"Формат выбран: {get_digest_preset_title(digest_preset)}.\n\n"
            "Теперь выбери timezone автосводки. Это нужно, чтобы 09:00 означало именно твоё локальное 09:00.\n\n"
            + format_timezone_examples(),
            reply_markup=subscription_timezone_keyboard,
        )
        return

    if state == "waiting_subscription_timezone":
        timezone_choice = parse_subscription_timezone_choice(text)
        if timezone_choice == "custom":
            user_states[user_id] = "waiting_subscription_custom_timezone"
            await message.answer(format_timezone_examples(), reply_markup=subscription_timezone_keyboard)
            return
        if not timezone_choice:
            await message.answer(
                "Не понял timezone. Выбери кнопку или нажми «✍️ Свой timezone».\n\n" + format_timezone_examples(),
                reply_markup=subscription_timezone_keyboard,
            )
            return

        user_parse_context[user_id] = {
            **user_parse_context.get(user_id, {}),
            "subscription_timezone": timezone_choice,
        }
        user_states[user_id] = "waiting_subscription_time"
        await message.answer(
            f"Timezone выбран: {timezone_choice}.\n\nТеперь выбери локальное время отправки.",
            reply_markup=subscription_time_keyboard,
        )
        return

    if state == "waiting_subscription_custom_timezone":
        timezone_choice = validate_timezone_name(text)
        if not timezone_choice:
            await message.answer(
                "Не получилось распознать timezone. " + format_timezone_examples(),
                reply_markup=subscription_timezone_keyboard,
            )
            return

        user_parse_context[user_id] = {
            **user_parse_context.get(user_id, {}),
            "subscription_timezone": timezone_choice,
        }
        user_states[user_id] = "waiting_subscription_time"
        await message.answer(
            f"Timezone выбран: {timezone_choice}.\n\nТеперь выбери локальное время отправки.",
            reply_markup=subscription_time_keyboard,
        )
        return

    if state == "waiting_subscription_time":
        time_choice = parse_subscription_time_choice(text)
        if time_choice == "custom":
            user_states[user_id] = "waiting_subscription_custom_time"
            await message.answer(format_time_examples(), reply_markup=subscription_time_keyboard)
            return
        if not time_choice:
            await message.answer(
                "Не понял время. Выбери кнопку или нажми «✍️ Своё время».\n\n" + format_time_examples(),
                reply_markup=subscription_time_keyboard,
            )
            return

        await finalize_digest_subscription_creation(message, db_user, time_choice)
        return

    if state == "waiting_subscription_custom_time":
        try:
            time_choice = format_send_time(text)
        except Exception:
            await message.answer(
                "Не получилось распознать время. " + format_time_examples(),
                reply_markup=subscription_time_keyboard,
            )
            return

        await finalize_digest_subscription_creation(message, db_user, time_choice)
        return

    if state == "waiting_subscription_manage_number":
        subscriptions = user_parse_context.get(user_id, {}).get("subscriptions_for_manage") or []
        index = parse_subscription_number(text, len(subscriptions))
        if index is None:
            await send_long_text(
                message,
                format_digest_subscriptions_list(subscriptions, title="Выбери автосводку для управления")
                + "\n\nНапиши номер из списка.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        subscription_id = int(subscriptions[index]["id"])
        await show_selected_subscription_card(message, db_user, subscription_id)
        return

    if state == "waiting_subscription_add_channel_numbers":
        context = user_parse_context.get(user_id, {})
        subscription_id = context.get("selected_subscription_id")
        candidates = context.get("subscription_add_candidate_channels") or []
        slots_left = int(context.get("subscription_add_slots_left") or MAX_DIGEST_CHANNELS)

        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        if not candidates:
            await show_selected_subscription_card(message, db_user, int(subscription_id))
            return

        indexes = parse_digest_channel_numbers(text, max_number=len(candidates), max_selected=max(1, slots_left))
        if indexes is None:
            await send_long_text(
                message,
                f"Нужно написать номера каналов из списка. Свободных мест: {slots_left}. Например: 1 3.\n\n"
                + format_digest_channels_for_pick(candidates, title="Какие каналы добавить в автосводку?"),
                reply_markup=await get_subscription_manage_keyboard_for_message(message),
            )
            return

        selected_channels = [candidates[index] for index in indexes]
        selected_ids = [int(channel["id"]) for channel in selected_channels if channel.get("id") is not None]
        result = await add_user_channels_to_digest_subscription(
            user_id=db_user["id"],
            subscription_id=int(subscription_id),
            user_channel_ids=selected_ids,
        )

        if not result.get("ok"):
            await message.answer(
                "Не получилось добавить каналы в автосводку. Возможно, автосводка удалена или каналы уже неактивны.",
                reply_markup=await get_subscription_manage_keyboard_for_message(message),
            )
            await show_selected_subscription_card(message, db_user, int(subscription_id))
            return

        added = result.get("added_channels") or []
        if added:
            added_text = "\n".join(
                f"• {normalize_username_for_display(channel.get('username'))} — {channel.get('title') or normalize_username_for_display(channel.get('username'))}"
                for channel in added
            )
            await message.answer(
                f"Каналы добавлены в автосводку: {len(added)}.\n\n{added_text}",
                reply_markup=await get_subscription_manage_keyboard_for_message(message),
            )
        else:
            await message.answer(
                "Новых каналов не добавлено: похоже, они уже были в этой автосводке.",
                reply_markup=await get_subscription_manage_keyboard_for_message(message),
            )

        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    if state == "waiting_subscription_remove_channel_numbers":
        context = user_parse_context.get(user_id, {})
        subscription_id = context.get("selected_subscription_id")
        candidates = context.get("subscription_remove_candidate_channels") or []

        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        if not candidates:
            await show_selected_subscription_card(message, db_user, int(subscription_id))
            return

        indexes = parse_digest_channel_numbers(text, max_number=len(candidates), max_selected=len(candidates))
        if indexes is None:
            await send_long_text(
                message,
                "Нужно написать номера каналов из списка. Например: 1 3.\n\n"
                + format_subscription_channels_edit_list(candidates, title="Какие каналы убрать из автосводки?"),
                reply_markup=await get_subscription_manage_keyboard_for_message(message),
            )
            return

        if len(indexes) >= len(candidates):
            await message.answer(
                "Нельзя убрать все каналы из автосводки. Должен остаться хотя бы один канал. "
                "Выбери меньше каналов или удали автосводку целиком.",
                reply_markup=await get_subscription_manage_keyboard_for_message(message),
            )
            return

        selected_channels = [candidates[index] for index in indexes]
        selected_ids = [int(channel["id"]) for channel in selected_channels if channel.get("id") is not None]
        result = await remove_user_channels_from_digest_subscription(
            user_id=db_user["id"],
            subscription_id=int(subscription_id),
            user_channel_ids=selected_ids,
        )

        if not result.get("ok"):
            if result.get("reason") == "would_be_empty":
                await message.answer(
                    "Нельзя убрать все каналы из автосводки. Должен остаться хотя бы один канал.",
                    reply_markup=await get_subscription_manage_keyboard_for_message(message),
                )
            else:
                await message.answer(
                    "Не получилось убрать каналы из автосводки. Возможно, автосводка удалена или каналы уже изменились.",
                    reply_markup=await get_subscription_manage_keyboard_for_message(message),
                )
            await show_selected_subscription_card(message, db_user, int(subscription_id))
            return

        removed = result.get("removed_channels") or []
        if removed:
            removed_text = "\n".join(
                f"• {normalize_username_for_display(channel.get('username'))} — {channel.get('title') or normalize_username_for_display(channel.get('username'))}"
                for channel in removed
            )
            await message.answer(
                f"Каналы убраны из автосводки: {len(removed)}.\n\n{removed_text}",
                reply_markup=await get_subscription_manage_keyboard_for_message(message),
            )
        else:
            await message.answer(
                "Ничего не изменилось: выбранные каналы уже не были в этой автосводке.",
                reply_markup=await get_subscription_manage_keyboard_for_message(message),
            )

        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    if state == "waiting_subscription_change_period":
        period_days = parse_subscription_period_days(text)
        if period_days is None:
            await message.answer(
                "Выбери новый период кнопкой: «🔁 Раз в 3 дня» или «🔁 Раз в неделю».",
                reply_markup=subscription_period_keyboard,
            )
            return

        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        updated = await update_digest_subscription_period(
            user_id=db_user["id"],
            subscription_id=int(subscription_id),
            period_days=period_days,
        )
        if updated:
            await message.answer(f"Период автосводки изменён: раз в {period_days} дн.")
        else:
            await message.answer("Не получилось изменить период.")
        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    if state == "waiting_subscription_change_preset":
        digest_preset = parse_digest_preset_choice(text)
        if not digest_preset:
            await message.answer(format_digest_preset_help(), reply_markup=subscription_digest_preset_keyboard)
            return

        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        updated = await update_digest_subscription_preset(db_user["id"], int(subscription_id), digest_preset)
        if updated:
            await message.answer(f"Формат автосводки изменён: {get_digest_preset_title(digest_preset)}.")
        else:
            await message.answer("Не получилось изменить формат.")
        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    if state == "waiting_subscription_change_time":
        time_choice = parse_subscription_time_choice(text)
        if time_choice == "custom":
            user_states[user_id] = "waiting_subscription_change_custom_time"
            await message.answer(format_time_examples(), reply_markup=subscription_time_keyboard)
            return
        if not time_choice:
            await message.answer(
                "Выбери время кнопкой или нажми «✍️ Своё время».\n\n" + format_time_examples(),
                reply_markup=subscription_time_keyboard,
            )
            return

        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        updated = await update_digest_subscription_time(db_user["id"], int(subscription_id), time_choice)
        if updated:
            await message.answer(f"Время автосводки изменено: {time_choice}.")
        else:
            await message.answer("Не получилось изменить время.")
        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    if state == "waiting_subscription_change_custom_time":
        try:
            time_choice = format_send_time(text)
        except Exception:
            await message.answer(
                "Не получилось распознать время. " + format_time_examples(),
                reply_markup=subscription_time_keyboard,
            )
            return

        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        updated = await update_digest_subscription_time(db_user["id"], int(subscription_id), time_choice)
        if updated:
            await message.answer(f"Время автосводки изменено: {time_choice}.")
        else:
            await message.answer("Не получилось изменить время.")
        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    if state == "waiting_subscription_change_timezone":
        timezone_choice = parse_subscription_timezone_choice(text)
        if timezone_choice == "custom":
            user_states[user_id] = "waiting_subscription_change_custom_timezone"
            await message.answer(format_timezone_examples(), reply_markup=subscription_timezone_keyboard)
            return
        if not timezone_choice:
            await message.answer(
                "Выбери timezone кнопкой или нажми «✍️ Свой timezone».\n\n" + format_timezone_examples(),
                reply_markup=subscription_timezone_keyboard,
            )
            return

        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        updated = await update_digest_subscription_timezone(db_user["id"], int(subscription_id), timezone_choice)
        if updated:
            await message.answer(f"Timezone автосводки изменён: {timezone_choice}.")
        else:
            await message.answer("Не получилось изменить timezone.")
        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    if state == "waiting_subscription_change_custom_timezone":
        timezone_choice = validate_timezone_name(text)
        if not timezone_choice:
            await message.answer(
                "Не получилось распознать timezone. " + format_timezone_examples(),
                reply_markup=subscription_timezone_keyboard,
            )
            return

        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")
        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        updated = await update_digest_subscription_timezone(db_user["id"], int(subscription_id), timezone_choice)
        if updated:
            await message.answer(f"Timezone автосводки изменён: {timezone_choice}.")
        else:
            await message.answer("Не получилось изменить timezone.")
        await show_selected_subscription_card(message, db_user, int(subscription_id))
        return

    if state == "waiting_subscription_delete_confirm":
        subscription_id = user_parse_context.get(user_id, {}).get("selected_subscription_id")

        if text == "↩️ Нет, оставить":
            if subscription_id:
                await show_selected_subscription_card(message, db_user, int(subscription_id))
            else:
                await show_autodigest_management_list(message, db_user)
            return

        if text != "✅ Да, удалить автосводку":
            await message.answer(
                "Подтверди удаление кнопкой или нажми «↩️ Нет, оставить».",
                reply_markup=subscription_delete_confirm_keyboard,
            )
            return

        if not subscription_id:
            await show_autodigest_management_list(message, db_user)
            return

        deleted = await delete_digest_subscription(db_user["id"], int(subscription_id))
        user_parse_context.pop(user_id, None)
        user_states[user_id] = "autodigest_menu"
        if deleted:
            await message.answer(
                f"Автосводка #{subscription_id} удалена.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
        else:
            await message.answer(
                "Не получилось удалить автосводку. Возможно, она уже удалена.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
        return

    if state == "waiting_subscription_disable_number":
        subscriptions = user_parse_context.get(user_id, {}).get("subscriptions_for_action") or []
        index = parse_subscription_number(text, len(subscriptions))
        if index is None:
            await send_long_text(
                message,
                format_digest_subscriptions_list(subscriptions, title="Какую автосводку отключить?")
                + "\n\nНапиши номер из списка.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        subscription_id = int(subscriptions[index]["id"])
        disabled = await disable_digest_subscription(db_user["id"], subscription_id)
        user_states[user_id] = "autodigest_menu"
        user_parse_context.pop(user_id, None)
        if disabled:
            await message.answer(
                f"Автосводка #{subscription_id} отключена.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
        else:
            await message.answer(
                "Не получилось отключить автосводку. Возможно, она уже отключена или удалена.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
        return


    if state == "waiting_subscription_preview_number":
        subscriptions = user_parse_context.get(user_id, {}).get("subscriptions_for_action") or []
        index = parse_subscription_number(text, len(subscriptions))
        if index is None:
            await send_long_text(
                message,
                format_digest_subscriptions_list(subscriptions, title="Для какой автосводки показать сообщения для ИИ?")
                + "\n\nНапиши номер из списка.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        subscription_id = int(subscriptions[index]["id"])
        await run_subscription_ai_preview_for_user(message, db_user, subscription_id)
        return

    if state == "waiting_subscription_run_number":
        subscriptions = user_parse_context.get(user_id, {}).get("subscriptions_for_action") or []
        run_mode = user_parse_context.get(user_id, {}).get("pending_subscription_run_mode") or "manual"
        index = parse_subscription_number(text, len(subscriptions))
        if index is None:
            title = (
                "Debug: какую автосводку проверить сейчас?"
                if run_mode == "debug"
                else "Какую автосводку запустить сейчас?"
            )
            await send_long_text(
                message,
                format_digest_subscriptions_list(subscriptions, title=title)
                + "\n\nНапиши номер из списка.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        subscription_id = int(subscriptions[index]["id"])
        await ask_subscription_run_progress_choice(message, db_user, subscription_id, run_mode)
        return

    if state == "waiting_subscription_run_update_choice":
        raw_choice = (text or "").strip()
        if raw_choice not in {"✅ Да, обновить точку парса", "👀 Нет, просто посмотреть"}:
            await message.answer(
                "Нужно выбрать, обновлять ли точку последнего парса после запуска.",
                reply_markup=subscription_run_update_keyboard,
            )
            return

        update_progress = raw_choice == "✅ Да, обновить точку парса"
        context = user_parse_context.get(user_id, {})
        subscription_id = context.get("pending_subscription_run_id") or context.get("selected_subscription_id")
        run_mode = context.get("pending_subscription_run_mode") or "manual"
        if not subscription_id:
            user_states[user_id] = "autodigest_menu"
            await message.answer(
                "Не нашёл выбранную автосводку. Открой управление и попробуй ещё раз.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        locked_by = (
            f"debug-preview:user:{db_user['id']}:subscription:{int(subscription_id)}"
            if run_mode == "debug"
            else f"manual-run:user:{db_user['id']}:subscription:{int(subscription_id)}"
        )
        locked = await lock_digest_subscription_now(
            user_id=db_user["id"],
            subscription_id=int(subscription_id),
            locked_by=locked_by,
        )
        if not locked:
            user_states[user_id] = "autodigest_menu"
            await message.answer(
                "Не получилось взять автосводку в работу. Возможно, она отключена, удалена или уже запущена scheduler-ом/другим запуском.",
                reply_markup=await get_autodigest_keyboard_for_message(message),
            )
            return

        set_user_busy(user_id)
        try:
            result = await run_digest_subscription(
                subscription_id=int(subscription_id),
                bot=bot,
                debug=(run_mode == "debug"),
                locked_by=locked_by,
                update_progress=update_progress,
            )
            user_parse_context.setdefault(user_id, {})["selected_subscription_id"] = int(subscription_id)
            user_parse_context[user_id].pop("pending_subscription_run_id", None)
            user_parse_context[user_id].pop("pending_subscription_run_mode", None)

            mode_title = format_subscription_run_mode_title(run_mode)
            progress_text = (
                "Точка последнего парса обновлена."
                if update_progress and result.get("ok")
                else "Точка последнего парса не обновлялась."
            )
            if result.get("ok"):
                await message.answer(
                    f"{mode_title} завершён. Дайджест отправлен выше.\n\n"
                    f"Статус: {result.get('status')}\n"
                    f"Сообщений: {result.get('messages_count', 0)}\n"
                    f"{progress_text}",
                    reply_markup=await get_subscription_manage_keyboard_for_message(message),
                )
            else:
                await message.answer(
                    f"{mode_title} завершился ошибкой.\n\n"
                    f"Статус: {result.get('status')}\n"
                    f"Ошибка: {result.get('error')}\n"
                    f"{progress_text}",
                    reply_markup=await get_subscription_manage_keyboard_for_message(message),
                )
        finally:
            clear_user_busy(user_id)

        await show_selected_subscription_card(message, db_user, int(subscription_id))
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
                search_result = await find_channels_by_user_query(text, limit=10)
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

            matched_user_channels = await get_user_channels_for_query(
                user_id=db_user["id"],
                query_category=query_markup.get("category"),
                additional_categories=query_markup.get("additional_categories") or [],
                limit=MAX_USER_CHANNELS_PER_ACCOUNT,
            )

            user_parse_context[user_id] = {
                "search_query": text,
                "query_markup": query_markup,
                "found_channels": found_channels,
                "matched_user_channels_count": len(matched_user_channels),
            }
            prepare_found_channel_context(user_parse_context[user_id])

            output_text = format_channel_search_results(
                user_query=text,
                query_markup=query_markup,
                results=found_channels,
            )

            if matched_user_channels:
                category_text = query_markup.get("category") or "подходящей категории"
                output_text += (
                    f"\n\nТакже есть личные каналы категории «{category_text}»: "
                    f"{len(matched_user_channels)}. Их можно добавить к текущему сбору кнопкой «➕ Добавить канал»."
                )

            if found_channels:
                user_states[user_id] = "waiting_found_channels_action"
                output_text += format_found_channel_selection_status(user_parse_context[user_id])
                await send_long_text(message, output_text, reply_markup=found_channels_keyboard)
            else:
                user_states[user_id] = "waiting_channel_search_query"
                await send_long_text(message, output_text, reply_markup=main_keyboard)
        finally:
            clear_user_busy(user_id)

        return

    if state == "waiting_found_channel_count":
        context = user_parse_context.get(user_id, {})
        found_channels = context.get("found_channels") or []

        if not found_channels:
            user_states[user_id] = "waiting_channel_search_query"
            await message.answer(
                "Список потерялся. Напиши запрос заново.",
                reply_markup=main_keyboard,
            )
            return

        if not text.isdigit():
            await message.answer(
                "Нужно выбрать число от 1 до 10.",
                reply_markup=found_channels_count_keyboard,
            )
            return

        max_count = min(MAX_FOUND_CHANNELS_FOR_USER_CHOICE, len(found_channels))
        count = int(text)

        if count < 1 or count > max_count:
            await message.answer(
                f"Можно выбрать от 1 до {max_count} каналов.",
                reply_markup=found_channels_count_keyboard,
            )
            return

        prepare_found_channel_context(context)
        context["found_channel_count"] = count

                                                                                                          
        if not context.get("found_manual_selection"):
            context["selected_found_channel_indexes"] = list(range(count))

        user_parse_context[user_id] = context
        user_states[user_id] = "waiting_found_channels_action"

        await send_long_text(
            message,
            "Количество каналов изменено." + format_found_channel_selection_status(context),
            reply_markup=found_channels_keyboard,
        )
        return

    if state == "waiting_found_channel_list":
        context = user_parse_context.get(user_id, {})
        found_channels = context.get("found_channels") or []

        if not found_channels:
            user_states[user_id] = "waiting_channel_search_query"
            await message.answer(
                "Список потерялся. Напиши запрос заново.",
                reply_markup=main_keyboard,
            )
            return

        indexes = parse_found_channel_numbers(text, max_number=len(found_channels))

        if indexes is None:
            await send_long_text(
                message,
                "Нужно написать номера каналов из топа. Например: 1 3 7\n\n"
                + format_found_channel_selection_status(context),
                reply_markup=found_channels_keyboard,
            )
            return

        prepare_found_channel_context(context)
        context["selected_found_channel_indexes"] = indexes
        context["found_manual_selection"] = True

                                                                                                         
                                                                           
        if len(indexes) > int(context.get("found_channel_count") or 0):
            context["found_channel_count"] = len(indexes)

        user_parse_context[user_id] = context
        user_states[user_id] = "waiting_found_channels_action"

        await send_long_text(
            message,
            "Список каналов изменён." + format_found_channel_selection_status(context),
            reply_markup=found_channels_keyboard,
        )
        return

    if state == "waiting_digest_category_view":
        category = category_from_view_button(text)

        if not category:
            await message.answer(
                "Выбери категорию кнопкой или нажми «📋 Все каналы» в режиме сводки.",
                reply_markup=digest_channel_view_keyboard,
            )
            return

        channels = await get_user_channels(db_user["id"], user_category=category)
        await show_digest_channels_for_pick(
            message=message,
            user_id=user_id,
            channels=channels,
            title=f"Каналы категории «{category}» для сводки",
        )
        return

    if state == "waiting_digest_channel_numbers":
        channels = user_parse_context.get(user_id, {}).get("digest_candidate_channels") or []

        if not channels:
            user_states[user_id] = "digest_channel_view_menu"
            await message.answer(
                "Список каналов потерялся. Выбери заново.",
                reply_markup=digest_channel_view_keyboard,
            )
            return

        indexes = parse_digest_channel_numbers(text, max_number=len(channels), max_selected=MAX_DIGEST_CHANNELS)

        if indexes is None:
            await send_long_text(
                message,
                f"Нужно написать номера каналов из списка. Можно выбрать до {MAX_DIGEST_CHANNELS}. Например: 1 2 5 8 10.\n\n"
                + format_digest_channels_for_pick(channels, title="Каналы для сводки"),
                reply_markup=digest_channel_view_keyboard,
            )
            return

        selected_user_channels = [channels[index] for index in indexes]
        selected_channels = normalize_digest_channels_for_collect(selected_user_channels)

        user_parse_context[user_id] = {
            **user_parse_context.get(user_id, {}),
            "selected_channels": selected_channels,
            "digest_user_channel_ids": [int(channel["id"]) for channel in selected_user_channels if channel.get("id") is not None],
            "digest_selected_user_channels": selected_user_channels,
        }
        user_states[user_id] = "waiting_digest_period"

        selected_text = "Выбраны каналы для сводки:\n\n"
        for index, channel in enumerate(selected_channels, start=1):
            selected_text += f"{index}. {channel['username']} — {channel.get('title') or channel['username']}\n"

        selected_text += "\nТеперь выбери период. Для первого запуска лучше выбрать «За день» или «За неделю»."

        await message.answer(
            selected_text,
            reply_markup=digest_period_keyboard,
        )
        return

    if state == "waiting_digest_period":
        if text == "📆 Свой период":
            user_states[user_id] = "waiting_digest_custom_period"
            await message.answer(
                "Введи период двумя датами.\n\n"
                "Формат: ДД.ММ.ГГГГ ДД.ММ.ГГГГ\n"
                "Например: 01.05.2026 20.05.2026\n\n"
                f"Ограничения: не больше {MAX_CUSTOM_RANGE_DAYS} дня и не старше {MAX_CUSTOM_LOOKBACK_DAYS} дней.",
                reply_markup=digest_period_keyboard,
            )
            return

        selected_channels = user_parse_context.get(user_id, {}).get("selected_channels", [])
        digest_user_channel_ids = user_parse_context.get(user_id, {}).get("digest_user_channel_ids", [])

        if not selected_channels:
            user_states[user_id] = "channels_menu"
            await message.answer(
                "Выбор каналов потерялся. Начни заново.",
                reply_markup=channels_keyboard,
            )
            return

        if text == "🕓 С прошлого дайджеста":
            digest_start = await get_user_channel_digest_period_start(
                user_id=db_user["id"],
                user_channel_ids=digest_user_channel_ids,
            )

            if not digest_start["ok"]:
                await message.answer(
                    "Период «с прошлого дайджеста» пока недоступен.\n"
                    "Сначала сделай обычную сводку.",
                    reply_markup=digest_period_keyboard,
                )
                return

            date_from = digest_start["date_from"]
            date_to = datetime.now(timezone.utc)
            period_label = "с прошлого дайджеста"

            await run_digest_for_selected_channels(
                message=message,
                db_user=db_user,
                user_id=user_id,
                date_from=date_from,
                date_to=date_to,
                period_label=period_label,
            )
            return

        period_range = get_period_range(text)

        if period_range is None:
            await message.answer(
                "Выбери период кнопкой: за день, за неделю, за месяц, свой период или с прошлого дайджеста.",
                reply_markup=digest_period_keyboard,
            )
            return

        date_from, date_to = period_range
        period_label = text

        await run_digest_for_selected_channels(
            message=message,
            db_user=db_user,
            user_id=user_id,
            date_from=date_from,
            date_to=date_to,
            period_label=period_label,
        )
        return

    if state == "waiting_digest_custom_period":
        custom_period = parse_custom_period(text)

        if not custom_period["ok"]:
            await message.answer(
                f"{custom_period['error']}\n\n"
                "Попробуй ещё раз или нажми «⬅️ Назад».",
                reply_markup=digest_period_keyboard,
            )
            return

        await run_digest_for_selected_channels(
            message=message,
            db_user=db_user,
            user_id=user_id,
            date_from=custom_period["date_from"],
            date_to=custom_period["date_to"],
            period_label=custom_period["label"],
        )
        return

    if state == "waiting_user_channel_category_view":
        category = category_from_view_button(text)

        if not category:
            await message.answer(
                "Выбери категорию кнопкой или нажми «📋 Показать все каналы».",
                reply_markup=channel_view_keyboard,
            )
            return

        channels = await get_user_channels(db_user["id"], user_category=category)
        await send_long_text(
            message,
            format_user_channels_list(channels, title=f"Каналы категории «{category}»"),
            reply_markup=channel_view_keyboard,
        )
        user_states[user_id] = "user_channels_view_menu"
        return

    if state == "waiting_user_channel_category":
        if text == "📚 Ещё категории":
            user_states[user_id] = "waiting_user_channel_category_full"
            await message.answer(
                "Выбери категорию из полного списка.",
                reply_markup=build_user_channel_category_keyboard(full=True),
            )
            return

        category = normalize_category_from_text(text)

        if not category:
            await message.answer(
                "Выбери категорию кнопкой. Если нужной нет — нажми «📚 Ещё категории».",
                reply_markup=build_user_channel_category_keyboard(full=False),
            )
            return

        pending_channel = user_parse_context.get(user_id, {}).get("pending_user_channel")
        if not pending_channel:
            user_states[user_id] = "channels_menu"
            await message.answer(
                "Данные проверенного канала потерялись. Добавь канал заново.",
                reply_markup=channels_keyboard,
            )
            return

        saved_channel = await add_user_channel(
            user_id=db_user["id"],
            username=pending_channel["username"],
            title=pending_channel.get("title"),
            user_category=category,
        )

        user_parse_context.pop(user_id, None)
        user_states[user_id] = "channels_menu"

        title_text = f"\nНазвание: {saved_channel.get('title')}" if saved_channel.get("title") else ""
        await message.answer(
            f"Канал добавлен:\n{saved_channel['username']}{title_text}\n"
            f"Категория: {saved_channel.get('user_category') or category}\n\n"
            f"Лимит личного списка: {await count_user_channels(db_user['id'])}/{MAX_USER_CHANNELS_PER_ACCOUNT}.",
            reply_markup=channels_keyboard,
        )
        return

    if state == "waiting_user_channel_category_full":
        category = normalize_category_from_text(text)

        if not category:
            await message.answer(
                "Выбери категорию кнопкой из полного списка.",
                reply_markup=build_user_channel_category_keyboard(full=True),
            )
            return

        pending_channel = user_parse_context.get(user_id, {}).get("pending_user_channel")
        if not pending_channel:
            user_states[user_id] = "channels_menu"
            await message.answer(
                "Данные проверенного канала потерялись. Добавь канал заново.",
                reply_markup=channels_keyboard,
            )
            return

        saved_channel = await add_user_channel(
            user_id=db_user["id"],
            username=pending_channel["username"],
            title=pending_channel.get("title"),
            user_category=category,
        )

        user_parse_context.pop(user_id, None)
        user_states[user_id] = "channels_menu"

        title_text = f"\nНазвание: {saved_channel.get('title')}" if saved_channel.get("title") else ""
        await message.answer(
            f"Канал добавлен:\n{saved_channel['username']}{title_text}\n"
            f"Категория: {saved_channel.get('user_category') or category}\n\n"
            f"Лимит личного списка: {await count_user_channels(db_user['id'])}/{MAX_USER_CHANNELS_PER_ACCOUNT}.",
            reply_markup=channels_keyboard,
        )
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

        channels_count = await count_user_channels(db_user["id"])
        if channels_count >= MAX_USER_CHANNELS_PER_ACCOUNT:
            user_states[user_id] = "channels_menu"
            await message.answer(
                f"У тебя уже {MAX_USER_CHANNELS_PER_ACCOUNT} каналов. Это максимум для личного списка.",
                reply_markup=channels_keyboard,
            )
            return

        channel_exists = await user_has_channel(db_user["id"], channel)

        if channel_exists:
            await message.answer(
                f"Этот канал уже есть в твоём списке:\n{channel}\n\n"
                "Можешь отправить другой канал или нажать «⬅️ Назад».",
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

            user_parse_context[user_id] = {
                "pending_user_channel": {
                    "username": channel,
                    "title": channel_check.get("title"),
                }
            }
            user_states[user_id] = "waiting_user_channel_category"

            title_text = f"\nНазвание: {channel_check['title']}" if channel_check.get("title") else ""
            await message.answer(
                f"Канал проверен:\n{channel}{title_text}\n\n"
                "Теперь выбери категорию. Это ручная категория — она нужна, чтобы потом быстро подключать личные каналы к поиску.",
                reply_markup=build_user_channel_category_keyboard(full=False),
            )
        finally:
            clear_user_busy(user_id)

        return

    if state == "waiting_delete_channel_number":
        context = user_parse_context.get(user_id, {})
        channels = context.get("delete_candidate_channels") or await get_user_channels(db_user["id"])

        if not channels:
            user_states[user_id] = "channels_menu"
            user_parse_context.pop(user_id, None)
            await message.answer(
                "Удалять пока нечего: у тебя нет активных личных каналов.",
                reply_markup=channels_keyboard,
            )
            return

        if not text.isdigit():
            await send_long_text(
                message,
                "Нужно написать номер канала из списка. Например: 1\n\n"
                + format_user_channels_delete_list(channels, title="Выбери канал для удаления"),
                reply_markup=channels_keyboard,
            )
            return

        channel_index = int(text) - 1

        if channel_index < 0 or channel_index >= len(channels):
            await send_long_text(
                message,
                "Такого номера нет в списке каналов.\n\n"
                + format_user_channels_delete_list(channels, title="Выбери канал для удаления"),
                reply_markup=channels_keyboard,
            )
            return

        channel = channels[channel_index]
        usage = await get_user_channel_subscription_usage(
            user_id=db_user["id"],
            user_channel_id=channel["id"],
        )

        context["delete_candidate_channels"] = channels
        context["pending_delete_channel"] = channel
        context["pending_delete_subscription_usage"] = usage
        user_parse_context[user_id] = context
        user_states[user_id] = "waiting_delete_channel_confirm"

        username = normalize_username_for_display(channel.get("username"))
        title_value = channel.get("title") or username
        usage_text = format_user_channel_subscription_usage(usage)

        extra_warning = ""
        if usage:
            extra_warning = (
                "\n\n⚠️ Канал будет убран из автосводок."
            )

        await send_long_text(
            message,
            f"Удалить личный канал?\n\n"
            f"{username} — {title_value}\n\n"
            f"{usage_text}"
            f"{extra_warning}\n\n"
            "Подтверди действие.",
            reply_markup=user_channel_delete_confirm_keyboard,
        )
        return

    if state == "waiting_delete_channel_confirm":
        context = user_parse_context.get(user_id, {})
        channel = context.get("pending_delete_channel")

        if text == "↩️ Нет, оставить":
            channels = context.get("delete_candidate_channels") or await get_user_channels(db_user["id"])
            user_parse_context[user_id] = {"delete_candidate_channels": channels}
            user_states[user_id] = "waiting_delete_channel_number"
            await send_long_text(
                message,
                "Ок, канал оставил. Можно выбрать другой канал для удаления.\n\n"
                + format_user_channels_delete_list(channels, title="Выбери канал для удаления"),
                reply_markup=channels_keyboard,
            )
            return

        if text != "✅ Да, удалить канал":
            await message.answer(
                "Подтверди удаление канала кнопкой или нажми «↩️ Нет, оставить».",
                reply_markup=user_channel_delete_confirm_keyboard,
            )
            return

        if not channel:
            user_states[user_id] = "channels_menu"
            user_parse_context.pop(user_id, None)
            await message.answer(
                "Не нашёл канал для удаления в текущем состоянии. Открой удаление заново.",
                reply_markup=channels_keyboard,
            )
            return

        result = await remove_user_channel_with_subscription_links(
            user_id=db_user["id"],
            user_channel_id=channel["id"],
        )

        user_states[user_id] = "channels_menu"
        user_parse_context.pop(user_id, None)

        username = normalize_username_for_display(channel.get("username"))
        if result.get("removed"):
            links_removed = int(result.get("subscription_links_removed") or 0)
            extra = ""
            if links_removed:
                extra = f"\nТакже канал убран из автосводок: {links_removed}."
            await message.answer(
                f"Канал удалён из твоего списка:\n{username}{extra}",
                reply_markup=channels_keyboard,
            )
        else:
            await message.answer(
                "Канал не найден в твоём списке или уже был удалён.",
                reply_markup=channels_keyboard,
            )

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
                "Список потерялся. Напиши запрос заново.",
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
                "Список потерялся. Напиши запрос заново.",
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

    await answer_state_fallback(
        message,
        state=state,
        context=user_parse_context.get(user_id, {}),
    )
