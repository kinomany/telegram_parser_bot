import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from typing import Any

from dotenv import load_dotenv

load_dotenv()

import asyncpg
from google import genai
from google.genai import types
from telethon import TelegramClient, functions
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.tl.types import Channel, User

from config import (
    API_HASH,
    API_ID,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    CHANNELS_TXT_PATH,
    CHANNEL_PROCESSOR_SESSION_NAME,
    GEMINI_API_KEY,
    GEMINI_KEYWORD_MODEL,
    GEMINI_KEYWORD_MODELS,
    AI_MAX_MESSAGES_TO_SCAN,
    AI_SHORT_MESSAGE_MIN_CHARS,
    AI_SHORT_MESSAGE_MAX_CHARS,
    AI_SHORT_MESSAGE_BUCKETS,
    AI_MIN_SHORT_MESSAGES_DESIRED,
    AI_LONG_MESSAGE_MIN_CHARS,
    AI_LONG_MESSAGE_MAX_CHARS,
    AI_LONG_MESSAGE_BUCKETS,
    AI_MIN_LONG_MESSAGES,
    AI_TOTAL_MESSAGES_MAX_CHARS,
    AI_BUCKET_NEIGHBOR_RADIUS,
    PROCESSING_STALE_MINUTES,
    CHANNELS_TO_PROCESS_PER_RUN,
    CHANNELS_TO_ADD_FROM_TXT_PER_RUN,
    CHANNEL_PROCESS_DELAY_SECONDS,
    CHANNEL_PROCESS_DELAY_JITTER_SECONDS,
    FLOOD_WAIT_SLEEP_CAP_SECONDS,
)

TELETHON_SESSION_NAME = CHANNEL_PROCESSOR_SESSION_NAME

                           
                         
                           

TELEGRAM_INNER_CALL_DELAY_SECONDS = 2.0
TELEGRAM_INNER_CALL_DELAY_JITTER_SECONDS = 3.0

                                                                                           
TELEGRAM_HISTORY_WAIT_SECONDS = 2

                                                          
TELEGRAM_MESSAGE_PROGRESS_PAUSE_EVERY = 20


                           
                       
                           
RUN_REPORTS_DIR = Path("run_reports")

PROGRAM_EXIT_INFO: dict[str, str] = {
    "status": "success",
    "title": "Программа выполнилась",
    "details": "Скрипт завершился без необработанных ошибок.",
}

RUN_STATS: dict[str, int] = {
    "channels_taken": 0,
    "channels_marked_success": 0,
    "channels_not_a_channel": 0,
    "channels_not_enough_data": 0,
    "channels_ai_error": 0,
    "channels_telegram_error": 0,
    "channels_unexpected_error": 0,
    "floodwait_stops": 0,
}


def reset_run_stats() -> None:
    for key in RUN_STATS:
        RUN_STATS[key] = 0


def build_run_summary_details(
    started_at: datetime,
    finished_at: datetime,
    total_seconds: float,
    process_limit: int,
    add_limit: int,
    extra_details: str = "",
) -> str:
    lines: list[str] = []

    lines.append(f"Время запуска: {started_at.strftime('%d.%m.%Y %H:%M:%S')}")
    lines.append(f"Время завершения: {finished_at.strftime('%d.%m.%Y %H:%M:%S')}")
    lines.append(f"Общее время выполнения: {format_duration(total_seconds)}")
    lines.append("")
    lines.append(f"Лимит обработки за запуск: {process_limit}")
    lines.append(f"Лимит добавления новых из txt: {add_limit}")
    lines.append("")
    lines.append(f"Каналов взял из очереди: {RUN_STATS['channels_taken']}")
    lines.append(f"Каналов без ошибок размечено: {RUN_STATS['channels_marked_success']}")
    lines.append(f"Не канал / username пользователя или бота: {RUN_STATS['channels_not_a_channel']}")
    lines.append(f"Недостаточно данных для разметки: {RUN_STATS['channels_not_enough_data']}")
    lines.append(f"Ошибок ИИ: {RUN_STATS['channels_ai_error']}")
    lines.append(f"Telegram-ошибок канала: {RUN_STATS['channels_telegram_error']}")
    lines.append(f"Неожиданных ошибок канала: {RUN_STATS['channels_unexpected_error']}")
    lines.append(f"Остановок из-за большого FloodWait: {RUN_STATS['floodwait_stops']}")

    if extra_details:
        lines.append("")
        lines.append("Дополнительно:")
        lines.append(extra_details)

    return "\n".join(lines)


def set_program_exit_info(status: str, title: str, details: str = "") -> None:
    PROGRAM_EXIT_INFO["status"] = status
    PROGRAM_EXIT_INFO["title"] = title
    PROGRAM_EXIT_INFO["details"] = details


def play_local_sound(is_error: bool = False) -> None:
    """Локальный звук. Telegram не трогаем."""
    try:
        if os.name == "nt":
            import winsound

            if is_error:
                winsound.MessageBeep(winsound.MB_ICONHAND)
                winsound.Beep(400, 700)
            else:
                winsound.MessageBeep(winsound.MB_OK)
                winsound.Beep(900, 250)
        else:
            print("\a", end="")
    except Exception as sound_error:
        print(f"Не удалось проиграть звук: {sound_error}")


def open_local_file(path: Path) -> None:
    """Открывает txt-отчёт стандартной программой ОС."""
    try:
        if os.name == "nt":
            os.startfile(str(path))           
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])         
        else:
            subprocess.Popen(["xdg-open", str(path)])         
    except Exception as open_error:
        print(f"Не удалось открыть файл отчёта: {open_error}")
        print(f"Файл лежит тут: {path.resolve()}")


def notify_program_result(
    status: str,
    title: str,
    details: str = "",
    error: BaseException | None = None,
) -> Path:
    """
    Создаёт локальный txt-отчёт, открывает его и проигрывает звук.
    status: success / error / stopped / floodwait
    """
    RUN_REPORTS_DIR.mkdir(exist_ok=True)

    now = datetime.now()
    filename_time = now.strftime("%Y-%m-%d_%H-%M-%S")
    report_path = RUN_REPORTS_DIR / f"{filename_time}_{status}.txt"

    lines: list[str] = []
    lines.append(title)
    lines.append("=" * 60)
    lines.append(f"Статус: {status}")
    lines.append(f"Время: {now.strftime('%d.%m.%Y %H:%M:%S')}")
    lines.append("")

    if details:
        lines.append("Детали:")
        lines.append(details)
        lines.append("")

    if error is not None:
        lines.append("Ошибка:")
        lines.append(f"{type(error).__name__}: {error}")
        lines.append("")
        lines.append("Traceback:")
        lines.append("".join(traceback.format_exception(type(error), error, error.__traceback__)))

    report_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nЛокальный отчёт создан: {report_path.resolve()}")

    play_local_sound(is_error=(status != "success"))
    open_local_file(report_path)

    return report_path


class NotTelegramChannelError(ValueError):
    """В channels.txt попал username пользователя/бота или другого объекта, а не канала."""


from tax_tree import (
    СИСТЕМНЫЙ_ПРОМТ_РАЗМЕТКИ_КАНАЛА,
    получить_дерево_для_промта,
    проверить_и_очистить_разметку_канала,
)

if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY не найден в .env")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)


                           
                            
                           


AI_SYSTEM_PROMPT = СИСТЕМНЫЙ_ПРОМТ_РАЗМЕТКИ_КАНАЛА


BAD_AI_KEYWORDS = {
    "канал",
    "пост",
    "посты",
    "информация",
    "контент",
    "новости канала",
    "telegram",
    "телеграм",
    "тг",
    "сми",
    "медиа",
    "журналистика",
    "журналист",
    "журналисты",
    "блог",
    "журнал",
    "онлайн",
    "live",
    "лайв",
    "эфир",
    "вещание",
    "живое вещание",
    "официальный канал",
    "приложение",
    "ios",
    "android",
    "app store",
    "google play",
    "сайт",
    "ссылка",
    "ссылки",
    "бот",
    "обратная связь",
    "донат",
    "реклама",
    "промокод",
    "скидка",
    "акция",
    "события",
}


AD_POST_RE = re.compile(
    r"(?iu)"
    r"(#\s*реклама\b|#\s*ad\b|"
    r"на\s+правах\s+рекламы|"
    r"рекламн(?:ый|ая|ое|ые)\s+материал|"
    r"партн[её]рск(?:ий|ая|ое|ие)\s+материал|"
    r"спонсорск(?:ий|ая|ое|ие)\s+материал|"
    r"промокод|"
    r"скидк[аи]|"
    r"розыгрыш|"
    r"купить\s+со\s+скидкой)"
)

TRASH_LINE_RE = re.compile(
    r"(?iu)"
    r"(подпишись|подписывайтесь|подписаться|"
    r"скачать|установить|app\s*store|google\s*play|ios|android|"
    r"приложени[ея]|"
    r"бот\b|написать\s+нам|обратная\s+связь|"
    r"наш\s+сайт|сайт\s*:|"
    r"донат|поддержать\s+нас|"
    r"зеркало|vpn|"
    r"реклама|рекламодатель|"
    r"промокод|скидка|акция|розыгрыш)"
)

FORMAT_NOISE_RE = re.compile(
    r"(?iu)\b("
    r"официальный\s+канал|"
    r"live|лайв|онлайн"
    r")\b"
)

STOPWORDS = {
    "это", "как", "что", "или", "для", "при", "над", "под", "без", "все", "всё",
    "его", "её", "она", "они", "оно", "мы", "вы", "нам", "вам", "нас", "вас",
    "там", "тут", "где", "когда", "уже", "ещё", "еще", "был", "была", "были",
    "будет", "будут", "может", "можно", "нужно", "очень", "также", "тоже",
    "после", "перед", "около", "среди", "через", "сейчас", "сегодня", "вчера",
    "который", "которая", "которые", "которых", "которого", "которой",
    "этот", "эта", "эти", "этих", "этого", "этой", "один", "одна", "много",
    "человек", "люди", "года", "году", "лет", "день", "раз", "время",
    "канал", "пост", "посты", "новость", "новости", "сообщает", "сообщили",
    "рассказал", "рассказала", "заявил", "заявила", "пишет", "говорит",
    "ссылка", "сайт", "бот", "подписка", "реклама", "приложение",
}

SINGLE_POST_TOPIC_RISK_WORDS = {
    "литература", "книги", "книга", "психология", "дети", "подростки",
    "музыка", "кино", "сериал", "игра", "атака", "беспилотники",
}


def normalize_word_for_topic(word: str) -> str | None:
    word = word.lower().replace("ё", "е")
    word = re.sub(r"[^a-zа-я0-9-]", "", word)

    if len(word) < 4:
        return None

    if word in STOPWORDS:
        return None

    if word.isdigit():
        return None

    for ending in (
        "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
        "ая", "ое", "ые", "ий", "ый", "ой", "ых", "их", "ам", "ям",
        "ах", "ях", "ов", "ев", "ия", "ие", "ии", "ей", "ую", "юю",
    ):
        if len(word) > 7 and word.endswith(ending):
            word = word[: -len(ending)]
            break

    if len(word) < 4 or word in STOPWORDS:
        return None

    return word


def extract_topic_words(text: str) -> set[str]:
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9-]{4,}", text)
    result = set()

    for word in words:
        normalized = normalize_word_for_topic(word)
        if normalized:
            result.add(normalized)

    return result


def build_common_topic_terms(cleaned_messages: list[str], limit: int = 18) -> list[str]:
    """
    Ищем слова, которые встречаются минимум в двух разных постах.
    Это дешевый локальный анти-бред: единичные темы не должны становиться keywords.
    """
    document_frequency: dict[str, int] = {}

    for message in cleaned_messages:
        for word in extract_topic_words(message):
            document_frequency[word] = document_frequency.get(word, 0) + 1

    common = [
        (word, count)
        for word, count in document_frequency.items()
        if count >= 2 and word not in BAD_AI_KEYWORDS
    ]

    common.sort(key=lambda item: (item[1], len(item[0])), reverse=True)
    return [word for word, _count in common[:limit]]


TG_USERNAME_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/(?:s/)?(?P<link_username>[A-Za-z0-9_]{5,32})(?=$|[/?#\s|;:,.!()\[\]{}\"'«»—-])"
    r"|(?<![A-Za-z0-9_])@(?P<at_username>[A-Za-z0-9_]{5,32})(?![A-Za-z0-9_])",
    flags=re.IGNORECASE,
)

STANDALONE_USERNAME_RE = re.compile(
    r"^@?(?P<username>[A-Za-z0-9_]{5,32})(?:\s*(?:#|//|\||;|—|-)\s*.*)?$",
    flags=re.IGNORECASE,
)

FULL_LINE_COMMENT_PREFIXES = ("#", "//", ";")


def normalize_username(raw: str) -> str | None:
    """
    Достаёт username канала из строки channels.txt.

    Поддерживает заметки:
    - https://t.me/maximkatz # авторский канал
    - https://t.me/s/politica_media | редакционный канал
    - @meduzalive — СМИ
    - maximkatz # заметка

    Важно: если в строке есть t.me/@username, берём только канал,
    остальной текст считаем заметкой и не отправляем в Telegram.
    """
    line = (raw or "").strip()

    if not line:
        return None

    if line.startswith(FULL_LINE_COMMENT_PREFIXES):
        return None

                                                                            
    match = TG_USERNAME_RE.search(line)
    if match:
        username = match.group("link_username") or match.group("at_username")
        return username.strip() if username else None

                                                                                 
                                                                           
    match = STANDALONE_USERNAME_RE.match(line)
    if match:
        return match.group("username").strip()

    return None


def read_channels_from_txt(path: str = CHANNELS_TXT_PATH) -> list[tuple[str, str]]:
    file_path = Path(path)

    if not file_path.exists():
        print(f"Файл {path} не найден. Создай его и добавь ссылки на каналы.")
        return []

    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    skipped_without_channel = 0

    for line_number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
        source_line = line.strip()

        if not source_line:
            continue

        if source_line.startswith(FULL_LINE_COMMENT_PREFIXES):
            continue

        username = normalize_username(source_line)

        if not username:
            skipped_without_channel += 1
            continue

        username_lower = username.lower()

        if username_lower in seen:
            continue

        seen.add(username_lower)
        result.append((username, source_line))

    if skipped_without_channel:
        print(f"Строк без Telegram-канала пропущено: {skipped_without_channel}.")

    return result


def remove_links_completely(text: str) -> str:
    """Удаляет ссылки, включая YouTube/youtu.be, даже если ссылка без https://."""
    if not text:
        return ""

                                                                       
    text = re.sub(
        r"\[([^\]]+)\]\((?:https?://|www\.|t\.me/|youtu\.be/|(?:m\.)?youtube\.com/|youtube-nocookie\.com/)[^\s)]+[^)]*\)",
        r"\1",
        text,
        flags=re.IGNORECASE,
    )

                                                                    
    url_pattern = re.compile(
        r"""(?ix)
        (?:https?://|www\.)[^\s<>()"'«»]+
        |\b(?:youtu\.be|(?:m\.)?youtube\.com|youtube-nocookie\.com)/[^\s<>()"'«»]+
        |\bt\.me/[^\s<>()"'«»]+
        |\bbit\.ly/[^\s<>()"'«»]+
        |\bgoo\.gl/[^\s<>()"'«»]+
        |\btinyurl\.com/[^\s<>()"'«»]+
        """
    )

    return url_pattern.sub(" ", text)

def clean_small_text(text: str | None) -> str:
    if not text:
        return ""

    text = str(text)
    text = remove_links_completely(text)

    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"#\w+", " ", text)

    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("`", "")
    text = text.replace("\xa0", " ")

    text = FORMAT_NOISE_RE.sub(" ", text)

    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if TRASH_LINE_RE.search(line):
            continue

        lines.append(line)

    text = " ".join(lines)
    text = re.sub(r"\s{2,}", " ", text)

    return text.strip()


def clean_message_for_ai(text: str | None) -> str | None:
    """
    Чистит сообщение для ИИ.
    Важно: здесь НЕ обрезаем текст до короткого лимита.
    Обрезка делается позже отдельно для коротких начал и длинных примеров.
    """
    if not text:
        return None

    text = str(text)

                                           
    if AD_POST_RE.search(text):
        return None

                                                                               
    links_count = len(re.findall(r"(https?://\S+|www\.\S+|t\.me/\S+)", text, flags=re.IGNORECASE))

    text = remove_links_completely(text)
    text = re.sub(r"@\w+", " ", text)
    text = re.sub(r"#\w+", " ", text)

    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("`", "")
    text = text.replace("\xa0", " ")

    cleaned_lines = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

                                        
        if TRASH_LINE_RE.search(line):
            continue

                                                   
        if len(line) < 20:
            continue

        cleaned_lines.append(line)

    text = " ".join(cleaned_lines)
    text = FORMAT_NOISE_RE.sub(" ", text)

                                                                                  
    text = re.sub(r"[^\w\sА-Яа-яЁё.,:;!?()«»\"'%-]", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()

                                                                                             
    if len(text) < AI_SHORT_MESSAGE_MIN_CHARS:
        return None

                                                                           
    if links_count >= 3 and len(text) < 350:
        return None

    return text


def cut_text(text: str, max_chars: int) -> str:
    """Просто отрезает конец, чтобы текст влезал в лимит."""
    text = re.sub(r"\s{2,}", " ", text).strip()

    if len(text) <= max_chars:
        return text

    return text[:max_chars].rstrip()


def score_message(text: str) -> int:
    """
    Скоринг нужен не для смысла уровня ИИ, а чтобы дешево выбрать 4 более полезных поста.
    Чем выше score, тем выше шанс, что пост отражает тематику канала.
    """
    score = 0
    length = len(text)

    if 250 <= length <= 900:
        score += 8
    elif 120 <= length < 250:
        score += 3
    elif 900 < length <= 1600:
        score += 5

    thematic_words = [
        "политика", "экономика", "война", "армия", "суд", "закон", "выборы",
        "правительство", "президент", "парламент", "санкции", "страна",
        "россия", "украина", "сша", "европа", "грузия", "китай", "израиль",
        "культура", "спорт", "наука", "технологии", "образование", "медицина",
        "происшествие", "расследование", "коррупция", "бизнес", "финансы",
    ]

    lower = text.lower()

    for word in thematic_words:
        if word in lower:
            score += 2

    if re.search(r"\b\d{1,4}\b", text):
        score += 2

    sentence_count = len(re.findall(r"[.!?]", text))
    if sentence_count >= 2:
        score += 2
    if sentence_count >= 5:
        score += 2

                                          
    if TRASH_LINE_RE.search(text):
        score -= 10

                                   
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    uppercase = re.findall(r"[A-ZА-ЯЁ]", text)
    if letters and len(uppercase) / len(letters) > 0.35:
        score -= 4

    return score


def select_typical_messages(raw_messages: list[str]) -> tuple[list[str], list[str], list[str]]:
    """
    Выбирает два набора сообщений для ИИ:

    1. Короткие начала 150–250 символов — чтобы ИИ увидела общий охват канала.
       0–10  -> до 6 сообщений
       10–30 -> до 8 сообщений
       30–80 -> до 10 сообщений

    2. Длинные примеры 400–600 символов — чтобы подтвердить тематику.
       0–5   -> до 1 сообщения
       5–15  -> до 1 сообщения
       15–30 -> до 2 сообщений
       30–50 -> до 2 сообщений
       50–80 -> до 2 сообщений

    Сообщения не повторяются: один и тот же индекс не попадёт и в короткие,
    и в длинные примеры. После очистки сообщение должно быть длиннее 150 символов.
    """
    cleaned_by_index: dict[int, str] = {}
    cleaned_messages: list[str] = []

    for index, message in enumerate(raw_messages):
        cleaned = clean_message_for_ai(message)

        if cleaned:
            cleaned_by_index[index] = cleaned
            cleaned_messages.append(cleaned)

    common_terms = build_common_topic_terms(cleaned_messages)
    common_terms_set = set(common_terms)

    selected_short: list[str] = []
    selected_long: list[str] = []
    selected_indexes: set[int] = set()
    selected_fingerprints: set[str] = set()
    total_chars = 0

    def get_fingerprint(text: str) -> str:
        words = sorted(extract_topic_words(text))[:14]
        return "|".join(words)

    def score_candidate(index: int, text: str, bucket_start: int, bucket_end: int, prefer_long: bool) -> int:
        words = extract_topic_words(text)
        overlap = words & common_terms_set

        score = len(overlap) * 10
        score += min(score_message(text), 12)

        if prefer_long:
            if len(text) >= 400:
                score += 8
            elif len(text) >= 250:
                score += 3
            else:
                score -= 4

        if index < bucket_start:
            score -= (bucket_start - index) * 2
        elif index >= bucket_end:
            score -= (index - bucket_end + 1) * 2

        return score

    def try_add_message(
        index: int,
        text: str,
        target: list[str],
        max_chars: int,
    ) -> bool:
        nonlocal total_chars

        if index in selected_indexes:
            return False

        if len(text) < AI_SHORT_MESSAGE_MIN_CHARS:
            return False

        fingerprint = get_fingerprint(text)
        if fingerprint and fingerprint in selected_fingerprints:
            return False

        prepared_text = cut_text(text, max_chars)

        if total_chars + len(prepared_text) > AI_TOTAL_MESSAGES_MAX_CHARS:
            remaining = AI_TOTAL_MESSAGES_MAX_CHARS - total_chars

            if remaining < AI_SHORT_MESSAGE_MIN_CHARS:
                return False

            prepared_text = cut_text(prepared_text, remaining)

        target.append(prepared_text)
        selected_indexes.add(index)

        if fingerprint:
            selected_fingerprints.add(fingerprint)

        total_chars += len(prepared_text)
        return True

    def select_from_buckets(
        buckets: list[tuple[int, int, int]],
        target: list[str],
        max_chars: int,
        group_name: str,
        prefer_long: bool,
    ) -> None:
        for bucket_start, bucket_end, needed_count in buckets:
            main_candidates = [
                (index, text)
                for index, text in cleaned_by_index.items()
                if bucket_start <= index < bucket_end
                and index not in selected_indexes
            ]

            if len(main_candidates) < needed_count:
                neighbor_start = max(0, bucket_start - AI_BUCKET_NEIGHBOR_RADIUS)
                neighbor_end = min(len(raw_messages), bucket_end + AI_BUCKET_NEIGHBOR_RADIUS)

                neighbor_candidates = [
                    (index, text)
                    for index, text in cleaned_by_index.items()
                    if neighbor_start <= index < neighbor_end
                    and index not in selected_indexes
                    and not (bucket_start <= index < bucket_end)
                ]
            else:
                neighbor_candidates = []

            candidates = main_candidates + neighbor_candidates

            if not candidates:
                print(
                    f"{group_name}. Блок сообщений {bucket_start}-{bucket_end}: "
                    f"подходящих сообщений после очистки нет. Перехожу дальше."
                )
                continue

            candidates.sort(
                key=lambda item: (
                    score_candidate(item[0], item[1], bucket_start, bucket_end, prefer_long),
                    len(item[1]),
                ),
                reverse=True,
            )

            added_in_bucket = 0
            for index, text in candidates:
                if added_in_bucket >= needed_count:
                    break

                if try_add_message(index, text, target, max_chars):
                    added_in_bucket += 1

            print(
                f"{group_name}. Блок сообщений {bucket_start}-{bucket_end}: "
                f"выбрано {added_in_bucket}/{needed_count}."
            )

    select_from_buckets(
        buckets=AI_LONG_MESSAGE_BUCKETS,
        target=selected_long,
        max_chars=AI_LONG_MESSAGE_MAX_CHARS,
        group_name="Длинные примеры",
        prefer_long=True,
    )

    select_from_buckets(
        buckets=AI_SHORT_MESSAGE_BUCKETS,
        target=selected_short,
        max_chars=AI_SHORT_MESSAGE_MAX_CHARS,
        group_name="Короткие начала",
        prefer_long=False,
    )

    return selected_short, selected_long, common_terms


def normalize_keywords(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    result: list[str] = []
    seen: set[str] = set()

    for item in value:
        if not isinstance(item, str):
            continue

        keyword = item.strip()
        keyword = re.sub(r"\s{2,}", " ", keyword)
        keyword = keyword.strip(" .,:;!?-—\"'«»")

        if not keyword:
            continue

        if len(keyword) > 40:
            continue

        lower = keyword.lower().replace("ё", "е")

        if lower in BAD_AI_KEYWORDS:
            continue

        if any(bad in lower for bad in ["ios", "android", "приложени", "ссылка", "сайт", "бот"]):
            continue

        if lower in seen:
            continue

        seen.add(lower)
        result.append(keyword)

    return result[:8]


def parse_ai_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()

    if not text:
        return None

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


def keyword_supported_by_context(keyword: str, context_text: str, common_terms: list[str]) -> bool:
    """
    Грубая проверка против галлюцинаций: keyword должен хотя бы чем-то
    подтверждаться title/description/постами/common_terms.
    Это не идеальная лемматизация, но лучше, чем слепо верить ИИ.
    """
    lower_context = context_text.lower().replace("ё", "е")
    lower_keyword = keyword.lower().replace("ё", "е")

    if lower_keyword in lower_context:
        return True

    keyword_words = [w for w in extract_topic_words(keyword) if w not in BAD_AI_KEYWORDS]
    if not keyword_words:
        return False

    context_words = extract_topic_words(lower_context)
    common_terms_set = set(common_terms)

    for word in keyword_words:
        if word in context_words or word in common_terms_set:
            return True

    return False


def filter_supported_keywords(
    keywords: list[str],
    tg_title: str,
    tg_description: str,
    typical_messages: list[str],
    common_terms: list[str],
) -> list[str]:
    context_text = "\n".join([
        clean_small_text(tg_title),
        clean_small_text(tg_description),
        " ".join(common_terms),
        "\n".join(typical_messages),
    ])

    filtered = [
        keyword
        for keyword in keywords
        if keyword_supported_by_context(keyword, context_text, common_terms)
    ]

    return filtered or keywords



def is_unclear_value(value: Any) -> bool:
    if value is None:
        return True

    text = str(value).strip().lower().replace("ё", "е")
    return text in {"", "none", "null", "неясно", "не указано", "не указана"}


def normalize_infer_text(*parts: Any) -> str:
    text = " ".join(str(part or "") for part in parts)
    text = text.lower().replace("ё", "е")
    text = re.sub(r"\s{2,}", " ", text)
    return text



EDITORIAL_MEDIA_MARKERS = (
    "редакция",
    "издание",
    "телеканал",
    "радио",
    "медиа",
    "сми",
    "информационное агентство",
    "агентство",
    "газета",
    "журнал",
    "новостной проект",
    "агрегатор новостей",
)

KNOWN_MEDIA_BRAND_MARKERS = (
    "медиазона",
    "mediazona",
    "mediazzzona",
    "можем объяснить",
    "mozhemobyasnit",
    "дождь",
    "tvrain",
    "медуза",
    "meduza",
    "meduzalive",
    "настоящее время",
    "currenttime",
    "новая газета",
    "novayagazeta",
)

KNOWN_OPPOSITION_MEDIA_MARKERS = (
    "медиазона",
    "mediazona",
    "mediazzzona",
    "можем объяснить",
    "mozhemobyasnit",
    "дождь",
    "tvrain",
    "медуза",
    "meduza",
    "meduzalive",
    "новая газета",
    "novayagazeta",
    "настоящее время",
    "currenttime",
)


def text_has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def fix_editorial_media_not_politics(
    classification: dict[str, Any],
    tg_title: str,
    tg_description: str,
) -> None:
    """
    Причина прошлой ошибки:
    ИИ видит политические/военные посты редакционного СМИ и ставит "политика"
    или "война и конфликты", хотя по сущности канала это СМИ.

    Это исправление смотрит на title/description/описание и поднимает редакции,
    издания, телеканалы, агентства и известные медиа-бренды в "новости и СМИ".
    """
    current_category = classification.get("категория")

    if current_category == "новости и СМИ":
        return

    text = normalize_infer_text(
        tg_title,
        tg_description,
        classification.get("описание"),
        " ".join(classification.get("темы") or []),
        " ".join(classification.get("объекты") or []),
        " ".join(classification.get("ключевые слова") or []),
    )

    has_media_marker = text_has_any(text, EDITORIAL_MEDIA_MARKERS) or text_has_any(text, KNOWN_MEDIA_BRAND_MARKERS)

    if not has_media_marker:
        return

    has_author_marker = text_has_any(text, AUTHOR_CHANNEL_MARKERS) or text_has_any(text, WAR_AUTHOR_MARKERS)
    has_strong_media_brand = text_has_any(text, EDITORIAL_MEDIA_MARKERS) or text_has_any(text, KNOWN_MEDIA_BRAND_MARKERS)

    if has_author_marker and not has_strong_media_brand:
        return

    old_category = current_category
    old_position = classification.get("позиция") or classification.get("позиция подачи")

    classification["категория"] = "новости и СМИ"

    if classification.get("формат") in {None, "", "неясно", "личный блог", "мнение"}:
        classification["формат"] = "новости"

    if is_unclear_value(classification.get("тип СМИ")):
        if "телеканал" in text or "дождь" in text or "tvrain" in text:
            classification["тип СМИ"] = "телеканал"
        elif "агрегатор" in text:
            classification["тип СМИ"] = "агрегатор новостей"
        elif "официальн" in text or "государствен" in text:
            classification["тип СМИ"] = "официальное СМИ"
        elif "независим" in text or text_has_any(text, KNOWN_OPPOSITION_MEDIA_MARKERS):
            classification["тип СМИ"] = "независимое СМИ"
        else:
            classification["тип СМИ"] = "онлайн-издание"

    news_topics = classification.get("новостная тема")
    if not isinstance(news_topics, list):
        news_topics = []

    if old_category in {"политика", "война и конфликты", "общество", "экономика", "происшествия"}:
        topic = old_category
        if topic not in news_topics:
            news_topics.append(topic)

    if any(marker in text for marker in ("войн", "сво", "фронт", "украин")) and "война и конфликты" not in news_topics:
        news_topics.append("война и конфликты")

    if any(marker in text for marker in ("полит", "власть", "оппози", "репресс")) and "политика" not in news_topics:
        news_topics.append("политика")

    classification["новостная тема"] = news_topics[:3]

    if is_unclear_value(old_position):
        if text_has_any(text, KNOWN_OPPOSITION_MEDIA_MARKERS) or any(
            marker in text for marker in ("независим", "иноагент", "репресс", "цензур", "против войны", "антивоен")
        ):
            classification["позиция"] = "оппозиционная"
        elif any(marker in text for marker in ("государствен", "официальн", "провласт", "кремл", "пророссий")):
            classification["позиция"] = "пророссийская"
        else:
            classification["позиция"] = "нейтральная"
    else:
        classification["позиция"] = old_position

    classification["позиция подачи"] = None


def infer_media_type_from_context(
    classification: dict[str, Any],
    tg_title: str,
    tg_description: str,
) -> None:
    """
    Локальная страховка: если ИИ поняла "новости и СМИ", но забыла тип СМИ,
    пробуем восстановить его из title/description.
    """
    if classification.get("категория") != "новости и СМИ":
        return

    if not is_unclear_value(classification.get("тип СМИ")):
        return

    text = normalize_infer_text(tg_title, tg_description, classification.get("описание"))

    if any(marker in text for marker in ("телеканал", "тв канал", "tv", "дождь")):
        classification["тип СМИ"] = "телеканал"
    elif any(marker in text for marker in ("издание", "медиа", "редакция", "газета", "журнал")):
        classification["тип СМИ"] = "онлайн-издание"
    elif any(marker in text for marker in ("агрегатор", "собираем новости", "главные новости")):
        classification["тип СМИ"] = "агрегатор новостей"
    elif any(marker in text for marker in ("авторский канал", "личный канал", "блог", "историк", "аналитик", "политик", "военкор", "сво", "фронт", "пропаганда")):
        classification["тип СМИ"] = "неясно"
    else:
        classification["тип СМИ"] = "независимое СМИ"


def infer_media_position_from_context(
    classification: dict[str, Any],
    tg_title: str,
    tg_description: str,
    typical_messages: list[str],
) -> None:
    """
    Локальная страховка для СМИ: позиция должна быть заполнена.
    Главную работу делает ИИ и tax_tree, а это добивает явно очевидные случаи.
    """
    if classification.get("категория") != "новости и СМИ":
        return

    if not is_unclear_value(classification.get("позиция")):
        return

    text = normalize_infer_text(
        tg_title,
        tg_description,
        classification.get("описание"),
        " ".join(typical_messages[:8]),
    )

    opposition_markers = (
        "оппозицион",
        "независим",
        "иноагент",
        "репресс",
        "цензур",
        "политзаключ",
        "против войны",
        "антивоен",
        "анти-воен",
    )
    official_markers = (
        "официальн",
        "государствен",
        "провласт",
        "кремл",
        "пророссий",
        "за россию",
    )
    pro_ukraine_markers = (
        "проукраин",
        "поддержк укра",
        "украинск",
    )

    if text_has_any(text, KNOWN_OPPOSITION_MEDIA_MARKERS) or any(marker in text for marker in opposition_markers):
        classification["позиция"] = "оппозиционная"
    elif any(marker in text for marker in pro_ukraine_markers):
        classification["позиция"] = "проукраинская"
    elif any(marker in text for marker in official_markers):
        classification["позиция"] = "пророссийская"
    else:
        classification["позиция"] = "нейтральная"



WAR_AUTHOR_MARKERS = (
    "военкор",
    "военный корреспондент",
    "военная пропаганда",
    "пропаганда",
    "z-канал",
    "z канал",
    "сво",
    "фронт",
    "сводк",
)

AUTHOR_CHANNEL_MARKERS = (
    "авторский канал",
    "личный канал",
    "персональный канал",
    "блог",
    "блогер",
    "историк",
    "аналитик",
    "политический аналитик",
    "политик",
    "депутат",
    "журналист",
    "эксперт",
)

MEDIA_BRAND_MARKERS = (
    "редакция",
    "издание",
    "телеканал",
    "медиа",
    "информационное агентство",
    "агентство",
    "газета",
    "журнал",
)


def fix_author_channel_not_media(
    classification: dict[str, Any],
    tg_title: str,
    tg_description: str,
) -> None:
    """
    Страховка против частой ошибки:
    текущие события -> ИИ ставит "новости и СМИ".

    Правильная иерархия:
    - редакция/издание/телеканал/агентство/медиа-проект/агрегатор -> новости и СМИ
    - военкор/военная пропаганда/СВО/фронт -> война и конфликты
    - один автор/политик/историк/аналитик/эксперт/блогер -> политика
    """
    if classification.get("категория") != "новости и СМИ":
        return

    text = normalize_infer_text(
        tg_title,
        tg_description,
        classification.get("описание"),
        " ".join(classification.get("темы") or []),
        " ".join(classification.get("объекты") or []),
        " ".join(classification.get("ключевые слова") or []),
    )

    has_media_brand_marker = any(marker in text for marker in MEDIA_BRAND_MARKERS)
    if has_media_brand_marker:
        return

    has_war_author_marker = any(marker in text for marker in WAR_AUTHOR_MARKERS)
    has_author_marker = any(marker in text for marker in AUTHOR_CHANNEL_MARKERS)

    if not has_war_author_marker and not has_author_marker:
        return

    old_position = classification.get("позиция") or classification.get("позиция подачи")

    if has_war_author_marker:
        classification["категория"] = "война и конфликты"
        classification["формат"] = "мнение"
        classification["конфликт"] = "Россия и Украина"

        if not classification.get("военная тема"):
            classification["военная тема"] = ["военная аналитика", "фронт"]

        if is_unclear_value(old_position):
            classification["позиция подачи"] = "пророссийская"
        elif str(old_position).strip() in {"оппозиционная", "независимая"}:
            classification["позиция подачи"] = "нейтральная"
        else:
            classification["позиция подачи"] = old_position

        classification["позиция"] = None

    else:
        classification["категория"] = "политика"
        classification["формат"] = "мнение"

        if is_unclear_value(old_position):
            classification["позиция"] = "оппозиционная" if any(
                marker in text for marker in ("критическ", "оппозицион", "репресс", "против войны", "антивоен")
            ) else "нейтральная"
        else:
            classification["позиция"] = old_position

        if not classification.get("политическая тема"):
            classification["политическая тема"] = ["внутренняя политика"]

        classification["позиция подачи"] = None

    classification["тип СМИ"] = None
    classification["новостная тема"] = []

    topics = classification.get("темы")
    if isinstance(topics, list):
        if has_war_author_marker and "военкор / военная пропаганда" not in topics:
            topics.append("военкор / военная пропаганда")
        elif has_author_marker and "авторский канал" not in topics:
            topics.append("авторский канал")



def fix_blog_channel_content_category(
    classification: dict[str, Any],
    tg_title: str,
    tg_description: str,
) -> None:
    """
    Если ИИ поставила "блоги", но по факту это предметный авторский канал,
    перекидываем в предметную категорию. Пример: TheBadComedian -> видео и фильмы.
    """
    if classification.get("категория") != "блоги":
        return

    text = normalize_infer_text(
        tg_title,
        tg_description,
        classification.get("описание"),
        " ".join(classification.get("темы") or []),
        " ".join(classification.get("объекты") or []),
        " ".join(classification.get("ключевые слова") or []),
    )

    if any(marker in text for marker in ("badcomedian", "кино", "фильм", "фильмы", "сериал", "youtube", "ютуб", "обзор фильм", "обзоры фильмов")):
        classification["категория"] = "видео и фильмы"
        classification["формат"] = "обзор"
        classification["видео тема"] = ["обзоры фильмов", "YouTube"]
        classification["тип блога"] = []

        topics = classification.get("темы")
        if isinstance(topics, list):
            for topic in ("кино", "обзоры фильмов", "YouTube"):
                if topic not in topics:
                    topics.append(topic)


def remove_irrelevant_warnings(
    classification: dict[str, Any],
    warnings: list[str],
) -> list[str]:
    """
    После локальных исправлений часть warning может стать неактуальной.
    Например ИИ дала неверный "тип блога", но мы перекинули канал в "видео и фильмы".
    """
    if not warnings:
        return []

    result = []

    for warning in warnings:
        if "тип блога" in warning and classification.get("категория") != "блоги":
            continue

        if "тип СМИ" in warning and classification.get("категория") != "новости и СМИ":
            continue

        if "политическая тема" in warning and classification.get("категория") != "политика":
            continue

        if "позиция подачи" in warning and classification.get("категория") != "война и конфликты":
            continue

        if "военная тема" in warning and classification.get("категория") != "война и конфликты":
            continue

        result.append(warning)

    return result


def build_keywords_from_classification(classification: dict[str, Any]) -> list[str]:
    """
    Если ИИ не дала нормальные "ключевые слова", собираем запасные ключи
    из структурной разметки: категория, регион, темы, объекты и уточняющие поля.
    """
    if not isinstance(classification, dict):
        return []

    skip_values = {
        "",
        "неясно",
        "другое",
        "смешанная",
        "смешанное",
        "смешанный формат",
        "смешанный фокус",
        "смешанный временной фокус",
    }

    candidates: list[str] = []

    important_fields = [
        "категория",
        "дополнительные категории",
        "регион",
        "страна",
        "город",
        "формат",
        "тип СМИ",
        "новостная тема",
        "тип охвата",
        "позиция",
        "политическая тема",
        "конфликт",
        "военная тема",
        "позиция подачи",
        "область экономики",
        "область технологий",
        "область науки",
        "социальная тема",
        "область культуры",
        "вид спорта",
        "тип происшествия",
        "тема здоровья",
        "область образования",
        "область развлечений",
        "местная тема",
        "темы",
        "объекты",
    ]

    for field in important_fields:
        value = classification.get(field)

        if isinstance(value, list):
            values = value
        else:
            values = [value]

        for item in values:
            if not isinstance(item, str):
                continue

            item = item.strip()
            if not item or item.lower().replace("ё", "е") in skip_values:
                continue

            candidates.append(item)

    return normalize_keywords(candidates)


def build_ai_user_prompt(
    username: str,
    tg_title: str,
    tg_description: str,
    short_messages: list[str],
    long_messages: list[str],
    common_terms: list[str],
) -> str:
    title = clean_small_text(tg_title)
    description = clean_small_text(tg_description)
    tax_tree_text = получить_дерево_для_промта()

    short_text = ""
    if short_messages:
        for index, message in enumerate(short_messages, start=1):
            short_text += f"\nКороткое начало {index}:\n{message}\n"
    else:
        short_text = "\nПодходящих коротких начал сообщений нет.\n"

    long_text = ""
    if long_messages:
        for index, message in enumerate(long_messages, start=1):
            long_text += f"\nДлинный пример {index}:\n{message}\n"
    else:
        long_text = "\nПодходящих длинных примеров нет.\n"

    common_terms_text = ", ".join(common_terms) if common_terms else "повторяющихся тем не найдено"

    return f"""
Разметь Telegram-канал по дереву вопросов.

Дерево вопросов и схема ответа:
{tax_tree_text}

Данные канала:
username: {username}
title: {title}
description: {description}

Повторяющиеся слова/темы по последним постам:
{common_terms_text}

Короткие начала сообщений 150–250 символов.
Они нужны, чтобы понять общий охват канала, а не случайный инфоповод:
{short_text}

Длинные примеры сообщений 400–600 символов.
Они нужны только как подтверждение, не делай главную категорию по нескольким похожим длинным примерам, если короткие начала показывают широкий канал:
{long_text}

Правила для широких новостных каналов:
Если канал пишет о разных темах, не выбирай узкую категорию только по одному свежему инфоповоду.
Если это редакция, издание, телеканал, агентство, новостной проект или медиа, выбирай категорию "новости и СМИ".
Если это военкор, военная пропаганда, Z-канал, канал со сводками СВО/фронта — выбирай "война и конфликты". Для пророссийских военкоров и пропагандистских каналов ставь "позиция подачи": "пророссийская".
Если это один автор, историк, политический аналитик, политик, блогер или эксперт и он устойчиво пишет о российской политике/власти/оппозиции — выбирай "политика".
Если авторский канал не политический, выбирай предметную категорию по содержанию: кино/YouTube-обзоры -> "видео и фильмы", игры -> "игры", технологии -> "технологии", книги -> "книги", еда -> "еда и кулинария" и т.д.
Авторский канал с критическим взглядом на российскую политику размечай как "политика" с позицией "оппозиционная", а не как СМИ.
Для СМИ обязательно заполняй поле "тип СМИ".
Для политических, общественных и военных СМИ обязательно заполняй поле "позиция".
Если СМИ критично к российской власти, репрессиям, цензуре или войне — ставь позицию "оппозиционная" или "независимая".
Если СМИ государственное, провластное, кремлёвское или поддерживает официальную линию РФ — ставь "официальная", "провластная" или "пророссийская".
Если СМИ освещает войну России и Украины, позиция не должна оставаться пустой: выбери "пророссийская", "проукраинская", "оппозиционная", "нейтральная" или "смешанная".
Для широких каналов ставь "тип охвата": "широкий новостной канал" или "агрегатор новостей", если такое поле есть в дереве.
Категория "война и конфликты" должна быть главной только если канал устойчиво посвящён войне, фронту, сводкам или конфликтам.
Если война — одна из тем широкого новостного СМИ, отражай это в "новостная тема", "темы" и "ключевые слова", но не делай категорию "война и конфликты" главной без устойчивых оснований.
Ключевые слова должны описывать канал целиком: страна/регион, главная категория, тип СМИ, позиция, формат и регулярные темы. Не делай их только из отдельных инфоповодов.
Объекты должны быть устойчивыми. Не добавляй случайных героев одного сообщения.
Уверенность выше 0.9 ставь только для узкотематических каналов.

Верни один JSON по схеме ответа из дерева.
Не добавляй markdown.
"""


async def create_db_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )


async def ensure_channels_table(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                source_url TEXT,
                tg_title TEXT,
                tg_description TEXT,
                ai_keywords JSONB,
                ai_description TEXT,
                ai_classification JSONB,
                status TEXT NOT NULL DEFAULT 'new',
                error TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMP
            );
            """
        )

                                                                                    
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS source_url TEXT;")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS tg_title TEXT;")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS tg_description TEXT;")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_keywords JSONB;")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_description TEXT;")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS ai_classification JSONB;")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new';")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS error TEXT;")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS created_at TIMESTAMP NOT NULL DEFAULT NOW();")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();")
        await conn.execute("ALTER TABLE channels ADD COLUMN IF NOT EXISTS processed_at TIMESTAMP;")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channels_status_id ON channels(status, id);")


async def ensure_channel_ai_markup_table(pool: asyncpg.Pool) -> None:
    """
    Новая отдельная таблица для ИИ-разметки каналов.
    channels хранит сам канал и статус обработки.
    channel_ai_markup хранит JSON-разметку по tax_tree.py.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_ai_markup (
                id BIGSERIAL PRIMARY KEY,
                channel_id BIGINT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                username TEXT NOT NULL,

                status TEXT NOT NULL DEFAULT 'done',
                error TEXT,
                model TEXT,
                is_current BOOLEAN NOT NULL DEFAULT TRUE,

                category TEXT,
                region TEXT,
                content_format TEXT,
                position_label TEXT,
                confidence NUMERIC(5, 4),

                ai_keywords JSONB NOT NULL DEFAULT '[]'::jsonb,
                ai_description TEXT,
                ai_classification JSONB NOT NULL DEFAULT '{}'::jsonb,
                ai_warnings JSONB NOT NULL DEFAULT '[]'::jsonb,

                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_channel_ai_markup_current
            ON channel_ai_markup (channel_id)
            WHERE is_current = TRUE;
            """
        )
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_channel_id ON channel_ai_markup (channel_id);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_username ON channel_ai_markup (username);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_category ON channel_ai_markup (category);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_region ON channel_ai_markup (region);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_content_format ON channel_ai_markup (content_format);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_position_label ON channel_ai_markup (position_label);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_ai_keywords ON channel_ai_markup USING GIN (ai_keywords);")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_channel_ai_markup_ai_classification ON channel_ai_markup USING GIN (ai_classification);")


async def insert_channels_from_txt(pool: asyncpg.Pool, limit: int | None = None) -> None:
    channels = read_channels_from_txt(CHANNELS_TXT_PATH)

    if not channels:
        print("В channels.txt нет подходящих публичных username.")
        return

    if limit is None:
        limit = CHANNELS_TO_ADD_FROM_TXT_PER_RUN

    limit = max(0, int(limit))

    if limit == 0:
        print(
            f"Каналов из txt найдено: {len(channels)}. "
            "Новых не добавляю: CHANNELS_TO_ADD_FROM_TXT_PER_RUN=0."
        )
        return

    async with pool.acquire() as conn:
        inserted = 0
        checked = 0

        for username, source_url in channels:
            checked += 1

            result = await conn.execute(
                """
                INSERT INTO channels (username, source_url)
                VALUES ($1, $2)
                ON CONFLICT (username) DO NOTHING;
                """,
                username,
                source_url,
            )

            if result == "INSERT 0 1":
                inserted += 1

                if inserted >= limit:
                    break

    print(
        f"Каналов из txt найдено: {len(channels)}. "
        f"Проверено строк txt: {checked}. "
        f"Новых добавлено в очередь: {inserted}/{limit}."
    )

async def reset_stale_processing_channels(pool: asyncpg.Pool) -> int:
    """
    Если программа/интернет упали после status='processing', канал не должен висеть вечно.
    Старые processing возвращаем в new.
    """
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE channels
            SET status = 'new',
                error = 'Сброшен зависший processing после перезапуска программы',
                updated_at = NOW()
            WHERE status = 'processing'
              AND updated_at < NOW() - ($1::int * INTERVAL '1 minute');
            """,
            PROCESSING_STALE_MINUTES,
        )

    return int(result.split()[-1])




def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours} ч {minutes} мин {secs} сек"

    if minutes:
        return f"{minutes} мин {secs} сек"

    return f"{secs} сек"


def get_channel_process_delay_seconds() -> float:
    """Пауза между каналами: базовая задержка + случайный jitter."""
    base_delay = max(0, CHANNEL_PROCESS_DELAY_SECONDS)
    jitter = max(0, CHANNEL_PROCESS_DELAY_JITTER_SECONDS)

    if jitter <= 0:
        return float(base_delay)

    return base_delay + random.uniform(0, jitter)


def get_telegram_inner_delay_seconds() -> float:
    """Маленькая пауза между Telegram-вызовами внутри одного канала."""
    base_delay = max(0.0, float(TELEGRAM_INNER_CALL_DELAY_SECONDS))
    jitter = max(0.0, float(TELEGRAM_INNER_CALL_DELAY_JITTER_SECONDS))

    if jitter <= 0:
        return base_delay

    return base_delay + random.uniform(0, jitter)


async def telegram_inner_pause(reason: str) -> None:
    """
    Небольшая случайная пауза внутри обработки канала.
    Это не лечит FloodWait полностью, но убирает резкие серии запросов к Telegram.
    """
    delay = get_telegram_inner_delay_seconds()

    if delay <= 0:
        return

    print(f"Малая Telegram-пауза ({reason}): {delay:.1f} сек.")
    await asyncio.sleep(delay)


async def get_next_new_channel(pool: asyncpg.Pool) -> asyncpg.Record | None:
    """
    Атомарно забираем один канал в обработку.
    Так две копии программы не схватят один и тот же канал.
    """
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            UPDATE channels
            SET status = 'processing',
                error = NULL,
                updated_at = NOW()
            WHERE id = (
                SELECT id
                FROM channels
                WHERE status = 'new'
                ORDER BY id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, username, source_url;
            """
        )


async def update_channel_status(
    pool: asyncpg.Pool,
    channel_id: int,
    status: str,
    error: str | None = None,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE channels
            SET status = $2,
                error = $3,
                updated_at = NOW()
            WHERE id = $1;
            """,
            channel_id,
            status,
            error,
        )


async def save_telegram_metadata(
    pool: asyncpg.Pool,
    channel_id: int,
    tg_title: str,
    tg_description: str,
) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE channels
            SET tg_title = $2,
                tg_description = $3,
                updated_at = NOW()
            WHERE id = $1;
            """,
            channel_id,
            tg_title,
            tg_description,
        )


async def save_ai_markup(
    pool: asyncpg.Pool,
    channel_id: int,
    username: str,
    ai_keywords: list[str],
    ai_description: str | None,
    ai_classification: dict[str, Any],
    ai_warnings: list[str] | None = None,
    model_used: str | None = None,
) -> None:
    """
    Сохраняет ИИ-разметку в новую таблицу channel_ai_markup.
    В channels обновляет только статус обработки.
    """
    ai_warnings = ai_warnings or []

    category = ai_classification.get("категория")
    region = ai_classification.get("регион")
    content_format = ai_classification.get("формат")
    position_label = ai_classification.get("позиция") or ai_classification.get("позиция подачи")

    confidence_raw = ai_classification.get("уверенность")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else None
    except (TypeError, ValueError):
        confidence = None

    async with pool.acquire() as conn:
        async with conn.transaction():
                                                            
            await conn.execute(
                """
                UPDATE channel_ai_markup
                SET is_current = FALSE,
                    updated_at = NOW()
                WHERE channel_id = $1
                  AND is_current = TRUE;
                """,
                channel_id,
            )

            await conn.execute(
                """
                INSERT INTO channel_ai_markup (
                    channel_id,
                    username,
                    status,
                    model,
                    category,
                    region,
                    content_format,
                    position_label,
                    confidence,
                    ai_keywords,
                    ai_description,
                    ai_classification,
                    ai_warnings,
                    is_current,
                    processed_at
                )
                VALUES (
                    $1,
                    $2,
                    'done',
                    $3,
                    $4,
                    $5,
                    $6,
                    $7,
                    $8,
                    $9::jsonb,
                    $10,
                    $11::jsonb,
                    $12::jsonb,
                    TRUE,
                    NOW()
                );
                """,
                channel_id,
                username,
                model_used,
                category,
                region,
                content_format,
                position_label,
                confidence,
                json.dumps(ai_keywords, ensure_ascii=False),
                ai_description,
                json.dumps(ai_classification, ensure_ascii=False),
                json.dumps(ai_warnings, ensure_ascii=False),
            )

            await conn.execute(
                """
                UPDATE channels
                SET status = 'done',
                    error = NULL,
                    processed_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1;
                """,
                channel_id,
            )


async def fetch_telegram_metadata_and_messages(
    tg_client: TelegramClient,
    username: str,
) -> tuple[str, str, list[str]]:
                                                        
    entity = await tg_client.get_entity(username)

                                                                              
                                                            
    if isinstance(entity, User):
        raise NotTelegramChannelError("Это username пользователя/бота, а не Telegram-канала")

    if not isinstance(entity, Channel):
        raise NotTelegramChannelError("Это не Telegram-канал")

                                                                                                      
    await telegram_inner_pause("после get_entity")

    title = getattr(entity, "title", "") or ""

                                                                      
    full = await tg_client(functions.channels.GetFullChannelRequest(entity))
    await telegram_inner_pause("после GetFullChannelRequest")

    description = getattr(full.full_chat, "about", "") or ""

    raw_messages: list[str] = []
    scanned_count = 0

                                                                                             
    async for message in tg_client.iter_messages(
        entity,
        limit=AI_MAX_MESSAGES_TO_SCAN,
        wait_time=TELEGRAM_HISTORY_WAIT_SECONDS,
    ):
        scanned_count += 1

        if not message:
            continue

        text = getattr(message, "message", None)

        if not text:
            continue

        raw_messages.append(text)

        if (
            TELEGRAM_MESSAGE_PROGRESS_PAUSE_EVERY > 0
            and scanned_count % TELEGRAM_MESSAGE_PROGRESS_PAUSE_EVERY == 0
            and scanned_count < AI_MAX_MESSAGES_TO_SCAN
        ):
            await telegram_inner_pause(f"после просмотра {scanned_count} сообщений")

    return title, description, raw_messages


                                           

async def ask_ai_for_channel_markup(
    username: str,
    tg_title: str,
    tg_description: str,
    short_messages: list[str],
    long_messages: list[str],
    common_terms: list[str],
) -> dict[str, Any]:
    user_prompt = build_ai_user_prompt(
        username=username,
        tg_title=tg_title,
        tg_description=tg_description,
        short_messages=short_messages,
        long_messages=long_messages,
        common_terms=common_terms,
    )

    last_error_text = "ошибки не было, но ответ не подошёл"

    for model_name in GEMINI_KEYWORD_MODELS:
        for attempt in range(4):
            try:
                response = await gemini_client.aio.models.generate_content(
                    model=model_name,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=AI_SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        temperature=0.2,
                    ),
                )

                text = (response.text or "").strip()
                data = parse_ai_json(text)

                if data is None:
                    last_error_text = f"{model_name}: Gemini вернул не JSON: {text[:300]}"
                    print(last_error_text)
                    break                            

                classification, warnings = проверить_и_очистить_разметку_канала(data)
                fix_editorial_media_not_politics(
                    classification=classification,
                    tg_title=tg_title,
                    tg_description=tg_description,
                )
                infer_media_type_from_context(
                    classification=classification,
                    tg_title=tg_title,
                    tg_description=tg_description,
                )
                infer_media_position_from_context(
                    classification=classification,
                    tg_title=tg_title,
                    tg_description=tg_description,
                    typical_messages=short_messages + long_messages,
                )
                fix_author_channel_not_media(
                    classification=classification,
                    tg_title=tg_title,
                    tg_description=tg_description,
                )
                fix_blog_channel_content_category(
                    classification=classification,
                    tg_title=tg_title,
                    tg_description=tg_description,
                )
                warnings = remove_irrelevant_warnings(classification, warnings)
                classification["модель"] = model_name

                keywords = normalize_keywords(classification.get("ключевые слова"))
                auto_keywords = build_keywords_from_classification(classification)

                                                                                
                                                                                                        
                keywords = normalize_keywords(keywords + auto_keywords)

                keywords = filter_supported_keywords(
                    keywords=keywords,
                    tg_title=tg_title,
                    tg_description=tg_description,
                    typical_messages=short_messages + long_messages,
                    common_terms=common_terms,
                )

                description = classification.get("описание")

                if isinstance(description, str):
                    description = description.strip()
                    description = clean_small_text(description)

                    if len(description) > 500:
                        description = description[:500].rsplit(" ", 1)[0]
                else:
                    description = None

                classification["ключевые слова"] = keywords
                classification["описание"] = description or ""

                if not keywords:
                    last_error_text = f"{model_name}: после разметки не получилось собрать ключевые слова."
                    print(last_error_text)
                    break                            

                return {
                    "ok": True,
                    "keywords": keywords,
                    "description": description,
                    "classification": classification,
                    "warnings": warnings,
                    "model_used": model_name,
                    "error": None,
                }

            except Exception as exc:
                error_text = str(exc)
                last_error_text = f"{model_name}: {type(exc).__name__}: {exc}"

                is_temporary_error = (
                    "503" in error_text
                    or "UNAVAILABLE" in error_text
                    or "429" in error_text
                    or "RESOURCE_EXHAUSTED" in error_text
                    or "500" in error_text
                    or "INTERNAL" in error_text
                )

                                                                                                   
                if not is_temporary_error:
                    print(f"Gemini ошибка модели {model_name}. Пробую следующую модель. Ошибка: {type(exc).__name__}: {exc}")
                    break

                delay = min(2 ** attempt, 20) + random.uniform(0, 1.5)

                print(
                    f"Gemini временно недоступен. "
                    f"Модель: {model_name}. "
                    f"Попытка {attempt + 1}/4. "
                    f"Пауза {delay:.1f} сек."
                )

                await asyncio.sleep(delay)
        else:
                                                                          
            print(f"Модель {model_name} не ответила после повторов. Пробую следующую модель.")

    return {
        "ok": False,
        "keywords": [],
        "description": None,
        "classification": None,
        "warnings": [],
        "model_used": None,
        "error": f"Все Gemini-модели не подошли или недоступны. Последняя ошибка: {last_error_text}",
    }


async def process_one_channel(pool: asyncpg.Pool, tg_client: TelegramClient) -> bool:
    channel = await get_next_new_channel(pool)

    if not channel:
        print("Нет каналов со status = 'new'.")
        return False

    channel_id = channel["id"]
    username = channel["username"]

    RUN_STATS["channels_taken"] += 1

    print(f"\nОбрабатываю канал: @{username}")

    try:
        tg_title, tg_description, raw_messages = await fetch_telegram_metadata_and_messages(
            tg_client=tg_client,
            username=username,
        )

        await save_telegram_metadata(
            pool=pool,
            channel_id=channel_id,
            tg_title=tg_title,
            tg_description=tg_description,
        )

        short_messages, long_messages, common_terms = select_typical_messages(raw_messages)

        if len(long_messages) < AI_MIN_LONG_MESSAGES:
            error = (
                f"Недостаточно длинных очищенных сообщений для разметки: "
                f"собрано {len(long_messages)}, нужно минимум {AI_MIN_LONG_MESSAGES}. "
                f"Разметку канала останавливаю."
            )
            await update_channel_status(pool, channel_id, "not_enough_data", error)
            RUN_STATS["channels_not_enough_data"] += 1
            print(error)
            return True

        if len(short_messages) < AI_MIN_SHORT_MESSAGES_DESIRED:
            print(
                f"Коротких начал сообщений мало: собрано {len(short_messages)}, "
                f"желательно минимум {AI_MIN_SHORT_MESSAGES_DESIRED}. Продолжаю разметку."
            )

        print(f"Telegram title: {tg_title}")
        print(f"Telegram description: {tg_description[:200]}")
        print(f"Сообщений просмотрено: {len(raw_messages)}")
        print(f"Повторяющиеся темы: {common_terms}")
        print(f"Коротких начал сообщений для ИИ: {len(short_messages)}")
        print(f"Длинных примеров для ИИ: {len(long_messages)}")

        for index, message in enumerate(short_messages, start=1):
            print(f"\n--- Короткое начало {index}, {len(message)} символов ---")
            print(message[:300])

        for index, message in enumerate(long_messages, start=1):
            print(f"\n--- Длинный пример {index}, {len(message)} символов ---")
            print(message[:500])

        result = await ask_ai_for_channel_markup(
            username=username,
            tg_title=tg_title,
            tg_description=tg_description,
            short_messages=short_messages,
            long_messages=long_messages,
            common_terms=common_terms,
        )

        if not result["ok"]:
            await update_channel_status(pool, channel_id, "error", result["error"])
            RUN_STATS["channels_ai_error"] += 1
            print(f"Ошибка ИИ: {result['error']}")
            return True

        await save_ai_markup(
            pool=pool,
            channel_id=channel_id,
            username=username,
            ai_keywords=result["keywords"],
            ai_description=result["description"],
            ai_classification=result["classification"],
            ai_warnings=result.get("warnings") or [],
            model_used=result.get("model_used"),
        )

        RUN_STATS["channels_marked_success"] += 1

        classification = result["classification"]

        print("\nГотово.")
        print(f"AI model used: {result.get('model_used')}")
        print(f"AI category: {classification.get('категория')}")
        print(f"AI additional categories: {classification.get('дополнительные категории')}")
        print(f"AI region: {classification.get('регион')}")
        print(f"AI format: {classification.get('формат')}")
        print(f"AI media type: {classification.get('тип СМИ')}")
        print(f"AI news topics: {classification.get('новостная тема')}")
        print(f"AI position: {classification.get('позиция') or classification.get('позиция подачи')}")
        print(f"AI coverage type: {classification.get('тип охвата')}")
        print(f"AI keywords: {result['keywords']}")
        print(f"AI description: {result['description']}")

        warnings = result.get("warnings") or []
        if warnings:
            print("Предупреждения проверки разметки:")
            for warning in warnings:
                print(f"- {warning}")

        print("AI classification:")
        print(json.dumps(classification, ensure_ascii=False, indent=2))

    except NotTelegramChannelError as exc:
        error = f"Not a Telegram channel: {exc}"
        await update_channel_status(pool, channel_id, "not_a_channel", error)
        RUN_STATS["channels_not_a_channel"] += 1
        print(error)

    except (ChannelInvalidError, ChannelPrivateError, UsernameInvalidError, UsernameNotOccupiedError) as exc:
        error = f"Telegram channel error: {type(exc).__name__}: {exc}"
        await update_channel_status(pool, channel_id, "error", error)
        RUN_STATS["channels_telegram_error"] += 1
        print(error)

    except FloodWaitError as exc:
                                                                                       
                                                              
        sleep_seconds = max(2, int(exc.seconds)) + 3

        error = (
            f"Telegram FloodWait: нужно подождать {exc.seconds} сек. "
            f"Канал возвращён в new."
        )

        await update_channel_status(pool, channel_id, "new", error)
        print(error)

        if sleep_seconds > FLOOD_WAIT_SLEEP_CAP_SECONDS:
            wait_until = datetime.now() + timedelta(seconds=sleep_seconds)
            stop_details = (
                f"Telegram FloodWait: нужно подождать {exc.seconds} сек.\n"
                f"С учётом запаса: {sleep_seconds} сек.\n"
                f"Можно попробовать продолжить после: {wait_until.strftime('%d.%m.%Y %H:%M:%S')}.\n"
                f"Канал @{username} возвращён в status='new'.\n"
                f"Лимит сна в скрипте: {FLOOD_WAIT_SLEEP_CAP_SECONDS} сек.\n"
                "Скрипт остановлен, чтобы не усугублять ограничения Telegram."
            )

            print(
                f"FloodWait слишком большой: {sleep_seconds} сек. "
                f"Лимит сна: {FLOOD_WAIT_SLEEP_CAP_SECONDS} сек. "
                "Останавливаю весь запуск, чтобы не усугублять ограничения Telegram."
            )

            RUN_STATS["floodwait_stops"] += 1

            set_program_exit_info(
                status="floodwait",
                title="Telegram попросил ждать",
                details=stop_details,
            )
            return False

        print(f"Малый FloodWait. Сплю {sleep_seconds} сек.")
        await asyncio.sleep(sleep_seconds)
        return True

    except Exception as exc:
        error = f"Unexpected error: {type(exc).__name__}: {exc}"
        await update_channel_status(pool, channel_id, "error", error)
        RUN_STATS["channels_unexpected_error"] += 1
        print(error)

    return True


async def main() -> None:
    reset_run_stats()

    set_program_exit_info(
        status="success",
        title="Программа выполнилась",
        details="Скрипт завершился без необработанных ошибок.",
    )

    started_at_wall = datetime.now()
    total_started_at = time.perf_counter()
    pool = await create_db_pool()
    processed_count = 0
    process_limit = max(1, CHANNELS_TO_PROCESS_PER_RUN)
    add_limit = max(0, CHANNELS_TO_ADD_FROM_TXT_PER_RUN)

    try:
        await ensure_channels_table(pool)
        await ensure_channel_ai_markup_table(pool)

        reset_count = await reset_stale_processing_channels(pool)
        if reset_count:
            print(f"Сброшено зависших processing-каналов: {reset_count}")

        await insert_channels_from_txt(pool, limit=add_limit)

        print(
            f"Лимит обработки за запуск: {process_limit} канал(ов). "
            f"Новых из txt добавить в очередь: {add_limit}. "
            f"Пауза между каналами: {CHANNEL_PROCESS_DELAY_SECONDS}+0..{CHANNEL_PROCESS_DELAY_JITTER_SECONDS} сек. "
            f"Малые Telegram-паузы внутри канала: "
            f"{TELEGRAM_INNER_CALL_DELAY_SECONDS}+0..{TELEGRAM_INNER_CALL_DELAY_JITTER_SECONDS} сек. "
            f"wait_time истории: {TELEGRAM_HISTORY_WAIT_SECONDS} сек."
        )

        async with TelegramClient(TELETHON_SESSION_NAME, API_ID, API_HASH) as tg_client:
                                                                           
                                                                                          
            tg_client.flood_sleep_threshold = 60

            for cycle_index in range(process_limit):
                channel_started_at = time.perf_counter()
                print(f"\n=== Цикл {cycle_index + 1}/{process_limit} ===")

                processed = await process_one_channel(pool, tg_client)
                channel_elapsed = time.perf_counter() - channel_started_at

                if not processed:
                    break

                processed_count += 1
                print(f"Время цикла {cycle_index + 1}: {format_duration(channel_elapsed)}")

                if processed_count >= process_limit:
                    break

                delay = get_channel_process_delay_seconds()

                if delay > 0:
                    print(f"Пауза перед следующим каналом: {delay:.1f} сек.")
                    await asyncio.sleep(delay)

    finally:
        total_elapsed = time.perf_counter() - total_started_at

        finished_at_wall = datetime.now()

        if processed_count:
            avg_seconds = total_elapsed / processed_count
            print(
                f"\nИТОГО: обработано каналов: {processed_count}/{process_limit}. "
                f"Общее время операции: {format_duration(total_elapsed)}. "
                f"Среднее время на канал: {format_duration(avg_seconds)}."
            )
        else:
            print(
                f"\nИТОГО: обработано каналов: 0/{process_limit}. "
                f"Общее время операции: {format_duration(total_elapsed)}."
            )

        current_details = PROGRAM_EXIT_INFO.get("details", "")
        PROGRAM_EXIT_INFO["details"] = build_run_summary_details(
            started_at=started_at_wall,
            finished_at=finished_at_wall,
            total_seconds=total_elapsed,
            process_limit=process_limit,
            add_limit=add_limit,
            extra_details=current_details,
        )

        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        notify_program_result(
            status="stopped",
            title="Программа остановлена вручную",
            details="Остановка через Ctrl+C или закрытие процесса.",
        )
        raise

    except Exception as error:
        notify_program_result(
            status="error",
            title="Программа завершилась с ошибкой",
            details="Скрипт упал. Ниже причина и traceback.",
            error=error,
        )
        raise

    else:
        notify_program_result(
            status=PROGRAM_EXIT_INFO["status"],
            title=PROGRAM_EXIT_INFO["title"],
            details=PROGRAM_EXIT_INFO["details"],
        )