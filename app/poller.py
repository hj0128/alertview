"""주기적 수집 → (페이지 단위 즉시 저장) 신규 감지 → 알림."""
from __future__ import annotations
import logging
from typing import List

import httpx

from . import db
from .adapters import active_adapters
from .adapters.base import Campaign
from .notifier import notify_new

log = logging.getLogger(__name__)


async def collect_new(demo: bool) -> List[Campaign]:
    """페이지가 들어오는 즉시 DB 저장(점진적 표시).
    첫 수집: 전부 저장하되 알림 생략, 끝까지. 이후: 새 캠페인 없는 페이지에서 조기 종료."""
    new_items: List[Campaign] = []
    async with httpx.AsyncClient(follow_redirects=True, trust_env=False) as client:
        for ad in active_adapters(demo):
            first_time = db.seen_count(ad.key) == 0
            stats = {"fresh": 0}

            def on_page(items, ad=ad, first_time=first_time, stats=stats) -> bool:
                db_new = 0
                for c in items:
                    if db.is_seen(c.site, c.cid):
                        continue
                    db.record_campaign(c)
                    stats["fresh"] += 1
                    db_new += 1
                    if not first_time:
                        new_items.append(c)
                return True if first_time else (db_new > 0)

            try:
                await ad.fetch(client, on_page=on_page)
            except Exception as e:
                log.warning("[%s] fetch 실패: %s", ad.key, e)
                continue
            log.info("[%s] 신규 %d건%s", ad.key, stats["fresh"],
                     " (첫 수집: 알림생략)" if first_time else "")
    return new_items


async def run_poll(bot, demo: bool) -> None:
    new_items = await collect_new(demo)
    if new_items:
        sent = await notify_new(bot, new_items)
        log.info("신규 %d건 → 메시지 %d건 발송", len(new_items), sent)
