"""강남맛집체험단(강남맛집.net = xn--939au0g4vj8sq.net) 어댑터.

cometoplay 와 같은 GnuBoard 'go' 테마. 목록은 AJAX 템플릿으로 페이징.
  GET /theme/go/_list_cmp_tpl.php?startnum=<offset>&endnum=<개수>
응답 카드(.list_item): .tit a[/cp/?id=N](제목, 지역은 제목 대괄호), .sub_tit,
  .label em(채널/유형), .dday(D-day), .numb(신청/모집), img(썸네일 //gangnam-review.net/...)
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
TPL = BASE + "/theme/go/_list_cmp_tpl.php?startnum={start}&endnum={cnt}"
PAGE = 30
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


def _parse(html: str) -> List[Campaign]:
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
            region=guess_region(title0), category="", channel=_channel(it),
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class GangnamAdapter(BaseAdapter):
    key = "gangnam"
    name = "강남맛집"
    enabled = True

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        cap = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500
        headers = {"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                   "Referer": BASE + "/cp/"}
        try:
            await client.get(BASE + "/cp/", headers={"User-Agent": UA}, timeout=20.0)
        except Exception as e:
            log.warning("[gangnam] 초기 GET 실패(무시): %s", e)
        log.info("[gangnam] 수집 시작...")
        start = 0
        for _ in range(cap):
            try:
                html = await self.get(client, TPL.format(start=start, cnt=PAGE), headers=headers)
            except Exception as e:
                log.warning("[gangnam] start=%d 요청 실패(중단): %s", start, e)
                raise
            cards = _parse(html)
            if not cards:
                break
            page_new = [c for c in cards if c.cid not in seen]
            for c in page_new:
                seen.add(c.cid)
            out.extend(page_new)
            keep = True
            if on_page and page_new:
                keep = on_page(page_new)
            log.info("[gangnam] start=%d +%d건 (누적 %d건)", start, len(page_new), len(out))
            start += len(cards)            # 실제 반환 개수만큼 전진(누락 방지)
            if keep is False:
                break
            await asyncio.sleep(0.3)
        log.info("[gangnam] 수집 완료 (총 %d건)", len(out))
        return out
