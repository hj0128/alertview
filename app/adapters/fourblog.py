"""포블로그(4blog.net) 어댑터. jQuery SSR + 무한스크롤(JSON API).

목록: GET /loadMoreDataCategory?offset=N&limit=L&category=all&category1=&location_param=&location1=&search=&bid=
  → JSON 배열. category=all 이면 방문(local)·배송(deliv)·기자단(reporter) 전부 섞여 옴.
  항목: CID, PRID, CAMPAIGN_NM, LOCATION_NM('[수원시/인계동]'·'[제품/배송]'), CATEGORY(채널 blog/insta/…),
        CATEGORY1(local/deliv/reporter), REVIEWER_CNT(모집), REVIEWER_REQ_CNT(신청), REMAINDATE(D-day), IMGKEY.
빈 응답이면 끝. 상세: /campaign/{CID}/
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx

from .base import BaseAdapter, Campaign, UA

BASE = "https://4blog.net"
LIST = BASE + "/loadMoreDataCategory?offset={offset}&limit={limit}&category=all&category1=&location_param=&location1=&search=&bid="
IMG = "https://d3oxv6xcx9d0j1.cloudfront.net/public/pr/{prid}/thumbnail/{key}"
LIMIT = 100
MAX_PAGES = 100
# CATEGORY1(소스 구분) → 우리 표준 카테고리. local(방문)은 제목으로 분류하게 빈값.
_CAT1 = {"deliv": "배송", "reporter": "기자단", "local": ""}
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError, TypeError):
        return None


def _channel(c: str) -> str:
    c = (c or "").lower()
    if "blog" in c:
        return "블로그"
    if "insta" in c:
        return "인스타"
    if "reel" in c:
        return "릴스"
    if "short" in c or "youtube" in c or "유튜브" in c:
        return "유튜브"
    if "clip" in c:
        return "클립"
    return ""


def _to_campaign(x: dict) -> Optional[Campaign]:
    cid = x.get("CID")
    if cid is None:
        return None
    name = (x.get("CAMPAIGN_NM") or "").strip()
    if not name:
        return None
    loc_raw = (x.get("LOCATION_NM") or "").strip()                 # '[수원시/인계동]' 등
    loc = re.sub(r"[\[\]]", "", loc_raw).replace("/", " ").strip()  # → '수원시 인계동'
    cat1 = (x.get("CATEGORY1") or "").lower()
    title = ((loc_raw + " " if loc_raw else "") + name).strip()
    # 4blog 은 음식/업종 카테고리가 없음 → 제목+키워드(해시태그)+제공내역으로 분류.
    # PAYBACK(환급금액) 있으면 페이백. deliv→배송, reporter→기자단(고정).
    # local(방문)은 분류하되, 못 잡으면 맛집 기본값(방문형 대부분 식당).
    if _to_int(x.get("PAYBACK")):
        category = "페이백"
    elif cat1 == "deliv":
        category = "배송"
    elif cat1 == "reporter":
        category = "기자단"
    else:
        from ..matcher import classify          # 지연 import(순환 참조 방지)
        # 방문(local): 제목+해시태그+제공내역으로 분류. 못 잡으면 기타(억지로 맛집으로 넣지 않음).
        blob = " ".join([name, x.get("KEYWORD") or "", x.get("REVIEWER_BENEFIT") or ""])
        category = classify(blob)
    recruit = _to_int(x.get("REVIEWER_CNT"))
    applicants = _to_int(x.get("REVIEWER_REQ_CNT"))
    competition = round(applicants / recruit, 1) if recruit and applicants is not None else None
    img = ""
    if x.get("IMGKEY") and x.get("PRID") is not None:
        img = IMG.format(prid=x["PRID"], key=x["IMGKEY"])
    return Campaign(
        site="4blog", site_name="포블로그", cid=str(cid), title=title,
        url=f"{BASE}/campaign/{cid}/",
        region=loc if cat1 == "local" else "",
        category=category, channel=_channel(x.get("CATEGORY")),
        dday=_to_int(x.get("REMAINDATE")),
        applicants=applicants, recruit=recruit, competition=competition, image=img,
    )


class FourblogAdapter(BaseAdapter):
    key = "4blog"
    name = "포블로그"
    enabled = True
    prunable = True          # category=all 로 현재 열린 목록 전체를 완주 → 소스에서 내려간 건 자동 삭제 가능

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                   "Referer": BASE + "/list/all", "Accept": "application/json"}
        log.info("[4blog] 수집 시작...")
        for page in range(MAX_PAGES):
            url = LIST.format(offset=page * LIMIT, limit=LIMIT)
            try:
                r = await client.get(url, headers=headers, timeout=20.0)
            except Exception as e:
                log.warning("[4blog] offset=%d 실패: %s", page * LIMIT, e)
                break
            if r.status_code != 200:
                break
            try:
                data = r.json()
            except Exception:
                break
            if not data:
                break
            page_cs = [c for c in (_to_campaign(x) for x in data) if c and c.cid not in seen]
            if not page_cs:
                break
            for c in page_cs:
                seen.add(c.cid)
            out.extend(page_cs)
            if on_page:
                on_page(page_cs)
            log.info("[4blog] offset=%d +%d건 (누적 %d건)", page * LIMIT, len(page_cs), len(out))
            await asyncio.sleep(0.3)
        log.info("[4blog] 수집 완료 (총 %d건)", len(out))
        return out
