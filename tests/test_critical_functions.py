from __future__ import annotations

import unittest
from datetime import datetime, time, timezone

from app.bot.back_navigation import get_back_navigation_target
from app.utils.parsing import (
    normalize_channel_input,
    normalize_telegram_username,
    parse_channel_numbers,
)
from app.utils.subscription_channels import (
    get_user_channel_ids_from_channels,
    normalize_channel_for_collect,
    normalize_subscription_channels,
)
from app.utils.timezones import calculate_next_run_at, parse_send_time


class ParseSendTimeTests(unittest.TestCase):
    def test_parse_hh_mm(self):
        self.assertEqual(parse_send_time("09:00"), time(9, 0))
        self.assertEqual(parse_send_time("09:00:00"), time(9, 0))
        self.assertEqual(parse_send_time("🌙 21:30"), time(21, 30))

    def test_parse_time_object(self):
        self.assertEqual(parse_send_time(time(18, 45, 33)), time(18, 45))

    def test_reject_bad_time(self):
        for value in ["24:00", "09:99", "утром", "9", ""]:
            with self.subTest(value=value):
                if value == "":
                                                                      
                    self.assertEqual(parse_send_time(value), time(9, 0))
                else:
                    with self.assertRaises(ValueError):
                        parse_send_time(value)


class CalculateNextRunAtTests(unittest.TestCase):
    def test_next_run_from_previous_schedule_in_timezone(self):
        now = datetime(2026, 6, 26, 8, 0, tzinfo=timezone.utc)
        from_time = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)

        result = calculate_next_run_at(
            period_days=7,
            send_time="09:00",
            timezone_name="Asia/Tbilisi",
            from_time=from_time,
            now=now,
        )

                                                              
        self.assertEqual(result, datetime(2026, 6, 27, 5, 0, tzinfo=timezone.utc))

    def test_rolls_forward_if_candidate_is_already_past(self):
        now = datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc)
        from_time = datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc)

        result = calculate_next_run_at(
            period_days=7,
            send_time="09:00",
            timezone_name="Asia/Tbilisi",
            from_time=from_time,
            now=now,
        )

        self.assertEqual(result, datetime(2026, 7, 4, 5, 0, tzinfo=timezone.utc))


class SubscriptionChannelNormalizationTests(unittest.TestCase):
    def test_json_string_list(self):
        raw = '[{"id": 1, "username": "@alpha", "title": "Alpha"}, {"id": "2", "username": "beta_channel"}]'
        channels = normalize_subscription_channels(raw)
        self.assertEqual(len(channels), 2)
        self.assertEqual(channels[0]["username"], "@alpha")
        self.assertEqual(normalize_channel_for_collect(channels[1])["username"], "@beta_channel")
        self.assertEqual(get_user_channel_ids_from_channels(channels), [1, 2])

    def test_plain_string_list_fallback(self):
        channels = normalize_subscription_channels("@alpha, beta_channel\n@gamma")
        usernames = [normalize_channel_for_collect(ch)["username"] for ch in channels]
        self.assertEqual(usernames, ["@alpha", "@beta_channel", "@gamma"])

    def test_dict_and_bad_values(self):
        self.assertEqual(normalize_subscription_channels(None), [])
        self.assertEqual(normalize_subscription_channels(123), [])
        one = normalize_subscription_channels({"id": 5, "username": "delta"})
        self.assertEqual(len(one), 1)
        self.assertEqual(normalize_channel_for_collect(one[0])["username"], "@delta")


class UsernameNormalizationTests(unittest.TestCase):
    def test_channel_input_requires_at_or_link(self):
        self.assertEqual(normalize_channel_input("@Some_Channel"), "@Some_Channel")
        self.assertEqual(normalize_channel_input("https://t.me/some_channel/123?single"), "@some_channel")
        self.assertIsNone(normalize_channel_input("some_channel"))

    def test_general_username_can_accept_plain(self):
        self.assertEqual(normalize_telegram_username("some_channel"), "@some_channel")
        self.assertEqual(normalize_telegram_username("t.me/some_channel"), "@some_channel")
        self.assertIsNone(normalize_telegram_username("https://t.me/+privateInvite"))
        self.assertIsNone(normalize_telegram_username("bad!name"))


class ChannelNumberParsingTests(unittest.TestCase):
    def test_valid_numbers(self):
        self.assertEqual(parse_channel_numbers("1 2 5", 10, max_selected=10), [0, 1, 4])
        self.assertEqual(parse_channel_numbers("1,2;2", 10, max_selected=10), [0, 1])

    def test_invalid_numbers(self):
        invalid = ["", "0", "11", "1 x", "1.2", "1 2 3 4"]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(parse_channel_numbers(value, 10, max_selected=3))


class BackNavigationTests(unittest.TestCase):
    def assertBack(self, state: str, expected_state: str):
        self.assertEqual(get_back_navigation_target(state).state, expected_state)

    def test_core_back_routes(self):
        cases = {
            "autodigest_menu": "channels_menu",
            "waiting_subscription_time": "waiting_subscription_timezone",
            "waiting_subscription_custom_time": "waiting_subscription_time",
            "waiting_subscription_change_custom_timezone": "waiting_subscription_change_timezone",
            "waiting_digest_period": "waiting_digest_channel_numbers",
            "waiting_found_channels_custom_period": "waiting_found_channels_period",
            "waiting_delete_channel_confirm": "waiting_delete_channel_number",
            "unknown_state": "menu",
        }
        for state, expected in cases.items():
            with self.subTest(state=state):
                self.assertBack(state, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)

class HandlerRegistrationTests(unittest.TestCase):
    def test_no_duplicate_catch_all_and_back_handlers(self):
        from pathlib import Path
        handlers_path = Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers.py"
        text = handlers_path.read_text(encoding="utf-8")
        self.assertEqual(text.count('@dp.message()'), 1)
        self.assertEqual(text.count('@dp.message(F.text == "⬅️ Назад")'), 1)
        self.assertEqual(text.count('async def text_handler'), 1)
        self.assertEqual(text.count('async def handle_back_navigation'), 1)

    def test_start_status_reset_are_registered(self):
        from pathlib import Path
        handlers_path = Path(__file__).resolve().parents[1] / "app" / "bot" / "handlers.py"
        text = handlers_path.read_text(encoding="utf-8")
        self.assertIn('@dp.message(CommandStart())\nasync def start_handler', text)
        self.assertIn('@dp.message(Command("status"))', text)
        self.assertIn('@dp.message(Command("reset"))', text)

class VacancyParserTests(unittest.TestCase):
    def test_parse_vacancy_keywords(self):
        from app.jobs.vacancy_parser import parse_vacancy_keywords
        self.assertEqual(
            parse_vacancy_keywords("Python, backend; remote\nPython"),
            ["python", "backend", "remote"],
        )

    def test_message_matches_keywords(self):
        from app.jobs.vacancy_parser import message_matches_keywords
        self.assertEqual(
            message_matches_keywords("Ищем Python backend разработчика на удалёнку", ["python", "frontend"]),
            ["python"],
        )
