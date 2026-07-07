import json
import re
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from app.bot import runtime

load_dotenv()

from config import (              
    GEMINI_API_KEY,
    GEMINI_REPORT_MODELS,
    DIGEST_MAX_MESSAGE_CHARS,
    DIGEST_CHANNEL_TOTAL_MAX_CHARS,
    DIGEST_FINAL_TOTAL_MAX_CHARS,
)

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY не найден в .env")

client = genai.Client(api_key=GEMINI_API_KEY)

CHANNEL_DIGEST_SYSTEM_PROMPT = """
Верни только JSON без markdown и без пояснений.

Ты делаешь сводку по одному Telegram-каналу за выбранный период.
Пользователь хочет понять, что он пропустил именно в этом канале.

Правила:
- не делай общий дайджест по всем каналам;
- смотри только на сообщения этого канала;
- не пересказывай каждое сообщение подряд;
- объединяй повторы внутри канала в один сюжет;
- отделяй главные события от коротких обновлений;
- не добавляй внешние знания;
- не делай громких выводов, если в сообщениях мало фактов;
- если важного мало, честно скажи об этом;
- указывай номера сообщений, на которых основаны выводы: [1], [2].

Если канал в основном повторял одну тему — выдели одну главную тему, не растягивай список искусственно.
Если сообщения почти полностью мусорные или полезных данных мало — ok=false.

Схема ответа:
{
  "ok": true,
  "channel": "@channel",
  "title": "Название канала",
  "summary": "Кратко, что важного было в канале за период.",
  "main_events": [
    {
      "title": "Название сюжета или события",
      "summary": "Суть события без внешних фактов.",
      "source_messages": [1, 2]
    }
  ],
  "minor_items": [
    "Второстепенная заметка, если она реально полезна."
  ],
  "low_value_items": [
    "Что можно было не включать: короткие повторы, анонсы, сообщения без нового факта."
  ],
  "importance": "high|medium|low",
  "used_sources": [1, 2, 3]
}

Если полезных сообщений недостаточно:
{
  "ok": false,
  "channel": "@channel",
  "error": "Недостаточно полезных сообщений для сводки канала.",
  "importance": "low",
  "used_sources": []
}
""".strip()

FINAL_DIGEST_SYSTEM_PROMPT = """
Верни только JSON без markdown и без пояснений.

Ты делаешь общий дайджест по сводкам Telegram-каналов.
На входе НЕ исходные сообщения, а уже готовые сводки по каждому каналу.

Задача:
- собрать общую картину за период;
- объединить одинаковые события из разных каналов;
- не повторять одно и то же несколько раз;
- если событие встречается в нескольких каналах, показать список каналов;
- если событие есть только в одном канале, можно включить его, но не называй общей тенденцией;
- показать разные акценты каналов, если они реально есть;
- не придумывать внешние факты;
- не делай блок пересечений, если пересечений нет;
- если данных мало, сделай короткий дайджест и прямо укажи ограничение.

Схема ответа:
{
  "ok": true,
  "title": "Короткий заголовок дайджеста",
  "summary": "Краткий вывод в 2-4 предложениях.",
  "main_events": [
    {
      "title": "Главное событие или сюжет",
      "summary": "Суть события по канальным сводкам.",
      "channels": ["@channel1", "@channel2"]
    }
  ],
  "by_channel": [
    {
      "channel": "@channel",
      "summary": "Что было важного именно в этом канале."
    }
  ],
  "multi_channel_events": [
    {
      "title": "Событие, о котором писали минимум два канала",
      "channels": ["@channel1", "@channel2"],
      "summary": "Общий смысл и различия акцентов."
    }
  ],
  "different_angles": [
    "Разница в акцентах каналов, если она реально видна."
  ],
  "uncertainty": [
    "Что неясно или где мало данных."
  ]
}

Если нет полезных канальных сводок:
{
  "ok": false,
  "error": "Недостаточно полезных канальных сводок для общего дайджеста."
}
""".strip()


DIGEST_PRESET_TITLES = {
    "brief": "⚡ Только главное",
    "normal": "🧾 Обычная сводка",
    "detailed": "📚 Подробная сводка",
}

CHANNEL_DIGEST_PRESET_PROMPTS = {
    "brief": """
Формат: ⚡ Только главное.
Сделай максимально короткую сводку.
- summary: 1-2 предложения;
- main_events: 1-3 самых важных сюжета;
- minor_items: пустой список, если без них можно понять картину;
- low_value_items: только если это действительно важно как ограничение;
- не растягивай ответ.
""".strip(),
    "normal": """
Формат: 🧾 Обычная сводка.
Сделай сбалансированную сводку.
- summary: 2-4 предложения;
- main_events: 3-6 важных сюжетов;
- minor_items: только полезные второстепенные пункты;
- low_value_items: коротко, если были заметные повторы/мусор/анонсы.
""".strip(),
    "detailed": """
Формат: 📚 Подробная сводка.
Сделай более развёрнутую сводку.
- summary: 3-6 предложений;
- main_events: до 8 важных сюжетов;
- minor_items: можно включить полезные второстепенные новости;
- low_value_items: укажи, что было несущественным или повторяющимся;
- добавь больше контекста из сообщений, но не добавляй внешние знания.
""".strip(),
}

FINAL_DIGEST_PRESET_PROMPTS = {
    "brief": """
Формат: ⚡ Только главное.
Сделай короткое общее резюме.
- summary: 1-2 предложения;
- main_events: 2-4 ключевых события;
- by_channel: только если без этого теряются важные различия;
- multi_channel_events/different_angles: только если пересечения явно есть.
""".strip(),
    "normal": """
Формат: 🧾 Обычная сводка.
Сделай нормальный общий дайджест.
- summary: 2-4 предложения;
- main_events: 3-7 главных сюжетов;
- by_channel: кратко по каждому каналу;
- multi_channel_events: только реальные пересечения между каналами;
- uncertainty: коротко, если есть ограничения.
""".strip(),
    "detailed": """
Формат: 📚 Подробная сводка.
Сделай более подробный общий дайджест.
- summary: 3-6 предложений;
- main_events: до 10 сюжетов;
- by_channel: обязательно кратко по каждому каналу;
- multi_channel_events: отдельно покажи пересечения;
- different_angles: покажи разные акценты каналов, если видны;
- uncertainty: укажи ограничения и спорные места.
""".strip(),
}


def normalize_digest_preset(value: str | None) -> str:
    preset = (value or "normal").strip().lower()
    return preset if preset in DIGEST_PRESET_TITLES else "normal"


def get_digest_preset_title(value: str | None) -> str:
    return DIGEST_PRESET_TITLES.get(normalize_digest_preset(value), DIGEST_PRESET_TITLES["normal"])


def get_channel_digest_system_prompt(digest_preset: str | None) -> str:
    preset = normalize_digest_preset(digest_preset)
    return CHANNEL_DIGEST_SYSTEM_PROMPT + "\n\n" + CHANNEL_DIGEST_PRESET_PROMPTS[preset]


def get_final_digest_system_prompt(digest_preset: str | None) -> str:
    preset = normalize_digest_preset(digest_preset)
    return FINAL_DIGEST_SYSTEM_PROMPT + "\n\n" + FINAL_DIGEST_PRESET_PROMPTS[preset]


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


def cut_text(text: str, limit: int) -> str:
    text = re.sub(r"\s{2,}", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def normalize_string_list(value: Any, limit: int = 10) -> list[str]:
    if not isinstance(value, list):
        return []

    result = []
    for item in value:
        if not isinstance(item, str):
            continue
        item = re.sub(r"\s{2,}", " ", item).strip()
        if not item:
            continue
        result.append(item)
        if len(result) >= limit:
            break

    return result


def normalize_dict_list(value: Any, limit: int = 10) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            result.append(item)
        if len(result) >= limit:
            break
    return result


def build_channel_digest_prompt(
    channel: dict,
    channel_messages: list[dict],
    period_label: str,
    digest_preset: str | None = "normal",
) -> str:
    username = channel.get("username") or ""
    title = channel.get("title") or username
    category = channel.get("user_category") or ""
    digest_preset = normalize_digest_preset(digest_preset)
    preset_title = get_digest_preset_title(digest_preset)

    messages_text = ""
    total_chars = 0

    for index, item in enumerate(channel_messages, start=1):
        date_text = item.get("date_text") or "без даты"
        cleaned_text = cut_text(item.get("cleaned_text") or "", DIGEST_MAX_MESSAGE_CHARS)

        block = (
            f"\nСообщение [{index}]\n"
            f"Дата: {date_text}\n"
            f"Текст:\n{cleaned_text}\n"
        )

        if total_chars + len(block) > DIGEST_CHANNEL_TOTAL_MAX_CHARS:
            break

        messages_text += block
        total_chars += len(block)

    return f"""
Период сводки:
{period_label}

Канал:
{title} ({username})
Категория пользователя: {category or "не указана"}
Формат сводки: {preset_title}

Сообщения канала за период:
{messages_text}

Сделай сводку только по этому каналу.
Не пересказывай каждое сообщение. Сгруппируй повторы и выдели главное.
""".strip()


async def create_channel_digest(
    channel: dict,
    channel_messages: list[dict],
    period_label: str,
    digest_preset: str | None = "normal",
) -> dict[str, Any]:
    username = channel.get("username") or ""
    title = channel.get("title") or username

    if not channel_messages:
        return {
            "ok": False,
            "channel": username,
            "title": title,
            "error": "Нет сообщений для сводки канала.",
            "importance": "low",
            "model_used": None,
            "digest_preset": normalize_digest_preset(digest_preset),
            "used_sources": [],
        }

    prompt = build_channel_digest_prompt(
        channel=channel,
        channel_messages=channel_messages,
        period_label=period_label,
        digest_preset=digest_preset,
    )
    last_error = "неизвестная ошибка"

    for model_name in GEMINI_REPORT_MODELS:
        try:
            async with runtime.ai_api_semaphore:
                response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=get_channel_digest_system_prompt(digest_preset),
                    response_mime_type="application/json",
                    temperature=0.15,
                ),
            )

            data = parse_json_response(response.text or "")

            if not data:
                last_error = f"{model_name}: модель вернула не JSON"
                continue

            data["channel"] = data.get("channel") or username
            data["title"] = data.get("title") or title
            data["model_used"] = model_name
            data["messages_count"] = len(channel_messages)
            data["digest_preset"] = normalize_digest_preset(digest_preset)

            if data.get("ok") is not True:
                data["ok"] = False
                data.setdefault("error", "ИИ решила, что полезных сообщений для сводки канала недостаточно.")
                data.setdefault("importance", "low")

            return data

        except Exception as exc:
            last_error = f"{model_name}: {type(exc).__name__}: {exc}"

    return {
        "ok": False,
        "channel": username,
        "title": title,
        "error": f"Не смог создать сводку канала через ИИ. Последняя ошибка: {last_error}",
        "importance": "low",
        "model_used": None,
        "digest_preset": normalize_digest_preset(digest_preset),
        "messages_count": len(channel_messages),
        "used_sources": [],
    }


def build_final_digest_prompt(
    selected_channels: list[dict],
    channel_digests: list[dict],
    period_label: str,
    digest_preset: str | None = "normal",
) -> str:
    digest_preset = normalize_digest_preset(digest_preset)
    preset_title = get_digest_preset_title(digest_preset)

    channels_text = ""
    for index, channel in enumerate(selected_channels, start=1):
        username = channel.get("username") or ""
        title = channel.get("title") or username
        category = channel.get("user_category") or ""
        channels_text += f"{index}. {title} ({username})"
        if category:
            channels_text += f" | категория: {category}"
        channels_text += "\n"

    digests_text = ""
    total_chars = 0

    for index, digest in enumerate(channel_digests, start=1):
        username = digest.get("channel") or ""
        title = digest.get("title") or username
        ok = digest.get("ok") is True
        importance = digest.get("importance") or "unknown"
        summary = digest.get("summary") or digest.get("error") or ""
        main_events = digest.get("main_events") or []
        minor_items = digest.get("minor_items") or []

        block = (
            f"\nКанальная сводка [{index}]\n"
            f"Канал: {title} ({username})\n"
            f"ok: {ok}\n"
            f"importance: {importance}\n"
            f"Кратко: {summary}\n"
        )

        if isinstance(main_events, list) and main_events:
            block += "Главные события:\n"
            for event in main_events[:8]:
                if isinstance(event, dict):
                    event_title = event.get("title") or "событие"
                    event_summary = event.get("summary") or ""
                    block += f"- {event_title}: {event_summary}\n"
                elif isinstance(event, str):
                    block += f"- {event}\n"

        if isinstance(minor_items, list) and minor_items:
            block += "Второстепенное:\n"
            for item in minor_items[:5]:
                block += f"- {item}\n"

        if total_chars + len(block) > DIGEST_FINAL_TOTAL_MAX_CHARS:
            break

        digests_text += block
        total_chars += len(block)

    return f"""
Период общего дайджеста:
{period_label}

Формат общего дайджеста:
{preset_title}

Выбранные каналы:
{channels_text}

Канальные сводки:
{digests_text}

Сделай общий дайджест по канальным сводкам.
Объединяй одинаковые события. Не повторяй один сюжет несколько раз.
Если несколько каналов писали об одном событии — покажи это как один пункт и перечисли каналы.
""".strip()


async def create_final_digest_report(
    selected_channels: list[dict],
    channel_digests: list[dict],
    period_label: str,
    digest_preset: str | None = "normal",
) -> dict[str, Any]:
    useful_digests = [item for item in channel_digests if item.get("ok") is True]

    if not useful_digests:
        return {
            "ok": False,
            "error": "Нет полезных канальных сводок для общего дайджеста.",
            "model_used": None,
        }

    prompt = build_final_digest_prompt(
        selected_channels=selected_channels,
        channel_digests=useful_digests,
        period_label=period_label,
        digest_preset=digest_preset,
    )
    last_error = "неизвестная ошибка"

    for model_name in GEMINI_REPORT_MODELS:
        try:
            async with runtime.ai_api_semaphore:
                response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=get_final_digest_system_prompt(digest_preset),
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )

            data = parse_json_response(response.text or "")

            if not data:
                last_error = f"{model_name}: модель вернула не JSON"
                continue

            data["model_used"] = model_name
            data["channel_digests_count"] = len(useful_digests)
            data["digest_preset"] = normalize_digest_preset(digest_preset)

            if data.get("ok") is not True:
                data["ok"] = False
                data.setdefault("error", "ИИ решила, что полезных канальных сводок недостаточно.")

            return data

        except Exception as exc:
            last_error = f"{model_name}: {type(exc).__name__}: {exc}"

    return {
        "ok": False,
        "error": f"Не смог создать общий дайджест через ИИ. Последняя ошибка: {last_error}",
        "model_used": None,
        "digest_preset": normalize_digest_preset(digest_preset),
    }


                                                                          
                                                                                       
async def create_digest_report(
    selected_channels: list[dict],
    digest_messages: list[dict],
    period_label: str,
    digest_preset: str | None = "normal",
) -> dict[str, Any]:
    channel_by_username = {
        (channel.get("username") or "").strip().lstrip("@").lower(): channel
        for channel in selected_channels
    }
    messages_by_username: dict[str, list[dict]] = {}

    for item in digest_messages:
        key = (item.get("username") or "").strip().lstrip("@").lower()
        if key:
            messages_by_username.setdefault(key, []).append(item)

    channel_digests = []
    for key, channel in channel_by_username.items():
        channel_digests.append(await create_channel_digest(channel, messages_by_username.get(key) or [], period_label, digest_preset=digest_preset))

    return await create_final_digest_report(selected_channels, channel_digests, period_label, digest_preset=digest_preset)


def format_channel_digest_for_user(digest: dict[str, Any]) -> str:
    channel = digest.get("channel") or digest.get("username") or "канал"
    title = digest.get("title") or channel
    model_used = digest.get("model_used")
    messages_count = digest.get("messages_count")
    digest_preset = digest.get("digest_preset")

    if digest.get("ok") is not True:
        error = digest.get("error") or "ИИ решила не делать сводку по этому каналу."
        text = f"📌 {title} ({channel})\n"
        text += "⚠️ Сводка канала не создана\n"
        text += f"Причина: {error}\n"
        if messages_count is not None:
            text += f"Сообщений: {messages_count}\n"
        if model_used:
            text += f"Модель: {model_used}\n"
        return text.strip()

    summary = digest.get("summary") or ""
    main_events = normalize_dict_list(digest.get("main_events"), limit=8)
    minor_items = normalize_string_list(digest.get("minor_items"), limit=6)
    low_value_items = normalize_string_list(digest.get("low_value_items"), limit=5)
    importance = digest.get("importance") or "medium"

    text = f"📌 {title} ({channel})\n"
    if digest_preset:
        text += f"Формат: {get_digest_preset_title(digest_preset)}\n"

    if summary:
        text += f"Кратко:\n{summary}\n\n"

    if main_events:
        text += "Главное в канале:\n"
        for index, event in enumerate(main_events, start=1):
            event_title = event.get("title") or "Сюжет"
            event_summary = event.get("summary") or ""
            source_messages = event.get("source_messages") or []
            sources_text = ""
            if isinstance(source_messages, list) and source_messages:
                sources_text = " " + "".join(f"[{item}]" for item in source_messages[:6])
            text += f"{index}. {event_title}{sources_text}\n"
            if event_summary:
                text += f"   {event_summary}\n"
        text += "\n"

    if minor_items:
        text += "Второстепенное:\n"
        for item in minor_items:
            text += f"• {item}\n"
        text += "\n"

    if low_value_items:
        text += "Что можно пропустить:\n"
        for item in low_value_items:
            text += f"• {item}\n"
        text += "\n"

    text += f"Важность канала за период: {importance}\n"
    if messages_count is not None:
        text += f"Сообщений в сводке: {messages_count}\n"
    if model_used:
        text += f"Модель: {model_used}\n"

    return text.strip()


def format_digest_report_for_user(report: dict[str, Any]) -> str:
    if not report.get("ok"):
        error = report.get("error") or "ИИ решила не делать дайджест."
        model_used = report.get("model_used")

        text = "⚠️ Дайджест не создан\n\n"
        text += f"Причина: {error}\n"
        if model_used:
            text += f"Модель: {model_used}\n"
        return text.strip()

    title = report.get("title") or "Дайджест по выбранным каналам"
    summary = report.get("summary") or ""
    main_events = normalize_dict_list(report.get("main_events"), limit=10)
    by_channel = normalize_dict_list(report.get("by_channel"), limit=12)
    multi_channel_events = normalize_dict_list(report.get("multi_channel_events"), limit=8)
    different_angles = normalize_string_list(report.get("different_angles"), limit=6)
    uncertainty = normalize_string_list(report.get("uncertainty"), limit=5)
    model_used = report.get("model_used")
    digest_preset = report.get("digest_preset")

    text = f"🧾 {title}\n"
    if digest_preset:
        text += f"Формат: {get_digest_preset_title(digest_preset)}\n"
    text += "\n"

    if summary:
        text += f"Коротко:\n{summary}\n\n"

    if main_events:
        text += "Главное:\n"
        for index, event in enumerate(main_events, start=1):
            event_title = event.get("title") or "Событие"
            event_summary = event.get("summary") or ""
            channels = event.get("channels") or []
            channels_text = ", ".join(str(item) for item in channels[:8]) if isinstance(channels, list) else ""
            text += f"{index}. {event_title}"
            if channels_text:
                text += f"\n   Писали: {channels_text}"
            if event_summary:
                text += f"\n   {event_summary}"
            text += "\n"
        text += "\n"

    if multi_channel_events:
        text += "События в нескольких каналах:\n"
        for item in multi_channel_events:
            title = item.get("title") or "Событие"
            channels = item.get("channels") or []
            summary = item.get("summary") or ""
            channels_text = ", ".join(str(x) for x in channels[:8]) if isinstance(channels, list) else ""
            if channels_text:
                text += f"• {title} — {channels_text}"
            else:
                text += f"• {title}"
            if summary:
                text += f": {summary}"
            text += "\n"
        text += "\n"

    if by_channel:
        text += "По каналам:\n"
        for item in by_channel:
            channel = item.get("channel") or item.get("username") or "канал"
            summary = item.get("summary") or item.get("text") or ""
            if summary:
                text += f"• {channel}: {summary}\n"
        text += "\n"

    if different_angles:
        text += "Разные акценты:\n"
        for item in different_angles:
            text += f"• {item}\n"
        text += "\n"

    if uncertainty:
        text += "Что неясно / ограничения:\n"
        for item in uncertainty:
            text += f"• {item}\n"
        text += "\n"

    if model_used:
        text += f"Модель: {model_used}\n"

    return text.strip()
