"""링블(ringble.co.kr) 어댑터. 구형 테이블 레이아웃 SSR.

목록: /category.php?category=ID  (카테고리별 전체, page 파라미터 없음 - 한 번에 전체 반환)
  대분류: 829 제품 / 832 방문 / 1015 인스타 / 833 유튜브 / 834 기자단
  → 각 대분류의 '세부 category 코드'를 순회해 소스 분류 그대로 우리 8종에 매핑.
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
# (세부 category 코드, 우리 카테고리, 기본 채널). 우선순위 순으로 중복제거(주제 리프가 채널 리프보다 먼저).
# 제품(택배 상품)은 화장품 포함 전부 배송, 서비스만 기타. 방문은 주제로. 인스타/유튜브 방문은 주제 없어 기타.
CATS = [
    # 방문(832) 세부 → 주제 (블로그, 제목에 채널표기 있으면 override)
    ("1041", "맛집", "블로그"),   # 카페
    ("890", "맛집", "블로그"),    # 음식점
    ("891", "뷰티", "블로그"),    # 뷰티(방문 뷰티샵)
    ("893", "여가", "블로그"),    # 숙박
    ("970", "여가", "블로그"),    # 체험
    ("1042", "여가", "블로그"),   # 사진관
    ("1043", "여가", "블로그"),   # 운동
    ("1003", "기타", "블로그"),   # 방문 기타
    # 제품(829) 세부 → 배송 (서비스만 기타)
    ("850", "배송", "블로그"),    # 화장품/미용
    ("852", "배송", "블로그"),    # 식품
    ("853", "배송", "블로그"),    # 생활용품
    ("854", "배송", "블로그"),    # 패션잡화
    ("1012", "배송", "블로그"),   # 디지털/가전
    ("1038", "배송", "블로그"),   # 가구인테리어
    ("1039", "배송", "블로그"),   # 반려동물
    ("1027", "배송", "블로그"),   # 육아용품
    ("1037", "배송", "블로그"),   # 도서
    ("1040", "기타", "블로그"),   # 서비스 → 기타
    # 인스타(1015) 세부 (채널 인스타)
    ("1028", "배송", "인스타"),   # 제품
    ("1051", "배송", "인스타"),   # 구매평
    ("1052", "배송", "인스타"),   # 구매평-포스팅
    ("1031", "기자단", "인스타"), # 기자단
    ("1029", "기타", "인스타"),   # 방문(주제 없음)
    # 유튜브(833) 세부 (채널 유튜브)
    ("900", "배송", "유튜브"),    # 제품
    ("902", "기자단", "유튜브"),  # 기자단
    ("901", "기타", "유튜브"),    # 방문(주제 없음)
    # 기자단(834) 단독
    ("834", "기자단", "블로그"),
    # 대분류 캐치올(세부에서 못 잡힌 leftover 누락 방지)
    ("829", "배송", "블로그"),    # 제품 전체
    ("832", "기타", "블로그"),    # 방문 전체(주제 미상)
    ("1015", "기타", "인스타"),   # 인스타 전체
    ("833", "기타", "유튜브"),    # 유튜브 전체
]
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


def _parse(html: str, default_ch: str, category: str = "") -> List[Campaign]:
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
            region=guess_region(title), category=category, channel=ch,
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class RingbleAdapter(BaseAdapter):
    key = "ringble"
    name = "링블"
    enabled = True
    prunable = True          # 카테고리별 전체 목록 완주 → 소스에서 내려간 건 자동 삭제 가능

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        headers = {"User-Agent": UA, "Referer": BASE + "/"}
        log.info("[ringble] 수집 시작...")
        for cat, our, ch in CATS:
            try:
                html = (await client.get(LIST.format(cat=cat), headers=headers, timeout=20.0)).text
            except Exception as e:
                log.warning("[ringble] cat %s 요청 실패: %s", cat, e)
                continue
            page_new = [c for c in _parse(html, ch, our) if c.cid not in seen]
            for c in page_new:
                seen.add(c.cid)
            out.extend(page_new)
            if on_page and page_new:
                on_page(page_new)
            log.info("[ringble] cat %s(%s) +%d건 (누적 %d건)", cat, our, len(page_new), len(out))
            await asyncio.sleep(0.3)
        log.info("[ringble] 수집 완료 (총 %d건)", len(out))
        return out
