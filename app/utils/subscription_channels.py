from __future__ import annotations

import json
from typing import Any

from app.utils.parsing import normalize_telegram_username


def normalize_subscription_channels(raw_channels: Any) -> list[dict[str, Any]]:
    """
    Приводит каналы автосводки к list[dict].

    Зачем: asyncpg/jsonb в разных настройках может вернуть list[dict], dict,
    JSON-строку или старую строку вида "@a,@b". Scheduler не должен падать
    на таких вариантах.
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

    normalized: list[dict[str, Any]] = []
    for item in raw_channels:
        if isinstance(item, str):
            username = item.strip()
            if username:
                normalized.append({"id": None, "username": username, "title": username, "user_category": None})
            continue

        if isinstance(item, dict):
            normalized.append(dict(item))

    return normalized


def normalize_channel_for_collect(channel: Any) -> dict[str, Any]:
    """Приводит один канал подписки к формату, который ждёт collect_messages_from_channels."""
    if not isinstance(channel, dict):
        channel = {"id": None, "username": str(channel or "").strip(), "title": str(channel or "").strip()}

    username = normalize_telegram_username(channel.get("username"), allow_plain=True) or ""
    return {
        "id": channel.get("id"),
        "username": username,
        "title": channel.get("title") or username,
        "source": "user_channel_subscription",
        "user_category": channel.get("user_category"),
    }


def get_user_channel_ids_from_channels(channels: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for channel in channels:
        if not isinstance(channel, dict):
            continue
        value = channel.get("id")
        if value is None:
            continue
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids
