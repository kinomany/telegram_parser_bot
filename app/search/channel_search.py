import json
import re

from app.ai.query_channel_selector import (
    analyze_user_query,
    create_db_pool as create_channel_search_db_pool,
    load_marked_channels,
    rank_channels,
)
from app.utils.debug import debug_print_block


def normalize_match_text(value: str | None) -> str:
    return (value or "").lower().replace("ё", "е")

def normalize_found_channel_username(username: str | None) -> str:
    username = (username or "").strip().lstrip("@")
    return f"@{username}" if username else ""


def convert_found_channels_for_collect(found_channels: list[dict], limit: int | None = 5) -> list[dict]:
    """Приводит найденные каналы из общей базы к формату collect_messages_from_channels()."""
    selected_channels = []
    items = found_channels if limit is None else found_channels[:limit]

    for item in items:
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

    default_count = min(5, len(results))
    text += (
        f"\nПо умолчанию бот возьмёт первые {default_count} каналов из этого списка. "
        "Но ты можешь изменить количество, выбрать конкретные номера из топа или включить/выключить добор."
    )
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


async def find_channels_by_user_query(user_query: str, limit: int = 10) -> dict:
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
