BOT_INTRO_TEXT = (
    "Привет. Я собираю сводки по Telegram-каналам.\n\n"
    "Что можно сделать:\n"
    "• 🔎 найти каналы по теме\n"
    "• 📋 управлять своими каналами\n"
    "• 🔔 настроить автосводки"
)

QUERY_GUIDE_TEXT = (
    "Как писать запрос:\n\n"
    "Пиши тему + уточнения + что исключить.\n\n"
    "Пример:\n"
    "Рынок труда в IT: вакансии, зарплаты и релокация; без курсов."
)

SHORT_QUERY_HINT_TEXT = (
    "Запрос слишком короткий.\n\n"
    "Напиши тему + уточнения.\n"
    "Пример: Рынок труда в IT: вакансии, зарплаты и релокация; без курсов."
)

CHANNEL_PICK_EXPLANATION = "Подбираю каналы из твоей базы."

REPORT_PRESET_BY_BUTTON = {
    "📰 Краткая сводка": "brief",
    "🧩 Общий сюжет": "story",
    "⚖️ Сравнение источников": "compare_sources",
    "✅ Практическая польза": "practical",
    "🚨 Срочное / происшествие": "urgent",
    "🧾 Только факты": "facts_only",
}

REPORT_PRESET_TITLE_BY_ID = {
    "brief": "Краткая сводка",
    "story": "Общий сюжет",
    "compare_sources": "Сравнение источников",
    "practical": "Практическая польза",
    "urgent": "Срочное / происшествие",
    "facts_only": "Только факты",
}

REPORT_PRESET_HELP_TEXT = (
    "Выбери тип отчёта:\n\n"
    "📰 Краткая — главное.\n"
    "🧩 Сюжет — общая картина.\n"
    "⚖️ Сравнение — кто что пишет.\n"
    "✅ Польза — выводы и риски.\n"
    "🚨 Срочное — что известно.\n"
    "🧾 Факты — без анализа."
)


def get_report_preset_from_text(text: str | None) -> str | None:
    return REPORT_PRESET_BY_BUTTON.get((text or "").strip())


def get_report_preset_title(report_preset: str | None) -> str:
    return REPORT_PRESET_TITLE_BY_ID.get(report_preset or "brief", "Краткая сводка")
