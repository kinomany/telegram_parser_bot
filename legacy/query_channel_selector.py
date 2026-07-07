import asyncio
import json
import os
import re
import sys
from typing import Any

import asyncpg
from dotenv import load_dotenv
from google import genai
from google.genai import types

                                             
load_dotenv()

from config import (              
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    GEMINI_API_KEY,
    GEMINI_QUERY_MODELS,
    TOP_CHANNELS_LIMIT,
    MIN_CHANNEL_SCORE,
)

try:
    from tax_tree import ГЛАВНЫЕ_КАТЕГОРИИ              
except Exception:
    ГЛАВНЫЕ_КАТЕГОРИИ = [
        "новости и СМИ",
        "политика",
        "война и конфликты",
        "экономика",
        "технологии",
        "игры",
        "здоровье и фитнес",
        "медицина",
        "образование",
        "культура",
        "спорт",
        "другое",
    ]

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY не найден в .env")

client = genai.Client(api_key=GEMINI_API_KEY)

ALLOWED_CATEGORIES = set(ГЛАВНЫЕ_КАТЕГОРИИ)
ALLOWED_CATEGORIES.add("неясно")

CATEGORY_ALIASES = {
    "здоровье": "здоровье и фитнес",
    "развлечения": "юмор и развлечения",
    "it": "технологии",
    "айти": "технологии",
    "сми": "новости и СМИ",
    "новости": "новости и СМИ",
    "медиа": "новости и СМИ",
    "гейминг": "игры",
    "видеоигры": "игры",
    "игровая индустрия": "игры",
}

ALLOWED_CATEGORIES_TEXT = "\n".join(f"- {category}" for category in ГЛАВНЫЕ_КАТЕГОРИИ + ["неясно"])

QUERY_SYSTEM_PROMPT = """
Верни только JSON без markdown и без пояснений.

Задача: разобрать короткий пользовательский запрос для подбора Telegram-каналов.
Не отвечай на запрос пользователя. Только классифицируй его.

Схема ответа:
{
  "category": "технологии",
  "additional_categories": [],
  "region": null,
  "country": null,
  "city": null,
  "format": null,
  "channel_type": null,
  "is_author_channel": null,
  "position": null,
  "topics": ["искусственный интеллект"],
  "objects": ["OpenAI"],
  "keywords": ["нейросети", "искусственный интеллект"],
  "negative_keywords": [],
  "time_focus": "свежие новости",
  "confidence": 0.8
}

Категории строго из списка:
__ALLOWED_CATEGORIES_TEXT__

Правила:
- category: одна главная категория. Если непонятно — "неясно".
- additional_categories: только если в запросе явно есть второй смысловой фокус.
- region/country/city: заполняй только если пользователь явно указал географию.
- format: новости, аналитика, мнение, расследования, инструкции, обзор, личный блог, агрегация новостей, юмор, реклама, null.
- channel_type: авторский, редакционный, официальный, агрегатор, сообщество, коммерческий, null.
- is_author_channel: true/false/null.
- position: официальная, оппозиционная, нейтральная, провластная, антивоенная, смешанная, null.
- topics: 1-6 устойчивых тем.
- objects: люди, организации, страны, компании, проекты из запроса.
- keywords: 2-8 слов/фраз для поиска каналов.
- negative_keywords: что пользователь явно НЕ хочет видеть.
- time_focus: свежие новости, последняя неделя, постоянная тема, смешанный временной фокус, null.

Важные правила категорий:
- Если запрос про игры, видеоигры, гейминг, Steam, релизы игр, новинки игр, ПК-игры, PC games — category="игры", а не "технологии".
- Если запрос про приложения, программы, сервисы, утилиты — category="софт и приложения".
- Если запрос про фильмы, сериалы, YouTube-видео, обзоры фильмов — category="видео и фильмы".
- Если запрос про советы по здоровью, фитнес, питание, профилактику — category="здоровье и фитнес".
- Если запрос про болезни, лечение, лекарства, врачей, клиники — category="медицина".
- Если запрос про юмор, мемы, развлечения — category="юмор и развлечения".

Не добавляй в keywords мусор: новости, канал, пост, телеграм, информация, события.
Не выдумывай сущности, которых нет в запросе.
""".replace("__ALLOWED_CATEGORIES_TEXT__", ALLOWED_CATEGORIES_TEXT).strip()


def normalize_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None

    value = re.sub(r"\s{2,}", " ", value).strip()
    value = value.strip(" .,:;!?-—\"'«»")

    return value or None


def normalize_lower(value: Any) -> str | None:
    value = normalize_text(value)
    return value.lower().replace("ё", "е") if value else None


def normalize_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []

    result: list[str] = []
    seen: set[str] = set()

    for item in value:
        item = normalize_text(item)
        if not item:
            continue

        if len(item) > 70:
            continue

        lower = item.lower().replace("ё", "е")
        if lower in seen:
            continue

        if lower in {"новости", "канал", "пост", "информация", "события", "телеграм"}:
            continue

        seen.add(lower)
        result.append(item)

        if len(result) >= limit:
            break

    return result


def parse_json_response(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()

    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None

    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def normalize_category(value: Any) -> str:
    category = normalize_lower(value) or "неясно"
    category = CATEGORY_ALIASES.get(category, category)

    if category not in ALLOWED_CATEGORIES:
        return "неясно"

    return category


def append_unique(values: list[str], new_values: list[str], limit: int = 8) -> list[str]:
    result = []
    seen = set()

    for value in [*values, *new_values]:
        value = normalize_text(value)
        if not value:
            continue

        key = value.lower().replace("ё", "е")
        if key in seen:
            continue

        seen.add(key)
        result.append(value)

        if len(result) >= limit:
            break

    return result


def is_game_query(user_query: str | None) -> bool:
    text = normalize_lower(user_query) or ""

    return any(
        marker in text
        for marker in (
            "игр",
            "видеоигр",
            "гейм",
            "steam",
            "стим",
            "pc game",
            "pc-game",
            "пк-игр",
            "пк игр",
            "для пк",
            "релиз",
            "новинк",
        )
    )


def apply_raw_query_overrides(markup: dict[str, Any], user_query: str | None) -> dict[str, Any]:
    text = normalize_lower(user_query) or ""

    if is_game_query(user_query):
        old_category = markup.get("category")
        markup["category"] = "игры"

        additional = list(markup.get("additional_categories") or [])
        if old_category and old_category not in {"игры", "неясно", "новости и СМИ"}:
            additional = append_unique(additional, [old_category], limit=5)
        markup["additional_categories"] = [item for item in additional if item != "игры"]

        topics_to_add = ["видеоигры", "новости игр"]
        keywords_to_add = ["игры", "гейминг", "релизы игр"]

        if any(marker in text for marker in ("пк", "pc", "steam", "стим", "для пк")):
            topics_to_add.extend(["ПК-игры", "PC-игры"])
            keywords_to_add.extend(["ПК-гейминг", "PC-гейминг", "Steam"])

        if any(marker in text for marker in ("новинк", "релиз")):
            topics_to_add.append("релизы игр")
            keywords_to_add.append("новинки игр")


        markup["topics"] = append_unique(markup.get("topics") or [], topics_to_add, limit=8)
        markup["keywords"] = append_unique(markup.get("keywords") or [], keywords_to_add, limit=8)

    return markup


def normalize_query_markup(data: dict[str, Any], user_query: str | None = None) -> dict[str, Any]:
    category = normalize_category(data.get("category"))

    additional_categories = []
    for item in normalize_list(data.get("additional_categories"), limit=5):
        lower = normalize_category(item)
        if lower in ALLOWED_CATEGORIES and lower != category:
            additional_categories.append(lower)

    confidence = data.get("confidence")
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.5

    confidence = max(0.0, min(confidence, 1.0))

    markup = {
        "category": category,
        "additional_categories": additional_categories,
        "region": normalize_text(data.get("region")),
        "country": normalize_text(data.get("country")),
        "city": normalize_text(data.get("city")),
        "format": normalize_lower(data.get("format")),
        "channel_type": normalize_lower(data.get("channel_type")),
        "is_author_channel": data.get("is_author_channel") if isinstance(data.get("is_author_channel"), bool) else None,
        "position": normalize_lower(data.get("position")),
        "topics": normalize_list(data.get("topics"), limit=8),
        "objects": normalize_list(data.get("objects"), limit=8),
        "keywords": normalize_list(data.get("keywords"), limit=8),
        "negative_keywords": normalize_list(data.get("negative_keywords"), limit=8),
        "time_focus": normalize_lower(data.get("time_focus")),
        "confidence": round(confidence, 2),
    }

    return apply_raw_query_overrides(markup, user_query)


async def analyze_user_query(user_query: str) -> dict[str, Any]:
    last_error = "неизвестная ошибка"

    for model_name in GEMINI_QUERY_MODELS:
        try:
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=f"Пользовательский запрос:\n{user_query}",
                config=types.GenerateContentConfig(
                    system_instruction=QUERY_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )

            data = parse_json_response(response.text or "")

            if not data:
                last_error = f"{model_name}: модель вернула не JSON"
                print(last_error)
                continue

            markup = normalize_query_markup(data, user_query=user_query)
            markup["model_used"] = model_name
            return markup

        except Exception as exc:
            last_error = f"{model_name}: {type(exc).__name__}: {exc}"
            print(f"Ошибка разбора запроса. {last_error}")

    raise RuntimeError(f"Не смог разобрать запрос через ИИ. Последняя ошибка: {last_error}")


async def create_db_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=1,
        max_size=5,
    )


def parse_jsonb(value: Any, default: Any) -> Any:
    if value is None:
        return default

    if isinstance(value, (dict, list)):
        return value

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    return default


async def load_marked_channels(pool: asyncpg.Pool) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.id AS channel_id,
                c.username,
                COALESCE(c.tg_title, c.username) AS title,
                c.tg_description,
                m.category,
                m.region,
                m.content_format,
                m.position_label,
                m.confidence,
                m.ai_keywords,
                m.ai_description,
                m.ai_classification
            FROM channels c
            JOIN channel_ai_markup m ON m.channel_id = c.id
            WHERE m.is_current = TRUE
              AND m.status = 'done'
            ORDER BY c.id ASC;
            """
        )

    result: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row)
        item["ai_keywords"] = parse_jsonb(item.get("ai_keywords"), [])
        item["ai_classification"] = parse_jsonb(item.get("ai_classification"), {})
        result.append(item)

    return result


def to_search_blob(channel: dict[str, Any]) -> str:
    classification = channel.get("ai_classification") or {}
    parts: list[str] = [
        str(channel.get("username") or ""),
        str(channel.get("title") or ""),
        str(channel.get("tg_description") or ""),
        str(channel.get("ai_description") or ""),
        str(channel.get("category") or ""),
        str(channel.get("region") or ""),
        str(channel.get("content_format") or ""),
        str(channel.get("position_label") or ""),
        " ".join(str(x) for x in channel.get("ai_keywords") or []),
    ]

                                                                     
                                                                                             
                                                                                
                                               
    for key in (
        "категория",
        "регион",
        "страна",
        "город",
        "формат",
        "тип охвата",
        "позиция",
        "политическая тема",
        "конфликт",
        "военная тема",
        "область экономики",
        "область технологий",
        "область науки",
        "социальная тема",
        "темы",
        "объекты",
        "ключевые слова",
        "описание",
    ):
        value = classification.get(key)
        if isinstance(value, list):
            parts.extend(str(x) for x in value)
        elif value is not None:
            parts.append(str(value))

    for value in classification.values():
        if isinstance(value, list):
            parts.extend(str(x) for x in value if x is not None)
        elif value is not None:
            parts.append(str(value))

    return " ".join(parts).lower().replace("ё", "е")


def values_from_classification(classification: dict[str, Any], keys: list[str]) -> list[str]:
    result: list[str] = []

    for key in keys:
        value = classification.get(key)
        if isinstance(value, list):
            result.extend(str(x) for x in value if x)
        elif value:
            result.append(str(value))

    return result


def text_matches(needle: str, haystack: str) -> bool:
    needle = needle.lower().replace("ё", "е").strip()
    if not needle:
        return False

    if needle in haystack:
        return True

    words = [word for word in re.findall(r"[a-zа-я0-9-]{4,}", needle) if word]
    return bool(words) and any(word in haystack for word in words)


def rank_channel(query: dict[str, Any], channel: dict[str, Any]) -> dict[str, Any]:
    classification = channel.get("ai_classification") or {}
    haystack = to_search_blob(channel)
    score = 0
    matched: list[str] = []

    query_category = query.get("category")
    channel_category = normalize_lower(channel.get("category") or classification.get("категория"))

    if query_category and query_category != "неясно":
        if channel_category == query_category:
            score += 35
            matched.append(f"категория: {query_category}")
        else:
                                                                                                 
            score -= 12

    channel_region = normalize_lower(channel.get("region") or classification.get("регион"))
    query_region = normalize_lower(query.get("region"))
    query_country = normalize_lower(query.get("country"))
    query_city = normalize_lower(query.get("city"))

    for label, value, points in (
        ("регион", query_region, 15),
        ("страна", query_country, 10),
        ("город", query_city, 18),
    ):
        if value and value != "неясно" and (value == channel_region or text_matches(value, haystack)):
            score += points
            matched.append(f"{label}: {value}")

    query_format = normalize_lower(query.get("format"))
    channel_format = normalize_lower(channel.get("content_format") or classification.get("формат"))
    if query_format and channel_format == query_format:
        score += 8
        matched.append(f"формат: {query_format}")

    query_position = normalize_lower(query.get("position"))
    channel_position = normalize_lower(channel.get("position_label") or classification.get("позиция") or classification.get("позиция подачи"))
    if query_position and channel_position == query_position:
        score += 15
        matched.append(f"позиция: {query_position}")

    query_channel_type = normalize_lower(query.get("channel_type"))
    if query_channel_type:
        channel_type_values = values_from_classification(classification, ["тип охвата", "тип канала", "channel_type"])
        channel_type_text = " ".join(channel_type_values).lower().replace("ё", "е")
        if query_channel_type in channel_type_text or text_matches(query_channel_type, haystack):
            score += 8
            matched.append(f"тип канала: {query_channel_type}")

    for additional_category in query.get("additional_categories") or []:
        if text_matches(additional_category, haystack):
            score += 10
            matched.append(f"доп. категория: {additional_category}")

    for keyword in query.get("keywords") or []:
        if text_matches(keyword, haystack):
            score += 10
            matched.append(f"keyword: {keyword}")

    for topic in query.get("topics") or []:
        if text_matches(topic, haystack):
            score += 6
            matched.append(f"тема: {topic}")

    for obj in query.get("objects") or []:
        if text_matches(obj, haystack):
            score += 7
            matched.append(f"объект: {obj}")

    for negative in query.get("negative_keywords") or []:
        if text_matches(negative, haystack):
            score -= 20
            matched.append(f"минус: {negative}")

    confidence = channel.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence = 0.0

    if confidence >= 0.75:
        score += 3

    return {
        "channel_id": channel.get("channel_id"),
        "username": channel.get("username"),
        "title": channel.get("title"),
        "score": score,
        "matched": matched[:12],
        "description": channel.get("ai_description"),
        "category": channel_category,
        "region": channel_region,
        "format": channel_format,
    }


def rank_channels(query: dict[str, Any], channels: list[dict[str, Any]], limit: int = TOP_CHANNELS_LIMIT) -> list[dict[str, Any]]:
    ranked = [rank_channel(query, channel) for channel in channels]
    ranked = [item for item in ranked if item["score"] >= MIN_CHANNEL_SCORE]
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return ranked[:limit]


def print_results(user_query: str, query_markup: dict[str, Any], results: list[dict[str, Any]]) -> None:
    print("\n=== Запрос пользователя ===")
    print(user_query)

    print("\n=== ИИ-разбор запроса ===")
    print(json.dumps(query_markup, ensure_ascii=False, indent=2))

    if not results:
        print("\nПодходящие каналы не найдены. Можно снизить MIN_CHANNEL_SCORE или добавить размеченные каналы.")
        return

    print("\n=== Топ каналов ===")

    for index, item in enumerate(results, start=1):
        print(f"\n{index}. @{item['username']} — {item.get('title') or ''}")
        print(f"   score: {item['score']}")
        print(f"   category: {item.get('category')}, region: {item.get('region')}, format: {item.get('format')}")

        if item.get("description"):
            print(f"   description: {item['description']}")

        if item.get("matched"):
            print("   matched: " + "; ".join(item["matched"]))


async def main() -> None:
    if len(sys.argv) > 1:
        user_query = " ".join(sys.argv[1:]).strip()
    else:
        user_query = input("Введите запрос пользователя: ").strip()

    if not user_query:
        print("Пустой запрос.")
        return

    query_markup = await analyze_user_query(user_query)

    pool = await create_db_pool()
    try:
        channels = await load_marked_channels(pool)
    finally:
        await pool.close()

    print(f"\nРазмеченных каналов загружено из БД: {len(channels)}")

    results = rank_channels(query_markup, channels)
    print_results(user_query, query_markup, results)


if __name__ == "__main__":
    asyncio.run(main())
