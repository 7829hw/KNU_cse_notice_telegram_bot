"""환경변수와 게시판 정의 등 모든 모듈이 공유하는 설정입니다."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# ================= 텔레그램 =================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_MARKDOWN_BLOCK_LIMIT = 3500

# ================= 저장 위치 =================
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "bot.db"))
# GitHub Actions 시절의 last_num.txt를 data/에 두면 최초 1회만 DB로 옮깁니다.
LEGACY_STATE_FILE = Path(os.getenv("LEGACY_STATE_FILE", DATA_DIR / "last_num.txt"))

# ================= 크롤링 주기 =================
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "600"))
# 컴퓨터학부 공지는 평일 업무시간에만 올라오므로 그 시간대에만 크롤링합니다.
CRAWL_START_HOUR = int(os.getenv("CRAWL_START_HOUR", "8"))
CRAWL_END_HOUR = int(os.getenv("CRAWL_END_HOUR", "20"))
CRAWL_WEEKDAYS = frozenset(range(0, 5))  # 월(0) ~ 금(4)

# ================= 게시판 =================
BOARDS = (
    {
        "key": "undergraduate",
        "name": "학부",
        "url": (
            "https://computer.knu.ac.kr/bbs/board.php"
            "?bo_table=sub6_1_a&lang=kor"
        ),
        "uses_legacy_cursor": True,
    },
    {
        "key": "graduate",
        "name": "대학원",
        "url": (
            "https://computer.knu.ac.kr/bbs/board.php"
            "?bo_table=sub6_1_b&lang=kor"
        ),
        "uses_legacy_cursor": True,
    },
    {
        "key": "undergraduate_recruitment",
        "name": "학부인재모집",
        "url": (
            "https://computer.knu.ac.kr/bbs/board.php"
            "?bo_table=sub6_3_a&lang=kor"
        ),
        "uses_legacy_cursor": False,
    },
)
BOARD_KEYS = tuple(board["key"] for board in BOARDS)
BOARD_NAMES = {board["key"]: board["name"] for board in BOARDS}

# 사용자가 개별로 켜고 끌 수 있는 설정 항목입니다.
CONTENT_OPTION_KEYS = ("include_content", "include_attachments")
SUBSCRIPTION_KEYS = BOARD_KEYS + CONTENT_OPTION_KEYS
OPTION_LABELS = {
    "undergraduate": "학부 공지",
    "graduate": "대학원 공지",
    "undergraduate_recruitment": "학부인재모집",
    "include_content": "본문 포함",
    "include_attachments": "첨부파일·이미지 포함",
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def is_within_crawl_window(now):
    """평일 업무시간 안에서만 크롤링하도록 현재 시각을 판정합니다."""
    return (
        now.weekday() in CRAWL_WEEKDAYS
        and CRAWL_START_HOUR <= now.hour < CRAWL_END_HOUR
    )
