from __future__ import annotations

import asyncio
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from aiogram import Bot

from config import (
    DIGEST_AI_PREVIEW_MESSAGE_CHARS,
    DIGEST_AI_PREVIEW_MESSAGES_PER_CHANNEL,
    DIGEST_AI_PREVIEW_TOTAL_MESSAGES,
    DIGEST_COLLECT_MESSAGES_PER_CHANNEL,
    DIGEST_SUBSCRIPTION_FAILED_BACKOFF_MINUTES,
    DIGEST_SUBSCRIPTION_LOCK_HEARTBEAT_SECONDS,
)
from app.utils.timezones import DEFAULT_TIMEZONE, format_send_time
from app.db.database import (
    create_digest_subscription_run,
    finish_digest_subscription_run,
    get_digest_subscription_for_run,
    mark_digest_subscription_failed,
    mark_digest_subscription_success,
    refresh_digest_subscription_lock,
    release_digest_subscription_lock,
    save_user_channel_digest_state,
)
from app.reports.digest_service import build_digest_report_output, group_digest_messages_by_channel
from app.telegram.collector import collect_messages_from_channels
from app.utils.error_logging import log_exception
from app.utils.subscription_channels import (
    get_user_channel_ids_from_channels,
    normalize_channel_for_collect,
    normalize_subscription_channels,
)


def ensure_aware_utc(value):
    if value is None:
        return None
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_subscription_period(subscription: dict[str, Any], force_period_days: int | None = None) -> tuple[datetime, datetime, str]:
    """
    Для автосводки период всегда считается от прошлого успешного period_to.
    Если успешного запуска ещё не было — берём последние period_days.
    """
    date_to = datetime.now(timezone.utc)
    last_success_to = ensure_aware_utc(subscription.get("last_success_to"))
    period_days = int(force_period_days or subscription.get("period_days") or 7)

    if last_success_to:
        date_from = last_success_to
        label = "с прошлого автодайджеста"
    else:
        date_from = date_to - timedelta(days=max(1, period_days))
        label = f"первые {period_days} дн. автосводки"

    return date_from, date_to, label


async def send_long_text_to_chat(bot: Bot, chat_id: int, text: str) -> None:
    limit = 3500
    chunks: list[str] = []
    current = ""

    for block in (text or "").split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate

    if current:
        chunks.append(current)

    if not chunks:
        chunks = ["Автосводка пуста."]

    for chunk in chunks:
        await bot.send_message(chat_id, chunk)


async def _safe_cleanup(label: str, action: Callable[..., Awaitable[Any]], *args, **kwargs) -> Any | None:
    """Cleanup не должен добивать основной exception и оставлять scheduler без ответа."""
    try:
        return await action(*args, **kwargs)
    except Exception as error:
        print(f"[digest_subscription] cleanup failed at {label}: {type(error).__name__}: {error}")
        traceback.print_exc()
        return None


async def _subscription_lock_heartbeat(subscription_id: int, locked_by: str, interval_seconds: int) -> None:
    """Пока автосводка долго выполняется, обновляем locked_at."""
    interval = max(30, int(interval_seconds or 60))
    try:
        while True:
            await asyncio.sleep(interval)
            ok = await refresh_digest_subscription_lock(subscription_id, locked_by)
            if not ok:
                print(
                    f"[digest_subscription] lock heartbeat not refreshed: "
                    f"subscription_id={subscription_id}, locked_by={locked_by}"
                )
                return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        print(f"[digest_subscription] lock heartbeat error: {type(error).__name__}: {error}")
        traceback.print_exc()



def _truncate_preview_text(text: str | None, limit: int | None = None) -> str:
    limit = max(80, int(limit or DIGEST_AI_PREVIEW_MESSAGE_CHARS or 500))
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _format_preview_period(date_from: datetime, date_to: datetime) -> str:
    return f"{date_from.strftime('%d.%m.%Y %H:%M')} → {date_to.strftime('%d.%m.%Y %H:%M')} UTC"


def format_digest_ai_preview(
    subscription: dict[str, Any],
    selected_channels: list[dict[str, Any]],
    result: dict[str, Any],
    period_label: str,
    date_from: datetime,
    date_to: datetime,
    digest_preset: str | None = "normal",
) -> str:
    """Формирует debug-превью сообщений, которые попадут в ИИ-сводки.

    Важно: это не вызывает ИИ и не обновляет last_success_to/next_run_at.
    Показываем ровно тот слой, который будет подан в create_channel_digest():
    сообщения уже собраны, очищены и сгруппированы по каналам.
    """
    structured_messages = result.get("structured_messages") or []
    grouped = group_digest_messages_by_channel(selected_channels, structured_messages)

    total_to_ai = sum(len(items) for items in grouped.values())
    title = subscription.get("title") or f"Автосводка #{subscription.get('id')}"
    timezone_name = subscription.get("timezone") or DEFAULT_TIMEZONE

    lines: list[str] = [
        "🧪 Что уйдёт в ИИ",
        "",
        f"Подписка: #{subscription.get('id')} — {title}",
        f"Период: {period_label}",
        f"Границы: {_format_preview_period(date_from, date_to)}",
        f"Формат: {digest_preset or 'normal'}",
        f"Расписание: {format_send_time(subscription.get('send_time'))} ({timezone_name})",
        "",
        f"Каналов: {len(selected_channels)}",
        f"Сообщений собрано: {int(result.get('messages_for_user') or len(structured_messages) or 0)}",
        f"В ИИ для канальных сводок пойдёт: {total_to_ai}",
        f"Показано в предпросмотре: до {DIGEST_AI_PREVIEW_MESSAGES_PER_CHANNEL} сообщений на канал, максимум {DIGEST_AI_PREVIEW_TOTAL_MESSAGES} всего.",
        "",
    ]

    if not structured_messages:
        error_text = result.get("error")
        lines.append("Полезных сообщений за этот период нет.")
        if error_text:
            lines.append(f"Ошибка/причина: {error_text}")
        return "\n".join(lines).strip()

    channel_stats_by_username = {}
    for item in result.get("channel_stats") or []:
        username = str(item.get("username") or "").strip().lstrip("@").lower()
        if username:
            channel_stats_by_username[username] = item

    lines.append("Статистика по каналам:")
    for channel in selected_channels:
        username = str(channel.get("username") or "").strip()
        key = username.lstrip("@").lower()
        items = grouped.get(key) or []
        stats = channel_stats_by_username.get(key) or {}
        lines.append(
            f"• {username}: в ИИ {len(items)}; "
            f"из кэша {int(stats.get('from_cache') or 0)}; "
            f"в периоде Telegram {int(stats.get('telegram_in_period') or 0)}; "
            f"отфильтровано {int(stats.get('filtered') or 0)}"
        )
        if stats.get("error"):
            lines.append(f"  Ошибка канала: {stats.get('error')}")

    lines.append("")
    lines.append("Сообщения для ИИ:")

    shown_total = 0
    for channel in selected_channels:
        username = str(channel.get("username") or "").strip()
        title_value = channel.get("title") or username
        key = username.lstrip("@").lower()
        items = grouped.get(key) or []

        if shown_total >= DIGEST_AI_PREVIEW_TOTAL_MESSAGES:
            break

        lines.append("")
        lines.append(f"📌 {username} — {title_value}")

        if not items:
            lines.append("Сообщений для ИИ по этому каналу нет.")
            continue

        per_channel_shown = 0
        for item in items:
            if shown_total >= DIGEST_AI_PREVIEW_TOTAL_MESSAGES:
                break
            if per_channel_shown >= DIGEST_AI_PREVIEW_MESSAGES_PER_CHANNEL:
                break

            shown_total += 1
            per_channel_shown += 1
            cache_label = "кэш" if item.get("from_cache") else "Telegram"
            date_text = item.get("date_text") or "без даты"
            text = _truncate_preview_text(item.get("cleaned_text"))
            lines.append(f"[{shown_total}] {date_text} | {cache_label}\n{text}")

        hidden = len(items) - per_channel_shown
        if hidden > 0:
            lines.append(f"… ещё {hidden} сообщений этого канала тоже уйдут в ИИ.")

    hidden_total = max(0, total_to_ai - shown_total)
    if hidden_total > 0:
        lines.append("")
        lines.append(f"Не показано в предпросмотре из-за лимита: {hidden_total} сообщений.")

    lines.append("")
    lines.append("Расписание и точка прошлого парса не изменены. ИИ не вызывался.")
    return "\n".join(lines).strip()


async def preview_digest_subscription_messages(
    subscription_id: int,
    locked_by: str = "ai-preview",
) -> dict[str, Any]:
    """Собирает сообщения подписки и возвращает текст предпросмотра без вызова ИИ.

    Ожидается, что caller уже поставил lock через lock_digest_subscription_now().
    После предпросмотра lock снимается независимо от результата.
    """
    subscription = await get_digest_subscription_for_run(subscription_id, locked_by=locked_by)
    if not subscription:
        return {
            "ok": False,
            "status": "not_found",
            "error": "Подписка не найдена, отключена или занята другим запуском.",
        }

    channels_raw = normalize_subscription_channels(subscription.get("channels"))
    selected_channels = [normalize_channel_for_collect(channel) for channel in channels_raw]
    selected_channels = [channel for channel in selected_channels if channel.get("username")]
    user_id = int(subscription["user_id"])
    date_from, date_to, period_label = resolve_subscription_period(subscription)
    digest_preset = subscription.get("digest_preset") or "normal"

    try:
        if not selected_channels:
            return {
                "ok": False,
                "status": "no_channels",
                "error": "В подписке нет активных личных каналов.",
            }

        result = await collect_messages_from_channels(
            db_user_id=user_id,
            selected_channels=selected_channels,
            date_from=date_from,
            date_to=date_to,
            max_messages_per_channel=DIGEST_COLLECT_MESSAGES_PER_CHANNEL,
        )

        if not result.get("ok"):
            return {
                "ok": False,
                "status": "collect_failed",
                "error": result.get("error") or "Ошибка сбора сообщений для предпросмотра.",
                "messages_count": int(result.get("messages_for_user") or 0),
                "preview_text": format_digest_ai_preview(
                    subscription=subscription,
                    selected_channels=selected_channels,
                    result=result,
                    period_label=period_label,
                    date_from=date_from,
                    date_to=date_to,
                    digest_preset=digest_preset,
                ),
            }

        preview_text = format_digest_ai_preview(
            subscription=subscription,
            selected_channels=selected_channels,
            result=result,
            period_label=period_label,
            date_from=date_from,
            date_to=date_to,
            digest_preset=digest_preset,
        )

        return {
            "ok": True,
            "status": "preview_ready",
            "subscription_id": int(subscription_id),
            "channels_count": len(selected_channels),
            "messages_count": int(result.get("messages_for_user") or 0),
            "period_from": date_from,
            "period_to": date_to,
            "preview_text": preview_text,
        }
    finally:
        await _safe_cleanup(
            "release_ai_preview_lock",
            release_digest_subscription_lock,
            int(subscription_id),
            locked_by,
        )


async def run_digest_subscription(
    subscription_id: int,
    bot: Bot,
    debug: bool = False,
    locked_by: str = "scheduler",
    update_progress: bool = True,
) -> dict[str, Any]:
    """
    Запускает одну подписку: парсинг -> иерархический дайджест -> отправка пользователю.

    Устойчивость:
    - lock продлевается heartbeat-ом;
    - при ошибке next_run_at переносится вперёд, чтобы не было бесконечного цикла;
    - last_success_to двигается только после успешной отправки или no_messages, если update_progress=True;
    - update_progress=False делает preview/debug без сдвига расписания и точки прошлого парса;
    - cleanup-ошибки не ломают весь scheduler-loop.
    """
    run_id: int | None = None
    heartbeat_task: asyncio.Task | None = None
    telegram_id: int | None = None
    user_id: int | None = None

    try:
        subscription = await get_digest_subscription_for_run(subscription_id, locked_by=locked_by)
        if not subscription:
            return {
                "ok": False,
                "status": "not_found",
                "error": "Подписка не найдена, отключена или занята другим запуском.",
            }

        channels_raw = normalize_subscription_channels(subscription.get("channels"))
        selected_channels = [normalize_channel_for_collect(channel) for channel in channels_raw]
        selected_channels = [channel for channel in selected_channels if channel.get("username")]
        digest_user_channel_ids = get_user_channel_ids_from_channels(channels_raw)
        telegram_id = int(subscription["telegram_id"])
        user_id = int(subscription["user_id"])
        date_from, date_to, period_label = resolve_subscription_period(subscription)
        digest_preset = (subscription.get("digest_preset") or "normal")

        if debug:
            prefix = "🧪 Debug-проверка автосводки"
        elif str(locked_by or "").startswith("digest-worker:"):
            prefix = "🔔 Автосводка по расписанию"
        else:
            prefix = "▶️ Ручной запуск автосводки"
        title = subscription.get("title") or f"Автосводка #{subscription_id}"

        heartbeat_task = asyncio.create_task(
            _subscription_lock_heartbeat(
                subscription_id=subscription_id,
                locked_by=locked_by,
                interval_seconds=DIGEST_SUBSCRIPTION_LOCK_HEARTBEAT_SECONDS,
            )
        )

        run_id = await create_digest_subscription_run(
            subscription_id=subscription_id,
            user_id=user_id,
            period_from=date_from,
            period_to=date_to,
            channels_count=len(selected_channels),
        )

        if not selected_channels:
            error_text = "В подписке нет активных личных каналов."
            await _safe_cleanup(
                "finish_no_channels_run",
                finish_digest_subscription_run,
                run_id=run_id,
                status="no_channels",
                messages_count=0,
                error_text=error_text,
                sent=False,
            )
            if update_progress:
                await _safe_cleanup(
                    "mark_no_channels_failed",
                    mark_digest_subscription_failed,
                    subscription_id,
                    error_text,
                    60,
                )
            else:
                await _safe_cleanup(
                    "release_no_channels_preview_lock",
                    release_digest_subscription_lock,
                    subscription_id,
                    locked_by,
                )
            try:
                await bot.send_message(telegram_id, f"{prefix} не выполнена.\n\nПричина: {error_text}")
            except Exception:
                traceback.print_exc()
            return {"ok": False, "status": "no_channels", "error": error_text}

        if debug:
            timezone_name = subscription.get("timezone") or DEFAULT_TIMEZONE
            await bot.send_message(
                telegram_id,
                f"{prefix}: запускаю подписку #{subscription_id}\n"
                f"Название: {title}\n"
                f"Каналов: {len(selected_channels)}\n"
                f"Период: {period_label}\n"
                f"Формат: {digest_preset}\n"
                f"Обновление точки парса: {'да' if update_progress else 'нет'}\n"
                f"Время по расписанию: {format_send_time(subscription.get('send_time'))} ({timezone_name})",
            )

        result = await collect_messages_from_channels(
            db_user_id=user_id,
            selected_channels=selected_channels,
            date_from=date_from,
            date_to=date_to,
            max_messages_per_channel=DIGEST_COLLECT_MESSAGES_PER_CHANNEL,
        )

        if not result.get("ok"):
            error_text = result.get("error") or "Ошибка сбора сообщений для автосводки."
            messages_count = int(result.get("messages_for_user") or 0)
            await _safe_cleanup(
                "finish_collect_failed_run",
                finish_digest_subscription_run,
                run_id=run_id,
                status="failed",
                messages_count=messages_count,
                error_text=error_text,
                sent=False,
            )
            if update_progress:
                await _safe_cleanup(
                    "mark_collect_failed",
                    mark_digest_subscription_failed,
                    subscription_id,
                    error_text,
                    DIGEST_SUBSCRIPTION_FAILED_BACKOFF_MINUTES,
                )
            else:
                await _safe_cleanup(
                    "release_collect_failed_preview_lock",
                    release_digest_subscription_lock,
                    subscription_id,
                    locked_by,
                )
            try:
                await bot.send_message(telegram_id, f"{prefix} не выполнена.\n\nПричина: {error_text}")
            except Exception:
                traceback.print_exc()
            return {
                "ok": False,
                "status": "collect_failed",
                "error": error_text,
                "messages_count": messages_count,
            }

        output_text = await build_digest_report_output(
            selected_channels=selected_channels,
            result=result,
            period_label=period_label,
            db_user_id=user_id,
            digest_preset=digest_preset,
            digest_user_channel_ids=digest_user_channel_ids,
            period_from=date_from,
            period_to=date_to,
        )

        timezone_name = subscription.get("timezone") or DEFAULT_TIMEZONE
        progress_note = "" if update_progress else "Режим: просмотр без обновления точки прошлого парса\n"
        header = (
            f"{prefix}: {title}\n"
            f"Период: {period_label}\n"
            f"Каналов: {len(selected_channels)}\n"
            f"Формат: {digest_preset}\n"
            f"{progress_note}"
            f"Расписание: {format_send_time(subscription.get('send_time'))} ({timezone_name})\n\n"
        )

                                                                                       
        await send_long_text_to_chat(bot, telegram_id, header + output_text)

        messages_count = int(result.get("messages_for_user") or 0)
        status = "success" if messages_count > 0 else "no_messages"

        await _safe_cleanup(
            "finish_success_run",
            finish_digest_subscription_run,
            run_id=run_id,
            status=status,
            messages_count=messages_count,
            error_text=None,
            sent=True,
        )
        if update_progress:
            await mark_digest_subscription_success(
                subscription_id=subscription_id,
                period_from=date_from,
                period_to=date_to,
            )
            await _safe_cleanup(
                "save_user_channel_digest_state",
                save_user_channel_digest_state,
                user_id=user_id,
                user_channel_ids=digest_user_channel_ids,
                last_digest_at=date_to,
                last_message_date=None,
            )
        else:
            await _safe_cleanup(
                "release_preview_lock",
                release_digest_subscription_lock,
                subscription_id,
                locked_by,
            )

        return {
            "ok": True,
            "status": status,
            "subscription_id": subscription_id,
            "channels_count": len(selected_channels),
            "messages_count": messages_count,
            "period_from": date_from,
            "period_to": date_to,
            "update_progress": update_progress,
        }

    except asyncio.CancelledError:
                                                                     
        error_text = "Autodigest task cancelled: бот остановлен во время выполнения автосводки."
        if run_id is not None:
            await _safe_cleanup(
                "finish_cancelled_run",
                finish_digest_subscription_run,
                run_id=run_id,
                status="interrupted",
                messages_count=0,
                error_text=error_text,
                sent=False,
            )
        if update_progress:
            await _safe_cleanup(
                "mark_cancelled_failed",
                mark_digest_subscription_failed,
                subscription_id,
                error_text,
                DIGEST_SUBSCRIPTION_FAILED_BACKOFF_MINUTES,
            )
        else:
            await _safe_cleanup(
                "release_cancelled_preview_lock",
                release_digest_subscription_lock,
                subscription_id,
                locked_by,
            )
        raise

    except Exception as error:
        error_text = f"{type(error).__name__}: {error}"
        traceback.print_exc()
        await log_exception(
            "digest_subscription.run",
            error,
            user_id=user_id,
            telegram_id=telegram_id,
            context={
                "subscription_id": subscription_id,
                "run_id": run_id,
                "debug": debug,
                "locked_by": locked_by,
                "update_progress": update_progress,
            },
        )
        if run_id is not None:
            await _safe_cleanup(
                "finish_exception_run",
                finish_digest_subscription_run,
                run_id=run_id,
                status="failed",
                messages_count=0,
                error_text=error_text,
                sent=False,
            )
        if update_progress:
            await _safe_cleanup(
                "mark_exception_failed",
                mark_digest_subscription_failed,
                subscription_id,
                error_text,
                DIGEST_SUBSCRIPTION_FAILED_BACKOFF_MINUTES,
            )
        else:
            await _safe_cleanup(
                "release_exception_preview_lock",
                release_digest_subscription_lock,
                subscription_id,
                locked_by,
            )
        if telegram_id is not None:
            try:
                if debug:
                    prefix = "🧪 Debug-проверка автосводки"
                elif str(locked_by or "").startswith("digest-worker:"):
                    prefix = "🔔 Автосводка по расписанию"
                else:
                    prefix = "▶️ Ручной запуск автосводки"
                await bot.send_message(telegram_id, f"{prefix} не выполнена из-за ошибки.\n\n{error_text}")
            except Exception:
                traceback.print_exc()
        return {"ok": False, "status": "failed", "error": error_text}

    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                traceback.print_exc()
