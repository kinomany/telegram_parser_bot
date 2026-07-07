import re

from config import TOP_RELEVANT_MESSAGES_LIMIT, MIN_RELEVANT_MESSAGE_SCORE
from app.utils.debug import debug_print_block, compact_debug_message, is_search_debug_enabled
from app.search.channel_search import normalize_match_text

def normalize_match_text(value: str | None) -> str:
    return (value or "").lower().replace("ё", "е")


MESSAGE_STOP_WORDS = {
    "новости", "новость", "канал", "пост", "посты", "информация", "события",
    "материалы", "telegram", "телеграм", "ссылка", "подробнее", "сегодня",
    "вчера", "завтра", "который", "которая", "которые", "этого", "этот", "эта",
    "что", "как", "где", "про", "для", "или", "если", "мне", "надо", "найди",
    "напиши", "расскажи", "сделай", "обзор", "дай", "есть", "все", "самое",
}


def normalize_token(token: str) -> str:
    token = normalize_match_text(token)
    token = re.sub(r"[^a-zа-я0-9-]", "", token)
    return token.strip("-_")


def token_stem(token: str) -> str:
    """
    Очень простой русский/английский стемминг без внешних библиотек.
    Нужен не для идеальной лингвистики, а чтобы 'Украина/Украине/Украины'
    и 'нейросеть/нейросети' чаще совпадали.
    """
    token = normalize_token(token)
    if len(token) <= 5:
        return token

    endings = (
        "иями", "ями", "ами", "ого", "ему", "ыми", "ими", "ее", "ие", "ые", "ое",
        "ей", "ий", "ый", "ой", "ая", "яя", "ую", "юю", "ам", "ям", "ах", "ях",
        "ов", "ев", "ом", "ем", "ою", "ею", "ия", "иям", "иях", "ии", "ию", "ия",
        "а", "я", "ы", "и", "е", "у", "ю", "о",
    )

    for ending in endings:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[:-len(ending)]

    return token


def extract_search_tokens(text: str | None, limit: int = 20) -> list[str]:
    text = normalize_match_text(text)
    raw_tokens = re.findall(r"[a-zа-я0-9-]{3,}", text)

    result: list[str] = []
    seen: set[str] = set()

    for raw in raw_tokens:
        token = normalize_token(raw)
        if not token or token in MESSAGE_STOP_WORDS:
            continue

        stem = token_stem(token)
        if len(stem) < 3 or stem in MESSAGE_STOP_WORDS or stem in seen:
            continue

        seen.add(stem)
        result.append(stem)

        if len(result) >= limit:
            break

    return result



def extract_user_query_phrases(user_query: str | None, limit: int = 5) -> list[str]:
    """
    Достаём короткие фразы прямо из запроса пользователя.
    Это нужно для запросов вроде:
    "Советы по здоровью. Защита от клещей"

    ИИ может дать общие keywords: здоровье, профилактика.
    А слово/фраза пользователя "клещей" должны сильно помогать отбору сообщений.
    """
    text = normalize_match_text(user_query)

    if not text:
        return []

    chunks = re.split(r"[.!?;,\n]+", text)
    result = []
    seen = set()

    for chunk in chunks:
        tokens = extract_search_tokens(chunk, limit=6)

        if len(tokens) < 2:
            continue

        phrase = " ".join(tokens)
        if phrase in seen:
            continue

        seen.add(phrase)
        result.append(phrase)

        if len(result) >= limit:
            break

    return result


def build_message_query_terms(query_markup: dict, user_query: str | None = None) -> list[dict]:
    """
    Собирает поисковые термины для постов.
    Важно: тут используются не только keywords от ИИ, но и слова из исходного запроса.
    Иначе один кривой JSON от ИИ убивает весь отбор сообщений.
    """
    weighted_sources = [
        (query_markup.get("objects") or [], 12, "объект"),
        (query_markup.get("keywords") or [], 10, "keyword"),
        (query_markup.get("topics") or [], 7, "тема"),
    ]

    terms: list[dict] = []
    seen: set[str] = set()

    for values, weight, label in weighted_sources:
        for value in values:
            if not isinstance(value, str):
                continue
            phrase = normalize_match_text(value).strip()
            if not phrase or phrase in MESSAGE_STOP_WORDS:
                continue

            key = f"phrase:{phrase}"
            if key not in seen:
                seen.add(key)
                terms.append({"value": phrase, "tokens": extract_search_tokens(phrase), "weight": weight, "label": label})

                                                                          
                                                             
    for phrase in extract_user_query_phrases(user_query, limit=5):
        key = f"user_phrase:{phrase}"
        if key not in seen:
            seen.add(key)
            terms.append({
                "value": phrase,
                "tokens": extract_search_tokens(phrase, limit=8),
                "weight": 14,
                "label": "фраза пользователя",
            })

    for token in extract_search_tokens(user_query, limit=12):
        key = f"user_token:{token}"
        if key not in seen:
            seen.add(key)
            terms.append({"value": token, "tokens": [token], "weight": 12, "label": "слово пользователя"})

    category = normalize_match_text(query_markup.get("category"))
    if category and category != "неясно":
        for token in extract_search_tokens(category, limit=3):
            key = f"category:{token}"
            if key not in seen:
                seen.add(key)
                terms.append({"value": token, "tokens": [token], "weight": 2, "label": "категория"})

    return terms


def token_matches_text(token: str, text_tokens: set[str]) -> bool:
    token = token_stem(token)
    if not token:
        return False

    for text_token in text_tokens:
        if token == text_token:
            return True
                                                                              
        if len(token) >= 5 and len(text_token) >= 5:
            if token.startswith(text_token) or text_token.startswith(token):
                return True

    return False


def term_score(term: dict, text: str, text_tokens: set[str]) -> int:
    value = normalize_match_text(term.get("value"))
    tokens = term.get("tokens") or []
    weight = int(term.get("weight") or 0)

    if not value or not tokens:
        return 0

                                            
    if value in text:
        return weight

    matched_count = sum(1 for token in tokens if token_matches_text(token, text_tokens))
    if matched_count <= 0:
        return 0

    if len(tokens) == 1:
        return max(1, int(weight * 0.75))

                                                                                
    ratio = matched_count / len(tokens)
    if ratio >= 0.66:
        return max(1, int(weight * ratio))

    return 0


def score_message_for_query(query_markup: dict, message_item: dict, user_query: str | None = None) -> tuple[int, list[str]]:
    text = normalize_match_text(message_item.get("cleaned_text") or "")
    title = normalize_match_text(message_item.get("title") or "")
    full_text = f"{title}\n{text}"
    text_tokens = set(extract_search_tokens(full_text, limit=300))

    score = 0
    reasons: list[str] = []
    debug_terms: list[dict] = []

    terms = build_message_query_terms(query_markup, user_query=user_query)

    for term in terms:
        points = term_score(term, full_text, text_tokens)
        debug_terms.append({
            "label": term.get("label"),
            "value": term.get("value"),
            "tokens": term.get("tokens"),
            "weight": term.get("weight"),
            "points": points,
        })

        if points > 0:
            score += points
            reasons.append(f"{term['label']}: {term['value']} +{points}")

    negative_debug = []

    for negative in query_markup.get("negative_keywords") or []:
        negative_tokens = extract_search_tokens(str(negative), limit=8)
        hit = bool(negative_tokens and any(token_matches_text(token, text_tokens) for token in negative_tokens))

        negative_debug.append({
            "value": negative,
            "tokens": negative_tokens,
            "hit": hit,
            "points": -20 if hit else 0,
        })

        if hit:
            score -= 20
            reasons.append(f"минус: {negative} -20")

    length = len(text)
    length_points = 0

    if 250 <= length <= 1800:
        length_points = 3
        score += length_points
        reasons.append("длина сообщения: нормальная +3")
    elif 100 <= length < 250:
        length_points = 1
        score += length_points
        reasons.append("длина сообщения: короткая, но ок +1")
    elif length > 2500:
        length_points = -2
        score += length_points
        reasons.append("длина сообщения: слишком длинное -2")

    if is_search_debug_enabled():
        debug_print_block(
            "Расчёт score сообщения",
            {
                "username": message_item.get("username"),
                "title": message_item.get("title"),
                "date_text": message_item.get("date_text"),
                "final_score": score,
                "text_length": length,
                "text_preview": (message_item.get("cleaned_text") or "")[:700],
                "query_markup": query_markup,
                "terms_all": debug_terms,
                "terms_matched": [item for item in debug_terms if item["points"] > 0],
                "negative_terms": negative_debug,
                "length_points": length_points,
                "final_reasons": reasons[:12],
            },
        )

    return score, reasons[:8]


def rank_messages_for_query(
    query_markup: dict,
    messages: list[dict],
    limit: int = TOP_RELEVANT_MESSAGES_LIMIT,
    user_query: str | None = None,
) -> list[dict]:
    ranked = []

    for item in messages:
        score, reasons = score_message_for_query(query_markup, item, user_query=user_query)

        if score < MIN_RELEVANT_MESSAGE_SCORE:
            continue

        ranked.append({
            **item,
            "score": score,
            "matched": reasons,
        })

    ranked.sort(key=lambda item: item["score"], reverse=True)
    result = ranked[:limit]

    debug_print_block(
        f"Топ-{limit} сообщений после score",
        [compact_debug_message(item) for item in result],
    )

    return result


def format_ranked_messages_for_user(ranked_messages: list[dict]) -> str:
    if not ranked_messages:
        return ""

    output_text = f"Топ-{len(ranked_messages)} сообщений по счёту:\n\n"

    for index, item in enumerate(ranked_messages, start=1):
        username = item.get("username") or ""
        title = item.get("title") or username
        date_text = item.get("date_text") or "без даты"
        score = item.get("score", 0)
        cleaned_text = item.get("cleaned_text") or ""
        matched = item.get("matched") or []

        output_text += f"{index}. 📌 {title} ({username})\n"
        output_text += f"score: {score}\n"
        output_text += f"🕒 {date_text}\n"

        if matched:
            output_text += "почему: " + "; ".join(matched) + "\n"

        output_text += f"{cleaned_text}\n\n"

    return output_text.strip()
