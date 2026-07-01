"""클라우드리뷰(cloudreview.co.kr) 어댑터. jQuery SSR 사이트.

목록: GET /campaign/{blog|buy|delivery}  — 각 페이지에 현재 캠페인 전체가 한 번에 SSR 렌더.
  (page 파라미터 무시 / 무한스크롤 블록은 별도지만 초기 페이지에 전량 포함)
카드(.campaign-image 의 부모): a[/campaign/detail/N], 제목 div.truncate('[카테고리] [지역]제목'),
  부제 a, 'N인 모집'(모집), 'N인 참여'(신청), '#방문형/#배송형/#구매형'(유형), 'N일남음'/'오늘마감', img@data-original.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .base import BaseAdapter, Campaign, guess_region, UA

BASE = "https://www.cloudreview.co.kr"
# (채널 페이지, 우리 채널명). 카테고리·유형은 카드 안(제목 [카테고리] + 유형태그)에 있어 페이지 무관.
PAGES = [("blog", "블로그"), ("instagram", "인스타"), ("short", "릴스"), ("buy", "블로그")]
_ID_RE = re.compile(r"/campaign/detail/(\d+)")
_DAY_RE = re.compile(r"(\d+)\s*일\s*남음")
_REC_RE = re.compile(r"([\d,]+)\s*인\s*모집")
_APP_RE = re.compile(r"([\d,]+)\s*인\s*참여")
# 제목 첫 대괄호 = 소스 카테고리(topic). 소스 9종.
_CATSET = {"맛집", "뷰티", "서비스", "패션", "디지털", "유아", "생활", "식품", "반려동물"}
# 방문형일 때 카테고리 → 우리 표준. 생활=골프·와인 등 여가시설, 패션/유아=체험·대여 → 여가.
_VISIT = {"맛집": "맛집", "식품": "맛집", "뷰티": "뷰티",
          "생활": "여가", "패션": "여가", "유아": "여가",
          "서비스": "기타", "디지털": "기타", "반려동물": "기타"}
_LEAD_CAT_RE = re.compile(r"^\s*\[(맛집|뷰티|서비스|패션|디지털|유아|생활|식품|반려동물)\]\s*")
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _category(title0: str, text: str) -> str:
    """유형 태그 우선(배송형/구매형→배송, 리뷰형=홍보기자단→기자단), 방문형은 제목 [카테고리]로."""
    m = re.match(r"\s*\[([^\]]+)\]", title0)
    cat = m.group(1).strip() if m else ""
    cat = cat if cat in _CATSET else ""
    if "배송형" in text or "구매형" in text:
        return "배송"
    if "리뷰형" in text:
        return "기자단"
    return _VISIT.get(cat, "기타")            # 방문형/유형태그 없음 → 카테고리로


def _parse(html: str, channel: str = "블로그") -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for ci in soup.select(".campaign-image"):
        card = ci.parent
        if card is None:
            continue
        a = card.select_one('a[href*="/campaign/detail/"]')
        if not a:
            continue
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        tdiv = card.select_one("div.truncate")
        title0 = (tdiv.get_text(" ", strip=True) if tdiv else "").strip()
        if not title0:
            continue
        # 부제(이미지 링크가 아닌, 텍스트가 있는 detail 링크)
        sub = ""
        for link in card.select('a[href*="/campaign/detail/"]'):
            t = link.get_text(" ", strip=True)
            if t:
                sub = t
                break
        txt = card.get_text(" ", strip=True)

        if (dm := _DAY_RE.search(txt)):
            dday = _to_int(dm.group(1))
        elif "오늘마감" in txt:
            dday = 0
        else:
            dday = None

        rec = _REC_RE.search(txt)
        app = _APP_RE.search(txt)
        recruit = _to_int(rec.group(1)) if rec else None
        applicants = _to_int(app.group(1)) if app else None
        competition = round(applicants / recruit, 1) if recruit and applicants is not None else None

        img_el = card.select_one("img")
        image = ""
        if img_el:
            image = (img_el.get("data-original") or img_el.get("src") or "").strip()
            if image.startswith("//"):
                image = "https:" + image

        category = _category(title0, txt)
        # 표시용 제목에서 앞쪽 [카테고리] 접두사는 제거([지역]은 유지 → 지역 추출/표시)
        disp = _LEAD_CAT_RE.sub("", title0)
        title = (disp + (f" {sub}" if sub else "")).strip()
        out.append(Campaign(
            site="cloudreview", site_name="클라우드리뷰", cid=cid, title=title,
            url=f"{BASE}/campaign/detail/{cid}",
            region=guess_region(title0), category=category, channel=channel,
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class CloudreviewAdapter(BaseAdapter):
    key = "cloudreview"
    name = "클라우드리뷰"
    enabled = True
    prunable = True          # 각 페이지에 현재 캠페인 전체를 매번 완주 → 소스에서 내려간 건 자동 삭제 가능

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "Referer": BASE + "/"}
        log.info("[cloudreview] 수집 시작...")
        for page, channel in PAGES:
            try:
                html = await self.get(client, f"{BASE}/campaign/{page}", headers=headers)
            except Exception as e:
                log.warning("[cloudreview] %s 요청 실패: %s", page, e)
                continue
            page_new = [c for c in _parse(html, channel) if c.cid not in seen]
            for c in page_new:
                seen.add(c.cid)
            out.extend(page_new)
            if on_page and page_new:
                on_page(page_new)
            log.info("[cloudreview] %s +%d건 (누적 %d건)", page, len(page_new), len(out))
            await asyncio.sleep(0.3)
        log.info("[cloudreview] 수집 완료 (총 %d건)", len(out))
        return out
