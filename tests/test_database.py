import json
import tempfile
import unittest
from pathlib import Path

from config import BOARD_KEYS, SUBSCRIPTION_KEYS
from database import Database


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.db_path = Path(self._directory.name) / "bot.db"
        self.database = Database(self.db_path)
        self.addCleanup(self.database.close)


class UserSettingsTest(DatabaseTestCase):
    def test_new_user_starts_with_every_option_enabled(self):
        settings, created = self.database.ensure_user(1001)

        self.assertTrue(created)
        self.assertTrue(settings["active"])
        for key in SUBSCRIPTION_KEYS:
            self.assertTrue(settings[key], key)

    def test_repeated_start_keeps_existing_settings(self):
        self.database.ensure_user(1001)
        self.database.toggle_option(1001, "graduate")
        self.database.toggle_option(1001, "include_attachments")

        settings, created = self.database.ensure_user(1001)

        self.assertFalse(created)
        self.assertFalse(settings["graduate"])
        self.assertFalse(settings["include_attachments"])
        self.assertTrue(settings["undergraduate"])

    def test_start_reactivates_a_stopped_user_without_resetting_options(self):
        self.database.ensure_user(1001)
        self.database.toggle_option(1001, "include_content")
        self.database.set_active(1001, False)

        settings, created = self.database.ensure_user(1001)

        self.assertFalse(created)
        self.assertTrue(settings["active"])
        self.assertFalse(settings["include_content"])

    def test_stop_keeps_the_user_and_options(self):
        self.database.ensure_user(1001)
        self.database.toggle_option(1001, "graduate")

        settings = self.database.set_active(1001, False)

        self.assertFalse(settings["active"])
        self.assertFalse(settings["graduate"])
        self.assertEqual(self.database.count_users(active_only=False), 1)

    def test_toggling_each_option_flips_only_that_option(self):
        self.database.ensure_user(1001)

        for key in SUBSCRIPTION_KEYS:
            with self.subTest(option=key):
                settings = self.database.toggle_option(1001, key)
                self.assertFalse(settings[key])
                others = [other for other in SUBSCRIPTION_KEYS if other != key]
                self.assertTrue(all(settings[other] for other in others))
                settings = self.database.toggle_option(1001, key)
                self.assertTrue(settings[key])

    def test_unknown_option_is_rejected(self):
        self.database.ensure_user(1001)

        with self.assertRaises(ValueError):
            self.database.toggle_option(1001, "active; DROP TABLE users")

    def test_enable_and_disable_all_options(self):
        self.database.ensure_user(1001)

        settings = self.database.set_all_options(1001, False)
        self.assertFalse(any(settings[key] for key in SUBSCRIPTION_KEYS))

        settings = self.database.set_all_options(1001, True)
        self.assertTrue(all(settings[key] for key in SUBSCRIPTION_KEYS))


class RecipientFilterTest(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        # 학부만 구독
        self.database.ensure_user(1)
        self.database.toggle_option(1, "graduate")
        self.database.toggle_option(1, "undergraduate_recruitment")
        # 대학원만 구독하고 본문은 받지 않음
        self.database.ensure_user(2)
        self.database.toggle_option(2, "undergraduate")
        self.database.toggle_option(2, "undergraduate_recruitment")
        self.database.toggle_option(2, "include_content")
        # 모두 구독하지만 알림 중지 상태
        self.database.ensure_user(3)
        self.database.set_active(3, False)

    def test_only_subscribers_of_the_board_receive_it(self):
        self.assertEqual(
            [row["chat_id"] for row in self.database.get_recipients("undergraduate")],
            [1],
        )
        self.assertEqual(
            [row["chat_id"] for row in self.database.get_recipients("graduate")],
            [2],
        )
        self.assertEqual(
            self.database.get_recipients("undergraduate_recruitment"),
            [],
        )

    def test_inactive_users_are_always_excluded(self):
        for board_key in BOARD_KEYS:
            with self.subTest(board=board_key):
                self.assertNotIn(
                    3,
                    [row["chat_id"] for row in self.database.get_recipients(board_key)],
                )

    def test_recipients_carry_content_and_attachment_options(self):
        recipient = self.database.get_recipients("graduate")[0]

        self.assertFalse(recipient["include_content"])
        self.assertTrue(recipient["include_attachments"])

    def test_unknown_board_has_no_recipients(self):
        self.assertEqual(self.database.get_recipients("nonexistent"), [])


class BoardStateTest(DatabaseTestCase):
    def test_last_numbers_default_to_zero_and_persist(self):
        self.assertEqual(
            self.database.get_last_numbers(),
            {key: 0 for key in BOARD_KEYS},
        )

        self.database.set_last_number("undergraduate", 29415)
        self.database.set_last_number("graduate", 29350)

        self.assertEqual(self.database.get_last_numbers()["undergraduate"], 29415)

    def test_state_survives_a_restart_of_the_process(self):
        self.database.set_last_number("undergraduate", 29415)
        self.database.close()

        with Database(self.db_path) as reopened:
            self.assertEqual(reopened.get_last_numbers()["undergraduate"], 29415)

    def test_legacy_json_state_file_is_imported_once(self):
        legacy = Path(self._directory.name) / "last_num.txt"
        legacy.write_text(
            json.dumps({
                "undergraduate": 29415,
                "graduate": 29350,
                "undergraduate_recruitment": 273,
            }),
            encoding="utf-8",
        )

        self.assertTrue(self.database.seed_board_state_from_legacy_file(legacy))
        self.assertEqual(
            self.database.get_last_numbers(),
            {
                "undergraduate": 29415,
                "graduate": 29350,
                "undergraduate_recruitment": 273,
            },
        )

        # 이미 상태가 있으면 다시 덮어쓰지 않습니다.
        self.database.set_last_number("undergraduate", 29500)
        self.assertFalse(self.database.seed_board_state_from_legacy_file(legacy))
        self.assertEqual(self.database.get_last_numbers()["undergraduate"], 29500)

    def test_legacy_single_number_applies_only_to_the_old_shared_boards(self):
        legacy = Path(self._directory.name) / "last_num.txt"
        legacy.write_text("29249", encoding="utf-8")

        self.database.seed_board_state_from_legacy_file(legacy)

        self.assertEqual(
            self.database.get_last_numbers(),
            {
                "undergraduate": 29249,
                "graduate": 29249,
                "undergraduate_recruitment": 0,
            },
        )

    def test_missing_legacy_file_is_ignored(self):
        self.assertFalse(
            self.database.seed_board_state_from_legacy_file(
                Path(self._directory.name) / "absent.txt"
            )
        )


class PendingMediaTest(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.database.ensure_user(1)
        self.file_info = {
            "name": "안내 이미지.png",
            "url": "https://example.com/notice.png",
        }

    def test_failed_media_is_queued_once_for_retry(self):
        for _ in range(2):
            self.database.enqueue_pending_media(
                1, self.file_info, "https://example.com/notice", "본문 이미지"
            )

        pending = self.database.list_pending_media()

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["file_info"], self.file_info)
        self.assertEqual(pending[0]["chat_id"], 1)

    def test_inactive_users_are_skipped_and_entries_can_be_cleared(self):
        self.database.enqueue_pending_media(
            1, self.file_info, "https://example.com/notice", "첨부파일"
        )
        self.database.set_active(1, False)
        self.assertEqual(self.database.list_pending_media(), [])

        self.database.set_active(1, True)
        pending = self.database.list_pending_media()
        self.database.delete_pending_media(pending[0]["id"])

        self.assertEqual(self.database.list_pending_media(), [])


if __name__ == "__main__":
    unittest.main()
