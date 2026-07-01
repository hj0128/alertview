"""강남맛집체험단(강남맛집.net = xn--939au0g4vj8sq.net) 어댑터. GnuBoard 'go' 테마.

목록 AJAX: GET /theme/go/_list_cmp_tpl.php?ca=<카테고리>&rpage=<page>&row_num=28
  - 페이지네이션은 rpage(1-base)+row_num. (startnum 방식은 더 이상 동작 안 함)
  - ca 는 소스의 세부 카테고리 코드. 각 ca 를 순회해 캠페인을 '소스가 분류한 대로' 태깅한다.
카드(.list_item): .tit a[/cp/?id=N](제목, 지역은 제목 대괄호), .sub_tit, .label em(채널),
  .dday(D-day), .numb(신청/모집), img(썸네일).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import BaseAdapter, Campaign, guess_region, UA

BASE = "https://xn--939au0g4vj8sq.net"            # 강남맛집.net
TPL = BASE + "/theme/go/_list_cmp_tpl.php?ca={ca}&rpage={rpage}&row_num=28"
MAX_PAGES = 250                                   # ca 당 안전 상한(빈 페이지면 그 전에 종료; 맛집이 130+page)
# 소스 세부 카테고리(ca) → 우리 표준 카테고리. '소스가 담아둔 분류'를 그대로 따른다.
#   지역(20): 맛집/뷰티/숙박/문화/배달/포장/기타 · 제품(30): 뷰티/패션/식품/생활/기타 · 기자단(40)
# 제품(30xx)은 '택배로 오는 상품'이라 뷰티(화장품)까지 전부 배송. 뷰티 카테고리는 지역>뷰티(뷰티샵 방문)만.
CATS = {
    "2005": "맛집", "2010": "뷰티", "2015": "여가", "2020": "여가",
    "2025": "맛집", "2030": "포장", "2035": "기타",
    "3005": "배송", "3010": "배송", "3015": "배송", "3020": "배송", "3030": "배송",
    "4005": "기자단", "4025": "기자단",
}
_ID_RE = re.compile(r"id=(\d+)")
_DAY_RE = re.compile(r"(\d+)\s*일\s*남음")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*모집\s*([\d,]+)")
_CH = {"blog": "블로그", "clip": "클립", "reels": "릴스", "reel": "릴스",
       "insta": "인스타", "instagram": "인스타", "shorts": "유튜브", "youtube": "유튜브"}
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _channel(it) -> str:
    for em in it.select(".label em"):
        cls = " ".join(em.get("class", []) or [])
        for key, name in _CH.items():
            if key in cls:
                return name
    return "블로그"


def _parse(html: str, category: str) -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for it in soup.select(".list_item"):
        a = it.select_one(".tit a") or it.select_one('a[href*="/cp/?id="]')
        if not a:
            continue
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        title0 = (it.select_one(".tit").get_text(" ", strip=True) if it.select_one(".tit") else "").strip()
        if not title0:
            continue
        sub = (it.select_one(".sub_tit").get_text(" ", strip=True) if it.select_one(".sub_tit") else "").strip()
        short = (sub[:40] + "…") if len(sub) > 41 else sub

        dday_txt = (it.select_one(".dday").get_text(" ", strip=True) if it.select_one(".dday") else "")
        if re.search(r"오늘|마감|D-?day", dday_txt, re.I):
            dday = 0
        elif (dm := _DAY_RE.search(dday_txt)):
            dday = _to_int(dm.group(1))
        else:
            dday = None

        numb = (it.select_one(".numb").get_text(" ", strip=True) if it.select_one(".numb") else "")
        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(numb)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        img_el = it.select_one("img")
        image = ""
        if img_el:
            image = (img_el.get("src") or img_el.get("data-src") or "").strip()
            if image.startswith("//"):
                image = "https:" + image

        title = title0 + (f" {short}" if short else "")
        out.append(Campaign(
            site="gangnam", site_name="강남맛집", cid=cid, title=title.strip(),
            url=f"{BASE}/cp/?id={cid}",
            region=guess_region(title0), category=category, channel=_channel(it),
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class GangnamAdapter(BaseAdapter):
    key = "gangnam"
    name = "강남맛집"
    enabled = True
    prunable = True          # 매 수집마다 ca 별 전체 목록 완주 → 소스에서 내려간 건 자동 삭제 가능

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                   "Referer": BASE + "/cp/"}
        try:
            await client.get(BASE + "/cp/", headers={"User-Agent": UA}, timeout=20.0)
        except Exception as e:
            log.warning("[gangnam] 초기 GET 실패(무시): %s", e)
        log.info("[gangnam] 수집 시작...")
        for ca, cat in CATS.items():
            n0 = len(out)
            for rpage in range(1, MAX_PAGES + 1):
                try:
                    html = await self.get(client, TPL.format(ca=ca, rpage=rpage), headers=headers)
                except Exception as e:
                    log.warning("[gangnam] ca=%s rpage=%d 실패(중단): %s", ca, rpage, e)
                    break
                page_new = [c for c in _parse(html, cat) if c.cid not in seen]
                if not page_new:                 # 빈 페이지 또는 더 이상 새 항목 없음 → 다음 ca
                    break
                for c in page_new:
                    seen.add(c.cid)
                out.extend(page_new)
                if on_page:
                    on_page(page_new)
                await asyncio.sleep(0.3)
            log.info("[gangnam] ca=%s(%s) +%d건 (누적 %d건)", ca, cat, len(out) - n0, len(out))
        log.info("[gangnam] 수집 완료 (총 %d건)", len(out))
        return out
