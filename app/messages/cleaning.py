import re

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


AD_MESSAGE_RE = re.compile(
    r"(?iu)"
    r"("
    r"#\s*реклама\b|"
    r"о\s+рекламодателе\b|"
    r"на\s+правах\s+рекламы|"
    r"рекламн(?:ый|ая|ое|ые)\s+материал|"
    r"партн[её]рск(?:ий|ая|ое|ие)\s+материал|"
    r"спонсорск(?:ий|ая|ое|ие)\s+материал|"
    r"узнать\s+больше\s+#?\s*реклама|"
    r"финансовые\s+услуги\s+оказывает|"
    r"пассивн(?:ый|ого)\s+доход|"
    r"\b\d{1,2}[,.]?\d?\s*%\s+годовых|"
    r"инвестируйте|"
    r"рентн(?:ые|ых)\s+выплат|"
    r"купи\s+.*\s+чек|"
    r"загрузи\s+чек|"
    r"выиграй\s+мечт|"
    r"promo\.[a-z0-9.-]+|"
    r"clients\.site|"
    r"alfacapital|"
    r"morpheusbed|"
    r"chernogolovka-promo"
    r")"
)

CROSS_PROMO_RE = re.compile(
    r"(?iu)"
    r"("
    r"рекомендуем\s+подписаться|"
    r"подписаться,\s*чтобы\s+не\s+пропустить|"
    r"подпишитесь,\s*чтобы\s+не\s+пропустить|"
    r"канал(?:у)?\s+за\s+вдохновение|"
    r"необычные\s+лайфхаки.*полезные\s+советы|"
    r"мы\s+в\s+мах\b"
    r")"
)


def clean_message_text(text: str | None) -> str:
    """Грубая очистка текста перед сохранением: удаляем ссылки, markdown-разметку и мусор."""
    if not text:
        return ""

    text = remove_links_completely(text)

                         
    text = re.sub(r"@\w+", " ", text)

                         
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = text.replace("`", "")

                         
    text = text.replace("\xa0", " ")

                               
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    return text.strip()

def get_filter_reason(cleaned_text: str, original_text: str | None) -> str | None:
    """
    Фильтр перед сохранением/score.

    Рекламу режем до попадания в structured_messages, иначе она забивает топ-10
    словами вроде "советы", "доход", "узнать больше", "лайфхаки".
    """
    if not original_text or not original_text.strip():
        return "empty_text"

    if AD_MESSAGE_RE.search(original_text):
        return "ad_marker"

    if CROSS_PROMO_RE.search(original_text):
        return "cross_promo"

    if not cleaned_text:
        return "only_links_or_mentions"

    return None


def format_filter_stats(filter_stats: dict) -> str:
    """Готовит текстовую статистику по причинам отбраковки сообщений."""
    if not filter_stats:
        return ""

    reason_titles = {
        "empty_text": "пустой текст",
        "only_links_or_mentions": "только ссылки/упоминания",
        "too_short": "слишком короткие",
        "ad_marker": "явная реклама",
        "cross_promo": "перекрёстная реклама / промо канала",
        "too_many_links": "много ссылок",
        "trash_words": "рекламные слова",
    }

    filter_text = "\n\nОтфильтровано:\n"

    for reason, count in sorted(filter_stats.items()):
        title = reason_titles.get(reason, reason)
        filter_text += f"{title}: {count}\n"

    return filter_text.rstrip()
