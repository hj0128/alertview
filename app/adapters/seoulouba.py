"""서울오빠(seoulouba.co.kr) 어댑터. GnuBoard SSR.

목록: /campaign/?qq=newopen&page=N  (최신순, page=N 페이징)
카드(li.campaign_content): .s_campaign_title(제목·지역 대괄호), .d_day, .recruit(신청/모집),
  채널 img alt(네이버블로그/인스타그램/릴스/클립/유튜브), .tum_img img(썸네일), /campaign/?c=ID
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import BaseAdapter, Campaign, UA

BASE = "https://www.seoulouba.co.kr"
LIST = BASE + "/campaign/?qq=newopen&page={page}"
_ID_RE = re.compile(r"c=(\d+)")
_DAY_RE = re.compile(r"D-(\d+)")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*모집\s*([\d,]+)")
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _channel(li) -> str:
    alts = " ".join(i.get("alt", "") for i in li.select("img[alt]"))
    if "클립" in alts:
        return "클립"
    if "릴스" in alts:
        return "릴스"
    if "인스타" in alts:
        return "인스타"
    if "유튜브" in alts or "쇼츠" in alts:
        return "유튜브"
    return "블로그"


def _region_cand(title: str) -> str:
    """제목의 대괄호 토큰 + 앞쪽 단어 2개를 합쳐 지역 후보 문자열 생성(중앙에서 정규화)."""
    brs = re.findall(r"\[([^\]]+)\]", title)
    lead = re.sub(r"\[[^\]]*\]", " ", title).split()[:2]
    parts = [b.replace("+", " ").replace("/", " ") for b in brs] + lead
    return " ".join(parts).strip()


def _parse(html: str) -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.campaign_content"):
        href = ""
        for a in li.select('a[href*="/campaign/"]'):
            if _ID_RE.search(a.get("href", "")):
                href = a.get("href", "")
                break
        m = _ID_RE.search(href)
        if not m:
            continue
        cid = m.group(1)
        title = (li.select_one(".s_campaign_title").get_text(" ", strip=True)
                 if li.select_one(".s_campaign_title") else "").strip()
        if not title:
            continue

        dday_txt = (li.select_one(".d_day").get_text(" ", strip=True) if li.select_one(".d_day") else "")
        if (dm := _DAY_RE.search(dday_txt)):
            dday = _to_int(dm.group(1))
        elif re.search(r"D-?day|오늘|마감", dday_txt, re.I):
            dday = 0
        else:
            dday = None

        rec_txt = (li.select_one(".recruit").get_text(" ", strip=True) if li.select_one(".recruit") else "")
        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(rec_txt)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        img = li.select_one(".tum_img img")
        image = ""
        if img:
            image = (img.get("src") or img.get("data-src") or "").strip()
            if image.startswith("//"):
                image = "https:" + image

        out.append(Campaign(
            site="seoulouba", site_name="서울오빠", cid=cid, title=title,
            url=f"{BASE}/campaign/?c={cid}",
            region=_region_cand(title), category="", channel=_channel(li),
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class SeouloubaAdapter(BaseAdapter):
    key = "seoulouba"
    name = "서울오빠"
    enabled = True

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        cap = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500
        headers = {"User-Agent": UA, "Referer": BASE + "/"}
        log.info("[seoulouba] 수집 시작...")
        for page in range(1, cap + 1):
            try:
                html = await self.get(client, LIST.format(page=page), headers=headers)
            except Exception as e:
                log.warning("[seoulouba] %d페이지 요청 실패(중단): %s", page, e)
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
            log.info("[seoulouba] %d페이지 +%d건 (누적 %d건)", page, len(page_new), len(out))
            if keep is False:
                break
            await asyncio.sleep(0.3)
        log.info("[seoulouba] 수집 완료 (총 %d건)", len(out))
        return out
