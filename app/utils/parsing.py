from __future__ import annotations

import re
from typing import Iterable


_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")


def normalize_telegram_username(value: str | None, *, allow_plain: bool = True) -> str | None:
    """
    Приводит Telegram username/link к виду @username.

    Поддерживает:
    - @channel
    - https://t.me/channel
    - http://t.me/channel
    - t.me/channel
    - telegram.me/channel
    - plain channel, если allow_plain=True

    Не принимает приватные invite-ссылки и нестандартные значения, потому что
    парсер работает с публичными каналами по username.
    """
    text = (value or "").strip()
    if not text:
        return None

    for prefix in ("https://", "http://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break

    lower = text.lower()
    if lower.startswith("t.me/"):
        username = text[5:]
    elif lower.startswith("telegram.me/"):
        username = text[12:]
    elif text.startswith("@"):
        username = text[1:]
    elif allow_plain:
        username = text
    else:
        return None

    username = username.strip()
    username = username.split("?")[0]
    username = username.split("#")[0]
    username = username.split("/")[0]
    username = username.strip().strip("@")

    if not _USERNAME_RE.fullmatch(username):
        return None

    return f"@{username}"


def normalize_channel_input(value: str | None) -> str | None:
    """Нормализация ввода при добавлении канала. Plain username без @ не принимаем."""
    return normalize_telegram_username(value, allow_plain=False)


def parse_channel_numbers(text: str | None, max_number: int, *, max_selected: int | None = None) -> list[int] | None:
    """
    Парсит выбор каналов: "1", "1 2 3", "1,2,3", "1; 2".
    Возвращает индексы с нуля, сохраняя порядок и убирая дубли.

    None означает невалидный ввод.
    """
    try:
        max_number = int(max_number)
    except (TypeError, ValueError):
        return None

    if max_number < 1:
        return None

    raw = (text or "").strip()
    if not raw:
        return None

    parts = [part for part in re.split(r"[\s,;]+", raw) if part]
    if not parts:
        return None

    if max_selected is not None:
        try:
            max_selected = int(max_selected)
        except (TypeError, ValueError):
            return None
        if max_selected < 1 or len(parts) > max_selected:
            return None

    indexes: list[int] = []
    seen: set[int] = set()
    for part in parts:
        if not part.isdigit():
            return None

        number = int(part)
        if number < 1 or number > max_number:
            return None

        index = number - 1
        if index not in seen:
            indexes.append(index)
            seen.add(index)

    return indexes
