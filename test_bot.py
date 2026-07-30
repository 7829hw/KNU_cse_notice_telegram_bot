import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

import bot
from bot import (
    BOARDS,
    TELEGRAM_MESSAGE_LIMIT,
    build_notice_messages,
    enqueue_pending_media,
    extract_table_matrix,
    get_display_width,
    html_content_to_markdown_blocks,
    load_last_notice_numbers,
    parse_notice_list,
    parse_notice_details,
    safe_filename,
    save_last_notice_numbers,
)


class NoticeBotTest(unittest.TestCase):
    @staticmethod
    def make_notice_list_html(board, post_id, title, display_number="1"):
        board_table = board["url"].split("bo_table=", 1)[1].split("&", 1)[0]
        return f"""
        <div id="bo_list">
          <form id="fboardlist">
            <div class="basic_tbl_head">
              <table>
                <caption>{board["name"]} 목록</caption>
                <tbody>
                  <tr>
                    <td class="td_num2">{display_number}</td>
                    <td class="td_subject">
                      <a class="bo_cate_link" href="?sca=test">분류</a>
                      <div class="bo_tit">
                        <a href="/bbs/board.php?bo_table={board_table}&amp;wr_id={post_id}">
                          {title}
                        </a>
                      </div>
                    </td>
                    <td class="td_name">작성자</td>
                    <td class="td_datetime rep">2026-07-31</td>
                  </tr>
                  <tr>
                    <td class="td_num2">2</td>
                    <td class="td_subject">
                      <div class="bo_tit"><a href="?page=2">잘못된 링크</a></div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </form>
        </div>
        """

    def test_board_urls_match_requested_sources(self):
        self.assertEqual(
            tuple(board["url"] for board in BOARDS),
            (
                (
                    "https://computer.knu.ac.kr/bbs/board.php"
                    "?bo_table=sub6_1_a&lang=kor"
                ),
                (
                    "https://computer.knu.ac.kr/bbs/board.php"
                    "?bo_table=sub6_1_b&lang=kor"
                ),
                (
                    "https://computer.knu.ac.kr/bbs/board.php"
                    "?bo_table=sub6_3_a&lang=kor"
                ),
            ),
        )

    def test_parse_all_new_notice_boards(self):
        fixtures = (
            (BOARDS[0], 29355, "학부 공지", "6889"),
            (BOARDS[1], 29330, "대학원 공지", "1107"),
            (BOARDS[2], 267, "학부인재모집 공지", "공지"),
        )

        for board, post_id, title, display_number in fixtures:
            with self.subTest(board=board["key"]):
                posts = parse_notice_list(
                    self.make_notice_list_html(
                        board, post_id, title, display_number
                    ),
                    board,
                )

                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0]["number"], post_id)
                self.assertEqual(posts[0]["title"], title)
                self.assertEqual(posts[0]["board_key"], board["key"])
                self.assertEqual(posts[0]["board_url"], board["url"])
                self.assertIn(f"wr_id={post_id}", posts[0]["link"])

    def test_get_latest_notices_requests_every_board(self):
        class FakeResponse:
            def __init__(self, text):
                self.text = text

            def raise_for_status(self):
                return None

        responses = [
            FakeResponse(self.make_notice_list_html(board, index, board["name"]))
            for index, board in enumerate(BOARDS, start=1)
        ]

        with patch.object(bot.requests, "Session") as session_class:
            session = session_class.return_value
            session.get.side_effect = responses

            posts_by_board = bot.get_latest_notices()

        self.assertEqual(
            [call.args[0] for call in session.get.call_args_list],
            [board["url"] for board in BOARDS],
        )
        self.assertEqual(
            set(posts_by_board),
            {board["key"] for board in BOARDS},
        )

    def test_notice_details_uses_the_source_board_as_referer(self):
        class FakeResponse:
            text = '<div id="bo_v_con"><p>본문</p></div>'

            def raise_for_status(self):
                return None

        board = BOARDS[1]
        page_url = f"{board['url']}&wr_id=29330"
        with patch.object(
            bot.requests, "get", return_value=FakeResponse()
        ) as request:
            details = bot.get_notice_details(page_url, board["url"])

        self.assertEqual(details["content"], "본문")
        self.assertEqual(
            request.call_args.kwargs["headers"]["Referer"],
            board["url"],
        )

    def test_legacy_last_number_migrates_without_blocking_new_board(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "last_num.txt"
            state_path.write_text("29249", encoding="utf-8")

            last_numbers = load_last_notice_numbers(state_path)

            self.assertEqual(last_numbers["undergraduate"], 29249)
            self.assertEqual(last_numbers["graduate"], 29249)
            self.assertEqual(last_numbers["undergraduate_recruitment"], 0)

            save_last_notice_numbers(last_numbers, state_path)
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8")),
                last_numbers,
            )

    def test_each_board_advances_its_own_cursor_and_sends(self):
        initial_state = {
            "undergraduate": 29354,
            "graduate": 29329,
            "undergraduate_recruitment": 266,
        }
        latest_posts = {}
        for board, post_id in zip(BOARDS, (29355, 29330, 267)):
            latest_posts[board["key"]] = [{
                "number": post_id,
                "title": f"{board['name']} 새 글",
                "link": f"{board['url']}&wr_id={post_id}",
                "board_key": board["key"],
                "board_name": board["name"],
                "board_url": board["url"],
            }]

        details = {
            "content": "본문",
            "content_markdown_blocks": ["본문"],
            "attachments": [],
            "inline_images": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "last_num.txt"
            state_path.write_text(
                json.dumps(initial_state, ensure_ascii=False),
                encoding="utf-8",
            )

            with (
                patch.object(bot, "TELEGRAM_TOKEN", "test-token"),
                patch.object(bot, "LAST_NUM_FILE", str(state_path)),
                patch.object(bot, "update_subscribers", return_value=["123"]),
                patch.object(bot, "load_pending_media", return_value=[]),
                patch.object(bot, "save_pending_media"),
                patch.object(bot, "get_latest_notices", return_value=latest_posts),
                patch.object(bot, "get_notice_details", return_value=details),
                patch.object(
                    bot, "send_telegram_message", return_value=True
                ) as send_message,
            ):
                bot.check_new_notices()

            saved_state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            saved_state,
            {
                "undergraduate": 29355,
                "graduate": 29330,
                "undergraduate_recruitment": 267,
            },
        )
        self.assertEqual(send_message.call_count, 3)
        sent_messages = "\n".join(
            call.args[0] for call in send_message.call_args_list
        )
        self.assertIn(r"\[학부\]", sent_messages)
        self.assertIn(r"\[대학원\]", sent_messages)
        self.assertIn(r"\[학부인재모집\]", sent_messages)

    def test_legacy_cursor_is_clamped_to_each_split_board(self):
        legacy_last_number = 300
        latest_posts = {
            BOARDS[0]["key"]: [{
                "number": 301,
                "title": "학부 새 글",
                "link": f"{BOARDS[0]['url']}&wr_id=301",
                "board_key": BOARDS[0]["key"],
                "board_name": BOARDS[0]["name"],
                "board_url": BOARDS[0]["url"],
            }],
            BOARDS[1]["key"]: [{
                "number": 250,
                "title": "대학원 현재 글",
                "link": f"{BOARDS[1]['url']}&wr_id=250",
                "board_key": BOARDS[1]["key"],
                "board_name": BOARDS[1]["name"],
                "board_url": BOARDS[1]["url"],
            }],
            BOARDS[2]["key"]: [
                {
                    "number": 200,
                    "title": "고정 글",
                    "link": f"{BOARDS[2]['url']}&wr_id=200",
                    "board_key": BOARDS[2]["key"],
                    "board_name": BOARDS[2]["name"],
                    "board_url": BOARDS[2]["url"],
                },
                {
                    "number": 267,
                    "title": "현재 최신 글",
                    "link": f"{BOARDS[2]['url']}&wr_id=267",
                    "board_key": BOARDS[2]["key"],
                    "board_name": BOARDS[2]["name"],
                    "board_url": BOARDS[2]["url"],
                },
            ],
        }
        details = {
            "content": "본문",
            "content_markdown_blocks": ["본문"],
            "attachments": [],
            "inline_images": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "last_num.txt"
            state_path.write_text(
                str(legacy_last_number),
                encoding="utf-8",
            )

            with (
                patch.object(bot, "TELEGRAM_TOKEN", "test-token"),
                patch.object(bot, "LAST_NUM_FILE", str(state_path)),
                patch.object(bot, "update_subscribers", return_value=["123"]),
                patch.object(bot, "load_pending_media", return_value=[]),
                patch.object(bot, "save_pending_media"),
                patch.object(bot, "get_latest_notices", return_value=latest_posts),
                patch.object(bot, "get_notice_details", return_value=details),
                patch.object(
                    bot, "send_telegram_message", return_value=True
                ) as send_message,
            ):
                bot.check_new_notices()

            saved_state = json.loads(state_path.read_text(encoding="utf-8"))

        self.assertEqual(
            saved_state,
            {
                "undergraduate": 301,
                "graduate": 250,
                "undergraduate_recruitment": 267,
            },
        )
        self.assertEqual(send_message.call_count, 1)
        self.assertIn(r"\[학부\]", send_message.call_args.args[0])

    def test_json_cursor_is_not_moved_back_by_an_older_page(self):
        saved_numbers = {
            "undergraduate": 301,
            "graduate": 300,
            "undergraduate_recruitment": 267,
        }
        board = BOARDS[1]
        older_posts = {
            board["key"]: [{
                "number": 250,
                "title": "현재 보이는 대학원 글",
                "link": f"{board['url']}&wr_id=250",
                "board_key": board["key"],
                "board_name": board["name"],
                "board_url": board["url"],
            }],
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "last_num.txt"
            state_path.write_text(
                json.dumps(saved_numbers, ensure_ascii=False),
                encoding="utf-8",
            )

            with (
                patch.object(bot, "TELEGRAM_TOKEN", "test-token"),
                patch.object(bot, "LAST_NUM_FILE", str(state_path)),
                patch.object(bot, "update_subscribers", return_value=[]),
                patch.object(bot, "load_pending_media", return_value=[]),
                patch.object(bot, "save_pending_media"),
                patch.object(bot, "get_latest_notices", return_value=older_posts),
                patch.object(bot, "get_notice_details") as get_details,
                patch.object(bot, "send_telegram_message") as send_message,
            ):
                bot.check_new_notices()

            final_numbers = json.loads(
                state_path.read_text(encoding="utf-8")
            )

        self.assertEqual(final_numbers, saved_numbers)
        get_details.assert_not_called()
        send_message.assert_not_called()

    def test_legacy_migration_waits_for_both_notice_boards(self):
        board = BOARDS[0]
        partial_posts = {
            board["key"]: [{
                "number": 301,
                "title": "학부 새 글",
                "link": f"{board['url']}&wr_id=301",
                "board_key": board["key"],
                "board_name": board["name"],
                "board_url": board["url"],
            }],
        }

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "last_num.txt"
            state_path.write_text("300", encoding="utf-8")

            with (
                patch.object(bot, "TELEGRAM_TOKEN", "test-token"),
                patch.object(bot, "LAST_NUM_FILE", str(state_path)),
                patch.object(bot, "update_subscribers", return_value=["123"]),
                patch.object(bot, "load_pending_media", return_value=[]),
                patch.object(bot, "save_pending_media"),
                patch.object(bot, "get_latest_notices", return_value=partial_posts),
                patch.object(bot, "get_notice_details") as get_details,
                patch.object(bot, "send_telegram_message") as send_message,
            ):
                bot.check_new_notices()

            final_state = state_path.read_text(encoding="utf-8")

        self.assertEqual(final_state, "300")
        get_details.assert_not_called()
        send_message.assert_not_called()

    def test_parse_notice_details(self):
        html = """
        <div id="bo_v_con">
          <p>첫 번째 <strong>중요 문단</strong>입니다.<br>다음 줄입니다.</p>
          <p><em>두 번째 문단</em>입니다.</p>
          <a href="/survey">설문 참여</a>
          <ul><li>첫 항목</li><li>둘째 항목</li></ul>
          <table><tr><th>구분</th><th>내용</th></tr><tr><td>A</td><td>B</td></tr></table>
          <img src="/images/notice.png" alt="안내 이미지.png">
        </div>
        <section id="bo_v_file">
          <a class="view_file_download" href="/bbs/download.php?no=0">
            <strong>신청서.hwp</strong>
          </a>
        </section>
        """

        details = parse_notice_details(
            html,
            (
                "https://computer.knu.ac.kr/bbs/board.php"
                "?bo_table=sub6_1_a&wr_id=1"
            ),
        )

        self.assertIn("첫 번째 중요 문단입니다.\n다음 줄입니다.", details["content"])
        self.assertIn("두 번째 문단입니다.", details["content"])
        self.assertIn(
            "설문 참여 (https://computer.knu.ac.kr/survey)",
            details["content"],
        )
        markdown = "\n\n".join(details["content_markdown_blocks"])
        self.assertIn(r"*중요 문단*", markdown)
        self.assertIn(r"_두 번째 문단_", markdown)
        self.assertIn(
            r"[설문 참여](https://computer.knu.ac.kr/survey)",
            markdown,
        )
        self.assertIn("• 첫 항목", markdown)
        self.assertIn("```\n구분 | 내용", markdown)
        self.assertEqual(details["attachments"][0]["name"], "신청서.hwp")
        self.assertEqual(
            details["inline_images"][0]["url"],
            "https://computer.knu.ac.kr/images/notice.png",
        )

    def test_long_notice_is_split_without_losing_content(self):
        post = {
            "title": "실제 공지 제목",
            "content": "가" * (TELEGRAM_MESSAGE_LIMIT * 2),
            "link": "https://example.com/notice",
        }

        messages = build_notice_messages(post)

        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= TELEGRAM_MESSAGE_LIMIT for message in messages))
        combined = "".join(messages).replace("\n", "")
        self.assertIn("가" * (TELEGRAM_MESSAGE_LIMIT * 2), combined)
        self.assertTrue(messages[0].startswith("📢 *실제 공지 제목*"))

    def test_markdown_special_characters_are_escaped(self):
        html = """
        <div id="bo_v_con">
          <p><strong>필수!</strong> 신청기간: 6.29~6.30</p>
          <p><span style="font-weight: 700">굵게</span></p>
          <p><span style="text-decoration: underline">연속</span><span style="text-decoration: underline">밑줄</span></p>
        </div>
        """

        blocks = html_content_to_markdown_blocks(
            BeautifulSoup(html, "html.parser").select_one("#bo_v_con")
        )

        self.assertEqual(
            blocks,
            [
                r"*필수\!* 신청기간: 6\.29\~6\.30",
                r"*굵게*",
                r"__연속밑줄__",
            ],
        )

    def test_table_cell_paragraphs_stay_in_one_row(self):
        html = """
        <div id="bo_v_con">
          <table>
            <tr>
              <td><p>구분</p></td>
              <td><p>졸업기준</p><p>학점</p></td>
              <td><p>총이수</p><p>학점</p></td>
            </tr>
            <tr><td>1</td><td>140</td><td>150</td></tr>
          </table>
        </div>
        """
        content = BeautifulSoup(html, "html.parser").select_one("#bo_v_con")

        blocks = html_content_to_markdown_blocks(content)
        table = next(block for block in blocks if block.startswith("```"))
        lines = table.splitlines()

        self.assertIn("구분 | 졸업기준 학점 | 총이수 학점", lines[1])
        self.assertIn("1    | 140", lines[3])
        header_cells = lines[1].split(" | ")
        data_cells = lines[3].split(" | ")
        self.assertEqual(
            [get_display_width(cell) for cell in header_cells[:-1]],
            [get_display_width(cell) for cell in data_cells[:-1]],
        )

    def test_table_rowspan_and_colspan_are_expanded(self):
        html = """
        <table>
          <tr><th rowspan="2">구분</th><th colspan="2">점수</th></tr>
          <tr><th>중간</th><th>기말</th></tr>
          <tr><td>A</td><td>90</td><td>95</td></tr>
        </table>
        """
        table = BeautifulSoup(html, "html.parser").select_one("table")

        self.assertEqual(
            extract_table_matrix(table),
            [
                ["구분", "점수", "점수"],
                ["구분", "중간", "기말"],
                ["A", "90", "95"],
            ],
        )

    def test_safe_filename(self):
        self.assertEqual(safe_filename('신청서:최종?.hwp'), "신청서_최종_.hwp")

    def test_failed_media_is_queued_once_for_retry(self):
        pending_media = []
        image = {
            "name": "안내 이미지.png",
            "url": "https://example.com/notice.png",
        }

        enqueue_pending_media(
            pending_media, image, "https://example.com/notice", "본문 이미지"
        )
        enqueue_pending_media(
            pending_media, image, "https://example.com/notice", "본문 이미지"
        )

        self.assertEqual(len(pending_media), 1)
        self.assertEqual(pending_media[0]["file_info"], image)


if __name__ == "__main__":
    unittest.main()
