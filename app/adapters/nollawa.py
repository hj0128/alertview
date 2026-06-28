"""놀러와체험단(cometoplay.kr) 어댑터.

item_list.php?category_id=...&page=N 형태로 페이징되는 서버 렌더링 목록을 파싱.
카드 텍스트 예:
  '[경기 의왕] 인기폭발... [오매기744] #태그 D-day 3 신청 0 명 / 모집 10 명'
캠페인 식별자는 링크의 it_id.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .. import config
from .base import BaseAdapter, Campaign, guess_region, guess_in, CATEGORIES

BASE = "https://www.cometoplay.kr"
# 사이트의 실제 서브카테고리(category_id)로 크롤 → 사이트 분류를 그대로 사용(추측 X).
# (category_id, 우리 표준 카테고리). 001=지역, 002=제품, 004=기자단.
CATS = [
    ("001012", "맛집"),
    ("001013", "뷰티"),
    ("001014", "여가"),    # 숙박
    ("001015", "여가"),    # 문화
    ("001016", "배달"),
    ("001017", "기타"),    # 지역-기타
    ("002006", "배송"),    # 생활
    ("002007", "배송"),    # 디지털
    ("002008", "배송"),    # 패션
    ("002009", "뷰티"),    # 제품-뷰티
    ("002010", "배송"),    # 식품
    ("002011", "기타"),    # 제품-기타
    ("004", "기자단"),
]
_ID_RE = re.compile(r"it_id=(\d+)")
_DDAY_RE = re.compile(r"D-?day\s*(\d+)")
_APPLY_RE = re.compile(r"신청\s*([\d,]+)\s*명\s*/\s*모집\s*([\d,]+)\s*명")
log = logging.getLogger(__name__)
_IMG_ATTRS = ["data-src", "data-original", "data-lazy-src", "src"]


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _card_node(anchor):
    """앵커에서 위로 올라가며 '신청 X 명 / 모집 Y 명'이 1건인 가장 작은 조상(카드) 반환."""
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
            if not v or "data:image" in v:
                continue
            if "/skin/" in v:          # 로고·아이콘(scrap_ic/end_ico 등) 제외
                continue
            # 썸네일은 './data/list/thumb/..' 같은 상대경로 → 절대 URL로 변환
            # (기존엔 '/'·'http' 로 시작하는 것만 처리해 './' 썸네일을 통째로 놓쳤음)
            full = urljoin(BASE + "/", v)
            if full.startswith("http"):
                return full
    return ""


def _parse_page(html: str, category: str = "") -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    # 1) it_id별 썸네일 수집: 썸네일은 텍스트와 다른(이미지 전용) 앵커에 들어있다.
    img_by_cid: dict = {}
    for a in soup.select('a[href*="it_id="]'):
        m = _ID_RE.search(a.get("href", ""))
        if not m or m.group(1) in img_by_cid:
            continue
        im = _pick_img(a)
        if im:
            img_by_cid[m.group(1)] = im
    # 2) 통계가 있는 앵커에서 캠페인 정보 추출
    out, seen = [], set()
    for a in soup.select('a[href*="it_id="]'):
        txt = a.get_text(" ", strip=True)
        am = _APPLY_RE.search(txt)
        if not am:                     # 통계 없는 앵커(썸네일/찜 버튼) 건너뜀
            continue
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        if cid in seen:
            continue
        seen.add(cid)

        title = txt
        cut = title.find("D-day")
        if cut < 0:
            cut = title.find("D-Day")
        if cut > 0:
            title = title[:cut].strip()
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue

        url = f"{BASE}/item.php?it_id={cid}"
        image = img_by_cid.get(cid, "") or _pick_img(_card_node(a))
        dday = _to_int(mm.group(1)) if (mm := _DDAY_RE.search(txt)) else None
        applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
        competition = round(applicants / recruit, 1) if recruit else None
        out.append(Campaign(
            site="nollawa", site_name="놀러와체험단", cid=cid, title=title, url=url,
            region=guess_region(title), category=category,
            channel="블로그",
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class NollawaAdapter(BaseAdapter):
    key = "nollawa"
    name = "놀러와체험단"
    enabled = True

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        limit = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500  # 0=끝까지(상한 500)
        log.info("[nollawa] 수집 시작...")
        for cat_id, category in CATS:
            for page in range(1, limit + 1):
                url = (f"{BASE}/item_list.php?category_id={cat_id}"
                       f"&sst=it_datetime&sod=desc&page={page}")
                try:
                    html = await self.get(client, url)
                except Exception as e:
                    # 부분 수집을 '완료'로 오인하지 않도록 중단하지 말고 전파(다음 수집에서 재시도)
                    log.warning("[nollawa] cat %s p%d 요청 실패(중단): %s", cat_id, page, e)
                    raise
                page_new = [c for c in _parse_page(html, category) if c.cid not in seen]
                for c in page_new:
                    seen.add(c.cid)
                out.extend(page_new)
                keep = True
                if on_page and page_new:
                    keep = on_page(page_new)
                log.info("[nollawa] cat %s(%s) p%d +%d건 (누적 %d건)",
                         cat_id, category, page, len(page_new), len(out))
                if not page_new:
                    break                      # 이 카테고리 마지막 페이지
                if keep is False:
                    break                      # 새 캠페인 없음 → 다음 카테고리로
                await asyncio.sleep(0.3)
        log.info("[nollawa] 수집 완료 (총 %d건)", len(out))
        return out
