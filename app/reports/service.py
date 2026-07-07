import asyncio

from aiogram.types import Message

from app.bot import runtime
from app.bot.utils import send_long_text
from app.search.message_ranker import rank_messages_for_query, format_ranked_messages_for_user
from app.utils.debug import debug_print_block, compact_debug_message
from app.ai.report_generator import create_ai_report, format_ai_report_for_user
from app.messages.cleaning import format_filter_stats
from config import TOP_RELEVANT_MESSAGES_LIMIT

def build_found_channel_messages_output(user_context: dict, result: dict) -> str | None:
    query_markup = user_context.get("query_markup") or {}
    structured_messages = result.get("structured_messages") or []
    ranked_messages = rank_messages_for_query(
        query_markup,
        structured_messages,
        limit=TOP_RELEVANT_MESSAGES_LIMIT,
        user_query=user_context.get("search_query"),
    )

    if ranked_messages:
        return format_ranked_messages_for_user(ranked_messages)

    if structured_messages:
        return (
            "Сообщения собраны, но ни одно не набрало положительный score по запросу.\n\n"
            "Большую простыню не показываю: сейчас включён тестовый режим топа по счёту."
        )

    return None


async def build_found_channel_report_output(user_context: dict, result: dict) -> str | None:
    """Берёт топ сообщений по score и просит ИИ сделать отчёт. Если сообщений нет — возвращает None."""
    user_query = user_context.get("search_query") or ""
    query_markup = user_context.get("query_markup") or {}
    structured_messages = result.get("structured_messages") or []

    ranked_messages = rank_messages_for_query(
        query_markup,
        structured_messages,
        limit=TOP_RELEVANT_MESSAGES_LIMIT,
        user_query=user_context.get("search_query"),
    )

    if not ranked_messages:
        if structured_messages:
            return (
                "⚠️ Отчёт не создан\n\n"
                "Причина: сообщения собраны, но ни одно не набрало положительный score по запросу."
            )

        return None

    debug_print_block(
        "Данные, которые отправляются в ИИ-отчёт",
        {
            "user_query": user_query,
            "query_markup": query_markup,
            "report_preset": user_context.get("report_preset") or "brief",
            "ranked_messages": [compact_debug_message(item, max_text_chars=1200) for item in ranked_messages],
        },
    )

    try:
        report = await asyncio.wait_for(
            create_ai_report(
                user_query=user_query,
                query_markup=query_markup,
                ranked_messages=ranked_messages,
                report_preset=user_context.get("report_preset") or "brief",
            ),
            timeout=runtime.AI_REPORT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return (
            "⚠️ Отчёт не создан\n\n"
            f"Причина: ИИ-отчёт не успел подготовиться за {runtime.AI_REPORT_TIMEOUT_SECONDS} сек. "
            "Бот не завис — я специально остановил ожидание."
        )
    except Exception as error:
        return (
            "⚠️ Отчёт не создан\n\n"
            f"Причина: ошибка при подготовке ИИ-отчёта: {type(error).__name__}: {error}"
        )

    return format_ai_report_for_user(report)


def format_collect_result_stats(result: dict) -> str:
    return (
        f"Получено для пользователя: {result['messages_for_user']}\n"
        f"Из них взято из кэша БД: {result['messages_from_cache']}\n"
        f"Проверено новых через Telegram: {result['messages_found']}\n"
        f"Сохранено новых в БД: {result['messages_saved']}"
    )


def format_channel_collect_debug(result: dict) -> str:
    channel_stats = result.get("channel_stats") or []

    if not channel_stats:
        return ""

    lines = ["", "Диагностика по каналам:"]

    for item in channel_stats[:10]:
        line = (
            f"{item.get('username')}: "
            f"получено от Telegram={item.get('telegram_seen', 0)}, "
            f"в периоде={item.get('telegram_in_period', 0)}, "
            f"полезных={item.get('useful_saved', 0)}, "
            f"из кэша={item.get('from_cache', 0)}, "
            f"отфильтровано={item.get('filtered', 0)}, "
            f"реклама={item.get('ads_rejected', 0)}, "
            f"ad_ratio={item.get('ad_ratio', 0)}"
        )

        if item.get("ad_heavy"):
            line += ", ПОМЕЧЕН КАК РЕКЛАМНЫЙ"

        if item.get("error"):
            line += f", ошибка={item['error']}"

        lines.append(line)

    if len(channel_stats) > 10:
        lines.append(f"...ещё каналов: {len(channel_stats) - 10}")

    return "\n".join(lines)


def format_no_messages_explanation(result: dict, period_label: str) -> str:
    details = [
        "⚠️ Отчёт не создан",
        "",
        "Каналы подобраны, но за выбранный период бот не получил полезные текстовые сообщения.",
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
    details.append(
        "Что попробовать: выбрать период «За неделю», увеличить MAX_TELEGRAM_CHECK_LIMIT_PER_CHANNEL "
        "или проверить в терминале диагностику по каналам."
    )

    return "\n".join(details)


async def send_found_channel_report_or_explanation(
    message: Message,
    user_context: dict,
    result: dict,
    period_label: str,
) -> None:
    structured_messages = result.get("structured_messages") or []
    query_markup = user_context.get("query_markup") or {}

    if not structured_messages:
        await send_long_text(
            message,
            format_no_messages_explanation(result, period_label),
        )
        return

    ranked_messages = rank_messages_for_query(
        query_markup,
        structured_messages,
        limit=TOP_RELEVANT_MESSAGES_LIMIT,
        user_query=user_context.get("search_query"),
    )

    if not ranked_messages:
        await send_long_text(
            message,
            (
                "⚠️ Отчёт не создан\n\n"
                "Сообщения собраны, но ни одно не набрало положительный score по запросу.\n\n"
                + format_collect_result_stats(result)
                + format_channel_collect_debug(result)
            ),
        )
        return

    await message.answer(f"Нашёл топ-{len(ranked_messages)} сообщений по счёту. Готовлю ИИ-отчёт...")

    output_text = await build_found_channel_report_output(
        user_context=user_context,
        result=result,
    )

    if output_text:
        await send_long_text(message, output_text)
    else:
        await message.answer("⚠️ Отчёт не создан: неизвестная причина.")
