from datetime import datetime, timezone

from telethon.errors import FloodWaitError

from config import MAX_MESSAGES_PER_CHANNEL, MAX_TELEGRAM_CHECK_LIMIT_PER_CHANNEL
from app.bot import runtime
from app.db.database import (
    create_parse_job,
    finish_parse_job,
    get_or_create_telegram_channel,
    get_cached_useful_messages,
    link_message_to_user_result,
    save_message,
    save_rejected_message,
    mark_channel_as_ad_heavy,
)
from app.messages.cleaning import clean_message_text, get_filter_reason
from app.telegram.access import get_telegram_global_block_message, telegram_request_pause, handle_telegram_flood_wait
from app.utils.dates import ensure_aware_utc
from app.utils.env import get_env_bool, get_env_float, get_env_int
from app.utils.debug import debug_print_block

AD_HEAVY_MIN_AD_MESSAGES = get_env_int("AD_HEAVY_MIN_AD_MESSAGES", 3)
AD_HEAVY_RATIO = get_env_float("AD_HEAVY_RATIO", 0.35)
AUTO_DISABLE_AD_HEAVY_CHANNELS = get_env_bool("AUTO_DISABLE_AD_HEAVY_CHANNELS", False)

                                                                   
telegram_api_lock = runtime.telegram_api_lock
telegram_client = runtime.telegram_client
TELEGRAM_HISTORY_WAIT_SECONDS = runtime.TELEGRAM_HISTORY_WAIT_SECONDS
TELEGRAM_PROGRESS_PAUSE_EVERY = runtime.TELEGRAM_PROGRESS_PAUSE_EVERY

def is_ad_filter_reason(reason: str | None) -> bool:
    return reason in {"ad_marker", "cross_promo"}


def should_mark_channel_ad_heavy(channel_stats: dict) -> tuple[bool, float]:
    checked_count = int(channel_stats.get("telegram_in_period") or 0)
    ad_count = int(channel_stats.get("ads_rejected") or 0)

    if checked_count <= 0:
        return False, 0.0

    ratio = ad_count / checked_count

    if ad_count >= AD_HEAVY_MIN_AD_MESSAGES and ratio >= AD_HEAVY_RATIO:
        return True, ratio

    return False, ratio


async def maybe_mark_ad_heavy_channel(channel: dict, channel_stats: dict) -> None:
    should_mark, ratio = should_mark_channel_ad_heavy(channel_stats)

    channel_stats["ad_ratio"] = round(ratio, 4)
    channel_stats["ad_heavy"] = bool(should_mark)

    if not should_mark:
        return

    username = channel.get("username") or ""
    ad_count = int(channel_stats.get("ads_rejected") or 0)
    checked_count = int(channel_stats.get("telegram_in_period") or 0)

    message = (
        f"Канал помечен как рекламный/замусоренный: {username}. "
        f"Рекламы: {ad_count}/{checked_count} ({ratio:.1%}). "
        f"auto_disable={AUTO_DISABLE_AD_HEAVY_CHANNELS}"
    )

    print(message)
    debug_print_block(
        "AD HEAVY CHANNEL",
        {
            "username": username,
            "ad_count": ad_count,
            "checked_count": checked_count,
            "ad_ratio": ratio,
            "auto_disable": AUTO_DISABLE_AD_HEAVY_CHANNELS,
        },
    )

    await mark_channel_as_ad_heavy(
        username=username,
        ad_count=ad_count,
        checked_count=checked_count,
        ad_ratio=ratio,
        auto_disable=AUTO_DISABLE_AD_HEAVY_CHANNELS,
    )


async def collect_messages_from_channels(
    db_user_id: int,
    selected_channels: list[dict],
    date_from: datetime,
    date_to: datetime | None = None,
    max_messages_per_channel: int | None = None,
) -> dict:
    """
    Собирает сообщения с учётом общего кэша.

    Исправленная логика кэша:
    1. Кэш берётся свежими сообщениями вперёд, а не старыми.
    2. Даже если кэш уже заполнил лимит, бот всё равно проверяет Telegram на новые посты.
    3. После чтения Telegram кэш и новые сообщения объединяются, сортируются по свежести,
       и только потом выбирается финальный MAX_MESSAGES_PER_CHANNEL для пользователя.
    """
    if date_to is None:
        date_to = datetime.now(timezone.utc)

    per_channel_limit = max(1, int(max_messages_per_channel or MAX_MESSAGES_PER_CHANNEL))

    selected_channels_text = ", ".join(channel["username"] for channel in selected_channels)
    job_id = await create_parse_job(
        user_id=db_user_id,
        source_type="user",
        selected_channels=selected_channels_text,
    )

    messages_found = 0
    messages_saved = 0
    messages_from_cache = 0
    messages_for_user = 0
    useful_messages = []
    structured_messages = []
    filter_stats = {}
    channel_stats = []

    def make_message_item(
        *,
        username: str,
        title: str,
        telegram_message_id: int,
        cleaned_text: str,
        msg_date: datetime | None,
        from_cache: bool,
        db_message_id: int | None = None,
    ) -> dict:
        msg_date = ensure_aware_utc(msg_date)
        date_text = msg_date.strftime("%d.%m.%Y %H:%M") if msg_date else "без даты"
        return {
            "db_message_id": db_message_id,
            "telegram_message_id": telegram_message_id,
            "username": username,
            "title": title,
            "date_obj": msg_date,
            "date_text": date_text,
            "cleaned_text": cleaned_text,
            "from_cache": from_cache,
        }

    def sort_message_item(item: dict) -> tuple:
        date_obj = item.get("date_obj")
        if date_obj is None:
            date_obj = datetime.min.replace(tzinfo=timezone.utc)
        return (date_obj, int(item.get("telegram_message_id") or 0))

    try:
        block_message = get_telegram_global_block_message()
        if block_message:
            raise RuntimeError(block_message)

        for channel in selected_channels:
            title = channel.get("title") or channel["username"]
            username = channel["username"]
            current_channel_stats = {
                "username": username,
                "from_cache": 0,
                "telegram_seen": 0,
                "telegram_in_period": 0,
                "useful_saved": 0,
                "filtered": 0,
                "ads_rejected": 0,
                "ad_ratio": 0.0,
                "ad_heavy": False,
                "error": None,
            }
            channel_stats.append(current_channel_stats)

            print(f"Сбор: канал {username}, период {date_from} — {date_to}")

            canonical_channel = await get_or_create_telegram_channel(
                username=username,
                title=title,
            )
            telegram_channel_id = int(canonical_channel["id"])

            per_channel_messages: dict[int, dict] = {}
            cached_message_ids: set[int] = set()

            cached_messages = await get_cached_useful_messages(
                source_type="user",
                username=username,
                date_from=date_from,
                date_to=date_to,
                limit=per_channel_limit,
            )

            for cached in cached_messages:
                telegram_message_id = int(cached["telegram_message_id"])
                cached_message_ids.add(telegram_message_id)
                per_channel_messages[telegram_message_id] = make_message_item(
                    username=username,
                    title=title,
                    telegram_message_id=telegram_message_id,
                    cleaned_text=cached["cleaned_text"] or "",
                    msg_date=cached.get("message_date"),
                    from_cache=True,
                    db_message_id=int(cached["id"]),
                )

            block_message = get_telegram_global_block_message()
            if block_message:
                raise RuntimeError(block_message)

            async with telegram_api_lock:
                block_message = get_telegram_global_block_message()
                if block_message:
                    raise RuntimeError(block_message)

                await telegram_request_pause(f"перед чтением {username}")

                async for msg in telegram_client.iter_messages(
                    username,
                    offset_date=date_to,
                    limit=MAX_TELEGRAM_CHECK_LIMIT_PER_CHANNEL,
                    wait_time=TELEGRAM_HISTORY_WAIT_SECONDS,
                ):
                    current_channel_stats["telegram_seen"] += 1
                    msg_date = ensure_aware_utc(msg.date)

                    if msg_date and msg_date < date_from:
                        break

                    if msg_date and msg_date >= date_to:
                        continue

                    current_channel_stats["telegram_in_period"] += 1
                    messages_found += 1

                    if (
                        TELEGRAM_PROGRESS_PAUSE_EVERY > 0
                        and messages_found % TELEGRAM_PROGRESS_PAUSE_EVERY == 0
                    ):
                        await telegram_request_pause(f"после проверки {messages_found} сообщений")

                    if msg.id in cached_message_ids:
                                                                                                   
                                                                                                 
                        continue

                    original_text = msg.text or ""
                    cleaned_text = clean_message_text(original_text)
                    filter_reason = get_filter_reason(cleaned_text, original_text)
                    is_useful = filter_reason is None

                    if not is_useful:
                        await save_rejected_message(
                            source_type="user",
                            source_channel_id=channel["id"],
                            username=username,
                            title=channel.get("title"),
                            telegram_message_id=msg.id,
                            original_text=original_text,
                            cleaned_text=cleaned_text,
                            reject_reason=filter_reason,
                            message_date=msg_date,
                            has_text=bool(original_text.strip()),
                            has_media=bool(msg.media),
                            views_count=getattr(msg, "views", None),
                            forwards_count=getattr(msg, "forwards", None),
                            replies_count=getattr(msg.replies, "replies", None) if msg.replies else None,
                        )

                        filter_stats[filter_reason] = filter_stats.get(filter_reason, 0) + 1
                        current_channel_stats["filtered"] += 1

                        if is_ad_filter_reason(filter_reason):
                            current_channel_stats["ads_rejected"] += 1

                        continue

                    save_result = await save_message(
                        source_type="user",
                        source_channel_id=channel["id"],                                                                    
                        telegram_channel_id=telegram_channel_id,
                        username=username,
                        title=channel.get("title"),
                        telegram_message_id=msg.id,
                        message_text=cleaned_text,
                        cleaned_text=cleaned_text,
                        message_date=msg_date,
                        has_text=bool(cleaned_text.strip()),
                        has_media=bool(msg.media),
                        is_useful=True,
                        filter_reason=None,
                        views_count=getattr(msg, "views", None),
                        forwards_count=getattr(msg, "forwards", None),
                        replies_count=getattr(msg.replies, "replies", None) if msg.replies else None,
                    )

                    if save_result["inserted"]:
                        messages_saved += 1

                    per_channel_messages[msg.id] = make_message_item(
                        username=username,
                        title=title,
                        telegram_message_id=msg.id,
                        cleaned_text=cleaned_text,
                        msg_date=msg_date,
                        from_cache=False,
                        db_message_id=save_result["id"],
                    )

            await maybe_mark_ad_heavy_channel(channel, current_channel_stats)

            final_channel_messages = sorted(
                per_channel_messages.values(),
                key=sort_message_item,
                reverse=True,
            )[:per_channel_limit]

            if final_channel_messages:
                useful_messages.append(f"📌 {title} ({username})")

            for item in final_channel_messages:
                if item.get("db_message_id"):
                    await link_message_to_user_result(
                        user_id=db_user_id,
                        parse_job_id=job_id,
                        message_id=item["db_message_id"],
                    )

                if item.get("from_cache"):
                    messages_from_cache += 1
                    current_channel_stats["from_cache"] += 1

                messages_for_user += 1
                current_channel_stats["useful_saved"] += 1

                useful_messages.append(f"🕒 {item['date_text']}\n{item['cleaned_text']}")
                structured_messages.append({
                    "username": item["username"],
                    "title": item["title"],
                    "date_text": item["date_text"],
                    "cleaned_text": item["cleaned_text"],
                    "from_cache": item["from_cache"],
                })

        await finish_parse_job(
            job_id=job_id,
            status="finished",
            messages_found=messages_found,
            messages_saved=messages_saved,
        )

        return {
            "ok": True,
            "messages_found": messages_found,
            "messages_saved": messages_saved,
            "messages_from_cache": messages_from_cache,
            "messages_for_user": messages_for_user,
            "filter_stats": filter_stats,
            "channel_stats": channel_stats,
            "useful_messages": useful_messages,
            "structured_messages": structured_messages,
            "error": None,
        }

    except FloodWaitError as error:
        error_text = await handle_telegram_flood_wait(error, "сбор сообщений")

        await finish_parse_job(
            job_id=job_id,
            status="failed",
            messages_found=messages_found,
            messages_saved=messages_saved,
            error_text=error_text,
        )

        return {
            "ok": False,
            "messages_found": messages_found,
            "messages_saved": messages_saved,
            "messages_from_cache": messages_from_cache,
            "messages_for_user": messages_for_user,
            "filter_stats": filter_stats,
            "channel_stats": channel_stats,
            "useful_messages": useful_messages,
            "structured_messages": structured_messages,
            "error": error_text,
        }

    except Exception as error:
        await finish_parse_job(
            job_id=job_id,
            status="failed",
            messages_found=messages_found,
            messages_saved=messages_saved,
            error_text=str(error),
        )

        return {
            "ok": False,
            "messages_found": messages_found,
            "messages_saved": messages_saved,
            "messages_from_cache": messages_from_cache,
            "messages_for_user": messages_for_user,
            "filter_stats": filter_stats,
            "channel_stats": channel_stats,
            "useful_messages": useful_messages,
            "structured_messages": structured_messages,
            "error": str(error),
        }
