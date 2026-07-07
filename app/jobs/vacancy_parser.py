from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_VACANCY_CHANNEL_LIMIT = 10
DEFAULT_VACANCY_DAYS = 3
DEFAULT_VACANCY_MAX_RESULTS = 20


def parse_vacancy_keywords(text: str | None) -> list[str]:
    raw = (text or "").strip()
    if not raw:
        return []

    parts = [part.strip().lower().replace("ё", "е") for part in re.split(r"[,;\n]+", raw) if part.strip()]
    result: list[str] = []
    seen: set[str] = set()

    for part in parts:
        part = re.sub(r"\s{2,}", " ", part).strip(" .,:;!?-—\"'«»")
        if len(part) < 2:
            continue
        if part not in seen:
            result.append(part)
            seen.add(part)

    return result[:30]


def normalize_match_text(value: str | None) -> str:
    return (value or "").lower().replace("ё", "е")


def message_matches_keywords(text: str | None, keywords: list[str]) -> list[str]:
    normalized_text = normalize_match_text(text)
    matched = []
    for keyword in keywords:
        keyword = normalize_match_text(keyword).strip()
        if keyword and keyword in normalized_text:
            matched.append(keyword)
    return matched


def build_message_link(username: str | None, telegram_message_id: int | None) -> str:
    username = (username or "").strip().lstrip("@")
    if not username or not telegram_message_id:
        return ""
    return f"https://t.me/{username}/{int(telegram_message_id)}"


def trim_text(value: str | None, limit: int = 450) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def format_vacancy_channels_preview(channels: list[dict[str, Any]], query: str) -> str:
    if not channels:
        return (
            "📍 Вакансии\n"
            "Каналы не найдены.\n\n"
            "Что дальше: попробуй другой запрос."
        )

    lines = [
        "📍 Вакансии",
        f"Запрос: {query}",
        f"Каналов найдено: {len(channels)}",
        "",
    ]

    for index, channel in enumerate(channels, start=1):
        lines.append(f"{index}. {channel.get('username')} — {channel.get('title') or channel.get('username')}")

    lines.extend([
        "",
        "Что дальше: напиши ключевики через запятую.",
        "Пример: python, backend, remote, django",
    ])
    return "\n".join(lines)


def format_vacancy_parse_result(result: dict[str, Any]) -> str:
    channels = result.get("channels") or []
    matches = result.get("matches") or []
    keywords = result.get("keywords") or []
    collect_result = result.get("collect_result") or {}

    lines = [
        "📍 Вакансии",
        f"Период: {result.get('days', DEFAULT_VACANCY_DAYS)} дня",
        f"Каналов: {len(channels)}",
        f"Сообщений собрано: {int(collect_result.get('messages_for_user') or 0)}",
        f"Найдено по ключам: {len(matches)}",
        f"Ключи: {', '.join(keywords[:12])}",
        "",
    ]

    if not channels:
        lines.append("Каналы не найдены.")
        return "\n".join(lines).strip()

    if not matches:
        lines.append("Совпадений нет.")
        lines.append("Что дальше: попробуй шире ключи или другой запрос каналов.")
        return "\n".join(lines).strip()

    lines.append("Найденные сообщения:")
    for index, item in enumerate(matches[:DEFAULT_VACANCY_MAX_RESULTS], start=1):
        username = item.get("username") or ""
        date_text = item.get("date_text") or "без даты"
        matched = ", ".join(item.get("matched_keywords") or [])
        link = item.get("link") or ""
        text = trim_text(item.get("cleaned_text"), 320)

        lines.append("")
        lines.append(f"{index}. {username} | {date_text}")
        if matched:
            lines.append(f"Ключи: {matched}")
        if link:
            lines.append(link)
        lines.append(text)

    hidden = len(matches) - DEFAULT_VACANCY_MAX_RESULTS
    if hidden > 0:
        lines.append("")
        lines.append(f"Ещё найдено: {hidden}. Сузь ключи, если нужно меньше мусора.")

    return "\n".join(lines).strip()


async def find_vacancy_channels(query: str, limit: int = DEFAULT_VACANCY_CHANNEL_LIMIT) -> dict[str, Any]:
    from app.search.channel_search import find_channels_by_user_query, convert_found_channels_for_collect

    search_result = await find_channels_by_user_query(query, limit=limit)
    channels = convert_found_channels_for_collect(search_result.get("results") or [], limit=limit)
    return {
        "query": query,
        "search_result": search_result,
        "channels": channels,
    }


async def parse_vacancies_from_channels(
    *,
    db_user_id: int,
    channels: list[dict[str, Any]],
    keywords: list[str],
    days: int = DEFAULT_VACANCY_DAYS,
) -> dict[str, Any]:
    from app.telegram.collector import collect_messages_from_channels

    now = datetime.now(timezone.utc)
    date_from = now - timedelta(days=max(1, int(days or DEFAULT_VACANCY_DAYS)))

    collect_result = await collect_messages_from_channels(
        db_user_id=db_user_id,
        selected_channels=channels,
        date_from=date_from,
        date_to=now,
        max_messages_per_channel=80,
    )

    matches: list[dict[str, Any]] = []
    for item in collect_result.get("structured_messages") or []:
        matched = message_matches_keywords(item.get("cleaned_text"), keywords)
        if not matched:
            continue

        telegram_message_id = item.get("telegram_message_id")
        link = build_message_link(item.get("username"), telegram_message_id)
        matches.append({
            **item,
            "matched_keywords": matched,
            "link": link,
        })

    return {
        "ok": bool(collect_result.get("ok")),
        "days": days,
        "channels": channels,
        "keywords": keywords,
        "collect_result": collect_result,
        "matches": matches,
        "error": collect_result.get("error"),
    }
