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
# 소스 카테고리(cat) 필터. /campaign/?cat=<코드>&page=N (약 192건/페이지).
LIST = BASE + "/campaign/?cat={cat}&page={page}"
# (cat 코드, 우리 카테고리). 우선순위 순서 - 앞 cat 에서 잡힌 cid 는 뒤에서 건너뜀(중복제거).
# 방문형 채널 하위(인스타/스레드/클립)는 주제 하위와 겹치므로 뒤에 둬서 '주제'가 이기게 하고,
# 주제 없이 채널로만 분류된 방문형은 기타로. 배송형/기자단은 상위 cat 하나로 전체 커버.
CATS = [
    ("448", "기자단"),   # 기자단
    ("383", "배송"),     # 배송형 전체(식품·뷰티·디지털·패션·생활·유아동·도서·펫·기타…)
    ("378", "맛집"),     # 방문형 맛집
    ("379", "여가"),     # 방문형 여행/숙박
    ("510", "여가"),     # 방문형 스포츠/레저
    ("381", "여가"),     # 방문형 문화/생활
    ("380", "뷰티"),     # 방문형 뷰티/패션
    ("382", "포장"),     # 방문형 테이크아웃
    ("446", "기타"),     # 방문형 기타
    ("449", "배송"),     # 구매평
    ("505", "기타"),     # 방문형 인스타(주제 없는 채널 캠페인)
    ("520", "기타"),     # 방문형 스레드
    ("523", "기타"),     # 방문형 클립
    ("450", "기타"),     # 서비스
]
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


def _parse(html: str, category: str = "") -> List[Campaign]:
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
        # cat 페이지는 최신순이라 앞쪽=활성, 뒤쪽=마감 아카이브(수년치). 마감된 건 수집 제외.
        # (fetch 는 활성 0건 페이지에서 그 cat 중단 → 아카이브 크롤 방지)
        if re.search(r"마감|종료", dday_txt) and "오늘" not in dday_txt:
            continue
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
            region=_region_cand(title), category=category, channel=_channel(li),
            dday=dday, applicants=applicants, recruit=recruit, competition=competition,
            image=image, extra=(f"D-{dday}" if dday is not None else ""),
        ))
    return out


class SeouloubaAdapter(BaseAdapter):
    key = "seoulouba"
    name = "서울오빠"
    enabled = True
    prunable = True          # cat 전체(방문형/배송형/기자단/구매평/서비스)를 완주 → 소스에서 내려간 건 자동 삭제

    async def fetch(self, client: httpx.AsyncClient, on_page=None) -> List[Campaign]:
        out, seen = [], set()
        cap = config.DQ_MAX_PAGES if config.DQ_MAX_PAGES > 0 else 500
        headers = {"User-Agent": UA, "Referer": BASE + "/"}
        log.info("[seoulouba] 수집 시작...")
        for cat, our in CATS:
            n0 = len(out)
            cat_seen: set = set()            # 이 cat 안에서 본 cid(끝/루프 판정용; 전역 dedup 과 분리)
            for page in range(1, cap + 1):
                try:
                    html = await self.get(client, LIST.format(cat=cat, page=page), headers=headers)
                except Exception as e:
                    log.warning("[seoulouba] cat=%s p%d 요청 실패(중단): %s", cat, page, e)
                    break
                cards = _parse(html, our)
                if not cards:
                    break
                cids = {c.cid for c in cards}
                if cids <= cat_seen:         # 이 cat 에서 새 cid 없음 = 끝(또는 페이징 루프)
                    break
                cat_seen |= cids
                page_new = [c for c in cards if c.cid not in seen]   # 전역 중복제거(우선순위 앞 cat 이 이김)
                for c in page_new:
                    seen.add(c.cid)
                out.extend(page_new)
                if on_page and page_new:
                    on_page(page_new)
                await asyncio.sleep(0.3)
            log.info("[seoulouba] cat=%s(%s) +%d건 (누적 %d건)", cat, our, len(out) - n0, len(out))
        log.info("[seoulouba] 수집 완료 (총 %d건)", len(out))
        return out
