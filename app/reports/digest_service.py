import asyncio
import json
from datetime import datetime, timezone

from aiogram.types import Message

from app.bot import runtime
from app.bot.utils import send_long_text
from app.ai.digest_generator import (
    create_channel_digest,
    create_final_digest_report,
    format_channel_digest_for_user,
    format_digest_report_for_user,
    get_digest_preset_title,
)
from app.db.database import (
    create_weekly_digest_job,
    finish_weekly_digest_job,
    save_weekly_channel_digest_part,
)
from app.messages.cleaning import format_filter_stats
from app.reports.service import format_collect_result_stats, format_channel_collect_debug
from config import DIGEST_MAX_MESSAGES_PER_CHANNEL, DIGEST_TOTAL_MAX_MESSAGES


def _username_key(username: str | None) -> str:
    return (username or "").strip().lstrip("@").lower()


def group_digest_messages_by_channel(
    selected_channels: list[dict],
    structured_messages: list[dict],
) -> dict[str, list[dict]]:
    messages_by_username: dict[str, list[dict]] = {}

    for item in structured_messages:
        key = _username_key(item.get("username"))
        if not key:
            continue
        messages_by_username.setdefault(key, []).append(item)

    result: dict[str, list[dict]] = {}
    total = 0

    for channel in selected_channels:
        key = _username_key(channel.get("username"))
        if not key:
            continue

        channel_messages = messages_by_username.get(key) or []
        limited = channel_messages[:DIGEST_MAX_MESSAGES_PER_CHANNEL]
        result[key] = limited
        total += len(limited)

        if total >= DIGEST_TOTAL_MAX_MESSAGES:
                                                                                                      
            break

                                                                                             
    if not any(result.values()) and structured_messages:
        for item in structured_messages[:DIGEST_TOTAL_MAX_MESSAGES]:
            key = _username_key(item.get("username"))
            if key:
                result.setdefault(key, []).append(item)

    return result


                                                                                 
                                                                     
def prepare_digest_messages(
    selected_channels: list[dict],
    structured_messages: list[dict],
) -> list[dict]:
    grouped = group_digest_messages_by_channel(selected_channels, structured_messages)
    result: list[dict] = []
    for channel in selected_channels:
        key = _username_key(channel.get("username"))
        result.extend(grouped.get(key) or [])
        if len(result) >= DIGEST_TOTAL_MAX_MESSAGES:
            return result[:DIGEST_TOTAL_MAX_MESSAGES]
    return result[:DIGEST_TOTAL_MAX_MESSAGES]


def format_no_digest_messages_explanation(result: dict, period_label: str) -> str:
    details = [
        "⚠️ Дайджест не создан",
        "",
        "За выбранный период бот не получил полезные текстовые сообщения из выбранных каналов.",
        "",
        f"Период: {period_label}",
        format_collect_result_stats(result),
    ]

    filter_text = format_filter_stats(result.get("filter_stats") or {})
    if filter_text:
        details.append(filter_text)

    debug_text = format_channel_collect_debug(result)
    if debug_text:
        details.append(debug_text)

    details.append("")
    details.append("Что попробовать: выбрать период «За неделю» или добавить более активные каналы.")

    return "\n".join(details)


def build_channel_lookup(selected_channels: list[dict]) -> dict[str, dict]:
    return {
        _username_key(channel.get("username")): channel
        for channel in selected_channels
        if _username_key(channel.get("username"))
    }


def build_user_channel_id_lookup(
    selected_channels: list[dict],
    user_channel_ids: list[int] | None,
) -> dict[str, int | None]:
    ids = list(user_channel_ids or [])
    result: dict[str, int | None] = {}

    for index, channel in enumerate(selected_channels):
        key = _username_key(channel.get("username"))
        if not key:
            continue
        result[key] = int(ids[index]) if index < len(ids) and ids[index] is not None else None

    return result


async def build_digest_report_output(
    selected_channels: list[dict],
    result: dict,
    period_label: str,
    db_user_id: int | None = None,
    digest_preset: str | None = "normal",
    digest_user_channel_ids: list[int] | None = None,
    period_from=None,
    period_to=None,
) -> str:
    structured_messages = result.get("structured_messages") or []

    if not structured_messages:
        return format_no_digest_messages_explanation(result, period_label)

    grouped_messages = group_digest_messages_by_channel(
        selected_channels=selected_channels,
        structured_messages=structured_messages,
    )

    digest_messages_count = sum(len(items) for items in grouped_messages.values())
    if digest_messages_count <= 0:
        return format_no_digest_messages_explanation(result, period_label)

    digest_job_id: int | None = None
    if db_user_id is not None:
        try:
            digest_job_id = await create_weekly_digest_job(
                user_id=db_user_id,
                period_from=period_from,
                period_to=period_to,
                selected_user_channel_ids=digest_user_channel_ids or [],
                channels_count=len(selected_channels),
                messages_count=len(structured_messages),
            )
        except Exception as error:
            print(f"Не удалось создать weekly_digest_job: {type(error).__name__}: {error}")
            digest_job_id = None

    channel_lookup = build_channel_lookup(selected_channels)
    user_channel_id_by_key = build_user_channel_id_lookup(selected_channels, digest_user_channel_ids)
    channel_digests: list[dict] = []
    errors: list[str] = []

    for channel in selected_channels:
        key = _username_key(channel.get("username"))
        if not key:
            continue

        channel_messages = grouped_messages.get(key) or []
        if not channel_messages:
            channel_digest = {
                "ok": False,
                "channel": channel.get("username"),
                "title": channel.get("title") or channel.get("username"),
                "error": "Нет полезных сообщений за период.",
                "importance": "low",
                "messages_count": 0,
                "model_used": None,
                "digest_preset": digest_preset,
            }
        else:
            try:
                channel_digest = await asyncio.wait_for(
                    create_channel_digest(
                        channel=channel,
                        channel_messages=channel_messages,
                        period_label=period_label,
                        digest_preset=digest_preset,
                    ),
                    timeout=runtime.AI_REPORT_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                channel_digest = {
                    "ok": False,
                    "channel": channel.get("username"),
                    "title": channel.get("title") or channel.get("username"),
                    "error": f"Сводка канала не успела подготовиться за {runtime.AI_REPORT_TIMEOUT_SECONDS} сек.",
                    "importance": "low",
                    "messages_count": len(channel_messages),
                    "model_used": None,
                    "digest_preset": digest_preset,
                }
            except Exception as error:
                channel_digest = {
                    "ok": False,
                    "channel": channel.get("username"),
                    "title": channel.get("title") or channel.get("username"),
                    "error": f"Ошибка сводки канала: {type(error).__name__}: {error}",
                    "importance": "low",
                    "messages_count": len(channel_messages),
                    "model_used": None,
                    "digest_preset": digest_preset,
                }

        channel_digests.append(channel_digest)

        if channel_digest.get("ok") is not True and channel_digest.get("error"):
            errors.append(f"{channel.get('username')}: {channel_digest.get('error')}")

        if digest_job_id is not None:
            try:
                await save_weekly_channel_digest_part(
                    job_id=digest_job_id,
                    user_channel_id=user_channel_id_by_key.get(key),
                    username=channel.get("username") or channel_digest.get("channel") or "",
                    title=channel.get("title") or channel_digest.get("title"),
                    messages_count=len(channel_messages),
                    summary_json=channel_digest,
                    model=channel_digest.get("model_used"),
                )
            except Exception as error:
                print(f"Не удалось сохранить weekly_channel_digest_part: {type(error).__name__}: {error}")

    useful_channel_digests = sum(1 for item in channel_digests if item.get("ok") is True)
    header = (
        f"Период: {period_label}\n"
        f"Формат: {get_digest_preset_title(digest_preset)}\n"
        f"Каналов: {len(selected_channels)}\n"
        f"Сообщений собрано: {len(structured_messages)}\n"
        f"В канальные сводки отправлено: {digest_messages_count}\n"
        f"Канальных сводок создано: {useful_channel_digests}/{len(selected_channels)}\n\n"
    )

                                                                                      
                                                                
    if len(selected_channels) == 1:
        single_report = channel_digests[0] if channel_digests else {
            "ok": False,
            "error": "Нет канальной сводки.",
            "model_used": None,
        }
        output_text = header + "🧾 Дайджест выбранного канала\n\n" + format_channel_digest_for_user(single_report)

        if errors:
            output_text += "\n\nОграничения по каналу:\n"
            for item in errors[:3]:
                output_text += f"• {item}\n"

        if digest_job_id is not None:
            try:
                await finish_weekly_digest_job(
                    job_id=digest_job_id,
                    status="finished" if single_report.get("ok") else "failed",
                    channel_digests_count=useful_channel_digests,
                    final_summary_json=single_report,
                    final_text=output_text,
                    model=single_report.get("model_used"),
                    error_text=None if single_report.get("ok") else single_report.get("error"),
                )
            except Exception as error:
                print(f"Не удалось завершить weekly_digest_job: {type(error).__name__}: {error}")

        return output_text

    channel_texts = []
    for digest in channel_digests:
        channel_texts.append(format_channel_digest_for_user(digest))

    try:
        final_report = await asyncio.wait_for(
            create_final_digest_report(
                selected_channels=selected_channels,
                channel_digests=channel_digests,
                period_label=period_label,
                digest_preset=digest_preset,
            ),
            timeout=runtime.AI_REPORT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        final_report = {
            "ok": False,
            "error": f"Общее резюме не успело подготовиться за {runtime.AI_REPORT_TIMEOUT_SECONDS} сек.",
            "model_used": None,
        }
    except Exception as error:
        final_report = {
            "ok": False,
            "error": f"Ошибка общего резюме: {type(error).__name__}: {error}",
            "model_used": None,
        }

    final_text = format_digest_report_for_user(final_report)

    output_text = header
    output_text += "🧾 Дайджесты по каналам\n\n"
    output_text += "\n\n".join(channel_texts)
    output_text += "\n\n🌐 Общее резюме по выбранным каналам\n\n"
    output_text += final_text

    if errors:
        output_text += "\n\nОграничения по каналам:\n"
        for item in errors[:8]:
            output_text += f"• {item}\n"

    if digest_job_id is not None:
        try:
            await finish_weekly_digest_job(
                job_id=digest_job_id,
                status="finished" if final_report.get("ok") else "failed",
                channel_digests_count=useful_channel_digests,
                final_summary_json=final_report,
                final_text=output_text,
                model=final_report.get("model_used"),
                error_text=None if final_report.get("ok") else final_report.get("error"),
            )
        except Exception as error:
            print(f"Не удалось завершить weekly_digest_job: {type(error).__name__}: {error}")

    return output_text


async def send_digest_report_or_explanation(
    message: Message,
    selected_channels: list[dict],
    result: dict,
    period_label: str,
    db_user_id: int | None = None,
    digest_preset: str | None = "normal",
    digest_user_channel_ids: list[int] | None = None,
    period_from=None,
    period_to=None,
) -> None:
    output_text = await build_digest_report_output(
        selected_channels=selected_channels,
        result=result,
        period_label=period_label,
        db_user_id=db_user_id,
        digest_preset=digest_preset,
        digest_user_channel_ids=digest_user_channel_ids,
        period_from=period_from,
        period_to=period_to,
    )
    await send_long_text(message, output_text)


def get_last_message_date_from_result(result: dict):
                                                                                                 
                                                           
    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
