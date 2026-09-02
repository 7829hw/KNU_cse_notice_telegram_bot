"""사용자 구독 설정과 공지 진행 상태를 SQLite에 보관합니다."""

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import BOARD_KEYS, BOARDS, SUBSCRIPTION_KEYS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id    INTEGER PRIMARY KEY,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id                   INTEGER PRIMARY KEY
        REFERENCES users(chat_id) ON DELETE CASCADE,
    undergraduate             INTEGER NOT NULL DEFAULT 1,
    graduate                  INTEGER NOT NULL DEFAULT 1,
    undergraduate_recruitment INTEGER NOT NULL DEFAULT 1,
    include_content           INTEGER NOT NULL DEFAULT 1,
    include_attachments       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS board_state (
    board_key   TEXT PRIMARY KEY,
    last_number INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_media (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
    file_name  TEXT    NOT NULL,
    file_url   TEXT    NOT NULL,
    referer    TEXT    NOT NULL,
    label      TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE(chat_id, file_url, referer, label)
);
"""


def utc_now():
    """저장용 UTC 시각 문자열을 만듭니다."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """봇 프로세스 전체가 공유하는 SQLite 연결입니다."""

    def __init__(self, path):
        self.path = Path(path)
        if self.path.parent != Path(""):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # JobQueue 스레드와 asyncio.to_thread 워커가 같은 연결을 쓰므로
        # 연결 공유를 허용하고 모든 접근을 락으로 직렬화합니다.
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            # WAL은 쓰기 도중 컨테이너가 종료돼도 DB가 손상되지 않게 해 줍니다.
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(SCHEMA)
            self._connection.commit()

    def close(self):
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exception):
        self.close()

    # ------------------------------------------------------------------
    # 사용자와 구독 설정
    # ------------------------------------------------------------------
    def ensure_user(self, chat_id):
        """/start 처리용. 신규 사용자는 기본값으로 만들고, 기존 사용자는 설정을 지킵니다."""
        with self._lock, self._connection as connection:
            existed = connection.execute(
                "SELECT 1 FROM users WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if existed:
                # 기존 사용자는 개인 설정을 그대로 두고 수신만 다시 켭니다.
                connection.execute(
                    "UPDATE users SET active = 1 WHERE chat_id = ?", (chat_id,)
                )
            else:
                connection.execute(
                    "INSERT INTO users (chat_id, active, created_at) VALUES (?, 1, ?)",
                    (chat_id, utc_now()),
                )
            # 예전 버전에서 만들어진 사용자에게 설정 행이 없을 수도 있습니다.
            connection.execute(
                "INSERT OR IGNORE INTO subscriptions (chat_id) VALUES (?)",
                (chat_id,),
            )
        return self.get_settings(chat_id), not existed

    def get_settings(self, chat_id):
        """사용자의 활성 여부와 모든 구독 항목을 하나의 dict로 반환합니다."""
        with self._lock:
            row = self._connection.execute(
                """
                SELECT users.chat_id, users.active, users.created_at,
                       subscriptions.undergraduate,
                       subscriptions.graduate,
                       subscriptions.undergraduate_recruitment,
                       subscriptions.include_content,
                       subscriptions.include_attachments
                  FROM users
                  JOIN subscriptions USING (chat_id)
                 WHERE users.chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        if row is None:
            return None
        settings = {"chat_id": row["chat_id"], "created_at": row["created_at"]}
        settings["active"] = bool(row["active"])
        for key in SUBSCRIPTION_KEYS:
            settings[key] = bool(row[key])
        return settings

    def toggle_option(self, chat_id, option):
        """게시판 또는 본문/첨부 설정을 반대 값으로 바꾸고 최신 설정을 돌려줍니다."""
        if option not in SUBSCRIPTION_KEYS:
            raise ValueError(f"알 수 없는 설정 항목입니다: {option}")
        with self._lock, self._connection as connection:
            connection.execute(
                f"UPDATE subscriptions SET {option} = 1 - {option} WHERE chat_id = ?",
                (chat_id,),
            )
        return self.get_settings(chat_id)

    def set_all_options(self, chat_id, value):
        """설정 화면의 전체 활성화/비활성화 버튼을 처리합니다."""
        assignments = ", ".join(f"{key} = ?" for key in SUBSCRIPTION_KEYS)
        with self._lock, self._connection as connection:
            connection.execute(
                f"UPDATE subscriptions SET {assignments} WHERE chat_id = ?",
                (*[int(bool(value))] * len(SUBSCRIPTION_KEYS), chat_id),
            )
        return self.get_settings(chat_id)

    def set_active(self, chat_id, active):
        """구독 설정은 남긴 채 수신 여부만 바꿉니다."""
        with self._lock, self._connection as connection:
            connection.execute(
                "UPDATE users SET active = ? WHERE chat_id = ?",
                (int(bool(active)), chat_id),
            )
        return self.get_settings(chat_id)

    def deactivate_user(self, chat_id):
        """차단 등 영구 오류가 확인된 사용자를 조용히 비활성화합니다."""
        return self.set_active(chat_id, False)

    def get_recipients(self, board_key):
        """해당 게시판을 구독 중인 활성 사용자만 가져옵니다."""
        if board_key not in BOARD_KEYS:
            return []
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT users.chat_id,
                       subscriptions.include_content,
                       subscriptions.include_attachments
                  FROM users
                  JOIN subscriptions USING (chat_id)
                 WHERE users.active = 1 AND subscriptions.{board_key} = 1
                 ORDER BY users.chat_id
                """
            ).fetchall()
        return [
            {
                "chat_id": row["chat_id"],
                "include_content": bool(row["include_content"]),
                "include_attachments": bool(row["include_attachments"]),
            }
            for row in rows
        ]

    def count_users(self, active_only=True):
        query = "SELECT COUNT(*) FROM users"
        if active_only:
            query += " WHERE active = 1"
        with self._lock:
            return self._connection.execute(query).fetchone()[0]

    # ------------------------------------------------------------------
    # 게시판 진행 상태
    # ------------------------------------------------------------------
    def get_last_numbers(self):
        """게시판별 마지막 처리 글 번호를 반환합니다."""
        last_numbers = {key: 0 for key in BOARD_KEYS}
        with self._lock:
            rows = self._connection.execute(
                "SELECT board_key, last_number FROM board_state"
            ).fetchall()
        for row in rows:
            if row["board_key"] in last_numbers:
                last_numbers[row["board_key"]] = max(0, int(row["last_number"]))
        return last_numbers

    def set_last_number(self, board_key, last_number):
        """다음 실행에서 같은 공지를 다시 보내지 않도록 커서를 저장합니다."""
        with self._lock, self._connection as connection:
            connection.execute(
                """
                INSERT INTO board_state (board_key, last_number) VALUES (?, ?)
                ON CONFLICT(board_key) DO UPDATE SET last_number = excluded.last_number
                """,
                (board_key, max(0, int(last_number))),
            )

    def has_board_state(self):
        with self._lock:
            return (
                self._connection.execute(
                    "SELECT COUNT(*) FROM board_state"
                ).fetchone()[0]
                > 0
            )

    def seed_board_state_from_legacy_file(self, path):
        """GitHub Actions 시절의 last_num.txt를 최초 1회만 DB로 옮깁니다."""
        path = Path(path)
        if self.has_board_state() or not path.exists():
            return False

        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            print(f"기존 상태 파일을 읽지 못했습니다: {error}")
            return False

        if content.isdigit():
            # 게시판이 나뉘기 전의 단일 번호는 그 시절 커서를 쓰던 게시판에만 적용합니다.
            legacy_number = int(content)
            saved_numbers = {
                board["key"]: legacy_number if board["uses_legacy_cursor"] else 0
                for board in BOARDS
            }
        else:
            try:
                saved_numbers = json.loads(content)
            except json.JSONDecodeError as error:
                print(f"기존 상태 파일 형식이 올바르지 않습니다: {error}")
                return False
            if not isinstance(saved_numbers, dict):
                print("기존 상태 파일이 게시판별 객체 형식이 아닙니다.")
                return False

        for board_key in BOARD_KEYS:
            try:
                last_number = max(0, int(saved_numbers.get(board_key, 0)))
            except (TypeError, ValueError):
                last_number = 0
            self.set_last_number(board_key, last_number)
        print(f"기존 공지 상태를 {path}에서 가져왔습니다.")
        return True

    # ------------------------------------------------------------------
    # 재전송 대기 미디어
    # ------------------------------------------------------------------
    def enqueue_pending_media(self, chat_id, file_info, referer, label):
        """전송에 실패한 이미지·첨부파일을 사용자별로 한 번만 기록합니다."""
        with self._lock, self._connection as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO pending_media
                    (chat_id, file_name, file_url, referer, label, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    file_info["name"],
                    file_info["url"],
                    referer,
                    label,
                    utc_now(),
                ),
            )

    def list_pending_media(self):
        """활성 사용자에게 남은 재전송 대기 항목만 가져옵니다."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT pending_media.*
                  FROM pending_media
                  JOIN users USING (chat_id)
                 WHERE users.active = 1
                 ORDER BY pending_media.id
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "chat_id": row["chat_id"],
                "file_info": {"name": row["file_name"], "url": row["file_url"]},
                "referer": row["referer"],
                "label": row["label"],
            }
            for row in rows
        ]

    def delete_pending_media(self, pending_id):
        with self._lock, self._connection as connection:
            connection.execute(
                "DELETE FROM pending_media WHERE id = ?", (pending_id,)
            )
