"""텔레그램 메시지 포맷 + 발송."""
from __future__ import annotations
import asyncio
import logging
from typing import List

from telegram import Bot
from telegram.constants import ParseMode

from .adapters.base import Campaign
from . import db, config
from .matcher import matches

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


async def notify_new(bot: Bot, campaigns: List[Campaign]) -> int:
    if not campaigns:
        return 0
    sent = 0
    for chat_id in db.active_users():
        f = db.get_all_filters(chat_id)
        for c in campaigns:
            if matches(c, f["keywords"], f["regions"], f["categories"], f["channels"],
                       f["max_competition"], f["max_dday"]):
                try:
                    await bot.send_message(
                        chat_id=chat_id, text=format_campaign(c),
                        parse_mode=ParseMode.HTML, disable_web_page_preview=False)
                    sent += 1
                    await asyncio.sleep(config.SEND_GAP)
                except Exception as e:
                    log.warning("send fail chat_id=%s: %s", chat_id, e)
    return sent
