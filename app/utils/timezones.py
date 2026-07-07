from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Tbilisi"
DEFAULT_SEND_TIME = "09:00"

COMMON_TIMEZONES = [
    "Asia/Tbilisi",
    "Europe/Moscow",
    "Europe/Kyiv",
    "Europe/Istanbul",
    "Europe/Berlin",
    "UTC",
]

                                                                 
                                                                            
_FIXED_OFFSET_FALLBACKS = {
    "UTC": timezone.utc,
    "Asia/Tbilisi": timezone(timedelta(hours=4), name="Asia/Tbilisi"),
    "Europe/Moscow": timezone(timedelta(hours=3), name="Europe/Moscow"),
    "Europe/Istanbul": timezone(timedelta(hours=3), name="Europe/Istanbul"),
}

_TIMEZONE_ALIASES = {
    "tbilisi": "Asia/Tbilisi",
    "georgia": "Asia/Tbilisi",
    "грузия": "Asia/Tbilisi",
    "тбилиси": "Asia/Tbilisi",
    "moscow": "Europe/Moscow",
    "москва": "Europe/Moscow",
    "msk": "Europe/Moscow",
    "kyiv": "Europe/Kyiv",
    "kiev": "Europe/Kyiv",
    "киев": "Europe/Kyiv",
    "київ": "Europe/Kyiv",
    "istanbul": "Europe/Istanbul",
    "стамбул": "Europe/Istanbul",
    "utc": "UTC",
}


def ensure_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_timezone_name(value: str | None, default: str = DEFAULT_TIMEZONE) -> str:
    text = (value or "").strip()
    if not text:
        return default

    if text.startswith("🌍"):
        text = text.replace("🌍", "", 1).strip()

    alias = _TIMEZONE_ALIASES.get(text.lower())
    if alias:
        return alias

                                                             
    if "/" in text or text.upper() == "UTC":
        return text

    return default


def get_timezone(value: str | None):
    name = normalize_timezone_name(value)
    if name in _FIXED_OFFSET_FALLBACKS:
                                                                                                    
        try:
            return ZoneInfo(name)
        except Exception:
            return _FIXED_OFFSET_FALLBACKS[name]
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError(
            f"Неизвестный timezone: {name}. Используй формат вроде Asia/Tbilisi или Europe/Moscow. "
            "Если запускаешь на Windows, установи пакет tzdata: pip install tzdata"
        ) from error


def validate_timezone_name(value: str | None) -> str | None:
    raw = (value or "").strip()
    if raw.startswith("🌍"):
        raw = raw.replace("🌍", "", 1).strip()
    if not raw:
        return None

    alias = _TIMEZONE_ALIASES.get(raw.lower())
    if alias:
        name = alias
    elif "/" in raw or raw.upper() == "UTC":
        name = raw
    else:
        return None

    try:
        get_timezone(name)
    except Exception:
        return None
    return normalize_timezone_name(name)


def parse_send_time(value: str | time | None, default: str = DEFAULT_SEND_TIME) -> time:
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)

    text = str(value or default).strip()
    for prefix in ("🕘", "🌅", "☀️", "🌆", "🌙"):
        if text.startswith(prefix):
            text = text.replace(prefix, "", 1).strip()

                                                                                          
    match_text = text.split()[0]
    parts = match_text.split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        raise ValueError("Время должно быть в формате HH:MM, например 09:00")

    hour = int(parts[0])
    minute = int(parts[1])
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("Время должно быть от 00:00 до 23:59")
    return time(hour=hour, minute=minute)


def format_send_time(value: str | time | None) -> str:
    try:
        parsed = parse_send_time(value)
    except Exception:
        parsed = parse_send_time(DEFAULT_SEND_TIME)
    return f"{parsed.hour:02d}:{parsed.minute:02d}"


def calculate_next_run_at(
    period_days: int,
    send_time: str | time | None = DEFAULT_SEND_TIME,
    timezone_name: str | None = DEFAULT_TIMEZONE,
    from_time: datetime | None = None,
    now: datetime | None = None,
) -> datetime:
    """
    Возвращает timezone-aware UTC datetime следующего запуска.

    Логика: следующий запуск через period_days от базовой даты, но строго в локальное send_time.
    Если рассчитанное время уже прошло, переносим ещё на period_days вперёд.
    """
    period_days = max(1, int(period_days or 7))
    local_tz = get_timezone(timezone_name)
    local_time = parse_send_time(send_time)

    now_utc = ensure_aware_utc(now) or datetime.now(timezone.utc)
    base_utc = ensure_aware_utc(from_time) or now_utc
    base_local = base_utc.astimezone(local_tz)

    candidate_date = base_local.date() + timedelta(days=period_days)
    candidate_local = datetime.combine(candidate_date, local_time, tzinfo=local_tz)
    candidate_utc = candidate_local.astimezone(timezone.utc)

    while candidate_utc <= now_utc:
        candidate_date = candidate_date + timedelta(days=period_days)
        candidate_local = datetime.combine(candidate_date, local_time, tzinfo=local_tz)
        candidate_utc = candidate_local.astimezone(timezone.utc)

    return candidate_utc


def format_datetime_local(value, timezone_name: str | None = DEFAULT_TIMEZONE, include_timezone: bool = True) -> str:
    if value is None:
        return "нет"
    try:
        aware = ensure_aware_utc(value)
        if aware is None:
            return "нет"
        name = normalize_timezone_name(timezone_name)
        local = aware.astimezone(get_timezone(name))
        result = local.strftime("%d.%m.%Y %H:%M")
        if include_timezone:
            result += f" ({name})"
        return result
    except Exception:
        return str(value)
