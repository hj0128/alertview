"""텔레그램 봇 명령어 핸들러."""
from __future__ import annotations
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from . import db
from .adapters import ALL_ADAPTERS

log = logging.getLogger(__name__)

HELP = (
    "<b>체험단 알림 봇</b>\n"
    "새 체험단 캠페인이 뜨면 내 조건에 맞을 때 바로 알려드려요.\n\n"
    "<b>명령어</b>\n"
    "/add 키워드 — 키워드 추가 (예: /add 강남)\n"
    "/region 지역 — 지역 추가 (예: /region 서울)\n"
    "/del 값 — 키워드·지역 삭제\n"
    "/list — 내 설정 보기\n"
    "/clear — 필터 전체 삭제\n"
    "/sites — 감시 중인 사이트\n"
    "/stop — 알림 끄기  /resume — 다시 켜기\n\n"
    "💡 필터를 하나도 안 넣으면 <b>모든 신규 캠페인</b>을 받습니다.\n"
    "키워드+지역을 같이 넣으면 둘 다 맞아야 알림이 옵니다."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_chat.id)
    await update.message.reply_text(
        "✅ 가입 완료! 이제 새 체험단이 뜨면 알려드릴게요.\n\n" + HELP,
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_chat.id)
    val = " ".join(ctx.args).strip()
    if not val:
        await update.message.reply_text("사용법: /add 키워드  (예: /add 오마카세)")
        return
    ok = db.add_filter(update.effective_chat.id, "keyword", val)
    await update.message.reply_text(
        f"{'➕ 키워드 추가: ' if ok else '이미 있는 키워드예요: '}{val}"
    )


async def cmd_region(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_chat.id)
    val = " ".join(ctx.args).strip()
    if not val:
        await update.message.reply_text("사용법: /region 지역  (예: /region 서울)")
        return
    ok = db.add_filter(update.effective_chat.id, "region", val)
    await update.message.reply_text(
        f"{'📍 지역 추가: ' if ok else '이미 있는 지역이에요: '}{val}"
    )


async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    val = " ".join(ctx.args).strip()
    if not val:
        await update.message.reply_text("사용법: /del 값  (키워드 또는 지역)")
        return
    cid = update.effective_chat.id
    removed = db.remove_filter(cid, "keyword", val) or db.remove_filter(cid, "region", val)
    await update.message.reply_text(
        f"🗑 삭제: {val}" if removed else f"그런 필터가 없어요: {val}"
    )


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    f = db.get_all_filters(update.effective_chat.id)
    msg = "<b>내 설정</b>\n"
    msg += f"키워드: {', '.join(f['keywords']) or '(없음)'}\n"
    msg += f"카테고리: {', '.join(f['categories']) or '(없음)'}\n"
    msg += f"채널: {', '.join(f['channels']) or '(없음)'}\n"
    msg += f"지역: {', '.join(f['regions']) or '(없음)'}\n"
    msg += f"경쟁률 ≤ {f['max_competition'] if f['max_competition'] is not None else '제한없음'}\n"
    msg += f"마감일 ≤ {f['max_dday'] if f['max_dday'] is not None else '제한없음'}"
    if not any([f["keywords"], f["regions"], f["categories"], f["channels"], f["max_competition"], f["max_dday"]]):
        msg += "\n\n→ 현재 <b>모든 신규 캠페인</b>을 받습니다."
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.clear_filters(update.effective_chat.id)
    await update.message.reply_text("🧹 필터를 모두 비웠어요. (모든 신규 캠페인 수신)")


async def cmd_sites(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["<b>감시 사이트</b>"]
    for a in ALL_ADAPTERS:
        lines.append(f"{'🟢' if a.enabled else '⚪️'} {a.name}{'' if a.enabled else ' (준비중)'}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.set_active(update.effective_chat.id, False)
    await update.message.reply_text("🔕 알림을 껐어요. /resume 으로 다시 켤 수 있어요.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db.upsert_user(update.effective_chat.id)
    await update.message.reply_text("🔔 알림을 다시 켰어요.")
