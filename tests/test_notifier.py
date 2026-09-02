import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, NetworkError, TimedOut

import notifier as notifier_module
from database import Database
from notifier import Notifier


def make_post(board_key="undergraduate", board_name="학부", number=29416):
    return {
        "number": number,
        "title": "테스트 공지",
        "link": f"https://computer.knu.ac.kr/bbs/board.php?wr_id={number}",
        "board_key": board_key,
        "board_name": board_name,
        "board_url": "https://computer.knu.ac.kr/bbs/board.php?bo_table=sub6_1_a",
        "content": "본문 내용",
        "content_markdown_blocks": ["본문 내용"],
        "attachments": [{"name": "신청서.hwp", "url": "https://example.com/a.hwp"}],
        "inline_images": [{"name": "img.png", "url": "https://example.com/i.png"}],
    }


class NotifierTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.database = Database(Path(self._directory.name) / "bot.db")
        self.addCleanup(self.database.close)
        self.bot = AsyncMock()
        self.notifier = Notifier(self.bot, self.database)

    def sent_texts(self):
        return [call.kwargs["text"] for call in self.bot.send_message.await_args_list]

    def recipient(self, chat_id, include_content=True, include_attachments=True):
        self.database.ensure_user(chat_id)
        return {
            "chat_id": chat_id,
            "include_content": include_content,
            "include_attachments": include_attachments,
        }


class DeliverNoticeTest(NotifierTestCase):
    async def test_content_subscribers_receive_the_body(self):
        recipients = [self.recipient(1)]

        with patch.object(self.notifier, "send_file_to_chats", AsyncMock(return_value=set())):
            delivered = await self.notifier.deliver_notice(make_post(), recipients)

        self.assertTrue(delivered)
        joined = "\n".join(self.sent_texts())
        self.assertIn("본문 내용", joined)
        self.assertIn("원문 보기", joined)

    async def test_body_off_subscribers_only_receive_board_title_and_link(self):
        recipients = [self.recipient(2, include_content=False)]

        with patch.object(self.notifier, "send_file_to_chats", AsyncMock(return_value=set())):
            await self.notifier.deliver_notice(make_post(), recipients)

        self.assertEqual(len(self.sent_texts()), 1)
        message = self.sent_texts()[0]
        self.assertIn(r"\[학부\] 테스트 공지", message)
        self.assertIn("원문 보기", message)
        self.assertNotIn("본문 내용", message)

    async def test_the_body_is_parsed_once_for_both_groups(self):
        recipients = [
            self.recipient(1, include_content=True),
            self.recipient(2, include_content=False),
        ]

        with (
            patch.object(self.notifier, "send_file_to_chats", AsyncMock(return_value=set())),
            patch.object(
                notifier_module.crawler,
                "build_notice_messages",
                wraps=notifier_module.crawler.build_notice_messages,
            ) as build_messages,
        ):
            await self.notifier.deliver_notice(make_post(), recipients)

        self.assertEqual(build_messages.call_count, 1)

    async def test_attachments_go_only_to_subscribers_who_want_them(self):
        recipients = [
            self.recipient(1, include_attachments=True),
            self.recipient(2, include_attachments=False),
        ]

        with patch.object(
            self.notifier, "send_file_to_chats", AsyncMock(return_value=set())
        ) as send_file:
            await self.notifier.deliver_notice(make_post(), recipients)

        self.assertEqual(send_file.await_count, 2)  # 본문 이미지 1개 + 첨부파일 1개
        for call in send_file.await_args_list:
            self.assertEqual(call.args[1], [1])

    async def test_no_file_is_sent_when_nobody_wants_attachments(self):
        recipients = [self.recipient(1, include_attachments=False)]

        with patch.object(
            self.notifier, "send_file_to_chats", AsyncMock(return_value=set())
        ) as send_file:
            await self.notifier.deliver_notice(make_post(), recipients)

        send_file.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()

    async def test_failed_media_is_queued_for_the_failing_user_only(self):
        recipients = [self.recipient(1), self.recipient(2)]

        with patch.object(
            self.notifier, "send_file_to_chats", AsyncMock(return_value={2})
        ):
            delivered = await self.notifier.deliver_notice(make_post(), recipients)

        pending = self.database.list_pending_media()
        self.assertTrue(delivered)  # 미디어 실패는 공지 커서를 막지 않습니다.
        self.assertEqual({item["chat_id"] for item in pending}, {2})


class DeliveryFailureTest(NotifierTestCase):
    async def test_blocked_user_is_deactivated_and_does_not_block_the_cursor(self):
        recipients = [self.recipient(1)]
        self.bot.send_message.side_effect = Forbidden("Forbidden: bot was blocked by the user")

        with patch.object(self.notifier, "send_file_to_chats", AsyncMock(return_value=set())):
            delivered = await self.notifier.deliver_notice(make_post(), recipients)

        self.assertTrue(delivered)
        self.assertFalse(self.database.get_settings(1)["active"])

    async def test_temporary_failures_keep_the_user_active_and_retry_later(self):
        recipients = [self.recipient(1)]
        self.bot.send_message.side_effect = NetworkError("연결 실패")

        with patch.object(self.notifier, "send_file_to_chats", AsyncMock(return_value=set())):
            delivered = await self.notifier.deliver_notice(make_post(), recipients)

        self.assertFalse(delivered)
        self.assertTrue(self.database.get_settings(1)["active"])

    async def test_timeout_does_not_deactivate_the_user(self):
        self.recipient(1)
        self.bot.send_message.side_effect = TimedOut()

        outcome = await self.notifier.send_message(1, "안녕하세요")

        self.assertEqual(outcome, notifier_module.TRANSIENT)
        self.assertTrue(self.database.get_settings(1)["active"])

    async def test_a_blocked_user_stops_receiving_the_remaining_parts(self):
        self.recipient(1)
        self.bot.send_message.side_effect = Forbidden("Forbidden: bot was blocked by the user")

        await self.notifier._broadcast_messages(["첫 번째", "두 번째", "세 번째"], [1])

        self.assertEqual(self.bot.send_message.await_count, 1)


class PendingMediaRetryTest(NotifierTestCase):
    async def test_pending_media_is_resent_without_the_body(self):
        self.recipient(1)
        file_info = {"name": "img.png", "url": "https://example.com/i.png"}
        self.database.enqueue_pending_media(
            1, file_info, "https://example.com/notice", "본문 이미지"
        )

        with patch.object(
            self.notifier, "send_file_to_chats", AsyncMock(return_value=set())
        ) as send_file:
            await self.notifier.retry_pending_media()

        send_file.assert_awaited_once()
        self.assertEqual(self.database.list_pending_media(), [])
        self.bot.send_message.assert_not_awaited()

    async def test_still_failing_media_stays_in_the_queue(self):
        self.recipient(1)
        self.database.enqueue_pending_media(
            1,
            {"name": "img.png", "url": "https://example.com/i.png"},
            "https://example.com/notice",
            "본문 이미지",
        )

        with patch.object(self.notifier, "send_file_to_chats", AsyncMock(return_value={1})):
            await self.notifier.retry_pending_media()

        self.assertEqual(len(self.database.list_pending_media()), 1)

    async def test_a_download_failure_queues_every_recipient(self):
        self.recipient(1)
        self.recipient(2)

        with patch.object(
            notifier_module.crawler,
            "download_file",
            side_effect=RuntimeError("다운로드 실패"),
        ):
            failed = await self.notifier.send_file_to_chats(
                {"name": "img.png", "url": "https://example.com/i.png"},
                [1, 2],
                "https://example.com/notice",
                "본문 이미지",
            )

        self.assertEqual(failed, {1, 2})
        self.bot.send_document.assert_not_awaited()

    async def test_a_file_is_downloaded_once_for_every_recipient(self):
        self.recipient(1)
        self.recipient(2)

        def fake_download(file_info, directory, referer):
            path = Path(directory) / file_info["name"]
            path.write_bytes(b"data")
            return path

        with patch.object(
            notifier_module.crawler, "download_file", side_effect=fake_download
        ) as download:
            failed = await self.notifier.send_file_to_chats(
                {"name": "img.png", "url": "https://example.com/i.png"},
                [1, 2],
                "https://example.com/notice",
                "본문 이미지",
            )

        self.assertEqual(failed, set())
        self.assertEqual(download.call_count, 1)
        self.assertEqual(self.bot.send_document.await_count, 2)


class PermanentErrorTest(unittest.TestCase):
    def test_permanent_and_temporary_errors_are_told_apart(self):
        from telegram.error import BadRequest

        self.assertTrue(
            notifier_module.is_permanent_chat_error(Forbidden("bot was blocked by the user"))
        )
        self.assertTrue(
            notifier_module.is_permanent_chat_error(BadRequest("Chat not found"))
        )
        self.assertFalse(
            notifier_module.is_permanent_chat_error(BadRequest("Can't parse entities"))
        )
        self.assertFalse(notifier_module.is_permanent_chat_error(NetworkError("일시 오류")))
        self.assertFalse(notifier_module.is_permanent_chat_error(TimedOut()))


if __name__ == "__main__":
    unittest.main()
