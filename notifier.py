"""사용자별 설정에 맞춰 공지를 Telegram DM으로 보냅니다."""

import asyncio
import tempfile
from collections import defaultdict

from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    InvalidToken,
    RetryAfter,
)

import crawler

# 다시 시도해도 같은 결과가 나오는, 채팅 자체를 쓸 수 없는 오류들입니다.
PERMANENT_BAD_REQUEST_HINTS = (
    "chat not found",
    "chat_id is empty",
    "user is deactivated",
    "peer_id_invalid",
    "bot was blocked",
    "bot can't initiate conversation",
    "user not found",
)

DELIVERED = "delivered"
PERMANENT = "permanent"
TRANSIENT = "transient"


def is_permanent_chat_error(error):
    """구독을 비활성화해야 하는 영구 오류인지 판단합니다."""
    if isinstance(error, (Forbidden, ChatMigrated)):
        return True
    if isinstance(error, BadRequest):
        message = str(error).lower()
        return any(hint in message for hint in PERMANENT_BAD_REQUEST_HINTS)
    return False


class Notifier:
    """공지 본문·이미지·첨부파일을 구독 설정에 맞춰 전송합니다."""

    def __init__(self, bot, database):
        self.bot = bot
        self.database = database

    # ------------------------------------------------------------------
    # 저수준 전송
    # ------------------------------------------------------------------
    async def _drop_user(self, chat_id, error):
        """차단 등 영구 오류가 난 사용자를 비활성화합니다. 데이터는 남깁니다."""
        print(f"Chat ID {chat_id} 전송 불가로 알림을 중지합니다: {error}")
        await asyncio.to_thread(self.database.deactivate_user, chat_id)

    async def send_message(self, chat_id, text):
        """MarkdownV2로 보내고, 서식 오류일 때만 일반 텍스트로 한 번 더 시도합니다."""
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return DELIVERED
        except RetryAfter as error:
            await asyncio.sleep(min(float(error.retry_after) + 1, 60))
            return await self._send_message_once(chat_id, text)
        except BadRequest as error:
            if is_permanent_chat_error(error):
                await self._drop_user(chat_id, error)
                return PERMANENT
            print(f"Chat ID {chat_id} 서식 전송 실패, 일반 텍스트로 재시도합니다: {error}")
            return await self._send_message_once(
                chat_id, crawler.markdown_v2_to_plain(text), parse_mode=None
            )
        except InvalidToken:
            raise
        except Exception as error:
            if is_permanent_chat_error(error):
                await self._drop_user(chat_id, error)
                return PERMANENT
            print(f"Chat ID {chat_id} 전송 실패: {error}")
            return TRANSIENT

    async def _send_message_once(self, chat_id, text, parse_mode=ParseMode.MARKDOWN_V2):
        """재시도 경로에서 쓰는 단발 전송입니다."""
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=True,
            )
            return DELIVERED
        except InvalidToken:
            raise
        except Exception as error:
            if is_permanent_chat_error(error):
                await self._drop_user(chat_id, error)
                return PERMANENT
            print(f"Chat ID {chat_id} 전송 실패: {error}")
            return TRANSIENT

    async def send_document(self, chat_id, path, caption):
        """이미 내려받은 파일을 한 사용자에게 전송합니다."""
        try:
            with path.open("rb") as document:
                await self.bot.send_document(
                    chat_id=chat_id,
                    document=document,
                    filename=path.name,
                    caption=caption,
                )
            return DELIVERED
        except InvalidToken:
            raise
        except Exception as error:
            if is_permanent_chat_error(error):
                await self._drop_user(chat_id, error)
                return PERMANENT
            print(f"Chat ID {chat_id} 파일 전송 실패 ({path.name}): {error}")
            return TRANSIENT

    async def send_file_to_chats(self, file_info, chat_ids, referer, label):
        """파일을 한 번만 내려받아 여러 사용자에게 보내고, 실패한 사용자만 대기열에 남깁니다."""
        if not chat_ids:
            return set()

        try:
            with tempfile.TemporaryDirectory() as directory:
                path = await asyncio.to_thread(
                    crawler.download_file, file_info, directory, referer
                )
                caption = f"📎 {label}: {file_info['name']}"
                failed = set()
                for chat_id in chat_ids:
                    outcome = await self.send_document(chat_id, path, caption)
                    if outcome == TRANSIENT:
                        failed.add(chat_id)
                return failed
        except Exception as error:
            print(f"파일 다운로드 실패 ({file_info['name']}): {error}")
            return set(chat_ids)

    # ------------------------------------------------------------------
    # 공지 전송
    # ------------------------------------------------------------------
    async def _broadcast_messages(self, messages, chat_ids):
        """같은 메시지 묶음을 여러 사용자에게 보냅니다."""
        transient_failure = False
        remaining = list(chat_ids)
        for message in messages:
            still_reachable = []
            for chat_id in remaining:
                outcome = await self.send_message(chat_id, message)
                if outcome == TRANSIENT:
                    transient_failure = True
                    still_reachable.append(chat_id)
                elif outcome == DELIVERED:
                    still_reachable.append(chat_id)
                # PERMANENT인 사용자는 남은 분할 메시지도 보내지 않습니다.
            remaining = still_reachable
        return transient_failure

    async def deliver_notice(self, post, recipients):
        """설정에 따라 본문/요약과 미디어를 나누어 전송합니다.

        일시적인 실패가 하나라도 있으면 False를 반환해 다음 실행에서
        같은 공지를 다시 시도하도록 합니다.
        """
        content_chats = [
            recipient["chat_id"]
            for recipient in recipients
            if recipient["include_content"]
        ]
        headline_chats = [
            recipient["chat_id"]
            for recipient in recipients
            if not recipient["include_content"]
        ]
        attachment_chats = [
            recipient["chat_id"]
            for recipient in recipients
            if recipient["include_attachments"]
        ]

        transient_failure = False
        # 본문 포함 여부로 나뉜 두 집단이 같은 파싱 결과를 함께 사용합니다.
        if content_chats:
            transient_failure |= await self._broadcast_messages(
                crawler.build_notice_messages(post), content_chats
            )
        if headline_chats:
            transient_failure |= await self._broadcast_messages(
                crawler.build_headline_messages(post), headline_chats
            )

        if attachment_chats:
            media = [
                ("본문 이미지", post.get("inline_images") or []),
                ("첨부파일", post.get("attachments") or []),
            ]
            for label, files in media:
                for file_info in files:
                    failed = await self.send_file_to_chats(
                        file_info, attachment_chats, post["link"], label
                    )
                    for chat_id in failed:
                        await asyncio.to_thread(
                            self.database.enqueue_pending_media,
                            chat_id,
                            file_info,
                            post["link"],
                            label,
                        )

        return not transient_failure

    async def retry_pending_media(self):
        """본문을 다시 보내지 않고, 이전에 실패한 미디어만 재전송합니다."""
        pending = await asyncio.to_thread(self.database.list_pending_media)
        if not pending:
            return

        grouped = defaultdict(list)
        for item in pending:
            key = (
                item["file_info"]["name"],
                item["file_info"]["url"],
                item["referer"],
                item["label"],
            )
            grouped[key].append(item)

        for (name, url, referer, label), items in grouped.items():
            chat_ids = [item["chat_id"] for item in items]
            failed = await self.send_file_to_chats(
                {"name": name, "url": url}, chat_ids, referer, label
            )
            for item in items:
                if item["chat_id"] in failed:
                    continue
                await asyncio.to_thread(
                    self.database.delete_pending_media, item["id"]
                )
            if len(failed) < len(chat_ids):
                print(f"대기 중 미디어 전송 완료: {name}")
