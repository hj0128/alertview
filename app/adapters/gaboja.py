"""가보자체험단(가보자체험단.com = xn--o39a04kpnjo4k9hgflp.com) 어댑터.

목록은 AJAX(POST)로 HTML 조각을 받아 렌더한다(무한스크롤, page 0-base):
  POST /main/ajax/_ajax.cmpSubList.php
    body: ct1, ct2, channel, sst(정렬), stx(검색), page, list(페이지당), ...
  → <li class="list_item"> 카드들. 빈 응답이면 끝.
ct1 없음은 '지역(11)'만 주므로 카테고리 3종(11 지역 / 10 제품 / 14 기자단)을 각각 훑는다.
카드: a[href*='?id=N'](상세), i.class(채널 blog/insta/…), <span>유형, 'N일 남음'/'오늘마감',
  <dt>제목(지역 대괄호), 신청 X / 모집 Y, 썸네일.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .base import BaseAdapter, Campaign, UA

BASE = "https://xn--o39a04kpnjo4k9hgflp.com"
LIST = BASE + "/main/ajax/_ajax.cmpSubList.php"
CATS = ["11", "10", "14"]          # 지역 / 제품 / 기자단
PER_PAGE = 50
MAX_PAGES = 60                     # 카테고리당 안전 상한(빈 응답이면 그 전에 종료)
_ID_RE = re.compile(r"[?&]id=(\d+)")
_DAY_RE = re.compile(r"(\d+)\s*일\s*남음")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*모집\s*([\d,]+)")
# 채널 아이콘 클래스 → 우리 채널 표준명. 미상은 빈값(피드에서 '기타').
_CH = {"blog": "블로그", "insta": "인스타", "instagram": "인스타", "youtube": "유튜브",
       "clip": "클립", "reels": "릴스", "shorts": "숏츠", "tiktok": "틱톡"}
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse(html: str) -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a[href*='?id=']"):
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        dt = a.select_one("dt")
        title = dt.get_text(" ", strip=True) if dt else (a.select_one("img") or {}).get("alt", "")
        title = (title or "").strip()
        if not title:
            continue
        ctext = a.get_text(" ", strip=True)

        if (dm := _DAY_RE.search(ctext)):
            dday = _to_int(dm.group(1))
        elif "오늘마감" in ctext or "오늘 마감" in ctext:
            dday = 0
        else:
            dday = None

        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(ctext)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        ch = ""
        icon = a.select_one(".cate i[class]")
        if icon:
            for cls in icon.get("class", []):
                if cls in _CH:
                    ch = _CH[cls]
                    break
        # 유형(방문형/배송형/기자단형) = 카테고리 분류 힌트
        type_span = a.select_one(".cate span")
        ctype = type_span.get_text(strip=True) if type_span else ""

        img = a.select_one("img")
        image = ""
        if img:
            src = img.get("src") or ""
            image = BASE + src if src.startswith("/") else src

        out.append(Campaign(
            site="gaboja", site_name="가보자체험단", cid=cid, title=title,
            url=f"{BASE}/cmp/?id={cid}", region="", category=ctype, channel=ch,
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image,
        ))
    return out


class GabojaAdapter(BaseAdapter):
    key = "gaboja"
    name = "가보자체험단"
    enabled = True

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "Referer": BASE + "/cmp/",
                   "X-Requested-With": "XMLHttpRequest"}
        log.info("[gaboja] 수집 시작...")
        for ct1 in CATS:
            for page in range(0, MAX_PAGES):
                data = {"ct1": ct1, "ct2": "", "channel": "", "sst": "", "stx": "",
                        "page": page, "list": PER_PAGE, "lc": "", "st": "", "sf": "", "empty": "0"}
                try:
                    r = await client.post(LIST, data=data, headers=headers, timeout=20.0)
                except Exception as e:
                    log.warning("[gaboja] ct1=%s page %d 실패: %s", ct1, page, e)
                    break
                if r.status_code != 200 or not r.text.strip():
                    break
                page_cs = [c for c in _parse(r.text) if c.cid not in seen]
                if not page_cs:
                    break
                for c in page_cs:
                    seen.add(c.cid)
                out.extend(page_cs)
                if on_page:
                    on_page(page_cs)
                await asyncio.sleep(0.3)
            log.info("[gaboja] ct1=%s 누적 %d건", ct1, len(out))
        log.info("[gaboja] 수집 완료 (총 %d건)", len(out))
        return out
