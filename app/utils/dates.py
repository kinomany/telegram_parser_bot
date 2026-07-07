import re
from datetime import date, datetime, time, timedelta, timezone

from config import MAX_CUSTOM_LOOKBACK_DAYS, MAX_CUSTOM_RANGE_DAYS

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
