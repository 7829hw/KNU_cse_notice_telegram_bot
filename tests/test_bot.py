import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import config
from database import Database


class FakeMessage:
    def __init__(self):
        self.replies = []
        self.edits = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(SimpleNamespace(text=text, reply_markup=reply_markup))


class FakeCallbackQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None):
        self.edits.append(SimpleNamespace(text=text, reply_markup=reply_markup))


def make_update(chat_id, callback_data=None):
    message = FakeMessage()
    query = FakeCallbackQuery(callback_data, message) if callback_data else None
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        callback_query=query,
    )


class BotTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.db_path = Path(self._directory.name) / "bot.db"
        self.database = Database(self.db_path)
        self.addCleanup(self.database.close)
        self.context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"database": self.database}),
            bot=AsyncMock(),
        )


class CommandTest(BotTestCase):
    async def test_start_registers_a_new_user_with_everything_on(self):
        update = make_update(1001)

        await bot.start_command(update, self.context)

        settings = self.database.get_settings(1001)
        self.assertTrue(settings["active"])
        self.assertTrue(all(settings[key] for key in config.SUBSCRIPTION_KEYS))
        self.assertIn("공지 알림을 시작합니다", update.effective_message.replies[0].text)

    async def test_start_again_keeps_the_existing_settings(self):
        await bot.start_command(make_update(1001), self.context)
        self.database.toggle_option(1001, "graduate")
        self.database.toggle_option(1001, "include_content")

        update = make_update(1001)
        await bot.start_command(update, self.context)

        settings = self.database.get_settings(1001)
        self.assertFalse(settings["graduate"])
        self.assertFalse(settings["include_content"])
        self.assertIn("다시 시작합니다", update.effective_message.replies[0].text)

    async def test_start_reactivates_a_stopped_user(self):
        await bot.start_command(make_update(1001), self.context)
        self.database.toggle_option(1001, "include_attachments")
        await bot.stop_command(make_update(1001), self.context)
        self.assertFalse(self.database.get_settings(1001)["active"])

        await bot.start_command(make_update(1001), self.context)

        settings = self.database.get_settings(1001)
        self.assertTrue(settings["active"])
        self.assertFalse(settings["include_attachments"])

    async def test_stop_keeps_the_user_and_settings(self):
        await bot.start_command(make_update(1001), self.context)

        update = make_update(1001)
        await bot.stop_command(update, self.context)

        settings = self.database.get_settings(1001)
        self.assertFalse(settings["active"])
        self.assertTrue(settings["undergraduate"])
        self.assertIn("중지", update.effective_message.replies[0].text)

    async def test_settings_shows_a_toggle_for_every_option(self):
        await bot.start_command(make_update(1001), self.context)

        update = make_update(1001)
        await bot.settings_command(update, self.context)

        reply = update.effective_message.replies[0]
        buttons = [
            button
            for row in reply.reply_markup.inline_keyboard
            for button in row
        ]
        self.assertEqual(
            [button.callback_data for button in buttons],
            [f"toggle:{key}" for key in config.SUBSCRIPTION_KEYS]
            + ["enable:all", "disable:all"],
        )
        self.assertIn("공지 알림 설정", reply.text)

    async def test_help_lists_every_command(self):
        update = make_update(1001)

        await bot.help_command(update, self.context)

        text = update.effective_message.replies[0].text
        for command in ("/start", "/settings", "/stop", "/help"):
            self.assertIn(command, text)


class CallbackTest(BotTestCase):
    async def test_each_toggle_updates_the_same_message(self):
        await bot.start_command(make_update(1001), self.context)

        for key in config.SUBSCRIPTION_KEYS:
            with self.subTest(option=key):
                update = make_update(1001, callback_data=f"toggle:{key}")
                await bot.settings_callback(update, self.context)

                self.assertFalse(self.database.get_settings(1001)[key])
                # 새 메시지가 아니라 기존 메시지를 갱신해야 합니다.
                self.assertEqual(len(update.callback_query.edits), 1)
                self.assertEqual(update.effective_message.replies, [])
                self.assertEqual(len(update.callback_query.answers), 1)

    async def test_enable_and_disable_all_buttons(self):
        await bot.start_command(make_update(1001), self.context)

        await bot.settings_callback(
            make_update(1001, callback_data="disable:all"), self.context
        )
        settings = self.database.get_settings(1001)
        self.assertFalse(any(settings[key] for key in config.SUBSCRIPTION_KEYS))

        await bot.settings_callback(
            make_update(1001, callback_data="enable:all"), self.context
        )
        settings = self.database.get_settings(1001)
        self.assertTrue(all(settings[key] for key in config.SUBSCRIPTION_KEYS))

    async def test_callback_from_an_unknown_user_is_answered(self):
        update = make_update(9999, callback_data="toggle:graduate")

        await bot.settings_callback(update, self.context)

        self.assertEqual(len(update.callback_query.answers), 1)
        self.assertEqual(update.callback_query.edits, [])

    async def test_unknown_callback_data_is_ignored(self):
        await bot.start_command(make_update(1001), self.context)
        update = make_update(1001, callback_data="toggle:active")

        await bot.settings_callback(update, self.context)

        self.assertEqual(update.callback_query.edits, [])
        self.assertEqual(len(update.callback_query.answers), 1)


class CrawlWindowTest(unittest.TestCase):
    def test_only_weekday_business_hours_are_crawled(self):
        cases = {
            datetime(2026, 9, 2, 9, 0, tzinfo=config.TIMEZONE): True,   # 수 09:00
            datetime(2026, 9, 2, 8, 0, tzinfo=config.TIMEZONE): True,   # 수 08:00
            datetime(2026, 9, 2, 19, 59, tzinfo=config.TIMEZONE): True,  # 수 19:59
            datetime(2026, 9, 2, 20, 0, tzinfo=config.TIMEZONE): False,  # 수 20:00
            datetime(2026, 9, 2, 7, 59, tzinfo=config.TIMEZONE): False,  # 수 07:59
            datetime(2026, 9, 2, 3, 0, tzinfo=config.TIMEZONE): False,   # 수 03:00
            datetime(2026, 9, 5, 10, 0, tzinfo=config.TIMEZONE): False,  # 토 10:00
            datetime(2026, 9, 6, 10, 0, tzinfo=config.TIMEZONE): False,  # 일 10:00
        }
        for now, expected in cases.items():
            with self.subTest(now=now):
                self.assertIs(config.is_within_crawl_window(now), expected)

    def test_the_job_skips_crawling_outside_the_window(self):
        async def run():
            context = SimpleNamespace(
                application=SimpleNamespace(bot_data={"database": None}),
                bot=AsyncMock(),
            )
            with (
                patch.object(bot, "run_notice_check", AsyncMock()) as check,
                patch.object(
                    bot,
                    "datetime",
                    SimpleNamespace(
                        now=lambda tz: datetime(2026, 9, 6, 10, 0, tzinfo=tz)
                    ),
                ),
            ):
                await bot.check_notices_job(context)
            check.assert_not_awaited()

        import asyncio

        asyncio.run(run())


class NoticeCheckTest(BotTestCase):
    def make_posts(self, numbers, board=None):
        board = board or config.BOARDS[0]
        return {
            board["key"]: [
                {
                    "number": number,
                    "title": f"{board['name']} {number}번 글",
                    "link": f"{board['url']}&wr_id={number}",
                    "board_key": board["key"],
                    "board_name": board["name"],
                    "board_url": board["url"],
                }
                for number in numbers
            ]
        }

    DETAILS = {
        "content": "본문",
        "content_markdown_blocks": ["본문"],
        "attachments": [],
        "inline_images": [],
    }

    async def run_check(self, posts_by_board, deliver_result=True):
        deliver = AsyncMock(return_value=deliver_result)
        with (
            patch.object(bot.crawler, "get_latest_notices", return_value=posts_by_board),
            patch.object(bot.crawler, "get_notice_details", return_value=self.DETAILS),
            patch.object(bot.Notifier, "deliver_notice", deliver),
            patch.object(bot.Notifier, "retry_pending_media", AsyncMock()),
        ):
            await bot.run_notice_check(self.context.bot, self.database)
        return deliver

    async def test_the_first_run_only_records_the_newest_notice(self):
        deliver = await self.run_check(self.make_posts([29414, 29415]))

        deliver.assert_not_awaited()
        self.assertEqual(self.database.get_last_numbers()["undergraduate"], 29415)

    async def test_new_notices_go_only_to_subscribers_of_that_board(self):
        self.database.set_last_number("undergraduate", 29415)
        self.database.ensure_user(1)
        self.database.ensure_user(2)
        self.database.toggle_option(2, "undergraduate")  # 학부 구독 해제

        deliver = await self.run_check(self.make_posts([29415, 29416]))

        deliver.assert_awaited_once()
        post, recipients = deliver.await_args.args
        self.assertEqual(post["number"], 29416)
        self.assertEqual([row["chat_id"] for row in recipients], [1])
        self.assertEqual(self.database.get_last_numbers()["undergraduate"], 29416)

    async def test_a_restart_does_not_resend_the_same_notice(self):
        self.database.set_last_number("undergraduate", 29415)
        self.database.ensure_user(1)
        posts = self.make_posts([29416])

        await self.run_check(posts)
        self.database.close()

        # 컨테이너 재시작을 흉내 내어 같은 DB 파일을 다시 엽니다.
        self.database = Database(self.db_path)
        self.addCleanup(self.database.close)
        deliver = await self.run_check(self.make_posts([29416]))

        deliver.assert_not_awaited()
        self.assertEqual(self.database.get_last_numbers()["undergraduate"], 29416)

    async def test_a_failed_delivery_keeps_the_cursor_for_a_retry(self):
        self.database.set_last_number("undergraduate", 29415)
        self.database.ensure_user(1)

        await self.run_check(self.make_posts([29416, 29417]), deliver_result=False)

        self.assertEqual(self.database.get_last_numbers()["undergraduate"], 29415)

    async def test_the_cursor_advances_even_when_nobody_subscribes(self):
        self.database.set_last_number("undergraduate", 29415)

        with patch.object(bot.crawler, "get_notice_details") as get_details:
            deliver = await self.run_check(self.make_posts([29416]))

        deliver.assert_not_awaited()
        get_details.assert_not_called()
        self.assertEqual(self.database.get_last_numbers()["undergraduate"], 29416)

    async def test_each_board_keeps_its_own_cursor(self):
        posts = {}
        for board, number in zip(config.BOARDS, (29416, 29351, 274)):
            self.database.set_last_number(board["key"], number - 1)
            posts.update(self.make_posts([number], board))
        self.database.ensure_user(1)

        deliver = await self.run_check(posts)

        self.assertEqual(deliver.await_count, 3)
        self.assertEqual(
            self.database.get_last_numbers(),
            {"undergraduate": 29416, "graduate": 29351, "undergraduate_recruitment": 274},
        )

    async def test_a_detail_failure_stops_the_board_without_losing_the_notice(self):
        self.database.set_last_number("undergraduate", 29415)
        self.database.ensure_user(1)
        deliver = AsyncMock(return_value=True)

        with (
            patch.object(
                bot.crawler,
                "get_latest_notices",
                return_value=self.make_posts([29416, 29417]),
            ),
            patch.object(
                bot.crawler, "get_notice_details", side_effect=RuntimeError("조회 실패")
            ),
            patch.object(bot.Notifier, "deliver_notice", deliver),
            patch.object(bot.Notifier, "retry_pending_media", AsyncMock()),
        ):
            await bot.run_notice_check(self.context.bot, self.database)

        deliver.assert_not_awaited()
        self.assertEqual(self.database.get_last_numbers()["undergraduate"], 29415)


if __name__ == "__main__":
    unittest.main()
