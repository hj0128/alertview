"""텔레그램 메시지 포맷 + 발송."""
from __future__ import annotations
import asyncio
import datetime
import logging
from typing import List

from telegram import Bot
from telegram.constants import ParseMode

from .adapters.base import Campaign
from . import db, config
from .matcher import matches_filter

log = logging.getLogger(__name__)


def format_campaign(c: Campaign) -> str:
    bits = []
    if c.region:
        bits.append(f"📍 {c.region}")
    if c.category:
        bits.append(f"🏷 {c.category}")
    if c.channel:
        bits.append(f"📝 {c.channel}")
    line2 = "   ".join(bits)
    stat = []
    if c.dday is not None:
        stat.append(f"⏰ D-{c.dday}")
    if c.competition is not None:
        stat.append(f"🔥 경쟁률 {c.competition}")
    if c.applicants is not None and c.recruit is not None:
        stat.append(f"👥 {c.applicants}/{c.recruit}")
    line3 = "   ".join(stat)
    out = [f"🆕 <b>새 체험단</b> · {c.site_name}", "", c.title]
    if line2:
        out.append(line2)
    if line3:
        out.append(line3)
    out.append(f"🔗 {c.url}")
    return "\n".join(out)


def _format_reminder(r: dict, dleft: int) -> str:
    dtxt = "오늘 마감" if dleft <= 0 else f"D-{dleft} (내일 마감)"
    bits = []
    if r.get("region"):
        bits.append(f"📍 {r['region']}")
    if r.get("competition") is not None:
        bits.append(f"🔥 경쟁률 {r['competition']}")
    out = [f"⏰ <b>찜한 체험단 마감임박</b> · {dtxt}", "", (r.get("title") or "").strip()]
    if bits:
        out.append("   ".join(bits))
    out.append(f"🔗 {r.get('url') or ''}")
    return "\n".join(out)


async def remind_deadlines(bot: Bot) -> int:
    """활성 사용자의 '찜한' 캠페인 중 마감 임박(D-1~D-0)인 것을 1회 리마인드."""
    if not bot:
        return 0
    today = datetime.date.today()
    sent = 0
    for chat_id in db.active_users():
        for r in db.favorites_rows(chat_id):
            dl = r.get("deadline")
            if not dl:
                continue
            try:
                dleft = (datetime.date.fromisoformat(dl) - today).days
            except (ValueError, TypeError):
                continue
            if not (0 <= dleft <= 1):
                continue
            if db.is_reminded(chat_id, r["site"], r["cid"]):
                continue
            try:
                await bot.send_message(chat_id=chat_id, text=_format_reminder(r, dleft),
                                       parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                db.mark_reminded(chat_id, r["site"], r["cid"])
                sent += 1
                await asyncio.sleep(config.SEND_GAP)
            except Exception as e:
                log.warning("리마인더 발송 실패 chat_id=%s: %s", chat_id, e)
    return sent


async def notify_new(bot: Bot, campaigns: List[Campaign]) -> int:
    if not campaigns:
        return 0
    sent = 0
    for chat_id in db.active_users():
        f = db.get_all_filters(chat_id)
        for c in campaigns:
            if matches_filter(c, f):
                try:
                    await bot.send_message(
                        chat_id=chat_id, text=format_campaign(c),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                    sent += 1
                    await asyncio.sleep(config.SEND_GAP)
                except Exception as e:
                    log.warning("send fail chat_id=%s: %s", chat_id, e)
    return sent
