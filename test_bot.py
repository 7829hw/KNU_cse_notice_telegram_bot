import unittest

from bs4 import BeautifulSoup

from bot import (
    TELEGRAM_MESSAGE_LIMIT,
    build_notice_messages,
    html_content_to_markdown_blocks,
    parse_notice_details,
    safe_filename,
)


class NoticeBotTest(unittest.TestCase):
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
          <a href="/bbs/download.php?no=0"><strong>신청서.hwp</strong></a>
        </section>
        """

        details = parse_notice_details(
            html, "https://cse.knu.ac.kr/bbs/board.php?wr_id=1"
        )

        self.assertIn("첫 번째 중요 문단입니다.\n다음 줄입니다.", details["content"])
        self.assertIn("두 번째 문단입니다.", details["content"])
        self.assertIn("설문 참여 (https://cse.knu.ac.kr/survey)", details["content"])
        markdown = "\n\n".join(details["content_markdown_blocks"])
        self.assertIn(r"*중요 문단*", markdown)
        self.assertIn(r"_두 번째 문단_", markdown)
        self.assertIn(r"[설문 참여](https://cse.knu.ac.kr/survey)", markdown)
        self.assertIn("• 첫 항목", markdown)
        self.assertIn("구분 │ 내용", markdown)
        self.assertEqual(details["attachments"][0]["name"], "신청서.hwp")
        self.assertEqual(
            details["inline_images"][0]["url"],
            "https://cse.knu.ac.kr/images/notice.png",
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

    def test_safe_filename(self):
        self.assertEqual(safe_filename('신청서:최종?.hwp'), "신청서_최종_.hwp")


if __name__ == "__main__":
    unittest.main()
