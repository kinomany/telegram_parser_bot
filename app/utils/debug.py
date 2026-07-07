import json
import os
import re

SEARCH_DEBUG_ENV_NAME = "SEARCH_DEBUG"

def is_search_debug_enabled() -> bool:
    value = os.getenv(SEARCH_DEBUG_ENV_NAME, "").strip().lower()
    return value in {"1", "true", "yes", "y", "on", "debug"}


def debug_print_block(title: str, data=None) -> None:
    if not is_search_debug_enabled():
        return

    print("\n" + "=" * 90)
    print(f"SEARCH DEBUG | {title}")
    print("=" * 90)

    if data is None:
        return

    if isinstance(data, str):
        print(data)
        return

    try:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    except TypeError:
        print(data)

def compact_debug_message(item: dict, max_text_chars: int = 500) -> dict:
    text = item.get("cleaned_text") or item.get("message_text") or ""
    text = re.sub(r"\s{2,}", " ", str(text)).strip()

    return {
        "username": item.get("username"),
        "title": item.get("title"),
        "date_text": item.get("date_text"),
        "score": item.get("score"),
        "matched": item.get("matched"),
        "text_preview": text[:max_text_chars],
    }
