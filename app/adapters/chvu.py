"""체험뷰(chvu.co.kr) 어댑터. Next.js SPA 지만 데이터는 깔끔한 JSON API 로 받는다(브라우저 불필요).

목록: GET /v2/campaigns?category={search|ad}&sort=-created&page=N&count=100
  → {"data":[{campaignId,title,subtitle,channel,activity,contentType,
              reviewerLimit,currentApplicants,closeAt(ms),mainImg,status,isAdMark}, ...]}
created 내림차순(최신순)이라 활성(마감 전) 캠페인은 앞쪽에 몰려 있다. 활성만 모으고
'활성 0인 페이지'를 만나면 그 카테고리는 더 안 판다(뒤는 전부 마감 지난 것).
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time
from typing import List, Optional

import httpx

from .. import config
from .base import BaseAdapter, Campaign, UA

BASE = "https://chvu.co.kr"
LIST = BASE + "/v2/campaigns?category={cat}&searchQuery=&sort=-created&page={page}&count={count}"
CATS = ["search", "ad"]
COUNT = 100
MAX_PAGES = 30
# channel(플랫폼) → 우리 채널 표준명
_CH = {"blog": "블로그", "insta": "인스타", "youtube": "유튜브", "misc": ""}
# activity(참여형태) → 표준 카테고리 폴백. visit 는 빈값(제목으로 분류: 맛집/뷰티/여가).
_ACT_CAT = {"report": "기자단", "delivery": "배송", "purchase": "배송", "visit": ""}
log = logging.getLogger(__name__)


def _to_int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _dday(close_ms) -> Optional[int]:
    if not close_ms:
        return None
    try:
        d = datetime.date.fromtimestamp(close_ms / 1000)
        return (d - datetime.date.today()).days
    except (ValueError, OSError, OverflowError):
        return None


def _to_campaign(x: dict) -> Optional[Campaign]:
    cid = x.get("campaignId")
    if cid is None:
        return None
    title = (x.get("title") or "").strip()
    if not title:
        return None
    # 제목 앞 '[경기/수원]' 의 '/' → 공백(지역 정규화가 인식하도록)
    title = re.sub(r"^\[([^\]]+)\]", lambda m: "[" + m.group(1).replace("/", " ") + "]", title)
    sub = (x.get("subtitle") or "").strip()
    full = (title + (f" {sub}" if sub else "")).strip()
    recruit = _to_int(x.get("reviewerLimit"))
    applicants = _to_int(x.get("currentApplicants"))
    competition = round(applicants / recruit, 1) if recruit and applicants is not None else None
    ch = _CH.get((x.get("channel") or "").lower(), "")
    if (x.get("contentType") or "") == "short-form" and ch == "인스타":
        ch = "릴스"
    img = x.get("mainImg") or ""
    if img.startswith("/"):
        img = BASE + img
    return Campaign(
        site="chvu", site_name="체험뷰", cid=str(cid), title=full,
        url=f"{BASE}/campaign/{cid}", region="",
        category=_ACT_CAT.get((x.get("activity") or "").lower(), ""),
        channel=ch, dday=_dday(x.get("closeAt")),
        applicants=applicants, recruit=recruit, competition=competition, image=img,
    )


class ChvuAdapter(BaseAdapter):
    key = "chvu"
    name = "체험뷰"
    enabled = True
    prunable = True                      # 활성 전체를 매번 완주 → 소스에서 내려간 건 자동 삭제 가능
    min_interval = config.CHVU_MIN_INTERVAL   # 별도 주기(기본 30분)로만 수집

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "Referer": BASE + "/campaign", "Accept": "application/json"}
        now_ms = time.time() * 1000
        log.info("[chvu] 수집 시작...")
        for cat in CATS:
            n0 = len(out)
            for page in range(1, MAX_PAGES + 1):
                try:
                    r = await client.get(LIST.format(cat=cat, page=page, count=COUNT),
                                         headers=headers, timeout=20.0)
                except Exception as e:
                    log.warning("[chvu] %s page %d 실패: %s", cat, page, e)
                    break
                if r.status_code != 200:
                    break
                data = (r.json() or {}).get("data") or []
                if not data:
                    break
                active = [x for x in data if (x.get("closeAt") or 0) > now_ms]
                page_cs = [c for c in (_to_campaign(x) for x in active) if c and c.cid not in seen]
                for c in page_cs:
                    seen.add(c.cid)
                out.extend(page_cs)
                if on_page and page_cs:
                    on_page(page_cs)
                if not active:               # 이 페이지에 활성 없음 → 뒤는 전부 마감
                    break
                await asyncio.sleep(0.3)
            log.info("[chvu] category=%s +%d건 (누적 %d건)", cat, len(out) - n0, len(out))
        log.info("[chvu] 수집 완료 (총 %d건)", len(out))
        return out
