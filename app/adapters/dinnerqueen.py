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
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*/\s*모집\s*([\d,]+)")
_DDAY_RE = re.compile(r"D-(\d+)")
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


def _parse_page(html: str) -> List[Campaign]:
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
        dday = _to_int(mm.group(1)) if (mm := _DDAY_RE.search(blob)) else None
        applicants = recruit = competition = None
        if (am := _APPLY_RE.search(blob)):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)
        out.append(Campaign(
            site="dinnerqueen", site_name="디너의여왕", cid=cid, title=title, url=url,
            region=guess_region(title), category=guess_in(blob, CATEGORIES),
            channel=("클립" if "클립" in blob else "릴스" if "릴스" in blob else "블로그"),
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class DinnerQueenAdapter(BaseAdapter):
    key = "dinnerqueen"
    name = "디너의여왕"

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        limit = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500  # 0=끝까지(안전상한 500)
        log.info("[dinnerqueen] 수집 시작...")
        for page in range(1, limit + 1):
            url = LIST_URL if page == 1 else f"{LIST_URL}&page={page}"
            try:
                html = await self.get(client, url)
            except Exception as e:
                log.warning("[dinnerqueen] %d\ud398\uc774\uc9c0 \uc694\uccad \uc2e4\ud328(%s): %s", page, url, e)
                break
            page_new = [c for c in _parse_page(html) if c.cid not in seen]
            for c in page_new:
                seen.add(c.cid)
            out.extend(page_new)
            keep = True
            if on_page and page_new:
                keep = on_page(page_new)  # 즉시 저장 + 계속 여부
            log.info("[dinnerqueen] %d페이지 +%d건 (누적 %d건)", page, len(page_new), len(out))
            if not page_new:
                log.info("[dinnerqueen] 마지막 페이지 → 종료 (총 %d건)", len(out))
                break
            if keep is False:
                log.info("[dinnerqueen] 새 캠페인 없음 → 조기 종료 (%d건 확인)", len(out))
                break
            await asyncio.sleep(0.3)
        return out
