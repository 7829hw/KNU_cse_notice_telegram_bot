"""Telegram 명령 처리와 주기적인 공지 확인을 담당하는 상시 실행 봇입니다."""

import asyncio
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    AIORateLimiter,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import config
import crawler
from config import BOARDS, OPTION_LABELS, SUBSCRIPTION_KEYS
from database import Database
from notifier import Notifier

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("knu-notice-bot")

HELP_TEXT = (
    "🤖 경북대학교 컴퓨터학부 공지 알림 봇\n\n"
    "/start - 알림 받기 시작 (기존 설정은 그대로 유지)\n"
    "/settings - 게시판·본문·첨부파일 알림 설정 변경\n"
    "/stop - 알림 일시 중지 (설정은 보관)\n"
    "/help - 이 도움말 보기"
)

# 같은 크롤링 작업이 겹쳐 실행되지 않도록 막습니다.
_crawl_lock = asyncio.Lock()


def get_database(context):
    return context.application.bot_data["database"]


# ----------------------------------------------------------------------
# 설정 화면
# ----------------------------------------------------------------------
def format_settings_text(settings):
    """현재 구독 상태를 사람이 읽을 수 있는 형태로 만듭니다."""
    def mark(key):
        return "✅" if settings[key] else "⬜"

    lines = ["🔔 공지 알림 설정", ""]
    lines += [
        f"{mark(board['key'])} {OPTION_LABELS[board['key']]}" for board in BOARDS
    ]
    lines.append("")
    lines += [
        f"{mark(key)} {OPTION_LABELS[key]}"
        for key in ("include_content", "include_attachments")
    ]
    lines.append("")
    if settings["active"]:
        lines.append("아래 버튼을 눌러 항목을 켜고 끌 수 있습니다.")
    else:
        lines.append("현재 알림이 중지되어 있습니다. /start 로 다시 시작할 수 있습니다.")
    return "\n".join(lines)


def build_settings_keyboard(settings):
    """항목별 토글과 전체 켜기/끄기 버튼을 만듭니다."""
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅' if settings[key] else '⬜'} {OPTION_LABELS[key]}",
                callback_data=f"toggle:{key}",
            )
        ]
        for key in SUBSCRIPTION_KEYS
    ]
    rows.append([
        InlineKeyboardButton("✅ 전체 활성화", callback_data="enable:all"),
        InlineKeyboardButton("🔕 전체 비활성화", callback_data="disable:all"),
    ])
    return InlineKeyboardMarkup(rows)


# ----------------------------------------------------------------------
# 명령 처리
# ----------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """신규 사용자는 기본값으로 등록하고, 기존 사용자는 설정을 지킨 채 재개합니다."""
    chat_id = update.effective_chat.id
    settings, created = await asyncio.to_thread(
        get_database(context).ensure_user, chat_id
    )

    if created:
        message = (
            "✅ 경북대학교 컴퓨터학부 공지 알림을 시작합니다.\n\n"
            "현재 모든 공지를 받고 있습니다.\n\n"
            "⚙️ /settings 명령에서 알림 설정을 변경할 수 있습니다."
        )
    else:
        enabled = [
            OPTION_LABELS[board["key"]]
            for board in BOARDS
            if settings[board["key"]]
        ]
        summary = ", ".join(enabled) if enabled else "없음 (모든 게시판이 꺼져 있습니다)"
        message = (
            "✅ 공지 알림을 다시 시작합니다.\n\n"
            f"기존 설정을 그대로 사용합니다.\n구독 중인 게시판: {summary}\n\n"
            "⚙️ /settings 명령에서 알림 설정을 변경할 수 있습니다."
        )
    await update.effective_message.reply_text(message)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """인라인 키보드 설정 화면을 보여 줍니다."""
    chat_id = update.effective_chat.id
    database = get_database(context)
    settings = await asyncio.to_thread(database.get_settings, chat_id)
    if settings is None:
        settings, _ = await asyncio.to_thread(database.ensure_user, chat_id)

    await update.effective_message.reply_text(
        format_settings_text(settings),
        reply_markup=build_settings_keyboard(settings),
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """설정을 지우지 않고 수신만 중지합니다."""
    chat_id = update.effective_chat.id
    database = get_database(context)
    settings = await asyncio.to_thread(database.get_settings, chat_id)
    if settings is not None:
        await asyncio.to_thread(database.set_active, chat_id, False)

    await update.effective_message.reply_text(
        "🔕 공지 알림을 중지했습니다.\n\n"
        "설정은 그대로 보관되며, /start 를 실행하면 이전 설정 그대로 다시 받을 수 있습니다."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT)


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """설정 버튼을 처리하고 새 메시지 대신 기존 메시지를 갱신합니다."""
    query = update.callback_query
    chat_id = update.effective_chat.id
    database = get_database(context)

    settings = await asyncio.to_thread(database.get_settings, chat_id)
    if settings is None:
        await query.answer("먼저 /start 를 실행해 주세요.", show_alert=True)
        return

    action, _, value = (query.data or "").partition(":")
    if action == "toggle" and value in SUBSCRIPTION_KEYS:
        settings = await asyncio.to_thread(database.toggle_option, chat_id, value)
        notice = f"{OPTION_LABELS[value]}: {'켜짐' if settings[value] else '꺼짐'}"
    elif action in {"enable", "disable"} and value == "all":
        settings = await asyncio.to_thread(
            database.set_all_options, chat_id, action == "enable"
        )
        notice = "모든 항목을 켰습니다." if action == "enable" else "모든 항목을 껐습니다."
    else:
        await query.answer("알 수 없는 설정입니다.")
        return

    await query.answer(notice)
    try:
        await query.edit_message_text(
            format_settings_text(settings),
            reply_markup=build_settings_keyboard(settings),
        )
    except BadRequest as error:
        # 같은 내용으로의 수정은 텔레그램이 거부하므로 무시해도 됩니다.
        if "not modified" not in str(error).lower():
            raise


# ----------------------------------------------------------------------
# 공지 확인
# ----------------------------------------------------------------------
async def run_notice_check(bot, database):
    """새 공지를 찾아 게시판을 구독한 사용자에게만 전송합니다."""
    notifier = Notifier(bot, database)
    await notifier.retry_pending_media()

    last_numbers = await asyncio.to_thread(database.get_last_numbers)
    posts_by_board = await asyncio.to_thread(crawler.get_latest_notices)
    if not posts_by_board:
        logger.warning("게시글을 불러오지 못했습니다.")
        return

    for board in BOARDS:
        board_key = board["key"]
        if board_key not in posts_by_board:
            continue

        posts = sorted(posts_by_board[board_key], key=lambda post: post["number"])
        last_number = last_numbers.get(board_key, 0)
        if last_number == 0:
            # 첫 실행에서 과거 공지를 한꺼번에 보내지 않도록 현재 최신 글부터 시작합니다.
            newest = max(post["number"] for post in posts)
            await asyncio.to_thread(database.set_last_number, board_key, newest)
            logger.info(
                "%s 첫 실행: %d번 글 이후부터 알립니다.", board["name"], newest
            )
            continue

        recipients = await asyncio.to_thread(database.get_recipients, board_key)
        new_last_number = last_number
        for post in posts:
            if post["number"] <= last_number:
                continue

            if recipients:
                try:
                    post.update(
                        await asyncio.to_thread(
                            crawler.get_notice_details,
                            post["link"],
                            post["board_url"],
                        )
                    )
                except Exception as error:
                    # 건너뛰면 실패한 공지를 영영 놓치므로 다음 실행에서 재시도합니다.
                    logger.warning(
                        "%s %d번 글 상세 내용 조회 실패: %s",
                        board["name"],
                        post["number"],
                        error,
                    )
                    break

                if not await notifier.deliver_notice(post, recipients):
                    logger.warning(
                        "%s %d번 글 전송 실패, 다음 실행에서 재시도합니다.",
                        board["name"],
                        post["number"],
                    )
                    break

                logger.info(
                    "알림 전송 완료: %s %d번 글 (%d명)",
                    board["name"],
                    post["number"],
                    len(recipients),
                )

            new_last_number = post["number"]
            # 한 건씩 저장해 두면 중간에 컨테이너가 멈춰도 다시 보내지 않습니다.
            await asyncio.to_thread(
                database.set_last_number, board_key, new_last_number
            )

        if new_last_number == last_number:
            logger.info(
                "%s 새로운 공지사항이 없습니다. (마지막 글 번호: %d)",
                board["name"],
                last_number,
            )


async def check_notices_job(context: ContextTypes.DEFAULT_TYPE):
    """평일 업무시간에만, 그리고 한 번에 하나씩만 크롤링합니다."""
    now = datetime.now(config.TIMEZONE)
    if not config.is_within_crawl_window(now):
        return
    if _crawl_lock.locked():
        logger.info("이전 공지 확인이 아직 끝나지 않아 이번 실행은 건너뜁니다.")
        return

    async with _crawl_lock:
        try:
            await run_notice_check(context.bot, get_database(context))
        except Exception:
            # 한 번의 오류로 스케줄러가 멈추지 않도록 여기서 막습니다.
            logger.exception("공지 확인 중 오류가 발생했습니다.")


# ----------------------------------------------------------------------
# 애플리케이션
# ----------------------------------------------------------------------
async def on_shutdown(application):
    """종료 시 SQLite 연결을 정상적으로 닫습니다."""
    database = application.bot_data.get("database")
    if database is not None:
        await asyncio.to_thread(database.close)
        logger.info("데이터베이스 연결을 닫았습니다.")


def build_application(database):
    application = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        # 구독자가 늘어도 텔레그램 전송 제한에 걸리지 않도록 속도를 조절합니다.
        .rate_limiter(AIORateLimiter())
        .post_shutdown(on_shutdown)
        .build()
    )
    application.bot_data["database"] = database

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(settings_callback))

    application.job_queue.run_repeating(
        check_notices_job,
        interval=config.CHECK_INTERVAL_SECONDS,
        first=10,
        name="check-notices",
    )
    return application


def main():
    if not config.TELEGRAM_TOKEN:
        raise SystemExit("오류: 환경변수에 TELEGRAM_TOKEN이 설정되지 않았습니다.")

    database = Database(config.DB_PATH)
    database.seed_board_state_from_legacy_file(config.LEGACY_STATE_FILE)

    logger.info(
        "봇을 시작합니다. 크롤링 주기 %d초, %s 평일 %02d:00~%02d:00",
        config.CHECK_INTERVAL_SECONDS,
        config.TIMEZONE,
        config.CRAWL_START_HOUR,
        config.CRAWL_END_HOUR,
    )
    build_application(database).run_polling(
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
