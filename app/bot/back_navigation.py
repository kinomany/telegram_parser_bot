from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackTarget:
    state: str
    screen: str


                                                                                  
                                                                                  
                                                                       
BACK_NAVIGATION_TARGETS: dict[str, BackTarget] = {
    "menu": BackTarget("menu", "Главное меню"),
    "settings": BackTarget("menu", "Главное меню"),
    "admin_menu": BackTarget("menu", "Главное меню"),
    "waiting_vacancy_channel_query": BackTarget("admin_menu", "Админка"),
    "waiting_vacancy_keywords": BackTarget("admin_menu", "Админка"),
    "channels_menu": BackTarget("menu", "Главное меню"),
    "user_channels_view_menu": BackTarget("channels_menu", "Мои каналы"),
    "waiting_channel": BackTarget("channels_menu", "Мои каналы"),
    "waiting_delete_channel_number": BackTarget("channels_menu", "Мои каналы"),
    "waiting_delete_channel_confirm": BackTarget("waiting_delete_channel_number", "Выбор канала для удаления"),
    "waiting_user_channel_category_full": BackTarget("waiting_user_channel_category", "Короткий список категорий"),
    "waiting_user_channel_category": BackTarget("waiting_channel", "Добавление канала"),
    "waiting_user_channel_category_view": BackTarget("user_channels_view_menu", "Просмотр каналов"),
    "digest_channel_view_menu": BackTarget("channels_menu", "Мои каналы"),
    "waiting_digest_history_number": BackTarget("channels_menu", "Мои каналы"),
    "waiting_digest_category_view": BackTarget("digest_channel_view_menu", "Выбор показа каналов для сводки"),
    "waiting_digest_channel_numbers": BackTarget("digest_channel_view_menu", "Выбор показа каналов для сводки"),
    "waiting_digest_period": BackTarget("waiting_digest_channel_numbers", "Выбор каналов для сводки"),
    "waiting_digest_custom_period": BackTarget("waiting_digest_period", "Выбор периода"),
    "autodigest_menu": BackTarget("channels_menu", "Мои каналы"),
    "waiting_subscription_disable_number": BackTarget("autodigest_menu", "Автосводки"),
    "waiting_subscription_run_number": BackTarget("autodigest_menu", "Автосводки"),
    "waiting_subscription_preview_number": BackTarget("autodigest_menu", "Автосводки"),
    "waiting_subscription_manage_number": BackTarget("autodigest_menu", "Автосводки"),
    "waiting_subscription_run_update_choice": BackTarget("waiting_subscription_manage_action", "Карточка автосводки"),
    "waiting_subscription_channel_numbers": BackTarget("autodigest_menu", "Автосводки"),
    "waiting_subscription_period": BackTarget("waiting_subscription_channel_numbers", "Выбор каналов для автосводки"),
    "waiting_subscription_preset": BackTarget("waiting_subscription_period", "Выбор частоты автосводки"),
    "waiting_subscription_timezone": BackTarget("waiting_subscription_preset", "Выбор формата автосводки"),
    "waiting_subscription_custom_timezone": BackTarget("waiting_subscription_timezone", "Выбор timezone"),
    "waiting_subscription_time": BackTarget("waiting_subscription_timezone", "Выбор timezone"),
    "waiting_subscription_custom_time": BackTarget("waiting_subscription_time", "Выбор времени"),
    "waiting_subscription_manage_action": BackTarget("waiting_subscription_manage_number", "Список автосводок"),
    "waiting_subscription_change_period": BackTarget("waiting_subscription_manage_action", "Карточка автосводки"),
    "waiting_subscription_change_preset": BackTarget("waiting_subscription_manage_action", "Карточка автосводки"),
    "waiting_subscription_change_time": BackTarget("waiting_subscription_manage_action", "Карточка автосводки"),
    "waiting_subscription_change_timezone": BackTarget("waiting_subscription_manage_action", "Карточка автосводки"),
    "waiting_subscription_delete_confirm": BackTarget("waiting_subscription_manage_action", "Карточка автосводки"),
    "waiting_subscription_add_channel_numbers": BackTarget("waiting_subscription_manage_action", "Карточка автосводки"),
    "waiting_subscription_remove_channel_numbers": BackTarget("waiting_subscription_manage_action", "Карточка автосводки"),
    "waiting_subscription_change_custom_time": BackTarget("waiting_subscription_change_time", "Выбор времени"),
    "waiting_subscription_change_custom_timezone": BackTarget("waiting_subscription_change_timezone", "Выбор timezone"),
    "waiting_found_channels_action": BackTarget("waiting_channel_search_query", "Новый запрос"),
    "waiting_found_channel_count": BackTarget("waiting_found_channels_action", "Настройка найденных каналов"),
    "waiting_found_channel_list": BackTarget("waiting_found_channels_action", "Настройка найденных каналов"),
    "waiting_found_report_preset": BackTarget("waiting_found_channels_action", "Настройка найденных каналов"),
    "waiting_found_channels_period": BackTarget("waiting_found_report_preset", "Выбор типа отчёта"),
    "waiting_found_channels_custom_period": BackTarget("waiting_found_channels_period", "Выбор периода"),
    "parse_menu": BackTarget("channels_menu", "Мои каналы"),
    "waiting_parse_channel_numbers": BackTarget("channels_menu", "Мои каналы"),
    "waiting_parse_period": BackTarget("waiting_parse_channel_numbers", "Выбор каналов"),
    "waiting_custom_period": BackTarget("waiting_parse_period", "Выбор периода"),
}


def get_back_navigation_target(state: str | None) -> BackTarget:
    return BACK_NAVIGATION_TARGETS.get((state or "menu"), BackTarget("menu", "Главное меню"))
