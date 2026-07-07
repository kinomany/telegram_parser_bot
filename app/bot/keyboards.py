from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from app.ai.tax_tree import ГЛАВНЫЕ_КАТЕГОРИИ

BACK_BUTTON_TEXT = "⬅️ Назад"


main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎯 Подобрать каналы по теме")],
        [KeyboardButton(text="📋 Мои каналы")],
        [KeyboardButton(text="ℹ️ Помощь")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие",
)


channels_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="👁 Просмотреть каналы"), KeyboardButton(text="➕ Добавить канал")],
        [KeyboardButton(text="🗑 Удалить канал")],
        [KeyboardButton(text="🧾 Сделать сводку"), KeyboardButton(text="🕘 История сводок")],
        [KeyboardButton(text="🔔 Автосводки")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Мои каналы",
)


channel_view_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="🗂 По категориям"), KeyboardButton(text="📋 Показать все каналы")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Просмотр каналов",
)


user_channel_delete_confirm_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="✅ Да, удалить канал")],
        [KeyboardButton(text="↩️ Нет, оставить")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Подтверждение удаления канала",
)


                                                                                                         
parse_keyboard = channels_keyboard


found_channels_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="📥 Читать выбранные каналы")],
        [KeyboardButton(text="🔢 Изменить количество"), KeyboardButton(text="☑️ Изменить список")],
        [KeyboardButton(text="🔄 Добор каналов"), KeyboardButton(text="➕ Добавить канал")],
        [KeyboardButton(text="🔁 Другой запрос")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Настройте каналы для чтения",
)


                                                               
search_results_keyboard = found_channels_keyboard


found_channels_count_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="1"), KeyboardButton(text="2"), KeyboardButton(text="3"), KeyboardButton(text="4"), KeyboardButton(text="5")],
        [KeyboardButton(text="6"), KeyboardButton(text="7"), KeyboardButton(text="8"), KeyboardButton(text="9"), KeyboardButton(text="10")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Сколько каналов читать",
)


report_preset_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="📰 Краткая сводка"), KeyboardButton(text="🧩 Общий сюжет")],
        [KeyboardButton(text="⚖️ Сравнение источников"), KeyboardButton(text="✅ Практическая польза")],
        [KeyboardButton(text="🚨 Срочное / происшествие"), KeyboardButton(text="🧾 Только факты")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите тип ИИ-отчёта",
)


period_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="📅 За день"), KeyboardButton(text="📅 За неделю")],
        [KeyboardButton(text="📅 За месяц"), KeyboardButton(text="📆 Свой период")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите период",
)


digest_channel_view_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="🗂 По категориям"), KeyboardButton(text="📋 Все каналы")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Каналы для сводки",
)



digest_history_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="🔄 Обновить историю")],
    ],
    resize_keyboard=True,
    input_field_placeholder="История сводок",
)

digest_period_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="📅 За день"), KeyboardButton(text="📅 За неделю")],
        [KeyboardButton(text="📅 За месяц"), KeyboardButton(text="📆 Свой период")],
        [KeyboardButton(text="🕓 С прошлого дайджеста")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Период для сводки",
)


def build_autodigest_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="📋 Мои автосводки"), KeyboardButton(text="➕ Создать автосводку")],
        [KeyboardButton(text="⚙️ Управление автосводками")],
        [KeyboardButton(text="▶️ Запустить сейчас")],
    ]

    if is_admin:
        keyboard.extend([
            [KeyboardButton(text="🧪 Что уйдёт в ИИ")],
            [KeyboardButton(text="🧪 Debug: проверить сейчас"), KeyboardButton(text="🧪 Debug: статус scheduler")],
        ])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Автосводки",
    )


def build_subscription_manage_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="▶️ Включить автосводку"), KeyboardButton(text="⏸ Отключить автосводку")],
        [KeyboardButton(text="➕ Добавить канал в автосводку"), KeyboardButton(text="➖ Убрать канал из автосводки")],
        [KeyboardButton(text="🔁 Изменить период"), KeyboardButton(text="🧾 Изменить пресет")],
        [KeyboardButton(text="🕘 Изменить время"), KeyboardButton(text="🌍 Изменить timezone")],
        [KeyboardButton(text="▶️ Запустить сейчас")],
    ]

    if is_admin:
        keyboard.extend([
            [KeyboardButton(text="🧪 Debug: проверить сейчас")],
            [KeyboardButton(text="🧪 Что уйдёт в ИИ")],
        ])

    keyboard.extend([
        [KeyboardButton(text="🗑 Удалить автосводку")],
        [KeyboardButton(text="📋 К списку автосводок")],
    ])

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Управление автосводкой",
    )


                                                          
                                                                      
autodigest_keyboard = build_autodigest_keyboard(is_admin=False)
subscription_manage_keyboard = build_subscription_manage_keyboard(is_admin=False)

subscription_delete_confirm_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="✅ Да, удалить автосводку")],
        [KeyboardButton(text="↩️ Нет, оставить")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Подтверждение удаления",
)


subscription_run_update_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="✅ Да, обновить точку парса")],
        [KeyboardButton(text="👀 Нет, просто посмотреть")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Обновить точку прошлого парса?",
)

subscription_period_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="🔁 Раз в 3 дня"), KeyboardButton(text="🔁 Раз в неделю")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Как часто отправлять",
)


subscription_digest_preset_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="⚡ Только главное")],
        [KeyboardButton(text="🧾 Обычная сводка")],
        [KeyboardButton(text="📚 Подробная сводка")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Формат автосводки",
)

subscription_timezone_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="🌍 Asia/Tbilisi"), KeyboardButton(text="🌍 Europe/Moscow")],
        [KeyboardButton(text="🌍 Europe/Kyiv"), KeyboardButton(text="🌍 UTC")],
        [KeyboardButton(text="✍️ Свой timezone")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Часовой пояс",
)


subscription_time_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="🕘 09:00"), KeyboardButton(text="☀️ 12:00")],
        [KeyboardButton(text="🌆 18:00"), KeyboardButton(text="🌙 21:00")],
        [KeyboardButton(text="✍️ Своё время")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Время отправки",
)


settings_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="📖 Команды"), KeyboardButton(text="🎯 Как писать запрос")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Помощь",
)

admin_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BACK_BUTTON_TEXT)],
        [KeyboardButton(text="📊 Статус системы"), KeyboardButton(text="❌ Последние ошибки")],
        [KeyboardButton(text="🔔 Статус автосводок")],
        [KeyboardButton(text="💼 Парс вакансий")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Админка",
)


COMMON_USER_CHANNEL_CATEGORIES = [
    "новости и СМИ",
    "политика",
    "война и конфликты",
    "экономика",
    "технологии",
    "софт и приложения",
    "игры",
    "медицина",
    "здоровье и фитнес",
    "местные новости",
    "культура",
    "спорт",
    "другое",
]


def build_user_channel_category_keyboard(full: bool = False) -> ReplyKeyboardMarkup:
    categories = ГЛАВНЫЕ_КАТЕГОРИИ if full else COMMON_USER_CHANNEL_CATEGORIES
    rows = []

    rows.append([KeyboardButton(text=BACK_BUTTON_TEXT)])

    for index in range(0, len(categories), 2):
        rows.append([KeyboardButton(text=category) for category in categories[index:index + 2]])

    if not full:
        rows.append([KeyboardButton(text="📚 Ещё категории")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите категорию канала",
    )


def build_user_channel_category_view_keyboard(categories: list[dict]) -> ReplyKeyboardMarkup:
    rows = []

    rows.append([KeyboardButton(text=BACK_BUTTON_TEXT)])

    for item in categories:
        category = item.get("user_category") or "другое"
        count = int(item.get("channels_count") or 0)
        rows.append([KeyboardButton(text=f"📁 {category} ({count})")])

    rows.append([KeyboardButton(text="📋 Показать все каналы")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите категорию",
    )
