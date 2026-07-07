"""
Interactive console for browsing and safely deleting Telegram channel AI markup.

This version works ONLY with channel_ai_markup and does not touch channels,
because some existing databases may not have the newer columns in channels.

Run:
    python db_channels_console_v3.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import csv
import json
import shlex
import re
from datetime import datetime, timedelta
from statistics import mean
from pathlib import Path
from textwrap import shorten
from typing import Any

import asyncpg

from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD


HELP_TEXT = """
Команды:

  /help
      Показать команды ещё раз.

  /ex
      Завершить работу.

  users
      Показать последних пользователей бота и их account_type.

  users --q 123456789
      Найти пользователя по telegram_id / id / username / имени.

  set-user-type --telegram-id 123456789 --type admin
      Сделать пользователя админом. После этого он увидит debug-кнопки и /admin.

  set-user-type --telegram-id 123456789 --type user
      Вернуть обычный тип аккаунта.

  set-user-type --id 1 --type admin
      То же самое, но по внутреннему users.id.

  list --category политика --position оппозиция
      Кратко показать каналы по категории и позиции.

  list --category политика --position оппозиция --limit 100
      Кратко показать до 100 каналов.

  list --category политика --position оппозиция --full
      Показать полную информацию по каждому найденному каналу.

  list --category политика --position оппозиция --txt politics_opposition.txt
      Сохранить ссылки найденных каналов в TXT.

  list --category политика --position оппозиция --csv politics_opposition.csv
      Сохранить результат в CSV.

  list --q медуза
      Свободный поиск по username, описанию и ключам.

  list --keyword выборы --keyword расследования
      Поиск по ключевым словам. --keyword можно писать несколько раз.

  stats --group category_position
      Статистика по категориям и позициям.

  stats --group category
      Статистика только по категориям.

  values --field category
  values --field position
  values --field region
  values --field format
      Показать реальные значения, которые сейчас лежат в базе.

  show meduzalive
      Показать полную карточку канала.


  delete-category --category "еда и кулинария"
      Удалить текущую разметку всех каналов в категории.
      Перед удалением покажет количество и попросит ввести DELETE.

  delete-category --category "еда и кулинария" --dry-run
      Только показать, сколько строк будет удалено. Ничего не удаляет.

  delete-category --category "еда и кулинария" --history
      Удалить не только текущую, но и историческую разметку этой категории.

  delete-category --category "кулинар" --contains
      Удалить категории по частичному совпадению. Осторожно.


  fix-done-without-markup --dry-run
      Проверить channels.status='done', но без текущей done-разметки в channel_ai_markup.
      Ничего не меняет, только показывает количество и примеры.

  fix-done-without-markup
      Вернуть такие каналы из done в new.
      Перед изменением попросит ввести FIX.

  fix-done-without-markup --limit 100
      Показать до 100 примеров перед исправлением.


  errors
      Подробно показать ошибки разметки из БД: channels.error и channel_ai_markup.error.

  errors --table channels
      Смотреть ошибки только в channels.

  errors --table markup
      Смотреть ошибки только в channel_ai_markup.

  errors --full
      Показать полный текст ошибок без обрезки.

  errors --group-errors
      Показать только группировку одинаковых ошибок.

  errors --q FloodWait
      Поиск по username / error / warnings.


  count-new
      Посчитать, сколько каналов осталось размечать: channels.status='new'.
      Также покажет разбивку по статусам и примерное число запусков при лимите 60.

  count-new --batch-size 60 --limit 20
      Указать размер пачки и сколько примеров новых каналов вывести.


  reports
      Проанализировать txt-отчёты из папки run_reports и рассчитать, когда примерно запускать разметчик снова.
      Формула: если последний отчёт success — max(40 минут, 1.25 средней длительности успешного запуска); если последний отчёт floodwait — дополнительно учитывается FloodWait с запасом + 10%.

  reports --folder run_reports
      Явно указать папку с отчётами.

  reports --safe-gap-minutes 150
      Задать свой безопасный интервал после успешного запуска, если хочешь переопределить расчёт.

Сокращения:
  l  = list
  ls = list
  s  = stats
  v  = values

Главный пример:
  list --category политика --position оппозиция
""".strip()

POSITION_ALIASES = {
    "оппозиция": ["оппозиция", "оппозиционная", "оппозиционный", "оппозиционное", "оппозиционн"],
    "оппозиционная": ["оппозиция", "оппозиционная", "оппозиционный", "оппозиционное", "оппозиционн"],
    "официальная": ["официальная", "официальный", "официальное", "провластная", "провластный", "государственная", "государственный"],
    "провластная": ["официальная", "официальный", "официальное", "провластная", "провластный", "государственная", "государственный"],
    "нейтральная": ["нейтральная", "нейтральный", "нейтральное", "нейтрал"],
}

ALIASES = {
    "l": "list",
    "ls": "list",
    "s": "stats",
    "v": "values",
    "card": "show",
    "delcat": "delete-category",
    "del-category": "delete-category",
    "fixdone": "fix-done-without-markup",
    "fix-done": "fix-done-without-markup",
    "report": "reports",
    "rep": "reports",
    "new": "count-new",
    "countnew": "count-new",
    "queue": "count-new",
    "err": "errors",
    "error": "errors",
}

FIELD_SQL = {
    "category": "m.category",
    "position": "m.position_label",
    "region": "m.region",
    "format": "m.content_format",
}

SELECT_FIELDS = """
SELECT
    m.id,
    m.channel_id,
    m.username,
    m.category,
    m.position_label,
    m.region,
    m.content_format,
    m.confidence,
    m.model,
    m.status,
    m.ai_keywords,
    m.ai_description,
    m.ai_classification,
    m.ai_warnings,
    m.processed_at
"""

BASE_FROM = """
FROM channel_ai_markup m
"""


def parse_json_maybe(value: Any, default: Any) -> Any:
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


def normalize_username(username: str) -> str:
    username = username.strip()
    username = username.removeprefix("https://t.me/")
    username = username.removeprefix("http://t.me/")
    username = username.removeprefix("t.me/")
    username = username.removeprefix("@")
    return username.strip().strip("/")


def channel_url(username: str) -> str:
    return f"https://t.me/{username}"


class QueryBuilder:
    def __init__(self) -> None:
        self.where: list[str] = ["m.is_current = TRUE"]
        self.params: list[Any] = []

    def add_param(self, value: Any) -> str:
        self.params.append(value)
        return f"${len(self.params)}"

    def add_equal_or_ilike(self, field_expr: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        exact = self.add_param(value.lower())
        like = self.add_param(f"%{value}%")
        self.where.append(f"(LOWER({field_expr}) = {exact} OR {field_expr} ILIKE {like})")

    def add_position(self, value: str) -> None:
        value = value.strip()
        if not value:
            return
        variants = POSITION_ALIASES.get(value.lower(), [value])
        parts = []
        for variant in variants:
            p = self.add_param(f"%{variant}%")
            parts.append(f"m.position_label ILIKE {p}")
        self.where.append("(" + " OR ".join(parts) + ")")

    def build_where(self) -> str:
        return "WHERE " + "\n  AND ".join(self.where)


def parse_flags(tokens: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "keyword": [],
        "limit": 50,
        "offset": 0,
        "group": "category_position",
        "dry_run": False,
        "history": False,
        "contains": False,
        "full": False,
        "batch_size": 60,
        "table": "both",
        "group_errors": False,
    }

    flags_with_value = {
        "--category": "category",
        "--position": "position",
        "--region": "region",
        "--format": "format",
        "--q": "q",
        "--status": "status",
        "--limit": "limit",
        "--offset": "offset",
        "--txt": "txt",
        "--csv": "csv",
        "--json": "json",
        "--group": "group",
        "--field": "field",
        "--keyword": "keyword",
        "--folder": "folder",
        "--safe-gap-minutes": "safe_gap_minutes",
        "--batch-size": "batch_size",
        "--table": "table",
        "--telegram-id": "telegram_id",
        "--id": "id",
        "--type": "type",
    }

    boolean_flags = {
        "--dry-run": "dry_run",
        "--history": "history",
        "--contains": "contains",
        "--full": "full",
        "--group-errors": "group_errors",
    }

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in boolean_flags:
            result[boolean_flags[token]] = True
            i += 1
            continue

        if token not in flags_with_value:
            raise ValueError(f"неизвестный параметр: {token}")
        if i + 1 >= len(tokens):
            raise ValueError(f"после {token} нужно указать значение")

        key = flags_with_value[token]
        value = tokens[i + 1]

        if key == "keyword":
            result["keyword"].append(value)
        elif key in {"limit", "offset", "safe_gap_minutes", "batch_size"}:
            try:
                result[key] = int(value)
            except ValueError:
                raise ValueError(f"{token} должен быть числом") from None
        else:
            result[key] = value

        i += 2

    return result


def add_common_filters(qb: QueryBuilder, opts: dict[str, Any]) -> None:
    if opts.get("category"):
        qb.add_equal_or_ilike("m.category", opts["category"])
    if opts.get("position"):
        qb.add_position(opts["position"])
    if opts.get("region"):
        qb.add_equal_or_ilike("m.region", opts["region"])
    if opts.get("format"):
        qb.add_equal_or_ilike("m.content_format", opts["format"])
    if opts.get("status"):
        qb.add_equal_or_ilike("m.status", opts["status"])

    for keyword in opts.get("keyword") or []:
        p = qb.add_param(f"%{keyword}%")
        qb.where.append(
            "(m.ai_keywords::text ILIKE " + p +
            " OR COALESCE(m.ai_description, '') ILIKE " + p + ")"
        )

    if opts.get("q"):
        p = qb.add_param(f"%{opts['q']}%")
        qb.where.append(
            "(m.username ILIKE " + p +
            " OR COALESCE(m.ai_description, '') ILIKE " + p +
            " OR m.ai_keywords::text ILIKE " + p + ")"
        )


def rows_to_dicts(rows: list[asyncpg.Record]) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        item = dict(row)
        item["ai_keywords"] = parse_json_maybe(item.get("ai_keywords"), [])
        item["ai_classification"] = parse_json_maybe(item.get("ai_classification"), {})
        item["ai_warnings"] = parse_json_maybe(item.get("ai_warnings"), [])
        items.append(item)
    return items


def print_short_table(items: list[dict[str, Any]]) -> None:
    """Print a compact table for quick browsing."""
    if not items:
        print("Ничего не найдено.")
        return

    headers = ["#", "username", "category", "position", "region", "format", "keywords", "description"]
    table = []
    for i, item in enumerate(items, start=1):
        keywords = item.get("ai_keywords") or []
        if isinstance(keywords, list):
            keywords_text = ", ".join(str(x) for x in keywords[:5])
        else:
            keywords_text = str(keywords)

        table.append([
            str(i),
            "@" + str(item.get("username") or ""),
            shorten(str(item.get("category") or ""), width=28, placeholder="…"),
            shorten(str(item.get("position_label") or ""), width=20, placeholder="…"),
            shorten(str(item.get("region") or ""), width=18, placeholder="…"),
            shorten(str(item.get("content_format") or ""), width=22, placeholder="…"),
            shorten(keywords_text, width=42, placeholder="…"),
            shorten(str(item.get("ai_description") or ""), width=70, placeholder="…"),
        ])

    widths = [len(h) for h in headers]
    for row in table:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in table:
        print(fmt(row))


def print_full_channels(items: list[dict[str, Any]]) -> None:
    """Print full channel cards."""
    if not items:
        print("Ничего не найдено.")
        return

    for i, item in enumerate(items, start=1):
        username = str(item.get("username") or "")
        keywords = item.get("ai_keywords") or []
        warnings = item.get("ai_warnings") or []
        classification = item.get("ai_classification") or {}

        print("=" * 100)
        print(f"{i}. @{username}")
        print(f"URL: {channel_url(username)}")
        print(f"markup_id: {item.get('id') or ''}")
        print(f"channel_id: {item.get('channel_id') or ''}")
        print(f"status: {item.get('status') or ''}")
        print(f"category: {item.get('category') or ''}")
        print(f"position_label: {item.get('position_label') or ''}")
        print(f"region: {item.get('region') or ''}")
        print(f"content_format: {item.get('content_format') or ''}")
        print(f"confidence: {item.get('confidence') or ''}")
        print(f"model: {item.get('model') or ''}")
        print(f"processed_at: {item.get('processed_at') or ''}")

        print("\nai_keywords:")
        if isinstance(keywords, list):
            if keywords:
                for keyword in keywords:
                    print(f"  - {keyword}")
            else:
                print("  []")
        else:
            print(str(keywords))

        print("\nai_description:")
        print(item.get("ai_description") or "")

        print("\nai_warnings:")
        if isinstance(warnings, list):
            if warnings:
                for warning in warnings:
                    print(f"  - {warning}")
            else:
                print("  []")
        else:
            print(str(warnings))

        print("\nai_classification:")
        print(json.dumps(classification, ensure_ascii=False, indent=2))

    print("=" * 100)


def export_files(items: list[dict[str, Any]], opts: dict[str, Any]) -> None:
    if opts.get("txt"):
        path = Path(opts["txt"])
        lines = [channel_url(str(item["username"])) for item in items]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        print(f"\nTXT сохранён: {path}")

    if opts.get("json"):
        path = Path(opts["json"])
        serializable = []
        for item in items:
            data = dict(item)
            for key, value in list(data.items()):
                if hasattr(value, "isoformat"):
                    data[key] = value.isoformat()
            serializable.append(data)
        path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON сохранён: {path}")

    if opts.get("csv"):
        path = Path(opts["csv"])
        fields = ["username", "category", "position_label", "region", "content_format", "ai_keywords", "ai_description", "url"]
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for item in items:
                row = dict(item)
                row["ai_keywords"] = ", ".join(row.get("ai_keywords") or []) if isinstance(row.get("ai_keywords"), list) else row.get("ai_keywords")
                row["url"] = channel_url(str(row.get("username") or ""))
                writer.writerow(row)
        print(f"CSV сохранён: {path}")


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=1,
        max_size=3,
    )


async def cmd_list(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    qb = QueryBuilder()
    add_common_filters(qb, opts)

    limit_p = qb.add_param(opts.get("limit", 50))
    offset_p = qb.add_param(opts.get("offset", 0))

    sql = f"""
{SELECT_FIELDS}
{BASE_FROM}
{qb.build_where()}
ORDER BY
    m.category NULLS LAST,
    m.position_label NULLS LAST,
    m.username ASC
LIMIT {limit_p}
OFFSET {offset_p};
"""
    async with pool.acquire() as con:
        rows = await con.fetch(sql, *qb.params)

    items = rows_to_dicts(rows)
    if opts.get("full"):
        print_full_channels(items)
    else:
        print_short_table(items)
    print(f"\nПоказано: {len(items)}")
    export_files(items, opts)


async def cmd_stats(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    group = opts.get("group", "category_position")
    group_map = {
        "category": [("category", "m.category")],
        "position": [("position", "m.position_label")],
        "region": [("region", "m.region")],
        "format": [("format", "m.content_format")],
        "category_position": [("category", "m.category"), ("position", "m.position_label")],
        "category_region": [("category", "m.category"), ("region", "m.region")],
    }
    if group not in group_map:
        print("Неизвестная группировка. Варианты: " + ", ".join(group_map))
        return

    groups = group_map[group]
    qb = QueryBuilder()
    add_common_filters(qb, opts)
    select_group = ",\n    ".join(f"{expr} AS {name}" for name, expr in groups)
    group_by = ", ".join(str(i) for i in range(1, len(groups) + 1))

    sql = f"""
SELECT
    {select_group},
    COUNT(*) AS count
{BASE_FROM}
{qb.build_where()}
GROUP BY {group_by}
ORDER BY count DESC, {", ".join(name for name, _ in groups)} NULLS LAST
LIMIT 200;
"""
    async with pool.acquire() as con:
        rows = await con.fetch(sql, *qb.params)

    if not rows:
        print("Ничего не найдено.")
        return

    headers = [name for name, _ in groups] + ["count"]
    table = [[str(row.get(h) or "") for h in headers] for row in rows]
    widths = [len(h) for h in headers]
    for row in table:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    def fmt(row: list[str]) -> str:
        return "  ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))

    print(fmt(headers))
    print(fmt(["-" * w for w in widths]))
    for row in table:
        print(fmt(row))


async def cmd_values(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    field = opts.get("field")
    if field not in FIELD_SQL:
        print("Нужно указать поле: values --field category|position|region|format")
        return

    qb = QueryBuilder()
    sql = f"""
SELECT {FIELD_SQL[field]} AS value, COUNT(*) AS count
{BASE_FROM}
{qb.build_where()}
GROUP BY 1
ORDER BY count DESC, value NULLS LAST;
"""
    async with pool.acquire() as con:
        rows = await con.fetch(sql, *qb.params)

    for row in rows:
        print(f"{row['value'] or '<пусто>'}: {row['count']}")


async def cmd_show(pool: asyncpg.Pool, tokens: list[str]) -> None:
    if not tokens:
        print("Нужно указать канал: show meduzalive")
        return

    username = normalize_username(tokens[0])
    qb = QueryBuilder()
    p = qb.add_param(username.lower())
    qb.where.append(f"LOWER(m.username) = {p}")

    sql = f"""
{SELECT_FIELDS}
{BASE_FROM}
{qb.build_where()}
ORDER BY m.processed_at DESC NULLS LAST, m.id DESC
LIMIT 1;
"""
    async with pool.acquire() as con:
        row = await con.fetchrow(sql, *qb.params)

    if row is None:
        print("Канал не найден.")
        return

    item = rows_to_dicts([row])[0]
    print(f"@{item['username']}")
    print(f"URL: {channel_url(item['username'])}")
    print(f"Категория: {item.get('category') or ''}")
    print(f"Позиция: {item.get('position_label') or ''}")
    print(f"Регион: {item.get('region') or ''}")
    print(f"Формат: {item.get('content_format') or ''}")
    print(f"Статус: {item.get('status') or ''}")
    print(f"Модель: {item.get('model') or ''}; уверенность: {item.get('confidence') or ''}; дата разметки: {item.get('processed_at') or ''}")
    print("\nКлючи:")
    print(", ".join(item.get("ai_keywords") or []))
    print("\nОписание:")
    print(item.get("ai_description") or "")
    print("\nПолная ai_classification:")
    print(json.dumps(item.get("ai_classification") or {}, ensure_ascii=False, indent=2))


REPORT_DT_FORMAT = "%d.%m.%Y %H:%M:%S"


def parse_report_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().rstrip(".")
    try:
        return datetime.strptime(value, REPORT_DT_FORMAT)
    except ValueError:
        return None


def parse_report_seconds(text: str, label: str) -> int | None:
    pattern = re.escape(label) + r"\s*:?\s*(\d+)\s*сек"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def parse_report_int(text: str, label: str) -> int | None:
    pattern = re.escape(label) + r"\s*:?\s*(\d+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def parse_report_value(text: str, label: str) -> str | None:
    pattern = re.escape(label) + r"\s*:?\s*(.+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def parse_run_report(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")

    status = parse_report_value(text, "Статус") or "unknown"
    report_time = parse_report_datetime(parse_report_value(text, "Время"))
    started_at = parse_report_datetime(parse_report_value(text, "Время запуска"))
    finished_at = parse_report_datetime(parse_report_value(text, "Время завершения"))
    can_continue_after = parse_report_datetime(parse_report_value(text, "Можно попробовать продолжить после"))

    duration_seconds = None
    if started_at and finished_at:
        duration_seconds = int((finished_at - started_at).total_seconds())

    return {
        "path": path,
        "file": path.name,
        "status": status.lower(),
        "report_time": report_time,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "limit": parse_report_int(text, "Лимит обработки за запуск"),
        "taken": parse_report_int(text, "Каналов взял из очереди"),
        "marked": parse_report_int(text, "Каналов без ошибок размечено"),
        "not_channel": parse_report_int(text, "Не канал / username пользователя или бота"),
        "not_enough_data": parse_report_int(text, "Недостаточно данных для разметки"),
        "ai_errors": parse_report_int(text, "Ошибок ИИ"),
        "telegram_errors": parse_report_int(text, "Telegram-ошибок канала"),
        "unexpected_errors": parse_report_int(text, "Неожиданных ошибок канала"),
        "flood_stops": parse_report_int(text, "Остановок из-за большого FloodWait"),
        "flood_wait_seconds": parse_report_seconds(text, "Telegram FloodWait: нужно подождать"),
        "flood_wait_with_margin_seconds": parse_report_seconds(text, "С учётом запаса"),
        "can_continue_after": can_continue_after,
    }


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "неизвестно"
    return value.strftime(REPORT_DT_FORMAT)


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "неизвестно"
    seconds = int(seconds)
    if seconds < 0:
        seconds = 0
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m:
        parts.append(f"{m} мин")
    if s or not parts:
        parts.append(f"{s} сек")
    return " ".join(parts)


def report_sort_key(item: dict[str, Any]) -> datetime:
    return item.get("started_at") or item.get("report_time") or datetime.min


def short_text(value: Any, width: int = 180) -> str:
    text = "" if value is None else str(value)
    return shorten(text.replace("\n", " "), width=width, placeholder="…")


def sql_text_col(columns: set[str], name: str, alias: str = "t") -> str:
    if name in columns:
        return f"{alias}.{name}"
    return "NULL::text"


def sql_bigint_col(columns: set[str], name: str, alias: str = "t") -> str:
    if name in columns:
        return f"{alias}.{name}"
    return "NULL::bigint"


def sql_timestamp_col(columns: set[str], name: str, alias: str = "t") -> str:
    if name in columns:
        return f"{alias}.{name}"
    return "NULL::timestamp"


def sql_jsonb_col(columns: set[str], name: str, alias: str = "t") -> str:
    if name in columns:
        return f"{alias}.{name}"
    return "'[]'::jsonb"


async def fetch_error_rows(
    pool: asyncpg.Pool,
    table_name: str,
    opts: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    columns = await get_table_columns(pool, table_name)
    if not columns:
        return [], []

    has_status = "status" in columns
    has_error = "error" in columns
    has_warnings = table_name == "channel_ai_markup" and "ai_warnings" in columns

    if not has_status and not has_error and not has_warnings:
        return [], []

    conditions: list[str] = []
    if has_status:
        conditions.append("LOWER(t.status) = 'error'")
    if has_error:
        conditions.append("NULLIF(TRIM(t.error), '') IS NOT NULL")
    if has_warnings:
        conditions.append("t.ai_warnings IS NOT NULL AND t.ai_warnings <> '[]'::jsonb")

    where_sql = "(" + " OR ".join(conditions) + ")"
    params: list[Any] = []

    if opts.get("q"):
        params.append(f"%{opts['q']}%")
        p = f"${len(params)}"
        q_parts = []
        if "username" in columns:
            q_parts.append(f"t.username ILIKE {p}")
        if has_error:
            q_parts.append(f"t.error ILIKE {p}")
        if has_warnings:
            q_parts.append(f"t.ai_warnings::text ILIKE {p}")
        if q_parts:
            where_sql += " AND (" + " OR ".join(q_parts) + ")"

    limit = int(opts.get("limit", 50))
    params_for_rows = list(params)
    params_for_rows.append(limit)
    limit_p = f"${len(params_for_rows)}"

    id_expr = sql_bigint_col(columns, "id")
    channel_id_expr = sql_bigint_col(columns, "channel_id")
    username_expr = sql_text_col(columns, "username")
    status_expr = sql_text_col(columns, "status")
    error_expr = sql_text_col(columns, "error")
    warnings_expr = sql_jsonb_col(columns, "ai_warnings")

    date_candidates = ["updated_at", "processed_at", "created_at"]
    order_col = next((col for col in date_candidates if col in columns), None)
    order_sql = f"t.{order_col} DESC NULLS LAST, t.id DESC" if order_col and "id" in columns else "1"
    date_expr = sql_timestamp_col(columns, order_col or "updated_at")

    rows_sql = f"""
SELECT
    {id_expr} AS id,
    {channel_id_expr} AS channel_id,
    {username_expr} AS username,
    {status_expr} AS status,
    {error_expr} AS error,
    {warnings_expr} AS ai_warnings,
    {date_expr} AS event_time
FROM {table_name} t
WHERE {where_sql}
ORDER BY {order_sql}
LIMIT {limit_p};
"""

    group_error_expr = "'<нет колонки error>'::text"
    if has_error:
        group_error_expr = "COALESCE(NULLIF(TRIM(t.error), ''), '<без текста ошибки>')"
    elif has_warnings:
        group_error_expr = "COALESCE(NULLIF(TRIM(t.ai_warnings::text), ''), '<без текста ошибки>')"

    group_sql = f"""
SELECT
    {group_error_expr} AS error_text,
    COUNT(*) AS count
FROM {table_name} t
WHERE {where_sql}
GROUP BY 1
ORDER BY count DESC, error_text ASC
LIMIT 30;
"""

    async with pool.acquire() as con:
        group_rows = await con.fetch(group_sql, *params)
        rows = await con.fetch(rows_sql, *params_for_rows)

    parsed_rows = []
    for row in rows:
        item = dict(row)
        item["source_table"] = table_name
        item["ai_warnings"] = parse_json_maybe(item.get("ai_warnings"), [])
        parsed_rows.append(item)

    parsed_groups = []
    for row in group_rows:
        item = dict(row)
        item["source_table"] = table_name
        parsed_groups.append(item)

    return parsed_rows, parsed_groups


def print_error_groups(groups: list[dict[str, Any]]) -> None:
    if not groups:
        print("Групп ошибок не найдено.")
        return

    print("Группировка ошибок:")
    for i, item in enumerate(groups, start=1):
        print(f"\n{i}. [{item['source_table']}] count={item['count']}")
        print(short_text(item.get("error_text"), width=260))


def print_error_rows(rows: list[dict[str, Any]], full: bool = False) -> None:
    if not rows:
        print("Строк с ошибками не найдено.")
        return

    print("\nПодробные строки ошибок:")
    for i, item in enumerate(rows, start=1):
        username = item.get("username") or ""
        error_text = item.get("error") or ""
        warnings = item.get("ai_warnings") or []

        if not full:
            print(
                f"{i}. [{item['source_table']}] "
                f"id={item.get('id') or ''} | "
                f"channel_id={item.get('channel_id') or ''} | "
                f"@{username} | status={item.get('status') or ''} | "
                f"time={item.get('event_time') or ''} | "
                f"error={short_text(error_text, width=180)}"
            )
            if warnings:
                print(f"   warnings={short_text(json.dumps(warnings, ensure_ascii=False), width=180)}")
            continue

        print("=" * 100)
        print(f"{i}. source_table: {item['source_table']}")
        print(f"id: {item.get('id') or ''}")
        print(f"channel_id: {item.get('channel_id') or ''}")
        print(f"username: @{username}")
        print(f"status: {item.get('status') or ''}")
        print(f"event_time: {item.get('event_time') or ''}")
        print("\nerror:")
        print(error_text)
        print("\nai_warnings:")
        print(json.dumps(warnings, ensure_ascii=False, indent=2))
    if full:
        print("=" * 100)


async def cmd_errors(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    table_mode = (opts.get("table") or "both").lower().strip()
    if table_mode not in {"both", "channels", "markup", "channel_ai_markup"}:
        print("--table должен быть: both, channels или markup")
        return

    tables = []
    if table_mode in {"both", "channels"}:
        tables.append("channels")
    if table_mode in {"both", "markup", "channel_ai_markup"}:
        tables.append("channel_ai_markup")

    all_rows: list[dict[str, Any]] = []
    all_groups: list[dict[str, Any]] = []

    for table in tables:
        rows, groups = await fetch_error_rows(pool, table, opts)
        all_rows.extend(rows)
        all_groups.extend(groups)

    print("Ошибки разметки")
    print("=" * 80)
    print(f"Источник: {table_mode}")
    print(f"Лимит строк: {opts.get('limit', 50)}")
    if opts.get("q"):
        print(f"Поиск: {opts['q']}")
    print()

    print_error_groups(all_groups)

    if opts.get("group_errors"):
        return

    print_error_rows(all_rows, full=bool(opts.get("full")))


async def cmd_count_new(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    channels_columns = await get_table_columns(pool, "channels")
    required = {"id", "username", "status"}
    missing = sorted(required - channels_columns)
    if missing:
        print("Не хватает колонок в channels: " + ", ".join(missing))
        return

    limit = int(opts.get("limit", 20))
    batch_size = int(opts.get("batch_size", 60))
    if batch_size <= 0:
        batch_size = 60

    has_is_available = "is_available" in channels_columns
    has_created_at = "created_at" in channels_columns

    order_expr = "created_at ASC, id ASC" if has_created_at else "id ASC"

    new_available_where = "LOWER(status) = 'new'"
    if has_is_available:
        new_available_where += " AND is_available = TRUE"

    status_sql = """
SELECT COALESCE(status, '<NULL>') AS status, COUNT(*) AS count
FROM channels
GROUP BY 1
ORDER BY count DESC, status ASC;
"""

    total_new_sql = "SELECT COUNT(*) FROM channels WHERE LOWER(status) = 'new';"
    available_new_sql = f"SELECT COUNT(*) FROM channels WHERE {new_available_where};"

    preview_select = "id, username, status"
    if has_is_available:
        preview_select += ", is_available"

    preview_sql = f"""
SELECT {preview_select}
FROM channels
WHERE {new_available_where}
ORDER BY {order_expr}
LIMIT $1;
"""

    async with pool.acquire() as con:
        status_rows = await con.fetch(status_sql)
        total_new = await con.fetchval(total_new_sql)
        available_new = await con.fetchval(available_new_sql)
        preview_rows = await con.fetch(preview_sql, limit)

    runs_needed = (int(available_new) + batch_size - 1) // batch_size if available_new else 0

    print("Осталось размечать")
    print("=" * 80)
    print(f"channels.status = 'new': {total_new}")
    if has_is_available:
        print(f"new и is_available = TRUE: {available_new}")
    else:
        print("Колонки is_available нет, считаю все new как доступные.")
    print(f"Размер пачки: {batch_size}")
    print(f"Примерно запусков осталось: {runs_needed}")

    print("\nРазбивка по status:")
    for row in status_rows:
        print(f"  {row['status']}: {row['count']}")

    if preview_rows:
        print(f"\nПервые {len(preview_rows)} каналов в очереди new:")
        for row in preview_rows:
            suffix = ""
            if has_is_available:
                suffix = f" | is_available={row['is_available']}"
            print(f"  id={row['id']} | @{row['username']} | status={row['status']}{suffix}")
    else:
        print("\nОчередь new пустая.")


async def cmd_reports(opts: dict[str, Any]) -> None:
    folder = Path(opts.get("folder") or "run_reports")
    if not folder.exists() or not folder.is_dir():
        print(f"Папка с отчётами не найдена: {folder}")
        print("Положи отчёты в папку run_reports или укажи: reports --folder путь_к_папке")
        return

    paths = sorted(folder.glob("*.txt"))
    if not paths:
        print(f"В папке нет txt-отчётов: {folder}")
        return

    reports = []
    for path in paths:
        try:
            reports.append(parse_run_report(path))
        except Exception as exc:
            print(f"Не смог прочитать {path.name}: {exc}")

    reports = [r for r in reports if r.get("started_at") or r.get("report_time")]
    reports.sort(key=report_sort_key)

    if not reports:
        print("Не получилось разобрать ни один отчёт.")
        return

    success_reports = [r for r in reports if r["status"] == "success"]
    flood_reports = [r for r in reports if r["status"] == "floodwait"]
    latest = reports[-1]
    latest_flood = flood_reports[-1] if flood_reports else None

    success_durations = [r["duration_seconds"] for r in success_reports if r.get("duration_seconds")]
    success_taken = [r["taken"] for r in success_reports if r.get("taken") is not None]
    success_marked = [r["marked"] for r in success_reports if r.get("marked") is not None]
    flood_waits = [r["flood_wait_with_margin_seconds"] or r["flood_wait_seconds"] for r in flood_reports if (r.get("flood_wait_with_margin_seconds") or r.get("flood_wait_seconds"))]

    avg_success_duration = int(mean(success_durations)) if success_durations else 0
    avg_taken = mean(success_taken) if success_taken else 0
    avg_marked = mean(success_marked) if success_marked else 0

    print("Аналитика run_reports")
    print("=" * 80)
    print(f"Папка: {folder}")
    print(f"Отчётов разобрано: {len(reports)}")
    print(f"Успешных запусков: {len(success_reports)}")
    print(f"FloodWait-остановок: {len(flood_reports)}")
    print(f"Среднее время успешного запуска: {format_duration(avg_success_duration)}")
    if success_reports:
        print(f"Средне каналов взято за успешный запуск: {avg_taken:.1f}")
        print(f"Средне каналов размечено без ошибок: {avg_marked:.1f}")

    print("\nПоследние отчёты:")
    for r in reports[-8:]:
        print(
            f"  {format_dt(r.get('started_at'))} -> {format_dt(r.get('finished_at'))} | "
            f"{r['status']} | взял={r.get('taken')} | размечено={r.get('marked')} | файл={r['file']}"
        )

    print("\nРекомендация по следующему запуску")
    print("=" * 80)

    if latest["status"] == "floodwait":
        print("!!! В ПРОШЛЫЙ РАЗ БЫЛ FLOODWAIT !!!")
        print("!!! НЕ ЗАПУСКАЙ СРАЗУ. СНАЧАЛА ДОЖДИСЬ РЕКОМЕНДОВАННОГО ВРЕМЕНИ !!!")
        print()

    latest_finish = latest.get("finished_at") or latest.get("report_time")
    if latest_finish is None:
        print("Не смог определить время завершения последнего отчёта.")
        return

    if opts.get("safe_gap_minutes"):
        recommended_gap = timedelta(minutes=int(opts["safe_gap_minutes"]))
        reason = "задан вручную через --safe-gap-minutes"
    else:
        min_gap = timedelta(minutes=40)
        by_duration = timedelta(seconds=max(avg_success_duration * 1.25, 0))

        if latest["status"] == "floodwait":
            flood_raw = latest.get("flood_wait_seconds") or 0
            flood_with_margin = latest.get("flood_wait_with_margin_seconds") or flood_raw
            by_flood = timedelta(seconds=int(flood_with_margin + flood_raw * 0.10))
            recommended_gap = max(min_gap, by_duration, by_flood)
            reason = "расчёт: последний отчёт FloodWait -> max(40 минут, 1.25 средней длительности успешного запуска, FloodWait с запасом + 10%)"
        else:
            recommended_gap = max(min_gap, by_duration)
            reason = "расчёт: последний отчёт не FloodWait -> max(40 минут, 1.25 средней длительности успешного запуска)"

    minimal_gap = timedelta(minutes=40)
    very_safe_gap = max(timedelta(hours=2), recommended_gap)

    minimal_time = latest_finish + minimal_gap
    recommended_time = latest_finish + recommended_gap
    very_safe_time = latest_finish + very_safe_gap

    print(f"Последний отчёт: {latest['status']} | завершение: {format_dt(latest_finish)}")
    if latest["status"] == "floodwait" and latest.get("can_continue_after"):
        print(f"Telegram в отчёте разрешал попробовать после: {format_dt(latest['can_continue_after'])}")
    print(f"Причина расчёта: {reason}")
    print(f"Минимально по формуле не раньше: {format_dt(minimal_time)}")
    print(f"Рекомендую запускать примерно после: {format_dt(recommended_time)}")
    print(f"Осторожный вариант: {format_dt(very_safe_time)}")

    now = datetime.now()
    if now >= recommended_time:
        print("\nПо текущему времени: рекомендуемое время уже прошло, запускать можно осторожно.")
    else:
        print(f"\nПо текущему времени ждать ещё примерно: {format_duration((recommended_time - now).total_seconds())}")


async def get_table_columns(pool: asyncpg.Pool, table_name: str) -> set[str]:
    async with pool.acquire() as con:
        rows = await con.fetch(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = $1;
            """,
            table_name,
        )
    return {row["column_name"] for row in rows}


async def cmd_fix_done_without_markup(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    """
    channels.status='done' means channel should have current done markup.
    If not, return channel to status='new'.
    """
    channels_columns = await get_table_columns(pool, "channels")
    markup_columns = await get_table_columns(pool, "channel_ai_markup")

    required_channels = {"id", "username", "status"}
    required_markup = {"channel_id", "username", "status", "is_current"}

    missing_channels = sorted(required_channels - channels_columns)
    missing_markup = sorted(required_markup - markup_columns)

    if missing_channels:
        print("Не хватает колонок в channels: " + ", ".join(missing_channels))
        return
    if missing_markup:
        print("Не хватает колонок в channel_ai_markup: " + ", ".join(missing_markup))
        return

    limit = int(opts.get("limit", 50))

    not_exists_sql = """
        NOT EXISTS (
            SELECT 1
            FROM channel_ai_markup m
            WHERE m.is_current = TRUE
              AND LOWER(m.status) = 'done'
              AND (
                    m.channel_id = c.id
                    OR LOWER(m.username) = LOWER(c.username)
                  )
        )
    """

    count_sql = f"""
SELECT COUNT(*)
FROM channels c
WHERE LOWER(c.status) = 'done'
  AND {not_exists_sql};
"""

    preview_sql = f"""
SELECT c.id, c.username, c.status
FROM channels c
WHERE LOWER(c.status) = 'done'
  AND {not_exists_sql}
ORDER BY c.id ASC
LIMIT $1;
"""

    async with pool.acquire() as con:
        count = await con.fetchval(count_sql)
        preview_rows = await con.fetch(preview_sql, limit)

    print("Проверка: channels.status='done' без текущей done-разметки в channel_ai_markup")
    print(f"Найдено каналов для возврата в new: {count}")

    if preview_rows:
        print("\nПримеры:")
        for row in preview_rows:
            print(f"  id={row['id']} | @{row['username']} | status={row['status']}")

    if not count:
        print("\nИсправлять нечего.")
        return

    if opts.get("dry_run"):
        print("\nDry-run: ничего не изменено.")
        return

    confirm = input("\nДля подтверждения исправления напиши FIX: ").strip()
    if confirm != "FIX":
        print("Исправление отменено.")
        return

    set_parts = ["status = 'new'"]
    if "updated_at" in channels_columns:
        set_parts.append("updated_at = NOW()")
    if "processed_at" in channels_columns:
        set_parts.append("processed_at = NULL")

    set_sql = ",\n    ".join(set_parts)

    update_sql = f"""
UPDATE channels c
SET
    {set_sql}
WHERE LOWER(c.status) = 'done'
  AND {not_exists_sql}
RETURNING c.id, c.username;
"""

    async with pool.acquire() as con:
        rows = await con.fetch(update_sql)

    print(f"Готово. Возвращено в new: {len(rows)}")
    if rows:
        print("\nПервые исправленные:")
        for row in rows[:20]:
            print(f"  id={row['id']} | @{row['username']}")


async def cmd_delete_category(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    category = (opts.get("category") or "").strip()
    if not category:
        print('Нужно указать категорию: delete-category --category "еда и кулинария"')
        return

    params: list[Any] = []

    def add_param(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    where_parts: list[str] = []
    if not opts.get("history"):
        where_parts.append("is_current = TRUE")

    if opts.get("contains"):
        p = add_param(f"%{category}%")
        where_parts.append(f"category ILIKE {p}")
        match_mode = "частичное совпадение"
    else:
        p = add_param(category)
        where_parts.append(f"LOWER(TRIM(category)) = LOWER(TRIM({p}))")
        match_mode = "точное совпадение"

    where_sql = "WHERE " + " AND ".join(where_parts)

    count_sql = f"SELECT COUNT(*) FROM channel_ai_markup {where_sql};"
    preview_sql = f"""
SELECT username, category, position_label, region, content_format
FROM channel_ai_markup
{where_sql}
ORDER BY username ASC
LIMIT 20;
"""

    async with pool.acquire() as con:
        count = await con.fetchval(count_sql, *params)
        preview_rows = await con.fetch(preview_sql, *params)

    print(f"Категория: {category}")
    print(f"Режим совпадения: {match_mode}")
    print(f"Удалять историю: {'да' if opts.get('history') else 'нет, только is_current = TRUE'}")
    print(f"Будет удалено строк из channel_ai_markup: {count}")

    if preview_rows:
        print("\nПервые строки под удаление:")
        for row in preview_rows:
            print(
                f"  @{row['username']} | "
                f"category={row['category'] or ''} | "
                f"position={row['position_label'] or ''} | "
                f"region={row['region'] or ''} | "
                f"format={row['content_format'] or ''}"
            )

    if not count:
        print("\nУдалять нечего.")
        return

    if opts.get("dry_run"):
        print("\nDry-run: ничего не удалено.")
        return

    confirm = input('\nДля подтверждения удаления напиши DELETE: ').strip()
    if confirm != "DELETE":
        print("Удаление отменено.")
        return

    delete_sql = f"DELETE FROM channel_ai_markup {where_sql};"
    async with pool.acquire() as con:
        result = await con.execute(delete_sql, *params)

    print(f"Готово. {result}")


def normalize_account_type_console(value: str | None) -> str:
    raw = str(value or "user").strip().lower()
    if raw in {"admin", "administrator", "админ"}:
        return "admin"
    if raw in {"user", "обычный", "default"}:
        return "user"
    raise ValueError("--type должен быть admin или user")


async def ensure_user_account_type_schema(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as con:
        await con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type TEXT NOT NULL DEFAULT 'user';")
        await con.execute("""
            UPDATE users
            SET account_type = 'user'
            WHERE account_type IS NULL OR account_type NOT IN ('user', 'admin');
        """)
        await con.execute("CREATE INDEX IF NOT EXISTS idx_users_account_type ON users (account_type);")


async def cmd_users(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    limit = int(opts.get("limit") or 50)
    limit = max(1, min(limit, 200))
    query = opts.get("q")

    where_sql = ""
    params: list[Any] = []
    if query:
        params.append(f"%{query.strip()}%")
        where_sql = """
        WHERE username ILIKE $1
           OR first_name ILIKE $1
           OR last_name ILIKE $1
           OR telegram_id::text ILIKE $1
           OR id::text ILIKE $1
        """

    sql = f"""
        SELECT
            id,
            telegram_id,
            username,
            first_name,
            last_name,
            COALESCE(account_type, 'user') AS account_type,
            created_at,
            updated_at
        FROM users
        {where_sql}
        ORDER BY updated_at DESC NULLS LAST, id DESC
        LIMIT {limit};
    """

    async with pool.acquire() as con:
        rows = await con.fetch(sql, *params)

    if not rows:
        print("Пользователи не найдены.")
        return

    print(f"\nПользователи: {len(rows)}")
    print("id | telegram_id | type | username | name | updated_at")
    print("-" * 100)
    for row in rows:
        username = row["username"] or ""
        name = " ".join(part for part in [row["first_name"], row["last_name"]] if part) or ""
        updated_at = row["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if row["updated_at"] else ""
        print(
            f"{row['id']} | {row['telegram_id']} | {row['account_type']} | "
            f"@{username if username else '-'} | {name or '-'} | {updated_at}"
        )


async def cmd_set_user_type(pool: asyncpg.Pool, opts: dict[str, Any]) -> None:
    account_type = normalize_account_type_console(opts.get("type"))
    telegram_id = opts.get("telegram_id")
    internal_id = opts.get("id")

    if not telegram_id and not internal_id:
        raise ValueError("укажи --telegram-id 123 или --id 1")

    if telegram_id and internal_id:
        raise ValueError("укажи что-то одно: --telegram-id или --id")

    if telegram_id:
        sql = """
            UPDATE users
            SET account_type = $2,
                updated_at = NOW()
            WHERE telegram_id = $1
            RETURNING id, telegram_id, username, first_name, last_name, account_type;
        """
        params = [int(telegram_id), account_type]
    else:
        sql = """
            UPDATE users
            SET account_type = $2,
                updated_at = NOW()
            WHERE id = $1
            RETURNING id, telegram_id, username, first_name, last_name, account_type;
        """
        params = [int(internal_id), account_type]

    async with pool.acquire() as con:
        row = await con.fetchrow(sql, *params)

    if not row:
        print("Пользователь не найден. Он должен хотя бы раз написать боту, чтобы попасть в users.")
        return

    username = row["username"] or "-"
    name = " ".join(part for part in [row["first_name"], row["last_name"]] if part) or "-"
    print("\nГотово.")
    print(f"id: {row['id']}")
    print(f"telegram_id: {row['telegram_id']}")
    print(f"username: @{username}")
    print(f"name: {name}")
    print(f"account_type: {row['account_type']}")



async def execute_command(pool: asyncpg.Pool, line: str) -> bool:
    lowered = line.lower().strip()
    if lowered in {"/ex", "/exit", "exit", "quit", "q"}:
        print("Выход.")
        return False

    if lowered in {"/help", "help", "?"}:
        print(HELP_TEXT)
        return True

    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        print(f"Ошибка разбора команды: {exc}")
        return True

    if not tokens:
        return True

    command = ALIASES.get(tokens[0], tokens[0])
    rest = tokens[1:]

    try:
        if command == "list":
            await cmd_list(pool, parse_flags(rest))
        elif command == "stats":
            await cmd_stats(pool, parse_flags(rest))
        elif command == "values":
            await cmd_values(pool, parse_flags(rest))
        elif command == "show":
            await cmd_show(pool, rest)
        elif command == "delete-category":
            await cmd_delete_category(pool, parse_flags(rest))
        elif command == "fix-done-without-markup":
            await cmd_fix_done_without_markup(pool, parse_flags(rest))
        elif command == "reports":
            await cmd_reports(parse_flags(rest))
        elif command == "count-new":
            await cmd_count_new(pool, parse_flags(rest))
        elif command == "errors":
            await cmd_errors(pool, parse_flags(rest))
        elif command == "users":
            await cmd_users(pool, parse_flags(rest))
        elif command == "set-user-type":
            await cmd_set_user_type(pool, parse_flags(rest))
        else:
            print("Неизвестная команда. Напиши /help")
    except ValueError as exc:
        print(f"Ошибка команды: {exc}")
    except asyncpg.PostgresError as exc:
        print(f"Ошибка PostgreSQL: {exc}")
    except Exception as exc:
        print(f"Ошибка: {exc}")

    return True


async def main() -> None:
    print(HELP_TEXT)
    print("\nПодключаюсь к БД...")

    pool = await create_pool()
    try:
        await ensure_user_account_type_schema(pool)
        print("Готово. Вводи команду. Для выхода: /ex\n")
        while True:
            try:
                line = input("db> ").strip()
            except KeyboardInterrupt:
                print("\nДля выхода напиши /ex")
                continue
            except EOFError:
                print()
                break

            if not line:
                continue

            keep_running = await execute_command(pool, line)
            if not keep_running:
                break
            print()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
