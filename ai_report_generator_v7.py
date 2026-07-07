from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


                           
                        
                           

DIGEST_PROMPT_VERSION = "v7"

MAX_MESSAGES_PER_CHANNEL_DIRECT = 120
MAX_MESSAGE_CHARS_FOR_DIGEST = 4500
MAX_TOTAL_CHARS_DIRECT_DIGEST = 180_000


@dataclass(slots=True)
class DigestMessage:
    """Удобная структура для сообщения, которое уходит в ИИ."""

    text: str
    msg_id: int | str | None = None
    date: str | None = None
    views: int | None = None
    forwards: int | None = None
    link: str | None = None


@dataclass(slots=True)
class ChannelDigestInput:
    """Данные одного канала для дайджеста."""

    username: str
    title: str | None
    period_label: str
    messages: list[DigestMessage]
    total_collected: int | None = None
    total_clean: int | None = None


@dataclass(slots=True)
class GeneratedChannelDigest:
    """Готовый дайджест одного канала после ответа ИИ."""

    username: str
    title: str | None
    digest_text: str
    total_collected: int | None = None
    total_clean: int | None = None



                           
                
                           

DIGEST_SYSTEM_PROMPT_V7 = """
Ты делаешь полезный Telegram-дайджест по сообщениям канала.

Главная задача: не писать красивую общую болтовню, а извлечь максимум полезной информации из сообщений.

Работай строго по данным из сообщений:
- не выдумывай факты;
- не добавляй внешние знания;
- не делай выводы, если в сообщениях нет основания;
- если факт спорный или это чья-то оценка, так и пиши: «по версии канала», «канал утверждает», «автор считает»;
- сохраняй важные имена, организации, города, страны, даты, суммы, проценты, числа, решения, последствия;
- объединяй повторы, но не теряй детали;
- важные одноразовые факты тоже включай, если они содержательные.

Не надо:
- писать воду вроде «канал активно освещал события»;
- пересказывать каждое сообщение отдельно, если они про одно и то же;
- делать эмоциональную публицистику;
- писать «в заключение»;
- ставить ссылки, если они не были переданы в данных.

Формат:
- пиши на русском;
- используй короткие абзацы;
- структура должна быть удобна для Telegram;
- если данных мало, честно скажи, что сообщений мало;
- номера источников указывай в квадратных скобках: [1], [2], [3].
""".strip()


SINGLE_CHANNEL_DIGEST_PROMPT_V7 = """
Сделай дайджест по одному Telegram-каналу за период.

Канал: {channel_title}
Username: @{username}
Период: {period_label}
Сообщений собрано: {total_collected}
Сообщений после очистки: {total_clean}

Ниже идут очищенные сообщения за период. Это НЕ выборка лучших сообщений, а все сообщения, которые остались после фильтров.

{messages_block}

Требования к ответу:

1. Начни с заголовка:
🧾 Дайджест: {channel_title_or_username}

2. Затем дай блок:
Коротко:
2-4 предложения. Не общими словами, а с конкретной сутью периода.

3. Затем блок:
Главное:
5-10 пунктов. Каждый пункт должен содержать конкретный факт, событие, решение, заявление, цифру или последствие.
Формат пункта:
1. Что произошло / что заявили / что изменилось — почему это важно [источники].

4. Затем блок:
Подробности:
Разбей по темам, если тем несколько. Внутри каждой темы дай полезные детали: кто, где, когда, сколько, последствия, контекст из сообщений.
Если тема одна — всё равно дай подробный связный разбор.

5. Затем блок:
Цифры и факты:
Выпиши отдельным списком все важные числа, даты, суммы, проценты, сроки, количества, географию. Если чисел нет, не выдумывай и напиши: «Значимых цифр в сообщениях мало».

6. Затем блок:
Кто упоминался:
Список людей, организаций, стран/регионов/городов, если они важны для понимания дайджеста. Рядом кратко: в каком контексте.

7. Затем блок:
Что изменилось / к чему это ведёт:
Только выводы, которые прямо следуют из сообщений. Если последствий в сообщениях нет — напиши это честно.

8. Затем блок:
Источники:
Кратко перечисли использованные сообщения:
[1] дата — короткая суть
[2] дата — короткая суть

Жёсткие правила:
- Не пропускай полезные детали из сообщений ради красивой краткости.
- Не делай только «топ новостей», дай нормальную информационную выжимку.
- Если несколько сообщений об одном событии, объединяй их в один сюжет и перечисляй источники вместе: [2], [5], [8].
- Если сообщения противоречат друг другу, отдельно отметь противоречие.
- Если в сообщениях есть только заявления без подтверждения, не превращай их в доказанный факт.
""".strip()


CHANNEL_DIGEST_PROMPT_V7 = """
Сделай отдельный дайджест по одному каналу. Этот текст потом будет использован в общем резюме по нескольким каналам.

Канал: {channel_title}
Username: @{username}
Период: {period_label}
Сообщений собрано: {total_collected}
Сообщений после очистки: {total_clean}

Сообщения:

{messages_block}

Требования к ответу:

📌 @{username} — {channel_title_or_username}

Коротко:
2-3 предложения с главной сутью канала за период.

Главные сюжеты:
1. Сюжет / событие — конкретные детали, цифры, участники, последствия [источники].
2. ...

Важные детали:
- факты, которые легко потерять при кратком пересказе;
- даты, цифры, география;
- кто что заявил/сделал;
- что канал подчёркивает особенно.

Акцент канала:
Кратко опиши, на чём канал сделал главный акцент. Не придумывай политическую позицию, если она не видна из сообщений.

Источники:
[1] дата — короткая суть
[2] дата — короткая суть

Правила:
- Не сравнивай с другими каналами. Ты видишь только этот канал.
- Не обобщай слишком рано: дай факты.
- Не теряй важные одноразовые события.
- Если сообщений мало, честно отметь это.
""".strip()


MULTI_CHANNEL_FINAL_OUTPUT_HEADER_V7 = """
📅 Период: {period_label}
Каналов: {channels_count}
Сообщений собрано: {total_collected}
Сообщений после очистки: {total_clean}

Ниже — дайджесты по каждому каналу, затем общее резюме.
""".strip()


COMMON_MULTI_CHANNEL_SUMMARY_PROMPT_V7 = """
Сделай общее резюме по нескольким Telegram-каналам за период.

Период: {period_label}
Каналов: {channels_count}

Ниже даны уже готовые дайджесты по каждому каналу. Общее резюме нужно делать по ним, а не придумывать новые факты.

{channel_digests_block}

Требования к ответу:

🧩 Общее резюме

Главная картина:
3-6 предложений. Что в целом происходило за период, какие темы были главными, какой общий сюжет складывается.

Общие темы:
1. Тема — какие каналы её поднимали и что именно они писали.
2. ...

Различия между каналами:
- @{channel}: какой был акцент;
- @{channel}: какой был акцент;
- если каналы писали об одном событии по-разному, сравни это аккуратно.

Что важно не пропустить:
5-10 конкретных фактов, цифр, решений или последствий, которые важны по всем каналам вместе.

Повторы и пересечения:
Кратко покажи, какие события повторялись в нескольких каналах, а какие были уникальны для одного канала.

Итог:
1 короткий абзац: что пользователь должен понять после чтения всех дайджестов.

Правила:
- Не добавляй факты, которых нет в дайджестах каналов.
- Не превращай резюме в пересказ всех каналов подряд.
- Не пиши воду.
- Если каналов мало или данных мало, честно отметь ограничение.
""".strip()


                           
                
                           


def _safe_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback



def _clip_text(text: str, limit: int) -> str:
    text = _safe_text(text)
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(" ", 1)[0].strip()
    return clipped + "..."



def format_messages_block_v7(messages: Iterable[DigestMessage]) -> str:
    """
    Форматирует сообщения для промта.

    Важно: функция НЕ ранжирует и НЕ выбирает лучшие сообщения.
    Она сохраняет порядок входного списка. Лучше передавать сообщения уже отсортированными по дате ASC.
    """
    parts: list[str] = []
    total_chars = 0

    for index, message in enumerate(messages, start=1):
        text = _clip_text(message.text, MAX_MESSAGE_CHARS_FOR_DIGEST)
        if not text:
            continue

        header_bits = [f"[{index}]"]

        if message.date:
            header_bits.append(f"date={message.date}")
        if message.msg_id is not None:
            header_bits.append(f"id={message.msg_id}")
        if message.views is not None:
            header_bits.append(f"views={message.views}")
        if message.forwards is not None:
            header_bits.append(f"forwards={message.forwards}")
        if message.link:
            header_bits.append(f"link={message.link}")

        block = " ".join(header_bits) + "\n" + text

        if total_chars + len(block) > MAX_TOTAL_CHARS_DIRECT_DIGEST:
            parts.append(
                "\n[system]\n"
                "Сообщений оказалось слишком много для одного прямого промта. "
                "Нужно обработать канал чанками, а затем собрать финальный дайджест из чанковых выжимок."
            )
            break

        parts.append(block)
        total_chars += len(block)

    if not parts:
        return "Сообщений после очистки нет."

    return "\n\n".join(parts)



def build_single_channel_digest_prompt_v7(channel: ChannelDigestInput) -> str:
    """Промт для случая, когда пользователь выбрал ровно один канал."""
    title = _safe_text(channel.title, f"@{channel.username}")
    messages_block = format_messages_block_v7(channel.messages)

    return SINGLE_CHANNEL_DIGEST_PROMPT_V7.format(
        channel_title=title,
        channel_title_or_username=title,
        username=channel.username.lstrip("@"),
        period_label=channel.period_label,
        total_collected=channel.total_collected if channel.total_collected is not None else "не указано",
        total_clean=channel.total_clean if channel.total_clean is not None else len(channel.messages),
        messages_block=messages_block,
    )



def build_channel_digest_prompt_v7(channel: ChannelDigestInput) -> str:
    """Промт для отдельного дайджеста канала в режиме 2+ каналов."""
    title = _safe_text(channel.title, f"@{channel.username}")
    messages_block = format_messages_block_v7(channel.messages)

    return CHANNEL_DIGEST_PROMPT_V7.format(
        channel_title=title,
        channel_title_or_username=title,
        username=channel.username.lstrip("@"),
        period_label=channel.period_label,
        total_collected=channel.total_collected if channel.total_collected is not None else "не указано",
        total_clean=channel.total_clean if channel.total_clean is not None else len(channel.messages),
        messages_block=messages_block,
    )



def build_common_multi_channel_summary_prompt_v7(
    *,
    period_label: str,
    channel_digests: list[str],
) -> str:
    """Промт для общего резюме по уже готовым дайджестам каналов."""
    channel_digests_block = "\n\n━━━━━━━━━━━━━━\n\n".join(
        digest.strip() for digest in channel_digests if digest and digest.strip()
    )

    if not channel_digests_block:
        channel_digests_block = "Дайджестов каналов нет."

    return COMMON_MULTI_CHANNEL_SUMMARY_PROMPT_V7.format(
        period_label=period_label,
        channels_count=len(channel_digests),
        channel_digests_block=channel_digests_block,
    )



                           
                      
                           


def _sum_optional(values: Iterable[int | None]) -> int | str:
    clean_values = [value for value in values if isinstance(value, int)]
    if not clean_values:
        return "не указано"
    return sum(clean_values)


def build_multi_channel_final_output_v7(
    *,
    period_label: str,
    channel_digests: Sequence[GeneratedChannelDigest | str],
    common_summary: str,
    total_collected: int | None = None,
    total_clean: int | None = None,
) -> str:
    """
    Собирает финальный текст для пользователя в режиме 2+ каналов.

    Важно: порядок именно такой:
    1) служебная шапка;
    2) ВСЕ отдельные дайджесты каналов;
    3) общее резюме.

    Эта функция ничего не сокращает и не пересказывает. Она только склеивает уже готовые ответы ИИ.
    """
    normalized_digests: list[GeneratedChannelDigest] = []

    for index, item in enumerate(channel_digests, start=1):
        if isinstance(item, GeneratedChannelDigest):
            normalized_digests.append(item)
        else:
            normalized_digests.append(
                GeneratedChannelDigest(
                    username=f"channel_{index}",
                    title=None,
                    digest_text=str(item),
                )
            )

    if total_collected is None:
        total_collected_value = _sum_optional(d.total_collected for d in normalized_digests)
    else:
        total_collected_value = total_collected

    if total_clean is None:
        total_clean_value = _sum_optional(d.total_clean for d in normalized_digests)
    else:
        total_clean_value = total_clean

    header = MULTI_CHANNEL_FINAL_OUTPUT_HEADER_V7.format(
        period_label=period_label,
        channels_count=len(normalized_digests),
        total_collected=total_collected_value,
        total_clean=total_clean_value,
    )

    digest_blocks: list[str] = []
    for digest in normalized_digests:
        text = _safe_text(digest.digest_text)
        if not text:
            username = digest.username.lstrip("@")
            title = _safe_text(digest.title, f"@{username}")
            text = (
                f"📌 @{username} — {title}\n\n"
                "Недостаточно данных для дайджеста или ИИ вернул пустой ответ."
            )
        digest_blocks.append(text.strip())

    common_summary = _safe_text(
        common_summary,
        "🧩 Общее резюме\n\nОбщее резюме не сформировано.",
    )

    return (
        header
        + "\n\n━━━━━━━━━━━━━━\n\n"
        + "\n\n━━━━━━━━━━━━━━\n\n".join(digest_blocks)
        + "\n\n━━━━━━━━━━━━━━\n\n"
        + common_summary.strip()
    ).strip()


def build_final_digest_output_v7(
    *,
    channels_count: int,
    single_digest: str | None = None,
    period_label: str | None = None,
    channel_digests: Sequence[GeneratedChannelDigest | str] | None = None,
    common_summary: str | None = None,
) -> str:
    """
    Универсальная сборка финального ответа.

    - 1 канал: возвращает единственный дайджест как есть.
    - 2+ каналов: возвращает все дайджесты каналов + общее резюме в конце.
    """
    if channels_count == 1:
        return _safe_text(single_digest, "Дайджест не сформирован.")

    return build_multi_channel_final_output_v7(
        period_label=_safe_text(period_label, "период не указан"),
        channel_digests=channel_digests or [],
        common_summary=_safe_text(common_summary, "🧩 Общее резюме\n\nОбщее резюме не сформировано."),
    )


                           
                         
                           


def get_digest_prompts_v7(channels: list[ChannelDigestInput]) -> dict[str, Any]:
    """
    Возвращает набор промтов под выбранное количество каналов.

    Если канал один:
        result["mode"] == "single_channel"
        result["prompts"] содержит один user prompt.

    Если каналов несколько:
        result["mode"] == "multi_channel"
        result["channel_prompts"] содержит user prompts для каждого канала.
        После генерации отдельных дайджестов нужно:
        1) вызвать build_common_multi_channel_summary_prompt_v7(...);
        2) получить common_summary от ИИ;
        3) вызвать build_multi_channel_final_output_v7(...), чтобы вывести
           ВСЕ дайджесты каналов и только потом общее резюме.
    """
    if len(channels) == 1:
        return {
            "version": DIGEST_PROMPT_VERSION,
            "mode": "single_channel",
            "system_prompt": DIGEST_SYSTEM_PROMPT_V7,
            "prompts": [build_single_channel_digest_prompt_v7(channels[0])],
        }

    return {
        "version": DIGEST_PROMPT_VERSION,
        "mode": "multi_channel",
        "system_prompt": DIGEST_SYSTEM_PROMPT_V7,
        "channel_prompts": [build_channel_digest_prompt_v7(channel) for channel in channels],
        "common_summary_builder": build_common_multi_channel_summary_prompt_v7,
        "final_output_builder": build_multi_channel_final_output_v7,
    }



                           
               
                           

MULTI_CHANNEL_FLOW_EXAMPLE_V7 = """
# 1) Для каждого канала строим prompt и получаем channel_digest_text от ИИ.
channel_digest_texts = []

for channel in selected_channels:
    prompt = build_channel_digest_prompt_v7(channel)
    digest_text = await ask_ai(system=DIGEST_SYSTEM_PROMPT_V7, user=prompt)

    channel_digest_texts.append(
        GeneratedChannelDigest(
            username=channel.username,
            title=channel.title,
            digest_text=digest_text,
            total_collected=channel.total_collected,
            total_clean=channel.total_clean,
        )
    )

# 2) Строим prompt для общего резюме по готовым дайджестам.
common_prompt = build_common_multi_channel_summary_prompt_v7(
    period_label=selected_channels[0].period_label,
    channel_digests=[item.digest_text for item in channel_digest_texts],
)
common_summary = await ask_ai(system=DIGEST_SYSTEM_PROMPT_V7, user=common_prompt)

# 3) Финальный ответ пользователю: сначала ВСЕ дайджесты, потом общее резюме.
final_text = build_multi_channel_final_output_v7(
    period_label=selected_channels[0].period_label,
    channel_digests=channel_digest_texts,
    common_summary=common_summary,
)
await message.answer(final_text)
""".strip()
