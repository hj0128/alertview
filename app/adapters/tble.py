"""티블(tble.kr) 어댑터. jQuery SSR.

목록: GET /category.php?type={l|d|p|c|r|shorts}  (타입별 현재 목록 전체, page 파라미터 없음)
  l=지역(방문) · d=배송(영수증) · p=제품 · c=구매평 · r=기자단 · shorts=숏폼
카드(.item): .sns_img <class>(채널), .info > a[?cp_id, title], .ps_remain(D-day),
  .t2(제목, 지역 대괄호), .t4('신청 N 명 / 모집 M명'), 썸네일.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .base import BaseAdapter, Campaign, guess_region, UA

BASE = "https://tble.kr"
LIST = BASE + "/category.php?type={type}"
LIST_CA = BASE + "/category.php?type={type}&ca={ca}"
# (type, ca, 우리 카테고리). 소스 그대로: 방문(l)은 세부 ca 주제로, 제품(p)·영수증(d)·구매평(c)은 배송,
# 기자단(r)은 기자단. 숏폼은 소스 주제가 없어 제목분류(빈값 → record_campaign). 캠페인 중복은 cid 로 제거.
CATS = [
    ("r", None, "기자단"),
    ("p", None, "배송"),        # 제품(뷰티 화장품 포함 전부 배송)
    ("c", None, "배송"),        # 구매평(온라인 구매 제품)
    ("l", "맛집", "맛집"),      # 방문 세부(소스 주제) - 영수증보다 먼저 잡아 topic 우선
    ("l", "뷰티샵", "뷰티"),
    ("l", "숙박", "여가"),
    ("l", "문화", "여가"),
    ("l", "스페셜", "기타"),
    ("l", "기타", "기타"),
    ("d", None, ""),            # 영수증 = 매장 방문+영수증리뷰(음식점·카페·마사지) → 제목 분류(배송 아님)
    ("l", None, ""),            # 방문 중 세부 ca 없는 leftover → 제목 분류(누락 방지)
    ("shorts", None, ""),       # 숏폼: 소스 주제 없음 → 제목 분류
]
_ID_RE = re.compile(r"cp_id=(\d+)")
_DAY_RE = re.compile(r"(\d+)\s*일\s*남음")
_APPLY_RE = re.compile(r"신청\s*([\d,]+).*?모집\s*([\d,]+)")
_CH = {"blog": "블로그", "clip": "클립", "insta": "인스타", "reels": "릴스",
       "shorts": "숏츠", "ytube": "유튜브", "youtube": "유튜브", "cafe": ""}
log = logging.getLogger(__name__)


def _to_int(s) -> Optional[int]:
    try:
        return int(str(s).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse(html: str, category: str) -> List[Campaign]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select("div.item"):
        info = item.select_one(".info")
        if not info:
            continue                       # 배너 등 캠페인 아닌 item 제외
        a = info.select_one("a[href*='cp_id']")
        if not a:
            continue
        m = _ID_RE.search(a.get("href", ""))
        if not m:
            continue
        cid = m.group(1)
        t2 = info.select_one(".t2")
        title = (t2.get_text(" ", strip=True) if t2 else a.get("title", "")).strip()
        if not title:
            continue

        rem = info.select_one(".ps_remain")
        rtxt = rem.get_text(" ", strip=True) if rem else ""
        if (dm := _DAY_RE.search(rtxt)):
            dday = _to_int(dm.group(1))
        elif "오늘마감" in rtxt or "오늘 마감" in rtxt or "마감" in rtxt:
            dday = 0
        else:
            dday = None

        applicants = recruit = competition = None
        t4 = info.select_one(".t4")
        if t4 and (am := _APPLY_RE.search(t4.get_text(" ", strip=True))):
            applicants, recruit = _to_int(am.group(1)), _to_int(am.group(2))
            if recruit:
                competition = round(applicants / recruit, 1)

        ch = ""
        sns = item.select_one(".sns_img")
        if sns:
            for cls in sns.get("class", []):
                if cls in _CH:
                    ch = _CH[cls]
                    break

        img = item.select_one(".img_in img") or item.select_one("img")
        image = ""
        if img:
            s = (img.get("src") or img.get("data-src") or "").strip()
            if s.startswith("//"):
                image = "https:" + s
            elif s.startswith("/"):
                image = BASE + s
            else:
                image = s

        # 카테고리는 소스(type+ca) 그대로 사용. 빈값(숏폼)만 record_campaign 이 제목으로 분류.
        out.append(Campaign(
            site="tble", site_name="티블", cid=cid, title=title,
            url=f"{BASE}/view.php?cp_id={cid}",
            region=guess_region(title), category=category, channel=ch,
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class TbleAdapter(BaseAdapter):
    key = "tble"
    name = "티블"
    enabled = True
    prunable = True          # 타입별 현재 목록 전체를 매번 완주 → 소스에서 내려간 건 자동 삭제 가능

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "Referer": BASE + "/"}
        log.info("[tble] 수집 시작...")
        from urllib.parse import quote
        for typ, ca, cat in CATS:
            url = LIST_CA.format(type=typ, ca=quote(ca)) if ca else LIST.format(type=typ)
            try:
                html = (await client.get(url, headers=headers, timeout=20.0)).text
            except Exception as e:
                log.warning("[tble] type=%s ca=%s 요청 실패: %s", typ, ca, e)
                continue
            page_new = [c for c in _parse(html, cat) if c.cid not in seen]
            for c in page_new:
                seen.add(c.cid)
            out.extend(page_new)
            if on_page and page_new:
                on_page(page_new)
            log.info("[tble] type=%s ca=%s +%d건 (누적 %d건)", typ, ca, len(page_new), len(out))
            await asyncio.sleep(0.3)
        log.info("[tble] 수집 완료 (총 %d건)", len(out))
        return out
