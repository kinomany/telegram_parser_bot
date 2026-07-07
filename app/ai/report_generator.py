import json
import os
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
    REPORT_MAX_MESSAGE_CHARS,
    REPORT_TOTAL_MAX_CHARS,
)

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY не найден в .env")

client = genai.Client(api_key=GEMINI_API_KEY)

REPORT_SYSTEM_PROMPT = """
Верни только JSON без markdown и без пояснений.

Ты делаешь краткий аналитический отчёт по Telegram-сообщениям.
Главное правило: если сообщения НЕ отвечают на вопрос пользователя, НЕ делай отчёт.
В этом случае верни ok=false и понятную причину.

Нельзя:
- выдумывать факты;
- добавлять внешние знания;
- делать общий обзор темы, если в сообщениях нет нужной информации;
- скрывать, что данных мало;
- делать отчёт, если сообщения случайные или не по вопросу.

Можно использовать только данные из переданных сообщений.
Сообщения пронумерованы. Если указываешь факт, желательно добавлять ссылку на номер сообщения: [1], [2].

Схема ответа:
{
  "ok": true,
  "relevance": "high",
  "title": "Короткий заголовок отчёта",
  "summary": "Краткий вывод в 2-4 предложениях.",
  "key_points": [
    "Главный пункт с опорой на сообщения [1].",
    "Второй пункт [2]."
  ],
  "details": [
    "Подробность или контекст [1][3]."
  ],
  "uncertainty": [
    "Что неясно или где данных недостаточно."
  ],
  "used_sources": [1, 2, 3]
}

Если сообщения не по вопросу:
{
  "ok": false,
  "relevance": "low",
  "error": "Сообщения не отвечают на вопрос пользователя: ...",
  "used_sources": []
}

Оцени релевантность строго:
- high: большинство сообщений явно про вопрос;
- medium: часть сообщений полезна, но данных мало;
- low: сообщения в основном не по вопросу.

Если relevance=low — ok должен быть false.
""".strip()


REPORT_PRESET_TITLES = {
    "brief": "Краткая сводка",
    "story": "Общий сюжет",
    "compare_sources": "Сравнение источников",
    "practical": "Практическая польза",
    "urgent": "Срочное / происшествие",
    "facts_only": "Только факты",
}

REPORT_PRESET_PROMPTS = {
    "brief": """
ПРЕСЕТ: КРАТКАЯ СВОДКА

Дай короткую сводку по найденным сообщениям.
Фокус: что произошло, главные события, важные детали.
Без глубокой аналитики, без попытки насильно искать общий сюжет.
Если сообщения про разные темы внутри запроса — аккуратно раздели их.
""".strip(),

    "story": """
ПРЕСЕТ: ОБЩИЙ СЮЖЕТ

Попробуй собрать найденные сообщения в общую картину.
Фокус: какие события связаны между собой, как развивается история, какие причинно-следственные связи прямо видны из сообщений.
Если связи нет — не выдумывай её, прямо напиши, что сообщения дают отдельные линии.
""".strip(),

    "compare_sources": """
ПРЕСЕТ: СРАВНЕНИЕ ИСТОЧНИКОВ

Сравни, как разные источники освещают тему.
Фокус: где источники согласны, где расходятся, какие факты подтверждаются несколькими каналами, какие утверждения есть только у одного источника.
Не усредняй позиции разных источников.
""".strip(),

    "practical": """
ПРЕСЕТ: ПРАКТИЧЕСКАЯ ПОЛЬЗА

Вытащи из сообщений практическую пользу для пользователя.
Фокус: что можно сделать, на что обратить внимание, какие риски упоминаются, чего не хватает для вывода.
Не превращай сообщения в медицинскую, юридическую или финансовую консультацию. Пиши осторожно: "в сообщениях говорится", "источник советует".
""".strip(),

    "urgent": """
ПРЕСЕТ: СРОЧНОЕ / ПРОИСШЕСТВИЕ

Подходит для ЧП, атак, аварий, задержаний, пожаров, катастроф и срочных новостей.
Фокус: что произошло, где и когда, кого затронуло, что подтверждено, что пока неясно.
Отделяй подтверждённое от неподтверждённого. Не добавляй эмоции и внешние данные.
""".strip(),

    "facts_only": """
ПРЕСЕТ: ТОЛЬКО ФАКТЫ

Без анализа, без интерпретаций, без общего вывода.
Просто перечисли факты из сообщений. Каждый важный факт должен иметь ссылку на сообщение [1], [2].
Если факт есть только в одном сообщении — не представляй его как подтверждённый несколькими источниками.
""".strip(),
}


def normalize_report_preset(report_preset: str | None) -> str:
    report_preset = (report_preset or "brief").strip().lower()
    return report_preset if report_preset in REPORT_PRESET_PROMPTS else "brief"


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


def build_report_prompt(
    user_query: str,
    query_markup: dict,
    ranked_messages: list[dict],
    report_preset: str = "brief",
) -> str:
    report_preset = normalize_report_preset(report_preset)
    preset_title = REPORT_PRESET_TITLES.get(report_preset, report_preset)
    preset_prompt = REPORT_PRESET_PROMPTS[report_preset]
    query_markup_text = json.dumps(query_markup or {}, ensure_ascii=False, indent=2)

    messages_text = ""
    total_chars = 0

    for index, item in enumerate(ranked_messages, start=1):
        username = item.get("username") or ""
        title = item.get("title") or username
        date_text = item.get("date_text") or "без даты"
        score = item.get("score", 0)
        matched = "; ".join(item.get("matched") or [])
        cleaned_text = cut_text(item.get("cleaned_text") or "", REPORT_MAX_MESSAGE_CHARS)

        block = (
            f"\nСообщение [{index}]\n"
            f"Канал: {title} ({username})\n"
            f"Дата: {date_text}\n"
            f"Score: {score}\n"
            f"Почему выбрано: {matched}\n"
            f"Текст:\n{cleaned_text}\n"
        )

        if total_chars + len(block) > REPORT_TOTAL_MAX_CHARS:
            break

        messages_text += block
        total_chars += len(block)

    return f"""
Вопрос пользователя:
{user_query}

ИИ-разбор запроса:
{query_markup_text}

Топ сообщений по локальному score:
{messages_text}

Выбранный пресет отчёта:
{preset_title}

Инструкция пресета:
{preset_prompt}

Проверь, отвечают ли эти сообщения на вопрос пользователя.
Если нет — верни ok=false.
Если да — сделай аккуратный отчёт по схеме и с учётом выбранного пресета.
""".strip()


async def create_ai_report(
    user_query: str,
    query_markup: dict,
    ranked_messages: list[dict],
    report_preset: str = "brief",
) -> dict[str, Any]:
    report_preset = normalize_report_preset(report_preset)
    if not ranked_messages:
        return {
            "ok": False,
            "error": "Нет сообщений для отчёта.",
            "relevance": "low",
            "model_used": None,
            "report_preset": report_preset,
        }

    prompt = build_report_prompt(
        user_query=user_query,
        query_markup=query_markup,
        ranked_messages=ranked_messages,
        report_preset=report_preset,
    )
    last_error = "неизвестная ошибка"

    for model_name in GEMINI_REPORT_MODELS:
        try:
            async with runtime.ai_api_semaphore:
                response = await client.aio.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=REPORT_SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )

            data = parse_json_response(response.text or "")

            if not data:
                last_error = f"{model_name}: модель вернула не JSON"
                continue

            data["model_used"] = model_name
            data["report_preset"] = report_preset

            if data.get("relevance") == "low":
                data["ok"] = False

            if data.get("ok") is not True:
                data["ok"] = False
                data.setdefault("error", "ИИ решила, что сообщения не отвечают на вопрос.")

            return data

        except Exception as exc:
            last_error = f"{model_name}: {type(exc).__name__}: {exc}"

    return {
        "ok": False,
        "error": f"Не смог создать отчёт через ИИ. Последняя ошибка: {last_error}",
        "relevance": "low",
        "model_used": None,
        "report_preset": report_preset,
    }


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


def format_ai_report_for_user(report: dict[str, Any]) -> str:
    if not report.get("ok"):
        error = report.get("error") or "ИИ решила не делать отчёт по этим сообщениям."
        relevance = report.get("relevance") or "low"
        model_used = report.get("model_used")
        report_preset = normalize_report_preset(report.get("report_preset"))
        report_preset_title = REPORT_PRESET_TITLES.get(report_preset, report_preset)

        text = "⚠️ Отчёт не создан\n\n"
        text += f"Причина: {error}\n"
        text += f"Релевантность: {relevance}\n"
        text += f"Пресет: {report_preset_title}\n"

        if model_used:
            text += f"Модель: {model_used}\n"

        return text.strip()

    title = report.get("title") or "Отчёт по найденным сообщениям"
    summary = report.get("summary") or ""
    key_points = normalize_string_list(report.get("key_points"), limit=8)
    details = normalize_string_list(report.get("details"), limit=8)
    uncertainty = normalize_string_list(report.get("uncertainty"), limit=5)
    relevance = report.get("relevance") or "medium"
    model_used = report.get("model_used")
    report_preset = normalize_report_preset(report.get("report_preset"))
    report_preset_title = REPORT_PRESET_TITLES.get(report_preset, report_preset)

    text = f"📄 {title}\n\n"

    if summary:
        text += f"Кратко:\n{summary}\n\n"

    if key_points:
        text += "Главное:\n"
        for index, point in enumerate(key_points, start=1):
            text += f"{index}. {point}\n"
        text += "\n"

    if details:
        text += "Подробности:\n"
        for detail in details:
            text += f"• {detail}\n"
        text += "\n"

    if uncertainty:
        text += "Что неясно / ограничение данных:\n"
        for item in uncertainty:
            text += f"• {item}\n"
        text += "\n"

    text += f"Релевантность: {relevance}\n"
    text += f"Пресет: {report_preset_title}\n"

    if model_used:
        text += f"Модель: {model_used}\n"

    return text.strip()
