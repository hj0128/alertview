"""디너의여왕 어댑터 (실제 동작). ?page=N 페이징 + 카드에서 상세/이미지 추출."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional, Tuple

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import BaseAdapter, Campaign, guess_region, guess_in, CHANNELS, CATEGORIES

BASE = "https://dinnerqueen.net"
LIST_URL = f"{BASE}/taste?ct=전체"
_ID_RE = re.compile(r"/taste/(\d+)")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*(?:모집\s*)?([\d,]+)")
_DDAY_RE = re.compile(r"D-(\d+)|(\d+)\s*일\s*남음")
log = logging.getLogger(__name__)
_IMG_ATTRS = ["data-src", "data-original", "data-lazy-src", "data-lazy", "src"]


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _card_node(anchor):
    """앵커에서 위로 올라가며 '신청 X / 모집 Y' 가 1건인 가장 작은 조상(카드)을 반환."""
    node = anchor
    for _ in range(6):
        node = getattr(node, "parent", None)
        if node is None:
            break
        if len(_APPLY_RE.findall(node.get_text(" ", strip=True))) == 1:
            return node
    return None


def _pick_img(node) -> str:
    if node is None:
        return ""
    for img in node.find_all("img"):
        for attr in _IMG_ATTRS:
            v = (img.get(attr) or "").strip()
            if v.startswith("//"):
                v = "https:" + v
            if v.startswith("http") and "data:image" not in v:
                return v
    return ""


def _parse_page(html: str, category: str = "") -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select('a[href*="/taste/"]'):
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        if cid in seen:
            continue
        raw = (a.get("title") or "").strip()
        if not raw.endswith("신청하기"):
            continue
        title = re.sub(r"\s*신청하기$", "", raw).strip()
        if not title:
            continue
        seen.add(cid)
        href = a.get("href", "")
        url = href if href.startswith("http") else f"{BASE}{href}"
        node = _card_node(a)
        blob = node.get_text(" ", strip=True) if node else title
        image = _pick_img(node) or _pick_img(a)
        # 디너의여왕 D-day 는 실제보다 1 적게 표기 → +1 보정('오늘마감'→D-1, 'D-1'→D-2 …)
        if (mm := _DDAY_RE.search(blob)):
            dday = _to_int(mm.group(1) or mm.group(2))
            if dday is not None:
                dday += 1
        elif "D'day" in blob or "D-day" in blob or "Dday" in blob or "오늘마감" in blob:
            dday = 1          # 오늘 마감 → D-1
        else:
            dday = None
        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(blob)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)
        out.append(Campaign(
            site="dinnerqueen", site_name="디너의여왕", cid=cid, title=title, url=url,
            region=guess_region(title), category=category,
            channel=("클립" if "클립" in blob else "릴스" if "릴스" in blob else "블로그"),
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class DinnerQueenAdapter(BaseAdapter):
    key = "dinnerqueen"
    name = "디너의여왕"
    prunable = True
    CATS = ["맛집", "여가", "뷰티", "페이백", "기자단", "배송"]

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        limit = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500  # 0=끝까지(안전상한 500)
        log.info("[dinnerqueen] 수집 시작...")
        for ct in self.CATS:
            n0 = len(out)
            for page in range(1, limit + 1):
                from urllib.parse import quote
                url = f"{BASE}/taste?ct={quote(ct)}" + ("" if page == 1 else f"&page={page}")
                try:
                    html = await self.get(client, url)
                except Exception as e:
                    log.warning("[dinnerqueen] ct=%s p%d fail: %s", ct, page, e)
                    raise
                page_new = [c for c in _parse_page(html, ct) if c.cid not in seen]
                for c in page_new:
                    seen.add(c.cid)
                out.extend(page_new)
                if on_page and page_new:
                    on_page(page_new)
                if not page_new:
                    break
                await asyncio.sleep(0.3)
            log.info("[dinnerqueen] ct=%s acc %d", ct, len(out))
        return out
