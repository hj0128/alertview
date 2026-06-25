"""링블(ringble.co.kr) 어댑터. 구형 테이블 레이아웃 SSR.

목록: /category.php?category=ID  (카테고리별 전체, page 파라미터 없음)
  카테고리: 829 제품 / 832 방문 / 1015 인스타 / 833 유튜브 / 834 기자단
카드(카드별 table): 제목 링크(지역 대괄호) /detail.php?number=N, 'N일 남음'/'오늘 마감',
  '신청 X / 모집 Y', 썸네일 ./mallimg/...
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

BASE = "https://www.ringble.co.kr"
LIST = BASE + "/category.php?category={cat}"
CATS = [("829", "블로그"), ("832", "블로그"), ("1015", "인스타"),
        ("833", "유튜브"), ("834", "블로그")]
_ID_RE = re.compile(r"number=(\d+)")
_DAY_RE = re.compile(r"(\d+)\s*일\s*남음")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*모집\s*([\d,]+)")
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _card_of(a):
    node = a
    for _ in range(9):
        node = node.parent
        if node is None:
            break
        t = node.get_text(" ", strip=True)
        if "신청" in t and "모집" in t:
            return node
    return None


def _parse(html: str, default_ch: str) -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select('a[href*="number="]'):
        title = a.get_text(" ", strip=True)
        if len(title) <= 3 or re.search(r"남음|마감", title):
            continue                          # 썸네일/디데이 앵커 제외 → 제목 앵커만
        m = _ID_RE.search(a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        cid = m.group(1)
        card = _card_of(a)
        if card is None:
            continue
        seen.add(cid)
        ctext = card.get_text(" ", strip=True)

        if (dm := _DAY_RE.search(ctext)):
            dday = _to_int(dm.group(1))
        elif "오늘 마감" in ctext or "오늘마감" in ctext:
            dday = 0
        else:
            dday = None

        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(ctext)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        image = ""
        for img in card.find_all("img"):
            s = img.get("src") or img.get("data-src") or ""
            if "mallimg" in s or "/upload" in s or "/data" in s:
                image = BASE + s[1:] if s.startswith("./") else (BASE + s if s.startswith("/") else s)
                break

        ch = default_ch
        if "인스타" in title:
            ch = "인스타"
        elif "릴스" in title:
            ch = "릴스"
        elif "유튜브" in title or "쇼츠" in title:
            ch = "유튜브"
        elif "클립" in title:
            ch = "클립"

        out.append(Campaign(
            site="ringble", site_name="링블", cid=cid, title=title,
            url=f"{BASE}/detail.php?number={cid}",
            region=guess_region(title), category="", channel=ch,
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class RingbleAdapter(BaseAdapter):
    key = "ringble"
    name = "링블"
    enabled = True

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "Referer": BASE + "/"}
        log.info("[ringble] 수집 시작...")
        for cat, ch in CATS:
            try:
                html = (await client.get(LIST.format(cat=cat), headers=headers, timeout=20.0)).text
            except Exception as e:
                log.warning("[ringble] cat %s 요청 실패: %s", cat, e)
                continue
            page_new = [c for c in _parse(html, ch) if c.cid not in seen]
            for c in page_new:
                seen.add(c.cid)
            out.extend(page_new)
            if on_page and page_new:
                on_page(page_new)
            log.info("[ringble] cat %s +%d건 (누적 %d건)", cat, len(page_new), len(out))
            await asyncio.sleep(0.3)
        log.info("[ringble] 수집 완료 (총 %d건)", len(out))
        return out
